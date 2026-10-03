import asyncio
import io
import shutil
import wave

import pytest

from openpin_muse.media import decode_ogg


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required for the actual audio conversion")
async def test_openpin_opus_becomes_muse_pcm_wave():
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-ar", "16000", "-ac", "1", "-c:a", "libopus", "-f", "ogg", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    source, error = await process.communicate()
    assert process.returncode == 0, error
    result = await decode_ogg(source)
    with wave.open(io.BytesIO(result)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.getnframes() == 16000
        assert any(wav.readframes(16000))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
async def test_bad_recording_is_rejected():
    with pytest.raises(ValueError, match="recording"):
        await decode_ogg(b"not an ogg recording")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
async def test_multiple_muse_replies_form_one_playable_recording():
    from openpin_muse.media import combine_mp3
    from test_integration import tone

    first = await tone("libmp3lame", "mp3")
    joined = await combine_mp3([first, first])
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-f", "mp3", "-i", "pipe:0", "-ar", "24000", "-ac", "1",
        "-f", "s16le", "pipe:1", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    pcm, error = await process.communicate(joined)
    assert process.returncode == 0, error
    assert 2 <= len(pcm) / (24000 * 2) < 2.3


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
@pytest.mark.parametrize("mime", ["image/jpeg", "video/mp4"])
async def test_large_capture_is_reencoded_for_muse_without_altering_original(mime, tmp_path):
    import struct
    from openpin_muse.media import prepare_capture
    target = tmp_path / ("fixture.jpg" if mime == "image/jpeg" else "fixture.mp4")
    command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=blue:size=64x64:rate=5"]
    command += ["-frames:v", "1"] if mime == "image/jpeg" else ["-t", "1", "-c:v", "libx264"]
    process = await asyncio.create_subprocess_exec(*command, str(target), stderr=asyncio.subprocess.PIPE)
    _, error = await process.communicate()
    assert process.returncode == 0, error
    original = target.read_bytes()
    padding = b"\0" * (8 * 1024 * 1024)
    # Oversized local fixture: image trailing bytes / legal MP4 free box.
    large = original + (struct.pack(">I4s", len(padding) + 8, b"free") if mime == "video/mp4" else b"") + padding
    result = await prepare_capture(large, mime)
    assert 0 < len(result) < 8 * 1024 * 1024
    assert large.startswith(original)
    assert result[:3] == b"\xff\xd8\xff" if mime == "image/jpeg" else result[4:8] == b"ftyp"
