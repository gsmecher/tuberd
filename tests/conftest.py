import pytest
import requests
import threading
import warnings

from tuber import codecs
from tuber import server as tuber_server

pytest_plugins = ("pytest_asyncio",)


# Add custom orjson marker
def pytest_configure(config):
    config.addinivalue_line("markers", "orjson: marks tests that require server-side serialization of numpy arrays")


# The same test machinery is run against different JSON libraries; "--orjson"
# switches the server to orjson and enables the tests that depend on it.
def pytest_addoption(parser):
    parser.addoption("--orjson", action="store_true", default=False)


# Some tests require orjson - the following skips them unless we're in
# --orjson mode.
def pytest_collection_modifyitems(config, items):
    if config.getoption("orjson"):
        return

    for item in items:
        if "orjson" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="Test depends on orjson fastpath"))


@pytest.fixture(scope="module")
def tuberd_host(tuberd):
    return f"localhost:{tuberd.port}"


@pytest.fixture(scope="module")
def tuberd(registry_file, pytestconfig):
    """
    Run a tuberd on a background thread for the duration of the module.

    Each test module says what to serve by defining a module-scoped
    ``registry_file`` fixture returning the path of a registry Python file
    (tests/test.py returns its own path; the example tests return
    example/example_server.py).
    """

    # Port 0 lets the kernel pick a free port, so concurrent test runs never
    # collide; the server reports the one it got.
    argv = [
        "--port=0",
        f"--registry={registry_file}",
        "--validate",
    ]

    if pytestconfig.getoption("orjson"):
        pytest.importorskip("orjson")
        argv.extend(["--json", "orjson"])

    server = tuber_server.Server(**vars(tuber_server.parse_args(argv)))

    # An exception on the server thread would otherwise only reach the
    # console; keep it so teardown can report it.
    failure = []

    def serve():
        try:
            server.serve()
        except BaseException as e:
            failure.append(e)

    thread = threading.Thread(target=serve, name="tuberd", daemon=True)
    thread.start()

    yield server

    server.stop()
    thread.join(timeout=30)
    if thread.is_alive():
        raise RuntimeError("tuberd did not stop")
    if failure:
        raise RuntimeError("tuberd failed") from failure[0]


# This fixture provides a much simpler, synchronous wrapper for functionality
# normally provided by tuber.py.  It's coded directly - which makes it less
# flexible, less performant, and easier to understand here.
@pytest.fixture(scope="module", params=["json", "cbor"])
def tuber_call(request, tuberd_host):
    URI = f"http://{tuberd_host}/tuber"

    accept = f"application/{request.param}"
    loads = lambda d: codecs.AcceptTypes[accept](d, encoding="utf-8", convert=False)

    session = requests.Session()

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
