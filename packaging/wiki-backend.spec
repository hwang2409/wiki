import os
import shutil
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path.cwd()
MODE = os.environ.get("WIKI_PYINSTALLER_MODE", "onedir").strip().lower()
if MODE not in {"onefile", "onedir"}:
    raise SystemExit(f"Unsupported WIKI_PYINSTALLER_MODE={MODE!r}")

ONEDIR_NAME = "wiki-backend-sidecar"
FRONTEND_DIST = Path(os.environ.get("WIKI_FRONTEND_DIST", ROOT / "frontend" / "dist"))
DASHBOARD_STATIC_DIR = ROOT / "backend" / "app" / "dashboard_static"
PYINSTALLER_DIST = Path(os.environ.get("WIKI_PYINSTALLER_DIST", ROOT / "dist"))
LAUNCHER_PATH = PYINSTALLER_DIST / "wiki-backend"
SCHEMA_FILES = sorted((ROOT / "schemas").glob("*.schema.json"))
WORKGRAPH_TEMPLATE_FILES = sorted(
    (ROOT / "templates" / "workgraphs").glob("*.workgraph.tpl.json")
)


def write_onedir_launcher() -> None:
    launcher = """#!/bin/sh
set -eu
SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -x "$SELF_DIR/wiki-backend-sidecar/wiki-backend" ]; then
  TARGET="$SELF_DIR/wiki-backend-sidecar/wiki-backend"
elif [ -x "$SELF_DIR/../Resources/wiki-backend-sidecar/wiki-backend" ]; then
  TARGET="$SELF_DIR/../Resources/wiki-backend-sidecar/wiki-backend"
else
  echo "wiki-backend bundle not found" >&2
  exit 1
fi
exec "$TARGET" "$@"
"""
    LAUNCHER_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LAUNCHER_PATH.exists():
        if LAUNCHER_PATH.is_dir():
            shutil.rmtree(LAUNCHER_PATH)
        else:
            LAUNCHER_PATH.unlink()
    LAUNCHER_PATH.write_text(launcher, encoding="utf-8")
    LAUNCHER_PATH.chmod(0o755)

hiddenimports = (
    collect_submodules("fastapi")
    + collect_submodules("starlette")
    + collect_submodules("uvicorn")
    + [
        "backend.app.agent_runtime.daemon",
        "backend.app.knowledge",
        "backend.app.knowledge_content",
        "backend.app.knowledge_runs",
        "backend.app.knowledge_schema",
        "backend.app.wiki_artifacts",
        "backend.app.frontend_static",
        "backend.app.image_scrub",
        "backend.app.main",
        "backend.app.transcripts",
        "backend.app.vaultops",
        "PIL.Image",
        "PIL.ImageOps",
        "PIL.JpegImagePlugin",
        "PIL.PngImagePlugin",
        "PIL.WebPImagePlugin",
        "wiki_cli.graph_lint",
        "uvicorn.lifespan.off",
        "uvicorn.lifespan.on",
        "uvicorn.loops.asyncio",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
    ]
)

# Keep module-relative asset destinations covered by backend/tests/test_packaging_spec.py.
datas = [
    (str(FRONTEND_DIST), "frontend_dist"),
    (str(DASHBOARD_STATIC_DIR), "backend/app/dashboard_static"),
    *((str(path), "schemas") for path in SCHEMA_FILES),
    *((str(path), "templates/workgraphs") for path in WORKGRAPH_TEMPLATE_FILES),
]


a = Analysis(
    [str(ROOT / "backend" / "native_server.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
if MODE == "onedir":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="wiki-backend",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name=ONEDIR_NAME,
    )
    write_onedir_launcher()
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="wiki-backend",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
