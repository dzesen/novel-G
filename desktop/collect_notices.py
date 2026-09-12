"""Preserve vendor license files alongside the staged desktop preview."""
from pathlib import Path
from importlib import metadata
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]

def collect(output: Path, frontend: Path = ROOT / "frontend"):
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "desktop/licenses", output / "microsoft", dirs_exist_ok=True)
    index = []
    for package in metadata.distributions():
        name, version = package.metadata["Name"], package.version
        index.append({"name": name, "version": version, "license": package.metadata.get("License-Expression") or package.metadata.get("License", ""), "project_urls": package.metadata.get_all("Project-URL") or []})
        for relative in package.files or []:
            source = Path(package.locate_file(relative))
            if source.is_file() and source.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "AUTHORS")):
                target = output / "python" / (name + "-" + version) / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    for package_path in (frontend / "node_modules").rglob("package.json"):
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        name, version = package.get("name"), package.get("version")
        if not name or not version:
            continue
        directory = output / "node-packages" / (name.replace("/", "_") + "-" + version)
        for source in package_path.parent.iterdir():
            if source.is_file() and source.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                directory.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, directory / source.name)
    for source, name in [(Path(sys.base_prefix) / "LICENSE.txt", "Python-LICENSE.txt"), (ROOT / "reports/desktop-toolchain/dotnet/LICENSE.txt", "dotnet-LICENSE.txt"), (ROOT / "reports/desktop-toolchain/dotnet/ThirdPartyNotices.txt", "dotnet-ThirdPartyNotices.txt")]:
        if source.exists():
            shutil.copy2(source, output / name)
    (output / "python-packages.json").write_text(json.dumps(index, indent=2), encoding="utf-8")

if __name__ == "__main__":
    collect(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else ROOT / "frontend")
