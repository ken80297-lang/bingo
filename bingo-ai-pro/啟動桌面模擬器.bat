@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv-desktop\Scripts\python.exe" (
  echo 找不到 .venv-desktop，請先執行「安裝桌面模擬器.bat」。
  pause
  exit /b 1
)
".venv-desktop\Scripts\python.exe" desktop\run_simulator.py
if errorlevel 1 (
  echo 桌面模擬器啟動失敗，請查看 desktop\logs\desktop_simulator.log
  pause
)
