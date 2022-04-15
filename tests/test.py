#!/usr/bin/env pytest

import pytest
import subprocess
import requests
from requests.packages.urllib3.util.retry import Retry

import numpy as np

TUBERD_PORT = 8080

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


class ObjectWithProperty:
    PROPERTY = "expected property value"


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


registry = {
    "NullObject": NullObject(),
    "ObjectWithMethod": ObjectWithMethod(),
    "ObjectWithProperty": ObjectWithProperty(),
    "Types": Types(),
    "NumPy": NumPy(),
}


@pytest.fixture(scope="session")
def tuberd(pytestconfig):
    """Spawn (and kill) a tuberd"""

    argv = [
        "./tuberd",
        f"-p{TUBERD_PORT}",
        f"--preamble=py/preamble.py",
        f"--registry=tests/test.py",
    ]

    argv.extend(pytestconfig.getoption("tuberd_option"))

    if pytestconfig.getoption("orjson_with_numpy"):
        argv.append("--orjson-with-numpy")

    s = subprocess.Popen(argv)
    yield s
    s.terminate()


@pytest.fixture(scope="session")
def tuber_call(tuberd):
    # The tuber daemon can take a little while to start (in particular, it
    # sources this script as a registry) - rather than adding a magic sleep to
    # the subprocess command, we teach the client interface to wait patiently.
    adapter = requests.adapters.HTTPAdapter(max_retries=Retry(total=10, backoff_factor=1))
    session = requests.Session()
    session.mount(f"http://localhost:{TUBERD_PORT}", adapter)

    def tuber_call(json=None, **kwargs):
        # The most explicit call style passes POST content via an explicit
        # "json" parameter.  However, for convenience's sake, we also allow
        # kwargs to supply a dict parameter since we often call with dicts and
        # this results in a more readable code style.
        return session.post(
            f"http://localhost:{TUBERD_PORT}/tuber",
            json=kwargs if json is None else json,
        ).json()

    yield tuber_call


#
# Sanity Checks
#

def Succeeded(args=None, **kwargs):
    return dict(result=kwargs or args)

def Failed(**kwargs):
    return dict(error=kwargs)

def test_empty_request_array(tuber_call):
    assert tuber_call(json=[]) == []


def test_fetch_null_metadata(tuber_call):
    assert tuber_call(object="NullObject") == Succeeded(__doc__=None, methods=[], properties=[])


def test_call_nonexistent_object(tuber_call):
    assert tuber_call(object="NothingHere") == Failed(
        message="KeyError: ('NothingHere',)\n\nAt:\n  py/preamble.py(24): describe\n")


def test_call_nonexistent_method(tuber_call):
    assert tuber_call(object="NullObject", method="does_not_exist") == Failed(
        message="AttributeError: 'NullObject' object has no attribute 'does_not_exist'")


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
    assert tuber_call(object="Types", method="string_function", args=["this is a string"]) == Succeeded("this is a string")
    assert tuber_call(object="Types", method="integer_function", args=[6789]) == Succeeded(6789)
    assert tuber_call(object="Types", method="float_function", args=[67.89]) == Succeeded(pytest.approx(67.89))
    assert tuber_call(object="Types", method="list_function", args=[[3, 4, 5, 6]]) == Succeeded([3, 4, 5, 6])
    assert tuber_call(object="Types", method="dict_function", args=[dict(one="two", three="four")]) == Succeeded(one="two", three="four")


#
# orjson / numpy fastpath tests
#


@pytest.mark.numpy
def test_numpy_types(tuber_call):
    assert tuber_call(object="NumPy", method="returns_numpy_array") == dict(result=[0, 1, 2, 3])
