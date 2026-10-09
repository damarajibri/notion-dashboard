"""Audit trail disimpan di BODY halaman Notion (bukan di properti).

Kenapa di body, bukan properti rich_text?
- Properti rich_text dibatasi ~2.000 karakter. Audit trail tumbuh seiring waktu,
  jadi akan cepat penuh. Body halaman bisa menampung banyak blok, jauh lebih besar.

Struktur di body:
    heading_2:  "audit_trail"            <- penanda section (idempoten)
    code(json): [ {versi}, {versi}, ... ] <- seluruh riwayat sebagai JSON

Satu rich_text object dibatasi 2.000 karakter saat ditulis via API, maka JSON
panjang dipecah menjadi beberapa segmen rich_text di DALAM satu blok code.
Saat membaca, semua segmen digabung lagi sebelum di-parse.

Bentuk satu entri versi:
    {
      "version":   int,                 # nomor urut, mulai dari 1
      "timestamp": "YYYY-MM-DDTHH:MM:SSZ (UTC)",
      "user":      {"id": .., "username": .., "full_name": ..},
      "action":    "update" | "create" | "rollback",
      "properties": { <nama properti Notion>: <nilai properti Notion mentah> }
    }

`properties` menyimpan objek properti Notion MENTAH (mis. {"status": {"name": ...}})
sehingga bisa langsung dipakai kembali untuk PATCH saat rollback.
"""

import json
import urllib.request

AUDIT_HEADING = "audit_trail"

# Batas aman karakter per rich_text segment (limit Notion = 2000).
_SEGMENT_LIMIT = 1900

# Notion menolak array children/rich_text yang terlalu besar; batasi jumlah
# segmen per blok code. 100 segmen x 1900 = ~190k karakter: cukup lega.
_MAX_SEGMENTS = 100


class AuditError(Exception):
    """Kesalahan terkait operasi audit trail."""


def _chunks(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _rich_text_segments(text):
    """Pecah teks panjang menjadi beberapa rich_text object (<=_SEGMENT_LIMIT)."""
    parts = _chunks(text, _SEGMENT_LIMIT)
    if len(parts) > _MAX_SEGMENTS:
        raise AuditError(
            f"Audit JSON terlalu besar: {len(text)} char > "
            f"{_MAX_SEGMENTS * _SEGMENT_LIMIT} char. Pertimbangkan memangkas riwayat."
        )
    return [{"type": "text", "text": {"content": p}} for p in parts]


def _join_rich_text(rich_text):
    return "".join(x.get("plain_text", "") for x in rich_text)


# ─── Operasi body/blocks via dependency injection ─────────────────────────────
# Modul ini tidak mengimpor app.py (hindari import siklik). Pemanggil menyuntik
# fungsi HTTP Notion (notion_get/post/patch/delete) yang sudah ada di app.py.

class NotionBlocks:
    def __init__(self, get, post, patch, delete):
        self._get = get
        self._post = post
        self._patch = patch
        self._delete = delete

    def list_children(self, block_id):
        """Kembalikan semua child block (mengikuti pagination)."""
        results, cursor, has_more = [], None, True
        while has_more:
            url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
            if cursor:
                url += f"&start_cursor={cursor}"
            resp = self._get(url)
            results.extend(resp.get("results", []))
            has_more = resp.get("has_more", False)
            cursor = resp.get("next_cursor")
        return results

    def append_children(self, block_id, children):
        return self._patch(
            f"https://api.notion.com/v1/blocks/{block_id}/children",
            {"children": children},
        )

    def update_code_block(self, block_id, rich_text):
        return self._patch(
            f"https://api.notion.com/v1/blocks/{block_id}",
            {"code": {"rich_text": rich_text, "language": "json"}},
        )


def _find_section(blocks):
    """Cari (idx_heading, code_block) untuk section audit_trail.

    Return (heading_index, code_block_dict) atau (None, None) jika belum ada.
    code_block adalah blok 'code' pertama SETELAH heading audit_trail.
    """
    heading_idx = None
    for i, b in enumerate(blocks):
        if b.get("type") == "heading_2":
            txt = _join_rich_text(b["heading_2"].get("rich_text", []))
            if txt.strip() == AUDIT_HEADING:
                heading_idx = i
                break
    if heading_idx is None:
        return None, None
    for b in blocks[heading_idx + 1:]:
        if b.get("type") == "code":
            return heading_idx, b
        # Berhenti jika ketemu heading_2 lain (section baru) tanpa code di antaranya.
        if b.get("type") == "heading_2":
            break
    return heading_idx, None


def ensure_section(blocks_api, page_id):
    """Pastikan section audit_trail ada di body. Buat jika belum.

    Return code_block_id (id blok code yang menyimpan JSON).
    """
    blocks = blocks_api.list_children(page_id)
    heading_idx, code_block = _find_section(blocks)

    if code_block is not None:
        return code_block["id"]

    # Buat yang belum ada.
    children = []
    if heading_idx is None:
        children.append({
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": AUDIT_HEADING}}]},
        })
    children.append({
        "object": "block", "type": "code",
        "code": {"language": "json", "rich_text": _rich_text_segments("[]")},
    })
    created = blocks_api.append_children(page_id, children)
    for b in created["results"]:
        if b.get("type") == "code":
            return b["id"]
    raise AuditError("Gagal membuat blok code audit_trail.")


def read_versions(blocks_api, page_id):
    """Baca & parse seluruh versi audit trail. Return list (bisa kosong)."""
    blocks = blocks_api.list_children(page_id)
    _, code_block = _find_section(blocks)
    if code_block is None:
        return []
    text = _join_rich_text(code_block["code"].get("rich_text", []))
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        # JSON korup -> jangan jatuhkan; kembalikan kosong agar caller bisa recover.
        return []


def _write_versions(blocks_api, page_id, versions):
    """Tulis ulang seluruh array versi ke blok code (buat section jika perlu)."""
    code_block_id = ensure_section(blocks_api, page_id)
    text = json.dumps(versions, ensure_ascii=False, indent=2)
    blocks_api.update_code_block(code_block_id, _rich_text_segments(text))
    return code_block_id


def append_version(blocks_api, page_id, properties, user, timestamp, action="update"):
    """Tambahkan satu snapshot versi baru ke audit trail. Return nomor versi baru."""
    versions = read_versions(blocks_api, page_id)
    next_version = (versions[-1]["version"] + 1) if versions else 1
    entry = {
        "version": next_version,
        "timestamp": timestamp,
        "user": user or {},
        "action": action,
        "properties": properties or {},
    }
    versions.append(entry)
    _write_versions(blocks_api, page_id, versions)
    return next_version


def get_version(blocks_api, page_id, version):
    """Ambil satu entri versi berdasarkan nomor version. None jika tidak ada."""
    for v in read_versions(blocks_api, page_id):
        if v.get("version") == version:
            return v
    return None


# ─── Snapshot & rollback helpers ──────────────────────────────────────────────
# Tipe properti Notion yang READ-ONLY (tidak bisa ditulis via Update Page).
# Properti ini tetap disimpan di snapshot (untuk tampilan & compare) tapi
# DILEWATI saat membangun payload rollback.
_READONLY_TYPES = {
    "formula", "rollup", "created_time", "created_by",
    "last_edited_time", "last_edited_by", "button",
    "unique_id", "rich_text_mention",
}


def snapshot_properties(raw_page):
    """Ambil snapshot SEMUA properti dari objek halaman Notion mentah.

    Menyimpan objek properti apa adanya (tanpa 'id'), sehingga:
    - bisa dibandingkan antar versi (compare), dan
    - bisa dibangun ulang menjadi payload PATCH untuk rollback (field writable).
    """
    props = raw_page.get("properties", {}) or {}
    snap = {}
    for name, val in props.items():
        if not isinstance(val, dict):
            continue
        ptype = val.get("type")
        # Simpan hanya bagian nilai + type; buang 'id' yang tak perlu.
        clean = {"type": ptype, ptype: val.get(ptype)}
        snap[name] = clean
    return snap


def build_rollback_payload(snapshot):
    """Bangun dict `properties` untuk Update Page dari snapshot.

    Hanya menyertakan properti yang WRITABLE. Relation, people, title,
    rich_text, select, multi_select, status, number, date, checkbox, url,
    email, phone_number, files didukung. Properti read-only dilewati.
    Return (properties, skipped_names).
    """
    out = {}
    skipped = []
    for name, val in (snapshot or {}).items():
        if not isinstance(val, dict):
            continue
        ptype = val.get("type")
        if ptype in _READONLY_TYPES or ptype is None:
            skipped.append(name)
            continue
        payload_val = val.get(ptype)

        if ptype == "title":
            out[name] = {"title": _strip_rt(payload_val)}
        elif ptype == "rich_text":
            out[name] = {"rich_text": _strip_rt(payload_val)}
        elif ptype == "select":
            out[name] = {"select": {"name": payload_val["name"]} if payload_val else None}
        elif ptype == "status":
            out[name] = {"status": {"name": payload_val["name"]} if payload_val else None}
        elif ptype == "multi_select":
            out[name] = {"multi_select": [{"name": o["name"]} for o in (payload_val or [])]}
        elif ptype == "number":
            out[name] = {"number": payload_val}
        elif ptype == "checkbox":
            out[name] = {"checkbox": bool(payload_val)}
        elif ptype == "date":
            out[name] = {"date": payload_val}
        elif ptype in ("url", "email", "phone_number"):
            out[name] = {ptype: payload_val}
        elif ptype == "people":
            out[name] = {"people": [{"id": p["id"]} for p in (payload_val or []) if p.get("id")]}
        elif ptype == "relation":
            out[name] = {"relation": [{"id": r["id"]} for r in (payload_val or []) if r.get("id")]}
        elif ptype == "files":
            out[name] = {"files": _rebuild_files(payload_val or [])}
        else:
            skipped.append(name)
    return out, skipped


def _strip_rt(rich_text):
    """Pangkas rich_text menjadi bentuk writable (type+text/content+annotations)."""
    result = []
    for x in (rich_text or []):
        if x.get("type") == "text":
            item = {"type": "text", "text": {"content": x["text"]["content"]}}
            link = x.get("text", {}).get("link")
            if link:
                item["text"]["link"] = link
            result.append(item)
        else:
            # mention/equation: pakai plain_text sebagai fallback teks biasa.
            result.append({"type": "text", "text": {"content": x.get("plain_text", "")}})
    return result


def _rebuild_files(files):
    """Bangun ulang array files agar bisa ditulis balik (external & file_upload).

    Entri 'file' (hosted Notion) tidak bisa di-reupload via API; dipertahankan
    apa adanya karena Notion menerima penulisan ulang objek file yang sama.
    """
    out = []
    for f in files:
        name = f.get("name", "")
        ftype = f.get("type")
        if ftype == "external":
            out.append({"type": "external", "name": name,
                        "external": {"url": f["external"]["url"]}})
        elif ftype == "file":
            out.append(f)  # pertahankan apa adanya
    return out


def compare_versions(blocks_api, page_id, version_a, version_b):
    """Bandingkan properti antar dua versi.

    Return dict: {field: {"from": <nilai A>, "to": <nilai B>}} untuk field yang berbeda.
    """
    va = get_version(blocks_api, page_id, version_a)
    vb = get_version(blocks_api, page_id, version_b)
    if va is None or vb is None:
        raise AuditError("Salah satu versi tidak ditemukan.")
    pa = va.get("properties", {}) or {}
    pb = vb.get("properties", {}) or {}
    diff = {}
    for key in set(pa) | set(pb):
        before = pa.get(key)
        after = pb.get(key)
        if before != after:
            diff[key] = {"from": before, "to": after}
    return diff
