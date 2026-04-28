from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ATSWorld


def set_rules(world: "ATSWorld") -> None:
    """
    Set access rules for locations.

    All state regions are always accessible — no unlock items are required to
    enter a state. Players can visit any state they own DLC for from the start.

    Garage upgrade locations: if shuffle_garages is on, require the
        "Garage Deed - <City>" item before the garage can be purchased.
    """
    from worlds.generic.Rules import add_rule
    from .items import GARAGE_DEED_ITEMS

    multiworld = world.multiworld
    player = world.player
    options = world.options

    # ── Garage upgrade location rules ──────────────────────────────────────────
    if options.shuffle_garages:
        for loc_name, loc_data in _iter_active_locations(world, "garage"):
            deed_name = _garage_deed_name_for_city(loc_data.game_id)
            if deed_name is None:
                continue
            location = multiworld.get_location(loc_name, player)
            if location is None:
                continue
            add_rule(location, lambda state, deed=deed_name: state.has(deed, player))

    # ── Goal location ──────────────────────────────────────────────────────────
    # The client enforces the actual win condition; no hard item rule needed here.
    from .locations import GOAL_LOCATION_NAME
    # (goal location has no rule — completion_condition in generate_basic handles it)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _garage_deed_name_for_city(city_id: str) -> str | None:
    from .items import GARAGE_DEED_ITEMS
    for name, data in GARAGE_DEED_ITEMS.items():
        if data.game_id == city_id:
            return name
    return None


def _iter_active_locations(world: "ATSWorld", category: str):
    from .locations import ALL_LOCATIONS, get_locations_for_options
    active = set(get_locations_for_options(world.options))
    for name, data in ALL_LOCATIONS.items():
        if name in active and data.category == category:
            yield name, data
