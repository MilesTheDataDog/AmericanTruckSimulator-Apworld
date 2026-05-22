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

# ── Save-grant persistence ────────────────────────────────────────────────────
# Tracks how much XP / money has already been written into the save file so we
# never double-apply across sessions.  Stored in grants.json next to items.json.

GRANTS_FILE = COMM_DIR / "grants.json"


def _load_save_grants() -> "tuple[int, int, int, int, int, int]":
    """Return (last_written_xp, last_written_money, write_total_xp, write_total_money, base_xp, reload_counter)."""
    try:
        if GRANTS_FILE.exists():
            data = _read_json(GRANTS_FILE)
            reload_counter = int(data.get("reload_counter", 0))
            base_xp        = int(data.get("base_xp", 0))
            # New format: tracks exact values written to disk.
            if "last_written_xp" in data:
                return (
                    int(data.get("last_written_xp",    0)),
                    int(data.get("last_written_money",  0)),
                    int(data.get("write_total_xp",      0)),
                    int(data.get("write_total_money",   0)),
                    base_xp,
                    reload_counter,
                )
            # Old format migration: applied_xp/applied_money → write_total_*.
            # last_written_* defaults to 0, which forces a safe re-apply of all
            # grants to the current save on the next poll.
            return (
                0,
                0,
                int(data.get("applied_xp",    0)),
                int(data.get("applied_money",  0)),
                base_xp,
                reload_counter,
            )
    except Exception:
        pass
    return 0, 0, 0, 0, 0, 0


def _persist_save_grants(
    last_written_xp: int,
    last_written_money: int,
    write_total_xp: int,
    write_total_money: int,
    base_xp: int = 0,
    reload_counter: int = 0,
) -> None:
    try:
        _write_json(GRANTS_FILE, {
            "last_written_xp":   last_written_xp,
            "last_written_money": last_written_money,
            "write_total_xp":    write_total_xp,
            "write_total_money": write_total_money,
            "base_xp":           base_xp,
            "reload_counter":    reload_counter,
        })
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
            if not slot.name.isdigit():
                continue  # only numbered manual slots; skip autosave/quicksave
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
                if not slot.name.isdigit():
                    continue  # only numbered manual slots; skip autosave/quicksave
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

    # visited_cities[N]: <city_id>
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


class ATSCommandProcessor(ClientCommandProcessor):
    def _cmd_status(self):
        """Show current ATS game state as seen by the client."""
        ctx: ATSContext = self.ctx
        logger.info(f"[ATS] Plugin connected: {ctx.plugin_connected}")
        logger.info(f"[ATS] Current level:    {ctx.current_level}")
        logger.info(f"[ATS] Current money:    ${ctx.current_money:,}")
        logger.info(f"[ATS] Checks sent:      {len(ctx.checked_locations)}")
        logger.info(f"[ATS] Goal satisfied:   {ctx.goal_complete}")

    def _cmd_checked(self):
        """List every location check the server has confirmed for this run."""
        ctx: ATSContext = self.ctx
        from worlds.american_truck_simulator.locations import ALL_LOCATIONS
        id_to_name = {data.code: name for name, data in ALL_LOCATIONS.items()
                      if data.code is not None}
        if not ctx.checked_locations:
            logger.info("[ATS] No locations checked yet.")
            return
        checked_names = sorted(
            id_to_name.get(loc_id, f"Unknown location {loc_id}")
            for loc_id in ctx.checked_locations
        )
        logger.info(f"[ATS] Checked locations ({len(checked_names)}):")
        for name in checked_names:
            logger.info(f"[ATS]   {name}")

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
        self._save_first_city_log: bool = False  # True after first city-count log
        self.current_xp: int = 0
        self._save_is_plain: bool = False  # True when save uses SiiN (g_save_format 2)
        self._save_scsc_meta: "Optional[dict]" = None  # set when save is an ScsC container
        self._save_not_found_warned: bool = False

        # Save-grant tracking (persisted across sessions in grants.json).
        # last_written_xp/money = the exact values in the last patched save.
        # write_total_xp/money  = total AP grants at the time of that write.
        # base_xp               = player's natural XP before any grants (fresh-profile detection).
        self._last_written_xp: int
        self._last_written_money: int
        self._last_write_total_xp: int
        self._last_write_total_money: int
        self._save_base_xp: int
        self._reload_counter: int
        (self._last_written_xp,
         self._last_written_money,
         self._last_write_total_xp,
         self._last_write_total_money,
         self._save_base_xp,
         self._reload_counter) = _load_save_grants()
        if self._last_written_xp or self._last_written_money:
            logger.info(
                f"[ATS] Loaded grant state from grants.json: "
                f"last_written_xp={self._last_written_xp:,}, "
                f"last_written_money=${self._last_written_money:,}, "
                f"write_total_xp={self._last_write_total_xp:,}, "
                f"base_xp={self._save_base_xp:,}"
            )

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
        _write_json(SLOT_DATA_FILE, self.slot_data)
        self._write_items_file()
        # Scout level milestone locations so item names are available when checks fire.
        # create_as_hint=0 means no permanent hints are created — purely informational.
        from worlds.american_truck_simulator.locations import LEVEL_MILESTONE_LOCATIONS
        milestone_ids = [d.code for d in LEVEL_MILESTONE_LOCATIONS.values() if d.code is not None]
        if milestone_ids:
            asyncio.create_task(self.send_msgs([{
                "cmd": "LocationScouts",
                "locations": milestone_ids,
                "create_as_hint": 0,
            }]))

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
        _evt_level = data.get("current_level", 0)
        _evt_money = data.get("current_money", 0)
        if _evt_level > 0:
            self.current_level = _evt_level
        if _evt_money > 0:
            self.current_money = _evt_money

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

        # Only read when the player has actually saved (mtime changed).
        # Do NOT bypass on has_pending — proactive reads use the stale pre-save
        # XP as the grant base, which loses the player's natural delivery XP.
        # Grants accumulate in memory and are applied in one correct write the
        # moment the player saves.
        pending_xp    = self._total_xp_granted    - self._last_write_total_xp
        pending_money = self._total_money_granted - self._last_write_total_money
        has_pending   = pending_xp > 0 or pending_money > 0

        if mtime <= self._save_last_mtime:
            return
        self._save_last_mtime = mtime

        text, fmt, scsc_meta = _read_sii_text(save_path)
        self._save_is_plain  = fmt in ("plain", "scsc_plain")
        self._save_scsc_meta = scsc_meta

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

                if fmt == "scsc_unreadable":
                    logger.warning(
                        "[ATS] Save file is in SCS HashFS (ScsC) container format but "
                        "the inner content could not be extracted.\n"
                        "This is unexpected for ATS 1.59 — please report this error "
                        "along with the hex bytes logged above."
                    )
                elif fmt == "no_crypto":
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
                elif fmt in ("bsii_v3", "scsc_bsii_v3"):
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

        # Detect a genuine save replacement (new profile / deleted save / manual
        # swap).  XP never decreases in ATS, so if the save XP is far below what
        # we've seen before, the file was replaced.
        #
        # We compare against _save_base_xp (the player's natural XP before any
        # grants), NOT against last_write_total_xp.  This prevents false resets when
        # ATS autosaves the pre-quickload state (which has the old, un-granted XP)
        # immediately after F9 fires — that autosave has the player's *natural* XP,
        # which equals base_xp, so the check correctly returns False.
        _save_xp = save["experience_points"]
        _reset_needed = False
        if self._save_base_xp > 0:
            # Reliable path: base_xp known → reset only if XP went below it.
            _reset_needed = _save_xp < self._save_base_xp
        elif self._last_write_total_xp > 0:
            # base_xp unknown (old grants.json migration) → reset only if XP is
            # dramatically below cumulative grants (genuine fresh-profile swap).
            _reset_needed = _save_xp < self._last_write_total_xp // 2

        if _reset_needed:
            logger.warning(
                f"[ATS] Save XP ({_save_xp:,}) dropped below base XP "
                f"({self._save_base_xp:,}) — save was likely replaced. "
                "Resetting grant tracking so grants are re-applied."
            )
            self._last_written_xp        = 0
            self._last_written_money      = 0
            self._last_write_total_xp     = 0
            self._last_write_total_money  = 0
            self._save_base_xp            = 0
            _persist_save_grants(0, 0, 0, 0, 0, self._reload_counter)

        # Grant-confirm check: if the save on disk already contains the XP we
        # last wrote (player loaded the patched save and has since saved again),
        # clear last_written so we don't keep re-applying the same grants.
        # We do NOT confirm from our own write-back (handled by _save_last_mtime).
        if self._last_written_xp > 0 and _save_xp >= self._last_written_xp:
            logger.info(
                f"[ATS] Grants confirmed in save: "
                f"save_xp ({_save_xp:,}) >= last_written ({self._last_written_xp:,}). "
                "Tracking cleared — ready for next grant."
            )
            self._last_written_xp   = 0
            self._last_written_money = 0
            _persist_save_grants(
                0, 0,
                self._last_write_total_xp, self._last_write_total_money,
                self._save_base_xp, self._reload_counter,
            )

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

        # Update live game state read by _check_win_condition.
        # XP is strictly monotonic in ATS; use max so that reading the pre-quickload
        # autosave (written by ATS immediately after F9 fires) never rolls back the
        # XP we already know the player has in their loaded save.
        level = _xp_to_level(save["experience_points"])
        self.current_level  = level
        self.current_money  = save["money"]
        self.current_xp     = max(self.current_xp, save["experience_points"])

        # ── Apply pending XP / money grants to the save file ──────────────────
        # total_*_granted      = cumulative AP grants this session.
        # last_written_*       = exact values written to the last patched save.
        # last_write_total_*   = total AP grants at the time of that write.
        #
        # Two cases:
        #  (A) save_xp >= last_written_xp  →  grants were loaded; add new delta only.
        #  (B) save_xp <  last_written_xp  →  player saved before loading our patch;
        #                                      bring save back up to last_written +
        #                                      any new grants received since then.

        # Stale-grants guard: write_total_xp can exceed total_xp_granted when
        # grants.json carries values from a previous AP run.  Reset so grants
        # re-apply to the current run.
        if self._last_write_total_xp > self._total_xp_granted > 0:
            logger.warning(
                f"[ATS] Stale grants detected: write_total_xp ({self._last_write_total_xp:,}) "
                f"> total_xp_granted ({self._total_xp_granted:,}). "
                "Resetting grant state so grants re-apply for this run."
            )
            self._last_written_xp        = 0
            self._last_written_money      = 0
            self._last_write_total_xp     = 0
            self._last_write_total_money  = 0
            self._save_base_xp            = 0
            _persist_save_grants(0, 0, 0, 0, 0, self._reload_counter)

        new_grants_xp    = max(0, self._total_xp_granted    - self._last_write_total_xp)
        new_grants_money = max(0, self._total_money_granted - self._last_write_total_money)

        if self._last_written_xp > 0 and _save_xp < self._last_written_xp:
            # Case (B): save was written from un-granted in-game state.
            # Bring XP back up to what we last wrote, then layer new grants on top.
            xp_delta    = (self._last_written_xp - _save_xp) + new_grants_xp
            # Money: if save_money is also below last_written (un-granted), restore it
            # too; otherwise only add new money grants.
            missing_money = max(0, self._last_written_money - save["money"])
            money_delta   = missing_money + new_grants_money
            if xp_delta > 0 or money_delta > 0:
                logger.info(
                    f"[ATS] Re-apply: save XP ({_save_xp:,}) < last-written "
                    f"({self._last_written_xp:,}); adding {xp_delta:,} XP, "
                    f"${money_delta:,} money."
                )
        else:
            # Case (A): grants are in this save (or no previous write).
            # Only apply genuinely new grants received since the last write.
            xp_delta    = new_grants_xp
            money_delta = new_grants_money

        if (xp_delta > 0 or money_delta > 0) and text is not None:
            # Use the save file's actual XP, not self.current_xp, which may be
            # inflated by a previous grant write the player hasn't loaded yet.
            new_xp    = _save_xp           + xp_delta
            new_money = self.current_money + money_delta

            # Patch the decrypted text, pinned to the economy/player block so we
            # never accidentally overwrite a hired driver's experience_points field.
            modified = text
            if xp_delta > 0:
                _econ_patch = _find_xp_block(modified)
                if _econ_patch:
                    _before = modified[:_econ_patch.end()]
                    _after  = modified[_econ_patch.end():]
                    _after  = re.sub(
                        r'\bexperience_points\s*:\s*\d+',
                        f'experience_points: {new_xp}',
                        _after, count=1,
                    )
                    # Log context around XP so we can identify the skill-points field name
                    _diag_m = re.search(r'\bexperience_points\s*:\s*\d+', _after)
                    if _diag_m:
                        _c0 = max(0, _diag_m.start() - 150)
                        _c1 = min(len(_after), _diag_m.end() + 400)
                        logger.info(f"[ATS] XP diag context:\n{_after[_c0:_c1]}")
                    modified = _before + _after
                    logger.debug(f"[ATS] Patched {_econ_patch.group(0)[:40].strip()} XP -> {new_xp:,}")
                else:
                    logger.warning("[ATS] No economy/player block found; patching first occurrence of experience_points")
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

            plain     = self._save_is_plain
            scsc_meta = self._save_scsc_meta

            if scsc_meta is not None:
                # Write as plain SiiNunit even when source was ScsC-encrypted.
                # ATS checks magic bytes on load and handles any format;
                # re-encrypting into ScsC has caused save corruption in testing
                # (zero-padding vs PKCS7 mismatch with the game's AES-CBC strip).
                _fmt_label = "SiiN plain-text (downgraded from ScsC)"
            elif plain:
                _fmt_label = "SiiN plain-text"
            else:
                _fmt_label = "BSII-v3-encrypted"
            logger.info(f"[ATS] Writing grants — format={_fmt_label}")

            def _write_save(dest: Path, content: str) -> bool:
                if scsc_meta is not None:
                    return _write_sii_plain(dest, content)
                return _write_sii_save(dest, content, plain=plain)

            # Write grants directly into the manual save slot that was read.
            # No quicksave involved — player just saves and reloads this slot.
            wrote_save = False
            try:
                wrote_save = _write_save(save_path, modified)
                if wrote_save:
                    # Verify: read back and confirm the XP landed correctly.
                    try:
                        _vtext, _vfmt, _ = _read_sii_text(save_path)
                        if _vtext:
                            _vecon = _find_xp_block(_vtext)
                            if _vecon:
                                _vregion = _vtext[_vecon.end():_vecon.end() + 100_000]
                                _vxp = re.search(r'\bexperience_points\s*:\s*(\d+)', _vregion)
                                logger.info(
                                    f"[ATS] VERIFY slot {save_path.parent.name} XP = "
                                    f"{int(_vxp.group(1)):,} (expected {new_xp:,})"
                                    if _vxp else
                                    f"[ATS] VERIFY slot {save_path.parent.name}: "
                                    "experience_points not found in block"
                                )
                            _vmoney = re.search(r'\bmoney_account\s*:\s*(-?\d+)', _vtext)
                            if _vmoney:
                                logger.info(
                                    f"[ATS] VERIFY slot {save_path.parent.name} money = "
                                    f"${int(_vmoney.group(1)):,} (expected ${new_money:,})"
                                )
                    except Exception as _ve:
                        logger.warning(f"[ATS] VERIFY read-back failed: {_ve}")
            except Exception as e:
                logger.warning(f"[ATS] Could not write save slot {save_path.parent.name}: {e}")

            if wrote_save:
                if self._save_base_xp == 0 and xp_delta > 0:
                    self._save_base_xp = _save_xp  # player's natural XP before any grants

                self._last_written_xp        = new_xp
                self._last_written_money      = new_money
                self._last_write_total_xp     = self._total_xp_granted
                self._last_write_total_money  = self._total_money_granted
                # Update mtime so the next poll doesn't re-read our own write.
                try:
                    self._save_last_mtime = save_path.stat().st_mtime
                except OSError:
                    pass
                _persist_save_grants(
                    self._last_written_xp,   self._last_written_money,
                    self._last_write_total_xp, self._last_write_total_money,
                    self._save_base_xp, self._reload_counter,
                )

                self.current_xp    = new_xp
                self.current_money = new_money

                logger.info(
                    f"[ATS] Grants written to save slot {save_path.parent.name}: "
                    f"+{xp_delta:,} XP, +${money_delta:,} money "
                    f"(totals: {new_xp:,} XP, ${new_money:,})"
                )
                logger.info(
                    f"[ATS] *** Reload save slot {save_path.parent.name} "
                    "(Esc → Load → select your save) to receive grants! ***"
                )
            else:
                logger.error(
                    f"[ATS] Grant write FAILED for slot {save_path.parent.name}. "
                    f"format={_fmt_label}. "
                    "If saves are BSII v3 encrypted, install the 'cryptography' package "
                    "or set 'g_save_format 0' in config.cfg."
                )
        else:
            if self._total_money_granted > 0 or self._total_xp_granted > 0:
                logger.debug(
                    f"[ATS] No pending grants: "
                    f"total_xp={self._total_xp_granted:,} write_total_xp={self._last_write_total_xp:,} "
                    f"last_written_xp={self._last_written_xp:,} save_xp={_save_xp:,} | "
                    f"total_money=${self._total_money_granted:,} write_total_money=${self._last_write_total_money:,}"
                )

        from worlds.american_truck_simulator.locations import (
            ALL_LOCATIONS, CITY_ARRIVAL_LOCATIONS, GARAGE_UPGRADE_LOCATIONS,
            STATE_ARRIVAL_LOCATIONS,
        )
        new_checks: List[int] = []

        # Level milestone checks (re-evaluate all milestones each poll)
        _locations_info = getattr(self, "locations_info", {})
        for loc_name, loc_data in ALL_LOCATIONS.items():
            if loc_data.category == "level":
                milestone = int(loc_data.game_id.split("_")[1])
                if level >= milestone:
                    if loc_data.code in self.checked_locations:
                        logger.debug(f"[ATS] Level {milestone} milestone already checked — skipping")
                    else:
                        new_checks.append(loc_data.code)
                        item_info = _locations_info.get(loc_data.code)
                        if item_info:
                            try:
                                item_name = self.item_names.lookup_in_game(item_info.item)
                            except Exception:
                                item_name = f"item#{item_info.item}"
                            recv_name = self.player_names.get(
                                item_info.player, f"Player {item_info.player}"
                            )
                            item_str = (item_name if item_info.player == self.slot
                                        else f"{item_name} → {recv_name}")
                            logger.info(
                                f"[ATS] Level milestone: Reached Level {milestone} — "
                                f"sending check (you receive: {item_str})"
                            )
                        else:
                            logger.info(
                                f"[ATS] Level milestone: Reached Level {milestone} — "
                                "sending check (item info not yet available)"
                            )

        # City first arrival checks + state first visit checks
        if not self._save_first_city_log:
            self._save_first_city_log = True
            logger.info(
                f"[ATS] Save poll (first read): {len(save['visited_cities'])} total cities in save: "
                f"{sorted(save['visited_cities'])}"
            )
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
                logger.info(f"[ATS] City '{city_id}' from save: no matching location (not in randomizer pool for this seed)")

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

    if ctx.auto_launch_game:
        _launch_ats_steam()

    logger.info("[ATS] Waiting for ATS plugin to connect...")

    _SAVE_POLL_INTERVAL_NORMAL  = 5.0   # seconds — idle, no grants in flight
    _SAVE_POLL_INTERVAL_PENDING = 0.5   # seconds — grants written but not yet confirmed
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

        # Use a fast poll interval whenever grants are in flight (written but
        # not yet confirmed loaded) so we can patch the save quickly after the
        # player saves — well within the time it takes to navigate the menus.
        grants_in_flight = (
            ctx._total_xp_granted    > ctx._last_write_total_xp  or
            ctx._total_money_granted > ctx._last_write_total_money or
            ctx._last_written_xp     > 0
        )
        interval = _SAVE_POLL_INTERVAL_PENDING if grants_in_flight else _SAVE_POLL_INTERVAL_NORMAL

        now = time.monotonic()
        if now - _last_save_poll >= interval:
            _last_save_poll = now
            try:
                ctx._poll_save_file()
            except Exception:
                logger.error(f"[ATS] Error polling save file:\n{traceback.format_exc()}")

        await asyncio.sleep(0.25)


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
    tmp = path.with_name(path.name + ".ap_tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    launch()
