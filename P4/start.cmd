@echo off
cd /d "%~dp0.."
for %%P in ("P4\.venv\Scripts\python.exe" "P1\.venv\Scripts\python.exe" "P4\var\python39\python.exe") do (
  if exist %%P (
    %%P -c "import sys; assert sys.version_info >= (3,9)" >nul 2>&1
    if not errorlevel 1 (
      %%P P4\scripts\control.py
      goto end
    )
  )
)
py -3 -c "import sys; assert sys.version_info >= (3,9)" >nul 2>&1
if not errorlevel 1 (
  py -3 P4\scripts\control.py
  goto end
)
python -c "import sys; assert sys.version_info >= (3,9)" >nul 2>&1
if not errorlevel 1 (
  python P4\scripts\control.py
  goto end
)
echo Python 3.9 or newer is required to open this panel.
:end
if errorlevel 1 pause
