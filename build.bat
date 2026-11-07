@echo off
REM Builds the Windows installer:  installer\BitTorrentClient-Setup-<version>.exe
REM Also leaves dist\BitTorrentClient\BitTorrentClient.exe (the program folder the installer packages).
REM Needs Python 3.10+ from python.org (tick "Add python.exe to PATH" and keep the tcl/tk option ticked).
REM Inno Setup (7 or 6) builds the installer; this script installs it for you with winget if it is missing.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
for /f %%v in ('%PY% -c "from version import VERSION; print(VERSION)"') do set VER=%%v
%PY% -m pip install --upgrade pyinstaller || goto :fail
echo Running self-test (Windows Firewall may ask about python.exe - Allow, it only uses loopback)...
%PY% test_local.py || echo WARNING: self-test failed - building anyway
%PY% -m PyInstaller --noconfirm --clean --onedir --windowed --icon BitTorrentClient.ico --name BitTorrentClient bittorrentclient.py || goto :fail

REM Built-in player: if libmpv-2.dll (or mpv-2.dll) sits next to this script it is bundled into the program folder.
for %%d in (libmpv-2.dll mpv-2.dll) do if exist "%~dp0%%d" copy /y "%~dp0%%d" dist\BitTorrentClient\ >nul
if not exist dist\BitTorrentClient\libmpv-2.dll if not exist dist\BitTorrentClient\mpv-2.dll (
  echo Downloading libmpv to build the player into the app...
  %PY% libmpv_get.py dist\BitTorrentClient || echo WARNING: could not download libmpv - the app will offer to download it on first use
)

call :findiscc
if not defined ISCC (
  echo Inno Setup not found - installing it with winget...
  winget install -e --id JRSoftware.InnoSetup --accept-source-agreements --accept-package-agreements
  call :findiscc
)
if not defined ISCC (
  echo.
  echo The program was built, but the installer needs Inno Setup 6 or 7.
  echo Install it from https://jrsoftware.org/isdl.php then run build.bat again.
  echo The program itself is in dist\BitTorrentClient\BitTorrentClient.exe
  pause
  exit /b 1
)
"%ISCC%" installer.iss /DMyAppVersion=%VER% || goto :fail
echo.
echo SUCCESS: installer\BitTorrentClient-Setup-%VER%.exe
pause
exit /b 0

:findiscc
set ISCC=
REM the last match wins, so 7 is preferred to 6 (7 installs to Program Files, 6 to Program Files (x86), per-user to LOCALAPPDATA)
for %%v in (6 7) do for %%p in ("%ProgramFiles(x86)%\Inno Setup %%v\ISCC.exe" "%ProgramFiles%\Inno Setup %%v\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup %%v\ISCC.exe") do if exist %%p set ISCC=%%~p
if not defined ISCC for /f "delims=" %%i in ('where ISCC 2^>nul') do set ISCC=%%i
exit /b 0

:fail
echo.
echo BUILD FAILED
pause
exit /b 1
