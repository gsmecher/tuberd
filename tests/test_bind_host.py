"""
Binding a server to a specific local address.

By default a server listens on every IPv4 interface. Passing ``host`` binds a
single address instead, which lets several servers share one port on different
addresses of the same machine (e.g. a simulation that gives each simulated
device its own loopback address).
"""

import socket
import threading

import pytest
import requests

from tuber import server as tuber_server


class Greeter:
    def hello(self):
        return "hello"


def can_bind(host):
    """True if this machine has the given local address (e.g. a loopback alias)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, 0))
        except OSError:
            return False
    return True


class Running:
    """Serve a registry on a background thread for the duration of a with-block."""

    def __init__(self, **kwargs):
        self.server = tuber_server.Server({"greeter": Greeter()}, **kwargs)
        self.thread = threading.Thread(target=self.server.serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.server

    def __exit__(self, *exc):
        self.server.stop()
        self.thread.join(timeout=30)
        assert not self.thread.is_alive(), "tuberd did not stop"


def call_hello(host, port):
    r = requests.post(
        f"http://{host}:{port}/tuber",
        json=[{"object": "greeter", "method": "hello"}],
        timeout=5,
    )
    r.raise_for_status()
    return r.json()[0]["result"]


def test_bind_host_serves_requests():
    with Running(host="127.0.0.1", port=0) as server:
        assert server.port > 0
        assert call_hello("127.0.0.1", server.port) == "hello"


def test_bind_host_default_is_all_interfaces():
    with Running(port=0) as server:
        assert call_hello("127.0.0.1", server.port) == "hello"


def test_bind_host_positional():
    # host comes before port, as in an address
    server = tuber_server.Server({"greeter": Greeter()}, "json", "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    try:
        assert call_hello("127.0.0.1", server.port) == "hello"
    finally:
        server.stop()
        thread.join(timeout=30)


def test_bind_host_invalid_address():
    with pytest.raises(RuntimeError, match="could not bind"):
        tuber_server.Server({}, host="203.0.113.1", port=0)


def test_bind_host_command_line():
    args = tuber_server.parse_args(["--host=127.0.0.1", "--port=0"], registry={"greeter": Greeter()})
    assert args.host == "127.0.0.1"
    with Running(**{k: v for k, v in vars(args).items() if k != "registry"}) as server:
        assert call_hello("127.0.0.1", server.port) == "hello"


@pytest.mark.skipif(not can_bind("127.0.0.2"), reason="127.0.0.2 is not a local address here")
def test_bind_host_shared_port():
    # Find a port that is free on both addresses, then put a server on each.
    with Running(host="127.0.0.1", port=0) as first:
        port = first.port
        with Running(host="127.0.0.2", port=port) as second:
            assert second.port == port
            assert call_hello("127.0.0.1", port) == "hello"
            assert call_hello("127.0.0.2", port) == "hello"
