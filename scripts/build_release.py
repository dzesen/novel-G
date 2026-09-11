"""Build a deterministic Windows source archive from a committed Git tree."""
from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = frozenset({
    ".git", ".venv", "node_modules", ".next", "__pycache__", ".pytest_cache",
    ".npm-cache", "reports", "logs", "backups", "managed-assets", "static",
    "dist", "test-results", "playwright-report", "coverage", ".claude",
    ".superpowers", ".impeccable", "work", "tests", "e2e",
})
PRIVATE_NAMES = frozenset({
    "config.yaml", "config.yaml.bak", "config.json", ".config-secret-versions.json",
    ".provider-capabilities.json", "publish-public.ps1", "PUBLIC_RELEASE.md",
})
PRIVATE_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".pyc"})
DOCUMENT_SUFFIXES = frozenset({".md", ".markdown", ".rst"})
PUBLIC_DOCUMENTS = frozenset({
    "docs/source-release.zh-CN.md",
    "docs/user-guide.zh-CN.md",
    "docs/troubleshooting.zh-CN.md",
    "docs/known-limitations.zh-CN.md",
    "docs/contributing.zh-CN.md",
    "docs/security.zh-CN.md",
    "docs/license-status.zh-CN.md",
    "docs/third-party-notices.zh-CN.md",
    "docs/releases/0.1.0-rc.1.zh-CN.md",
    "docs/releases/0.1.0-rc.2.zh-CN.md",
    "docs/releases/0.1.0-rc.3.zh-CN.md",
})
# The production job repository imports the outline journal, which has these
# transitive dependencies. Keep this closure until that coupling is removed.
RUNTIME_EVALUATION_FILES = frozenset({
    "backend/evaluation/__init__.py",
    "backend/evaluation/batch_job_acceptance_sample.py",
    "backend/evaluation/required_book_successor_acceptance.py",
    "backend/evaluation/required_book_successor_acceptance_identity.py",
    "backend/evaluation/required_book_successor_acceptance_ledger.py",
    "backend/evaluation/required_book_successor_acceptance_outline.py",
    "backend/evaluation/required_book_successor_judge_probe.py",
    "backend/evaluation/required_book_successor_judge_probe_execution.py",
    "backend/evaluation/required_book_successor_judge_probe_store.py",
})
RELEASE_FILES = frozenset({
    ".gitattributes", ".gitignore", "VERSION", "LICENSE", "README.md", "README.zh-CN.md",
    "requirements.txt", "setup.bat", "start.bat", "launcher.py", "main.py",
    "frontend/.gitignore", "frontend/package.json", "frontend/package-lock.json",
    "frontend/next.config.ts", "frontend/tsconfig.json", "frontend/postcss.config.mjs",
    "frontend/eslint.config.mjs", "scripts/build_release.py", "scripts/install_local.py",
}) | PUBLIC_DOCUMENTS | RUNTIME_EVALUATION_FILES
RUNTIME_TREES = ("backend", "frontend/src", "frontend/public")
DEVELOPMENT_PATHS = ("backend/evaluation", "backend/verification.py")
REQUIRED_FILES = (RELEASE_FILES - {"LICENSE"}) | {
    "backend/config/config_default.yaml", "backend/preflight.py", "backend/diagnostics.py",
}
VERSION_PATTERN = re.compile(r"[0-9]+[.][0-9]+[.][0-9]+(?:-[0-9A-Za-z.-]+)?")


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def excluded_path(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe archive path: {name}")
    if (
        any(part in EXCLUDED_PARTS for part in path.parts)
        or path.name in PRIVATE_NAMES
        or path.name == ".env" or path.name.startswith(".env.")
        or path.suffix.lower() in PRIVATE_SUFFIXES
    ):
        return True
    if name in RELEASE_FILES:
        return False
    if path.suffix.lower() in DOCUMENT_SUFFIXES:
        return True
    if any(name == prefix or name.startswith(prefix + "/") for prefix in DEVELOPMENT_PATHS):
        return True
    return not any(name.startswith(prefix + "/") for prefix in RUNTIME_TREES)


def release_attributes() -> str:
    """Render the matching policy for git archive and GitHub source downloads."""
    lines = [
        "# Auto detect text files and perform LF normalization",
        "* text=auto",
        "*.bat text eol=crlf",
        "",
        "# Application source exports only. The private checkout keeps development files.",
        "# Generated from scripts/build_release.py; regression tests enforce parity.",
        "* export-ignore",
    ]
    directories: set[str] = set()
    for name in RELEASE_FILES | set(RUNTIME_TREES):
        directories.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != ".")
    directories.update(RUNTIME_TREES)
    for directory in sorted(directories, key=lambda value: (value.count("/"), value)):
        lines.append(f"/{directory} -export-ignore")
    for directory in RUNTIME_TREES:
        lines.append(f"/{directory}/** -export-ignore")
    for name in DEVELOPMENT_PATHS:
        lines.append(f"/{name} export-ignore")
    # Excluding the directory itself would prevent archive from visiting the
    # exact production dependencies that are re-included below.
    lines.append("/backend/evaluation -export-ignore")
    lines.append("/backend/evaluation/** export-ignore")
    for suffix in sorted(DOCUMENT_SUFFIXES):
        pattern = "".join(
            f"[{char.lower()}{char.upper()}]" if char.isalpha() else char
            for char in suffix
        )
        lines.append(f"*{pattern} export-ignore")
    for name in sorted(RELEASE_FILES):
        lines.append(f"/{name} -export-ignore")
    for part in sorted(EXCLUDED_PARTS):
        lines.extend((f"{part} export-ignore", f"**/{part}/** export-ignore"))
    for name in sorted(PRIVATE_NAMES):
        lines.append(f"{name} export-ignore")
    lines.extend((".env export-ignore", ".env.* export-ignore"))
    for suffix in sorted(PRIVATE_SUFFIXES):
        pattern = "".join(
            f"[{char.lower()}{char.upper()}]" if char.isalpha() else char
            for char in suffix
        )
        lines.append(f"*{pattern} export-ignore")
    return "\n".join(lines) + "\n"


def validate_document_links(documents: dict[str, bytes], included: set[str]) -> None:
    """Reject local Markdown links whose targets would be missing from the package."""
    for name, data in documents.items():
        content = data.decode("utf-8-sig")
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", content):
            link = urlsplit(target.strip("<>"))
            if link.scheme or link.netloc or not link.path:
                continue
            resolved = posixpath.normpath(
                posixpath.join(str(PurePosixPath(name).parent), unquote(link.path))
            )
            if resolved not in included:
                raise ValueError(f"Public document link is outside the release: {name} -> {resolved}")


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
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError(f"Release source cannot contain symlinks or submodules: {name}")
        if excluded_path(name):
            excluded.append(name)
            continue
        files[name] = (mode, oid)
    required = REQUIRED_FILES if preview else REQUIRED_FILES | {"LICENSE"}
    missing = required - files.keys()
    if missing:
        raise ValueError(f"Missing release inputs: {', '.join(sorted(missing))}")
    package = json.loads(_git(root, "show", f"{commit}:frontend/package.json"))
    lock = json.loads(_git(root, "show", f"{commit}:frontend/package-lock.json"))
    if package["version"] != version or lock["version"] != version:
        raise ValueError("VERSION and frontend package versions differ")
    documents = {
        name: _git(root, "cat-file", "blob", oid)
        for name, (_mode, oid) in files.items()
        if PurePosixPath(name).suffix.lower() in DOCUMENT_SUFFIXES
    }
    validate_document_links(documents, set(files))
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
        "source_epoch": epoch, "content_profile": "application-source-v1",
        "excluded_file_count": len(excluded), "files": {},
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
