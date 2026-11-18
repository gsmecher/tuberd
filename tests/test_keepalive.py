"""
Keep-alive idle limits.

tuberd closes a connection once it has been idle for 5 s (cpp-httplib's
default). A request sent on a pooled connection just as the server closes it
would be lost, so both clients stop reusing a connection once it has been idle
for 4 s, and connect afresh instead.

The reuse tests use a stub server that keeps connections open regardless, and
records the client port of each request, to tell whether a connection was
reused.
"""

import asyncio
import http.server
import json
import threading
import time

import pytest
import requests

import tuber
from tuber import server as tuber_server

# Timing: a delay (e.g. on a loaded CI runner) only lengthens a connection's
# idle time. That can't break the STALE checks, which expect a new connection;
# the REUSED checks, which expect the same one, have 3.9 s to spare.
REUSED = 0.1  # idle time well within the clients' 4 s limit
STALE = 4.5  # idle time past it


class Stub(http.server.ThreadingHTTPServer):
    """Answer tuber requests for a "Pinger" object with a single "ping" method,
    without ever closing an idle connection."""

    daemon_threads = True

    def __init__(self):
        self.ports = []  # client port of each request
        super().__init__(("127.0.0.1", 0), StubHandler)

    @property
    def host(self):
        return "127.0.0.1:%d" % self.server_address[1]


class StubHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.server.ports.append(self.client_address[1])
        calls = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        meta = {"__doc__": None, "methods": {"ping": {"__doc__": None, "__signature__": {"parameters": []}}}}
        body = json.dumps([{"result": "pong" if "method" in c else meta} for c in calls]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub():
    s = Stub()
    thread = threading.Thread(target=s.serve_forever, daemon=True)
    thread.start()
    yield s
    s.shutdown()
    s.server_close()


def test_simple_client_reuse(stub):
    with tuber.resolve_simple(stub.host, "Pinger") as obj:
        assert obj.ping() == "pong"

        time.sleep(REUSED)
        assert obj.ping() == "pong"
        assert stub.ports[-1] == stub.ports[-2], "connection within the idle limit was not reused"

        time.sleep(STALE)
        assert obj.ping() == "pong"
        assert stub.ports[-1] != stub.ports[-2], "connection idle past the idle limit was reused"


@pytest.mark.asyncio
async def test_async_client_reuse(stub):
    obj = await tuber.resolve(stub.host, "Pinger")
    assert await obj.ping() == "pong"

    await asyncio.sleep(REUSED)
    assert await obj.ping() == "pong"
    assert stub.ports[-1] == stub.ports[-2], "connection within the idle limit was not reused"

    await asyncio.sleep(STALE)
    assert await obj.ping() == "pong"
    assert stub.ports[-1] != stub.ports[-2], "connection idle past the idle limit was reused"


class Pinger:
    def ping(self):
        return "pong"


def test_tuberd_timeout():
    """tuberd's keep-alive timeout, which it advertises, must stay longer than
    the clients' 4 s idle limit (with a margin: see requests_session in
    tuber/client.py)."""
    server = tuber_server.Server({"Pinger": Pinger()}, port=0)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    try:
        r = requests.post(f"http://localhost:{server.port}/tuber", json=[{"object": "Pinger", "method": "ping"}])
        r.raise_for_status()
        assert r.headers["Keep-Alive"].startswith("timeout=5,")
    finally:
        server.stop()
        thread.join(timeout=30)
    assert not thread.is_alive(), "tuberd did not stop"
