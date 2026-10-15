# FMS — Facebook Marketplace Auto-Lister

Personal testing tool. Everything runs on your own laptop; nothing is uploaded anywhere.

---

## ▶ Just use it (no technical knowledge needed)

1. **Download `FMS.exe`** (from the link you were given).
2. **Double-click `FMS.exe`.**
3. Your browser opens the FMS dashboard automatically (`http://127.0.0.1:8000`).
4. The first start takes a little longer (it unpacks itself once) — afterwards it's quick.
5. To quit: click **Quit FMS** in the bottom of the dashboard.

That's it. No Python, no installs, no commands.

### Your first listing run (5 minutes)

1. **Accounts** tab → create a group (e.g. "My IDs") → add your Facebook accounts
   (email/phone + password). Cookie login and 2FA are optional extras.
2. **Products** tab → **+ New product**: name, price, category, titles
   (one per line — each listing gets a different one), locations
   (e.g. `Manchester, United Kingdom`), and the folder with your product photos.
3. **Runs** tab → pick the group + product, set **batch size** and **ads per account**,
   then **Start run**.
4. Watch the live board: accounts go Pending → In Progress → Done.
   You can **Pause / Resume / Stop** anytime. If the laptop shuts down mid-run,
   start again with "Fresh data" **unchecked** to continue where you stopped.

> **Honest note:** Facebook discourages automation and accounts can get banned.
> Start small (1–2 accounts, a few listings a day) and scale up slowly.

### Where is my data?

- Listings, accounts and logs: stored on your laptop only
  (Windows: `%APPDATA%\FMS\fms.db`).
- Passwords are encrypted. The key file is `%APPDATA%\FMS\fms.key` —
  **back it up**: if it's lost, saved passwords can't be recovered.

### Batch size = waves

Batch size **N** opens **N browser windows at once, each on a different account**.
Each window finishes its account's full listing count, and only then does the
next wave start with N new accounts.

---

## 🛠 For developers

### Dev setup (Windows, macOS or Linux)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |  macOS/Linux: source .venv/bin/activate
pip install -r fms/requirements.txt
python -m playwright install chromium   # real browser for listing runs
```

### Run the dashboard (dev)

```bash
uvicorn fms.app:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

### Run the tests (no real Facebook — mock driver only)

```bash
python fms/tests/smoke_test.py
```

Covers: encryption, DB/API CRUD, dashboard routes, wave batching (parallelism),
title rotation + price randomization, pause/resume, stop → resume continuation,
proxy validation, human-like typing, 2FA codes.

### Project layout

```
fms/
  __init__.py      version + frozen-exe bootstrap (bundled Chromium path)
  config.py        data dirs, encryption-key path, resource_path() (PyInstaller-safe)
  crypto.py        Fernet encryption for secrets at rest
  db.py            SQLite schema + thread-safe wrapper
  accounts.py      groups, accounts CRUD, bulk import, TOTP 2FA codes
  proxies.py       optional per-account proxies + validation
  products.py      products, rotating titles/locations, images, categories
  selectors.py     ALL Facebook Marketplace selectors in one place (update here when FB changes)
  human.py         human-like typing / mouse / delays
  browser.py       BaseDriver + PlaywrightDriver (real) + MockDriver (tests)
  engine.py        wave scheduler: waves, rotation, pause/resume, resume-after-interruption
  app.py           FastAPI dashboard (pages + /api/*)
  launcher.py      FMS.exe entry point: server + auto-open browser
  templates/, static/   dashboard UI (bundled into the exe via --add-data)
  tests/smoke_test.py
build_exe.py       PyInstaller build script (run on Windows)
.github/workflows/build-windows.yml   GitHub Actions: builds FMS.exe on a Windows runner
```

### Build the .exe

PyInstaller can't cross-compile: the Windows exe must be built on Windows.

- **Automatic (recommended):** push this folder to GitHub → Actions tab →
  "Build FMS.exe" → Run workflow → download the `FMS-windows` artifact.
- **Manual on a Windows machine:** `pip install -r fms/requirements.txt pyinstaller`
  → `python -m playwright install chromium` → `python build_exe.py`
  → `dist\FMS.exe` (~200–300 MB, mostly Chromium).

### Facebook selectors

All Marketplace selectors live in `fms/selectors.py` — nothing else hard-codes them.
Facebook changes its page structure regularly: when a step breaks, update the
selector there. They are best-effort and must be verified against the live site.

### What's NOT in Phase 1 (coming later)

Messenger inbox, Scrap module, Script Engine (beyond a simple run config),
Verification Helper, analytics, subscription/licensing (not planned — personal tool).
