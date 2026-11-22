@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ============================================================
echo   vmagent installer - VideoMatrix Agent Bridge
echo ============================================================
echo.

REM ---- 找 Python ----
set PY=
where py >nul 2>&1 && set PY=py -3
if not defined PY (
  where python >nul 2>&1 && set PY=python
)
if not defined PY (
  echo [X] Python not found.
  echo     Install Python 3.8+ from https://www.python.org/downloads/
  echo     Remember to check "Add Python to PATH" during install.
  echo.
  pause
  exit /b 2
)

echo [1] Python found:
%PY% -c "import sys;print('    '+sys.version.split()[0]+'  '+sys.executable)"
echo.

echo [2] Checking environment...
%PY% -m vmagent.install check
if errorlevel 1 (
  echo.
  echo [X] Environment check failed. Fix the issue above first.
  pause
  exit /b 2
)
echo.

echo [3] Registering to Agent clients...
%PY% -m vmagent.install
echo.

echo ============================================================
echo   Done. Next steps:
echo     1. Open VideoMatrix desktop app
echo     2. Restart your Agent client (Claude Desktop / Cursor / Codex)
echo     3. Ask it to call vm_discover
echo ============================================================
echo.
echo Run this anytime to check status:
echo     %PY% -m vmagent.install doctor
echo.
pause