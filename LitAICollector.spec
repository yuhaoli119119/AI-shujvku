# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT
import os

block_cipher = None
BASE_DIR = os.path.abspath('.')

a = Analysis(
    ['launch.py'],
    pathex=[BASE_DIR],
    binaries=[],
    datas=[
        ('app', 'app'),
        ('findpapers', 'findpapers'),
        ('src', 'src'),
        ('big_big_wolf.png', '.'),
    ],
    hiddenimports=[
        'paper_finder',
        'yaml',
        'app.core.models',
        'app.core.project_manager',
        'app.core.config',
        'app.services.search_services',
        'app.services.xmol_service',
        'app.services.download_service',
        'app.services.ai_extractor',
        'app.services.pdf_parser',
        'app.ui.main_window',
        'app.ui.search_page',
        'app.ui.library_page',
        'app.ui.extraction_page',
        'app.ui.theme_manager',
        'app.ui.project_dialog',
        'sqlmodel',
        'pydantic',
        'httpx',
        'loguru',
        'feedparser',
        'requests',
        'bs4',
        'lxml',
        'fitz',
        'openai',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'fastapi',
        'uvicorn',
        'celery',
        'redis',
        'psycopg',
        'psycopg2',
        'streamlit',
        'django',
        'flask',
        'matplotlib',
        'numpy',
        'scipy',
        'tkinter',
        'unittest',
        'pydoc',
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
    name='LitAICollector',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='big_big_wolf.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='LitAICollector',
)
