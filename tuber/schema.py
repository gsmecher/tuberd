"""
JSON Schemas for tuber requests and responses.

These schemas are only verified in deployment when the "--validate" flag is set
on tuberd. This is not ordinarily true in deployment. Hence, jsonquery is a
belt-and-braces way to ensure the server and client code are conformant to a
particular specification. It is not meant to protect against ill-crafted or
malicious requests.
"""

"""
Request schema
"""

request_single = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "args": {"type": "array"},
        "kwargs": {"type": "object"},
        "object": {
            "oneOf": [
                {"type": "null"},
                {"type": "string"},
                {"type": "array"},
            ],
        },
        "property": {"type": "string"},
        "method": {"type": "string"},
        "resolve": {"type": "boolean"},
    },
    "additionalProperties": False,
}

request_array = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "array",
    "items": request_single,
}

request = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "oneOf": [request_single, request_array],
}

"""
Metadata schema (resolved only)
"""

metadata_method = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "method metadata",
    "type": "object",
    "properties": {
        "__doc__": {"oneOf": [{"type": "string"}, {"type": "null"}]},
        "__signature__": {
            "type": "object",
            "properties": {
                "parameters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "annotation": {"type": "string"},
                            "kind": {"type": "number"},
                            "name": {"type": "string"},
                            "default": {"type": ["string", "number", "object", "array", "boolean", "null"]},
                        },
                        "additionalProperties": False,
                    },
                },
                "return_annotation": {"type": "string"},
            },
            "additionalProperties": False,
        },
        # argument descriptor goes here
    },
    "additionalProperties": False,
}

# Description of an object
metadata_object = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "object metadata",
    "type": "object",
    "properties": {
        "__doc__": {"type": ["string", "null"]},
        "properties": {"type": "object"},
        "methods": {
            "type": "object",
            "additionalProperties": metadata_method,
        },
        "keys": {
            "oneOf": [
                {"type": "array"},
                {"type": "integer"},
            ],
        },
        "values": {
            "oneOf": [
                {"type": "array"},
                {"type": "object"},
            ],
        },
        "objects": {
            "oneOf": [
                {"type": "object"},
                {"type": "null"},
            ],
        },
    },
    "additionalProperties": False,
}

# Root metadata, when no object/method/property has been specified
metadata_root = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "root metadata",
    "type": "object",
    "additionalProperties": metadata_object,
}

"""
Response schema
"""

response_warnings = {
    "type": "array",
    "items": {
        "type": "string",
    },
}

response_valid_single = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "result": {
            # any JSON type allowed
        },
        "warnings": response_warnings,
    },
    "required": ["result"],
    "additionalProperties": False,
}

response_error_single = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "error": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
            },
        },
        "warnings": response_warnings,
    },
    "required": ["error"],
    "additionalProperties": False,
}

response_single = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "oneOf": [
        response_valid_single,
        response_error_single,
    ],
}

response_array = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "array",
    "items": response_single,
}

response = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "oneOf": [
        response_single,
        response_array,
    ],
}
