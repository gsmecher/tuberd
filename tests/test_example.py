"""
Run the scripts under example/ as a user would.

The examples double as documentation, so this keeps them from drifting away
from the API. The client scripts are run as separate processes against a
server serving the example registry; the embedded example and the example
server's own command line are run as-is.
"""

import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "example"


@pytest.fixture(scope="module")
def registry_file():
    """The tuberd fixture (conftest.py) serves the example registry."""
    return EXAMPLE_DIR / "example_server.py"


def run_script(script, *args, timeout=120):
    """Run an example script to completion and return its stdout."""
    result = subprocess.run(
        [sys.executable, str(EXAMPLE_DIR / script), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert (
        result.returncode == 0
    ), f"{script} exited with {result.returncode}\n--- stdout\n{result.stdout}\n--- stderr\n{result.stderr}"
    return result.stdout


@pytest.mark.parametrize("script", ["example_client.py", "example_async_client.py"])
def test_example_client(tuberd, script):
    out = run_script(script, "-p", str(tuberd.port))

    # Exit code 0 is the real check. These lines confirm the interesting paths
    # were reached: a re-raised remote exception, a call across an array of
    # objects, and a numpy array received over CBOR.
    assert "Remote error: ValueError" in out
    assert "Calibrations: [0.0, 0.1, 0.2, 0.3]" in out
    assert "Samples: ndarray float64 (8,)" in out


def test_example_embedded():
    out = run_script("example_embedded.py")

    assert "Knob: 42" in out
    assert out.rstrip().endswith("Server stopped")


def test_example_server_script():
    """Run example_server.py as its docstring says to, and drive it with the client."""
    server = subprocess.Popen(
        [sys.executable, str(EXAMPLE_DIR / "example_server.py"), "-p", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # The server announces its port once it is bound and listening, so the
        # client can be started as soon as this line arrives.
        line = server.stdout.readline()
        match = re.fullmatch(r"Serving on port (\d+)\n", line)
        assert match, f"unexpected first line from example_server.py: {line!r}"

        out = run_script("example_client.py", "-p", match[1])
        assert "Samples: ndarray float64 (8,)" in out
    finally:
        # Ctrl-C is how the docs say to stop it; it should exit cleanly.
        server.send_signal(signal.SIGINT)
        try:
            _, stderr = server.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate()
            raise

    assert server.returncode == 0, stderr
