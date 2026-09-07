# Coproton

<img width="828" height="505" alt="image" src="https://github.com/user-attachments/assets/3573269b-67c5-46f8-a396-d7549226941a" />

Runs a Steam game together with an arbitrary Windows program (a trainer, an overlay, anything)
**inside the same wine prefix**, and therefore under the same `wineserver`. That shared
wineserver is what lets a trainer reach the game's memory.

Like Steam Tinker Launch, but three fields and a checkbox. Press Play in Steam, a small window
appears, you pick what to run alongside the game, and the game starts.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/vladimirstempel/coproton/main/install.sh | sh
```

Manual: copy `launcher.py`, `coproton.desktop` and both `.vdf` files anywhere and run
`./launcher.py --register`.

Everything lands in `~/.local`, no root and no package manager, so this also works on SteamOS,
Steam Deck, Bazzite and other immutable distros.

### Dependencies

* `python3` — required. Every machine running Steam already has it, since `proton` is itself
  a Python script.
* `tk` — needed only for the configuration window. `install.sh` detects the package manager
  (pacman, apt, dnf, zypper, apk, xbps) and offers to install it for you; answer no and it
  prints the command instead. Set `COPROTON_YES=1` to skip the prompt. On SteamOS, where the
  system image is read-only, it prints the `steamos-readonly` dance rather than attempting it.
  Without Tk games still launch using the settings already saved, so this never fails the
  install.
* `winetricks` — optional, only for the .NET checkbox.

## Usage

1. Restart Steam, then set game properties → Compatibility → **Coproton**.
2. Press Play. The launch window appears:
   * **Game** — preselected by Steam, shown for confirmation.
   * **Proton** — which build actually runs the game. The default entry, *Steam default*,
     follows whatever is set in Steam Settings → Compatibility, so Coproton does not become
     a second place to keep that choice up to date. Steam records it as the appid `0` entry
     of `CompatToolMapping` in `config.vdf`, and it only exists once "Enable Steam Play for
     all other titles" has been turned on. When it is unset, the entry says so and the newest
     installed build is used instead.
   * **Programs** — one row per executable to run alongside the game, each with its own
     arguments. **+** adds a row, **×** drops one. They all start after the delay, in the
     order listed, and share the game's prefix. Arguments are split the way a shell splits
     them, and leading `NAME=value` pairs — again as in a shell — set environment variables
     for that one program, which is how a program that needs a wine setting of its own gets
     it without the game being affected.
   * **Install .NET 4.8** — rarely needed, see below. Proton's wine-mono already runs most
     .NET programs, and installing .NET removes it.
   * **Delay** — how long to wait after the game starts before launching the programs.
3. **Cancel** aborts the launch, **Save** stores the settings without starting the game,
   **Save and Run** starts the game.

Every button carries a tooltip saying what it does, and long jobs (the .NET install, the
WeMod download) run in the background: the buttons lock, a bar spins, and **Cancel** turns
into **Stop** for anything that can be abandoned.

**Update** fetches the newest Coproton from GitHub: `launcher.py` and the two `.vdf` files
Steam reads to offer the tool at all. The launcher is compiled and a manifest has to look
like one before either is put in place, so a broken or half-finished update cannot leave the
tool unable to start a game or missing from Steam's list. Files that already match are left
alone, the registration symlink and the settings are never touched, and the new version
applies when the window is opened again.

**Run in prefix...** picks a program and runs it in the selected game's wine prefix straight
away, with no game and no launch: a patcher, a mod installer, a redistributable, a
configuration tool. It uses the wine of the Proton selected above, so the prefix has to exist
already — start the game once if it does not. Whatever the program prints goes to
`~/.config/coproton/last.log`.

The same window opens from the application menu (or by running `coproton`) to edit settings
without launching anything. There **Save and Run** is disabled, since there is no game to run.

## Non-Steam games

Games added through "Add a Non-Steam Game" work too and are marked `[non-Steam]`.
They have no `appmanifest_*.acf`, so they are read from the binary `shortcuts.vdf`, and their
AppID is the unsigned form of the signed int32 stored there, which is also the name of their
`steamapps/compatdata` directory. That path, not `SteamAppId`, is what identifies the game at
launch, because for a non-Steam shortcut `SteamAppId` is `0`.

## The .NET checkbox

**Usually you do not need it.** Proton ships wine-mono, and .NET programs, WPF ones included,
normally run on it unchanged. Installing .NET removes wine-mono, so tick this only when a
program genuinely refuses to start without the real framework.

When ticked, `winetricks -q dotnet48` runs against the game's prefix using the wine binary
from the Proton build you picked. Three things get in the way, and all three are handled:

* **Proton's symlinks.** Proton links system libraries into its own read-only directory. An
  installer writing through such a link changes nothing and still reports success, which is
  how a .NET install finishes while wine's own `mscoree.dll` stays in place and every managed
  program keeps running on wine-mono. Those links are turned into real files first. The trick
  comes from [wemod-launcher](https://github.com/DeckCheatz/wemod-launcher), which hit the
  same wall.
* **A .NET that does not exist.** Proton pre-registers .NET 4.7 under `NDP\v4` so that
  programs checking for the framework find one. The real installer reads the same keys,
  decides .NET is already present and quits silently, so they are removed first.
* **The Windows version.** The `dotnet48` recipe switches the prefix to Windows XP and never
  switches it back, which makes any modern game refuse D3D12 ("DirectX 12 is not supported on
  your system"). It is restored afterwards whether or not the install succeeded.

One thing is **not** handled: wine 11 fails to unpack the installer's cabinets
(`err:msi:extract_cabinet FDICopy failed`), so nothing installs under Proton Experimental or
GE-Proton11. wine 10 unpacks them fine, so pick a GE-Proton10 build when you need .NET.

## WeMod

The **WeMod** button next to the Program field downloads WeMod, unpacks it and points the
Program field at it. It also ticks .NET, which this one genuinely needs.

Two things are deliberate:

* **The version is pinned to 11.6.0.** WeMod 12.x starts, stays running, logs nothing at all
  and draws a black window. Nothing fixes it: not `--disable-gpu`, not disabling
  DirectComposition, not `--no-sandbox`, not pure SwiftShader rendering, not swapping the
  graphics libraries for the ones other launchers install. 11.6.0 renders and applies cheats
  in the same prefix, so the build is the variable that matters.
* **It is unpacked, not installed.** WeMod ships as a NuGet package, and `lib/net45` inside it
  is a portable copy. No installer, no Squirrel stubs that swallow arguments, no updater that
  would drag the version forward again. The download is checked against a known SHA-256.

It needs the real .NET Framework, so the prefix has to go through the .NET install described
above, which in turn means a Proton built on wine 10.

**Non-Steam games have to be added inside WeMod by hand.** WeMod does not find them on its
own: open its game list, use *Add game* and point it at the game's `.exe`. Games installed
through Steam are usually detected.

**The login is shared between games.** WeMod keeps its account in the wine prefix, and every
game has its own prefix, so it would ask for the login again for every game. Coproton keeps
one profile in `~/.local/share/coproton/wemod-profile` and links it into each prefix the first
time WeMod starts there, so signing in once is enough. Run one game at a time: two copies of
WeMod on the same profile will fight over the file locks.

WeMod's Pro features can be unlocked in the unpacked copy with a third party patch.
Coproton does not apply it, but it does launch WeMod so that a patched copy works. See
[WEMOD-PRO.md](WEMOD-PRO.md).

`vkd3d` and `dxvk2030`, which [wemod-launcher](https://github.com/DeckCheatz/wemod-launcher)
installs, are **not** needed. They were tried and reverted: they change nothing for WeMod, and
the leftover `libvkd3d-*.dll` they leave behind coincided with the game dying on a
`vkCreateComputePipelines` assertion inside winevulkan.

## Blish HUD

Blish HUD crashes a few seconds after it finds Guild Wars 2, with
`System.NotImplementedException` from `NAudio.CoreAudioApi.AudioSessionManager.Finalize`.
It is not a Coproton problem and no Proton build avoids it: when the game starts, Blish walks
every audio endpoint and touches `device.AudioSessionManager`, wine answers `E_NOTIMPL` for
the session notifications behind it, Blish swallows that exception, and the half-built object
then throws the same error again from its finaliser, where nothing can catch it.

Giving Blish a prefix with no audio devices at all avoids the walk. Put this in its arguments
row, which applies to Blish and leaves the game's sound alone:

```
WINEDLLOVERRIDES=winepulse.drv,winealsa.drv,wineoss.drv,winecoreaudio.drv=
```

Blish then has no sound of its own. Everything else, the game included, is unaffected.

That gets Blish running, but its overlay is then a black rectangle over the game: it draws a
transparent window, and wine has no transparency to give it. Blish's own maintainers point
Linux users at [external-dx11-overlay](https://github.com/SorryQuick/external-dx11-overlay)
instead, which renders Blish inside the game the way arcdps does, so no window and no
transparency are needed — and it bundles its own Blish build, which makes both of the
problems above Coproton's business no longer. Unzip it into the game folder, add
`addons/LOADER_public/Gw2-Simple-Addon-Loader.exe` as a non-Steam game, and give that
shortcut a specific Proton rather than the global default. On the Steam version of the game,
its launch options need `USE_STEAM_LOGIN=1 %command%`, or the launcher asks for ArenaNet
credentials a Steam account does not have.

## When the program does not start

Read `~/.config/coproton/last.log` first. Everything the program prints on startup is captured
there, and it usually names the cause outright.

In order of how often it is the answer:

1. **The prefix is dirty.** A prefix that winetricks has been through breaks in two ways at
   once: it is left on Windows XP, so the game refuses D3D12, and wine-mono is gone, so
   nothing managed runs. Move `steamapps/compatdata/<appid>` aside, let Proton build a fresh
   one, and copy the saves back. This fixes more than it has any right to.
2. **The delay is too short.** The program starts before the game exists and gives up. Raise
   it under Advanced; 20 seconds suits an Unreal Engine title.
3. **The Proton build.** Try another one. Prefix and build are worth changing one at a time,
   not together.
4. **The program is doing it deliberately.** Some trainers relaunch themselves to ask for
   administrator rights, check for a debugger, and exit with code 0 when they dislike the
   answer. The log shows a clean exit and no error, and there is nothing to fix on this side.

## Configuration

`~/.config/coproton/config.json`, keyed by AppID:

```json
{
  "2456085599": {
    "proton": "/mnt/d/SteamLibrary/steamapps/common/Proton - Experimental/proton",
    "program": "/mnt/d/Games/Trainers/Trainer.exe",
    "dotnet": true,
    "delay": 10,
    "dotnet_done": true
  }
}
```

The last launch is logged to `~/.config/coproton/last.log`, including anything the program
printed as it started.

## How it works

Coproton registers itself as a compatibility tool (a symlink to its own directory in
`compatibilitytools.d`), so Steam launches it instead of Proton.

The tricky part is the runtime container. Modern Proton declares `require_tool_appid` in its
own manifest and crashes when run outside that container, but Steam only sets one up for the
tool it launches directly — which is now Coproton, not Proton. Coproton could simply declare
the same requirement, except that the container has no Tk, no `yad`, no `zenity` and no
`winetricks`, so no window could be drawn from inside it.

So the work is split:

1. **On the host**, where a GUI exists, Coproton reads the config, shows the launch window, and
   looks up which runtime the selected Proton asks for by parsing that Proton's own manifest.
2. It then **re-enters itself through that runtime's `_v2-entry-point`**, so the game and the
   program share one container as well as one prefix.
3. **Inside the container** it starts the game with `proton waitforexitandrun`, waits out the
   delay, and starts the program with `proton runinprefix`. `run` must not be used here: it
   re-initialises the prefix and can take down the game's wineserver.
4. It waits for the game to exit, stops the program, and returns the game's exit code so Steam
   records playtime correctly.

Steam also invokes a compatibility tool to ask about paths rather than to launch anything.
Those verbs are passed straight through, and start no program.

## Known limitations

* The delay before starting the program is a fixed number of seconds rather than a wait for
  the game process to appear.
* Anti-cheat systems (EAC, BattlEye) dislike foreign processes in the prefix. Not something
  this tool can fix.
* Old Proton builds that declare no runtime are launched directly on the host, which is what
  they expect.

## Uninstall

```sh
rm -rf ~/.local/lib/coproton ~/.local/bin/coproton ~/.config/coproton \
       ~/.local/share/applications/coproton.desktop \
       ~/.steam/root/compatibilitytools.d/coproton
```

## Development

`python3 launcher.py --selftest` checks the acf/vdf parsers, the binary `shortcuts.vdf` reader,
the runtime-requirement lookup, .NET detection, and the whole launch sequence against a stub
`proton` — including that a quoted path is cleaned before use and that path queries start
nothing. No external dependencies, no test framework.

## License

MIT
