#!/usr/bin/env python3
"""
ATS Data Extractor — reads def.scs (SCS HashFS v2) and extracts:
  - Cargo internal IDs + display names  → ats_cargo_ids.json
  - City internal IDs                   → ats_city_ids.json

Usage (run on the Windows machine that has ATS installed):
    python extract_ats_data.py [path-to-ATS-install-dir]

If no argument given, common Steam install paths are tried automatically.

Output files are written to the current directory:
    ats_cargo_ids.json
    ats_city_ids.json
"""

import json
import os
import re
import struct
import sys
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# SCS HashFS v2 reader
# Header (16 bytes): magic(4) version(2) salt(2) hash_type(4) entry_count(4)
# Entry (32 bytes):  hash(8) offset(8) flags(4) crc(4) size(4) comp_size(4)
# ---------------------------------------------------------------------------

HASHFS_MAGIC = b"SCS#"

FLAG_DIRECTORY  = 1
FLAG_COMPRESSED = 2
FLAG_ENCRYPTED  = 4


class _Entry:
    __slots__ = ("hash", "offset", "flags", "crc", "size", "comp_size")

    def __init__(self, hash_val, offset, flags, crc, size, comp_size):
        self.hash      = hash_val
        self.offset    = offset
        self.flags     = flags
        self.crc       = crc
        self.size      = size
        self.comp_size = comp_size

    @property
    def is_directory(self):  return bool(self.flags & FLAG_DIRECTORY)
    @property
    def is_compressed(self): return bool(self.flags & FLAG_COMPRESSED)
    @property
    def is_encrypted(self):  return bool(self.flags & FLAG_ENCRYPTED)


def _open_hashfs(path: Path):
    """Open a HashFS v2 archive and return (file_handle, entries_dict)."""
    f = open(path, "rb")
    magic = f.read(4)
    if magic != HASHFS_MAGIC:
        f.close()
        raise ValueError(f"Not a HashFS file — magic={magic!r}")

    # Header is exactly 16 bytes total
    version, salt, hash_type, entry_count = struct.unpack("<HHII", f.read(12))
    print(f"  HashFS v{version}, {entry_count:,} entries, salt={salt:#06x}, hash_type={hash_type:#010x}")

    entries: Dict[int, _Entry] = {}
    for _ in range(entry_count):
        raw = f.read(32)
        if len(raw) < 32:
            break
        h, off, fl, crc, sz, csz = struct.unpack("<QQIIII", raw)
        entries[h] = _Entry(h, off, fl, crc, sz, csz)

    return f, entries


def _read_entry(f, entry: _Entry) -> Optional[bytes]:
    """Read and decompress (if needed) a single entry."""
    if entry.is_encrypted:
        return None
    try:
        f.seek(entry.offset)
        data = f.read(entry.comp_size)
    except OSError:
        return None
    if entry.is_compressed:
        try:
            data = zlib.decompress(data)
        except zlib.error:
            return None
    return data


# ---------------------------------------------------------------------------
# Brute-force content scan — no path hashing needed
# ---------------------------------------------------------------------------

def _scan_all_sii(f, entries: Dict[int, _Entry], keyword: str) -> List[str]:
    """
    Read every non-directory, non-encrypted entry (up to 256 KB each),
    decode as UTF-8, and return those whose text contains `keyword`.
    """
    results = []
    checked = 0
    for entry in entries.values():
        if entry.is_directory or entry.is_encrypted:
            continue
        if entry.size > 256 * 1024:   # skip large non-text files
            continue
        data = _read_entry(f, entry)
        if data is None:
            continue
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            continue
        if keyword in text:
            results.append(text)
        checked += 1
    print(f"  Scanned {checked:,} text entries, found {len(results)} matching '{keyword}'")
    return results


# ---------------------------------------------------------------------------
# SII parser helpers
# ---------------------------------------------------------------------------

def _cargo_token(text: str) -> Optional[str]:
    m = re.search(r"\bcargo_data\s*:\s*cargo\.(\w+)", text)
    if m:
        return m.group(1)
    m = re.search(r'\bcargo_data\s*:\s*"cargo\.(\w+)"', text)
    return m.group(1) if m else None


def _cargo_name_key(text: str) -> Optional[str]:
    m = re.search(r'\bname(?:_key)?\s*:\s*"?([\w@.]+)"?', text)
    return m.group(1) if m else None


def _city_token(text: str) -> Optional[str]:
    m = re.search(r"\bcity_data\s*:\s*city\.(\w+)", text)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Locale loading (optional — gives display names for cargo)
# ---------------------------------------------------------------------------

def _load_locale(locale_scs: Path) -> Dict[str, str]:
    locale_map: Dict[str, str] = {}
    if not locale_scs.exists():
        return locale_map
    try:
        f, entries = _open_hashfs(locale_scs)
    except Exception as e:
        print(f"  [WARN] Could not open locale.scs: {e}")
        return locale_map

    print(f"  Scanning locale.scs for English cargo names ...")
    matched = _scan_all_sii(f, entries, "@@cargo")
    f.close()

    for text in matched:
        for m in re.finditer(r'^\s*([\w@.]+)\s*:\s*"([^"]+)"', text, re.MULTILINE):
            locale_map[m.group(1).lstrip("@")] = m.group(2)

    print(f"  Loaded {len(locale_map)} locale strings.")
    return locale_map


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------

def find_ats_install() -> Optional[Path]:
    candidates = [
        Path(r"C:\SteamLibrary\steamapps\common\American Truck Simulator"),
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\American Truck Simulator"),
        Path(r"D:\SteamLibrary\steamapps\common\American Truck Simulator"),
        Path(r"D:\Steam\steamapps\common\American Truck Simulator"),
        Path(r"E:\SteamLibrary\steamapps\common\American Truck Simulator"),
        Path(r"E:\Steam\steamapps\common\American Truck Simulator"),
    ]
    for c in candidates:
        if c.exists() and (c / "def.scs").exists():
            return c
    return None


def extract_cargo(def_scs: Path, locale_scs: Optional[Path]) -> List[Dict]:
    locale_map: Dict[str, str] = {}
    if locale_scs and locale_scs.exists():
        print(f"\nLoading locale from {locale_scs} ...")
        locale_map = _load_locale(locale_scs)

    print(f"\nOpening {def_scs} for cargo scan ...")
    f, entries = _open_hashfs(def_scs)
    sii_texts = _scan_all_sii(f, entries, "cargo_data")
    f.close()

    results: List[Dict] = []
    for text in sii_texts:
        token = _cargo_token(text)
        if not token:
            continue
        name_key = _cargo_name_key(text)
        display = ""
        if name_key:
            bare = name_key.lstrip("@")
            display = locale_map.get(bare, locale_map.get(name_key, ""))
        results.append({
            "internal_id": token,
            "display_name": display or name_key or token,
            "locale_key": name_key or "",
        })

    results.sort(key=lambda x: x["internal_id"])
    return results


def extract_cities(def_scs: Path) -> List[Dict]:
    print(f"\nOpening {def_scs} for city scan ...")
    f, entries = _open_hashfs(def_scs)
    sii_texts = _scan_all_sii(f, entries, "city_data")
    f.close()

    results: List[Dict] = []
    for text in sii_texts:
        token = _city_token(text)
        if token:
            results.append({"internal_id": token})

    results.sort(key=lambda x: x["internal_id"])
    return results


def main():
    if len(sys.argv) > 1:
        ats_dir = Path(sys.argv[1])
    else:
        ats_dir = find_ats_install()
        if ats_dir is None:
            print("ERROR: Could not auto-detect ATS install. Pass the path as an argument:")
            print("  python extract_ats_data.py \"D:\\SteamLibrary\\steamapps\\common\\American Truck Simulator\"")
            sys.exit(1)
        print(f"Found ATS at: {ats_dir}")

    def_scs    = ats_dir / "def.scs"
    locale_scs = ats_dir / "locale.scs"

    if not def_scs.exists():
        print(f"ERROR: def.scs not found at {def_scs}")
        sys.exit(1)

    # --- Cargo ---
    print("\n=== Extracting cargo IDs ===")
    cargo = extract_cargo(def_scs, locale_scs if locale_scs.exists() else None)
    print(f"Found {len(cargo)} cargo types.")
    cargo_out = Path("ats_cargo_ids.json")
    with open(cargo_out, "w", encoding="utf-8") as fh:
        json.dump(cargo, fh, indent=2, ensure_ascii=False)
    print(f"Wrote {cargo_out.resolve()}")

    print("\nFirst 30 cargo entries:")
    for c in cargo[:30]:
        print(f"  {c['internal_id']:35s}  {c['display_name']}")

    # --- Cities ---
    print("\n=== Extracting city IDs ===")
    cities = extract_cities(def_scs)
    print(f"Found {len(cities)} cities.")
    city_out = Path("ats_city_ids.json")
    with open(city_out, "w", encoding="utf-8") as fh:
        json.dump(cities, fh, indent=2, ensure_ascii=False)
    print(f"Wrote {city_out.resolve()}")

    print("\nFirst 30 city entries:")
    for c in cities[:30]:
        print(f"  {c['internal_id']}")

    print("\nDone! Share ats_cargo_ids.json and ats_city_ids.json.")


if __name__ == "__main__":
    main()
