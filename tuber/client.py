"""
Tuber object interface
"""

from __future__ import annotations
import asyncio
import textwrap
import types
import warnings

from . import TuberError, TuberStateError, TuberRemoteError
from .codecs import AcceptTypes, Codecs


__all__ = [
    "TuberObject",
    "SimpleTuberObject",
    "resolve",
    "resolve_simple",
]


async def resolve(hostname: str, objname: str | None = None, accept_types: list[str] | None = None):
    """Create a local reference to a networked resource.

    This is the recommended way to connect asynchronously to remote tuberd instances.
    """

    instance = TuberObject(objname, hostname=hostname, accept_types=accept_types)
    await instance.tuber_resolve()
    return instance


def resolve_simple(hostname: str, objname: str | None = None, accept_types: list[str] | None = None):
    """Create a local reference to a networked resource.

    This is the recommended way to connect serially to remote tuberd instances.
    """

    instance = SimpleTuberObject(objname, hostname=hostname, accept_types=accept_types)
    instance.tuber_resolve()
    return instance


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


class SubContext:
    """A container for attributes of a top-level (registry) Context object"""

    def __init__(self, objname: str, methods: list[str] | None, parent: "SimpleContext", **kwargs):
        self.objname = objname
        self.methods = methods
        self.parent = parent
        self.ctx_kwargs = kwargs

    def __getattr__(self, name: str):
        if attribute_blacklisted(name):
            raise AttributeError(f"{name} is not a valid method or property!")

        # Short-circuit for resolved objects
        if self.methods is not None and name not in self.methods:
            raise AttributeError(f"{name} is not a valid method or property!")

        # Call the parent context with this object name and its method
        def caller(*args, **kwargs):
            kwargs.update(self.parent.ctx_kwargs)
            kwargs.update(self.ctx_kwargs)
            return self.parent._add_call(object=self.objname, method=name, args=args, kwargs=kwargs)

        setattr(self, name, caller)
        return caller


class SimpleContext:
    """A serial context container for TuberCalls. Permits calls to be aggregated.

    Commands are dispatched strictly in-order, but are automatically bundled
    up to reduce roundtrips.
    """

    def __init__(self, obj: "SimpleTuberObject", *, accept_types: list[str] | None = None, **ctx_kwargs):
        self.calls: list[dict] = []
        self.obj = obj
        self.uri = f"http://{obj._tuber_host}/tuber"
        if accept_types is None:
            accept_types = self.obj._accept_types
        if accept_types is None:
            self.accept_types = list(AcceptTypes.keys())
        else:
            for accept_type in accept_types:
                if accept_type not in AcceptTypes.keys():
                    raise ValueError(f"Unsupported accept type: {accept_type}")
            self.accept_types = accept_types
        self.ctx_kwargs = ctx_kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.calls:
            self()

    def _add_call(self, **request):
        self.calls.append(request)

    def __getattr__(self, name):
        if attribute_blacklisted(name):
            raise AttributeError(f"{name} is not a valid method or property!")

        # Queue methods of registry entries using the top-level registry context
        if self.obj._tuber_objname is None:
            # Short-circuit for resolved objects
            try:
                objects = self.obj._tuber_meta.objects
            except AttributeError:
                objects = None
            if objects is not None and name not in objects:
                raise AttributeError(f"{name} is not a valid attribute!")

            try:
                methods = getattr(self.obj, name)._tuber_meta.methods
            except AttributeError:
                methods = None

            ctx = SubContext(name, methods=methods, parent=self)
            setattr(self, name, ctx)
            return ctx

        # Short-circuit for resolved objects
        try:
            methods = self.obj._tuber_meta.methods
        except AttributeError:
            methods = None
        if methods is not None and name not in methods:
            raise AttributeError(f"{name} is not a valid method or property!")

        # Queue methods calls.
        def caller(*args, **kwargs):
            # Add extra arguments where they're provided
            kwargs.update(self.ctx_kwargs)

            # ensure that a new unique future is returned
            # each time this function is called
            return self._add_call(object=self.obj._tuber_objname, method=name, args=args, kwargs=kwargs)

        setattr(self, name, caller)
        return caller

    def send(self, continue_on_error: bool = False):
        """Break off a set of calls and return them for execution."""

        # An empty Context returns an empty list of calls
        if not self.calls:
            return []

        calls = list(self.calls)
        self.calls.clear()

        import requests

        # Declare the media types we want to allow getting back
        headers = {"Accept": ", ".join(self.accept_types)}
        if continue_on_error:
            headers["X-Tuber-Options"] = "continue-on-error"
        # Create a HTTP request to complete the call.
        return requests.post(self.uri, json=calls, headers=headers)

    def receive(self, response: "requests.Response", continue_on_error: bool = False):
        """Parse response from a previously sent HTTP request."""

        # An empty Context returns an empty list of calls
        if response is None or response == []:
            return []

        with response as resp:
            raw_out = resp.content
            if not resp.ok:
                try:
                    text = resp.text
                except Exception:
                    raise TuberRemoteError(f"Request failed with status {resp.status_code}")
                raise TuberRemoteError(f"Request failed with status {resp.status_code}: {text}")
            content_type = resp.headers["Content-Type"]
            # Check that the resulting media type is one which can actually be handled;
            # this is slightly more liberal than checking that it is really among those we declared
            if content_type not in AcceptTypes:
                raise TuberError(f"Unexpected response content type: {content_type}")
            json_out = AcceptTypes[content_type](raw_out, resp.apparent_encoding)

        if hasattr(json_out, "error"):
            # Oops - this is actually a server-side error that bubbles
            # through. (See test_tuberpy_async_context_with_unserializable.)
            # We made an array request, and received an object response
            # because of an exception-catching scope in the server. Do the
            # best we can.
            raise TuberRemoteError(json_out.error.message)

        results = []
        for r in json_out:
            # Always emit warnings, if any occurred
            if hasattr(r, "warnings") and r.warnings:
                for w in r.warnings:
                    warnings.warn(w)

            # Resolve either a result or an error
            if hasattr(r, "error") and r.error:
                exc = TuberRemoteError(getattr(r.error, "message", "Unknown error"))
                if continue_on_error:
                    results.append(exc)
                else:
                    raise exc
            elif hasattr(r, "result"):
                results.append(r.result)
            else:
                raise TuberError("Result has no 'result' attribute")

        # Return a list of results
        return results

    def __call__(self, continue_on_error: bool = False):
        """Break off a set of calls and return them for execution."""

        resp = self.send(continue_on_error=continue_on_error)
        return self.receive(resp, continue_on_error=continue_on_error)


class Context(SimpleContext):
    """An asynchronous context container for TuberCalls. Permits calls to be
    aggregated.

    Commands are dispatched strictly in-order, but are automatically bundled
    up to reduce roundtrips.
    """

    def __init__(self, obj: "TuberObject", **kwargs):
        super().__init__(obj, **kwargs)
        self.calls: list[tuple[dict, asyncio.Future]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Ensure the context is flushed."""
        if self.calls:
            await self()

    def __enter__(self):
        raise NotImplementedError

    def __exit__(self):
        raise NotImplementedError

    def _add_call(self, **request):
        future = asyncio.Future()
        self.calls.append((request, future))
        return future

    async def __call__(self, continue_on_error: bool = False):
        """Break off a set of calls and return them for execution."""

        # An empty Context returns an empty list of calls
        if not self.calls:
            return []

        calls = []
        futures = []
        while self.calls:
            (c, f) = self.calls.pop(0)

            calls.append(c)
            futures.append(f)

        loop = asyncio.get_running_loop()
        if not hasattr(loop, "_tuber_session"):
            # hide import for non-library package that may not be invoked
            import aiohttp

            # Monkey-patch tuber session memory handling with the running event loop
            loop._tuber_session = aiohttp.ClientSession(json_serialize=Codecs["json"].encode)

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
        headers = {"Accept": ", ".join(self.accept_types)}
        if continue_on_error:
            headers["X-Tuber-Options"] = "continue-on-error"
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
                raise TuberError("Unexpected response content type: " + content_type)
            json_out = AcceptTypes[content_type](raw_out, resp.charset)

        if hasattr(json_out, "error"):
            # Oops - this is actually a server-side error that bubbles
            # through. (See test_tuberpy_async_context_with_unserializable.)
            # We made an array request, and received an object response
            # because of an exception-catching scope in the server. Do the
            # best we can.
            raise TuberRemoteError(json_out.error.message)

        # Resolve futures
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
                    f.set_result(r.result)
                else:
                    f.set_exception(TuberError("Result has no 'result' attribute"))

        # Return a list of results
        return [await f for f in futures]


class SimpleTuberObject:
    """A base class for serial TuberObjects.

    This is a great way of using Python to correspond with network resources
    over a HTTP tunnel. It hides most of the gory details and makes your
    networked resource look and behave like a local Python object.

    To use it, you should subclass this SimpleTuberObject.
    """

    _context_class = SimpleContext

    def __init__(
        self,
        objname: str | None,
        *,
        hostname: str | None = None,
        accept_types: list[str] | None = None,
        parent: "SimpleTuberObject" | None = None,
    ):
        self._tuber_objname = objname
        if parent is None:
            assert hostname, "Argument 'hostname' required"
            self._tuber_host = hostname
            self._accept_types = accept_types
        else:
            self._tuber_host = parent._tuber_host
            self._accept_types = parent._accept_types

    def object_factory(self, objname):
        """Construct a child TuberObject for the given resource name.

        Overload this method to create child objects using different subclasses.
        """
        if self._tuber_objname is not None:
            raise NotImplementedError
        return self.__class__(objname, parent=self)

    def tuber_context(self, **kwargs):
        """Return a context manager for aggregating method calls on this object."""

        return self._context_class(self, **kwargs)

    def tuber_resolve(self, force=False):
        """Retrieve metadata associated with the remote network resource.

        This class retrieves object-wide metadata, which is used to build
        up properties and methods with tab-completion and docstrings.
        """
        if not force:
            try:
                return (self._tuber_meta, self._tuber_meta_properties, self._tuber_meta_methods)
            except AttributeError:
                pass

        with self.tuber_context() as ctx:
            ctx._add_call(object=self._tuber_objname)
            meta = ctx()
            meta = meta[0]

            if self._tuber_objname is not None:
                for p in meta.properties:
                    ctx._add_call(object=self._tuber_objname, property=p)
                prop_list = ctx()

                for m in meta.methods:
                    ctx._add_call(object=self._tuber_objname, property=m)
                meth_list = ctx()

                props = dict(zip(meta.properties, prop_list))
                methods = dict(zip(meta.methods, meth_list))
            else:
                props = methods = None

        # Top-level registry entries
        for objname in getattr(meta, "objects", []):
            obj = self.object_factory(objname)
            obj.tuber_resolve()
            setattr(self, objname, obj)

        for propname in getattr(meta, "properties", []):
            setattr(self, propname, props[propname])

        for methname in getattr(meta, "methods", []):
            # Generate a callable prototype
            def invoke_wrapper(name):
                def invoke(self, *args, **kwargs):
                    with self.tuber_context() as ctx:
                        getattr(ctx, name)(*args, **kwargs)
                        results = ctx()
                    return results[0]

                return invoke

            invoke = invoke_wrapper(methname)

            # Attach DocStrings, if provided and valid
            try:
                invoke.__doc__ = textwrap.dedent(methods[methname].__doc__)
            except:
                pass

            # Associate as a class method.
            setattr(self, methname, types.MethodType(invoke, self))

        self.__doc__ = meta.__doc__
        self._tuber_meta = meta
        self._tuber_meta_properties = props
        self._tuber_meta_methods = methods
        return (meta, props, methods)


class TuberObject(SimpleTuberObject):
    """A base class for TuberObjects.

    This is a great way of using Python to correspond with network resources
    over a HTTP tunnel. It hides most of the gory details and makes your
    networked resource look and behave like a local Python object.

    To use it, you should subclass this TuberObject.
    """

    _context_class = Context

    async def tuber_resolve(self, force=False):
        """Retrieve metadata associated with the remote network resource.

        This class retrieves object-wide metadata, which is used to build
        up properties and methods with tab-completion and docstrings.
        """
        if not force:
            try:
                return (self._tuber_meta, self._tuber_meta_properties, self._tuber_meta_methods)
            except AttributeError:
                pass

        async with self.tuber_context() as ctx:
            ctx._add_call(object=self._tuber_objname)
            meta = await ctx()
            meta = meta[0]

            if self._tuber_objname is not None:
                for p in meta.properties:
                    ctx._add_call(object=self._tuber_objname, property=p)
                prop_list = await ctx()

                for m in meta.methods:
                    ctx._add_call(object=self._tuber_objname, property=m)
                meth_list = await ctx()

                props = dict(zip(meta.properties, prop_list))
                methods = dict(zip(meta.methods, meth_list))
            else:
                props = methods = None

        # Top-level registry entries
        for objname in getattr(meta, "objects", []):
            obj = self.object_factory(objname)
            await obj.tuber_resolve()
            setattr(self, objname, obj)

        for propname in getattr(meta, "properties", []):
            setattr(self, propname, props[propname])

        for methname in getattr(meta, "methods", []):
            # Generate a callable prototype
            def invoke_wrapper(name):
                async def invoke(self, *args, **kwargs):
                    async with self.tuber_context() as ctx:
                        result = getattr(ctx, name)(*args, **kwargs)
                    return await result

                return invoke

            invoke = invoke_wrapper(methname)

            # Attach DocStrings, if provided and valid
            try:
                invoke.__doc__ = textwrap.dedent(methods[methname].__doc__)
            except:
                pass

            # Associate as a class method.
            setattr(self, methname, types.MethodType(invoke, self))

        self.__doc__ = meta.__doc__
        self._tuber_meta = meta
        self._tuber_meta_properties = props
        self._tuber_meta_methods = methods
        return (meta, props, methods)


# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
