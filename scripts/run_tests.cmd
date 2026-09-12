@echo off
rem 一键：静态自检 + 全部自动化测试（零成本，不调模型）
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

python -X utf8 main.py check
if errorlevel 1 (
  echo.
  echo [FAIL] 静态自检没通过，先修上面标 FAIL 的项。
  pause
  exit /b 1
)

python -X utf8 main.py test
if errorlevel 1 (
  echo.
  echo [FAIL] 有测试没通过，见上面的 FAIL/ERROR 行。
  pause
  exit /b 1
)

echo.
echo [OK] 静态自检与全部测试都通过了。
endlocal
