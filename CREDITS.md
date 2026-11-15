# Credits

## Speech model

leetsay uses **Parakeet TDT 0.6B v2** (English) and, if you choose it,
**Parakeet TDT 0.6B v3** (25 European languages), speech recognition models
made by NVIDIA and released under the
[Creative Commons Attribution 4.0 International licence (CC-BY-4.0)](https://creativecommons.org/licenses/by/4.0/).

- Original work by NVIDIA Corporation:
  [nvidia/parakeet-tdt-0.6b-v2](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2) and
  [nvidia/parakeet-tdt-0.6b-v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3).
  Their model cards describe what the models can and cannot do.
- leetsay downloads ONNX conversions published as
  `istupakov/parakeet-tdt-0.6b-v2-onnx` and `istupakov/parakeet-tdt-0.6b-v3-onnx`.
  Changes made to NVIDIA's originals (by that publisher): exported to the ONNX
  format, and weights reduced to 8 bits (int8). leetsay uses them as published
  and makes no changes of its own.
- NVIDIA has not reviewed or endorsed leetsay or these conversions.
- The models are not covered by leetsay's MIT licence. Their licence,
  CC-BY-4.0, applies to them, and they are provided by their authors without
  warranty.

Please note: Hugging Face is a third-party website that we do not run or
control.

## Pause detection

For long dictations leetsay uses the **Silero VAD** voice activity model,
Copyright (c) 2020-present Silero Team, under the MIT licence
([snakers4/silero-vad](https://github.com/snakers4/silero-vad)). The file
leetsay downloads (published as `istupakov/silero-vad-onnx`) is identical to
the one in Silero VAD's official releases v6.2 to v6.2.3.

## Libraries

- [onnx-asr](https://github.com/istupakov/onnx-asr) (MIT): runs the speech models.
- [ONNX Runtime](https://github.com/microsoft/onnxruntime) (MIT): the speech engine.
- [Qt for Python / PySide6](https://www.qt.io/qt-for-python) (LGPL-3.0): windows and the tray icon.
- [sounddevice](https://github.com/spatialaudio/python-sounddevice) (MIT) and PortAudio (MIT): the microphone.
- [NumPy](https://numpy.org) (BSD-3-Clause): audio handling.
- [keyboard](https://github.com/boppreh/keyboard) (MIT): the global hotkey.
- [huggingface_hub](https://github.com/huggingface/huggingface_hub) (Apache-2.0): the one-time model download.

Each of these is the work of its own authors and is used under its own
licence; the MIT licence of leetsay does not cover them.

## Trademarks and affiliation

leetsay is an independent project. It is not affiliated with, endorsed or
sponsored by NVIDIA, Hugging Face, Microsoft, the Qt Company or the Python
Software Foundation. All product names and trademarks belong to their
respective owners and are used only to describe what leetsay works with.

## leetsay itself

The sounds are generated tones and the icon is drawn in code: four bars with
heights 1, 3, 3 and 7.
