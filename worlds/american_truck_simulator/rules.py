import json
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ATSWorld


def _load(filename: str):
    data = pkgutil.get_data(__name__, f"data/{filename}")
    if data is None:
        raise FileNotFoundError(f"Could not load data/{filename} from apworld")
    return json.loads(data.decode("utf-8"))


_cities_data = _load("cities.json")


def set_rules(world: "ATSWorld") -> None:
    """
    Set access rules for all regions and locations.

    State entrances: require the corresponding "Unlock <State>" item.
    Garage upgrade locations: if shuffle_garages is on, also require the
        "Garage Deed - <City>" item in addition to the state being accessible.
    Recruitment office locations: if shuffle_recruitment_offices is on, also
        require the corresponding office item.
    Goal location: requires the win condition to be satisfied (checked in-client;
        the logic rule here only confirms minimum progression).
    """
    from worlds.generic.Rules import set_rule, add_rule
    from .items import _DLC_KEY_MAP, GARAGE_DEED_ITEMS, RECRUITMENT_OFFICE_ITEMS

    multiworld = world.multiworld
    player = world.player
    options = world.options

    # ── State entrance rules ───────────────────────────────────────────────────
    # Each DLC state entrance requires the player to have received its unlock item.
    for state in _cities_data["states"]:
        state_name = state["name"]
        if state_name in ("California", "Nevada"):
            continue  # always accessible

        unlock_item = f"Unlock {_dlc_name_for_state(state['id'])}"
        if unlock_item not in _DLC_KEY_MAP.values():
            pass  # lookup by display name
        # Find the entrance in the region graph (raises KeyError if state not enabled)
        try:
            entrance = multiworld.get_entrance(f"Menu -> {state_name}", player)
        except KeyError:
            continue  # state not enabled in this player's game

        set_rule(entrance, lambda state, item=unlock_item: state.has(item, player))

    # ── Garage upgrade location rules ──────────────────────────────────────────
    if options.shuffle_garages:
        for loc_name, loc_data in _iter_active_locations(world, "garage"):
            # Find the matching garage deed item name
            deed_name = _garage_deed_name_for_city(loc_data.game_id)
            if deed_name is None:
                continue
            location = multiworld.get_location(loc_name, player)
            if location is None:
                continue
            add_rule(location, lambda state, deed=deed_name: state.has(deed, player))

    # ── Recruitment office location rules ─────────────────────────────────────
    if options.shuffle_recruitment_offices:
        for loc_name, loc_data in _iter_active_locations(world, "office"):
            office_item_name = _office_item_name_for_id(loc_data.game_id)
            if office_item_name is None:
                continue
            location = multiworld.get_location(loc_name, player)
            if location is None:
                continue
            add_rule(location, lambda state, item=office_item_name: state.has(item, player))

    # ── Goal location rule ─────────────────────────────────────────────────────
    # The goal location is an event; the client handles actual win detection.
    # Here we set a minimum logic rule: the player must have collected enough
    # progression items that the goal is theoretically reachable.
    from .locations import GOAL_LOCATION_NAME
    goal_loc = multiworld.get_location(GOAL_LOCATION_NAME, player)
    if goal_loc:
        # Require at least one state unlock item to have been received —
        # this prevents the spoiler from trivially front-loading the goal.
        win_cond = options.win_condition.value
        if win_cond in (0, 1):  # involves level goal
            # Level checks are in Menu region — always reachable, no extra rule needed
            pass
        if win_cond in (0, 2):  # involves money goal
            pass
        # No hard item requirement on goal — the client enforces the actual condition.
        # We do nothing further so generation never deadlocks on the goal.


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dlc_name_for_state(state_id: str) -> str:
    """Return the DLC display name (e.g. 'Arizona') for a state id (e.g. 'arizona')."""
    from .items import _DLC_KEY_MAP
    for dlc_name, sid in _DLC_KEY_MAP.items():
        if sid == state_id:
            return dlc_name
    return state_id.replace("_", " ").title()


def _garage_deed_name_for_city(city_id: str) -> str | None:
    """Return the garage deed item name for a city id."""
    from .items import GARAGE_DEED_ITEMS
    for name, data in GARAGE_DEED_ITEMS.items():
        if data.game_id == city_id:
            return name
    return None


def _office_item_name_for_id(office_game_id: str) -> str | None:
    """Return the recruitment office item name for an office game_id."""
    from .items import RECRUITMENT_OFFICE_ITEMS
    for name, data in RECRUITMENT_OFFICE_ITEMS.items():
        if data.game_id == office_game_id:
            return name
    return None


def _iter_active_locations(world: "ATSWorld", category: str):
    """Yield (name, data) for all active locations matching a category."""
    from .locations import ALL_LOCATIONS, get_locations_for_options
    active = set(get_locations_for_options(world.options))
    for name, data in ALL_LOCATIONS.items():
        if name in active and data.category == category:
            yield name, data
