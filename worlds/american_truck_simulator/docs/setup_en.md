# American Truck Simulator — Setup Guide

This guide walks you through everything you need to play American Truck Simulator
(ATS) in an Archipelago multiworld. Follow the steps in order. You only have to
do the "One-Time Setup" once; after that, joining a game takes about a minute.

If a step uses a word you don't recognize, check the **Glossary** at the bottom.

---

## What you need

- **American Truck Simulator** installed (Steam version recommended).
- **Archipelago** installed on your computer. Get it from
  <https://github.com/ArchipelagoMW/Archipelago/releases> (download the
  "Setup" file and run it).
- Three files for ATS (your host or the release page provides these):
  1. `american_truck_simulator.apworld` — the game logic for Archipelago.
  2. `ats_archipelago.dll` — the plugin that talks to the game.
  3. `ATSClient` — the program that connects you to the multiworld.

> **Good to know:** DLC is optional. The base game (California + Nevada) works on
> its own. Arizona is free DLC and safe to include. Only turn on the DLC states
> you actually own (see **Settings** below).

---

## One-Time Setup

You only need to do this once. It has three small parts.

### 1. Install the ATS world into Archipelago

- Find the file `american_truck_simulator.apworld`.
- **Double-click it.** Archipelago will install it automatically.
- If double-clicking does nothing, open the **Archipelago Launcher**, click
  **"Install APWorld,"** and pick the file.

### 2. Install the game plugin (the DLL)

The plugin is what lets Archipelago give you money, XP, and track your progress.

- Copy `ats_archipelago.dll` into this folder:

  ```
  <your Steam folder>\steamapps\common\American Truck Simulator\bin\win_x64\plugins\
  ```

- If the `plugins` folder does not exist, **create it** (spelled exactly
  `plugins`).
- That's it. You do **not** need to configure anything.

> **Not sure where your game folder is?** In Steam, right-click American Truck
> Simulator → **Manage → Browse local files.** That opens the game folder. From
> there, go into `bin` → `win_x64`, and make the `plugins` folder there.

### 3. Get the client ready

The **client** is the little program that connects you to the multiworld server.

- If you were given `ATSClient.exe`, just remember where it is (a common spot is
  `C:\ProgramData\Archipelago\ATSClient.exe`).
- You can also launch it from the **Archipelago Launcher**: open the Launcher and
  click **"American Truck Simulator Client."**

You're done with setup!

---

## Create Your Settings (YAML)

Before you can join a game, you tell Archipelago how you want to play. This is
done in a settings file called a **YAML**.

1. Find the file **`American Truck Simulator.yaml`** (it comes with the ATS
   files). Open it in any text editor (Notepad is fine).
2. Change **`name:`** to your player name (no spaces, up to 16 characters).
3. Look through the options and set them how you like. Every option has a comment
   above it explaining what it does. If you're unsure, the defaults are fine.
4. **Important:** only list the DLC states you actually own under `enabled_dlc`.
   Delete any you don't own. California and Nevada are always included — don't add
   them.
5. Save the file.

Not sure your YAML is valid? Paste it into <https://archipelago.gg/check> to
check it before generating.

A quick tour of the options is in the **Settings Explained** section below.

---

## Join a Game

There are two common ways to play.

### A) Someone else is hosting

1. Send your finished `American Truck Simulator.yaml` to whoever is generating
   the game.
2. They give you a **server address** (like `archipelago.gg:12345`) and, if used,
   a password.
3. Skip to **How to Play** below.

### B) You are hosting (or playing solo)

1. Put all players' YAML files (including yours) into Archipelago's `Players`
   folder.
2. Open the **Archipelago Launcher** and click **"Generate."**
3. Upload the generated file to <https://archipelago.gg/uploads> to host it
   online, or host it locally with **"Host."**
4. Note the server address it gives you.

---

## How to Play

1. **Open the ATS client.** Use the Archipelago Launcher → **"American Truck
   Simulator Client,"** or run `ATSClient.exe`.
2. **Connect.** In the box at the top of the client, type the server address
   (for example `archipelago.gg:12345`) and press Enter. If asked, type your
   player name, and a password if the game uses one.
3. **The game launches automatically.** The client starts American Truck
   Simulator through Steam for you. (If it doesn't, just start ATS yourself.)
4. **Load your save and drive!** Play normally — take jobs, deliver cargo, visit
   cities. As you complete checks, other players get items, and you'll receive
   money, XP, and other rewards from them.

Keep the client window open while you play. That's what sends and receives items.

> **Tip:** Type `/help` in the client to see commands. `/status` shows how your
> game is doing (money, level, goal progress, and more).

---

## Settings Explained (in plain words)

| Setting | What it does |
|---|---|
| **win_condition** | How you win: reach a level, reach a money amount, either, or both. |
| **goal_level** | The driver level you need to reach (if your win uses levels). |
| **goal_money** | The money you need to reach, in thousands (1000 = $1,000,000). |
| **enabled_dlc** | The DLC map states you own and want to include. |
| **level_milestone_checks** | Reaching levels 5, 10, 15, 20, 25, 30 each count as a check. |
| **cargo_delivery_checks** | The first time you deliver each cargo type is a check. |
| **city_arrival_checks** | The first time you arrive in each city is a check. |
| **state_arrival_checks** | The first time you enter each DLC state is a check. |
| **state_unlocks** | Optional. See **State Unlocks** below. |
| **trap_percentage** | Chance that a filler item is a "trap" (a fine that costs you money). 0 = no traps. |
| **death_link** | Optional. See **Death Link** below. |
| **death_link_penalty** | If Death Link is on, how much a received death costs (percent of your money). |

### State Unlocks (optional)

If you turn this on, each DLC state must be **unlocked** before its city and
state checks count. You get an "Unlock \<State\>" item from the multiworld, and
when it arrives, that state's checks are sent.

**You are never blocked from driving anywhere.** You can drive into any state and
deliver there at any time. The only difference is that the *checks* for a locked
state are **held** by the client and sent the moment its unlock item arrives. This
turns your states into real progression, so late-game unlocks feel rewarding.

California and Nevada are always unlocked. This option does nothing unless city
and/or state arrival checks are also turned on.

### Death Link (optional)

If you turn this on, wrecking your truck (engine damage reaching 90%, basically
undriveable) sends a "death" to every other Death Link player in the game. When
one of **them** dies, you pay an **emergency towing fee** — a percentage of your
current money, set by `death_link_penalty`. Your balance never drops below $0.

---

## Troubleshooting

**The client says it can't find my save / no city checks happen.**
- Make sure you have played and **saved** in ATS at least once, so a save file
  exists.
- Name your ATS in-game profile the same as your player name — it helps the
  client find your save quickly.

**I'm not receiving money or XP in the game.**
- Make sure the plugin DLL is in the `plugins` folder (Step 2 of setup) and that
  ATS is actually running.
- After a game update, money/XP may pause for a moment while the plugin
  re-locates them. It fixes itself automatically, usually after your next
  delivery. Anything owed to you is saved and delivered — nothing is lost.
- You can check status any time with `/status` in the client.

**The client connected but my win condition or options look wrong.**
- This can happen if the game was generated with a different version of the ATS
  world. In the client, run `/setoptions` (type `/help` to see how), or restart
  the client with your YAML using `--yaml "path\to\American Truck Simulator.yaml"`.

**The plugin isn't loading.**
- Confirm the DLL is in `bin\win_x64\plugins\` (not directly in `bin\win_x64\`).
- Make sure the folder is named exactly `plugins`.

**Where is the communication folder?** (You normally never need this.)
```
Documents\American Truck Simulator\archipelago\
```
The client and plugin use it to talk to each other.

---

## Glossary

- **Archipelago** — the system that connects many different games into one shared
  "multiworld." Your items can appear in other people's games, and theirs in
  yours.
- **apworld** — the file that teaches Archipelago how ATS works.
- **Plugin / DLL** — a small add-on that runs inside the game so Archipelago can
  give you money and XP and see your progress.
- **Client** — the program you connect to the server with. It sends your checks
  and receives your items. Keep it open while playing.
- **Check** — something you do in-game (a delivery, a first city arrival, a level
  milestone) that unlocks an item for someone in the multiworld.
- **Item** — a reward you receive (money, XP, a state unlock, or a trap).
- **YAML** — your settings file that describes how you want to play.
- **Server address** — where your game is hosted, like `archipelago.gg:12345`.

Happy trucking!
