"""Tests for API tokens (Bearer auth) and the OpenAPI document.

Runs against a throwaway SQLite file; makes no Notion calls.
"""
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp()
os.environ['DASHBOARD_DB'] = os.path.join(_tmp, 'test.db')
os.environ['NOTION_TOKEN'] = ''          # keep .env value out; no network use
os.environ['FLASK_SECRET_KEY'] = 'test'

import app as app_module  # noqa: E402
import db  # noqa: E402

app_module.app.config['TESTING'] = True


@pytest.fixture(autouse=True)
def fresh_db():
    db.init_db()
    conn = db.get_conn()
    conn.execute("DELETE FROM api_tokens")
    conn.execute("DELETE FROM users WHERE username != ?", (db.ROOT_USERNAME,))
    conn.commit()
    conn.close()


def _login(client, user):
    with client.session_transaction() as s:
        s['uid'], s['username'], s['role'] = user['id'], user['username'], user['role']


def _mk_user(name, role='user'):
    return db.create_user(name, 'pw12345', role=role)


@pytest.fixture
def root():
    return db.get_user_by_username(db.ROOT_USERNAME)


def _new_token(client, **body):
    body.setdefault('name', 't')
    body.setdefault('expires_in_days', 7)
    r = client.post('/api/tokens', json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def bearer(tok):
    return {'Authorization': f'Bearer {tok}'}


# ── OpenAPI document ──────────────────────────────────────────────────────────

def _spec():
    with open(os.path.join(ROOT, 'openapi.json'), encoding='utf-8') as f:
        return json.load(f)


def test_spec_matches_flask_routes():
    """Every documented /api route exists and every /api route is documented."""
    spec = _spec()
    documented = {(p, m.upper()) for p, ops in spec['paths'].items() for m in ops}
    actual = set()
    for rule in app_module.app.url_map.iter_rules():
        path = re.sub(r'<(?:\w+:)?(\w+)>', r'{\1}', rule.rule)
        if not (path.startswith('/api/') or path == '/download/template-csv'):
            continue
        for m in rule.methods - {'HEAD', 'OPTIONS'}:
            actual.add((path, m))
    # /api/users/<id> accepts PUT and POST; the spec documents PUT and notes POST.
    actual.discard(('/api/users/{user_id}', 'POST'))
    assert documented == actual, {
        'documented_but_missing': sorted(documented - actual),
        'undocumented': sorted(actual - documented),
    }


def test_spec_is_valid_openapi():
    validator = pytest.importorskip('openapi_spec_validator')
    validator.validate_spec(_spec())


def test_docs_require_login():
    c = app_module.app.test_client()
    assert c.get('/docs').status_code == 302
    assert c.get('/api/openapi.json').status_code == 401


def test_docs_served_after_login(root):
    c = app_module.app.test_client()
    _login(c, root)
    assert c.get('/docs').status_code == 200
    assert c.get('/api/openapi.json').get_json()['openapi'].startswith('3.')


# ── Token lifecycle ───────────────────────────────────────────────────────────

def test_token_authenticates_api_call(root):
    c = app_module.app.test_client()
    _login(c, root)
    tok = _new_token(c)['token']
    assert tok.startswith('nds_')
    anon = app_module.app.test_client()
    r = anon.get('/api/users', headers=bearer(tok))
    assert r.status_code == 200 and r.get_json()['ok']


def test_token_value_not_stored_or_listed(root):
    c = app_module.app.test_client()
    _login(c, root)
    data = _new_token(c)
    listed = c.get('/api/tokens').get_json()['tokens']
    assert data['token'] not in json.dumps(listed)
    conn = db.get_conn()
    rows = conn.execute("SELECT token_hash FROM api_tokens").fetchall()
    conn.close()
    assert data['token'] not in [r[0] for r in rows]


def test_invalid_and_missing_token_rejected():
    c = app_module.app.test_client()
    r = c.get('/api/users', headers=bearer('nds_doesnotexist'))
    assert r.status_code == 401
    assert 'Bearer' in r.headers.get('WWW-Authenticate', '')
    assert c.get('/api/users').status_code == 401


def test_invalid_bearer_overrides_valid_session(root):
    c = app_module.app.test_client()
    _login(c, root)
    assert c.get('/api/users').status_code == 200
    assert c.get('/api/users', headers=bearer('nds_bad')).status_code == 401


def test_expired_token_rejected(root):
    c = app_module.app.test_client()
    _login(c, root)
    tok = _new_token(c)['token']
    conn = db.get_conn()
    conn.execute("UPDATE api_tokens SET expires_at = '2000-01-01T00:00:00Z'")
    conn.commit()
    conn.close()
    anon = app_module.app.test_client()
    assert anon.get('/api/users', headers=bearer(tok)).status_code == 401
    assert c.get('/api/tokens').get_json()['tokens'][0]['status'] == 'expired'


def test_revoked_token_rejected(root):
    c = app_module.app.test_client()
    _login(c, root)
    data = _new_token(c)
    anon = app_module.app.test_client()
    assert anon.get('/api/users', headers=bearer(data['token'])).status_code == 200
    assert c.delete(f"/api/tokens/{data['item']['id']}").status_code == 200
    assert anon.get('/api/users', headers=bearer(data['token'])).status_code == 401
    assert c.delete(f"/api/tokens/{data['item']['id']}").status_code == 400


def test_no_expiry_token(root):
    c = app_module.app.test_client()
    _login(c, root)
    item = _new_token(c, no_expiry=True, expires_in_days=None)['item']
    assert item['expires_at'] is None and item['status'] == 'active'


def test_expires_at_date_and_datetime(root):
    c = app_module.app.test_client()
    _login(c, root)
    future = (datetime.now(timezone.utc) + timedelta(days=3)).strftime('%Y-%m-%d')
    item = _new_token(c, expires_in_days=None, expires_at=future)['item']
    assert item['expires_at'] == future + 'T23:59:59Z'
    dt = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
    assert _new_token(c, expires_in_days=None, expires_at=dt)['item']['expires_at'] == dt


@pytest.mark.parametrize('body', [
    {'name': 'x'},                                                   # no expiry choice
    {'name': 'x', 'expires_in_days': 7, 'no_expiry': True},          # two choices
    {'name': 'x', 'expires_in_days': 0},
    {'name': 'x', 'expires_in_days': 99999},
    {'name': 'x', 'expires_in_days': 'abc'},
    {'name': 'x', 'expires_at': '2000-01-01'},                       # in the past
    {'name': 'x', 'expires_at': 'not-a-date'},
    {'name': 'x', 'expires_at': 123},
    {'name': '', 'expires_in_days': 7},
    {'name': 'a' * 101, 'expires_in_days': 7},
])
def test_create_validation(root, body):
    c = app_module.app.test_client()
    _login(c, root)
    r = c.post('/api/tokens', json=body)
    assert r.status_code == 400, r.get_json()
    assert r.get_json()['ok'] is False


def test_token_cannot_manage_tokens_or_password(root):
    c = app_module.app.test_client()
    _login(c, root)
    tok = _new_token(c)['token']
    anon = app_module.app.test_client()
    h = bearer(tok)
    assert anon.post('/api/tokens', json={'name': 'x', 'expires_in_days': 1}, headers=h).status_code == 403
    assert anon.get('/api/tokens', headers=h).status_code == 403
    assert anon.post('/api/account/password', json={}, headers=h).status_code == 403


def test_token_uses_current_role_and_active_flag(root):
    admin = _mk_user('adm', 'admin')
    c = app_module.app.test_client()
    _login(c, admin)
    tok = _new_token(c)['token']
    anon = app_module.app.test_client()
    assert anon.get('/api/users', headers=bearer(tok)).status_code == 200
    db.update_user(admin['id'], role='user')              # demoted: effective immediately
    assert anon.get('/api/users', headers=bearer(tok)).status_code == 403
    db.update_user(admin['id'], role='admin', is_active=False)
    assert anon.get('/api/users', headers=bearer(tok)).status_code == 401


def test_tokens_are_per_user(root):
    u1, u2 = _mk_user('u1'), _mk_user('u2')
    c1, c2 = app_module.app.test_client(), app_module.app.test_client()
    _login(c1, u1); _login(c2, u2)
    item = _new_token(c1)['item']
    assert c2.get('/api/tokens').get_json()['tokens'] == []
    assert c2.delete(f"/api/tokens/{item['id']}").status_code == 404     # not owner
    assert c2.get('/api/tokens?all=1').status_code == 403
    cr = app_module.app.test_client(); _login(cr, root)
    assert len(cr.get('/api/tokens?all=1').get_json()['tokens']) == 1
    assert cr.delete(f"/api/tokens/{item['id']}").status_code == 200     # root may revoke


def test_active_token_limit(root):
    c = app_module.app.test_client()
    _login(c, root)
    for _ in range(db.MAX_TOKENS_PER_USER):
        _new_token(c)
    assert c.post('/api/tokens', json={'name': 'x', 'expires_in_days': 1}).status_code == 400


def test_deleting_user_removes_tokens(root):
    u = _mk_user('gone')
    c = app_module.app.test_client()
    _login(c, u)
    _new_token(c)
    db.delete_user(u['id'])
    assert db.list_api_tokens() == []


def test_token_calls_are_attributed_in_activity_log(root):
    c = app_module.app.test_client()
    _login(c, root)
    tok = _new_token(c)['token']
    anon = app_module.app.test_client()
    anon.post('/api/task/delete', json={}, headers=bearer(tok))     # 400, no Notion call
    rows = db.list_activity(action='api.post')
    assert rows and rows[0]['username'] == root['username']
    assert '"via": "token"' in rows[0]['detail']
