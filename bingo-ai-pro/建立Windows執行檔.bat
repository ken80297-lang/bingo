@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv-desktop\Scripts\python.exe" (
  echo 找不到 .venv-desktop，請先執行「安裝桌面模擬器.bat」。
  pause
  exit /b 1
)
".venv-desktop\Scripts\python.exe" -m pip install pyinstaller
".venv-desktop\Scripts\python.exe" desktop\build_windows_exe.py
if errorlevel 1 (
  echo 建立 EXE 失敗，請查看輸出訊息。
  pause
  exit /b 1
)
echo Windows 執行檔已輸出至 desktop\dist
pause
