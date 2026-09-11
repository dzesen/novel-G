@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Novel-G 环境安装
set "NO_PAUSE="
for %%A in (%*) do if /i "%%~A"=="--no-pause" set "NO_PAUSE=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "scripts\install_local.py" (
    echo [错误] 缺少安装脚本，请重新解压完整的源码发布包。
    call :pause_if_needed
    exit /b 1
)

set "PYTHON_CMD="
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys,struct; raise SystemExit(0 if (3,11)<=sys.version_info<(3,13) and struct.calcsize('P')==8 else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=.venv\Scripts\python.exe"
)
if not defined PYTHON_CMD (
    py -3.12 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3.12"
)
if not defined PYTHON_CMD (
    py -3.11 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3.11"
)
if not defined PYTHON_CMD (
    python -c "import sys,struct; raise SystemExit(0 if (3,11)<=sys.version_info<(3,13) and struct.calcsize('P')==8 else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
    echo [错误] 未找到受支持的 Python。请安装 64 位 Python 3.11 或 3.12，包含 Tcl/Tk 并勾选 Add Python to PATH。
    echo [说明] docs\source-release.zh-CN.md
    call :pause_if_needed
    exit /b 1
)

%PYTHON_CMD% scripts\install_local.py %*
set "INSTALL_EXIT=%ERRORLEVEL%"
call :pause_if_needed
exit /b %INSTALL_EXIT%

:pause_if_needed
if not defined NO_PAUSE pause
exit /b 0
