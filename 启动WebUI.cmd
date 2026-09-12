@echo off
rem ============================================================
rem  aiplan Web 控制台 —— Windows 一键启动
rem  双击本文件即可；也可以在终端里执行  启动WebUI.cmd
rem  默认 http://127.0.0.1:8765 ，只监听本机，按 Ctrl+C 停止。
rem  换端口：scripts\serve.cmd 9000
rem ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PY_EXE="
where python >nul 2>nul
if not errorlevel 1 set "PY_EXE=python"
if not defined PY_EXE (
  where py >nul 2>nul
  if not errorlevel 1 set "PY_EXE=py"
)
if not defined PY_EXE (
  echo [FAIL] 没有找到 python。请先安装 Python 3.9+，
  echo        安装时务必勾选 "Add Python to PATH"，然后重新双击本文件。
  echo        下载地址：https://www.python.org/downloads/
  pause
  exit /b 2
)

echo [..] 正在启动 aiplan Web 控制台（会自动打开浏览器）...
echo      默认地址 http://127.0.0.1:8765/    按 Ctrl+C 停止
echo.
"%PY_EXE%" -X utf8 "%~dp0main.py" webui --port 8765

if errorlevel 1 (
  echo.
  echo [FAIL] 启动异常退出。常见原因：端口 8765 被占用或上一个实例还没关。
  echo        换个端口重试：python main.py webui --port 8766
  pause
)

endlocal
