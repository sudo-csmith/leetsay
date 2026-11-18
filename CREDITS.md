# Credits

## Speech model

Speech model: Parakeet TDT 0.6B (v2 for English, v3 for 25 European languages) by NVIDIA Corporation, under the CC-BY-4.0 licence, converted to ONNX and reduced to 8 bits by this project (github.com/sudo-csmith/leetsay-models). Not reviewed or endorsed by NVIDIA.

leetsay uses **Parakeet TDT 0.6B v2** (English) and, if you choose it,
**Parakeet TDT 0.6B v3** (25 European languages), speech recognition models
made by NVIDIA and released under the
[Creative Commons Attribution 4.0 International licence (CC-BY-4.0)](https://creativecommons.org/licenses/by/4.0/).

- Original work by NVIDIA Corporation. NVIDIA's model pages (with their model
  cards, which describe what the models can and cannot do):
  [v2](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2) and
  [v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3).
- leetsay downloads them from this project's own model page,
  [leetsay-models](https://github.com/sudo-csmith/leetsay-models), where they are converted in the open from NVIDIA's
  originals. Changes made: exported to the ONNX format with NVIDIA's own
  toolkit, and weights reduced to 8 bits (int8). Nothing was retrained. The
  exact source version, the steps and the fingerprints are published there.
- NVIDIA has not reviewed or endorsed leetsay or these conversions.
- The models are not covered by leetsay's MIT licence. Their licence,
  CC-BY-4.0, applies to them, and they are provided by their authors without
  warranty.

Please note: the websites linked here are third-party websites that we do
not run or control.

## Pause detection

For long dictations leetsay uses the **Silero VAD** voice activity model,
Copyright (c) 2020-present Silero Team, under the MIT licence
([snakers4/silero-vad](https://github.com/snakers4/silero-vad)). leetsay
downloads it, unchanged, from [leetsay-models](https://github.com/sudo-csmith/leetsay-models); it is the file from Silero VAD's
official release v6.2.3, with its licence.

## Libraries

- [Qt](https://www.qt.io) and [Qt for Python](https://www.qt.io/qt-for-python)
  (PySide6 and shiboken6), by The Qt Company and contributors, under the
  GNU Lesser General Public License v3 (LGPL-3.0): windows, the pop-up and
  the tray icon. leetsay uses only the modules QtCore, QtGui and QtWidgets,
  unchanged. The installer includes Qt and PySide6 6.12.0 (the exact version is
  also in `licenses\README.txt` in the installed program). Source code of that
  version: [Qt 6.12.0](https://download.qt.io/archive/qt/6.12/6.12.0/) and
  [Qt for Python v6.12.0](https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.12.0). The
  installed program is a folder of files, not one packed file, so the Qt
  libraries in it can be replaced with other versions. Qt itself contains
  code from other projects (FreeType, HarfBuzz, libpng, libjpeg-turbo, PCRE2,
  zlib and others) under their own licences; they are listed in
  `licenses\Qt-third-party.txt` in the installed program and on the Qt
  Project's [licences page](https://doc.qt.io/qt-6/licenses-used-in-qt.html).
- [onnx-asr](https://github.com/istupakov/onnx-asr) (MIT): runs the speech models.
- [ONNX Runtime](https://github.com/microsoft/onnxruntime) (MIT, with the
  third-party notices it ships): the speech engine. It uses
  [protobuf](https://github.com/protocolbuffers/protobuf) (BSD-3-Clause),
  [flatbuffers](https://github.com/google/flatbuffers) (Apache-2.0) and
  [packaging](https://github.com/pypa/packaging) (Apache-2.0 or BSD-2-Clause).
- [sounddevice](https://github.com/spatialaudio/python-sounddevice) (MIT),
  with [PortAudio](https://www.portaudio.com) (MIT) and
  [cffi](https://github.com/python-cffi/cffi) /
  [pycparser](https://github.com/eliben/pycparser) (both BSD-3-Clause): the
  microphone. Only PortAudio's standard Windows build is included.
- [NumPy](https://numpy.org) (BSD-3-Clause, with the licences of its bundled
  parts): audio handling.
- [keyboard](https://github.com/boppreh/keyboard) (MIT): the global hotkey.
- [Python](https://www.python.org) (PSF License), in the installed program.

Each of these is the work of its own authors and is used under its own
licence; the MIT licence of leetsay does not cover them. The installed
program includes the full text of every one of these licences, in its
`licenses` folder.

## Trademarks and affiliation

leetsay is an independent project. It is not affiliated with, endorsed or
sponsored by NVIDIA, GitHub, Microsoft, the Qt Company, the Silero Team or
the Python Software Foundation. All product names and trademarks belong to their
respective owners and are used only to describe what leetsay works with.

## leetsay itself

The sounds are generated tones and the icon is drawn in code: four bars with
heights 1, 3, 3 and 7.
