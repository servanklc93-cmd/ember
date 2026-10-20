"""RESP2 encoding and decoding.

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
        if prefix == 0x2A:  # *
            n = int(line)
            if n == -1:
                return None, nxt
            items = []
            for _ in range(n):
                item, nxt = parse(buf, nxt)
                if item is INCOMPLETE:
                    return INCOMPLETE, pos
                items.append(item)
            return items, nxt
    except ValueError:
        raise ProtocolError("invalid length") from None

    # inline command, e.g. someone typing "PING" over telnet
    return bytes(buf[pos:end]).split(), nxt


def encode(value):
    if value is None:
        return b"$-1\r\n"
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
        return b"*%d\r\n" % len(value) + b"".join(encode(v) for v in value)
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
