# Unlocking WeMod Pro

> **You do this at your own risk.** The patch is a third party modification of WeMod's
> client. It goes against WeMod's terms of service, it can get your account suspended, and
> the binary it installs comes from a release that neither Coproton nor its author built or
> audited. Coproton does not ship, download or apply any of it, and the author of Coproton
> accepts no responsibility for what happens if you follow this document: not for your WeMod
> account, not for your games or prefixes, not for anything the patch does on your machine.
> Read what the tool does, decide for yourself, and keep your own backups.

Coproton does not patch WeMod and is not going to: it is a call for you to make, not
something an installer should make behind your back. What Coproton does do is let a patched
copy start: it launches WeMod with `WINEDLLOVERRIDES="version=n,b"`, which is the one piece
that cannot be done from outside the launcher.

[wemod_enhancer](https://github.com/e-gleba/wemod_enhancer) unlocks Pro, turns the updater
off, disables the mobile pairing prompt and adds F12 for DevTools. It rewrites
`resources/app.asar` and puts a proxy `version.dll` next to `WeMod.exe` that flips
Electron's integrity fuse inside the process.

Fetch WeMod through the **WeMod** button first, and close it before patching. Then, from
any directory you like:

```sh
curl -LO https://github.com/e-gleba/wemod_enhancer/releases/latest/download/wemod_enhancer-windows-llvm-mingw-amd64.tar.xz
mkdir -p wemod_enhancer
tar -xf wemod_enhancer-windows-llvm-mingw-amd64.tar.xz -C wemod_enhancer --strip-components=1
python3 wemod_enhancer/bin/wemod_enhancer.py patch --install-dir ~/.local/share/coproton/wemod-11.6.0
```

The Linux package is the right one even though the DLL inside it is a Windows binary: the
patcher runs on your Python, the DLL is for wine. There is nothing to configure afterwards.
Start the game as usual and WeMod comes up with Pro active.

To undo it, using the backups the patcher left beside the originals:

```sh
python3 wemod_enhancer/bin/wemod_enhancer.py restore --install-dir ~/.local/share/coproton/wemod-11.6.0
```

Two things to know:

* **A reinstall wipes the patch.** Delete `~/.local/share/coproton/wemod-11.6.0` and the
  **WeMod** button downloads a clean copy again, so patch it again after that.
* **`Integrity check failed for asar archive` in the log means the proxy did not load**, not
  that the patch is bad. Electron only says this when it read a rewritten `app.asar` while
  wine was still answering with its own builtin `version.dll`. Make sure the launcher
  Coproton actually runs is current: it lives in `~/.local/lib/coproton`, and editing the
  clone without running `./install.sh` leaves the old one in place.
