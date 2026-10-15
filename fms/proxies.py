"""Proxy Manager: OPTIONAL per-account proxies with validation.

Proxies are optional in FMS. When used, each account may have its own proxy
(host/port + optional username/password). Validation does a real request
through the proxy before it is trusted.
"""
from __future__ import annotations

import socket
import urllib.request

from . import crypto
from .db import Database, utcnow

# Lightweight endpoint that returns HTTP 204 with (almost) no body.
PROBE_URL = "http://www.gstatic.com/generate_204"
PROBE_TIMEOUT = 10


def parse_proxy(text: str) -> dict:
    """Parse 'host:port' or 'host:port:user:pass' (user/pass may contain ':')."""
    text = text.strip()
    if text.startswith("http://"):
        text = text[len("http://"):]
    elif text.startswith("https://"):
        text = text[len("https://"):]
    parts = text.split(":")
    if len(parts) < 2:
        raise ValueError(f"Bad proxy format (need host:port): {text!r}")
    host = parts[0].strip()
    try:
        port = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"Bad proxy port: {parts[1]!r}") from exc
    username = parts[2] if len(parts) > 2 else None
    password = ":".join(parts[3:]) if len(parts) > 3 else None
    if not host or not (1 <= port <= 65535):
        raise ValueError(f"Bad proxy host/port: {text!r}")
    return {"host": host, "port": port, "username": username, "password": password}


def validate_proxy(
    host: str,
    port: int,
    username: str | None = None,
    password: str | None = None,
    timeout: int = PROBE_TIMEOUT,
) -> tuple[bool, str]:
    """Return (working, message). Performs a real HTTP request via the proxy."""
    if username:
        auth = f"{username}:{password or ''}@"
    else:
        auth = ""
    proxy_url = f"http://{auth}{host}:{port}"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )
    req = urllib.request.Request(PROBE_URL, headers={"User-Agent": "FMS/1.0"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            if resp.status in (200, 204):
                return True, f"OK (HTTP {resp.status})"
            return False, f"Unexpected HTTP {resp.status}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def quick_port_check(host: str, port: int, timeout: int = 5) -> bool:
    """Fast TCP connect check (does not prove the proxy actually proxies)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def add_proxy(
    db: Database,
    proxy_text: str,
    country: str | None = None,
    validate: bool = True,
) -> int:
    parsed = parse_proxy(proxy_text)
    working: bool | None = None
    if validate:
        ok, msg = validate_proxy(
            parsed["host"], parsed["port"], parsed["username"], parsed["password"]
        )
        working = 1 if ok else 0
        if not ok:
            raise ValueError(f"Proxy validation failed: {msg}")
    cur = db.execute(
        """INSERT INTO proxies (host, port, username, password_enc, country,
                                working, last_check)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            parsed["host"],
            parsed["port"],
            parsed["username"],
            crypto.encrypt_str(parsed["password"]),
            country,
            working,
            utcnow() if validate else None,
        ),
    )
    return cur.lastrowid


def assign_proxies_round_robin(
    db: Database, group_id: int, proxy_texts: list[str], validate: bool = False
) -> dict[int, int]:
    """Create proxies and deal them round-robin across the group's accounts.

    Returns {account_id: proxy_id}. Validation is off by default here because
    bulk validation is slow; use check_all() afterwards.
    """
    proxy_ids = [add_proxy(db, t, validate=validate) for t in proxy_texts if t.strip()]
    if not proxy_ids:
        raise ValueError("No proxies provided.")
    accounts = db.fetchall(
        "SELECT id FROM accounts WHERE group_id = ? ORDER BY id", (group_id,)
    )
    mapping: dict[int, int] = {}
    for i, acc in enumerate(accounts):
        pid = proxy_ids[i % len(proxy_ids)]
        db.execute("UPDATE accounts SET proxy_id = ? WHERE id = ?", (pid, acc["id"]))
        mapping[acc["id"]] = pid
    return mapping


def check_proxy_status(db: Database, proxy_id: int) -> tuple[bool, str]:
    row = db.fetchone("SELECT * FROM proxies WHERE id = ?", (proxy_id,))
    if not row:
        raise ValueError(f"Unknown proxy id {proxy_id}")
    password = crypto.decrypt_str(row["password_enc"])
    ok, msg = validate_proxy(row["host"], row["port"], row["username"], password)
    db.execute(
        "UPDATE proxies SET working = ?, last_check = ? WHERE id = ?",
        (1 if ok else 0, utcnow(), proxy_id),
    )
    return ok, msg


def check_all_proxies(db: Database) -> list[dict]:
    results = []
    for row in db.fetchall("SELECT id FROM proxies"):
        ok, msg = check_proxy_status(db, row["id"])
        results.append({"proxy_id": row["id"], "working": ok, "message": msg})
    return results


def remove_not_working(db: Database) -> int:
    cur = db.execute("DELETE FROM proxies WHERE working = 0")
    return cur.rowcount


def remove_all_proxies(db: Database) -> int:
    cur = db.execute("DELETE FROM proxies")
    return cur.rowcount
