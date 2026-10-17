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
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from . import config, human, selectors as S

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
        # v0.2.49: set by the engine — called the instant the browser window
        # is closed so the account flips to Failed immediately.
        self._on_close_callback = None

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

    def _launch(self) -> None:
        # IXBrowser (anti-detect, optional): route through its Local API when
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
        # v0.2.49: same close watcher as the main launch path.
        try:
            self._context.on("close", self._handle_context_close)
        except Exception:
            pass

    def _ensure(self) -> None:
        if self._page is None:
            self._launch()

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
            human.human_pause(200, 500)
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
    def _select_location(self, page, location: str) -> None:
        # v0.2.53: log failures instead of silent return.
        loc = S.to_playwright(page, S.F_LOCATION)
        try:
            loc.wait_for(timeout=10000)
        except Exception as exc_l:
            self._last_form_error = (
                f"Location field not found: {type(exc_l).__name__}")
            return
        self._type(loc, location)
        human.human_pause(800, 1500)
        try:
            first = S.to_playwright(page, S.F_LOCATION_FIRST_RESULT)
            first.wait_for(timeout=8000)
            human.human_click(page, first)
            human.human_pause(200, 500)
        except Exception:
            pass  # leave typed text; Facebook may still accept it

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
            # 5. The Title field = the form is ready.
            def _wait_title():
                try:
                    S.to_playwright(page, S.F_TITLE).wait_for(timeout=20000)
                except Exception:
                    # v0.2.48 diagnostic: dump what inputs ARE on the page so
                    # the logs reveal Facebook's real markup (no more guessing).
                    # v0.2.51: more defensive — also capture URL.
                    try:
                        url_now = page.url
                    except Exception:
                        url_now = "?"
                    try:
                        dump = page.evaluate(
                            """() => Array.from(document.querySelectorAll(
                                'input, textarea')).slice(0, 25).map(el => [
                                el.tagName,
                                el.getAttribute('type') || '',
                                el.getAttribute('aria-label') || '',
                                el.getAttribute('placeholder') || '',
                                el.id || ''].join('|')).join(' ;; ')}""")
                        if not dump:
                            dump = "(no inputs found)"
                    except Exception as exc_e:
                        dump = f"(evaluate failed: {type(exc_e).__name__})"
                    raise TimeoutError(
                        f"Title not found @ {url_now[:80]}. "
                        f"Page inputs: {dump[:600]}")
            err = step("Title field wait", _wait_title)
        if not err:
            return True, "click-through flow OK"
        detail = f"click-through failed at [{err}]"
        # Fallback: direct URL (previous behavior).
        try:
            page.goto(S.MKT_CREATE_ITEM_URL, wait_until="domcontentloaded")
            human.human_pause(1500, 3000)
            try:
                S.to_playwright(page, S.F_TITLE).wait_for(timeout=20000)
            except Exception:
                try:
                    dump = page.evaluate(
                        """() => Array.from(document.querySelectorAll(
                            'input, textarea')).slice(0, 25).map(el => [
                            el.tagName,
                            el.getAttribute('type') || '',
                            el.getAttribute('aria-label') || '',
                            el.getAttribute('placeholder') || '',
                            el.id || ''].join('|')).join(' ;; ')}""")
                except Exception:
                    dump = "?"
                raise TimeoutError(f"Title not found. Page inputs: {dump[:600]}")
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
        try:
            ok, detail = self._open_create_item_form(page)
            if not ok:
                self._last_form_error = detail
                return False

            # 1. Expand "More details" first.
            try:
                md = S.to_playwright(page, S.MORE_DETAILS)
                md.wait_for(timeout=6000)
                human.human_click(page, md)
                human.human_pause(400, 800)
            except Exception:
                pass  # already expanded or absent

            # 2. Photos FIRST (before any text).
            images = payload.get("image_paths") or []
            if images:
                try:
                    file_input = S.to_playwright(page, S.F_ADD_PHOTOS)
                    file_input.wait_for(timeout=8000)
                    # Playwright file inputs accept a list of paths.
                    page.set_input_files(S.F_ADD_PHOTOS[1], [str(p) for p in images[:10]])
                    human.human_pause(2000, 3500)  # let uploads finish
                except Exception:
                    pass

            # Optional video
            if payload.get("video_path"):
                try:
                    page.set_input_files(S.F_ADD_VIDEO[1], str(payload["video_path"]))
                    human.human_pause(2000, 3500)
                except Exception:
                    pass

            # 3. Title, 4. Price.
            title = S.to_playwright(page, S.F_TITLE)
            # (already waited visible inside _open_create_item_form)
            self._type(title, payload.get("title", ""), hybrid_ok=True)
            human.human_pause(200, 500)

            price = S.to_playwright(page, S.F_PRICE)
            self._type(price, str(payload.get("price", "")))
            human.human_pause(200, 500)

            # Category: CLICK the field, then SELECT from the dropdown list.
            # v0.2.54: user confirmed these are lists, not type-in fields.
            # The old code kept trying to TYPE, which never selected anything.
            category = payload.get("category")
            if category:
                try:
                    cat = S.to_playwright(page, S.F_CATEGORY)
                    cat.wait_for(timeout=8000)
                    human.human_click(page, cat)
                    human.human_pause(800, 1500)
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
                    human.human_pause(200, 500)
                except Exception as exc_c:
                    self._last_form_error = (
                        f"Category '{category}' not selected: "
                        f"{type(exc_c).__name__}")
                    # Don't return False yet — try the rest, Next check
                    # will catch an incomplete form.

            # Condition: CLICK the field, then SELECT from the list.
            # v0.2.54: same list-select approach as category.
            condition = (payload.get("condition") or "New").strip()
            if condition:
                try:
                    # Open the condition dropdown.
                    cond_field = S.to_playwright(page, S.F_CONDITION_FIELD)
                    cond_field.wait_for(timeout=8000)
                    human.human_click(page, cond_field)
                    human.human_pause(800, 1500)
                    # Click the matching option in the list.
                    opt = page.locator(
                        "[role='listbox'] [role='option'], "
                        "[role='menu'] [role='menuitem']").filter(
                        has_text=condition).first
                    try:
                        opt.wait_for(timeout=6000)
                    except Exception:
                        opt = page.get_by_text(condition, exact=False).first
                        opt.wait_for(timeout=6000)
                    human.human_click(page, opt)
                    human.human_pause(200, 500)
                except Exception as exc_co:
                    self._last_form_error = (
                        f"Condition '{condition}' not selected: "
                        f"{type(exc_co).__name__}")

            # Description
            desc = payload.get("description") or ""
            if desc:
                self._type(S.to_playwright(page, S.F_DESCRIPTION), desc,
                           hybrid_ok=True)
                human.human_pause(200, 500)

            # Location
            if payload.get("location"):
                self._select_location(page, payload["location"])

            # Hide From Friends (matches the real tool's setting; best-effort)
            if payload.get("hide_from_friends"):
                try:
                    human.human_click(page, S.to_playwright(page, S.F_HIDE_FROM_FRIENDS))
                    human.human_pause(200, 500)
                except Exception:
                    pass

            # Delivery options (real tool's product dialog checkboxes; best-effort)
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
                    human.human_click(page, S.to_playwright(page, _sel))
                    human.human_pause(200, 500)
                except Exception:
                    pass

            # (Photos/video already attached at the top — v0.2.50 order.)
            return True
        except Exception as exc:
            # v0.2.29: keep the real failure reason + page so the run logs
            # say WHY the form never completed (instead of a silent False).
            try:
                url = page.url
            except Exception:
                url = "?"
            self._last_form_error = (f"{type(exc).__name__}: "
                                     f"{str(exc)[:180]} @ {url}")
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
        self._ensure()
        page = self._page
        assert page is not None
        self._last_error = None  # v0.2.29: surfaced in the run logs on failure
        try:
            if not self._fill_form(page, payload):
                self._last_error = (getattr(self, "_last_form_error", None)
                                    or "form fill failed")
                return None
            # Publish: form -> Next -> Publish (two-step, v0.2.51).
            # The Publish button only appears AFTER clicking Next; the old
            # code skipped Next entirely and timed out waiting for Publish.
            # v0.2.53: wait for Next to be ENABLED (not just visible). If the
            # form is incomplete (category/condition missing), Next stays
            # disabled — clicking a disabled button does nothing.
            try:
                nxt = S.to_playwright(page, S.F_NEXT_BUTTON)
                nxt.wait_for(timeout=15000)
                # Wait until it's actually clickable (enabled).
                try:
                    nxt.wait_for(state="enabled", timeout=10000)
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
                    raise RuntimeError(
                        "Next is disabled (form incomplete). " +
                        ("Errors: " + missing[0] if missing else
                         "Check category/condition/location."))
                human.human_click(page, nxt)
                human.human_pause(2000, 3500)
            except Exception as exc_n:
                self._last_error = (f"Next button: "
                                    f"{str(exc_n)[:200]} @ {page.url[:80]}")
                return None
            try:
                pub = S.to_playwright(page, S.F_PUBLISH_BUTTON)
                pub.wait_for(timeout=15000)
                human.human_click(page, pub)
            except Exception as exc_p:
                self._last_error = (f"Publish button not found/clickable: "
                                    f"{type(exc_p).__name__} @ {page.url[:80]}")
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
    def create_draft(self, payload: dict) -> str | None:
        """Fill the form and SAVE AS DRAFT (tab closes as each draft completes)."""
        self._ensure()
        page = self._page
        assert page is not None
        try:
            if not self._fill_form(page, payload):
                return None
            human.human_click(page, S.to_playwright(page, S.F_SAVE_DRAFT_BUTTON))
            human.human_pause(2000, 4000)
            url = page.url
            draft_id = None
            if "listing_id=" in url:
                import re
                m = re.search(r"listing_id=(\d+)", url)
                draft_id = m.group(1) if m else None
            return draft_id or "draft"
        except Exception:
            return None

    def publish_draft(self, draft_id: str) -> bool:
        """Open the drafts page and publish one draft. Best-effort."""
        self._ensure()
        page = self._page
        assert page is not None
        try:
            page.goto(S.MKT_DRAFTS_URL, wait_until="domcontentloaded")
            human.human_pause(1500, 3000)
            human.human_click(page, S.to_playwright(page, S.F_PUBLISH_DRAFT_BUTTON))
            human.human_pause(2500, 5000)
            return True
        except Exception:
            return False

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
            page.goto(f"https://www.facebook.com/marketplace/item/{listing_id}/",
                      wait_until="domcontentloaded")
            human.human_pause(1500, 3000)
            human.human_click(page, S.to_playwright(page, S.F_DELETE_LISTING_BUTTON))
            human.human_pause(1000, 2000)
            human.human_click(page, S.to_playwright(page, S.F_CONFIRM_DELETE_BUTTON))
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
            page.goto(f"https://www.facebook.com/marketplace/item/{listing_id}/",
                      wait_until="domcontentloaded")
            human.human_pause(1500, 3000)
            human.human_click(page, S.to_playwright(page, S.F_RENEW_BUTTON))
            human.human_pause(2500, 4000)
            return True
        except Exception:
            return False

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
        time.sleep(max(0.0, seconds))

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
            human.human_scroll(page, 300)
            time.sleep(2)

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
