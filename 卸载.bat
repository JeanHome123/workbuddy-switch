@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "WBROOT=%USERPROFILE%\.workbuddy"
set "PYVER="
if exist "%WBROOT%\binaries\python\versions\current" (
  set /p PYVER=<"%WBROOT%\binaries\python\versions\current"
)
if defined PYVER set "PY=%WBROOT%\binaries\python\versions\%PYVER%\python.exe"
if not defined PY set "PY=python"

if not exist "%PY%" (
  echo [错误] 找不到 WorkBuddy 自带的 Python：
  echo   %PY%
  pause
  exit /b 1
)

"%PY%" "%~dp0wbs.py" uninstall
echo.
echo 已移除后台自启和环境变量。切号器后台进程也会被结束。
pause
