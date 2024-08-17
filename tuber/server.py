import inspect

from .codecs import Codecs


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
