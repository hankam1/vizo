@echo off
REM Build vi.log with Nuitka (compiled to native code)
REM Requires: pip install nuitka ordered-set

REM Version single-sourced from config.py — the updater compares config.VERSION
REM against the GitHub release tag, so the exe metadata must match it.
for /f "delims=" %%v in ('py -c "import config; print(config.VERSION)"') do set APPVER=%%v
if "%APPVER%"=="" (
  echo ERROR: could not read VERSION from config.py
  pause
  exit /b 1
)

REM Icon: generate if missing; build without icon as a last resort.
if not exist logo.ico (
  if exist logo_256.png py make_icon.py
)
set ICON_FLAG=--windows-icon-from-ico=logo.ico
if not exist logo.ico (
  echo WARNING: logo.ico not found - building without icon
  set ICON_FLAG=
)

echo Building vizo %APPVER% with Nuitka...
echo This will take 10-20 minutes (first build is slow; subsequent builds are faster).
echo.

py -m nuitka ^
  --standalone ^
  --onefile ^
  --windows-console-mode=disable ^
  --disable-plugin=pywebview ^
  --include-package=webview ^
  --include-package=clr_loader ^
  --include-package=pythonnet ^
  --include-package=winotify ^
  --include-package-data=webview ^
  --no-deployment-flag=excluded-module-usage ^
  --include-data-dir=ui=ui ^
  --include-data-dir=prompts=prompts ^
  %ICON_FLAG% ^
  --output-filename=vizo.exe ^
  --output-dir=build-nuitka ^
  --assume-yes-for-downloads ^
  --company-name="hankam1" ^
  --product-name="vizo" ^
  --file-description="vizo - YouTube automation" ^
  --product-version=%APPVER% ^
  --file-version=%APPVER% ^
  app.py

if errorlevel 1 (
  echo.
  echo BUILD FAILED
  pause
  exit /b 1
)

echo.
echo Build done. Output: build-nuitka\vizo.exe
if not "%VIZO_NOPAUSE%"=="1" pause
