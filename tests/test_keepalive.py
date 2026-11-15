"""
Keep-alive timeouts.

The server closes a connection once it has been idle for its keep-alive
timeout. A request sent on a pooled connection just as the server closes it
would be lost, so clients stop reusing a connection once it has been idle for
their own, shorter, keep-alive timeout, and connect afresh instead. The two
timeouts are configured separately.

These tests use a 1 s server timeout and a 0.5 s client timeout: a connection
idle for 0.7 s must be replaced, although the server would still have
accepted a request on it.
"""

import asyncio
import socket
import threading
import time

import pytest
import requests

import tuber
from tuber import server as tuber_server
from tuber.client import DEFAULT_KEEP_ALIVE_TIMEOUT

SERVER_TIMEOUT = 1  # seconds
CLIENT_TIMEOUT = 0.5
REUSED = 0.1  # idle time well within the client's timeout
STALE = 0.7  # idle time past the client's timeout, before the server's


class Pinger:
    def ping(self):
        return "pong"


class Running:
    """Serve a registry on a background thread for the duration of a with-block."""

    def __init__(self, **kwargs):
        self.server = tuber_server.Server({"Pinger": Pinger()}, port=0, **kwargs)
        self.thread = threading.Thread(target=self.server.serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.server

    def __exit__(self, *exc):
        self.server.stop()
        self.thread.join(timeout=30)
        assert not self.thread.is_alive(), "tuberd did not stop"


@pytest.fixture(scope="module")
def server():
    with Running(keep_alive_timeout=SERVER_TIMEOUT) as s:
        yield s


def ping_request(port):
    body = b'[{"object": "Pinger", "method": "ping"}]'
    return (
        f"POST /tuber HTTP/1.1\r\nHost: localhost:{port}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode() + body


def post_ping(port):
    r = requests.post(f"http://localhost:{port}/tuber", json=[{"object": "Pinger", "method": "ping"}])
    r.raise_for_status()
    return r


def test_server_timeout_applied(server):
    # cpp-httplib reports its keep-alive timeout in the Keep-Alive header
    assert post_ping(server.port).headers["Keep-Alive"].startswith(f"timeout={SERVER_TIMEOUT},")


def test_server_default_timeout():
    with Running() as s:
        assert post_ping(s.port).headers["Keep-Alive"].startswith("timeout=5,")


def test_server_rejects_negative_timeout():
    with pytest.raises(ValueError):
        tuber_server.Server({}, port=0, keep_alive_timeout=-1)


def test_parse_args_timeout():
    args = tuber_server.parse_args(["--keep-alive-timeout=30"], registry={})
    assert args.keep_alive_timeout == 30


def test_default_timeouts_are_paired():
    """The clients' default must be shorter than the server's."""
    server_default = tuber_server.parse_args([], registry={}).keep_alive_timeout
    assert DEFAULT_KEEP_ALIVE_TIMEOUT < server_default


def test_server_closes_idle_connection(server):
    """The server keeps an idle connection open until its timeout, then closes it."""
    with socket.create_connection(("localhost", server.port)) as sock:
        sock.sendall(ping_request(server.port))
        sock.settimeout(5)
        data = b""
        while b"pong" not in data:
            chunk = sock.recv(4096)
            assert chunk, "server closed the connection before responding"
            data += chunk
        t0 = time.monotonic()
        while sock.recv(4096):  # the rest of the response, then the server's close
            pass
        idle = time.monotonic() - t0
    assert SERVER_TIMEOUT - 0.1 < idle < SERVER_TIMEOUT + 1


def test_client_rejects_negative_timeout():
    with pytest.raises(ValueError):
        tuber.SimpleTuberObject(None, hostname="localhost:1", keep_alive_timeout=-1)


def test_client_timeout_inherited(server):
    registry = tuber.resolve_simple(f"localhost:{server.port}", keep_alive_timeout=CLIENT_TIMEOUT)
    assert registry._keep_alive_timeout == CLIENT_TIMEOUT
    assert registry.Pinger._keep_alive_timeout == CLIENT_TIMEOUT
    assert tuber.resolve_simple(f"localhost:{server.port}")._keep_alive_timeout == DEFAULT_KEEP_ALIVE_TIMEOUT


def pooled_port(obj):
    """Local port of the simple client's pooled connection to the server."""
    session = obj._tuber_requests_session
    pools = session.get_adapter(f"http://{obj._tuber_host}/tuber").poolmanager.pools
    ports = [
        c.sock.getsockname()[1]
        for key in pools.keys()
        for c in list(pools[key].pool.queue)
        if c is not None and c.sock is not None
    ]
    assert len(ports) == 1
    return ports[0]


def test_simple_client_reuse(server):
    obj = tuber.resolve_simple(f"localhost:{server.port}", "Pinger", keep_alive_timeout=CLIENT_TIMEOUT)
    assert obj.ping() == "pong"
    port = pooled_port(obj)

    time.sleep(REUSED)
    assert obj.ping() == "pong"
    assert pooled_port(obj) == port, "connection within the client's timeout was not reused"

    time.sleep(STALE)
    assert obj.ping() == "pong"
    assert pooled_port(obj) != port, "connection idle past the client's timeout was reused"


def async_pooled_ports(keep_alive_timeout):
    """Local ports of the async client's pooled connections (this event loop's
    session for the given keep-alive timeout)."""
    session = asyncio.get_running_loop()._tuber_aiohttp_sessions[keep_alive_timeout]
    return [
        proto.transport.get_extra_info("sockname")[1]
        for conns in session.connector._conns.values()
        for proto, _ in conns
        if proto.transport is not None
    ]


@pytest.mark.asyncio
async def test_async_client_reuse(server):
    obj = await tuber.resolve(f"localhost:{server.port}", "Pinger", keep_alive_timeout=CLIENT_TIMEOUT)
    assert await obj.ping() == "pong"
    [port] = async_pooled_ports(CLIENT_TIMEOUT)

    await asyncio.sleep(REUSED)
    assert await obj.ping() == "pong"
    assert async_pooled_ports(CLIENT_TIMEOUT) == [port], "connection within the client's timeout was not reused"

    await asyncio.sleep(STALE)
    assert await obj.ping() == "pong"
    [new_port] = async_pooled_ports(CLIENT_TIMEOUT)
    assert new_port != port, "connection idle past the client's timeout was reused"


@pytest.mark.asyncio
async def test_async_sessions_per_timeout(server):
    """Objects on a loop share a session (connection pool) only if their
    keep-alive timeouts match."""
    host = f"localhost:{server.port}"
    a = await tuber.resolve(host, "Pinger", keep_alive_timeout=CLIENT_TIMEOUT)
    b = await tuber.resolve(host, "Pinger", keep_alive_timeout=CLIENT_TIMEOUT)
    c = await tuber.resolve(host, "Pinger")
    for obj in (a, b, c):
        assert await obj.ping() == "pong"
    assert set(asyncio.get_running_loop()._tuber_aiohttp_sessions) == {CLIENT_TIMEOUT, DEFAULT_KEEP_ALIVE_TIMEOUT}
