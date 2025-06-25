#!/usr/bin/env -S pytest -svv

import aiohttp
import asyncio
import importlib
import inspect
import numpy as np
import os
import pytest
import warnings
import tuber

if os.getenv("CMAKE_TEST"):
    import test_module as tm
else:
    from tuber.tests import test_module as tm

from tuber.server import TuberContainer, TuberArray


# REGISTRY DEFINITIONS
#
# Tuberd needs a registry to export. Since it's intimately connected with the
# test code, we place them into the same Python file. This also allows us to
# verify that network-exported Python code works the same when run locally.
class NullObject:
    pass


class ObjectWithMethod:
    def method(self):
        return "expected return value"


class ObjectWithDictMethod:
    def method(self):
        return {"a": "expected return value", "b": "expected return value"}


class ObjectWithProperty:
    PROPERTY = "expected property value"


class ObjectWithPrivateMethod:
    def __private_method(self):
        raise RuntimeError("how did you get here?")


class ObjectWithContainerProperties:
    property_objects = TuberArray([ObjectWithProperty(), ObjectWithProperty()])
    method_objects = TuberArray({"a": ObjectWithMethod(), "b": ObjectWithMethod()})


class Types:
    # These properties can be accessed as properties, directly
    STRING = "this is a string property"
    INTEGER = 1234
    FLOAT = 0.1234
    LIST = [1, 2, 3, 4]
    DICT = {"1": "2", "3": "4"}

    # Properties are also exposed as default arguments to functions
    def string_function(self, arg=STRING):
        assert isinstance(arg, str)
        return arg

    def integer_function(self, arg=INTEGER):
        assert isinstance(arg, int)
        return arg

    def float_function(self, arg=FLOAT):
        assert isinstance(arg, float)
        return arg

    def list_function(self, arg=LIST):
        assert isinstance(arg, list)
        return arg

    def dict_function(self, arg=DICT):
        assert isinstance(arg, dict)
        return arg


class NumPy:
    def returns_numpy_array(self):
        return np.array([0, 1, 2, 3])


class WarningsClass:
    def single_warning(self, warning_text, error=False):
        warnings.resetwarnings()  # ensure no filters
        warnings.warn(warning_text)

        if error:
            raise RuntimeError("Oops!")

        return True

    def multiple_warnings(self, warning_count=1, error=False):
        warnings.resetwarnings()  # ensure no filters
        for n in range(warning_count):
            warnings.warn(f"Warning {n+1}")

        if error:
            raise RuntimeError("Oops!")

        return True


registry = {
    "NullObject": NullObject(),
    "ObjectWithMethod": ObjectWithMethod(),
    "ObjectWithDictMethod": ObjectWithDictMethod(),
    "ObjectWithProperty": ObjectWithProperty(),
    "ObjectWithPrivateMethod": ObjectWithPrivateMethod(),
    "ObjectWithContainerProperties": ObjectWithContainerProperties(),
    "ObjectList": TuberArray([ObjectWithContainerProperties(), ObjectWithContainerProperties()]),
    "ObjectDict": TuberArray({"a": ObjectWithContainerProperties(), "b": ObjectWithContainerProperties()}),
    "ObjectListList": TuberArray(
        [TuberArray([ObjectWithContainerProperties()]), TuberArray([ObjectWithContainerProperties])]
    ),
    "Container": TuberContainer({"a": ObjectWithProperty(), "b": ObjectWithMethod()}),
    "Types": Types(),
    "NumPy": NumPy(),
    "Warnings": WarningsClass(),
    "Wrapper": tm.Wrapper(),
}


#
# Sanity Checks - Python -> JSON -> C++ -> Python and back again
#


def Succeeded(args=None, warnings=None, **kwargs):
    """Wrap a return value for a successful call in its JSON-RPC wrapper"""
    if warnings is not None:
        return dict(result=kwargs or args, warnings=warnings)

    return dict(result=kwargs or args)


def Failed(warnings=None, **kwargs):
    """Wrap a return value for an error in its JSON-RPC wrapper"""
    if warnings is not None:
        return dict(error=kwargs, warnings=warnings)

    return dict(error=kwargs)


container_success = Succeeded(__doc__=TuberArray.__doc__.strip(), methods=["tuber_call", "tuber_meta"], properties=[])


def test_empty_request_array(tuber_call):
    assert tuber_call(json=[]) == []


def test_describe(tuber_call):
    assert tuber_call(json={}) == Succeeded(objects=list(registry))
    assert tuber_call(object="ObjectWithPrivateMethod") == Succeeded(__doc__=None, methods=[], properties=[])

    assert tuber_call(object="ObjectWithContainerProperties", property="property_objects") == container_success
    assert tuber_call(object=["ObjectWithContainerProperties", "property_objects"]) == container_success
    assert tuber_call(object="ObjectWithContainerProperties", property="method_objects") == container_success
    assert tuber_call(object=["ObjectWithContainerProperties", ("property_objects", 0)]) == Succeeded(
        __doc__=None, methods=[], properties=["PROPERTY"]
    )
    assert tuber_call(object=["ObjectWithContainerProperties", ("method_objects", "a")]) == Succeeded(
        __doc__=None, methods=["method"], properties=[]
    )

    assert tuber_call(object="ObjectList") == container_success
    assert tuber_call(object=[("ObjectListList", 0)]) == container_success
    assert tuber_call(object="ObjectDict") == container_success
    assert tuber_call(object=[("ObjectDict", "a")]) == Succeeded(__doc__=None, methods=[], properties=[])


def test_fetch_null_metadata(tuber_call):
    assert tuber_call(object="NullObject") == Succeeded(__doc__=None, methods=[], properties=[])


def test_call_nonexistent_object(tuber_call):
    assert tuber_call(object="NothingHere") == Failed(
        message="AttributeError: 'TuberRegistry' object has no attribute 'NothingHere' (Invalid object name 'NothingHere')"
    )


def test_call_nonexistent_method(tuber_call):
    assert tuber_call(object="NullObject", method="does_not_exist") == Failed(
        message="AttributeError: 'NullObject' object has no attribute 'does_not_exist'"
    )


def test_property_types(tuber_call):
    assert tuber_call(object="Types", property="STRING") == Succeeded(Types.STRING)
    assert tuber_call(object="Types", property="INTEGER") == Succeeded(Types.INTEGER)
    assert tuber_call(object="Types", property="FLOAT") == Succeeded(pytest.approx(Types.FLOAT))
    assert tuber_call(object="Types", property="LIST") == Succeeded(Types.LIST)
    assert tuber_call(object="Types", property="DICT") == Succeeded(Types.DICT)


def test_function_types_with_default_arguments(tuber_call):
    assert tuber_call(object="Types", method="string_function") == Succeeded(Types.STRING)
    assert tuber_call(object="Types", method="integer_function") == Succeeded(Types.INTEGER)
    assert tuber_call(object="Types", method="float_function") == Succeeded(pytest.approx(Types.FLOAT))
    assert tuber_call(object="Types", method="list_function") == Succeeded(Types.LIST)
    assert tuber_call(object="Types", method="dict_function") == Succeeded(Types.DICT)


def test_function_types_with_correct_argument_types(tuber_call):
    assert tuber_call(object="Types", method="string_function", args=["this is a string"]) == Succeeded(
        "this is a string"
    )
    assert tuber_call(object="Types", method="integer_function", args=[6789]) == Succeeded(6789)
    assert tuber_call(object="Types", method="float_function", args=[67.89]) == Succeeded(pytest.approx(67.89))
    assert tuber_call(object="Types", method="list_function", args=[[3, 4, 5, 6]]) == Succeeded([3, 4, 5, 6])
    assert tuber_call(object="Types", method="dict_function", args=[dict(one="two", three="four")]) == Succeeded(
        one="two", three="four"
    )


def test_container_properties(tuber_call):
    assert tuber_call(
        object=["ObjectWithContainerProperties", ("property_objects", 0)], property="PROPERTY"
    ) == Succeeded("expected property value")
    assert tuber_call(object=["ObjectWithContainerProperties", ("method_objects", "a")], method="method") == Succeeded(
        "expected return value"
    )
    assert tuber_call(object=[("ObjectList", 0), ("method_objects", "a")], method="method") == Succeeded(
        "expected return value"
    )
    assert tuber_call(object=[("ObjectDict", "a"), ("property_objects", 0)], property="PROPERTY") == Succeeded(
        "expected property value"
    )
    assert tuber_call(object=[("ObjectListList", 1, 0), ("method_objects", "a")], method="method") == Succeeded(
        "expected return value"
    )


#
# orjson / numpy fastpath tests
#


@pytest.mark.orjson
def test_numpy_types(tuber_call):
    result = tuber_call(object="NumPy", method="returns_numpy_array")
    # Attempting to compare the whole result object to its expected value does not work well if a
    # numpy array is involved, becauase comparisons on the array will produce array results, and
    # numpy insists that "The truth value of an array with more than one element is ambiguous"
    # (even if all values in that array are the same), so we must use .all() to force a scalar
    # truth value.
    assert isinstance(result, dict)
    assert len(result) == 1
    assert "result" in result
    assert (np.array([0, 1, 2, 3]) == result["result"]).all()

    #
    # pybind11 wrappers
    #

    assert tuber_call(object="Types", method="string_function", args=["this is a string"]) == Succeeded(
        "this is a string"
    )


@pytest.mark.orjson
def test_double_vector(tuber_call):
    assert tuber_call(object="Wrapper", method="increment", args=[[1, 2, 3, 4, 5]]) == Succeeded([2, 3, 4, 5, 6])


def test_unserializable(tuber_call):
    # Errors differ between orjson, standard json, and CBOR
    message = tuber_call(object="Wrapper", method="unserializable")["error"]["message"]
    assert (
        message.startswith("ValueError:")
        or message.startswith("CBOREncodeTypeError:")
        or message.startswith("TypeError: default serializer")
        or message.startswith("CBOREncodeTypeError: cannot serialize")
    )


#
# pybind11 strenum tests. These tests are direct library imports and do not
# exercise tuberd.
#


def test_cpp_enum_direct_instantiation():
    # Directly instantiate enums
    x = tm.Kind("X")
    y = tm.Kind("Y")
    assert x != y

    # Compare two instiantiations
    assert x == tm.Kind("X")
    assert y == tm.Kind("Y")


def test_cpp_enum_cpp_to_py():
    w = tm.Wrapper()
    x = w.return_x()
    y = w.return_y()

    assert x == tm.Kind("X")
    assert y == tm.Kind("Y")


def test_cpp_enum_py_to_cpp_types():
    w = tm.Wrapper()
    x = tm.Kind("X")
    y = tm.Kind("Y")

    assert w.is_x(x)
    assert w.is_y(y)
    assert not w.is_x(y)


def test_cpp_enum_py_to_cpp_strings():
    w = tm.Wrapper()

    assert w.is_x("X")
    assert w.is_y("Y")
    assert not w.is_x("Y")


@pytest.mark.skip(reason="Semantics are unclear")
def test_cpp_enum_py_to_py():
    x = tm.Kind("X")
    y = tm.Kind("Y")

    assert x == "X"
    assert y == "Y"
    assert y != "X"


@pytest.mark.orjson
def test_cpp_enum_orjson_serialize():
    orjson = pytest.importorskip("orjson")

    x = tm.Kind("X")
    y = tm.Kind("Y")

    assert orjson.dumps(x) == b'"X"'
    assert orjson.dumps(y) == b'"Y"'


#
# tuber.py tests
#

ACCEPT_TYPES = {
    "json": [
        "application/json",
    ],
    "cbor": [
        "application/cbor",
    ],
    "both": [
        "application/json",
        "application/cbor",
    ],
}


@pytest.fixture(scope="module", params=list(ACCEPT_TYPES))
def accept_types(request):
    return ACCEPT_TYPES[request.param]


@pytest.fixture(scope="module", params=["simple", "async"])
def resolve(request, tuberd_host, accept_types):
    async def resolver(objname=None, convert_json=None):
        if request.param == "simple":
            return tuber.resolve_simple(tuberd_host, objname, accept_types, convert_json=convert_json)
        else:
            return await tuber.resolve(tuberd_host, objname, accept_types, convert_json=convert_json)

    return resolver


class AsyncSimpleContext:
    def __init__(self, ctx):
        self.ctx = ctx

    async def __call__(self, **kwargs):
        return self.ctx(**kwargs)

    def __getattr__(self, name):
        return getattr(self.ctx, name)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args, **kwargs):
        self.ctx()


def tuber_context(obj, **kwargs):
    ctx = obj.tuber_context(**kwargs)
    if inspect.iscoroutinefunction(ctx.__call__):
        return ctx
    return AsyncSimpleContext(ctx)


async def tuber_result(res):
    if inspect.isawaitable(res):
        return await res
    try:
        return res.result()
    except AttributeError:
        return res


@pytest.mark.asyncio
async def test_tuberpy_hello(resolve):
    s = await resolve("Wrapper")
    x = await tuber_result(s.increment([1, 2, 3, 4, 5]))
    assert x == [2, 3, 4, 5, 6]


@pytest.mark.asyncio
async def test_tuberpy_dir(resolve):
    """Ensure embedded methods end up in dir() of objects.

    This is a crude proxy for the ability to tab-complete."""
    s = await resolve("Wrapper")
    assert "increment" in dir(s)


@pytest.mark.asyncio
async def test_tuberpy_module_docstrings(resolve):
    """Ensure docstrings in C++ methods end up in the TuberObject's __doc__ dunder."""

    s = await resolve("Wrapper")
    assert s.__doc__.strip() == tm.Wrapper.__doc__.strip()


@pytest.mark.asyncio
async def test_tuberpy_method_docstrings(resolve):
    """Ensure docstrings in C++ methods end up in the TuberObject's __doc__ dunder."""

    s = await resolve("Wrapper")
    assert s.increment.__doc__.strip() == tm.Wrapper.increment.__doc__.split("\n", 1)[-1].strip()

    # check signature
    sig = inspect.signature(s.increment)
    assert "x" in sig.parameters


@pytest.mark.asyncio
async def test_tuberpy_session_cache(accept_types, tuberd_host):
    """Ensure we don't create a new ClientSession with every call."""
    s = await tuber.resolve(tuberd_host, "Wrapper", accept_types)
    await s.increment([1, 2, 3])
    aiohttp.ClientSession = None  # break ClientSession instantiation
    await s.increment([4, 5, 6])
    importlib.reload(aiohttp)
    # ensure we fixed it.
    assert aiohttp.ClientSession  # type: ignore[truthy-function]


@pytest.mark.asyncio
async def test_tuberpy_async_context(resolve):
    """Ensure we can use tuber_contexts to batch calls."""
    s = await resolve("Wrapper")

    async with tuber_context(s) as ctx:
        r1 = ctx.increment([1, 2, 3])
        r2 = ctx.increment([2, 3, 4])

    r1, r2 = await asyncio.gather(*map(tuber_result, [r1, r2]))

    assert r1 == [2, 3, 4]
    assert r2 == [3, 4, 5]


@pytest.mark.asyncio
async def test_tuberpy_async_context_with_kwargs(resolve):
    """Ensure we can use tuber_contexts to batch calls."""
    s = await resolve("Wrapper")

    async with tuber_context(s, x=[1, 2, 3]) as ctx:
        r1 = ctx.increment()
        r2 = ctx.increment()

    r1, r2 = await asyncio.gather(*map(tuber_result, [r1, r2]))

    assert r1 == [2, 3, 4]
    assert r2 == [2, 3, 4]


@pytest.mark.asyncio
async def test_tuberpy_async_context_with_exception(resolve):
    """Ensure exceptions in a sequence of calls show up as expected."""
    s = await resolve("Wrapper")

    with pytest.raises(tuber.TuberRemoteError):
        async with tuber_context(s) as ctx:
            r1 = ctx.increment([1, 2, 3])  # fine
            r2 = ctx.increment(4)  # wrong type
            r3 = ctx.increment([5, 6, 6])  # shouldn't execute

        # execution happens when ctx falls out of scope - exception raised

    # the first call should have succeeded
    await tuber_result(r1)

    # the second call generated the exception
    with pytest.raises(tuber.TuberRemoteError):
        await tuber_result(r2)

    # the third call should not have been executed (propagated here as an
    # exception too)
    with pytest.raises(tuber.TuberRemoteError):
        await tuber_result(r3)


@pytest.mark.asyncio
async def test_tuberpy_unserializable(resolve):
    """Ensure unserializable objects return an error."""
    s = await resolve("Wrapper")
    with pytest.raises(tuber.TuberRemoteError):
        r = await tuber_result(s.unserializable())


@pytest.mark.asyncio
async def test_tuberpy_serialize_enum_class(resolve):
    """Return an enum class, which must be converted in pybind11 to something serializable."""
    s = await resolve("Wrapper")

    # Retrieve a Kind::X value
    r = await tuber_result(s.return_x())

    # Make sure it's serialized to a string as expected
    assert r == "X"

    # Ensure we can round-trip it back into C++
    r = await tuber_result(s.is_x(r))
    assert r is True


@pytest.mark.xfail
@pytest.mark.asyncio
async def test_tuberpy_async_context_with_unserializable(resolve):
    """Ensure exceptions in a sequence of calls show up as expected."""
    s = await resolve("Wrapper")

    async with tuber_context(s) as ctx:
        r1 = ctx.increment([1, 2, 3])  # fine
        r2 = ctx.unserializable()
        r3 = ctx.increment([5, 6, 6])  # shouldn't execute

    await tuber_result(r1)

    with pytest.raises(tuber.TuberRemoteError):
        await tuber_result(r2)

    with pytest.raises(tuber.TuberRemoteError):
        await tuber_result(r3)


@pytest.mark.asyncio
async def test_tuberpy_warnings(resolve):
    """Ensure warnings are captured"""
    s = await resolve("Warnings")

    # Single, simple warning
    with pytest.warns(match="This is a warning"):
        r = await tuber_result(s.single_warning("This is a warning"))

    # Several in a row
    with pytest.warns() as ws:
        r = await tuber_result(s.multiple_warnings(warning_count=5))
        assert len(ws) == 5

    # Check with exceptions
    with pytest.raises(tuber.TuberRemoteError), pytest.warns(match="This is a warning"):
        r = await tuber_result(s.single_warning("This is a warning", error=True))


@pytest.mark.asyncio
async def test_tuberpy_resolve_all(resolve):
    """Ensure resolve finds all registry entries"""
    s = await resolve()

    assert set(dir(s)) >= set(registry)
    assert set(dir(s.Types)) >= set(dir(registry["Types"]))


@pytest.mark.asyncio
async def test_tuberpy_registry_context(resolve):
    """Ensure registry entries are accessible from top level context"""

    s = await resolve()

    async with tuber_context(s) as ctx:
        ctx.Wrapper.increment(x=[1, 2, 3])
        ctx.Types.integer_function()
        r1, r2 = await ctx()

        with pytest.raises(tuber.TuberRemoteError):
            ctx.Wrapper.not_a_function()
            await ctx()
        with pytest.raises(tuber.TuberRemoteError):
            ctx.NotAnAttribute.not_a_function()
            await ctx()

    assert r1 == [2, 3, 4]
    assert r2 == Types.INTEGER


def test_tuberpy_fake_async(accept_types, tuberd_host):
    """Ensure async execution works with simple context"""

    s = tuber.resolve_simple(tuberd_host, accept_types=accept_types)

    with s.tuber_context() as ctx:
        ctx.Wrapper.increment(x=[1, 2, 3])
        resp1 = ctx.send()
        ctx.Types.integer_function()
        resp2 = ctx.send()

    r1, r2 = map(lambda resp: ctx.receive(resp)[0], [resp1, resp2])

    assert r1 == [2, 3, 4]
    assert r2 == Types.INTEGER


@pytest.mark.parametrize("return_exceptions", [True, False])
@pytest.mark.asyncio
async def test_tuberpy_return_exceptions(return_exceptions, resolve):
    """Ensure errors are turned into warnings"""
    s = await resolve()

    with pytest.warns(match="This is a warning"):
        async with tuber_context(s) as ctx:
            r1 = ctx.Wrapper.increment([1, 2, 3])  # fine
            r2 = ctx.Warnings.single_warning("This is a warning", error=True)
            r3 = ctx.Wrapper.increment([5, 6, 6])  # should still execute
            if not return_exceptions:
                with pytest.raises(tuber.TuberRemoteError):
                    await ctx()
                with pytest.raises(tuber.TuberRemoteError):
                    await tuber_result(r2)
                with pytest.raises(tuber.TuberRemoteError):
                    await tuber_result(r3)

            else:
                [r1b, r2b, r3b] = await ctx(return_exceptions=True)
                with pytest.raises(tuber.TuberRemoteError):
                    await tuber_result(r2)
                r3 = await tuber_result(r3)

    r1 = await tuber_result(r1)
    assert r1 == [2, 3, 4]
    if return_exceptions:
        assert r1b == [2, 3, 4]
        assert r3 == [6, 7, 7]
        assert r3b == [6, 7, 7]
        assert isinstance(r2b, tuber.TuberRemoteError)


@pytest.mark.asyncio
async def test_tuberpy_containers(resolve):
    """Test dynamic attributes and container access"""
    s = await resolve()

    assert len(s.ObjectList) == 2
    assert len(s.ObjectListList) == 2
    assert len(s.ObjectListList[0]) == 1
    assert len(s.ObjectDict) == 2
    assert list(s.ObjectDict.keys()) == ["a", "b"]
    assert len(s.ObjectWithContainerProperties.property_objects) == 2
    assert list(s.ObjectWithContainerProperties.method_objects.keys()) == ["a", "b"]
    assert s.ObjectWithContainerProperties.property_objects[0].PROPERTY == "expected property value"
    assert s.Container["a"].PROPERTY == "expected property value"

    r1 = await tuber_result(s.ObjectList[0].method_objects["a"].method())
    r2 = await tuber_result(s.ObjectListList[1][0].method_objects["a"].method())
    r3 = await tuber_result(s.ObjectDict["a"].method_objects["a"].method())
    r4 = await tuber_result(s.ObjectWithContainerProperties.method_objects["b"].method())
    r5 = await tuber_result(s.Container["b"].method())

    assert all([x == "expected return value" for x in [r1, r2, r3, r4, r5]])


@pytest.mark.asyncio
async def test_tuberpy_container_context(resolve):
    """Ensure containers work in contexts"""
    s = await resolve()

    async with tuber_context(s) as ctx:
        for idx, obj in enumerate(s.ObjectList):
            for k in obj.method_objects.keys():
                ctx.ObjectList[idx].method_objects[k].method()
        r1 = await ctx()

    assert all([x == "expected return value" for x in r1])


@pytest.mark.asyncio
async def test_tuberpy_container_property_context(resolve):
    """Ensure methods of container objects work in contexts"""
    s = await resolve("ObjectWithContainerProperties")

    r1 = await tuber_result(s.method_objects["a"].method())
    assert r1 == "expected return value"

    assert s.property_objects[0].PROPERTY == "expected property value"

    async with tuber_context(s) as ctx:
        r2 = ctx.method_objects["a"].method()
    r2 = await tuber_result(r2)

    assert r2 == "expected return value"


@pytest.mark.asyncio
async def test_tuberpy_container_properties(resolve):
    """Collect properties and method calls for container objects"""
    s = await resolve()

    pobjs = s.ObjectWithContainerProperties.property_objects
    r1 = pobjs.tuber_get("PROPERTY")
    assert len(r1) == len(pobjs)
    assert all([x == "expected property value" for x in r1])

    mobjs = s.ObjectWithContainerProperties.method_objects
    r2 = await tuber_result(mobjs.tuber_call("method"))

    assert len(r2) == len(mobjs)
    assert all([x == "expected return value" for x in r2])


@pytest.mark.asyncio
async def test_tuberpy_method(resolve):
    """Check method return types"""
    s1 = await resolve(convert_json=True)
    r1 = await tuber_result(s1.ObjectWithDictMethod.method())
    assert r1.a == "expected return value"
    assert r1.b == "expected return value"

    s2 = await resolve(convert_json=False)
    r2 = await tuber_result(s2.ObjectWithDictMethod.method())
    assert r2["a"] == "expected return value"
    assert r2["b"] == "expected return value"
