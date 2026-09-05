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

# The window needs Tk. The launch path does not, so a missing Tk is a warning, not a failure.
if ! python3 -c "import tkinter" >/dev/null 2>&1; then
  echo
  echo "Warning: python3 tkinter is missing, the configuration window cannot open."
  if   command -v pacman >/dev/null 2>&1; then echo "  sudo pacman -S tk"
  elif command -v apt    >/dev/null 2>&1; then echo "  sudo apt install python3-tk"
  elif command -v dnf    >/dev/null 2>&1; then echo "  sudo dnf install python3-tkinter"
  elif command -v zypper >/dev/null 2>&1; then echo "  sudo zypper install python3-tk"
  else echo "  install the tkinter package for python3"
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
