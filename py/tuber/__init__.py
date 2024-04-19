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

# The tuber module is imported in both server and client environments. Because
# the server execution environment may be minimal, we may bump into
# ModuleNotFoundErrors in client code - which we may want to ignore.
try:
    from .client import (
        TuberObject,
        resolve,
        resolve_all,
    )

    __all__ += ["TuberObject", "resolve", "resolve_all"]
except ImportError as ie:
    import os

    if "TUBER_SERVER" not in os.environ:
        raise ie


# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
