"""Privacy guard for this repository.

Refuses to let personal details or private files reach GitHub.

  python tools/privacy_check.py pre-push   (run by the local pre-push hook)
      Checks every commit about to be pushed: its message, the names of the
      files it changes and the content of those files (text or binary).
  python tools/privacy_check.py ci         (run by the GitHub tests)
      Checks every tracked file and every commit message on this branch.

Always checked (no private data needed):
  - home folder paths such as C:\\Users\\<someone>\\ (placeholders such as
    %USERPROFILE% or <name> are fine);
  - notes, handoff and assistant files, logs, recordings, local settings, and
    the private word list itself.

Also checked in pre-push mode, when the file exists: every word or phrase in
.privacy-blocklist (one per line, any capitals; lines starting with # are
ignored). That file is listed in .gitignore and is never uploaded.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCKLIST = os.path.join(ROOT, ".privacy-blocklist")
ZERO = "0" * 40

# Files that must never be committed (matched against the file's name).
PRIVATE_FILES = re.compile(
    r"(?i)(^|/)(claude[^/]*\.md|memory\.md|notes[^/]*\.md|start-here[^/]*|[^/]*handoff[^/]*"
    r"|dictate\.(json|log|pos|pid|ready)|\.privacy-blocklist|\.env|[^/]*\.env"
    r"|[^/]*\.(log|wav|mp3|m4a|flac|ogg|webm|aac))$"
    r"|(^|/)(\.claude|notes|handoff|recordings|shots|scratch)/")

# A real home folder: C:\Users\name\ or C:/Users/name/ (also with doubled
# backslashes, as in code). Placeholders and Windows' shared folders are fine.
HOME_PATH = re.compile(
    rb"(?i)[a-z]:(?:\\\\|\\|/)users(?:\\\\|\\|/)"
    rb"(?!public\b|default\b|all users\b|%|<|\{|\$|\.\.\.|name\b|username\b|you\b)"
    rb"[a-z0-9._-]{1,40}(?:\\\\|\\|/)")


def git(*args, binary=False):
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout
    return out if binary else out.decode("utf-8", "replace")


def private_words():
    if not os.path.isfile(BLOCKLIST):
        return []
    words = []
    with open(BLOCKLIST, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                words.append(line.lower())
    return words


def find_words(data, words):
    """Words from the private list found in data (bytes), in UTF-8 or UTF-16."""
    low = data.lower()
    hits = []
    for word in words:
        if word.encode("utf-8") in low or word.encode("utf-16-le") in low:
            hits.append(word)
    return hits


def check_blob(label, data, words, problems):
    if HOME_PATH.search(data):
        problems.append("%s: contains a home folder path" % label)
    hits = find_words(data, words)
    if hits:
        # Never print the private words themselves (the output may be shared).
        problems.append("%s: contains %d word(s) from the private list" % (label, len(hits)))


def check_commit(sha, words, problems):
    short = sha[:7]
    message = git("log", "-1", "--format=%B", sha)
    check_blob("commit %s message" % short, message.encode("utf-8"), words, problems)
    author = git("log", "-1", "--format=%an <%ae> | %cn <%ce>", sha)
    check_blob("commit %s author" % short, author.encode("utf-8"), words, problems)
    changed = git("diff-tree", "--no-commit-id", "-r", "--root", "--name-status", sha)
    for line in changed.splitlines():
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        if PRIVATE_FILES.search(path):
            problems.append("commit %s adds or changes a private file: %s" % (short, path))
        check_blob("commit %s file name %s" % (short, path), path.encode("utf-8"), words, problems)
        if not status.startswith("D"):
            check_blob("commit %s file %s" % (short, path),
                       git("show", "%s:%s" % (sha, path), binary=True), words, problems)


def commits_to_push():
    """The commits a push would send, from what git gives the hook on stdin."""
    shas = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO:
            continue                              # a branch or tag being deleted
        local, remote = parts[1], parts[3]
        if remote == ZERO:
            found = git("rev-list", local, "--not", "--remotes")
        else:
            found = git("rev-list", "%s..%s" % (remote, local))
        shas += [s for s in found.split() if s not in shas]
    return shas


def main(mode):
    problems = []
    if mode == "pre-push":
        words = private_words()
        if not words:
            print("privacy check: note: no .privacy-blocklist on this PC, only the "
                  "public rules are checked")
        shas = commits_to_push()
        for sha in shas:
            check_commit(sha, words, problems)
        checked = "%d commit(s)" % len(shas)
    elif mode == "ci":
        files = git("ls-files").splitlines()
        for path in files:
            if PRIVATE_FILES.search(path):
                problems.append("tracked private file: %s" % path)
            full = os.path.join(ROOT, path)
            if os.path.isfile(full):
                with open(full, "rb") as fh:
                    check_blob(path, fh.read(), [], problems)
        messages = git("log", "--format=%H%x00%B%x01")
        for entry in messages.split("\x01"):
            if "\x00" in entry:
                sha, body = entry.strip().split("\x00", 1)
                check_blob("commit %s message" % sha[:7], body.encode("utf-8"), [], problems)
        checked = "%d tracked file(s) and the commit messages" % len(files)
    else:
        print(__doc__)
        return 2
    if problems:
        print("privacy check FAILED (%s):" % checked)
        for problem in problems:
            print("  - " + problem)
        print("Nothing was pushed. Remove the personal detail or private file, then "
              "amend the commit and push again.")
        return 1
    print("privacy check passed (%s)" % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
