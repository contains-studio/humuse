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
