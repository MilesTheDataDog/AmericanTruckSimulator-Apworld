import json
import pkgutil
from typing import Dict, List, NamedTuple, Optional
from BaseClasses import LocationProgressType

from .items import ATS_BASE_ID

# ── Location ID offsets ────────────────────────────────────────────────────────
# Cargo deliveries:       ATS_BASE_ID + 10000   (slots 10000–10499)
# City first arrivals:    ATS_BASE_ID + 11000   (slots 11000–11999)
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
    category: str          # "cargo", "city", "level", "garage", "office", "goal"
    # game_id is sent to the client so it knows what in-game event satisfies this check
    game_id: str


_cities_data = _load("cities.json")
_cargo_data = _load("cargo_types.json")

# Build state_id → state display name lookup for region assignment
_state_id_to_name: Dict[str, str] = {
    s["id"]: s["name"] for s in _cities_data["states"]
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
# These are built per-state so they live in the correct region.
CITY_ARRIVAL_LOCATIONS: Dict[str, ATSLocationData] = {}

_city_index = 0

for _state in _cities_data["states"]:
    _state_name = _state["name"]
    _region_name = _state_name  # region names match state names exactly

    for _city in _state["cities"]:
        _city_display = f"{_city['name']}, {_state_name}"

        CITY_ARRIVAL_LOCATIONS[f"First Arrival - {_city_display}"] = ATSLocationData(
            code=ATS_BASE_ID + 11000 + _city_index,
            region=_region_name,
            category="city",
            game_id=_city["id"],
        )
        _city_index += 1

# ── State first visit locations ────────────────────────────────────────────────
# One location per DLC state (California and Nevada excluded — always accessible).
# Fires the first time the player arrives in any city within that state.
STATE_ARRIVAL_LOCATIONS: Dict[str, ATSLocationData] = {}
_state_arrival_index = 0
for _state in _cities_data["states"]:
    if _state["name"] in ("California", "Nevada"):
        continue
    STATE_ARRIVAL_LOCATIONS[f"First Visit - {_state['name']}"] = ATSLocationData(
        code=ATS_BASE_ID + 15000 + _state_arrival_index,
        region=_state["name"],
        category="state_arrival",
        game_id=_state["id"],
    )
    _state_arrival_index += 1

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
