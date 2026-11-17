# SpiDrive

Your Telegram account as a personal cloud drive on Windows. Files live in a private Telegram channel; SpiDrive shows them as drive **S:** in File Explorer, in a web dashboard, and (optionally) keeps a normal folder on your PC in two-way sync.

## What it does

- **Drive S: in File Explorer.** Drag and drop files and folders, rename, delete, open. The drive shows its own size (10 TB by default) and has no per-file limit.
- **Web dashboard** at http://127.0.0.1:8000. Browse, search, upload, and stream photos, music and video without downloading the whole file. Storage stats are in the sidebar.
- **From Telegram.** Anything you send or forward to the channel appears in **Telegram Inbox** within about 15 seconds.
- **Two-way folder sync.** A folder on your PC mirrors a folder on the drive.
- **Share.** Send any file to a Telegram contact with one click.
- **Large files** are split into parts automatically.
- **Optional AES-256 encryption** before anything leaves your PC.
- **Safe uploads.** Files are saved on your PC first, so copying finishes fast. Uploads resume after a network drop or a restart.

## Files

| File | What it is |
|---|---|
| `SpiDrive.bat` | Run from source: installs whatever is missing, then starts SpiDrive (one file for everything) |
| `config.py` | Your settings (see below). Keep it private. |
| `session.session` | Your Telegram login. **Never share it.** |
| `server.py` | Runs everything: dashboard, drive, uploads, sync, sharing |
| `drive_core.py` | Storage engine: chunks, encryption, index, upload queue |
| `dav.py` | The drive server that S: connects to |
| `mount.py` | Starts and stops rclone for S: |
| `sync.py` | Two-way folder sync |
| `drive.py` | Command line: `upload`, `download`, `ls`, `restore-index`… |
| `web/index.html` | The dashboard |
| `app.py` | The app: tray icon, setup wizard, restarts, start with Windows |
| `settings.py` | App settings in `%LOCALAPPDATA%\SpiDrive\settings.json` (replaces `config.py` in the app) |
| `setup_server.py`, `web/setup.html` | The first-run setup wizard |
| `build.bat`, `build_helper.py`, `SpiDrive.iss` | Build `SpiDrive.exe` and `SpiDrive-Setup.exe` |
| `THIRD_PARTY.txt` | Licenses of the open-source parts |
| `proxy.py` | Connects through a proxy when Telegram is blocked: finds one by itself, or uses yours |

## The app (SpiDrive.exe), the easy way

### Build the installer (once, on your PC)

1. Put all the files in one folder, together with your current `config.py`. Its API ID gets built into the app, so users don't need one of their own.
2. Optional: download the WinFsp `.msi` from https://winfsp.dev/rel/ into a folder named `installer`, so the installer can set up WinFsp too.
3. Double-click **`build.bat`**. It installs the build tools, makes `dist\SpiDrive\SpiDrive.exe`, then **`dist\SpiDrive-Setup.exe`**.

### Install and use

1. Run **SpiDrive-Setup.exe**. No admin rights are needed, except the one Windows prompt for WinFsp.
2. A setup page opens in your browser:
   - Log in with your phone number and the code Telegram sends you.
   - Choose a storage channel: create **MyDrive**, or pick one you already own.
   - Choose encryption, the drive letter, an optional sync folder, and whether SpiDrive starts with Windows.
   - **Already using SpiDrive?** The page offers to import your old folder (config, login and files).
3. SpiDrive's icon sits next to the clock. Click it to open the dashboard; right-click it for *Open drive*, *Open log*, *Restart* and *Quit*.
4. Change anything later with the **gear button** in the dashboard. Saving restarts SpiDrive by itself.

The app keeps its settings, login, index and logs in `%LOCALAPPDATA%\SpiDrive`. Uninstalling keeps that folder, so your file list is never lost. Secrets in `settings.json` are protected with Windows' own encryption and only work for your Windows account.

`app_id.py` (created by the build) contains your API hash. Don't upload it to GitHub.

## Run from source: SpiDrive.bat

Put the files in one folder and double-click **`SpiDrive.bat`**, every time. Each time, it checks what is
missing and only does that, then starts SpiDrive with its icon next to the clock:

1. **Python:** finds Python 3.10 or newer and creates the `venv` folder.
2. **Packages:** installs the packages, the first time and whenever `requirements.txt` changes.
   If the download is blocked, it retries through a VPN app on this PC (v2rayN, Clash, Nekoray).
3. **WinFsp:** installs WinFsp for drive S:. Windows asks for permission once.
4. **rclone:** installs rclone.
5. **Start:** starts SpiDrive. The first time, a setup page opens in your browser. There you log in to Telegram,
   choose the channel and options, and, if Telegram is blocked, connect through a proxy.
   If this folder already has a `config.py` and `session.session`, the page offers to use them.

`SpiDrive.bat console` starts it in the window instead, with the log shown there.
The first run takes a few minutes; later runs start in seconds. It replaces the old
`install.bat`, `setup_rclone.bat`, `start.bat` and `setup_drive.bat`.

## Settings in config.py

```python
API_ID = 123456
API_HASH = "..."
PHONE = "+98..."
CHANNEL = -100...                 # from the setup page / telegram_login.py
DATA_DIR = r"D:\TelegramDrive\data"
CHUNK_SIZE = 256 * 1024 * 1024    # part size for big files (max about 1.9 GB)
ENCRYPT = False                   # True = AES-256 before upload
PASSPHRASE = ""                   # needed when ENCRYPT = True. If you lose it, encrypted files are lost.
WEB_PORT = 8000
DAV_PORT = 8080

# optional
DRIVE_SIZE = "10T"                # size File Explorer shows for S: (Telegram itself has no limit)
RCLONE_CACHE_SIZE = "20G"         # rclone's local cache limit
SYNC_FOLDERS = {                  # two-way sync: PC folder -> drive folder
    r"D:\SpiDrive Sync": "/Sync",
}
WEB_HOST = "0.0.0.0"              # only to open the dashboard from your phone (needs WEB_PASSWORD)
WEB_PASSWORD = "choose-one"
PROXY_MODE = "auto"               # auto | manual | off
PROXY = ""                        # manual proxy link
```

Restart SpiDrive after changing `config.py`.

## When Telegram is blocked: proxy

In **Settings → Connection to Telegram** (or with `PROXY_MODE` in `config.py`):

| Mode | What it does |
|---|---|
| **Automatic** (default) | Connects directly when Telegram is reachable. When it is not, it uses the first of these that works: the proxy that worked last time; a VPN app on this PC (v2rayN, Nekoray, Clash, Tor); proxies that worked before; public MTProto lists on GitHub. Each proxy is tested for real before use. If the proxy stops working, SpiDrive switches to another one by itself. |
| **Always use my proxy** | Uses the link you paste: `tg://proxy?…`, `https://t.me/proxy?…`, `socks5://…` or `http://…` |
| **Always connect directly** | No proxy. |

**Find a working proxy now** in Settings searches again. The proxy lists can be changed under
*Proxy lists used by Automatic*, one address per line. When GitHub is blocked, the same files are fetched
through jsDelivr; when both are blocked, the last downloaded copy is used.

**Privacy:** Telegram traffic is encrypted end to end, so a proxy cannot read your files. It can see
when and how much data you send. MTProto proxies are preferred because they are made for Telegram.
"Fake-TLS" proxies (secrets starting with `ee`) are skipped, because SpiDrive's Telegram library
cannot use them.

## Two-way folder sync

Add `SYNC_FOLDERS` to `config.py` and restart. Every 10 seconds:

| You do this… | SpiDrive does this |
|---|---|
| Add or change a file in the PC folder | Uploads it to the drive folder |
| Add or change a file on the drive (dashboard, S:, Telegram) | Downloads it to the PC folder |
| Rename or move a file on either side | Renames it on the other side, without uploading again |
| Delete on the PC | Moves the drive copy to the **Trash** (restore it in the dashboard for 30 days) |
| Delete on the drive | Moves the PC copy to `data\sync-trash` (emptied after 30 days) |
| Change the same file on both sides | Keeps both: your PC copy becomes `name (conflict date).ext` |

**Backup only** (for Documents, Pictures, ...): choose *Backup only* for a folder in Settings, or click
**+ Back up Documents** / **+ Back up Pictures**. New and changed files on the PC are uploaded (the older
drive copy is kept as a version), a file missing on the drive is uploaded again, deleting on the PC never
deletes the drive copy, and nothing ever downloads back to the PC. In `config.py`:
`SYNC_FOLDERS = {r"C:\Users\me\Documents": {"remote": "/Backup/Documents", "mode": "backup"}}`

Sync never deletes anything permanently: files deleted on the PC wait in the dashboard's **Trash** for 30 days. (Older SpiDrive versions used a `/SpiDrive Trash` folder; it is still left alone by sync.) If the PC folder disappears (for example, an unplugged USB disk), sync pauses instead of deleting anything. Tip: syncing `/Telegram Inbox` brings everything you send from your phone straight into a folder on your PC.

## Trash and earlier versions

Deleting a file or folder (in the dashboard, on drive S:, with sync or `drive.py rm`) moves it to the
**Trash** in the sidebar. Nothing leaves Telegram yet. Open the Trash to **Restore** an item to where it
was (if that name is taken now, it comes back as `name (2).ext`), or **Delete forever**. Items are removed
from Telegram for good after 30 days. Right after deleting in the dashboard you can also click **Undo**.

When a file is replaced (uploaded again, saved over on drive S:, or changed in a synced folder), the old
one is kept as an **earlier version**. Click the clock button next to a file to see its versions, open
one, or **Restore** it: it becomes the current file again, and the one it replaced becomes a version.
SpiDrive keeps the last 10 versions of each file, each for 30 days. Change this with `TRASH_DAYS` and
`KEEP_VERSIONS` in `config.py`. Trashed files and versions still use space in your channel until they
are removed; the sidebar shows how much.

## Photos

**Photos** in the sidebar shows every photo and video on the drive, newest first, grouped by month (by
the date the photo was taken, when the camera recorded it). Previews are small pictures SpiDrive makes
once, so browsing does not download full-size images: photos uploaded through SpiDrive get theirs at
upload time, older photos the first time they are shown. Videos show a preview when they were sent from
Telegram. Previews are stored in `data\thumbs.db`, never in the channel.

## Working with many files

Tick the box in front of files and folders (Shift-click to pick a range, Ctrl+A for all, Esc to clear).
The bar at the top then lets you:

- **Download** them as one ZIP file. It is streamed while the files arrive from Telegram, so it starts
  at once and works for any size. A folder's download button does the same for that folder.
- **Move** them to another folder.
- **Keep on this PC**: a copy is stored in `data\offline` (decrypted), so they open instantly and
  without Telegram. Kept files show **On this PC**.
- **Free up space**: remove those copies; the files stay in Telegram.
- **Delete**: move them to the Trash.

## Search

Search finds files in all folders. Persian spellings match each other: ي and ی, ك and ک, ۱۲ and 12,
with or without the half-space (so "میخواهم" finds "می‌خواهم"). Add filters, alone or with words
(click **?** next to the search box for the list):

| Filter | Example |
|---|---|
| `type:` video, image, audio, doc, archive, other | `type:video` |
| `size:` | `size:>1gb`, `size:<500mb`, `size:10mb..1gb` |
| `after:` `before:` `date:` | `after:2026-01`, `before:2026-03-15`, `date:2026-02`, `after:30d`, Persian years `after:1404-07` |
| `in:` folder name contains | `in:Videos` |
| `ext:` | `ext:pdf` |
| `"…"` | `"annual report"` (words next to each other) |

Dates are when a photo was taken, otherwise the file's date.

## Player

- Music plays the **next track** in the folder by itself.
- Videos (and audio longer than 10 minutes) **remember where you stopped**. Reopen one to continue;
  click *Start over* to begin again.
- A video shows **subtitles** from a `.srt` or `.vtt` file with the same name in the same folder:
  `Movie.srt`, `Movie.fa.srt`, `Movie.en.srt`... With several, the one in your browser's language (else
  Persian) is shown; pick another in the player's ⋮ menu. Old Persian subtitles saved as Windows-1256 are
  read correctly.

## Sharing a file

Click the **paper-plane** button next to a file:

- **Send on Telegram:** type an **@username**, a phone number saved in your Telegram contacts, or **me**
  (your Saved Messages). The recipient gets a normal, readable file. Encrypted and split files are decrypted
  and joined first. Telegram's limit for one file is 2 GB (4 GB with Premium).
- **Link on this Wi-Fi:** choose how long it works (1 hour to 30 days) and click **Create link**. It is
  copied for you, like `http://192.168.1.20:8001/s/Xk3f9QaB2mPz`. Anyone on the same network who opens it
  sees the file (photos, music and video play in the browser) and a Download button. The dialog lists
  the file's links with their download count; **Remove** stops one at once. Links work while SpiDrive runs
  on this PC. They use port 8001 (`SHARE_PORT`); the first time, Windows may ask to allow SpiDrive on
  private networks. That small server serves only these links, nothing else.

## Your folders in Telegram

Your storage channel has a pinned message, **📁 My Drive**. It lists the top folders and files; tap a
folder to jump to its own message, which lists what is inside (with ⬆️ to go back up), and tap a file to
jump to it. Big folders continue over several messages (*Next* / *Previous*). SpiDrive updates these
messages about once a minute after you add, move or delete something, a few at a time, so a big change
can take some minutes to show everywhere.

Files that are encrypted (🔒) or split into parts (🧩) are listed by name but can't open in Telegram: send
`/get` and the name to your Saved Messages to receive a normal copy. The lists show real file names even
when encryption is on; turn them off in **Settings → Telegram** (the folder messages are then deleted) to
keep names out of the channel.

## Control from your phone (Saved Messages)

In Telegram on your phone, open **Saved Messages** (your chat with yourself) and send:

| Command | What your PC does |
|---|---|
| `/find invoice 2026` | Lists matching files, numbered (all search filters work: `/find type:video size:>1gb`) |
| `/get 2` | Sends result 2 of the last `/find` to Saved Messages. `/get /Docs/a.pdf` or `/get <words>` also work |
| `/keep 2` | Keeps that file on the PC, so it opens without Telegram |
| `/stats` | Storage used, uploads waiting, trash, drive health, last index backup |
| `/help` | The list of commands |

It works from anywhere, through your own Telegram account: no port is opened and nothing listens on the
internet. Only your own Saved Messages are read. The PC must be on with SpiDrive running; commands sent
while it is off are not answered later. Turn it off in **Settings → Telegram**.

## Telegram Inbox folders

Files you send or forward to the storage channel are filed by type inside **Telegram Inbox**:
`Photos/2026-11` and `Videos/2026-11` (by month), `Voice` (voice messages), `Music`, `Docs` and `Other`.
Turn it off in **Settings → Telegram** to keep everything in the Inbox itself.

## Uploads: pause, speed, duplicates

The upload panel (bottom right) has **Pause** and **Speed**. Speed is **automatic** by default: when
Telegram answers "too fast, wait N seconds" (FloodWait), SpiDrive halves its upload speed and runs one
upload fewer at a time; after each calm minute it speeds up by a quarter, until it is unlimited again. This
keeps your account from looking like abuse. The sidebar shows *auto · slowed to …* while it is holding
back. Pick a fixed speed in the panel to turn this off. Paused files wait on this PC (they
are already in the drive and open normally) and continue from their last finished part when you resume.
Up to 3 small files upload at the same time; files over 20 MB go one at a time.

Before uploading, SpiDrive checks whether a file with exactly the same content is already in the channel
(comparing the SHA-256 of each part). If it is, the new file points at it and is "uploaded" instantly,
without using more space. A file saved again without changes doesn't create a new version.
**Duplicates** in the sidebar lists identical files on the drive and how much is stored more than once;
**Select extra copies** keeps the oldest of each.

## Weekly digest

Once a week a short summary lands in your Saved Messages, for example:

```
📅 SpiDrive, 10–17 Nov
• 42 files, 3.1 GB added
• 1 failed upload (still retrying): report.pdf
• Health OK, 64% verified this month
• 2 files in the trash expire this week
• In total: 1,234 files, 120 GB
```

Send `/digest` in Saved Messages to see it at any time, or use **Send one now** in Settings → Telegram,
where you can also turn it off.

## Recently opened files and low disk space

When a file is opened in full from Telegram (in the dashboard, shared or zipped), a copy stays on the PC
(`data\recent`), so it opens instantly next time; the dashboard marks it **recent**. These copies use at
most 10 GB (`RECENT_CACHE_SIZE`). Every minute SpiDrive checks the disk: below 10% free
(`LOW_DISK_PERCENT`), it deletes recently opened copies, the least recently opened first, until there is a
little more room. Files you chose to **Keep on this PC** are never freed. If the disk stays low with nothing
left to free, the sidebar says so. Drive S: (rclone 1.62 or newer) keeps the same 10% free by dropping its
own cached files. The files themselves always stay in Telegram.

## Encryption keys and the recovery key

Your files are encrypted with one key, which your passphrase unlocks. So in **Settings → Encryption**:

- **Change passphrase** takes a second: no file is encrypted again. You need the current passphrase or
  the recovery key.
- **Create recovery key** shows a key like `K3Z4-6N6E-...` once. Print it or write it down. It unlocks
  your files if you forget the passphrase (*Change passphrase → Use your recovery key*), also on a new PC.

With a passphrase set, the daily **index backup** in the channel is encrypted too, so the channel shows
no file names at all. The sidebar says whether the last backup was encrypted. Only the newest 7 backups
are kept (`BACKUP_KEEP`).

## Drive health

Every hour SpiDrive downloads a few random parts again (up to 64 MB, parts not checked recently first)
and compares them with the SHA-256 saved when they were uploaded. The **Drive health** card in the sidebar
shows how much was verified in the last 30 days, and **See problems** lists anything damaged or missing.
**Check now** runs a check at once. Turn the hourly check off in **Settings → General**.

## On your phone

Set `WEB_HOST = "0.0.0.0"` and a `WEB_PASSWORD`, restart SpiDrive, and open `http://<your PC's IP>:8000` on your phone while it's on the same Wi-Fi. In Chrome, choose **Add to Home screen** to get a SpiDrive icon. Your PC must be on.

## If something goes wrong

- **Lost or damaged index (or a new PC):** the index (`data\index.db`) is the list of which messages belong to which file. SpiDrive backs it up to the channel every day. To get it back:
  ```
  venv\Scripts\activate
  python drive.py restore-index
  ```
  Close SpiDrive first. Your old index is kept as a copy. An encrypted backup asks for your passphrase or
  your recovery key; with the recovery key, it also asks you to choose a new passphrase.
- **Change the passphrase when running from config.py:** `python drive.py change-passphrase`, then put the
  new one in `config.py`.
- **S: doesn't appear:** the SpiDrive window prints the reason. rclone's own log is `data\rclone.log`.
- **"FloodWait" pauses:** Telegram is limiting upload speed. SpiDrive waits and continues on its own.
- **Uploads stuck "retrying":** check your internet. SpiDrive keeps retrying.

## Keep private

`config.py` (your API hash), `session.session` (full access to your Telegram account) and your `PASSPHRASE`. Don't upload these to GitHub or send them to anyone.
