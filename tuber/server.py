import inspect
import os
import warnings
from .codecs import Codecs

__all__ = ["run", "main"]


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


def describe(registry, request):
    """
    Tuber slow path

    This is invoked with a "request" object that does _not_ contain "object"
    and "method" keys, which would indicate a RPC operation.

    Instead, we are requesting one of the following:

    - A registry descriptor (no "object" or "method" or "property")
    - An object descriptor ("object" but no "method" or "property")
    - A method descriptor ("object" and a "property" corresponding to a method)
    - A property descriptor ("object" and a "property" that is static data)

    Since these are all cached on the client side, we are more concerned about
    correctness and robustness than performance here.
    """

    objname = request["object"] if "object" in request else None
    methodname = request["method"] if "method" in request else None
    propertyname = request["property"] if "property" in request else None

    if not objname and not methodname and not propertyname:
        # registry metadata
        return result_response(objects=list(registry))

    try:
        obj = registry[objname]
    except KeyError:
        return error_response(f"Request for an object ({objname}) that wasn't in the registry!")

    if not methodname and not propertyname:
        # Object metadata.
        methods = []
        properties = []
        clsname = obj.__class__.__name__

        for c in dir(obj):
            # Don't export dunder methods or attributes - this avoids exporting
            # Python internals on the server side to any client.
            if c.startswith("__") or c.startswith(f"_{clsname}__"):
                continue

            if callable(getattr(obj, c)):
                methods.append(c)
            else:
                properties.append(c)

        return result_response(__doc__=inspect.getdoc(obj), methods=methods, properties=properties)

    if propertyname:
        # Sanity check
        if not hasattr(obj, propertyname):
            return error_response(f"{propertyname} is not a method or property of object {objname}")

        # Returning a method description or property evaluation
        attr = getattr(obj, propertyname)

        # Simple case: just a property evaluation
        if not callable(attr):
            return result_response(attr)

        # Complex case: return a description of a method
        doc = inspect.getdoc(attr)
        sig = None
        try:
            sig = str(inspect.signature(attr))
        except:
            # pybind docstrings include a signature as the first line
            if doc and doc.startswith(attr.__name__ + "("):
                if "\n" in doc:
                    sig, doc = doc.split("\n", 1)
                    doc = doc.strip()
                else:
                    sig = doc
                    doc = None
                sig = "(" + sig.split("(", 1)[1]

        return result_response(__doc__=doc, __signature__=sig)

    return error_response(f"Invalid request (object={objname}, method={methodname}, property={propertyname})")


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
        methodname = request["method"]

        try:
            obj = registry[objname]
        except KeyError:
            raise AttributeError(f"Object '{objname}' not found in registry.")

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

    def __init__(self, registry, json_module="json", default_format="application/json", validate=False):
        """
        Arguments
        ---------
        registry : dict
            Dictionary of user-defined objects with properties and methods.
        json_module : str
            Python package to use for encoding and decoding JSON requests.
        default_format : str
            Default encoding format to assume for requests and responses.
        validate : bool
            If True, validate incoming and outgoing packets with jsonschema.
        """
        # ensure registry is a dictionary
        assert isinstance(registry, dict), "Invalid registry"
        self.registry = registry

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
        self._validate = validate

    def validate(self, data, schema_type):
        """
        Validate data packet using jsonschema.

        schema_type must be a valid attribute of the tuber.schema module.
        """
        if not self._validate:
            return

        import jsonschema
        from . import schema

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            jsonschema.validate(data, getattr(schema, schema_type))

    def encode(self, data, fmt=None):
        """
        Encode the input data using the requested format.

        Returns the response format and the encoded data.
        """
        if fmt is None:
            fmt = self.default_format
        try:
            self.validate(data, "response")
        except Exception as e:
            data = error_response(e)
        return fmt, self.codecs[fmt].encode(data)

    def decode(self, data, fmt=None):
        """
        Decode the input data using the requested format.

        Returns the decoded data.
        """
        if fmt is None:
            fmt = self.default_format
        data = self.codecs[fmt].decode(data)
        self.validate(data, "request")
        return data

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


def run(registry, json_module="json", port=80, webroot="/var/www/", max_age=3600, validate=False, verbose=0):
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
    validate : bool
        If True, validate incoming and outgoing data packets using jsonschema
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
    handler = RequestHandler(registry, json_module, validate=validate)

    # run
    run_server(
        handler,
        port=port,
        webroot=webroot,
        max_age=max_age,
        verbose=verbose,
    )


def load_registry(filename):
    """
    Load a user registry from a user provided python file
    """

    import importlib.util

    modname = os.path.splitext(os.path.basename(filename))[0]
    spec = importlib.util.spec_from_file_location(modname, filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    return mod.registry


def main(registry=None):
    """
    Server entry point.

    If supplied, run the server with the given registry.  Otherwise, use the
    ``--registry`` command-line argument to provide a path to a registry file.
    """

    import argparse as ap

    P = ap.ArgumentParser(description="Tuber server")
    if registry is None:
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
    P.add_argument(
        "--validate", action="store_true", help="Validate incoming and outgoing data packets using jsonschema"
    )
    P.add_argument("-v", "--verbose", type=int, default=0)
    args = P.parse_args()

    # setup environment
    os.environ["TUBER_SERVER"] = "1"

    # load registry
    args.registry = registry if registry else load_registry(args.registry)

    run(**vars(args))


if __name__ == "__main__":
    main()
