#!/usr/bin/env python3
"""
build_city_coords.py — Build the validated city coordinate + radius table.

Workflow:
  1. Run extract_ats_data.py against your ATS installation to produce
     ats_city_ids.json (now includes pos_x / pos_z fields).
  2. Run this script from the repo root:
       python build_city_coords.py
  3. Review the cross-check report printed to stdout.
  4. The script writes worlds/american_truck_simulator/data/cities.json
     with pos_x, pos_z, and radius added to every city entry that has
     coordinates available.

Cross-check source: Koenvh1/ETS2-City-Coordinate-Retriever cities_ats.json
  (telemetry-captured coordinates for the original CA+NV launch cities).
  27 of our 273 cities appear in that dataset.  Discrepancy threshold: 500 m.
  Note: city_data SII blocks do not embed pos: coordinates; coordinates must
  come from community data or map-file extraction.
"""

import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Koenvh1 telemetry-verified reference data (original CA+NV, 2015 launch)
# Token mapping notes:
#   Koenvh1 uses game-internal tokens, which ATS truncates to 12 chars.
#   carlsbad     → skipped  (Koenvh1's carlsbad = CA; our NM city is carlsbad_nm)
#   hornbrook    → hilt     (both tokens now coexist in game; hilt is the hub)
#   oakdale      → carlsbad, oakdale, san_rafael still active in game as CA cities
# ---------------------------------------------------------------------------
KOENVH1: dict[str, tuple[float, float]] = {
    "bakersfield":   (-52261.9,  20598.8),
    "barstow":       (-47300.4,  21963.2),
    "carson_city":   (-51957.3,   8904.9),
    "el_centro":     (-41183.4,  29223.9),
    "elko":          (-45027.1,   2043.1),
    "ely":           (-43077.7,   7074.1),
    "eureka":        (-68616.5,   3021.1),
    "fresno":        (-54802.6,  16248.6),
    "hilt":          (-63040.5,  -2368.5),
    "huron":         (-56245.7,  18908.2),
    "jackpot":       (-41684.1,  -1865.5),
    "las_vegas":     (-41596.9,  17626.8),
    "los_angeles":   (-52693.3,  24704.3),
    "oakland":       (-58786.3,  14301.8),
    "oxnard":        (-56628.7,  21492.7),
    "pioche":        (-40938.4,  10214.7),
    "primm":         (-43011.8,  20256.2),
    "redding":       (-61340.0,   2201.1),
    "reno":          (-55425.1,   5836.5),
    "sacramento":    (-59012.1,  10440.7),
    "san_diego":     (-46897.8,  29857.3),
    "san_francisc":  (-60374.2,  13271.0),
    "santa_cruz":    (-58791.1,  18772.1),
    "stockton":      (-57824.6,  12037.9),
    "tonopah":       (-48104.3,  12496.8),
    "truckee":       (-56640.5,   8566.7),
    "winnemucca":    (-50540.7,   1847.2),
}

# ---------------------------------------------------------------------------
# Radius overrides (metres).  Default is 1500.  Larger for cities whose
# in-game representation spans multiple distinct map areas or has wide sprawl.
# ---------------------------------------------------------------------------
LARGE_RADIUS: dict[str, int] = {
    # Tier 1 — multiple distinct mapped hubs / very wide in-game footprint
    "los_angeles":    4000,
    "dallas":         3500,
    "houston":        3500,
    "phoenix":        3500,
    # Tier 2 — single hub but large or sprawling
    "san_antonio":    3000,
    "austin":         2500,
    "fort_worth":     2500,
    "san_diego":      2500,
    "seattle":        2500,
    "portland":       2500,
    "denver":         2500,
    "las_vegas":      2500,
    "salt_lake":      2500,
    "kansas_ci_ks":   2500,
    "kansas_city_mo": 2500,
    "oklahoma_cit":   2500,
    "el_paso":        2500,
    "tulsa":          2500,
    "omaha":          2500,
    "st_louis":       2500,
    "new_orleans":    2500,
    # Tier 3 — medium-large, slightly larger than default
    "san_francisc":   2000,
    "san_jose":       2000,
    "sacramento":     2000,
    "albuquerque":    2000,
    "tucson":         2000,
    "fresno":         2000,
    "reno":           2000,
    "spokane":        2000,
    "boise":          2000,
    "colorado_spr":   2000,
    "amarillo":       2000,
    "lubbock":        2000,
    "wichita":        2000,
    "des_moines":     2000,
    "little_rock":    2000,
    "baton_rouge":    2000,
    "shreveport":     2000,
}
DEFAULT_RADIUS = 1500
DISCREPANCY_THRESHOLD = 500  # metres


def load_cities_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_extracted(path: Path) -> dict[str, dict]:
    """Return {token: entry} from ats_city_ids.json."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {e["internal_id"]: e for e in data}


def cross_check(extracted: dict[str, dict]) -> None:
    print("\n=== Cross-check: def.scs extraction vs. Koenvh1 telemetry ===")
    agreements = 0
    discrepancies = []
    missing_in_extract = []

    for token, (kx, kz) in sorted(KOENVH1.items()):
        if token not in extracted:
            missing_in_extract.append(token)
            continue
        entry = extracted[token]
        if "pos_x" not in entry:
            print(f"  [WARN] {token}: in extracted file but missing pos_x")
            continue
        ex, ez = entry["pos_x"], entry["pos_z"]
        dx, dz = abs(ex - kx), abs(ez - kz)
        dist = (dx**2 + dz**2) ** 0.5
        if dist <= DISCREPANCY_THRESHOLD:
            agreements += 1
            print(f"  OK    {token:25s}  extract=({ex:9.1f}, {ez:9.1f})  "
                  f"koenvh1=({kx:9.1f}, {kz:9.1f})  diff={dist:.0f}m")
        else:
            discrepancies.append((token, ex, ez, kx, kz, dist))
            print(f"  DIFF  {token:25s}  extract=({ex:9.1f}, {ez:9.1f})  "
                  f"koenvh1=({kx:9.1f}, {kz:9.1f})  diff={dist:.0f}m  *** LARGE")

    if missing_in_extract:
        print(f"\n  Tokens in Koenvh1 but not in extracted file: {missing_in_extract}")
    print(f"\n  Agreements (≤{DISCREPANCY_THRESHOLD}m): {agreements}/{len(KOENVH1)}")
    if discrepancies:
        print(f"  Large discrepancies: {len(discrepancies)}")
        print("  ACTION REQUIRED: verify these cities manually before proceeding.")
    else:
        print("  All checked cities within threshold — coordinates validated.")


def build_table(
    cities_json: dict,
    extracted: dict[str, dict],
) -> tuple[dict, list[str], list[str]]:
    """
    Merge coordinates + radii into cities_json structure.
    Returns (updated_json, missing_coords, extra_tokens).
    """
    our_tokens: set[str] = set()
    for state in cities_json["states"]:
        for city in state["cities"]:
            our_tokens.add(city["id"])

    extract_tokens = set(extracted.keys())
    extra_in_extract = sorted(extract_tokens - our_tokens)
    missing_coords: list[str] = []

    for state in cities_json["states"]:
        for city in state["cities"]:
            token = city["id"]
            entry = extracted.get(token, {})
            if "pos_x" in entry:
                city["pos_x"] = entry["pos_x"]
                city["pos_z"] = entry["pos_z"]
            else:
                missing_coords.append(token)
            city["radius"] = LARGE_RADIUS.get(token, DEFAULT_RADIUS)

    return cities_json, missing_coords, extra_in_extract


def main():
    repo_root = Path(__file__).parent
    extract_file = Path("ats_city_ids.json")
    cities_file = repo_root / "worlds" / "american_truck_simulator" / "data" / "cities.json"

    if not extract_file.exists():
        print("ERROR: ats_city_ids.json not found.")
        print("  Run: python extract_ats_data.py <path-to-ATS-dir>")
        print("  The modified extractor now includes pos_x/pos_z fields.")
        sys.exit(1)

    print(f"Loading {extract_file} ...")
    extracted = load_extracted(extract_file)
    with_coords = sum(1 for e in extracted.values() if "pos_x" in e)
    print(f"  {len(extracted)} cities extracted, {with_coords} have coordinates.")

    print(f"\nLoading {cities_file} ...")
    cities_json = load_cities_json(cities_file)
    total_cities = sum(len(s["cities"]) for s in cities_json["states"])
    print(f"  {total_cities} cities in our game database.")

    cross_check(extracted)

    print("\n=== Building annotated cities.json ===")
    updated, missing_coords, extra_tokens = build_table(cities_json, extracted)

    if extra_tokens:
        print(f"\n  Tokens in def.scs but NOT in our cities.json ({len(extra_tokens)}):")
        for t in extra_tokens:
            print(f"    {t}")
        print("  These may be removed cities, scenario cities, or modded content.")

    if missing_coords:
        print(f"\n  Cities with no extracted coordinates ({len(missing_coords)}):")
        for t in sorted(missing_coords):
            print(f"    {t}")
        print("  These cities will have no pos_x/pos_z — proximity detection will skip them.")
    else:
        covered = sum(1 for s in updated["states"] for c in s["cities"] if "pos_x" in c)
        print(f"  All {covered} cities have coordinates.")

    # Summary by state
    print("\n  Coordinates by state:")
    for state in updated["states"]:
        covered = sum(1 for c in state["cities"] if "pos_x" in c)
        total = len(state["cities"])
        bar = "OK" if covered == total else f"PARTIAL {covered}/{total}"
        print(f"    {state['name']:20s}  {bar}")

    # Radius summary
    custom = [(c["id"], c["radius"]) for s in updated["states"]
              for c in s["cities"] if c.get("radius", DEFAULT_RADIUS) != DEFAULT_RADIUS]
    print(f"\n  Cities with non-default radius ({len(custom)}):")
    for cid, r in sorted(custom, key=lambda x: -x[1]):
        print(f"    {cid:30s}  {r} m")

    out = cities_file
    with open(out, "w", encoding="utf-8") as f:
        json.dump(updated, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"\nWrote {out}")
    print("Phase 1 complete — review report above, then proceed to Phase 2 (DLL wiring).")


if __name__ == "__main__":
    main()
