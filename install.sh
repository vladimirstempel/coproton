#!/bin/sh
# Install Coproton. No root, no package manager, no dependencies.
#   curl -fsSL https://raw.githubusercontent.com/vladimirstempel/coproton/main/install.sh | sh
# or run ./install.sh from a clone of the repo.
set -eu

REPO="${COPROTON_REPO:-vladimirstempel/coproton}"
BRANCH="${COPROTON_BRANCH:-main}"
DEST="${COPROTON_DEST:-$HOME/.local/lib/coproton}"
BIN="$HOME/.local/bin"
FILES="launcher.py toolmanifest.vdf compatibilitytool.vdf"

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

"$DEST/launcher.py" --register || true

case ":$PATH:" in
  *":$BIN:"*) cmd="coproton" ;;
  *) cmd="$BIN/coproton"
     echo
     echo "Warning: $BIN is not in your PATH." ;;
esac

echo
echo "Done. Configure with:  $cmd"
echo "Then restart Steam and pick Coproton in the game properties."
echo "Uninstall:            rm -rf '$DEST' '$BIN/coproton' ~/.steam/root/compatibilitytools.d/coproton"
