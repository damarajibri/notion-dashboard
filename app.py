import json, os, io, csv, urllib.request, urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template, request, send_from_directory

import db
import sync

app = Flask(__name__)

# ── On-demand incremental sync (Option C) ────────────────────────────────────
# Read endpoints (/api/data, /api/monthly) serve from a local SQLite cache.
# When the cache is older than SYNC_TTL_SECONDS, an incremental sync (only rows
# changed since last sync, via Notion's last_edited_time filter) refreshes it
# first. This keeps data fresh (~5 min) with no external scheduler, entirely on
# PythonAnywhere, behind the app's own auth. Sync failures degrade gracefully
# to whatever is already cached.
SYNC_TTL_SECONDS = int(os.environ.get('SYNC_TTL_SECONDS', '300'))

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


def _ensure_fresh():
    try:
        if _sync_is_due():
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
    done_statuses = {'Done', 'Complete', 'Not Required'}
    for df in DOC_FIELDS:
        val  = props.get(df, {})
        done = False
        if val.get('type') == 'status':
            done = val.get('status', {}).get('name', '') in done_statuses
        elif val.get('type') == 'checkbox':
            done = bool(val.get('checkbox'))
        if done:
            doc_done += 1
        doc_detail[df] = '✅' if done else '❌'

    total_docs = len(DOC_FIELDS)

    return {
        'title':      title,
        'status':     status_name,
        'priority':   priority_name,
        'assignees':  assignees,
        'completion': comp_val,
        'docs':       f'{doc_done}/{total_docs}',
        'doc_done':   doc_done,
        'doc_total':  total_docs,
        'doc_detail': doc_detail,
        'created':    r['created_time'][:10],
        'edited':     r['last_edited_time'][:10],
        'due':        due,
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

    # Relation SPK → tampilkan No SPK-nya
    spk_rel = props.get('📋 SPK', {}).get('relation', [])
    spk_no  = ', '.join(spk_no_map.get(rel['id'], '?') for rel in spk_rel) if spk_rel else ''

    return {
        'name':            name,
        'periode':         _date_start(props, 'Periode'),
        'no_spk':          spk_no,
        'nilai_tagihan':   nilai,
        'prognosa':        prognosa,
        'status_invoice':  _sel_name(props, 'Status Invoice'),
        'status_bayar':    _sel_name(props, 'Status Pembayaran'),
        'status_ba':       _sel_name(props, 'Status BA Performansi'),
        'status_rekon':    _sel_name(props, 'Status Rekon'),
        'dok_lp':          dok_lp,
        'tgl_serah_ba':    _date_start(props, 'Tanggal Serah BA Performansi'),
        'tgl_masuk_ba':    _date_start(props, 'Tanggal masuk BA Performansi'),
        'tgl_rekon':       _date_start(props, 'Tanggal Rekon'),
    }

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/download/template-csv')
def download_template_csv():
    """Kirim file contoh/template CSV untuk import Monthly Performance."""
    return send_from_directory(
        os.path.dirname(os.path.abspath(__file__)),
        'data.csv',
        as_attachment=True,
        download_name='template_monthly_performance.csv',
        mimetype='text/csv',
    )

@app.route('/api/data')
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
    for s in spk:
        s['perpanjangan'] = [
            {
                'title':  proj_map.get(pid, '?'),
                'status': s['status_perpanjangan'][i] if i < len(s['status_perpanjangan']) else ''
            }
            for i, pid in enumerate(s['perp_ids'])
        ]
        del s['perp_ids'], s['status_perpanjangan'], s['_id']

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
def api_monthly():
    """
    Endpoint TERPISAH untuk data Monthly Performance — di-load MANUAL (on-demand)
    agar tidak membebani load utama dashboard. Dipanggil hanya saat user menekan
    tombol "Load Monthly Performance" di tab-nya.
    """
    if not TOKEN:
        # No token: cannot sync, but we can still serve whatever is cached.
        app.logger.warning('NOTION_TOKEN not set; serving cached Monthly Performance.')
    else:
        _ensure_fresh()  # Option C: refresh cache from Notion only if stale

    # Peta internal SPK page-id → No SPK (untuk menampilkan No SPK dari relation)
    spk_no_map = {}
    for r in cached_query(SPK_DB):
        arr = r['properties'].get('No SPK', {}).get('title', [])
        spk_no_map[r['id']] = arr[0]['plain_text'] if arr else ''

    rows = [extract_monthly(r, spk_no_map) for r in cached_query(MONTHLY_DB)]

    # Ringkasan agregat
    total_tagihan  = sum(x['nilai_tagihan'] or 0 for x in rows)
    total_prognosa = sum(x['prognosa'] or 0 for x in rows)

    # Opsi filter unik
    periodes = sorted({x['periode'][:7] for x in rows if x['periode']}, reverse=True)

    return jsonify({
        'ok': True,
        'monthly': rows,
        'count': len(rows),
        'total_tagihan': total_tagihan,
        'total_prognosa': total_prognosa,
        'periodes': periodes,
    })


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
def api_verify_db():
    payload = request.get_json(silent=True) or {}
    db_id = (payload.get('database_id') or '').strip()
    result = verify_database(db_id)
    return jsonify(result), (200 if result.get('ok') else 400)


@app.route('/api/import-csv', methods=['POST'])
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
