#!/usr/bin/env pytest-3

import pytest
import subprocess
import requests
from requests.packages.urllib3.util.retry import Retry

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


registry = {
    "NullObject": NullObject(),
    "ObjectWithMethod": ObjectWithMethod(),
    "ObjectWithProperty": ObjectWithProperty(),
    "Types": Types(),
}


@pytest.fixture(scope="session")
def tuberd():
    """Spawn (and kill) a tuberd"""

    s = subprocess.Popen(
        [
            "./tuberd",
            f"-p{TUBERD_PORT}",
            f"--preamble=py/preamble.py",
            f"--registry=tests/test.py",
        ]
    )
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
        return session.post(f"http://localhost:{TUBERD_PORT}/tuber", json=kwargs if json is None else json).json()

    yield tuber_call


#
# Sanity Checks
#


def test_empty_request_array(tuber_call):
    assert tuber_call(json=[]) == []


def test_fetch_null_metadata(tuber_call):
    assert tuber_call(object="NullObject") == {"result": {"__doc__": None, "methods": [], "properties": []}}


def test_call_nonexistent_object(tuber_call):
    assert tuber_call(object="NothingHere") == {
        "error": {"message": "KeyError: ('NothingHere',)\n\nAt:\n  py/preamble.py(24): describe\n"}
    }


def test_call_nonexistent_method(tuber_call):
    assert tuber_call(object="NullObject", method="does_not_exist") == {
        "error": {"message": "AttributeError: 'NullObject' object has no attribute 'does_not_exist'"}
    }

def test_property_types(tuber_call):
    assert tuber_call(object="Types", property="STRING") == dict(result=Types.STRING)
    assert tuber_call(object="Types", property="INTEGER") == dict(result=Types.INTEGER)
    assert tuber_call(object="Types", property="FLOAT") == dict(result=pytest.approx(Types.FLOAT))
    assert tuber_call(object="Types", property="LIST") == dict(result=Types.LIST)
    assert tuber_call(object="Types", property="DICT") == dict(result=Types.DICT)


def test_function_types_with_default_arguments(tuber_call):
    assert tuber_call(object="Types", method="string_function") == dict(result=Types.STRING)
    assert tuber_call(object="Types", method="integer_function") == dict(result=Types.INTEGER)
    assert tuber_call(object="Types", method="float_function") == dict(result=pytest.approx(Types.FLOAT))
    assert tuber_call(object="Types", method="list_function") == dict(result=Types.LIST)
    assert tuber_call(object="Types", method="dict_function") == dict(result=Types.DICT)


def test_function_types_with_correct_argument_types(tuber_call):
    assert tuber_call(object="Types", method="string_function", args=["this is a string"]) == dict(result="this is a string")
    assert tuber_call(object="Types", method="integer_function", args=[6789]) == dict(result=6789)
    assert tuber_call(object="Types", method="float_function", args=[67.89]) == dict(result=pytest.approx(67.89))
    assert tuber_call(object="Types", method="list_function", args=[[3, 4, 5, 6]]) == dict(result=[3, 4, 5, 6])
    assert tuber_call(object="Types", method="dict_function", args=[dict(one="two", three="four")]) == dict(result=dict(one="two", three="four"))
