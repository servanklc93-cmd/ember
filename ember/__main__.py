import argparse
import asyncio
import logging

from .aof import AOF, FSYNC_POLICIES
from .server import Server


def main(argv=None):
    p = argparse.ArgumentParser(prog="ember", description="Redis-compatible server in pure Python")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6380)
    p.add_argument("--aof", metavar="PATH", help="enable append-only file persistence")
    p.add_argument("--appendfsync", choices=FSYNC_POLICIES, default="everysec")
    p.add_argument("--uvloop", action="store_true", help="use uvloop if it is installed")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.uvloop:
        import uvloop
        uvloop.install()

    aof = AOF(args.aof, args.appendfsync) if args.aof else None
    server = Server(args.host, args.port, aof)
    try:
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        if aof is not None:
            aof.close()


if __name__ == "__main__":
    main()
