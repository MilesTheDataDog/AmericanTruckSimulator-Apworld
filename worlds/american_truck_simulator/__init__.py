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
    import shutil
    import subprocess
    import sys

    def _launch_ats_client():
        from Utils import local_path, is_frozen
        exe = local_path("ATSClient.exe")
        script = local_path("ATSClient.py")

        if is_frozen():
            # sys.executable is Archipelago.exe — it cannot run .py scripts.
            # Prefer a pre-built ATSClient.exe; fall back to any Python on PATH.
            if os.path.isfile(exe):
                subprocess.Popen([exe])
            else:
                python = shutil.which("python") or shutil.which("python3")
                if python and os.path.isfile(script):
                    subprocess.Popen([python, script])
        else:
            # Running from source — sys.executable is python.exe.
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
    ALL_ITEMS,
    ATSItemData,
    ITEM_NAME_TO_ID,
    FILLER_ITEMS,
    VICTORY_ITEM_NAME,
    VICTORY_ITEM,
    get_items_for_options,
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
    release_mode = "auto"

    item_name_to_id = ITEM_NAME_TO_ID
    location_name_to_id = {
        name: code
        for name, code in LOCATION_NAME_TO_ID.items()
        if code is not None
    }

    # Expose item groups for hint purposes
    item_name_groups = {
        "Money Grants": {name for name in ALL_ITEMS if name.endswith("Money Grant")},
        "XP Grants": {name for name in ALL_ITEMS if name.endswith("XP Grant")},
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

    def generate_early(self) -> None:
        import logging
        log = logging.getLogger("Archipelago")
        o = self.options
        log.info(
            f"[ATS] Player {self.player} options: "
            f"win_condition={o.win_condition.value} "
            f"goal_level={o.goal_level.value} "
            f"goal_money={o.goal_money.value}"
        )
        # Warn loudly if every option is at its default — this almost certainly
        # means the YAML options block was not parsed (wrong game key, wrong AP
        # version, or wrong apworld installed).
        if (o.win_condition.value == 0 and o.goal_level.value == 35
                and o.goal_money.value == 1000):
            log.warning(
                f"[ATS] Player {self.player}: all options are at their defaults. "
                "If your YAML sets non-default values, check that the game key "
                "is exactly 'American Truck Simulator:' and that you are using "
                "the current apworld version. You can also use numeric values "
                "(e.g. win_condition: 1) to bypass string-parsing issues."
            )

    def generate_basic(self) -> None:
        self.multiworld.completion_condition[self.player] = lambda state: \
            state.has(VICTORY_ITEM_NAME, self.player)

    def get_filler_item_name(self) -> str:
        return self.random.choice(list(FILLER_ITEMS.keys()))

    def fill_slot_data(self) -> Dict[str, Any]:
        """
        Data sent to the client via the Archipelago server after connection.
        The client uses this to know the player's exact win condition and options.
        game_version acts as a marker: if it's absent from the server's slot_data
        the client knows AP used a built-in world with a broken fill_slot_data()
        and falls back to reading options from the player's YAML file instead.
        """
        o = self.options
        return {
            "game_version": "1.1.0",
            "win_condition": int(o.win_condition.value),
            "goal_level": int(o.goal_level.value),
            "goal_money": int(o.goal_money.value),  # in thousands
            "level_milestone_checks": bool(o.level_milestone_checks.value),
            "cargo_delivery_checks": bool(o.cargo_delivery_checks.value),
            "city_arrival_checks": bool(o.city_arrival_checks.value),
            "state_arrival_checks": bool(o.state_arrival_checks.value),
            "death_link": bool(o.death_link.value),
        }
