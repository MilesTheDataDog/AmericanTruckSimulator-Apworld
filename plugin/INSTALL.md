# ATS Archipelago Plugin — Build & Install Guide

## What This Is

`ats_archipelago.dll` is a C++ plugin loaded by American Truck Simulator via the
official SCS SDK plugin system. It detects game events (deliveries, city arrivals,
level-ups) and enforces Archipelago rules (locked states, truck restrictions).

---

## Pre-built Release (Recommended)

If a pre-built DLL is available in the GitHub Releases tab:

1. Download `ats_archipelago.dll` from the latest release.
2. Copy it to:
   ```
   <Steam>\steamapps\common\American Truck Simulator\bin\win_x64\plugins\
   ```
   Create the `plugins\` folder if it does not exist.
3. Launch ATS — no further configuration needed.

---

## Building From Source

### Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| CMake | 3.20+ | https://cmake.org/download/ |
| Visual Studio | 2022 (Community OK) | With "Desktop development with C++" workload |
| SCS SDK | Latest | See below |
| nlohmann/json | 3.11+ | See below |

### Step 1 — Download the SCS SDK

1. Go to: https://modding.scssoft.com/wiki/SDK
2. Download the SDK archive.
3. Extract it so the file `include/scssdk_telemetry.h` exists.
4. Copy or move the contents to:
   ```
   plugin/vendor/scs_sdk/
   ```
   Final structure:
   ```
   plugin/vendor/scs_sdk/include/scssdk_telemetry.h
   plugin/vendor/scs_sdk/include/amtrucks/scssdk_telemetry_ats.h
   plugin/vendor/scs_sdk/include/eurotrucks2/scssdk_eut2.h
   ...
   ```

### Step 2 — Download nlohmann/json

1. Go to: https://github.com/nlohmann/json/releases/latest
2. Download `json.hpp` (single-header file).
3. Place it at:
   ```
   plugin/vendor/nlohmann/json.hpp
   ```

### Step 3 — Configure and Build

Open a **x64 Native Tools Command Prompt for VS 2022**, navigate to the
`plugin/` directory, and run:

```bat
cmake -B build -A x64
cmake --build build --config Release
```

The output DLL will be at:
```
plugin/build/Release/ats_archipelago.dll
```

### Step 4 — Install

**Option A — CMake install:**
```bat
cmake --install build --prefix "C:\Program Files (x86)\Steam\steamapps\common\American Truck Simulator"
```

**Option B — Manual copy:**
Copy `plugin/build/Release/ats_archipelago.dll` to:
```
<ATS install folder>\bin\win_x64\plugins\ats_archipelago.dll
```

---

## Verifying the Plugin Loaded

After installing and launching ATS:

1. Open the ATS game log at:
   ```
   %USERPROFILE%\Documents\American Truck Simulator\game.log.txt
   ```
2. Search for `[ATS-AP]` — you should see:
   ```
   [ATS-AP] Archipelago plugin initializing v1.0.0
   [ATS-AP] Communication folder: C:\Users\<you>\Documents\American Truck Simulator\archipelago
   [ATS-AP] Archipelago plugin initialized. Waiting for Python client...
   ```

---

## Communication Files

The plugin creates and uses these files in:
```
%USERPROFILE%\Documents\American Truck Simulator\archipelago\
```

| File | Written by | Read by | Purpose |
|------|-----------|---------|---------|
| `events.json` | Plugin (C++) | Client (Python) | Game events: deliveries, city visits, level-ups |
| `items.json` | Client (Python) | Plugin (C++) | Unlocked states, trucks, garages, offices |
| `slot_data.json` | Client (Python) | Plugin + Lua mod | Player options from Archipelago server |

---

## Troubleshooting

**Plugin not loading:**
- Confirm the DLL is in `bin\win_x64\plugins\` (not `bin\win_x64\` directly).
- Check `game.log.txt` for errors.
- Ensure ATS is fully updated (plugin targets latest SDK version).

**Events not appearing:**
- Confirm the Python client (`ATSClient.py`) is running and connected.
- Confirm the `archipelago\` folder exists in your ATS documents folder.

**State enforcement not working:**
- The state bounding boxes in the plugin source are approximate placeholders.
  They must be calibrated using in-game coordinate measurement (use the ATS
  console or a position-display mod to read X/Z values at state borders).
  Once calibrated, update `STATE_BOUNDS` in `ats_archipelago.cpp` and rebuild.

**Money/XP/city addresses failed to verify after a game update:**
- `game.log.txt` will show `ADDR money FAIL@ ...` (or xp/city) with the bytes
  found at the old address. This means the game update moved the instructions.
- Items still arrive: the AP client automatically writes pending money/XP into
  your save file whenever the game is closed, and city checks fall back to
  save-file polling.
- To restore instant live injection, find the new instruction addresses
  (Cheat Engine: "money increase" / "XP write" / "visited-city count write")
  and create `Documents\American Truck Simulator\archipelago\addresses.json`:

  ```json
  {
    "money_rva": "0x76DA69",
    "xp_rva":    "0x41FE66",
    "city_rva":  "0x41A9DF"
  }
  ```

  Values are RVAs (absolute address minus the `amtrucks.exe base=` value the
  plugin logs at startup), as hex strings. No DLL rebuild needed — restart ATS.
