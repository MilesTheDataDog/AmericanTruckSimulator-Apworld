"""
American Truck Simulator — Archipelago Client

Run this through the Archipelago Launcher or directly:
    python ATSClient.py

Requires:
  - The ATS Archipelago C++ plugin installed in:
      <ATS install>/bin/win_x64/plugins/ats_archipelago.dll
  - ATS running (plugin writes events to the communication folder)

Communication folder (created automatically):
    %USERPROFILE%/Documents/American Truck Simulator/archipelago/
  Files:
    events.json       — written by plugin; list of game events to process
    items.json        — written by client; current unlocked items state
    slot_data.json    — written by client after connecting; player options
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import colorama
from colorama import Fore, Style

# Archipelago imports — these work when run via the Archipelago launcher
import Utils
from CommonClient import CommonContext, server_loop, gui_enabled, ClientCommandProcessor, logger, get_base_parser
from NetUtils import ClientStatus

colorama.init()

GAME_NAME = "American Truck Simulator"
CLIENT_VERSION = "1.0.0"

# ── Communication folder ───────────────────────────────────────────────────────
def _get_comm_dir() -> Path:
    docs = Path(os.environ.get("USERPROFILE", Path.home())) / "Documents" / "American Truck Simulator" / "archipelago"
    docs.mkdir(parents=True, exist_ok=True)
    return docs


COMM_DIR: Path = _get_comm_dir()
EVENTS_FILE = COMM_DIR / "events.json"
ITEMS_FILE = COMM_DIR / "items.json"
SLOT_DATA_FILE = COMM_DIR / "slot_data.json"

# ── Win condition constants (match options.py) ────────────────────────────────
WIN_LEVEL_AND_MONEY = 0
WIN_LEVEL_ONLY = 1
WIN_MONEY_ONLY = 2
WIN_LEVEL_OR_MONEY = 3


class ATSCommandProcessor(ClientCommandProcessor):
    def _cmd_status(self):
        """Show current ATS game state as seen by the client."""
        ctx: ATSContext = self.ctx
        logger.info(f"[ATS] Plugin connected: {ctx.plugin_connected}")
        logger.info(f"[ATS] Current level:    {ctx.current_level}")
        logger.info(f"[ATS] Current money:    ${ctx.current_money:,}")
        logger.info(f"[ATS] Checks sent:      {len(ctx.checked_locations)}")
        logger.info(f"[ATS] Goal satisfied:   {ctx.goal_complete}")

    def _cmd_resync(self):
        """Re-read the events file and resend any unchecked locations."""
        ctx: ATSContext = self.ctx
        logger.info("[ATS] Resyncing with plugin events file...")
        ctx._process_events_file(force=True)


class ATSContext(CommonContext):
    command_processor = ATSCommandProcessor
    game = GAME_NAME
    items_handling = 0b111  # receive all items

    def __init__(self, server_address: str, password: Optional[str]) -> None:
        super().__init__(server_address, password)

        # Slot data from server
        self.slot_data: Dict[str, Any] = {}

        # Plugin communication state
        self.plugin_connected: bool = False
        self._last_events_mtime: float = 0.0
        self._processed_event_ids: Set[str] = set()

        # Game state tracked by client
        self.current_level: int = 0
        self.current_money: int = 0
        self.goal_complete: bool = False

        # Items received from server (sent to plugin)
        self._unlocked_states: Set[str] = set()
        self._unlocked_trucks: Set[str] = set()
        self._unlocked_upgrade_tiers: Dict[str, int] = {}
        self._unlocked_garages: Set[str] = set()
        self._unlocked_offices: Set[str] = set()
        self._pending_money_bonuses: List[int] = []
        self._pending_xp_bonuses: List[str] = []

    # ── Archipelago callbacks ──────────────────────────────────────────────────

    async def server_auth(self, password_requested: bool = False) -> None:
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    def on_package(self, cmd: str, args: Dict) -> None:
        super().on_package(cmd, args)
        if cmd == "Connected":
            self.slot_data = args.get("slot_data", {})
            self._on_connected()
        elif cmd == "ReceivedItems":
            self._on_items_received(args["items"])

    def _on_connected(self) -> None:
        logger.info(f"[ATS] Connected to Archipelago server as {self.username}")
        logger.info(f"[ATS] Win condition: {self._win_condition_description()}")
        # Write slot data so the plugin/mod can read player options
        _write_json(SLOT_DATA_FILE, self.slot_data)
        self._write_items_file()

    def _on_items_received(self, items) -> None:
        for item in items:
            item_name = self.item_names.lookup_in_game(item.item)
            self._apply_item(item_name)
        self._write_items_file()

    def _apply_item(self, item_name: str) -> None:
        if item_name.startswith("Unlock "):
            rest = item_name[len("Unlock "):]
            # Distinguish state vs truck by checking DLC names
            _dlc_states = {
                "Arizona", "New Mexico", "Oregon", "Washington", "Utah", "Idaho",
                "Colorado", "Wyoming", "Montana", "Texas", "Oklahoma", "Kansas",
                "Nebraska", "Arkansas", "Missouri", "Iowa", "Louisiana",
            }
            if rest in _dlc_states:
                # State unlock — extract state_id from rest
                state_id = rest.lower().replace(" ", "_")
                self._unlocked_states.add(state_id)
                logger.info(f"[ATS] Unlocked state: {rest}")
            else:
                # Truck model unlock — find game_id from item name
                self._unlocked_trucks.add(item_name)
                logger.info(f"[ATS] Unlocked truck: {rest}")

        elif item_name in ("Engine Tier 2", "Engine Tier 3", "Engine Tier 4", "Engine Tier 5"):
            tier = int(item_name.split()[-1])
            self._unlocked_upgrade_tiers["engine"] = max(
                self._unlocked_upgrade_tiers.get("engine", 1), tier
            )
            logger.info(f"[ATS] Engine unlocked to tier {tier}")

        elif item_name in ("Transmission Tier 2", "Transmission Tier 3", "Transmission Tier 4"):
            tier = int(item_name.split()[-1])
            self._unlocked_upgrade_tiers["transmission"] = max(
                self._unlocked_upgrade_tiers.get("transmission", 1), tier
            )

        elif item_name == "Chassis Upgrade Pack":
            self._unlocked_upgrade_tiers["chassis"] = 2

        elif item_name == "Cab Upgrade Pack":
            self._unlocked_upgrade_tiers["cab"] = 2

        elif item_name == "Accessories Pack":
            self._unlocked_upgrade_tiers["accessories"] = 2

        elif item_name.startswith("Garage Deed - "):
            # city id embedded in game_id; look it up
            city_id = self._city_id_from_item_name(item_name, "Garage Deed - ")
            if city_id:
                self._unlocked_garages.add(city_id)
                logger.info(f"[ATS] Garage deed received: {city_id}")

        elif item_name.startswith("Recruitment Office - "):
            city_id = self._city_id_from_item_name(item_name, "Recruitment Office - ")
            if city_id:
                self._unlocked_offices.add(city_id)
                logger.info(f"[ATS] Office unlocked: {city_id}")

        elif item_name.startswith("Money Bonus"):
            amount = self._parse_money_bonus(item_name)
            if amount:
                self._pending_money_bonuses.append(amount)
                logger.info(f"[ATS] Money bonus queued: ${amount:,}")

        elif item_name.startswith("XP Bonus"):
            self._pending_xp_bonuses.append(item_name)
            logger.info(f"[ATS] XP bonus queued: {item_name}")

    # ── Items file (client → plugin) ───────────────────────────────────────────

    def _write_items_file(self) -> None:
        """Write the current unlocked-items state for the plugin/mod to read."""
        # California and Nevada are always unlocked
        all_unlocked_states = {"california", "nevada"} | self._unlocked_states

        payload = {
            "version": 1,
            "timestamp": time.time(),
            "unlocked_states": sorted(all_unlocked_states),
            "unlocked_trucks": sorted(self._unlocked_trucks),
            "upgrade_tiers": self._unlocked_upgrade_tiers,
            "unlocked_garages": sorted(self._unlocked_garages),
            "unlocked_offices": sorted(self._unlocked_offices),
            "pending_money_bonuses": self._pending_money_bonuses[:],
            "pending_xp_bonuses": self._pending_xp_bonuses[:],
            "win_condition": self.slot_data.get("win_condition", 0),
            "goal_level": self.slot_data.get("goal_level", 35),
            "goal_money_thousands": self.slot_data.get("goal_money", 1000),
            "shuffle_trucks": self.slot_data.get("shuffle_trucks", True),
            "shuffle_garages": self.slot_data.get("shuffle_garages", True),
            "shuffle_recruitment_offices": self.slot_data.get("shuffle_recruitment_offices", True),
            "shuffle_truck_upgrades": self.slot_data.get("shuffle_truck_upgrades", False),
        }
        _write_json(ITEMS_FILE, payload)

    # ── Events file (plugin → client) ─────────────────────────────────────────

    def _process_events_file(self, force: bool = False) -> None:
        """Read plugin-generated events and send location checks to server."""
        if not EVENTS_FILE.exists():
            if not self.plugin_connected:
                return
            return

        try:
            mtime = EVENTS_FILE.stat().st_mtime
        except OSError:
            return

        if not force and mtime <= self._last_events_mtime:
            return

        self._last_events_mtime = mtime

        try:
            data = _read_json(EVENTS_FILE)
        except Exception:
            return

        if not isinstance(data, dict):
            return

        self.plugin_connected = data.get("plugin_alive", False)
        self.current_level = data.get("current_level", self.current_level)
        self.current_money = data.get("current_money", self.current_money)

        new_checks: List[int] = []

        for event in data.get("events", []):
            event_id = event.get("id")
            if event_id in self._processed_event_ids:
                continue
            self._processed_event_ids.add(event_id)

            location_id = self._resolve_event_to_location_id(event)
            if location_id is not None and location_id not in self.checked_locations:
                new_checks.append(location_id)

        if new_checks:
            asyncio.create_task(self.send_msgs([{
                "cmd": "LocationChecks",
                "locations": new_checks,
            }]))

        # Check win condition
        if not self.goal_complete and self._check_win_condition():
            self.goal_complete = True
            asyncio.create_task(self.send_msgs([{
                "cmd": "StatusUpdate",
                "status": ClientStatus.CLIENT_GOAL,
            }]))
            logger.info("[ATS] Goal complete! Congratulations!")

        # Clear delivered bonuses (plugin acknowledges via events file)
        delivered = data.get("delivered_bonuses", [])
        for bonus in delivered:
            if bonus in self._pending_money_bonuses:
                self._pending_money_bonuses.remove(bonus)

    def _resolve_event_to_location_id(self, event: Dict) -> Optional[int]:
        """Map a plugin event to an Archipelago location ID."""
        from worlds.american_truck_simulator.locations import ALL_LOCATIONS

        etype = event.get("type")
        game_id = event.get("game_id", "")

        if etype == "cargo_delivered":
            loc_name = f"Delivered - {event.get('cargo_name', '')}"
        elif etype == "city_arrived":
            loc_name = f"First Arrival - {event.get('city_display', '')}"
        elif etype == "level_reached":
            loc_name = f"Reached Level {event.get('level', 0)}"
        elif etype == "garage_upgraded":
            loc_name = f"Garage Upgraded - {event.get('city_display', '')}"
        elif etype == "office_found":
            loc_name = f"Found Office - {event.get('office_display', '')}"
        else:
            return None

        loc_data = ALL_LOCATIONS.get(loc_name)
        if loc_data is None:
            logger.warning(f"[ATS] Unknown location from event: {loc_name!r}")
            return None
        return loc_data.code

    def _check_win_condition(self) -> bool:
        win_cond = self.slot_data.get("win_condition", WIN_LEVEL_AND_MONEY)
        goal_level = self.slot_data.get("goal_level", 35)
        goal_money = self.slot_data.get("goal_money", 1000) * 1000

        level_ok = self.current_level >= goal_level
        money_ok = self.current_money >= goal_money

        if win_cond == WIN_LEVEL_AND_MONEY:
            return level_ok and money_ok
        elif win_cond == WIN_LEVEL_ONLY:
            return level_ok
        elif win_cond == WIN_MONEY_ONLY:
            return money_ok
        elif win_cond == WIN_LEVEL_OR_MONEY:
            return level_ok or money_ok
        return False

    def _win_condition_description(self) -> str:
        wc = self.slot_data.get("win_condition", 0)
        lvl = self.slot_data.get("goal_level", 35)
        money = self.slot_data.get("goal_money", 1000) * 1000
        if wc == WIN_LEVEL_AND_MONEY:
            return f"Reach Level {lvl} AND ${money:,}"
        elif wc == WIN_LEVEL_ONLY:
            return f"Reach Level {lvl}"
        elif wc == WIN_MONEY_ONLY:
            return f"Earn ${money:,}"
        else:
            return f"Reach Level {lvl} OR ${money:,}"

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _city_id_from_item_name(item_name: str, prefix: str) -> Optional[str]:
        """Extract a city ID from an item name by cross-referencing the items table."""
        from worlds.american_truck_simulator.items import ALL_ITEMS
        data = ALL_ITEMS.get(item_name)
        if data:
            return data.game_id
        return None

    @staticmethod
    def _parse_money_bonus(item_name: str) -> Optional[int]:
        try:
            return int(item_name.split("$")[1].replace(",", ""))
        except (IndexError, ValueError):
            return None


# ── Main game watcher loop ─────────────────────────────────────────────────────

async def game_watcher(ctx: ATSContext) -> None:
    """Polls the plugin events file and manages game state."""
    logger.info("[ATS] Game watcher started. Waiting for plugin...")
    logger.info(f"[ATS] Communication folder: {COMM_DIR}")
    logger.info("[ATS] Make sure the ATS Archipelago plugin DLL is installed and ATS is running.")

    while not ctx.exit_event.is_set():
        try:
            ctx._process_events_file()
        except Exception:
            logger.error(f"[ATS] Error in game watcher:\n{traceback.format_exc()}")
        await asyncio.sleep(1.0)


# ── Entry point ────────────────────────────────────────────────────────────────

def launch():
    async def main(args):
        ctx = ATSContext(args.connect, args.password)
        ctx.server_task = asyncio.ensure_future(server_loop(ctx), loop=ctx.loop)
        ctx.watcher_task = asyncio.ensure_future(game_watcher(ctx), loop=ctx.loop)

        if gui_enabled:
            input_task = None
            from kvui import GameManager
            ctx.ui = GameManager(ctx)
            ctx.ui_task = asyncio.ensure_future(ctx.ui.async_run(), loop=ctx.loop)
        else:
            input_task = asyncio.ensure_future(
                ctx.ui_task if hasattr(ctx, "ui_task") else asyncio.sleep(0),
                loop=ctx.loop,
            )

        await ctx.exit_event.wait()
        ctx.server_task.cancel()
        ctx.watcher_task.cancel()
        await ctx.shutdown()

    parser = get_base_parser(description="American Truck Simulator Archipelago Client")
    args, _ = parser.parse_known_args()
    colorama.init()

    import logging
    logging.basicConfig(level=logging.INFO)

    asyncio.run(main(args))


# ── Utility ────────────────────────────────────────────────────────────────────

def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    launch()
