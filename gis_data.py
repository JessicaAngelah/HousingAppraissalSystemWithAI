"""
gis_data.py
Data GIS untuk Step 4 (Faktor Pengurang): menggantikan tebakan "web search + LLM"
dengan data spasial sungguhan di sekitar koordinat properti.

Sumber data:
- OpenStreetMap lewat Overpass API (gratis, tanpa key): minimarket (Indomaret/
  Alfamart), rumah sakit, sekolah, pasar, tempat ibadah, rel kereta, SUTET
  (power=line), kawasan industri, makam, TPA, sungai/kanal, dan JALAN terdekat
  (kelas jalan + tag lebar kalau ada).
- BNPB InaRISK (ArcGIS ImageServer) untuk indeks bahaya RESMI (banjir,
  longsor, gempa). URL layer bisa diganti di HAZARD_LAYERS di bawah kalau
  BNPB mengubah alamat servisnya.
- Link/embed Google Street View.

Semua fungsi jaringan gagal dengan aman (mengembalikan status error, bukan
melempar exception) supaya Step 4 bisa fallback ke analisis lama.
"""

import math
import re
import time
import requests

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# Layer bahaya resmi BNPB InaRISK. Kalau salah satu URL berubah/tidak bisa
# diakses, layer itu ditandai "tidak tersedia" di UI (tidak merusak yang lain).
HAZARD_LAYERS = [
    ("banjir", "Banjir", "Flood",
     "https://gis.bnpb.go.id/server/rest/services/inarisk/layer_bahaya_banjir/ImageServer"),
    ("longsor", "Tanah Longsor", "Landslide",
     "https://gis.bnpb.go.id/server/rest/services/inarisk/layer_bahaya_tanah_longsor/ImageServer"),
    ("gempa", "Gempa Bumi", "Earthquake",
     "https://gis.bnpb.go.id/server/rest/services/inarisk/layer_bahaya_gempabumi/ImageServer"),
]
INARISK_PORTAL = "https://inarisk.bnpb.go.id/"

# Ambang jarak (meter) untuk menyalakan flag otomatis Step 4.
THRESHOLDS_M = {
    "sutet": 100,
    "railway": 100,
    "industry": 500,
    "hospital": 1000,
    "school": 500,
    "market": 500,
    "main_road": 300,
    "public_facilities": 500,
}

MAIN_ROAD_TYPES = {"motorway", "trunk", "primary", "secondary"}
NARROW_ROAD_TYPES = {"service", "track", "path", "living_street", "footway", "pedestrian"}
CAT_LABELS = {
    "minimarket": ("Minimarket (Indomaret/Alfamart dll.)", "Minimart (Indomaret/Alfamart etc.)"),
    "supermarket": ("Supermarket/Mall", "Supermarket/Mall"),
    "hospital": ("Rumah Sakit", "Hospital"),
    "clinic": ("Klinik/Puskesmas", "Clinic"),
    "school": ("Sekolah/Kampus", "School/Campus"),
    "market": ("Pasar", "Market"),
    "worship": ("Tempat Ibadah", "Place of Worship"),
    "cemetery": ("Makam", "Cemetery"),
    "railway": ("Rel Kereta", "Railway"),
    "rail_station": ("Stasiun", "Station"),
    "power_line": ("SUTET/Jaringan Transmisi", "High-voltage Line"),
    "substation": ("Gardu Induk", "Substation"),
    "industrial": ("Kawasan Industri", "Industrial Area"),
    "landfill": ("TPA/Landfill", "Landfill"),
    "waterway": ("Sungai/Kanal", "River/Canal"),
    "park": ("Taman/Lapangan Olahraga", "Park/Sports Field"),
    "bus_station": ("Terminal/Halte", "Bus Terminal"),
    "road": ("Jalan", "Road"),
}


def _tt(lang, id_text, en_text):
    return en_text if lang == "en" else id_text


# ---------------------------------------------------------------------------
# Geometri
# ---------------------------------------------------------------------------
def _to_xy(lat, lon, lat0, lon0):
    return ((lon - lon0) * 111320.0 * math.cos(math.radians(lat0)),
            (lat - lat0) * 110540.0)


def _point_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay), ax, ay
    tt = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + tt * dx, ay + tt * dy
    return math.hypot(px - cx, py - cy), cx, cy


def _inside(px, py, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > py) != (y2 > py) and px < (x2 - x1) * (py - y1) / ((y2 - y1) or 1e-12) + x1:
            inside = not inside
    return inside


def _polylines(el):
    """Ambil semua polyline [(lat, lon), ...] dari elemen Overpass (node/way/relation)."""
    out = []
    if el.get("type") == "node" and "lat" in el:
        out.append([(el["lat"], el["lon"])])
    elif el.get("geometry"):
        out.append([(g["lat"], g["lon"]) for g in el["geometry"] if g])
    for m in el.get("members", []) or []:
        if m.get("geometry"):
            out.append([(g["lat"], g["lon"]) for g in m["geometry"] if g])
    if not out and el.get("center"):
        out.append([(el["center"]["lat"], el["center"]["lon"])])
    return [p for p in out if p]


def _distance_and_nearest(lat0, lon0, polylines, area=False):
    """Jarak terdekat (meter) dari titik ke polyline + koordinat titik terdekat."""
    best = (float("inf"), None, None)
    for pl in polylines:
        pts = [_to_xy(la, lo, lat0, lon0) for la, lo in pl]
        if area and len(pts) >= 3 and pts[0] == pts[-1] and _inside(0.0, 0.0, pts):
            return 0.0, lat0, lon0
        if len(pts) == 1:
            d = math.hypot(pts[0][0], pts[0][1])
            if d < best[0]:
                best = (d, pl[0][0], pl[0][1])
            continue
        for i in range(len(pts) - 1):
            d, cx, cy = _point_seg(0.0, 0.0, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
            if d < best[0]:
                best = (d, lat0 + cy / 110540.0,
                        lon0 + cx / (111320.0 * math.cos(math.radians(lat0))))
    return best


# ---------------------------------------------------------------------------
# Kategori elemen OSM
# ---------------------------------------------------------------------------
_MINIMARKET_RE = re.compile(r"indomaret|alfamart|alfamidi|alfa express|lawson|circle k|familymart|yomart|superindo express", re.I)


def _categorize(tags):
    a = tags.get("amenity", "")
    shop = tags.get("shop", "")
    if shop in ("convenience",) or (shop == "supermarket" and _MINIMARKET_RE.search(tags.get("name", "") + tags.get("brand", ""))):
        return "minimarket"
    if _MINIMARKET_RE.search(tags.get("name", "") + " " + tags.get("brand", "")) and shop:
        return "minimarket"
    if shop in ("supermarket", "mall"):
        return "supermarket"
    if a == "hospital":
        return "hospital"
    if a in ("clinic", "doctors"):
        return "clinic"
    if a in ("school", "kindergarten", "college", "university"):
        return "school"
    if a == "marketplace":
        return "market"
    if a == "place_of_worship":
        return "worship"
    if a == "grave_yard" or tags.get("landuse") == "cemetery":
        return "cemetery"
    if a in ("bus_station",):
        return "bus_station"
    if tags.get("railway") in ("rail", "light_rail", "subway"):
        return "railway"
    if tags.get("railway") in ("station", "halt"):
        return "rail_station"
    if tags.get("power") == "line":
        return "power_line"
    if tags.get("power") == "substation":
        return "substation"
    if tags.get("landuse") == "industrial":
        return "industrial"
    if tags.get("landuse") == "landfill":
        return "landfill"
    if tags.get("waterway") in ("river", "canal", "stream", "drain"):
        return "waterway"
    if tags.get("leisure") in ("park", "pitch", "stadium", "sports_centre", "playground"):
        return "park"
    if "highway" in tags:
        return "road"
    return None


# ---------------------------------------------------------------------------
# Overpass
# ---------------------------------------------------------------------------
def _overpass_queries(lat, lon, r_poi=1000, r_line=500, r_road=300):
    """Dipecah jadi 3 query kecil (lebih ringan & tidak gampang timeout di
    area padat seperti Jakarta) - hasil yang berhasil digabung."""
    a = f"(around:{r_poi},{lat},{lon})"
    b = f"(around:{r_line},{lat},{lon})"
    c = f"(around:{r_road},{lat},{lon})"
    head = "[out:json][timeout:25];"
    return [
        ("POI", head + f"""(
  nwr["amenity"~"^(hospital|clinic|doctors|school|kindergarten|college|university|marketplace|place_of_worship|grave_yard|bus_station)$"]{a};
  nwr["shop"~"^(convenience|supermarket|mall)$"]{a};
  nwr["leisure"~"^(park|pitch|stadium|sports_centre|playground)$"]{b};
  nwr["power"="substation"]{b};
  nwr["railway"~"^(station|halt)$"]{b};
);
out tags center qt;"""),
        ("lines/areas", head + f"""(
  way["railway"~"^(rail|light_rail|subway)$"]{b};
  way["power"="line"]{b};
  nwr["landuse"~"^(industrial|cemetery|landfill)$"]{b};
  way["waterway"~"^(river|canal|stream|drain)$"]{b};
);
out tags geom qt;"""),
        ("roads", head + f"""way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service|track|path)$"]{c};
out tags geom qt;"""),
    ]


def _run_overpass(query):
    """Coba semua mirror (2 putaran). Return (ok, elements_or_error)."""
    last_err = "unknown error"
    for attempt in range(2):
        for url in OVERPASS_URLS:
            try:
                resp = requests.post(url, data={"data": query}, timeout=35,
                                     headers={"User-Agent": "HousingAppraisalSystem/1.0"})
                if resp.status_code == 200:
                    data = resp.json()
                    # Overpass kadang membalas 200 tapi query-nya kena timeout/limit memori.
                    if data.get("remark") and not data.get("elements"):
                        last_err = f"{url}: {data['remark'][:120]}"
                        continue
                    return True, data.get("elements", [])
                last_err = f"HTTP {resp.status_code} from {url}"
            except requests.RequestException as e:
                last_err = f"{type(e).__name__} from {url}"
            except ValueError:
                last_err = f"Invalid JSON from {url}"
        time.sleep(2)
    return False, last_err


def fetch_osm(lat, lon):
    """Return (ok, elements_or_error, warnings). ok=True kalau minimal 1 dari 3 query berhasil."""
    elements, warnings, any_ok = [], [], False
    for label, q in _overpass_queries(lat, lon):
        ok, res = _run_overpass(q)
        if ok:
            any_ok = True
            elements.extend(res)
        else:
            warnings.append(f"{label}: {res}")
    if not any_ok:
        return False, "; ".join(warnings), warnings
    return True, elements, warnings


# ---------------------------------------------------------------------------
# Bahaya resmi (BNPB InaRISK)
# ---------------------------------------------------------------------------
def _classify_hazard(value):
    """Nilai raster -> (kelas, indeks). Integer 1-3 = kelas; 0-1 desimal = indeks."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None, None
    if v >= 1 and abs(v - round(v)) < 1e-9 and v <= 3:
        return {1: "Rendah", 2: "Sedang", 3: "Tinggi"}[int(round(v))], v
    if 0 <= v <= 1:
        return ("Rendah" if v < 0.33 else "Sedang" if v < 0.66 else "Tinggi"), v
    return None, v


def fetch_hazard(lat, lon):
    """Query semua layer InaRISK. Return dict key -> {label, ok, kelas, value, error}."""
    out = {}
    for key, label_id, label_en, url in HAZARD_LAYERS:
        entry = {"label_id": label_id, "label_en": label_en, "ok": False,
                 "kelas": None, "value": None, "error": None}
        try:
            resp = requests.get(
                f"{url}/identify",
                params={
                    "geometry": f'{{"x":{lon},"y":{lat},"spatialReference":{{"wkid":4326}}}}',
                    "geometryType": "esriGeometryPoint", "returnGeometry": "false",
                    "returnCatalogItems": "false", "f": "json",
                },
                timeout=15, headers={"User-Agent": "HousingAppraisalSystem/1.0"},
            )
            data = resp.json()
            if "error" in data:
                entry["error"] = str(data["error"].get("message", data["error"]))
            else:
                raw = data.get("value")
                if raw in (None, "", "NoData"):
                    entry["error"] = "NoData"
                else:
                    kelas, val = _classify_hazard(raw)
                    entry.update(ok=kelas is not None, kelas=kelas, value=val,
                                 error=None if kelas else f"unrecognised value {raw}")
        except (requests.RequestException, ValueError) as e:
            entry["error"] = f"{type(e).__name__}: {e}"
        out[key] = entry
    return out


def fetch_gis_raw(lat, lon):
    """Ambil semua data mentah. Return (ok, raw_dict_or_error). ok=False bila OSM gagal."""
    ok, osm, warnings = fetch_osm(lat, lon)
    if not ok:
        return False, osm
    return True, {"lat": lat, "lon": lon, "osm": osm, "warnings": warnings,
                  "hazard": fetch_hazard(lat, lon)}


# ---------------------------------------------------------------------------
# Analisis
# ---------------------------------------------------------------------------
def _parse_width(v):
    if not v:
        return None
    m = re.search(r"[\d.]+", str(v).replace(",", "."))
    try:
        return float(m.group(0)) if m else None
    except ValueError:
        return None


def _simplify(pl, max_pts=40):
    if len(pl) <= max_pts:
        return [list(p) for p in pl]
    step = math.ceil(len(pl) / max_pts)
    return [list(p) for p in pl[::step]] + [list(pl[-1])]


def analyze_gis(raw, lang="id"):
    """
    raw dari fetch_gis_raw -> (flags, notes, gis).
    flags/notes memakai key yang sama dengan Step 4 lama.
    gis berisi fitur terdekat per kategori, info jalan, bahaya resmi, dan petunjuk restriksi.
    """
    lat, lon = raw["lat"], raw["lon"]
    features = []
    for el in raw.get("osm", []):
        tags = el.get("tags", {}) or {}
        cat = _categorize(tags)
        if not cat:
            continue
        pls = _polylines(el)
        if not pls:
            continue
        area = cat in ("industrial", "cemetery", "landfill", "park")
        dist, nlat, nlon = _distance_and_nearest(lat, lon, pls, area=area)
        if dist == float("inf"):
            continue
        feat = {
            "cat": cat, "name": tags.get("name") or tags.get("brand") or "",
            "dist_m": round(dist), "lat": nlat, "lon": nlon,
            "highway": tags.get("highway"), "width": _parse_width(tags.get("width")),
            "lanes": tags.get("lanes"), "voltage": tags.get("voltage"),
            "surface": tags.get("surface"),
        }
        if cat in ("railway", "power_line", "road", "waterway"):
            feat["line"] = _simplify(max(pls, key=len))
        features.append(feat)

    def nearest(cats, pred=None):
        c = [f for f in features if f["cat"] in cats and (pred is None or pred(f))]
        return min(c, key=lambda f: f["dist_m"]) if c else None

    def desc(f):
        nm = f" \"{f['name']}\"" if f["name"] else ""
        return f"{f['dist_m']} m{nm}"

    L = lambda i, e: _tt(lang, i, e)
    flags, notes = {}, {}

    def set_flag(key, cats, pred=None, thr=None, label_id="", label_en=""):
        f = nearest(cats, pred)
        limit = thr if thr is not None else THRESHOLDS_M[key]
        if f and f["dist_m"] <= limit:
            flags[key] = True
            notes[key] = L(f"{label_id} terdekat {desc(f)} (OSM, ambang {limit} m).",
                           f"Nearest {label_en}: {desc(f)} (OSM, threshold {limit} m).")
        else:
            flags[key] = False
            if f:
                notes[key] = L(f"{label_id} terdekat {desc(f)} — di luar ambang {limit} m.",
                               f"Nearest {label_en}: {desc(f)} — beyond the {limit} m threshold.")
            else:
                notes[key] = L(f"Tidak ada {label_id.lower()} tercatat di OSM sekitar titik ini.",
                               f"No {label_en.lower()} recorded in OSM around this point.")

    set_flag("sutet", {"power_line"}, label_id="SUTET", label_en="high-voltage line")
    set_flag("railway", {"railway"}, label_id="Rel kereta", label_en="railway")
    set_flag("industry", {"industrial"}, label_id="Kawasan industri", label_en="industrial area")
    set_flag("hospital", {"hospital"}, label_id="Rumah sakit", label_en="hospital")
    set_flag("school", {"school"}, label_id="Sekolah", label_en="school")
    set_flag("market", {"market"}, label_id="Pasar", label_en="market")
    set_flag("main_road", {"road"}, pred=lambda f: f["highway"] in MAIN_ROAD_TYPES,
             label_id="Jalan utama", label_en="main road")
    set_flag("public_facilities",
             {"hospital", "clinic", "school", "market", "worship", "park", "bus_station", "cemetery"},
             label_id="Fasilitas umum", label_en="public facility")

    # --- Jalan terdekat (akses / lebar) ---
    road = nearest({"road"})
    road_info = None
    if road:
        hw, w = road["highway"], road["width"]
        if w is not None:
            narrow = w < 3.0
            verdict = L(f"lebar tercatat {w:g} m" + (" — SEMPIT (<3 m)" if narrow else ""),
                        f"recorded width {w:g} m" + (" — NARROW (<3 m)" if narrow else ""))
        elif hw in NARROW_ROAD_TYPES:
            narrow = True
            verdict = L("kelas jalan kecil/gang (lebar tidak tercatat di OSM) — kemungkinan SEMPIT, cek lapangan",
                        "small road/alley class (width not recorded in OSM) — likely NARROW, verify on site")
        elif hw in ("residential", "unclassified"):
            narrow = None
            verdict = L("jalan lingkungan (lebar tidak tercatat di OSM) — cek lapangan/citra satelit",
                        "residential street (width not recorded in OSM) — verify on site/satellite")
        else:
            narrow = False
            verdict = L("kelas jalan cukup besar", "larger road class")
        road_info = {"name": road["name"], "highway": hw, "width": w, "dist_m": road["dist_m"],
                     "narrow": narrow, "verdict": verdict, "lanes": road["lanes"],
                     "surface": road["surface"]}

    # --- Bahaya resmi ---
    hazard = raw.get("hazard", {}) or {}
    fl = hazard.get("banjir", {})
    if fl.get("ok"):
        flags["flood_risk"] = fl["kelas"] in ("Sedang", "Tinggi")
        notes["flood_risk"] = L(
            f"BNPB InaRISK: bahaya banjir {fl['kelas']} (nilai {fl['value']:g}).",
            f"BNPB InaRISK: flood hazard {fl['kelas']} (value {fl['value']:g}).")
    else:
        near_water = nearest({"waterway"})
        flags["flood_risk"] = False
        base = L("Data resmi InaRISK tidak tersedia.", "Official InaRISK data unavailable.")
        if near_water:
            base += L(f" Sungai/kanal terdekat {desc(near_water)} (petunjuk saja, bukan data banjir).",
                      f" Nearest river/canal {desc(near_water)} (hint only, not flood data).")
        notes["flood_risk"] = base

    # --- Petunjuk untuk checkbox restriksi (TIDAK dicentang otomatis; SOP wajib cek lapangan) ---
    hints = {}
    if road_info and road_info["narrow"]:
        hints["akses_sempit"] = L(f"GIS: jalan terdekat {road_info['dist_m']} m — {road_info['verdict']}.",
                                  f"GIS: nearest road {road_info['dist_m']} m — {road_info['verdict']}.")
    risky = [f for f in features if f["cat"] in ("cemetery", "railway", "landfill", "substation") and f["dist_m"] <= 30]
    if risky:
        r0 = min(risky, key=lambda f: f["dist_m"])
        lab = CAT_LABELS[r0["cat"]][1 if lang == "en" else 0]
        hints["berbatasan_lokasi_berisiko"] = L(f"GIS: {lab} hanya {r0['dist_m']} m dari titik.",
                                                f"GIS: {lab} only {r0['dist_m']} m from the point.")
    high = [h for k, h in hazard.items() if k in ("banjir", "longsor") and h.get("ok") and h["kelas"] == "Tinggi"]
    if high:
        names = ", ".join(h["label_en" if lang == "en" else "label_id"] for h in high)
        hints["rawan_bencana"] = L(f"BNPB InaRISK: bahaya TINGGI untuk {names}.",
                                   f"BNPB InaRISK: HIGH hazard for {names}.")

    # Fitur terdekat per kategori (untuk tabel)
    per_cat = {}
    for f in features:
        if f["cat"] not in per_cat or f["dist_m"] < per_cat[f["cat"]]["dist_m"]:
            per_cat[f["cat"]] = f

    gis = {"lat": lat, "lon": lon, "features": features, "nearest": per_cat,
           "road": road_info, "hazard": hazard, "hints": hints,
           "warnings": raw.get("warnings", [])}
    return flags, notes, gis


# ---------------------------------------------------------------------------
# Street View / link peta
# ---------------------------------------------------------------------------
def streetview_link(lat, lon):
    return f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat},{lon}"


def google_maps_link(lat, lon):
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"


def streetview_embed_url(lat, lon):
    """Embed Street View TANPA API key (tidak resmi - bisa kosong kalau tidak ada cakupan)."""
    return f"https://www.google.com/maps?layer=c&cbll={lat},{lon}&cbp=12,0,0,0,0&output=svembed"


def mapillary_link(lat, lon):
    return f"https://www.mapillary.com/app/?lat={lat}&lng={lon}&z=17"