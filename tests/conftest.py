import cbor2
import http.server
import json
import jsonschema
import os
import pytest
import requests
import socketserver
import subprocess
import sys
import threading
import urllib

from tuber import schema, codecs

pytest_plugins = ("pytest_asyncio",)


# Add custom orjson marker
def pytest_configure(config):
    config.addinivalue_line("markers", "orjson: marks tests that require server-side serialization of numpy arrays")


# Allow test invocation to specify arguments to tuberd backend (this way, we
# can re-use the same test machinery across different json libraries.)
def pytest_addoption(parser):
    # Create a pass-through path for tuberd options (e.g. for verbosity)
    parser.addoption("--tuberd-option", action="append", default=[])

    # The "--orjson" option is handled as a special case because it
    # changes test behaviour.
    parser.addoption("--orjson", action="store_true", default=False)

    # Allow tuberd and proxy ports to be specified
    parser.addoption("--tuberd-port", default=8080)
    parser.addoption("--proxy-port", default=8081)


# Some tests require orjson - the following skips them unless we're in
# --orjson mode.
def pytest_collection_modifyitems(config, items):
    if config.getoption("orjson"):
        return

    for item in items:
        if "orjson" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="Test depends on orjson fastpath"))


@pytest.fixture(scope="module")
def proxy_uri(pytestconfig):
    return f"http://localhost:{pytestconfig.getoption('proxy_port')}/tuber"


@pytest.fixture(scope="module")
def tuberd_uri(pytestconfig):
    return f"http://localhost:{pytestconfig.getoption('tuberd_port')}/tuber"


@pytest.fixture(scope="module")
def tuberd(tuberd_noproxy, proxy_uri, tuberd_uri):
    # Create a proxy server that does nothing but validate schemas

    def proxy_server():
        adapter = requests.adapters.HTTPAdapter(
            max_retries=requests.packages.urllib3.util.retry.Retry(total=10, backoff_factor=1)
        )
        session = requests.Session()
        session.mount(tuberd_uri, adapter)

        class ProxyHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a, **kw):
                # be quiet
                pass

            def do_POST(self):
                content_length = int(self.headers["Content-Length"])
                request_data = self.rfile.read(content_length)
                req = urllib.request.Request(
                    tuberd_uri,
                    data=request_data,
                    headers=self.headers,
                    method="POST",
                )

                # validate request to server
                request_obj = json.loads(request_data)
                try:
                    jsonschema.validate(request_obj, schema.request)
                except jsonschema.ValidationError as e:
                    print(e)
                    self.send_response(500)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(f"Request validation failed: {e}\n\n{traceback.format_exc()}".encode("utf-8"))
                    return

                # dispatch request to server
                response = session.post(tuberd_uri, request_data, headers=self.headers)

                self.send_response(response.status_code)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                response_data = response.content

                # validate response from server
                response_type = response.headers["Content-Type"]
                if response_type == "application/json":
                    response_obj = json.loads(response_data)
                elif response_type == "application/cbor":
                    response_obj = cbor2.loads(response_data)
                else:
                    raise RuntimeError(f"Unexpected content-type: {response_type}")

                try:
                    jsonschema.validate(response_obj, schema.response)
                except jsonschema.ValidationError as e:
                    print(e)
                    self.send_response(500)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(f"Response validation failed: {e}\n\n{traceback.format_exc()}".encode("utf-8"))
                    return

                self.wfile.write(response_data)

        class ProxyServer(socketserver.TCPServer):
            allow_reuse_address = True

        # Start the proxy server
        proxy_uri_parsed = urllib.parse.urlparse(proxy_uri)
        with ProxyServer((proxy_uri_parsed.hostname, proxy_uri_parsed.port), ProxyHandler) as httpd:
            httpd.serve_forever()

    p = threading.Thread(target=proxy_server, daemon=True)
    p.start()

    yield tuberd_noproxy


@pytest.fixture(scope="module")
def tuberd_noproxy(request, pytestconfig):
    """Spawn (and kill) a tuberd"""

    TUBERD_PORT = pytestconfig.getoption("tuberd_port")

    if os.getenv("CMAKE_TEST"):
        tuberd = [sys.executable, "-m", "tuber.server"]
    else:
        tuberd = ["tuberd"]

    registry = request.node.fspath

    argv = tuberd + [
        f"-p{TUBERD_PORT}",
        f"--registry={registry}",
    ]

    argv.extend(pytestconfig.getoption("tuberd_option"))

    if pytestconfig.getoption("orjson"):
        # If we can't import orjson here, it's presumably missing from the
        # tuberd execution environment as well - in which case, we should skip
        # the test.
        pytest.importorskip("orjson")
        argv.extend(["--json", "orjson"])

    s = subprocess.Popen(argv)
    yield s
    s.terminate()


# This fixture provides a much simpler, synchronous wrapper for functionality
# normally provided by tuber.py.  It's coded directly - which makes it less
# flexible, less performant, and easier to understand here.
@pytest.fixture(scope="module", params=["json", "cbor"])
def tuber_call(request, tuberd, pytestconfig):

    PROXY_PORT = int(pytestconfig.getoption("tuberd_port"))
    URI_PROXY = f"http://localhost:{PROXY_PORT}/tuber"

    # Although the tuberd argument is not used here, it creates a dependency on
    # the daemon so it's launched and terminated.

    if request.param == "json":
        accept = "application/json"
        loads = json.loads
    elif request.param == "cbor":
        accept = "application/cbor"
        loads = lambda data: cbor2.loads(data, tag_hook=codecs.cbor_tag_decode)

    # The tuber daemon can take a little while to start (in particular, it
    # sources this script as a registry) - rather than adding a magic sleep to
    # the subprocess command, we teach the client interface to wait patiently.
    adapter = requests.adapters.HTTPAdapter(
        max_retries=requests.packages.urllib3.util.retry.Retry(total=10, backoff_factor=1)
    )
    session = requests.Session()
    session.mount(URI_PROXY, adapter)

    def tuber_call(json=None, **kwargs):
        # The most explicit call style passes POST content via an explicit
        # "json" parameter.  However, for convenience's sake, we also allow
        # kwargs to supply a dict parameter since we often call with dicts and
        # this results in a more readable code style.
        return loads(
            session.post(
                URI_PROXY,
                json=kwargs if json is None else json,
                headers={"Accept": accept},
            ).content
        )

    yield tuber_call
