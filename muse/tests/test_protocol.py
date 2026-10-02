"""Wire-format fixtures mirror BackendManager.kt, without requiring codecs."""

import json

import pytest

from openpin_muse.protocol import (
    HEADER_SIZE,
    MAX_AUDIO_BYTES,
    PayloadTooLarge,
    ProtocolError,
    encode_voice_response,
    parse_voice_header,
    parse_voice_request,
)


# Synthetic media markers: these tests exercise framing, not media decoding.
AUDIO = b"OggS\x00audio fixture"
IMAGE = b"\xff\xd8\xff\xe0image fixture\xff\xd9"


def packet(*, audio=AUDIO, image=IMAGE, **changes):
    metadata = {
        "audioSize": len(audio),
        "audioFormat": "ogg",
        "imageSize": len(image),
        "deviceId": "fixture-device",
        "audioBitrate": "64k",
        "battery": 0.5,
    }
    metadata.update(changes)
    header = json.dumps(metadata).encode() + b"\0"
    return header.ljust(HEADER_SIZE, b"\0") + image + audio


def test_android_image_then_audio_layout():
    parsed = parse_voice_request(packet())
    assert parsed.metadata.device_id == "fixture-device"
    assert parsed.audio == AUDIO
    assert parsed.image == IMAGE


def test_audio_only_layout():
    parsed = parse_voice_request(packet(image=b""))
    assert parsed.audio == AUDIO
    assert parsed.image is None


@pytest.mark.parametrize("changes", [
    {"audioSize": -1}, {"audioSize": True}, {"audioSize": "10"},
    {"imageSize": 1.5}, {"deviceId": ""}, {"deviceId": 1},
    {"audioFormat": "mp3"}, {"battery": float("nan")},
    {"latitude": 91}, {"longitude": -181}, {"audioBitrate": "64k;sh"},
])
def test_invalid_metadata(changes):
    with pytest.raises(ProtocolError):
        parse_voice_request(packet(**changes))


@pytest.mark.parametrize("body", [b"", b"x" * 511, b"x" * 512,
                                      packet()[:-1], packet() + b"extra"])
def test_truncated_malformed_or_extra_bytes(body):
    with pytest.raises(ProtocolError):
        parse_voice_request(body)


def test_header_requires_zero_padding_and_unique_keys():
    header = packet()[:HEADER_SIZE]
    with pytest.raises(ProtocolError):
        parse_voice_header(header[:-1] + b"!")
    duplicate = (
        b'{"deviceId":"a","deviceId":"b","audioSize":1,"imageSize":0,"audioFormat":"ogg"}\0'
    ).ljust(HEADER_SIZE, b"\0")
    with pytest.raises(ProtocolError, match="Duplicate JSON field"):
        parse_voice_header(duplicate)


def test_oversize_is_detectable_from_header_before_body_read():
    with pytest.raises(PayloadTooLarge):
        parse_voice_header(packet(audioSize=MAX_AUDIO_BYTES + 1)[:HEADER_SIZE])


@pytest.mark.parametrize("changes", [{"audio": b"RIFFnot ogg"}, {"image": b"not jpeg"}])
def test_media_type_markers(changes):
    with pytest.raises(ProtocolError):
        parse_voice_request(packet(**changes))


def test_response_has_parseable_android_metadata_then_exact_mp3():
    mp3 = b"ID3synthetic mp3 fixture"
    response = encode_voice_response(mp3)
    metadata = json.loads(response[:HEADER_SIZE].rstrip(b"\0"))
    assert metadata["disabled"] is False
    assert metadata["doUpdate"] is False
    assert response[HEADER_SIZE:] == mp3
    assert len(response[:HEADER_SIZE]) == 512
