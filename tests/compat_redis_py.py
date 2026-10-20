"""Smoke test with the official redis-py client. Run against a live server:

    python -m ember --port 6390 &
    python tests/compat_redis_py.py 6390
"""
import sys
import threading

import redis

port = int(sys.argv[1]) if len(sys.argv) > 1 else 6390

for proto in (2, 3):
    r = redis.Redis(port=port, protocol=proto)
    r.flushdb()
    assert r.set("a", "1") and r.get("a") == b"1"
    r.hset("h", mapping={"x": 1, "y": 2})
    assert r.hgetall("h") == {b"x": b"1", b"y": b"2"}
    assert r.smembers("nope") == set()

    pipe = r.pipeline(transaction=True)
    pipe.incr("c").incr("c").get("c")
    assert pipe.execute() == [1, 2, b"2"]

    def bump():
        with r.pipeline() as p:
            while True:
                try:
                    p.watch("bal")
                    value = int(p.get("bal") or 0)
                    p.multi()
                    p.set("bal", value + 1)
                    p.execute()
                    return
                except redis.WatchError:
                    pass

    threads = [threading.Thread(target=lambda: [bump() for _ in range(20)]) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert r.get("bal") == b"100", r.get("bal")

    ps = r.pubsub()
    ps.subscribe("ch")
    ps.get_message(timeout=1)
    r.publish("ch", "hi")
    assert ps.get_message(timeout=1)["data"] == b"hi"
    ps.close()
    print(f"redis-py {redis.__version__} with RESP{proto}: ok")
