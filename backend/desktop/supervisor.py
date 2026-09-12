"""Own one isolated MongoDB/backend/frontend set and stop it in dependency order."""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import httpx
from pymongo import MongoClient
from pymongo.errors import AutoReconnect, ConnectionFailure, OperationFailure
import yaml


class DesktopFailure(RuntimeError):
    pass


def runtime_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not relative or Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise DesktopFailure("runtime_missing")
    if not path.exists():
        raise DesktopFailure("runtime_missing")
    return path


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


class DataLock:
    def __init__(self, path: Path):
        self.file = path.open("a+b")
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                if not path.stat().st_size:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            raise DesktopFailure("already_running") from error

    def close(self) -> None:
        self.file.close()


class Supervisor:
    def __init__(self, runtime: Path, data: Path, language: str):
        self.runtime, self.data, self.language = runtime, data, language
        self.children: dict[str, asyncio.subprocess.Process] = {}
        self.log_files: list = []
        self.mongo_uri = ""
        self.mongo_port = 0
        self.lock: DataLock | None = None
        self.protocol = sys.stdout
        self.log_dir = data / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (data / "reports").mkdir(parents=True, exist_ok=True)

    def emit(self, state: str, detail: str | None = None, **values) -> None:
        stream = self.protocol
        if stream is None:
            return
        try:
            print(json.dumps({"state": state, "detail": detail or state + "_detail", **values}), file=stream, flush=True)
        except OSError:
            # A crashed native host closes stdout as well as stdin. Losing the
            # status channel must never interrupt database shutdown.
            self.protocol = None
            if sys.stdout is stream:
                sys.stdout = open(os.devnull, "w", encoding="utf-8")

    def log(self, message: str) -> None:
        with (self.log_dir / "desktop-services.log").open("a", encoding="utf-8") as output:
            output.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")

    async def spawn(self, name: str, arguments: list[str], environment: dict | None = None) -> None:
        output = (self.log_dir / f"desktop-{name}.log").open("ab")
        self.log_files.append(output)
        pending = asyncio.create_task(asyncio.create_subprocess_exec(
            *arguments, cwd=self.data, env=environment or os.environ.copy(),
            stdin=asyncio.subprocess.PIPE, stdout=output, stderr=output,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        ))
        try:
            self.children[name] = await asyncio.shield(pending)
        except asyncio.CancelledError:
            # Ownership must be recorded even if Close arrives during process creation.
            self.children[name] = await pending
            raise

    def initialize_data(self) -> None:
        from backend.config.config import ensure_config_files, CONFIG_PATH

        marker = self.data / "reports" / "desktop-data.json"
        if marker.exists():
            value = json.loads(marker.read_text(encoding="utf-8"))
            if value.get("schema_version") != 1 or value.get("mongo_major") != 8:
                raise DesktopFailure("data_incompatible")
        elif (self.data / "mongodb").exists() and any((self.data / "mongodb").iterdir()):
            raise DesktopFailure("data_incompatible")
        ensure_config_files()
        config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        previous = config.get("_desktop_runtime", {})
        if previous and (not isinstance(previous, dict) or previous.get("schema_version") != 1):
            raise DesktopFailure("data_incompatible")
        password = previous.get("mongo_password") or secrets.token_urlsafe(40)
        self.mongo_uri = f"mongodb://novel_g_desktop:{password}@127.0.0.1:{self.mongo_port}/?authSource=admin"
        config["_desktop_runtime"] = {"schema_version": 1, "mongo_password": password}
        config["mongodb_url"] = self.mongo_uri
        config["mongo_database_name"] = "novel_g_desktop"
        # Both internal credentials and provider keys stay in this one private config file.
        temporary = CONFIG_PATH.with_suffix(".tmp")
        temporary.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        temporary.replace(CONFIG_PATH)
        write_json(marker, {"schema_version": 1, "mongo_major": 8})

    def authenticate_mongo(self) -> bool:
        with MongoClient(self.mongo_uri, serverSelectionTimeoutMS=700, connectTimeoutMS=700) as client:
            try:
                client.admin.command("ping")
                # ping does not always require authentication; this operation does.
                client.admin.command("connectionStatus")
                return True
            except OperationFailure as error:
                if error.code != 18:
                    raise
        # --auth remains enabled during initialization. Mongo's localhost exception
        # allows only first-user creation while no users exist; no unauthenticated server.
        with MongoClient(f"mongodb://127.0.0.1:{self.mongo_port}", serverSelectionTimeoutMS=700) as bootstrap:
            password = urlsplit(self.mongo_uri).password
            try:
                bootstrap.admin.command("createUser", "novel_g_desktop", pwd=password, roles=["root"])
            except OperationFailure:
                return False
        return True

    async def wait_mongo(self) -> None:
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            if self.children["mongodb"].returncode is not None:
                raise DesktopFailure("database_failed")
            try:
                if await asyncio.to_thread(self.authenticate_mongo):
                    return
            except (ConnectionFailure, OperationFailure):
                pass
            await asyncio.sleep(0.3)
        raise DesktopFailure("database_failed")

    async def wait_http(self, name: str, address: str, identity: str | None = None) -> None:
        deadline = time.monotonic() + 90
        async with httpx.AsyncClient(trust_env=False, timeout=2, follow_redirects=True) as client:
            while time.monotonic() < deadline:
                if self.children[name].returncode is not None:
                    raise DesktopFailure("service_failed")
                try:
                    response = await client.get(address)
                    if response.status_code == 200 and (identity is None or response.json().get("service") == identity):
                        return
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(0.4)
        raise DesktopFailure("service_failed")

    async def start(self) -> None:
        if self.data.is_relative_to(self.runtime):
            raise DesktopFailure("data_incompatible")
        self.lock = DataLock(self.data / "reports" / "desktop-services.lock")
        if os.name == "nt" and not ctypes.windll.kernel32.IsProcessorFeaturePresent(39):
            raise DesktopFailure("unsupported_cpu")
        manifest = json.loads((self.runtime / "desktop-runtime.json").read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 1:
            raise DesktopFailure("runtime_missing")
        paths = {key: runtime_path(self.runtime, manifest[key]) for key in ("backend", "mongodb", "node", "frontend")}
        ports: set[int] = set()
        while len(ports) < 3:
            ports.add(free_port())
        self.mongo_port, backend_port, frontend_port = sorted(ports)
        self.initialize_data()
        mongo_dir = self.data / "mongodb"
        mongo_dir.mkdir(exist_ok=True)
        self.emit("starting", "database")
        await self.spawn("mongodb", [str(paths["mongodb"]), "--dbpath", str(mongo_dir), "--bind_ip", "127.0.0.1",
                                     "--port", str(self.mongo_port), "--auth", "--wiredTigerCacheSizeGB", "0.25"])
        await self.wait_mongo()
        frontend = f"http://127.0.0.1:{frontend_port}"
        backend = f"http://127.0.0.1:{backend_port}"
        environment = os.environ.copy()
        environment.update(NOVEL_G_CORS_ORIGINS=frontend, NOVEL_G_BACKEND_HOST="127.0.0.1", PYTHONUTF8="1")
        self.emit("starting", "backend")
        backend_command = [str(paths["backend"])]
        if not getattr(sys, "frozen", False):
            backend_command = [sys.executable, "-m", "backend.desktop.entry"]
        await self.spawn("backend", backend_command + ["serve", "--port", str(backend_port), "--data-dir", str(self.data)], environment)
        await self.wait_http("backend", backend + "/api/health", "novel-g-backend")
        self.emit("starting", "frontend")
        environment.update(PORT=str(frontend_port), HOSTNAME="127.0.0.1", NODE_ENV="production", NEXT_TELEMETRY_DISABLED="1")
        await self.spawn("frontend", [str(paths["node"]), str(paths["frontend"])], environment)
        await self.wait_http("frontend", frontend + "/" + self.language)
        self.emit("running", frontend=frontend, backend=backend)

    def shutdown_mongo(self) -> None:
        with MongoClient(self.mongo_uri, serverSelectionTimeoutMS=2000) as client:
            try:
                client.admin.command("shutdown", force=False, timeoutSecs=30)
            except AutoReconnect:
                pass  # Mongo closes the connection while completing normal shutdown.

    async def stop(self) -> bool:
        self.emit("stopping")
        # Backend must finish writing before the database can be stopped.
        backend = self.children.get("backend")
        if backend and backend.returncode is None:
            if backend.stdin:
                backend.stdin.close()
            try:
                await asyncio.wait_for(backend.wait(), timeout=60)
            except TimeoutError:
                self.emit("failed", "stop_blocked")
                return False
        frontend = self.children.get("frontend")
        if frontend and frontend.returncode is None:
            frontend.terminate()
            await frontend.wait()
        mongo = self.children.get("mongodb")
        if mongo and mongo.returncode is None:
            try:
                await asyncio.to_thread(self.shutdown_mongo)
                await asyncio.wait_for(mongo.wait(), timeout=45)
            except (ConnectionFailure, OperationFailure, TimeoutError):
                self.emit("failed", "stop_blocked")
                return False
        self.emit("idle")
        return True

    async def run(self) -> None:
        commands: asyncio.Queue[str] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def read_commands() -> None:
            for line in sys.stdin:
                try:
                    if json.loads(line).get("command") == "stop":
                        loop.call_soon_threadsafe(commands.put_nowait, "stop")
                except (ValueError, AttributeError):
                    pass
            loop.call_soon_threadsafe(commands.put_nowait, "eof")

        threading.Thread(target=read_commands, daemon=True).start()
        start = asyncio.create_task(self.start())
        command = asyncio.create_task(commands.get())
        failure: str | None = None
        try:
            done, _ = await asyncio.wait((start, command), return_when=asyncio.FIRST_COMPLETED)
            if start in done:
                try:
                    start.result()
                except Exception as error:
                    failure = str(error) if isinstance(error, DesktopFailure) else "runtime_missing"
                    self.log(f"startup failed: {type(error).__name__} ({failure})")
                if failure is None:
                    watchers = [asyncio.create_task(child.wait()) for child in self.children.values()]
                    exited, _ = await asyncio.wait([command, *watchers], return_when=asyncio.FIRST_COMPLETED)
                    if command not in exited:
                        failure = "service_failed"
                    for task in watchers:
                        task.cancel()
            else:
                start.cancel()
                await asyncio.gather(start, return_exceptions=True)
            while not await self.stop():
                # Keep ownership and allow a retry. After parent death, retry ourselves.
                await asyncio.sleep(5)
            if failure:
                self.emit("failed", failure)
        finally:
            command.cancel()
            if self.lock:
                self.lock.close()
            for output in self.log_files:
                output.close()
