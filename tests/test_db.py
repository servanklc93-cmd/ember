from collections import deque

import pytest

from ember.db import Database, WrongType


class FakeClock:
    def __init__(self, t=1_000_000):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def db(clock):
    return Database(clock=clock)


def test_set_get_delete(db):
    db.set(b"k", b"v")
    assert db.get(b"k") == b"v"
    assert db.delete(b"k") is True
    assert db.delete(b"k") is False
    assert db.get(b"k") is None


def test_wrong_type(db):
    db.set(b"k", b"v")
    with pytest.raises(WrongType):
        db.get(b"k", deque)


def test_lazy_expiry(db, clock):
    db.set(b"k", b"v")
    db.set_expiry(b"k", clock.t + 1000)
    assert db.ttl_ms(b"k") == 1000
    clock.t += 1000
    assert db.get(b"k") is None
    assert db.ttl_ms(b"k") == -2


def test_set_clears_ttl_unless_kept(db, clock):
    db.set(b"k", b"v")
    db.set_expiry(b"k", clock.t + 1000)
    db.set(b"k", b"v2", keep_ttl=True)
    assert db.ttl_ms(b"k") == 1000
    db.set(b"k", b"v3")
    assert db.ttl_ms(b"k") == -1


def test_expiry_in_the_past_deletes(db, clock):
    db.set(b"k", b"v")
    db.set_expiry(b"k", clock.t - 1)
    assert not db.exists(b"k")


def test_active_expire_removes_unread_keys(db, clock):
    for i in range(500):
        db.set(b"k%d" % i, b"v")
        db.set_expiry(b"k%d" % i, clock.t + 10)
    db.set(b"forever", b"v")
    clock.t += 10

    db.active_expire()
    # sampling stops once the hit rate drops, but with every key
    # expired it should get through almost all of them
    assert len(db.expires) < 25
    assert db.exists(b"forever")


def test_versions_change_on_write(db):
    v0 = db.version(b"k")
    db.set(b"k", b"v")
    v1 = db.version(b"k")
    db.delete(b"k")
    assert v0 < v1 < db.version(b"k")


def test_empty_containers_are_dropped(db):
    lst = db.get_or_create(b"l", deque)
    lst.append(b"x")
    lst.pop()
    db.drop_if_empty(b"l")
    assert db.type_of(b"l") == "none"
