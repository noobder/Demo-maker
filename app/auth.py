"""Accounts and sessions: scrypt-hashed passwords in SQLite, random session tokens in an HttpOnly cookie."""
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from pathlib import Path

from fastapi import HTTPException, Request

DB_PATH = Path(__file__).resolve().parent.parent / "workspace" / "users.db"
COOKIE = "dm_session"
SESSION_SECONDS = 30 * 24 * 3600
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_FAILS, FAIL_WINDOW = 5, 600

_fails: dict[str, list[float]] = {}


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
                salt BLOB NOT NULL, pw_hash BLOB NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires REAL NOT NULL);
            """
        )


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _public(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "name": row["name"], "email": row["email"]}


def register(name: str, email: str, password: str) -> dict:
    name, email = name.strip(), email.strip().lower()
    if not 1 <= len(name) <= 60:
        raise HTTPException(400, "Enter your name (up to 60 characters).")
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise HTTPException(400, "Enter a valid email address.")
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    if len(password) > 200:
        raise HTTPException(400, "Password is too long.")
    salt = secrets.token_bytes(16)
    try:
        with _db() as conn:
            cur = conn.execute(
                "INSERT INTO users (name, email, salt, pw_hash, created) VALUES (?,?,?,?,?)",
                (name, email, salt, _hash(password, salt), time.time()))
            row = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    except sqlite3.IntegrityError:
        raise HTTPException(409, "An account with this email already exists. Try signing in.")
    return _public(row)


def login(email: str, password: str, client: str) -> dict:
    email = email.strip().lower()
    key = f"{client}|{email}"
    now = time.time()
    recent = [t for t in _fails.get(key, []) if now - t < FAIL_WINDOW]
    if len(recent) >= MAX_FAILS:
        wait = int(FAIL_WINDOW - (now - recent[0]))
        raise HTTPException(429, f"Too many failed attempts. Try again in {max(1, wait // 60 + 1)} minute(s).")
    with _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    # Hash even for unknown emails so response time does not reveal which accounts exist.
    expected = row["pw_hash"] if row else b"\0" * 32
    given = _hash(password, row["salt"] if row else b"\0" * 16)
    if not row or not hmac.compare_digest(expected, given):
        _fails[key] = recent + [now]
        raise HTTPException(401, "Incorrect email or password.")
    _fails.pop(key, None)
    return _public(row)


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with _db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        conn.execute("INSERT INTO sessions (token_hash, user_id, expires) VALUES (?,?,?)",
                     (hashlib.sha256(token.encode()).hexdigest(), user_id, time.time() + SESSION_SECONDS))
    return token


def end_session(token: str | None) -> None:
    if token:
        with _db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))


def user_from_request(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=? AND s.expires > ?",
            (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    return _public(row) if row else None


def require_user(request: Request) -> dict:
    user = user_from_request(request)
    if not user:
        raise HTTPException(401, "Please sign in to continue.")
    return user
