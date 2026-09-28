# Context prompt — Sistem Penilaian Agunan Properti (paste this to continue work)

I'm building **Sistem Penilaian Agunan Properti**, an interactive Python/Streamlit
web app (NOT a CLI) that walks an appraiser through a 10-step property valuation
workflow for Indonesian collateral appraisal, ending in a downloadable report (LPA).

The workflow now follows a revised internal SOP spec ("Alur Sistem AI Housing
Appraisal — Revisi"). I'm not pasting the full spec doc here — just the parts
that affect implementation — but I have it and can paste specific sections on
request if you need exact wording.

## Stack & files

- **Streamlit** for the UI (multi-step wizard using `st.session_state.step`, no CLI).
- `app.py` — the wizard itself: one `elif st.session_state.step == N:` block per
  step (1-10), each with its own form/buttons, ending in "← Back" / "Continue →".
- `calculations.py` — pure Python valuation formulas (land value, cost-approach
  building value + depreciation, faktor pengurang rule engine, market value
  validation, liquidation value, NJOP ratio, comparable similarity score). No
  API calls here, fully unit-testable.
- `agents.py` — orchestration layer that calls external APIs per step:
  - `run_znt_agent()` (Step 2): geocode (if needed) → real Bhumi ATR/BPN scrape
    via `bhumi_agent.py` → fallback to Serper search + Groq/Gemini LLM estimate
    if the scrape fails.
  - `run_pinpoint_agent()` (Step 4, spatial): Serper searches per risk topic
    (flood, SUTET, railway, industry, hospital, school, market, main road,
    public facilities) + LLM to turn snippets into true/false flags + notes.
  - `run_manual_checklist_agent()` (Step 4, manual checklist pre-fill): rule-based
    `legalitas` from `status_sertifikat`, `akses_jalan`/`kondisi_lingkungan`
    derived from the pinpoint flags (reused, no extra calls), and a Serper+LLM
    search for `peruntukan_lahan` (RTRW/zonasi). The other 6 manual-checklist
    items (bentuk_tanah, kontur_tanah, posisi_tanah, kondisi_bangunan,
    kualitas_konstruksi, perawatan_bangunan) are intentionally left for the
    appraiser — they need a physical site visit/photos and are not guessed.
  - `run_comparable_agent()` (Step 6): Serper searches across 9 sources
    (Rumah123/99.co/OLX/Pinhome/Lamudi/RayWhite/ERA/DotProperty/Google) with
    tolerance-aware queries (LB/LT × 0.6–1.4) → fetches actual listing pages
    for the fetchable domains for richer detail → LLM extracts structured
    comparable listings (address, price, LT, LB, year, catatan). Defaults to
    15 results, but count is UI-adjustable, and there's an incremental
    "search more" path (`exclude_links`) that doesn't lose or duplicate
    existing results.
- `bhumi_agent.py` — **the real ZNT scraper** (Playwright, headless Chromium):
  opens https://bhumi.atrbpn.go.id/peta, accepts the disclaimer, dismisses
  onboarding, enables the "Zona Nilai Tanah" layer, searches by lat/lng, clicks
  the marker, reads the popup, and returns kode_zona/nilai_min/nilai_max/tahun/
  kelurahan/kecamatan/kabkota. Optionally cleans the extracted text into JSON
  via Gemini (`google-genai` SDK); falls back to a regex parser
  (`parse_standard_znt_fields`) if no Gemini key or the SDK isn't installed.
  Exposes `run_bhumi_znt_sync()` (sync wrapper), `parse_rupiah()`, and
  `map_bhumi_result_to_app_schema()` for use by `agents.py`.
- `geocode.py` — free OpenStreetMap Nominatim geocoding (address → lat/lon),
  used when the appraiser didn't pin coordinates manually in Step 1.
- `api_clients.py` — thin wrappers: `SerperClient` (google.serper.dev search),
  `GroqClient` (OpenAI-compatible chat completions, JSON mode), `GeminiClient`
  (REST generateContent, JSON mode). All return `(ok: bool, data_or_error)`.
- `requirements.txt` / `README.md` — deps (streamlit, requests, playwright,
  google-genai) and setup notes (`playwright install chromium` is required).

## How data flows

`st.session_state` holds everything between steps: `data` (Step 1 inputs),
`znt_result`, `bangunan_result`, `auto_flags`/`auto_notes` (spatial),
`checklist_auto_scores`/`checklist_auto_notes`/`checklist_auto_keys` (manual
checklist pre-fill), `manual_scores`/`faktor_pengurang`, `nilai_pasar_awal`,
`comparables`/`comparable_count`, `validasi`, `nilai_pasar_akhir`,
`nilai_likuidasi`, `njop_result`. API keys (Serper/Groq/Gemini) live in the
sidebar text inputs and are read into `SerperClient`/`GroqClient`/`GeminiClient`
at the top of `app.py` on every rerun — never written to disk.

## Formulas — locked per the revised SOP (do not change without re-reading the spec)

- **Nilai Tanah (Step 2) = ZNT per m² × Luas Tanah.** This is the *only*
  formula for Nilai Tanah. Comparable/pembanding data is NEVER used to derive
  Nilai Tanah — its role is entirely in Step 6/7 as a validation check against
  Nilai Pasar Awal. If you ever see or are asked to add logic that computes
  land value by subtracting a normalized building value from a comparable's
  price, that's wrong per the current SOP — don't add it.
- **Nilai Bangunan (Step 3) = BRB − Penyusutan** (cost approach). BRB currently
  uses a manually-input Rp/m² rate (proxy) instead of the official MAPPI BTB
  table, because that table is licensed and access isn't confirmed yet. Data
  structures are ready for the real table to be dropped in later.
- **Faktor Pengurang (Step 4) is capped at 30%** (not 50% — this was a bug,
  now fixed in `calculations.py`), with 4 status levels matching the SOP's
  color scheme: 🟢 Hijau / 🟡 Kuning / 🟠 Oranye / 🔴 Merah (currently split
  into even quartiles of the 0–30% range — thresholds are a reasonable
  default, not signed off by the Appraisal team yet).
- **Nilai Pasar Awal (Step 5) = (Nilai Tanah + Nilai Bangunan) × (1 − Faktor Pengurang)**.
- **Step 6/7 validation**: compare Nilai Pasar Awal against the average of
  Include-only comparables. Tolerance is now a UI-configurable slider
  (default 10%, per SOP's [KONFIGURASI] note) instead of a hardcoded constant.
  Within tolerance → Nilai Pasar Awal accepted as-is (pre-filled, not forced).
  Outside tolerance → the system does NOT auto-decide; it shows a warning and
  a suggested midpoint as a *starting value* in an editable field, and the
  appraiser must confirm/adjust before continuing.
- **Nilai Likuidasi (Step 8) = Nilai Pasar Akhir × Rasio Likuidasi.** Rasio
  Likuidasi now defaults from a per-`status_sertifikat` proxy table
  (`calc.estimasi_rasio_likuidasi`) instead of one fixed 80% for every status —
  still clearly labeled [KONFIGURASI]/proxy in the UI since the official table
  hasn't been provided by the Appraisal team.
- **Rasio NJOP (Step 9)** = (NJOP Tanah + NJOP Bangunan) / Nilai Pasar Akhir.
  Optional — skipped and flagged "data NJOP tidak tersedia" if not filled in
  Step 1, not treated as a failure.

## What's real vs. best-effort right now

- Step 1, 3, 5, 7, 8, 9, 10: fully implemented and internally consistent with
  the SOP above. Steps 3 and 8 still rely on proxy/config tables pending
  official data from the Appraisal team (flagged in-UI as [KONFIGURASI]).
- Step 2 (ZNT): real scrape via `bhumi_agent.py` is the primary path; falls
  back to an LLM estimate (clearly lower confidence) if scraping fails.
- Step 4 has two sub-agents now: the spatial pinpoint agent (flood/SUTET/etc.)
  and the manual-checklist pre-fill agent (4 of 10 items auto-estimated,
  6 left manual by design — see `run_manual_checklist_agent` docstring).
  Both depend on Serper search quality + LLM extraction accuracy — still the
  weakest links and most likely to need tuning.
- Step 6 (comparables): now fetches real listing pages, not just snippets,
  and defaults to 15 results (adjustable, expandable via "search more").
  Still depends on site scraping succeeding and LLM extraction accuracy.
- No automated tests exist yet. `calculations.py` is the easiest target for
  unit tests since it's pure and has no API dependency.
- "Pinpoint on Map" mode in Step 1 is a UI placeholder — it doesn't actually
  render a clickable map yet (would need `streamlit-folium` or similar).

## Known rough edges / good next tasks

1. Add a real interactive map for Step 1's "Pinpoint on Map" mode
   (e.g. `streamlit-folium`), so lat/lon doesn't require typing or geocoding.
2. Add caching/rate-limiting around Serper calls in Step 4 and Step 6 to
   reduce cost/latency (Step 6 alone can now be ~11 searches + up to 12 page
   fetches per run).
3. Add retry/backoff for transient Groq/Gemini/Serper failures instead of
   failing the whole step on one bad response.
4. Persist a run (e.g. to JSON or SQLite) so an appraiser can save/reload a
   valuation instead of losing everything on browser refresh.
5. Export the Step 10 report as PDF/DOCX in addition to Markdown.
6. Unit tests for `calculations.py` (formulas are pure and easy to pin down) —
   especially worth locking in the 30% faktor-pengurang cap and the 4-level
   status-risiko thresholds now that they've changed.
7. Handle Bhumi ATR/BPN site markup changes gracefully — the CSS
   selectors/text matches in `bhumi_agent.py` are brittle by nature of
   scraping; consider adding a "site changed" self-check or screenshot-on-
   failure for debugging.
8. Tighten the comparable-listing extraction prompt in `run_comparable_agent`
   — still permissive; consider requiring a source URL match per field.
9. **[KONFIGURASI — needs sign-off from the Appraisal team, not a code task]:**
   (a) official MAPPI BTB BRB-per-m² table for Step 3, (b) official
   rasio-likuidasi-per-status-sertifikat table for Step 8, (c) confirmation of
   whether Step 7's "outside tolerance" default should stay a simple
   SOP/market midpoint or use a different weighted-blend rule, (d) sign-off on
   the faktor-pengurang status-risiko thresholds (currently even quartiles of
   0–30%, not yet reviewed by the team).
10. Doc mentions "Sikumbang" as a comparable source alongside Rumah123/OLX/
    99.co — not currently in the Step 6 site list (couldn't confirm what
    domain this refers to). Add it if/when the correct URL is confirmed.

## What I want you to do

[Describe the specific feature to add or bug to fix here.]

Read the relevant file(s) above before editing, keep the Indonesian UI labels
and existing formula/field names consistent with what's already there, keep
`calculations.py` free of API calls, and don't touch the "locked" formulas
above without re-confirming against the SOP spec first.
