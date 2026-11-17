# Copy this file to config.py and fill it in. Never upload config.py.
# (Only needed when running from source without the setup page.)

API_ID = 0                      # from https://my.telegram.org/apps
API_HASH = ""                   # from https://my.telegram.org/apps
PHONE = "+00000000000"          # your Telegram phone number
CHANNEL = ""                    # -100... id of your private storage channel

DATA_DIR = r"D:\SpiDrive\data"  # index, upload queue and caches
CHUNK_SIZE = 256 * 1024 * 1024  # part size for big files (max about 1.9 GB)
ENCRYPT = False                 # True = AES-256 before upload
PASSPHRASE = ""                 # required when ENCRYPT = True

WEB_PORT = 8000
DAV_PORT = 8080
DRIVE_SIZE = "1P"               # size File Explorer shows for the drive
PROXY_MODE = "auto"             # auto | manual | off
PROXY = ""                      # proxy link for manual mode
SYNC_FOLDERS = {}               # {r"D:\SpiDrive Sync": "/Sync"}
TRASH_DAYS = 30                 # deleted files and old versions are kept this long
KEEP_VERSIONS = 10              # earlier versions kept per file
PHONE_COMMANDS = True           # answer /find, /get, /stats, /keep in Saved Messages
INBOX_ORGANIZE = True           # sort the Telegram Inbox into Photos/2026-11, Voice, Docs...
HEALTH_CHECK = True             # re-check random parts against their SHA-256 every hour
SHARE_PORT = 8001               # share links on the Wi-Fi
UPLOAD_PARALLEL = 3             # small files uploading at the same time
BACKUP_KEEP = 7                 # index backups kept in the channel
TELEGRAM_FOLDERS = True         # one message per folder in the channel, under a pinned "My Drive"
WEEKLY_DIGEST = True            # a short summary in Saved Messages once a week
LOW_DISK_PERCENT = 10           # below this much free space, recently opened copies are freed
RECENT_CACHE_SIZE = 10 * 1024 ** 3   # at most this much for recently opened copies
