"""
Tuber object interface
"""

from __future__ import annotations
import aiohttp
import asyncio
from collections.abc import Mapping
import textwrap
import types
import warnings

from .codecs import wrap_bytes_for_json, cbor_augment_encode, cbor_tag_decode


__all__ = [
    "TuberError",
    "TuberStateError",
    "TuberRemoteError",
    "TuberResult",
    "TuberObject",
    "resolve",
    "resolve_all",
]


async def resolve(objname: str, hostname: str, accept_types: list[str] | None = None):
    """Create a local reference to a networked resource.

    This is the recommended way to connect to remote tuberd instances.
    """

    instance = TuberObject(objname, f"http://{hostname}/tuber", accept_types=accept_types)
    await instance.tuber_resolve()
    return instance


async def resolve_all(hostname: str, accept_types: list[str] | None = None, create=True):
    """Discover all objects on a networked resource.

    This is the recommended way to connect to remote tuberd instances.

    Arguments
    ---------
    hostname : str
        Hostname on which the tuberd instance is running.
    accept_types: list of str
        List of response data types accepted by the client.
    create : bool
        If True, return a dictionary of resolved TuberObjects keyed by object name.
        Otherwise, return a list of object names.

    Returns
    -------
    objs : list of str or dict of TuberObjects
        List of object names, or dict of TuberObjects, depending on the value of
        the ``create`` option.
    """
    async with Context(uri=f"http://{hostname}/tuber", accept_types=accept_types) as ctx:
        ctx._add_call()
        meta = await ctx()
        objnames = meta[0].objects

    if not create:
        return objnames

    return {obj: await resolve(obj, hostname, accept_types) for obj in objnames}


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


# This variable is used to track the media types we are able to decode, mapping their names to
# decoding functions. The interface of the decoding function is to take two arguments: a bytes-like
# object containing the encoded data, and an encoding name (which may be None) given by the
# character set information (if any) included in the Content-Type header attached to the data.
AcceptTypes = {}

# Prefer SimpleJSON, but fall back on built-in
try:
    import simplejson as json
except ModuleNotFoundError:
    import json  # type: ignore[no-redef]
def decode_json(response_data, encoding):
    if encoding is None:  # guess the typical default if unspecified
        encoding = "utf-8"
    def ohook(obj):
        if isinstance(obj, Mapping) and "bytes" in obj \
          and (len(obj) == 1 or (len(obj) == 2 and "subtype" in obj)):
            try:
                return bytes(obj["bytes"])
            except e as ValueError:
                pass
        return TuberResult(obj)
    return json.JSONDecoder(object_hook=ohook).decode(response_data.decode(encoding))
AcceptTypes["application/json"] = decode_json

# Use cbor2 to handle CBOR, if available
try:
    import cbor2 as cbor
    def decode_cbor(response_data, encoding):
        return cbor.loads(response_data,
                          object_hook=lambda dec,data: TuberResult(data),
                          tag_hook=cbor_tag_decode)
    AcceptTypes["application/cbor"] = decode_cbor
except:
    pass


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

    def __init__(
        self,
        obj: "TuberObject" | None = None,
        uri: str | None = None,
        accept_types: list[str] | None = None,
        **ctx_kwargs
    ):
        self.calls: list[tuple[dict, asyncio.Future]] = []
        self.obj = obj
        if obj is None:
            if uri is None:
                raise ValueError("Argument 'uri' required if 'obj' not provided")
            self.uri = uri
        else:
            self.uri = self.obj._tuber_uri
        if accept_types is None:
            self.accept_types = list(AcceptTypes.keys())
        else:
            for accept_type in accept_types:
                if accept_type not in AcceptTypes.keys():
                    raise ValueError(f"Unsupported accept type: {accept_type}")
            self.accept_types = accept_types
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
        if not hasattr(loop, "_tuber_session"):
            # Monkey-patch tuber session memory handling with the running event loop
            loop._tuber_session = aiohttp.ClientSession(json_serialize=json.dumps)

            # Ensure that ClientSession.close() is called when the loop is
            # closed.  ClientSession.__del__ does not close the session, so it
            # is not sufficient to simply attach the session to the loop to
            # ensure garbage collection.
            loop_close = loop.close
            def close(self):
                if hasattr(self, "_tuber_session"):
                    if not self.is_closed():
                        self.run_until_complete(self._tuber_session.close())
                    del self._tuber_session
                loop_close()
            loop.close = types.MethodType(close, loop)

        cs = loop._tuber_session

        # Declare the media types we want to allow getting back
        headers={"Accept":", ".join(self.accept_types)}
        # Create a HTTP request to complete the call. This is a coroutine,
        # so we queue the call and then suspend execution (via 'yield')
        # until it's complete.
        async with cs.post(self.uri, json=calls, headers=headers) as resp:
            raw_out = await resp.read()
            if not resp.ok:
                try:
                    text = raw_out.decode(resp.charset or "utf-8")
                except Exception as ex:
                    raise TuberRemoteError(f"Request failed with status {resp.status}")
                raise TuberRemoteError(f"Request failed with status {resp.status}: {text}")
            content_type = resp.content_type
            # Check that the resulting media type is one which can actually be handled;
            # this is slightly more liberal than checking that it is really among those we declared
            if content_type not in AcceptTypes:
                raise TuberError("Unexpected response content type: "+content_type)
            json_out = AcceptTypes[content_type](raw_out, resp.charset)

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
                if hasattr(r.error, "message"):
                    f.set_exception(TuberRemoteError(r.error.message))
                else:
                    f.set_exception(TuberRemoteError("Unknown error"))
            else:
                if hasattr(r, "result"):
                    results.append(r.result)
                    f.set_result(r.result)
                else:
                    f.set_exception(TuberError("Result has no 'result' attribute"))

        # Return a list of results
        return [ await f for f in futures ]

    def __getattr__(self, name):
        if attribute_blacklisted(name) or self.obj is None:
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

    def __init__(self, objname: str, uri: str, accept_types: list[str] | None = None):
        self._tuber_objname = objname
        self._tuber_uri = uri
        self._accept_types = accept_types

    def tuber_context(self, **kwargs):
        return Context(self, accept_types=self._accept_types, **kwargs)

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
