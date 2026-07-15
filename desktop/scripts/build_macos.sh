#!/usr/bin/env bash
# Build the ad-hoc-signed macOS "AIZU Worker.app" with the onedir sidecar embedded.
#
# WHY the manual embed + re-sign (instead of Tauri `bundle.resources`): Tauri's resource
# copier fails on the PyInstaller ONEDIR tree (`_internal/Python` is a Mach-O/framework →
# "Not a directory"). So we build the app WITHOUT the sidecar resource, copy the onedir
# folder into the .app ourselves, then re-codesign the whole bundle ad-hoc (Apple Silicon
# requires every embedded Mach-O to carry at least an ad-hoc signature to execute).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENG="$ROOT/engine"
DESK="$ROOT/desktop"
VENV="$ENG/.venv/bin"
export PATH="$HOME/.cargo/bin:/opt/homebrew/bin:$PATH"

echo "[1/4] PyInstaller onedir sidecar (near-instant cold start)"
( cd "$ENG" && "$VENV/pyinstaller" --clean --noconfirm \
    --distpath "$DESK/pyinstaller/dist" --workpath "$DESK/pyinstaller/build" \
    "$DESK/pyinstaller/sidecar.spec" )

echo "[2/4] Tauri app bundle (no sidecar resource)"
( cd "$DESK/src-tauri" && tauri build --bundles app )

APP="$DESK/src-tauri/target/release/bundle/macos/AIZU Worker.app"
echo "[3/4] Embed onedir sidecar → Resources/sidecar/reelradar-worker/"
rm -rf "$APP/Contents/Resources/sidecar"
mkdir -p "$APP/Contents/Resources/sidecar"
cp -R "$DESK/pyinstaller/dist/reelradar-worker" "$APP/Contents/Resources/sidecar/reelradar-worker"

echo "[4/5] Re-sign the whole bundle ad-hoc"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP" && echo "    codesign OK (ad-hoc)"

# Install the COMPLETE, signed bundle to /Applications. This is the final step ON PURPOSE:
# earlier a bundle was copied to /Applications BEFORE the sidecar embed (step 3), yielding an
# app with no sidecar that launches but never connects. Copying only here guarantees the
# installed app is always complete + signed. Skip with NO_INSTALL=1.
if [[ "${NO_INSTALL:-0}" != "1" ]]; then
  DEST="/Applications/AIZU Worker.app"
  echo "[5/5] Install → $DEST"
  # Stop any running instance so we don't overwrite a bundle with mapped-in executables.
  pkill -f "$DEST" 2>/dev/null || true
  rm -rf "$DEST"
  cp -R "$APP" "$DEST"
  codesign --verify --deep --strict "$DEST" && echo "    installed + codesign OK (ad-hoc)"
else
  echo "[5/5] Skipped /Applications install (NO_INSTALL=1)"
fi

echo "Done: $APP"
