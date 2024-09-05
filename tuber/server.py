import inspect
import warnings
from .codecs import Codecs, TuberResult

__all__ = ["TuberContainer", "run"]


# request handling
def result_response(arg=None, **kwargs):
    """
    Return a valid result response to the server to be parsed by the client.
    Inputs must be either a single positional argument or a set of keyword
    arguments.
    """
    return {"result": kwargs or arg}


def error_response(message):
    """
    Return an error message to the server to be raised by the client.
    """
    if isinstance(message, Exception):
        message = f"{message.__class__.__name__}: {str(message)}"
    return {"error": {"message": message}}


def resolve_method(method):
    """
    Return a description of a method.
    """
    doc = inspect.getdoc(method)
    sig = None
    try:
        sig = str(inspect.signature(method))
    except:
        # pybind docstrings include a signature as the first line
        if doc and doc.startswith(method.__name__ + "("):
            if "\n" in doc:
                sig, doc = doc.split("\n", 1)
                doc = doc.strip()
            else:
                sig = doc
                doc = None
            sig = "(" + sig.split("(", 1)[1]

    return dict(__doc__=doc, __signature__=sig)


def resolve_object(obj, simple=False, only_attrs=None):
    """
    Return a dictionary of all valid object attributes classified by type.
    If simple=False, return dictionaries with complete descriptions of all
    child attributes, recursing through the entire object tree.
    If only_attrs is given, only include these attributes in the output set.

    objects: TuberContainer objects that are to be further resolved
    methods: Callable attributes
    properties: Static property attributes
    """

    if not simple and isinstance(obj, TuberContainer):
        return obj.resolve()

    objects = {}
    methods = {}
    props = {}
    clsname = obj.__class__.__name__

    if only_attrs is None:
        only_attrs = dir(obj)

    for d in only_attrs:
        # Don't export dunder methods or attributes - this avoids exporting
        # Python internals on the server side to any client.
        if d.startswith("__") or d.startswith(f"_{clsname}__"):
            continue
        attr = getattr(obj, d)
        if getattr(attr, "__tuber_object__", False):
            objects[d] = True if simple else resolve_object(attr)
        elif callable(attr):
            methods[d] = True if simple else resolve_method(attr)
        else:
            props[d] = attr

    if simple:
        objects = list(objects)
        methods = list(methods)
        props = list(props)

    return dict(__doc__=inspect.getdoc(obj), objects=objects, methods=methods, properties=props)


class TuberContainer:
    """Container for grouping objects of the same type"""

    __tuber_object__ = True

    def __init__(self, data):
        """
        Arguments
        ---------
        data : list or dict
            All values in the collection are assumed to have the same set of methods and properties.
        """
        if isinstance(data, list):
            if not data:
                raise ValueError("Empty list container")
            values = data
        elif isinstance(data, dict):
            if not data:
                raise ValueError("Empty dict container")
            values = list(data.values())
        else:
            raise TypeError("Invalid container type")

        # Ensure that all objects are the same type
        tp = type(values[0])
        for v in values:
            # check for exact match
            if type(v) != tp:
                raise TypeError(f"All entries must be of type {tp}")

        self._tuber_container = type(data).__name__
        self.__data = data

    def resolve(self):
        """
        Return a dict with descriptions of all items in the collection, with
        sufficient information to reconstruct the collection on the client side.
        """
        if self._tuber_container == "list":
            out = [None] * len(self.__data)
            keys = range(len(out))
        else:
            out = {}
            keys = self.__data.keys()

        item_attrs = None
        doc = None
        methods = None
        for k in keys:
            res = resolve_object(self.__data[k], only_attrs=item_attrs)
            if "container" not in res:
                if item_attrs is None:
                    doc = res.pop("__doc__", None)
                    methods = res.pop("methods", {})
                    item_attrs = list(res.get("objects", [])) + list(res.get("properties", []))
                else:
                    res.pop("__doc__", None)
                    res.pop("methods", None)
            out[k] = res

        return {"container": self._tuber_container, "item_doc": doc, "item_methods": methods, "items": out}

    def __getattr__(self, name):
        return getattr(self.__data, name)

    def __len__(self):
        return len(self.__data)

    def __iter__(self):
        return iter(self.__data)

    def __getitem__(self, item):
        if isinstance(item, slice):
            return TuberContainer(self.__data[item])
        return self.__data[item]


class TuberRegistry(TuberResult):
    """
    Registry class.
    """

    def __getitem__(self, objname):
        """
        Extract an object from the registry for the given name.  The object name
        may be a simple string name for the registry entry, or an attribute
        accessor.  For example, the name

            "Class.Attribute[0]"

        results in the object

            registry["Class"].Attribute[0]
        """
        try:
            return eval(f"self.{objname}")
        except Exception as e:
            raise e.__class__(f"{str(e)} (Invalid object name '{objname}')")


def describe(registry, request):
    """
    Tuber slow path

    This is invoked with a "request" object that does _not_ contain "object"
    and "method" keys, which would indicate a RPC operation.

    Instead, we are requesting one of the following:

    - A registry descriptor (no "object" or "method" or "property")
    - An object descriptor ("object" but no "method" or "property")
    - A container descriptor ("object" and a "property" corresponding to a container)
    - A method descriptor ("object" and a "property" corresponding to a method)
    - A property descriptor ("object" and a "property" that is static data)

    Since these are all cached on the client side, we are more concerned about
    correctness and robustness than performance here.
    """

    objname = request["object"] if "object" in request else None
    methodname = request["method"] if "method" in request else None
    propertyname = request["property"] if "property" in request else None
    resolve = request["resolve"] if "resolve" in request else False

    if not objname and not methodname and not propertyname:
        # registry metadata
        if resolve:
            objects = {obj: resolve_object(registry[obj]) for obj in registry}
        else:
            objects = list(registry)
        return result_response(objects=objects)

    obj = registry[objname]

    if not methodname and not propertyname:
        # Object metadata.
        return result_response(**resolve_object(obj, simple=not resolve))

    if propertyname:
        # Sanity check
        if not hasattr(obj, propertyname):
            raise AttributeError(f"'{objname}' object has no attribute '{propertyname}'")

        # Returning a method description or property evaluation
        attr = getattr(obj, propertyname)

        # Complex case: return a description of an object
        if getattr(attr, "__tuber_object__", False):
            return result_response(**resolve_object(attr, simple=not resolve))

        # Simple case: just a property evaluation
        if not callable(attr):
            return result_response(attr)

        # Complex case: return a description of a method
        return result_response(**resolve_method(attr))

    raise ValueError(f"Invalid request ({request})")


def invoke(registry, request):
    """
    Tuber command path

    This is invoked with a "request" object with any of the following combinations of keys:

    - A registry descriptor (no "object" or "method" or "property")
    - An object descriptor ("object" but no "method" or "property")
    - A method descriptor ("object" and a "property" corresponding to a method)
    - A property descriptor ("object" and a "property" that is static data)
    - A method call ("object" and "method", with optional "args" and/or "kwargs")
    """

    try:
        if not ("object" in request and "method" in request):
            return describe(registry, request)

        objname = request["object"]
        obj = registry[objname]

        methodname = request["method"]
        method = getattr(obj, methodname)

        args = request.get("args", [])
        if not isinstance(args, list):
            raise TypeError(f"Argument 'args' for method {objname}.{methodname} must be a list.")

        kwargs = request.get("kwargs", {})
        if not isinstance(kwargs, dict):
            raise TypeError(f"Argument 'kwargs' for method {objname}.{methodname} must be a dict.")

    except Exception as e:
        return error_response(e)

    with warnings.catch_warnings(record=True) as wlist:
        try:
            response = result_response(method(*args, **kwargs))
        except Exception as e:
            response = error_response(e)

        if len(wlist):
            response["warnings"] = [str(w.message) for w in wlist]

    return response


class RequestHandler:
    """
    Tuber server request handler.
    """

    def __init__(self, registry, json_module="json", default_format="application/json"):
        """
        Arguments
        ---------
        registry : dict
            Dictionary of user-defined objects with properties and methods.
        json_module : str
            Python package to use for encoding and decoding JSON requests.
        default_format : str
            Default encoding format to assume for requests and responses.
        """
        # ensure registry is a dictionary
        assert isinstance(registry, dict), "Invalid registry"
        self.registry = TuberRegistry(registry)

        # populate codecs
        self.codecs = {}

        try:
            self.codecs["application/json"] = Codecs[json_module]
        except Exception as e:
            raise RuntimeError(f"Unable to import {json_module} codec ({str(e)})")

        try:
            self.codecs["application/cbor"] = Codecs["cbor"]
        except Exception as e:
            warnings.warn(f"Unable to import cbor codec ({str(e)})")

        assert default_format in self.codecs, f"Missing codec for {default_format}"
        self.default_format = default_format

    def encode(self, data, fmt=None):
        """
        Encode the input data using the requested format.

        Returns the response format and the encoded data.
        """
        if fmt is None:
            fmt = self.default_format
        return fmt, self.codecs[fmt].encode(data)

    def decode(self, data, fmt=None):
        """
        Decode the input data using the requested format.

        Returns the decoded data.
        """
        if fmt is None:
            fmt = self.default_format
        return self.codecs[fmt].decode(data)

    def handle(self, request, headers):
        """
        Handle the input request from the server.

        Arguments
        ---------
        request : str
            Encoded request string.
        headers : dict
            Dictionary of headers from the posted request.  Valid keys are:

                Content-Type: a string containing a valid request format type,
                    e.g. "application/json" or "application/cbor"
                Accept: a string containing a valid response format type,
                    e.g. "application/json", "application/cbor" or "*/*"
                X-Tuber-Options: a configuration option for the handler,
                    e.g. "continue-on-error"

        Returns
        -------
        response_format : str
            The format into which the response is encoded
        response : str
            The encoded response string
        """
        request_format = response_format = self.default_format
        encode = lambda d: self.encode(d, response_format)

        try:
            # parse request format
            content_type = headers.get("Content-Type", request_format)
            if content_type not in self.codecs:
                raise ValueError(f"Not able to decode media type {content_type}")
            # Default to using the same response format as the request
            request_format = response_format = content_type

            # parse response format
            if "Accept" in headers:
                accept_types = [v.strip() for v in headers["Accept"].split(",")]
                if "*/*" in accept_types or "application/*" in accept_types:
                    response_format = request_format
                else:
                    for t in accept_types:
                        if t in self.codecs:
                            response_format = t
                            break
                    else:
                        msg = f"Not able to encode any media type matching {headers['Accept']}"
                        raise ValueError(msg)

            # decode request
            request_obj = self.decode(request, request_format)

            # parse single request
            if isinstance(request_obj, dict):
                result = invoke(self.registry, request_obj)
                return encode(result)

            if not isinstance(request_obj, list):
                raise TypeError("Unexpected type in request")

            # optionally allow requests to continue to the next item if an error
            # is raised for any request in the list
            xopts = [v.strip() for v in headers.get("X-Tuber-Options", "").split(",")]
            continue_on_error = "continue-on-error" in xopts

            # parse sequence of requests
            results = [None for r in request_obj]
            early_bail = False
            for i, r in enumerate(request_obj):
                if early_bail:
                    results[i] = error_response("Something went wrong in a preceding call")
                    continue

                results[i] = invoke(self.registry, r)

                if "error" in results[i] and not continue_on_error:
                    early_bail = True

            return encode(results)

        except Exception as e:
            return encode(error_response(e))

    def __call__(self, *args, **kwargs):
        return self.handle(*args, **kwargs)


def run(registry, json_module="json", port=80, webroot="/var/www/", max_age=3600, verbose=0):
    """
    Run tuber server with the given registry.

    Arguments
    ---------
    registry : dict
        Dictionary of user-defined objects with properties and methods.
    json_module : str
        Python package to use for encoding and decoding JSON requests.
    port : int
        Port on which to run the server
    webroot : str
        Location to serve static content
    max_age : int
        Maximum cache residency for static (file) assets
    verbose : int
        Verbosity level (0-2)
    """
    # setup environment
    os.environ["TUBER_SERVER"] = "1"

    # import runtime
    if os.getenv("CMAKE_TEST"):
        from _tuber_runtime import run_server
    else:
        from ._tuber_runtime import run_server

    # prepare handler
    handler = RequestHandler(registry, json_module)

    # run
    run_server(
        handler,
        port=port,
        webroot=webroot,
        max_age=max_age,
        verbose=verbose,
    )


def main():
    """
    Server entry point
    """

    import argparse as ap
    import os

    P = ap.ArgumentParser(description="Tuber server")
    P.add_argument(
        "-r",
        "--registry",
        default="/usr/share/tuberd/registry.py",
        help="Location of registry Python code",
    )
    P.add_argument(
        "-j",
        "--json",
        default="json",
        dest="json_module",
        help="Python JSON module to use for serialization/deserialization",
    )
    P.add_argument("-p", "--port", default=80, type=int, help="Port")
    P.add_argument("-w", "--webroot", default="/var/www/", help="Location to serve static content")
    P.add_argument(
        "-a",
        "--max-age",
        default=3600,
        type=int,
        help="Maximum cache residency for static (file) assets",
    )
    P.add_argument("-v", "--verbose", type=int, default=0)
    args = P.parse_args()

    # setup environment
    os.environ["TUBER_SERVER"] = "1"

    code = compile(open(args.registry, "r").read(), args.registry, "exec")
    exec(code, globals())
    args.registry = registry

    run(**vars(args))


if __name__ == "__main__":
    main()
