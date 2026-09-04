import json, os, urllib.request
from collections import defaultdict
from flask import Flask, jsonify, render_template

app = Flask(__name__)

TOKEN = os.environ.get('NOTION_TOKEN', '')
TASKS_DB    = '2c3a31d192f481d68c65d0f289ebd111'
PROJECTS_DB = '2c3a31d192f48104ba5fecc8ee9c66d1'
PERSONEL_DB = '2c4a31d192f480aab819f688af756ed1'
SPK_DB      = '2c5a31d192f4803a86e4fb50b19df8dc'

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
    """Return dict {page_id: name} dari database Personel."""
    p = {}
    for r in query_all(PERSONEL_DB):
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

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data')
def api_data():
    from datetime import datetime, timedelta

    personel     = get_personel()
    raw_tasks    = query_all(TASKS_DB)
    raw_projects = query_all(PROJECTS_DB)
    raw_spk      = query_all(SPK_DB)

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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
