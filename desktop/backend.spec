from pathlib import Path
import runpy
from PyInstaller.utils.hooks import collect_submodules
# Only immutable default resources are bundled; never collect the config directory.
root = Path(SPECPATH).parent
release_policy = runpy.run_path(str(root / "scripts/build_release.py"))
excluded_path = release_policy["excluded_path"]
modules = collect_submodules("backend")
def is_public_module(name):
    relative = name.replace(".", "/")
    path = relative + ("/__init__.py" if (root / relative).is_dir() else ".py")
    return not excluded_path(path)
private_modules = [name for name in modules if not is_public_module(name)]
analysis = Analysis(
    [str(root / "backend/desktop/entry.py")], pathex=[str(root)],
    binaries=[], datas=[
        (str(root / "backend/config/config_default.yaml"), "backend/config"),
        (str(root / "backend/llm/prompts/prompt_default.yaml"), "backend/llm/prompts"),
    ],
    hiddenimports=[name for name in modules if is_public_module(name)] + ["main", "uvicorn.logging", "uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["pytest", "tkinter", "customtkinter"] + private_modules, noarchive=False,
)
for name, source, _kind in analysis.pure:
    if name.startswith("backend.") and not is_public_module(name):
        raise ValueError("Private module entered desktop bundle: " + name)
archive = PYZ(analysis.pure)
executable = EXE(archive, analysis.scripts, [], exclude_binaries=True, name="novel-g-service",
                 debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True)
bundle = COLLECT(executable, analysis.binaries, analysis.datas, strip=False, upx=False, name="novel-g-service")
