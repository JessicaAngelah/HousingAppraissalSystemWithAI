"""
calculations.py
Semua rumus perhitungan penilaian properti (appraisal).
Murni fungsi matematis - tidak ada panggilan API di sini,
supaya mudah di-unit-test dan tidak tergantung koneksi internet.
"""

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# STEP 2 - Nilai Tanah (Bhumi ZNT)
# ---------------------------------------------------------------------------
def hitung_nilai_tanah(znt_per_m2: float, luas_tanah: float) -> float:
    """Nilai Tanah = ZNT per m2 x Luas Tanah"""
    return round(znt_per_m2 * luas_tanah, 2)


# ---------------------------------------------------------------------------
# STEP 3 - Nilai Bangunan (Cost Approach)
# ---------------------------------------------------------------------------
def hitung_brb(biaya_reproduksi_per_m2: float, luas_bangunan: float) -> float:
    """Biaya Reproduksi Baru (BRB) = biaya per m2 x luas bangunan"""
    return round(biaya_reproduksi_per_m2 * luas_bangunan, 2)


def hitung_penyusutan(
    brb: float,
    umur_efektif: float,
    umur_ekonomis: float,
    metode: str = "garis_lurus",
) -> float:
    """
    Penyusutan (depresiasi) bangunan.
    Default: metode garis lurus (straight line).
    Depresiasi dibatasi maksimum 80% dari BRB (sisa nilai minimum 20%),
    sesuai praktik umum penilaian agunan.
    """
    if umur_ekonomis <= 0:
        return 0.0
    persen_penyusutan = min(umur_efektif / umur_ekonomis, 0.8)
    return round(brb * persen_penyusutan, 2)


def hitung_nilai_bangunan(brb: float, penyusutan: float) -> float:
    """Nilai Bangunan = BRB - Penyusutan"""
    return round(max(brb - penyusutan, 0), 2)


# Tabel Umur Ekonomis/Manfaat & Penyusutan per Tahun berdasarkan struktur
# utama/dominan bangunan, sesuai SOP internal yang mengacu pada Biaya Teknis
# Bangunan (BTB) MAPPI 2023. Dipakai sebagai DEFAULT persentase penyusutan di
# Kalkulator Penyusutan Persentase - appraiser tetap bisa menimpa manual.
MAPPI_UMUR_EKONOMIS_TABLE = {
    "Rumah Sederhana": {"umur_ekonomis_tahun": 20, "penyusutan_pct_tahun": 0.05},
    "Rumah Menengah":  {"umur_ekonomis_tahun": 30, "penyusutan_pct_tahun": 0.0333},
    "Rumah Mewah":     {"umur_ekonomis_tahun": 50, "penyusutan_pct_tahun": 0.02},
    "Pabrik / Gudang": {"umur_ekonomis_tahun": 30, "penyusutan_pct_tahun": 0.0333},
    "Toko / Kios":     {"umur_ekonomis_tahun": 20, "penyusutan_pct_tahun": 0.05},
}

# Tabel klasifikasi bangunan (dipakai di Step 3) - kriteria luas bangunan, umur
# ekonomis, rate penyusutan/tahun, dan default Biaya Reproduksi Baru (BRB) per m².
# Sesuai SOP.
KLASIFIKASI_BANGUNAN_TABLE = {
    "Sederhana": {
        "kriteria": "LB ≤ 36m² atau subsidi",
        "umur_ekonomis_tahun": 20,
        "rate_per_tahun": 0.05,
        "brb_per_m2": 4_000_000,
    },
    "Menengah": {
        "kriteria": "LB 36–70m² komersil",
        "umur_ekonomis_tahun": 30,
        "rate_per_tahun": 0.0333,
        "brb_per_m2": 6_000_000,
    },
    "Mewah": {
        "kriteria": "LB > 70m²",
        "umur_ekonomis_tahun": 50,
        "rate_per_tahun": 0.02,
        "brb_per_m2": 10_000_000,
    },
}

# Klasifikasi bangunan (dipakai di Step 3) -> jenis bangunan MAPPI terdekat,
# supaya default persentase penyusutan otomatis mengikuti klasifikasi yang
# sudah dipilih appraiser tanpa perlu input dobel.
KLASIFIKASI_KE_JENIS_MAPPI = {
    "Sederhana": "Rumah Sederhana",
    "Menengah": "Rumah Menengah",
    "Mewah": "Rumah Mewah",
}


def hitung_penyusutan_persentase(
    nilai_awal: float, persentase: float, periode: int, unit: str = "Tahun"
) -> list:
    """
    Kalkulator Penyusutan Persentase (Percentage / Declining Balance Depreciation
    Calculator) - metode ALTERNATIF dari garis lurus di hitung_penyusutan() di atas.
    Setiap periode, nilai disusutkan sebesar persentase TETAP dari nilai SISA
    periode sebelumnya (bukan dari nilai awal keseluruhan), sehingga jumlah
    penyusutan mengecil tiap periode (mirip reducing-balance/declining-balance
    method).

    nilai_awal: nilai aset di awal periode (mis. BRB bangunan).
    persentase: laju penyusutan PER TAHUN, dalam desimal (0.05 = 5%/tahun) -
    sesuai tabel Umur Ekonomis MAPPI 2023 (mis. 5% Rumah Sederhana, 3.33%
    Rumah Menengah, 2% Rumah Mewah).
    periode: jumlah periode yang dihitung, satuannya mengikuti `unit`.
    unit: "Tahun" (default) atau "Bulan". Kalau "Bulan", persentase per
    periode dikonversi jadi persentase/12 (asumsi penyusutan tahunan dibagi
    rata per bulan), dan `periode` berarti jumlah BULAN.

    Formula per periode:
        Depreciation = Beginning Value x Percentage(per periode)
        Balance      = Beginning Value - Depreciation
    (Balance periode ini menjadi Beginning Value periode berikutnya.)

    Mengembalikan list of dict: [{"period", "period_label", "beginning_value",
    "depreciation", "balance"}, ...] - satu baris per periode, dipakai untuk
    menampilkan jadwal penyusutan (depreciation schedule) di UI.
    """
    unit = unit if unit in ("Tahun", "Bulan") else "Tahun"
    persen_per_periode = persentase if unit == "Tahun" else persentase / 12.0
    persen_per_periode = max(0.0, min(persen_per_periode, 1.0))
    label = "Tahun" if unit == "Tahun" else "Bulan"

    schedule = []
    beginning = max(nilai_awal, 0.0)
    for period in range(1, max(int(periode), 0) + 1):
        depresiasi = round(beginning * persen_per_periode, 2)
        balance = round(beginning - depresiasi, 2)
        schedule.append({
            "period": period,
            "period_label": f"{label} {period}",
            "beginning_value": round(beginning, 2),
            "depreciation": depresiasi,
            "balance": balance,
        })
        beginning = balance
    return schedule


# ---------------------------------------------------------------------------
# STEP 4 - Faktor Pengurang (Rule Engine)
# ---------------------------------------------------------------------------
# Bobot pengurang default untuk setiap risiko yang terdeteksi otomatis.
# Bisa ditimpa (override) dari UI / config.
DEFAULT_AUTO_WEIGHTS = {
    "flood_risk": 0.05,
    "sutet": 0.07,
    "railway": 0.03,
    "industry": 0.02,
    "hospital": -0.005,   # kedekatan fasilitas bisa jadi nilai tambah kecil
    "school": -0.005,
    "market": -0.003,
    "main_road": -0.01,
    "public_facilities": -0.005,
}

# Definisi lengkap "Public Facilities" sesuai SOP - dipakai sebagai teks bantuan
# (help text) di UI supaya appraiser tahu persis apa saja yang termasuk kategori ini.
PUBLIC_FACILITIES_DEFINITION = (
    "Tempat ibadah, rumah sakit/puskesmas/klinik, gedung/lapangan olahraga publik, "
    "perpustakaan umum, tempat rekreasi publik (spt. taman bermain/kebun binatang), "
    "terminal angkutan umum, sekolah, pasar, kuburan/rumah abu/rumah duka/dll, dan "
    "properti lainnya sesuai definisi di atas."
)

# Bobot pengurang untuk checklist manual (skala 0-3 tiap butir, dikonversi)
DEFAULT_MANUAL_WEIGHTS = {
    "bentuk_tanah": 0.01,
    "kontur_tanah": 0.01,
    "posisi_tanah": 0.01,
    "kondisi_bangunan": 0.02,
    "kualitas_konstruksi": 0.02,
    "perawatan_bangunan": 0.01,
    "legalitas": 0.03,
    "kondisi_lingkungan": 0.01,
    "peruntukan_lahan": 0.02,
    "akses_jalan": 0.01,
}

# ---------------------------------------------------------------------------
# STEP 4 - Faktor Pembatas / Red-Flag Tambahan (SOP)
# ---------------------------------------------------------------------------
# Kondisi-kondisi berikut sifatnya MEMBATASI kelayakan properti sebagai agunan
# (bukan sekadar mengurangi nilai secara proporsional), sehingga bobotnya lebih
# besar daripada Automatic Analysis/Manual Checklist biasa di atas. Selain
# berkontribusi ke Faktor Pengurang (tetap dibatasi maksimum 30% sesuai SOP),
# appraiser SELALU diberi peringatan eksplisit di UI kalau ada salah satu yang
# tercentang, karena butuh perhatian & pertimbangan khusus (bisa saja properti
# akhirnya dinyatakan tidak layak sebagai agunan terlepas dari angka %-nya).
DEFAULT_RESTRIKSI_WEIGHTS = {
    "sengketa_hukum": 0.10,
    "tanah_adat": 0.08,
    "rawan_bencana": 0.06,
    "cagar_lindung": 0.08,
    "jalur_hijau_fasum": 0.07,
    "pelebaran_jalan": 0.05,
    "akses_sempit": 0.04,
    "berbatasan_lokasi_berisiko": 0.08,
}

RESTRIKSI_LABELS = {
    "sengketa_hukum": (
        "Tanah dalam/terkait dengan sengketa yang dapat dibuktikan secara hukum "
        "dan terdaftar di pengadilan setempat."
    ),
    "tanah_adat": "Tanah adat/tanah ulayat/tanah bengkok.",
    "rawan_bencana": (
        "Area rawan banjir pasang air laut dan/atau rawan longsor dan/atau tanah "
        "miring/lereng/jurang."
    ),
    "cagar_lindung": "Cagar alam/cagar budaya/hutan lindung/suaka margasatwa.",
    "jalur_hijau_fasum": (
        "Ada rencana dan/atau telah menjadi jalur hijau/fasilitas umum/fasilitas sosial."
    ),
    "pelebaran_jalan": (
        "Ada rencana pelebaran jalan sehingga penggunaan tanahnya sudah tidak maksimal "
        "(prinsip highest and best use/HBU) sesuai peruntukannya (dilihat dari lokasi "
        "agunan dan/atau hasil pengecekan ke tata kota atau dinas terkait setempat)."
    ),
    "akses_sempit": (
        "Lebar jalan kurang dari 3 meter dari badan jalan atau hanya berupa gang "
        "kecuali untuk area tertentu yang marketable."
    ),
    "berbatasan_lokasi_berisiko": (
        "Berbatasan langsung dengan dan/atau merupakan bagian dari kuburan keluarga "
        "(tanah wakaf), kuburan umum, Gardu Listrik (khusus area hunian), rumah abu, "
        "rumah duka, jalur kereta api, tempat pembuangan sampah/tempat pembuangan "
        "akhir/limbah, tempat penyimpanan dan/atau kegiatan yang berhubungan dengan "
        "Bahan Berbahaya dan Beracun (B3), dan/atau tempat berpotensi tinggi seperti "
        "tempat latih tempur militer, tempat produksi/penyimpanan bahan peledak - "
        "sangat mudah terbakar seperti depot kilang minyak dan/atau gas atau reaktor nuklir."
    ),
}


def hitung_faktor_pengurang(auto_flags: dict, manual_scores: dict, restriksi_flags: dict = None) -> dict:
    """
    auto_flags: {"flood_risk": True/False, "sutet": True/False, ...}
    manual_scores: {"bentuk_tanah": 0-3, ...} (0 = tidak masalah, 3 = masalah berat)
    restriksi_flags: {"sengketa_hukum": True/False, ...} - faktor pembatas/red-flag
        tambahan sesuai SOP (lihat DEFAULT_RESTRIKSI_WEIGHTS/RESTRIKSI_LABELS).

    Mengembalikan dict berisi total_faktor_pengurang (0-1), status_risiko,
    confidence_level (perkiraan berdasarkan kelengkapan data), serta
    ada_restriksi (bool) dan restriksi_aktif (list key) untuk keperluan
    menampilkan peringatan eksplisit di UI.
    """
    restriksi_flags = restriksi_flags or {}

    total = 0.0
    for key, is_present in auto_flags.items():
        if is_present:
            total += DEFAULT_AUTO_WEIGHTS.get(key, 0.0)

    for key, score in manual_scores.items():
        weight = DEFAULT_MANUAL_WEIGHTS.get(key, 0.0)
        # score diasumsikan 0-3, dinormalisasi ke 0-1 lalu dikali bobot
        total += weight * (score / 3.0)

    restriksi_aktif = [key for key, is_present in restriksi_flags.items() if is_present]
    for key in restriksi_aktif:
        total += DEFAULT_RESTRIKSI_WEIGHTS.get(key, 0.0)

    # SOP: batas maksimum Faktor Pengurang adalah 30% (bukan 50%).
    total = max(0.0, min(total, 0.30))

    # 4 level status risiko sesuai SOP (Hijau/Kuning/Oranye/Merah), dibagi rata
    # dari 0% s.d. batas maksimum 30%. Ambang batas ini adalah default yang bisa
    # disesuaikan tim Appraisal.
    if total < 0.075:
        status = "Hijau"
    elif total < 0.15:
        status = "Kuning"
    elif total < 0.225:
        status = "Oranye"
    else:
        status = "Merah"

    filled = sum(1 for v in auto_flags.values() if v is not None) + sum(
        1 for v in manual_scores.values() if v is not None
    )
    total_fields = len(auto_flags) + len(manual_scores)
    confidence = round(filled / total_fields, 2) if total_fields else 0.0

    return {
        "total_faktor_pengurang": round(total, 4),
        "status_risiko": status,
        "confidence_level": confidence,
        "ada_restriksi": bool(restriksi_aktif),
        "restriksi_aktif": restriksi_aktif,
    }



# ---------------------------------------------------------------------------
# STEP 4b - Estimasi otomatis sebagian item Manual Checklist
# ---------------------------------------------------------------------------
# Rule tetap (tanpa API) untuk menurunkan skor "legalitas" (0-3) dari status
# sertifikat yang sudah diisi appraiser di Step 1. 0 = paling aman, 3 = paling
# berisiko.
LEGALITAS_SCORE_BY_SERTIFIKAT = {
    "SHM": 0,
    "SHGB": 1,
    "SHMASRS": 1,
    "Girik": 3,
    "Lainnya": 2,
}


def estimasi_legalitas_score(status_sertifikat: str) -> int:
    """Skor legalitas (0-3) berdasarkan status sertifikat. Deterministik, tanpa API."""
    return LEGALITAS_SCORE_BY_SERTIFIKAT.get(status_sertifikat, 2)


def estimasi_dari_flag_risiko(auto_flags: dict) -> dict:
    """
    Menurunkan perkiraan skor "akses_jalan" dan "kondisi_lingkungan" (0-3)
    dari flag risiko lokasi yang SUDAH dideteksi oleh Pinpoint Agent di Step 4
    (reuse hasil yang ada, tidak perlu pencarian/LLM tambahan).

    - akses_jalan: dekat jalan utama (main_road=True) dianggap akses baik -> skor 0,
      kalau tidak terdeteksi -> skor 1 (perlu verifikasi, bukan otomatis dianggap buruk).
    - kondisi_lingkungan: makin banyak flag risiko lingkungan (banjir, SUTET, rel
      kereta, kawasan industri) yang aktif, makin tinggi skornya (dibatasi maks 3).
    """
    akses_jalan = 0 if auto_flags.get("main_road") else 1

    risk_keys = ["flood_risk", "sutet", "railway", "industry"]
    n_risk = sum(1 for k in risk_keys if auto_flags.get(k))
    kondisi_lingkungan = min(n_risk, 3)

    return {"akses_jalan": akses_jalan, "kondisi_lingkungan": kondisi_lingkungan}


# ---------------------------------------------------------------------------
# STEP 5 - Nilai Pasar Awal
# ---------------------------------------------------------------------------
def hitung_nilai_pasar_awal(
    nilai_tanah: float, nilai_bangunan: float, faktor_pengurang: float
) -> float:
    """Nilai Pasar Awal = (Nilai Tanah + Nilai Bangunan) x (1 - Faktor Pengurang)"""
    nilai_properti = nilai_tanah + nilai_bangunan
    return round(nilai_properti * (1 - faktor_pengurang), 2)


# ---------------------------------------------------------------------------
# STEP 6/7 - Statistik Pembanding & Validasi
# ---------------------------------------------------------------------------
def statistik_pembanding(harga_per_m2_list: list) -> dict:
    if not harga_per_m2_list:
        return {"average": 0, "median": 0, "minimum": 0, "maximum": 0}
    data = sorted(harga_per_m2_list)
    n = len(data)
    avg = sum(data) / n
    median = data[n // 2] if n % 2 == 1 else (data[n // 2 - 1] + data[n // 2]) / 2
    return {
        "average": round(avg, 2),
        "median": round(median, 2),
        "minimum": round(min(data), 2),
        "maximum": round(max(data), 2),
    }


def nilai_median_untuk_luas(harga_per_m2_list: list, luas_target: float) -> dict:
    """
    Dari daftar harga/m2 sejumlah N properti pembanding (mis. 12 pembanding),
    hitung median harga/m2 lalu proyeksikan ke luas properti subjek (luas_target).
    Contoh: 12 pembanding -> median Rp/m2 -> "Median untuk 120 m2: Rp X".
    """
    stats = statistik_pembanding(harga_per_m2_list)
    total_median = round(stats["median"] * luas_target, 2) if luas_target else 0.0
    return {
        "n_pembanding": len(harga_per_m2_list),
        "median_per_m2": stats["median"],
        "luas_target": luas_target,
        "total_median": total_median,
    }


def rentang_nilai_pasar(nilai_pasar_awal: float, comparable_stats_total: dict) -> dict:
    """
    Alih-alih memaksa SATU angka tunggal saat Nilai Pasar Awal (internal, dari
    pendekatan biaya/ZNT) berbeda jauh dari data pembanding pasar (mis. internal
    Rp500jt vs pembanding rata-rata Rp900jt), sistem sekarang menyajikan RENTANG
    nilai pasar yang wajar, plus satu titik estimasi (point estimate) kalau
    appraiser tetap butuh angka tunggal untuk keperluan lain (mis. LTV bank).

    comparable_stats_total: hasil statistik_pembanding() yang averagenya SUDAH
    dikonversi ke total (bukan per-m2) - kalikan average/median/minimum/maximum
    dengan luas tanah subjek sebelum dipanggil di sini.

    - min/max rentang = batas terendah & tertinggi dari (Nilai Pasar Awal,
      minimum pembanding, maximum pembanding) - jadi rentang mencakup baik
      estimasi internal maupun sebaran pasar.
    - point = median Nilai Pasar Awal & median pembanding (titik tengah yang
      mempertimbangkan kedua sumber secara seimbang). Median dipakai (bukan
      rata-rata/average) karena median jauh lebih tahan terhadap listing
      pembanding yang harganya outlier/ekstrem (mis. satu listing yang jauh
      lebih mahal/murah dari yang lain akan menarik AVERAGE ke arahnya,
      sedangkan MEDIAN tetap merepresentasikan nilai "tengah" yang wajar
      dari sebaran pembanding).
    """
    minimum = comparable_stats_total.get("minimum", 0) or 0
    maximum = comparable_stats_total.get("maximum", 0) or 0
    median = comparable_stats_total.get("median", 0) or 0

    kandidat = [v for v in [nilai_pasar_awal, minimum, maximum] if v]
    if not kandidat:
        return {"min": nilai_pasar_awal, "max": nilai_pasar_awal, "point": nilai_pasar_awal}

    point = round((nilai_pasar_awal + median) / 2, 2) if median else nilai_pasar_awal
    return {
        "min": min(kandidat),
        "max": max(kandidat),
        "point": point,
    }


def bandingkan_harga_pengajuan(harga_pengajuan: float, nilai_appraisal: float) -> dict:
    """
    Membandingkan Harga yang Diajukan pemilik/pemohon (diisi di Step 1) dengan
    hasil appraisal sistem (mis. Nilai Pasar Akhir, atau Nilai Bangunan untuk
    keperluan kalkulator penyusutan). Dipakai di Step 10 (Perbandingan Harga &
    Depresiasi Final).
    """
    if not harga_pengajuan:
        return {"available": False}
    selisih_pct = hitung_selisih_pct(nilai_appraisal, harga_pengajuan)
    return {
        "available": True,
        "harga_pengajuan": harga_pengajuan,
        "nilai_appraisal": nilai_appraisal,
        "selisih_pct": selisih_pct,
        "lebih_tinggi_dari_pengajuan": nilai_appraisal > harga_pengajuan,
    }


def validasi_nilai_pasar(
    nilai_pasar_awal: float, average_comparable_total: float, toleransi: float = 0.10
) -> dict:
    """
    Membandingkan Nilai Pasar Awal terhadap rata-rata nilai pembanding.
    toleransi default 10%.
    """
    if average_comparable_total == 0:
        diff_pct = 0.0
    else:
        diff_pct = (nilai_pasar_awal - average_comparable_total) / average_comparable_total

    within_tolerance = abs(diff_pct) <= toleransi
    status = "Diterima" if within_tolerance else "Perlu Review"
    if within_tolerance:
        rekomendasi = "Nilai Pasar Awal dapat diterima sebagai Nilai Pasar Akhir."
    elif diff_pct > 0:
        rekomendasi = "Nilai Pasar Awal lebih tinggi dari pembanding. Pertimbangkan revisi turun."
    else:
        rekomendasi = "Nilai Pasar Awal lebih rendah dari pembanding. Pertimbangkan revisi naik."

    return {
        "difference_pct": round(diff_pct * 100, 2),
        "within_tolerance": within_tolerance,
        "status": status,
        "recommendation": rekomendasi,
    }


# ---------------------------------------------------------------------------
# STEP 8 - Nilai Likuidasi
# ---------------------------------------------------------------------------
# [KONFIGURASI] Rasio likuidasi resmi per status sertifikat BELUM tersedia dari
# tim Appraisal (lihat catatan SOP Tahap 8). Tabel di bawah adalah PROXY
# sementara (urutan relatif masuk akal: legalitas lebih kuat -> lebih likuid),
# bukan angka resmi. Ganti nilai-nilai ini begitu tabel resmi tersedia -
# struktur (dict per status_sertifikat) sudah siap dipakai tanpa mengubah
# pemanggil di app.py.
DEFAULT_RASIO_LIKUIDASI_BY_SERTIFIKAT = {
    "SHM": 0.85,
    "SHGB": 0.80,
    "SHMASRS": 0.78,
    "Girik": 0.65,
    "Lainnya": 0.70,
}


def estimasi_rasio_likuidasi(status_sertifikat: str) -> float:
    """Rasio likuidasi proxy (0-1) berdasarkan status sertifikat. Lihat catatan [KONFIGURASI] di atas."""
    return DEFAULT_RASIO_LIKUIDASI_BY_SERTIFIKAT.get(status_sertifikat, 0.75)


def hitung_nilai_likuidasi(nilai_pasar_akhir: float, rasio_likuidasi: float = 0.80) -> float:
    """Nilai Likuidasi = Nilai Pasar Akhir x Rasio Likuidasi (default 80%)"""
    return round(nilai_pasar_akhir * rasio_likuidasi, 2)


def hitung_nilai_likuidasi_range(
    nilai_pasar_akhir: float, rasio_min: float = 0.70, rasio_max: float = 0.85
) -> dict:
    """
    PERUBAHAN KEBIJAKAN: Nilai Likuidasi (dan turunannya, plafon pinjaman/loan)
    sekarang disajikan sebagai RENTANG (range), bukan satu angka tunggal - rasio
    likuidasi memang bervariasi tergantung kondisi pasar/likuiditas aset saat
    penjualan cepat, jadi satu titik tunggal bisa menyesatkan.

    rasio_min/rasio_max: batas bawah/atas rasio likuidasi (desimal, mis. 0.70-0.85).
    Mengembalikan {"min", "max", "mid"} dalam Rupiah - "mid" adalah titik tengah
    rentang untuk referensi cepat kalau appraiser tetap butuh satu angka.
    """
    if rasio_min > rasio_max:
        rasio_min, rasio_max = rasio_max, rasio_min
    nilai_min = round(nilai_pasar_akhir * rasio_min, 2)
    nilai_max = round(nilai_pasar_akhir * rasio_max, 2)
    return {
        "min": nilai_min,
        "max": nilai_max,
        "mid": round((nilai_min + nilai_max) / 2, 2),
        "rasio_min": rasio_min,
        "rasio_max": rasio_max,
    }


# ---------------------------------------------------------------------------
# STEP 9 - Analisis NJOP
# ---------------------------------------------------------------------------
def analisis_njop(
    njop_tanah: Optional[float],
    njop_bangunan: Optional[float],
    nilai_pasar_akhir: float,
) -> dict:
    if njop_tanah is None and njop_bangunan is None:
        return {"available": False}

    total_njop = (njop_tanah or 0) + (njop_bangunan or 0)
    rasio = round(total_njop / nilai_pasar_akhir, 4) if nilai_pasar_akhir else 0.0
    return {
        "available": True,
        "njop_tanah": njop_tanah or 0,
        "njop_bangunan": njop_bangunan or 0,
        "total_njop": round(total_njop, 2),
        "rasio_njop": rasio,
    }


def hitung_selisih_pct(nilai: float, nilai_acuan: float) -> Optional[float]:
    """
    Selisih persentase `nilai` terhadap `nilai_acuan` (mis. harga/m² pembanding
    vs harga/m² tersirat dari Nilai Pasar Awal subjek). None kalau nilai acuan
    tidak tersedia/nol, supaya tidak menyesatkan (bukan 0%).
    """
    if not nilai_acuan:
        return None
    return round(((nilai - nilai_acuan) / nilai_acuan) * 100, 2)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Jarak garis lurus (great-circle) antara dua koordinat, dalam km."""
    from math import radians, sin, cos, sqrt, atan2

    r = 6371.0  # radius bumi rata-rata, km
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return r * 2 * atan2(sqrt(a), sqrt(1 - a))


def _dalam_toleransi(nilai_subjek, nilai_pembanding, toleransi_pct: float) -> bool:
    """
    True kalau nilai_pembanding berada dalam +/- toleransi_pct dari nilai_subjek.
    Kalau salah satu nilai tidak tersedia (0/None), dianggap TIDAK bisa
    diverifikasi -> tetap True (jangan otomatis exclude karena data kosong,
    biar appraiser yang memutuskan lewat centang "include" manual).

    toleransi_pct <= 0 berarti "tanpa toleransi" (filter dimatikan untuk
    kriteria ini) -> semua pembanding dianggap memenuhi, BUKAN "harus persis
    sama" (kalau diperlakukan sebagai match presisi, hampir semua pembanding
    riil akan langsung gugur karena luasnya nyaris tidak pernah identik).
    """
    if toleransi_pct <= 0:
        return True
    if not nilai_subjek or not nilai_pembanding:
        return True
    lo = nilai_subjek * (1 - toleransi_pct)
    hi = nilai_subjek * (1 + toleransi_pct)
    return lo <= nilai_pembanding <= hi


def memenuhi_kriteria_pembanding(
    subjek: dict, pembanding: dict, radius_km: float = 5.0,
    luas_tanah_toleransi_pct: float = 0.20, luas_bangunan_toleransi_pct: float = 0.20,
) -> dict:
    """
    Cek kriteria pembanding SESUAI kebijakan baru:
    - luas tanah pembanding harus dalam toleransi luas_tanah_toleransi_pct
      (default +/-20%, bisa dikustomisasi terpisah dari luas bangunan) dari
      luas tanah subjek, DAN luas bangunan pembanding harus dalam toleransi
      luas_bangunan_toleransi_pct (default +/-20%, juga bisa dikustomisasi
      terpisah) dari luas bangunan subjek.
    - jarak pembanding ke subjek (distance_km, hasil geocoding) HARUS berada
      dalam radius maksimum (default 5km, bisa dikustomisasi).
    Mengembalikan dict {"luas_ok": bool, "jarak_ok": bool, "ok": bool}.
    Kalau distance_km belum diketahui (geocoding gagal/belum dijalankan),
    jarak_ok diberi nilai None supaya UI bisa menampilkan "tidak diketahui"
    alih-alih salah menandai sebagai memenuhi/tidak memenuhi kriteria.
    """
    luas_ok = (
        _dalam_toleransi(subjek.get("luas_tanah"), pembanding.get("luas_tanah"), luas_tanah_toleransi_pct)
        and _dalam_toleransi(subjek.get("luas_bangunan"), pembanding.get("luas_bangunan"), luas_bangunan_toleransi_pct)
    )
    dist = pembanding.get("distance_km")
    jarak_ok = None if dist is None else (dist <= radius_km)
    ok = luas_ok and bool(jarak_ok)
    return {"luas_ok": luas_ok, "jarak_ok": jarak_ok, "ok": ok}


def similarity_score(subjek: dict, pembanding: dict, radius_km: float = 5.0) -> float:
    """
    Skor kemiripan (0-100) antara properti subjek dan properti pembanding.

    PERUBAHAN KEBIJAKAN: skor sekarang MENITIKBERATKAN pada kedekatan JARAK
    (pembanding paling dekat ke subjek = skor tertinggi), bukan lagi pada
    seberapa mirip luasnya. Luas tanah/bangunan tetap dipertimbangkan (dengan
    syarat sudah berada dalam toleransi +/-20%, lihat `memenuhi_kriteria_pembanding`)
    tapi bobotnya jauh lebih kecil daripada jarak. Komponen tahun bangun
    dihapus dari skor karena dominan jarak & luas sudah lebih relevan untuk
    keperluan pembanding pasar.

    Bobot: 70% jarak, 30% kemiripan luas.
    - Skor jarak = 100 kalau jarak 0 km, menurun linear ke 0 saat jarak = radius_km,
      dan 0 kalau lebih jauh dari radius_km.
    - Kalau jarak tidak diketahui (geocoding gagal), skor jarak dianggap netral (50).
    """
    dist = pembanding.get("distance_km")
    if dist is None:
        skor_jarak = 50.0
    elif radius_km <= 0:
        skor_jarak = 0.0
    else:
        skor_jarak = 100.0 * max(0.0, 1 - (dist / radius_km))

    def rel_diff(a, b):
        if not a or not b:
            return 0.5
        return abs(a - b) / max(a, b)

    d_lt = rel_diff(subjek.get("luas_tanah"), pembanding.get("luas_tanah"))
    d_lb = rel_diff(subjek.get("luas_bangunan"), pembanding.get("luas_bangunan"))
    skor_luas = 100 * (1 - (0.5 * d_lt + 0.5 * d_lb))
    skor_luas = max(0.0, min(skor_luas, 100.0))

    score = (0.7 * skor_jarak) + (0.3 * skor_luas)
    return round(max(0.0, min(score, 100.0)), 1)
