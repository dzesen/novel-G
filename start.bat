@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist ".venv\Scripts\pythonw.exe" (
    echo [错误] 尚未安装本地运行环境，请先双击 setup.bat。
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -c "import customtkinter" >nul 2>nul
if errorlevel 1 (
    echo [错误] 启动器依赖不完整，请重新双击 setup.bat 修复。
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" launcher.py
exit
