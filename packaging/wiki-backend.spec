from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path.cwd()

hiddenimports = (
    collect_submodules("fastapi")
    + collect_submodules("starlette")
    + collect_submodules("uvicorn")
    + [
        "backend.app.frontend_static",
        "backend.app.main",
        "backend.app.transcripts",
        "backend.app.vaultops",
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

datas = [
    (str(ROOT / "frontend" / "dist"), "frontend_dist"),
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
