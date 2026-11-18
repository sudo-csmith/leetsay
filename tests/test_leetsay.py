"""Tests for leetsay's plain logic. They need no microphone, no network and no
speech model, and never touch the real settings folder: leetsay is loaded with
its data folder pointed at a temporary one.

Run:  py -3.13 -B -m unittest discover -s tests -v
"""
import ast
import base64
import ctypes
import hashlib
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import types
import unittest
import urllib.request
from importlib.machinery import SourceFileLoader

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "leetsay.pyw")

# Never the real %LOCALAPPDATA%\leetsay: settings, sounds and the log go here.
_REAL_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")   # only to find the helper
_DATA = tempfile.mkdtemp(prefix="leetsay-tests-")
import atexit  # noqa: E402
import shutil  # noqa: E402
atexit.register(shutil.rmtree, _DATA, True)       # removed again when the tests end
os.environ["LOCALAPPDATA"] = _DATA
os.environ["APPDATA"] = _DATA

_spec = importlib.util.spec_from_loader("leetsay", SourceFileLoader("leetsay", SOURCE))
ls = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ls)


def text_of(sentence):
    """What leetsay would type for what the speech model heard."""
    return ls.join_words(ls.clean_words(sentence.split()))


class Settings:
    """Change some of leetsay's settings for one test, then put them back."""

    def __init__(self, **values):
        self.values, self.saved = values, {}

    def __enter__(self):
        for name, value in self.values.items():
            self.saved[name] = getattr(ls, name)
            setattr(ls, name, value)

    def __exit__(self, *_exc):
        for name, value in self.saved.items():
            setattr(ls, name, value)


class TestDataFolder(unittest.TestCase):
    def test_uses_the_temporary_folder(self):
        self.assertTrue(os.path.normcase(ls._DATA).startswith(os.path.normcase(_DATA)))


class TestWordReplacements(unittest.TestCase):
    def test_built_in_fixes(self):
        self.assertEqual(text_of("I use leaked say every day."), "I use leetsay every day.")
        self.assertEqual(text_of("Leet say, please."), "Leetsay, please.")   # a capital at the start stays

    def test_own_rules_whole_words_and_punctuation(self):
        with Settings(REPLACEMENTS=[["jason", "JSON"], ["open a i", "OpenAI"]]):
            self.assertEqual(ls.replace_words("Parse the Jason, then".split()),
                             "Parse the JSON, then".split())
            self.assertEqual(ls.replace_words("ask open a i.".split()), ["ask", "OpenAI."])
            self.assertEqual(ls.replace_words("Jasonville stays".split()),
                             "Jasonville stays".split())

    def test_no_rules(self):
        with Settings(REPLACEMENTS=[]):
            self.assertEqual(ls.replace_words(["leaked", "say"]), ["leaked", "say"])


class TestFillerWords(unittest.TestCase):
    def test_removed_and_tidied(self):
        with Settings(REMOVE_FILLERS=True):
            self.assertEqual(" ".join(ls.drop_fillers("Um, it seems fine.".split())),
                             "It seems fine.")
            self.assertEqual(" ".join(ls.drop_fillers("so, um, this works".split())),
                             "so, this works")
            self.assertEqual(" ".join(ls.drop_fillers("right now um.".split())), "right now.")

    def test_meant_words_stay(self):
        with Settings(REMOVE_FILLERS=True):
            words = "I like it, you know.".split()
            self.assertEqual(ls.drop_fillers(words), words)

    def test_switched_off(self):
        with Settings(REMOVE_FILLERS=False):
            words = "Um, it seems fine.".split()
            self.assertEqual(ls.drop_fillers(words), words)


class TestNewLine(unittest.TestCase):
    def test_spoken_breaks(self):
        with Settings(SPOKEN_BREAKS=True):
            self.assertEqual(text_of("Hi Tom. New line. Can we meet at three?"),
                             "Hi Tom.\nCan we meet at three?")
            self.assertEqual(text_of("Dear team, new line, thanks for today."),
                             "Dear team,\nThanks for today.")
            self.assertEqual(text_of("First point. New paragraph. Second point."),
                             "First point.\n\nSecond point.")
            self.assertEqual(text_of("New line. Hi."), "\nHi.")

    def test_ordinary_sentences_stay_words(self):
        with Settings(SPOKEN_BREAKS=True):
            for sentence in ("I bought a new line of credit.", "Is this new? Line two.",
                             "I need a new line.", "The new paragraph is fine.",
                             "Hello new line"):
                self.assertEqual(text_of(sentence), sentence)

    def test_switched_off(self):
        with Settings(SPOKEN_BREAKS=False):
            self.assertEqual(text_of("Hi. New line. Bye."), "Hi. New line. Bye.")

    def test_terminals_get_a_space(self):
        with unittest.mock.patch.object(ls, "is_terminal", return_value=True):
            self.assertEqual(ls.no_breaks_in_terminal("ls\nrm", 1), "ls rm")
        with unittest.mock.patch.object(ls, "is_terminal", return_value=False):
            self.assertEqual(ls.no_breaks_in_terminal("a\nb", 1), "a\nb")


def settings_line(data):
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return ls._PORTABLE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class TestSettingsExportImport(unittest.TestCase):
    def setUp(self):
        self.saved = dict(ls._config)

    def tearDown(self):
        ls._config.clear()
        ls._config.update(self.saved)

    def test_round_trip(self):
        ls._config.update({"accent": "#123456", "remove_fillers": False, "live_lines": 3,
                           "last_word_ms": 600})
        good, careful, dropped, problem = ls.read_settings_line(ls.export_settings_line())
        self.assertEqual(problem, "")
        self.assertEqual(careful, {})
        for key, value in {"accent": "#123456", "remove_fillers": False, "live_lines": 3,
                           "last_word_ms": 600}.items():
            self.assertEqual(good[key], value)

    def test_never_exported(self):
        ls._config.update({"mic": "My microphone", "model": "nemo-parakeet-tdt-0.6b-v3",
                           "sound_device": "Speakers"})
        line = ls.export_settings_line()
        body = line[len(ls._PORTABLE_PREFIX):]
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        for key in ("mic", "model", "sound_device", "model_files", "responsibility_ok"):
            self.assertNotIn(key, data)

    def test_bad_lines_refused(self):
        for line in ("", "hello", ls._PORTABLE_PREFIX + "!!!not-base64",
                     ls._PORTABLE_PREFIX + "x" * 9000, settings_line(["a list"])):
            good, careful, _dropped, problem = ls.read_settings_line(line)
            self.assertTrue(problem, line[:40])
            self.assertEqual((good, careful), ({}, {}))

    def test_bad_values_dropped(self):
        good, _careful, dropped, problem = ls.read_settings_line(settings_line({
            "accent": "red", "live_lines": 99, "remove_fillers": "yes",
            "hotkey": "a", "last_word_ms": 5, "volume": 0.5}))
        self.assertEqual(problem, "")
        self.assertEqual(good, {"volume": 0.5})
        self.assertEqual(dropped, 5)

    def test_careful_settings_need_a_yes(self):
        ls._config.update({"keep_last": False, "check_app_updates": False,
                           "textbox_only": True, "type_text": True})
        good, careful, _dropped, _problem = ls.read_settings_line(settings_line({
            "keep_last": True, "check_app_updates": True, "textbox_only": False,
            "type_text": False, "after_key": "enter"}))
        for key in ("keep_last", "check_app_updates", "textbox_only", "type_text",
                    "after_key"):
            self.assertIn(key, careful)
            self.assertNotIn(key, good)

    # A real line exported by the Mac version (it exports only changed settings).
    MAC_LINE = settings_line({"accent": "#4DA3FF", "after_key": "cmd+enter",
                              "hold_to_talk": False, "hotkey": "right_command",
                              "live_type": True, "max_seconds": 600, "sounds": False})

    def test_line_from_a_mac(self):
        """The shared settings apply; the Mac's own key names ("right_command",
        "cmd+enter") are not valid on Windows and are left out, never applied."""
        body = self.MAC_LINE[len(ls._PORTABLE_PREFIX):]
        self.assertEqual(json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
                         ["hotkey"], "right_command")
        good, careful, dropped, problem = ls.read_settings_line(self.MAC_LINE)
        self.assertEqual(problem, "")
        self.assertEqual(careful, {})
        self.assertEqual(good, {"accent": "#4DA3FF", "hold_to_talk": False,
                                "live_type": True, "max_seconds": 600, "sounds": False})
        self.assertEqual(dropped, 2)              # hotkey and after_key

    def test_empty_line_from_a_mac(self):
        """The Mac's line with nothing changed: "leetsay-settings-1:e30" ({})."""
        self.assertEqual(ls.read_settings_line("leetsay-settings-1:e30"), ({}, {}, 0, ""))

    def test_hotkeys_cannot_clash(self):
        ls._config.update({"hotkey": "ctrl+space"})
        good, _careful, _dropped, _problem = ls.read_settings_line(settings_line({
            "paste_hotkey": "ctrl+space"}))
        self.assertNotIn("paste_hotkey", good)


class TestModelHashCheck(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(dir=_DATA)
        self.files = {}
        for name, content in (("encoder.onnx", b"enc" * 1000), ("vocab.txt", b"a\nb\n")):
            with open(os.path.join(self.folder, name), "wb") as fh:
                fh.write(content)
            self.files[name] = (len(content), hashlib.sha256(content).hexdigest())

    def check(self, key="test|1"):
        return ls.check_files(self.folder, self.files, key)

    def test_matching_files_pass(self):
        self.assertEqual(self.check(), [])
        self.assertEqual(self.check(), [])       # again, from the saved record

    def test_changed_byte_fails(self):
        self.assertEqual(self.check(), [])
        path = os.path.join(self.folder, "encoder.onnx")
        with open(path, "r+b") as fh:
            fh.write(b"X")                      # same size, different content
        stat = os.stat(path)
        os.utime(path, (stat.st_atime, stat.st_mtime + 10))
        self.assertEqual(self.check(), ["encoder.onnx"])

    def test_missing_and_wrong_size_fail(self):
        os.remove(os.path.join(self.folder, "vocab.txt"))
        with open(os.path.join(self.folder, "encoder.onnx"), "ab") as fh:
            fh.write(b"extra")
        self.assertEqual(sorted(self.check("test|2")), ["encoder.onnx", "vocab.txt"])

    def test_built_in_fingerprints_look_right(self):
        for model, releases in ls.MODEL_RELEASES.items():
            self.assertTrue(releases, model)
            for release in releases:
                self.assertTrue(release["files"], model)
                for name, (size, sha) in release["files"].items():
                    self.assertGreater(size, 0, name)
                    self.assertRegex(sha, r"^[0-9a-f]{64}$", name)
        for name, _label, _quant, _size in ls.MODELS:
            self.assertIn(name, ls.MODEL_RELEASES)


class TestNetworkBlock(unittest.TestCase):
    def tearDown(self):
        ls.close_network("test")

    def test_block_verified(self):
        self.assertFalse(ls.network_is_open())
        self.assertTrue(ls.verify_network_block())

    def test_connect_and_lookup_refused(self):
        with self.assertRaises(ls.NetworkBlocked):
            socket.create_connection(("127.0.0.1", 9), timeout=0.2)
        with self.assertRaises(ls.NetworkBlocked):
            socket.getaddrinfo("localhost", 80)
        with self.assertRaises(ls.NetworkBlocked):
            urllib.request.urlopen("http://127.0.0.1:9/", timeout=0.2)

    @unittest.skipUnless(os.name == "nt", "Windows network libraries")
    def test_network_libraries_refused(self):
        with self.assertRaises(ls.NetworkBlocked):
            ctypes.WinDLL("winhttp")

    def test_open_only_towards_github(self):
        ls.open_network("test")
        self.assertTrue(ls._host_allowed("github.com"))
        self.assertTrue(ls._host_allowed("release-assets.githubusercontent.com"))
        self.assertFalse(ls._host_allowed("example.com"))
        self.assertFalse(ls._host_allowed("github.com.example.com"))
        with self.assertRaises(ls.NetworkBlocked):
            socket.getaddrinfo("example.com", 443)          # refused before any lookup
        with self.assertRaises(ls.NetworkBlocked):
            socket.create_connection(("127.0.0.1", 9), timeout=0.2)   # not a looked-up address

    def test_closes_again(self):
        ls.open_network("test")
        ls.close_network("test")
        self.assertFalse(ls.network_is_open())
        with self.assertRaises(ls.NetworkBlocked):
            socket.getaddrinfo("github.com", 443)


class TestLogPrivacy(unittest.TestCase):
    def test_home_folder_hidden(self):
        home = os.path.expanduser("~")
        out = io.StringIO()
        log = ls._PrivateLog(out)
        log.write("Model in %s\\leetsay and %s\n" % (home, home.replace("\\", "/")))
        self.assertNotIn(home, out.getvalue())
        self.assertIn("%USERPROFILE%", out.getvalue())

    def test_no_log_line_prints_dictated_text(self):
        """Every print() in leetsay (the log) is checked: none may use a value
        that holds dictated words. Counts (len(...)) are allowed."""
        text_names = {"text", "words", "heard", "typed", "part", "piece", "transcript",
                      "cleaned", "sentence", "reading", "again", "last_text", "lt_done",
                      "lt_pending", "pending", "fresh", "coming", "lt_all", "lt_held"}
        tree = ast.parse(open(SOURCE, encoding="utf-8").read())
        found = []

        def scan(node, line):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "len"):
                return
            name = node.id if isinstance(node, ast.Name) else (
                node.attr if isinstance(node, ast.Attribute) else None)
            if name in text_names:
                found.append((line, name))
            for child in ast.iter_child_nodes(node):
                scan(child, line)

        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "print"):
                for arg in node.args:
                    scan(arg, node.lineno)
        self.assertEqual(found, [], "log lines that may contain dictated text")


class TestUpdateCheck(unittest.TestCase):
    """The installer link offered by "Check for app updates", from fake
    release replies as GitHub sends them."""
    REPO = "https://github.com/sudo-csmith/leetsay/releases/download/"

    def offered(self, assets):
        return ls.pick_installer(assets) if ls.safe_release_link(
            ls.pick_installer(assets), ls.INSTALLER_LINK_START) else ""

    @staticmethod
    def asset(name, link):
        return {"name": name, "browser_download_url": link}

    def test_plain_name(self):
        link = self.REPO + "v0.2.4/leetsay-setup.exe"
        self.assertEqual(self.offered([self.asset("SHA256SUMS.txt", self.REPO + "v0.2.4/SHA256SUMS.txt"),
                                       self.asset("leetsay-setup.exe", link)]), link)

    def test_old_versioned_name(self):
        link = self.REPO + "v0.2.1/leetsay-setup-0.2.1.exe"
        self.assertEqual(self.offered([self.asset("leetsay-setup-0.2.1.exe", link)]), link)

    def test_plain_name_preferred(self):
        plain = self.REPO + "v0.3.0/leetsay-setup.exe"
        self.assertEqual(self.offered([
            self.asset("leetsay-setup-0.3.0.exe", self.REPO + "v0.3.0/leetsay-setup-0.3.0.exe"),
            self.asset("leetsay-setup.exe", plain)]), plain)

    def test_links_elsewhere_refused(self):
        for link in ("https://example.com/leetsay-setup.exe",
                     "https://github.com/someone-else/leetsay/releases/download/v1/leetsay-setup.exe",
                     "http://github.com/sudo-csmith/leetsay/releases/download/v1/leetsay-setup.exe",
                     self.REPO + "../../../evil/leetsay-setup.exe",
                     self.REPO + "v1/%2e%2e/leetsay-setup.exe",
                     self.REPO + "v1/leetsay-setup.exe?x=1",
                     "https://github.com@example.com/sudo-csmith/leetsay/releases/download/v1/leetsay-setup.exe"):
            self.assertEqual(self.offered([self.asset("leetsay-setup.exe", link)]), "", link)

    def test_nothing_suitable(self):
        self.assertEqual(self.offered([self.asset("SHA256SUMS.txt", self.REPO + "v1/SHA256SUMS.txt")]), "")
        self.assertEqual(self.offered(None), "")
        self.assertEqual(self.offered(["not a dict"]), "")


class TestSameTextBox(unittest.TestCase):
    """Words go only into the text box the dictation started in: a box that
    grows a line is still the same one; another box in the same window is
    not; a dictation going to a pinned box is not judged this way."""

    def judge(self, start, now, pinned=False):
        holder = types.SimpleNamespace(start_rect=start, pin_used=pinned)
        return ls.Dictation._box_moved(holder, now)

    def test_same_box(self):
        start = (100, 500, 700, 540)
        self.assertFalse(self.judge(start, start))
        self.assertFalse(self.judge(start, (100, 480, 700, 540)))   # grew upwards
        self.assertFalse(self.judge(start, (100, 500, 700, 580)))   # grew downwards
        self.assertFalse(self.judge(start, (104, 503, 696, 538)))   # a few pixels off

    def test_another_box(self):
        start = (100, 500, 700, 540)
        self.assertTrue(self.judge(start, (100, 200, 700, 240)))    # a box higher up
        self.assertTrue(self.judge(start, (800, 500, 1200, 540)))   # a box beside it

    def test_not_judged(self):
        self.assertFalse(self.judge(None, (1, 2, 3, 4)))             # didn't start in a box
        self.assertFalse(self.judge((100, 500, 700, 540), None))     # place unknown
        self.assertFalse(self.judge((100, 500, 700, 540), (0, 0, 9, 9), pinned=True))


class TestNoRepeatedPhrase(unittest.TestCase):
    """Type as you speak: the first words of a new piece that only repeat the
    end of what was just typed (the context read again) are not typed twice;
    a short echo ("no, no") and new words are kept."""

    def skip(self, typed, words):
        holder = types.SimpleNamespace(lt_done=typed, lt_pending="",
                                       REPEAT_MIN=ls.Dictation.REPEAT_MIN,
                                       _plain=ls.Dictation._plain)
        return ls.Dictation._skip_repeat(holder, words)

    def test_whole_phrase_read_again(self):
        typed = "I only said it once and just put it twice for some reason."
        words = "and just put it twice for some reason, so I'm not sure.".split()
        self.assertEqual(self.skip(typed, words), 8)

    def test_end_read_again(self):
        self.assertEqual(self.skip("Hello there for some reason.",
                                   "for some reason it fails".split()), 3)

    def test_new_words_kept(self):
        self.assertEqual(self.skip("Hello there.", "How are you".split()), 0)
        self.assertEqual(self.skip("I said no", "no, it is fine".split()), 0)
        self.assertEqual(self.skip("", "one two three".split()), 0)


class TestSpeechDecision(unittest.TestCase):
    """The rule: nothing is dropped without the pop-up saying so, short sound
    without speech is not typed (today's click-and-cough rule), and longer
    sound without detected speech is read anyway."""

    def test_rule(self):
        quiet = ls.MIC_QUIET_PEAK / 2
        loud = ls.MIC_QUIET_PEAK * 20
        self.assertEqual(ls.speech_decision(True, quiet, 0.5), "read")
        self.assertEqual(ls.speech_decision(True, loud, 10), "read")
        self.assertEqual(ls.speech_decision(False, quiet, 10), "quiet")
        self.assertEqual(ls.speech_decision(False, quiet, 1), "quiet")
        self.assertEqual(ls.speech_decision(False, loud, 1.5), "noise")
        self.assertEqual(ls.speech_decision(False, loud, 2.0), "read")
        self.assertEqual(ls.speech_decision(False, loud, 30), "read")


def _helper_dir():
    """The voice detector (Silero VAD, about 2 MB): from LEETSAY_HELPER_DIR (the
    GitHub test run downloads it there), else leetsay's own copy on this PC.
    Used only if every file matches the fingerprints built into leetsay."""
    for folder in (os.environ.get("LEETSAY_HELPER_DIR", ""),
                   os.path.join(_REAL_LOCALAPPDATA, "leetsay", "models", "silero-vad")):
        if folder and os.path.isdir(folder):
            for name, (size, sha) in ls.HELPER_RELEASE["files"].items():
                path = os.path.join(folder, name)
                if not os.path.isfile(path) or os.path.getsize(path) != size \
                        or ls.file_fingerprint(path) != sha:
                    raise AssertionError("the helper in %s does not match its fingerprint"
                                         % folder)
            return folder
    return None


_SENTENCES = ["Please send the report to the team before lunch tomorrow.",
              "Can we move the meeting to three o'clock on Friday?",
              "The new version fixes the bug in the settings window.",
              "I think we should test this again on the other computer.",
              "Thanks for your help today, it made a big difference.",
              "Remind me to call the office about the delivery."]


def _spoken_clips(folder):
    """Six spoken sentences made with Windows' built-in voices (two voices,
    three speeds), as 16 kHz mono WAV files in folder. Nothing is committed:
    no real person's voice is stored in the repository."""
    import subprocess
    script = ["Add-Type -AssemblyName System.Speech",
              "$v = (New-Object System.Speech.Synthesis.SpeechSynthesizer)"
              ".GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }",
              "if (-not $v) { exit 3 }"]
    fmt = ("(New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
           "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
           "[System.Speech.AudioFormat.AudioChannel]::Mono))")
    for i, sentence in enumerate(_SENTENCES):
        script += ["$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
                   "$s.SelectVoice(@($v)[%d %% @($v).Count])" % i,
                   "$s.Rate = %d" % (-3, 0, 3)[i % 3],
                   "$s.SetOutputToWaveFile('%s', %s)"
                   % (os.path.join(folder, "clip%d.wav" % i), fmt),
                   "$s.Speak('%s')" % sentence.replace("'", "''"),
                   "$s.Dispose()"]
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                             "; ".join(script)], capture_output=True, timeout=120)
    if result.returncode != 0:
        return []
    clips = []
    import wave
    import numpy as np
    for i in range(len(_SENTENCES)):
        with wave.open(os.path.join(folder, "clip%d.wav" % i)) as fh:
            raw = fh.readframes(fh.getnframes())
        clips.append(np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0)
    return clips


@unittest.skipUnless(os.name == "nt", "Windows voices")
class TestSpeechCheck(unittest.TestCase):
    """The voice detector on six spoken sentences at four microphone levels,
    and on four kinds of noise."""
    LEVELS = (1.0, 0.1, 0.02, 0.005)

    @classmethod
    def setUpClass(cls):
        import numpy as np
        cls.np = np
        folder = _helper_dir()
        if folder is None:
            raise unittest.SkipTest("the voice detector (helper) is not on this PC")
        import onnx_asr
        cls.vad = onnx_asr.load_vad("silero", folder)
        cls.clips = _spoken_clips(tempfile.mkdtemp(dir=_DATA))
        cls.rng = np.random.default_rng(1)

    def outcome(self, raw):
        """What leetsay does with a recording: as in Dictation._transcribe."""
        found = ls.has_speech(self.vad, ls.leveled(raw))
        peak = float(self.np.max(self.np.abs(raw))) if len(raw) else 0.0
        return found, ls.speech_decision(found, peak, len(raw) / 16000.0)

    def test_speech_at_every_level(self):
        if not self.clips:
            self.skipTest("no Windows voices on this PC to make the spoken sentences")
        self.assertEqual(len(self.clips), 6)
        for n, clip in enumerate(self.clips):
            for level in self.LEVELS:
                raw = (clip * level + self.rng.normal(0, 0.0005, len(clip))).astype(
                    self.np.float32)                          # a little microphone hiss
                found, decision = self.outcome(raw)
                if level >= 0.02:
                    self.assertTrue(found, "sentence %d at level %s" % (n, level))
                # Never dropped silently: read, or "Can't hear you" on the pop-up.
                self.assertIn(decision, ("read", "quiet"), "sentence %d at %s" % (n, level))

    def test_noise_is_not_speech(self):
        np, rng, rate = self.np, self.rng, 16000
        noises = {
            "room hiss": rng.normal(0, 0.002, 10 * rate),
            "silence": np.zeros(10 * rate),
            "key clicks": np.where(rng.random(3 * rate) < 0.0005, 0.6, 0.0)
            + rng.normal(0, 0.001, 3 * rate),
            "hum": 0.05 * np.sin(2 * np.pi * 50 * np.arange(5 * rate) / rate),
        }
        for name, noise in noises.items():
            found, _decision = self.outcome(noise.astype(np.float32))
            self.assertFalse(found, name)
            # A short bit of the same sound (a click, a cough): nothing typed,
            # and the pop-up says so ("noise" or "quiet"), never "read".
            short = noise[:int(1.5 * rate)].astype(np.float32)
            _found, decision = self.outcome(short)
            self.assertIn(decision, ("noise", "quiet"), name)


import unittest.mock  # noqa: E402  (used above, loaded after leetsay on purpose)

if __name__ == "__main__":
    unittest.main()
