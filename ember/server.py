import asyncio
import logging
import os
import resource
import time
from fnmatch import fnmatchcase

from . import __version__
from .commands import COMMANDS
from .db import CommandError, Database
from .protocol import (
    INCOMPLETE, OK, PONG, QUEUED, ErrorReply, ProtocolError, SimpleString,
    encode, parse,
)

log = logging.getLogger("ember")

# commands a client may still send while subscribed
PUBSUB_ALLOWED = {"SUBSCRIBE", "UNSUBSCRIBE", "PSUBSCRIBE", "PUNSUBSCRIBE", "PING", "QUIT", "RESET"}


class Client:
    def __init__(self, server, reader, writer):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.addr = writer.get_extra_info("peername")
        self.channels = set()
        self.patterns = set()
        self.queue = None      # list while inside MULTI
        self.queue_failed = False
        self.watched = {}
        self.closing = False

    @property
    def subscribed(self):
        return bool(self.channels or self.patterns)

    async def serve(self):
        buf = bytearray()
        try:
            while not self.closing:
                data = await self.reader.read(65536)
                if not data:
                    break
                buf += data

                out = []
                pos = 0
                while not self.closing:
                    argv, pos = parse(buf, pos)
                    if argv is INCOMPLETE:
                        break
                    reply = self.execute(argv)
                    if reply is not None:
                        out.append(reply)
                del buf[:pos]

                # one write per read, so pipelined commands get one syscall
                if out:
                    self.writer.write(b"".join(out))
                    await self.writer.drain()
        except ProtocolError as e:
            self.writer.write(encode(ErrorReply(f"ERR Protocol error: {e}")))
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.server.disconnect(self)
            self.writer.close()

    def execute(self, argv):
        if not isinstance(argv, list) or not argv:
            return None
        try:
            name = argv[0].decode().upper()
        except (UnicodeDecodeError, AttributeError):
            return encode(ErrorReply("ERR unknown command"))
        args = argv[1:]

        if self.subscribed and name not in PUBSUB_ALLOWED:
            return encode(ErrorReply(
                f"ERR Can't execute '{name.lower()}': only (P)SUBSCRIBE / (P)UNSUBSCRIBE / PING / QUIT / RESET are allowed in this context"))

        special = getattr(self, "cmd_" + name.lower(), None)
        if special is not None and name in CLIENT_COMMANDS:
            if self.queue is not None and name not in ("EXEC", "DISCARD", "MULTI", "WATCH"):
                self.queue.append(argv)
                return encode(QUEUED)
            try:
                return special(args)
            except CommandError as e:
                return encode(ErrorReply(str(e)))

        cmd = COMMANDS.get(name)
        if cmd is None:
            self._fail_transaction()
            return encode(ErrorReply(f"ERR unknown command '{argv[0].decode(errors='replace')}'"))
        if not cmd.check_arity(len(argv)):
            self._fail_transaction()
            return encode(ErrorReply(f"ERR wrong number of arguments for '{name.lower()}' command"))

        if self.queue is not None:
            self.queue.append(argv)
            return encode(QUEUED)

        return encode(self.server.call(cmd, args))

    def _fail_transaction(self):
        if self.queue is not None:
            self.queue_failed = True

    # --- connection-level commands -----------------------------------------

    def cmd_quit(self, args):
        self.closing = True
        return encode(OK)

    def cmd_ping(self, args):
        if self.subscribed:
            return encode([b"pong", args[0] if args else b""])
        return encode(args[0] if args else PONG)

    def cmd_select(self, args):
        if args != [b"0"]:
            raise CommandError("ember only has database 0")
        return encode(OK)

    def cmd_reset(self, args):
        self.server.unsubscribe_all(self)
        self.queue = None
        self.watched.clear()
        return encode(SimpleString("RESET"))

    def cmd_multi(self, args):
        if self.queue is not None:
            raise CommandError("MULTI calls can not be nested")
        self.queue = []
        self.queue_failed = False
        return encode(OK)

    def cmd_discard(self, args):
        if self.queue is None:
            raise CommandError("DISCARD without MULTI")
        self.queue = None
        self.watched.clear()
        return encode(OK)

    def cmd_watch(self, args):
        if self.queue is not None:
            raise CommandError("WATCH inside MULTI is not allowed")
        db = self.server.db
        for key in args:
            self.watched.setdefault(key, (db.version(key), db.flushes))
        return encode(OK)

    def cmd_unwatch(self, args):
        self.watched.clear()
        return encode(OK)

    def cmd_exec(self, args):
        if self.queue is None:
            raise CommandError("EXEC without MULTI")
        queue, self.queue = self.queue, None
        watched, self.watched = self.watched, {}

        if self.queue_failed:
            return encode(ErrorReply("EXECABORT Transaction discarded because of previous errors."))

        db = self.server.db
        for key, (version, flushes) in watched.items():
            # reading the key first lets a lazily expired key count as modified
            db.get(key)
            if db.version(key) != version or db.flushes != flushes:
                return b"*-1\r\n"

        # nothing awaits in here, so no other client can run in between
        replies = [self.execute(argv) for argv in queue]
        return b"*%d\r\n" % len(replies) + b"".join(replies)

    def cmd_subscribe(self, args):
        return self._subscribe(args, self.channels, self.server.channels, b"subscribe")

    def cmd_psubscribe(self, args):
        return self._subscribe(args, self.patterns, self.server.patterns, b"psubscribe")

    def cmd_unsubscribe(self, args):
        return self._unsubscribe(args, self.channels, self.server.channels, b"unsubscribe")

    def cmd_punsubscribe(self, args):
        return self._unsubscribe(args, self.patterns, self.server.patterns, b"punsubscribe")

    def _subscribe(self, names, mine, registry, kind):
        if not names:
            raise CommandError(f"wrong number of arguments for '{kind.decode()}' command")
        out = []
        for name in names:
            mine.add(name)
            registry.setdefault(name, set()).add(self)
            out.append(encode([kind, name, len(self.channels) + len(self.patterns)]))
        return b"".join(out)

    def _unsubscribe(self, names, mine, registry, kind):
        names = names or sorted(mine)
        if not names:
            return encode([kind, None, 0])
        out = []
        for name in names:
            mine.discard(name)
            subs = registry.get(name)
            if subs:
                subs.discard(self)
                if not subs:
                    del registry[name]
            out.append(encode([kind, name, len(self.channels) + len(self.patterns)]))
        return b"".join(out)

    def cmd_publish(self, args):
        if len(args) != 2:
            raise CommandError("wrong number of arguments for 'publish' command")
        return encode(self.server.publish(*args))

    def cmd_info(self, args):
        return encode(self.server.info())

    def cmd_config(self, args):
        # redis-cli and redis-benchmark poke at CONFIG on startup; keep them happy
        if args and args[0].upper() == b"GET":
            return encode([])
        return encode(OK)

    def cmd_command(self, args):
        return encode([])

    def cmd_client(self, args):
        return encode(OK)

    def cmd_bgrewriteaof(self, args):
        if self.server.aof is None:
            raise CommandError("AOF is not enabled")
        self.server.aof.rewrite(self.server.db)
        return encode(SimpleString("Background append only file rewriting started"))


CLIENT_COMMANDS = {
    "QUIT", "PING", "SELECT", "RESET", "MULTI", "DISCARD", "WATCH", "UNWATCH",
    "EXEC", "SUBSCRIBE", "PSUBSCRIBE", "UNSUBSCRIBE", "PUNSUBSCRIBE", "PUBLISH",
    "INFO", "CONFIG", "COMMAND", "CLIENT", "BGREWRITEAOF",
}


class Server:
    def __init__(self, host="127.0.0.1", port=6380, aof=None):
        self.host = host
        self.port = port
        self.db = Database()
        self.aof = aof
        self.clients = set()
        self.channels = {}
        self.patterns = {}
        self.started = time.time()
        self.commands_processed = 0
        self._server = None
        self._tasks = []

    def call(self, cmd, args):
        self.commands_processed += 1
        dirty = self.db.dirty
        try:
            result = cmd.handler(self.db, args)
        except CommandError as e:
            return ErrorReply(str(e))
        if self.aof is not None and cmd.write and self.db.dirty != dirty:
            self.aof.log(self.db, cmd.name, args)
        return result

    def publish(self, channel, message):
        receivers = 0
        for client in self.channels.get(channel, ()):
            client.writer.write(encode([b"message", channel, message]))
            receivers += 1
        text = channel.decode("latin-1")
        for pattern, clients in self.patterns.items():
            if fnmatchcase(text, pattern.decode("latin-1")):
                for client in clients:
                    client.writer.write(encode([b"pmessage", pattern, channel, message]))
                    receivers += 1
        return receivers

    def unsubscribe_all(self, client):
        for name in client.channels:
            self.channels.get(name, set()).discard(client)
        for name in client.patterns:
            self.patterns.get(name, set()).discard(client)
        self.channels = {k: v for k, v in self.channels.items() if v}
        self.patterns = {k: v for k, v in self.patterns.items() if v}
        client.channels.clear()
        client.patterns.clear()

    def disconnect(self, client):
        self.unsubscribe_all(client)
        self.clients.discard(client)

    def info(self):
        usage = resource.getrusage(resource.RUSAGE_SELF)
        lines = [
            "# Server",
            f"ember_version:{__version__}",
            f"process_id:{os.getpid()}",
            f"tcp_port:{self.port}",
            f"uptime_in_seconds:{int(time.time() - self.started)}",
            "",
            "# Clients",
            f"connected_clients:{len(self.clients)}",
            "",
            "# Memory",
            f"max_rss_kb:{usage.ru_maxrss // 1024 if os.uname().sysname == 'Darwin' else usage.ru_maxrss}",
            "",
            "# Persistence",
            f"aof_enabled:{int(self.aof is not None)}",
            "",
            "# Stats",
            f"total_commands_processed:{self.commands_processed}",
            "",
            "# Keyspace",
            f"db0:keys={len(self.db.data)},expires={len(self.db.expires)}",
        ]
        return "\r\n".join(lines) + "\r\n"

    async def _handle(self, reader, writer):
        client = Client(self, reader, writer)
        self.clients.add(client)
        await client.serve()

    async def _cron(self):
        # roughly what redis' serverCron does, minus the parts we don't have
        while True:
            await asyncio.sleep(0.1)
            self.db.active_expire()
            if self.aof is not None:
                self.aof.tick(self.db)

    async def start(self):
        if self.aof is not None:
            loaded = self.aof.load(self.db)
            log.info("loaded %d commands from %s", loaded, self.aof.path)
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._tasks.append(asyncio.create_task(self._cron()))
        log.info("ember %s listening on %s:%d", __version__, self.host, self.port)

    async def serve_forever(self):
        await self.start()
        async with self._server:
            await self._server.serve_forever()

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for client in list(self.clients):
            client.writer.close()
        if self.aof is not None:
            self.aof.close()
