from ember.protocol import Map, Push, SetReply, encode_command

from tests.conftest import RunningServer


def test_basic_roundtrip(conn):
    assert conn("PING") == "PONG"
    assert conn("SET", "k", "v") == "OK"
    assert conn("GET", "k") == b"v"


def test_errors_are_replies_not_disconnects(conn):
    assert conn("NOPE").startswith("ERR unknown command")
    assert conn("GET").startswith("ERR wrong number of arguments")
    assert conn("PING") == "PONG"


def test_pipelining(conn):
    payload = b"".join(encode_command(["INCR", "n"]) for _ in range(1000))
    conn.sock.sendall(payload)
    replies = [conn.read() for _ in range(1000)]
    assert replies == list(range(1, 1001))


def test_command_split_across_packets(conn):
    raw = encode_command(["SET", "split", "value"])
    for byte in raw:
        conn.sock.sendall(bytes([byte]))
    assert conn.read() == "OK"
    assert conn("GET", "split") == b"value"


def test_inline_commands(conn):
    conn.sock.sendall(b"SET inline works\r\n")
    assert conn.read() == "OK"
    assert conn("GET", "inline") == b"works"


def test_multi_exec(conn):
    assert conn("MULTI") == "OK"
    assert conn("INCR", "c") == "QUEUED"
    assert conn("INCR", "c") == "QUEUED"
    assert conn("EXEC") == [1, 2]


def test_exec_aborts_after_queueing_error(conn):
    conn("MULTI")
    conn("SET", "a", "1")
    assert conn("NOTACOMMAND").startswith("ERR")
    assert conn("EXEC").startswith("EXECABORT")
    assert conn("GET", "a") is None


def test_runtime_errors_inside_exec_dont_abort(conn):
    conn("SET", "s", "text")
    conn("MULTI")
    conn("INCR", "s")
    conn("SET", "after", "ok")
    result = conn("EXEC")
    assert result[0].startswith("ERR")
    assert result[1] == "OK"


def test_watch_aborts_when_key_changes(server):
    a, b = server.connect(), server.connect()
    a("SET", "balance", "100")
    a("WATCH", "balance")
    b("SET", "balance", "50")
    a("MULTI")
    a("SET", "balance", "200")
    assert a("EXEC") is None
    assert a("GET", "balance") == b"50"


def test_watch_passes_when_untouched(server):
    a, b = server.connect(), server.connect()
    a("WATCH", "x")
    b("SET", "other", "1")
    a("MULTI")
    a("SET", "x", "1")
    assert a("EXEC") == ["OK"]


def test_pubsub(server):
    sub, pub = server.connect(), server.connect()
    assert sub("SUBSCRIBE", "chat") == [b"subscribe", b"chat", 1]
    assert sub("PSUBSCRIBE", "chat.*") == [b"psubscribe", b"chat.*", 2]

    assert pub("PUBLISH", "chat", "hi") == 1
    assert sub.read() == [b"message", b"chat", b"hi"]

    assert pub("PUBLISH", "chat.room1", "yo") == 1
    assert sub.read() == [b"pmessage", b"chat.*", b"chat.room1", b"yo"]

    assert sub("GET", "x").startswith("ERR Can't execute 'get'")
    assert sub("PING") == [b"pong", b""]


def test_unsubscribe_on_disconnect(server):
    sub, pub = server.connect(), server.connect()
    sub("SUBSCRIBE", "chat")
    sub.close()
    # give the server a moment to notice
    for _ in range(50):
        if pub("PUBLISH", "chat", "anyone?") == 0:
            break
    assert pub("PUBLISH", "chat", "anyone?") == 0


def test_aof_survives_restart(tmp_path):
    path = str(tmp_path / "data.aof")

    srv = RunningServer(path).start()
    c = srv.connect()
    c("SET", "name", "ember", "EX", "1000")
    c("RPUSH", "list", "a", "b")
    c("HSET", "h", "f", "v")
    c("SADD", "s", "x")
    c("INCRBY", "n", "41")
    c("INCR", "n")
    c("SET", "gone", "x")
    c("DEL", "gone")
    c("SET", "nx", "1", "NX")
    c("SET", "nx", "2", "NX")  # no-op, should not be logged
    srv.stop()

    srv = RunningServer(path).start()
    c = srv.connect()
    assert c("GET", "name") == b"ember"
    assert 990 < c("TTL", "name") <= 1000
    assert c("LRANGE", "list", "0", "-1") == [b"a", b"b"]
    assert c("HGET", "h", "f") == b"v"
    assert c("SMEMBERS", "s") == [b"x"]
    assert c("GET", "n") == b"42"
    assert c("EXISTS", "gone") == 0
    assert c("GET", "nx") == b"1"
    srv.stop()


def test_aof_rewrite_compacts(tmp_path):
    path = tmp_path / "data.aof"
    srv = RunningServer(str(path)).start()
    c = srv.connect()
    for _ in range(500):
        c("INCR", "counter")
    before = path.stat().st_size
    c("BGREWRITEAOF")
    after = path.stat().st_size
    srv.stop()

    assert after < before / 50
    srv = RunningServer(str(path)).start()
    assert srv.connect()("GET", "counter") == b"500"
    srv.stop()


def test_truncated_aof_is_repaired(tmp_path):
    path = tmp_path / "data.aof"
    good = encode_command(["SET", "a", "1"])
    path.write_bytes(good + encode_command(["SET", "b", "2"])[:-5])

    srv = RunningServer(str(path)).start()
    c = srv.connect()
    assert c("GET", "a") == b"1"
    assert c("GET", "b") is None
    srv.stop()
    assert path.read_bytes() == good


def test_hello_switches_to_resp3(conn):
    info = conn("HELLO", "3")
    assert isinstance(info, Map)
    fields = dict(zip(info[::2], info[1::2]))
    assert fields[b"proto"] == 3 and fields[b"server"] == b"ember"

    conn("HSET", "h", "f", "v")
    assert isinstance(conn("HGETALL", "h"), Map)
    conn("SADD", "s", "m")
    assert isinstance(conn("SMEMBERS", "s"), SetReply)
    conn.sock.sendall(encode_command(["GET", "missing"]))
    assert conn.sock.recv(16) == b"_\r\n"


def test_hello_rejects_unknown_version(conn):
    assert conn("HELLO", "4").startswith("NOPROTO")
    assert conn("HELLO")[5] == 2  # still on RESP2


def test_resp3_pubsub_uses_push_and_allows_commands(server):
    sub, pub = server.connect(), server.connect()
    sub("HELLO", "3")
    assert isinstance(sub("SUBSCRIBE", "news"), Push)
    # in RESP3 a subscribed connection can still run normal commands
    assert sub("SET", "k", "v") == "OK"
    pub("PUBLISH", "news", "hi")
    msg = sub.read()
    assert isinstance(msg, Push) and msg == [b"message", b"news", b"hi"]


def test_watch_abort_is_null_in_resp3(server):
    a, b = server.connect(), server.connect()
    a("HELLO", "3")
    a("WATCH", "k")
    b("SET", "k", "1")
    a("MULTI")
    a("SET", "k", "2")
    a.sock.sendall(encode_command(["EXEC"]))
    assert a.sock.recv(16) == b"_\r\n"


def test_aof_rewrites_itself_when_it_doubles(tmp_path, monkeypatch):
    import ember.aof
    monkeypatch.setattr(ember.aof, "REWRITE_MIN_SIZE", 10_000)
    path = tmp_path / "data.aof"

    srv = RunningServer(str(path)).start()
    c = srv.connect()
    for i in range(200):
        c("SET", "key", "x" * 500)  # same key over and over, mostly garbage in the log
    # the cron runs every 100ms
    import time
    for _ in range(30):
        if path.stat().st_size < 5_000:
            break
        time.sleep(0.1)
    assert path.stat().st_size < 5_000
    assert c("GET", "key") == b"x" * 500
    srv.stop()
