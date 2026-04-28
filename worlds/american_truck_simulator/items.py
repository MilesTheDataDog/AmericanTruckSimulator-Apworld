import json
import pkgutil
from typing import Dict, List, NamedTuple, Optional
from BaseClasses import ItemClassification

ATS_BASE_ID = 17_000_000

# ── Item ID offsets ────────────────────────────────────────────────────────────
# Truck model unlocks:    ATS_BASE_ID + 100   (slots 100–149)
# Truck upgrade packs:    ATS_BASE_ID + 200   (slots 200–219)
# Garage deeds:           ATS_BASE_ID + 1000  (slots 1000–1499)
# Recruitment offices:    ATS_BASE_ID + 2000  (slots 2000–2499)
# Filler items:           ATS_BASE_ID + 9000  (slots 9000–9099)


def _load(filename: str):
    data = pkgutil.get_data(__name__, f"data/{filename}")
    if data is None:
        raise FileNotFoundError(f"Could not load data/{filename} from apworld")
    return json.loads(data.decode("utf-8"))


class ATSItemData(NamedTuple):
    code: int
    classification: ItemClassification
    # category used by the client to know what to do with this item
    category: str
    # game_id is the machine-readable key sent over the slot-data wire
    game_id: str


# ── Build item tables from JSON data ──────────────────────────────────────────

_cities_data = _load("cities.json")
_trucks_data = _load("trucks.json")

# DLC name → state id mapping (matches cities.json dlc field).
# Used to map the EnabledDLC option values to state IDs for location filtering.
_DLC_KEY_MAP: Dict[str, str] = {
    "Arizona":      "arizona",
    "New Mexico":   "new_mexico",
    "Oregon":       "oregon",
    "Washington":   "washington",
    "Utah":         "utah",
    "Idaho":        "idaho",
    "Colorado":     "colorado",
    "Wyoming":      "wyoming",
    "Montana":      "montana",
    "Texas":        "texas",
    "Oklahoma":     "oklahoma",
    "Kansas":       "kansas",
    "Nebraska":     "nebraska",
    "Arkansas":     "arkansas",
    "Missouri":     "missouri",
    "Iowa":         "iowa",
    "Louisiana":    "louisiana",
}

# ── Truck model unlock items ───────────────────────────────────────────────────
TRUCK_UNLOCK_ITEMS: Dict[str, ATSItemData] = {}
for _i, _truck in enumerate(_trucks_data["shuffle_trucks"]):
    _name = f"Unlock {_truck['manufacturer']} {_truck['model']}"
    if _truck.get("year"):
        _name = f"Unlock {_truck['manufacturer']} {_truck['model']} ({_truck['year']})"
    TRUCK_UNLOCK_ITEMS[_name] = ATSItemData(
        code=ATS_BASE_ID + 100 + _i,
        classification=ItemClassification.useful,
        category="truck_unlock",
        game_id=_truck["id"],
    )

# ── Truck upgrade pack items ───────────────────────────────────────────────────
TRUCK_UPGRADE_ITEMS: Dict[str, ATSItemData] = {}
for _i, _pack in enumerate(_trucks_data["upgrade_packs"]):
    TRUCK_UPGRADE_ITEMS[_pack["name"]] = ATSItemData(
        code=ATS_BASE_ID + 200 + _i,
        classification=ItemClassification.useful,
        category="truck_upgrade",
        game_id=_pack["id"],
    )

# ── Garage deed items ──────────────────────────────────────────────────────────
# One item per city that has a garage, ordered by state then alphabetically.
# This ordering is STABLE — new states append at the end.
GARAGE_DEED_ITEMS: Dict[str, ATSItemData] = {}
_garage_index = 0
for _state in _cities_data["states"]:
    _state_name = _state["name"]
    for _city in _state["cities"]:
        if _city.get("has_garage"):
            _item_name = f"Garage Deed - {_city['name']}, {_state_name}"
            GARAGE_DEED_ITEMS[_item_name] = ATSItemData(
                code=ATS_BASE_ID + 1000 + _garage_index,
                classification=ItemClassification.progression,
                category="garage_deed",
                game_id=_city["id"],
            )
            _garage_index += 1

# ── Recruitment office items ───────────────────────────────────────────────────
# One item per office slot (city_id + sequential suffix for cities with multiple).
RECRUITMENT_OFFICE_ITEMS: Dict[str, ATSItemData] = {}
_office_index = 0
for _state in _cities_data["states"]:
    _state_name = _state["name"]
    for _city in _state["cities"]:
        for _slot in range(_city.get("recruitment_office_count", 0)):
            _suffix = f" #{_slot + 1}" if _city["recruitment_office_count"] > 1 else ""
            _item_name = f"Recruitment Office - {_city['name']}{_suffix}, {_state_name}"
            RECRUITMENT_OFFICE_ITEMS[_item_name] = ATSItemData(
                code=ATS_BASE_ID + 2000 + _office_index,
                classification=ItemClassification.useful,
                category="recruitment_office",
                game_id=f"{_city['id']}_office_{_slot + 1}",
            )
            _office_index += 1

# ── Filler items ───────────────────────────────────────────────────────────────
FILLER_ITEMS: Dict[str, ATSItemData] = {
    "Trucking Permit": ATSItemData(
        code=ATS_BASE_ID + 9000,
        classification=ItemClassification.filler,
        category="filler",
        game_id="filler",
    ),
}

# ── Victory item (not shuffled, placed at goal location) ──────────────────────
VICTORY_ITEM_NAME = "Victory"
VICTORY_ITEM = ATSItemData(
    code=ATS_BASE_ID + 9999,
    classification=ItemClassification.progression,
    category="victory",
    game_id="victory",
)

# ── Combined lookup tables ─────────────────────────────────────────────────────
ALL_ITEMS: Dict[str, ATSItemData] = {
    **TRUCK_UNLOCK_ITEMS,
    **TRUCK_UPGRADE_ITEMS,
    **GARAGE_DEED_ITEMS,
    **RECRUITMENT_OFFICE_ITEMS,
    **FILLER_ITEMS,
    VICTORY_ITEM_NAME: VICTORY_ITEM,
}

ITEM_NAME_TO_ID: Dict[str, int] = {name: data.code for name, data in ALL_ITEMS.items()}


def get_items_for_options(options) -> List[str]:
    """Return the list of item names to place into the pool given player options."""
    items: List[str] = []

    enabled_state_ids = _get_enabled_state_ids(options)

    # Truck unlocks
    if options.shuffle_trucks:
        items.extend(TRUCK_UNLOCK_ITEMS.keys())

    # Truck upgrade packs
    if options.shuffle_truck_upgrades:
        items.extend(TRUCK_UPGRADE_ITEMS.keys())

    # Garage deeds — only for enabled states
    if options.shuffle_garages:
        for name, data in GARAGE_DEED_ITEMS.items():
            city_state = _city_state_map.get(data.game_id)
            if city_state in enabled_state_ids or city_state in ("california", "nevada"):
                items.append(name)

    # Recruitment office items — only for enabled states
    if options.shuffle_recruitment_offices:
        for name, data in RECRUITMENT_OFFICE_ITEMS.items():
            city_id = data.game_id.rsplit("_office_", 1)[0]
            city_state = _city_state_map.get(city_id)
            if city_state in enabled_state_ids or city_state in ("california", "nevada"):
                items.append(name)

    return items


def _get_enabled_state_ids(options) -> set:
    """Return the set of state IDs (from cities.json) for all enabled DLC."""
    return {_DLC_KEY_MAP[dlc] for dlc in options.enabled_dlc.value if dlc in _DLC_KEY_MAP}


# Build city_id → state_id reverse lookup
_city_state_map: Dict[str, str] = {}
for _state in _cities_data["states"]:
    for _city in _state["cities"]:
        _city_state_map[_city["id"]] = _state["id"]
