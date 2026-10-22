"""
excel_import.py
Import data properti dari file Excel/CSV untuk mengisi Step 1 secara massal -
appraiser bisa menyiapkan banyak properti sekaligus di spreadsheet (format
bebas, header fleksibel) lalu tinggal pilih barisnya di app, tanpa ketik ulang
tiap field satu per satu.

Tidak ada dependensi ke Gemini/LLM - murni pembacaan file + heuristik regex,
jadi tidak butuh API key apa pun dan bisa dipakai offline.
"""

import csv
import io
import re

STATUS_SERTIFIKAT_OPTIONS = ["SHM", "SHGB", "SHMASRS", "Girik", "Lainnya"]

# Header kolom dicari dengan substring (case-insensitive) - urutan tidak masalah,
# nama kolom boleh beda-beda asal mengandung salah satu frasa di bawah.
_HEADER_HINTS = {
    "no": ["no"],
    "alamat": ["alamat"],
    "lokasi": ["lokasi", "koordinat", "latitude", "lat/long", "lat long"],
    "dokumen": ["dokumen", "status sertifikat", "sertifikat"],
    "data_bangunan": ["data bangunan"],
    "luas_tanah": ["luas tanah"],
    "luas_bangunan": ["luas bangunan"],
    "tanggal": ["tanggal"],
}


def _norm_header(h) -> str:
    return re.sub(r"\s+", " ", str(h or "").strip().lower())


def _find_columns(headers):
    """Cocokkan tiap header kolom ke field yang kita kenal. Field "luas_tanah"/
    "luas_bangunan" dicek lebih dulu supaya tidak ketabrak "data_bangunan"
    (yang juga mengandung kata "bangunan")."""
    norm = [_norm_header(h) for h in headers]
    field_order = ["no", "luas_tanah", "luas_bangunan", "alamat", "lokasi",
                   "dokumen", "data_bangunan", "tanggal"]
    taken = set()
    col_idx = {}
    for field in field_order:
        for i, h in enumerate(norm):
            if i in taken:
                continue
            if any(hint in h for hint in _HEADER_HINTS[field]):
                col_idx[field] = i
                taken.add(i)
                break
    # Fallback untuk "data_bangunan": kalau tidak ada kolom literal "Data
    # Bangunan", pakai kolom BELUM TERPAKAI mana pun yang memuat kata
    # "bangunan" (mis. "Keterangan Bangunan", "Kondisi Bangunan", "Deskripsi
    # Bangunan") - "Luas Bangunan" sendiri sudah diklaim duluan di atas jadi
    # aman dari tabrakan.
    if "data_bangunan" not in col_idx:
        for i, h in enumerate(norm):
            if i in taken:
                continue
            if "bangunan" in h:
                col_idx["data_bangunan"] = i
                taken.add(i)
                break
    return col_idx


def _to_text(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _to_number(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"(?i)\s*(m\s*\^?\s*[2²]|meter(\s+persegi)?|sqm)", "", str(v))
    s = re.sub(r"[^0-9.,\-]", "", s)
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) != 3 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_latlon(text):
    """"-6.174327, 106.748311" -> (-6.174327, 106.748311). None kalau tidak valid
    atau di luar wilayah Indonesia (sanity check, sama seperti auto-read dokumen)."""
    if not text:
        return None, None
    nums = re.findall(r"-?\d+\.\d+", str(text))
    if len(nums) < 2:
        return None, None
    try:
        lat, lon = float(nums[0]), float(nums[1])
    except ValueError:
        return None, None
    if -11.5 <= lat <= 6.5 and 94.5 <= lon <= 141.5:
        return lat, lon
    return None, None


def _map_status_sertifikat(text):
    t = (text or "").lower()
    if "shmasrs" in t or "rumah susun" in t or "strata" in t:
        return "SHMASRS"
    if "shgb" in t or "hak guna bangunan" in t or re.search(r"\bhgb\b", t):
        return "SHGB"
    if "shm" in t or "hak milik" in t:
        return "SHM"
    if "girik" in t:
        return "Girik"
    return "Lainnya"


def _extract_tahun_bangun(text, this_year=2026):
    if not text:
        return None
    m = re.search(r"tahun\s*bangun\D{0,12}(\d{4})", text, re.I)
    if not m:
        return None
    y = int(m.group(1))
    return y if 1900 <= y <= this_year + 1 else None


def _parse_admin_from_alamat(alamat):
    """Tebak Kecamatan/Kabupaten-Kota/Provinsi dari teks alamat lengkap (kalau
    ditulis dengan label eksplisit, mis. "Kecamatan X", "Kota Y", "Kabupaten Z").
    Provinsi ditebak dari segmen TERAKHIR kalau tidak mengandung angka. Semua
    ini cuma bantuan awal - appraiser tetap perlu cek field-nya."""
    segs = [s.strip() for s in str(alamat or "").split(",") if s.strip()]
    kecamatan = kabkota = provinsi = None
    for s in segs:
        low = s.lower()
        if kecamatan is None and ("kecamatan" in low or low.startswith("kec.")):
            kecamatan = re.sub(r"(?i)^\s*(kecamatan|kec\.)\s*", "", s).strip(" .")
        elif kabkota is None and ("kabupaten" in low or low.startswith("kab.") or "kota" in low):
            kabkota = s
    if segs:
        cand = segs[-1]
        if not re.search(r"\d", cand) and len(cand.split()) <= 4 and cand.lower() not in ("indonesia",):
            provinsi = cand
    return kecamatan, kabkota, provinsi


def _rows_from_xlsx(data: bytes):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    return rows


def _rows_from_csv(data: bytes):
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return list(csv.reader(io.StringIO(text), dialect))


def parse_property_excel(data: bytes, filename: str = ""):
    """
    Baca file Excel (.xlsx) atau CSV berisi daftar properti dan ubah jadi list
    dict siap pakai untuk mengisi Step 1. Header kolom boleh bebas asal
    mengandung salah satu dari: Alamat, Lokasi (lat,lon), Dokumen (status
    sertifikat), Data Bangunan (teks bebas - dicari "Tahun bangun: YYYY" di
    dalamnya), Luas Tanah, Luas Bangunan. Baris tanpa Alamat/Luas Tanah/
    Luas Bangunan dilewati (biasanya baris sub-header seperti "Positif"/
    "Negatif" di bawah header gabungan).

    Mengembalikan (ok, hasil): hasil = list[dict] kalau ok=True, atau pesan
    error (str) kalau ok=False.
    """
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    try:
        if ext in ("xlsx", "xlsm"):
            raw_rows = _rows_from_xlsx(data)
        elif ext in ("csv", "tsv", "txt"):
            raw_rows = _rows_from_csv(data)
        elif ext == "xls":
            return False, ("Format .xls (lama) belum didukung - simpan ulang file sebagai "
                           ".xlsx (File > Save As > Excel Workbook) lalu unggah lagi.")
        else:
            return False, f"Format file tidak didukung: .{ext or '?'}. Gunakan .xlsx atau .csv."
    except Exception as e:
        return False, f"Gagal membaca file: {e}"

    raw_rows = [r for r in raw_rows if any(_to_text(c) for c in r)]
    if not raw_rows:
        return False, "File kosong - tidak ada baris yang terbaca."

    headers = raw_rows[0]
    col = _find_columns(headers)
    if "alamat" not in col:
        return False, ('Kolom "Alamat" tidak ditemukan di baris pertama file ini - pastikan '
                       'baris pertama berisi nama-nama kolom (header), salah satunya memuat '
                       'kata "Alamat".')

    def _get(row, field):
        i = col.get(field)
        if i is None or i >= len(row):
            return None
        return row[i]

    results = []
    for row in raw_rows[1:]:
        alamat = _to_text(_get(row, "alamat"))
        luas_tanah = _to_number(_get(row, "luas_tanah"))
        luas_bangunan = _to_number(_get(row, "luas_bangunan"))
        # Lewati baris yang bukan data properti (mis. baris sub-header "Positif"/
        # "Negatif" di bawah kolom "Faktor..." gabungan) - baris properti asli
        # SELALU punya Alamat, dan biasanya juga Luas Tanah atau Luas Bangunan.
        if not alamat or (luas_tanah is None and luas_bangunan is None):
            continue

        lokasi_text = _to_text(_get(row, "lokasi"))
        lat, lon = _parse_latlon(lokasi_text)
        dokumen_text = _to_text(_get(row, "dokumen"))
        data_bangunan_text = _to_text(_get(row, "data_bangunan"))
        kecamatan, kabkota, provinsi = _parse_admin_from_alamat(alamat)

        results.append({
            "no": _to_text(_get(row, "no")) or str(len(results) + 1),
            "tanggal": _to_text(_get(row, "tanggal")) or None,
            "alamat": alamat,
            "lat": lat, "lon": lon,
            "lokasi_text": lokasi_text or None,
            "kecamatan": kecamatan, "kabkota": kabkota, "provinsi": provinsi,
            "status_sertifikat": _map_status_sertifikat(dokumen_text),
            "dokumen_text": dokumen_text or None,
            "tahun_bangun": _extract_tahun_bangun(data_bangunan_text),
            "data_bangunan_text": data_bangunan_text or None,
            "luas_tanah": luas_tanah,
            "luas_bangunan": luas_bangunan,
        })

    if not results:
        return False, ('Tidak ada baris data properti yang terbaca (butuh minimal kolom '
                       '"Alamat" dan salah satu dari "Luas Tanah"/"Luas Bangunan" terisi).')
    return True, results