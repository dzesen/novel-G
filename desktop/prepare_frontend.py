"""Copy frontend build inputs into a fresh, isolated directory."""
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
output = Path(sys.argv[1]).resolve()
if output.exists():
    raise SystemExit("Use a fresh frontend build directory")
if not output.is_relative_to((ROOT / "reports").resolve()):
    raise SystemExit("Frontend build directory must be inside reports")
shutil.copytree(ROOT / "frontend", output, ignore=shutil.ignore_patterns("node_modules", ".next", "out", "test-results", "playwright-report", ".env", ".env.*", "*.tsbuildinfo"))
print(str(output))
