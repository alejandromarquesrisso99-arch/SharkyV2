# -*- mode: python ; coding: utf-8 -*-
"""Empaquetado de Sharky: modo carpeta, sin consola y sin UPX (GUIA §3).

Lo que aquí parece de más está por las trampas conocidas de §3: keyring no descubre su
backend dentro del exe, los metadatos hacen falta para leer la versión y yfinance habla por
curl_cffi, que lleva sus propias bibliotecas y certificados.
"""

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

RAIZ = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH lo inyecta PyInstaller
ORIGEN = RAIZ / "src"
RECURSOS = ORIGEN / "sharky" / "resources"
ICONO = RECURSOS / "sharky.ico"

datas = [(str(RECURSOS), "sharky/resources")]
datas += copy_metadata("sharky")  # __version__ se lee de los metadatos del paquete
datas += collect_data_files("certifi")  # certificados de TLS
datas += collect_data_files("curl_cffi")
datas += collect_data_files("yfinance")

binaries = collect_dynamic_libs("curl_cffi")

hiddenimports = collect_submodules("keyring.backends") + collect_submodules("win32ctypes")

a = Analysis(  # noqa: F821
    [str(RAIZ / "packaging" / "launcher.py")],
    pathex=[str(ORIGEN)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Sharky",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICONO),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Sharky",
)
