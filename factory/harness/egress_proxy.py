"""Egress proxy for agent sandboxes: HTTPS CONNECT only, to an allowlist of hosts.

The agent's container sits on an internal Docker network with no route out; this proxy is the
only way out, so the agent reaches its model's API and nothing else (no GitHub, no package
index: a public fix cannot be looked up). Standard library only: it runs as
``python egress_proxy.py <port> <pattern,pattern,...>`` in any Python image. Patterns are
fnmatch-style host names (``*.example.com``); ``*`` allows every host (for finding out which
hosts an agent needs). One log line per connection: ``ALLOW host`` or ``DENY host:port from <ip>``
(the client's address on the internal network tells parallel sandboxes apart).
"""
from __future__ import annotations

import asyncio
import fnmatch
import sys

TIMEOUT_S = 30


def allowed(host: str, port: str, patterns: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return port == "443" and any(fnmatch.fnmatchcase(host, p) for p in patterns)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def _handle(patterns: list[str], reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request = (await asyncio.wait_for(reader.readline(), TIMEOUT_S)).decode("latin-1").split()
        while (await asyncio.wait_for(reader.readline(), TIMEOUT_S)) not in (b"\r\n", b"\n", b""):
            pass  # request headers
        if len(request) < 2 or request[0].upper() != "CONNECT":
            print(f"DENY {' '.join(request[:2])} (only HTTPS CONNECT) from {(writer.get_extra_info('peername') or ('?',))[0]}", flush=True)
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            await writer.drain()
            return
        host, _, port = request[1].rpartition(":")
        host = host.strip("[]")
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        if not allowed(host, port, patterns):
            print(f"DENY {host}:{port} from {peer}", flush=True)
            writer.write(b"HTTP/1.1 403 Forbidden (task-factory egress allowlist)\r\n\r\n")
            await writer.drain()
            return
        up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), TIMEOUT_S)
        print(f"ALLOW {host}", flush=True)
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
    except (asyncio.TimeoutError, ConnectionError, OSError, ValueError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", flush=True)
    finally:
        writer.close()


async def _main(port: int, patterns: list[str]) -> None:
    server = await asyncio.start_server(lambda r, w: _handle(patterns, r, w), "0.0.0.0", port)
    print(f"egress proxy on :{port}, allowed: {', '.join(patterns) or '(nothing)'}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_main(int(sys.argv[1]), [p.strip().lower() for p in sys.argv[2].split(",") if p.strip()]))
