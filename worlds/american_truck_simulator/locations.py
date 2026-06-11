import json
import pkgutil
from typing import Dict, List, NamedTuple, Optional
from BaseClasses import LocationProgressType

from .items import ATS_BASE_ID

# ── Location ID offsets ────────────────────────────────────────────────────────
# Cargo deliveries:       ATS_BASE_ID + 10000   (slots 10000–10209)
# City first arrivals:    ATS_BASE_ID + 11000   (slots 11000–11272)
# Level milestones:       ATS_BASE_ID + 12000   (slots 12000–12009)
# State first visit:      ATS_BASE_ID + 15000   (slots 15000–15049)
# Goal location:          ATS_BASE_ID + 19999


def _load(filename: str):
    data = pkgutil.get_data(__name__, f"data/{filename}")
    if data is None:
        raise FileNotFoundError(f"Could not load data/{filename} from apworld")
    return json.loads(data.decode("utf-8"))


class ATSLocationData(NamedTuple):
    code: Optional[int]
    region: str            # region name this location lives in
    category: str          # "cargo", "city", "level", "goal"
    # game_id is sent to the client so it knows what in-game event satisfies this check
    game_id: str


_cities_data = _load("cities.json")
_cargo_data = _load("cargo_types.json")

# Build state_id → state display name lookup for region assignment
_state_id_to_name: Dict[str, str] = {
    s["id"]: s["name"] for s in _cities_data["states"]
}

# ── Stable city location ID offsets ───────────────────────────────────────────
# Keyed by ATS internal city token (the canonical game ID, may be truncated).
# IMPORTANT: Never renumber existing entries — doing so invalidates every
# generated seed that used this apworld version.  To add new cities, append
# new tokens at the END of this dict with the next sequential offset (273+).
# The initial 0-272 assignment is alphabetical by token.
_CITY_TOKEN_OFFSET: Dict[str, int] = {
    "aberdeen_wa": 0,
    "abilene": 1,
    "alamogordo": 2,
    "alamosa": 3,
    "albuquerque": 4,
    "alexandria_la": 5,
    "alliance": 6,
    "amarillo": 7,
    "ardmore": 8,
    "artesia": 9,
    "astoria": 10,
    "austin": 11,
    "bakersfield": 12,
    "barstow": 13,
    "baton_rouge": 14,
    "beaumont": 15,
    "bellingham": 16,
    "bend": 17,
    "billings": 18,
    "blythe": 19,
    "boise": 20,
    "bozeman": 21,
    "brownsville": 22,
    "burlington": 23,
    "burlington_ia": 24,
    "burns": 25,
    "butte": 26,
    "camp_verde": 27,
    "cape_girardeau": 28,
    "carlsbad": 29,
    "carlsbad_nm": 30,
    "carson_city": 31,
    "casper": 32,
    "cedar_city": 33,
    "cedar_rapids": 34,
    "chadron": 35,
    "cheyenne": 36,
    "clifton": 37,
    "clinton": 38,
    "clovis": 39,
    "cody": 40,
    "coeur_dalene": 41,
    "colby": 42,
    "colorado_spr": 43,
    "columbia_mo": 44,
    "columbus": 45,
    "colville": 46,
    "coos_bay": 47,
    "corpus_chris": 48,
    "council_bluffs": 49,
    "dalhart": 50,
    "dallas": 51,
    "davenport": 52,
    "del_rio": 53,
    "denver": 54,
    "deridder": 55,
    "des_moines": 56,
    "dodge_city": 57,
    "dubuque": 58,
    "durango": 59,
    "ehrenberg": 60,
    "el_centro": 61,
    "el_dorado": 62,
    "el_paso": 63,
    "elko": 64,
    "ely": 65,
    "emporia": 66,
    "enid": 67,
    "eugene": 68,
    "eureka": 69,
    "evanston": 70,
    "everett": 71,
    "farmington": 72,
    "fayetteville": 73,
    "flagstaff": 74,
    "fort_collins": 75,
    "fort_dodge": 76,
    "fort_smith": 77,
    "fort_stockto": 78,
    "fort_worth": 79,
    "fresno": 80,
    "g_canyon_vlg": 81,
    "gallup": 82,
    "galveston": 83,
    "garden_city": 84,
    "gillette": 85,
    "glasgow_mt": 86,
    "glendive": 87,
    "grand_coulee": 88,
    "grand_island": 89,
    "grand_juncti": 90,
    "grangeville": 91,
    "great_falls": 92,
    "guymon": 93,
    "harrison": 94,
    "havre": 95,
    "hays": 96,
    "helena": 97,
    "hilt": 98,
    "hobbs": 99,
    "holbrook": 100,
    "hornbrook": 101,
    "hot_springs": 102,
    "houma": 103,
    "houston": 104,
    "huntsville": 105,
    "huron": 106,
    "hutchinson": 107,
    "idabel": 108,
    "idaho_falls": 109,
    "indio": 110,
    "iowa_city": 111,
    "jackpot": 112,
    "jackson": 113,
    "jefferson_city": 114,
    "jonesboro": 115,
    "joplin": 116,
    "junction": 117,
    "junction_cty": 118,
    "kalispell": 119,
    "kansas_ci_ks": 120,
    "kansas_city_mo": 121,
    "kayenta": 122,
    "kennewick": 123,
    "ketchum": 124,
    "kingman": 125,
    "kirksville": 126,
    "klamath_f": 127,
    "lafayette_la": 128,
    "lake_charles": 129,
    "lake_havasu": 130,
    "lakeview": 131,
    "lamar": 132,
    "laramie": 133,
    "laredo": 134,
    "las_cruces": 135,
    "las_vegas": 136,
    "laurel": 137,
    "lawton": 138,
    "lewiston": 139,
    "lewistown": 140,
    "lincoln": 141,
    "little_rock": 142,
    "logan": 143,
    "longview": 144,
    "longview_tx": 145,
    "los_angeles": 146,
    "lubbock": 147,
    "lufkin": 148,
    "marysville": 149,
    "maryville_mo": 150,
    "mason_city": 151,
    "mcalester": 152,
    "mcallen": 153,
    "mccook": 154,
    "medford": 155,
    "miles_city": 156,
    "missoula": 157,
    "moab": 158,
    "modesto": 159,
    "mojave": 160,
    "monroe_la": 161,
    "montrose": 162,
    "nampa": 163,
    "natchitoches": 164,
    "new_orleans": 165,
    "newport": 166,
    "nogales": 167,
    "norfolk": 168,
    "north_platte": 169,
    "oakdale": 170,
    "oakland": 171,
    "odessa": 172,
    "ogden": 173,
    "oklahoma_cit": 174,
    "olympia": 175,
    "omaha": 176,
    "omak": 177,
    "ontario": 178,
    "ottumwa": 179,
    "oxnard": 180,
    "page": 181,
    "pajarito": 182,
    "pedro": 183,
    "pendleton": 184,
    "phillipsburg": 185,
    "phoenix": 186,
    "pine_bluff": 187,
    "pioche": 188,
    "pittsburg": 189,
    "pocatello": 190,
    "poplar_bluff": 191,
    "port_angeles": 192,
    "port_fourchon": 193,
    "portland": 194,
    "price": 195,
    "primm": 196,
    "provo": 197,
    "pueblo": 198,
    "rangely": 199,
    "raton": 200,
    "rawlins": 201,
    "redding": 202,
    "reno": 203,
    "riverton": 204,
    "rock_springs": 205,
    "rolla": 206,
    "roswell": 207,
    "sacramento": 208,
    "salem": 209,
    "salina": 210,
    "salina_ks": 211,
    "salmon": 212,
    "salt_lake": 213,
    "san_angelo": 214,
    "san_antonio": 215,
    "san_diego": 216,
    "san_francisc": 217,
    "san_jose": 218,
    "san_rafael": 219,
    "san_simon": 220,
    "sandpoint": 221,
    "santa_cruz": 222,
    "santa_fe": 223,
    "santa_maria": 224,
    "scottsbluff": 225,
    "seattle": 226,
    "sheridan": 227,
    "show_low": 228,
    "shreveport": 229,
    "sidney": 230,
    "sidney_ne": 231,
    "sierra_vista": 232,
    "sioux_city": 233,
    "socorro": 234,
    "spokane": 235,
    "springdale": 236,
    "springfield_mo": 237,
    "st_george": 238,
    "st_joseph": 239,
    "st_louis": 240,
    "steamboat_sp": 241,
    "sterling": 242,
    "stockton": 243,
    "tacoma": 244,
    "texarkana": 245,
    "texarkana_ar": 246,
    "the_dalles": 247,
    "thompson_f": 248,
    "tonopah": 249,
    "topeka": 250,
    "truckee": 251,
    "tucson": 252,
    "tucumcari": 253,
    "tulsa": 254,
    "twin_falls": 255,
    "tyler": 256,
    "ukiah": 257,
    "valentine": 258,
    "van_horn": 259,
    "vancouver": 260,
    "vernal": 261,
    "victoria": 262,
    "waco": 263,
    "waterloo": 264,
    "wenatchee": 265,
    "wichita": 266,
    "wichita_fall": 267,
    "winnemucca": 268,
    "winslow": 269,
    "woodward": 270,
    "yakima": 271,
    "yuma": 272,
    # ── Future cities: append here with offset 273, 274, ... ──────────────────
}

# ── Level milestone locations ──────────────────────────────────────────────────
_MILESTONE_LEVELS = [5, 10, 15, 20, 25, 30]

LEVEL_MILESTONE_LOCATIONS: Dict[str, ATSLocationData] = {}
for _i, _lvl in enumerate(_MILESTONE_LEVELS):
    LEVEL_MILESTONE_LOCATIONS[f"Reached Level {_lvl}"] = ATSLocationData(
        code=ATS_BASE_ID + 12000 + _i,
        region="Menu",
        category="level",
        game_id=f"level_{_lvl}",
    )

# ── Cargo delivery locations ───────────────────────────────────────────────────
# All cargo types land in "Menu" (always reachable) regardless of required_dlc.
# ATS job markets can offer any cargo type even without owning the DLC state
# where it was introduced, so filtering by DLC would silently block valid checks.
CARGO_DELIVERY_LOCATIONS: Dict[str, ATSLocationData] = {}
for _i, _cargo in enumerate(_cargo_data["cargo_types"]):
    CARGO_DELIVERY_LOCATIONS[f"Delivered - {_cargo['name']}"] = ATSLocationData(
        code=ATS_BASE_ID + 10000 + _i,
        region="Menu",
        category="cargo",
        game_id=_cargo["id"],
    )

# ── City first arrival locations ───────────────────────────────────────────────
# IDs are stable: determined by _CITY_TOKEN_OFFSET, not by position in cities.json.
# Adding or reordering cities in cities.json does NOT shift any existing ID.
CITY_ARRIVAL_LOCATIONS: Dict[str, ATSLocationData] = {}

for _state in _cities_data["states"]:
    _state_name = _state["name"]
    _region_name = _state_name  # region names match state names exactly

    for _city in _state["cities"]:
        _token = _city["id"]
        _offset = _CITY_TOKEN_OFFSET.get(_token)
        if _offset is None:
            # Token not yet in the stable table — skip (add to table to enable)
            import warnings
            warnings.warn(
                f"[ATS] City token '{_token}' not in _CITY_TOKEN_OFFSET — "
                "add it at the end of the dict with the next offset.",
                stacklevel=2,
            )
            continue

        _city_display = f"{_city['name']}, {_state_name}"
        CITY_ARRIVAL_LOCATIONS[f"First Arrival - {_city_display}"] = ATSLocationData(
            code=ATS_BASE_ID + 11000 + _offset,
            region=_region_name,
            category="city",
            game_id=_token,
        )

# ── Stable state first-visit ID offsets ───────────────────────────────────────
# Keyed by ATS internal state token (cities.json state id field).
# IMPORTANT: Never renumber existing entries — doing so invalidates every
# generated seed.  To add new DLC states, append at the END with the next
# sequential offset (17+).  California and Nevada are omitted deliberately
# (always accessible; no unlock gate).
# Initial 0-16 assignment follows the cities.json state order at v1.2.
_STATE_TOKEN_OFFSET: Dict[str, int] = {
    "arizona":    0,
    "new_mexico": 1,
    "oregon":     2,
    "washington": 3,
    "utah":       4,
    "idaho":      5,
    "colorado":   6,
    "wyoming":    7,
    "montana":    8,
    "texas":      9,
    "oklahoma":   10,
    "kansas":     11,
    "nebraska":   12,
    "arkansas":   13,
    "missouri":   14,
    "iowa":       15,
    "louisiana":  16,
    # ── Future DLC states: append here with offset 17, 18, ... ───────────────
}

# States excluded from arrival checks (always accessible, no DLC gate).
_STATES_EXCLUDED = frozenset({"california", "nevada"})

# ── State first visit locations ────────────────────────────────────────────────
# One location per DLC state; IDs stable via _STATE_TOKEN_OFFSET above.
# Generated codes are identical to the previous positional scheme — no seed
# regeneration is required when upgrading from any prior v1.2 apworld.
STATE_ARRIVAL_LOCATIONS: Dict[str, ATSLocationData] = {}
for _state in _cities_data["states"]:
    _sid = _state["id"]
    if _sid in _STATES_EXCLUDED:
        continue
    _s_offset = _STATE_TOKEN_OFFSET.get(_sid)
    if _s_offset is None:
        import warnings
        warnings.warn(
            f"[ATS] State token '{_sid}' not in _STATE_TOKEN_OFFSET — "
            "add it at the end of the dict with the next offset.",
            stacklevel=2,
        )
        continue
    STATE_ARRIVAL_LOCATIONS[f"First Visit - {_state['name']}"] = ATSLocationData(
        code=ATS_BASE_ID + 15000 + _s_offset,
        region=_state["name"],
        category="state_arrival",
        game_id=_sid,
    )

# ── Goal location (always exists, victory item placed here) ───────────────────
GOAL_LOCATION_NAME = "Complete the Run"
GOAL_LOCATION = ATSLocationData(
    code=None,  # event location — no code, not sent to server
    region="Menu",
    category="goal",
    game_id="goal",
)

# ── Combined lookup tables ─────────────────────────────────────────────────────
ALL_LOCATIONS: Dict[str, ATSLocationData] = {
    **LEVEL_MILESTONE_LOCATIONS,
    **CARGO_DELIVERY_LOCATIONS,
    **CITY_ARRIVAL_LOCATIONS,
    **STATE_ARRIVAL_LOCATIONS,
    GOAL_LOCATION_NAME: GOAL_LOCATION,
}

LOCATION_NAME_TO_ID: Dict[str, Optional[int]] = {
    name: data.code for name, data in ALL_LOCATIONS.items()
}


def get_locations_for_options(options) -> List[str]:
    """Return the list of location names to include given player options."""
    locations: List[str] = []

    _active_regions = _get_active_regions(options)

    if options.level_milestone_checks:
        locations.extend(LEVEL_MILESTONE_LOCATIONS.keys())

    if options.cargo_delivery_checks:
        for name, data in CARGO_DELIVERY_LOCATIONS.items():
            if data.region == "Menu" or data.region in _active_regions:
                locations.append(name)

    if options.city_arrival_checks:
        for name, data in CITY_ARRIVAL_LOCATIONS.items():
            if data.region in _active_regions:
                locations.append(name)

    if options.state_arrival_checks:
        for name, data in STATE_ARRIVAL_LOCATIONS.items():
            if data.region in _active_regions:
                locations.append(name)

    # Goal location is always included
    locations.append(GOAL_LOCATION_NAME)

    return locations


def _get_active_regions(options) -> set:
    """Return region names (state display names) that are active given options."""
    from .items import _DLC_KEY_MAP
    enabled_ids = {_DLC_KEY_MAP[dlc] for dlc in options.enabled_dlc.value if dlc in _DLC_KEY_MAP}
    active = {"California", "Nevada"}  # always active
    for _state in _cities_data["states"]:
        if _state["id"] in enabled_ids:
            active.add(_state["name"])
    return active
