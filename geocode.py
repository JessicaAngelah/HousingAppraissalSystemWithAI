"""
geocode.py
Geocoding gratis pakai OpenStreetMap Nominatim (tanpa API key), dipakai
sebagai fallback saat user memilih mode "Search Address" di Step 1 dan
belum ada latitude/longitude - padahal BhumiZntAgent butuh koordinat
persis untuk mencari titik di peta Bhumi ATR/BPN.

Nominatim usage policy mewajibkan header User-Agent yang jelas dan
membatasi rate ~1 request/detik, jadi jangan dipanggil berulang cepat.
"""

import time
import threading
import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"

# --- Throttle GLOBAL antar-proses (bukan per-session) ---------------------
# Nominatim usage policy membatasi ketat ~1 request/detik PER IP. App ini
# (mis. di-deploy Streamlit) menjalankan semua sesi user dalam satu proses
# yang keluar lewat IP yang SAMA - kalau dua appraiser klik "Cari Alamat"/
# "Isi Otomatis Alamat" hampir bersamaan (atau satu appraiser klik cepat
# berkali-kali karena rerun Streamlit), request-nya bisa saling tabrakan dan
# kena 429 walau retry-nya sendiri sudah benar. _throttle() memaksa jeda
# minimum di sisi kita SENDIRI (bukan cuma reaktif lewat retry) sebelum tiap
# request ke Nominatim, dikunci lewat threading.Lock supaya aman dipanggil
# dari banyak thread Streamlit sekaligus.
_throttle_lock = threading.Lock()
_last_call_ts = 0.0
_MIN_INTERVAL_S = 1.1  # sedikit di atas 1 req/detik supaya ada margin aman


def _throttle():
    global _last_call_ts
    with _throttle_lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL_S - (now - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.monotonic()


# --- Cache kecil in-memory untuk reverse_geocode ---------------------------
# Kalau appraiser klik "Isi Otomatis Alamat" berkali-kali untuk titik yang
# SAMA (mis. klik lagi setelah gagal karena 429 sebelumnya), tidak perlu
# panggil Nominatim ulang - pakai hasil yang sudah didapat sebelumnya kalau
# ada dan belum kedaluwarsa. Key dibulatkan ke 6 desimal (~11cm) supaya
# perbedaan floating-point kecil tidak dianggap titik berbeda.
_reverse_cache = {}
_REVERSE_CACHE_TTL_S = 300  # 5 menit
_REVERSE_CACHE_MAX = 200


def _reverse_cache_get(lat: float, lon: float):
    key = (round(lat, 6), round(lon, 6))
    entry = _reverse_cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _REVERSE_CACHE_TTL_S:
        _reverse_cache.pop(key, None)
        return None
    return value


def _reverse_cache_set(lat: float, lon: float, value):
    if len(_reverse_cache) >= _REVERSE_CACHE_MAX:
        _reverse_cache.clear()
    _reverse_cache[(round(lat, 6), round(lon, 6))] = (time.monotonic(), value)

# Kode ISO 3166-2:ID -> nama provinsi (Bahasa Indonesia). Dipakai sebagai
# fallback TERAKHIR untuk mengisi field Provinsi di reverse_geocode() kalau
# key teks "state" dkk memang tidak ada di respons Nominatim (kejadian nyata
# untuk sebagian titik di DKI Jakarta) - kode ISO ini baku/deterministik,
# jadi jauh lebih aman dipakai daripada menebak dari key administratif lain.
# Sumber: ISO 3166-2:ID (en.wikipedia.org/wiki/ISO_3166-2:ID), termasuk
# provinsi hasil pemekaran Papua 2022-2023.
_ISO3166_2_ID_TO_PROVINSI = {
    "ID-AC": "Aceh",
    "ID-BA": "Bali",
    "ID-BB": "Kepulauan Bangka Belitung",
    "ID-BT": "Banten",
    "ID-BE": "Bengkulu",
    "ID-GO": "Gorontalo",
    "ID-JK": "DKI Jakarta",
    "ID-JA": "Jambi",
    "ID-JB": "Jawa Barat",
    "ID-JT": "Jawa Tengah",
    "ID-JI": "Jawa Timur",
    "ID-KB": "Kalimantan Barat",
    "ID-KS": "Kalimantan Selatan",
    "ID-KT": "Kalimantan Tengah",
    "ID-KI": "Kalimantan Timur",
    "ID-KU": "Kalimantan Utara",
    "ID-KR": "Kepulauan Riau",
    "ID-LA": "Lampung",
    "ID-MA": "Maluku",
    "ID-MU": "Maluku Utara",
    "ID-NB": "Nusa Tenggara Barat",
    "ID-NT": "Nusa Tenggara Timur",
    "ID-PA": "Papua",
    "ID-PB": "Papua Barat",
    "ID-PD": "Papua Barat Daya",
    "ID-PE": "Papua Pegunungan",
    "ID-PS": "Papua Selatan",
    "ID-PT": "Papua Tengah",
    "ID-RI": "Riau",
    "ID-SR": "Sulawesi Barat",
    "ID-SN": "Sulawesi Selatan",
    "ID-ST": "Sulawesi Tengah",
    "ID-SG": "Sulawesi Tenggara",
    "ID-SA": "Sulawesi Utara",
    "ID-SB": "Sumatera Barat",
    "ID-SS": "Sumatera Selatan",
    "ID-SU": "Sumatera Utara",
    "ID-YO": "DI Yogyakarta",
}


def _call(q: str, limit: int, max_retries: int = 3):
    delay = 2.0
    for attempt in range(max_retries + 1):
        _throttle()
        try:
            resp = requests.get(
                NOMINATIM_URL,
                params={
                    "q": q, "format": "json", "limit": limit,
                    "countrycodes": "id", "addressdetails": 1,
                },
                headers={"User-Agent": "sistem-penilaian-agunan-properti/1.0"},
                timeout=15,
            )
            if resp.status_code in (429, 503) and attempt < max_retries:
                time.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            return True, resp.json()
        except requests.Timeout:
            if attempt < max_retries:
                time.sleep(delay)
                delay *= 2
                continue
            return False, "Geocoding timeout - server peta tidak merespons, coba lagi."
        except requests.RequestException as e:
            return False, f"Geocoding error: {e}"
    return False, "Geocoding gagal setelah beberapa percobaan (server sedang sibuk)."


def geocode_search(query: str, limit: int = 5):
    """
    Cari alamat dan kembalikan BEBERAPA kandidat lokasi (bukan cuma yang
    teratas) - penting karena nama jalan/perumahan seperti "Jl Palem" bisa
    ada di banyak kabupaten/kota berbeda (mis. Ciracas Jakarta Timur vs
    Garut), jadi user harus memilih sendiri yang benar alih-alih sistem
    menebak otomatis kandidat pertama.

    Returns (ok: bool, result) where result is a LIST of dicts
    {"lat": float, "lon": float, "display_name": str, "type": str}
    on success (list bisa berisi 1-limit item, urut dari yang paling
    relevan menurut Nominatim), atau pesan error (str) kalau ok=False.

    CATATAN: ini HANYA lewat Nominatim/OSM (gratis, tanpa API key) - basis
    datanya sering TIDAK punya nama kompleks perumahan kecil di Indonesia
    (mis. "Perumahan Azna Residence") walaupun properti itu ada dan mudah
    ditemukan di Google Maps. Kalau fungsi ini gagal padahal appraiser yakin
    alamatnya benar, pakai geocode_search_with_places_fallback() di bawah
    (butuh Serper API key) supaya ikut mencoba lewat Google Places juga.
    """
    if not query or not query.strip():
        return False, "Alamat kosong."

    ok, results = _call(query.strip(), limit)
    if not ok:
        return False, results

    # Fallback: kalau alamat lengkap (mis. "Perumahan X Blok Y, Kecamatan Z")
    # tidak ketemu sama sekali, coba lagi dengan bagian setelah koma pertama
    # saja - sering kali nama perumahan/blok spesifik tidak ada di data OSM,
    # tapi kecamatan/kota-nya ada.
    if not results and "," in query:
        simplified = query.split(",", 1)[1].strip()
        if simplified:
            ok2, results2 = _call(simplified, limit)
            if ok2 and results2:
                results = results2

    if not results:
        return False, ("Alamat tidak ditemukan. Coba alamat yang lebih singkat/umum "
                        "(mis. hanya kecamatan/kota), atau gunakan mode \"Pinpoint on Map\".")

    candidates = []
    for r in results:
        try:
            candidates.append({
                "lat": float(r["lat"]),
                "lon": float(r["lon"]),
                "display_name": r.get("display_name", ""),
                "type": r.get("type") or r.get("class") or "",
            })
        except (KeyError, ValueError):
            continue

    if not candidates:
        return False, "Geocoding response tidak terduga: tidak ada koordinat valid."
    return True, candidates


def geocode_search_with_places_fallback(query: str, serper=None, limit: int = 8):
    """
    Sama seperti geocode_search(), TAPI kalau Nominatim/OSM tidak menemukan
    apa-apa DAN sebuah SerperClient (dengan API key terisi) diberikan lewat
    parameter `serper`, otomatis coba lagi lewat Google Places (via Serper)
    sebagai fallback kedua sebelum benar-benar menyerah.

    Ini untuk kasus seperti "Perumahan Azna Residence" - nama kompleks
    perumahan kecil yang sering TIDAK ada di data OpenStreetMap (basis data
    Nominatim), padahal ada dan gampang ditemukan di Google Maps. Google
    Places (lewat endpoint /places Serper) memakai basis data yang sama
    dengan Google Maps, jadi cakupannya jauh lebih lengkap untuk kasus ini -
    tapi BUTUH Serper API key (beda dari Nominatim yang gratis).

    Returns (ok: bool, result, source: str) - result adalah LIST kandidat
    dengan format sama seperti geocode_search() (lat/lon/display_name/type).
    source adalah "nominatim" atau "google_places" supaya UI bisa kasih tahu
    user hasilnya dari mana.
    """
    ok, result = geocode_search(query, limit=limit)
    if ok and result:
        return True, result, "nominatim"

    if serper is None or not getattr(serper, "api_key", None):
        # Tidak ada Serper key - tetap kembalikan error asli dari Nominatim
        # (biasanya lebih informatif), TAPI tambahkan catatan eksplisit bahwa
        # fallback Google Places TIDAK dicoba karena key kosong - supaya user
        # tahu ini alasannya kalau sebelumnya alamat sebenarnya ADA di Google
        # Maps tapi tidak ketemu di sini (mis. isi dulu Serper API Key di
        # sidebar sebelum mencari alamat di Step 1).
        base_msg = result if isinstance(result, str) else "Alamat tidak ditemukan."
        return False, f"{base_msg} (Fallback Google Places tidak dicoba - isi dulu Serper API Key di sidebar.)", "nominatim"

    ok_places, data = serper.places(query.strip(), num=limit)
    if not ok_places:
        return False, f"Nominatim tidak menemukan alamat, dan Google Places juga gagal: {data}", "google_places"

    places = (data or {}).get("places", [])
    candidates = []
    for p in places:
        try:
            lat = p.get("latitude")
            lon = p.get("longitude")
            if lat is None or lon is None:
                continue
            title = p.get("title", "")
            address = p.get("address", "")
            display_name = f"{title} — {address}" if title and address else (title or address)
            candidates.append({
                "lat": float(lat),
                "lon": float(lon),
                "display_name": display_name or query,
                "type": p.get("type", "") or "",
            })
        except (TypeError, ValueError):
            continue

    if not candidates:
        return False, ("Alamat tidak ditemukan baik lewat Nominatim maupun Google Places. Coba "
                        "alamat yang lebih singkat/umum (mis. hanya kecamatan/kota), atau gunakan "
                        "mode \"Pinpoint on Map\"."), "google_places"
    return True, candidates, "google_places"


PHOTON_REVERSE_URL = "https://photon.komoot.io/reverse"


def _reverse_geocode_nominatim(lat: float, lon: float, lang: str, max_retries: int):
    """Coba reverse geocoding lewat Nominatim/OSM. Returns (ok, data|error_msg)
    - data adalah JSON mentah dari Nominatim kalau ok=True."""
    delay = 2.0
    for attempt in range(max_retries + 1):
        _throttle()
        try:
            resp = requests.get(
                NOMINATIM_REVERSE_URL,
                params={
                    "lat": lat, "lon": lon, "format": "json",
                    "addressdetails": 1, "zoom": 18,
                    "accept-language": lang,
                },
                headers={"User-Agent": "sistem-penilaian-agunan-properti/1.0"},
                timeout=15,
            )
            if resp.status_code in (429, 503) and attempt < max_retries:
                time.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            return True, resp.json()
        except requests.Timeout:
            if attempt < max_retries:
                time.sleep(delay)
                delay *= 2
                continue
            return False, "timeout"
        except requests.RequestException as e:
            return False, str(e)
    return False, "rate_limited"


def _reverse_geocode_photon(lat: float, lon: float, lang: str):
    """
    Fallback KEDUA kalau Nominatim gagal (mis. 429 - server publiknya lagi
    membatasi/nge-block IP hosting aplikasi ini, yang seringkali dipakai
    bersama banyak aplikasi lain kalau di-deploy di platform seperti
    Streamlit Cloud, sehingga retry/throttle di sisi kita sendiri saja tidak
    selalu cukup). Photon (photon.komoot.io) juga berbasis data OpenStreetMap
    dan GRATIS tanpa API key, TAPI di-host di server terpisah dari Nominatim -
    jadi kalaupun IP kita lagi dibatasi khusus di Nominatim, Photon biasanya
    masih bisa diakses.

    CATATAN: skema properti admin Photon untuk Indonesia tidak selalu
    sekonsisten/selengkap Nominatim (mis. kadang kecamatan/kelurahan kosong
    padahal ada di Nominatim) - makanya ini dipakai sebagai fallback KEDUA,
    bukan pengganti Nominatim.

    Returns (ok, data) dengan skema field sama seperti hasil parsed
    reverse_geocode() (bukan JSON mentah Photon), atau (False, error_msg).
    """
    try:
        resp = requests.get(
            PHOTON_REVERSE_URL,
            params={"lat": lat, "lon": lon, "lang": lang if lang in ("en", "de", "fr") else "default"},
            headers={"User-Agent": "sistem-penilaian-agunan-properti/1.0"},
            timeout=15,
        )
        resp.raise_for_status()
        geojson = resp.json()
    except requests.RequestException as e:
        return False, f"Photon error: {e}"

    features = (geojson or {}).get("features") or []
    if not features:
        return False, "Photon: tidak ada hasil untuk titik ini."

    props = features[0].get("properties", {}) or {}

    street = props.get("street") or ""
    house_number = props.get("housenumber") or ""
    jalan = f"{street} {house_number}".strip() if street else ""

    kelurahan = props.get("suburb") or props.get("neighbourhood") or ""
    kecamatan = props.get("district") or ""
    kabkota = props.get("city") or props.get("county") or ""
    provinsi = props.get("state") or ""
    postcode = props.get("postcode") or ""

    alamat_parts = [p for p in [jalan, kelurahan] if p]
    if not alamat_parts:
        alamat_parts = [p for p in [props.get("name"), kecamatan, kabkota] if p]
    display_name = ", ".join(
        [p for p in [props.get("name"), street, kelurahan, kecamatan, kabkota, provinsi] if p]
    )

    return True, {
        "display_name": display_name,
        "alamat": ", ".join(alamat_parts) if alamat_parts else display_name,
        "kecamatan": kecamatan,
        "kabkota": kabkota,
        "provinsi": provinsi,
        "postcode": postcode,
        "raw_address": props,
        "source": "photon",
    }


def reverse_geocode(lat: float, lon: float, lang: str = "id", max_retries: int = 3):
    """
    Reverse geocoding: dari koordinat (lat/lon) balik jadi alamat manusiawi -
    dipakai untuk mode "Pinpoint on Map" ala Gojek/Grab/Shopee. Begitu
    appraiser mengklik/menggeser pin di peta lalu menekan tombol "Konfirmasi
    Lokasi Ini", titik itu dikirim ke sini supaya Alamat/Kecamatan/Kabupaten-
    Kota/Provinsi di form Step 1 bisa terisi OTOMATIS dari titik yang
    diklik - persis seperti appstage pinpoint di Gojek/Grab/Shopee yang
    langsung menuliskan alamat begitu pin dikonfirmasi, alih-alih appraiser
    harus mengetik ulang alamat secara manual.

    CATATAN: hasil dari Nominatim/OSM tidak selalu punya nama kompleks
    perumahan kecil (mis. jalan di dalam suatu perumahan yang belum dipetakan
    OSM) - appraiser tetap bisa mengedit manual field alamat/kecamatan/
    kabkota/provinsi kalau hasilnya kurang presisi. Koordinat (lat/lon) yang
    dipakai untuk Bhumi ZNT Agent dkk tetap dari pin yang diklik, TIDAK
    terpengaruh sama sekali walaupun reverse geocoding gagal/kurang akurat.

    Returns (ok: bool, result) where result on success is a dict:
    {
        "display_name": str,   # alamat lengkap versi Nominatim
        "alamat": str,         # jalan + no rumah (+ kelurahan) - untuk field Alamat Properti
        "kecamatan": str,      # kecamatan/distrik
        "kabkota": str,        # kabupaten/kota
        "provinsi": str,       # provinsi
        "postcode": str,
        "raw_address": dict,   # address block mentah dari Nominatim (buat debug/edge-case)
    }
    atau pesan error (str) kalau ok=False.
    """
    cached = _reverse_cache_get(lat, lon)
    if cached is not None:
        return True, cached

    max_retries = max(max_retries, 3)
    ok_nom, data_or_err = _reverse_geocode_nominatim(lat, lon, lang, max_retries)

    if not ok_nom:
        # Nominatim gagal (429/503/timeout/error lain) - coba fallback Photon
        # sebelum menyerah, karena IP hosting seringkali dibatasi khusus di
        # Nominatim tapi masih bisa akses Photon (server terpisah).
        ok_photon, photon_result = _reverse_geocode_photon(lat, lon, lang)
        if ok_photon:
            _reverse_cache_set(lat, lon, photon_result)
            return True, photon_result
        # Nominatim dan Photon dua-duanya gagal - kembalikan pesan gabungan
        # supaya jelas keduanya sudah dicoba, bukan cuma satu.
        nom_msg = {
            "timeout": "Nominatim timeout (server peta tidak merespons).",
            "rate_limited": "Nominatim membatasi permintaan (429) setelah beberapa percobaan.",
        }.get(data_or_err, f"Nominatim error: {data_or_err}")
        return False, (
            f"{nom_msg} Fallback Photon juga gagal: {photon_result} "
            "Titik ini kemungkinan sedang tidak bisa diterjemahkan jadi alamat "
            "lewat layanan geocoding gratis manapun untuk saat ini - coba lagi "
            "sebentar lagi, atau isi alamat secara manual."
        )

    data = data_or_err

    if not data or "address" not in data or data.get("error"):
        return False, ("Titik ini tidak bisa diterjemahkan jadi alamat (mis. di tengah laut/hutan "
                        "tanpa data jalan di OpenStreetMap). Koordinat tetap tersimpan - isi alamat "
                        "manual di bawah.")

    addr = data.get("address", {}) or {}

    road = addr.get("road") or addr.get("pedestrian") or addr.get("footway") or ""
    house_number = addr.get("house_number") or ""
    jalan = f"{road} {house_number}".strip() if road else ""

    # Nominatim tidak punya skema key yang TETAP untuk struktur alamat
    # Indonesia (jalan/kelurahan/kecamatan/kabkota/provinsi) - ini masalah
    # yang sudah diketahui di Nominatim sendiri (lih. diskusi upstream
    # osm-search/Nominatim #2379). Jadi dicoba beberapa kemungkinan nama key
    # yang umum muncul dulu (Langkah 1) - ini yang paling bisa dipercaya
    # karena key-nya sendiri sudah eksplisit menyebut level administratifnya.
    kelurahan = (addr.get("village") or addr.get("hamlet")
                 or addr.get("neighbourhood") or addr.get("quarter") or "")
    kecamatan = (addr.get("city_district") or addr.get("district")
                 or addr.get("subdistrict") or addr.get("borough") or "")
    kabkota = (addr.get("city") or addr.get("county") or addr.get("regency")
               or addr.get("town") or addr.get("municipality") or "")
    provinsi = addr.get("state") or addr.get("state_district") or addr.get("region") or ""
    postcode = addr.get("postcode") or ""

    # --- Langkah 2: key "suburb" SENGAJA tidak dipakai di Langkah 1 karena
    # artinya tidak konsisten di data OSM - untuk sebagian wilayah (terutama
    # DKI Jakarta) "suburb" ternyata dipakai untuk KECAMATAN bukan kelurahan.
    # Dipakai di sini HANYA untuk mengisi field yang masih kosong (kelurahan
    # DULU baru kecamatan, supaya kalau kelurahan sudah ketemu lewat "village"
    # dkk di atas, "suburb" tidak dipakai ulang untuk kelurahan dan bebas
    # dipakai untuk kecamatan) - TIDAK menebak-nebak dari key lain yang tidak
    # relevan (mis. RT/RW), supaya field lebih baik dibiarkan kosong daripada
    # salah isi.
    suburb_val = addr.get("suburb") or ""
    if suburb_val:
        if not kelurahan:
            kelurahan = suburb_val
        elif not kecamatan and suburb_val != kelurahan:
            kecamatan = suburb_val

    # --- Langkah 3: kalau provinsi masih kosong (key "state" dkk memang
    # tidak ada di respons - ini terjadi untuk sebagian titik di DKI Jakarta),
    # coba turunkan dari kode ISO 3166-2 provinsi yang Nominatim SERINGKALI
    # tetap sertakan (mis. "ISO3166-2-lvl4": "ID-JK") walau nama teks
    # provinsinya sendiri tidak ada di address block. Kode ISO ini baku/
    # deterministik jadi jauh lebih aman dipakai sebagai fallback daripada
    # menebak dari key administratif lain yang levelnya tidak jelas.
    if not provinsi:
        iso = addr.get("ISO3166-2-lvl4") or ""
        provinsi = _ISO3166_2_ID_TO_PROVINSI.get(iso.upper(), "")

    # Alamat dirangkai selengkap mungkin dari bagian yang tersedia: jalan +
    # no rumah, RT/RW (kalau ada dan bukan yang sudah dipakai sebagai
    # kecamatan/kelurahan di atas), lalu kelurahan - supaya appraiser dapat
    # detail sebanyak mungkin dari OSM, bukan cuma nama kelurahan doang.
    quarter_val = addr.get("quarter") or ""
    rtrw = quarter_val if quarter_val not in (kelurahan, kecamatan) else ""
    alamat_parts = [p for p in [jalan, rtrw, kelurahan] if p]
    alamat = ", ".join(alamat_parts) if alamat_parts else data.get("display_name", "")

    result = {
        "display_name": data.get("display_name", ""),
        "alamat": alamat,
        "kecamatan": kecamatan,
        "kabkota": kabkota,
        "provinsi": provinsi,
        "postcode": postcode,
        "raw_address": addr,
    }
    _reverse_cache_set(lat, lon, result)
    return True, result


def geocode_address(query: str, max_retries: int = 2):
    """
    Kompatibilitas mundur: kembalikan HANYA kandidat teratas sebagai dict
    tunggal (dipakai di tempat yang cuma butuh perkiraan titik tengah peta,
    bukan pemilihan lokasi final - mis. tombol "Cari & Pindah Peta").
    Untuk alur pencarian alamat utama di Step 1, pakai geocode_search()
    supaya user bisa memilih di antara beberapa kandidat.
    """
    ok, result = geocode_search(query, limit=1)
    if not ok:
        return False, result
    return True, result[0]


def _places_candidates(serper, query: str, limit: int):
    """Helper: panggil serper.places() dan konversi ke format kandidat
    standar {"lat","lon","display_name","type"}. Return None kalau gagal
    total (bukan cuma 0 hasil) supaya caller bisa bedakan "API error" vs
    "genuinely 0 hasil"."""
    ok_places, data = serper.places(query, num=limit)
    if not ok_places:
        return None
    places = (data or {}).get("places", [])
    candidates = []
    for p in places:
        try:
            lat = p.get("latitude")
            lon = p.get("longitude")
            if lat is None or lon is None:
                continue
            title = p.get("title", "")
            address = p.get("address", "")
            display_name = f"{title} — {address}" if title and address else (title or address)
            candidates.append({
                "lat": float(lat),
                "lon": float(lon),
                "display_name": display_name or query,
                "type": p.get("type", "") or "",
            })
        except (TypeError, ValueError):
            continue
    return candidates


def geocode_search_gmaps_style(raw_query: str, serper=None, context_hint: str = "", limit: int = 8):
    """
    Pencarian alamat ala kotak pencarian Google Maps: ketik nama tempat APA
    ADANYA, dapat hasil nyata - TANPA memaksa masukkan konteks (kecamatan/
    kabkota/provinsi yang sudah diisi di form) ke dalam query dari awal.

    Versi sebelumnya (geocode_search_with_places_fallback dipanggil dengan
    query yang sudah "diperkaya" konteks di app.py) py BUG NYATA: kalau
    field kecamatan/kabkota di form masih terisi dari properti SEBELUMNYA
    yang sedang dites appraiser (mis. "Ciracas, Jakarta Timur" dari sesi
    sebelumnya), lalu appraiser cari alamat properti BARU di lokasi
    berbeda (mis. "Taman Sunter Agung" - sebenarnya di Jakarta Utara),
    query yang dikirim jadi "Taman Sunter Agung, Ciracas, Jakarta Timur,
    Jakarta" - kalimat yang SALING BERTENTANGAN (tempat itu tidak ada di
    Ciracas), sehingga Nominatim/Google Places bingung dan cuma
    mengembalikan kecocokan sebagian (nama kecamatan yang dikenali) sambil
    mengabaikan nama tempat yang sebenarnya dicari. Ini persis kebalikan
    dari cara kerja pencarian Google Maps, yang selalu mencari APA YANG
    DIKETIK dulu, baru menampilkan lokasi ASLI dari hasil yang ketemu.

    Strategi baru (urutan percobaan):
    1. Query MENTAH (persis yang diketik user) lewat Google Places (kalau
       ada Serper key) - ini yang paling mirip pengalaman search box Google
       Maps, cakupan POI/kompleks perumahan paling lengkap.
    2. Query MENTAH lewat Nominatim/OSM (gratis, kalau (1) tidak
       tersedia/kosong).
    3. HANYA kalau (1) dan (2) SAMA-SAMA kosong, baru dicoba lagi dengan
       konteks (kecamatan/kabkota/provinsi) ditambahkan di BELAKANG query -
       sebagai upaya terakhir untuk query yang genuinely ambigu/pendek
       (mis. cuma "Jl Palem"), BUKAN default yang dipaksakan dari awal.
       Kalau langkah ini yang berhasil, caller diberi tahu lewat
       used_context=True supaya UI transparan soal ini.

    CATATAN PENTING soal cakupan data: TANPA Serper API key (jadi HANYA
    lewat Nominatim/OpenStreetMap, gratis), nama kompleks perumahan kecil
    (mis. "Perumahan Palem Azna Residence") SERING TIDAK KETEMU sama
    sekali walaupun tempatnya nyata dan gampang ditemukan di Google Maps -
    ini BUKAN bug, tapi keterbatasan basis data OSM yang memang tidak
    selengkap Google Maps untuk kompleks perumahan kecil di Indonesia.
    Untuk pengalaman pencarian yang benar-benar setara Google Maps, isi
    Serper API Key di sidebar (mengaktifkan pencarian lewat Google Places).
    Kalau key itu tidak ada/tidak mau diisi, gunakan mode "Pinpoint on Map"
    lalu tombol "Isi Otomatis Alamat" sebagai alternatif - appraiser tinggal
    klik titik yang tepat di peta (lihat langsung dari tampilan peta,
    bukan dari nama tempat yang dicari), dan alamatnya akan ditulis otomatis
    dari koordinat itu.

    Returns (ok: bool, result, source: str, used_context: bool).
    source: "google_places" atau "nominatim".
    """
    raw_query = (raw_query or "").strip()
    # Bersihkan koma/spasi nyasar di ujung query (mis. sisa mengetik dari
    # placeholder contoh "Perumahan X, Kecamatan Y, Kota Z" tapi appraiser
    # cuma mengisi bagian pertama lalu meninggalkan koma trailing) - koma
    # kosong di ujung tidak menambah informasi apa pun buat pencarian dan
    # kadang bikin sebagian geocoder jadi lebih pemilih/gagal.
    raw_query = raw_query.strip(", ").strip()
    if not raw_query:
        return False, "Alamat kosong.", "nominatim", False

    has_serper = serper is not None and getattr(serper, "api_key", None)

    # --- Tahap 1: query mentah, Google Places dulu (paling mirip Google Maps) ---
    if has_serper:
        places_result = _places_candidates(serper, raw_query, limit)
        if places_result:
            return True, places_result, "google_places", False

    # --- Tahap 2: query mentah, Nominatim ---
    ok, result = geocode_search(raw_query, limit=limit)
    if ok and result:
        return True, result, "nominatim", False

    # --- Tahap 3: upaya terakhir - tambahkan konteks kecamatan/kabkota/provinsi ---
    if context_hint and context_hint.strip():
        broadened = raw_query
        missing = [p.strip() for p in context_hint.split(",")
                   if p.strip() and p.strip().lower() not in raw_query.lower()]
        if missing:
            broadened = f"{raw_query}, {', '.join(missing)}"

        if broadened.lower() != raw_query.lower():
            if has_serper:
                places_result2 = _places_candidates(serper, broadened, limit)
                if places_result2:
                    return True, places_result2, "google_places", True
            ok2, result2 = geocode_search(broadened, limit=limit)
            if ok2 and result2:
                return True, result2, "nominatim", True

    # --- Semua tahap gagal ---
    if not has_serper:
        return False, (
            f"{result if isinstance(result, str) else 'Alamat tidak ditemukan.'} "
            "Tanpa Serper API Key, pencarian cuma lewat OpenStreetMap (gratis) - basis "
            "datanya sering TIDAK punya nama kompleks perumahan kecil (mis. \"Perumahan "
            "Azna Residence\") walaupun tempatnya nyata dan gampang ditemukan di Google "
            "Maps. Ini bukan berarti alamatnya salah. Untuk pencarian setara Google Maps, "
            "isi Serper API Key di sidebar. Kalau tidak ada key-nya, langsung klik "
            "titik yang tepat di peta lewat mode \"Pinpoint on Map\", lalu tekan tombol "
            "\"📝 Isi Otomatis Alamat\" di bawah - alamatnya akan ditulis otomatis dari "
            "titik yang diklik, tanpa perlu ketik nama tempat sama sekali."
        ), "nominatim", False
    return False, (
        "Alamat tidak ditemukan baik lewat Google Places maupun OpenStreetMap - dicoba "
        "dengan query asli dan dengan tambahan konteks lokasi. Coba nama tempat yang "
        "lebih umum/singkat (mis. hanya nama jalan/kelurahan), atau langsung klik titik "
        "yang tepat di peta lewat mode \"Pinpoint on Map\" lalu tekan tombol \"📝 Isi "
        "Otomatis Alamat\" di bawah."
    ), "google_places", False
