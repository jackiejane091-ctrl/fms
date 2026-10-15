"""FMS local dashboard (FastAPI). Runs on localhost only.

HTML pages for humans + /api/* JSON endpoints for scripting.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import accounts as accounts_mod
from . import browsers as browsers_mod
from . import config
from . import engine as engine_mod
from . import products as products_mod
from . import proxies as proxies_mod
from . import scrap as scrap_mod
from . import scripts as scripts_mod
from . import updater as updater_mod
from .browser import get_driver
from .db import Database
from .engine import ListingEngine, RunConfig

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(config.resource_path("templates")))


class AccountIn(BaseModel):
    group_id: int
    label: str
    password: str | None = None
    cookie: str | None = None
    twofa_secret: str | None = None
    proxy_id: int | None = None
    browser: str = "chrome"
    ix_profile_id: str | None = None


class AccountUpdate(BaseModel):
    label: str | None = None
    password: str | None = None
    cookie: str | None = None
    twofa_secret: str | None = None
    proxy_id: int | None = None
    browser: str | None = None
    ix_profile_id: str | None = None
    active: int | None = None
    group_id: int | None = None


class ProductIn(BaseModel):
    name: str
    base_price: float | None = None
    description: str | None = None
    tags: str | None = None
    category: str | None = None
    attributes: dict | None = None
    image_folder: str | None = None
    video_path: str | None = None
    titles: list[str] | None = None
    locations: list[str] | None = None
    # Alignment pass #4: full Add Product dialog.
    listing_type: str = "item"
    condition: str | None = None
    availability: str | None = None
    append_title_count: int = 0
    main_image_folder: str | None = None
    extra_image_folder: str | None = None
    delivery_options: list[str] | None = None
    descriptions: list[str] | None = None


class RunIn(BaseModel):
    group_id: int
    product_ids: list[int] = []
    mode: str = "complete"
    script_id: int | None = None
    batch_size: int = 2
    min_ads: int = 1
    max_ads: int = 3
    renew_min_count: int = 1
    renew_max_count: int = 5
    scrap_include_description: bool = True
    scrap_include_location: bool = True
    download_folder: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    min_delay_s: float = 2.0
    max_delay_s: float = 5.0
    shuffle: bool = False
    split_locations: bool = False
    condition: str = "New"
    availability: str = "In Stock"
    publish_mode: str = "all_together"
    publish_gap_min: float = 5.0
    fresh: bool = True
    # -- alignment settings (v6.1.2.0) ---------------------------------------
    hide_from_friends: bool = False
    account_start_gap_s: float = 5.0
    max_parallel_browsers: int = 5
    retry_count: int = 3
    tabs_without_limit: int = 2
    tabs_draft_publish: int = 6
    action_urls: list[str] = []
    new_location: str | None = None
    wait_minutes: float = 0
    exclude_labels: list[str] = []
    speed: float = 1.0
    # -- alignment pass #2: typing mode + optional IXBrowser -------------------
    typing_mode: str = "human"
    typing_preset: str = "normal"
    min_delay_ms: int = 30
    max_delay_ms: int = 80
    thinking_pause_min_ms: int = 200
    thinking_pause_max_ms: int = 600
    extra_delay_after_punctuation_ms: int = 80
    newline_pause_min_ms: int = 300
    newline_pause_max_ms: int = 900
    hybrid_threshold_chars: int = 500
    ix_browser_enabled: bool = False
    ix_local_api_host: str = "127.0.0.1"
    ix_local_api_port: int = 53200


class ScriptIn(BaseModel):
    name: str
    blocks: list[dict] | None = None
    shuffle: bool = False


class ScriptUpdate(BaseModel):
    name: str | None = None
    blocks: list[dict] | None = None
    shuffle: bool | None = None


class ScrapIn(BaseModel):
    group_id: int
    urls: list[str]
    category: str
    overrides: dict | None = None
    customize: dict[str, dict] | None = None
    fallback_description: str = ""
    country_append: str = ""
    backup_location: str = ""


class ScrapSaveIn(BaseModel):
    name: str
    items: list[dict]


class _ProxyBulk(BaseModel):
    account_ids: list[int] = []
    proxies_text: str = ""


class _Ids(BaseModel):
    account_ids: list[int] = []


class _ProductIds(BaseModel):
    product_ids: list[int] = []


def create_app(driver_name: str = "playwright") -> FastAPI:
    db = Database()
    engine = ListingEngine(db, driver_factory=get_driver(driver_name))

    app = FastAPI(title="FMS Dashboard", docs_url="/api/docs")
    app.state.db = db
    app.state.engine = engine
    app.mount("/static", StaticFiles(directory=str(config.resource_path("static"))),
              name="static")

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        stats = {
            "groups": len(accounts_mod.list_groups(db)),
            "accounts": db.fetchone("SELECT COUNT(*) AS c FROM accounts")["c"],
            "products": db.fetchone("SELECT COUNT(*) AS c FROM products")["c"],
            "runs": db.fetchone("SELECT COUNT(*) AS c FROM runs")["c"],
        }
        return templates.TemplateResponse(
            request, "index.html", {"request": request, "stats": stats,
                                    "runs": engine.list_runs(10)})

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request, group_id: int | None = None):
        groups = accounts_mod.list_groups(db)
        if group_id is None and groups:
            group_id = groups[0]["id"]
        accts = accounts_mod.list_accounts(db, group_id) if group_id else []
        # Enrich rows for the table: proxy text + status, secret presence.
        enriched = []
        for a in accts:
            d = dict(a)
            d["has_password"] = bool(a["password_enc"])
            d["has_cookie"] = bool(a["cookie_enc"])
            d["proxy_text"] = None
            d["proxy_status"] = None  # True working / False dead / None unknown
            if a["proxy_id"]:
                prow = db.fetchone("SELECT host, port, working FROM proxies"
                                   " WHERE id = ?", (a["proxy_id"],))
                if prow:
                    d["proxy_text"] = f"{prow['host']}:{prow['port']}"
                    d["proxy_status"] = (None if prow["working"] is None
                                         else bool(prow["working"]))
            enriched.append(d)
        group_products = (accounts_mod.list_group_products(db, group_id)
                          if group_id else [])
        return templates.TemplateResponse(request, "accounts.html", {"request": request, "groups": groups, "group_id": group_id,
             "accounts": enriched,
             "products": products_mod.list_products(db),
             "group_products": group_products})

    @app.get("/products", response_class=HTMLResponse)
    def products_page(request: Request):
        return templates.TemplateResponse(request, "products.html", {"request": request, "products": products_mod.list_products(db),
             "categories": products_mod.load_categories(),
             "listing_types": products_mod.LISTING_TYPES,
             "delivery_options": products_mod.DELIVERY_LABELS})

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        return templates.TemplateResponse(request, "runs.html", {"request": request, "runs": engine.list_runs(30),
             "groups": accounts_mod.list_groups(db),
             "products": products_mod.list_products(db),
             "scripts": scripts_mod.list_scripts(db),
             "actions": engine_mod.ACTIONS})

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail_page(request: Request, run_id: int):
        run = engine.get_run(run_id)
        if not run:
            raise HTTPException(404, "run not found")
        return templates.TemplateResponse(
            request, "run_detail.html", {"request": request, "run": run,
                                         "logs": engine.recent_logs(run_id, 100)})

    @app.get("/scripts", response_class=HTMLResponse)
    def scripts_page(request: Request):
        return templates.TemplateResponse(
            request, "scripts.html",
            {"request": request, "scripts": scripts_mod.list_scripts(db),
             "products": products_mod.list_products(db),
             "block_types": scripts_mod.BLOCK_TYPES})

    @app.get("/scripts/{script_id}", response_class=HTMLResponse)
    def script_edit_page(request: Request, script_id: int):
        script = scripts_mod.get_script(db, script_id)
        if not script:
            raise HTTPException(404, "script not found")
        return templates.TemplateResponse(
            request, "script_edit.html",
            {"request": request, "script": script,
             "products": products_mod.list_products(db),
             "block_types": scripts_mod.BLOCK_TYPES,
             "block_labels": scripts_mod.BLOCK_LABELS,
             "builder_categories": scripts_mod.BUILDER_CATEGORIES,
             "settings_meta": scripts_mod.BLOCK_SETTINGS_META,
             "legacy_params": sorted(scripts_mod.LEGACY_ACTIVITY_PARAMS)})

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page(request: Request, run_id: int | None = None):
        return templates.TemplateResponse(request, "logs.html", {"request": request, "logs": engine.recent_logs(run_id, 300),
             "run_id": run_id, "runs": engine.list_runs(30)})

    # ------------------------------------------------------------------- API
    @app.get("/api/groups")
    def api_list_groups():
        return accounts_mod.list_groups(db)

    @app.post("/api/groups")
    def api_create_group(name: str = Form(...)):
        gid = accounts_mod.create_group(db, name)
        return RedirectResponse("/accounts", status_code=303)

    @app.delete("/api/groups/{group_id}")
    def api_delete_group(group_id: int):
        accounts_mod.delete_group(db, group_id)
        return {"ok": True}

    @app.post("/api/groups/{group_id}/clone")
    def api_clone_group(group_id: int, new_name: str | None = Form(None)):
        """Duplicate a group: '<name> (copy)' with its accounts + products."""
        try:
            new_id = accounts_mod.clone_group(db, group_id, new_name)
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        return {"ok": True, "id": new_id}

    @app.put("/api/groups/{group_id}")
    def api_rename_group(group_id: int, name: str = Form(...)):
        accounts_mod.rename_group(db, group_id, name)
        return {"ok": True}

    @app.post("/api/groups/{group_id}/products")
    def api_set_group_products(group_id: int, data: _ProductIds):
        accounts_mod.set_group_products(db, group_id, data.product_ids)
        return {"ok": True}

    @app.get("/api/groups/{group_id}/products")
    def api_get_group_products(group_id: int):
        return accounts_mod.list_group_products(db, group_id)

    @app.get("/api/accounts")
    def api_list_accounts(group_id: int | None = None):
        rows = accounts_mod.list_accounts(db, group_id)
        # Never expose encrypted blobs via the API.
        return [{k: v for k, v in r.items() if not k.endswith("_enc")}
                for r in rows]

    @app.post("/api/accounts")
    def api_add_account(data: AccountIn):
        aid = accounts_mod.add_account(
            db, data.group_id, data.label, data.password, data.cookie,
            data.twofa_secret, data.proxy_id, data.browser, data.ix_profile_id)
        return {"ok": True, "id": aid}

    @app.post("/api/accounts/bulk")
    def api_bulk_accounts(group_id: int = Form(...), text: str = Form(...)):
        added, errors = accounts_mod.bulk_add_accounts(db, group_id, text)
        return {"ok": True, "added": added, "errors": errors}

    @app.post("/api/accounts/import")
    async def api_import_accounts_file(group_id: int = Form(...),
                                       file: UploadFile = File(...)):
        """Import from File: a .txt file with the same bulk format."""
        try:
            raw = (await file.read()).decode("utf-8-sig")
        except Exception as exc:
            raise HTTPException(400, f"cannot read file: {exc}")
        added, errors = accounts_mod.bulk_add_accounts(db, group_id, raw)
        return {"ok": True, "added": added, "errors": errors}

    @app.post("/api/accounts/proxies/assign")
    def api_assign_proxies(data: _ProxyBulk):
        try:
            mapping = accounts_mod.assign_proxies_to_accounts(
                db, data.account_ids, data.proxies_text.splitlines())
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "assigned": mapping}

    @app.post("/api/accounts/proxies/remove")
    def api_remove_proxies(data: _Ids):
        n = accounts_mod.remove_proxies_from_accounts(db, data.account_ids)
        return {"ok": True, "cleared": n}

    @app.post("/api/accounts/proxies/check")
    def api_check_account_proxies(data: _Ids):
        return {"ok": True,
                "results": accounts_mod.check_account_proxies(db,
                                                             data.account_ids)}

    @app.post("/api/accounts/proxies/remove-dead")
    def api_remove_dead_proxies(data: _Ids):
        return {"ok": True,
                **accounts_mod.remove_not_working_proxies(db,
                                                         data.account_ids)}

    @app.post("/api/accounts/delete-selected")
    def api_delete_selected(data: _Ids):
        n = accounts_mod.delete_accounts(db, data.account_ids)
        return {"ok": True, "deleted": n}

    @app.put("/api/accounts/{account_id}")
    def api_update_account(account_id: int, data: AccountUpdate):
        fields = {k: v for k, v in data.model_dump().items() if v is not None}
        accounts_mod.update_account(db, account_id, **fields)
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/toggle")
    def api_toggle_account(account_id: int):
        return {"ok": True, "active": accounts_mod.toggle_active(db, account_id)}

    @app.delete("/api/accounts/{account_id}")
    def api_delete_account(account_id: int):
        accounts_mod.delete_account(db, account_id)
        return {"ok": True}

    @app.get("/api/accounts/{account_id}/2fa")
    def api_twofa(account_id: int):
        code = accounts_mod.get_twofa_code(db, account_id)
        if code is None:
            raise HTTPException(404, "no 2FA secret stored for this account")
        return {"code": code}

    # -- proxies ---------------------------------------------------------------
    @app.post("/api/accounts/{account_id}/proxy")
    def api_set_proxy(account_id: int, proxy: str = Form(...),
                      validate_proxy: bool = Form(True)):
        pid = proxies_mod.add_proxy(db, proxy, validate=validate_proxy)
        accounts_mod.update_account(db, account_id, proxy_id=pid)
        return RedirectResponse("/accounts", status_code=303)

    @app.post("/api/proxies/check")
    def api_check_proxies():
        return {"results": proxies_mod.check_all_proxies(db)}

    @app.post("/api/proxies/validate")
    def api_validate_proxy(proxy: str = Form(...)):
        p = proxies_mod.parse_proxy(proxy)
        ok, msg = proxies_mod.validate_proxy(p["host"], p["port"],
                                             p["username"], p["password"])
        return {"working": ok, "message": msg}

    # -- products --------------------------------------------------------------
    @app.get("/api/products")
    def api_list_products():
        return products_mod.list_products(db)

    @app.get("/api/products/{product_id}")
    def api_get_product(product_id: int):
        prod = products_mod.get_product(db, product_id)
        if not prod:
            raise HTTPException(404, "product not found")
        return prod

    @app.post("/api/products")
    def api_create_product(data: ProductIn):
        pid = products_mod.create_product(
            db, data.name, data.base_price, data.description, data.tags,
            data.category, data.attributes, data.image_folder, data.video_path,
            listing_type=data.listing_type, condition=data.condition,
            availability=data.availability,
            append_title_count=data.append_title_count,
            main_image_folder=data.main_image_folder,
            extra_image_folder=data.extra_image_folder,
            delivery_options=data.delivery_options,
            descriptions=data.descriptions)
        if data.titles:
            products_mod.set_titles(db, pid, data.titles)
        if data.locations:
            products_mod.set_locations(db, pid, data.locations)
        return {"ok": True, "id": pid}

    @app.put("/api/products/{product_id}")
    def api_update_product(product_id: int, data: dict):
        fields = dict(data)
        titles = fields.pop("titles", None)
        locations = fields.pop("locations", None)
        descriptions = fields.pop("descriptions", None)
        products_mod.update_product(db, product_id, **fields)
        if titles is not None:
            products_mod.set_titles(db, product_id, titles)
        if locations is not None:
            products_mod.set_locations(db, product_id, locations)
        if descriptions is not None:
            products_mod.set_descriptions(db, product_id, descriptions)
        return {"ok": True}

    @app.post("/api/products/{product_id}/clone")
    def api_clone_product(product_id: int):
        """Duplicate a product with all fields, lists and image folders."""
        new_id = products_mod.clone_product(db, product_id)
        return {"ok": True, "id": new_id}

    @app.delete("/api/products/{product_id}")
    def api_delete_product(product_id: int):
        products_mod.delete_product(db, product_id)
        return {"ok": True}

    @app.get("/api/categories")
    def api_categories():
        return products_mod.load_categories()

    # -- actions ---------------------------------------------------------------
    @app.get("/api/actions")
    def api_list_actions():
        """All 18 run actions of the real tool, in its exact order."""
        return [{"mode": mode, "label": label}
                for mode, label in engine_mod.ACTIONS]

    # -- runs ------------------------------------------------------------------
    @app.get("/api/runs")
    def api_list_runs():
        return engine.list_runs()

    @app.post("/api/runs")
    def api_start_run(data: RunIn):
        cfg = RunConfig(**{k: v for k, v in data.model_dump().items()
                           if k != "fresh"})
        run_id = engine.start_run(cfg, fresh=data.fresh)
        return {"ok": True, "run_id": run_id}

    @app.get("/api/runs/{run_id}")
    def api_get_run(run_id: int):
        run = engine.get_run(run_id)
        if not run:
            raise HTTPException(404, "run not found")
        return run

    @app.post("/api/runs/{run_id}/pause")
    def api_pause_run(run_id: int):
        return {"ok": engine.pause_run(run_id)}

    @app.post("/api/runs/{run_id}/resume")
    def api_resume_run(run_id: int):
        return {"ok": engine.resume_run(run_id)}

    @app.post("/api/runs/{run_id}/stop")
    def api_stop_run(run_id: int):
        return {"ok": engine.stop_run(run_id)}

    @app.post("/api/runs/{run_id}/signal")
    def api_signal_run(run_id: int, name: str = Form(...)):
        """Dashboard-initiated signal, e.g. 'download_start' for the
        Download Images & Titles manual-assist flow."""
        return {"ok": engine.signal_run(run_id, name)}

    @app.post("/api/runs/{run_id}/tabs/{tab_id}")
    def api_set_tab_status(run_id: int, tab_id: int, status: str = Form(...)):
        ok = engine.set_tab_status(run_id, tab_id, status)
        if not ok:
            raise HTTPException(400, "bad tab id or status")
        return {"ok": True}

    # -- scripts ---------------------------------------------------------------
    @app.get("/api/scripts")
    def api_list_scripts():
        return scripts_mod.list_scripts(db)

    @app.post("/api/scripts")
    def api_create_script(data: ScriptIn):
        try:
            sid = scripts_mod.create_script(db, data.name, data.blocks,
                                            data.shuffle)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "id": sid}

    @app.get("/api/scripts/{script_id}")
    def api_get_script(script_id: int):
        script = scripts_mod.get_script(db, script_id)
        if not script:
            raise HTTPException(404, "script not found")
        return script

    @app.put("/api/scripts/{script_id}")
    def api_update_script(script_id: int, data: ScriptUpdate):
        try:
            scripts_mod.update_script(db, script_id, data.name, data.blocks,
                                      data.shuffle)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.delete("/api/scripts/{script_id}")
    def api_delete_script(script_id: int):
        scripts_mod.delete_script(db, script_id)
        return {"ok": True}

    @app.get("/api/block-types")
    def api_block_types():
        return scripts_mod.BLOCK_TYPES

    # -- scrap -----------------------------------------------------------------
    @app.post("/api/scrap")
    def api_scrap(data: ScrapIn):
        """Scrape listing URLs with the group's first active account (sync)."""
        accts = accounts_mod.active_accounts_for_run(db, data.group_id)
        if not accts:
            raise HTTPException(400, "no active accounts in group")
        creds = accounts_mod.account_credentials(db, dict(accts[0]))
        driver = get_driver(driver_name)(creds)
        try:
            if not driver.login():
                raise HTTPException(400, "scrap account login failed")
            items = scrap_mod.scrape_listings(
                driver, data.urls, category=data.category,
                overrides=data.overrides, customize=data.customize,
                fallback_description=data.fallback_description,
                country_append=data.country_append,
                download_photos=False,
                log=lambda m: db.log(m, run_id=None,
                                     account_id=dict(accts[0])["id"]),
            )
            return {"ok": True, "items": items, "count": len(items)}
        finally:
            try:
                driver.close()
            except Exception:
                pass

    @app.post("/api/scrap/save")
    def api_scrap_save(data: ScrapSaveIn):
        try:
            pid = scrap_mod.save_as_product(db, data.name, data.items)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "product_id": pid}

    @app.get("/api/logs")
    def api_logs(run_id: int | None = None, limit: int = 200):
        return engine.recent_logs(run_id, limit)

    @app.get("/api/board")
    def api_board(group_id: int):
        """Main-screen status panels: Accounts / Pending / In Progress /
        Done Posting / Failed (+ exclude list and recent logs), driven by the
        group's most recent run."""
        accts = accounts_mod.list_accounts(db, group_id)
        board = {
            "accounts": [a["label"] for a in accts],
            "pending": [], "in_progress": [],
            "done_posting": [], "failed": [],
            "exclude": [], "run_id": None, "run_live": False, "logs": [],
        }
        runs = engine.list_runs(50)
        run = next((r for r in runs if r.get("group_id") == group_id), None)
        if run is None:
            return board
        full = engine.get_run(run["id"])
        board["run_id"] = run["id"]
        board["run_live"] = bool(full.get("live"))
        live = bool(full.get("live"))
        quotas = {int(k): v for k, v in
                  full.get("settings", {}).get("quotas", {}).items()}
        for a in full.get("per_account", []):
            label = a["label"]
            quota = quotas.get(a["id"])
            if a["failed"] > 0 and a["posted"] == 0:
                board["failed"].append(label)
            elif a["posted"] > 0 and (quota is None or a["posted"] >= quota
                                      or not live):
                board["done_posting"].append(label)
            elif live:
                if a["posted"] > 0 or a["failed"] > 0:
                    board["in_progress"].append(label)
                else:
                    board["pending"].append(label)
            elif a["posted"] == 0 and a["failed"] == 0:
                board["pending"].append(label)
            else:
                board["done_posting"].append(label)
        board["exclude"] = full.get("settings", {}).get("exclude_labels", [])
        board["logs"] = [
            f"[{l['created_at']}] {l['level']}: {l['message']}"
            for l in engine.recent_logs(run["id"], 50)
        ]
        return board

    # ------------------------------------------------- version / self-update
    @app.get("/api/version")
    def api_version():
        return {"version": updater_mod.CURRENT_VERSION,
                "browsers_ready": browsers_mod.browsers_ready()}

    @app.get("/api/update/check")
    def api_update_check():
        try:
            return {"ok": True, **updater_mod.check_for_update()}
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/update/download")
    def api_update_download():
        try:
            info = updater_mod.fetch_version_info()
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))
        exe_url = info.get("exe_url") or ""
        try:
            updater_mod.download_update(exe_url, info.get("exe_size"))
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.get("/api/update/progress")
    def api_update_progress():
        st = updater_mod.download_state()
        total = st["total"] or 0
        pct = round(st["downloaded"] / total * 100, 1) if total else 0
        return {"ok": True, **st, "percent": pct,
                "downloaded_mb": round(st["downloaded"] / 1e6, 1),
                "total_mb": round(total / 1e6, 1) if total else None}

    # ------------------------------------------------- browsers one-time setup
    @app.get("/api/browsers/status")
    def api_browsers_status():
        folder = browsers_mod.browsers_dir()
        return {"ok": True, "ready": browsers_mod.browsers_ready(folder),
                "path": str(folder)}

    @app.post("/api/browsers/download")
    def api_browsers_download():
        try:
            info = updater_mod.fetch_version_info()
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))
        url = info.get("browsers_url") or ""
        if not url:
            raise HTTPException(400, "No browsers download URL configured.")
        with updater_mod._download_lock:
            if updater_mod._download_state["state"] == "downloading":
                raise HTTPException(400, "A download is already in progress.")

        def _run():
            updater_mod._set_state(state="downloading", downloaded=0,
                                   total=None, message="")
            try:
                def _cb(done, total):
                    updater_mod._set_state(downloaded=done, total=total)
                browsers_mod.download_and_extract(url, progress_cb=_cb)
                # Point this process at the new folder for subsequent runs.
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(
                    browsers_mod.browsers_dir())
                updater_mod._set_state(state="done",
                                       message="Browsers installed. You can "
                                       "start using FMS now.")
            except Exception as exc:
                updater_mod._set_state(state="error", message=str(exc))

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    @app.post("/api/shutdown")
    def api_shutdown():
        """Quit the whole application (used by the Quit button in the .exe)."""
        def _quit():
            time.sleep(0.8)
            os._exit(0)

        threading.Thread(target=_quit, daemon=True).start()
        return {"ok": True}

    return app


# Default app instance: `uvicorn fms.app:app`
app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("fms.app:app", host="127.0.0.1", port=8000)
