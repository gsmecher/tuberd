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

# The tuber module is imported in both server and client environments. Because
# the server execution environment may be minimal, we may bump into
# ModuleNotFoundErrors in client code - which we may want to ignore.
try:
    from .client import (
        TuberObject,
        SimpleTuberObject,
        resolve,
        resolve_simple,
    )

    __all__ += ["TuberObject", "SimpleTuberObject", "resolve", "resolve_simple"]
except ImportError as ie:
    import os

    if "TUBER_SERVER" not in os.environ:
        raise ie


def get_include():
    """
    Return the path to the tuber include directory.
    """
    from pathlib import Path

    return str(Path(__file__).parent / "include")


# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
