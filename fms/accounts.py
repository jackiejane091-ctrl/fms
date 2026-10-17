"""Account Manager: groups + accounts CRUD, bulk import, 2FA codes.

Login model (user-confirmed): primary login is email/phone + password.
Cookie login is OPTIONAL; 2FA is OPTIONAL (only where enabled).
"""
from __future__ import annotations

import pyotp

from . import crypto
from .db import Database, utcnow


# ---------------------------------------------------------------- groups ---
LOC_CHANGE_MODES = ("do_not_change", "change_once", "change_everytime")


def _norm_locations(locations) -> list[str]:
    if locations is None:
        return []
    if isinstance(locations, str):
        # one per line, like the FBM Robo dialog (commas are kept — they are
        # part of real locations like "Manchester, United Kingdom")
        items = [l.strip() for l in locations.splitlines()]
    else:
        items = [str(l).strip() for l in locations]
    return [l for l in items if l]


def _norm_loc_mode(mode: str | None) -> str:
    mode = (mode or "do_not_change").strip()
    return mode if mode in LOC_CHANGE_MODES else "do_not_change"


def create_group(db: Database, name: str, locations=None,
                 apply_fingerprint: bool = True,
                 loc_change_mode: str | None = None,
                 loc_change_location: str | None = None) -> int:
    name = name.strip()
    if not name:
        raise ValueError("Group name cannot be empty.")
    import json as _json
    cur = db.execute(
        "INSERT INTO groups (name, created_at, locations, apply_fingerprint,"
        " loc_change_mode, loc_change_location) VALUES (?, ?, ?, ?, ?, ?)",
        (name, utcnow(), _json.dumps(_norm_locations(locations)),
         1 if apply_fingerprint else 0, _norm_loc_mode(loc_change_mode),
         (loc_change_location or "").strip()),
    )
    return cur.lastrowid


def get_group(db: Database, group_id: int) -> dict | None:
    row = db.fetchone("SELECT * FROM groups WHERE id = ?", (group_id,))
    if not row:
        return None
    return _group_dict(db, row)


def _group_dict(db: Database, row) -> dict:
    import json as _json
    d = dict(row)
    try:
        d["locations"] = _json.loads(d.get("locations") or "[]")
    except Exception:
        d["locations"] = []
    d["apply_fingerprint"] = bool(d.get("apply_fingerprint", 1))
    d["loc_change_mode"] = _norm_loc_mode(d.get("loc_change_mode"))
    d["loc_change_location"] = d.get("loc_change_location") or ""
    d["account_count"] = db.fetchone(
        "SELECT COUNT(*) AS c FROM accounts WHERE group_id = ?", (row["id"],)
    )["c"]
    d["product_count"] = group_product_count(db, row["id"])
    return d


def update_group(db: Database, group_id: int, name: str | None = None,
                 locations=None, apply_fingerprint: bool | None = None,
                 loc_change_mode: str | None = None,
                 loc_change_location: str | None = None) -> None:
    """Update any subset of a group's General Settings fields."""
    import json as _json
    sets, vals = [], []
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("Group name cannot be empty.")
        sets.append("name = ?")
        vals.append(name)
    if locations is not None:
        sets.append("locations = ?")
        vals.append(_json.dumps(_norm_locations(locations)))
    if apply_fingerprint is not None:
        sets.append("apply_fingerprint = ?")
        vals.append(1 if apply_fingerprint else 0)
    if loc_change_mode is not None:
        sets.append("loc_change_mode = ?")
        vals.append(_norm_loc_mode(loc_change_mode))
    if loc_change_location is not None:
        sets.append("loc_change_location = ?")
        vals.append(loc_change_location.strip())
    if sets:
        vals.append(group_id)
        db.execute(f"UPDATE groups SET {', '.join(sets)} WHERE id = ?", vals)


def list_groups(db: Database) -> list[dict]:
    rows = db.fetchall("SELECT * FROM groups ORDER BY name")
    return [_group_dict(db, r) for r in rows]


def group_product_count(db: Database, group_id: int) -> int:
    row = db.fetchone(
        "SELECT COUNT(*) AS c FROM group_products WHERE group_id = ?",
        (group_id,),
    )
    return row["c"] if row else 0


def list_group_products(db: Database, group_id: int) -> list[dict]:
    return [
        dict(r)
        for r in db.fetchall(
            """SELECT p.* FROM products p
               JOIN group_products gp ON gp.product_id = p.id
               WHERE gp.group_id = ? ORDER BY gp.position, p.id""",
            (group_id,),
        )
    ]


def set_group_products(db: Database, group_id: int, product_ids: list[int]) -> None:
    db.execute("DELETE FROM group_products WHERE group_id = ?", (group_id,))
    for pos, pid in enumerate(product_ids):
        db.execute(
            "INSERT INTO group_products (group_id, product_id, position)"
            " VALUES (?, ?, ?)",
            (group_id, pid, pos),
        )


def clone_group(db: Database, group_id: int, new_name: str | None = None) -> int:
    """Duplicate a group: new group '<name> (copy)' with copied accounts and
    product assignments. Proxies are cloned (not shared) so the copy is
    independent of the original."""
    src = db.fetchone("SELECT * FROM groups WHERE id = ?", (group_id,))
    if not src:
        raise ValueError(f"Unknown group id {group_id}")
    base = (new_name or f"{src['name']} (copy)").strip()
    name = base
    n = 2
    while db.fetchone("SELECT id FROM groups WHERE name = ?", (name,)):
        name = f"{base} {n}"
        n += 1
    import json as _json
    try:
        _locs = _json.loads(src["locations"] or "[]")
    except Exception:
        _locs = []
    new_gid = create_group(
        db, name,
        locations=_locs,
        apply_fingerprint=bool(src["apply_fingerprint"]),
        loc_change_mode=src["loc_change_mode"],
        loc_change_location=src["loc_change_location"],
    )
    proxy_map: dict[int, int] = {}
    for acc in db.fetchall("SELECT * FROM accounts WHERE group_id = ?",
                           (group_id,)):
        new_proxy_id = None
        if acc["proxy_id"]:
            if acc["proxy_id"] not in proxy_map:
                prow = db.fetchone("SELECT * FROM proxies WHERE id = ?",
                                   (acc["proxy_id"],))
                if prow:
                    cur = db.execute(
                        """INSERT INTO proxies
                           (host, port, username, password_enc, country,
                            working, last_check)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (prow["host"], prow["port"], prow["username"],
                         prow["password_enc"], prow["country"],
                         prow["working"], prow["last_check"]),
                    )
                    proxy_map[acc["proxy_id"]] = cur.lastrowid
            new_proxy_id = proxy_map.get(acc["proxy_id"])
        db.execute(
            """INSERT INTO accounts
               (group_id, label, password_enc, cookie_enc, twofa_secret_enc,
                proxy_id, browser, ix_profile_id, active, status,
                status_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_gid, acc["label"], acc["password_enc"], acc["cookie_enc"],
             acc["twofa_secret_enc"], new_proxy_id, acc["browser"],
             acc["ix_profile_id"], acc["active"], acc["status"],
             acc["status_reason"], utcnow()),
        )
    for gp in db.fetchall(
            "SELECT product_id, position FROM group_products WHERE group_id = ?",
            (group_id,)):
        db.execute(
            "INSERT INTO group_products (group_id, product_id, position)"
            " VALUES (?, ?, ?)",
            (new_gid, gp["product_id"], gp["position"]),
        )
    return new_gid


def rename_group(db: Database, group_id: int, name: str) -> None:
    db.execute("UPDATE groups SET name = ? WHERE id = ?", (name.strip(), group_id))


def delete_group(db: Database, group_id: int) -> None:
    db.execute("DELETE FROM groups WHERE id = ?", (group_id,))


# --------------------------------------------------------------- accounts ---
def add_account(
    db: Database,
    group_id: int,
    label: str,
    password: str | None = None,
    cookie: str | None = None,
    twofa_secret: str | None = None,
    proxy_id: int | None = None,
    browser: str = "chrome",
    ix_profile_id: str | None = None,
) -> int:
    label = label.strip()
    if not label:
        raise ValueError("Account label (email/phone) cannot be empty.")
    cur = db.execute(
        """INSERT INTO accounts
           (group_id, label, password_enc, cookie_enc, twofa_secret_enc,
            proxy_id, browser, ix_profile_id, active, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?)""",
        (
            group_id,
            label,
            crypto.encrypt_str(password),
            crypto.encrypt_str(cookie),
            crypto.encrypt_str(twofa_secret.strip().replace(" ", "") if twofa_secret else None),
            proxy_id,
            browser or "chrome",
            (ix_profile_id or "").strip() or None,
            utcnow(),
        ),
    )
    return cur.lastrowid


def parse_account_line(line: str) -> dict | None:
    """Parse one bulk-import line. Supported formats:

    - label;password                              (classic)
    - label;password;proxy_host:port              (classic)
    - label;password;proxy_host:port:proxy_user:proxy_pass
    - label;password;True|False[;Browser]         (FBM Robo v6.2.0.0 format:
                                                  True/False = active flag,
                                                  login may be email OR phone)
    - label;password;True|False;proxy_host:port   (FBM format + proxy)
    - label|password|twofa_secret                (pipe format)
    - label|password|twofa_secret|cookie
    Lines starting with # are ignored. A line that is not blank but has no
    usable login raises ValueError (reported per line by the caller).
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "|" in line and ";" not in line:
        parts = [p.strip() for p in line.split("|")]
        if not parts[0]:
            raise ValueError("empty login")
        return {
            "label": parts[0],
            "password": parts[1] if len(parts) > 1 else None,
            "twofa_secret": parts[2] if len(parts) > 2 else None,
            "cookie": parts[3] if len(parts) > 3 else None,
        }
    parts = [p.strip() for p in line.split(";")]
    if not parts[0]:
        raise ValueError("empty login")
    if len(parts) < 2 or not parts[1]:
        raise ValueError(f"bad format, need 'login;password[;...]': {line!r}")
    out: dict = {
        "label": parts[0],
        "password": parts[1],
        "active": 1,
        "browser": "chrome",
    }
    rest = parts[2:]
    if rest and rest[0].lower() in ("true", "false"):
        # FBM Robo format: login;password;True|False[;Browser|proxy]
        out["active"] = 1 if rest[0].lower() == "true" else 0
        rest = rest[1:]
        if rest and rest[0]:
            if ":" in rest[0]:
                out["proxy"] = rest[0]
            else:
                out["browser"] = rest[0]
    elif rest and rest[0]:
        # classic format: 3rd field is a proxy
        out["proxy"] = rest[0]
    return out


def bulk_add_accounts(db: Database, group_id: int, text: str,
                      validate_proxies: bool = True) -> tuple[int, list[str]]:
    """Add many accounts from pasted text. Returns (added_count, errors)."""
    added, errors = 0, []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        try:
            parsed = parse_account_line(raw)
            if parsed is None:
                continue
            proxy_id = None
            if parsed.get("proxy"):
                from . import proxies as _proxies

                proxy_id = _proxies.add_proxy(db, parsed["proxy"],
                                              validate=validate_proxies)
            aid = add_account(
                db,
                group_id,
                label=parsed["label"],
                password=parsed.get("password"),
                cookie=parsed.get("cookie"),
                twofa_secret=parsed.get("twofa_secret"),
                proxy_id=proxy_id,
                browser=parsed.get("browser", "chrome"),
            )
            if parsed.get("active") == 0:
                # False in the FBM format = stays inactive, never opened.
                db.execute("UPDATE accounts SET active = 0 WHERE id = ?",
                           (aid,))
            added += 1
        except Exception as exc:  # keep going; report the bad line
            errors.append(f"line {lineno}: {exc}")
    return added, errors


def import_accounts_file(db: Database, group_id: int, path: str) -> tuple[int, list[str]]:
    with open(path, encoding="utf-8-sig") as fh:
        return bulk_add_accounts(db, group_id, fh.read())


def list_accounts(db: Database, group_id: int | None = None) -> list[dict]:
    if group_id is None:
        rows = db.fetchall("SELECT * FROM accounts ORDER BY id")
    else:
        rows = db.fetchall(
            "SELECT * FROM accounts WHERE group_id = ? ORDER BY id", (group_id,)
        )
    return [dict(r) for r in rows]


def get_account(db: Database, account_id: int) -> dict | None:
    row = db.fetchone("SELECT * FROM accounts WHERE id = ?", (account_id,))
    return dict(row) if row else None


def update_account(db: Database, account_id: int, **fields) -> None:
    """Update label/browser/active/cookie/twofa_secret/password/proxy_id/group_id.

    Secret fields accept plaintext (or None to clear); they are encrypted here.
    """
    allowed = {"label", "browser", "active", "group_id", "proxy_id",
               "status", "status_reason", "ix_profile_id"}
    secret_fields = {"password", "cookie", "twofa_secret"}
    sets, params = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key} = ?")
            params.append(value)
        elif key in secret_fields:
            col = {"password": "password_enc", "cookie": "cookie_enc",
                   "twofa_secret": "twofa_secret_enc"}[key]
            sets.append(f"{col} = ?")
            params.append(crypto.encrypt_str(value))
        else:
            raise ValueError(f"Unknown account field: {key}")
    if not sets:
        return
    params.append(account_id)
    db.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", tuple(params))


def toggle_active(db: Database, account_id: int) -> int:
    row = db.fetchone("SELECT active FROM accounts WHERE id = ?", (account_id,))
    new_val = 0 if row["active"] else 1
    db.execute("UPDATE accounts SET active = ? WHERE id = ?", (new_val, account_id))
    return new_val


def delete_account(db: Database, account_id: int) -> None:
    db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))


def delete_accounts(db: Database, account_ids: list[int]) -> int:
    if not account_ids:
        return 0
    placeholders = ",".join("?" for _ in account_ids)
    cur = db.execute(
        f"DELETE FROM accounts WHERE id IN ({placeholders})", tuple(account_ids)
    )
    return cur.rowcount


# --------------------------------------------------- proxy tools (selected) ---
def assign_proxies_to_accounts(
    db: Database, account_ids: list[int], proxy_texts: list[str],
    validate: bool = False,
) -> dict[int, int]:
    """Create proxies and assign them to the given accounts in order, cycling
    if there are fewer proxies than accounts. Returns {account_id: proxy_id}."""
    from . import proxies as _proxies

    clean = [t.strip() for t in proxy_texts if t.strip()]
    if not clean:
        raise ValueError("No proxies provided.")
    if not account_ids:
        raise ValueError("No accounts selected.")
    proxy_ids = [_proxies.add_proxy(db, t, validate=validate) for t in clean]
    mapping: dict[int, int] = {}
    for i, aid in enumerate(account_ids):
        pid = proxy_ids[i % len(proxy_ids)]
        db.execute("UPDATE accounts SET proxy_id = ? WHERE id = ?", (pid, aid))
        mapping[aid] = pid
    return mapping


def remove_proxies_from_accounts(db: Database, account_ids: list[int]) -> int:
    """Clear the proxy on each selected account. Returns accounts updated."""
    if not account_ids:
        return 0
    placeholders = ",".join("?" for _ in account_ids)
    cur = db.execute(
        f"UPDATE accounts SET proxy_id = NULL WHERE id IN ({placeholders})",
        tuple(account_ids),
    )
    return cur.rowcount


def check_account_proxies(db: Database, account_ids: list[int]) -> list[dict]:
    """Test each selected account's proxy and record the result. Returns a
    per-account report {account_id, label, proxy, working, message}."""
    from . import proxies as _proxies

    results = []
    for aid in account_ids:
        acc = db.fetchone(
            "SELECT a.id, a.label, a.proxy_id, p.host, p.port "
            "FROM accounts a LEFT JOIN proxies p ON p.id = a.proxy_id "
            "WHERE a.id = ?",
            (aid,),
        )
        if not acc:
            continue
        if not acc["proxy_id"]:
            results.append({"account_id": aid, "label": acc["label"],
                            "proxy": None, "working": None,
                            "message": "no proxy"})
            continue
        ok, msg = _proxies.check_proxy_status(db, acc["proxy_id"])
        results.append({
            "account_id": aid, "label": acc["label"],
            "proxy": f"{acc['host']}:{acc['port']}",
            "working": ok, "message": msg,
        })
    return results


def remove_not_working_proxies(db: Database,
                               account_ids: list[int]) -> dict[str, int]:
    """Detach proxies whose last check failed from the selected accounts and
    delete the dead proxy rows. Returns {accounts_cleared, proxies_deleted}."""
    if not account_ids:
        return {"accounts_cleared": 0, "proxies_deleted": 0}
    placeholders = ",".join("?" for _ in account_ids)
    dead = db.fetchall(
        f"""SELECT a.id AS account_id, a.proxy_id FROM accounts a
            JOIN proxies p ON p.id = a.proxy_id
            WHERE a.id IN ({placeholders}) AND p.working = 0""",
        tuple(account_ids),
    )
    dead_pids = sorted({r["proxy_id"] for r in dead})
    if not dead_pids:
        return {"accounts_cleared": 0, "proxies_deleted": 0}
    ph = ",".join("?" for _ in dead_pids)
    cur = db.execute(
        f"UPDATE accounts SET proxy_id = NULL WHERE proxy_id IN ({ph})",
        tuple(dead_pids),
    )
    cleared = cur.rowcount
    cur = db.execute(f"DELETE FROM proxies WHERE id IN ({ph})",
                     tuple(dead_pids))
    return {"accounts_cleared": cleared, "proxies_deleted": cur.rowcount}


def active_accounts_for_run(db: Database, group_id: int) -> list[dict]:
    """Accounts eligible for a listing run: all active accounts.

    v0.2.39: no more locked/banned exclusions — the user asked for it
    simple. Every Start tries every active account; failures land in
    the Failed panel with their reason.
    """
    return db.fetchall(
        """SELECT * FROM accounts
           WHERE group_id = ? AND active = 1
           ORDER BY id""",
        (group_id,),
    )


def set_status(db: Database, account_id: int, status: str, reason: str = "") -> None:
    db.execute(
        "UPDATE accounts SET status = ?, status_reason = ? WHERE id = ?",
        (status, reason, account_id),
    )


def set_ix_profile_id(db: Database, account_id: int, profile_id: str) -> None:
    """Persist an auto-created IXBrowser profile id back to the account."""
    db.execute(
        "UPDATE accounts SET ix_profile_id = ? WHERE id = ?",
        (profile_id, account_id),
    )


def get_twofa_code(db: Database, account_id: int) -> str | None:
    """Generate the current TOTP code for an account (None if no secret stored)."""
    row = db.fetchone(
        "SELECT twofa_secret_enc FROM accounts WHERE id = ?", (account_id,)
    )
    if not row:
        return None
    secret = crypto.decrypt_str(row["twofa_secret_enc"])
    if not secret:
        return None
    return pyotp.TOTP(secret).now()


def account_credentials(db: Database, account: dict) -> dict:
    """Return a dict with DECRYPTED secrets for automation use (never log these)."""
    proxy = None
    if account.get("proxy_id"):
        prow = db.fetchone("SELECT * FROM proxies WHERE id = ?", (account["proxy_id"],))
        if prow:
            proxy = dict(prow)
            proxy["password"] = crypto.decrypt_str(prow["password_enc"])
    return {
        "id": account["id"],
        "label": account["label"],
        "password": crypto.decrypt_str(account["password_enc"]),
        "cookie": crypto.decrypt_str(account["cookie_enc"]),
        "twofa_secret": crypto.decrypt_str(account["twofa_secret_enc"]),
        "browser": account.get("browser") or "chrome",
        "ix_profile_id": account.get("ix_profile_id"),
        "proxy": proxy,
    }
