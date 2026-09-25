class TuberError(Exception):
    pass


class TuberStateError(TuberError):
    pass


class TuberRemoteError(TuberError):
    pass


__all__ = [
    "TuberError",
    "TuberRemoteError",
    "TuberStateError",
]

# version.py is auto-generated from setuptools_scm
try:
    from ._version import __version__, __version_tuple__
except ImportError:
    __version__ = "???"

# The tuber module is imported in both server and client environments, and a
# server environment may be minimal. The client module keeps its third-party
# imports (requests, aiohttp, ...) inside the functions that need them, so
# importing it here never requires them: a missing dependency surfaces when a
# client function is first called, not at import time.
from .client import (
    TuberObject,
    SimpleTuberObject,
    resolve,
    resolve_simple,
)

__all__ += ["TuberObject", "SimpleTuberObject", "resolve", "resolve_simple"]


def get_include():
    """
    Return the path to the tuber include directory.
    """
    from pathlib import Path

    return str(Path(__file__).parent / "include")


# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
