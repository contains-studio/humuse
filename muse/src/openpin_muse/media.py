"""Convert the Pin's Ogg/Opus recording to Muse's PCM16 WAV voice note."""

import asyncio
import io
import wave

MAX_AUDIO_SECONDS = 21  # OpenPin releases the assistant gesture after 20 seconds.


async def decode_ogg(recording: bytes) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
        "-f", "ogg", "-i", "pipe:0", "-t", str(MAX_AUDIO_SECONDS + 0.1),
        "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        pcm, _ = await asyncio.wait_for(process.communicate(recording), timeout=15)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode or not pcm or len(pcm) % 2:
        raise ValueError("The Pin recording is not valid Ogg audio")
    if len(pcm) > MAX_AUDIO_SECONDS * 16000 * 2:
        raise ValueError(f"The Pin recording exceeds {MAX_AUDIO_SECONDS} seconds")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return output.getvalue()
