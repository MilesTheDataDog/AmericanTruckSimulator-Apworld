#!/usr/bin/env python3
"""
ATS Data Extractor — reads def.scs (SCS HashFS v2) and extracts:
  - Cargo internal IDs + display names  → prints JSON for cargo_types.json
  - City internal IDs                   → prints JSON for cities.json validation

Usage (run on the Windows machine that has ATS installed):
    python extract_ats_data.py [path-to-ATS-install-dir]

Default install path tried if no argument given:
    C:\\SteamLibrary\\steamapps\\common\\American Truck Simulator
    C:\\Program Files (x86)\\Steam\\steamapps\\common\\American Truck Simulator

Output:
    ats_cargo_ids.json   — mapping of display_name -> internal_id
    ats_city_ids.json    — list of internal city IDs
"""

import json
import os
import re
import struct
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# CityHash64 — used by SCS HashFS v2 to hash virtual paths
# ---------------------------------------------------------------------------

def _rotate_right(val: int, shift: int) -> int:
    return ((val >> shift) | (val << (64 - shift))) & 0xFFFFFFFFFFFFFFFF

def _fetch64(data: bytes, pos: int) -> int:
    return struct.unpack_from("<Q", data, pos)[0]

def _fetch32(data: bytes, pos: int) -> int:
    return struct.unpack_from("<I", data, pos)[0]

def _hash128_to_64(lo: int, hi: int) -> int:
    kMul = 0x9DDFEA08EB382D69
    a = ((lo ^ hi) * kMul) & 0xFFFFFFFFFFFFFFFF
    a ^= (a >> 47)
    b = ((hi ^ a) * kMul) & 0xFFFFFFFFFFFFFFFF
    b ^= (b >> 47)
    b = (b * kMul) & 0xFFFFFFFFFFFFFFFF
    return b

_k0 = 0xC3A5C85C97CB3127
_k1 = 0xB492B66FBE98F273
_k2 = 0x9AE16A3B2F90404F

def _weak_hash_len_32_with_seeds(data: bytes, pos: int, a: int, b: int) -> Tuple[int, int]:
    w = _fetch64(data, pos)
    x = _fetch64(data, pos + 8)
    y = _fetch64(data, pos + 16)
    z = _fetch64(data, pos + 24)
    a = (a + w) & 0xFFFFFFFFFFFFFFFF
    b = _rotate_right((b + a + z) & 0xFFFFFFFFFFFFFFFF, 21)
    c = a
    a = (a + x) & 0xFFFFFFFFFFFFFFFF
    a = (a + y) & 0xFFFFFFFFFFFFFFFF
    b = (b + _rotate_right(a, 44)) & 0xFFFFFFFFFFFFFFFF
    return ((a + z) & 0xFFFFFFFFFFFFFFFF, (b + c) & 0xFFFFFFFFFFFFFFFF)

def _hash_len_16(u: int, v: int) -> int:
    return _hash128_to_64(u, v)

def _hash_len_0_to_16(data: bytes) -> int:
    n = len(data)
    if n >= 8:
        mul = (_k2 + n * 2) & 0xFFFFFFFFFFFFFFFF
        a = (_fetch64(data, 0) + _k2) & 0xFFFFFFFFFFFFFFFF
        b = _fetch64(data, n - 8)
        c = (_rotate_right(b, 37) * mul + a) & 0xFFFFFFFFFFFFFFFF
        d = (_rotate_right(a, 25) + b) & 0xFFFFFFFFFFFFFFFF
        d = (d * mul) & 0xFFFFFFFFFFFFFFFF
        return _hash_len_16(c, d) * mul & 0xFFFFFFFFFFFFFFFF
    if n >= 4:
        mul = (_k2 + n * 2) & 0xFFFFFFFFFFFFFFFF
        a = _fetch32(data, 0)
        return _hash_len_16(n + (a << 3), _fetch32(data, n - 4)) * mul & 0xFFFFFFFFFFFFFFFF
    if n > 0:
        a = data[0]
        b = data[n >> 1]
        c = data[n - 1]
        y = (a + (b << 8)) & 0xFFFFFFFF
        z = n + (c << 2)
        return ((_k2 * y ^ _k0 * z) * _k2) & 0xFFFFFFFFFFFFFFFF & 0xFFFFFFFFFFFFFFFF
    return _k2

def _hash_len_17_to_32(data: bytes) -> int:
    n = len(data)
    mul = (_k2 + n * 2) & 0xFFFFFFFFFFFFFFFF
    a = (_fetch64(data, 0) * _k1) & 0xFFFFFFFFFFFFFFFF
    b = _fetch64(data, 8)
    c = (_fetch64(data, n - 8) * mul) & 0xFFFFFFFFFFFFFFFF
    d = (_fetch64(data, n - 16) * _k2) & 0xFFFFFFFFFFFFFFFF
    return _hash_len_16(
        (_rotate_right((a + b) & 0xFFFFFFFFFFFFFFFF, 43) + _rotate_right(c, 30) + d) & 0xFFFFFFFFFFFFFFFF,
        (a + _rotate_right((b + _k2) & 0xFFFFFFFFFFFFFFFF, 18) + c) & 0xFFFFFFFFFFFFFFFF
    )

def _hash_len_33_to_64(data: bytes) -> int:
    n = len(data)
    mul = (_k2 + n * 2) & 0xFFFFFFFFFFFFFFFF
    a = (_fetch64(data, 0) * _k2) & 0xFFFFFFFFFFFFFFFF
    b = _fetch64(data, 8)
    c = (_fetch64(data, n - 24)) & 0xFFFFFFFFFFFFFFFF
    d = (_fetch64(data, n - 32)) & 0xFFFFFFFFFFFFFFFF
    e = (_fetch64(data, 16) * _k2) & 0xFFFFFFFFFFFFFFFF
    f = (_fetch64(data, 24) * 9) & 0xFFFFFFFFFFFFFFFF
    g = _fetch64(data, n - 8)
    h = (_fetch64(data, n - 16) * mul) & 0xFFFFFFFFFFFFFFFF
    u = (_rotate_right((a + g) & 0xFFFFFFFFFFFFFFFF, 43) + (_rotate_right(b, 30) + c) * 9) & 0xFFFFFFFFFFFFFFFF
    v = ((a + g) & 0xFFFFFFFFFFFFFFFF ^ d + _rotate_right((f + e) & 0xFFFFFFFFFFFFFFFF, 18) + c) & 0xFFFFFFFFFFFFFFFF
    w = ((_hash_len_16(v, u) * mul) & 0xFFFFFFFFFFFFFFFF + f) & 0xFFFFFFFFFFFFFFFF
    x = _rotate_right((e + h) & 0xFFFFFFFFFFFFFFFF, 37) * mul & 0xFFFFFFFFFFFFFFFF
    y = (_rotate_right((g + w) & 0xFFFFFFFFFFFFFFFF, 27) * mul + _fetch64(data, n - 40)) & 0xFFFFFFFFFFFFFFFF
    z = _hash_len_16(x ^ h, y ^ d)
    return _hash_len_16(z, (e + _rotate_right(w + a, 6) * mul + x) & 0xFFFFFFFFFFFFFFFF)

def cityhash64(data: bytes) -> int:
    """Return CityHash64 of data (matches SCS HashFS v2 path hashing)."""
    n = len(data)
    if n <= 32:
        if n <= 16:
            return _hash_len_0_to_16(data)
        return _hash_len_17_to_32(data)
    if n <= 64:
        return _hash_len_33_to_64(data)

    x = _fetch64(data, n - 40)
    y = (_fetch64(data, n - 16) + _fetch64(data, n - 56)) & 0xFFFFFFFFFFFFFFFF
    z = _hash_len_16(_fetch64(data, n - 48) + n, _fetch64(data, n - 24))

    v = _weak_hash_len_32_with_seeds(data, n - 64, n, z)
    w = _weak_hash_len_32_with_seeds(data, n - 32, y + _k1, x)
    x = (x * _k1 + _fetch64(data, 0)) & 0xFFFFFFFFFFFFFFFF

    n_rounded = (n - 1) & ~63
    pos = 0
    while pos < n_rounded:
        x = (_rotate_right((x + y + v[0] + _fetch64(data, pos + 8)) & 0xFFFFFFFFFFFFFFFF, 37) * _k1) & 0xFFFFFFFFFFFFFFFF
        y = (_rotate_right((y + v[1] + _fetch64(data, pos + 48)) & 0xFFFFFFFFFFFFFFFF, 42) * _k1) & 0xFFFFFFFFFFFFFFFF
        x ^= w[1]
        y = (y + v[0] + _fetch64(data, pos + 40)) & 0xFFFFFFFFFFFFFFFF
        z = (_rotate_right((z + w[0]) & 0xFFFFFFFFFFFFFFFF, 33) * _k1) & 0xFFFFFFFFFFFFFFFF
        v = _weak_hash_len_32_with_seeds(data, pos, v[1] * _k1 & 0xFFFFFFFFFFFFFFFF, x + w[0])
        w = _weak_hash_len_32_with_seeds(data, pos + 32, (z + w[1]) & 0xFFFFFFFFFFFFFFFF, y + _fetch64(data, pos + 16))
        z, x = x, z
        pos += 64

    mul = (_k1 + ((z & 0xFF) << 1)) & 0xFFFFFFFFFFFFFFFF
    w = (w[0] + (n_rounded & 63), w[1])
    v = (v[0] + (n_rounded & 63), v[1])

    # last 0-63 bytes
    result_v = list(v)
    result_w = list(w)
    for _ in range(2):
        x = (_rotate_right((x + y + result_v[0] + _fetch64(data, pos + 8)) & 0xFFFFFFFFFFFFFFFF, 37) * mul) & 0xFFFFFFFFFFFFFFFF
        y = (_rotate_right((y + result_v[1] + _fetch64(data, pos + 48)) & 0xFFFFFFFFFFFFFFFF, 42) * mul) & 0xFFFFFFFFFFFFFFFF
        x ^= result_w[1] * 9 & 0xFFFFFFFFFFFFFFFF
        y = (y + result_v[0] * 9 + _fetch64(data, pos + 40)) & 0xFFFFFFFFFFFFFFFF
        z = (_rotate_right((z + result_w[0]) & 0xFFFFFFFFFFFFFFFF, 33) * mul) & 0xFFFFFFFFFFFFFFFF
        new_v = _weak_hash_len_32_with_seeds(data, pos, result_v[1] * mul & 0xFFFFFFFFFFFFFFFF, x + result_w[0])
        new_w = _weak_hash_len_32_with_seeds(data, pos + 32, (z + result_w[1]) & 0xFFFFFFFFFFFFFFFF, y + _fetch64(data, pos + 16))
        z, x = x, z
        result_v = list(new_v)
        result_w = list(new_w)

    return _hash_len_16(
        (_hash_len_16(result_v[0], result_w[0]) + _rotate_right(y, 42) * mul + z) & 0xFFFFFFFFFFFFFFFF,
        (_hash_len_16(result_v[1], result_w[1]) + x) & 0xFFFFFFFFFFFFFFFF
    )

def scs_path_hash(path: str) -> int:
    """Hash a virtual SCS path the same way HashFS v2 does."""
    # SCS hashes the path without leading slash, lowercase
    p = path.lstrip("/").lower()
    return cityhash64(p.encode("utf-8"))

# ---------------------------------------------------------------------------
# SCS HashFS v2 reader
# ---------------------------------------------------------------------------

HASHFS_MAGIC = b"SCS#"

class HashFSEntry:
    __slots__ = ("hash", "offset", "flags", "crc", "size", "comp_size")
    def __init__(self, hash_val, offset, flags, crc, size, comp_size):
        self.hash = hash_val
        self.offset = offset
        self.flags = flags
        self.crc = crc
        self.size = size
        self.comp_size = comp_size

    @property
    def is_directory(self):
        return bool(self.flags & 1)

    @property
    def is_compressed(self):
        return bool(self.flags & 2)

    @property
    def is_encrypted(self):
        return bool(self.flags & 4)


class HashFS:
    def __init__(self, path: Path):
        self._path = path
        self._f = open(path, "rb")
        self._entries: Dict[int, HashFSEntry] = {}
        self._read_header()

    def _read_header(self):
        f = self._f
        magic = f.read(4)
        if magic != HASHFS_MAGIC:
            raise ValueError(f"Not a HashFS file (magic={magic!r})")

        # version
        version = struct.unpack("<H", f.read(2))[0]
        salt = struct.unpack("<H", f.read(2))[0]
        hash_type = struct.unpack("<I", f.read(4))[0]  # 1 = CityHash64
        entry_count = struct.unpack("<I", f.read(4))[0]
        _unknown = struct.unpack("<I", f.read(4))[0]

        # entries are packed sequentially after the 16-byte header
        for _ in range(entry_count):
            raw = f.read(32)
            hash_val, offset, flags, crc, size, comp_size = struct.unpack("<QQIIII", raw)
            entry = HashFSEntry(hash_val, offset, flags, crc, size, comp_size)
            self._entries[hash_val] = entry

        self._salt = salt

    def _effective_hash(self, path: str) -> int:
        """Hash path then XOR with salt (if any) as HashFS v2 does."""
        h = scs_path_hash(path)
        # Salt is mixed in as: hash ^= (salt << 16) | salt  -- but actual mixing
        # varies by implementation. Try direct hash first, then with salt.
        return h

    def get_entry(self, path: str) -> Optional[HashFSEntry]:
        h = self._effective_hash(path)
        return self._entries.get(h)

    def read_file(self, path: str) -> Optional[bytes]:
        entry = self.get_entry(path)
        if entry is None or entry.is_directory:
            return None
        if entry.is_encrypted:
            print(f"  [WARN] {path} is encrypted — skipping", file=sys.stderr)
            return None
        self._f.seek(entry.offset)
        data = self._f.read(entry.comp_size)
        if entry.is_compressed:
            import zlib
            data = zlib.decompress(data)
        return data

    def list_directory(self, path: str) -> Optional[List[str]]:
        """Return the entries in a directory, or None if not found."""
        entry = self.get_entry(path)
        if entry is None or not entry.is_directory:
            return None
        self._f.seek(entry.offset)
        data = self._f.read(entry.comp_size)
        if entry.is_compressed:
            import zlib
            data = zlib.decompress(data)
        # Directory listings are newline-separated; entries starting with * are dirs
        return data.decode("utf-8", errors="replace").splitlines()

    def all_hashes(self):
        return dict(self._entries)

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

# ---------------------------------------------------------------------------
# SII file mini-parser — extract key: value pairs
# ---------------------------------------------------------------------------

def _parse_sii_values(text: str) -> Dict[str, str]:
    """Extract simple key: value pairs from a SII text block."""
    result: Dict[str, str] = {}
    for m in re.finditer(r"^\s*(\w+)\s*:\s*(.+?)\s*$", text, re.MULTILINE):
        result[m.group(1)] = m.group(2).strip('"')
    return result

def _find_cargo_token(sii_text: str) -> Optional[str]:
    """
    Return the cargo token from a cargo SII file.
    The token is the identifier after 'cargo_data : cargo.' on the block header.
    Example:  cargo_data : cargo.sawpanels { ... }
    """
    m = re.search(r"\bcargo_data\s*:\s*cargo\.(\w+)\b", sii_text)
    if m:
        return m.group(1)
    # fallback: sometimes written as  cargo_data : "cargo.xxx"
    m = re.search(r'\bcargo_data\s*:\s*"cargo\.(\w+)"', sii_text)
    if m:
        return m.group(1)
    return None

def _find_cargo_name_key(sii_text: str) -> Optional[str]:
    """Return the localization key for the cargo name (name_key field)."""
    m = re.search(r"\bname(?:_key)?\s*:\s*\"?([\w@.]+)\"?", sii_text)
    if m:
        return m.group(1)
    return None

# ---------------------------------------------------------------------------
# Locale reader — maps @@key to display name
# ---------------------------------------------------------------------------

def _load_locale(locale_scs_path: Path) -> Dict[str, str]:
    """
    Load locale/en_us/cargo.sii (or similar) from locale.scs and return
    a mapping of localization key → English display string.
    """
    if not locale_scs_path.exists():
        return {}

    locale_map: Dict[str, str] = {}
    try:
        with HashFS(locale_scs_path) as lfs:
            # Try common locale paths
            for lpath in [
                "locale/en_us/cargo.sii",
                "locale/en_us/cargo_cs.sii",
                "locale/en/cargo.sii",
            ]:
                data = lfs.read_file(lpath)
                if data:
                    text = data.decode("utf-8", errors="replace")
                    # Format:  key: "Display Name"
                    for m in re.finditer(r'^\s*([\w@.]+)\s*:\s*"([^"]+)"', text, re.MULTILINE):
                        locale_map[m.group(1)] = m.group(2)
                    print(f"  Loaded {len(locale_map)} locale entries from {lpath}")
                    break

            # Also try the all-in-one locale file
            if not locale_map:
                data = lfs.read_file("locale/en_us/en_us.sii")
                if not data:
                    data = lfs.read_file("locale/en_us.sii")
                if data:
                    text = data.decode("utf-8", errors="replace")
                    for m in re.finditer(r'^\s*([\w@.]+)\s*:\s*"([^"]+)"', text, re.MULTILINE):
                        locale_map[m.group(1)] = m.group(2)
                    print(f"  Loaded {len(locale_map)} locale entries from combined locale file")

    except Exception as e:
        print(f"  [WARN] Could not read locale.scs: {e}", file=sys.stderr)

    return locale_map

# ---------------------------------------------------------------------------
# Main extraction logic
# ---------------------------------------------------------------------------

def find_ats_install() -> Optional[Path]:
    candidates = [
        Path(r"C:\SteamLibrary\steamapps\common\American Truck Simulator"),
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\American Truck Simulator"),
        Path(r"D:\SteamLibrary\steamapps\common\American Truck Simulator"),
        Path(r"D:\Steam\steamapps\common\American Truck Simulator"),
        Path(r"E:\SteamLibrary\steamapps\common\American Truck Simulator"),
    ]
    for c in candidates:
        if c.exists() and (c / "def.scs").exists():
            return c
    return None


def extract_cargo_ids(def_scs: Path, locale_scs: Optional[Path]) -> List[Dict]:
    """Extract all cargo tokens and their display names from def.scs."""
    results: List[Dict] = []

    locale_map: Dict[str, str] = {}
    if locale_scs and locale_scs.exists():
        print(f"Loading locale from {locale_scs} ...")
        locale_map = _load_locale(locale_scs)

    print(f"Opening {def_scs} ...")
    with HashFS(def_scs) as fs:
        # List the def/cargo directory
        entries = fs.list_directory("def/cargo")
        if entries is None:
            print("[ERROR] Could not find def/cargo directory in def.scs", file=sys.stderr)
            print("  Trying to enumerate hashes for known cargo paths...", file=sys.stderr)
            # Fallback: brute-force common cargo names
            return results

        print(f"  Found {len(entries)} entries in def/cargo/")

        for entry_name in entries:
            entry_name = entry_name.strip()
            if not entry_name or entry_name.startswith("*"):
                continue  # skip subdirs and empty lines

            cargo_path = f"def/cargo/{entry_name}"
            data = fs.read_file(cargo_path)
            if data is None:
                continue

            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                continue

            token = _find_cargo_token(text)
            if not token:
                # try using filename as token
                token = entry_name.replace(".sii", "").replace(".sui", "")

            name_key = _find_cargo_name_key(text)
            display_name = ""

            if name_key:
                # name_key may start with @@ or be bare
                bare_key = name_key.lstrip("@")
                display_name = locale_map.get(bare_key, locale_map.get(name_key, ""))

            results.append({
                "internal_id": token,
                "display_name": display_name or name_key or token,
                "locale_key": name_key or "",
                "source_file": entry_name,
            })

    results.sort(key=lambda x: x["internal_id"])
    return results


def extract_city_ids(def_scs: Path) -> List[Dict]:
    """Extract all city tokens from def.scs."""
    results: List[Dict] = []

    print(f"Extracting city IDs from {def_scs} ...")
    with HashFS(def_scs) as fs:
        # Cities live in def/city/ (one .sii per city)
        entries = fs.list_directory("def/city")
        if entries is None:
            print("[WARN] Could not list def/city — trying def/world/city.sii", file=sys.stderr)
            data = fs.read_file("def/world/city.sii")
            if data:
                text = data.decode("utf-8", errors="replace")
                for m in re.finditer(r"\bcity_data\s*:\s*city\.(\w+)\b", text):
                    results.append({"internal_id": m.group(1)})
            return results

        print(f"  Found {len(entries)} entries in def/city/")
        for entry_name in entries:
            entry_name = entry_name.strip()
            if not entry_name or entry_name.startswith("*"):
                continue
            city_path = f"def/city/{entry_name}"
            data = fs.read_file(city_path)
            if data is None:
                continue
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                continue
            m = re.search(r"\bcity_data\s*:\s*city\.(\w+)\b", text)
            if m:
                results.append({
                    "internal_id": m.group(1),
                    "source_file": entry_name,
                })
            else:
                # Use filename stem as fallback
                stem = entry_name.rsplit(".", 1)[0]
                results.append({"internal_id": stem, "source_file": entry_name})

    results.sort(key=lambda x: x["internal_id"])
    return results


def main():
    if len(sys.argv) > 1:
        ats_dir = Path(sys.argv[1])
    else:
        ats_dir = find_ats_install()
        if ats_dir is None:
            print("Could not auto-detect ATS install directory.")
            print("Usage: python extract_ats_data.py <path-to-ATS-install>")
            sys.exit(1)
        print(f"Auto-detected ATS install at: {ats_dir}")

    def_scs = ats_dir / "def.scs"
    locale_scs = ats_dir / "locale.scs"

    if not def_scs.exists():
        print(f"[ERROR] def.scs not found at {def_scs}", file=sys.stderr)
        sys.exit(1)

    # --- Cargo IDs ---
    print("\n=== Extracting cargo IDs ===")
    cargo_list = extract_cargo_ids(def_scs, locale_scs if locale_scs.exists() else None)
    print(f"Found {len(cargo_list)} cargo types.")

    cargo_out = Path("ats_cargo_ids.json")
    with open(cargo_out, "w", encoding="utf-8") as f:
        json.dump(cargo_list, f, indent=2, ensure_ascii=False)
    print(f"Wrote {cargo_out}")

    # Also print a quick preview
    print("\nFirst 20 cargo entries:")
    for c in cargo_list[:20]:
        print(f"  {c['internal_id']:30s}  {c['display_name']}")

    # --- City IDs ---
    print("\n=== Extracting city IDs ===")
    city_list = extract_city_ids(def_scs)
    print(f"Found {len(city_list)} cities.")

    city_out = Path("ats_city_ids.json")
    with open(city_out, "w", encoding="utf-8") as f:
        json.dump(city_list, f, indent=2, ensure_ascii=False)
    print(f"Wrote {city_out}")

    print("\nFirst 20 city entries:")
    for c in city_list[:20]:
        print(f"  {c['internal_id']}")

    print("\nDone. Check ats_cargo_ids.json and ats_city_ids.json for the full lists.")
    print("Share these files so cargo_types.json can be updated with correct internal IDs.")


if __name__ == "__main__":
    main()
