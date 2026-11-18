<p align="center">
  <a href="https://github.com/sudo-csmith/leetsay/releases"><img src="assets/leetsay-type-as-you-speak.gif" alt="Animation of leetsay typing words into a text box while the dictation card shows them." width="100%"></a>
</p>
<p align="center"><sub>Illustration of typing as you speak (an optional setting). The real card follows your accent colour and theme.</sub></p>

Local dictation for Windows. Hold a hotkey, speak, let go: your words are
typed into whatever program you are using.

- Talk your prompts into an AI coding tool, or dictate emails, chats and documents.
- Speech is recognised on your own PC. No accounts, statistics or tracking.
- Open source, provided as is: please read the [Disclaimer](#disclaimer).

**[Download for Windows](https://github.com/sudo-csmith/leetsay/releases)** (Windows 10 or 11, 64-bit, a microphone, about 1 GB of free memory and about 900 MB of disk space, including the 660 MB speech model). A Mac version is planned.

## How it works

- **The microphone** is on only while you hold the hotkey (or, in toggle
  mode, between two presses).
- **Speech becomes text on your PC**, using NVIDIA's Parakeet speech model.
  Your audio is never sent anywhere and never saved.
- **The model is downloaded once** (about 660 MB) from this project's
  [leetsay-models](https://github.com/sudo-csmith/leetsay-models) page on
  github.com, after you agree in a window that says "This application will go
  online". Every file is checked against a fingerprint built into leetsay.
- **leetsay blocks its own network access** and opens it only for a download
  or update check you have just agreed to, only to github.com. Settings >
  Privacy shows the block; the log confirms it at every start.
- **This covers leetsay, not the programs you type into.** A chat or web page
  may send your text online; leetsay cannot see or control that.

More in [PRIVACY.md](PRIVACY.md).

## Installing

1. Download `leetsay-setup.exe` from the newest
   [release](https://github.com/sudo-csmith/leetsay/releases) and run it. It
   installs for your Windows user only; no administrator password needed.
2. If Edge says the file **isn't commonly downloaded**: **Keep** (or **Show
   more**, then **Keep anyway**).
3. If Windows says **"Windows protected your PC"**: choose **More info**, then
   **Run anyway**. leetsay is signed (from 0.2.3), but new, so Windows may warn
   until more people have downloaded it.

**Smart App Control:** versions 0.2.1 and earlier are unsigned and are
blocked by it. Releases from 0.2.3 are signed, which it should accept, but
this has not been confirmed yet on a PC with Smart App Control switched on.
Use the newest release.

**Checking the download (optional).** Each release lists a SHA-256
fingerprint. In your Downloads folder, right-click an empty spot, choose
**Open in Terminal**, run `Get-FileHash .\leetsay-setup.exe`, and compare the
code after **Hash**. If it differs, don't run the file.

**With Python instead:** install [Python 3.13](https://www.python.org/downloads/)
(python.org is a third-party website that we do not run or control), download
this repository, run `py -3.13 -m pip install -r requirements.txt` in its
folder, then double-click `leetsay.pyw`.

## Using it

- **Hold Ctrl+Space, speak, let go.** The text is typed where your cursor is.
  The hotkey and toggle mode can be changed in Settings.
- **Settings:** click the leetsay icon by the clock, or right-click the small
  pill on screen. **Quit** is at the bottom of Settings.
- **The dot on the pill** is a status light: your colour when ready, amber
  while loading, red when something needs you. Click it to see what.
- **Type as you speak** (Settings > Typing, off by default) types your words
  while you talk. It is a little less accurate, because each word is typed
  before the rest of the sentence is heard.
- **Languages:** English by default; a model for 25 European languages is in
  Settings > Model.
- **Word replacements** fix names the model doesn't know. Filler words such as
  "um" are left out by default.
- Ctrl+Space is also used by some code editors and Asian keyboard layouts;
  pick another hotkey if you need it. Windows doesn't let leetsay type into
  programs running as administrator.

## Uninstalling

**Windows Settings > Apps > Installed apps > leetsay > Uninstall**, or
**Uninstall leetsay** on leetsay's Privacy tab. It asks whether to keep your
settings or remove everything, including the speech model. With Python:
remove the model (Model tab), quit, and delete `%LOCALAPPDATA%\leetsay` and
the leetsay folder.

## Why I built this

I wanted dictation on Windows that kept my voice on my own PC, started fast,
and looked good. It's open source because I'd want to read the code of
anything that listens to my microphone. I designed and tested leetsay myself
and built it with AI coding tools.

## Disclaimer

- **No warranty.** leetsay is provided "as is", without warranty of any kind.
  The author is not liable for any claim or damages arising from it or its
  use. Full terms: [LICENSE](LICENSE). You use it at your own risk.
- **The text comes from a speech model made by others** and can contain
  mistakes, missing words or words never said. Always check it before you
  send it, especially if leetsay presses Enter for you. Do not rely on it
  where a wrong word could cause harm or loss (medical, legal, financial,
  safety or emergency use). It is not an assistive or medical device.
- **Where the text goes is up to you.** leetsay types into whichever window
  has the focus and cannot control what that program does with the text. Do
  not dictate passwords or sensitive information. People nearby can hear you
  and anyone who sees your screen can read the pop-up.
- **Your responsibility.** You are responsible for how you use leetsay,
  including laws on recording speech, consent from others who may be
  recorded, and your employer's or school's rules.
- **Third parties.** Speech models, libraries and websites used by leetsay
  belong to others and have their own licences and terms
  ([CREDITS.md](CREDITS.md)). Linked websites are third-party websites that we
  do not run or control.
- **No affiliation.** leetsay is independent and not affiliated with,
  endorsed or sponsored by NVIDIA, GitHub, Microsoft, the Qt Company, the
  Silero Team or the Python Software Foundation. Their names and trademarks
  are used only to describe what leetsay works with.
- **Official downloads** are only those on this repository's
  [Releases](https://github.com/sudo-csmith/leetsay/releases) page.
- **Name and icon.** The MIT licence covers the code, not the name "leetsay"
  or its icon. Modified copies must use a different name and must not appear
  official.
- **No support obligation.** Help and updates are offered when possible, with
  no promise; any version or download may be withdrawn.
- **Limits.** Nothing here limits liability that cannot be limited by law. If
  any part cannot be enforced, the rest still applies. The installer shows
  the full terms for each version.

## Code signing

Releases from 0.2.3 are signed with Microsoft's Azure Artifact Signing, under
the developer's identity as verified by Microsoft. Only `leetsay.exe`, the
installer, its setup helper and the uninstaller are signed, by GitHub Actions
from this repository; every release is approved by hand.

## Licence and credits

MIT licence, see [LICENSE](LICENSE). Speech models by NVIDIA under CC-BY-4.0;
these and the libraries leetsay uses are listed in [CREDITS.md](CREDITS.md).
Security problems: [SECURITY.md](SECURITY.md).
