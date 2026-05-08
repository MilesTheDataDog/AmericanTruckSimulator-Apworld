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

# ── Save-grant persistence ────────────────────────────────────────────────────
# Tracks how much XP / money has already been written into the save file so we
# never double-apply across sessions.  Stored in grants.json next to items.json.

GRANTS_FILE = COMM_DIR / "grants.json"


def _load_save_grants() -> "tuple[int, int]":
    """Return (applied_xp, applied_money) from the last session, or (0, 0)."""
    try:
        if GRANTS_FILE.exists():
            data = _read_json(GRANTS_FILE)
            return int(data.get("applied_xp", 0)), int(data.get("applied_money", 0))
    except Exception:
        pass
    return 0, 0


def _persist_save_grants(applied_xp: int, applied_money: int) -> None:
    try:
        _write_json(GRANTS_FILE, {"applied_xp": applied_xp, "applied_money": applied_money})
    except Exception as e:
        logger.error(f"[ATS] Could not write grants.json: {e}")


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


_ATS_STEAM_APP_ID_STR = "270880"


def _find_steam_path() -> Optional[Path]:
    """Return the Steam installation directory by reading the Windows Registry."""
    try:
        import winreg
        for hive, flag in [
            (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_32KEY),
            (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
            (winreg.HKEY_CURRENT_USER,  winreg.KEY_READ),
        ]:
            try:
                key = winreg.OpenKey(hive, r"SOFTWARE\Valve\Steam", 0, flag)
                val, _ = winreg.QueryValueEx(key, "InstallPath")
                winreg.CloseKey(key)
                p = Path(val)
                if p.is_dir():
                    return p
            except OSError:
                pass
    except ImportError:
        pass
    return None


def _steam_userdata_roots() -> List[Path]:
    """Return every <SteamPath>/userdata/<uid>/270880/remote/ directory found."""
    roots: List[Path] = []
    steam = _find_steam_path()
    if not steam:
        return roots
    userdata = steam / "userdata"
    if not userdata.is_dir():
        return roots
    for uid_dir in userdata.iterdir():
        if not uid_dir.is_dir():
            continue
        remote = uid_dir / _ATS_STEAM_APP_ID_STR / "remote"
        if remote.is_dir():
            roots.append(remote)
    return roots


def _scan_profiles_dir(profiles_dir: Path, best: Optional[Path], best_mtime: float):
    """Walk a profiles root and return (best_path, best_mtime)."""
    if not profiles_dir.is_dir():
        return best, best_mtime
    for profile in profiles_dir.iterdir():
        if not profile.is_dir():
            continue
        save_dir = profile / "save"
        if not save_dir.is_dir():
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
    return best, best_mtime


def _find_ats_save_file() -> Optional[Path]:
    """Return the most-recently-modified game.sii across all ATS profiles/slots.

    Searches (in order of preference):
      1. Steam userdata directory (Steam Cloud saves):
           <SteamPath>/userdata/<uid>/270880/remote/steam/profiles/
      2. Documents (local / non-Cloud saves):
           <Documents>/American Truck Simulator/profiles/
           <Documents>/American Truck Simulator/steam/profiles/
    """
    docs = Path(os.environ.get("USERPROFILE", Path.home())) / "Documents" / "American Truck Simulator"

    best: Optional[Path] = None
    best_mtime = 0.0

    # 1. Steam userdata (covers Steam Cloud / PC_steam_cloud profile type)
    for remote in _steam_userdata_roots():
        best, best_mtime = _scan_profiles_dir(remote / "steam" / "profiles", best, best_mtime)
        # Some older setups store directly under remote/profiles
        best, best_mtime = _scan_profiles_dir(remote / "profiles", best, best_mtime)

    # 2. Documents fallback (local saves, non-Cloud)
    best, best_mtime = _scan_profiles_dir(docs / "profiles", best, best_mtime)
    best, best_mtime = _scan_profiles_dir(docs / "steam" / "profiles", best, best_mtime)

    return best


def _cryptography_available() -> bool:
    """Return True if the cryptography package can be imported."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher  # noqa: F401
        return True
    except ImportError:
        return False


def _decrypt_bsii_v3(payload: bytes) -> Optional[bytes]:
    """AES-256-ECB decrypt a BSII v3 payload, then zlib-decompress it."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        cipher = Cipher(algorithms.AES(_BSII_AES_KEY), modes.ECB(), backend=default_backend())
        dec = cipher.decryptor()
        decrypted = dec.update(payload) + dec.finalize()
        # Use decompressobj so trailing zero-padding bytes (AES block alignment)
        # are silently stored in unused_data instead of raising zlib.error.
        d = zlib.decompressobj()
        return d.decompress(decrypted)
    except ImportError:
        return None  # cryptography not available — caller handles this
    except Exception as exc:
        logger.debug(f"[ATS] BSII v3 decrypt attempt failed: {exc}")
        return None


def _write_sii_plain(path: Path, text: str) -> bool:
    """Write plaintext SiiNunit text as a plain-text save (g_save_format 2)."""
    try:
        raw = text.encode("utf-8")
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        tmp.replace(path)
        return True
    except Exception as e:
        logger.error(f"[ATS] _write_sii_plain failed for {path}: {e}")
        return False


def _write_sii_encrypted(path: Path, text: str) -> bool:
    """Encode plaintext SiiNunit text as a BSII v3 (AES-256-ECB + zlib) save file."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend

        raw = text.encode("utf-8")
        compressed = zlib.compress(raw, level=9)

        rem = len(compressed) % 16
        if rem:
            compressed += b"\x00" * (16 - rem)

        cipher = Cipher(algorithms.AES(_BSII_AES_KEY), modes.ECB(), backend=default_backend())
        enc = cipher.encryptor()
        encrypted = enc.update(compressed) + enc.finalize()

        header = b"BSII" + struct.pack("<I", 3) + struct.pack("<I", len(raw))
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(header + encrypted)
        tmp.replace(path)
        return True
    except Exception as e:
        logger.error(f"[ATS] _write_sii_encrypted failed for {path}: {e}")
        return False


def _write_sii_save(path: Path, text: str, plain: bool = False) -> bool:
    """Write SII save — plain text if plain=True, encrypted BSII v3 otherwise."""
    if plain:
        return _write_sii_plain(path, text)
    return _write_sii_encrypted(path, text)


def _read_sii_text(path: Path) -> "tuple[Optional[str], str]":
    """Read a .sii save file.

    Returns (text, format_tag) where format_tag is one of:
      'plain'    — SiiN plain text (success)
      'bsii_v2'  — BSII v2 zlib (success or failure noted in text=None)
      'bsii_v3'  — BSII v3 AES+zlib (success or failure noted in text=None)
      'no_crypto' — BSII v3 but cryptography package missing
      'unknown'  — unrecognised magic
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None, "unreadable"

    if len(data) < 8:
        return None, "too_small"

    magic = data[:4]

    if magic == _SIIN_MAGIC:
        return data.decode("utf-8", errors="replace"), "plain"

    if magic != _BSII_MAGIC:
        return None, f"unknown_magic_{magic!r}"

    version = struct.unpack_from("<I", data, 4)[0]
    payload = data[8:]

    if version == 2:
        try:
            return zlib.decompress(payload).decode("utf-8", errors="replace"), "bsii_v2"
        except zlib.error:
            return None, "bsii_v2"

    if version == 3:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher  # noqa: F401
        except ImportError:
            return None, "no_crypto"

        # Try both payload layouts: with or without a 4-byte uncompressed-size prefix.
        for skip in (0, 4):
            result = _decrypt_bsii_v3(payload[skip:])
            if result and result[:4] == _SIIN_MAGIC:
                return result.decode("utf-8", errors="replace"), "bsii_v3"
        return None, "bsii_v3"

    return None, f"bsii_v{version}"


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

    # Pin to the economy.economy block so we never accidentally read a hired
    # driver's experience_points, which appears as the same field name.
    _econ_m = re.search(r'\beconomy\s*:\s*economy\.\w+\s*\{', text)
    _xp_region = text[_econ_m.end():_econ_m.end() + 20_000] if _econ_m else text
    xp_m = re.search(r"\bexperience_points\s*:\s*(\d+)", _xp_region)
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


try:
    from kvui import GameManager as _GameManager

    class ATSManager(_GameManager):
        base_title = "American Truck Simulator Client"
        title = f"American Truck Simulator Client {Utils.__version__}"

except ImportError:
    ATSManager = None  # type: ignore[assignment,misc]


class ATSContext(CommonContext):
    command_processor = ATSCommandProcessor
    game = GAME_NAME
    items_handling = 0b111  # receive all items
    if ATSManager is not None:
        game_manager_class = ATSManager

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
        self._events_err_count: int = 0  # consecutive events.json read failures

        # Game state tracked by client
        self.current_level: int = 0
        self.current_money: int = 0
        self.goal_complete: bool = False

        # Items received from server (sent to plugin)
        self._unlocked_trucks: Set[str] = set()
        self._unlocked_upgrade_tiers: Dict[str, int] = {}
        self._total_money_granted: int = 0
        self._total_xp_granted: int = 0

        # Track how many items we have applied so we can skip them on reconnect/resync
        self._applied_item_count: int = 0

        # Notification queue for in-game popups (written to items.json)
        self._notifications: List[Dict] = []
        self._notification_counter: int = 0

        # Save file polling state
        self._save_last_mtime: float = 0.0
        self._save_known_cities: Set[str] = set()
        self._save_known_garages: Set[str] = set()
        self._save_known_states: Set[str] = set()
        self._save_warned_unreadable: bool = False
        self._fresh_save_checked: bool = False
        self._save_path_logged: bool = False
        self.current_xp: int = 0
        self._save_is_plain: bool = False  # True when save uses SiiN (g_save_format 2)
        self._save_not_found_warned: bool = False

        # Save-grant tracking: how much XP / money has been baked into the save
        # file already (persisted across sessions in grants.json).
        self._save_applied_xp: int
        self._save_applied_money: int
        self._save_applied_xp, self._save_applied_money = _load_save_grants()

        # Incremented each time we write a patched save; DLL watches this value
        # and fires F9 (quick-load) when it changes.
        self._reload_counter: int = 0

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
            # Truck model unlock — store game_id (e.g. "kenworth_w900") not display name
            from worlds.american_truck_simulator.items import ALL_ITEMS
            item_data = ALL_ITEMS.get(item_name)
            truck_game_id = item_data.game_id if item_data else item_name
            self._unlocked_trucks.add(truck_game_id)
            logger.info(f"[ATS] Unlocked truck: {item_name} ({truck_game_id})")

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

        elif item_name.endswith("Money Grant"):
            from worlds.american_truck_simulator.items import ALL_ITEMS
            item_data = ALL_ITEMS.get(item_name)
            if item_data:
                # game_id is "money_10000", "money_50000", or "money_150000"
                amount = int(item_data.game_id.split("_")[1])
                self._total_money_granted += amount
                logger.info(f"[ATS] Money grant: +${amount:,} (total granted: ${self._total_money_granted:,})")

        elif item_name.endswith("XP Grant"):
            from worlds.american_truck_simulator.items import ALL_ITEMS
            item_data = ALL_ITEMS.get(item_name)
            if item_data:
                # game_id is "xp_2000", "xp_10000", or "xp_50000"
                amount = int(item_data.game_id.split("_")[1])
                self._total_xp_granted += amount
                logger.info(f"[ATS] XP grant: +{amount:,} XP (total granted: {self._total_xp_granted:,})")


    # ── Items file (client → plugin) ───────────────────────────────────────────

    def _write_items_file(self) -> None:
        """Write the current unlocked-items state for the plugin/mod to read."""
        payload = {
            "version": 1,
            "timestamp": time.time(),
            "unlocked_trucks": sorted(self._unlocked_trucks),
            "upgrade_tiers": self._unlocked_upgrade_tiers,
            "total_money_granted": self._total_money_granted,
            "total_xp_granted": self._total_xp_granted,
            "win_condition": self.slot_data.get("win_condition", 0),
            "goal_level": self.slot_data.get("goal_level", 35),
            "goal_money_thousands": self.slot_data.get("goal_money", 1000),
            "shuffle_trucks": self.slot_data.get("shuffle_trucks", True),
            "shuffle_truck_upgrades": self.slot_data.get("shuffle_truck_upgrades", False),
            "item_notifications": self._notifications,
            # Incremented each time we patch the save; DLL fires F9 on change.
            "reload_counter": self._reload_counter,
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

        # Update mtime only on success so transient PermissionErrors are retried.
        try:
            data = _read_json(EVENTS_FILE)
            self._last_events_mtime = mtime
            if self._events_err_count > 0:
                logger.info("[ATS] events.json read recovered after lock.")
            self._events_err_count = 0
        except PermissionError:
            self._events_err_count += 1
            if self._events_err_count == 1:
                logger.warning(
                    "[ATS] events.json is momentarily locked by the plugin DLL "
                    "(rename race) — will retry next poll. This is normal if rare."
                )
            return
        except Exception:
            self._events_err_count += 1
            logger.error(f"[ATS] Failed to read events file: {traceback.format_exc()}")
            self._last_events_mtime = mtime  # don't retry persistent errors
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

    def _resolve_event_to_location_id(self, event: Dict) -> Optional[int]:
        """Map a plugin event to an Archipelago location ID."""
        from worlds.american_truck_simulator.locations import ALL_LOCATIONS, CARGO_DELIVERY_LOCATIONS

        etype = event.get("type")
        game_id = event.get("game_id", "")

        if etype == "cargo_delivered":
            # Match by cargo game_id (stable internal ID) rather than display name,
            # which can differ between game versions and localizations.
            for loc_data in CARGO_DELIVERY_LOCATIONS.values():
                if loc_data.game_id == game_id:
                    return loc_data.code
            logger.warning(
                f"[ATS] No cargo location for id={game_id!r} "
                f"(cargo_name={event.get('cargo_name', '')!r}) — "
                f"this cargo may not be included in the randomizer for this seed."
            )
            return None

        if etype == "city_arrived":
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
            if not self._save_not_found_warned:
                self._save_not_found_warned = True
                _docs = Path(os.environ.get("USERPROFILE", Path.home())) / "Documents" / "American Truck Simulator"
                _steam_roots = _steam_userdata_roots()
                _searched = [
                    f"  {_docs / 'profiles'}",
                    f"  {_docs / 'steam' / 'profiles'}",
                ]
                for _r in _steam_roots:
                    _searched.append(f"  {_r / 'steam' / 'profiles'} (Steam Cloud)")
                if not _steam_roots:
                    _searched.append("  (Steam install not found in registry)")
                logger.warning(
                    "[ATS] No ATS save file (game.sii) found. Searched:\n"
                    + "\n".join(_searched) + "\n"
                    "XP/money grants cannot be applied until a save file is found. "
                    "Make sure ATS has been saved at least once."
                )
            return
        if not self._save_path_logged:
            self._save_not_found_warned = False  # reset in case it recovers
            self._save_path_logged = True
            logger.info(f"[ATS] Found save file: {save_path}")
        logger.debug(f"[ATS] Watching save file: {save_path}")

        try:
            mtime = save_path.stat().st_mtime
        except OSError:
            return

        # If there are pending grants we need to apply, bypass the mtime guard
        # so we don't wait for the next game autosave to bake them in.
        pending_xp    = self._total_xp_granted    - self._save_applied_xp
        pending_money = self._total_money_granted - self._save_applied_money
        has_pending   = pending_xp > 0 or pending_money > 0

        if not has_pending and mtime <= self._save_last_mtime:
            return
        self._save_last_mtime = mtime

        text, fmt = _read_sii_text(save_path)
        self._save_is_plain = (fmt == "plain")

        if text is None:
            if not self._save_warned_unreadable:
                self._save_warned_unreadable = True
                # Build config.cfg path hints for the user message.
                _cfg_paths = []
                for _remote in _steam_userdata_roots():
                    try:
                        save_path.relative_to(_remote)
                        _cfg_paths.append(str(_remote / "config.cfg"))
                        break
                    except ValueError:
                        pass
                _docs_cfg = (
                    Path(os.environ.get("USERPROFILE", Path.home()))
                    / "Documents" / "American Truck Simulator" / "config.cfg"
                )
                _cfg_paths.append(str(_docs_cfg))
                _cfg_hint = "\n       ".join(_cfg_paths)

                if fmt == "no_crypto":
                    # The startup executor should have installed cryptography already.
                    # If we still get here it means the install failed — tell the user.
                    logger.warning(
                        "[ATS] Save is BSII v3 encrypted and 'cryptography' could not be "
                        "auto-installed in this Python environment.\n"
                        "Switch ATS to plain-text saves instead:\n"
                        f"  1. Open config.cfg:\n"
                        f"       {_cfg_hint}\n"
                        "  2. Add this line:  uset g_save_format \"2\"\n"
                        "  3. In ATS: complete any delivery (autosave) OR use pause → Save\n"
                        "     NOTE: 'Current profile saved' in the game log does NOT update\n"
                        "     the autosave file — you must actually save via the game menu.\n"
                        "  4. Restart the client."
                    )
                elif fmt == "bsii_v3":
                    logger.warning(
                        "[ATS] Save is BSII v3 encrypted but decryption failed.\n"
                        "Most likely cause: the autosave on disk is from BEFORE you added\n"
                        "'g_save_format 2' to config.cfg.  To create a fresh plain-text save:\n"
                        f"  1. Config.cfg location:\n"
                        f"       {_cfg_hint}\n"
                        "  2. Confirm this line is present:  uset g_save_format \"2\"\n"
                        "  3. In ATS: complete any delivery (autosave) OR pause → Save\n"
                        "     NOTE: 'Current profile saved' in the game log is profile metadata,\n"
                        "     NOT the autosave file — you must trigger a real save.\n"
                        "  4. Restart the client."
                    )
                else:
                    logger.warning(
                        f"[ATS] Could not read save file (format tag: {fmt}).\n"
                        "This is unexpected — please report this error."
                    )
            return
        self._save_warned_unreadable = False

        save = _parse_sii_save(text)
        log_fn = logger.info if has_pending else logger.debug
        log_fn(f"[ATS] Save parsed: xp={save['experience_points']:,}, "
               f"money=${save['money']:,}, cities={len(save['visited_cities'])}"
               + (f" — pending grants: +{pending_xp:,} XP, +${pending_money:,}" if has_pending else ""))

        # Sanity check: if save XP is below what we've tracked as applied, the
        # save was replaced (new profile, deleted profile, manual save swap, etc.).
        # XP never decreases in ATS, so this reliably detects a stale grants.json.
        if save["experience_points"] < self._save_applied_xp:
            logger.warning(
                f"[ATS] Save XP ({save['experience_points']:,}) < applied grants "
                f"({self._save_applied_xp:,}) — save was likely replaced. "
                "Resetting grant tracking so grants are re-applied."
            )
            self._save_applied_xp = 0
            self._save_applied_money = 0
            _persist_save_grants(0, 0)

        # One-time fresh-save check. Only warn when the server has no checked
        # locations yet — if it does, the player is resuming a legitimate run.
        if not self._fresh_save_checked:
            self._fresh_save_checked = True
            if not self.checked_locations:
                level_at_check = _xp_to_level(save["experience_points"])
                if level_at_check > 1:
                    logger.warning(
                        "[ATS] WARNING: Your save file does not appear to be from a fresh "
                        f"profile (current level: {level_at_check}). For a proper Archipelago "
                        "run please start a new profile in American Truck Simulator."
                    )

        # Update live game state read by _check_win_condition
        level = _xp_to_level(save["experience_points"])
        self.current_level  = level
        self.current_money  = save["money"]
        self.current_xp     = save["experience_points"]

        # ── Apply pending XP / money grants to the save file ──────────────────
        # total_*_granted = cumulative amount AP has sent this session.
        # _save_applied_*  = cumulative amount already written into the save.
        # The delta is what still needs to be added.
        xp_delta    = self._total_xp_granted    - self._save_applied_xp
        money_delta = self._total_money_granted - self._save_applied_money

        if (xp_delta > 0 or money_delta > 0) and text is not None:
            new_xp    = self.current_xp    + xp_delta
            new_money = self.current_money + money_delta

            # Patch the decrypted text, pinned to the economy.economy block so we
            # never accidentally overwrite a hired driver's experience_points field.
            modified = text
            if xp_delta > 0:
                _econ_patch = re.search(r'\beconomy\s*:\s*economy\.\w+\s*\{', modified)
                if _econ_patch:
                    _before = modified[:_econ_patch.end()]
                    _after  = modified[_econ_patch.end():]
                    _after  = re.sub(
                        r'\bexperience_points\s*:\s*\d+',
                        f'experience_points: {new_xp}',
                        _after, count=1,
                    )
                    modified = _before + _after
                    logger.debug(f"[ATS] Patched economy.economy XP -> {new_xp:,}")
                else:
                    logger.warning("[ATS] economy.economy block not found; patching first occurrence of experience_points")
                    modified = re.sub(
                        r'\bexperience_points\s*:\s*\d+',
                        f'experience_points: {new_xp}',
                        modified, count=1,
                    )
            if money_delta > 0:
                modified = re.sub(
                    r'\bmoney_account\s*:\s*-?\d+',
                    f'money_account: {new_money}',
                    modified, count=1,
                )

            plain = self._save_is_plain
            logger.info(f"[ATS] Writing grants — format={'plain-text' if plain else 'BSII-v3-encrypted'}")

            # Write to the quicksave slot first — F9 loads quicksave (slot 1), not autosave.
            # ATS stores the F5/F9 quicksave at save/1/game.sii, NOT save/quicksave/.
            quicksave_dir  = save_path.parent.parent / "1"
            quicksave_path = quicksave_dir / "game.sii"
            wrote_quicksave = False
            try:
                quicksave_dir.mkdir(parents=True, exist_ok=True)
                wrote_quicksave = _write_sii_save(quicksave_path, modified, plain=plain)
                # Verify: read the file back and confirm economy XP landed correctly.
                if wrote_quicksave:
                    try:
                        _vtext, _vplain = _read_sii_text(quicksave_path)
                        if _vtext:
                            _vecon = re.search(r'\beconomy\s*:\s*economy\.\w+\s*\{', _vtext)
                            if _vecon:
                                _vregion = _vtext[_vecon.end():_vecon.end() + 20_000]
                                _vxp = re.search(r'\bexperience_points\s*:\s*(\d+)', _vregion)
                                logger.info(
                                    f"[ATS] VERIFY quicksave economy XP = "
                                    f"{int(_vxp.group(1)):,} (expected {new_xp:,})"
                                    if _vxp else
                                    "[ATS] VERIFY quicksave: experience_points not found in economy block"
                                )
                            else:
                                logger.warning("[ATS] VERIFY quicksave: economy.economy block not found in file")
                    except Exception as _ve:
                        logger.warning(f"[ATS] VERIFY quicksave read-back failed: {_ve}")
            except Exception as e:
                logger.warning(f"[ATS] Could not write quicksave: {e}")

            # Also write back to the autosave slot so the next natural autosave
            # does not stomp the grants if the player saves before F9 fires.
            wrote_autosave = _write_sii_save(save_path, modified, plain=plain)

            if wrote_quicksave:
                # Only increment reload_counter when quicksave succeeded —
                # F9 loads the quicksave slot, so firing it without a valid
                # quicksave would reload the un-patched save.
                self._save_applied_xp    += xp_delta
                self._save_applied_money += money_delta
                _persist_save_grants(self._save_applied_xp, self._save_applied_money)

                self.current_xp    = new_xp
                self.current_money = new_money
                self._reload_counter += 1

                logger.info(
                    f"[ATS] Grants written to save: "
                    f"+{xp_delta:,} XP (total {new_xp:,}), "
                    f"+${money_delta:,} (total ${new_money:,}) — "
                    f"reload_counter={self._reload_counter} "
                    f"(autosave={'ok' if wrote_autosave else 'FAIL'}, "
                    f"quicksave=ok)"
                )
                self._write_items_file()   # sends updated reload_counter to DLL
            elif wrote_autosave:
                logger.warning(
                    "[ATS] Grants written to autosave only — quicksave write failed. "
                    "Grants will appear after the next time ATS loads that save slot "
                    "(sleep in-game or use Load Game). F9 quick-load will NOT be triggered."
                )
                # Still mark as applied so we don't try to re-apply on next poll.
                self._save_applied_xp    += xp_delta
                self._save_applied_money += money_delta
                _persist_save_grants(self._save_applied_xp, self._save_applied_money)
                self.current_xp    = new_xp
                self.current_money = new_money
            else:
                logger.error(
                    "[ATS] Grant write FAILED for both autosave and quicksave. "
                    f"format={'plain-text' if plain else 'BSII-v3'}. "
                    "If saves are encrypted, install the 'cryptography' package "
                    "or set 'g_save_format 2' in config.cfg."
                )
        else:
            # No pending grants — still write items.json if anything else changed
            # (handled by the caller / item-receive path; no extra write needed here)
            pass

        from worlds.american_truck_simulator.locations import (
            ALL_LOCATIONS, CITY_ARRIVAL_LOCATIONS, GARAGE_UPGRADE_LOCATIONS,
            STATE_ARRIVAL_LOCATIONS,
        )
        new_checks: List[int] = []

        # Level milestone checks (re-evaluate all milestones each poll)
        for loc_name, loc_data in ALL_LOCATIONS.items():
            if loc_data.category == "level":
                milestone = int(loc_data.game_id.split("_")[1])
                if level >= milestone and loc_data.code not in self.checked_locations:
                    new_checks.append(loc_data.code)

        # City first arrival checks + state first visit checks
        new_cities = save["visited_cities"] - self._save_known_cities
        if new_cities:
            logger.info(f"[ATS] Save poll: {len(new_cities)} new city/cities detected: {sorted(new_cities)}")
        for city_id in new_cities:
            self._save_known_cities.add(city_id)
            matched = False
            for loc_data in CITY_ARRIVAL_LOCATIONS.values():
                if loc_data.game_id == city_id:
                    matched = True
                    if loc_data.code not in self.checked_locations:
                        new_checks.append(loc_data.code)
                        logger.info(f"[ATS] City arrival check queued: {city_id} → location {loc_data.code}")
                        # Check if this city reveals a new state
                        state_name = loc_data.region  # region == state display name
                        if state_name not in self._save_known_states:
                            self._save_known_states.add(state_name)
                            for sa_data in STATE_ARRIVAL_LOCATIONS.values():
                                if sa_data.region == state_name and sa_data.code not in self.checked_locations:
                                    new_checks.append(sa_data.code)
                                    logger.info(f"[ATS] First visit to state: {state_name}")
                                    break
                    else:
                        logger.debug(f"[ATS] City {city_id} already checked — skipping")
                    break
            if not matched:
                logger.debug(f"[ATS] City '{city_id}' from save has no matching location (not in enabled DLC or base states)")

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

    if _cryptography_available():
        logger.info("[ATS] cryptography package found — BSII v3 saves supported.")
    else:
        logger.warning(
            "[ATS] 'cryptography' package not found. Encrypted (BSII v3) saves "
            "cannot be read. To fix, run:  pip install cryptography  "
            "in the same Python environment as this client, then restart."
        )

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

        if gui_enabled:
            try:
                ctx.run_gui()
            except Exception as exc:
                logger.warning(f"[ATS] GUI failed to start ({exc!r}), running in CLI mode.")
        ctx.run_cli()

        await ctx.exit_event.wait()
        # server_loop sets ctx.server_task = None on exit; guard before cancelling.
        if ctx.server_task is not None:
            ctx.server_task.cancel()
        if ctx.watcher_task is not None:
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
