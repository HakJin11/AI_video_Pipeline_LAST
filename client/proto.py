"""UDP protocol shared by the server and the client.

Every request is one UTF-8 JSON datagram:
    {"v": 1, "id": <int>, "cmd": "<command>", "pw": "<password or empty>", ...arguments}
Every answer is one UTF-8 JSON datagram with the same "id":
    {"id": <int>, "ok": true, ...result}   or   {"id": <int>, "ok": false, "error": "<message>"}

Files (finished videos) travel as binary datagrams, requested a window at a time:
    MAGIC (4 bytes) | request id (uint32) | chunk index (uint32) | chunk bytes
UDP can drop, reorder or duplicate datagrams: the client re-sends a request that got no answer, and asks again
for the chunks that did not arrive. Commands that change something (start, render, assemble) are answered from a
short cache when the same request id arrives twice, so a retry never starts a second render.
"""
import json
import struct

VERSION = 1
DEFAULT_PORT = 9999
MAGIC = b"AVC1"
HEADER = struct.Struct("!4sII")
CHUNK_SIZE = 8000  # bytes of file per datagram (a few IP fragments on a LAN; safe under the 64 KB UDP limit)
MAX_DATAGRAM = 65507


def encode(obj: dict) -> bytes:
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_DATAGRAM:
        raise ValueError("message too large for one UDP datagram")
    return data


def decode(data: bytes) -> dict | None:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def pack_chunk(req_id: int, index: int, payload: bytes) -> bytes:
    return HEADER.pack(MAGIC, req_id & 0xFFFFFFFF, index) + payload


def unpack_chunk(data: bytes) -> tuple[int, int, bytes] | None:
    if len(data) < HEADER.size or not data.startswith(MAGIC):
        return None
    _, req_id, index = HEADER.unpack_from(data)
    return req_id, index, data[HEADER.size:]
