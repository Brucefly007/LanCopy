@echo off
chcp 65001 >nul
set PY=C:\Python311\python.exe
set SRC=%~dp0lan_copy.py
set OUT=%USERPROFILE%\Desktop
echo [1] ensure pyinstaller...
%PY% -m pip install -q pyinstaller pywin32 windnd
echo [2] build...
cd /d %~dp0
%PY% -m PyInstaller -F -w -n LanCopy --clean --noconfirm "%SRC%"
if errorlevel 1 (
  echo build failed
  exit /b 1
)
copy /Y "dist\LanCopy.exe" "%OUT%\LanCopy.exe"
copy /Y "使用说明.txt" "%OUT%\局域网复制_使用说明.txt"
REM try Chinese name copy
copy /Y "dist\LanCopy.exe" "%OUT%\局域网复制_新版.exe"
echo done
dir "%OUT%\LanCopy.exe"
