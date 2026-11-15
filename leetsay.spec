# PyInstaller build file for leetsay.exe.
# Build with:  pyinstaller --noconfirm leetsay.spec
# Result: dist\leetsay\leetsay.exe plus its libraries (a folder, not a single
# file, so the Qt libraries stay replaceable as their LGPL licence asks).
import importlib.metadata
import os
import shutil
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

APP = "leetsay"

# onnx_asr ships small model helpers (such as the audio preprocessor) as data
# files, and loads some of its parts by name. Some libraries also read their
# own version information at run time, so that goes in too.
datas = collect_data_files("onnx_asr")
datas += copy_metadata("onnx-asr", recursive=True)
for _dist in ("onnxruntime", "numpy", "packaging", "sounddevice"):
    try:
        datas += copy_metadata(_dist)
    except Exception:
        pass
hiddenimports = collect_submodules("onnx_asr")

a = Analysis(
    ["leetsay.pyw"],
    datas=datas,
    hiddenimports=hiddenimports,
    # Not needed (the speech models are downloaded by leetsay itself).
    excludes=["tkinter", "unittest", "pydoc_data", "PySide6.QtNetwork",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtWebEngineCore",
              "PySide6.QtMultimedia", "PySide6.Qt3DCore", "PySide6.QtCharts",
              "PySide6.QtDataVisualization", "PySide6.QtPdf"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP,
    icon="leetsay.ico",
    console=False,            # a windowed program: no console window
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=APP, upx=False)

# Licence texts of everything bundled, as their licences require when shipped.
out = os.path.join(DISTPATH, APP, "licenses")
os.makedirs(out, exist_ok=True)
shutil.copy("LICENSE", os.path.join(out, "leetsay-LICENSE.txt"))
shutil.copy("CREDITS.md", os.path.join(out, "CREDITS.md"))
python_licence = os.path.join(sys.base_prefix, "LICENSE.txt")
if os.path.exists(python_licence):
    shutil.copy(python_licence, os.path.join(out, "Python-LICENSE.txt"))
for dist in ("PySide6", "PySide6_Essentials", "shiboken6", "onnxruntime", "onnx-asr",
             "numpy", "sounddevice", "keyboard", "cffi", "pycparser", "packaging",
             "protobuf", "flatbuffers"):
    try:
        files = importlib.metadata.files(dist) or []
    except importlib.metadata.PackageNotFoundError:
        continue
    for f in files:
        name = os.path.basename(str(f)).upper()
        if name.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")):
            source = f.locate()
            if os.path.isfile(source):
                shutil.copy(source, os.path.join(out, "%s-%s" % (dist, os.path.basename(str(f)))))
with open(os.path.join(out, "README.txt"), "w", encoding="utf-8") as fh:
    fh.write(
        "leetsay bundles the libraries listed in CREDITS.md. Their licence texts\n"
        "are in this folder. Qt and Qt for Python (PySide6) are used under the\n"
        "GNU Lesser General Public License v3; their source code is available\n"
        "from https://www.qt.io and https://code.qt.io (third-party websites that\n"
        "we do not run or control). The Qt libraries are separate files in the\n"
        "program folder and can be replaced.\n")
