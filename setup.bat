@echo off
setlocal
chcp 65001 >nul
cd /d %~dp0

echo [1/5] 检查 Python...
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.9 或更高版本。
    pause
    exit /b 1
)
python -c "import sys; raise SystemExit(0 if sys.version_info ^>= (3, 9) else 1)"
if errorlevel 1 (
    echo [错误] Python 版本过低，至少需要 Python 3.9，推荐 3.10 或更高版本。
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [2/5] 创建本地 Python 环境...
    python -m venv .venv
    if errorlevel 1 goto :failed
) else (
    echo [2/5] 本地 Python 环境已存在。
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
