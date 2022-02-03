#!/usr/bin/python3

"""
Double-entry style asynchronous coding.

The following example coroutine sleeps asynchronously, then returns double its
argument:

    >>> @tworoutine
    ... async def double_slowly(x):
    ...     await asyncio.sleep(1)
    ...     return 2*x

Unlike traditional coroutines, we can call tworoutines synchronously:

    >>> double_slowly(2)
    4

(This is cheating, and intended only as a shortcut! You *cannot* use the
synchronous invocation from within an asynchronous event loop, since event
loops cannot nest.)

We can also, using a little sugar, call it asynchronously:

    >>> c = (~double_slowly)(2)
    >>> print(c) #doctest: +ELLIPSIS
    <coroutine object double_slowly at 0x...>
    >>> asyncio.run(c)
    4

...which devolves to the usual asyncio case.

Tworoutines can also be used as class methods:

    >>> class Foo:
    ...     @tworoutine
    ...     async def multiply_slowly(self, a, b):
    ...         await asyncio.sleep(1)
    ...         return a * b
    >>> f = Foo()
    >>> f.multiply_slowly(3, 4)
    12
    >>> c = (~f.multiply_slowly)(4, 4)
    >>> asyncio.run(c)
    16

We can also implement these things via inheritance:

    >>> class TripleSlowly(tworoutine):
    ...     async def __acall__(self, x):
    ...         await asyncio.sleep(1)
    ...         return 3*x

This works the same way:

    >>> ts = TripleSlowly()
    >>> ts(2)
    6
    >>> c = (~ts)(3)
    >>> print(c) #doctest: +ELLIPSIS
    <coroutine object TripleSlowly.__acall__ at 0x...>
    >>> asyncio.run(c)
    9
"""

import asyncio
import functools

__all__ = ["tworoutine"]


class tworoutine:
    """
    Base class for double-entry style asynchronous coding.

    For client code, this class provides two points of entry:

    - tworoutine(args) Synchronous, via tworoutine(args), and
    - Asynchronous, via ~(tworoutine)(args).

    The synchronous entry is
    """

    __instance = None

    def __init__(self, coroutine=None, instance=None):

        self.__instance = instance

        if coroutine is not None:
            # This path is intended for use as a @tworoutine decorator.
            self.__acall__ = coroutine

        functools.update_wrapper(self, self.__acall__)

    def __get__(self, instance, owner):
        """Descriptor allowing us to behave as bound methods."""

        return self.__class__(instance=instance, coroutine=self.__acall__)

    def __call__(self, *args, **kwargs):
        """Stub for ordinary, serial call."""

        # By default, presume a fancy asynchronous version has been coded and
        # invoke it synchronously. This is often all the serial version needs
        # to do anyways.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()

        coroutine = (~self)(*args, **kwargs)
        return loop.run_until_complete(coroutine)

    def __invert__(self):
        if self.__instance:
            return functools.partial(self.__acall__, self.__instance)
        else:
            return self.__acall__

    async def __acall__(self, *args, **kwargs):
        """Stub for asynchronous call that returns a Future."""
        raise NotImplementedError()


# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
