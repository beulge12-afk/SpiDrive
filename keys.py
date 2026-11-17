"""
Encryption keys.

Files are encrypted with one master key. The master key is stored in the index only
"wrapped" (encrypted with AES-GCM), once with a key made from your passphrase and,
if you create one, once with a key made from your recovery key. So:

  - changing the passphrase only re-wraps the master key: no file is touched;
  - the recovery key can unlock everything if the passphrase is forgotten;
  - the index backup sent to the channel is encrypted with the master key, and its
    header carries the wrapped copies, so it can be restored on a new PC with the
    passphrase or the recovery key, and with nothing else.

For drives set up before this existed, the master key is the key that was derived
from the passphrase, so existing files keep working.
"""
import base64
import json
import re
import secrets
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # no I, O, 0, 1: easy to read back from paper
RECOVERY_CHARS = 32                               # 32 x 5 bits = 160 bits
BACKUP_MAGIC = b"SPIDRIVE-INDEX\x01"
_AAD_KEY = b"spidrive-master-key"


class WrongKey(ValueError):
    pass


def kdf(secret_text, salt):
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(secret_text.encode("utf-8"))


def wrap(kek, master):
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(kek).encrypt(nonce, master, _AAD_KEY)


def unwrap(kek, blob):
    try:
        return AESGCM(kek).decrypt(blob[:12], blob[12:], _AAD_KEY)
    except InvalidTag:
        raise WrongKey("That passphrase or recovery key is not right.") from None


def new_recovery_key():
    raw = "".join(secrets.choice(ALPHABET) for _ in range(RECOVERY_CHARS))
    return "-".join(raw[i:i + 4] for i in range(0, RECOVERY_CHARS, 4))


def normalize_recovery_key(text):
    """Accepts any spacing, dashes and letter case. Raises ValueError if it can't be one."""
    t = re.sub(r"[\s\-_]", "", str(text or "")).upper()
    if len(t) != RECOVERY_CHARS or any(c not in ALPHABET for c in t):
        raise ValueError("A recovery key has 32 letters and digits, like ABCD-EFGH-....")
    return t


def looks_like_recovery_key(text):
    try:
        normalize_recovery_key(text)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- index backups

def _b64(b):
    return base64.b64encode(b).decode() if b is not None else None


def encrypt_backup(data, master, wraps):
    """wraps: {"pass": (salt, blob), "recovery": (salt, blob) or None}."""
    header = json.dumps({k: [_b64(v[0]), _b64(v[1])] for k, v in wraps.items() if v}).encode()
    nonce = secrets.token_bytes(12)
    body = AESGCM(master).encrypt(nonce, data, BACKUP_MAGIC + header)
    return BACKUP_MAGIC + struct.pack(">I", len(header)) + header + nonce + body


def is_encrypted_backup(blob):
    return blob[:len(BACKUP_MAGIC)] == BACKUP_MAGIC


def decrypt_backup(blob, secret):
    """secret = the passphrase or the recovery key. Returns (index bytes, master key, 'pass'|'recovery')."""
    n = len(BACKUP_MAGIC)
    (hlen,) = struct.unpack(">I", blob[n:n + 4])
    header_raw = blob[n + 4:n + 4 + hlen]
    header = json.loads(header_raw)
    nonce, body = blob[n + 4 + hlen:n + 16 + hlen], blob[n + 16 + hlen:]
    tries = []
    if looks_like_recovery_key(secret) and "recovery" in header:
        tries.append(("recovery", normalize_recovery_key(secret)))
    if "pass" in header:
        tries.append(("pass", secret))
    for kind, text in tries:
        salt, wrapped = (base64.b64decode(x) for x in header[kind])
        try:
            master = unwrap(kdf(text, salt), wrapped)
        except WrongKey:
            continue
        return AESGCM(master).decrypt(nonce, body, BACKUP_MAGIC + header_raw), master, kind
    raise WrongKey("That passphrase or recovery key does not open this backup.")
