# FMS — Full Feature Set Design
*(Tool name set by user, 2026-10-15. Personal testing build: no subscription, no license server, no approval flow.)*

Designed 2026-10-15 from the FBM Robo feature analysis (`FBM_ROBO_FEATURE_LIST.md`).
This is an **original design** inspired by the observed workflow — not a copy of any existing software.

---

## 1. Vision

A Windows desktop tool that automates Facebook Marketplace listing work for the user's own
accounts: manage many accounts safely, prepare product data once, post listings on schedule
with human-like behavior, and handle buyer messages — while minimizing automation detection.

**Out of scope (per user decision):** subscription system, license keys, manual approval,
payments, multi-user/team server. Everything runs locally on the user's laptop.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    DASHBOARD (local web UI)              │
│  Accounts │ Products │ Scripts │ Runs │ Messages │ Logs  │
└────────────────────────┬────────────────────────────────┘
                         │  FastAPI (local server)
┌────────────────────────▼────────────────────────────────┐
│                      CORE SERVICES                       │
│  Account Mgr │ Proxy Mgr │ Product Mgr │ Listing Engine  │
│  Script Engine │ Human-Behavior Layer │ Scrap │ Messenger │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│              BROWSER AUTOMATION (Playwright)              │
│  Persistent context per account (isolated profile)       │
│  Per-account proxy │ Human-like typing/mouse │ Fingerprint│
└────────────────────────┬────────────────────────────────┘
                         │
                    Facebook Marketplace
┌─────────────────────────────────────────────────────────┐
│              LOCAL DATA (SQLite + folders)               │
│  accounts, groups, products, titles, images, locations,  │
│  scripts, runs, listings, messages, logs                │
└─────────────────────────────────────────────────────────┘
```

**Why this shape:** the UI, the automation, and the data are separated, so any module
(typing engine, a posting mode, the messenger) can be improved or replaced without
rewriting the rest.

---

## 3. Tech Stack (recommended)

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Readable, huge automation ecosystem, runs on Windows |
| Browser automation | Playwright (Python) | Modern, persistent per-account browser contexts, good control over typing/mouse/timing; auto-downloads its browser |
| Dashboard | FastAPI + simple HTML/JS (local only) | No install friction; opens in the user's own browser at `localhost` |
| Database | SQLite | Zero-setup, file-based, enough for personal use |
| 2FA codes | `pyotp` (TOTP) | Derives 6-digit codes from stored secrets — no external tool needed |
| Fingerprinting | Playwright context options + optional anti-detect browser API later | Per-account user-agent, locale, timezone, viewport; WebRTC control |
| Packaging | PyInstaller (later) | Turns it into a double-click `.exe` when testing is done |

**Assumption:** Windows 10/11 laptop, Chrome/Chromium available via Playwright.

---

## 4. Modules

### 4.1 Account Manager
- **Groups:** create/rename/delete groups; each group = set of accounts + assigned products.
- **Add accounts:** single form (email, password, browser profile, proxy, 2FA secret, cookie),
  bulk paste (one per line), or import from file.
- **Login (user-corrected 2026-10-15):** primary login is **email/phone number + password**
  (Gmail, phone number, ya email — jo account par ho). **Cookie login OPTIONAL** hai —
  lazmi nahi. **2FA bhi OPTIONAL** hai — sirf un accounts ke liye jahan laga ho; TOTP secret
  store karne par one-click/automatic code generation via `pyotp`.
- **Browser (user-corrected 2026-10-15):** default **Chrome** hi use hota hai; browser
  selection OPTIONAL hai — zaroorat ho to override kar sakte ho.
- **Activate/deactivate** accounts; edit with an unsaved-changes guard; bulk remove.
- **Per-account status:** Active / Inactive / Banned / Locked with reason and timestamp.

### 4.2 Proxy Manager
- **Proxies OPTIONAL hain (user-corrected 2026-10-15)** — lazmi nahi. Jahan lagao, wahan
  **per-account** proxy lagta hai (IP:port + username/password), account ke saath stored.
- **Validation on add:** test the proxy before saving; re-verify on load.
- **Health tools:** Check Status (all), Remove Not Working, Remove All; pre-run check —
  if a proxy is dead the run logs the reason instead of failing silently.
- **Round-robin assignment** when pasting a proxy list onto many accounts
  (N proxies / M accounts → even distribution).
- **Country discipline:** warn if proxy country ≠ listing country.

### 4.3 Product Manager
- **Product editor:** name, base price, tags, category (+ category-specific attributes),
  title list (one per line → rotated so every ad gets a different title),
  images folder, locations list, optional video.
- **Categories:** hierarchy like Facebook's (Furniture → Beds & bed frames); attribute sets
  per category (beds: color + size; wardrobes: style + finish; …); extensible — new attributes
  addable without code changes. Fallback: if an account only shows "flat" categories,
  automatically fall back to the parent category.
- **Locations:** one per line with country prefix ("UK" before city). **Verification helper:**
  checks each location against Facebook's own suggestion and flags names that won't resolve
  (the #1 silent failure in auto-listing).
- **Image hygiene:** "don't reuse an image within N days" setting + duplicate-image scan
  recommendation before assigning a folder.

### 4.4 Listing Engine (the core)
- **Posting modes:**
  - *Complete Posting* — fills the whole form automatically.
  - *Draft & Publish* — creates drafts first, then publishes: **all at once** or
    **one-by-one with a configurable gap** (for limit-constrained accounts).
  - *Manual-publish helper* — fills forms across open tabs; the user clicks Publish
    per listing (maximum human signal).
  - *Relist/Edit* — rewrites existing (sold/out-of-stock) listings instead of creating
    new ones, for accounts under new-listing limits.
  - *Renew / Delete* — scheduled renew and cleanup of old listings.
- **Run settings — batch size as WAVES (user-corrected 2026-10-15, exact semantics):**
  Batch size = N ka matlab hai **N Chrome windows ek saath khulti hain, har window par
  ek alag ID**. Har Chrome apni ID par **poori listing complete karta hai** — jitni listings
  hum ne us ID ke liye kahi hon (5, 10, 20, 100…). Jab **poora batch complete** ho jaye, tab
  **agli wave** shuru hoti hai: phir se N Chrome windows, har ek par **nayi different ID**.
  Example: batch size 1 → ek waqt me 1 Chrome, 1 ID par saari listings, phir next ID.
  Batch size 2 → 2 Chrome ek saath, dono par same time par listing, complete hone par
  next 2 different IDs. (Hardware ke hisaab se 1–10 tak.)
- Min/max ads per account (e.g. 45–65), min/max price (random per ad), min/max delay between posts, tabs-to-load-parallel,
  per-turn ad limits with account rotation, image-reuse window, shuffle order,
  **Schedule At** (future start), speed multiplier.
- **Data generation prompt:** "Create new data?" — Yes = fresh run; No = resume exactly
  where the last run stopped (survives laptop shutdowns).
- **Live run board:** Pending → In Progress → Done (+ Failed / Inactive with reasons);
  per-account remaining count; Pause/Resume; close a struggling account mid-run.
- **Rotation engine:** titles, prices, locations, images and descriptions are combined
  per ad so no two listings look identical; optional "split locations across accounts"
  (each account gets a different slice).
- **Response proof:** after runs, shows messages/clicks received per listing.

### 4.5 Scrap Module (competitor-data reuse)
- Point the tool at high-performing Marketplace listings; a side panel lets the user
  **select listings** (toggle select/browse mode); every selected listing gets a category.
- Extracts title, price, description, photos, location, availability.
- **Overrides:** global title/location/description/price lists replace scraped values;
  per-listing customize; "always edit description" popup (swap in your own contact,
  strip the original seller's); 5-minute timeout with skip; fallback description when
  the source has none; country-append and backup-location rules.
- Priority: customized → global → original. Scraped sets become products, then run
  through the normal Listing Engine.

### 4.6 Script Engine (behavior composer)
- **Named scripts** built from action blocks: Post (N listings) → Scroll (M min) →
  Create drafts → Watch videos/reels → Publish drafts → Wait for next turn …
- **Data binding:** each action binds its own data set (e.g. "Luxury Bags" for posts,
  "Wardrobe Set" for drafts).
- **Wait For Next Turn:** after its batch, an account yields to the next account in the
  queue; configurable minimum gap (0 = yield only if someone waits; N minutes = guaranteed rest).
- **Activity blocks** with human-like probabilities (e.g. feed scroll: 55% keep scrolling,
  15% like a stopped-on post; reels: 40% skip, 45% like) — each account's session looks different.
- **Grouping:** run "K of N" actions per account (e.g. 1 of 3 → accounts diverge);
  **shuffle** toggle randomizes block order.
- **Recovery:** interrupted runs offer "continue where you left off" (skips completed steps)
  or "start fresh".

### 4.7 Human-Behavior Layer (anti-detection core)
- **Typing modes** (fallback chain): ① Human-like typing (default) — character-by-character
  with 30–80 ms jitter, thinking pauses every 20–50 chars, extra pause after punctuation
  and on newlines; long-text strategies (type-all / paste-only / hybrid).
  ② Fast JS fill. ③ OS clipboard paste (last resort, with warning).
- **Mouse:** human-like movement curves and click timing (no instant jumps).
- **Fingerprints:** per-account browser context (user-agent, locale, timezone, viewport,
  WebRTC handling) so accounts aren't linked.
- **Optional anti-detect browser:** integration point for an external anti-detect browser's
  local API (same pattern as ixBrowser: host/port in settings, per-account profile IDs,
  auto-create profiles). Proxies stay in our tool, never in the external browser.
- **Behavior training (later):** record the user's own manual sessions (click coordinates
  on FB UI elements) and replay the rhythm during automation.

### 4.8 Messenger Module — ⏸️ DEFERRED (user decision 2026-10-15)
- NOT building now. The user hasn't built their messenger yet; it will be built separately later.
- When ready, its API key will be added into FMS (integration point kept in Settings).
- (Original spec preserved below for later.)
- **Unified inbox:** connects each launched account's Facebook session; all Marketplace
  chats in one view with per-chat account label, buyer profile, and the listing context.
- **Auto-reply ("Default Message"):** per-account or global first-response templates
  (e.g. "Yes available — contact on WhatsApp …"), sent on the first inbound message.
- **Reply-safety options:** voice-message-first mode; mark-as-sold + delete listing before
  replying (user-confirmed tricks, no guarantees).
- **Team-safe:** messages can't be deleted from the inbox (oversight), when teams are added later.
- **Requirement:** an account's browser session must be open for its messages to flow.

### 4.9 Verification Helper
- **Guided phone verification:** drives each account to Marketplace's identity/phone prompt,
  holds at the code step; the user pastes the OTP (from any OTP provider); **Submit All**
  verifies the whole batch at once. Batch size adjustable mid-run; queue auto-advances.
- **Troubleshooting built in:** retry patterns for "something's wrong" (retype digits by hand),
  resend-code waits, skip logic for ID-card-asking accounts.
- Country-must-match enforcement (number country = listing country).

### 4.10 Dashboard, Logs & Analytics
- Run board, product/account/group CRUD, script builder (visual block list), inbox,
  settings (typing, delays, fingerprints, browser API).
- **Logs:** per-account, per-run, expandable, copyable; failure reasons in plain language.
- **Analytics (later):** listings/day per account, messages/clicks per listing, ban/suspension
  events — the feedback loop for tuning safety settings.

---

## 5. Data Model (SQLite)

- `groups` (id, name)
- `accounts` (id, group_id, email, password_enc, browser, proxy_id, twofa_secret, cookie,
  active, status, status_reason, created_at)
- `proxies` (id, account_id, host, port, username, password_enc, country, last_check, working)
- `products` (id, name, base_price, tags, category, attributes_json, title_file, image_folder,
  video_path, created_at)
- `titles` / `locations` (product_id, value, sort order) — or read from files; TBD
- `scripts` (id, name, blocks_json, created_at)
- `runs` (id, group_id, script_id, mode, settings_json, status, started_at, finished_at)
- `listings` (id, run_id, account_id, product_id, title_used, price_used, location_used,
  images_used, status, fb_listing_id, posted_at)
- `messages` (id, account_id, listing_id, sender, body, direction, received_at)
- `logs` (id, run_id, account_id, level, message, created_at)

Passwords, cookies, proxy credentials and 2FA secrets are **encrypted at rest**
(local key; never leave the laptop).

---

## 6. Key Workflows

**A. First-time setup:** add group → add accounts (bulk/cookie) → assign proxies (validate) →
verify proxies → warm-up checklist → create products (or scrap) → verify locations →
build script → dry-run on 1 account → scale up.

**B. Daily listing run:** select group + script → run settings (batch, ads/account, delays) →
"new data or resume?" → live board → pause/resume → done → response proof.

**C. Limit-constrained accounts:** Draft & Publish (all-together or gapped one-by-one) or
Relist/Edit existing listings.

**D. New phone-verification batch:** select accounts → Verification Helper → OTP pastes →
Submit All → statuses recorded.

---

## 7. Safety & Honesty Notes

- Facebook's terms discourage automation; **account bans are a real, accepted risk** of this
  entire category. The design minimizes detection signals but **cannot guarantee** any account's safety.
- Built-in discipline: ramping (start ~100 ads/account, never blast), warm-ups, country alignment,
  human-like timing, per-account isolation, pause-on-anomaly.
- The tool is for the **user's own accounts and listings only**. Scraped content must be
  rewritten (titles/descriptions in your own words, your own contact) — never repost someone
  else's photos or text verbatim.
- No credential ever leaves the laptop; no central server; encrypted local storage.

---

## 8. Build Phases (recommended order)

- **Phase 1 — Core:** Account Manager, Proxy Manager, Product Manager, Listing Engine
  (Complete Posting + basic Draft & Publish), human-like typing, dashboard, logs.
- **Phase 2 — Behavior:** Script Engine, activity blocks + probabilities, Wait For Next Turn,
  mouse behavior, fingerprints, Relist/Edit, Scrap module, resume/recovery.
- **Phase 3 — Comms & polish:** Messenger inbox + auto-reply, Verification Helper,
  analytics, PyInstaller `.exe` packaging.

Each phase ends with a **test gate** on 1–2 of the user's own accounts before scaling.

---

## 9. Open Questions for the user

1. ~~Tool name?~~ — Decided: **FMS** (2026-10-15).
2. Windows-only is fine, or also needed elsewhere?
3. For Phase 1 testing: how many Facebook accounts and roughly how many listings/day?
