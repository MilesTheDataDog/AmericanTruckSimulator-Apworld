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
import hashlib
import hmac as _hmac
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
CLIENT_VERSION = "1.1.0"

# ── Communication folder ───────────────────────────────────────────────────────
def _get_comm_dir() -> Path:
    docs = Path(os.environ.get("USERPROFILE", Path.home())) / "Documents" / "American Truck Simulator" / "archipelago"
    docs.mkdir(parents=True, exist_ok=True)
    return docs


COMM_DIR: Path = _get_comm_dir()
EVENTS_FILE = COMM_DIR / "events.json"
ITEMS_FILE = COMM_DIR / "items.json"
SLOT_DATA_FILE = COMM_DIR / "slot_data.json"
# Optional local overrides for slot_data received from the AP server.
# Keys present here replace what the server sends — useful when the multiworld
# was generated with incorrect options and cannot be regenerated.
# Example contents:  {"win_condition": 1, "goal_level": 5}
SLOT_DATA_OVERRIDE_FILE = COMM_DIR / "slot_data_override.json"
# Persisted user settings written by /setoptions. Read as fallback when server
# slot_data is empty and YAML search also fails.
CLIENT_OPTIONS_FILE = COMM_DIR / "ats_client_options.json"
# Persistent city-coordinate table built from captured telemetry positions.
# Survives across sessions; seeded at startup with 29 Koenvh1-verified entries.
COORD_STORE_FILE = COMM_DIR / "coord_store.json"

# ── City coordinate store ──────────────────────────────────────────────────────

DEFAULT_CITY_RADIUS = 600  # metres; default proximity trigger radius

# Per-city radius overrides for sprawling cities (metres).
# Tokens use the canonical ATS internal IDs (may be truncated to 12 chars).
# Existing coord_store.json entries keep their stored radius until deleted.
CITY_RADIUS_OVERRIDES: Dict[str, int] = {
    # Tier 1 — multiple distinct mapped hubs / very wide footprint
    "los_angeles":  1500,
    "dallas":       1200,
    "houston":      1200,
    "phoenix":      1200,
    # Tier 2 — single hub but large or sprawling
    "san_antonio":  1000,
    "austin":       900,
    "fort_worth":   900,
    "san_diego":    900,
    "seattle":      900,
    "portland":     900,
    "denver":       900,
    "las_vegas":    900,
    "salt_lake":    900,
    "kansas_ci_ks": 900,
    "kansas_city_mo": 900,
    "oklahoma_cit": 900,
    "el_paso":      900,
    "tulsa":        900,
    "omaha":        900,
    "st_louis":     900,
    "new_orleans":  900,
    # Tier 3 — medium-large
    "san_francisc": 800,
    "san_jose":     800,
    "sacramento":   800,
    "albuquerque":  800,
    "tucson":       800,
    "fresno":       800,
    "reno":         800,
    "spokane":      800,
    "boise":        800,
    "colorado_spr": 800,
    "amarillo":     800,
    "lubbock":      800,
    "wichita":      800,
    "des_moines":   800,
    "little_rock":  800,
    "baton_rouge":  800,
    "shreveport":   800,
}

# Koenvh1 telemetry-verified seed data — 29 original CA+NV cities (2015 launch).
# Tokens are the canonical ATS-internal IDs confirmed by def.scs extraction.
# (x, z) = world-space coordinates in metres.
_KOENVH1_SEED: Dict[str, tuple] = {
    "bakersfield":  (-52261.9,  20598.8),
    "barstow":      (-47300.4,  21963.2),
    "carson_city":  (-51957.3,   8904.9),
    "el_centro":    (-41183.4,  29223.9),
    "elko":         (-45027.1,   2043.1),
    "ely":          (-43077.7,   7074.1),
    "eureka":       (-68616.5,   3021.1),
    "fresno":       (-54802.6,  16248.6),
    "hilt":         (-63040.5,  -2368.5),
    "huron":        (-56245.7,  18908.2),
    "jackpot":      (-41684.1,  -1865.5),
    "las_vegas":    (-41596.9,  17626.8),
    "los_angeles":  (-52693.3,  24704.3),
    "oakland":      (-58786.3,  14301.8),
    "oxnard":       (-56628.7,  21492.7),
    "pioche":       (-40938.4,  10214.7),
    "primm":        (-43011.8,  20256.2),
    "redding":      (-61340.0,   2201.1),
    "reno":         (-55425.1,   5836.5),
    "sacramento":   (-59012.1,  10440.7),
    "san_diego":    (-46897.8,  29857.3),
    "san_francisc": (-60374.2,  13271.0),
    "santa_cruz":   (-58791.1,  18772.1),
    "stockton":     (-57824.6,  12037.9),
    "tonopah":      (-48104.3,  12496.8),
    "truckee":      (-56640.5,   8566.7),
    "winnemucca":   (-50540.7,   1847.2),
}


def _load_coord_store() -> Dict[str, Any]:
    """Load the persistent coordinate store from disk, or return empty dict."""
    try:
        if COORD_STORE_FILE.is_file():
            data = json.loads(COORD_STORE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.debug(f"[ATS] Could not load coord store: {e}")
    return {}


def _seed_coord_store(store: Dict[str, Any]) -> int:
    """Add Koenvh1 entries not already present. Returns count added."""
    added = 0
    for city_id, (x, z) in _KOENVH1_SEED.items():
        if city_id not in store:
            store[city_id] = {
                "x": x,
                "z": z,
                "radius": CITY_RADIUS_OVERRIDES.get(city_id, DEFAULT_CITY_RADIUS),
            }
            added += 1
    if added:
        try:
            _write_json(COORD_STORE_FILE, store)
        except Exception:
            pass
    return added


def _refresh_coord_radii(store: Dict[str, Any]) -> int:
    """Refresh every stored coord entry's radius from the current override table.

    Called at startup so that changes to DEFAULT_CITY_RADIUS or
    CITY_RADIUS_OVERRIDES take effect immediately even for cities that were
    already persisted in coord_store.json from a previous session.
    Returns the count of entries whose radius changed.
    """
    updated = 0
    for city_id, entry in store.items():
        if not isinstance(entry, dict):
            continue
        new_r = CITY_RADIUS_OVERRIDES.get(city_id, DEFAULT_CITY_RADIUS)
        if entry.get("radius") != new_r:
            entry["radius"] = new_r
            updated += 1
    return updated


# ── Save file parsing ─────────────────────────────────────────────────────────

_BSII_MAGIC = b"BSII"
_SIIN_MAGIC = b"SiiN"
_SCSC_MAGIC = b"ScsC"

# AES-256 key used by SCS in BSII v3 saves (sourced from the open-source
# SII_Decrypt community tool by Zukf / Xpericode).
_BSII_AES_KEY = bytes([
    0x2a, 0x5d, 0x6e, 0x3f, 0x8a, 0x14, 0x2c, 0x0d,
    0x9b, 0x7f, 0x4e, 0x21, 0xc6, 0xa1, 0x8d, 0x35,
    0xb7, 0xe9, 0x4f, 0x2c, 0x0d, 0x1a, 0x6b, 0x8e,
    0x3c, 0x7f, 0x50, 0x29, 0xd4, 0xe1, 0x6a, 0x38,
])

# AES-256-CBC key used by SCS in ScsC save containers (ATS 1.49+).
# Sourced from TheLazyTomcat/SII_Decrypt and fangyi-zhou/sii-decode-rs.
# The container header is: magic(4) + HMAC-SHA256(32) + IV(16) + DataSize(4) + ciphertext.
_SCSC_AES_KEY = bytes([
    0x2a, 0x5f, 0xcb, 0x17, 0x91, 0xd2, 0x2f, 0xb6,
    0x02, 0x45, 0xb3, 0xd8, 0x36, 0x9e, 0xd0, 0xb2,
    0xc2, 0x73, 0x71, 0x56, 0x3f, 0xbf, 0x1f, 0x3c,
    0x9e, 0xdf, 0x6b, 0x11, 0x82, 0x5a, 0x5d, 0x0a,
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


def _scan_profiles_dir(profiles_dir: Path, candidates: "list[tuple[float, Path]]") -> None:
    """Append (mtime, path) for every game.sii found under profiles_dir."""
    if not profiles_dir.is_dir():
        return
    for profile in profiles_dir.iterdir():
        if not profile.is_dir():
            continue
        save_dir = profile / "save"
        if not save_dir.is_dir():
            continue
        for slot in save_dir.iterdir():
            if not slot.is_dir():
                continue
            if slot.name == "quicksave":
                continue  # skip — client writes here; reading it back causes stale-state loops
            game_sii = slot / "game.sii"
            if not game_sii.exists():
                continue
            try:
                candidates.append((game_sii.stat().st_mtime, game_sii))
            except OSError:
                pass


def _profile_id_from_config(docs: Path) -> "Optional[str]":
    """Return the last-selected ATS profile ID from config.cfg, or None."""
    try:
        with (docs / "config.cfg").open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.search(r'\bg_last_select_profile_id\s+"([0-9A-Fa-f]+)"', line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def _find_ats_save_file() -> Optional[Path]:
    """Return the most-recently-modified READABLE game.sii for the active profile.

    Strategy 1 (preferred): read g_last_select_profile_id from config.cfg and
    scan only that profile's save directory.  This avoids picking up stale saves
    from old profiles before the player loads their current session.

    Strategy 2 (fallback): scan every profile across all known locations, sorted
    newest-first.  Used when config.cfg is missing or its profile ID has no saves.
    """
    docs = Path(os.environ.get("USERPROFILE", Path.home())) / "Documents" / "American Truck Simulator"

    candidates: "list[tuple[float, Path]]" = []

    # Strategy 1: config.cfg tells us exactly which profile is active.
    profile_id = _profile_id_from_config(docs)
    if profile_id:
        logger.debug(f"[ATS] config.cfg last profile: {profile_id}")
        profile_dirs = [
            docs / "profiles" / profile_id,
            docs / "steam" / "profiles" / profile_id,
        ]
        for remote in _steam_userdata_roots():
            profile_dirs.append(remote / "steam" / "profiles" / profile_id)
            profile_dirs.append(remote / "profiles" / profile_id)

        for profile_dir in profile_dirs:
            save_dir = profile_dir / "save"
            if not save_dir.is_dir():
                continue
            for slot in save_dir.iterdir():
                if not slot.is_dir():
                    continue
                if slot.name == "quicksave":
                    continue  # client writes here; reading it back causes stale-state loops
                game_sii = slot / "game.sii"
                if not game_sii.exists():
                    continue
                try:
                    candidates.append((game_sii.stat().st_mtime, game_sii))
                except OSError:
                    pass

    # Strategy 2: fall back to scanning all profiles if config gave us nothing.
    if not candidates:
        if profile_id:
            logger.debug(
                f"[ATS] Profile {profile_id} from config.cfg has no saves on disk — "
                "falling back to full profile scan"
            )
        for remote in _steam_userdata_roots():
            _scan_profiles_dir(remote / "steam" / "profiles", candidates)
            _scan_profiles_dir(remote / "profiles", candidates)
        _scan_profiles_dir(docs / "profiles", candidates)
        _scan_profiles_dir(docs / "steam" / "profiles", candidates)

    # Pick the newest readable save from whichever strategy produced candidates.
    for _mtime, path in sorted(candidates, key=lambda x: x[0], reverse=True):
        try:
            with path.open("rb") as _f:
                magic = _f.read(4)
        except OSError:
            logger.debug(f"[ATS] Save scan: could not open {path}")
            continue
        if magic in (_SIIN_MAGIC, _BSII_MAGIC, _SCSC_MAGIC):
            logger.debug(f"[ATS] Save scan: accepted {path} (magic={magic!r})")
            return path
        logger.info(f"[ATS] Save scan: skipped {path} (magic={magic!r}, unrecognised format)")

    return None



# ── Pure-Python AES-256-CBC ───────────────────────────────────────────────────
# No external dependencies — works in frozen PyInstaller builds and bare Python.

_AES_SBOX = bytes([
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
])
_AES_INV_SBOX = bytes([
    0x52,0x09,0x6a,0xd5,0x30,0x36,0xa5,0x38,0xbf,0x40,0xa3,0x9e,0x81,0xf3,0xd7,0xfb,
    0x7c,0xe3,0x39,0x82,0x9b,0x2f,0xff,0x87,0x34,0x8e,0x43,0x44,0xc4,0xde,0xe9,0xcb,
    0x54,0x7b,0x94,0x32,0xa6,0xc2,0x23,0x3d,0xee,0x4c,0x95,0x0b,0x42,0xfa,0xc3,0x4e,
    0x08,0x2e,0xa1,0x66,0x28,0xd9,0x24,0xb2,0x76,0x5b,0xa2,0x49,0x6d,0x8b,0xd1,0x25,
    0x72,0xf8,0xf6,0x64,0x86,0x68,0x98,0x16,0xd4,0xa4,0x5c,0xcc,0x5d,0x65,0xb6,0x92,
    0x6c,0x70,0x48,0x50,0xfd,0xed,0xb9,0xda,0x5e,0x15,0x46,0x57,0xa7,0x8d,0x9d,0x84,
    0x90,0xd8,0xab,0x00,0x8c,0xbc,0xd3,0x0a,0xf7,0xe4,0x58,0x05,0xb8,0xb3,0x45,0x06,
    0xd0,0x2c,0x1e,0x8f,0xca,0x3f,0x0f,0x02,0xc1,0xaf,0xbd,0x03,0x01,0x13,0x8a,0x6b,
    0x3a,0x91,0x11,0x41,0x4f,0x67,0xdc,0xea,0x97,0xf2,0xcf,0xce,0xf0,0xb4,0xe6,0x73,
    0x96,0xac,0x74,0x22,0xe7,0xad,0x35,0x85,0xe2,0xf9,0x37,0xe8,0x1c,0x75,0xdf,0x6e,
    0x47,0xf1,0x1a,0x71,0x1d,0x29,0xc5,0x89,0x6f,0xb7,0x62,0x0e,0xaa,0x18,0xbe,0x1b,
    0xfc,0x56,0x3e,0x4b,0xc6,0xd2,0x79,0x20,0x9a,0xdb,0xc0,0xfe,0x78,0xcd,0x5a,0xf4,
    0x1f,0xdd,0xa8,0x33,0x88,0x07,0xc7,0x31,0xb1,0x12,0x10,0x59,0x27,0x80,0xec,0x5f,
    0x60,0x51,0x7f,0xa9,0x19,0xb5,0x4a,0x0d,0x2d,0xe5,0x7a,0x9f,0x93,0xc9,0x9c,0xef,
    0xa0,0xe0,0x3b,0x4d,0xae,0x2a,0xf5,0xb0,0xc8,0xeb,0xbb,0x3c,0x83,0x53,0x99,0x61,
    0x17,0x2b,0x04,0x7e,0xba,0x77,0xd6,0x26,0xe1,0x69,0x14,0x63,0x55,0x21,0x0c,0x7d,
])
_AES_RCON = bytes([0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36])


def _aes_gmul(a: int, b: int) -> int:
    """GF(2^8) multiply under the AES irreducible polynomial x^8+x^4+x^3+x+1."""
    p = 0
    while b:
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xff
        if hi:
            a ^= 0x1b
        b >>= 1
    return p


# Pre-compute multiplication tables for InvMixColumns and MixColumns.
_G2 = bytes(_aes_gmul(i, 2) for i in range(256))
_G3 = bytes(_aes_gmul(i, 3) for i in range(256))
_G9 = bytes(_aes_gmul(i, 9) for i in range(256))
_GB = bytes(_aes_gmul(i, 0x0b) for i in range(256))
_GD = bytes(_aes_gmul(i, 0x0d) for i in range(256))
_GE = bytes(_aes_gmul(i, 0x0e) for i in range(256))


def _aes256_key_expand(key: bytes) -> "list[bytearray]":
    """Return list of 15 round-key bytearrays (each 16 bytes) for AES-256."""
    # AES-256: Nk=8, Nr=14, so 15 round keys of 4 words each = 60 words total.
    w = [bytearray(key[i*4:(i+1)*4]) for i in range(8)]
    for i in range(8, 60):
        temp = bytearray(w[i-1])
        if i % 8 == 0:
            temp = bytearray([
                _AES_SBOX[temp[1]] ^ _AES_RCON[i//8 - 1],
                _AES_SBOX[temp[2]],
                _AES_SBOX[temp[3]],
                _AES_SBOX[temp[0]],
            ])
        elif i % 8 == 4:
            temp = bytearray(_AES_SBOX[b] for b in temp)
        w.append(bytearray(a ^ b for a, b in zip(w[i-8], temp)))
    # Pack into 15 round keys (4 words each)
    return [bytearray(b for word in w[i*4:(i+1)*4] for b in word) for i in range(15)]


def _aes_add_round_key(state: bytearray, rk: bytearray) -> None:
    for i in range(16):
        state[i] ^= rk[i]


def _aes_decrypt_block(block: bytes, rks: "list[bytearray]") -> bytes:
    """Decrypt a single 16-byte AES-256 block (14 rounds)."""
    # State: column-major, s[row + 4*col]
    state = bytearray(block)
    _aes_add_round_key(state, rks[14])
    for rnd in range(13, 0, -1):
        # InvShiftRows
        state[1], state[5], state[9], state[13] = state[13], state[1], state[5], state[9]
        state[2], state[6], state[10], state[14] = state[10], state[14], state[2], state[6]
        state[3], state[7], state[11], state[15] = state[7], state[11], state[15], state[3]
        # InvSubBytes
        for i in range(16):
            state[i] = _AES_INV_SBOX[state[i]]
        _aes_add_round_key(state, rks[rnd])
        # InvMixColumns
        for c in range(4):
            s0, s1, s2, s3 = state[c*4], state[c*4+1], state[c*4+2], state[c*4+3]
            state[c*4]   = _GE[s0] ^ _GB[s1] ^ _GD[s2] ^ _G9[s3]
            state[c*4+1] = _G9[s0] ^ _GE[s1] ^ _GB[s2] ^ _GD[s3]
            state[c*4+2] = _GD[s0] ^ _G9[s1] ^ _GE[s2] ^ _GB[s3]
            state[c*4+3] = _GB[s0] ^ _GD[s1] ^ _G9[s2] ^ _GE[s3]
    # Final round (no InvMixColumns)
    state[1], state[5], state[9], state[13] = state[13], state[1], state[5], state[9]
    state[2], state[6], state[10], state[14] = state[10], state[14], state[2], state[6]
    state[3], state[7], state[11], state[15] = state[7], state[11], state[15], state[3]
    for i in range(16):
        state[i] = _AES_INV_SBOX[state[i]]
    _aes_add_round_key(state, rks[0])
    return bytes(state)


def _aes_encrypt_block(block: bytes, rks: "list[bytearray]") -> bytes:
    """Encrypt a single 16-byte AES-256 block (14 rounds)."""
    state = bytearray(block)
    _aes_add_round_key(state, rks[0])
    for rnd in range(1, 15):
        # SubBytes
        for i in range(16):
            state[i] = _AES_SBOX[state[i]]
        # ShiftRows
        state[1], state[5], state[9], state[13] = state[5], state[9], state[13], state[1]
        state[2], state[6], state[10], state[14] = state[10], state[14], state[2], state[6]
        state[3], state[7], state[11], state[15] = state[15], state[3], state[7], state[11]
        if rnd < 14:
            # MixColumns
            for c in range(4):
                s0, s1, s2, s3 = state[c*4], state[c*4+1], state[c*4+2], state[c*4+3]
                state[c*4]   = _G2[s0] ^ _G3[s1] ^ s2 ^ s3
                state[c*4+1] = s0 ^ _G2[s1] ^ _G3[s2] ^ s3
                state[c*4+2] = s0 ^ s1 ^ _G2[s2] ^ _G3[s3]
                state[c*4+3] = _G3[s0] ^ s1 ^ s2 ^ _G2[s3]
        _aes_add_round_key(state, rks[rnd])
    return bytes(state)


def _aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """AES-256-CBC decrypt. ciphertext must be a multiple of 16 bytes."""
    rks = _aes256_key_expand(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(ciphertext), 16):
        block = ciphertext[i:i+16]
        dec = _aes_decrypt_block(block, rks)
        out.extend(a ^ b for a, b in zip(dec, prev))
        prev = block
    return bytes(out)


def _aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """AES-256-CBC encrypt. plaintext must be a multiple of 16 bytes."""
    rks = _aes256_key_expand(key)
    out = bytearray()
    prev = bytearray(iv)
    for i in range(0, len(plaintext), 16):
        block = bytes(a ^ b for a, b in zip(plaintext[i:i+16], prev))
        enc = _aes_encrypt_block(block, rks)
        out.extend(enc)
        prev = bytearray(enc)
    return bytes(out)


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


def _decode_scsc(data: bytes) -> "Optional[tuple[bytes, Optional[dict]]]":
    """Decrypt and decompress an SCS ScsC save container (ATS 1.49+).

    Header layout (56 bytes before ciphertext):
        [  0- 3]  magic    "ScsC"
        [  4-35]  HMAC-SHA256 over ciphertext only (32 bytes), keyed with _SCSC_AES_KEY
        [ 36-51]  AES-IV   random 16-byte IV
        [ 52-55]  DataSize uint32-LE — uncompressed size after decryption+inflation
        [ 56+  ]  ciphertext — AES-256-CBC, zero-padded to 16-byte boundary

    Returns (inner_bytes, meta) on success; meta contains the IV so _write_scsc
    can re-encrypt using a fresh IV (meta is kept for API consistency — the IV
    is regenerated on write anyway).  Returns None on failure.
    """
    _SCSC_HEADER = 56
    if len(data) < _SCSC_HEADER + 16:
        logger.warning(
            "[ATS] ScsC: file too small to contain a valid header "
            f"({len(data)} bytes)"
        )
        return None

    iv         = data[36:52]
    data_size  = struct.unpack_from("<I", data, 52)[0]
    ciphertext = data[_SCSC_HEADER:]

    try:
        decrypted = _aes_cbc_decrypt(_SCSC_AES_KEY, iv, ciphertext)
        inner     = zlib.decompress(decrypted)
    except Exception as exc:
        logger.warning(f"[ATS] ScsC: decryption/decompression failed: {exc}")
        return None

    if len(inner) != data_size:
        logger.debug(
            f"[ATS] ScsC: DataSize field={data_size} but decompressed={len(inner)} bytes "
            "(mismatch is non-fatal)"
        )

    inner_magic = inner[:4]
    if inner_magic not in (_SIIN_MAGIC, _BSII_MAGIC):
        logger.warning(
            f"[ATS] ScsC: decrypted successfully but inner magic={inner_magic!r}, "
            "expected SiiN or BSII"
        )
        return None

    logger.debug(
        f"[ATS] ScsC: decrypted OK — inner magic={inner_magic!r}, "
        f"plain size={len(inner):,} bytes"
    )
    meta = {"iv": iv}  # IV captured for reference; _write_scsc generates a fresh one
    return inner, meta


def _write_scsc(path: Path, text: str, meta: dict) -> bool:
    """Encrypt and write SiiNunit text as an SCS ScsC save container (ATS 1.49+).

    Mirrors the container format read by _decode_scsc:
        magic(4) + HMAC-SHA256(32) + fresh-IV(16) + DataSize(4) + AES-256-CBC ciphertext

    HMAC is keyed with _SCSC_AES_KEY and covers the ciphertext only (not IV or DataSize).
    AES-CBC uses PKCS7 padding (standard).
    """
    try:
        raw       = text.encode("utf-8")
        compressed = zlib.compress(raw, level=6)

        # PKCS7 padding to AES block boundary (1..16 bytes, never 0)
        pad_len   = 16 - (len(compressed) % 16)
        plaintext = compressed + bytes([pad_len] * pad_len)

        iv         = os.urandom(16)
        ciphertext = _aes_cbc_encrypt(_SCSC_AES_KEY, iv, plaintext)
        # HMAC covers the ciphertext only, keyed with the AES key.
        # (per TheLazyTomcat/SII_Decrypt and fangyi-zhou/sii-decode-rs)
        data_size_bytes = struct.pack("<I", len(raw))
        mac        = _hmac.new(_SCSC_AES_KEY, ciphertext, hashlib.sha256).digest()

        file_bytes = (
            _SCSC_MAGIC
            + mac
            + iv
            + data_size_bytes
            + ciphertext
        )

        tmp = path.with_name(path.name + ".ap_tmp")
        tmp.write_bytes(file_bytes)
        tmp.replace(path)
        return True
    except Exception as e:
        logger.error(f"[ATS] _write_scsc failed for {path}: {e}")
        return False


def _write_sii_plain(path: Path, text: str) -> bool:
    """Write plaintext SiiNunit text as a plain-text save (g_save_format 2)."""
    try:
        raw = text.encode("utf-8")
        tmp = path.with_name(path.name + ".ap_tmp")
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
        tmp = path.with_name(path.name + ".ap_tmp")
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


def _read_sii_text(path: Path) -> "tuple[Optional[str], str, Optional[dict]]":
    """Read a .sii save file.

    Returns (text, format_tag, scsc_meta) where:
      text       — decoded SiiNunit text, or None on failure
      format_tag — one of: 'plain', 'bsii_v2', 'bsii_v3', 'scsc_plain',
                   'scsc_bsii_v2', 'scsc_bsii_v3', 'no_crypto',
                   'scsc_unreadable', 'unknown_magic_...', 'unreadable', 'too_small'
      scsc_meta  — dict for _write_scsc if the file was an ScsC container,
                   None for all other formats
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None, "unreadable", None

    if len(data) < 8:
        return None, "too_small", None

    magic = data[:4]

    if magic == _SIIN_MAGIC:
        return data.decode("utf-8", errors="replace"), "plain", None

    if magic == _SCSC_MAGIC:
        result = _decode_scsc(data)
        if result is None:
            return None, "scsc_unreadable", None
        inner, scsc_meta = result
        inner_magic = inner[:4]
        if inner_magic == _SIIN_MAGIC:
            return inner.decode("utf-8", errors="replace"), "scsc_plain", scsc_meta
        if inner_magic == _BSII_MAGIC:
            inner_version = struct.unpack_from("<I", inner, 4)[0]
            inner_payload = inner[8:]
            if inner_version == 2:
                try:
                    text = zlib.decompress(inner_payload).decode("utf-8", errors="replace")
                    return text, "scsc_bsii_v2", scsc_meta
                except zlib.error:
                    return None, "scsc_bsii_v2", scsc_meta
            if inner_version == 3:
                try:
                    from cryptography.hazmat.primitives.ciphers import Cipher  # noqa: F401
                except ImportError:
                    return None, "no_crypto", scsc_meta
                for skip in (0, 4):
                    dec = _decrypt_bsii_v3(inner_payload[skip:])
                    if dec and dec[:4] == _SIIN_MAGIC:
                        return dec.decode("utf-8", errors="replace"), "scsc_bsii_v3", scsc_meta
                return None, "scsc_bsii_v3", scsc_meta
        return None, "scsc_unreadable", scsc_meta

    if magic != _BSII_MAGIC:
        return None, f"unknown_magic_{magic!r}", None

    version = struct.unpack_from("<I", data, 4)[0]
    payload = data[8:]

    if version == 2:
        try:
            return zlib.decompress(payload).decode("utf-8", errors="replace"), "bsii_v2", None
        except zlib.error:
            return None, "bsii_v2", None

    if version == 3:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher  # noqa: F401
        except ImportError:
            return None, "no_crypto", None

        # Try both payload layouts: with or without a 4-byte uncompressed-size prefix.
        for skip in (0, 4):
            result = _decrypt_bsii_v3(payload[skip:])
            if result and result[:4] == _SIIN_MAGIC:
                return result.decode("utf-8", errors="replace"), "bsii_v3", None
        return None, "bsii_v3", None

    return None, f"bsii_v{version}", None


def _find_xp_block(text: str) -> "Optional[re.Match]":
    """Return a match whose .end() is just inside the block that contains
    experience_points and money_account.

    In all known ATS save formats these fields live in the economy unit:
      "economy : economy.economy {"   (old named style)
      "economy : _nameless.XXXXXXXX {" (ATS 1.59+ _nameless style)

    Older/unusual saves occasionally put them directly in a player block;
    that is handled as a fallback.
    """
    # Economy block — handles both named and _nameless IDs
    m = re.search(r'\beconomy\s*:\s*\S+\s*\{', text)
    if m:
        return m
    # Fallback: older saves that use a top-level player block
    return re.search(r'\bplayer\s*:\s*\S+\s*\{', text)


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

    _econ_m = _find_xp_block(text)
    _xp_region = text[_econ_m.end():_econ_m.end() + 100_000] if _econ_m else text
    xp_m = re.search(r"\bexperience_points\s*:\s*(\d+)", _xp_region)
    if xp_m:
        result["experience_points"] = int(xp_m.group(1))

    money_m = re.search(r"\bmoney_account\s*:\s*(-?\d+)", text)
    if money_m:
        result["money"] = int(money_m.group(1))

    # ATS 1.49+ format: visited_cities[N]: <city_id>  (plural key, no "city." prefix)
    for m in re.finditer(r"\bvisited_cities\[\d+\]\s*:\s*(\w+)", text):
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

_WIN_COND_NAME_TO_INT: Dict[str, int] = {
    "level_and_money": 0,
    "level_only":      1,
    "money_only":      2,
    "level_or_money":  3,
    "0": 0, "1": 1, "2": 2, "3": 3,
}


def _resolve_weighted(raw: Any) -> Any:
    """Resolve an AP weighted-choice dict {option: weight} to its chosen value.

    AP YAML templates express every option as a weighted dict, e.g.:
        win_condition:
          level_only: 50
          level_and_money: 0
    PyYAML parses that as {"level_only": 50, "level_and_money": 0}.
    This function returns the key with the highest positive weight.
    Plain int/string values pass through unchanged.
    """
    if not isinstance(raw, dict) or not raw:
        return raw
    candidates = {k: v for k, v in raw.items() if isinstance(v, (int, float)) and v > 0}
    if not candidates:
        return raw
    return max(candidates, key=lambda k: candidates[k])


def _parse_yaml_win_condition(raw: Any) -> Optional[int]:
    raw = _resolve_weighted(raw)
    if isinstance(raw, int) and 0 <= raw <= 3:
        return raw
    return _WIN_COND_NAME_TO_INT.get(str(raw).strip().lower())


def _find_ats_yaml(username: str, yaml_hint: Optional[Path] = None) -> Optional[Dict]:
    """Search for the player's ATS YAML file and return its options block.

    A YAML file is accepted if it contains an 'American Truck Simulator:' mapping
    section (regardless of the top-level 'game:' value).

    Search order:
      1. yaml_hint path (from --yaml CLI arg) — tried directly as a file
      2. C:\\ProgramData\\Archipelago\\Players  (most common AP install)
      3. Utils.local_path("Players") — AP launcher's own Players folder
      4. Players/ next to ATSClient.py
      5. Players/ in cwd and its parent
      6. Other common Windows AP installation paths
    """
    try:
        import yaml as _yaml
    except ImportError:
        logger.warning("[ATS] YAML search: PyYAML not available — cannot read YAML options")
        return None

    def _parse_yaml_file(path: Path) -> "Optional[tuple]":
        """Return (data_dict, ats_block) if path is a valid ATS YAML, else None."""
        try:
            raw = path.read_bytes()
            # Strip UTF-8 BOM if present
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            text = raw.decode("utf-8", errors="replace")
            data = _yaml.safe_load(text)
            if not isinstance(data, dict):
                return None
            ats_block = data.get("American Truck Simulator")
            if not isinstance(ats_block, dict):
                return None
            return data, ats_block
        except Exception as e:
            logger.info(f"[ATS] YAML search:   parse error in {path.name}: {e}")
            return None

    # ── If the user pointed us directly at a file ────────────────────────────
    if yaml_hint is not None:
        result = _parse_yaml_file(yaml_hint)
        if result:
            _, ats = result
            logger.info(f"[ATS] Using YAML from --yaml arg: {yaml_hint}")
            return ats
        logger.warning(
            f"[ATS] --yaml path {yaml_hint!r} is not a valid ATS YAML "
            "(no 'American Truck Simulator:' section found) — falling back to search"
        )

    # ── Build candidate directory list (most-likely first) ───────────────────
    candidate_dirs: List[Path] = []

    # Most common AP installation path on Windows — try it first
    if os.name == "nt":
        candidate_dirs.append(Path(r"C:\ProgramData\Archipelago\Players"))
        candidate_dirs.append(Path(r"C:\Archipelago\Players"))
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            up = Path(userprofile)
            candidate_dirs.append(up / "AppData" / "Local" / "Archipelago" / "Players")
            candidate_dirs.append(up / "AppData" / "Local" / "Programs" / "Archipelago" / "Players")
            candidate_dirs.append(up / "Desktop" / "Archipelago" / "Players")
            candidate_dirs.append(up / "Archipelago" / "Players")

    # AP launcher's own Players folder
    try:
        import Utils
        candidate_dirs.append(Path(Utils.local_path("Players")))
    except Exception:
        pass

    # Players/ next to ATSClient.py
    try:
        candidate_dirs.append(Path(__file__).resolve().parent / "Players")
    except Exception:
        pass

    # cwd and parent
    candidate_dirs.append(Path.cwd() / "Players")
    candidate_dirs.append(Path.cwd().parent / "Players")

    # Deduplicate while preserving order
    seen: set = set()
    unique_dirs: List[Path] = []
    for d in candidate_dirs:
        try:
            key = d.resolve()
        except Exception:
            key = d
        if key not in seen:
            seen.add(key)
            unique_dirs.append(d)

    user_lower = (username or "").lower().strip()

    for players_dir in unique_dirs:
        if not players_dir.is_dir():
            logger.debug(f"[ATS] YAML search: {players_dir} — not a directory, skipping")
            continue

        # Glob both .yaml and .yml
        yaml_files = sorted(players_dir.glob("*.yaml")) + sorted(players_dir.glob("*.yml"))
        logger.info(f"[ATS] YAML search: scanning {players_dir} ({len(yaml_files)} yaml/yml file(s))")

        matches: List[tuple] = []
        for yaml_path in yaml_files:
            result = _parse_yaml_file(yaml_path)
            if result is None:
                continue
            data, ats_block = result
            matches.append((yaml_path, data, ats_block))
            logger.info(
                f"[ATS] YAML search:   ATS YAML found: {yaml_path.name} "
                f"(name={data.get('name', '?')!r}, "
                f"win_condition={ats_block.get('win_condition', '?')!r})"
            )

        if not matches:
            logger.debug(f"[ATS] YAML search: no ATS YAMLs in {players_dir}")
            continue

        # Prefer an exact player-name match
        for yaml_path, data, ats_block in matches:
            yaml_name = str(data.get("name", "")).lower().strip()
            if user_lower and yaml_name == user_lower:
                logger.info(f"[ATS] Using YAML matched by player name '{username}': {yaml_path}")
                return ats_block

        # Single YAML in the folder — use it regardless of name
        if len(matches) == 1:
            yaml_path, _, ats_block = matches[0]
            logger.info(f"[ATS] Using sole ATS YAML found: {yaml_path}")
            return ats_block

        logger.warning(
            f"[ATS] Found {len(matches)} ATS YAMLs in {players_dir} "
            f"but none matched player name '{username}'. "
            "Use --yaml <path> to point directly at the correct file."
        )

    logger.warning(
        "[ATS] YAML search exhausted all candidate directories — "
        "options not found. Use  --yaml \"<full path to your YAML>\"  to fix this."
    )
    return None



def _load_client_options() -> Optional[Dict]:
    """Load user-saved options written by /setoptions."""
    try:
        if CLIENT_OPTIONS_FILE.is_file():
            data = json.loads(CLIENT_OPTIONS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.debug(f"[ATS] Could not load client options file: {e}")
    return None


def _apply_yaml_options(ctx: "ATSContext") -> None:
    """Patch ctx.slot_data with ATS options.

    Tries, in order:
      1. Player's YAML file (searched automatically or via --yaml)
      2. Saved options from /setoptions  (CLIENT_OPTIONS_FILE)

    Only called when the server's slot_data lacks 'game_version'.
    """
    yaml_hint: Optional[Path] = getattr(ctx, "_yaml_hint", None)
    yaml_opts = _find_ats_yaml(getattr(ctx, "username", "") or "", yaml_hint)
    source = "YAML"

    if yaml_opts is None:
        saved = _load_client_options()
        if saved:
            yaml_opts = saved
            source = "saved config (/setoptions)"
            logger.info("[ATS] YAML not found — using options saved by /setoptions.")
        else:
            logger.warning(
                "[ATS] Could not find YAML or saved options. "
                "Win condition defaults to level_and_money.\n"
                "[ATS] Fix: run  /setoptions win_condition=level_only  (and optionally "
                "goal_level=5 goal_money=1000) to save your settings permanently."
            )
            return

    changed: List[str] = []

    wc_raw = yaml_opts.get("win_condition")
    if wc_raw is not None:
        wc_int = _parse_yaml_win_condition(wc_raw)
        if wc_int is not None:
            ctx.slot_data["win_condition"] = wc_int
            changed.append(f"win_condition={wc_int} ({wc_raw})")

    gl_raw = _resolve_weighted(yaml_opts.get("goal_level"))
    if gl_raw is not None:
        try:
            ctx.slot_data["goal_level"] = int(gl_raw)
            changed.append(f"goal_level={gl_raw}")
        except (ValueError, TypeError):
            pass

    gm_raw = _resolve_weighted(yaml_opts.get("goal_money"))
    if gm_raw is not None:
        try:
            ctx.slot_data["goal_money"] = int(gm_raw)
            changed.append(f"goal_money={gm_raw}")
        except (ValueError, TypeError):
            pass

    if changed:
        logger.info(f"[ATS] Applied options from {source}: {', '.join(changed)}")
        ctx._options_source = source
    else:
        logger.warning(f"[ATS] {source} found but no win_condition/goal_level/goal_money could be parsed.")


class ATSCommandProcessor(ClientCommandProcessor):
    def _cmd_status(self):
        """Show current ATS game state as seen by the client."""
        ctx: ATSContext = self.ctx
        logger.info(f"[ATS] Seed name (AP):      {getattr(ctx, 'seed_name', '<not set>')}")
        logger.info(f"[ATS] Seed ID (DLL):       {ctx._seed_id()}")
        logger.info(f"[ATS] Plugin connected:    {ctx.plugin_connected}")
        logger.info(f"[ATS] Money ptr ready:     {ctx._ptr_money_ready}")
        logger.info(f"[ATS] XP ptr ready:        {ctx._ptr_xp_ready}")
        logger.info(f"[ATS] City ptr ready:      {ctx._ptr_city_ready}")
        logger.info(f"[ATS] Current level:       {ctx.current_level}")
        logger.info(f"[ATS] Current money:       ${ctx.current_money:,}")
        logger.info(f"[ATS] Total money granted: ${ctx._total_money_granted:,}")
        logger.info(f"[ATS] Total XP granted:    {ctx._total_xp_granted:,}")
        logger.info(f"[ATS] Checks sent:         {len(ctx.checked_locations)}")
        logger.info(f"[ATS] Goal satisfied:      {ctx.goal_complete}")
        logger.info(f"[ATS] Coord store:         {len(ctx._coord_store)} cities with coordinates")
        pos = ctx._truck_pos
        if pos:
            logger.info(f"[ATS] Truck position:      ({pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f})")
        logger.info(f"[ATS] Raw slot_data keys:  {list(ctx.slot_data.keys())}")
        src = ctx._options_source or ("server" if ctx.slot_data.get("game_version") else "none — run /setoptions")
        yaml_hint = ctx._yaml_hint
        logger.info(f"[ATS] Options source:      {src}"
                    + (f" (--yaml {yaml_hint})" if yaml_hint else ""))

        wc       = ctx.slot_data.get("win_condition", 0)
        lvl      = ctx.slot_data.get("goal_level", 35)
        money_k  = ctx.slot_data.get("goal_money", 1000)
        goal_money = money_k * 1000
        wc_names = {0: "level_and_money", 1: "level_only", 2: "money_only", 3: "level_or_money"}
        logger.info(f"[ATS] Win condition:       {wc_names.get(wc, wc)} (slot_data={wc})")
        logger.info(f"[ATS]   Level:  {ctx.current_level} >= {lvl} → {ctx.current_level >= lvl}")
        logger.info(f"[ATS]   Money:  ${ctx.current_money:,} >= ${goal_money:,} → {ctx.current_money >= goal_money}")

    def _cmd_resync(self):
        """Re-read the events file and resend any unchecked locations."""
        ctx: ATSContext = self.ctx
        logger.info("[ATS] Resyncing with plugin events file...")
        ctx._process_events_file(force=True)

    def _cmd_yamldebug(self):
        """Show all paths the client searched for your YAML, and retry the search."""
        ctx: ATSContext = self.ctx
        logger.info("[ATS] ── YAML search debug ──────────────────────────────────")
        yaml_hint: Optional[Path] = ctx._yaml_hint
        if yaml_hint:
            logger.info(f"[ATS]  --yaml arg provided: {yaml_hint}")
        try:
            import Utils
            ap_dir = Utils.local_path("")
            logger.info(f"[ATS]  Utils.local_path(''): {ap_dir}")
        except Exception as e:
            logger.info(f"[ATS]  Utils.local_path: unavailable ({e})")
        logger.info(f"[ATS]  __file__: {Path(__file__).resolve()}")
        logger.info(f"[ATS]  cwd:      {Path.cwd()}")
        logger.info("[ATS]  Retrying YAML search now...")
        _apply_yaml_options(ctx)
        logger.info(f"[ATS]  Options source after retry: {ctx._options_source}")
        logger.info("[ATS] ─────────────────────────────────────────────────────────")

    def _cmd_setoptions(self, args: str):
        """Manually set win condition and goals; saved permanently.

        Usage: /setoptions win_condition=level_only goal_level=5 goal_money=1000
          win_condition: level_only | level_and_money | money_only | level_or_money
          goal_level:    5–35  (driver level target)
          goal_money:    100–10000  (target in thousands; 1000 = $1,000,000)

        Settings are saved to a local file and loaded automatically on every
        future connect when the server slot_data is empty.
        """
        ctx: ATSContext = self.ctx
        wc_names = {0: "level_and_money", 1: "level_only", 2: "money_only", 3: "level_or_money"}
        opts: Dict[str, Any] = {}

        for token in args.strip().split():
            if "=" not in token:
                logger.warning(f"[ATS] /setoptions: skipping {token!r} — expected key=value")
                continue
            key, _, val = token.partition("=")
            key = key.strip().lower()
            val = val.strip()

            if key == "win_condition":
                wc = _parse_yaml_win_condition(val)
                if wc is None:
                    logger.warning(
                        f"[ATS] /setoptions: unknown win_condition {val!r}. "
                        "Valid values: level_only, level_and_money, money_only, level_or_money"
                    )
                    continue
                ctx.slot_data["win_condition"] = wc
                opts["win_condition"] = wc
                logger.info(f"[ATS] win_condition → {wc_names[wc]} ({wc})")

            elif key == "goal_level":
                try:
                    gl = int(val)
                    ctx.slot_data["goal_level"] = gl
                    opts["goal_level"] = gl
                    logger.info(f"[ATS] goal_level → {gl}")
                except ValueError:
                    logger.warning(f"[ATS] /setoptions: goal_level must be a number, got {val!r}")

            elif key == "goal_money":
                try:
                    gm = int(val)
                    ctx.slot_data["goal_money"] = gm
                    opts["goal_money"] = gm
                    logger.info(f"[ATS] goal_money → {gm} (= ${gm * 1000:,})")
                except ValueError:
                    logger.warning(f"[ATS] /setoptions: goal_money must be a number (thousands), got {val!r}")

            else:
                logger.warning(f"[ATS] /setoptions: unknown key {key!r}")

        if opts:
            existing = _load_client_options() or {}
            existing.update(opts)
            _write_json(CLIENT_OPTIONS_FILE, existing)
            ctx._options_source = "saved config (/setoptions)"
            _write_json(SLOT_DATA_FILE, ctx.slot_data)
            ctx._write_items_file()
            logger.info(
                f"[ATS] Options saved to {CLIENT_OPTIONS_FILE.name}. "
                "Active now and will load automatically on future connects."
            )
        else:
            logger.info(
                "[ATS] /setoptions: no valid options provided.\n"
                "[ATS] Example: /setoptions win_condition=level_only goal_level=5"
            )


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
    want_slot_data = True   # request slot_data from server on connect
    def make_gui(self):
        if ATSManager is not None:
            return ATSManager
        return super().make_gui()

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

        # Memory grant tracking — DLL applies grants directly to live memory
        self._total_money_granted: int = 0
        self._total_xp_granted: int = 0
        self._applied_item_count: int = 0

        # DLL pointer status (read from events.json)
        self._ptr_money_ready: bool = False
        self._ptr_xp_ready: bool = False
        self._ptr_city_ready: bool = False

        # Force a save poll on the next watcher cycle (used at startup and after delivery)
        self._force_save_poll: bool = False
        # True while a _poll_save_for_city_arrival task is running, prevents stacking tasks.
        self._city_arrival_poll_active: bool = False

        # Last level value for which we logged a win-condition progress line
        self._last_logged_level: int = 0

        # Notification queue for in-game popups (written to items.json)
        self._notifications: List[Dict] = []
        self._notification_counter: int = 0

        # Save file polling state
        self._save_last_mtime: float = 0.0
        self._save_known_cities: Set[str] = set()
        self._save_known_states: Set[str] = set()
        self._save_path_logged: bool = False
        self._save_first_city_log: bool = False  # True after first city-count log
        self.current_xp: int = 0
        self._save_not_found_warned: bool = False

        # Delivery-state tracking for arrival reward coalescing.
        # While a delivery is active, items.json writes from item receipts are
        # deferred so the arrival grant and the delivery grant coalesce into one
        # plugin apply at job_delivered.  Flushed at cargo_delivered or job_cancelled.
        self._job_active: bool = False
        self._items_write_deferred: bool = False
        self._held_arrival_labels: List[str] = []

        # Set by launch() from --yaml CLI arg; used as first search hint for YAML fallback
        self._yaml_hint: Optional[Path] = None
        # Set to "YAML", "saved config (/setoptions)", etc. when options loaded from fallback
        self._options_source: Optional[str] = None

        # ── Coordinate store (Option C proximity detection) ─────────────────
        # Persistent map of city_id → {x, z, radius} built from live telemetry.
        # Seeded at startup with Koenvh1 data; grows as the player visits cities.
        self._coord_store: Dict[str, Any] = _load_coord_store()
        added = _seed_coord_store(self._coord_store)
        updated_r = _refresh_coord_radii(self._coord_store)
        if added:
            logger.info(f"[ATS] Coord store: {len(self._coord_store)} cities "
                        f"({added} seeded from Koenvh1 verified data)")
        else:
            logger.info(f"[ATS] Coord store loaded: {len(self._coord_store)} cities with known coordinates")
        if updated_r:
            logger.info(f"[ATS] Coord store: {updated_r} radius value(s) refreshed to current config")
            try:
                _write_json(COORD_STORE_FILE, self._coord_store)
            except Exception:
                pass
        # Last truck position reported by the DLL via events.json [x, y, z].
        self._truck_pos: Optional[List[float]] = None
        # True only when the game world is active (telemetry_started, not in menu).
        # Gates proximity checks so they don't fire during career-select or loading screens.
        self._in_game: bool = False
        # Cities for which a city_arrival_hint has already been processed this session.
        # Separate from _save_known_cities (which is pre-seeded from the save baseline)
        # so that baseline cities still receive hint-triggered checks when delivered to.
        self._hint_sent_cities: Set[str] = set()

    # ── Archipelago callbacks ──────────────────────────────────────────────────

    async def server_auth(self, password_requested: bool = False) -> None:
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    def on_package(self, cmd: str, args: Dict) -> None:
        logger.debug(f"[ATS] Packet in: {cmd}")
        super().on_package(cmd, args)
        if cmd == "Connected":
            raw_sd = args.get("slot_data", {})
            # Log the raw slot_data from the server BEFORE any patching so the
            # user can confirm what the server actually baked into the seed.
            logger.info(f"[ATS] Raw slot_data from server: {json.dumps(raw_sd)}")
            self.slot_data = raw_sd

            # If the server's slot_data lacks "game_version" it was produced by AP's
            # built-in ATS world (not our custom apworld) whose fill_slot_data() may
            # return empty or default values.  Fall back to the player's YAML file,
            # which is the authoritative source of what the user intended.
            if not self.slot_data.get("game_version"):
                logger.warning(
                    "[ATS] slot_data from server has no 'game_version' key — "
                    "AP likely used its built-in ATS world instead of the custom apworld. "
                    "Attempting to read options from the player's YAML file..."
                )
                _apply_yaml_options(self)

            self._on_connected()
        # ReceivedItems is intentionally NOT handled here.
        # args["items"] contains raw JSON lists, not NetworkItem objects.
        # The base class converts them and appends to ctx.items_received;
        # game_watcher polls that list and calls _on_items_received with
        # proper NetworkItem objects.

    def _on_connected(self) -> None:
        logger.info(f"[ATS] Connected to Archipelago server as {self.username}")
        gl = self.slot_data.get("goal_level", 35)
        gm = self.slot_data.get("goal_money", 1000) * 1000
        logger.info(f"[ATS] Win condition: {self._win_condition_description()} (goal_level={gl}, goal_money=${gm:,})")
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
            if self._job_active:
                logger.debug("[ATS] Item write deferred — delivery in progress")
                self._items_write_deferred = True
            else:
                self._write_items_file()

    def _apply_item(self, item_name: str) -> None:
        if item_name.endswith("Money Grant"):
            from worlds.american_truck_simulator.items import ALL_ITEMS
            item_data = ALL_ITEMS.get(item_name)
            if item_data:
                amount = int(item_data.game_id.split("_")[1])
                self._total_money_granted += amount
                logger.info(f"[ATS] Money grant: +${amount:,} (total: ${self._total_money_granted:,})")

        elif item_name.endswith("XP Grant"):
            from worlds.american_truck_simulator.items import ALL_ITEMS
            item_data = ALL_ITEMS.get(item_name)
            if item_data:
                amount = int(item_data.game_id.split("_")[1])
                self._total_xp_granted += amount
                logger.info(f"[ATS] XP grant: +{amount:,} XP (total: {self._total_xp_granted:,})")

        elif item_name not in ("Victory", "Trucking Permit"):
            logger.warning(
                f"[ATS] Received unknown item {item_name!r} — no in-game effect. "
                "This item is from an older game seed; regenerate with the current apworld."
            )


    # ── Items file (client → plugin) ───────────────────────────────────────────

    def _seed_id(self) -> str:
        """Return a stable, per-seed string for the DLL's new-seed detection.

        AP's seed_name attribute is None in some versions.  When that happens we
        build a fallback from the server address + slot number, which changes
        every time the user opens a new room on archipelago.gg (each room has a
        unique port), giving the DLL a reliable 'this is a new game' signal.
        """
        seed = (getattr(self, "seed_name", "") or "")
        if not seed:
            addr = getattr(self, "server_address", "") or ""
            slot = getattr(self, "slot", 0) or 0
            if addr:
                seed = f"auto_{addr}_{slot}"
        return seed

    def _write_items_file(self) -> None:
        """Write cumulative grant totals and win-condition config for the DLL."""
        payload = {
            "version": 2,
            "timestamp": time.time(),
            # seed lets the DLL detect a new AP game and reset its applied counters,
            # preventing stale carryover from a previous seed from blocking new grants.
            "seed": self._seed_id(),
            "total_money_granted": self._total_money_granted,
            "total_xp_granted": self._total_xp_granted,
            "win_condition": self.slot_data.get("win_condition", 0),
            "goal_level": self.slot_data.get("goal_level", 35),
            "goal_money_thousands": self.slot_data.get("goal_money", 1000),
            "item_notifications": self._notifications,
        }
        _write_json(ITEMS_FILE, payload)

    def _flush_held_grants(self, reason: str) -> None:
        """Write items.json now, combining all arrival grants held during a delivery."""
        if self._items_write_deferred:
            labels = self._held_arrival_labels[:]
            logger.info(
                f"[ATS] Flushing held arrival grants ({reason})"
                + (f" — arrivals: {labels}" if labels else "")
            )
            self._items_write_deferred = False
            self._write_items_file()
        self._held_arrival_labels.clear()

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

        # Read live values from DLL memory pointers (non-zero when pointer captured)
        _evt_level = data.get("current_level", 0)
        _evt_money = data.get("current_money", 0)
        _evt_xp    = data.get("current_xp", 0)
        if _evt_level > 0:
            self.current_level = _evt_level
        if _evt_money > 0:
            self.current_money = _evt_money
        if _evt_xp > 0:
            self.current_xp = _evt_xp
            self.current_level = _xp_to_level(_evt_xp)

        # Track whether the game world is active (player driving, not in a menu).
        # Proximity must not fire during career-select, loading screens, or pause menus.
        self._in_game = data.get("in_game", False)

        # Update truck position from DLL telemetry; run proximity checks each poll.
        _pos = data.get("truck_position")
        if isinstance(_pos, list) and len(_pos) >= 3:
            self._truck_pos = _pos
        if self._truck_pos and self.auth and self._in_game:
            self._run_proximity_checks()

        # Log once per level-up so the player can see their progress toward the goal
        if self.current_level != self._last_logged_level and self.auth and self.slot_data:
            self._last_logged_level = self.current_level
            wc = self.slot_data.get("win_condition", WIN_LEVEL_AND_MONEY)
            gl = self.slot_data.get("goal_level", 35)
            if wc in (WIN_LEVEL_ONLY, WIN_LEVEL_AND_MONEY, WIN_LEVEL_OR_MONEY):
                l_ok = self.current_level >= gl
                logger.info(
                    f"[ATS] Level up: {self.current_level} "
                    f"(goal_level={gl} — {'GOAL MET' if l_ok else 'not yet met'})"
                )

        # DLL pointer status
        self._ptr_money_ready = data.get("ptr_money_ready", False)
        self._ptr_xp_ready    = data.get("ptr_xp_ready", False)
        self._ptr_city_ready  = data.get("ptr_city_ready", False)

        # Log when DLL confirms grants applied
        _applied_money = data.get("applied_money_total", 0)
        _applied_xp    = data.get("applied_xp_total", 0)
        if _applied_money > 0 or _applied_xp > 0:
            logger.debug(f"[ATS] DLL applied grants: money=${_applied_money:,} xp={_applied_xp:,}")

        # Track delivery state for arrival-reward coalescing.
        # job_active stays true in the plugin after delivery (until next job config);
        # cargo_delivered is the authoritative signal that delivery has completed.
        new_job_active = data.get("job_active", False)
        if self._job_active and not new_job_active:
            # Job was cancelled (plugin cleared job_active without a delivery event).
            logger.info("[ATS] Job no longer active — flushing any held arrival grants")
            self._flush_held_grants("job cancelled")
        self._job_active = new_job_active

        # When the DLL signals a new city, start a dedicated polling task that
        # retries reading the save file until the game writes it (autosave or delivery).
        # This decouples city checks from deliveries: the check fires at the next save,
        # not at the next delivery.
        # Backstop: the game's ks_visit_cities stat updated (may be minutes after entry).
        # The live city_arrival_hint events are the primary trigger; this catches
        # bobtail exploration where no job is active to provide a hint.
        if data.get("city_count_changed", False):
            logger.info("[ATS] City count incremented (backstop) — scheduling save poll")
            if not self._city_arrival_poll_active:
                self._city_arrival_poll_active = True
                asyncio.create_task(self._poll_save_for_city_arrival())

        new_checks: List[int] = []

        for event in data.get("events", []):
            event_id = event.get("id")
            if event_id in self._processed_event_ids:
                continue  # silently skip already-processed events
            logger.debug(f"[ATS] New event: {event_id}")
            self._processed_event_ids.add(event_id)

            # Live city-arrival hint from the DLL (source city at job start, or
            # destination city at delivery).  Process immediately — no save poll needed.
            # Also capture the truck's current coordinate to seed the proximity store.
            if event.get("type") == "city_arrival_hint":
                hint_city = event.get("game_id", "")
                _hint_type = event.get("hint_type", "")  # "source" or "destination"
                _hint_label = f"DLL hint: {_hint_type}" if _hint_type else "DLL hint"
                if hint_city and self.auth:
                    if self._truck_pos and hint_city not in self._coord_store:
                        self._capture_coord(hint_city, self._truck_pos)
                    self._process_city_arrival_hint(hint_city, _hint_label)
                continue

            # On delivery: clear local job-active state and flush any arrival grants
            # held during the delivery so they combine with the delivery grant in
            # one plugin apply.  Then schedule a post-delivery save poll.
            if event.get("type") == "cargo_delivered":
                self._job_active = False
                self._flush_held_grants("delivery complete")
                asyncio.create_task(self._poll_save_after_delivery())

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

        # Check win condition — only when connected (slot_data populated from server)
        if self.auth and not self.goal_complete and self._check_win_condition():
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
        else:
            return None

        loc_data = ALL_LOCATIONS.get(loc_name)
        if loc_data is None:
            logger.warning(f"[ATS] Unknown location from event: {loc_name!r}")
            return None
        return loc_data.code

    def _check_win_condition(self) -> bool:
        if not self.slot_data:
            return False  # slot_data not yet received from server; don't use defaults
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

    def _process_city_arrival_hint(self, city_id: str, source_label: str) -> None:
        """Immediately process a live city-arrival hint from the DLL.

        Fires the city/state location check at the instant the DLL signals the
        arrival (job start or delivery) rather than waiting for the save file.
        Idempotent per-session via _hint_sent_cities (intentionally independent
        of _save_known_cities so baseline cities still receive hint-triggered checks).
        """
        from worlds.american_truck_simulator.locations import (
            CITY_ARRIVAL_LOCATIONS, STATE_ARRIVAL_LOCATIONS,
        )
        if city_id in self._hint_sent_cities:
            logger.debug(f"[ATS] City arrival hint ({source_label}): {city_id} already hint-processed — skip")
            return
        self._hint_sent_cities.add(city_id)
        logger.info(f"[ATS] Live city arrival: {city_id} ({source_label}) — new, signalling immediately")
        self._save_known_cities.add(city_id)
        new_checks: List[int] = []
        for loc_data in CITY_ARRIVAL_LOCATIONS.values():
            if loc_data.game_id == city_id:
                _reward_tag = ("(reward held — delivery active)"
                               if self._job_active else "(reward immediate — no active delivery)")
                if loc_data.code not in self.checked_locations:
                    new_checks.append(loc_data.code)
                    if self._job_active and city_id not in self._held_arrival_labels:
                        self._held_arrival_labels.append(city_id)
                    logger.info(f"[ATS] City arrival check: {city_id} → {loc_data.code} {_reward_tag}")
                state_name = loc_data.region
                if state_name not in self._save_known_states:
                    logger.info(f"[ATS] New state detected: {state_name}")
                    self._save_known_states.add(state_name)
                    for sa_data in STATE_ARRIVAL_LOCATIONS.values():
                        if sa_data.region == state_name and sa_data.code not in self.checked_locations:
                            new_checks.append(sa_data.code)
                            _slabel = f"state:{state_name}"
                            if self._job_active and _slabel not in self._held_arrival_labels:
                                self._held_arrival_labels.append(_slabel)
                            logger.info(f"[ATS] State arrival check: {state_name} → {sa_data.code} {_reward_tag}")
                            break
                break
        if new_checks:
            logger.info(f"[ATS] Sending {len(new_checks)} live arrival check(s) for {city_id}")
            asyncio.create_task(self.send_msgs([{
                "cmd": "LocationChecks",
                "locations": new_checks,
            }]))
        else:
            logger.debug(f"[ATS] City arrival hint ({source_label}): {city_id} — no unchecked locations to send")

    def _capture_coord(self, city_id: str, pos: List[float]) -> None:
        """Record city_id → (x, z) into the persistent coordinate store.

        Called when the DLL fires a city_arrival_hint and we have a current
        truck position.  The position at that moment is at or near the city,
        making it a reliable seed coordinate.  No-ops if the coord is already
        in the store so established entries are never overwritten.
        """
        if city_id in self._coord_store:
            return
        x = round(pos[0], 1)
        z = round(pos[2], 1)
        r = CITY_RADIUS_OVERRIDES.get(city_id, DEFAULT_CITY_RADIUS)
        self._coord_store[city_id] = {"x": x, "z": z, "radius": r}
        logger.info(f"[ATS] Coordinate captured: {city_id} = ({x}, {z})")
        try:
            _write_json(COORD_STORE_FILE, self._coord_store)
        except Exception as e:
            logger.warning(f"[ATS] Could not persist coord store: {e}")

    def _run_proximity_checks(self) -> None:
        """Check truck position against the coordinate store for unvisited cities.

        Called on every events.json read (~2 s interval when the DLL is running).
        Cities without a stored coordinate are skipped — they will be caught by
        the job-hint or city-count-backstop paths, and their coordinate will be
        captured at that time so proximity works on future visits.
        """
        if not self._truck_pos:
            return
        tx = self._truck_pos[0]
        tz = self._truck_pos[2]
        for city_id, entry in self._coord_store.items():
            if city_id in self._save_known_cities:
                continue  # already marked visited — skip
            cx = entry.get("x", 0.0)
            cz = entry.get("z", 0.0)
            r  = entry.get("radius", DEFAULT_CITY_RADIUS)
            dist = ((tx - cx) ** 2 + (tz - cz) ** 2) ** 0.5
            if dist <= r:
                logger.info(
                    f"[ATS] Proximity trigger: entering {city_id} "
                    f"(dist={dist:.0f}m, limit={r}m)"
                )
                self._process_city_arrival_hint(city_id, f"proximity ({dist:.0f}m)")

    async def _poll_save_after_delivery(self) -> None:
        """
        Wait for the game to finish writing game.sii after a delivery, then
        force a save poll to catch the destination city and any new cities the
        save file now contains.  The game typically writes game.sii 12-18 s
        after the delivery event; we wait 20 s to be safe.
        """
        await asyncio.sleep(20.0)
        logger.info("[ATS] Post-delivery poll: forcing save read for delivery-city checks")
        self._force_save_poll = True

    async def _poll_save_for_city_arrival(self) -> None:
        """
        Poll game.sii with retries after the DLL reports city_count_changed.
        ATS writes the save file on delivery, hotel sleep, or periodic autosave
        (~every 60-120 s).  We retry up to ~120 s so the check fires at the
        next autosave without requiring a delivery.

        City checks are sent immediately when the save file reflects the new
        visit — no delivery is needed.  This coroutine is idempotent: only one
        instance runs at a time (guarded by _city_arrival_poll_active).
        """
        # Delays between retries: 5 s, then 10 s × 11 = ~115 s total
        RETRY_DELAYS = [5] + [10] * 11
        known_before = len(self._save_known_cities)
        try:
            for delay in RETRY_DELAYS:
                await asyncio.sleep(delay)
                if not self.auth:
                    return
                self._force_save_poll = True
                self._poll_save_file()
                if len(self._save_known_cities) > known_before:
                    logger.info(
                        f"[ATS] City arrival poll: new city confirmed in save "
                        f"({len(self._save_known_cities) - known_before} new)"
                    )
                    return
                logger.debug(
                    f"[ATS] City arrival poll: save not yet updated "
                    f"(known={len(self._save_known_cities)})"
                )
            logger.warning(
                "[ATS] City arrival poll: gave up after ~120 s — "
                "city will appear on next save read (delivery or autosave)"
            )
        finally:
            self._city_arrival_poll_active = False

    def _poll_save_file(self) -> None:
        """
        Read the most recent ATS game.sii to detect new city/state visits and
        update level/money as a fallback when DLL memory pointers are not yet
        captured.  No save-file patching occurs here — grants are applied
        directly to live memory by the DLL.
        """
        if not self.auth:
            return

        save_path = _find_ats_save_file()
        if not save_path:
            if not self._save_not_found_warned:
                self._save_not_found_warned = True
                logger.warning(
                    "[ATS] No ATS save file (game.sii) found. "
                    "City/state checks will not fire until a save is found. "
                    "Make sure ATS has been saved at least once."
                )
            return

        if not self._save_path_logged:
            self._save_not_found_warned = False
            self._save_path_logged = True
            logger.info(f"[ATS] Found save file: {save_path}")

        try:
            mtime = save_path.stat().st_mtime
        except OSError:
            return

        if not self._force_save_poll and mtime <= self._save_last_mtime:
            return
        self._force_save_poll = False
        self._save_last_mtime = mtime

        text, fmt, _ = _read_sii_text(save_path)
        if text is None:
            logger.debug(f"[ATS] Save file unreadable (format: {fmt}) — skipping poll")
            return

        save = _parse_sii_save(text)

        # Update level/money from save as fallback when DLL pointers not yet captured.
        if not self._ptr_xp_ready:
            level = _xp_to_level(save["experience_points"])
            self.current_level = level
            self.current_xp    = save["experience_points"]
        if not self._ptr_money_ready:
            self.current_money = save["money"]

        from worlds.american_truck_simulator.locations import (
            CITY_ARRIVAL_LOCATIONS, STATE_ARRIVAL_LOCATIONS,
        )
        new_checks: List[int] = []

        # Level milestone checks
        from worlds.american_truck_simulator.locations import ALL_LOCATIONS
        level_checks_sent: List[str] = []
        for loc_name, loc_data in ALL_LOCATIONS.items():
            if loc_data.category == "level":
                milestone = int(loc_data.game_id.split("_")[1])
                if self.current_level >= milestone and loc_data.code not in self.checked_locations:
                    new_checks.append(loc_data.code)
                    level_checks_sent.append(loc_name)

        if level_checks_sent and not self.goal_complete:
            wc      = self.slot_data.get("win_condition", WIN_LEVEL_AND_MONEY)
            gl      = self.slot_data.get("goal_level", 35)
            gm      = self.slot_data.get("goal_money", 1000) * 1000
            wc_name = {0: "Level+Money", 1: "Level Only", 2: "Money Only", 3: "Level or Money"}.get(wc, str(wc))
            l_ok    = self.current_level >= gl
            m_ok    = self.current_money  >= gm
            logger.info(
                f"[ATS] Level milestone(s) fired: {level_checks_sent} — "
                f"win condition ({wc_name}): "
                f"level {self.current_level}/{gl} ({'met' if l_ok else 'NOT MET'}), "
                f"money ${self.current_money:,}/${gm:,} ({'met' if m_ok else 'NOT MET'})"
            )

        # City first arrival checks
        # The first save read after client launch establishes a baseline — we must NOT
        # fire checks for cities already in the save at that point, because they may be
        # from previous runs.  Only cities that appear in SUBSEQUENT reads (i.e. visited
        # during this session) are eligible for checks.
        is_first_read = not self._save_first_city_log
        if not self._save_first_city_log:
            self._save_first_city_log = True
            game_cities_raw = set()
            for m in re.finditer(r"\bvisited_city\s*\[\d+\]\s*:\s*(\S+)", text):
                game_cities_raw.add(m.group(1))
            logger.info(
                f"[ATS] Save poll (first read, fmt={fmt}): "
                f"{len(save['visited_cities'])} cities — seeding baseline, firing unchecked state arrivals "
                f"(game.sii raw={len(game_cities_raw)})"
            )
            if len(game_cities_raw) == 0 and text:
                idx = text.lower().find("visited")
                if idx >= 0:
                    logger.info(f"[ATS] Save 'visited' context: {text[max(0,idx-20):idx+200]!r}")
                else:
                    logger.info(f"[ATS] Save file has no 'visited' keyword. First 400 chars: {text[:400]!r}")

        new_cities = save["visited_cities"] - self._save_known_cities
        if new_cities and not is_first_read:
            logger.info(f"[ATS] New cities detected: {sorted(new_cities)}")
        for city_id in new_cities:
            self._save_known_cities.add(city_id)
            for loc_data in CITY_ARRIVAL_LOCATIONS.values():
                if loc_data.game_id == city_id:
                    if is_first_read:
                        # Seed known states; fire check if server hasn't confirmed it yet.
                        # This recovers state checks for states visited before the randomizer.
                        state_name = loc_data.region
                        if state_name not in self._save_known_states:
                            self._save_known_states.add(state_name)
                            for sa_data in STATE_ARRIVAL_LOCATIONS.values():
                                if sa_data.region == state_name and sa_data.code not in self.checked_locations:
                                    new_checks.append(sa_data.code)
                                    logger.info(f"[ATS] State arrival check: {state_name} → {sa_data.code} (baseline recovery)")
                                    break
                    else:
                        _reward_tag = "(reward held — delivery active)" if self._job_active else "(reward immediate — no active delivery)"
                        # City check — independent of state check below
                        if loc_data.code not in self.checked_locations:
                            new_checks.append(loc_data.code)
                            if self._job_active and city_id not in self._held_arrival_labels:
                                self._held_arrival_labels.append(city_id)
                            logger.info(f"[ATS] City arrival check: {city_id} → {loc_data.code} {_reward_tag}")
                        # State check — always runs regardless of city check status
                        state_name = loc_data.region
                        if state_name not in self._save_known_states:
                            logger.info(f"[ATS] New state detected: {state_name}")
                            self._save_known_states.add(state_name)
                            for sa_data in STATE_ARRIVAL_LOCATIONS.values():
                                if sa_data.region == state_name and sa_data.code not in self.checked_locations:
                                    new_checks.append(sa_data.code)
                                    _slabel = f"state:{state_name}"
                                    if self._job_active and _slabel not in self._held_arrival_labels:
                                        self._held_arrival_labels.append(_slabel)
                                    logger.info(f"[ATS] State arrival check: {state_name} → {sa_data.code} {_reward_tag}")
                                    break
                    break

        if new_checks:
            logger.info(f"[ATS] Save poll: sending {len(new_checks)} check(s).")
            asyncio.create_task(self.send_msgs([{
                "cmd": "LocationChecks",
                "locations": new_checks,
            }]))

        if not self.goal_complete and self._check_win_condition():
            self.goal_complete = True
            asyncio.create_task(self.send_msgs([{
                "cmd": "StatusUpdate",
                "status": ClientStatus.CLIENT_GOAL,
            }]))
            logger.info("[ATS] Goal complete!")

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
        if ctx._force_save_poll or now - _last_save_poll >= _SAVE_POLL_INTERVAL:
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
    parser.add_argument(
        "--yaml",
        metavar="PATH",
        default=None,
        help=(
            "Path to your 'American Truck Simulator.yaml' file. "
            "Used to read win_condition, goal_level, and goal_money when the server's "
            "slot_data is missing those values (happens when AP's built-in ATS world "
            "is used instead of the custom apworld). "
            "Example: --yaml \"C:\\AP\\Players\\American Truck Simulator.yaml\""
        ),
    )
    args, _ = parser.parse_known_args()
    colorama.init()

    async def main():
        ctx = ATSContext(
            args.connect,
            args.password,
            auto_launch_game=not args.no_launch,
        )
        logger.info(f"[ATS] Client starting — game={ctx.game!r}, want_slot_data={ctx.want_slot_data}")
        # Store the --yaml hint on the context so _apply_yaml_options can use it.
        ctx._yaml_hint = Path(args.yaml) if args.yaml else None
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
    tmp = path.with_name(path.name + ".ap_tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    launch()
