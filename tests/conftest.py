import pytest

# Allow test invocation to specify arguments to tuberd backend (this way, we
# can re-use the same test machinery across different json libraries.)
def pytest_addoption(parser):

    # Create a pass-through path for tuberd options (e.g. for verbosity)
    parser.addoption("--tuberd-option", action="append", default=[])

    # The "--orjson-with-numpy" option is handled as a special case because it
    # changes test behaviour. 
    parser.addoption("--orjson-with-numpy", action="store_true", default=False)

def pytest_collection_modifyitems(config, items):
    if config.getoption("orjson_with_numpy"):
        return

    # Ensure we have orjson, at all - and fail here if we don't.
    import orjson

    skip_numpy = pytest.mark.skip(reason="NumPy is not serializable except with orjson fastpath")
    for item in items:
        if "numpy" in item.keywords:
            item.add_marker(skip_numpy)
