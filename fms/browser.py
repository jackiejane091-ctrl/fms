"""Browser drivers: a common interface with a real Playwright driver and a mock.

- BaseDriver: login(account) / post_listing(payload) / close().
- PlaywrightDriver: drives a real Chromium via Playwright with a per-account
  persistent context (own cookies/storage), per-account proxy, and per-account
  fingerprint options (user-agent, locale, timezone, viewport).
- MockDriver: no browser at all; simulates posting with sleeps and records every
  call. Used by the automated tests (and the dashboard dry-run).
"""
from __future__ import annotations

import random
import re
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from . import config, human, selectors as S
def _ensure_visible(page, locator, timeout=3000):
    """Smart scroll: only scroll if element is not visible.
    
    User's idea: don't hardcode scroll per field. Check visibility first.
    Returns True if element is visible (after optional scroll).
    """
    try:
        if locator.is_visible(timeout=1000):
            return True
    except Exception:
        pass
    try:
        locator.scroll_into_view_if_needed(timeout=timeout)
        return True
    except Exception:
        return False

def _listing_timing_target(payload: dict) -> tuple[float, float, float]:
    """Return normalized per-listing min/max and a random soft target.

    User-configured timing values are targets only: callers must never treat
    max_seconds_to_post as a deadline or a reason to stop the listing.
    """
    try:
        minimum = max(0.0, float(payload.get("min_seconds_to_post", 25)))
    except (TypeError, ValueError):
        minimum = 25.0
    try:
        maximum = max(0.0, float(payload.get("max_seconds_to_post", 40)))
    except (TypeError, ValueError):
        maximum = 40.0
    if maximum < minimum:
        minimum, maximum = maximum, minimum
    return minimum, maximum, random.uniform(minimum, maximum)

# A small pool of desktop user agents to rotate per account.
# v0.2.32: NO user-agent override at all. The bundled browser is Chrome for
# Testing 156 — any UA string we invent (even a Chrome one) risks mismatching
# navigator.userAgentData, which still reports the REAL version. Facebook
# compares the two. Letting the browser report its own honest UA keeps the
# fingerprint self-consistent (UA string <-> client hints <-> platform).

# Screen-friendly sizes: the browser WINDOW is sized to the viewport so it
# never opens bigger than the laptop screen.
VIEWPORTS = [(1280, 720), (1366, 768), (1440, 900)]
LOCALES = ["en-GB", "en-US"]
# v0.2.31: no more random timezones. London/New York clocks on a Pakistani IP
# made Facebook flag the session and serve a limited header (missing
# notifications/messages/profile icons). The browser now always uses the
# laptop's REAL system timezone (timezone_id is simply not overridden).


# v0.2.33: stealth init script — hide the remaining automation traces so
# Facebook sees a normal human-driven browser (anti "suspected automated
# behavior"). Runs in every page before any site script.
_STEALTH_JS = """() => {
  // navigator.webdriver -> undefined (belt & braces with the blink flag)
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  } catch (e) {}
  // plugins: real Chrome ships a few; an empty list is a bot tell
  try {
    const plugs = [
      { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
        description: 'Portable Document Format' },
      { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
        description: '' },
      { name: 'Native Client', filename: 'internal-nacl-plugin',
        description: '' },
    ];
    plugs.item = (i) => plugs[i] || null;
    plugs.namedItem = (n) => plugs.find((p) => p.filename === n) || null;
    Object.defineProperty(navigator, 'plugins', { get: () => plugs });
  } catch (e) {}
  // languages: consistent with the context locale (__LOCALE__)
  try {
    Object.defineProperty(navigator, 'languages',
      { get: () => ['__LOCALE__', 'en-US'] });
  } catch (e) {}
  // permissions: don't leak automation via notification-permission probes
  try {
    const orig = window.navigator.permissions.query.bind(
      window.navigator.permissions);
    window.navigator.permissions.query = (params) =>
      params && params.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : orig(params);
  } catch (e) {}
}"""


class BaseDriver(ABC):
    """Interface the listing engine programs against."""

    def __init__(self, account: dict):
        self.account = account

    @abstractmethod
    def login(self) -> bool:
        """Log the account in (or reuse its session). Return True on success."""

    @abstractmethod
    def post_listing(self, payload: dict) -> str | None:
        """Post one listing. Return the Facebook listing id, or None on failure.

        payload keys: title, price, description, category, attributes (dict),
        condition, availability, location, tags, image_paths (list), video_path.
        """

    def human_warmup(self) -> None:
        """Brief human-like feed browsing before the first post (v0.2.33).

        A human doesn't log in and instantly open the create-listing page;
        they scroll the feed a little first. Default: no-op (MockDriver).
        """
        return None

    # -- Phase 2: drafts, manual-publish, relist/edit, scrap, activities --------
    def create_draft(self, payload: dict) -> str | None:
        """Fill the form but SAVE AS DRAFT instead of publishing.
        Return the draft id, or None on failure."""
        raise NotImplementedError

    def publish_draft(self, draft_id: str) -> bool:
        """Publish a previously saved draft. Return True on success."""
        raise NotImplementedError

    def reset_list_publish(self) -> None:
        """Reset the simple list-order publish session (tried-card memory)."""
        raise NotImplementedError

    def publish_next_draft_from_list(self) -> dict:
        """Publish ONE draft from the loaded Drafts list, in page order.

        Returns {"ok": bool, "label": str|None, "reason": str|None,
        "empty": bool}. ok=True means Publish was clicked (no Facebook
        confirmation check, per user design). empty=True means the list
        has no untried draft card left.
        """
        raise NotImplementedError

    def fill_listing_form(self, payload: dict) -> str | None:
        """Fill one listing form in a NEW tab WITHOUT publishing (manual-publish
        helper). Return a tab handle, or None on failure."""
        raise NotImplementedError

    def get_my_listings(self) -> list[dict]:
        """Return the account's existing listings: [{id, title}]."""
        raise NotImplementedError

    def edit_listing(self, listing_id: str, payload: dict) -> bool:
        """Open the Edit page for listing_id, rewrite fields, save."""
        raise NotImplementedError

    def scrape_listing(self, url: str) -> dict | None:
        """Extract public data from one Marketplace listing URL.
        Returns {title, price, description, photo_urls, location, availability}
        or None on failure."""
        raise NotImplementedError

    # -- Alignment (v6.1.2.0): delete / renew / location / download actions ---
    # The driver records a `keep_open` flag for Browser Launch: when True the
    # engine must NOT close the browser (it stays open for manual work).
    keep_open: bool = False

    def delete_listing(self, listing_id: str) -> bool:
        """Delete one listing by id. Return True on success."""
        raise NotImplementedError

    def delete_drafts(self) -> int:
        """Delete all saved drafts. Return the number deleted."""
        raise NotImplementedError

    def delete_duplicate_listings(self) -> list[str]:
        """Find duplicate listings (same normalized title) and delete all but
        the first of each group. Return the deleted listing ids."""
        raise NotImplementedError

    def renew_listing(self, listing_id: str) -> bool:
        """Renew one active listing. Return True on success."""
        raise NotImplementedError

    def renew_listings_from_page(self, max_count: int) -> dict:
        """Renew up to max_count listings from Facebook's Renew Listings page."""
        raise NotImplementedError

    def delete_relist_from_rows(self, max_count: int) -> dict:
        """Delete & Relist UI action contract."""
        raise NotImplementedError

    def renew_listings_from_rows(self, max_count: int) -> dict:
        """Renew listings by finding each row's own clickable Renew button; no ID parsing."""
        raise NotImplementedError

    def set_marketplace_location(self, location: str) -> bool:
        """Change the account's Marketplace location. Return True on success."""
        raise NotImplementedError

    def download_listing_assets(self, url: str, dest_dir: str) -> dict | None:
        """Scrape one listing URL and download its photos into dest_dir.
        Returns {title, photo_paths} or None on failure."""
        raise NotImplementedError

    def open_marketplace(self) -> None:
        """Navigate the main page to Marketplace (Download Images & Titles)."""
        raise NotImplementedError

    def download_open_tabs(self, dest_dir: str) -> list[dict]:
        """Download images + titles from every open tab except the first.
        Returns [{title, photo_paths}]."""
        raise NotImplementedError

    def get_take_action_listings(self) -> list[dict]:
        """Listings Facebook flagged with a 'take action' (identity/phone
        verification) requirement. Returns [{id, title}]. Best-effort."""
        raise NotImplementedError

    # Human-like activity blocks (used by script runs). Durations are in
    # real minutes/seconds; drivers should keep them interruptible.
    def scroll_feed(self, minutes: float, probs: dict | None = None) -> None:
        raise NotImplementedError

    def watch_reels(self, minutes: float, probs: dict | None = None) -> None:
        raise NotImplementedError

    def watch_videos(self, minutes: float, probs: dict | None = None) -> None:
        raise NotImplementedError

    def scroll_marketplace(self, minutes: float, probs: dict | None = None) -> None:
        raise NotImplementedError

    def read_notifications(self, seconds: float,
                           probs: dict | None = None) -> None:
        raise NotImplementedError

    def scroll_messages(self, seconds: float,
                        probs: dict | None = None) -> None:
        raise NotImplementedError

    def goto_messages(self) -> None:
        raise NotImplementedError

    def launch_fast(self) -> None:
        """Open the browser session with minimal checks (Browser Launch
        Fast). Base implementation is a no-op; MockDriver records it."""
        return None

    @abstractmethod
    def close(self) -> None:
        """Release the browser."""


# ------------------------------------------------------------- real driver ---
class PlaywrightDriver(BaseDriver):
    """Real driver using Playwright + persistent per-account browser profile."""

    def __init__(self, account: dict, headless: bool = False):
        super().__init__(account)
        self.headless = headless
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._listing_tabs = []
        self._listing_tab_index = 0
        # Consumed by the first listing so its timer can include browser startup.
        self._pending_browser_start_time = None
        # v0.2.49: set by the engine — called the instant the browser window
        # is closed so the account flips to Failed immediately.
        self._on_close_callback = None
        # v0.2.56: path of the most recent self-diagnosis screenshot (None
        # when no failure was captured yet). Never sensitive: it's a page
        # picture inside the user's own data dir, surfaced in their Logs.
        self._last_screenshot: str | None = None

    # -- self-diagnosis ---------------------------------------------------------
    def _snap(self, page, reason: str) -> str | None:
        """Screenshots are intentionally disabled; keep the call site compatible."""
        return None

    def _note_field_error(self, page, reason: str, msg: str) -> None:
        """Record a form-field failure AND save a screenshot of the page.

        Replaces bare ``self._last_form_error = msg`` assignments so every
        failure the user sees in the Logs comes with a picture they can
        forward instead of taking one themselves (v0.2.56).

        v0.2.66: accumulate ALL errors in _all_field_errors (not just last).
        """
        # Keep diagnostic text without creating or referencing screenshot files.
        self._last_form_error = msg
        # Accumulate for the final report
        if not hasattr(self, "_all_field_errors"):
            self._all_field_errors = []
        self._all_field_errors.append(f"{reason}: {msg}")

    def _handle_context_close(self) -> None:
        """Fires the moment the browser window/context closes (user ×)."""
        cb = getattr(self, "_on_close_callback", None)
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    # -- typing / ixbrowser config (injected by the engine per run) ------------
    def _typing_cfg(self) -> "human.TypingConfig":
        return human.TypingConfig.from_dict(self.account.get("typing"))

    def _type(self, locator, text, hybrid_ok: bool = False) -> None:
        """Type into a field honoring the run's typing mode setting."""
        human.type_text(locator, text or "", self._typing_cfg(),
                        hybrid_ok=hybrid_ok)

    def _ix_cfg(self) -> dict:
        """IXBrowser config injected by the engine; {} when disabled."""
        cfg = self.account.get("ixbrowser") or {}
        return cfg if cfg.get("enabled") else {}

    # -- setup ----------------------------------------------------------------
    def _proxy_config(self) -> dict | None:
        proxy = self.account.get("proxy")
        if not proxy:
            return None
        cfg: dict[str, Any] = {
            "server": f"http://{proxy['host']}:{proxy['port']}",
        }
        if proxy.get("username"):
            cfg["username"] = proxy["username"]
            cfg["password"] = proxy.get("password") or ""
        return cfg

    def _maximize_window(self) -> None:
        """Maximize the launched Chromium window before any workflow actions.

        Uses the Chromium DevTools window-state API for both bundled Chrome and
        CDP-connected IXBrowser. Failures are non-fatal; they must not block a run.
        """
        page = self._page
        context = self._context
        if page is None or context is None:
            return
        try:
            session = context.new_cdp_session(page)
            try:
                info = session.send("Browser.getWindowForTarget")
                window_id = info.get("windowId")
                if window_id is not None:
                    session.send("Browser.setWindowBounds", {
                        "windowId": window_id,
                        "bounds": {"windowState": "maximized"},
                    })
            finally:
                try:
                    session.detach()
                except Exception:
                    pass
        except Exception:
            # --start-maximized remains enabled for the bundled Chrome path.
            pass

    def _launch(self) -> None:
        # Remember this browser's opening start so the first listing timer can
        # include startup/login/loading time. Later listings get fresh timers.
        self._pending_browser_start_time = time.monotonic()
        # IXBrowser (optional): route through its Local API when
        # the run enabled it. Disabled -> bundled Chrome exactly as before.
        if self._ix_cfg():
            self._launch_ixbrowser()
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        profile_dir = config.profiles_dir() / f"account_{self.account['id']}"
        proxy = self._proxy_config()
        # Deterministic-ish fingerprint per account (stable across runs).
        # v0.2.25: the group's "Apply Fingerprint" switch can turn the
        # randomization off (fixed default instead).
        # v0.2.31: timezone is NEVER randomized — always the laptop's real
        # system clock (see the note above).
        # v0.2.32: user-agent is NEVER overridden — the browser reports its
        # own honest UA so it matches navigator.userAgentData.
        if self.account.get("apply_fingerprint", True):
            rng = random.Random(self.account["id"])
            loc = rng.choice(LOCALES)
        else:
            loc = "en-GB"
        # v0.2.45: no forced 1440x900 viewport/window. A fixed viewport wider
        # than the user's real screen CUT OFF the page's right edge (FB's
        # notifications/Messenger icons) — and maximizing the OS window did
        # NOT help because the Playwright viewport stayed 1440. Now the
        # browser opens maximized and the page uses the real window size,
        # like a normal human-driven browser (also the most honest
        # fingerprint — v0.2.32 principle). Slight cascade so parallel
        # account browsers don't stack pixel-exact on top of each other.
        cascade = (self.account.get("id") or 0) % 5
        self._context = self._pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=self.headless,
            proxy=proxy,
            locale=loc,
            # v0.2.31: no timezone_id -> the laptop's real system timezone,
            # so Facebook sees a consistent clock for the proxy's location.
            # v0.2.32: no user_agent override -> honest self-consistent UA.
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized",
                  f"--window-position={40 * cascade},{30 * cascade}"],
        )
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        self._maximize_window()
        # v0.2.49: watch for the user closing the window — the engine marks
        # the account Failed the instant this fires (no 8-10s stall).
        try:
            self._context.on("close", self._handle_context_close)
        except Exception:
            pass
        # v0.2.33: stealth init script so Facebook sees a human-driven
        # browser, not automation ("we suspected automated behavior").
        try:
            self._context.add_init_script(
                _STEALTH_JS.replace("__LOCALE__", loc))
        except Exception:
            pass

    # -- IXBrowser (anti-detect, optional) -------------------------------------
    IX_GROUP = "FBMRobo"  # profiles FMS creates are placed in this IX group

    def _launch_ixbrowser(self) -> None:
        """Connect Playwright to an IXBrowser profile via its Local API.

        Per-account Profile ID comes from Manage Accounts; empty means
        auto-create on first launch (placed in the FBMRobo group).
        Raises RuntimeError with a clear message when IXBrowser is
        unreachable — the engine turns that into a failed account, not a
        crashed run.
        """
        import json as _json
        import urllib.error
        import urllib.parse
        import urllib.request

        cfg = self._ix_cfg()
        host = cfg.get("host") or "127.0.0.1"
        port = int(cfg.get("port") or 53200)
        base = f"http://{host}:{port}"
        profile_id = cfg.get("profile_id") or self.account.get("ix_profile_id")

        def _call(method: str, path: str, payload: dict | None = None):
            data = _json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(
                base + path, data=data, method=method,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return _json.loads(resp.read().decode() or "{}")
            except urllib.error.URLError as exc:
                raise RuntimeError(
                    f"IXBrowser is not reachable at {host}:{port} "
                    f"({exc}). Install IXBrowser, enable its Local API, "
                    f"and retry — or turn IXBrowser off to use Chrome."
                ) from exc

        if not profile_id:
            # Auto-create a profile on first launch, in the FBMRobo group.
            created = _call("POST", "/api/v2/browser/create", {
                "name": f"FMS-{self.account.get('label', 'account')}",
                "groupName": self.IX_GROUP,
            })
            profile_id = (
                (created.get("data") or {}).get("id")
                or created.get("id")
                or created.get("profileId")
            )
            if not profile_id:
                raise RuntimeError(
                    "IXBrowser did not return a profile id on auto-create; "
                    "set the per-account Profile ID in Manage Accounts."
                )
            # Let the engine persist it back to the account.
            self.account["ix_profile_id_created"] = profile_id

        opened = _call(
            "GET",
            "/api/v2/browser/open?" + urllib.parse.urlencode({"id": profile_id}),
        )
        data = opened.get("data") or opened
        endpoint = (
            data.get("wsEndpoint") or data.get("webSocketDebuggerUrl")
            or data.get("debuggerUrl")
        )
        if not endpoint and data.get("port"):
            endpoint = f"http://{host}:{data['port']}"
        if not endpoint:
            raise RuntimeError(
                "IXBrowser opened the profile but gave no debug endpoint; "
                "check the IXBrowser Local API version."
            )

        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        # IXBrowser profiles carry their own proxy/fingerprint; connect only.
        self._browser = self._pw.chromium.connect_over_cdp(endpoint)
        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else self._browser.new_context()
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        self._maximize_window()
        # v0.2.49: same close watcher as the main launch path.
        try:
            self._context.on("close", self._handle_context_close)
        except Exception:
            pass

    def _bind_pause_checkpoint(self, page=None) -> None:
        """Bind the run's Pause All callback to a page for shared UI helpers."""
        page = page or self._page
        callback = getattr(self, "_pause_checkpoint_callback", None)
        if page is not None and callable(callback):
            try:
                setattr(page, "_fms_pause_checkpoint", callback)
            except Exception:
                pass

    def _pause_checkpoint(self) -> None:
        """Wait at a safe UI-action boundary when the owning run is paused."""
        self._bind_pause_checkpoint()
        callback = getattr(self, "_pause_checkpoint_callback", None)
        if callable(callback):
            callback()

    def _ensure(self) -> None:
        if self._page is None:
            self._launch()
        self._bind_pause_checkpoint()
        # Every public browser operation enters through _ensure; this gives
        # all modes a common checkpoint even when they use separate workflows.
        self._pause_checkpoint()

    def prepare_listing_tabs(self, count: int = 1) -> int:
        """Prepare a finite pool of logged-in tabs for sequential listing work.

        Tabs are reused one at a time; this method does not publish concurrently.
        The create-item page is loaded in each tab before the queue starts.
        """
        self._ensure()
        if self._context is None:
            return 1
        try:
            requested = max(1, int(count or 1))
        except (TypeError, ValueError):
            requested = 1
        # Avoid accidentally exhausting memory/browser resources from a typo.
        requested = min(requested, 20)
        existing = [pg for pg in (self._listing_tabs or []) if not pg.is_closed()]
        if not existing:
            existing = [self._page] if self._page is not None and not self._page.is_closed() else []
        while len(existing) < requested:
            try:
                existing.append(self._context.new_page())
            except Exception:
                break
        self._listing_tabs = existing[:requested] or [self._page]
        for tab in self._listing_tabs:
            self._bind_pause_checkpoint(tab)
        for idx, tab in enumerate(self._listing_tabs):
            try:
                if not tab.url.startswith("https://www.facebook.com/marketplace/create/item"):
                    tab.goto(S.MKT_CREATE_ITEM_URL, wait_until="domcontentloaded", timeout=45000)
                try:
                    tab.wait_for_load_state("load", timeout=15000)
                except Exception:
                    # Facebook can keep subresources active; DOM readiness is enough
                    # to begin the existing form-opening routine.
                    tab.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception as exc:
                print(f"[Tabs Without Limit] tab {idx + 1} pre-load warning: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        self.activate_listing_tab(0)
        return len(self._listing_tabs)

    def activate_listing_tab(self, index: int) -> bool:
        """Select one prepared tab; callers must process listings sequentially."""
        self._ensure()
        tabs = [pg for pg in (self._listing_tabs or []) if not pg.is_closed()]
        if not tabs:
            tabs = [self._page] if self._page is not None and not self._page.is_closed() else []
        if not tabs:
            return False
        self._listing_tabs = tabs
        self._listing_tab_index = int(index) % len(tabs)
        self._page = tabs[self._listing_tab_index]
        self._bind_pause_checkpoint(self._page)
        self._pause_checkpoint()
        try:
            self._page.bring_to_front()
        except Exception:
            pass
        return True

    # -- login -----------------------------------------------------------------
    def _is_logged_in(self) -> bool:
        try:
            S.to_playwright(self._page, S.LOGGED_IN_INDICATOR).wait_for(timeout=8000)
            return True
        except Exception:
            return False

    def _find_login_field(self, page, fallbacks, timeout_each: int = 5000):
        """Try each login selector in order; return the first visible locator.

        v0.2.26: fast pass first — every selector gets only a short wait
        (600ms). A selector that is simply absent used to burn the full
        5s timeout, which caused the ~10s stall between filling the
        password and clicking Log in. The full-timeout pass still runs
        afterwards for slow-loading pages.
        """
        last_exc = None
        for timeout in (600, timeout_each):
            for sel in fallbacks:
                try:
                    loc = S.to_playwright(page, sel)
                    loc.wait_for(state="visible", timeout=timeout)
                    return loc
                except Exception as exc:
                    last_exc = exc
                    continue
        if last_exc:
            raise last_exc
        raise RuntimeError("No login selector matched.")

    def login(self) -> bool:
        """Email/password login with robust submit + outcome detection.

        On failure, ``self.login_error`` holds a human-readable reason so the
        run log shows WHY instead of a silent stall (v0.2.23).
        """
        self._ensure()
        self.login_error = ""
        page = self._page
        assert page is not None

        def fail(reason: str) -> bool:
            self.login_error = reason
            return False

        cookie = self.account.get("cookie")
        if cookie:
            # Cookie format: "name=value; name2=value2" for .facebook.com
            try:
                cookies = []
                for part in cookie.split(";"):
                    part = part.strip()
                    if "=" in part:
                        name, value = part.split("=", 1)
                        cookies.append({"name": name.strip(), "value": value.strip(),
                                        "domain": ".facebook.com", "path": "/"})
                if cookies:
                    self._context.add_cookies(cookies)
            except Exception:
                pass
            page.goto(S.MARKETPLACE_URL, wait_until="domcontentloaded")
            human.human_pause()
            if self._is_logged_in():
                return True
            # Cookie stale -> fall through to email/password.

        password = self.account.get("password")
        label = self.account.get("label", "")
        if not password:
            return False
        page.goto(S.LOGIN_URL, wait_until="domcontentloaded")
        human.human_pause()
        # v0.2.42: the browser profile may ALREADY be logged in (persistent
        # session) — facebook.com/login then redirects straight to the feed
        # and no login form ever appears. Check first; only fill the form
        # when we are genuinely logged out. Without this, an already-logged-in
        # account fails 4x with "could not fill login form" and never posts.
        if self._is_logged_in():
            return True
        # v0.2.44: extra safety — if Facebook redirected us OFF the login
        # page entirely, the session is authenticated even when the
        # indicator selector misses (FB UI drift). Checkpoints excluded.
        url_now = (page.url or "").lower()
        if ("facebook.com" in url_now and "/login" not in url_now
                and "checkpoint" not in url_now):
            return True
        try:
            email = self._find_login_field(page, S.LOGIN_EMAIL_FALLBACKS)
            self._type(email, label)
            human.human_pause(100, 250)
            pass_field = self._find_login_field(page, S.LOGIN_PASS_FALLBACKS)
            self._type(pass_field, password)
            # v0.2.43: user asked for fast login — submit via Enter the moment
            # the password is filled. No pause, no slow mouse move to the
            # button. Button click stays as fallback if Enter doesn't submit.
            try:
                pass_field.press("Enter")
            except Exception:
                pass
            human.human_pause(2000, 3000)
            if "login" in (page.url or "").lower() and not self._is_logged_in():
                try:
                    human.human_click(
                        page, self._find_login_field(page, S.LOGIN_BUTTON_FALLBACKS))
                except Exception:
                    pass
                human.human_pause(2500, 4000)
        except Exception as exc:
            # v0.2.28: keep the real error text so the engine can tell a
            # closed browser apart from a bad form.
            return fail(f"could not fill login form: {type(exc).__name__}: {exc}")

        # Outcome detection: poll for up to ~60s and classify what happened
        # instead of silently stalling (v0.2.23).
        deadline = time.time() + 60
        twofa_handled = False
        while time.time() < deadline:
            if self._is_logged_in():
                return True
            url = (page.url or "").lower()
            try:
                body_text = (page.inner_text("body") or "")[:4000]
            except Exception:
                body_text = ""
            low = body_text.lower()
            # Wrong credentials
            if ("the password that you've entered is incorrect" in low
                    or "wrong credentials" in low
                    or "the email address or mobile number you entered" in low
                    or "isn't connected to an account" in low):
                return fail("Facebook rejected the credentials (wrong email/password)")
            # Suspicious-login / checkpoint interstitials
            if ("we noticed an unusual login" in low
                    or "suspicious login" in low
                    or "confirm your identity" in low
                    or "was this you" in low):
                try:
                    cont = S.to_playwright(page, S.CHECKPOINT_CONTINUE)
                    cont.wait_for(state="visible", timeout=4000)
                    human.human_click(page, cont)
                    human.human_pause(3000, 5000)
                    continue
                except Exception:
                    return fail("Facebook checkpoint needs manual review "
                                "(unusual-login screen) — open this account once "
                                "in a normal browser and approve it")
            # 2FA code prompt
            if not twofa_handled:
                try:
                    code_input = S.to_playwright(page, S.TWOFA_CODE_INPUT)
                    code_input.wait_for(state="visible", timeout=3000)
                    twofa_handled = True
                    secret = self.account.get("twofa_secret")
                    if secret:
                        import pyotp
                        self._type(code_input, pyotp.TOTP(secret).now())
                        human.human_click(page, S.to_playwright(page, S.TWOFA_SUBMIT))
                        human.human_pause(2000, 4000)
                        continue
                    return fail("Facebook asks for a 2FA code but no 2FA secret "
                                "is stored for this account")
                except Exception:
                    pass  # no 2FA prompt (yet)
            # Still sitting on the login page with no error -> submit didn't go through
            if "login" in url:
                human.human_pause(3000, 4000)
                continue
            human.human_pause(2000, 3000)
        if self._is_logged_in():
            return True
        return fail("login did not complete within 60s (still not logged in)")

    # -- posting ----------------------------------------------------------------
    def _location_candidate_identity(self, page, loc) -> tuple[bool, str]:
        """Prove a resolved element is the Location field's own input.

        Read-only. Returns (ok, description). ok=True only when the
        element's aria-label/placeholder mentions 'location', or a field
        row above it is captioned 'Location...'. This is the guard that
        makes it impossible to type the location into Title (or any other
        field): an unprovable candidate is rejected, never typed into.
        """
        try:
            info = loc.evaluate(
                """el => {
                    const out = {
                        tag: el.tagName || '',
                        type: el.getAttribute('type') || '',
                        role: el.getAttribute('role') || '',
                        aria: el.getAttribute('aria-label') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        rowLabel: '',
                        locRow: false,
                    };
                    let node = el;
                    for (let i = 0; i < 7; i++) {
                        node = node.parentElement;
                        if (!node) break;
                        const t = (node.innerText || '').trim();
                        if (t && !out.rowLabel) {
                            out.rowLabel = t.split('\\n')[0].slice(0, 60);
                        }
                        if (/^location\\b/i.test(t)) {
                            out.locRow = true;
                            break;
                        }
                    }
                    return out;
                }""")
        except Exception as exc:
            return False, f"identity unreadable ({type(exc).__name__})"
        if not isinstance(info, dict):
            return False, "identity unreadable (no data)"
        desc = (f"tag={info.get('tag')} type={info.get('type')} "
                f"role={info.get('role') or '-'} "
                f"aria={info.get('aria') or '-'} "
                f"row={info.get('rowLabel') or '-'}")
        aria = (info.get("aria") or "").lower()
        ph = (info.get("placeholder") or "").lower()
        if "location" in aria or "location" in ph or info.get("locRow"):
            return True, desc
        return False, desc

    def _find_location_input_in_section(self, page):
        """Find the Location field's input by section membership.

        Searches from each visible 'Location' label: climb to the
        SMALLEST container that holds a visible text input; that
        container must not also hold the Title or Price inputs (their
        row captions betray them), and the candidate must FOLLOW the
        label inside the container. The found element is marked with a
        data attribute and returned as a locator. Unlike the caption
        check, this works even when Facebook renders the input with no
        label text of its own nearby. Returns (locator, description) or
        (None, diagnostic).
        """
        try:
            outcome = page.evaluate(
                """() => {
                    const MARK = 'data-fms-location-input';
                    document.querySelectorAll('[' + MARK + ']').forEach(
                        e => e.removeAttribute(MARK));
                    const isVis = el => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        const cs = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 &&
                               cs.display !== 'none' &&
                               cs.visibility !== 'hidden';
                    };
                    const caption = el => {
                        let n = el;
                        for (let i = 0; i < 6; i++) {
                            n = n.parentElement;
                            if (!n) break;
                            const t = (n.innerText || '').trim();
                            if (t) return t.split('\\n')[0].slice(0, 40);
                        }
                        return '';
                    };
                    const notes = [];
                    const spans = Array.from(
                        document.querySelectorAll('span')).filter(
                        s => (s.textContent || '').trim() === 'Location'
                             && isVis(s));
                    for (const span of spans) {
                        let node = span;
                        for (let d = 0; d < 8; d++) {
                            node = node.parentElement;
                            if (!node) break;
                            const inputs = Array.from(node.querySelectorAll(
                                'input[type="text"]')).filter(isVis);
                            if (!inputs.length) continue;
                            const caps = inputs.map(caption);
                            if (caps.some(c => /^title\\b/i.test(c) ||
                                               /^price\\b/i.test(c))) {
                                notes.push('section too broad: it also '
                                           + 'holds Title/Price');
                                break;
                            }
                            const cand = inputs.find(el =>
                                span.compareDocumentPosition(el) &
                                Node.DOCUMENT_POSITION_FOLLOWING);
                            if (cand) {
                                cand.setAttribute(MARK, '1');
                                return {found: true,
                                        caption: caption(cand),
                                        aria: cand.getAttribute(
                                            'aria-label') || '',
                                        placeholder: cand.getAttribute(
                                            'placeholder') || ''};
                            }
                            notes.push('section inputs exist but none '
                                       + 'follow the Location label');
                            break;
                        }
                    }
                    return {found: false, notes};
                }""")
        except Exception as exc:
            return None, f"section search failed ({type(exc).__name__})"
        if not isinstance(outcome, dict) or not outcome.get("found"):
            notes = "; ".join((outcome or {}).get("notes") or [])
            return None, ("Location input not found inside its section"
                          + (f" ({notes})" if notes else ""))
        desc = (f"tag=INPUT type=text role=- "
                f"aria={outcome.get('aria') or '-'} "
                f"row={outcome.get('caption') or '-'} (section match)")
        return page.locator('[data-fms-location-input="1"]').first, desc

    def _select_location(self, page, location: str) -> tuple[bool, str]:
        # v0.2.55: extra care (user's explicit instruction). Type the FULL
        # location text, wait for the suggestion list, then click the FIRST
        # suggestion — never pick blindly.
        # v0.2.99 (location-only fix): (a) resolve the input via a cascade —
        # the long-standing "Location field not found" timeouts show the
        # label-based locator alone does not reliably resolve this control
        # (its sibling dropdowns use span-anchored lookups; the saved DOM
        # shows Location as a section pattern, not a label-wrapped input).
        # Primary stays F_LOCATION; the fallback anchors on the "Location"
        # text and takes the text input inside the nearest container that
        # holds one (the F_PRICE pattern). (b) return (ok, step-detail)
        # instead of swallowing the failing step, so callers can log it per
        # account. (c) verify the field's value after the suggestion click.
        # v0.2.102 (targeted fix, user-approved): identity-guarded
        # resolution. The v0.2.99 fallback climbed from the 'Location'
        # label to the nearest ancestor containing ANY text input and
        # took its FIRST input — when the Location input had not rendered
        # yet, that climb reached the whole form panel and selected the
        # TITLE input (live evidence 2026-10-20: page scrolled to top,
        # title erased/replaced by the location text, log detail
        # 'suggestions: no suggestion list appeared'). Now every candidate
        # must PROVE it is the Location field's own input (see
        # _location_candidate_identity) before anything is clicked or
        # typed, and the fallback may only take the first text input
        # FOLLOWING the 'Location' label in document order — Title and
        # Price precede the label, so they can never be reached.
        loc = None
        ident = ""
        try:
            cand = S.to_playwright(page, S.F_LOCATION)
            cand.wait_for(timeout=4000)
            if cand.is_visible():
                _ok_id, _desc = self._location_candidate_identity(page, cand)
                if _ok_id:
                    loc, ident = cand, _desc
        except Exception:
            loc = None
        if loc is None:
            # v0.2.103: section-scoped search (the caption-based identity
            # check alone rejected the real input on Facebook's current
            # layout — the field was never touched at all). Brief render
            # wait, then a safe, diagnosed failure. Never climbs to the
            # panel, never considers Title/Price.
            _desc2 = ""
            for _attempt in range(8):
                cand2, _desc2 = self._find_location_input_in_section(page)
                if cand2 is not None:
                    loc, ident = cand2, _desc2
                    break
                try:
                    page.wait_for_timeout(750)
                except Exception:
                    break
            if loc is None:
                self._note_field_error(
                    page, "location-field",
                    f"Location input not identified: {_desc2}")
                return False, f"field: {_desc2}"
        # Clear then type the full text.
        try:
            loc.click()
            human.human_pause(200, 400)
            loc.fill("")
            human.human_pause(200, 400)
        except Exception:
            pass
        # Diagnostic (v0.2.102): which element actually holds focus now —
        # recorded in the step details so a wrong target can never hide.
        focus_desc = ""
        try:
            focus_desc = page.evaluate(
                """() => { const a = document.activeElement;
                    if (!a) return 'none';
                    return (a.tagName || '') + '/' +
                           (a.getAttribute('type') || '') + '/' +
                           (a.getAttribute('aria-label') || ''); }""") or ""
        except Exception:
            focus_desc = ""
        self._type(loc, location)
        human.human_pause(1500, 2500)  # let suggestions appear
        try:
            first = S.to_playwright(page, S.F_LOCATION_FIRST_RESULT)
            first.wait_for(timeout=8000)
        except Exception as exc_s:
            self._note_field_error(
                page, "location-select",
                f"Location '{location}' suggestion not selected: "
                f"{type(exc_s).__name__}")
            return False, (f"suggestions: no suggestion list appeared "
                           f"({type(exc_s).__name__}) "
                           f"[field: {ident}; focus: {focus_desc}]")
        # Sanity: the suggestion should contain part of our text.
        sug_text = ""
        try:
            sug_text = (first.inner_text() or "").strip()
            want = location.split(",")[0].strip().lower()
            if want and want not in sug_text.lower() \
                    and sug_text.lower() not in want:
                self._note_field_error(
                    page, "location-mismatch",
                    f"Location suggestion mismatch: wanted '{location}', "
                    f"got '{sug_text[:60]}' — skipped to avoid wrong pick")
                return False, f"mismatch: first suggestion was '{sug_text[:60]}'"
        except Exception:
            pass
        try:
            human.human_click(page, first)
        except Exception as exc_c:
            self._note_field_error(
                page, "location-select",
                f"Location '{location}' suggestion click failed: "
                f"{type(exc_c).__name__}")
            return False, (f"click: suggestion click failed "
                           f"({type(exc_c).__name__})")
        human.human_pause(100, 250)
        # Verify: the field itself must now hold the selected location.
        actual = ""
        try:
            actual = (loc.input_value() or "").strip()
        except Exception:
            pass
        want_city = location.split(",")[0].strip().lower()
        if actual and want_city and want_city in actual.lower():
            return True, (f"selected '{sug_text or actual}' "
                          f"[field: {ident}; focus: {focus_desc}]")
        return False, (f"verify: suggestion clicked but field value is "
                       f"'{actual[:60]}' [field: {ident}]")

    def _open_create_item_form(self, page) -> tuple[bool, str]:
        """Open the create-item form the FBM Robo way: feed -> click the
        Marketplace icon in the header -> Marketplace page -> click
        'Create listing' -> choose 'Item for sale' -> wait for the form.

        Returns (ok, detail). Falls back to direct URL navigation when a
        click step fails. v0.2.44: direct-URL navigation left ID 1 staring
        at a page whose Title field never appeared (20s timeout); the
        click-through path is what the proven tool uses.
        """
        def step(name, fn):
            try:
                fn()
                return None
            except Exception as exc:
                return f"{name}: {type(exc).__name__}"

        # 1. Feed first — let it settle, like FBM Robo does.
        err = step("feed load", lambda: (
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded"),
            human.human_pause(2000, 3500),
            S.to_playwright(page, S.MKT_NAV_ICON).wait_for(timeout=10000),
        ))
        if not err:
            # 2. Click the Marketplace icon in the header nav.
            def _click_mkt_icon():
                human.human_click(page, S.to_playwright(page, S.MKT_NAV_ICON))
                human.human_pause(2000, 3500)
                # v0.2.48: try href selector, fall back to visible text.
                try:
                    S.to_playwright(page, S.CREATE_LISTING_BUTTON).wait_for(
                        timeout=8000)
                except Exception:
                    page.get_by_text("Create listing", exact=False).first.wait_for(
                        timeout=8000)
            err = step("Marketplace icon click", _click_mkt_icon)
        if not err:
            # 3. Click "Create listing".
            def _click_create():
                try:
                    loc = S.to_playwright(page, S.CREATE_LISTING_BUTTON)
                    loc.wait_for(timeout=5000)
                except Exception:
                    loc = page.get_by_text("Create listing",
                                           exact=False).first
                human.human_click(page, loc)
                human.human_pause(2000, 3000)
            err = step("Create listing click", _click_create)
        if not err:
            # 4. Choose the listing type if a chooser appears (best effort —
            #    Facebook sometimes goes straight to the item form).
            try:
                opt = S.to_playwright(page, S.ITEM_FOR_SALE_OPTION)
                opt.wait_for(timeout=8000)
                human.human_click(page, opt)
                human.human_pause(2000, 3000)
            except Exception:
                pass
            # 5. Form ready check (v0.2.59: FBM Robo style — NO GATE).
            # The old code REQUIRED the Title field; if missing, the whole
            # form fill was skipped (no pictures, nothing!). Now we just
            # verify the page URL; each field filler tries independently.
            def _wait_form_ready():
                try:
                    url = page.url or ""
                except Exception:
                    url = ""
                if "/marketplace/create" not in url:
                    raise TimeoutError(f"not on create page: {url[:60]}")
                # Best-effort: Title present is a bonus, not a requirement.
                try:
                    S.to_playwright(page, S.F_TITLE).wait_for(timeout=8000)
                except Exception:
                    pass
            err = step("form ready check", _wait_form_ready)
        if not err:
            return True, "click-through flow OK"
        detail = f"click-through failed at [{err}]"
        # Fallback: direct URL (previous behavior).
        try:
            page.goto(S.MKT_CREATE_ITEM_URL, wait_until="domcontentloaded")
            human.human_pause(1500, 3000)
            try:
                url2 = page.url or ""
            except Exception:
                url2 = ""
            if "/marketplace/create" not in url2:
                raise TimeoutError(f"direct URL did not land: {url2[:60]}")
            return True, detail + " | direct URL worked"
        except Exception as exc2:
            return False, (detail +
                           f" | direct URL also failed: {type(exc2).__name__}: {str(exc2)[:300]}")

    def _fill_form(self, page, payload: dict) -> bool:
        """Fill the whole create-item form. Shared by post/draft/manual/edit.

        v0.2.50 order (user's explicit sequence): More details first, then
        photos, then title, price, and the rest of the details. Faster —
        short pauses instead of long ones.
        """
        # v0.2.64: Field completion checklist — track every field:
        # attempted, succeeded, verified, error. Don't click Next until
        # required fields pass (ChatGPT's debugging plan).
        checklist = {}
        checklist_lines = []
        def _pause_checkpoint():
            # Check only at safe boundaries between browser UI actions.
            callback = payload.get("_pause_checkpoint")
            if callable(callback):
                callback()
        def _check(field, status, detail=""):
            checklist[field] = {"status": status, "detail": detail}
            line = f"[CHECKLIST] {field}: {status}" + (f" — {detail}" if detail else "")
            checklist_lines.append(line)
            # Log to console for visibility
            print(line, flush=True)
            # v0.2.66: also store for the final summary so it's visible
            # in FMS logs, not just console.


        try:
            ok, detail = self._open_create_item_form(page)
            if not ok:
                # v0.2.56: the form never opened — still snap so the user
                # sees what the page showed instead.
                self._note_field_error(page, "form-open", detail or
                                       "create-item form did not open")
                return False

            # v0.2.58: user's EXACT form order (no more guessing):
            # Main form: Photos -> Title -> Price -> Category -> Condition.
            # THEN click "More details" -> Description -> Availability ->
            # (skip Tags, skip SKU) -> Location -> Delivery -> Hide from friends.
            # THEN Next becomes clickable -> Publish.

            # 1. Photos FIRST (before any text).
            _pause_checkpoint()
            # v0.2.79: Use file_chooser (Playwright recommended).
            images = payload.get("image_paths") or []
            if not images:
                self._note_field_error(page, "pictures",
                                       "no image_paths in payload — 0 images to upload")
            else:
                _attempted = False
                _last_err = ""
                # Try 1: file chooser via "Add photos" click.
                try:
                    with page.expect_file_chooser(timeout=8000) as _fc:
                        _add = page.get_by_text("Add photos", exact=False).first
                        _add.wait_for(timeout=5000)
                        human.human_click(page, _add)
                    _chooser = _fc.value
                    _chooser.set_files([str(p) for p in images[:10]])
                    _attempted = True
                except Exception as _e1:
                    _last_err = f"chooser: {type(_e1).__name__}: {str(_e1)[:100]}"
                # Try 2: direct set_input_files (fallback).
                if not _attempted:
                    for _sel in ("input[type='file']",
                                 "input[type='file'][accept*='image']"):
                        try:
                            page.set_input_files(_sel, [str(p) for p in images[:10]])
                            _attempted = True
                            break
                        except Exception as _e2:
                            _last_err = f"direct: {type(_e2).__name__}: {str(_e2)[:100]}"
                            continue
                if not _attempted:
                    self._note_field_error(
                        page, "pictures",
                        f"upload failed: {_last_err[:200]}")
                else:
                    # VERIFY: did photos actually appear?
                    human.human_pause(3000, 4000)
                    _photos_ok = False
                    _verify_detail = ""
                    try:
                        _counter = page.get_by_text("Photos", exact=False).first
                        _ctext = _counter.inner_text(timeout=3000) if _counter else ""
                        _verify_detail = f"counter='{_ctext}'"
                        import re as _re
                        _m = _re.search(r"(\d+)\s*/\s*10", _ctext)
                        if _m and int(_m.group(1)) > 0:
                            _photos_ok = True
                    except Exception as _e3:
                        _verify_detail += f" counter-err={type(_e3).__name__}"
                    if not _photos_ok:
                        self._note_field_error(
                            page, "pictures",
                            f"upload attempted but NO photos visible! {_verify_detail[:150]}")
            # Optional video
            if payload.get("video_path"):
                try:
                    page.set_input_files(S.F_ADD_VIDEO[1], str(payload["video_path"]))
                    human.human_pause(2000, 3500)
                except Exception:
                    pass

            # 3. Title (v0.2.59: INDEPENDENT — a Title failure must NOT
            # block pictures/price/category/etc. FBM Robo style: try it,
            # log the result, move on to the next field).
            _pause_checkpoint()
            _check("title", "attempted")
            try:
                title = S.to_playwright(page, S.F_TITLE)
                title.wait_for(timeout=8000)
                self._type(title, payload.get("title", ""), hybrid_ok=True)
                human.human_pause(100, 300)
                _check("title", "succeeded")
            except Exception as exc_t:
                self._note_field_error(
                    page, "title",
                    f"Title not filled: {type(exc_t).__name__}")
                _check("title", "failed", type(exc_t).__name__)
            # 4. Price (v0.2.59: independent, with verification).

            # v0.2.60: Price is FULLY INDEPENDENT — never references 'title'.
            # (The old code crashed with NameError if Title had failed.)
            _pause_checkpoint()
            price_val = str(payload.get("price", ""))
            _check("price", "attempted", f"value='{price_val}'")
            if price_val:
                try:
                    price = S.to_playwright(page, S.F_PRICE)
                    price.wait_for(timeout=8000)
                    price.click()
                    human.human_pause(100, 250)
                    price.fill(price_val)
                    human.human_pause(100, 250)
                    # Verify the price field actually holds the price.
                    try:
                        got = price.input_value() or ""
                        if price_val not in got:
                            self._last_form_error = (
                                f"Price verification failed: wanted '{price_val}', "
                                f"field has '{got[:40]}'")
                    except Exception:
                        pass
                except Exception as exc_p:
                    self._last_form_error = (
                        f"Price '{price_val}' not filled: {type(exc_p).__name__}: "
                        f"{str(exc_p)[:100]}")

            # Category: CLICK the field, then SELECT from the dropdown list.
            # v0.2.54: user confirmed these are lists, not type-in fields.
            # v0.2.65: checklist tracking (dynamic value from payload).
            _check("category", "attempted")
            # The old code kept trying to TYPE, which never selected anything.
            category = payload.get("category")
            if category:
                try:
                    cat = S.to_playwright(page, S.F_CATEGORY)
                    cat.wait_for(timeout=8000)
                    human.human_click(page, cat)
                    human.human_pause(300, 600)
                    # The dropdown list is open — click the matching option.
                    # Scope to listbox/option roles so we don't hit stray text.
                    opt = page.locator(
                        "[role='listbox'] [role='option'], "
                        "[role='menu'] [role='menuitem']").filter(
                        has_text=category).first
                    try:
                        opt.wait_for(timeout=6000)
                    except Exception:
                        # Fallback: any visible element with the text.
                        opt = page.get_by_text(category, exact=False).first
                        opt.wait_for(timeout=6000)
                    human.human_click(page, opt)
                    human.human_pause(100, 250)
                except Exception as exc_c:
                    self._note_field_error(
                        page, "category",
                        f"Category '{category}' not selected: "
                        f"{type(exc_c).__name__}")
                    # Don't return False yet — try the rest, Next check
                    # will catch an incomplete form.

            # Condition: CLICK the field, then SELECT from the list.
            # v0.2.54: same list-select approach as category.
            # PERMANENT RULE (2026-10-19): Automation follows DB saved value.
            # No thinking, no intelligence, no guessing. DB says X → use X.
            condition = (payload.get("condition") or "").strip() or "New"
            _check("condition", "attempted", f"value='{condition}'")
            if condition:
                try:
                    # v0.2.91: EXACTLY like Category (user request).
                    # Simple: find field, click, find option, click.
                    cond_field = S.to_playwright(page, S.F_CONDITION_FIELD)
                    cond_field.wait_for(timeout=8000)
                    human.human_click(page, cond_field)
                    human.human_pause(300, 600)
                    # Click the matching option in the list.
                    # v0.2.93: EXACT match only (per instructions).
                    # "New" must not match "Used - like new".
                    opt = page.get_by_role(
                        "option", name=condition, exact=True).first
                    try:
                        opt.wait_for(timeout=6000)
                    except Exception:
                        # Fallback: exact text match within listbox
                        opt = page.locator("[role='listbox']").get_by_text(
                            condition, exact=True).first
                        opt.wait_for(timeout=6000)
                    human.human_click(page, opt)
                    human.human_pause(100, 250)
                    _check("condition", "succeeded")
                except Exception as exc_co:
                    self._note_field_error(
                        page, "condition",
                        f"Condition '{condition}' not selected: "
                        f"{type(exc_co).__name__}")
                    _check("condition", "failed", type(exc_co).__name__)

            _pause_checkpoint()
            # 5b. Click "More details" to reveal Description, Availability,
            # Location, Delivery, Hide from friends (user's exact order).
            for _md_text in ("More details", "Show more details",
                             "More Details"):
                try:
                    md = page.get_by_text(_md_text, exact=False).first
                    md.wait_for(timeout=4000)
                    human.human_click(page, md)
                    human.human_pause(200, 400)
                    break
                except Exception:
                    continue

            # 6. Description: COPY-PASTE, not typing (v0.2.55 — user's
            # explicit instruction: simple and easy, no character-by-character).
            _pause_checkpoint()
            desc = payload.get("description") or ""
            if desc:
                try:
                    dloc = S.to_playwright(page, S.F_DESCRIPTION)
                    dloc.wait_for(timeout=8000)
                    # v0.2.95: ensure visible before fill (user: fix Description only)
                    try:
                        dloc.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    dloc.click()
                    human.human_pause(200, 400)
                    # fill() sets the value instantly (paste-like).
                    dloc.fill(desc)
                    human.human_pause(100, 250)
                except Exception as exc_d:
                    self._note_field_error(
                        page, "description",
                        f"Description not pasted: {type(exc_d).__name__}")

            # 7. Availability: dropdown -> select "List as in stock"
            # (user: product saves availability as "list as stock").
            # Tags and SKU are SKIPPED (user's explicit instruction).
            _pause_checkpoint()
            _avail = (payload.get("availability") or "").strip().lower()
            # Map product values to the FB option text.
            _avail_map = {
                "in_stock": "List as in stock",
                "list as in stock": "List as in stock",
                "stock": "List as in stock",
                "single": "List as single item",
                "list as single item": "List as single item",
            }
            _avail_text = _avail_map.get(_avail, "List as in stock")
            try:
                av = S.to_playwright(page, S.F_AVAILABILITY_FIELD)
                av.wait_for(timeout=8000)
                human.human_click(page, av)
                human.human_pause(300, 600)
                opt = page.locator(
                    "[role='listbox'] [role='option'], "
                    "[role='menu'] [role='menuitem']").filter(
                    has_text=_avail_text).first
                try:
                    opt.wait_for(timeout=6000)
                except Exception:
                    opt = page.get_by_text(_avail_text, exact=False).first
                    opt.wait_for(timeout=6000)
                human.human_click(page, opt)
                human.human_pause(100, 250)
            except Exception as exc_av:
                self._note_field_error(
                    page, "availability",
                    f"Availability '{_avail_text}' not selected: "
                    f"{type(exc_av).__name__}")

            # 8. Location (Tags and SKU skipped per user's instruction).
            # v0.2.78: scroll into view (form has its own scrollbar).
            if payload.get("location"):
                try:
                    _loc_field = page.get_by_text("Location", exact=False).first
                    _loc_field.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                self._select_location(page, payload["location"])

            # 9. Hide From Friends: it's a TOGGLE switch (user's screenshot).
            # Only click if it's currently OFF (we want it ON).
            if payload.get("hide_from_friends"):
                try:
                    hff = S.to_playwright(page, S.F_HIDE_FROM_FRIENDS)
                    # v0.2.78: scroll into view first.
                    try:
                        hff.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    hff.wait_for(timeout=6000)
                    # Check the toggle state — don't flip it if already on.
                    try:
                        _checked = hff.evaluate(
                            "e => e.getAttribute('aria-checked')")
                    except Exception:
                        _checked = None
                    if _checked not in ("true", True):
                        human.human_click(page, hff)
                        human.human_pause(100, 250)
                except Exception as exc_h:
                    self._note_field_error(
                        page, "hide-from-friends",
                        f"Hide-from-friends not set: {type(exc_h).__name__}")
            # 9. Delivery options: user ticks "Door drop-off".
            # v0.2.58: state-aware — only click if not already selected
            # (clicking a selected checkbox would UNSELECT it).
            _delivery_sel = {
                "public_meetup": S.F_DELIVERY_MEETUP,
                "door_pickup": S.F_DELIVERY_PICKUP,
                "door_dropoff": S.F_DELIVERY_DROPOFF,
            }
            for _opt in payload.get("delivery_options") or []:
                _sel = _delivery_sel.get(_opt)
                if not _sel:
                    continue
                try:
                    dloc = S.to_playwright(page, _sel)
                    # v0.2.78: scroll into view first.
                    try:
                        dloc.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    dloc.wait_for(timeout=6000)
                    # Check if already selected (checkbox/radio).
                    try:
                        _sel_state = dloc.evaluate(
                            "e => e.checked !== undefined ? e.checked : "
                            "e.getAttribute('aria-checked')")
                    except Exception:
                        _sel_state = None
                    if _sel_state not in (True, "true"):
                        human.human_click(page, dloc)
                        human.human_pause(100, 250)
                except Exception as exc_do:
                    self._note_field_error(
                        page, f"delivery-{_opt}",
                        f"Delivery '{_opt}' not set: {type(exc_do).__name__}")
            # (Photos/video already attached at the top — v0.2.50 order.)
            return True
        except Exception as exc:
            # v0.2.29: keep the real failure reason + page so the run logs
            # say WHY the form never completed (instead of a silent False).
            try:
                url = page.url
            except Exception:
                url = "?"
            self._note_field_error(
                page, "form-fatal",
                f"{type(exc).__name__}: {str(exc)[:180]} @ {url}")
            return False

    def _expand_more_details_draft_publish(self, page) -> bool:
        """Draft & Publish only (v0.2.124): open the form's "More details"
        section WITHOUT ever collapsing it, then verify it is open.

        The shared _fill_form blind-clicks the "More details" toggle. When
        Facebook has the section already expanded, that click collapses it
        and Description / Availability / Location / Delivery — all inside
        the section — become unfillable while the fields outside it still
        succeed. This helper mirrors the Post action's proven state-aware
        logic (single_post_test._more_details_state): read the toggle's
        aria-expanded FIRST; click only when collapsed; never blind-click
        an unreadable toggle; verify success by the Description control's
        actual presence. Returns True when the section is open.
        """
        state, btn = "unknown", None
        for _label in ("More details", "Show more details", "More Details"):
            try:
                _raw = page.get_by_text(_label, exact=False)
                if _raw.count() == 0:
                    continue
                _text_loc = _raw.first
                _btn_loc = _text_loc.locator(
                    "xpath=ancestor::*[@role='button'][@aria-expanded][1]")
                if _btn_loc.count() > 0:
                    _val = _btn_loc.first.evaluate(
                        "e => e.getAttribute('aria-expanded')")
                    state = ("true" if _val == "true"
                             else "false" if _val == "false" else "unknown")
                    btn = _btn_loc.first
                else:
                    _val = _text_loc.evaluate(
                        "e => e.getAttribute('aria-expanded')")
                    state = ("true" if _val == "true"
                             else "false" if _val == "false" else "unknown")
                    btn = _text_loc
                break
            except Exception:
                continue
        if state == "false" and btn is not None:
            # Collapsed -> exactly one click. (Already-open is left alone:
            # clicking it would collapse the section.)
            try:
                human.human_click(page, btn)
                human.human_pause(200, 400)
            except Exception:
                pass
        try:
            S.to_playwright(page, S.F_DESCRIPTION).wait_for(timeout=8000)
            return True
        except Exception:
            self._note_field_error(
                page, "more-details",
                "More details section not open (toggle state: "
                f"{state}) — Description/Availability/Location/Delivery "
                "may be unfillable")
            return False

    def _fill_form_draft_publish(self, page, payload: dict) -> bool:
        """Fill the whole create-item form. Shared by post/draft/manual/edit.

        v0.2.50 order (user's explicit sequence): More details first, then
        photos, then title, price, and the rest of the details. Faster —
        short pauses instead of long ones.
        """
        # v0.2.64: Field completion checklist — track every field:
        # attempted, succeeded, verified, error. Don't click Next until
        # required fields pass (ChatGPT's debugging plan).
        checklist = {}
        checklist_lines = []
        def _pause_checkpoint():
            # Check only at safe boundaries between browser UI actions.
            callback = payload.get("_pause_checkpoint")
            if callable(callback):
                callback()
        def _check(field, status, detail=""):
            checklist[field] = {"status": status, "detail": detail}
            line = f"[CHECKLIST] {field}: {status}" + (f" — {detail}" if detail else "")
            checklist_lines.append(line)
            # Log to console for visibility
            print(line, flush=True)
            # v0.2.66: also store for the final summary so it's visible
            # in FMS logs, not just console.


        try:
            ok, detail = self._open_create_item_form(page)
            if not ok:
                # v0.2.56: the form never opened — still snap so the user
                # sees what the page showed instead.
                self._note_field_error(page, "form-open", detail or
                                       "create-item form did not open")
                return False

            # v0.2.58: user's EXACT form order (no more guessing):
            # Main form: Photos -> Title -> Price -> Category -> Condition.
            # THEN click "More details" -> Description -> Availability ->
            # (skip Tags, skip SKU) -> Location -> Delivery -> Hide from friends.
            # THEN Next becomes clickable -> Publish.

            # 1. Photos FIRST (before any text).
            _pause_checkpoint()
            # v0.2.79: Use file_chooser (Playwright recommended).
            images = payload.get("image_paths") or []
            if not images:
                self._note_field_error(page, "pictures",
                                       "no image_paths in payload — 0 images to upload")
            else:
                _attempted = False
                _last_err = ""
                # Try 1: file chooser via "Add photos" click.
                try:
                    with page.expect_file_chooser(timeout=8000) as _fc:
                        _add = page.get_by_text("Add photos", exact=False).first
                        _add.wait_for(timeout=5000)
                        human.human_click(page, _add)
                    _chooser = _fc.value
                    _chooser.set_files([str(p) for p in images[:10]])
                    _attempted = True
                except Exception as _e1:
                    _last_err = f"chooser: {type(_e1).__name__}: {str(_e1)[:100]}"
                # Try 2: direct set_input_files (fallback).
                if not _attempted:
                    for _sel in ("input[type='file']",
                                 "input[type='file'][accept*='image']"):
                        try:
                            page.set_input_files(_sel, [str(p) for p in images[:10]])
                            _attempted = True
                            break
                        except Exception as _e2:
                            _last_err = f"direct: {type(_e2).__name__}: {str(_e2)[:100]}"
                            continue
                if not _attempted:
                    self._note_field_error(
                        page, "pictures",
                        f"upload failed: {_last_err[:200]}")
                else:
                    # VERIFY: did photos actually appear?
                    human.human_pause(3000, 4000)
                    _photos_ok = False
                    _verify_detail = ""
                    try:
                        _counter = page.get_by_text("Photos", exact=False).first
                        _ctext = _counter.inner_text(timeout=3000) if _counter else ""
                        _verify_detail = f"counter='{_ctext}'"
                        import re as _re
                        _m = _re.search(r"(\d+)\s*/\s*10", _ctext)
                        if _m and int(_m.group(1)) > 0:
                            _photos_ok = True
                    except Exception as _e3:
                        _verify_detail += f" counter-err={type(_e3).__name__}"
                    if not _photos_ok:
                        self._note_field_error(
                            page, "pictures",
                            f"upload attempted but NO photos visible! {_verify_detail[:150]}")
            # Optional video
            if payload.get("video_path"):
                try:
                    page.set_input_files(S.F_ADD_VIDEO[1], str(payload["video_path"]))
                    human.human_pause(2000, 3500)
                except Exception:
                    pass

            # 3. Title (v0.2.59: INDEPENDENT — a Title failure must NOT
            # block pictures/price/category/etc. FBM Robo style: try it,
            # log the result, move on to the next field).
            _pause_checkpoint()
            _check("title", "attempted")
            try:
                title = S.to_playwright(page, S.F_TITLE)
                title.wait_for(timeout=8000)
                self._type(title, payload.get("title", ""), hybrid_ok=True)
                human.human_pause(100, 300)
                _check("title", "succeeded")
            except Exception as exc_t:
                self._note_field_error(
                    page, "title",
                    f"Title not filled: {type(exc_t).__name__}")
                _check("title", "failed", type(exc_t).__name__)
            # 4. Price (v0.2.59: independent, with verification).

            # v0.2.60: Price is FULLY INDEPENDENT — never references 'title'.
            # (The old code crashed with NameError if Title had failed.)
            _pause_checkpoint()
            price_val = str(payload.get("price", ""))
            _check("price", "attempted", f"value='{price_val}'")
            if price_val:
                try:
                    price = S.to_playwright(page, S.F_PRICE)
                    price.wait_for(timeout=8000)
                    price.click()
                    human.human_pause(100, 250)
                    price.fill(price_val)
                    human.human_pause(100, 250)
                    # Verify the price field actually holds the price.
                    try:
                        got = price.input_value() or ""
                        if price_val not in got:
                            self._last_form_error = (
                                f"Price verification failed: wanted '{price_val}', "
                                f"field has '{got[:40]}'")
                    except Exception:
                        pass
                except Exception as exc_p:
                    self._last_form_error = (
                        f"Price '{price_val}' not filled: {type(exc_p).__name__}: "
                        f"{str(exc_p)[:100]}")

            # Category: CLICK the field, then SELECT from the dropdown list.
            # v0.2.54: user confirmed these are lists, not type-in fields.
            # v0.2.65: checklist tracking (dynamic value from payload).
            _check("category", "attempted")
            # The old code kept trying to TYPE, which never selected anything.
            category = payload.get("category")
            if category:
                try:
                    cat = S.to_playwright(page, S.F_CATEGORY)
                    cat.wait_for(timeout=8000)
                    # Wait for the dropdown to actually render before selecting.
                    human.human_click(page, cat)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    human.human_pause(900, 1400)
                    opt = page.locator(
                        "[role='listbox'] [role='option'], "
                        "[role='menu'] [role='menuitem']").filter(
                        has_text=category).first
                    try:
                        opt.wait_for(state="visible", timeout=12000)
                    except Exception:
                        opt = page.get_by_text(category, exact=False).first
                        opt.wait_for(state="visible", timeout=12000)
                    opt.scroll_into_view_if_needed(timeout=5000)
                    opt.click(timeout=10000)
                    # Do not continue until the selected value is reflected in the field.
                    human.human_pause(700, 1100)
                    selected = False
                    try:
                        field_text = (cat.inner_text(timeout=2500) or "").strip()
                        field_value = (cat.get_attribute("aria-label") or "").strip()
                        selected = (category.casefold() in field_text.casefold()
                                    or category.casefold() in field_value.casefold())
                    except Exception:
                        pass
                    if not selected:
                        # A visible selected option/value is also acceptable evidence.
                        try:
                            selected = page.locator(
                                "[role='option'][aria-selected='true'], "
                                "[role='menuitem'][aria-checked='true']").filter(
                                has_text=category).count() > 0
                        except Exception:
                            selected = False
                    if not selected:
                        raise RuntimeError("category click was not verifiably selected")
                except Exception as exc_c:
                    self._note_field_error(
                        page, "category",
                        f"Category '{category}' not selected: "
                        f"{type(exc_c).__name__}")
                    # Don't return False yet — try the rest, Next check
                    # will catch an incomplete form.

            # Condition: CLICK the field, then SELECT from the list.
            # v0.2.54: same list-select approach as category.
            # PERMANENT RULE (2026-10-19): Automation follows DB saved value.
            # No thinking, no intelligence, no guessing. DB says X → use X.
            condition = (payload.get("condition") or "").strip() or "New"
            _check("condition", "attempted", f"value='{condition}'")
            if condition:
                try:
                    # v0.2.91: EXACTLY like Category (user request).
                    # Simple: find field, click, find option, click.
                    cond_field = S.to_playwright(page, S.F_CONDITION_FIELD)
                    cond_field.wait_for(timeout=8000)
                    human.human_click(page, cond_field)
                    human.human_pause(300, 600)
                    # Click the matching option in the list.
                    # v0.2.93: EXACT match only (per instructions).
                    # "New" must not match "Used - like new".
                    opt = page.get_by_role(
                        "option", name=condition, exact=True).first
                    try:
                        opt.wait_for(timeout=6000)
                    except Exception:
                        # Fallback: exact text match within listbox
                        opt = page.locator("[role='listbox']").get_by_text(
                            condition, exact=True).first
                        opt.wait_for(timeout=6000)
                    human.human_click(page, opt)
                    human.human_pause(100, 250)
                    _check("condition", "succeeded")
                except Exception as exc_co:
                    self._note_field_error(
                        page, "condition",
                        f"Condition '{condition}' not selected: "
                        f"{type(exc_co).__name__}")
                    _check("condition", "failed", type(exc_co).__name__)

            _pause_checkpoint()
            # 5b. Draft & Publish ONLY (v0.2.124): expand "More details"
            # STATE-AWARELY via the dedicated helper. The shared _fill_form
            # blind-clicks the toggle, which COLLAPSES the section when
            # Facebook already has it open — leaving Description,
            # Availability, Location and Delivery unfillable. The helper
            # reads aria-expanded first, clicks only when collapsed, and
            # verifies via the Description control (the same proven logic
            # the Post action uses in single_post_test).
            self._expand_more_details_draft_publish(page)

            # 6. Description: COPY-PASTE, not typing (v0.2.55 — user's
            # explicit instruction: simple and easy, no character-by-character).
            _pause_checkpoint()
            desc = payload.get("description") or ""
            if desc:
                try:
                    dloc = S.to_playwright(page, S.F_DESCRIPTION)
                    dloc.wait_for(timeout=8000)
                    # v0.2.95: ensure visible before fill (user: fix Description only)
                    try:
                        dloc.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    dloc.click()
                    human.human_pause(200, 400)
                    # fill() sets the value instantly (paste-like).
                    dloc.fill(desc)
                    human.human_pause(100, 250)
                except Exception as exc_d:
                    self._note_field_error(
                        page, "description",
                        f"Description not pasted: {type(exc_d).__name__}")

            # 7. Availability: dropdown -> select "List as in stock"
            # (user: product saves availability as "list as stock").
            # Tags and SKU are SKIPPED (user's explicit instruction).
            _pause_checkpoint()
            _avail = (payload.get("availability") or "").strip().lower()
            # Map product values to the FB option text.
            _avail_map = {
                "in_stock": "List as in stock",
                "list as in stock": "List as in stock",
                "stock": "List as in stock",
                "single": "List as single item",
                "list as single item": "List as single item",
            }
            _avail_text = _avail_map.get(_avail, "List as in stock")
            try:
                av = S.to_playwright(page, S.F_AVAILABILITY_FIELD)
                av.wait_for(timeout=8000)
                human.human_click(page, av)
                human.human_pause(300, 600)
                opt = page.locator(
                    "[role='listbox'] [role='option'], "
                    "[role='menu'] [role='menuitem']").filter(
                    has_text=_avail_text).first
                try:
                    opt.wait_for(timeout=6000)
                except Exception:
                    opt = page.get_by_text(_avail_text, exact=False).first
                    opt.wait_for(timeout=6000)
                human.human_click(page, opt)
                human.human_pause(100, 250)
            except Exception as exc_av:
                self._note_field_error(
                    page, "availability",
                    f"Availability '{_avail_text}' not selected: "
                    f"{type(exc_av).__name__}")

            # 8. Location (Tags and SKU skipped per user's instruction).
            # v0.2.78: scroll into view (form has its own scrollbar).
            if payload.get("location"):
                try:
                    _loc_field = page.get_by_text("Location", exact=False).first
                    _loc_field.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                self._select_location(page, payload["location"])

            # 9. Hide From Friends: it's a TOGGLE switch (user's screenshot).
            # Only click if it's currently OFF (we want it ON).
            if payload.get("hide_from_friends"):
                try:
                    hff = S.to_playwright(page, S.F_HIDE_FROM_FRIENDS)
                    # v0.2.78: scroll into view first.
                    try:
                        hff.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    hff.wait_for(timeout=6000)
                    # Check the toggle state — don't flip it if already on.
                    try:
                        _checked = hff.evaluate(
                            "e => e.getAttribute('aria-checked')")
                    except Exception:
                        _checked = None
                    if _checked not in ("true", True):
                        human.human_click(page, hff)
                        human.human_pause(100, 250)
                except Exception as exc_h:
                    self._note_field_error(
                        page, "hide-from-friends",
                        f"Hide-from-friends not set: {type(exc_h).__name__}")
            # 9. Delivery options: user ticks "Door drop-off".
            # v0.2.58: state-aware — only click if not already selected
            # (clicking a selected checkbox would UNSELECT it).
            _delivery_sel = {
                "public_meetup": S.F_DELIVERY_MEETUP,
                "door_pickup": S.F_DELIVERY_PICKUP,
                "door_dropoff": S.F_DELIVERY_DROPOFF,
            }
            for _opt in payload.get("delivery_options") or []:
                _sel = _delivery_sel.get(_opt)
                if not _sel:
                    continue
                try:
                    dloc = S.to_playwright(page, _sel)
                    # v0.2.78: scroll into view first.
                    try:
                        dloc.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    dloc.wait_for(timeout=6000)
                    # Check if already selected (checkbox/radio).
                    try:
                        _sel_state = dloc.evaluate(
                            "e => e.checked !== undefined ? e.checked : "
                            "e.getAttribute('aria-checked')")
                    except Exception:
                        _sel_state = None
                    if _sel_state not in (True, "true"):
                        human.human_click(page, dloc)
                        human.human_pause(100, 250)
                except Exception as exc_do:
                    self._note_field_error(
                        page, f"delivery-{_opt}",
                        f"Delivery '{_opt}' not set: {type(exc_do).__name__}")
            # (Photos/video already attached at the top — v0.2.50 order.)
            return True
        except Exception as exc:
            # v0.2.29: keep the real failure reason + page so the run logs
            # say WHY the form never completed (instead of a silent False).
            try:
                url = page.url
            except Exception:
                url = "?"
            self._note_field_error(
                page, "form-fatal",
                f"{type(exc).__name__}: {str(exc)[:180]} @ {url}")
            return False

    def human_warmup(self) -> None:
        """Brief human-like feed browsing before the first post (v0.2.33).

        A human doesn't log in and instantly open the create-listing page;
        they scroll the feed a little first. This closes that behavioral gap
        behind Facebook's "suspected automated behavior" flag.
        """
        self._ensure()
        page = self._page
        assert page is not None
        for _ in range(random.randint(3, 5)):
            human.human_scroll(page, random.randint(300, 800))
            # human_scroll already pauses 300-900ms after each scroll

    def post_listing(self, payload: dict) -> str | None:
        # Per-listing timer starts at the beginning of this workflow, BEFORE
        # _ensure() can open/attach the browser. It resets on every listing.
        # Min/Max are configurable timing targets, not deadlines or stop gates.
        _t0 = getattr(self, "_pending_browser_start_time", None) or time.monotonic()
        # Browser-open time belongs to the first listing only. Every subsequent
        # listing starts a fresh timer at its own workflow entry.
        self._pending_browser_start_time = None
        _min_s, _max_s, _target_s = _listing_timing_target(payload)
        # Select a fresh target for each listing within the user's configured
        # range. If form/browser/network work already exceeds this target, do
        # not wait further and do not stop: continue to the normal Next flow.

        self._ensure()
        # If this call itself had to open the browser, _launch() stamped the
        # real opening time. Use it and consume it so the next listing gets a
        # fresh timer rather than inheriting this browser-start timestamp.
        _launched_at = getattr(self, "_pending_browser_start_time", None)
        if _launched_at is not None:
            _t0 = _launched_at
            self._pending_browser_start_time = None
        page = self._page
        assert page is not None
        self._last_error = None  # v0.2.29: surfaced in the run logs on failure
        try:
            if not self._fill_form(page, payload):
                self._last_error = (getattr(self, "_last_form_error", None)
                                    or "form fill failed")
                return None
            # Soft timing target only: wait if the workflow was faster than the
            # selected target. Max is NEVER a timeout; slow loading can exceed it.
            _elapsed = time.monotonic() - _t0
            if _elapsed < _target_s:
                _wait = _target_s - _elapsed
                human.human_pause(int(_wait * 1000), int(_wait * 1000) + 250)
            # Publish: form -> Next -> Publish (two-step, v0.2.51).
            # The Publish button only appears AFTER clicking Next; the old
            # code skipped Next entirely and timed out waiting for Publish.
            # v0.2.53: wait for Next to be ENABLED (not just visible). If the
            # form is incomplete (category/condition missing), Next stays
            # disabled — clicking a disabled button does nothing.
            try:
                nxt = S.to_playwright(page, S.F_NEXT_BUTTON)
                # No Max-time stop: wait for Facebook's Next control to be
                # visible and enabled, however long loading takes.
                nxt.wait_for(timeout=0)
                # Wait until it's actually clickable (enabled), with no timer
                # deadline. Min/Max only shape the soft timing target above.
                try:
                    nxt.wait_for(state="enabled", timeout=0)
                except Exception:
                    # Still disabled — find which required fields are missing.
                    missing = []
                    try:
                        errs = page.evaluate(
                            """() => Array.from(document.querySelectorAll(
                                '[role=\"alert\"], .x1lliihq')).slice(0, 10)
                                .map(e => e.innerText || '').filter(t => t)
                                .join(' | ')""")
                        if errs:
                            missing.append(errs[:200])
                    except Exception:
                        pass
                    # v0.2.56: dump the form's field structure so we stop
                    # guessing selectors. Lists every labeled control.
                    try:
                        formdump = page.evaluate(
                            """() => Array.from(document.querySelectorAll(
                                'input, textarea, [role=\"combobox\"], '
                                + '[role=\"listbox\"], button')).slice(0, 40)
                                .map(e => [
                                e.tagName,
                                (e.getAttribute('aria-label') || '').slice(0,30),
                                (e.getAttribute('placeholder') || '').slice(0,20),
                                (e.innerText || '').slice(0,30).replace(/\\n/g,' ')
                                ].join('|')).join(' ;; ')""")
                    except Exception:
                        formdump = "?"
                    # v0.2.66: include ALL field errors so the user sees
                    # WHICH fields failed and WHY (not just empty "Errors:").
                    _all_errs = getattr(self, "_all_field_errors", []) or []
                    _field_errs = " ;; ".join(_all_errs) if _all_errs else (getattr(self, "_last_form_error", "") or "")
                    raise RuntimeError(
                        "Next is disabled (form incomplete). " +
                        ("Errors: " + missing[0] if missing else
                         "Check category/condition/location.") +
                        (f" | Field errors: {_field_errs[:500]}" if _field_errs else "") +
                        f" | Form fields: {formdump[:800]}")
                human.human_click(page, nxt)
                human.human_pause(2000, 3500)
            except Exception as exc_n:
                # v0.2.56: self-diagnosis — save a screenshot so the user
                # gets a picture of the stuck form without taking one.
                shot = self._snap(page, "next-disabled")
                self._last_error = (f"Next button: "
                                    f"{str(exc_n)[:200]} @ {page.url[:80]}"
                                    + (f" | Screenshot: {shot}" if shot
                                       else ""))
                return None
            try:
                pub = S.to_playwright(page, S.F_PUBLISH_BUTTON)
                pub.wait_for(timeout=15000)
                human.human_click(page, pub)
            except Exception as exc_p:
                shot = self._snap(page, "publish")
                self._last_error = (f"Publish button not found/clickable: "
                                    f"{type(exc_p).__name__} @ {page.url[:80]}"
                                    + (f" | Screenshot: {shot}" if shot
                                       else ""))
                return None
            human.human_pause(3000, 6000)

            # Try to capture the new listing id from the URL.
            url = page.url
            listing_id = None
            if "/marketplace/item/" in url:
                listing_id = url.rstrip("/").split("/")[-1].split("?")[0]
            return listing_id or "posted"
        except Exception as exc:
            try:
                url = page.url
            except Exception:
                url = "?"
            self._last_error = f"{type(exc).__name__}: {str(exc)[:180]} @ {url}"
            return None

    # -- Phase 2: drafts --------------------------------------------------------
    def _create_draft_draft_publish(self, payload: dict) -> str | None:
        """State-aware draft workflow used by Draft & Publish and Create Drafts.

        Facebook does not consistently expose listing_id immediately after Save Draft.
        Draft & Publish queues the saved draft by title for its later publish phase;
        Create Drafts uses the same form/save flow but stops after draft verification.
        Its optional image callback is registered only by Create Drafts.
        """
        self._ensure()
        page = self._page
        assert page is not None
        import re
        try:
            if not self._fill_form_draft_publish(page, payload):
                self._last_error = (getattr(self, "_last_form_error", None)
                                    or "Draft & Publish form fill failed")
                return None

            candidates = []
            try:
                candidates.append(page.get_by_role("button", name=re.compile(r"^\s*save\s+draft\s*$", re.I)))
            except Exception:
                pass
            try:
                candidates.append(S.to_playwright(page, S.F_SAVE_DRAFT_BUTTON))
            except Exception:
                pass
            try:
                candidates.append(page.get_by_text(re.compile(r"^\s*save\s+draft\s*$", re.I), exact=True))
            except Exception:
                pass

            save_button = None
            for candidate in candidates:
                try:
                    for i in range(min(candidate.count(), 10)):
                        item = candidate.nth(i) if candidate.count() > 1 else candidate
                        if item.is_visible() and item.is_enabled():
                            save_button = item
                            break
                    if save_button is not None:
                        break
                except Exception:
                    continue
            if save_button is None:
                shot = self._snap(page, "draft_save_button_missing")
                self._last_error = "Draft & Publish: visible/enabled Save Draft button not found" + (f"; screenshot={shot}" if shot else "")
                return None

            try:
                save_button.scroll_into_view_if_needed(timeout=5000)
                save_button.click(timeout=8000)
            except Exception as exc:
                shot = self._snap(page, "draft_save_click_failed")
                self._last_error = f"Draft & Publish: Save Draft click failed: {type(exc).__name__}: {str(exc)[:140]}" + (f"; screenshot={shot}" if shot else "")
                return None

            # Let Facebook finish its save transition before returning to create the next item.
            try:
                page.wait_for_timeout(2200)
            except Exception:
                human.human_pause(1800, 2600)

            # Create Drafts only: reserve selected images as soon as Save Draft
            # completes, before navigating to Your Drafts for verification. The
            # callback is absent in the locked Draft & Publish action, so its
            # behavior remains unchanged.
            _saved_callback = getattr(self, "_fms_draft_saved_callback", None)
            if callable(_saved_callback):
                try:
                    _saved_callback(payload)
                except Exception as _callback_exc:
                    self._last_error = (
                        "Draft saved but image cooldown callback failed: "
                        f"{type(_callback_exc).__name__}: {str(_callback_exc)[:120]}")

            title = str(payload.get("title") or "").strip()
            if not title:
                self._last_error = "Draft & Publish: saved item has no title to identify it in Your Drafts"
                return None

            # The publish phase will use the exact title to open this draft from
            # Facebook's visible Drafts list. Do not require listing_id here.
            try:
                # Draft & Publish 1 ONLY: wait without a fixed deadline. Facebook can
                # take seconds or minutes; document load alone is not list readiness.
                page.goto("https://www.facebook.com/marketplace/you/selling?state=DRAFT", wait_until="domcontentloaded", timeout=0)
                page.wait_for_load_state("load", timeout=0)
                # Wait for this run's exact draft card/title to be visible. timeout=0
                # means Playwright waits until the condition is met, not for a time limit.
                page.get_by_text(title, exact=True).first.wait_for(state="visible", timeout=0)
            except Exception as exc:
                self._last_error = f"Draft saved; could not open Your Drafts yet: {type(exc).__name__}: {str(exc)[:120]}"
                return None

            # Return a workflow-local token. This is NOT represented as a Facebook ID.
            # publish_draft recognizes it and opens the corresponding card in a new tab.
            try:
                page.goto("https://www.facebook.com/marketplace/create/item",
                          wait_until="domcontentloaded", timeout=30000)
                human.human_pause(1200, 2200)
            except Exception:
                pass
            return "draft-title:" + title
        except Exception as exc:
            self._last_error = f"Draft & Publish save failed: {type(exc).__name__}: {str(exc)[:160]}"
            return None

    def create_draft(self, payload: dict) -> str | None:
        """Fill and save one draft; refresh the same Item for Sale page for the next draft."""
        # The specialized, state-aware form workflow is explicitly enabled by
        # Draft & Publish and Create Drafts. All other callers retain the legacy path.
        if getattr(self, "_fms_draft_publish_fill", False):
            return self._create_draft_draft_publish(payload)
        self._ensure()
        page = self._page
        assert page is not None
        try:
            # The flag is set only by Draft & Publish and Create Drafts.
            # Script draft blocks and other callers keep the shared _fill_form.
            _fill = (self._fill_form_draft_publish
                     if getattr(self, "_fms_draft_publish_fill", False)
                     else self._fill_form)
            if not _fill(page, payload):
                self._last_error = (getattr(self, "_last_form_error", None)
                                    or "form fill failed")
                return None
            human.human_click(page, S.to_playwright(page, S.F_SAVE_DRAFT_BUTTON))
            human.human_pause(2500, 4000)
            import re
            draft_id = None
            # Facebook may expose the saved listing id in either the URL or DOM.
            url = page.url
            m = re.search(r"listing_id=(\d+)", url)
            if m:
                draft_id = m.group(1)
            if not draft_id:
                try:
                    for a in page.locator("a[href*='listing_id=']").all():
                        href = a.get_attribute("href") or ""
                        m = re.search(r"listing_id=(\d+)", href)
                        if m:
                            draft_id = m.group(1)
                            break
                except Exception:
                    pass
            # Keep the existing browser/page and return to the same Item for Sale
            # flow by refreshing the current page after Facebook confirms save.
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
                human.human_pause(1200, 2200)
            except Exception:
                pass
            return draft_id or None
        except Exception as exc:
            self._last_error = f"draft save failed: {type(exc).__name__}: {str(exc)[:160]}"
            return None

    def publish_draft(self, draft_id: str) -> bool:
        """Draft & Publish: open a queued draft from Your Drafts in a NEW tab and publish it.

        Normal ID-based callers keep the existing exact-ID path. The isolated new
        Draft & Publish workflow passes draft-title:<title>, which follows the
        user's requested Drafts-list -> new tab -> Publish sequence.
        """
        self._ensure()
        page = self._page
        assert page is not None
        import re
        token = str(draft_id or "").strip()
        if token.startswith("draft-title:"):
            title = token[len("draft-title:"):].strip()
            if not title:
                self._last_error = "Draft title token was empty"
                return False
            tab = None
            try:
                # Draft & Publish 1 ONLY: no fixed timeout for loading the list.
                # Wait for both document load and the exact requested draft to render.
                page.goto("https://www.facebook.com/marketplace/you/selling?state=DRAFT", wait_until="domcontentloaded", timeout=0)
                page.wait_for_load_state("load", timeout=0)
                page.get_by_text(title, exact=True).first.wait_for(state="visible", timeout=0)

                # Find the title on the drafts page and walk upward to its card.
                # Prefer the newest/first exact-title card because Facebook lists
                # the latest saved drafts first. Each successful publish removes it
                # from this list, so repeated titles naturally advance to the next.
                title_nodes = page.get_by_text(title, exact=True)
                count = min(title_nodes.count(), 30)
                target_href = None
                for i in range(count):
                    node = title_nodes.nth(i)
                    ancestor = node
                    for _depth in range(8):
                        try:
                            links = ancestor.locator("a[href]")
                            for j in range(min(links.count(), 20)):
                                href = links.nth(j).get_attribute("href") or ""
                                if href and ("marketplace" in href or "listing" in href or "item" in href):
                                    target_href = href
                                    break
                        except Exception:
                            pass
                        if target_href:
                            break
                        try:
                            ancestor = ancestor.locator("xpath=..")
                        except Exception:
                            break
                    if target_href:
                        break

                if not target_href:
                    self._last_error = f"Your Drafts: could not find an openable card for '{title}'"
                    return False
                if target_href.startswith("/"):
                    target_href = "https://www.facebook.com" + target_href

                # Open the selected draft in a fresh browser tab, preserving the
                # original Drafts tab for the next item and existing tab workflow.
                assert self._context is not None
                tab = self._context.new_page()
                # Draft & Publish 1 ONLY: wait indefinitely for the draft page to load.
                # Then wait for its exact title and an enabled Publish action, proving
                # the listing editor is ready instead of closing the tab prematurely.
                tab.goto(target_href, wait_until="domcontentloaded", timeout=0)
                tab.wait_for_load_state("load", timeout=0)
                tab.get_by_text(title, exact=True).first.wait_for(state="visible", timeout=0)

                # Draft & Publish 1 ONLY: the edit-listing form requires Next before
                # Facebook reveals the final Publish action. Wait for a visible,
                # enabled Next control indefinitely; do not treat a partially loaded
                # editor as ready and do not introduce a fixed time limit.
                next_candidates = [
                    tab.get_by_role("button", name=re.compile(r"^\s*next\s*$", re.I)),
                    tab.locator("div[role='button'][aria-label='Next'], button[aria-label='Next' i]"),
                    tab.get_by_text(re.compile(r"^\s*next\s*$", re.I), exact=True),
                ]
                next_button = None
                while next_button is None:
                    for candidate in next_candidates:
                        try:
                            total = min(candidate.count(), 20)
                            for j in range(total):
                                item = candidate.nth(j) if total > 1 else candidate
                                if item.is_visible() and item.is_enabled():
                                    next_button = item
                                    break
                            if next_button is not None:
                                break
                        except Exception:
                            continue
                    if next_button is None:
                        tab.wait_for_timeout(1000)
                next_button.scroll_into_view_if_needed(timeout=5000)
                human.human_pause(500, 900)
                next_button.click(timeout=10000)

                # The final Publish action only appears after Next. Facebook may
                # progressively hydrate this screen, so keep checking until a
                # visible, enabled Publish control exists; no elapsed-time cutoff.
                buttons = [
                    tab.get_by_role("button", name=re.compile(r"^\s*publish(?:\s+listing)?\s*$", re.I)),
                    tab.locator("div[role='button'][aria-label='Publish'], button[aria-label*='Publish' i]"),
                    tab.get_by_text(re.compile(r"^\s*publish\s*$", re.I), exact=True),
                ]
                publish_button = None
                while publish_button is None:
                    for candidate in buttons:
                        try:
                            total = min(candidate.count(), 20)
                            for j in range(total):
                                item = candidate.nth(j) if total > 1 else candidate
                                if item.is_visible() and item.is_enabled():
                                    publish_button = item
                                    break
                            if publish_button is not None:
                                break
                        except Exception:
                            continue
                    if publish_button is None:
                        tab.wait_for_timeout(1000)
                publish_button.scroll_into_view_if_needed(timeout=5000)
                human.human_pause(500, 900)
                publish_button.click(timeout=10000)
                human.human_pause(2500, 4000)
                # The click is the requested action. Allow Facebook time to process
                # it; if the editor clearly remains unchanged, report pending.
                try:
                    if tab.get_by_role("button", name=re.compile(r"^\s*publish(?:\s+listing)?\s*$", re.I)).count() > 0:
                        self._last_error = f"Publish was clicked for '{title}', but Facebook still shows the Publish action"
                        return False
                except Exception:
                    pass
                return True
            except Exception as exc:
                self._last_error = f"Draft-list publish failed for '{title}': {type(exc).__name__}: {str(exc)[:140]}"
                return False
            finally:
                if tab is not None:
                    try:
                        tab.close()
                    except Exception:
                        pass

        # NOTE (v0.2.131 scope): the exact-ID path below is preserved
        # verbatim from v0.2.130 so Action -> Publish Drafts is untouched.
        draft_id = str(draft_id or "").strip()
        if not draft_id or draft_id.lower() == "draft":
            self._last_error = "Facebook did not return a real draft listing_id; refusing generic Publish click"
            return False
        try:
            page.goto(S.MKT_DRAFTS_URL, wait_until="domcontentloaded")
            human.human_pause(1800, 2800)
            # Facebook's drafts page can render progressively; allow a short settle.
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            # Find a link/card tied to this exact listing id. Never click a generic
            # Publish button unless it belongs to the matching draft card.
            candidates = page.locator(f"a[href*='listing_id={draft_id}'], a[href*='/{draft_id}']")
            count = candidates.count()
            target = None
            for i in range(min(count, 30)):
                link = candidates.nth(i)
                try:
                    href = link.get_attribute("href") or ""
                    if draft_id not in href:
                        continue
                    card = link
                    for _ in range(6):
                        try:
                            if card.locator("div[aria-label='Publish'][role='button'], button[aria-label*='Publish' i]").count():
                                target = card
                                break
                            card = card.locator("xpath=..")
                        except Exception:
                            break
                    if target is not None:
                        break
                except Exception:
                    continue
            if target is None:
                # Alternate DOM path: locate the smallest ancestor containing the
                # exact draft URL and a Publish action.
                exact = page.locator(f"[href*='listing_id={draft_id}']")
                for i in range(min(exact.count(), 30)):
                    el = exact.nth(i)
                    try:
                        ancestor = el
                        for _ in range(8):
                            ancestor = ancestor.locator("xpath=..")
                            buttons = ancestor.locator("div[aria-label='Publish'][role='button'], button[aria-label*='Publish' i]")
                            if buttons.count() == 1:
                                target = ancestor
                                break
                        if target is not None:
                            break
                    except Exception:
                        continue
            if target is None:
                self._last_error = f"Could not find Publish action attached to draft {draft_id}"
                return False
            button = target.locator("div[aria-label='Publish'][role='button'], button[aria-label*='Publish' i]").first
            human.human_click(page, button)
            human.human_pause(2500, 4000)
            # Success only if UI navigates away from drafts or the matching draft
            # is no longer present; do not mark success just because click returned.
            try:
                if page.locator(f"[href*='listing_id={draft_id}']").count() == 0:
                    return True
            except Exception:
                pass
            if "/marketplace/item/" in page.url:
                return True
            self._last_error = f"Publish click was attempted for draft {draft_id}, but success was not confirmed"
            return False
        except Exception as exc:
            self._last_error = f"publish draft {draft_id} failed: {type(exc).__name__}: {str(exc)[:160]}"
            return False

    # -- Simple list-order Publish Drafts (user design 2026-10-28) -----------
    # Open the Drafts list, let it fully load, then take draft cards one
    # by one in page order into a fresh tab and click Next -> Publish.
    # No title search, no title verification, no publish confirmation:
    # the Publish click itself counts as published. Every wait is bounded,
    # so a missing card or button can never hang the run the way the
    # unbounded title wait did. publish_draft() is untouched: Draft &
    # Publish 1 keeps using it.
    def reset_list_publish(self) -> None:
        self._fms_list_tried: set[str] = set()

    def _list_draft_links(self, page) -> list[dict]:
        """Collect untried draft-card links from the loaded list page, page order."""
        from urllib.parse import urlparse
        tried = getattr(self, "_fms_list_tried", set())
        links: list[dict] = []
        seen: set[str] = set()
        try:
            anchors = page.locator("a[href]")
            total = min(anchors.count(), 200)
            for i in range(total):
                try:
                    el = anchors.nth(i)
                    href = (el.get_attribute("href") or "").strip()
                except Exception:
                    continue
                if href.startswith("/"):
                    href = "https://www.facebook.com" + href
                if not href or href in seen or href in tried:
                    continue
                parsed = urlparse(href)
                low_path = parsed.path.lower().rstrip("/")
                low_query = parsed.query.lower()
                # Marketplace is part of the URL path, not the hostname.
                # Accept Facebook subdomains (www.facebook.com, m.facebook.com)
                # while rejecting unrelated hosts that merely contain the text.
                host = (parsed.hostname or "").lower()
                if host != "facebook.com" and not host.endswith(".facebook.com"):
                    continue
                # Only accept links that look like an actual saved-draft editor.
                # Broad terms such as "listing" or "item" also match public
                # listing-detail links and the generic Create Item form, which
                # caused extra, incorrect tabs to open.
                if low_path in ("/marketplace", "/marketplace/you/selling",
                                "/marketplace/you/drafts", "/marketplace/create",
                                "/marketplace/create/item") and not low_query:
                    continue
                draft_query = any(key in low_query for key in (
                    "draft_id=", "draftid=", "draft_id%3d", "draftid%3d",
                    "is_draft=", "state=draft", "draft_token="))
                draft_route = ("/draft" in low_path or "/edit" in low_path)
                create_editor_with_draft_id = (
                    low_path in ("/marketplace/create/item", "/marketplace/create")
                    and draft_query)
                if not (draft_query or draft_route or create_editor_with_draft_id):
                    continue
                try:
                    label = (el.inner_text() or "").strip()
                except Exception:
                    label = ""
                seen.add(href)
                links.append({"href": href, "label": label})
        except Exception:
            pass
        return links

    def _wait_button(self, tab, candidates, bound_s: int = 60):
        """Poll button candidates until one is visible+enabled, else None."""
        import time
        deadline = time.time() + bound_s
        while time.time() < deadline:
            for candidate in candidates:
                try:
                    total = min(candidate.count(), 20)
                    for j in range(total):
                        item = candidate.nth(j) if total > 1 else candidate
                        if item.is_visible() and item.is_enabled():
                            return item
                except Exception:
                    continue
            try:
                tab.wait_for_timeout(1000)
            except Exception:
                return None
        return None

    def publish_next_draft_from_list(self, location_lookup=None) -> dict:
        """Publish the next untried draft card, applying its saved location first.

        location_lookup(label) may return the saved location for this draft.
        """
        self._ensure()
        page = self._page
        assert page is not None
        import re
        import time
        tried = getattr(self, "_fms_list_tried", None)
        if tried is None:
            tried = self._fms_list_tried = set()
        try:
            page.goto("https://www.facebook.com/marketplace/you/selling?state=DRAFT",
                      wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            return {"ok": False, "label": None,
                    "reason": f"Drafts list did not open: {type(exc).__name__}",
                    "empty": False}
        try:
            page.wait_for_load_state("load", timeout=60000)
        except Exception:
            pass  # Page may keep polling; collect whatever rendered.
        # Let the list render, then take whatever cards are present.
        links: list[dict] = []
        deadline = time.time() + 30
        while time.time() < deadline:
            links = self._list_draft_links(page)
            if links:
                break
            try:
                page.wait_for_timeout(1000)
            except Exception:
                break
        if not links:
            return {"ok": False, "label": None, "reason": None, "empty": True}
        card = links[0]
        href = card["href"]
        tab = None
        base_pages = set(self._context.pages) if self._context is not None else set()
        try:
            assert self._context is not None
            tab = self._context.new_page()
            tab.goto(href, wait_until="domcontentloaded", timeout=60000)
            try:
                tab.wait_for_load_state("load", timeout=60000)
            except Exception:
                pass
            # Location is the only additional step: resolve it from this
            # draft's saved product/listing record, then select it before Next.
            location = None
            if callable(location_lookup):
                try:
                    location = (location_lookup(card.get("label") or "") or "").strip()
                except Exception:
                    location = None
            if not location:
                # Fail closed: never advance/publish a draft when its saved
                # location could not be resolved from the product/listing record.
                return {"ok": False, "label": card["label"] or None,
                        "reason": "Saved location was not found for this draft; Next was not clicked",
                        "empty": False}

            # The editor tab can exist before Facebook has rendered the full
            # form. Wait up to 60 seconds for the actual Location input (not
            # merely DOMContentLoaded/load) before trying to fill it.
            location_input = None
            location_field_detail = ""
            location_deadline = time.time() + 60
            while time.time() < location_deadline:
                try:
                    candidate, location_field_detail = self._find_location_input_in_section(tab)
                    if candidate is not None and candidate.is_visible() and candidate.is_enabled():
                        location_input = candidate
                        break
                except Exception:
                    pass
                try:
                    tab.wait_for_timeout(750)
                except Exception:
                    break
            if location_input is None:
                return {"ok": False, "label": card["label"] or None,
                        "reason": "Draft form Location field did not load within 60 seconds; Next was not clicked",
                        "empty": False}

            location_ok, location_detail = self._select_location(tab, location)
            if not location_ok:
                return {"ok": False, "label": card["label"] or None,
                        "reason": f"Saved location could not be selected: {location_detail}",
                        "empty": False}

            # Re-check the selected value before enabling the Next step.
            try:
                selected_location = (location_input.input_value() or "").strip()
            except Exception:
                selected_location = ""
            if not selected_location or location.split(",")[0].strip().lower() not in selected_location.lower():
                return {"ok": False, "label": card["label"] or None,
                        "reason": "Location selection could not be verified; Next was not clicked",
                        "empty": False}

            next_candidates = [
                tab.get_by_role("button", name=re.compile(r"^\s*next\s*$", re.I)),
                tab.locator("div[role='button'][aria-label='Next'], button[aria-label='Next' i]"),
                tab.get_by_text(re.compile(r"^\s*next\s*$", re.I), exact=True),
            ]
            next_button = self._wait_button(tab, next_candidates, bound_s=60)
            if next_button is None:
                return {"ok": False, "label": card["label"] or None,
                        "reason": "Next button not found in draft editor", "empty": False}
            try:
                next_button.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            human.human_pause(500, 900)
            next_button.click(timeout=10000)
            publish_candidates = [
                tab.get_by_role("button", name=re.compile(r"^\s*publish(?:\s+listing)?\s*$", re.I)),
                tab.locator("div[role='button'][aria-label='Publish'], button[aria-label*='Publish' i]"),
                tab.get_by_text(re.compile(r"^\s*publish\s*$", re.I), exact=True),
            ]
            publish_button = self._wait_button(tab, publish_candidates, bound_s=60)
            if publish_button is None:
                return {"ok": False, "label": card["label"] or None,
                        "reason": "Publish button not found in draft editor", "empty": False}
            try:
                publish_button.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            human.human_pause(500, 900)
            publish_button.click(timeout=10000)
            # Allow Facebook 2–3 seconds to process the Publish click before
            # moving to the next saved draft. Do not open or navigate any other
            # listing/create-item pages as part of this action.
            human.human_pause(2000, 3000)
            return {"ok": True, "label": card["label"] or None, "reason": None, "empty": False}
        except Exception as exc:
            return {"ok": False, "label": card["label"] or None,
                    "reason": f"Draft editor failed: {type(exc).__name__}", "empty": False}
        finally:
            tried.add(href)
            # Close the editor tab created for this draft and any unexpected
            # popup tabs spawned by Facebook during this one-draft operation.
            # Preserve the original Drafts-list tab and any pre-existing tabs.
            if self._context is not None:
                for extra_tab in list(self._context.pages):
                    if extra_tab in base_pages:
                        continue
                    try:
                        extra_tab.close()
                    except Exception:
                        pass

    # -- Phase 2: manual-publish helper ------------------------------------------
    def fill_listing_form(self, payload: dict) -> str | None:
        """Open a NEW tab, fill the form, do NOT publish. Returns a tab handle."""
        self._ensure()
        assert self._context is not None
        try:
            tab = self._context.new_page()
            if not self._fill_form(tab, payload):
                try:
                    tab.close()
                except Exception:
                    pass
                return None
            return f"tab-{len(self._context.pages)}"
        except Exception:
            return None

    # -- Phase 2: relist / edit ---------------------------------------------------
    def get_my_listings(self) -> list[dict]:
        """Read the account's 'Your listings' page. Best-effort."""
        self._ensure()
        page = self._page
        assert page is not None
        out: list[dict] = []
        try:
            page.goto(S.MKT_YOUR_LISTINGS_URL, wait_until="domcontentloaded")
            human.human_pause(2000, 3500)
            links = page.locator(S.SCR_MY_LISTING_LINKS[1]).all()
            seen: set[str] = set()
            for link in links[:50]:
                try:
                    href = link.get_attribute("href") or ""
                except Exception:
                    continue
                if "/marketplace/item/" not in href:
                    continue
                lid = href.rstrip("/").split("/")[-1].split("?")[0]
                if lid and lid not in seen:
                    seen.add(lid)
                    try:
                        title = (link.inner_text() or "").strip()[:120]
                    except Exception:
                        title = ""
                    out.append({"id": lid, "title": title})
        except Exception:
            pass
        return out

    def edit_listing(self, listing_id: str, payload: dict) -> bool:
        """Open the Edit page and rewrite title/price/category/description."""
        self._ensure()
        page = self._page
        assert page is not None
        try:
            page.goto(S.MKT_EDIT_URL(listing_id), wait_until="domcontentloaded")
            human.human_pause(1500, 3000)
            try:
                t = S.to_playwright(page, S.F_TITLE)
                t.wait_for(timeout=12000)
                t.fill("")
                self._type(t, payload.get("title", ""), hybrid_ok=True)
            except Exception:
                pass
            try:
                p = S.to_playwright(page, S.F_PRICE)
                p.fill("")
                self._type(p, str(payload.get("price", "")))
            except Exception:
                pass
            if payload.get("category"):
                try:
                    c = S.to_playwright(page, S.F_CATEGORY)
                    c.fill("")
                    self._type(c, payload["category"])
                    human.human_pause(1200, 2200)
                    first = S.to_playwright(page, S.F_LOCATION_FIRST_RESULT)
                    first.wait_for(timeout=6000)
                    human.human_click(page, first)
                except Exception:
                    pass
            if payload.get("description"):
                try:
                    d = S.to_playwright(page, S.F_DESCRIPTION)
                    d.fill("")
                    self._type(d, payload["description"], hybrid_ok=True)
                except Exception:
                    pass
            human.human_click(page, S.to_playwright(page, S.F_SAVE_EDIT_BUTTON))
            human.human_pause(2500, 5000)
            return True
        except Exception:
            return False

    # -- Phase 2: scrap (read-only) -------------------------------------------------
    def open_marketplace(self) -> None:
        """Navigate the main page to Marketplace (Download Images & Titles:
        the user then opens wanted products each in a new tab)."""
        self._ensure()
        page = self._page
        assert page is not None
        page.goto(S.MARKETPLACE_URL, wait_until="domcontentloaded")
        human.human_pause()

    def download_open_tabs(self, dest_dir: str) -> list[dict]:
        """Download images + titles from every open tab except the first
        (main) one. The user opened each wanted product in its own tab.
        Returns [{title, photo_paths}]."""
        import urllib.request

        self._ensure()
        dest = Path(dest_dir)
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except Exception:
            return []
        results: list[dict] = []
        for i, pg in enumerate(self._context.pages[1:]):
            try:
                data = self._extract_listing_from_page(pg)
                if not data or not data.get("title"):
                    continue
                photo_paths: list[str] = []
                for j, photo_url in enumerate(data.get("photo_urls") or []):
                    target = dest / f"tab{i:02d}_{j:03d}.jpg"
                    try:
                        req = urllib.request.Request(
                            photo_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=30) as resp, \
                                open(target, "wb") as fh:
                            fh.write(resp.read())
                        photo_paths.append(str(target))
                    except Exception:
                        continue
                results.append({"title": data["title"],
                                "photo_paths": photo_paths})
            except Exception:
                continue
        return results

    def _extract_listing_from_page(self, page) -> dict | None:
        """Extract title + photo URLs from an already-open listing page."""
        try:
            def _text(selector):
                try:
                    el = S.to_playwright(page, selector)
                    el.wait_for(timeout=5000)
                    return (el.inner_text() or "").strip()
                except Exception:
                    return ""

            title = _text(S.SCR_TITLE)
            if not title:
                return None
            photo_urls: list[str] = []
            try:
                for img in page.locator(S.SCR_PHOTOS[1]).all()[:12]:
                    try:
                        src = img.get_attribute("src")
                        if src and src.startswith("http"):
                            photo_urls.append(src)
                    except Exception:
                        continue
            except Exception:
                pass
            return {"title": title, "photo_urls": photo_urls,
                    "url": page.url}
        except Exception:
            return None

    def scrape_listing(self, url: str) -> dict | None:
        """Extract public fields from one listing URL. Best-effort, read-only."""
        self._ensure()
        page = self._page
        assert page is not None
        try:
            page.goto(url, wait_until="domcontentloaded")
            human.human_pause(1500, 3000)

            def _text(selector):
                try:
                    el = S.to_playwright(page, selector)
                    el.wait_for(timeout=5000)
                    return (el.inner_text() or "").strip()
                except Exception:
                    return ""

            title = _text(S.SCR_TITLE)
            if not title:
                return None
            photo_urls: list[str] = []
            try:
                for img in page.locator(S.SCR_PHOTOS[1]).all()[:12]:
                    try:
                        src = img.get_attribute("src")
                        if src and src.startswith("http"):
                            photo_urls.append(src)
                    except Exception:
                        continue
            except Exception:
                pass
            return {
                "title": title,
                "price": _text(S.SCR_PRICE),
                "description": _text(S.SCR_DESCRIPTION),
                "photo_urls": photo_urls,
                "location": _text(S.SCR_LOCATION),
                "availability": _text(S.SCR_AVAILABILITY) or "In Stock",
                "url": url,
            }
        except Exception:
            return None

    # -- Alignment (v6.1.2.0): delete / renew / location / download ----------
    def delete_listing(self, listing_id: str) -> bool:
        """Best-effort single-listing deletion via the listing page."""
        self._ensure()
        page = self._page
        assert page is not None
        try:
            self._pause_checkpoint()
            page.goto(f"https://www.facebook.com/marketplace/item/{listing_id}/",
                      wait_until="domcontentloaded")
            self._pause_checkpoint()
            human.human_pause(1500, 3000)
            human.human_click(page, S.to_playwright(page, S.F_DELETE_LISTING_BUTTON))
            self._pause_checkpoint()
            human.human_pause(1000, 2000)
            human.human_click(page, S.to_playwright(page, S.F_CONFIRM_DELETE_BUTTON))
            self._pause_checkpoint()
            human.human_pause(2500, 4000)
            return True
        except Exception:
            return False

    def delete_drafts(self) -> int:
        """Best-effort: discard every draft on the drafts page."""
        self._ensure()
        page = self._page
        assert page is not None
        deleted = 0
        try:
            page.goto(S.MKT_DRAFTS_URL, wait_until="domcontentloaded")
            human.human_pause(1500, 3000)
            for _ in range(50):  # safety cap
                try:
                    btn = S.to_playwright(page, S.F_DISCARD_DRAFT_BUTTON)
                    btn.wait_for(timeout=4000)
                except Exception:
                    break
                human.human_click(page, btn)
                human.human_pause(1000, 2000)
                try:
                    confirm = S.to_playwright(page, S.F_CONFIRM_DELETE_BUTTON)
                    confirm.wait_for(timeout=3000)
                    human.human_click(page, confirm)
                    human.human_pause(1000, 2000)
                except Exception:
                    pass
                deleted += 1
        except Exception:
            pass
        return deleted

    def delete_duplicate_listings(self) -> list[str]:
        """Delete all but the first listing of each normalized-title group."""
        import re

        def _norm(title: str) -> str:
            return re.sub(r"\s+", " ", (title or "").strip().lower())

        deleted: list[str] = []
        try:
            seen: dict[str, str] = {}
            for lst in self.get_my_listings():
                key = _norm(lst.get("title", ""))
                if key and key in seen:
                    if self.delete_listing(lst["id"]):
                        deleted.append(lst["id"])
                elif key:
                    seen[key] = lst["id"]
        except Exception:
            pass
        return deleted

    def renew_listing(self, listing_id: str) -> bool:
        """Best-effort listing renewal via the listing page."""
        self._ensure()
        page = self._page
        assert page is not None
        try:
            self._pause_checkpoint()
            page.goto(f"https://www.facebook.com/marketplace/item/{listing_id}/",
                      wait_until="domcontentloaded")
            self._pause_checkpoint()
            human.human_pause(1500, 3000)
            human.human_click(page, S.to_playwright(page, S.F_RENEW_BUTTON))
            self._pause_checkpoint()
            human.human_pause(2500, 4000)
            return True
        except Exception:
            return False

    def renew_listings_from_page(self, max_count: int) -> dict:
        """Existing Renew Listing action; preserve its current behaviour."""
        return self._renew_listings_from_page_impl(max_count, row_scoped=False)

    def renew_listings_from_rows(self, max_count: int) -> dict:
        """Renew Listing 1: find each visible row's actual Renew button without parsing item IDs."""
        return self._renew_listings_from_page_impl(max_count, row_scoped=True)

    def _renew_listings_from_page_impl(self, max_count: int, row_scoped: bool = False) -> dict:
        """Renew eligible listings one at a time, scrolling the full renew list.
        The renew-list dialog is scanned from top to bottom, clicking each
        visible Renew button in order. After the full current list is scanned,
        the page is refreshed and any listing still eligible is attempted again
        until max_count is reached or Facebook displays an empty state. No
        confirmation is awaited after clicks; existing pause checkpoints remain.
        """
        self._ensure()
        page = self._page
        assert page is not None
        renewed: list[dict] = []
        failed = 0
        max_count = max(0, int(max_count))
        renew_url = "https://www.facebook.com/marketplace/selling/renew_listings/?is_routable_dialog=true"
        empty_phrases = (
            "no eligible listing", "no eligible listings",
            "no listings to renew", "no listings available to renew",
            "nothing to renew", "you have no eligible listing",
            "you have no eligible listings",
        )
        # Track successful/failed attempts only for the current full-list pass.
        # If a listing is still eligible after refresh, it must be clicked again.
        processed: set[str] = set()
        attempted_this_pass: set[str] = set()

        def renew_buttons():
            # Facebook commonly renders the action as a button whose visible
            # label is only "Renew" (not "Renew listing"). Always return a
            # clickable button/role=button; never return the inner text span.
            selectors = (
                "button[aria-label*='Renew' i], "
                "[role='button'][aria-label*='Renew' i]",
                "button:has-text('Renew listing'), "
                "[role='button']:has-text('Renew listing')",
                "button:has-text('Renew'), "
                "[role='button']:has-text('Renew')",
            )
            for selector in selectors:
                buttons = page.locator(selector)
                if buttons.count():
                    return buttons
            return page.locator("button:has-text('Renew'), [role='button']:has-text('Renew')")

        def wait_for_ready_content() -> str:
            # Wait for Facebook to render the list, but do not hang forever if
            # a refresh leaves the dialog with no eligible listings and no
            # localized/recognized empty-state phrase. A full 30-second grace
            # period protects slower page loads before treating the list as empty.
            no_content_since = time.monotonic()
            while True:
                self._pause_checkpoint()
                try:
                    buttons = renew_buttons()
                    for i in range(min(buttons.count(), 300)):
                        btn = buttons.nth(i)
                        try:
                            if btn.is_visible() and btn.is_enabled():
                                return "listings"
                        except Exception:
                            continue
                    # Only treat an empty state as terminal when the Renew
                    # Listings dialog itself says so. Page-wide text can contain
                    # unrelated Marketplace/sidebar text and must not end the
                    # configured Min/Max renewal run early.
                    dialog = page.locator("[role='dialog']")
                    if dialog.count() > 0:
                        dialog_text = (dialog.last.inner_text(timeout=0) or "").lower()
                        if any(phrase in dialog_text for phrase in empty_phrases):
                            # Recheck live controls in the dialog before calling
                            # it empty; a stale/hidden message must not override
                            # currently available clickable Renew buttons.
                            dialog_buttons = dialog.last.locator(
                                "button[aria-label*='Renew' i], "
                                "[role='button'][aria-label*='Renew' i], "
                                "button:has-text('Renew listing'), "
                                "[role='button']:has-text('Renew listing'), "
                                "button:has-text('Renew'), "
                                "[role='button']:has-text('Renew')"
                            )
                            live_button = False
                            for j in range(min(dialog_buttons.count(), 300)):
                                try:
                                    candidate = dialog_buttons.nth(j)
                                    if candidate.is_visible() and candidate.is_enabled():
                                        live_button = True
                                        break
                                except Exception:
                                    continue
                            if not live_button:
                                return "empty"
                        # The dialog exists but has no live Renew control. If
                        # Facebook has finished rendering and still has no
                        # eligible row after refresh, end this account cleanly.
                        if time.monotonic() - no_content_since >= 30:
                            return "empty"
                    else:
                        # Do not apply the empty timeout until the target dialog
                        # has appeared; it may still be loading after navigation.
                        no_content_since = time.monotonic()
                except Exception:
                    pass
                time.sleep(0.5)

        def listing_row_candidates():
            """Yield row-scoped Renew controls. Renew Listing 1 never parses item IDs."""
            if row_scoped:
                # Renew listing 1: treat each actual clickable Renew control as
                # its own candidate. Do not deduplicate by an ancestor's text or
                # bounding box: Facebook can wrap several listing rows in one
                # shared div, which previously collapsed the whole list to one.
                buttons = page.locator(
                    "button[aria-label*='Renew' i], "
                    "[role='button'][aria-label*='Renew' i], "
                    "button:has-text('Renew listing'), "
                    "[role='button']:has-text('Renew listing'), "
                    "button:has-text('Renew'), "
                    "[role='button']:has-text('Renew')"
                )
                try:
                    count = min(buttons.count(), 500)
                except Exception:
                    count = 0
                for index in range(count):
                    try:
                        button = buttons.nth(index)
                        if not button.is_visible() or not button.is_enabled():
                            continue
                        # Walk upward from the actual button and pick the
                        # smallest ancestor that contains exactly one Renew
                        # control. This avoids accidentally treating a wrapper
                        # around multiple listing rows as one row.
                        row = button
                        candidate = button
                        for _ in range(10):
                            try:
                                parent = candidate.locator("xpath=..")
                                if parent.count() < 1:
                                    break
                                parent_buttons = parent.locator(
                                    "button[aria-label*='Renew' i], "
                                    "[role='button'][aria-label*='Renew' i], "
                                    "button:has-text('Renew listing'), "
                                    "[role='button']:has-text('Renew listing'), "
                                    "button:has-text('Renew'), "
                                    "[role='button']:has-text('Renew')"
                                )
                                if parent_buttons.count() == 1:
                                    row = parent
                                    break
                                candidate = parent
                                row = parent
                            except Exception:
                                break
                        row_buttons = row.locator(
                            "button[aria-label*='Renew' i], "
                            "[role='button'][aria-label*='Renew' i], "
                            "button:has-text('Renew listing'), "
                            "[role='button']:has-text('Renew listing'), "
                            "button:has-text('Renew'), "
                            "[role='button']:has-text('Renew')"
                        )
                        # If a usable one-button row could not be identified,
                        # still attempt the actual button rather than silently
                        # skipping a visible Renew control.
                        if row_buttons.count() != 1:
                            row = button
                            row_buttons = (button,)
                        # Track the actual button DOM node for this full-list pass.
                        # Row text/occurrence keys are unstable when a click changes
                        # button text or the DOM re-renders; that can make the next
                        # row inherit a previously processed key and be skipped.
                        # This token is attached to the clickable element itself,
                        # not derived from a listing ID, link, or screen position.
                        try:
                            key = button.evaluate("""el => {
                                if (!el.__fmsRenewAttemptToken) {
                                    el.__fmsRenewAttemptToken =
                                        'renew-button-' + Math.random().toString(36).slice(2)
                                        + '-' + Date.now().toString(36);
                                }
                                return el.__fmsRenewAttemptToken;
                            }""")
                        except Exception:
                            # A locator-level fallback remains unique within this
                            # scan; it is not reused as a persistent listing ID.
                            key = f"renew-button-scan-{index}"
                        yield key, None, row, row_buttons
                    except Exception:
                        # A single malformed DOM candidate must not hide later
                        # visible Renew buttons in the same list.
                        continue
                return

            # Legacy action path retained separately for backward compatibility.
            links = page.locator("a[href*='/marketplace/item/']")
            try:
                count = min(links.count(), 500)
            except Exception:
                count = 0
            seen_rows = set()
            for index in range(count):
                try:
                    link = links.nth(index)
                    href = link.get_attribute("href") or ""
                    match = re.search(r"/marketplace/item/(\d+)", href)
                    listing_id = match.group(1) if match else ""
                    if not listing_id or listing_id in seen_rows:
                        continue
                    row = link.locator(
                        "xpath=ancestor::*[self::div or self::li]"
                        "[.//a[contains(@href,'/marketplace/item/')]]"
                        "[.//button or .//*[@role='button']][1]"
                    )
                    row_buttons = row.locator(
                        "button[aria-label*='Renew' i], "
                        "[role='button'][aria-label*='Renew' i], "
                        "button:has-text('Renew listing'), "
                        "[role='button']:has-text('Renew listing'), "
                        "button:has-text('Renew'), "
                        "[role='button']:has-text('Renew')"
                    )
                    if row_buttons.count() > 0:
                        seen_rows.add(listing_id)
                        yield listing_id, link, row, row_buttons
                except Exception:
                    continue

        def listing_identity(btn) -> tuple[str, str, str]:
            """Return a stable-ish key, display title, and listing id."""
            title = ""
            listing_id = ""
            href = ""
            try:
                card = btn.locator(
                    "xpath=ancestor::*[self::div or self::li]"
                    "[.//a[contains(@href,'/marketplace/item/')]][1]"
                )
                link = card.locator("a[href*='/marketplace/item/']").first
                href = link.get_attribute("href") or ""
                match = re.search(r"/marketplace/item/(\d+)", href)
                listing_id = match.group(1) if match else ""
                title = (link.inner_text() or "").strip()[:160]
                if not title:
                    title = (card.inner_text() or "").strip()[:160]
            except Exception:
                try:
                    title = (btn.locator("xpath=..").inner_text() or "").strip()[:160]
                except Exception:
                    pass
            key = listing_id or href or title
            if not key:
                try:
                    key = (btn.get_attribute("aria-label") or "") + "|" + str(btn.bounding_box())
                except Exception:
                    key = str(id(btn))
            return key, title, listing_id

        def scroll_list_down() -> tuple[bool, int, int]:
            """Scroll the Renew dialog's own right-side list scrollbar."""
            try:
                result = page.locator("div[role='dialog']").evaluate("""dialog => {
                    const nodes = [dialog, ...dialog.querySelectorAll('*')];
                    const candidates = nodes.filter(n => {
                        const s = getComputedStyle(n);
                        return n.scrollHeight > n.clientHeight + 8 &&
                               /(auto|scroll|overlay)/.test(s.overflowY);
                    });
                    const target = candidates.sort((a,b) =>
                        (b.scrollHeight-b.clientHeight) - (a.scrollHeight-a.clientHeight)
                    )[0];
                    if (!target) return {moved:false, top:0, max:0};
                    const before = target.scrollTop;
                    const step = Math.max(180, Math.floor(target.clientHeight * 0.72));
                    target.scrollTop = Math.min(target.scrollHeight, before + step);
                    return {moved:target.scrollTop > before, top:target.scrollTop,
                            max:target.scrollHeight-target.clientHeight};
                }""")
                return bool(result.get("moved")), int(result.get("top", 0)), int(result.get("max", 0))
            except Exception:
                try:
                    result = page.evaluate("""() => {
                        const nodes = [...document.querySelectorAll('*')];
                        const candidates = nodes.filter(n => {
                            const s = getComputedStyle(n);
                            return n.scrollHeight > n.clientHeight + 8 &&
                                   /(auto|scroll|overlay)/.test(s.overflowY) &&
                                   n.querySelector('[aria-label*="Renew" i], button');
                        });
                        const target = candidates.sort((a,b) =>
                            (b.scrollHeight-b.clientHeight) - (a.scrollHeight-a.clientHeight)
                        )[0];
                        if (!target) return {moved:false, top:0, max:0};
                        const before = target.scrollTop;
                        target.scrollTop = Math.min(target.scrollHeight,
                            before + Math.max(180, Math.floor(target.clientHeight*0.72)));
                        return {moved:target.scrollTop > before, top:target.scrollTop,
                                max:target.scrollHeight-target.clientHeight};
                    }""")
                    return bool(result.get("moved")), int(result.get("top", 0)), int(result.get("max", 0))
                except Exception:
                    return False, 0, 0

        try:
            self._pause_checkpoint()
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=0)
            while True:
                self._pause_checkpoint()
                try:
                    body_text = page.locator("body").inner_text(timeout=0).strip()
                    if body_text and len(body_text) >= 80 and page.locator("[role='feed'], [role='main']").count() > 0:
                        break
                except Exception:
                    pass
                time.sleep(0.5)

            self._pause_checkpoint()
            page.goto(S.MARKETPLACE_URL, wait_until="domcontentloaded", timeout=0)
            while True:
                self._pause_checkpoint()
                try:
                    body_text = page.locator("body").inner_text(timeout=0).strip()
                    if ("marketplace" in body_text.lower() and len(body_text) >= 80
                            and page.locator("[role='main']").count() > 0):
                        break
                except Exception:
                    pass
                time.sleep(0.5)

            self._pause_checkpoint()
            page.goto(renew_url, wait_until="domcontentloaded", timeout=0)
            state = wait_for_ready_content()
            if state == "empty":
                return {"renewed": renewed, "no_eligible": True, "failed": 0}

            while len(renewed) < max_count:
                self._pause_checkpoint()
                # Always restart the locator scan after each click: Facebook may
                # remove/re-index rows immediately, so incrementing a stale
                # button index can skip the next listing.
                attempted_button = False
                # Primary path: enumerate listing links, resolve each listing row,
                # then find/click Renew only within that row. Rebuild the entire
                # candidate set after every click because Facebook can re-render it.
                row_candidates = list(listing_row_candidates())
                candidates = []
                if row_scoped:
                    # Renew listing 1 intentionally has no page-wide/ID-based
                    # fallback: only a button found inside a listing row qualifies.
                    for row_key, link, row, row_buttons in row_candidates:
                        title = ""
                        try:
                            if link is not None:
                                title = (link.inner_text() or "").strip()[:160]
                            if not title:
                                title = (row.inner_text() or "").strip()[:160]
                        except Exception:
                            pass
                        candidates.append((row_key, title, row_buttons))
                elif row_candidates:
                    for listing_id, link, row, row_buttons in row_candidates:
                        title = ""
                        try:
                            if link is not None:
                                title = (link.inner_text() or "").strip()[:160]
                            if not title:
                                title = (row.inner_text() or "").strip()[:160]
                        except Exception:
                            pass
                        candidates.append((listing_id, title, row_buttons))
                else:
                    # Legacy compatibility fallback for the existing action only.
                    buttons = renew_buttons()
                    for i in range(min(buttons.count(), 300)):
                        btn = buttons.nth(i)
                        key, title, listing_id = listing_identity(btn)
                        candidates.append((key, title, (btn,)))

                for key, title, row_buttons in candidates:
                    self._pause_checkpoint()
                    try:
                        if key in processed or key in attempted_this_pass:
                            continue
                        btn = row_buttons.nth(0) if hasattr(row_buttons, "nth") else row_buttons[0]
                        if not btn.is_visible() or not btn.is_enabled():
                            continue
                        attempted_button = True
                        try:
                            # Click this row's real clickable control; never wait
                            # for a confirmation message after the click.
                            btn.click(timeout=0)
                            processed.add(key)
                            renewed.append({"id": key if key.isdigit() else "", "title": title})
                            human.human_pause(1000, 2000)
                        except Exception:
                            failed += 1
                            attempted_this_pass.add(key)
                        break  # locate all rows/buttons again after every attempt
                    except Exception:
                        continue

                if len(renewed) >= max_count:
                    break
                if attempted_button:
                    continue

                # No unattempted button remains in this viewport/list. Scroll
                # only the Renew dialog's own list area to expose more rows.
                moved, scroll_top, scroll_max = scroll_list_down()
                if moved:
                    self._pause_checkpoint()
                    while True:
                        self._pause_checkpoint()
                        try:
                            if renew_buttons().count() > 0:
                                break
                        except Exception:
                            pass
                        time.sleep(0.5)
                    continue

                # The full current list has been scanned. Re-open the exact
                # Renew Listings URL instead of relying on a browser reload;
                # Facebook may keep the selling dialog in SPA state and a plain
                # reload can leave the workflow on the same exhausted view.
                # The next ready-content check also detects the genuine empty
                # state after navigation, before another pass begins.
                self._pause_checkpoint()
                page.goto(renew_url, wait_until="domcontentloaded", timeout=0)
                state = wait_for_ready_content()
                if state == "empty":
                    return {"renewed": renewed, "no_eligible": not renewed, "failed": failed}
                processed.clear()
                attempted_this_pass.clear()

            return {"renewed": renewed, "no_eligible": False, "failed": failed}
        except Exception as exc:
            return {"renewed": renewed, "no_eligible": False, "failed": failed,
                    "error": f"{type(exc).__name__}: {exc}"}


    def delete_relist_from_rows(self, max_count: int) -> dict:
        """Process Delete & Relist buttons row-by-row and reload the dedicated URL."""
        return self._delete_relist_from_page_impl(max_count)

    def _delete_relist_from_page_impl(self, max_count: int) -> dict:
        """Delete & Relist eligible listings one at a time, scrolling the full relist list.
        The relist-list dialog is scanned from top to bottom, clicking each
        visible Delete & Relist button in order. After the full current list is scanned,
        the page is refreshed and any listing still eligible is attempted again
        until max_count is reached or Facebook displays an empty state. No
        confirmation is awaited after clicks; existing pause checkpoints remain.
        """
        row_scoped = True
        self._ensure()
        page = self._page
        assert page is not None
        relisted: list[dict] = []
        failed = 0
        max_count = max(0, int(max_count))
        relist_url = "https://www.facebook.com/marketplace/selling/relist_items/?is_routable_dialog=true&show_only_delete_and_relist=true"
        empty_phrases = (
            "no eligible listing", "no eligible listings",
            "no listings to relist", "no listings available to relist",
            "nothing to relist", "you have no eligible listing",
            "you have no eligible listings",
            "you have no more listings eligible to be deleted and relisted",
        )
        # Track successful/failed attempts only for the current full-list pass.
        # If a listing is still eligible after refresh, it must be clicked again.
        processed: set[str] = set()
        attempted_this_pass: set[str] = set()

        def relist_buttons():
            # Facebook commonly renders the action as a button whose visible
            # label is only "Delete & Relist" (not "Delete & Relist"). Always return a
            # clickable button/role=button; never return the inner text span.
            selectors = (
                "button[aria-label*='Delete & Relist' i], "
                "[role='button'][aria-label*='Delete & Relist' i]",
                "button:has-text('Delete & Relist'), "
                "[role='button']:has-text('Delete & Relist')",
                "button:has-text('Delete & Relist'), "
                "[role='button']:has-text('Delete & Relist')",
            )
            for selector in selectors:
                buttons = page.locator(selector)
                if buttons.count():
                    return buttons
            return page.locator("button:has-text('Delete & Relist'), [role='button']:has-text('Delete & Relist')")

        def wait_for_ready_content() -> str:
            # Wait for Facebook to render the list, but do not hang forever if
            # a refresh leaves the dialog with no eligible listings and no
            # localized/recognized empty-state phrase. A full 30-second grace
            # period protects slower page loads before treating the list as empty.
            no_content_since = time.monotonic()
            while True:
                self._pause_checkpoint()
                try:
                    buttons = relist_buttons()
                    for i in range(min(buttons.count(), 300)):
                        btn = buttons.nth(i)
                        try:
                            if btn.is_visible() and btn.is_enabled():
                                return "listings"
                        except Exception:
                            continue
                    # Only treat an empty state as terminal when the Delete & Relist
                    # Listings dialog itself says so. Page-wide text can contain
                    # unrelated Marketplace/sidebar text and must not end the
                    # configured Min/Max relistal run early.
                    dialog = page.locator("[role='dialog']")
                    if dialog.count() > 0:
                        dialog_text = (dialog.last.inner_text(timeout=0) or "").lower()
                        if any(phrase in dialog_text for phrase in empty_phrases):
                            # Recheck live controls in the dialog before calling
                            # it empty; a stale/hidden message must not override
                            # currently available clickable Delete & Relist buttons.
                            dialog_buttons = dialog.last.locator(
                                "button[aria-label*='Delete & Relist' i], "
                                "[role='button'][aria-label*='Delete & Relist' i], "
                                "button:has-text('Delete & Relist'), "
                                "[role='button']:has-text('Delete & Relist'), "
                                "button:has-text('Delete & Relist'), "
                                "[role='button']:has-text('Delete & Relist')"
                            )
                            live_button = False
                            for j in range(min(dialog_buttons.count(), 300)):
                                try:
                                    candidate = dialog_buttons.nth(j)
                                    if candidate.is_visible() and candidate.is_enabled():
                                        live_button = True
                                        break
                                except Exception:
                                    continue
                            if not live_button:
                                return "empty"
                        # The dialog exists but has no live Delete & Relist control. If
                        # Facebook has finished rendering and still has no
                        # eligible row after refresh, end this account cleanly.
                        if time.monotonic() - no_content_since >= 30:
                            return "empty"
                    else:
                        # Do not apply the empty timeout until the target dialog
                        # has appeared; it may still be loading after navigation.
                        no_content_since = time.monotonic()
                except Exception:
                    pass
                time.sleep(0.5)

        def listing_row_candidates():
            """Yield row-scoped Delete & Relist controls. Delete & Relist never parses item IDs."""
            if row_scoped:
                # Delete & Relist: treat each actual clickable Delete & Relist control as
                # its own candidate. Do not deduplicate by an ancestor's text or
                # bounding box: Facebook can wrap several listing rows in one
                # shared div, which previously collapsed the whole list to one.
                buttons = page.locator(
                    "button[aria-label*='Delete & Relist' i], "
                    "[role='button'][aria-label*='Delete & Relist' i], "
                    "button:has-text('Delete & Relist'), "
                    "[role='button']:has-text('Delete & Relist'), "
                    "button:has-text('Delete & Relist'), "
                    "[role='button']:has-text('Delete & Relist')"
                )
                try:
                    count = min(buttons.count(), 500)
                except Exception:
                    count = 0
                for index in range(count):
                    try:
                        button = buttons.nth(index)
                        if not button.is_visible() or not button.is_enabled():
                            continue
                        # Walk upward from the actual button and pick the
                        # smallest ancestor that contains exactly one Delete & Relist
                        # control. This avoids accidentally treating a wrapper
                        # around multiple listing rows as one row.
                        row = button
                        candidate = button
                        for _ in range(10):
                            try:
                                parent = candidate.locator("xpath=..")
                                if parent.count() < 1:
                                    break
                                parent_buttons = parent.locator(
                                    "button[aria-label*='Delete & Relist' i], "
                                    "[role='button'][aria-label*='Delete & Relist' i], "
                                    "button:has-text('Delete & Relist'), "
                                    "[role='button']:has-text('Delete & Relist'), "
                                    "button:has-text('Delete & Relist'), "
                                    "[role='button']:has-text('Delete & Relist')"
                                )
                                if parent_buttons.count() == 1:
                                    row = parent
                                    break
                                candidate = parent
                                row = parent
                            except Exception:
                                break
                        row_buttons = row.locator(
                            "button[aria-label*='Delete & Relist' i], "
                            "[role='button'][aria-label*='Delete & Relist' i], "
                            "button:has-text('Delete & Relist'), "
                            "[role='button']:has-text('Delete & Relist'), "
                            "button:has-text('Delete & Relist'), "
                            "[role='button']:has-text('Delete & Relist')"
                        )
                        # If a usable one-button row could not be identified,
                        # still attempt the actual button rather than silently
                        # skipping a visible Delete & Relist control.
                        if row_buttons.count() != 1:
                            row = button
                            row_buttons = (button,)
                        # Track the actual button DOM node for this full-list pass.
                        # Row text/occurrence keys are unstable when a click changes
                        # button text or the DOM re-renders; that can make the next
                        # row inherit a previously processed key and be skipped.
                        # This token is attached to the clickable element itself,
                        # not derived from a listing ID, link, or screen position.
                        try:
                            key = button.evaluate("""el => {
                                if (!el.__fmsDeleteRelistAttemptToken) {
                                    el.__fmsDeleteRelistAttemptToken =
                                        'relist-button-' + Math.random().toString(36).slice(2)
                                        + '-' + Date.now().toString(36);
                                }
                                return el.__fmsDeleteRelistAttemptToken;
                            }""")
                        except Exception:
                            # A locator-level fallback remains unique within this
                            # scan; it is not reused as a persistent listing ID.
                            key = f"relist-button-scan-{index}"
                        yield key, None, row, row_buttons
                    except Exception:
                        # A single malformed DOM candidate must not hide later
                        # visible Delete & Relist buttons in the same list.
                        continue
                return

            # Legacy action path retained separately for backward compatibility.
            links = page.locator("a[href*='/marketplace/item/']")
            try:
                count = min(links.count(), 500)
            except Exception:
                count = 0
            seen_rows = set()
            for index in range(count):
                try:
                    link = links.nth(index)
                    href = link.get_attribute("href") or ""
                    match = re.search(r"/marketplace/item/(\d+)", href)
                    listing_id = match.group(1) if match else ""
                    if not listing_id or listing_id in seen_rows:
                        continue
                    row = link.locator(
                        "xpath=ancestor::*[self::div or self::li]"
                        "[.//a[contains(@href,'/marketplace/item/')]]"
                        "[.//button or .//*[@role='button']][1]"
                    )
                    row_buttons = row.locator(
                        "button[aria-label*='Delete & Relist' i], "
                        "[role='button'][aria-label*='Delete & Relist' i], "
                        "button:has-text('Delete & Relist'), "
                        "[role='button']:has-text('Delete & Relist'), "
                        "button:has-text('Delete & Relist'), "
                        "[role='button']:has-text('Delete & Relist')"
                    )
                    if row_buttons.count() > 0:
                        seen_rows.add(listing_id)
                        yield listing_id, link, row, row_buttons
                except Exception:
                    continue

        def listing_identity(btn) -> tuple[str, str, str]:
            """Return a stable-ish key, display title, and listing id."""
            title = ""
            listing_id = ""
            href = ""
            try:
                card = btn.locator(
                    "xpath=ancestor::*[self::div or self::li]"
                    "[.//a[contains(@href,'/marketplace/item/')]][1]"
                )
                link = card.locator("a[href*='/marketplace/item/']").first
                href = link.get_attribute("href") or ""
                match = re.search(r"/marketplace/item/(\d+)", href)
                listing_id = match.group(1) if match else ""
                title = (link.inner_text() or "").strip()[:160]
                if not title:
                    title = (card.inner_text() or "").strip()[:160]
            except Exception:
                try:
                    title = (btn.locator("xpath=..").inner_text() or "").strip()[:160]
                except Exception:
                    pass
            key = listing_id or href or title
            if not key:
                try:
                    key = (btn.get_attribute("aria-label") or "") + "|" + str(btn.bounding_box())
                except Exception:
                    key = str(id(btn))
            return key, title, listing_id

        def scroll_list_down() -> tuple[bool, int, int]:
            """Scroll the Delete & Relist dialog's own right-side list scrollbar."""
            try:
                result = page.locator("div[role='dialog']").evaluate("""dialog => {
                    const nodes = [dialog, ...dialog.querySelectorAll('*')];
                    const candidates = nodes.filter(n => {
                        const s = getComputedStyle(n);
                        return n.scrollHeight > n.clientHeight + 8 &&
                               /(auto|scroll|overlay)/.test(s.overflowY);
                    });
                    const target = candidates.sort((a,b) =>
                        (b.scrollHeight-b.clientHeight) - (a.scrollHeight-a.clientHeight)
                    )[0];
                    if (!target) return {moved:false, top:0, max:0};
                    const before = target.scrollTop;
                    const step = Math.max(180, Math.floor(target.clientHeight * 0.72));
                    target.scrollTop = Math.min(target.scrollHeight, before + step);
                    return {moved:target.scrollTop > before, top:target.scrollTop,
                            max:target.scrollHeight-target.clientHeight};
                }""")
                return bool(result.get("moved")), int(result.get("top", 0)), int(result.get("max", 0))
            except Exception:
                try:
                    result = page.evaluate("""() => {
                        const nodes = [...document.querySelectorAll('*')];
                        const candidates = nodes.filter(n => {
                            const s = getComputedStyle(n);
                            return n.scrollHeight > n.clientHeight + 8 &&
                                   /(auto|scroll|overlay)/.test(s.overflowY) &&
                                   n.querySelector('[aria-label*="Delete & Relist" i], button');
                        });
                        const target = candidates.sort((a,b) =>
                            (b.scrollHeight-b.clientHeight) - (a.scrollHeight-a.clientHeight)
                        )[0];
                        if (!target) return {moved:false, top:0, max:0};
                        const before = target.scrollTop;
                        target.scrollTop = Math.min(target.scrollHeight,
                            before + Math.max(180, Math.floor(target.clientHeight*0.72)));
                        return {moved:target.scrollTop > before, top:target.scrollTop,
                                max:target.scrollHeight-target.clientHeight};
                    }""")
                    return bool(result.get("moved")), int(result.get("top", 0)), int(result.get("max", 0))
                except Exception:
                    return False, 0, 0

        try:
            self._pause_checkpoint()
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=0)
            while True:
                self._pause_checkpoint()
                try:
                    body_text = page.locator("body").inner_text(timeout=0).strip()
                    if body_text and len(body_text) >= 80 and page.locator("[role='feed'], [role='main']").count() > 0:
                        break
                except Exception:
                    pass
                time.sleep(0.5)

            self._pause_checkpoint()
            page.goto(S.MARKETPLACE_URL, wait_until="domcontentloaded", timeout=0)
            while True:
                self._pause_checkpoint()
                try:
                    body_text = page.locator("body").inner_text(timeout=0).strip()
                    if ("marketplace" in body_text.lower() and len(body_text) >= 80
                            and page.locator("[role='main']").count() > 0):
                        break
                except Exception:
                    pass
                time.sleep(0.5)

            self._pause_checkpoint()
            page.goto(relist_url, wait_until="domcontentloaded", timeout=0)
            state = wait_for_ready_content()
            if state == "empty":
                return {"relisted": relisted, "no_eligible": True, "failed": 0}

            while len(relisted) < max_count:
                self._pause_checkpoint()
                # Always restart the locator scan after each click: Facebook may
                # remove/re-index rows immediately, so incrementing a stale
                # button index can skip the next listing.
                attempted_button = False
                # Primary path: enumerate listing links, resolve each listing row,
                # then find/click Delete & Relist only within that row. Rebuild the entire
                # candidate set after every click because Facebook can re-render it.
                row_candidates = list(listing_row_candidates())
                candidates = []
                if row_scoped:
                    # Delete & Relist intentionally has no page-wide/ID-based
                    # fallback: only a button found inside a listing row qualifies.
                    for row_key, link, row, row_buttons in row_candidates:
                        title = ""
                        try:
                            if link is not None:
                                title = (link.inner_text() or "").strip()[:160]
                            if not title:
                                title = (row.inner_text() or "").strip()[:160]
                        except Exception:
                            pass
                        candidates.append((row_key, title, row_buttons))
                elif row_candidates:
                    for listing_id, link, row, row_buttons in row_candidates:
                        title = ""
                        try:
                            if link is not None:
                                title = (link.inner_text() or "").strip()[:160]
                            if not title:
                                title = (row.inner_text() or "").strip()[:160]
                        except Exception:
                            pass
                        candidates.append((listing_id, title, row_buttons))
                else:
                    # Legacy compatibility fallback for the existing action only.
                    buttons = relist_buttons()
                    for i in range(min(buttons.count(), 300)):
                        btn = buttons.nth(i)
                        key, title, listing_id = listing_identity(btn)
                        candidates.append((key, title, (btn,)))

                for key, title, row_buttons in candidates:
                    self._pause_checkpoint()
                    try:
                        if key in processed or key in attempted_this_pass:
                            continue
                        btn = row_buttons.nth(0) if hasattr(row_buttons, "nth") else row_buttons[0]
                        if not btn.is_visible() or not btn.is_enabled():
                            continue
                        attempted_button = True
                        try:
                            # Click this row's real clickable control; never wait
                            # for a confirmation message after the click.
                            btn.click(timeout=0)
                            processed.add(key)
                            relisted.append({"id": key if key.isdigit() else "", "title": title})
                            human.human_pause(1000, 2000)
                        except Exception:
                            failed += 1
                            attempted_this_pass.add(key)
                        break  # locate all rows/buttons again after every attempt
                    except Exception:
                        continue

                if len(relisted) >= max_count:
                    break
                if attempted_button:
                    continue

                # No unattempted button remains in this viewport/list. Scroll
                # only the Delete & Relist dialog's own list area to expose more rows.
                moved, scroll_top, scroll_max = scroll_list_down()
                if moved:
                    self._pause_checkpoint()
                    while True:
                        self._pause_checkpoint()
                        try:
                            if relist_buttons().count() > 0:
                                break
                        except Exception:
                            pass
                        time.sleep(0.5)
                    continue

                # The full current list has been scanned. Re-open the exact
                # Delete & Relist URL instead of relying on a browser reload;
                # Facebook may keep the selling dialog in SPA state and a plain
                # reload can leave the workflow on the same exhausted view.
                # The next ready-content check also detects the genuine empty
                # state after navigation, before another pass begins.
                self._pause_checkpoint()
                page.goto(relist_url, wait_until="domcontentloaded", timeout=0)
                state = wait_for_ready_content()
                if state == "empty":
                    return {"relisted": relisted, "no_eligible": not relisted, "failed": failed}
                processed.clear()
                attempted_this_pass.clear()

            return {"relisted": relisted, "no_eligible": False, "failed": failed}
        except Exception as exc:
            return {"relisted": relisted, "no_eligible": False, "failed": failed,
                    "error": f"{type(exc).__name__}: {exc}"}

    def set_marketplace_location(self, location: str) -> bool:
        """Best-effort: change the Marketplace location picker value."""
        self._ensure()
        page = self._page
        assert page is not None
        try:
            page.goto(S.MARKETPLACE_URL, wait_until="domcontentloaded")
            human.human_pause(1500, 3000)
            human.human_click(page, S.to_playwright(page, S.MKT_LOCATION_BUTTON))
            human.human_pause(1000, 2000)
            loc = S.to_playwright(page, S.MKT_LOCATION_INPUT)
            loc.wait_for(timeout=8000)
            loc.fill("")
            self._type(loc, location)
            human.human_pause(1200, 2200)
            first = S.to_playwright(page, S.F_LOCATION_FIRST_RESULT)
            first.wait_for(timeout=8000)
            human.human_click(page, first)
            human.human_pause(1500, 2500)
            return True
        except Exception:
            return False

    def download_listing_assets(self, url: str, dest_dir: str) -> dict | None:
        """Scrape one listing and download its photos into dest_dir."""
        import urllib.request

        data = self.scrape_listing(url)
        if not data:
            return None
        dest = Path(dest_dir)
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        photo_paths: list[str] = []
        for i, photo_url in enumerate(data.get("photo_urls") or []):
            target = dest / f"dl_{i:03d}.jpg"
            try:
                req = urllib.request.Request(
                    photo_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as resp, \
                        open(target, "wb") as fh:
                    fh.write(resp.read())
                photo_paths.append(str(target))
            except Exception:
                continue
        return {"title": data.get("title", ""), "photo_paths": photo_paths}

    def get_take_action_listings(self) -> list[dict]:
        """Best-effort: find listings flagged with a 'take action' badge on
        the selling page, then extract their listing ids from link hrefs."""
        import re

        self._ensure()
        page = self._page
        assert page is not None
        flagged: list[dict] = []
        try:
            page.goto(S.MKT_YOUR_LISTINGS_URL, wait_until="domcontentloaded")
            human.human_pause(1500, 3000)
            badges = S.to_playwright(page, S.F_TAKE_ACTION_BADGE).all()
            for badge in badges:
                try:
                    card = badge.locator(
                        "xpath=ancestor::a[contains(@href,'/marketplace/item/')][1]")
                    href = card.first.get_attribute("href") or ""
                    m = re.search(r"/marketplace/item/(\d+)/", href)
                    if m:
                        flagged.append({
                            "id": m.group(1),
                            "title": (card.first.inner_text() or "").strip()[:120],
                        })
                except Exception:
                    continue
        except Exception:
            pass
        return flagged

    # -- Phase 2: human-like activities (best-effort) -------------------------------
    def _activity_loop(self, url: str, minutes: float,
                       scrolls_per_min: int = 6) -> None:
        """Visit a page and scroll human-likely for N minutes."""
        self._ensure()
        page = self._page
        assert page is not None
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception:
            return
        end = time.time() + max(0.0, minutes) * 60.0
        n = 0
        while time.time() < end:
            self._pause_checkpoint()
            n += 1
            human.human_scroll(page)
            if n % 3 == 0:  # occasionally pause longer, like reading
                human.human_pause(1500, 4000)
            time.sleep(max(1.0, 60.0 / max(1, scrolls_per_min)))

    def scroll_feed(self, minutes: float, probs: dict | None = None) -> None:
        self._activity_loop(S.HOME_URL, minutes)

    def watch_reels(self, minutes: float, probs: dict | None = None) -> None:
        self._activity_loop("https://www.facebook.com/reel/", minutes,
                            scrolls_per_min=3)

    def watch_videos(self, minutes: float, probs: dict | None = None) -> None:
        self._activity_loop("https://www.facebook.com/watch/", minutes,
                            scrolls_per_min=3)

    def scroll_marketplace(self, minutes: float, probs: dict | None = None) -> None:
        self._activity_loop(S.MARKETPLACE_URL, minutes)

    def read_notifications(self, seconds: float,
                           probs: dict | None = None) -> None:
        self._ensure()
        page = self._page
        assert page is not None
        try:
            page.goto("https://www.facebook.com/notifications",
                      wait_until="domcontentloaded")
        except Exception:
            return
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            self._pause_checkpoint()
            time.sleep(min(0.2, max(0.0, end - time.time())))

    def scroll_messages(self, seconds: float,
                        probs: dict | None = None) -> None:
        self._ensure()
        page = self._page
        assert page is not None
        try:
            page.goto("https://www.facebook.com/messages",
                      wait_until="domcontentloaded")
        except Exception:
            return
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            self._pause_checkpoint()
            human.human_scroll(page, 300)
            # Short sleeps keep Pause All responsive without interrupting scroll.
            for _ in range(10):
                self._pause_checkpoint()
                time.sleep(0.2)

    def goto_messages(self) -> None:
        self._ensure()
        page = self._page
        assert page is not None
        try:
            page.goto("https://www.facebook.com/messages",
                      wait_until="domcontentloaded")
            human.human_pause()
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._page = self._context = self._pw = None


# ------------------------------------------------------------- mock driver ---
class MockDriver(BaseDriver):
    """Test double: simulates posting with sleeps; records everything.

    Class-level tracking lets tests assert wave parallelism (max concurrent
    post_listing calls) and ordering.
    """

    calls: list[dict] = []
    _lock = threading.Lock()
    _active = 0
    max_concurrent = 0
    fail_labels: set[str] = set()   # account labels whose login should fail
    post_delay: float = 0.05        # simulated seconds per posted listing
    activity_scale: float = 1.0     # multiply activity durations (tests set ~0.001)
    fake_listings: list[dict] = []  # get_my_listings() returns this (per test)
    fake_scrapes: dict[str, dict] = {}  # scrape_listing(url) returns this
    fake_take_action: list[dict] = []  # get_take_action_listings() returns this

    def __init__(self, account: dict):
        super().__init__(account)

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls.calls = []
            cls._active = 0
            cls.max_concurrent = 0
            cls.fail_labels = set()
            cls.post_delay = 0.05
            cls.activity_scale = 1.0
            cls.fake_listings = []
            cls.fake_scrapes = {}
            cls.fake_take_action = []
            cls.fake_tabs = []
            cls.fake_draft_count = 2
            cls._draft_seq = 0
            cls._tab_seq = 0

    def _record(self, event: str, **kw) -> None:
        with self._lock:
            self.calls.append({"event": event, "label": self.account["label"],
                               "t": time.time(), **kw})

    def login(self) -> bool:
        ix = self.account.get("ixbrowser") or {"enabled": False}
        with self._lock:
            self.calls.append({"event": "login", "label": self.account["label"],
                               "t": time.time(),
                               "has_cookie": bool(self.account.get("cookie"))})
            # Which browser backend this account launched with.
            self.calls.append({
                "event": "launch",
                "label": self.account["label"],
                "backend": "ixbrowser" if ix.get("enabled") else "chrome",
                "ix_host": ix.get("host"), "ix_port": ix.get("port"),
                "ix_profile_id": ix.get("profile_id"),
                "t": time.time(),
            })
        time.sleep(0.02)
        return self.account["label"] not in self.fail_labels

    def type_text(self, text: str, cfg=None) -> str:
        """Test double for human.type_text: records the typing mode used."""
        mode = (cfg or {}).get("mode", "human") if isinstance(cfg, dict) \
            else getattr(cfg, "mode", "human")
        self._record("fill", mode=mode, text_len=len(text or ""))
        return mode

    def post_listing(self, payload: dict) -> str | None:
        cls = type(self)
        with cls._lock:
            cls._active += 1
            cls.max_concurrent = max(cls.max_concurrent, cls._active)
            cls.calls.append({"event": "post_start", "label": self.account["label"],
                              "title": payload.get("title"),
                              "description": payload.get("description"),
                              "hide_from_friends": bool(payload.get("hide_from_friends")),
                              "delivery_options": payload.get("delivery_options") or [],
                              "condition": payload.get("condition"),
                              "availability": payload.get("availability"),
                              "listing_type": payload.get("listing_type") or "item",
                              "category": payload.get("category"),
                              "typing_mode": (self.account.get("typing") or {}).get("mode", "human"),
                              "t": time.time()})
        try:
            time.sleep(cls.post_delay)
            return f"mock-{random.randint(1000, 9999)}"
        finally:
            with cls._lock:
                cls.calls.append({"event": "post_end", "label": self.account["label"],
                                  "t": time.time()})
                cls._active -= 1

    def close(self) -> None:
        if getattr(self, "keep_open", False):
            self._record("kept_open")
            return
        with self._lock:
            self.calls.append({"event": "close", "label": self.account["label"],
                               "t": time.time()})

    # -- Alignment (v6.1.2.0) simulated -------------------------------------------
    def delete_listing(self, listing_id: str) -> bool:
        self._record("delete_listing", listing_id=listing_id)
        time.sleep(0.02)
        return True

    def delete_drafts(self) -> int:
        n = getattr(type(self), "fake_draft_count", 2)
        self._record("delete_drafts", count=n)
        time.sleep(0.02)
        return n

    def delete_duplicate_listings(self) -> list[str]:
        import re

        def _norm(t: str) -> str:
            return re.sub(r"\s+", " ", (t or "").strip().lower())

        seen: dict[str, str] = {}
        dupes: list[str] = []
        for lst in type(self).fake_listings:
            key = _norm(lst.get("title", ""))
            if key and key in seen:
                dupes.append(lst["id"])
            elif key:
                seen[key] = lst["id"]
        for lid in dupes:
            self._record("delete_listing", listing_id=lid)
        self._record("delete_duplicates", count=len(dupes), ids=dupes)
        time.sleep(0.02)
        return dupes

    def renew_listing(self, listing_id: str) -> bool:
        self._record("renew_listing", listing_id=listing_id)
        time.sleep(type(self).post_delay)
        return True

    def renew_listings_from_page(self, max_count: int) -> dict:
        self._record("renew_listings_from_page", max_count=max_count)
        chosen = [dict(x) for x in type(self).fake_listings if x.get("id")][:max(0, int(max_count))]
        for item in chosen:
            self._record("renew_listing", listing_id=item["id"])
        return {"renewed": chosen, "no_eligible": not chosen, "failed": 0}

    def delete_relist_from_rows(self, max_count: int) -> dict:
        n = min(max(0, int(max_count)), getattr(type(self), "fake_renew_count", 3))
        return {"renewed": [{"id": str(i + 1), "title": f"Mock listing {i + 1}"} for i in range(n)], "no_eligible": n == 0, "failed": 0}

    def renew_listings_from_rows(self, max_count: int) -> dict:
        self._record("renew_listings_from_rows", max_count=max_count)
        chosen = [dict(x) for x in type(self).fake_listings if x.get("id")][:max(0, int(max_count))]
        for item in chosen:
            self._record("renew_listing_row_button", title=item.get("title", ""))
        # Deliberately omit listing IDs from this action's result.
        renewed = [{"title": item.get("title", "")} for item in chosen]
        return {"renewed": renewed, "no_eligible": not renewed, "failed": 0}

    def set_marketplace_location(self, location: str) -> bool:
        self._record("set_location", location=location)
        time.sleep(0.02)
        return True

    def download_listing_assets(self, url: str, dest_dir: str) -> dict | None:
        data = type(self).fake_scrapes.get(url)
        self._record("download_assets", url=url, found=data is not None)
        if not data:
            return None
        return {"title": data.get("title", ""),
                "photo_paths": [f"{dest_dir}/dl_{i:03d}.jpg"
                                for i in range(len(data.get("photo_urls", [])))]}

    def open_marketplace(self) -> None:
        self._record("open_marketplace", outcome="completed")

    def download_open_tabs(self, dest_dir: str) -> list[dict]:
        tabs = [dict(t) for t in type(self).fake_tabs]
        self._record("download_open_tabs", count=len(tabs), dest=dest_dir)
        return [{"title": t.get("title", ""),
                 "photo_paths": [f"{dest_dir}/tab{i:02d}_{j:03d}.jpg"
                                 for j in range(len(t.get("photo_urls", [])))]}
                for i, t in enumerate(tabs)]

    def get_take_action_listings(self) -> list[dict]:
        flagged = [dict(x) for x in type(self).fake_take_action]
        self._record("get_take_action", count=len(flagged))
        return flagged

    # -- Phase 2 simulated -------------------------------------------------------
    def create_draft(self, payload: dict) -> str | None:
        cls = type(self)
        with cls._lock:
            cls._draft_seq += 1
            did = f"mock-draft-{cls._draft_seq}"
            cls.calls.append({"event": "create_draft", "label": self.account["label"],
                              "t": time.time(), "title": payload.get("title"),
                              "draft_id": did})
        time.sleep(cls.post_delay)
        return did

    def publish_draft(self, draft_id: str) -> bool:
        self._record("publish_draft", draft_id=draft_id)
        time.sleep(0.02)
        return True

    def reset_list_publish(self) -> None:
        return None

    def publish_next_draft_from_list(self) -> dict:
        self._record("publish_next_draft_from_list")
        return {"ok": False, "label": None, "reason": None, "empty": True}

    def fill_listing_form(self, payload: dict) -> str | None:
        cls = type(self)
        with cls._lock:
            cls._tab_seq += 1
            handle = f"mock-tab-{cls._tab_seq}"
            cls.calls.append({"event": "fill_form", "label": self.account["label"],
                              "t": time.time(), "title": payload.get("title"),
                              "tab": handle})
        time.sleep(cls.post_delay)
        return handle

    def get_my_listings(self) -> list[dict]:
        self._record("get_my_listings", count=len(type(self).fake_listings))
        return [dict(x) for x in type(self).fake_listings]

    def edit_listing(self, listing_id: str, payload: dict) -> bool:
        self._record("edit_listing", listing_id=listing_id,
                     title=payload.get("title"))
        time.sleep(type(self).post_delay)
        return True

    def scrape_listing(self, url: str) -> dict | None:
        data = type(self).fake_scrapes.get(url)
        self._record("scrape_listing", url=url, found=data is not None)
        return dict(data) if data else None

    def _activity(self, name: str, minutes: float,
                  probs: dict | None = None) -> None:
        self._record(f"activity:{name}", minutes=minutes, probs=probs or {},
                     outcome="completed")
        time.sleep(max(0.0, minutes) * 60.0 * type(self).activity_scale)

    def scroll_feed(self, minutes: float, probs: dict | None = None) -> None:
        self._activity("scroll_feed", minutes, probs=probs)

    def watch_reels(self, minutes: float, probs: dict | None = None) -> None:
        self._activity("watch_reels", minutes, probs=probs)

    def watch_videos(self, minutes: float, probs: dict | None = None) -> None:
        self._activity("watch_videos", minutes, probs=probs)

    def scroll_marketplace(self, minutes: float,
                           probs: dict | None = None) -> None:
        self._activity("scroll_marketplace", minutes, probs=probs)

    def read_notifications(self, seconds: float,
                           probs: dict | None = None) -> None:
        self._record("activity:read_notifications", seconds=seconds,
                     probs=probs or {}, outcome="completed")
        time.sleep(max(0.0, seconds) * type(self).activity_scale)

    def scroll_messages(self, seconds: float,
                        probs: dict | None = None) -> None:
        self._record("activity:scroll_messages", seconds=seconds,
                     probs=probs or {}, outcome="completed")
        time.sleep(max(0.0, seconds) * type(self).activity_scale)

    def goto_messages(self) -> None:
        self._record("activity:goto_messages", outcome="completed")

    def launch_fast(self) -> None:
        self._record("launch_fast", outcome="completed")


# class-level counters (assigned after class creation to keep reset() simple)
MockDriver._draft_seq = 0
MockDriver._tab_seq = 0
MockDriver.fake_draft_count = 2


def get_driver(name: str):
    return {"mock": MockDriver, "playwright": PlaywrightDriver}[name]
