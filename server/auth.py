"""Lightweight account authentication for Z.R.I.C local multiplayer."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import fastapi
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field


auth_router = APIRouter(tags=["账号"])

_db_file = ""
_PBKDF2_ITERATIONS = 210_000
_SESSION_DAYS = 30


def configure_auth(db_file: str) -> None:
    global _db_file
    _db_file = db_file


def get_db_connection() -> sqlite3.Connection:
    if not _db_file:
        raise RuntimeError("auth module is not configured")
    conn = sqlite3.connect(_db_file, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def safe_db():
    conn = get_db_connection()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_auth_tables() -> None:
    with safe_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_accounts (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                username       TEXT NOT NULL UNIQUE,
                username_key   TEXT NOT NULL UNIQUE,
                display_name   TEXT NOT NULL DEFAULT '',
                password_salt  TEXT NOT NULL,
                password_hash  TEXT NOT NULL,
                created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                last_login_at  TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_sessions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id    INTEGER NOT NULL,
                token_hash    TEXT NOT NULL UNIQUE,
                created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                last_seen_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                expires_at    TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(account_id) REFERENCES auth_accounts(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_account ON auth_sessions(account_id)")
        conn.commit()


class AuthRequest(BaseModel):
    username: str = Field(default="", max_length=60)
    password: str = Field(default="", max_length=200)
    display_name: str = Field(default="", max_length=40)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _expires_at() -> str:
    return (datetime.now() + timedelta(days=_SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_username(value: Any) -> str:
    username = re.sub(r"\s+", "", str(value or "").strip())[:60]
    if len(username) < 3:
        raise fastapi.HTTPException(status_code=400, detail="账号至少需要 3 个字符")
    if not re.match(r"^[A-Za-z0-9_.@\-\u4e00-\u9fff]+$", username):
        raise fastapi.HTTPException(status_code=400, detail="账号只能包含中英文、数字、下划线、点、@ 或短横线")
    return username


def _username_key(username: str) -> str:
    return username.casefold()


def _normalize_display_name(value: Any, fallback: str) -> str:
    name = re.sub(r"\s+", " ", str(value or "").strip())[:40]
    return name or fallback[:40] or "玩家"


def _validate_password(password: str) -> str:
    password = str(password or "")
    if len(password) < 6:
        raise fastapi.HTTPException(status_code=400, detail="密码至少需要 6 个字符")
    if len(password) > 200:
        raise fastapi.HTTPException(status_code=400, detail="密码过长")
    return password


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        _PBKDF2_ITERATIONS,
    ).hex()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def account_player_id(account: sqlite3.Row | dict[str, Any] | None) -> str:
    if not account:
        return ""
    return f"account:{int(dict(account)['id'])}"


def _serialize_account(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": int(data["id"]),
        "username": data.get("username", ""),
        "display_name": data.get("display_name", "") or data.get("username", "玩家"),
        "player_id": account_player_id(data),
    }


def _issue_session(conn: sqlite3.Connection, account_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        """
        INSERT INTO auth_sessions (account_id, token_hash, created_at, last_seen_at, expires_at)
        VALUES (?,?,?,?,?)
        """,
        (account_id, _hash_token(token), _now(), _now(), _expires_at()),
    )
    conn.execute("UPDATE auth_accounts SET last_login_at=?, updated_at=? WHERE id=?", (_now(), _now(), account_id))
    return token


def get_account_by_token(token: str | None) -> dict[str, Any] | None:
    token = str(token or "").strip()
    if not token:
        return None
    token_hash = _hash_token(token)
    with safe_db() as conn:
        row = conn.execute(
            """
            SELECT a.*
            FROM auth_sessions s
            JOIN auth_accounts a ON a.id=s.account_id
            WHERE s.token_hash=?
              AND (s.expires_at='' OR s.expires_at >= datetime('now','localtime'))
            LIMIT 1
            """,
            (token_hash,),
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE auth_sessions SET last_seen_at=? WHERE token_hash=?", (_now(), token_hash))
        conn.commit()
        return _serialize_account(row)


def auth_token_from_request(request: Request) -> str:
    return (request.headers.get("X-Auth-Token") or "").strip()


def account_from_request(request: Request) -> dict[str, Any] | None:
    return get_account_by_token(auth_token_from_request(request))


def require_account_from_request(request: Request) -> dict[str, Any]:
    account = account_from_request(request)
    if not account:
        raise fastapi.HTTPException(status_code=401, detail="请先登录账号")
    return account


@auth_router.post("/api/auth/register")
def register_account(req: AuthRequest):
    username = _normalize_username(req.username)
    password = _validate_password(req.password)
    display_name = _normalize_display_name(req.display_name, username)
    salt = secrets.token_hex(16)
    password_hash = _hash_password(password, salt)
    with safe_db() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO auth_accounts
                (username, username_key, display_name, password_salt, password_hash, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (username, _username_key(username), display_name, salt, password_hash, _now(), _now()),
            )
            token = _issue_session(conn, int(cur.lastrowid))
            conn.commit()
        except sqlite3.IntegrityError:
            raise fastapi.HTTPException(status_code=409, detail="账号已存在") from None
        row = conn.execute("SELECT * FROM auth_accounts WHERE id=?", (cur.lastrowid,)).fetchone()
    return {"status": "success", "token": token, "account": _serialize_account(row)}


@auth_router.post("/api/auth/login")
def login_account(req: AuthRequest):
    username = _normalize_username(req.username)
    password = _validate_password(req.password)
    with safe_db() as conn:
        row = conn.execute("SELECT * FROM auth_accounts WHERE username_key=?", (_username_key(username),)).fetchone()
        if not row or not hmac.compare_digest(_hash_password(password, row["password_salt"]), row["password_hash"]):
            raise fastapi.HTTPException(status_code=401, detail="账号或密码不正确")
        token = _issue_session(conn, int(row["id"]))
        conn.commit()
    return {"status": "success", "token": token, "account": _serialize_account(row)}


@auth_router.get("/api/auth/me")
def get_current_account(request: Request):
    account = require_account_from_request(request)
    return {"status": "success", "account": account}


@auth_router.post("/api/auth/logout")
def logout_account(request: Request):
    token = auth_token_from_request(request)
    if token:
        with safe_db() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (_hash_token(token),))
            conn.commit()
    return {"status": "success"}
