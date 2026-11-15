"""
leetsay.pyw - local dictation for Windows, using NVIDIA's Parakeet model.

No statistics, no history, no account. Just:
  1. Hold the hotkey   -> a small pop-up appears at the bottom of the screen
                          with a live waveform and a timer.
  2. Let go            -> the speech is turned into text on this PC and typed
                          into whatever program you were in.
  3. Hover the pop-up and click it to cancel. Drag it to move it.

Setup (once), with Python 3.13:
    py -3.13 -m pip install -r requirements.txt

Run: double-click leetsay.pyw (the .pyw ending means "no console window"), or
install it with the Windows installer (see README.md).

The first start asks before downloading the speech model (several hundred MB)
from this project's model releases on github.com. While it runs, the program
blocks its own network access; it opens it only for a download the user has
just agreed to.

Click the tray icon for Settings; right-click it to quit. Settings and the log
(dictate.json, dictate.log) are kept in %LOCALAPPDATA%\\leetsay.
"""

import os
import sys

# Security: never load code from the folder this script sits in. Python looks
# there first, so a stray file such as "json.py" or "keyboard.py" next to it
# (easily the case in a Downloads folder) would otherwise be run inside this
# program, which sees every key pressed. Only installed libraries are used.
FROZEN = bool(getattr(sys, "frozen", False))     # installed: running as leetsay.exe
_SELF = sys.executable if FROZEN else os.path.abspath(__file__)   # what to start again
if not FROZEN:   # (the installed program carries its own libraries, nothing else)
    _SCRIPT_DIR = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))
    sys.path[:] = [entry for entry in sys.path
                   if entry and os.path.normcase(os.path.abspath(entry)) != _SCRIPT_DIR]

APP_REPO = "sudo-csmith/leetsay"                 # where new versions are published

if sys.argv[1:2] == ["--check-app-update"] and len(sys.argv) >= 3:
    # Not the application: a short-lived helper, started only after the user
    # agreed to "This application will go online" (or switched on the update
    # check at startup, which asked the same). It reads the version number of
    # the latest published release on github.com, writes it to the file it was
    # given, and exits. Nothing is downloaded or installed.
    import json
    import urllib.error
    import urllib.request
    try:
        _request = urllib.request.Request(
            "https://api.github.com/repos/%s/releases/latest" % APP_REPO,
            headers={"User-Agent": "app-update-check", "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(_request, timeout=20) as _reply:
                _data = json.load(_reply)
            _installer = ""
            for _asset in _data.get("assets") or []:
                _name = str(_asset.get("name") or "")
                if _name.lower().startswith("leetsay-setup-") and _name.lower().endswith(".exe"):
                    _installer = str(_asset.get("browser_download_url") or "")
            _result = {"ok": True, "tag": _data.get("tag_name") or "",
                       "url": _data.get("html_url") or "", "installer": _installer,
                       "notes": str(_data.get("body") or "")[:4000]}
        except urllib.error.HTTPError as _exc:
            if _exc.code != 404:
                raise
            _result = {"ok": True, "tag": "", "url": ""}      # nothing published yet
    except Exception as _exc:
        _result = {"ok": False, "error": str(_exc)}
    with open(sys.argv[2], "w", encoding="utf-8") as _fh:
        json.dump(_result, _fh)
    os._exit(0)

import ctypes  # noqa: E402  (after the path is made safe)
import json
import math
import os
import re
import signal
import struct
import sys
import threading
import time
import wave
from collections import deque
from ctypes import wintypes



VERSION = "0.2.0"              # shown in Settings and About
APP_TITLE = "leetsay"                # the name shown to the user (one place)
_HERE = os.path.dirname(_SELF)        # the program's own folder


def is_former_folder(path):
    """True only for the data folder of this application under its former name:
    "Dictate" is a common word, and another program's folder of that name must
    never be taken over or deleted."""
    if not os.path.isdir(path):
        return False
    try:
        names = set(os.listdir(path))
    except Exception:
        return False
    return bool(names & {"dictate.json", "dictate.ready", "dictate.pid"})


def _data_folder():
    """Where settings, the log and other working files are kept:
    %LOCALAPPDATA%\\leetsay. Falls back to the script's own folder."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        folder = os.path.join(base, APP_TITLE)
        # The application used to be called Dictate: carry its folder over once
        # (settings, log and any speech model in it), so nothing is lost.
        former = os.path.join(base, "Dictate")
        if is_former_folder(former) and not os.path.exists(folder):
            try:
                os.rename(former, folder)
            except Exception:
                # A copy still running under the old name keeps its log open, so
                # the folder cannot be moved: copy it instead and leave the old one
                # (finish_folder_move tidies it up later). Copied under a
                # temporary name first, so a copy that fails half-way (a full
                # disk) never leaves a half-filled folder in use.
                import shutil
                partial = folder + ".moving"
                try:
                    shutil.rmtree(partial, ignore_errors=True)
                    shutil.copytree(former, partial)
                    os.rename(partial, folder)
                except Exception:
                    shutil.rmtree(partial, ignore_errors=True)
        try:
            os.makedirs(folder, exist_ok=True)
            return folder
        except Exception:
            pass
    return _HERE


_DATA = _data_folder()

# Earlier versions kept these next to the script: bring them along once.
if _DATA != _HERE:
    for _name in ("dictate.json", "dictate.ready"):
        _old, _new = os.path.join(_HERE, _name), os.path.join(_DATA, _name)
        try:
            if os.path.exists(_old):
                if os.path.exists(_new):
                    os.remove(_old)
                else:
                    import shutil
                    shutil.move(_old, _new)
        except Exception:
            pass
    for _name in ("dictate.log", "dictate.pos"):          # no longer used there
        try:
            os.remove(os.path.join(_HERE, _name))
        except Exception:
            pass
_T0 = time.perf_counter()

# When running without a console window there is nowhere to print, so
# messages go to dictate.log in the data folder instead.
_NO_CONSOLE = sys.stderr is None or sys.stdout is None
if _NO_CONSOLE:
    try:
        # Appended to at first: a start that only opens Settings in the running
        # copy must not wipe that copy's log. The old part is dropped once this
        # copy really takes over (see start_fresh_log).
        _log = open(os.path.join(_DATA, "dictate.log"), "a", buffering=1,
                    encoding="utf-8", errors="replace")
    except Exception:
        _log = open(os.devnull, "w")


class _PrivateLog:
    """Writes to the log with the user's own folder (C:\\Users\\<name>) shown as
    %USERPROFILE%, so a log pasted into a bug report never shows the Windows
    user name."""

    def __init__(self, stream):
        import re
        self._stream = stream
        home = os.path.expanduser("~")
        forms = {home, home.replace("\\", "\\\\"), home.replace("\\", "/")}
        self._home = re.compile("|".join(re.escape(f) for f in sorted(forms, key=len,
                                                                       reverse=True)),
                                re.IGNORECASE) if len(home) > 3 else None

    def write(self, text):
        if self._home is not None and isinstance(text, str):
            text = self._home.sub("%USERPROFILE%", text)
        return self._stream.write(text)

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


if _NO_CONSOLE:
    sys.stdout = sys.stderr = _PrivateLog(_log)
_LOG_START = _log.tell() if _NO_CONSOLE else 0


def start_fresh_log():
    """This copy is now the running one: keep only its own lines in the log."""
    if not _NO_CONSOLE:
        return
    try:
        _log.flush()
        path = os.path.join(_DATA, "dictate.log")
        with open(path, encoding="utf-8", errors="replace") as fh:
            fh.seek(_LOG_START)
            mine = fh.read()
        _log.seek(0)
        _log.truncate()
        _log.write(mine)
        _log.flush()
    except Exception:
        pass


def _message_box(text, error=True):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE,
                                         0x10 if error else 0x40)
    except Exception:
        pass


def _pause_and_exit(message):
    """Show an error where it can be read: a dialog, or the open console."""
    print("\n" + message, file=sys.stderr)
    if _NO_CONSOLE:
        _message_box(message)
    else:
        try:
            input("\nPress Enter to close...")
        except Exception:
            pass
    sys.exit(1)


try:
    import keyboard
    import numpy as np
    import sounddevice as sd
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt
except ImportError as _exc:
    # Double-clicking may start a Python version that doesn't have the packages
    # (e.g. 3.15). Look for an installed version that does, and restart with it.
    if os.name == "nt" and os.environ.get("DICTATE_RELAUNCHED") != "1":
        import shutil
        import subprocess
        _HIDDEN = 0x08000000                      # CREATE_NO_WINDOW
        if shutil.which("py"):
            _env = dict(os.environ, DICTATE_RELAUNCHED="1")
            _runner = "pyw" if shutil.which("pyw") else "py"
            for _ver in ("-3.13", "-3.12", "-3.14", "-3.11", "-3.10"):
                try:
                    _probe = subprocess.run(
                        ["py", _ver, "-c",
                         "import importlib.util as u, sys; sys.exit(0 if all("
                         "u.find_spec(m) for m in ('keyboard', 'numpy', "
                         "'sounddevice', 'PySide6', 'onnx_asr')) else 1)"],
                        capture_output=True, timeout=30, creationflags=_HIDDEN)
                except Exception:
                    continue
                if _probe.returncode == 0:
                    subprocess.Popen(
                        [_runner, _ver, os.path.abspath(__file__)] + sys.argv[1:],
                        env=_env, creationflags=_HIDDEN)
                    sys.exit(0)
    _pause_and_exit(
        "A required package is missing: %s\n\n"
        "Open a terminal and run this once, then start this program again:\n\n"
        '    "%s" -m pip install PySide6 sounddevice numpy keyboard "onnx-asr[cpu,hub]"'
        % (_exc, sys.executable))

# ----------------------------------------------------------------------------
# Settings - change these to taste
# ----------------------------------------------------------------------------
HOTKEY = "ctrl+space"               # dictation hotkey
HOLD_TO_TALK = True                  # True: hold to record, let go to finish
                                     # False: press once to start, again to finish
MODEL = "nemo-parakeet-tdt-0.6b-v2"  # English only. Use ...-v3 for multilingual
QUANTIZATION = "int8"                # smaller + faster on CPU. None = full size
SOUNDS = True                        # soft tone on start and on finish
SOUND_SET = "soft"                   # soft, bright, click, low, or custom
SOUND_START_FILE = ""                # your own .wav files, used when the set is
SOUND_FINISH_FILE = ""               # "custom"
SOUND_VOLUME = 0.25                  # 0.0 - 1.0
# Offline always: see the network guard. Going online needs a yes each time.
HIDE_CONSOLE = True                  # run in the background, no command prompt
SHOW_TRANSCRIBING = False            # False: pop-up vanishes as soon as you finish
STARTUP_MESSAGE = False              # show a "Loading..." pop-up while starting
STARTUP_FLASH = True                 # briefly show the pop-up once it can be used
ALWAYS_SHOW = True                   # always show a small status pill on screen (on by
                                     # default: it shows the hotkey; owner)
ACCENT_OUTLINE = True                # a ring in the accent colour round the pop-up
CARD_STYLE = "glow"                  # the dictation card: "clean" (a soft neutral
                                     # shadow and a crisp edge), "accent" (the same,
                                     # with a fine edge in the accent colour while
                                     # dictating) or "glow" (a coloured glow round it)
CARD_STYLES = [("glow", "Glow"), ("clean", "Clean"),
               ("accent", "Clean with a colour edge")]
OUTLINE_LIGHT = True                 # a band of light that travels round that ring
GLOW = "medium"                      # glow round the pop-up: off, subtle, medium, strong
GLOW_FROM_APP = True                 # tint the glow with the colour of the app's icon
GLOW_COLOUR = ""                     # your own glow colour ("" = the accent colour);
                                     # used when the glow doesn't follow the app's icon
SCREEN_GLOW = True                   # also glow round the edge of the whole screen
SCREEN_GLOW_STRENGTH = 60            # how bright that glow is, 5-100
SCREEN_GLOW_SIZE = 56                # how far it reaches in from the edge, 20-200 pixels
SCREEN_GLOW_FLOW = True              # a light moves along it, and it ripples with your voice
SCREEN_GLOW_EDGES = "bottom"         # where: one side ("bottom", "top", "left", "right")
                                     # or "all" (a border round the whole screen)
SCREEN_GLOW_FOCUS = "middle"         # one side's glow: rising in the "middle", gathered in
                                     # the "corners", or "even" along the whole edge
SCREEN_GLOW_SCREENS = "popup"        # on which screens: "popup" (the pop-up's), "all", "main"
TRAY_LIVE = False                    # the tray icon's bars move with your voice
SHOW_HOTKEY_HINT = True              # show the hotkey on that pill and at startup
PILL_MARK = "logo"                   # the pill's mark: "logo" (leetsay's), "dot" or "none"
PILL_MARKS = ("logo", "dot", "none")
LIVE_LINES = 1                       # lines of live words in the pop-up (1-6)
LIVE_ALIGN = "left"                  # rows of live words: "left" (words never move
                                     # once shown) or "center" (rows re-centre)
CARD_WIDTH = 0                       # your own pop-up width, set by dragging its left
                                     # or right edge (0 = the usual Narrow / Wide)
WIDE_CARD = False                    # a wider pop-up when several lines are shown
LIVE_PREVIEW = True                  # show the words in the pop-up as you speak
LIVE_TYPE = False                    # type the words into the app at each pause, as you
                                     # speak (a little less accurate than all at once)
LIVE_EVERY_MS = 250                  # the preview refreshes this often, or as fast as
                                     # the PC can manage if a pass takes longer
TEXT_SIZE = 12                       # size of the live words in the pop-up
# How long the speech model rests between readings while you speak, as a share
# of the last reading's time: on PCs with up to 8 cores it rests as long as it
# worked (reading along then uses at most about half the processor time it
# would), on bigger PCs only briefly, so words appear as soon as possible.
LIVE_REST = 1.0 if (os.cpu_count() or 4) <= 8 else 0.25
LIVE_WINDOW_SECONDS = 10             # recent audio the preview re-reads, per line shown
RETURN_FOCUS = True                  # paste into the window you started in, even if
                                     # another program grabbed the focus meanwhile
KEEP_LAST = False                    # "Missed text safety": a quick tap of the hotkey
KEEP_LAST_SECONDS = 300              # pastes the last dictation again; it is held in
                                     # memory this long. Off unless switched on.
TAP_SECONDS = 0.35                   # a press shorter than this counts as a tap
TYPE_TEXT = True                     # True: type the text, never touch the clipboard
                                     # False: paste, then put your clipboard back
MAX_RECORD_SECONDS = 300             # a recording is always stopped after this long
MIC_DEVICE = ""                      # microphone by name; "" = Windows' default one
SOUND_DEVICE = ""                    # where the sounds play; "" = Windows' default output
ADD_SPACE = True                     # put a space after each dictation
REMOVE_FILLERS = True                # leave out "um", "uh" and the like
# Word replacements: what the speech model writes -> what you want instead, for
# names and words it doesn't know (it can't learn new words). Whole words only.
REPLACEMENTS = [["leet say", "leetsay"], ["leaked say", "leetsay"],
                ["lit say", "leetsay"], ["leet-say", "leetsay"]]
REPLACE_MAX, REPLACE_LEN = 50, 60            # at most 50 rules of 60 characters
# Offered by "Add tech terms" in the Word replacements window (not on by
# default: someone writing prose may want "gigabytes" spelled out). Only clear
# cases: nothing that is also an ordinary word or name ("Jason" for JSON).
TECH_TERMS = [["kilobytes", "KB"], ["kilobyte", "KB"], ["megabytes", "MB"],
              ["megabyte", "MB"], ["gigabytes", "GB"], ["gigabyte", "GB"],
              ["terabytes", "TB"], ["terabyte", "TB"], ["kilohertz", "kHz"],
              ["megahertz", "MHz"], ["gigahertz", "GHz"],
              ["java script", "JavaScript"], ["type script", "TypeScript"],
              ["git hub", "GitHub"], ["c plus plus", "C++"], ["c sharp", "C#"],
              ["node js", "Node.js"], ["vs code", "VS Code"]]
TEXTBOX_ONLY = True                  # typing as you speak: only into a text box (asks
                                     # Windows what kind of control has the focus)
POINTER_HINT = True                  # ... and say "No text box" by the mouse pointer too
AFTER_KEY = ""                       # key pressed for you once the text is in:
                                     # "" (nothing), "enter", "ctrl+enter" or "tab"
PASTE_HOTKEY = ""                    # optional extra hotkey for "missed text safety"
CLIPBOARD_RESTORE_MS = 1000          # how long the paste gets before the restore
                                     # (a busy program may read the clipboard late,
                                     # and must get the dictation, not what was there)
SHOW_APP_ICON = True                 # show the icon of the app you're dictating into
SHOW_APP_NAME = False                # show its name next to the icon
SHOW_POPUP = True                    # show the pop-up while dictating (off: sounds only)
SHOW_WAVEFORM = True                 # show the wave line that moves with your voice
SHOW_TIMER = True                    # show how long you have been dictating
SHOW_TRAY_ICON = True                # tray icon is the only way to quit pythonw
BOTTOM_MARGIN = 36                   # gap between the pop-up and the taskbar (its
                                     # glow stays clear of the taskbar's icons)
EDGE_GAP = 8                         # the pop-up never comes closer to a screen edge

SAMPLE_RATE = 16000
MIC_SILENT_PEAK = 0.0008             # below this the microphone is giving no signal
MIC_QUIET_PEAK = 0.01                # below this it can barely hear anything
N_DOTS = 46                          # how much level history is kept
ACCENT = "#3DDC97"                   # pop-up colour (any hex colour)
POPUP_BG = "#16171B"                 # pop-up background; text adapts to light or dark
MENU_THEME = "dark"                  # Settings and the other windows: "dark" or "light"
LONG_AUDIO_SECONDS = 30              # above this, split on pauses before transcribing

# ----------------------------------------------------------------------------
# Choices made in Settings are remembered in dictate.json (in the data folder)
# ----------------------------------------------------------------------------
_CONFIG_FILE = os.path.join(_DATA, "dictate.json")


def _load_config():
    try:
        with open(_CONFIG_FILE, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
        raise ValueError("not a set of settings")
    except FileNotFoundError:
        return {}
    except Exception as exc:
        # Damaged (a power cut mid-save in an older version, or edited by hand):
        # keep it aside instead of quietly overwriting it, and start afresh.
        print("The settings file could not be read (%s); it was kept as "
              "dictate.json.damaged and the settings start afresh." % exc, file=sys.stderr)
        try:
            os.replace(_CONFIG_FILE, _CONFIG_FILE + ".damaged")
        except Exception:
            pass
        return {}


_config = _load_config()


_config_lock = threading.RLock()          # the model thread saves too
_config_frozen = [False]                  # set when a restart hands over: no more saves


def save_setting(key, value):
    with _config_lock:
        if value is None:
            _config.pop(key, None)
        else:
            _config[key] = value
        write_config()


def write_config():
    """Save all settings. Written to a temporary file first, then swapped in
    whole: a power cut or crash mid-save leaves the previous settings intact,
    never half a file. One save at a time, from a snapshot."""
    temporary = _CONFIG_FILE + ".saving"
    if _config_frozen[0]:
        return
    with _config_lock:
        try:
            snapshot = json.loads(json.dumps(_config))
            with open(temporary, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, _CONFIG_FILE)
        except Exception as exc:
            print("Could not save the settings:", exc, file=sys.stderr)


if isinstance(_config.get("accent"), str):
    ACCENT = _config["accent"]
if isinstance(_config.get("sounds"), bool):
    SOUNDS = _config["sounds"]
if isinstance(_config.get("hold_to_talk"), bool):
    HOLD_TO_TALK = _config["hold_to_talk"]

# Speech models that can be picked in Settings: (name, label, quantization, size).
MODELS = [
    ("nemo-parakeet-tdt-0.6b-v2", "English (Parakeet v2)", "int8", "about 660 MB"),
    ("nemo-parakeet-tdt-0.6b-v3", "25 European languages (Parakeet v3)", "int8", "about 670 MB"),
]
if _config.get("model") in [entry[0] for entry in MODELS]:
    MODEL = _config["model"]
    QUANTIZATION = dict((entry[0], entry[2]) for entry in MODELS)[MODEL]

# Where the speech models come from: this project's own model releases on
# github.com, converted in the open from NVIDIA's originals (see CREDITS.md).
MODELS_REPO = "sudo-csmith/leetsay-models"
MODELS_PAGE = "https://github.com/%s/releases" % MODELS_REPO
# Every file of every model, with its exact size and SHA-256 fingerprint. A file
# is used only if it matches, wherever it came from: a changed, damaged or
# swapped file is refused. A newer model comes only with a newer version of the
# application, which carries the new fingerprints. Each model lists its current
# release first; older releases that are still trusted may follow, and are used
# only while the current one cannot be downloaded. A release found to be bad is
# simply dropped from this list in the next version, and is refused from then on.
MODEL_RELEASES = {
    "nemo-parakeet-tdt-0.6b-v2": [{
        "tag": "parakeet-tdt-0.6b-v2-int8-r1",
        "files": {
            "config.json": (98, "21565538716b1cb134a096e3df9fa3f27f0470bc09c596d9faa578dc27d945d5"),
            "vocab.txt": (9384, "ec182b70dd42113aff6c5372c75cac58c952443eb22322f57bbd7f53977d497d"),
            "encoder-model.int8.onnx":
                (652282027, "e6698cc5b6ad84289b568b47e43b8e8d056d47dfdbb7de6f71d98eb073b1bcf1"),
            "decoder_joint-model.int8.onnx":
                (8998286, "a449f49acd68979d418651dd2dcb737cc0f1bf0225e009e29ee326354edbf7d3"),
        }}],
    "nemo-parakeet-tdt-0.6b-v3": [{
        "tag": "parakeet-tdt-0.6b-v3-int8-r1",
        "files": {
            "config.json": (98, "21565538716b1cb134a096e3df9fa3f27f0470bc09c596d9faa578dc27d945d5"),
            "vocab.txt": (93939, "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d"),
            "encoder-model.int8.onnx":
                (652282012, "89e59ef6598d7edce584759b8648b5daaf518dc221e948c1d4db2270554cafcb"),
            "decoder_joint-model.int8.onnx":
                (18202004, "eea7483ee3d1a30375daedc8ed83e3960c91b098812127a0d99d1c8977667a70"),
        }}],
}
# The small helper that finds the pauses in long dictations (Silero VAD, MIT),
# the makers' own unchanged file.
HELPER = "silero-vad"
HELPER_RELEASE = {
    "tag": "silero-vad-v6.2.3",
    "files": {
        "config.json": (31, "5d8aaa4970ba7f3b089d11ee1e7e8033363c52a16ce7afb2595f0c941a280996"),
        "silero_vad.onnx": (2327524, "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"),
    }}
MODEL_FILES = ["config.json", "vocab.txt",
               "encoder-model.int8.onnx", "decoder_joint-model.int8.onnx"]


def current_release(name=None):
    """The release of a model this version of the application uses."""
    releases = MODEL_RELEASES.get(name or MODEL) or [None]
    return releases[0]


def release_page(release):
    return "https://github.com/%s/releases/tag/%s" % (MODELS_REPO, release["tag"])


def model_dir(name=None):
    """The folder a model's files are kept in, downloaded or put there by hand:
    %LOCALAPPDATA%\\leetsay\\models\\<model>."""
    return os.path.join(_DATA, "models", name or MODEL)


def helper_dir():
    return os.path.join(_DATA, "models", HELPER)


def local_model_dir(name=None):
    """The model's folder if all its files are there ('' if not). Whether they
    are the right files is checked separately, before anything is loaded."""
    folder = model_dir(name)
    if all(os.path.isfile(os.path.join(folder, item)) for item in MODEL_FILES):
        return folder
    return ""


def helper_on_disk():
    return all(os.path.isfile(os.path.join(helper_dir(), item))
               for item in HELPER_RELEASE["files"])


def engine_options():
    """Settings for the speech engine. Measured on a 16-core PC with an 18 s
    recording: without the engine's memory pool the app uses about 250 MB less
    and is at most 0.05 s slower; more than 4 threads made no difference."""
    import onnxruntime
    options = onnxruntime.SessionOptions()
    options.enable_cpu_mem_arena = False
    options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 4))
    options.inter_op_num_threads = 1
    return options


def model_label(name=None):
    name = MODEL if name is None else name
    return dict((entry[0], entry[1]) for entry in MODELS).get(name, name)


_MODIFIER_NAMES = ("ctrl", "shift", "alt", "windows")


def valid_hotkey(value, empty_ok=False):
    """A hotkey that can be pressed and never takes over an ordinary key.
    Every key before the last must be Ctrl, Shift, Alt or Windows. The last is
    an ordinary key (then at least one modifier comes first, unless it is an
    F-key), or a modifier too, when the whole hotkey is modifiers (at least
    two of them, e.g. Ctrl+Shift). So "space", "e" or "a+space" never pass:
    they would swallow that key everywhere and switch the microphone on."""
    if not isinstance(value, str):
        return False
    if value == "":
        return empty_ok
    if len(value) > 40 or value != value.strip().lower():
        return False
    parts = value.split("+")
    if any(not part for part in parts) or len(set(parts)) != len(parts):
        return False
    *mods, last = parts
    if any(m not in _MODIFIER_NAMES for m in mods):
        return False
    if last in _MODIFIER_NAMES:
        if len(parts) < 2:
            return False
    elif not mods and last not in ["f%d" % n for n in range(1, 25)]:
        return False
    try:
        keyboard.parse_hotkey(value)
    except Exception:
        return False
    return True


if valid_hotkey(_config.get("hotkey")):
    HOTKEY = _config["hotkey"]

HOTKEY_CHOICES = ["ctrl+space", "ctrl+shift+space", "ctrl+alt+space"]


def set_hotkey(hotkey):
    global HOTKEY
    HOTKEY = hotkey
    save_setting("hotkey", hotkey)


if _config.get("layout") != 2:
    # The pop-up was redesigned (icon, wave and timer on one row). Settings saved
    # for the old design start from the new default once: the app's name is off.
    # It can be switched back on in Settings.
    _config.pop("show_app_name", None)
    _config.pop("show_app", None)
    save_setting("layout", 2)
if _config.get("show_app") is False:         # older single switch, when it was off
    SHOW_APP_ICON = SHOW_APP_NAME = False
for _name, _key in (("SHOW_APP_ICON", "show_app_icon"), ("SHOW_APP_NAME", "show_app_name"),
                    ("SHOW_WAVEFORM", "show_waveform"), ("SHOW_TIMER", "show_timer"),
                    ("SHOW_POPUP", "show_popup"), ("SCREEN_GLOW", "screen_glow"),
                    ("TRAY_LIVE", "tray_live"),
                    ("ALWAYS_SHOW", "always_show"), ("ACCENT_OUTLINE", "accent_outline"),
                    ("WIDE_CARD", "wide_card"),
                    ("OUTLINE_LIGHT", "outline_light"), ("GLOW_FROM_APP", "glow_from_app"),
                    ("SHOW_HOTKEY_HINT", "hotkey_hint"), ("KEEP_LAST", "keep_last"),
                    ("LIVE_TYPE", "live_type"),
                    ("REMOVE_FILLERS", "remove_fillers"), ("TEXTBOX_ONLY", "textbox_only"),
                    ("POINTER_HINT", "pointer_hint"),
                    ("SCREEN_GLOW_FLOW", "screen_glow_flow"),
                    ("RETURN_FOCUS", "return_focus")):
    if isinstance(_config.get(_key), bool):
        globals()[_name] = _config[_key]
if isinstance(_config.get("volume"), (int, float)) and not isinstance(_config["volume"], bool):
    SOUND_VOLUME = max(0.0, min(1.0, float(_config["volume"])))
for _name, _key, _low, _high in (("SCREEN_GLOW_STRENGTH", "screen_glow_strength", 5, 100),
                                 ("SCREEN_GLOW_SIZE", "screen_glow_size", 20, 200)):
    _value = _config.get(_key)
    if isinstance(_value, (int, float)) and not isinstance(_value, bool):
        globals()[_name] = int(max(_low, min(_high, _value)))
def plain_text(part):
    """Ordinary text only: no control or invisible formatting characters
    (typed into an app, some of them act as keys: one of them pastes)."""
    import unicodedata
    return all(unicodedata.category(c)[0] != "C" for c in part)


def valid_replacements(value):
    """A list of [heard, write] pairs of short, non-empty, one-line, plain
    texts."""
    return (isinstance(value, list) and len(value) <= REPLACE_MAX and all(
        isinstance(pair, list) and len(pair) == 2 and all(
            isinstance(part, str) and part.strip() and len(part) <= REPLACE_LEN
            and plain_text(part) for part in pair)
        for pair in value))


if valid_replacements(_config.get("replacements")):
    REPLACEMENTS = _config["replacements"]
if _config.get("pill_mark") in PILL_MARKS:
    PILL_MARK = _config["pill_mark"]
elif isinstance(_config.get("pill_logo"), bool):   # saved by an earlier 0.2.0 build
    PILL_MARK = "logo" if _config["pill_logo"] else "dot"
if _config.get("screen_glow_focus") in ("middle", "corners", "even"):
    SCREEN_GLOW_FOCUS = _config["screen_glow_focus"]
if _config.get("screen_glow_screens") in ("popup", "all", "main"):
    SCREEN_GLOW_SCREENS = _config["screen_glow_screens"]
if _config.get("screen_glow_edges") in ("bottom", "top", "left", "right", "all"):
    SCREEN_GLOW_EDGES = _config["screen_glow_edges"]
if _config.get("card_style") in [key for key, _label in CARD_STYLES]:
    CARD_STYLE = _config["card_style"]
if isinstance(_config.get("card_width"), int) and not isinstance(_config["card_width"], bool):
    CARD_WIDTH = max(0, _config["card_width"])
TEXT_SIZES = [(11, "Small"), (12, "Medium"), (14, "Large"), (16, "Extra large")]
if _config.get("text_size") in [size for size, _label in TEXT_SIZES]:
    TEXT_SIZE = _config["text_size"]
if isinstance(_config.get("live_lines"), int) and not isinstance(_config["live_lines"], bool):
    LIVE_LINES = max(1, min(6, _config["live_lines"]))


# (Set before the saved values are read below; setting them after would throw
# the saved ones away, as an earlier version did.)
ACTION_HOTKEY = ""                   # a second hotkey: dictate, then press ACTION_KEY
ACTION_KEY = "enter"                 # what that second hotkey presses afterwards
for _name, _key in (("SOUND_SET", "sound_set"), ("SOUND_START_FILE", "sound_start"),
                    ("SOUND_FINISH_FILE", "sound_finish"), ("POPUP_BG", "popup_bg"),
                    ("MIC_DEVICE", "mic"), ("SOUND_DEVICE", "sound_device"),
                    ("GLOW_COLOUR", "glow_colour"), ("ACTION_KEY", "action_key")):
    if isinstance(_config.get(_key), str):
        globals()[_name] = _config[_key]
def windows_app_mode():
    """Windows' own "app mode" (Settings > Personalization > Colours): "light"
    or "dark". Reads that one setting, on this PC."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return "light" if winreg.QueryValueEx(key, "AppsUseLightTheme")[0] else "dark"
    except Exception:
        return "dark"


if _config.get("menu_theme") in ("dark", "light"):
    MENU_THEME = _config["menu_theme"]
else:
    # Not chosen yet (a first start): follow Windows. Not saved, so it keeps
    # following Windows until a choice is made in the setup or in Settings.
    MENU_THEME = windows_app_mode()
if not isinstance(_config.get("popup_bg"), str) and windows_app_mode() == "light":
    POPUP_BG = "#F8F8FA"                 # the pop-up too (BACKGROUND_CHOICES' Light)
for _name, _key in (("PASTE_HOTKEY", "paste_hotkey"), ("ACTION_HOTKEY", "action_hotkey")):
    if valid_hotkey(_config.get(_key), empty_ok=True) and _config[_key] != HOTKEY:
        globals()[_name] = _config[_key]


if isinstance(_config.get("add_space"), bool):
    ADD_SPACE = _config["add_space"]
if isinstance(_config.get("max_seconds"), int) and not isinstance(_config["max_seconds"], bool):
    MAX_RECORD_SECONDS = max(30, min(3600, _config["max_seconds"]))


GLOW_LEVELS = [("off", "Off", 0.0), ("subtle", "Subtle", 0.45), ("medium", "Medium", 0.75),
               ("strong", "Strong", 1.1)]
if _config.get("glow") in [level[0] for level in GLOW_LEVELS]:
    GLOW = _config["glow"]


def glow_amount():
    return dict((key, amount) for key, _label, amount in GLOW_LEVELS).get(GLOW, 0.75)


AFTER_DELAY = 0                      # seconds to wait before that key is pressed
AFTER_DELAYS = [(0, "right away"), (1, "after 1 second"), (2, "after 2 seconds"),
                (3, "after 3 seconds"), (5, "after 5 seconds")]
if _config.get("after_delay") in [seconds for seconds, _label in AFTER_DELAYS]:
    AFTER_DELAY = _config["after_delay"]
AFTER_CHOICES = [("", "do nothing"), ("enter", "press Enter"),
                 ("ctrl+enter", "press Ctrl+Enter"), ("tab", "press Tab")]
if _config.get("after_key") in [choice[0] for choice in AFTER_CHOICES]:
    AFTER_KEY = _config["after_key"]


def set_option(name, key, value):
    """Change one of the settings above and remember it."""
    globals()[name] = value
    save_setting(key, value)


if isinstance(_config.get("type_text"), bool):
    TYPE_TEXT = _config["type_text"]
if isinstance(_config.get("live_preview"), bool):
    LIVE_PREVIEW = _config["live_preview"]


def set_live_preview(on):
    global LIVE_PREVIEW
    LIVE_PREVIEW = bool(on)
    save_setting("live_preview", LIVE_PREVIEW)


def set_type_text(on):
    global TYPE_TEXT
    TYPE_TEXT = bool(on)
    save_setting("type_text", TYPE_TEXT)


# Off by default: when on, each start asks github.com (in a separate helper, so
# this program's own network block stays closed) whether a newer version of
# the application has been published. Nothing is downloaded by it.
CHECK_APP_UPDATES = _config.get("check_app_updates") is True
APP_RELEASES_PAGE = "https://github.com/%s/releases/latest" % APP_REPO


def set_check_app_updates(on):
    global CHECK_APP_UPDATES
    CHECK_APP_UPDATES = bool(on)
    save_setting("check_app_updates", CHECK_APP_UPDATES)


def version_tuple(text):
    """"v0.58.4" -> (0, 58, 4); anything unreadable -> ()."""
    parts = str(text or "").strip().lstrip("vV").split(".")
    try:
        return tuple(int(p) for p in parts) if parts and parts[0] else ()
    except ValueError:
        return ()


def fetch_latest_release():
    """Ask github.com for the latest published version, in a separate helper
    process (see --check-app-update at the top). Returns (tag, page, installer,
    None), with "" for what is not there, or (None, None, None, why it failed).
    Links are only ever accepted if they point at this repository."""
    import subprocess
    import tempfile
    handle, out_path = tempfile.mkstemp(suffix=".json")
    os.close(handle)
    try:
        helper = ([sys.executable, "--check-app-update", out_path] if FROZEN
                  else [sys.executable, "-I", _SELF, "--check-app-update", out_path])
        subprocess.run(helper, timeout=40, creationflags=0x08000000 if os.name == "nt" else 0)
        with open(out_path, encoding="utf-8") as fh:
            result = json.load(fh)
    except Exception as exc:
        return None, None, None, str(exc)
    finally:
        try:
            os.remove(out_path)
        except Exception:
            pass
    if not result.get("ok"):
        return None, None, None, result.get("error") or "github.com could not be reached"
    page = str(result.get("url") or "")
    if not page.startswith("https://github.com/%s/" % APP_REPO):
        page = APP_RELEASES_PAGE                 # only ever this repository's page
    installer = str(result.get("installer") or "")
    if not installer.startswith("https://github.com/%s/releases/download/" % APP_REPO):
        installer = ""                           # not this repository's: not offered
    # A release can say that older versions should no longer be used, with a
    # line "Minimum safe version: x.y.z" in its notes.
    import re
    found = re.search(r"minimum safe version:\s*v?([0-9]+(?:\.[0-9]+)*)",
                      str(result.get("notes") or ""), re.IGNORECASE)
    _app_update_work["minimum"] = found.group(1) if found else ""
    return str(result.get("tag") or ""), page, installer, None


ACCENT_CHOICES = [("Mint", "#3DDC97"), ("Sky", "#4DA3FF"), ("Violet", "#9B8CFF"),
                  ("Amber", "#FFB547"), ("Rose", "#FF6B8B"), ("White", "#F2F2F5")]


def set_accent(colour):
    global ACCENT
    ACCENT = colour
    save_setting("accent", colour)


def set_sounds(on):
    global SOUNDS
    SOUNDS = bool(on)
    save_setting("sounds", SOUNDS)


def set_hold_to_talk(on):
    global HOLD_TO_TALK
    HOLD_TO_TALK = bool(on)
    save_setting("hold_to_talk", HOLD_TO_TALK)


# ----------------------------------------------------------------------------
# Start with Windows (a per-user entry in the registry's Run list)
# ----------------------------------------------------------------------------
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = APP_TITLE
_OLD_RUN_NAME = "DictatePy"            # the entry made under the former name


def _startup_command():
    if FROZEN:
        return '"%s"' % sys.executable
    exe = sys.executable or "pythonw.exe"
    folder, name = os.path.split(exe)
    windowless = os.path.join(folder, "pythonw.exe")
    if name.lower() == "python.exe" and os.path.exists(windowless):
        exe = windowless
    return '"%s" "%s"' % (exe, os.path.abspath(__file__))


def startup_command_saved():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            return winreg.QueryValueEx(key, _RUN_NAME)[0]
    except Exception:
        return None


def starts_with_windows():
    """True if the "Start with Windows" entry starts this very copy."""
    return startup_command_saved() == _startup_command()


def carry_over_startup():
    """Start with Windows was switched on under the former name: remove that old
    entry (it starts a program that is no longer there). Nothing is written in
    its place: the user ticks "Start with Windows" again if they want it, since
    a program must never add itself to start-up on its own."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            try:
                winreg.QueryValueEx(key, _OLD_RUN_NAME)
            except FileNotFoundError:
                return False
            winreg.DeleteValue(key, _OLD_RUN_NAME)
            print("Removed the start-up entry made under the former name.")
            return True
    except Exception as exc:
        print("Could not carry over the startup setting:", exc, file=sys.stderr)
        return False


def set_startup(on):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, _RUN_NAME, 0, winreg.REG_SZ, _startup_command())
            else:
                try:
                    winreg.DeleteValue(key, _RUN_NAME)
                except FileNotFoundError:
                    pass
    except Exception as exc:
        print("Could not change the startup setting:", exc, file=sys.stderr)


# ----------------------------------------------------------------------------
# Model files: checked against their built-in fingerprints, downloaded safely
# ----------------------------------------------------------------------------
def file_fingerprint(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_files(folder, files, key):
    """Compare the files in `folder` with {name: (size, SHA-256)}. Returns the
    names that are missing or do not match. A file whose size and time are as
    recorded the last time it matched is not read again, so the check costs
    nothing on an ordinary start; any change means it is read in full."""
    saved = _config.get("model_files")
    known = saved.get(key, {}) if isinstance(saved, dict) else {}
    bad, record = [], {}
    for name, (size, sha) in sorted(files.items()):
        path = os.path.join(folder, name)
        try:
            info = os.stat(path)
            if info.st_size != size:
                bad.append(name)
                continue
            old = known.get(name)
            same = (isinstance(old, list) and len(old) == 3 and old[0] == size
                    and abs(float(old[1]) - info.st_mtime) < 2.0 and old[2] == sha)
            if not same and file_fingerprint(path) != sha:
                bad.append(name)
                continue
            record[name] = [size, info.st_mtime, sha]
        except OSError:
            bad.append(name)
        except Exception as exc:
            print("Could not check %s: %s" % (name, exc), file=sys.stderr)
            bad.append(name)
    if record != known:
        saved = dict(saved) if isinstance(saved, dict) else {}
        saved[key] = record
        save_setting("model_files", saved)
    return bad


def matching_release(name=None):
    """The trusted release that the model's files on this PC are exactly (the
    current one is tried first), or None."""
    name = name or MODEL
    if not local_model_dir(name):
        return None
    for release in MODEL_RELEASES.get(name) or []:
        if not check_files(model_dir(name), release["files"], "%s|%s" % (name, release["tag"])):
            return release
    return None


def model_mismatches(name=None):
    """The model's files that are missing or differ from the current release."""
    name = name or MODEL
    release = current_release(name)
    if release is None:
        return list(MODEL_FILES)
    return check_files(model_dir(name), release["files"], "%s|%s" % (name, release["tag"]))


def helper_ok():
    return helper_on_disk() and not check_files(
        helper_dir(), HELPER_RELEASE["files"], "%s|%s" % (HELPER, HELPER_RELEASE["tag"]))


def model_files():
    """{file name: full path} of the model's files that are on this PC."""
    folder = model_dir()
    return {name: os.path.join(folder, name) for name in MODEL_FILES
            if os.path.isfile(os.path.join(folder, name))}


def _download_file(url, path, size, sha):
    """Fetch one file, carrying on from a part that an earlier, interrupted try
    left behind. Kept only if it is exactly the right size and fingerprint;
    otherwise it is deleted and the attempt fails."""
    import urllib.request
    part = path + ".part"
    have = os.path.getsize(part) if os.path.isfile(part) else 0
    if have > size:
        os.remove(part)
        have = 0
    if have < size:
        request = urllib.request.Request(url, headers={"User-Agent": APP_TITLE})
        if have:
            request.add_header("Range", "bytes=%d-" % have)
        with urllib.request.urlopen(request, timeout=30) as reply:
            if have and getattr(reply, "status", 200) != 206:
                have = 0                         # sent from the start again
            with open(part, "ab" if have else "wb") as out:
                while True:
                    block = reply.read(1024 * 1024)
                    if not block:
                        break
                    out.write(block)
    got = os.path.getsize(part)
    if got < size:
        # The connection ended early: keep what arrived, the next try carries on.
        raise ConnectionError("%s stopped at %d of %d bytes"
                              % (os.path.basename(path), got, size))
    if got != size or file_fingerprint(part) != sha:
        os.remove(part)
        raise IOError("%s did not match its fingerprint, so it was deleted"
                      % os.path.basename(path))
    os.replace(part, path)


def download_release(release, folder):
    """Fetch every file of a release into a separate folder beside `folder`,
    check each one, and only then put the new folder in place of the old one.
    If the connection drops or a file does not match, what is already in
    `folder` is untouched (and still used), and the files that did arrive
    complete are kept, so the next try carries on from there."""
    import shutil
    staging = folder + ".download"
    os.makedirs(staging, exist_ok=True)
    for name, (size, sha) in sorted(release["files"].items(), key=lambda item: item[1][0]):
        target = os.path.join(staging, name)
        if os.path.isfile(target) and os.path.getsize(target) == size \
                and file_fingerprint(target) == sha:
            continue                             # arrived complete in an earlier try
        _download_file("https://github.com/%s/releases/download/%s/%s"
                       % (MODELS_REPO, release["tag"], name), target, size, sha)
    old = folder + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(old):
        old = "%s.old%d" % (folder, int(time.time()))
    if os.path.isdir(folder):
        os.replace(folder, old)
    os.replace(staging, folder)
    shutil.rmtree(old, ignore_errors=True)
    print("Downloaded and checked %s (%d files)." % (release["tag"], len(release["files"])))


def _release_bytes(release):
    return sum(size for size, _sha in release["files"].values())


def model_total_bytes():
    """How much this download fetches: the model, plus the helper if missing."""
    release = current_release()
    total = _release_bytes(release) if release else 0
    if not helper_on_disk():
        total += _release_bytes(HELPER_RELEASE)
    return total


def model_bytes_on_disk():
    """How much of this download has arrived so far (partly arrived files too)."""
    got = 0
    for folder, release in ((model_dir(), current_release()), (helper_dir(), HELPER_RELEASE)):
        if release is None:
            continue
        if os.path.isdir(folder + ".download"):
            got += folder_bytes(folder + ".download")
        elif folder == helper_dir() and helper_on_disk():
            got += _release_bytes(release)
        elif folder == model_dir() and local_model_dir():
            got += _release_bytes(release)
    return got


def folder_bytes(folder):
    """The size of everything in a folder."""
    total = 0
    for where, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(where, name))
            except Exception:
                pass
    return total


def size_words(count):
    """A byte count the way people say it: 104 KB, 12.4 MB, 660 MB."""
    if count < 1024 * 1024:
        return "%d KB" % (count // 1024)
    if count < 100 * 1024 * 1024:
        return "%.1f MB" % (count / (1024.0 * 1024.0))
    return "%d MB" % (count // (1024 * 1024))


def folder_megabytes(folder):
    return int(folder_bytes(folder) / (1024 * 1024))


def _models_root():
    return os.path.normcase(os.path.abspath(os.path.join(_DATA, "models")))


def finish_pending_removal():
    """Delete model files that could not be removed while the program was
    running. Only folders inside the application's own models folder are ever
    touched."""
    pending = _config.get("delete_at_start")
    if not pending:
        return
    save_setting("delete_at_start", "")
    import shutil
    for item in (pending if isinstance(pending, list) else [pending]):
        try:
            wanted = os.path.normcase(os.path.abspath(str(item)))
            if not wanted.startswith(_models_root() + os.sep):
                continue
            if os.path.isdir(wanted):
                shutil.rmtree(wanted, ignore_errors=True)
            elif os.path.isfile(wanted):
                os.remove(wanted)
        except Exception as exc:
            print("Could not finish removing the model:", exc, file=sys.stderr)


def model_folders(name=None):
    """The folders on this PC that belong to this model: its own folder and any
    download in progress or left behind."""
    base = model_dir(name)
    return [folder for folder in (base, base + ".download", base + ".old")
            if os.path.isdir(folder)]


def forget_model():
    """Delete this model from this PC, with every note about it, so the next
    start offers to download it afresh (after asking). The helper goes too,
    unless another model is still here that uses it. Returns what was done."""
    import shutil
    report = {"removed": [], "left": [], "freed": 0}
    folders = model_folders()
    others = [entry[0] for entry in MODELS if entry[0] != MODEL and local_model_dir(entry[0])]
    if not others:
        folders += [folder for folder in (helper_dir(), helper_dir() + ".download")
                    if os.path.isdir(folder)]
    for folder in folders:
        size = folder_megabytes(folder)
        shutil.rmtree(folder, ignore_errors=True)
        if os.path.isdir(folder):
            report["left"].append(folder)        # Windows still holds a file open
            report["freed"] += max(0, size - folder_megabytes(folder))
        else:
            report["removed"].append(folder)
            report["freed"] += size
    if report["left"]:
        # Finished at the next start, before anything is loaded.
        save_setting("delete_at_start", list(report["left"]))
    saved = _config.get("model_files")
    if isinstance(saved, dict):
        save_setting("model_files", {key: value for key, value in saved.items()
                                     if not key.startswith(MODEL + "|")})
    if _config.get("download_yes_once"):
        save_setting("download_yes_once", "")
    print("Model removed: %s" % report)
    return report


def model_location():
    """Return (folder, size in MB) of the model on this PC, or (None, 0)."""
    folder = model_dir()
    if not os.path.isdir(folder) or not model_files():
        return None, 0
    return folder, folder_megabytes(folder)


def is_downloaded(name):
    return bool(local_model_dir(name))


def _setup_done():
    return bool(local_model_dir())


# The network guard. It is installed when the program starts and cannot be
# removed for the life of the process: Python reports every network action
# (looking up a name, connecting, sending) to it first, from whatever library
# or module it comes, and the guard refuses it. Only after the user has agreed,
# in a window that says "This application will go online", is it opened for
# this run, and then only towards github.com, where this project's speech
# models are published (and GitHub's own server that delivers the files). It
# closes again as soon as the download is done.
_NET_MESSAGE = APP_TITLE + ": network access is blocked (offline)"
_NET_ALLOWED_HOSTS = frozenset(("github.com", "release-assets.githubusercontent.com",
                                "objects.githubusercontent.com"))
_NET_EVENTS = frozenset(("socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                         "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto",
                         "socket.sendmsg", "socket.bind"))
_NET_DLLS = frozenset(("wininet", "winhttp", "urlmon", "ws2_32", "dnsapi", "mswsock",
                       "websocket", "iphlpapi"))
_net = {"open": False, "why": "", "verified": None, "hosts": set(), "ips": set()}


class NetworkBlocked(ConnectionRefusedError):
    """Raised for any network action while the guard is closed."""


def _proxy_hosts():
    """Proxy servers the user has set up for this PC (so a download that was
    agreed to can still go through them)."""
    from urllib.parse import urlsplit
    hosts = set()
    values = []
    try:      # the proxy set in Windows' own settings, which downloads also use
        import urllib.request
        values = list(urllib.request.getproxies().values())
    except Exception:
        pass
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        values += [os.environ.get(name), os.environ.get(name.lower())]
    for value in values:
        if not value:
            continue
        try:
            host = urlsplit(value if "://" in value else "http://" + value).hostname
            if host:
                hosts.add(host.lower())
        except Exception:
            pass
    return hosts


def _host_allowed(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host or "").lower().rstrip(".")
    if host in _NET_ALLOWED_HOSTS:
        return True
    return host in _proxy_hosts()


def _address_of(sockaddr):
    try:
        return str(sockaddr[0]).split("%")[0].lower()
    except Exception:
        return ""


def _network_guard(event, args):
    if event in _NET_EVENTS:
        if not _net["open"]:
            raise NetworkBlocked(_NET_MESSAGE)
        if event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
            # Even while open: only to an address that a lookup of an allowed
            # name returned, so nothing can connect to a number directly.
            target = args[1] if len(args) > 1 else None
            if event == "socket.sendmsg":
                target = args[-1] if args else None
            if _address_of(target) not in _net["ips"]:
                print("Network: refused a connection to %r (not an allowed address)"
                      % (_address_of(target),), file=sys.stderr)
                raise NetworkBlocked(_NET_MESSAGE)
            return
        if event in ("socket.getaddrinfo", "socket.gethostbyname"):
            host = args[0] if args else None
            if not _host_allowed(host):
                print("Network: refused a lookup of %r (only github.com is allowed)"
                      % (host,), file=sys.stderr)
                raise NetworkBlocked(_NET_MESSAGE)
            if host not in _net["hosts"]:
                _net["hosts"].add(host)
                print("Network: connecting to %s (agreed: %s)" % (host, _net["why"]))
    elif event == "ctypes.dlopen" and not _net["open"]:
        name = os.path.basename(str(args[0] if args else "")).lower()
        if name.endswith(".dll"):
            name = name[:-4]
        if name in _NET_DLLS:
            raise NetworkBlocked(_NET_MESSAGE)
    elif event == "urllib.Request" and not _net["open"]:
        raise NetworkBlocked(_NET_MESSAGE)


sys.addaudithook(_network_guard)


def _remember_addresses():
    """Note the addresses that lookups of allowed names return (only possible
    while the guard is open, since it refuses every other lookup), so that
    connections can be held to exactly those."""
    import socket
    real_getaddrinfo = socket.getaddrinfo
    real_gethostbyname = socket.gethostbyname

    def getaddrinfo(*args, **kwargs):
        found = real_getaddrinfo(*args, **kwargs)
        for entry in found:
            _net["ips"].add(_address_of(entry[4]))
        return found

    def gethostbyname(*args, **kwargs):
        address = real_gethostbyname(*args, **kwargs)
        _net["ips"].add(str(address).lower())
        return address

    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname


_remember_addresses()


def open_network(why):
    """Allow network access towards github.com for this run. Only ever called
    right after the user agreed to go online (or for a start that a "go online"
    window in Settings covered)."""
    _net["why"] = why
    _net["open"] = True
    print("Network: opened for this run (%s)." % why)


def close_network(reason="done"):
    if _net["open"]:
        _net["open"] = False
        _net["ips"].clear()
        print("Network: closed again (%s)." % reason)


def network_is_open():
    return _net["open"]


def verify_network_block():
    """Check, without anything leaving this PC, that the guard really refuses:
    a connection to this PC itself and a name lookup must both be stopped by
    the guard (a refusal from Windows itself would not count)."""
    import socket
    results = []
    for attempt in ("connect", "lookup"):
        try:
            if attempt == "connect":
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    probe.settimeout(0.2)
                    probe.connect(("127.0.0.1", 9))
                finally:
                    probe.close()
            else:
                socket.getaddrinfo("localhost", 9)
            results.append(False)                  # it went through: not blocked
        except NetworkBlocked:
            results.append(True)
        except Exception:
            results.append(False)                  # stopped, but not by the guard
    _net["verified"] = all(results)
    print("Network block check: %s" % ("verified, all network access is refused"
                                       if _net["verified"] else "FAILED"))
    return _net["verified"]


def network_words(short=False):
    """How the network stands, in words for Settings and About."""
    if short:                                    # the status line in Settings
        if _net["open"]:
            return "Open for this download only (github.com)"
        if _net["verified"] is False:
            return "Blocked, but the check at start failed (see the log)"
        return ("Blocked for %s · checked at start" % APP_TITLE if _net["verified"]
                else "Blocked for %s" % APP_TITLE)
    if _net["open"]:
        return "open for this run only, to github.com (%s)" % _net["why"]
    if _net["verified"]:
        return "blocked for this program (offline, checked at start)"
    if _net["verified"] is False:
        return "blocked, but the check at start did not pass (see dictate.log)"
    return "blocked for this program (offline)"


# Ask the libraries not to phone home, in any mode.
os.environ["DO_NOT_TRACK"] = "1"

START_WITHOUT_MODEL = False              # set at start-up when the user chooses to
                                         # come in without a speech model for now

# A download agreed to in Settings (switching to a model not on this PC yet,
# downloading it again, or another try at a stalled download) covers this one
# start. The yes is used up the moment it is read, so a start that fails later
# on cannot leave it behind for another start to use without asking.
DOWNLOAD_AGREED_ONCE = _config.get("download_yes_once") == MODEL
if _config.get("download_yes_once") or _config.get("download_ok"):
    save_setting("download_yes_once", "")
    save_setting("download_ok", "")             # an older, lasting yes: dropped unread
# Settings that earlier test versions kept and that no longer exist.
for _old_key in ("check_updates", "update_once", "own_model_files", "published_fingerprints"):
    if _old_key in _config:
        save_setting(_old_key, None)
# Offline unless this start was agreed to in a "go online" window in Settings.
# The first download asks later, in main().
if DOWNLOAD_AGREED_ONCE:
    open_network("download agreed to in Settings")


# ----------------------------------------------------------------------------
# Which app is focused? (its name and icon are shown in the capsule)
# ----------------------------------------------------------------------------
def _file_description(path):
    """The friendly name stored in an .exe, e.g. 'Windows Explorer'."""
    ver = ctypes.windll.version
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return ""
    data = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, data):
        return ""
    ptr, length = ctypes.c_void_p(), wintypes.UINT()
    ok = ver.VerQueryValueW(data, "\\VarFileInfo\\Translation",
                            ctypes.byref(ptr), ctypes.byref(length))
    if not ok or length.value < 4:
        return ""
    lang, codepage = struct.unpack("HH", ctypes.string_at(ptr.value, 4))
    key = "\\StringFileInfo\\%04x%04x\\FileDescription" % (lang, codepage)
    ok = ver.VerQueryValueW(data, key, ctypes.byref(ptr), ctypes.byref(length))
    if not ok or not length.value:
        return ""
    return ctypes.wstring_at(ptr.value, length.value).rstrip("\x00").strip()


def foreground_window():
    """Handle of the window that currently has the focus (None if unknown)."""
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        return user32.GetForegroundWindow()
    except Exception:
        return None


def _process_elevated(handle):
    """True / False if the process token can be read, None if it cannot."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):   # TOKEN_QUERY
        return None
    try:
        elevated, size = wintypes.DWORD(0), wintypes.DWORD(0)
        if not advapi32.GetTokenInformation(token, 20, ctypes.byref(elevated),  # TokenElevation
                                            ctypes.sizeof(elevated), ctypes.byref(size)):
            return None
        return bool(elevated.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(token)


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


def focus_details(hwnd):
    """(the focused control's window, does it show a text cursor?) for the
    program that owns hwnd."""
    try:
        user32 = ctypes.windll.user32
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        thread = user32.GetWindowThreadProcessId(hwnd, None)
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if thread and user32.GetGUIThreadInfo(thread, ctypes.byref(info)):
            return (int(info.hwndFocus or 0), bool(info.hwndCaret))
    except Exception:
        pass
    return (0, False)


class _VARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("value", ctypes.c_longlong),
                ("extra", ctypes.c_longlong)]


def _guid(text):
    import uuid
    return (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(text).bytes_le)


# Windows' kinds of control (UI Automation's ControlType numbers) that can
# never be typed into. Not here: edit boxes and documents (typable), combo
# boxes and spinners (sometimes), panes, groups and custom controls (could be
# anything), and spreadsheet cells (data items: typed into).
_NOT_TYPABLE = frozenset((
    50000,   # button
    50001,   # calendar
    50002,   # check box
    50005,   # link
    50006,   # image (e.g. a picture preview in VS Code)
    50007,   # list item
    50008,   # list
    50009,   # menu
    50010,   # menu bar
    50011,   # menu item
    50012,   # progress bar
    50013,   # radio button
    50014,   # scroll bar
    50015,   # slider
    50017,   # status bar
    50018,   # tab
    50019,   # tab item
    50020,   # text label
    50021,   # toolbar
    50022,   # tooltip
    50023,   # tree
    50024,   # tree item
    50027,   # thumb (a slider's handle)
    50031,   # split button
    50034,   # header
    50035,   # header item
    50037,   # title bar
    50038,   # separator
))

_focus_seen = {"last": None}       # for tests only: the kinds last seen, never text


def focused_text_box():
    """Is the keyboard focus in something you can type into? Asks Windows'
    accessibility service (what screen readers use) about the focused
    control. True: a text box. False: clearly not one (a web page, a video,
    a button: typed letters would work as shortcuts there, and Space would
    scroll). None: can't tell, so typing goes ahead as usual. Run it off the
    main thread: a busy program can be slow to answer."""
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 0)                # this thread: multithreaded COM
    automation = ctypes.c_void_p()
    element = ctypes.c_void_p()
    try:
        if ole32.CoCreateInstance(_guid("ff48dba4-60ef-4201-aa87-54103eef594e"), None,
                                  1 | 4, _guid("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee"),
                                  ctypes.byref(automation)) != 0 or not automation:
            return None

        def method(obj, index, *types):
            table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(table[index])

        # IUIAutomation::GetFocusedElement
        if method(automation, 8, ctypes.POINTER(ctypes.c_void_p))(
                automation, ctypes.byref(element)) != 0 or not element:
            return None
        get = method(element, 10, ctypes.c_int, ctypes.POINTER(_VARIANT))   # GetCurrentPropertyValue

        def prop(number):
            value = _VARIANT()
            if get(element, number, ctypes.byref(value)) != 0:
                return None
            try:
                if value.vt == 3:                # a number
                    return ctypes.c_int32(value.value & 0xFFFFFFFF).value
                if value.vt == 11:               # yes / no
                    return (value.value & 0xFFFF) != 0
                return None
            finally:
                ctypes.windll.oleaut32.VariantClear(ctypes.byref(value))
        if prop(30019):                          # IsPassword: never type into it live
            return False
        kind = prop(30003)                       # ControlType
        has_value = prop(30043)                  # IsValuePatternAvailable
        read_only = prop(30046)                  # ValueIsReadOnly
        has_text = prop(30040)                   # IsTextPatternAvailable
        _focus_seen["last"] = (kind, has_value, read_only, has_text)   # kinds only
        if kind in _NOT_TYPABLE:
            # A button, an image, a menu, a list... (see _NOT_TYPABLE): never
            # somewhere to type, whatever else it says
            return False
        if kind == 50004:                        # an edit box
            return read_only is not True
        if kind == 50030:                        # a document: Word, Notepad, or a web page
            if has_value and read_only:
                return False                     # a web page (read only)
            if has_value and read_only is False:
                return True
            return None if has_text else False
        if has_value and read_only is False:
            return True                          # something whose text can be changed
        if kind in (50026, 50033, 50025):        # group, pane, custom: e.g. a video
            return None if has_text else False
        return None
    except Exception:
        return None
    finally:
        for obj in (element, automation):
            if obj:
                try:
                    table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
                    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])(obj)   # Release
                except Exception:
                    pass
        try:
            ole32.CoUninitialize()
        except Exception:
            pass


def window_out_of_reach(hwnd):
    """True if Windows will not let this program type into that window: it
    belongs to a program running as administrator (or with higher rights) while
    this one is not. Windows drops such keystrokes silently, so the words would
    simply vanish. False when it can be typed into, or when it cannot be told."""
    if not hwnd:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        if _process_elevated(kernel32.GetCurrentProcess()):
            return False                         # we have the same rights: fine
        pid = wintypes.DWORD(0)
        ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        if not pid.value or pid.value == os.getpid():
            return False
        handle = kernel32.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFORMATION
        if not handle:
            return False                         # cannot tell: try as usual
        try:
            elevated = _process_elevated(handle)
        finally:
            kernel32.CloseHandle(handle)
        # A token we may not even read belongs to a program with higher rights.
        return elevated is None or elevated
    except Exception:
        return False


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


# Left and right Ctrl, Shift, Alt, then the two Windows keys.
_LIFT_VKS = (0xA2, 0xA3, 0xA0, 0xA1, 0xA4, 0xA5, 0x5B, 0x5C)
_EXTENDED_VKS = (0xA3, 0xA5, 0x5B, 0x5C)
_QUIET_VK = 0xE8        # a key no keyboard has: see type_while_held
_FAMILY = {0xA2: "ctrl", 0xA3: "ctrl", 0xA0: "shift", 0xA1: "shift",
           0xA4: "alt", 0xA5: "alt", 0x5B: "windows", 0x5C: "windows"}


def type_while_held(text, really_held=None):
    """Type text into the focused window while the hotkey may still be held
    down (typing as you speak). Ctrl held down turns typed letters into
    shortcuts (Ctrl+A would select everything), so for the app the held
    modifier keys are let go first and pressed again straight after, all in
    one unbroken batch that nothing else can come between. The letters go as
    Unicode characters, so the keyboard layout doesn't matter. Letting go of
    Alt or the Windows key on its own would open a menu or Start: a key that
    does nothing is pressed around them, as Windows itself advises. Returns
    True if Windows took every keystroke.

    really_held: the modifier keys (by name) that are physically down, as
    the keyboard hook saw them. Only those are pressed again afterwards, so
    a key let go a split second earlier is never left stuck down."""
    if not text:
        return True
    try:
        user32 = ctypes.windll.user32
        down = [vk for vk in _LIFT_VKS if user32.GetAsyncKeyState(vk) & 0x8000]
        held = [vk for vk in down if really_held is None or _FAMILY[vk] in really_held]
        quiet = any(vk in (0xA4, 0xA5, 0x5B, 0x5C) for vk in held)
        events = []

        def key(vk=0, scan=0, up=False, unicode=False):
            flags = (0x0002 if up else 0) | (0x0004 if unicode else 0)
            if vk in _EXTENDED_VKS:
                flags |= 0x0001
            events.append(_INPUT(1, _INPUTUNION(ki=_KEYBDINPUT(vk, scan, flags, 0, 0))))

        if quiet or any(vk in (0xA4, 0xA5, 0x5B, 0x5C) for vk in down):
            key(_QUIET_VK); key(_QUIET_VK, up=True)
        for vk in down:                          # everything down is let go...
            key(vk, up=True)
        units = text.encode("utf-16-le")
        for i in range(0, len(units), 2):
            unit = units[i] | (units[i + 1] << 8)
            key(scan=unit, unicode=True); key(scan=unit, up=True, unicode=True)
        for vk in held:                          # ...but only real ones pressed again
            key(vk)
        if held:
            # Again a key that does nothing, so that letting go of Ctrl next
            # time doesn't count as a lone tap of it (which some programs, and
            # Windows' "show the pointer" option, react to).
            key(_QUIET_VK); key(_QUIET_VK, up=True)
        batch = (_INPUT * len(events))(*events)
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        return user32.SendInput(len(events), batch, ctypes.sizeof(_INPUT)) == len(events)
    except Exception:
        return False


def release_stuck(really_held):
    """Let go of any modifier key Windows still thinks is down although it
    isn't physically held (the end of typing as you speak: a key let go at
    the very moment it was pressed again would otherwise stay down)."""
    try:
        user32 = ctypes.windll.user32
        stuck = [vk for vk in _LIFT_VKS if user32.GetAsyncKeyState(vk) & 0x8000
                 and _FAMILY[vk] not in really_held]
        if not stuck:
            return
        events = []
        for vk, up in [(_QUIET_VK, False), (_QUIET_VK, True)] + [(vk, True) for vk in stuck]:
            flags = (0x0002 if up else 0) | (0x0001 if vk in _EXTENDED_VKS else 0)
            events.append(_INPUT(1, _INPUTUNION(ki=_KEYBDINPUT(vk, 0, flags, 0, 0))))
        batch = (_INPUT * len(events))(*events)
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
        user32.SendInput(len(events), batch, ctypes.sizeof(_INPUT))
        print("Let go of %d modifier key(s) left down after typing." % len(stuck))
    except Exception:
        pass


def find_pause(audio, rate):
    """Where a piece of a dictation can be cut off and typed (typing as you
    speak): in the middle of the last short pause after some speech, as a
    sample index into audio, or None if there is no such pause yet. Talking
    on and on without a pause: after about 8 seconds it is cut at the
    quietest moment of the last few seconds instead."""
    frame = rate // 50                           # 20 ms
    count = len(audio) // frame
    if count < 30:
        return None
    loud = np.sqrt(np.mean(np.square(
        audio[:count * frame].astype(np.float32).reshape(count, frame)), axis=1))
    floor, top = float(np.percentile(loud, 3)), float(np.percentile(loud, 95))
    # Judged against this microphone's own background level, never a fixed
    # loudness: microphones differ a lot (some are very quiet).
    if top < 0.0002 or top < floor * 3.0:
        return None                              # nothing that stands out as speech
    quiet = loud < floor + 0.15 * (top - floor)
    gap = 22                                     # a pause of about 0.45 s
    speech, run, cut = 0, 0, None
    for i, still in enumerate(quiet):
        if still:
            run += 1
            if run == gap and speech >= 15:     # 0.3 s of speech before it
                cut = (i - gap + 1 + gap // 2) * frame
        else:
            run = 0
            speech += 1
    if cut is None and count > 8 * 50:           # no pause at all: cut anyway
        lo = count - 200
        smooth = np.convolve(loud, np.ones(10) / 10.0, mode="same")
        cut = (lo + int(np.argmin(smooth[lo:count - 10]))) * frame
    return cut


FILLERS = {"um", "umm", "ummm", "uh", "uhh", "uhm", "erm"}
_SENTENCE_END = (".", "!", "?", "\u2026")


def drop_fillers(words):
    """Leave out "um", "uh" and the like (a list of words in, a list out),
    tidying up around them: "Um, it seems" -> "It seems", "so, um, this" ->
    "so, this", "right now um." -> "right now.". Only these sounds: words
    such as "like" or "you know" are often meant, so they stay."""
    if not REMOVE_FILLERS:
        return list(words)
    out, capital = [], False
    for word in words:
        core = word.strip(".,!?;:\u2026").lower()
        if core in FILLERS:
            marks = word[len(word.rstrip(".,!?;:\u2026")):]
            starts = not out or out[-1].endswith(_SENTENCE_END)
            if starts and word[:1].isupper():
                capital = True                   # the next word starts the sentence
            if out and marks and marks[0] in "!?.\u2026" \
                    and not out[-1].endswith(_SENTENCE_END):
                out[-1] = out[-1].rstrip(",;:") + marks[0]    # "now um." -> "now."
                capital = False
            continue
        if capital and word[:1].islower():
            word = word[0].upper() + word[1:]
        capital = False
        out.append(word)
    return out


def replace_words(words):
    """Your word replacements, applied to a list of words: a phrase the
    speech model wrote ("leaked say") becomes what you want ("leetsay").
    Matched as whole words, ignoring capitals and punctuation; the
    punctuation after the last matched word is kept."""
    rules = []
    for heard, write in REPLACEMENTS:
        key = [w for w in ("".join(c for c in part.lower() if c.isalnum() or c == "'")
                           for part in heard.split()) if w]
        if key:
            rules.append((key, write.split()))
    if not rules:
        return list(words)
    rules.sort(key=lambda rule: -len(rule[0]))  # longer phrases first
    plain = ["".join(c for c in w.lower() if c.isalnum() or c == "'") for w in words]
    out, i = [], 0
    while i < len(words):
        for key, write in rules:
            if plain[i:i + len(key)] == key and write:
                last = words[i + len(key) - 1]
                end = last[len(last.rstrip(".,!?;:\u2026")):]
                write = list(write)
                first = words[i]
                if first[:1].isupper() and write[0][:1].islower():
                    write[0] = write[0][0].upper() + write[0][1:]   # a sentence's capital
                out.extend(write[:-1] + [write[-1] + end])
                i += len(key)
                break
        else:
            out.append(words[i])
            i += 1
    return out


_TIME_DOT = re.compile(r"^(\d{1,2})\.([0-5]\d)((?:[ap]\.?m\.?)?)([.,!?;:]*)$", re.IGNORECASE)
_AM_PM = re.compile(r"^[ap]\.?m\.?[.,!?;:]*$", re.IGNORECASE)


def fix_times(words):
    """The speech model sometimes writes a time with a dot: "3.47 PM". With
    AM or PM right after it, it is a time, so: "3:47 PM". Without, it is left
    alone (it may be a price or a number: "3.47 dollars")."""
    out = list(words)
    for i, word in enumerate(out):
        match = _TIME_DOT.match(word)
        if not match or not 1 <= int(match.group(1)) <= 12:
            continue
        if match.group(3) or (i + 1 < len(out) and _AM_PM.match(out[i + 1])):
            out[i] = "%s:%s%s%s" % match.groups()
    return out


def clean_words(words):
    """Filler words out, then times, then your word replacements."""
    return replace_words(fix_times(drop_fillers(words)))


def holds_speech(audio, whole, rate):
    """Does this leftover bit of a recording hold any speech? Measured against
    the whole recording's speaking level, so that background noise left
    after the last pause is not read as an "uh"."""
    frame = rate // 50
    def loudness(a):
        n = len(a) // frame
        if n < 1:
            return np.zeros(1, dtype=np.float32)
        return np.sqrt(np.mean(np.square(a[:n * frame].astype(np.float32).reshape(n, frame)), axis=1))
    every = loudness(whole)
    floor, top = float(np.percentile(every, 3)), float(np.percentile(every, 95))
    part = loudness(audio)
    return int(np.sum(part > floor + 0.25 * (top - floor))) >= 8     # 0.16 s of it


def microphone_present():
    """Does Windows have a microphone at all right now? (Asked of Windows
    directly: it notices one being plugged in or out at once.)"""
    try:
        return ctypes.windll.winmm.waveInGetNumDevs() > 0
    except Exception:
        return True                              # can't tell: don't cry wolf


def taskbar_see_through():
    """Is Windows' taskbar see-through ("Transparency effects" on, the
    default)? Then a glow placed behind it shows through, behind its icons.
    Reads that one Windows setting, on this PC."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "EnableTransparency")[0])
    except Exception:
        return False


def tuck_behind_taskbar(widget):
    """Put a window just below the taskbar (which stays on top of it, icons
    and all), above everything else. Windows brings the taskbar forward now
    and then, so this is done again on every frame."""
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        tray = user32.FindWindowW("Shell_TrayWnd", None)
        if tray:
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            wintypes.UINT]
            user32.SetWindowPos(int(widget.winId()), tray, 0, 0, 0, 0,
                                0x0001 | 0x0002 | 0x0010)    # keep size and place; don't activate
    except Exception:
        pass


def focus_window(hwnd):
    """Bring a window back to the front. True if it now has the focus."""
    try:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = wintypes.HWND
        for fn in (user32.IsWindow, user32.IsIconic, user32.SetForegroundWindow,
                   user32.BringWindowToTop):
            fn.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                    ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        if not hwnd or not user32.IsWindow(hwnd):
            return False                          # that window has been closed
        if user32.GetForegroundWindow() == hwnd:
            return True
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)            # SW_RESTORE: it was minimised
        user32.SetForegroundWindow(hwnd)
        if user32.GetForegroundWindow() != hwnd:
            # Windows refuses focus changes from background programs unless the
            # caller shares input with the window that currently has it.
            mine = kernel32.GetCurrentThreadId()
            theirs = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            if theirs and theirs != mine:
                user32.AttachThreadInput(mine, theirs, True)
                try:
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                finally:
                    user32.AttachThreadInput(mine, theirs, False)
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def foreground_app(hwnd=None):
    """Return (display name, exe path) of a window (the focused one by default),
    or ('', '')."""
    try:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                    ctypes.POINTER(wintypes.DWORD)]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        if hwnd is None:
            hwnd = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return "", ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return "", ""
            path = buf.value
        finally:
            kernel32.CloseHandle(handle)

        try:
            name = _file_description(path)
        except Exception:
            name = ""
        if not name:
            name = path.replace("/", "\\").rsplit("\\", 1)[-1].rsplit(".", 1)[0].title()
        return name, path
    except Exception:
        return "", ""


# ----------------------------------------------------------------------------
# Sounds (generated here, no audio files needed)
# ----------------------------------------------------------------------------
SOUND_RATE = 44100


def _tone(freq, seconds):
    """A pure tone that starts and ends at exactly zero (no click, no hiss)."""
    t = np.arange(int(SOUND_RATE * seconds)) / SOUND_RATE
    attack = 0.5 - 0.5 * np.cos(np.pi * np.clip(t / 0.012, 0.0, 1.0))
    release = 0.5 - 0.5 * np.cos(np.pi * np.clip((seconds - t) / 0.045, 0.0, 1.0))
    decay = np.exp(-t / (seconds * 0.42))
    return np.sin(2 * np.pi * freq * t) * attack * release * decay


def _mix(parts, seconds):
    """Lay tones onto silence, with a little quiet before and after."""
    lead, tail = 0.03, 0.10
    out = np.zeros(int(SOUND_RATE * (lead + seconds + tail)))
    for offset, tone in parts:
        i = int(SOUND_RATE * (lead + offset))
        out[i:i + len(tone)] += tone
    return out.astype(np.float32)


SOUND_SETS = [("soft", "Soft"), ("bright", "Bright"), ("click", "Click"),
              ("low", "Low"), ("custom", "My own files")]


def _make_sounds(which):
    """The start and finish cues for one of the built-in sets."""
    if which == "bright":       # higher, glassier pair
        start = _mix([(0.0, _tone(880.0, 0.10)), (0.07, _tone(1174.7, 0.16))], 0.23)
        finish = _mix([(0.0, _tone(1174.7, 0.10)), (0.07, _tone(880.0, 0.18))], 0.25)
    elif which == "click":      # short ticks: one to start, two to finish
        start = _mix([(0.0, _tone(1400.0, 0.05))], 0.05)
        finish = _mix([(0.0, _tone(1400.0, 0.05)), (0.09, _tone(1050.0, 0.05))], 0.14)
    elif which == "low":        # one deep note each
        start = _mix([(0.0, _tone(220.0, 0.22))], 0.22)
        finish = _mix([(0.0, _tone(164.8, 0.26))], 0.26)
    else:                       # soft: two rising notes, then one lower note
        start = _mix([(0.0, _tone(523.25, 0.12)), (0.08, _tone(659.25, 0.16))], 0.24)
        finish = _mix([(0.0, _tone(392.0, 0.24))], 0.24)
    return {"start": start, "finish": finish}


_SOUNDS = {}
_SOUND_FILES = {}


def _prepare_sound_files():
    """Write the current set's tones as small WAV files so Windows itself can
    play them. Files are only written when missing."""
    global _SOUNDS
    which = SOUND_SET if SOUND_SET in ("soft", "bright", "click", "low") else "soft"
    _SOUNDS = _make_sounds(which)
    folder = os.path.join(_DATA, "sounds")
    os.makedirs(folder, exist_ok=True)
    tag = "%s_%s_%d" % (which, VERSION, int(round(SOUND_VOLUME * 100)))
    for name, samples in _SOUNDS.items():
        path = os.path.join(folder, "%s_%s.wav" % (name, tag))
        _SOUND_FILES[name] = path
        if os.path.exists(path):
            continue                             # already made
        for old in os.listdir(folder):           # clear out other sets/volumes
            if old.startswith(name + "_") or old == name + ".wav":
                try:
                    os.remove(os.path.join(folder, old))
                except Exception:
                    pass
        pcm = (np.clip(samples * SOUND_VOLUME, -1.0, 1.0) * 32767.0).astype("<i2")
        stereo = np.repeat(pcm[:, None], 2, axis=1)
        with wave.open(path, "wb") as fh:
            fh.setnchannels(2)
            fh.setsampwidth(2)
            fh.setframerate(SOUND_RATE)
            fh.writeframes(stereo.tobytes())


try:
    _prepare_sound_files()
except Exception:
    _SOUND_FILES = {}


def sound_path(name):
    """The file to play for 'start' or 'finish': your own if set, else built-in."""
    if SOUND_SET == "custom":
        own = SOUND_START_FILE if name == "start" else SOUND_FINISH_FILE
        if own and os.path.isfile(own):
            return own
    return _SOUND_FILES.get(name)


def glow_base():
    """The glow's colour when it isn't taken from the app's icon: your own
    choice if you made one, else the accent colour."""
    colour = QtGui.QColor(GLOW_COLOUR) if GLOW_COLOUR else QtGui.QColor(ACCENT)
    return colour if colour.isValid() else QtGui.QColor(ACCENT)


def open_mic_settings():
    """Open Windows' own microphone privacy page, where desktop apps are allowed
    or blocked. (Windows does not ask per app, so this is where to look.)"""
    try:
        os.startfile("ms-settings:privacy-microphone")
    except Exception as exc:
        print("Could not open Windows' microphone settings:", exc, file=sys.stderr)


def output_devices():
    """Names of the speakers and headphones Windows offers (one entry each)."""
    names = []
    try:
        api = sd.default.hostapi
        for device in sd.query_devices():
            if device.get("max_output_channels", 0) > 0 and device.get("hostapi") == api:
                if device["name"] not in names:
                    names.append(device["name"])
    except Exception:
        pass
    return names


def chosen_output():
    """Index of the chosen output, or None for Windows' default."""
    if not SOUND_DEVICE:
        return None
    try:
        api = sd.default.hostapi
        for index, device in enumerate(sd.query_devices()):
            if (device.get("name") == SOUND_DEVICE and device.get("hostapi") == api
                    and device.get("max_output_channels", 0) > 0):
                return index
    except Exception:
        pass
    return None                                  # unplugged: fall back to the default


_WAV_CACHE = {}


def _read_wav(path):
    """A WAV file as (samples, rate), for playing on a chosen output. Read once
    and reused until the file changes."""
    key = (path, os.path.getmtime(path))
    if key not in _WAV_CACHE:
        with wave.open(path, "rb") as fh:
            width, channels, rate = fh.getsampwidth(), fh.getnchannels(), fh.getframerate()
            raw = fh.readframes(fh.getnframes())
        if width == 2:
            data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif width == 1:
            data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif width == 4:
            data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
        else:
            raise ValueError("unsupported WAV format")
        if len(_WAV_CACHE) > 6:                  # old versions of changed files
            _WAV_CACHE.clear()
        _WAV_CACHE[key] = (data.reshape(-1, channels), rate)
    return _WAV_CACHE[key]


def play_sound(name):
    if not SOUNDS:
        return
    if SOUND_DEVICE:                             # a particular output was chosen
        try:
            device = chosen_output()
            if device is not None:
                data, rate = _read_wav(sound_path(name))
                sd.play(data, rate, device=device)
                return
        except Exception as exc:
            print("Could not play on the chosen output:", exc, file=sys.stderr)
    try:
        # Played by Windows' own mixer, the same way system sounds are.
        import winsound
        winsound.PlaySound(sound_path(name), winsound.SND_FILENAME
                           | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        try:
            sd.play(_SOUNDS[name] * SOUND_VOLUME, SOUND_RATE)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Microphone
# ----------------------------------------------------------------------------
def input_devices():
    """Names of the microphones Windows offers (one entry each)."""
    names = []
    try:
        api = sd.default.hostapi
        for device in sd.query_devices():
            if device.get("max_input_channels", 0) > 0 and device.get("hostapi") == api:
                if device["name"] not in names:
                    names.append(device["name"])
    except Exception:
        pass
    return names


def chosen_input():
    """Index of the chosen microphone, or None for Windows' default."""
    if not MIC_DEVICE:
        return None
    try:
        api = sd.default.hostapi
        for index, device in enumerate(sd.query_devices()):
            if (device.get("name") == MIC_DEVICE and device.get("hostapi") == api
                    and device.get("max_input_channels", 0) > 0):
                return index
    except Exception:
        pass
    return None                                  # unplugged: fall back to the default


def leveled(audio):
    """Quiet recordings are turned up before the model hears them, so a
    microphone set low in Windows still transcribes well. Loud ones, and
    silence, are left alone."""
    if len(audio) == 0:
        return audio
    peak = float(np.max(np.abs(audio)))
    if peak < MIC_SILENT_PEAK or peak >= 0.25:
        return audio
    return (audio * min(0.5 / peak, 25.0)).astype(np.float32)


class Recorder:
    def __init__(self):
        self.chunks = []
        self.level = 0.0      # 0..1, read by the pop-up to draw the waveform
        self.peak = 0.0       # loudest sample in this recording (0 = dead silent)
        self.stream = None

    def start(self):
        # Never two microphone streams: one still open is closed first, so an
        # older one cannot be left recording, out of reach, for the whole run.
        self.close()
        self.chunks = []
        self.level = 0.0
        self.peak = 0.0
        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                dtype="float32", callback=self._callback,
                                device=chosen_input())
        try:
            stream.start()
        except Exception:
            try:
                stream.close()                 # opened but would not start: let it go
            except Exception:
                pass
            raise
        self.stream = stream

    def close(self):
        """Close the microphone, whatever state it is in."""
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    def recent(self, seconds):
        """The last few seconds recorded so far, without stopping."""
        chunks = list(self.chunks)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)[-int(seconds * SAMPLE_RATE):]

    def _callback(self, indata, frames, time_info, status):
        mono = indata[:, 0].copy()
        self.chunks.append(mono)
        rms = float(np.sqrt(np.mean(mono * mono))) if len(mono) else 0.0
        if len(mono):
            self.peak = max(self.peak, float(np.max(np.abs(mono))))
        loud = min(1.0, (rms * 10.0) ** 0.7)
        self.level = max(loud, self.level * 0.6)   # quick rise, soft fall

    def stop(self):
        self.close()
        if not self.chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self.chunks)


# ----------------------------------------------------------------------------
# The pop-up
# ----------------------------------------------------------------------------
class Overlay(QtWidgets.QWidget):
    cancel_clicked = QtCore.Signal()
    pill_clicked = QtCore.Signal()        # the small always-on pill was clicked
    settings_wanted = QtCore.Signal()     # ... or right-clicked
    retry_wanted = QtCore.Signal()        # the download card was right-clicked
    finish_clicked = QtCore.Signal()      # a recording started that way was clicked
    welcome_clicked = QtCore.Signal()
    notice_clicked = QtCore.Signal()
    hotkey_picked = QtCore.Signal(str)

    CARD_W, CARD_H = 164, 76       # compact by default; drag an edge to widen it
    WIDE_W = 300                   # card width when several lines of words are shown
    WELCOME_W, WELCOME_H = 256, 100
    WIN_W = 560                    # window width (and the widest the card can be
                                   # dragged); narrower cards are centred in it

    @property
    def LINE_H(self):
        """Height of one line of live words: follows the chosen text size."""
        return TEXT_SIZE + 5
    NOTE_W, NOTE_H = 120, 30       # every small pill: idle, notices, one-row dictation
    # The small pills (idle, notices): a dot or the leetsay logo in the accent
    # colour, then the hotkey or a short notice, flat and crisp: no shadow,
    # glow or shading. Sizes are at 100% scale; Qt multiplies them by the
    # screen's scaling factor.
    DOT_R = 4.5                    # the dot's radius
    LOGO_H, LOGO_MIN = 16.0, 3.0   # the logo's height; its shortest bar at least
    PILL_PAD_V, PILL_PAD_L, PILL_PAD_R = 6.0, 11.0, 13.0
    MARK_GAP, WORD_GAP = 8.0, 5.0  # mark to words; between a key and its "+"

    @staticmethod
    def _pill_font(weight):
        font = ui_font()
        font.setPixelSize(13)
        font.setWeight(weight)
        # Letters snapped to the pixel grid: the pill is see-through, where
        # Windows smooths text less, so this keeps them sharp.
        font.setHintingPreference(QtGui.QFont.PreferFullHinting)
        return font

    @classmethod
    def _key_font(cls):
        return cls._pill_font(QtGui.QFont.DemiBold)     # 600

    @classmethod
    def _plus_font(cls):
        return cls._pill_font(QtGui.QFont.Medium)       # 500

    @staticmethod
    def _pill_colours():
        """Fill, outline, key names, "+": the dark look, or the same contrast
        on the light theme."""
        if LEET_MODE:
            return (QtGui.QColor("#050A06"), QtGui.QColor("#1E7A3C"),
                    QtGui.QColor("#6EFF96"), QtGui.QColor("#2FB85C"))
        if is_light(POPUP_BG):
            # A faint see-through edge (a solid grey line showed its steps
            # round the curves); a soft shadow does the rest (see _draw_pill).
            return (QtGui.QColor("#FBFBFC"), QtGui.QColor(0, 0, 0, 34),
                    QtGui.QColor("#1B1D22"), QtGui.QColor("#737885"))
        return (QtGui.QColor("#1B1D22"), QtGui.QColor("#3A3E47"),
                QtGui.QColor("#EEF0F3"), QtGui.QColor("#8D929D"))

    @staticmethod
    def _hotkey_keys(note):
        """The keys to show when the pill is showing the hotkey, e.g.
        ["Ctrl", "Space"]; None for any other text."""
        if not note or note != pretty(HOTKEY):
            return None
        return [part for part in note.split("+") if part]

    @classmethod
    def _logo_metrics(cls):
        """Bar width, gap and height per unit of the logo: bars 8 units wide
        with 3-unit gaps, heights 1 : 3 : 3 : 7, the tallest filling LOGO_H."""
        unit = cls.LOGO_H / float(max(ICON_BARS))
        s = unit / 5.4                           # the app icon's own unit scale
        return 8.0 * s, 3.0 * s, unit

    def _mark(self):
        """The mark the pill shows now: "logo", "dot" or None. With the mark
        hidden, a dot still appears while something needs you (the status
        light: amber while loading, red for a problem)."""
        if LEET_MODE:
            return "logo"                        # 1337 mode: the four bars, in green
        if PILL_MARK == "none":
            return "dot" if self.status != "ok" else None
        return PILL_MARK

    def _mark_width(self):
        mark = self._mark()
        if mark == "logo":
            width, gap, _unit = self._logo_metrics()
            return len(ICON_BARS) * width + (len(ICON_BARS) - 1) * gap
        return 2.0 * self.DOT_R if mark == "dot" else 0.0

    def _draw_mark(self, p, left, cy, colour):
        """The dot, or the logo's four 1337 bars, centred on cy."""
        mark = self._mark()
        if mark is None:
            return
        p.setPen(Qt.NoPen)
        p.setBrush(colour)
        if mark == "dot":
            p.drawEllipse(QtCore.QPointF(left + self.DOT_R, cy), self.DOT_R, self.DOT_R)
            return
        width, gap, unit = self._logo_metrics()
        for i, units in enumerate(ICON_BARS):
            h = max(self.LOGO_MIN, unit * units)
            p.drawRoundedRect(QtCore.QRectF(left + i * (width + gap), cy - h / 2.0, width, h),
                              width / 2.0, width / 2.0)

    def _hotkey_words(self, keys):
        """[(word, is_plus, width)] for "Ctrl", "+", "Space"..., and the total width."""
        key_metrics = QtGui.QFontMetricsF(self._key_font())
        plus_metrics = QtGui.QFontMetricsF(self._plus_font())
        sized = []
        for i, key in enumerate(keys):
            if i:
                sized.append(("+", True, plus_metrics.horizontalAdvance("+")))
            sized.append((key, False, key_metrics.horizontalAdvance(key)))
        total = sum(w for _word, _plus, w in sized) + self.WORD_GAP * (len(sized) - 1)
        return sized, total

    def _pill_height(self):
        return float(self.NOTE_H)

    def _pill_width(self, keys, text=""):
        """Width of a small pill: the mark, then the hotkey or a notice."""
        if not keys and not text:
            # The mark alone: a round button for the dot, a pill that hugs the logo.
            if self._mark() != "logo":
                return self._pill_height()
            return float(round(self.PILL_PAD_L + self._mark_width() + self.PILL_PAD_L))
        if self._mark() is None:                 # no mark: the words, evenly padded
            width = 2 * self.PILL_PAD_R
        else:
            width = self.PILL_PAD_L + self._mark_width() + self.MARK_GAP + self.PILL_PAD_R
        if keys:
            width += self._hotkey_words(keys)[1]
        else:
            # (+2: room for the outline and pixel rounding, so it is never cut short)
            width += QtGui.QFontMetricsF(self._key_font()).horizontalAdvance(text) + 2.0
        return float(round(min(380.0, width)))

    def _draw_pill(self, p, keys, card, accent, text=""):
        """A whole small pill, flat: its fill and a 1 px outline lined up with
        the screen's real pixels, the mark, then the hotkey's words or a short
        notice."""
        ratio = self.devicePixelRatioF() or 1.0
        snap = lambda value: round(value * ratio) / ratio
        # The outline is one or two whole screen pixels (1 px at 150% falls
        # between pixels and looks fuzzy), drawn as a filled ring so its curves
        # come out smooth.
        line = max(1, int(round(ratio))) / ratio
        outer_box = QtCore.QRectF(snap(card.left()), snap(card.top()),
                                  snap(card.width()), snap(card.height()))
        box = outer_box.adjusted(line / 2.0, line / 2.0, -line / 2.0, -line / 2.0)
        fill, outline, key_colour, plus_colour = self._pill_colours()
        outer = QtGui.QPainterPath()
        outer.addRoundedRect(outer_box, outer_box.height() / 2.0, outer_box.height() / 2.0)
        inner_box = outer_box.adjusted(line, line, -line, -line)
        inner = QtGui.QPainterPath()
        inner.addRoundedRect(inner_box, inner_box.height() / 2.0, inner_box.height() / 2.0)
        p.setPen(Qt.NoPen)
        if is_light(POPUP_BG):
            # On the light theme a soft shadow lifts the pill off the screen,
            # as light pop-ups do, instead of a strong outline.
            shade = glow_pixmap(outer_box.width(), outer_box.height(),
                                outer_box.height() / 2.0, QtGui.QColor(0, 0, 0), 10.0, 5.0,
                                ratio)
            if shade is not None:
                p.save()
                p.setOpacity(p.opacity() * 0.11)
                p.drawPixmap(outer_box.adjusted(-10, -7, 10, 13), shade,
                             QtCore.QRectF(shade.rect()))
                p.restore()
        # The whole shape filled first, the outline laid over it: drawn as two
        # pieces that only touch, a faint seam of the background showed
        # between them (worst on a light pill).
        p.fillPath(outer, QtGui.QBrush(fill))
        # Edge correction: a light shape on a dark screen (or the other way)
        # loses some strength in its softened edge pixels, so the curve looks
        # thin and stepped. A hair-thin line of the fill colour, half strength,
        # along the edge gives those pixels back their proper weight.
        firm = QtGui.QColor(fill)
        firm.setAlphaF(0.5)
        p.setPen(QtGui.QPen(firm, 0.6))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(outer_box.adjusted(0.3, 0.3, -0.3, -0.3),
                          outer_box.height() / 2.0 - 0.3, outer_box.height() / 2.0 - 0.3)
        p.setPen(Qt.NoPen)
        p.fillPath(outer.subtracted(inner), QtGui.QBrush(outline))
        cy = box.center().y()
        still = self.mode == "idle" or self.previewing
        pulse = 1.0 if still else 0.5 + 0.5 * math.sin(self.phase * 0.16)
        colour = QtGui.QColor(accent)
        colour.setAlphaF(colour.alphaF() * (0.55 + 0.45 * pulse))   # breathes when busy
        if not keys and not text:                # the mark alone, in the middle
            if self._mark() is None:             # nothing else to show: a quiet dot
                p.setPen(Qt.NoPen)
                p.setBrush(colour)
                p.drawEllipse(QtCore.QPointF(box.center().x(), cy), self.DOT_R, self.DOT_R)
                return
            self._draw_mark(p, snap(box.center().x() - self._mark_width() / 2.0), cy, colour)
            return
        if self._mark() is None:
            x = snap(card.left() + self.PILL_PAD_R)
        else:
            left = snap(card.left() + self.PILL_PAD_L)
            self._draw_mark(p, left, cy, colour)
            x = left + self._mark_width() + self.MARK_GAP
        # Centred on the capital letters, not on the whole line (which also
        # holds room for letters that hang below it, and so sat the words low):
        # the gap above "Ctrl" equals the gap below it. The line it sits on is
        # put on a whole pixel, so the letters stay sharp.
        baseline = snap(cy + QtGui.QFontMetricsF(self._key_font()).capHeight() / 2.0)
        if not keys:                             # a short notice
            metrics = QtGui.QFontMetricsF(self._key_font())
            room = box.right() - self.PILL_PAD_R - x
            p.setFont(self._key_font())
            # A missing microphone is said in red: nothing can work without it.
            p.setPen(popup_theme()["danger"] if text == self.NO_MIC else key_colour)
            p.drawText(QtCore.QPointF(snap(x), baseline),
                       metrics.elidedText(text, Qt.ElideRight, room))
            return
        words, _total = self._hotkey_words(keys)
        for word, plus, width in words:
            p.setFont(self._plus_font() if plus else self._key_font())
            p.setPen(plus_colour if plus else key_colour)
            p.drawText(QtCore.QPointF(snap(x), baseline), word)
            x += width + self.WORD_GAP
    GLOW = 36          # transparent room around the card for its glow and shadow

    def __init__(self, recorder):
        super().__init__(None,
                         Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus)   # clickable, never takes focus
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.preview_words = None  # third-row text while previewing
        self.warning = ""          # microphone problem shown where the words go
        self.box_missing = False   # typing as you speak: no text box has the focus
        self.status = "ok"         # the pill's light: "ok" (accent), "busy" (amber),
                                   # "problem" (red: no microphone, no model...)
        self._line_key = None      # what the card's words line shows, and since when
        self._line_t0 = 0.0
        self.picking = False       # welcome card is showing the hotkey choices
        self._hits = {}            # welcome card: name -> (x, y, w, h) click targets
        self._mouse = None         # last mouse position over the window
        self._card = None          # (x, y, w, h) of the card as last drawn
        self.ring = 0.0            # how far the outline is swollen by the voice
        self.app_colour = None     # main colour of the focused app's icon
        self.pulse_started = None  # when the one-off "hello" pulse began
        self.progress = None       # 0..1 while a download's progress shows on the pill
        self._own_icon = None      # this app's icon, for the download card
        self.message_stays = False # the notice showing has no time limit
        self.rows = 1.0            # rows of words the card currently has room for
        self.half_step = False     # flips every tick: step-based things run on True
        self.click_started = False # this recording was started by clicking the pill
        self._resizing = None      # 'left' / 'right' while an edge is being dragged
        self._edge_hover = None    # the edge the mouse is resting on, if any
        self.hover_armed = True    # False until the mouse has left the card once
        self.spin = 0.0            # where the band of light is on the outline, degrees
        self.apply_size()
        self.welcome_ready = False # first-run card: model finished loading?
        self.live_text = ""        # words shown so far in this recording
        self.live_target = ""      # words heard so far (shown ones catch up to this)
        self.reveal_credit = 0.0
        self._clear_words()

        self.recorder = recorder
        self.mode = "message"            # "recording" | "transcribing" | "message"
        self.message = ""
        self.levels = deque([0.0] * N_DOTS, maxlen=N_DOTS)
        self.phase = 0.0
        self._message_id = 0
        self.fade = 0.0            # current opacity of the whole pop-up, 0..1
        self.fade_target = 0.0
        self.started = time.monotonic()
        self.previewing = False    # showing a demo, not a real recording
        self.app_name = ""         # focused app, shown while recording
        self.app_icon = None
        self.hover = False         # mouse is over the pop-up
        self._press = None         # where a mouse press started
        self._offset = None
        self._dragging = False
        self._pos = self._load_position()    # where the user dragged it, if anywhere
        self.setMouseTracking(True)

        self._morph = None             # a shape change under way (pill <-> card)
        self._shape_kind = None        # "small", "card" or "welcome": what was drawn
        self._last_frame = None        # the last picture, at full strength
        self._last_origin = QtCore.QPointF(0, 0)
        self._last_shape = None        # ... and its card's rectangle and corner radius
        self._last_fade = 0.0
        self._card_radius = 0.0
        self._clamp_shift = QtCore.QPointF(0, 0)   # how far the card was moved to fit
        self._card_screen = None       # the screen the card is on
        self._glow_margin = None       # the glow's size, kept steady while dragging
        self._slide = None             # the pill moving in from an edge for its pulse
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(16)               # about 60 frames a second
        self.timer.timeout.connect(self._tick)

    # -- public --------------------------------------------------------------
    def show_recording(self, app_name="", exe_path=""):
        self.mode = "recording"
        self.started = time.monotonic()
        self.app_name = app_name if SHOW_APP_NAME else ""
        self.app_icon = None
        self.app_colour = None
        if (SHOW_APP_ICON or GLOW_FROM_APP) and exe_path:
            try:
                icon = QtWidgets.QFileIconProvider().icon(QtCore.QFileInfo(exe_path))
                if not icon.isNull():
                    # Take the largest picture the app offers and shrink it once,
                    # with proper smoothing, to exactly the pixels it will cover.
                    # (Shrinking a big icon on every frame skips pixels and leaves
                    # rough edges, which show most against a dark card.)
                    picture = icon.pixmap(256, 256)
                    if SHOW_APP_ICON:
                        ratio = self.devicePixelRatioF()
                        side = max(1, int(round(20 * ratio)))
                        small = picture.scaled(side, side, Qt.KeepAspectRatio,
                                               Qt.SmoothTransformation)
                        small.setDevicePixelRatio(ratio)
                        self.app_icon = small
                    if GLOW_FROM_APP:
                        self.app_colour = icon_colour(picture)
            except Exception:
                self.app_icon = None
        self.levels = deque([0.0] * N_DOTS, maxlen=N_DOTS)
        self.live_text = ""
        self.live_target = ""
        self.reveal_credit = 0.0
        self._clear_words()
        self.warning = ""
        self.box_missing = False
        self.rows = 1.0
        self._present()

    NO_SOUND = "No sound from the microphone"
    CARD_EDGE_GAP = 24             # a dictation card's room from the screen's edges
    TASKBAR_GAP = 18               # ... and from the taskbar
    NO_BOX = "No text box selected"
    NO_MIC = "Microphone not detected  \u00b7  click for help"

    def _mic_warning(self):
        """Say so if the microphone is giving nothing a couple of seconds in."""
        elapsed = time.monotonic() - self.started
        peak = getattr(self.recorder, "peak", 1.0)
        if elapsed > 1.5 and peak < MIC_SILENT_PEAK:
            return self.NO_SOUND
        if elapsed > 5.0 and peak < MIC_QUIET_PEAK and not self.live_text:
            return "Can't hear you. Check the mic"
        if self.box_missing:
            return self.NO_BOX                   # typing as you speak is waiting
        return ""

    # -- size ------------------------------------------------------------------
    @property
    def live_h(self):
        """Height set aside for the live words (grows with the line count)."""
        return 8 + self.LINE_H * LIVE_LINES

    def _below(self):
        """Room below the pill's bottom line. A card opens centred on the pill,
        but a pill dragged to the top of the screen has its card open
        downwards instead, so there is room for the whole card below it."""
        return int(self.CARD_H + self.live_h - self.NOTE_H) + 4

    def apply_size(self):
        """Size the window for the current settings, keeping its base in place."""
        anchor = self._anchor() if self.isVisible() else None
        # Twice the widest card: a pill pushed against a screen edge opens its
        # card beside it, shifted inwards, and that card must still fit in the
        # (see-through) window.
        self.resize(2 * (self.WIN_W + self.GLOW),
                    max(self.CARD_H + self.live_h, self.WELCOME_H) + 2 * self.GLOW
                    + self._below())
        if anchor is not None:
            self.move(self._top_left(anchor))
        self.update()

    def _anchor(self):
        """The pill's bottom-centre point: stays put when the size changes."""
        return QtCore.QPoint(self.pos().x() + self.width() // 2,
                             self.pos().y() + self.height() - self.GLOW - self._below())

    def _top_left(self, anchor):
        return QtCore.QPoint(anchor.x() - self.width() // 2,
                             anchor.y() - self.height() + self.GLOW + self._below())

    def show_idle(self):
        """The small always-there pill between dictations."""
        self.mode = "idle"
        self.previewing = False
        self._present()

    def _note_text(self):
        """Text of the small pill, or None when the full card is showing."""
        if self.mode == "message":
            return self.message.replace("\n", " ")
        if self.mode == "idle":
            # Without the hotkey text the pill is just its dot: a small round
            # button, as quiet as it can be.
            return pretty(HOTKEY) if SHOW_HOTKEY_HINT else ""
        return None

    def show_welcome(self, ready):
        """First launch: stays on screen until it is clicked."""
        self.mode = "welcome"
        self.welcome_ready = bool(ready)
        self.previewing = False
        self._present()

    def _target_at(self, point):
        """Which welcome-card target (button, hotkey, a choice) is at this spot."""
        if self.mode != "welcome" or point is None:
            return None
        px, py = point
        for name, (x, y, w, h) in self._hits.items():
            if x <= px <= x + w and y <= py <= y + h:
                return name
        return None

    def _clear_words(self):
        self.live_lines = []       # rows of words, as laid out on the card
        self.line_w = []           # each row's drawn width (glides to its real width)
        self.pending = []          # words heard but not on the card yet
        self.row_slide = 0.0          # rows sliding up after a new row started
        self.word_fade = 1.0       # the newest word fades in
        self.second_look = {}      # word number -> a change the model suggested once

    def _measure(self):
        font = ui_font()
        font.setPixelSize(TEXT_SIZE)
        return QtGui.QFontMetricsF(font).horizontalAdvance

    def _fit_width(self, base):
        """The card's width once the app's name is on it: wide enough to show
        the name in full with a decent length of wave beside it."""
        if not self.app_name:
            return base
        font = ui_font()
        font.setPixelSize(12)
        font.setWeight(QtGui.QFont.DemiBold)
        name_w = QtGui.QFontMetricsF(font).horizontalAdvance(self.app_name)
        icon_w = 20.0 if self.app_icon is not None else 10.0
        need = (28.0 + icon_w + 8.0 + name_w + (40.0 if SHOW_TIMER else 0.0)
                + (76.0 if SHOW_WAVEFORM else 0.0))
        return max(float(base), min(float(self.WIN_W), need))

    def _words_room(self):
        if CARD_WIDTH:
            base = max(self.CARD_W, min(self.WIN_W, CARD_WIDTH))
        else:
            base = self.WIDE_W if WIDE_CARD else self.CARD_W
        return self._fit_width(base) - 32

    def _rewrap(self):
        """Lay the words out again from the start (the card changed width)."""
        words = [word for line in self.live_lines for word in line]
        measure, room = self._measure(), self._words_room()
        self.live_lines, self.line_w = [], []
        for word in words:
            last = self.live_lines[-1] if self.live_lines else None
            if last is not None and measure(" ".join(last + [word])) <= room:
                last.append(word)
            else:
                self.live_lines.append([word])
                self.line_w.append(None)
        self.row_slide = 0.0
        self.second_look = {}

    def _edge_at(self, spot):
        """'left' or 'right' when this point is on that edge of the full card
        (where a drag resizes it), else None."""
        if self.mode != "recording" or self._card is None:
            return None
        left, top, width, height = self._card
        if height <= self.NOTE_H or not (top <= spot.y() <= top + height):
            return None                          # the one-row pill has a set width
        if abs(spot.x() - left) <= 9:
            return "left"
        if abs(spot.x() - (left + width)) <= 9:
            return "right"
        return None

    def set_live_text(self, text):
        """Take the model's newest reading of the recent audio.

        Words already on the card keep their place: a reworded or re-punctuated
        word is swapped where it stands, rows that are full are never re-broken,
        and older words the model no longer re-reads stay as they were. Only
        words that are actually new are fed in, at the end."""
        self.live_target = text
        target = text.split()
        shown = [word for line in self.live_lines for word in line]
        start = overlap_start(shown, target)
        have = len(shown) - start                # words on the card this reading covers
        self.pending = target[have:]
        # The model often flips a word back and forth between readings
        # ("All right" / "Alright"). A word on the card is only swapped once
        # two readings in a row agree on the change, so it doesn't flicker.
        words, votes = list(shown[:start]), {}
        for i, new in enumerate(target[:have]):
            at = start + i
            if new == shown[at] or self.second_look.get(at) == new:
                words.append(new)
            else:
                votes[at] = new
                words.append(shown[at])
        self.second_look = votes
        if words != shown:
            self._refill(words)
        self.update()

    def _refill(self, words):
        """Put `words` back into the rows as they are, word for word."""
        measure, room = self._measure(), self._words_room()
        at, lines = 0, []
        for line in self.live_lines:
            if at >= len(words):
                break
            lines.append(words[at:at + len(line)])
            at += len(line)
        for i, line in enumerate(lines):         # a swap made a row far too long
            while len(line) > 1 and measure(" ".join(line)) > room + 14:
                if i + 1 == len(lines):
                    lines.append([])
                lines[i + 1].insert(0, line.pop())
        self.live_lines = lines
        self.line_w = (self.line_w + [None] * len(lines))[:len(lines)]
        self.live_text = " ".join(words)

    def _add_word(self, word):
        measure, room = self._measure(), self._words_room()
        last = self.live_lines[-1] if self.live_lines else None
        if last is None or measure(" ".join(last + [word])) > room:
            if len(self.live_lines) >= max(1, LIVE_LINES):
                self.row_slide = min(self.row_slide + self.LINE_H, float(self.LINE_H))
            self.live_lines.append([word])
            self.line_w.append(None)
            if len(self.live_lines) > 60:        # plenty of history to line up against
                del self.live_lines[0], self.line_w[0]
        else:
            last.append(word)
        self.word_fade = 0.0
        self.live_text = " ".join(w for line in self.live_lines for w in line)

    def _reveal_words(self):
        """Feed waiting words in at a steady pace (faster when many wait)."""
        if not self.pending:
            return
        self.reveal_credit += max(0.25, len(self.pending) / 10.0)
        step = int(self.reveal_credit)
        self.reveal_credit -= step
        for _ in range(min(step, len(self.pending))):
            self._add_word(self.pending.pop(0))

    def show_transcribing(self):
        self.mode = "transcribing"
        self.phase = 0.0
        self._present()

    PULSE_SECONDS = 0.9

    def show_message(self, text, ms=1600, pulse=False):
        """Short notice. ms=None keeps it up until something replaces it.
        pulse=True sends one wave of the accent colour out from it."""
        self.mode = "message"
        self.message = text
        self.message_stays = not ms              # no time limit: only replaced, never timed out
        self.progress = None                     # set again by whoever shows progress
        self.pulse_started = time.monotonic() if pulse else None
        self._message_id += 1
        this_id = self._message_id
        self._present()
        if ms:
            QtCore.QTimer.singleShot(ms, lambda: self._hide_message(this_id))

    def invite_drag(self):
        self.show_message("Drag me to where you want me", ms=7000)

    def reset_position(self):
        self._pos = None
        save_setting("anchor", None)
        save_setting("anchor2", None)
        if self.isVisible():
            self.move(self._top_left(self._home()))

    def preview(self, ms=2600, label="Your app", words=None, shape=True):
        """Show the full pop-up as a still picture: at startup, and after a
        setting change. Nothing is recorded and nothing moves, so it can't be
        mistaken for the microphone being on. shape=False leaves the bars flat."""
        self.previewing = True
        self.preview_ms = ms                     # None: stays until replaced
        self.preview_words = words
        self.show_recording(label)
        if SHOW_APP_ICON:                        # a stand-in for the app's own icon
            try:
                ratio = self.devicePixelRatioF()
                side = max(1, int(round(20 * ratio)))
                picture = app_icon().pixmap(128, 128).scaled(
                    side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                picture.setDevicePixelRatio(ratio)
                self.app_icon = picture
            except Exception:
                self.app_icon = None
        if shape:                                # a fixed, made-up outline
            still = [0.12, 0.3, 0.2, 0.55, 0.35, 0.8, 0.5, 1.0][::-1]
            for i in range(N_DOTS):
                self.levels[-1 - i] = still[i] * 0.8 if i < len(still) else 0.0
        self._message_id += 1
        this_id = self._message_id
        if ms:                                   # ms=None: stays until replaced
            QtCore.QTimer.singleShot(ms, lambda: self._end_preview(this_id))

    def _end_preview(self, preview_id):
        # The card stays a still picture while it fades out: "previewing" is
        # only cleared when a real recording (or another card) takes over.
        if self.previewing and preview_id == self._message_id:
            self.dismiss()

    def dismiss(self, force=False):
        """Fade out (the window hides once fully transparent). With the
        always-on setting, settle back to the small pill instead."""
        if ALWAYS_SHOW and not force:
            self.show_idle()
            return
        self.fade_target = 0.0
        if not self.isVisible():
            self.timer.stop()
        else:
            self.timer.start()                   # the idle pill's clock is stopped: wake
                                                 # it, or the fade-out never happens
    # -- internals -----------------------------------------------------------
    def _hide_message(self, message_id):
        if self.mode == "message" and message_id == self._message_id:
            self.dismiss()

    def _present(self):
        if not self._dragging:
            self.move(self._top_left(self._home()))
        if not self.isVisible():
            self.fade = 0.0
        self.fade_target = 1.0
        self.show()
        self._set_hover(False)                   # worked out afresh on the next frame
        self.timer.start()
        self.update()

    def _home(self):
        """Bottom-centre point for the pop-up: the dragged spot, else just above
        the taskbar in the middle of the screen."""
        if self._pos is not None:
            # The pill must be fully on a screen, a little away from its edges
            # (older versions let it be dragged half off). A spot on a monitor
            # that has since been unplugged or moved falls back to the usual
            # place; a spot partly off a screen is pulled back onto it.
            card = QtCore.QRect(self._pos.x() - 30, self._pos.y() - 26, 60, 20)
            for screen in QtGui.QGuiApplication.screens():
                if screen.availableGeometry().intersects(card):
                    # Half the idle pill's own width (never a card's: a card
                    # as wide would push the pill away from where you put it).
                    keys = self._hotkey_keys(pretty(HOTKEY)) if SHOW_HOTKEY_HINT else None
                    half = self._pill_width(keys) / 2.0
                    spot = self._inside(screen.availableGeometry(), QtCore.QRectF(
                        self._pos.x() - half, self._pos.y() - self.NOTE_H,
                        2 * half, self.NOTE_H))
                    return QtCore.QPoint(self._pos.x() + int(round(spot.x())),
                                         self._pos.y() + int(round(spot.y())))
        screen = QtGui.QGuiApplication.primaryScreen()    # the main monitor
        area = screen.availableGeometry()     # excludes the taskbar
        return QtCore.QPoint(area.x() + area.width() // 2,
                             area.y() + area.height() - BOTTOM_MARGIN)

    @staticmethod
    def _inside(area, rect, gaps=None):
        """How far rect (in screen coordinates) must move to sit inside area,
        EDGE_GAP away from its edges (or gaps: left, top, right, bottom).
        Too big to fit: its top-left wins."""
        gl, gt, gr, gb = gaps or (EDGE_GAP,) * 4
        left, top = area.left() + gl, area.top() + gt
        right, bottom = area.right() + 1 - gr, area.bottom() + 1 - gb
        dx = dy = 0.0
        if rect.right() > right:
            dx = right - rect.right()
        if rect.left() + dx < left:
            dx = left - rect.left()
        if rect.bottom() > bottom:
            dy = bottom - rect.bottom()
        if rect.top() + dy < top:
            dy = top - rect.top()
        return QtCore.QPointF(dx, dy)

    @staticmethod
    def _load_position():
        try:
            if "anchor2" in _config:
                x, y = _config["anchor2"]
            else:                                # older versions saved the window's base,
                x, y = _config["anchor"]         # 18 px below the card's
                y = int(y) - 18
            return QtCore.QPoint(int(x), int(y))
        except Exception:
            return None

    def _save_position(self):
        save_setting("anchor2", [self._pos.x(), self._pos.y()])

    # -- mouse: hover shows "Cancel recording", click cancels, drag moves ----
    def _set_hover(self, value):
        value = bool(value)
        if not value:
            self.hover_armed = True
        if value != self.hover:
            self.hover = value
            self.update()
        clickable = value and ((self.mode == "recording" and not self.previewing)
                               or self._target_at(self._mouse) is not None)
        if value and (self._edge_hover or self._resizing):
            self.setCursor(Qt.SizeHorCursor)     # on an edge: drag to resize
            return
        self.setCursor(Qt.PointingHandCursor if clickable else Qt.ArrowCursor)

    def _cursor_on_card(self):
        """Is the mouse over the card that is actually drawn? (The window is
        bigger than the card: it has see-through space around it.)"""
        if self._card is None:
            return False
        spot = QtGui.QCursor.pos()
        x, y = spot.x() - self.pos().x(), spot.y() - self.pos().y()
        left, top, width, height = self._card
        return left <= x <= left + width and top <= y <= top + height

    def enterEvent(self, event):
        self._set_hover(self._cursor_on_card())

    def leaveEvent(self, event):
        self._mouse = None
        self._edge_hover = None
        self._set_hover(False)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press = event.globalPosition().toPoint()
            self._offset = self._press - self.pos()
            self._dragging = False
            self._message_id += 1          # keep a notice up while it is held
            self._resizing = self._edge_at(event.position())

    def mouseMoveEvent(self, event):
        spot = event.position()
        self._mouse = (spot.x(), spot.y())
        if self._resizing and self._card is not None and (event.buttons() & Qt.LeftButton):
            # Dragging an edge: the card stays centred and both sides follow.
            centre = self._card[0] + self._card[2] / 2.0
            wanted = int(round(abs(spot.x() - centre) * 2.0))
            wanted = max(self.CARD_W, min(self.WIN_W, wanted))
            if wanted != CARD_WIDTH:
                globals()["CARD_WIDTH"] = wanted         # saved when you let go
                self._rewrap()
                self.update()
            return
        if self._press is None:
            edge = self._edge_at(spot)
            if edge != self._edge_hover:
                self._edge_hover = edge
                self._set_hover(self._cursor_on_card())
        if self.mode == "idle" and not self._dragging:
            self.setCursor(Qt.PointingHandCursor)      # the pill can be clicked
        if self.mode == "welcome" and not self._dragging:
            self._set_hover(self._cursor_on_card())    # refreshes the cursor shape
            self.update()                         # and the hover highlight
        if self._press is None or not (event.buttons() & Qt.LeftButton):
            return
        here = event.globalPosition().toPoint()
        if not self._dragging and (here - self._press).manhattanLength() > 6:
            self._dragging = True
        if self._dragging:
            spot = here - self._offset
            if self._card is not None:
                # Stop at the edges of the screen under the mouse (the taskbar
                # included), a few pixels short, so it can't be lost off-screen.
                screen = QtGui.QGuiApplication.screenAt(here) or self.screen()
                if screen is not None:
                    left, top, width, height = self._card
                    nudge = self._inside(screen.availableGeometry(), QtCore.QRectF(
                        spot.x() + left, spot.y() + top, width, height))
                    spot += QtCore.QPoint(int(round(nudge.x())), int(round(nudge.y())))
            self.move(spot)
            self.update()

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.RightButton and self.mode in ("idle", "message")
                and self.fade_target > 0.0 and self._cursor_on_card()):
            if self.mode == "message" and self.progress is not None:
                self.retry_wanted.emit()         # right-click the download card: retry
            else:
                self.settings_wanted.emit()      # right-click the pill: Settings
            return
        if event.button() != Qt.LeftButton or self._press is None:
            return
        if self._resizing:                       # finished resizing: keep the width
            self._resizing = None
            self._press = None
            self._dragging = False
            save_setting("card_width", int(CARD_WIDTH))
            if self.previewing:
                this_id = self._message_id
                QtCore.QTimer.singleShot(2500, lambda: self._end_preview(this_id))
            self.update()
            return
        was_drag = self._dragging
        self._press = None
        self._dragging = False
        if self.previewing and self.mode == "recording":
            # A still preview card that was moved or clicked: the press cancelled
            # its timer, so set it again, or the card would stay (and keep
            # redrawing) until the next dictation.
            if was_drag:
                self._pos = self._anchor()
                self._save_position()
            if getattr(self, "preview_ms", None):
                this_id = self._message_id
                QtCore.QTimer.singleShot(2500, lambda: self._end_preview(this_id))
            self.update()
            return
        if was_drag:
            self._pos = self._anchor()
            self._save_position()
        elif self.mode == "welcome" and self.fade_target > 0.0:
            spot = event.position()
            target = self._target_at((spot.x(), spot.y()))
            if target == "start":
                self.welcome_clicked.emit()
            elif target == "hotkey":
                self.picking = not self.picking
            elif target is not None and target.startswith("pick:"):
                self.picking = False
                self.hotkey_picked.emit(target[5:])
            elif self.picking and target is None:
                self.picking = False              # clicked away from the choices
        elif (self.mode == "recording" and self.fade_target > 0.0
              and self._cursor_on_card()):
            if self.click_started:
                self.finish_clicked.emit()
            else:
                self.cancel_clicked.emit()
        elif self.mode == "idle" and self.fade_target > 0.0 and self._cursor_on_card():
            self.pill_clicked.emit()
        elif self.mode == "message" and self._cursor_on_card():
            self.notice_clicked.emit()
        if self.mode == "message" and not self.message_stays:
            # A timed notice that was held or clicked goes away shortly after.
            # One with no time limit (a download in progress, say) stays put.
            this_id = self._message_id
            QtCore.QTimer.singleShot(1500, lambda: self._hide_message(this_id))
        self.update()

    def _tick(self):
        if self._press is not None and \
                not (QtGui.QGuiApplication.mouseButtons() & Qt.LeftButton):
            # The button was let go where this window never heard of it (the
            # screen locked mid-click, say): forget the press, or the pop-up
            # could never hide.
            self._press = None
            self._dragging = False
            self._resizing = None
        # Fade in quickly (about 120 ms), fade out a little slower.
        speed = 0.30 if self.fade_target > self.fade else 0.16
        self.fade += (self.fade_target - self.fade) * speed
        if self.fade_target == 0.0 and self.fade < 0.03 and self._press is None:
            self.fade = 0.0
            self.hover = False
            self.timer.stop()
            self.hide()
            return
        if self.mode == "idle" and abs(self.fade - self.fade_target) < 0.02 \
                and self._morph is None and self._slide is None:
            self.fade = self.fade_target        # nothing moves while idle:
            self.timer.stop()                   # stop redrawing, use no CPU
            self.update()
            return

        # A card that sits still (a notice, the download card, a still preview)
        # needs far fewer redraws than a live dictation: save the CPU.
        settled = abs(self.fade - self.fade_target) < 0.02 and self.word_fade >= 1.0
        if settled and self.previewing and self.mode == "recording":
            wanted = 100                          # a still picture
        elif settled and self.mode == "message":
            wanted = 50                           # progress and small movements only
        else:
            wanted = 16
        if self._morph is not None or self._slide is not None:
            wanted = 16                           # a shape change or a slide: every frame
        if self.timer.interval() != wanted:
            self.timer.setInterval(wanted)

        # Motion (fades, the card growing, the glow) is redrawn on every tick.
        # Things that move in steps (the waveform, the words, timers) advance
        # on every second tick, so they keep the pace they were tuned for.
        self.half_step = not self.half_step
        if self.word_fade < 1.0:
            self.word_fade = min(1.0, self.word_fade + 0.14)
        if self.half_step:
            self.phase += 1.0
            if self._press is None:              # keep "is the mouse on it?" honest
                self._set_hover(self._cursor_on_card())
            if self.mode == "recording" and self.fade_target > 0.0:
                if not self.previewing:
                    self.levels.append(self.recorder.level)
                    self.warning = self._mic_warning()
                    self._reveal_words()
            elif self.mode == "transcribing":
                for i in range(N_DOTS):                  # bars settle down
                    self.levels[i] *= 0.72
        self.update()

    def paintEvent(self, event):
        """Draw the pop-up, faded as one piece. Each frame is drawn fresh, at
        full strength, at the screen's real scale, then shown at the right
        transparency. (Fading each layer separately lets the shadow and glow
        show through the card, which turns it a muddy grey.) While the pill
        turns into the card or back, the two are blended as one changing
        shape (see _morph_frame)."""
        fade = max(0.0, min(1.0, self.fade))
        ratio = self.devicePixelRatioF()
        area = self._paint_area()
        frame = QtGui.QPixmap(max(1, int(round(area.width() * ratio))),
                              max(1, int(round(area.height() * ratio))))
        frame.setDevicePixelRatio(ratio)
        frame.fill(Qt.transparent)
        p = QtGui.QPainter(frame)
        try:
            p.translate(-area.x(), -area.y())
            self._draw(p)
        finally:
            p.end()
        shown = self._morph_frame(frame, fade, area)
        p = QtGui.QPainter(self)
        try:
            if fade < 0.995:
                p.setOpacity(fade)
            p.drawPixmap(area.topLeft(), shown)
        finally:
            p.end()

    SLIDE_IN, SLIDE_OUT = 0.18, 0.25     # seconds: the pill moving in for its pulse, and back

    def _ready_slide(self, card, note, width, height):
        """The "Ready" pulse needs room round the pill. With the pill close to
        a screen edge it would be cut off, so the pill first slides in just
        far enough, sends out its pulse, then glides back to its place.
        Returns how far the pill is moved in right now."""
        if note is None or self.mode != "message" or self._dragging:
            self._slide = None                   # only for the small pill's pulse
            return QtCore.QPointF(0, 0)
        now = time.monotonic()
        if self._slide is None and self.pulse_started is not None \
                and now - self.pulse_started < 0.5 and animations_on():
            screen = self._card_screen or self.screen()
            if screen is not None:
                whole = QtCore.QRectF(screen.geometry()).translated(-self.x(), -self.y())
                # How far the pulse reaches (its outer part is too faint to count).
                reach_x = (width + 2.0 * self.GLOW) * 1.42 / 2.0 * 0.82
                reach_y = (height + 2.0 * self.GLOW) * 1.62 / 2.0 * 0.82
                cx, cy = card.center().x(), card.center().y()
                dx = max(0.0, whole.left() - (cx - reach_x)) - max(0.0, (cx + reach_x) - whole.right())
                dy = max(0.0, whole.top() - (cy - reach_y)) - max(0.0, (cy + reach_y) - whole.bottom())
                if abs(dx) > 1.0 or abs(dy) > 1.0:
                    self._slide = {"t0": self.pulse_started, "shift": QtCore.QPointF(dx, dy)}
        if self._slide is None:
            return QtCore.QPointF(0, 0)
        t = now - self._slide["t0"]
        hold = self.SLIDE_IN + self.PULSE_SECONDS
        if t < self.SLIDE_IN:                    # in: quick at first, gentle at the end
            amount = 1.0 - (1.0 - t / self.SLIDE_IN) ** 3
        elif t < hold:
            amount = 1.0
        elif t < hold + self.SLIDE_OUT:          # back: gentle at both ends
            u = (t - hold) / self.SLIDE_OUT
            amount = 1.0 - u * u * (3.0 - 2.0 * u)
        else:
            self._slide = None
            return QtCore.QPointF(0, 0)
        return self._slide["shift"] * amount

    def _paint_area(self):
        """The part of the window drawn this frame: the card with room for its
        glow and shadow. The window is far wider than the card (so a card can
        shift at a screen edge), and drawing all of it every frame cost three
        times as much. The whole window is drawn when the shape is changing
        (pill to card), for the welcome card and the "Ready" pulse."""
        whole = QtCore.QRect(0, 0, self.width(), self.height())
        note = self._note_text()
        kind = "small" if note is not None else ("welcome" if self.mode == "welcome" else "card")
        if (self._card is None or self._morph is not None or kind != self._shape_kind
                or kind == "welcome" or self.pulse_started is not None or self._dragging
                or self._slide is not None):
            return whole
        left, top, width, height = self._card
        room = self.GLOW + 28
        return QtCore.QRectF(left - room, top - room, width + 2 * room,
                             height + 2 * room).toAlignedRect().intersected(whole)

    def _morph_frame(self, frame, fade, area):
        """Pill to card (or back) as one shape: its position, size, corners and
        fill move smoothly from one to the other (about 200 ms growing, 160 ms
        shrinking, fast at first, gentle at the end). What the pill showed
        fades out in the first third, what the card shows fades in over the
        last half, and the card's glow comes last. The contents are never
        stretched: each is a picture drawn at its real size, only revealed
        inside the changing shape. Windows' "Animation effects" off: no
        animation, the new shape is simply there."""
        note = self._note_text()
        kind = "small" if note is not None else ("welcome" if self.mode == "welcome" else "card")
        target = QtCore.QRectF(*self._card) if self._card else None
        radius = float(self._card_radius)
        now = time.monotonic()
        if (kind != self._shape_kind and self._last_frame is not None
                and self._last_shape is not None and target is not None
                and fade > 0.9 and self._last_fade > 0.9 and "welcome" not in
                (kind, self._shape_kind) and animations_on()):
            start, start_radius = self._last_shape
            grow = target.width() * target.height() >= start.width() * start.height()
            self._morph = {"t0": now, "length": 0.20 if grow else 0.16, "rect": start,
                           "radius": start_radius, "old": self._last_frame,
                           "origin": QtCore.QPointF(self._last_origin),
                           "from": self._shape_fill(self._shape_kind),
                           "to": self._shape_fill(kind)}
            self.timer.start()
        elif kind != self._shape_kind:
            self._morph = None
        self._shape_kind = kind
        self._last_fade = fade
        shown = frame
        morph = self._morph
        if morph is not None and target is not None:
            t = min(1.0, (now - morph["t0"]) / morph["length"])
            eased = 1.0 - (1.0 - t) ** 3

            def mix(a, b):
                return a + (b - a) * eased
            was = morph["rect"]
            shape = QtCore.QRectF(mix(was.left(), target.left()), mix(was.top(), target.top()),
                                  mix(was.width(), target.width()),
                                  mix(was.height(), target.height()))
            corner = min(mix(morph["radius"], radius), shape.height() / 2.0)
            a, b = morph["from"], morph["to"]
            body = QtGui.QColor(int(mix(a.red(), b.red())), int(mix(a.green(), b.green())),
                                int(mix(a.blue(), b.blue())), int(mix(a.alpha(), b.alpha())))
            shown = QtGui.QPixmap(frame.size())
            shown.setDevicePixelRatio(frame.devicePixelRatio())
            shown.fill(Qt.transparent)
            p = QtGui.QPainter(shown)
            try:
                p.setRenderHint(QtGui.QPainter.Antialiasing)
                p.setPen(QtGui.QPen(self._pill_colours()[1], 1.0))
                p.setBrush(body)
                p.drawRoundedRect(shape, corner, corner)
                outline = QtGui.QPainterPath()
                outline.addRoundedRect(shape, corner, corner)
                p.save()
                p.setClipPath(outline)
                leaving = max(0.0, 1.0 - t / 0.33)        # the pill's contents
                if leaving > 0.0:
                    p.setOpacity(leaving)
                    p.drawPixmap(morph["origin"], morph["old"])
                arriving = max(0.0, (t - 0.5) / 0.5)      # the card's contents
                if arriving > 0.0:
                    p.setOpacity(arriving)
                    p.drawPixmap(0, 0, frame)
                p.restore()
                outside = max(0.0, (t - 0.75) / 0.25)     # its glow, last of all
                if outside > 0.0:
                    # Only round the shape: inside it the card is drawn already.
                    around = QtGui.QPainterPath()
                    around.addRect(QtCore.QRectF(0, 0, frame.width(), frame.height()))
                    p.save()
                    p.setClipPath(around.subtracted(outline))
                    p.setOpacity(outside)
                    p.drawPixmap(0, 0, frame)
                    p.restore()
            finally:
                p.end()
            if t >= 1.0:
                self._morph = None
        self._last_frame = frame
        self._last_origin = QtCore.QPointF(area.topLeft())   # where that picture sits
        self._last_shape = (QtCore.QRectF(target), radius) if target is not None else None
        return shown

    def _shape_fill(self, kind):
        """The fill colour of a small pill, or of a card."""
        if kind == "small":
            return QtGui.QColor(self._pill_colours()[0])
        return QtGui.QColor(popup_theme()["bg"])

    def _download_lines(self, note):
        """The download card's two lines of text, and whether it is stalled."""
        title, _dot, detail = note.partition("  \u00b7  ")
        stuck = "not moving" in note or "seems blocked" in note
        if title.startswith("Downloading"):
            title = "Downloading"                # the icon and the amount say the rest
        percent = "%d%%" % int(max(0.0, min(1.0, self.progress or 0.0)) * 100)
        if stuck:
            # Short, so it is no wider than the title above it (same wording
            # style as the pill's "Click: dictate" hints).
            words = "Click: help  \u00b7  Right-click: retry"
        elif detail:
            words = "%s  \u00b7  %s" % (detail, percent)
        else:
            words = "Starting\u2026"
        return title, words, stuck

    def _draw_download(self, p, card, font, accent, theme, note):
        """The download card: the app's icon, what is happening, how much has
        arrived, and a bar with a light that travels along it. If the download
        stops moving the bar turns amber and the light stops."""
        title, words, stuck = self._download_lines(note)
        left, right, top = card.left() + 18.0, card.right() - 18.0, card.top()
        if self._own_icon is None:
            try:
                ratio = self.devicePixelRatioF()
                side = max(1, int(round(30 * ratio)))
                picture = app_icon().pixmap(128, 128).scaled(
                    side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                picture.setDevicePixelRatio(ratio)
                self._own_icon = picture
            except Exception:
                self._own_icon = False
        # The icon and the two lines of text form one group, placed in the
        # middle of the card so neither side is left emptier than the other.
        font.setPixelSize(12)
        font.setWeight(QtGui.QFont.DemiBold)
        title_w = QtGui.QFontMetricsF(font).horizontalAdvance(title)
        font.setWeight(QtGui.QFont.Normal)
        # A stalled download's hint is what to do next: a size larger, in the
        # ordinary text colour (amber small print was hard to read; the amber
        # bar already says something is wrong).
        hint_size = 12 if stuck else 11
        font.setPixelSize(hint_size)
        words_w = QtGui.QFontMetricsF(font).horizontalAdvance(words)
        lead = 40.0 if self._own_icon else 0.0
        group = lead + max(title_w, words_w)
        x = card.center().x() - group / 2.0
        if self._own_icon:
            p.drawPixmap(QtCore.QRectF(x, top + 14, 30, 30), self._own_icon,
                         QtCore.QRectF(self._own_icon.rect()))
        text_left = x + lead
        font.setPixelSize(12)
        font.setWeight(QtGui.QFont.DemiBold)
        p.setFont(font)
        p.setPen(theme["text"])
        p.drawText(QtCore.QRectF(text_left, top + 11, title_w + 4, 20),
                   Qt.AlignLeft | Qt.AlignVCenter, title)
        font.setWeight(QtGui.QFont.Normal)
        font.setPixelSize(hint_size)
        p.setFont(font)
        p.setPen(theme["text"] if stuck else theme["muted"])
        p.drawText(QtCore.QRectF(text_left, top + 29, words_w + 4, 18),
                   Qt.AlignLeft | Qt.AlignVCenter, words)
        font.setPixelSize(12)
        p.setFont(font)

        bar = QtCore.QRectF(left, top + 57.0, right - left, 6.0)
        p.setPen(Qt.NoPen)
        p.setBrush(theme["quiet"])
        p.drawRoundedRect(bar, 3, 3)
        done = max(6.0, max(0.0, min(1.0, self.progress)) * bar.width())
        fill = QtCore.QRectF(bar.left(), bar.top(), done, 6.0)
        colour = QtGui.QColor(theme["warning"]) if stuck else QtGui.QColor(accent)
        p.setBrush(colour)
        p.drawRoundedRect(fill, 3, 3)
        if not stuck:
            # A soft light sweeping along the filled part: it is alive, and moving.
            bright = QtGui.QColor(int(colour.red() + (255 - colour.red()) * 0.55),
                                  int(colour.green() + (255 - colour.green()) * 0.55),
                                  int(colour.blue() + (255 - colour.blue()) * 0.55))
            sweep = (time.monotonic() * 90.0) % (bar.width() + 60.0) - 60.0
            p.save()
            p.setClipRect(fill)
            glint = QtGui.QLinearGradient(QtCore.QPointF(bar.left() + sweep, 0),
                                          QtCore.QPointF(bar.left() + sweep + 60.0, 0))
            clear = QtGui.QColor(bright)
            clear.setAlpha(0)
            glint.setColorAt(0.0, clear)
            glint.setColorAt(0.5, bright)
            glint.setColorAt(1.0, clear)
            p.setBrush(QtGui.QBrush(glint))
            p.drawRoundedRect(fill, 3, 3)
            p.restore()

    def _draw_wave(self, p, x1, x2, y, accent, quiet):
        """The sound wave: one flowing line. Your voice sets how tall it is,
        loudest and newest in the middle, and it settles to a faint ripple
        when you are quiet. It always fits its row: the ends meet the centre
        line and the peaks stop short of the row's edges."""
        span = x2 - x1
        if span < 20.0:
            return
        drift = 0.0 if self.previewing else time.monotonic() * 5.0
        steps = max(8, int(span / 2.0))
        reach = (N_DOTS - 1) * 0.5               # how far back in time the ends show
        # Loudness jumps about from moment to moment; averaged over its
        # neighbours it makes a smooth swell instead of a jagged line.
        raw = [self.levels[-1 - j] for j in range(N_DOTS)]
        soft = []
        for j in range(N_DOTS):
            near = raw[max(0, j - 3):j + 4]
            soft.append(sum(near) / len(near))
        path = QtGui.QPainterPath()
        for i in range(steps + 1):
            u = i / float(steps)
            back = abs(u - 0.5) * 2.0 * reach
            k = int(back)
            a = soft[min(k, N_DOTS - 1)]
            b = soft[min(k + 1, N_DOTS - 1)]
            level = max(0.0, min(1.0, a + (b - a) * (back - k)))
            taper = math.sin(math.pi * u) ** 0.6             # nothing at either end
            height = (0.8 + 8.2 * min(1.0, level * 1.25)) * taper   # at most 9 px each way
            point = QtCore.QPointF(x1 + u * span,
                                   y + math.sin(u * span * 0.16 - drift) * height)
            if i == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        speaking = max(self.levels[-1 - j] for j in range(6)) > 0.05
        pen = QtGui.QPen(accent if speaking else quiet, 1.8)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setPen(Qt.NoPen)

    def _draw(self, p):
        """Everything in the pop-up, at full strength, onto the given painter."""
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)    # no jagged icons
        fade = 1.0
        p.setOpacity(fade)
        g = self.GLOW
        base = self.height() - g - self._below()  # the pill sits on this line
        note = self._note_text()
        warning = "" if self.previewing else self.warning
        third = LIVE_PREVIEW or bool(self.previewing and self.preview_words) or bool(warning)
        drop = 28                             # one top row: icon, wave and timer side
                                              # by side (there is no middle row)
        lines = LIVE_LINES if (LIVE_PREVIEW and not self.previewing) else 1
        if third and lines > 1 and not warning:
            # How many rows the words need right now: the card starts with one
            # and grows a row at a time, up to the number chosen in Settings.
            needed = float(max(1, min(lines, len(self.live_lines))))
        else:
            needed = 1.0
        self.rows += (needed - self.rows) * 0.16             # eased growth
        if abs(needed - self.rows) < 0.02:
            self.rows = needed

        if note is not None:            # small pill that grows to fit its text
            note_font = ui_font()
            note_font.setPixelSize(12)
            wanted = QtGui.QFontMetricsF(note_font).horizontalAdvance(note) + 58.0
            width = max(80.0, min(380.0, wanted))                # hugs short text
            height, radius = float(self.NOTE_H), self.NOTE_H / 2.0
            # Every small pill (idle, "Ready", notices) is one height and one
            # look, so moving from one to the next never jumps in size.
            keys = self._hotkey_keys(note)
            width = self._pill_width(keys, "" if keys else note)
            height = self._pill_height()
            radius = height / 2.0
            if self.progress is not None and self.mode == "message":
                # The download card: just wide enough for its text, with the
                # same margin on every side.
                bold = ui_font()
                bold.setPixelSize(12)
                bold.setWeight(QtGui.QFont.DemiBold)
                small = ui_font()
                small.setPixelSize(11)
                title, words, _stuck = self._download_lines(note)
                widest = max(QtGui.QFontMetricsF(bold).horizontalAdvance(title),
                             QtGui.QFontMetricsF(small).horizontalAdvance(words))
                width, height, radius = 18.0 + 40.0 + widest + 18.0, 80.0, 18.0
        elif self.mode == "welcome":
            width, height, radius = float(self.WELCOME_W), float(self.WELCOME_H), 18.0
        else:
            width = float(self.WIDE_W if (third and lines > 1 and WIDE_CARD)
                          else self.CARD_W)
            if CARD_WIDTH:                       # dragged to your own width
                width = float(max(self.CARD_W, min(self.WIN_W, CARD_WIDTH)))
            width = self._fit_width(width)       # room for the app's name in full
            if warning:                          # a microphone warning, never cut short
                said = ui_font()
                said.setPixelSize(TEXT_SIZE)
                width = max(width, min(float(self.WIN_W),
                                       QtGui.QFontMetricsF(said).horizontalAdvance(warning)
                                       + 34.0))
            # What sits on the top row, left to right: the app's icon (or a
            # dot), its name if wanted, the wave, the timer.
            one_row = not third                  # no words under it: a pill
            hug = one_row and not SHOW_WAVEFORM  # nothing stretchy: fit the contents
            top_font = ui_font()
            top_font.setPixelSize(12)
            top_font.setWeight(QtGui.QFont.DemiBold)
            top_metrics = QtGui.QFontMetricsF(top_font)
            icon_w = 20.0 if self.app_icon is not None else 10.0
            label = self.app_name
            if not label and self.app_icon is None and not SHOW_WAVEFORM:
                label = "Dictating"              # otherwise the row would be just a dot
            name_room = (width - 28.0 - icon_w - 8.0
                         - (40.0 if SHOW_TIMER else 0.0)
                         - (44.0 if SHOW_WAVEFORM else 0.0))
            label = top_metrics.elidedText(label, Qt.ElideRight, max(20.0, name_room)) \
                if label else ""
            name_w = top_metrics.horizontalAdvance(label) if label else 0.0
            parts_w = (icon_w + ((8.0 + name_w) if label else 0.0)
                       + (40.0 if SHOW_TIMER else 0.0))
            if hug:
                width = min(width, max(64.0, parts_w + 28.0))
            height = float(self.CARD_H - drop
                           + ((8 + self.LINE_H * self.rows) if third else 0))
            radius = 18.0
            if one_row:
                # One row only: a proper pill, the same shape and height as the
                # resting pill, not a short card with card-sized corners.
                height = float(self.NOTE_H)
                radius = height / 2.0
        top = base - height                      # small pills, welcome, download card
        if note is None and self.mode != "welcome":
            # A dictation card opens centred on the pill: it grows out evenly
            # from the pill's middle. Never into the taskbar or off the screen,
            # though: then it sits just high enough to stay clear.
            top = base - self.NOTE_H / 2.0 - height / 2.0
        card = QtCore.QRectF((self.width() - width) / 2.0, top, width, height)
        if self._dragging:
            card.translate(self._clamp_shift)    # as it was when the drag began
        else:
            # Never into the taskbar or off the screen: a card opened from a
            # pill near an edge or a corner moves just far enough to fit.
            # (While dragging it keeps that shift: the drag itself stops at
            # the edges, and dropping it changes nothing for one frame.)
            centre = self.mapToGlobal(QtCore.QPoint(self.width() // 2, int(base)))
            screen = (QtGui.QGuiApplication.screenAt(centre) or self.screen()
                      or QtGui.QGuiApplication.primaryScreen())
            if screen is not None:
                room, whole = screen.availableGeometry(), screen.geometry()
                gaps = None
                if note is None and self.mode != "welcome":
                    # A dictation card keeps a little more room from the
                    # screen's real edges, so its glow has space to fade out
                    # instead of meeting the edge, and from the taskbar, so
                    # its glow doesn't touch the icons there (owner).
                    gaps = tuple(self.CARD_EDGE_GAP if at_edge else self.TASKBAR_GAP
                                 for at_edge in (
                        room.left() == whole.left(), room.top() == whole.top(),
                        room.right() == whole.right(), room.bottom() == whole.bottom()))
                shift = self._inside(room, card.translated(self.x(), self.y()), gaps)
                card.translate(shift)
                self._clamp_shift = shift
                self._card_screen = screen
        card.translate(self._ready_slide(card, note, width, height))
        # On whole screen pixels: a card at half a pixel smears every letter
        # in it over two rows of pixels, and the text looks slightly blurred.
        ratio = max(1.0, float(self.devicePixelRatioF()))
        card.moveTo(round(card.left() * ratio) / ratio, round(card.top() * ratio) / ratio)
        self._card = (card.left(), card.top(), width, height)
        self._card_radius = radius
        cy = card.center().y()
        cx = card.center().x()
        accent = QtGui.QColor(ACCENT)
        theme = popup_theme()
        if is_light(POPUP_BG):
            accent = accent_on_light(accent)     # keeps the bars vivid on a pale card
        flat = note is not None and self.progress is None

        # Soft drop shadow under the card: lighter and tighter under the small
        # pill, where a full shadow reads as a heavy dark ring.
        shadow = QtGui.QColor(theme["shadow"])
        spread = 9
        if note is not None:
            shadow.setAlpha(int(shadow.alpha() * 0.45))
            spread = 5
        p.setPen(Qt.NoPen)
        p.setBrush(shadow)
        # (The card no longer gets this stepped shadow: a soft blurred shadow
        # or the glow does that job, and the steps showed as faint rings.)

        # A single pulse of the accent colour, used once when the app says
        # "Ready": it swells out from the pill and fades as it goes.
        if self.pulse_started is not None and self.mode == "message":
            delay = self.SLIDE_IN if self._slide is not None else 0.0
            t = (time.monotonic() - self.pulse_started - delay) / self.PULSE_SECONDS
            if t >= 1.0:
                self.pulse_started = None
            elif t >= 0.0:
                wave = glow_pixmap(width, height, radius, QtGui.QColor(ACCENT),
                                   float(self.GLOW), 15.0, self.devicePixelRatioF())
                if wave is not None:
                    eased = 1.0 - (1.0 - t) ** 3             # fast at first, then slowing
                    scale = 1.0 + 0.42 * eased
                    gw = (width + 2.0 * self.GLOW) * scale
                    gh = (height + 2.0 * self.GLOW) * (1.0 + 0.62 * eased)
                    p.setOpacity(fade * (1.0 - t) ** 1.6)
                    p.drawPixmap(QtCore.QRectF(card.center().x() - gw / 2.0,
                                               card.center().y() - gh / 2.0, gw, gh),
                                 wave, QtCore.QRectF(wave.rect()))
                    p.setOpacity(fade)

        if flat:
            # A small pill (idle, "Ready", a notice): flat and crisp, with no
            # shadow, glow or shading. ("Ready" still sends out its one pulse.)
            keys = self._hotkey_keys(note)
            # The mark is a status light: the accent colour when all is well,
            # amber while the model loads, red when something needs you.
            light = accent
            if self.status == "problem":
                light = QtGui.QColor(theme["danger"])
            elif self.status == "busy":
                light = QtGui.QColor(theme["warning"])
            self._draw_pill(p, keys, card, light, "" if keys else note)
            return

        if note is not None and self.progress is not None and self.mode == "message":
            halo = glow_pixmap(width, height, radius,
                               QtGui.QColor(theme["warning"]) if "not moving" in note
                               else QtGui.QColor(ACCENT), float(self.GLOW), 15.0,
                               self.devicePixelRatioF())
            if halo is not None:
                breath = 0.5 + 0.5 * math.sin(time.monotonic() * 1.6)
                p.setOpacity(fade * (0.30 + 0.16 * breath))
                p.drawPixmap(QtCore.QRectF(card.left() - self.GLOW, card.top() - self.GLOW,
                                           width + 2 * self.GLOW, height + 2 * self.GLOW),
                             halo, QtCore.QRectF(halo.rect()))
                p.setOpacity(fade)

        # Around the card: a soft glow and a ring, both in the accent colour and
        # both optional. They sit outside the card, on whatever is behind it, so
        # they use the accent as chosen (the deepened version made for pale cards
        # would vanish against a dark screen).
        live = self.mode == "recording" and not self.previewing
        if live:
            voice = max(0.0, min(1.0, self.levels[-1]))
            self.ring += (voice - self.ring) * 0.19          # eased, so it doesn't flicker
            self.spin = (self.spin + 1.5 + 3.5 * self.ring) % 360.0
        else:
            self.ring = 0.0                      # still cards stay plain
        vivid = glow_base()
        if GLOW_FROM_APP and self.app_colour is not None and self.mode == "recording":
            vivid = QtGui.QColor(self.app_colour)    # glow in the app icon's own colour
        amount = glow_amount() if CARD_STYLE == "glow" else 0.0
        if CARD_STYLE != "glow":
            # A soft, neutral shadow under the card, the way the system's own
            # pop-ups float: dark and wide, a little lower than the card, never
            # tinted (a coloured halo looks muddy over some apps).
            shade = glow_pixmap(width, height, radius, QtGui.QColor(0, 0, 0),
                                float(self.GLOW), 16.0, self.devicePixelRatioF())
            if shade is not None:
                p.setOpacity(fade * (0.22 if is_light(POPUP_BG) else 0.42))
                p.drawPixmap(QtCore.QRectF(card.left() - self.GLOW,
                                           card.top() - self.GLOW + 5.0,
                                           width + 2 * self.GLOW, height + 2 * self.GLOW),
                             shade, QtCore.QRectF(shade.rect()))
                p.setOpacity(fade)

        # The glow belongs to dictating: it is there while you are talking to
        # the app (and in previews from Settings), never on the idle pill, the
        # notices or the welcome card.
        if amount > 0.0 and (live or self.previewing):
            breath = 0.5 + 0.5 * math.sin(self.phase * 0.07) if live else 0.5
            strength = ((0.55 + 0.10 * breath + 0.35 * self.ring) if live else 0.6) * amount
            spread = 1.0 + (0.012 * breath + 0.05 * self.ring if live else 0.0)
            # Near a screen edge the whole glow draws in, evenly on every
            # side, to the room there is: smooth all round, never cut off by
            # the edge or squeezed on one side.
            margin = float(self.GLOW)
            screen = (self._card_screen or self.screen()
                      or QtGui.QGuiApplication.primaryScreen())
            if self._dragging and self._glow_margin is not None:
                margin = self._glow_margin       # steady while dragging (no rebuilds)
            elif screen is not None:
                whole = QtCore.QRectF(screen.geometry()).translated(-self.x(), -self.y())
                space = min(card.left() - whole.left(), card.top() - whole.top(),
                            whole.right() - card.right(), whole.bottom() - card.bottom())
                # In steps of 4 px, so the picture is made a few times, not
                # once for every pixel the card moves.
                margin = float(max(8, min(self.GLOW, (int(space) - 2) // 4 * 4)))
            self._glow_margin = margin
            glow = glow_pixmap(width, height, radius, vivid, margin, 15.0 * margin / self.GLOW,
                               self.devicePixelRatioF())
            if glow is not None:
                # One smooth picture of the glow, faded in and out and nudged
                # outward: in silence it breathes slowly, your voice pushes it.
                # The swell is a set number of pixels, the same as on the
                # standard card, however wide the card has been dragged. (As a
                # share of the card's size it ballooned on a wide one.)
                gw = width + 2 * margin + (self.CARD_W + 2 * margin) * (spread - 1.0)
                gh = height + 2 * margin + (100.0 + 2 * margin) * (spread - 1.0)
                p.setOpacity(fade * min(1.0, strength * 1.15))
                outer = QtCore.QRectF(cx - gw / 2.0, cy - gh / 2.0, gw, gh)
                p.drawPixmap(outer, glow, QtCore.QRectF(glow.rect()))
                if live and self.ring > 0.06 and animations_on():
                    # Your voice brings in the partner shade over the app's colour.
                    partner = glow_pixmap(width, height, radius, partner_colour(vivid),
                                          margin, 15.0 * margin / self.GLOW,
                                          self.devicePixelRatioF())
                    if partner is not None:
                        p.setOpacity(fade * min(1.0, strength * 1.15) * min(0.45, self.ring * 0.7))
                        p.drawPixmap(outer, partner, QtCore.QRectF(partner.rect()))
                p.setOpacity(fade)
            else:
                # Fallback: the same glow built from many faint outlines.
                reach = 24.0 + 3.0 * breath + 7.0 * self.ring
                p.setBrush(Qt.NoBrush)
                layer = int(reach)
                while layer > 0:
                    halo = QtGui.QColor(vivid)
                    halo.setAlpha(min(255, int(
                        58 * strength * (1.0 - layer / (reach + 1.0)) ** 2.6)))
                    p.setPen(QtGui.QPen(halo, layer * 2.0))
                    p.drawRoundedRect(card, radius, radius)
                    layer -= 1

        if ACCENT_OUTLINE:
            # The ring sits just outside the card and follows its shape exactly.
            # It swells with your voice; optionally a brighter band of light
            # travels round it.
            thickness = 1.4 + 1.6 * self.ring
            if OUTLINE_LIGHT:
                sweep = QtGui.QConicalGradient(QtCore.QPointF(cx, cy), -self.spin)
                # The travelling light is the same colour, a shade lighter. (Much
                # lighter and it turns white, which reads as a glint on a dark card.)
                bright = QtGui.QColor(int(vivid.red() + (255 - vivid.red()) * 0.22),
                                      int(vivid.green() + (255 - vivid.green()) * 0.22),
                                      int(vivid.blue() + (255 - vivid.blue()) * 0.22))
                for position, colour in ((0.00, bright), (0.14, vivid), (0.86, vivid),
                                         (1.00, bright)):
                    sweep.setColorAt(position, colour)
                pen = QtGui.QPen(QtGui.QBrush(sweep), thickness)
            else:
                pen = QtGui.QPen(vivid, thickness)
            grow = thickness / 2.0
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(card.adjusted(-grow, -grow, grow, grow),
                              radius + grow, radius + grow)
            p.setPen(Qt.NoPen)

        # The card itself: a touch lighter at the top than the bottom, with a
        # faint highlight along its upper edge, so it reads as a raised surface
        # and not a flat block.
        top_colour = QtGui.QColor(theme["bg"]).lighter(124 if not is_light(POPUP_BG) else 101)
        top_colour.setAlpha(theme["bg"].alpha())
        surface = QtGui.QLinearGradient(QtCore.QPointF(0, card.top()),
                                        QtCore.QPointF(0, card.bottom()))
        surface.setColorAt(0.0, top_colour)
        surface.setColorAt(1.0, theme["bg"])
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QBrush(surface))
        p.drawRoundedRect(card, radius, radius)          # the surface, drawn once
        p.setBrush(Qt.NoBrush)

        # Its edge, drawn last so nothing covers part of it (a half-hidden edge
        # is what makes rounded corners look jagged). While the glow is showing,
        # the edge is a fine line in the glow's colour; otherwise a quiet neutral.
        if not ACCENT_OUTLINE:
            if amount > 0.0 and (live or self.previewing):
                edge = QtGui.QColor(vivid)
                edge.setAlpha(125)
            else:
                # A bezel: the one edge line catches light at the top and falls
                # away toward the bottom, like the rim of a raised button. (It is
                # the same single line, not an extra one inside it.)
                dark = not is_light(POPUP_BG)
                tone = (255, 255, 255) if dark else (0, 0, 0)
                rim = QtGui.QLinearGradient(QtCore.QPointF(0, card.top()),
                                            QtCore.QPointF(0, card.bottom()))
                for position, alpha in (((0.0, 78), (0.45, 30), (1.0, 14)) if dark
                                        else ((0.0, 14), (0.55, 24), (1.0, 40))):
                    rim.setColorAt(position, QtGui.QColor(*tone, alpha))
                edge = QtGui.QBrush(rim)
                if CARD_STYLE == "accent" and (live or self.previewing):
                    edge = QtGui.QColor(glow_base())   # your accent, never the app's
                    edge.setAlpha(170)
            if True:
                # One whole screen pixel or two, lined up with the pixels, so
                # the edge is crisp rather than a soft grey smear.
                ratio = self.devicePixelRatioF() or 1.0
                line = max(1, int(round(ratio))) / ratio
                snap = lambda value: round(value * ratio) / ratio
                # Drawn as a filled ring (the card's shape less the same shape
                # one line smaller): its curves come out smoother than a thin
                # drawn line, which shows steps at the corners at 100%.
                outer_box = QtCore.QRectF(snap(card.left()), snap(card.top()),
                                          snap(card.width()), snap(card.height()))
                outer = QtGui.QPainterPath()
                outer.addRoundedRect(outer_box, radius, radius)
                inner = QtGui.QPainterPath()
                inner.addRoundedRect(outer_box.adjusted(line, line, -line, -line),
                                     radius - line, radius - line)
                p.setPen(Qt.NoPen)
                p.fillPath(outer.subtracted(inner), QtGui.QBrush(edge))
        p.setPen(Qt.NoPen)

        font = ui_font()
        font.setPixelSize(12)
        p.setFont(font)

        still = self.mode == "idle" or self.previewing
        pulse = 1.0 if still else 0.5 + 0.5 * math.sin(self.phase * 0.16)

        def breathing_dot(centre, size):
            halo = QtGui.QColor(accent)
            halo.setAlpha(int(70 * (1.0 - pulse)))
            p.setPen(Qt.NoPen)
            p.setBrush(halo)
            p.drawEllipse(centre, size + 1.0 + size * pulse, size + 1.0 + size * pulse)
            p.setBrush(accent)
            p.drawEllipse(centre, size, size)

        if note is not None and self.progress is not None and self.mode == "message":
            self._draw_download(p, card, font, accent, theme, note)
            return

        if note is not None:
            metrics = QtGui.QFontMetricsF(font)
            text = metrics.elidedText(note, Qt.ElideRight, width - 52)
            text_w = metrics.horizontalAdvance(text)
            start = cx - (text_w + 16) / 2.0
            breathing_dot(QtCore.QPointF(start + 4, cy), 4.0)
            p.setPen(theme["text"])
            p.drawText(QtCore.QRectF(start + 16, card.top(), text_w + 4, height),
                       Qt.AlignLeft | Qt.AlignVCenter, text)
            return

        if self.mode == "welcome":
            self._paint_welcome(p, card, font, accent, breathing_dot)
            return

        cancel_view = (self.mode == "recording" and self.hover and not self.previewing
                       and not self._dragging and self.fade_target > 0.0
                       and self.hover_armed and not self._resizing
                       and not self._edge_hover)
        if cancel_view:
            p.setOpacity(fade * 0.2)

        # The top row, left to right: the app being dictated into (its icon,
        # with a breathing dot on the corner; with no icon the dot stands
        # alone), its name if wanted, the wave, and the timer on the right.
        row1 = cy if one_row else card.top() + 24
        font.setWeight(QtGui.QFont.DemiBold)
        p.setFont(font)
        x = (cx - parts_w / 2.0) if hug else card.left() + 14.0
        if hug and not label and self.app_icon is not None and not SHOW_TIMER:
            x -= 1.5                             # the corner dot hangs off the right:
                                                 # centre the pair, not the icon alone
        end = (cx + parts_w / 2.0) if hug else card.right() - 14.0
        if self.app_icon is not None:
            p.drawPixmap(QtCore.QRectF(x, row1 - 10, 20, 20), self.app_icon,
                         QtCore.QRectF(self.app_icon.rect()))     # already the right size
            breathing_dot(QtCore.QPointF(x + 19, row1 + 8), 3.0)
        else:
            breathing_dot(QtCore.QPointF(x + 5, row1), 4.0)
        x += icon_w
        if label:
            p.setPen(theme["text"])
            p.drawText(QtCore.QRectF(x + 8, row1 - 11, name_w + 4, 22),
                       Qt.AlignLeft | Qt.AlignVCenter, label)
            x += 8.0 + name_w
        font.setWeight(QtGui.QFont.Normal)
        p.setFont(font)
        seconds = 0 if self.previewing else int(max(0.0, time.monotonic() - self.started))
        if SHOW_TIMER:
            p.setPen(theme["muted"])
            p.drawText(QtCore.QRectF(end - 36, row1 - 11, 36, 22),
                       Qt.AlignRight | Qt.AlignVCenter, "%d:%02d" % divmod(seconds, 60))
            end -= 40.0
        if SHOW_WAVEFORM:
            # The wave fills the space between the icon and the timer: short
            # on the compact card, longer as the card is dragged wider.
            self._draw_wave(p, x + 12.0, end - 2.0, row1, accent, theme["quiet"])

        # Below: the words heard so far.        # Below: the words heard so far. One line shows the latest words; with
        # more lines the text wraps and the most recent lines are kept.
        if third:
            font.setPixelSize(TEXT_SIZE)
            p.setFont(font)
            metrics = QtGui.QFontMetricsF(font)
            room = width - 32
            if warning:
                words, lines = warning, 1
                colour = theme["danger" if warning == self.NO_SOUND else "warning"]
            elif self.previewing:
                words = self.preview_words or "Your words appear here"
                colour = theme["muted"]
            elif self.live_text:
                words, colour = self.live_text, theme["text2"]
            else:
                words, colour = "Listening\u2026", theme["faint"]
            p.setPen(colour)
            # When the line changes between your words and a notice ("No text
            # box selected"), the new one fades in over a fifth of a second
            # instead of snapping in.
            key = warning or "words"
            now = time.monotonic()
            if key != self._line_key:
                self._line_key, self._line_t0 = key, now
            appear = min(1.0, (now - self._line_t0) / 0.2) if animations_on() else 1.0
            p.save()
            p.setOpacity(p.opacity() * (1.0 - (1.0 - appear) ** 2))
            if lines == 1:
                side = Qt.ElideRight if warning else Qt.ElideLeft
                p.drawText(QtCore.QRectF(card.left() + 16, card.top() + 70 - drop, room,
                                         self.LINE_H + 4),
                           Qt.AlignCenter, metrics.elidedText(words, side, room))
            elif not self.live_lines:
                p.drawText(QtCore.QRectF(card.left() + 16, card.top() + 70 - drop, room,
                                         self.LINE_H + 4), Qt.AlignCenter, words)
            else:
                rows = max(1, min(lines, int(self.rows + 0.999)))
                line_h = self.LINE_H
                self.row_slide *= 0.80              # rows glide up to make room
                if self.row_slide < 0.3:
                    self.row_slide = 0.0
                count = len(self.live_lines)
                extra = 1 if (self.row_slide > 0.0 and count > rows) else 0
                first = max(0, count - rows - extra)
                top = card.top() + 70 - drop + self.row_slide - extra * line_h
                p.save()                         # rows sliding in or out stay in their area
                p.setClipRect(QtCore.QRectF(card.left(), card.top() + 68 - drop,
                                            card.width(),
                                            max(0.0, card.bottom() - card.top() - 68 + drop)))
                for i in range(first, count):
                    line = self.live_lines[i]
                    text = " ".join(line)
                    real = metrics.horizontalAdvance(text)
                    drawn = self.line_w[i]
                    if drawn is None or abs(real - drawn) < 0.4:
                        drawn = real
                    else:                        # centred, but it glides rather than hops
                        drawn += (real - drawn) * 0.22
                    self.line_w[i] = drawn
                    x = card.left() + 16 if LIVE_ALIGN == "left" else cx - drawn / 2.0
                    y = top + (i - first) * line_h
                    box = QtCore.QRectF(x, y, real + 8, line_h + 4)
                    side = Qt.AlignLeft | Qt.AlignVCenter
                    if extra and i == first:     # the row leaving: fades as it slides
                        leaving = QtGui.QColor(colour)
                        leaving.setAlpha(int(leaving.alpha()
                                             * max(0.0, self.row_slide / line_h) ** 2))
                        p.setPen(leaving)
                        p.drawText(box, side, text)
                    elif i == count - 1 and self.word_fade < 1.0:
                        head = " ".join(line[:-1])
                        if head:
                            p.setPen(colour)
                            p.drawText(box, side, head)
                        soft = QtGui.QColor(colour)
                        soft.setAlpha(int(soft.alpha() * self.word_fade))
                        p.setPen(soft)
                        shift = metrics.horizontalAdvance(head + " ") if head else 0.0
                        p.drawText(QtCore.QRectF(x + shift, y, real + 8, line_h + 4),
                                   side, line[-1])
                    else:
                        p.setPen(colour)
                        p.drawText(box, side, text)
                p.restore()
            font.setPixelSize(12)
            p.setFont(font)

            p.restore()
        if cancel_view:
            p.setOpacity(fade)
            font.setWeight(QtGui.QFont.DemiBold)
            p.setFont(font)
            p.setPen(theme["text"])
            hint = "Click to finish" if self.click_started else "Click to cancel"
            if card.width() < 110:
                hint = "Finish" if self.click_started else "Cancel"
            p.drawText(card, Qt.AlignCenter, hint)


BACKGROUND_CHOICES = [("Dark", "#16171B"), ("Light", "#F8F8FA")]

class PointerBadge(QtWidgets.QWidget):
    """A small "No text box" tag that follows the mouse pointer while typing
    as you speak waits for a text box (your eyes are often where the pointer
    is, not on the pop-up). Clicks pass straight through it, it never takes
    the keyboard, and the real pointer is left alone."""

    OFFSET = QtCore.QPoint(16, 4)                # right beside the pointer's tip

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                            | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.text = "No text box"
        font = ui_font()
        font.setPixelSize(12)
        font.setWeight(QtGui.QFont.DemiBold)
        self.font_ = font
        width = QtGui.QFontMetricsF(font).horizontalAdvance(self.text)
        self.resize(int(width + 41), 24)          # room for the leetsay logo
        self.timer = QtCore.QTimer(self)
        # Follows the pointer about 100 times a second while it shows (30 a
        # second looked jerky beside the pointer itself).
        self.timer.setInterval(10)
        self.timer.timeout.connect(self._follow)

    def appear(self):
        self.target = 1.0
        if not self.isVisible():
            self._follow()
            self.setWindowOpacity(0.0)
            self.show()
        self.timer.start()

    def vanish(self):
        self.target = 0.0                        # fades out, then hides itself
        if self.isVisible():
            self.timer.start()

    def _follow(self):
        # Fade towards showing or gone (about a tenth of a second), not a pop.
        shown = self.windowOpacity()
        target = getattr(self, "target", 1.0)
        if abs(shown - target) > 0.01:
            step = 0.1 if animations_on() else 1.0     # still about a tenth of a second
            shown = min(target, shown + step) if target > shown else max(target, shown - step)
            self.setWindowOpacity(shown)
        if target == 0.0 and shown <= 0.01:
            self.timer.stop()
            self.hide()
            return
        spot = QtGui.QCursor.pos() + self.OFFSET
        screen = QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
        if screen is not None:                   # never off the screen's edge
            room = screen.availableGeometry()
            spot.setX(min(spot.x(), room.right() - self.width()))
            spot.setY(min(spot.y(), room.bottom() - self.height()))
        if spot != self.pos():
            self.move(spot)

    def paintEvent(self, _event):
        theme = popup_theme()
        p = QtGui.QPainter(self)
        try:
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            box = QtCore.QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
            fill = QtGui.QColor(theme["bg"])
            fill.setAlpha(240)
            p.setPen(QtGui.QPen(QtGui.QColor(theme["warning"]), 1.0))
            p.setBrush(fill)
            p.drawRoundedRect(box, box.height() / 2.0, box.height() / 2.0)
            # leetsay's logo (its four 1337 bars) in amber, so it is clear
            # where the note comes from.
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(theme["warning"]))
            bar, gap, tallest = 2.4, 1.6, 11.0
            unit = tallest / float(max(ICON_BARS))
            cy = box.center().y()
            for i, units in enumerate(ICON_BARS):
                h = max(2.4, unit * units)
                p.drawRoundedRect(QtCore.QRectF(10.0 + i * (bar + gap), cy - h / 2.0, bar, h),
                                  bar / 2.0, bar / 2.0)
            p.setFont(self.font_)
            p.setPen(theme["text"])
            p.drawText(QtCore.QRectF(29, 0, self.width() - 33, self.height()),
                       Qt.AlignLeft | Qt.AlignVCenter, self.text)
        finally:
            p.end()


class EdgeStrip(QtWidgets.QWidget):
    """One edge of the screen glow: a thin see-through window along a screen
    edge. Clicks pass straight through it, and it can never take the
    keyboard.

    The light is built like real light: a thin bright core at the very
    edge, a tight glow behind it and a wide soft haze, rounding off smoothly
    in the corners. Its brightness is a picture made once per size; the
    colour is laid over it, and with "flowing colours" it drifts slowly round
    the screen (the strip is then redrawn while you dictate, about 30 times a
    second; otherwise it is painted once and only its opacity changes)."""

    def __init__(self, side):
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                            | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.side = side
        self.colour = QtGui.QColor(ACCENT)
        self.phase = 0.0                     # where the moving light is on its way round
        self.voice = []                      # recent voice levels (the bottom edge's ripples)
        self.alone = False                   # the only strip: the bottom edge on its own
        self.behind = False                  # sits behind the taskbar, showing through it
        self.span = (0.0, 1.0)               # this strip's share of the way round
        self._mask = None                    # (key, picture of the brightness)

    @staticmethod
    def light(distance, depth):
        """How bright the glow is at `distance` pixels in from the edge
        (numbers, or a numpy array), for a glow `depth` pixels deep: a thin
        hot core, a tight glow and a wide haze, each fading smoothly."""
        x = np.maximum(distance, 0.0)
        core = np.exp(-x / 1.6)
        glow = 0.55 * np.exp(-(x / np.maximum(2.0, 0.14 * depth)) ** 2)
        haze = 0.34 * np.exp(-(x / np.maximum(4.0, 0.42 * depth)) ** 1.6)
        fade = np.clip(1.0 - x / depth, 0.0, 1.0) ** 0.6     # gone by the far side
        return np.minimum(1.0, (core + glow + haze) * fade)

    def _brightness(self, w, h, ratio):
        """The strip's brightness as a picture (white, see-through), made at
        the screen's real pixels, with a whisper of noise so the soft fade
        shows no bands."""
        key = (w, h, self.side, round(ratio, 2), self.alone, SCREEN_GLOW_FOCUS, self.behind)
        if self._mask is not None and self._mask[0] == key:
            return self._mask[1]
        pw, ph = max(1, int(round(w * ratio))), max(1, int(round(h * ratio)))
        depth = (h if self.side in ("top", "bottom") else w) * ratio
        xs = np.arange(pw, dtype=np.float32) + 0.5
        ys = np.arange(ph, dtype=np.float32) + 0.5
        gx, gy = np.meshgrid(xs, ys)
        if self.alone:
            # One side on its own (no corners to share): distance from its edge,
            # and how far along it a point is (0 at one end, 1 at the other).
            dist = {"left": gx, "right": pw - gx, "top": gy, "bottom": ph - gy}[self.side]
            along = gy / ph if self.side in ("left", "right") else gx / pw
        elif self.side in ("left", "right"):
            dist = gx if self.side == "left" else pw - gx
        else:
            dist = gy if self.side == "top" else ph - gy
            # The corners (the top and bottom strips own them): the distance
            # to the nearest edge, softened, so the light bends round in a
            # smooth curve instead of meeting the other edge in a crease.
            # (Further along, it is just the distance to this edge.)
            soft = max(1.0, 0.12 * depth)
            dist = -soft * np.log(np.exp(-dist / soft) + np.exp(-gx / soft)
                                  + np.exp(-(pw - gx) / soft))
            dist = np.maximum(dist, 0.0)
        if self.alone:
            # On its own it carries more light, gathered in its two corners:
            # there it reaches the full depth, in the middle (where people
            # read, and the pop-up sits) only a slim band, faint.
            side = np.abs(2.0 * along - 1.0)             # 0 in the middle, 1 at the ends
            # "corners": fullest at the two ends; "middle": rising highest in
            # the centre, like the system itself listening.
            gather = side if SCREEN_GLOW_FOCUS == "corners" else 1.0 - side
            if SCREEN_GLOW_FOCUS == "even":
                gather = np.ones_like(side)              # one even line, end to end
            reach = depth * (0.38 + 0.62 * gather ** 1.4)
            rise = 0.85 * np.exp(-(np.maximum(dist, 0.0) / (0.45 * reach)) ** 1.3)
            alpha = np.minimum(1.0, self.light(dist, reach) * 2.0 + rise)
            if self.behind:
                # Behind the taskbar its frosted layer dims the light: give it
                # more, filling more of the taskbar's height.
                alpha = np.minimum(1.0, alpha * 1.7 + 0.3 * np.exp(-dist / (0.8 * reach)))
            alpha = alpha * (0.15 + 0.85 * gather ** 1.3)
        else:
            alpha = self.light(dist, depth)
        alpha = alpha + (np.random.default_rng(7).random(alpha.shape, dtype=np.float32)
                         - 0.5) / 255.0
        a = np.clip(alpha * 255.0, 0, 255).astype(np.uint8)
        pixels = np.empty((ph, pw, 4), dtype=np.uint8)
        pixels[..., 0] = a
        pixels[..., 1] = a
        pixels[..., 2] = a
        pixels[..., 3] = a                   # premultiplied white
        image = QtGui.QImage(np.ascontiguousarray(pixels).tobytes(), pw, ph, 4 * pw,
                             QtGui.QImage.Format_ARGB32_Premultiplied).copy()
        image.setDevicePixelRatio(ratio)
        self._mask = (key, image)
        return image

    def _tint(self, position, along):
        """The colour (and strength) at a point on the way round the screen
        (0..1); `along` is how far along this strip (0..1). With "moving
        light": a brighter band in a neighbouring shade travels round the
        edge, and the bottom edge ripples out from its middle with your voice."""
        base = QtGui.QColor(self.colour)
        if not SCREEN_GLOW_FLOW:
            return base
        hue, sat, val, _a = base.getHsvF()
        hue = max(0.0, hue)
        gap = abs(position - self.phase) % 1.0
        gap = min(gap, 1.0 - gap)                # round the screen, either way
        light = math.exp(-(gap / 0.07) ** 2)     # the travelling band
        shifted = partner_colour(base)            # a shade that goes with it
        colour = QtGui.QColor(int(base.red() + (shifted.red() - base.red()) * light),
                              int(base.green() + (shifted.green() - base.green()) * light),
                              int(base.blue() + (shifted.blue() - base.blue()) * light))
        strength = (0.8 + 0.2 * light) if self.alone else (0.55 + 0.45 * light)
        if self.alone and self.voice:
            # Your voice, rippling out from the middle (or in from the
            # corners): the newest sound there, older sounds further along.
            out = abs(along - 0.5) * 2.0
            if SCREEN_GLOW_FOCUS == "corners":
                out = 1.0 - out
            spot = min(len(self.voice) - 1, int(out * len(self.voice)))
            strength += 0.6 * self.voice[-1 - spot]
        colour.setAlphaF(min(1.0, strength))
        return colour

    def paintEvent(self, _event):
        w, h = self.width(), self.height()
        ratio = self.devicePixelRatioF() or 1.0
        mask = self._brightness(w, h, ratio)
        layer = QtGui.QImage(mask)
        q = QtGui.QPainter(layer)
        q.setCompositionMode(QtGui.QPainter.CompositionMode_SourceIn)
        along = (QtCore.QPointF(0, 0), QtCore.QPointF(w, 0)) \
            if self.side in ("top", "bottom") else (QtCore.QPointF(0, 0), QtCore.QPointF(0, h))
        tint = QtGui.QLinearGradient(*along)
        first, last = self.span
        for i in range(41):
            colour = self._tint(first + (last - first) * i / 40.0, i / 40.0)
            if self.behind:
                # Behind the taskbar's frosted layer: a lighter, brighter shade
                # of the same colour gets through it far better than a deep one.
                lift = 0.45
                colour = QtGui.QColor(int(colour.red() + (255 - colour.red()) * lift),
                                      int(colour.green() + (255 - colour.green()) * lift),
                                      int(colour.blue() + (255 - colour.blue()) * lift),
                                      colour.alpha())
            tint.setColorAt(i / 40.0, colour)
        q.fillRect(QtCore.QRectF(0, 0, w, h), QtGui.QBrush(tint))
        # The hot core is lighter, nearly white, right at the edge: that is
        # what makes it read as light rather than paint.
        q.setCompositionMode(QtGui.QPainter.CompositionMode_SourceAtop)
        if self.side == "top":
            start, stop = QtCore.QPointF(0, 0), QtCore.QPointF(0, 4)
        elif self.side == "bottom":
            start, stop = QtCore.QPointF(0, h), QtCore.QPointF(0, h - 4)
        elif self.side == "left":
            start, stop = QtCore.QPointF(0, 0), QtCore.QPointF(4, 0)
        else:
            start, stop = QtCore.QPointF(w, 0), QtCore.QPointF(w - 4, 0)
        hot = QtGui.QLinearGradient(start, stop)
        hot.setColorAt(0.0, QtGui.QColor(255, 255, 255, 105))
        hot.setColorAt(1.0, QtGui.QColor(255, 255, 255, 0))
        q.fillRect(QtCore.QRectF(0, 0, w, h), QtGui.QBrush(hot))
        if self.side in ("top", "bottom") and not self.alone:
            # The corners belong to this strip: the side edges' hot line runs
            # on into them, right up to the corner.
            for x0, x1 in ((0.0, 4.0), (float(w), w - 4.0)):
                hot = QtGui.QLinearGradient(QtCore.QPointF(x0, 0), QtCore.QPointF(x1, 0))
                hot.setColorAt(0.0, QtGui.QColor(255, 255, 255, 105))
                hot.setColorAt(1.0, QtGui.QColor(255, 255, 255, 0))
                q.fillRect(QtCore.QRectF(min(x0, x1), 0, 4.0, h), QtGui.QBrush(hot))
        q.end()
        p = QtGui.QPainter(self)
        p.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
        p.drawImage(QtCore.QPointF(0, 0), layer)
        p.end()


class ScreenGlow:
    """A glow round the edge of the whole screen while you dictate (optional,
    off by default). It breathes in when dictation starts, swells with your
    voice and fades out at the end."""

    def __init__(self):
        self.sets = {}                           # a screen's rectangle -> its four strips
        self.strips = []                         # every strip in use now
        self.fade = 0.0
        self.showing = False

    def start(self, colour, areas):
        """Show round `areas` (one screen's rectangle, or a list of them: the
        glow can be on several screens at once) in `colour`."""
        if isinstance(areas, QtCore.QRect):
            areas = [areas]
        keys = []
        for area in areas:
            key = (area.x(), area.y(), area.width(), area.height())
            if key in keys:
                continue
            keys.append(key)
            if key not in self.sets:             # made the first time a screen is used
                self.sets[key] = [EdgeStrip(side) for side in
                                  ("top", "bottom", "left", "right", "bottom")]
            self._place(self.sets[key], colour, area)
        for key, strips in self.sets.items():    # screens not chosen this time
            if key not in keys:
                for strip in strips:
                    strip.hide()
        self.strips = [strip for key in keys for strip in self.sets[key]]
        if not self.showing:                     # already up (Settings demo): no restart
            self.fade = 0.0
        self.showing = True

    @staticmethod
    def _move(strip, rect):
        """Move or resize a strip without a jump: hidden for that moment and
        redrawn at its new size before it shows again. (Windows would show
        the old picture stretched to the new size for an instant, so a
        change of size made the glow leap up and settle back.)"""
        if strip.geometry() == rect:
            return
        if strip.isVisible():
            strip.setWindowOpacity(0.0)
            strip.setGeometry(rect)
            strip.repaint()                      # the new picture, before it shows
        else:
            strip.setGeometry(rect)

    def _shown(self, strip):
        """How strongly a strip shows: the glow along the bottom on its own
        shows stronger, so the low glow still reads clearly."""
        return min(1.0, self.fade * (2.6 if strip.behind else 1.8 if strip.alone else 1.0))

    def _place(self, strips, colour, area):
        """Put one screen's four strips in place for the chosen style."""
        x, y, w, h = area.x(), area.y(), area.width(), area.height()
        d = int(SCREEN_GLOW_SIZE)
        if SCREEN_GLOW_EDGES in ("bottom", "top", "left", "right"):
            # One side on its own, fullest in its two corners (it reaches
            # about 1.8 x the edge size there, a slim band in the middle);
            # "Edge size" in Settings makes it bigger. It starts from the edge
            # of the usable screen, so a taskbar on that side doesn't hide it.
            # Slim and soft on every side, like the glow under the taskbar's
            # icons (about 18 px; "Edge size" in More glow options makes it bigger).
            depth = max(12, int(SCREEN_GLOW_SIZE * 0.32))
            room = QtCore.QRect(area)
            screen = QtGui.QGuiApplication.screenAt(area.center())
            if screen is not None and area.contains(screen.availableGeometry()):
                room = screen.availableGeometry()
            rx, ry, rw, rh = room.x(), room.y(), room.width(), room.height()
            # The bottom glow lives in the taskbar. With a see-through taskbar
            # (Windows' default) on the main screen it sits behind it, filling
            # its height, and shines through behind the icons: nothing is
            # covered.
            taskbar = (y + h) - (ry + rh)
            main = QtGui.QGuiApplication.primaryScreen()
            on_main = main is not None and main.geometry() == area
            behind = taskbar > 0 and on_main and taskbar_see_through()
            if behind:
                bottom_rect = QtCore.QRect(rx, y + h - taskbar, rw, taskbar)
            else:
                # Another screen's taskbar, or a solid one: a slim glow just
                # above it, so its icons and their marks are never covered.
                bottom_rect = QtCore.QRect(rx, ry + rh - depth, rw, depth)
            place = {"top": QtCore.QRect(rx, ry, rw, depth),
                     "bottom": bottom_rect,
                     "left": QtCore.QRect(rx, ry, depth, rh),
                     "right": QtCore.QRect(rx + rw - depth, ry, depth, rh)}[SCREEN_GLOW_EDGES]
            one = strips[("top", "bottom", "left", "right").index(SCREEN_GLOW_EDGES)]
            for strip in strips[:4]:
                strip.alone = strip is one
                strip.behind = strip is one and SCREEN_GLOW_EDGES == "bottom" and behind
                if strip is not one:
                    strip.hide()
            one.span = (0.0, 1.0)
            one.colour = QtGui.QColor(colour)
            self._move(one, place)
            one.setWindowOpacity(self._shown(one) if self.showing else 0.0)
            one.show()
            strips[4].hide()                     # (a line in front: tried, looked worse)
            if one.behind:
                tuck_behind_taskbar(one)
            one.update()
            return
        d = max(12, int(SCREEN_GLOW_SIZE * 0.32))   # the full border: slim as well
        strips[4].hide()                         # the taskbar's line: not for the border
        for strip in strips[:4]:
            strip.alone = False
            strip.behind = False
            strip.setWindowOpacity(self.fade if self.showing else 0.0)
        places = ((x, y, w, d), (x, y + h - d, w, d),
                  (x, y + d, d, h - 2 * d), (x + w - d, y + d, d, h - 2 * d))
        # Each strip's share of the way round the screen, clockwise from the
        # top left, so the colours flow on from one edge into the next.
        round_trip = 2.0 * (w + h)
        spans = ((0.0, w / round_trip), ((2.0 * w + h) / round_trip, (w + h) / round_trip),
                 (1.0 - d / round_trip, (2.0 * w + h + d) / round_trip),
                 ((w + d) / round_trip, (w + h - d) / round_trip))
        for strip, place, span in zip(strips, places, spans):
            strip.span = span
            strip.colour = QtGui.QColor(colour)
            self._move(strip, QtCore.QRect(*place))
            strip.setWindowOpacity(self._shown(strip) if self.showing else 0.0)
            strip.show()
            strip.update()

    def step(self, level, active, still=False):
        """Advance one frame. Returns False once it has faded away. still:
        hold steady (no moving light, no ripples, no swelling with your
        voice), e.g. while nothing can be typed ("No text box selected")."""
        if not self.showing:
            return False
        if still:
            level = 0.0
        target = ((0.68 + 0.32 * max(0.0, min(1.0, level)))
                  * SCREEN_GLOW_STRENGTH / 100.0) if active else 0.0
        self.fade += (target - self.fade) * (0.16 if target > self.fade else 0.10)
        if not active and self.fade < 0.02:
            self.stop()
            return False
        moving = SCREEN_GLOW_FLOW and animations_on() and not still
        if moving:
            # The light travels round clockwise, once in about 6.5 s, quicker
            # while you speak. Your voice is kept for a moment, for the bottom
            # edge's ripples (none while you are quiet).
            voice = max(0.0, min(1.0, level)) if active else 0.0
            self.phase = (getattr(self, "phase", 0.0) + 0.005 * (1.0 + 1.5 * voice)) % 1.0
            self.voice = (getattr(self, "voice", []) + [voice])[-20:]
        for strip in self.strips:
            if not strip.isVisible():
                continue
            if strip.behind:
                tuck_behind_taskbar(strip)       # the taskbar stays in front of it
            strip.setWindowOpacity(self._shown(strip))
            if moving:
                strip.phase = self.phase
                strip.voice = self.voice if strip.alone else []
                strip.update()
            elif still and strip.voice:
                strip.voice = []                 # held still: no ripple left frozen
                strip.update()
        if still:
            self.voice = []
        return True

    def stop(self):
        self.showing = False
        self.fade = 0.0
        for strips in self.sets.values():
            for strip in strips:
                strip.hide()


_GLOW_CACHE = {}
_GLOW_BROKEN = False


def glow_pixmap(width, height, radius, colour, margin, blur, dpr):
    """A soft glow in the shape of the card, made by really blurring that shape
    (smooth at any size, no visible steps). Made once per size and colour, then
    reused; returns None if this PC can't make it."""
    global _GLOW_BROKEN
    if _GLOW_BROKEN:
        return None
    key = (int(round(width)), int(round(height)), int(round(radius)), colour.name(),
           int(margin), int(blur), round(dpr, 2))
    if key in _GLOW_CACHE:
        return _GLOW_CACHE[key]
    try:
        w = int(round((width + 2 * margin) * dpr))
        h = int(round((height + 2 * margin) * dpr))
        shape = QtGui.QPixmap(w, h)
        shape.fill(Qt.transparent)
        painter = QtGui.QPainter(shape)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.scale(dpr, dpr)
        painter.setPen(Qt.NoPen)
        painter.setBrush(colour)
        spill = 4.0                              # a little larger than the card itself
        painter.drawRoundedRect(QtCore.QRectF(margin - spill, margin - spill,
                                              width + 2 * spill, height + 2 * spill),
                                radius + spill, radius + spill)
        painter.end()

        scene = QtWidgets.QGraphicsScene()
        item = QtWidgets.QGraphicsPixmapItem(shape)
        effect = QtWidgets.QGraphicsBlurEffect()
        effect.setBlurRadius(blur * dpr)
        effect.setBlurHints(QtWidgets.QGraphicsBlurEffect.QualityHint)
        item.setGraphicsEffect(effect)
        scene.addItem(item)
        image = QtGui.QImage(w, h, QtGui.QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QtGui.QPainter(image)
        scene.render(painter, QtCore.QRectF(0, 0, w, h), QtCore.QRectF(0, 0, w, h))
        painter.end()
        result = QtGui.QPixmap.fromImage(image)
        while len(_GLOW_CACHE) > 16:             # forget the oldest, keep the rest
            _GLOW_CACHE.pop(next(iter(_GLOW_CACHE)))
        _GLOW_CACHE[key] = result
        return result
    except Exception as exc:
        print("Smooth glow unavailable, using the simple one:", exc, file=sys.stderr)
        _GLOW_BROKEN = True
        return None


def icon_colour(pixmap):
    """The main colour of an app's icon, brightened into something that glows
    well. None if the icon is basically grey, black or white."""
    try:
        image = pixmap.toImage().scaled(24, 24, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        buckets = {}
        for y in range(image.height()):
            for x in range(image.width()):
                c = image.pixelColor(x, y)
                if c.alpha() < 140:
                    continue
                saturation, value = c.hsvSaturationF(), c.valueF()
                if saturation * saturation * value < 0.08:
                    continue                     # greys, whites, blacks don't count
                # The brightest, purest parts of the icon count most, so a shaded
                # logo is judged by its lit side, not its shadows.
                weight = saturation * value ** 3
                bucket = int(c.hsvHueF() * 12) % 12
                total = buckets.setdefault(bucket, [0.0, 0.0, 0.0, 0.0])
                total[0] += weight
                total[1] += c.red() * weight
                total[2] += c.green() * weight
                total[3] += c.blue() * weight
        if not buckets:
            return None
        weight, red, green, blue = max(buckets.values(), key=lambda t: t[0])
        if weight < 4.0:
            return None                          # too little colour to go on
        colour = QtGui.QColor(int(red / weight), int(green / weight), int(blue / weight))
        # A glow is see-through light over a dark screen, which dulls it (orange
        # turns brownish). So it is made at full brightness, a little less heavy,
        # and lifted slightly towards white: it then reads as the icon's colour.
        full = QtGui.QColor.fromHsvF(max(0.0, colour.hsvHueF()),
                                     min(0.88, max(0.55, colour.hsvSaturationF())), 1.0)
        lift = 0.16
        return QtGui.QColor(int(full.red() + (255 - full.red()) * lift),
                            int(full.green() + (255 - full.green()) * lift),
                            int(full.blue() + (255 - full.blue()) * lift))
    except Exception:
        return None


def partner_colour(colour):
    """A second shade that goes with a colour: its neighbour on the colour
    wheel, a little lighter (warm with warm, cool with cool, never a clash).
    Your voice brings it in, over the app's own colour."""
    base = QtGui.QColor(colour)
    hue, sat, _val, _a = base.getHsvF()
    near = QtGui.QColor.fromHsvF((max(0.0, hue) + 0.09) % 1.0, min(1.0, sat * 0.85), 1.0)
    lift = 0.14
    return QtGui.QColor(int(near.red() + (255 - near.red()) * lift),
                        int(near.green() + (255 - near.green()) * lift),
                        int(near.blue() + (255 - near.blue()) * lift))


def accent_on_light(accent):
    """A deeper, richer version of the accent colour for pale backgrounds
    (the same hue, so it still reads as the colour that was picked)."""
    hue, saturation, lightness, _alpha = accent.getHslF()
    if saturation > 0.25:                        # leave greys and white neutral
        saturation = min(1.0, saturation * 1.1 + 0.08)
    return QtGui.QColor.fromHslF(max(0.0, hue), saturation, min(lightness, 0.37))


_animations = {"checked": 0.0, "on": True}


def animations_on():
    """Windows' own "Animation effects" switch (Settings, Accessibility, Visual
    effects). Off: the pop-up changes shape at once instead of animating."""
    now = time.monotonic()
    if now - _animations["checked"] > 5.0:
        _animations["checked"] = now
        try:
            value = wintypes.BOOL(True)
            if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0):
                _animations["on"] = bool(value.value)      # SPI_GETCLIENTAREAANIMATION
        except Exception:
            _animations["on"] = True
    return _animations["on"]


def is_light(colour):
    """True if a colour is light enough to need dark text on it."""
    c = QtGui.QColor(colour)

    def linear(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    luminance = (0.2126 * linear(c.red()) + 0.7152 * linear(c.green())
                 + 0.0722 * linear(c.blue()))
    return luminance > 0.179             # the point where dark text reads better


def popup_theme():
    """Every colour the pop-up uses, worked out from its background colour."""
    if LEET_MODE:                                # green on black, all of it
        return {"bg": QtGui.QColor(5, 10, 6, 250), "border": QtGui.QColor(57, 255, 106, 60),
                "shadow": QtGui.QColor(0, 0, 0, 10),
                "text": QtGui.QColor(110, 255, 150), "text2": QtGui.QColor(70, 230, 110),
                "muted": QtGui.QColor(40, 170, 80), "faint": QtGui.QColor(30, 120, 60),
                "quiet": QtGui.QColor(57, 255, 106, 60),
                "warning": QtGui.QColor(255, 200, 70), "danger": QtGui.QColor(255, 90, 90),
                "chip": QtGui.QColor(57, 255, 106, 18),
                "chip_hot": QtGui.QColor(57, 255, 106, 40),
                "chip_border": QtGui.QColor(57, 255, 106, 70),
                "sheen": QtGui.QColor(57, 255, 106, 16)}
    bg = QtGui.QColor(POPUP_BG)
    if not bg.isValid():
        bg = QtGui.QColor("#16171B")
    if is_light(bg):
        # Solid (a see-through pale card picks up whatever is behind it and
        # looks grubby), a hairline edge, a fuller shadow, and soft blacks.
        bg.setAlpha(255)
        return {"bg": bg, "border": QtGui.QColor(0, 0, 0, 24),
                "shadow": QtGui.QColor(0, 0, 0, 17),
                "text": QtGui.QColor(29, 29, 31), "text2": QtGui.QColor(60, 60, 67),
                "muted": QtGui.QColor(110, 110, 118), "faint": QtGui.QColor(142, 142, 150),
                "quiet": QtGui.QColor(0, 0, 0, 34), "warning": QtGui.QColor(181, 95, 0),
                "danger": QtGui.QColor(200, 40, 40),
                "chip": QtGui.QColor(0, 0, 0, 10), "chip_hot": QtGui.QColor(0, 0, 0, 24),
                "chip_border": QtGui.QColor(0, 0, 0, 40),
                "sheen": QtGui.QColor(255, 255, 255, 150)}
    bg.setAlpha(247)
    return {"bg": bg, "border": QtGui.QColor(255, 255, 255, 28),
            "shadow": QtGui.QColor(0, 0, 0, 10),
            "text": QtGui.QColor(240, 240, 244), "text2": QtGui.QColor(214, 216, 224),
            "muted": QtGui.QColor(170, 172, 182), "faint": QtGui.QColor(120, 122, 132),
            "quiet": QtGui.QColor(255, 255, 255, 60), "warning": QtGui.QColor(255, 181, 71),
            "danger": QtGui.QColor(255, 107, 107),
            "chip": QtGui.QColor(255, 255, 255, 14), "chip_hot": QtGui.QColor(255, 255, 255, 32),
            "chip_border": QtGui.QColor(255, 255, 255, 46),
            "sheen": QtGui.QColor(255, 255, 255, 16)}


def _plain(word):
    return "".join(ch for ch in word.lower() if ch.isalnum())


def overlap_start(shown, target):
    """Where in `shown` (words on the card) the model's new reading `target`
    begins. Usually 0. Once a long dictation outgrows what the preview re-reads,
    the reading starts later, and this finds where by lining the words up
    (ignoring capitals and punctuation, which the model keeps changing)."""
    if not shown or not target:
        return 0
    a, b = [_plain(w) for w in shown], [_plain(w) for w in target]
    best, best_hits = None, 0
    for start in range(len(a)):
        length = min(len(a) - start, len(b))
        hits = sum(1 for i in range(length) if a[start + i] == b[i])
        # On a tie the later start wins: a repeated phrase ("I said yes, I said
        # yes") lines up with its last repeat, not its first, so the words
        # already on the card are not cut off.
        if (hits > best_hits or (hits == best_hits and hits > 0)) and hits * 2 >= length:
            best, best_hits = start, hits
    if best is None:                             # nothing lines up: swap from the end
        return max(0, len(a) - len(b))
    return best


def wrap_words(text, measure, room):
    """Break text into lines no wider than `room` (measure gives a width)."""
    lines, current = [], ""
    for word in text.split():
        trial = word if not current else current + " " + word
        if current and measure(trial) > room:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines or [""]


def _paint_welcome(self, p, card, font, accent, breathing_dot):
    """First-run card: shows the hotkey (click it to pick another) and a
    Get started button. Only the drawn controls respond to clicks."""
    cx = card.center().x()
    theme = popup_theme()
    white = theme["text"]
    grey = theme["muted"]
    over = None if self._dragging else self._target_at(self._mouse)
    hits = {}

    def chip(rect, text, name, selected=False):
        x, y, w, h = rect
        hits[name] = rect
        hot = over == name
        p.setPen(QtGui.QPen(accent if (selected or hot) else theme["chip_border"], 1.0))
        p.setBrush(theme["chip_hot"] if hot else theme["chip"])
        p.drawRoundedRect(QtCore.QRectF(x, y, w, h), h / 2.0, h / 2.0)
        p.setPen(white)
        p.drawText(QtCore.QRectF(x, y, w, h), Qt.AlignCenter, text)

    font.setPixelSize(13)
    font.setWeight(QtGui.QFont.DemiBold)
    p.setFont(font)
    p.setPen(white)
    if self.picking:
        title = "Choose a hotkey"
    else:
        title = "Dictation is ready" if self.welcome_ready else "Setting up dictation"
    p.drawText(QtCore.QRectF(card.left(), card.top() + 8, card.width(), 20),
               Qt.AlignCenter, title)
    font.setPixelSize(12)
    font.setWeight(QtGui.QFont.Normal)
    p.setFont(font)
    metrics = QtGui.QFontMetricsF(font)

    if self.picking:                            # two columns of choices
        choices = list(HOTKEY_CHOICES)
        if HOTKEY not in choices:
            choices.insert(0, HOTKEY)
        choices = choices[:6]
        chip_w, chip_h, gap = 116.0, 19.0, 4.0
        left = cx - chip_w - gap / 2.0
        for i, choice in enumerate(choices):
            chip((left + (i % 2) * (chip_w + gap),
                  card.top() + 32 + (i // 2) * (chip_h + gap), chip_w, chip_h),
                 pretty(choice), "pick:" + choice, selected=(choice == HOTKEY))
        self._hits = hits
        return

    # "Hold [Ctrl+Space] and speak": the hotkey is a chip you can click.
    before = "Hold" if HOLD_TO_TALK else "Press"
    after = "and speak"
    key = pretty(HOTKEY)
    chip_w = metrics.horizontalAdvance(key) + 22.0
    widths = [metrics.horizontalAdvance(before), chip_w, metrics.horizontalAdvance(after)]
    x = cx - (sum(widths) + 16.0) / 2.0
    row = card.top() + 45
    p.setPen(grey)
    p.drawText(QtCore.QRectF(x, row - 11, widths[0] + 2, 22),
               Qt.AlignLeft | Qt.AlignVCenter, before)
    x += widths[0] + 8.0
    chip((x, row - 11, chip_w, 22.0), key, "hotkey", selected=True)
    x += chip_w + 8.0
    p.setPen(grey)
    p.drawText(QtCore.QRectF(x, row - 11, widths[2] + 2, 22),
               Qt.AlignLeft | Qt.AlignVCenter, after)

    row = card.top() + 78
    if self.welcome_ready:                      # the button, and only the button
        rect = (cx - 52.0, row - 12.0, 104.0, 24.0)
        hits["start"] = rect
        fill = QtGui.QColor(accent)
        if over != "start":
            fill.setAlpha(210)
        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(QtCore.QRectF(*rect), 12, 12)
        font.setWeight(QtGui.QFont.DemiBold)
        p.setFont(font)
        p.setPen(QtGui.QColor(16, 17, 20))
        p.drawText(QtCore.QRectF(*rect), Qt.AlignCenter, "Get started")
    else:                                       # still loading / downloading
        font.setPixelSize(11)
        p.setFont(font)
        note = "Getting the speech model ready\u2026"
        width = QtGui.QFontMetricsF(font).horizontalAdvance(note)
        breathing_dot(QtCore.QPointF(cx - width / 2.0 - 10, row), 3.0)
        p.setPen(grey)
        p.drawText(QtCore.QRectF(cx - width / 2.0, row - 10, width + 4, 20),
                   Qt.AlignLeft | Qt.AlignVCenter, note)
    self._hits = hits


Overlay._paint_welcome = _paint_welcome


def pretty(hotkey):
    return "+".join(part.strip().capitalize() for part in hotkey.split("+"))


# ----------------------------------------------------------------------------
# Glue: hotkey -> record -> transcribe -> paste
# ----------------------------------------------------------------------------
class Dictation(QtCore.QObject):
    toggle = QtCore.Signal()
    released = QtCore.Signal()
    cancel = QtCore.Signal()
    model_ready = QtCore.Signal()
    model_failed = QtCore.Signal(str)
    model_suspect = QtCore.Signal(str)        # files that fail their fingerprint check
    finished = QtCore.Signal(str)
    live_text = QtCore.Signal(int, str)
    live_piece = QtCore.Signal(int, str, int, int, str)   # typing as you speak: a finished piece
    live_words = QtCore.Signal(int, str, int, str)   # ... and a reading of the piece so far
    hotkey_captured = QtCore.Signal(str)
    paste_again = QtCore.Signal()
    failed = QtCore.Signal(str)

    def __init__(self):
        super().__init__()
        self.state = "loading"       # loading | idle | recording | transcribing
        self.model = None
        self.model_vad = None
        self.load_seconds = None
        self.target_window = None              # window that had focus at the start
        self.after_ticket = 0                  # which pending "after" key press is current
        self.last_text = ""                    # last dictation, in memory only
        self.last_time = 0.0
        self.model_lock = threading.Lock()     # one recognition at a time
        self.session = 0                       # which recording a preview belongs to
        self.live_busy = False
        self.live_seen = 0
        self.model_timed = None                # the model, also saying when words began
        self._final_skip = None                # the context to leave out of the last reading
        self._final_before = ""
        # The speech model rests between readings at least as long as the last
        # one took, so reading along uses at most about half the processor
        # time it otherwise would (it can use several cores while it reads).
        self._live_rest_until = 0.0
        # Typing as you speak: what part of the recording is already done
        # (typed, or waiting for its window), as a sample index.
        self.lt_active = False
        self.lt_commit = 0
        self.lt_index = (0, 0)               # a chunk of the recording and where it starts
        self.lt_done = ""                    # typed so far, this dictation
        self.lt_pending = ""                 # read but not typed yet (window not in front)
        self.lt_counts = [0, 0, 0]           # parts typed, waits for the window, refused
        self.lt_prev = []                    # the last reading of the piece, its words
        self.lt_seg_words = []               # words of this piece typed so far
        self.lt_all = []                     # every word typed, this dictation
        self.lt_held = None                  # punctuation held back at a piece's end
        self.lt_pacer = QtCore.QTimer()      # types the waiting words a little at a time
        self.lt_credit = 0.0                 # letters it may type now (the pace)
        self.lt_box = {"key": None, "verdict": None, "busy": False}   # is it a text box?
        self.lt_pacer.setInterval(30)
        self.lt_pacer.timeout.connect(self._pace_tick)
        self.live_timer = QtCore.QTimer()
        self.live_timer.setInterval(LIVE_EVERY_MS)
        self.live_timer.timeout.connect(self._live_tick)
        self.watch_timer = QtCore.QTimer()     # runs only while recording
        self.watch_timer.setInterval(500)
        self.watch_timer.timeout.connect(self._watchdog)
        self.record_started = 0.0
        self.screen_glow = ScreenGlow()
        self.tray_levels = deque([0.0] * 3, maxlen=3)
        self.fx_count = 0
        self.fx_demo_until = 0.0               # Settings is showing the glow off
        # Set for each dictation in _start, but the glow (and its preview from
        # Settings) reads them before the first one: without these the screen
        # glow preview failed after every start until something was dictated.
        self.popup_shown = SHOW_POPUP
        self.app_tint = None
        self.fx_timer = QtCore.QTimer()        # runs only while an effect is showing
        self.fx_timer.setInterval(33)
        self.fx_timer.timeout.connect(self._fx_tick)
        self.pointer_badge = None              # the "No text box" tag by the pointer
        self.keys_up_checks = 0
        self.status_timer = QtCore.QTimer()    # the pill's status light, every few seconds
        self.status_timer.setInterval(3000)
        self.status_timer.timeout.connect(self.update_status)
        self.status_timer.start()
        self.recorder = Recorder()
        self.overlay = Overlay(self.recorder)

        self.toggle.connect(self.on_toggle)
        self.released.connect(self.on_released)
        self.cancel.connect(self.on_cancel)
        self.overlay.cancel_clicked.connect(self.on_cancel)
        self.overlay.pill_clicked.connect(self.on_pill_clicked)
        self.overlay.finish_clicked.connect(self.on_finish_clicked)
        self.overlay.welcome_clicked.connect(self.on_welcome_done)
        self.overlay.notice_clicked.connect(self.cancel_after)
        self.overlay.retry_wanted.connect(self.retry_download)
        self.load_thread = None                # the thread loading (or downloading) the model
        self.load_failed_at = None             # when a download attempt last gave up
        self.overlay.hotkey_picked.connect(self.change_hotkey)
        self.tray = None                       # set once the tray icon exists
        self.model_ready.connect(self.on_model_ready)
        self.model_failed.connect(self.on_model_failed)
        self.model_suspect.connect(self.on_model_suspect)
        self.model_outdated = False            # loaded an older trusted release
        self.finished.connect(self.on_finished)
        self.live_text.connect(self.on_live_text)
        self.live_piece.connect(self.on_live_piece)
        self.live_words.connect(self.on_live_words)
        self.hotkey_captured.connect(self.on_hotkey_captured)
        self.paste_again.connect(self.on_paste_again)
        self._capture_apply = True             # is the hotkey being recorded the main one?
        self._capture = False                  # waiting for the user to press a hotkey
        self._capture_done = None              # who to tell when they have
        self._swallow_up = None                # key whose release should be hidden too
        self._typing_until = 0.0               # ignore our own typed keystrokes
        self._phys = set()                     # modifier keys physically down (by name)
        self.failed.connect(self.on_failed)

        # The keyboard library calls back on its own thread; signals hop to Qt's.
        # Own keyboard hook: it swallows the trigger key (Space) for as long as
        # it is held, so the app underneath never sees it or its auto-repeats.
        self._held = False
        self._action_pressed = False           # the hotkey just pressed was the action one
        self.do_action = False                 # this dictation ends with the action key
        self.apply_hotkey()
        keyboard.hook(self._on_key_event, suppress=True)

        self.intro_open = not _config.get("welcomed")   # first launch: the introduction
        self.downloading = False
        self.download_got = 0
        self.download_moved = time.monotonic()  # when more of it last arrived
        self.download_stuck = False
        self.download_failures = 0             # attempts in a row that gave up
        self.download_blocked = False          # gave up for now: waits for a retry
        self.download_timer = QtCore.QTimer()  # runs only while a download is going
        self.download_timer.setInterval(500)
        self.download_timer.timeout.connect(self._download_tick)
        if START_WITHOUT_MODEL:
            # The user chose to come in without a speech model for now: nothing
            # is downloaded or loaded. Settings work; dictation waits for a model.
            self.state = "nomodel"
            if not self.intro_open:
                self.overlay.show_message(self.NO_MODEL, ms=4000)
            return
        needs_download = not (_setup_done() or local_model_dir())
        # The download card shows even while the introduction is open: the
        # progress is something to see while reading it.
        if (not self.intro_open or needs_download) and (STARTUP_FLASH or STARTUP_MESSAGE):
            # Up straight away while the model loads behind it: just a small
            # pill with a word on it. No waveform, no timer, nothing recorded.
            self.overlay.show_message(
                "Starting…" if (_setup_done() or local_model_dir())
                else self.DOWNLOADING, ms=None)
            if not (_setup_done() or local_model_dir()):
                self.overlay.progress = 0.0     # the download card, from the start
        self.downloading = not (_setup_done() or local_model_dir())
        if self.downloading:
            self.download_timer.start()
        # Let the card finish fading in before the heavy loading starts, so
        # the animation isn't fighting it for the processor.
        QtCore.QTimer.singleShot(300, self._begin_load)

    # -- model ---------------------------------------------------------------
    def _load_model(self):
        try:
            started = time.perf_counter()
            try:      # Microsoft's runtime can emit usage telemetry on Windows
                import onnxruntime
                onnxruntime.disable_telemetry_events()
            except Exception:
                pass
            release = current_release()
            if release is None:
                raise RuntimeError("no download is known for the model %r" % MODEL)
            if network_is_open():
                # A download the user agreed to. The files go into a separate
                # folder and are checked against their fingerprints; only then
                # do they take the place of anything already here. If it fails
                # (no connection, a file that does not match), the model already
                # on this PC, if it is a trusted one, is used instead.
                try:
                    if matching_release() is not release:
                        download_release(release, model_dir())
                    if not helper_ok():
                        try:
                            download_release(HELPER_RELEASE, helper_dir())
                        except Exception as exc:
                            # Only long dictations use it; they work without.
                            print("The helper for long dictations could not be "
                                  "downloaded: %s" % exc, file=sys.stderr)
                except Exception as exc:
                    if matching_release() is None:
                        raise                    # nothing usable here: this try failed
                    print("The download did not finish (%s); using the model already "
                          "on this PC." % exc, file=sys.stderr)
                # Everything agreed to is done: shut the door again at once.
                close_network("the download is done")
            using = matching_release()
            if using is None:
                if local_model_dir():
                    # Files are here but are not the official ones: refuse them.
                    changed = model_mismatches()
                    print("Model files differ from their fingerprints:", ", ".join(changed),
                          file=sys.stderr)
                    self.model_suspect.emit(", ".join(changed))
                    return
                raise FileNotFoundError("the speech model's files are not on this PC")
            self.model_outdated = using is not release
            import onnx_asr
            kwargs = {"quantization": QUANTIZATION} if QUANTIZATION else {}
            # Lighter engine settings (measured): no memory pool saves about
            # 250 MB, and 4 threads are as fast as all of them for dictation.
            kwargs["sess_options"] = engine_options()
            print("Loading model %s from %s" % (using["tag"], model_dir()))
            self.model = onnx_asr.load_model(MODEL, model_dir(), **kwargs)
            self.model_timed = None              # made again from this model
            self.load_seconds = time.perf_counter() - started
            print("Model loaded in %.1fs (%.1fs since launch, offline)" % (
                self.load_seconds, time.perf_counter() - _T0))
            self.model_ready.emit()        # usable now
            try:   # then one silent run, so the first real dictation isn't slower.
                # It holds the model for a moment; a dictation started meanwhile
                # simply waits its turn.
                quiet = (np.random.randn(SAMPLE_RATE) * 1e-4).astype(np.float32)
                with self.model_lock:
                    self.model.recognize(quiet, sample_rate=SAMPLE_RATE)
            except Exception:
                pass
        except Exception as exc:
            if not network_is_open() and not local_model_dir():
                # Files went missing: the next start asks again (as on a first
                # launch) before going online to fetch them.
                print("Model files weren't found on this PC. Quit and start "
                      "the program again to download them.", file=sys.stderr)
                self.model_failed.emit("Model files missing: restart to download")
            else:
                print("The speech model could not be loaded: %s" % exc, file=sys.stderr)
                self.model_failed.emit(str(exc))

    def apply_hotkey(self):
        """Read the HOTKEY setting into what the keyboard hook looks for."""
        parts = [part.strip().lower() for part in HOTKEY.split("+") if part.strip()]
        self._modifiers = set(parts[:-1])
        self._trigger = parts[-1]
        extra = [part.strip().lower() for part in PASTE_HOTKEY.split("+") if part.strip()]
        if extra and PASTE_HOTKEY != HOTKEY:
            self._paste_modifiers, self._paste_trigger = set(extra[:-1]), extra[-1]
        else:
            self._paste_modifiers, self._paste_trigger = set(), None
        extra = [part.strip().lower() for part in ACTION_HOTKEY.split("+") if part.strip()]
        if extra and ACTION_HOTKEY not in (HOTKEY, PASTE_HOTKEY):
            self._action_modifiers, self._action_trigger = set(extra[:-1]), extra[-1]
        else:
            self._action_modifiers, self._action_trigger = set(), None
        self._held_trigger, self._held_modifiers = self._trigger, self._modifiers
        self.overlay.update()

    # -- recording a new hotkey ------------------------------------------------
    def start_capture(self, done, main=True):
        """Take the next key (with whatever modifiers are held) as a hotkey.
        `done` is called with it, or '' if Esc cancelled. main=True makes it the
        dictation hotkey; otherwise the caller decides what it is for."""
        if self.state == "recording":
            done("")
            return
        self._capture_done = done
        self._capture_apply = main
        self._capture = True

    def cancel_capture(self):
        self._capture = False
        self._capture_done = None

    def on_hotkey_captured(self, hotkey):
        done, self._capture_done = self._capture_done, None
        if hotkey and not valid_hotkey(hotkey):
            # A plain key (Space, a letter) would be taken from every program
            # and switch the microphone on: not accepted as a hotkey.
            self.overlay.show_message("Add Ctrl, Shift, Alt or Windows to that key", ms=3500)
            hotkey = ""
        if hotkey and self._capture_apply:
            self.change_hotkey(hotkey)
        if done is not None:
            done(hotkey)

    def show_listening(self, on):
        """Swap the tray icon while the microphone is on."""
        if self.tray is not None:
            try:
                self.tray.setIcon(app_icon(listening=bool(on)))
            except Exception:
                pass

    def change_hotkey(self, hotkey):
        if self.state == "recording":
            return
        set_hotkey(hotkey)
        self.apply_hotkey()
        if self.tray is not None:
            self.tray.setToolTip("%s  \u00b7  %s" % (APP_TITLE, pretty(HOTKEY)))

    def on_intro_done(self):
        """The introduction was finished or closed."""
        self.intro_open = False
        save_setting("welcomed", True)
        if self.state == "idle":
            self.overlay.show_message(pretty(HOTKEY) if SHOW_HOTKEY_HINT else "Ready",
                                      ms=1800, pulse=True)
            self.maybe_pill_tip()
        elif self.state == "loading":
            # A download still going (or blocked) shows as such, never as a
            # plain "Starting", which would look as if it were nearly ready.
            words = self.download_words() if self.downloading else ""
            self.overlay.show_message(words or "Starting\u2026", ms=None)
            if self.downloading:
                self.overlay.progress = self.overlay.progress or 0.0

    PILL_TIP = "Right-click me for Settings"

    def maybe_pill_tip(self):
        """Once ever: the pill itself says where Settings are, for a few
        seconds, then shows the hotkey again. (No separate card by the clock:
        Windows' tray panel covered it, owner.) Only with the pill always on
        screen, where right-clicking it works."""
        if _config.get("pill_tip_seen") or not ALWAYS_SHOW or self.intro_open:
            return

        def show():
            if self.state != "idle" or self.intro_open or _config.get("pill_tip_seen"):
                return                           # busy: another time
            save_setting("pill_tip_seen", True)
            self.overlay.show_message(self.PILL_TIP, ms=4500, pulse=True)
        QtCore.QTimer.singleShot(2600, show)     # after the hotkey's own flash

    def on_welcome_done(self):
        save_setting("welcomed", True)
        if self.overlay.mode == "welcome":
            self.overlay.dismiss()

    @staticmethod
    def _hint():
        if not SHOW_HOTKEY_HINT:
            return None
        return "%s %s to dictate" % ("Hold" if HOLD_TO_TALK else "Press", pretty(HOTKEY))

    def on_model_ready(self):
        was_waiting = self.overlay.isVisible()     # a "still loading" notice is up
        self.state = "idle"
        if self.model_outdated:                    # an older trusted model is in use
            QtCore.QTimer.singleShot(6000, self.offer_newer_model)
        self.maybe_pill_tip()                      # once ever, when usable
        if self.intro_open and not was_waiting:
            return                                 # the introduction says it is ready
        if self.overlay.mode == "welcome" and was_waiting:
            self.overlay.show_welcome(ready=True)  # turns into the Get started button
        elif STARTUP_FLASH or STARTUP_MESSAGE or was_waiting:
            # Straight to the point: show the hotkey itself, with the pulse.
            self.overlay.show_message(pretty(HOTKEY) if SHOW_HOTKEY_HINT else "Ready",
                                      ms=1500, pulse=True)
        elif ALWAYS_SHOW:
            self.overlay.show_idle()

    DOWNLOADING = "Downloading the speech model"
    NO_MODEL = "No speech model  \u00b7  download it in Settings"
    STUCK = "The download is not moving  \u00b7  click for help"
    BLOCKED = "The download seems blocked  ·  click for help"
    GIVE_UP_AFTER = 3                          # failed attempts before stopping
    STUCK_AFTER = 20.0                         # seconds with nothing new arriving
    RETRY_AFTER = 15.0                         # seconds between download attempts

    def download_words(self):
        """How the download is going, in words, or '' when nothing is downloading."""
        if not self.downloading or self.state != "loading":
            return ""
        total = model_total_bytes()
        if self.download_blocked:
            return self.BLOCKED
        if self.download_stuck:
            return self.STUCK
        if self.download_got <= 0:
            return self.DOWNLOADING
        if total:
            return "%s  \u00b7  %s of %s" % (self.DOWNLOADING, size_words(self.download_got),
                                             size_words(max(total, self.download_got)))
        return "%s  \u00b7  %s" % (self.DOWNLOADING, size_words(self.download_got))

    def _download_tick(self):
        """Twice a second while downloading: see how much has arrived (by the
        size of the files on disk) and show it on the pill."""
        if self.state != "loading" or not self.downloading:
            self.download_timer.stop()
            self.downloading = False
            self.overlay.progress = None
            return
        try:
            got = model_bytes_on_disk()
        except Exception:
            return
        if got != self.download_got:
            self.download_got = got
            self.download_moved = time.monotonic()
            self.download_stuck = False
        elif time.monotonic() - self.download_moved > self.STUCK_AFTER:
            self.download_stuck = True         # nothing new for a while: say so
        if (self.load_failed_at is not None and not self.download_blocked
                and time.monotonic() - self.load_failed_at > self.RETRY_AFTER
                and (self.load_thread is None or not self.load_thread.is_alive())):
            print("Trying the download again.")
            self._begin_load()                 # it really does keep trying
        total = model_total_bytes()
        ours = (self.overlay.message.startswith(self.DOWNLOADING)
                or self.overlay.message in (self.STUCK, self.BLOCKED))
        if self.overlay.mode == "idle" or not self.overlay.isVisible():
            # The notice went away while the download is still going (it must
            # never look as if the app were ready): put it back.
            self.overlay.show_message(self.download_words(), ms=None)
            ours = True
        if self.overlay.mode == "message" and ours:
            self.overlay.message = self.download_words()
            self.overlay.progress = (min(0.99, self.download_got / float(total))
                                     if total else None)
            self.overlay.update()

    def unload_model(self):
        """Let go of the speech model (it is about to be removed from this PC).
        The application stays open: settings still work, dictation does not.
        Returns False (and does nothing) while the model is still loading: the
        loading would only bring it back."""
        if self.state == "loading":
            return False
        self.on_cancel()                       # a dictation under way: mic off first
        with self.model_lock:                  # waits for anything using it to finish
            self.model = None
            self.model_vad = None
            self.model_timed = None
        self.state = "nomodel"
        import gc
        gc.collect()                           # so Windows lets the files be deleted
        self.overlay.dismiss(force=True)
        return True

    def on_model_suspect(self, names):
        """The model's files are not the official ones. They are never loaded:
        the only choices are to download them again, or to quit."""
        self.on_cancel()                       # a dictation under way: mic off first
        self.state = "loading"
        self.overlay.dismiss(force=True)
        again = dialog(
            None, "%s: speech model does not match" % APP_TITLE,
            "The speech model's files do not match",
            "These files are not exactly the official ones that %(app)s checks them "
            "against:\n\n%(names)s\n\nThey may be damaged, only partly downloaded, or "
            "replaced by something else, so %(app)s will not use them.\n\n"
            "Download again deletes them and restarts. This application will go "
            "online once, to github.com (a third-party website that we do not run or "
            "control), to download fresh copies from this project's model page, and "
            "checks every file before using it." % {"app": APP_TITLE, "names": names},
            accept="Download again", reject="Quit")
        if again is True:
            forget_model()
            save_setting("download_yes_once", MODEL)
            restart_app()
        else:
            QtWidgets.QApplication.quit()

    def offer_newer_model(self):
        """The model on this PC is an older trusted release (the newer one, which
        came with this version, could not be downloaded yet). It works; offer
        the newer one, once per release, after a "go online" warning."""
        release = current_release()
        if not self.model_outdated or release is None \
                or _config.get("model_update_offered") == release["tag"]:
            return
        save_setting("model_update_offered", release["tag"])
        if dialog(None, "%s: newer speech model" % APP_TITLE,
                  "This application will go online",
                  "This version of %(app)s comes with a newer release of the speech "
                  "model (%(size)s). The one on this PC still works and stays in use "
                  "until the new one has fully arrived and been checked.\n\n"
                  "Download it now? %(app)s restarts and goes online once, to github.com "
                  "(a third-party website that we do not run or control)."
                  % {"app": APP_TITLE, "size": size_words(_release_bytes(release))},
                  accept="Download and restart", reject="Not now") is True:
            save_setting("download_yes_once", MODEL)
            restart_app()

    def _begin_load(self):
        """Start loading the model (which downloads it first if need be)."""
        self.load_failed_at = None
        self.load_thread = threading.Thread(target=self._load_model, daemon=True)
        self.load_thread.start()

    def retry_download(self):
        """Try the download again, now. If the last attempt has given up, a new
        one starts straight away. If it is still hanging on a connection that
        cannot be interrupted, the application restarts itself, which is the
        one sure way to get a fresh connection; what has arrived is kept."""
        if self.state != "loading" or not self.downloading:
            return
        gave_up = self.load_thread is None or not self.load_thread.is_alive()
        if not gave_up and not self.download_stuck:
            # It is downloading and data is arriving: there is nothing to retry.
            # (Restarting here would only throw away a working connection.)
            return
        self.download_moved = time.monotonic()
        self.download_stuck = False
        if self.download_blocked:
            # The user asks for another go after it stopped trying. The download
            # was agreed to at the start of this run, so the network may open
            # again, for this download only.
            self.download_blocked = False
            self.download_failures = 0
            open_network("another try at the download, asked for just now")
        if self.load_thread is None or not self.load_thread.is_alive():
            print("Retrying the download.")
            self._begin_load()
            self.overlay.show_message(self.DOWNLOADING, ms=None)
            self.overlay.progress = 0.0
        else:
            print("Retrying the download with a fresh start.")
            save_setting("download_yes_once", MODEL)   # already agreed: do not ask again
            restart_app()

    def on_model_failed(self, error):
        print("Could not load the model:", error, file=sys.stderr)
        if self.downloading and self.state == "loading":
            # A download attempt gave up (no connection, or it was blocked). The
            # download card stays, and another attempt follows shortly, up to a
            # few times. Then it stops trying (and the network is closed again)
            # until the user asks for another go: a blocked download would
            # otherwise go online every few minutes for as long as it runs.
            self.load_failed_at = time.monotonic()
            self.download_failures += 1
            if self.download_failures >= self.GIVE_UP_AFTER:
                self.download_blocked = True
                close_network("the download failed %d times in a row" % self.download_failures)
                self.overlay.show_message(self.BLOCKED, ms=None)
            return
        # Not a download: the model is on disk but would not load. Nothing more
        # will happen this run, so leave "loading" (which would otherwise say
        # "Still loading" for ever) and close the network if it was open.
        close_network("the model could not be loaded")
        if self.state == "loading":
            self.state = "nomodel"
        self.overlay.show_message(error if len(error) < 44
                                  else "Model failed to load (see dictate.log)", ms=8000)

    # -- hotkey --------------------------------------------------------------
    def on_toggle(self):
        if self.state == "nomodel":
            self.overlay.show_message(self.NO_MODEL, ms=3000)
        elif self.state == "loading":
            if not self.overlay.isVisible():
                self.overlay.show_message("Still loading the speech model…", ms=None)
            # otherwise the welcome or "Starting" card already says so
        elif self.state == "idle":
            self._start()
            self.do_action = self._action_pressed
        elif self.state == "recording" and not HOLD_TO_TALK:
            self.do_action = self.do_action or self._action_pressed   # either press counts
            self._finish_soon()
        elif self.state == "transcribing":
            # One at a time, so the text always lands in the order you said it.
            self.overlay.show_message("Still on the last one\u2026", ms=1200)
            session = self.session
            QtCore.QTimer.singleShot(1250, lambda: self._still_working(session))
        # hold-to-talk: repeats from the held key are ignored while recording

    _MODIFIER_VKS = {"ctrl": (0x11,), "shift": (0x10,), "alt": (0x12,),
                     "windows": (0x5B, 0x5C)}
    _FKEY_VKS = dict(("f%d" % n, 0x6F + n) for n in range(1, 25))   # F1 = 0x70

    def _modifiers_match(self, wanted=None):
        """True when exactly these modifier keys (the dictation hotkey's by
        default) are physically down."""
        wanted = self._modifiers if wanted is None else wanted
        state = ctypes.windll.user32.GetAsyncKeyState
        for name, vks in self._MODIFIER_VKS.items():
            down = any(state(vk) & 0x8000 for vk in vks)
            if down != (name in wanted):
                return False
        return True

    def _mods_down(self):
        """The modifier keys physically down right now, by name."""
        state = ctypes.windll.user32.GetAsyncKeyState
        return {name for name, vks in self._MODIFIER_VKS.items()
                if any(state(vk) & 0x8000 for vk in vks)}

    def _on_key_event(self, event):
        """Sees every key event, on the keyboard thread.
        Return True to let the key through, False to swallow it."""
        try:
            # Keys this app sends itself carry no hardware scan code (the
            # keyboard library then reports minus the key's number); a real
            # keyboard always has one. So the two can be told apart.
            ours = (getattr(event, "scan_code", 1) or 0) <= 0
            family = (event.name or "").lower().replace("left ", "").replace("right ", "")
            family = "alt" if family == "alt gr" else family
            if not ours and family in self._MODIFIER_VKS:
                if event.event_type == "down":
                    self._phys.add(family)
                else:
                    self._phys.discard(family)
            if self.lt_active and self.state == "recording":
                # Typing as you speak types almost all the time, so the hotkey
                # must keep listening throughout: only our own keys are let by.
                # (Standing aside, as below, missed you letting go of the hotkey.)
                if ours:
                    return True
            elif time.monotonic() < self._typing_until:
                # Our own typed text: hands off. But a hotkey being let go just
                # now still counts, or the next Space would be taken for it.
                trigger = family == self._held_trigger
                if self._held and trigger:
                    if event.event_type == "up":
                        self._held = False
                        self.released.emit()
                    elif self._held_trigger not in self._MODIFIER_VKS:
                        # The held hotkey repeating (Windows repeats a held
                        # key): still swallowed.
                        return False
                return True
            name = (event.name or "").lower()
            for prefix in ("left ", "right "):
                if name.startswith(prefix):
                    name = name[len(prefix):]
            if name == "+":
                name = "plus"                         # "+" separates keys in a hotkey
            is_down = event.event_type == "down"

            if self._swallow_up is not None and name == self._swallow_up and not is_down:
                self._swallow_up = None
                return False
            if self._capture:
                if name in self._MODIFIER_VKS and not is_down:
                    # Modifier keys on their own, such as Ctrl+Shift: taken when
                    # the first of them is let go. (Windows has not yet marked
                    # this key as up, or would be about to, so count it in.)
                    held = self._mods_down() | {name}
                    if len(held) >= 2:
                        self._capture = False
                        self.hotkey_captured.emit("+".join(
                            m for m in ("ctrl", "shift", "alt", "windows") if m in held))
                    return True
                if name in self._MODIFIER_VKS or not name:
                    return True                       # wait for the rest of the combination
                if is_down:
                    self._capture = False
                    self._swallow_up = name
                    if name in ("esc", "escape"):
                        self.hotkey_captured.emit("")
                    else:
                        state = ctypes.windll.user32.GetAsyncKeyState
                        held = [m for m in ("ctrl", "shift", "alt", "windows")
                                if any(state(vk) & 0x8000 for vk in self._MODIFIER_VKS[m])]
                        self.hotkey_captured.emit("+".join(held + [name]))
                    return False
                return True

            if (KEEP_LAST and self._paste_trigger is not None
                    and name == self._paste_trigger and is_down and not self._held
                    and self._modifiers_match(self._paste_modifiers)):
                self._swallow_up = name               # the paste-again hotkey
                self.paste_again.emit()
                return False

            # Modifier keys (Ctrl, Shift...) are never swallowed, even when they
            # are a hotkey on their own: other shortcuts still need them.
            through = name in self._MODIFIER_VKS
            if self._held and name == self._held_trigger:
                if is_down:
                    return through                    # auto-repeat while held
                self._held = False                    # trigger released
                self.released.emit()
                return through
            if is_down and not self._held:
                # Two hotkeys can start a dictation: the usual one, and the
                # action one, which also presses a key once the text is in.
                which = None
                for label, mods, trigger in (
                        ("action", self._action_modifiers, self._action_trigger),
                        ("main", self._modifiers, self._trigger)):
                    if trigger is None:
                        continue
                    if trigger in self._MODIFIER_VKS:
                        # Modifiers only: any of them going down completes it,
                        # once exactly that set is held.
                        combo = set(mods) | {trigger}
                        hit = name in combo and (self._mods_down() | {name}) == combo
                    else:
                        combo = set(mods)
                        hit = name == trigger and self._modifiers_match(mods)
                    if hit:
                        which = label
                        break
                if which:
                    self._held = True
                    self._held_trigger = name
                    self._held_modifiers = combo
                    self._action_pressed = which == "action"
                    self.toggle.emit()
                    return through
                return True                           # an ordinary key press

            # Letting go of Ctrl first also finishes; Space stays swallowed
            # until it is released too.
            if self._held and not is_down and name in self._held_modifiers:
                self.released.emit()
            return True
        except Exception:
            return True                               # never block the keyboard

    def update_status(self):
        """Set the pill's status light: amber while the model loads; red when
        there is no microphone in Windows or no usable model; otherwise the
        accent colour. Only what can be checked without listening: a muted
        microphone is said on the card while you dictate (a light that turned
        red after a silent dictation stayed red after unmuting, as nothing
        listens between dictations to notice)."""
        if not self.overlay.isVisible():
            return                               # nothing shows the light: don't ask
        if self.state == "loading":
            status = "busy"
        elif self.state == "nomodel" or not microphone_present():
            status = "problem"
        else:
            status = "ok"
        if status != self.overlay.status:
            self.overlay.status = status
            self.overlay.update()

    TAIL_MS = 250        # keep listening this long after you let go

    def _finish_soon(self):
        """You let go (or pressed again, or clicked): keep listening a
        quarter of a second more, then finish. Letting go right as the last
        word ends otherwise cut it off before it was all heard, and the
        speech model dropped it."""
        if self.state != "recording" or getattr(self, "_finishing", False):
            return
        self._finishing = True
        session = self.session

        def now():
            if self.session == session and self.state == "recording":
                self._finish()
        QtCore.QTimer.singleShot(self.TAIL_MS, now)

    def on_released(self):
        if HOLD_TO_TALK and self.state == "recording":
            self._finish_soon()

    def on_pill_clicked(self):
        """Clicking the small always-on pill starts a dictation; clicking the
        card again finishes it (whatever the hotkey is set to do)."""
        if self.state == "idle":
            self._start(by_click=True)

    def on_finish_clicked(self):
        if self.state == "recording":
            self._finish_soon()

    def _start(self, by_click=False):
        self._finishing = False
        self.do_action = False
        self.overlay.click_started = False
        self.after_ticket += 1                 # a pending "after" key press is dropped
        self.target_window = foreground_window()
        name, path = (foreground_app(self.target_window)
                      if (SHOW_APP_ICON or SHOW_APP_NAME or GLOW_FROM_APP) else ("", ""))
        try:
            self.recorder.start()
        except Exception as exc:
            print("Microphone error:", exc, file=sys.stderr)
            self.overlay.show_message(Overlay.NO_MIC, ms=6000)
            return
        self.state = "recording"
        play_sound("start")
        self.overlay.previewing = False
        if not _config.get("welcomed"):
            save_setting("welcomed", True)       # using it counts as getting started
        self.overlay.click_started = by_click
        # With the pop-up switched off there is nothing on screen, only the
        # sounds. (A dictation started by clicking the pill still gets its card:
        # that card is what you click to finish.)
        self.popup_shown = SHOW_POPUP or by_click
        self.app_tint = None                     # the app icon's colour, with no card
        if not self.popup_shown and SCREEN_GLOW and GLOW_FROM_APP and path:
            try:                                 # glow only: still match the app
                icon = QtWidgets.QFileIconProvider().icon(QtCore.QFileInfo(path))
                if not icon.isNull():
                    self.app_tint = icon_colour(icon.pixmap(256, 256))
            except Exception:
                self.app_tint = None
        if self.popup_shown:
            self.overlay.show_recording(name, path)
            # After a click the mouse is still on the card: don't cover it with
            # "Click to finish" until the mouse has left once.
            self.overlay.hover = by_click
            self.overlay.hover_armed = not by_click
        self.session += 1
        self.live_seen = 0
        self.lt_active = LIVE_TYPE
        self.lt_commit, self.lt_index = 0, (0, 0)
        self.lt_done = self.lt_pending = ""
        self.lt_counts = [0, 0, 0]
        self.lt_prev, self.lt_seg_words = [], []
        self.lt_all, self.lt_held = [], None
        self.lt_box = {"key": None, "verdict": None, "busy": False}
        # Typing as you speak reads along more often, so words appear sooner.
        self.live_timer.setInterval(120 if LIVE_TYPE else LIVE_EVERY_MS)
        # No card and no typing as you speak: no need to read along.
        if (LIVE_PREVIEW and self.popup_shown) or LIVE_TYPE:
            self.live_timer.start()
        self.record_started = time.monotonic()
        self.keys_up_checks = 0
        self.watch_timer.start()
        self.show_listening(True)
        self._start_effects()

    def demo_screen_glow(self):
        """From Settings: show the screen glow for a moment (nothing records)."""
        if self.state in ("idle", "loading", "nomodel"):
            self.fx_demo_until = time.monotonic() + 3.0   # long enough to look at
            self._start_effects(demo=True)

    def _start_effects(self, demo=False):
        """The optional extras: a glow round the screen, a moving tray icon.
        Neither may ever get in the way of dictating, so errors are swallowed."""
        try:
            if not demo:
                self.fx_demo_until = 0.0
            if SCREEN_GLOW:
                colour = glow_base()
                tint = getattr(self.overlay, "app_colour", None)
                if not self.popup_shown:
                    tint = getattr(self, "app_tint", None)
                if GLOW_FROM_APP and tint is not None and not demo:
                    colour = QtGui.QColor(tint)
                spot = self.overlay.frameGeometry().center()
                screen = (QtGui.QGuiApplication.screenAt(spot)
                          or QtGui.QGuiApplication.primaryScreen())
                # The pop-up's screen, every screen, or the main one (Settings).
                if SCREEN_GLOW_SCREENS == "all":
                    areas = [s.geometry() for s in QtGui.QGuiApplication.screens()]
                elif SCREEN_GLOW_SCREENS == "main":
                    areas = [QtGui.QGuiApplication.primaryScreen().geometry()]
                else:
                    areas = [screen.geometry()]
                self.screen_glow.start(colour, areas)
            if SCREEN_GLOW or (TRAY_LIVE and self.tray is not None):
                self.fx_timer.start()
        except Exception as exc:
            print("Effect could not start:", exc, file=sys.stderr)

    def _fx_tick(self):
        try:
            active = self.state == "recording"
            level = float(getattr(self.recorder, "level", 0.0)) if active else 0.0
            if not active and time.monotonic() < self.fx_demo_until:
                alive = self.screen_glow.step(0.55, True)        # the Settings demo
            else:
                # Nothing can be typed right now ("No text box selected"): the
                # glow holds still, so it doesn't look as if you are typing.
                still = bool(active and self.lt_active and self.overlay.box_missing)
                alive = self.screen_glow.step(level, active, still)
            if active and TRAY_LIVE and self.tray is not None:
                alive = True
                self.fx_count += 1
                if self.fx_count % 3 == 0:               # about ten times a second
                    self.tray_levels.appendleft(level)
                    self.tray.setIcon(app_icon(True, list(self.tray_levels)))
            if not alive:
                self.fx_timer.stop()
        except Exception:
            self.fx_timer.stop()

    def _watchdog(self):
        """Safety net: the microphone must never stay on by accident."""
        if self.state != "recording":
            self.watch_timer.stop()
            if self.recorder.stream is not None:
                print("The microphone was still open outside a dictation; closed it.")
                self.recorder.close()
                self.show_listening(False)
            return
        if time.monotonic() - self.record_started > MAX_RECORD_SECONDS:
            print("Recording reached the %d s limit and was stopped." % MAX_RECORD_SECONDS)
            self._finish()
            return
        trigger_vk = self._FKEY_VKS.get(getattr(self, "_held_trigger", "") or "")
        if HOLD_TO_TALK and not self._held_modifiers and trigger_vk \
                and not self.overlay.click_started:
            # A lone F-key hotkey: no modifiers to watch, so watch the key.
            try:
                down = bool(ctypes.windll.user32.GetAsyncKeyState(trigger_vk) & 0x8000)
            except Exception:
                return
            self.keys_up_checks = 0 if down else self.keys_up_checks + 1
            if self.keys_up_checks >= 2:
                print("Hotkey was released without the app noticing; recording stopped.")
                self._held = False
                self._finish()
            return
        if HOLD_TO_TALK and self._held_modifiers and not self.overlay.click_started:
            # Hold-to-talk: if the hotkey's modifier keys are no longer down but
            # the release was somehow missed (screen lock, a crash elsewhere),
            # stop anyway. (Not judged right after typing as you speak: it lets
            # go of the keys for the app for a moment.)
            if time.monotonic() - getattr(self, "_last_batch", 0.0) < 0.15:
                return
            try:
                state = ctypes.windll.user32.GetAsyncKeyState
                held = all(any(state(vk) & 0x8000 for vk in self._MODIFIER_VKS[name])
                           for name in self._held_modifiers if name in self._MODIFIER_VKS)
            except Exception:
                return
            self.keys_up_checks = 0 if held else self.keys_up_checks + 1
            if self.keys_up_checks >= 2:
                print("Hotkey was released without the app noticing; recording stopped.")
                self._held = False
                self._finish()

    # -- live preview: re-read the recent audio every so often ---------------
    def _live_tick(self):
        if self.state != "recording" or not (LIVE_PREVIEW or self.lt_active):
            self.live_timer.stop()
            return
        if getattr(self.recorder, "peak", 1.0) < MIC_SILENT_PEAK:
            # No sound at all yet (a muted microphone): nothing to read. Quiet
            # audio is turned up before reading, and pure noise turned up
            # makes the speech model "hear" a word such as "Okay", which would
            # then be shown and typed.
            return
        if self.lt_active:
            self._flush_live()                   # back in the right window: catch up
            if TEXTBOX_ONLY and self.target_window \
                    and foreground_window() == self.target_window:
                # Said on the card as it happens: "No text box selected". Only
                # after two checks in a row, so a quick change of focus doesn't
                # make it flicker on and off.
                missing = not self._in_text_box(self.target_window)
                fresh = self.lt_box.get("at", 0.0) != getattr(self, "lt_missing_at", None)
                if getattr(self, "lt_box_waiting", False):
                    # The focus has just moved: Windows is being asked about
                    # the new spot. Drop the old warning at once; if this
                    # spot isn't a text box either, it comes back when Windows
                    # says so. (Typing still waits for the answer.)
                    self.lt_missing = 0
                elif not missing:
                    self.lt_missing = 0
                elif fresh:                      # count each new answer once
                    self.lt_missing = getattr(self, "lt_missing", 0) + 1
                    self.lt_missing_at = self.lt_box.get("at", 0.0)
                was = self.overlay.box_missing
                self.overlay.box_missing = getattr(self, "lt_missing", 0) >= 2
                if was and not self.overlay.box_missing:
                    # Back in a text box: the card's words line comes back
                    # with the text as it is now, not as it was before.
                    self._show_typed(self.session, [])
            else:
                self.lt_missing = 0
                self.overlay.box_missing = False
            self._pointer_hint(self.overlay.box_missing)
        if self.live_busy:
            return                               # previous pass still running
        if time.monotonic() < self._live_rest_until:
            return                               # the model is resting a moment
        if self.lt_active:
            self._live_type_tick()
            return
        total = sum(len(chunk) for chunk in list(self.recorder.chunks))
        if total < SAMPLE_RATE * 0.6 or total == self.live_seen:
            return
        self.live_seen = total
        audio = self.recorder.recent(LIVE_WINDOW_SECONDS * LIVE_LINES)
        self.live_busy = True
        threading.Thread(target=self._live_run, args=(audio, self.session),
                         daemon=True).start()

    def _live_run(self, audio, session):
        try:
            audio = leveled(audio)
            with self.model_lock:
                began = time.monotonic()
                text = str(self.model.recognize(audio, sample_rate=SAMPLE_RATE))
                done = time.monotonic()
                self._live_rest_until = done + (done - began) * LIVE_REST
            self.live_text.emit(session, " ".join(text.split()))
        except Exception:
            pass
        finally:
            self.live_busy = False

    def on_live_text(self, session, text):
        if session == self.session and self.state == "recording" and text \
                and LIVE_PREVIEW and self.popup_shown:
            self.overlay.set_live_text(text)

    # -- typing as you speak -------------------------------------------------------
    def _audio_since(self, start):
        """The recording from sample `start` on (without joining all of it
        again on every tick: it remembers which chunk that was)."""
        chunks = list(self.recorder.chunks)
        i, pos = self.lt_index
        if pos > start or i > len(chunks):
            i, pos = 0, 0
        while i < len(chunks) and pos + len(chunks[i]) <= start:
            pos += len(chunks[i])
            i += 1
        self.lt_index = (i, pos)
        if i >= len(chunks):
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks[i:])[start - pos:]

    LT_CONTEXT = 2.0     # seconds of what came before, read along with each piece

    def _live_type_tick(self):
        """Typing as you speak. The piece being spoken (since the last pause)
        is read again and again, together with the last couple of seconds
        before it (so a word right after a pause is understood in its
        sentence, and the sentence isn't broken up). A word is typed once two
        readings in a row agree on it, the newest word excepted (it is often
        still changing). At a pause the piece is read once more to finish it."""
        start = max(0, self.lt_commit - int(self.LT_CONTEXT * SAMPLE_RATE))
        audio = self._audio_since(start)
        lead = self.lt_commit - start            # the part before the piece
        cut = find_pause(audio[lead:], SAMPLE_RATE)
        if cut:
            self.live_busy = True
            threading.Thread(target=self._live_piece_run,
                             args=(audio[:lead + cut], self.lt_commit + cut, self.session,
                                   self.lt_commit, lead / float(SAMPLE_RATE)),
                             daemon=True).start()
            return
        total = start + len(audio)
        if len(audio) - lead < SAMPLE_RATE * 0.4 or total == self.live_seen:
            return
        self.live_seen = total
        self.live_busy = True
        threading.Thread(target=self._live_read_run,
                         args=(audio, self.lt_commit, self.session, lead / float(SAMPLE_RATE)),
                         daemon=True).start()

    def _timed_read(self, audio, skip):
        """Read audio, leaving out the words spoken in its first `skip`
        seconds (the context read along before a piece). The speech model
        says when each word began, so this is exact, even when the same
        words repeat ("leet say, leet say"). Returns (the piece's words, the
        last word of the context, "" if none). Call with model_lock held."""
        if self.model_timed is None:
            self.model_timed = self.model.with_timestamps()
        result = self.model_timed.recognize(audio, sample_rate=SAMPLE_RATE)
        words = []                                   # [text, when it began]
        for token, at in zip(result.tokens, result.timestamps):
            if token.startswith(" ") or not words:
                words.append([token.strip(), float(at)])
            else:
                words[-1][0] += token
        words = [w for w in words if w[0]]
        before = [w for w, at in words if at < skip - 0.04]
        piece = [w for w, at in words if at >= skip - 0.04]
        return " ".join(piece), (before[-1] if before else "")

    def _live_read_run(self, audio, start, session, skip):
        try:
            audio = leveled(audio)
            with self.model_lock:
                began = time.monotonic()
                text, before = self._timed_read(audio, skip)
                done = time.monotonic()
                self._live_rest_until = done + (done - began) * LIVE_REST
            self.live_busy = False               # free for the next reading at once
            self.live_words.emit(session, text, start, before)
        except Exception:
            pass
        finally:
            self.live_busy = False

    def _live_piece_run(self, audio, end, session, start, skip):
        try:
            audio = leveled(audio)
            with self.model_lock:
                began = time.monotonic()
                text, before = self._timed_read(audio, skip)
                done = time.monotonic()
                self._live_rest_until = done + (done - began) * LIVE_REST
            self.live_piece.emit(session, text, end, start, before)
        except Exception:
            pass                                 # not done: the final pass reads it
        finally:
            self.live_busy = False

    @staticmethod
    def _plain(words):
        """Words compared without capitals or punctuation ("Hello," = "hello")."""
        return ["".join(c for c in w.lower() if c.isalnum() or c == "'") for w in words]

    @staticmethod
    def _end_marks(word):
        """The punctuation a word ends with ("great." -> ".")."""
        i = len(word)
        while i > 0 and word[i - 1] in ".,!?;:\u2026":
            i -= 1
        return word[i:] if i > 0 else ""

    def _after_typed(self, plain):
        """Where, in a new reading, the words not typed yet begin. A reading
        starts with words typed already (the end of the last piece, read
        again for context, and this piece's typed words), and they can come
        out a little differently the second time: line them up, and skip
        past them. Only the start of the reading is searched, so a common
        word further on ("the") is never mistaken for one already typed."""
        typed = self.lt_seg_words
        if not typed:
            return 0
        import difflib
        near = plain[:len(typed) + 3]
        blocks = [b for b in difflib.SequenceMatcher(None, typed, near, autojunk=False)
                  .get_matching_blocks() if b.size]
        if not blocks:
            return min(len(typed), len(plain))
        last = blocks[-1]
        return min(len(plain), last.b + last.size + (len(typed) - (last.a + last.size)))

    def _compose(self, words, before=None, piece_end=False):
        """The text to type for these new words. The last word's full stop
        (or comma) is held back: the model often only decides it on hearing
        what comes next, so the next words, read together with it, show
        whether the sentence really ended there."""
        text = ""
        if self.lt_held is not None:
            # The held-back mark, as the newest reading has it (none: the
            # sentence went on).
            text = self.lt_held if before is None else self._end_marks(before)
            first = words[0] if words else ""
            if (not text and first[:1].isupper()
                    and self._plain([first])[0] not in ("i", "i'm", "i've", "i'll", "i'd")):
                text = self.lt_held                # a capital: a new sentence after all
            if not words and before is None:
                self.lt_held = None
                return text
            self.lt_held = None
        if not words:
            return text
        words = list(words)
        if piece_end:
            # Held back, even when it has none yet: a full stop often appears
            # only once the model hears the next word ("now" -> "now. So").
            marks = self._end_marks(words[-1])
            if marks and len(marks) < len(words[-1]):
                words[-1] = words[-1][:-len(marks)]
            else:
                marks = ""
            self.lt_held = marks
        if self.lt_done or self.lt_pending or text:
            text += " "
        self.lt_all += self._plain(words)
        return text + " ".join(words)

    def _queue_words(self, words, before=None, piece_end=False):
        text = self._compose(words, before, piece_end)
        if text:
            self.lt_pending += text
            self._flush_live()

    def on_live_words(self, session, text, start, before=""):
        if session != self.session or self.state != "recording" or not self.lt_active:
            return
        if start != self.lt_commit:
            return                               # a reading of a piece already done
        words = clean_words(text.split())
        plain = self._plain(words)
        begin = self._after_typed(plain)
        self._show_typed(session, words[begin:])  # the card: as it is in your app
        fresh = plain[begin:]
        # Compare only the words not typed yet: a typed word read a little
        # differently again must not hold up everything after it.
        agreed = 0
        for a, b in zip(fresh, self.lt_prev):
            if a != b:
                break
            agreed += 1
        self.lt_prev = fresh
        upto = begin + min(agreed, len(fresh) - 1)       # never the newest word
        # The next reading starts as soon as the model has rested, not at the
        # next tick of the clock: words are confirmed as soon as it can manage.
        wait = max(0.0, self._live_rest_until - time.monotonic())
        QtCore.QTimer.singleShot(int(wait * 1000) + 1, self._live_tick)
        if upto > begin:
            self._queue_words(words[begin:upto], words[begin - 1] if begin else (before or None),
                              piece_end=True)
            self.lt_seg_words += plain[begin:upto]

    def on_live_piece(self, session, text, end, start, before=""):
        # Too late (the dictation is over): the final pass reads this part
        # anyway, as the done mark was not moved on. A second reading of a
        # piece already done (its start has moved on) is dropped, or its words
        # would be typed twice.
        if session != self.session or self.state != "recording" or not self.lt_active:
            return
        if start != self.lt_commit:
            return
        words = clean_words(text.split())
        begin = self._after_typed(self._plain(words))
        self._queue_words(words[begin:], words[begin - 1] if begin else (before or None),
                          piece_end=True)
        self.lt_commit = max(self.lt_commit, end)
        self.lt_prev = []
        self.lt_seg_words = []                   # the next piece starts afresh
        self._show_typed(session, [])

    def _show_typed(self, session, coming):
        """The card, while typing as you speak: the text as it is in your app
        (typed, or waiting to be) and then the newest words still being
        settled, so the card and the text box match. Only the last few
        dozen words: the card shows the end."""
        words = (self.lt_done + self.lt_pending).split() + list(coming)
        if words:
            self.on_live_text(session, " ".join(words[-60:]))

    def _flush_live(self):
        """Words are waiting: start typing them (see _pace_tick)."""
        if self.lt_pending and not self.lt_pacer.isActive():
            self.lt_credit = 0.0
            self.lt_pacer.start()

    def _in_text_box(self, target):
        """Typing as you speak only goes into a text box. Asked again whenever
        the focus moves (and every second), in the background; until the
        answer is in, it waits. "Can't tell" counts as yes, so programs that
        don't answer still get their words."""
        if not TEXTBOX_ONLY:
            return True                          # switched off: Windows isn't asked
        focus, _caret = focus_details(target)
        key = (int(target), focus)
        box = self.lt_box
        # Asked again every quarter of a second: in a browser the whole page
        # is one window, so a click from a text box onto the page doesn't
        # change the window that has the focus.
        stale = box["key"] != key or time.monotonic() - box.get("at", 0.0) > 0.25
        if stale and not box["busy"]:
            box["busy"] = True

            def ask():
                try:
                    verdict = focused_text_box()
                except Exception:
                    verdict = None
                box.update(key=key, verdict=verdict, at=time.monotonic(), busy=False)
            threading.Thread(target=ask, daemon=True).start()
        self.lt_box_waiting = box["key"] != key
        if self.lt_box_waiting:
            return False                         # not asked yet about this one: wait
        # A clear answer counts (a web page's "not a text box" wins even over a
        # text cursor some browsers keep); "can't tell" types as usual.
        return box["verdict"] is not False

    def _release_stuck_soon(self):
        """After typing as you speak: let go of any modifier key left down that
        you are not holding. Now, and again a moment later, because the last
        batch of keystrokes can still be on its way through Windows."""
        release_stuck(set(self._phys))
        QtCore.QTimer.singleShot(250, lambda: release_stuck(set(self._phys)))

    def _lt_forget(self):
        """Drop everything typing as you speak kept of this dictation: the
        words stay in memory no longer than needed ("Missed text safety"
        decides separately whether the last dictation is kept)."""
        self.lt_done = self.lt_pending = ""
        self.lt_all, self.lt_prev, self.lt_seg_words = [], [], []
        self.lt_held = None
        self._final_before, self._final_skip, self._final_timed = "", None, None

    def _text_box_now(self, target):
        """The same question, answered now (waits at most half a second)."""
        if not TEXTBOX_ONLY:
            return True
        answer = {}
        asking = threading.Thread(target=lambda: answer.setdefault("v", focused_text_box()),
                                  daemon=True)
        asking.start()
        asking.join(0.5)
        return answer.get("v")

    def _pace_tick(self):
        """Typing as you speak, smoothly: the waiting words go in a few letters
        at a time at an even pace rather than in jumps, a little faster when
        more are waiting, so it never falls behind. Only into the window the
        dictation started in: with another window in front it waits (and is
        typed when you come back, or with the rest when you let go)."""
        if self.state != "recording" or not self.lt_active or not self.lt_pending:
            self.lt_pacer.stop()
            return
        target = self.target_window
        if not target or foreground_window() != target or window_out_of_reach(target):
            self.lt_counts[1] += 1
            self.lt_pacer.stop()                 # the live tick tries again
            return
        if not self._in_text_box(target):
            # Not in a text box (a web page, a video): typed letters would be
            # shortcuts there and Space would scroll. Wait: the words stay on
            # the card, and go in if you click into a text box.
            self.lt_counts[1] += 1
            self.lt_pacer.stop()
            return
        # A steady flow: the more is waiting, the faster it goes (about a
        # quarter of a second to catch up), never stopping and starting in jumps.
        self.lt_credit += max(len(self.lt_pending) / 6.0, 2.0)
        count = min(len(self.lt_pending), int(self.lt_credit))
        if count < 1:
            return
        self.lt_credit -= count
        part = self.lt_pending[:count]
        self._typing_until = time.monotonic() + 5.0          # our own keystrokes
        try:
            typed = type_while_held(part, set(self._phys))
        finally:
            self._typing_until = time.monotonic() + 0.3
            self._last_batch = time.monotonic()
        if typed:
            self.lt_done += part
            self.lt_pending = self.lt_pending[count:]
            self.lt_counts[0] += 1
        else:
            self.lt_counts[2] += 1
            self.lt_pacer.stop()

    def move_popup(self):
        if self.state in ("idle", "loading", "nomodel"):
            self.overlay.invite_drag()

    def resize_popup(self):
        """From Settings: hold the pop-up on screen so its edges can be dragged."""
        if self.state in ("idle", "loading", "nomodel") and self.overlay.mode != "welcome":
            self.overlay.preview(ms=12000, words="Drag either edge")

    def preview_popup(self):
        if self.state in ("idle", "loading", "nomodel") and self.overlay.mode != "welcome":
            self.overlay.preview()

    def preview_pill(self):
        """After a change to the small pill (its mark, the hotkey text): show
        the pill itself, not the dictation card. Always on screen: it simply
        changes in place; otherwise it shows for a moment."""
        if self.state not in ("idle", "loading", "nomodel") or self.overlay.mode == "welcome":
            return
        if ALWAYS_SHOW and self.overlay.isVisible() and self.overlay.mode == "idle":
            self.overlay.update()
            return
        self.overlay.show_message(pretty(HOTKEY) if SHOW_HOTKEY_HINT else "", ms=2500)

    def refresh_popup(self):
        """After a settings change: resize, and show or hide the idle pill."""
        self.overlay.apply_size()
        if self.state not in ("idle", "loading", "nomodel"):
            return
        mode, visible = self.overlay.mode, self.overlay.isVisible()
        if ALWAYS_SHOW and self.state == "idle" and (not visible or mode == "idle"):
            self.overlay.show_idle()
        elif not ALWAYS_SHOW and visible and mode == "idle":
            self.overlay.dismiss(force=True)

    def on_cancel(self):
        self._pointer_hint(False)
        if self.state != "recording":
            return
        self.live_timer.stop()
        self.lt_pacer.stop()
        self.watch_timer.stop()
        self.recorder.stop()
        self.show_listening(False)
        self.state = "idle"
        if self.lt_active:
            self._release_stuck_soon()
        self.lt_active = False                   # what was typed already stays
        self._lt_forget()
        self.lt_pending = ""
        self.overlay.dismiss()

    def _pointer_hint(self, show):
        """The "No text box" tag by the mouse pointer (Settings: on by default)."""
        if show and POINTER_HINT:
            if self.pointer_badge is None:
                self.pointer_badge = PointerBadge()
            self.pointer_badge.appear()
        elif self.pointer_badge is not None:
            self.pointer_badge.vanish()

    def _finish(self):
        self._finishing = False
        self._pointer_hint(False)
        if self.state != "recording":
            return                               # already finished (or cancelled)
        if self.lt_active:
            self._release_stuck_soon()
        self.live_timer.stop()
        self.lt_pacer.stop()
        self.watch_timer.stop()
        audio = self.recorder.stop()
        self.show_listening(False)
        play_sound("finish")
        if len(audio) < SAMPLE_RATE * TAP_SECONDS:    # a tap: nothing was said
            self.state = "idle"
            again = self.last_dictation()
            if again:                          # paste the last dictation once more
                self.overlay.show_message("Pasted again", ms=1300)
                QtCore.QTimer.singleShot(60, lambda: self._deliver(again, 30))
            else:
                self.overlay.dismiss()
            return
        if self.recorder.peak < MIC_SILENT_PEAK:  # the microphone gave pure silence
            self.state = "idle"
            if getattr(self, "popup_shown", True):
                self.overlay.dismiss()   # the card already said so while recording
            else:                        # no card was showing: say it now
                self.overlay.show_message(
                    "No sound from the microphone  \u00b7  click for help", ms=6000)
            return
        self.state = "transcribing"
        if self.lt_active and self.lt_commit:
            # Typing as you speak: only the piece since the last pause is left
            # to read, with a little of what came before (its first words are
            # typed already: see on_finished).
            whole, rest = audio, audio[self.lt_commit:]
            if len(rest) < SAMPLE_RATE * 0.3 or not holds_speech(rest, whole, SAMPLE_RATE):
                self.overlay.dismiss()
                QtCore.QTimer.singleShot(0, lambda: self.on_finished(""))
                return
            if len(rest) > LONG_AUDIO_SECONDS * SAMPLE_RATE:
                # A long stretch with no pause: read in pieces like any long
                # dictation (without the context; matched to the typed words).
                audio = rest
            else:
                first = max(0, self.lt_commit - int(self.LT_CONTEXT * SAMPLE_RATE))
                audio = whole[first:]
                self._final_skip = (self.lt_commit - first) / float(SAMPLE_RATE)
        if SHOW_TRANSCRIBING and getattr(self, "popup_shown", True):
            self.overlay.show_transcribing()
        else:
            self.overlay.dismiss()         # vanish now; the text follows shortly
            # ... unless it is not short: after a moment, say what is going on.
            session = self.session
            QtCore.QTimer.singleShot(700, lambda: self._still_working(session))
        threading.Thread(target=self._transcribe, args=(audio,), daemon=True).start()

    WORKING, TYPING = "Transcribing\u2026", "Typing\u2026"

    def _still_working(self, session):
        """A long dictation takes a few seconds to turn into text: show a small
        pill meanwhile, so it doesn't look as if nothing is happening."""
        if (self.state == "transcribing" and session == self.session
                and getattr(self, "popup_shown", True)):
            self.overlay.show_message(self.WORKING, ms=None)

    # -- transcription -------------------------------------------------------
    def _transcribe(self, audio):
        try:
            text = None
            audio = leveled(audio)
            skip, self._final_skip = self._final_skip, None
            with self.model_lock:              # waits for a preview pass to end
                if skip is not None:
                    # The rest of a live dictation, read with its context: only
                    # the words after the context are new.
                    text, self._final_before = self._timed_read(audio, skip)
                    self.finished.emit(text.strip())
                    return
                if len(audio) > LONG_AUDIO_SECONDS * SAMPLE_RATE:
                    if self.model_vad is None:     # loaded on first use, not at start
                        try:
                            if not helper_ok():    # only the official file is used
                                raise FileNotFoundError("the helper is not on this PC")
                            import onnx_asr
                            self.model_vad = self.model.with_vad(
                                onnx_asr.load_vad("silero", helper_dir()))
                        except Exception as exc:
                            print("Long dictation without the helper: %s" % exc,
                                  file=sys.stderr)
                            self.model_vad = False
                if self.model_vad and len(audio) > LONG_AUDIO_SECONDS * SAMPLE_RATE:
                    try:
                        parts = self.model_vad.recognize(audio, sample_rate=SAMPLE_RATE)
                        text = " ".join(str(getattr(part, "text", part)).strip()
                                        for part in parts)
                    except Exception:
                        text = None
                if text is None:
                    text = self.model.recognize(audio, sample_rate=SAMPLE_RATE)
            self.finished.emit(str(text).strip())
        except Exception as exc:
            self.failed.emit(str(exc))

    def on_failed(self, error):
        print("Transcription error:", error, file=sys.stderr)
        if self.lt_active and (self.lt_done or self.lt_pending):
            self.on_finished("")                 # keep (and type) what was read already
            return
        self.state = "idle"
        self.overlay.show_message("Transcription failed", ms=2500)

    def on_finished(self, text):
        self.state = "idle"
        text = " ".join(clean_words(text.split()))   # one line: a stray Enter could
                                               # send a message
        typed = ""
        if self.lt_active:
            # Typing as you speak: what is left, after what was typed already
            # (and anything that waited for its window to come back).
            self.lt_active = False
            # Counts only: the log never holds your words or program names.
            print("Typing as you speak: %d bits typed while speaking, %d waits for "
                  "the starting window or a text box, %d refused by Windows."
                  % tuple(self.lt_counts))
            typed = self.lt_done
            words = text.split()                 # skip the words typed already
            begin = self._after_typed(self._plain(words)) if words else 0
            text = self._compose(words[begin:], words[begin - 1] if begin
                                 else (self._final_before or None))
            self._final_before = ""
            text = self.lt_pending + text
            self._lt_forget()
            if text.strip() and self.target_window \
                    and foreground_window() != self.target_window:
                # Typing as you speak only ever types into the window you
                # started in: back to it, or the rest isn't typed.
                if not focus_window(self.target_window):
                    print("Typing as you speak: the starting window is gone or out of "
                          "reach; the rest was not typed.")
                    self.remember(typed + text)  # kept only with "Missed text safety"
                    self.overlay.show_message("Back in the other window? The rest "
                                              "wasn't typed", ms=3000)
                    return
            if (text.strip() and TEXTBOX_ONLY and self.target_window
                    and foreground_window() == self.target_window
                    and self._text_box_now(self.target_window) is False):
                # Still no text box when you let go (a web page, a video):
                # typing it now would set off shortcuts there, so it isn't.
                print("Typing as you speak: no text box at the end; the rest was not typed.")
                self.remember(typed + text)      # kept only with "Missed text safety"
                if self.overlay.box_missing:
                    # The card already said "No text box selected": just close.
                    self.overlay.dismiss()
                else:                            # it had no chance to say so
                    self.overlay.show_message("No text box, so it wasn't typed", ms=1500)
                return
            if not text and typed:
                text = " " if ADD_SPACE else ""
                after_key = ACTION_KEY if (self.do_action and ACTION_KEY) else AFTER_KEY
                self.remember(typed)
                if text or after_key:
                    self.overlay.dismiss()
                    QtCore.QTimer.singleShot(60, lambda: self._deliver(
                        "" if after_key else text, 30, finish=True, after_key=after_key))
                else:
                    self.overlay.dismiss()
                return
        if not text:
            self.overlay.show_message("Didn't catch that", ms=1400)
            return
        if TYPE_TEXT and len(text) > 150 and getattr(self, "popup_shown", True):
            self.overlay.show_message(self.TYPING, ms=None)    # a long one takes a moment
        else:
            self.overlay.dismiss()
        # The key to press afterwards: the action hotkey's own, or the usual one.
        after_key = ACTION_KEY if (self.do_action and ACTION_KEY) else AFTER_KEY
        if ADD_SPACE and not after_key:
            text += " "                        # so the next dictation doesn't run on
        self.remember(typed + text)
        delay = 60
        if RETURN_FOCUS and self.target_window:
            now = foreground_window()
            if now and now != self.target_window:
                # Something else took the focus while you were speaking.
                back = focus_window(self.target_window)
                # The log never names the programs you use (least data kept).
                print("Focus moved to another window during dictation; %s." % (
                    "returned to the window you started in" if back
                    else "could not return to the starting window"))
                delay = 200                    # give the window a moment to wake up
        QtCore.QTimer.singleShot(
            delay, lambda: self._deliver(text, 30, finish=True, after_key=after_key))

    # -- the last dictation, kept briefly so a missed paste isn't lost --------
    def remember(self, text):
        if not KEEP_LAST:
            self.forget()
            return
        self.last_text = text
        self.last_time = stamp = time.monotonic()
        QtCore.QTimer.singleShot(int(KEEP_LAST_SECONDS * 1000) + 500,
                                 lambda: self.forget(only_if=stamp))

    def on_paste_again(self):
        """The paste-again hotkey: put the last dictation in once more."""
        if self.state != "idle":
            return
        again = self.last_dictation()
        if again:
            self.overlay.show_message("Pasted again", ms=1300)
            QtCore.QTimer.singleShot(60, lambda: self._deliver(again, 30))
        else:
            self.overlay.show_message("Nothing to paste", ms=1300)

    def forget(self, only_if=None):
        if only_if is None or only_if == self.last_time:
            self.last_text = ""
            self.last_time = 0.0

    def last_dictation(self):
        """The last text if it is still being kept, else ''."""
        if not KEEP_LAST or not self.last_text:
            return ""
        if time.monotonic() - self.last_time > KEEP_LAST_SECONDS:
            self.forget()
            return ""
        return self.last_text

    def _deliver(self, text, tries, finish=False, after_key=None):
        """Put the text into the focused app. finish=True (a fresh dictation,
        not a paste-again) also presses the "afterwards" key if one is set."""
        # Wait until the hotkey's modifier keys are released, or they would
        # combine with the keys sent below.
        if tries > 0 and any(keyboard.is_pressed(k) for k in ("ctrl", "shift", "alt")):
            QtCore.QTimer.singleShot(
                50, lambda: self._deliver(text, tries - 1, finish, after_key))
            return
        after = (AFTER_KEY if after_key is None else after_key) if finish else ""
        if window_out_of_reach(foreground_window()):
            # Windows would drop every keystroke without a word. Say so instead
            # (and leave the clipboard alone too: it could not be pasted either).
            print("The focused window runs as administrator; the text was not typed.")
            if self.overlay.mode == "message" and self.overlay.message == self.TYPING:
                self.overlay.dismiss()
            self.overlay.show_message("Can't type into an admin window", ms=5000)
            return
        # While these keystrokes go out, the hotkey listener stands aside, so
        # nothing sent here can be mistaken for a hotkey.
        self._typing_until = time.monotonic() + 1.5 + len(text) * 0.004
        if TYPE_TEXT:
            try:
                self._typing_until = time.monotonic() + 120.0     # for as long as it takes
                keyboard.write(text, delay=0)   # typed out; clipboard untouched
            finally:
                # Done: hand the hotkey back almost at once, so the next
                # dictation can start straight after this one lands.
                self._typing_until = time.monotonic() + 0.25
                if self.overlay.mode == "message" and self.overlay.message == self.TYPING:
                    self.overlay.dismiss()
            if after:                           # a beat for the app to take the text in
                self._press_after(after, 180)
            return
        # Paste: borrow the clipboard for a moment, then put back what was there.
        before = self._snapshot_clipboard()
        self._copy_privately(text)
        QtCore.QTimer.singleShot(40, lambda: keyboard.send("ctrl+v"))
        if after:
            self._press_after(after, 260)
        QtCore.QTimer.singleShot(40 + CLIPBOARD_RESTORE_MS,
                                 lambda: self._restore_clipboard(before, text))

    def _press_after(self, key, settle_ms):
        """Press the "after each dictation" key, once the chosen wait is over.
        During a wait a notice shows what is coming; clicking it calls it off."""
        self.after_ticket += 1
        ticket = self.after_ticket
        wait_ms = settle_ms + int(AFTER_DELAY * 1000)
        target = foreground_window()             # the window the text went into
        if AFTER_DELAY > 0:
            self.overlay.show_message(
                "%s in %d s  \u00b7  click to cancel" % (pretty(key), AFTER_DELAY),
                ms=wait_ms)

        def press():
            if ticket != self.after_ticket or self.state == "recording":
                return                           # cancelled, or you started talking again
            if foreground_window() != target:
                # You moved to another window meanwhile: Enter (or Tab) must not
                # land there and send something you did not mean to send.
                print("The window changed before the key after the dictation; "
                      "it was not pressed.")
                return
            self._typing_until = time.monotonic() + 1.0
            keyboard.send(key)

        QtCore.QTimer.singleShot(wait_ms, press)

    def cancel_after(self):
        """A click on a notice. On the waiting notice: don't press the key after
        all. On a microphone problem: open Windows' microphone settings."""
        if getattr(self.overlay, "message", "") in (self.STUCK, self.BLOCKED):
            download_help()                      # the notice stays; the download goes on
            return
        if (getattr(self.overlay, "message", "") or "").startswith(self.DOWNLOADING):
            return                               # a click on the download card does nothing
        if "microphone" in (getattr(self.overlay, "message", "") or "").lower():
            self.overlay.dismiss()
            open_mic_settings()
            return
        self.after_ticket += 1
        self.overlay.dismiss()

    @staticmethod
    def _snapshot_clipboard():
        """A copy of whatever is on the clipboard right now (None if empty)."""
        try:
            source = QtGui.QGuiApplication.clipboard().mimeData()
            formats = list(source.formats()) if source is not None else []
            if not formats:
                return None
            copy = QtCore.QMimeData()
            for fmt in formats:
                copy.setData(fmt, source.data(fmt))
            if source.hasImage():
                copy.setImageData(source.imageData())
            if source.hasUrls():
                copy.setUrls(source.urls())
            if source.hasHtml():
                copy.setHtml(source.html())
            if source.hasText():
                copy.setText(source.text())
            return copy
        except Exception:
            return None

    @staticmethod
    def _restore_clipboard(before, text):
        """Take the dictated text back off the clipboard."""
        try:
            clipboard = QtGui.QGuiApplication.clipboard()
            if clipboard.text() != text:
                return                         # you copied something else meanwhile
            if before is None:
                clipboard.clear()
            else:
                clipboard.setMimeData(before)
        except Exception:
            pass

    @staticmethod
    def _copy_privately(text):
        """Put text on the clipboard, flagged so Windows keeps it out of
        clipboard history (Win+V) and out of cloud clipboard sync."""
        clipboard = QtGui.QGuiApplication.clipboard()
        try:
            mime = QtCore.QMimeData()
            mime.setText(text)
            zero = QtCore.QByteArray(b"\x00\x00\x00\x00")
            for fmt in ("ExcludeClipboardContentFromMonitorProcessing",
                        "CanIncludeInClipboardHistory",
                        "CanUploadToCloudClipboard"):
                mime.setData('application/x-qt-windows-mime;value="%s"' % fmt, zero)
            clipboard.setMimeData(mime)
        except Exception:
            clipboard.setText(text)


ICON_BARS = (1, 3, 3, 7)                 # the icon's bar heights: 1337


def app_icon(listening=False, levels=None):
    """This program's own icon, drawn here: a rounded tile with a four-bar
    waveform in the accent colour. The bars are 1, 3, 3 and 7 units tall:
    1337, "leet". While the microphone is on, the colours swap so the tray
    icon shows it at a glance. With `levels` (recent loudness, newest first)
    the bars grow and shrink together with your voice, keeping that shape."""
    accent, dark = QtGui.QColor(ACCENT), QtGui.QColor(22, 23, 27)
    tile, bars = (accent, dark) if listening else (dark, accent)
    loudness = 1.0
    if levels:
        loud = max(0.0, min(1.0, max((list(levels) + [0.0])[:2])))
        loudness = 0.45 + 0.55 * loud        # quiet: a small 1337; loud: the full one
    icon = QtGui.QIcon()
    # Each size is drawn on its own. Shrinking the big one blurs the gaps
    # between the bars away at tray size; drawn small, they stay sharp.
    for size in (16, 20, 24, 32, 40, 48, 64):
        icon.addPixmap(_icon_pixmap(size, tile, bars, loudness))
    return icon


def _icon_pixmap(size, tile, bars, loudness=1.0):
    """The icon at one size, in whole pixels where it matters."""
    pix = QtGui.QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QtGui.QPainter(pix)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    s = size / 64.0
    edge = max(1.0, round(3 * s))
    p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 40), max(1.0, 2.0 * s)) if size >= 24
             else Qt.NoPen)
    p.setBrush(tile)
    p.drawRoundedRect(QtCore.QRectF(edge, edge, size - 2 * edge, size - 2 * edge),
                      16 * s, 16 * s)
    p.setPen(Qt.NoPen)
    p.setBrush(bars)
    if size >= 48:                           # big enough: the original proportions
        width, gap, unit = 7.0 * s, 4.0 * s, 5.4 * s * loudness
    else:                                    # small: whole pixels, at least 1 px apart
        width = float(max(2, round(7 * s)))
        gap = float(max(1, round(4 * s)))
        unit = max(1.0, round(5.4 * s * loudness * 2) / 2.0)
    left = round((size - (len(ICON_BARS) * width + (len(ICON_BARS) - 1) * gap)) / 2.0)
    middle = size / 2.0
    for i, units in enumerate(ICON_BARS):
        h = max(2.0 if size < 48 else 0.0, unit * units)     # the "1" stays visible
        if size < 48:
            h = float(round(h))
            top = round(middle - h / 2.0)
        else:
            top = middle - h / 2.0
        round_ = min(3.0 * s if size >= 48 else 1.0, h / 2.0)
        p.drawRoundedRect(QtCore.QRectF(left + i * (width + gap), top, width, h),
                          round_, round_)
    p.end()
    return pix


def tick_image():
    """Path of a small tick picture for ticked check boxes ('' if it can't be made)."""
    path = os.path.join(_DATA, "tick.png")
    try:
        if not os.path.exists(path):
            pix = QtGui.QPixmap(32, 32)
            pix.fill(Qt.transparent)
            p = QtGui.QPainter(pix)
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            pen = QtGui.QPen(QtGui.QColor(16, 17, 20), 4.5)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.drawPolyline([QtCore.QPointF(7, 17), QtCore.QPointF(13.5, 23),
                            QtCore.QPointF(25, 9.5)])
            p.end()
            pix.save(path, "PNG")
        return path.replace("\\", "/")
    except Exception:
        return ""


def dark_title_bar(window):
    """Ask Windows to draw this window's title bar dark, to match its contents."""
    try:
        value = ctypes.c_int(0 if light_menus() else 1)   # dark, or light menus
        for attribute in (20, 19):               # DWMWA_USE_IMMERSIVE_DARK_MODE
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    wintypes.HWND(int(window.winId())), attribute,
                    ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def light_menus():
    """Settings and the other windows in the light look (not in 1337 mode)."""
    return MENU_THEME == "light" and not LEET_MODE


def menu_palette():
    """The colours Qt uses for menus and dialogs: dark or light."""
    if not light_menus():
        return dark_palette()
    palette = QtGui.QPalette()
    roles = QtGui.QPalette
    for role, colour in ((roles.Window, "#F4F5F7"), (roles.WindowText, "#1B1D22"),
                         (roles.Base, "#FFFFFF"), (roles.AlternateBase, "#F0F1F4"),
                         (roles.Text, "#1B1D22"), (roles.Button, "#FFFFFF"),
                         (roles.ButtonText, "#1B1D22"), (roles.ToolTipBase, "#FFFFFF"),
                         (roles.ToolTipText, "#1B1D22"), (roles.Highlight, "#DDE0E6"),
                         (roles.HighlightedText, "#111216"),
                         (roles.PlaceholderText, "#7A7E89")):
        palette.setColor(role, QtGui.QColor(colour))
    return palette


def dark_palette():
    palette = QtGui.QPalette()
    roles = QtGui.QPalette
    for role, colour in ((roles.Window, "#16171B"), (roles.WindowText, "#E6E7EC"),
                         (roles.Base, "#1E2025"), (roles.AlternateBase, "#24262C"),
                         (roles.Text, "#E6E7EC"), (roles.Button, "#24262C"),
                         (roles.ButtonText, "#E6E7EC"), (roles.ToolTipBase, "#24262C"),
                         (roles.ToolTipText, "#E6E7EC"), (roles.Highlight, "#3A3D46"),
                         (roles.HighlightedText, "#FFFFFF"),
                         (roles.PlaceholderText, "#8E909A")):
        palette.setColor(role, QtGui.QColor(colour))
    return palette


def settings_style():
    tick = tick_image()
    return SETTINGS_STYLE + """
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid #555862; background: #1E2025; }
QCheckBox::indicator:hover { border-color: #8A8D98; }
QCheckBox::indicator:checked { background: %(accent)s; border-color: %(accent)s; %(tick)s }
QTabBar::tab:selected { color: #FFFFFF; border-bottom: 2px solid %(accent)s; }
QProgressBar::chunk { background: %(accent)s; border-radius: 3px; }
QPushButton#primary[hold="true"]:hover, QPushButton#primary[hold="true"]:pressed,
QPushButton#primary[hold="true"]:focus { border-color: %(accent)s; }
QSlider { min-height: 22px; }
QSlider::groove:horizontal { height: 4px; background: #34363E; border-radius: 2px; }
QSlider::sub-page:horizontal { height: 4px; background: %(accent)s; border-radius: 2px; }
QSlider::add-page:horizontal { height: 4px; background: #34363E; border-radius: 2px; }
QSlider::handle:horizontal { width: 14px; height: 14px; margin: -5px 0;
    border-radius: 7px; background: #F2F2F5; }
QSlider::handle:horizontal:disabled { background: #6B6D77; }
QSlider::sub-page:horizontal:disabled { background: #454852; }
""" % {"accent": ACCENT, "tick": ('image: url("%s");' % tick) if tick else ""} + (
        LEET_STYLE if LEET_MODE else LIGHT_STYLE if light_menus() else "")


# The light look for Settings and the other windows. Laid over the dark one;
# each rule starts with "QDialog" so it wins over the windows' own additions.
LIGHT_STYLE = """
QDialog { background: #F4F5F7; }
QDialog QLabel, QDialog QCheckBox { color: #1B1D22; }
QDialog QLabel#field { color: #5A5E69; }
QDialog QLabel#heading { color: #111216; }
QDialog QLabel#headline { color: #111216; }
QDialog QLabel#body { color: #4A4E59; }
QDialog QLabel#note { color: #3A3E47; }
QDialog QLabel#info { color: #6B6F7A; }
QDialog QFrame#rule { background: #DDE0E6; }
QDialog QFrame#panel { background: #FFFFFF; border: 1px solid #E0E2E7; }
QDialog QComboBox, QDialog QPushButton { background: #FFFFFF; color: #1B1D22;
    border: 1px solid #CDD0D7; }
QDialog QPushButton:hover, QDialog QComboBox:hover { background: #F0F1F4; border-color: #B5B9C2; }
QDialog QPushButton:pressed { background: #E6E8EC; }
QDialog QPushButton:disabled { color: #9A9EA8; }
QDialog QComboBox:disabled { color: #9A9EA8; background: #F4F5F7; border-color: #E0E2E7; }
QDialog QComboBox QAbstractItemView { background: #FFFFFF; color: #1B1D22;
    border: 1px solid #CDD0D7; selection-background-color: #E6E8EC; }
QDialog QCheckBox:disabled, QDialog QLabel:disabled { color: #A3A7B0; }
QDialog QCheckBox::indicator { border: 1px solid #B5B9C2; background: #FFFFFF; }
QDialog QCheckBox::indicator:hover { border-color: #7A7E89; }
QDialog QTabWidget::pane { border: 1px solid #E0E2E7; background: #FFFFFF; }
QDialog QTabBar::tab { color: #5A5E69; }
QDialog QTabBar::tab:hover { color: #1B1D22; }
QDialog QTabBar::tab:selected { color: #111216; }
QDialog QScrollBar::handle:vertical { background: #CDD0D7; }
QDialog QScrollBar::handle:vertical:hover { background: #B5B9C2; }
QDialog QSlider::groove:horizontal, QDialog QSlider::add-page:horizontal { background: #DDE0E6; }
QDialog QSlider::handle:horizontal { background: #FFFFFF; border: 1px solid #B5B9C2; }
QDialog QProgressBar { background: #E6E8EC; }
QDialog QPlainTextEdit, QDialog QLineEdit { background: #FFFFFF; color: #1B1D22;
    border: 1px solid #CDD0D7; }
QToolTip { background: #FFFFFF; color: #1B1D22; border: 1px solid #CDD0D7; }
"""


# 1337 mode's Settings: green on black in a typewriter font (the red Quit button
# stays red). Laid over the usual look, so everything else stays the same.
LEET_STYLE = """
QDialog, QWidget#tabpage { font-family: Consolas; }
QDialog { background: #050A06; }
QLabel, QCheckBox { color: #8CFFAE; font-family: Consolas; font-size: 12px; }
QLabel#field { color: #3FBF6A; font-size: 11px; }
QLabel#heading { color: #B6FFC9; }
QLabel#info { color: #2F9E57; }
QFrame#rule { background: #123A1E; }
QComboBox, QPushButton { background: #0B160E; color: #8CFFAE; border-color: #1E5A30;
    font-family: Consolas; font-size: 12px; }
QPushButton:hover, QComboBox:hover { background: #102516; border-color: #2FB85C; }
QPushButton:pressed { background: #071009; }
QComboBox QAbstractItemView { background: #0B160E; color: #8CFFAE; border-color: #1E5A30;
    selection-background-color: #174D27; }
QCheckBox::indicator { border-color: #1E5A30; background: #071009; }
QTabWidget::pane { border-color: #123A1E; background: #071009; }
QTabBar::tab { color: #3FBF6A; font-family: Consolas; font-size: 12px; }
QTabBar::tab:hover { color: #8CFFAE; }
QTabBar::tab:selected { color: #B6FFC9; }
QSlider::groove:horizontal, QSlider::add-page:horizontal { background: #123A1E; }
QSlider::handle:horizontal { background: #8CFFAE; }
QScrollBar::handle:vertical { background: #174D27; }
QProgressBar { background: #0B160E; }
QToolTip { background: #0B160E; color: #8CFFAE; border-color: #1E5A30; }
QPushButton#danger { background: #B8352F; color: #FFFFFF; border-color: #C9443D; }
"""


SETTINGS_STYLE = """
QDialog { background: #16171B; }
QLabel, QCheckBox { color: #E6E7EC; font-size: 13px; }
QLabel#field { color: #A9ABB5; }
QLabel#heading { color: #FFFFFF; font-size: 13px; font-weight: 600; }
QFrame#rule { background: #2A2C33; max-height: 1px; border: none; }
QProgressBar { background: #24262C; border: none; border-radius: 3px; max-height: 6px; }
QLabel#info { color: #8E909A; font-size: 11px; }
QComboBox, QPushButton { background: #24262C; color: #E6E7EC; font-size: 13px;
    border: 1px solid #34363E; border-radius: 6px; padding: 4px 11px; min-height: 18px; }
QPushButton:hover, QComboBox:hover { background: #2D3037; border-color: #454852; }
QPushButton:pressed { background: #1E2025; }
QPushButton:disabled { color: #8E909A; }
QComboBox:disabled { color: #6E707A; background: #1F2025; border-color: #2C2E35; }
QPushButton#danger { background: #B8352F; color: #FFFFFF; border-color: #C9443D; }
QPushButton#danger:hover { background: #CC423B; border-color: #E0574F; }
QPushButton#danger:pressed { background: #9E2D28; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView { background: #24262C; color: #E6E7EC; border: 1px solid #34363E;
    selection-background-color: #3A3D46; outline: none; }
QCheckBox { spacing: 9px; }
QCheckBox:disabled, QLabel:disabled { color: #6B6D77; }
QTabWidget::pane { border: 1px solid #2A2C33; border-radius: 10px; background: #1B1C21;
    top: -1px; }
QScrollArea#tabscroll, QWidget#tabpage { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 6px 2px 6px 0; }
QScrollBar::handle:vertical { background: #3A3D46; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #4A4E59; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QTabBar { qproperty-drawBase: 0; }
QTabBar::tab { background: transparent; color: #9A9CA6; font-size: 13px;
    padding: 7px 14px; border: none; border-bottom: 2px solid transparent; }
QTabBar::tab:hover { color: #E6E7EC; }
QToolTip { background: #24262C; color: #E6E7EC; border: 1px solid #3A3D46; padding: 6px; }
"""


# ----------------------------------------------------------------------------
# Export and import settings: one line of text to carry to another PC
# ----------------------------------------------------------------------------
# Only choices about how the app looks and behaves travel. Never: model files
# or their fingerprints, file paths, devices, window positions, or anything
# that would make the app delete, download or go online without asking. A
# pasted line is treated as untrusted: every value is checked, unknown keys are
# dropped, and options that touch the clipboard, keep text or go online are
# only switched on after the same warning Settings shows.
_PORTABLE_PREFIX = APP_TITLE.lower() + "-settings-1:"
_PORTABLE_MAX = 8000                             # characters; a real line is far shorter
_COLOUR_RE = None


def _is_colour(value, empty_ok=False):
    global _COLOUR_RE
    if _COLOUR_RE is None:
        import re
        _COLOUR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
    return isinstance(value, str) and (_COLOUR_RE.fullmatch(value) is not None
                                       or (empty_ok and value == ""))


def _is_int_in(low, high):
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and low <= v <= high


_is_hotkey = valid_hotkey                         # one rule everywhere (see there)


# key: check for its value. The ones in _PORTABLE_CAREFUL need a yes to turn on.
# 1337 mode: a hidden look (click the title in About seven times). Green on
# black, a typewriter font, the logo on the pill. Looks only: the typed text is
# never changed. Your own colours are kept: the mode only covers them while on.
LEET_MODE = bool(_config.get("leet_mode") is True)
_LEET_LOOK = {"ACCENT": "#39FF6A", "POPUP_BG": "#050A06", "GLOW_COLOUR": "",
              "GLOW_FROM_APP": False, "CARD_STYLE": "glow"}
_LEET_SAVED = {}


def apply_leet(on):
    """Put the 1337 look on (or take it off again), in memory only: nothing
    of your own look is overwritten in the settings file."""
    global LEET_MODE
    LEET_MODE = bool(on)
    if on:
        for name, value in _LEET_LOOK.items():
            _LEET_SAVED.setdefault(name, globals()[name])
            globals()[name] = value
    else:
        for name, value in _LEET_SAVED.items():
            globals()[name] = value
        _LEET_SAVED.clear()


if LEET_MODE:
    apply_leet(True)


def ui_font():
    """The pop-up's font: Segoe UI, or Consolas (a typewriter font that comes
    with Windows) in 1337 mode."""
    font = QtGui.QFont("Consolas" if LEET_MODE else "Segoe UI")
    # Letters snapped to the pixel grid: the pop-up is see-through, where
    # Windows smooths text less, so this keeps them sharp.
    font.setHintingPreference(QtGui.QFont.PreferFullHinting)
    return font


_PORTABLE_CHECKS = {
    "hotkey": lambda v: _is_hotkey(v),
    "action_hotkey": lambda v: _is_hotkey(v, empty_ok=True),
    "paste_hotkey": lambda v: _is_hotkey(v, empty_ok=True),
    "action_key": lambda v: v in [c[0] for c in AFTER_CHOICES if c[0]],
    "after_key": lambda v: v in [c[0] for c in AFTER_CHOICES],
    "after_delay": lambda v: v in [d[0] for d in AFTER_DELAYS],
    "accent": lambda v: _is_colour(v),
    "popup_bg": lambda v: _is_colour(v),
    "menu_theme": lambda v: v in ("dark", "light"),
    "glow_colour": lambda v: _is_colour(v, empty_ok=True),
    "glow": lambda v: v in [g[0] for g in GLOW_LEVELS],
    "sound_set": lambda v: v in [s[0] for s in SOUND_SETS if s[0] != "custom"],
    "volume": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    and 0.0 <= v <= 1.0,
    "text_size": lambda v: v in [t[0] for t in TEXT_SIZES],
    "live_lines": _is_int_in(1, 6),
    "card_width": _is_int_in(0, 2000),
    "max_seconds": _is_int_in(30, 3600),
    "screen_glow_strength": _is_int_in(5, 100),
    "screen_glow_size": _is_int_in(20, 200),
}
for _key in ("add_space", "always_show", "accent_outline", "outline_light", "glow_from_app",
             "hotkey_hint", "return_focus", "screen_glow", "show_app_icon", "show_app_name",
             "show_popup", "show_timer", "show_waveform", "sounds", "tray_live", "wide_card",
             "hold_to_talk", "live_preview", "type_text", "keep_last",
             "check_app_updates", "live_type", "remove_fillers",
             "textbox_only", "screen_glow_flow", "pointer_hint"):
    _PORTABLE_CHECKS[_key] = lambda v: isinstance(v, bool)
_PORTABLE_CHECKS["card_style"] = lambda v: v in [key for key, _label in CARD_STYLES]
_PORTABLE_CHECKS["screen_glow_edges"] = lambda v: v in ("bottom", "top", "left", "right", "all")
_PORTABLE_CHECKS["screen_glow_screens"] = lambda v: v in ("popup", "all", "main")
_PORTABLE_CHECKS["screen_glow_focus"] = lambda v: v in ("middle", "corners", "even")
_PORTABLE_CHECKS["pill_mark"] = lambda v: v in PILL_MARKS

# Turning these on touches the clipboard, keeps text, or goes online. For
# "type_text" it is turning it OFF that switches to pasting via the clipboard.
_PORTABLE_CAREFUL = {
    "type_text": (False, "Paste using the clipboard instead of typing"),
    "keep_last": (True, "Missed text safety (keeps your last dictation in memory "
                        "for %d minutes)" % (KEEP_LAST_SECONDS // 60)),
    "check_app_updates": (True, "Check for app updates at startup (this application will "
                                "go online, to github.com, a third-party website that we "
                                "do not run or control)"),
}


def export_settings_line():
    """This PC's choices as one line of plain text."""
    import base64
    picked = {key: _config[key] for key in sorted(_PORTABLE_CHECKS)
              if key in _config and _PORTABLE_CHECKS[key](_config[key])}
    raw = json.dumps(picked, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _PORTABLE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def read_settings_line(line):
    """Check a pasted line. Returns (good, careful, dropped, problem): the
    settings that can be applied as they are, the ones that need a yes first
    ({key: value}), how many entries were left out, and what is wrong if the
    line cannot be used at all."""
    import base64
    line = "".join(str(line or "").split())       # line breaks from e-mail or chat
    if not line:
        return {}, {}, 0, "Nothing was pasted."
    if len(line) > _PORTABLE_MAX:
        return {}, {}, 0, "That is far too long to be a settings line."
    if not line.startswith(_PORTABLE_PREFIX):
        return {}, {}, 0, ("That is not a settings line from %s. It should start with "
                           "\"%s\"." % (APP_TITLE, _PORTABLE_PREFIX))
    body = line[len(_PORTABLE_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
                          .decode("utf-8"))
    except Exception:
        return {}, {}, 0, "The line is damaged (part of it may be missing)."
    if not isinstance(data, dict):
        return {}, {}, 0, "The line is damaged."
    good, careful, dropped = {}, {}, 0
    for key, value in data.items():
        check = _PORTABLE_CHECKS.get(key) if isinstance(key, str) else None
        if check is None or not check(value):
            dropped += 1
            continue
        if key in ("after_key", "action_hotkey") and value and not _config.get(key):
            careful[key] = value                 # a key pressed for you: ask first
            continue
        risky = _PORTABLE_CAREFUL.get(key)
        if risky and value == risky[0] and _config.get(key, globals().get(
                {"type_text": "TYPE_TEXT", "keep_last": "KEEP_LAST",
                 "check_app_updates": "CHECK_APP_UPDATES"}[key])) != value:
            careful[key] = value                 # would switch it on here: ask first
        else:
            good[key] = value
    # Two hotkeys must not be the same keys.
    keys = [good.get(k, _config.get(k, globals().get(k.upper(), "")))
            for k in ("hotkey", "action_hotkey", "paste_hotkey")]
    if keys[0] and keys[0] in keys[1:]:
        for k in ("action_hotkey", "paste_hotkey"):
            if good.get(k) == keys[0]:
                good.pop(k)
                dropped += 1
    return good, careful, dropped, ""


def export_settings_window(parent):
    """Show the line to copy. The app does not put it on the clipboard itself."""
    box = QtWidgets.QDialog(parent)
    box.setWindowTitle("Export settings")
    box.setWindowIcon(app_icon())
    box.setStyleSheet(settings_style() + """
QLabel#headline { color: #FFFFFF; font-size: 15px; font-weight: 600; }
QLabel#body { color: #C4C6CF; font-size: 13px; }
QPlainTextEdit { background: #121317; color: #E6E7EC; border: 1px solid #2A2C33;
    border-radius: 6px; padding: 6px; font-family: Consolas, monospace; font-size: 12px; }
QPushButton { padding: 6px 16px; }
""")
    dark_title_bar(box)
    layout = QtWidgets.QVBoxLayout(box)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(10)
    for words, name in (("Your settings as one line", "headline"),
                        ("Select the line below and copy it (Ctrl+C), then paste it into "
                         "Import settings on another PC. %s does not put it on the "
                         "clipboard for you.\n\nIt holds only your choices: hotkeys, looks, "
                         "sounds and pop-up options. No text you dictated, no file paths, "
                         "no devices, nothing about the speech model." % APP_TITLE, "body")):
        label = QtWidgets.QLabel(words)
        label.setObjectName(name)
        label.setWordWrap(True)
        label.setFixedWidth(420)
        layout.addWidget(label)
    field = QtWidgets.QPlainTextEdit(export_settings_line())
    field.setReadOnly(True)
    field.setFixedSize(420, 110)
    field.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)
    field.selectAll()
    layout.addWidget(field)
    buttons = QtWidgets.QHBoxLayout()
    buttons.addStretch(1)
    close = QtWidgets.QPushButton("Close")
    close.clicked.connect(lambda _checked=False: box.accept())
    buttons.addWidget(close)
    layout.addLayout(buttons)
    field.setFocus()
    box.exec()


def import_settings_window(parent):
    """Paste a line, check it, and apply what is safe; ask about the rest."""
    box = QtWidgets.QDialog(parent)
    box.setWindowTitle("Import settings")
    box.setWindowIcon(app_icon())
    box.setStyleSheet(settings_style() + """
QLabel#headline { color: #FFFFFF; font-size: 15px; font-weight: 600; }
QLabel#body { color: #C4C6CF; font-size: 13px; }
QLabel#problem { color: #F2B45A; font-size: 13px; }
QPlainTextEdit { background: #121317; color: #E6E7EC; border: 1px solid #2A2C33;
    border-radius: 6px; padding: 6px; font-family: Consolas, monospace; font-size: 12px; }
QPushButton { padding: 6px 16px; }
QPushButton#primary { background: %(accent)s; color: #101114; border: 1px solid %(accent)s;
    font-weight: 600; }
""" % {"accent": ACCENT})
    dark_title_bar(box)
    layout = QtWidgets.QVBoxLayout(box)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(10)
    for words, name in (("Paste a settings line", "headline"),
                        ("Paste a line made with Export settings (Ctrl+V). Every value is "
                         "checked first. Anything that would use the clipboard, keep text "
                         "or go online is only switched on if you say yes. The "
                         "application restarts to apply the settings.", "body")):
        label = QtWidgets.QLabel(words)
        label.setObjectName(name)
        label.setWordWrap(True)
        label.setFixedWidth(420)
        layout.addWidget(label)
    field = QtWidgets.QPlainTextEdit()
    field.setFixedSize(420, 110)
    field.setPlaceholderText(_PORTABLE_PREFIX + "…")
    layout.addWidget(field)
    problem = QtWidgets.QLabel("")
    problem.setObjectName("problem")
    problem.setWordWrap(True)
    problem.setFixedWidth(420)
    problem.setVisible(False)
    layout.addWidget(problem)
    buttons = QtWidgets.QHBoxLayout()
    buttons.addStretch(1)
    cancel = QtWidgets.QPushButton("Cancel")
    cancel.clicked.connect(lambda _checked=False: box.reject())
    buttons.addWidget(cancel)
    apply_ = QtWidgets.QPushButton("Import and restart")
    apply_.setObjectName("primary")
    buttons.addWidget(apply_)
    layout.addLayout(buttons)

    def go(_checked=False):
        good, careful, dropped, why = read_settings_line(field.toPlainText())
        if why or not (good or careful):
            problem.setText(why or "The line holds no settings that can be used here.")
            problem.setVisible(True)
            box.adjustSize()
            return
        if careful:
            online = "check_app_updates" in careful
            names = "\n".join(
                "•  " + (_PORTABLE_CAREFUL[key][1] if key in _PORTABLE_CAREFUL
                         else "A key pressed for you after dictating (it can send or "
                              "submit without asking)")
                for key in careful)
            agreed = dialog(box, "Switch these on too?",
                            "This application will go online" if online
                            else "These options are off on this PC",
                            "The line would also switch on:\n\n%s\n\nThey are off by default "
                            "because they use the clipboard, keep text in memory or go "
                            "online. Leave them off unless you want them on this PC too."
                            % names,
                            accept="Switch them on", reject="Leave them off")
            if agreed is True:
                good.update(careful)
        _config.update(good)
        write_config()
        print("Imported %d settings (%d left out)." % (len(good), dropped))
        box.accept()
        note = ("%d settings imported." % len(good)
                + (" %d entries were left out (unknown or not valid)." % dropped
                   if dropped else ""))
        dialog(parent, "Settings imported", "The application will restart",
               note + "\n\nIt restarts now to use them.", accept=None, reject="Restart")
        restart_app()

    apply_.clicked.connect(go)
    box.exec()


def reset_all_settings():
    """Wipe the saved choices and start again with the defaults."""
    keep = {"welcomed": True} if _config.get("welcomed") else {}
    _config.clear()
    _config.update(keep)
    write_config()
    set_startup(False)                          # "Start with Windows" is off at first
    restart_app()


def restart_app():
    """Start a fresh copy; it takes over from this one and reads the settings anew."""
    try:
        import subprocess
        subprocess.Popen(([sys.executable] if FROZEN else [sys.executable, _SELF])
                         + [RESTART_FLAG],
                         creationflags=0x00000008 | 0x00000200, close_fds=True)
    except Exception as exc:
        print("Could not restart:", exc, file=sys.stderr)
        return
    # This copy is on its way out. From now on it saves nothing: everything
    # was saved already, and a late save could put back a one-time yes the new
    # copy has just used up.
    _config_frozen[0] = True


def on_main_screen(window):
    """Open a window centred on the main monitor (not on whichever monitor the
    mouse happens to be on). Placed before it shows: moving it afterwards
    between monitors with different scaling makes it change size."""
    try:
        screen = QtGui.QGuiApplication.primaryScreen()
        window.setScreen(screen)
        window.adjustSize()
        area = screen.availableGeometry()
        size = window.sizeHint().expandedTo(window.size())
        window.move(area.x() + (area.width() - size.width()) // 2,
                    area.y() + (area.height() - size.height()) // 2)
    except Exception as exc:
        print("Could not place a window:", exc, file=sys.stderr)


def dialog(parent, title, headline, body, accept=None, reject="Close", other=None,
           on_headline_clicks=None):
    """The app's own dialog: a headline, a paragraph at a comfortable width, and
    buttons on the right. Returns True if the accept button was chosen. With
    `other` there is a third button on the left; choosing it returns "other"."""
    box = QtWidgets.QDialog(parent)
    box.setWindowTitle(title)
    box.setWindowIcon(app_icon())
    box.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.WindowCloseButtonHint
                       | Qt.WindowStaysOnTopHint)
    box.setStyleSheet(settings_style() + """
QLabel#headline { color: #FFFFFF; font-size: 15px; font-weight: 600; }
QLabel#body { color: #C4C6CF; font-size: 13px; }
QPushButton { padding: 6px 16px; }
QPushButton#primary { background: %(accent)s; color: #101114; border: 1px solid %(accent)s;
    font-weight: 600; }
QPushButton#primary:hover { background: %(accent)s; border-color: #FFFFFF; }
""" % {"accent": ACCENT})
    dark_title_bar(box)
    layout = QtWidgets.QVBoxLayout(box)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(10)
    top = QtWidgets.QLabel(headline)
    top.setObjectName("headline")
    if on_headline_clicks is not None:          # a hidden switch (see show_about)
        clicks = {"n": 0}

        def pressed(_event):
            clicks["n"] += 1
            if clicks["n"] == 7:
                clicks["n"] = 0
                on_headline_clicks()
        top.mousePressEvent = pressed
    top.setWordWrap(True)
    top.setFixedWidth(400)
    layout.addWidget(top)
    text = QtWidgets.QLabel(body)
    text.setObjectName("body")
    text.setWordWrap(True)
    text.setFixedWidth(400)                      # wide enough to read as sentences
    text.setTextInteractionFlags(Qt.TextSelectableByMouse)
    layout.addWidget(text)
    layout.addSpacing(8)
    buttons = QtWidgets.QHBoxLayout()
    picked = {"other": False}
    if other:
        third = QtWidgets.QPushButton(other)

        def choose_other(_checked=False):
            picked["other"] = True
            box.reject()

        third.clicked.connect(choose_other)
        buttons.addWidget(third)
    buttons.addStretch(1)
    no = QtWidgets.QPushButton(reject)
    no.clicked.connect(lambda _checked=False: box.reject())
    buttons.addWidget(no)
    if accept:
        yes = QtWidgets.QPushButton(accept)
        yes.setObjectName("primary")
        yes.clicked.connect(lambda _checked=False: box.accept())
        buttons.addWidget(yes)
        no.setDefault(True)                      # Enter picks the safe choice
    layout.addLayout(buttons)
    if parent is None:
        on_main_screen(box)
    accepted = box.exec() == QtWidgets.QDialog.Accepted
    return "other" if picked["other"] else accepted


def model_check_window(parent):
    """Check the speech model's files and say, in plain words, whether they are
    all right and what to do if they are not."""
    box = QtWidgets.QDialog(parent)
    box.setWindowTitle("Speech model check")
    box.setWindowIcon(app_icon())
    box.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.WindowCloseButtonHint
                       | Qt.WindowStaysOnTopHint)
    box.setStyleSheet(settings_style() + """
QLabel#headline { color: #FFFFFF; font-size: 15px; font-weight: 600; }
QLabel#body { color: #C4C6CF; font-size: 13px; }
QLabel#good { color: #3DDC97; font-size: 12px; }
QLabel#bad { color: #FF6B6B; font-size: 12px; }
QLabel#plain { color: #A9ABB5; font-size: 12px; }
QLabel#mono { color: #8E909A; font-size: 11px; }
QPushButton { padding: 6px 14px; }
QPushButton#primary { background: %(accent)s; color: #101114; border: 1px solid %(accent)s;
    font-weight: 600; }
""" % {"accent": ACCENT})
    dark_title_bar(box)
    layout = QtWidgets.QVBoxLayout(box)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(8)
    layout.setSizeConstraint(QtWidgets.QLayout.SetFixedSize)   # grows to fit its text

    def label(words, name="body", width=440):
        made = QtWidgets.QLabel(words)
        made.setObjectName(name)
        made.setWordWrap(True)
        made.setFixedWidth(width)
        made.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return made

    headline = label("Checking…", "headline")
    layout.addWidget(headline)
    summary = label("")
    layout.addWidget(summary)
    layout.addSpacing(4)
    rows = {}
    for name in MODEL_FILES:
        line = QtWidgets.QHBoxLayout()
        line.addWidget(label(name, "plain", 190))
        state = label("", "plain", 250)
        line.addWidget(state)
        layout.addLayout(line)
        rows[name] = state
    layout.addSpacing(4)
    details = label("", "mono")
    details.setVisible(False)
    layout.addWidget(details)
    layout.addSpacing(6)
    buttons = QtWidgets.QHBoxLayout()
    again = QtWidgets.QPushButton("Download the model again")
    again.setVisible(False)                      # offered when something is wrong
    show = QtWidgets.QPushButton("Show fingerprints")
    close = QtWidgets.QPushButton("Close")
    for button in (again, show):
        buttons.addWidget(button)
    buttons.addStretch(1)
    buttons.addWidget(close)
    layout.addLayout(buttons)
    work = {"result": None, "busy": False}

    def paint(name, words, kind):
        rows[name].setText(words)
        rows[name].setObjectName(kind)
        rows[name].setStyleSheet("")             # picks up the new colour
        rows[name].setStyleSheet({"good": "color: #3DDC97;", "bad": "color: #FF6B6B;"}
                                 .get(kind, "color: #A9ABB5;"))

    def verdict(problems):
        if problems:
            headline.setText("Something is wrong with the model's files")
            summary.setText(
                "%s not what %s expected. The files may have been damaged, replaced, "
                "or only partly downloaded.\n\nWhat to do: click \"Download the model "
                "again\". It deletes these files and fetches fresh copies, after asking "
                "you first. If the same thing happens with fresh copies, do not use "
                "them, and report it on the project's page."
                % ("1 file is" if len(problems) == 1 else "%d files are" % len(problems),
                   APP_TITLE))
            again.setVisible(True)
            again.setObjectName("primary")
            again.setStyleSheet("")
        else:
            headline.setText("The model's files are the official ones")
            summary.setText(
                "Every file matches the fingerprint built into %s, so it is exactly "
                "the file published on this project's model page. %s checks them again "
                "each time it starts, on this PC, without going online. Nothing needs "
                "doing." % (APP_TITLE, APP_TITLE))
        box.adjustSize()

    def local_check():
        files = model_files()
        if not files:
            headline.setText("No model files were found")
            summary.setText("The speech model has not been downloaded yet.")
            for name in MODEL_FILES:
                paint(name, "not found", "plain")
            show.setEnabled(False)
            return
        changed = model_mismatches()
        for name in MODEL_FILES:
            if name not in files:
                paint(name, "missing", "bad")
            elif name in changed:
                paint(name, "does NOT match the official file", "bad")
            else:
                paint(name, "matches the official file", "good")
        verdict([n for n in MODEL_FILES if n in changed or n not in files])

    def show_details():
        marks = []
        for name, path in sorted(model_files().items()):
            try:
                marks.append("%s\n%s" % (name, file_fingerprint(path)))
            except Exception as exc:
                marks.append("%s\ncould not be read (%s)" % (name, exc))
        details.setText("SHA-256 fingerprints, for anyone who wants to compare them by "
                        "hand:\n\n%s\n\nFolder: %s"
                        % ("\n\n".join(marks), model_location()[0] or ""))
        details.setVisible(True)
        show.setEnabled(False)
        box.adjustSize()

    def download_again():
        if dialog(box, "Download the model again?", "Delete these files and start afresh",
                  "The model's files on this PC will be deleted and the application "
                  "will restart. It then asks before downloading fresh copies, as it "
                  "did the first time.", accept="Delete and restart", reject="Cancel"):
            forget_model()
            restart_app()

    again.clicked.connect(lambda _checked=False: download_again())
    show.clicked.connect(lambda _checked=False: show_details())
    close.clicked.connect(lambda _checked=False: box.reject())
    local_check()
    box.exec()


STUCK_HELP = ("Some of the speech model has arrived, but the rest is not coming through. "
              "The model comes from this project's model page on github.com, and GitHub "
              "sends the files themselves from release-assets.githubusercontent.com. "
              "When nothing more arrives, something between this PC and those addresses "
              "is usually blocking them: a firewall, an ad blocker, or a DNS filter (a "
              "Pi-hole, for example).\n\n"
              "What you can do:\n\n"
              "1.  Allow github.com and release-assets.githubusercontent.com in whatever "
              "is filtering your network, then start %s again. It carries on from where "
              "it stopped.\n\n"
              "2.  Or download the model yourself with your web browser, on this PC or "
              "another one. Click \"Manual download\" for the steps.\n\n"
              "%s tries again by itself every 15 seconds or so. To make it try "
              "right now, for instance after changing a setting on your network, "
              "right-click the download card. If you have just switched a filter off, "
              "Windows may remember the old answer for a while: restarting the PC, or "
              "running \"ipconfig /flushdns\" in a command window, clears that.\n\n"
              "To stop it and close the application, click \"Quit %s\".")


def download_help():
    """What to do when the model's download has stopped moving."""
    choice = dialog(None, "%s: download not moving" % APP_TITLE,
                    "The download has stopped moving", STUCK_HELP % (APP_TITLE, APP_TITLE, APP_TITLE),
                    accept="Manual download", reject="Close", other="Quit %s" % APP_TITLE)
    if choice is True:
        manual_download_steps()
    elif choice == "other":
        # Stop trying and close the application. What has arrived so far is
        # kept, so a later start carries on from there (after asking first).
        print("Quit from the download help window.")
        leave_now(QtWidgets.QApplication.instance())


def manual_download_steps():
    """Explain how to fetch the model by hand, for people who would rather this
    program never went online at all. The link to the model's page stays hidden
    until it is asked for: it is someone else's website, and going there is the
    user's own decision."""
    folder, helper = model_dir(), helper_dir()
    release = current_release()
    page = release_page(release) if release else MODELS_PAGE
    try:
        os.makedirs(folder, exist_ok=True)
        os.makedirs(helper, exist_ok=True)
    except Exception:
        pass
    box = QtWidgets.QDialog(None)
    box.setWindowTitle("%s: manual download" % APP_TITLE)
    box.setWindowIcon(app_icon())
    box.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.WindowCloseButtonHint
                       | Qt.WindowStaysOnTopHint)
    box.setStyleSheet(settings_style() + """
QLabel#headline { color: #FFFFFF; font-size: 15px; font-weight: 600; }
QLabel#body { color: #C4C6CF; font-size: 13px; }
QLabel#aside { color: #A9ABB5; font-size: 12px; font-style: italic; }
QLabel#link { color: #FFFFFF; font-size: 13px; }
QPushButton { padding: 6px 16px; }
QPushButton#primary { background: %(accent)s; color: #101114; border: 1px solid %(accent)s;
    font-weight: 600; }
QPushButton#primary:hover { background: %(accent)s; border-color: #FFFFFF; }
""" % {"accent": ACCENT})
    dark_title_bar(box)
    layout = QtWidgets.QVBoxLayout(box)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(10)

    def text(words, name="body"):
        label = QtWidgets.QLabel(words)
        label.setObjectName(name)
        label.setWordWrap(True)
        label.setFixedWidth(420)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(label)
        return label

    text("Download the speech model yourself", "headline")
    text("%s will not go online. Fetch the model with your web browser instead "
         "(about 660 MB), on this PC or any other." % APP_TITLE)
    text("1.  Open this project's model page on github.com.")
    text("Please note: github.com is a third-party website that we do not run or "
         "control. Opening it and downloading from it is your choice.", "aside")
    reveal = QtWidgets.QPushButton("Click to view the link")
    link = text(page, "link")
    link.setVisible(False)                       # shown only when asked for
    copy = QtWidgets.QPushButton("Copy link")
    copy.setVisible(False)

    def show_link(_checked=False):
        reveal.setVisible(False)
        link.setVisible(True)
        copy.setVisible(True)
        box.adjustSize()

    def copy_link(_checked=False):
        QtGui.QGuiApplication.clipboard().setText(page)
        copy.setText("Copied")

    reveal.clicked.connect(show_link)
    copy.clicked.connect(copy_link)
    row = QtWidgets.QHBoxLayout()
    row.addWidget(reveal)
    row.addWidget(copy)
    row.addStretch(1)
    layout.addLayout(row)
    text("2.  Under %s, download these four files and put them in this folder:\n%s"
         "\n\n%s" % (release["tag"] if release else "the newest release",
                     "\n".join("     " + name for name in MODEL_FILES), folder))
    text("3.  For long dictations, also download silero_vad.onnx and config.json "
         "from the release %s (on the same page's list of releases), and put them in "
         "this folder:\n%s" % (HELPER_RELEASE["tag"], helper))
    text("4.  Start %s again. It checks every file against its fingerprint, "
         "downloads nothing, and goes straight to the introduction." % APP_TITLE)
    layout.addSpacing(8)
    buttons = QtWidgets.QHBoxLayout()
    buttons.addStretch(1)
    close = QtWidgets.QPushButton("Close")
    close.clicked.connect(lambda _checked=False: box.reject())
    buttons.addWidget(close)
    opener = QtWidgets.QPushButton("Open the folder")
    opener.setObjectName("primary")
    opener.clicked.connect(lambda _checked=False: box.accept())
    buttons.addWidget(opener)
    layout.addLayout(buttons)
    if box.exec() == QtWidgets.QDialog.Accepted:
        try:
            os.startfile(folder)
        except Exception as exc:
            print("Could not open the folder:", exc, file=sys.stderr)


APP_UPDATE_WARNING = (
    "%(app)s will connect to github.com to read the version number of its latest "
    "published release. That is a third-party website that we do not run or "
    "control.\n\nThis is done by a small separate helper; %(app)s itself stays "
    "blocked from the network. Nothing about you or your use of %(app)s is sent, "
    "apart from what any visit to a website involves (such as your IP address). "
    "Nothing is downloaded or installed: if there is a newer version, you are told "
    "and can open its download page yourself.")


def ask_app_update_check(parent, at_startup=False):
    """The warning before the application update check goes online."""
    if at_startup:
        return dialog(parent, "Check for app updates at startup?",
                      "This application will go online",
                      "With this switched on, every time it starts, " + APP_UPDATE_WARNING
                      % {"app": APP_TITLE}, accept="Turn on", reject="Keep off") is True
    return dialog(parent, "Check for app updates?", "This application will go online",
                  "Once, now, " + APP_UPDATE_WARNING % {"app": APP_TITLE},
                  accept="Check now", reject="Cancel") is True


_app_update_work = {"busy": False}


def run_app_update_check(parent=None, manual=True, done=None):
    """Check for a newer version in the background, then say what was found.
    manual=False (the check at startup) says nothing unless there is a newer
    version, and mentions each new version only once."""
    if _app_update_work["busy"]:
        return
    _app_update_work.update(busy=True, result=None)

    def work():
        _app_update_work["result"] = fetch_latest_release()

    threading.Thread(target=work, daemon=True).start()
    poll = QtCore.QTimer(QtWidgets.QApplication.instance())
    poll.setInterval(250)

    def check():
        if _app_update_work.get("result") is None:
            return
        poll.stop()
        poll.deleteLater()
        _app_update_work["busy"] = False
        tag, page, installer, why = _app_update_work["result"]
        if done is not None:
            done()
        show_app_update_result(parent, tag, page, why, manual, installer)

    poll.timeout.connect(check)
    poll.start()


def show_app_update_result(parent, tag, page, why, manual, installer=""):
    if why is not None:
        print("App update check: github.com could not be reached (%s)." % why)
        if manual:
            dialog(parent, "Check for app updates", "Could not check for updates",
                   "github.com could not be reached, so nothing was checked.\n\n(%s)" % why)
        return
    if not tag or not version_tuple(tag):
        print("App update check: no published version found.")
        if manual:
            dialog(parent, "Check for app updates", "No published version found",
                   "No version of %s has been published on github.com yet." % APP_TITLE)
        return
    latest = tag.lstrip("vV")
    minimum = _app_update_work.get("minimum") or ""
    if minimum and version_tuple(VERSION) < version_tuple(minimum) \
            and version_tuple(latest) > version_tuple(VERSION):
        # This version has been withdrawn: say so plainly, even at startup.
        print("App update check: this version (%s) is below the minimum safe "
              "version %s." % (VERSION, minimum))
        offer_update(parent, latest, page, installer, unsafe=True)
        return
    if version_tuple(latest) <= version_tuple(VERSION):
        print("App update check: up to date (%s)." % VERSION)
        if manual:
            dialog(parent, "Check for app updates", "You have the latest version",
                   "%s %s is the newest published version." % (APP_TITLE, VERSION))
        return
    print("App update check: version %s is available (this is %s)." % (latest, VERSION))
    if manual:
        offer_update(parent, latest, page, installer)    # asked for: answer straight away
        return
    # At startup: a notification by the clock (once per new version) and an
    # "Update available" line in the tray menu (for as long as it is newer),
    # instead of a window popping up in the middle of whatever you are doing.
    announce_update(latest, page, installer,
                    notify=_config.get("app_update_seen") != latest)
    save_setting("app_update_seen", latest)


_update_offer = {"connected": False, "action": None, "args": None}


def announce_update(latest, page, installer, notify=True):
    """Tell about a newer version without interrupting: a Windows notification
    (clicking it opens the update window) and a line in the tray menu. Without
    a tray icon, the window itself is shown."""
    app = QtWidgets.QApplication.instance()
    tray = getattr(app, "_tray_icon", None)
    _update_offer["args"] = (latest, page, installer)
    if tray is None:
        offer_update(None, latest, page, installer)
        return

    def open_offer(*_ignored):
        if _update_offer["args"]:
            offer_update(None, *_update_offer["args"])

    if not _update_offer["connected"]:
        tray.messageClicked.connect(open_offer)
        _update_offer["connected"] = True
    menu = tray.contextMenu()
    if menu is not None:
        if _update_offer["action"] is None:
            action = QtGui.QAction(menu)
            action.triggered.connect(open_offer)
            first = menu.actions()[0] if menu.actions() else None
            menu.insertAction(first, action)
            menu.insertSeparator(first)
            _update_offer["action"] = action
        _update_offer["action"].setText("Update available (%s)…" % latest)
    if notify:
        tray.showMessage("%s update available" % APP_TITLE,
                         "Version %s is ready to download. Click for details." % latest,
                         app_icon(), 10000)


def offer_update(parent, latest, page, installer, unsafe=False):
    """The update window: Download now / What's new / Not now. unsafe=True:
    the newest release says this version should no longer be used."""
    if installer:
        how = ("Download now opens the new installer in your web browser, which "
               "downloads it from github.com. Then run it: your settings and speech "
               "model are kept.")
    else:
        how = ("What's new opens the release page, where you can download the new "
               "installer and run it. Your settings and speech model are kept.")
    if unsafe:
        headline = "Please update %s" % APP_TITLE
        why = ("A problem was found in earlier versions, including yours (%s), and "
               "they should no longer be used. Version %s fixes it. Details are on "
               "the release page (What's new)." % (VERSION, latest))
    else:
        headline = "%s %s is available" % (APP_TITLE, latest)
        why = "You have version %s." % VERSION
    choice = dialog(parent, "%s update" % APP_TITLE, headline,
                    "%s\n\n%s\n\ngithub.com is a third-party website that we do not run "
                    "or control. Nothing has been downloaded yet." % (why, how),
                    accept="Download now" if installer else "What's new",
                    reject="Not now", other="What's new" if installer else None)
    if choice is True:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(installer or page))
    elif choice == "other":
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(page))


def open_feedback_page(parent, kind):
    """Open the GitHub form for a bug report or an idea, in the web browser,
    after the third-party notice. The application itself does not go online."""
    form = "bug_report.yml" if kind == "bug" else "feature_request.yml"
    what = "report a problem" if kind == "bug" else "suggest an idea"
    if dialog(parent, "Open github.com?", "This opens a page in your web browser",
              "To %s, %s opens a form on github.com in your web browser. That is a "
              "third-party website that we do not run or control; you need a free "
              "GitHub account to send the form, and what you write there is public.\n\n"
              "Please do not include anything private. %s itself does not go "
              "online for this." % (what, APP_TITLE, APP_TITLE),
              accept="Open the page", reject="Cancel") is True:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(
            "https://github.com/%s/issues/new?template=%s" % (APP_REPO, form)))


def replacements_window(parent):
    """Edit the word replacements: what the speech model writes, and what you
    want instead. Empty rows are ignored; saved when the window closes."""
    box = QtWidgets.QDialog(parent)
    box.setWindowTitle("Word replacements")
    box.setWindowIcon(app_icon())
    box.setStyleSheet(settings_style())
    dark_title_bar(box)
    layout = QtWidgets.QVBoxLayout(box)
    layout.setContentsMargins(20, 18, 20, 16)
    note = QtWidgets.QLabel(
        "The speech model can't learn new words, so names it doesn't know come out "
        "as something close (\"leet\" as \"leaked\"). Add what it writes on the "
        "left and what you want on the right. Whole words only; capitals don't "
        "matter. Kept in your settings on this PC.")
    note.setObjectName("info")
    note.setWordWrap(True)
    note.setFixedWidth(440)
    layout.addWidget(note)
    table = QtWidgets.QTableWidget(max(12, len(REPLACEMENTS) + 4), 2)
    table.setHorizontalHeaderLabels(["It writes", "You want"])
    table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
    table.verticalHeader().setVisible(False)
    table.setFixedWidth(440)
    table.setMinimumHeight(300)
    for row_index, (heard, write) in enumerate(REPLACEMENTS):
        table.setItem(row_index, 0, QtWidgets.QTableWidgetItem(heard))
        table.setItem(row_index, 1, QtWidgets.QTableWidgetItem(write))
    layout.addWidget(table)
    buttons = QtWidgets.QHBoxLayout()

    def add_tech_terms():
        """Fill in common tech terms (MB, GHz, JavaScript...) that aren't in
        the list yet, in the first empty rows; remove any you don't want."""
        have = {(" ".join((table.item(r, 0).text() if table.item(r, 0) else "").lower().split()))
                for r in range(table.rowCount())}
        missing = [pair for pair in TECH_TERMS if pair[0] not in have]
        empty = [r for r in range(table.rowCount())
                 if not (table.item(r, 0) and table.item(r, 0).text().strip())]
        rows_now = sum(1 for r in range(table.rowCount())
                       if table.item(r, 0) and table.item(r, 0).text().strip())
        missing = missing[:max(0, REPLACE_MAX - rows_now)]
        if len(empty) < len(missing):
            table.setRowCount(table.rowCount() + len(missing) - len(empty))
            empty = [r for r in range(table.rowCount())
                     if not (table.item(r, 0) and table.item(r, 0).text().strip())]
        for (heard, write), r in zip(missing, empty):
            table.setItem(r, 0, QtWidgets.QTableWidgetItem(heard))
            table.setItem(r, 1, QtWidgets.QTableWidgetItem(write))

    tech = QtWidgets.QPushButton("Add tech terms")
    tech.setToolTip("Adds common tech terms written the short way: megabytes as MB, "
                    "gigahertz as GHz, java script as JavaScript and more. Remove any "
                    "you don't want.")
    tech.clicked.connect(lambda _checked=False: add_tech_terms())
    buttons.addWidget(tech)
    buttons.addStretch(1)
    done = QtWidgets.QPushButton("Save")
    done.clicked.connect(lambda _checked=False: box.accept())
    buttons.addWidget(done)
    layout.addLayout(buttons)
    box.exec()                                   # Save, the X or Esc: kept either way
    pairs = []
    for row_index in range(table.rowCount()):
        cells = [table.item(row_index, col) for col in (0, 1)]
        heard, write = [(" ".join(c.text().split()) if c else "")[:REPLACE_LEN] for c in cells]
        heard = "".join(c for c in heard if plain_text(c))      # pasted oddities out
        write = "".join(c for c in write if plain_text(c))
        if heard and write and len(pairs) < REPLACE_MAX:
            pairs.append([heard, write])
    globals()["REPLACEMENTS"] = pairs
    save_setting("replacements", pairs)


def uninstaller_path():
    """The installed copy's own uninstaller, or None (running from Python, or
    not found)."""
    if not FROZEN:
        return None
    folder = os.path.dirname(os.path.abspath(sys.executable))
    path = os.path.join(folder, "unins000.exe")
    # Only the installer's own uninstaller: it always comes with its .dat file.
    if os.path.isfile(path) and os.path.isfile(os.path.join(folder, "unins000.dat")):
        return path
    return None


def start_uninstall(parent):
    """Settings > Privacy: start the real uninstaller (the same one as in
    Windows' list of installed apps) and close, so the files can go. The
    uninstaller asks itself whether to remove settings and the speech model."""
    path = uninstaller_path()
    if not path:
        return
    if not dialog(parent, "Uninstall", "Uninstall %s?" % APP_TITLE,
                  "%s will close and Windows' uninstaller will start. It asks "
                  "whether to remove your settings and the speech model as well, "
                  "or to keep them for later." % APP_TITLE,
                  accept="Uninstall", reject="Cancel"):
        return
    import subprocess
    try:
        subprocess.Popen([path], close_fds=True)
    except Exception as exc:
        print("Could not start the uninstaller:", exc, file=sys.stderr)
        return
    print("Uninstall started from Settings; closing.")
    leave_now()


def open_data_folder():
    try:
        os.startfile(_DATA)                      # Windows: opens it in Explorer
    except Exception as exc:
        print("Could not open the data folder:", exc, file=sys.stderr)


def swatch_picture(colour, chosen, ratio):
    """A colour circle for Settings, drawn with smooth edges at the screen's
    real resolution. The chosen one gets a thin ring with a gap around it."""
    size = 24
    picture = QtGui.QPixmap(int(round(size * ratio)), int(round(size * ratio)))
    picture.fill(Qt.transparent)
    p = QtGui.QPainter(picture)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.scale(ratio, ratio)
    middle = QtCore.QPointF(size / 2.0, size / 2.0)
    light = light_menus()
    # On the light look a very light colour gets a faint edge, or it is lost.
    faint = light and QtGui.QColor(colour).lightnessF() > 0.85
    p.setPen(QtGui.QPen(QtGui.QColor("#B5B9C2"), 1.0) if faint else Qt.NoPen)
    p.setBrush(QtGui.QColor(colour))
    dot = 7.0 if chosen else 9.0
    p.drawEllipse(middle, dot, dot)
    if chosen:
        p.setBrush(Qt.NoBrush)
        p.setPen(QtGui.QPen(QtGui.QColor("#1B1D22" if light else "#FFFFFF"), 1.6))
        p.drawEllipse(middle, 10.4, 10.4)
    p.end()
    picture.setDevicePixelRatio(ratio)
    return picture




class Sketch(QtWidgets.QWidget):
    """A small picture for a page of the introduction, drawn here (no image
    files). `kind` picks the picture; it redraws from the current settings, so
    it follows the hotkey and colours as they are chosen."""

    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        # The little screen needs room; the keys need less (owner: wasted space).
        self.setFixedHeight({"look": 116, "hotkey": 60, "tips": 74}.get(kind, 96))

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        try:
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            getattr(self, "_" + self.kind)(p, float(self.width()), float(self.height()))
        except Exception as exc:                 # a picture must never break the window
            print("Sketch could not be drawn:", exc, file=sys.stderr)
        finally:
            p.end()

    @staticmethod
    def _edge():
        """Outlines in the pictures: soft on the light look, as on the dark."""
        return "#C4C8CF" if light_menus() else "#4A4D57"

    @staticmethod
    def _quiet():
        """Small captions in the pictures."""
        return "#6B6F7A" if light_menus() else "#8E909A"

    @staticmethod
    def _font(size, bold=False):
        font = ui_font()
        font.setPixelSize(size)
        if bold:
            font.setWeight(QtGui.QFont.DemiBold)
        return font

    @staticmethod
    def _wave(p, x1, x2, y, height, colour, width=2.0):
        path = QtGui.QPainterPath()
        steps = max(8, int((x2 - x1) / 2))
        for i in range(steps + 1):
            u = i / float(steps)
            swell = math.sin(math.pi * u) ** 0.7 * (0.55 + 0.45 * math.sin(u * 9.0 + 1.0) ** 2)
            point = QtCore.QPointF(x1 + u * (x2 - x1),
                                   y + math.sin(u * (x2 - x1) * 0.2) * height * swell)
            if i == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        pen = QtGui.QPen(QtGui.QColor(colour), width)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

    def _card(self, p, x, y, w, glow=True):
        """A little copy of the pop-up: icon, wave, timer, a line of words."""
        edge = QtGui.QColor(glow_base())
        if glow:
            # The same soft, truly blurred glow the real pop-up has.
            margin = 18.0
            soft = None
            try:
                soft = glow_pixmap(w, 58.0, 14.0, edge, margin, 8.0, self.devicePixelRatioF())
            except Exception:
                soft = None
            if soft is not None:
                p.setOpacity(0.55)
                p.drawPixmap(QtCore.QRectF(x - margin, y - margin, w + 2 * margin,
                                           58 + 2 * margin), soft, QtCore.QRectF(soft.rect()))
                p.setOpacity(1.0)
        p.setPen(QtGui.QPen(edge, 1.2))
        p.setBrush(QtGui.QColor(POPUP_BG))
        p.drawRoundedRect(QtCore.QRectF(x, y, w, 58), 14, 14)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(ACCENT))         # the app's own mark: bars 1, 3, 3, 7
        bar, gap = 2.6, 1.6
        for i, units in enumerate(ICON_BARS):
            tall = max(2.6, 2.0 * units)
            p.drawRoundedRect(QtCore.QRectF(x + 13 + i * (bar + gap), y + 18 - tall / 2.0,
                                            bar, tall), bar / 2.0, bar / 2.0)
        self._wave(p, x + 38, x + w - 44, y + 18, 6.0, ACCENT, 1.8)
        light = is_light(POPUP_BG)
        p.setFont(self._font(11))
        p.setPen(QtGui.QColor("#6E6E76" if light else "#AAACB6"))
        p.drawText(QtCore.QRectF(x + w - 42, y + 8, 30, 20), Qt.AlignRight | Qt.AlignVCenter,
                   "0:03")
        p.setPen(QtGui.QColor("#3C3C43" if light else "#D6D8E0"))
        p.drawText(QtCore.QRectF(x + 12, y + 32, w - 24, 18), Qt.AlignHCenter | Qt.AlignVCenter,
                   "Your words appear here")

    def _welcome(self, p, w, h):
        icon = app_icon().pixmap(128, 128)
        p.drawPixmap(QtCore.QRectF(w / 2 - 110, h / 2 - 28, 56, 56), icon,
                     QtCore.QRectF(icon.rect()))
        self._wave(p, w / 2 - 36, w / 2 + 110, h / 2, 16.0, ACCENT, 2.4)

    def _hotkey(self, p, w, h):
        keys = [pretty(part) for part in HOTKEY.split("+") if part]
        p.setFont(self._font(13, bold=True))
        metrics = QtGui.QFontMetricsF(p.font())
        sizes = [max(46.0, metrics.horizontalAdvance(key) + 26.0) for key in keys]
        total = sum(sizes) + 22.0 * (len(keys) - 1)
        x = (w - total) / 2.0
        light = light_menus()                    # keys to match the window's look
        for i, (key, size) in enumerate(zip(keys, sizes)):
            p.setPen(QtGui.QPen(QtGui.QColor(self._edge()), 1.2))
            p.setBrush(QtGui.QColor("#FFFFFF" if light else "#24262C"))
            p.drawRoundedRect(QtCore.QRectF(x, h / 2 - 20, size, 40), 8, 8)
            p.setPen(QtGui.QColor("#1B1D22" if light else "#F0F0F4"))
            p.drawText(QtCore.QRectF(x, h / 2 - 20, size, 40), Qt.AlignCenter, key)
            x += size
            if i < len(keys) - 1:
                p.setPen(QtGui.QColor(self._quiet()))
                p.drawText(QtCore.QRectF(x, h / 2 - 20, 22, 40), Qt.AlignCenter, "+")
                x += 22.0

    def _look(self, p, w, h):
        # A little screen, with the glow round its edge if that is switched on.
        screen = QtCore.QRectF(w / 2 - 150, 4, 300, h - 8)
        light = light_menus()                    # a light grey screen in the light look
        p.setPen(QtGui.QPen(QtGui.QColor("#D5D8DE" if light else "#34363E"), 1.0))
        p.setBrush(QtGui.QColor("#E9EBEF" if light else "#0E0F12"))
        p.drawRoundedRect(screen, 6, 6)
        if SCREEN_GLOW:
            # The screen glow in miniature: light coming in from each edge and
            # fading smoothly, as strong and as deep as the sliders say.
            depth = 5.0 + SCREEN_GLOW_SIZE * 0.11
            peak = int(40 + 175 * SCREEN_GLOW_STRENGTH / 100.0)
            tint = glow_base()
            shape = QtGui.QPainterPath()
            shape.addRoundedRect(screen, 6, 6)
            p.save()
            p.setClipPath(shape)
            p.setPen(Qt.NoPen)
            edges = (
                (QtCore.QRectF(screen.left(), screen.top(), screen.width(), depth),
                 (screen.left(), screen.top()), (screen.left(), screen.top() + depth)),
                (QtCore.QRectF(screen.left(), screen.bottom() - depth, screen.width(), depth),
                 (screen.left(), screen.bottom()), (screen.left(), screen.bottom() - depth)),
                (QtCore.QRectF(screen.left(), screen.top(), depth, screen.height()),
                 (screen.left(), screen.top()), (screen.left() + depth, screen.top())),
                (QtCore.QRectF(screen.right() - depth, screen.top(), depth, screen.height()),
                 (screen.right(), screen.top()), (screen.right() - depth, screen.top())))
            for area, start, end in edges:
                fall = QtGui.QLinearGradient(QtCore.QPointF(*start), QtCore.QPointF(*end))
                for position, share in ((0.0, 1.0), (0.2, 0.5), (0.5, 0.16), (1.0, 0.0)):
                    shade = QtGui.QColor(tint)
                    shade.setAlpha(int(peak * share))
                    fall.setColorAt(position, shade)
                p.fillRect(area, QtGui.QBrush(fall))
            p.restore()
        # The pop-up sits in the middle of the little screen, clear of its edges.
        self._card(p, w / 2 - 82, screen.center().y() - 29, 164)

    def _mic(self, p, w, h):
        # A microphone, with the wave coming off it.
        body = QtGui.QColor("#C4C6CF")
        p.setPen(Qt.NoPen)
        p.setBrush(body)
        p.drawRoundedRect(QtCore.QRectF(w / 2 - 92, h / 2 - 30, 22, 38), 11, 11)
        p.setBrush(Qt.NoBrush)
        p.setPen(QtGui.QPen(body, 2.2))
        p.drawArc(QtCore.QRectF(w / 2 - 99, h / 2 - 18, 36, 34), 180 * 16, 180 * 16)
        p.drawLine(QtCore.QPointF(w / 2 - 81, h / 2 + 16), QtCore.QPointF(w / 2 - 81, h / 2 + 26))
        p.drawLine(QtCore.QPointF(w / 2 - 91, h / 2 + 26), QtCore.QPointF(w / 2 - 71, h / 2 + 26))
        self._wave(p, w / 2 - 44, w / 2 + 104, h / 2 - 4, 15.0, ACCENT, 2.4)

    def _tips(self, p, w, h):
        # The resting pill, with the two ways to use it written either side.
        text = pretty(HOTKEY) if SHOW_HOTKEY_HINT else "Ready"
        p.setFont(self._font(12))
        pill_w = QtGui.QFontMetricsF(p.font()).horizontalAdvance(text) + 52.0
        mid = 26.0                               # the pill's middle; a caption below
        pill = QtCore.QRectF(w / 2 - pill_w / 2, mid - 18, pill_w, 36)
        p.setPen(QtGui.QPen(QtGui.QColor(self._edge()), 1.2))
        p.setBrush(QtGui.QColor(POPUP_BG))
        p.drawRoundedRect(pill, 18, 18)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(ACCENT))
        p.drawEllipse(QtCore.QPointF(pill.left() + 18, mid), 4, 4)
        p.setPen(QtGui.QColor("#1D1D1F" if is_light(POPUP_BG) else "#F0F0F4"))
        p.drawText(pill.adjusted(30, 0, -8, 0), Qt.AlignLeft | Qt.AlignVCenter, text)
        p.setFont(self._font(11))
        p.setPen(QtGui.QColor(self._quiet()))
        p.drawText(QtCore.QRectF(0, mid - 18, pill.left() - 12, 36),
                   Qt.AlignRight | Qt.AlignVCenter, "Click it: dictate")
        p.drawText(QtCore.QRectF(pill.right() + 12, mid - 18, w - pill.right() - 12, 36),
                   Qt.AlignLeft | Qt.AlignVCenter, "Right-click it: Settings")
        # Said plainly: this is the pill on the screen, not a button here (owner).
        p.drawText(QtCore.QRectF(0, pill.bottom() + 4, w, 18), Qt.AlignHCenter | Qt.AlignTop,
                   "The pill on your screen")


class ChoiceButton(QtWidgets.QPushButton):
    """A choice button (such as Dark / Light) drawn by hand, so its rounded
    border is smooth: style-sheet borders are not anti-aliased and their
    corners look jagged, most of all in the bright colour of a chosen one."""

    def __init__(self, text, radius=6.0, height=28):
        super().__init__(text)
        self._chosen = False
        self._radius = float(radius)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover)
        self.setFixedHeight(height)
        self.setMinimumWidth(self.fontMetrics().horizontalAdvance(text) + 32)

    def setChosen(self, chosen):
        self._chosen = bool(chosen)
        self.update()

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        hover = self.underMouse()
        light = light_menus()
        if light:
            fill = QtGui.QColor("#F0F1F4" if hover else "#FFFFFF")
            if self.isDown():
                fill = QtGui.QColor("#E6E8EC")
            edge = QtGui.QColor(accent_on_light(QtGui.QColor(ACCENT))) if self._chosen \
                else QtGui.QColor("#B5B9C2" if hover else "#CDD0D7")
        else:
            fill = QtGui.QColor("#2D3037" if hover else "#24262C")
            if self.isDown():
                fill = QtGui.QColor("#1E2025")
            edge = QtGui.QColor(ACCENT) if self._chosen else QtGui.QColor(
                "#454852" if hover else "#34363E")
        p.setPen(QtGui.QPen(edge, 1.0))
        p.setBrush(fill)
        p.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                          self._radius, self._radius)
        p.setPen(QtGui.QColor("#1B1D22" if light else "#E6E7EC"))
        p.setFont(self.font())
        p.drawText(self.rect(), Qt.AlignCenter, self.text())
        p.end()


class HoldButton(QtWidgets.QPushButton):
    """A button that has to be held down for a few seconds: it fills up while
    held and counts down; letting go early drains it back and starts over.
    Holding Space or Enter works the same, for the keyboard. Used where a
    quick click should not be enough, so that the words above it get read."""

    completed = QtCore.Signal()
    FILL_STYLE = "lighter"                       # how it fills while held (see paintEvent)
    FRAME_MS = 8                                 # redraw often, on a precise timer
    DRAIN = 0.25                                 # seconds to empty after letting go

    def __init__(self, text, seconds=3.0):
        super().__init__(text)
        self._label = text
        self._seconds = float(seconds)
        self._held_since = None                  # when the current hold began
        self._held_from = 0.0                    # the fill when it began
        self._drain_since = None                 # when a let-go began draining
        self._drain_from = 0.0
        self._fill = 0.0                         # 0..1, what is drawn
        self._done = False
        self._shown_number = None
        self._timer = QtCore.QTimer(self)
        # Windows' ordinary timers tick unevenly (about every 15 ms, sometimes
        # 31): a precise one keeps the fill moving smoothly.
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._tick)
        self.setObjectName("primary")
        self.setProperty("hold", True)           # its own hover look (settings_style)
        # A fixed width, so the countdown text never makes the button resize.
        metrics = self.fontMetrics()
        widest = max(metrics.horizontalAdvance(text),
                     metrics.horizontalAdvance("Keep holding… 3"))
        self.setMinimumWidth(widest + 48)

    def reset(self):
        """Ready to be held again (coming back to its page)."""
        self._timer.stop()
        self._done = False
        self._held_since = self._drain_since = None
        self._fill = 0.0
        self._shown_number = None
        self.setText(self._label)
        self.update()

    def _begin(self):
        if self._done or self._held_since is not None:
            return
        self._held_since = time.monotonic()
        self._held_from = self._fill             # carries on from where a drain got to
        self._drain_since = None
        self._timer.start()
        self._tick()

    def _cancel(self):
        if self._done or self._held_since is None:
            return
        self._held_since = None
        self._drain_since = time.monotonic()     # empty smoothly, not in one jump
        self._drain_from = self._fill
        self._shown_number = None
        self.setText(self._label)
        self._timer.start()

    def _tick(self):
        now = time.monotonic()
        if self._held_since is not None:
            self._fill = min(1.0, self._held_from + (now - self._held_since) / self._seconds)
            left = self._seconds * (1.0 - self._fill)
            if self._fill >= 1.0:
                self._done = True
                self._timer.stop()
                self.setText(self._label)
                self.update()
                # A moment on the full button, then on: it feels finished.
                QtCore.QTimer.singleShot(120, self.completed.emit)
                return
            number = max(1, int(math.ceil(left - 1e-6)))
            if number != self._shown_number:     # only when it changes: no re-measuring
                self._shown_number = number
                self.setText("Keep holding… %d" % number)
        elif self._drain_since is not None:
            gone = (now - self._drain_since) / self.DRAIN
            self._fill = max(0.0, self._drain_from * (1.0 - gone))
            if self._fill <= 0.0:
                self._drain_since = None
                self._timer.stop()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setFocus()
            self._begin()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._cancel()
        event.accept()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            if not event.isAutoRepeat():
                self._begin()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            if not event.isAutoRepeat():
                self._cancel()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self._cancel()                           # clicked or tabbed away: start over
        super().focusOutEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._fill <= 0.0:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        inner = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        edge = inner.left() + inner.width() * self._fill
        path = QtGui.QPainterPath()
        path.addRoundedRect(inner, 6, 6)
        p.setClipPath(path)
        soft = 14.0
        style = self.FILL_STYLE
        if style in ("deeper", "lighter"):
            # The button fills with a lighter (or deeper) shade of its own
            # colour (owner: lighter; a white sweep looked washed out, a
            # darker one too heavy).
            deep = QtGui.QColor(ACCENT).lighter(122) if style == "lighter"                 else QtGui.QColor(ACCENT).darker(122)
            clear = QtGui.QColor(deep)
            clear.setAlpha(0)
            sweep = QtGui.QLinearGradient(QtCore.QPointF(edge - soft, 0), QtCore.QPointF(edge, 0))
            sweep.setColorAt(0.0, deep)
            sweep.setColorAt(1.0, clear)
            p.setBrush(QtGui.QBrush(sweep))
            p.drawRect(QtCore.QRectF(inner.left(), inner.top(), edge - inner.left(),
                                     inner.height()))
        elif style == "bar":
            # A slim bar along the bottom of the button fills up.
            p.setBrush(QtGui.QColor(16, 17, 20, 150))
            p.drawRect(QtCore.QRectF(inner.left(), inner.bottom() - 3.0, edge - inner.left(), 3.0))
        else:
            # The first look: a light sweep.
            sweep = QtGui.QLinearGradient(QtCore.QPointF(edge - soft, 0), QtCore.QPointF(edge, 0))
            sweep.setColorAt(0.0, QtGui.QColor(255, 255, 255, 85))
            sweep.setColorAt(1.0, QtGui.QColor(255, 255, 255, 0))
            p.setBrush(QtGui.QBrush(sweep))
            p.drawRect(QtCore.QRectF(inner.left(), inner.top(), edge - inner.left(),
                                     inner.height()))
        p.setClipping(False)
        p.setPen(QtGui.QColor("#101114"))        # the words stay sharp on top
        p.setFont(self.font())
        p.drawText(self.rect(), Qt.AlignCenter, self.text())
        p.end()


def hold_confirm_window(parent, title, headline, paragraphs, hold_text, cancel_text):
    """A window whose yes has to be held down for three seconds (see
    HoldButton), so that what it says gets read. Returns True if confirmed;
    the other button, or closing the window, gives False."""
    box = QtWidgets.QDialog(parent)
    box.setWindowTitle(title)
    box.setWindowIcon(app_icon())
    box.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.WindowCloseButtonHint
                       | Qt.WindowStaysOnTopHint)
    box.setStyleSheet(settings_style() + """
QLabel#headline { color: #FFFFFF; font-size: 17px; font-weight: 600; }
QLabel#body { color: #C4C6CF; font-size: 13px; }
QPushButton { padding: 7px 16px; }
QPushButton#primary { background: %(accent)s; color: #101114; border: 1px solid %(accent)s;
    font-weight: 600; }
QPushButton#primary:hover { background: %(accent)s; border-color: #FFFFFF; }
""" % {"accent": ACCENT})
    dark_title_bar(box)
    layout = QtWidgets.QVBoxLayout(box)
    layout.setContentsMargins(26, 22, 26, 20)
    layout.setSpacing(12)
    for words, name in [(headline, "headline")] + [(text, "body") for text in paragraphs]:
        label = QtWidgets.QLabel(words)
        label.setObjectName(name)
        label.setWordWrap(True)
        label.setFixedWidth(420)
        layout.addWidget(label)
    layout.addSpacing(6)
    buttons = QtWidgets.QHBoxLayout()
    cancel = QtWidgets.QPushButton(cancel_text)
    cancel.clicked.connect(lambda _checked=False: box.reject())
    hold = HoldButton(hold_text)
    hold.completed.connect(box.accept)
    buttons.addWidget(cancel)
    buttons.addStretch(1)
    buttons.addWidget(hold)
    layout.addLayout(buttons)
    hint = QtWidgets.QLabel("Press and hold the button for 3 seconds (or hold Space).")
    hint.setObjectName("info")
    hint.setAlignment(Qt.AlignRight)
    layout.addWidget(hint)
    hold.setFocus()
    if parent is None:
        on_main_screen(box)
    return box.exec() == QtWidgets.QDialog.Accepted


def responsibility_notice():
    """Shown once before first use (and after Reset all settings): what the
    application does and does not do, and that where the text goes is up to
    the user. Quit, or closing the window, quits. Returns True if understood.
    (On a first start it is a page of the setup window instead.)"""
    return hold_confirm_window(
        None, "%s: before you start" % APP_TITLE, "Before you start",
        responsibility_paragraphs(), "Hold to confirm: I understand", "Quit")


def responsibility_paragraphs():
    """The words of "Before you start"."""
    app = {"app": APP_TITLE}
    # Short lines, one point each (long ones left a word or two hanging on a
    # line of their own). The bullets are one paragraph, one line per point.
    return ["%(app)s types your words into whichever window has your keyboard "
            "input, usually the text box you last clicked. It does not check which "
            "program that is; that is up to you." % app,
            "•  Check where your cursor is before you speak.\n"
            "•  Speech recognition makes mistakes: read before you send.\n"
            # A general rule, not a list (a list could read as "anything else
            # is fine"); the README gives examples.
            "•  Don't rely on it where a wrong word could cause harm.\n"
            "•  Don't dictate passwords or anything private.",
            "You are responsible for what %(app)s types and where it goes. It is "
            "free software, provided as is, without any warranty." % app]


def download_question_words():
    """The words of the one-time download question (a window, or a page of
    the setup window on a first start)."""
    size = dict((entry[0], entry[3]) for entry in MODELS).get(MODEL, "several hundred MB")
    return ("This application will go online once to download the speech model "
            "(%(model)s, %(size)s) and a small helper (about 2 MB) from this "
            "project's page on github.com, a third-party website that we do not run "
            "or control. Every file is checked; after that, %(app)s works offline."
            % dict(app=APP_TITLE, model=model_label(MODEL), size=size))


def auto_key_warning(parent, key):
    """Before a key is pressed automatically after dictating (the second
    hotkey, or "After dictating"): what that can do. Returns True if confirmed."""
    app = {"app": APP_TITLE, "key": key}
    return hold_confirm_window(
        parent, "%s: pressing a key for you" % APP_TITLE, "Pressing a key for you",
        ["With this on, %(app)s presses %(key)s for you right after typing your "
         "words. In many applications that sends a message, submits a form or "
         "confirms an action straight away, with no chance to read or correct the "
         "text first, and it can skip confirmation steps those applications would "
         "normally give you." % app,
         "You are responsible for where it is used and what it sends."],
        "Hold to confirm: turn it on", "Cancel")


def dot_off():
    """The colour of a page dot that is not the current page."""
    return "#CDD0D7" if light_menus() else "#3A3D46"


def set_menu_theme(theme):
    """Dark or light Settings (and the other windows), applied at once to
    every window that is open."""
    set_option("MENU_THEME", "menu_theme", theme)
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    app.setPalette(menu_palette())
    for widget in QtWidgets.QApplication.topLevelWidgets():
        try:
            if isinstance(widget, SettingsWindow):
                widget.setStyleSheet(settings_style())
                widget._paint_swatches()
                widget._paint_backgrounds()
                widget._paint_menu_theme()
            elif isinstance(widget, IntroWindow):
                widget._restyle()
                widget._paint_swatches()
                widget._go(widget.stack.currentIndex())
            if widget.isVisible() and isinstance(widget, QtWidgets.QDialog):
                dark_title_bar(widget)
            for child in widget.findChildren(QtWidgets.QWidget):
                child.update()
            widget.update()
        except Exception as exc:
            print("A window could not change its look:", exc, file=sys.stderr)


class _PageStack(QtWidgets.QStackedWidget):
    """The setup window's pages. Says it has no height-for-width: otherwise
    Qt tells Windows the window may never be shorter than its longest page
    (wrapped text makes it ask), and Windows then refuses to shrink it for a
    short page (measured). The window's height is set by hand instead
    (IntroWindow._fit_page)."""

    def hasHeightForWidth(self):
        return False


class IntroWindow(QtWidgets.QDialog):
    """The setup window of a first start: one window, page by page.

    First the steps that come before anything else is started: the look,
    "Before you start" and (if the model is not here) the one-time download
    question. main() waits for them with run_gate(); closing the window there
    quits, as the download question always has. Then, once the application is
    running (attach()), the hotkey and microphone, how it should work, and
    where things are. Every choice here is an ordinary setting and can be
    changed later in Settings."""

    CANT_HEAR = ("We can't hear you. Is your microphone muted, or is a different "
                 "input device in use? Choose another one above and try again.")

    def __init__(self, dictation=None, need_ok=False, need_download=False):
        super().__init__()
        self.dictation = dictation
        self.gate_result = None                  # "go", "download", "manual" or "quit"
        self.understood = False                  # "Before you start" has been held
        self._gate_loop = None
        self.ok_page = self.download_page = -1
        self.setWindowTitle("Welcome to %s" % APP_TITLE)
        self.setWindowIcon(app_icon())
        self.setWindowFlags(Qt.Window | Qt.WindowTitleHint | Qt.WindowCloseButtonHint
                            | Qt.WindowStaysOnTopHint)
        self.setFixedWidth(500)
        self._restyle()
        dark_title_bar(self)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(34, 26, 34, 24)
        outer.setSpacing(20)
        self.stack = _PageStack()
        outer.addWidget(self.stack)
        self.sketches = []
        self.swatches = []
        self.themes = []

        def page(kind, headline, body, controls=True):
            """One page: a picture (if `kind`), a headline, a paragraph, and
            (for pages with things to set) a panel that holds the controls.
            Returns the layout to put the controls in."""
            holder = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            if kind:
                sketch = Sketch(kind)
                self.sketches.append(sketch)
                layout.addWidget(sketch)
                layout.addSpacing(18)
            title = QtWidgets.QLabel(headline)
            title.setObjectName("headline")
            layout.addWidget(title)
            layout.addSpacing(8)
            text = QtWidgets.QLabel(body)
            text.setObjectName("body")
            text.setWordWrap(True)
            layout.addWidget(text)
            self.stack.addWidget(holder)
            if not controls:
                layout.addStretch(1)
                return layout
            layout.addSpacing(18)
            panel = QtWidgets.QFrame()           # the controls sit together on a card
            panel.setObjectName("panel")
            inside = QtWidgets.QVBoxLayout(panel)
            inside.setContentsMargins(18, 16, 18, 16)
            inside.setSpacing(10)
            layout.addWidget(panel)
            layout.addStretch(1)
            return inside

        def line(layout, *widgets):
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(8)
            for widget in widgets:
                if widget is None:
                    row.addStretch(1)
                else:
                    row.addWidget(widget)
            layout.addLayout(row)

        def small(text):
            label = QtWidgets.QLabel(text)
            label.setObjectName("info")
            label.setWordWrap(True)
            return label

        def field(text):
            name = QtWidgets.QLabel(text)
            name.setObjectName("field")
            name.setFixedWidth(86)
            return name

        # 1. Before you start (once; held for 3 seconds so it gets read).
        if need_ok:
            words = responsibility_paragraphs()
            ok = page(None, "Before you start", words[0])
            self.ok_page = self.stack.count() - 1
            points = words[1].split("\n")
            for i, text in enumerate(points + words[2:]):
                label = QtWidgets.QLabel(text)
                label.setObjectName("note")
                label.setWordWrap(True)
                ok.addWidget(label)
                if i == len(points) - 1:
                    ok.addSpacing(6)             # the points, then the last word
            ok.addSpacing(12)                    # room above the buttons
            leave = QtWidgets.QPushButton("Quit")
            leave.clicked.connect(lambda: self._pass_gate("quit"))
            self.hold = HoldButton("Hold to confirm: I understand")
            self.hold.completed.connect(self._understood)
            # Both buttons the same height, so they line up.
            self.hold.ensurePolished()
            leave.setFixedHeight(self.hold.sizeHint().height())
            leave.setMinimumWidth(84)
            line(ok, leave, None, self.hold)
            hint = small("Press and hold the button for 3 seconds (or hold Space).")
            hint.setAlignment(Qt.AlignRight)
            ok.addWidget(hint)
        # 2. The one-time download (only when the model is not on this PC).
        # Download starts it straight away (the download pill shows at the
        # bottom of the screen) while the rest is set up.
        if need_download:
            down = page(None, "One-time download needed", download_question_words())
            self.download_page = self.stack.count() - 1
            leave = QtWidgets.QPushButton("Quit")
            leave.clicked.connect(lambda: self._pass_gate("quit"))
            manual = QtWidgets.QPushButton("Manual download")
            manual.setToolTip("Fetch the files yourself with your web browser; the "
                              "steps are shown.")
            manual.clicked.connect(lambda: self._download_answer("manual"))
            fetch = QtWidgets.QPushButton("Download")
            fetch.setObjectName("primary")
            fetch.clicked.connect(lambda: self._download_answer("download"))
            line(down, leave, None, manual, fetch)
        self.gate = self.stack.count()           # from here on the application runs

        # 3. Make it yours (the application runs from here on, and a download
        # goes on in the background). The real screen glow shows while this
        # page is up, so the colour can be seen for real. Nothing is recorded.
        look = page("look", "Welcome to %s" % APP_TITLE,
                    "Dictation that stays on your PC. Choose a look to get started.")
        self.look_page = self.stack.count() - 1
        colours = []
        for label, value in ACCENT_CHOICES:
            swatch = QtWidgets.QPushButton()
            swatch.setFixedSize(24, 24)
            swatch.setCursor(Qt.PointingHandCursor)
            swatch.setToolTip(label)
            swatch.clicked.connect(lambda _checked=False, v=value: self._colour(v))
            self.swatches.append((swatch, value))
            colours.append(swatch)
        line(look, field("Colour"), *(colours + [None]))
        # One choice here for the pop-up and Settings together (two rows of
        # the same Dark / Light looked like a mistake). Settings > Look keeps
        # them apart, with Custom, for anyone who wants to mix them.
        self.menu_themes = []
        for name, value in (("Dark", "dark"), ("Light", "light")):
            button_ = ChoiceButton(name)
            button_.setMinimumWidth(68)
            button_.clicked.connect(lambda _checked=False, v=value: self._theme(v))
            self.menu_themes.append((button_, value))
        line(look, field("Theme"), *([b for b, _v in self.menu_themes] + [None]))
        # How lively it looks: a tick box, not another pair of buttons (two
        # rows of the same buttons looked repetitive, owner).
        self.effects = QtWidgets.QCheckBox("Glow and moving light while you dictate")
        self.effects.setChecked(bool(SCREEN_GLOW or SCREEN_GLOW_FLOW))
        self.effects.setToolTip("Off: no moving effects; quieter, and lighter on older PCs.")
        self.effects.toggled.connect(
            lambda on: self._effects_chosen("full" if on else "calm"))
        look.addWidget(self.effects)
        self.pill = QtWidgets.QCheckBox("Always show a small status pill on screen")
        self.pill.setChecked(bool(ALWAYS_SHOW))
        self.pill.toggled.connect(self._pill_toggled)
        look.addWidget(self.pill)
        self.live = ScreenGlow()
        self.live_timer = QtCore.QTimer(self)
        self.live_timer.setInterval(33)
        self.live_timer.timeout.connect(self._live_tick)

        # 4. The hotkey and the microphone, on one page, as short labelled rows.
        # (There is no separate microphone test: trying the hotkey in the box
        # below is the test, and a note appears if nothing is heard.)
        second = page("hotkey", "Hotkey and microphone",
                      "The microphone is only on while you dictate.")

        def labelled(title, *widgets):
            line(second, field(title), *widgets)

        self.record = QtWidgets.QPushButton("Record")
        self.record.setFixedWidth(84)
        self.record.clicked.connect(self._record)
        self.hotkey_label = QtWidgets.QLabel(pretty(HOTKEY))
        self.hotkey_label.setObjectName("info")
        labelled("Hotkey", self.record, self.hotkey_label, None)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("Hold while speaking", True)
        self.mode.addItem("Press to start and stop", False)
        self.mode.setCurrentIndex(0 if HOLD_TO_TALK else 1)
        self.mode.setFixedWidth(210)
        self.mode.currentIndexChanged.connect(
            lambda index: (set_hold_to_talk(bool(self.mode.itemData(index))),
                           self._refresh_try()))
        labelled("How", self.mode, None)
        self.mic = QtWidgets.QComboBox()
        self.mic.addItem("Windows default", "")
        names = input_devices()
        if MIC_DEVICE and MIC_DEVICE not in names:
            names.insert(0, MIC_DEVICE)          # chosen earlier, unplugged right now
        for name in names:
            self.mic.addItem(name, name)
        self.mic.setCurrentIndex(max(0, self.mic.findData(MIC_DEVICE)))
        self.mic.setFixedWidth(210)
        self.mic.currentIndexChanged.connect(self._mic_chosen)
        labelled("Microphone", self.mic, None)
        # A place to try it straight away: dictation types into whatever has the
        # cursor, so a text box here is all it takes.
        self.try_label = small("")
        second.addWidget(self.try_label)
        self.try_box = QtWidgets.QPlainTextEdit()
        self.try_box.setFixedHeight(52)
        self.try_box.setStyleSheet("QPlainTextEdit { border-radius: 6px; padding: 5px; }")
        second.addWidget(self.try_box)
        QtWidgets.QApplication.instance().focusChanged.connect(
            lambda _old, _new: self._refresh_try() if self.dictation is not None else None)
        # Shown for a few seconds when the hotkey is held and nothing is heard.
        self.mic_note = QtWidgets.QLabel(self.CANT_HEAR)
        self.mic_note.setWordWrap(True)
        self.mic_note.setStyleSheet("color: #F2B45A;")
        self.mic_note.setVisible(False)
        second.addWidget(self.mic_note)
        self.mic_help = QtWidgets.QPushButton("Open Windows microphone settings")
        self.mic_help.clicked.connect(open_mic_settings)
        self.mic_help.setVisible(False)
        line(second, self.mic_help, None)
        self._note_until = 0.0
        self.hear = QtCore.QTimer(self)          # only reads what dictation already knows
        self.hear.setInterval(250)
        self.hear.timeout.connect(self._check_hearing)

        # 5. Getting around (how words appear is not asked here: all at
        # once, the most accurate, unless chosen in Settings; owner)
        fourth = page("tips", "Good to know",
                      "Settings: right-click the pill, or click the %s icon by the "
                      "clock." % APP_TITLE)
        self.startup = QtWidgets.QCheckBox("Start with Windows")
        # Ticked for a new user: applied when the introduction is finished (a
        # choice they can see and untick, never one made on its own).
        self.startup.setChecked(starts_with_windows() or not _config.get("welcomed"))
        self.startup.toggled.connect(lambda on: set_startup(bool(on)))
        fourth.addWidget(self.startup)
        fourth.addSpacing(6)
        # Off unless chosen here or in Settings; ticking it asks first.
        self.app_updates = QtWidgets.QCheckBox("Check for app updates at startup")
        self.app_updates.setChecked(bool(CHECK_APP_UPDATES))
        self.app_updates.setToolTip("Goes online to github.com each time it starts, "
                                    "after asking you here. Nothing is downloaded.")
        self.app_updates.toggled.connect(self._app_updates_toggled)
        fourth.addWidget(self.app_updates)
        fourth.addSpacing(12)                    # the button is a separate thing
        more = QtWidgets.QPushButton("Finish and open Settings")
        more.setToolTip("Finish here and open Settings, where everything else lives: "
                        "sounds, what the pop-up shows, the action hotkey and more.")
        more.clicked.connect(self._open_settings)
        line(fourth, more, None)
        fourth.addSpacing(8)
        self.status = small("")
        # Room for its two lines from the start: the window takes its height
        # before the text is known, and a later line would be cut off.
        self.status.setMinimumHeight(36)
        fourth.addWidget(self.status)
        fourth.addStretch(1)

        # Footer: where you are, and the way on.
        footer = QtWidgets.QHBoxLayout()
        footer.setSpacing(6)
        self.dots = []
        for _ in range(self.stack.count()):
            dot = QtWidgets.QLabel("")
            dot.setFixedSize(8, 8)
            self.dots.append(dot)
            footer.addWidget(dot)
        footer.addStretch(1)
        self.skip = QtWidgets.QPushButton("Skip")
        self.skip.setToolTip("Close the introduction. Everything here can be found "
                             "later in Settings.")
        self.skip.clicked.connect(lambda: self.close())
        footer.addWidget(self.skip)
        self.back = QtWidgets.QPushButton("Back")
        self.back.clicked.connect(lambda: self._go(self.stack.currentIndex() - 1))
        footer.addWidget(self.back)
        self.next = QtWidgets.QPushButton("Next")
        self.next.setObjectName("primary")
        self.next.setDefault(True)
        self.next.clicked.connect(self._next)
        footer.addWidget(self.next)
        outer.addLayout(footer)
        # Skip and Back as tall as Next, so the row lines up (owner).
        self.next.ensurePolished()
        for small_button in (self.skip, self.back):
            small_button.setFixedHeight(self.next.sizeHint().height())
            small_button.setMinimumWidth(72)

        # (Not "self.done": a window already has a done() of its own, which is
        # what closes it. A value stored under that name stops it closing.)
        self.finished_up = False
        self.watch = QtCore.QTimer(self)         # keeps the last page's status current
        self.watch.setInterval(500)
        self.watch.timeout.connect(self._refresh_status)
        self._paint_swatches()
        self._go(0)
        if self.gate == 0:                       # nothing to ask before the start
            self._pass_gate("go")

    # -- looks ---------------------------------------------------------------
    def _restyle(self):
        self.setStyleSheet(settings_style() + """
QLabel#headline { color: #FFFFFF; font-size: 21px; font-weight: 600; }
QLabel#body { color: #B9BBC6; font-size: 14px; }
QLabel#note { color: #C4C6CF; font-size: 13px; }
QLabel#info { color: #8E909A; font-size: 12px; }
QLabel#field { color: #E6E7EC; font-size: 13px; }
QFrame#panel { background: #1D1F25; border: 1px solid #2A2C33; border-radius: 12px; }
QLabel#field { color: #A9ABB5; font-size: 12px; }
QPushButton { padding: 5px 14px; border-radius: 6px; font-size: 12px; }
QPushButton#primary { background: %(accent)s; color: #101114; border: 1px solid %(accent)s;
    font-weight: 600; padding: 7px 20px; font-size: 13px; }
QPushButton#primary:hover { background: %(accent)s; border-color: #FFFFFF; }
QComboBox { padding: 4px 10px; border-radius: 6px; font-size: 12px; min-height: 16px; }
QCheckBox { font-size: 13px; spacing: 10px; }
QProgressBar { border: none; border-radius: 3px; background: #2A2C33; max-height: 6px;
    min-height: 6px; }
QProgressBar::chunk { border-radius: 3px; background: %(accent)s; }
QPlainTextEdit { font-size: 13px; background: #17181C; color: #F0F0F4;
    border: 1px solid #34363E; }
""" % {"accent": ACCENT} + (LIGHT_STYLE if light_menus() else ""))

    def _paint_swatches(self):
        for button, value in self.swatches:
            button.setStyleSheet("background: transparent; border: none; padding: 0; "
                                 "min-height: 0;")
            button.setIcon(QtGui.QIcon(swatch_picture(value, value.lower() == ACCENT.lower(),
                                                      3.0)))
            button.setIconSize(QtCore.QSize(24, 24))
        for button, value in self.themes:
            button.setChosen(value.lower() == POPUP_BG.lower())
        for button, value in getattr(self, "menu_themes", []):
            button.setChosen(value == MENU_THEME)

    def _redraw(self):
        for sketch in self.sketches:
            sketch.update()

    # -- moving between pages ---------------------------------------------------
    def _go(self, index):
        early = self.dictation is None           # the steps before the application runs
        index = max(0 if early else self.gate, min(self.stack.count() - 1, index))
        self._note_until = 0.0
        self._show_note(False)
        if not early:
            self.dictation.cancel_capture()      # a half-recorded hotkey is dropped
        self.record.setText("Record")
        self.record.setEnabled(True)
        self.hotkey_label.setText(pretty(HOTKEY))
        self.stack.setCurrentIndex(index)
        self._fit_page()
        QtCore.QTimer.singleShot(0, self._fit_page)  # once its page is laid out
        last = index == self.stack.count() - 1
        own_buttons = index == self.download_page or (
            index == self.ok_page and not self.understood)   # held once: Next works too
        self.back.setVisible(index > (0 if early else self.gate))
        # Before the application runs there is nothing to skip to: the way on
        # is Next (or the page's own buttons), and closing the window quits.
        self.skip.setVisible(not last and not early)
        self.next.setVisible(not own_buttons)
        self.next.setText("Get started" if last else "Next")
        for i, dot in enumerate(self.dots):
            dot.setStyleSheet("border-radius: 4px; background: %s;"
                              % (ACCENT if i == index else dot_off()))
        if not early:
            self._refresh_status()
        self._redraw()
        self._live(index == self.look_page)
        if index == self.ok_page:
            self.hold.reset()                    # ready again after a Back
            self.hold.setFocus()

    def _live(self, on):
        """The real screen glow, while the look page is up (nothing recorded)."""
        try:
            if on and SCREEN_GLOW:
                screen = QtGui.QGuiApplication.primaryScreen()
                self.live.start(glow_base(), screen.geometry())
                self.live_timer.start()
            elif not on and self.live_timer.isActive():
                self.live_timer.stop()
                self.live.stop()
        except Exception as exc:
            print("The glow preview could not be shown:", exc, file=sys.stderr)

    def _live_tick(self):
        try:
            self.live.step(0.55, bool(SCREEN_GLOW))
        except Exception:
            pass

    def _download_answer(self, choice):
        """Download or Manual download (after "Before you start"): the
        application starts, and with it the download."""
        self._pass_gate(choice)

    def _understood(self):
        """ "Before you start" was held: on to the download question, or the
        application can start."""
        self.understood = True
        save_setting("responsibility_ok", True)
        if self.download_page >= 0:
            self._go(self.download_page)
        else:
            self._pass_gate("go")

    def _pass_gate(self, result):
        """The steps before the application runs are done (or Quit was chosen):
        main() carries on from run_gate()."""
        if self.gate_result is not None:
            return
        self.gate_result = result
        self._live(False)
        self.next.setEnabled(False)              # until the application is running
        self.back.setEnabled(False)
        if self._gate_loop is not None:
            self._gate_loop.quit()

    def run_gate(self):
        """Wait (the window stays open) until the first steps are done. Returns
        "go", "download", "manual" or "quit"."""
        if self.gate_result is None:
            self._gate_loop = QtCore.QEventLoop()
            self._gate_loop.exec()
            self._gate_loop = None
        return self.gate_result

    def attach(self, dictation):
        """The application is running now: on to the rest of the pages."""
        self.dictation = dictation
        self.next.setEnabled(True)
        self.back.setEnabled(True)
        if not self.isVisible():                 # closed while it was starting
            self.closeEvent(None)
            return
        self.hear.start()
        self.watch.start()
        self._go(self.gate)

    def _open_settings(self):
        self.close()                             # finishes the introduction
        open_settings(self.dictation)

    def _next(self):
        if self.dictation is None and self.stack.currentIndex() + 1 >= self.gate:
            self._pass_gate("go")
            return
        if self.stack.currentIndex() == self.stack.count() - 1:
            self.close()                         # closeEvent finishes up
        else:
            self._go(self.stack.currentIndex() + 1)

    def _refresh_try(self):
        """The try-it box says what to do, in terms of the hotkey as it is now."""
        how = "hold %s and speak, then let go" if HOLD_TO_TALK else \
            "press %s, speak, then press it again"
        progress = getattr(self.dictation, "download_words", lambda: "")()
        if self.dictation.state == "nomodel":
            self.try_label.setText("Try it out: this needs the speech model, which is "
                                   "not on this PC yet.")
            self.try_box.setPlaceholderText("Not available until the model is added")
            return
        if getattr(self.dictation, "download_stuck", False):
            progress = "the download is not moving (see the last page)"
        if self.dictation.state == "loading":
            self.try_label.setText("Try it out: %s" % (
                (progress + ". One moment.") if progress
                else "the speech model is still getting ready, one moment\u2026"))
            self.try_box.setPlaceholderText("Ready in a moment\u2026")
        elif not self.try_box.hasFocus():
            # Words go wherever the cursor is: the habit to learn, here first.
            self.try_label.setText("Try it out: your words go where the cursor is.")
            self.try_box.setPlaceholderText("Click here first")
        else:
            self.try_label.setText("Try it out: " + how % pretty(HOTKEY) + ".")
            self.try_box.setPlaceholderText("Now " + how % pretty(HOTKEY))

    def _refresh_status(self):
        self._refresh_try()
        loading = self.dictation.state == "loading"
        progress = getattr(self.dictation, "download_words", lambda: "")()
        # Two lines at most (a longer note is cut off in this box). The
        # download card at the bottom of the screen has the details and help.
        if self.dictation.state == "nomodel":
            self.status.setText("No speech model yet, so dictation is off. It can be "
                                "downloaded later from Settings, on the Model tab.")
        elif loading and getattr(self.dictation, "download_blocked", False):
            self.status.setText("The download seems blocked. Click the card at the "
                                "bottom of the screen for help.")
        elif loading and getattr(self.dictation, "download_stuck", False):
            self.status.setText("The download is not moving. Click the card at the "
                                "bottom of the screen for help.")
        elif loading and progress:
            self.status.setText("The speech model is downloading. You can finish here; "
                                "dictation works once it is done.")
        else:
            self.status.setText("The speech model is still getting ready. You can finish "
                                "here; dictation works as soon as it is."
                                if loading else "The speech model is ready.")

    # -- the choices -----------------------------------------------------------
    def _record(self):
        self.record.setText("Press keys")
        self.record.setEnabled(False)
        self.hotkey_label.setText("Press the keys, then let go.  Esc cancels")
        self.dictation.start_capture(self._recorded)

    def _recorded(self, _hotkey):
        self.record.setText("Record")
        self.record.setEnabled(True)
        self.hotkey_label.setText(pretty(HOTKEY))
        self._refresh_try()
        self._redraw()

    # -- the microphone check ------------------------------------------------------
    def _mic_chosen(self, index):
        self._note_until = 0.0
        self._show_note(False)
        set_option("MIC_DEVICE", "mic", self.mic.itemData(index) or "")

    def _check_hearing(self):
        """While the hotkey is being tried: say so if the microphone hears nothing."""
        now = time.monotonic()
        silent = False
        if self.dictation.state == "recording":
            try:
                silent = bool(self.dictation.overlay._mic_warning())
            except Exception:
                silent = False
        if silent:
            self._note_until = now + 8.0
        self._show_note(now < self._note_until)

    def _show_note(self, on):
        if self.mic_note.isHidden() == bool(on):
            self.mic_note.setVisible(bool(on))
            self.mic_help.setVisible(bool(on))
            QtCore.QTimer.singleShot(0, self._fit_page)   # the note needs its room

    def sizeHint(self):
        # Used when the window is first placed (adjustSize): already the right
        # height for its page, so it never has to shrink while Windows is
        # still showing it (that left a stale copy of its bottom on screen).
        height = self._page_height()
        return QtCore.QSize(self.width(), height) if height > 0 else super().sizeHint()

    def adjustSize(self):
        # on_main_screen() sizes a window before it opens: here that is the
        # page's own height (Qt's would be the longest page's).
        self._fit_page()

    def _page_height(self):
        """How tall the window must be for the page showing (0 if unknown)."""
        try:
            for i in range(self.stack.count()):
                shown = i == self.stack.currentIndex()
                policy = QtWidgets.QSizePolicy.Preferred if shown else                     QtWidgets.QSizePolicy.Ignored
                self.stack.widget(i).setSizePolicy(policy, policy)
            # Measured by hand: Qt's page stack counts every page's wrapped
            # text, so it would still be as tall as the longest page.
            outer = self.layout()
            outer.setSizeConstraint(QtWidgets.QLayout.SetNoConstraint)
            holder = self.stack.currentWidget()
            holder.ensurePolished()              # its fonts set, or it measures wrong
            for child in holder.findChildren(QtWidgets.QWidget):
                child.ensurePolished()
            page = holder.layout()
            margins = outer.contentsMargins()
            width = self.width() - margins.left() - margins.right()
            page.invalidate()
            page_h = (page.totalHeightForWidth(width) if page.hasHeightForWidth()
                      else page.totalSizeHint().height())
            if page_h <= 0:
                return 0
            footer = outer.itemAt(outer.count() - 1)
            return (margins.top() + margins.bottom() + page_h + outer.spacing()
                    + footer.sizeHint().height())
        except Exception as exc:
            print("The setup window could not measure its page:", exc, file=sys.stderr)
            return 0

    def _fit_page(self):
        """The window as tall as the page showing (like Settings), not as tall
        as the longest page: no empty space under a short one. The top stays
        where it is; only the bottom moves."""
        height = self._page_height()
        if height > 0 and (height != self.height() or self.minimumHeight() != height):
            self.setFixedHeight(height)          # (see _PageStack for why it can shrink)

    def _colour(self, colour):
        set_accent(colour)
        self._paint_swatches()
        self._restyle()
        self.setWindowIcon(app_icon())
        tray = getattr(self.dictation, "tray", None)
        if tray is not None:
            tray.setIcon(app_icon())
        index = self.stack.currentIndex()
        for i, dot in enumerate(self.dots):
            dot.setStyleSheet("border-radius: 4px; background: %s;"
                              % (ACCENT if i == index else dot_off()))
        self._redraw()
        self._live(index == self.look_page)      # the real glow, in the new colour

    def _theme(self, key):
        """Dark or light: the pop-up and Settings together."""
        background = dict((name.lower(), value) for name, value in BACKGROUND_CHOICES)[key]
        set_option("POPUP_BG", "popup_bg", background)
        set_menu_theme(key)                      # restyles this window too
        self._paint_swatches()
        self._redraw()

    def _effects_chosen(self, key):
        self._effects_style(key)
        self._live(self.stack.currentIndex() == self.look_page)   # shown at once

    def _effects_style(self, key):
        """How lively it should look. Full: glow round the pop-up, the screen
        glow with its moving light. Calm: none of the moving extras."""
        full = key == "full"
        set_option("SCREEN_GLOW", "screen_glow", full)
        set_option("SCREEN_GLOW_FLOW", "screen_glow_flow", full)
        set_option("OUTLINE_LIGHT", "outline_light", full)
        set_option("CARD_STYLE", "card_style", "glow" if full else "clean")
        self._redraw()

    def _pill_toggled(self, on):
        set_option("ALWAYS_SHOW", "always_show", bool(on))
        if self.dictation is not None:
            self.dictation.refresh_popup()

    def _app_updates_toggled(self, on):
        if on and not ask_app_update_check(self, at_startup=True):
            self.app_updates.blockSignals(True)  # untick it again, quietly
            self.app_updates.setChecked(False)
            self.app_updates.blockSignals(False)
            on = False
        set_check_app_updates(on)

    def closeEvent(self, event):
        if self.dictation is None:
            # Before the application runs, closing the window is Quit (as the
            # download question has always been); main() leaves.
            self._live(False)
            self._pass_gate("quit")
            if event is not None:
                event.accept()
            self.hide()
            return
        # Finishing and closing the window are the same thing: nothing here
        # has to be completed, and every choice made so far is already saved.
        try:
            self.hear.stop()
            if not self.finished_up:
                self.finished_up = True
                if self.startup.isChecked() and not starts_with_windows():
                    set_startup(True)            # the ticked choice, now finished
                self.watch.stop()
                self.dictation.cancel_capture()
                self.dictation.on_intro_done()
        except Exception as exc:                 # whatever happens, the window closes
            print("Introduction did not finish cleanly:", exc, file=sys.stderr)
        if event is not None:
            event.accept()
        self.hide()

    def keyPressEvent(self, event):
        # Esc would only hide the window without finishing the introduction
        # (no "Ready", the introduction again next time): finish it properly.
        if event.key() == Qt.Key_Escape:
            if self.dictation is not None:       # never quits by accident early on
                self.close()
            return
        super().keyPressEvent(event)


class _TabScroll(QtWidgets.QScrollArea):
    """A Settings tab that scrolls up and down if it must, but is always exactly
    as wide as the window shows."""

    def resizeEvent(self, event):
        super().resizeEvent(event)
        page = self.widget()
        if page is not None:
            page.setFixedWidth(self.viewport().width())
            page.setMinimumHeight(page.sizeHint().height())


class SettingsWindow(QtWidgets.QDialog):
    """Every option in one place. Changes apply straight away and are saved."""

    RECORD_LABEL = "Record"

    def __init__(self, dictation, tray):
        super().__init__()
        self.dictation = dictation
        self.tray = tray
        self.setWindowTitle("%s settings  \u00b7  v%s" % (APP_TITLE, VERSION))
        self.setWindowIcon(app_icon())
        self.setWindowFlags(Qt.Window | Qt.WindowTitleHint | Qt.WindowCloseButtonHint
                            | Qt.WindowMinimizeButtonHint
                            | Qt.WindowStaysOnTopHint)    # stays above other windows
        self.setStyleSheet(settings_style())
        self.setFixedWidth(440)
        dark_title_bar(self)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 14)
        outer.setSpacing(12)
        # Only while 1337 mode is on: a bar at the top with the way back out.
        self.leet_bar = QtWidgets.QWidget()
        bar = QtWidgets.QHBoxLayout(self.leet_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar_text = QtWidgets.QLabel("1337 mode is on")
        bar_text.setObjectName("info")
        bar.addWidget(bar_text)
        bar.addStretch(1)
        leave_leet = QtWidgets.QPushButton("Exit 1337 mode")
        leave_leet.setCursor(Qt.PointingHandCursor)
        leave_leet.clicked.connect(lambda: toggle_leet(self.dictation))
        bar.addWidget(leave_leet)
        self.leet_bar.setVisible(LEET_MODE)
        outer.addWidget(self.leet_bar)
        tabs = QtWidgets.QTabWidget()
        tabs.setDocumentMode(False)
        outer.addWidget(tabs)
        self.swatches = []
        page = {"layout": None}          # the tab currently being filled

        def tab(title):
            # Each tab scrolls if it does not fit, so on a small screen nothing
            # is ever cut off (the window itself is kept within the screen).
            holder = QtWidgets.QWidget()
            holder.setObjectName("tabpage")
            layout = QtWidgets.QVBoxLayout(holder)
            layout.setContentsMargins(16, 14, 16, 14)
            layout.setSpacing(10)
            # Never wider than the window: the page is held to the visible width,
            # so a row that is too wide is squeezed, never cut off at the edge.
            layout.setSizeConstraint(QtWidgets.QLayout.SetNoConstraint)
            area = _TabScroll()
            area.setObjectName("tabscroll")
            area.setWidgetResizable(True)
            area.setFrameShape(QtWidgets.QFrame.NoFrame)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            area.setWidget(holder)
            area.viewport().setAutoFillBackground(False)
            tabs.addTab(area, title)
            page["layout"] = layout

        def end_tab():
            page["layout"].addStretch(1)

        def row(*widgets):
            line = QtWidgets.QHBoxLayout()
            line.setSpacing(8)
            for widget in widgets:
                if widget is None:
                    line.addStretch(1)
                else:
                    line.addWidget(widget)
            page["layout"].addLayout(line)

        def button_grid(*buttons):
            """Buttons two to a row, all the same width: tidier than a row of
            buttons each as wide as its own words."""
            grid = QtWidgets.QGridLayout()
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(8)
            for i, made in enumerate(buttons):
                made.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                   QtWidgets.QSizePolicy.Fixed)
                grid.addWidget(made, i // 2, i % 2)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            page["layout"].addLayout(grid)

        def field(text):
            label = QtWidgets.QLabel(text)
            label.setObjectName("field")
            label.setFixedWidth(104)             # one column on every tab, so controls line up
            return label

        def heading(text, first=False):
            if not first:                        # a thin line between sections
                rule = QtWidgets.QFrame()
                rule.setObjectName("rule")
                rule.setFixedHeight(1)
                page["layout"].addSpacing(2)
                page["layout"].addWidget(rule)
                page["layout"].addSpacing(2)
            label = QtWidgets.QLabel(text)
            label.setObjectName("heading")
            page["layout"].addWidget(label)

        def button(text, handler, width=None):
            made = QtWidgets.QPushButton(text)
            made.clicked.connect(lambda _checked=False: handler())
            if width:
                made.setFixedWidth(width)
            return made

        def note(text):
            label = QtWidgets.QLabel(text)
            label.setObjectName("info")
            label.setWordWrap(True)
            page["layout"].addWidget(label)
            return label

        def check(text, value, handler, tip=None, hint=None):
            box = QtWidgets.QCheckBox(text)
            box.setChecked(bool(value))
            box.toggled.connect(handler)
            if tip:
                box.setToolTip("<p style='white-space: pre-wrap; width: 300px;'>%s</p>" % tip)
            page["layout"].addWidget(box)
            if hint:                             # a line of explanation under the box
                label = QtWidgets.QLabel(hint)
                label.setObjectName("info")
                label.setWordWrap(True)
                label.setContentsMargins(0, 0, 0, 2)
                page["layout"].addWidget(label)
            return box

        def option(name, key, refresh=False, preview=False):
            def handler(value):
                set_option(name, key, bool(value))
                if refresh:
                    dictation.refresh_popup()
                if preview:
                    dictation.preview_popup()
            return handler

        # ================= General =================
        tab("General")
        heading("Dictation hotkey", first=True)
        self.record = button(self.RECORD_LABEL, self._record_hotkey, width=84)
        self.record.setToolTip("Click, then press any key or key combination to "
                               "use it as the hotkey. Esc cancels.")
        self.hotkey = QtWidgets.QComboBox()
        choices = list(HOTKEY_CHOICES)
        if HOTKEY not in choices:
            choices.insert(0, HOTKEY)            # a recorded or hand-set one
        for choice in choices:
            self.hotkey.addItem(pretty(choice), choice)
        self.hotkey.setCurrentIndex(choices.index(HOTKEY))
        self.hotkey.currentIndexChanged.connect(self._hotkey_changed)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("Hold while speaking", True)
        self.mode.addItem("Press to start and stop", False)
        self.mode.setCurrentIndex(0 if HOLD_TO_TALK else 1)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        # On its own row: its words are longer than one third of the row.
        row(self.record, self.hotkey, None)
        row(self.mode, None)
        self.hotkey_note = note("")
        self._refresh_hotkey_note()

        heading("Text")
        check("Type as you speak", LIVE_TYPE, option("LIVE_TYPE", "live_type"),
              tip="Off by default. Your words are typed into your app while you "
                  "speak, about half a second behind you, instead of all at once "
                  "when you finish. The hotkey can stay held: it doesn't get in "
                  "the way of the typing.\n\n"
                  "Words only go into the window you started in, and only into "
                  "a text box (see below). Otherwise typing waits; if you click "
                  "into a text box, it catches up.\n\n"
                  "Less accurate: each part is read on its own, before the rest "
                  "of the sentence is heard, and words already typed are never "
                  "changed afterwards. Small words like \"and\" or \"on\" are the "
                  "most often wrong.",
              hint="Words appear as you say them. Less accurate than all at once "
                   "(off is recommended).")
        check("Text box detection", TEXTBOX_ONLY, option("TEXTBOX_ONLY", "textbox_only"),
              tip="On by default. While typing as you speak, words only go in "
                  "when the cursor is in a text box: on a web page or a video, "
                  "typed letters would work as shortcuts and Space would scroll. "
                  "If there is no text box, the pop-up says \"No text box "
                  "selected\" and nothing is typed until you click into one.\n\n"
                  "To tell, the app asks Windows' accessibility service (the one "
                  "screen readers use) what kind of control has the focus: a text "
                  "box, a button, a page. Only that: never the text in it, the "
                  "page or the program. Nothing is kept or written down.\n\n"
                  "Off: Windows isn't asked, and words are typed wherever the "
                  "focus is.",
              hint="Typing as you speak waits for a text box, and the pop-up says "
                   "when there isn't one. Asks Windows only what kind of control "
                   "has the focus.")
        check("Show it at the mouse pointer", POINTER_HINT,
              option("POINTER_HINT", "pointer_hint"),
              hint="A small \"No text box\" tag follows the pointer while typing "
                   "as you speak waits for one.")
        check("Remove filler words", REMOVE_FILLERS,
              option("REMOVE_FILLERS", "remove_fillers"),
              hint="Leaves out um, uh and erm.")
        check("Add a space after each dictation", ADD_SPACE,
              option("ADD_SPACE", "add_space"),
              hint="So two dictations in a row don't run together.")
        replace_button = button("Word replacements\u2026",
                                lambda: replacements_window(self), width=170)
        replace_button.setToolTip("Fix words the speech model gets wrong, such as names it "
                                  "doesn't know: what it writes, and what you want instead.")
        row(field("Words"), replace_button, None)
        self.after = QtWidgets.QComboBox()
        for key, label in AFTER_CHOICES:
            self.after.addItem(label, key)
        self.after.setCurrentIndex(max(0, self.after.findData(AFTER_KEY)))
        def after_key_chosen(index):
            key = self.after.itemData(index)
            if key and not AFTER_KEY and not auto_key_warning(self, pretty(key)):
                self.after.blockSignals(True)    # not confirmed: back to "do nothing"
                self.after.setCurrentIndex(max(0, self.after.findData("")))
                self.after.blockSignals(False)
                self.after_delay.setEnabled(False)
                return
            set_option("AFTER_KEY", "after_key", key)

        self.after.currentIndexChanged.connect(after_key_chosen)
        self.after_delay = QtWidgets.QComboBox()
        for seconds, label in AFTER_DELAYS:
            self.after_delay.addItem(label, seconds)
        self.after_delay.setCurrentIndex(max(0, self.after_delay.findData(AFTER_DELAY)))
        self.after_delay.currentIndexChanged.connect(
            lambda index: set_option("AFTER_DELAY", "after_delay",
                                     int(self.after_delay.itemData(index))))
        self.after.setMinimumWidth(120)
        self.after_delay.setMinimumWidth(120)

        def after_changed(_index=None):          # a wait only matters if a key is pressed
            self.after_delay.setEnabled(bool(self.after.currentData()))

        self.after.currentIndexChanged.connect(after_changed)
        after_changed()
        row(field("After dictating"), self.after, self.after_delay, None)
        note("A key pressed for you once the text is typed: Enter sends a chat message, "
             "Tab moves to the next box. With a wait, a notice counts it down; click "
             "the notice to cancel.")

        # A second hotkey: dictate as usual, then press a key. For when you only
        # sometimes want that (say, to send a chat message straight away).
        action_title = field("Second hotkey")
        self.action_label = QtWidgets.QLabel("")
        self.action_label.setObjectName("info")
        self.action_record = button(self.RECORD_LABEL, lambda: record_action(), width=84)
        self.action_clear = button("Clear", lambda: clear_action(), width=84)
        self.action_key = QtWidgets.QComboBox()
        for key, label in AFTER_CHOICES:
            if key:
                self.action_key.addItem(label, key)
        self.action_key.setCurrentIndex(max(0, self.action_key.findData(ACTION_KEY)))
        self.action_key.currentIndexChanged.connect(
            lambda index: set_option("ACTION_KEY", "action_key",
                                     self.action_key.itemData(index)))
        self.action_key.setMinimumWidth(170)

        def show_action():
            self.action_label.setText(pretty(ACTION_HOTKEY) if ACTION_HOTKEY else "not set")
            self.action_clear.setEnabled(bool(ACTION_HOTKEY))
            self.action_key.setEnabled(bool(ACTION_HOTKEY))

        def action_recorded(hotkey):
            self.action_record.setText(self.RECORD_LABEL)
            self.action_record.setEnabled(True)
            if hotkey and hotkey not in (HOTKEY, PASTE_HOTKEY):
                if not ACTION_HOTKEY and not auto_key_warning(
                        self, pretty(ACTION_KEY or "enter")):
                    show_action()                # not confirmed: stays unset
                    return
                set_option("ACTION_HOTKEY", "action_hotkey", hotkey)
                dictation.apply_hotkey()
            show_action()
            if hotkey and hotkey in (HOTKEY, PASTE_HOTKEY):
                self.action_label.setText("already in use")

        def record_action():
            self.action_record.setText("Press keys")
            self.action_record.setEnabled(False)
            self.action_label.setText("Esc cancels")
            dictation.start_capture(action_recorded, main=False)

        def clear_action():
            set_option("ACTION_HOTKEY", "action_hotkey", "")
            dictation.apply_hotkey()
            show_action()

        row(action_title, self.action_record, self.action_clear, self.action_label, None)
        row(field("Then"), self.action_key, None)
        show_action()
        note("A second hotkey that dictates like the first and then presses a key. "
             "Your usual hotkey stays as it is. It can be keys like Ctrl+Shift on "
             "their own, though those will also set it off inside longer shortcuts.")
        check("Stay in the window I started in", RETURN_FOCUS,
              option("RETURN_FOCUS", "return_focus"),
              hint="If another program pops up and takes over while you are speaking, "
                   "your words still go to the window you were in when you pressed "
                   "the hotkey.")

        heading("Startup")
        check("Start with Windows", starts_with_windows(), set_startup)

        end_tab()

        # ================= Pop-up =================
        tab("Pop-up")
        def popup_changed(on):
            set_option("SHOW_POPUP", "show_popup", bool(on))
            if on:
                dictation.preview_popup()

        heading("While dictating", first=True)
        check("Show the pop-up while dictating", SHOW_POPUP,
              lambda on: popup_changed(on),
              hint="Unticked, nothing appears on screen while you dictate: you only "
                   "hear the start and finish sounds. Notices, such as a microphone "
                   "problem, still show.")
        check("Show the icon of the app you're dictating into", SHOW_APP_ICON,
              option("SHOW_APP_ICON", "show_app_icon", preview=True))
        check("Show the name of the app you're dictating into", SHOW_APP_NAME,
              option("SHOW_APP_NAME", "show_app_name", preview=True))
        check("Show the sound wave", SHOW_WAVEFORM,
              option("SHOW_WAVEFORM", "show_waveform", preview=True))
        check("Show the timer", SHOW_TIMER,
              option("SHOW_TIMER", "show_timer", preview=True))
        heading("Your words")
        check("Show words as you speak", LIVE_PREVIEW, self._live_changed)
        self.lines = QtWidgets.QComboBox()
        for count in range(1, 7):
            self.lines.addItem("%d line%s" % (count, "" if count == 1 else "s"), count)
        self.lines.setCurrentIndex(LIVE_LINES - 1)
        self.lines.currentIndexChanged.connect(self._lines_changed)
        self.wide = QtWidgets.QComboBox()
        self.wide.addItem("Narrow", False)
        self.wide.addItem("Wide", True)
        self.wide.setCurrentIndex(1 if WIDE_CARD else 0)
        self.wide.currentIndexChanged.connect(self._wide_changed)
        self.wide.setToolTip("How wide the pop-up is when it shows more than one line "
                             "of words. Narrow keeps it the usual size.")
        row(field("Words shown"), self.lines, self.wide, None)
        self.text_size = QtWidgets.QComboBox()
        for size, label in TEXT_SIZES:
            self.text_size.addItem(label, size)
        self.text_size.setCurrentIndex(max(0, self.text_size.findData(TEXT_SIZE)))
        self.text_size.currentIndexChanged.connect(self._text_size_changed)
        row(field("Text size"), self.text_size, None)
        heading("Between dictations")
        check("Always show a small status pill on screen", ALWAYS_SHOW,
              option("ALWAYS_SHOW", "always_show", refresh=True),
              tip="Shows a small pill between dictations so you can see the app is "
                  "running. The microphone stays off until you press the hotkey.")
        check("Show the hotkey text", SHOW_HOTKEY_HINT,
              lambda value: (option("SHOW_HOTKEY_HINT", "hotkey_hint", refresh=True)(value),
                             dictation.preview_pill()))
        self.pill_mark = QtWidgets.QComboBox()
        self.pill_mark.addItem("%s logo" % APP_TITLE, "logo")
        self.pill_mark.addItem("Dot", "dot")
        self.pill_mark.addItem("None", "none")
        self.pill_mark.setCurrentIndex(max(0, self.pill_mark.findData(PILL_MARK)))
        self.pill_mark.setToolTip("What the small pill shows next to the hotkey: the %s "
                                  "logo (four bars), a dot, or nothing. It is also a "
                                  "status light: with None, a dot still appears while "
                                  "something needs you." % APP_TITLE)
        self.pill_mark.currentIndexChanged.connect(
            lambda index: (set_option("PILL_MARK", "pill_mark",
                                      self.pill_mark.itemData(index)),
                           dictation.refresh_popup(), dictation.preview_pill()))
        row(field("Pill mark"), self.pill_mark, None)
        heading("Position")
        def reset_popup():
            dictation.overlay.reset_position()
            set_option("CARD_WIDTH", "card_width", 0)    # and its usual width
            dictation.refresh_popup()
            dictation.preview_popup()            # show where and how big it is now

        row(field("Pop-up"), button("Move", dictation.move_popup),
            button("Resize", dictation.resize_popup), button("Reset", reset_popup), None)
        note("You can also drag the pop-up's left or right edge while you dictate "
             "to make it wider, so more of your words fit on each line.")
        end_tab()

        # ================= Look =================
        tab("Look")
        heading("Colours", first=True)
        colour_row = [field("Colour")]
        for label, value in ACCENT_CHOICES:
            swatch = QtWidgets.QPushButton()
            swatch.setFixedSize(24, 24)
            swatch.setCursor(Qt.PointingHandCursor)
            swatch.setToolTip(label)
            swatch.clicked.connect(lambda _checked=False, v=value: self._accent_chosen(v))
            self.swatches.append((swatch, value))
            colour_row.append(swatch)
        row(*(colour_row + [button("Custom", self._accent_custom), None]))
        self._paint_swatches()

        self.backgrounds = []
        background_row = [field("Background")]
        for label, value in BACKGROUND_CHOICES:
            choice = ChoiceButton(label)
            choice.clicked.connect(lambda _checked=False, v=value: self._background_chosen(v))
            self.backgrounds.append((choice, value))
            background_row.append(choice)
        row(*(background_row + [button("Custom", self._background_custom), None]))
        self._paint_backgrounds()
        self.menu_themes = []
        menu_row = [field("Settings")]
        for label, value in (("Dark", "dark"), ("Light", "light")):
            choice = ChoiceButton(label)
            choice.setToolTip("How this window and the other %s windows look." % APP_TITLE)
            choice.clicked.connect(lambda _checked=False, v=value: set_menu_theme(v))
            self.menu_themes.append((choice, value))
            menu_row.append(choice)
        row(*(menu_row + [None]))
        self._paint_menu_theme()
        heading("Card")
        self.card_style = QtWidgets.QComboBox()
        for key, label in CARD_STYLES:
            self.card_style.addItem(label, key)
        self.card_style.setCurrentIndex(max(0, self.card_style.findData(CARD_STYLE)))
        self.card_style.setToolTip(
            "How the pop-up stands out while you dictate. Clean: a soft shadow and a "
            "crisp edge. With a colour edge: the same, with a fine line in your "
            "accent colour. Glow: a coloured glow round it (set below).")

        def card_style_chosen(index):
            set_option("CARD_STYLE", "card_style", self.card_style.itemData(index))
            self.glow.setEnabled(CARD_STYLE == "glow")
            dictation.preview_popup()

        self.card_style.currentIndexChanged.connect(card_style_chosen)
        row(field("Style"), self.card_style, None)

        heading("Glow")
        self.glow = QtWidgets.QComboBox()
        for key, label, _amount in GLOW_LEVELS:
            self.glow.addItem(label, key)
        self.glow.setCurrentIndex(max(0, self.glow.findData(GLOW)))
        self.glow.currentIndexChanged.connect(self._glow_changed)
        row(field("Pop-up glow"), self.glow, None)
        # Where the screen glow goes, or none: one choice (it replaces a
        # separate on/off box).
        self.glow_where = QtWidgets.QComboBox()
        for label, key in (("Off", "off"), ("Bottom (in the taskbar)", "bottom"),
                           ("Top", "top"), ("Left side", "left"), ("Right side", "right"),
                           ("Full-screen border", "all")):
            self.glow_where.addItem(label, key)
        self.glow_where.setCurrentIndex(max(0, self.glow_where.findData(
            SCREEN_GLOW_EDGES if SCREEN_GLOW else "off")))
        self.glow_where.setToolTip("A soft glow at the edge of the screen while you "
                                   "dictate, which reacts to your voice.")

        def glow_where_chosen(index):
            key = self.glow_where.itemData(index)
            set_option("SCREEN_GLOW", "screen_glow", key != "off")
            if key != "off":
                set_option("SCREEN_GLOW_EDGES", "screen_glow_edges", key)
            self.glow_focus.setEnabled(SCREEN_GLOW and SCREEN_GLOW_EDGES != "all")
            self.screen_strength.setEnabled(SCREEN_GLOW)
            if key == "off":                     # gone at once, no preview to wait out
                dictation.fx_demo_until = 0.0
                dictation.screen_glow.stop()
            else:
                dictation.demo_screen_glow()

        self.glow_where.currentIndexChanged.connect(glow_where_chosen)
        row(field("Screen glow"), self.glow_where, None)
        self.glow_focus = QtWidgets.QComboBox()
        self.glow_focus.addItem("In the middle", "middle")
        self.glow_focus.addItem("In the corners", "corners")
        self.glow_focus.addItem("The whole edge", "even")
        self.glow_focus.setCurrentIndex(max(0, self.glow_focus.findData(SCREEN_GLOW_FOCUS)))
        self.glow_focus.setToolTip("Where a glow along one side is strongest: rising in "
                                   "the middle, gathered in its two corners, or an even "
                                   "line along the whole edge. Greyed out with the "
                                   "full-screen border, which is even all round.")
        self.glow_focus.currentIndexChanged.connect(
            lambda index: (set_option("SCREEN_GLOW_FOCUS", "screen_glow_focus",
                                      self.glow_focus.itemData(index)),
                           dictation.demo_screen_glow()))
        row(field("Light from"), self.glow_focus, None)

        def glow_slider(label, name, key, low, high, value):
            slider = QtWidgets.QSlider(Qt.Horizontal)
            slider.setRange(low, high)
            slider.setValue(int(value))
            slider.setFixedWidth(200)

            def moved(_value=None):              # shows the result as you drag
                globals()[name] = int(slider.value())
                if not slider.isSliderDown():    # arrow keys, a click on the track
                    save_setting(key, int(slider.value()))
                self.dictation.demo_screen_glow()   # these change the screen glow only

            slider.valueChanged.connect(moved)
            slider.sliderReleased.connect(
                lambda: set_option(name, key, int(slider.value())))
            row(field(label), slider, None)
            return slider

        self.screen_strength = glow_slider("Strength", "SCREEN_GLOW_STRENGTH",
                                           "screen_glow_strength", 5, 100,
                                           SCREEN_GLOW_STRENGTH)
        self.glow_focus.setEnabled(SCREEN_GLOW and SCREEN_GLOW_EDGES != "all")
        self.screen_strength.setEnabled(SCREEN_GLOW)

        def more_glow_options():
            """The fine-tuning, in a small window of its own. Built with the
            same helpers as the tabs, pointed at this window for the moment."""
            box = QtWidgets.QDialog(self)
            box.setWindowTitle("More glow options")
            box.setWindowIcon(app_icon())
            box.setStyleSheet(settings_style())
            dark_title_bar(box)
            inside = QtWidgets.QVBoxLayout(box)
            inside.setContentsMargins(20, 16, 20, 16)
            inside.setSpacing(8)
            outer_layout, page["layout"] = page["layout"], inside
            try:
                self.glow_colour = QtWidgets.QComboBox()
                self.glow_colour.addItem("The app's icon colour", "app")
                self.glow_colour.addItem("My accent colour", "accent")
                self.glow_colour.addItem("Custom\u2026", "custom")

                def glow_source():
                    return "app" if GLOW_FROM_APP else ("custom" if GLOW_COLOUR else "accent")

                def show_glow_source():
                    self.glow_colour.blockSignals(True)
                    self.glow_colour.setCurrentIndex(
                        max(0, self.glow_colour.findData(glow_source())))
                    self.glow_colour.blockSignals(False)

                def glow_colour_changed(index):
                    choice = self.glow_colour.itemData(index)
                    if choice == "custom":
                        before = GLOW_COLOUR
                        was_app = GLOW_FROM_APP
                        globals()["GLOW_FROM_APP"] = False       # so the picker's preview shows it

                        def try_out(colour):
                            globals()["GLOW_COLOUR"] = colour    # shown, not yet saved
                        picked = self._pick_colour("Glow colour", GLOW_COLOUR or ACCENT, try_out,
                                                   edge_glow=True)
                        if picked:
                            set_option("GLOW_FROM_APP", "glow_from_app", False)
                            set_option("GLOW_COLOUR", "glow_colour", picked)
                        else:                                    # cancelled: as it was
                            globals()["GLOW_COLOUR"] = before
                            globals()["GLOW_FROM_APP"] = was_app
                            show_glow_source()
                        return
                    set_option("GLOW_FROM_APP", "glow_from_app", choice == "app")
                    if choice == "accent":
                        set_option("GLOW_COLOUR", "glow_colour", "")
                    self._flash_glow()

                show_glow_source()
                self.glow_colour.currentIndexChanged.connect(glow_colour_changed)
                self.glow_colour.setToolTip("Used by the glow round the pop-up and the glow "
                                            "round the screen. The app's icon colour changes "
                                            "with whichever app you are dictating into.")
                row(field("Glow colour"), self.glow_colour, None)

                self.screen_size = glow_slider("Edge size", "SCREEN_GLOW_SIZE",
                                               "screen_glow_size", 20, 200, SCREEN_GLOW_SIZE)
                self.glow_screens = QtWidgets.QComboBox()
                self.glow_screens.addItem("The pop-up's screen", "popup")
                self.glow_screens.addItem("All screens", "all")
                self.glow_screens.addItem("Main screen", "main")
                self.glow_screens.setCurrentIndex(
                    max(0, self.glow_screens.findData(SCREEN_GLOW_SCREENS)))
                self.glow_screens.setToolTip("Which screens the glow shows on while you dictate.")
                self.glow_screens.currentIndexChanged.connect(
                    lambda index: (set_option("SCREEN_GLOW_SCREENS", "screen_glow_screens",
                                              self.glow_screens.itemData(index)),
                                   dictation.demo_screen_glow()))
                row(field("Glow on"), self.glow_screens, None)

                def flow_changed(value):
                    set_option("SCREEN_GLOW_FLOW", "screen_glow_flow", bool(value))
                    dictation.demo_screen_glow()
                check("Moving light", SCREEN_GLOW_FLOW, flow_changed,
                      hint="A light travels along the glow while you dictate, and it "
                           "ripples with your voice. Off: one still colour, a little "
                           "lighter on the PC.")
                heading("Outline")
                check("Outline in the accent colour", ACCENT_OUTLINE,
                      option("ACCENT_OUTLINE", "accent_outline", refresh=True, preview=True))
                check("Moving light on the outline", OUTLINE_LIGHT,
                      option("OUTLINE_LIGHT", "outline_light", refresh=True, preview=True))

                closing = QtWidgets.QHBoxLayout()
                closing.addStretch(1)
                done = QtWidgets.QPushButton("Close")
                done.clicked.connect(lambda _checked=False: box.accept())
                closing.addWidget(done)
                inside.addLayout(closing)
            finally:
                page["layout"] = outer_layout
            box.exec()
            box.deleteLater()                    # not kept: one window per opening

        more = button("More glow options\u2026", more_glow_options, width=170)
        more.setToolTip("Glow colour, size, which screens, the moving light and the "
                        "pop-up's outline.")
        row(field(""), more, None)
        heading("Tray icon")
        check("Tray icon updates with your dictation status", TRAY_LIVE,
              option("TRAY_LIVE", "tray_live"))
        end_tab()

        # ================= Audio =================
        tab("Audio")
        heading("Microphone", first=True)
        self.mic = QtWidgets.QComboBox()
        # Takes the room the row has; a long device name is shortened with "…"
        # rather than pushing the row past the window's edge.
        self.mic.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.mic.setMinimumContentsLength(12)
        self.mic.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self._fill_microphones()
        self.mic.currentIndexChanged.connect(self._mic_changed)
        line = QtWidgets.QHBoxLayout()
        line.setSpacing(8)
        line.addWidget(field("Input"))
        line.addWidget(self.mic, 1)
        line.addWidget(button("Refresh", self._fill_microphones))
        page["layout"].addLayout(line)
        self.mic_test = button("Test", self._test_mic, width=84)
        self.mic_bar = QtWidgets.QProgressBar()
        self.mic_bar.setRange(0, 100)
        self.mic_bar.setTextVisible(False)
        self.mic_bar.setMinimumWidth(60)
        self.mic_note = QtWidgets.QLabel("")
        self.mic_note.setObjectName("info")
        line = QtWidgets.QHBoxLayout()
        line.setSpacing(8)
        line.addWidget(field("Level"))
        line.addWidget(self.mic_test)
        line.addWidget(self.mic_bar, 1)
        line.addWidget(self.mic_note)
        page["layout"].addLayout(line)
        self.mic_heard = QtWidgets.QLabel("")    # the words heard during a test
        self.mic_heard.setWordWrap(True)
        self.mic_heard.setMinimumHeight(34)
        self.mic_heard.setContentsMargins(2, 0, 0, 0)
        self.mic_heard.setVisible(False)         # only during a test: no empty gap
        page["layout"].addWidget(self.mic_heard)
        note("Test shows the level and the words it hears, for about 12 seconds. "
             "Nothing is typed anywhere and nothing is kept.")
        self._test_audio = []                    # the test's sound, in memory only
        self._test_text = ""
        self._test_busy = False
        self._test_ticket = 0
        self._test_ticks = 0
        self._test_stream = None
        self._test_level = 0.0
        self._test_until = 0.0
        self._test_timer = QtCore.QTimer(self)
        self._test_timer.setInterval(50)
        self._test_timer.timeout.connect(self._test_tick)

        self.limit = QtWidgets.QComboBox()
        limits = [60, 120, 300, 600, 1800]
        if MAX_RECORD_SECONDS not in limits:
            limits.append(MAX_RECORD_SECONDS)
        for seconds in sorted(limits):
            self.limit.addItem("%d minute%s" % (seconds // 60, "" if seconds == 60 else "s")
                               if seconds % 60 == 0 else "%d seconds" % seconds, seconds)
        self.limit.setCurrentIndex(max(0, self.limit.findData(MAX_RECORD_SECONDS)))
        self.limit.currentIndexChanged.connect(
            lambda index: set_option("MAX_RECORD_SECONDS", "max_seconds",
                                     int(self.limit.itemData(index))))
        row(field("Time limit"), self.limit, None)
        note("A dictation ends by itself after this long. This is a safety net: if the "
             "hotkey gets stuck or a dictation is accidentally left running, it will "
             "stop automatically after this time.")

        heading("Sounds")
        check("Play a sound when dictation starts and finishes", SOUNDS, set_sounds)
        self.speaker = QtWidgets.QComboBox()
        self.speaker.setFixedWidth(250)
        self.speaker.addItem("Windows default", "")
        outputs = output_devices()
        if SOUND_DEVICE and SOUND_DEVICE not in outputs:
            outputs.insert(0, SOUND_DEVICE)      # chosen earlier, unplugged right now
        for name in outputs:
            self.speaker.addItem(name, name)
        self.speaker.setCurrentIndex(max(0, self.speaker.findData(SOUND_DEVICE)))

        def speaker_changed(index):
            set_option("SOUND_DEVICE", "sound_device", self.speaker.itemData(index) or "")
            play_sound("start")                  # hear where it now plays

        self.speaker.currentIndexChanged.connect(speaker_changed)
        row(field("Output"), self.speaker, None)
        self.sound_set = QtWidgets.QComboBox()
        for key, label in SOUND_SETS:
            self.sound_set.addItem(label, key)
        self.sound_set.setCurrentIndex(max(0, self.sound_set.findData(SOUND_SET)))
        self.sound_set.currentIndexChanged.connect(self._sound_set_changed)
        row(field("Sound set"), self.sound_set, button("Play", self._play_both), None)

        self.own_rows = []
        for which, label in (("start", "Start sound"), ("finish", "Finish sound")):
            name = QtWidgets.QLabel("")
            name.setObjectName("info")
            browse = QtWidgets.QPushButton("Browse")
            browse.clicked.connect(lambda _checked=False, w=which: self._browse_sound(w))
            title = field(label)
            row(title, browse, name, None)
            self.own_rows.append((which, title, browse, name))

        self.volume = QtWidgets.QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(round(SOUND_VOLUME * 100)))
        self.volume.setFixedWidth(200)
        self.volume.sliderReleased.connect(self._volume_changed)
        # A click on the track, the arrow keys or the mouse wheel move it too:
        # save those as well (a drag is saved once, when it is let go).
        self.volume.valueChanged.connect(
            lambda _value: None if self.volume.isSliderDown() else self._volume_changed())
        self.volume_label = field("Volume")
        row(self.volume_label, self.volume, None)
        self._refresh_own_rows()
        end_tab()

        # ================= Model =================
        tab("Model")
        heading("Speech model", first=True)
        self.model = QtWidgets.QComboBox()
        for name, label, _quant, _size in MODELS:
            self.model.addItem(label, name)
        if self.model.findData(MODEL) < 0:
            self.model.addItem(MODEL, MODEL)     # one set by hand in the file
        self.model.setCurrentIndex(max(0, self.model.findData(MODEL)))
        self.model.currentIndexChanged.connect(self._model_changed)
        row(field("Model"), self.model, None)
        note("Runs on this PC. Choosing a model that is not on this PC yet asks first, "
             "then restarts the application to download it.")

        def open_model_folder():
            folder = model_location()[0]
            if not folder:
                dialog(self, "Speech model", "The model is not on this PC",
                       "There is no model folder to open yet.")
                return
            try:
                os.startfile(folder)
            except Exception as exc:
                print("Could not open the model's folder:", exc, file=sys.stderr)

        def model_present():
            return bool(model_files()) or bool(model_location()[0])

        def show_model_button():
            self.model_button.setText("Remove model" if model_present()
                                      else "Download model")

        def remove_or_download():
            if not model_present():
                # Starting afresh is how a download begins: the application asks
                # first, exactly as it did the very first time.
                if dialog(self, "Download the speech model?", "The application will restart",
                          "It then asks before going online to download the model, as it "
                          "did the first time.", accept="Restart", reject="Cancel"):
                    restart_app()
                return
            folder, megabytes = model_location()
            if dialog(self, "Remove the speech model?",
                      "Delete the speech model from this PC",
                      "This deletes the model's files%s. The application stays open so "
                      "you can still change settings, but dictation will not work until "
                      "the model is downloaded again.\n\nYour settings are kept.\n\n"
                      "Folder:\n%s"
                      % ((" (about %d MB)" % megabytes) if megabytes else "", folder),
                      accept="Remove", reject="Cancel"):
                if not dictation.unload_model():
                    dialog(self, "Still loading", "The speech model is still loading",
                           "Please wait until it has loaded, then try again.")
                    return
                report = forget_model() or {}
                show_model_button()
                lines = ["Deleted from this PC: about %d MB." % report.get("freed", 0)]
                if report.get("left"):
                    lines.append("Windows is still holding some of the files open. They "
                                 "will be deleted the next time the application starts:"
                                 "\n%s" % "\n".join(report["left"]))
                elif not report.get("removed"):
                    lines.append("No model folder was found.")
                lines.append("Dictation is off until the model is downloaded again. To do "
                             "that, click \"Download model\" here, or just start the "
                             "application again later: it asks before downloading.")
                dialog(self, "Speech model removed", "The speech model has been removed",
                       "\n\n".join(lines))

        heading("Files")
        self.model_button = button("Remove model", remove_or_download)
        row(button("Check files", lambda: model_check_window(self)),
            button("Open folder", open_model_folder), self.model_button, None)
        show_model_button()
        note("Check files compares the model's files with their known fingerprints. "
             "Removing frees about 630 MB; it can be downloaded again later.")

        note("A newer speech model only ever comes with a newer version of %s, "
             "together with its fingerprints (see Updates on the Privacy tab)." % APP_TITLE)

        end_tab()

        # ================= Privacy =================
        tab("Privacy")
        heading("Updates", first=True)
        self.app_updates = check(
            "Check for %s updates at startup" % APP_TITLE, CHECK_APP_UPDATES,
            self._app_updates_changed,
            tip="Off by default. With this ticked, every start asks github.com "
                "whether a newer version of the application has been published, and "
                "tells you once if there is. Nothing is downloaded or installed.")
        self.app_update_button = button("Check now", self._check_app_now, width=96)
        self.app_update_button.setToolTip("Checks once, now, for a newer version of the "
                                          "application (goes online after asking; only "
                                          "reads the latest version number).")
        page["layout"].removeWidget(self.app_updates)
        row(self.app_updates, None, self.app_update_button)
        note("You have version %s." % VERSION)

        heading("Clipboard and kept text")
        self.paste_mode = check(
            "Paste using the clipboard instead of typing", not TYPE_TEXT,
            self._paste_mode_changed,
            tip="Off by default. The words are normally typed as keystrokes, so "
                "the clipboard is not used.\n\n"
                "With this on, the text is pasted instead: it borrows the "
                "clipboard for a second, then puts back what you had copied. "
                "Pasting is quicker for long text and safer if no text box is "
                "selected, but other programs that watch the clipboard could see "
                "the text during that second.")
        minutes = round(KEEP_LAST_SECONDS / 60.0)
        self.keep_last = check(
            "Missed text safety", KEEP_LAST, self._keep_last_changed,
            tip="Why this exists: if no text box is selected when you finish "
                "speaking, the words have nowhere to go and are lost.\n\n"
                "With this on, the app keeps your last dictation for %d minutes so a "
                "quick tap of the hotkey pastes it again once you have clicked the "
                "right box.\n\n"
                "Off by default, because it means holding on to your words after "
                "you have finished. They stay in this program's memory only: not "
                "written to disk or left on the clipboard." % minutes,
            hint="Keeps your last dictation for %d minutes so you can paste it again "
                 "if needed." % minutes)
        self.paste_title = field("Paste hotkey")
        self.paste_label = QtWidgets.QLabel("")
        self.paste_label.setObjectName("info")
        row(self.paste_title, self.paste_label, None)
        self.paste_record = button(self.RECORD_LABEL, self._record_paste_hotkey, width=84)
        self.paste_clear = button("Clear", self._clear_paste_hotkey, width=84)
        row(field(""), self.paste_record, self.paste_clear, None)
        self._refresh_paste_row()
        heading("Move settings to another PC")
        note("Your choices as one line of text, to copy and paste on another PC. "
             "Nothing private is included.")
        button_grid(button("Export settings", lambda: export_settings_window(self)),
                    button("Import settings", lambda: import_settings_window(self)))

        heading("Help and information")
        network = QtWidgets.QLabel(network_words(short=True))
        network.setObjectName("info")
        network.setWordWrap(True)
        network.setToolTip("%s blocks its own internet access while it runs, and checks "
                           "that the block works each time it starts. It only opens for "
                           "a download you agree to." % APP_TITLE)
        row(field("Internet"), network)
        button_grid(button("Report a problem", lambda: open_feedback_page(self, "bug")),
                    button("Suggest an idea", lambda: open_feedback_page(self, "idea")),
                    button("Open data folder", open_data_folder),
                    button("About %s" % APP_TITLE, lambda: show_about(dictation)))
        if uninstaller_path():
            button_grid(button("Uninstall %s\u2026" % APP_TITLE,
                               lambda: start_uninstall(self)))
        end_tab()
        # Open on the tab that was showing last time (remembered only while
        # the application is running; nothing is written to disk for this).
        tabs.setCurrentIndex(min(_settings_open.get("tab", 0), tabs.count() - 1))
        tabs.currentChanged.connect(lambda index: _settings_open.update(tab=int(index)))
        self._tabs = tabs
        tabs.currentChanged.connect(lambda _index: self._fit_height())

        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(button("Reset all settings", self._reset_all))

        def quit_app():
            print("Quit from the settings window.")
            leave_now(QtWidgets.QApplication.instance())

        bottom.addStretch(1)                     # Reset on the left; the two ways out on the right
        quit_button = button("Quit %s" % APP_TITLE, quit_app)
        quit_button.setObjectName("danger")      # red: it closes the application
        bottom.addWidget(quit_button)
        bottom.addWidget(button("Close", self.close, width=84))
        outer.addLayout(bottom)

    # -- size: fits the tab that is showing, and never more than the screen ----
    def showEvent(self, event):
        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self._fit_height)

    def _fit_height(self):
        """Make the window as tall as the tab that is showing needs, so a short
        tab has no empty space at the bottom, but never taller than the screen:
        on a small screen the tab scrolls instead, and Close and Quit stay in
        view."""
        try:
            area = self._tabs.currentWidget()
            content = area.widget().sizeHint().height()       # the tab, unclipped
            around = self.height() - area.viewport().height() # tabs, buttons, margins
            screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
            room = screen.availableGeometry()
            title_bar = max(0, self.frameGeometry().height() - self.height())
            limit = room.height() - title_bar - 16
            height = max(320, min(around + content, limit))
            if height != self.height():
                self.resize(self.width(), height)
            frame = self.frameGeometry()
            if frame.bottom() > room.bottom() or frame.top() < room.top():
                self.move(self.x(), max(room.top(), room.bottom() - frame.height()))
        except Exception as exc:
            print("Settings could not fit its height:", exc, file=sys.stderr)

    # -- handlers ------------------------------------------------------------
    def _paint_swatches(self):
        try:
            ratio = max(1.0, float(self.devicePixelRatioF())) * 2.0
        except Exception:
            ratio = 3.0
        for button, value in self.swatches:
            button.setStyleSheet("background: transparent; border: none; padding: 0; "
                                 "min-height: 0;")
            button.setIcon(QtGui.QIcon(swatch_picture(value,
                                                      value.lower() == ACCENT.lower(),
                                                      ratio)))
            button.setIconSize(QtCore.QSize(24, 24))

    def _paint_backgrounds(self):
        for button, value in self.backgrounds:
            button.setChosen(value.lower() == POPUP_BG.lower())

    def _paint_menu_theme(self):
        for button, value in getattr(self, "menu_themes", []):
            button.setChosen(value == MENU_THEME)

    def _background_chosen(self, colour):
        set_option("POPUP_BG", "popup_bg", colour)
        self._paint_backgrounds()
        self.dictation.overlay.update()
        self.dictation.preview_popup()

    def _pick_colour(self, title, start, try_out, edge_glow=False):
        """Open a colour picker that shows each colour on the pop-up as you move
        through them. Returns the chosen colour's name, or None if cancelled
        (the caller's try_out is then called with the starting colour again).
        edge_glow=True: the colour also tints the glow round the screen, so that
        stays on, following each colour, for as long as the picker is open."""
        picker = QtWidgets.QColorDialog(QtGui.QColor(start), self)
        picker.setWindowTitle(title)
        picker.setOption(QtWidgets.QColorDialog.DontUseNativeDialog, True)   # live updates
        picker.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        dictation = self.dictation
        overlay = dictation.overlay
        can_show = dictation.state in ("idle", "loading", "nomodel") and overlay.mode != "welcome"
        show_edges = can_show and edge_glow and SCREEN_GLOW

        def edges_on():                          # (re)start, in the colour tried now
            dictation.fx_demo_until = time.monotonic() + 3600
            dictation._start_effects(demo=True)

        if can_show:
            overlay.preview(ms=None)             # stays up for as long as you are choosing
        if show_edges:
            edges_on()

        def tried(colour):
            try_out(colour.name())
            overlay.update()
            if show_edges:
                edges_on()

        picker.currentColorChanged.connect(tried)
        accepted = picker.exec() == QtWidgets.QDialog.Accepted
        chosen = picker.selectedColor().name() if accepted else None
        if not accepted:
            try_out(start)
        if show_edges:                           # show the result a moment, then fade
            edges_on()
            dictation.fx_demo_until = time.monotonic() + 1.2
        if can_show and overlay.previewing:
            overlay.preview(ms=1600)             # linger a moment on the result, then go
        return chosen

    def _background_custom(self):
        def try_out(colour):
            globals()["POPUP_BG"] = colour       # shown, not yet saved
        chosen = self._pick_colour("Pop-up background", POPUP_BG, try_out)
        if chosen:
            self._background_chosen(chosen)

    def _accent_chosen(self, colour):
        set_accent(colour)
        self._paint_swatches()
        self._paint_backgrounds()
        self.setStyleSheet(settings_style())     # ticks and slider follow the colour
        icon = app_icon()                        # so do the tray and window icons
        self.setWindowIcon(icon)
        if self.tray is not None:
            self.tray.setIcon(icon)
        self.dictation.overlay.update()
        self.dictation.preview_popup()

    def _accent_custom(self):
        def try_out(colour):
            globals()["ACCENT"] = colour         # shown, not yet saved
        # The screen glow uses the accent colour unless a glow colour is chosen.
        chosen = self._pick_colour("Accent colour", ACCENT, try_out,
                                   edge_glow=not GLOW_COLOUR and not GLOW_FROM_APP)
        if chosen:
            self._accent_chosen(chosen)

    def _hotkey_changed(self, index):
        if index < 0:
            return
        if self.dictation.state == "recording":
            # Not while dictating: put the list back so it shows the real hotkey.
            self.hotkey.blockSignals(True)
            self.hotkey.setCurrentIndex(max(0, self.hotkey.findData(HOTKEY)))
            self.hotkey.blockSignals(False)
            return
        self.dictation.change_hotkey(self.hotkey.itemData(index))
        self._refresh_hotkey_note()
        self._refresh_paste_row()

    def _record_hotkey(self):
        self.record.setText("Press keys")
        self.record.setEnabled(False)
        self.hotkey_note.setText("Press the key or keys you want to use. Esc cancels.")
        self.hotkey_note.show()
        self.dictation.start_capture(self._hotkey_recorded)

    def _hotkey_recorded(self, hotkey):
        self.record.setText(self.RECORD_LABEL)
        self.record.setEnabled(True)
        if not hotkey:
            self._refresh_hotkey_note()
            return
        index = self.hotkey.findData(hotkey)
        if index < 0:                            # a new one: add it to the list
            self.hotkey.insertItem(0, pretty(hotkey), hotkey)
            index = 0
        self.hotkey.blockSignals(True)
        self.hotkey.setCurrentIndex(index)
        self.hotkey.blockSignals(False)
        self._refresh_hotkey_note()

    def _refresh_hotkey_note(self):
        if "+" not in HOTKEY and not HOTKEY.startswith("f"):
            self.hotkey_note.setText(
                "%s on its own is the hotkey, so that key won't type anything "
                "while this program is running." % pretty(HOTKEY))
            self.hotkey_note.show()
        else:
            self.hotkey_note.hide()

    def closeEvent(self, event):
        self.dictation.cancel_capture()          # stop waiting for a hotkey
        self._stop_test()                        # and never leave the mic test running
        super().closeEvent(event)

    def keyPressEvent(self, event):
        # Esc would only hide the window, skipping the tidy-up above (the
        # microphone test would carry on): close it properly instead.
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def _mode_changed(self, index):
        set_hold_to_talk(bool(self.mode.itemData(index)))
        self.dictation.overlay.update()

    def _live_changed(self, on):
        set_live_preview(on)
        self.dictation.refresh_popup()
        self.dictation.preview_popup()

    def _model_changed(self, index):
        name = self.model.itemData(index)
        if name == MODEL:
            return
        size = dict((entry[0], entry[3]) for entry in MODELS).get(name, "several hundred MB")
        have = is_downloaded(name)
        if have:
            agreed = dialog(self, "Switch speech model?",
                            "Switch to %s" % model_label(name),
                            "This model is already on this PC. The application will "
                            "restart to load it, and stays offline.",
                            accept="Switch and restart", reject="Cancel")
        else:
            agreed = dialog(self, "Download speech model?",
                            "This application will go online",
                            "%s is not on this PC yet.\n\nThe application will restart "
                            "and connect to github.com once to download it (%s) from this "
                            "project's model page. github.com is a third-party website "
                            "that we do not run or control. Every file is checked before "
                            "it is used, and after that it works offline again."
                            % (model_label(name), size),
                            accept="Download and restart", reject="Cancel")
        if agreed:
            save_setting("model", name)
            if not have:
                # Only when it must be downloaded: the yes just given covers this
                # one restart. (A model already here loads offline, as the window
                # said: the network stays shut.)
                save_setting("download_yes_once", name)
            restart_app()
        else:                                    # put the list back as it was
            self.model.blockSignals(True)
            self.model.setCurrentIndex(max(0, self.model.findData(MODEL)))
            self.model.blockSignals(False)

    def _glow_changed(self, index):
        set_option("GLOW", "glow", self.glow.itemData(index))
        self.dictation.refresh_popup()
        self.dictation.preview_popup()           # the pop-up's own glow: the pop-up only

    def _flash_glow(self):
        """Show a glow change for a moment: the pop-up, and the screen glow too."""
        self.dictation.preview_popup()
        if SCREEN_GLOW:
            self.dictation.demo_screen_glow()

    def _text_size_changed(self, index):
        set_option("TEXT_SIZE", "text_size", int(self.text_size.itemData(index)))
        self.dictation.refresh_popup()
        self.dictation.preview_popup()

    def _wide_changed(self, index):
        set_option("WIDE_CARD", "wide_card", bool(self.wide.itemData(index)))
        set_option("CARD_WIDTH", "card_width", 0)        # back to a standard width
        self.dictation.refresh_popup()

    def _lines_changed(self, index):
        set_option("LIVE_LINES", "live_lines", int(self.lines.itemData(index)))
        self.dictation.refresh_popup()

    def _app_updates_changed(self, on):
        if on and not ask_app_update_check(self, at_startup=True):
            self.app_updates.blockSignals(True)  # untick it again, quietly
            self.app_updates.setChecked(False)
            self.app_updates.blockSignals(False)
            on = False
        set_check_app_updates(on)

    def _check_app_now(self):
        if not ask_app_update_check(self):
            return
        self.app_update_button.setEnabled(False)
        self.app_update_button.setText("Checking…")

        def done():
            try:
                self.app_update_button.setEnabled(True)
                self.app_update_button.setText("Check now")
            except RuntimeError:
                pass                             # the window was closed meanwhile

        run_app_update_check(self, manual=True, done=done)

    # -- missed text safety: the paste-again hotkey -----------------------------
    def _refresh_paste_row(self):
        if PASTE_HOTKEY:
            self.paste_label.setText(pretty(PASTE_HOTKEY))
        else:
            self.paste_label.setText("quick tap of %s" % pretty(HOTKEY))
        for widget in (self.paste_title, self.paste_record, self.paste_label):
            widget.setEnabled(KEEP_LAST)
        self.paste_clear.setEnabled(KEEP_LAST and bool(PASTE_HOTKEY))

    def _record_paste_hotkey(self):
        self.paste_record.setText("Press keys")
        self.paste_record.setEnabled(False)
        self.paste_label.setText("Esc cancels")
        self.dictation.start_capture(self._paste_hotkey_recorded, main=False)

    def _paste_hotkey_recorded(self, hotkey):
        self.paste_record.setText(self.RECORD_LABEL)
        taken = hotkey and hotkey in (HOTKEY, ACTION_HOTKEY)
        if hotkey and not taken:
            set_option("PASTE_HOTKEY", "paste_hotkey", hotkey)
            self.dictation.apply_hotkey()
        self._refresh_paste_row()
        if taken:
            self.paste_label.setText("%s is already in use" % pretty(hotkey))

    def _clear_paste_hotkey(self):
        set_option("PASTE_HOTKEY", "paste_hotkey", "")
        self.dictation.apply_hotkey()
        self._refresh_paste_row()

    # -- reset ------------------------------------------------------------------
    def _reset_all(self):
        if dialog(self, "Reset all settings?", "Put every setting back to how it was at first",
                  "This includes the hotkey, colours, sounds, the pop-up's position and "
                  "starting with Windows.\n\nThe application will restart.",
                  accept="Reset", reject="Cancel"):
            reset_all_settings()

    def _paste_mode_changed(self, on):
        if not on:
            set_type_text(True)                  # back to typing: clipboard untouched
            return
        if dialog(self, "Paste using the clipboard?",
                  "Your words will pass through the clipboard",
                  "Instead of typing your words, the application will put them on the "
                  "clipboard for about a second, paste them, and then put back what "
                  "you had copied.\n\nThey are marked so that Windows keeps them out "
                  "of clipboard history and clipboard sync, but other programs that "
                  "watch the clipboard could see them during that second.",
                  accept="Turn on", reject="Keep typing"):
            set_type_text(False)
        else:
            self.paste_mode.blockSignals(True)
            self.paste_mode.setChecked(False)
            self.paste_mode.blockSignals(False)
            set_type_text(True)

    def _keep_last_changed(self, on):
        if not on:
            set_option("KEEP_LAST", "keep_last", False)
            self.dictation.forget()              # drop anything being held, now
            self._refresh_paste_row()
            return
        minutes = round(KEEP_LAST_SECONDS / 60.0)
        if dialog(self, "Turn on missed text safety?",
                  "Your last dictation will be kept for %d minutes" % minutes,
                  "This is so a quick tap of the hotkey can paste it again if it missed "
                  "the text box.\n\nThe words are held in this program's memory only. "
                  "leetsay does not write them to disk or leave them on the clipboard, and "
                  "they are dropped after %d minutes, at your next dictation, or when "
                  "the program closes." % minutes, accept="Turn on", reject="Keep off"):
            set_option("KEEP_LAST", "keep_last", True)
        else:
            self.keep_last.blockSignals(True)
            self.keep_last.setChecked(False)
            self.keep_last.blockSignals(False)
            set_option("KEEP_LAST", "keep_last", False)
        self._refresh_paste_row()

    def _refresh_own_rows(self):
        """The two file rows are always shown; they say what will play."""
        own = SOUND_SET == "custom"
        for which, title, browse, name in self.own_rows:
            path = SOUND_START_FILE if which == "start" else SOUND_FINISH_FILE
            if path and os.path.isfile(path):
                text = os.path.basename(path)
                if not own:
                    text += "  (not in use)"
            elif path:
                text = "file not found"
            else:
                text = "none chosen"
            name.setText(text)
        self.volume.setEnabled(not own)          # your own files play at their own level
        self.volume_label.setEnabled(not own)

    # -- microphone ---------------------------------------------------------------
    def _fill_microphones(self):
        self.mic.blockSignals(True)
        self.mic.clear()
        self.mic.addItem("Windows default", "")
        names = input_devices()
        if MIC_DEVICE and MIC_DEVICE not in names:
            names.insert(0, MIC_DEVICE)          # chosen earlier, unplugged right now
        for name in names:
            self.mic.addItem(name, name)
        self.mic.setCurrentIndex(max(0, self.mic.findData(MIC_DEVICE)))
        self.mic.blockSignals(False)

    def _mic_changed(self, index):
        self._stop_test()
        set_option("MIC_DEVICE", "mic", self.mic.itemData(index) or "")

    def _test_mic(self):
        """Listen for a few seconds and show the level and the words heard, so
        you can see the chosen microphone and the dictation both work. The
        words only appear here: nothing is typed, and the sound is dropped
        when the test ends."""
        if self._test_stream is not None:
            self._stop_test()
            return
        if self.dictation.state == "recording":
            return

        def heard(indata, frames, time_info, status):
            block = indata[:, 0]
            self._test_level = float(np.sqrt(np.mean(block * block))) if len(block) else 0.0
            self._test_audio.append(block.copy())

        try:
            self._test_stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                               dtype="float32", callback=heard,
                                               device=chosen_input())
            self._test_stream.start()
        except Exception as exc:
            print("Microphone test failed:", exc, file=sys.stderr)
            self._test_stream = None
            self.mic_note.setText("couldn't open it")
            return
        self._test_level = 0.0
        self._test_until = time.monotonic() + 12.0
        self._test_ticket += 1
        self._test_audio = []
        self._test_text = ""
        self._test_ticks = 0
        self.mic_test.setText("Stop")
        self.mic_note.setText("say something")
        self.mic_heard.setText("" if self.dictation.model is not None
                               else "The speech model is still loading, so this "
                                    "test only shows the level.")
        self.mic_heard.setVisible(True)          # room for the words, during the test
        self._fit_height()
        self._test_timer.start()

    def _test_tick(self):
        self.mic_bar.setValue(int(min(1.0, (self._test_level * 10.0) ** 0.7) * 100))
        if self._test_text:
            self.mic_heard.setText(self._test_text)
        self._test_ticks += 1
        if (self._test_ticks % 12 == 0 and not self._test_busy
                and self.dictation.model is not None and self._test_audio):
            sound = np.concatenate(list(self._test_audio))
            if len(sound) > SAMPLE_RATE // 2:
                self._test_busy = True
                threading.Thread(target=self._test_read,
                                 args=(sound, self._test_ticket), daemon=True).start()
        if time.monotonic() > self._test_until:
            self._stop_test()

    def _test_read(self, sound, ticket):
        """Read the test's sound (on a worker thread). The result is shown in
        Settings only."""
        try:
            sound = leveled(sound)
            with self.dictation.model_lock:
                text = str(self.dictation.model.recognize(sound, sample_rate=SAMPLE_RATE))
            if ticket == self._test_ticket:
                self._test_text = " ".join(text.split())
        except Exception:
            pass
        finally:
            self._test_busy = False

    def _stop_test(self):
        self._test_timer.stop()
        self._test_ticket += 1                   # a reading still running is ignored
        self._test_audio = []                    # the sound is dropped here
        stream, self._test_stream = self._test_stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            self.mic_note.setText("")
        self.mic_bar.setValue(0)
        self.mic_test.setText("Test")
        try:     # the test is over: its words go too, and the gap closes
            self.mic_heard.setText("")
            self.mic_heard.setVisible(False)
            self._fit_height()
        except RuntimeError:
            pass                                 # the window is already gone

    def _sound_set_changed(self, index):
        set_option("SOUND_SET", "sound_set", self.sound_set.itemData(index))
        try:
            _prepare_sound_files()
        except Exception:
            pass
        self._refresh_own_rows()
        if SOUND_SET != "custom":
            play_sound("start")

    def _browse_sound(self, which):
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose the %s sound" % which, "", "WAV sounds (*.wav)")
        if not path:
            return
        if which == "start":
            set_option("SOUND_START_FILE", "sound_start", path)
        else:
            set_option("SOUND_FINISH_FILE", "sound_finish", path)
        if SOUND_SET != "custom":                # choosing a file means you want it used
            index = self.sound_set.findData("custom")
            if index >= 0:
                self.sound_set.setCurrentIndex(index)    # runs _sound_set_changed
            else:
                set_option("SOUND_SET", "sound_set", "custom")
        self._refresh_own_rows()
        play_sound(which)

    def _play_both(self):
        play_sound("start")
        QtCore.QTimer.singleShot(700, lambda: play_sound("finish"))

    def _volume_changed(self):
        set_option("SOUND_VOLUME", "volume", self.volume.value() / 100.0)
        try:
            _prepare_sound_files()
        except Exception:
            pass
        play_sound("start")


_settings_open = {"window": None, "tab": 0}      # keeps the window alive, and
                                                 # which tab was last open


def open_settings(dictation):
    """Show Settings (from the tray icon, or a right-click on the pill)."""
    window = _settings_open["window"]
    if window is None or not window.isVisible():
        window = SettingsWindow(dictation, getattr(dictation, "tray", None))
        _settings_open["window"] = window            # rebuilt so it is current
    window.show()
    window.raise_()
    window.activateWindow()


def make_tray(app, dictation):
    tray = QtWidgets.QSystemTrayIcon(app_icon(), app)
    tray.setToolTip("%s  \u00b7  %s" % (APP_TITLE, pretty(HOTKEY)))
    menu = QtWidgets.QMenu()
    keep = {"menu": menu}                       # keep Qt objects alive
    dictation.tray = tray

    def open_settings():
        globals()["open_settings"](dictation)

    def clicked(reason):
        if reason in (QtWidgets.QSystemTrayIcon.Trigger,
                      QtWidgets.QSystemTrayIcon.DoubleClick):
            open_settings()

    menu.addAction("Settings").triggered.connect(lambda _checked=False: open_settings())
    menu.addAction("Quit").triggered.connect(lambda _checked=False: app.quit())
    tray.setContextMenu(menu)
    tray.activated.connect(clicked)
    tray.show()
    return tray, keep


def show_about(dictation):
    folder, megabytes = model_location()
    quant = " (%s)" % QUANTIZATION if QUANTIZATION else ""
    lines = [
        "%s %s" % (APP_TITLE, VERSION),
        "",
        "Local dictation for Windows. Speech is recognised on this PC by",
        "NVIDIA's Parakeet model; leetsay does not save your audio or send it",
        "anywhere.",
        "",
        "Model:  %s%s, release %s" % (model_label(MODEL), quant,
                                      (current_release() or {}).get("tag", "unknown")),
        "Stored in:  %s" % (folder or "not downloaded yet"),
    ]
    if megabytes:
        lines.append("Size on disk:  %d MB" % megabytes)
    if dictation.load_seconds is not None:
        lines.append("Loaded in:  %.1f seconds" % dictation.load_seconds)
    elif dictation.state == "loading":
        lines.append("Still loading…")
    lines += [
        "Network:  %s" % network_words(),
        "",
        "Hotkey:  %s  (%s)" % (pretty(HOTKEY),
                               "hold while speaking" if HOLD_TO_TALK else "press to start and stop"),
        "Tip:  tap the hotkey to paste your last dictation again." if KEEP_LAST else "",
        "Settings file:  %s" % _CONFIG_FILE,
        "Log file:  %s" % os.path.join(_DATA, "dictate.log"),
        "",
        "Speech model: Parakeet TDT 0.6B %s by NVIDIA Corporation, under the "
        "CC-BY-4.0 licence, converted to ONNX and reduced to 8 bits by this "
        "project (github.com/%s). Not reviewed or endorsed by NVIDIA. Helper "
        "for long dictations: Silero VAD by the Silero Team, MIT licence."
        % ("v3" if MODEL.endswith("v3") else "v2", MODELS_REPO),
        "",
        "Free software under the MIT licence, provided as is, without any "
        "warranty. Speech recognition makes mistakes, and can add words that "
        "were never said: check the text before you send it, and do not rely "
        "on it where a wrong word could cause harm. You are responsible for "
        "where the text it types goes. The licences of everything it uses are "
        "in its licenses folder and CREDITS.md; see also the README.",
    ]
    dialog(None, "About %s" % APP_TITLE, "%s %s" % (APP_TITLE, VERSION),
           "\n".join(line for line in lines[2:]),
           on_headline_clicks=lambda: toggle_leet(dictation))


def toggle_leet(dictation):
    """Turn 1337 mode on or off (About's title clicked seven times, or the
    "Exit 1337 mode" button at the top of Settings, shown only while it's on)."""
    apply_leet(not LEET_MODE)
    save_setting("leet_mode", LEET_MODE)
    dictation.overlay.apply_size()
    # The icon is drawn in the accent colour: green while the mode is on.
    if dictation.tray is not None:
        dictation.tray.setIcon(app_icon())
    # The open Settings window takes the new look at once (other windows,
    # with looks of their own, take it when they next open).
    for widget in QtWidgets.QApplication.topLevelWidgets():
        if isinstance(widget, SettingsWindow):
            widget.setStyleSheet(settings_style())
            widget.leet_bar.setVisible(LEET_MODE)
            QtCore.QTimer.singleShot(0, widget._fit_height)
        widget.setWindowIcon(app_icon())
    dictation.overlay.show_message("1337 mode on" if LEET_MODE else "1337 mode off",
                                   ms=2200, pulse=True)


def hide_console():
    """Restart without a console window (pythonw.exe), then close this one."""
    if not HIDE_CONSOLE or os.name != "nt" or _NO_CONSOLE:
        return
    exe = sys.executable or ""
    if os.path.basename(exe).lower() != "python.exe":
        return
    windowless = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if not os.path.exists(windowless):
        return                                   # no pythonw: keep the console
    import subprocess
    subprocess.Popen([windowless, os.path.abspath(__file__)] + sys.argv[1:],
                     creationflags=0x00000008 | 0x00000200,   # detached, own group
                     close_fds=True)
    sys.exit(0)


_instance_mutex = None
_quit_event = None
_MUTEX_NAME = "dictate_py_single_instance"
_QUIT_EVENT_NAME = "dictate_py_quit_request"


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = wintypes.HANDLE
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateEventW.restype = wintypes.HANDLE
    k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                               wintypes.LPCWSTR]
    k.OpenEventW.restype = wintypes.HANDLE
    k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.SetEvent.argtypes = [wintypes.HANDLE]
    k.ResetEvent.argtypes = [wintypes.HANDLE]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


def _claim_instance(k):
    """Try to become the one running copy. True if we are."""
    global _instance_mutex
    ctypes.set_last_error(0)
    handle = k.CreateMutexW(None, False, _MUTEX_NAME)
    if ctypes.get_last_error() == 183 or not handle:
        # ERROR_ALREADY_EXISTS, or no handle at all: a copy started "as
        # administrator" holds a lock this one may not even open. Either way
        # another copy is running; two would type everything twice.
        if handle:
            k.CloseHandle(handle)
        return False
    _instance_mutex = handle
    return True


_PID_FILE = os.path.join(_DATA, "dictate.pid")    # which process is the running copy


def _note_running_copy():
    """Write down which process is the running copy, and which file it runs."""
    try:
        with open(_PID_FILE, "w", encoding="utf-8") as fh:
            fh.write("%d\n%s\n%s" % (os.getpid(),
                                     os.path.basename(_SELF), VERSION))
    except Exception:
        pass


def _command_line_args(pid):
    """(program name, [arguments]) of a running process, or None. The command
    line is split exactly the way Windows itself splits it."""
    import subprocess
    hidden = 0x08000000 if os.name == "nt" else 0
    query = ("$p = Get-CimInstance Win32_Process -Filter 'ProcessId=%d'; "
             "if ($p) { $p.Name; $p.CommandLine }" % int(pid))
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", query],
                             capture_output=True, text=True, timeout=20,
                             creationflags=hidden).stdout
    except Exception as exc:
        print("Could not look at process %d: %s" % (pid, exc), file=sys.stderr)
        return None
    lines = out.splitlines()
    if len(lines) < 2:
        return None
    shell32 = ctypes.WinDLL("shell32")
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    count = ctypes.c_int(0)
    argv = shell32.CommandLineToArgvW(" ".join(lines[1:]).strip(), ctypes.byref(count))
    if not argv:
        return None
    try:
        args = [argv[i] for i in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)
    return lines[0].strip(), args


def _force_close_old_copy():
    """An older copy would not close when asked (it may be stuck, or sitting on
    a question nobody can see). End it, and nothing else.

    Only the one process that copy wrote down for itself (in dictate.pid, when
    it started) is considered, and it is ended only if it is still a Python
    process running a script of exactly that file name. A process that merely
    has "leetsay" somewhere in its command line, in a folder name for
    instance, is never touched."""
    import subprocess
    hidden = 0x08000000 if os.name == "nt" else 0
    notes = [_PID_FILE]
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:      # a copy from before the rename keeps its note in the old folder
        notes.append(os.path.join(base, "Dictate", "dictate.pid"))
    closed = False
    for note in notes:
        try:
            with open(note, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            pid = int(lines[0].strip())
            script = lines[1].strip() if len(lines) > 1 else ""
        except Exception:
            continue
        if pid == os.getpid() or not script:
            continue
        found = _command_line_args(pid)
        if not found:
            print("The copy noted in %s (process %d) is not running." % (note, pid))
            continue
        program, args = found
        is_python = program.lower() in ("python.exe", "pythonw.exe", "py.exe", "pyw.exe")
        runs_it = any(os.path.basename(arg).lower() == script.lower()
                      and os.path.splitext(arg)[1].lower() in (".py", ".pyw")
                      for arg in args[1:])
        # The installed program runs as leetsay.exe itself. Only a process with
        # exactly that name counts, so a damaged note can never name another.
        own_exe = APP_TITLE.lower() + ".exe"
        is_installed = script.lower() == own_exe and program.lower() == own_exe
        if not ((is_python and runs_it) or is_installed):
            print("Process %d is no longer this program (%s); left alone." % (pid, program))
            continue
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                           timeout=10, creationflags=hidden)
            print("Closed an older copy that was not responding (process %d, %s)."
                  % (pid, script))
            closed = True
        except Exception:
            pass
    return closed


RESTART_FLAG = "--restart"                       # set when the app restarts itself
_SHOW_EVENT_NAME = "dictate_py_show_settings"


def _running_version():
    """The version the running copy noted for itself ('' if it did not)."""
    try:
        with open(_PID_FILE, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        return lines[2].strip() if len(lines) > 2 else ""
    except Exception:
        return ""


def ask_running_copy_to_show_settings():
    """Started again while this same version is running (a double-click on the
    icon, say): open the running copy's Settings instead of starting over.
    True if the running copy was reached."""
    if RESTART_FLAG in sys.argv or _running_version() != VERSION:
        return False                              # a restart or a different version
    try:
        k = _kernel32()
        # EVENT_MODIFY_STATE | SYNCHRONIZE: set it, then see whether it is taken.
        event = k.OpenEventW(0x0002 | 0x00100000, False, _SHOW_EVENT_NAME)
        if not event:
            return False
        try:
            try:   # this start came from the user, so it may hand on the right to
                # bring a window to the front: Settings then opens on top.
                with open(_PID_FILE, encoding="utf-8") as fh:
                    ctypes.windll.user32.AllowSetForegroundWindow(int(fh.readline().strip()))
            except Exception:
                pass
            k.SetEvent(event)
            # The running copy clears the signal when it acts on it. If it is
            # still set after 2 s, that copy is not responding: replace it.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if k.WaitForSingleObject(event, 0) != 0:     # cleared: it answered
                    return True
                time.sleep(0.1)
            k.ResetEvent(event)
            print("The running copy did not answer; replacing it.")
            return False
        finally:
            k.CloseHandle(event)
    except Exception:
        return False


def watch_for_show_request(app, dictation):
    """Let a second start of this same version open Settings here."""
    try:
        k = _kernel32()
        # Manual reset: cleared here when acted on, so the new start can tell
        # that this copy answered (see ask_running_copy_to_show_settings).
        event = k.CreateEventW(None, True, False, _SHOW_EVENT_NAME)
    except Exception:
        return None
    if not event:
        return None

    def check():
        if k.WaitForSingleObject(event, 0) == 0:  # signalled
            k.ResetEvent(event)
            print("Started again while running: opening Settings.")
            open_settings(dictation)

    timer = QtCore.QTimer(app)
    timer.setInterval(300)
    timer.timeout.connect(check)
    timer.start()
    timer.event_handle = event                    # kept open with the timer
    return timer


def take_over():
    """Become the running copy. If another one is already running, ask it to
    quit and wait for it to go; if it will not, close it. False only if even
    that failed."""
    try:
        k = _kernel32()
    except Exception:
        return True                               # not Windows: nothing to do
    if _claim_instance(k):
        _note_running_copy()
        return True
    if ask_running_copy_to_show_settings():
        return "shown"                            # the running copy carries on
    event = k.OpenEventW(0x0002, False, _QUIT_EVENT_NAME)     # EVENT_MODIFY_STATE
    if event:
        k.SetEvent(event)
        k.CloseHandle(event)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if _claim_instance(k):
            _note_running_copy()
            return True
    if _force_close_old_copy():                   # it did not listen: end it
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            time.sleep(0.2)
            if _claim_instance(k):
                _note_running_copy()
                return True
    return False


def finish_folder_move():
    """The data folder used to be %LOCALAPPDATA%\\Dictate. If an old copy was
    still running when this one started, the folder could not be moved, only
    copied, and the old one was left behind. That old copy has closed by now
    (this runs after take_over), so tidy up: bring across any speech model kept
    only in the old folder, then delete the old folder."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        return
    former = os.path.join(base, "Dictate")
    if not is_former_folder(former) or \
            os.path.normcase(os.path.abspath(_DATA)) != \
            os.path.normcase(os.path.abspath(os.path.join(base, APP_TITLE))):
        return
    import shutil
    try:
        old_models = os.path.join(former, "models")
        if os.path.isdir(old_models):
            for name in os.listdir(old_models):
                source = os.path.join(old_models, name)
                target = os.path.join(_DATA, "models", name)
                if not os.path.isdir(source):
                    continue
                if not os.path.exists(target):
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.move(source, target)
                elif folder_bytes(source) != folder_bytes(target):
                    # The copy here differs (an interrupted copy, say): keep the
                    # old folder, so no model is lost, and say so.
                    print("The old Dictate folder holds a model that differs from the "
                          "copy here; the old folder was kept:", source, file=sys.stderr)
                    return
        shutil.rmtree(former, ignore_errors=True)
        if os.path.isdir(former):
            print("The old Dictate folder could not be removed yet; trying again "
                  "next start:", former, file=sys.stderr)
        else:
            print("Removed the old Dictate folder (its contents were already moved here).")
    except Exception as exc:
        print("Could not finish moving the old Dictate folder:", exc, file=sys.stderr)


def leave_now(app=None, code=0):
    """End this process for certain, right now.

    An ordinary exit waits for every background task to finish first. A model
    download that is stalled never finishes, so the process used to linger
    out of sight, with no tray icon, and block the next start. Here the tray
    icon is taken down, the log is flushed, and the process ends at once."""
    try:
        if app is not None:
            for widget in app.topLevelWidgets():
                widget.hide()
            tray = getattr(app, "_tray_icon", None)
            if tray is not None:
                tray.hide()
            app.processEvents()
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(code)


def watch_for_quit_request(app):
    """Let a newer copy of this program ask this one to close."""
    global _quit_event
    try:
        k = _kernel32()
        _quit_event = k.CreateEventW(None, True, False, _QUIT_EVENT_NAME)
    except Exception:
        return None
    if not _quit_event:
        return None

    def check():
        if k.WaitForSingleObject(_quit_event, 0) == 0:        # signalled
            # A newer copy is taking over. Close whatever is open (a question on
            # screen included) and make sure this process really ends, even if
            # it is not yet, or no longer, in its main loop.
            print("A newer copy asked this one to close.")
            leave_now(app)

    timer = QtCore.QTimer(app)
    timer.setInterval(400)
    timer.timeout.connect(check)
    timer.start()
    return timer


def main():
    start_fresh_log()                                  # this copy is the running one now
    print("%s %s starting from %s" % (APP_TITLE, VERSION, _SELF))
    finish_folder_move()
    carry_over_startup()
    signal.signal(signal.SIGINT, signal.SIG_DFL)      # let Ctrl+C in the console quit
    try:      # so Windows shows this program's own icon, not Python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_TITLE)
    except Exception:
        pass
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")                             # same look on every PC
    app.setPalette(menu_palette())                     # dark (or light) menus and dialogs
    app.setWindowIcon(app_icon())
    # Listen for a newer copy asking this one to close from the very start, so
    # it works even while a question is on screen (before, this began later,
    # and a copy waiting on a question could not be closed or even seen).
    quit_watch = watch_for_quit_request(app)           # noqa: F841 (kept alive)
    if not network_is_open():
        verify_network_block()                         # proves the guard works, offline
    finish_pending_removal()
    print("Model on this PC at start: %s | helper: %s"
          % ([(folder, folder_megabytes(folder)) for folder in model_folders()] or "none",
             "yes" if helper_on_disk() else "no"))
    # Nothing is downloaded without saying so first. If the speech model is not
    # on this PC yet, explain the one-time download and wait for a yes.
    # (Settings can ask for these first-launch screens to be shown again, as a
    # replay: the same question and welcome card, with nothing downloaded.)
    replay = _config.get("replay_first_run") is True
    if replay:
        save_setting("replay_first_run", False)        # once only
        _config["welcomed"] = False                    # in memory: the welcome card shows
    # A yes given in Settings (when switching models) covers the one restart
    # that follows it, and no more. If the model is still not here on a later
    # start, because the download was interrupted or blocked, ask again:
    # the application never goes online for it without a fresh yes.
    # (Earlier versions kept a lasting yes under "download_ok". That is dropped,
    # unread: it must not stand in for a fresh answer.)
    agreed_in_settings = DOWNLOAD_AGREED_ONCE     # read (and used up) at start
    needed =not (_setup_done() or local_model_dir()) and not agreed_in_settings
    need_ok = _config.get("responsibility_ok") is not True or replay
    intro = None
    answer = None
    if not _config.get("welcomed"):
        # A first launch (or a replay of one): one setup window. Its first
        # pages ("Before you start", then the download question if needed)
        # come before anything else starts; the rest follow once it runs.
        intro = IntroWindow(None, need_ok=need_ok, need_download=needed or replay)
        on_main_screen(intro)
        intro.show()
        answer = intro.run_gate()
        if answer == "quit":
            print("The setup window was closed before the start; closing.")
            leave_now(app)
            return
        need_ok = False                          # held on its page
    if need_ok:
        # Once, before first use: what the application does with your words,
        # and that where they go is up to you. Not understood (Quit, or the
        # window closed): the application closes, as with the download question.
        if not responsibility_notice():
            print("The 'Before you start' notice was not confirmed; closing.")
            leave_now(app)
            return
        save_setting("responsibility_ok", True)
    if needed or replay:
        if intro is not None:                    # answered on its page
            agreed = {"download": True, "manual": "other"}.get(answer, False)
        else:
            agreed = dialog(
                None, "%s: one-time download" % APP_TITLE, "One-time download needed",
                download_question_words(),
                accept="Download", reject="Quit", other="Manual download")
        if agreed == "other":
            # Not through this program: show how to fetch the files by hand.
            manual_download_steps()
            if needed and not local_model_dir():
                print("Manual download chosen; nothing was downloaded by the program.")
                # The files are not in place yet. Come in anyway, or leave it there?
                if dialog(None, "%s: no speech model yet" % APP_TITLE,
                          "Continue without the speech model?",
                          "You can look around and change settings, but dictation will "
                          "not work until the model's files are in place.\n\n%s looks "
                          "for them each time it starts. You can also download the model "
                          "later from Settings, on the General tab."
                          % APP_TITLE, accept="Continue without it", reject="Quit") is not True:
                    sys.exit(0)
                globals()["START_WITHOUT_MODEL"] = True
            agreed = True                        # carry on (in a replay too)
        if not agreed:
            # Quit, or the X: the application closes, and nothing is downloaded.
            # (The same in a replay of the first launch, so that it behaves
            # exactly as a real one would.)
            print("The download question was declined; closing.")
            leave_now(app)
            return
        if needed and not START_WITHOUT_MODEL and not local_model_dir():
            open_network("first download, agreed just now")
    dictation = Dictation()                            # noqa: F841 (kept alive)
    show_watch = watch_for_show_request(app, dictation)  # noqa: F841 (kept alive)
    # The "Start with Windows" entry is written only when the user ticks it (or
    # chooses it in the installer), never on its own at start-up: a program
    # that quietly adds itself to start-up is what malware does, and Windows'
    # protection rightly stops it. (If this copy moved, the box in Settings
    # shows unticked, and ticking it again points the entry here.)
    tray = make_tray(app, dictation) if SHOW_TRAY_ICON else None
    dictation.overlay.settings_wanted.connect(lambda: open_settings(dictation))
    if tray is not None:
        dictation.tray = tray[0]
        app._tray_icon = tray[0]                       # so leaving can take it down
        app.aboutToQuit.connect(tray[0].hide)          # no ghost icon left behind
    if CHECK_APP_UPDATES:
        # Switched on (after its "This application will go online" window): ask
        # github.com, in a separate helper, a little after start-up so that it
        # never slows the start. Says something only if there is a new version.
        QtCore.QTimer.singleShot(10000, lambda: run_app_update_check(None, manual=False))
    if intro is not None:                              # first launch: on with the setup
        intro.attach(dictation)
    code = app.exec()                                  # runs until Quit
    leave_now(app, code)                               # and then really ends


if __name__ == "__main__":
    try:
        hide_console()
        taken = take_over()
        if taken == "shown":
            sys.exit(0)                           # Settings opened in the running copy
        if not taken:
            _message_box("An older copy of " + APP_TITLE + " is still running and could not be "
                         "closed.\n\nOpen Task Manager (Ctrl+Shift+Esc), find "
                         "\"Python\" or \"pythonw.exe\" in the list, choose End task, "
                         "then start this again.", error=False)
            sys.exit(0)
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        _pause_and_exit(APP_TITLE + " crashed:\n\n" + traceback.format_exc())
