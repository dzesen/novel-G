@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Novel-G 启动器
set "NO_PAUSE="
for %%A in (%*) do if /i "%%~A"=="--no-pause" set "NO_PAUSE=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not defined TCL_LIBRARY if exist ".venv\tcl-runtime\tcl8.6\init.tcl" set "TCL_LIBRARY=%CD%\.venv\tcl-runtime\tcl8.6"
if not defined TK_LIBRARY if exist ".venv\tcl-runtime\tk8.6\tk.tcl" set "TK_LIBRARY=%CD%\.venv\tcl-runtime\tk8.6"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [错误] 尚未安装本地运行环境，请先双击 setup.bat。
    call :pause_if_needed
    exit /b 1
)
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 13) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [错误] 当前 .venv 不是受支持的 Python 3.11/3.12，请重新运行 setup.bat。
    call :pause_if_needed
    exit /b 1
)
".venv\Scripts\python.exe" -c "import customtkinter" >nul 2>nul
if errorlevel 1 (
    echo [错误] 启动器依赖不完整，请重新双击 setup.bat 修复。
    call :pause_if_needed
    exit /b 1
)

if not exist "logs" mkdir "logs"
if not defined NOVEL_G_LAUNCHER_STARTUP_LOG set "NOVEL_G_LAUNCHER_STARTUP_LOG=%CD%\logs\launcher-startup.log"
".venv\Scripts\python.exe" launcher.py --check-startup >"%NOVEL_G_LAUNCHER_STARTUP_LOG%" 2>&1
if errorlevel 1 (
    echo [错误] 桌面启动器初始化失败，已阻止静默闪退。
    echo [日志] %NOVEL_G_LAUNCHER_STARTUP_LOG%
    echo.
    type "%NOVEL_G_LAUNCHER_STARTUP_LOG%"
    call :pause_if_needed
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" launcher.py %*
if errorlevel 1 (
    echo [错误] 无法创建桌面启动器进程。
    echo [日志] %NOVEL_G_LAUNCHER_STARTUP_LOG%
    call :pause_if_needed
    exit /b 1
)
exit /b 0

:pause_if_needed
if not defined NO_PAUSE pause
exit /b 0
