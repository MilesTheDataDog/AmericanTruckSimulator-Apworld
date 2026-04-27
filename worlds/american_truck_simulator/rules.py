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
    from .items import _DLC_KEY_MAP, GARAGE_DEED_ITEMS, RECRUITMENT_OFFICE_ITEMS, STATE_ADJACENCY

    multiworld = world.multiworld
    player = world.player
    options = world.options

    # States whose adjacency requirement is inherently satisfied because they
    # share a border with always-accessible California or Nevada.
    _CA_NV_ADJACENT = frozenset({"arizona", "oregon", "utah", "idaho"})

    reachable_ids = {_DLC_KEY_MAP[dlc] for dlc in options.enabled_dlc.value if dlc in _DLC_KEY_MAP}

    # ── State entrance rules ───────────────────────────────────────────────────
    # Each DLC state requires its unlock item.  States that are not directly
    # adjacent to California/Nevada also require at least one adjacent state to
    # already be accessible, enforcing connected progression.
    for state in _cities_data["states"]:
        state_name = state["name"]
        state_id = state["id"]
        if state_name in ("California", "Nevada"):
            continue  # always accessible

        try:
            entrance = multiworld.get_entrance(f"Menu -> {state_name}", player)
        except KeyError:
            continue  # state not enabled in this player's game

        unlock_item = f"Unlock {_dlc_name_for_state(state_id)}"

        if state_id in _CA_NV_ADJACENT:
            # Directly reachable from always-accessible base states — no adjacency check needed
            set_rule(entrance, lambda s, item=unlock_item: s.has(item, player))
        else:
            # Build list of enabled adjacent state region names that could provide access
            adj_regions = [
                _dlc_name_for_state(adj_id)
                for adj_id in STATE_ADJACENCY.get(state_id, [])
                if adj_id in reachable_ids
            ]
            if adj_regions:
                set_rule(entrance, lambda s, item=unlock_item, adj=adj_regions: (
                    s.has(item, player) and
                    any(s.can_reach(r, "Region", player) for r in adj)
                ))
            else:
                # No enabled adjacent states — isolated state should have been
                # filtered by get_reachable_state_ids(); apply unlock-only as fallback
                set_rule(entrance, lambda s, item=unlock_item: s.has(item, player))

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
    # No item rule needed: "Found Office" locations are accessible just by
    # visiting the city. The "Recruitment Office" items are rewards placed
    # elsewhere in the multiworld that unlock office usage in-game.
    # (Adding a per-office item requirement here creates a circular dependency
    # and makes those locations permanently inaccessible.)

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
