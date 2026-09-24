import os
import pytest
import requests
import socket
import subprocess
import sys
import time

from tuber import codecs

pytest_plugins = ("pytest_asyncio",)


# Add custom orjson markers
def pytest_configure(config):
    config.addinivalue_line("markers", "orjson: marks tests that require server-side serialization of numpy arrays")
    config.addinivalue_line("markers", "no_orjson: marks tests that are incompatible with the orjson codec")


# Allow test invocation to specify arguments to tuberd backend (this way, we
# can re-use the same test machinery across different json libraries.)
def pytest_addoption(parser):
    # Create a pass-through path for tuberd options (e.g. for verbosity)
    parser.addoption("--tuberd-option", action="append", default=[])

    # The "--orjson" option is handled as a special case because it
    # changes test behaviour.
    parser.addoption("--orjson", action="store_true", default=False)

    # The "--simplejson" option is handled as a special case because it
    # changes test behaviour.
    parser.addoption("--simplejson", action="store_true", default=False)

    # Allow tuberd port to be specified
    parser.addoption("--tuberd-port", default=8080)


# Some tests require orjson - the following skips them unless we're in
# --orjson mode.  Conversely, some tests are incompatible with orjson and are
# skipped when --orjson is active.
def pytest_collection_modifyitems(config, items):
    orjson = config.getoption("orjson")

    for item in items:
        if not orjson and "orjson" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="Test depends on orjson fastpath"))
        if orjson and "no_orjson" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="Test incompatible with orjson"))


@pytest.fixture(scope="module")
def tuberd_host(pytestconfig):
    return f"localhost:{pytestconfig.getoption('tuberd_port')}"


def run_tuberd(registry, port, *args):
    """Spawn a tuberd, and wait for it to start listening"""

    if os.getenv("CMAKE_TEST"):
        argv = [sys.executable, "-m", "tuber.server"]
    else:
        argv = ["tuberd"]

    argv += [f"-p{port}", f"--registry={registry}", "--validate", *args]

    s = subprocess.Popen(argv)

    # The server takes a moment to come up (it sources this test file as a
    # registry) - don't release tests against it until it's listening.
    for _ in range(100):
        if s.poll() is not None:
            raise RuntimeError(f"tuberd exited on startup with code {s.returncode}")
        try:
            with socket.create_connection(("localhost", int(port)), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        s.terminate()
        raise RuntimeError("tuberd did not start listening")

    return s


@pytest.fixture(scope="module", autouse=True)
def tuberd(request, pytestconfig):
    """Spawn (and kill) a tuberd"""

    args = list(pytestconfig.getoption("tuberd_option"))

    if pytestconfig.getoption("orjson"):
        # If we can't import orjson here, it's presumably missing from the
        # tuberd execution environment as well - in which case, we should skip
        # the test.
        pytest.importorskip("orjson")
        args += ["--json", "orjson"]

    if pytestconfig.getoption("simplejson"):
        # If we can't import simplejson here, it's presumably missing from the
        # tuberd execution environment as well - in which case, we should skip
        # the test.
        pytest.importorskip("simplejson")
        args += ["--json", "simplejson"]

    s = run_tuberd(request.node.fspath, pytestconfig.getoption("tuberd_port"), *args)

    yield s
    s.terminate()


@pytest.fixture
def spawn_tuberd(request):
    """
    Spawn additional tuberd instances with custom command line arguments.

    Each is given a port of its own, so as not to collide with the tuberd shared
    by the rest of the module, and is terminated when the test ends.  Returns the
    host on which the new server is listening.
    """

    servers = []

    def spawn(*args):
        with socket.socket() as s:
            s.bind(("localhost", 0))
            port = s.getsockname()[1]
        servers.append(run_tuberd(request.node.fspath, port, *args))
        return f"localhost:{port}"

    yield spawn

    for s in servers:
        s.terminate()
        s.wait()


# This fixture provides a much simpler, synchronous wrapper for functionality
# normally provided by tuber.py.  It's coded directly - which makes it less
# flexible, less performant, and easier to understand here.
@pytest.fixture(scope="module", params=["json", "cbor"])
def tuber_call(request, tuberd_host):
    URI = f"http://{tuberd_host}/tuber"

    accept = f"application/{request.param}"
    loads = lambda d: codecs.AcceptTypes[accept](d, encoding="utf-8", convert=False)

    # The tuber daemon can take a little while to start (in particular, it
    # sources this script as a registry) - rather than adding a magic sleep to
    # the subprocess command, we teach the client interface to wait patiently.
    adapter = requests.adapters.HTTPAdapter(
        max_retries=requests.packages.urllib3.util.retry.Retry(total=10, backoff_factor=1)
    )
    session = requests.Session()
    session.mount(URI, adapter)

    def tuber_call(json=None, **kwargs):
        # The most explicit call style passes POST content via an explicit
        # "json" parameter.  However, for convenience's sake, we also allow
        # kwargs to supply a dict parameter since we often call with dicts and
        # this results in a more readable code style.
        return loads(
            session.post(
                URI,
                json=kwargs if json is None else json,
                headers={"Accept": accept},
            ).content
        )

    yield tuber_call
