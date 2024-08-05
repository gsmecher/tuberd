import pytest

pytest_plugins = ("pytest_asyncio",)


# Add custom orjson marker
def pytest_configure(config):
    config.addinivalue_line("markers", "orjson: marks tests that require server-side serialization of numpy arrays")


# Allow test invocation to specify arguments to tuberd backend (this way, we
# can re-use the same test machinery across different json libraries.)
def pytest_addoption(parser):
    # Create a pass-through path for tuberd options (e.g. for verbosity)
    parser.addoption("--tuberd-option", action="append", default=[])

    # The "--orjson-with-numpy" option is handled as a special case because it
    # changes test behaviour.
    parser.addoption("--orjson-with-numpy", action="store_true", default=False)


# Some tests require orjson - the following skips them unless we're in
# --orjson-with-numpy mode.
def pytest_collection_modifyitems(config, items):
    if config.getoption("orjson_with_numpy"):
        return

    for item in items:
        if "orjson" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="Test depends on orjson fastpath"))
