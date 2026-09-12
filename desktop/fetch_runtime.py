"""Download pinned build/runtime archives with mandatory content hashes."""
from pathlib import Path
import hashlib
import json
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "reports/desktop-toolchain"

def extract_zip(archive: Path, destination: Path, mongo=False):
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            path = (destination / entry.filename).resolve()
            if not path.is_relative_to(destination.resolve()):
                raise ValueError("Archive path escapes destination")
            if mongo and not (entry.filename.endswith("/bin/mongod.exe") or Path(entry.filename).name in {"LICENSE-Community.txt", "MPL-2", "THIRD-PARTY-NOTICES"}):
                continue
            if path.is_file() and path.stat().st_size == entry.file_size:
                with path.open("rb") as existing, source.open(entry) as expected:
                    if hashlib.file_digest(existing, "sha256").digest() == hashlib.file_digest(expected, "sha256").digest():
                        continue  # Shared build servers may still hold identical SDK DLLs open.
            source.extract(entry, destination)

if __name__ == "__main__":
    TARGET.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT / "desktop/runtime-lock.json").read_text())
    for name, item in manifest.items():
        archive = TARGET / item["file"]
        if not archive.exists():
            partial = archive.with_suffix(archive.suffix + ".partial")
            print("Downloading " + name, flush=True)
            urllib.request.urlretrieve(item["url"], partial)
            partial.replace(archive)
        kind = "sha512" if "sha512" in item else "sha256"
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, kind).hexdigest()
        if digest != item[kind]:
            raise SystemExit("Checksum mismatch: " + item["file"])
        print("Verified " + name, flush=True)
        if name in {"node", "dotnet", "mongodb"}:
            extract_zip(archive, TARGET / name, mongo=name == "mongodb")
        elif name == "webview2":
            destination = TARGET / name
            destination.mkdir(exist_ok=True)
            subprocess.run(["expand.exe", str(archive), "-F:*", str(destination)], check=True, stdout=subprocess.DEVNULL)
