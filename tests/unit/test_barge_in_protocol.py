import json

import pytest
from pydantic import ValidationError

from src.server.protocol import (
    InterruptedMessage,
    InterruptMessage,
    parse_client_message,
    serialize_server_message,
)


def test_interrupt_message_serialization():
    msg = InterruptMessage()
    data = json.loads(serialize_server_message(msg))
    assert data == {"type": "interrupt"}


def test_parse_interrupt_message():
    msg = parse_client_message(json.dumps({"type": "interrupt"}))
    assert isinstance(msg, InterruptMessage)
    assert msg.type == "interrupt"


def test_interrupted_message_serialization():
    msg = InterruptedMessage(chunk_id=7)
    data = json.loads(serialize_server_message(msg))
    assert data == {"type": "interrupted", "chunk_id": 7}


def test_interrupted_message_requires_chunk_id():
    with pytest.raises((ValidationError, Exception)):
        InterruptedMessage()


def test_interrupt_chunk_id_zero_allowed():
    msg = InterruptedMessage(chunk_id=0)
    assert msg.chunk_id == 0