"""FMS Phase 1 smoke tests. Run:  python fms/tests/smoke_test.py

Covers: crypto, DB/API CRUD, dashboard routes, wave batching, rotation,
pause/resume, resume-after-interruption, proxy validation (negative),
human typing, 2FA. Uses MockDriver only — NEVER touches real Facebook.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

# Isolate all state in a temp dir BEFORE importing fms modules.
_TMP = tempfile.mkdtemp(prefix="fms_smoke_")
os.environ["FMS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fms import accounts as accounts_mod          # noqa: E402
from fms import crypto                            # noqa: E402
from fms import products as products_mod          # noqa: E402
from fms import proxies as proxies_mod            # noqa: E402
from fms.browser import MockDriver                # noqa: E402
from fms.db import Database                       # noqa: E402
from fms.engine import ListingEngine, RunConfig    # noqa: E402
from fms.human import human_type                   # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, fn) -> None:
    try:
        fn()
        results.append((PASS, name, ""))
        print(f"  [{PASS}] {name}")
    except Exception as exc:
        results.append((FAIL, name, f"{type(exc).__name__}: {exc}"))
        print(f"  [{FAIL}] {name}: {type(exc).__name__}: {exc}")


def wait_until(fn, timeout: float = 30.0, poll: float = 0.2):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(poll)
    return False


# ------------------------------------------------------------------ crypto ---
def t_crypto():
    assert crypto.decrypt_str(crypto.encrypt_str("s3cr3t!")) == "s3cr3t!"
    assert crypto.encrypt_str(None) is None
    assert crypto.decrypt_str("") is None
    # key persists in the data dir
    assert Path(_TMP, "fms.key").exists()


# ---------------------------------------------------------------------- db ---
DB = Database()


def t_db_crud():
    gid = accounts_mod.create_group(DB, "smoke-group")
    assert gid > 0
    added, errors = accounts_mod.bulk_add_accounts(
        DB, gid, "a@gmail.com;pw1\n03001234567;pw2\n# comment\n\nb@gmail.com;pw3;9.9.9.9:8080",
        validate_proxies=False)  # no direct network in CI; validation is tested separately
    assert added == 3, f"added={added} errors={errors}"
    assert not errors, errors
    accts = accounts_mod.list_accounts(DB, gid)
    assert len(accts) == 3
    # the proxy line was imported (validation skipped in this env)
    assert accts[2]["proxy_id"] is not None
    # password is encrypted at rest
    row = DB.fetchone("SELECT password_enc FROM accounts WHERE label='a@gmail.com'")
    assert row["password_enc"] != "pw1"
    assert "pw1" not in (row["password_enc"] or "")
    creds = accounts_mod.account_credentials(DB, accts[0])
    assert creds["password"] == "pw1"
    # toggle + 2fa
    assert accounts_mod.toggle_active(DB, accts[0]["id"]) == 0
    assert accounts_mod.toggle_active(DB, accts[0]["id"]) == 1
    aid = accounts_mod.add_account(DB, gid, "c@gmail.com", "pw",
                                   twofa_secret="JBSWY3DPEHPK3PXP")
    code = accounts_mod.get_twofa_code(DB, aid)
    assert code and len(code) == 6 and code.isdigit(), code
    assert accounts_mod.get_twofa_code(DB, accts[0]["id"]) is None


def t_products():
    imgdir = Path(_TMP, "imgs")
    imgdir.mkdir()
    for i in range(3):
        (imgdir / f"pic{i}.jpg").write_bytes(b"\xff\xd8fake")
    (imgdir / "notes.txt").write_text("nope")
    pid = products_mod.create_product(
        DB, "Test Bed", base_price=299.0, description="Nice bed",
        tags="bed, furniture", category="Furniture > Beds & bed frames",
        attributes={"color": "grey"}, image_folder=str(imgdir))
    assert products_mod.set_titles(DB, pid, ["T1", "T2", "T3"]) == 3
    assert products_mod.set_locations(
        DB, pid, ["Manchester, United Kingdom", "London, United Kingdom"]) == 2
    p = products_mod.get_product(DB, pid)
    assert p["titles"] == ["T1", "T2", "T3"]
    assert len(p["images"]) == 3, p["images"]
    assert p["attributes"] == {"color": "grey"}
    assert products_mod.category_attributes("Furniture > Beds & bed frames")
    return pid, gid_of_products()


def gid_of_products():
    return DB.fetchone("SELECT id FROM groups WHERE name='smoke-group'")["id"]


# ------------------------------------------------------------------- proxy ---
def t_proxy_negative():
    ok, msg = proxies_mod.validate_proxy("127.0.0.1", 1, timeout=2)
    assert ok is False, msg
    p = proxies_mod.parse_proxy("1.2.3.4:8080:user:pass")
    assert (p["host"], p["port"], p["username"], p["password"]) == \
        ("1.2.3.4", 8080, "user", "pass")
    try:
        proxies_mod.parse_proxy("nope")
        raise AssertionError("should have raised")
    except ValueError:
        pass


# ------------------------------------------------------------------- human ---
class _FakeLocator:
    def __init__(self):
        self.chars: list[str] = []

    def click(self):
        pass

    def press(self, ch: str):
        self.chars.append(ch)


def t_human_typing():
    fake = _FakeLocator()
    human_type(fake, "Hi!")
    assert "".join(fake.chars) == "Hi!", fake.chars


# ------------------------------------------------------------------ engine ---
def make_engine():
    MockDriver.reset()
    return ListingEngine(Database(), driver_factory=MockDriver)


def seed_run_group(db: Database, n_accounts: int, n_titles=("T1", "T2", "T3")):
    gid = accounts_mod.create_group(db, f"g-{time.time_ns()}")
    for i in range(n_accounts):
        accounts_mod.add_account(db, gid, f"user{i}@x.com", f"pw{i}")
    pid = products_mod.create_product(db, "Prod", base_price=100.0)
    products_mod.set_titles(db, pid, list(n_titles))
    products_mod.set_locations(db, pid, ["Manchester, United Kingdom"])
    return gid, pid


def t_wave_batching():
    eng = make_engine()
    gid, pid = seed_run_group(eng.db, 5)
    cfg = RunConfig(group_id=gid, product_ids=[pid], batch_size=2,
                    min_ads=3, max_ads=3, min_delay_s=0.01, max_delay_s=0.02,
                    account_start_gap_s=0)  # gap would serialize the wave
    rid = eng.start_run(cfg)
    assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 30)
    run = eng.get_run(rid)
    assert run["total_posted"] == 15, run
    assert MockDriver.max_concurrent == 2, MockDriver.max_concurrent
    per = {}
    for c in MockDriver.calls:
        if c["event"] == "post_start":
            per[c["label"]] = per.get(c["label"], 0) + 1
    assert per == {f"user{i}@x.com": 3 for i in range(5)}, per


def t_rotation():
    eng = make_engine()
    gid, pid = seed_run_group(eng.db, 2, n_titles=("A", "B", "C"))
    cfg = RunConfig(group_id=gid, product_ids=[pid], batch_size=2,
                    min_ads=3, max_ads=3, min_delay_s=0.01, max_delay_s=0.02,
                    min_price=10, max_price=20)
    rid = eng.start_run(cfg)
    assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 30)
    rows = eng.db.fetchall("SELECT title_used, price_used FROM listings WHERE run_id=?",
                           (rid,))
    assert len(rows) == 6
    titles = [r["title_used"] for r in rows]
    assert sorted(titles) == ["A", "A", "B", "B", "C", "C"], titles
    for r in rows:
        assert 10 <= r["price_used"] <= 20, r["price_used"]


def t_pause_resume():
    eng = make_engine()
    MockDriver.post_delay = 0.25
    gid, pid = seed_run_group(eng.db, 1)
    cfg = RunConfig(group_id=gid, product_ids=[pid], batch_size=1,
                    min_ads=6, max_ads=6, min_delay_s=0.05, max_delay_s=0.1)
    rid = eng.start_run(cfg)
    time.sleep(0.7)
    assert eng.pause_run(rid) is True
    time.sleep(0.3)
    n1 = eng.get_run(rid)["total_posted"]
    time.sleep(0.9)
    n2 = eng.get_run(rid)["total_posted"]
    assert n1 == n2, f"progress during pause: {n1} -> {n2}"
    assert eng.get_run(rid)["paused"] is True
    assert eng.resume_run(rid) is True
    assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 30)
    assert eng.get_run(rid)["total_posted"] == 6
    MockDriver.post_delay = 0.05


def t_resume_after_interruption():
    eng = make_engine()
    MockDriver.post_delay = 0.25
    gid, pid = seed_run_group(eng.db, 2)
    cfg = RunConfig(group_id=gid, product_ids=[pid], batch_size=2,
                    min_ads=4, max_ads=4, min_delay_s=0.05, max_delay_s=0.1)
    rid = eng.start_run(cfg, fresh=True)
    # Stop deterministically mid-run (not via a fixed sleep).
    assert wait_until(lambda: eng.get_run(rid)["total_posted"] >= 3, 30)
    assert eng.stop_run(rid) is True
    assert wait_until(lambda: eng.get_run(rid)["status"] == "stopped", 30)
    posted_before = eng.get_run(rid)["total_posted"]
    assert 0 < posted_before < 8, posted_before
    # resume: same run id continues, already-posted listings are skipped
    rid2 = eng.start_run(cfg, fresh=False)
    assert rid2 == rid, (rid2, rid)
    assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 30)
    run = eng.get_run(rid)
    assert run["total_posted"] == 8, run
    for a in run["per_account"]:
        assert a["posted"] <= 4, a
    MockDriver.post_delay = 0.05


# ---------------------------------------------------------------- dashboard ---
def t_dashboard_routes():
    from fastapi.testclient import TestClient

    from fms.app import create_app

    app = create_app(driver_name="mock")
    c = TestClient(app)
    assert c.get("/").status_code == 200
    assert c.get("/accounts").status_code == 200
    assert c.get("/products").status_code == 200
    assert c.get("/runs").status_code == 200
    assert c.get("/logs").status_code == 200
    assert c.get("/api/categories").status_code == 200
    r = c.post("/api/groups", data={"name": "web-group"})
    assert r.status_code in (200, 303), r.status_code
    gid = c.get("/api/groups").json()[-1]["id"]
    r = c.post("/api/accounts", json={"group_id": gid, "label": "w@x.com",
                                      "password": "pw"})
    assert r.status_code == 200, r.text
    r = c.post("/api/products", json={"name": "WebProd", "base_price": 50,
                                      "titles": ["WT1"], "locations": ["Leeds, United Kingdom"]})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    r = c.post("/api/runs", json={"group_id": gid, "product_ids": [pid],
                                  "batch_size": 1, "min_ads": 2, "max_ads": 2,
                                  "min_delay_s": 0.01, "max_delay_s": 0.02})
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    assert wait_until(lambda: c.get(f"/api/runs/{rid}").json()["status"]
                      == "completed", 30), c.get(f"/api/runs/{rid}").json()
    assert c.get(f"/runs/{rid}").status_code == 200
    assert c.get("/api/logs", params={"run_id": rid}).status_code == 200
    # pause/resume/stop endpoints exist (run already done -> ok False)
    assert c.post(f"/api/runs/{rid}/pause").json() == {"ok": False}


def main() -> int:
    print("FMS Phase 1 smoke tests  (MockDriver only — no real Facebook)\n")
    check("crypto roundtrip + key file", t_crypto)
    check("db: groups/accounts CRUD, encryption, bulk import, 2FA", t_db_crud)
    check("db: products, titles, locations, images, categories", t_products)
    check("proxy: negative validation + parsing", t_proxy_negative)
    check("human: char-by-char typing order", t_human_typing)
    check("engine: wave batching (N=2 parallel, 5 accounts x 3 ads)", t_wave_batching)
    check("engine: title rotation + price randomization", t_rotation)
    check("engine: pause blocks progress, resume completes", t_pause_resume)
    check("engine: stop -> resume continues same run, no over-post", t_resume_after_interruption)
    check("dashboard: pages + API + full mock run via HTTP", t_dashboard_routes)
    print()
    failed = [r for r in results if r[0] == FAIL]
    print(f"{len(results) - len(failed)}/{len(results)} passed.")
    for _, name, err in failed:
        print(f"  FAILED: {name}: {err}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
