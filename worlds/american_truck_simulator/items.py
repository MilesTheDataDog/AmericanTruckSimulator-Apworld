import json
import pkgutil
from typing import Dict, List, NamedTuple, Optional
from BaseClasses import ItemClassification

ATS_BASE_ID = 17_000_000

# ── Item ID offsets ────────────────────────────────────────────────────────────
# Truck model unlocks:    ATS_BASE_ID + 100   (slots 100–149)
# Truck upgrade packs:    ATS_BASE_ID + 200   (slots 200–219)
# Money grant items:      ATS_BASE_ID + 3000  (slots 3000–3002)
# XP grant items:         ATS_BASE_ID + 3100  (slots 3100–3102)
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

# ── Money grant items ──────────────────────────────────────────────────────────
# Received as Archipelago items; client writes total to items.json and the DLL
# adds the amount directly to the player's in-game money via pointer chain.
MONEY_GRANT_ITEMS: Dict[str, ATSItemData] = {
    "Small Money Grant": ATSItemData(
        code=ATS_BASE_ID + 3000,
        classification=ItemClassification.filler,
        category="money_grant",
        game_id="money_10000",
    ),
    "Medium Money Grant": ATSItemData(
        code=ATS_BASE_ID + 3001,
        classification=ItemClassification.filler,
        category="money_grant",
        game_id="money_50000",
    ),
    "Large Money Grant": ATSItemData(
        code=ATS_BASE_ID + 3002,
        classification=ItemClassification.filler,
        category="money_grant",
        game_id="money_150000",
    ),
}

# ── XP grant items ─────────────────────────────────────────────────────────────
# Client writes total to items.json; DLL adds via pointer chain to live XP value.
XP_GRANT_ITEMS: Dict[str, ATSItemData] = {
    "Small XP Grant": ATSItemData(
        code=ATS_BASE_ID + 3100,
        classification=ItemClassification.filler,
        category="xp_grant",
        game_id="xp_100",
    ),
    "Medium XP Grant": ATSItemData(
        code=ATS_BASE_ID + 3101,
        classification=ItemClassification.filler,
        category="xp_grant",
        game_id="xp_500",
    ),
    "Large XP Grant": ATSItemData(
        code=ATS_BASE_ID + 3102,
        classification=ItemClassification.filler,
        category="xp_grant",
        game_id="xp_2500",
    ),
}

# ── Filler items ───────────────────────────────────────────────────────────────
FILLER_ITEMS: Dict[str, ATSItemData] = {
    **MONEY_GRANT_ITEMS,
    **XP_GRANT_ITEMS,
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
    **MONEY_GRANT_ITEMS,
    **XP_GRANT_ITEMS,
    **FILLER_ITEMS,
    VICTORY_ITEM_NAME: VICTORY_ITEM,
}

ITEM_NAME_TO_ID: Dict[str, int] = {name: data.code for name, data in ALL_ITEMS.items()}


def get_items_for_options(options) -> List[str]:
    """Return the list of item names to place into the pool given player options."""
    items: List[str] = []

    # Truck unlocks
    if options.shuffle_trucks:
        items.extend(TRUCK_UNLOCK_ITEMS.keys())

    # Truck upgrade packs
    if options.shuffle_truck_upgrades:
        items.extend(TRUCK_UPGRADE_ITEMS.keys())

    return items


def _get_enabled_state_ids(options) -> set:
    """Return the set of state IDs (from cities.json) for all enabled DLC."""
    return {_DLC_KEY_MAP[dlc] for dlc in options.enabled_dlc.value if dlc in _DLC_KEY_MAP}
