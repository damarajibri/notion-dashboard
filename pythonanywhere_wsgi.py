# ─────────────────────────────────────────────────────────────────────────────
# Template WSGI untuk PythonAnywhere
#
# Cara pakai:
# 1. Di dashboard PythonAnywhere → tab "Web" → buka file WSGI kamu:
#      /var/www/damaraji_pythonanywhere_com_wsgi.py
# 2. Hapus seluruh isinya, lalu SALIN isi file ini ke sana.
# 3. Ganti nilai NOTION_TOKEN dengan token BARU (regenerate di Notion dulu).
# 4. FLASK_SECRET_KEY sudah terisi kunci acak; boleh diganti dengan kunci lain.
# 5. Simpan, lalu klik tombol hijau "Reload" di tab Web.
#
# Catatan: JANGAN commit token/secret asli ke Git. Isi hanya di file WSGI
# yang ada di server PythonAnywhere (di luar repo).
# ─────────────────────────────────────────────────────────────────────────────

import sys
import os

# Path ke folder project hasil clone dari GitHub
project_home = '/home/damaraji/notion-dashboard'
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# Token integrasi Notion (WAJIB). Ganti dengan token baru kamu.
os.environ['NOTION_TOKEN'] = 'GANTI_DENGAN_TOKEN_NOTION_BARU'

# Secret key untuk menandatangani cookie session login (WAJIB & RAHASIA).
# Ganti dengan kunci acak, JANGAN bagikan / commit ke Git.
# Kalau kunci ini diganti, semua sesi login yang sedang aktif akan logout.
# Generate kunci:  python3 -c "import secrets; print(secrets.token_hex(32))"
os.environ['FLASK_SECRET_KEY'] = 'GANTI_DENGAN_SECRET_KEY_ACAK'

# Import aplikasi Flask. Objek 'app' di app.py di-expose sebagai 'application'
# sesuai standar WSGI yang dipakai PythonAnywhere.
from app import app as application
