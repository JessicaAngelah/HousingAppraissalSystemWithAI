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
            ai = json.loads(response.text.strip())
            if not isinstance(ai, dict):
                raise ValueError("Gemini returned non-object JSON")
            # Nilai dari parser regex (deterministik) diutamakan; Gemini hanya
            # dipakai untuk mengisi yang kosong - jangan sampai Gemini yang
            # mengembalikan null/format aneh menimpa hasil regex yang benar.
            merged = parse_standard_znt_fields(text_context)
            for k, v in ai.items():
                if merged.get(k) in (None, "") and v not in (None, ""):
                    merged[k] = v
            return merged
        except Exception as e:
            self.log_step(f"AI parsing skipped/failed (using direct native values): {e}")
            return parse_standard_znt_fields(text_context)


    async def _dump_buttons(self, page, limit=60):
        """Tulis daftar tombol yang terlihat (teks/aria/data-umami-event/title) ke log,
        supaya kelihatan persis UI Bhumi saat ini kalau selector gagal."""
        try:
            items = await page.evaluate("""() => Array.from(document.querySelectorAll('button, [role=button], a'))
                .filter(e => e.offsetParent !== null)
                .map(e => [e.tagName, (e.innerText||'').trim().slice(0,30), e.getAttribute('aria-label')||'',
                           e.getAttribute('data-umami-event')||'', e.getAttribute('title')||'',
                           (e.className && e.className.toString ? e.className.toString() : '').slice(0,40)])""")
            self.log_step(f"[DEBUG] {len(items)} tombol terlihat:")
            for it in items[:limit]:
                self.log_step("[DEBUG] " + " | ".join(str(x) for x in it))
        except Exception as e:
            self.log_step(f"[DEBUG] gagal dump tombol: {e}")

    async def _click_by_keyword(self, page, keywords):
        """Klik elemen (tombol/link) pertama yang atribut/teksnya memuat salah satu kata kunci."""
        return await page.evaluate("""(kws) => {
            const els = Array.from(document.querySelectorAll('button, [role=button], a, li, div[class*=menu]'));
            for (const e of els) {
              const hay = ((e.innerText||'') + ' ' + (e.getAttribute('aria-label')||'') + ' ' +
                           (e.getAttribute('data-umami-event')||'') + ' ' + (e.getAttribute('title')||'') + ' ' +
                           (e.className||'')).toLowerCase();
              if (kws.some(k => hay.includes(k)) && e.offsetParent !== null && (e.innerText||'').length < 60) {
                e.click(); return true; }
            }
            return false; }""", [k.lower() for k in keywords])


    async def _open_coordinate_dialog(self, page):
        """Buka dialog 'Pencarian Koordinat'. UI Bhumi sudah berubah (tombol ikon tanpa
        label), jadi: coba jalur lama dulu, lalu coba klik satu per satu tombol ikon /
        tombol menu dan cek apakah muncul 'Pencarian Koordinat' atau input #longitude."""
        lng_input = page.locator("#longitude").first

        async def dialog_open():
            try:
                return await lng_input.is_visible()
            except Exception:
                return False

        async def try_menu_item():
            for loc in (page.locator(".intro-menukoordinat").first,
                        page.get_by_text("Pencarian Koordinat").first):
                try:
                    if await loc.is_visible():
                        await loc.click(timeout=3000)
                        await page.wait_for_timeout(800)
                        if await dialog_open():
                            return True
                except Exception:
                    pass
            return await dialog_open()

        if await dialog_open():
            return True

        # a) jalur lama (ikon SVG 'M 19 3')
        try:
            old_btn = page.locator("button").filter(has=page.locator("path[d*='M 19 3']")).first
            if await old_btn.count() > 0:
                await old_btn.click(timeout=3000)
                await page.wait_for_timeout(800)
                if await try_menu_item():
                    return True
        except Exception:
            pass

        # a2) UI baru: menu samping -> tombol "Pencarian" -> "Pencarian Koordinat"
        try:
            pencarian = page.locator("button").filter(has_text=re.compile(r"^\s*Pencarian\s*$")).first
            if not await pencarian.is_visible():
                menu_toggle = page.locator("button[title='Buka Menu']").first
                if await menu_toggle.count() > 0:
                    await menu_toggle.click(force=True, timeout=3000)
                    await page.wait_for_timeout(1000)
            if await pencarian.is_visible():
                await pencarian.click(timeout=3000)
                await page.wait_for_timeout(1200)
                self.log_step("Menu 'Pencarian' dibuka.")
                if await try_menu_item():
                    return True
                # mungkin ada tab/opsi 'Koordinat' di panel pencarian
                for loc in (page.get_by_text("Koordinat", exact=True).first,
                            page.get_by_text(re.compile(r"koordinat", re.I)).first):
                    try:
                        if await loc.is_visible():
                            await loc.click(timeout=3000)
                            await page.wait_for_timeout(1000)
                            if await dialog_open():
                                return True
                    except Exception:
                        pass
                try:
                    body = await page.locator("body").inner_text(timeout=3000)
                    self.log_step("[DEBUG] teks halaman setelah klik Pencarian: " + body[:700].replace("\n", " | "))
                except Exception:
                    pass
            else:
                self.log_step("[DEBUG] tombol 'Pencarian' tidak terlihat di menu samping.")
        except Exception as e:
            self.log_step(f"[DEBUG] jalur menu Pencarian gagal: {e}")

        # b) tombol ikon tanpa teks (UI baru) + tombol menu lain
        handles = await page.query_selector_all("button")
        cands = []
        for h in handles:
            try:
                if not await h.is_visible():
                    continue
                info = await h.evaluate("""e => [(e.innerText||'').trim(), e.getAttribute('title')||'',
                                                  e.getAttribute('aria-label')||'']""")
                txt, title, aria = info
                label = (title or aria or txt).lower()
                skip = ("tutup", "masuk", "rating", "reset utara", "tampilan 3d", "bangunan 3d",
                        "lokasi saya", "analisis", "google maps", "ulasan", "favorit",
                        "toggle attribution", "meja kerja", "katalog", "layers")
                if any(s in label for s in skip):
                    continue
                cands.append(h)
            except Exception:
                continue
        self.log_step(f"Mencari tombol pencarian koordinat: {len(cands)} kandidat...")
        for n, h in enumerate(cands):
            try:
                await h.click(timeout=2000)
                await page.wait_for_timeout(700)
                if await try_menu_item():
                    self.log_step(f"Dialog koordinat terbuka lewat kandidat #{n + 1}.")
                    return True
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
            except Exception:
                continue
        return False

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
            if not await self._open_coordinate_dialog(page):
                await self._dump_buttons(page)
                raise RuntimeError("Tombol pencarian koordinat tidak ditemukan di UI Bhumi (lihat daftar [DEBUG] di atas)")

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
            if "tidak ditemukan di UI" in str(e):
                self._ui_broken = True
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
        # Fallback: kalau selector popup tidak menangkap apa-apa (class CSS Bhumi
        # berubah), ambil teks seluruh halaman dan ambil bagian yang memuat label ZNT.
        if not any(k in extracted_text for k in ("Nomor Zone", "Range Nilai", "Kode Zona")):
            try:
                body = await page.locator("body").inner_text(timeout=5000)
                idx = body.find("Nomor Zone")
                if idx == -1:
                    idx = body.find("Range Nilai")
                if idx != -1:
                    extracted_text += "\n" + body[max(0, idx - 200): idx + 800]
                    self.log_step(self.t("Popup tidak terbaca lewat selector - memakai teks halaman.",
                                         "Popup not readable via selectors - using page text."))
            except Exception:
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
            # Pakai pengaturan browser default (1280x720) - ini yang sebelumnya terbukti
            # cocok dengan tata letak tombol Bhumi; jangan diubah tanpa tes.
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
            try:
                await page.goto("https://bhumi.atrbpn.go.id/peta", timeout=60000)
            except Exception as e:
                await browser.close()
                if "ERR_NAME_NOT_RESOLVED" in str(e) or "ERR_INTERNET_DISCONNECTED" in str(e):
                    raise RuntimeError(self.t(
                        "Komputer/server ini tidak bisa menjangkau bhumi.atrbpn.go.id (DNS gagal). "
                        "Cek internet/VPN/proxy, atau buka https://bhumi.atrbpn.go.id/peta di browser biasa.",
                        "This machine/server cannot reach bhumi.atrbpn.go.id (DNS failure). "
                        "Check internet/VPN/proxy, or open https://bhumi.atrbpn.go.id/peta in a normal browser."))
                raise
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
                # UI baru: tombol katalog punya title 'Buka Katalog Data'
                new_katalog = page.locator("button[title='Buka Katalog Data']").first
                if await new_katalog.count() > 0 and await new_katalog.is_visible():
                    await new_katalog.click()
                    self.log_step("Opened data catalog dialog (new UI).")
                    await page.wait_for_timeout(2500)
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

            await page.screenshot(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "bhumi_debug.png"))
            self.log_step("Screenshot disimpan: bhumi_debug.png (di folder app)")

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
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(800)
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
                if i == 0 and getattr(self, "_ui_broken", False):
                    break
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

            # Simpan teks mentah popup (untuk diagnostik - lihat apa persis yang
            # ditampilkan Bhumi) dan coba cari angka ZNT PRESISI dari respons
            # jaringan yang tadi disadap (kalau ada dan cocok dengan kode zona).
            try:
                result_json["raw_popup_text"] = extracted_text.strip()[:1500]
                exact, net_debug = extract_exact_znt_from_network(
                    network_znt_data, result_json.get("kode_zona"),
                    parse_rupiah(result_json.get("nilai_min")),
                    parse_rupiah(result_json.get("nilai_max")),
                )
                result_json["nilai_exact"] = exact
                self.log_step(self.t(
                    f"Popup Bhumi: {result_json.get('nilai_min')} - {result_json.get('nilai_max')} "
                    f"(respons jaringan disadap: {len(network_znt_data)})",
                    f"Bhumi popup: {result_json.get('nilai_min')} - {result_json.get('nilai_max')} "
                    f"(network responses captured: {len(network_znt_data)})"))
                if exact:
                    self.log_step(self.t(
                        f"✓ Angka ZNT presisi ditemukan di data jaringan Bhumi: Rp {exact:,.0f}".replace(",", "."),
                        f"✓ Precise ZNT value found in Bhumi network data: Rp {exact:,.0f}"))
                elif net_debug:
                    self.log_step(net_debug)
            except Exception as e:
                self.log_step(f"Precise-value extraction skipped: {e}")

            if not (result_json.get("kode_zona") or result_json.get("nilai_min") or result_json.get("nilai_max")):
                try:
                    result_json["debug_screenshot"] = await page.screenshot(type="png")
                except Exception:
                    pass
                try:
                    result_json["debug_body_text"] = (await page.locator("body").inner_text(timeout=3000))[:1500]
                except Exception:
                    pass

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

    # Popup UI baru memberi format berbaris ("Nomor Zone\n: 468\nTahun Dibuat\n: 2026").
    # Rapikan jadi satu baris "Label: nilai Label: nilai" supaya regex di bawah cocok
    # untuk format lama (tanpa spasi) maupun format baru (per baris).
    text = re.sub(r"\s*\n\s*:\s*", ": ", text or "")
    text = re.sub(r"\s*\n\s*", " ", text)

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

def extract_exact_znt_from_network(payloads, kode_zona, nilai_min, nilai_max):
    """
    Cari angka ZNT tunggal (bukan rentang) di respons JSON yang disadap dari
    jaringan saat peta Bhumi dimuat. Hanya dipakai kalau ada TEPAT SATU angka
    yang (a) berada di objek yang memuat kode zona yang sama dengan popup,
    (b) berada di key bernama nilai/harga/znt (bukan min/max/range), dan
    (c) jatuh di dalam rentang popup. Selain itu -> None (jangan menebak).
    Return (nilai_atau_None, pesan_debug).
    """
    if not kode_zona or not payloads:
        return None, ""
    kz = str(kode_zona).strip().lower()
    key_re = re.compile(r"(nilai|harga|znt)", re.I)
    skip_re = re.compile(r"(min|max|range|rentang|tahun|kode|id)", re.I)
    found = set()

    def to_num(v):
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            return parse_rupiah(v)
        return None

    def walk(o):
        if isinstance(o, dict):
            if any(isinstance(v, (str, int)) and str(v).strip().lower() == kz for v in o.values()):
                for k, v in o.items():
                    if key_re.search(str(k)) and not skip_re.search(str(k)):
                        n = to_num(v)
                        if n and n >= 1000:
                            found.add(n)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    parsed_any = False
    for text in payloads:
        try:
            walk(json.loads(text))
            parsed_any = True
        except Exception:
            continue

    lo = nilai_min if nilai_min is not None else 0
    hi = nilai_max if nilai_max is not None else float("inf")
    cands = sorted(v for v in found if lo * 0.999 <= v <= hi * 1.001)
    if len(cands) == 1:
        return cands[0], ""
    if not parsed_any:
        return None, "Network data captured but not JSON - only the popup range is available."
    return None, (f"No single precise value in network data (candidates: {cands})"
                  if cands else "No precise value in network data - only the popup range is available.")


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

    nilai_exact = parse_rupiah(bhumi_result.get("nilai_exact"))
    if nilai_exact:
        harga_znt_per_m2 = round(nilai_exact, 0)
    elif nilai_min is not None and nilai_max is not None:
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
        "nilai_min": nilai_min,
        "nilai_max": nilai_max,
        "nilai_exact": nilai_exact,
        "raw_popup_text": bhumi_result.get("raw_popup_text", ""),
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
    # Selalu jalankan di thread sendiri dengan event loop baru - menghindari
    # konflik dengan event loop Streamlit/Tornado (terutama di Windows).
    import concurrent.futures

    def _runner():
        return asyncio.run(agent.run())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_runner).result()


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
