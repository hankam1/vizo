# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for vi.log
# Build: pyinstaller vilog.spec

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Playwright needs its driver bundled
playwright_datas = collect_data_files("playwright")
playwright_hidden = collect_submodules("playwright")

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('ui', 'ui'),
        ('prompts', 'prompts'),
    ] + playwright_datas,
    hiddenimports=[
        'webview.platforms.edgechromium',
        'webview.platforms.mshtml',
        'aiohttp',
        'pyperclip',
    ] + playwright_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Never bundle user-specific / generated dirs
    excludes=[
        'chrome_profile',
        'user_settings.json',
        'output',
        'fetch_docs',
        'fetch_openapi',
        'parse_openapi',
        'parse_openapi2',
    ],
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
    name='vi.log',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='logo.ico' if __import__('os').path.exists('logo.ico') else None,
)
