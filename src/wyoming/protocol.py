import asyncio
import json


async def read_event(
    reader: asyncio.StreamReader,
) -> tuple[str, dict, bytes]:
    line = await reader.readline()
    if not line:
        return "", {}, b""
    header = json.loads(line.decode())
    event_type = header.get("type", "")
    data = header.get("data", {})
    # wyoming library sends event data as a separate block after the header line
    data_length = header.get("data_length", 0)
    if data_length > 0:
        data_bytes = await reader.readexactly(data_length)
        data.update(json.loads(data_bytes))
    payload_length = header.get("payload_length", 0)
    payload = await reader.readexactly(payload_length) if payload_length else b""
    return event_type, data, payload


async def write_event(
    writer,
    event_type: str,
    data: dict | None = None,
    payload: bytes = b"",
) -> None:
    header: dict = {"type": event_type}
    if data is not None:
        header["data"] = data
    if payload:
        header["payload_length"] = len(payload)
    writer.write((json.dumps(header) + "\n").encode())
    if payload:
        writer.write(payload)
    await writer.drain()
