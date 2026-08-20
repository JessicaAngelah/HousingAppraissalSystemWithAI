"""
agents.py
"Agent" di sini bukan agent otonom yang berjalan sendiri, melainkan
fungsi orkestrasi: Serper untuk mencari data mentah di web, lalu
Groq/Gemini untuk mengekstraksi/meringkas data tersebut menjadi
struktur yang dipakai oleh calculations.py.

PENTING - keterbatasan jujur:
Bhumi ATR/BPN (https://bhumi.atrbpn.go.id) tidak menyediakan API publik
resmi untuk Zona Nilai Tanah (ZNT), dan halamannya berbasis peta interaktif
yang tidak bisa di-scrape lewat pencarian teks biasa. Karena itu ZNTAgent
di sini melakukan *pencarian web* (berita/rilis ZNT terdekat, data properti
sekitar, dsb) lalu meminta LLM membuat ESTIMASI ZNT dengan confidence level
yang jujur dinyatakan rendah/sedang. Appraiser tetap harus mengisi/mengoreksi
angka ZNT final secara manual (lihat parameter allow_manual_override di UI).
"""

import json
import re
import time
import concurrent.futures
from api_clients import SerperClient, GroqClient, GeminiClient
from geocode import geocode_address, geocode_search_with_places_fallback
import calculations as calc
# bhumi_agent is imported lazily inside run_znt_agent (see below) because it
# does `from playwright.async_api import async_playwright` at module level -
# if Playwright isn't installed yet, we still want the rest of the app to
# work and gracefully fall back to the web-search estimate.


def _t(lang: str, id_text: str, en_text: str) -> str:
    """Pilih teks Indonesia atau Inggris tergantung `lang` ('id'/'en') -
    dipakai di semua log/emit di bawah supaya progress log ikut toggle
    bahasa di sidebar, bukan cuma teks UI statis di app.py."""
    return en_text if lang == "en" else id_text


def _pick_llm(groq: GroqClient, gemini: GeminiClient):
    """Pilih LLM yang key-nya tersedia; Groq diprioritaskan karena lebih cepat/murah."""
    if groq.api_key:
        return "groq", groq
    if gemini.api_key:
        return "gemini", gemini
    return None, None


def run_znt_agent(alamat: str, provinsi: str, kabkota: str, kecamatan: str,
                   lat, lon,
                   serper: SerperClient, groq: GroqClient, gemini: GeminiClient,
                   log_callback=None, lang: str = "id"):
    """
    Mengembalikan (status_log: list[str], result: dict)
    result keys: kode_zona, zona_nilai_tanah, harga_znt_per_m2,
                 tanggal_data, confidence_level, source_notes

    Urutan usaha:
    1. Kalau lat/lon belum ada, geocode alamat dulu (OpenStreetMap Nominatim).
    2. Coba ambil data ZNT ASLI langsung dari peta Bhumi ATR/BPN lewat
       BhumiZntAgent (Playwright) - ini butuh `pip install playwright` +
       `playwright install chromium` sudah dijalankan di server.
    3. Kalau langkah 2 gagal (Playwright belum terpasang, situs berubah,
       koordinat di luar layer ZNT, dll), fallback ke estimasi lama:
       pencarian web (Serper) + LLM (Groq/Gemini), dengan confidence
       yang jujur dinyatakan lebih rendah.
    """
    log = []

    def emit(msg):
        log.append(msg)
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    def t(id_text, en_text):
        return _t(lang, id_text, en_text)

    # --- 1. Pastikan ada koordinat ---
    if not lat or not lon:
        full_address = f"{alamat}, {kecamatan}, {kabkota}, {provinsi}, Indonesia"
        emit(t(f"Mencari koordinat properti dari alamat: {full_address}",
               f"Searching for property coordinates from address: {full_address}"))
        # PENTING: sebelumnya di sini cuma pakai geocode_address() (Nominatim
        # / OpenStreetMap saja) - alamat Indonesia yang detail sampai ke
        # gang/RT-RW (mis. "Gg. Mufakat No.Rt 04/02...") SERING tidak ada di
        # data OSM walau lokasinya jelas ada & gampang ditemukan di Google
        # Maps, jadi ZNT langsung menyerah ke estimasi fallback padahal
        # koordinat sebenarnya bisa didapat lewat Google Places (function ini
        # sudah dipakai di tempat lain di app, mis. Step 1/2, tapi sebelumnya
        # tidak diikutsertakan di sini). Sekarang pakai fallback yang sama.
        ok, candidates, geo_source = geocode_search_with_places_fallback(full_address, serper=serper, limit=1)
        if ok and candidates:
            lat, lon = candidates[0]["lat"], candidates[0]["lon"]
            sumber_label = "Google Places" if geo_source == "google_places" else "Nominatim/OSM"
            emit(t(f"✓ Koordinat ditemukan ({sumber_label}): {lat:.6f}, {lon:.6f}",
                   f"✓ Coordinates found ({sumber_label}): {lat:.6f}, {lon:.6f}"))
        else:
            emit(t(f"⚠ Gagal menemukan koordinat presisi untuk alamat lengkap: {candidates}",
                   f"⚠ Could not find precise coordinates for the full address: {candidates}"))
            # Fallback KEDUA: alamat lengkap sampai gang/RT-RW seringkali
            # justru bikin geocoder (baik Nominatim maupun Google Places)
            # gagal parsing sama sekali - bukan cuma tidak ketemu tapi query-
            # nya sendiri terlalu spesifik/"berisik" untuk dicocokkan. Coba
            # lagi dengan alamat yang dipangkas ke tingkat kecamatan/kota
            # saja (masih cukup presisi untuk lookup ZNT yang memang berbasis
            # zona/wilayah, bukan titik persis per rumah) sebelum benar-benar
            # menyerah ke estimasi pencarian web.
            short_address = f"{kecamatan}, {kabkota}, {provinsi}, Indonesia"
            emit(t(f"Mencoba lagi dengan alamat yang lebih singkat: {short_address}",
                   f"Retrying with a shorter address: {short_address}"))
            ok, candidates, geo_source = geocode_search_with_places_fallback(short_address, serper=serper, limit=1)
            if ok and candidates:
                lat, lon = candidates[0]["lat"], candidates[0]["lon"]
                sumber_label = "Google Places" if geo_source == "google_places" else "Nominatim/OSM"
                emit(t(f"✓ Koordinat ditemukan dari alamat singkat ({sumber_label}): {lat:.6f}, {lon:.6f} "
                       "(catatan: ini titik tengah kecamatan, bukan lokasi persis rumah - cukup akurat "
                       "untuk lookup ZNT karena datanya memang berbasis zona/wilayah).",
                       f"✓ Coordinates found from the shortened address ({sumber_label}): {lat:.6f}, {lon:.6f} "
                       "(note: this is the district's center point, not the exact house location - "
                       "accurate enough for ZNT lookup since the data is zone/area-based anyway)."))
            else:
                emit(t(f"⚠ Gagal menemukan koordinat otomatis: {candidates}",
                       f"⚠ Failed to find coordinates automatically: {candidates}"))

    # --- 2. Coba data resmi Bhumi ATR/BPN ---
    if lat and lon:
        emit(t("Menghubungkan ke Bhumi ATR/BPN...", "Connecting to Bhumi ATR/BPN..."))
        emit(t("Mengaktifkan layer Zona Nilai Tanah...", "Enabling the Land Value Zone layer..."))
        try:
            from bhumi_agent import run_bhumi_znt_sync, map_bhumi_result_to_app_schema

            bhumi_raw = run_bhumi_znt_sync(
                lat=lat, lng=lon, headed=False,
                api_key=gemini.api_key or None,
                log_callback=emit,
                lang=lang,
            )
            has_data = bhumi_raw and (bhumi_raw.get("kode_zona") or bhumi_raw.get("nilai_min") or bhumi_raw.get("nilai_max"))
            if has_data:
                emit(t("✓ Data ZNT resmi berhasil diambil dari Bhumi ATR/BPN",
                       "✓ Official ZNT data successfully retrieved from Bhumi ATR/BPN"))
                mapped = map_bhumi_result_to_app_schema(bhumi_raw, lang=lang)
                mapped["lat"] = lat
                mapped["lon"] = lon
                return log, mapped
            else:
                emit(t("⚠ Layer ZNT kosong / tidak ada data di titik koordinat ini.",
                       "⚠ ZNT layer is empty / no data at this coordinate point."))
        except ImportError:
            emit(t("⚠ Playwright belum terpasang. Jalankan: pip install playwright && playwright install chromium",
                   "⚠ Playwright is not installed. Run: pip install playwright && playwright install chromium"))
        except Exception as e:
            emit(t(f"⚠ Pengambilan data resmi Bhumi ATR/BPN gagal: {e}",
                   f"⚠ Failed to retrieve official Bhumi ATR/BPN data: {e}"))

    # --- 3. Fallback: estimasi via pencarian web + LLM ---
    emit(t("Memakai estimasi cadangan (pencarian web + AI)...",
           "Using fallback estimate (web search + AI)..."))
    query = f"Zona Nilai Tanah ZNT {kecamatan} {kabkota} {provinsi} bhumi ATR BPN"

    ok, search_result = serper.search(query, num=8)
    if not ok:
        emit(t(f"⚠ Pencarian gagal: {search_result}", f"⚠ Search failed: {search_result}"))
        snippets = ""
    else:
        emit(t("✓ Data pencarian diterima", "✓ Search data received"))
        organic = search_result.get("organic", [])[:6]
        snippets = "\n".join(
            f"- {o.get('title','')}: {o.get('snippet','')}" for o in organic
        )

    engine_name, llm = _pick_llm(groq, gemini)
    if not llm:
        emit(t("⚠ Tidak ada API key LLM (Groq/Gemini) untuk analisis lanjutan.",
               "⚠ No LLM API key (Groq/Gemini) available for further analysis."))
        return log, {
            "kode_zona": "-",
            "zona_nilai_tanah": "-",
            "harga_znt_per_m2": 0,
            "tanggal_data": "-",
            "confidence_level": t("Rendah (perlu input manual)", "Low (needs manual input)"),
            "source_notes": t("Tidak ada LLM key tersedia; isi manual.", "No LLM key available; fill in manually."),
            "lat": lat, "lon": lon,
        }

    emit(t("✓ Menganalisis data dengan LLM...", "✓ Analyzing data with LLM..."))
    prompt_system = (
        "Anda adalah asisten penilaian properti di Indonesia. Anda akan diberi "
        "cuplikan hasil pencarian web terkait Zona Nilai Tanah (ZNT). Perkirakan "
        "nilai ZNT per m2 untuk lokasi yang diminta HANYA jika ada indikasi jelas "
        "di cuplikan; jika tidak ada data yang cukup, kembalikan confidence_level "
        "'Rendah' dan harga_znt_per_m2: 0. JANGAN mengarang angka spesifik yang "
        "tidak didukung data. Jawab HANYA dalam JSON dengan keys: "
        "kode_zona, zona_nilai_tanah, harga_znt_per_m2 (angka, rupiah), "
        "tanggal_data, confidence_level (Rendah/Sedang/Tinggi), source_notes."
    )
    prompt_user = f"Lokasi: {alamat}, {kecamatan}, {kabkota}, {provinsi}\n\nCuplikan pencarian:\n{snippets}"

    if engine_name == "groq":
        ok, data = llm.chat(prompt_system, prompt_user, json_mode=True)
    else:
        ok, data = llm.generate(prompt_system + "\n\n" + prompt_user, json_mode=True)

    if not ok:
        emit(t(f"⚠ Analisis LLM gagal: {data}", f"⚠ LLM analysis failed: {data}"))
        return log, {
            "kode_zona": "-",
            "zona_nilai_tanah": "-",
            "harga_znt_per_m2": 0,
            "tanggal_data": "-",
            "confidence_level": t("Rendah (perlu input manual)", "Low (needs manual input)"),
            "source_notes": str(data),
            "lat": lat, "lon": lon,
        }

    emit(t("✓ Menghitung Nilai Tanah", "✓ Calculating Land Value"))
    data["lat"] = lat
    data["lon"] = lon
    return log, data


def run_pinpoint_agent(alamat: str, lat: float, lon: float,
                        serper: SerperClient, groq: GroqClient, gemini: GeminiClient,
                        lang: str = "id"):
    """
    Mendeteksi faktor risiko lokasi (banjir, SUTET, rel kereta, dsb).
    Mengembalikan (log, auto_flags: dict[str,bool], notes: dict[str,str])
    """
    log = []
    checks = {
        "flood_risk": "riwayat banjir daerah",
        "sutet": "SUTET tegangan tinggi dekat",
        "railway": "rel kereta api dekat",
        "industry": "kawasan industri pabrik dekat",
        "hospital": "rumah sakit terdekat",
        "school": "sekolah terdekat",
        "market": "pasar terdekat",
        "main_road": "akses jalan utama",
        "public_facilities": "fasilitas umum terdekat",
    }
    label_id = {
        "flood_risk": "Mendeteksi area banjir",
        "sutet": "Mendeteksi jalur SUTET",
        "railway": "Mendeteksi rel kereta",
        "industry": "Mendeteksi kawasan industri",
        "hospital": "Mendeteksi rumah sakit",
        "school": "Mendeteksi sekolah",
        "market": "Mendeteksi pasar",
        "main_road": "Mendeteksi akses jalan utama",
        "public_facilities": "Mendeteksi fasilitas umum",
    }
    label_en = {
        "flood_risk": "Detecting flood-prone areas",
        "sutet": "Detecting high-voltage power lines (SUTET)",
        "railway": "Detecting railway lines",
        "industry": "Detecting industrial areas",
        "hospital": "Detecting hospitals",
        "school": "Detecting schools",
        "market": "Detecting markets",
        "main_road": "Detecting main road access",
        "public_facilities": "Detecting public facilities",
    }
    labels = label_en if lang == "en" else label_id

    log.append("Analyzing location..." if lang == "en" else "Menganalisis lokasi...")
    snippets_all = {}
    for key, topic in checks.items():
        log.append(f"{labels[key]}...")
        query = f"{topic} {alamat}"
        ok, res = serper.search(query, num=5)
        if ok:
            organic = res.get("organic", [])[:3]
            snippets_all[key] = "\n".join(
                f"- {o.get('title','')}: {o.get('snippet','')}" for o in organic
            )
        else:
            snippets_all[key] = ""

    engine_name, llm = _pick_llm(groq, gemini)
    if not llm:
        if lang == "en":
            log.append("⚠ No LLM API key; all flags set to False (needs manual review).")
            return log, {k: False for k in checks}, {k: "LLM not available" for k in checks}
        log.append("⚠ Tidak ada API key LLM; semua flag di-set False (perlu review manual).")
        return log, {k: False for k in checks}, {k: "LLM tidak tersedia" for k in checks}

    prompt_system = (
        "Anda asisten penilaian properti. Berdasarkan cuplikan pencarian web untuk "
        "tiap topik risiko lokasi, tentukan apakah risiko tersebut TERDETEKSI (true) "
        "atau TIDAK (false) untuk alamat yang diberikan. Jika data tidak cukup jelas, "
        "gunakan false dan catat di notes. Jawab HANYA JSON dengan struktur: "
        '{"flags": {"flood_risk": bool, "sutet": bool, "railway": bool, "industry": bool, '
        '"hospital": bool, "school": bool, "market": bool, "main_road": bool, '
        '"public_facilities": bool}, "notes": {"<key>": "alasan singkat", ...}}'
    )
    if lang == "en":
        prompt_system += (
            "\n\nWrite every value under \"notes\" in English (translate/summarize the "
            "Indonesian search snippets into a short English reason), even though the "
            "search snippets you're given are in Indonesian."
        )
    combined = "\n\n".join(f"## {k}\n{v}" for k, v in snippets_all.items())
    prompt_user = f"Alamat: {alamat} (lat={lat}, lon={lon})\n\n{combined}"

    if engine_name == "groq":
        ok, data = llm.chat(prompt_system, prompt_user, json_mode=True)
    else:
        ok, data = llm.generate(prompt_system + "\n\n" + prompt_user, json_mode=True)

    if not ok or "flags" not in data:
        if lang == "en":
            log.append(f"⚠ LLM analysis failed: {data}")
            return log, {k: False for k in checks}, {k: "Analysis failed" for k in checks}
        log.append(f"⚠ Analisis LLM gagal: {data}")
        return log, {k: False for k in checks}, {k: "Analisis gagal" for k in checks}

    log.append("✓ Location analysis complete" if lang == "en" else "✓ Analisis lokasi selesai")
    return log, data.get("flags", {}), data.get("notes", {})


def run_manual_checklist_agent(alamat: str, kecamatan: str, kabkota: str, provinsi: str,
                                status_sertifikat: str, auto_flags: dict,
                                serper: SerperClient, groq: GroqClient, gemini: GeminiClient,
                                lang: str = "id"):
    """
    Best-effort otomatisasi untuk SEBAGIAN item "Manual Checklist" di Step 4,
    supaya appraiser tidak perlu menggeser semua 10 slider dari nol setiap kali.

    Yang BISA diperkirakan otomatis secara jujur (tanpa mengarang data fisik):
    - legalitas          -> rule tetap dari status_sertifikat (deterministik)
    - akses_jalan        -> diturunkan dari flag 'main_road' (reuse hasil Pinpoint Agent)
    - kondisi_lingkungan -> diturunkan dari flag risiko lingkungan (reuse hasil Pinpoint Agent)
    - peruntukan_lahan   -> pencarian web (RTRW/zonasi) + LLM

    6 item sisanya (bentuk_tanah, kontur_tanah, posisi_tanah, kondisi_bangunan,
    kualitas_konstruksi, perawatan_bangunan) TIDAK diisi otomatis - item-item ini
    butuh pengamatan fisik/foto lapangan langsung, dan mengarang angkanya lewat
    pencarian teks akan menyesatkan, bukan membantu. Appraiser tetap mengisi
    manual untuk 6 item tersebut.

    Return: (log, scores: dict, notes: dict, auto_keys: set[str])
    """
    _en = lang == "en"
    log = []
    scores = {}
    notes = {}

    # --- rule-based, tanpa API sama sekali ---
    scores["legalitas"] = calc.estimasi_legalitas_score(status_sertifikat)
    notes["legalitas"] = (
        f"Standard rule based on certificate status: {status_sertifikat}" if _en
        else f"Aturan baku berdasarkan status sertifikat: {status_sertifikat}"
    )
    log.append(
        f"✓ Legality estimated from certificate status ({status_sertifikat})" if _en
        else f"✓ Legalitas diperkirakan dari status sertifikat ({status_sertifikat})"
    )

    derived = calc.estimasi_dari_flag_risiko(auto_flags)
    scores.update(derived)
    if _en:
        notes["akses_jalan"] = (
            "Detected near a main road (from the Step 4 pinpoint analysis)"
            if auto_flags.get("main_road")
            else "Not detected near a main road in the pinpoint analysis - please verify"
        )
    else:
        notes["akses_jalan"] = (
            "Terdeteksi dekat jalan utama (dari analisis pinpoint Step 4)"
            if auto_flags.get("main_road")
            else "Tidak terdeteksi dekat jalan utama pada analisis pinpoint - mohon verifikasi"
        )
    n_risk = sum(1 for k in ["flood_risk", "sutet", "railway", "industry"] if auto_flags.get(k))
    if _en:
        notes["kondisi_lingkungan"] = (
            f"{n_risk} of 4 environmental risk factors (flood/SUTET/railway/industry) "
            "detected in the Step 4 pinpoint analysis"
        )
    else:
        notes["kondisi_lingkungan"] = (
            f"{n_risk} dari 4 faktor risiko lingkungan (banjir/SUTET/rel/industri) "
            "terdeteksi pada analisis pinpoint Step 4"
        )
    log.append(
        "✓ Road access & environmental condition derived from the Pinpoint Agent result" if _en
        else "✓ Akses jalan & kondisi lingkungan diturunkan dari hasil Pinpoint Agent"
    )
    auto_keys = {"legalitas", "akses_jalan", "kondisi_lingkungan"}

    # --- peruntukan_lahan: butuh pencarian web + LLM ---
    log.append(
        "Searching for land use / zoning (RTRW) data..." if _en
        else "Mencari data peruntukan lahan / RTRW / zonasi..."
    )
    query = f"peruntukan lahan zonasi RTRW {alamat} {kecamatan} {kabkota} {provinsi}"
    ok, res = serper.search(query, num=6)
    snippets = ""
    if ok:
        organic = res.get("organic", [])[:5]
        snippets = "\n".join(f"- {o.get('title','')}: {o.get('snippet','')}" for o in organic)
    else:
        log.append(
            f"⚠ Land use search failed: {res}" if _en
            else f"⚠ Pencarian peruntukan lahan gagal: {res}"
        )

    engine_name, llm = _pick_llm(groq, gemini)
    if llm and snippets:
        prompt_system = (
            "Anda asisten penilaian properti di Indonesia. Berdasarkan cuplikan pencarian "
            "web tentang zonasi/RTRW/peruntukan lahan untuk lokasi ini, nilai seberapa besar "
            "ketidaksesuaian peruntukan lahan properti (misal tanah untuk hunian yang berada "
            "di zona industri/komersial akan berisiko, sedangkan zona hunian/sesuai RTRW aman). "
            "Beri skor 0-3 (0 = sesuai peruntukan/tidak ada masalah, 3 = tidak sesuai/berisiko "
            "besar). Jika data pada cuplikan tidak cukup jelas untuk menyimpulkan, WAJIB gunakan "
            "skor 0 dan jelaskan di note bahwa data zonasi tidak ditemukan (JANGAN mengarang). "
            'Jawab HANYA JSON dengan struktur: {"skor": number, "note": str}'
        )
        if _en:
            prompt_system += (
                "\n\nWrite the \"note\" value in English, even though the search snippets "
                "you're given are in Indonesian."
            )
        prompt_user = f"Lokasi: {alamat}, {kecamatan}, {kabkota}, {provinsi}\n\nCuplikan pencarian:\n{snippets}"
        if engine_name == "groq":
            ok2, data = llm.chat(prompt_system, prompt_user, json_mode=True)
        else:
            ok2, data = llm.generate(prompt_system + "\n\n" + prompt_user, json_mode=True)

        if ok2 and isinstance(data, dict) and "skor" in data:
            try:
                skor = max(0, min(int(round(float(data.get("skor", 0)))), 3))
            except (TypeError, ValueError):
                skor = 0
            scores["peruntukan_lahan"] = skor
            notes["peruntukan_lahan"] = data.get("note", "")
            auto_keys.add("peruntukan_lahan")
            log.append("✓ Land use estimate complete" if _en else "✓ Estimasi peruntukan lahan selesai")
        else:
            if _en:
                log.append(f"⚠ Land use analysis failed: {data if not ok2 else 'unexpected format'} - please fill in manually.")
            else:
                log.append(f"⚠ Analisis peruntukan lahan gagal: {data if not ok2 else 'format tidak sesuai'} - isi manual.")
    else:
        log.append(
            "⚠ No search result/LLM key available for land use - please fill in manually." if _en
            else "⚠ Tidak ada hasil pencarian/LLM key untuk peruntukan lahan - isi manual."
        )

    if _en:
        log.append(
            f"Summary: {len(auto_keys)} of 10 checklist items estimated automatically "
            f"({', '.join(sorted(auto_keys))}). The other 6 physical-condition items still "
            "need a direct field assessment by the appraiser."
        )
    else:
        log.append(
            f"Ringkasan: {len(auto_keys)} dari 10 item checklist diperkirakan otomatis "
            f"({', '.join(sorted(auto_keys))}). 6 item kondisi fisik lainnya tetap perlu "
            "penilaian langsung oleh appraiser di lapangan."
        )
    return log, scores, notes, auto_keys



# Situs listing yang halamannya bisa di-fetch langsung untuk detail lebih akurat
# (harga/luas seringkali tidak lengkap di snippet Google saja).
FETCHABLE_DOMAINS = [
    "rumah123.com", "99.co", "olx.co.id", "pinhome.id",
    "lamudi.co.id", "raywhite.co.id", "era.id", "dotproperty.id",
]


def _is_fetchable(url: str) -> bool:
    return any(domain in url for domain in FETCHABLE_DOMAINS)


# Path/pola URL yang menandakan halaman KATEGORI atau HASIL PENCARIAN (banyak
# properti sekaligus), bukan satu listing spesifik - mis.
# "rumah123.com/jual/jakarta-timur/cipayung/rumah/" (bisa berisi 1.000+
# properti) atau "99.co/id/jual/rumah". Kalau halaman ini lolos ke tahap
# fetch+ekstraksi LLM, LLM akan "mengarang" satu harga/LT/LB dari halaman
# yang sebenarnya berisi banyak properti berbeda - jadi harus dibuang di
# awal, sebelum fetch/ekstraksi, bukan cuma diandalkan ke LLM untuk sortir.
_PROP_TYPE_WORDS = (r"rumah|tanah|apartemen|ruko|kios|villa|gudang|kantor|"
                    r"kondotel|hotel|pabrik|gedung|toko|komersial")
_CATEGORY_URL_REGEXES = [
    rf"/jual/[a-z0-9\-]+/[a-z0-9\-]+/({_PROP_TYPE_WORDS})/?(\?.*)?$",
    rf"/jual/[a-z0-9\-]+/({_PROP_TYPE_WORDS})/?(\?.*)?$",
    rf"/jual/({_PROP_TYPE_WORDS})/?(\?.*)?$",
    # Urutan sebaliknya: /jual/{tipe}/{lokasi}/ (mis. pola Pinhome). PENTING:
    # segmen terakhir HARUS tanpa angka ([a-z\-]+, bukan [a-z0-9\-]+) - nama
    # lokasi/kecamatan biasanya teks murni tanpa angka, sedangkan slug listing
    # individual HAMPIR SELALU menyertakan angka (ID/kode unik). Kalau memakai
    # [a-z0-9\-]+ di sini, pattern ini SALAH menangkap listing individual asli
    # yang kebetulan strukturnya "/jual/rumah/{slug-ada-angkanya}/" sebagai
    # kategori padahal itu satu listing spesifik - ini pernah jadi bug nyata
    # yang bikin jumlah pembanding valid anjlok drastis.
    rf"/jual/({_PROP_TYPE_WORDS})/[a-z\-]+/?(\?.*)?$",
    # Pola "/{lokasi}/{tipe}/for-sale/" atau "/{tipe}/for-sale/" (mis. Lamudi)
    r"/for-(sale|rent)/?(\?.*)?$",
    r"/jual/cari/?(\?.*)?$",
    r"/(search|cari)(/|\?|$)",
    r"[?&]location=",
    r"/(dijual|disewa)/?(\?.*)?$",
    r"/sale/house/?$",
    r"/en/(sale|rent)/[a-z\-]+/?$",
]
_CATEGORY_GENERIC_LAST_SEGMENTS = {
    "rumah", "tanah", "apartemen", "ruko", "kios", "villa", "gudang",
    "kantor", "kondotel", "hotel", "pabrik", "gedung", "toko", "jual",
    "sewa", "cari", "search", "properti", "property", "house", "houses",
    "dijual", "disewa", "komersial", "for-sale", "for-rent",
}


def _is_category_page(url: str) -> bool:
    """Heuristik untuk mendeteksi halaman kategori/listing-index (BUKAN satu
    listing properti spesifik). Semua individual listing di situs-situs ini
    (Rumah123, 99.co, OLX, Pinhome, Lamudi, RayWhite, ERA, DotProperty)
    selalu diakhiri kode/ID unik yang mengandung angka (mis. "hos40272095",
    "-iid12345678", "properti-abc-1234567"); halaman kategori/pencarian
    berakhir di kata kategori polos (tanpa angka) atau parameter pencarian
    generik seperti "?location=". Kalau tidak yakin (mis. domain di luar
    daftar situs listing di atas, atau tidak cocok pola manapun), anggap
    BUKAN kategori (biarkan lolos) - lebih aman melewatkan satu listing
    yang salah dianggap kategori, daripada membuang listing valid.
    """
    from urllib.parse import urlparse
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    path = parsed.path.rstrip("/")
    last_seg = path.rsplit("/", 1)[-1].lower() if path else ""
    if last_seg in _CATEGORY_GENERIC_LAST_SEGMENTS:
        return True
    for pat in _CATEGORY_URL_REGEXES:
        if re.search(pat, url, flags=re.IGNORECASE):
            return True
    return False


def _fetch_listing_page(url: str, timeout: int = 8) -> str:
    """
    Ambil teks polos dari halaman listing (harga/luas biasanya lebih lengkap
    di body halaman daripada snippet Google). Mengembalikan '' kalau gagal -
    kegagalan di sini TIDAK menghentikan proses, cuma mengurangi detail.

    PENTING: dulu fungsi ini cuma ambil SATU jendela ~1500 karakter di
    sekitar kata kunci PERTAMA yang ditemukan (mis. "rp " atau "luas") -
    kalau harga ada di box atas halaman sementara tabel spesifikasi Luas
    Tanah/Luas Bangunan ada jauh di bawah, salah satunya bisa ke-cut dari
    jendela itu dan akhirnya hilang saat LLM ekstraksi, meskipun datanya
    memang ada di halaman aslinya. Sekarang diambil beberapa jendela
    terpisah - satu di sekitar tiap kata kunci penting (luas tanah, luas
    bangunan, harga) - lalu digabung, supaya ketiganya punya peluang jauh
    lebih besar ikut terbawa ke teks yang dibaca LLM.
    """
    import re
    import requests

    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
            },
            timeout=timeout,
        )
        html = resp.text
        html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<[^>]+>", " ", html)
        html = re.sub(r"\s+", " ", html).strip()
        lower = html.lower()

        # Satu grup = beberapa variasi frasa untuk hal yang SAMA; ambil jendela
        # di sekitar kemunculan PERTAMA dari tiap grup (bukan cuma grup
        # pertama yang cocok), supaya harga & LT & LB semua kebagian jendela
        # masing-masing meski letaknya tersebar jauh di halaman.
        keyword_groups = [
            ["luas tanah", "lt:", "lt :", "tanah:", " lt "],
            ["luas bangunan", "lb:", "lb :", "bangunan:", " lb "],
            ["harga", "rp "],
            ["dijual", "kamar", "spesifikasi"],
        ]
        windows = []
        for group in keyword_groups:
            for kw in group:
                idx = lower.find(kw)
                if idx > 0:
                    windows.append((max(0, idx - 150), min(len(html), idx + 350)))
                    break  # satu jendela per grup sudah cukup

        if not windows:
            detail = html[:1500]
        else:
            windows.sort()
            merged = []
            for start, end in windows:
                if merged and start <= merged[-1][1] + 100:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            detail = " ... ".join(html[s:e] for s, e in merged)[:3000]

        # Cuplikan tambahan khusus tanggal upload/update listing (BUKAN tanggal
        # kita men-scrape halaman ini) - situs listing biasanya menampilkan ini
        # terpisah dari blok harga/luas, mis. "Diperbarui 3 hari lalu" atau
        # "Diposting pada 12 Mei 2025".
        date_kw = ["diperbarui", "diupdate", "diposting", "dipasang", "hari lalu",
                   "minggu lalu", "bulan lalu", "posted", "updated"]
        for kw in date_kw:
            idx = lower.find(kw)
            if idx > 0:
                snippet = html[max(0, idx - 40):idx + 60]
                if snippet not in detail:
                    detail += f" | TglListing: {snippet}"
                break
        return detail
    except Exception:
        return ""


def run_comparable_agent(alamat: str, kecamatan: str, kabkota: str, provinsi: str,
                          luas_tanah: float, luas_bangunan: float,
                          serper: SerperClient, groq: GroqClient, gemini: GeminiClient,
                          max_results: int = 15, exclude_links: set = None,
                          fetch_pages: bool = True, max_pages_to_fetch: int = None,
                          subjek_lat: float = None, subjek_lon: float = None,
                          radius_km: float = 5.0,
                          luas_tanah_toleransi_pct: float = 0.20,
                          luas_bangunan_toleransi_pct: float = 0.20,
                          geocode_comparables: bool = True,
                          search_page: int = 1,
                          lang: str = "id"):
    """
    Mencari properti pembanding dari beberapa situs listing lewat Serper,
    mem-fetch halaman listing yang bisa diakses langsung untuk detail harga/luas
    yang lebih lengkap, lalu memakai LLM untuk mengekstrak baris terstruktur
    (alamat, harga, LT, LB, tahun, tanggal upload listing, catatan). Mengembalikan
    (log, list_of_comparables).

    max_results default 15 (bisa dikustomisasi dari UI). exclude_links dipakai
    supaya tombol "Cari Lebih Banyak" di Step 6 tidak mengembalikan listing yang
    sudah ada di daftar.

    subjek_lat/subjek_lon: koordinat properti subjek (dari geocoding Step 1/2) -
    dipakai untuk menghitung jarak tiap pembanding (radius_km, default 5km, bisa
    dikustomisasi dari UI) supaya pembanding TERDEKAT yang diprioritaskan, bukan
    cuma yang mirip luasnya. luas_tanah_toleransi_pct dan
    luas_bangunan_toleransi_pct (default masing-masing 20%, bisa dikustomisasi
    TERPISAH satu sama lain) dipakai sebagai batas toleransi luas tanah dan
    luas bangunan pembanding vs subjek.
    geocode_comparables: kalau True, tiap alamat pembanding hasil ekstraksi LLM
    di-geocode (Nominatim, gratis) untuk mendapatkan lat/lon lalu jarak ke
    subjek - proses ini dibatasi ~1 request/detik sesuai kebijakan Nominatim,
    jadi bisa menambah beberapa detik per pembanding.

    max_pages_to_fetch: kalau None (default), dihitung OTOMATIS dari
    max_results (lihat di bawah) - PENTING karena ekstraksi harga+LT+LB
    LENGKAP nyaris selalu butuh detail halaman hasil fetch (snippet Google
    saja jarang memuat ketiganya sekaligus), jadi kalau max_pages_to_fetch
    tidak ikut naik saat appraiser menaikkan "Jumlah pembanding yang dicari",
    hasil akhir akan mentok jauh di bawah angka yang diminta (mis. minta 15,
    yang benar-benar lengkap cuma 4) karena cuma segelintir listing yang
    sempat di-fetch detailnya sama sekali.

    search_page: diteruskan ke SerperClient.search() sebagai nomor halaman
    Google. WAJIB dinaikkan (2, 3, 4, ...) tiap kali fungsi ini dipanggil
    ULANG dengan exclude_links yang sudah berisi hasil ronde sebelumnya (lihat
    search_comparables_until_target) - kalau tetap 1, query yang identik akan
    mengembalikan hasil yang nyaris sama dengan ronde sebelumnya, sehingga
    kelihatannya seperti "listing sudah habis" padahal cuma belum pernah
    benar-benar mencari lebih dalam dari halaman pertama.
    """
    log = []

    def t(id_text, en_text):
        return _t(lang, id_text, en_text)

    exclude_links = exclude_links or set()
    if max_pages_to_fetch is None:
        # ~2x max_results sebagai buffer (tidak semua fetch berhasil / tidak
        # semua listing yang di-fetch punya harga+LT+LB lengkap), dengan batas
        # bawah 12 (perilaku lama) dan batas atas 45 supaya waktu proses &
        # jumlah request tetap wajar untuk permintaan yang sangat besar (mis. 50).
        max_pages_to_fetch = max(12, min(max_results * 2, 45))
    lb_min = luas_bangunan * (1 - luas_bangunan_toleransi_pct)
    lb_max = luas_bangunan * (1 + luas_bangunan_toleransi_pct)
    lt_min = luas_tanah * (1 - luas_tanah_toleransi_pct)
    lt_max = luas_tanah * (1 + luas_tanah_toleransi_pct)
    location_str = f"{kecamatan} {kabkota} {provinsi}"

    sites = {
        "Rumah123": "site:rumah123.com",
        "99.co": "site:99.co",
        "OLX": "site:olx.co.id",
        "Pinhome": "site:pinhome.id",
        "Lamudi": "site:lamudi.co.id",
        "RayWhite": "site:raywhite.co.id",
        "ERA": "site:era.id",
        "DotProperty": "site:dotproperty.id",
        "Google": "",
    }
    # PENTING: query per-situs dulu HANYA menyertakan angka LUAS BANGUNAN
    # subjek persis (mis. "60m2") - listing riil jarang punya ukuran PERSIS
    # sama, jadi banyak hasil yang muncul ternyata jauh di luar toleransi
    # LT/LB yang diminta. Sekarang tiap query situs menyertakan LT & LB
    # SEKALIGUS (bukan cuma LB) supaya lebih terarah ke ukuran yang diminta,
    # ditambah beberapa query "anchor" tambahan (di titik bawah/tengah/atas
    # rentang toleransi, bukan cuma titik tengah persis) lewat pencarian
    # Google umum supaya listing yang ukurannya mendekati BATAS toleransi
    # (bukan cuma pas di tengah) juga py peluang ketemu secara literal di
    # teks listing - tanpa melipatgandakan jumlah panggilan Serper per situs.
    queries = [
        (name, f"jual rumah tanah {location_str} luas tanah {int(luas_tanah)}m2 "
               f"luas bangunan {int(luas_bangunan)}m2 {site_filter}".strip())
        for name, site_filter in sites.items()
    ]
    queries.append(("Google", f"rumah dijual {location_str} luas {int(lb_min)}-{int(lb_max)}m2 harga"))
    queries.append(("Google", f"properti dijual {location_str} tanah {int(lt_min)}-{int(lt_max)}m2"))
    if lb_max > lb_min:
        queries.append(("Google", f"rumah dijual {location_str} luas bangunan {int(lb_min)}m2"))
        queries.append(("Google", f"rumah dijual {location_str} luas bangunan {int(lb_max)}m2"))
    if lt_max > lt_min:
        queries.append(("Google", f"tanah rumah dijual {location_str} luas tanah {int(lt_min)}m2"))
        queries.append(("Google", f"tanah rumah dijual {location_str} luas tanah {int(lt_max)}m2"))


    all_organic = []
    # PENTING (kecepatan): dulu 14 query (8 situs + ~6 query Google umum)
    # dipanggil SATU PER SATU secara berurutan ke Serper - kalau tiap
    # panggilan makan ~1-2 detik, itu 15-30 detik cuma untuk tahap pencarian
    # saja, SEBELUM fetch halaman & ekstraksi LLM. Serper adalah API
    # berbayar milik appraiser sendiri (bukan layanan gratis dengan rate
    # limit ketat per-IP seperti Nominatim), jadi aman & jauh lebih cepat
    # dijalankan PARALEL - SerperClient.search() sendiri sudah py retry/
    # backoff internal untuk 429/5xx per panggilan, jadi paralelisasi di
    # sini tidak mengurangi keandalan tsb.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(queries))) as ex:
        future_to_name = {
            ex.submit(serper.search, query, 8, "id", "id", 2, search_page): name
            for name, query in queries
        }
        for fut in concurrent.futures.as_completed(future_to_name):
            name = future_to_name[fut]
            log.append(f"Searching {name}...")
            try:
                ok, res = fut.result()
            except Exception as e:
                ok, res = False, str(e)
            if ok:
                for o in res.get("organic", []):
                    o["_source_site"] = name
                    all_organic.append(o)
            else:
                log.append(t(f"⚠ {name} gagal: {res}", f"⚠ {name} failed: {res}"))

    log.append("Removing duplicates...")
    seen_links = set(exclude_links)
    deduped = []
    for o in all_organic:
        link = o.get("link")
        if link and link not in seen_links:
            seen_links.add(link)
            deduped.append(o)

    if not deduped:
        log.append(t("⚠ Tidak ada listing baru ditemukan.", "⚠ No new listings found."))
        return log, []

    log.append(t("Membuang halaman kategori/hasil pencarian (bukan listing spesifik)...",
                  "Discarding category/search-result pages (not specific listings)..."))
    n_before_cat_filter = len(deduped)
    deduped = [o for o in deduped if not _is_category_page(o.get("link", ""))]
    n_filtered_out = n_before_cat_filter - len(deduped)
    if n_filtered_out:
        log.append(t(
            f"✓ {n_filtered_out} halaman kategori/listing-index dibuang "
            f"(mis. '.../jual/{{kota}}/{{kecamatan}}/rumah/' yang berisi ratusan/ribuan "
            "properti sekaligus, bukan satu properti spesifik).",
            f"✓ {n_filtered_out} category/listing-index pages discarded "
            f"(e.g. '.../jual/{{city}}/{{district}}/rumah/' pages that list hundreds/thousands "
            "of properties at once, not a single specific property)."))
    if not deduped:
        log.append(t("⚠ Semua hasil ternyata halaman kategori, tidak ada listing spesifik yang tersisa.",
                      "⚠ All results turned out to be category pages, no specific listings remain."))
        return log, []

    if fetch_pages:
        log.append(t(f"Mengambil detail halaman untuk hingga {max_pages_to_fetch} listing teratas...",
                      f"Fetching page details for up to {max_pages_to_fetch} top listings..."))
        # PENTING (kecepatan): dulu tiap halaman di-fetch SATU PER SATU secara
        # berurutan (_fetch_listing_page, timeout 8 detik/halaman) - dengan
        # max_pages_to_fetch bisa sampai 45 (lihat komentar di atas), ini
        # bisa berarti MENIT hanya untuk tahap fetch kalau banyak listing
        # lambat/timeout, sebelum ekstraksi LLM sama sekali dimulai. Tiap
        # fetch adalah request independen ke domain LISTING (bukan API
        # dengan rate limit ketat seperti Nominatim), jadi aman dan jauh
        # lebih cepat dilakukan PARALEL dengan beberapa worker sekaligus -
        # satu listing yang lambat/timeout tidak lagi memblokir listing lain.
        fetchable = [o for o in deduped if _is_fetchable(o.get("link", ""))][:max_pages_to_fetch]
        n_fetched = 0
        if fetchable:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(fetchable))) as ex:
                future_to_o = {ex.submit(_fetch_listing_page, o.get("link", "")): o for o in fetchable}
                for fut in concurrent.futures.as_completed(future_to_o):
                    o = future_to_o[fut]
                    try:
                        page_text = fut.result()
                    except Exception:
                        page_text = None
                    if page_text:
                        o["_page_detail"] = page_text
                        n_fetched += 1
        log.append(t(f"✓ {n_fetched} halaman berhasil diambil detailnya",
                      f"✓ {n_fetched} pages successfully fetched in detail"))

    engine_name, llm = _pick_llm(groq, gemini)
    if not llm:
        log.append(t("⚠ Tidak ada API key LLM untuk ekstraksi pembanding.",
                      "⚠ No LLM API key available for comparable extraction."))
        return log, []

    log.append("Ranking comparables...")
    # PENTING - riwayat bug di sini (baca sebelum ubah):
    # 1) Awalnya _page_detail (bisa 3000 char/listing) dimasukkan APA ADANYA
    #    untuk sampai 35 listing dalam SATU prompt -> gampang 60-100 ribu
    #    karakter -> Groq balas "413 Payload Too Large" dan EKSTRAKSI GAGAL
    #    TOTAL (bukan cuma sebagian) meski API key valid & fetch sukses.
    # 2) Fix pertama: cap per-entry (900 char detail, 1400 char/entry) + cap
    #    total 18000 char, buang entry dari belakang kalau kelebihan. Ini
    #    mencegah SATU entry raksasa menghabiskan seluruh daftar (bug
    #    terpisah), TAPI ternyata 18000 karakter (+ system prompt + instruksi)
    #    ATAU jumlah gambar/karakter kumulatif MASIH bisa memicu 413 dari Groq
    #    di beberapa kasus nyata (lihat log appraiser: 25 halaman berhasil
    #    di-fetch tapi ekstraksi tetap gagal 413, seluruh ronde itu jadi 0
    #    hasil walau fetch-nya sukses). Selain itu, membuang entry dari
    #    belakang berarti listing dengan prioritas rendah TIDAK PERNAH
    #    dievaluasi LLM sama sekali walaupun datanya mungkin lengkap.
    # 3) Fix final (di bawah): jangan kirim SATU request besar sama sekali.
    #    Pecah semua entry jadi BATCH KECIL (~5000 karakter/batch), panggil
    #    LLM terpisah per batch, gabungkan hasilnya. Ini membuat 413 SECARA
    #    STRUKTURAL tidak mungkin lagi (tiap batch jauh di bawah limit), DAN
    #    setiap listing yang berhasil di-fetch benar-benar dievaluasi LLM -
    #    bukan cuma sebagian yang kebetulan muat di satu prompt.
    MAX_DETAIL_CHARS_PER_ENTRY = 900
    MAX_TITLE_SNIPPET_CHARS = 300
    MAX_ENTRY_CHARS = 1400  # hard cap PER ENTRY (title+snippet+link+detail gabungan)
    CHUNK_CHAR_BUDGET = 5000  # target ukuran tiap batch dikirim ke LLM
    MAX_CHUNKS = 8  # batas jumlah panggilan LLM per ronde, supaya biaya/waktu terkontrol

    # Sama seperti max_pages_to_fetch di atas - jumlah kandidat yang dipakai
    # untuk prompt LLM juga perlu ikut naik kalau max_results besar, supaya
    # ada cukup "bahan baku" untuk LLM pilih dari, bukan mentok di 35 kandidat
    # tetap padahal appraiser minta 40-50 pembanding.
    candidates = deduped[: max(35, max_results * 3)]
    # Prioritaskan listing yang sudah berhasil di-fetch detail halamannya -
    # itu yang paling mungkin punya harga+LT+LB lengkap untuk diekstrak.
    candidates.sort(key=lambda o: 0 if o.get("_page_detail") else 1)

    entries = []
    for o in candidates:
        title = (o.get("title") or "")[:MAX_TITLE_SNIPPET_CHARS]
        snippet = (o.get("snippet") or "")[:MAX_TITLE_SNIPPET_CHARS]
        entry = f"[{o.get('_source_site')}] {title} | {snippet} | {o.get('link','')}"
        detail = o.get("_page_detail")
        if detail:
            entry += f" | Detail halaman: {detail[:MAX_DETAIL_CHARS_PER_ENTRY]}"
        entries.append(entry[:MAX_ENTRY_CHARS])

    # Bagi entries jadi batch-batch kecil (greedy fill sampai CHUNK_CHAR_BUDGET),
    # bukan satu prompt raksasa. Ini yang menggantikan pendekatan lama yang
    # membuang entry dari belakang kalau kelebihan - sekarang SEMUA entry
    # (sampai MAX_CHUNKS batch) tetap dikirim & dievaluasi, cuma dipecah jadi
    # beberapa request terpisah.
    chunks = []
    cur_chunk = []
    cur_len = 0
    for e in entries:
        if cur_chunk and cur_len + len(e) > CHUNK_CHAR_BUDGET:
            chunks.append(cur_chunk)
            cur_chunk = []
            cur_len = 0
        cur_chunk.append(e)
        cur_len += len(e)
    if cur_chunk:
        chunks.append(cur_chunk)

    n_skipped_chunks_entries = 0
    if len(chunks) > MAX_CHUNKS:
        for skipped in chunks[MAX_CHUNKS:]:
            n_skipped_chunks_entries += len(skipped)
        chunks = chunks[:MAX_CHUNKS]

    log.append(t(
        f"Mengekstrak {len(entries)} listing lewat {len(chunks)} batch LLM terpisah "
        f"(supaya tidak kena limit ukuran request)...",
        f"Extracting {len(entries)} listings via {len(chunks)} separate LLM batches "
        f"(to avoid hitting the request size limit)..."))
    if n_skipped_chunks_entries:
        log.append(t(
            f"⚠ {n_skipped_chunks_entries} listing prioritas terendah dilewati "
            f"(batas {MAX_CHUNKS} batch/ronde tercapai).",
            f"⚠ {n_skipped_chunks_entries} lowest-priority listings skipped "
            f"(reached the {MAX_CHUNKS} batches/round limit)."))

    prompt_system = (
        "Anda adalah analis properti Indonesia. Dari daftar hasil pencarian & detail "
        "halaman listing properti berikut, ekstrak SEBANYAK MUNGKIN properti pembanding "
        "yang relevan sebagai perumahan/tanah dijual di lokasi yang "
        "diminta. JANGAN memakai halaman KATEGORI/HASIL PENCARIAN (halaman yang menampilkan "
        "banyak properti sekaligus, biasanya ditandai teks seperti 'Menampilkan X properti', "
        "'X Unit Berkualitas', 'Ada X Hasil', atau daftar ringkasan berulang tanpa satu harga/LT/LB "
        "yang jelas untuk SATU properti spesifik) sebagai satu pembanding - kalau sebuah listing "
        "di teks terlihat seperti ini, LEWATI sepenuhnya. Prioritaskan urutan: (1) luas tanah DAN "
        "luas bangunan SAMA-SAMA berada di dalam rentang toleransi yang disebutkan di bawah; (2) "
        "kecamatan yang sama; (3) kabupaten/kota yang sama; (4) provinsi yang sama. Listing yang "
        "ukurannya JAUH di luar rentang toleransi boleh tetap disertakan - jangan pernah membuang "
        "listing yang SESUAI rentang demi listing yang TIDAK sesuai rentang. Untuk tiap listing, "
        "ekstrak field dari teks - baca SELURUH teks listing (termasuk 'Detail halaman' "
        "kalau ada) karena harga, luas tanah, dan luas bangunan seringkali muncul di "
        "bagian berbeda dari teks yang sama, bukan cuma berdekatan. WAJIB: harga, "
        "luas_tanah, DAN luas_bangunan harus ke-3nya terisi dengan angka nyata dari teks "
        "supaya listing itu bisa dipakai sebagai pembanding - JANGAN mengarang salah "
        "satu dari ketiganya; kalau SALAH SATU SAJA dari ketiga field itu benar-benar "
        "tidak disebutkan di teks manapun untuk listing tsb, LEWATI (jangan sertakan) "
        "listing tersebut sepenuhnya daripada mengisi 0/null untuk field yang hilang. "
        "PENTING SOAL FORMAT HARGA: situs properti Indonesia sering menyingkat harga, mis. "
        "\"Rp 1,4 M\", \"Rp 1.4 Miliar\", \"Rp 1,4 milyar\" berarti 1.400.000.000 (1,4 miliar "
        "rupiah, BUKAN 1.400.000) - dan \"Rp 850 jt\", \"Rp 850 Juta\" berarti 850.000.000 "
        "(BUKAN 850.000). Selalu konversi ke angka RUPIAH PENUH (kalikan M/Miliar/Milyar "
        "dengan 1.000.000.000, dan jt/Juta dengan 1.000.000) - JANGAN pernah mengeluarkan "
        "angka mentah tanpa mengalikan sesuai satuannya. Sebagai sanity check: harga rumah "
        "wajar di Indonesia hampir selalu di atas Rp 50.000.000 (bukan rumah petak sangat "
        "kecil) - kalau hasil akhir angka harga di bawah itu, kemungkinan besar satuan "
        "M/jt-nya terlewat saat konversi, periksa ulang sebelum menjawab. "
        "Jawab HANYA JSON: "
        '{{"comparables": [{{"alamat": str, "harga": number, "luas_tanah": number, '
        '"luas_bangunan": number, "tahun_bangun": number|null, "tanggal_upload": str|null, '
        '"sumber": str, "link": str, "catatan": str}}]}} '
        "tanggal_upload: tanggal listing itu SENDIRI diunggah/diperbarui oleh pemasang iklan "
        "(mis. \"12 Mei 2025\", \"3 hari lalu\", \"Diperbarui 2 minggu lalu\") - ambil HANYA "
        "kalau memang disebut eksplisit di teks/detail halaman (biasanya di dekat penanda "
        "'TglListing:', 'diperbarui', 'diposting', 'hari lalu'). JANGAN mengisi tanggal hari "
        "ini atau tanggal pencarian sebagai pengganti - kalau tidak disebutkan, isi null. "
        "catatan: keterangan singkat (maks 80 karakter) jika ada hal relevan (mis. ukuran "
        "jauh dari toleransi, harga negotiable, dll), boleh string kosong."
    )

    def _run_chunk(chunk_idx, chunk):
        raw_text = "\n".join(chunk)
        prompt_user = (
            f"Lokasi subjek: {location_str}\n"
            f"LT subjek: {luas_tanah} m2 (toleransi {lt_min:.0f}-{lt_max:.0f} m2)\n"
            f"LB subjek: {luas_bangunan} m2 (toleransi {lb_min:.0f}-{lb_max:.0f} m2)\n\n"
            f"Listing:\n{raw_text}"
        )
        if engine_name == "groq":
            ok, data = llm.chat(prompt_system, prompt_user, json_mode=True)
        else:
            ok, data = llm.generate(prompt_system + "\n\n" + prompt_user, json_mode=True)
        return chunk_idx, ok, data

    comps_raw_all = []
    n_chunk_failures = 0
    # PENTING (kecepatan): dulu tiap batch dikirim ke LLM SATU PER SATU
    # berurutan (sampai 8 batch/ronde) - kalau tiap panggilan LLM makan
    # beberapa detik, itu bisa puluhan detik hanya untuk tahap ekstraksi.
    # Dijalankan paralel dengan concurrency terbatas (3) supaya tetap lebih
    # cepat TANPA membombardir API key Groq/Gemini appraiser dengan terlalu
    # banyak request sekaligus (yang berisiko malah memicu lebih banyak 429) -
    # retry/backoff internal per panggilan di GroqClient/GeminiClient tetap
    # berlaku seperti sebelumnya, jadi keandalan per-batch tidak berkurang.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(chunks))) as ex:
        futures = [ex.submit(_run_chunk, idx, chunk) for idx, chunk in enumerate(chunks, start=1)]
        results = []
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())
    # Urutkan kembali by chunk_idx supaya urutan log batch tetap konsisten
    # (mudah dibandingkan appraiser antar-run), walau eksekusinya paralel.
    for chunk_idx, ok, data in sorted(results, key=lambda r: r[0]):
        if not ok or "comparables" not in data:
            # PENTING: batch lain TETAP lanjut walau satu batch gagal (mis.
            # timeout/rate-limit sesaat) - beda dari perilaku lama di mana
            # satu kegagalan LLM menggagalkan SELURUH ronde termasuk listing
            # dari batch lain yang sebenarnya baik-baik saja.
            n_chunk_failures += 1
            log.append(t(f"⚠ Batch {chunk_idx}/{len(chunks)} gagal diekstrak: {data}",
                          f"⚠ Batch {chunk_idx}/{len(chunks)} failed to extract: {data}"))
            continue
        comps_raw_all.extend(data.get("comparables", []))

    comps_raw = comps_raw_all
    if n_chunk_failures:
        log.append(t(
            f"⚠ {n_chunk_failures}/{len(chunks)} batch gagal diekstrak (lihat detail di atas) - "
            "batch lainnya tetap diproses normal.",
            f"⚠ {n_chunk_failures}/{len(chunks)} batches failed to extract (see details above) - "
            "the other batches were still processed normally."))

    comps = []
    n_incomplete = 0
    seen_extract_links = set()
    for c in comps_raw:
        # Filter keras: pembanding TANPA harga, luas_tanah, ATAU luas_bangunan
        # tidak bisa dipakai membandingkan (itu 3 angka utama yang dipakai di
        # seluruh Step 6-7) - daripada tampil membingungkan dengan salah satu
        # kosong/0 padahal datanya sebetulnya ada di halaman aslinya, listing
        # begini dibuang di sini saja dan appraiser bisa cek manual / tambah
        # lewat form manual kalau memang mau dipakai.
        if not (c.get("harga") and c.get("luas_tanah") and c.get("luas_bangunan")):
            n_incomplete += 1
            continue
        # Dengan ekstraksi per-batch, listing yang sama secara teori bisa
        # muncul di >1 batch (mis. duplikat link yang lolos dedup awal
        # karena URL sedikit berbeda) - dedup lagi di sini by link.
        link = c.get("link")
        if link and link in seen_extract_links:
            continue
        if link:
            seen_extract_links.add(link)
        # Jaring pengaman kode (jangan cuma andalkan LLM patuh instruksi
        # format harga di atas): tandai (BUKAN buang - appraiser tetap perlu
        # lihat & putuskan sendiri) listing dengan harga yang sangat tidak
        # masuk akal untuk sebuah rumah/tanah di Indonesia (mis. Rp 1.4 juta
        # untuk rumah 70m2/85m2) - ini pertanda kuat kesalahan konversi
        # satuan "M"/"Miliar"/"jt"/"Juta" saat ekstraksi (mis. "Rp 1,4 M"
        # -> harusnya 1.400.000.000, bukan 1.400.000, selisih persis 1000x).
        harga = c.get("harga") or 0
        if 0 < harga < 20_000_000:
            c["catatan"] = ((c.get("catatan") or "") +
                             " ⚠ Harga tampak tidak wajar (kemungkinan salah "
                             "konversi satuan M/Miliar/jt saat ekstraksi otomatis - "
                             "cek listing asli sebelum dipakai).").strip()
            c["harga_mencurigakan"] = True
        comps.append(c)

    # PENTING: mengandalkan LLM SAJA untuk memprioritaskan listing yang sesuai
    # rentang toleransi LT/LB tidak selalu konsisten - jadi di sini dipastikan
    # lagi lewat kode: urutkan supaya listing yang LT & LB-nya SAMA-SAMA masuk
    # rentang toleransi selalu didahulukan sebelum dipotong ke max_results.
    # Listing di luar rentang TETAP disertakan (tidak dibuang) untuk mengisi
    # sisa slot / tetap ditampilkan ke appraiser untuk ditinjau, sesuai
    # permintaan - hanya urutannya yang diubah supaya yang akurat lebih dulu.
    def _dalam_rentang(c):
        lt = c.get("luas_tanah") or 0
        lb = c.get("luas_bangunan") or 0
        return (lt_min <= lt <= lt_max) and (lb_min <= lb <= lb_max)

    comps.sort(key=lambda c: 0 if _dalam_rentang(c) else 1)
    n_dalam_rentang = sum(1 for c in comps if _dalam_rentang(c))
    comps = comps[:max_results]
    n_dalam_rentang_final = sum(1 for c in comps if _dalam_rentang(c))
    log.append(t(f"✓ {len(comps)} properti pembanding ditemukan (harga+LT+LB lengkap)",
                  f"✓ {len(comps)} comparable properties found (complete price+land+building area)"))
    log.append(t(
        f"✓ {n_dalam_rentang_final}/{len(comps)} di antaranya sudah sesuai rentang toleransi "
        f"LT {lt_min:.0f}-{lt_max:.0f} m2 & LB {lb_min:.0f}-{lb_max:.0f} m2 "
        f"(dari total {n_dalam_rentang} yang ditemukan sebelum dipotong ke {max_results}) "
        "- yang sesuai rentang selalu diprioritaskan lebih dulu.",
        f"✓ {n_dalam_rentang_final}/{len(comps)} of these already fit the tolerance range "
        f"land area {lt_min:.0f}-{lt_max:.0f} m2 & building area {lb_min:.0f}-{lb_max:.0f} m2 "
        f"(out of {n_dalam_rentang} found before trimming to {max_results}) "
        "- in-range comparables are always prioritized first."))
    if n_incomplete:
        log.append(t(
            f"⚠ {n_incomplete} listing dilewati karena harga/LT/LB tidak lengkap "
            f"di hasil ekstraksi otomatis - cek manual/tambahkan lewat form kalau perlu.",
            f"⚠ {n_incomplete} listings skipped because price/land/building area was incomplete "
            f"in the automatic extraction - check manually or add via the form if needed."))

    subjek = {"luas_tanah": luas_tanah, "luas_bangunan": luas_bangunan}

    if geocode_comparables and comps:
        log.append(t(f"Menghitung jarak pembanding (radius maksimum {radius_km:g} km)...",
                      f"Calculating comparable distances (maximum radius {radius_km:g} km)..."))
        for i, c in enumerate(comps):
            alamat_asli = (c.get("alamat") or "").strip()
            ok_geo, geo = (False, None)
            if alamat_asli:
                # Percobaan 1: alamat pembanding + konteks kecamatan/kabkota/
                # provinsi SUBJEK. Ini akurat SELAMA pembanding memang ada di
                # area yang sama dengan subjek.
                ok_geo, geo = geocode_address(f"{alamat_asli}, {kecamatan}, {kabkota}, {provinsi}")
                # PENTING - BUG YANG DIPERBAIKI: fallback SEBELUMNYA di sini
                # mengganti query jadi HANYA "{kecamatan}, {kabkota}, {provinsi}"
                # (konteks SUBJEK saja, alamat pembanding yang asli dibuang).
                # Ini choose menyesatkan: kalau alamat pembanding sebenarnya di
                # kota LAIN (mis. "..., Tangerang, Banten") sehingga query
                # gabungan #1 di atas gagal (kontradiktif - Nominatim tidak
                # bisa mencocokkan alamat Tangerang dengan konteks kab/kota
                # subjek), fallback lama itu MEMAKSA titik pembanding
                # ditempatkan tepat di sekitar kecamatan SUBJEK - membuat
                # jarak yang dihitung jadi kecil/salah (mis. "4.23 km")
                # padahal properti itu SEBENARNYA jauh (di Tangerang/Banten,
                # bukan di area subjek sama sekali) dan seharusnya malah
                # disingkirkan oleh filter radius, bukan lolos dengan jarak
                # palsu. Perbaikan: fallback sekarang geocode alamat
                # pembanding APA ADANYA (tanpa konteks subjek yang
                # dipaksakan) supaya titik yang ditemukan adalah lokasi ASLI
                # listing tsb (betul di Tangerang kalau memang di sana),
                # sehingga jarak yang dihitung akurat dan filter radius bisa
                # menyaring pembanding yang lokasinya jauh/beda kota dengan
                # semestinya.
                if not ok_geo:
                    ok_geo, geo = geocode_address(alamat_asli)
            if ok_geo and geo:
                c["lat"], c["lon"] = geo["lat"], geo["lon"]
                if subjek_lat is not None and subjek_lon is not None:
                    c["distance_km"] = round(
                        calc.haversine_km(subjek_lat, subjek_lon, geo["lat"], geo["lon"]), 2
                    )
                else:
                    c["distance_km"] = None
                # Cek sanity tambahan: kalau nama provinsi SUBJEK sama sekali
                # tidak muncul di display_name hasil geocode (mis. provinsi
                # subjek "DKI Jakarta" tapi hasil geocode "..., Tangerang,
                # Banten, Indonesia"), tandai supaya appraiser tahu titik
                # yang ditemukan kemungkinan bukan properti yang dimaksud -
                # walau jarak yang dihitung kebetulan kecil/masuk radius.
                _provinsi_hasil = (geo.get("display_name") or "")
                c["lokasi_hasil_geocode"] = _provinsi_hasil
                c["lokasi_provinsi_cocok"] = (
                    not provinsi or not _provinsi_hasil or provinsi.strip().lower() in _provinsi_hasil.lower()
                )
            else:
                c["lat"], c["lon"], c["distance_km"] = None, None, None
            # CATATAN: dulu ada time.sleep(1.05) manual tambahan di sini untuk
            # mematuhi batas ~1 request/detik Nominatim - sekarang REDUNDAN
            # dan dihapus karena geocode.py sendiri sudah menerapkan throttle
            # GLOBAL (lihat _throttle() di geocode.py) sebelum SETIAP request
            # ke Nominatim, termasuk yang dipanggil dari sini lewat
            # geocode_address(). Sleep tambahan di sini dulu membuat jeda
            # antar-pembanding jadi dobel (throttle internal + sleep manual),
            # jadi menghapusnya mempercepat pencarian tanpa melanggar
            # kebijakan rate-limit Nominatim.
        n_with_dist = sum(1 for c in comps if c.get("distance_km") is not None)
        log.append(t(f"✓ {n_with_dist}/{len(comps)} pembanding berhasil dihitung jaraknya",
                      f"✓ {n_with_dist}/{len(comps)} comparables had their distance calculated successfully"))

    if subjek_lat is not None and subjek_lon is not None:
        for c in comps:
            kriteria = calc.memenuhi_kriteria_pembanding(
                subjek, c, radius_km=radius_km,
                luas_tanah_toleransi_pct=luas_tanah_toleransi_pct,
                luas_bangunan_toleransi_pct=luas_bangunan_toleransi_pct,
            )
            c["luas_ok"] = kriteria["luas_ok"]
            c["jarak_ok"] = kriteria["jarak_ok"]
            c["memenuhi_kriteria"] = kriteria["ok"]
        n_ok = sum(1 for c in comps if c.get("memenuhi_kriteria"))
        log.append(t(
            f"✓ {n_ok}/{len(comps)} pembanding memenuhi kriteria "
            f"(luas tanah ±{luas_tanah_toleransi_pct*100:.0f}%, "
            f"luas bangunan ±{luas_bangunan_toleransi_pct*100:.0f}%, "
            f"radius {radius_km:g} km) - sisanya tetap ditampilkan tapi ditandai di luar kriteria.",
            f"✓ {n_ok}/{len(comps)} comparables meet the criteria "
            f"(land area ±{luas_tanah_toleransi_pct*100:.0f}%, "
            f"building area ±{luas_bangunan_toleransi_pct*100:.0f}%, "
            f"radius {radius_km:g} km) - the rest are still shown but flagged as out of criteria."
        ))

    return log, comps


def search_comparables_until_target(
    alamat, kecamatan, kabkota, provinsi, luas_tanah, luas_bangunan,
    serper, groq, gemini, target: int, subjek_lat=None, subjek_lon=None,
    radius_km: float = 5.0, luas_tanah_toleransi_pct: float = 0.20,
    luas_bangunan_toleransi_pct: float = 0.20, max_rounds: int = 10,
    progress_cb=None, exclude_links=None, lang: str = "id",
):
    """
    Bungkus run_comparable_agent supaya PENCARIAN OTOMATIS DIULANG beberapa
    kali sampai jumlah pembanding yang ditemukan mencapai `target`, bukan
    cuma sekali jalan lalu berhenti walau appraiser minta angka yang lebih
    besar. Sebelumnya, appraiser yang minta 15 pembanding tapi hasil pass
    pertama cuma 2 harus MANUAL klik "Cari Lebih Banyak" berkali-kali sendiri
    - sekarang app melakukan itu secara otomatis.

    Tiap ronde: exclude_links diisi dari semua link yang sudah ketemu supaya
    tidak muncul listing duplikat, max_results per ronde dihitung dari SISA
    kebutuhan (target - jumlah yang sudah ada), dan search_page dinaikkan
    per ronde (1, 2, 3, ...) supaya Serper benar-benar mencari HALAMAN
    BERBEDA dari Google, bukan query yang identik berulang kali (yang cuma
    akan mengembalikan hasil yang nyaris sama seperti ronde sebelumnya - itu
    penyebab utama kenapa versi awal fungsi ini mentok di ~11/15 walau
    sebenarnya masih ada listing lain yang belum pernah benar-benar dicari).

    Berhenti kalau salah satu dari ini terjadi duluan:
    - jumlah pembanding sudah mencapai target, ATAU
    - sudah mencoba `max_rounds` kali (naik jadi 10 dari 4 sebelumnya, karena
      sekarang tiap ronde benar-benar mencari halaman baru sehingga usaha
      tambahan lebih mungkin membuahkan hasil, bukan cuma mengulang sia-sia), ATAU
    - 2 ronde BERTURUT-TURUT sama sekali tidak menambah pembanding baru
      (tanda kuat listing yang tersedia di pasar/radius/toleransi ini memang
      sudah habis - meneruskan pencarian cuma buang-buang waktu & kuota API
      tanpa hasil tambahan). Ini sekarang jadi sinyal yang JAUH lebih bisa
      dipercaya daripada sebelumnya, karena tiap ronde memang mencari halaman
      Google yang berbeda - kalau 2 ronde berturut ke halaman BERBEDA tetap
      nihil, itu pertanda kuat listingnya memang habis, bukan artefak query
      yang diulang-ulang.

    progress_cb (opsional): dipanggil dengan (round_no, comps_so_far, target)
    di awal tiap ronde, supaya UI bisa menampilkan status real-time
    ("Ronde 2/10 - sudah 4/15 ditemukan, mencari lagi...").

    exclude_links (opsional): link yang SUDAH ada di daftar pembanding
    appraiser SEBELUM pemanggilan ini (mis. dari pencarian awal, saat
    fungsi ini dipanggil ulang lewat tombol "Cari Lebih Banyak"). Wajib
    di-seed ke seen_links dari awal ronde 1, bukan cuma dipakai untuk
    filter di akhir - kalau tidak, listing yang sudah ada bisa
    ketemu-lagi di tengah pencarian, dihitung sebagai "progress" menuju
    target, bikin loop berhenti terlalu cepat, padahal setelah caller
    membuang duplikatnya sendiri di akhir jumlah pembanding BARU yang
    tersisa jauh di bawah target yang diminta.

    Mengembalikan (full_log, comps) - full_log gabungan semua ronde dengan
    penanda "=== Ronde N ===" di antaranya, comps adalah gabungan unik
    (dedup by link) dari semua ronde, TIDAK termasuk exclude_links.
    """
    all_comps = []
    seen_links = set(exclude_links) if exclude_links else set()
    full_log = []
    consecutive_empty_rounds = 0

    def t(id_text, en_text):
        return _t(lang, id_text, en_text)

    for round_no in range(1, max_rounds + 1):
        remaining = target - len(all_comps)
        if remaining <= 0:
            break
        if progress_cb:
            progress_cb(round_no, len(all_comps), target)
        full_log.append(t(
            f"=== Ronde {round_no}/{max_rounds} "
            f"(sudah {len(all_comps)}/{target}, mencari {remaining} lagi, "
            f"halaman Google #{round_no}) ===",
            f"=== Round {round_no}/{max_rounds} "
            f"({len(all_comps)}/{target} found so far, searching {remaining} more, "
            f"Google page #{round_no}) ==="))
        # Ronde lanjutan minta lebih banyak dari sisa kebutuhan (buffer 1.5x)
        # supaya walau sebagian gagal ekstraksi/di luar kriteria, tetap ada
        # peluang wajar mendekati target - dibatasi max 30/ronde supaya satu
        # ronde tidak jadi terlalu lama.
        round_target = min(30, max(remaining, int(remaining * 1.5)))
        log, comps = run_comparable_agent(
            alamat, kecamatan, kabkota, provinsi, luas_tanah, luas_bangunan,
            serper, groq, gemini, max_results=round_target,
            exclude_links=seen_links,
            subjek_lat=subjek_lat, subjek_lon=subjek_lon, radius_km=radius_km,
            luas_tanah_toleransi_pct=luas_tanah_toleransi_pct,
            luas_bangunan_toleransi_pct=luas_bangunan_toleransi_pct,
            search_page=round_no,
            lang=lang,
        )
        full_log.extend(log)

        n_new = 0
        for c in comps:
            link = c.get("link")
            if link and link in seen_links:
                continue
            if link:
                seen_links.add(link)
            all_comps.append(c)
            n_new += 1

        if n_new == 0:
            consecutive_empty_rounds += 1
        else:
            consecutive_empty_rounds = 0

        if consecutive_empty_rounds >= 2:
            full_log.append(t(
                "⚠ 2 ronde berturut-turut tidak menambah pembanding baru - "
                "kemungkinan besar listing publik yang tersedia di radius & "
                "toleransi ini memang sudah habis. Menghentikan pencarian "
                "otomatis (coba perbesar radius/toleransi kalau masih ingin "
                "lebih banyak, atau tambahkan pembanding manual).",
                "⚠ 2 consecutive rounds added no new comparables - the public "
                "listings available within this radius & tolerance are likely "
                "exhausted. Stopping the automatic search (try widening the "
                "radius/tolerance if you still want more, or add comparables "
                "manually)."
            ))
            break

    full_log.append(t(
        f"=== Selesai: {len(all_comps)}/{target} pembanding ditemukan "
        f"setelah {round_no} ronde pencarian ===",
        f"=== Done: {len(all_comps)}/{target} comparables found "
        f"after {round_no} search rounds ==="))

    # Dedup TAMBAHAN by konten (bukan cuma link persis): situs seperti 99.co
    # kadang menyajikan listing yang SAMA lewat beberapa URL berbeda (mis.
    # slug/anchor beda tapi properti & harga sama persis) - link-based dedup
    # di atas tidak menangkap ini, jadi appraiser bisa lihat "properti yang
    # sama" muncul 2-3x di daftar akhir seolah-olah pembanding independen.
    # Sinyal duplikat: sumber sama + harga sama + LT sama + LB sama.
    def _sig(c):
        return (c.get("sumber"), c.get("harga"), c.get("luas_tanah"), c.get("luas_bangunan"))

    seen_sig = set()
    deduped_comps = []
    n_dup_content = 0
    for c in all_comps:
        sig = _sig(c)
        if sig in seen_sig and all(sig):
            n_dup_content += 1
            continue
        seen_sig.add(sig)
        deduped_comps.append(c)
    if n_dup_content:
        full_log.append(t(
            f"⚠ {n_dup_content} listing dibuang karena kelihatannya "
            "duplikat properti yang sama (sumber+harga+LT+LB identik) "
            "walau URL-nya berbeda.",
            f"⚠ {n_dup_content} listings removed because they looked like "
            "duplicates of the same property (identical source+price+land+building area) "
            "despite having different URLs."))
    all_comps = deduped_comps

    return full_log, all_comps


# ---------------------------------------------------------------------------
# STEP 3 (tambahan) - AI OCR Estimasi Umur & Klasifikasi Bangunan dari Foto
# ---------------------------------------------------------------------------
# Catatan: fitur ini BUTUH Gemini API key secara spesifik (bukan Groq), karena
# saat ini hanya GeminiClient yang mendukung input multimodal (gambar) lewat
# generate_with_image(). Kalau Gemini key tidak diisi, fungsi ini langsung
# mengembalikan error yang jelas supaya UI bisa menampilkan pesan yang tepat
# (bukan diam-diam gagal atau memakai Groq yang tidak mendukung gambar).
BUILDING_AGE_PROMPT_SINGLE = (
    "Anda adalah surveyor properti berpengalaman di Indonesia. Amati foto rumah/bangunan "
    "berikut dan perkirakan dua hal:\n"
    "1. klasifikasi_bangunan: kategorikan bangunan ke SALAH SATU dari 3 kelas berikut "
    "berdasarkan kualitas material, finishing, dan kesan umum: \"Sederhana\" (dinding "
    "sederhana, atap genteng biasa, tanpa banyak ornamen), \"Menengah\" (kualitas standar "
    "perumahan modern, keramik, cat rapi), atau \"Mewah\" (material premium, desain "
    "arsitektural khusus, finishing detail, tanda-tanda properti kelas atas).\n"
    "2. estimasi_umur_tahun: perkirakan umur bangunan dalam TAHUN (angka bulat) "
    "berdasarkan tanda-tanda visual seperti kondisi cat/dinding, gaya arsitektur, "
    "keausan atap, kondisi taman/halaman, dan elemen desain yang menunjukkan era "
    "pembangunan. Kalau bangunan tampak baru/baru direnovasi total, boleh mendekati 0.\n\n"
    "PENTING: Ini HANYA estimasi visual kasar dari satu foto, BUKAN pemeriksaan "
    "struktural - appraiser WAJIB tetap memverifikasi langsung di lapangan. Jangan "
    "mengarang detail yang tidak terlihat di foto. Jawab HANYA JSON dengan format:\n"
    '{"klasifikasi_bangunan": "Sederhana"|"Menengah"|"Mewah", '
    '"estimasi_umur_tahun": number, "confidence": "Rendah"|"Sedang"|"Tinggi", '
    '"alasan_singkat": str (maks 200 karakter, jelaskan tanda visual yang dipakai)}'
)

BUILDING_AGE_PROMPT_MULTI = (
    "Anda adalah surveyor properti berpengalaman di Indonesia. Anda diberi BEBERAPA foto "
    "dari rumah/bangunan YANG SAMA (mis. tampak depan, samping, belakang, atau detail "
    "lainnya). Gabungkan semua foto sebagai satu kesatuan bukti visual dan perkirakan dua "
    "hal untuk bangunan tersebut secara keseluruhan (bukan per foto):\n"
    "1. klasifikasi_bangunan: kategorikan bangunan ke SALAH SATU dari 3 kelas berikut "
    "berdasarkan kualitas material, finishing, dan kesan umum: \"Sederhana\" (dinding "
    "sederhana, atap genteng biasa, tanpa banyak ornamen), \"Menengah\" (kualitas standar "
    "perumahan modern, keramik, cat rapi), atau \"Mewah\" (material premium, desain "
    "arsitektural khusus, finishing detail, tanda-tanda properti kelas atas).\n"
    "2. estimasi_umur_tahun: perkirakan umur bangunan dalam TAHUN (angka bulat) "
    "berdasarkan tanda-tanda visual seperti kondisi cat/dinding, gaya arsitektur, "
    "keausan atap, kondisi taman/halaman, dan elemen desain yang menunjukkan era "
    "pembangunan. Kalau bangunan tampak baru/baru direnovasi total, boleh mendekati 0. "
    "Kalau foto-foto menunjukkan hal yang berbeda-beda (mis. bagian depan baru "
    "direnovasi tapi bagian belakang tampak tua), pertimbangkan kondisi bangunan secara "
    "keseluruhan dan sebutkan hal itu di alasan_singkat.\n\n"
    "PENTING: Ini HANYA estimasi visual kasar dari foto, BUKAN pemeriksaan struktural - "
    "appraiser WAJIB tetap memverifikasi langsung di lapangan. Jangan mengarang detail "
    "yang tidak terlihat di foto. Jawab HANYA JSON dengan format:\n"
    '{"klasifikasi_bangunan": "Sederhana"|"Menengah"|"Mewah", '
    '"estimasi_umur_tahun": number, "confidence": "Rendah"|"Sedang"|"Tinggi", '
    '"alasan_singkat": str (maks 200 karakter, jelaskan tanda visual yang dipakai dari '
    'foto-foto tersebut)}'
)


def run_building_age_ocr_agent(images, gemini: GeminiClient):
    """
    Estimasi klasifikasi bangunan (Sederhana/Menengah/Mewah) dan umur bangunan
    (tahun) dari SATU ATAU LEBIH foto rumah, memakai Gemini vision. Semua foto
    dikirim dalam satu request supaya AI mensintesis satu kesimpulan dari
    beberapa sudut (mis. tampak depan + samping + belakang) alih-alih menilai
    tiap foto terpisah.

    `images` bisa berupa:
    - satu tuple (image_bytes, mime_type) - kompatibilitas mundur untuk
      pemanggil lama yang hanya punya satu foto, ATAU
    - list berisi tuple (image_bytes, mime_type) untuk beberapa foto sekaligus.

    Mengembalikan (ok: bool, result: dict|str) - result adalah dict berisi
    klasifikasi_bangunan, estimasi_umur_tahun, confidence, alasan_singkat
    kalau ok=True, atau pesan error (str) kalau ok=False.

    Ini murni ALAT BANTU PENGISIAN AWAL Step 3 (mengisi field Klasifikasi &
    Umur Bangunan) - appraiser tetap bisa/harus mengoreksi manual hasilnya,
    sama seperti Bhumi ZNT Agent di Step 2.
    """
    if not gemini or not gemini.api_key:
        return False, ("Fitur ini butuh Gemini API key (belum tersedia untuk gambar lewat "
                        "Groq). Isi Gemini API Key di sidebar ⚙️ API Keys terlebih dahulu.")

    # Normalisasi input: terima baik satu tuple tunggal maupun list of tuple.
    if isinstance(images, tuple) and len(images) == 2 and isinstance(images[0], (bytes, bytearray)):
        images = [images]
    images = list(images or [])
    if not images:
        return False, "Tidak ada foto yang diunggah."

    prompt = BUILDING_AGE_PROMPT_SINGLE if len(images) == 1 else BUILDING_AGE_PROMPT_MULTI
    ok, data = gemini.generate_with_images(prompt, images, json_mode=True)
    if not ok:
        return False, f"Analisa foto gagal: {data}"

    required = {"klasifikasi_bangunan", "estimasi_umur_tahun"}
    if not isinstance(data, dict) or not required.issubset(data.keys()):
        return False, f"Respons AI tidak lengkap: {data}"

    if data.get("klasifikasi_bangunan") not in ("Sederhana", "Menengah", "Mewah"):
        data["klasifikasi_bangunan"] = "Menengah"  # fallback aman
    try:
        data["estimasi_umur_tahun"] = max(0, int(round(float(data["estimasi_umur_tahun"]))))
    except (TypeError, ValueError):
        data["estimasi_umur_tahun"] = 0

    return True, data
