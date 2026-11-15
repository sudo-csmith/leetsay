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
from huggingface.co. While it runs, the program blocks its own network access;
it opens it only for a download the user has just agreed to.

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

if sys.argv[1:2] == ["--fetch-fingerprints"] and len(sys.argv) >= 4:
    # Not the application: a short-lived helper, started only by "Check now"
    # after its "This application will go online" window was agreed to. It
    # reads the model's public file list from huggingface.co, writes it to the
    # file it was given, and exits. It runs before anything else is loaded.
    import json
    import urllib.request
    _out = {}
    try:
        _request = urllib.request.Request(
            "https://huggingface.co/api/models/%s/tree/main" % sys.argv[2],
            headers={"User-Agent": "model-check"})
        with urllib.request.urlopen(_request, timeout=25) as _reply:
            for _entry in json.load(_reply):
                _out[_entry.get("path", "")] = (_entry.get("lfs") or {}).get("oid", "")
        _result = {"ok": True, "files": _out}
    except Exception as _exc:
        _result = {"ok": False, "error": str(_exc)}
    with open(sys.argv[3], "w", encoding="utf-8") as _fh:
        json.dump(_result, _fh)
    os._exit(0)

import ctypes  # noqa: E402  (after the path is made safe)
import json
import math
import os
import signal
import struct
import sys
import threading
import time
import wave
from collections import deque
from ctypes import wintypes



VERSION = "0.58.4"              # shown in Settings and About
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
    sys.stdout = sys.stderr = _log
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
# If this folder exists, the model is loaded from it and nothing is downloaded.
MODEL_DIR = os.path.join(_HERE, "parakeet-model")
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
ALWAYS_SHOW = False                  # keep a small pill on screen between dictations
ACCENT_OUTLINE = False               # a ring in the accent colour round the pop-up
OUTLINE_LIGHT = True                 # a band of light that travels round that ring
GLOW = "medium"                      # glow round the pop-up: off, subtle, medium, strong
GLOW_FROM_APP = True                 # tint the glow with the colour of the app's icon
GLOW_COLOUR = ""                     # your own glow colour ("" = the accent colour);
                                     # used when the glow doesn't follow the app's icon
SCREEN_GLOW = True                   # also glow round the edge of the whole screen
SCREEN_GLOW_STRENGTH = 60            # how bright that glow is, 5-100
SCREEN_GLOW_SIZE = 56                # how far it reaches in from the edge, 20-200 pixels
TRAY_LIVE = False                    # the tray icon's bars move with your voice
SHOW_HOTKEY_HINT = True              # show the hotkey on that pill and at startup
LIVE_LINES = 1                       # lines of live words in the pop-up (1-6)
LIVE_ALIGN = "left"                  # rows of live words: "left" (words never move
                                     # once shown) or "center" (rows re-centre)
CARD_WIDTH = 0                       # your own pop-up width, set by dragging its left
                                     # or right edge (0 = the usual Narrow / Wide)
WIDE_CARD = False                    # a wider pop-up when several lines are shown
LIVE_PREVIEW = True                  # show the words in the pop-up as you speak
LIVE_EVERY_MS = 250                  # the preview refreshes this often, or as fast as
                                     # the PC can manage if a pass takes longer
TEXT_SIZE = 12                       # size of the live words in the pop-up
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
BOTTOM_MARGIN = 28                   # gap between the pop-up and the taskbar

SAMPLE_RATE = 16000
MIC_SILENT_PEAK = 0.0008             # below this the microphone is giving no signal
MIC_QUIET_PEAK = 0.01                # below this it can barely hear anything
N_DOTS = 46                          # how much level history is kept
ACCENT = "#3DDC97"                   # pop-up colour (any hex colour)
POPUP_BG = "#16171B"                 # pop-up background; text adapts to light or dark
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
    ("nemo-parakeet-tdt-0.6b-v3", "25 European languages (Parakeet v3)", "int8", "about 680 MB"),
]
if _config.get("model") in [entry[0] for entry in MODELS]:
    MODEL = _config["model"]
    QUANTIZATION = dict((entry[0], entry[2]) for entry in MODELS)[MODEL]

# Where each model's files can be fetched by hand (the same files the automatic
# download gets), and the files that are needed.
MODEL_PAGES = {
    "nemo-parakeet-tdt-0.6b-v2": "https://huggingface.co/istupakov/parakeet-tdt-0.6b-v2-onnx/tree/main",
    "nemo-parakeet-tdt-0.6b-v3": "https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx/tree/main",
}
# (The model's page also lists nemo128.onnx. It is not needed: the speech
# library brings its own copy of that small helper, and the automatic download
# does not fetch it either.)
MODEL_FILES = ["config.json", "vocab.txt",
               "encoder-model.int8.onnx", "decoder_joint-model.int8.onnx"]


def manual_model_dir(name=None):
    """The folder to put a model's files in when downloading them by hand."""
    return os.path.join(_DATA, "models", name or MODEL)


def local_model_dir():
    """A folder of model files put there by hand, or '' if there is none.
    When there is one the model is loaded from it and nothing is downloaded."""
    folder = manual_model_dir()
    if all(os.path.isfile(os.path.join(folder, name)) for name in MODEL_FILES):
        return folder
    # The older place, next to the program: only if every file is really there
    # (an empty or half-filled folder must not stop the download being offered).
    if all(os.path.isfile(os.path.join(MODEL_DIR, name)) for name in MODEL_FILES):
        return MODEL_DIR
    return ""


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


# Off by default: when on, the next start goes online to ask Hugging Face
# whether the model files have changed, and downloads them if so.
CHECK_UPDATES = _config.get("check_updates") is True


def set_check_updates(on):
    global CHECK_UPDATES
    CHECK_UPDATES = bool(on)
    save_setting("check_updates", CHECK_UPDATES)


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
# Model file fingerprints: a changed, damaged or swapped file is caught
# ----------------------------------------------------------------------------
# The published SHA-256 fingerprint of each model file, per model. Left empty,
# the fingerprints are recorded the first time the files are used and every
# later start is checked against that record. Filled in (for a release), the
# files must match these exactly, including straight after downloading.
MODEL_CHECKSUMS = {
    # istupakov/parakeet-tdt-0.6b-v2-onnx, revision 0bbb45a3. The two .onnx
    # values are the SHA-256 that huggingface.co publishes for those files (their
    # LFS ids); the two small files were hashed from that same download.
    "nemo-parakeet-tdt-0.6b-v2": {
        "config.json": "666903c76b9798caf2c210afd4f6cd60b08a8dbf9800ec8d7a3bc0d2148ac466",
        "vocab.txt": "ec182b70dd42113aff6c5372c75cac58c952443eb22322f57bbd7f53977d497d",
        "encoder-model.int8.onnx":
            "3e0581fda6ab843888b51e56d7ee78b6d5bc3237ec113af1f732d1d5286aa155",
        "decoder_joint-model.int8.onnx":
            "a449f49acd68979d418651dd2dcb737cc0f1bf0225e009e29ee326354edbf7d3",
    },
    # nemo-parakeet-tdt-0.6b-v3: not filled in yet (fingerprints are recorded
    # the first time its files are used, as before).
}


_SILERO_REPO = "istupakov/silero-vad-onnx"     # the pause-detection helper


def model_repo(name=None):
    """The Hugging Face repository a model is downloaded from, e.g.
    "istupakov/parakeet-tdt-0.6b-v2-onnx" ('' if unknown)."""
    page = MODEL_PAGES.get(name or MODEL, "")
    if "huggingface.co/" not in page:
        return ""
    return page.split("huggingface.co/", 1)[1].split("/tree/")[0]


def cache_folder_for(repo):
    """The download cache's folder for exactly this repository. (Matching on a
    part of the name would also catch other programs' models, such as the
    original NVIDIA one, and Remove would delete them.)"""
    if not repo:
        return ""
    hub = os.path.dirname(model_location_root())
    return os.path.join(hub, "models--" + repo.replace("/", "--"))


def current_snapshot(root):
    """The version of the files the speech library loads (refs/main), not an
    older one still lying in the cache after an update."""
    try:
        with open(os.path.join(root, "refs", "main"), encoding="utf-8") as fh:
            ref = fh.read().strip()
        folder = os.path.join(root, "snapshots", ref)
        return folder if ref and os.path.isdir(folder) else ""
    except Exception:
        return ""


def model_files():
    """{file name: full path} of the model's files on this PC, wherever they
    are: the by-hand folder, or the cache the automatic download fills."""
    found = {}
    folder = local_model_dir()
    if folder:
        for name in MODEL_FILES:
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                found[name] = path
        return found
    root, _size = model_location()
    snapshot = current_snapshot(root) if root else ""
    if snapshot:
        for name in MODEL_FILES:
            path = os.path.join(snapshot, name)
            if os.path.isfile(path):
                found[name] = path
    return found


def file_fingerprint(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def uses_own_files(name=None):
    """True if the user knowingly accepted model files that differ from the
    built-in fingerprints, for this model."""
    accepted = _config.get("own_model_files")
    return isinstance(accepted, list) and (name or MODEL) in accepted


def set_uses_own_files(on, name=None):
    name = name or MODEL
    accepted = _config.get("own_model_files")
    accepted = [m for m in accepted if isinstance(m, str)] if isinstance(accepted, list) else []
    if on and name not in accepted:
        accepted.append(name)
    elif not on and name in accepted:
        accepted.remove(name)
    save_setting("own_model_files", accepted)


def check_model_files():
    """Check the model's files against their fingerprints.

    Returns (changed, record): the names of files that do not match what is
    expected, and a fresh record {name: [size, modified time, fingerprint]}.
    A file whose size and time are as recorded is taken as unchanged without
    being read again, so the check costs nothing on an ordinary start."""
    saved = _config.get("model_files")
    known = saved.get(MODEL, {}) if isinstance(saved, dict) else {}
    fetched = _config.get("published_fingerprints")
    # The built-in fingerprints apply unless the user knowingly took other files
    # for this model (an agreed update, or "Use them anyway"): then the record
    # of the files they accepted is what every later start checks against.
    pinned = {} if uses_own_files() else (MODEL_CHECKSUMS.get(MODEL) or {})
    published = pinned or (fetched.get(MODEL, {}) if isinstance(fetched, dict) else {})
    changed, record = [], {}
    for name, path in sorted(model_files().items()):
        try:
            info = os.stat(path)
            old = known.get(name)
            same = (isinstance(old, list) and len(old) == 3 and old[0] == info.st_size
                    and abs(float(old[1]) - info.st_mtime) < 2.0)
            mark = old[2] if same else file_fingerprint(path)
            expected = published.get(name) or (old[2] if isinstance(old, list)
                                               and len(old) == 3 else None)
            if expected and mark.lower() != str(expected).lower():
                changed.append(name)
            record[name] = [info.st_size, info.st_mtime, mark]
        except Exception as exc:
            print("Could not check %s: %s" % (name, exc), file=sys.stderr)
    return changed, record


_FETCH_SCRIPT = r"""
import json, sys, urllib.request
out = {}
try:
    request = urllib.request.Request(
        "https://huggingface.co/api/models/%s/tree/main" % sys.argv[1],
        headers={"User-Agent": "leetsay-model-check"})
    with urllib.request.urlopen(request, timeout=25) as reply:
        for entry in json.load(reply):
            out[entry.get("path", "")] = (entry.get("lfs") or {}).get("oid", "")
    result = {"ok": True, "files": out}
except Exception as exc:
    result = {"ok": False, "error": str(exc)}
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(result, fh)
"""


def fetch_published_fingerprints():
    """Ask the model's own website for the published fingerprint of each of its
    files. Returns ({file name: sha256 or ''}, None) or (None, why it failed).

    This is the one thing here that goes online, and only when the user asks.
    It runs in a separate short-lived process, so this program's own block on
    network access stays exactly as it is. Nothing is sent but the request for
    the public file list."""
    import subprocess
    import tempfile
    page = MODEL_PAGES.get(MODEL, "")
    if "huggingface.co/" not in page:
        return None, "no page is known for this model"
    repo = page.split("huggingface.co/", 1)[1].split("/tree/")[0]
    handle, out_path = tempfile.mkstemp(suffix=".json")
    os.close(handle)
    try:
        helper = ([sys.executable, "--fetch-fingerprints", repo, out_path] if FROZEN
                  else [sys.executable, "-I", "-c", _FETCH_SCRIPT, repo, out_path])
        subprocess.run(helper,
                       timeout=40, creationflags=0x08000000 if os.name == "nt" else 0)
        with open(out_path, encoding="utf-8") as fh:
            result = json.load(fh)
    except Exception as exc:
        return None, str(exc)
    finally:
        try:
            os.remove(out_path)
        except Exception:
            pass
    if not result.get("ok"):
        return None, result.get("error") or "the website could not be reached"
    return result.get("files") or {}, None


def model_total_bytes():
    """Roughly how big the chosen model's download is (from the size shown to
    the user, e.g. "about 660 MB")."""
    words = dict((entry[0], entry[3]) for entry in MODELS).get(MODEL, "")
    digits = "".join(ch for ch in words if ch.isdigit())
    return int(digits) * 1024 * 1024 if digits else 0


def model_bytes_on_disk():
    """How much of the model is on this PC so far, part-finished files included."""
    folder = model_location()[0]
    return folder_bytes(folder) if folder else 0


def folder_bytes(folder):
    """The real size of a model folder. Shortcuts are followed to the file they
    point to (which may be in the download library's shared store), and each
    real file is counted once, however many shortcuts lead to it."""
    seen, total = set(), 0
    for where, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(where, name)
            try:
                real = os.path.normcase(os.path.realpath(path))
                if real not in seen:
                    seen.add(real)
                    total += os.path.getsize(path)
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


def model_location_root():
    """The folder the automatic download keeps models in (one sub-folder each)."""
    cache = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache", "huggingface"), "hub")
    return os.path.join(cache, "x")              # dirname() of this is the hub folder


def finish_pending_removal():
    """Delete a model folder that could not be fully removed while the program
    was running. Only folders in the places models are kept are ever touched."""
    pending = _config.get("delete_at_start")
    if not pending:
        return
    save_setting("delete_at_start", "")
    # One folder (as earlier versions saved it) or a list of folders and files.
    for item in (pending if isinstance(pending, list) else [pending]):
        try:
            import shutil
            wanted = os.path.normcase(os.path.abspath(str(item)))
            allowed = [os.path.normcase(os.path.abspath(os.path.dirname(model_location_root()))),
                       os.path.normcase(os.path.abspath(os.path.join(_DATA, "models")))]
            if not any(wanted.startswith(place + os.sep) for place in allowed):
                continue
            if os.path.isdir(wanted):
                shutil.rmtree(wanted, ignore_errors=True)
            elif os.path.isfile(wanted):
                os.remove(wanted)
                try:
                    os.remove(wanted + ".refs")
                except FileNotFoundError:
                    pass
        except Exception as exc:
            print("Could not finish removing the model:", exc, file=sys.stderr)


def folder_megabytes(folder):
    return int(folder_bytes(folder) / (1024 * 1024))


def _shared_files_of(folder):
    """The files in the download library's shared store ("hub\\blobs") that
    shortcuts inside this model folder point to."""
    hub = os.path.normcase(os.path.abspath(os.path.dirname(model_location_root())))
    store = os.path.join(hub, "blobs") + os.sep
    found = set()
    for where, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(where, name)
            try:
                if os.path.islink(path):
                    real = os.path.normcase(os.path.realpath(path))
                    if real.startswith(store) and os.path.isfile(real):
                        found.add(real)
            except Exception:
                pass
    return found


def _still_used_elsewhere(shared):
    """True if any other model in the cache still has a shortcut to this shared
    file. The store lists who uses each file in "<file>.refs"; every entry is
    checked on disk. If that list can't be read, the file counts as in use:
    better to leave it than to delete something another program needs."""
    hub = os.path.dirname(os.path.dirname(os.path.dirname(shared)))
    try:
        with open(shared + ".refs", encoding="utf-8") as fh:
            entries = [line.strip() for line in fh.read().splitlines() if line.strip()]
    except Exception:
        return True
    for entry in entries:
        user = os.path.join(hub, *entry.split("/"))
        try:
            if os.path.islink(user) and \
                    os.path.normcase(os.path.realpath(user)) == os.path.normcase(shared):
                return True
        except Exception:
            return True
    return False


def _remove_model_folder(folder, report):
    """Delete one model folder and the shared-store files only it was using."""
    import shutil
    shared = _shared_files_of(folder)
    size = folder_megabytes(folder)
    shutil.rmtree(folder, ignore_errors=True)
    left = [folder] if os.path.isdir(folder) else []
    kept = 0
    for path in shared:
        if _still_used_elsewhere(path):
            kept += os.path.getsize(path)
            continue
        try:
            os.remove(path)
            try:
                os.remove(path + ".refs")
            except FileNotFoundError:
                pass
        except FileNotFoundError:
            pass
        except Exception:
            left.append(path)                # still open: removed at the next start
    remaining = kept
    for item in left:
        try:
            remaining += folder_bytes(item) if os.path.isdir(item) else os.path.getsize(item)
        except Exception:
            pass
    report["freed"] += max(0, size - int(remaining / (1024 * 1024)))
    if left:
        report["left"].extend(left)
    else:
        report["removed"].append(folder)
    return left


def model_folders():
    """Every folder on this PC that holds this model: the by-hand folder, and
    any folder for it in the automatic download's cache."""
    found = []
    by_hand = manual_model_dir()
    if os.path.isdir(by_hand):
        found.append(by_hand)
    if local_model_dir() == MODEL_DIR:           # the older place, next to the program
        found.append(MODEL_DIR)
    cached = cache_folder_for(model_repo())
    if cached and os.path.isdir(cached):
        found.append(cached)
    return found


def download_cache_folder():
    """Where the download library keeps its own extra cache of downloaded
    pieces (shared by any program on this PC that uses the same library)."""
    return os.environ.get("HF_XET_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache", "huggingface"), "xet")


def forget_model():
    """Delete this model from this PC, everywhere it is kept, and every note
    about it, so the next start downloads it afresh (after asking, as on a
    first launch). Returns a short account of what was done."""
    import shutil
    report = {"removed": [], "left": [], "freed": 0, "cache_freed": 0}
    for folder in model_folders():
        _remove_model_folder(folder, report)
    # (The download library's "xet" cache is not touched: it is shared with any
    # other program that uses the library, and this application never uses it.)
    have = [m for m in downloaded_models() if m != _marker_text()]
    try:
        with open(_MARKER, "w", encoding="utf-8") as fh:
            fh.write("\n".join(have))
    except Exception:
        pass
    # The small pause-detection helper fetched alongside it goes too.
    try:
        helper = cache_folder_for(_SILERO_REPO)
        if os.path.isdir(helper):
            _remove_model_folder(helper, {"removed": [], "left": report["left"], "freed": 0})
    except Exception:
        pass
    if report["left"]:
        # Windows would not let go of a file the running program still has
        # open. Finish the job at the next start, before anything is loaded.
        save_setting("delete_at_start", list(report["left"]))
    if uses_own_files():
        set_uses_own_files(False)                 # a fresh download is checked as new
    for key in ("model_files", "published_fingerprints"):
        saved = _config.get(key)
        if isinstance(saved, dict) and MODEL in saved:
            saved = dict(saved)
            saved.pop(MODEL, None)
            save_setting(key, saved)
    if _config.get("download_yes_once") or _config.get("download_ok"):
        save_setting("download_ok", "")
        save_setting("download_yes_once", "")
    print("Model removed: %s" % report)
    return report


def remember_model_files(record):
    saved = _config.get("model_files")
    saved = dict(saved) if isinstance(saved, dict) else {}
    saved[MODEL] = record
    save_setting("model_files", saved)


# ----------------------------------------------------------------------------
# Where the model lives on disk (for the About box)
# ----------------------------------------------------------------------------
def model_location():
    """Return (folder, size in MB) of the downloaded model, or (None, 0)."""
    folder = None
    if local_model_dir():
        folder = local_model_dir()
    else:
        cached = cache_folder_for(model_repo())
        if cached and os.path.isdir(cached):
            folder = cached
    if folder is None:
        return None, 0
    return folder, folder_megabytes(folder)


# ----------------------------------------------------------------------------
# Privacy: once the model has been downloaded, cut this program off the network
# ----------------------------------------------------------------------------
_MARKER = os.path.join(_DATA, "dictate.ready")   # written after the first good start


def _marker_text(model=None, quantization=None):
    return "%s|%s" % (MODEL if model is None else model,
                      QUANTIZATION if model is None else quantization)


def downloaded_models():
    """The models this PC already has, one "name|quantization" per line."""
    try:
        with open(_MARKER, encoding="utf-8") as fh:
            return [line.strip() for line in fh.read().splitlines() if line.strip()]
    except Exception:
        return []


def is_downloaded(name):
    quantization = dict((entry[0], entry[2]) for entry in MODELS).get(name, QUANTIZATION)
    return _marker_text(name, quantization) in downloaded_models()


def _setup_done():
    return _marker_text() in downloaded_models()


def mark_downloaded():
    have = downloaded_models()
    if _marker_text() not in have:
        have.append(_marker_text())
        with open(_MARKER, "w", encoding="utf-8") as fh:
            fh.write("\n".join(have))


# The network guard. It is installed when the program starts and cannot be
# removed for the life of the process: Python reports every network action
# (looking up a name, connecting, sending) to it first, from whatever library
# or module it comes, and the guard refuses it. Only after the user has agreed,
# in a window that says "This application will go online", is it opened for
# this run, and then only towards huggingface.co. It closes again as soon as
# the download or check is done.
_NET_MESSAGE = APP_TITLE + ": network access is blocked (offline)"
_NET_ALLOWED_DOMAINS = ("huggingface.co", "hf.co")
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
    if any(host == domain or host.endswith("." + domain) for domain in _NET_ALLOWED_DOMAINS):
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
                print("Network: refused a connection to %r (not huggingface.co)"
                      % (_address_of(target),), file=sys.stderr)
                raise NetworkBlocked(_NET_MESSAGE)
            return
        if event in ("socket.getaddrinfo", "socket.gethostbyname"):
            host = args[0] if args else None
            if not _host_allowed(host):
                print("Network: refused a lookup of %r (only huggingface.co is allowed)"
                      % (host,), file=sys.stderr)
                raise NetworkBlocked(_NET_MESSAGE)
            if host not in _net["hosts"]:
                _net["hosts"].add(host)
                print("Network: connecting to %s (agreed: %s)" % (host, _net["why"]))
    elif event == "import":
        # The download library's native file engine does its own networking,
        # out of the guard's sight. It is never used: every download goes
        # through Python, where the guard sees it.
        name = str(args[0]) if args else ""
        if name == "hf_xet" or name.startswith("hf_xet."):
            raise ImportError("hf_xet is not used by " + APP_TITLE)
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
    """Allow network access towards huggingface.co for this run. Only ever
    called right after the user agreed to go online (or for a start that a
    "go online" window in Settings covered)."""
    os.environ["HF_HUB_OFFLINE"] = "0"
    _net["why"] = why
    _net["open"] = True
    print("Network: opened for this run (%s)." % why)


def close_network(reason="done"):
    if _net["open"]:
        _net["open"] = False
        _net["ips"].clear()
        os.environ["HF_HUB_OFFLINE"] = "1"
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


def network_words():
    """How the network stands, in words for Settings and About."""
    if _net["open"]:
        return "open for this run only, to huggingface.co (%s)" % _net["why"]
    if _net["verified"]:
        return "blocked for this program (offline, checked at start)"
    if _net["verified"] is False:
        return "blocked, but the check at start did not pass (see dictate.log)"
    return "blocked for this program (offline)"


# Ask the libraries not to phone home, in any mode.
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
# The download library can keep a second, hidden copy of what it downloads (a
# "chunk cache") so that a repeat download is quick. That copy would survive
# removing the model, so it is switched off: one copy only, in one known place.
os.environ["HF_XET_CHUNK_CACHE_SIZE_BYTES"] = "0"
# Newer versions of the download library also keep files in a store shared by
# every model ("hub\blobs"), leaving only shortcuts in the model's own folder.
# Removing the model folder would then free nothing, and a later "download"
# would quietly re-link the old files. Switched off: the files stay in the
# model's own folder. (Files already in that store are cleared on removal.)
os.environ["HF_HUB_DISABLE_SHARED_BLOBS"] = "1"
os.environ["DO_NOT_TRACK"] = "1"
# Every download goes through Python, where the network guard sees it.
os.environ["HF_HUB_DISABLE_XET"] = "1"

START_WITHOUT_MODEL = False              # set at start-up when the user chooses to
                                         # come in without a speech model for now

# "Check now" in Settings found a newer model and the user asked for it: this
# one start goes online to fetch it, then everything is offline again.
UPDATE_ONCE = _config.get("update_once") is True
# A download agreed to in Settings (switching to a model not on this PC yet, or
# another try at a stalled download) covers this one start. Both one-shot yeses
# are used up the moment they are read, so a start that fails later on cannot
# leave them behind for another start to use without asking.
DOWNLOAD_AGREED_ONCE = _config.get("download_yes_once") == MODEL
if UPDATE_ONCE:
    save_setting("update_once", False)
if _config.get("download_yes_once") or _config.get("download_ok"):
    save_setting("download_yes_once", "")
    save_setting("download_ok", "")             # an older, lasting yes: dropped unread
UPDATING = CHECK_UPDATES or UPDATE_ONCE          # the model's files may change this run
# Offline unless this start was agreed to in a "go online" window: the update
# check at startup (switched on with a warning), a model update or a model
# switch chosen in Settings. The first download asks later, in main().
os.environ["HF_HUB_OFFLINE"] = "1"           # use the files on disk, never check online
if UPDATE_ONCE:
    open_network("model update chosen in Settings")
elif CHECK_UPDATES:
    open_network("update check at startup, switched on in Settings")
elif DOWNLOAD_AGREED_ONCE:
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
    NOTE_W, NOTE_H = 120, 40       # the smaller pill used for short notices (min width)
    GLOW = 36          # transparent room around the card for its glow and shadow

    def __init__(self, recorder):
        super().__init__(None,
                         Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus)   # clickable, never takes focus
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.preview_words = None  # third-row text while previewing
        self.warning = ""          # microphone problem shown where the words go
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
        self.rows = 1.0
        self._present()

    def _mic_warning(self):
        """Say so if the microphone is giving nothing a couple of seconds in."""
        elapsed = time.monotonic() - self.started
        peak = getattr(self.recorder, "peak", 1.0)
        if elapsed > 1.5 and peak < MIC_SILENT_PEAK:
            return "No sound from the microphone"
        if elapsed > 5.0 and peak < MIC_QUIET_PEAK and not self.live_text:
            return "Can't hear you. Check the mic"
        return ""

    # -- size ------------------------------------------------------------------
    @property
    def live_h(self):
        """Height set aside for the live words (grows with the line count)."""
        return 8 + self.LINE_H * LIVE_LINES

    def apply_size(self):
        """Size the window for the current settings, keeping its base in place."""
        anchor = self._anchor() if self.isVisible() else None
        self.resize(self.WIN_W + 2 * self.GLOW,
                    max(self.CARD_H + self.live_h, self.WELCOME_H) + 2 * self.GLOW)
        if anchor is not None:
            self.move(self._top_left(anchor))
        self.update()

    def _anchor(self):
        """The card's bottom-centre point: stays put when the size changes."""
        return QtCore.QPoint(self.pos().x() + self.width() // 2,
                             self.pos().y() + self.height() - self.GLOW)

    def _top_left(self, anchor):
        return QtCore.QPoint(anchor.x() - self.width() // 2,
                             anchor.y() - self.height() + self.GLOW)

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
            if SHOW_HOTKEY_HINT:
                return pretty(HOTKEY)
            return "Ready"
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
        font = QtGui.QFont("Segoe UI")
        font.setPixelSize(TEXT_SIZE)
        return QtGui.QFontMetricsF(font).horizontalAdvance

    def _fit_width(self, base):
        """The card's width once the app's name is on it: wide enough to show
        the name in full with a decent length of wave beside it."""
        if not self.app_name:
            return base
        font = QtGui.QFont("Segoe UI")
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
            # The card itself must be on a screen, not merely the (mostly
            # see-through) window around it: test a patch of the card just above
            # its bottom-centre point. A spot on a monitor that has since been
            # unplugged or moved falls back to the usual place.
            card = QtCore.QRect(self._pos.x() - 30, self._pos.y() - 26, 60, 20)
            if any(screen.availableGeometry().contains(card)
                   for screen in QtGui.QGuiApplication.screens()):
                return self._pos
        screen = QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos()) \
            or QtGui.QGuiApplication.primaryScreen()
        area = screen.availableGeometry()     # excludes the taskbar
        return QtCore.QPoint(area.x() + area.width() // 2,
                             area.y() + area.height() - BOTTOM_MARGIN)

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
            self.move(here - self._offset)
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
        # Fade in quickly, fade out a little slower.
        speed = 0.19 if self.fade_target > self.fade else 0.16
        self.fade += (self.fade_target - self.fade) * speed
        if self.fade_target == 0.0 and self.fade < 0.03 and self._press is None:
            self.fade = 0.0
            self.hover = False
            self.timer.stop()
            self.hide()
            return
        if self.mode == "idle" and abs(self.fade - self.fade_target) < 0.02:
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
        """Draw the pop-up, faded as one piece. While fading in or out, the
        whole picture is drawn at full strength off-screen and then shown at
        the right transparency. (Fading each layer separately lets the shadow
        and glow show through the card, which turns it a muddy grey.)"""
        fade = max(0.0, min(1.0, self.fade))
        if fade >= 0.995:
            p = QtGui.QPainter(self)
            try:
                self._draw(p)
            finally:
                p.end()
            return
        ratio = self.devicePixelRatioF()
        picture = QtGui.QPixmap(max(1, int(round(self.width() * ratio))),
                                max(1, int(round(self.height() * ratio))))
        picture.setDevicePixelRatio(ratio)
        picture.fill(Qt.transparent)
        p = QtGui.QPainter(picture)
        try:
            self._draw(p)
        finally:
            p.end()
        p = QtGui.QPainter(self)
        try:
            p.setOpacity(fade)
            p.drawPixmap(0, 0, picture)
        finally:
            p.end()

    def _download_lines(self, note):
        """The download card's two lines of text, and whether it is stalled."""
        title, _dot, detail = note.partition("  \u00b7  ")
        stuck = "not moving" in note
        if title.startswith("Downloading"):
            title = "Downloading"                # the icon and the amount say the rest
        percent = "%d%%" % int(max(0.0, min(1.0, self.progress or 0.0)) * 100)
        if stuck:
            words = "Click for help  \u00b7  right-click to retry"
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
        font.setPixelSize(11)
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
        font.setPixelSize(11)
        p.setFont(font)
        p.setPen(theme["warning"] if stuck else theme["muted"])
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
        base = self.height() - g                 # every card sits on this line
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
            note_font = QtGui.QFont("Segoe UI")
            note_font.setPixelSize(12)
            wanted = QtGui.QFontMetricsF(note_font).horizontalAdvance(note) + 58.0
            width = max(80.0, min(380.0, wanted))                # hugs short text
            height, radius = float(self.NOTE_H), self.NOTE_H / 2.0
            if self.progress is not None and self.mode == "message":
                # The download card: just wide enough for its text, with the
                # same margin on every side.
                bold = QtGui.QFont("Segoe UI")
                bold.setPixelSize(12)
                bold.setWeight(QtGui.QFont.DemiBold)
                small = QtGui.QFont("Segoe UI")
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
            # What sits on the top row, left to right: the app's icon (or a
            # dot), its name if wanted, the wave, the timer.
            one_row = not third                  # no words under it: a pill
            hug = one_row and not SHOW_WAVEFORM  # nothing stretchy: fit the contents
            top_font = QtGui.QFont("Segoe UI")
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
        card = QtCore.QRectF(g + (self.WIN_W - width) / 2.0, base - height, width, height)
        self._card = (card.left(), card.top(), width, height)
        cy = card.center().y()
        cx = card.center().x()
        accent = QtGui.QColor(ACCENT)
        theme = popup_theme()
        if is_light(POPUP_BG):
            accent = accent_on_light(accent)     # keeps the bars vivid on a pale card

        # Soft drop shadow under the card: lighter and tighter under the small
        # pill, where a full shadow reads as a heavy dark ring.
        shadow = QtGui.QColor(theme["shadow"])
        spread = 9
        if note is not None:
            shadow.setAlpha(int(shadow.alpha() * 0.45))
            spread = 5
        p.setPen(Qt.NoPen)
        p.setBrush(shadow)
        for i in range(spread, 0, -1):
            p.drawRoundedRect(card.adjusted(-i, 2 - i, i, 2 + i), radius + i, radius + i)

        # A single pulse of the accent colour, used once when the app says
        # "Ready": it swells out from the pill and fades as it goes.
        if self.pulse_started is not None and self.mode == "message":
            t = (time.monotonic() - self.pulse_started) / self.PULSE_SECONDS
            if t >= 1.0:
                self.pulse_started = None
            else:
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
        amount = glow_amount()

        # The glow belongs to dictating: it is there while you are talking to
        # the app (and in previews from Settings), never on the idle pill, the
        # notices or the welcome card.
        if amount > 0.0 and (live or self.previewing):
            breath = 0.5 + 0.5 * math.sin(self.phase * 0.07) if live else 0.5
            strength = ((0.55 + 0.10 * breath + 0.35 * self.ring) if live else 0.6) * amount
            spread = 1.0 + (0.012 * breath + 0.05 * self.ring if live else 0.0)
            margin = float(self.GLOW)
            glow = glow_pixmap(width, height, radius, vivid, margin, 15.0,
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
                p.drawPixmap(QtCore.QRectF(cx - gw / 2.0, cy - gh / 2.0, gw, gh), glow,
                             QtCore.QRectF(glow.rect()))
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
                edge.setAlpha(150)
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
            p.setPen(QtGui.QPen(edge, 1.2))
            p.drawRoundedRect(card, radius, radius)
        p.setPen(Qt.NoPen)

        font = QtGui.QFont("Segoe UI")
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
                words, colour, lines = warning, theme["warning"], 1
            elif self.previewing:
                words = self.preview_words or "Your words appear here"
                colour = theme["muted"]
            elif self.live_text:
                words, colour = self.live_text, theme["text2"]
            else:
                words, colour = "Listening\u2026", theme["faint"]
            p.setPen(colour)
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

class EdgeStrip(QtWidgets.QWidget):
    """One edge of the screen glow: a thin see-through window that fades from
    the screen edge inward. Clicks pass straight through it, and it can never
    take the keyboard. It is painted once; after that only its overall
    opacity changes, which costs next to nothing."""

    def __init__(self, side):
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                            | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.side = side
        self.colour = QtGui.QColor(ACCENT)

    # How far in -> how strong. A thin, fully bright line right at the edge with
    # a short halo behind it: that is what makes it look like light. (Dim and
    # wide looks like smoke, most of all over a dark screen.)
    STOPS = ((0.0, 255), (0.035, 240), (0.09, 135), (0.2, 58), (0.4, 20), (0.7, 5),
             (1.0, 0))

    def _shade(self, alpha):
        # A lighter shade of the colour: bright at the edge and gone quickly
        # reads as light. (A faint, even tint reads as a shadow.)
        base = QtGui.QColor(self.colour)
        # The brighter the point, the closer to white: a hot core, coloured halo.
        lift = 0.10 + 0.25 * (alpha / 255.0) ** 2
        shade = QtGui.QColor(int(base.red() + (255 - base.red()) * lift),
                             int(base.green() + (255 - base.green()) * lift),
                             int(base.blue() + (255 - base.blue()) * lift))
        shade.setAlpha(alpha)
        return shade

    def paintEvent(self, _event):
        w, h = float(self.width()), float(self.height())
        p = QtGui.QPainter(self)
        if self.side in ("left", "right"):
            # The side strips stop short of the corners (the top and bottom
            # strips own those), so nothing overlaps and doubles up.
            start, end = (0.0, w) if self.side == "left" else (w, 0.0)
            fall = QtGui.QLinearGradient(QtCore.QPointF(start, 0), QtCore.QPointF(end, 0))
            for position, alpha in self.STOPS:
                fall.setColorAt(position, self._shade(alpha))
            p.fillRect(QtCore.QRectF(0, 0, w, h), QtGui.QBrush(fall))
            p.end()
            return
        d = h                                    # the strip is as tall as the glow is deep
        edge, inner = (0.0, d) if self.side == "top" else (d, 0.0)
        fall = QtGui.QLinearGradient(QtCore.QPointF(0, edge), QtCore.QPointF(0, inner))
        for position, alpha in self.STOPS:
            fall.setColorAt(position, self._shade(alpha))
        p.fillRect(QtCore.QRectF(d, 0, max(0.0, w - 2 * d), d), QtGui.QBrush(fall))
        # Corners: the two edges meet with no join and no doubled-up patch.
        size = max(1, int(round(d)))
        for at_right in (False, True):
            picture = self._corner(size, at_right, self.side == "bottom")
            p.drawImage(QtCore.QRectF(w - d if at_right else 0.0, 0, d, d), picture)
        p.end()

    def _corner(self, size, flip_x, flip_y):
        """One corner of the glow as a picture, worked out point by point with
        the same fall-off as the straight edges."""
        base = QtGui.QColor(self.colour)
        across = (np.arange(size, dtype=np.float32) + 0.5)
        gap_x, gap_y = np.meshgrid(size - across, size - across)
        # Square to the screen: each point is exactly as strong as it would be
        # on the nearer edge, so a corner is no heavier than the edges and
        # covers nothing extra. (A rounded turn filled the corner in.)
        reach = np.maximum(gap_x, gap_y)
        depth = np.clip(1.0 - reach / size, 0.0, 1.0)
        alpha = np.interp(depth, [stop[0] for stop in self.STOPS],
                          [stop[1] for stop in self.STOPS]) / 255.0
        pixels = np.empty((size, size, 4), dtype=np.uint8)   # blue, green, red, alpha
        lift = 0.10 + 0.25 * alpha ** 2          # same rule as _shade
        pixels[..., 0] = (base.blue() + (255 - base.blue()) * lift) * alpha
        pixels[..., 1] = (base.green() + (255 - base.green()) * lift) * alpha
        pixels[..., 2] = (base.red() + (255 - base.red()) * lift) * alpha
        pixels[..., 3] = 255 * alpha
        if flip_x:
            pixels = pixels[:, ::-1]
        if flip_y:
            pixels = pixels[::-1]
        data = np.ascontiguousarray(pixels).tobytes()
        return QtGui.QImage(data, size, size, 4 * size,
                            QtGui.QImage.Format_ARGB32_Premultiplied).copy()


class ScreenGlow:
    """A glow round the edge of the whole screen while you dictate (optional,
    off by default). It breathes in when dictation starts, swells with your
    voice and fades out at the end."""

    def __init__(self):
        self.strips = None                       # made the first time it is used
        self.fade = 0.0
        self.showing = False

    def start(self, colour, area):
        """Show round `area` (the screen's rectangle) in `colour`."""
        if self.strips is None:
            self.strips = [EdgeStrip(side) for side in ("top", "bottom", "left", "right")]
        x, y, w, h = area.x(), area.y(), area.width(), area.height()
        d = int(SCREEN_GLOW_SIZE)
        places = ((x, y, w, d), (x, y + h - d, w, d),
                  (x, y + d, d, h - 2 * d), (x + w - d, y + d, d, h - 2 * d))
        for strip, place in zip(self.strips, places):
            strip.colour = QtGui.QColor(colour)
            strip.setGeometry(*place)
            if not self.showing:
                strip.setWindowOpacity(0.0)
            strip.show()
            strip.update()
        if not self.showing:                     # already up (Settings demo): no restart
            self.fade = 0.0
        self.showing = True

    def step(self, level, active):
        """Advance one frame. Returns False once it has faded away."""
        if not self.showing:
            return False
        target = ((0.68 + 0.32 * max(0.0, min(1.0, level)))
                  * SCREEN_GLOW_STRENGTH / 100.0) if active else 0.0
        self.fade += (target - self.fade) * (0.16 if target > self.fade else 0.10)
        if not active and self.fade < 0.02:
            self.stop()
            return False
        for strip in self.strips:
            strip.setWindowOpacity(self.fade)
        return True

    def stop(self):
        self.showing = False
        self.fade = 0.0
        for strip in self.strips or ():
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
        if len(_GLOW_CACHE) > 12:
            _GLOW_CACHE.clear()
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


def accent_on_light(accent):
    """A deeper, richer version of the accent colour for pale backgrounds
    (the same hue, so it still reads as the colour that was picked)."""
    hue, saturation, lightness, _alpha = accent.getHslF()
    if saturation > 0.25:                        # leave greys and white neutral
        saturation = min(1.0, saturation * 1.1 + 0.08)
    return QtGui.QColor.fromHslF(max(0.0, hue), saturation, min(lightness, 0.37))


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
                "chip": QtGui.QColor(0, 0, 0, 10), "chip_hot": QtGui.QColor(0, 0, 0, 24),
                "chip_border": QtGui.QColor(0, 0, 0, 40),
                "sheen": QtGui.QColor(255, 255, 255, 150)}
    bg.setAlpha(247)
    return {"bg": bg, "border": QtGui.QColor(255, 255, 255, 28),
            "shadow": QtGui.QColor(0, 0, 0, 10),
            "text": QtGui.QColor(240, 240, 244), "text2": QtGui.QColor(214, 216, 224),
            "muted": QtGui.QColor(170, 172, 182), "faint": QtGui.QColor(120, 122, 132),
            "quiet": QtGui.QColor(255, 255, 255, 60), "warning": QtGui.QColor(255, 181, 71),
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
        self.fx_timer = QtCore.QTimer()        # runs only while an effect is showing
        self.fx_timer.setInterval(33)
        self.fx_timer.timeout.connect(self._fx_tick)
        self.keys_up_checks = 0
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
        self.accept_changed = False            # the user chose to use changed files
        self.finished.connect(self.on_finished)
        self.live_text.connect(self.on_live_text)
        self.hotkey_captured.connect(self.on_hotkey_captured)
        self.paste_again.connect(self.on_paste_again)
        self._capture_apply = True             # is the hotkey being recorded the main one?
        self._capture = False                  # waiting for the user to press a hotkey
        self._capture_done = None              # who to tell when they have
        self._swallow_up = None                # key whose release should be hidden too
        self._typing_until = 0.0               # ignore our own typed keystrokes
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
        if not self.intro_open and (STARTUP_FLASH or STARTUP_MESSAGE):
            # Up straight away while the model loads behind it: just a small
            # pill with a word on it. No waveform, no timer, nothing recorded.
            self.overlay.show_message(
                "Startingâ€¦" if (_setup_done() or local_model_dir())
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
            # Files already here are checked against their fingerprints before
            # they are used. (With the update check on, a change is expected.)
            saved = _config.get("model_files")
            expecting = bool(MODEL_CHECKSUMS.get(MODEL)) or (
                isinstance(saved, dict) and bool(saved.get(MODEL)))
            if expecting and not self.accept_changed and not UPDATING:
                changed, _record = check_model_files()
                if changed:
                    print("Model files differ from their fingerprints:", ", ".join(changed),
                          file=sys.stderr)
                    self.model_suspect.emit(", ".join(changed))
                    return
            import onnx_asr
            kwargs = {"quantization": QUANTIZATION} if QUANTIZATION else {}
            by_hand = local_model_dir()
            if not by_hand and network_is_open():
                # A download (or update check) the user agreed to. The files are
                # fetched first and checked against their fingerprints BEFORE
                # anything is loaded from them. (The speech library would also
                # never look online while older files are on disk, so an update
                # must ask huggingface.co itself.)
                from huggingface_hub import snapshot_download
                try:
                    folder = snapshot_download(model_repo(), allow_patterns=MODEL_FILES)
                    print("Fetched the model from huggingface.co (version %s)."
                          % os.path.basename(folder))
                except Exception:
                    if not UPDATING or not model_files():
                        raise                    # no model at all: the download failed
                    print("Could not check for a newer model; using the one on this PC.",
                          file=sys.stderr)
                if MODEL_CHECKSUMS.get(MODEL) and not self.accept_changed and not UPDATING \
                        and not uses_own_files():
                    changed, _record = check_model_files()
                    if changed:
                        print("Downloaded model files do not match the published "
                              "fingerprints:", ", ".join(changed), file=sys.stderr)
                        close_network("the downloaded files did not match")
                        self.model_suspect.emit(", ".join(changed))
                        return
            # Lighter engine settings (measured): no memory pool saves about
            # 250 MB, and 4 threads are as fast as all of them for dictation.
            kwargs["sess_options"] = engine_options()
            if by_hand:
                # Model files were downloaded by hand: load them, no network.
                print("Loading model from", by_hand)
                self.model = onnx_asr.load_model(MODEL, by_hand, **kwargs)
            else:
                self.model = onnx_asr.load_model(MODEL, **kwargs)
            self.load_seconds = time.perf_counter() - started
            print("Model loaded in %.1fs (%.1fs since launch, %s)" % (
                self.load_seconds, time.perf_counter() - _T0,
                "online, agreed" if network_is_open() else "offline"))
            self.model_ready.emit()        # usable now
            try:   # then one silent run, so the first real dictation isn't slower.
                # It holds the model for a moment; a dictation started meanwhile
                # simply waits its turn.
                quiet = (np.random.randn(SAMPLE_RATE) * 1e-4).astype(np.float32)
                with self.model_lock:
                    self.model.recognize(quiet, sample_rate=SAMPLE_RATE)
            except Exception:
                pass
            if network_is_open():
                try:   # first run: fetch the small pause-detection model too, so
                    onnx_asr.load_vad("silero")    # it is on disk for offline use
                except Exception:
                    pass
            # Everything agreed to is on disk now: shut the door again at once,
            # rather than leaving it open until the next start.
            close_network("the model is on this PC")
            try:   # remember that everything is on disk -> offline from now on
                mark_downloaded()
            except Exception:
                pass
            try:
                # Record each file's fingerprint (read in full only the first
                # time, or after a change). Freshly downloaded files are held
                # to the published fingerprints, when there are any.
                if UPDATING:
                    # A newer model may just have been fetched: the published
                    # fingerprints saved for the old one no longer apply.
                    fetched = _config.get("published_fingerprints")
                    if isinstance(fetched, dict) and MODEL in fetched:
                        fetched = dict(fetched)
                        fetched.pop(MODEL, None)
                        save_setting("published_fingerprints", fetched)
                    # An agreed update may bring files newer than the built-in
                    # fingerprints: those files are the ones checked from now on.
                    if MODEL_CHECKSUMS.get(MODEL) and not uses_own_files() \
                            and check_model_files()[0]:
                        set_uses_own_files(True)
                changed, record = check_model_files()
                if changed and MODEL_CHECKSUMS.get(MODEL) and not self.accept_changed \
                        and not UPDATING:
                    print("Downloaded model files do not match the published "
                          "fingerprints:", ", ".join(changed), file=sys.stderr)
                    self.model = None
                    self.model_suspect.emit(", ".join(changed))
                else:
                    remember_model_files(record)
            except Exception as exc:
                print("Could not record the model's fingerprints:", exc, file=sys.stderr)
        except Exception as exc:
            if not network_is_open() and not model_files():
                # Files went missing: the next start asks again (as on a first
                # launch) before going online to fetch them.
                try:
                    have = [m for m in downloaded_models() if m != _marker_text()]
                    with open(_MARKER, "w", encoding="utf-8") as fh:
                        fh.write("\n".join(have))
                except Exception:
                    pass
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
        QtCore.QTimer.singleShot(2500, self.maybe_show_tray_tip)
        if self.state == "idle":
            self.overlay.show_message(pretty(HOTKEY) if SHOW_HOTKEY_HINT else "Ready",
                                      ms=1800, pulse=True)
        elif self.state == "loading":
            self.overlay.show_message("Starting\u2026", ms=None)

    def maybe_show_tray_tip(self):
        """Once: point out where Settings live, by the clock (see TrayTip)."""
        if (_config.get("tray_tip_seen") or self.tray is None or self.intro_open
                or getattr(self, "tray_tip", None) is not None):
            return
        self.tray_tip = TrayTip(self.tray, lambda: setattr(self, "tray_tip", None))
        self.tray_tip.show()

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
    BLOCKED = "The download seems blocked  Â·  click for help"
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
        if (self.overlay.mode == "idle" or not self.overlay.isVisible()) \
                and not self.intro_open:
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
        self.state = "nomodel"
        import gc
        gc.collect()                           # so Windows lets the files be deleted
        self.overlay.dismiss(force=True)
        return True

    def on_model_suspect(self, names):
        """The model's files are not the ones checked before. Nothing is loaded
        from them unless the user says so."""
        self.on_cancel()                       # a dictation under way: mic off first
        self.state = "loading"
        self.overlay.dismiss(force=True)
        use = dialog(
            None, "%s: speech model changed" % APP_TITLE,
            "The speech model's files have changed",
            "These files no longer match the fingerprints %s checks them against:"
            "\n\n%s\n\nThat can mean they were replaced, damaged, or updated by "
            "something else. If you were not expecting it, do not continue: quit, "
            "delete the model's folder and download it again.\n\n"
            "The folder is shown under Settings, General, Speech model."
            % (APP_TITLE, names),
            accept="Use them anyway", reject="Quit")
        if use is True:
            self.accept_changed = True
            if MODEL_CHECKSUMS.get(MODEL):
                set_uses_own_files(True)         # do not ask again at every start
            try:
                remember_model_files(check_model_files()[1])
            except Exception:
                pass
            self._begin_load()
        else:
            QtWidgets.QApplication.quit()

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
                self.overlay.show_message("Still loading the speech modelâ€¦", ms=None)
            # otherwise the welcome or "Starting" card already says so
        elif self.state == "idle":
            self._start()
            self.do_action = self._action_pressed
        elif self.state == "recording" and not HOLD_TO_TALK:
            self.do_action = self.do_action or self._action_pressed   # either press counts
            self._finish()
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
            if time.monotonic() < self._typing_until:
                # Our own typed text: hands off. But a hotkey being let go just
                # now still counts, or the next Space would be taken for it.
                if self._held and event.event_type == "up" and \
                        (event.name or "").lower().replace("left ", "").replace(
                            "right ", "") == self._held_trigger:
                    self._held = False
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

    def on_released(self):
        if HOLD_TO_TALK and self.state == "recording":
            self._finish()

    def on_pill_clicked(self):
        """Clicking the small always-on pill starts a dictation; clicking the
        card again finishes it (whatever the hotkey is set to do)."""
        if self.state == "idle":
            self._start(by_click=True)

    def on_finish_clicked(self):
        if self.state == "recording":
            self._finish()

    def _start(self, by_click=False):
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
            self.overlay.show_message("No microphone found  \u00b7  click for help", ms=6000)
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
        if LIVE_PREVIEW and self.popup_shown:    # no card: no need to read along
            self.live_timer.start()
        self.record_started = time.monotonic()
        self.keys_up_checks = 0
        self.watch_timer.start()
        self.show_listening(True)
        self._start_effects()

    def demo_screen_glow(self):
        """From Settings: show the screen glow for a moment (nothing records)."""
        if self.state in ("idle", "loading", "nomodel"):
            self.fx_demo_until = time.monotonic() + 1.6
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
                self.screen_glow.start(colour, screen.geometry())
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
                alive = self.screen_glow.step(level, active)
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
            # stop anyway.
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
        if self.state != "recording" or not LIVE_PREVIEW:
            self.live_timer.stop()
            return
        if self.live_busy:
            return                               # previous pass still running
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
                text = str(self.model.recognize(audio, sample_rate=SAMPLE_RATE))
            self.live_text.emit(session, " ".join(text.split()))
        except Exception:
            pass
        finally:
            self.live_busy = False

    def on_live_text(self, session, text):
        if session == self.session and self.state == "recording" and text:
            self.overlay.set_live_text(text)

    def move_popup(self):
        if self.state in ("idle", "loading", "nomodel"):
            self.overlay.invite_drag()

    def resize_popup(self):
        """From Settings: hold the pop-up on screen so its edges can be dragged."""
        if self.state in ("idle", "loading", "nomodel") and self.overlay.mode != "welcome":
            self.overlay.preview(ms=12000, words="Drag my left or right edge")

    def preview_popup(self):
        if self.state in ("idle", "loading", "nomodel") and self.overlay.mode != "welcome":
            self.overlay.preview()

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
        if self.state != "recording":
            return
        self.live_timer.stop()
        self.watch_timer.stop()
        self.recorder.stop()
        self.show_listening(False)
        self.state = "idle"
        self.overlay.dismiss()

    def _finish(self):
        self.live_timer.stop()
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
            with self.model_lock:              # waits for a preview pass to end
                if len(audio) > LONG_AUDIO_SECONDS * SAMPLE_RATE:
                    if self.model_vad is None:     # loaded on first use, not at start
                        try:
                            import onnx_asr
                            self.model_vad = self.model.with_vad(onnx_asr.load_vad("silero"))
                        except Exception:
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
        self.state = "idle"
        self.overlay.show_message("Transcription failed", ms=2500)

    def on_finished(self, text):
        self.state = "idle"
        text = " ".join(text.split())          # one line: a stray Enter could send a message
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
        self.remember(text)
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
        value = ctypes.c_int(1)
        for attribute in (20, 19):               # DWMWA_USE_IMMERSIVE_DARK_MODE
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    wintypes.HWND(int(window.winId())), attribute,
                    ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


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
QSlider { min-height: 22px; }
QSlider::groove:horizontal { height: 4px; background: #34363E; border-radius: 2px; }
QSlider::sub-page:horizontal { height: 4px; background: %(accent)s; border-radius: 2px; }
QSlider::add-page:horizontal { height: 4px; background: #34363E; border-radius: 2px; }
QSlider::handle:horizontal { width: 14px; height: 14px; margin: -5px 0;
    border-radius: 7px; background: #F2F2F5; }
QSlider::handle:horizontal:disabled { background: #6B6D77; }
QSlider::sub-page:horizontal:disabled { background: #454852; }
""" % {"accent": ACCENT, "tick": ('image: url("%s");' % tick) if tick else ""}


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
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView { background: #24262C; color: #E6E7EC; border: 1px solid #34363E;
    selection-background-color: #3A3D46; outline: none; }
QCheckBox { spacing: 9px; }
QCheckBox:disabled, QLabel:disabled { color: #6B6D77; }
QTabWidget::pane { border: 1px solid #2A2C33; border-radius: 10px; background: #1B1C21;
    top: -1px; }
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
_PORTABLE_CHECKS = {
    "hotkey": lambda v: _is_hotkey(v),
    "action_hotkey": lambda v: _is_hotkey(v, empty_ok=True),
    "paste_hotkey": lambda v: _is_hotkey(v, empty_ok=True),
    "action_key": lambda v: v in [c[0] for c in AFTER_CHOICES if c[0]],
    "after_key": lambda v: v in [c[0] for c in AFTER_CHOICES],
    "after_delay": lambda v: v in [d[0] for d in AFTER_DELAYS],
    "accent": lambda v: _is_colour(v),
    "popup_bg": lambda v: _is_colour(v),
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
             "hold_to_talk", "live_preview", "type_text", "keep_last", "check_updates"):
    _PORTABLE_CHECKS[_key] = lambda v: isinstance(v, bool)

# Turning these on touches the clipboard, keeps text, or goes online. For
# "type_text" it is turning it OFF that switches to pasting via the clipboard.
_PORTABLE_CAREFUL = {
    "type_text": (False, "Paste using the clipboard instead of typing"),
    "keep_last": (True, "Missed text safety (keeps your last dictation in memory "
                        "for %d minutes)" % (KEEP_LAST_SECONDS // 60)),
    "check_updates": (True, "Check for model updates at startup (this application will "
                            "go online, to huggingface.co, a third-party website that we "
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
        risky = _PORTABLE_CAREFUL.get(key)
        if risky and value == risky[0] and _config.get(key, globals().get(
                {"type_text": "TYPE_TEXT", "keep_last": "KEEP_LAST",
                 "check_updates": "CHECK_UPDATES"}[key])) != value:
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
    field.setPlaceholderText(_PORTABLE_PREFIX + "â€¦")
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
            online = "check_updates" in careful
            names = "\n".join("â€¢  " + _PORTABLE_CAREFUL[key][1] for key in careful)
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


def dialog(parent, title, headline, body, accept=None, reject="Close", other=None):
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

    headline = label("Checkingâ€¦", "headline")
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
    online = QtWidgets.QPushButton("Check against the published files")
    show = QtWidgets.QPushButton("Show fingerprints")
    close = QtWidgets.QPushButton("Close")
    for button in (again, online, show):
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

    def verdict(problems, checked_online):
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
        elif checked_online:
            headline.setText("The model's files are genuine")
            summary.setText("They match the fingerprints published on the model's own "
                            "website. Those fingerprints are now saved on this PC. Every "
                            "time %s starts it checks the files against them without "
                            "going online. Nothing needs doing." % APP_TITLE)
        else:
            headline.setText("The model's files are unchanged")
            summary.setText(
                "They are exactly as they were when %s first used them. Every time it "
                "starts it checks them again on this PC without going online. Nothing "
                "needs doing.\n\nTo also "
                "confirm they match the files published on the model's website, click "
                "\"Check against the published files\"." % APP_TITLE)
        box.adjustSize()

    def local_check():
        files = model_files()
        if not files:
            headline.setText("No model files were found")
            summary.setText("The speech model has not been downloaded yet.")
            for name in MODEL_FILES:
                paint(name, "not found", "plain")
            online.setEnabled(False)
            show.setEnabled(False)
            return
        changed, _record = check_model_files()
        for name in MODEL_FILES:
            if name not in files:
                paint(name, "missing", "bad")
            elif name in changed:
                paint(name, "changed since it was first checked", "bad")
            else:
                paint(name, "unchanged", "good")
        verdict([n for n in MODEL_FILES if n in changed or n not in files], False)

    def online_work():
        published, why = fetch_published_fingerprints()
        mine = {}
        if published is not None:
            for name, path in model_files().items():
                try:
                    mine[name] = file_fingerprint(path)
                except Exception:
                    mine[name] = ""
        work["result"] = (published, why, mine)

    def online_check():
        if work["busy"]:
            return
        if not dialog(box, "Go online to check?", "This application will go online",
                      "It will connect to huggingface.co once, now, to read the published "
                      "fingerprints of the model's files. That is a third-party website "
                      "that we do not run or control.\n\nNothing about you or your files "
                      "is sent. The comparison is done on this PC.",
                      accept="Check now", reject="Cancel"):
            return
        work["busy"], work["result"] = True, None
        online.setEnabled(False)
        headline.setText("Checking against the published filesâ€¦")
        summary.setText("Reading the published fingerprints and comparing each file. "
                        "This takes a few seconds.")
        threading.Thread(target=online_work, daemon=True).start()
        poll.start()

    def online_done():
        if work["result"] is None:
            return
        poll.stop()
        work["busy"] = False
        online.setEnabled(True)
        published, why, mine = work["result"]
        if published is None:
            headline.setText("The website could not be reached")
            summary.setText("Nothing was compared, and nothing has changed. Check your "
                            "Internet connection and try again.\n\n(%s)" % why)
            box.adjustSize()
            return
        problems, good = [], {}
        for name in MODEL_FILES:
            want, have = published.get(name, ""), mine.get(name, "")
            if not have:
                paint(name, "missing", "bad")
                problems.append(name)
            elif not want:
                paint(name, "no published fingerprint (small file)", "plain")
            elif want.lower() == have.lower():
                paint(name, "matches the published file", "good")
                good[name] = have
            else:
                paint(name, "does NOT match the published file", "bad")
                problems.append(name)
        if not problems and good:
            # From now on every start is checked against the published values.
            saved = _config.get("published_fingerprints")
            saved = dict(saved) if isinstance(saved, dict) else {}
            saved[MODEL] = good
            save_setting("published_fingerprints", saved)
        verdict(problems, True)

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

    poll = QtCore.QTimer(box)
    poll.setInterval(200)
    poll.timeout.connect(online_done)
    again.clicked.connect(lambda _checked=False: download_again())
    online.clicked.connect(lambda _checked=False: online_check())
    online.setToolTip("Goes online once, after asking, to read the published fingerprints.")
    show.clicked.connect(lambda _checked=False: show_details())
    close.clicked.connect(lambda _checked=False: box.reject())
    local_check()
    box.exec()
    poll.stop()


STUCK_HELP = ("Some of the speech model has arrived, but the rest is not coming through. "
              "The small files download from huggingface.co; the large ones come from "
              "other addresses ending in hf.co. When only the small ones arrive, "
              "something between this PC and those addresses is usually blocking them: "
              "a firewall, an ad blocker, or a DNS filter (a Pi-hole, for "
              "example).\n\n"
              "What you can do:\n\n"
              "1.  Allow huggingface.co and addresses ending in hf.co in whatever is "
              "filtering your network, then start %s again. It carries on from where "
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
    folder = manual_model_dir()
    page = MODEL_PAGES.get(MODEL, "https://huggingface.co/istupakov")
    try:
        os.makedirs(folder, exist_ok=True)
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
    text("1.  Open the model's page.")
    text("Please note: this is a third-party website that we do not run or control. "
         "Opening it and downloading from it is your choice.", "aside")
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
    text("2.  Download these %s files:\n%s"
         % ({4: "four", 5: "five"}.get(len(MODEL_FILES), str(len(MODEL_FILES))),
            "\n".join("     " + name for name in MODEL_FILES)))
    text("3.  Put them in this folder:\n%s" % folder)
    text("4.  Start %s again. It finds the files, downloads nothing, and goes "
         "straight to the introduction." % APP_TITLE)
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
    p.setPen(Qt.NoPen)
    p.setBrush(QtGui.QColor(colour))
    dot = 7.0 if chosen else 9.0
    p.drawEllipse(middle, dot, dot)
    if chosen:
        p.setBrush(Qt.NoBrush)
        p.setPen(QtGui.QPen(QtGui.QColor("#FFFFFF"), 1.6))
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
        self.setFixedHeight(116 if kind == "look" else 96)   # the little screen needs room

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
    def _font(size, bold=False):
        font = QtGui.QFont("Segoe UI")
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
        p.setBrush(QtGui.QColor("#5B8DEF"))
        p.drawRoundedRect(QtCore.QRectF(x + 12, y + 10, 16, 16), 4, 4)
        self._wave(p, x + 38, x + w - 44, y + 18, 6.0, ACCENT, 1.8)
        light = is_light(POPUP_BG)
        p.setFont(self._font(11))
        p.setPen(QtGui.QColor("#6E6E76" if light else "#AAACB6"))
        p.drawText(QtCore.QRectF(x + w - 42, y + 8, 30, 20), Qt.AlignRight | Qt.AlignVCenter,
                   "0:03")
        p.setPen(QtGui.QColor("#3C3C43" if light else "#D6D8E0"))
        p.drawText(QtCore.QRectF(x + 12, y + 32, w - 24, 18), Qt.AlignLeft | Qt.AlignVCenter,
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
        for i, (key, size) in enumerate(zip(keys, sizes)):
            p.setPen(QtGui.QPen(QtGui.QColor("#4A4D57"), 1.2))
            p.setBrush(QtGui.QColor("#24262C"))
            p.drawRoundedRect(QtCore.QRectF(x, h / 2 - 20, size, 40), 8, 8)
            p.setPen(QtGui.QColor("#F0F0F4"))
            p.drawText(QtCore.QRectF(x, h / 2 - 20, size, 40), Qt.AlignCenter, key)
            x += size
            if i < len(keys) - 1:
                p.setPen(QtGui.QColor("#8E909A"))
                p.drawText(QtCore.QRectF(x, h / 2 - 20, 22, 40), Qt.AlignCenter, "+")
                x += 22.0

    def _look(self, p, w, h):
        # A little screen, with the glow round its edge if that is switched on.
        screen = QtCore.QRectF(w / 2 - 150, 4, 300, h - 8)
        p.setPen(QtGui.QPen(QtGui.QColor("#34363E"), 1.0))
        p.setBrush(QtGui.QColor("#0E0F12"))
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
        pill = QtCore.QRectF(w / 2 - pill_w / 2, h / 2 - 18, pill_w, 36)
        p.setPen(QtGui.QPen(QtGui.QColor("#4A4D57"), 1.2))
        p.setBrush(QtGui.QColor(POPUP_BG))
        p.drawRoundedRect(pill, 18, 18)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(ACCENT))
        p.drawEllipse(QtCore.QPointF(pill.left() + 18, h / 2), 4, 4)
        p.setPen(QtGui.QColor("#1D1D1F" if is_light(POPUP_BG) else "#F0F0F4"))
        p.drawText(pill.adjusted(30, 0, -8, 0), Qt.AlignLeft | Qt.AlignVCenter, text)
        p.setFont(self._font(11))
        p.setPen(QtGui.QColor("#8E909A"))
        p.drawText(QtCore.QRectF(0, h / 2 - 18, pill.left() - 12, 36),
                   Qt.AlignRight | Qt.AlignVCenter, "Click: dictate")
        p.drawText(QtCore.QRectF(pill.right() + 12, h / 2 - 18, w - pill.right() - 12, 36),
                   Qt.AlignLeft | Qt.AlignVCenter, "Right-click: Settings")


def first_look_window():
    """The very first thing a new user sees: pick a colour, dark or light, and
    whether the screen glows. It comes before the download question, so the
    download card and everything after it already look the way they chose."""
    box = QtWidgets.QDialog(None)
    box.setWindowTitle("Welcome to %s" % APP_TITLE)
    box.setWindowIcon(app_icon())
    box.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.WindowCloseButtonHint
                       | Qt.WindowStaysOnTopHint)
    box.setFixedWidth(500)

    def restyle():
        box.setStyleSheet(settings_style() + """
QLabel#headline { color: #FFFFFF; font-size: 21px; font-weight: 600; }
QLabel#body { color: #B9BBC6; font-size: 14px; }
QLabel#field { color: #E6E7EC; font-size: 13px; }
QCheckBox { font-size: 13px; spacing: 10px; }
QPushButton { padding: 8px 20px; border-radius: 8px; font-size: 13px; }
QPushButton#primary { background: %(accent)s; color: #101114; border: 1px solid %(accent)s;
    font-weight: 600; }
QPushButton#primary:hover { background: %(accent)s; border-color: #FFFFFF; }
QPushButton#chosen { border: 1px solid %(accent)s; }
""" % {"accent": ACCENT})

    restyle()
    dark_title_bar(box)
    layout = QtWidgets.QVBoxLayout(box)
    layout.setContentsMargins(34, 26, 34, 24)
    layout.setSpacing(14)
    picture = Sketch("look")
    layout.addWidget(picture)
    layout.addSpacing(6)
    headline = QtWidgets.QLabel("Welcome to %s" % APP_TITLE)
    headline.setObjectName("headline")
    headline.setAlignment(Qt.AlignHCenter)       # under the centred picture
    layout.addWidget(headline)
    body = QtWidgets.QLabel("First, make it yours. This is the small pop-up you will see "
                            "while you dictate. Pick a colour and a look; you can change "
                            "all of it later in Settings.")
    body.setObjectName("body")
    body.setWordWrap(True)
    body.setAlignment(Qt.AlignHCenter)
    layout.addWidget(body)
    layout.addSpacing(4)

    def line(title, widgets):
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        label = QtWidgets.QLabel(title)
        label.setObjectName("field")
        label.setFixedWidth(84)
        row.addWidget(label)
        for widget in widgets:
            row.addWidget(widget)
        row.addStretch(1)
        layout.addLayout(row)

    swatches, themes = [], []

    def repaint():
        for button, value in swatches:
            button.setStyleSheet("background: transparent; border: none; padding: 0; "
                                 "min-height: 0;")
            button.setIcon(QtGui.QIcon(swatch_picture(value, value.lower() == ACCENT.lower(),
                                                      3.0)))
            button.setIconSize(QtCore.QSize(24, 24))
        for button, value in themes:
            button.setObjectName("chosen" if value.lower() == POPUP_BG.lower() else "")
        restyle()
        box.setWindowIcon(app_icon())
        picture.update()

    def pick_colour(value):
        set_accent(value)
        repaint()
        show_live()

    def pick_theme(value):
        set_option("POPUP_BG", "popup_bg", value)
        repaint()

    for name, value in ACCENT_CHOICES:
        button = QtWidgets.QPushButton()
        button.setFixedSize(24, 24)
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(name)
        button.clicked.connect(lambda _checked=False, v=value: pick_colour(v))
        swatches.append((button, value))
    line("Colour", [button for button, _value in swatches])
    for name, value in BACKGROUND_CHOICES:
        button = QtWidgets.QPushButton(name)
        button.clicked.connect(lambda _checked=False, v=value: pick_theme(v))
        themes.append((button, value))
    line("Pop-up", [button for button, _value in themes])
    glow = QtWidgets.QCheckBox("Glow around the edge of the screen while I dictate")
    glow.setChecked(bool(SCREEN_GLOW))
    layout.addWidget(glow)

    # The real glow, on the real screen, while it is being chosen: what you set
    # here is exactly what you will get. Nothing is recorded.
    live = ScreenGlow()
    sliders = []

    def show_live():
        try:
            if SCREEN_GLOW:
                screen = QtGui.QGuiApplication.primaryScreen()
                live.start(glow_base(), screen.geometry())
        except Exception as exc:
            print("The glow preview could not be shown:", exc, file=sys.stderr)

    def live_tick():
        try:
            live.step(0.55, bool(SCREEN_GLOW))
        except Exception:
            pass

    def slider(title, name, key, low, high):
        made = QtWidgets.QSlider(Qt.Horizontal)
        made.setRange(low, high)
        made.setValue(int(globals()[name]))
        made.setFixedWidth(220)

        def moved(_value=None):
            globals()[name] = int(made.value())      # seen at once, on screen
            if not made.isSliderDown():
                save_setting(key, int(made.value()))
            picture.update()
            show_live()

        made.valueChanged.connect(moved)
        made.sliderReleased.connect(lambda: save_setting(key, int(made.value())))
        sliders.append(made)
        line(title, [made])
        return made

    slider("Strength", "SCREEN_GLOW_STRENGTH", "screen_glow_strength", 5, 100)
    slider("Size", "SCREEN_GLOW_SIZE", "screen_glow_size", 20, 200)

    def glow_toggled(on):
        set_option("SCREEN_GLOW", "screen_glow", bool(on))
        for made in sliders:
            made.setEnabled(bool(on))
        picture.update()
        show_live()

    glow.toggled.connect(glow_toggled)
    for made in sliders:
        made.setEnabled(bool(SCREEN_GLOW))
    ticker = QtCore.QTimer(box)
    ticker.setInterval(33)
    ticker.timeout.connect(live_tick)
    ticker.start()
    layout.addSpacing(8)
    buttons = QtWidgets.QHBoxLayout()
    buttons.addStretch(1)
    go = QtWidgets.QPushButton("Continue")
    go.setObjectName("primary")
    go.setDefault(True)
    go.clicked.connect(lambda _checked=False: box.accept())
    buttons.addWidget(go)
    layout.addLayout(buttons)
    repaint()
    show_live()
    box.exec()                                   # closing it also carries on
    ticker.stop()
    try:
        live.stop()                              # the preview glow goes with the window
    except Exception:
        pass


class IntroWindow(QtWidgets.QDialog):
    """Shown the first time the application starts: a few short pages to say
    hello, choose a hotkey and a look, and point out how to get around. Every
    choice here is an ordinary setting and can be changed later in Settings."""

    CANT_HEAR = ("We can't hear you. Is your microphone muted, or is a different "
                 "input device in use? Choose another one above and try again.")

    def __init__(self, dictation, look_chosen=False):
        super().__init__()
        self.dictation = dictation
        self.look_page = -1                      # no look page unless one is added below
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
        self.stack = QtWidgets.QStackedWidget()
        outer.addWidget(self.stack)
        self.sketches = []
        self.swatches = []

        def page(kind, headline, body, controls=True):
            """One page: a picture, a headline, a paragraph, and (for pages
            with things to set) a panel that holds the controls. Returns the
            layout to put the controls in."""
            holder = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
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

        # 1. Hello
        first = page("welcome", ("How %s works" if look_chosen else "Welcome to %s")
                     % APP_TITLE, controls=False, body=
                     "Hold a hotkey, speak, and your words are typed into whatever "
                     "application you are using.\n\nYour speech is turned into text "
                     "on this PC. %s does not save it or send it anywhere; where the "
                     "text goes is up to the program you type into." % APP_TITLE)
        first.addStretch(1)

        # 2. The hotkey and the microphone, on one page, as short labelled rows.
        # (There is no separate microphone test: trying the hotkey in the box
        # below is the test, and a note appears if nothing is heard.)
        second = page("hotkey", "Hotkey and microphone",
                      "Choose what you press to dictate and which microphone to use. "
                      "The microphone is only on while you dictate.")

        def labelled(title, *widgets):
            name = QtWidgets.QLabel(title)
            name.setObjectName("field")
            name.setFixedWidth(86)
            line(second, name, *widgets)

        self.record = QtWidgets.QPushButton("Record")
        self.record.setFixedWidth(84)
        self.record.clicked.connect(self._record)
        self.hotkey_label = QtWidgets.QLabel(pretty(HOTKEY))
        self.hotkey_label.setObjectName("info")
        labelled("Hotkey", self.record, self.hotkey_label, None)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("Hold to talk", True)
        self.mode.addItem("Toggle mode", False)
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
        self.try_box.setStyleSheet("QPlainTextEdit { background: #17181C; color: #F0F0F4; "
                                   "border: 1px solid #34363E; border-radius: 6px; "
                                   "padding: 5px; }")
        second.addWidget(self.try_box)
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
        self.hear.start()

        # 3. The look. Left out when it was already chosen on the very first
        # screen, before the model was downloaded.
        if not look_chosen:
            third = page("look", "Make it yours",
                         "Pick a colour. A small pop-up shows while you dictate; the "
                         "glow round the screen is optional. Both are shown on your "
                         "screen now, so you can see what you are choosing.")
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
            line(third, *(colours + [None]))
            self.glow = QtWidgets.QCheckBox("Glow around the edge of the screen while I dictate")
            self.glow.setChecked(bool(SCREEN_GLOW))
            self.glow.toggled.connect(self._glow_toggled)
            third.addWidget(self.glow)
            third.addStretch(1)

        # 4. Getting around
        fourth = page("tips", "Good to know",
                      "Settings are always two clicks away: right-click the pill on "
                      "screen, or click the %s icon in the taskbar tray, by the clock."
                      "\n\nNo hotkey handy: click the pill to dictate, then click the "
                      "pop-up to finish.\n\nChanged your mind mid-sentence: hover the "
                      "pop-up and click to cancel." % APP_TITLE)
        self.pill = QtWidgets.QCheckBox("Keep a small pill on screen between dictations")
        self.pill.setChecked(bool(ALWAYS_SHOW))
        self.pill.toggled.connect(self._pill_toggled)
        fourth.addWidget(self.pill)
        fourth.addSpacing(6)                     # room between the two choices
        self.startup = QtWidgets.QCheckBox("Start with Windows")
        self.startup.setChecked(starts_with_windows())
        self.startup.toggled.connect(lambda on: set_startup(bool(on)))
        fourth.addWidget(self.startup)
        fourth.addSpacing(12)                    # the button is a separate thing
        more = QtWidgets.QPushButton("Open all settings")
        more.setToolTip("Finish here and open Settings, where everything else lives: "
                        "sounds, what the pop-up shows, the action hotkey and more.")
        more.clicked.connect(self._open_settings)
        line(fourth, more, None)
        fourth.addSpacing(8)
        self.status = small("")
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

        # (Not "self.done": a window already has a done() of its own, which is
        # what closes it. A value stored under that name stops it closing.)
        self.finished_up = False
        self.watch = QtCore.QTimer(self)         # keeps the last page's status current
        self.watch.setInterval(500)
        self.watch.timeout.connect(self._refresh_status)
        self.watch.start()
        self._paint_swatches()
        self._go(0)

    # -- looks ---------------------------------------------------------------
    def _restyle(self):
        self.setStyleSheet(settings_style() + """
QLabel#headline { color: #FFFFFF; font-size: 21px; font-weight: 600; }
QLabel#body { color: #B9BBC6; font-size: 14px; }
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
QPlainTextEdit { font-size: 13px; }
""" % {"accent": ACCENT})

    def _paint_swatches(self):
        for button, value in self.swatches:
            button.setStyleSheet("background: transparent; border: none; padding: 0; "
                                 "min-height: 0;")
            button.setIcon(QtGui.QIcon(swatch_picture(value, value.lower() == ACCENT.lower(),
                                                      3.0)))
            button.setIconSize(QtCore.QSize(24, 24))

    def _redraw(self):
        for sketch in self.sketches:
            sketch.update()

    # -- moving between pages ---------------------------------------------------
    def _go(self, index):
        index = max(0, min(self.stack.count() - 1, index))
        self._note_until = 0.0
        self._show_note(False)
        self.dictation.cancel_capture()          # a half-recorded hotkey is dropped
        self.record.setText("Record")
        self.record.setEnabled(True)
        self.hotkey_label.setText(pretty(HOTKEY))
        self.stack.setCurrentIndex(index)
        last = index == self.stack.count() - 1
        self.back.setVisible(index > 0)
        self.skip.setVisible(not last)           # the last page's own button closes it
        self.next.setText("Get started" if last else "Next")
        for i, dot in enumerate(self.dots):
            dot.setStyleSheet("border-radius: 4px; background: %s;"
                              % (ACCENT if i == index else "#3A3D46"))
        self._refresh_status()
        self._redraw()
        if index == self.look_page:
            self._show_look()

    def _show_look(self):
        """Show the real pop-up, and the screen glow if it is on, so the choices
        on the look page can be seen for real. Nothing is recorded."""
        try:
            self.dictation.preview_popup()
            if SCREEN_GLOW:
                self.dictation.demo_screen_glow()
        except Exception as exc:
            print("Preview could not be shown:", exc, file=sys.stderr)

    def _open_settings(self):
        self.close()                             # finishes the introduction
        open_settings(self.dictation)

    def _next(self):
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
        else:
            self.try_label.setText("Try it out: click in the box, " + how % pretty(HOTKEY)
                                   + ".")
            self.try_box.setPlaceholderText("Your words will be typed here")

    def _refresh_status(self):
        self._refresh_try()
        loading = self.dictation.state == "loading"
        progress = getattr(self.dictation, "download_words", lambda: "")()
        if self.dictation.state == "nomodel":
            self.status.setText("There is no speech model on this PC yet, so dictation "
                                "is off. Put the files in place and start again, or "
                                "download it from Settings, on the General tab.")
        elif loading and getattr(self.dictation, "download_stuck", False):
            self.status.setText("The download is not moving. A firewall, ad blocker or "
                                "DNS filter may be blocking huggingface.co or addresses "
                                "ending in hf.co. Allow them, or start again and choose "
                                "Manual download.")
        elif loading and progress:
            self.status.setText("%s. You can finish here; dictation works as soon as it "
                                "is done." % progress)
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
        if self.mic_note.isVisible() != bool(on):
            self.mic_note.setVisible(bool(on))
            self.mic_help.setVisible(bool(on))

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
                              % (ACCENT if i == index else "#3A3D46"))
        self._redraw()
        self._show_look()                        # the real thing, in the new colour

    def _glow_toggled(self, on):
        set_option("SCREEN_GLOW", "screen_glow", bool(on))
        self._redraw()
        self._show_look()

    def _pill_toggled(self, on):
        set_option("ALWAYS_SHOW", "always_show", bool(on))
        self.dictation.refresh_popup()

    def closeEvent(self, event):
        # Finishing and closing the window are the same thing: nothing here
        # has to be completed, and every choice made so far is already saved.
        try:
            self.hear.stop()
            if not self.finished_up:
                self.finished_up = True
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
            self.close()
            return
        super().keyPressEvent(event)


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
        tabs = QtWidgets.QTabWidget()
        tabs.setDocumentMode(False)
        outer.addWidget(tabs)
        self.swatches = []
        page = {"layout": None}          # the tab currently being filled

        def tab(title):
            holder = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(holder)
            layout.setContentsMargins(16, 14, 16, 14)
            layout.setSpacing(10)
            tabs.addTab(holder, title)
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

        def field(text):
            label = QtWidgets.QLabel(text)
            label.setObjectName("field")
            label.setFixedWidth(84)              # one column, so controls line up
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
                label.setContentsMargins(26, 0, 0, 2)
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
        self.mode.addItem("Hold to talk", True)
        self.mode.addItem("Toggle mode", False)
        self.mode.setCurrentIndex(0 if HOLD_TO_TALK else 1)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        row(self.record, self.hotkey, self.mode, None)
        self.hotkey_note = note("")
        self._refresh_hotkey_note()

        heading("Text")
        check("Add a space after each dictation", ADD_SPACE,
              option("ADD_SPACE", "add_space"),
              hint="So two dictations in a row don't run together.")
        self.after = QtWidgets.QComboBox()
        for key, label in AFTER_CHOICES:
            self.after.addItem(label, key)
        self.after.setCurrentIndex(max(0, self.after.findData(AFTER_KEY)))
        self.after.currentIndexChanged.connect(
            lambda index: set_option("AFTER_KEY", "after_key", self.after.itemData(index)))
        self.after_delay = QtWidgets.QComboBox()
        for seconds, label in AFTER_DELAYS:
            self.after_delay.addItem(label, seconds)
        self.after_delay.setCurrentIndex(max(0, self.after_delay.findData(AFTER_DELAY)))
        self.after_delay.currentIndexChanged.connect(
            lambda index: set_option("AFTER_DELAY", "after_delay",
                                     int(self.after_delay.itemData(index))))
        after_label = QtWidgets.QLabel("After each dictation:")
        after_label.setObjectName("field")
        after_label.setFixedWidth(138)
        self.after.setMinimumWidth(160)
        row(after_label, self.after, None)
        wait_label = QtWidgets.QLabel("When:")
        wait_label.setObjectName("field")
        wait_label.setFixedWidth(138)            # lines up with the row above
        self.after_delay.setMinimumWidth(160)
        row(wait_label, self.after_delay, None)
        note("Press a key after the dictation ends. Enter sends a chat message. With a "
             "wait, a notice counts it in, and clicking that notice cancels it.")

        # A second hotkey: dictate as usual, then press a key. For when you only
        # sometimes want that (say, to send a chat message straight away).
        action_title = QtWidgets.QLabel("Action hotkey:")
        action_title.setObjectName("field")
        action_title.setFixedWidth(138)
        self.action_label = QtWidgets.QLabel("")
        self.action_label.setObjectName("info")
        self.action_record = button(self.RECORD_LABEL, lambda: record_action(), width=84)
        self.action_clear = button("Clear", lambda: clear_action(), width=84)
        self.action_key = QtWidgets.QComboBox()
        for key, label in AFTER_CHOICES:
            if key:
                self.action_key.addItem("then " + label, key)
        self.action_key.setCurrentIndex(max(0, self.action_key.findData(ACTION_KEY)))
        self.action_key.currentIndexChanged.connect(
            lambda index: set_option("ACTION_KEY", "action_key",
                                     self.action_key.itemData(index)))
        self.action_key.setMinimumWidth(160)

        def show_action():
            self.action_label.setText(pretty(ACTION_HOTKEY) if ACTION_HOTKEY else "not set")
            self.action_clear.setEnabled(bool(ACTION_HOTKEY))
            self.action_key.setEnabled(bool(ACTION_HOTKEY))

        def action_recorded(hotkey):
            self.action_record.setText(self.RECORD_LABEL)
            self.action_record.setEnabled(True)
            if hotkey and hotkey not in (HOTKEY, PASTE_HOTKEY):
                set_option("ACTION_HOTKEY", "action_hotkey", hotkey)
                dictation.apply_hotkey()
            show_action()
            if hotkey and hotkey in (HOTKEY, PASTE_HOTKEY):
                self.action_label.setText("%s is already in use" % pretty(hotkey))

        def record_action():
            self.action_record.setText("Press keys")
            self.action_record.setEnabled(False)
            self.action_label.setText("Press the keys, then let go.  Esc cancels")
            dictation.start_capture(action_recorded, main=False)

        def clear_action():
            set_option("ACTION_HOTKEY", "action_hotkey", "")
            dictation.apply_hotkey()
            show_action()

        row(action_title, self.action_record, self.action_clear, None)

        def gap():
            blank = QtWidgets.QLabel("")
            blank.setFixedWidth(138)
            return blank

        row(gap(), self.action_label, None)      # its own row: never cut off
        row(gap(), self.action_key, None)
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

        heading("Speech model")
        self.model = QtWidgets.QComboBox()
        for name, label, _quant, _size in MODELS:
            self.model.addItem(label, name)
        if self.model.findData(MODEL) < 0:
            self.model.addItem(MODEL, MODEL)     # one set by hand in the file
        self.model.setCurrentIndex(max(0, self.model.findData(MODEL)))
        self.model.currentIndexChanged.connect(self._model_changed)
        row(self.model, None)
        note("Runs entirely on this PC. Choosing a model you haven't used before "
             "restarts the application and downloads it straight away.")

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
            self.model_button.setText("Remove the model from this PC" if model_present()
                                      else "Download the model")

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
                if report.get("cache_freed"):
                    lines.append("Also cleared the download library's own cache, which "
                                 "held a second copy: about %d MB." % report["cache_freed"])
                if report.get("left"):
                    lines.append("Windows is still holding some of the files open. They "
                                 "will be deleted the next time the application starts:"
                                 "\n%s" % "\n".join(report["left"]))
                elif not report.get("removed"):
                    lines.append("No model folder was found.")
                lines.append("Dictation is off until the model is downloaded again. To do "
                             "that, click \"Download the model\" here, or just start the "
                             "application again later: it asks before downloading.")
                dialog(self, "Speech model removed", "The speech model has been removed",
                       "\n\n".join(lines))

        row(button("Check the model's files", lambda: model_check_window(self)),
            button("Open its folder", open_model_folder), None)
        self.model_button = button("Remove the model from this PC", remove_or_download)
        row(self.model_button, None)
        show_model_button()
        note("To remove it by hand instead, quit the application and delete that "
             "folder. Nothing else on this PC depends on it.")
        end_tab()

        # ================= Pop-up =================
        tab("Pop-up")
        def screen_glow_changed(on):
            set_option("SCREEN_GLOW", "screen_glow", bool(on))
            if on:
                dictation.demo_screen_glow()     # show what it looks like

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
        check("Always keep a small pill on screen", ALWAYS_SHOW,
              option("ALWAYS_SHOW", "always_show", refresh=True),
              tip="Shows a small pill between dictations so you can see the app is "
                  "running. The microphone stays off until you press the hotkey.")
        check("Show the hotkey text", SHOW_HOTKEY_HINT,
              option("SHOW_HOTKEY_HINT", "hotkey_hint", refresh=True))
        heading("Position")
        def reset_popup():
            dictation.overlay.reset_position()
            set_option("CARD_WIDTH", "card_width", 0)    # and its usual width
            dictation.refresh_popup()

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
            choice = QtWidgets.QPushButton(label)
            choice.clicked.connect(lambda _checked=False, v=value: self._background_chosen(v))
            self.backgrounds.append((choice, value))
            background_row.append(choice)
        row(*(background_row + [button("Custom", self._background_custom), None]))
        self._paint_backgrounds()
        heading("Glow")
        self.glow = QtWidgets.QComboBox()
        for key, label, _amount in GLOW_LEVELS:
            self.glow.addItem(label, key)
        self.glow.setCurrentIndex(max(0, self.glow.findData(GLOW)))
        self.glow.currentIndexChanged.connect(self._glow_changed)
        row(field("Glow"), self.glow, None)
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
                picked = self._pick_colour("Glow colour", GLOW_COLOUR or ACCENT, try_out)
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
            dictation.preview_popup()

        show_glow_source()
        self.glow_colour.currentIndexChanged.connect(glow_colour_changed)
        self.glow_colour.setToolTip("Used by the glow round the pop-up and the glow "
                                    "round the screen. The app's icon colour changes "
                                    "with whichever app you are dictating into.")
        row(field("Glow colour"), self.glow_colour, None)
        check("Glow around the edge of the screen", SCREEN_GLOW, screen_glow_changed,
              hint="A soft glow round the whole screen while you dictate, which "
                   "swells with your voice.")

        def glow_slider(label, name, key, low, high, value):
            slider = QtWidgets.QSlider(Qt.Horizontal)
            slider.setRange(low, high)
            slider.setValue(int(value))
            slider.setFixedWidth(200)

            def moved(_value=None):              # shows the result as you drag
                globals()[name] = int(slider.value())
                if not slider.isSliderDown():    # arrow keys, a click on the track
                    save_setting(key, int(slider.value()))
                if SCREEN_GLOW:
                    dictation.demo_screen_glow()

            slider.valueChanged.connect(moved)
            slider.sliderReleased.connect(
                lambda: set_option(name, key, int(slider.value())))
            row(field(label), slider, None)
            return slider

        self.screen_strength = glow_slider("Strength", "SCREEN_GLOW_STRENGTH",
                                           "screen_glow_strength", 5, 100,
                                           SCREEN_GLOW_STRENGTH)
        self.screen_size = glow_slider("Size", "SCREEN_GLOW_SIZE", "screen_glow_size",
                                       20, 200, SCREEN_GLOW_SIZE)
        heading("Outline")
        check("Outline in the accent colour", ACCENT_OUTLINE,
              option("ACCENT_OUTLINE", "accent_outline", refresh=True, preview=True))
        check("Moving light on the outline", OUTLINE_LIGHT,
              option("OUTLINE_LIGHT", "outline_light", refresh=True, preview=True))
        heading("Tray icon")
        check("Tray icon interacts with your dictation", TRAY_LIVE,
              option("TRAY_LIVE", "tray_live"),
              hint="To keep the icon always visible, drag it out of the hidden "
                   "icons onto the taskbar.")
        end_tab()

        # ================= Audio =================
        tab("Audio")
        heading("Microphone", first=True)
        self.mic = QtWidgets.QComboBox()
        self.mic.setFixedWidth(250)
        self._fill_microphones()
        self.mic.currentIndexChanged.connect(self._mic_changed)
        row(field("Input"), self.mic, button("Refresh", self._fill_microphones), None)
        self.mic_test = button("Test", self._test_mic, width=84)
        self.mic_bar = QtWidgets.QProgressBar()
        self.mic_bar.setRange(0, 100)
        self.mic_bar.setTextVisible(False)
        self.mic_bar.setFixedWidth(160)
        self.mic_note = QtWidgets.QLabel("")
        self.mic_note.setObjectName("info")
        row(field("Level"), self.mic_test, self.mic_bar, self.mic_note, None)
        self.mic_heard = QtWidgets.QLabel("")    # the words heard during a test
        self.mic_heard.setWordWrap(True)
        self.mic_heard.setMinimumHeight(34)
        self.mic_heard.setContentsMargins(2, 0, 0, 0)
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
        check("Play a sound on start and finish", SOUNDS, set_sounds)
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
        for which, label in (("start", "Start file"), ("finish", "Finish file")):
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

        # ================= Privacy =================
        tab("Privacy")
        self.paste_mode = check(
            "Paste using the clipboard instead of typing", not TYPE_TEXT,
            self._paste_mode_changed,
            tip="Off by default. The words are normally typed as keystrokes, so "
                "the clipboard is never touched.\n\n"
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
                "you have finished. They stay in this program's memory only: never "
                "on disk, never left on the clipboard." % minutes,
            hint="Keeps your last dictation for %d minutes so you can paste it again "
                 "if needed." % minutes)
        def indent():                            # lines these rows up under the tick-box text
            gap = QtWidgets.QLabel("")
            gap.setFixedWidth(18)
            return gap

        self.paste_title = QtWidgets.QLabel("Paste again hotkey:")
        self.paste_title.setObjectName("field")
        self.paste_label = QtWidgets.QLabel("")
        row(indent(), self.paste_title, self.paste_label, None)
        self.paste_record = button(self.RECORD_LABEL, self._record_paste_hotkey, width=84)
        self.paste_clear = button("Clear", self._clear_paste_hotkey, width=84)
        row(indent(), self.paste_record, self.paste_clear, None)
        self._refresh_paste_row()
        self.updates = check("Check for model updates at startup (goes online)",
                             CHECK_UPDATES, self._updates_changed,
                             tip="With this ticked, the application goes online each "
                                 "time it starts and checks whether a newer version "
                                 "of the speech model is available, so you always "
                                 "have the most up-to-date one. A newer version is "
                                 "downloaded automatically.\n\nLeft unticked, the "
                                 "application does not go online for updates and "
                                 "keeps using the model already on this PC.")
        self.update_note = QtWidgets.QLabel("")
        self.update_note.setObjectName("info")
        self.update_note.setWordWrap(True)
        self.update_button = button("Check for a model update now", self._check_update_now)
        self.update_button.setToolTip("Goes online once, now, after asking. Nothing is "
                                      "checked at startup unless the box above is ticked.")
        row(indent(), self.update_button, None)
        page["layout"].addWidget(self.update_note)
        self._update_work = {"result": None}
        self._update_poll = QtCore.QTimer(self)
        self._update_poll.setInterval(200)
        self._update_poll.timeout.connect(self._update_checked)
        quant = " (%s)" % QUANTIZATION if QUANTIZATION else ""
        note("Model: %s%s\nNetwork: %s" % (MODEL, quant, network_words()))
        row(button("Export settings", lambda: export_settings_window(self)),
            button("Import settings", lambda: import_settings_window(self)), None)
        row(button("Open data folder", open_data_folder),
            button("About", lambda: show_about(dictation)), None)
        end_tab()
        # Open on the tab that was showing last time (remembered only while
        # the application is running; nothing is written to disk for this).
        tabs.setCurrentIndex(min(_settings_open.get("tab", 0), tabs.count() - 1))
        tabs.currentChanged.connect(lambda index: _settings_open.update(tab=int(index)))

        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(button("Reset all settings", self._reset_all))

        def quit_app():
            print("Quit from the settings window.")
            leave_now(QtWidgets.QApplication.instance())

        bottom.addStretch(1)                     # Reset on the left; the two ways out on the right
        bottom.addWidget(button("Quit %s" % APP_TITLE, quit_app))
        bottom.addWidget(button("Close", self.close, width=84))
        outer.addLayout(bottom)

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
            chosen = value.lower() == POPUP_BG.lower()
            button.setStyleSheet("border: 1px solid %s;" % (ACCENT if chosen else "#34363E"))

    def _background_chosen(self, colour):
        set_option("POPUP_BG", "popup_bg", colour)
        self._paint_backgrounds()
        self.dictation.overlay.update()
        self.dictation.preview_popup()

    def _pick_colour(self, title, start, try_out):
        """Open a colour picker that shows each colour on the pop-up as you move
        through them. Returns the chosen colour's name, or None if cancelled
        (the caller's try_out is then called with the starting colour again)."""
        picker = QtWidgets.QColorDialog(QtGui.QColor(start), self)
        picker.setWindowTitle(title)
        picker.setOption(QtWidgets.QColorDialog.DontUseNativeDialog, True)   # live updates
        picker.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        overlay = self.dictation.overlay
        can_show = self.dictation.state in ("idle", "loading", "nomodel") and overlay.mode != "welcome"
        if can_show:
            overlay.preview(ms=None)             # stays up for as long as you are choosing
        picker.currentColorChanged.connect(
            lambda colour: (try_out(colour.name()), overlay.update()))
        accepted = picker.exec() == QtWidgets.QDialog.Accepted
        chosen = picker.selectedColor().name() if accepted else None
        if not accepted:
            try_out(start)
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
        chosen = self._pick_colour("Accent colour", ACCENT, try_out)
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
                            "and connect to huggingface.co once to download it (%s). That "
                            "is a third-party website that we do not run or control. "
                            "After that it works offline again."
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
        self.dictation.preview_popup()

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

    # -- check for a model update, once, now -----------------------------------------
    def _check_update_now(self):
        if not model_files():
            self.update_note.setText("The speech model has not been downloaded yet.")
            return
        if not dialog(self, "Go online to check?", "This application will go online",
                      "It will connect to huggingface.co once, now, to see whether a "
                      "newer version of the speech model has been published. That is a "
                      "third-party website that we do not run or control.\n\nNothing "
                      "about you or your files is sent, and nothing is downloaded unless "
                      "you then choose to update.",
                      accept="Check now", reject="Cancel"):
            return
        self.update_button.setEnabled(False)
        self.update_note.setText("Checking\u2026")
        self._update_work["result"] = None

        def work():
            published, why = fetch_published_fingerprints()
            mine = {}
            if published is not None:
                for name, path in model_files().items():
                    if published.get(name):          # only files that have one published
                        try:
                            mine[name] = file_fingerprint(path)
                        except Exception:
                            mine[name] = ""
            self._update_work["result"] = (published, why, mine)

        threading.Thread(target=work, daemon=True).start()
        self._update_poll.start()

    def _update_checked(self):
        if self._update_work["result"] is None:
            return
        self._update_poll.stop()
        self.update_button.setEnabled(True)
        published, why, mine = self._update_work["result"]
        if published is None:
            self.update_note.setText("The website could not be reached, so nothing was "
                                     "checked. (%s)" % why)
            return
        differs = [name for name, mark in mine.items()
                   if mark.lower() != str(published.get(name, "")).lower()]
        if not mine:
            self.update_note.setText("The website lists no fingerprints to compare with, "
                                     "so it is not possible to tell.")
        elif not differs:
            self.update_note.setText("You have the latest version of this speech model.")
        elif local_model_dir():
            self.update_note.setText(
                "A different version of this model is published. You downloaded yours "
                "by hand, so to update, download the files again from the model's page "
                "and replace the ones in your model folder.")
        else:
            self.update_note.setText("A newer version of this speech model is published.")
            size = dict((entry[0], entry[3]) for entry in MODELS).get(MODEL, "several hundred MB")
            if dialog(self, "Update the speech model?", "This application will go online",
                      "A newer version of the speech model is available.\n\nThe "
                      "application will restart and connect to huggingface.co once more "
                      "to download it (%s). That is a third-party website that we do "
                      "not run or control. After that it is offline again, as before."
                      % size,
                      accept="Update and restart", reject="Not now"):
                save_setting("update_once", True)
                restart_app()

    def _updates_changed(self, on):
        if not on:
            set_check_updates(False)
            return
        if dialog(self, "Go online at startup?", "This application will go online",
                  "With this setting enabled, the application will connect to "
                  "huggingface.co every time it starts, to check for updates to the "
                  "speech model, and download a newer version if there is one. That is "
                  "a third-party website that we do not run or control.",
                  accept="Turn on", reject="Keep offline"):
            set_check_updates(True)
        else:                                    # untick it again, quietly
            self.updates.blockSignals(True)
            self.updates.setChecked(False)
            self.updates.blockSignals(False)
            set_check_updates(False)

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
                  "They are never written to disk and never left on the clipboard, and "
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


class TrayTip(QtWidgets.QWidget):
    """A small card by the clock, shown once: where Settings live. It stays
    until "Got it" is clicked (so it is known to have been seen), and is never
    shown again after that. Drawn in the pop-up's own colours."""

    ARROW = 9                                    # the little pointer at the tray icon

    def __init__(self, tray, on_done):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle(APP_TITLE)
        self.on_done = on_done
        # Always dark, like Settings and the other windows (not the pop-up's
        # own colour, which may be light).
        self.theme = {"bg": QtGui.QColor(27, 28, 33, 250),
                      "border": QtGui.QColor(255, 255, 255, 30),
                      "text": QtGui.QColor(240, 240, 244),
                      "text2": QtGui.QColor(185, 187, 198)}
        self.arrow_x = None                      # where the pointer goes, if anywhere
        self.arrow_down = True
        accent = QtGui.QColor(ACCENT)
        text, muted = self.theme["text"].name(), self.theme["text2"].name()
        dark_on_accent = "#101114" if is_light(accent) else "#FFFFFF"

        # One row: icon, words, button. Even padding all round.
        row = QtWidgets.QHBoxLayout(self)
        row.setSpacing(10)
        icon = QtWidgets.QLabel()
        icon.setPixmap(app_icon().pixmap(20, 20))
        icon.setStyleSheet("background: none;")
        row.addWidget(icon, 0, Qt.AlignVCenter)
        title = QtWidgets.QLabel("Settings are accessible via the tray icon")
        title.setStyleSheet("color: %s; font-size: 13px; background: none;" % text)
        row.addWidget(title, 0, Qt.AlignVCenter)
        row.addSpacing(6)
        ok = QtWidgets.QPushButton("Got it")
        ok.setCursor(Qt.PointingHandCursor)
        ok.setStyleSheet(
            "QPushButton { background: %s; color: %s; border: none; border-radius: 6px;"
            " padding: 5px 14px; font-size: 12px; font-weight: 600; }"
            "QPushButton:hover { background: %s; }"
            % (accent.name(), dark_on_accent, accent.lighter(112).name()))
        ok.clicked.connect(self.done_with)
        row.addWidget(ok, 0, Qt.AlignVCenter)
        self.pad("below")
        self.place(tray)

    def pad(self, arrow):
        """Even padding round the row, plus room for the pointer on the side it
        sits ("below" the card, "above" it, or None)."""
        p, side = 12, 14
        self.layout().setContentsMargins(side, p + (self.ARROW if arrow == "above" else 0),
                                         side - 2, p + (self.ARROW if arrow == "below" else 0))
        self.adjustSize()

    def place(self, tray):
        """Just above (or below) the tray icon when Windows says where it is;
        otherwise in the corner by the clock."""
        screen = QtGui.QGuiApplication.primaryScreen()
        icon = QtCore.QRect()
        try:
            icon = tray.geometry() if tray is not None else QtCore.QRect()
            if icon.isValid() and not icon.isEmpty():
                screen = QtGui.QGuiApplication.screenAt(icon.center()) or screen
        except Exception:
            icon = QtCore.QRect()
        room = screen.availableGeometry()
        gap = 10
        if icon.isValid() and not icon.isEmpty() and screen.geometry().contains(icon.center()):
            self.arrow_down = icon.center().y() > room.center().y()     # taskbar at the bottom
            x = icon.center().x() - self.width() // 2
            x = max(room.left() + gap, min(x, room.right() - self.width() - gap))
            if self.arrow_down:
                y = room.bottom() - self.height() - 2
            else:
                y = room.top() + 2
                self.pad("above")
            self.arrow_x = icon.center().x() - x
        else:                                    # the icon is out of sight: the corner
            x = room.right() - self.width() - 16
            y = room.bottom() - self.height() - 16
            self.arrow_x = None
            self.pad(None)
        self.move(x, y)

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        card = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.arrow_x is not None:
            if self.arrow_down:
                card.setBottom(card.bottom() - self.ARROW)
            else:
                card.setTop(card.top() + self.ARROW)
        path = QtGui.QPainterPath()
        path.addRoundedRect(card, 12, 12)
        if self.arrow_x is not None:
            ax = max(card.left() + 18, min(float(self.arrow_x), card.right() - 18))
            pointer = QtGui.QPainterPath()
            if self.arrow_down:
                pointer.moveTo(ax - self.ARROW, card.bottom() - 1)
                pointer.lineTo(ax, card.bottom() + self.ARROW)
                pointer.lineTo(ax + self.ARROW, card.bottom() - 1)
            else:
                pointer.moveTo(ax - self.ARROW, card.top() + 1)
                pointer.lineTo(ax, card.top() - self.ARROW)
                pointer.lineTo(ax + self.ARROW, card.top() + 1)
            pointer.closeSubpath()
            path = path.united(pointer)
        p.setPen(QtGui.QPen(self.theme["border"], 1))
        p.setBrush(self.theme["bg"])
        p.drawPath(path)
        p.end()

    def done_with(self, _checked=False):
        save_setting("tray_tip_seen", True)      # seen: never shown again
        self.hide()
        self.deleteLater()
        if self.on_done:
            self.on_done()


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
        "NVIDIA's Parakeet model; the audio is not saved or sent anywhere.",
        "",
        "Model:  %s%s" % (MODEL, quant),
        "Stored in:  %s" % (folder or "not downloaded yet"),
    ]
    if megabytes:
        lines.append("Size on disk:  %d MB" % megabytes)
    if dictation.load_seconds is not None:
        lines.append("Loaded in:  %.1f seconds" % dictation.load_seconds)
    elif dictation.state == "loading":
        lines.append("Still loadingâ€¦")
    lines += [
        "Network:  %s" % network_words(),
        "Model update check:  %s" % ("on (takes effect at the next start)"
                                     if CHECK_UPDATES else "off"),
        "",
        "Hotkey:  %s  (%s)" % (pretty(HOTKEY),
                               "hold to talk" if HOLD_TO_TALK else "toggle mode"),
        "Tip:  tap the hotkey to paste your last dictation again." if KEEP_LAST else "",
        "Settings file:  %s" % _CONFIG_FILE,
        "Log file:  %s" % os.path.join(_DATA, "dictate.log"),
    ]
    dialog(None, "About %s" % APP_TITLE, "%s %s" % (APP_TITLE, VERSION),
           "\n".join(line for line in lines[2:]))


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
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("%s.Offline" % APP_TITLE)
    except Exception:
        pass
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")                             # same look on every PC
    app.setPalette(dark_palette())                     # dark menus and dialogs
    app.setWindowIcon(app_icon())
    # Listen for a newer copy asking this one to close from the very start, so
    # it works even while a question is on screen (before, this began later,
    # and a copy waiting on a question could not be closed or even seen).
    quit_watch = watch_for_quit_request(app)           # noqa: F841 (kept alive)
    if not network_is_open():
        verify_network_block()                         # proves the guard works, offline
    finish_pending_removal()
    print("Model on this PC at start: %s | marked as downloaded: %s | download cache: %s"
          % ([(folder, folder_megabytes(folder)) for folder in model_folders()] or "none",
             _setup_done(),
             ("%d MB" % folder_megabytes(download_cache_folder()))
             if os.path.isdir(download_cache_folder()) else "none"))
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
    look_chosen = False
    if not _config.get("welcomed"):
        # A first launch (or a replay of one): choose the look before anything
        # else, so the download card and the rest already wear it.
        try:
            first_look_window()
            look_chosen = True
        except Exception as exc:
            print("The first screen could not be shown:", exc, file=sys.stderr)
    agreed_in_settings = DOWNLOAD_AGREED_ONCE     # read (and used up) at start
    needed =not (_setup_done() or local_model_dir()) and not agreed_in_settings
    if needed or replay:
        size = dict((entry[0], entry[3]) for entry in MODELS).get(MODEL, "several hundred MB")
        agreed = dialog(
            None, "%s: one-time download" % APP_TITLE, "This application will go online once",
            "%(app)s turns your speech into text on this PC. To do that it needs a "
            "speech model, %(model)s (%(size)s), which is not on this PC yet. With "
            "it comes a small helper that splits long dictations at pauses "
            "(Silero, about 2 MB).\n\n"
            "If you continue, %(app)s connects to huggingface.co once to download "
            "them. That is a third-party website that we do not run or control. "
            "After that %(app)s works without an internet connection, and does not "
            "connect again unless you agree to something that says it will.\n\n"
            "The downloaded files are checked against their known fingerprints "
            "before they are used, and again on this PC every time %(app)s starts, "
            "so a changed or damaged file is caught.\n\n"
            "Your voice is turned into text by the model on this PC. %(app)s does "
            "not send your voice or your words anywhere."
            % dict(app=APP_TITLE, model=model_label(MODEL), size=size),
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
    if tray is not None and not dictation.intro_open:
        # Not seen yet (an earlier version, or "Got it" was never clicked).
        QtCore.QTimer.singleShot(3000, dictation.maybe_show_tray_tip)
    intro = None
    if dictation.intro_open:                           # first launch (or a replay of it)
        intro = IntroWindow(dictation, look_chosen=look_chosen)   # noqa: F841 (kept alive)
        intro.show()
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
