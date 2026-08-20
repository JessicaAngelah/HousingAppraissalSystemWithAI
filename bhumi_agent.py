import os
import sys
import re
import json
import argparse
import asyncio
from playwright.async_api import async_playwright

def _t(lang: str, id_text: str, en_text: str) -> str:
    """Pick the Indonesian or English string depending on `lang` ('id'/'en')."""
    return en_text if lang == "en" else id_text


class BhumiZntAgent:
    def __init__(self, lat, lng, headed=False, api_key=None, log_callback=None, lang="id"):
        self.lat = lat
        self.lng = lng
        self.headed = headed
        self.lang = lang
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.client = None
        # log_callback(message: str) is optional - if provided (e.g. from Streamlit),
        # each log line is also forwarded there in addition to stdout.
        self.log_callback = log_callback
        if self.api_key:
            try:
                from google import genai
                self.client = genai.Client(api_key=self.api_key)
            except ImportError:
                self.log_step("[Warning] google-genai package is missing. AI formatting is disabled.")

    def log_step(self, message):
        """Standard warning-free log method."""
        print(f"[*] {message}", flush=True)
        if self.log_callback:
            try:
                self.log_callback(message)
            except Exception:
                pass

    def t(self, id_text, en_text):
        return _t(self.lang, id_text, en_text)

    def clean_text_extraction(self, text_context):
        """Utilizes Gemini 2.5 Flash only at the very end to clean the extracted raw text."""
        if not self.client:
            return parse_standard_znt_fields(text_context)
            
        from google.genai import types
        prompt = f"""
Convert the following Indonesian land value details into standard JSON structure.
Fields:
- kode_zona
- nilai_min
- nilai_max
- tahun (integer)
- kelurahan
- kecamatan
- kabkota

Input context:
{text_context}

Return JSON match:
{{
  "kode_zona": string or null,
  "nilai_min": string or null,
  "nilai_max": string or null,
  "tahun": integer or null,
  "kelurahan": string or null,
  "kecamatan": string or null,
  "kabkota": string or null
}}
Return raw JSON only. Do not wrap in markdown blocks.
"""
        try:
            response = self.client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            return json.loads(response.text.strip())
        except Exception as e:
            self.log_step(f"AI parsing skipped/failed (using direct native values): {e}")
            return parse_standard_znt_fields(text_context)

    async def _try_point(self, page, lat, lng, label=""):
        """
        Perform the "search coordinate -> click marker -> read popup" cycle
        for a single (lat, lng) on a page where the ZNT layer is already
        active. Returns whatever popup text was found (possibly empty).

        Split out from run() so a single lat/lng miss (e.g. the exact pin
        lands in a gap between ZNT polygons - common when the point comes
        from geocoding an address or a map click, right on a gang/alley or
        a plot boundary) can be retried at a few nearby offsets without
        re-navigating the whole site / re-enabling the layer each time.
        """
        suffix = f" ({label})" if label else ""
        self.log_step(f"Searching location coordinates{suffix}: Lat={lat}, Lng={lng}")

        # 4. Search and center on coordinate
        try:
            # 4.1 Press coordinate dropdown trigger button containing search map pin SVG path "M 19 3"
            trigger_btn = page.locator("button").filter(has=page.locator("path[d*='M 19 3']")).first
            await trigger_btn.click()
            await page.wait_for_timeout(1000)

            # 4.2 Click "Pencarian Koordinat" option
            coord_menu_item = page.locator(".intro-menukoordinat").first
            if not await coord_menu_item.is_visible():
                coord_menu_item = page.get_by_text("Pencarian Koordinat").first
            await coord_menu_item.click()
            await page.wait_for_timeout(1000)

            # 4.3 Fill in Longitude
            longitude_input = page.locator("#longitude").first
            await longitude_input.fill(str(lng))

            # 4.4 Fill in Latitude
            latitude_input = page.locator("#latitude").first
            await latitude_input.fill(str(lat))

            # 4.5 Press "Cari Koordinat" button
            cari_koordinat_btn = page.locator(".intro-carikoordinat, button[data-umami-event='pencarian-koordinat']").first
            await cari_koordinat_btn.click()

            # EXTENDED ANIMATION BUFFER
            await page.wait_for_timeout(5000)
        except Exception as e:
            self.log_step(f"Coordinate search automation failed{suffix}: {e}")
            return ""

        # 5. Click the routing marker to reveal the popup details
        try:
            marker = page.locator("img[src*='ic-routing-marker.svg']").first
            await marker.wait_for(state="visible", timeout=12000)

            viewport = page.viewport_size
            vw = viewport["width"] if viewport else 1280
            vh = viewport["height"] if viewport else 720

            last_box = None
            for _ in range(16):
                box = await marker.bounding_box()
                if box:
                    is_inside_viewport = (0 <= box["x"] <= vw) and (0 <= box["y"] <= vh)
                    is_stable = last_box and abs(box["x"] - last_box["x"]) < 1 and abs(box["y"] - last_box["y"]) < 1
                    if is_inside_viewport and is_stable:
                        break
                last_box = box
                await page.wait_for_timeout(500)

            marker_parent = page.locator(".mapboxgl-marker, .leaflet-marker-icon").filter(has=page.locator("img[src*='ic-routing-marker.svg']")).first
            click_target = marker
            if await marker_parent.is_visible():
                click_target = marker_parent

            await click_target.click(force=True, timeout=5000)

            popup_loaded = False
            for selector in [".mapboxgl-popup-content", ".leaflet-popup-content", ".info-window", ".panel"]:
                try:
                    popup_el = page.locator(selector).first
                    await popup_el.wait_for(state="visible", timeout=8000)
                    for _ in range(20):
                        txt = await popup_el.text_content()
                        if txt and any(k in txt for k in ["Zone", "Nilai", "Kantor", "Tahun", "Rp", "Zona"]):
                            popup_loaded = True
                            break
                        await page.wait_for_timeout(500)
                    if popup_loaded:
                        break
                except Exception:
                    pass
        except Exception as e:
            self.log_step(f"Marker selection click failed{suffix}: {e}")

        # 6. Gather page text elements from active popup overlay
        extracted_text = ""
        for selector in [
            ".mapboxgl-popup-content",    # Mapbox GL standard popup container
            ".leaflet-popup-content",     # Leaflet standard popup container
            ".info-window",
            ".sidebar",
            ".panel",
            "div:has-text('Nama Kantor')"
        ]:
            try:
                elements = page.locator(selector)
                count = await elements.count()
                for i in range(count):
                    txt = await elements.nth(i).text_content()
                    if txt:
                        extracted_text += "\n" + txt.strip()
            except:
                pass
        return extracted_text

    async def run(self):
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=not self.headed)
            except Exception as e:
                # PENTING: di server managed seperti Streamlit Community
                # Cloud, "pip install playwright" (dari requirements.txt)
                # HANYA memasang package Python-nya - binary browser Chromium
                # itu sendiri (ratusan MB) TIDAK ikut terunduh otomatis dan
                # butuh perintah terpisah "playwright install chromium" yang
                # TIDAK ADA cara resminya dijalankan otomatis di server
                # seperti itu (tidak ada akses shell/terminal untuk appraiser).
                # Makanya di sini kita coba jalankan sendiri secara otomatis
                # SEKALI saat pertama kali dibutuhkan (biasanya makan waktu
                # 20-60 detik tergantung koneksi server) - kalau berhasil,
                # percobaan berikutnya (appraiser lain / sesi lain di server
                # yang sama) akan langsung cepat karena binary-nya sudah ada
                # di cache. Kalau gagal juga (mis. server tidak izinkan
                # subprocess, atau read-only filesystem), tetap lempar error
                # supaya run_znt_agent di agents.py bisa fallback ke estimasi
                # pencarian web seperti biasa - JANGAN sampai gagal diam-diam.
                if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
                    self.log_step(self.t(
                        "Chromium belum terpasang di server - mencoba unduh otomatis "
                        "(sekali saja, bisa makan waktu ~30-60 detik)...",
                        "Chromium is not installed on the server - attempting an automatic "
                        "download (one-time, may take ~30-60 seconds)..."))
                    import subprocess
                    import sys
                    try:
                        result = subprocess.run(
                            [sys.executable, "-m", "playwright", "install", "chromium"],
                            capture_output=True, text=True, timeout=180,
                        )
                        if result.returncode != 0:
                            self.log_step(self.t(
                                f"⚠ Gagal mengunduh Chromium otomatis: {result.stderr[-300:]}",
                                f"⚠ Failed to auto-download Chromium: {result.stderr[-300:]}"))
                            raise
                        self.log_step(self.t("✓ Chromium berhasil diunduh, mencoba lagi...",
                                              "✓ Chromium downloaded successfully, retrying..."))
                        browser = await p.chromium.launch(headless=not self.headed)
                    except Exception:
                        raise e
                else:
                    raise
            page = await browser.new_page()
            
            # Intercept network responses in case we catch raw ZNT data
            network_znt_data = []
            async def handle_response(res):
                if "znt" in res.url.lower() or "zonanilaitanah" in res.url.lower():
                    try:
                        text = await res.text()
                        if "kode" in text.lower() or "nilai" in text.lower():
                            network_znt_data.append(text)
                    except:
                        pass
            page.on("response", handle_response)

            self.log_step("Navigating to Bhumi ATR/BPN...")
            await page.goto("https://bhumi.atrbpn.go.id/peta", timeout=60000)
            await page.wait_for_timeout(3000)

            # 1. Handle Legal Disclaimer Pop-up
            try:
                setuju_btn = page.get_by_role("button", name="Saya Setuju").first
                if await setuju_btn.is_visible():
                    await setuju_btn.click()
                    self.log_step("Accepted disclaimer.")
            except:
                pass

            # 2. Handle Onboarding/Tutorial Pop-ups ("Lewati")
            self.log_step("Checking for onboarding pop-up...")
            for selector in ["button:has-text('Lewati')", "text=Lewati", "//button[contains(., 'Lewati')]"]:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible():
                        try:
                            checkbox = page.locator("input[type='checkbox']").first
                            if await checkbox.is_visible():
                                await checkbox.check(timeout=1000)
                        except:
                            pass
                        await btn.click()
                        self.log_step("Dismissed onboarding popup.")
                        break
                except Exception:
                    pass

            # 3. Enable Zona Nilai Tanah Layer
            self.log_step("Enabling Zona Nilai Tanah (ZNT) layer...")
            try:
                # Open Sidebar Menu
                menu_btn = page.locator("button:has-text('Menu'), .menu-toggle, #menu-btn").first
                if await menu_btn.is_visible():
                    await menu_btn.click()
                    await page.wait_for_timeout(1000)
                    
                # Open Catalogue Menu
                katalog_btn = page.get_by_text("Katalog Data").first
                if await katalog_btn.is_visible():
                    await katalog_btn.click()
                    self.log_step("Opened data catalog dialog.")
                    await page.wait_for_timeout(2500)

                # 3.1 Click "Dataset Utama" folder item
                dataset_utama_folder = page.get_by_text("Dataset Utama").first
                if await dataset_utama_folder.is_visible():
                    await dataset_utama_folder.click()
                    self.log_step("Opened 'Dataset Utama' folder.")
                    await page.wait_for_timeout(1000)

                # 3.2 Search "Zona Nilai Tanah" in search input
                search_dataset = page.locator("input[placeholder='Cari Dataset']").first
                if await search_dataset.is_visible():
                    await search_dataset.click()
                    await search_dataset.fill("Zona Nilai Tanah")
                    await search_dataset.press("Enter")
                    self.log_step("Searched for 'Zona Nilai Tanah'.")
                    await page.wait_for_timeout(1500)

                # 3.3 Click "Tambah Data" button
                tambah_btn = page.locator("button[data-umami-event='tambah-layer-Zona Nilai Tanah']").first
                kurangi_btn = page.locator("button[data-umami-event='kurangi-layer-Zona Nilai Tanah']").first
                
                if await tambah_btn.is_visible():
                    await tambah_btn.click()
                    self.log_step("Clicked 'Tambah Data' for Zona Nilai Tanah.")
                    await page.wait_for_timeout(1000)
                elif await kurangi_btn.is_visible():
                    self.log_step("ZNT layer is already active (Kurangi Data is visible).")

                # 3.4 Dismiss the dialog
                terapkan_btn = page.locator("button.intro-terapkan, button:has-text('Terapkan Pada Peta')").first
                if await terapkan_btn.is_visible() and not await terapkan_btn.is_disabled():
                    await terapkan_btn.click()
                    self.log_step("Clicked 'Terapkan Pada Peta'.")
                    await page.wait_for_timeout(1000)
            except Exception as e:
                self.log_step(f"Error while enabling layer standard UI logic: {e}")

            # 3.5 STRICT CLOSE ROUTINE FOR MODAL DISMISSAL
            self.log_step("Ensuring Katalog Data modal is closed...")
            modal_closed = False
            for selector in [
                "button:has(svg path[d*='M12 .707'])",        # SVG close 'X' path
                "button:has(svg clipPath#ic-cross_svg__a)",   # SVG close cross clip-path
                "button:has-text('×')",                       # Text symbol '×'
                "button:has-text('Kembali')",                 # Kembali button
                "button.close-modal"
            ]:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(1000)
                        if not await page.locator("text=Katalog Data").is_visible():
                            self.log_step(f"Modal closed via selector: '{selector}'")
                            modal_closed = True
                            break
                except:
                    pass

            if not modal_closed:
                self.log_step("Modal remains open. Forcing close by clicking off-screen...")
                await page.mouse.click(10, 10)
                await page.wait_for_timeout(1500)

            # Wait for modal overlay portal to fully hidden/detach to prevent click interception
            try:
                await page.locator("text=Katalog Data").wait_for(state="hidden", timeout=5000)
                await page.locator("#headlessui-portal-root").wait_for(state="hidden", timeout=5000)
                self.log_step("Catalog modal fully hidden and backdrop overlay detached.")
            except:
                pass

            # 4-6. Search coordinate -> click marker -> read popup, with a
            # small "nudge" retry. An exact point coming from a geocoded
            # address or a map tap can land right in a gap between ZNT
            # polygons (e.g. on a gang/alley or a plot boundary) even though
            # the surrounding block does have ZNT data - a manually-typed
            # lat/long tends to avoid this only by luck. Try the exact point
            # first, then a handful of nearby offsets (~25-55m) before
            # accepting that this spot has no data.
            offsets = [
                (0.0, 0.0, self.t("titik asli", "original point")),
                (0.00025, 0.0, self.t("geser ~25m ke utara", "shifted ~25m north")),
                (-0.00025, 0.0, self.t("geser ~25m ke selatan", "shifted ~25m south")),
                (0.0, 0.00025, self.t("geser ~25m ke timur", "shifted ~25m east")),
                (0.0, -0.00025, self.t("geser ~25m ke barat", "shifted ~25m west")),
                (0.0004, 0.0004, self.t("geser ~55m ke timur laut", "shifted ~55m northeast")),
                (-0.0004, -0.0004, self.t("geser ~55m ke barat daya", "shifted ~55m southwest")),
            ]
            extracted_text = ""
            for i, (dlat, dlng, label) in enumerate(offsets):
                text = await self._try_point(page, self.lat + dlat, self.lng + dlng, label=label)
                parsed_probe = parse_standard_znt_fields(text)
                extracted_text = text
                if parsed_probe.get("kode_zona") or parsed_probe.get("nilai_min") or parsed_probe.get("nilai_max"):
                    if i == 0:
                        self.log_step("Popup data successfully populated.")
                    else:
                        self.log_step(self.t(f"Data ZNT ditemukan setelah {label}.",
                                              f"ZNT data found after {label}."))
                    break
                elif i < len(offsets) - 1:
                    self.log_step(self.t(
                        f"Tidak ada data ZNT di titik ini ({label}), mencoba titik terdekat...",
                        f"No ZNT data at this point ({label}), trying a nearby point..."))
                else:
                    self.log_step(self.t(
                        "Dynamic popup wait timeout exceeded / tidak ada data ZNT di sekitar titik ini "
                        "setelah beberapa percobaan.",
                        "Dynamic popup wait timeout exceeded / no ZNT data around this point after "
                        "several attempts."))

            # Parse results offline and structure JSON
            result_json = parse_standard_znt_fields(extracted_text)
            
            # Use Gemini as clean/format fallback only
            if extracted_text.strip():
                result_json = self.clean_text_extraction(extracted_text)

            await browser.close()
            return result_json

def parse_standard_znt_fields(text):
    data = {
        "kode_zona": None,
        "nilai_min": None,
        "nilai_max": None,
        "tahun": None,
        "kelurahan": None,
        "kecamatan": None,
        "kabkota": None
    }

    # The text scraped from the Bhumi popup often has NO separator (not even a
    # space) between one field's value and the next field's label - e.g.
    # "Nomor Zone: 1407Tahun Dibuat: 2025Range Nilai: 2.000.000 - 5.000.000
    # Peta GoogleBeri UlasanKoordinat: -6.360201, 106.876270". A plain
    # `[^\n]+` capture (the old approach) has nothing to stop it and swallows
    # everything up to the end of that single line, producing garbage like
    # "1407Tahun Dibuat: 2025Range Nilai: ...Koordinat: -6.360201...".
    #
    # Fix: bound every capture with a lookahead for the next known label (or
    # end of string) so the match stops exactly where the next field begins,
    # even with zero whitespace between them.
    NEXT_LABELS = [
        "Nomor Zone", "Tahun Dibuat", "Nama Kantor", "Range Nilai",
        "Kode Zona", "Nilai Min", "Nilai Minimum", "Nilai Max", "Nilai Maksimum",
        "Kelurahan", "Desa", "Kecamatan", "Peta Google", "Beri Ulasan",
        "Tambahkan ke Koleksi", "Koordinat", "Rating", "Informasi data layer",
    ]
    boundary = "|".join(re.escape(l) for l in NEXT_LABELS)

    def bounded(label_pattern):
        m = re.search(rf"{label_pattern}\s*:\s*(.+?)(?=(?:{boundary})|$)", text, re.IGNORECASE)
        return m.group(1).strip(" \t\n-–—") if m else None

    # 1. Nomor Zone -> kode_zona
    data["kode_zona"] = bounded("Nomor Zone")

    # 2. Tahun Dibuat -> tahun
    tahun_match = re.search(r"Tahun Dibuat\s*:\s*(\d{4})", text, re.IGNORECASE)
    if tahun_match:
        data["tahun"] = int(tahun_match.group(1).strip())

    # 3. Nama Kantor -> kabkota
    data["kabkota"] = bounded("Nama Kantor")

    # 4. Range Nilai -> nilai_min, nilai_max
    val_str = bounded("Range Nilai")
    if val_str:
        # Trim anything after the actual numeric range in case the boundary
        # lookahead still let through trailing junk (e.g. no space before a
        # label we don't know about) - keep only digits/./,/space/-/<>.
        num_match = re.match(r"[\d.,\s<>-]+", val_str)
        if num_match:
            val_str = num_match.group(0).strip()
        if ">" in val_str:
            data["nilai_min"] = val_str.replace(">", "").strip()
            data["nilai_max"] = "Max"
        elif "<" in val_str:
            data["nilai_min"] = "0"
            data["nilai_max"] = val_str.replace("<", "").strip()
        elif "-" in val_str:
            parts = val_str.split("-")
            data["nilai_min"] = parts[0].strip()
            data["nilai_max"] = parts[1].strip()
        else:
            data["nilai_min"] = val_str
            data["nilai_max"] = val_str

    # General fallbacks
    if not data["kode_zona"]:
        data["kode_zona"] = bounded("(?:Kode Zona|Kode)")

    if not data["nilai_min"]:
        data["nilai_min"] = bounded("(?:Nilai Min|Nilai Minimum)")

    if not data["nilai_max"]:
        data["nilai_max"] = bounded("(?:Nilai Max|Nilai Maksimum)")

    tahun_fallback = re.search(r"(?:Tahun)\s*:\s*(\d{4})", text, re.IGNORECASE)
    if not data["tahun"] and tahun_fallback:
        data["tahun"] = int(tahun_fallback.group(1).strip())

    data["kelurahan"] = bounded("(?:Kelurahan|Desa)")
    data["kecamatan"] = bounded("(?:Kecamatan)")

    return data

def parse_rupiah(value):
    """
    Parse strings like 'Rp 1.500.000', '1.500.000', '>3.000.000', 'Max', '0'
    into a float. Returns None if it can't parse anything numeric.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s.lower() == "max":
        return None
    s = s.replace("Rp", "").replace("rp", "").replace(">", "").replace("<", "").strip()
    # Indonesian formatting uses '.' as thousands separator and ',' as decimal
    s = s.replace(".", "").replace(",", ".")
    match = re.search(r"[\d.]+", s)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def map_bhumi_result_to_app_schema(bhumi_result: dict, lang: str = "id") -> dict:
    """
    Converts BhumiZntAgent.run() output (kode_zona, nilai_min, nilai_max, tahun,
    kelurahan, kecamatan, kabkota) into the schema app.py / agents.py expect
    for Step 2 (kode_zona, zona_nilai_tanah, harga_znt_per_m2, tanggal_data,
    confidence_level, source_notes).
    """
    nilai_min = parse_rupiah(bhumi_result.get("nilai_min"))
    nilai_max = parse_rupiah(bhumi_result.get("nilai_max"))

    if nilai_min is not None and nilai_max is not None:
        harga_znt_per_m2 = round((nilai_min + nilai_max) / 2, 0)
    elif nilai_min is not None:
        harga_znt_per_m2 = nilai_min
    elif nilai_max is not None:
        harga_znt_per_m2 = nilai_max
    else:
        harga_znt_per_m2 = 0

    if nilai_min is not None and nilai_max is not None:
        zona_nilai_tanah = f"Rp {nilai_min:,.0f} - Rp {nilai_max:,.0f}".replace(",", ".")
    elif harga_znt_per_m2:
        zona_nilai_tanah = f"Rp {harga_znt_per_m2:,.0f}".replace(",", ".")
    else:
        zona_nilai_tanah = "-"

    if lang == "en":
        confidence_level = ("High (official Bhumi ATR/BPN data)" if harga_znt_per_m2
                             else "Low (ZNT layer empty at this point)")
        source_notes = (
            f"Kelurahan (village): {bhumi_result.get('kelurahan') or '-'}, "
            f"Kecamatan (district): {bhumi_result.get('kecamatan') or '-'}, "
            f"Office: {bhumi_result.get('kabkota') or '-'} "
            f"(taken directly from the Bhumi ATR/BPN map)"
        )
    else:
        confidence_level = ("Tinggi (data resmi Bhumi ATR/BPN)" if harga_znt_per_m2
                             else "Rendah (layer ZNT kosong di titik ini)")
        source_notes = (
            f"Kelurahan: {bhumi_result.get('kelurahan') or '-'}, "
            f"Kecamatan: {bhumi_result.get('kecamatan') or '-'}, "
            f"Kantor: {bhumi_result.get('kabkota') or '-'} "
            f"(diambil langsung dari peta Bhumi ATR/BPN)"
        )

    return {
        "kode_zona": bhumi_result.get("kode_zona") or "-",
        "zona_nilai_tanah": zona_nilai_tanah,
        "harga_znt_per_m2": harga_znt_per_m2,
        "tanggal_data": str(bhumi_result.get("tahun") or "-"),
        "confidence_level": confidence_level,
        "source_notes": source_notes,
    }


def run_bhumi_znt_sync(lat, lng, headed=False, api_key=None, log_callback=None, lang="id"):
    """
    Synchronous wrapper around BhumiZntAgent.run(), safe to call from a plain
    (non-async) context like a Streamlit script. Returns the raw dict from
    BhumiZntAgent (kode_zona, nilai_min, nilai_max, tahun, kelurahan,
    kecamatan, kabkota).
    """
    agent = BhumiZntAgent(lat=lat, lng=lng, headed=headed, api_key=api_key,
                           log_callback=log_callback, lang=lang)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're already inside an event loop (rare for Streamlit's main script
        # thread, but can happen with some setups) - run in a fresh thread
        # with its own loop instead of nesting asyncio.run().
        import concurrent.futures

        def _runner():
            return asyncio.run(agent.run())

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_runner).result()

    return asyncio.run(agent.run())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bhumi ATR/BPN ZNT Extraction Agent")
    parser.add_argument("--lat", type=float, required=True, help="Latitude coordinate")
    parser.add_argument("--lng", type=float, required=True, help="Longitude coordinate")
    parser.add_argument("--headed", action="store_true", help="Launch visual browser UI instead of headless mode")
    args = parser.parse_args()

    agent = BhumiZntAgent(lat=args.lat, lng=args.lng, headed=args.headed)
    result = asyncio.run(agent.run())
    print("\n--- EXTRACTED JSON RESULT ---")
    print(json.dumps(result, indent=2))