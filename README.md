# Sistem Penilaian Agunan Properti (Python / Streamlit)

Aplikasi web interaktif (bukan CLI) untuk alur penilaian properti **11 langkah**,
dari input data properti sampai Laporan Penilaian Agunan (LPA). Semua
dilakukan lewat form, tombol, peta, dan slider. Antarmuka tersedia dalam
**Bahasa Indonesia** dan **English** (tombol di pojok atas sidebar).

## Instalasi

```bash
pip install -r requirements.txt
playwright install chromium
```

`playwright install chromium` wajib dijalankan sekali agar Step 2 bisa membuka
browser headless untuk mengambil data ZNT asli dari Bhumi ATR/BPN.

> **Windows:** kalau perintah `pip` atau `playwright` tidak dikenali, pakai
> `py -m pip install -r requirements.txt` dan `py -m playwright install chromium`.

## Menjalankan

```bash
streamlit run app.py
```

**Kalau muncul error `streamlit is not recognized` / `command not found`**
(biasanya karena Streamlit sudah terpasang tapi folder Scripts belum ada di
PATH), jalankan lewat Python:

```bash
py -m streamlit run app.py
```

Alternatif: `python -m streamlit run app.py` (atau `python3 -m streamlit run app.py`
di macOS/Linux).

Browser akan terbuka otomatis. Kalau tidak, buka alamat yang tampil di
terminal (biasanya http://localhost:8501).

## API Keys

Buka sidebar **⚙️ API Keys** di kiri layar dan isi:

| Key | Link | Fungsi |
| --- | --- | --- |
| **Serper API Key** | https://serper.dev | Pencarian web (Bhumi ZNT Agent cadangan, Pinpoint Screening Agent, Property Reference Agent) |
| **Groq API Key** | https://console.groq.com | LLM utama untuk ekstraksi/analisis data (diprioritaskan kalau keduanya diisi) |
| **Gemini API Key** | https://aistudio.google.com/apikey | LLM cadangan kalau Groq kosong, perapih data ZNT, dan **satu-satunya** yang bisa membaca foto (AI OCR Step 3) |

Tidak wajib mengisi ketiganya. Sistem tetap berjalan dan meminta input manual
di langkah yang tidak punya key. **Step 4** menampilkan kotak
**"API Key yang sudah ditambahkan"** (✅ terisi / ⚠️ belum diisi) sebelum
analisis berjalan, jadi Anda langsung tahu key mana yang masih kurang.

### Supaya tidak mengetik key berulang kali (opsional)

Key yang diketik di sidebar hanya tersimpan di session Streamlit (memori,
selama tab dibuka) dan tidak ditulis ke disk. Untuk mengisi otomatis, pakai
salah satu cara berikut (urutan prioritas):

1. **`.streamlit/secrets.toml`** (disarankan, juga untuk deploy ke Streamlit
   Community Cloud). Buat file ini di folder proyek:

   ```toml
   SERPER_API_KEY = "isi-key-anda"
   GROQ_API_KEY   = "isi-key-anda"
   GEMINI_API_KEY = "isi-key-anda"
   ```

2. **Environment variable** dengan nama yang sama, contoh di Windows
   PowerShell:

   ```powershell
   $env:SERPER_API_KEY="isi-key-anda"
   py -m streamlit run app.py
   ```

   atau di macOS/Linux: `SERPER_API_KEY=isi-key-anda streamlit run app.py`

> Jangan commit `secrets.toml` atau key Anda ke Git.

## Alur 11 Step

1. **Input Data Properti** — isi manual, atau impor dari file Excel/CSV.
   Lokasi punya 3 mode: *Search Address*, *Pinpoint on Map*, dan *Manual
   Latitude & Longitude*.
2. **Nilai Tanah (Bhumi ZNT)** — data ZNT asli dari Bhumi ATR/BPN, dengan
   koreksi manual.
3. **Nilai Bangunan (Cost Approach)** — penyusutan Garis Lurus atau
   Persentase Tetap (Declining Balance), plus AI OCR foto rumah (opsional).
4. **Faktor Pengurang** — peta & data GIS lokasi, Pinpoint Screening Agent,
   dan checklist manual.
5. **Nilai Pasar Awal**
6. **Property Reference AI** — pencarian properti pembanding dari beberapa
   situs.
7. **Perbandingan Harga Tanah per m²** — average / median / minimum / maximum
   pembanding.
8. **Validasi & Rentang Nilai Pasar**
9. **Hasil Appraisal: Perbandingan Harga** — perbandingan pendekatan
   Referensi, ZNT, dan Internal Final.
10. **Nilai Likuidasi** — berupa rentang.
11. **Laporan Penilaian Agunan (LPA)** — dapat diunduh sebagai file Markdown.

## Fitur Utama per Step

### Step 1 — Input & Lokasi
- **Impor Excel/CSV** (`excel_import.py`): siapkan banyak properti di
  spreadsheet lalu pilih barisnya di app. Nama kolom fleksibel asal
  mengandung salah satu kata: Alamat, Lokasi (format `lat, lon`), Dokumen,
  Data Bangunan (teks `Tahun bangun: YYYY` otomatis terbaca), Luas Tanah,
  Luas Bangunan. Tidak butuh API key.
- **Search Address**: tombol "🔍 Cari" men-geocode alamat (OpenStreetMap
  Nominatim) dan menampilkan Latitude/Longitude hasilnya.
- **Pinpoint on Map**: peta interaktif (folium + streamlit-folium), klik di
  peta untuk menandai lokasi persis. Ada kotak pencarian terpisah untuk
  memindahkan tampilan peta dulu sebelum klik presisi.
- **Manual Latitude & Longitude**: input angka langsung.
- Titik yang sudah dipilih tetap tersimpan walau berpindah mode.
- **Harga yang Diajukan** (opsional): harga penawaran/klaim pemilik, dipakai
  untuk perbandingan otomatis di langkah-langkah berikutnya.

### Step 2 — Nilai Tanah (Bhumi ZNT)
`bhumi_agent.py` (Playwright, browser headless) membuka
https://bhumi.atrbpn.go.id/peta, mengaktifkan layer "Zona Nilai Tanah",
mencari koordinat, klik marker, lalu membaca popup (Nomor Zone, Range Nilai,
Tahun Dibuat, dst).

- Kalau lat/lon belum diisi, alamat di-geocode dulu lewat Nominatim.
- Kalau Gemini key diisi, teks popup dirapikan jadi JSON oleh Gemini; kalau
  tidak, dipakai parser regex bawaan (`parse_standard_znt_fields`).
- Kalau scraping gagal (situs berubah, Playwright belum terpasang, koordinat
  di luar layer ZNT, dll), otomatis fallback ke estimasi web (Serper) + LLM
  dengan confidence level yang ditandai lebih rendah. Field tetap bisa
  dikoreksi manual.
- Tombol **"🔄 Ambil ulang data ZNT"** berguna kalau geocoding kurang presisi:
  perbaiki lat/lon di Step 1, lalu ulangi Step 2.
- Scraping butuh Chromium headless dan koneksi keluar ke
  `bhumi.atrbpn.go.id`, jadi bisa makan waktu sekitar 20-40 detik.

### Step 3 — Nilai Bangunan
- Dua metode penyusutan: **Garis Lurus**, atau **Persentase Tetap / Declining
  Balance** yang menampilkan jadwal penyusutan tahun per tahun
  (`Depreciation = Beginning Value × Percentage`,
  `Balance = Beginning Value − Depreciation`).
- **AI OCR foto rumah (opsional)**: unggah satu atau beberapa foto (tampak
  depan, samping, belakang), Gemini vision memperkirakan klasifikasi
  (Sederhana/Menengah/Mewah) dan umur bangunan. Ini hanya alat bantu
  pengisian awal, appraiser tetap wajib verifikasi lapangan. Butuh Gemini
  API key (Groq tidak mendukung gambar).

### Step 4 — Faktor Pengurang
- **Data GIS sungguhan** (`gis_data.py`): OpenStreetMap lewat Overpass API
  (minimarket, rumah sakit, sekolah, pasar, tempat ibadah, rel kereta, SUTET,
  kawasan industri, makam, TPA, sungai/kanal, dan jalan terdekat), indeks
  bahaya resmi BNPB InaRISK (banjir, longsor, gempa), plus Google Street View.
  Gratis dan tanpa API key.
- Kalau data GIS gagal diambil, otomatis fallback ke analisis web (Serper) +
  LLM (**Pinpoint Screening Agent**).
- Checklist manual ikut terisi sebagian secara otomatis (mis. legalitas dari
  status sertifikat, akses jalan dari data GIS), dan semuanya tetap bisa
  diubah.

### Step 6 — Property Reference AI
Mengandalkan hasil pencarian Google (via Serper) yang menyebut
Rumah123/99.co/OLX/Pinhome, lalu LLM mengekstrak data harga dari cuplikan
snippet, termasuk tanggal listing diunggah/diperbarui. Ini **bukan** scraping
resmi ke situs-situs tersebut, jadi hasilnya bisa tidak lengkap. Selalu ada
opsi **"Tambah properti pembanding manual"**.

- Tiap pembanding di-geocode untuk menghitung jarak ke properti subjek.
- Kriteria default: luas tanah/bangunan dalam ±20% dari subjek **dan** jarak
  ≤ 5 km (bisa diubah di UI sebelum pencarian). Pembanding di luar kriteria
  tetap ditampilkan (badge merah) tetapi tidak otomatis dicentang.
- Similarity score menitikberatkan **jarak (70%)** dibanding kemiripan luas
  (30%), jadi pembanding terdekat diprioritaskan.
- Ditampilkan juga median harga/m² dari semua pembanding tercentang,
  diproyeksikan ke luas tanah subjek.

### Step 8 — Rentang Nilai Pasar
Selain Nilai Pasar Akhir (titik tunggal), sistem menampilkan **rentang**
nilai pasar (mempertimbangkan Nilai Pasar Awal internal dan sebaran
minimum-maximum pembanding). Berguna saat kedua angka berbeda jauh, mis.
internal Rp500jt vs pembanding Rp900jt menghasilkan rentang Rp500jt-Rp900jt,
plus satu titik estimasi bila butuh angka tunggal.

### Step 10 & 11 — Likuidasi & Laporan
- Nilai Likuidasi berupa **rentang** (default sekitar ±7-8% dari rasio proxy
  status sertifikat, bisa disesuaikan).
- Laporan memuat perbandingan Harga Tanah per m², rentang nilai pasar, dan
  rentang nilai likuidasi. Tombol **"🔄 Mulai Penilaian Baru"** menghapus data
  penilaian tetapi mempertahankan API key di sidebar.
- Tabel Umur Ekonomis & Penyusutan per Tahun mengacu pada Biaya Teknis
  Bangunan (BTB) MAPPI 2023 (Rumah Sederhana 5%/20th, Menengah 3,33%/30th,
  Mewah 2%/50th, Pabrik/Gudang 3,33%/30th, Toko/Kios 5%/20th).

## Catatan & Keterbatasan

- **Gemini retry otomatis**: untuk error 429 (Too Many Requests) dan 503,
  `GeminiClient` mencoba ulang dengan jeda bertahap (2s → 4s → 8s). Kalau
  tetap gagal, pesan error menjelaskan bahwa ini rate limit/kuota API key
  (bukan bug aplikasi); coba lagi nanti atau cek kuota di Google AI Studio.
- Semua rumus (nilai tanah, cost approach, faktor pengurang, validasi,
  likuidasi, NJOP) ada di `calculations.py` dan berjalan murni lokal tanpa
  API key, sehingga bisa dites/diubah terpisah dari bagian AI.
- Hasil AI (OCR foto, estimasi web, checklist otomatis) hanya alat bantu.
  Appraiser tetap wajib memverifikasi.

## Struktur File

```
app.py              - antarmuka Streamlit (wizard 11 step)
agents.py           - orkestrasi Serper + Groq/Gemini + BhumiZntAgent per step
bhumi_agent.py      - scraper Playwright untuk data ZNT asli dari Bhumi ATR/BPN
gis_data.py         - data GIS Step 4 (OpenStreetMap Overpass, BNPB InaRISK, Street View)
geocode.py          - geocoding alamat -> lat/lon (OpenStreetMap Nominatim)
excel_import.py     - impor data properti dari Excel/CSV untuk Step 1
api_clients.py      - wrapper API Serper / Groq / Gemini
calculations.py     - rumus-rumus penilaian (murni Python, testable)
requirements.txt    - dependensi Python
packages.txt        - dependensi sistem Linux untuk Chromium (deploy Streamlit Cloud)
CONTINUE_PROMPT.md  - catatan konteks untuk melanjutkan pengembangan
```