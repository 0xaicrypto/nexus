# Releasing the desktop app

How to cut a new release of the **desktop-v2** Tauri client and get
installers to users.

> The legacy Avalonia client (`packages/desktop`) was removed from `main`;
> it lives at git tag `legacy/avalonia-final` if you need the old build
> instructions.

## TL;DR

```bash
# macOS (one-shot local build)
cd packages/desktop-v2
pnpm install
bash scripts/build-macos.sh
# → src-tauri/target/release/bundle/dmg/Nexus_*.dmg
```

For a versioned release:

```bash
cd packages/desktop-v2
DESKTOP_VERSION_OVERRIDE=0.1.18 bash scripts/build-macos.sh
```

## Versioning

The desktop source of truth is `packages/desktop-v2/package.json`.
`scripts/bump-version.mjs` writes the same version into:

- `package.json`
- `src-tauri/tauri.conf.json`
- `src-tauri/Cargo.toml`
- `src/lib/build-info.ts`

For day-to-day dev builds the script auto-bumps the patch component on
every `pnpm tauri:build`. For a tagged release, pin an exact version
with `DESKTOP_VERSION_OVERRIDE=X.Y.Z`.

## What gets built

| Platform | Command / path | Output |
|----------|----------------|--------|
| macOS    | `bash scripts/build-macos.sh` | `src-tauri/target/release/bundle/dmg/Nexus_*.dmg` |
| Arch Linux | `bash scripts/build-arch.sh` | `src-tauri/target/release/bundle/appimage/*.AppImage`, `*.deb`, `*.rpm` |
| Windows  | `pnpm tauri build` (on Windows) | `src-tauri/target/release/bundle/msi/*.msi`, `*.exe` |
| other Linux | `pnpm tauri build` (on Linux) | `src-tauri/target/release/bundle/appimage/*.AppImage`, `*.deb` |

`scripts/build-macos.sh` and `scripts/build-arch.sh` are fully automated;
they bootstrap system deps, Python, pnpm, Rust, install the three local
Python packages (`nexus-core`, `nexus`, `nexus-server`), PyInstaller-
bundle the backend, and finally run `pnpm tauri:build`.

## Manual build on Arch Linux

```bash
cd packages/desktop-v2

# One-time system prerequisites
sudo pacman -S --needed --noconfirm \
  base-devel python python-pip python-virtualenv pnpm rust cargo \
  webkit2gtk-4.1 libsoup3 openssl

# Build AppImage + deb + rpm
bash scripts/build-arch.sh

# For a tagged release, pin the version
DESKTOP_VERSION_OVERRIDE=0.1.18 bash scripts/build-arch.sh
```

Outputs:

```
src-tauri/target/release/bundle/appimage/Nexus_*.AppImage
src-tauri/target/release/bundle/deb/*.deb
src-tauri/target/release/bundle/rpm/*.rpm
```

Install locally:

```bash
# AppImage (no install needed, just executable)
chmod +x src-tauri/target/release/bundle/appimage/Nexus_*.AppImage
./src-tauri/target/release/bundle/appimage/Nexus_*.AppImage

# or pacman/DPKG/RPM
sudo pacman -U src-tauri/target/release/bundle/aur/*.tar.zst 2>/dev/null || true
sudo dpkg -i src-tauri/target/release/bundle/deb/*.deb 2>/dev/null || true
sudo rpm -i src-tauri/target/release/bundle/rpm/*.rpm 2>/dev/null || true
```

## CI note

There is no GitHub Actions workflow for desktop-v2 releases yet. The
legacy `release-desktop.yml` (Avalonia / .NET) was removed because it
pointed at the deleted `packages/desktop/` tree. Add a new Tauri-based
workflow when you want automated cross-platform builds.

## What your users see (unsigned builds)

These builds are **not code-signed**. Every OS pops a one-time warning.

### macOS

> **"Nexus.app cannot be opened because Apple cannot check it for
> malicious software"**
>
> Right-click `Nexus.app` → **Open** → confirm. Once.

If they get **"Nexus.app is damaged and can't be opened"**, the .dmg was
downloaded with a strict quarantine flag:

```bash
xattr -d com.apple.quarantine /Applications/Nexus.app
```

### Windows

> **"Windows protected your PC — Microsoft Defender SmartScreen
> prevented an unrecognized app from starting."**
>
> Click **More info** → **Run anyway**.

### Linux

```bash
chmod +x Nexus_*.AppImage
./Nexus_*.AppImage
```

If `dlopen failed: libfuse.so.2`, install FUSE:

- Arch: `sudo pacman -S fuse2`
- Debian/Ubuntu: `sudo apt install libfuse2` (or `libfuse2t64` on Ubuntu 24.04+).

### After it opens

The Welcome wizard asks for the Nexus server URL. The URL is persisted
in the per-user app data directory so users only enter it once.
