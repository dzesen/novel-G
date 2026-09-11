"""Build a deterministic Windows source archive from a committed Git tree."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = frozenset({
    ".git", ".venv", "node_modules", ".next", "__pycache__", ".pytest_cache",
    ".npm-cache", "reports", "logs", "backups", "managed-assets", "static",
    "dist", "test-results", ".claude", ".superpowers", ".impeccable", "work",
})
PRIVATE_NAMES = frozenset({
    "config.yaml", "config.yaml.bak", "config.json", ".config-secret-versions.json",
    ".provider-capabilities.json", "publish-public.ps1", "PUBLIC_RELEASE.md",
})
REQUIRED_FILES = frozenset({
    "VERSION", "LICENSE", "README.md", "README.zh-CN.md", "requirements.txt",
    "setup.bat", "start.bat", "verify.bat", "launcher.py", "main.py",
    "frontend/package.json", "frontend/package-lock.json",
    "backend/config/config_default.yaml", "scripts/build_release.py",
})
VERSION_PATTERN = re.compile(r"[0-9]+[.][0-9]+[.][0-9]+(?:-[0-9A-Za-z.-]+)?")


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def excluded_path(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe archive path: {name}")
    return (
        any(part in EXCLUDED_PARTS for part in path.parts)
        or path.name in PRIVATE_NAMES
        or path.name == ".env" or path.name.startswith(".env.")
        or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".pyc"}
    )


def build_release(root: Path, output_dir: Path, ref: str = "HEAD", *, preview: bool = False) -> dict[str, object]:
    root = root.resolve()
    commit = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    # Refuse to silently omit local edits from a release of the current checkout.
    if ref == "HEAD" and _git(root, "status", "--porcelain", "--untracked-files=normal").strip():
        raise ValueError("Commit the working tree before building a release")
    version = _git(root, "show", f"{commit}:VERSION").decode("utf-8").strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("VERSION must be a plain semantic version")
    files: dict[str, tuple[str, str]] = {}
    excluded: list[str] = []
    for record in _git(root, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not record:
            continue
        meta, raw_name = record.split(b"\t", 1)
        mode, kind, oid = meta.decode("ascii").split()
        name = raw_name.decode("utf-8")
        if excluded_path(name):
            excluded.append(name)
            continue
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError(f"Release source cannot contain symlinks or submodules: {name}")
        files[name] = (mode, oid)
    required = REQUIRED_FILES - {"LICENSE"} if preview else REQUIRED_FILES
    missing = required - files.keys()
    if missing:
        raise ValueError(f"Missing release inputs: {', '.join(sorted(missing))}")
    package = json.loads(_git(root, "show", f"{commit}:frontend/package.json"))
    lock = json.loads(_git(root, "show", f"{commit}:frontend/package-lock.json"))
    if package["version"] != version or lock["version"] != version:
        raise ValueError("VERSION and frontend package versions differ")
    epoch = int(_git(root, "show", "-s", "--format=%ct", commit))
    stamp = time.gmtime(max(315532800, min(epoch, 4354819198)))[:6]
    prefix = f"novel-g-{version}"
    output_dir.mkdir(parents=True, exist_ok=True)
    kind = "review" if preview else "source"
    archive = output_dir / f"{prefix}-{kind}.zip"
    checksums = output_dir / "SHA256SUMS.txt"
    if archive.exists() or checksums.exists():
        raise FileExistsError("Choose an empty output directory; existing releases are never overwritten")
    manifest: dict[str, object] = {
        "format": "novel-g-source-release", "version": version, "commit": commit,
        "distribution": "private-review" if preview else "source-release",
        "license": package.get("license", "UNCONFIRMED"),
        "source_epoch": epoch, "excluded": sorted(excluded), "files": {},
    }
    temporary = archive.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for name, (mode, oid) in sorted(files.items()):
                data = _git(root, "cat-file", "blob", oid)
                if name.endswith(".bat"):
                    data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                info = zipfile.ZipInfo(f"{prefix}/{name}", stamp)
                info.create_system = 3
                info.external_attr = (0o100755 if mode == "100755" else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                bundle.writestr(info, data)
                manifest["files"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            data = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
            info = zipfile.ZipInfo(f"{prefix}/RELEASE-MANIFEST.json", stamp)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, data)
        temporary.rename(archive)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksums.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    return {"archive": str(archive.resolve()), "sha256": digest, "commit": commit,
            "version": version, "files": len(files), "size": archive.stat().st_size}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true", help="Build a private review package while project licensing is pending")
    parser.add_argument("--ref", default="HEAD", help="Committed source revision (default: HEAD)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    try:
        result = build_release(ROOT, args.output_dir, args.ref, preview=args.preview)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Release build failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
