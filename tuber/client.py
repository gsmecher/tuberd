"""
Tuber object interface
"""

from __future__ import annotations
import asyncio
import concurrent
import textwrap
import types
import warnings
import inspect
import functools

from . import TuberError, TuberStateError, TuberRemoteError
from .codecs import AcceptTypes, Codecs, TuberResult


__all__ = [
    "TuberObject",
    "SimpleTuberObject",
    "resolve",
    "resolve_simple",
]


async def resolve(
    hostname: str,
    objname: str | None = None,
    accept_types: list[str] | None = None,
    convert_json: bool | None = None,
    return_exceptions: bool | None = None,
):
    """Create a local reference to a networked resource.

    This is the recommended way to connect asynchronously to remote tuberd instances.

    Arguments
    ---------
    hostname : str
        Hostname to connect to.  Maybe an IP address or a resolved DNS name.
    objname : str
        Object to attach to on the server.  If None, attach to the top-level registry.
        Otherwise must be an entry in the registry dictionary.
    accept_types : list of str
        List of codecs that the client is able to decode.
    convert_json : bool
        If True, convert json dicts to namespace objects in the server response.  If
        False, return any non-error outputs as standard Python dicts.  Otherwise,
        fall back to context default.  This default may be overridden in the context
        construction or in each individual context call.
    return_exceptions : bool
        If True, return exceptions in the server response, allowing inspection of all
        entries in the response list.  If False, any errors in the output are raised
        as exceptions.  Otherwise, fall back to context default.  This default may be
        overridden in the context construction or in each individual context call.

    Returns
    -------
    obj : TuberObject
        A tuber object whose methods are awaitable, and may be used with the
        ``asyncio`` library for asynchronous execution.
    """

    instance = TuberObject(
        objname,
        hostname=hostname,
        accept_types=accept_types,
        convert_json=convert_json,
        return_exceptions=return_exceptions,
    )
    await instance.tuber_resolve()
    return instance


def resolve_simple(
    hostname: str,
    objname: str | None = None,
    accept_types: list[str] | None = None,
    convert_json: bool | None = None,
    return_exceptions: bool | None = None,
):
    """Create a local reference to a networked resource.

    This is the recommended way to connect serially to remote tuberd instances.

    Arguments
    ---------
    hostname : str
        Hostname to connect to.  Maybe an IP address or a resolved DNS name.
    objname : str
        Object to attach to on the server.  If None, attach to the top-level registry.
        Otherwise must be an entry in the registry dictionary.
    accept_types : list of str
        List of codecs that the client is able to decode.
    convert_json : bool
        If True, convert json dicts to namespace objects in the server response.  If
        False, return any non-error outputs as standard Python dicts.  Otherwise,
        fall back to context default.  This default may be overridden in the context
        construction or in each individual context call.
    return_exceptions : bool
        If True, return exceptions in the server response, allowing inspection of all
        entries in the response list.  If False, any errors in the output are raised
        as exceptions.  Otherwise, fall back to context default.  This default may be
        overridden in the context construction or in each individual context call.

    Returns
    -------
    obj : SimpleTuberObject
        A tuber object whose methods may be called serially, and can be integrated with
        the ``concurrent`` library for asynchronous execution.
    """

    instance = SimpleTuberObject(
        objname,
        hostname=hostname,
        accept_types=accept_types,
        convert_json=convert_json,
        return_exceptions=return_exceptions,
    )
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


def tuber_wrapper(func: callable, meta: dict):
    """
    Annotate the wrapper function with docstrings and signature.
    """

    # Attach docstring, if provided and valid
    try:
        func.__doc__ = textwrap.dedent(meta["__doc__"])
    except:
        pass

    # Attach a function signature, if provided and valid
    try:
        if isinstance(meta["__signature__"], str):
            func.__text_signature__ = meta["__signature__"]
        else:
            sig = meta["__signature__"]
            if not isinstance(sig["parameters"][0], inspect.Parameter):
                sig["parameters"] = [inspect.Parameter(**p) for p in sig["parameters"]]
            func.__signature__ = inspect.Signature(**sig)
    except:
        pass

    return func


def get_object_name(parent: str | list, attr: str | None = None, item: str | int | None = None):
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
    objname: list
        A valid object name.
    """
    if isinstance(parent, str):
        out = [parent]
    else:
        out = parent
    if item is None and attr is None:
        return out
    if attr is not None:
        out = out + [attr]
    if item is not None:
        last = out[-1]
        if isinstance(last, str):
            last = [last]
        else:
            last = list(last)
        out = out[:-1] + [tuple(last + [item])]
    return out


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
            objname = get_object_name(self.objname, attr=self.attrname, item=item)
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

    def __init__(
        self,
        obj: "SimpleTuberObject",
        *,
        accept_types: list[str] | None = None,
        convert_json: bool | None = None,
        return_exceptions: bool | None = None,
        **ctx_kwargs,
    ):
        """
        Arguments
        ---------
        obj : SimpleTuberObject
            Parent tuber object whose methods to call.
        accept_types : list of str
            List of codecs that the client is able to decode.
        convert_json : bool
            If True (default), all responses from the server should be converted into
            namespace objects by default.  This default may be overridden in the
            context construction or in each individual context call.
        return_exceptions : bool
            If True, return server-side exceptions in the response list by default.
            If False (default), raise the exception when parsing the server response.
            This default may be overridden in the context construction or in each
            individual context call.
        ctx_kwargs :
            Any remaining keyword arguments are added as additional keywords to any
            method call made by this context.
        """
        self.calls: list[tuple[dict, "Future"]] = []
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
        if convert_json is None:
            convert_json = self.obj._convert_json
        self.convert_json = True if convert_json is None else convert_json
        if return_exceptions is None:
            return_exceptions = self.obj._return_exceptions
        self.return_exceptions = False if return_exceptions is None else return_exceptions
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
            ctx = SubContext([name], parent=self)
        else:
            ctx = SubContext(self.obj._tuber_objname, attrname=name, parent=self)

        setattr(self, name, ctx)
        return ctx

    def _add_call(self, **request):
        future = concurrent.futures.Future()
        self.calls.append((request, future))
        return future

    def send(self, convert_json: bool | None = None, return_exceptions: bool | None = None):
        """Break off a set of calls and return them for execution.

        Arguments
        ---------
        convert_json : bool
            If True, convert json dicts to namespace objects in the server response.
            If False, return any non-error outputs as standard Python dicts.
            Otherwise, fall back to context default.
        return_exceptions : bool
            If True, return exceptions in the server response, allowing inspection of
            all entries in the response list.  If False, any errors in the output
            are raised as exceptions.  Otherwise, fall back to context default.

        Returns
        -------
        response : concurrent.futures.Future
            Future object corresponding to the server request.  Use ``receive()`` to
            retrieve the result from the server.
        """

        # An empty Context returns an empty list of calls
        if not self.calls:
            return

        calls = []
        futures = []
        while self.calls:
            (c, f) = self.calls.pop(0)

            calls.append(c)
            futures.append(f)

        if not hasattr(self.obj, "_tuber_session"):
            # session object should persist beyond the lifetime of the context,
            # akin to the asyncio event loop
            from requests_futures.sessions import FuturesSession

            self.obj._tuber_session = FuturesSession()

        cs = self.obj._tuber_session

        # Declare the media types we want to allow getting back
        headers = {"Accept": ", ".join(self.accept_types)}
        if return_exceptions:
            headers["X-Tuber-Options"] = "continue-on-error"

        # Hook function for parsing the response from the server
        def hook(r, *args, **kwargs):
            self._receive(r, futures, convert_json, return_exceptions)
            return r

        # Create a HTTP request to complete the call.
        # Returns a Future whose result has been processed by the response hook.
        return cs.post(self.uri, json=calls, headers=headers, hooks={"response": hook})

    @staticmethod
    def _parse_json(json_out, futures: list, converted: bool, return_exceptions: bool):
        """Parse json object and assign results to futures corresponding to the set of
        calls that were sent to the server.

        Arguments
        ---------
        json_out:
            JSON-decoded response from the server. May be an error message or a list
            of responses, one per server call.
        futures: list
            List of futures corresponding to each call sent to the server.
        converted : bool
            If True, any dicts in json_out have been converted to namespace objects.
            Otherwise, dicts remain as they were returned from the server.
        return_exceptions : bool
            If True, return exceptions in the server response, allowing inspection of
            all entries in the response list.  Otherwise, any errors in the output
            are raised as exceptions.

        Returns
        -------
        responses : list
            List of responses from the server, corresponding to the set of sent calls.
            Each response is also assigned to its corresponding future.  If
            ``return_exceptions`` is True, each response may be a ``TuberRemoteError``
            object, rather than a namespace object (if ``converted`` is True) or a dict.
        """
        if converted:
            haskey = hasattr

            def getkey(d, *k):
                return functools.reduce(lambda d, k: getattr(d, k), k, d)

        else:

            def haskey(d, k):
                return isinstance(d, dict) and k in d

            def getkey(d, *k):
                return functools.reduce(lambda d, k: d[k], k, d)

        if haskey(json_out, "error"):
            # Oops - this is actually a server-side error that bubbles
            # through. (See test_tuberpy_async_context_with_unserializable.)
            # We made an array request, and received an object response
            # because of an exception-catching scope in the server. Do the
            # best we can.
            for f in futures:
                f.cancel()
            raise TuberRemoteError(getkey(json_out, "error", "message"))

        for f, r in zip(futures, json_out):
            # Always emit warnings, if any occurred
            if haskey(r, "warnings") and getkey(r, "warnings"):
                for w in getkey(r, "warnings"):
                    warnings.warn(w)

            # Resolve either a result or an error
            if haskey(r, "error") and getkey(r, "error"):
                err = getkey(r, "error")
                if haskey(err, "message"):
                    f.set_exception(TuberRemoteError(getkey(err, "message")))
                else:
                    f.set_exception(TuberRemoteError("Unknown error"))
            else:
                if haskey(r, "result"):
                    f.set_result(getkey(r, "result"))
                else:
                    f.set_exception(TuberError("Result has no 'result' attribute"))

        # Return a list of results
        if return_exceptions:
            out = []
            for f in futures:
                try:
                    out.append(f.result())
                except Exception as e:
                    out.append(e)
            return out

        return [f.result() for f in futures]

    def _receive(
        self,
        response: "requests.Response",
        futures: list["concurrent.futures.Future"],
        convert_json: bool | None = None,
        return_exceptions: bool | None = None,
    ):
        """Parse response from a previously sent HTTP request.  Assign results to
        futures corresponding to the set of calls that were sent to the server, and
        store the results as the ``.tuber_results`` attribute of the response object.

        Arguments
        ---------
        response : requests.Response
            Response object corresponding to the server request.
        futures : list
            List of futures corresponding to each call sent to the server.
        convert_json : bool
            If True, convert json dicts to namespace objects in the server response.
            If False, return any non-error outputs as standard Python dicts.
            Otherwise, fall back to context default.
        return_exceptions : bool
            If True, return exceptions in the server response, allowing inspection of
            all entries in the response list.  If False, any errors in the output
            are raised as exceptions.  Otherwise, fall back to context default.

        Returns
        -------
        responses : list
            List of responses from the server, corresponding to the set of sent calls.
            Each response is also assigned to its corresponding future.  If
            ``return_exceptions`` is True, each response may be a ``TuberRemoteError``
            object, rather than a namespace object (if ``convert_json`` is True) or a dict.
        """

        if convert_json is None:
            convert_json = self.convert_json
        if return_exceptions is None:
            return_exceptions = self.return_exceptions

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
            json_out = AcceptTypes[content_type](raw_out, resp.apparent_encoding, convert=convert_json)

        response.tuber_results = self._parse_json(json_out, futures, convert_json, return_exceptions)
        return response.tuber_results

    def receive(self, response: "concurrent.futures.Future"):
        """Wait for a response from a previously sent HTTP request.

        Arguments
        ---------
        response : requests.Response
            Response object corresponding to the server request.

        Returns
        -------
        responses : list
            List of responses from the server, corresponding to the set of sent calls.
        """
        if response is None:
            return []
        return response.result().tuber_results

    def __call__(self, convert_json: bool | None = None, return_exceptions: bool | None = None):
        """Wait for any pending calls to complete and return the results from the server

        Arguments
        ---------
        convert_json : bool
            If True, convert json dicts to namespace objects in the server response.
            If False, return any non-error outputs as standard Python dicts.
            Otherwise, fall back to context default.
        return_exceptions : bool
            If True, return exceptions in the server response, allowing inspection of
            all entries in the response list.  If False, any errors in the output
            are raised as exceptions.  Otherwise, fall back to context default.

        Returns
        -------
        response : list
            List of responses from the server, corresponding to each of the requested
            calls.
        """
        resp = self.send(convert_json=convert_json, return_exceptions=return_exceptions)
        return self.receive(resp)


class Context(SimpleContext):
    """An asynchronous context container for TuberCalls. Permits calls to be
    aggregated.

    Commands are dispatched strictly in-order, but are automatically bundled
    up to reduce roundtrips.
    """

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
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.calls.append((request, future))
        return future

    async def __call__(self, convert_json: bool | None = None, return_exceptions: bool | None = None):
        """Break off a set of calls and return them for execution.

        Arguments
        ---------
        convert_json : bool
            If True, convert json dicts to namespace objects in the server response.
            If False, return any non-error outputs as standard Python dicts.
            Otherwise, fall back to context default.
        return_exceptions : bool
            If True, return exceptions in the server response, allowing inspection of
            all entries in the response list.  If False, any errors in the output
            are raised as exceptions.  Otherwise, fall back to context default.

        Returns
        -------
        response : list
            List of responses from the server, corresponding to each of the requested
            calls.
        """

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

            # aiohttp.resolver.AsyncResolver does not support mDNS and is the
            # DefaultResolver. Instead, we try to force the use of an
            # MDNS-capable async resolver (if available) and use a threaded
            # fallback that supports mDNS.
            try:
                from aiohttp_asyncmdnsresolver.api import AsyncMDNSResolver as Resolver
            except ImportError:
                Resolver = aiohttp.resolver.ThreadedResolver

            # Monkey-patch tuber session memory handling with the running event loop
            loop._tuber_session = aiohttp.ClientSession(
                json_serialize=Codecs["json"].encode, connector=aiohttp.TCPConnector(resolver=Resolver())
            )

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

        if convert_json is None:
            convert_json = self.convert_json
        if return_exceptions is None:
            return_exceptions = self.return_exceptions

        # Declare the media types we want to allow getting back
        headers = {"Accept": ", ".join(self.accept_types)}
        if return_exceptions:
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
            json_out = AcceptTypes[content_type](raw_out, resp.charset, convert=convert_json)

        return self._parse_json(json_out, futures, convert_json, return_exceptions)


class SimpleTuberObject:
    """A base class for serial TuberObjects.

    This is a great way of using Python to correspond with network resources
    over a HTTP tunnel. It hides most of the gory details and makes your
    networked resource look and behave like a local Python object.

    To use it, you should subclass this SimpleTuberObject.
    """

    _context_class = SimpleContext
    _tuber_objname = None

    def __init__(
        self,
        objname: str | None,
        *,
        hostname: str | None = None,
        accept_types: list[str] | None = None,
        convert_json: bool | None = None,
        return_exceptions: bool | None = None,
        parent: "SimpleTuberObject" | None = None,
    ):
        """
        Arguments
        ---------
        objname : str
            Object to attach to on the server.  If None, attach to the top-level registry.
            Otherwise must be an entry in the registry dictionary.
        hostname : str
            Hostname to connect to.  Maybe an IP address or a resolved DNS name.
        accept_types : list of str
            List of codecs that the client is able to decode.
        convert_json : bool
            If True, convert json dicts to namespace objects in the server response.
            If False, return any non-error outputs as standard Python dicts.
            Otherwise, fall back to context default.  This default may be overridden
            in the context construction or in each individual context call.
        return_exceptions : bool
            If True, return exceptions in the server response, allowing inspection of
            all entries in the response list.  If False, any errors in the output are
            raised as exceptions.  Otherwise, fall back to context default.  This
            default may be overridden in the context construction or in each
            individual context call.
        parent: SimpleTuberObject
            If given, assume this object is an attribute of this parent object.
        """
        self._tuber_objname = objname
        self._tuber_resolved = False
        if parent is None:
            assert hostname, "Argument 'hostname' required"
            self._tuber_host = hostname
            self._accept_types = accept_types
            self._convert_json = convert_json
            self._return_exceptions = return_exceptions
        else:
            self._tuber_host = parent._tuber_host
            self._accept_types = parent._accept_types
            self._convert_json = parent._convert_json
            self._return_exceptions = parent._return_exceptions

    @property
    def is_container(self):
        warnings.warn(
            "Please replace TuberObject.is_container with "
            "TuberObject.tuber_is_container - this maintains "
            "namespace separation",
            DeprecationWarning,
        )
        return self.tuber_is_container

    @property
    def tuber_is_container(self):
        """True if object is a container (list or dict) of remote items,
        otherwise False if resolved or None if not resolved."""
        if self._tuber_resolved:
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
        except AttributeError:
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
        if self._tuber_resolved and not force:
            return

        with self.tuber_context(convert_json=False, return_exceptions=False) as ctx:
            ctx._add_call(object=self._tuber_objname, resolve=True)
            meta = ctx()
            meta = meta[0]

        self._resolve_meta(meta)

    @staticmethod
    def _resolve_method(name: str, meta: dict):
        """Resolve a remote method call into a callable function"""

        def invoke(self, *args, **kwargs):
            with self.tuber_context() as ctx:
                r = getattr(ctx, name)(*args, **kwargs)
            return r.result()

        return tuber_wrapper(invoke, meta)

    def _resolve_object(
        self,
        attr: str | None = None,
        item: str | int | None = None,
        meta: dict | None = None,
    ):
        """Create a TuberObject representing the given attribute or container
        item, resolving any supplied metadata."""
        assert attr is not None or item is not None, "One of attr or item required"
        if self._tuber_objname is None:
            objname = [attr]
        else:
            objname = get_object_name(self._tuber_objname, attr=attr, item=item)
        obj = self.object_factory(objname)
        if meta is not None:
            obj._resolve_meta(meta)
        return obj

    def _resolve_meta(self, meta: dict):
        """Parse metadata packet and recursively resolve all attributes."""

        # docstring
        if doc := meta.get("__doc__", None):
            self.__doc__ = meta["__doc__"]

        # object attributes
        objects = meta.get("objects", {})
        for k, v in objects.items():
            obj = self._resolve_object(attr=k, meta=v)
            setattr(self, k, obj)

        # methods
        if methods := meta.setdefault("methods", {}):
            # backwards compatibility for v0.15 and older: when assembling
            # metadata, older tuberd did not understand the "resolve=True"
            # argument and returned a list of methods as metadata, rather than
            # a dictionary that describes each one. Because this is a transient
            # workaround for newer client / older server installs, it's more
            # important that this code path is comprehensible than performant.
            # To get the job done, we create a transient SimpleTuberObject
            # rather than manage potentially async calls in what is normally a
            # synchronous context.
            if isinstance(methods, list):
                with SimpleContext(self, convert_json=False, return_exceptions=False) as ctx:
                    for m in methods:
                        ctx._add_call(object=self._tuber_objname, property=m)
                    methods = meta["methods"] = dict(zip(methods, ctx()))

            for k, v in methods.items():
                if not callable(v):
                    v = self._resolve_method(k, v)
                    # create method once and bind to each item in a container
                    methods[k] = v

                setattr(self, k, types.MethodType(v, self))

        # static properties
        if properties := meta.setdefault("properties", {}):
            # same workaround as above
            if isinstance(properties, list):
                with SimpleContext(self, convert_json=False, return_exceptions=False) as ctx:
                    for m in properties:
                        ctx._add_call(object=self._tuber_objname, property=m)
                    meta["properties"] = properties = dict(zip(properties, ctx()))

            # properties are really the only place where metadata is exported
            # directly into a user-visible object. If "convert_json" is True,
            # we need to ensure any dict-like properties are converted into
            # TuberResult objects.
            def recurse(obj: Any) -> Any:
                if isinstance(obj, dict):
                    return TuberResult(**{k: recurse(v) for k, v in obj.items()})
                return obj

            for k, v in properties.items():
                setattr(self, k, recurse(v) if self._convert_json else v)

        # container of objects
        if values := meta.setdefault("values", None):
            keys = meta.get("keys", None)
            if keys is None or isinstance(keys, int):
                islist = True
                if isinstance(keys, int):
                    size = keys
                    values = [values] * size
                else:
                    size = len(values)
                keys = range(size)
                items = [None] * size
            else:
                islist = False
                if not isinstance(values, list):
                    values = [values] * len(keys)
                items = dict()

            for k, objmeta in zip(keys, values):
                items[k] = self._resolve_object(item=k, meta=objmeta)

            self._items = items

            if not islist:
                setattr(self, "keys", types.MethodType(lambda o: o._items.keys(), self))
                setattr(self, "values", types.MethodType(lambda o: o._items.values(), self))
                setattr(self, "items", types.MethodType(lambda o: o._items.items(), self))

            def tuber_get(self, name: str, keys: list[str | int] | None = None):
                """Get a property of every container item.

                Return a list of property values for each item.  If ``keys`` is
                supplied, return only the values corresponding to the given set
                of container items.
                """
                if keys is None:
                    if isinstance(self._items, list):
                        keys = range(len(self._items))
                    else:
                        keys = self._items.keys()
                return [getattr(self._items[k], name) for k in keys]

            setattr(self, "tuber_get", types.MethodType(tuber_get, self))

        self._tuber_resolved = True
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
        if self._tuber_resolved and not force:
            return

        async with self.tuber_context(convert_json=False, return_exceptions=False) as ctx:
            ctx._add_call(object=self._tuber_objname, resolve=True)
            meta = await ctx()
            meta = meta[0]

        self._resolve_meta(meta)

    @staticmethod
    def _resolve_method(name: str, meta: dict):
        """Resolve a remote method call into an async callable function"""

        async def invoke(self, *args, **kwargs):
            async with self.tuber_context() as ctx:
                getattr(ctx, name)(*args, **kwargs)
                results = await ctx()
            return results[0]

        return tuber_wrapper(invoke, meta)


# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
