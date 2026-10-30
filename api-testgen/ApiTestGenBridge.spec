# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import copy_metadata, collect_submodules

datas = []
datas += copy_metadata('Flask')
datas += copy_metadata('flask-cors')
datas += copy_metadata('flask-socketio')
datas += copy_metadata('python-socketio')
datas += copy_metadata('python-engineio')
# Read-only assets this app bundles alongside the code - see server.py's
# BUNDLED_DIR/_bundled_dir() (resolves to sys._MEIPASS at runtime, root of
# this datas list). network_inject.js is loaded via load_js() and injected
# into the Playwright-driven page; prompt_config.json is the DEFAULT
# framework config, copied to a writable location next to the executable
# on first run (server.py seeds PROMPT_CONFIG_FILE from this if missing) -
# after that, the user's own edited copy (via the UI's config editor) is
# what's actually read, never this bundled one.
datas += [('network_inject.js', '.'), ('ui_recorder.js', '.'), ('prompt_config.json', '.')]

a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    # engineio picks its async driver by STRING (async_mode="threading" in
    # server.py) - PyInstaller's static import analysis can't see that
    # dynamic import, so it has to be listed explicitly or the frozen build
    # fails at Socket.IO connection time, not at build time.
    # Libraries that "Run script" steps (DB / API scripts) use. The packaged app runs scripts on its own built-in Python, so they must be in
    # the build: nothing is imported statically from server.py, hence listed here. dns = dnspython, needed for mongodb+srv:// addresses.
    hiddenimports=['flask_cors', 'engineio.async_drivers.threading', 'pymongo', 'bson', 'gridfs', 'requests', 'certifi', 'dns.resolver',
                   *collect_submodules('pymongo'), *collect_submodules('dns')],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ApiTestGenBridge',
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
