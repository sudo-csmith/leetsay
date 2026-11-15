# Privacy

This page describes how leetsay handles information. leetsay is free,
open-source software that runs on your own PC. Its author does not run any
server or service for it and does not receive, collect or store any
information about you or your use of it.

This page describes how the software is designed to behave. It is not a
contract and gives no guarantee; leetsay is provided "as is", without warranty
of any kind (see [LICENSE](LICENSE) and the Disclaimer in the
[README](README.md)). You can check every statement here yourself by reading
the source code.

## What the developer collects

Nothing. leetsay has no accounts, no analytics, no telemetry, no crash
reporting, no advertising and no servers of its own. The developer has no way
to see who uses it or what they do with it.

## Your voice and your words

- Speech is turned into text on your PC by a speech model running on your PC.
  leetsay is designed not to send audio or text anywhere and not to save audio
  to disk. Audio is held in memory while you dictate and released afterwards.
- The microphone is opened only while you dictate, and while you use the
  microphone test in Settings.
- The text is typed into the window you are using. leetsay does not write it to
  disk or to its log.
- Once the text is typed into another program (a chat, an email, a web page, a
  document), that program has it, and its own privacy terms apply. leetsay
  cannot see or control what another program does with your text. If that
  program is online, the text may leave your PC through it.
- **Missed text safety** (off by default): if you turn it on, your last
  dictation is kept in memory for 5 minutes so you can paste it again.
- **Paste using the clipboard** (off by default): if you turn it on, the text
  is placed on the clipboard for about a second to paste it, then your previous
  clipboard is restored. The text is marked so that Windows keeps it out of
  clipboard history and clipboard sync, but other programs that read the
  clipboard during that second could see it.
- To notice its hotkey, leetsay listens to the keyboard while it runs, as any
  program with a global hotkey must. It does not record or store key presses.
- **Type as you speak** (off by default): if you turn it on, your words are
  typed into the window you started in while you are still speaking. They are
  kept in memory only until the dictation ends (longer only if **Missed text
  safety** is on).
- **Text box detection** (on by default, used only by Type as you speak): to
  type only into a text box, leetsay asks Windows' accessibility service (the
  one screen readers use) what kind of control has the keyboard focus: a text
  box, a password box, a button or a page, and whether it can be typed into.
  It does not read the text in it, the page, the window's title or the
  program's name. The answer is used on the spot and is not stored, logged or
  sent anywhere. Turn it off and Windows is not asked at all.
- **Word replacements** (the ones you add in Settings) are kept in your
  settings file on this PC. They are not included when you export your
  settings, and are never sent anywhere.
- For the glow along the bottom of the screen, leetsay reads one Windows
  setting (whether the taskbar is see-through) to place the glow behind it.
  It changes nothing in Windows.
- To show a red status light when no microphone is connected, leetsay asks
  Windows every few seconds how many microphones there are, while its pop-up
  is showing. This does not open or listen to any microphone.

## The network

- While it runs, leetsay blocks its own network access, except as described
  below. It checks this block each time it starts; the check does not leave
  your PC. The result is shown in Settings, on the Privacy tab.
- It connects to the internet only after you agree in a window that says
  **"This application will go online"**, and only to `github.com` (and
  `release-assets.githubusercontent.com`, GitHub's server that delivers the
  files), a third-party website that we do not run or control. It downloads
  from this project's model page there. That happens:
  - the first time you start it, to download the speech model (about 660 MB)
    and a small helper for long dictations (about 2 MB);
  - if you choose another speech model in Settings, or download one again;
  - if a new version of leetsay comes with a newer speech model, and you
    agree to download it.
- **Checking for app updates** (off by default): if you turn on **Check for
  app updates at startup**, or click **Check for app updates now**, a small
  separate helper asks `github.com`, a third-party website that we do not run
  or control, for the version number of the latest published release.
  leetsay itself stays blocked from the network. Nothing is downloaded or
  installed: if there is a newer version, you are told and can open its
  download page yourself. Each switch-on, and each manual check, asks first.
- When it connects, standard technical information that any download involves
  (such as your IP address and the files requested) reaches GitHub and the
  networks in between. That is handled under GitHub's own privacy policy, not
  this one. leetsay sends no account details or identifiers of its own.
- When the download or check is finished, the network block is restored.
- Your agreement is not remembered: each of these asks again.

## What is stored on your PC

In `%LOCALAPPDATA%\leetsay`:

- `dictate.json`: your settings, and a note of when the speech model's files
  were last checked against their fingerprints.
- `dictate.log`: a short technical log of the current session (start-up, model
  loading, network check). It is designed not to contain anything you said or
  the names of programs you use, and is started afresh each time.
- `sounds\`: the start and finish tones, generated on your PC.
- `models\`: the speech model and the helper for long dictations.

If you turn on **Start with Windows**, one entry is added to your Windows
start-up list. These files stay on your PC; keeping your PC secure is up to you.

## Third parties

- **GitHub** (where leetsay and its speech models are published) is a
  third-party website that we do not run or control. Its own terms and
  privacy policy apply when you use it.
- The speech model and libraries leetsay uses are made by others; see
  [CREDITS.md](CREDITS.md).

## Children

leetsay collects nothing from anyone, children included: there are no
accounts, and nothing is sent to us.

## Reporting a problem

The "Report a problem" and "Suggest an idea" buttons open GitHub's issue forms
in your browser. Anything you write there, and any log you choose to attach,
is **public** on GitHub, under GitHub's own terms and privacy policy. Read a log
before you attach it. (leetsay's log is designed not to contain your dictated text, and
your Windows user name is replaced with `%USERPROFILE%`.) Security problems
should be reported privately instead; see [SECURITY.md](SECURITY.md).

## Removing everything

See "Uninstalling" in the [README](README.md).

## Changes

This page may change when leetsay changes. The current version is always the
one in this repository.
