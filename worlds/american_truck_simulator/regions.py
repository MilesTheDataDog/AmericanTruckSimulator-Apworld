import json
import pkgutil
from typing import TYPE_CHECKING
from BaseClasses import Region

if TYPE_CHECKING:
    from . import ATSWorld


def _load(filename: str):
    data = pkgutil.get_data(__name__, f"data/{filename}")
    if data is None:
        raise FileNotFoundError(f"Could not load data/{filename} from apworld")
    return json.loads(data.decode("utf-8"))


_cities_data = _load("cities.json")


def create_regions(world: "ATSWorld") -> None:
    """
    Build all regions and connect them.

    Region layout:
    - "Menu"       → always reachable (Archipelago requires this)
    - "California" → always reachable (base game, always connected from Menu)
    - "Nevada"     → always reachable (base game, always connected from Menu)
    - <DLC State>  → always connected, UNLESS state_unlocks is enabled, in which
                     case its entrance requires the "Unlock <State>" item

    All cargo delivery and level milestone locations live in "Menu" so they are
    always logically reachable.  City arrival and state first-visit locations
    live in the corresponding state region; with state_unlocks on they become
    logically reachable only once that state's unlock item is received.
    """
    from .locations import (
        ALL_LOCATIONS,
        get_locations_for_options,
        GOAL_LOCATION_NAME,
        GOAL_LOCATION,
    )
    from .items import _DLC_KEY_MAP

    multiworld = world.multiworld
    player = world.player
    options = world.options

    active_location_names = set(get_locations_for_options(options))

    # Base states (California, Nevada, Arizona) are always active; paid map-DLC
    # states are active only when enabled.
    from .items import BASE_STATE_NAMES
    enabled_state_ids = {_DLC_KEY_MAP[dlc] for dlc in options.enabled_dlc.value if dlc in _DLC_KEY_MAP}
    active_state_names = set(BASE_STATE_NAMES)
    for state in _cities_data["states"]:
        if state["id"] in enabled_state_ids:
            active_state_names.add(state["name"])

    # Create all active regions
    regions = {}
    for region_name in ["Menu"] + sorted(active_state_names):
        region = Region(region_name, player, multiworld)
        regions[region_name] = region
        multiworld.regions.append(region)

    # Populate each region with its locations
    for loc_name, loc_data in ALL_LOCATIONS.items():
        if loc_name not in active_location_names:
            continue

        target_region_name = loc_data.region
        if target_region_name not in regions:
            # Location's state is not enabled — skip
            continue

        from BaseClasses import Location
        location = ATSLocation(player, loc_name, loc_data.code, regions[target_region_name])
        regions[target_region_name].locations.append(location)

    # Connect Menu → active state regions.
    #
    # With state_unlocks enabled, each paid map-DLC state's entrance requires its
    # "Unlock <State>" progression item, so that state's city/first-visit checks
    # are logically gated behind the unlock.  Base states (California, Nevada,
    # Arizona) are always connected and never gated.  This gating is purely
    # logical — the client never blocks driving; it only holds the checks until
    # the unlock is received.
    unlocks_on = bool(getattr(options, "state_unlocks", None) and options.state_unlocks.value
                      and (options.city_arrival_checks.value or options.state_arrival_checks.value))
    base_states = set(BASE_STATE_NAMES)
    id_to_name = {s["id"]: s["name"] for s in _cities_data["states"]}
    name_to_id = {v: k for k, v in id_to_name.items()}

    menu = regions["Menu"]
    for state_name in active_state_names:
        if unlocks_on and state_name not in base_states:
            _tok = name_to_id.get(state_name)
            _item = f"Unlock {state_name}"
            menu.connect(
                regions[state_name],
                rule=lambda state, item=_item: state.has(item, player),
            )
        else:
            menu.connect(regions[state_name])


class ATSLocation:
    """Thin Location subclass so we don't need to import BaseClasses.Location everywhere."""

    def __new__(cls, player: int, name: str, code, region: Region):
        from BaseClasses import Location
        loc = Location.__new__(Location)
        loc.__init__(player, name, code, region)
        return loc
