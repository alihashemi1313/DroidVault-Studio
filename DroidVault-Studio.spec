# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all

datas = [('app_icon.ico', '.'), ('app_icon.png', '.')]
platform_name = 'windows' if os.name == 'nt' else 'linux'
platform_tools = os.path.join('tools', platform_name)
if os.path.isdir(platform_tools):
    # Add files individually so PyInstaller preserves the intended layout and
    # never creates tools/<platform>/tools/<platform> when given a directory.
    for root, _, files in os.walk(platform_tools):
        for filename in files:
            source = os.path.join(root, filename)
            relative = os.path.relpath(source, platform_tools)
            destination = os.path.join('tools', platform_name, os.path.dirname(relative))
            datas.append((source, destination))
binaries = []
hiddenimports = []
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='DroidVault-Studio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app_icon.ico'],
)
