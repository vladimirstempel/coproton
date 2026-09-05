#!/bin/sh
# Install Coproton. No root, no package manager, no dependencies.
#   curl -fsSL https://raw.githubusercontent.com/vladimirstempel/coproton/main/install.sh | sh
# or run ./install.sh from a clone of the repo.
set -eu

REPO="${COPROTON_REPO:-vladimirstempel/coproton}"
BRANCH="${COPROTON_BRANCH:-main}"
DEST="${COPROTON_DEST:-$HOME/.local/lib/coproton}"
BIN="$HOME/.local/bin"
FILES="launcher.py toolmanifest.vdf compatibilitytool.vdf coproton.desktop"

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required. Every distro that runs Steam ships it, install it the usual way." >&2
  exit 1
}

# Running from a clone of the repo rather than through curl | sh.
src=""
d=$(dirname -- "$0" 2>/dev/null || echo .)
if [ -f "$d/launcher.py" ]; then src=$(cd -- "$d" && pwd); fi

tmp=""
if [ -z "$src" ]; then
  command -v curl >/dev/null 2>&1 || { echo "curl is required." >&2; exit 1; }
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT INT TERM
  echo "Downloading $REPO ($BRANCH)..."
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" \
    | tar xz -C "$tmp" --strip-components=1
  src="$tmp"
fi

mkdir -p "$DEST" "$BIN"
for f in $FILES; do
  [ -f "$src/$f" ] || { echo "missing file: $f" >&2; exit 1; }
  cp "$src/$f" "$DEST/$f"
done
chmod +x "$DEST/launcher.py"
ln -sf "$DEST/launcher.py" "$BIN/coproton"

# Desktop entry, so the window can be opened without a terminal.
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
sed "s|__EXEC__|$BIN/coproton|" "$DEST/coproton.desktop" > "$APPS/coproton.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS" 2>/dev/null || true
fi

"$DEST/launcher.py" --register || true

# The configuration window needs Tk. Launching a game does not, so failing to install it
# is never fatal here: the game still starts with whatever settings were already saved.

# "<package>|<install command>" for the local package manager, empty if unknown.
tk_package() {
  if   command -v pacman       >/dev/null 2>&1; then echo "tk|pacman -S --needed --noconfirm tk"
  elif command -v apt-get      >/dev/null 2>&1; then echo "python3-tk|apt-get install -y python3-tk"
  elif command -v dnf          >/dev/null 2>&1; then echo "python3-tkinter|dnf install -y python3-tkinter"
  elif command -v zypper       >/dev/null 2>&1; then echo "python3-tk|zypper --non-interactive install python3-tk"
  elif command -v apk          >/dev/null 2>&1; then echo "python3-tkinter|apk add python3-tkinter"
  elif command -v xbps-install >/dev/null 2>&1; then echo "python3-tkinter|xbps-install -y python3-tkinter"
  fi
}

# Reads from /dev/tty, not stdin: under "curl | sh" stdin is the script itself.
confirm() {
  [ "${COPROTON_YES:-}" = "1" ] && return 0
  [ -e /dev/tty ] || return 1
  printf '%s [Y/n] ' "$1" >/dev/tty
  read -r answer </dev/tty || return 1
  case "$answer" in "" | y | Y | yes | YES) return 0 ;; *) return 1 ;; esac
}

if ! python3 -c "import tkinter" >/dev/null 2>&1; then
  echo
  echo "The configuration window needs python3 tkinter, which is not installed."
  spec=$(tk_package || true)
  package=${spec%%|*}
  install_cmd=${spec#*|}

  if [ "$(id -u)" -eq 0 ]; then
    sudo_cmd=""
  elif command -v sudo >/dev/null 2>&1; then
    sudo_cmd="sudo"
  else
    sudo_cmd="none"
  fi

  if [ -z "$spec" ]; then
    echo "Could not identify the package manager. Install the tkinter package for python3."
  elif command -v steamos-readonly >/dev/null 2>&1; then
    # SteamOS and friends: the rootfs is read-only and unlocking it is the user's call.
    echo "This looks like SteamOS, where the system image is read-only. To install it:"
    echo "  sudo steamos-readonly disable && sudo $install_cmd && sudo steamos-readonly enable"
  elif [ "$sudo_cmd" = "none" ]; then
    echo "sudo is not available. Install it as root:  $install_cmd"
  elif confirm "Install $package now?"; then
    # shellcheck disable=SC2086
    if $sudo_cmd $install_cmd && python3 -c "import tkinter" >/dev/null 2>&1; then
      echo "tkinter is ready."
    else
      echo "Installation did not succeed. Install it by hand:  $sudo_cmd $install_cmd"
    fi
  else
    echo "Skipped. Install it later with:  $sudo_cmd $install_cmd"
  fi
fi

case ":$PATH:" in
  *":$BIN:"*) cmd="coproton" ;;
  *) cmd="$BIN/coproton"
     echo
     echo "Warning: $BIN is not in your PATH." ;;
esac

echo
echo "Done. Configure with:  $cmd"
echo "Then restart Steam and pick Coproton in the game properties."
echo "Uninstall:            rm -rf '$APPS/coproton.desktop' '$DEST' '$BIN/coproton' ~/.steam/root/compatibilitytools.d/coproton"
