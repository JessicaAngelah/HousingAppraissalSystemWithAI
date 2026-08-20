# Sistem Penilaian Agunan Properti (Interactive, Python/Streamlit)

Aplikasi web interaktif (bukan CLI) untuk alur penilaian properti 10 langkah,
dari input data properti sampai Laporan Penilaian Agunan (LPA).

## Instalasi

```bash
pip install -r requirements.txt
playwright install chromium
```

`playwright install chromium` wajib dijalankan sekali agar Step 2 bisa
membuka browser headless yang scraping data ZNT asli dari Bhumi ATR/BPN.

## Menjalankan

```bash
streamlit run app.py
```

Ini akan membuka jendela browser dengan antarmuka interaktif — tidak ada
interaksi command line, semua lewat form, tombol, dan slider.

## API Keys

Buka sidebar **⚙️ API Keys** di kiri layar dan isi:

- **Serper API Key** — https://serper.dev — dipakai untuk pencarian web
  (Bhumi ZNT Agent, Pinpoint Screening Agent, Property Reference Agent).
- **Groq API Key** — https://console.groq.com — LLM cepat untuk
  ekstraksi/analisis data terstruktur (diprioritaskan jika keduanya diisi).
- **Gemini API Key** — https://aistudio.google.com/apikey — LLM cadangan
  jika Groq key tidak diisi.

Key disimpan hanya di session Streamlit (memori, selama tab dibuka) — tidak
ditulis ke disk. Anda tidak wajib mengisi semua tiga; sistem tetap berjalan
dan meminta input manual di step yang tidak punya key.

## Alur 10 Step

1. Input Data Properti
2. Nilai Tanah (Bhumi ZNT Agent) — pencarian web + estimasi LLM, dengan
   koreksi manual
3. Nilai Bangunan (Cost Approach)
4. Analisis Faktor Pengurang (Pinpoint Screening Agent + checklist manual)
5. Nilai Pasar Awal
6. Property Reference AI (pencarian listing dari beberapa situs + ekstraksi LLM)
7. Validasi Nilai Pasar
8. Nilai Likuidasi
9. Analisis NJOP
10. Laporan Penilaian Agunan (LPA) — bisa diunduh sebagai file Markdown

## Catatan Penting / Keterbatasan

- **Step 2 sekarang mengambil data ZNT ASLI** langsung dari peta interaktif
  Bhumi ATR/BPN memakai `bhumi_agent.py` (Playwright, browser headless):
  1. Kalau Step 1 tidak diisi lat/lon manual, sistem geocode alamat dulu
     lewat OpenStreetMap Nominatim (gratis, tanpa API key).
  2. `BhumiZntAgent` membuka https://bhumi.atrbpn.go.id/peta, mengaktifkan
     layer "Zona Nilai Tanah", mencari koordinat, klik marker, dan membaca
     popup (Nomor Zone, Range Nilai, Tahun Dibuat, dst).
  3. Kalau Gemini key diisi, teks popup dirapikan jadi JSON oleh Gemini
     (`google-genai`); kalau tidak, dipakai parser regex bawaan
     (`parse_standard_znt_fields`) sebagai fallback — hasilnya tetap valid.
  4. Kalau scraping gagal (situs berubah, Playwright belum terpasang,
     koordinat di luar layer ZNT, dll), sistem otomatis fallback ke
     estimasi lama (pencarian web via Serper + LLM) dengan confidence
     level yang jujur ditandai lebih rendah, dan field tetap bisa dikoreksi
     manual.
  - Tombol **"🔄 Ambil ulang data ZNT"** di Step 2 berguna kalau geocoding
    alamat kurang presisi — perbaiki lat/lon di Step 1 (mode "Manual
    Latitude & Longitude"), lalu ulangi Step 2.
  - Scraping butuh browser Chromium headless, jadi Step 2 bisa makan waktu
    ~20-40 detik dan butuh koneksi keluar (outbound) ke bhumi.atrbpn.go.id.
- **Step 1 — Harga yang Diajukan (opsional).** Field baru untuk mencatat
  harga penawaran/klaim pemilik, dipakai untuk perbandingan otomatis di
  Step 10.
- **Step 10 (baru) — Perbandingan Harga & Depresiasi Final**, sebelum
  Laporan Agunan:
  - Menampilkan tabel Umur Ekonomis/Manfaat & Penyusutan per Tahun dari
    Biaya Teknis Bangunan (BTB) MAPPI 2023 (Rumah Sederhana 5%/20th, Rumah
    Menengah 3.33%/30th, Rumah Mewah 2%/50th, Pabrik/Gudang 3.33%/30th,
    Toko/Kios 5%/20th) sebagai referensi SOP.
  - Membandingkan Harga yang Diajukan (Step 1) vs Nilai Pasar Akhir hasil
    appraisal.
  - Kalkulator Penyusutan Persentase FINAL: Asset Value default dari Nilai
    Bangunan Step 3, persentase default otomatis mengikuti tabel MAPPI
    berdasarkan klasifikasi bangunan (appraiser bisa timpa manual jenis
    bangunan/persentase), dan Period bisa dalam satuan Tahun ATAU Bulan
    (kalau Bulan, persentase tahunan otomatis dibagi 12 per periode).
  - Laporan (Step 11) sekarang menyertakan bagian "9b. Perbandingan Harga &
    Depresiasi Final" merangkum hasil ini.
- **Step 1 — Lokasi Properti kini sepenuhnya berfungsi**, tiga mode:
  - **Search Address**: tombol "🔍 Cari" men-geocode alamat/teks pencarian
    (Nominatim) dan menampilkan Latitude/Longitude hasil pencarian secara
    eksplisit.
  - **Pinpoint on Map**: peta interaktif (folium + streamlit-folium) - klik
    di peta untuk menandai lokasi persis; koordinat yang dipilih ditampilkan
    di bawah peta. Ada kotak pencarian terpisah untuk memindahkan tampilan
    peta ke area tertentu dulu sebelum klik presisi. Butuh `folium` &
    `streamlit-folium` (sudah ditambahkan ke requirements.txt).
  - **Manual Latitude & Longitude**: input angka langsung seperti sebelumnya.
  - Titik yang sudah dipilih tersimpan di session state dan tetap ada
    walau berpindah mode input.
- **Gemini API — retry otomatis untuk error 429 (Too Many Requests) & 503**:
  `GeminiClient` sekarang mencoba ulang otomatis dengan jeda meningkat
  (2s → 4s → 8s) sebelum menyerah. Kalau tetap gagal, pesan error menjelaskan
  bahwa ini rate limit/kuota API key (bukan bug aplikasi) dan menyarankan
  mencoba lagi nanti atau memeriksa kuota di Google AI Studio. Ini berlaku
  untuk fitur AI OCR foto rumah (Step 3) maupun panggilan Gemini lainnya.
- **Step 3 — AI OCR Klasifikasi & Umur Bangunan (opsional).** Unggah foto
  tampak depan rumah, Gemini vision akan memperkirakan klasifikasi
  (Sederhana/Menengah/Mewah) dan umur bangunan (tahun). Ini alat bantu
  pengisian awal saja — appraiser tetap wajib verifikasi lapangan. Butuh
  Gemini API key (Groq tidak mendukung gambar).
- **Step 3 — Dua metode penyusutan bangunan:** Garis Lurus (default lama)
  atau Persentase Tetap / Declining Balance (baru) — yang terakhir
  menampilkan jadwal penyusutan (depreciation schedule) tahun per tahun:
  `Depreciation = Beginning Value × Percentage`, `Balance = Beginning Value − Depreciation`.
- **Pinpoint peta interaktif** (klik di peta) belum diimplementasikan;
  gunakan mode "Manual Latitude & Longitude" di Step 1, atau tambahkan
  komponen `streamlit-folium` jika ingin peta klik-untuk-pin.
- **Property Reference AI (Step 6)** mengandalkan hasil pencarian Google (via
  Serper) yang menyebut Rumah123/99.co/OLX/Pinhome, lalu LLM mengekstrak data
  harga dari cuplikan snippet — termasuk tanggal listing itu diunggah/
  diperbarui (bukan tanggal sistem melakukan scraping). Ini bukan scraping
  resmi ke masing-masing situs (yang umumnya memblokir scraping otomatis),
  jadi hasilnya bisa tidak lengkap — selalu ada opsi "Tambah properti
  pembanding manual".
  - Tiap pembanding di-geocode (Nominatim) untuk menghitung jarak ke
    properti subjek. Kriteria default: luas tanah/bangunan dalam ±20% dari
    subjek DAN jarak ≤ 5 km (radius dan toleransi luas bisa dikustomisasi
    di UI sebelum pencarian). Pembanding di luar kriteria tetap ditampilkan
    (ditandai badge merah) tapi tidak otomatis dicentang.
  - Similarity score sekarang menitikberatkan pada kedekatan JARAK (70%)
    dibanding kemiripan luas (30%) — pembanding TERDEKAT diprioritaskan,
    bukan lagi yang paling mirip luasnya.
  - Ditampilkan juga median harga/m² dari seluruh pembanding tercentang,
    diproyeksikan ke luas tanah subjek (mis. "median 12 pembanding → Rp
    X/m² × 120 m² = Rp Y").
- **Step 7 — Rentang Nilai Pasar.** Selain Nilai Pasar Akhir (titik
  tunggal), sistem sekarang juga menampilkan RENTANG nilai pasar
  (mempertimbangkan baik Nilai Pasar Awal internal maupun sebaran
  minimum-maximum pembanding) — berguna saat kedua angka berbeda jauh,
  mis. internal Rp500jt vs pembanding Rp900jt → rentang Rp500jt–Rp900jt,
  plus satu titik estimasi untuk keperluan yang butuh angka tunggal.
- **Step 8 — Nilai Likuidasi kini berupa RENTANG** (bukan satu angka),
  memakai rentang rasio likuidasi yang bisa disesuaikan (default sekitar
  ±7-8% dari rasio proxy status sertifikat).
- **Step 10 — Laporan** kini juga membandingkan Harga Tanah per m² (ZNT
  subjek vs average/median/min/max pembanding), dan menampilkan rentang
  nilai pasar & nilai likuidasi (bukan hanya titik tunggal).
- Semua rumus (nilai tanah, cost approach, faktor pengurang, validasi,
  likuidasi, NJOP) ada di `calculations.py` dan berjalan murni lokal
  (tidak butuh API key), sehingga bisa dites/diubah terpisah dari bagian AI.

## Struktur File

```
app.py            - antarmuka Streamlit (wizard 10 step)
agents.py          - orkestrasi Serper + Groq/Gemini + BhumiZntAgent per step
bhumi_agent.py      - scraper Playwright untuk data ZNT asli dari Bhumi ATR/BPN
geocode.py          - geocoding alamat -> lat/lon (OpenStreetMap Nominatim)
api_clients.py      - wrapper API Serper / Groq / Gemini
calculations.py     - rumus-rumus penilaian (murni Python, testable)
requirements.txt
```
