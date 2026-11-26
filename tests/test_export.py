"""Tests for the Excel/CSV export feature (static/export.js + index.html wiring).

JS checks need `node` and are skipped when it is not installed. The workbook is
validated with the standard library only (zipfile + ElementTree).
"""
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp()
os.environ['DASHBOARD_DB'] = os.path.join(_tmp, 'test.db')
os.environ['NOTION_TOKEN'] = ''
os.environ['FLASK_SECRET_KEY'] = 'test'

import app as app_module  # noqa: E402
import db  # noqa: E402

NODE = shutil.which('node')
needs_node = pytest.mark.skipif(NODE is None, reason='node not installed')
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def _run_node(script):
    r = subprocess.run([NODE, '-e', script], capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _build(sheets):
    """Build a workbook with export.js, return {path: bytes} of the zip parts."""
    out = os.path.join(_tmp, 'o.xlsx')
    _run_node(
        "const K=require('./static/export.js');"
        "require('fs').writeFileSync(%s, K.buildXLSX(%s));" % (json.dumps(out), json.dumps(sheets)))
    z = zipfile.ZipFile(out)
    assert z.testzip() is None
    return {n: z.read(n) for n in z.namelist()}


def _cells(sheet_xml):
    """{ref: (type, value)} for all cells of a worksheet part."""
    root = ET.fromstring(sheet_xml)
    out = {}
    for c in root.iter('{%s}c' % NS['m']):
        if c.get('t') == 'inlineStr':
            out[c.get('r')] = ('s', ''.join(t.text or '' for t in c.iter('{%s}t' % NS['m'])))
        else:
            out[c.get('r')] = ('n', c.find('m:v', NS).text)
    return out


@needs_node
def test_xlsx_structure_and_values():
    parts = _build([
        {'name': 'Data', 'header': ['#', 'Nama', 'Nilai'], 'types': ['int', 'text', 'money'],
         'rows': [[1, 'A & B <x> "q"', 1500000.5], [2, '=HYPERLINK("x")', 20], [3, None, None]]},
        {'name': 'Info: bad/name?', 'plain': True, 'header': ['K', 'V'], 'rows': [['a', 'b']]},
    ])
    for name, data in parts.items():
        ET.fromstring(data)                                  # every part is well-formed XML
    assert list(parts)[0] == '[Content_Types].xml'
    wb = ET.fromstring(parts['xl/workbook.xml'])
    names = [s.get('name') for s in wb.iter('{%s}sheet' % NS['m'])]
    assert names == ['Data', 'Info  bad name']               # forbidden chars removed
    cells = _cells(parts['xl/worksheets/sheet1.xml'])
    assert cells['B2'] == ('s', 'A & B <x> "q"')
    assert cells['B3'] == ('s', '=HYPERLINK("x")')           # stays text, never a formula
    assert cells['C2'] == ('n', '1500000.5') and cells['A2'] == ('n', '1')
    assert 'B4' not in cells and 'C4' not in cells           # empty values omit the cell
    sheet1 = parts['xl/worksheets/sheet1.xml'].decode()
    assert 'state="frozen"' in sheet1 and '<autoFilter ref="A1:C4"/>' in sheet1
    assert '<f>' not in sheet1
    assert 'state="frozen"' not in parts['xl/worksheets/sheet2.xml'].decode()


@needs_node
def test_xlsx_strips_invalid_xml_characters_and_handles_unicode():
    parts = _build([{'name': 'U', 'header': ['T'], 'rows': [['a\u0000b\u0008c ✓ 日本 é']]}])
    assert _cells(parts['xl/worksheets/sheet1.xml'])['A2'] == ('s', 'abc ✓ 日本 é')


@needs_node
def test_xlsx_many_columns_use_excel_letters():
    header = ['c%d' % i for i in range(30)]
    parts = _build([{'name': 'W', 'header': header, 'rows': [list(range(30))]}])
    cells = _cells(parts['xl/worksheets/sheet1.xml'])
    assert cells['Z2'] == ('n', '25') and cells['AA2'] == ('n', '26') and cells['AD2'] == ('n', '29')


@needs_node
def test_csv_quoting_bom_and_formula_guard():
    out = os.path.join(_tmp, 'o.csv')
    _run_node(
        "const K=require('./static/export.js');"
        "require('fs').writeFileSync(%s, K.buildCSV({header:['a','b','c'],"
        "rows:[['x,y','say \"hi\"\\nnext',5],['=1+1','@cmd',-3],['+x','-y',null]]}));" % json.dumps(out))
    raw = open(out, 'rb').read()
    assert raw.startswith(b'\xef\xbb\xbf')                    # UTF-8 BOM for Excel
    rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline='')))
    assert rows[0] == ['a', 'b', 'c']
    assert rows[1] == ['x,y', 'say "hi"\nnext', '5']
    assert rows[2] == ["'=1+1", "'@cmd", '-3']                # text guarded, real numbers untouched
    assert rows[3] == ["'+x", "'-y", '']


@needs_node
def test_inline_script_in_index_has_valid_syntax():
    src = open(os.path.join(ROOT, 'templates', 'index.html'), encoding='utf-8').read()
    script = re.findall(r'<script>(.*?)</script>', src, re.S)[-1]
    path = os.path.join(_tmp, 'main.js')
    open(path, 'w', encoding='utf-8').write(script)
    r = subprocess.run([NODE, '--check', path], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_index_wires_export_buttons_and_row_indexes():
    c = app_module.app.test_client()
    u = db.get_user_by_username(db.ROOT_USERNAME)
    with c.session_transaction() as s:
        s['uid'], s['username'], s['role'] = u['id'], u['username'], u['role']
    html = c.get('/').get_data(as_text=True)
    assert re.search(r'/static/export\.js\?v=\d+', html)
    for key in ('tasks', 'projects', 'spk', 'spkGroup', 'monthly'):
        assert "openExportMenu(event, '%s')" % key in html
    assert html.count('<tr data-ri="${i}"') >= 4              # tasks, projects, spk, monthly rows
    assert c.get('/static/export.js').status_code == 200
