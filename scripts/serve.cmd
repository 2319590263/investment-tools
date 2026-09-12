@echo off
rem 启动 Web 控制台（可指定端口）：scripts\serve.cmd [端口]
rem 等价于：python main.py webui --port 8765
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"

python -X utf8 main.py webui --port %PORT%
if errorlevel 1 (
  echo.
  echo [FAIL] 启动失败（常见原因：端口 %PORT% 被占用）。
  pause
)
endlocal
