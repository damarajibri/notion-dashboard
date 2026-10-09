"""SQLite storage layer for the Notion dashboard.

Design:
- We store the *raw* Notion row JSON for each page. This lets the existing
  extract_*() functions in app.py keep working unchanged, since they operate
  on the raw `properties` structure returned by the Notion API.
- One table per Notion database, keyed by the Notion page id.
- A `sync_meta` table tracks the last successful sync time per database so
  incremental syncs can filter by `last_edited_time`.
- WAL mode is enabled so the web app can read while sync writes.
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time

DB_PATH = os.environ.get(
    "DASHBOARD_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.db"),
)

# Logical name -> table name. Keeps table names stable and readable.
TABLES = {
    "tasks": "tasks",
    "projects": "projects",
    "personel": "personel",
    "spk": "spk",
    "monthly_perf": "monthly_perf",
}


def get_conn():
    """Return a SQLite connection with WAL enabled for concurrent read/write."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db():
    """Create tables if they do not exist."""
    conn = get_conn()
    try:
        for table in TABLES.values():
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    page_id        TEXT PRIMARY KEY,
                    last_edited    TEXT,
                    raw            TEXT NOT NULL
                );
                """
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_meta (
                db_key           TEXT PRIMARY KEY,
                last_sync_time   TEXT,
                last_full_sync   TEXT
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL,
                full_name     TEXT,
                role          TEXT NOT NULL DEFAULT 'user',
                password_hash TEXT NOT NULL,
                is_active     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT,
                updated_at    TEXT,
                last_login    TEXT
            );
            """
        )
        # Migration: add last_login to pre-existing user tables that lack it.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "last_login" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_login TEXT;")
        # Activity log: every user action in the app (login, logout, page views,
        # user CRUD, data edits, sync, ...). username is denormalised so the log
        # stays readable even after the user is deleted.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT NOT NULL,
                user_id    INTEGER,
                username   TEXT,
                action     TEXT NOT NULL,
                target     TEXT,
                detail     TEXT,
                changes    TEXT,
                ip         TEXT,
                user_agent TEXT
            );
            """
        )
        # Migration: add `changes` to pre-existing activity_log tables.
        acols = {row["name"] for row in conn.execute("PRAGMA table_info(activity_log)")}
        if "changes" not in acols:
            conn.execute("ALTER TABLE activity_log ADD COLUMN changes TEXT;")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_activity_user ON activity_log(user_id, ts);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log(ts);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_activity_action ON activity_log(action);"
        )
        conn.commit()
    finally:
        conn.close()
    # Seed the built-in root account after tables are guaranteed to exist.
    seed_root_user()


def upsert_rows(db_key, rows):
    """Insert or replace raw Notion rows for a given logical database.

    `rows` is the list of raw page objects from the Notion API.
    """
    table = TABLES[db_key]
    conn = get_conn()
    try:
        conn.executemany(
            f"""
            INSERT INTO {table} (page_id, last_edited, raw)
            VALUES (?, ?, ?)
            ON CONFLICT(page_id) DO UPDATE SET
                last_edited=excluded.last_edited,
                raw=excluded.raw;
            """,
            [
                (r["id"], r.get("last_edited_time", ""), json.dumps(r))
                for r in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()


def delete_missing(db_key, present_page_ids):
    """Remove rows no longer present in Notion (used after a full sync).

    Returns the number of rows deleted.
    """
    table = TABLES[db_key]
    conn = get_conn()
    try:
        existing = {row["page_id"] for row in conn.execute(f"SELECT page_id FROM {table}")}
        stale = existing - set(present_page_ids)
        if stale:
            conn.executemany(
                f"DELETE FROM {table} WHERE page_id = ?",
                [(pid,) for pid in stale],
            )
            conn.commit()
        return len(stale)
    finally:
        conn.close()


def load_rows(db_key):
    """Return all raw Notion row objects for a logical database.

    Resilient to a not-yet-created table (returns [] instead of raising),
    so the web app can serve empty data before the first sync/bootstrap.
    """
    table = TABLES[db_key]
    conn = get_conn()
    try:
        try:
            cur = conn.execute(f"SELECT raw FROM {table}")
        except sqlite3.OperationalError:
            # Table does not exist yet (DB not bootstrapped).
            return []
        return [json.loads(row["raw"]) for row in cur]
    finally:
        conn.close()


def get_sync_meta(db_key):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT last_sync_time, last_full_sync FROM sync_meta WHERE db_key = ?",
            (db_key,),
        ).fetchone()
        if row:
            return {"last_sync_time": row["last_sync_time"], "last_full_sync": row["last_full_sync"]}
        return {"last_sync_time": None, "last_full_sync": None}
    finally:
        conn.close()


def set_sync_meta(db_key, last_sync_time=None, last_full_sync=None):
    current = get_sync_meta(db_key)
    last_sync_time = last_sync_time if last_sync_time is not None else current["last_sync_time"]
    last_full_sync = last_full_sync if last_full_sync is not None else current["last_full_sync"]
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO sync_meta (db_key, last_sync_time, last_full_sync)
            VALUES (?, ?, ?)
            ON CONFLICT(db_key) DO UPDATE SET
                last_sync_time=excluded.last_sync_time,
                last_full_sync=excluded.last_full_sync;
            """,
            (db_key, last_sync_time, last_full_sync),
        )
        conn.commit()
    finally:
        conn.close()


def oldest_sync_time():
    """Return the oldest last_sync_time across all databases (ISO string), or None.

    Used by the web app to decide whether an on-demand incremental sync is due.
    """
    conn = get_conn()
    try:
        try:
            rows = conn.execute("SELECT last_sync_time FROM sync_meta").fetchall()
        except sqlite3.OperationalError:
            # sync_meta table not created yet -> treat as due.
            return None
        times = [r["last_sync_time"] for r in rows if r["last_sync_time"]]
        if not times or len(times) < len(TABLES):
            # If any database has never synced, treat as due.
            return None
        return min(times)
    finally:
        conn.close()


def oldest_full_sync_time():
    """Return the oldest last_full_sync across all databases (ISO string), or None.

    Used by the web app to decide whether an on-demand FULL sync is due. A full
    sync reconciles deletions (rows removed directly in Notion), which the
    incremental sync cannot detect. Returns None (treated as "due") if any
    database has never had a full sync recorded.
    """
    conn = get_conn()
    try:
        try:
            rows = conn.execute("SELECT last_full_sync FROM sync_meta").fetchall()
        except sqlite3.OperationalError:
            # sync_meta table not created yet -> treat as due.
            return None
        times = [r["last_full_sync"] for r in rows if r["last_full_sync"]]
        if not times or len(times) < len(TABLES):
            # If any database has never had a full sync, treat as due.
            return None
        return min(times)
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# USER / AUTH LAYER
# ─────────────────────────────────────────────────────────────────────────────
# Passwords are stored using PBKDF2-HMAC-SHA256 with a per-user random salt and
# a high iteration count. This is a standard, well-vetted password hashing
# scheme available in the Python stdlib (no extra dependency), which works on
# PythonAnywhere out of the box. The stored format is:
#
#     pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
#
# Verification is done with hmac.compare_digest() to avoid timing attacks.

# The built-in, undeletable root account. Credentials seeded on first init.
ROOT_USERNAME = "exo"
_ROOT_DEFAULT_PASSWORD = "ns0/3n0/3x0"

PBKDF2_ITERATIONS = 240_000
_PBKDF2_ALGO = "sha256"


def hash_password(password, iterations=PBKDF2_ITERATIONS):
    """Hash a plaintext password with PBKDF2-HMAC-SHA256 + random salt.

    Returns a self-describing string: pbkdf2_sha256$<iters>$<salt>$<hash>.
    """
    if not isinstance(password, str) or password == "":
        raise ValueError("password must be a non-empty string")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_{_PBKDF2_ALGO}${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    """Verify a plaintext password against a stored PBKDF2 hash string.

    Uses a constant-time comparison. Returns False on any malformed input.
    """
    if not password or not stored:
        return False
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
        if algo != f"pbkdf2_{_PBKDF2_ALGO}":
            return False
        iterations = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _row_to_user(row):
    """Convert a sqlite Row to a plain dict WITHOUT the password hash."""
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "full_name": row["full_name"],
        "role": row["role"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login": row["last_login"] if "last_login" in row.keys() else None,
    }


def seed_root_user():
    """Create the built-in root account if it does not yet exist.

    Idempotent: only inserts when the username is missing, so a changed root
    password is never overwritten on restart.
    """
    conn = get_conn()
    try:
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?", (ROOT_USERNAME,)
        ).fetchone()
        if existing:
            return
        now = _now_iso()
        conn.execute(
            """
            INSERT INTO users (username, full_name, role, password_hash,
                               is_active, created_at, updated_at)
            VALUES (?, ?, 'root', ?, 1, ?, ?);
            """,
            (
                ROOT_USERNAME,
                "Root Administrator",
                hash_password(_ROOT_DEFAULT_PASSWORD),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_by_username(username):
    """Return the full user row (including password_hash) or None."""
    if not username:
        return None
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE username = ?", (username.strip(),)
        ).fetchone()
    finally:
        conn.close()


def authenticate(username, password):
    """Return a safe user dict if credentials are valid & active, else None."""
    row = get_user_by_username(username)
    if not row or not row["is_active"]:
        return None
    if verify_password(password, row["password_hash"]):
        return _row_to_user(row)
    return None


def touch_last_login(user_id):
    """Record the current UTC time as the user's last successful login."""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (_now_iso(), user_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_users():
    """Return all users (without password hashes), ordered by id."""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [_row_to_user(r) for r in rows]
    finally:
        conn.close()


def get_user(user_id):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_user(row)
    finally:
        conn.close()


class UserError(Exception):
    """Raised for user-management validation errors (duplicate, not found...)."""


VALID_ROLES = {"root", "admin", "user"}


def create_user(username, password, full_name="", role="user", is_active=True):
    """Create a new user. Raises UserError on validation failure."""
    username = (username or "").strip()
    if not username:
        raise UserError("Username wajib diisi.")
    if not password:
        raise UserError("Password wajib diisi.")
    if role not in VALID_ROLES:
        raise UserError(f"Role tidak valid: {role}")
    if role == "root":
        raise UserError("Role 'root' tidak dapat diberikan ke user baru.")
    if get_user_by_username(username):
        raise UserError(f"Username '{username}' sudah dipakai.")

    now = _now_iso()
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO users (username, full_name, role, password_hash,
                               is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                username,
                (full_name or "").strip(),
                role,
                hash_password(password),
                1 if is_active else 0,
                now,
                now,
            ),
        )
        conn.commit()
        return get_user(cur.lastrowid)
    finally:
        conn.close()


def update_user(user_id, full_name=None, role=None, password=None, is_active=None):
    """Update an existing user. Only provided fields change.

    The built-in root account cannot be demoted or deactivated.
    Raises UserError on validation failure.
    """
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise UserError("User tidak ditemukan.")
        is_root = row["username"] == ROOT_USERNAME

        sets, params = [], []
        if full_name is not None:
            sets.append("full_name = ?")
            params.append(full_name.strip())
        if role is not None:
            if role not in VALID_ROLES:
                raise UserError(f"Role tidak valid: {role}")
            if is_root and role != "root":
                raise UserError("Role akun root tidak dapat diubah.")
            if not is_root and role == "root":
                raise UserError("Role 'root' tidak dapat diberikan.")
            sets.append("role = ?")
            params.append(role)
        if is_active is not None:
            if is_root and not is_active:
                raise UserError("Akun root tidak dapat dinonaktifkan.")
            sets.append("is_active = ?")
            params.append(1 if is_active else 0)
        if password:
            sets.append("password_hash = ?")
            params.append(hash_password(password))

        if not sets:
            return get_user(user_id)

        sets.append("updated_at = ?")
        params.append(_now_iso())
        params.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
        return get_user(user_id)
    finally:
        conn.close()


def delete_user(user_id):
    """Delete a user by id. The built-in root account cannot be deleted."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise UserError("User tidak ditemukan.")
        if row["username"] == ROOT_USERNAME:
            raise UserError("Akun root tidak dapat dihapus.")
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return True
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# ACTIVITY LOG LAYER
# ─────────────────────────────────────────────────────────────────────────────
# Records every user action in the app (login/logout, page views, user CRUD,
# data edits, sync, ...). Rows are append-only and queried with filters +
# pagination for the Master User activity view. The `username` column is
# denormalised (copied in at write time) so the log remains meaningful even
# after the referenced user is deleted.


def log_activity(action, user_id=None, username=None, target=None,
                 detail=None, changes=None, ip=None, user_agent=None):
    """Insert one activity record. Never raises on failure (logging must not
    break the request); returns the new row id or None.

    `detail` and `changes` may be a dict/list (stored as JSON) or a plain string.
    `changes` is meant to hold the data a user added/edited/removed, e.g.
    {"changed": ["Status", "Priority"]} or {"fields": [...], "op": "update"}.
    """
    if not action:
        return None

    def _as_json(v):
        if v is None or isinstance(v, str):
            return v
        try:
            return json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(v)

    detail = _as_json(detail)
    changes = _as_json(changes)
    try:
        conn = get_conn()
        try:
            cur = conn.execute(
                """
                INSERT INTO activity_log
                    (ts, user_id, username, action, target, detail, changes, ip, user_agent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (_now_iso(), user_id, username, action, target, detail, changes,
                 ip, user_agent),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except sqlite3.Error:
        # Logging is best-effort: swallow DB errors so the caller is unaffected.
        return None


def _activity_filters(user_id=None, username=None, action=None,
                      date_from=None, date_to=None):
    """Build a WHERE clause + params list from optional filters."""
    clauses, params = [], []
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    if username:
        clauses.append("username = ?")
        params.append(username)
    if action:
        # Prefix match so 'user' matches user.create/user.update/user.delete.
        clauses.append("action LIKE ?")
        params.append(action + "%")
    if date_from:
        clauses.append("ts >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("ts <= ?")
        params.append(date_to)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def list_activity(user_id=None, username=None, action=None,
                  date_from=None, date_to=None, limit=50, offset=0):
    """Return activity rows (newest first) matching the filters."""
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    where, params = _activity_filters(user_id, username, action, date_from, date_to)
    conn = get_conn()
    try:
        try:
            rows = conn.execute(
                f"""
                SELECT id, ts, user_id, username, action, target, detail, changes, ip, user_agent
                FROM activity_log{where}
                ORDER BY id DESC
                LIMIT ? OFFSET ?;
                """,
                params + [limit, offset],
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_activity(user_id=None, username=None, action=None,
                   date_from=None, date_to=None):
    """Return the total number of activity rows matching the filters."""
    where, params = _activity_filters(user_id, username, action, date_from, date_to)
    conn = get_conn()
    try:
        try:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM activity_log{where};", params
            ).fetchone()
        except sqlite3.OperationalError:
            return 0
        return row["n"] if row else 0
    finally:
        conn.close()


def distinct_activity_actions():
    """Return the sorted list of distinct action values (for filter dropdown)."""
    conn = get_conn()
    try:
        try:
            rows = conn.execute(
                "SELECT DISTINCT action FROM activity_log ORDER BY action;"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [r["action"] for r in rows]
    finally:
        conn.close()


# Default retention for 'view' activity. Views are high-volume and low-value
# long-term, so they are pruned after this many days. Other actions
# (auth.*, user.*, api.*) are kept indefinitely.
VIEW_RETENTION_DAYS = int(os.environ.get("ACTIVITY_VIEW_RETENTION_DAYS", "90"))


def purge_old_views(days=None):
    """Delete 'view' activity older than `days` (default VIEW_RETENTION_DAYS).

    Only rows with action = 'view' are removed; all other actions are retained.
    Best-effort: returns the number of rows deleted, or 0 on any error.
    """
    try:
        days = int(days) if days is not None else VIEW_RETENTION_DAYS
    except (TypeError, ValueError):
        days = VIEW_RETENTION_DAYS
    if days <= 0:
        return 0
    cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400)
    )
    try:
        conn = get_conn()
        try:
            cur = conn.execute(
                "DELETE FROM activity_log WHERE action = 'view' AND ts < ?;",
                (cutoff,),
            )
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
