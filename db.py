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

import json
import os
import sqlite3

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
        conn.commit()
    finally:
        conn.close()


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
    """Return all raw Notion row objects for a logical database."""
    table = TABLES[db_key]
    conn = get_conn()
    try:
        return [json.loads(row["raw"]) for row in conn.execute(f"SELECT raw FROM {table}")]
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
        rows = conn.execute("SELECT last_sync_time FROM sync_meta").fetchall()
        times = [r["last_sync_time"] for r in rows if r["last_sync_time"]]
        if not times or len(times) < len(TABLES):
            # If any database has never synced, treat as due.
            return None
        return min(times)
    finally:
        conn.close()
