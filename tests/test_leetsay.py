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
import unittest
import urllib.request
from importlib.machinery import SourceFileLoader

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "leetsay.pyw")

# Never the real %LOCALAPPDATA%\leetsay: settings, sounds and the log go here.
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

    def test_line_from_a_mac(self):
        """A line made on a Mac (assumed to use the same format): the shared
        settings apply; Mac-only settings and Mac-style hotkeys are left out."""
        good, careful, dropped, problem = ls.read_settings_line(settings_line({
            "accent": "#3DDC97", "remove_fillers": False, "spoken_breaks": True,
            "hotkey": "cmd+shift+space", "mac_menu_bar_icon": True,
            "launch_at_login": True}))
        self.assertEqual(problem, "")
        self.assertEqual(careful, {})
        self.assertEqual(good, {"accent": "#3DDC97", "remove_fillers": False,
                                "spoken_breaks": True})
        self.assertEqual(dropped, 3)

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


import unittest.mock  # noqa: E402  (used above, loaded after leetsay on purpose)

if __name__ == "__main__":
    unittest.main()
