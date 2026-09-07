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
# Steam pushes its own housekeeping through the compat tool with a launch verb, once per
# Steam game, right before the game itself. Without this the window opened twice and the
# program started twice. Non-Steam shortcuts have no install script, hence no second run.
STEAM_HELPERS = ("iscriptevaluator.exe",)
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
WEMOD_PROFILE = WEMOD_DIR.parent / "wemod-profile"
# What the Update button fetches: the launcher, plus the two files Steam reads to offer
# Coproton in the first place.
UPDATE_BASE = "https://raw.githubusercontent.com/vladimirstempel/coproton/main/"
UPDATE_FILES = ("launcher.py", "toolmanifest.vdf", "compatibilitytool.vdf")


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


def entry_programs(entry):
    """The programs to run with the game, as a list of {"path", "args"}.

    Configs written before there could be more than one carry a single program/args
    pair instead, so they are read as a list of one.
    """
    listed = entry.get("programs")
    if listed is None:
        single = unquote_path(entry.get("program"))
        listed = [{"path": single, "args": entry.get("args", "")}] if single else []
    return [{"path": unquote_path(item.get("path")), "args": item.get("args", "")}
            for item in listed if unquote_path(item.get("path"))]


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


def install_wemod(report=print, stop=None):
    """Download the pinned WeMod build and unpack it. Returns the exe path, or None.

    stop is an optional threading.Event: setting it abandons the download.
    """
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
                    if stop is not None and stop.is_set():
                        report("download cancelled")
                        return None
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


def share_wemod_profile(report=print):
    """Point this prefix's WeMod profile at one shared copy. Returns True when linked.

    WeMod keeps an Electron profile inside the prefix, and every game has its own prefix,
    so the login would have to be typed again for every game. A symlink to one directory
    under ~/.local/share gives all of them the same account. Only one game at a time may
    use it: Chromium profiles hold locks on the files they open.
    """
    data = os.environ.get("STEAM_COMPAT_DATA_PATH")
    if not data:
        return False
    profile = Path(data) / "pfx/drive_c/users/steamuser/AppData/Roaming/WeMod"
    try:
        if profile.is_symlink():
            if profile.resolve() == WEMOD_PROFILE.resolve():
                return True
            profile.unlink()                       # Someone else's link, take it over.
        elif profile.is_dir():
            # The first prefix that already holds a login seeds the shared profile.
            if WEMOD_PROFILE.exists():
                shutil.rmtree(profile)
            else:
                WEMOD_PROFILE.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(profile), str(WEMOD_PROFILE))
        WEMOD_PROFILE.mkdir(parents=True, exist_ok=True)
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.symlink_to(WEMOD_PROFILE)
    except OSError as exc:                         # Never let this stop the launch.
        report("could not share the WeMod profile: %s" % exc)
        return False
    report("WeMod profile shared from %s" % WEMOD_PROFILE)
    return True


def update_self(report=print, stop=None, target=None):
    """Replace the installed files with the ones on main. Returns True when any changed.

    launcher.py is compiled before it is installed, and a manifest has to look like one:
    a truncated or mangled download would otherwise leave every game unable to start, or
    take Coproton out of Steam's list altogether. Files that already match are left alone,
    so the registration symlink and the settings never need touching.
    """
    import urllib.request
    here = Path(target or __file__).resolve().parent
    changed = []
    for name in UPDATE_FILES:
        if stop is not None and stop.is_set():
            report("update cancelled")
            break
        try:
            with urllib.request.urlopen(UPDATE_BASE + name) as response:
                fresh = response.read()
        except OSError as exc:
            report("could not download %s: %s" % (name, exc))
            break
        if name.endswith(".py"):
            try:
                compile(fresh, name, "exec")
            except (SyntaxError, ValueError) as exc:
                report("%s is not a working launcher: %s" % (name, exc))
                break
        elif b'"' not in fresh:
            report("%s does not look like a Steam manifest" % name)
            break
        path = here / name
        if path.exists() and path.read_bytes() == fresh:
            continue
        try:
            # Written beside the original and moved into place, so a failure half way
            # through leaves the working copy alone.
            fresh_file = path.with_name(path.name + ".new")
            fresh_file.write_bytes(fresh)
            fresh_file.chmod(0o755 if name.endswith(".py") else 0o644)
            fresh_file.replace(path)
        except OSError as exc:
            report("could not replace %s: %s" % (path, exc))
            break
        changed.append(name)
    if not changed:
        report("already up to date")
        return False
    report("updated %s, close and open Coproton again to use it" % ", ".join(changed))
    return True


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


def run_in_prefix(proton, appid, program, args=(), report=print, stop=None):
    """Run one program in the game's prefix, with no game. Returns True on exit code 0.

    For patchers, mod installers, config tools and the like: they have to see the same
    wine prefix the game runs in, and waiting for a launch to do that is silly.
    """
    pfx, wine = prefix_path(appid), wine_bin(proton)
    if not pfx or not (pfx / "pfx" / "system.reg").exists():
        report("no prefix yet, start the game once and try again")
        return False
    if not wine:
        report("no wine binary in the selected Proton")
        return False
    env = dict(os.environ, WINEPREFIX=str(pfx / "pfx"),
               WINESERVER=wine_bin(proton, "wineserver") or "", WINEDEBUG="-all")
    name = Path(program).name
    report("running %s in the prefix..." % name)
    try:
        # Output is kept: a patcher that refuses to start says why here and nowhere else.
        with LOG.open("a") as out:
            running = subprocess.Popen([wine, program, *args], env=env,
                                       cwd=str(Path(program).parent),
                                       stdout=out, stderr=subprocess.STDOUT)
    except OSError as exc:
        report("could not start %s: %s" % (name, exc))
        return False
    while running.poll() is None:
        if stop is not None and stop.is_set():
            running.terminate()
            report("%s stopped" % name)
            return False
        time.sleep(0.2)
    report("%s finished" % name if not running.returncode
           else "%s exited with %d, see the log" % (name, running.returncode))
    return running.returncode == 0


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


def is_launch(verb, cmd):
    """Whether this invocation is the game starting, rather than a Steam chore."""
    if verb not in LAUNCH_VERBS:
        return False
    return not any(Path(part).name.lower() in STEAM_HELPERS for part in cmd)


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

    if is_launch(verb, cmd):
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


ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def split_env(args):
    """Split "NAME=value ... arguments" the way a shell does. Returns (env, arguments).

    It is the only way to give one program its own environment, for the cases where a wine
    setting has to apply to it and not to the game.
    """
    tokens = shlex.split(args or "")
    env = {}
    while tokens and ENV_ASSIGNMENT.match(tokens[0]):
        name, value = tokens.pop(0).split("=", 1)
        env[name] = value
    return env, tokens


def start_program(proton, path, args=""):
    """Start one program in the game's prefix. Returns the process, or None."""
    extra_env, arguments = split_env(args)
    env = dict(os.environ, **extra_env) if extra_env else os.environ
    if path == wemod_exe():
        share_wemod_profile(log)
        # wemod_enhancer drops a proxy version.dll next to WeMod.exe. Wine answers
        # with its own builtin unless the load order asks for a native one first,
        # and then Electron rejects the patched app.asar.
        env = dict(env, WINEDLLOVERRIDES="version=n,b", **extra_env)
    log("starting %s" % path)
    try:
        # Output is captured: a trainer that dies on startup used to fail silently.
        with LOG.open("a") as out:
            return subprocess.Popen([proton, "runinprefix", path, *arguments],
                                    cwd=str(Path(path).parent), env=env,
                                    stdout=out, stderr=subprocess.STDOUT)
    except OSError as exc:
        log("could not start %s: %s" % (Path(path).name, exc))
        return None


def inner(argv):
    """Container side: start the game, then the program, in one prefix."""
    verb, cmd = argv[0], argv[1:]
    appid = current_appid()
    entry = load().get(appid, {})
    proton = os.environ.get("COPROTON_PROTON") or resolve_proton(entry)[0]
    if not proton:
        sys.exit("[coproton] no Proton found")

    if not is_launch(verb, cmd):
        # Steam also asks for paths and runs its install-script evaluator through us.
        # Answer, and start nothing alongside it.
        return subprocess.run([proton, verb, *cmd]).returncode

    log("appid=%s proton=%s" % (appid or "?", Path(proton).parent.name))
    game = subprocess.Popen([proton, verb, *cmd])

    programs, extras = [], []
    for item in entry_programs(entry):
        if Path(item["path"]).exists():
            programs.append(item)
        else:
            log("program not found: %s" % item["path"])
    if programs:
        time.sleep(entry.get("delay", DEFAULT_DELAY))
        if game.poll() is None:
            extras = [p for p in (start_program(proton, item["path"], item["args"])
                                  for item in programs) if p]
            # A program that dies at once used to leave nothing but "starting" in the
            # log. The usual cause is a prefix the game is still building.
            time.sleep(2)
            for dead in (p for p in extras if p.poll() is not None):
                log("a program exited immediately with %d, the prefix may still be "
                    "under construction: raise the delay and try again" % dead.returncode)
        else:
            log("game exited before the programs could start")

    code = game.wait()
    for extra in extras:
        if extra.poll() is None:
            # ponytail: only the wrapper is killed, Proton reaps the wine processes on
            # shutdown.
            extra.terminate()
    log("game exited with %d" % code)
    return code


# ------------------------------------------------------------------------- GUI

# Where Omarchy keeps the colours of the theme in use, newest layout first.
OMARCHY_COLORS = ("~/.local/state/omarchy/current/theme/colors.toml",
                  "~/.config/omarchy/current/theme/colors.toml")
# Tokyo Night, the Omarchy default, for machines that do not run Omarchy at all.
FALLBACK_COLORS = {"background": "#1a1b26", "lighter_background": "#24283b",
                   "foreground": "#c0caf5", "dark_foreground": "#565f89",
                   "muted": "#414868", "accent": "#7aa2f7", "selection": "#33467c",
                   "yellow": "#e0af68"}
FONTS = ("CaskaydiaMono Nerd Font", "JetBrainsMono Nerd Font", "Noto Sans")


def theme_colors():
    """Colours of the current Omarchy theme, falling back to Tokyo Night."""
    colors = dict(FALLBACK_COLORS)
    for candidate in OMARCHY_COLORS:
        try:
            text = Path(candidate).expanduser().read_text()
        except OSError:
            continue
        # colors.toml is flat `key = "#rrggbb"` lines, so no toml parser is needed.
        colors.update(re.findall(r'^\s*(\w+)\s*=\s*"(#[0-9a-fA-F]{3,8})"', text, re.M))
        break
    return colors


def style_window(root, ttk, tkfont):
    """Dress the window in the desktop's own colours. Returns the colours used."""
    c = theme_colors()
    bg, surface = c["background"], c.get("lighter_background", c["background"])
    fg, dim = c["foreground"], c.get("dark_foreground", c["muted"])
    accent, selection = c["accent"], c.get("selection", c["accent"])

    installed = set(tkfont.families())
    family = next((f for f in FONTS if f in installed), tkfont.nametofont("TkDefaultFont")["family"])
    for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont"):
        tkfont.nametofont(name).configure(family=family, size=10)

    style = ttk.Style(root)
    style.theme_use("clam")            # The only stock theme whose colours can be set.
    root.configure(background=bg)
    style.configure(".", background=bg, foreground=fg, fieldbackground=surface,
                    bordercolor=surface, lightcolor=surface, darkcolor=surface,
                    troughcolor=surface, arrowcolor=fg, insertcolor=fg,
                    focuscolor=accent, borderwidth=0)
    style.configure("TLabel", padding=(0, 2))
    style.configure("Title.TLabel", foreground=accent, font=(family, 13, "bold"))
    style.configure("Hint.TLabel", foreground=c.get("yellow", accent))
    style.configure("Tip.TLabel", background=surface, foreground=fg, padding=(9, 6))
    for widget in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(widget, padding=6, insertwidth=1)
        style.map(widget, fieldbackground=[("readonly", surface), ("disabled", bg)],
                  foreground=[("disabled", dim)], arrowcolor=[("", accent)])
    style.configure("TButton", padding=(14, 7), background=surface, relief="flat")
    style.map("TButton", background=[("active", selection), ("disabled", bg)],
              foreground=[("disabled", dim)])
    # The primary action gets the accent, the way the rest of the desktop marks one.
    style.configure("Accent.TButton", background=accent, foreground=bg)
    style.map("Accent.TButton", background=[("active", selection), ("disabled", surface)],
              foreground=[("disabled", dim)])
    style.configure("TCheckbutton", padding=(0, 4), indicatorsize=13, indicatormargin=(0, 0, 8, 0),
                    indicatorbackground=surface, indicatorforeground=bg)
    style.map("TCheckbutton", indicatorbackground=[("selected", accent), ("active", selection)],
              indicatorforeground=[("selected", bg)])
    style.configure("TProgressbar", background=accent, troughcolor=surface, thickness=6)
    # The combobox popup is a plain Tk listbox and only listens to the option database.
    for option, value in (("background", surface), ("foreground", fg),
                          ("selectBackground", accent), ("selectForeground", bg)):
        root.option_add("*TCombobox*Listbox." + option, value)
    return c


def show_hidden_files(root, tk):
    """Make the file dialog show dot-directories, and offer a checkbox to hide them again.

    Tk hides them by default, which puts ~/.local and ~/.steam out of reach, and that is
    where both WeMod and the prefixes live.
    """
    try:
        root.tk.call("catch", "tk_getOpenFile -badoption")   # Loads the dialog's own code.
        root.tk.call("set", "::tk::dialog::file::showHiddenBtn", "1")
        root.tk.call("set", "::tk::dialog::file::showHiddenVar", "1")
    except tk.TclError:                                      # A native dialog, nothing to do.
        pass


def gui(appid=None, launch=False):
    """Configuration window. Returns "cancel", "save" or "run".

    tkinter is imported here and nowhere else: the launch path must keep working on
    machines without it, and inside the runtime container it does not exist at all.
    """
    import queue as queuelib
    import threading
    import tkinter as tk
    from tkinter import font as tkfont, ttk, filedialog

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
    root.minsize(620, 0)
    colors = style_window(root, ttk, tkfont)
    show_hidden_files(root, tk)
    frame = ttk.Frame(root, padding=20)
    frame.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)      # A tiling WM hands out more space than we asked for.
    frame.columnconfigure(1, weight=1)
    result = {"action": "cancel"}
    row = 0

    def tooltip(widget, text):
        """Tk has no tooltips, and this is the smallest thing that behaves like one."""
        tip = {"window": None, "timer": None}

        def show():
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.configure(background=colors["accent"])        # Serves as a 1px border.
            ttk.Label(win, text=text, style="Tip.TLabel", wraplength=340).pack(padx=1, pady=1)
            win.geometry("+%d+%d" % (widget.winfo_rootx(),
                                     widget.winfo_rooty() + widget.winfo_height() + 6))
            tip["window"] = win

        def enter(_=None):
            hide()
            tip["timer"] = widget.after(450, show)

        def hide(_=None):
            if tip["timer"]:
                widget.after_cancel(tip["timer"])
                tip["timer"] = None
            if tip["window"]:
                tip["window"].destroy()
                tip["window"] = None

        widget.bind("<Enter>", enter, add="+")
        widget.bind("<Leave>", hide, add="+")
        widget.bind("<ButtonPress>", hide, add="+")
        root.bind("<Destroy>", hide, add="+")

    def add(text, widget, extra_widget=None):
        nonlocal row
        ttk.Label(frame, text=text).grid(row=row, column=0, sticky="w", pady=(0, 10), padx=(0, 14))
        widget.grid(row=row, column=1, sticky="ew", pady=(0, 10))
        if extra_widget is not None:
            extra_widget.grid(row=row, column=2, sticky="w", padx=(10, 0), pady=(0, 10))
        row += 1

    ttk.Label(frame, text="Coproton", style="Title.TLabel").grid(
        row=row, column=0, columnspan=3, sticky="w", pady=(0, 16))
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

    # One row per program: as many as the game needs, all started in this order.
    programs_box = ttk.Frame(frame)
    programs_box.columnconfigure(0, weight=1)
    rows = []

    def redraw_rows():
        for i, item in enumerate(rows):
            item["entry"].grid(row=i, column=0, sticky="ew", pady=(0, 6))
            item["args_entry"].grid(row=i, column=1, sticky="ew", padx=(8, 0), pady=(0, 6))
            item["browse"].grid(row=i, column=2, padx=(8, 0), pady=(0, 6))
            item["remove"].grid(row=i, column=3, padx=(6, 0), pady=(0, 6))
        toolbar.grid(row=len(rows), column=0, columnspan=4, sticky="w", pady=(2, 0))

    def add_row(path="", args=""):
        item = {"path_var": tk.StringVar(value=path), "args_var": tk.StringVar(value=args)}
        item["entry"] = ttk.Entry(programs_box, textvariable=item["path_var"])
        item["args_entry"] = ttk.Entry(programs_box, textvariable=item["args_var"], width=14)
        item["browse"] = ttk.Button(programs_box, text="Browse...",
                                    command=lambda: browse(item))
        item["remove"] = ttk.Button(programs_box, text="\u00d7", width=2,
                                    command=lambda: drop_row(item))
        item["path_var"].trace_add("write", lambda *_: refresh_hint())
        tooltip(item["args_entry"], "Arguments for this program, split the way a shell "
                                    "would. Electron programs usually need --disable-gpu. "
                                    "Leading NAME=value pairs, again as in a shell, set "
                                    "environment for this program alone.")
        tooltip(item["remove"], "Drop this program from the list.")
        rows.append(item)
        redraw_rows()
        return item

    def drop_row(item):
        for key in ("entry", "args_entry", "browse", "remove"):
            item[key].destroy()
        rows.remove(item)
        if not rows:                     # Always leave one row to type into.
            add_row()
        redraw_rows()
        refresh_hint()

    def free_row():
        """A row with nothing in it, new if every existing one is taken."""
        return next((item for item in rows if not item["path_var"].get().strip()), None) \
            or add_row()

    def browse(item):
        start = all_games.get(current_game(), {}).get("dir") or str(Path.home())
        chosen = filedialog.askopenfilename(
            parent=root, title="Select a program to run with the game",
            initialdir=start if Path(start).is_dir() else str(Path.home()),
            filetypes=[("Windows executables", "*.exe"), ("All files", "*")])
        if chosen:
            item["path_var"].set(chosen)
            refresh_hint()

    def use_wemod():
        """Fetch WeMod on first use, then put it in a row of its own."""
        def picked(exe):
            if exe:
                free_row()["path_var"].set(exe)
                dotnet_var.set(True)   # The net45 build needs the framework, not wine-mono.
                refresh_hint()
        exe = wemod_exe()
        if exe:
            picked(exe)
            return
        run_busy("Setting up WeMod, this downloads about 160 MB...",
                 lambda report, stop: install_wemod(report=report, stop=stop),
                 picked, cancellable=True)

    toolbar = ttk.Frame(programs_box)
    plus_button = ttk.Button(toolbar, text="+", width=2, command=lambda: add_row())
    plus_button.grid(row=0, column=0)
    wemod_button = ttk.Button(toolbar, text="WeMod", command=use_wemod)
    wemod_button.grid(row=0, column=1, padx=(6, 0))
    add("Programs", programs_box)
    tooltip(plus_button, "Add another program. They all start together with the game, in "
                         "the order listed, and share its prefix.")
    tooltip(wemod_button, "Download WeMod and add it to the list. It needs the real .NET "
                          "Framework, so the box below is ticked for it.")

    dotnet_var = tk.BooleanVar()
    ttk.Checkbutton(frame, text="Install .NET 4.8 into the prefix (rarely needed)",
                    variable=dotnet_var).grid(row=row, column=1, sticky="w", pady=(0, 8))
    row += 1

    delay_var = tk.StringVar(value=str(DEFAULT_DELAY))
    add("Delay, seconds", ttk.Spinbox(frame, from_=0, to=600, textvariable=delay_var, width=8))

    spinner = ttk.Progressbar(frame, mode="indeterminate")
    spinner.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(0, 8))
    spinner.grid_remove()        # Keeps the row; the bar only shows while a job runs.
    row += 1

    hint = ttk.Label(frame, text="", wraplength=560, style="Hint.TLabel")
    hint.grid(row=row, column=0, columnspan=3, sticky="nw", pady=(2, 12))
    frame.rowconfigure(row, weight=1)   # Spare height goes here, keeping the buttons down.
    row += 1

    def chosen_proton():
        picked = proton_var.get()
        return "" if picked == follow else all_protons.get(picked, "")

    def current_game():
        picked = game_var.get()
        for gid in order:
            if label(gid) == picked:
                return gid
        return ""

    def listed_programs():
        return [{"path": unquote_path(item["path_var"].get()),
                 "args": item["args_var"].get().strip()}
                for item in rows if unquote_path(item["path_var"].get())]

    def refresh_hint():
        notes = []
        paths = [item["path"] for item in listed_programs()]
        missing = [p for p in paths if not Path(p).exists()]
        if missing:
            notes.append("Not found: %s" % ", ".join(Path(p).name for p in missing))
        # Warn about ticking the box, not about leaving it alone: almost every .NET
        # program runs on the wine-mono that Proton already ships, and installing .NET
        # removes it.
        if dotnet_var.get():
            if not shutil.which("winetricks"):
                notes.append("winetricks is not installed, .NET cannot be installed.")
            elif wemod_exe() in paths:
                notes.append("WeMod is a net45 build and does need the real framework, "
                             "so leave this ticked.")
            else:
                notes.append("Rarely needed: .NET programs, WPF included, normally run on "
                             "the wine-mono Proton ships, and installing .NET removes it. "
                             "Tick this only if a program fails without it.")
        hint.config(text="  ".join(notes))

    def load_game(*_):
        c = cfg.get(current_game(), {})
        chosen = c.get("proton")
        proton_var.set(next((n for n, p in all_protons.items() if p == chosen), follow)
                       if chosen else follow)
        for item in list(rows):
            for key in ("entry", "args_entry", "browse", "remove"):
                item[key].destroy()
            rows.remove(item)
        for program in entry_programs(c):
            add_row(program["path"], program["args"])
        if not rows:
            add_row()
        dotnet_var.set(bool(c.get("dotnet")))
        delay_var.set(str(c.get("delay", DEFAULT_DELAY)))
        refresh_hint()

    game_box.bind("<<ComboboxSelected>>", load_game)
    dotnet_var.trace_add("write", lambda *_: refresh_hint())

    busy = {"stop": None}

    def set_busy(stop):
        """Lock the window while a job runs. stop is None when nothing is running."""
        running = stop is not None
        busy["stop"] = stop
        save_button.state(["disabled"] if running else ["!disabled"])
        patch_button.state(["disabled"] if running else ["!disabled"])
        update_button.state(["disabled"] if running else ["!disabled"])
        run_button.state(["disabled"] if running or not launch else ["!disabled"])
        # Cancel doubles as the stop button, but only for jobs that can be stopped.
        cancel_button.state(["disabled"] if running and not stop.settable else ["!disabled"])
        cancel_button.config(text="Stop" if running and stop.settable else "Cancel")
        root.config(cursor="watch" if running else "")
        if running:
            spinner.grid()
            spinner.start(12)
        else:
            spinner.stop()
            spinner.grid_remove()

    def run_busy(message, work, done, cancellable=False):
        """Run work(report, stop) on a worker thread and keep the window alive.

        Tk is not thread safe, so the worker only puts text on a queue; the window is
        touched from the Tk thread alone, by the poller below.
        """
        stop = threading.Event()
        stop.settable = cancellable        # Whether Cancel may interrupt this job.
        messages = queuelib.Queue()
        outcome = {}

        def worker():
            try:
                outcome["value"] = work(messages.put, stop)
            except Exception as exc:       # A crash must not leave the window locked.
                outcome["error"] = exc
            messages.put(None)

        def poll():
            while True:
                try:
                    message = messages.get_nowait()
                except queuelib.Empty:
                    root.after(100, poll)
                    return
                if message is None:
                    set_busy(None)
                    if "error" in outcome:
                        hint.config(text="Failed: %s" % outcome["error"])
                    done(outcome.get("value"))
                    return
                hint.config(text=message)

        hint.config(text=message)
        set_busy(stop)
        threading.Thread(target=worker, daemon=True).start()
        poll()

    def store():
        """Write the fields into the config. Returns the game id, or None."""
        gid = current_game()
        if not gid:
            hint.config(text="Pick a game first.")
            return None
        entry = cfg.setdefault(gid, {})
        was = entry.get("dotnet")
        entry.update(proton=chosen_proton(),
                     programs=listed_programs(),
                     dotnet=dotnet_var.get(),
                     delay=int(delay_var.get() or 0))
        for old_key in ("program", "args"):   # Replaced by the list; keep one truth.
            entry.pop(old_key, None)
        if was != entry["dotnet"]:
            entry.pop("dotnet_done", None)
        save(cfg)
        return gid

    def close(action):
        result["action"] = action
        root.destroy()

    def finish(action):
        if action == "cancel":
            close(action)
            return
        gid = store()
        if gid is None:
            return
        entry = cfg[gid]
        if entry["dotnet"] and not entry.get("dotnet_done"):
            proton = resolve_proton(entry)[0]

            def installed(ok):
                if ok:
                    entry["dotnet_done"] = True
                    save(cfg)
                close(action)      # A failed install is in the log; the launch goes ahead.

            # .NET goes into the prefix with the wine of whichever Proton will run it.
            run_busy("Installing .NET, this takes a few minutes...",
                     lambda report, stop: install_dotnet(proton, gid, report=report),
                     installed)
            return
        close(action)

    def run_patcher():
        """Run a patcher or installer in the game's prefix, without starting the game."""
        gid = current_game()
        if not gid:
            hint.config(text="Pick a game first.")
            return
        start = all_games.get(gid, {}).get("dir") or str(Path.home())
        chosen = filedialog.askopenfilename(
            parent=root, title="Run a program in the game's prefix",
            initialdir=start if Path(start).is_dir() else str(Path.home()),
            filetypes=[("Windows executables", "*.exe"), ("All files", "*")])
        if not chosen:
            return
        proton = resolve_proton({"proton": chosen_proton()})[0]
        if not proton:
            hint.config(text="No Proton found.")
            return
        run_busy("Starting %s..." % Path(chosen).name,
                 lambda report, stop: run_in_prefix(proton, gid, unquote_path(chosen),
                                                    report=report, stop=stop),
                 lambda _: None, cancellable=True)

    def cancel_clicked():
        stop = busy["stop"]
        if stop is None:
            finish("cancel")
        elif stop.settable:
            stop.set()

    # One bar across the whole width, so the buttons cannot widen the label column.
    bottom = ttk.Frame(frame)
    bottom.grid(row=row, column=0, columnspan=3, sticky="ew")
    bottom.columnconfigure(1, weight=1)
    bottom_left = ttk.Frame(bottom)
    bottom_left.grid(row=0, column=0, sticky="w")
    patch_button = ttk.Button(bottom_left, text="Run in prefix...", command=run_patcher)
    patch_button.grid(row=0, column=0)
    update_button = ttk.Button(bottom_left, text="Update", command=lambda: run_busy(
        "Looking for a newer Coproton...",
        lambda report, stop: update_self(report=report, stop=stop), lambda _: None))
    update_button.grid(row=0, column=1, padx=(8, 0))
    buttons = ttk.Frame(bottom)
    buttons.grid(row=0, column=2, sticky="e")
    cancel_button = ttk.Button(buttons, text="Cancel", command=cancel_clicked)
    cancel_button.grid(row=0, column=0, padx=4)
    save_button = ttk.Button(buttons, text="Save", command=lambda: finish("save"))
    save_button.grid(row=0, column=1, padx=4)
    run_button = ttk.Button(buttons, text="Save and Run", style="Accent.TButton",
                            command=lambda: finish("run"))
    run_button.grid(row=0, column=2, padx=(4, 0))
    if not launch:
        run_button.state(["disabled"])            # Nothing to run outside a Steam launch.

    tooltip(update_button, "Fetch the newest Coproton from GitHub and replace this one. "
                           "Settings and games are untouched; reopen the window afterwards.")
    tooltip(patch_button, "Pick a program and run it in this game's wine prefix right now, "
                          "without starting the game: a patcher, a mod installer, a config "
                          "tool. Its output goes to the log.")
    tooltip(cancel_button, "Close without saving; the game is not started. While something "
                           "is downloading it turns into Stop and abandons it.")
    tooltip(save_button, "Save these settings for this game and close. The game is not "
                         "started.")
    tooltip(run_button, "Save and start the game with the program alongside it. Available "
                        "only when Steam is launching the game." if not launch else
                        "Save and start the game, with the program alongside it.")

    if appid and appid in all_games:
        game_var.set(label(appid))
        game_box.state(["disabled"])              # Steam already chose the game.
    elif order:
        game_var.set(label(order[0]))
    load_game()

    root.bind("<Escape>", lambda *_: cancel_clicked())
    root.protocol("WM_DELETE_WINDOW", cancel_clicked)
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


def check_wemod_profile():
    """One shared profile: the first prefix seeds it, the next one just links to it."""
    global WEMOD_PROFILE
    import tempfile
    kept_profile, kept_env = WEMOD_PROFILE, os.environ.get("STEAM_COMPAT_DATA_PATH")
    quiet = lambda *_: None
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        WEMOD_PROFILE = tmp / "shared"

        def roaming(name, contents=None):
            pfx = tmp / name
            wemod = pfx / "pfx/drive_c/users/steamuser/AppData/Roaming/WeMod/App"
            if contents is not None:
                wemod.mkdir(parents=True)
                (wemod / "init.json").write_text(contents)
            os.environ["STEAM_COMPAT_DATA_PATH"] = str(pfx)
            return wemod.parent

        try:
            first = roaming("one", '{"login": "yes"}')
            assert share_wemod_profile(quiet), "the first prefix should link"
            assert first.is_symlink(), "the profile was not replaced by a link"
            assert (WEMOD_PROFILE / "App/init.json").read_text() == '{"login": "yes"}', \
                "the existing login should seed the shared profile"

            second = roaming("two", "{}")            # A second game, its own throwaway login.
            assert share_wemod_profile(quiet), "the second prefix should link"
            assert second.is_symlink() and second.resolve() == WEMOD_PROFILE.resolve()
            assert (WEMOD_PROFILE / "App/init.json").read_text() == '{"login": "yes"}', \
                "the shared login must not be overwritten by a later prefix"

            assert share_wemod_profile(quiet), "linking again must be a no-op"
            third = roaming("three")                 # A prefix WeMod has never run in.
            assert share_wemod_profile(quiet) and third.is_symlink()

            os.environ.pop("STEAM_COMPAT_DATA_PATH")
            assert not share_wemod_profile(quiet), "outside a launch there is no prefix"
        finally:
            WEMOD_PROFILE = kept_profile
            os.environ.pop("STEAM_COMPAT_DATA_PATH", None)
            if kept_env is not None:
                os.environ["STEAM_COMPAT_DATA_PATH"] = kept_env


def check_theme():
    """Colours come out of the desktop's own flat colors.toml, or Tokyo Night if absent."""
    global OMARCHY_COLORS
    import tempfile
    kept = OMARCHY_COLORS
    with tempfile.TemporaryDirectory() as tmp:
        palette = Path(tmp) / "colors.toml"
        palette.write_text('mode = "dark"\n\naccent = "#d32f2f"\n'
                           'background = "#121212"\nfont = "JetBrainsMono"\n')
        try:
            OMARCHY_COLORS = (str(Path(tmp) / "missing.toml"), str(palette))
            colors = theme_colors()
            assert colors["accent"] == "#d32f2f" and colors["background"] == "#121212"
            assert "font" not in colors, "only colours may be picked up"
            # Keys the theme does not define keep their default.
            assert colors["foreground"] == FALLBACK_COLORS["foreground"]
            OMARCHY_COLORS = (str(Path(tmp) / "missing.toml"),)
            assert theme_colors() == FALLBACK_COLORS, "no theme means the built-in palette"
        finally:
            OMARCHY_COLORS = kept


def check_is_launch():
    """Command lines taken from a real Steam launch: the game counts, the chore does not."""
    evaluator = ["/home/ds/.local/share/Steam/legacycompat/iscriptevaluator.exe",
                 "legacycompat\\evaluatorscript_1693980.vdf"]
    assert not is_launch("run", evaluator), "the install script evaluator is not a launch"
    game = "/mnt/d/SteamLibrary/steamapps/common/Dead Space (2023)/Dead Space.exe"
    assert is_launch("waitforexitandrun", [game])
    # Some games are started through a handler URL rather than a path.
    assert is_launch("waitforexitandrun", ["link2ea://launchgame/1238860?platform=steam"])
    assert is_launch("run", ["/games/trainer.exe"]), "the run verb still starts things"
    assert not is_launch("getcompatpath", ["C:/windows"])


def check_run_in_prefix():
    """A patcher gets the game's prefix and the Proton's own wine, and no prefix says so."""
    global prefix_path, LOG
    import tempfile
    kept_prefix, kept_log = prefix_path, LOG
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wine, calls = tmp / "pt" / "files" / "bin" / "wine", tmp / "calls"
        wine.parent.mkdir(parents=True)
        wine.write_text('#!/bin/sh\necho "$@" >> "%s"\necho "$WINEPREFIX" >> "%s"\nexit 3\n'
                        % (calls, calls))
        wine.chmod(0o755)
        proton = tmp / "pt" / "proton"
        proton.touch()
        pfx = tmp / "compatdata" / "1234"
        (pfx / "pfx").mkdir(parents=True)
        (pfx / "pfx" / "system.reg").touch()
        patcher = tmp / "patch.exe"
        patcher.touch()
        LOG = tmp / "log"
        quiet = lambda *_: None
        try:
            prefix_path = lambda appid: pfx
            assert not run_in_prefix(str(proton), "1234", str(patcher), report=quiet), \
                "a non-zero exit code is a failure"
            lines = calls.read_text().splitlines()
            assert lines[0] == str(patcher), lines
            assert lines[1] == str(pfx / "pfx"), "the game's prefix must be used: %s" % lines
            prefix_path = lambda appid: None
            assert not run_in_prefix(str(proton), "1234", str(patcher), report=quiet), \
                "without a prefix there is nothing to run in"
        finally:
            prefix_path, LOG = kept_prefix, kept_log


def check_entry_programs():
    """The list of programs, including configs written before there was a list."""
    old = {"program": '"/games/My Trainer.exe"', "args": "--flag"}
    assert entry_programs(old) == [{"path": "/games/My Trainer.exe", "args": "--flag"}]
    assert entry_programs({}) == []
    assert entry_programs({"program": ""}) == []
    listed = {"programs": [{"path": "/a.exe", "args": ""}, {"path": " ", "args": "x"},
                           {"path": "'/b.exe'", "args": "--gpu"}]}
    assert entry_programs(listed) == [{"path": "/a.exe", "args": ""},
                                      {"path": "/b.exe", "args": "--gpu"}], \
        "blank rows are dropped and quotes cleaned"
    # The list wins: it is what the window writes now.
    assert entry_programs({"program": "/old.exe", "programs": []}) == []


def check_update_self():
    """Only a download that parses, and actually differs, is installed."""
    global UPDATE_BASE
    import tempfile
    kept = UPDATE_BASE
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        installed, served = tmp / "installed", tmp / "served"
        installed.mkdir()
        served.mkdir()
        UPDATE_BASE = served.as_uri() + "/"
        target = installed / "launcher.py"
        manifest, tool = "toolmanifest.vdf", "compatibilitytool.vdf"
        said = []

        def serve(launcher, first='"manifest" { "version" "2" }', second='"tool" { }'):
            (served / "launcher.py").write_text(launcher)
            (served / manifest).write_text(first)
            (served / tool).write_text(second)

        try:
            serve("print('old')\n")
            for name in UPDATE_FILES:
                (installed / name).write_bytes((served / name).read_bytes())
            assert not update_self(report=said.append, target=target), "no change, no update"
            assert "already up to date" in said[-1], said

            serve("def broken(:\n")
            assert not update_self(report=said.append, target=target)
            assert target.read_text() == "print('old')\n", "a broken launcher must be refused"

            serve("print('old')\n", first="not a manifest at all")
            assert not update_self(report=said.append, target=target)
            assert (installed / manifest).read_text() == '"manifest" { "version" "2" }', \
                "a manifest that is not one must be refused"

            serve("print('new')\n", first='"manifest" { "version" "3" }')
            assert update_self(report=said.append, target=target)
            assert target.read_text() == "print('new')\n"
            assert (installed / manifest).read_text() == '"manifest" { "version" "3" }'
            assert (installed / tool).read_text() == '"tool" { }', "unchanged files are left"
            assert os.access(target, os.X_OK), "the launcher has to stay executable"
            assert not list(installed.glob("*.new")), "no temporary file may be left behind"
        finally:
            UPDATE_BASE = kept


def check_split_env():
    """Leading NAME=value tokens belong to the environment, the rest to the program."""
    env, args = split_env("WINEDLLOVERRIDES=winepulse.drv= --disable-gpu --flag=a")
    assert env == {"WINEDLLOVERRIDES": "winepulse.drv="}, env
    assert args == ["--disable-gpu", "--flag=a"], "a flag with = is not an assignment"
    assert split_env("") == ({}, [])
    assert split_env("--windowed") == ({}, ["--windowed"])
    # Two of them, and quoting works as in a shell.
    env, args = split_env('A=1 B="two words" prog.exe')
    assert env == {"A": "1", "B": "two words"} and args == ["prog.exe"], (env, args)
    # Only leading ones: anything after the first real argument is an argument.
    assert split_env("--flag NAME=value") == ({}, ["--flag", "NAME=value"])


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
        (tmp / "overlay.exe").touch()
        CONFIG, LOG = tmp / "config.json", tmp / "log"
        save({"3755078775": {"proton": str(stub), "delay": 0, "programs": [
            # A quoted path, exactly as a file manager hands it over.
            {"path": '"%s"' % (tmp / "trainer.exe"), "args": "--disable-gpu --flag=a b"},
            {"path": str(tmp / "overlay.exe"), "args": ""}]}})
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
        deadline, lines = time.time() + 4, []
        while time.time() < deadline:
            lines = sorted(calls.read_text().splitlines()) if calls.exists() else []
            if len(lines) == 4:
                break
            time.sleep(0.05)
    assert code == 7, code
    # Sorted, not in call order: with delay 0 the stubs append concurrently.
    assert len(lines) == 4, lines
    assert lines[0] == "getcompatpath /games/game.exe", lines
    assert lines[1].startswith("runinprefix ") and "overlay.exe" in lines[1], lines
    assert lines[2].startswith("runinprefix ") and "trainer.exe" in lines[2], lines
    assert '"' not in lines[2], "the quoted path must be cleaned before use: %s" % lines[2]
    # Electron programs need flags like --disable-gpu, split the way a shell would.
    assert lines[2].endswith("--disable-gpu --flag=a b"), lines[2]
    assert lines[3] == "waitforexitandrun /games/game.exe -windowed", lines


def selftest():
    check_parsers()
    check_runtime_manifest()
    check_steam_settings()
    check_deref()
    check_wemod_unpack()
    check_wemod_profile()
    check_theme()
    check_is_launch()
    check_run_in_prefix()
    check_entry_programs()
    check_split_env()
    check_update_self()
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
