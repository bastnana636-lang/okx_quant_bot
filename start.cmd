@echo off
chcp 65001 >nul
title OKX Quant Trader
cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel% equ 0 (
  py -3 native\launcher.py start
) else (
  python native\launcher.py start
)

set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo 启动失败，请查看上方错误信息。
  pause
)
exit /b %EXIT_CODE%
