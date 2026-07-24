@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo [1/5] 检查 Python...
set "PYTHON_CMD="
set "USE_EXISTING_VENV="

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 13) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo [错误] 现有 .venv 不是 Python 3.11/3.12。请先关闭程序，将 .venv 重命名或删除后重新运行 setup.bat。
        pause
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
        pause
        exit /b 1
    )

    echo [2/5] 创建本地 Python 环境...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 goto :failed
) else (
    echo [2/5] 本地 Python 3.11/3.12 环境已存在。
)

echo [3/5] 安装后端依赖...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo [4/5] 安装前端依赖...
where npm.cmd >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 npm，请安装 Node.js 20.9 或更高版本。
    pause
    exit /b 1
)
node -e "const [a,b]=process.versions.node.split('.').map(Number); process.exit(a>20 || (a===20 && b>=9) ? 0 : 1)"
if errorlevel 1 (
    echo [错误] Node.js 版本过低，请安装 Node.js 20.9 或更高版本。
    pause
    exit /b 1
)

pushd frontend
if exist package-lock.json (
    call npm.cmd ci
) else (
    call npm.cmd install
)
if errorlevel 1 (
    popd
    goto :failed
)
echo [5/5] 生成可复用的前端生产构建...
call npm.cmd run build
if errorlevel 1 (
    popd
    goto :failed
)
popd

echo.
echo [完成] 本地环境已准备好。请先启动 MongoDB，然后双击 start.bat。
pause
exit /b 0

:failed
echo.
echo [错误] 安装没有完成，请检查上方提示后重试。
pause
exit /b 1
