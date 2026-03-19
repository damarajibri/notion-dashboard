import json, os, urllib.request
from collections import defaultdict
from flask import Flask, jsonify, render_template

app = Flask(__name__)

TOKEN = os.environ.get('NOTION_TOKEN', '')
TASKS_DB = '2c3a31d192f481d68c65d0f289ebd111'
PROJECTS_DB = '2c3a31d192f48104ba5fecc8ee9c66d1'
PERSONEL_DB = '2c4a31d192f480aab819f688af756ed1'
SPK_DB = '2c5a31d192f4803a86e4fb50b19df8dc'
HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Notion-Version': '2022-06-28',
    'Content-Type': 'application/json'
}

def notion_post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=HEADERS, method='POST')
    return json.loads(urllib.request.urlopen(req).read())

def notion_get(url):
    req = urllib.request.Request(url, headers={k:v for k,v in HEADERS.items() if k != 'Content-Type'})
    return json.loads(urllib.request.urlopen(req).read())

def query_all(db_id, filt=None):
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    results, cursor, has_more = [], None, True
    while has_more:
        body = {'page_size': 100}
        if filt: body['filter'] = filt
        if cursor: body['start_cursor'] = cursor
        resp = notion_post(url, body)
        results.extend(resp.get('results', []))
        has_more, cursor = resp.get('has_more', False), resp.get('next_cursor')
    return results

def get_personel():
    p = {}
    for r in query_all(PERSONEL_DB):
        for v in r['properties'].values():
            if v.get('type') == 'title' and v['title']:
                p[r['id']] = v['title'][0]['plain_text']
    return p

def extract_task(r, personel):
    props = r['properties']
    title = props.get('Task name',{}).get('title',[])
    name = title[0]['plain_text'] if title else ''
    status = props.get('Status',{}).get('status',{})
    status_name = status.get('name','') if status else ''
    due = props.get('due',{}).get('date',{})
    due_start = due.get('start','')[:10] if due and due.get('start') else ''
    comp = props.get('Completed on',{}).get('date',{})
    comp_date = comp.get('start','')[:10] if comp else ''
    rel = props.get('Assignee relation',{}).get('relation',[])
    assignees = [personel.get(a['id'],'?') for a in rel]
    progress = props.get('Progress',{}).get('number')
    return {
        'name': name, 'status': status_name, 'due': due_start,
        'assignees': assignees, 'created': r['created_time'][:10],
        'edited': r['last_edited_time'][:10],
        'done_date': comp_date or (r['last_edited_time'][:10] if status_name == 'Done' else ''),
        'progress': int(progress*100) if progress is not None else None
    }

def extract_spk(r, personel):
    props = r['properties']
    no_spk = props.get('No SPK',{}).get('title',[])
    no_spk = no_spk[0]['plain_text'] if no_spk else ''
    proj = props.get('Project Name',{}).get('rich_text',[])
    proj_name = proj[0]['plain_text'] if proj else ''
    vendor = props.get('Vendor',{}).get('select',{})
    vendor_name = vendor.get('name','') if vendor else '-'
    status = props.get('Status',{}).get('select',{})
    status_name = status.get('name','') if status else '-'
    jt = props.get('Jatuh Tempo',{}).get('date',{})
    jatuh_tempo = jt.get('start','')[:10] if jt and jt.get('start') else ''
    sisa = props.get('Sisa Hari',{}).get('formula',{})
    sisa_hari = sisa.get('number') if sisa else None
    nilai = props.get('Nilai Kontrak SPK',{}).get('number')
    notes = props.get('Notes',{}).get('rich_text',[])
    notes_text = notes[0]['plain_text'] if notes else ''
    uid = props.get('id',{}).get('unique_id',{})
    spk_id = f"{uid.get('prefix','')}-{uid.get('number','')}" if uid else ''
    pic = []
    pic_rollup = props.get('PIC Perpanjangan',{}).get('rollup',{}).get('array',[])
    for item in pic_rollup:
        if item.get('type') == 'relation':
            for rel in item.get('relation',[]):
                name = personel.get(rel['id'],'?')
                if name not in pic: pic.append(name)
    return {
        'spk_id': spk_id, 'no_spk': no_spk, 'project': proj_name, 'vendor': vendor_name,
        'status': status_name, 'jatuh_tempo': jatuh_tempo, 'sisa_hari': sisa_hari,
        'nilai': nilai, 'notes': notes_text, 'pic': pic
    }

def extract_project(r, personel):
    props = r['properties']
    title = ''
    for v in props.values():
        if v.get('type') == 'title' and v.get('title'):
            title = v['title'][0]['plain_text']; break
    status = props.get('Status',{}).get('status',{})
    status_name = status.get('name','') if status else ''
    priority = props.get('Priority',{}).get('select',{})
    priority_name = priority.get('name','') if priority else '-'
    rel = props.get('Assignee',{}).get('relation',[])
    assignees = [personel.get(a['id'],'?') for a in rel]
    comp = props.get('Completion',{})
    comp_val = None
    if comp.get('type') == 'number' and comp.get('number') is not None:
        comp_val = round(comp['number']*100)
    elif comp.get('type') == 'rollup' and comp.get('rollup',{}).get('number') is not None:
        comp_val = round(comp['rollup']['number']*100)
    doc_fields = ['TOR','FS (Feasibility Study)','Izin Prinsip','Izin Anggaran','Penilaian Teknis','PI (Pakta Integritas)','TPRA (Third Party Risk Assesment)','BenchMark','Aanwidjzing']
    doc_done = 0
    for df in doc_fields:
        val = props.get(df,{})
        if val.get('type') == 'status' and val.get('status',{}).get('name','') in ('Done','Complete'): doc_done += 1
        elif val.get('type') == 'checkbox' and val.get('checkbox'): doc_done += 1
    dates = props.get('Dates',{}).get('date',{})
    due = ''
    if dates and dates.get('end'): due = dates['end'][:10]
    elif dates and dates.get('start'): due = dates['start'][:10]
    return {
        'title': title, 'status': status_name, 'priority': priority_name,
        'assignees': assignees, 'completion': comp_val, 'docs': f'{doc_done}/9',
        'doc_done': doc_done, 'created': r['created_time'][:10],
        'edited': r['last_edited_time'][:10], 'due': due
    }

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data')
def api_data():
    personel = get_personel()
    raw_tasks = query_all(TASKS_DB)
    raw_projects = query_all(PROJECTS_DB)
    raw_spk = query_all(SPK_DB)
    tasks = [extract_task(r, personel) for r in raw_tasks]
    projects = [extract_project(r, personel) for r in raw_projects]
    spk = [extract_spk(r, personel) for r in raw_spk]

    # Task stats
    task_status = defaultdict(int)
    created_monthly, done_monthly = defaultdict(int), defaultdict(int)
    person_done, person_total = defaultdict(int), defaultdict(int)
    overdue, on_track, no_due = 0, 0, 0
    from datetime import datetime
    today = datetime.now().strftime('%Y-%m-%d')

    for t in tasks:
        task_status[t['status']] += 1
        if t['created']: created_monthly[t['created'][:7]] += 1
        if t['done_date']: done_monthly[t['done_date'][:7]] += 1
        for a in t['assignees']:
            person_total[a] += 1
            if t['status'] == 'Done': person_done[a] += 1
        if t['status'] != 'Done':
            if not t['due']: no_due += 1
            elif t['due'] < today: overdue += 1
            else: on_track += 1

    # Monthly backlog
    months = sorted(set(list(created_monthly.keys()) + list(done_monthly.keys())))
    monthly = []
    cum_c, cum_d = 0, 0
    for m in months:
        cum_c += created_monthly[m]; cum_d += done_monthly[m]
        monthly.append({'month': m, 'created': created_monthly[m], 'done': done_monthly[m], 'backlog': cum_c - cum_d})

    # Daily task resolve progress
    from datetime import datetime, timedelta
    daily_done = defaultdict(int)
    daily_created = defaultdict(int)
    for t in tasks:
        if t['done_date']: daily_done[t['done_date']] += 1
        if t['created']: daily_created[t['created']] += 1
    all_days = sorted(set(list(daily_done.keys()) + list(daily_created.keys())))
    daily = []
    cum_done = 0
    for day in all_days:
        active = sum(1 for t in tasks if t['created'] <= day and (t['status'] != 'Done' or (t['done_date'] and t['done_date'] > day)))
        cum_done += daily_done[day]
        daily.append({'date': day, 'active': active, 'done_cumul': cum_done, 'done_day': daily_done[day]})

    # Project stats
    proj_status = defaultdict(int)
    proj_person = defaultdict(lambda: defaultdict(int))
    for p in projects:
        proj_status[p['status']] += 1
        for a in p['assignees']: proj_person[a][p['status']] += 1

    return jsonify({
        'tasks': tasks, 'projects': projects, 'personel': list(personel.values()),
        'task_status': dict(task_status), 'monthly': monthly,
        'person_done': dict(person_done), 'person_total': dict(person_total),
        'overdue': overdue, 'on_track': on_track, 'no_due': no_due,
        'proj_status': dict(proj_status),
        'proj_person': {k: dict(v) for k,v in proj_person.items()},
        'daily': daily,
        'spk': spk
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
