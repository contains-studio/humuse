"""OpenPin's 512-byte JSON header, JPEG, Ogg, and MP3 framing.

This is the layout emitted by the unmodified Android BackendManager. Media
signatures are an early format check; the bridge's decoder validates codecs.
"""

from dataclasses import dataclass, field
import json
import math
import re


HEADER_SIZE = 512
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VOICE_BYTES = HEADER_SIZE + MAX_AUDIO_BYTES + MAX_IMAGE_BYTES


class ProtocolError(ValueError):
    """Malformed OpenPin wire data, safe to describe without echoing input."""


class PayloadTooLarge(ProtocolError):
    """A declared or received body exceeds the companion's limit."""


@dataclass(frozen=True)
class VoiceMetadata:
    audio_size: int
    image_size: int
    device_id: str = field(repr=False)
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True)
class VoiceRequest:
    metadata: VoiceMetadata
    audio: bytes = field(repr=False)
    image: bytes | None = field(repr=False)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ProtocolError("Non-finite JSON number")


def decode_json(data: bytes) -> dict:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError("Invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("JSON object required")
    return value


def parse_voice_header(header: bytes) -> VoiceMetadata:
    if len(header) != HEADER_SIZE:
        raise ProtocolError("Voice header must contain 512 bytes")
    terminator = header.find(b"\0")
    if terminator < 1 or any(header[terminator:]):
        raise ProtocolError("Voice header requires NUL termination and padding")
    value = decode_json(header[:terminator])
    for name, maximum, minimum in (("audioSize", MAX_AUDIO_BYTES, 1),
                                    ("imageSize", MAX_IMAGE_BYTES, 0)):
        size = value.get(name)
        if type(size) is not int or size < minimum:
            raise ProtocolError("Invalid media length")
        if size > maximum:
            raise PayloadTooLarge("Media exceeds configured size limit")
    device_id = value.get("deviceId")
    if not isinstance(device_id, str) or not 1 <= len(device_id) <= 256:
        raise ProtocolError("Invalid device identifier")
    if value.get("audioFormat") != "ogg":
        raise ProtocolError("OpenPin audio must use Ogg")
    bitrate = value.get("audioBitrate", "64k")
    if not isinstance(bitrate, str) or not re.fullmatch(r"[1-9][0-9]{0,3}k", bitrate):
        raise ProtocolError("Invalid audio bitrate")
    for name, minimum, maximum in (("battery", 0, 1), ("latitude", -90, 90),
                                    ("longitude", -180, 180)):
        number = value.get(name)
        if number is None:
            continue
        if type(number) not in (int, float) or not minimum <= number <= maximum or not math.isfinite(number):
            raise ProtocolError("Invalid numeric metadata")
    latitude, longitude = value.get("latitude"), value.get("longitude")
    if (latitude is None) != (longitude is None):
        raise ProtocolError("Location requires latitude and longitude")
    return VoiceMetadata(value["audioSize"], value["imageSize"], device_id, latitude, longitude)


def validate_capture(data: bytes, mime_type: str) -> None:
    if mime_type == "image/jpeg" and len(data) >= 4 and data.startswith(b"\xff\xd8\xff"):
        return
    if mime_type == "video/mp4" and len(data) >= 12 and data[4:8] == b"ftyp":
        return
    raise ProtocolError("Media does not match its declared format")


def parse_voice_request(body: bytes) -> VoiceRequest:
    if len(body) > MAX_VOICE_BYTES:
        raise PayloadTooLarge("Voice request exceeds configured size limit")
    metadata = parse_voice_header(body[:HEADER_SIZE])
    expected = HEADER_SIZE + metadata.image_size + metadata.audio_size
    if len(body) != expected:
        raise ProtocolError("Voice body does not match declared lengths")
    boundary = HEADER_SIZE + metadata.image_size
    image = body[HEADER_SIZE:boundary] if metadata.image_size else None
    audio = body[boundary:]
    if not audio.startswith(b"OggS"):
        raise ProtocolError("Audio is not an Ogg container")
    if image is not None:
        validate_capture(image, "image/jpeg")
    return VoiceRequest(metadata, audio, image)


def encode_voice_response(mp3: bytes) -> bytes:
    """Supply all fields expected by Android's ResponseMetadata data class."""
    if not isinstance(mp3, bytes) or not mp3:
        raise ValueError("Bridge returned no MP3 audio")
    metadata = {"nextUpdate": 0, "disabled": False, "doUpdate": False,
                "takePic": False, "wifi": True, "bt": True, "gnss": True,
                "spkVol": 1.0, "lLevel": 1.0}
    header = json.dumps(metadata, separators=(",", ":")).encode() + b"\0"
    return header.ljust(HEADER_SIZE, b"\0") + mp3
