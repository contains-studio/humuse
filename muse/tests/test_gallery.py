import asyncio
import stat

import pytest

from openpin_muse.gallery import Gallery, GalleryFull


JPEG = b"\xff\xd8\xffgallery image fixture\xff\xd9"
MP4 = b"\x00\x00\x00\x18ftypmp42gallery video fixture"


async def test_gallery_preserves_originals_and_status_across_restart(tmp_path):
    gallery = Gallery(tmp_path)
    image = await gallery.save(JPEG, "image/jpeg")
    video = await gallery.save(MP4, "video/mp4")
    assert image["muse_status"] == "pending"
    assert video["filename"].endswith(".mp4")
    await gallery.mark_forwarded(image["id"], "sent")
    restarted = Gallery(tmp_path)
    metadata, path = await restarted.get(image["id"])
    assert metadata["muse_status"] == "sent"
    assert path.read_bytes() == JPEG
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert {item["id"] for item in await restarted.list()} == {image["id"], video["id"]}
    assert await restarted.delete(image["id"])
    assert not await restarted.delete(image["id"])
    assert not path.exists()
    assert await restarted.get(image["id"]) is None


async def test_quota_is_atomic_and_never_evicts_existing_media(tmp_path):
    gallery = Gallery(tmp_path, max_bytes=len(JPEG))
    results = await asyncio.gather(*(gallery.save(JPEG, "image/jpeg") for _ in range(2)),
                                   return_exceptions=True)
    assert sum(isinstance(result, GalleryFull) for result in results) == 1
    assert len(await gallery.list()) == 1
    saved = next(result for result in results if isinstance(result, dict))
    await gallery.delete(saved["id"])
    assert (await gallery.save(JPEG, "image/jpeg"))["size"] == len(JPEG)


async def test_gallery_rejects_traversal_bad_formats_and_symlinked_originals(tmp_path):
    gallery = Gallery(tmp_path)
    assert await gallery.get("../secret") is None
    assert not await gallery.delete("../secret")
    with pytest.raises(ValueError):
        await gallery.save(b"not jpeg", "image/jpeg")
    with pytest.raises(ValueError):
        await gallery.save(JPEG, "text/html")
    image = await gallery.save(JPEG, "image/jpeg")
    _, path = await gallery.get(image["id"])
    path.unlink()
    path.symlink_to(tmp_path / "secret")
    assert await gallery.get(image["id"]) is None
    with pytest.raises(ValueError):
        await gallery.mark_forwarded(image["id"], "anything")
