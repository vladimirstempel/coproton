#!/usr/bin/env python3
"""Coproton - run a Steam game together with an arbitrary program (a trainer).

Modes:
    coproton                    browser GUI
    coproton --register         register as a Steam compatibility tool
    coproton --selftest         run the built-in checks
    coproton <verb> <cmd...>    compat tool entry point, invoked by Steam
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "coproton" / "config.json"
LOG = CONFIG.parent / "last.log"

STEAM_ROOTS = ("~/.steam/root", "~/.steam/steam", "~/.local/share/Steam",
               "~/.var/app/com.valvesoftware.Steam/data/Steam")
# Tools and runtimes that are not games.
NOT_A_GAME = re.compile(r"^(Proton|Steam Linux Runtime|Steamworks|SteamVR|.*Redistributable)")
DEFAULT_DELAY = 10


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


def parse_acf(text):
    """Pull appid, name and installdir out of an appmanifest_*.acf."""
    def field(k):
        m = re.search(r'"%s"\s+"([^"]*)"' % k, text)
        return m.group(1) if m else None
    return field("appid"), field("name"), field("installdir")


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
    """{appid: {name, dir, non_steam}} for games added to Steam via "Add a Non-Steam Game"."""
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
                exe = (entry.get("exe") or "").strip().strip('"')
                start = (entry.get("startdir") or "").strip().strip('"')
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


def find_exes(directory, limit=300):
    """Every .exe under the game directory - the candidates for a trainer."""
    out = []
    root = Path(directory)
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        if len(Path(dirpath).relative_to(root).parts) >= 4:
            dirnames.clear()
        for f in filenames:
            if f.lower().endswith(".exe"):
                out.append(str(Path(dirpath) / f))
                if len(out) >= limit:
                    return sorted(out)
    return sorted(out)


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


# ------------------------------------------------------- launching (compat tool)

def log(msg):
    print("[coproton] %s" % msg, file=sys.stderr, flush=True)
    try:
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


def install_dotnet(proton):
    """Run winetricks -q dotnet48 in the game prefix, using wine from the chosen Proton."""
    winetricks = shutil.which("winetricks")
    if not winetricks:
        log("winetricks not found, skipping .NET")
        return False
    data = os.environ.get("STEAM_COMPAT_DATA_PATH")
    wine = wine_bin(proton)
    if not data or not wine:
        log("no STEAM_COMPAT_DATA_PATH or no wine in this Proton, skipping .NET")
        return False
    pfx = Path(data) / "pfx"
    if not (pfx / "system.reg").exists():
        log("prefix is empty, letting Proton create it")
        subprocess.run([proton, "run", "cmd", "/c", "exit"], check=False)
    env = dict(os.environ,
               WINEPREFIX=str(pfx),
               WINE=wine,
               WINESERVER=wine_bin(proton, "wineserver") or "",
               WINEDLLOVERRIDES="mscoree=d",
               WINEDEBUG="-all")
    log("running winetricks -q dotnet48, this takes a while")
    ok = subprocess.run([winetricks, "-q", "dotnet48"], env=env).returncode == 0
    log(".NET installed" if ok else "winetricks returned an error")
    return ok


def shim(argv):
    verb, cmd = argv[0], argv[1:]
    appid = current_appid()
    cfg = load()
    entry = cfg.get(appid, {})
    proton = entry.get("proton") or default_proton()
    if not proton or not os.access(proton, os.X_OK):
        sys.exit("[coproton] no Proton found, configure it by running `coproton`")

    if entry.get("dotnet") and not entry.get("dotnet_done"):
        if install_dotnet(proton):
            entry["dotnet_done"] = True
            cfg[appid] = entry
            save(cfg)

    log("appid=%s proton=%s" % (appid or "?", Path(proton).parent.name))
    game = subprocess.Popen([proton, verb, *cmd])

    program, extra = entry.get("program"), None
    if program and Path(program).exists():
        time.sleep(entry.get("delay", DEFAULT_DELAY))
        if game.poll() is None:
            log("starting %s" % program)
            extra = subprocess.Popen([proton, "runinprefix", program],
                                     cwd=str(Path(program).parent))
    elif program:
        log("program not found: %s" % program)

    code = game.wait()
    if extra and extra.poll() is None:
        # ponytail: only the wrapper is killed, Proton reaps the wine processes on shutdown.
        extra.terminate()
    return code


# ------------------------------------------------------------------------- GUI

PAGE = r"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Coproton</title>
<style>
:root { color-scheme: dark light }
body { font: 15px/1.5 system-ui, sans-serif; max-width: 620px; margin: 40px auto; padding: 0 20px }
h1 { font-size: 20px; margin: 0 0 4px }
p.sub { margin: 0 0 28px; opacity: .6 }
label { display: block; margin: 18px 0 6px; font-weight: 600 }
select, input[type=text], input[type=number] {
  width: 100%; padding: 8px; font: inherit; box-sizing: border-box;
  border: 1px solid rgba(128,128,128,.5); border-radius: 6px; background: transparent; color: inherit }
.row { display: flex; align-items: center; gap: 8px; margin: 20px 0 }
.row input { width: auto }
.row label { margin: 0; font-weight: 400 }
button { font: inherit; padding: 9px 18px; border-radius: 6px; border: 1px solid rgba(128,128,128,.5);
  background: transparent; color: inherit; cursor: pointer }
button.primary { background: #4a7; border-color: #4a7; color: #041; font-weight: 600 }
details { margin: 20px 0; opacity: .8 }
summary { cursor: pointer }
#msg { margin-top: 18px; min-height: 22px; color: #4a7 }
.warn { padding: 10px 14px; border: 1px solid #c84; border-radius: 6px; margin-bottom: 20px }
</style>
<body>
<h1>Coproton</h1>
<p class="sub">A game and a trainer, sharing one wine prefix.</p>
<div id="warn"></div>

<label for="game">Game</label>
<select id="game"></select>

<label for="proton">Proton</label>
<select id="proton"></select>

<label for="program">Program to run alongside the game</label>
<input type="text" id="program" list="exes" placeholder="/path/to/trainer.exe" spellcheck="false">
<datalist id="exes"></datalist>

<div class="row">
  <input type="checkbox" id="dotnet">
  <label for="dotnet">Install .NET 4.8 into the prefix (requires winetricks)</label>
</div>

<details>
  <summary>Advanced</summary>
  <label for="delay">Delay before starting the program, seconds</label>
  <input type="number" id="delay" min="0" max="600">
</details>

<button class="primary" id="save">Save</button>
<button id="quit">Close</button>
<div id="msg"></div>

<script>
const D = __DATA__;
const $ = id => document.getElementById(id);

function fill(sel, entries, empty) {
  sel.innerHTML = "";
  if (empty) sel.append(new Option(empty, ""));
  for (const [value, text] of entries) sel.append(new Option(text, value));
}

fill($("game"),
     Object.entries(D.games).map(([id, g]) =>
       [id, g.name + (g.non_steam ? "  [non-Steam]" : "") + "  (" + id + ")"]),
     Object.keys(D.games).length ? null : "no games found");
fill($("proton"), Object.entries(D.protons).map(([name, path]) => [path, name]));

const warn = [];
if (!Object.keys(D.protons).length) warn.push("No Proton installation found.");
if (!D.winetricks) warn.push("winetricks is not installed, the .NET checkbox will do nothing.");
if (!D.registered) warn.push("Could not register with Steam, run <code>coproton --register</code>.");
$("warn").innerHTML = warn.length ? '<div class="warn">' + warn.join("<br>") + "</div>" : "";

async function loadGame() {
  const id = $("game").value;
  const c = D.config[id] || {};
  $("proton").value = c.proton || D.default_proton || "";
  $("program").value = c.program || "";
  $("dotnet").checked = !!c.dotnet;
  $("delay").value = c.delay ?? D.default_delay;
  $("exes").innerHTML = "";
  if (!id) return;
  const exes = await (await fetch("/exes?appid=" + id)).json();
  for (const e of exes) $("exes").append(new Option(e));
}

$("game").onchange = loadGame;

$("save").onclick = async () => {
  const id = $("game").value;
  if (!id) { $("msg").textContent = "Pick a game first."; return; }
  const body = { appid: id, proton: $("proton").value, program: $("program").value.trim(),
                 dotnet: $("dotnet").checked, delay: Number($("delay").value) || 0 };
  D.config[id] = await (await fetch("/save", { method: "POST", body: JSON.stringify(body) })).json();
  $("msg").textContent = "Saved. Now pick Coproton as the compatibility tool in the game properties.";
};

$("quit").onclick = () => { fetch("/quit"); $("msg").textContent = "You can close this tab."; };

loadGame();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, body, ctype="application/json"):
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "%s; charset=utf-8" % ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            data = {"games": games(), "protons": protons(), "config": load(),
                    "default_proton": default_proton(), "default_delay": DEFAULT_DELAY,
                    "winetricks": bool(shutil.which("winetricks")),
                    "registered": bool(register(quiet=True))}
            self.reply(PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False)), "text/html")
        elif url.path == "/exes":
            appid = (parse_qs(url.query).get("appid") or [""])[0]
            self.reply(json.dumps(find_exes(games().get(appid, {}).get("dir", ""))))
        elif url.path == "/quit":
            self.reply("{}")
            self.server.stop = True
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/save":
            return self.send_error(404)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        cfg = load()
        entry = cfg.setdefault(body["appid"], {})
        # Toggling the .NET checkbox clears the "already installed" marker.
        if entry.get("dotnet") != body["dotnet"]:
            entry.pop("dotnet_done", None)
        entry.update(proton=body["proton"], program=body["program"],
                     dotnet=body["dotnet"], delay=body["delay"])
        save(cfg)
        self.reply(json.dumps(entry, ensure_ascii=False))


def gui():
    register(quiet=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.stop = False
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    print("Coproton: %s  (Ctrl+C to quit)" % url)
    webbrowser.open(url)
    try:
        while not server.stop:
            server.handle_request()
    except KeyboardInterrupt:
        pass


# ------------------------------------------------------------------- selfchecks

def check_parsers():
    acf = '"AppState"{"appid" "220"\n"name" "Half-Life 2"\n"installdir" "Half-Life 2"}'
    assert parse_acf(acf) == ("220", "Half-Life 2", "Half-Life 2"), parse_acf(acf)
    assert parse_acf("{}") == (None, None, None)
    assert parse_paths('"path" "/mnt/games"\n"path" "/home/x/Steam"') == ["/mnt/games", "/home/x/Steam"]
    assert NOT_A_GAME.match("Proton 6.3") and NOT_A_GAME.match("Steam Linux Runtime 3.0")
    assert not NOT_A_GAME.match("Elden Ring")
    assert "__DATA__" in PAGE and PAGE.count("__DATA__") == 1


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
    # No appid field: fall back to the crc32 algorithm rather than dropping the game.
    assert shortcut_appid({"exe": '"/x/g.exe"', "appname": "G"}).isdigit()
    assert int(shortcut_appid({"exe": '"/x/g.exe"', "appname": "G"})) >= 0x80000000


def check_shim():
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
        # A non-Steam appid, to prove it is read from the compatdata path and not SteamAppId.
        save({"3755078775": {"proton": str(stub), "program": str(tmp / "trainer.exe"), "delay": 0}})
        os.environ.update(SteamAppId="0", STEAM_COMPAT_DATA_PATH=str(tmp / "3755078775"))
        try:
            code = shim(["waitforexitandrun", "/games/game.exe", "-windowed"])
        finally:
            CONFIG, LOG = saved_config, saved_log
            os.environ.clear()
            os.environ.update(saved_env)
        deadline, lines = time.time() + 3, []
        while time.time() < deadline:
            lines = sorted(calls.read_text().splitlines()) if calls.exists() else []
            if len(lines) == 2:
                break
            time.sleep(0.05)
    assert code == 7, code
    # Sorted, not in call order: with delay 0 both stubs append concurrently. The ordering
    # that matters (program after the game, and only while it lives) is the sleep + poll.
    assert len(lines) == 2, lines
    assert lines[0].startswith("runinprefix ") and lines[0].endswith("trainer.exe"), lines
    assert lines[1] == "waitforexitandrun /games/game.exe -windowed", lines


def selftest():
    check_parsers()
    check_shortcuts()
    check_shim()
    for name, path in protons().items():   # Real environment, when there is one.
        assert os.access(path, os.X_OK), name
    found = games()
    non_steam = sum(1 for g in found.values() if g["non_steam"])
    print("ok: %d protons, %d games (%d non-Steam), %d libraries"
          % (len(protons()), len(found), non_steam, len(libraries())))


def main():
    args = sys.argv[1:]
    if not args:
        return gui()
    if args[0] == "--register":
        return 0 if register() else 1
    if args[0] == "--selftest":
        return selftest()
    if args[0] in ("-h", "--help"):
        return print(__doc__)
    return shim(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
