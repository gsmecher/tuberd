"""
Tuber object interface
"""

import aiohttp
import asyncio
import atexit
import contextlib
import socket
import textwrap
import types
import urllib
import warnings
import weakref

# Prefer SimpleJSON, but fall back on built-in
try:
    import simplejson as json
except ModuleNotFoundError:
    import json  # type: ignore[no-redef]


async def resolve(objname: str, hostname: str):
    """Create a local reference to a networked resource.

    This is the recommended way to connect to remote tuberd instances.
    """

    instance = TuberObject(objname, f"http://{hostname}/tuber")
    await instance.tuber_resolve()
    return instance


# Keep a mapping between event loops and client session objects, so we can
# reuse clientsessions in an event-loop safe way. This is a slightly cheeky
# way to avoid carrying around global state, and requiring that state be
# consistent with whatever event loop is running in whichever context it's
# used. See https://docs.aiohttp.org/en/stable/faq.html
_clientsession: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class TuberError(Exception):
    pass


class TuberStateError(TuberError):
    pass


class TuberRemoteError(TuberError):
    pass


class TuberResult:
    def __init__(self, d):
        "Allow dotted accessors, like an object"
        self.__dict__.update(d)

    def __iter__(self):
        "Make the results object iterate as a list of keys, like a dict"
        return iter(self.__dict__)

    def __repr__(self):
        "Return a concise representation string"
        return repr(self.__dict__)


_json_loads = json.JSONDecoder(object_hook=TuberResult).decode


def attribute_blacklisted(name):
    """
    Keep Python-specific attributes from being treated as potential remote
    resources. This blacklist covers SQLAlchemy, IPython, and Tuber internals.
    """

    if name.startswith(
        (
            "_sa",
            "_ipython",
            "_tuber",
        )
    ):
        return True

    return False


class Context(object):
    """A context container for TuberCalls. Permits calls to be aggregated.

    Commands are dispatched strictly in-order, but are automatically bundled
    up to reduce roundtrips.
    """

    def __init__(self, obj, **ctx_kwargs):
        self.calls = []
        self.obj = obj
        self.ctx_kwargs = ctx_kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Ensure the context is flushed."""
        if self.calls:
            await self()

    def _add_call(self, **request):
        future = asyncio.Future()
        self.calls.append((request, future))
        return future

    async def __call__(self):
        """Break off a set of calls and return them for execution."""

        calls = []
        futures = []
        while self.calls:
            (c, f) = self.calls.pop(0)

            calls.append(c)
            futures.append(f)

        # An empty Context returns an empty list of calls
        if not calls:
            return []

        loop = asyncio.get_running_loop()
        try:
            cs = _clientsession[loop]
        except KeyError as e:
            _clientsession[loop] = cs = aiohttp.ClientSession(json_serialize=json.dumps)

        # Create a HTTP request to complete the call. This is a coroutine,
        # so we queue the call and then suspend execution (via 'yield')
        # until it's complete.
        async with cs.post(self.obj._tuber_uri, json=calls) as resp:
            json_out = await resp.json(loads=_json_loads, content_type=None)

        if hasattr(json_out, "error"):
            # Oops - this is actually a server-side error that bubbles
            # through. (See test_tuberpy_async_context_with_unserializable.)
            # We made an array request, and received an object response
            # because of an exception-catching scope in the server. Do the
            # best we can.
            raise TuberRemoteError(json_out.error.message)

        # Resolve futures
        results = []
        for f, r in zip(futures, json_out):
            # Always emit warnings, if any occurred
            if hasattr(r, "warnings") and r.warnings:
                for w in r.warnings:
                    warnings.warn(w)

            # Resolve either a result or an error
            if hasattr(r, "error") and r.error:
                f.set_exception(TuberRemoteError(r.error.message))
            else:
                results.append(r.result)
                f.set_result(r.result)

        # Return a list of results
        return results

    def __getattr__(self, name):
        if attribute_blacklisted(name):
            raise AttributeError(f"{name} is not a valid method or property!")

        # Queue methods calls.
        def caller(*args, **kwargs):
            # Add extra arguments where they're provided
            kwargs = kwargs.copy()
            kwargs.update(self.ctx_kwargs)

            # ensure that a new unique future is returned
            # each time this function is called
            future = self._add_call(object=self.obj._tuber_objname, method=name, args=args, kwargs=kwargs)

            return future

        setattr(self, name, caller)
        return caller


class TuberObject:
    """A base class for TuberObjects.

    This is a great way of using Python to correspond with network resources
    over a HTTP tunnel. It hides most of the gory details and makes your
    networked resource look and behave like a local Python object.

    To use it, you should subclass this TuberObject.
    """

    def __init__(self, objname, uri):
        self._tuber_objname = objname
        self._tuber_uri = uri

    def tuber_context(self, **kwargs):
        return Context(self, **kwargs)

    @property
    def __doc__(self):
        """Construct DocStrings using metadata from the underlying resource."""

        return self._tuber_meta.__doc__

    def __dir__(self):
        """Provide a list of what's here. (Used for tab-completion.)"""

        attrs = dir(super(TuberObject, self))
        return sorted(attrs + self._tuber_meta.properties + self._tuber_meta.methods)

    async def tuber_resolve(self):
        """Retrieve metadata associated with the remote network resource.

        This data isn't strictly needed to construct "blind" JSON-RPC calls,
        except for user-friendliness:

           * tab-completion requires knowledge of what the board does, and
           * docstrings are useful, but must be retrieved and attached.

        This class retrieves object-wide metadata, which can be used to build
        up properties and values (with tab-completion and docstrings)
        on-the-fly as they're needed.
        """
        try:
            return (self._tuber_meta, self._tuber_meta_properties, self._tuber_meta_methods)
        except AttributeError:
            async with self.tuber_context() as ctx:
                ctx._add_call(object=self._tuber_objname)
                meta = await ctx()
                meta = meta[0]

                for p in meta.properties:
                    ctx._add_call(object=self._tuber_objname, property=p)
                prop_list = await ctx()

                for m in meta.methods:
                    ctx._add_call(object=self._tuber_objname, property=m)
                meth_list = await ctx()

                props = dict(zip(meta.properties, prop_list))
                methods = dict(zip(meta.methods, meth_list))

            self._tuber_meta = meta
            self._tuber_meta_properties = props
            self._tuber_meta_methods = methods
            return (meta, props, methods)

    def __getattr__(self, name):
        """Remote function call magic.

        This function is called to get attributes (e.g. class variables and
        functions) that don't exist on "self". Since we build up a cache of
        descriptors for things we've seen before, we don't need to avoid
        round-trips to the board for metadata in the following code.
        """

        # Refuse to __getattr__ a couple of special names used elsewhere.
        if attribute_blacklisted(name):
            raise AttributeError(f"'{name}' is not a valid method or property!")

        # Make sure this request corresponds to something in the underlying
        # TuberObject.
        try:
            meta, metap, metam = (self._tuber_meta, self._tuber_meta_properties, self._tuber_meta_methods)
        except KeyError as e:
            raise TuberStateError(
                e,
                "No metadata! Did you forget to call tuber_resolve()?",
            )

        if name not in meta.methods and name not in meta.properties:
            raise AttributeError(f"'{name}' is not a valid method or property!")

        if name in meta.properties:
            # Fall back on properties.
            setattr(self, name, metap[name])
            return getattr(self, name)

        if name in meta.methods:
            # Generate a callable prototype
            async def invoke(self, *args, **kwargs):
                async with self.tuber_context() as ctx:
                    result = getattr(ctx, name)(*args, **kwargs)
                return await result

            # Attach DocStrings, if provided and valid
            try:
                invoke.__doc__ = textwrap.dedent(metam[name].__doc__)
            except:
                pass

            # Associate as a class method.
            setattr(self, name, types.MethodType(invoke, self))
            return getattr(self, name)


# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
