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
import re
import struct
import sys
import time
import traceback
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import colorama
from colorama import Fore, Style

# Archipelago imports — these work when run via the Archipelago launcher
import Utils
from CommonClient import CommonContext, server_loop, ClientCommandProcessor, logger, get_base_parser
from NetUtils import ClientStatus

# Force-register our world with AutoWorldRegister so CommonContext can look it up.
# In a PyInstaller bundle worlds.__path__ may point to a non-existent directory;
# we fix it to include sys._MEIPASS/worlds so Python can find our subpackage.
try:
    import worlds as _worlds_pkg
    if hasattr(sys, "_MEIPASS"):
        _bundle_worlds = os.path.join(sys._MEIPASS, "worlds")
        if _bundle_worlds not in _worlds_pkg.__path__:
            _worlds_pkg.__path__.insert(0, _bundle_worlds)
    import worlds.american_truck_simulator  # noqa: F401 — registers via AutoWorldRegister metaclass
except Exception as _e:
    print(f"[ATSClient] Warning: could not pre-register ATS world: {_e}")

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

# ── Save file parsing ─────────────────────────────────────────────────────────

_BSII_MAGIC = b"BSII"
_SIIN_MAGIC = b"SiiN"

# AES-256 key used by SCS in BSII v3 saves (sourced from the open-source
# SII_Decrypt community tool by Zukf / Xpericode).
_BSII_AES_KEY = bytes([
    0x2a, 0x5d, 0x6e, 0x3f, 0x8a, 0x14, 0x2c, 0x0d,
    0x9b, 0x7f, 0x4e, 0x21, 0xc6, 0xa1, 0x8d, 0x35,
    0xb7, 0xe9, 0x4f, 0x2c, 0x0d, 0x1a, 0x6b, 0x8e,
    0x3c, 0x7f, 0x50, 0x29, 0xd4, 0xe1, 0x6a, 0x38,
])

# Cumulative XP required to reach each level (index = level number).
# Needs calibration against in-game observation — verify with /status once
# connected and playing. These match community-documented ATS XP tables.
_ATS_XP_THRESHOLDS: List[int] = [
    0,       # 0 (sentinel)
    0,       # 1
    500,     # 2
    1_200,   # 3
    2_100,   # 4
    3_200,   # 5
    4_500,   # 6
    6_000,   # 7
    7_700,   # 8
    9_600,   # 9
    11_700,  # 10
    14_000,  # 11
    16_500,  # 12
    19_200,  # 13
    22_100,  # 14
    25_200,  # 15
    28_500,  # 16
    32_000,  # 17
    35_700,  # 18
    39_600,  # 19
    43_700,  # 20
    48_000,  # 21
    52_500,  # 22
    57_200,  # 23
    62_100,  # 24
    67_200,  # 25
    72_500,  # 26
    78_000,  # 27
    83_700,  # 28
    89_600,  # 29
    95_700,  # 30
    102_000, # 31
    108_500, # 32
    115_200, # 33
    122_100, # 34
    129_200, # 35
    136_500, # 36
    144_000, # 37
    151_700, # 38
    159_600, # 39
    167_700, # 40
]


def _xp_to_level(xp: int) -> int:
    """Convert ATS experience_points value to player level."""
    for lvl in range(len(_ATS_XP_THRESHOLDS) - 1, 0, -1):
        if xp >= _ATS_XP_THRESHOLDS[lvl]:
            return lvl
    return 1


def _find_ats_save_file() -> Optional[Path]:
    """Return the most-recently-modified game.sii across all ATS profiles/slots."""
    docs = Path(os.environ.get("USERPROFILE", Path.home())) / "Documents" / "American Truck Simulator"
    profiles_dir = docs / "profiles"
    if not profiles_dir.exists():
        return None
    best: Optional[Path] = None
    best_mtime = 0.0
    for profile in profiles_dir.iterdir():
        if not profile.is_dir():
            continue
        save_dir = profile / "save"
        if not save_dir.exists():
            continue
        for slot in save_dir.iterdir():
            if not slot.is_dir():
                continue
            game_sii = slot / "game.sii"
            if game_sii.exists():
                try:
                    mtime = game_sii.stat().st_mtime
                    if mtime > best_mtime:
                        best_mtime = mtime
                        best = game_sii
                except OSError:
                    pass
    return best


def _decrypt_bsii_v3(payload: bytes) -> Optional[bytes]:
    """AES-256-ECB decrypt a BSII v3 payload, then zlib-decompress it."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        cipher = Cipher(algorithms.AES(_BSII_AES_KEY), modes.ECB(), backend=default_backend())
        dec = cipher.decryptor()
        decrypted = dec.update(payload) + dec.finalize()
        return zlib.decompress(decrypted)
    except Exception:
        return None


def _read_sii_text(path: Path) -> Optional[str]:
    """Read a .sii save file and return plaintext SiiNunit content, or None on failure."""
    try:
        data = path.read_bytes()
    except OSError:
        return None

    if len(data) < 8:
        return None

    magic = data[:4]

    if magic == _SIIN_MAGIC:
        # Already plaintext
        return data.decode("utf-8", errors="replace")

    if magic != _BSII_MAGIC:
        return None

    version = struct.unpack_from("<I", data, 4)[0]
    payload = data[8:]

    if version == 2:
        # Raw zlib deflate
        try:
            return zlib.decompress(payload).decode("utf-8", errors="replace")
        except zlib.error:
            return None

    if version == 3:
        # AES-256-ECB then zlib. Some files have a 4-byte plaintext-size
        # prefix before the encrypted data; try both layouts.
        for skip in (0, 4):
            result = _decrypt_bsii_v3(payload[skip:])
            if result and result[:4] == _SIIN_MAGIC:
                return result.decode("utf-8", errors="replace")
        return None

    return None


def _parse_sii_save(text: str) -> Dict[str, Any]:
    """
    Extract gameplay fields from SiiNunit save text.

    Returns a dict with keys:
        experience_points (int)
        money            (int)
        visited_cities   (set[str])  — city IDs e.g. {"bakersfield", "fresno"}
        owned_garages    (set[str])  — city IDs whose garage has status 2
    """
    result: Dict[str, Any] = {
        "experience_points": 0,
        "money": 0,
        "visited_cities": set(),
        "owned_garages": set(),
    }

    xp_m = re.search(r"\bexperience_points\s*:\s*(\d+)", text)
    if xp_m:
        result["experience_points"] = int(xp_m.group(1))

    money_m = re.search(r"\bmoney_account\s*:\s*(-?\d+)", text)
    if money_m:
        result["money"] = int(money_m.group(1))

    # visited_city[N]: city.<city_id>
    for m in re.finditer(r"\bvisited_city\[\d+\]\s*:\s*city\.(\w+)", text):
        result["visited_cities"].add(m.group(1))

    # garage : garage.<city_id> { ... status: 2 ... }
    for block_m in re.finditer(
        r"garage\s*:\s*garage\.(\w+)\s*\{([^}]*)\}", text, re.DOTALL
    ):
        city_id = block_m.group(1)
        body = block_m.group(2)
        status_m = re.search(r"\bstatus\s*:\s*(\d+)", body)
        if status_m and int(status_m.group(1)) == 2:
            result["owned_garages"].add(city_id)

    return result


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

    def __init__(self, server_address: str, password: Optional[str],
                 auto_launch_game: bool = True) -> None:
        super().__init__(server_address, password)

        self.auto_launch_game: bool = auto_launch_game

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

        # Track how many items we have applied so we can skip them on reconnect/resync
        self._applied_item_count: int = 0

        # Notification queue for in-game popups (written to items.json)
        self._notifications: List[Dict] = []
        self._notification_counter: int = 0

        # Save file polling state
        self._save_last_mtime: float = 0.0
        self._save_known_cities: Set[str] = set()
        self._save_known_garages: Set[str] = set()
        self._save_warned_unreadable: bool = False

    # ── Archipelago callbacks ──────────────────────────────────────────────────

    async def server_auth(self, password_requested: bool = False) -> None:
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    async def on_package(self, cmd: str, args: Dict) -> None:
        logger.debug(f"[ATS] Packet in: {cmd}")
        # Await super() correctly regardless of whether this AP version uses
        # async or sync on_package — unawaited coroutine = items_received never filled.
        result = super().on_package(cmd, args)
        if asyncio.iscoroutine(result):
            await result
        if cmd == "Connected":
            self.slot_data = args.get("slot_data", {})
            self._on_connected()
        # ReceivedItems is intentionally NOT handled here.
        # args["items"] contains raw JSON lists, not NetworkItem objects.
        # The base class converts them and appends to ctx.items_received;
        # game_watcher polls that list and calls _on_items_received with
        # proper NetworkItem objects.

    def _on_connected(self) -> None:
        logger.info(f"[ATS] Connected to Archipelago server as {self.username}")
        logger.info(f"[ATS] Win condition: {self._win_condition_description()}")
        # Write slot data so the plugin/mod can read player options
        _write_json(SLOT_DATA_FILE, self.slot_data)
        self._write_items_file()

    def _on_items_received(self, start_index: int, items) -> None:
        applied_any = False
        for i, item in enumerate(items):
            global_index = start_index + i
            if global_index < self._applied_item_count:
                continue  # already applied on a previous receive/resync
            try:
                item_name = self.item_names.lookup_in_game(item.item)
            except Exception:
                item_name = str(item.item)
                logger.warning(f"[ATS] Could not look up item name for id {item.item}")
            try:
                sender_name = self.player_names.get(item.player, f"Player {item.player}")
            except Exception:
                sender_name = "Unknown"
            if item.player == self.slot:
                display_text = f"You found: {item_name}"
            else:
                display_text = f"{item_name} (from {sender_name})"
            logger.info(f"[ATS] Received item #{global_index}: {display_text}")
            try:
                self._apply_item(item_name)
            except Exception:
                logger.error(f"[ATS] Error applying item {item_name!r}:\n{traceback.format_exc()}")
            # Queue in-game notification popup (plugin reads item_notifications from items.json)
            self._notifications.append({
                "id": self._notification_counter,
                "item_name": item_name,
                "from_player": sender_name,
                "text": display_text,
            })
            self._notification_counter += 1
            if len(self._notifications) > 50:
                self._notifications = self._notifications[-50:]
            self._applied_item_count += 1
            applied_any = True
        if applied_any:
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
                # Truck model unlock — store game_id (e.g. "kenworth_w900") not display name
                from worlds.american_truck_simulator.items import ALL_ITEMS
                item_data = ALL_ITEMS.get(item_name)
                truck_game_id = item_data.game_id if item_data else item_name
                self._unlocked_trucks.add(truck_game_id)
                logger.info(f"[ATS] Unlocked truck: {rest} ({truck_game_id})")

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
            "item_notifications": self._notifications,
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
            logger.error(f"[ATS] Failed to read events file: {traceback.format_exc()}")
            return

        if not isinstance(data, dict):
            logger.warning("[ATS] events.json is not a dict — skipping")
            return

        self.plugin_connected = data.get("plugin_alive", False)
        self.current_level = data.get("current_level", self.current_level)
        self.current_money = data.get("current_money", self.current_money)

        new_checks: List[int] = []

        for event in data.get("events", []):
            event_id = event.get("id")
            if event_id in self._processed_event_ids:
                continue  # silently skip already-processed events
            logger.debug(f"[ATS] New event: {event_id}")
            self._processed_event_ids.add(event_id)

            try:
                location_id = self._resolve_event_to_location_id(event)
            except Exception:
                logger.error(f"[ATS] Error resolving event {event_id}:\n{traceback.format_exc()}")
                continue

            if location_id is None:
                continue
            if location_id in self.checked_locations:
                logger.info(f"[ATS] Location already checked: {event_id}")
                continue
            logger.info(f"[ATS] Queuing check for location id {location_id} ({event_id})")
            new_checks.append(location_id)

        if new_checks:
            logger.info(f"[ATS] Sending {len(new_checks)} location check(s) to server.")
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

    def _poll_save_file(self) -> None:
        """
        Read the most recent ATS game.sii, extract level/money/cities/garages,
        update client state, and queue any newly satisfied location checks.

        Only runs when connected to an AP server (needs checked_locations).
        """
        if not self.auth:
            return  # not connected yet

        save_path = _find_ats_save_file()
        if not save_path:
            return

        try:
            mtime = save_path.stat().st_mtime
        except OSError:
            return

        if mtime <= self._save_last_mtime:
            return
        self._save_last_mtime = mtime

        text = _read_sii_text(save_path)
        if text is None:
            if not self._save_warned_unreadable:
                self._save_warned_unreadable = True
                logger.warning(
                    "[ATS] Could not read save file — it may use AES encryption "
                    "(BSII v3) with an unrecognised key. Level milestones, city "
                    "arrivals, and garage upgrades will not fire until this is resolved."
                )
            return
        self._save_warned_unreadable = False

        save = _parse_sii_save(text)

        # Update live game state read by _check_win_condition
        level = _xp_to_level(save["experience_points"])
        self.current_level = level
        self.current_money = save["money"]

        from worlds.american_truck_simulator.locations import (
            ALL_LOCATIONS, CITY_ARRIVAL_LOCATIONS, GARAGE_UPGRADE_LOCATIONS,
        )
        new_checks: List[int] = []

        # Level milestone checks (re-evaluate all milestones each poll)
        for loc_name, loc_data in ALL_LOCATIONS.items():
            if loc_data.category == "level":
                milestone = int(loc_data.game_id.split("_")[1])
                if level >= milestone and loc_data.code not in self.checked_locations:
                    new_checks.append(loc_data.code)

        # City first arrival checks
        new_cities = save["visited_cities"] - self._save_known_cities
        for city_id in new_cities:
            self._save_known_cities.add(city_id)
            for loc_data in CITY_ARRIVAL_LOCATIONS.values():
                if loc_data.game_id == city_id and loc_data.code not in self.checked_locations:
                    new_checks.append(loc_data.code)
                    break

        # Garage upgrade checks (status == 2 means player-owned)
        new_garages = save["owned_garages"] - self._save_known_garages
        for city_id in new_garages:
            self._save_known_garages.add(city_id)
            for loc_data in GARAGE_UPGRADE_LOCATIONS.values():
                if loc_data.game_id == city_id and loc_data.code not in self.checked_locations:
                    new_checks.append(loc_data.code)
                    break

        if new_checks:
            logger.info(f"[ATS] Save poll: {len(new_checks)} new location check(s) from save file.")
            asyncio.create_task(self.send_msgs([{
                "cmd": "LocationChecks",
                "locations": new_checks,
            }]))

        # Win condition (level/money both come from save file)
        if not self.goal_complete and self._check_win_condition():
            self.goal_complete = True
            asyncio.create_task(self.send_msgs([{
                "cmd": "StatusUpdate",
                "status": ClientStatus.CLIENT_GOAL,
            }]))
            logger.info("[ATS] Goal complete! Congratulations!")

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


# ── Steam launcher ─────────────────────────────────────────────────────────────

ATS_STEAM_APP_ID = "270880"


def _launch_ats_steam() -> None:
    """Open American Truck Simulator via the Steam URI protocol."""
    import webbrowser
    try:
        webbrowser.open(f"steam://rungameid/{ATS_STEAM_APP_ID}")
        logger.info("[ATS] Sent launch request to Steam for American Truck Simulator.")
    except Exception as exc:
        logger.warning(f"[ATS] Could not send Steam launch request: {exc}")


# ── Main game watcher loop ─────────────────────────────────────────────────────

async def game_watcher(ctx: ATSContext) -> None:
    """Polls the plugin events file and ATS save file, managing game state."""
    logger.info("[ATS] Game watcher started.")
    logger.info(f"[ATS] Communication folder: {COMM_DIR}")

    if ctx.auto_launch_game:
        _launch_ats_steam()

    logger.info("[ATS] Waiting for ATS plugin to connect...")

    _SAVE_POLL_INTERVAL = 5.0  # seconds between save file reads
    _last_save_poll = 0.0

    while not ctx.exit_event.is_set():
        try:
            ctx._process_events_file()
        except Exception:
            logger.error(f"[ATS] Error processing events file:\n{traceback.format_exc()}")

        # Apply any items the server has sent.
        # The base class populates ctx.items_received with proper NetworkItem
        # objects once on_package correctly awaits super().on_package().
        received = list(getattr(ctx, "items_received", []))
        if len(received) > ctx._applied_item_count:
            logger.debug(f"[ATS] Applying {len(received) - ctx._applied_item_count} new item(s) "
                         f"(have {len(received)}, applied {ctx._applied_item_count})")
            ctx._on_items_received(ctx._applied_item_count, received[ctx._applied_item_count:])

        now = time.monotonic()
        if now - _last_save_poll >= _SAVE_POLL_INTERVAL:
            _last_save_poll = now
            try:
                ctx._poll_save_file()
            except Exception:
                logger.error(f"[ATS] Error polling save file:\n{traceback.format_exc()}")

        await asyncio.sleep(1.0)


# ── Entry point ────────────────────────────────────────────────────────────────

def launch():
    import logging
    logging.basicConfig(level=logging.DEBUG)

    parser = get_base_parser(description="American Truck Simulator Archipelago Client")
    parser.add_argument(
        "--no-launch",
        action="store_true",
        default=False,
        help="Do not automatically launch American Truck Simulator via Steam.",
    )
    args, _ = parser.parse_known_args()
    colorama.init()

    async def main():
        ctx = ATSContext(
            args.connect,
            args.password,
            auto_launch_game=not args.no_launch,
        )
        ctx.server_task = asyncio.ensure_future(server_loop(ctx))
        ctx.watcher_task = asyncio.ensure_future(game_watcher(ctx))

        try:
            from Utils import gui_enabled
        except ImportError:
            gui_enabled = False

        _gui_log = os.path.join(os.path.dirname(sys.executable), "ATSClient_gui_debug.log")
        if gui_enabled:
            try:
                with open(_gui_log, "w", encoding="utf-8") as _f:
                    _f.write(f"gui_enabled={gui_enabled}\n")
                    _f.write(f"sys.stdout={sys.stdout!r}\n")
                    _f.write(f"sys.stderr={sys.stderr!r}\n")
                    _f.write("calling run_gui()...\n")
                ctx.run_gui()
                with open(_gui_log, "a", encoding="utf-8") as _f:
                    _f.write("run_gui() returned normally\n")
            except Exception as exc:
                with open(_gui_log, "a", encoding="utf-8") as _f:
                    _f.write(f"run_gui() raised: {exc!r}\n")
                    _f.write(traceback.format_exc())
                logger.warning(f"[ATS] GUI failed to start ({exc!r}), running in CLI mode.")
        else:
            with open(_gui_log, "w", encoding="utf-8") as _f:
                _f.write(f"gui_enabled={gui_enabled} — skipping GUI\n")
                _f.write(f"sys.stdout={sys.stdout!r}\n")
        ctx.run_cli()

        await ctx.exit_event.wait()
        ctx.server_task.cancel()
        ctx.watcher_task.cancel()
        await ctx.shutdown()

    asyncio.run(main())


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
