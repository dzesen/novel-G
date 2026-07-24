@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo [错误] 尚未安装本地运行环境，请先双击 setup.bat。
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 13) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [错误] 当前 .venv 不是受支持的 Python 3.11/3.12，请重新运行 setup.bat。
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
if errorlevel 1 (
    echo [错误] 无法启动桌面启动器，请在终端运行 .venv\Scripts\python.exe launcher.py 查看错误。
    pause
    exit /b 1
)
exit /b 0
