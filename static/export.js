/* ExportKit: membuat file .xlsx dan .csv di browser tanpa dependensi.
 *
 * Model sheet:
 *   { name: 'Tasks',
 *     header: ['#', 'Task', ...],
 *     types:  ['int', 'text', 'money', 'number', ...],   // opsional, per kolom
 *     rows:   [[1, 'Judul', 1500000.5, ...], ...],        // string | number | null
 *     plain:  true }                                      // opsional: tanpa freeze/filter
 *
 * Tipe kolom: 'text' (default), 'int', 'number', 'money' (format #,##0 atau
 * #,##0.00 bila ada pecahan). Teks di .xlsx disimpan sebagai inline string,
 * jadi tidak pernah dievaluasi sebagai rumus. Di .csv, teks yang diawali
 * = + - @ diberi awalan ' (mitigasi CSV/formula injection).
 *
 * Modul ini juga bisa di-require di Node (dipakai oleh tes).
 */
(function (root) {
  'use strict';

  var MAX_CELL = 32767;                               // batas karakter per sel Excel
  var BAD_XML = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\uFFFE\uFFFF]/g;

  function cleanText(v) {
    var s = String(v).replace(BAD_XML, '');
    return s.length > MAX_CELL ? s.slice(0, MAX_CELL) : s;
  }

  function xmlEscape(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
  }

  function colLetter(i) {                               // 0 -> A, 26 -> AA
    var s = '';
    for (i += 1; i > 0; i = Math.floor((i - 1) / 26)) {
      s = String.fromCharCode(65 + ((i - 1) % 26)) + s;
    }
    return s;
  }

  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function isEmpty(v) { return v === null || v === undefined || v === ''; }

  // ── CSV ────────────────────────────────────────────────────────────────────
  function csvField(v) {
    if (isEmpty(v)) return '';
    if (isNum(v)) return String(v);
    var s = cleanText(v);
    if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  function buildCSV(sheet) {
    var lines = [sheet.header.map(csvField).join(',')];
    sheet.rows.forEach(function (r) { lines.push(r.map(csvField).join(',')); });
    return '\uFEFF' + lines.join('\r\n') + '\r\n';      // BOM agar Excel membaca UTF-8
  }

  // ── XLSX ───────────────────────────────────────────────────────────────────
  var NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main';
  var NS_R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
  var XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n';

  var STYLES_XML = XML_HEAD +
    '<styleSheet xmlns="' + NS + '">' +
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>' +
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>' +
    '<fills count="3"><fill><patternFill patternType="none"/></fill>' +
    '<fill><patternFill patternType="gray125"/></fill>' +
    '<fill><patternFill patternType="solid"><fgColor rgb="FFE8E8F5"/><bgColor indexed="64"/></patternFill></fill></fills>' +
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>' +
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' +
    '<cellXfs count="4">' +
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>' +
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>' +
    '<xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' +
    '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' +
    '</cellXfs>' +
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>' +
    '</styleSheet>';

  var S_HEADER = 1, S_INT = 2, S_DEC = 3;

  function safeSheetName(name, used) {
    var base = String(name || 'Sheet').replace(/[\[\]:*?\/\\]/g, ' ').replace(/^'+|'+$/g, '')
                 .trim().slice(0, 31) || 'Sheet';
    var n = base, k = 2;
    while (used[n.toLowerCase()]) {
      var suffix = ' (' + (k++) + ')';
      n = base.slice(0, 31 - suffix.length) + suffix;
    }
    used[n.toLowerCase()] = true;
    return n;
  }

  function columnStyles(sheet) {
    var types = sheet.types || [];
    return sheet.header.map(function (_, c) {
      var t = types[c] || 'text';
      if (t === 'int') return S_INT;
      if (t === 'money') {
        var frac = sheet.rows.some(function (r) { return isNum(r[c]) && r[c] % 1 !== 0; });
        return frac ? S_DEC : S_INT;
      }
      return 0;
    });
  }

  function columnWidths(sheet) {
    return sheet.header.map(function (h, c) {
      var w = String(h).length;
      var limit = Math.min(sheet.rows.length, 500);
      for (var r = 0; r < limit; r++) {
        var v = sheet.rows[r][c];
        if (isEmpty(v)) continue;
        var len = isNum(v) ? 14 : String(v).split('\n')[0].length;
        if (len > w) w = len;
      }
      return Math.max(8, Math.min(60, w + 2));
    });
  }

  function cellXml(ref, v, style) {
    if (isEmpty(v)) return '';
    if (isNum(v)) return '<c r="' + ref + '"' + (style ? ' s="' + style + '"' : '') + '><v>' + v + '</v></c>';
    return '<c r="' + ref + '"' + (style ? ' s="' + style + '"' : '') +
           ' t="inlineStr"><is><t xml:space="preserve">' + xmlEscape(cleanText(v)) + '</t></is></c>';
  }

  function sheetXml(sheet) {
    var styles = columnStyles(sheet);
    var widths = columnWidths(sheet);
    var ncol = sheet.header.length;
    var lastCol = colLetter(Math.max(ncol - 1, 0));
    var out = [XML_HEAD, '<worksheet xmlns="', NS, '" xmlns:r="', NS_R, '">'];
    if (!sheet.plain) {
      out.push('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" ' +
               'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>');
    }
    out.push('<cols>' + widths.map(function (w, i) {
      return '<col min="' + (i + 1) + '" max="' + (i + 1) + '" width="' + w + '" customWidth="1"/>';
    }).join('') + '</cols>');
    out.push('<sheetData>');
    out.push('<row r="1">' + sheet.header.map(function (h, c) {
      return cellXml(colLetter(c) + '1', h, S_HEADER);
    }).join('') + '</row>');
    sheet.rows.forEach(function (row, i) {
      var r = i + 2;
      out.push('<row r="' + r + '">' + row.map(function (v, c) {
        return cellXml(colLetter(c) + r, v, styles[c]);
      }).join('') + '</row>');
    });
    out.push('</sheetData>');
    if (!sheet.plain && sheet.rows.length) {
      out.push('<autoFilter ref="A1:' + lastCol + (sheet.rows.length + 1) + '"/>');
    }
    out.push('</worksheet>');
    return out.join('');
  }

  function buildXLSX(sheets) {
    var enc = new TextEncoder();
    var used = {};
    var names = sheets.map(function (s) { return safeSheetName(s.name, used); });

    var files = [];
    function add(name, text) { files.push({ name: name, data: enc.encode(text) }); }

    add('[Content_Types].xml', XML_HEAD +
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
      '<Default Extension="xml" ContentType="application/xml"/>' +
      '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
      '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' +
      sheets.map(function (_, i) {
        return '<Override PartName="/xl/worksheets/sheet' + (i + 1) +
               '.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>';
      }).join('') + '</Types>');

    add('_rels/.rels', XML_HEAD +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' +
      '</Relationships>');

    add('xl/workbook.xml', XML_HEAD +
      '<workbook xmlns="' + NS + '" xmlns:r="' + NS_R + '"><sheets>' +
      names.map(function (n, i) {
        return '<sheet name="' + xmlEscape(n) + '" sheetId="' + (i + 1) + '" r:id="rId' + (i + 1) + '"/>';
      }).join('') + '</sheets></workbook>');

    add('xl/_rels/workbook.xml.rels', XML_HEAD +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      sheets.map(function (_, i) {
        return '<Relationship Id="rId' + (i + 1) + '" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet' + (i + 1) + '.xml"/>';
      }).join('') +
      '<Relationship Id="rId' + (sheets.length + 1) + '" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' +
      '</Relationships>');

    add('xl/styles.xml', STYLES_XML);
    sheets.forEach(function (s, i) { add('xl/worksheets/sheet' + (i + 1) + '.xml', sheetXml(s)); });
    return zipStore(files);
  }

  // ── ZIP (tanpa kompresi) ───────────────────────────────────────────────────
  var CRC_TABLE = (function () {
    var t = new Uint32Array(256);
    for (var n = 0; n < 256; n++) {
      var c = n;
      for (var k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      t[n] = c >>> 0;
    }
    return t;
  })();

  function crc32(bytes) {
    var c = 0xFFFFFFFF;
    for (var i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
    return (c ^ 0xFFFFFFFF) >>> 0;
  }

  function zipStore(files) {
    var enc = new TextEncoder();
    var d = new Date();
    var dosTime = (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1);
    var dosDate = ((Math.max(d.getFullYear(), 1980) - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();

    var parts = [], central = [], offset = 0;
    files.forEach(function (f) {
      var name = enc.encode(f.name);
      var crc = crc32(f.data);
      var size = f.data.length;

      var lh = new DataView(new ArrayBuffer(30));
      lh.setUint32(0, 0x04034B50, true);
      lh.setUint16(4, 20, true);
      lh.setUint16(6, 0x0800, true);
      lh.setUint16(8, 0, true);
      lh.setUint16(10, dosTime, true);
      lh.setUint16(12, dosDate, true);
      lh.setUint32(14, crc, true);
      lh.setUint32(18, size, true);
      lh.setUint32(22, size, true);
      lh.setUint16(26, name.length, true);
      lh.setUint16(28, 0, true);
      parts.push(new Uint8Array(lh.buffer), name, f.data);

      var ch = new DataView(new ArrayBuffer(46));
      ch.setUint32(0, 0x02014B50, true);
      ch.setUint16(4, 20, true);
      ch.setUint16(6, 20, true);
      ch.setUint16(8, 0x0800, true);
      ch.setUint16(10, 0, true);
      ch.setUint16(12, dosTime, true);
      ch.setUint16(14, dosDate, true);
      ch.setUint32(16, crc, true);
      ch.setUint32(20, size, true);
      ch.setUint32(24, size, true);
      ch.setUint16(28, name.length, true);
      ch.setUint32(42, offset, true);
      central.push(new Uint8Array(ch.buffer), name);

      offset += 30 + name.length + size;
    });

    var cdSize = central.reduce(function (n, p) { return n + p.length; }, 0);
    var end = new DataView(new ArrayBuffer(22));
    end.setUint32(0, 0x06054B50, true);
    end.setUint16(8, files.length, true);
    end.setUint16(10, files.length, true);
    end.setUint32(12, cdSize, true);
    end.setUint32(16, offset, true);

    var all = parts.concat(central, [new Uint8Array(end.buffer)]);
    var total = all.reduce(function (n, p) { return n + p.length; }, 0);
    var out = new Uint8Array(total), pos = 0;
    all.forEach(function (p) { out.set(p, pos); pos += p.length; });
    return out;
  }

  // ── Unduh (browser) ────────────────────────────────────────────────────────
  var XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';

  function download(data, filename, mime) {
    var blob = new Blob([data], { type: mime });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
  }

  var api = {
    buildCSV: buildCSV, buildXLSX: buildXLSX, download: download,
    XLSX_MIME: XLSX_MIME, CSV_MIME: 'text/csv;charset=utf-8',
    _crc32: crc32, _colLetter: colLetter
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.ExportKit = api;
})(typeof window !== 'undefined' ? window : this);
