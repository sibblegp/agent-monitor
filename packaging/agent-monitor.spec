# PyInstaller spec for the Agent Monitor backend.
#
# Produces a self-contained `agent-monitor-backend` directory that Electron
# ships inside the .app and spawns instead of a system Python. Built as onedir
# rather than onefile: onefile unpacks itself to a temp directory on every
# launch, which shows up as a second or two of dead time before the window has
# anything to display.
#
# Build with:  pyinstaller packaging/agent-monitor.spec --noconfirm
#
# The one thing this cannot bundle is `git`. The app shells out to it for every
# diff, and it stays a runtime requirement — see electron/main.js, which checks
# for it and says so plainly rather than failing on the first scan.

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

# Paths in a spec resolve against the spec's own directory, not the project
# root, so anchor everything to SPECPATH rather than assuming a working dir.
PROJECT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [(os.path.join(PROJECT, "agent_monitor", "static"), "agent_monitor/static")]
binaries = []

# uvicorn resolves its protocol, loop, and lifespan implementations by string at
# runtime, so static analysis finds none of them.
hiddenimports = [
    *collect_submodules("uvicorn"),
    "websockets",
    "websockets.legacy",
    "anyio",
]

# Optional extras: bundle them when present, skip silently when not, so a
# minimal install still produces a working binary.
for optional in ("anthropic", "tree_sitter", "tree_sitter_language_pack"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(optional)
    except Exception:
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden


a = Analysis(
    ["backend_entry.py"],
    pathex=[PROJECT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here draws a GUI — the window is Electron's. Dropping these keeps
    # the bundle from carrying a Tk runtime it never loads.
    excludes=["tkinter", "matplotlib", "PIL", "numpy", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="agent-monitor-backend",
    debug=False,
    strip=False,
    upx=False,  # UPX-compressed binaries fail macOS code signing
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="agent-monitor-backend",
)
