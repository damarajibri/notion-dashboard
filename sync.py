"""Notion -> SQLite sync.

Two entry points:
- full_sync():        pull every row from every database and reconcile deletions.
                      Run occasionally (e.g. once a day) to catch deleted rows.
- incremental_sync(): pull only rows edited since the last sync, using Notion's
                      built-in `last_edited_time` timestamp filter. Fast; safe to
                      run on-demand from the web app.

Notion is the source of truth; SQLite is a local cache + history store.
Deleted rows are NOT detected by incremental sync (the timestamp filter cannot
see removals), which is why a periodic full_sync() is also provided.
"""

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

import db

TOKEN = os.environ.get("NOTION_TOKEN", "")

# Database ids (mirror app.py).
DATABASES = {
    "tasks": "2c3a31d192f481d68c65d0f289ebd111",
    "projects": "2c3a31d192f48104ba5fecc8ee9c66d1",
    "personel": "2c4a31d192f480aab819f688af756ed1",
    "spk": "2c5a31d192f4803a86e4fb50b19df8dc",
    "monthly_perf": "358a31d192f4809ca281cd6849efa28a",
}

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# Overlap window subtracted from the last sync time to avoid missing rows due to
# clock skew between our server and Notion. Overlapping rows are simply re-UPSERTed.
OVERLAP_MINUTES = 5


def _notion_post(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=HEADERS, method="POST"
    )
    return json.loads(urllib.request.urlopen(req).read())


def _query(db_id, filt=None):
    """Query a Notion database, following pagination. Returns raw page objects."""
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    results, cursor, has_more = [], None, True
    while has_more:
        body = {"page_size": 100}
        if filt:
            body["filter"] = filt
        if cursor:
            body["start_cursor"] = cursor
        resp = _notion_post(url, body)
        results.extend(resp.get("results", []))
        has_more, cursor = resp.get("has_more", False), resp.get("next_cursor")
    return results


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def full_sync():
    """Pull all rows for every database and reconcile deletions."""
    db.init_db()
    now = _now_iso()
    summary = {}
    for key, db_id in DATABASES.items():
        rows = _query(db_id)
        db.upsert_rows(key, rows)
        deleted = db.delete_missing(key, [r["id"] for r in rows])
        db.set_sync_meta(key, last_sync_time=now, last_full_sync=now)
        summary[key] = {"fetched": len(rows), "deleted": deleted}
    return summary


def incremental_sync():
    """Pull only rows edited since the last sync (per database)."""
    db.init_db()
    now = _now_iso()
    summary = {}
    for key, db_id in DATABASES.items():
        meta = db.get_sync_meta(key)
        last = meta["last_sync_time"]
        if not last:
            # Never synced this database -> do a full pull for it.
            rows = _query(db_id)
            db.upsert_rows(key, rows)
            db.delete_missing(key, [r["id"] for r in rows])
            db.set_sync_meta(key, last_sync_time=now, last_full_sync=now)
            summary[key] = {"fetched": len(rows), "mode": "full(bootstrap)"}
            continue

        # Subtract an overlap window to avoid clock-skew gaps.
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%S.000Z").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            last_dt = datetime.now(timezone.utc)
        since = (last_dt - timedelta(minutes=OVERLAP_MINUTES)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )

        filt = {
            "timestamp": "last_edited_time",
            "last_edited_time": {"on_or_after": since},
        }
        rows = _query(db_id, filt)
        if rows:
            db.upsert_rows(key, rows)
        db.set_sync_meta(key, last_sync_time=now)
        summary[key] = {"fetched": len(rows), "mode": "incremental", "since": since}
    return summary


if __name__ == "__main__":
    import sys

    if not TOKEN:
        print("ERROR: NOTION_TOKEN env var is not set.")
        sys.exit(1)

    mode = sys.argv[1] if len(sys.argv) > 1 else "incremental"
    if mode == "full":
        print("Running FULL sync...")
        print(json.dumps(full_sync(), indent=2))
    else:
        print("Running INCREMENTAL sync...")
        print(json.dumps(incremental_sync(), indent=2))
