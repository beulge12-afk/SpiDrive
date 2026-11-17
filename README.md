# SpiDrive

**Turn your Telegram account into a personal cloud drive on Windows.**

SpiDrive stores your files in a private Telegram channel that only you can see, and shows them as a normal
drive in File Explorer, in a web dashboard, and in folders that stay in sync with your PC. Telegram has no
fixed storage limit, so neither does SpiDrive. Everything runs on your own computer: no server, no account
with us, and your encryption key never leaves your device.

![SpiDrive dashboard](screenshot.png)

## Features

- **A drive in File Explorer.** Drag and drop files and folders, open, rename and delete them like on any disk.
- **Web dashboard.** Browse, search and upload. Photos, music and videos stream straight from Telegram, with
  seeking, without downloading the whole file first.
- **Two-way sync.** A folder on your PC stays identical to a folder on the drive, including renames.
  Deleted files go to a trash on both sides.
- **From Telegram to your drive.** Anything you send or forward to your storage channel from your phone
  appears in the drive within seconds.
- **Large files.** Files bigger than Telegram's 2 GB limit are split into parts and joined again automatically.
- **AES-256 encryption (optional).** Files are encrypted on your PC before they are uploaded, and their
  parts get meaningless names (the folder messages below still list real names; turn them off to hide them).
- **Trash and earlier versions.** Deleted files wait in the Trash for 30 days, and replaced files keep
  their previous versions. Restore either from the dashboard.
- **Photos timeline.** Photos and videos grouped by month, with small previews, so browsing doesn't
  download full-size images.
- **Select many files** to move, delete, keep on this PC, or download as one ZIP (folders too).
- **Keep on this PC.** Chosen files are stored locally so they open instantly and offline;
  *Free up space* removes the copies again.
- **Smart search.** Persian spelling variants (ي/ی, ك/ک, digits, half-space) match each other, and
  filters like `type:video size:>1gb after:2026-01` narrow it down.
- **Better player.** Plays the next track by itself, remembers where you stopped in a video, and shows
  subtitles from a matching `.srt` file.
- **Works where Telegram is blocked.** Connects through your VPN app or a proxy link, or finds a working
  MTProto proxy automatically, and switches when one stops working.
- **Safe by design.** Uploads resume after a disconnect, every part is checked with SHA-256, and the file
  index is backed up to your channel every day.

**Telegram-powered**

- **Weekly digest in Saved Messages,** like "42 files, 3.1 GB added, 1 failed upload, health OK, 2 files
  in the trash expiring".
- **Your folders in the channel.** A pinned **📁 My Drive** message lists your folders and files as links;
  tap a folder to open its own message. Kept up to date as you add, move and delete.
- **Phone control through Saved Messages.** Send `/find`, `/get`, `/stats` or `/keep` to yourself, and your
  PC replies with the file or answer. It works from anywhere, with no open ports.
- **Auto-organized Telegram Inbox.** Files you send from your phone are filed into folders like
  `Photos/2026-11`, `Voice` and `Docs`.
- **Share** a file with any Telegram contact, or create a **time-limited link** that opens on your Wi-Fi.

**Sync and backup**

- **Backup-only folders.** Documents and Pictures upload to the drive but never download back, and
  deleting on the PC never deletes the drive copy.
- **Upload controls.** Pause, a speed limit, and several small files uploading at once.
- **Adaptive speed.** SpiDrive watches Telegram's FloodWait answers and slows down or speeds up by itself,
  so your account never looks like abuse and a manual speed limit isn't needed.
- **Disk-aware freeing.** Files you open stay on the PC for next time; when the disk drops below 10% free,
  the least recently opened ones are freed first. Files you chose to keep on this PC always stay.
- **Duplicate finder.** Spots identical files, and uploads instantly when the content is already there.

**Security**

- **Encrypted index backup.** With a passphrase set, the daily backup is encrypted; only recent copies are kept.
- **Recovery key.** Change the passphrase without re-encrypting anything, and print a recovery key.
- **Drive health.** A background check re-verifies random files against their stored SHA-256, and the
  dashboard shows a health card.

## Installation

### Option 1: Installer (recommended)

1. Download **`SpiDrive-Setup.exe`** from the [Releases](../../releases) page and run it.
2. A setup page opens in your browser. Enter your Telegram API ID and API hash (create them for free at [my.telegram.org/apps](https://my.telegram.org/apps); the page shows how), then log in with your phone number and the code Telegram sends you.
3. Choose a storage channel, encryption and a drive letter. Done.

Windows may show *"Windows protected your PC"* the first time. Click **More info → Run anyway**.

### Option 2: Run from source

Requirements: Windows 10 or 11 and [Python 3.10+](https://www.python.org/downloads/windows/)
(tick *"Add python.exe to PATH"* during installation).

```bat
git clone https://github.com/sirvanjafari/SpiDrive.git
cd SpiDrive
SpiDrive.bat
```

`SpiDrive.bat` installs everything that is missing (Python packages, [WinFsp](https://winfsp.dev) and
[rclone](https://rclone.org) for the drive letter), then starts SpiDrive. The first run takes a few
minutes; later runs start in seconds. The setup page opens in your browser the first time.

You need your own Telegram API ID: create an app at [my.telegram.org/apps](https://my.telegram.org/apps) and
enter the **api_id** and **api_hash** on the setup page.

### Build the installer yourself

Put your `config.py` (see `config.example.py`) in the folder and run `build.bat`. It creates
`dist\SpiDrive-Setup.exe` with [PyInstaller](https://pyinstaller.org) and [Inno Setup](https://jrsoftware.org/isinfo.php).

## Usage

- **Open SpiDrive:** click its icon next to the Windows clock. Right-click it for *Open drive*, *Restart* and *Quit*.
- **Dashboard:** http://127.0.0.1:8000
- **Settings:** the gear button in the dashboard: drive letter and size, encryption, sync folders,
  connection and proxy, start with Windows.
- **Stop SpiDrive:** right-click the tray icon → **Quit SpiDrive**.

The full user guide (sync, proxy, file status, recovery, troubleshooting) is in [GUIDE.md](GUIDE.md).

## How it works

```
 File Explorer (S:)   Web dashboard   Synced folders
          \                |                /
           SpiDrive engine (runs on your PC)
     split · encrypt · SHA-256 · SQLite index · upload queue
                           |
                  MTProto (direct or proxy)
                           |
            Your private Telegram channel
```

Each file is stored as one or more messages in your private channel. A local SQLite index records which
messages belong to which file. Encryption uses AES-256-CTR with a key derived from your passphrase
(scrypt), which also allows streaming and seeking in encrypted files.

## Project structure

| File | Purpose |
|---|---|
| `app.py` | The app: tray icon, setup wizard, restarts, start with Windows |
| `drive_core.py` | Storage engine: parts, encryption, index, uploads, downloads, trash, versions, previews |
| `search.py` | Smart search: Persian spelling variants and filters |
| `thumbs.py`, `zipper.py` | Photo previews, streamed ZIP downloads |
| `webguard.py` | Stops other websites from using SpiDrive's local servers |
| `keys.py` | Master key, passphrase and recovery key, encrypted index backups |
| `phone.py` | `/find`, `/get`, `/stats`, `/keep` in Saved Messages |
| `adaptive.py`, `digest.py` | Adaptive upload speed; the weekly digest |
| `tgfolders.py` | One message per folder in the channel, under a pinned "My Drive" |
| `links.py` | Share links on the Wi-Fi (their own small server: nothing else is reachable) |
| `tests/` | Tests (`pip install pytest pytest-aiohttp`, then `pytest`) |
| `server.py` | Web dashboard and its API |
| `dav.py`, `mount.py` | The drive letter (WebDAV server + rclone/WinFsp) |
| `sync.py` | Two-way sync and backup-only folders |
| `proxy.py` | Direct, manual-proxy or automatic-proxy connection to Telegram |
| `settings.py`, `setup_server.py` | Settings and the first-run setup page |
| `drive.py` | Command-line tool |
| `web/` | Dashboard and setup pages |
| `SpiDrive.bat` | Install what is missing and start (run from source) |
| `build.bat`, `SpiDrive.iss` | Build the `.exe` and the installer |

## Security and privacy

- Your Telegram login (`*.session`), `config.py`, `app_id.py` and `settings.json` give access to your
  account and files. **Never share or upload them.** They are listed in `.gitignore`.
- If you lose your encryption passphrase and have no recovery key, encrypted files cannot be recovered.
- Share links work for anyone on the same network who has the link, until it expires. Make them short-lived
  on networks you don't trust.
- A proxy cannot read your files (Telegram traffic is encrypted), but it can see when and how much data
  you send.
- Telegram stores your data. Keep another copy of anything irreplaceable.

## Contributing

Found a bug or have an idea? Open an issue in the [Issues](../../issues) tab.
Pull requests are welcome.

## License

Copyright (C) 2026 Sirvan Jafari

SpiDrive is free software, licensed under the GNU General Public License v3.0.
You may use, study, share and improve it, but you must keep this copyright
notice and the names of the original authors, and share your changes under
the same license. See [LICENSE](LICENSE) for the full text.

## Disclaimer

SpiDrive uses the Telegram API. It is not made by, endorsed by or affiliated with Telegram. Use it in line
with [Telegram's Terms of Service](https://telegram.org/tos) and
[API Terms](https://core.telegram.org/api/terms).

Third-party software used by SpiDrive is listed in `THIRD_PARTY.txt`.
