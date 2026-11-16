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
# Leave out what leetsay never uses: PortAudio's ASIO builds (made with
# Steinberg's ASIO SDK, which has its own licence) and the builds for other
# processors and systems. leetsay uses Windows' standard audio only.
def _wanted(entry):
    name = str(entry[0]).replace("\\", "/").lower()
    if "portaudio-binaries" in name:
        return name.endswith("libportaudio64bit.dll") or name.endswith("readme.md")
    # Qt's on-screen virtual keyboard (GPL-only; for touch screens): pulled in
    # with Qt's input plugins, never used by leetsay. Its library and plugin.
    if "virtualkeyboard" in name:
        return False
    return True


a.binaries = [entry for entry in a.binaries if _wanted(entry)]
a.datas = [entry for entry in a.datas if _wanted(entry)]

# Qt modules that are only available under the GPL (or a commercial licence)
# must never be bundled: they would put stricter terms on the whole app. The
# build stops if one slips in. (leetsay itself uses QtCore, QtGui, QtWidgets.)
_GPL_ONLY = ("qt6charts", "qt6datavisualization", "qt6graphs", "qt6virtualkeyboard",
             "qt6quick3d", "qt63d", "qt6networkauth", "qt6lottie", "qt6shadertools",
             "qt6quicktimeline", "qt6quickvectorimage", "qt6httpserver", "qt6protobuf",
             "qt6grpc", "qt6coap", "qt6mqtt", "qt6opcua")
_found = sorted({os.path.basename(str(entry[0])) for entry in a.binaries + a.datas
                 if os.path.basename(str(entry[0])).lower().startswith(_GPL_ONLY)})
if _found:
    raise SystemExit("GPL-only Qt modules would be bundled: %s" % ", ".join(_found))

# Name, version and publisher written into leetsay.exe (Properties > Details),
# read from VERSION in leetsay.pyw. Code signing requires them (the product
# name must be the project's, the version the same in every file).
import re
from PyInstaller.utils.win32.versioninfo import (
    VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct,
    VarFileInfo, VarStruct)
with open("leetsay.pyw", encoding="utf-8") as _fh:
    _VERSION = re.search(r'^VERSION = "([0-9.]+)"', _fh.read(), re.M).group(1)
_v4 = tuple((list(map(int, _VERSION.split("."))) + [0, 0, 0, 0])[:4])
_version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_v4, prodvers=_v4, mask=0x3F, flags=0x0, OS=0x40004,
                      fileType=0x1, subtype=0x0, date=(0, 0)),
    kids=[StringFileInfo([StringTable("040904B0", [
              StringStruct("CompanyName", "sudo-csmith"),
              StringStruct("FileDescription", "leetsay - local dictation"),
              StringStruct("FileVersion", _VERSION),
              StringStruct("InternalName", APP),
              StringStruct("LegalCopyright", "Copyright (c) 2026 sudo-csmith. MIT licence."),
              StringStruct("OriginalFilename", APP + ".exe"),
              StringStruct("ProductName", APP),
              StringStruct("ProductVersion", _VERSION)])]),
          VarFileInfo([VarStruct("Translation", [0x0409, 1200])])])

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
    version=_version_info,
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
        path = str(f).replace("\\", "/")
        name = os.path.basename(path).upper()
        if name.endswith((".PY", ".PYC", ".DLL", ".PYD")):
            continue
        # PySide6's packages carry only Qt's commercial licence text, which
        # does not apply here (leetsay uses Qt under the LGPL, see README.txt):
        # left out so nobody takes it for the terms that apply.
        if "COMMERCIAL" in name:
            continue
        if ("/licenses/" in path.lower() or "LICENSE" in name or "LICENCE" in name
                or name.startswith(("COPYING", "NOTICE", "AUTHORS", "THIRDPARTY"))):
            source = f.locate()
            if os.path.isfile(source):
                flat = path.split(".dist-info/")[-1].replace("/", "-")
                shutil.copy(source, os.path.join(out, "%s-%s" % (dist, flat)))
# Licence texts that the libraries do not carry themselves: Qt and Qt for
# Python (LGPL v3, which builds on the GPL v3, so both), flatbuffers (Apache
# 2.0) and PortAudio (inside sounddevice).
for name in os.listdir("licenses"):
    shutil.copy(os.path.join("licenses", name), os.path.join(out, name))
import PySide6
from PySide6 import QtCore as _QtCore
_pyside = PySide6.__version__                    # the exact versions bundled
_qt = _QtCore.qVersion()
_qt_minor = ".".join(_qt.split(".")[:2])
with open(os.path.join(out, "README.txt"), "w", encoding="utf-8") as fh:
    fh.write(
        "leetsay bundles the libraries listed in CREDITS.md. Their licence texts\n"
        "are in this folder.\n\n"
        "Qt %s and Qt for Python (PySide6 and shiboken6 %s) are used under the\n"
        "GNU Lesser General Public License v3 (LGPL-3.0.txt, which refers to\n"
        "GPL-3.0.txt). leetsay uses only the Qt modules QtCore, QtGui and\n"
        "QtWidgets, unchanged. The source code of exactly these versions is\n"
        "available from https://download.qt.io/archive/qt/%s/%s/ (Qt) and\n"
        "https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v%s\n"
        "(Qt for Python), third-party websites that we do not run or control.\n"
        "The program is a folder of files, not one packed file, so the Qt\n"
        "libraries in it can be replaced with other versions.\n"
        % (_qt, _pyside, _qt_minor, _qt, _pyside))
