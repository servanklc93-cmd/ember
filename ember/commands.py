"""Keyspace commands.

Each handler gets the database and the arguments (without the command
name) and returns a value that protocol.encode() understands. Commands
that need connection state (MULTI, SUBSCRIBE, ...) live in server.py.
"""

from collections import deque
from dataclasses import dataclass
from fnmatch import fnmatchcase
from itertools import islice
from typing import Callable

from .db import CommandError
from .protocol import OK, PONG, Map, SetReply, SimpleString


@dataclass
class Command:
    name: str
    handler: Callable
    arity: int  # like redis: exact count incl. the name, negative means "at least"
    write: bool

    def check_arity(self, argc):
        if self.arity >= 0:
            return argc == self.arity
        return argc >= -self.arity


COMMANDS = {}


def command(name, arity, write=False):
    def register(fn):
        COMMANDS[name] = Command(name, fn, arity, write)
        return fn
    return register


def to_int(raw, what="value"):
    try:
        return int(raw)
    except ValueError:
        raise CommandError(f"{what} is not an integer or out of range") from None


def syntax_error():
    return CommandError("syntax error")


# --- generic ---------------------------------------------------------------

@command("PING", -1)
def ping(db, args):
    return args[0] if args else PONG


@command("ECHO", 2)
def echo(db, args):
    return args[0]


@command("DEL", -2, write=True)
def delete(db, args):
    return sum(db.delete(k) for k in args)


@command("EXISTS", -2)
def exists(db, args):
    return sum(db.exists(k) for k in args)


@command("TYPE", 2)
def type_(db, args):
    return SimpleString(db.type_of(args[0]))


@command("KEYS", 2)
def keys(db, args):
    pattern = args[0].decode("latin-1")
    return [k for k in db.keys() if fnmatchcase(k.decode("latin-1"), pattern)]


@command("DBSIZE", 1)
def dbsize(db, args):
    return len(db.data)


@command("FLUSHDB", -1, write=True)
def flushdb(db, args):
    db.flush()
    return OK


COMMANDS["FLUSHALL"] = Command("FLUSHALL", flushdb, -1, True)


@command("RENAME", 3, write=True)
def rename(db, args):
    src, dst = args
    value = db.get(src)
    if value is None:
        raise CommandError("no such key")
    deadline = db.expires.get(src)
    db.delete(src)
    db.set(dst, value)
    if deadline is not None:
        db.set_expiry(dst, deadline)
    return OK


# --- expiry ----------------------------------------------------------------

def _expire(db, key, at_ms):
    return int(db.set_expiry(key, at_ms))


@command("EXPIRE", 3, write=True)
def expire(db, args):
    return _expire(db, args[0], db.clock() + to_int(args[1]) * 1000)


@command("PEXPIRE", 3, write=True)
def pexpire(db, args):
    return _expire(db, args[0], db.clock() + to_int(args[1]))


@command("EXPIREAT", 3, write=True)
def expireat(db, args):
    return _expire(db, args[0], to_int(args[1]) * 1000)


@command("PEXPIREAT", 3, write=True)
def pexpireat(db, args):
    return _expire(db, args[0], to_int(args[1]))


@command("TTL", 2)
def ttl(db, args):
    ms = db.ttl_ms(args[0])
    return ms if ms < 0 else (ms + 500) // 1000


@command("PTTL", 2)
def pttl(db, args):
    return db.ttl_ms(args[0])


@command("PERSIST", 2, write=True)
def persist(db, args):
    key = args[0]
    if db.exists(key) and db.expires.pop(key, None) is not None:
        db.touch(key)
        return 1
    return 0


# --- strings ---------------------------------------------------------------

@command("GET", 2)
def get(db, args):
    return db.get(args[0], bytes)


@command("SET", -3, write=True)
def set_(db, args):
    key, value, *opts = args
    nx = xx = keep_ttl = return_old = False
    deadline = None

    i = 0
    while i < len(opts):
        opt = opts[i].upper()
        if opt == b"NX":
            nx = True
        elif opt == b"XX":
            xx = True
        elif opt == b"KEEPTTL":
            keep_ttl = True
        elif opt == b"GET":
            return_old = True
        elif opt in (b"EX", b"PX", b"EXAT", b"PXAT") and i + 1 < len(opts):
            n = to_int(opts[i + 1])
            if n <= 0:
                raise CommandError("invalid expire time in 'set' command")
            i += 1
            deadline = {
                b"EX": db.clock() + n * 1000,
                b"PX": db.clock() + n,
                b"EXAT": n * 1000,
                b"PXAT": n,
            }[opt]
        else:
            raise syntax_error()
        i += 1

    if nx and xx or keep_ttl and deadline is not None:
        raise syntax_error()

    old = db.get(key, bytes) if return_old else db.get(key)
    if (nx and old is not None) or (xx and old is None):
        return old if return_old else None

    db.set(key, value, keep_ttl=keep_ttl)
    if deadline is not None:
        db.set_expiry(key, deadline)
    return old if return_old else OK


@command("SETNX", 3, write=True)
def setnx(db, args):
    if db.exists(args[0]):
        return 0
    db.set(args[0], args[1])
    return 1


@command("GETDEL", 2, write=True)
def getdel(db, args):
    value = db.get(args[0], bytes)
    if value is not None:
        db.delete(args[0])
    return value


@command("MGET", -2)
def mget(db, args):
    out = []
    for key in args:
        value = db.get(key)
        out.append(value if isinstance(value, bytes) else None)
    return out


@command("MSET", -3, write=True)
def mset(db, args):
    if len(args) % 2:
        raise CommandError("wrong number of arguments for 'mset' command")
    for key, value in zip(args[::2], args[1::2]):
        db.set(key, value)
    return OK


@command("APPEND", 3, write=True)
def append(db, args):
    key, extra = args
    value = (db.get(key, bytes) or b"") + extra
    db.set(key, value, keep_ttl=True)
    return len(value)


@command("STRLEN", 2)
def strlen(db, args):
    return len(db.get(args[0], bytes) or b"")


def _incr(db, key, by):
    current = db.get(key, bytes)
    n = to_int(current) if current is not None else 0
    n += by
    if not -(2 ** 63) <= n < 2 ** 63:
        raise CommandError("increment or decrement would overflow")
    db.set(key, str(n).encode(), keep_ttl=True)
    return n


@command("INCR", 2, write=True)
def incr(db, args):
    return _incr(db, args[0], 1)


@command("DECR", 2, write=True)
def decr(db, args):
    return _incr(db, args[0], -1)


@command("INCRBY", 3, write=True)
def incrby(db, args):
    return _incr(db, args[0], to_int(args[1]))


@command("DECRBY", 3, write=True)
def decrby(db, args):
    return _incr(db, args[0], -to_int(args[1]))


# --- lists -----------------------------------------------------------------

def _push(db, args, left):
    key, *values = args
    lst = db.get_or_create(key, deque)
    if left:
        lst.extendleft(values)
    else:
        lst.extend(values)
    db.touch(key)
    return len(lst)


@command("LPUSH", -3, write=True)
def lpush(db, args):
    return _push(db, args, left=True)


@command("RPUSH", -3, write=True)
def rpush(db, args):
    return _push(db, args, left=False)


def _pop(db, args, left):
    if len(args) > 2:
        raise syntax_error()
    key = args[0]
    lst = db.get(key, deque)
    count = to_int(args[1]) if len(args) == 2 else None
    if count is not None and count < 0:
        raise CommandError("value is out of range, must be positive")
    if lst is None:
        return None

    take = lst.popleft if left else lst.pop
    if count is None:
        out = take()
    else:
        out = [take() for _ in range(min(count, len(lst)))]
    db.touch(key)
    db.drop_if_empty(key)
    return out


@command("LPOP", -2, write=True)
def lpop(db, args):
    return _pop(db, args, left=True)


@command("RPOP", -2, write=True)
def rpop(db, args):
    return _pop(db, args, left=False)


@command("LLEN", 2)
def llen(db, args):
    lst = db.get(args[0], deque)
    return len(lst) if lst else 0


def _normalize_range(start, stop, length):
    if start < 0:
        start = max(length + start, 0)
    if stop < 0:
        stop = length + stop
    return start, min(stop, length - 1)


@command("LRANGE", 4)
def lrange(db, args):
    lst = db.get(args[0], deque)
    if not lst:
        return []
    start, stop = _normalize_range(to_int(args[1]), to_int(args[2]), len(lst))
    if start > stop:
        return []
    return list(islice(lst, start, stop + 1))


@command("LINDEX", 3)
def lindex(db, args):
    lst = db.get(args[0], deque)
    i = to_int(args[1])
    if not lst or not -len(lst) <= i < len(lst):
        return None
    return lst[i]


# --- hashes ----------------------------------------------------------------

@command("HSET", -4, write=True)
def hset(db, args):
    key, *pairs = args
    if len(pairs) % 2:
        raise CommandError("wrong number of arguments for 'hset' command")
    h = db.get_or_create(key, dict)
    added = 0
    for field, value in zip(pairs[::2], pairs[1::2]):
        added += field not in h
        h[field] = value
    db.touch(key)
    return added


@command("HMSET", -4, write=True)
def hmset(db, args):
    hset(db, args)
    return OK


@command("HGET", 3)
def hget(db, args):
    h = db.get(args[0], dict)
    return h.get(args[1]) if h else None


@command("HMGET", -3)
def hmget(db, args):
    h = db.get(args[0], dict) or {}
    return [h.get(f) for f in args[1:]]


@command("HDEL", -3, write=True)
def hdel(db, args):
    key, *fields = args
    h = db.get(key, dict)
    if not h:
        return 0
    removed = sum(h.pop(f, None) is not None for f in fields)
    if removed:
        db.touch(key)
        db.drop_if_empty(key)
    return removed


@command("HGETALL", 2)
def hgetall(db, args):
    h = db.get(args[0], dict) or {}
    return Map(x for pair in h.items() for x in pair)


@command("HKEYS", 2)
def hkeys(db, args):
    return list(db.get(args[0], dict) or {})


@command("HVALS", 2)
def hvals(db, args):
    return list((db.get(args[0], dict) or {}).values())


@command("HLEN", 2)
def hlen(db, args):
    return len(db.get(args[0], dict) or {})


@command("HEXISTS", 3)
def hexists(db, args):
    return int(args[1] in (db.get(args[0], dict) or {}))


@command("HINCRBY", 4, write=True)
def hincrby(db, args):
    key, field, by = args
    h = db.get_or_create(key, dict)
    n = to_int(h.get(field, b"0"), "hash value") + to_int(by)
    h[field] = str(n).encode()
    db.touch(key)
    return n


# --- sets ------------------------------------------------------------------

@command("SADD", -3, write=True)
def sadd(db, args):
    key, *members = args
    s = db.get_or_create(key, set)
    before = len(s)
    s.update(members)
    if len(s) != before:
        db.touch(key)
    return len(s) - before


@command("SREM", -3, write=True)
def srem(db, args):
    key, *members = args
    s = db.get(key, set)
    if not s:
        return 0
    before = len(s)
    s.difference_update(members)
    if len(s) != before:
        db.touch(key)
        db.drop_if_empty(key)
    return before - len(s)


@command("SMEMBERS", 2)
def smembers(db, args):
    return SetReply(sorted(db.get(args[0], set) or ()))


@command("SISMEMBER", 3)
def sismember(db, args):
    return int(args[1] in (db.get(args[0], set) or ()))


@command("SCARD", 2)
def scard(db, args):
    return len(db.get(args[0], set) or ())


def _sets(db, keys):
    return [db.get(k, set) or set() for k in keys]


@command("SINTER", -2)
def sinter(db, args):
    return SetReply(sorted(set.intersection(*_sets(db, args))))


@command("SUNION", -2)
def sunion(db, args):
    return SetReply(sorted(set.union(*_sets(db, args))))


@command("SDIFF", -2)
def sdiff(db, args):
    first, *rest = _sets(db, args)
    return SetReply(sorted(first.difference(*rest)))
