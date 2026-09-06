# Coproton

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
   * **Program** — the executable to run alongside it, picked with a file dialog.
   * **Install .NET 4.8** — most trainers are .NET applications and do nothing without it.
   * **Delay** — how long to wait after the game starts before launching the program.
3. **Cancel** aborts the launch, **Save** stores the settings without starting the game,
   **Save and Run** starts the game.

The same window opens from the application menu (or by running `coproton`) to edit settings
without launching anything. There **Save and Run** is disabled, since there is no game to run.

## Non-Steam games

Games added through "Add a Non-Steam Game" work too and are marked `[non-Steam]`.
They have no `appmanifest_*.acf`, so they are read from the binary `shortcuts.vdf`, and their
AppID is the unsigned form of the signed int32 stored there, which is also the name of their
`steamapps/compatdata` directory. That path, not `SteamAppId`, is what identifies the game at
launch, because for a non-Steam shortcut `SteamAppId` is `0`.

## The .NET checkbox

Most trainers are .NET applications: without the runtime in the prefix they exit immediately
and silently. Ticking the box runs `winetricks -q dotnet48` against the game's prefix, using
the wine binary from the Proton build you picked, and records that it is done.

The window warns when the chosen executable imports `mscoree.dll` (that is, needs .NET) while
the checkbox is off, so the silent failure becomes a visible one.

The prefix has to exist first, so if the game has never been launched, start it once and then
tick the box.

Two things get in the way, and both are handled:

* Proton pre-registers a .NET 4.7 that does not exist, so that programs checking for the
  framework find one and then run on wine-mono. The real installer reads the same registry
  keys, decides .NET is already present and quits silently, so those keys are removed first.
* The `dotnet48` recipe switches the prefix to Windows XP and never switches it back, which
  makes any modern game refuse D3D12 ("DirectX 12 is not supported on your system"). The
  version is restored afterwards whether or not the install succeeded.

One thing that is **not** handled: wine 11 fails to unpack the installer's cabinets
(`err:msi:extract_cabinet FDICopy failed`), so the install cannot succeed under Proton builds
based on it, including Proton Experimental and GE-Proton11. wine 10 unpacks them fine, so pick
a GE-Proton10 build for the game and the install goes through.

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
