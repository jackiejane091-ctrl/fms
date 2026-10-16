# FMS pre-release X-ray — release procedure

Standing rule since 2026-10-16 (user's words: **"Pehle khud X-ray kiya kroo, phr mjhy bheja krooo"**).
No version reaches the user's laptop without passing every step below. No exceptions,
no "jaldi me bhej diya".

## Why

On 2026-10-16 two broken updates shipped within one hour:
1. **v0.2.16** — Accounts page crashed with "Internal Server Error" (`chr(39)` in
   `accounts.html`; Jinja2 has no `chr`). The old smoke test passed because it
   rendered pages on an **empty** database and the bug only fires when a group exists.
2. **v0.2.16 → v0.2.19 update** — the in-app download never started because the
   staged `fms_version.json` carried a **wrong download URL**. Nobody verified the
   manifest against the real artifact before announcing "Update dabao".

## The gate (code side — automated)

```bash
cd ~/workspace/marketplace-autolister
.venv/bin/python fms/tests/prerelease_xray.py
```

- Seeds a realistic database (groups — one named `Jigar's Test Group` with an
  apostrophe, accounts with password/cookie/proxy rows, products, a script with
  blocks, settings) and renders **every** HTML page: a single 500 = FAIL.
- Verifies API endpoints return valid JSON.
- With `--manifest PATH --exe PATH`: proves the staged manifest matches the
  staged exe (version == `fms.__version__`, `exe_size` == real bytes,
  `exe_url` present and http).
- Proven: FAILS on the v0.2.16 `accounts.html` (HTTP 500), PASSES on v0.2.19.

The GitHub workflow (`.github/workflows/build-windows.yml`) runs this **before**
PyInstaller. A red X-ray stops the build — no broken exe can be produced, let
alone uploaded.

## The gate (Drive side — manual, after upload)

After the new `FMS.exe` is on Drive and the Drive `fms_version.json` is updated,
verify from the **public** side before telling the user anything:

```bash
# 1. Public manifest serves the NEW version
curl -s "<VERSION_URL>" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['version'], d['exe_size'])"
#    -> must print the new version, e.g. 0.2.19 <byte-count>

# 2. The download URL actually resolves to the right file
curl -sIL "<exe_url>" | grep -i -E "^HTTP|content-length"
#    -> HTTP 200 and Content-Length == exe_size from step 1
```

Only when both pass: tell the user to press **Update** in FMS.

## Checklist (every release)

- [ ] `prerelease_xray.py` green on the release commit (CI enforces this too)
- [ ] Workflow build succeeded; downloaded artifact is a valid exe
- [ ] `fms_version.json` staged with correct `version`, `exe_size` (real bytes),
      `exe_url`, changelog — verified with `--manifest/--exe`
- [ ] Drive `FMS.exe` replaced; Drive manifest updated
- [ ] Public manifest + download URL verified with the curl steps above
- [ ] THEN announce the update to the user
