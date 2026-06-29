@echo off
REM Debug build - with console window to see errors
py -m nuitka ^
  --standalone ^
  --onefile ^
  --disable-plugin=pywebview ^
  --include-package=webview ^
  --include-package=clr_loader ^
  --include-package=pythonnet ^
  --include-package-data=webview ^
  --no-deployment-flag=excluded-module-usage ^
  --include-data-dir=ui=ui ^
  --include-data-dir=prompts=prompts ^
  --output-filename=vi.log-debug.exe ^
  --output-dir=build-nuitka ^
  --assume-yes-for-downloads ^
  app.py
pause
