import inspect

def describe(registry, request):
    '''
    Tuber slow path
    
    This is invoked with a "request" object that does _not_ contain "object"
    and "method" keys, which would indicate a RPC operation.

    Instead, we are requesting one of the following:

    - An object descriptor ("object" but no "method" or "property")
    - A method descriptor ("object" and a "property" corresponding to a method)
    - A property descriptor ("object" and a "property" that is static data)

    Since these are all cached on the client side, we are more concerned about
    correctness and robustness than performance here.
    '''

    objname = request['object'] if 'object' in request else None
    methodname = request['method'] if 'method' in request else None
    propertyname = request['property'] if 'property' in request else None

    obj = registry[objname]

    if not methodname and not propertyname:
        # Object metadata.
        methods = []
        properties = []

        for c in dir(obj):
            if c[0] == '_':
                continue

            if callable(getattr(obj, c)):
                methods.append(c)
            else:
                properties.append(c)

        return {
            'result': {
                '__doc__': inspect.getdoc(obj),
                'methods': methods,
                'properties': properties,
            }
        }

    if propertyname and hasattr(obj, propertyname):
        # Returning a method description or property evaluation
        attr = getattr(obj, propertyname)

        # Simple case: just a property evaluation
        if not callable(attr):
            return {'result': attr}

        # Complex case: return a description of a method
        doc = inspect.getdoc(attr)
        sig = str(inspect.signature(attr))

        return {
            'result': {
                '__doc__': f'{objname}.{propertyname}{sig}\n\n{doc}'
            }
        }
