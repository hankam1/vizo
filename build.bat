@echo off
REM Build vi.log with Nuitka (compiled to native code)
REM Requires: py -3.12 -m pip install -r requirements.txt nuitka ordered-set
REM
REM Python 3.12 explicitly: the system `py` resolves to 3.14, which Nuitka
REM cannot compile against (C errors in the GC headers — _gc_runtime_state has
REM no `young` field). MinGW64 is used because there is no MSVC on the machine;
REM Nuitka downloads it itself thanks to --assume-yes-for-downloads.
set PY=py -3.12

REM Version single-sourced from config.py — the updater compares config.VERSION
REM against the GitHub release tag, so the exe metadata must match it.
for /f "delims=" %%v in ('%PY% -c "import config; print(config.VERSION)"') do set APPVER=%%v
if "%APPVER%"=="" (
  echo ERROR: could not read VERSION from config.py
  pause
  exit /b 1
)

REM Icon: generate if missing; build without icon as a last resort.
if not exist logo.ico (
  if exist logo_256.png %PY% make_icon.py
)
set ICON_FLAG=--windows-icon-from-ico=logo.ico
if not exist logo.ico (
  echo WARNING: logo.ico not found - building without icon
  set ICON_FLAG=
)

echo Building vizo %APPVER% with Nuitka...
echo This will take 10-20 minutes (first build is slow; subsequent builds are faster).
echo.

%PY% -m nuitka ^
  --standalone ^
  --onefile ^
  --mingw64 ^
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
