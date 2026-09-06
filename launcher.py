#!/usr/bin/env python3
"""Coproton - run a Steam game together with an arbitrary program (a trainer).

Modes:
    coproton                    configuration window
    coproton --register         register as a Steam compatibility tool
    coproton --selftest         run the built-in checks
    coproton <verb> <cmd...>    compat tool entry point, invoked by Steam
    coproton --inner <verb> ... the same, already inside the Steam runtime container

The compat tool entry point runs on the host, where a GUI toolkit exists, shows the
launch window, and then re-enters itself through the Steam Linux Runtime so that the
game and the program end up in one container, sharing one wine prefix.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "coproton" / "config.json"
LOG = CONFIG.parent / "last.log"

STEAM_ROOTS = ("~/.steam/root", "~/.steam/steam", "~/.local/share/Steam",
               "~/.var/app/com.valvesoftware.Steam/data/Steam")
# Tools and runtimes that are not games.
NOT_A_GAME = re.compile(r"^(Proton|Steam Linux Runtime|Steamworks|SteamVR|.*Redistributable)")
# Verbs that actually start the game. Steam also asks for paths, and those must not
# spawn anything.
LAUNCH_VERBS = ("run", "waitforexitandrun")
DEFAULT_DELAY = 10

# WeMod ships as a NuGet package, so unpacking lib/net45 gives a portable copy with no
# installer, no Squirrel stubs and no updater. The version is pinned on purpose: 12.x draws
# nothing but a black window under wine no matter what is done to the prefix or which
# Chromium flags are passed, while 11.6.0 renders and applies cheats. Hash and URL shape
# come from the scoop-games manifest.
WEMOD_VERSION = "11.6.0"
WEMOD_URL = "https://storage-cdn.wemod.com/app/releases/stable/WeMod-%s-full.nupkg" % WEMOD_VERSION
WEMOD_SHA256 = "5b94ae5592e698b13cbc06fae4c096fe2438cbd362daac3f842e13190bf836ba"
WEMOD_DIR = (Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
             / "coproton" / ("wemod-" + WEMOD_VERSION))


# ------------------------------------------------------------------ discovery

def steam_roots():
    """Existing Steam roots, deduplicated (~/.steam/root is usually a symlink)."""
    seen, out = set(), []
    for raw in STEAM_ROOTS:
        p = Path(raw).expanduser()
        if p.is_dir() and p.resolve() not in seen:
            seen.add(p.resolve())
            out.append(p)
    return out


def parse_paths(vdf_text):
    return re.findall(r'"path"\s+"([^"]+)"', vdf_text)


def libraries():
    """Every steamapps directory, including libraries on other drives."""
    cands = []
    for root in steam_roots():
        cands.append(root / "steamapps")
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            cands += [Path(p) / "steamapps" for p in parse_paths(vdf.read_text(errors="replace"))]
    seen, out = set(), []
    for p in cands:
        if p.is_dir() and p.resolve() not in seen:
            seen.add(p.resolve())
            out.append(p)
    return out


def protons():
    """{display name: path to the proton executable}."""
    out = {}
    for root in steam_roots():
        for p in root.glob("compatibilitytools.d/*/proton"):
            out.setdefault(p.parent.name, str(p))
    for lib in libraries():
        for p in lib.glob("common/*/proton"):
            out.setdefault(p.parent.name, str(p))
    return dict(sorted(out.items()))


def default_proton():
    # ponytail: lexical sort, so GE-Proton11-6 beats GE-Proton10-32 and Proton 6.3.
    # Switch to numeric comparison if a GE-Proton100 ever ships.
    names = protons()
    return names[sorted(names, reverse=True)[0]] if names else None


def vdf_block(text, key):
    """The brace-balanced body that follows "key" in a text vdf, or ""."""
    at = text.find('"%s"' % key)
    if at < 0:
        return ""
    start = text.find("{", at)
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i]
    return ""


def official_internal_name(dirname):
    """Steam addresses its own Proton builds by an internal name, not by directory.

    "Proton - Experimental" is proton_experimental, "Proton 9.0" is proton_9,
    "Proton 6.3" is proton_63.
    """
    name = dirname.strip()
    if name.lower().endswith(".0"):
        name = name[:-2]
    return "_".join(re.findall(r"[a-z0-9]+", name.lower().replace(".", "")))


def compat_tool_names():
    """{name Steam stores in its settings: path to that tool's proton}."""
    out = {}
    for root in steam_roots():
        for vdf in root.glob("compatibilitytools.d/*/compatibilitytool.vdf"):
            proton = vdf.parent / "proton"
            if not proton.exists():
                continue                       # A wrapper like Coproton itself, not a Proton.
            text = vdf.read_text(errors="replace")
            for key in re.findall(r'"([^"]+)"\s*\{', text):
                if key not in ("compatibilitytools", "compat_tools"):
                    out[key] = str(proton)
    for lib in libraries():
        for proton in lib.glob("common/*/proton"):
            out.setdefault(official_internal_name(proton.parent.name), str(proton))
    return out


def steam_default_tool_name():
    """The compatibility tool chosen in Steam Settings -> Compatibility, or None.

    Steam records it as the appid "0" entry of CompatToolMapping. It is absent when
    "Enable Steam Play for all other titles" has never been turned on.
    """
    for root in steam_roots():
        config = root / "config" / "config.vdf"
        if not config.exists():
            continue
        block = vdf_block(config.read_text(errors="replace"), "CompatToolMapping")
        m = re.search(r'"0"\s*\{[^}]*?"name"\s*"([^"]*)"', block, re.S)
        if m and m.group(1):
            return m.group(1)
    return None


def steam_default_proton():
    """Path to the Proton that Steam itself would default to, or None."""
    name = steam_default_tool_name()
    return compat_tool_names().get(name) if name else None


def resolve_proton(entry):
    """Which Proton to launch with, and a word on where that choice came from.

    An empty "proton" in the config means "whatever Steam is set to use".
    """
    chosen = entry.get("proton")
    if chosen:
        return chosen, "configured"
    followed = steam_default_proton()
    if followed:
        return followed, "Steam default"
    return default_proton(), "newest installed"


def parse_acf(text):
    """Pull appid, name and installdir out of an appmanifest_*.acf."""
    def field(k):
        m = re.search(r'"%s"\s+"([^"]*)"' % k, text)
        return m.group(1) if m else None
    return field("appid"), field("name"), field("installdir")


def app_dir(appid):
    """Install directory of an installed app, by AppID."""
    for lib in libraries():
        acf = lib / ("appmanifest_%s.acf" % appid)
        if acf.exists():
            _, _, installdir = parse_acf(acf.read_text(errors="replace"))
            if installdir:
                return lib / "common" / installdir
    return None


def parse_runtime_appid(manifest_text):
    """The require_tool_appid of a toolmanifest.vdf, ignoring // comments."""
    text = re.sub(r"//[^\n]*", "", manifest_text)
    m = re.search(r'"require_tool_appid"\s+"(\d+)"', text)
    return m.group(1) if m else None


def proton_runtime(proton):
    """Path to the Steam Linux Runtime entry point a given Proton demands, or None.

    Modern Proton crashes outside its runtime, and Steam only sets one up for the tool
    it launches directly. Since Coproton is that tool, it has to build the container
    itself, and the Proton's own manifest is the authority on which one is needed.
    """
    manifest = Path(proton).parent / "toolmanifest.vdf"
    if not manifest.exists():
        return None
    appid = parse_runtime_appid(manifest.read_text(errors="replace"))
    if not appid:
        return None                      # Old Proton, runs fine on the host.
    directory = app_dir(appid)
    entry = directory / "_v2-entry-point" if directory else None
    return str(entry) if entry and entry.exists() else None


def parse_binary_vdf(data, i=0):
    """Minimal binary VDF reader, enough for shortcuts.vdf. Returns (dict, offset).

    Type bytes: 0x00 nested map, 0x01 string, 0x02 int32, 0x07 uint64, 0x08 end of map.
    """
    out = {}
    while i < len(data):
        kind = data[i]
        i += 1
        if kind == 0x08:
            break
        end = data.index(0, i)
        key = data[i:end].decode("utf-8", "replace")
        i = end + 1
        if kind == 0x00:
            out[key], i = parse_binary_vdf(data, i)
        elif kind == 0x01:
            end = data.index(0, i)
            out[key] = data[i:end].decode("utf-8", "replace")
            i = end + 1
        elif kind == 0x02:
            out[key] = int.from_bytes(data[i:i + 4], "little", signed=True)
            i += 4
        elif kind == 0x07:
            out[key] = int.from_bytes(data[i:i + 8], "little")
            i += 8
        else:
            break  # Unknown type, the rest of the stream is no longer parseable.
    return out, i


def shortcut_appid(entry):
    """AppID of a non-Steam shortcut, matching the steamapps/compatdata directory name.

    shortcuts.vdf stores it as a signed int32, Steam uses the unsigned value. Very old
    Steam versions omit the field, in which case it is crc32(Exe + AppName) | 0x80000000.
    """
    appid = entry.get("appid")
    if appid is None:
        seed = (entry.get("exe", "") + entry.get("appname", "")).encode("utf-8", "replace")
        appid = zlib.crc32(seed) | 0x80000000
    return str(appid & 0xFFFFFFFF)


def shortcuts():
    """{appid: {name, dir, non_steam}} for games added via "Add a Non-Steam Game"."""
    out = {}
    for root in steam_roots():
        for f in root.glob("userdata/*/config/shortcuts.vdf"):
            try:
                data, _ = parse_binary_vdf(f.read_bytes())
            except (OSError, ValueError, IndexError):
                continue
            top = next((v for k, v in data.items()
                        if k.lower() == "shortcuts" and isinstance(v, dict)), {})
            for raw in top.values():
                if not isinstance(raw, dict):
                    continue
                entry = {k.lower(): v for k, v in raw.items()}
                name = (entry.get("appname") or "").strip()
                if not name:
                    continue
                exe = unquote_path(entry.get("exe") or "")
                start = unquote_path(entry.get("startdir") or "")
                out[shortcut_appid(entry)] = {
                    "name": name,
                    "dir": start or (str(Path(exe).parent) if exe else ""),
                    "non_steam": True,
                }
    return out


def games():
    """{appid: {name, dir, non_steam}} for installed Steam games and non-Steam shortcuts."""
    out = {}
    for lib in libraries():
        for acf in lib.glob("appmanifest_*.acf"):
            try:
                appid, name, installdir = parse_acf(acf.read_text(errors="replace"))
            except OSError:
                continue
            if not (appid and name) or NOT_A_GAME.match(name):
                continue
            out[appid] = {"name": name,
                          "dir": str(lib / "common" / installdir) if installdir else "",
                          "non_steam": False}
    out.update(shortcuts())
    return dict(sorted(out.items(), key=lambda kv: kv[1]["name"].lower()))


def prefix_path(appid):
    """steamapps/compatdata/<appid>, wherever Steam put it."""
    for lib in libraries():
        p = lib / "compatdata" / str(appid)
        if p.is_dir():
            return p
    return None


def unquote_path(value):
    """Paths pasted from a file manager or a vdf arrive wrapped in quotes."""
    return (value or "").strip().strip('"').strip("'").strip()


def wine_bin(proton, name="wine"):
    """Proton ships wine under files/ (current) or dist/ (older builds)."""
    base = Path(proton).parent
    for sub in ("files", "dist"):
        p = base / sub / "bin" / name
        if p.exists():
            return str(p)
    return None


# --------------------------------------------------------------------- config

def load():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


# ----------------------------------------------------------------- registration

def register(quiet=False):
    """Symlink this directory into compatibilitytools.d of every Steam install found."""
    done = []
    for root in steam_roots():
        link = root / "compatibilitytools.d" / "coproton"
        if link.is_symlink() and link.resolve() == HERE:
            done.append(link)
            continue
        if link.exists() or link.is_symlink():
            if not quiet:
                print("occupied, skipping: %s" % link, file=sys.stderr)
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(HERE)
        done.append(link)
        if not quiet:
            print("installed: %s -> %s" % (link, HERE))
    if not quiet:
        print("restart Steam, the tool shows up in the game properties" if done
              else "no Steam installation found")
    return done


# ------------------------------------------------------------------------- .NET

# Build number -> the winetricks verb that sets it. Proton itself leaves win10 or win11.
WINVER_BUILDS = {"3790": "winxp64", "7601": "win7", "9600": "win81",
                 "19045": "win10", "22000": "win11", "26100": "win11"}


def prefix_winver(pfx, default="win10"):
    """The winetricks verb matching the Windows version currently set in the prefix."""
    try:
        text = (Path(pfx) / "system.reg").read_text(errors="replace")
    except OSError:
        return default
    m = re.search(r'"CurrentBuild"="(\d+)"', text)
    return WINVER_BUILDS.get(m.group(1), default) if m else default


def wemod_exe():
    """Path to the unpacked WeMod, or None when it has not been set up yet."""
    exe = WEMOD_DIR / "WeMod.exe"
    return str(exe) if exe.exists() else None


def unpack_wemod(package, dest):
    """Unpack lib/net45 out of a WeMod .nupkg into dest. Returns the path to WeMod.exe."""
    import zipfile
    prefix = "lib/net45/"
    dest = Path(dest)
    with zipfile.ZipFile(package) as archive:
        members = [n for n in archive.namelist()
                   if n.startswith(prefix) and not n.endswith("/")]
        if not any(n == prefix + "WeMod.exe" for n in members):
            raise ValueError("no lib/net45/WeMod.exe in %s" % Path(package).name)
        for name in members:
            # Refuse anything that would climb out of dest.
            target = (dest / name[len(prefix):]).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise ValueError("archive escapes the destination: %s" % name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return str(dest / "WeMod.exe")


def install_wemod(report=print):
    """Download the pinned WeMod build and unpack it. Returns the exe path, or None."""
    import hashlib
    import tempfile
    import urllib.request
    if wemod_exe():
        return wemod_exe()
    WEMOD_DIR.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    try:
        with tempfile.NamedTemporaryFile(suffix=".nupkg", delete=False) as tmp:
            package = tmp.name
            with urllib.request.urlopen(WEMOD_URL) as response:
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                for chunk in iter(lambda: response.read(1 << 20), b""):
                    tmp.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if total:
                        report("downloading WeMod %s: %d%%" % (WEMOD_VERSION, done * 100 // total))
        if digest.hexdigest() != WEMOD_SHA256:
            report("the download does not match its known checksum, discarding it")
            return None
        report("unpacking WeMod %s..." % WEMOD_VERSION)
        exe = unpack_wemod(package, WEMOD_DIR)
    except (OSError, ValueError) as exc:
        report("could not set up WeMod: %s" % exc)
        return None
    finally:
        try:
            os.unlink(package)
        except (OSError, NameError):
            pass
    report("WeMod %s is ready" % WEMOD_VERSION)
    return exe


def deref_symlinks(win_dir, report=print):
    """Replace file symlinks under drive_c/windows with real copies. Returns the count.

    Proton symlinks system libraries into its own read-only directory. An installer that
    writes through such a link changes nothing and reports success, which is how a .NET
    install can finish and still leave wine's own mscoree.dll in place, sending every
    managed program to wine-mono instead of the framework that was just installed.
    Borrowed from DeckCheatz/wemod-launcher, which hit the same wall.
    """
    replaced = 0
    for link in Path(win_dir).rglob("*"):
        if not link.is_symlink() or link.is_dir():
            continue                     # Directory links are part of the prefix layout.
        try:
            data = link.read_bytes() if link.exists() else None
            link.unlink()
            if data is not None:
                link.write_bytes(data)
            replaced += 1
        except OSError as exc:
            report("could not dereference %s: %s" % (link.name, exc))
    return replaced


def install_dotnet(proton, appid, report=print):
    """winetricks -q dotnet48 into the game prefix, using wine from the chosen Proton.

    Runs on the host: the runtime container ships neither winetricks nor a shell for it.
    """
    winetricks = shutil.which("winetricks")
    if not winetricks:
        report("winetricks is not installed")
        return False
    pfx = prefix_path(appid)
    wine = wine_bin(proton)
    if not pfx or not (pfx / "pfx" / "system.reg").exists():
        report("no prefix yet, start the game once and try again")
        return False
    if not wine:
        report("no wine binary in the selected Proton")
        return False
    env = dict(os.environ,
               WINEPREFIX=str(pfx / "pfx"),
               WINE=wine,
               WINESERVER=wine_bin(proton, "wineserver") or "",
               WINEDLLOVERRIDES="mscoree=d",
               WINEDEBUG="-all")
    # Real files first, or the installer writes through Proton's symlinks into nothing.
    freed = deref_symlinks(pfx / "pfx" / "drive_c" / "windows", report)
    if freed:
        report("made %d linked system files real" % freed)

    # Proton pre-registers a .NET 4.7 that does not exist, so that apps checking for the
    # framework find one and then run on wine-mono. The real installer reads the same keys,
    # decides .NET is already present and quits without a word.
    for view in ("Software", r"Software\Wow6432Node"):
        subprocess.run([wine, "reg", "delete",
                        r"HKLM\%s\Microsoft\NET Framework Setup\NDP\v4" % view, "/f"],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if env["WINESERVER"]:
        subprocess.run([env["WINESERVER"], "-w"], env=env, check=False)

    # The dotnet48 recipe switches the prefix to Windows XP and never switches it back.
    # A modern game then refuses to use D3D12 ("DirectX 12 is not supported on your
    # system"), so the version has to be restored whether or not the install succeeded.
    before = prefix_winver(pfx / "pfx")
    report("installing .NET 4.8, this takes several minutes...")
    ok = subprocess.run([winetricks, "-q", "dotnet48"], env=env).returncode == 0
    after = prefix_winver(pfx / "pfx")
    if after != before:
        report("restoring the Windows version to %s (winetricks left it at %s)" % (before, after))
        subprocess.run([winetricks, "-q", before], env=env)
    if ok:
        report(".NET installed")
    else:
        # wine 11 cannot unpack the installer's cabinets ("err:msi:extract_cabinet FDICopy
        # failed"), while wine 10 can. Naming a Proton that works beats a bare failure.
        report("winetricks failed. The .NET installer does not unpack under wine 11, "
               "so pick a Proton built on wine 10 (GE-Proton10) and try again.")
    return ok


# --------------------------------------------------------------------- launching

def log(msg):
    print("[coproton] %s" % msg, file=sys.stderr, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def current_appid():
    """The compatdata directory name is the authoritative appid.

    It is the only source that works for non-Steam shortcuts too, where SteamAppId is 0.
    """
    data = os.environ.get("STEAM_COMPAT_DATA_PATH")
    if data:
        return Path(data).name
    return os.environ.get("SteamAppId") or os.environ.get("SteamGameId") or ""


def outer(argv):
    """Host side: show the launch window, then re-enter inside the Steam runtime."""
    verb, cmd = argv[0], argv[1:]
    appid = current_appid()
    entry = load().get(appid, {})

    if verb in LAUNCH_VERBS:
        action = "run"
        try:
            action = gui(appid=appid, launch=True)
        except Exception as exc:                      # noqa: BLE001
            # A broken GUI must never stop the game from starting.
            log("launch window unavailable (%s), using the saved settings" % exc)
        if action == "cancel":
            log("cancelled by the user")
            return 0
        if action == "save":
            log("settings saved, game not started")
            return 0
        entry = load().get(appid, {})

    proton, source = resolve_proton(entry)
    if not proton or not os.access(proton, os.X_OK):
        sys.exit("[coproton] no Proton found, configure it by running `coproton`")
    log("proton=%s (%s)" % (Path(proton).parent.name, source))

    inner_cmd = [sys.executable, str(HERE / "launcher.py"), "--inner", verb, *cmd]
    runtime = proton_runtime(proton)
    if runtime:
        log("entering %s" % Path(runtime).parent.name)
        inner_cmd = [runtime, "--verb=%s" % verb, "--", *inner_cmd]
    else:
        log("selected Proton needs no runtime container")
    os.environ["COPROTON_PROTON"] = proton
    code = subprocess.run(inner_cmd).returncode
    if code != 0:
        # This used to fail silently, which hid a broken container for a whole session.
        log("inner process exited with %d" % code)
    return code


def inner(argv):
    """Container side: start the game, then the program, in one prefix."""
    verb, cmd = argv[0], argv[1:]
    appid = current_appid()
    entry = load().get(appid, {})
    proton = os.environ.get("COPROTON_PROTON") or resolve_proton(entry)[0]
    if not proton:
        sys.exit("[coproton] no Proton found")

    if verb not in LAUNCH_VERBS:
        # Steam also asks for paths. Answer and start nothing.
        return subprocess.run([proton, verb, *cmd]).returncode

    log("appid=%s proton=%s" % (appid or "?", Path(proton).parent.name))
    game = subprocess.Popen([proton, verb, *cmd])

    program, extra = unquote_path(entry.get("program")), None
    # Electron programs need --disable-gpu under wine, so the flags have to be settable.
    args = shlex.split(entry.get("args") or "")
    if program and Path(program).exists():
        time.sleep(entry.get("delay", DEFAULT_DELAY))
        if game.poll() is None:
            log("starting %s" % program)
            try:
                # Output is captured: a trainer that dies on startup used to fail silently.
                with LOG.open("a") as out:
                    extra = subprocess.Popen([proton, "runinprefix", program, *args],
                                             cwd=str(Path(program).parent),
                                             stdout=out, stderr=subprocess.STDOUT)
            except OSError as exc:
                log("could not start the program: %s" % exc)
        else:
            log("game exited before the program could start")
    elif program:
        log("program not found: %s" % program)

    code = game.wait()
    if extra and extra.poll() is None:
        # ponytail: only the wrapper is killed, Proton reaps the wine processes on shutdown.
        extra.terminate()
    log("game exited with %d" % code)
    return code


# ------------------------------------------------------------------------- GUI

def gui(appid=None, launch=False):
    """Configuration window. Returns "cancel", "save" or "run".

    tkinter is imported here and nowhere else: the launch path must keep working on
    machines without it, and inside the runtime container it does not exist at all.
    """
    import tkinter as tk
    from tkinter import ttk, filedialog

    all_games, all_protons = games(), protons()
    cfg = load()
    order = list(all_games)
    if appid and appid not in all_games:                 # Game Steam knows and we do not.
        all_games[appid] = {"name": "AppID %s" % appid, "dir": "", "non_steam": True}
        order.insert(0, appid)

    def label(gid):
        g = all_games[gid]
        return "%s%s" % (g["name"], "  [non-Steam]" if g["non_steam"] else "")

    root = tk.Tk()
    root.title("Coproton")
    root.minsize(560, 0)
    frame = ttk.Frame(root, padding=14)
    frame.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    frame.columnconfigure(1, weight=1)
    result = {"action": "cancel"}
    row = 0

    def add(text, widget, extra_widget=None):
        nonlocal row
        ttk.Label(frame, text=text).grid(row=row, column=0, sticky="w", pady=(0, 8), padx=(0, 10))
        widget.grid(row=row, column=1, sticky="ew", pady=(0, 8))
        if extra_widget is not None:
            extra_widget.grid(row=row, column=2, sticky="w", padx=(8, 0), pady=(0, 8))
        row += 1

    game_var = tk.StringVar()
    game_box = ttk.Combobox(frame, textvariable=game_var, state="readonly",
                            values=[label(g) for g in order])
    add("Game", game_box)

    # Following Steam is the default: an empty "proton" in the config means this entry.
    steam_pick = steam_default_proton()
    follow = "Steam default (%s)" % (Path(steam_pick).parent.name if steam_pick
                                     else "not set in Steam settings")
    proton_var = tk.StringVar()
    proton_box = ttk.Combobox(frame, textvariable=proton_var, state="readonly",
                              values=[follow] + list(all_protons))
    add("Proton", proton_box)

    program_var = tk.StringVar()
    program_entry = ttk.Entry(frame, textvariable=program_var)

    def browse():
        start = all_games.get(current_game(), {}).get("dir") or str(Path.home())
        chosen = filedialog.askopenfilename(
            parent=root, title="Select the program to run with the game",
            initialdir=start if Path(start).is_dir() else str(Path.home()),
            filetypes=[("Windows executables", "*.exe"), ("All files", "*")])
        if chosen:
            program_var.set(chosen)
            refresh_hint()

    def use_wemod():
        """Fetch WeMod on first use, then point the Program field at it."""
        exe = wemod_exe()
        if not exe:
            hint.config(text="Setting up WeMod, this downloads about 160 MB...")
            root.update()
            exe = install_wemod(report=lambda m: (hint.config(text=m), root.update()))
        if exe:
            program_var.set(exe)
            dotnet_var.set(True)   # The net45 build needs the real framework, not wine-mono.
            refresh_hint()

    program_buttons = ttk.Frame(frame)
    ttk.Button(program_buttons, text="Browse...", command=browse).grid(row=0, column=0)
    ttk.Button(program_buttons, text="WeMod", command=use_wemod).grid(row=0, column=1, padx=(6, 0))
    add("Program", program_entry, program_buttons)

    dotnet_var = tk.BooleanVar()
    ttk.Checkbutton(frame, text="Install .NET 4.8 into the prefix (rarely needed)",
                    variable=dotnet_var).grid(row=row, column=1, sticky="w", pady=(0, 8))
    row += 1

    args_var = tk.StringVar()
    add("Arguments", ttk.Entry(frame, textvariable=args_var))

    delay_var = tk.StringVar(value=str(DEFAULT_DELAY))
    add("Delay, seconds", ttk.Spinbox(frame, from_=0, to=600, textvariable=delay_var, width=8))

    hint = ttk.Label(frame, text="", wraplength=520, foreground="#b06000")
    hint.grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 10))
    row += 1

    def current_game():
        picked = game_var.get()
        for gid in order:
            if label(gid) == picked:
                return gid
        return ""

    def refresh_hint():
        notes = []
        program = unquote_path(program_var.get())
        if program and not Path(program).exists():
            notes.append("The program path does not exist.")
        # Warn about ticking the box, not about leaving it alone: almost every .NET
        # program runs on the wine-mono that Proton already ships, and installing .NET
        # removes it.
        if dotnet_var.get():
            if not shutil.which("winetricks"):
                notes.append("winetricks is not installed, .NET cannot be installed.")
            elif program and program == wemod_exe():
                notes.append("WeMod is a net45 build and does need the real framework, "
                             "so leave this ticked.")
            else:
                notes.append("Rarely needed: .NET programs, WPF included, normally run on "
                             "the wine-mono Proton ships, and installing .NET removes it. "
                             "Tick this only if the program fails without it.")
        hint.config(text="  ".join(notes))

    def load_game(*_):
        c = cfg.get(current_game(), {})
        chosen = c.get("proton")
        proton_var.set(next((n for n, p in all_protons.items() if p == chosen), follow)
                       if chosen else follow)
        program_var.set(unquote_path(c.get("program")))
        dotnet_var.set(bool(c.get("dotnet")))
        args_var.set(c.get("args", ""))
        delay_var.set(str(c.get("delay", DEFAULT_DELAY)))
        refresh_hint()

    game_box.bind("<<ComboboxSelected>>", load_game)
    program_var.trace_add("write", lambda *_: refresh_hint())
    dotnet_var.trace_add("write", lambda *_: refresh_hint())

    def store():
        gid = current_game()
        if not gid:
            hint.config(text="Pick a game first.")
            return None
        entry = cfg.setdefault(gid, {})
        was = entry.get("dotnet")
        picked = proton_var.get()
        entry.update(proton="" if picked == follow else all_protons.get(picked, ""),
                     program=unquote_path(program_var.get()),
                     dotnet=dotnet_var.get(),
                     args=args_var.get().strip(),
                     delay=int(delay_var.get() or 0))
        if was != entry["dotnet"]:
            entry.pop("dotnet_done", None)
        save(cfg)
        if entry["dotnet"] and not entry.get("dotnet_done"):
            hint.config(text="Installing .NET, the window will stay busy for a few minutes...")
            root.update()
            # .NET goes into the prefix with the wine of whichever Proton will run it.
            if install_dotnet(resolve_proton(entry)[0], gid,
                              report=lambda m: (hint.config(text=m), root.update())):
                entry["dotnet_done"] = True
                save(cfg)
        return gid

    def finish(action):
        if action != "cancel" and store() is None:
            return
        result["action"] = action
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=row, column=0, columnspan=3, sticky="e")
    ttk.Button(buttons, text="Cancel", command=lambda: finish("cancel")).grid(row=0, column=0, padx=4)
    ttk.Button(buttons, text="Save", command=lambda: finish("save")).grid(row=0, column=1, padx=4)
    run_button = ttk.Button(buttons, text="Save and Run", command=lambda: finish("run"))
    run_button.grid(row=0, column=2, padx=4)
    if not launch:
        run_button.state(["disabled"])            # Nothing to run outside a Steam launch.

    if appid and appid in all_games:
        game_var.set(label(appid))
        game_box.state(["disabled"])              # Steam already chose the game.
    elif order:
        game_var.set(label(order[0]))
    load_game()

    root.bind("<Escape>", lambda *_: finish("cancel"))
    root.eval("tk::PlaceWindow . center")
    root.mainloop()
    return result["action"]


# ------------------------------------------------------------------- selfchecks

def check_parsers():
    acf = '"AppState"{"appid" "220"\n"name" "Half-Life 2"\n"installdir" "Half-Life 2"}'
    assert parse_acf(acf) == ("220", "Half-Life 2", "Half-Life 2"), parse_acf(acf)
    assert parse_acf("{}") == (None, None, None)
    assert parse_paths('"path" "/mnt/games"\n"path" "/home/x/Steam"') == ["/mnt/games", "/home/x/Steam"]
    assert NOT_A_GAME.match("Proton 6.3") and NOT_A_GAME.match("Steam Linux Runtime 3.0")
    assert not NOT_A_GAME.match("Elden Ring")
    # A quoted path is what you get from a file manager, and it used to be stored verbatim.
    assert unquote_path('"/games/My Trainer.exe"') == "/games/My Trainer.exe"
    assert unquote_path("  '/games/t.exe' ") == "/games/t.exe"
    assert unquote_path(None) == ""


def check_runtime_manifest():
    """The runtime requirement must survive the comments a real manifest carries."""
    real = ('"manifest"\n{\n  "version" "2"\n  "commandline" "/proton %verb%"\n'
            '  "require_tool_appid" "4183110"\n  "use_sessions" "1"\n}')
    assert parse_runtime_appid(real) == "4183110"
    commented = '"manifest"\n{\n  // "require_tool_appid" "1628350"\n  "version" "2"\n}'
    assert parse_runtime_appid(commented) is None, "a commented-out line must not count"
    assert parse_runtime_appid('"manifest" { "version" "2" }') is None


def check_steam_settings():
    """The global Proton choice lives in a nested vdf block keyed by appid "0"."""
    assert official_internal_name("Proton - Experimental") == "proton_experimental"
    assert official_internal_name("Proton Hotfix") == "proton_hotfix"
    assert official_internal_name("Proton 9.0") == "proton_9", official_internal_name("Proton 9.0")
    assert official_internal_name("Proton 6.3") == "proton_63"
    assert official_internal_name("Proton 4.11") == "proton_411"

    config = '''"InstallConfigStore"
{
  "Software" { "Valve" { "Steam"
  {
    "CompatToolMapping"
    {
      "0"       { "name" "proton_experimental" "config" "" "priority" "75" }
      "1284210" { "name" "GE-Proton10-32"      "config" "" "priority" "250" }
    }
  } } }
}'''
    block = vdf_block(config, "CompatToolMapping")
    assert '"0"' in block and "GE-Proton10-32" in block
    # The block must stop at its own closing brace, not swallow the rest of the file.
    assert "InstallConfigStore" not in block
    assert re.search(r'"0"\s*\{[^}]*?"name"\s*"([^"]*)"', block).group(1) == "proton_experimental"
    assert vdf_block(config, "NoSuchKey") == ""

    # An explicit choice wins, then Steam's, then the newest installed build.
    assert resolve_proton({"proton": "/x/proton"}) == ("/x/proton", "configured")
    path, source = resolve_proton({})
    assert source in ("Steam default", "newest installed"), source
    assert resolve_proton({"proton": ""})[1] == source, "empty means follow Steam"


def check_winver():
    """Reading the prefix Windows version, so the .NET install can put it back."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        pfx = Path(tmp)
        win10 = ('[Software\\\\Microsoft\\\\Windows NT\\\\CurrentVersion]\n'
                 '"CurrentBuild"="19045"\n"CurrentVersion"="6.3"\n')
        (pfx / "system.reg").write_text(win10)
        assert prefix_winver(pfx) == "win10", prefix_winver(pfx)
        # What winetricks dotnet48 leaves behind, and what broke D3D12.
        (pfx / "system.reg").write_text(win10.replace("19045", "3790").replace("6.3", "5.2"))
        assert prefix_winver(pfx) == "winxp64", prefix_winver(pfx)
        (pfx / "system.reg").write_text('"CurrentBuild"="99999"\n')
        assert prefix_winver(pfx) == "win10", "an unknown build falls back to win10"
    assert prefix_winver(Path("/nonexistent")) == "win10", "a missing prefix must not raise"


def check_deref():
    """A link that an installer would write through becomes a real file it can replace."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "store").mkdir()
        real = tmp / "store" / "mscoree.dll"
        real.write_bytes(b"builtin")
        win = tmp / "windows"
        (win / "system32").mkdir(parents=True)
        link = win / "system32" / "mscoree.dll"
        link.symlink_to(real)
        (win / "system32" / "plain.dll").write_bytes(b"already real")
        (win / "linkdir").symlink_to(tmp / "store")   # Layout links must survive.
        broken = win / "system32" / "gone.dll"
        broken.symlink_to(tmp / "store" / "missing.dll")

        assert deref_symlinks(win, report=lambda *_: None) == 2, "one file link, one broken"
        assert not link.is_symlink() and link.read_bytes() == b"builtin"
        assert not broken.exists(), "a broken link is dropped, not recreated"
        assert (win / "linkdir").is_symlink(), "directory links are left alone"
        # The point of it all: the copy can be replaced without touching the original.
        link.write_bytes(b"microsoft")
        assert real.read_bytes() == b"builtin", "the read-only original must be untouched"


def check_wemod_unpack():
    """Only lib/net45 is taken out of the package, and nothing may escape the target."""
    import tempfile
    import zipfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        package = tmp / "wemod.nupkg"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("lib/net45/WeMod.exe", b"MZ app")
            archive.writestr("lib/net45/resources/app.asar", b"resources")
            archive.writestr("WeMod.nuspec", b"metadata that must stay behind")
        exe = unpack_wemod(package, tmp / "out")
        assert Path(exe).read_bytes() == b"MZ app", "the executable is unpacked"
        assert (tmp / "out" / "resources" / "app.asar").exists(), "and so is what it needs"
        assert not (tmp / "out" / "WeMod.nuspec").exists(), "packaging metadata is skipped"

        escaping = tmp / "evil.nupkg"
        with zipfile.ZipFile(escaping, "w") as archive:
            archive.writestr("lib/net45/WeMod.exe", b"MZ")
            archive.writestr("lib/net45/../../../escaped", b"nope")
        try:
            unpack_wemod(escaping, tmp / "out2")
            raise AssertionError("a path climbing out of the target must be refused")
        except ValueError:
            pass

        wrong = tmp / "wrong.nupkg"
        with zipfile.ZipFile(wrong, "w") as archive:
            archive.writestr("lib/net48/WeMod.exe", b"MZ")
        try:
            unpack_wemod(wrong, tmp / "out3")
            raise AssertionError("a package without lib/net45/WeMod.exe must be refused")
        except ValueError:
            pass


def check_shortcuts():
    """Byte-for-byte shape of a real shortcuts.vdf entry, including the negative appid."""
    blob = (b"\x00shortcuts\x00"
            b"\x000\x00"
            b"\x02appid\x00\x77\xf4\xd1\xdf"
            b"\x01AppName\x00Blood of Dawnwalker\x00"
            b'\x01Exe\x00"/mnt/d/Games/Dawnwalker/game.exe"\x00'
            b"\x01StartDir\x00/mnt/d/Games/Dawnwalker/\x00"
            b"\x08\x08\x08")
    data, _ = parse_binary_vdf(blob)
    entry = {k.lower(): v for k, v in data["shortcuts"]["0"].items()}
    assert entry["appid"] == -539888521, entry["appid"]
    # Steam names the prefix directory with the unsigned value.
    assert shortcut_appid(entry) == "3755078775", shortcut_appid(entry)
    assert entry["startdir"] == "/mnt/d/Games/Dawnwalker/"
    assert shortcut_appid({"exe": '"/x/g.exe"', "appname": "G"}).isdigit()
    assert int(shortcut_appid({"exe": '"/x/g.exe"', "appname": "G"})) >= 0x80000000



def check_launch():
    """Game and program go to the same proton, and the game's exit code is propagated."""
    import tempfile
    global CONFIG, LOG
    saved_config, saved_log, saved_env = CONFIG, LOG, dict(os.environ)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        stub, calls = tmp / "pt" / "proton", tmp / "calls"
        stub.parent.mkdir()
        # The stub outlives its own log write, so the game cannot exit (and take the
        # program down with it) before the program has recorded that it started.
        stub.write_text('#!/bin/sh\necho "$@" >> "%s"\nsleep 0.5\nexit 7\n' % calls)
        stub.chmod(0o755)
        (tmp / "trainer.exe").touch()
        CONFIG, LOG = tmp / "config.json", tmp / "log"
        # A quoted path, exactly as the old GUI used to store it.
        save({"3755078775": {"proton": str(stub),
                             "program": '"%s"' % (tmp / "trainer.exe"),
                             "args": "--disable-gpu --flag=a b", "delay": 0}})
        os.environ.update(SteamAppId="0", STEAM_COMPAT_DATA_PATH=str(tmp / "3755078775"))
        os.environ.pop("COPROTON_PROTON", None)
        try:
            code = inner(["waitforexitandrun", "/games/game.exe", "-windowed"])
            # A path query must answer without starting anything.
            inner(["getcompatpath", "/games/game.exe"])
        finally:
            CONFIG, LOG = saved_config, saved_log
            os.environ.clear()
            os.environ.update(saved_env)
        deadline, lines = time.time() + 3, []
        while time.time() < deadline:
            lines = sorted(calls.read_text().splitlines()) if calls.exists() else []
            if len(lines) == 3:
                break
            time.sleep(0.05)
    assert code == 7, code
    # Sorted, not in call order: with delay 0 both stubs append concurrently.
    assert len(lines) == 3, lines
    assert lines[0] == "getcompatpath /games/game.exe", lines
    assert lines[1].startswith("runinprefix ") and "trainer.exe" in lines[1], lines
    assert '"' not in lines[1], "the quoted path must be cleaned before use: %s" % lines[1]
    # Electron programs need flags like --disable-gpu, split the way a shell would.
    assert lines[1].endswith("--disable-gpu --flag=a b"), lines[1]
    assert lines[2] == "waitforexitandrun /games/game.exe -windowed", lines


def selftest():
    check_parsers()
    check_runtime_manifest()
    check_steam_settings()
    check_deref()
    check_wemod_unpack()
    check_winver()
    check_shortcuts()
    check_launch()
    for name, path in protons().items():   # Real environment, when there is one.
        assert os.access(path, os.X_OK), name
    found = games()
    non_steam = sum(1 for g in found.values() if g["non_steam"])
    runtimes = {Path(r).parent.name for r in
                (proton_runtime(p) for p in protons().values()) if r}
    print("ok: %d protons, %d games (%d non-Steam), %d libraries, runtimes: %s"
          % (len(protons()), len(found), non_steam, len(libraries()),
             ", ".join(sorted(runtimes)) or "none"))


def main():
    args = sys.argv[1:]
    if not args:
        return 0 if gui() else 0
    if args[0] == "--register":
        return 0 if register() else 1
    if args[0] == "--selftest":
        return selftest()
    if args[0] == "--inner":
        return inner(args[1:])
    if args[0] in ("-h", "--help"):
        return print(__doc__)
    return outer(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
