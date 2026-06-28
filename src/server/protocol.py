import json
from typing import Any, Literal

from pydantic import BaseModel, ValidationError


# ── Client → Server ──────────────────────────────────────────────────────────

class AuthMessage(BaseModel):
    type: Literal["auth"]
    token: str


class TokenMessage(BaseModel):
    type: Literal["token"]
    text: str


class FlushMessage(BaseModel):
    type: Literal["flush"]


class EndMessage(BaseModel):
    type: Literal["end"]


ClientMessage = AuthMessage | TokenMessage | FlushMessage | EndMessage


def parse_client_message(raw: str) -> ClientMessage:
    data: dict[str, Any] = json.loads(raw)
    msg_type = data.get("type")
    match msg_type:
        case "auth":
            return AuthMessage.model_validate(data)
        case "token":
            return TokenMessage.model_validate(data)
        case "flush":
            return FlushMessage.model_validate(data)
        case "end":
            return EndMessage.model_validate(data)
        case _:
            raise ValidationError.from_exception_data(
                title="ClientMessage",
                input_type="python",
                line_errors=[
                    {
                        "type": "literal_error",
                        "loc": ("type",),
                        "msg": f"Unknown message type: {msg_type!r}",
                        "input": msg_type,
                        "ctx": {"expected": "auth, token, flush, end"},
                    }
                ],
            )


# ── Server → Client ──────────────────────────────────────────────────────────

class AuthOkMessage(BaseModel):
    type: Literal["auth_ok"] = "auth_ok"


class AuthErrorMessage(BaseModel):
    type: Literal["auth_error"] = "auth_error"
    reason: str


class AudioStartMessage(BaseModel):
    type: Literal["audio_start"] = "audio_start"
    chunk_id: int
    sample_rate: int
    channels: int = 1
    encoding: str = "pcm16"


class AudioEndMessage(BaseModel):
    type: Literal["audio_end"] = "audio_end"
    chunk_id: int


class ChunkAbortedMessage(BaseModel):
    type: Literal["chunk_aborted"] = "chunk_aborted"
    chunk_id: int


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str


class DoneMessage(BaseModel):
    type: Literal["done"] = "done"


ServerMessage = (
    ChunkAbortedMessage
    | AuthOkMessage
    | AuthErrorMessage
    | AudioStartMessage
    | AudioEndMessage
    | ErrorMessage
    | DoneMessage
)


def serialize_server_message(msg: ServerMessage) -> str:
    return msg.model_dump_json()
