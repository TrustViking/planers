# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec планера (ТЗ §9, этап 5). Лежит в корне репо, собирается скриптами build_local.bat / build_release.bat
из единственного окружения .venv_planers (в нём же pyinstaller==6.19.0).

Режим — одна папка (COLLECT): onefile распаковывается во временный каталог при каждом запуске.
Точка входа — app/main.py. Консольное приложение: сводку печатает в консоль, запускается из planer.bat.
Номер версии — только из app/version.py. Иконка exe — planers.ico из корня репо (как icon= в broadcaster.spec).

В datas нет ни конфигов, ни app/examples: config\\planer.json кладёт рядом с exe build_release.bat.
Frozen-корень планера — папка exe (app/paths.py::resolve_root): config\\, secrets\\, bcast\\, keystreams\\,
logs\\ и app\\state\\ создаются рядом с planer.exe, а не в _internal\\.

googleapiclient: build(..., cache_discovery=False) берёт встроенный документ описания API
из googleapiclient/discovery_cache/documents/. Из ~600 документов (~100 МБ) в сборку идёт только
youtube.v3.json — остальные вычищаются из a.datas, если их подтянул hook.
"""
import sys
from pathlib import Path

import googleapiclient

ROOT = Path(SPECPATH).resolve()                 # SPECPATH — корень репо, где лежит planer.spec
sys.path.insert(0, str(ROOT))
from app.version import APP_VERSION  # noqa: E402  (печатается в лог сборки, чтобы версия была видна)

print(f"[planer.spec] APP_VERSION={APP_VERSION}")

DISCOVERY_DOCUMENTS = "googleapiclient/discovery_cache/documents"
YOUTUBE_DOCUMENT = "youtube.v3.json"
APP_ICON = ROOT / "planers.ico"
youtube_document = Path(googleapiclient.__file__).parent / "discovery_cache" / "documents" / YOUTUBE_DOCUMENT


def _is_other_discovery_document(dest: str) -> bool:
    normalized = dest.replace("\\", "/")
    return normalized.startswith(DISCOVERY_DOCUMENTS + "/") and not normalized.endswith("/" + YOUTUBE_DOCUMENT)


a = Analysis(
    [str(ROOT / "app" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(youtube_document), DISCOVERY_DOCUMENTS)],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "_pytest",
        "pluggy",
        "iniconfig",
        "pygments",
        "app.tests",
        "app.tools",
        "tkinter",
    ],
    noarchive=False,
)
a.datas = [entry for entry in a.datas if not _is_other_discovery_document(entry[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="planer",
    icon=str(APP_ICON),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
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
    upx=False,
    upx_exclude=[],
    name="planer",
)
