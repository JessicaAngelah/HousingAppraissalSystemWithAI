"""
api_clients.py
Wrapper tipis untuk 3 API eksternal yang dipakai sistem:
- Serper.dev  -> pencarian Google (dipakai oleh Bhumi ZNT Agent,
                 Pinpoint Screening Agent, dan Property Reference Agent)
- Groq        -> LLM cepat (dipakai untuk ekstraksi/parsing terstruktur)
- Gemini      -> LLM (dipakai untuk analisis & ringkasan)

Semua fungsi mengembalikan (ok: bool, data/error: Any) supaya UI mudah
menampilkan pesan error tanpa exception yang mematikan seluruh app.
"""

import json
import re
import requests

SERPER_URL = "https://google.serper.dev/search"
SERPER_PLACES_URL = "https://google.serper.dev/places"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_MODELS_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models?key={key}"
GEMINI_URL_TMPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
)

# Groq mem-pensiunkan model dengan cepat juga (mis. llama-3.3-70b-versatile
# dan llama-3.1-8b-instant dijadwalkan shutdown 16 Agustus 2026 - lihat
# https://console.groq.com/docs/deprecations). Sama seperti Gemini di bawah,
# ini daftar URUTAN PERCOBAAN: kalau model utama sudah retired (400/404) atau
# kena rate limit (429), klien otomatis lanjut ke model berikutnya alih-alih
# membuat seluruh Step 4/6 gagal total.
GROQ_FALLBACK_MODELS = [
    "openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b",
    "llama-3.3-70b-versatile",
]

# Google mem-pensiunkan model Gemini dengan cepat (mis. semua 1.5 dan 2.0
# sudah 404 per pertengahan 2026), jadi daftar ini adalah URUTAN PERCOBAAN,
# bukan jaminan - kalau satu model sudah retired (404) atau kuotanya habis
# (429), klien otomatis lanjut ke model berikutnya. Sebagai jaring pengaman
# terakhir, list_models() dipanggil untuk menemukan model yang benar-benar
# masih aktif saat ini kalau SEMUA kandidat di bawah ini gagal.
GEMINI_FALLBACK_MODELS = [
    "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.1-flash-lite",
    "gemini-3.5-flash", "gemini-2.0-flash",
]


def _redact_key(text: str) -> str:
    """Buang API key dari pesan error (mis. dari URL yang di-echo balik oleh
    requests.HTTPError) supaya tidak bocor ke tampilan/log."""
    return re.sub(r"([?&]key=)[^&\s]+", r"\1[REDACTED]", str(text))


class SerperClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(self, query: str, num: int = 10, gl: str = "id", hl: str = "id",
               max_retries: int = 2, page: int = 1):
        """
        Step 4 (dan Step 6) memanggil ini berkali-kali berturut-turut (mis.
        Pinpoint Agent = 9x per properti), jadi satu timeout/429 sesaat tidak
        boleh menggagalkan seluruh analisis - retry singkat dengan backoff
        untuk error yang biasanya sementara (429/5xx/timeout/koneksi putus).

        page: nomor halaman hasil pencarian Google (1=default/teratas,
        2=halaman berikutnya, dst). PENTING untuk pencarian berulang (mis.
        Step 6 comparable search yang mengulang ronde pencarian) - tanpa ini,
        query yang SAMA persis dipanggil lagi akan mengembalikan hasil yang
        HAMPIR IDENTIK ke panggilan sebelumnya (Google search deterministik
        untuk query yang sama), jadi ronde ke-2/3/4 nyaris tidak menemukan
        listing BARU sama sekali walau exclude_links sudah dipasang - bukan
        karena listingnya sudah habis, tapi karena tidak pernah benar-benar
        mencari lebih dalam dari halaman pertama.
        """
        if not self.api_key:
            return False, "SERPER_API_KEY belum diisi."
        import time as _time
        delay = 1.5
        last_err = None
        body = {"q": query, "num": num, "gl": gl, "hl": hl}
        if page and page > 1:
            body["page"] = page
        for attempt in range(max_retries + 1):
            try:
                resp = requests.post(
                    SERPER_URL,
                    headers={
                        "X-API-KEY": self.api_key,
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=20,
                )
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                    _time.sleep(delay)
                    delay *= 2
                    continue
                resp.raise_for_status()
                return True, resp.json()
            except requests.RequestException as e:
                last_err = _redact_key(f"Serper error: {e}")
                if attempt < max_retries:
                    _time.sleep(delay)
                    delay *= 2
                    continue
                return False, last_err
        return False, last_err or "Serper error: permintaan gagal setelah beberapa percobaan."

    def places(self, query: str, num: int = 8, gl: str = "id", hl: str = "id", max_retries: int = 2):
        """
        Cari lewat Google Places (via endpoint /places Serper), BUKAN Google
        Search biasa - dipakai sebagai FALLBACK geocoding kalau Nominatim/OSM
        (gratis, dipakai di geocode.py) tidak menemukan alamat. Nominatim
        sering tidak punya data untuk nama perumahan/kompleks kecil di
        Indonesia (mis. "Perumahan Azna Residence") padahal Google Maps
        punya, karena basis datanya OpenStreetMap (kontribusi sukarela) vs
        Google Maps (jauh lebih lengkap untuk POI Indonesia). Butuh Serper
        API key (sama seperti search()) - beda dari Nominatim yang gratis
        tanpa key.
        Mengembalikan (ok: bool, data: dict) - data["places"] berisi list
        {"title","address","latitude","longitude",...} kalau ok=True.
        """
        if not self.api_key:
            return False, "SERPER_API_KEY belum diisi."
        import time as _time
        delay = 1.5
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                resp = requests.post(
                    SERPER_PLACES_URL,
                    headers={
                        "X-API-KEY": self.api_key,
                        "Content-Type": "application/json",
                    },
                    json={"q": query, "num": num, "gl": gl, "hl": hl},
                    timeout=20,
                )
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                    _time.sleep(delay)
                    delay *= 2
                    continue
                resp.raise_for_status()
                return True, resp.json()
            except requests.RequestException as e:
                last_err = _redact_key(f"Serper places error: {e}")
                if attempt < max_retries:
                    _time.sleep(delay)
                    delay *= 2
                    continue
                return False, last_err
        return False, last_err or "Serper places error: permintaan gagal setelah beberapa percobaan."


class GroqClient:
    def __init__(self, api_key: str, model: str = "openai/gpt-oss-120b"):
        self.api_key = api_key
        self.model = model

    def _post_once(self, model: str, payload: dict, timeout: int):
        """Satu percobaan POST. Mengembalikan (ok, resp|kode_error_khusus|pesan)."""
        payload = dict(payload, model=model)
        try:
            resp = requests.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
            if resp.status_code == 429:
                return False, "RATE_LIMIT_429"
            if resp.status_code in (400, 404):
                # model sudah di-retire / tidak dikenali Groq - coba model lain,
                # bukan diulang di model yang sama.
                return False, "MODEL_UNAVAILABLE"
            resp.raise_for_status()
            return True, resp
        except requests.RequestException as e:
            return False, _redact_key(f"Groq error: {e}")

    def chat(self, system_prompt: str, user_prompt: str, json_mode: bool = False, max_retries: int = 3):
        """
        Kirim chat completion, dengan:
        1. Retry + backoff untuk 429 (rate limit) di model yang sama.
        2. Fallback otomatis ke model lain (lihat GROQ_FALLBACK_MODELS) kalau
           model utama sudah di-retire Groq (400/404) - supaya app tidak mati
           total begitu Groq mempensiunkan satu model ID.
        """
        if not self.api_key:
            return False, "GROQ_API_KEY belum diisi."
        import time as _time

        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        models_to_try = [self.model] + [m for m in GROQ_FALLBACK_MODELS if m != self.model]
        unavailable = []
        last_err = None
        for model in models_to_try:
            delay = 1.5
            for attempt in range(max_retries + 1):
                ok, resp_or_err = self._post_once(model, payload, timeout=30)
                if ok:
                    resp = resp_or_err
                    try:
                        content = resp.json()["choices"][0]["message"]["content"]
                    except (KeyError, IndexError, ValueError) as e:
                        return False, f"Groq response tidak terduga: {e}"
                    if json_mode:
                        try:
                            return True, json.loads(content)
                        except json.JSONDecodeError:
                            return False, f"Groq tidak mengembalikan JSON valid: {content[:300]}"
                    return True, content
                if resp_or_err == "RATE_LIMIT_429":
                    last_err = f"Model {model} kena rate limit (429)"
                    if attempt < max_retries:
                        _time.sleep(delay)
                        delay *= 2
                        continue
                    unavailable.append(model)
                    break
                if resp_or_err == "MODEL_UNAVAILABLE":
                    unavailable.append(model)
                    break
                # error lain (401/koneksi/dll) - ganti model tidak akan membantu
                return False, resp_or_err
        return False, (
            f"Semua model Groq yang dicoba gagal (retired/rate-limited): "
            f"{', '.join(unavailable) or '-'}. {last_err or ''} "
            "Coba lagi sebentar lagi, atau periksa https://console.groq.com/docs/models "
            "untuk model yang masih aktif."
        )


class GeminiClient:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self.api_key = api_key
        self.model = model
        self._discovered_model = None  # cache hasil list_models() supaya tidak dipanggil berulang

    def _post_with_retry(self, model: str, body: dict, timeout: int, max_retries: int = 4):
        """
        POST ke Gemini dengan retry + exponential backoff KHUSUS untuk error
        429 (Too Many Requests / rate limit habis) dan 503 (server sedang
        sibuk) - dua error ini biasanya sementara dan sering berhasil kalau
        dicoba ulang setelah jeda singkat. Error lain (401/400/dll) langsung
        dilempar tanpa retry karena mengulang tidak akan membantu.

        Mengembalikan (ok: bool, response|pesan_error). Pesan error TIDAK
        pernah mengandung API key mentah (di-redact), termasuk kalau bocor
        lewat requests.HTTPError yang meng-echo URL request.
        """
        import time as _time

        url = GEMINI_URL_TMPL.format(model=model, key=self.api_key)
        delay = 2.0
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                resp = requests.post(url, json=body, timeout=timeout)
                if resp.status_code in (429, 503) and attempt < max_retries:
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else delay
                    _time.sleep(min(wait, 20))
                    delay *= 2
                    continue
                resp.raise_for_status()
                return True, resp
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                if status == 429:
                    last_error = "RATE_LIMIT_429"
                    if attempt < max_retries:
                        _time.sleep(min(delay, 20))
                        delay *= 2
                        continue
                    return False, last_error
                if status == 404:
                    # Model sudah dipensiunkan/tidak ada - retry pada model yang
                    # sama tidak akan pernah berhasil, langsung sinyal ke
                    # caller supaya lanjut ke model fallback berikutnya.
                    return False, "MODEL_NOT_FOUND_404"
                return False, _redact_key(f"Gemini error: {e}")
            except requests.RequestException as e:
                last_error = _redact_key(f"Gemini error: {e}")
                if attempt < max_retries:
                    _time.sleep(delay)
                    delay *= 2
                    continue
                return False, last_error
        return False, last_error or "Gemini error: permintaan gagal setelah beberapa percobaan."

    def list_models(self):
        """
        Tanya ke Google model apa saja yang SAAT INI benar-benar aktif untuk
        API key ini (dipakai sebagai jaring pengaman terakhir kalau semua
        model di GEMINI_FALLBACK_MODELS sudah retired/404 - Google sering
        mempensiunkan model baru tanpa banyak pemberitahuan). Hasil di-cache
        di instance ini supaya tidak dipanggil berulang-ulang.
        Mengembalikan (ok: bool, list_nama_model|pesan_error).
        """
        if self._discovered_model is not None:
            return True, [self._discovered_model]
        try:
            url = GEMINI_MODELS_URL_TMPL.format(key=self.api_key)
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            names = []
            for m in data.get("models", []):
                actions = m.get("supportedGenerationMethods", [])
                if "generateContent" in actions:
                    name = m.get("name", "").replace("models/", "")
                    if name:
                        names.append(name)
            # Prioritaskan model "flash" (lebih murah/cepat, cukup untuk
            # tugas klasifikasi/ekstraksi di aplikasi ini) di atas "pro".
            names.sort(key=lambda n: (0 if "flash" in n else 1, n))
            if not names:
                return False, "Tidak ada model generateContent yang tersedia untuk API key ini."
            return True, names
        except requests.RequestException as e:
            return False, _redact_key(f"Gemini error saat list_models: {e}")
        except (KeyError, ValueError) as e:
            return False, f"Gemini list_models response tidak terduga: {e}"

    def _post_with_model_fallback(self, body: dict, timeout: int):
        """
        Coba model utama dulu; kalau gagal karena 429 (kuota habis) atau 404
        (model sudah dipensiunkan Google), coba model-model fallback satu
        per satu. Kalau SEMUA kandidat hardcoded gagal, panggil list_models()
        untuk menemukan model yang benar-benar masih aktif hari ini dan coba
        itu sebagai upaya terakhir sebelum menyerah.
        """
        models_to_try = [self.model] + [m for m in GEMINI_FALLBACK_MODELS if m != self.model]
        rate_limited, not_found = [], []
        for model in models_to_try:
            ok, resp_or_err = self._post_with_retry(model, body, timeout=timeout)
            if ok:
                return True, resp_or_err
            if resp_or_err == "RATE_LIMIT_429":
                rate_limited.append(model)
                continue
            if resp_or_err == "MODEL_NOT_FOUND_404":
                not_found.append(model)
                continue
            return False, resp_or_err  # error lain (400/401/dll) - ganti model tidak akan membantu

        # Semua kandidat hardcoded gagal (retired dan/atau rate-limited) -
        # coba temukan model yang aktif secara dinamis sebagai upaya terakhir.
        ok_list, models_or_err = self.list_models()
        if ok_list:
            for model in models_or_err:
                if model in models_to_try:
                    continue  # sudah dicoba di atas
                ok, resp_or_err = self._post_with_retry(model, body, timeout=timeout)
                if ok:
                    self._discovered_model = model  # cache supaya panggilan berikutnya langsung pakai ini
                    return True, resp_or_err
                if resp_or_err == "RATE_LIMIT_429":
                    rate_limited.append(model)
                elif resp_or_err == "MODEL_NOT_FOUND_404":
                    not_found.append(model)

        if not_found and not rate_limited:
            return False, (
                f"Semua model Gemini yang dicoba sudah tidak tersedia lagi (404 - retired oleh "
                f"Google): {', '.join(not_found)}. Ini biasanya terjadi karena Google sering "
                "mempensiunkan model lama; coba periksa https://ai.google.dev/gemini-api/docs/models "
                "untuk nama model terbaru, atau laporkan ke pengembang aplikasi untuk update daftar model."
            )
        return False, (
            f"Gemini API bermasalah pada semua model yang dicoba - rate-limited: "
            f"{', '.join(rate_limited) or '-'}; tidak ditemukan/retired: {', '.join(not_found) or '-'}. "
            "Ini BUKAN bug di aplikasi. Coba lagi dalam beberapa menit, atau periksa kuota/billing "
            "Gemini API key Anda di Google AI Studio (aistudio.google.com/apikey)."
        )

    def generate(self, prompt: str, json_mode: bool = False):
        if not self.api_key:
            return False, "GEMINI_API_KEY belum diisi."
        try:
            body = {"contents": [{"parts": [{"text": prompt}]}]}
            if json_mode:
                body["generationConfig"] = {"response_mime_type": "application/json"}

            ok, resp_or_err = self._post_with_model_fallback(body, timeout=30)
            if not ok:
                return False, resp_or_err
            data = resp_or_err.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            if json_mode:
                try:
                    return True, json.loads(text)
                except json.JSONDecodeError:
                    return False, f"Gemini tidak mengembalikan JSON valid: {text[:300]}"
            return True, text
        except requests.RequestException as e:
            return False, _redact_key(f"Gemini error: {e}")
        except (KeyError, IndexError) as e:
            return False, f"Gemini response tidak terduga: {e}"

    def generate_with_image(self, prompt: str, image_bytes: bytes, mime_type: str = "image/jpeg",
                             json_mode: bool = False):
        """
        Sama seperti generate(), tapi menyertakan satu gambar (mis. foto rumah)
        sebagai input multimodal. Tipis - delegasi ke generate_with_images()
        dengan list berisi satu gambar, supaya jalur kode lama tetap kompatibel.
        """
        return self.generate_with_images(prompt, [(image_bytes, mime_type)], json_mode=json_mode)

    def generate_with_images(self, prompt: str, images: list, json_mode: bool = False):
        """
        Sama seperti generate(), tapi menyertakan SATU ATAU LEBIH gambar (mis.
        foto tampak depan + samping + belakang rumah) sebagai input multimodal
        dalam satu request - dipakai untuk fitur "AI OCR Estimasi Umur &
        Klasifikasi Bangunan dari Foto" di Step 3. `images` adalah list berisi
        tuple (image_bytes, mime_type); tiap image_bytes adalah data biner
        mentah file gambar (JPEG/PNG), di-encode base64 di sini. Gemini
        membaca semua gambar dalam satu context yang sama sehingga bisa
        mensintesis satu kesimpulan dari beberapa sudut foto sekaligus.

        Otomatis retry dengan backoff kalau kena rate limit (429), dan kalau
        model utama benar-benar habis kuota akan otomatis mencoba model
        fallback yang lebih ringan - lihat _post_with_model_fallback di atas.
        """
        import base64

        if not self.api_key:
            return False, "GEMINI_API_KEY belum diisi."
        if not images:
            return False, "Tidak ada gambar yang diberikan."
        try:
            parts = [{"text": prompt}]
            for image_bytes, mime_type in images:
                b64_data = base64.b64encode(image_bytes).decode("utf-8")
                parts.append({"inline_data": {"mime_type": mime_type or "image/jpeg", "data": b64_data}})

            body = {"contents": [{"parts": parts}]}
            if json_mode:
                body["generationConfig"] = {"response_mime_type": "application/json"}

            ok, resp_or_err = self._post_with_model_fallback(body, timeout=45)
            if not ok:
                return False, resp_or_err
            data = resp_or_err.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            if json_mode:
                try:
                    return True, json.loads(text)
                except json.JSONDecodeError:
                    return False, f"Gemini tidak mengembalikan JSON valid: {text[:300]}"
            return True, text
        except requests.RequestException as e:
            return False, _redact_key(f"Gemini error: {e}")
        except (KeyError, IndexError) as e:
            return False, f"Gemini response tidak terduga: {e}"
