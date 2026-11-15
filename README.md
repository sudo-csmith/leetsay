# leetsay

Free, offline dictation for Windows. Hold a hotkey, speak, let go: your words
are typed into whatever program you are using.

Your speech is turned into text on your own PC by NVIDIA's Parakeet speech
model. leetsay is designed so that what you say stays on your PC and is not
saved, and it has no accounts, statistics or tracking. See
[PRIVACY.md](PRIVACY.md).

leetsay is free software, provided as is, without any warranty. Please read
the [Disclaimer](#disclaimer) before using it.

## What you need

- Windows 10 or 11, 64-bit.
- About 1 GB of free memory while it runs, and about 700 MB of disk space for
  the speech model.
- A microphone.

## Installing

### With Python

1. Install [Python 3.13](https://www.python.org/downloads/) (64-bit). During
   setup, tick **Add python.exe to PATH**. Please note: python.org is a
   third-party website that we do not run or control.
2. Download this repository (the green **Code** button, then **Download ZIP**)
   and unzip it to a folder of its own, for example `Documents\leetsay`.
   Keep it out of your Downloads folder.
3. Open that folder, click in the address bar, type `cmd` and press Enter.
   In the window that opens, run:

   ```
   py -3.13 -m pip install -r requirements.txt
   ```

4. Double-click `leetsay.pyw` to start it.

## The first time

1. Choose a colour and a look.
2. leetsay asks before it downloads the speech model (about 660 MB) from
   huggingface.co, a third-party website that we do not run or control. Choose
   **Download**, or **Manual download** to fetch the files yourself with your
   web browser (the steps are shown), or **Quit**.
3. A short introduction shows the hotkey and lets you try it.

After that, leetsay starts in the background (with Windows, if you like) and
shows a small icon by the clock. Windows may hide new icons behind the **^**
arrow by the clock; you can drag the icon out next to the clock to keep it in
sight.

## Using it

- **Hold Ctrl+Space, speak, let go.** The text is typed where your cursor is.
  You can change the hotkey, or switch to "press once to start, again to
  stop", in Settings.
- **Settings:** click the leetsay icon by the clock. Starting leetsay again
  while it is running also opens Settings.
- **Cancel** a dictation: hover over the pop-up and click it.
- **Quit:** right-click the icon by the clock and choose **Quit**.

Good to know:

- Ctrl+Space is also used by some code editors (for suggestions) and by
  Chinese and Japanese keyboards (to switch input). While leetsay runs, it
  takes that key. If you need it, choose another hotkey in Settings.
- Windows does not let leetsay type into programs running as administrator.
  leetsay tells you when that happens.
- To move your settings to another PC, use **Export settings** and **Import
  settings** on the Privacy tab.

## Checking that it is offline

- In Settings, on the **Privacy** tab, the **Network** line says *blocked for
  this program (offline, checked at start)*.
- leetsay checks its network block every time it starts. The result is the
  second line of `%LOCALAPPDATA%\leetsay\dictate.log`:
  `Network block check: verified, all network access is refused`.
- You can also watch it yourself: open **Resource Monitor** (press Windows+R,
  type `resmon`, press Enter), go to the **Network** tab, and look for
  `pythonw.exe` running leetsay under **Network Activity**. It should not
  appear there.

leetsay goes online only after you agree in a window that says "This
application will go online", and only to download or update the speech model.

## Uninstalling

1. In Settings, on the **General** tab, choose **Remove the model from this
   PC**. This deletes the speech model (about 630 MB).
2. Untick **Start with Windows** (General tab), then quit leetsay (right-click
   the icon by the clock, **Quit**).
3. Delete the folder `%LOCALAPPDATA%\leetsay` (paste that into the File
   Explorer address bar to open it). It holds your settings and the log.
4. Delete the folder you put leetsay in.

The Python packages can be removed with
`py -3.13 -m pip uninstall -r requirements.txt`.

## Disclaimer

- **No warranty.** leetsay is free, open-source software, provided "as is",
  without warranty of any kind, express or implied. In no event shall the
  author be liable for any claim, damages or other liability arising from it
  or its use. The full terms are in the [LICENSE](LICENSE). You use leetsay
  entirely at your own risk.
- **Speech recognition makes mistakes.** Always check text before you send or
  submit it, especially if you set leetsay to press Enter after a dictation.
  Do not rely on leetsay where a wrong, missing or misplaced word could cause
  harm or loss, such as medical, legal, financial, safety or emergency use.
- **Your responsibility.** You are responsible for how you use leetsay and for
  what it types on your behalf, including following any laws or rules about
  recording or processing speech that apply where you are.
- **Third parties.** The speech models, libraries and websites leetsay uses
  belong to others and come with their own licences and terms (see
  [CREDITS.md](CREDITS.md)). Websites mentioned here are third-party websites
  that we do not run or control.
- **No affiliation.** leetsay is an independent project. It is not affiliated
  with, endorsed or sponsored by NVIDIA, Hugging Face, Microsoft or the Python
  Software Foundation. NVIDIA, Parakeet, Hugging Face, Windows and Python are
  trademarks of their respective owners, used here only to describe what
  leetsay works with.
- **No support obligation.** Help, fixes and updates are offered when possible,
  with no promise of any.

## Licence and credits

leetsay is released under the MIT licence, see [LICENSE](LICENSE). The speech
models are by NVIDIA under CC-BY-4.0; see [CREDITS.md](CREDITS.md) for these
and the libraries leetsay uses.

Security problems: see [SECURITY.md](SECURITY.md).
