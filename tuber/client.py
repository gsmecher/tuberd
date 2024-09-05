"""
Tuber object interface
"""

from __future__ import annotations
import asyncio
import textwrap
import types
import warnings
import inspect

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


def attribute_blacklisted(name: str):
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


def tuber_wrapper(func: callable, meta: "TuberResult"):
    """
    Annotate the wrapper function with docstrings and signature.
    """

    # Attach docstring, if provided and valid
    try:
        func.__doc__ = textwrap.dedent(meta.__doc__)
    except:
        pass

    # Attach a function signature, if provided and valid
    try:
        # build a dummy function to parse its signature with inspect
        code = compile(f"def sigfunc{meta.__signature__}:\n pass", "sigfunc", "single")
        exec(code, globals())
        sig = inspect.signature(sigfunc)
        func.__signature__ = sig
    except:
        pass

    return func


def get_object_name(parent: str, attr: str | None = None, item: str | int | None = None):
    """
    Construct a valid object name for accessing objects in a registry.

    Arguments
    ---------
    parent : str
        Parent object name
    attr : str
        If supplied, this attribute name is joined with the parent name as "parent.attr".
    item : str or int
        If supplied, this item name is treated as an index into the parent as "parent[item]".

    Returns
    -------
    objname: str
        A valid object name.
    """
    if attr is not None:
        if item is not None:
            raise ValueError("Only one of 'attr' or 'item' arguments may be provided")
        return f"{parent}.{attr}"
    elif item is not None:
        return f"{parent}[{repr(item)}]"
    return parent


class SubContext:
    """A container for attributes of a Context object"""

    def __init__(self, objname: str, parent: "SimpleContext", attrname: str | None = None, **kwargs):
        self.objname = objname
        self.attrname = attrname
        self.parent = parent
        self.ctx_kwargs = kwargs
        self.container = {}

    def __call__(self, *args, **kwargs):
        """method-like sub-context"""
        kwargs.update(self.parent.ctx_kwargs)
        kwargs.update(self.ctx_kwargs)
        return self.parent._add_call(object=self.objname, method=self.attrname, args=args, kwargs=kwargs)

    def __getitem__(self, item: str | int):
        """container-like sub-context"""
        if item not in self.container:
            objname = get_object_name(self.objname, attr=self.attrname)
            objname = get_object_name(objname, item=item)
            self.container[item] = SubContext(objname, parent=self.parent)
        return self.container[item]

    def __getattr__(self, name: str):
        """object-like sub-context"""
        if attribute_blacklisted(name):
            raise AttributeError(f"{name} is not a valid method or property!")

        objname = get_object_name(self.objname, attr=self.attrname)
        caller = SubContext(objname, attrname=name, parent=self.parent)
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
        self.container = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.calls:
            self()

    def __getitem__(self, item: str | int):
        if item not in self.container:
            objname = get_object_name(self.obj._tuber_objname, item=item)
            self.container[item] = SubContext(objname, parent=self)
        return self.container[item]

    def __getattr__(self, name: str):
        if attribute_blacklisted(name):
            raise AttributeError(f"{name} is not a valid method or property!")

        # Queue methods of registry entries using the top-level registry context
        if self.obj._tuber_objname is None:
            ctx = SubContext(name, parent=self)
        else:
            ctx = SubContext(self.obj._tuber_objname, attrname=name, parent=self)

        setattr(self, name, ctx)
        return ctx

    def _add_call(self, **request):
        self.calls.append(request)

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

    @property
    def is_container(self):
        """True if object is a container (list or dict) of remote items,
        otherwise False if resolved or None if not resolved."""
        if hasattr(self, "_tuber_meta"):
            return hasattr(self, "_items")

    def __getattr__(self, name: str):
        # Useful hint
        raise AttributeError(f"'{self._tuber_objname}' has no attribute '{name}'.  Did you run tuber_resolve()?")

    def __len__(self):
        try:
            return len(self._items)
        except AttributeError:
            raise TypeError(f"'{self._tuber_objname}' object has no len()")

    def __getitem__(self, item: str | int):
        try:
            return self._items[item]
        except AttributeError:
            raise TypeError(f"'{self._tuber_objname}' object is not subscriptable")

    def __iter__(self):
        try:
            return iter(self._items)
        except AttributError:
            raise TypeError(f"'{self._tuber_objname}' object is not iterable")

    def object_factory(self, objname: str):
        """Construct a child TuberObject for the given resource name.

        Overload this method to create child objects using different subclasses.
        """
        return self.__class__(objname, parent=self)

    def tuber_context(self, **kwargs):
        """Return a context manager for aggregating method calls on this object."""

        return self._context_class(self, **kwargs)

    def tuber_resolve(self, force: bool = False):
        """Retrieve metadata associated with the remote network resource.

        This class retrieves object-wide metadata, which is used to build
        up properties and methods with tab-completion and docstrings.
        """
        if not force:
            try:
                return self._tuber_meta
            except AttributeError:
                pass

        with self.tuber_context() as ctx:
            ctx._add_call(object=self._tuber_objname, resolve=True)
            meta = ctx()
            meta = meta[0]

        return self._resolve_meta(meta)

    @staticmethod
    def _resolve_method(name: str, meta: "TuberResult"):
        """Resolve a remote method call into a callable function"""

        def invoke(self, *args, **kwargs):
            with self.tuber_context() as ctx:
                getattr(ctx, name)(*args, **kwargs)
                results = ctx()
            return results[0]

        return tuber_wrapper(invoke, meta)

    def _resolve_object(
        self, attr: str | None = None, item: str | int | None = None, meta: "TuberResult" | None = None
    ):
        """Create a TuberObject representing the given attribute or container
        item, resolving any supplied metadata."""
        assert attr is not None or item is not None, "One of attr or item required"
        if self._tuber_objname is None:
            objname = attr
        else:
            objname = get_object_name(self._tuber_objname, attr=attr, item=item)
        obj = self.object_factory(objname)
        if meta is not None:
            obj._resolve_meta(meta)
        return obj

    def _resolve_meta(self, meta: "TuberResult"):
        """Parse metadata packet and recursively resolve all attributes."""
        # docstring
        if hasattr(meta, "__doc__"):
            self.__doc__ = meta.__doc__

        # object attributes
        for objname in getattr(meta, "objects", []) or []:
            objmeta = getattr(meta.objects, objname)
            obj = self._resolve_object(attr=objname, meta=objmeta)
            setattr(self, objname, obj)

        # methods
        for methname in getattr(meta, "methods", []) or []:
            method = getattr(meta.methods, methname)
            if not callable(method):
                method = self._resolve_method(methname, method)
                # create method once and bind to each item in a container
                setattr(meta.methods, methname, method)
            setattr(self, methname, types.MethodType(method, self))

        # static properties
        for propname in getattr(meta, "properties", []) or []:
            setattr(self, propname, getattr(meta.properties, propname))

        # container of objects
        if hasattr(meta, "container"):
            if meta.container == "list":
                keys = range(len(meta.items))
                metaitems = meta.items
                items = [None] * len(keys)
            elif meta.container == "dict":
                keys = list(meta.items)
                metaitems = meta.items.__dict__
                items = dict()
            else:
                raise ValueError(f"Invalid container type {meta.container}")

            for k in keys:
                objmeta = metaitems[k]
                # populate common metadata elements
                objmeta.__doc__ = meta.item_doc
                objmeta.methods = meta.item_methods
                obj = self._resolve_object(item=k, meta=objmeta)
                items[k] = obj

            self._items = items

            if meta.container == "dict":
                setattr(self, "keys", types.MethodType(lambda o: o._items.keys(), self))
                setattr(self, "values", types.MethodType(lambda o: o._items.values(), self))
                setattr(self, "items", types.MethodType(lambda o: o._items.items(), self))

        self._tuber_meta = meta
        return meta


class TuberObject(SimpleTuberObject):
    """A base class for async TuberObjects.

    This is a great way of using Python to correspond with network resources
    over a HTTP tunnel. It hides most of the gory details and makes your
    networked resource look and behave like a local Python object.

    To use it, you should subclass this TuberObject.
    """

    _context_class = Context

    async def tuber_resolve(self, force: bool = False):
        """Retrieve metadata associated with the remote network resource.

        This class retrieves object-wide metadata, which is used to build
        up properties and methods with tab-completion and docstrings.
        """
        if not force:
            try:
                return self._tuber_meta
            except AttributeError:
                pass

        async with self.tuber_context() as ctx:
            ctx._add_call(object=self._tuber_objname, resolve=True)
            meta = await ctx()
            meta = meta[0]

        return self._resolve_meta(meta)

    @staticmethod
    def _resolve_method(name: str, meta: "TuberResult"):
        """Resolve a remote method call into an async callable function"""

        async def invoke(self, *args, **kwargs):
            async with self.tuber_context() as ctx:
                getattr(ctx, name)(*args, **kwargs)
                results = await ctx()
            return results[0]

        return tuber_wrapper(invoke, meta)


# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
