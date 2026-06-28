import json

import pytest
from pydantic import ValidationError

from src.server.protocol import (
    AudioEndMessage,
    AudioStartMessage,
    AuthErrorMessage,
    AuthOkMessage,
    DoneMessage,
    ErrorMessage,
    parse_client_message,
    serialize_server_message,
)


# 1. Parse auth message
def test_parse_auth():
    msg = parse_client_message(json.dumps({"type": "auth", "token": "abc123"}))
    assert msg.type == "auth"
    assert msg.token == "abc123"


# 2. Parse token message
def test_parse_token():
    msg = parse_client_message(json.dumps({"type": "token", "text": "Hello"}))
    assert msg.type == "token"
    assert msg.text == "Hello"


# 3. Parse flush message
def test_parse_flush():
    msg = parse_client_message(json.dumps({"type": "flush"}))
    assert msg.type == "flush"


# 4. Parse end message
def test_parse_end():
    msg = parse_client_message(json.dumps({"type": "end"}))
    assert msg.type == "end"


# 5. Unknown type raises ValidationError
def test_unknown_type_raises():
    with pytest.raises((ValidationError, Exception)):
        parse_client_message(json.dumps({"type": "bogus"}))


# 6. auth missing token raises ValidationError
def test_auth_missing_token():
    with pytest.raises((ValidationError, Exception)):
        parse_client_message(json.dumps({"type": "auth"}))


# 7. token missing text raises ValidationError
def test_token_missing_text():
    with pytest.raises((ValidationError, Exception)):
        parse_client_message(json.dumps({"type": "token"}))


# 8. Serialize server messages
def test_serialize_auth_ok():
    msg = AuthOkMessage()
    data = json.loads(serialize_server_message(msg))
    assert data["type"] == "auth_ok"


def test_serialize_auth_error():
    msg = AuthErrorMessage(reason="bad token")
    data = json.loads(serialize_server_message(msg))
    assert data["type"] == "auth_error"
    assert data["reason"] == "bad token"


def test_serialize_audio_start():
    msg = AudioStartMessage(chunk_id=0, sample_rate=24000)
    data = json.loads(serialize_server_message(msg))
    assert data["type"] == "audio_start"
    assert data["chunk_id"] == 0
    assert data["sample_rate"] == 24000
    assert data["channels"] == 1
    assert data["encoding"] == "pcm16"


def test_serialize_audio_end():
    msg = AudioEndMessage(chunk_id=0)
    data = json.loads(serialize_server_message(msg))
    assert data["type"] == "audio_end"


def test_serialize_done():
    msg = DoneMessage()
    data = json.loads(serialize_server_message(msg))
    assert data["type"] == "done"


def test_serialize_error():
    msg = ErrorMessage(code="oops", message="something went wrong")
    data = json.loads(serialize_server_message(msg))
    assert data["type"] == "error"
    assert data["code"] == "oops"


from src.server.protocol import ChunkAbortedMessage


def test_serialize_chunk_aborted():
    msg = ChunkAbortedMessage(chunk_id=7)
    data = json.loads(serialize_server_message(msg))
    assert data["type"] == "chunk_aborted"
    assert data["chunk_id"] == 7


def test_parse_client_does_not_match_chunk_aborted():
    """chunk_aborted is server→client only; clients must not send it."""
    with pytest.raises((ValidationError, Exception)):
        parse_client_message(json.dumps({"type": "chunk_aborted", "chunk_id": 1}))
