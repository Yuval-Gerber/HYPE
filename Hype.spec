# PyInstaller spec — builds Hype.app (windowed macOS bundle, custom icon).
# Build:  pyinstaller --noconfirm Hype.spec
# Output: dist/Hype.app  (double-click to launch; no terminal)

block_cipher = None

a = Analysis(
    ['hype_app.py'],
    pathex=[],
    binaries=[],
    datas=[('hype/ui/assets', 'assets')],   # -> bundled as MEIPASS/assets/*
    hiddenimports=['keyring.backends.macOS', 'pyqtgraph'],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='Hype',
    debug=False,
    strip=False,
    upx=False,
    console=False,          # windowed: no terminal
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Hype')

app = BUNDLE(
    coll,
    name='Hype.app',
    icon='hype/ui/assets/Hype.icns',
    bundle_identifier='com.hype.trading',
    info_plist={
        'CFBundleName': 'Hype',
        'CFBundleDisplayName': 'Hype',
        'CFBundleShortVersionString': '0.5.0',
        'NSHighResolutionCapable': True,
        # Keep the app in light appearance even when macOS is in dark mode.
        'NSRequiresAquaSystemAppearance': True,
        # Touch ID prompt strings (LocalAuthentication).
        'NSFaceIDUsageDescription': 'Hype uses Touch ID to unlock and authorize actions.',
    },
)
