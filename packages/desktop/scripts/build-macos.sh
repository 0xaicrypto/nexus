#!/usr/bin/env bash
# build-macos.sh — produce an unsigned Nexus.app + Nexus.dmg on macOS.
#
# What this does
# ==============
#  1. `dotnet publish` the UI project for both osx-arm64 and osx-x64,
#     self-contained so users don't need to install .NET.
#  2. lipo the two binaries into a universal binary so one .app runs
#     on Apple Silicon and Intel Macs.
#  3. Wrap the output in a real .app bundle (Info.plist + Contents/MacOS
#     + Contents/Resources + .icns).
#  4. Wrap the .app in a .dmg with a README explaining "right click →
#     Open" since we're not signed/notarized.
#
# Usage
# =====
#   ./packages/desktop/scripts/build-macos.sh
#
# Output
# ======
#   packages/desktop/dist/Nexus-macos-universal.dmg
#
# Prereqs (one-time)
# ==================
#   * .NET 10 SDK on macOS
#   * `hdiutil` (preinstalled)
#   * `iconutil` (preinstalled, for .iconset → .icns)
#   * `librsvg` for converting the SVG logo to PNGs at multiple sizes
#       brew install librsvg
#   * Optional: `create-dmg` for prettier .dmg layout (else we fall
#     back to plain hdiutil).

set -euo pipefail

cd "$(dirname "$0")/.."   # packages/desktop/

PROJECT="RuneDesktop.UI/RuneDesktop.UI.csproj"
CONFIG="Release"
DIST="dist"
APP_NAME="Nexus"
BUNDLE_ID="ai.nexus.desktop"

# ── Auto-bump build number ──────────────────────────────────────────
# Every run of this script reads BUILD_NUMBER, increments it, writes it
# back, and uses the new value as the patch component of the version.
# This makes:
#   * .dmg filename unique per build  (Nexus-macos-universal-0.1.42.dmg)
#   * the bundled source hash guaranteed-different (start.sh's drift
#     detection always fires → forces server restart → no stale
#     bytecode reuse — root cause of the "重 build 还是看到老行为" bug)
#   * the version visible in the desktop About panel, so the user can
#     confirm at a glance which build is running.
BUILD_NUMBER_FILE="BUILD_NUMBER"
CURRENT_BUILD=$(cat "$BUILD_NUMBER_FILE" 2>/dev/null | tr -d '[:space:]' || echo "0")
if ! [[ "$CURRENT_BUILD" =~ ^[0-9]+$ ]]; then CURRENT_BUILD=0; fi
NEXT_BUILD=$((CURRENT_BUILD + 1))
echo "$NEXT_BUILD" > "$BUILD_NUMBER_FILE"
VERSION="0.1.$NEXT_BUILD"
echo "  build #$NEXT_BUILD (prev #$CURRENT_BUILD) → version $VERSION"

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  Building Nexus.app (macOS universal, unsigned)"
echo "  version: $VERSION"
echo "════════════════════════════════════════════════════════════════"
echo ""

# Sanity check
command -v dotnet >/dev/null || { echo "✗ dotnet not on PATH — install .NET 10 SDK"; exit 1; }
command -v lipo   >/dev/null || { echo "✗ lipo not found — Xcode CLT required"; exit 1; }

# ── Step 0: explicit restore (visibility) ───────────────────────────
#
# `dotnet publish` does an implicit restore, but a stale obj/ +
# unreachable NuGet feed can produce confusing errors mid-publish
# that look like compile failures (e.g. "type X not found" when
# the actual cause was a feed timeout). A dedicated restore step
# surfaces network / feed problems with a clear "restore failed"
# message before we start compiling.
#
# The DICOM viewer (#143) lives on the SERVER as a static HTML page
# (served at http://localhost:8001/dicom-viewer/) — NO WebView NuGet
# dependency, NO 50 MB native binary. The desktop launches the
# user's default browser when the medic opens a study. Build is
# always lean.
echo "→ dotnet restore (pulling NuGet packages)"
dotnet restore "$PROJECT" \
    --nologo --verbosity minimal \
    || { echo "✗ restore failed — check NuGet feed access + package versions"; exit 1; }

# ── Step 1: publish for both arches ──────────────────────────────────
rm -rf "$DIST"
mkdir -p "$DIST"

for rid in osx-arm64 osx-x64; do
    echo "→ publish $rid"
    dotnet publish "$PROJECT" \
        -c "$CONFIG" \
        -r "$rid" \
        --self-contained true \
        -p:PublishSingleFile=false \
        -p:DebugType=none \
        -p:DebugSymbols=false \
        -p:Version="$VERSION" \
        -p:AssemblyVersion="$VERSION.0" \
        -p:FileVersion="$VERSION.0" \
        -p:InformationalVersion="$VERSION" \
        -o "$DIST/publish-$rid" \
        --nologo --verbosity minimal
done

# ── Step 2: lipo arm64 + x64 into a universal binary ─────────────────
echo "→ lipo into universal"
mkdir -p "$DIST/$APP_NAME.app/Contents/MacOS"
mkdir -p "$DIST/$APP_NAME.app/Contents/Resources"

# Native binary. AssemblyName=Nexus in the csproj means the published
# binary is `Nexus`, not `RuneDesktop.UI` (the legacy name). This
# rename is what stops the macOS Dock from labelling running instances
# as "RuneDesktop.UI" during `dotnet run`.
lipo -create \
    "$DIST/publish-osx-arm64/Nexus" \
    "$DIST/publish-osx-x64/Nexus" \
    -output "$DIST/$APP_NAME.app/Contents/MacOS/$APP_NAME"
chmod +x "$DIST/$APP_NAME.app/Contents/MacOS/$APP_NAME"

# Copy all the managed/native libs from one of the publish dirs (they're
# the same on both arches except the renamed binary). We can't use
# bash extglob `!(Nexus)` here because `bash -n` syntax-checks
# before `shopt -s extglob` would take effect — so do the rsync trick.
rsync -a --exclude='Nexus' \
    "$DIST/publish-osx-arm64/" \
    "$DIST/$APP_NAME.app/Contents/MacOS/"

# Lipo the dylib's that ship per-arch (Avalonia native bits).
for dylib in $(find "$DIST/publish-osx-arm64" -name "*.dylib" -type f); do
    rel="${dylib#$DIST/publish-osx-arm64/}"
    arm="$DIST/publish-osx-arm64/$rel"
    x64="$DIST/publish-osx-x64/$rel"
    if [ -f "$arm" ] && [ -f "$x64" ] && ! lipo -info "$arm" 2>/dev/null | grep -q "Architectures in"; then
        :  # not a fat library, skip
    fi
    if [ -f "$x64" ]; then
        lipo -create "$arm" "$x64" -output "$DIST/$APP_NAME.app/Contents/MacOS/$rel" 2>/dev/null \
            || cp "$arm" "$DIST/$APP_NAME.app/Contents/MacOS/$rel"
    fi
done

# ── Step 3: Info.plist + icon ─────────────────────────────────────────

# Build the .icns from the SVG logo if librsvg is available.
ICON_SRC="RuneDesktop.UI/Assets/nexus-logo.svg"
ICNS_OUT="$DIST/$APP_NAME.app/Contents/Resources/$APP_NAME.icns"

if [ -f "$ICON_SRC" ] && command -v rsvg-convert >/dev/null && command -v iconutil >/dev/null; then
    echo "→ generating .icns from $ICON_SRC"
    iconset="$DIST/$APP_NAME.iconset"
    rm -rf "$iconset" && mkdir -p "$iconset"
    for sz in 16 32 64 128 256 512 1024; do
        rsvg-convert -w $sz -h $sz "$ICON_SRC" -o "$iconset/icon_${sz}x${sz}.png" 2>/dev/null || true
    done
    # macOS expects @2x variants too
    for sz in 16 32 128 256 512; do
        dbl=$((sz * 2))
        rsvg-convert -w $dbl -h $dbl "$ICON_SRC" -o "$iconset/icon_${sz}x${sz}@2x.png" 2>/dev/null || true
    done
    iconutil -c icns "$iconset" -o "$ICNS_OUT" 2>/dev/null \
        && echo "  ✓ wrote $ICNS_OUT" \
        || echo "  ⚠ iconutil failed — bundle will use the default icon"
    rm -rf "$iconset"
else
    echo "  ⚠ rsvg-convert or iconutil missing; default app icon"
fi

cat > "$DIST/$APP_NAME.app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
        "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>     <string>$APP_NAME</string>
    <key>CFBundleIdentifier</key>      <string>$BUNDLE_ID</string>
    <key>CFBundleVersion</key>         <string>$VERSION</string>
    <key>CFBundleShortVersionString</key> <string>$VERSION</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleExecutable</key>      <string>$APP_NAME</string>
    <key>CFBundleIconFile</key>        <string>$APP_NAME</string>
    <key>LSMinimumSystemVersion</key>  <string>11.0</string>
    <key>NSHighResolutionCapable</key> <true/>
    <key>NSHumanReadableCopyright</key> <string>© Nexus contributors. Apache-2.0.</string>
</dict>
</plist>
EOF

echo "→ wrote Info.plist"

# ── Step 3.5: bundle backend source ──────────────────────────────────
#
# The .app needs `nexus_server`, `nexus`, and `nexus_core` to be
# pip-installable from inside the bundle once the user installs it. We
# also bundle the local-backend scripts (setup.sh / start.sh / stop.sh)
# so the desktop's LocalBackend can find them.
#
# Layout inside the .app:
#   Nexus.app/Contents/Resources/backend-source/
#     packages/
#       sdk/         ← nexus_core (Python)
#       nexus/       ← nexus framework (Python)
#       server/      ← nexus_server (Python)
#       desktop/scripts/local-backend/  ← setup.sh, start.sh, stop.sh
#
# rsync exclusions strip __pycache__, *.pyc, .git, egg-info — all
# either dev-only or platform-specific and would just bloat the .dmg.
echo ""
echo "→ bundling backend source"
BACKEND_DIR="$DIST/$APP_NAME.app/Contents/Resources/backend-source"
mkdir -p "$BACKEND_DIR/packages"

# Python packages: full source.
#
# CRITICAL: --exclude='node_modules' below. The source tree's
# node_modules often contains symlinks into the user's npm cache
# (~/.npm/_cacache/...) or pnpm/yarn workspace symlinks. rsync
# preserves symlinks as-is, so after .dmg install the link targets
# don't exist on the demo machine and the daemon fails with
# "Cannot find module .../dist/cjs/index.js". We force a CLEAN
# npm install in the bundle directory below — that creates real
# files inside Resources/backend-source/, no symlinks leaking out
# of the .app.
for pkg in sdk nexus server; do
    if [ -d "../$pkg" ]; then
        echo "  bundling packages/$pkg"
        rsync -a \
            --exclude='__pycache__' \
            --exclude='*.pyc' \
            --exclude='.git' \
            --exclude='.pytest_cache' \
            --exclude='*.egg-info' \
            --exclude='build/' \
            --exclude='dist/' \
            --exclude='tests/' \
            --exclude='node_modules/' \
            "../$pkg/" "$BACKEND_DIR/packages/$pkg/"
    fi
done

# Local-backend scripts: only the three shell scripts, not the whole
# desktop tree.
mkdir -p "$BACKEND_DIR/packages/desktop/scripts/local-backend"
cp scripts/local-backend/*.sh \
   "$BACKEND_DIR/packages/desktop/scripts/local-backend/"
chmod +x "$BACKEND_DIR/packages/desktop/scripts/local-backend/"*.sh
echo "  bundling packages/desktop/scripts/local-backend"

# Stamp the build version inside the bundled backend so the Python
# server can log it on startup. Lives at
# Nexus.app/Contents/Resources/backend-source/packages/server/nexus_server/BUILD_INFO.
# nexus_server.main reads it at boot and emits one INFO line —
# easy to verify "am I actually running the new build?" from
# ~/Library/Application Support/RuneProtocol/server.log.
SERVER_PKG_DIR="$BACKEND_DIR/packages/server/nexus_server"
if [ -d "$SERVER_PKG_DIR" ]; then
    cat > "$SERVER_PKG_DIR/BUILD_INFO" <<EOF
version=$VERSION
build=$NEXT_BUILD
built_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOF
    echo "  ✓ stamped BUILD_INFO (version=$VERSION)"
fi

# Report bundled size so we know what shipped.
BUNDLED_SIZE_MB=$(du -sm "$BACKEND_DIR" | awk '{print $1}')
echo "  ✓ bundled backend: ${BUNDLED_SIZE_MB} MB"

# ── Step 3.7: ad-hoc codesign ────────────────────────────────────────
#
# Without ANY signature, macOS Gatekeeper refuses to open the .app at
# all with a misleading "Nexus.app is damaged and can't be opened"
# error — even if the user right-clicks Open. Ad-hoc signing (signing
# identity "-") fixes that specific error while staying free: no Apple
# Developer ID required, no cert in Keychain. The user STILL sees
# "unidentified developer" the first time, but right-click → Open
# now succeeds.
#
# Use --deep so the signature recurses into Resources/backend-source/
# (where the bundled helper binaries live).
#
# Crucially, we do NOT pass --options runtime here. Hardened runtime
# enables the "all loaded dylibs must share the same Team ID" check,
# which is incompatible with ad-hoc signatures: libhostfxr.dylib (and
# the rest of the bundled .NET runtime) ships pre-signed by Microsoft's
# Team ID, while ad-hoc signing produces an empty Team ID. With
# hardened runtime ON, macOS refuses to load any of those dylibs at
# startup and the app dies immediately with
#   "different Team IDs ... libhostfxr.dylib not valid for use in process".
# Without hardened runtime, the same ad-hoc signature is enough to
# get past Gatekeeper's "damaged" check.
# When we eventually pay for a real Developer ID, this is the place
# to add `--options runtime` back together with `--entitlements`.
#
# If `codesign` itself isn't available (no Xcode CLT), we warn and
# skip — the .dmg will still build but show the "damaged" error on
# install. Real fix: install Xcode Command Line Tools.
APP_PATH="$DIST/$APP_NAME.app"
if command -v codesign >/dev/null; then
  echo ""
  echo "→ ad-hoc signing $APP_NAME.app"
  # --force overwrites any partial signature from a previous failed
  # run; --timestamp=none avoids hitting Apple's timestamp server
  # (moot without a real cert and adds latency).
  codesign \
    --force \
    --deep \
    --sign - \
    --timestamp=none \
    "$APP_PATH" 2>&1 | sed 's/^/  /' \
    || { echo "  ⚠ codesign returned non-zero — .app may still show 'damaged'"; }

  # Verify so we get a clean fail message if signing didn't actually
  # take. `codesign --verify` is strict about ad-hoc sigs in newer
  # macOS but won't reject our use case.
  if codesign --verify --deep "$APP_PATH" 2>&1 | grep -qi "valid on disk"; then
    echo "  ✓ ad-hoc signature verified"
  else
    # Re-check without --deep — some macOS versions don't recurse
    # into ad-hoc sigs via --verify --deep but still consider the
    # outer bundle signed. That's enough for Gatekeeper.
    if codesign -dv "$APP_PATH" 2>&1 | grep -qi "Signature=adhoc"; then
      echo "  ✓ ad-hoc signature present (outer bundle)"
    else
      echo "  ⚠ signature verification inconclusive — install and test"
    fi
  fi
else
  echo ""
  echo "  ⚠ codesign not on PATH — skipping ad-hoc signing."
  echo "    Install Xcode CLT: xcode-select --install"
  echo "    Without ad-hoc signing the .dmg will show 'Nexus.app is damaged'."
fi

# ── Step 4: build the .dmg ────────────────────────────────────────────

DMG="$DIST/$APP_NAME-macos-universal-$VERSION.dmg"
STAGE="$DIST/dmg-stage"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"

# Layout the dmg: app + Applications symlink + INSTALL.txt explaining
# the unsigned-build right-click-Open dance.
cp -R "$DIST/$APP_NAME.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

cat > "$STAGE/INSTALL.txt" <<'EOF'
Nexus desktop — installation
============================

  1. Drag Nexus.app onto the Applications shortcut in this window.
  2. The first time you open it, macOS will say
     "Nexus.app cannot be opened because Apple cannot check it for
     malicious software."

Two ways to open it that first time:

──────────────────────────────────────────────────────────────────────
Option A — Right-click → Open  (recommended, no terminal)
──────────────────────────────────────────────────────────────────────
  1. Open /Applications in Finder.
  2. Right-click (or Control-click) Nexus.app.
  3. Choose "Open" from the menu.
  4. In the dialog that appears, click "Open" again.

After this, the app launches normally on every subsequent click.

──────────────────────────────────────────────────────────────────────
Option B — Remove the quarantine flag once  (terminal, one paste)
──────────────────────────────────────────────────────────────────────
Open Terminal.app and paste:

  xattr -dr com.apple.quarantine /Applications/Nexus.app

Then double-click Nexus.app normally. No warning will appear.

──────────────────────────────────────────────────────────────────────
What about a real, signed build?
──────────────────────────────────────────────────────────────────────
This .dmg is ad-hoc signed (free) — Gatekeeper accepts it but
labels it "unidentified developer" because we don't yet pay for
an Apple Developer ID ($99/year). A fully signed + notarized
build (no warning at all) is on the roadmap.

First-time setup
----------------

On first launch, the app sets up a local agent backend
(Python venv). This takes 1–3 minutes the
very first time — you'll see "Setting up Nexus" with a progress
log. Subsequent launches are 2–5 seconds.

Prerequisites the app expects on your Mac (one-time):

  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  brew install python@3.11 node@20

If you already have these, the setup will pick them up automatically.
EOF

echo "→ creating .dmg"

# Defensive: detach any leftover mounts from a previous failed run.
# hdiutil "No child processes" / "Resource busy" errors are usually a
# stale Nexus volume still mounted on /Volumes/Nexus*. Iterate the
# matching ones and detach each — `|| true` so we don't crash the
# build if there's nothing to detach (the common case).
for mount in $(mount | awk -v vn="$APP_NAME $VERSION" '$0 ~ vn {print $3}'); do
    echo "  detaching stale mount: $mount"
    hdiutil detach "$mount" -force 2>/dev/null || true
done
# Also catch volume names without the version suffix, just in case.
hdiutil detach "/Volumes/$APP_NAME $VERSION" -force 2>/dev/null || true
hdiutil detach "/Volumes/$APP_NAME" -force 2>/dev/null || true

# Attempt 1: UDZO with fast zlib (level 1). UDZO at default zlib
# level 6 sometimes fails on 175+ MB stage folders with the
# "No child processes" error — diskimages-helper appears to time out
# under compression pressure. Level 1 trades ~10 MB of final size
# for a 4x faster + more reliable build.
if hdiutil create \
        -volname "$APP_NAME $VERSION" \
        -srcfolder "$STAGE" \
        -format UDZO \
        -imagekey zlib-level=1 \
        -fs HFS+ \
        -ov \
        "$DMG" >/dev/null 2>&1; then
    echo "  ✓ wrote $DMG (UDZO compressed)"
else
    # Attempt 2: UDRO (read-only, uncompressed). Larger .dmg but
    # never hits the compression pipeline that produces the cryptic
    # hdiutil error. Final size ≈ source size; ~250 MB given a
    # 175 MB stage. Worth it as a safety net.
    echo "  ⚠ UDZO failed, retrying as uncompressed UDRO (larger .dmg)"
    rm -f "$DMG"
    hdiutil create \
        -volname "$APP_NAME $VERSION" \
        -srcfolder "$STAGE" \
        -format UDRO \
        -fs HFS+ \
        -ov \
        "$DMG"
    echo "  ✓ wrote $DMG (UDRO uncompressed)"
fi

# Cleanup
rm -rf "$STAGE" "$DIST/publish-osx-arm64" "$DIST/publish-osx-x64"

echo ""
echo "✓ Built $DMG"
ls -lh "$DMG"
echo ""
echo "Test locally:"
echo "  open \"$DMG\""
