#!/usr/bin/env python3
"""
ATS Data Extractor — scans def.scs for cargo and city SII definitions.

This version uses a raw byte scan instead of relying on the exact HashFS
entry structure — it searches the file directly for SII blocks containing
cargo_data and city_data tokens. Works regardless of compression or layout.

Usage:
    python extract_ats_data.py [path-to-ATS-install-dir]

Output (written to current directory):
    ats_cargo_ids.json   — internal_id + display_name for every cargo type
    ats_city_ids.json    — internal_id for every city
"""

import json
import os
import re
import struct
import sys
import zlib
from pathlib import Path
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# HashFS v2 reader — 20-byte header, entries are a hash table (many empty)
# ---------------------------------------------------------------------------
# Header (20 bytes): magic(4) version(2) salt(2) hash_method(4)
#                    entry_count(4) start_offset(4)
# We try TWO entry sizes (28 and 32 bytes) and pick whichever gives
# sane offset values.
# ---------------------------------------------------------------------------

HASHFS_MAGIC = b"SCS#"

FLAG_COMPRESSED = 2
FLAG_ENCRYPTED  = 4


def _read_hashfs_entries(path: Path):
    """
    Return list of (offset, comp_size, size, flags) for all non-empty entries.
    Tries entry sizes 28 and 32; picks the one that gives the most valid offsets.
    """
    file_size = path.stat().st_size
    entries = []

    with open(path, "rb") as f:
        header = f.read(20)
        magic = header[:4]
        if magic != HASHFS_MAGIC:
            raise ValueError(f"Not a HashFS file: {magic!r}")

        # version(2) salt(2) hash_method(4) entry_count(4) start_offset(4)
        _ver, _salt, _hm, entry_count, _start = struct.unpack_from("<HHIII", header, 4)
        print(f"  HashFS v2: {entry_count:,} hash table slots, table at offset {_start}")

        # Read entire entry table — try 28-byte and 32-byte entry sizes
        # 28-byte: hash(8) offset(4) flags(4) crc(4) comp_size(4) size(4)
        # 32-byte: hash(8) offset(8) flags(4) crc(4) size(4)       comp(4)
        best_entries = []
        best_valid = -1

        for entry_size, fmt in [
            (28, "<Q I I I I I"),   # hash, offset(4), flags, crc, comp_size, size
            (32, "<Q Q I I I I"),   # hash, offset(8), flags, crc, size, comp_size
        ]:
            table_bytes = entry_count * entry_size
            f.seek(_start)
            table_data = f.read(table_bytes)
            candidate = []
            valid = 0
            fmt_struct = struct.Struct(fmt)
            for i in range(entry_count):
                raw = table_data[i * entry_size : i * entry_size + entry_size]
                if len(raw) < entry_size:
                    break
                fields = fmt_struct.unpack(raw)
                h = fields[0]
                if h == 0:
                    continue  # empty hash table slot
                offset  = fields[1]
                flags   = fields[2]
                # For 28-byte: comp_size=fields[4], size=fields[5]
                # For 32-byte: size=fields[4],      comp_size=fields[5]
                if entry_size == 28:
                    comp_size = fields[4]
                    size      = fields[5]
                else:
                    size      = fields[4]
                    comp_size = fields[5]
                read_size = comp_size if comp_size > 0 else size
                ok = (0 < offset < file_size) and (read_size <= file_size) and (size <= 100_000_000)
                if ok:
                    valid += 1
                    candidate.append((offset, comp_size, size, flags))
            print(f"  Entry size {entry_size}: {valid} valid non-empty entries")
            if valid > best_valid:
                best_valid = valid
                best_entries = candidate

    return best_entries


# ---------------------------------------------------------------------------
# Extract text content from all readable entries
# ---------------------------------------------------------------------------

# zlib magic: byte 0x78 followed by one of these second bytes
_ZLIB_SECOND = frozenset((0x01, 0x5E, 0x9C, 0xDA))


def _scan_data_section_zlib(path: Path, keyword: str) -> List[str]:
    """
    Modern ATS def.scs stores SII files as inline zlib streams packed into a
    data section that begins at start_offset (from the HashFS header).  Scan
    that section for every 0x78 zlib magic byte, decompress, and return text
    blocks that contain keyword.  This sidesteps hash-table parsing entirely.
    """
    with open(path, "rb") as f:
        header = f.read(32)
        if header[:4] != HASHFS_MAGIC:
            raise ValueError("Not a HashFS file")
        start_offset = struct.unpack_from("<I", header, 16)[0]
        print(f"  Data section at offset {start_offset:,}")
        f.seek(start_offset)
        blob = f.read()          # entire data section into memory

    kw = keyword.encode("utf-8")
    results: List[str] = []
    pos = 0
    attempts = 0
    n = len(blob)
    while pos < n - 1:
        if blob[pos] == 0x78 and blob[pos + 1] in _ZLIB_SECOND:
            attempts += 1
            try:
                dec = zlib.decompress(blob[pos:])
                if kw in dec:
                    results.append(dec.decode("utf-8", errors="replace"))
            except zlib.error:
                pass
        pos += 1

    print(f"  Tried {attempts:,} zlib positions → {len(results)} matching '{keyword}'")
    return results


def _read_all_text(path: Path, keyword: str) -> List[str]:
    """Return text of every SCS entry whose decompressed content contains keyword."""
    # Primary: zlib stream scan of the data section (works for modern ATS def.scs)
    try:
        results = _scan_data_section_zlib(path, keyword)
        if results:
            return results
        print(f"  Zlib scan found 0 — trying legacy HashFS entry parse")
    except Exception as e:
        print(f"  [WARN] Zlib scan failed: {e} — trying legacy HashFS entry parse")

    # Legacy: hash-table entry parsing (older SCS file versions)
    try:
        entries = _read_hashfs_entries(path)
    except Exception as e:
        print(f"  [WARN] HashFS parsing failed: {e}")
        return _fallback_raw_scan(path, keyword)

    results = []
    file_size = path.stat().st_size
    with open(path, "rb") as f:
        for offset, comp_size, size, flags in entries:
            if flags & FLAG_ENCRYPTED:
                continue
            read_size = comp_size if comp_size > 0 else size
            if read_size == 0 or read_size > 5_000_000:
                continue
            if offset + read_size > file_size:
                continue
            try:
                f.seek(offset)
                data = f.read(read_size)
            except OSError:
                continue
            if (flags & FLAG_COMPRESSED) or (comp_size > 0 and comp_size != size):
                try:
                    data = zlib.decompress(data)
                except zlib.error:
                    try:
                        data = zlib.decompress(data, -15)
                    except zlib.error:
                        continue
            try:
                text = data.decode("utf-8", errors="strict")
            except (UnicodeDecodeError, AttributeError):
                continue
            if keyword in text:
                results.append(text)

    if not results:
        print(f"  HashFS scan found 0 matches — falling back to raw scan")
        return _fallback_raw_scan(path, keyword)

    print(f"  Found {len(results)} entries matching '{keyword}'")
    return results


def _fallback_raw_scan(path: Path, keyword: str) -> List[str]:
    """
    Fallback: decompress the whole file as a zlib stream, or scan raw bytes,
    looking for keyword occurrences and extracting surrounding text blocks.
    """
    print(f"  Raw-scanning {path.name} for '{keyword}' ...")
    results = []
    kw_bytes = keyword.encode()

    with open(path, "rb") as f:
        data = f.read()

    # Try zlib decompression of everything past the header
    for start in (20, 0):
        try:
            decompressed = zlib.decompress(data[start:])
            if kw_bytes in decompressed:
                text = decompressed.decode("utf-8", errors="replace")
                # Split on SiiNunit block boundaries
                for block in re.split(r'(?=\b(?:cargo|city)_data\s*:)', text):
                    if keyword in block:
                        results.append(block)
                if results:
                    print(f"  Raw zlib scan found {len(results)} blocks")
                    return results
        except zlib.error:
            pass

    # Last resort: find keyword in raw bytes and extract surrounding context
    pos = 0
    while True:
        idx = data.find(kw_bytes, pos)
        if idx < 0:
            break
        start = max(0, idx - 200)
        end   = min(len(data), idx + 2000)
        chunk = data[start:end]
        try:
            results.append(chunk.decode("utf-8", errors="replace"))
        except Exception:
            pass
        pos = idx + 1

    print(f"  Raw byte scan found {len(results)} occurrences of '{keyword}'")
    return results


# ---------------------------------------------------------------------------
# SII parsers
# ---------------------------------------------------------------------------

def _cargo_token(text: str) -> Optional[str]:
    m = re.search(r'\bcargo_data\s*:\s*(?:")?cargo\.(\w+)', text)
    return m.group(1) if m else None

def _cargo_name_key(text: str) -> Optional[str]:
    m = re.search(r'\bname(?:_key)?\s*:\s*"?([\w@.]+)"?', text)
    return m.group(1) if m else None

def _city_token(text: str) -> Optional[str]:
    m = re.search(r'\bcity_data\s*:\s*(?:")?city\.(\w+)', text)
    return m.group(1) if m else None

def _city_pos(text: str):
    """Return (pos_x, pos_z) floats from a city_data block, or (None, None) if absent."""
    m = re.search(
        r'\bpos\s*:\s*\(\s*([-\d.eE+]+)\s*,\s*[-\d.eE+]+\s*,\s*([-\d.eE+]+)\s*\)',
        text,
    )
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


# ---------------------------------------------------------------------------
# Locale loading
# ---------------------------------------------------------------------------

def _load_locale(locale_scs: Path) -> Dict[str, str]:
    locale_map: Dict[str, str] = {}
    if not locale_scs.exists():
        return locale_map
    print(f"  Scanning locale.scs ...")
    try:
        # locale.scs uses the same inline-zlib format; scan for "cn_" keys
        texts = _scan_data_section_zlib(locale_scs, "cn_")
        if not texts:
            texts = _read_all_text(locale_scs, "cn_")
    except Exception as e:
        print(f"  [WARN] locale.scs failed: {e}")
        return locale_map
    for text in texts:
        for m in re.finditer(r'^\s*([\w@.]+)\s*:\s*"([^"]+)"', text, re.MULTILINE):
            locale_map[m.group(1).lstrip("@")] = m.group(2)
    print(f"  Loaded {len(locale_map)} locale strings.")
    return locale_map


# ---------------------------------------------------------------------------
# Install finder
# ---------------------------------------------------------------------------

def find_ats_install() -> Optional[Path]:
    for p in [
        r"C:\SteamLibrary\steamapps\common\American Truck Simulator",
        r"C:\Program Files (x86)\Steam\steamapps\common\American Truck Simulator",
        r"D:\SteamLibrary\steamapps\common\American Truck Simulator",
        r"D:\Steam\steamapps\common\American Truck Simulator",
        r"E:\SteamLibrary\steamapps\common\American Truck Simulator",
    ]:
        q = Path(p)
        if q.exists() and (q / "def.scs").exists():
            return q
    return None


# ---------------------------------------------------------------------------
# State .scs files — each state's city data lives in its own archive
# ---------------------------------------------------------------------------

# Cargo and base-game cities (CA) live in def.scs.
# Each state DLC bundles its own city_data SII files.
# Newer DLCs (MO/IA/LA) are included but silently skipped if absent.
STATE_SCS_FILES = [
    "def.scs",          # California (base game)
    "dlc_nevada.scs",   # Nevada
    "dlc_arizona.scs",  # Arizona
    "dlc_nm.scs",       # New Mexico
    "dlc_or.scs",       # Oregon
    "dlc_wa.scs",       # Washington
    "dlc_ut.scs",       # Utah
    "dlc_id.scs",       # Idaho
    "dlc_co.scs",       # Colorado
    "dlc_wy.scs",       # Wyoming
    "dlc_mt.scs",       # Montana
    "dlc_tx.scs",       # Texas
    "dlc_ok.scs",       # Oklahoma
    "dlc_ks.scs",       # Kansas
    "dlc_ne.scs",       # Nebraska
    "dlc_ar.scs",       # Arkansas
    "dlc_mo.scs",       # Missouri  (may not exist yet)
    "dlc_ia.scs",       # Iowa      (may not exist yet)
    "dlc_la.scs",       # Louisiana (may not exist yet)
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) > 1:
        ats_dir = Path(sys.argv[1])
    else:
        ats_dir = find_ats_install()
        if ats_dir is None:
            print("ERROR: Cannot find ATS. Pass install path as argument.")
            sys.exit(1)
        print(f"Found ATS at: {ats_dir}")

    def_scs    = ats_dir / "def.scs"
    locale_scs = ats_dir / "locale.scs"

    if not def_scs.exists():
        print(f"ERROR: {def_scs} not found")
        sys.exit(1)

    # ── Locale ─────────────────────────────────────────────────────────────
    locale_map: Dict[str, str] = {}
    if locale_scs.exists():
        print(f"\nLoading locale ...")
        locale_map = _load_locale(locale_scs)

    # ── Cargo (only in def.scs) ─────────────────────────────────────────────
    print(f"\n=== Extracting cargo IDs ===")
    print(f"Scanning {def_scs.name} ...")
    cargo_texts = _read_all_text(def_scs, "cargo_data")

    cargo_list = []
    seen: set = set()
    for text in cargo_texts:
        token = _cargo_token(text)
        if not token or token in seen:
            continue
        seen.add(token)
        name_key = _cargo_name_key(text)
        display = ""
        if name_key:
            bare = name_key.lstrip("@")
            display = locale_map.get(bare, locale_map.get(name_key, ""))
        cargo_list.append({
            "internal_id": token,
            "display_name": display or name_key or token,
            "locale_key": name_key or "",
        })
    cargo_list.sort(key=lambda x: x["internal_id"])

    print(f"Found {len(cargo_list)} cargo types.")
    out = Path("ats_cargo_ids.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cargo_list, f, indent=2, ensure_ascii=False)
    print(f"Wrote {out.resolve()}")
    print("\nFirst 30:")
    for c in cargo_list[:30]:
        print(f"  {c['internal_id']:35s}  {c['display_name']}")

    # ── Cities (def.scs + every state DLC) ─────────────────────────────────
    print(f"\n=== Extracting city IDs ===")
    city_list = []
    seen = set()
    no_pos: List[str] = []

    for scs_name in STATE_SCS_FILES:
        scs_path = ats_dir / scs_name
        if not scs_path.exists():
            continue
        print(f"Scanning {scs_name} ...")
        try:
            city_texts = _read_all_text(scs_path, "city_data")
        except Exception as e:
            print(f"  [WARN] {scs_name}: {e}")
            continue
        file_new = 0
        for text in city_texts:
            token = _city_token(text)
            if token and token not in seen:
                seen.add(token)
                pos_x, pos_z = _city_pos(text)
                entry: Dict[str, object] = {"internal_id": token}
                if pos_x is not None:
                    entry["pos_x"] = round(pos_x, 1)
                    entry["pos_z"] = round(pos_z, 1)
                else:
                    no_pos.append(token)
                city_list.append(entry)
                file_new += 1
        print(f"  → {file_new} new cities (running total: {len(city_list)})")

    city_list.sort(key=lambda x: x["internal_id"])

    has_coords = sum(1 for c in city_list if "pos_x" in c)
    print(f"\nTotal: {len(city_list)} cities ({has_coords} with pos coordinates).")
    if no_pos:
        print(f"  Cities missing pos ({len(no_pos)}): {', '.join(sorted(no_pos))}")
    out = Path("ats_city_ids.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(city_list, f, indent=2, ensure_ascii=False)
    print(f"Wrote {out.resolve()}")
    print("\nFirst 30:")
    for c in city_list[:30]:
        coord = f"  pos=({c['pos_x']}, {c['pos_z']})" if "pos_x" in c else "  [no pos]"
        print(f"  {c['internal_id']:35s}{coord}")

    print("\nDone! Share ats_cargo_ids.json and ats_city_ids.json.")


if __name__ == "__main__":
    main()
