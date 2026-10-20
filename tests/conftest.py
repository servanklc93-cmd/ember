import asyncio
import socket
import threading

import pytest

from ember.aof import AOF
from ember.protocol import INCOMPLETE, encode_command, parse
from ember.server import Server


class Conn:
    """Minimal blocking client, good enough for tests."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        self.buf = b""

    def send(self, *args):
        self.sock.sendall(encode_command(args))

    def read(self):
        while True:
            value, pos = parse(self.buf)
            if value is not INCOMPLETE:
                self.buf = self.buf[pos:]
                return value
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed the connection")
            self.buf += chunk

    def __call__(self, *args):
        self.send(*args)
        return self.read()

    def close(self):
        self.sock.close()


class RunningServer:
    def __init__(self, aof_path=None, fsync="always"):
        self.aof_path = aof_path
        self.fsync = fsync
        self.conns = []

    def start(self):
        aof = AOF(self.aof_path, self.fsync) if self.aof_path else None
        self.server = Server(port=0, aof=aof)
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.server.start())
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        ready.wait(5)
        return self

    def connect(self):
        conn = Conn(self.server.port)
        self.conns.append(conn)
        return conn

    def stop(self):
        for conn in self.conns:
            conn.close()
        asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


@pytest.fixture
def server():
    srv = RunningServer().start()
    yield srv
    srv.stop()


@pytest.fixture
def conn(server):
    return server.connect()
