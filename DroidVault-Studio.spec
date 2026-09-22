# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all

datas = [('app_icon.ico', '.'), ('app_icon.png', '.')]
platform_name = 'windows' if os.name == 'nt' else 'linux'
platform_tools = os.path.join('tools', platform_name)
if os.path.isdir(platform_tools):
    datas.append((platform_tools, os.path.join('tools', platform_name)))
elif platform_name == 'windows' and os.path.isdir('scrcpy-win64-v4.1'):
    datas.append(('scrcpy-win64-v4.1', os.path.join('tools', 'windows')))
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
    upx=True,
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
