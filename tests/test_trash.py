import drive_core
from conftest import flush, put, read


async def test_delete_goes_to_trash_and_restores(drive):
    await put(drive, "/docs/a.txt", b"hello" * 30000)          # several parts
    msgs = set(drive.client.msgs)
    assert await drive.delete("/docs/a.txt") == 1
    assert drive.get_entry("/docs/a.txt") is None
    assert set(drive.client.msgs) == msgs                       # nothing left Telegram
    [item] = drive.trash_list()
    assert item["path"] == "/docs/a.txt" and item["files"] == 1 and item["bytes"] == 150000
    assert drive.stats()["files"] == 0 and drive.stats()["trash"] == 1
    assert await drive.restore(item["id"]) == ["/docs/a.txt"]
    assert await read(drive, "/docs/a.txt") == b"hello" * 30000
    assert drive.trash_list() == []


async def test_restore_when_the_name_is_taken(drive):
    await put(drive, "/a.txt", b"old")
    await drive.delete("/a.txt")
    await put(drive, "/a.txt", b"new")
    [item] = drive.trash_list()
    assert await drive.restore(item["id"]) == ["/a (2).txt"]
    assert await read(drive, "/a (2).txt") == b"old"
    assert await read(drive, "/a.txt") == b"new"


async def test_folder_with_empty_subfolder_comes_back(drive):
    await put(drive, "/f/x.bin", b"x" * 1000)
    await put(drive, "/f/sub/y.bin", b"y" * 1000)
    drive.mkdir("/f/empty")
    assert await drive.delete("/f") == 2
    assert not drive.is_dir("/f")
    [item] = drive.trash_list()
    assert item["is_dir"] and item["files"] == 2
    await drive.restore(item["id"])
    assert drive.listdir("/f")[0] == ["empty", "sub"]
    assert await read(drive, "/f/sub/y.bin") == b"y" * 1000


async def test_overwrite_keeps_versions_and_restore_swaps(drive):
    await put(drive, "/n.txt", b"v1")
    await put(drive, "/n.txt", b"v2")
    await put(drive, "/n.txt", b"v3")
    vs = drive.versions("/n.txt")
    assert [v["size"] for v in vs] == [2, 2] and len(vs) == 2
    assert drive.trash_list() == []                              # versions are not in the trash list
    rows = [drive.get_file_by_id(v["file_id"]) for v in vs]
    assert [b"".join([x async for x in drive.iter_row(r)]) for r in rows] == [b"v2", b"v1"]
    await drive.restore(vs[1]["id"])                             # back to v1
    assert await read(drive, "/n.txt") == b"v1"
    contents = [b"".join([x async for x in drive.iter_row(drive.get_file_by_id(v["file_id"]))])
                for v in drive.versions("/n.txt")]
    assert contents == [b"v3", b"v2"]


async def test_old_versions_are_limited(drive, monkeypatch):
    monkeypatch.setattr(drive_core, "KEEP_VERSIONS", 2)
    for i in range(5):
        await put(drive, "/n.txt", b"v%d" % i)
    assert len(drive.versions("/n.txt")) == 2
    assert len(drive.client.msgs) == 3                           # current + 2 versions


async def test_purge_deletes_messages_but_not_shared_copies(drive):
    await put(drive, "/a.bin", b"a" * 100)
    await drive.copy("/a.bin", "/copy.bin")
    await drive.delete("/a.bin")
    assert await drive.empty_trash() == 1
    assert len(drive.client.msgs) == 1                           # the copy still uses it
    await drive.delete("/copy.bin")
    await drive.purge_expired(days=0)
    assert drive.client.msgs == {} and drive.trash_list() == []


async def test_deleting_a_waiting_upload_keeps_the_local_copy(drive):
    await put(drive, "/w.txt", b"waiting", upload=False)
    await drive.delete("/w.txt")
    assert drive.pending() == []
    [item] = drive.trash_list()
    await drive.restore(item["id"])
    assert drive.get_entry("/w.txt")["status"] == "pending"
    await flush(drive)
    assert await read(drive, "/w.txt") == b"waiting"


async def test_versions_follow_renames(drive):
    await put(drive, "/d/n.txt", b"1")
    await put(drive, "/d/n.txt", b"2")
    drive.move("/d", "/e")
    assert len(drive.versions("/e/n.txt")) == 1


async def test_trash_is_hidden_from_listing_search_and_sync_scans(drive):
    await put(drive, "/trash me.txt", b"x")
    await drive.delete("/trash me.txt")
    assert drive.listdir("/") == ([], [])
    assert drive.search("trash")[0] == []
    assert drive.walk("/") == []
