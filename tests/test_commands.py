import pytest

from ember.commands import COMMANDS
from ember.db import CommandError, Database
from tests.test_db import FakeClock


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def run(clock):
    db = Database(clock=clock)

    def call(*parts):
        name, *args = parts
        args = [a.encode() if isinstance(a, str) else a for a in args]
        return COMMANDS[name].handler(db, args)

    call.db = db
    return call


def test_set_get(run):
    assert run("SET", "k", "v") == "OK"
    assert run("GET", "k") == b"v"
    assert run("GET", "missing") is None


def test_set_nx_xx(run):
    assert run("SET", "k", "1", "XX") is None
    assert run("SET", "k", "1", "NX") == "OK"
    assert run("SET", "k", "2", "NX") is None
    assert run("SET", "k", "3", "XX") == "OK"
    assert run("GET", "k") == b"3"


def test_set_get_option_returns_old_value(run):
    run("SET", "k", "old")
    assert run("SET", "k", "new", "GET") == b"old"
    assert run("GET", "k") == b"new"


def test_set_with_expiry(run, clock):
    run("SET", "k", "v", "EX", "10")
    assert run("TTL", "k") == 10
    clock.t += 10_000
    assert run("GET", "k") is None


def test_set_rejects_bad_options(run):
    with pytest.raises(CommandError, match="syntax"):
        run("SET", "k", "v", "NX", "XX")
    with pytest.raises(CommandError, match="syntax"):
        run("SET", "k", "v", "BOGUS")
    with pytest.raises(CommandError, match="invalid expire"):
        run("SET", "k", "v", "EX", "0")


def test_incr_family(run):
    assert run("INCR", "n") == 1
    assert run("INCRBY", "n", "10") == 11
    assert run("DECRBY", "n", "20") == -9
    assert run("GET", "n") == b"-9"
    run("SET", "s", "abc")
    with pytest.raises(CommandError, match="not an integer"):
        run("INCR", "s")


def test_incr_overflow(run):
    run("SET", "n", str(2 ** 63 - 1))
    with pytest.raises(CommandError, match="overflow"):
        run("INCR", "n")


def test_incr_keeps_ttl(run):
    run("SET", "n", "1", "EX", "100")
    run("INCR", "n")
    assert run("TTL", "n") == 100


def test_wrongtype(run):
    run("LPUSH", "l", "a")
    with pytest.raises(CommandError, match="WRONGTYPE"):
        run("GET", "l")
    assert run("MGET", "l", "nope") == [None, None]


def test_mset_mget_append_strlen(run):
    run("MSET", "a", "1", "b", "2")
    assert run("MGET", "a", "b", "c") == [b"1", b"2", None]
    assert run("APPEND", "a", "23") == 3
    assert run("STRLEN", "a") == 3


def test_expire_ttl_persist(run, clock):
    run("SET", "k", "v")
    assert run("TTL", "k") == -1
    assert run("TTL", "nope") == -2
    assert run("EXPIRE", "k", "5") == 1
    assert run("PTTL", "k") == 5000
    assert run("PERSIST", "k") == 1
    assert run("TTL", "k") == -1
    assert run("EXPIRE", "nope", "5") == 0


def test_negative_expire_deletes(run):
    run("SET", "k", "v")
    run("EXPIRE", "k", "-1")
    assert run("EXISTS", "k") == 0


def test_lists(run):
    assert run("RPUSH", "l", "a", "b", "c") == 3
    assert run("LPUSH", "l", "z") == 4
    assert run("LRANGE", "l", "0", "-1") == [b"z", b"a", b"b", b"c"]
    assert run("LRANGE", "l", "-2", "100") == [b"b", b"c"]
    assert run("LRANGE", "l", "3", "1") == []
    assert run("LINDEX", "l", "-1") == b"c"
    assert run("LPOP", "l") == b"z"
    assert run("RPOP", "l", "2") == [b"c", b"b"]
    assert run("LLEN", "l") == 1


def test_lpush_order_matches_redis(run):
    run("LPUSH", "l", "1", "2", "3")
    assert run("LRANGE", "l", "0", "-1") == [b"3", b"2", b"1"]


def test_popping_last_element_removes_key(run):
    run("RPUSH", "l", "x")
    run("LPOP", "l")
    assert run("TYPE", "l") == "none"
    assert run("LPOP", "l") is None


def test_hashes(run):
    assert run("HSET", "h", "a", "1", "b", "2") == 2
    assert run("HSET", "h", "a", "9") == 0
    assert run("HGET", "h", "a") == b"9"
    assert run("HMGET", "h", "a", "x") == [b"9", None]
    assert run("HLEN", "h") == 2
    assert run("HINCRBY", "h", "b", "5") == 7
    assert sorted(run("HGETALL", "h")) == sorted([b"a", b"9", b"b", b"7"])
    assert run("HDEL", "h", "a", "b", "nope") == 2
    assert run("EXISTS", "h") == 0


def test_sets(run):
    assert run("SADD", "s", "a", "b", "a") == 2
    assert run("SADD", "t", "b", "c") == 2
    assert run("SISMEMBER", "s", "a") == 1
    assert run("SINTER", "s", "t") == [b"b"]
    assert run("SUNION", "s", "t") == [b"a", b"b", b"c"]
    assert run("SDIFF", "s", "t") == [b"a"]
    assert run("SREM", "s", "a", "b") == 2
    assert run("SCARD", "s") == 0


def test_keys_pattern(run):
    run("MSET", "user:1", "a", "user:2", "b", "post:1", "c")
    assert sorted(run("KEYS", "user:*")) == [b"user:1", b"user:2"]
    assert run("KEYS", "user:?") == run("KEYS", "user:[12]")


def test_rename_keeps_ttl(run):
    run("SET", "a", "v", "EX", "50")
    run("RENAME", "a", "b")
    assert run("GET", "b") == b"v"
    assert run("TTL", "b") == 50
    with pytest.raises(CommandError, match="no such key"):
        run("RENAME", "a", "c")


def test_arity():
    assert COMMANDS["GET"].check_arity(2)
    assert not COMMANDS["GET"].check_arity(3)
    assert COMMANDS["DEL"].check_arity(5)
    assert not COMMANDS["DEL"].check_arity(1)
