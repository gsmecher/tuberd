import os
import pytest
import requests
import subprocess
import sys
import warnings

from tuber import codecs

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

    # Allow tuberd port to be specified
    parser.addoption("--tuberd-port", default=8080)


# Some tests require orjson - the following skips them unless we're in
# --orjson mode.
def pytest_collection_modifyitems(config, items):
    if config.getoption("orjson"):
        return

    for item in items:
        if "orjson" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="Test depends on orjson fastpath"))


@pytest.fixture(scope="module")
def tuberd_host(pytestconfig):
    return f"localhost:{pytestconfig.getoption('tuberd_port')}"


@pytest.fixture(scope="module", autouse=True)
def tuberd(request, pytestconfig):
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
        f"--validate",
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
def tuber_call(request, tuberd_host):
    URI = f"http://{tuberd_host}/tuber"

    accept = f"application/{request.param}"
    loads = codecs.Codecs[request.param].decode

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
