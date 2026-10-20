import random
import time
from collections import deque


class CommandError(Exception):
    def __init__(self, message):
        if not message.split(" ", 1)[0].isupper():
            message = "ERR " + message
        super().__init__(message)


class WrongType(CommandError):
    def __init__(self):
        super().__init__("WRONGTYPE Operation against a key holding the wrong kind of value")


TYPE_NAMES = {bytes: "string", deque: "list", dict: "hash", set: "set"}


def now_ms():
    return int(time.time() * 1000)


class Database:
    """A single keyspace.

    Values are plain Python objects: bytes for strings, deque for lists,
    dict for hashes and set for sets. Expiry deadlines live in a separate
    dict (absolute unix ms), like Redis does it.

    Every mutation goes through touch(), which bumps a per-key version.
    WATCH compares those versions, and the server uses `dirty` to decide
    whether a command needs to be written to the AOF.
    """

    def __init__(self, clock=now_ms):
        self.data = {}
        self.expires = {}
        self.versions = {}
        self.dirty = 0
        self.flushes = 0
        self.clock = clock

    def touch(self, key):
        self.versions[key] = self.versions.get(key, 0) + 1
        self.dirty += 1

    def version(self, key):
        return self.versions.get(key, 0)

    def _expire_if_needed(self, key):
        deadline = self.expires.get(key)
        if deadline is not None and deadline <= self.clock():
            self.delete(key)
            return True
        return False

    def get(self, key, kind=None):
        if self._expire_if_needed(key):
            return None
        value = self.data.get(key)
        if value is not None and kind is not None and type(value) is not kind:
            raise WrongType()
        return value

    def get_or_create(self, key, kind):
        value = self.get(key, kind)
        if value is None:
            value = kind()
            self.data[key] = value
        return value

    def set(self, key, value, keep_ttl=False):
        self.data[key] = value
        if not keep_ttl:
            self.expires.pop(key, None)
        self.touch(key)

    def delete(self, key):
        if key not in self.data:
            return False
        del self.data[key]
        self.expires.pop(key, None)
        self.touch(key)
        return True

    def drop_if_empty(self, key):
        # redis never keeps empty lists/sets/hashes around
        value = self.data.get(key)
        if value is not None and not isinstance(value, bytes) and not value:
            self.delete(key)

    def exists(self, key):
        return self.get(key) is not None

    def set_expiry(self, key, at_ms):
        if not self.exists(key):
            return False
        if at_ms <= self.clock():
            self.delete(key)
        else:
            self.expires[key] = at_ms
            self.touch(key)
        return True

    def ttl_ms(self, key):
        if not self.exists(key):
            return -2
        deadline = self.expires.get(key)
        if deadline is None:
            return -1
        return max(deadline - self.clock(), 0)

    def keys(self):
        return [k for k in list(self.data) if not self._expire_if_needed(k)]

    def flush(self):
        self.data.clear()
        self.expires.clear()
        self.versions.clear()
        self.flushes += 1
        self.dirty += 1

    def type_of(self, key):
        value = self.get(key)
        return "none" if value is None else TYPE_NAMES[type(value)]

    def active_expire(self, sample_size=20, budget_ms=25):
        """Redis' probabilistic expiry: sample keys that have a TTL, delete the
        expired ones, and keep going while more than 25% of the sample was
        expired. Keeps memory from filling up with keys nobody reads again.
        """
        started = time.perf_counter()
        removed = 0
        while self.expires:
            sample = random.sample(list(self.expires), min(sample_size, len(self.expires)))
            expired = sum(1 for k in sample if self._expire_if_needed(k))
            removed += expired
            if expired <= len(sample) // 4:
                break
            if (time.perf_counter() - started) * 1000 > budget_ms:
                break
        return removed
