import re

import pytest

import conftest
from conftest import put
from tgfolders import FolderMessages


@pytest.fixture
def folders(drive):
    return FolderMessages(drive, log=lambda m: None)


def text_of(drive, folders, path, page=0):
    mid = drive.db.execute("SELECT msg_id FROM tg_folders WHERE path=? AND page=?", (path, page)).fetchone()[0]
    return drive.client.msgs[mid].message, mid


def links(text):
    return dict((name, int(mid)) for mid, name in re.findall(r'<a href="https://t.me/c/1001/(\d+)">([^<]+)</a>', text))


async def test_folder_messages_link_folders_and_files(drive, folders, monkeypatch):
    await put(drive, "/Photos/2026 Trip/a.jpg", b"jpg")
    await put(drive, "/notes.txt", b"n")
    await put(drive, "/Docs/big.iso", b"x" * 200_000)                 # 4 parts: listed, not linked
    monkeypatch.setattr(conftest.cfg, "ENCRYPT", True)
    monkeypatch.setattr(conftest.cfg, "PASSPHRASE", "long passphrase")
    await put(drive, "/Docs/secret.pdf", b"s")
    drive.mkdir("/Empty")
    await folders.run(budget=100)
    root, root_id = text_of(drive, folders, "/")
    assert drive.client.pinned == root_id and "📁 <b>My Drive</b>" in root
    l = links(root)
    assert set(l) == {"Docs", "Empty", "Photos", "notes.txt"}
    assert l["notes.txt"] == drive._chunks(drive.get_file("/notes.txt")["id"])[0]["msg_id"]
    photos, photos_id = text_of(drive, folders, "/Photos")
    assert l["Photos"] == photos_id and links(photos)["My Drive"] == root_id
    trip, trip_id = text_of(drive, folders, "/Photos/2026 Trip")
    assert links(photos)["2026 Trip"] == trip_id and "a.jpg" in links(trip)
    docs, _ = text_of(drive, folders, "/Docs")
    assert "big.iso" not in links(docs) and "🧩4" in docs and "secret.pdf 🔒" not in links(docs)
    assert "🔒" in docs and "/get" in docs
    assert "<i>empty</i>" in text_of(drive, folders, "/Empty")[0]
    assert await folders.run(budget=100) == []                         # nothing changed: nothing edited


async def test_changes_are_followed(drive, folders):
    await put(drive, "/A/one.txt", b"1")
    await put(drive, "/B/two.txt", b"2")
    await folders.run(budget=100)
    a_id = text_of(drive, folders, "/A")[1]
    drive.move("/A/one.txt", "/B/one.txt")
    await drive.delete("/A")
    await folders.run(budget=100)
    assert a_id not in drive.client.msgs                               # folder gone: message deleted
    assert set(links(text_of(drive, folders, "/B")[0])) == {"My Drive", "one.txt", "two.txt"}
    assert "A" not in links(text_of(drive, folders, "/")[0])


async def test_big_folders_get_pages(drive, folders):
    for i in range(130):
        await put(drive, f"/Many/file {i:03d}.txt", b"x", upload=False)
    await conftest.flush(drive)
    await folders.run(budget=100)
    pages = drive.db.execute("SELECT COUNT(*) FROM tg_folders WHERE path='/Many'").fetchone()[0]
    assert pages == 3
    first, _ = text_of(drive, folders, "/Many", 0)
    second, second_id = text_of(drive, folders, "/Many", 1)
    assert links(first)["Next ➡️"] == second_id and "⬅️ Previous" in links(second)
    assert "page 1 of 3" in first and all(len(text_of(drive, folders, "/Many", p)[0]) < 4096 for p in range(3))


async def test_small_budget_converges_and_deleted_messages_come_back(drive, folders):
    for i in range(6):
        await put(drive, f"/F{i}/x.txt", b"x", upload=False)
    await conftest.flush(drive)
    for _ in range(10):
        await folders.run(budget=3)
    assert set(links(text_of(drive, folders, "/")[0])) == {f"F{i}" for i in range(6)}
    old = text_of(drive, folders, "/F2")[1]
    drive.client.msgs.pop(old)                                         # someone deleted it in Telegram
    folders._last_verify = 0                                           # time for the 10-minute check
    for _ in range(3):
        await folders.run(budget=100)
    new = text_of(drive, folders, "/F2")[1]
    assert new != old and links(text_of(drive, folders, "/")[0])["F2"] == new


async def test_turning_it_off_removes_the_messages(drive, folders):
    await put(drive, "/A/one.txt", b"1")
    await folders.run(budget=100)
    ids = [r[0] for r in drive.db.execute("SELECT msg_id FROM tg_folders")]
    assert ids and all(i in drive.client.msgs for i in ids)
    await folders.run(enabled=False)
    assert not any(i in drive.client.msgs for i in ids)
    assert drive.db.execute("SELECT COUNT(*) FROM tg_folders").fetchone()[0] == 0


async def test_folder_messages_are_not_imported_as_files(drive, folders):
    await put(drive, "/A/one.txt", b"1")
    await folders.run(budget=100)
    assert await drive.import_new_messages(min_age=0) == []
