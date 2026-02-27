# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for ChatHub

Build command:
    pyinstaller chathub.spec

This creates a standalone Windows executable that includes:
- Python interpreter
- All dependencies
- Playwright Chromium browser
- Static files and templates
"""

import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Get the project directory
PROJECT_DIR = Path(SPECPATH)

# Collect all submodules for key packages
hiddenimports = [
    'uvicorn.logging',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'uvicorn.lifespan.off',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.main',
    'email.mime.multipart',
    'email.mime.text',
    'email.mime.base',
    'passlib.handlers.bcrypt',
    'passlib.handlers.sha2_crypt',
    'pystray._win32',
    'PIL._tkinter_finder',
    'httpx',
    'httpcore',
    'anyio',
    'sniffio',
    'h11',
    'h2',
    'certifi',
    'charset_normalizer',
    'multipart',
    'python_multipart',
    'jose',
    'jose.jwt',
    'jose.jws',
    'jose.exceptions',
    'cryptography',
    'slowapi',
    'sqlalchemy',
    'sqlalchemy.dialects.sqlite',
    'jinja2',
    'starlette',
    'starlette.applications',
    'starlette.routing',
    'starlette.middleware',
    'starlette.requests',
    'starlette.responses',
    'starlette.staticfiles',
    'starlette.templating',
    'starlette.websockets',
    'fastapi',
    'pydantic',
    'pydantic_core',
    'openai',
    'anthropic',
    'playwright',
    'playwright.sync_api',
    'playwright.async_api',
    'greenlet',
    'pdfplumber',
    'PIL',
    'pystray',
]

# Collect data files
datas = [
    # Application templates and static files
    (str(PROJECT_DIR / 'app' / 'templates'), 'app/templates'),
    (str(PROJECT_DIR / 'static'), 'static'),
    # .env.example for reference
    (str(PROJECT_DIR / '.env.example'), '.'),
]

# Add Playwright browsers if they exist
playwright_browsers = Path.home() / '.cache' / 'ms-playwright'
if sys.platform == 'win32':
    playwright_browsers = Path(os.environ.get('LOCALAPPDATA', '')) / 'ms-playwright'

if playwright_browsers.exists():
    # Include Chromium browser
    for browser_dir in playwright_browsers.glob('chromium-*'):
        datas.append((str(browser_dir), f'playwright/driver/package/.local-browsers/{browser_dir.name}'))

# Analysis
a = Analysis(
    ['tray_app.py'],
    pathex=[str(PROJECT_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'pytest',
        'IPython',
        'notebook',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

# Create PYZ archive
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# Create executable
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ChatHub',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # No console window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_DIR / 'static' / 'favicon.ico') if (PROJECT_DIR / 'static' / 'favicon.ico').exists() else None,
)

# Collect all files
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ChatHub',
)
