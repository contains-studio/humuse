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


async def combine_mp3(parts: list[bytes]) -> bytes:
    """Decode separately generated replies and produce one seekable MP3 stream.

    Joining MP3 bytes preserves per-file duration headers, causing players to
    stop after the first response. Decode through concat before encoding once.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="humuse-reply-") as directory:
        root = Path(directory)
        for index, data in enumerate(parts):
            (root / f"{index}.mp3").write_bytes(data)
        listing = root / "parts.txt"
        listing.write_text("".join(f"file '{index}.mp3'\n" for index in range(len(parts))))
        pcm = await _ffmpeg(
            "-f", "concat", "-safe", "1", "-i", str(listing), "-t", "181",
            "-vn", "-ac", "1", "-ar", "24000", "-f", "s16le", "pipe:1",
        )
        if len(pcm) > 180 * 24000 * 2:
            raise ValueError("Muse reply exceeds three minutes")
        return await _ffmpeg(
            "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0",
            "-c:a", "libmp3lame", "-b:a", "64k", "-write_xing", "0", "-f", "mp3", "pipe:1",
            data=pcm,
        )


async def _ffmpeg(*arguments: str, data: bytes | None = None) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe", *arguments,
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(data), timeout=45)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode or not output:
        raise ValueError("Media conversion failed")
    return output


async def prepare_capture(data: bytes, mime_type: str) -> bytes:
    """Keep originals in the gallery; fit large camera captures into Muse's 8 MiB budget."""
    import json
    from pathlib import Path
    import tempfile

    maximum = 8 * 1024 * 1024
    if len(data) <= maximum:
        return data
    with tempfile.TemporaryDirectory(prefix="humuse-capture-") as directory:
        source = Path(directory) / ("capture.jpg" if mime_type == "image/jpeg" else "capture.mp4")
        await asyncio.to_thread(source.write_bytes, data)
        if mime_type == "image/jpeg":
            output = await _ffmpeg(
                "-i", str(source), "-vf", "scale='min(2048,iw)':'min(2048,ih)':force_original_aspect_ratio=decrease",
                "-frames:v", "1", "-q:v", "3", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
            )
        elif mime_type == "video/mp4":
            process = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
                "format=duration", "-of", "json", str(source), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                metadata, _ = await asyncio.wait_for(process.communicate(), 10)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
            try:
                duration = float(json.loads(metadata)["format"]["duration"])
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError("Cannot read capture duration") from exc
            if process.returncode or not 0 < duration <= 30:
                raise ValueError("Large video forwarding supports clips up to 30 seconds")
            output = await _ffmpeg(
                "-i", str(source), "-vf", "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-c:v", "libx264", "-preset", "veryfast", "-b:v", "1500k", "-maxrate", "1800k",
                "-bufsize", "3600k", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
                "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "pipe:1",
            )
        else:
            raise ValueError("Unsupported capture type")
    if len(output) > maximum:
        raise ValueError("Converted capture still exceeds Muse's attachment limit")
    return output
