import json
import os
from typing import Dict, List, NamedTuple, Optional
from BaseClasses import ItemClassification

ATS_BASE_ID = 17_000_000

# ── Item ID offsets ────────────────────────────────────────────────────────────
# State unlocks:          ATS_BASE_ID + 0     (slots 0–29)
# Truck model unlocks:    ATS_BASE_ID + 100   (slots 100–149)
# Truck upgrade packs:    ATS_BASE_ID + 200   (slots 200–219)
# Garage deeds:           ATS_BASE_ID + 1000  (slots 1000–1499)
# Recruitment offices:    ATS_BASE_ID + 2000  (slots 2000–2499)
# Filler items:           ATS_BASE_ID + 9000  (slots 9000–9099)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _load(filename: str):
    with open(os.path.join(_DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


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

# DLC name → state id mapping (matches cities.json dlc field)
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

# ── State unlock items ─────────────────────────────────────────────────────────
# California and Nevada are always accessible (base game), so they have no item.
_STATE_UNLOCK_ORDER = list(_DLC_KEY_MAP.keys())  # stable order = stable IDs

STATE_UNLOCK_ITEMS: Dict[str, ATSItemData] = {}
for _i, _dlc_name in enumerate(_STATE_UNLOCK_ORDER):
    _state_id = _DLC_KEY_MAP[_dlc_name]
    STATE_UNLOCK_ITEMS[f"Unlock {_dlc_name}"] = ATSItemData(
        code=ATS_BASE_ID + _i,
        classification=ItemClassification.progression,
        category="state_unlock",
        game_id=_state_id,
    )

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
    "Money Bonus - $5,000": ATSItemData(
        code=ATS_BASE_ID + 9000,
        classification=ItemClassification.filler,
        category="money_bonus",
        game_id="money_5000",
    ),
    "Money Bonus - $10,000": ATSItemData(
        code=ATS_BASE_ID + 9001,
        classification=ItemClassification.filler,
        category="money_bonus",
        game_id="money_10000",
    ),
    "Money Bonus - $25,000": ATSItemData(
        code=ATS_BASE_ID + 9002,
        classification=ItemClassification.filler,
        category="money_bonus",
        game_id="money_25000",
    ),
    "Money Bonus - $50,000": ATSItemData(
        code=ATS_BASE_ID + 9003,
        classification=ItemClassification.filler,
        category="money_bonus",
        game_id="money_50000",
    ),
    "XP Bonus - Small": ATSItemData(
        code=ATS_BASE_ID + 9004,
        classification=ItemClassification.filler,
        category="xp_bonus",
        game_id="xp_small",
    ),
    "XP Bonus - Large": ATSItemData(
        code=ATS_BASE_ID + 9005,
        classification=ItemClassification.filler,
        category="xp_bonus",
        game_id="xp_large",
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
    **STATE_UNLOCK_ITEMS,
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

    # State unlocks — only for enabled DLC states (skip always-accessible ones)
    for dlc_name in _STATE_UNLOCK_ORDER:
        if dlc_name in options.enabled_dlc.value:
            items.append(f"Unlock {dlc_name}")

    # Truck unlocks
    if options.shuffle_trucks:
        items.extend(TRUCK_UNLOCK_ITEMS.keys())

    # Truck upgrade packs
    if options.shuffle_truck_upgrades:
        items.extend(TRUCK_UPGRADE_ITEMS.keys())

    # Garage deeds — only for enabled states
    if options.shuffle_garages:
        _enabled_state_ids = _get_enabled_state_ids(options)
        for name, data in GARAGE_DEED_ITEMS.items():
            city_state = _city_state_map.get(data.game_id)
            if city_state in _enabled_state_ids or city_state in ("california", "nevada"):
                items.append(name)

    # Recruitment office items — only for enabled states
    if options.shuffle_recruitment_offices:
        _enabled_state_ids = _get_enabled_state_ids(options)
        for name, data in RECRUITMENT_OFFICE_ITEMS.items():
            city_id = data.game_id.rsplit("_office_", 1)[0]
            city_state = _city_state_map.get(city_id)
            if city_state in _enabled_state_ids or city_state in ("california", "nevada"):
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
