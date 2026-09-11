"""Install and check the local source release using only the Python standard library."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
PIP_VERSION = "26.2.1"
STATE_VERSION = 1
SOURCE_EXCLUDES = {"node_modules", ".next", ".git", ".npm-cache", "test-results", "playwright-report"}


class InstallError(RuntimeError):
    pass


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def hash_files(root: Path, *, exclude: set[str] | None = None) -> str:
    entries = []
    for directory, folders, files in os.walk(root):
        folders[:] = sorted(name for name in folders if name not in (exclude or set()))
        for name in sorted(files):
            if name in {"next-env.d.ts", "tsconfig.tsbuildinfo"}:
                continue
            path = Path(directory) / name
            entries.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    return digest(entries)


class Runner:
    def __init__(self, log: Callable[[str], None]):
        self.log = log

    def run(self, command: list[str], cwd: Path, env: dict[str, str], *, stream: bool = False, timeout: int = 30) -> str:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        if not stream:
            try:
                result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=timeout, creationflags=flags)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise InstallError(f"无法完成检查（{type(exc).__name__}）。") from exc
            if result.returncode:
                raise InstallError(result.stdout.strip() or result.stderr.strip() or f"检查退出码 {result.returncode}")
            return result.stdout.strip()
        try:
            proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", creationflags=flags,
                                    start_new_session=os.name != "nt")
        except OSError as exc:
            raise InstallError(f"无法启动安装步骤（{type(exc).__name__}）。") from exc
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self.log(line.rstrip())
            if proc.wait():
                raise InstallError(f"步骤退出码 {proc.returncode}；修正上方错误后重新运行 setup.bat。")
        except BaseException:
            if proc.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                                   creationflags=flags, timeout=10, check=False)
                else:
                    os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
            raise
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
        return ""


@contextmanager
def installation_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise InstallError("另一个安装或检查正在运行，请等待它结束后重试。") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Installer:
    def __init__(self, root: Path, *, runner=None, log=print, env: dict[str, str] | None = None):
        self.root = root
        self.frontend = root / "frontend"
        self.python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.state_path = root / "reports" / "installation" / "state.json"
        self.log = log
        self.runner = runner or Runner(log)
        self.env = dict(os.environ if env is None else env)
        self.env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PIP_DISABLE_PIP_VERSION_CHECK="1",
                        npm_config_cache=str(root / ".npm-cache"), npm_config_update_notifier="false")
        self.state: dict = {}
        self.node = ""
        self.npm = ""
        self.runtime = ""

    def command(self, command: list[str], *, frontend: bool = False, stream: bool = False, timeout: int = 30) -> str:
        return self.runner.run(command, self.frontend if frontend else self.root, self.env,
                               stream=stream, timeout=timeout)

    def prerequisites(self) -> None:
        errors = []
        if not (3, 11) <= sys.version_info < (3, 13) or struct.calcsize("P") != 8:
            errors.append("请安装 64 位 Python 3.11 或 3.12（含 Tcl/Tk）。")
        for name in ("requirements.txt", "launcher.py", "frontend/package.json", "frontend/package-lock.json"):
            if not (self.root / name).is_file():
                errors.append(f"源码包缺少 {name}，请重新解压完整发布包。")
        self.node = shutil.which("node", path=self.env.get("PATH")) or ""
        self.npm = shutil.which("npm.cmd" if os.name == "nt" else "npm", path=self.env.get("PATH")) or ""
        if not self.node or not self.npm:
            errors.append("未找到 Node.js 或 npm。请安装 Node.js 22.13+ 或 24 LTS，再重新打开 setup.bat。")
        else:
            try:
                version = self.command([self.node, "-p", "JSON.stringify({version:process.versions.node,arch:process.arch})"])
                data = json.loads(version)
                major, minor, *_ = map(int, data["version"].split("."))
                if not (major >= 24 or (major == 22 and minor >= 13)) or data["arch"] not in {"x64", "arm64"}:
                    raise ValueError
                npm_version = self.command([self.npm, "--version"])
                self.runtime = digest([data, npm_version, str(self.root.resolve())])
            except (InstallError, ValueError, KeyError, TypeError):
                errors.append("Node.js/npm 无法运行或版本不受支持。请安装 64 位 Node.js 22.13+ 或 24 LTS。")
        if errors:
            raise InstallError("\n".join(errors))
        self.log("[OK] Python、Node.js、npm 和源码文件检查通过。")

    def load_state(self) -> None:
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.state = saved["steps"] if saved.get("version") == STATE_VERSION and isinstance(saved.get("steps"), dict) else {}
        except (OSError, ValueError, TypeError, AttributeError):
            self.state = {}

    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": STATE_VERSION, "steps": self.state}, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.state_path)

    def ensure_python(self, *, check: bool) -> None:
        if not self.python.is_file():
            if check:
                raise InstallError("尚未创建本地 Python 环境，请运行 setup.bat。")
            self.log("[1/4] 创建本地 Python 环境…")
            self.command([sys.executable, "-m", "venv", str(self.root / ".venv")], stream=True)
        try:
            self.command([str(self.python), "-c", "import sys,struct; assert (3,11)<=sys.version_info<(3,13) and struct.calcsize('P')==8"])
        except InstallError as exc:
            raise InstallError("现有 .venv 不兼容或已损坏。请关闭程序并将 .venv 重命名后重新运行 setup.bat。") from exc
        if os.name != "nt":
            return
        self.env.pop("TCL_LIBRARY", None)
        self.env.pop("TK_LIBRARY", None)
        local = self.root / ".venv" / "tcl-runtime"
        probe = [str(self.python), "-c", "import tkinter as tk; root=tk.Tk(); root.withdraw(); root.destroy()"]
        if (local / "tcl8.6" / "init.tcl").is_file():
            self.env.update(TCL_LIBRARY=str(local / "tcl8.6"), TK_LIBRARY=str(local / "tk8.6"))
        try:
            self.command(probe)
            return
        except InstallError:
            if check:
                raise InstallError("Tcl/Tk 桌面运行库检查失败，请运行 setup.bat 修复。")
        self.env.pop("TCL_LIBRARY", None)
        self.env.pop("TK_LIBRARY", None)
        try:
            self.command(probe)
            return
        except InstallError:
            base = Path(self.command([str(self.python), "-c", "import sys; print(sys.base_prefix)"])) / "tcl"
            if not (base / "tcl8.6" / "init.tcl").is_file():
                raise InstallError("Python 缺少 Tcl/Tk，请重新安装包含 Tcl/Tk 的 Python 3.11/3.12。")
            shutil.copytree(base, local, dirs_exist_ok=True)
            self.env.update(TCL_LIBRARY=str(local / "tcl8.6"), TK_LIBRARY=str(local / "tk8.6"))
            self.command(probe)
            self.log("[OK] 已准备本地 Tcl/Tk 桌面运行库。")

    def backend_artifact(self) -> str:
        self.command([str(self.python), "-m", "pip", "check"])
        packages = self.command([str(self.python), "-c",
            "import customtkinter,fastapi,pymongo,yaml; import importlib.metadata as m,json; "
            "print(json.dumps(sorted((d.metadata['Name'],d.version) for d in m.distributions())))"])
        return digest(packages)

    def frontend_artifact(self) -> str:
        modules = self.frontend / "node_modules"
        for name in ("next/dist/bin/next", "react/package.json", ".package-lock.json"):
            if not (modules / name).is_file():
                raise InstallError("前端依赖文件不完整，请重新运行 setup.bat。")
        tree = json.loads(self.command([self.npm, "ls", "--depth=0", "--json"], frontend=True))
        return digest([tree, hashlib.sha256((modules / ".package-lock.json").read_bytes()).hexdigest()])

    def build_artifact(self) -> str:
        build = self.frontend / ".next"
        for name in ("BUILD_ID", "required-server-files.json", "server", "static"):
            if not (build / name).exists():
                raise InstallError("前端生产构建不完整，请重新运行 setup.bat。")
        return hash_files(build, exclude={"cache", "diagnostics"})

    def step(self, name: str, title: str, inputs: Callable[[], str], probe: Callable[[], str],
             action: Callable[[], None], *, repair: bool, check: bool) -> None:
        before = inputs()
        saved = self.state.get(name)
        if isinstance(saved, dict) and saved.get("input") == before and not repair:
            try:
                if saved.get("artifact") == probe():
                    self.log(f"{title} 已验证，复用上次结果。")
                    return
            except (InstallError, OSError, ValueError):
                pass
        if check:
            raise InstallError(f"{title} 需要安装或更新，请运行 setup.bat。")
        # Invalidate before mutation, so a failed repair never certifies an old result.
        self.state.pop(name, None)
        self.save_state()
        self.log(f"{title} 正在执行…")
        action()
        artifact = probe()
        if before != inputs():
            raise InstallError("安装期间源码或配置发生变化，请保存修改后重新运行 setup.bat。")
        self.state[name] = {"input": before, "artifact": artifact}
        self.save_state()
        self.log(f"{title} 完成。")

    def run(self, *, repair: bool = False, check: bool = False) -> int:
        # Fail prerequisites before creating a venv or downloading anything.
        self.prerequisites()
        with installation_lock(self.state_path.with_suffix(".lock")):
            self.load_state()
            self.ensure_python(check=check)
            python_identity = self.command([str(self.python), "-c", "import sys; print(sys.version); print(sys.base_prefix)"])
            backend_input = lambda: digest([PIP_VERSION, python_identity, str(self.python.resolve()),
                                          (self.root / "requirements.txt").read_bytes().hex()])
            frontend_input = lambda: digest([self.runtime, (self.frontend / "package.json").read_bytes().hex(),
                                            (self.frontend / "package-lock.json").read_bytes().hex()])
            build_input = lambda: digest([frontend_input(), hash_files(self.frontend, exclude=SOURCE_EXCLUDES),
                                         {key: value for key, value in self.env.items() if key.startswith("NEXT_PUBLIC_")}])

            def install_backend():
                base = [str(self.python), "-m", "pip", "install", "--timeout", "30", "--retries", "2"]
                self.command([*base, "--upgrade", f"pip=={PIP_VERSION}"], stream=True)
                self.command([*base, *(["--force-reinstall"] if repair else []), "-r", "requirements.txt"], stream=True)

            self.step("backend", "[2/4] 后端依赖", backend_input, self.backend_artifact, install_backend, repair=repair, check=check)
            self.step("frontend", "[3/4] 前端依赖", frontend_input, self.frontend_artifact,
                      lambda: self.command([self.npm, "ci", "--include=dev", "--no-audit", "--no-fund"], frontend=True, stream=True), repair=repair, check=check)
            self.step("build", "[4/4] 前端生产构建", build_input, self.build_artifact,
                      lambda: self.command([self.npm, "run", "build"], frontend=True, stream=True), repair=repair, check=check)
            try:
                result = self.command([str(self.python), "-m", "backend.preflight"], timeout=20)
            except InstallError as exc:
                self.log(f"[待处理] 依赖和构建已完成，但运行环境尚未就绪。\n{exc}")
                self.log("启动 MongoDB 或修正本地配置后，运行 setup.bat --check 重新检查。")
                return 2
            self.log(result)
            self.log("[完成] 本地环境已就绪，可以双击 start.bat。")
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Novel-G 本地安装、修复和环境检查")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="检查安装结果与 MongoDB，不下载依赖或重新构建")
    mode.add_argument("--repair", action="store_true", help="重新安装依赖并构建，保留配置与数据")
    parser.add_argument("--no-pause", action="store_true", help="由批处理入口处理")
    args = parser.parse_args()
    logs = ROOT / "logs" / "installation"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"setup-{datetime.now():%Y%m%d-%H%M%S-%f}-{os.getpid()}.log"
    with log_path.open("w", encoding="utf-8", buffering=1) as output:
        def log(message: str):
            print(message, flush=True)
            output.write(message + "\n")
        log(f"[日志] {log_path}")
        try:
            return Installer(ROOT, log=log).run(repair=args.repair, check=args.check)
        except KeyboardInterrupt:
            log("[已取消] 下次运行 setup.bat 将检查并复用已完成的步骤。")
            return 130
        except (InstallError, OSError, ValueError) as exc:
            log(f"[错误] {exc}")
            log("安装未完成。修正错误后重新运行 setup.bat；已完成步骤会保留。")
            return 1
        finally:
            log(f"[日志] 详细记录保存在 {log_path}")


if __name__ == "__main__":
    raise SystemExit(main())
