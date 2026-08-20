"""
app.py
Sistem Penilaian Agunan Properti - antarmuka interaktif (Streamlit, bukan CLI).

Jalankan dengan:
    streamlit run app.py

Isi API key (Serper, Groq, Gemini) di sidebar "⚙️ API Keys" sebelum
menjalankan Step 2, 4, dan 6 (yang membutuhkan pencarian web + LLM).
"""

import datetime
import os
import streamlit as st

from api_clients import SerperClient, GroqClient, GeminiClient
from agents import (
    run_znt_agent, run_pinpoint_agent, run_manual_checklist_agent,
    run_comparable_agent, run_building_age_ocr_agent,
    search_comparables_until_target,
)
from geocode import (
    geocode_address, geocode_search, geocode_search_with_places_fallback,
    geocode_search_gmaps_style, reverse_geocode,
)
import calculations as calc

st.set_page_config(page_title="Sistem Penilaian Agunan Properti", layout="wide")

# Streamlit's st.metric widget truncates long values with an ellipsis (e.g.
# "Rp 400.0...") when the column is narrow - this happens a lot in this app
# since many metrics show large Rupiah amounts in multi-column layouts. Force
# the value text to wrap instead of truncating, app-wide.
st.markdown(
    """
    <style>
    div[data-testid="stMetricValue"] {
        overflow: visible;
        white-space: normal;
        text-overflow: unset;
        word-break: break-word;
        line-height: 1.2;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Session state init
# ---------------------------------------------------------------------------
def init_state():
    defaults = {
        "lang": "id",        # "id" (Bahasa Indonesia) or "en" (English)
        "step": 1,
        "data": {},          # Step 1 input
        "znt_result": {},    # Step 2
        "bangunan_result": {},  # Step 3
        "building_ocr_result": {},  # Step 3 - hasil AI OCR foto rumah (opsional)
        "auto_flags": {},    # Step 4
        "auto_notes": {},
        "checklist_auto_scores": {},   # Step 4 - manual checklist pre-fill
        "checklist_auto_notes": {},
        "checklist_auto_keys": set(),
        "manual_scores": {},
        "restriksi_flags": {},  # Step 4 - faktor pembatas/red-flag tambahan (SOP)
        "faktor_pengurang": {},
        "nilai_pasar_awal": 0,
        "comparables": [],   # Step 6
        "comparable_count": 15,
        "radius_km": 5.0,          # Step 6 - radius maksimum pencarian pembanding
        "luas_tanah_toleransi_pct": 0.20,      # Step 6 - toleransi luas tanah +/-20% (default)
        "luas_bangunan_toleransi_pct": 0.20,   # Step 6 - toleransi luas bangunan +/-20% (default)
        "validasi": {},      # Step 7
        "rentang_nilai_pasar": {},  # Step 7 - rentang nilai pasar (min/max/point)
        "nilai_pasar_akhir": 0,
        "nilai_likuidasi": 0,  # Step 8
        "njop_result": {},   # Step 9
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


init_state()


def t(id_text, en_text=None):
    """Return the Indonesian or English version of a UI string depending on
    the language toggle in the sidebar."""
    if en_text is None:
        return id_text
    return en_text if st.session_state.get("lang", "id") == "en" else id_text


def goto(step: int):
    st.session_state.step = step
    st.rerun()


def fmt_rp(x):
    try:
        return f"Rp {x:,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return str(x)


def _default_api_key(name: str) -> str:
    """
    Cari nilai default untuk API key `name` (mis. "SERPER_API_KEY") supaya
    appraiser TIDAK perlu ketik ulang key yang sama tiap kali buka app -
    dicek di 2 tempat (urutan prioritas):
    1. st.secrets - lewat file .streamlit/secrets.toml (lihat contoh di
       .streamlit/secrets.toml.example). Ini cara yang DISARANKAN kalau
       app di-deploy ke Streamlit Community Cloud, karena secrets.toml
       tidak ikut ke Git/tidak terlihat user lain.
    2. Environment variable dengan nama yang sama (mis. jalankan lewat
       `SERPER_API_KEY=xxx streamlit run app.py`, atau export dulu di
       shell/file .env sebelum menjalankan `streamlit run app.py`).

    Kalau tidak ketemu di keduanya, dikembalikan string kosong seperti
    sebelumnya (field API key di sidebar tetap kosong, appraiser isi
    manual seperti biasa) - TIDAK ADA key yang di-hardcode di kode ini.
    """
    try:
        val = st.secrets.get(name)  # type: ignore[union-attr]
        if val:
            return str(val)
    except Exception:
        # st.secrets melempar exception kalau file secrets.toml belum ada
        # sama sekali (bukan cuma dictionary kosong) - ini kondisi NORMAL
        # kalau appraiser belum pernah setup secrets.toml, jadi diamkan
        # dan lanjut coba environment variable di bawah.
        pass
    return os.environ.get(name, "")


# ---------------------------------------------------------------------------
# Sidebar - API keys & progress
# ---------------------------------------------------------------------------
with st.sidebar:
    lang_col1, lang_col2 = st.columns(2)
    with lang_col1:
        if st.button("Indonesia", use_container_width=True,
                      type="primary" if st.session_state.lang == "id" else "secondary"):
            st.session_state.lang = "id"
            st.rerun()
    with lang_col2:
        if st.button("English", use_container_width=True,
                      type="primary" if st.session_state.lang == "en" else "secondary"):
            st.session_state.lang = "en"
            st.rerun()
    st.divider()

    st.header("⚙️ API Keys")
    st.caption(t("Dipakai oleh Bhumi ZNT Agent, Pinpoint Screening Agent, dan Property Reference Agent.",
                 "Used by the Bhumi ZNT Agent, Pinpoint Screening Agent, and Property Reference Agent."))
    # Isi default dari .streamlit/secrets.toml atau environment variable
    # (lihat _default_api_key()) supaya appraiser TIDAK perlu ketik ulang
    # API key yang sama tiap kali buka app - st.session_state.setdefault
    # dipakai (bukan parameter value= langsung) karena widget dengan `key=`
    # mengambil nilainya dari session_state kalau sudah ada; setdefault
    # memastikan default ini HANYA dipakai saat field itu benar-benar belum
    # pernah diisi (mis. run pertama), bukan menimpa key yang appraiser
    # sudah ketik/ubah manual sebelumnya di sesi yang sama.
    st.session_state.setdefault("serper_key", _default_api_key("SERPER_API_KEY"))
    st.session_state.setdefault("groq_key", _default_api_key("GROQ_API_KEY"))
    st.session_state.setdefault("gemini_key", _default_api_key("GEMINI_API_KEY"))
    serper_key = st.text_input("Serper API Key", type="password", key="serper_key")
    groq_key = st.text_input("Groq API Key", type="password", key="groq_key")
    gemini_key = st.text_input("Gemini API Key", type="password", key="gemini_key")

    st.divider()
    st.header(t("Progress", "Progress"))
    steps_label = [
        t("1. Input Data Properti", "1. Property Data Input"),
        t("2. Nilai Tanah (Bhumi ZNT)", "2. Land Value (Bhumi ZNT)"),
        t("3. Nilai Bangunan (Cost Approach)", "3. Building Value (Cost Approach)"),
        t("4. Faktor Pengurang", "4. Reduction Factors"),
        t("5. Nilai Pasar Awal", "5. Initial Market Value"),
        t("6. Property Reference AI", "6. Property Reference AI"),
        t("7. Perbandingan Harga Tanah per m²", "7. Land Price per m² Comparison"),
        t("8. Validasi & Rentang Nilai Pasar", "8. Validation & Market Value Range"),
        t("9. Hasil Appraisal: Perbandingan Harga", "9. Appraisal Result: Price Comparison"),
        t("10. Analisis Rasio NJOP", "10. NJOP Ratio Analysis"),
        t("11. Nilai Likuidasi", "11. Liquidation Value"),
        t("12. Laporan Penilaian Agunan (LPA)", "12. Collateral Appraisal Report (LPA)"),
    ]
    current_step = st.session_state.step
    # Step 12 ("Laporan") merender laporan LENGKAP di satu halaman, termasuk
    # bagian "Ringkasan Hasil Appraisal" - jadi begitu user sampai di halaman
    # ini, tidak ada lagi yang "in progress"; semuanya sudah selesai.
    for i, label in enumerate(steps_label, start=1):
        if current_step >= 12:
            prefix = "✅"
        elif i == current_step:
            prefix = "➡️"
        elif i < current_step:
            prefix = "✅"
        else:
            prefix = "⬜"
        st.write(f"{prefix} {label}")

serper = SerperClient(serper_key)
groq = GroqClient(groq_key)
gemini = GeminiClient(gemini_key)

st.title(t("🏠 Sistem Penilaian Agunan Properti", "🏠 Collateral Property Appraisal System"))

# ===========================================================================
# STEP 1 - Input Data Properti
# ===========================================================================
if st.session_state.step == 1:
    st.header(t("Step 1 — Input Data Properti", "Step 1 — Property Data Input"))

    # Provinsi/kabkota/kecamatan/alamat dipakai sebagai konteks pencarian di
    # bagian Lokasi Properti di bawah ini. Karena Lokasi Properti sengaja
    # ditampilkan SEBELUM field Basic Property Information (field itu baru
    # dirender belakangan, di bawah), nilainya diambil dari session_state
    # milik widget-widget itu (key yang sama dipakai saat widget-nya benar-
    # benar dirender di bawah) - jadi nilai dari rerun sebelumnya tetap
    # tersedia untuk memperkaya query pencarian.
    provinsi = st.session_state.get("step1_provinsi", "")
    kabkota = st.session_state.get("step1_kabkota", "")
    kecamatan = st.session_state.get("step1_kecamatan", "")
    alamat = st.session_state.get("step1_alamat", "")

    st.subheader(t("Lokasi Properti", "Property Location"))
    # Search Address ditaruh PALING PERTAMA (jadi mode default) - appraiser
    # coba cari alamat lewat nama tempat dulu (cara paling cepat kalau
    # ketemu). Pinpoint on Map & Manual Lat/Lon tetap tersedia sebagai
    # alternatif untuk kasus pencarian nama tempat tidak ketemu (mis.
    # kompleks perumahan kecil yang belum ada di OpenStreetMap), atau saat
    # koordinat sudah diketahui persis.
    lokasi_mode = st.radio(
        t("Metode input lokasi", "Location input method"),
        ["Search Address", "Pinpoint on Map", "Manual Latitude & Longitude"],
        horizontal=True,
    )

    # lat/lon dipertahankan lintas rerun & pergantian mode lewat session_state,
    # supaya titik yang sudah dicari/diklik tidak hilang kalau widget lain di
    # form ini berubah (Streamlit rerun seluruh script tiap interaksi).
    if "pin_lat" not in st.session_state:
        st.session_state.pin_lat = None
    if "pin_lon" not in st.session_state:
        st.session_state.pin_lon = None
    # Label alamat hasil tombol "Isi Otomatis Alamat" (reverse geocoding dari
    # koordinat) - ditampilkan sebagai konfirmasi setelah tombol ditekan,
    # berlaku untuk ketiga mode input lokasi (Pinpoint, Search Address, Manual
    # Lat/Lon) karena semuanya berujung ke pin_lat/pin_lon yang sama.
    if "autofill_address_label" not in st.session_state:
        st.session_state.autofill_address_label = None
    if "autofill_coords" not in st.session_state:
        st.session_state.autofill_coords = None

    lat, lon = st.session_state.pin_lat, st.session_state.pin_lon

    if lokasi_mode == "Search Address":
        st.caption(t(
            "Cari alamat seperti di Google Maps — ketik nama tempatnya, sistem mencari "
            "PERSIS itu dulu (lewat Google Places kalau Serper key terisi, lalu OpenStreetMap "
            "Nominatim sebagai pelengkap). Kecamatan/Kabupaten-Kota/Provinsi yang sudah "
            "diisi di atas TIDAK dipaksakan ke dalam pencarian - itu cuma dipakai sebagai "
            "upaya terakhir kalau pencarian nama tempatnya sendiri benar-benar tidak ketemu "
            "sama sekali. Nama tempat yang umum (mis. \"Jl Palem\") bisa muncul di banyak "
            "kota berbeda, jadi hasil pencarian akan menampilkan beberapa kandidat untuk "
            "dipilih — bukan langsung menebak yang pertama.",
            "Search for an address like Google Maps — type the place name, the system "
            "searches for EXACTLY that first (via Google Places if a Serper key is set, then "
            "OpenStreetMap Nominatim as a fallback). The Sub-district/City-Regency/Province "
            "filled in above are NOT forced into the search — they're only used as a last "
            "resort if searching the place name alone truly finds nothing. Common place names "
            "(e.g. \"Jl Palem\") can appear in many different cities, so results will show "
            "several candidates to pick from — instead of guessing the first one."
        ))
        sc1, sc2 = st.columns([3, 1])
        with sc1:
            search_query = st.text_input(
                t("Cari alamat", "Search address"), value=alamat or "", key="search_query_step1",
                placeholder=t("mis. Perumahan Palem Azna Residence, Ciracas, Jakarta Timur",
                               "e.g. Perumahan Palem Azna Residence, Ciracas, East Jakarta"),
            )
        with sc2:
            st.write("")
            st.write("")
            do_search = st.button(t("🔍 Cari", "🔍 Search"), use_container_width=True, key="btn_search_address")
        if do_search:
            if not search_query.strip():
                st.error(t("Isi dulu alamat yang ingin dicari.", "Please enter an address to search for."))
            else:
                # PENTING: dulu di sini kecamatan/kabkota/provinsi form LANGSUNG
                # digabung ke query SEBELUM pencarian pertama - kalau field itu
                # masih terisi dari properti lain yang sedang/sudah ditest
                # appraiser (mis. "Ciracas, Jakarta Timur" tersisa dari sesi
                # sebelumnya) padahal alamat yang dicari sekarang di kota lain
                # (mis. "Taman Sunter Agung" - itu di Jakarta Utara), hasilnya
                # query yang saling bertentangan dan pencarian gagal total -
                # padahal alamatnya sendiri gampang ditemukan. Sekarang query
                # MENTAH (persis yang diketik) dicoba dulu; konteks form cuma
                # dipakai sebagai upaya TERAKHIR kalau itu benar-benar gagal.
                context_hint = ", ".join(
                    p.strip() for p in [kecamatan, kabkota, provinsi] if p and p.strip()
                )

                with st.spinner(t("Mencari lokasi...", "Searching for location...")):
                    ok_geo, geo, geo_source, used_context = geocode_search_gmaps_style(
                        search_query, serper=serper, context_hint=context_hint, limit=8
                    )
                if ok_geo and geo:
                    st.session_state.search_candidates = geo
                    st.session_state.search_candidates_source = geo_source
                    st.session_state.search_candidate_pick = 0
                    if used_context:
                        if st.session_state.lang == "en":
                            st.caption(f"🔎 Place name alone not found — search expanded with "
                                       f"location context ({context_hint}).")
                        else:
                            st.caption(f"🔎 Nama tempat saja tidak ketemu — pencarian diperluas dengan "
                                       f"konteks lokasi ({context_hint}).")
                    if geo_source == "google_places":
                        st.caption(t("📍 Hasil dari Google Places (via Serper).", "📍 Results from Google Places (via Serper)."))
                    else:
                        st.caption(t("📍 Hasil dari OpenStreetMap (Nominatim).", "📍 Results from OpenStreetMap (Nominatim)."))
                else:
                    st.session_state.search_candidates = []
                    if st.session_state.lang == "en":
                        st.error(f"Search failed — {geo}")
                    else:
                        st.error(f"Pencarian gagal — {geo}")

        candidates = st.session_state.get("search_candidates") or []
        if candidates:
            labels = [f"{i+1}. {c['display_name']}" for i, c in enumerate(candidates)]
            if st.session_state.lang == "en":
                st.markdown(f"**Found {len(candidates)} location candidates — pick the best match:**")
            else:
                st.markdown(f"**Ditemukan {len(candidates)} kandidat lokasi — pilih yang paling sesuai:**")
            picked_idx = st.radio(
                t("Pilih lokasi yang sesuai", "Pick the matching location"), options=list(range(len(candidates))),
                format_func=lambda i: labels[i], key="search_candidate_pick",
                label_visibility="collapsed",
            )
            picked = candidates[picked_idx]
            st.session_state.pin_lat, st.session_state.pin_lon = picked["lat"], picked["lon"]
            st.session_state.search_result_label = picked["display_name"]
            st.session_state.map_center = [picked["lat"], picked["lon"]]
            # Simpan juga sebagai place hint (lihat penjelasan di mode "Pinpoint
            # on Map" di atas) - kalau kandidat yang dipilih berasal dari Google
            # Places, nama tempatnya (mis. "Perumahan Palem Azna Residence") bisa
            # dipakai lagi saat "Isi Otomatis Alamat" ditekan, bukan cuma hasil
            # reverse geocoding Nominatim yang sering tidak tahu nama kompleks
            # perumahan kecil.
            st.session_state.pin_place_hint = {
                "label": picked["display_name"],
                "lat": picked["lat"], "lon": picked["lon"],
                "source": st.session_state.get("search_candidates_source", "nominatim"),
            }
            if st.session_state.lang == "en":
                st.success(
                    f"📍 Location selected: {picked['display_name']} "
                    f"(Latitude {picked['lat']:.6f}, Longitude {picked['lon']:.6f})"
                )
            else:
                st.success(
                    f"📍 Lokasi dipilih: {picked['display_name']} "
                    f"(Latitude {picked['lat']:.6f}, Longitude {picked['lon']:.6f})"
                )

        lat, lon = st.session_state.pin_lat, st.session_state.pin_lon
        if not candidates and (not lat or not lon):
            st.info(t(
                "Ketik alamat lalu klik \"🔍 Cari\" untuk menemukan koordinat. Kalau alamat "
                "tidak ditemukan (sering terjadi untuk nama kompleks perumahan kecil yang "
                "belum ada di OpenStreetMap), coba alamat yang lebih singkat/umum (mis. "
                "hanya nama kelurahan/kecamatan), isi Serper API Key di sidebar untuk "
                "pencarian setara Google Maps, atau langsung pindah ke mode \"Pinpoint on "
                "Map\" - klik titik yang tepat di peta, lalu tekan tombol \"📝 Isi Otomatis "
                "Alamat\" di bawah form ini.",
                "Type an address then click \"🔍 Search\" to find coordinates. If the address "
                "isn't found (common for small housing-complex names not yet in "
                "OpenStreetMap), try a shorter/more general address (e.g. just the village/"
                "sub-district name), add a Serper API Key in the sidebar for Google-Maps-"
                "grade search, or switch to \"Pinpoint on Map\" mode - click the exact point "
                "on the map, then press the \"📝 Auto-fill Address\" button below this form."
            ))

    elif lokasi_mode == "Pinpoint on Map":
        try:
            import folium
            from streamlit_folium import st_folium
            _folium_ok = True
        except ImportError:
            _folium_ok = False

        if not _folium_ok:
            st.warning(t(
                "Fitur peta interaktif butuh paket `folium` dan `streamlit-folium` "
                "(sudah ditambahkan di requirements.txt - jalankan `pip install -r "
                "requirements.txt` lalu restart app). Untuk saat ini, gunakan mode "
                "\"Search Address\" atau \"Manual Latitude & Longitude\" sebagai alternatif.",
                "The interactive map feature needs the `folium` and `streamlit-folium` "
                "packages (already added to requirements.txt - run `pip install -r "
                "requirements.txt` then restart the app). For now, use \"Search Address\" "
                "or \"Manual Latitude & Longitude\" mode instead."
            ))
        else:
            st.caption(t(
                "Seperti pinpoint lokasi di Gojek/Grab/Shopee: cari area (opsional) untuk "
                "memindahkan peta, lalu klik titik yang tepat di peta untuk menandai lokasi "
                "properti. Setelah itu, pakai tombol \"📝 Isi Otomatis Alamat\" di bawah form "
                "ini untuk mengisi Alamat, Kecamatan, Kabupaten/Kota, dan Provinsi otomatis "
                "dari titik itu (tetap bisa diedit manual kalau kurang presisi).",
                "Just like pinpointing a location in Gojek/Grab/Shopee: search for an area "
                "(optional) to move the map, then click the exact point on the map to mark "
                "the property location. Afterward, use the \"📝 Auto-fill Address\" button "
                "below this form to fill in the Address, Sub-district, City/Regency, and "
                "Province automatically from that point (still editable manually if not "
                "precise enough)."
            ))
            rc1, rc2 = st.columns([3, 1])
            with rc1:
                recenter_query = st.text_input(
                    t("Cari lokasi untuk memindahkan peta (opsional)", "Search for a location to move the map (optional)"), value="",
                    key="map_recenter_query", placeholder=t("mis. Ciracas, Jakarta Timur", "e.g. Ciracas, East Jakarta"),
                )
            with rc2:
                st.write("")
                st.write("")
                do_recenter = st.button(t("🔍 Cari & Pindah Peta", "🔍 Search & Move Map"), use_container_width=True, key="btn_recenter_map")
            if do_recenter:
                if not recenter_query.strip():
                    st.error(t("Isi dulu lokasi yang ingin dicari.", "Please enter a location to search for."))
                else:
                    context_parts = [p.strip() for p in [kecamatan, kabkota, provinsi] if p and p.strip()]
                    enriched_recenter = recenter_query.strip()
                    missing_ctx = [p for p in context_parts if p.lower() not in enriched_recenter.lower()]
                    if missing_ctx:
                        enriched_recenter = f"{enriched_recenter}, {', '.join(missing_ctx)}"
                    with st.spinner(t("Mencari lokasi...", "Searching for location...")):
                        ok_geo, geo_candidates, geo_source = geocode_search_with_places_fallback(
                            enriched_recenter, serper=serper, limit=1
                        )
                    geo = geo_candidates[0] if ok_geo and geo_candidates else geo_candidates
                    if ok_geo and geo_candidates:
                        st.session_state.map_center = [geo["lat"], geo["lon"]]
                        st.session_state.map_zoom = 17
                        # Simpan hasil pencarian ini sebagai "petunjuk lokasi" (place
                        # hint) - kalau appraiser lalu klik titik di peta yang DEKAT
                        # dengan koordinat hasil pencarian ini, tombol "Isi Otomatis
                        # Alamat" di bawah bisa memakai nama tempat (mis. "Perumahan
                        # Palem Azna Residence") dari sini, BUKAN cuma reverse
                        # geocoding Nominatim yang sering kosong untuk nama kompleks
                        # perumahan kecil (hasilnya jadi cuma nama kelurahan generik,
                        # mis. "Cibubur" saja, tanpa nama kompleksnya sama sekali).
                        st.session_state.pin_place_hint = {
                            "label": geo.get("display_name") or recenter_query.strip(),
                            "lat": geo["lat"], "lon": geo["lon"],
                            "source": geo_source,
                        }
                        if st.session_state.lang == "en":
                            st.success(f"🗺️ Map moved to: {geo.get('display_name', recenter_query)} "
                                        "— click the exact point on the map to mark the property location.")
                        else:
                            st.success(f"🗺️ Peta dipindah ke: {geo.get('display_name', recenter_query)} "
                                        "— klik titik yang tepat di peta untuk menandai lokasi properti.")
                    else:
                        if st.session_state.lang == "en":
                            st.error(f"Location not found: {geo}")
                        else:
                            st.error(f"Lokasi tidak ditemukan: {geo}")

            default_center = st.session_state.get("map_center") or [
                st.session_state.pin_lat or -6.2088, st.session_state.pin_lon or 106.8456,
            ]
            default_zoom = st.session_state.get("map_zoom", 16)
            fmap = folium.Map(location=default_center, zoom_start=default_zoom)
            if st.session_state.pin_lat and st.session_state.pin_lon:
                folium.Marker(
                    [st.session_state.pin_lat, st.session_state.pin_lon],
                    tooltip=t("Lokasi Terpilih", "Selected Location") + f" ({st.session_state.pin_lat:.6f}, {st.session_state.pin_lon:.6f})",
                    icon=folium.Icon(color="red", icon="home", prefix="fa"),
                ).add_to(fmap)
            map_data = st_folium(
                fmap, height=420, use_container_width=True, key="pinpoint_map",
                returned_objects=["last_clicked"],
            )

            if map_data and map_data.get("last_clicked"):
                clicked_lat = map_data["last_clicked"]["lat"]
                clicked_lon = map_data["last_clicked"]["lng"]
                if (round(clicked_lat, 8), round(clicked_lon, 8)) != (
                    round(st.session_state.pin_lat, 8) if st.session_state.pin_lat else None,
                    round(st.session_state.pin_lon, 8) if st.session_state.pin_lon else None,
                ):
                    st.session_state.pin_lat = clicked_lat
                    st.session_state.pin_lon = clicked_lon
                    st.rerun()

            lat, lon = st.session_state.pin_lat, st.session_state.pin_lon
            if lat and lon:
                if st.session_state.lang == "en":
                    st.success(f"📍 Point Selected on Map — Latitude: {lat:.6f} | Longitude: {lon:.6f} "
                               "— use the \"Auto-fill Address\" button below to fill in the address fields.")
                else:
                    st.success(f"📍 Titik Dipilih di Peta — Latitude: {lat:.6f} | Longitude: {lon:.6f} "
                               "— pakai tombol \"Isi Otomatis Alamat\" di bawah untuk mengisi field alamat.")
            else:
                st.info(t(
                    "Belum ada titik dipilih — klik langsung di peta di atas untuk menandai "
                    "lokasi properti (marker merah akan muncul di titik yang diklik).",
                    "No point selected yet — click directly on the map above to mark the "
                    "property location (a red marker will appear at the clicked point)."
                ))

    else:  # Manual Latitude & Longitude
        c1, c2 = st.columns(2)
        with c1:
            lat = st.number_input(t("Lintang (Latitude)", "Latitude"), format="%.6f", value=float(lat or 0.0))
        with c2:
            lon = st.number_input(t("Bujur (Longitude)", "Longitude"), format="%.6f", value=float(lon or 0.0))
        st.session_state.pin_lat, st.session_state.pin_lon = lat, lon

    lat, lon = st.session_state.pin_lat, st.session_state.pin_lon
    if lat and lon:
        if st.session_state.lang == "en":
            st.markdown(f"📍 **Property Location Coordinates (saved):** Latitude `{lat:.6f}` | Longitude `{lon:.6f}`")
        else:
            st.markdown(f"📍 **Koordinat Lokasi Properti (tersimpan):** Latitude `{lat:.6f}` | Longitude `{lon:.6f}`")
    else:
        st.caption(t("📍 Koordinat Lokasi Properti: belum dipilih.", "📍 Property Location Coordinates: not yet selected."))

    st.subheader(t("Basic Property Information", "Basic Property Information"))
    kategori = st.radio(
        t("Kategori Properti", "Property Category"),
        [t("Tidak Ditentukan / Umum", "Not Specified / General"), "Subsidy", t("Komersial", "Commercial")],
        horizontal=True, index=0,
        help=t(
            "Default \"Tidak Ditentukan / Umum\" untuk properti yang bukan bagian dari skema "
            "KPR Subsidi maupun kategori Komersial khusus - appraiser tinggal ganti kalau memang "
            "relevan.",
            "Default \"Not Specified / General\" for properties that aren't part of a "
            "subsidized mortgage scheme or a special Commercial category - the appraiser can "
            "change it if relevant."
        ),
    )

    # Tombol UNIVERSAL untuk isi otomatis Provinsi/Kabupaten-Kota/Kecamatan/
    # Alamat di bawah dari koordinat lokasi yang sudah didapat di atas -
    # jalan untuk ketiga mode input lokasi (Search Address, Pinpoint on Map,
    # Manual Latitude & Longitude) karena semuanya berujung ke pin_lat/lon
    # yang sama di session_state. Ditaruh di SINI (dalam Basic Property
    # Information, tepat di atas field Provinsi/Kabkota/Kecamatan/Alamat)
    # supaya jelas kelihatan field mana yang bakal keisi otomatis - bukan
    # cuma "tombol lokasi" yang terpisah jauh di atas.
    lat, lon = st.session_state.pin_lat, st.session_state.pin_lon
    if lat and lon:
        # Dihitung DI SINI (sebelum tombol dirender) supaya tombolnya sendiri
        # bisa di-disable begitu titik saat ini SUDAH pernah di-autofill -
        # mencegah appraiser menekan tombol yang sama berulang kali padahal
        # tidak ada perubahan (request Nominatim baru pun jadi tidak perlu).
        # Begitu titiknya berubah (klik ulang di peta / cari alamat baru /
        # ganti manual lat-lon), _already_autofilled jadi False lagi otomatis
        # karena koordinatnya sudah beda dari autofill_coords yang tersimpan.
        _already_autofilled = (
            st.session_state.get("autofill_coords") == (round(lat, 6), round(lon, 6))
        )
        af1, af2 = st.columns([1, 2])
        with af1:
            do_autofill_addr = st.button(
                t("📝 Isi Otomatis (Provinsi/Kabkota/Kecamatan/Alamat)",
                  "📝 Auto-fill (Province/Regency/Sub-district/Address)"),
                type="primary", use_container_width=True, key="btn_autofill_address",
                disabled=_already_autofilled,
            )
        with af2:
            if _already_autofilled:
                st.caption(t(
                    f"Sudah diisi otomatis untuk titik ini (Latitude {lat:.6f}, "
                    f"Longitude {lon:.6f}). Tombol aktif lagi kalau titik lokasinya "
                    f"diganti (klik ulang di peta / cari alamat baru / ubah lat-lon "
                    f"manual).",
                    f"Already auto-filled for this point (Latitude {lat:.6f}, "
                    f"Longitude {lon:.6f}). The button re-enables once the location "
                    f"point changes (re-click the map / search a new address / edit "
                    f"lat-lon manually)."
                ))
            else:
                st.caption(t(
                    f"Isi field Provinsi, Kabupaten/Kota, Kecamatan & Alamat di bawah "
                    f"otomatis dari koordinat lokasi yang sudah dipilih di atas "
                    f"(Latitude {lat:.6f}, Longitude {lon:.6f}). Tetap bisa diedit manual "
                    f"sesudahnya.",
                    f"Fills the Province, City/Regency, Sub-district & Address fields below "
                    f"automatically from the location coordinates selected above "
                    f"(Latitude {lat:.6f}, Longitude {lon:.6f}). Still editable manually "
                    f"afterward."
                ))

        if do_autofill_addr:
            with st.spinner(t("Mengambil alamat dari koordinat...", "Fetching address from coordinates...")):
                ok_rev, rev = reverse_geocode(lat, lon, lang=("en" if st.session_state.lang == "en" else "id"))
            if ok_rev:
                # Nominatim/OSM sering tidak tahu nama kompleks perumahan kecil
                # (mis. "Perumahan Palem Azna Residence") - hasil reverse geocoding
                # bisa jatuh ke cuma nama kelurahan/kecamatan generik (mis. cuma
                # "Cibubur"), padahal appraiser BARU SAJA menemukan nama tempat itu
                # persis lewat pencarian (Search Address / recenter peta) yang
                # dibantu Google Places. Kalau petunjuk (place hint) dari pencarian
                # itu masih ada, sumbernya Google Places, DAN titiknya dekat (<=300m)
                # dengan koordinat yang di-autofill sekarang, pakai nama tempat itu
                # untuk field Alamat - jauh lebih spesifik daripada hasil Nominatim.
                alamat_final = rev["alamat"]
                hint = st.session_state.get("pin_place_hint")
                if hint and hint.get("source") == "google_places":
                    jarak_hint_km = calc.haversine_km(hint["lat"], hint["lon"], lat, lon)
                    if jarak_hint_km <= 0.3:
                        alamat_final = hint["label"]
                # Isi langsung ke session_state key milik widget-widget di bawah
                # (step1_alamat/kecamatan/kabkota/provinsi) SEBELUM widget itu
                # dirender - Streamlit mengizinkan ini karena baris ini dieksekusi
                # lebih dulu daripada widget-widget tsb pada run yang sama.
                st.session_state.step1_alamat = alamat_final
                if rev["kecamatan"]:
                    st.session_state.step1_kecamatan = rev["kecamatan"]
                if rev["kabkota"]:
                    st.session_state.step1_kabkota = rev["kabkota"]
                if rev["provinsi"]:
                    st.session_state.step1_provinsi = rev["provinsi"]
                st.session_state.autofill_address_label = alamat_final
                # Dicatat koordinat mana yang dipakai untuk auto-fill ini, supaya kalau
                # nanti titiknya berubah lagi (klik ulang di peta / cari ulang / ganti
                # manual lat-lon) tanpa menekan tombol ini lagi, banner konfirmasi di
                # bawah otomatis TIDAK ditampilkan lagi (mencegah info yang sudah basi/
                # tidak sesuai titik yang aktif sekarang).
                st.session_state.autofill_coords = (round(lat, 6), round(lon, 6))
                st.rerun()
            else:
                st.session_state.autofill_address_label = None
                if st.session_state.lang == "en":
                    st.error(f"Couldn't auto-fill the address for these coordinates — {rev} "
                              "Please fill in the fields manually below.")
                else:
                    st.error(f"Gagal mengisi alamat otomatis untuk koordinat ini — {rev} "
                              "Silakan isi field secara manual di bawah.")

        if st.session_state.get("autofill_address_label") and _already_autofilled:
            if st.session_state.lang == "en":
                st.success(f"✅ Fields below auto-filled from: {st.session_state.autofill_address_label} "
                            "— still editable if not precise enough.")
            else:
                st.success(f"✅ Field di bawah terisi otomatis dari: {st.session_state.autofill_address_label} "
                            "— tetap bisa diedit kalau kurang presisi.")
    else:
        st.caption(t(
            "📝 Pilih lokasi properti dulu di bagian \"Lokasi Properti\" di atas untuk bisa "
            "mengisi Provinsi/Kabkota/Kecamatan/Alamat secara otomatis.",
            "📝 Select the property location in the \"Property Location\" section above "
            "first to auto-fill Province/Regency/Sub-district/Address."
        ))

    col1, col2, col3 = st.columns(3)
    with col1:
        provinsi = st.text_input(t("Provinsi", "Province"), key="step1_provinsi")
    with col2:
        kabkota = st.text_input(t("Kabupaten / Kota", "Regency / City"), key="step1_kabkota")
    with col3:
        kecamatan = st.text_input(t("Kecamatan", "Sub-district"), key="step1_kecamatan")

    alamat = st.text_area(t("Alamat Properti", "Property Address"), key="step1_alamat")

    st.subheader(t("Property Details", "Property Details"))
    c1, c2, c3 = st.columns(3)
    with c1:
        luas_tanah = st.number_input(t("Luas Tanah / LT (m²)", "Land Area / LT (m²)"), min_value=0.0, step=1.0)
    with c2:
        luas_bangunan = st.number_input(t("Luas Bangunan / LB (m²)", "Building Area / LB (m²)"), min_value=0.0, step=1.0)
    with c3:
        tahun_bangun = st.number_input(
            t("Tahun Bangun (opsional)", "Year Built (optional)"), min_value=1900, max_value=2100,
            value=None, step=1, placeholder=t("Kosongkan jika tidak diketahui", "Leave blank if unknown"),
            help=t(
                "Opsional - kosongkan kalau tahun bangun tidak diketahui atau tidak relevan "
                "(mis. tanah kosong). Kalau dikosongkan, umur bangunan di Step 3 perlu diisi "
                "manual oleh appraiser.",
                "Optional - leave blank if the year built is unknown or not relevant (e.g. "
                "vacant land). If left blank, the building age in Step 3 needs to be filled "
                "in manually by the appraiser."
            ),
        )

    _status_sertifikat_options = ["SHM", "SHGB", "SHMASRS", "Girik", "Lainnya"]
    _status_sertifikat_display = {"Lainnya": t("Lainnya", "Other")}
    status_sertifikat = st.selectbox(
        t("Status Sertifikat", "Certificate Status"), _status_sertifikat_options,
        index=_status_sertifikat_options.index("Lainnya"),
        format_func=lambda o: _status_sertifikat_display.get(o, o),
    )

    st.subheader(t("Harga (untuk Perbandingan)", "Price (for Comparison)"))
    harga_pengajuan = st.number_input(
        t("Harga yang Diajukan / Harga Penawaran Pemilik (Rp)", "Proposed Price / Owner's Asking Price (Rp)"), min_value=0.0, step=1_000_000.0,
        value=0.0,
        help=t(
            "Opsional - dipakai untuk membandingkan hasil appraisal sistem (Nilai Pasar Akhir & "
            "Nilai Bangunan) terhadap harga yang diajukan pemilik/pemohon. Perbandingan lengkap "
            "akan ditampilkan di Step 11 (Hasil Appraisal: Perbandingan Harga).",
            "Optional - used to compare the system's appraisal result (Final Market Value & "
            "Building Value) against the price proposed by the owner/applicant. A full "
            "comparison is shown in Step 11 (Appraisal Result: Price Comparison)."
        ),
    )

    st.subheader(t("Optional", "Optional"))
    c1, c2 = st.columns(2)
    with c1:
        njop_tanah = st.number_input(t("NJOP Tanah (Rp)", "NJOP Land (Rp)"), min_value=0.0, step=1000.0, value=0.0)
    with c2:
        njop_bangunan = st.number_input(t("NJOP Bangunan (Rp)", "NJOP Building (Rp)"), min_value=0.0, step=1000.0, value=0.0)

    if st.button("Continue →", type="primary"):
        if not alamat or luas_tanah <= 0 or luas_bangunan <= 0:
            st.error(t("Mohon lengkapi minimal Alamat, Luas Tanah, dan Luas Bangunan.",
                       "Please fill in at least the Address, Land Area, and Building Area."))
        else:
            st.session_state.data = {
                "kategori": kategori,
                "provinsi": provinsi,
                "kabkota": kabkota,
                "kecamatan": kecamatan,
                "alamat": alamat,
                "lokasi_mode": lokasi_mode,
                "lat": lat,
                "lon": lon,
                "luas_tanah": luas_tanah,
                "luas_bangunan": luas_bangunan,
                "tahun_bangun": tahun_bangun,
                "status_sertifikat": status_sertifikat,
                "harga_pengajuan": harga_pengajuan or None,
                "njop_tanah": njop_tanah or None,
                "njop_bangunan": njop_bangunan or None,
            }
            goto(2)

# ===========================================================================
# STEP 2 - Nilai Tanah (Bhumi ZNT)
# ===========================================================================
elif st.session_state.step == 2:
    st.header(t("Step 2 — Perhitungan Nilai Tanah (Bhumi ZNT)", "Step 2 — Land Value Calculation (Bhumi ZNT)"))
    d = st.session_state.data

    if not st.session_state.znt_result:
        with st.status(t("Mengambil data Zona Nilai Tanah...", "Fetching Land Value Zone data..."), expanded=True) as status:
            log_box = st.empty()
            lines = []

            def push(msg):
                lines.append(msg)
                log_box.markdown("\n".join(f"- {l}" for l in lines))

            log, result = run_znt_agent(
                d["alamat"], d["provinsi"], d["kabkota"], d["kecamatan"],
                d.get("lat"), d.get("lon"),
                serper, groq, gemini, log_callback=push,
                lang=st.session_state.lang,
            )
            status.update(label=t("Selesai", "Done"), state="complete")
        st.session_state.znt_result = result
        # simpan koordinat yang berhasil ditemukan (dari input manual atau geocoding)
        if result.get("lat") and result.get("lon"):
            st.session_state.data["lat"] = result["lat"]
            st.session_state.data["lon"] = result["lon"]

    result = st.session_state.znt_result
    _conf = str(result.get("confidence_level", "")).lower()
    is_official = "resmi" in _conf or "official" in _conf
    if is_official:
        st.success(t(
            f"✅ Data resmi dari Bhumi ATR/BPN — {result.get('confidence_level')}",
            f"✅ Official data from Bhumi ATR/BPN — {result.get('confidence_level')}",
        ))
    else:
        st.info(t(
            "Hasil di bawah adalah estimasi berbasis pencarian web + LLM (data resmi Bhumi ATR/BPN "
            "tidak berhasil diambil). Selalu periksa/koreksi angka ZNT secara manual sebelum lanjut.",
            "The result below is a web search + LLM based estimate (official Bhumi ATR/BPN data "
            "could not be retrieved). Always check/correct the ZNT figure manually before continuing."
        ))
    if result.get("source_notes"):
        st.caption(result["source_notes"])

    if st.button(t("🔄 Ambil ulang data ZNT", "🔄 Re-fetch ZNT data")):
        st.session_state.znt_result = {}
        st.rerun()

    st.caption(t(
        "Kode Zona, Tanggal Data, dan Confidence Level berasal langsung dari agent/Bhumi ATR-BPN "
        "dan tidak dapat diubah manual. Hanya Zona Nilai Tanah dan Harga ZNT/m² yang bisa "
        "disesuaikan appraiser (mis. jika ada koreksi lapangan).",
        "Zone Code, Data Date, and Confidence Level come directly from the agent/Bhumi "
        "ATR-BPN and cannot be edited manually. Only the Land Value Zone and ZNT Price/m² can "
        "be adjusted by the appraiser (e.g. for a field correction)."
    ))
    c1, c2 = st.columns(2)
    with c1:
        st.metric(t("Kode Zona", "Zone Code"), str(result.get("kode_zona", "-")))
        zona_nilai_tanah = st.text_input(t("Zona Nilai Tanah", "Land Value Zone"), value=str(result.get("zona_nilai_tanah", "-")))
        harga_znt = st.number_input(
            t("Harga ZNT / m² (Rp)", "ZNT Price / m² (Rp)"), min_value=0.0, step=1000.0,
            value=float(result.get("harga_znt_per_m2", 0) or 0),
        )
    with c2:
        st.metric(t("Tanggal Data", "Data Date"), str(result.get("tanggal_data", "-")))
        st.metric("Confidence Level", str(result.get("confidence_level", "-")))
        st.metric(t("Luas Tanah", "Land Area"), f"{d['luas_tanah']:.0f} m²")

    nilai_tanah = calc.hitung_nilai_tanah(harga_znt, d["luas_tanah"])
    st.metric(t("Total Nilai Tanah", "Total Land Value"), fmt_rp(nilai_tanah))
    st.caption(t("Formula: Nilai Tanah = ZNT per m² × Luas Tanah", "Formula: Land Value = ZNT per m² × Land Area"))

    col_back, col_next = st.columns(2)
    with col_back:
        if st.button("← Back"):
            goto(1)
    with col_next:
        if st.button("Continue →", type="primary"):
            st.session_state.znt_result = {
                "kode_zona": result.get("kode_zona", "-"),
                "zona_nilai_tanah": zona_nilai_tanah,
                "harga_znt_per_m2": harga_znt,
                "tanggal_data": result.get("tanggal_data", "-"),
                "confidence_level": result.get("confidence_level", "-"),
                "nilai_tanah": nilai_tanah,
                "source_notes": result.get("source_notes", ""),
                "lat": result.get("lat"),
                "lon": result.get("lon"),
            }
            goto(3)

# ===========================================================================
# STEP 3 - Nilai Bangunan (Cost Approach)
# ===========================================================================
elif st.session_state.step == 3:
    st.header(t("Step 3 — Perhitungan Nilai Bangunan (Cost Approach)", "Step 3 — Building Value Calculation (Cost Approach)"))
    d = st.session_state.data

    # -----------------------------------------------------------------
    # AI OCR: estimasi klasifikasi & umur bangunan dari foto (opsional)
    # -----------------------------------------------------------------
    with st.expander(t("🤖 AI: Estimasi Klasifikasi & Umur Bangunan dari Foto (opsional)",
                        "🤖 AI: Estimate Building Classification & Age from Photos (optional)")):
        st.caption(t(
            "Unggah foto rumah (bisa lebih dari satu - mis. tampak depan, samping, "
            "belakang) - AI (Gemini vision) akan memperkirakan klasifikasi bangunan "
            "(Sederhana/Menengah/Mewah) dan umur bangunan dalam tahun berdasarkan "
            "semua foto sekaligus. Ini HANYA alat bantu pengisian awal - appraiser "
            "tetap wajib memverifikasi langsung di lapangan. Butuh Gemini API key.",
            "Upload house photos (more than one is fine - e.g. front, side, back) - AI "
            "(Gemini vision) will estimate the building classification "
            "(Simple/Medium/Luxury) and building age in years from all photos at once. "
            "This is ONLY an initial-fill-in aid - the appraiser must still verify "
            "directly in the field. Requires a Gemini API key."
        ))
        foto_rumah_list = st.file_uploader(
            t("Foto Rumah", "House Photos"), type=["jpg", "jpeg", "png"], key="foto_rumah_ocr",
            accept_multiple_files=True,
        )
        if foto_rumah_list:
            st.image(
                [f.getvalue() for f in foto_rumah_list],
                caption=[f.name for f in foto_rumah_list],
                width=140,
            )
        if foto_rumah_list and st.button(t("🔍 Analisa Foto", "🔍 Analyze Photos")):
            with st.spinner(t(f"Menganalisa {len(foto_rumah_list)} foto...", f"Analyzing {len(foto_rumah_list)} photos...")):
                images = [(f.getvalue(), f.type or "image/jpeg") for f in foto_rumah_list]
                ok_ocr, ocr_result = run_building_age_ocr_agent(images, gemini)
            if ok_ocr:
                st.session_state.building_ocr_result = ocr_result
            else:
                st.error(ocr_result)

        ocr_result = st.session_state.get("building_ocr_result")
        if ocr_result:
            oc1, oc2, oc3 = st.columns(3)
            oc1.metric(t("Klasifikasi (AI)", "Classification (AI)"), ocr_result.get("klasifikasi_bangunan", "-"))
            oc2.metric(t("Estimasi Umur (AI)", "Estimated Age (AI)"), t(f"{ocr_result.get('estimasi_umur_tahun', '-')} tahun", f"{ocr_result.get('estimasi_umur_tahun', '-')} years"))
            oc3.metric("Confidence", ocr_result.get("confidence", "-"))
            if ocr_result.get("alasan_singkat"):
                st.caption(f"ℹ️ {ocr_result['alasan_singkat']}")
            if st.button(t("✓ Gunakan Hasil AI untuk Klasifikasi & Umur", "✓ Use AI Result for Classification & Age")):
                st.session_state.bangunan_result["klasifikasi"] = ocr_result.get("klasifikasi_bangunan", "Menengah")
                st.session_state.bangunan_result["umur_bangunan_override"] = ocr_result.get("estimasi_umur_tahun")
                st.session_state.ai_klasifikasi_umur_applied = {
                    "klasifikasi": ocr_result.get("klasifikasi_bangunan", "Menengah"),
                    "umur": ocr_result.get("estimasi_umur_tahun"),
                }
                st.session_state.show_ai_applied_popup = True
                st.rerun()

    if st.session_state.get("show_ai_applied_popup"):
        @st.dialog(t("✅ Hasil AI Diterapkan", "✅ AI Result Applied"))
        def _ai_applied_popup():
            info = st.session_state.get("ai_klasifikasi_umur_applied", {})
            if st.session_state.lang == "en":
                st.write(f"Building Classification auto-filled to: **{info.get('klasifikasi', '-')}**")
                st.write(f"Building Age auto-filled to: **{info.get('umur', '-')} years**")
            else:
                st.write(f"Klasifikasi Bangunan diisi otomatis ke: **{info.get('klasifikasi', '-')}**")
                st.write(f"Umur Bangunan diisi otomatis ke: **{info.get('umur', '-')} tahun**")
            st.caption(t("Ini hanya alat bantu pengisian awal — appraiser tetap wajib "
                       "memverifikasi langsung di lapangan.",
                       "This is only an initial-fill-in aid — the appraiser must still "
                       "verify directly in the field."))
            if st.button(t("OK, Mengerti", "OK, Understood"), type="primary"):
                st.session_state.show_ai_applied_popup = False
                st.rerun()

        _ai_applied_popup()

    klasifikasi = st.selectbox(
        t("Klasifikasi Bangunan", "Building Classification"), ["Sederhana", "Menengah", "Mewah"],
        index=["Sederhana", "Menengah", "Mewah"].index(
            st.session_state.bangunan_result.get("klasifikasi", "Menengah")
        ) if st.session_state.bangunan_result else 1,
        format_func=lambda k: t(k, {"Sederhana": "Simple", "Menengah": "Medium", "Mewah": "Luxury"}[k]),
    )

    with st.expander(t("📋 Tabel Klasifikasi Bangunan (acuan SOP)", "📋 Building Classification Table (SOP reference)")):
        _kelas_en = {"Sederhana": "Simple", "Menengah": "Medium", "Mewah": "Luxury"}
        klasifikasi_table_rows = [
            {
                t("Kelas", "Class"): t(kelas, _kelas_en.get(kelas, kelas)),
                t("Kriteria", "Criteria"): v["kriteria"],
                t("Umur Ekonomis", "Economic Life"): t(f"{v['umur_ekonomis_tahun']} thn", f"{v['umur_ekonomis_tahun']} yrs"),
                "Rate/thn": f"{v['rate_per_tahun']*100:.2f}%",
                "BRB/m²": fmt_rp(v["brb_per_m2"]),
            }
            for kelas, v in calc.KLASIFIKASI_BANGUNAN_TABLE.items()
        ]
        st.dataframe(klasifikasi_table_rows, use_container_width=True, hide_index=True)

    klasifikasi_default = calc.KLASIFIKASI_BANGUNAN_TABLE[klasifikasi]
    default_brb_per_m2 = klasifikasi_default["brb_per_m2"]

    _tahun_bangun = d.get("tahun_bangun")
    default_umur = st.session_state.bangunan_result.get("umur_bangunan_override")
    if default_umur is None:
        default_umur = (
            max(0, datetime.date.today().year - int(_tahun_bangun)) if _tahun_bangun else 0
        )
    if not _tahun_bangun:
        st.caption(t("ℹ️ Tahun bangun tidak diisi di Step 1 - umur bangunan di bawah perlu "
                    "dicek/diisi manual oleh appraiser.",
                    "ℹ️ Year built was not filled in at Step 1 - the building age below needs "
                    "to be checked/filled in manually by the appraiser."))

    c1, c2 = st.columns(2)
    with c1:
        brb_per_m2 = st.number_input(
            t("Biaya Reproduksi Baru / m² (Rp)", "New Reproduction Cost / m² (Rp)"), min_value=0.0, step=50_000.0,
            value=float(st.session_state.bangunan_result.get("brb_per_m2", 5_000_000.0)),
            help=t(
                f"Default Rp 5.000.000/m². Tabel klasifikasi di atas ({fmt_rp(default_brb_per_m2)} "
                f"untuk '{klasifikasi}') hanya acuan - sesuaikan manual dengan survei pasar/kontraktor/"
                "data BTB terbaru bila berbeda.",
                f"Default Rp 5,000,000/m². The classification table above ({fmt_rp(default_brb_per_m2)} "
                f"for '{klasifikasi}') is a reference only - adjust manually with a market/contractor "
                "survey or the latest BTB data if different."
            ),
        )
        umur_bangunan = st.number_input(
            t("Umur Bangunan (tahun)", "Building Age (years)"), min_value=0, value=int(default_umur),
        )
    with c2:
        umur_ekonomis = st.number_input(
            t("Umur Ekonomis (tahun)", "Economic Life (years)"), min_value=1.0,
            value=float(st.session_state.bangunan_result.get(
                "umur_ekonomis", klasifikasi_default["umur_ekonomis_tahun"]
            )),
        )
    st.caption(t("ℹ️ Catatan: Penyusutan (depresiasi) bangunan dibatasi maksimal 80%.",
                 "ℹ️ Note: Building depreciation is capped at a maximum of 80%."))

    brb = calc.hitung_brb(brb_per_m2 or 0.0, d["luas_bangunan"])

    st.markdown(t("**Metode Penyusutan (Depresiasi)**", "**Depreciation Method**"))
    metode_penyusutan = st.radio(
        t("Metode", "Method"), ["Garis Lurus (Straight Line)", "Persentase Tetap (Declining Balance)"],
        horizontal=True, key="metode_penyusutan",
        format_func=lambda m: t(m, "Straight Line") if m == "Garis Lurus (Straight Line)" else t(m, "Declining Balance"),
    )

    if metode_penyusutan == "Garis Lurus (Straight Line)":
        penyusutan = calc.hitung_penyusutan(brb, umur_bangunan, umur_ekonomis)
        nilai_bangunan = calc.hitung_nilai_bangunan(brb, penyusutan)

        c1, c2, c3 = st.columns(3)
        c1.metric(t("Luas Bangunan", "Building Area"), f"{d['luas_bangunan']:.0f} m²")
        c2.metric(t("Biaya Reproduksi Baru (BRB)", "New Reproduction Cost (BRB)"), fmt_rp(brb))
        c3.metric(t("Penyusutan", "Depreciation"), fmt_rp(penyusutan))
        st.metric(t("Nilai Bangunan", "Building Value"), fmt_rp(nilai_bangunan))
        st.caption(t("Formula: Nilai Bangunan = BRB − Penyusutan (Penyusutan = BRB × (Umur Bangunan / Umur Ekonomis), dibatasi maks 80%)",
                     "Formula: Building Value = BRB − Depreciation (Depreciation = BRB × (Building Age / Economic Life), capped at 80%)"))
        depresiasi_detail = {"metode": "garis_lurus"}
    else:
        st.caption(t(
            "Kalkulator Penyusutan Persentase (Percentage / Declining Balance Depreciation "
            "Calculator): setiap periode nilai disusutkan sebesar persentase TETAP dari nilai "
            "SISA periode sebelumnya (bukan dari nilai awal keseluruhan), sehingga jumlah "
            "penyusutan mengecil tiap periode. Persentase default mengikuti tabel Umur Ekonomis "
            "MAPPI 2023 (Biaya Teknis Bangunan) berdasarkan jenis bangunan - appraiser tetap bisa "
            "menimpa manual jenis bangunan, persentase, maupun periodenya (Tahun atau Bulan).",
            "Percentage / Declining Balance Depreciation Calculator: each period the value is "
            "depreciated by a FIXED percentage of the REMAINING value from the previous period "
            "(not the overall original value), so the depreciation amount shrinks each period. "
            "The default percentage follows the MAPPI 2023 Economic Life table (Building "
            "Technical Cost) based on building type - the appraiser can still manually override "
            "the building type, percentage, or period (Years or Months)."
        ))

        jenis_default = calc.KLASIFIKASI_KE_JENIS_MAPPI.get(klasifikasi, "Rumah Menengah")
        jenis_options = list(calc.MAPPI_UMUR_EKONOMIS_TABLE.keys())
        jenis_bangunan = st.selectbox(
            t("Jenis Bangunan (acuan tabel MAPPI)", "Building Type (MAPPI table reference)"), jenis_options,
            index=jenis_options.index(jenis_default) if jenis_default in jenis_options else 1,
            help=t(f"Otomatis disarankan dari Klasifikasi Bangunan ('{klasifikasi}') di atas - bisa diganti manual.",
                   f"Automatically suggested from the Building Classification ('{klasifikasi}') above - can be changed manually."),
        )
        mappi_default = calc.MAPPI_UMUR_EKONOMIS_TABLE[jenis_bangunan]

        with st.expander(t("📋 Tabel Umur Ekonomis & Penyusutan per Tahun (acuan MAPPI 2023)", "📋 Economic Life & Annual Depreciation Table (MAPPI 2023 reference)")):
            mappi_table_rows = [
                {t("Jenis Bangunan", "Building Type"): jenis, t("Umur Ekonomis (Tahun)", "Economic Life (Years)"): v["umur_ekonomis_tahun"],
                 t("Penyusutan Per Tahun", "Depreciation Per Year"): f"{v['penyusutan_pct_tahun']*100:.2f}%"}
                for jenis, v in calc.MAPPI_UMUR_EKONOMIS_TABLE.items()
            ]
            st.dataframe(mappi_table_rows, use_container_width=True, hide_index=True)
            st.caption(t("Sumber: Biaya Teknis Bangunan (BTB) MAPPI 2023.", "Source: MAPPI 2023 Building Technical Cost (BTB)."))

        dc1, dc2, dc3, dc4 = st.columns(4)
        with dc1:
            asset_value = st.number_input(
                "Asset Value (Rp)", min_value=0.0, step=1_000_000.0, value=float(brb),
                help=t("Default terisi dari Biaya Reproduksi Baru (BRB) di atas, bisa diubah manual.",
                       "Default filled from the New Reproduction Cost (BRB) above, can be changed manually."),
            )
        with dc2:
            persen_penyusutan_input = st.number_input(
                t("Percentage (%) per Tahun", "Percentage (%) per Year"), min_value=0.0, max_value=100.0, step=0.01,
                value=round(mappi_default["penyusutan_pct_tahun"] * 100, 2),
                help=t(
                    f"Default {mappi_default['penyusutan_pct_tahun']*100:.2f}%/tahun sesuai tabel MAPPI "
                    f"untuk '{jenis_bangunan}' (umur ekonomis {mappi_default['umur_ekonomis_tahun']} tahun).",
                    f"Default {mappi_default['penyusutan_pct_tahun']*100:.2f}%/year per the MAPPI table "
                    f"for '{jenis_bangunan}' (economic life {mappi_default['umur_ekonomis_tahun']} years)."
                ),
            )
        with dc3:
            unit_periode = st.radio(t("Satuan Periode", "Period Unit"), ["Tahun", "Bulan"], horizontal=True, key="unit_periode_step3",
                                     format_func=lambda u: t("Tahun", "Years") if u == "Tahun" else t("Bulan", "Months"))
        with dc4:
            default_periode = mappi_default["umur_ekonomis_tahun"] if unit_periode == "Tahun" else mappi_default["umur_ekonomis_tahun"] * 12
            _unit_disp = t("Tahun", "Years") if unit_periode == "Tahun" else t("Bulan", "Months")
            periode_tahun = st.number_input(
                f"{t('Periode', 'Period')} ({_unit_disp})", min_value=1, max_value=600, value=int(default_periode), step=1,
            )

        schedule = calc.hitung_penyusutan_persentase(
            asset_value, persen_penyusutan_input / 100.0, periode_tahun, unit=unit_periode
        )
        penyusutan = round(asset_value - schedule[-1]["balance"], 2) if schedule else 0.0
        nilai_bangunan = schedule[-1]["balance"] if schedule else asset_value

        c1, c2, c3 = st.columns(3)
        c1.metric(t("Luas Bangunan", "Building Area"), f"{d['luas_bangunan']:.0f} m²")
        c2.metric("Asset Value (Awal)" if st.session_state.lang == "id" else "Asset Value (Initial)", fmt_rp(asset_value))
        c3.metric(t("Total Penyusutan", "Total Depreciation"), fmt_rp(penyusutan))
        _unit_disp2 = t("Tahun", "Years") if unit_periode == "Tahun" else t("Bulan", "Months")
        st.metric(t(f"Nilai Bangunan (Balance setelah {periode_tahun} {unit_periode.lower()})",
                    f"Building Value (Balance after {periode_tahun} {_unit_disp2.lower()})"), fmt_rp(nilai_bangunan))
        st.caption("Formula: Depreciation = Beginning Value × Percentage (per periode) | Balance = Beginning Value − Depreciation")

        if schedule:
            with st.expander("📋 Depreciation Schedule"):
                sched_df = [
                    {
                        "Period": row["period_label"],
                        "Beginning Value": fmt_rp(row["beginning_value"]),
                        "Depreciation": fmt_rp(row["depreciation"]),
                        "Balance": fmt_rp(row["balance"]),
                    }
                    for row in schedule
                ]
                st.dataframe(sched_df, use_container_width=True, hide_index=True)

        depresiasi_detail = {
            "metode": "persentase_tetap",
            "jenis_bangunan": jenis_bangunan,
            "asset_value": asset_value,
            "persentase": persen_penyusutan_input / 100.0,
            "periode": periode_tahun,
            "unit": unit_periode,
            "schedule": schedule,
        }

    col_back, col_next = st.columns(2)
    with col_back:
        if st.button("← Back"):
            goto(2)
    with col_next:
        if st.button("Continue →", type="primary"):
            if not brb_per_m2:
                st.error(t("⚠️ Biaya Reproduksi Baru / m² belum diisi - isi dulu sebelum lanjut.",
                           "⚠️ New Reproduction Cost / m² not filled in yet - please fill it in before continuing."))
            else:
                st.session_state.bangunan_result = {
                    "klasifikasi": klasifikasi,
                    "brb_per_m2": brb_per_m2,
                    "brb": brb,
                    "umur_bangunan": umur_bangunan,
                    "umur_ekonomis": umur_ekonomis,
                    "penyusutan": penyusutan,
                    "nilai_bangunan": nilai_bangunan,
                    "depresiasi_detail": depresiasi_detail,
                }
                goto(4)

# ===========================================================================
# STEP 4 - Faktor Pengurang
# ===========================================================================
elif st.session_state.step == 4:
    st.header(t("Step 4 — Analisis Faktor Pengurang", "Step 4 — Reduction Factor Analysis"))
    d = st.session_state.data

    if not st.session_state.auto_flags or st.session_state.get("auto_flags_lang") != st.session_state.lang:
        with st.status(t("Menganalisis lokasi...", "Analyzing location..."), expanded=True) as status:
            log, flags, notes = run_pinpoint_agent(
                d["alamat"], d.get("lat") or 0, d.get("lon") or 0, serper, groq, gemini,
                lang=st.session_state.lang,
            )
            for line in log:
                st.write(line)
            status.update(label=t("Selesai", "Done"), state="complete")
        st.session_state.auto_flags = flags
        st.session_state.auto_notes = notes
        st.session_state.auto_flags_lang = st.session_state.lang

    if not st.session_state.checklist_auto_scores or st.session_state.get("checklist_auto_lang") != st.session_state.lang:
        with st.status(t("Memperkirakan sebagian item checklist...", "Estimating some checklist items..."), expanded=True) as status:
            log2, cl_scores, cl_notes, cl_auto_keys = run_manual_checklist_agent(
                d["alamat"], d.get("kecamatan", ""), d.get("kabkota", ""), d.get("provinsi", ""),
                d.get("status_sertifikat", ""), st.session_state.auto_flags,
                serper, groq, gemini, lang=st.session_state.lang,
            )
            for line in log2:
                st.write(line)
            status.update(label=t("Selesai", "Done"), state="complete")
        st.session_state.checklist_auto_scores = cl_scores
        st.session_state.checklist_auto_notes = cl_notes
        st.session_state.checklist_auto_keys = cl_auto_keys
        st.session_state.checklist_auto_lang = st.session_state.lang

    manual_items = [
        "bentuk_tanah", "kontur_tanah", "posisi_tanah", "kondisi_bangunan",
        "kualitas_konstruksi", "perawatan_bangunan", "legalitas",
        "kondisi_lingkungan", "peruntukan_lahan", "akses_jalan",
    ]

    st.subheader(t("Analisis Otomatis", "Automatic Analysis"))
    flag_labels = {
        "flood_risk": t("Risiko Banjir", "Flood Risk"),
        "sutet": "SUTET",
        "railway": t("Rel Kereta", "Railway"),
        "industry": t("Industri", "Industry"),
        "hospital": t("Rumah Sakit", "Hospital"),
        "school": t("Sekolah", "School"),
        "market": t("Pasar", "Market"),
        "main_road": t("Jalan Utama", "Main Road"),
        "public_facilities": t("Fasilitas Umum", "Public Facilities"),
    }
    PUBLIC_FACILITIES_DEFINITION_EN = (
        "Places of worship, hospitals/community health centers/clinics, public sports "
        "buildings/fields, public libraries, public recreation areas (e.g. playgrounds/zoos), "
        "public transit terminals, schools, markets, cemeteries/columbariums/funeral homes/"
        "etc., and other properties per the definition above."
    )
    flag_help = {
        "public_facilities": t(calc.PUBLIC_FACILITIES_DEFINITION, PUBLIC_FACILITIES_DEFINITION_EN),
    }
    cols = st.columns(3)
    updated_flags = {}
    for i, (key, label) in enumerate(flag_labels.items()):
        with cols[i % 3]:
            val = st.checkbox(
                label, value=bool(st.session_state.auto_flags.get(key, False)), key=f"flag_{key}",
                help=flag_help.get(key),
            )
            note = st.session_state.auto_notes.get(key, "")
            if note:
                st.caption(note)
            updated_flags[key] = val

    if st.button(t("🔄 Ulangi analisis lokasi otomatis", "🔄 Redo automatic location analysis")):
        st.session_state.auto_flags = {}
        st.session_state.auto_notes = {}
        for key in flag_labels:
            st.session_state.pop(f"flag_{key}", None)
        # Checklist manual sebagian diturunkan dari hasil analisis lokasi ini
        # (akses_jalan, kondisi_lingkungan) - reset juga supaya ikut ditarik ulang
        # dari flag lokasi yang baru, bukan flag lokasi lama yang sudah dibuang.
        st.session_state.checklist_auto_scores = {}
        st.session_state.checklist_auto_notes = {}
        st.session_state.checklist_auto_keys = set()
        for key in manual_items:
            st.session_state.pop(f"manual_{key}", None)
        st.rerun()

    st.subheader(t("Manual Checklist", "Manual Checklist"))
    st.caption(t(
        "0 = tidak masalah, 3 = masalah berat. Item bertanda 🤖 sudah diperkirakan "
        "otomatis (silakan koreksi jika perlu); item bertanda 📋 butuh pemeriksaan "
        "lapangan langsung dan tidak diisi otomatis.",
        "0 = no problem, 3 = severe problem. Items marked 🤖 have already been "
        "estimated automatically (please correct if needed); items marked 📋 need a "
        "direct field inspection and are not auto-filled."
    ))
    manual_labels = {
        "bentuk_tanah": "Bentuk Tanah", "kontur_tanah": "Kontur Tanah",
        "posisi_tanah": "Posisi Tanah", "kondisi_bangunan": "Kondisi Bangunan",
        "kualitas_konstruksi": "Kualitas Konstruksi", "perawatan_bangunan": "Perawatan Bangunan",
        "legalitas": "Legalitas", "kondisi_lingkungan": "Kondisi Lingkungan",
        "peruntukan_lahan": "Peruntukan Lahan", "akses_jalan": "Akses Jalan",
    }
    _manual_labels_en = {
        "bentuk_tanah": "Land Shape", "kontur_tanah": "Land Contour",
        "posisi_tanah": "Land Position", "kondisi_bangunan": "Building Condition",
        "kualitas_konstruksi": "Construction Quality", "perawatan_bangunan": "Building Maintenance",
        "legalitas": "Legality", "kondisi_lingkungan": "Environmental Condition",
        "peruntukan_lahan": "Land Use Designation", "akses_jalan": "Road Access",
    }
    manual_labels = {k: t(v, _manual_labels_en[k]) for k, v in manual_labels.items()}
    auto_keys = st.session_state.checklist_auto_keys
    auto_scores = st.session_state.checklist_auto_scores
    auto_notes = st.session_state.checklist_auto_notes

    manual_scores = {}
    cols = st.columns(2)
    for i, key in enumerate(manual_items):
        with cols[i % 2]:
            is_auto = key in auto_keys
            badge = "🤖 Auto" if is_auto else t("📋 Manual", "📋 Manual")
            default_val = int(auto_scores.get(key, 0)) if is_auto else 0
            manual_scores[key] = st.slider(
                f"{manual_labels[key]} ({badge})", 0, 3, default_val, key=f"manual_{key}"
            )
            note = auto_notes.get(key)
            if is_auto and note:
                st.caption(f"🤖 {note}")
            elif not is_auto:
                st.caption(t("📋 Perlu pemeriksaan/foto lapangan - isi manual.", "📋 Needs a field inspection/photo - fill in manually."))

    if st.button(t("🔄 Ulangi estimasi otomatis checklist", "🔄 Redo automatic checklist estimate")):
        st.session_state.checklist_auto_scores = {}
        st.session_state.checklist_auto_notes = {}
        st.session_state.checklist_auto_keys = set()
        for key in manual_items:
            st.session_state.pop(f"manual_{key}", None)
        st.rerun()

    st.subheader(t("⚠️ Faktor Pembatas / Red-Flag Tambahan (SOP)", "⚠️ Additional Restricting Factors / Red-Flags (SOP)"))
    st.caption(t(
        "Kondisi berikut sifatnya MEMBATASI kelayakan properti sebagai agunan, bukan sekadar "
        "mengurangi nilai secara proporsional - appraiser WAJIB memeriksa lapangan/dinas terkait "
        "sebelum mencentang. Kalau salah satu tercentang, properti mungkin perlu dinyatakan tidak "
        "layak sebagai agunan terlepas dari angka Faktor Pengurang di bawah.",
        "The following conditions RESTRICT the property's eligibility as collateral, rather "
        "than just proportionally reducing its value - the appraiser MUST verify in the field/"
        "with the relevant agency before checking. If any is checked, the property may need to "
        "be declared ineligible as collateral regardless of the Reduction Factor figure below."
    ))
    RESTRIKSI_LABELS_EN = {
        "sengketa_hukum": "Land in/related to a dispute that can be legally proven and is "
            "registered with the local court.",
        "tanah_adat": "Customary land / communary land/anchesteral land / crooked land.",
        "rawan_bencana": "Area prone to tidal flooding and/or landslides and/or sloped/hillside/"
            "ravine land.",
        "cagar_lindung": "Nature reserve / cultural heritage site / protected forest / wildlife "
            "sanctuary.",
        "jalur_hijau_fasum": "There are plans for, and/or it has become, a green corridor / "
            "public facility / social facility.",
        "pelebaran_jalan": "There are road-widening plans so the land use is no longer optimal "
            "(highest and best use/HBU principle) per its designation (based on the "
            "collateral's location and/or checks with the local city planning office or "
            "relevant agency).",
        "akses_sempit": "Road width less than 3 meters from the road body, or only an alley, "
            "except for certain marketable areas.",
        "berbatasan_lokasi_berisiko": "Directly bordering and/or part of a family cemetery, "
            "public cemetery, electrical substation (residential areas only), "
            "crematorium, funeral home, railway line, waste dump/landfill, storage of and/or "
            "activities related to Hazardous and Toxic Materials, and/or high-risk "
            "locations such as military training grounds, explosives production/storage sites "
            "- highly flammable sites like oil/gas depots or nuclear reactors.",
    }
    restriksi_flags = {}
    for key, label in calc.RESTRIKSI_LABELS.items():
        restriksi_flags[key] = st.checkbox(
            t(label, RESTRIKSI_LABELS_EN.get(key, label)), value=bool(st.session_state.get("restriksi_flags", {}).get(key, False)),
            key=f"restriksi_{key}",
        )

    if any(restriksi_flags.values()):
        aktif_labels = [t(calc.RESTRIKSI_LABELS[k], RESTRIKSI_LABELS_EN.get(k, calc.RESTRIKSI_LABELS[k])) for k, v in restriksi_flags.items() if v]
        st.error(
            t("🚫 **Perhatian:** properti terindikasi memenuhi kondisi pembatas berikut - "
              "tinjau ulang kelayakannya sebagai agunan sebelum melanjutkan:\n\n",
              "🚫 **Attention:** the property appears to meet the following restricting "
              "conditions - review its eligibility as collateral before continuing:\n\n")
            + "\n".join(f"- {lbl}" for lbl in aktif_labels)
        )

    rule_result = calc.hitung_faktor_pengurang(updated_flags, manual_scores, restriksi_flags)
    STATUS_RISIKO_EMOJI = {"Hijau": "🟢", "Kuning": "🟡", "Oranye": "🟠", "Merah": "🔴"}
    STATUS_RISIKO_EN = {"Hijau": "Green", "Kuning": "Yellow", "Oranye": "Orange", "Merah": "Red"}
    c1, c2, c3 = st.columns(3)
    c1.metric(t("Total Faktor Pengurang", "Total Reduction Factor"), f"{rule_result['total_faktor_pengurang']*100:.2f}%")
    status_label = rule_result["status_risiko"]
    c2.metric(t("Status Risiko", "Risk Status"), f"{STATUS_RISIKO_EMOJI.get(status_label, '')} {t(status_label, STATUS_RISIKO_EN.get(status_label, status_label))}")
    c3.metric("Confidence Level", f"{rule_result['confidence_level']*100:.0f}%")
    st.caption(t("Faktor Pengurang dibatasi maksimum 30% sesuai SOP. "
               "🟢 Hijau <7.5% · 🟡 Kuning 7.5–15% · 🟠 Oranye 15–22.5% · 🔴 Merah ≥22.5%.",
               "Reduction Factor is capped at 30% per SOP. "
               "🟢 Green <7.5% · 🟡 Yellow 7.5–15% · 🟠 Orange 15–22.5% · 🔴 Red ≥22.5%."))

    col_back, col_next = st.columns(2)
    with col_back:
        if st.button("← Back"):
            goto(3)
    with col_next:
        if st.button("Continue →", type="primary"):
            st.session_state.auto_flags = updated_flags
            st.session_state.manual_scores = manual_scores
            st.session_state.restriksi_flags = restriksi_flags
            st.session_state.faktor_pengurang = rule_result
            goto(5)

# ===========================================================================
# STEP 5 - Nilai Pasar Awal
# ===========================================================================
elif st.session_state.step == 5:
    st.header(t("Step 5 — Perhitungan Nilai Pasar Awal", "Step 5 — Initial Market Value Calculation"))

    nilai_tanah = st.session_state.znt_result.get("nilai_tanah", 0)
    nilai_bangunan = st.session_state.bangunan_result.get("nilai_bangunan", 0)
    faktor = st.session_state.faktor_pengurang.get("total_faktor_pengurang", 0)

    nilai_properti = nilai_tanah + nilai_bangunan
    nilai_pasar_awal = calc.hitung_nilai_pasar_awal(nilai_tanah, nilai_bangunan, faktor)
    st.session_state.nilai_pasar_awal = nilai_pasar_awal

    c1, c2 = st.columns(2)
    c1.metric(t("Nilai Tanah", "Land Value"), fmt_rp(nilai_tanah))
    c2.metric(t("Nilai Bangunan", "Building Value"), fmt_rp(nilai_bangunan))
    st.metric(t("Nilai Properti (Tanah + Bangunan)", "Property Value (Land + Building)"), fmt_rp(nilai_properti))
    st.metric(t("Faktor Pengurang", "Reduction Factor"), f"{faktor*100:.2f}%")
    st.metric(t("💰 Nilai Pasar Awal", "💰 Initial Market Value"), fmt_rp(nilai_pasar_awal))
    st.caption(t("Formula: Nilai Pasar Awal = (Nilai Tanah + Nilai Bangunan) × (1 − Faktor Pengurang)",
                 "Formula: Initial Market Value = (Land Value + Building Value) × (1 − Reduction Factor)"))

    col_back, col_next = st.columns(2)
    with col_back:
        if st.button("← Back"):
            goto(4)
    with col_next:
        if st.button("Continue →", type="primary"):
            goto(6)

# ===========================================================================
# STEP 6 - Property Reference AI
# ===========================================================================
elif st.session_state.step == 6:
    st.header("Step 6 — Property Reference AI")
    d = st.session_state.data
    _kb_id_en = {
        "luas di luar toleransi": "area outside tolerance",
        "jarak > radius": "distance > radius",
        "jarak tidak diketahui": "distance unknown",
        "di luar kriteria": "outside criteria",
    }

    SOURCE_COLORS = {
        "Rumah123": "#fb8c00", "99.co": "#1e88e5", "OLX": "#f4511e",
        "Pinhome": "#8e24aa", "Lamudi": "#00897b", "RayWhite": "#3949ab",
        "ERA": "#6d4c41", "DotProperty": "#00acc1", "Manual": "#546e7a",
        "Google": "#43a047",
    }

    def source_badge(name):
        color = SOURCE_COLORS.get(name, "#607d8b")
        return (
            f'<span style="background:{color};color:white;padding:2px 10px;'
            f'border-radius:10px;font-size:0.75em;font-weight:600;">{name or "-"}</span>'
        )

    def kriteria_badge(c):
        """Badge status kriteria pembanding: luas +/-toleransi & jarak <= radius."""
        luas_ok = c.get("luas_ok")
        jarak_ok = c.get("jarak_ok")
        if luas_ok is None and jarak_ok is None:
            return ""  # belum dievaluasi (mis. koordinat subjek tidak ada)
        if luas_ok and jarak_ok:
            return (f'<span style="background:#2e7d32;color:white;padding:2px 10px;border-radius:10px;font-size:0.75em;font-weight:600;">'
                    f'✓ {t("Memenuhi kriteria", "Meets criteria")}</span>')
        reasons = []
        if luas_ok is False:
            reasons.append("luas di luar toleransi")
        if jarak_ok is False:
            reasons.append("jarak > radius")
        if jarak_ok is None:
            reasons.append("jarak tidak diketahui")
        label = ", ".join(reasons) if reasons else "di luar kriteria"
        if st.session_state.lang == "en":
            label = ", ".join(_kb_id_en.get(r, r) for r in reasons) if reasons else "outside criteria"
        return (f'<span style="background:#c62828;color:white;padding:2px 10px;'
                f'border-radius:10px;font-size:0.75em;font-weight:600;">⚠ {label}</span>')

    def enrich_comp(c, subjek, subjek_harga_per_m2, radius_km,
                     luas_tanah_toleransi_pct, luas_bangunan_toleransi_pct,
                     subjek_lat=None, subjek_lon=None, znt_per_m2=None, brb_per_m2=None):
        c.setdefault("include", True)
        _harga = c.get("harga") or 0
        _luas_tanah = c.get("luas_tanah") or 0
        _luas_bangunan = c.get("luas_bangunan") or 0
        c["harga_per_m2"] = round(_harga / _luas_tanah, 0) if _luas_tanah else 0

        # Estimasi Harga Bangunan & Harga Tanah pembanding: Harga Bangunan
        # diperkirakan dari Biaya Reproduksi Baru (BRB) per m² - default
        # Rp 5.000.000/m² (tidak ikut BRB/m² Step 3) - dikali Luas Bangunan
        # pembanding. Harga Tanah adalah sisa dari harga listing setelah
        # dikurangi estimasi Harga Bangunan tadi. Ini hanya estimasi kasar, bukan
        # hasil cost approach penuh - berguna sebagai pembanding kasar saja.
        if brb_per_m2:
            c["harga_bangunan_estimasi"] = round(_luas_bangunan * brb_per_m2, 0)
            c["harga_tanah_estimasi"] = round(_harga - c["harga_bangunan_estimasi"], 0)
            c["harga_tanah_per_m2"] = round(c["harga_tanah_estimasi"] / _luas_tanah, 0) if _luas_tanah else None
        else:
            c["harga_bangunan_estimasi"] = None
            c["harga_tanah_estimasi"] = None
            c["harga_tanah_per_m2"] = None

        if znt_per_m2 and c.get("harga_tanah_per_m2") is not None:
            c["selisih_tanah_pct"] = calc.hitung_selisih_pct(c["harga_tanah_per_m2"], znt_per_m2)
        else:
            c["selisih_tanah_pct"] = None

        # Kalau comp belum punya lat/lon/distance_km (mis. ditambah manual atau
        # agent tidak menjalankan geocoding), coba geocode di sini sekali saja
        # supaya kriteria radius tetap bisa dievaluasi.
        if c.get("distance_km") is None and subjek_lat is not None and subjek_lon is not None and c.get("alamat"):
            _alamat_c = c["alamat"].strip()
            ok_geo, geo = geocode_address(
                f"{_alamat_c}, {d.get('kecamatan','')}, {d.get('kabkota','')}, {d.get('provinsi','')}"
            )
            # Fallback: kalau alamat pembanding + konteks kecamatan/kabkota/
            # provinsi SUBJEK gagal (mis. karena alamat pembanding memang
            # menyebut kota LAIN yang bertentangan dengan konteks subjek),
            # geocode alamat pembanding APA ADANYA - JANGAN mundur ke
            # "kecamatan/kabkota/provinsi subjek saja", karena itu akan
            # memaksa titik pembanding ditempatkan di sekitar lokasi SUBJEK
            # walau propertinya sebenarnya jauh, sehingga jarak yang
            # dihitung jadi kecil & salah (properti yang sebenarnya di kota
            # lain terlihat cuma beberapa km, padahal seharusnya disaring
            # keluar oleh kriteria radius).
            if not ok_geo:
                ok_geo, geo = geocode_address(_alamat_c)
            if ok_geo and geo:
                c["lat"], c["lon"] = geo["lat"], geo["lon"]
                c["distance_km"] = round(calc.haversine_km(subjek_lat, subjek_lon, geo["lat"], geo["lon"]), 2)
                _provinsi_hasil = (geo.get("display_name") or "")
                _provinsi_subjek = d.get("provinsi", "")
                c["lokasi_hasil_geocode"] = _provinsi_hasil
                c["lokasi_provinsi_cocok"] = (
                    not _provinsi_subjek or not _provinsi_hasil
                    or _provinsi_subjek.strip().lower() in _provinsi_hasil.lower()
                )

        c["similarity_score"] = calc.similarity_score(subjek, c, radius_km=radius_km)
        c["selisih_pct"] = calc.hitung_selisih_pct(c["harga_per_m2"], subjek_harga_per_m2)

        if subjek_lat is not None and subjek_lon is not None:
            kriteria = calc.memenuhi_kriteria_pembanding(
                subjek, c, radius_km=radius_km,
                luas_tanah_toleransi_pct=luas_tanah_toleransi_pct,
                luas_bangunan_toleransi_pct=luas_bangunan_toleransi_pct,
            )
            c["luas_ok"] = kriteria["luas_ok"]
            c["jarak_ok"] = kriteria["jarak_ok"]
            c["memenuhi_kriteria"] = kriteria["ok"]
            # Default centang hanya untuk yang memenuhi kriteria (appraiser tetap
            # bisa mencentang manual pembanding yang di luar kriteria kalau perlu).
            if "include" not in c or c.get("_include_auto_set"):
                c["include"] = bool(kriteria["ok"])
                c["_include_auto_set"] = True
        return c

    subjek = {"luas_tanah": d["luas_tanah"], "luas_bangunan": d["luas_bangunan"],
              "tahun_bangun": d.get("tahun_bangun")}
    # Harga/m² tersirat dari Nilai Pasar Awal (Step 5) - dipakai sebagai acuan selisih%.
    subjek_harga_per_m2 = (
        round(st.session_state.nilai_pasar_awal / d["luas_tanah"], 0)
        if st.session_state.get("nilai_pasar_awal") and d.get("luas_tanah") else 0
    )
    subjek_lat, subjek_lon = d.get("lat"), d.get("lon")
    # ZNT (Harga Tanah/m²) dari Step 2 - dipakai untuk membandingkan Harga Tanah
    # (hasil estimasi di bawah) tiap pembanding terhadap ZNT properti subjek.
    znt_subjek_step6 = st.session_state.znt_result.get("harga_znt_per_m2", 0) or 0
    # BRB/m² (Biaya Reproduksi Baru) untuk mengestimasi Harga Bangunan tiap
    # pembanding (lihat enrich_comp di atas). Sengaja SELALU pakai default
    # Rp 5.000.000/m² di sini, tidak ikut BRB/m² Step 3 - supaya estimasi Harga
    # Bangunan pembanding tetap konsisten walau Step 3 diubah.
    brb_subjek_step6 = 5_000_000.0

    # --- Ringkasan Data Properti Subjek (dari Step 1-3) - sebagai acuan cepat
    # sebelum membandingkan dengan properti pembanding di bawah. ---
    _nilai_tanah_subjek = st.session_state.znt_result.get("nilai_tanah")
    _nilai_bangunan_subjek = st.session_state.bangunan_result.get("nilai_bangunan")
    _harga_pengajuan_subjek = d.get("harga_pengajuan")
    _zona_znt_subjek = st.session_state.znt_result.get("zona_nilai_tanah")
    _total_faktor_pengurang_subjek = st.session_state.faktor_pengurang.get("total_faktor_pengurang")
    _nilai_pasar_awal_subjek = st.session_state.get("nilai_pasar_awal")
    with st.expander(t("📋 Data Properti Subjek (dari Step 1-5)", "📋 Subject Property Data (from Steps 1-5)"), expanded=False):
        sc1, sc2, sc3 = st.columns(3)
        sc1.metric(t("Luas Bangunan (Step 1)", "Building Area (Step 1)"), f"{d.get('luas_bangunan', '-')} m²")
        sc2.metric(t("Luas Tanah (Step 1)", "Land Area (Step 1)"), f"{d.get('luas_tanah', '-')} m²")
        sc3.metric(
            t("Harga (Step 1)", "Price (Step 1)"),
            fmt_rp(_harga_pengajuan_subjek) if _harga_pengajuan_subjek else "N/A",
            help=t("Harga yang Diajukan / Harga Penawaran Pemilik dari Step 1.",
                   "Proposed Price / Owner's Asking Price from Step 1."),
        )

        # Koordinat & alamat ditampilkan sebagai teks biasa (bukan st.metric) -
        # st.metric memotong angka panjang dengan "..." di beberapa layout.
        st.markdown(f"**{t('Lokasi (Step 1)', 'Location (Step 1)')}**")
        if subjek_lat and subjek_lon:
            st.markdown(f"`{subjek_lat:.6f}, {subjek_lon:.6f}`")
        else:
            st.markdown("N/A")
        if d.get("alamat"):
            st.caption(f"📍 {d['alamat']}")

        sc5, sc6, sc7 = st.columns(3)
        sc5.metric(
            t("Nilai Bangunan (Step 3)", "Building Value (Step 3)"),
            fmt_rp(_nilai_bangunan_subjek) if _nilai_bangunan_subjek else "N/A",
        )
        sc6.metric(
            t("Total Nilai Tanah (Step 2)", "Total Land Value (Step 2)"),
            fmt_rp(_nilai_tanah_subjek) if _nilai_tanah_subjek else "N/A",
            help=t("= Harga ZNT/m² × Luas Tanah.", "= ZNT Price/m² × Land Area."),
        )
        sc7.metric(
            t("Harga ZNT / m² (Step 2)", "ZNT Price / m² (Step 2)"),
            fmt_rp(znt_subjek_step6) if znt_subjek_step6 else "N/A",
            help=t("= Harga ZNT/m² dari Step 2.", "= ZNT Price/m² from Step 2."),
        )

        sc8, sc9, sc10 = st.columns(3)
        sc8.metric(
            t("Zona Nilai Tanah / ZNT (Step 2)", "Land Value Zone / ZNT (Step 2)"),
            str(_zona_znt_subjek) if _zona_znt_subjek else "N/A",
        )
        sc9.metric(
            t("Total Faktor Pengurang (Step 4)", "Total Reduction Factor (Step 4)"),
            f"{_total_faktor_pengurang_subjek*100:.2f}%" if _total_faktor_pengurang_subjek is not None else "N/A",
        )
        sc10.metric(
            t("💰 Nilai Pasar Awal (Step 5)", "💰 Initial Market Value (Step 5)"),
            fmt_rp(_nilai_pasar_awal_subjek) if _nilai_pasar_awal_subjek else "N/A",
            help=t("= (Nilai Tanah + Nilai Bangunan) × (1 − Faktor Pengurang).",
                   "= (Land Value + Building Value) × (1 − Reduction Factor)."),
        )

    if not subjek_lat or not subjek_lon:
        st.warning(t(
            "⚠ Koordinat properti subjek belum tersedia (biasanya diisi otomatis di Step 2 "
            "lewat geocoding alamat). Tanpa ini, filter radius & similarity berbasis jarak "
            "tidak bisa dihitung — kembali ke Step 1/2 dan pastikan alamat/lat-lon terisi, "
            "atau lanjutkan tanpa filter jarak (similarity akan memakai skor netral untuk jarak).",
            "⚠ Subject property coordinates aren't available yet (usually auto-filled at Step 2 "
            "via address geocoding). Without this, radius and distance-based similarity filters "
            "can't be calculated — go back to Step 1/2 and make sure the address/lat-lon is "
            "filled in, or continue without the distance filter (similarity will use a neutral "
            "score for distance)."
        ))

    # Kriteria radius & toleransi luas SELALU bisa disesuaikan di sini - baik
    # sebelum pencarian pertama, maupun setelah pembanding sudah ditemukan.
    # Mengubah nilai ini akan langsung mengevaluasi ulang status "memenuhi
    # kriteria" untuk pembanding yang sudah ada, tanpa perlu mencari ulang.
    st.subheader(t("Kriteria Pencarian & Filter Pembanding", "Search Criteria & Comparable Filters"))
    st.caption(t(
        "⚠️ Luas tanah (LT) dan luas bangunan (LB) dicek TERPISAH dan keduanya harus lolos "
        "(bukan rata-rata) — contoh: kalau LB pembanding jauh lebih besar dari subjek tapi LT-nya "
        "persis sama, pembanding itu tetap GUGUR karena gagal di toleransi LB, walaupun LT-nya cocok.",
        "⚠️ Land area (LT) and building area (LB) are checked SEPARATELY and both must pass "
        "(not an average) — e.g. if a comparable's LB is much bigger than the subject's but its "
        "LT is an exact match, it still FAILS due to the LB tolerance, even though its LT matches."
    ))
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        st.session_state.radius_km = st.number_input(
            t("Radius maksimum (km)", "Maximum radius (km)"), min_value=0.5, max_value=50.0,
            value=st.session_state.radius_km, step=0.5,
            help=t("Pembanding HARUS berada dalam radius ini dari properti subjek. Default 5 km, bisa "
                 "disesuaikan kapan saja - kriteria pembanding yang sudah ada akan otomatis dievaluasi ulang.",
                 "Comparables MUST be within this radius of the subject property. Default 5 km, "
                 "can be adjusted anytime - existing comparables' criteria will be automatically "
                 "re-evaluated."),
        )
    with col_b:
        luas_tanah_toleransi_persen = st.slider(
            t("Toleransi luas tanah vs subjek", "Land area tolerance vs subject"),
            min_value=0, max_value=50,
            value=int(round(st.session_state.luas_tanah_toleransi_pct * 100)), step=5,
            format="±%d%%",
            help=t("Pembanding harus punya luas TANAH dalam rentang ini dibanding properti subjek "
                 "(default ±20%). Set ke 0% untuk MEMATIKAN filter ini (semua luas tanah dianggap "
                 "memenuhi kriteria). Bisa diubah kapan saja, termasuk setelah pembanding ditemukan.",
                 "Comparables must have a LAND area within this range vs the subject property "
                 "(default ±20%). Set to 0% to DISABLE this filter (all land areas are treated as "
                 "meeting the criterion). Can be changed anytime, including after comparables are found."),
        )
        st.session_state.luas_tanah_toleransi_pct = luas_tanah_toleransi_persen / 100.0
        _lt = d.get("luas_tanah")
        if luas_tanah_toleransi_persen == 0:
            st.caption(t("Toleransi LT dimatikan - semua luas tanah dianggap memenuhi kriteria ini.",
                         "LT tolerance disabled - all land areas are treated as meeting this criterion."))
        elif _lt:
            _lt_lo = _lt * (1 - st.session_state.luas_tanah_toleransi_pct)
            _lt_hi = _lt * (1 + st.session_state.luas_tanah_toleransi_pct)
            st.caption(t(f"Rentang LT diterima: **{_lt_lo:.0f} - {_lt_hi:.0f} m²** (subjek: {_lt:.0f} m²)",
                         f"Accepted LT range: **{_lt_lo:.0f} - {_lt_hi:.0f} m²** (subject: {_lt:.0f} m²)"))
    with col_c:
        luas_bangunan_toleransi_persen = st.slider(
            t("Toleransi luas bangunan vs subjek", "Building area tolerance vs subject"),
            min_value=0, max_value=50,
            value=int(round(st.session_state.luas_bangunan_toleransi_pct * 100)), step=5,
            format="±%d%%",
            help=t("Pembanding harus punya luas BANGUNAN dalam rentang ini dibanding properti subjek "
                 "(default ±20%). Set ke 0% untuk MEMATIKAN filter ini (semua luas bangunan dianggap "
                 "memenuhi kriteria). Bisa diubah kapan saja, termasuk setelah pembanding ditemukan.",
                 "Comparables must have a BUILDING area within this range vs the subject property "
                 "(default ±20%). Set to 0% to DISABLE this filter (all building areas are treated "
                 "as meeting the criterion). Can be changed anytime, including after comparables are found."),
        )
        st.session_state.luas_bangunan_toleransi_pct = luas_bangunan_toleransi_persen / 100.0
        _lb = d.get("luas_bangunan")
        if luas_bangunan_toleransi_persen == 0:
            st.caption(t("Toleransi LB dimatikan - semua luas bangunan dianggap memenuhi kriteria ini.",
                         "LB tolerance disabled - all building areas are treated as meeting this criterion."))
        elif _lb:
            _lb_lo = _lb * (1 - st.session_state.luas_bangunan_toleransi_pct)
            _lb_hi = _lb * (1 + st.session_state.luas_bangunan_toleransi_pct)
            st.caption(t(f"Rentang LB diterima: **{_lb_lo:.0f} - {_lb_hi:.0f} m²** (subjek: {_lb:.0f} m²)",
                         f"Accepted LB range: **{_lb_lo:.0f} - {_lb_hi:.0f} m²** (subject: {_lb:.0f} m²)"))

    if not st.session_state.comparables:
        st.subheader(t("Cari Properti Pembanding", "Search for Comparable Properties"))
        st.session_state.comparable_count = st.number_input(
            t("Jumlah pembanding yang dicari", "Number of comparables to search for"), min_value=1, max_value=50,
            value=st.session_state.comparable_count, step=1,
            help=t("Default 15 - appraiser bebas menyesuaikan jumlah pembanding yang ingin dicari.",
                   "Default 15 - the appraiser is free to adjust how many comparables to search for."),
        )
        if st.button(t("🔍 Mulai Pencarian", "🔍 Start Search"), type="primary"):
            # Dipakai search_comparables_until_target untuk PENCARIAN OTOMATIS
            # BERULANG sampai jumlah yang diminta tercapai (bukan cuma sekali
            # jalan lalu berhenti walau hasilnya jauh di bawah target) - lihat
            # docstring fungsi tsb di agents.py untuk detail kapan ia berhenti
            # (target tercapai / max_rounds habis / 2 ronde kosong berturut).
            with st.status(t("Mencari properti pembanding...", "Searching for comparable properties..."), expanded=True) as status:
                progress_placeholder = st.empty()

                def _progress(round_no, n_so_far, target):
                    progress_placeholder.write(
                        t(f"**Ronde {round_no} — {n_so_far}/{target} ditemukan, mencari lagi...**",
                          f"**Round {round_no} — {n_so_far}/{target} found, searching more...**")
                    )

                log, comps = search_comparables_until_target(
                    d["alamat"], d["kecamatan"], d["kabkota"], d["provinsi"],
                    d["luas_tanah"], d["luas_bangunan"], serper, groq, gemini,
                    target=st.session_state.comparable_count,
                    subjek_lat=subjek_lat, subjek_lon=subjek_lon,
                    radius_km=st.session_state.radius_km,
                    luas_tanah_toleransi_pct=st.session_state.luas_tanah_toleransi_pct,
                    luas_bangunan_toleransi_pct=st.session_state.luas_bangunan_toleransi_pct,
                    progress_cb=_progress,
                    lang=st.session_state.lang,
                )
                for line in log:
                    st.write(line)
                status.update(
                    label=(t("Selesai", "Done") if comps else t("Selesai - 0 pembanding ditemukan", "Done - 0 comparables found")),
                    state="complete" if comps else "error",
                    expanded=True,
                )
            # Simpan log ke session_state SEBELUM rerun - kalau tidak, log di atas
            # akan hilang begitu st.rerun() menjalankan ulang skrip dari awal,
            # dan kalau comps kosong, UI akan kembali persis ke tampilan sebelum
            # tombol diklik seolah-olah klik tidak berpengaruh sama sekali.
            st.session_state.last_comparable_search_log = log
            comps = [
                enrich_comp(c, subjek, subjek_harga_per_m2, st.session_state.radius_km,
                            st.session_state.luas_tanah_toleransi_pct,
                            st.session_state.luas_bangunan_toleransi_pct, subjek_lat, subjek_lon,
                            znt_per_m2=znt_subjek_step6, brb_per_m2=brb_subjek_step6)
                for c in comps
            ]
            st.session_state.comparables = comps
            st.rerun()
        st.info(t(
            "Klik \"Mulai Pencarian\" untuk mencari properti pembanding secara otomatis, "
            "atau tambahkan pembanding manual di bawah tanpa pencarian otomatis.",
            "Click \"Start Search\" to search for comparable properties automatically, "
            "or add a manual comparable below without an automatic search."
        ))

    comps = st.session_state.comparables

    # Log pencarian TERAKHIR - selalu ditampilkan (bukan cuma saat 0 pembanding
    # ditemukan) supaya appraiser bisa lihat diagnostik lengkap (berapa hasil
    # mentah dari Serper, berapa halaman kategori dibuang, berapa halaman
    # berhasil di-fetch, berapa listing dilewati LLM karena harga/LT/LB tidak
    # lengkap, dst) bahkan kalau pencarian "berhasil" tapi jumlahnya jauh di
    # bawah yang diminta (mis. minta 15, cuma dapat 2) - sebelumnya log ini
    # cuma muncul kalau comparables masih kosong, jadi begitu ada 1 pembanding
    # ditemukan sekalipun, log diagnostik ini langsung hilang dari tampilan
    # walau appraiser masih ingin tahu kenapa jumlahnya sedikit.
    last_log = st.session_state.get("last_comparable_search_log")
    if last_log:
        n_found_last = len(comps)
        with st.expander(
            t(f"📋 Log pencarian pembanding terakhir ({len(last_log)} baris)",
              f"📋 Last comparable search log ({len(last_log)} lines)"),
            expanded=(n_found_last == 0),
        ):
            for line in last_log:
                st.write(line)

    # Evaluasi ulang kriteria (luas & jarak) untuk pembanding yang sudah ada
    # setiap kali halaman ini di-render - supaya perubahan slider radius/
    # toleransi di atas langsung terlihat efeknya tanpa perlu pencarian ulang.
    if comps and subjek_lat is not None and subjek_lon is not None:
        for c in comps:
            kriteria = calc.memenuhi_kriteria_pembanding(
                subjek, c, radius_km=st.session_state.radius_km,
                luas_tanah_toleransi_pct=st.session_state.luas_tanah_toleransi_pct,
                luas_bangunan_toleransi_pct=st.session_state.luas_bangunan_toleransi_pct,
            )
            c["luas_ok"] = kriteria["luas_ok"]
            c["jarak_ok"] = kriteria["jarak_ok"]
            c["memenuhi_kriteria"] = kriteria["ok"]

    # Hitung ulang estimasi Harga Bangunan/Tanah tiap pembanding juga di setiap
    # render - supaya kalau BRB (Step 3) atau ZNT (Step 2) diubah/diambil ulang,
    # estimasi ini otomatis ikut ter-update tanpa perlu pencarian ulang pembanding.
    if comps and brb_subjek_step6:
        for c in comps:
            _lb_c = c.get("luas_bangunan") or 0
            _lt_c = c.get("luas_tanah") or 0
            _harga_c = c.get("harga") or 0
            c["harga_bangunan_estimasi"] = round(_lb_c * brb_subjek_step6, 0)
            c["harga_tanah_estimasi"] = round(_harga_c - c["harga_bangunan_estimasi"], 0)
            c["harga_tanah_per_m2"] = round(c["harga_tanah_estimasi"] / _lt_c, 0) if _lt_c else None
            if znt_subjek_step6 and c["harga_tanah_per_m2"] is not None:
                c["selisih_tanah_pct"] = calc.hitung_selisih_pct(c["harga_tanah_per_m2"], znt_subjek_step6)
            else:
                c["selisih_tanah_pct"] = None

    if comps:
        n_ok = sum(1 for c in comps if c.get("memenuhi_kriteria"))
        st.subheader(t("Daftar Properti Pembanding", "List of Comparable Properties"))
        st.caption(t(f"{len(comps)} ditemukan, {n_ok} memenuhi kriteria",
                     f"{len(comps)} found, {n_ok} meet the criteria"))
        st.caption(t(f"Kriteria aktif: luas tanah ±{st.session_state.luas_tanah_toleransi_pct*100:.0f}%, "
                    f"luas bangunan ±{st.session_state.luas_bangunan_toleransi_pct*100:.0f}%, "
                    f"radius maksimum {st.session_state.radius_km:g} km dari properti subjek.",
                    f"Active criteria: land area ±{st.session_state.luas_tanah_toleransi_pct*100:.0f}%, "
                    f"building area ±{st.session_state.luas_bangunan_toleransi_pct*100:.0f}%, "
                    f"maximum radius {st.session_state.radius_km:g} km from the subject property."))

        sel_col1, sel_col2, sel_col3 = st.columns([1, 1, 2])
        with sel_col1:
            if st.button(t("☑️ Pilih Semua", "☑️ Select All"), use_container_width=True):
                for i, c in enumerate(comps):
                    c["include"] = True
                    st.session_state[f"inc_{i}"] = True
                st.rerun()
        with sel_col2:
            if st.button(t("☐ Batalkan Semua", "☐ Deselect All"), use_container_width=True):
                for i, c in enumerate(comps):
                    c["include"] = False
                    st.session_state[f"inc_{i}"] = False
                st.rerun()
        with sel_col3:
            n_selected = sum(1 for c in comps if c.get("include", True))
            st.write("")
            st.caption(f"**{n_selected}** selected")

        with st.container(border=True):
            st.markdown(t("**🎯 Auto Select Pembanding**", "**🎯 Auto Select Comparables**"))
            st.caption(t(
                "Atur toleransi/kriteria di bawah lalu klik Terapkan - semua pembanding yang "
                "memenuhi SEMUA kriteria akan dicentang, yang tidak akan dibatalkan centangnya. "
                "Kriteria ini terpisah dari \"Kriteria Pencarian Pembanding\" di atas.",
                "Set the tolerances/criteria below then click Apply - every comparable meeting "
                "ALL criteria will be checked, and any that don't will be unchecked. These "
                "criteria are independent from \"Comparable Search Criteria\" above."
            ))

            as_col1, as_col2 = st.columns([1, 2])
            with as_col1:
                auto_select_pct = st.number_input(
                    t("Toleransi Harga Tanah (%)", "Land Price Tolerance (%)"), min_value=1, max_value=500,
                    value=int(st.session_state.get("auto_select_pct", 150)), step=10,
                    key="auto_select_pct",
                    help=t(
                        "Selisih Harga Tanah/m² (bukan harga properti total) terhadap Harga "
                        "ZNT/m² Subjek (Step 2) harus berada dalam rentang ± toleransi ini.",
                        "The Land Price/m² (not total property price) difference vs the "
                        "Subject's ZNT Price/m² (Step 2) must be within ± this tolerance."
                    ),
                )
            with as_col2:
                st.write("")
                if znt_subjek_step6:
                    _range_lo = znt_subjek_step6 * (1 - auto_select_pct / 100.0)
                    _range_hi = znt_subjek_step6 * (1 + auto_select_pct / 100.0)
                    st.caption(t(
                        f"Rentang Harga Tanah/m² yang akan dipilih: **{fmt_rp(max(_range_lo, 0))} – {fmt_rp(_range_hi)}**",
                        f"Land Price/m² range that will be selected: **{fmt_rp(max(_range_lo, 0))} – {fmt_rp(_range_hi)}**"
                    ))
                else:
                    st.caption(t("Butuh Harga ZNT/m² Subjek dari Step 2.", "Needs the Subject's ZNT Price/m² from Step 2."))

            arl_col1, arl_col2, arl_col3 = st.columns(3)
            with arl_col1:
                auto_select_radius_km = st.number_input(
                    t("Radius maksimum (km)", "Maximum radius (km)"), min_value=0.5, max_value=50.0,
                    value=float(st.session_state.get("auto_select_radius_km", st.session_state.radius_km)),
                    step=0.5, key="auto_select_radius_km",
                )
            with arl_col2:
                auto_select_luas_tanah_pct_pct = st.slider(
                    t("Toleransi Luas Tanah (%)", "Land Area Tolerance (%)"), min_value=0, max_value=100,
                    value=int(round(st.session_state.get("auto_select_luas_tanah_pct", st.session_state.luas_tanah_toleransi_pct) * 100)),
                    step=5, key="auto_select_luas_tanah_pct_slider",
                )
                st.session_state.auto_select_luas_tanah_pct = auto_select_luas_tanah_pct_pct / 100.0
            with arl_col3:
                auto_select_luas_bangunan_pct_pct = st.slider(
                    t("Toleransi Luas Bangunan (%)", "Building Area Tolerance (%)"), min_value=0, max_value=100,
                    value=int(round(st.session_state.get("auto_select_luas_bangunan_pct", st.session_state.luas_bangunan_toleransi_pct) * 100)),
                    step=5, key="auto_select_luas_bangunan_pct_slider",
                )
                st.session_state.auto_select_luas_bangunan_pct = auto_select_luas_bangunan_pct_pct / 100.0

            st.caption(t(
                f"Kriteria yang akan diterapkan: selisih Harga Tanah ±{auto_select_pct}%, "
                f"radius maksimum {auto_select_radius_km:g} km, "
                f"luas tanah ±{auto_select_luas_tanah_pct_pct}% vs subjek, "
                f"luas bangunan ±{auto_select_luas_bangunan_pct_pct}% vs subjek.",
                f"Criteria that will be applied: Land Price difference ±{auto_select_pct}%, "
                f"maximum radius {auto_select_radius_km:g} km, "
                f"land area ±{auto_select_luas_tanah_pct_pct}% vs subject, "
                f"building area ±{auto_select_luas_bangunan_pct_pct}% vs subject."
            ))

            auto_select_clicked = st.button(
                t("Terapkan Auto Select", "Apply Auto Select"),
                disabled=not znt_subjek_step6,
            )
            if auto_select_clicked:
                for i, c in enumerate(comps):
                    st_pct = c.get("selisih_tanah_pct")
                    harga_tanah_ok = st_pct is not None and abs(st_pct) <= auto_select_pct
                    kriteria = calc.memenuhi_kriteria_pembanding(
                        subjek, c, radius_km=auto_select_radius_km,
                        luas_tanah_toleransi_pct=st.session_state.auto_select_luas_tanah_pct,
                        luas_bangunan_toleransi_pct=st.session_state.auto_select_luas_bangunan_pct,
                    )
                    ok = harga_tanah_ok and bool(kriteria["ok"])
                    c["include"] = ok
                    st.session_state[f"inc_{i}"] = ok
                st.rerun()

        for i, c in enumerate(comps):
            with st.container(border=True):
                top = st.columns([0.4, 0.5, 3.2])
                c["include"] = top[0].checkbox("✓", value=c.get("include", True), key=f"inc_{i}")
                top[1].markdown(f"**#{i+1}**")
                link = c.get("link", "")
                alamat_line = f"**{c.get('alamat','-')}**"
                if link:
                    alamat_line += f"  \n[{link[:60]}]({link})"
                top[2].markdown(alamat_line)
                badges = source_badge(c.get("sumber", "-"))
                kb = kriteria_badge(c)
                if kb:
                    badges += "&nbsp;" + kb
                top[2].markdown(badges, unsafe_allow_html=True)

                m = st.columns(4)
                m[0].metric(t("Harga", "Price"), fmt_rp(c.get("harga", 0)))
                hb = c.get("harga_bangunan_estimasi")
                m[1].metric(
                    t("Harga Bangunan (Estimasi)", "Building Price (Estimate)"),
                    fmt_rp(hb) if hb is not None else "N/A",
                    help=t(
                        "Estimasi = Luas Bangunan pembanding × BRB/m² (Biaya Reproduksi Baru) "
                        "dari Step 3 (kalau Step 3 belum diisi, pakai default Rp 5.000.000/m²). "
                        "Bukan hasil cost approach penuh - hanya perkiraan kasar untuk pembanding.",
                        "Estimate = this comparable's Building Area × BRB/m² (New Reproduction "
                        "Cost) from Step 3 (defaults to Rp 5,000,000/m² if Step 3 isn't filled "
                        "in yet). Not a full cost approach result - just a rough estimate for "
                        "comparison."
                    ),
                )
                ht = c.get("harga_tanah_estimasi")
                m[2].metric(
                    t("Harga Tanah (Estimasi)", "Land Price (Estimate)"),
                    fmt_rp(ht) if ht is not None else "N/A",
                    help=t(
                        "Estimasi = Harga listing − Harga Bangunan (Estimasi) di sebelah kiri.",
                        "Estimate = Listing price − the Building Price (Estimate) shown to the left."
                    ),
                )
                htm2 = c.get("harga_tanah_per_m2")
                m[3].metric(
                    t("Harga Tanah / m²", "Land Price / m²"),
                    fmt_rp(htm2) if htm2 is not None else "N/A",
                    help=t("Harga Tanah (Estimasi) ÷ Luas Tanah pembanding.",
                           "Land Price (Estimate) ÷ this comparable's Land Area."),
                )

                m2 = st.columns(4)
                m2[0].metric(t("Luas Tanah", "Land Area"), f"{c.get('luas_tanah','-')} m²")
                m2[1].metric(t("Luas Bangunan", "Building Area"), f"{c.get('luas_bangunan','-')} m²")
                dist = c.get("distance_km")
                m2[2].metric(t("Jarak ke Subjek", "Distance to Subject"), f"{dist:.2f} km" if dist is not None else t("Tidak diketahui", "Unknown"))
                m2[3].metric(t("Tanggal Upload Listing", "Listing Upload Date"), c.get("tanggal_upload") or t("Tidak diketahui", "Unknown"))

                m3 = st.columns(3)
                st_pct = c.get("selisih_tanah_pct")
                m3[0].metric(
                    t("Selisih Harga Tanah vs ZNT", "Land Price Difference vs ZNT"),
                    "N/A" if st_pct is None else "",
                    delta=f"{st_pct:+.1f}%" if st_pct is not None else None,
                    delta_color="inverse",
                    help=t(
                        "Selisih Harga Tanah/m² (Estimasi, di atas) pembanding terhadap Harga "
                        "ZNT/m² Subjek dari Step 2. Dipakai oleh \"Auto Select berdasarkan "
                        "Harga Tanah\" di atas.",
                        "Difference of this comparable's Land Price/m² (Estimate, above) vs "
                        "the Subject's ZNT Price/m² from Step 2. Used by \"Auto Select by "
                        "Land Price\" above."
                    ),
                )
                m3[1].metric("Similarity", f"{c.get('similarity_score','-')}%" if c.get('similarity_score') is not None else "-")
                m3[2].markdown("")

                if c.get("catatan"):
                    st.caption(f"ℹ️ {c['catatan']}")

        col_more1, col_more2 = st.columns([2, 1])
        with col_more1:
            add_n = st.number_input(
                t("Tambah berapa pembanding lagi?", "How many more comparables to add?"), min_value=5, max_value=30, value=10, step=5,
                key="add_n_comparables",
            )
        with col_more2:
            st.write("")
            st.write("")
            if st.button(t("🔍 Cari Lebih Banyak", "🔍 Search for More")):
                existing_links = {c.get("link") for c in comps if c.get("link")}
                with st.status(t("Mencari properti pembanding tambahan...", "Searching for additional comparable properties..."), expanded=True) as status:
                    progress_placeholder2 = st.empty()

                    def _progress2(round_no, n_so_far, target):
                        progress_placeholder2.write(
                            t(f"**Ronde {round_no} — {n_so_far}/{target} tambahan ditemukan, mencari lagi...**",
                              f"**Round {round_no} — {n_so_far}/{target} additional found, searching more...**")
                        )

                    # Link yang SUDAH ada di daftar sebelum tombol ini diklik
                    # di-seed lewat exclude_links supaya search_comparables_
                    # until_target menganggapnya "sudah ketemu" SEJAK RONDE 1
                    # - bukan cuma difilter di akhir. Kalau cuma difilter di
                    # akhir (seperti sebelumnya), listing lama yang ketemu
                    # lagi di tengah pencarian dihitung sebagai progress menuju
                    # add_n, loop berhenti mengira target tercapai, padahal
                    # setelah difilter jumlah pembanding BARU yang tersisa jauh
                    # di bawah add_n yang diminta appraiser.
                    log, more_comps = search_comparables_until_target(
                        d["alamat"], d["kecamatan"], d["kabkota"], d["provinsi"],
                        d["luas_tanah"], d["luas_bangunan"], serper, groq, gemini,
                        target=add_n,
                        subjek_lat=subjek_lat, subjek_lon=subjek_lon,
                        radius_km=st.session_state.radius_km,
                        luas_tanah_toleransi_pct=st.session_state.luas_tanah_toleransi_pct,
                        luas_bangunan_toleransi_pct=st.session_state.luas_bangunan_toleransi_pct,
                        progress_cb=_progress2,
                        exclude_links=existing_links,
                        lang=st.session_state.lang,
                    )
                    for line in log:
                        st.write(line)
                    status.update(label=t("Selesai", "Done"), state="complete", expanded=True)
                st.session_state.last_comparable_search_log = log
                more_comps = [
                    enrich_comp(c, subjek, subjek_harga_per_m2, st.session_state.radius_km,
                                st.session_state.luas_tanah_toleransi_pct,
                                st.session_state.luas_bangunan_toleransi_pct, subjek_lat, subjek_lon,
                                znt_per_m2=znt_subjek_step6, brb_per_m2=brb_subjek_step6)
                    for c in more_comps
                ]
                st.session_state.comparables.extend(more_comps)
                st.rerun()
    else:
        st.warning(t(
            "Belum ada properti pembanding (butuh Serper key + Groq/Gemini key, dan koneksi internet). "
            "Anda bisa tambahkan pembanding manual di bawah, atau lanjut tanpa pembanding.",
            "No comparable properties yet (needs a Serper key + Groq/Gemini key, and an internet "
            "connection). You can add a manual comparable below, or continue without any."
        ))

    with st.expander(t("+ Tambah properti pembanding manual", "+ Add a manual comparable property")):
        with st.form("manual_comp_form"):
            m_alamat = st.text_input(t("Alamat", "Address"))
            mc1, mc2, mc3, mc4 = st.columns(4)
            m_harga = mc1.number_input(t("Harga (Rp)", "Price (Rp)"), min_value=0.0, step=1_000_000.0)
            m_lt = mc2.number_input("LT (m²)", min_value=0.0, step=1.0)
            m_lb = mc3.number_input("LB (m²)", min_value=0.0, step=1.0)
            m_thn = mc4.number_input(t("Tahun", "Year"), min_value=1900, max_value=2100, value=2015)
            m_tgl_upload = st.text_input(
                t("Tanggal Upload Listing (isi manual, bukan tanggal hari ini)",
                  "Listing Upload Date (fill in manually, not today's date)"),
                placeholder=t("mis. 12 Mei 2025, atau \"3 hari lalu\" sesuai listing aslinya",
                               "e.g. 12 May 2025, or \"3 days ago\" per the original listing"),
            )
            if st.form_submit_button(t("Tambahkan", "Add")):
                new_comp = {
                    "alamat": m_alamat, "harga": m_harga, "luas_tanah": m_lt,
                    "luas_bangunan": m_lb, "tahun_bangun": m_thn, "sumber": "Manual",
                    "tanggal_upload": m_tgl_upload or None,
                    "link": "", "include": True,
                }
                new_comp = enrich_comp(new_comp, subjek, subjek_harga_per_m2,
                                        st.session_state.radius_km,
                                        st.session_state.luas_tanah_toleransi_pct,
                                        st.session_state.luas_bangunan_toleransi_pct,
                                        subjek_lat, subjek_lon, znt_per_m2=znt_subjek_step6,
                                        brb_per_m2=brb_subjek_step6)
                st.session_state.comparables.append(new_comp)
                st.rerun()

    col_back, col_next = st.columns(2)
    with col_back:
        if st.button("← Back"):
            goto(5)
    with col_next:
        if st.button("Continue →", type="primary"):
            goto(7)

# ===========================================================================
# STEP 7 - Perbandingan Harga Tanah per m²
# ===========================================================================
elif st.session_state.step == 7:
    st.header(t("Step 7 — Perbandingan Harga Tanah per m²", "Step 7 — Land Price per m² Comparison"))
    d = st.session_state.data

    included = [c for c in st.session_state.comparables if c.get("include")]
    harga_tanah_m2_included = [c.get("harga_tanah_per_m2", 0) for c in included if c.get("harga_tanah_per_m2")]
    stats = calc.statistik_pembanding(harga_tanah_m2_included)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Average /m²", fmt_rp(stats["average"]))
    c2.metric("Median /m²", fmt_rp(stats["median"]))
    c3.metric("Minimum /m²", fmt_rp(stats["minimum"]))
    c4.metric("Maximum /m²", fmt_rp(stats["maximum"]))

    znt_subjek = st.session_state.znt_result.get("harga_znt_per_m2", 0) or 0
    if znt_subjek:
        st.metric(t("Harga ZNT/m² (Subjek)", "ZNT Price/m² (Subject)"), fmt_rp(znt_subjek))

    if harga_tanah_m2_included and d.get("luas_tanah"):
        median_info = calc.nilai_median_untuk_luas(harga_tanah_m2_included, d["luas_tanah"])
        st.info(t(
            f"📊 Median dari {median_info['n_pembanding']} properti pembanding yang dicentang: "
            f"**{fmt_rp(median_info['median_per_m2'])}/m²** → diproyeksikan untuk "
            f"**{median_info['luas_target']:.0f} m²** (luas tanah subjek) = "
            f"**{fmt_rp(median_info['total_median'])}**.",
            f"📊 Median from {median_info['n_pembanding']} checked comparable properties: "
            f"**{fmt_rp(median_info['median_per_m2'])}/m²** → projected for "
            f"**{median_info['luas_target']:.0f} m²** (subject land area) = "
            f"**{fmt_rp(median_info['total_median'])}**."
        ))

    col_back, col_next = st.columns(2)
    with col_back:
        if st.button("← Back"):
            goto(6)
    with col_next:
        if st.button("Continue →", type="primary"):
            goto(8)

# ===========================================================================
# STEP 8 - Validasi & Rentang Nilai Pasar
# ===========================================================================
elif st.session_state.step == 8:
    st.header(t("Step 8 — Validasi & Rentang Nilai Pasar", "Step 8 — Validation & Market Value Range"))
    d = st.session_state.data

    included = [c for c in st.session_state.comparables if c.get("include")]
    harga_per_m2_list = [c.get("harga_per_m2", 0) for c in included if c.get("harga_per_m2")]
    stats = calc.statistik_pembanding(harga_per_m2_list)
    average_comparable_total = stats["average"] * d["luas_tanah"]
    stats_total = {k: v * d["luas_tanah"] for k, v in stats.items()}  # min/max/avg/median dlm total Rp

    nilai_pasar_awal = st.session_state.nilai_pasar_awal

    difference_pct = (
        round(((nilai_pasar_awal - average_comparable_total) / average_comparable_total) * 100, 2)
        if average_comparable_total else 0.0
    )
    validasi = {"difference_pct": difference_pct}
    st.session_state.validasi = validasi

    c1, c2 = st.columns(2)
    c1.metric(t("Nilai Pasar Awal", "Initial Market Value"), fmt_rp(nilai_pasar_awal))
    c2.metric("Average Comparable (total)", fmt_rp(average_comparable_total))
    st.metric("Difference vs Average Comparable (%)", f"{difference_pct}%")
    with st.expander(t("🧮 Lihat cara hitung", "🧮 Show calculation details")):
        st.caption(t(
            f"**Cara hitung:**\n"
            f"- Average Comparable/m² = rata-rata dari (Harga Listing ÷ Luas Tanah) tiap pembanding "
            f"yang dicentang = **{fmt_rp(stats['average'])}/m²** (dari {len(harga_per_m2_list)} pembanding)\n"
            f"- Average Comparable (total) = Average Comparable/m² × Luas Tanah Subjek "
            f"= {fmt_rp(stats['average'])} × {d.get('luas_tanah','-')} m² = **{fmt_rp(average_comparable_total)}**\n"
            f"- Difference (%) = (Nilai Pasar Awal − Average Comparable total) ÷ Average Comparable total × 100% "
            f"= ({fmt_rp(nilai_pasar_awal)} − {fmt_rp(average_comparable_total)}) ÷ {fmt_rp(average_comparable_total)} × 100% "
            f"= **{difference_pct}%**\n\n"
            f"Catatan: Harga Listing di sini adalah harga TOTAL pembanding (tanah + bangunan, "
            f"belum dikurangi estimasi bangunan) dibagi Luas Tanahnya - berbeda dari \"Average /m²\" "
            f"di Step 7 yang memakai Harga Tanah (Estimasi) per m² (sudah dikurangi estimasi harga "
            f"bangunan). Positif = Nilai Pasar Awal lebih tinggi dari rata-rata pembanding; "
            f"negatif = lebih rendah.",
            f"**How it's calculated:**\n"
            f"- Average Comparable/m² = average of (Listing Price ÷ Land Area) across checked "
            f"comparables = **{fmt_rp(stats['average'])}/m²** (from {len(harga_per_m2_list)} comparables)\n"
            f"- Average Comparable (total) = Average Comparable/m² × Subject's Land Area "
            f"= {fmt_rp(stats['average'])} × {d.get('luas_tanah','-')} m² = **{fmt_rp(average_comparable_total)}**\n"
            f"- Difference (%) = (Initial Market Value − Average Comparable total) ÷ Average Comparable total × 100% "
            f"= ({fmt_rp(nilai_pasar_awal)} − {fmt_rp(average_comparable_total)}) ÷ {fmt_rp(average_comparable_total)} × 100% "
            f"= **{difference_pct}%**\n\n"
            f"Note: Listing Price here is each comparable's TOTAL price (land + building, not "
            f"reduced by any building estimate) divided by its Land Area - different from the "
            f"\"Average /m²\" shown in Step 7, which uses estimated Land Price/m² (already net of "
            f"an estimated building value). Positive = Initial Market Value is above the "
            f"comparables' average; negative = below."
        ))

    # Rentang Nilai Pasar - dipakai saat internal (Nilai Pasar Awal) dan pembanding
    # pasar berbeda cukup jauh (mis. internal Rp500jt vs pembanding rata-rata
    # Rp900jt): daripada memaksa satu angka, tampilkan rentang yang mencakup
    # keduanya, plus satu titik estimasi (point) untuk keperluan yang butuh
    # angka tunggal (mis. LTV bank).
    rentang = calc.rentang_nilai_pasar(nilai_pasar_awal, stats_total)
    st.session_state.rentang_nilai_pasar = rentang

    st.markdown(t("**📊 Rentang Nilai Pasar**", "**📊 Market Value Range**"))
    rc1, rc2 = st.columns(2)
    rc1.metric(t("Rentang", "Range"), f"{fmt_rp(rentang['min'])} – {fmt_rp(rentang['max'])}")
    rc2.metric(t("Titik Estimasi (Point)", "Estimate Point"), fmt_rp(rentang['point']))
    st.caption(t("Rentang = batas terendah & tertinggi dari (Nilai Pasar Awal, Minimum Pembanding, "
                "Maximum Pembanding). Titik Estimasi = rata-rata Nilai Pasar Awal & MEDIAN pembanding "
                "(median dipakai, bukan average, supaya titik estimasi tidak gampang tertarik oleh "
                "satu-dua listing pembanding yang harganya outlier/ekstrem).",
                "Range = lowest & highest bound of (Initial Market Value, Minimum Comparable, "
                "Maximum Comparable). Estimate Point = average of Initial Market Value & the "
                "comparables' MEDIAN (median is used instead of average so the estimate point "
                "isn't easily skewed by one or two outlier-priced comparables)."))

    default_final = rentang["point"]
    nilai_pasar_akhir = st.number_input(
        t("Nilai Pasar Akhir (appraiser dapat menyesuaikan, mis. pilih titik dalam rentang di atas)",
          "Final Market Value (the appraiser can adjust, e.g. pick a point within the range above)"),
        min_value=0.0, value=float(default_final), step=1_000_000.0,
    )

    col_back, col_next = st.columns(2)
    with col_back:
        if st.button("← Back"):
            goto(7)
    with col_next:
        if st.button("Continue →", type="primary"):
            st.session_state.nilai_pasar_akhir = nilai_pasar_akhir
            goto(9)

# ===========================================================================
# STEP 9 - Hasil Appraisal: Perbandingan Harga
# ===========================================================================
elif st.session_state.step == 9:
    st.header(t("Step 9 — Hasil Appraisal: Perbandingan Harga", "Step 9 — Appraisal Result: Price Comparison"))
    d = st.session_state.data

    # --- Perbandingan 3 pendekatan appraisal: Referensi (median pembanding),
    # ZNT (Bhumi ATR-BPN resmi), dan Internal Final (Nilai Pasar Akhir hasil
    # appraiser di Step 8). Harga Bangunan SAMA di ketiganya (konstan, dari
    # Step 3 - Cost Approach) - yang berbeda hanya Harga Tanah per pendekatan,
    # supaya appraiser bisa lihat langsung seberapa jauh selisih tiap metode.
    st.subheader(t("🏘️ Perbandingan Pendekatan Appraisal", "🏘️ Appraisal Approach Comparison"))

    harga_bangunan = st.session_state.bangunan_result.get("nilai_bangunan", 0) or 0
    luas_tanah = d.get("luas_tanah", 0) or 0

    # Harga Tanah - Referensi (median dari properti pembanding yang dicentang,
    # sama seperti perhitungan Step 7).
    included = [c for c in st.session_state.comparables if c.get("include")]
    harga_tanah_m2_included = [c.get("harga_tanah_per_m2", 0) for c in included if c.get("harga_tanah_per_m2")]
    stats_pembanding = calc.statistik_pembanding(harga_tanah_m2_included)
    harga_tanah_referensi = round(stats_pembanding["median"] * luas_tanah, 2)

    # Harga Tanah - ZNT (Bhumi ATR-BPN resmi/estimasi, dari Step 2).
    harga_tanah_znt = st.session_state.znt_result.get("nilai_tanah", 0) or 0

    # Harga Tanah - Internal Final (residual dari Nilai Pasar Akhir appraiser
    # di Step 8, dikurangi Harga Bangunan yang konstan).
    nilai_pasar_akhir = st.session_state.nilai_pasar_akhir
    harga_tanah_internal_final = round(nilai_pasar_akhir - harga_bangunan, 2)

    app_referensi = round(harga_bangunan + harga_tanah_referensi, 2)
    app_znt = round(harga_bangunan + harga_tanah_znt, 2)
    app_internal_final = round(harga_bangunan + harga_tanah_internal_final, 2)

    st.caption(t(
        f"Harga Bangunan (konstan, dari Cost Approach Step 3): **{fmt_rp(harga_bangunan)}**",
        f"Building Value (constant, from Step 3's Cost Approach): **{fmt_rp(harga_bangunan)}**",
    ))

    rows = [
        {
            "label": t("App Referensi", "App Reference"),
            "help": t(
                f"Harga Bangunan + Harga Tanah dari median {len(harga_tanah_m2_included)} "
                f"properti pembanding (Step 6/7) = {fmt_rp(harga_bangunan)} + {fmt_rp(harga_tanah_referensi)}.",
                f"Building Value + Land Value from the median of {len(harga_tanah_m2_included)} "
                f"comparable properties (Step 6/7) = {fmt_rp(harga_bangunan)} + {fmt_rp(harga_tanah_referensi)}."
            ),
            "harga_tanah": harga_tanah_referensi,
            "total": app_referensi,
        },
        {
            "label": t("App ZNT (Harga Akhir Pasar)", "App ZNT (Market Value)"),
            "help": t(
                f"Harga Bangunan + Harga Tanah dari ZNT resmi/estimasi Bhumi ATR-BPN (Step 2) "
                f"= {fmt_rp(harga_bangunan)} + {fmt_rp(harga_tanah_znt)}.",
                f"Building Value + Land Value from Bhumi ATR-BPN's official/estimated ZNT "
                f"(Step 2) = {fmt_rp(harga_bangunan)} + {fmt_rp(harga_tanah_znt)}."
            ),
            "harga_tanah": harga_tanah_znt,
            "total": app_znt,
        },
        {
            "label": t("App Internal Final", "App Internal Final"),
            "help": t(
                f"Harga Bangunan + Harga Tanah Final (= Nilai Pasar Akhir Step 8 dikurangi "
                f"Harga Bangunan) = {fmt_rp(harga_bangunan)} + {fmt_rp(harga_tanah_internal_final)} "
                f"= Nilai Pasar Akhir ({fmt_rp(nilai_pasar_akhir)}).",
                f"Building Value + Final Land Value (= Step 8's Final Market Value minus "
                f"Building Value) = {fmt_rp(harga_bangunan)} + {fmt_rp(harga_tanah_internal_final)} "
                f"= Final Market Value ({fmt_rp(nilai_pasar_akhir)})."
            ),
            "harga_tanah": harga_tanah_internal_final,
            "total": app_internal_final,
        },
    ]

    for row in rows:
        rc1, rc2, rc3, rc4 = st.columns(4)
        rc1.markdown(f"**{row['label']}**")
        rc2.metric(t("Harga Bangunan", "Building Value"), fmt_rp(harga_bangunan))
        rc3.metric(t("Harga Tanah", "Land Value"), fmt_rp(row["harga_tanah"]))
        rc4.metric(t("Total", "Total"), fmt_rp(row["total"]))
        st.caption(row["help"])

    st.divider()

    st.subheader(t("💰 Perbandingan Harga yang Diajukan vs Hasil Appraisal", "💰 Proposed Price vs Appraisal Result Comparison"))
    harga_pengajuan = d.get("harga_pengajuan")
    if harga_pengajuan:
        perbandingan = calc.bandingkan_harga_pengajuan(harga_pengajuan, st.session_state.nilai_pasar_akhir)
        pc1, pc2, pc3 = st.columns(3)
        pc1.metric(t("Harga yang Diajukan (Step 1)", "Proposed Price (Step 1)"), fmt_rp(harga_pengajuan))
        pc2.metric(t("Nilai Pasar Akhir (Appraisal)", "Final Market Value (Appraisal)"), fmt_rp(st.session_state.nilai_pasar_akhir))
        selisih = perbandingan.get("selisih_pct")
        pc3.metric(
            t("Selisih (Appraisal vs Pengajuan)", "Difference (Appraisal vs Proposed)"),
            f"{selisih:+.1f}%" if selisih is not None else "N/A",
            delta=f"{selisih:+.1f}%" if selisih is not None else None,
        )
        if perbandingan.get("lebih_tinggi_dari_pengajuan"):
            st.info(t("ℹ️ Hasil appraisal LEBIH TINGGI dari harga yang diajukan pemilik/pemohon.",
                      "ℹ️ The appraisal result is HIGHER than the price proposed by the owner/applicant."))
        else:
            st.info(t("ℹ️ Hasil appraisal LEBIH RENDAH dari harga yang diajukan pemilik/pemohon.",
                      "ℹ️ The appraisal result is LOWER than the price proposed by the owner/applicant."))
    else:
        st.info(t(
            "Harga yang Diajukan belum diisi di Step 1 - lewati saja bagian ini kalau tidak relevan, "
            "atau kembali ke Step 1 untuk mengisinya kalau ingin perbandingan otomatis di sini.",
            "Proposed Price wasn't filled in at Step 1 - just skip this section if it's not "
            "relevant, or go back to Step 1 to fill it in for an automatic comparison here."
        ))

    col_back, col_next = st.columns(2)
    with col_back:
        if st.button("← Back"):
            goto(8)
    with col_next:
        if st.button("Continue →", type="primary"):
            goto(10)

# ===========================================================================
# STEP 10 - Analisis Rasio NJOP
# ===========================================================================
elif st.session_state.step == 10:
    st.header(t("Step 10 — Analisis Rasio NJOP", "Step 10 — NJOP Ratio Analysis"))
    d = st.session_state.data

    njop_result = calc.analisis_njop(
        d.get("njop_tanah"), d.get("njop_bangunan"), st.session_state.nilai_pasar_akhir
    )
    st.session_state.njop_result = njop_result

    if njop_result.get("available"):
        c1, c2 = st.columns(2)
        c1.metric(t("NJOP Tanah", "NJOP Land"), fmt_rp(njop_result["njop_tanah"]))
        c2.metric(t("NJOP Bangunan", "NJOP Building"), fmt_rp(njop_result["njop_bangunan"]))
        st.metric("Total NJOP", fmt_rp(njop_result["total_njop"]))
        st.metric(t("Rasio NJOP / Nilai Pasar Akhir", "NJOP Ratio / Final Market Value"), f"{njop_result['rasio_njop']*100:.2f}%")
    else:
        st.info("NJOP not available.")

    col_back, col_next = st.columns(2)
    with col_back:
        if st.button("← Back"):
            goto(9)
    with col_next:
        if st.button("Continue →", type="primary"):
            goto(11)

# ===========================================================================
# STEP 11 - Nilai Likuidasi
# ===========================================================================
elif st.session_state.step == 11:
    st.header(t("Step 11 — Nilai Likuidasi", "Step 11 — Liquidation Value"))
    d = st.session_state.data

    st.caption(t(
        "[SOP] Rasio likuidasi default 80% sesuai SOP. Geser kedua ujung slider di bawah bila "
        "appraiser ingin menyajikan Nilai Likuidasi sebagai RENTANG (mis. karena kondisi likuiditas "
        "pasar saat penjualan cepat) - biarkan kedua ujung sama untuk nilai tunggal 80%.",
        "[SOP] Default liquidation ratio is 80% per SOP. Move both ends of the slider below if "
        "the appraiser wants to present the Liquidation Value as a RANGE (e.g. due to market "
        "liquidity conditions for a quick sale) - leave both ends the same for a single 80% value."
    ))
    rasio_min_pct, rasio_max_pct = st.slider(
        t("Rasio Likuidasi (%) - geser jadi rentang bila perlu", "Liquidation Ratio (%) - drag into a range if needed"), 50, 100, (80, 80),
    )
    likuidasi_range = calc.hitung_nilai_likuidasi_range(
        st.session_state.nilai_pasar_akhir, rasio_min_pct / 100.0, rasio_max_pct / 100.0
    )
    st.session_state.nilai_likuidasi = likuidasi_range  # dict {min, max, mid, ...}

    c1, c2 = st.columns(2)
    c1.metric(t("Nilai Pasar Akhir", "Final Market Value"), fmt_rp(st.session_state.nilai_pasar_akhir))
    if rasio_min_pct == rasio_max_pct:
        c2.metric(t("Rasio Likuidasi", "Liquidation Ratio"), f"{rasio_min_pct}%")
        st.metric(t("💧 Nilai Likuidasi", "💧 Liquidation Value"), fmt_rp(likuidasi_range["mid"]))
        st.caption(t("Formula: Nilai Likuidasi = Nilai Pasar Akhir × Rasio Likuidasi",
                     "Formula: Liquidation Value = Final Market Value × Liquidation Ratio"))
    else:
        c2.metric(t("Rentang Rasio Likuidasi", "Liquidation Ratio Range"), f"{rasio_min_pct}% – {rasio_max_pct}%")
        st.metric(
            t("💧 Nilai Likuidasi (Rentang)", "💧 Liquidation Value (Range)"),
            f"{fmt_rp(likuidasi_range['min'])} – {fmt_rp(likuidasi_range['max'])}",
        )
        st.caption(t(f"Titik tengah (mid-point) bila dibutuhkan satu angka: {fmt_rp(likuidasi_range['mid'])}",
                     f"Mid-point if a single figure is needed: {fmt_rp(likuidasi_range['mid'])}"))
        st.caption(t("Formula: Nilai Likuidasi = Nilai Pasar Akhir × Rasio Likuidasi (dihitung untuk batas bawah & atas rentang)",
                     "Formula: Liquidation Value = Final Market Value × Liquidation Ratio (calculated for the range's lower & upper bound)"))

    col_back, col_next = st.columns(2)
    with col_back:
        if st.button("← Back"):
            goto(10)
    with col_next:
        if st.button("Continue →", type="primary"):
            goto(12)

# ===========================================================================
# STEP 12 - Laporan Penilaian Agunan (LPA)
# ===========================================================================
elif st.session_state.step == 12:
    st.header(t("Step 12 — Laporan Penilaian Agunan (LPA) & Ringkasan Hasil Appraisal",
                "Step 12 — Collateral Appraisal Report (LPA) & Appraisal Result Summary"))
    _en = st.session_state.lang == "en"
    d = st.session_state.data
    znt = st.session_state.znt_result
    bgn = st.session_state.bangunan_result
    fp = st.session_state.faktor_pengurang
    val = st.session_state.validasi
    rentang = st.session_state.get("rentang_nilai_pasar") or {}
    njop = st.session_state.njop_result
    likuidasi = st.session_state.get("nilai_likuidasi") or {}
    harga_pengajuan = d.get("harga_pengajuan")
    included = [c for c in st.session_state.comparables if c.get("include")]

    def fmt_likuidasi(lik):
        if isinstance(lik, dict):
            return f"{fmt_rp(lik.get('min', 0))} – {fmt_rp(lik.get('max', 0))} (mid: {fmt_rp(lik.get('mid', 0))})"
        return fmt_rp(lik)

    # Statistik harga tanah/m² dari pembanding (denominator = luas_tanah pembanding,
    # jadi ini murni harga TANAH per m², sebanding dengan Harga ZNT/m² subjek).
    harga_tanah_m2_list = [c.get("harga_tanah_per_m2", 0) for c in included if c.get("harga_tanah_per_m2")]
    stats_tanah_m2 = calc.statistik_pembanding(harga_tanah_m2_list)
    znt_subjek = znt.get("harga_znt_per_m2", 0) or 0
    selisih_znt_vs_pasar = calc.hitung_selisih_pct(stats_tanah_m2["average"], znt_subjek)

    RESTRIKSI_LABELS_EN2 = {
        "sengketa_hukum": "Land in/related to a dispute that can be legally proven and is "
            "registered with the local court.",
        "tanah_adat": "Customary land / communary land/anchesteral land / crooked land.",
        "rawan_bencana": "Area prone to tidal flooding and/or landslides and/or sloped/hillside/"
            "ravine land.",
        "cagar_lindung": "Nature reserve / cultural heritage site / protected forest / wildlife "
            "sanctuary.",
        "jalur_hijau_fasum": "There are plans for, and/or it has become, a green corridor / "
            "public facility / social facility.",
        "pelebaran_jalan": "There are road-widening plans so the land use is no longer optimal "
            "(highest and best use/HBU principle) per its designation.",
        "akses_sempit": "Road width less than 3 meters from the road body, or only an alley, "
            "except for certain marketable areas.",
        "berbatasan_lokasi_berisiko": "Directly bordering and/or part of a family cemetery, "
            "public cemetery, electrical substation, crematorium, funeral home, railway line, "
            "waste dump/landfill, hazardous material storage, or other high-risk location.",
    }

    report_lines = []
    if _en:
        report_lines.append("# Collateral Appraisal Report (LPA)\n")
        report_lines.append("## 1. Property Object Information")
        report_lines.append(f"- Category: {d.get('kategori')}")
        report_lines.append(f"- Address: {d.get('alamat')}, {d.get('kecamatan')}, {d.get('kabkota')}, {d.get('provinsi')}")
        report_lines.append(f"- Land Area: {d.get('luas_tanah')} m² | Building Area: {d.get('luas_bangunan')} m²")
        report_lines.append(f"- Year Built: {d.get('tahun_bangun') or 'Unknown'} | "
                             f"Certificate Status: {d.get('status_sertifikat')}")
        lat_r, lon_r = d.get("lat"), d.get("lon")
        if lat_r and lon_r:
            report_lines.append(f"- Location Coordinates (Latitude, Longitude): {lat_r:.6f}, {lon_r:.6f}\n")
        else:
            report_lines.append("- Location Coordinates: not yet selected\n")

        report_lines.append("## 2. Land Value Result (Bhumi ZNT)")
        report_lines.append(f"- Zone Code: {znt.get('kode_zona')} | ZNT: {znt.get('zona_nilai_tanah')}")
        report_lines.append(f"- ZNT Price/m²: {fmt_rp(znt.get('harga_znt_per_m2'))} | Confidence: {znt.get('confidence_level')}")
        report_lines.append(f"- **Land Value: {fmt_rp(znt.get('nilai_tanah'))}**\n")

        report_lines.append("## 3. Building Value Result (Cost Approach)")
        depresiasi_detail = bgn.get("depresiasi_detail", {})
        metode_label = "Percentage / Declining Balance" if depresiasi_detail.get("metode") == "persentase_tetap" else "Straight Line"
        report_lines.append(f"- Classification: {bgn.get('klasifikasi')} | BRB: {fmt_rp(bgn.get('brb'))}")
        report_lines.append(f"- Depreciation Method: {metode_label}")
        if depresiasi_detail.get("metode") == "persentase_tetap":
            report_lines.append(
                f"- Asset Value: {fmt_rp(depresiasi_detail.get('asset_value'))} | "
                f"Percentage: {depresiasi_detail.get('persentase', 0)*100:.1f}%/year | "
                f"Period: {depresiasi_detail.get('periode')} years"
            )
        else:
            report_lines.append(f"- Building/Economic Age: {bgn.get('umur_bangunan')}/{bgn.get('umur_ekonomis')} years")
        report_lines.append(f"- Total Depreciation: {fmt_rp(bgn.get('penyusutan'))}")
        report_lines.append(f"- **Building Value: {fmt_rp(bgn.get('nilai_bangunan'))}**\n")

        report_lines.append("## 4. Pinpoint Pre-Screening & Reduction Factors")
        active_flags = [k for k, v in st.session_state.auto_flags.items() if v]
        report_lines.append(f"- Risks detected: {', '.join(active_flags) if active_flags else 'None'}")
        report_lines.append(f"- Total Reduction Factor: {fp.get('total_faktor_pengurang', 0)*100:.2f}% | Risk Status: {fp.get('status_risiko')}")
        if fp.get("ada_restriksi"):
            aktif_labels = [RESTRIKSI_LABELS_EN2.get(k, calc.RESTRIKSI_LABELS.get(k, k)) for k in fp.get("restriksi_aktif", [])]
            report_lines.append("- ⚠️ **Restricting Factor/Red-Flag detected** - the property's eligibility as collateral needs review:")
            for lbl in aktif_labels:
                report_lines.append(f"  - {lbl}")
            report_lines.append("")
        else:
            report_lines.append("")

        report_lines.append("## 5. Initial Market Value")
        report_lines.append(f"- **Initial Market Value: {fmt_rp(st.session_state.nilai_pasar_awal)}**\n")

        report_lines.append("## 6. List of Comparable Properties (Included)")
        for c in included:
            dist = c.get("distance_km")
            dist_str = f"{dist:.2f}km" if dist is not None else "distance n/a"
            tgl = c.get("tanggal_upload") or "upload date n/a"
            report_lines.append(
                f"- {c.get('alamat')} | {fmt_rp(c.get('harga'))} | {c.get('luas_tanah')}m² | "
                f"{dist_str} | {tgl} | Similarity: "
                f"{c.get('similarity_score')}{'%' if c.get('similarity_score') is not None else ''}"
            )
        report_lines.append("")

        report_lines.append("## 7. Land Price per m² Comparison")
        report_lines.append(
            f"- ZNT Price/m² (Subject, official/estimated Bhumi ATR-BPN): **{fmt_rp(znt_subjek)}**"
        )
        report_lines.append(
            f"- Comparables' Land Price/m² — Average: {fmt_rp(stats_tanah_m2['average'])} | "
            f"Median: {fmt_rp(stats_tanah_m2['median'])} | "
            f"Min: {fmt_rp(stats_tanah_m2['minimum'])} | Max: {fmt_rp(stats_tanah_m2['maximum'])}"
        )
        if selisih_znt_vs_pasar is not None:
            report_lines.append(
                f"- Difference: Subject ZNT vs Comparables Average: {selisih_znt_vs_pasar:+.1f}%\n"
            )
        else:
            report_lines.append("")

        report_lines.append("## 8. Validation & Market Value Range")
        report_lines.append(f"- Difference (vs comparables average): {val.get('difference_pct')}%")
        if rentang:
            report_lines.append(
                f"- **Market Value Range: {fmt_rp(rentang.get('min'))} – {fmt_rp(rentang.get('max'))}** "
                f"(Estimate Point: {fmt_rp(rentang.get('point'))})\n"
            )
        else:
            report_lines.append("")

        report_lines.append("## 9. Final Market Value & Liquidation Value")
        report_lines.append(f"- **Final Market Value: {fmt_rp(st.session_state.nilai_pasar_akhir)}**")
        report_lines.append(f"- **Liquidation Value: {fmt_likuidasi(likuidasi)}**\n")

        report_lines.append("## 10. NJOP Ratio Analysis")
        if njop.get("available"):
            report_lines.append(f"- Total NJOP: {fmt_rp(njop.get('total_njop'))} | NJOP Ratio: {njop.get('rasio_njop')*100:.2f}%\n")
        else:
            report_lines.append("- NJOP not available.\n")

        report_lines.append("## 11. Final Price Comparison & Depreciation")
        if harga_pengajuan:
            perbandingan_final = calc.bandingkan_harga_pengajuan(harga_pengajuan, st.session_state.nilai_pasar_akhir)
            selisih_final = perbandingan_final.get("selisih_pct")
            report_lines.append(f"- Proposed Price (Step 1): {fmt_rp(harga_pengajuan)}")
            report_lines.append(f"- Final Market Value (Appraisal): {fmt_rp(st.session_state.nilai_pasar_akhir)}")
            report_lines.append(
                f"- Difference Appraisal vs Proposed: {selisih_final:+.1f}%" if selisih_final is not None else "- Difference: N/A"
            )
        else:
            report_lines.append("- Proposed Price was not filled in at Step 1.")
        depresiasi_detail = bgn.get("depresiasi_detail") or {}
        if depresiasi_detail.get("metode") == "persentase_tetap":
            report_lines.append(
                f"- Depreciation Calculator (Step 3) — Building Type (MAPPI 2023 reference): "
                f"{depresiasi_detail.get('jenis_bangunan')} | Percentage: "
                f"{depresiasi_detail.get('persentase', 0)*100:.2f}%/year | "
                f"Period: {depresiasi_detail.get('periode')} {depresiasi_detail.get('unit', 'Tahun').lower()}"
            )
            report_lines.append(
                f"- Asset Value: {fmt_rp(depresiasi_detail.get('asset_value'))} | "
                f"Total Depreciation: {fmt_rp(bgn.get('penyusutan'))} | "
                f"**Building Value: {fmt_rp(bgn.get('nilai_bangunan'))}**\n"
            )
        else:
            report_lines.append("")

        report_lines.append("## 12. Appraisal Result Summary")
        report_lines.append(f"| Item | Value |")
        report_lines.append(f"|---|---|")
        if lat_r and lon_r:
            report_lines.append(f"| Location Coordinates (Lat, Lon) | {lat_r:.6f}, {lon_r:.6f} |")
        report_lines.append(f"| Land Value | {fmt_rp(znt.get('nilai_tanah'))} |")
        report_lines.append(f"| Building Value | {fmt_rp(bgn.get('nilai_bangunan'))} |")
        report_lines.append(f"| Initial Market Value | {fmt_rp(st.session_state.nilai_pasar_awal)} |")
        if rentang:
            report_lines.append(f"| Market Value Range | {fmt_rp(rentang.get('min'))} – {fmt_rp(rentang.get('max'))} |")
        report_lines.append(f"| Final Market Value | {fmt_rp(st.session_state.nilai_pasar_akhir)} |")
        report_lines.append(f"| Liquidation Value | {fmt_likuidasi(likuidasi)} |")
        report_lines.append(f"| ZNT Price/m² (Subject) | {fmt_rp(znt_subjek)} |")
        report_lines.append(f"| Comparables' Land Price/m² (Average) | {fmt_rp(stats_tanah_m2['average'])} |")
        if harga_pengajuan:
            report_lines.append(f"| Proposed Price | {fmt_rp(harga_pengajuan)} |")
    else:
        report_lines.append(f"# Laporan Penilaian Agunan (LPA)\n")
        report_lines.append("## 1. Informasi Objek Properti")
        report_lines.append(f"- Kategori: {d.get('kategori')}")
        report_lines.append(f"- Alamat: {d.get('alamat')}, {d.get('kecamatan')}, {d.get('kabkota')}, {d.get('provinsi')}")
        report_lines.append(f"- Luas Tanah: {d.get('luas_tanah')} m² | Luas Bangunan: {d.get('luas_bangunan')} m²")
        report_lines.append(f"- Tahun Bangun: {d.get('tahun_bangun') or 'Tidak diketahui'} | "
                             f"Status Sertifikat: {d.get('status_sertifikat')}")
        lat_r, lon_r = d.get("lat"), d.get("lon")
        if lat_r and lon_r:
            report_lines.append(f"- Koordinat Lokasi (Latitude, Longitude): {lat_r:.6f}, {lon_r:.6f}\n")
        else:
            report_lines.append("- Koordinat Lokasi: belum dipilih\n")

        report_lines.append("## 2. Hasil Nilai Tanah (Bhumi ZNT)")
        report_lines.append(f"- Kode Zona: {znt.get('kode_zona')} | ZNT: {znt.get('zona_nilai_tanah')}")
        report_lines.append(f"- Harga ZNT/m²: {fmt_rp(znt.get('harga_znt_per_m2'))} | Confidence: {znt.get('confidence_level')}")
        report_lines.append(f"- **Nilai Tanah: {fmt_rp(znt.get('nilai_tanah'))}**\n")

        report_lines.append("## 3. Hasil Nilai Bangunan (Cost Approach)")
        depresiasi_detail = bgn.get("depresiasi_detail", {})
        metode_label = "Persentase Tetap (Declining Balance)" if depresiasi_detail.get("metode") == "persentase_tetap" else "Garis Lurus (Straight Line)"
        report_lines.append(f"- Klasifikasi: {bgn.get('klasifikasi')} | BRB: {fmt_rp(bgn.get('brb'))}")
        report_lines.append(f"- Metode Penyusutan: {metode_label}")
        if depresiasi_detail.get("metode") == "persentase_tetap":
            report_lines.append(
                f"- Asset Value: {fmt_rp(depresiasi_detail.get('asset_value'))} | "
                f"Percentage: {depresiasi_detail.get('persentase', 0)*100:.1f}%/tahun | "
                f"Period: {depresiasi_detail.get('periode')} tahun"
            )
        else:
            report_lines.append(f"- Umur Bangunan/Ekonomis: {bgn.get('umur_bangunan')}/{bgn.get('umur_ekonomis')} tahun")
        report_lines.append(f"- Total Penyusutan: {fmt_rp(bgn.get('penyusutan'))}")
        report_lines.append(f"- **Nilai Bangunan: {fmt_rp(bgn.get('nilai_bangunan'))}**\n")

        report_lines.append("## 4. Pinpoint Pre-Screening & Faktor Pengurang")
        active_flags = [k for k, v in st.session_state.auto_flags.items() if v]
        report_lines.append(f"- Risiko terdeteksi: {', '.join(active_flags) if active_flags else 'Tidak ada'}")
        report_lines.append(f"- Total Faktor Pengurang: {fp.get('total_faktor_pengurang', 0)*100:.2f}% | Status Risiko: {fp.get('status_risiko')}")
        if fp.get("ada_restriksi"):
            aktif_labels = [calc.RESTRIKSI_LABELS.get(k, k) for k in fp.get("restriksi_aktif", [])]
            report_lines.append("- ⚠️ **Faktor Pembatas/Red-Flag terdeteksi** - properti perlu ditinjau ulang kelayakannya sebagai agunan:")
            for lbl in aktif_labels:
                report_lines.append(f"  - {lbl}")
            report_lines.append("")
        else:
            report_lines.append("")

        report_lines.append("## 5. Nilai Pasar Awal")
        report_lines.append(f"- **Nilai Pasar Awal: {fmt_rp(st.session_state.nilai_pasar_awal)}**\n")

        report_lines.append("## 6. Daftar Properti Pembanding (Include)")
        for c in included:
            dist = c.get("distance_km")
            dist_str = f"{dist:.2f}km" if dist is not None else "jarak n/a"
            tgl = c.get("tanggal_upload") or "tgl upload n/a"
            report_lines.append(
                f"- {c.get('alamat')} | {fmt_rp(c.get('harga'))} | {c.get('luas_tanah')}m² | "
                f"{dist_str} | {tgl} | Similarity: "
                f"{c.get('similarity_score')}{'%' if c.get('similarity_score') is not None else ''}"
            )
        report_lines.append("")

        report_lines.append("## 7. Perbandingan Harga Tanah per m²")
        report_lines.append(
            f"- Harga ZNT/m² (Subjek, resmi/estimasi Bhumi ATR-BPN): **{fmt_rp(znt_subjek)}**"
        )
        report_lines.append(
            f"- Harga Tanah/m² Pembanding — Average: {fmt_rp(stats_tanah_m2['average'])} | "
            f"Median: {fmt_rp(stats_tanah_m2['median'])} | "
            f"Min: {fmt_rp(stats_tanah_m2['minimum'])} | Max: {fmt_rp(stats_tanah_m2['maximum'])}"
        )
        if selisih_znt_vs_pasar is not None:
            report_lines.append(
                f"- Selisih ZNT Subjek vs Average Pembanding: {selisih_znt_vs_pasar:+.1f}%\n"
            )
        else:
            report_lines.append("")

        report_lines.append("## 8. Validasi & Rentang Nilai Pasar")
        report_lines.append(f"- Difference (vs average pembanding): {val.get('difference_pct')}%")
        if rentang:
            report_lines.append(
                f"- **Rentang Nilai Pasar: {fmt_rp(rentang.get('min'))} – {fmt_rp(rentang.get('max'))}** "
                f"(Titik Estimasi: {fmt_rp(rentang.get('point'))})\n"
            )
        else:
            report_lines.append("")

        report_lines.append("## 9. Nilai Pasar Akhir & Nilai Likuidasi")
        report_lines.append(f"- **Nilai Pasar Akhir: {fmt_rp(st.session_state.nilai_pasar_akhir)}**")
        report_lines.append(f"- **Nilai Likuidasi: {fmt_likuidasi(likuidasi)}**\n")

        report_lines.append("## 10. Analisis Rasio NJOP")
        if njop.get("available"):
            report_lines.append(f"- Total NJOP: {fmt_rp(njop.get('total_njop'))} | Rasio NJOP: {njop.get('rasio_njop')*100:.2f}%\n")
        else:
            report_lines.append("- NJOP not available.\n")

        report_lines.append("## 11. Perbandingan Harga & Depresiasi Final")
        if harga_pengajuan:
            perbandingan_final = calc.bandingkan_harga_pengajuan(harga_pengajuan, st.session_state.nilai_pasar_akhir)
            selisih_final = perbandingan_final.get("selisih_pct")
            report_lines.append(f"- Harga yang Diajukan (Step 1): {fmt_rp(harga_pengajuan)}")
            report_lines.append(f"- Nilai Pasar Akhir (Appraisal): {fmt_rp(st.session_state.nilai_pasar_akhir)}")
            report_lines.append(
                f"- Selisih Appraisal vs Pengajuan: {selisih_final:+.1f}%" if selisih_final is not None else "- Selisih: N/A"
            )
        else:
            report_lines.append("- Harga yang Diajukan tidak diisi di Step 1.")
        depresiasi_detail = bgn.get("depresiasi_detail") or {}
        if depresiasi_detail.get("metode") == "persentase_tetap":
            report_lines.append(
                f"- Kalkulator Penyusutan (Step 3) — Jenis Bangunan (acuan MAPPI 2023): "
                f"{depresiasi_detail.get('jenis_bangunan')} | Percentage: "
                f"{depresiasi_detail.get('persentase', 0)*100:.2f}%/tahun | "
                f"Period: {depresiasi_detail.get('periode')} {depresiasi_detail.get('unit', 'Tahun').lower()}"
            )
            report_lines.append(
                f"- Asset Value: {fmt_rp(depresiasi_detail.get('asset_value'))} | "
                f"Total Penyusutan: {fmt_rp(bgn.get('penyusutan'))} | "
                f"**Nilai Bangunan: {fmt_rp(bgn.get('nilai_bangunan'))}**\n"
            )
        else:
            report_lines.append("")

        report_lines.append("## 12. Ringkasan Hasil Appraisal")
        report_lines.append(f"| Item | Nilai |")
        report_lines.append(f"|---|---|")
        if lat_r and lon_r:
            report_lines.append(f"| Koordinat Lokasi (Lat, Lon) | {lat_r:.6f}, {lon_r:.6f} |")
        report_lines.append(f"| Nilai Tanah | {fmt_rp(znt.get('nilai_tanah'))} |")
        report_lines.append(f"| Nilai Bangunan | {fmt_rp(bgn.get('nilai_bangunan'))} |")
        report_lines.append(f"| Nilai Pasar Awal | {fmt_rp(st.session_state.nilai_pasar_awal)} |")
        if rentang:
            report_lines.append(f"| Rentang Nilai Pasar | {fmt_rp(rentang.get('min'))} – {fmt_rp(rentang.get('max'))} |")
        report_lines.append(f"| Nilai Pasar Akhir | {fmt_rp(st.session_state.nilai_pasar_akhir)} |")
        report_lines.append(f"| Nilai Likuidasi | {fmt_likuidasi(likuidasi)} |")
        report_lines.append(f"| Harga ZNT/m² (Subjek) | {fmt_rp(znt_subjek)} |")
        report_lines.append(f"| Harga Tanah/m² Pembanding (Average) | {fmt_rp(stats_tanah_m2['average'])} |")
        if harga_pengajuan:
            report_lines.append(f"| Harga yang Diajukan | {fmt_rp(harga_pengajuan)} |")

    report_md = "\n".join(report_lines)
    st.markdown(report_md)

    st.download_button(
        t("⬇️ Download Laporan (Markdown)", "⬇️ Download Report (Markdown)"), data=report_md,
        file_name="Laporan_Penilaian_Agunan.md", mime="text/markdown",
    )

    col_back, col_restart = st.columns(2)
    with col_back:
        if st.button("← Back"):
            goto(11)
    with col_restart:
        if st.button(t("🔄 Mulai Penilaian Baru", "🔄 Start New Appraisal")):
            for k in list(st.session_state.keys()):
                if k not in ("serper_key", "groq_key", "gemini_key"):
                    del st.session_state[k]
            st.rerun()
