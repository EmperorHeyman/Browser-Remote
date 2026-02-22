# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Seamless browser remote.
Build with:  pyinstaller projector_remote.spec
"""

import os
import sys

block_cipher = None
base_dir = os.path.dirname(os.path.abspath(SPEC))

# Include SSL certs if they exist at build time
_extra_datas = []
for _cert in ('key.pem', 'cert.pem'):
    _cert_path = os.path.join(base_dir, _cert)
    if os.path.isfile(_cert_path):
        _extra_datas.append((_cert, '.'))

a = Analysis(
    ['launcher.py'],
    pathex=[base_dir],
    binaries=[],
    datas=[
        ('server.py', '.'),
        ('config.py', '.'),
        ('static', 'static'),
        ('about.md', '.'),
    ] + _extra_datas,
    hiddenimports=[
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'fastapi',
        'fastapi.applications',
        'fastapi.routing',
        'fastapi.responses',
        'fastapi.staticfiles',
        'fastapi.websockets',
        'starlette',
        'starlette.routing',
        'starlette.responses',
        'starlette.staticfiles',
        'starlette.websockets',
        'pydantic',
        'playwright',
        'playwright.async_api',
        'anyio',
        'anyio._backends',
        'anyio._backends._asyncio',
        'httptools',
        'websockets',
        'PyQt6',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'qrcode',
        'qrcode.main',
        'mss',
        'mss.tools',
        'PIL',
        'PIL.Image',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
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
    [],
    exclude_binaries=True,
    name='ProjectorRemote',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # No console window — PyQt6 GUI is the interface
    icon=None,      # Set to 'icon.ico' if you add an icon file
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ProjectorRemote',
)
