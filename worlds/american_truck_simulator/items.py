import json
import pkgutil
from typing import Dict, List, NamedTuple, Optional
from BaseClasses import ItemClassification

ATS_BASE_ID = 17_000_000

# ── Item ID offsets ────────────────────────────────────────────────────────────
# Money grant items:      ATS_BASE_ID + 3000  (slots 3000–3002)
# XP grant items:         ATS_BASE_ID + 3100  (slots 3100–3102)
# Money trap items:       ATS_BASE_ID + 4000  (slots 4000–4006)
# State unlock items:     ATS_BASE_ID + 5000  (slots 5000–5049)
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
        game_id="money_25000",
    ),
    "Large Money Grant": ATSItemData(
        code=ATS_BASE_ID + 3002,
        classification=ItemClassification.filler,
        category="money_grant",
        game_id="money_50000",
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

# ── Money trap items ──────────────────────────────────────────────────────────
# Received as Archipelago trap items; client deducts the fine amount from the
# player's running money total, which the DLL subtracts from live in-game money.
MONEY_TRAP_ITEMS: Dict[str, ATSItemData] = {
    "Fine ($500)": ATSItemData(
        code=ATS_BASE_ID + 4000,
        classification=ItemClassification.trap,
        category="money_trap",
        game_id="money_trap_500",
    ),
    "Fine ($1,000)": ATSItemData(
        code=ATS_BASE_ID + 4001,
        classification=ItemClassification.trap,
        category="money_trap",
        game_id="money_trap_1000",
    ),
    "Fine ($2,500)": ATSItemData(
        code=ATS_BASE_ID + 4002,
        classification=ItemClassification.trap,
        category="money_trap",
        game_id="money_trap_2500",
    ),
    "Fine ($5,000)": ATSItemData(
        code=ATS_BASE_ID + 4003,
        classification=ItemClassification.trap,
        category="money_trap",
        game_id="money_trap_5000",
    ),
    "Fine ($10,000)": ATSItemData(
        code=ATS_BASE_ID + 4004,
        classification=ItemClassification.trap,
        category="money_trap",
        game_id="money_trap_10000",
    ),
    "Fine ($25,000)": ATSItemData(
        code=ATS_BASE_ID + 4005,
        classification=ItemClassification.trap,
        category="money_trap",
        game_id="money_trap_25000",
    ),
    "Fine ($50,000)": ATSItemData(
        code=ATS_BASE_ID + 4006,
        classification=ItemClassification.trap,
        category="money_trap",
        game_id="money_trap_50000",
    ),
}

# Base states are always accessible and never need an unlock item, mirroring how
# ATS ships California + Nevada (and the free Arizona DLC everyone can use).
# They still have First Visit / city checks; they are simply never gated.
BASE_STATE_NAMES: frozenset = frozenset({"California", "Nevada", "Arizona"})

# ── State unlock items (progression) ──────────────────────────────────────────
# When the state_unlocks option is on, receiving "Unlock <State>" lets the client
# release that state's held city/state-arrival checks.  One per PAID map-DLC state.
#
# IMPORTANT: stable IDs keyed by offset.  Never renumber existing entries; add new
# DLC states at the END with the next offset.  Arizona (formerly offset 0) is a
# base state now and has NO unlock item — its old offset 0 is intentionally left
# unused so every other state keeps its established ID.
_STATE_UNLOCK_OFFSET: Dict[str, int] = {
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
    # ── Future DLC states: append here with offset 19, 20, ... ───────────────
}

# state token → display name (from cities.json), for building item names.
_STATE_ID_TO_NAME: Dict[str, str] = {s["id"]: s["name"] for s in _cities_data["states"]}

STATE_UNLOCK_ITEMS: Dict[str, ATSItemData] = {}
for _tok, _off in _STATE_UNLOCK_OFFSET.items():
    _sname = _STATE_ID_TO_NAME.get(_tok, _tok)
    STATE_UNLOCK_ITEMS[f"Unlock {_sname}"] = ATSItemData(
        code=ATS_BASE_ID + 5000 + _off,
        classification=ItemClassification.progression,
        category="state_unlock",
        game_id=_tok,
    )

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
    **MONEY_GRANT_ITEMS,
    **XP_GRANT_ITEMS,
    **MONEY_TRAP_ITEMS,
    **STATE_UNLOCK_ITEMS,
    **FILLER_ITEMS,
    VICTORY_ITEM_NAME: VICTORY_ITEM,
}

ITEM_NAME_TO_ID: Dict[str, int] = {name: data.code for name, data in ALL_ITEMS.items()}


def get_items_for_options(options) -> List[str]:
    """Return the list of pool item names to place given player options.

    Only state-unlock progression items are placed here; grants, traps, and
    filler are added as padding by the world's create_items().  Unlock items are
    created only for enabled DLC states, and only when there are arrival checks
    for them to gate (otherwise they would be dead progression).
    """
    names: List[str] = []
    if not getattr(options, "state_unlocks", None) or not options.state_unlocks.value:
        return names
    # Nothing to gate unless city or state arrival checks are enabled.
    if not (options.city_arrival_checks.value or options.state_arrival_checks.value):
        return names
    enabled_ids = _get_enabled_state_ids(options)
    for tok, off in _STATE_UNLOCK_OFFSET.items():
        if tok in enabled_ids:
            sname = _STATE_ID_TO_NAME.get(tok, tok)
            names.append(f"Unlock {sname}")
    return names


def _get_enabled_state_ids(options) -> set:
    """Return the set of state IDs (from cities.json) for all enabled DLC."""
    return {_DLC_KEY_MAP[dlc] for dlc in options.enabled_dlc.value if dlc in _DLC_KEY_MAP}
