@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Novel-G 环境安装
set "NO_PAUSE="
if /i "%~1"=="--no-pause" set "NO_PAUSE=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "npm_config_cache=%CD%\.npm-cache"
set "npm_config_update_notifier=false"

if not exist "requirements.txt" (
    echo [错误] 当前目录缺少 requirements.txt，请确认项目文件完整。
    call :pause_if_needed
    exit /b 1
)
if not exist "frontend\package.json" (
    echo [错误] 当前目录缺少 frontend\package.json，请确认项目文件完整。
    call :pause_if_needed
    exit /b 1
)

echo [1/5] 检查 Python...
set "PYTHON_CMD="
set "USE_EXISTING_VENV="

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 13) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo [错误] 现有 .venv 不是 Python 3.11/3.12。请先关闭程序，将 .venv 重命名或删除后重新运行 setup.bat。
        call :pause_if_needed
        exit /b 1
    )
    set "USE_EXISTING_VENV=1"
)

if not defined USE_EXISTING_VENV (
    py -3.12 -c "import sys" >nul 2>nul && set "PYTHON_CMD=py -3.12"
    if not defined PYTHON_CMD py -3.11 -c "import sys" >nul 2>nul && set "PYTHON_CMD=py -3.11"
    if not defined PYTHON_CMD where python >nul 2>nul && python -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 13) else 1)" >nul 2>nul && set "PYTHON_CMD=python"
    if not defined PYTHON_CMD (
        echo [错误] 未找到受支持的 Python。请安装 64 位 Python 3.11 或 3.12，并勾选 Add Python to PATH。
        call :pause_if_needed
        exit /b 1
    )

    echo [2/5] 创建本地 Python 环境...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 goto :failed
) else (
    echo [2/5] 本地 Python 3.11/3.12 环境已存在。
)

call :ensure_tk_runtime
if errorlevel 1 goto :failed

echo [3/5] 安装后端依赖...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo [4/5] 安装前端依赖...
where npm.cmd >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 npm，请安装 Node.js 20.9 或更高版本。
    call :pause_if_needed
    exit /b 1
)
node -e "const [a,b]=process.versions.node.split('.').map(Number); process.exit(a>20 || (a===20 && b>=9) ? 0 : 1)"
if errorlevel 1 (
    echo [错误] Node.js 版本过低，请安装 Node.js 20.9 或更高版本。
    call :pause_if_needed
    exit /b 1
)

pushd frontend
if errorlevel 1 goto :failed
if exist package-lock.json (
    call npm.cmd ci
) else (
    call npm.cmd install
)
set "NPM_INSTALL_EXIT=%ERRORLEVEL%"
if not "%NPM_INSTALL_EXIT%"=="0" (
    popd
    goto :failed
)
echo [5/5] 生成可复用的前端生产构建...
call npm.cmd run build
set "FRONTEND_BUILD_EXIT=%ERRORLEVEL%"
if not "%FRONTEND_BUILD_EXIT%"=="0" (
    popd
    goto :failed
)
popd

echo.
echo [完成] 本地环境已准备好。请先启动 MongoDB，然后双击 start.bat。
call :pause_if_needed
exit /b 0

:failed
echo.
echo [错误] 安装没有完成，请检查上方提示后重试。
call :pause_if_needed
exit /b 1

:pause_if_needed
if not defined NO_PAUSE pause
exit /b 0

:ensure_tk_runtime
echo [2/5] 检查桌面 GUI 运行库...
set "LOCAL_TCL_ROOT=%CD%\.venv\tcl-runtime"
if exist "%LOCAL_TCL_ROOT%\tcl8.6\init.tcl" (
    set "TCL_LIBRARY=%LOCAL_TCL_ROOT%\tcl8.6"
    set "TK_LIBRARY=%LOCAL_TCL_ROOT%\tk8.6"
    ".venv\Scripts\python.exe" -c "import tkinter as tk; root=tk.Tk(); root.withdraw(); root.destroy()" >nul 2>nul
    if not errorlevel 1 exit /b 0
)

set "TCL_LIBRARY="
set "TK_LIBRARY="
".venv\Scripts\python.exe" -c "import tkinter as tk; root=tk.Tk(); root.withdraw(); root.destroy()" >nul 2>nul
if not errorlevel 1 exit /b 0

set "BASE_TCL_ROOT="
for /f "delims=" %%I in ('".venv\Scripts\python.exe" -c "import sys; from pathlib import Path; print(Path(sys.base_prefix) / 'tcl')"') do set "BASE_TCL_ROOT=%%I"
if not defined BASE_TCL_ROOT (
    echo [错误] 无法定位 Python 的 Tcl/Tk 运行库。
    exit /b 1
)
if not exist "%BASE_TCL_ROOT%\tcl8.6\init.tcl" (
    echo [错误] Python 安装缺少 Tcl/Tk，请安装包含 Tcl/Tk 的 64 位 Python 3.11/3.12。
    exit /b 1
)

if not exist "%LOCAL_TCL_ROOT%" mkdir "%LOCAL_TCL_ROOT%"
xcopy "%BASE_TCL_ROOT%\*" "%LOCAL_TCL_ROOT%\" /E /I /Y /Q >nul
if errorlevel 1 (
    echo [错误] 无法复制 Tcl/Tk 到本地虚拟环境。
    exit /b 1
)

set "TCL_LIBRARY=%LOCAL_TCL_ROOT%\tcl8.6"
set "TK_LIBRARY=%LOCAL_TCL_ROOT%\tk8.6"
".venv\Scripts\python.exe" -c "import tkinter as tk; root=tk.Tk(); root.withdraw(); root.destroy()" >nul 2>nul
if errorlevel 1 (
    echo [错误] Tcl/Tk 本地修复后仍无法创建桌面窗口，请重新安装 Python 3.11/3.12。
    exit /b 1
)
echo [信息] 已为当前虚拟环境准备独立 Tcl/Tk 运行库。
exit /b 0
