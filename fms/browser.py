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
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

# Screen-friendly sizes: the browser WINDOW is sized to the viewport so it
# never opens bigger than the laptop screen.
VIEWPORTS = [(1280, 720), (1366, 768), (1440, 900)]
LOCALES = ["en-GB", "en-US"]
TIMEZONES = ["Europe/London", "America/New_York"]


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
        # randomization off (fixed default fingerprint instead).
        if self.account.get("apply_fingerprint", True):
            rng = random.Random(self.account["id"])
            ua = rng.choice(USER_AGENTS)
            loc = rng.choice(LOCALES)
            tz = rng.choice(TIMEZONES)
        else:
            ua, loc, tz = USER_AGENTS[0], "en-US", "America/New_York"
        # v0.2.23: fixed large window for every account (the random small
        # viewports looked broken). Slight cascade so parallel account
        # browsers don't stack pixel-exact on top of each other.
        vw, vh = 1440, 900
        cascade = (self.account.get("id") or 0) % 5
        self._context = self._pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=self.headless,
            proxy=proxy,
            user_agent=ua,
            locale=loc,
            timezone_id=tz,
            viewport={"width": vw, "height": vh},
            args=["--disable-blink-features=AutomationControlled",
                  f"--window-size={vw},{vh}",
                  f"--window-position={40 * cascade},{30 * cascade}"],
        )
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()

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
        """Try each login selector in order; return the first visible locator."""
        last_exc = None
        for sel in fallbacks:
            try:
                loc = S.to_playwright(page, sel)
                loc.wait_for(state="visible", timeout=timeout_each)
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
        try:
            email = self._find_login_field(page, S.LOGIN_EMAIL_FALLBACKS)
            self._type(email, label)
            human.human_pause(200, 500)
            pass_field = self._find_login_field(page, S.LOGIN_PASS_FALLBACKS)
            self._type(pass_field, password)
            human.human_pause(300, 800)
            # Submit: click the Log in button; if we are still on the login
            # page afterwards, press Enter in the password field as fallback.
            try:
                human.human_click(
                    page, self._find_login_field(page, S.LOGIN_BUTTON_FALLBACKS))
            except Exception:
                pass
            human.human_pause(2500, 4000)
            if "login" in (page.url or "").lower() and not self._is_logged_in():
                try:
                    pass_field.press("Enter")
                except Exception:
                    pass
                human.human_pause(2500, 4000)
        except Exception as exc:
            return fail(f"could not fill login form: {type(exc).__name__}")

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
        loc = S.to_playwright(page, S.F_LOCATION)
        try:
            loc.wait_for(timeout=10000)
        except Exception:
            return
        self._type(loc, location)
        human.human_pause(1200, 2200)
        try:
            first = S.to_playwright(page, S.F_LOCATION_FIRST_RESULT)
            first.wait_for(timeout=8000)
            human.human_click(page, first)
            human.human_pause()
        except Exception:
            pass  # leave typed text; Facebook may still accept it

    def _fill_form(self, page, payload: dict) -> bool:
        """Fill the whole create-item form. Shared by post/draft/manual/edit."""
        try:
            page.goto(S.MKT_CREATE_ITEM_URL, wait_until="domcontentloaded")
            human.human_pause(1500, 3000)

            title = S.to_playwright(page, S.F_TITLE)
            title.wait_for(timeout=20000)
            self._type(title, payload.get("title", ""), hybrid_ok=True)
            human.human_pause(300, 700)

            price = S.to_playwright(page, S.F_PRICE)
            self._type(price, str(payload.get("price", "")))
            human.human_pause(300, 700)

            # Category: type and pick the first suggestion; fall back gracefully.
            category = payload.get("category")
            if category:
                try:
                    cat = S.to_playwright(page, S.F_CATEGORY)
                    self._type(cat, category)
                    human.human_pause(1200, 2200)
                    first = S.to_playwright(page, S.F_LOCATION_FIRST_RESULT)
                    first.wait_for(timeout=6000)
                    human.human_click(page, first)
                except Exception:
                    pass
                human.human_pause()

            # Condition
            condition = (payload.get("condition") or "New").lower()
            try:
                sel = S.F_CONDITION_NEW if "new" in condition else S.F_CONDITION_USED_LIKE_NEW
                human.human_click(page, S.to_playwright(page, sel))
                human.human_pause()
            except Exception:
                pass

            # Description
            desc = payload.get("description") or ""
            if desc:
                self._type(S.to_playwright(page, S.F_DESCRIPTION), desc,
                           hybrid_ok=True)
                human.human_pause()

            # Location
            if payload.get("location"):
                self._select_location(page, payload["location"])

            # Hide From Friends (matches the real tool's setting; best-effort)
            if payload.get("hide_from_friends"):
                try:
                    human.human_click(page, S.to_playwright(page, S.F_HIDE_FROM_FRIENDS))
                    human.human_pause()
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
                    human.human_pause()
                except Exception:
                    pass

            # Photos
            images = payload.get("image_paths") or []
            if images:
                try:
                    file_input = S.to_playwright(page, S.F_ADD_PHOTOS)
                    file_input.wait_for(timeout=8000)
                    # Playwright file inputs accept a list of paths.
                    page.set_input_files(S.F_ADD_PHOTOS[1], [str(p) for p in images[:10]])
                    human.human_pause(2500, 5000)  # let uploads finish
                except Exception:
                    pass

            # Optional video
            if payload.get("video_path"):
                try:
                    page.set_input_files(S.F_ADD_VIDEO[1], str(payload["video_path"]))
                    human.human_pause(2500, 5000)
                except Exception:
                    pass
            return True
        except Exception:
            return False

    def post_listing(self, payload: dict) -> str | None:
        self._ensure()
        page = self._page
        assert page is not None
        try:
            if not self._fill_form(page, payload):
                return None
            # Publish
            human.human_click(page, S.to_playwright(page, S.F_PUBLISH_BUTTON))
            human.human_pause(3000, 6000)

            # Try to capture the new listing id from the URL.
            url = page.url
            listing_id = None
            if "/marketplace/item/" in url:
                listing_id = url.rstrip("/").split("/")[-1].split("?")[0]
            return listing_id or "posted"
        except Exception:
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
