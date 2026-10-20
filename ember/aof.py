"""Append-only file persistence.

Every write command that actually changed something is appended to the
log in RESP format. On startup the log is replayed to rebuild the dataset.

Relative expiries (EXPIRE 10, SET .. EX 10) are rewritten to absolute
PEXPIREAT deadlines before logging, otherwise replaying a day-old log
would give every key a fresh TTL.
"""

import logging
import os
import time
from collections import deque

from .commands import COMMANDS
from .db import CommandError
from .protocol import INCOMPLETE, ProtocolError, encode_command, parse

log = logging.getLogger("ember")

FSYNC_POLICIES = ("always", "everysec", "no")
REWRITE_MIN_SIZE = 1024 * 1024
BATCH = 64  # items per RPUSH/SADD/HSET when rewriting


class AOF:
    def __init__(self, path, fsync="everysec"):
        if fsync not in FSYNC_POLICIES:
            raise ValueError(f"appendfsync must be one of {FSYNC_POLICIES}")
        self.path = path
        self.fsync = fsync
        self.file = open(path, "ab")
        self.pending_sync = False
        self.last_sync = time.monotonic()
        self.base_size = max(os.path.getsize(path), REWRITE_MIN_SIZE)

    def log(self, db, name, args):
        for argv in translate(db, name, args):
            self.file.write(encode_command(argv))
        if self.fsync == "always":
            self._sync()
        else:
            self.pending_sync = True

    def _sync(self):
        self.file.flush()
        os.fsync(self.file.fileno())
        self.pending_sync = False
        self.last_sync = time.monotonic()

    def tick(self, db):
        """Called from the server cron every 100ms."""
        if self.fsync == "everysec" and self.pending_sync and time.monotonic() - self.last_sync >= 1:
            self._sync()
        elif self.fsync == "no" and self.pending_sync:
            self.file.flush()
            self.pending_sync = False

        # same rule as redis' auto-aof-rewrite-percentage 100
        self.file.flush()
        if self.file.tell() > 2 * self.base_size:
            self.rewrite(db)

    def load(self, db):
        if not os.path.exists(self.path):
            return 0
        with open(self.path, "rb") as f:
            buf = f.read()

        pos = count = 0
        while pos < len(buf):
            try:
                argv, new_pos = parse(buf, pos)
            except ProtocolError:
                argv = INCOMPLETE
            if argv is INCOMPLETE:
                # probably a crash halfway through a write; keep what we have
                log.warning("AOF has a truncated tail at byte %d, ignoring %d bytes", pos, len(buf) - pos)
                self._truncate(pos)
                break
            pos = new_pos

            name = argv[0].decode().upper()
            cmd = COMMANDS.get(name)
            if cmd is None:
                raise RuntimeError(f"unknown command in AOF: {name}")
            try:
                cmd.handler(db, argv[1:])
            except CommandError as e:
                log.warning("AOF command %s failed on replay: %s", name, e)
            count += 1
        return count

    def _truncate(self, size):
        self.file.close()
        with open(self.path, "r+b") as f:
            f.truncate(size)
        self.file = open(self.path, "ab")

    def rewrite(self, db):
        """Write the smallest log that rebuilds the current dataset, then swap it in.

        This blocks the event loop while it runs. Redis forks for this; we could
        too with os.fork, but then writes made during the rewrite need to be
        buffered and replayed onto the new file. Not worth it at this size.
        """
        tmp = self.path + ".rewrite"
        with open(tmp, "wb") as out:
            for key in db.keys():
                for argv in snapshot(db, key):
                    out.write(encode_command(argv))
            out.flush()
            os.fsync(out.fileno())

        self.file.close()
        os.replace(tmp, self.path)
        self.file = open(self.path, "ab")
        self.pending_sync = False
        self.base_size = max(os.path.getsize(self.path), REWRITE_MIN_SIZE)
        log.info("AOF rewritten, %d bytes", os.path.getsize(self.path))

    def close(self):
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()


def translate(db, name, args):
    """Turn a command into the form we want in the log."""
    if name in ("EXPIRE", "PEXPIRE", "EXPIREAT", "PEXPIREAT"):
        key = args[0]
        deadline = db.expires.get(key)
        if deadline is None:
            return [[b"DEL", key]]
        return [[b"PEXPIREAT", key, str(deadline).encode()]]

    if name == "SET":
        key, value, *opts = args
        upper = [o.upper() for o in opts]
        argv = [b"SET", key, value]
        if b"KEEPTTL" in upper:
            argv.append(b"KEEPTTL")
        out = [argv]
        deadline = db.expires.get(key)
        if deadline is not None and b"KEEPTTL" not in upper:
            out.append([b"PEXPIREAT", key, str(deadline).encode()])
        return out

    return [[name.encode(), *args]]


def snapshot(db, key):
    value = db.data[key]
    if isinstance(value, bytes):
        out = [[b"SET", key, value]]
    elif isinstance(value, deque):
        items = list(value)
        out = [[b"RPUSH", key, *items[i:i + BATCH]] for i in range(0, len(items), BATCH)]
    elif isinstance(value, set):
        items = sorted(value)
        out = [[b"SADD", key, *items[i:i + BATCH]] for i in range(0, len(items), BATCH)]
    elif isinstance(value, dict):
        pairs = [x for kv in value.items() for x in kv]
        step = BATCH * 2
        out = [[b"HSET", key, *pairs[i:i + step]] for i in range(0, len(pairs), step)]
    else:
        raise TypeError(type(value))

    deadline = db.expires.get(key)
    if deadline is not None:
        out.append([b"PEXPIREAT", key, str(deadline).encode()])
    return out
