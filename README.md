# Coproton

Runs a Steam game together with an arbitrary Windows program (a trainer, an overlay, anything)
**inside the same wine prefix**, and therefore under the same `wineserver`. That shared
wineserver is what lets a trainer reach the game's memory.

Like Steam Tinker Launch, but three fields and a checkbox.

## Install

No root, no package manager, no dependencies beyond `python3` (which every machine running
Steam already has, since `proton` itself is a Python script):

```sh
curl -fsSL https://raw.githubusercontent.com/vladimirstempel/coproton/main/install.sh | sh
```

Manual: copy `launcher.py` and both `.vdf` files anywhere and run `./launcher.py --register`.

Works on SteamOS, Steam Deck, Bazzite and other immutable distros: everything lands in
`~/.local` and the rootfs is never touched.

## Usage

1. Run `coproton`. A browser tab opens.
2. Pick the game, the Proton build and the program. Tick .NET if the program needs it. Save.
3. Restart Steam.
4. Game properties → Compatibility → **Coproton**.
5. Launch the game as usual.

The program starts 10 seconds after the game, so the game process has time to appear.
If your trainer needs to attach sooner or later, change the delay under "Advanced".

## Non-Steam games

Games added through "Add a Non-Steam Game" are listed too, marked `[non-Steam]`.
They have no `appmanifest_*.acf`, so they are read from the binary `shortcuts.vdf`, and their
AppID is the unsigned form of the signed int32 stored there, which is also the name of their
`steamapps/compatdata` directory.

## The .NET checkbox

Installs `dotnet48` with `winetricks` into the game's prefix, using the wine binary from the
Proton build you picked. It runs once on the next launch and is then recorded in the config
(toggling the checkbox off and on again clears that marker). Expect the first launch to be slow.

`winetricks` is not a hard dependency: everything else works without it and the checkbox just
prints a warning. Install it the usual way: `pacman -S winetricks`, `apt install winetricks`,
`dnf install winetricks`.

## Configuration

`~/.config/coproton/config.json`, keyed by AppID:

```json
{
  "1245620": {
    "proton": "/home/me/.steam/root/compatibilitytools.d/GE-Proton11-6-x86_64/proton",
    "program": "/home/me/trainers/EldenRing.exe",
    "dotnet": true,
    "delay": 10,
    "dotnet_done": true
  }
}
```

The last launch is logged to `~/.config/coproton/last.log`.

## How it works

Coproton registers itself as a compatibility tool (a symlink to its own directory in
`compatibilitytools.d`). Steam then calls `launcher.py waitforexitandrun <game exe>`, which:

1. resolves the AppID from `STEAM_COMPAT_DATA_PATH` and loads its config;
2. installs .NET through winetricks if asked to;
3. starts the game with `proton waitforexitandrun <game>`;
4. waits, then starts the program with `proton runinprefix <program>` in the same prefix.
   `run` must not be used here: it re-initialises the prefix and can take down the game's
   wineserver;
5. waits for the game to exit, stops the program, and returns the game's exit code so Steam
   records playtime correctly.

## Known limitations

* `toolmanifest.vdf` declares no `require_tool_appid`, so Proton runs outside the Steam Linux
  Runtime container. Aurora Launcher does the same and it is fine for the vast majority of
  games. If a specific game refuses to start without the container, add
  `"require_tool_appid" "1628350"` (sniper) to the manifest and restart Steam.
* Anti-cheat systems (EAC, BattlEye) dislike foreign processes in the prefix. Not something
  this tool can fix.

## Uninstall

```sh
rm -rf ~/.local/lib/coproton ~/.local/bin/coproton \
       ~/.steam/root/compatibilitytools.d/coproton ~/.config/coproton
```

## Development

`python3 launcher.py --selftest` checks the vdf/acf parsers, the binary `shortcuts.vdf` reader,
the page template, and the whole launch sequence against a stub `proton`. No external
dependencies, no test framework.

## License

MIT
