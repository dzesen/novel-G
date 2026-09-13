"""Assemble only build artifacts and pinned vendor runtimes into a fresh directory."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil

ROOT = Path(__file__).resolve().parents[1]

def copy_tree(source, target):
    shutil.copytree(source, target)

def stage(output: Path, toolchain: Path, build: Path, frontend: Path):
    if output.exists():
        raise SystemExit("Stage directory must be new; choose another output path")
    lock = json.loads((ROOT / "desktop/runtime-lock.json").read_text())
    for item in lock.values():
        source = toolchain / item["file"]
        kind = "sha512" if "sha512" in item else "sha256"
        digest = hashlib.file_digest(source.open("rb"), kind).hexdigest()
        if digest != item[kind]:
            raise SystemExit("Runtime archive checksum mismatch: " + item["file"])
    copy_tree(build / "host", output)
    copy_tree(build / "backend/novel-g-service", output / "backend")
    copy_tree(frontend / ".next/standalone", output / "frontend")
    copy_tree(frontend / ".next/static", output / "frontend/.next/static")
    copy_tree(frontend / "public", output / "frontend/public")
    mongo = toolchain / "mongodb/mongodb-win32-x86_64-windows-8.0.32"
    copy_tree(mongo, output / "mongodb")
    copy_tree(toolchain / "webview2/Microsoft.WebView2.FixedVersionRuntime.153.0.4234.32.x64", output / "webview2")
    node = toolchain / "node/node-v24.21.0-win-x64"
    (output / "node").mkdir()
    for name in ("node.exe", "LICENSE"):
        shutil.copy2(node / name, output / "node" / name)
    shutil.copy2(ROOT / "LICENSE", output / "LICENSE.txt")
    shutil.copy2(ROOT / "desktop/runtime-lock.json", output / "runtime-lock.json")
    manifest = {"schema_version":1, "version":"0.1.0-preview.2", "backend":"backend/novel-g-service.exe",
                "node":"node/node.exe", "frontend":"frontend/server.js", "mongodb":"mongodb/bin/mongod.exe", "webview2":"webview2"}
    (output / "desktop-runtime.json").write_text(json.dumps(manifest, indent=2))
    forbidden = {"config.yaml", "config.yaml.bak", "prompt.yaml", ".config-secret-versions.json"}
    for path in output.rglob("*"):
        if path.is_file() and (path.name in forbidden or path.name.startswith(".env")):
            raise SystemExit("Private configuration found in staging: " + str(path.relative_to(output)))
    print(str(output))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--toolchain", type=Path, default=ROOT / "reports/desktop-toolchain")
    parser.add_argument("--build", type=Path, default=ROOT / "reports/desktop-build")
    parser.add_argument("--frontend", type=Path, default=ROOT / "frontend")
    args = parser.parse_args()
    stage(args.output.resolve(), args.toolchain.resolve(), args.build.resolve(), args.frontend.resolve())
