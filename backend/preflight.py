"""Local launcher preflight checks for the backend runtime."""

from __future__ import annotations

from urllib.parse import urlsplit

from pymongo import MongoClient

from backend.config.config import get_config_value


def _display_mongo_target(uri: str) -> str:
    """Return a credential-free MongoDB target for user-facing diagnostics."""
    parsed = urlsplit(uri)
    hostname = parsed.hostname or "localhost"
    port = parsed.port or 27017
    return f"{hostname}:{port}"


def run_preflight() -> tuple[bool, str]:
    """Check imports/config and verify that the configured MongoDB is reachable."""
    mongo_uri = str(get_config_value("mongodb_url", "mongodb://localhost:27017"))
    database_name = str(get_config_value("mongo_database_name", "novel_generator"))
    timeout_ms = min(int(get_config_value("mongo_timeout_ms", 5000)), 5000)
    target = _display_mongo_target(mongo_uri)

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=timeout_ms)
    try:
        client.admin.command("ping")
    except Exception as exc:
        return (
            False,
            f"[ERROR] 无法连接 MongoDB（{target}，{type(exc).__name__}）。\n"
            "请先启动 MongoDB，或在 backend/config/config.yaml 中修改连接地址。",
        )
    finally:
        client.close()

    return True, f"[OK] Python 依赖正常，MongoDB {target}/{database_name} 可连接。"


def main() -> int:
    try:
        is_ready, message = run_preflight()
    except Exception as exc:
        print(f"[ERROR] 后端配置或依赖检查失败：{exc}")
        print("请重新运行 setup.bat；如仍失败，请检查 backend/config/config.yaml。")
        return 1

    print(message)
    return 0 if is_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
