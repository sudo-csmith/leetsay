# Security

leetsay is a free, volunteer-maintained project, provided "as is" without
warranty of any kind (see [LICENSE](LICENSE)). Security reports are welcome and
are looked at on a best-effort basis; no response time, fix or support is
promised.

## Supported versions

Only the latest version in this repository is maintained. Please update before
reporting a problem.

## Reporting a problem

If you find a security problem, please report it privately with GitHub's
**Report a vulnerability** button on this repository's **Security** tab,
rather than in a public issue. Include what you found, how to reproduce it,
and the leetsay version (shown in the title of the Settings window).

Please give a reasonable amount of time for a fix before describing the
problem publicly. There is no bug bounty.

## How leetsay is designed to protect you

- **Network blocked by design.** While it runs, leetsay blocks its own network
  access.
  The block is lifted only after you agree to a download, only towards
  `huggingface.co`, and is restored when the download is done. It is checked
  each time the program starts.
- **Checked model files.** The speech model's files are compared with known
  SHA-256 fingerprints before they are used; if they do not match, leetsay asks
  before using them.
- **No code from beside the program.** leetsay loads only its installed
  libraries, never stray Python files in the same folder.
- **Careful with other programs.** When a new copy replaces an old one that
  will not close, only that copy is ended, after checking that it is leetsay.
- **Checked settings lines.** A settings line pasted into Import settings can
  only set known options to valid values. Options that use the clipboard, keep
  text or go online need your agreement.

## Limits

- The network block works inside Python. The speech engine, the window library
  and the audio library are compiled code that the block cannot inspect. They
  are not used to go online, but only a firewall can enforce that at the level
  of Windows.
- To notice its hotkey, leetsay listens to the keyboard while it runs. Key
  presses are not stored or sent.
- Windows does not allow leetsay to type into programs running as administrator
  (unless leetsay also runs as administrator). leetsay says so instead.
- No software is free of bugs. Use leetsay at your own risk.
