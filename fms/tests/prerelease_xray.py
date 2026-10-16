"""FMS pre-release X-ray — MANDATORY before any version reaches the user.

Why this file exists (2026-10-16): v0.2.16 shipped TWO bugs that the existing
tests did not catch, and both landed on the user's laptop:
  1. Accounts page -> "Internal Server Error". Cause: `chr(39)` inside
     accounts.html (Jinja2 has no `chr`). It only crashes when at least one
     group EXISTS, so the old smoke test (empty database) passed green.
  2. In-app Update -> download never started. Cause: the staged
     fms_version.json carried a wrong download URL. Nobody verified the
     manifest against the real uploaded artifact before announcing the update.

This gate fixes both gaps:
  A. Every HTML page is rendered with a SEEDED database that looks like the
     user's real one — groups (one with an apostrophe in its name, the exact
     chr(39) class), accounts with passwords/cookies/proxies, products with
     full dialog fields, scripts with blocks. A 500 on any page = FAIL.
  B. Optional --manifest/--exe checks prove the staged release files are
     consistent (version matches fms.__version__, exe_size matches the real
     bytes, download URL present) BEFORE anything is uploaded to Drive.

The Drive-side half (public manifest + download URL actually reachable) is a
manual step in PRE_RELEASE_XRAY.md — it needs the live Drive link and cannot
run from this repo alone.

Usage:
    python fms/tests/prerelease_xray.py
    python fms/tests/prerelease_xray.py --manifest ~/workspace/fms-build/fms_version_new.json --exe ~/workspace/fms-build/FMS.exe

Exit 0 = all green. Any FAIL -> exit 1, do NOT ship.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

# Repo root override (used by the negative self-test). Defaults to this checkout.
_REPO = Path(os.environ.get("FMS_XRAY_REPO", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(_REPO))

# Isolate ALL state in a temp dir BEFORE importing fms modules.
_TMP = tempfile.mkdtemp(prefix="fms_xray_")
os.environ["FMS_DATA_DIR"] = _TMP

from fms import accounts as accounts_mod          # noqa: E402
from fms import products as products_mod          # noqa: E402
from fms import proxies as proxies_mod            # noqa: E402
from fms import scripts as scripts_mod            # noqa: E402
from fms import settings as settings_mod          # noqa: E402
from fms import __version__ as APP_VERSION        # noqa: E402
from fms.app import create_app                    # noqa: E402
from fms.db import Database                       # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def check(name: str, fn) -> None:
    try:
        fn()
        results.append((PASS, name, ""))
        print(f"  [{PASS}] {name}")
    except Exception as exc:  # noqa: BLE001 - the gate must catch everything
        results.append((FAIL, name, f"{type(exc).__name__}: {exc}"))
        print(f"  [{FAIL}] {name}: {type(exc).__name__}: {exc}")


def skip(name: str, reason: str) -> None:
    results.append((SKIP, name, reason))
    print(f"  [{SKIP}] {name} ({reason})")


# ------------------------------------------------------------------ seeding ---
# The whole point of this file: render pages the way the USER's database looks,
# not the way an empty test database looks.
SEEDED: dict = {}


def seed() -> Database:
    db = Database()
    # Group names deliberately include an apostrophe — the exact chr(39) bug
    # class that crashed the Accounts page in v0.2.16.
    g1 = accounts_mod.create_group(db, "Jigar's Test Group")
    g2 = accounts_mod.create_group(db, "Second Group")
    px = proxies_mod.add_proxy(db, "127.0.0.1:8080", validate=False)
    accounts_mod.add_account(db, g1, "test1@example.com", "pw-one",
                             proxy_id=px)
    accounts_mod.add_account(db, g1, "+447700900123", "pw-two")
    aid3 = accounts_mod.add_account(db, g2, "cookie@example.com",
                                    cookie="cookie-jar-value")
    accounts_mod.toggle_active(db, aid3)  # inactive account row also renders
    p1 = products_mod.create_product(
        db, "Brand New Bed", base_price=120.0,
        description="Comfortable double bed, Manchester pickup.",
        listing_type="item", condition="New", availability="In Stock",
        append_title_count=2, main_image_folder="/tmp/bed",
        delivery_options=["pickup"], tags="bed,furniture")
    p2 = products_mod.create_product(
        db, "Sofa Set", base_price=85.0, listing_type="item",
        condition="Used - like new")
    accounts_mod.set_group_products(db, g1, [p1, p2])
    blocks = [scripts_mod.make_block("scroll_feed", "b1"),
              scripts_mod.make_block("post", "b2", product_id=p1)]
    sid = scripts_mod.create_script(db, "Morning Script", blocks=blocks)
    settings_mod.save_all(db, {"batch_size": "2", "shuffle": "1"})
    SEEDED.update(group1=g1, group2=g2, script_id=sid,
                  product1=p1, product2=p2)
    return db


# ------------------------------------------------------------------- pages ---
def t_pages_render() -> None:
    """Every human HTML page must return 200 with seeded data — no 500s."""
    from fastapi.testclient import TestClient
    app = create_app()
    c = TestClient(app, raise_server_exceptions=False)
    g1, sid = SEEDED["group1"], SEEDED["script_id"]
    pages = [
        "/", "/accounts", f"/accounts?group_id={g1}",
        "/products", "/scripts", f"/scripts/{sid}",
        "/runs", "/logs", "/logs?run_id=1", "/settings",
    ]
    for path in pages:
        r = c.get(path)
        assert r.status_code == 200, f"{path} -> HTTP {r.status_code}"
        body = r.text
        assert "Internal Server Error" not in body, f"{path} rendered 500 page"
        assert "Traceback" not in body, f"{path} leaked a traceback"
        assert "<html" in body.lower(), f"{path} did not return HTML"
    # Not-found paths must 404 cleanly, not 500.
    for path in ("/runs/999999", "/scripts/999999"):
        r = c.get(path)
        assert r.status_code == 404, f"{path} -> HTTP {r.status_code} (want 404)"


def t_api_sanity() -> None:
    from fastapi.testclient import TestClient
    app = create_app()
    c = TestClient(app, raise_server_exceptions=False)
    g1 = SEEDED["group1"]
    for path in ("/api/groups", f"/api/accounts?group_id={g1}",
                 "/api/products", "/api/scripts", "/api/settings",
                 "/api/actions", "/api/logs"):
        r = c.get(path)
        assert r.status_code == 200, f"{path} -> HTTP {r.status_code}"
        r.json()  # must be valid JSON


def t_seed_roundtrip() -> None:
    """The seeded rows the pages rendered must actually exist."""
    assert SEEDED["group1"] and SEEDED["group2"]
    assert SEEDED["script_id"]
    assert SEEDED["product1"] and SEEDED["product2"]


# ------------------------------------------------- release artifact checks ---
def t_manifest(args) -> None:
    """Staged fms_version.json must describe THIS build truthfully."""
    if not args.manifest:
        skip("manifest consistency", "no --manifest given")
        return
    mp = Path(args.manifest)
    assert mp.exists(), f"manifest not found: {mp}"
    info = json.loads(mp.read_text(encoding="utf-8"))
    assert str(info.get("version", "")) == APP_VERSION, (
        f"manifest version {info.get('version')!r} != app version {APP_VERSION!r}")
    exe_url = str(info.get("exe_url") or "")
    assert exe_url.startswith("http"), "exe_url missing/empty — download cannot start"
    size = info.get("exe_size")
    assert isinstance(size, int) and size > 40_000_000, (
        f"exe_size {size!r} looks wrong (want real byte count of the exe)")
    if args.exe:
        real = Path(args.exe).stat().st_size
        assert size == real, (f"manifest exe_size {size} != staged exe bytes {real} "
                              f"— user would download a truncated/wrong file")


def t_exe_magic(args) -> None:
    if not args.exe:
        skip("exe sanity", "no --exe given")
        return
    ep = Path(args.exe)
    assert ep.exists(), f"exe not found: {ep}"
    with open(ep, "rb") as f:
        magic = f.read(2)
    assert magic == b"MZ", f"{ep.name} is not a Windows executable (bad magic)"
    assert ep.stat().st_size > 40_000_000, f"{ep.name} suspiciously small"


# ------------------------------------------------------------------- main ---
def main() -> int:
    ap = argparse.ArgumentParser(description="FMS pre-release X-ray gate")
    ap.add_argument("--manifest", default="",
                    help="staged fms_version.json to verify")
    ap.add_argument("--exe", default="",
                    help="staged FMS.exe to verify")
    args = ap.parse_args()

    print(f"FMS pre-release X-ray  (app version {APP_VERSION}, repo {_REPO})")
    print("--- seeding a realistic database ---")
    seed()
    print("--- page renders (the v0.2.16 bug class) ---")
    check("seed rows exist", t_seed_roundtrip)
    check("all HTML pages render 200, no 500s", t_pages_render)
    check("API endpoints sane", t_api_sanity)
    print("--- release artifacts (the wrong-URL bug class) ---")
    check("manifest consistency", lambda: t_manifest(args))
    check("exe sanity", lambda: t_exe_magic(args))

    n_fail = sum(1 for s, _, _ in results if s == FAIL)
    n_skip = sum(1 for s, _, _ in results if s == SKIP)
    print(f"\nX-ray result: {len(results)-n_fail-n_skip} passed, "
          f"{n_skip} skipped, {n_fail} failed")
    if n_fail:
        print("DO NOT SHIP — fix the failures above first.")
        return 1
    print("GREEN — safe to proceed with the PRE_RELEASE_XRAY.md upload steps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
