import pytest

from ember.protocol import (
    INCOMPLETE, NULL_ARRAY, ErrorReply, Map, ProtocolError, Push, SetReply, SimpleString,
    encode, encode_command, parse,
)


def test_parse_command_array():
    buf = b"*2\r\n$3\r\nGET\r\n$3\r\nfoo\r\n"
    assert parse(buf) == ([b"GET", b"foo"], len(buf))


def test_parse_scalars():
    assert parse(b"+OK\r\n")[0] == "OK"
    assert isinstance(parse(b"-ERR bad\r\n")[0], ErrorReply)
    assert parse(b":42\r\n")[0] == 42
    assert parse(b"$-1\r\n")[0] is None
    assert parse(b"$0\r\n\r\n")[0] == b""


def test_bulk_can_contain_crlf():
    assert parse(b"$4\r\na\r\nb\r\n")[0] == b"a\r\nb"


def test_incomplete_input_returns_marker():
    full = b"*2\r\n$3\r\nGET\r\n$3\r\nfoo\r\n"
    for cut in range(len(full)):
        value, pos = parse(full[:cut])
        assert value is INCOMPLETE and pos == 0


def test_pipelined_commands():
    buf = b"*1\r\n$4\r\nPING\r\n*1\r\n$4\r\nPING\r\n"
    first, pos = parse(buf)
    second, pos = parse(buf, pos)
    assert first == second == [b"PING"]
    assert pos == len(buf)


def test_inline_command():
    assert parse(b"SET  k   v\r\n")[0] == [b"SET", b"k", b"v"]


def test_bad_length_raises():
    with pytest.raises(ProtocolError):
        parse(b"$abc\r\n")
    with pytest.raises(ProtocolError):
        parse(b"$-5\r\n")


def test_encode():
    assert encode(SimpleString("OK")) == b"+OK\r\n"
    assert encode(ErrorReply("ERR x")) == b"-ERR x\r\n"
    assert encode(7) == b":7\r\n"
    assert encode(None) == b"$-1\r\n"
    assert encode(b"hi") == b"$2\r\nhi\r\n"
    assert encode([b"a", 1, None]) == b"*3\r\n$1\r\na\r\n:1\r\n$-1\r\n"


def test_encode_command_round_trip():
    args = [b"SET", b"key", b"with\r\nnewline"]
    assert parse(encode_command(args))[0] == args


def test_resp3_shapes_degrade_on_resp2():
    assert encode(Map([b"a", 1])) == b"*2\r\n$1\r\na\r\n:1\r\n"
    assert encode(SetReply([b"x"])) == b"*1\r\n$1\r\nx\r\n"
    assert encode(None) == b"$-1\r\n"
    assert encode(NULL_ARRAY) == b"*-1\r\n"


def test_resp3_encoding():
    assert encode(Map([b"a", 1]), 3) == b"%1\r\n$1\r\na\r\n:1\r\n"
    assert encode(SetReply([b"x"]), 3) == b"~1\r\n$1\r\nx\r\n"
    assert encode(Push([b"message"]), 3) == b">1\r\n$7\r\nmessage\r\n"
    assert encode(None, 3) == b"_\r\n"
    assert encode(NULL_ARRAY, 3) == b"_\r\n"


def test_resp3_parsing():
    assert parse(b"%1\r\n+k\r\n:1\r\n")[0] == Map(["k", 1])
    assert isinstance(parse(b"~0\r\n")[0], SetReply)
    assert isinstance(parse(b">1\r\n+x\r\n")[0], Push)
    assert parse(b"_\r\n")[0] is None
    assert parse(b"#t\r\n")[0] is True
    assert parse(b",1.5\r\n")[0] == 1.5
