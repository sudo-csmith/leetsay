# Credits

## Speech model

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

- [onnx-asr](https://github.com/istupakov/onnx-asr) (MIT): runs the speech models.
- [ONNX Runtime](https://github.com/microsoft/onnxruntime) (MIT): the speech engine.
- [Qt for Python / PySide6](https://www.qt.io/qt-for-python) (LGPL-3.0): windows and the tray icon.
- [sounddevice](https://github.com/spatialaudio/python-sounddevice) (MIT) and PortAudio (MIT): the microphone.
- [NumPy](https://numpy.org) (BSD-3-Clause): audio handling.
- [keyboard](https://github.com/boppreh/keyboard) (MIT): the global hotkey.

Each of these is the work of its own authors and is used under its own
licence; the MIT licence of leetsay does not cover them.

## Trademarks and affiliation

leetsay is an independent project. It is not affiliated with, endorsed or
sponsored by NVIDIA, GitHub, Microsoft, the Qt Company or the Python
Software Foundation. All product names and trademarks belong to their
respective owners and are used only to describe what leetsay works with.

## leetsay itself

The sounds are generated tones and the icon is drawn in code: four bars with
heights 1, 3, 3 and 7.
