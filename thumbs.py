"""
Small JPEG previews of photos, so the dashboard can browse a photo library without
downloading every full-size image. Needs Pillow (installed with SpiDrive); without
it, the dashboard shows file icons instead.

Previews are stored in DATA_DIR/thumbs.db (not in index.db, so they are never sent
to the channel with the index backup). They can always be made again.
"""
import io
import time

try:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 120_000_000           # refuse "decompression bombs"
except ImportError:                                # pragma: no cover
    Image = ImageOps = None

SIZE = 360            # longest side, in pixels
QUALITY = 78
SOURCE_LIMIT = 40 * 1024 * 1024   # don't download images bigger than this just for a preview

_EXIF_IFD, _DATETIME_ORIGINAL, _DATETIME = 0x8769, 36867, 306


def available():
    return Image is not None


def _taken(img):
    """Photo date from EXIF (when the picture was taken), as a timestamp, or None."""
    try:
        exif = img.getexif()
        raw = exif.get_ifd(_EXIF_IFD).get(_DATETIME_ORIGINAL) or exif.get(_DATETIME)
        if not raw:
            return None
        t = time.strptime(str(raw).strip().rstrip("\x00")[:19], "%Y:%m:%d %H:%M:%S")
        return time.mktime(t) if t.tm_year > 1900 else None
    except Exception:
        return None


def make(source):
    """source = a file path or bytes. Returns (jpeg bytes, taken timestamp or None).
    Raises on anything that is not a readable image."""
    if Image is None:
        raise RuntimeError("Pillow is not installed")
    with Image.open(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source) as img:
        taken = _taken(img)
        img.draft("RGB", (SIZE * 2, SIZE * 2))      # fast JPEG decoding at reduced size
        img = ImageOps.exif_transpose(img)
        img.thumbnail((SIZE, SIZE))
        if img.mode not in ("RGB", "L"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.convert("RGBA").split()[-1])
            img = bg
        out = io.BytesIO()
        img.save(out, "JPEG", quality=QUALITY, optimize=True)
        return out.getvalue(), taken
