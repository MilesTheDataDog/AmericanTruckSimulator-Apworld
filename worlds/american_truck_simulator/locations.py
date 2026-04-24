import json
import os
from typing import Dict, List, NamedTuple, Optional
from BaseClasses import LocationProgressType

from .items import ATS_BASE_ID

# ── Location ID offsets ────────────────────────────────────────────────────────
# Cargo deliveries:       ATS_BASE_ID + 10000   (slots 10000–10499)
# City first arrivals:    ATS_BASE_ID + 11000   (slots 11000–11999)
# Level milestones:       ATS_BASE_ID + 12000   (slots 12000–12009)
# Garage upgrades:        ATS_BASE_ID + 13000   (slots 13000–13499)
# Recruitment office find:ATS_BASE_ID + 14000   (slots 14000–14499)
# Goal location:          ATS_BASE_ID + 19999

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _load(filename: str):
    with open(os.path.join(_DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


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
# Base game cargo (required_dlc = null) lands in "Menu" — always reachable.
# DLC state cargo (required_dlc = state_id) lands in that state's region so it
# is automatically excluded when the DLC state is not enabled.
CARGO_DELIVERY_LOCATIONS: Dict[str, ATSLocationData] = {}
for _i, _cargo in enumerate(_cargo_data["cargo_types"]):
    _req = _cargo.get("required_dlc")
    _region = _state_id_to_name.get(_req, "Menu") if _req else "Menu"
    CARGO_DELIVERY_LOCATIONS[f"Delivered - {_cargo['name']}"] = ATSLocationData(
        code=ATS_BASE_ID + 10000 + _i,
        region=_region,
        category="cargo",
        game_id=_cargo["id"],
    )

# ── City first arrival, garage upgrade, and recruitment office locations ────────
# These are built per-state so they live in the correct region.
CITY_ARRIVAL_LOCATIONS: Dict[str, ATSLocationData] = {}
GARAGE_UPGRADE_LOCATIONS: Dict[str, ATSLocationData] = {}
RECRUITMENT_OFFICE_LOCATIONS: Dict[str, ATSLocationData] = {}

_city_index = 0
_garage_loc_index = 0
_office_loc_index = 0

for _state in _cities_data["states"]:
    _state_name = _state["name"]
    _region_name = _state_name  # region names match state names exactly

    for _city in _state["cities"]:
        _city_display = f"{_city['name']}, {_state_name}"

        # City first arrival
        CITY_ARRIVAL_LOCATIONS[f"First Arrival - {_city_display}"] = ATSLocationData(
            code=ATS_BASE_ID + 11000 + _city_index,
            region=_region_name,
            category="city",
            game_id=_city["id"],
        )
        _city_index += 1

        # Garage upgrade (fully upgraded = 5 slots)
        if _city.get("has_garage"):
            GARAGE_UPGRADE_LOCATIONS[f"Garage Upgraded - {_city_display}"] = ATSLocationData(
                code=ATS_BASE_ID + 13000 + _garage_loc_index,
                region=_region_name,
                category="garage",
                game_id=_city["id"],
            )
            _garage_loc_index += 1

        # Recruitment office discoveries
        for _slot in range(_city.get("recruitment_office_count", 0)):
            _suffix = f" #{_slot + 1}" if _city["recruitment_office_count"] > 1 else ""
            _office_name = f"Found Office - {_city['name']}{_suffix}, {_state_name}"
            RECRUITMENT_OFFICE_LOCATIONS[_office_name] = ATSLocationData(
                code=ATS_BASE_ID + 14000 + _office_loc_index,
                region=_region_name,
                category="office",
                game_id=f"{_city['id']}_office_{_slot + 1}",
            )
            _office_loc_index += 1

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
    **GARAGE_UPGRADE_LOCATIONS,
    **RECRUITMENT_OFFICE_LOCATIONS,
    GOAL_LOCATION_NAME: GOAL_LOCATION,
}

LOCATION_NAME_TO_ID: Dict[str, Optional[int]] = {
    name: data.code for name, data in ALL_LOCATIONS.items()
}


def get_locations_for_options(options) -> List[str]:
    """Return the list of location names to include given player options."""
    locations: List[str] = []

    _enabled_state_ids = _get_enabled_state_ids(options)
    _active_regions = _get_active_regions(options, _enabled_state_ids)

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

    if options.garage_upgrade_checks:
        for name, data in GARAGE_UPGRADE_LOCATIONS.items():
            if data.region in _active_regions:
                locations.append(name)

    if options.recruitment_office_checks:
        for name, data in RECRUITMENT_OFFICE_LOCATIONS.items():
            if data.region in _active_regions:
                locations.append(name)

    # Goal location is always included
    locations.append(GOAL_LOCATION_NAME)

    return locations


def _get_enabled_state_ids(options) -> set:
    from .items import _DLC_KEY_MAP
    return {_DLC_KEY_MAP[dlc] for dlc in options.enabled_dlc.value if dlc in _DLC_KEY_MAP}


def _get_active_regions(options, enabled_state_ids: set) -> set:
    """Return region names (state names) that are active given options."""
    active = {"California", "Nevada"}  # always active
    for _state in _cities_data["states"]:
        if _state["id"] in enabled_state_ids:
            active.add(_state["name"])
    return active
