@echo off
rem ================================================================
rem  EyeGuard build script: install deps and build single-file exe
rem  Output: dist\EyeGuard.exe  (no console window)
rem  Tip: if you want a custom exe icon, put eye.ico beside this file.
rem ================================================================
cd /d "%~dp0"

rem Use the project venv when present, otherwise the system python
set "PY=python"
if exist .venv\Scripts\python.exe set "PY=.venv\Scripts\python.exe"

echo [1/3] Installing dependencies (via %PY%)...
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Build FAILED at dependency install.
    pause
    exit /b 1
)

echo [2/3] Cleaning previous build output...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build

echo [3/3] Building with PyInstaller...
set "ICON="
if exist eye.ico set "ICON=--icon=eye.ico"
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name EyeGuard --add-data "characters;characters" %ICON% main.py
if errorlevel 1 (
    echo.
    echo Build FAILED. See messages above.
    pause
    exit /b 1
)

rem 角色包随 exe 一起分发（程序从 exe 旁的 characters\ 目录加载）
if exist characters xcopy /e /i /y characters dist\characters\ >nul

echo.
echo Build OK: %~dp0dist\EyeGuard.exe  (+ characters\)
pause
exit /b 0
