@echo off
chcp 65001 >nul
set PY=C:\Python311\python.exe
set SRC=%~dp0lan_copy.py
echo [1] ensure deps...
%PY% -m pip install -q pyinstaller pywin32 pystray Pillow windnd
echo [2] build onedir...
cd /d %~dp0
%PY% -m PyInstaller -D -w -n 局域网复制 --clean --noconfirm --hidden-import=win32timezone --collect-all=pystray "%SRC%"
if errorlevel 1 (
  echo build failed
  exit /b 1
)
echo done: dist\局域网复制\
dir "dist\局域网复制"
