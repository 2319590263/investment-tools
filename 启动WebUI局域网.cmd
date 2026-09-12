@echo off
rem ============================================================
rem  aiplan Web 控制台 —— 局域网安全启动
rem  首次运行会请求一次管理员权限，仅为放行 8765 端口；
rem  防火墙规则只允许“本地子网”访问。服务本身仍以普通权限运行。
rem ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "RULE_NAME=aiplan WebUI 8765"

if /i "%~1"=="--install-firewall" goto install_firewall

netsh advfirewall firewall show rule name="%RULE_NAME%" >nul 2>&1
if errorlevel 1 (
  echo [..] 首次运行需要添加 Windows 防火墙规则，请在 UAC 窗口选择“是”...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '--install-firewall' -Verb RunAs -Wait"
  if errorlevel 1 (
    echo [FAIL] 未获得管理员权限，无法放行局域网端口。
    pause
    exit /b 5
  )
)

set "PY_EXE="
where python >nul 2>nul
if not errorlevel 1 set "PY_EXE=python"
if not defined PY_EXE (
  where py >nul 2>nul
  if not errorlevel 1 set "PY_EXE=py"
)
if not defined PY_EXE (
  echo [FAIL] 没有找到 Python。请先安装 Python 3.9+，安装时勾选 "Add Python to PATH"。
  pause
  exit /b 2
)

echo [..] 正在启动 aiplan Web 控制台（局域网安全模式）...
echo      本机地址与手机/其它电脑访问地址会打印在下方。
echo      手机建议打开 [LAN] 手机专用页面 对应的 /m 地址，密码见下方 [安全] 行。
echo      想固定密码：在 config\webui配置.json 写入 {"密码":"你的口令"}（该文件不入库）。
echo.
"%PY_EXE%" -X utf8 "%~dp0main.py" webui --host 0.0.0.0 --port 8765

if errorlevel 1 (
  echo.
  echo [FAIL] 启动异常退出。常见原因：端口 8765 被占用或上一个实例还没关。
  pause
)

endlocal
exit /b 0

:install_firewall
netsh advfirewall firewall delete rule name="%RULE_NAME%" >nul 2>&1
netsh advfirewall firewall add rule name="%RULE_NAME%" dir=in action=allow protocol=TCP localport=8765 remoteip=LocalSubnet profile=any
if errorlevel 1 (
  echo [FAIL] 防火墙规则添加失败。
  pause
  exit /b 5
)
echo [OK] 防火墙已放行 TCP 8765（仅本地子网）。
exit /b 0
