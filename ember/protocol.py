"""RESP2 / RESP3 encoding and decoding.

The parser is incremental: it works on a growing buffer and returns
INCOMPLETE when it needs more bytes, so the server can feed it whatever
the socket hands over (half a command, or fifty pipelined ones).
"""

CRLF = b"\r\n"
MAX_BULK = 512 * 1024 * 1024
INCOMPLETE = object()


class SimpleString(str):
    pass


class ErrorReply(str):
    pass


class ProtocolError(Exception):
    pass


# Reply shapes that RESP3 can express but RESP2 can't. On a RESP2
# connection they all degrade to plain arrays, which is what redis does.

class Map(list):
    """Flat [k1, v1, k2, v2, ...] list, sent as a map in RESP3."""


class SetReply(list):
    pass


class Push(list):
    """Out-of-band data such as pub/sub messages."""


class NullArray:
    pass


NULL_ARRAY = NullArray()


OK = SimpleString("OK")
QUEUED = SimpleString("QUEUED")
PONG = SimpleString("PONG")


def parse(buf, pos=0):
    """Parse one value from buf[pos:]. Returns (value, next_pos).

    If the buffer does not hold a complete value yet, returns (INCOMPLETE, pos).
    """
    if pos >= len(buf):
        return INCOMPLETE, pos
    end = buf.find(CRLF, pos)
    if end == -1:
        if len(buf) - pos > 64 * 1024:
            raise ProtocolError("line too long")
        return INCOMPLETE, pos

    prefix = buf[pos]
    line = bytes(buf[pos + 1:end])
    nxt = end + 2

    try:
        if prefix == 0x2B:  # +
            return SimpleString(line.decode()), nxt
        if prefix == 0x2D:  # -
            return ErrorReply(line.decode()), nxt
        if prefix == 0x3A:  # :
            return int(line), nxt
        if prefix == 0x24:  # $
            n = int(line)
            if n == -1:
                return None, nxt
            if n < 0 or n > MAX_BULK:
                raise ProtocolError("invalid bulk length")
            if len(buf) < nxt + n + 2:
                return INCOMPLETE, pos
            return bytes(buf[nxt:nxt + n]), nxt + n + 2
        if prefix == 0x5F:  # _ null
            return None, nxt
        if prefix == 0x23:  # # boolean
            return line == b"t", nxt
        if prefix == 0x2C:  # , double
            return float(line), nxt
        if prefix in (0x2A, 0x7E, 0x3E, 0x25):  # * ~ > %
            n = int(line)
            if n == -1:
                return None, nxt
            if prefix == 0x25:
                n *= 2
            items = []
            for _ in range(n):
                item, nxt = parse(buf, nxt)
                if item is INCOMPLETE:
                    return INCOMPLETE, pos
                items.append(item)
            kind = {0x2A: list, 0x7E: SetReply, 0x3E: Push, 0x25: Map}[prefix]
            return kind(items), nxt
    except ValueError:
        raise ProtocolError("invalid length") from None

    # inline command, e.g. someone typing "PING" over telnet
    return bytes(buf[pos:end]).split(), nxt


def encode(value, proto=2):
    if value is None:
        return b"_\r\n" if proto == 3 else b"$-1\r\n"
    if value is NULL_ARRAY:
        return b"_\r\n" if proto == 3 else b"*-1\r\n"
    if isinstance(value, SimpleString):
        return b"+" + value.encode() + CRLF
    if isinstance(value, ErrorReply):
        return b"-" + value.encode() + CRLF
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return b":%d\r\n" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, (bytes, bytearray)):
        return b"$%d\r\n%s\r\n" % (len(value), value)
    if isinstance(value, (list, tuple)):
        body = b"".join(encode(v, proto) for v in value)
        if proto == 3:
            if isinstance(value, Map):
                return b"%%%d\r\n" % (len(value) // 2) + body
            if isinstance(value, SetReply):
                return b"~%d\r\n" % len(value) + body
            if isinstance(value, Push):
                return b">%d\r\n" % len(value) + body
        return b"*%d\r\n" % len(value) + body
    raise TypeError(f"cannot encode {type(value).__name__}")


def encode_command(args):
    parts = [b"*%d\r\n" % len(args)]
    for arg in args:
        if isinstance(arg, str):
            arg = arg.encode()
        elif isinstance(arg, int):
            arg = str(arg).encode()
        parts.append(b"$%d\r\n%s\r\n" % (len(arg), arg))
    return b"".join(parts)
