"""
╔══════════════════════════════════════════════════════════════╗
║              GeoBound — Asset Generator v2                   ║
║  Generate GeoJSON boundary files → upload GitHub → auto-load ║
╚══════════════════════════════════════════════════════════════╝

CARA PAKAI:
  1. pip install requests
  2. python generate_assets.py
  3. Upload folder output/ ke GitHub repo
  4. Update GITHUB_RAW_URL di geobound.html

ESTIMASI WAKTU (Indonesia):
  Level 4→5 (Provinsi→Kota/Kab): ~5 menit,  ~38 files,  ~8 MB
  Level 5→6 (Kota→Kecamatan):    ~30 menit, ~514 files, ~75 MB
  Level 6→7 (Kec→Kelurahan):     ~3 jam,    ~7000 files (opsional)
"""

import requests, json, os, time
from pathlib import Path

# ══════════════════════════════════════════════════════
# KONFIGURASI
# ══════════════════════════════════════════════════════
OUTPUT_DIR = "output/data"

GENERATE_LEVELS = [
    # (parent_osm_level, child_osm_level, folder_name)
    ("4", "5", "L4_L5"),   # Provinsi → Kota/Kabupaten
    ("5", "6", "L5_L6"),   # Kota/Kab → Kecamatan
    # ("6", "7", "L6_L7"), # Kecamatan → Kelurahan (uncomment jika perlu)
]

# ISO 3166-1 alpha-2, huruf besar. Kosongkan [] untuk semua negara.
COUNTRY_FILTER = ["ID"]

DELAY    = 1.5   # detik jeda antar request
TIMEOUT  = 120   # detik timeout
SKIP_OK  = True  # skip jika file sudah ada (bisa resume)

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
HEADERS = {"User-Agent": "GeoBound-AssetGen/2.0"}
# ══════════════════════════════════════════════════════


def overpass(q):
    for ep in ENDPOINTS:
        try:
            r = requests.post(ep, data={"data": q}, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            print(f"    [warn] {ep.split('/')[2]}: HTTP {r.status_code}")
        except requests.Timeout:
            print(f"    [warn] {ep.split('/')[2]}: timeout")
        except Exception as e:
            print(f"    [warn] {ep.split('/')[2]}: {e}")
    raise RuntimeError("Semua endpoint gagal")


def get_parents(level):
    """Ambil semua wilayah di level tertentu."""
    cc_filter = ""
    if COUNTRY_FILTER:
        cc_regex = "|".join(COUNTRY_FILTER)
        cc_filter = f'["is_in:country_code"~"^({cc_regex})$"]'

    q = f"""
[out:json][timeout:{TIMEOUT}];
relation["admin_level"="{level}"]["boundary"="administrative"]{cc_filter};
out tags;
"""
    data = overpass(q)
    result = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        cc   = tags.get("is_in:country_code", "")
        if COUNTRY_FILTER and cc.upper() not in COUNTRY_FILTER:
            continue
        result.append({
            "osm_id":  el["id"],
            "name":    tags.get("name", f"unnamed_{el['id']}"),
            "name_en": tags.get("name:en", tags.get("name", "")),
            "country": cc,
        })
    result.sort(key=lambda x: x["name"])
    return result


def get_children(parent_id, child_level):
    """
    Ambil boundary anak dalam parent.
    Query format terbukti berhasil:
      relation(ID)->.parent;
      .parent map_to_area ->.area;
      relation(area.area)[admin_level=X]->.children;
    """
    q = f"""
[out:json][timeout:{TIMEOUT}];
relation({parent_id})->.parent;
.parent map_to_area ->.parentArea;
relation(area.parentArea)["admin_level"="{child_level}"]["boundary"="administrative"]->.children;
(.children;);
out geom;
"""
    data = overpass(q)
    return [e for e in data.get("elements", [])
            if e.get("tags", {}).get("admin_level") == child_level]


def el2feature(el):
    """Overpass element → GeoJSON Feature."""
    tags    = el.get("tags", {})
    outers  = [m for m in el.get("members", [])
               if m.get("role") == "outer" and m.get("type") == "way"]
    if not outers:
        return None
    coords = []
    for way in outers:
        coords.extend([[p["lon"], p["lat"]] for p in way.get("geometry", [])])
    if len(coords) < 3:
        return None
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return {
        "type": "Feature",
        "properties": {
            "name":        tags.get("name", ""),
            "name_en":     tags.get("name:en", tags.get("name", "")),
            "admin_level": tags.get("admin_level", ""),
            "osm_id":      str(el.get("id", "")),
        },
        "geometry": {"type": "Polygon", "coordinates": [coords]}
    }


def save_json(data, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def run_pair(parent_lv, child_lv, folder):
    out = f"{OUTPUT_DIR}/{folder}"
    print(f"\n{'═'*60}")
    print(f"  OSM Level {parent_lv} → {child_lv}  |  {out}/")
    print(f"{'═'*60}")

    # Ambil semua parent
    print("  Fetching parent list...")
    parents = get_parents(parent_lv)
    if not parents:
        print("  Tidak ada parent ditemukan!")
        return
    print(f"  → {len(parents)} wilayah")

    # Simpan index
    index = {
        "parent_level": parent_lv, "child_level": child_lv,
        "countries": COUNTRY_FILTER, "count": len(parents),
        "generated": time.strftime("%Y-%m-%d"),
        "regions": [{"osm_id": str(r["osm_id"]), "name": r["name"],
                     "name_en": r["name_en"], "country": r["country"]} for r in parents]
    }
    save_json(index, f"{out}/index.json")
    print(f"  Index: {out}/index.json\n")

    ok = skip = fail = 0
    for i, p in enumerate(parents, 1):
        oid, name = p["osm_id"], p["name"]
        outfile = f"{out}/{oid}.geojson"
        prefix  = f"  [{i:3}/{len(parents)}] {name[:35]:<35} ({oid})"

        if SKIP_OK and os.path.exists(outfile):
            print(f"{prefix} skip"); skip += 1; continue

        try:
            children = get_children(oid, child_lv)
            features = [f for f in (el2feature(e) for e in children) if f]

            geojson = {
                "type": "FeatureCollection",
                "metadata": {
                    "parent_osm_id": str(oid), "parent_name": name,
                    "parent_level": parent_lv, "child_level": child_lv,
                    "count": len(features), "generated": time.strftime("%Y-%m-%d"),
                    "source": "OpenStreetMap contributors / Overpass API",
                },
                "features": features
            }
            save_json(geojson, outfile)
            print(f"{prefix} ✓ {len(features)} children")
            ok += 1
        except Exception as e:
            print(f"{prefix} ✗ {e}")
            fail += 1

        time.sleep(DELAY)

    print(f"\n  Selesai: ✓{ok}  skip:{skip}  ✗{fail}  total:{len(parents)}")


def main():
    print("\n" + "═"*60)
    print("  GeoBound Asset Generator v2")
    print("═"*60)
    print(f"  Output : {OUTPUT_DIR}/")
    print(f"  Negara : {COUNTRY_FILTER or 'SEMUA'}")
    print(f"  Levels : {[(p,c) for p,c,_ in GENERATE_LEVELS]}")

    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    for parent_lv, child_lv, folder in GENERATE_LEVELS:
        run_pair(parent_lv, child_lv, folder)

    # Meta index
    save_json({
        "generated": time.strftime("%Y-%m-%d"),
        "countries": COUNTRY_FILTER,
        "levels": [{"parent": p, "child": c, "dir": f} for p, c, f in GENERATE_LEVELS]
    }, f"{OUTPUT_DIR}/meta.json")

    print(f"""
{'═'*60}
  SELESAI! Langkah selanjutnya:
{'═'*60}

  1. Buat GitHub repo kosong (cth: geobound-data)

  2. Push folder output/:
       cd output
       git init && git add .
       git commit -m "boundary data"
       git remote add origin https://github.com/USERNAME/geobound-data
       git push -u origin main

  3. Update 1 baris di geobound.html:
       const GITHUB_RAW_URL =
         "https://raw.githubusercontent.com/USERNAME/geobound-data/main/data";

  4. Web app langsung load otomatis tanpa upload manual! ✅
{'═'*60}
""")


if __name__ == "__main__":
    main()
