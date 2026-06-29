@echo off
REM ============================================================
REM  Publish a new vizo release (Windows .exe) to GitHub.
REM  Builds the exe, then creates a GitHub Release with it attached,
REM  so users can download it AND the in-app "Check updates" button works.
REM
REM  Requirements (one-time):
REM    - gh CLI installed + logged in:  gh auth login
REM    - Build deps installed:          pip install -r requirements.txt
REM
REM  Steps to ship an update:
REM    1) Bump VERSION in config.py  (e.g. "1.1.0" -> "1.2.0")
REM    2) Run this script:           release.bat
REM ============================================================

for /f "delims=" %%v in ('py -c "import config; print(config.VERSION)"') do set APPVER=%%v
if "%APPVER%"=="" (
  echo ERROR: could not read VERSION from config.py
  pause & exit /b 1
)

echo ============================================================
echo  Releasing vizo v%APPVER%
echo ============================================================
echo  This builds the exe (10-20 min) and publishes release v%APPVER%.
echo  Make sure you bumped VERSION in config.py FIRST.
echo.
set /p CONFIRM="Continue? (y/n): "
if /i not "%CONFIRM%"=="y" ( echo Cancelled. & exit /b 0 )

REM --- Build (skip build.bat's trailing pause) ---
set VIZO_NOPAUSE=1
call build.bat
if errorlevel 1 ( echo. & echo BUILD FAILED & pause & exit /b 1 )

if not exist build-nuitka\vizo.exe (
  echo ERROR: build-nuitka\vizo.exe not found after build
  pause & exit /b 1
)

REM --- Publish the GitHub release ---
echo.
echo Publishing GitHub release v%APPVER% ...
gh release create v%APPVER% build-nuitka\vizo.exe ^
  --title "vizo v%APPVER%" ^
  --notes "Windows: скачайте vizo.exe ниже.^

Обновление также доступно прямо в приложении: Настройки -> Проверить обновления."
if errorlevel 1 (
  echo.
  echo RELEASE FAILED. Возможно тег v%APPVER% уже существует — подними VERSION в config.py.
  pause & exit /b 1
)

echo.
echo ============================================================
echo  Done! Release v%APPVER% опубликован.
echo  Пользователи на старых версиях увидят обновление по кнопке.
echo ============================================================
pause
