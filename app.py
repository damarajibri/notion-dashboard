import json, os, io, csv, time, urllib.request, urllib.error, random, secrets, uuid, mimetypes
from functools import wraps
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from flask import (
    Flask, jsonify, render_template, request, send_from_directory,
    session, redirect, url_for, abort,
)

import db
import sync
import audit_trail


def _load_dotenv():
    """Muat variabel dari file .env di folder aplikasi ke os.environ.

    Loader ringan tanpa dependensi (python-dotenv tidak selalu tersedia di
    PythonAnywhere free tier). Hanya mengisi variabel yang BELUM ada di
    environment, sehingga nilai yang diset lewat WSGI (produksi) tetap menang.
    Format didukung: baris `KEY=VALUE`, mengabaikan komentar (#) dan baris
    kosong; tanda kutip di sekeliling nilai dilepas.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    try:
        with open(path, encoding='utf-8') as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, val = line.split('=', 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass


_load_dotenv()

app = Flask(__name__)

# ── Session / auth secret ─────────────────────────────────────────────────────
# SECRET_KEY signs the session cookie. In production set FLASK_SECRET_KEY in the
# environment (e.g. the PythonAnywhere WSGI file). We fall back to a random
# per-process key so the app still runs locally, but note that a random key
# means sessions are invalidated on restart.
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

# ── On-demand incremental sync (Option C) ────────────────────────────────────
# Read endpoints (/api/data, /api/monthly) serve from a local SQLite cache.
# When the cache is older than SYNC_TTL_SECONDS, an incremental sync (only rows
# changed since last sync, via Notion's last_edited_time filter) refreshes it
# first. This keeps data fresh (~5 min) with no external scheduler, entirely on
# PythonAnywhere, behind the app's own auth. Sync failures degrade gracefully
# to whatever is already cached.
SYNC_TTL_SECONDS = int(os.environ.get('SYNC_TTL_SECONDS', '300'))

# Full sync interval (Option B). Incremental sync cannot detect rows deleted
# directly in Notion (the last_edited_time filter never returns removed rows),
# so ghost rows would linger in the cache indefinitely. To reconcile deletions
# without an external scheduler, a full_sync() (which pulls every row and prunes
# rows no longer present in Notion) is triggered on-demand when the last full
# sync is older than FULL_SYNC_TTL_SECONDS. Default: 12 hours. A full sync is
# heavier than an incremental one, so this interval is intentionally long; the
# request that happens to trigger it will be slightly slower.
FULL_SYNC_TTL_SECONDS = int(os.environ.get('FULL_SYNC_TTL_SECONDS', '43200'))

# Ensure the SQLite schema exists at import time. Under WSGI (PythonAnywhere)
# the __main__ block never runs, so tables must be created here.
try:
    db.init_db()
except Exception as e:  # noqa: BLE001
    app.logger.warning('db.init_db() at import failed: %s', e)


def _sync_is_due():
    oldest = db.oldest_sync_time()
    if not oldest:
        return True
    try:
        oldest_dt = datetime.strptime(oldest, '%Y-%m-%dT%H:%M:%S.000Z').replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - oldest_dt > timedelta(seconds=SYNC_TTL_SECONDS)


def _full_sync_is_due():
    """True if a full sync (deletion reconciliation) is overdue.

    Returns True when no full sync has ever been recorded, when the stored
    timestamp is unparseable, or when the oldest last_full_sync is older than
    FULL_SYNC_TTL_SECONDS.
    """
    oldest = db.oldest_full_sync_time()
    if not oldest:
        return True
    try:
        oldest_dt = datetime.strptime(oldest, '%Y-%m-%dT%H:%M:%S.000Z').replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - oldest_dt > timedelta(seconds=FULL_SYNC_TTL_SECONDS)


def _ensure_fresh():
    try:
        # Prefer a full sync when it is due: it both refreshes changed rows AND
        # reconciles deletions, so there is no need to also run an incremental
        # sync in the same request.
        if _full_sync_is_due():
            sync.full_sync()
        elif _sync_is_due():
            sync.incremental_sync()
    except Exception as e:  # noqa: BLE001 - never fail the request on sync error
        app.logger.warning('On-demand sync failed, serving cached data: %s', e)


# Logical name for each Notion DB id, used to read cached rows from SQLite.
_DBKEY_BY_ID = {
    '2c3a31d192f481d68c65d0f289ebd111': 'tasks',
    '2c3a31d192f48104ba5fecc8ee9c66d1': 'projects',
    '2c4a31d192f480aab819f688af756ed1': 'personel',
    '2c5a31d192f4803a86e4fb50b19df8dc': 'spk',
    '358a31d192f4809ca281cd6849efa28a': 'monthly_perf',
}


def cached_query(db_id):
    """Return raw Notion rows for db_id from the local SQLite cache.

    Falls back to a live Notion query if the id is unknown (should not happen).
    """
    key = _DBKEY_BY_ID.get(db_id)
    if key:
        return db.load_rows(key)
    return query_all(db_id)


# ── Error handler global: pastikan endpoint /api/* selalu balas JSON ──────────
# Tanpa ini, exception tak tertangani membuat Flask mengembalikan halaman HTML
# error (diawali '<'), sehingga frontend gagal JSON.parse → "Unexpected token '<'".
@app.errorhandler(Exception)
def _handle_any_error(e):
    from werkzeug.exceptions import HTTPException
    path = request.path if request else ''
    code = e.code if isinstance(e, HTTPException) else 500
    if path.startswith('/api/'):
        return jsonify({
            'ok': False,
            'error': f'Server error: {type(e).__name__}: {e}',
        }), code
    # Untuk non-API, biarkan perilaku default Flask
    if isinstance(e, HTTPException):
        return e
    return ('Internal Server Error', 500)

TOKEN = os.environ.get('NOTION_TOKEN', '')
TASKS_DB    = '2c3a31d192f481d68c65d0f289ebd111'
PROJECTS_DB = '2c3a31d192f48104ba5fecc8ee9c66d1'
PERSONEL_DB = '2c4a31d192f480aab819f688af756ed1'
SPK_DB      = '2c5a31d192f4803a86e4fb50b19df8dc'
MONTHLY_DB  = '358a31d192f4809ca281cd6849efa28a'

HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Notion-Version': '2022-06-28',
    'Content-Type': 'application/json'
}

# ─── Notion helpers ──────────────────────────────────────────────────────────

def notion_post(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=HEADERS, method='POST'
    )
    return json.loads(urllib.request.urlopen(req).read())

def notion_get(url):
    hdrs = {k: v for k, v in HEADERS.items() if k != 'Content-Type'}
    req = urllib.request.Request(url, headers=hdrs)
    return json.loads(urllib.request.urlopen(req).read())


def notion_delete(url):
    hdrs = {k: v for k, v in HEADERS.items() if k != 'Content-Type'}
    req = urllib.request.Request(url, headers=hdrs, method='DELETE')
    return json.loads(urllib.request.urlopen(req).read())


# Instance helper audit trail (body halaman). notion_patch didefinisikan di
# bawah; audit_blocks hanya memakainya saat request, jadi aman direferensikan.
audit_blocks = audit_trail.NotionBlocks(
    get=notion_get,
    post=notion_post,
    patch=lambda url, body: notion_patch(url, body),
    delete=notion_delete,
)


def _audit_user_dict():
    """Ringkas user yang sedang login untuk disimpan di audit trail."""
    u = current_user()
    if not u:
        return {"id": None, "username": "(anonymous)", "full_name": ""}
    return {"id": u.get("id"), "username": u.get("username"),
            "full_name": u.get("full_name") or ""}


def _audit_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _record_audit(page_id, action='update'):
    """Ambil halaman SEGAR dari Notion, snapshot semua properti, lalu simpan
    satu versi baru ke audit trail di body halaman. Best-effort: kegagalan
    audit tidak boleh menjatuhkan operasi utama. Return nomor versi atau None.
    """
    try:
        raw = notion_get(f'https://api.notion.com/v1/pages/{page_id}')
        snap = audit_trail.snapshot_properties(raw)
        return audit_trail.append_version(
            audit_blocks, page_id, snap, _audit_user_dict(), _audit_now(), action
        )
    except Exception as e:  # noqa: BLE001
        app.logger.warning('Audit record failed for %s: %s', page_id, e)
        return None


# ─── Notion Direct Upload (file upload) ───────────────────────────────────────
# Alur: (1) create file_upload → dapat id+upload_url, (2) send bytes (multipart),
# (3) attach id ke properti 'files' via Update Page. File harus di-attach dalam
# ~1 jam. Endpoint file_uploads bekerja dengan Notion-Version yang dipakai proyek
# (diverifikasi). Tidak menambah dependensi: multipart dibangun manual via urllib.

def notion_create_file_upload(filename, content_type):
    """Langkah 1: buat objek file_upload. Return dict berisi id & upload_url."""
    body = {'filename': filename}
    if content_type:
        body['content_type'] = content_type
    return notion_post('https://api.notion.com/v1/file_uploads', body)


def _build_multipart(file_bytes, filename, content_type):
    """Bangun body multipart/form-data dengan satu field 'file'. Return (body, boundary)."""
    boundary = '----NotionUpload' + uuid.uuid4().hex
    ct = content_type or 'application/octet-stream'
    # Escape tanda kutip pada nama file (RFC 2388).
    safe_name = (filename or 'file').replace('"', '')
    pre = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
        f'Content-Type: {ct}\r\n\r\n'
    ).encode('utf-8')
    post = f'\r\n--{boundary}--\r\n'.encode('utf-8')
    return pre + file_bytes + post, boundary


def notion_send_file_upload(file_upload_id, file_bytes, filename, content_type):
    """Langkah 2: kirim isi file (multipart) ke endpoint send. Return objek file_upload."""
    url = f'https://api.notion.com/v1/file_uploads/{file_upload_id}/send'
    data, boundary = _build_multipart(file_bytes, filename, content_type)
    hdrs = {
        'Authorization': HEADERS['Authorization'],
        'Notion-Version': HEADERS['Notion-Version'],
        'Content-Type': f'multipart/form-data; boundary={boundary}',
    }
    req = urllib.request.Request(url, data=data, headers=hdrs, method='POST')
    return json.loads(urllib.request.urlopen(req).read())


def notion_set_files_property(page_id, field, files_list):
    """Langkah 3 / edit: tulis ulang properti 'files' sebuah halaman.

    `files_list` adalah daftar objek file Notion. Notion menerima penulisan:
      - {'type':'file_upload','file_upload':{'id':...},'name':...}  (hasil upload)
      - {'type':'external','external':{'url':...},'name':...}        (link eksternal)
      - {'type':'file','file':{...},'name':...}                      (entri lama;
        diverifikasi dapat ditulis ulang apa adanya untuk dipertahankan)
    Untuk add/delete: ambil halaman SEGAR, modifikasi array, lalu PATCH.
    """
    body = {'properties': {field: {'files': files_list}}}
    return notion_patch(f'https://api.notion.com/v1/pages/{page_id}', body)


def query_all(db_id, filt=None):
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    results, cursor, has_more = [], None, True
    while has_more:
        body = {'page_size': 100}
        if filt:   body['filter']       = filt
        if cursor: body['start_cursor'] = cursor
        resp = notion_post(url, body)
        results.extend(resp.get('results', []))
        has_more = resp.get('has_more', False)
        cursor   = resp.get('next_cursor')
    return results

# ─── Personel ─────────────────────────────────────────────────────────────────

def get_personel():
    """Return dict {page_id: name} dari database Personel (cached SQLite)."""
    p = {}
    for r in cached_query(PERSONEL_DB):
        for v in r['properties'].values():
            if v.get('type') == 'title' and v['title']:
                p[r['id']] = v['title'][0]['plain_text']
    return p

# ─── Page title cache ──────────────────────────────────────────────────────────

_title_cache = {}

def get_page_title(page_id):
    try:
        data = notion_get(f'https://api.notion.com/v1/pages/{page_id}')
        for v in data.get('properties', {}).values():
            if v.get('type') == 'title' and v.get('title'):
                return v['title'][0]['plain_text']
    except Exception:
        pass
    return '?'

def resolve_title(page_id, personel):
    if page_id in personel:
        return personel[page_id]
    if page_id not in _title_cache:
        _title_cache[page_id] = get_page_title(page_id)
    return _title_cache[page_id]

# ─── extract_task ─────────────────────────────────────────────────────────────
# Fields yang digunakan:
#   Task name   (title)
#   Status      (status)
#   Due Date    (date)
#   Priority    (select)          ← baru
#   Assignee relation (relation)  ← gunakan ini, bukan 'Owner' people
#   Progress    (number, format percent)
#   Tags        (multi_select)
#   Completed on (date)           ← tetap dipakai sebagai done_date fallback
#   last edited time (last_edited_time)

def extract_task(r, personel):
    props = r['properties']

    # Nama task
    title = props.get('Task name', {}).get('title', [])
    name  = title[0]['plain_text'] if title else ''

    # Status
    status_obj  = props.get('Status', {}).get('status') or {}
    status_name = status_obj.get('name', '')

    # Due Date
    due_raw  = props.get('Due Date', {}).get('date') or {}
    due_start = due_raw.get('start', '')[:10] if due_raw.get('start') else ''

    # Priority
    priority_obj  = props.get('Priority', {}).get('select') or {}
    priority_name = priority_obj.get('name', '')

    # Assignee — dari relasi ke Personel DB
    rel       = props.get('Assignee relation', {}).get('relation', [])
    assignees = [personel.get(a['id'], '?') for a in rel]
    assignee_ids = [a['id'] for a in rel]

    # Progress (0.0–1.0 → kita simpan 0–100)
    progress_raw = props.get('Progress', {}).get('number')
    progress     = int(progress_raw * 100) if progress_raw is not None else None

    # Tags
    tags = [t['name'] for t in props.get('Tags', {}).get('multi_select', [])]

    # Completed on — sebagai done_date jika ada, fallback ke last_edited saat Done
    comp_raw  = props.get('Completed on', {}).get('date') or {}
    comp_date = comp_raw.get('start', '')[:10] if comp_raw.get('start') else ''

    done_date = comp_date or (r['last_edited_time'][:10] if status_name == 'Done' else '')

    return {
        'name':      name,
        'status':    status_name,
        'due':       due_start,
        'priority':  priority_name,
        'assignees': assignees,
        'created':   r['created_time'][:10],
        'edited':    r['last_edited_time'][:10],
        'done_date': done_date,
        'progress':  progress,
        'tags':      tags,
        'priority_raw': priority_obj.get('name', '') or '',
        'assignee_ids': assignee_ids,
        'page_id':   r['id'],
    }

# ─── extract_project ─────────────────────────────────────────────────────────
# Fields yang digunakan:
#   Project name (title)          ← nama field sudah pasti 'Project name'
#   Status       (status)
#   Priority     (select)
#   Assignee     (relation → Personel)
#   Progress     (formula → number, 0.0–1.0)  ← pakai formula, bukan rollup
#   Dates        (date)
#   Dokumen: TOR, FS, Izin Prinsip, Izin Anggaran, Penilaian Teknis,
#            PI (Pakta Integritas), TPRA, BenchMark, Aanwidjzing,
#            Risk Assessment                ← tambah Risk Assessment
#   SPK baru / SPK sebelumnya dihapus dari sini, diambil dari SPK DB

DOC_FIELDS = [
    'TOR',
    'FS (Feasibility Study)',
    'Izin Prinsip',
    'Izin Anggaran',
    'Penilaian Teknis',
    'PI (Pakta Integritas)',
    'TPRA (Third Party Risk Assesment)',
    'BenchMark',
    'Aanwidjzing',
    'Risk Assessment',
]

# Field bertipe "files" di database Projects (untuk ditampilkan sebagai tautan).
# Catatan: 'Izin Anggaran FIle' mengikuti typo asli di Notion (jangan diperbaiki
# di sini, harus sama persis dengan nama property di Notion agar cocok).
PROJECT_FILE_FIELDS = [
    'TOR File',
    'FS File',
    'Izin Prinsip File',
    'Izin Anggaran FIle',
    'Penilaian Teknis File',
    'PI file (Pakta Integritas)',
    'TPRA File',
    'Benchmark File',
    'Risk Assessment File',
]


def _file_meta(props, field, page_id):
    """Kembalikan metadata file untuk satu property bertipe 'files'.

    Kita sengaja TIDAK menyimpan URL hasil sync, karena URL file Notion
    (type='file') adalah presigned URL S3 yang kedaluwarsa ~1 jam. Sebagai
    gantinya kita simpan koordinat (page_id, field, idx) + nama file, lalu URL
    segar diambil on-demand lewat endpoint /api/file saat user mengklik.

    File bertipe 'external' (URL permanen) kita sertakan url-nya langsung,
    karena tidak kedaluwarsa dan bisa dibuka tanpa resolve.

    Returns list of dict: {page_id, field, idx, name, external_url?}
    """
    out = []
    files = (props.get(field, {}) or {}).get('files') or []
    for idx, f in enumerate(files):
        meta = {
            'page_id': page_id,
            'field':   field,
            'idx':     idx,
            'name':    f.get('name', 'file'),
        }
        if f.get('type') == 'external':
            meta['external_url'] = f.get('external', {}).get('url', '')
        out.append(meta)
    return out


def extract_project(r, personel):
    props = r['properties']

    # Nama — field bernama 'Project name'
    title_prop = props.get('Project name', {}).get('title', [])
    title = title_prop[0]['plain_text'] if title_prop else ''

    # Status
    status_obj  = props.get('Status', {}).get('status') or {}
    status_name = status_obj.get('name', '')

    # Priority
    priority_obj  = props.get('Priority', {}).get('select') or {}
    priority_name = priority_obj.get('name', '') or '-'

    # Assignee (relation ke Personel DB)
    rel       = props.get('Assignee', {}).get('relation', [])
    assignees = [personel.get(a['id'], '?') for a in rel]
    assignee_ids = [a['id'] for a in rel]

    # No. Izin Prinsip (rich_text) & SPK sebelumnya (relation page_ids)
    nip_raw = props.get('No. Izin Prinsip', {}).get('rich_text', [])
    no_izin_prinsip = nip_raw[0]['plain_text'] if nip_raw else ''
    spk_sebelumnya_ids = [x['id'] for x in props.get('SPK sebelumnya', {}).get('relation', [])]

    # Progress dari formula (0.0–1.0)
    prog_formula = props.get('Progress', {}).get('formula') or {}
    comp_val     = None
    if prog_formula.get('number') is not None:
        comp_val = round(prog_formula['number'] * 100)

    # Due date dari field Dates
    dates_raw = props.get('Dates', {}).get('date') or {}
    if dates_raw.get('end'):
        due = dates_raw['end'][:10]
    elif dates_raw.get('start'):
        due = dates_raw['start'][:10]
    else:
        due = ''

    # Dokumen checklist (10 dokumen termasuk Risk Assessment)
    doc_done   = 0
    doc_detail = {}
    doc_status = {}
    done_statuses = {'Done', 'Complete', 'Not Required'}
    for df in DOC_FIELDS:
        val  = props.get(df, {})
        done = False
        raw_name = ''
        if val.get('type') == 'status':
            raw_name = (val.get('status') or {}).get('name', '') or ''
            done = raw_name in done_statuses
        elif val.get('type') == 'checkbox':
            done = bool(val.get('checkbox'))
            raw_name = 'Done' if done else 'Not started'
        if done:
            doc_done += 1
        doc_detail[df] = '✅' if done else '❌'
        doc_status[df] = raw_name

    total_docs = len(DOC_FIELDS)

    # File metadata (fetch-on-click) dari semua field file project.
    files = []
    for ff in PROJECT_FILE_FIELDS:
        files.extend(_file_meta(props, ff, r['id']))

    return {
        'title':      title,
        'status':     status_name,
        'priority':   priority_name,
        'priority_raw': priority_obj.get('name', '') or '',
        'nominal_ip': props.get('Nominal IP', {}).get('number'),
        'no_izin_prinsip': no_izin_prinsip,
        'spk_sebelumnya_ids': spk_sebelumnya_ids,
        'assignee_ids': assignee_ids,
        'assignees':  assignees,
        'completion': comp_val,
        'docs':       f'{doc_done}/{total_docs}',
        'doc_done':   doc_done,
        'doc_total':  total_docs,
        'doc_detail': doc_detail,
        'doc_status': doc_status,
        'created':    r['created_time'][:10],
        'edited':     r['last_edited_time'][:10],
        'due':        due,
        'files':      files,
        'page_id':    r['id'],
    }

# ─── extract_spk ─────────────────────────────────────────────────────────────
# Fields yang digunakan:
#   No SPK          (title)
#   Project Name    (rich_text)
#   Vendor          (relation)
#   Status          (status)
#   SPK Selesai     (date)        ← ganti dari 'Jatuh Tempo'
#   SPK Mulai       (date)        ← baru
#   Sisa Hari       (formula)
#   Total Anggaran  (number)      ← ganti dari 'Nilai Kontrak SPK'
#   Sisa Anggaran   (formula)     ← baru
#   Total Terbayar  (rollup)      ← baru
#   Jenis Anggaran  (select)      ← baru
#   Klasifikasi Pengadaan (select) ← baru
#   Baru-Sisa Bayar-Perpanjangan (select) ← baru (Tipe)
#   Notes           (rich_text)
#   id              (unique_id)
#   PIC Perpanjangan (rollup)
#   Projects Perpanjangan (relation)
#   Status Project Perpanjangan (rollup)

def extract_spk(r, personel):
    props = r['properties']

    # ID unik
    uid    = props.get('id', {}).get('unique_id') or {}
    spk_id = f"{uid.get('prefix', '')}-{uid.get('number', '')}" if uid else ''

    # Nomor SPK
    no_spk_raw = props.get('No SPK', {}).get('title', [])
    no_spk     = no_spk_raw[0]['plain_text'] if no_spk_raw else ''

    # Nama project
    proj_raw  = props.get('Project Name', {}).get('rich_text', [])
    proj_name = proj_raw[0]['plain_text'] if proj_raw else ''

    # Vendor
    vendor_rel  = props.get('Vendor', {}).get('relation', [])
    vendor_name = ', '.join(
        resolve_title(v['id'], personel) for v in vendor_rel
    ) if vendor_rel else '-'

    # Status
    status_obj  = props.get('Status', {}).get('status') or {}
    status_name = status_obj.get('name', '') or '-'

    # SPK Selesai (dulu "Jatuh Tempo")
    spk_selesai_raw = props.get('SPK Selesai', {}).get('date') or {}
    spk_selesai     = spk_selesai_raw.get('start', '')[:10] if spk_selesai_raw.get('start') else ''

    # SPK Mulai
    spk_mulai_raw = props.get('SPK Mulai', {}).get('date') or {}
    spk_mulai     = spk_mulai_raw.get('start', '')[:10] if spk_mulai_raw.get('start') else ''

    # Sisa Hari (formula)
    sisa_raw  = props.get('Sisa Hari', {}).get('formula') or {}
    sisa_hari = sisa_raw.get('number')

    # Total Anggaran
    total_anggaran = props.get('Total Anggaran', {}).get('number')

    # Sisa Anggaran (formula)
    sisa_ang_raw  = props.get('Sisa Anggaran', {}).get('formula') or {}
    sisa_anggaran = sisa_ang_raw.get('number')

    # Total Terbayar (rollup sum)
    terbayar_raw   = props.get('Total Terbayar', {}).get('rollup') or {}
    total_terbayar = terbayar_raw.get('number')

    # Jenis Anggaran
    jenis_obj   = props.get('Jenis Anggaran', {}).get('select') or {}
    jenis_ang   = jenis_obj.get('name', '') or '-'

    # Klasifikasi Pengadaan
    klasifikasi_obj = props.get('Klasifikasi Pengadaan', {}).get('select') or {}
    klasifikasi     = klasifikasi_obj.get('name', '') or '-'

    # Tipe (Baru / Perpanjangan / Sisa Bayar)
    tipe_obj = props.get('Baru-Sisa Bayar-Perpanjangan', {}).get('select') or {}
    tipe     = tipe_obj.get('name', '') or '-'

    # Notes
    notes_raw  = props.get('Notes', {}).get('rich_text', [])
    notes_text = notes_raw[0]['plain_text'] if notes_raw else ''

    # PIC Perpanjangan (rollup → array of relation)
    pic = []
    pic_rollup = props.get('PIC Perpanjangan', {}).get('rollup', {}).get('array', [])
    for item in pic_rollup:
        if item.get('type') == 'relation':
            for rel in item.get('relation', []):
                name = personel.get(rel['id'], '?')
                if name not in pic:
                    pic.append(name)

    # Projects Perpanjangan
    perp_ids = [rel['id'] for rel in props.get('Projects Perpanjangan', {}).get('relation', [])]

    # Status Project Perpanjangan (rollup)
    status_perp_arr = props.get('Status Project Perpanjangan', {}).get('rollup', {}).get('array', [])
    status_perp = [
        item['status']['name']
        for item in status_perp_arr
        if item.get('type') == 'status' and item.get('status')
    ]

    # File metadata (fetch-on-click) — gabungan 'SPK File' + 'File & media'.
    # URL tidak disimpan (expiry ~1 jam); hanya koordinat utk /api/file.
    files = _file_meta(props, 'SPK File', r['id']) + _file_meta(props, 'File & media', r['id'])

    return {
        'spk_id':        spk_id,
        'no_spk':        no_spk,
        'project':       proj_name,
        'vendor':        vendor_name,
        'status':        status_name,
        'spk_mulai':     spk_mulai,
        'spk_selesai':   spk_selesai,
        'sisa_hari':     sisa_hari,
        'total_anggaran': total_anggaran,
        'sisa_anggaran': sisa_anggaran,
        'total_terbayar': total_terbayar,
        'jenis_anggaran': jenis_ang,
        'klasifikasi':   klasifikasi,
        'tipe':          tipe,
        'notes':         notes_text,
        'pic':           pic,
        'perp_ids':      perp_ids,
        'status_perpanjangan': status_perp,
        'files':         files,
        'page_id':       r['id'],
        '_id':           r['id'],
    }

# ─── extract_monthly ──────────────────────────────────────────────────────────
# Ekstrak satu baris database Monthly Performance untuk ditampilkan di dashboard.
# Field: Name (title), Periode (date range), Nilai Tagihan (number),
#        Prognosa (number), Status Invoice/Pembayaran/BA Performansi/Rekon (select),
#        No. Dokumen BA LP (rich_text), Tanggal Serah/masuk BA Performansi &
#        Tanggal Rekon (date), 📋 SPK (relation → No SPK).

def _sel_name(props, key):
    obj = props.get(key, {}).get('select') or {}
    return obj.get('name', '') or ''

def _date_start(props, key):
    d = props.get(key, {}).get('date') or {}
    return (d.get('start') or '')[:10]

def extract_monthly(r, spk_no_map):
    props = r['properties']

    title = props.get('Name', {}).get('title', [])
    name  = title[0]['plain_text'] if title else ''

    nilai    = props.get('Nilai Tagihan', {}).get('number')
    prognosa = props.get('Prognosa', {}).get('number')

    doklp_raw = props.get('No. Dokumen BA LP', {}).get('rich_text', [])
    dok_lp    = doklp_raw[0]['plain_text'] if doklp_raw else ''

    # No. Invoice (rich_text)
    inv_raw    = props.get('No. Invoice', {}).get('rich_text', [])
    no_invoice = inv_raw[0]['plain_text'] if inv_raw else ''

    # Keterangan (rich_text)
    ket_raw    = props.get('Keterangan', {}).get('rich_text', [])
    keterangan = ket_raw[0]['plain_text'] if ket_raw else ''

    # ID (unique_id) → gabungan prefix-number
    uid    = props.get('ID', {}).get('unique_id') or {}
    doc_id = f"{uid.get('prefix', '')}-{uid.get('number', '')}" if uid.get('number') is not None else ''

    # Files (BAST & BALP) → metadata fetch-on-click (bukan URL basi).
    # URL file Notion type='file' kedaluwarsa ~1 jam, jadi kita simpan
    # koordinat (page_id, field, idx) dan resolve URL segar lewat /api/file.
    files_bast = _file_meta(props, 'Files BAST', r['id'])
    files_balp = _file_meta(props, 'Files BALP', r['id'])

    # Relation SPK → tampilkan No SPK-nya
    spk_rel = props.get('📋 SPK', {}).get('relation', [])
    spk_no  = ', '.join(spk_no_map.get(rel['id'], '?') for rel in spk_rel) if spk_rel else ''

    return {
        'id':              doc_id,
        'page_id':         r['id'],
        'name':            name,
        'periode':         _date_start(props, 'Periode'),
        'no_spk':          spk_no,
        'nilai_tagihan':   nilai,
        'prognosa':        prognosa,
        'status_invoice':  _sel_name(props, 'Status Invoice'),
        'status_bayar':    _sel_name(props, 'Status Pembayaran'),
        'status_ba':       _sel_name(props, 'Status BA Performansi'),
        'status_rekon':    _sel_name(props, 'Status Rekon'),
        'no_invoice':      no_invoice,
        'dok_lp':          dok_lp,
        'keterangan':      keterangan,
        'tgl_serah_ba':    _date_start(props, 'Tanggal Serah BA Performansi'),
        'tgl_masuk_ba':    _date_start(props, 'Tanggal masuk BA Performansi'),
        'tgl_rekon':       _date_start(props, 'Tanggal Rekon'),
        'files_bast':      files_bast,
        'files_balp':      files_balp,
    }

# ─── AUTH: captcha, session helpers, decorators ──────────────────────────────

# A simple, dependency-free text CAPTCHA. We generate a short random code, store
# it (case-insensitively) in the server-side session, and render it on the login
# page with light visual distortion via CSS. The user must type it back. This
# defends against trivial automated login attempts without any external service.
_CAPTCHA_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # omit ambiguous 0/O/1/I


def generate_captcha(length=5):
    """Create a new captcha code, store it in the session, and return it."""
    code = ''.join(random.choice(_CAPTCHA_CHARS) for _ in range(length))
    session['captcha'] = code
    return code


def check_captcha(answer):
    """Compare the user's answer with the stored captcha (case-insensitive).

    The captcha is single-use: it is cleared from the session on any check.
    """
    expected = session.pop('captcha', None)
    if not expected or not answer:
        return False
    return answer.strip().upper() == expected.upper()


def current_user():
    """Return the logged-in user dict from session, or None."""
    uid = session.get('uid')
    if uid is None:
        return None
    return db.get_user(uid)


def login_required(view):
    """Redirect to /login for pages, or return 401 JSON for /api/* endpoints."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get('uid') is None:
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': 'Unauthorized. Silakan login.'}), 401
            return redirect(url_for('login', next=request.path))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    """Restrict a view to the given roles (in addition to requiring login)."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if session.get('uid') is None:
                if request.path.startswith('/api/'):
                    return jsonify({'ok': False, 'error': 'Unauthorized.'}), 401
                return redirect(url_for('login', next=request.path))
            if session.get('role') not in roles:
                if request.path.startswith('/api/'):
                    return jsonify({'ok': False, 'error': 'Akses ditolak (role tidak cukup).'}), 403
                return abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.context_processor
def inject_user():
    """Make `user` available in all templates."""
    return {'user': current_user()}


# ─── ACTIVITY LOG ─────────────────────────────────────────────────────────────
# One helper to record any user action, pulling identity from the session and
# network metadata from the request. Logging is best-effort and must never
# break a request, so db.log_activity() swallows its own errors.

def _client_ip():
    """Best-effort client IP, honouring a single proxy hop (X-Forwarded-For)."""
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or ''


# Fields that are identifiers/metadata, not user-edited data — excluded from
# the "what changed" list so the log shows only meaningful data fields.
_CHANGE_IGNORE_KEYS = {'page_id', 'id', 'csrf_token', 'db_key', 'database'}


def _extract_change_fields(method):
    """Build a `changes` dict describing the data a user added/edited/removed.

    Reads the request JSON body (the payload the client sent) and lists the
    field names present, tagged with an operation derived from the HTTP method:
      POST -> 'create'   PUT/PATCH -> 'update'   DELETE -> 'delete'
    Returns None when there is no meaningful body (e.g. a plain DELETE).
    Best-effort: never raises.
    """
    op = {'POST': 'create', 'PUT': 'update', 'PATCH': 'update',
          'DELETE': 'delete'}.get(method, method.lower())
    try:
        payload = request.get_json(silent=True)
    except Exception:  # noqa: BLE001
        payload = None
    if isinstance(payload, dict):
        fields = [k for k in payload.keys() if k not in _CHANGE_IGNORE_KEYS]
        if fields:
            return {'op': op, 'fields': sorted(fields)}
    if op == 'delete':
        return {'op': 'delete'}
    return None


def record_activity(action, target=None, detail=None, changes=None, user=None):
    """Record one activity row for the current request/session.

    `user` may be passed explicitly (e.g. on login, before the session is set);
    otherwise identity is read from the session.
    `changes` holds the data a user added/edited/removed (stored as JSON).
    """
    if user is not None:
        uid = user.get('id')
        uname = user.get('username')
    else:
        uid = session.get('uid')
        uname = session.get('username')
    db.log_activity(
        action=action,
        user_id=uid,
        username=uname,
        target=target,
        detail=detail,
        changes=changes,
        ip=_client_ip(),
        user_agent=(request.headers.get('User-Agent') or '')[:400],
    )


# Page paths we DON'T want to log as "views" (static assets, the log viewer's
# own polling endpoint, captcha refresh, and the activity API itself).
_VIEW_LOG_SKIP_PREFIXES = ('/static/', '/favicon')
_VIEW_LOG_SKIP_EXACT = {'/captcha/refresh'}

# Mutating API paths that already record their own domain-specific activity
# (user CRUD), so the generic mutation logger skips them to avoid duplicates.
_MUTATION_LOG_SKIP = ('/api/users',)

# Auto-purge of old 'view' activity (retention handled in db.purge_old_views).
# Runs opportunistically from the request path, throttled to at most once per
# interval so it adds negligible overhead and needs no external scheduler.
_PURGE_INTERVAL_SECONDS = int(os.environ.get('ACTIVITY_PURGE_INTERVAL', '21600'))  # 6h
_last_view_purge = 0.0


def _maybe_purge_views():
    """Prune old 'view' rows at most once per _PURGE_INTERVAL_SECONDS."""
    global _last_view_purge
    now = time.time()
    if now - _last_view_purge < _PURGE_INTERVAL_SECONDS:
        return
    _last_view_purge = now  # set first so concurrent requests don't pile up
    try:
        deleted = db.purge_old_views()
        if deleted:
            app.logger.info('Activity auto-purge: removed %d old view rows.', deleted)
    except Exception as e:  # noqa: BLE001
        app.logger.warning('Activity auto-purge failed: %s', e)


@app.after_request
def _log_page_view(response):
    """Log user activity automatically from the request/response.

    Two kinds are captured here so individual handlers stay untouched:
      1. Page views: GET requests to real HTML pages (not static/polling).
      2. Data mutations: POST/PUT/PATCH/DELETE to /api/* endpoints, logged
         generically as 'api.<method> <path>' with the response status.

    Endpoints that log richer, domain-specific activity themselves (auth,
    user CRUD) are listed in _MUTATION_LOG_SKIP so they are not double-logged.
    Logging is best-effort and must never break the response.
    """
    try:
        method = request.method
        path = request.path or ''

        # ── Data mutations on the API ────────────────────────────────────────
        if method in ('POST', 'PUT', 'PATCH', 'DELETE') and path.startswith('/api/'):
            if session.get('uid') is None:
                return response
            if any(path == p or path.startswith(p) for p in _MUTATION_LOG_SKIP):
                return response  # already logged with domain detail
            ok = response.status_code < 400
            record_activity(
                f'api.{method.lower()}',
                target=path,
                detail={'status': response.status_code, 'ok': ok},
                changes=_extract_change_fields(method),
            )
            return response

        # ── Page views ───────────────────────────────────────────────────────
        if method != 'GET':
            return response
        if session.get('uid') is None:
            return response
        if path in _VIEW_LOG_SKIP_EXACT:
            return response
        if any(path.startswith(p) for p in _VIEW_LOG_SKIP_PREFIXES):
            return response
        if path.startswith('/api/'):
            return response
        if response.status_code >= 400:
            return response
        ctype = response.headers.get('Content-Type', '')
        if 'text/html' not in ctype:
            return response
        qs = request.query_string.decode('utf-8', 'ignore')
        detail = {'query': qs} if qs else None
        record_activity('view', target=path, detail=detail)
        _maybe_purge_views()
    except Exception:  # noqa: BLE001 — logging must never break the response.
        pass
    return response


# ─── AUTH ROUTES ──────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    # Already logged in -> go to dashboard.
    if session.get('uid') is not None and request.method == 'GET':
        return redirect(url_for('index'))

    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        captcha_answer = request.form.get('captcha') or ''

        if not check_captcha(captcha_answer):
            error = 'Captcha salah. Coba lagi.'
            record_activity('auth.login_failed', target=username,
                            detail={'reason': 'captcha'})
        else:
            user = db.authenticate(username, password)
            if user:
                db.touch_last_login(user['id'])
                session.clear()
                session.permanent = True
                session['uid'] = user['id']
                session['username'] = user['username']
                session['role'] = user['role']
                record_activity('auth.login', user=user)
                nxt = request.args.get('next') or url_for('index')
                # Only allow local redirects.
                if not nxt.startswith('/'):
                    nxt = url_for('index')
                return redirect(nxt)
            error = 'Username atau password salah, atau akun nonaktif.'
            record_activity('auth.login_failed', target=username,
                            detail={'reason': 'bad_credentials'})

    # (Re)generate a fresh captcha for every rendered login form.
    captcha = generate_captcha()
    return render_template('login.html', error=error, captcha=captcha)


@app.route('/captcha/refresh')
def captcha_refresh():
    """Return a fresh captcha code as JSON (for the 'reload captcha' button)."""
    return jsonify({'captcha': generate_captcha()})


@app.route('/logout')
def logout():
    record_activity('auth.logout')
    session.clear()
    return redirect(url_for('login'))


# ─── MASTER USER (CRUD) ───────────────────────────────────────────────────────
# Only root & admin may manage users.

@app.route('/users')
@roles_required('root', 'admin')
def users_page():
    return render_template('users.html', users=db.list_users())


@app.route('/api/users', methods=['GET'])
@roles_required('root', 'admin')
def api_users_list():
    return jsonify({'ok': True, 'users': db.list_users()})


@app.route('/api/users', methods=['POST'])
@roles_required('root', 'admin')
def api_users_create():
    payload = request.get_json(silent=True) or request.form
    try:
        u = db.create_user(
            username=payload.get('username'),
            password=payload.get('password'),
            full_name=payload.get('full_name', ''),
            role=payload.get('role', 'user'),
            is_active=str(payload.get('is_active', 'true')).lower() in ('1', 'true', 'yes', 'on'),
        )
        record_activity('user.create', target=str(u['id']),
                        detail={'username': u['username'], 'role': u['role']},
                        changes={'op': 'create',
                                 'fields': sorted(k for k in ('username', 'full_name',
                                                              'role', 'is_active', 'password')
                                                  if payload.get(k) not in (None, ''))})
        return jsonify({'ok': True, 'user': u}), 201
    except db.UserError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/users/<int:user_id>', methods=['PUT', 'POST'])
@roles_required('root', 'admin')
def api_users_update(user_id):
    payload = request.get_json(silent=True) or request.form
    # Build kwargs only for provided fields so unset fields are untouched.
    kwargs = {}
    if 'full_name' in payload:
        kwargs['full_name'] = payload.get('full_name')
    if 'role' in payload and payload.get('role'):
        kwargs['role'] = payload.get('role')
    if payload.get('password'):
        kwargs['password'] = payload.get('password')
    if 'is_active' in payload:
        kwargs['is_active'] = str(payload.get('is_active')).lower() in ('1', 'true', 'yes', 'on')
    try:
        u = db.update_user(user_id, **kwargs)
        changed = sorted(kwargs.keys())
        if 'password' in changed:
            changed = [c if c != 'password' else 'password(reset)' for c in changed]
        record_activity('user.update', target=str(user_id),
                        detail={'username': u['username'], 'changed': changed},
                        changes={'op': 'update', 'fields': changed})
        return jsonify({'ok': True, 'user': u})
    except db.UserError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@roles_required('root', 'admin')
def api_users_delete(user_id):
    # Prevent deleting yourself to avoid lockout confusion.
    if session.get('uid') == user_id:
        return jsonify({'ok': False, 'error': 'Tidak dapat menghapus akun yang sedang login.'}), 400
    try:
        victim = db.get_user(user_id)
        db.delete_user(user_id)
        record_activity('user.delete', target=str(user_id),
                        detail={'username': victim['username'] if victim else None},
                        changes={'op': 'delete',
                                 'user': victim['username'] if victim else None})
        return jsonify({'ok': True})
    except db.UserError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


# ─── ACTIVITY LOG API ─────────────────────────────────────────────────────────
# Read-only view of the activity log for the Master User page. Root & admin only.

@app.route('/api/activity', methods=['GET'])
@roles_required('root', 'admin')
def api_activity_list():
    args = request.args

    def _int(name):
        v = args.get(name)
        if v is None or v == '':
            return None
        try:
            return int(v)
        except ValueError:
            return None

    user_id = _int('user_id')
    username = (args.get('username') or '').strip() or None
    action = (args.get('action') or '').strip() or None
    date_from = (args.get('from') or '').strip() or None
    date_to = (args.get('to') or '').strip() or None

    page = _int('page') or 1
    if page < 1:
        page = 1
    page_size = _int('page_size') or 50
    page_size = max(1, min(page_size, 200))
    offset = (page - 1) * page_size

    rows = db.list_activity(
        user_id=user_id, username=username, action=action,
        date_from=date_from, date_to=date_to,
        limit=page_size, offset=offset,
    )
    total = db.count_activity(
        user_id=user_id, username=username, action=action,
        date_from=date_from, date_to=date_to,
    )
    return jsonify({
        'ok': True,
        'items': rows,
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': (total + page_size - 1) // page_size if page_size else 1,
        'actions': db.distinct_activity_actions(),
        'users': [
            {'id': u['id'], 'username': u['username']}
            for u in db.list_users()
        ],
    })


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/download/template-csv')
@login_required
def download_template_csv():
    """Kirim file contoh/template CSV untuk import Monthly Performance."""
    return send_from_directory(
        os.path.dirname(os.path.abspath(__file__)),
        'data.csv',
        as_attachment=True,
        download_name='template_monthly_performance.csv',
        mimetype='text/csv',
    )

@app.route('/api/file')
@login_required
def api_file():
    """Resolve & redirect ke URL file Notion yang SELALU segar (fetch-on-click).

    URL file Notion bertipe 'file' adalah presigned URL S3 yang kedaluwarsa
    ~1 jam, sehingga tidak boleh disimpan di cache. Endpoint ini mengambil
    halaman terbaru dari Notion API saat diklik, membaca file pada
    (field, idx), lalu me-redirect user ke URL yang baru dibuat Notion.

    Query params:
      - page_id : id halaman Notion (wajib)
      - field   : nama property bertipe 'files' (wajib)
      - idx     : indeks file dalam property tsb (opsional, default 0)
    """
    page_id = (request.args.get('page_id') or '').strip()
    field   = (request.args.get('field') or '').strip()
    try:
        idx = int(request.args.get('idx') or 0)
    except (TypeError, ValueError):
        idx = 0

    if not page_id or not field:
        return jsonify({'ok': False, 'error': 'page_id dan field wajib diisi.'}), 400
    if not TOKEN:
        return jsonify({'ok': False, 'error': 'NOTION_TOKEN tidak diset di server.'}), 503

    try:
        page = notion_get(f'https://api.notion.com/v1/pages/{page_id}')
    except Exception as e:  # noqa: BLE001
        return jsonify({'ok': False, 'error': f'Gagal mengambil halaman Notion: {e}'}), 502

    prop  = (page.get('properties', {}) or {}).get(field, {}) or {}
    files = prop.get('files') or []
    if prop.get('type') != 'files' or idx < 0 or idx >= len(files):
        return jsonify({'ok': False, 'error': 'File tidak ditemukan.'}), 404

    f = files[idx]
    ftype = f.get('type')
    if ftype == 'file':
        url = f.get('file', {}).get('url', '')
    elif ftype == 'external':
        url = f.get('external', {}).get('url', '')
    else:
        url = ''

    if not url:
        return jsonify({'ok': False, 'error': 'URL file kosong.'}), 404

    # Redirect langsung ke URL file (presigned fresh untuk type=file).
    return redirect(url)


# Daftar properti bertipe 'files' yang boleh dimodifikasi lewat endpoint upload/
# delete. Membatasi ini mencegah penulisan ke properti sembarang.
_ALLOWED_FILE_FIELDS = set(PROJECT_FILE_FIELDS) | {
    'SPK File', 'File & media',   # SPK
    'Files BAST', 'Files BALP',   # Monthly Performance
}

# Batas ukuran unggah (Direct Upload "small file" Notion = 20 MB).
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _fresh_files(page_id, field):
    """Ambil daftar file TERKINI dari Notion untuk (page_id, field).

    Mengembalikan objek file apa adanya (type file/external/file_upload) yang
    aman ditulis ulang. Return (files_list, error|None).
    """
    try:
        page = notion_get(f'https://api.notion.com/v1/pages/{page_id}')
    except Exception as e:  # noqa: BLE001
        return None, f'Gagal mengambil halaman Notion: {e}'
    prop = (page.get('properties', {}) or {}).get(field, {}) or {}
    if prop.get('type') != 'files':
        return None, 'Properti bukan bertipe files.'
    return list(prop.get('files') or []), None


@app.route('/api/file/upload', methods=['POST'])
@login_required
def api_file_upload():
    """Upload file baru dan lampirkan ke properti 'files' sebuah halaman.

    Form-data: page_id, field, file (biner). File lama dipertahankan; file baru
    ditambahkan di akhir. Mengembalikan daftar nama file terbaru.
    """
    if not TOKEN:
        return jsonify({'ok': False, 'error': 'NOTION_TOKEN belum di-set di server.'}), 400

    page_id = (request.form.get('page_id') or '').strip()
    field   = (request.form.get('field') or '').strip()
    upload  = request.files.get('file')

    if not page_id or not field:
        return jsonify({'ok': False, 'error': 'page_id dan field wajib diisi.'}), 400
    if field not in _ALLOWED_FILE_FIELDS:
        return jsonify({'ok': False, 'error': f'Field tidak diizinkan: {field}'}), 400
    if upload is None or not upload.filename:
        return jsonify({'ok': False, 'error': 'File tidak ada.'}), 400

    file_bytes = upload.read()
    if not file_bytes:
        return jsonify({'ok': False, 'error': 'File kosong.'}), 400
    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        return jsonify({'ok': False, 'error': 'Ukuran file melebihi 20 MB.'}), 400

    filename = upload.filename
    content_type = (
        upload.mimetype
        or mimetypes.guess_type(filename)[0]
        or 'application/octet-stream'
    )

    # 1) buat objek upload, 2) kirim bytes
    try:
        created = notion_create_file_upload(filename, content_type)
        fu_id = created.get('id')
        if not fu_id:
            return jsonify({'ok': False, 'error': 'Gagal membuat file upload.'}), 502
        sent = notion_send_file_upload(fu_id, file_bytes, filename, content_type)
        if sent.get('status') != 'uploaded':
            return jsonify({'ok': False, 'error': f"Status upload tidak 'uploaded': {sent.get('status')}"}), 502
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': f'Notion HTTP {e.code}: {detail}'}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({'ok': False, 'error': f'Gagal upload: {e}'}), 502

    # 3) gabungkan dgn file lama (SEGAR) lalu PATCH
    existing, err = _fresh_files(page_id, field)
    if err:
        return jsonify({'ok': False, 'error': err}), 502
    new_entry = {'type': 'file_upload', 'file_upload': {'id': fu_id}, 'name': filename}
    files_list = existing + [new_entry]
    try:
        notion_set_files_property(page_id, field, files_list)
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': f'Notion HTTP {e.code}: {detail}'}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({'ok': False, 'error': f'Gagal attach: {e}'}), 502

    try:
        sync.incremental_sync()
    except Exception as e:  # noqa: BLE001
        app.logger.warning('Post-upload sync failed: %s', e)

    names = [f.get('name', 'file') for f in files_list]
    return jsonify({'ok': True, 'count': len(files_list), 'files': names})


@app.route('/api/file/delete', methods=['POST'])
@login_required
def api_file_delete():
    """Hapus satu file (berdasarkan idx) dari properti 'files' sebuah halaman."""
    if not TOKEN:
        return jsonify({'ok': False, 'error': 'NOTION_TOKEN belum di-set di server.'}), 400

    payload = request.get_json(silent=True) or {}
    page_id = (payload.get('page_id') or '').strip()
    field   = (payload.get('field') or '').strip()
    try:
        idx = int(payload.get('idx'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'idx tidak valid.'}), 400

    if not page_id or not field:
        return jsonify({'ok': False, 'error': 'page_id dan field wajib diisi.'}), 400
    if field not in _ALLOWED_FILE_FIELDS:
        return jsonify({'ok': False, 'error': f'Field tidak diizinkan: {field}'}), 400

    existing, err = _fresh_files(page_id, field)
    if err:
        return jsonify({'ok': False, 'error': err}), 502
    if idx < 0 or idx >= len(existing):
        return jsonify({'ok': False, 'error': 'Indeks file di luar jangkauan.'}), 404

    files_list = [f for i, f in enumerate(existing) if i != idx]
    try:
        notion_set_files_property(page_id, field, files_list)
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': f'Notion HTTP {e.code}: {detail}'}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({'ok': False, 'error': f'Gagal hapus: {e}'}), 502

    try:
        sync.incremental_sync()
    except Exception as e:  # noqa: BLE001
        app.logger.warning('Post-delete sync failed: %s', e)

    names = [f.get('name', 'file') for f in files_list]
    return jsonify({'ok': True, 'count': len(files_list), 'files': names})


@app.route('/api/data')
@login_required
def api_data():
    from datetime import datetime, timedelta

    _ensure_fresh()  # Option C: refresh cache from Notion only if stale

    personel     = get_personel()
    raw_tasks    = cached_query(TASKS_DB)
    raw_projects = cached_query(PROJECTS_DB)
    raw_spk      = cached_query(SPK_DB)

    tasks    = [extract_task(r, personel)    for r in raw_tasks]
    projects = [extract_project(r, personel) for r in raw_projects]
    spk      = [extract_spk(r, personel)     for r in raw_spk]

    # ── Project ID → title map (untuk label perpanjangan SPK) ──
    proj_map = {}
    for r in raw_projects:
        tp = r['properties'].get('Project name', {}).get('title', [])
        proj_map[r['id']] = tp[0]['plain_text'] if tp else ''

    # ── SPK internal ID → No SPK map ──
    spk_map = {s['_id']: s['no_spk'] for s in spk if s.get('_id')}

    # ── SPK Selesai map (untuk fallback due date proyek) ──
    spk_selesai_map = {s['_id']: s['spk_selesai'] for s in spk if s.get('_id')}

    # ── Selesaikan field perpanjangan per SPK ──
    # Reverse map: project page_id → daftar SPK perpanjangan yang menunjuk ke
    # project tsb (dibangun dari sisi SPK.Projects Perpanjangan).
    proj_to_spk = {}
    for s in spk:
        for pid in s['perp_ids']:
            proj_to_spk.setdefault(pid, []).append({
                'no_spk':      s['no_spk'],
                'status':      s['status'],
                'spk_selesai': s['spk_selesai'],
                'page_id':     s['_id'],
            })

    for s in spk:
        s['perpanjangan'] = [
            {
                'title':  proj_map.get(pid, '?'),
                'status': s['status_perpanjangan'][i] if i < len(s['status_perpanjangan']) else ''
            }
            for i, pid in enumerate(s['perp_ids'])
        ]
        # Simpan page_id project perpanjangan agar form edit bisa pre-select
        # relasi saat ini (hanya yang pertama dipakai oleh dropdown single-select).
        s['perp_project_ids'] = list(s['perp_ids'])
        del s['perp_ids'], s['status_perpanjangan'], s['_id']

    # Attach daftar SPK perpanjangan ke tiap project (relasi balik).
    for p in projects:
        p['spk_perpanjangan'] = proj_to_spk.get(p.get('page_id'), [])

    # ── Resolve due date proyek dari SPK sebelumnya jika kosong ──
    for p in projects:
        if not p['due']:
            # Cari SPK sebelumnya yang terkait via SPK DB Projects Asal
            pass  # due date diambil dari field Dates proyek itu sendiri

    # ── Hitung remaining_days proyek ──
    today_dt = datetime.now().date()
    for p in projects:
        if p['due']:
            p['remaining_days'] = (datetime.strptime(p['due'], '%Y-%m-%d').date() - today_dt).days
        else:
            p['remaining_days'] = None

    # ═══════════════════════════════════════
    # TASK STATISTICS
    # ═══════════════════════════════════════
    task_status   = defaultdict(int)
    created_monthly = defaultdict(int)
    done_monthly    = defaultdict(int)
    person_done   = defaultdict(int)
    person_total  = defaultdict(int)
    overdue = on_track = no_due = 0
    today_str = datetime.now().strftime('%Y-%m-%d')

    for t in tasks:
        task_status[t['status']] += 1
        if t['created']:  created_monthly[t['created'][:7]] += 1
        if t['done_date']: done_monthly[t['done_date'][:7]]  += 1
        for a in t['assignees']:
            person_total[a] += 1
            if t['status'] == 'Done':
                person_done[a] += 1
        if t['status'] != 'Done':
            if not t['due']:
                no_due += 1
            elif t['due'] < today_str:
                overdue += 1
            else:
                on_track += 1

    # Monthly backlog
    months  = sorted(set(list(created_monthly) + list(done_monthly)))
    monthly = []
    cum_c = cum_d = 0
    for m in months:
        cum_c += created_monthly[m]
        cum_d += done_monthly[m]
        monthly.append({
            'month':   m,
            'created': created_monthly[m],
            'done':    done_monthly[m],
            'backlog': cum_c - cum_d,
        })

    # Daily progress
    daily_done    = defaultdict(int)
    daily_created = defaultdict(int)
    for t in tasks:
        if t['done_date']: daily_done[t['done_date']]    += 1
        if t['created']:   daily_created[t['created']]   += 1
    all_days = sorted(set(list(daily_done) + list(daily_created)))
    daily    = []
    cum_done = 0
    for day in all_days:
        active = sum(
            1 for t in tasks
            if t['created'] <= day and (
                t['status'] != 'Done' or (t['done_date'] and t['done_date'] > day)
            )
        )
        cum_done += daily_done[day]
        daily.append({
            'date':       day,
            'active':     active,
            'done_cumul': cum_done,
            'done_day':   daily_done[day],
        })

    # ═══════════════════════════════════════
    # PROJECT STATISTICS
    # ═══════════════════════════════════════
    proj_status = defaultdict(int)
    proj_person = defaultdict(lambda: defaultdict(int))
    for p in projects:
        proj_status[p['status']] += 1
        for a in p['assignees']:
            proj_person[a][p['status']] += 1

    # ═══════════════════════════════════════
    # SPK STATISTICS
    # ═══════════════════════════════════════
    spk_status      = defaultdict(int)
    spk_jenis       = defaultdict(int)
    spk_klasifikasi = defaultdict(int)
    spk_tipe        = defaultdict(int)
    total_anggaran_all = 0
    total_terbayar_all = 0

    for s in spk:
        spk_status[s['status']] += 1
        spk_jenis[s['jenis_anggaran']] += 1
        spk_klasifikasi[s['klasifikasi']] += 1
        spk_tipe[s['tipe']] += 1
        if s['total_anggaran']: total_anggaran_all += s['total_anggaran']
        if s['total_terbayar']: total_terbayar_all += s['total_terbayar']

    return jsonify({
        # Data utama
        'tasks':    tasks,
        'projects': projects,
        'spk':      spk,
        'personel': list(personel.values()),
        # Peta nama → page_id personel (untuk autocomplete assignee di form Task).
        'personel_map': {name: pid for pid, name in personel.items()},

        # Task stats
        'task_status':  dict(task_status),
        'monthly':      monthly,
        'daily':        daily,
        'person_done':  dict(person_done),
        'person_total': dict(person_total),
        'overdue':      overdue,
        'on_track':     on_track,
        'no_due':       no_due,

        # Project stats
        'proj_status': dict(proj_status),
        'proj_person': {k: dict(v) for k, v in proj_person.items()},

        # SPK stats
        'spk_status':         dict(spk_status),
        'spk_jenis':          dict(spk_jenis),
        'spk_klasifikasi':    dict(spk_klasifikasi),
        'spk_tipe':           dict(spk_tipe),
        'total_anggaran_all': total_anggaran_all,
        'total_terbayar_all': total_terbayar_all,
    })


@app.route('/api/monthly')
@login_required
def api_monthly():
    """
    Endpoint Monthly Performance dengan FILTER SERVER-SIDE.

    Mode:
      - ?options=1
            Kembalikan HANYA daftar opsi filter (periodes/invoices/dst.) tanpa
            baris. Dipakai frontend untuk mengisi dropdown sebelum load, agar
            user bisa memilih filter dulu.
      - dengan parameter filter (search/periode/invoice/bayar/ba/rekon)
            Kembalikan hanya baris yang cocok + agregat. Ini membuat load cepat
            karena tidak mengirim seluruh 10rb baris ke browser.

    Semantik filter dibuat identik dengan applyMonthlyFilters() di frontend.
    """
    if not TOKEN:
        app.logger.warning('NOTION_TOKEN not set; serving cached Monthly Performance.')
    else:
        _ensure_fresh()  # Option C: refresh cache from Notion only if stale

    # Peta internal SPK page-id → No SPK (untuk menampilkan No SPK dari relation)
    spk_no_map = {}
    for r in cached_query(SPK_DB):
        arr = r['properties'].get('No SPK', {}).get('title', [])
        spk_no_map[r['id']] = arr[0]['plain_text'] if arr else ''

    all_rows = [extract_monthly(r, spk_no_map) for r in cached_query(MONTHLY_DB)]

    # ── Options-only mode: kirim daftar opsi filter, tanpa baris ──
    if request.args.get('options') in ('1', 'true', 'yes'):
        def uniq_sorted(key):
            return sorted({(x.get(key) or '') for x in all_rows if x.get(key)})
        periodes = sorted({x['periode'][:7] for x in all_rows if x['periode']}, reverse=True)
        return jsonify({
            'ok': True,
            'options': True,
            'total_available': len(all_rows),
            'periodes':  periodes,
            'invoices':  uniq_sorted('status_invoice'),
            'bayars':    uniq_sorted('status_bayar'),
            'bas':       uniq_sorted('status_ba'),
            'rekons':    uniq_sorted('status_rekon'),
        })

    # ── Baca parameter filter ──
    q       = (request.args.get('search') or '').strip().lower()
    periode = request.args.get('periode') or 'all'
    inv     = request.args.get('invoice') or 'all'
    bayar   = request.args.get('bayar')   or 'all'
    ba      = request.args.get('ba')      or 'all'
    rekon   = request.args.get('rekon')   or 'all'

    # ── Terapkan filter di server (semantik = frontend applyMonthlyFilters) ──
    rows = all_rows
    if periode != 'all':
        rows = [x for x in rows if (x.get('periode') or '').startswith(periode)]
    if inv != 'all':
        rows = [x for x in rows if x.get('status_invoice') == inv]
    if bayar != 'all':
        rows = [x for x in rows if x.get('status_bayar') == bayar]
    if ba != 'all':
        rows = [x for x in rows if x.get('status_ba') == ba]
    if rekon != 'all':
        rows = [x for x in rows if x.get('status_rekon') == rekon]
    if q:
        rows = [
            x for x in rows
            if q in ((x.get('name') or '') + (x.get('no_spk') or '') + (x.get('dok_lp') or '') + (x.get('no_invoice') or '') + (x.get('keterangan') or '')).lower()
        ]

    # Ringkasan agregat (atas hasil terfilter)
    total_tagihan  = sum(x['nilai_tagihan'] or 0 for x in rows)
    total_prognosa = sum(x['prognosa'] or 0 for x in rows)

    # Opsi filter unik (dari SELURUH data, agar dropdown tetap lengkap)
    periodes = sorted({x['periode'][:7] for x in all_rows if x['periode']}, reverse=True)

    return jsonify({
        'ok': True,
        'monthly': rows,
        'count': len(rows),
        'total_available': len(all_rows),
        'total_tagihan': total_tagihan,
        'total_prognosa': total_prognosa,
        'periodes': periodes,
    })


# ─── UPDATE satu baris Monthly Performance ────────────────────────────────────
# Dipanggil dari dialog edit di frontend saat user mengklik Name pada tabel.
# Menerima JSON {page_id, ...field}. Hanya field yang dikirim yang diubah.
# Field yang dapat diedit di sini (Files & relation SPK dikelola langsung di
# Notion, tidak lewat dialog ini).

# Peta field frontend → (nama properti Notion, tipe).
_MONTHLY_EDITABLE = {
    'name':           ('Name', 'title'),
    'nilai_tagihan':  ('Nilai Tagihan', 'number'),
    'prognosa':       ('Prognosa', 'number'),
    'status_invoice': ('Status Invoice', 'select'),
    'status_bayar':   ('Status Pembayaran', 'select'),
    'status_ba':      ('Status BA Performansi', 'select'),
    'status_rekon':   ('Status Rekon', 'select'),
    'no_invoice':     ('No. Invoice', 'rich_text'),
    'dok_lp':         ('No. Dokumen BA LP', 'rich_text'),
    'keterangan':     ('Keterangan', 'rich_text'),
    'periode':        ('Periode', 'date_month'),
    'tgl_serah_ba':   ('Tanggal Serah BA Performansi', 'date'),
    'tgl_masuk_ba':   ('Tanggal masuk BA Performansi', 'date'),
    'tgl_rekon':      ('Tanggal Rekon', 'date'),
}


@app.route('/api/monthly/update', methods=['POST'])
@login_required
def api_monthly_update():
    if not TOKEN:
        return jsonify({'ok': False, 'error': 'NOTION_TOKEN belum di-set di server.'}), 400

    payload = request.get_json(silent=True) or {}
    page_id = (payload.get('page_id') or '').strip()
    if not page_id:
        return jsonify({'ok': False, 'error': 'page_id wajib diisi.'}), 400

    props = {}
    errors = []
    for fkey, (notion_name, ftype) in _MONTHLY_EDITABLE.items():
        if fkey not in payload:
            continue  # hanya ubah field yang dikirim
        raw = payload.get(fkey)
        val = ('' if raw is None else str(raw)).strip()

        if ftype == 'title':
            props[notion_name] = {'title': [{'text': {'content': val}}] if val else []}
        elif ftype == 'rich_text':
            props[notion_name] = {'rich_text': [{'text': {'content': val}}] if val else []}
        elif ftype == 'number':
            if val == '':
                props[notion_name] = {'number': None}
            else:
                # Terima format ribuan Indonesia (titik/koma/spasi sbg pemisah).
                digits = val.replace('.', '').replace(',', '').replace(' ', '')
                try:
                    props[notion_name] = {'number': float(digits)}
                except ValueError:
                    errors.append(f"'{notion_name}' bukan angka valid: {raw!r}")
        elif ftype == 'select':
            props[notion_name] = {'select': {'name': val} if val else None}
        elif ftype == 'date':
            if val == '':
                props[notion_name] = {'date': None}
            else:
                iso, derr = normalize_date(val)
                if iso:
                    props[notion_name] = {'date': {'start': iso}}
                else:
                    errors.append(f"'{notion_name}': {derr}")
        elif ftype == 'date_month':
            # Periode disimpan sebagai rentang satu bulan penuh (konsisten dgn import).
            if val == '':
                props[notion_name] = {'date': None}
            else:
                iso, derr = normalize_date(val)
                if iso:
                    start, end = month_range(iso)
                    props[notion_name] = {'date': {'start': start, 'end': end}}
                else:
                    errors.append(f"'{notion_name}': {derr}")

    if errors:
        return jsonify({'ok': False, 'error': 'Validasi gagal.', 'errors': errors}), 400
    if not props:
        return jsonify({'ok': False, 'error': 'Tidak ada field yang diubah.'}), 400

    # PATCH ke Notion
    try:
        notion_patch(f'https://api.notion.com/v1/pages/{page_id}', {'properties': props})
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': f'Notion HTTP {e.code}: {detail}'}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({'ok': False, 'error': f'Gagal update: {e}'}), 502

    audit_version = _record_audit(page_id, action='update')

    # Refresh cache agar dashboard mencerminkan perubahan (best-effort).
    try:
        sync.incremental_sync()
    except Exception as e:  # noqa: BLE001
        app.logger.warning('Post-update sync failed: %s', e)

    return jsonify({'ok': True, 'updated': list(props.keys()),
                    'audit_version': audit_version})

# ─── Shared: bangun properties Notion dari peta editable ───────────────────────
def _build_props_from_map(editable_map, payload, only_present=True):
    """Bangun dict `properties` Notion dari peta editable + payload frontend.

    Mendukung tipe: title, rich_text, select, status, number, date.
    - only_present=True (update): hanya proses field yang ADA di payload.
    - only_present=False (create): proses semua field di peta (yang kosong
      dilewati utk status/select agar tidak kirim null tak perlu).
    Return (props, errors).
    """
    props, errors = {}, []
    for fkey, (notion_name, ftype) in editable_map.items():
        if only_present and fkey not in payload:
            continue
        raw = payload.get(fkey)
        val = ('' if raw is None else str(raw)).strip()

        if ftype == 'title':
            props[notion_name] = {'title': [{'text': {'content': val}}] if val else []}
        elif ftype == 'rich_text':
            props[notion_name] = {'rich_text': [{'text': {'content': val}}] if val else []}
        elif ftype == 'select':
            if val:
                props[notion_name] = {'select': {'name': val}}
            elif only_present:
                props[notion_name] = {'select': None}
        elif ftype == 'status':
            # Status tidak boleh null; hanya set bila ada nilai.
            if val:
                props[notion_name] = {'status': {'name': val}}
        elif ftype == 'relation':
            # Nilai = satu page_id (atau kosong untuk mengosongkan relasi).
            if val:
                props[notion_name] = {'relation': [{'id': val}]}
            elif only_present:
                props[notion_name] = {'relation': []}
        elif ftype == 'relation_multi':
            # Nilai = page_id dipisah koma (atau kosong untuk mengosongkan).
            ids = [x.strip() for x in val.split(',') if x.strip()]
            if ids:
                props[notion_name] = {'relation': [{'id': x} for x in ids]}
            elif only_present:
                props[notion_name] = {'relation': []}
        elif ftype == 'multi_select':
            # Nilai = nama opsi dipisah koma.
            names = [x.strip() for x in val.split(',') if x.strip()]
            if names:
                props[notion_name] = {'multi_select': [{'name': n} for n in names]}
            elif only_present:
                props[notion_name] = {'multi_select': []}
        elif ftype == 'progress':
            # Frontend mengirim 0–100; Notion menyimpan 0.0–1.0.
            if val == '':
                if only_present:
                    props[notion_name] = {'number': None}
            else:
                try:
                    pct = float(val.replace(',', '.'))
                    props[notion_name] = {'number': max(0.0, min(1.0, pct / 100.0))}
                except ValueError:
                    errors.append(f"'{notion_name}' bukan angka valid: {raw!r}")
        elif ftype == 'number':
            if val == '':
                if only_present:
                    props[notion_name] = {'number': None}
            else:
                # Terima format ribuan Indonesia (mis. "1.000.000" / "1 000 000").
                # Titik, koma, dan spasi diperlakukan sebagai pemisah ribuan dan
                # dibuang; nilai Rupiah di sini berupa bilangan bulat.
                digits = val.replace('.', '').replace(',', '').replace(' ', '')
                try:
                    props[notion_name] = {'number': float(digits)}
                except ValueError:
                    errors.append(f"'{notion_name}' bukan angka valid: {raw!r}")
        elif ftype == 'date':
            if val == '':
                if only_present:
                    props[notion_name] = {'date': None}
            else:
                iso, derr = normalize_date(val)
                if iso:
                    props[notion_name] = {'date': {'start': iso}}
                else:
                    errors.append(f"'{notion_name}': {derr}")
    return props, errors


def _create_notion_page(db_id, props):
    """Buat halaman baru di database Notion. Return (ok, result_or_error, http_code)."""
    try:
        res = notion_post('https://api.notion.com/v1/pages',
                          {'parent': {'database_id': db_id}, 'properties': props})
        return True, res, 200
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode()
        except Exception:
            pass
        return False, f'Notion HTTP {e.code}: {detail}', 502
    except Exception as e:  # noqa: BLE001
        return False, f'Gagal membuat halaman: {e}', 502


# ─── Project Submission: edit → Notion ─────────────────────────────────────────
# Peta field frontend → (nama properti Notion, tipe). Hanya field skalar yang
# aman diedit. 'Completion' adalah rollup (read-only) dan 'Assignee' adalah
# relation (dikelola di Notion), jadi tidak disertakan di sini.
# 10 dokumen di bawah bertipe 'status' di Notion.
_PROJECT_EDITABLE = {
    'title':    ('Project name', 'title'),
    'status':   ('Status', 'status'),
    'priority': ('Priority', 'select'),
    'due':      ('Dates', 'date'),
    'nilai_project': ('Nominal IP', 'number'),
    'no_izin_prinsip': ('No. Izin Prinsip', 'rich_text'),
    'spk_sebelumnya':  ('SPK sebelumnya', 'relation'),
    'pic':             ('Assignee', 'relation_multi'),
    # Dokumen (status). Key frontend memakai nama properti apa adanya.
    'TOR':                                 ('TOR', 'status'),
    'FS (Feasibility Study)':              ('FS (Feasibility Study)', 'status'),
    'Izin Prinsip':                        ('Izin Prinsip', 'status'),
    'Izin Anggaran':                       ('Izin Anggaran', 'status'),
    'Penilaian Teknis':                    ('Penilaian Teknis', 'status'),
    'PI (Pakta Integritas)':               ('PI (Pakta Integritas)', 'status'),
    'TPRA (Third Party Risk Assesment)':   ('TPRA (Third Party Risk Assesment)', 'status'),
    'BenchMark':                           ('BenchMark', 'status'),
    'Aanwidjzing':                         ('Aanwidjzing', 'status'),
    'Risk Assessment':                     ('Risk Assessment', 'status'),
}


@app.route('/api/project/update', methods=['POST'])
@login_required
def api_project_update():
    """Update satu Project Submission ke Notion (PATCH).

    Hanya field yang dikirim di payload yang diubah. Mendukung tipe:
    title, select, status, date. page_id wajib.
    """
    if not TOKEN:
        return jsonify({'ok': False, 'error': 'NOTION_TOKEN belum di-set di server.'}), 400

    payload = request.get_json(silent=True) or {}
    page_id = (payload.get('page_id') or '').strip()
    if not page_id:
        return jsonify({'ok': False, 'error': 'page_id wajib diisi.'}), 400

    props, errors = _build_props_from_map(_PROJECT_EDITABLE, payload, only_present=True)

    if errors:
        return jsonify({'ok': False, 'error': 'Validasi gagal.', 'errors': errors}), 400
    if not props:
        return jsonify({'ok': False, 'error': 'Tidak ada field yang diubah.'}), 400

    try:
        notion_patch(f'https://api.notion.com/v1/pages/{page_id}', {'properties': props})
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': f'Notion HTTP {e.code}: {detail}'}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({'ok': False, 'error': f'Gagal update: {e}'}), 502

    # Catat snapshot SEMUA properti (setelah perubahan) + user + waktu ke
    # audit trail di body halaman. Best-effort; tidak menjatuhkan request.
    audit_version = _record_audit(page_id, action='update')

    # Refresh cache agar dashboard mencerminkan perubahan (best-effort).
    try:
        sync.incremental_sync()
    except Exception as e:  # noqa: BLE001
        app.logger.warning('Post-update sync failed: %s', e)

    return jsonify({'ok': True, 'updated': list(props.keys()),
                    'audit_version': audit_version})


# ─── Project Audit Trail (body halaman) ───────────────────────────────────────

@app.route('/api/project/audit', methods=['GET'])
@app.route('/api/audit', methods=['GET'])
@login_required
def api_project_audit():
    """Baca riwayat versi audit trail untuk satu project.

    Query param: page_id. Return list versi (terbaru di akhir).
    """
    if not TOKEN:
        return jsonify({'ok': False, 'error': 'NOTION_TOKEN belum di-set di server.'}), 400
    page_id = (request.args.get('page_id') or '').strip()
    if not page_id:
        return jsonify({'ok': False, 'error': 'page_id wajib diisi.'}), 400
    try:
        versions = audit_trail.read_versions(audit_blocks, page_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({'ok': False, 'error': f'Gagal baca audit: {e}'}), 502
    return jsonify({'ok': True, 'page_id': page_id,
                    'count': len(versions), 'versions': versions})


@app.route('/api/project/audit/compare', methods=['GET'])
@app.route('/api/audit/compare', methods=['GET'])
@login_required
def api_project_audit_compare():
    """Bandingkan dua versi audit. Query: page_id, a (version), b (version)."""
    if not TOKEN:
        return jsonify({'ok': False, 'error': 'NOTION_TOKEN belum di-set di server.'}), 400
    page_id = (request.args.get('page_id') or '').strip()
    try:
        va = int(request.args.get('a', ''))
        vb = int(request.args.get('b', ''))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Parameter a & b (nomor versi) wajib angka.'}), 400
    if not page_id:
        return jsonify({'ok': False, 'error': 'page_id wajib diisi.'}), 400
    try:
        diff = audit_trail.compare_versions(audit_blocks, page_id, va, vb)
    except audit_trail.AuditError as e:
        return jsonify({'ok': False, 'error': str(e)}), 404
    except Exception as e:  # noqa: BLE001
        return jsonify({'ok': False, 'error': f'Gagal compare: {e}'}), 502
    return jsonify({'ok': True, 'page_id': page_id, 'a': va, 'b': vb, 'diff': diff})


@app.route('/api/project/audit/rollback', methods=['POST'])
@app.route('/api/audit/rollback', methods=['POST'])
@login_required
def api_project_audit_rollback():
    """Terapkan kembali versi lama ke properti project di Notion.

    Body JSON: {page_id, version}. Membangun payload Update Page dari snapshot
    versi tersebut (hanya properti writable), PATCH ke Notion, lalu mencatat
    satu versi audit BARU dengan action='rollback' (sejarah tidak dihapus).
    """
    if not TOKEN:
        return jsonify({'ok': False, 'error': 'NOTION_TOKEN belum di-set di server.'}), 400
    payload = request.get_json(silent=True) or {}
    page_id = (payload.get('page_id') or '').strip()
    version = payload.get('version')
    if not page_id:
        return jsonify({'ok': False, 'error': 'page_id wajib diisi.'}), 400
    try:
        version = int(version)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'version wajib angka.'}), 400

    entry = audit_trail.get_version(audit_blocks, page_id, version)
    if entry is None:
        return jsonify({'ok': False, 'error': f'Versi {version} tidak ditemukan.'}), 404

    props, skipped = audit_trail.build_rollback_payload(entry.get('properties', {}))
    if not props:
        return jsonify({'ok': False, 'error': 'Tidak ada properti writable untuk di-rollback.'}), 400

    try:
        notion_patch(f'https://api.notion.com/v1/pages/{page_id}', {'properties': props})
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': f'Notion HTTP {e.code}: {detail}'}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({'ok': False, 'error': f'Gagal rollback: {e}'}), 502

    # Catat versi baru hasil rollback (snapshot kondisi setelah apply).
    new_version = _record_audit(page_id, action='rollback')

    try:
        sync.incremental_sync()
    except Exception as e:  # noqa: BLE001
        app.logger.warning('Post-rollback sync failed: %s', e)

    return jsonify({'ok': True, 'rolled_back_to': version,
                    'applied': list(props.keys()), 'skipped': skipped,
                    'new_audit_version': new_version})


@app.route('/api/refresh', methods=['POST'])
@login_required
def api_refresh():
    """Paksa tarik data terbaru dari Notion (incremental sync) ke cache lokal.

    Dipakai saat membuka detail agar data yang ditampilkan 'segar dari sumber'
    (Opsi 1), tanpa menunggu TTL cache. Query/body opsional: mode=full untuk
    full sync. Best-effort: kegagalan dikembalikan sebagai ok=False tapi tidak
    menjatuhkan server.
    """
    if not TOKEN:
        return jsonify({'ok': False, 'error': 'NOTION_TOKEN belum di-set di server.'}), 400
    payload = request.get_json(silent=True) or {}
    mode = (payload.get('mode') or request.args.get('mode') or 'incremental').strip()
    try:
        if mode == 'full':
            summary = sync.full_sync()
        else:
            summary = sync.incremental_sync()
    except Exception as e:  # noqa: BLE001
        app.logger.warning('Manual refresh failed: %s', e)
        return jsonify({'ok': False, 'error': f'Refresh gagal: {e}'}), 502
    return jsonify({'ok': True, 'mode': mode, 'summary': summary})


# ─── SPK: edit → Notion ────────────────────────────────────────────────────────
# Peta field frontend → (nama properti Notion, tipe). Hanya field skalar yang
# aman diedit. 'Total Terbayar' adalah rollup, 'Sisa Anggaran'/'Sisa Hari'
# adalah formula (read-only), dan 'Vendor' adalah relation (dikelola di Notion),
# jadi tidak disertakan.
_SPK_EDITABLE = {
    'no_spk':          ('No SPK', 'title'),
    'project':         ('Project Name', 'rich_text'),
    'status':          ('Status', 'status'),
    'jenis_anggaran':  ('Jenis Anggaran', 'select'),
    'klasifikasi':     ('Klasifikasi Pengadaan', 'select'),
    'tipe':            ('Baru-Sisa Bayar-Perpanjangan', 'select'),
    'total_anggaran':  ('Total Anggaran', 'number'),
    'spk_mulai':       ('SPK Mulai', 'date'),
    'spk_selesai':     ('SPK Selesai', 'date'),
    'notes':           ('Notes', 'rich_text'),
    'perp_project':    ('Projects Perpanjangan', 'relation'),
}


@app.route('/api/spk/update', methods=['POST'])
@login_required
def api_spk_update():
    """Update satu baris SPK ke Notion (PATCH).

    Hanya field yang dikirim di payload yang diubah. Mendukung tipe:
    title, rich_text, status, select, number, date. page_id wajib.
    """
    if not TOKEN:
        return jsonify({'ok': False, 'error': 'NOTION_TOKEN belum di-set di server.'}), 400

    payload = request.get_json(silent=True) or {}
    page_id = (payload.get('page_id') or '').strip()
    if not page_id:
        return jsonify({'ok': False, 'error': 'page_id wajib diisi.'}), 400

    props, errors = _build_props_from_map(_SPK_EDITABLE, payload, only_present=True)

    if errors:
        return jsonify({'ok': False, 'error': 'Validasi gagal.', 'errors': errors}), 400
    if not props:
        return jsonify({'ok': False, 'error': 'Tidak ada field yang diubah.'}), 400

    try:
        notion_patch(f'https://api.notion.com/v1/pages/{page_id}', {'properties': props})
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': f'Notion HTTP {e.code}: {detail}'}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({'ok': False, 'error': f'Gagal update: {e}'}), 502

    audit_version = _record_audit(page_id, action='update')

    # Refresh cache agar dashboard mencerminkan perubahan (best-effort).
    try:
        sync.incremental_sync()
    except Exception as e:  # noqa: BLE001
        app.logger.warning('Post-update sync failed: %s', e)

    return jsonify({'ok': True, 'updated': list(props.keys()),
                    'audit_version': audit_version})

# ─── Create: SPK & Project baru → Notion ───────────────────────────────────────
@app.route('/api/spk/create', methods=['POST'])
@login_required
def api_spk_create():
    """Buat SPK baru di Notion. Wajib: no_spk (title)."""
    if not TOKEN:
        return jsonify({'ok': False, 'error': 'NOTION_TOKEN belum di-set di server.'}), 400

    payload = request.get_json(silent=True) or {}
    if not (payload.get('no_spk') or '').strip():
        return jsonify({'ok': False, 'error': 'No SPK wajib diisi.'}), 400

    props, errors = _build_props_from_map(_SPK_EDITABLE, payload, only_present=False)
    if errors:
        return jsonify({'ok': False, 'error': 'Validasi gagal.', 'errors': errors}), 400
    if 'No SPK' not in props:
        return jsonify({'ok': False, 'error': 'No SPK wajib diisi.'}), 400

    ok, res, code = _create_notion_page(SPK_DB, props)
    if not ok:
        return jsonify({'ok': False, 'error': res}), code

    try:
        sync.incremental_sync()
    except Exception as e:  # noqa: BLE001
        app.logger.warning('Post-create sync failed: %s', e)

    return jsonify({'ok': True, 'page_id': res.get('id'), 'created': list(props.keys())})


@app.route('/api/project/create', methods=['POST'])
@login_required
def api_project_create():
    """Buat Project Submission baru di Notion. Wajib: title (Project name)."""
    if not TOKEN:
        return jsonify({'ok': False, 'error': 'NOTION_TOKEN belum di-set di server.'}), 400

    payload = request.get_json(silent=True) or {}
    if not (payload.get('title') or '').strip():
        return jsonify({'ok': False, 'error': 'Nama Project wajib diisi.'}), 400

    props, errors = _build_props_from_map(_PROJECT_EDITABLE, payload, only_present=False)
    if errors:
        return jsonify({'ok': False, 'error': 'Validasi gagal.', 'errors': errors}), 400
    if 'Project name' not in props:
        return jsonify({'ok': False, 'error': 'Nama Project wajib diisi.'}), 400

    ok, res, code = _create_notion_page(PROJECTS_DB, props)
    if not ok:
        return jsonify({'ok': False, 'error': res}), code

    try:
        sync.incremental_sync()
    except Exception as e:  # noqa: BLE001
        app.logger.warning('Post-create sync failed: %s', e)

    return jsonify({'ok': True, 'page_id': res.get('id'), 'created': list(props.keys())})






# ─── Tasks: create / update / delete → Notion ──────────────────────────────────
# Peta field frontend → (nama properti Notion, tipe).
#   progress: frontend kirim 0–100, disimpan Notion 0.0–1.0.
#   assignee: relation ke Personel DB (bisa banyak, page_id dipisah koma).
#   tags: multi_select.
_TASK_EDITABLE = {
    'name':      ('Task name', 'title'),
    'status':    ('Status', 'status'),
    'due':       ('Due Date', 'date'),
    'priority':  ('Priority', 'select'),
    'progress':  ('Progress', 'progress'),
    'assignee':  ('Assignee relation', 'relation_multi'),
    'tags':      ('Tags', 'multi_select'),
}


@app.route('/api/task/create', methods=['POST'])
@login_required
def api_task_create():
    """Buat Task baru di Notion. Wajib: name (Task name)."""
    if not TOKEN:
        return jsonify({'ok': False, 'error': 'NOTION_TOKEN belum di-set di server.'}), 400

    payload = request.get_json(silent=True) or {}
    if not (payload.get('name') or '').strip():
        return jsonify({'ok': False, 'error': 'Nama Task wajib diisi.'}), 400

    props, errors = _build_props_from_map(_TASK_EDITABLE, payload, only_present=False)
    if errors:
        return jsonify({'ok': False, 'error': 'Validasi gagal.', 'errors': errors}), 400
    if 'Task name' not in props:
        return jsonify({'ok': False, 'error': 'Nama Task wajib diisi.'}), 400

    ok, res, code = _create_notion_page(TASKS_DB, props)
    if not ok:
        return jsonify({'ok': False, 'error': res}), code

    try:
        sync.incremental_sync()
    except Exception as e:  # noqa: BLE001
        app.logger.warning('Post-create sync failed: %s', e)

    return jsonify({'ok': True, 'page_id': res.get('id'), 'created': list(props.keys())})


@app.route('/api/task/update', methods=['POST'])
@login_required
def api_task_update():
    """Update satu Task ke Notion (PATCH). Hanya field yang dikirim diubah."""
    if not TOKEN:
        return jsonify({'ok': False, 'error': 'NOTION_TOKEN belum di-set di server.'}), 400

    payload = request.get_json(silent=True) or {}
    page_id = (payload.get('page_id') or '').strip()
    if not page_id:
        return jsonify({'ok': False, 'error': 'page_id wajib diisi.'}), 400

    props, errors = _build_props_from_map(_TASK_EDITABLE, payload, only_present=True)
    if errors:
        return jsonify({'ok': False, 'error': 'Validasi gagal.', 'errors': errors}), 400
    if not props:
        return jsonify({'ok': False, 'error': 'Tidak ada field yang diubah.'}), 400

    try:
        notion_patch(f'https://api.notion.com/v1/pages/{page_id}', {'properties': props})
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': f'Notion HTTP {e.code}: {detail}'}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({'ok': False, 'error': f'Gagal update: {e}'}), 502

    audit_version = _record_audit(page_id, action='update')

    try:
        sync.incremental_sync()
    except Exception as e:  # noqa: BLE001
        app.logger.warning('Post-update sync failed: %s', e)

    return jsonify({'ok': True, 'updated': list(props.keys()),
                    'audit_version': audit_version})


@app.route('/api/task/delete', methods=['POST'])
@login_required
def api_task_delete():
    """Hapus (archive) sebuah Task di Notion. Notion API tidak punya hard delete,
    jadi halaman di-archive (archived=true) sehingga hilang dari tampilan."""
    if not TOKEN:
        return jsonify({'ok': False, 'error': 'NOTION_TOKEN belum di-set di server.'}), 400

    payload = request.get_json(silent=True) or {}
    page_id = (payload.get('page_id') or '').strip()
    if not page_id:
        return jsonify({'ok': False, 'error': 'page_id wajib diisi.'}), 400

    try:
        notion_patch(f'https://api.notion.com/v1/pages/{page_id}', {'archived': True})
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': f'Notion HTTP {e.code}: {detail}'}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({'ok': False, 'error': f'Gagal hapus: {e}'}), 502

    try:
        sync.full_sync()  # full sync agar baris terhapus hilang dari cache
    except Exception as e:  # noqa: BLE001
        app.logger.warning('Post-delete sync failed: %s', e)

    return jsonify({'ok': True})

# ─── CSV IMPORT → Monthly Performance ──────────────────────────────────────────
# Kolom yang didukung di CSV (header harus sama persis):
#   Name (title, wajib), Periode DD-MM-YYYY (date → properti 'Periode'),
#   Nilai Tagihan (number), Prognosa (number),
#   Status Invoice / Status Pembayaran / Status BA Performansi / Status Rekon (select),
#   Tanggal Serah BA Performansi / Tanggal masuk BA Performansi / Tanggal Rekon (date),
#   No. Dokumen BA LP (rich_text),
#   No SPK (wajib → dicocokkan ke database SPK untuk mengisi relation '📋 SPK')
#
# Semua kolom tanggal memakai format DD-MM-YYYY (mis. 01-08-2026).
# Catatan: field 'ID' (unique_id), 'Files BAST' & 'Files BALP' (files) tidak dapat
# diisi lewat CSV — ID di-generate otomatis oleh Notion, file harus diunggah manual.

# Pemetaan header CSV → nama properti Notion. Header di CSV boleh berbeda dari
# nama properti (mis. untuk memberi petunjuk format), tetapi harus dipetakan
# balik ke nama properti Notion yang sebenarnya sebelum di-upload.
CSV_HEADER_MAP = {
    'Periode DD-MM-YYYY': 'Periode',
}

CSV_NUMBER_FIELDS = [
    'Nilai Tagihan', 'Prognosa'
]
CSV_SELECT_FIELDS = [
    'Status Invoice', 'Status Pembayaran', 'Status BA Performansi', 'Status Rekon'
]
CSV_DATE_FIELDS = [
    'Periode', 'Tanggal Serah BA Performansi', 'Tanggal masuk BA Performansi',
    'Tanggal Rekon'
]
CSV_TEXT_FIELDS = [
    'No. Dokumen BA LP'
]


def notion_patch(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=HEADERS, method='PATCH'
    )
    return json.loads(urllib.request.urlopen(req).read())


def verify_database(db_id):
    """
    Verifikasi Database ID.
    Return dict: {ok: bool, title: str, error: str, relation_prop: str|None,
                  has_title: bool, missing: [..]}
    """
    if not TOKEN:
        return {'ok': False, 'error': 'NOTION_TOKEN belum di-set di server.'}
    if not db_id or len(db_id.replace('-', '')) < 32:
        return {'ok': False, 'error': 'Database ID tidak valid (harus 32 karakter).'}

    try:
        data = notion_get(f'https://api.notion.com/v1/databases/{db_id}')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {'ok': False, 'error': 'Database tidak ditemukan / belum di-share ke integration.'}
        if e.code == 401:
            return {'ok': False, 'error': 'Token tidak berwenang (401).'}
        return {'ok': False, 'error': f'HTTP {e.code} saat mengakses database.'}
    except Exception as e:
        return {'ok': False, 'error': f'Gagal mengakses database: {e}'}

    title = ''
    if data.get('title'):
        title = ''.join(t.get('plain_text', '') for t in data['title'])

    props = data.get('properties', {})

    # Cari properti relation yang menunjuk ke database SPK
    relation_prop = None
    spk_norm = SPK_DB.replace('-', '')
    for name, p in props.items():
        if p.get('type') == 'relation':
            target = (p.get('relation', {}).get('database_id', '') or '').replace('-', '')
            if target == spk_norm:
                relation_prop = name
                break

    # Pastikan ada kolom title
    has_title = any(p.get('type') == 'title' for p in props.values())

    # Kolom yang diharapkan untuk import (informasi saja, tidak semua wajib)
    expected = ['Name'] + CSV_NUMBER_FIELDS + CSV_TEXT_FIELDS + CSV_SELECT_FIELDS + CSV_DATE_FIELDS
    missing = [c for c in expected if c not in props]

    return {
        'ok': True,
        'title': title,
        'relation_prop': relation_prop,
        'has_title': has_title,
        'missing': missing,
        'properties': list(props.keys()),
    }


_spk_lookup_cache = {}

def find_spk_page_id(no_spk):
    """Cari page id di database SPK berdasarkan No SPK (case-insensitive + trim)."""
    if not no_spk or not no_spk.strip():
        return None
    target = no_spk.strip()
    key = target.lower()
    if key in _spk_lookup_cache:
        return _spk_lookup_cache[key]

    url = f'https://api.notion.com/v1/databases/{SPK_DB}/query'
    cursor, has_more = None, True
    while has_more:
        body = {'page_size': 100, 'filter': {'property': 'No SPK', 'title': {'contains': target}}}
        if cursor:
            body['start_cursor'] = cursor
        try:
            resp = notion_post(url, body)
        except Exception:
            _spk_lookup_cache[key] = None
            return None
        for page in resp.get('results', []):
            title_arr = page.get('properties', {}).get('No SPK', {}).get('title', [])
            title = ''.join(t.get('plain_text', '') for t in title_arr)
            if title.strip().lower() == key:
                _spk_lookup_cache[key] = page['id']
                return page['id']
        has_more = resp.get('has_more', False)
        cursor = resp.get('next_cursor')

    _spk_lookup_cache[key] = None
    return None


def normalize_date(s, dayfirst=True):
    """
    Ubah berbagai format tanggal ke ISO 8601 (YYYY-MM-DD).
    Format utama yang diharapkan: DD-MM-YYYY (mis. 01-08-2026 = 1 Agustus 2026).
    Juga mendukung: ISO YYYY-MM-DD, YYYY/MM/DD, dan variasi pemisah '/', '-', '.'.
    Bila dayfirst=True (default), komponen pertama dianggap HARI (DD-MM-YYYY);
    kalau ternyata hari > 31 atau tidak valid, dicoba sebagai bulan (MM-DD-YYYY).
    Return (iso|None, error|None).
    """
    from datetime import datetime
    if not s:
        return None, None
    s = str(s).strip()
    if not s:
        return None, None

    # Sudah ISO penuh (dengan waktu) → ambil bagian tanggalnya
    if 'T' in s:
        s = s.split('T', 1)[0]

    # Format ISO langsung (tahun 4 digit di depan)
    for fmt in ('%Y-%m-%d', '%Y/%m/%d'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d'), None
        except ValueError:
            pass

    # Pisahkan komponen
    sep = None
    for c in ('/', '-', '.'):
        if c in s:
            sep = c
            break
    if sep:
        parts = [p for p in s.split(sep) if p != '']
        if len(parts) == 3:
            a, b, c = parts
            try:
                # Kalau komponen pertama 4 digit → Y M D
                if len(a) == 4:
                    y, m, d = int(a), int(b), int(c)
                else:
                    ia, ib, ic = int(a), int(b), int(c)
                    if ic < 100:
                        ic += 2000
                    if dayfirst:
                        # DD-MM-YYYY (default)
                        if ia > 31 or ia == 0:
                            # komponen pertama mustahil jadi hari → tafsir MM-DD
                            m, d, y = ia, ib, ic
                        else:
                            d, m, y = ia, ib, ic
                            # bila 'bulan' > 12 padahal 'hari' <= 12 → sebenarnya MM-DD
                            if m > 12 and d <= 12:
                                d, m = m, d
                    else:
                        # MM-DD-YYYY (gaya en-US)
                        m, d, y = ia, ib, ic
                        if m > 12 and d <= 12:
                            m, d = d, m
                return datetime(y, m, d).strftime('%Y-%m-%d'), None
            except (ValueError, TypeError):
                pass

    return None, f"format tanggal '{s}' tidak dikenali (pakai DD-MM-YYYY)"


def month_range(iso_date):
    """
    Dari tanggal ISO (YYYY-MM-DD), kembalikan (hari_pertama, hari_terakhir)
    dari bulan tsb dalam format ISO.
    Contoh: '2026-10-15' -> ('2026-10-01', '2026-10-31').
    """
    import calendar
    from datetime import date
    d = date.fromisoformat(iso_date)
    last_day = calendar.monthrange(d.year, d.month)[1]
    start = date(d.year, d.month, 1).isoformat()
    end = date(d.year, d.month, last_day).isoformat()
    return start, end


def build_page_properties(row, relation_prop):
    """Bangun payload properties Notion dari satu baris CSV."""
    props = {}

    def val(k):
        v = row.get(k)
        if v is None:
            return None
        v = str(v).strip()
        return v if v else None

    if val('Name'):
        props['Name'] = {'title': [{'text': {'content': val('Name')}}]}

    for col in CSV_NUMBER_FIELDS:
        if val(col):
            digits = ''.join(ch for ch in val(col) if ch.isdigit())
            if digits:
                props[col] = {'number': float(digits)}

    for col in CSV_TEXT_FIELDS:
        if val(col):
            props[col] = {'rich_text': [{'text': {'content': val(col)}}]}

    for col in CSV_SELECT_FIELDS:
        if val(col):
            props[col] = {'select': {'name': val(col)}}

    for col in CSV_DATE_FIELDS:
        if val(col):
            iso, _ = normalize_date(val(col))
            if iso:
                if col == 'Periode':
                    # Periode = rentang satu bulan penuh (tgl 1 s/d tgl terakhir)
                    start, end = month_range(iso)
                    props[col] = {'date': {'start': start, 'end': end}}
                else:
                    props[col] = {'date': {'start': iso}}

    if relation_prop and val('No SPK'):
        spk_id = find_spk_page_id(val('No SPK'))
        if spk_id:
            props[relation_prop] = {'relation': [{'id': spk_id}]}

    return props


@app.route('/api/verify-db', methods=['POST'])
@login_required
def api_verify_db():
    payload = request.get_json(silent=True) or {}
    db_id = (payload.get('database_id') or '').strip()
    result = verify_database(db_id)
    return jsonify(result), (200 if result.get('ok') else 400)


@app.route('/api/import-csv', methods=['POST'])
@login_required
def api_import_csv():
    """
    Import CSV ke database yang ID-nya diberikan user.
    Aturan: (1) Database ID diverifikasi dulu; (2) 'No SPK' wajib diisi dan
    harus ditemukan (case-insensitive) di database SPK; (3) all-or-nothing —
    bila ada satu baris tidak valid, tidak ada baris yang dibuat.
    """
    db_id = (request.form.get('database_id') or '').strip()
    file = request.files.get('file')
    delim_choice = (request.form.get('delimiter') or 'auto').strip().lower()

    if not db_id:
        return jsonify({'ok': False, 'error': 'Database ID wajib diisi untuk verifikasi.'}), 400
    if not file:
        return jsonify({'ok': False, 'error': 'File CSV wajib diunggah.'}), 400

    # 1) Verifikasi database
    v = verify_database(db_id)
    if not v.get('ok'):
        return jsonify({'ok': False, 'stage': 'verify', 'error': v.get('error')}), 400
    if not v.get('has_title'):
        return jsonify({'ok': False, 'stage': 'verify',
                        'error': 'Database tidak memiliki kolom Title.'}), 400
    relation_prop = v.get('relation_prop')

    # 2) Baca CSV
    try:
        raw = file.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        raw = file.read().decode('latin-1')

    # ── Delimiter: BASIS selalu KOMA (,) ──────────────────────────────────────
    # Aturan sesuai kebutuhan:
    #   • File koma  → langsung diproses.
    #   • File titik koma (;) → DIKONVERSI dulu ke koma, baru diproses.
    # Deteksi: bila user memilih eksplisit pakai itu; bila 'auto', tebak dari
    # baris header (mana yang lebih banyak muncul, ';' atau ',').
    header_line = raw.split('\n', 1)[0]
    if delim_choice in (',', ';'):
        source_delim = delim_choice
    else:
        source_delim = ';' if header_line.count(';') > header_line.count(',') else ','

    converted = False
    if source_delim == ';':
        # Konversi ';' → ',' secara aman (berbasis parsing, bukan replace kasar,
        # sehingga koma di dalam nilai tetap ter-escape dengan benar).
        out = io.StringIO()
        writer = csv.writer(out, delimiter=',')
        reader_semi = csv.reader(io.StringIO(raw), delimiter=';')
        for row in reader_semi:
            writer.writerow(row)
        raw = out.getvalue()
        converted = True

    # Mulai titik ini, data DIPASTIKAN berpemisah koma.
    reader = csv.DictReader(io.StringIO(raw), delimiter=',')
    rows = list(reader)
    if not rows:
        return jsonify({'ok': False, 'error': 'CSV kosong / tidak ada baris data.'}), 400

    # Petakan header CSV → nama properti Notion (mis. 'Periode DD-MM-YYYY' → 'Periode')
    if CSV_HEADER_MAP:
        remapped = []
        for row in rows:
            new_row = {}
            for k, cell in row.items():
                key = k.strip() if isinstance(k, str) else k
                new_row[CSV_HEADER_MAP.get(key, key)] = cell
            remapped.append(new_row)
        rows = remapped

    # 3) Fase validasi (all-or-nothing)
    _spk_lookup_cache.clear()
    errors = []
    for i, row in enumerate(rows, start=1):
        name = (row.get('Name') or '').strip()
        no_spk = (row.get('No SPK') or '').strip()

        if not name:
            errors.append(f"Baris {i}: kolom 'Name' wajib diisi.")
        if not no_spk:
            errors.append(f"Baris {i} ('{name}'): kolom 'No SPK' WAJIB diisi.")
        elif not relation_prop:
            errors.append(f"Baris {i}: database tujuan tidak punya relation ke SPK, "
                          f"tetapi ada 'No SPK'.")
        else:
            if not find_spk_page_id(no_spk):
                errors.append(f"Baris {i} ('{name}'): No SPK '{no_spk}' TIDAK DITEMUKAN "
                              f"di database SPK.")

        # Validasi format tanggal (bila diisi)
        for dcol in CSV_DATE_FIELDS:
            dval = (row.get(dcol) or '').strip()
            if dval:
                iso, derr = normalize_date(dval)
                if not iso:
                    errors.append(f"Baris {i} ('{name}'): kolom '{dcol}' {derr}.")

    if errors:
        return jsonify({'ok': False, 'stage': 'validate', 'imported': 0,
                        'errors': errors,
                        'error': f'Import dibatalkan. {len(errors)} baris tidak valid. '
                                 f'Tidak ada data yang di-upload.'}), 400

    # 4) Fase upload (semua sudah valid)
    created, fails = 0, []
    for i, row in enumerate(rows, start=1):
        body = {'parent': {'database_id': db_id},
                'properties': build_page_properties(row, relation_prop)}
        try:
            notion_post('https://api.notion.com/v1/pages', body)
            created += 1
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read().decode()
            except Exception:
                pass
            fails.append(f"Baris {i} ('{row.get('Name','')}'): {e.code} {detail}")
        except Exception as e:
            fails.append(f"Baris {i} ('{row.get('Name','')}'): {e}")

    # New rows were written to Notion; refresh the local cache so the dashboard
    # reflects them without waiting for the TTL. Best-effort; ignore failures.
    if created:
        try:
            sync.incremental_sync()
        except Exception as e:  # noqa: BLE001
            app.logger.warning('Post-import sync failed: %s', e)

    return jsonify({
        'ok': len(fails) == 0,
        'stage': 'upload',
        'imported': created,
        'failed': len(fails),
        'errors': fails,
        'db_title': v.get('title'),
        'converted': converted,
    }), (200 if not fails else 207)


if __name__ == '__main__':
    db.init_db()
    app.run(host='0.0.0.0', port=8080, debug=True)
