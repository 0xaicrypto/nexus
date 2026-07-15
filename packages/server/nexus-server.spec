# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for bundling nexus-server into a single Linux binary.

Used by packages/desktop-v2/scripts/build-*.sh to produce the Tauri sidecar.
"""
from PyInstaller.building.build_main import Analysis, PYZ, EXE
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# Collect package data that setuptools would normally ship in the wheel.
# Without this, PyInstaller drops ABI JSONs, starter pack assets, and the
# DICOM viewer static files, causing runtime 404s / ModuleNotFoundError.
datas = []
binaries = []
hiddenimports = []
for pkg in ("nexus_core", "nexus_server"):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas.extend(pkg_datas)
    binaries.extend(pkg_binaries)
    hiddenimports.extend(pkg_hiddenimports)

# FastAPI/Pydantic often need these at runtime even if not directly imported.
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "pydantic.deprecated.decorator",
    "pkg_resources",
]

a = Analysis(
    ["nexus_server/__main__.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="nexus-server-x86_64-unknown-linux-gnu",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
