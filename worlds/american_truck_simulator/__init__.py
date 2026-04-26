import json
import os
from typing import Any, Dict, List, Optional

from BaseClasses import Item, ItemClassification, Tutorial
from worlds.AutoWorld import World, WebWorld

# Register the ATS client with the Archipelago Launcher.
# Wrapped in try/except so the world still loads during server-side generation
# where LauncherComponents may not be importable.
try:
    from worlds.LauncherComponents import Component, components
    import subprocess
    import sys

    def _launch_ats_client():
        from Utils import local_path
        script = local_path("ATSClient.py")
        exe = local_path("ATSClient.exe")
        if os.path.isfile(script):
            subprocess.Popen([sys.executable, script])
        elif os.path.isfile(exe):
            subprocess.Popen([exe])

    components += [Component(
        "American Truck Simulator Client",
        "ATSClient",
        func=_launch_ats_client,
    )]
except Exception:
    pass

from .items import (
    ATS_BASE_ID,
    ALL_ITEMS,
    ATSItemData,
    ITEM_NAME_TO_ID,
    FILLER_ITEMS,
    VICTORY_ITEM_NAME,
    VICTORY_ITEM,
    get_items_for_options,
    _DLC_KEY_MAP,
)
from .locations import (
    ALL_LOCATIONS,
    LOCATION_NAME_TO_ID,
    GOAL_LOCATION_NAME,
    get_locations_for_options,
)
from .options import ATSOptions
from .regions import create_regions
from .rules import set_rules


class ATSItem(Item):
    game = "American Truck Simulator"


class ATSWebWorld(WebWorld):
    theme = "ocean"
    tutorials = [
        Tutorial(
            "Multiworld Setup Guide",
            "A guide to setting up American Truck Simulator for Archipelago.",
            "English",
            "setup_en.md",
            "setup/en",
            ["ATS Archipelago Community"],
        )
    ]


class ATSWorld(World):
    """
    American Truck Simulator — drive across the American West, delivering cargo,
    discovering cities, and building your trucking empire. In Archipelago mode,
    states must be unlocked before you can enter them, trucks and upgrades are
    shuffled into the multiworld pool, and your goal is to reach a configurable
    level and/or money target.
    """

    game = "American Truck Simulator"
    options_dataclass = ATSOptions
    options: ATSOptions
    web = ATSWebWorld()

    item_name_to_id = ITEM_NAME_TO_ID
    location_name_to_id = {
        name: code
        for name, code in LOCATION_NAME_TO_ID.items()
        if code is not None
    }

    # Expose item and location groups for hint purposes
    item_name_groups = {
        "State Unlocks": {name for name in ALL_ITEMS if name.startswith("Unlock ") and
                          any(s in name for s in _DLC_KEY_MAP.keys())},
        "Trucks": {name for name in ALL_ITEMS if name.startswith("Unlock ") and
                   not any(s in name for s in _DLC_KEY_MAP.keys())},
        "Garage Deeds": {name for name in ALL_ITEMS if name.startswith("Garage Deed")},
        "Recruitment Offices": {name for name in ALL_ITEMS if name.startswith("Recruitment Office")},
        "Truck Upgrades": {name for name in ALL_ITEMS if any(
            name == pack for pack in [
                "Engine Tier 2", "Engine Tier 3", "Engine Tier 4", "Engine Tier 5",
                "Transmission Tier 2", "Transmission Tier 3", "Transmission Tier 4",
                "Chassis Upgrade Pack", "Cab Upgrade Pack", "Accessories Pack",
            ]
        )},
        "Money Bonuses": {name for name in FILLER_ITEMS if "Money" in name},
    }

    def create_item(self, name: str) -> ATSItem:
        data = ALL_ITEMS[name]
        return ATSItem(name, data.classification, data.code, self.player)

    def create_items(self) -> None:
        pool_names = get_items_for_options(self.options)
        active_loc_names = set(get_locations_for_options(self.options))

        # Count real (non-goal, non-event) locations
        real_loc_count = sum(
            1 for name in active_loc_names
            if name != GOAL_LOCATION_NAME and LOCATION_NAME_TO_ID.get(name) is not None
        )

        # Create items; track how many we have
        items: List[ATSItem] = [self.create_item(name) for name in pool_names]

        # Pad with filler to match location count
        filler_names = list(FILLER_ITEMS.keys())
        filler_idx = 0
        while len(items) < real_loc_count:
            items.append(self.create_item(filler_names[filler_idx % len(filler_names)]))
            filler_idx += 1

        # If we have more items than locations, trim lowest-priority filler
        while len(items) > real_loc_count:
            items.pop()

        for item in items:
            self.multiworld.itempool.append(item)

    def create_regions(self) -> None:
        create_regions(self)

    def set_rules(self) -> None:
        set_rules(self)
        # Place the Victory item at the goal location (event, not in pool)
        self._place_victory()

    def _place_victory(self) -> None:
        goal_loc = self.multiworld.get_location(GOAL_LOCATION_NAME, self.player)
        if goal_loc:
            victory = ATSItem(VICTORY_ITEM_NAME, ItemClassification.progression,
                              None, self.player)
            goal_loc.place_locked_item(victory)

    def generate_basic(self) -> None:
        self.multiworld.completion_condition[self.player] = lambda state: \
            state.has(VICTORY_ITEM_NAME, self.player)

    def get_filler_item_name(self) -> str:
        return self.random.choice(list(FILLER_ITEMS.keys()))

    def fill_slot_data(self) -> Dict[str, Any]:
        """
        Data sent to the client via the Archipelago server after connection.
        The client uses this to know the player's exact win condition and options.
        """
        from .items import _DLC_KEY_MAP
        return {
            "game_version": "1.0.0",
            "win_condition": self.options.win_condition.value,
            "goal_level": self.options.goal_level.value,
            "goal_money": self.options.goal_money.value,  # in thousands
            "enabled_dlc": sorted(self.options.enabled_dlc.value),
            "shuffle_trucks": bool(self.options.shuffle_trucks),
            "shuffle_truck_upgrades": bool(self.options.shuffle_truck_upgrades),
            "shuffle_garages": bool(self.options.shuffle_garages),
            "shuffle_recruitment_offices": bool(self.options.shuffle_recruitment_offices),
            "level_milestone_checks": bool(self.options.level_milestone_checks),
            "cargo_delivery_checks": bool(self.options.cargo_delivery_checks),
            "city_arrival_checks": bool(self.options.city_arrival_checks),
            "garage_upgrade_checks": bool(self.options.garage_upgrade_checks),
            "recruitment_office_checks": bool(self.options.recruitment_office_checks),
            "death_link": bool(self.options.death_link),
        }
