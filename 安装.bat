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
  echo 请先打开一次 WorkBuddy，再运行本文件。
  pause
  exit /b 1
)

echo ============================================
echo   WorkBuddy 切号器 - 安装并启动后台
echo ============================================
echo.
"%PY%" "%~dp0wbs.py" install
set "RC=%ERRORLEVEL%"
echo.
"%PY%" "%~dp0wbs.py" status
echo.
if not "%RC%"=="0" (
  echo [失败] 后台没有启动，请看上面的提示和 logs\switcher.log。
  pause
  exit /b %RC%
)
echo 已就绪：点头像，底部就是「切换账号」。
echo 窗口里显示「控制端 127.0.0.1:9340」即表示后台正在运行。
echo 本次登录只需运行一次；以后登录 Windows 会自动启动。
echo.
pause
