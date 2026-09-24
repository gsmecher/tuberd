from collections.abc import Sequence, Mapping
import json
import sys
import types

try:
    import numpy

    have_numpy = True
except ImportError:
    have_numpy = False

try:
    import simplejson

    have_simplejson = True
except ImportError:
    have_simplejson = False

try:
    import orjson

    have_orjson = True
except ImportError:
    have_orjson = False

try:
    import cbor2
    import importlib.metadata

    have_cbor = True
    _cbor2_version = tuple(int(p) for p in importlib.metadata.version("cbor2").split(".")[:2])
except ImportError:
    have_cbor = False
    _cbor2_version = (0, 0)


class TuberResult(types.SimpleNamespace):
    pass


def wrap_bytes_for_json(obj):
    """
    JSON cannot (natively) encode bytes, so we provide a simple encoding for them.
    This allows uniformity when using either JSON or binary formats (CBOR, etc.)
    which do have native binary support. The JSON encoding is not meant to be
    especially efficient, since anyone wanting seriously move around significant
    amounts of binary data should use another format, but it provides a
    consistent, readable/debuggable, fall-back.
    """
    if isinstance(obj, bytes):
        data = [int(v) for v in obj]
        return {"bytes": data}
    return obj


def cbor_encode_ndarray(enc, arr):
    # At the moment, this handles only contiguous arrays of data types which can be represented
    # as CBOR typed arrays, as these can be handled with a singleblock copy of the underlying data,
    # with no per-element handling.

    # start with big endian tags, and then patch up later if the data turn out to be little endian
    type_tags = {
        "u": {
            1: 64,
            2: 65,
            4: 66,
            8: 67,
        },
        "i": {
            1: 72,
            2: 73,
            4: 74,
            8: 75,
        },
        "f": {
            2: 80,
            4: 81,
            8: 82,
            16: 83,
        },
    }
    if arr.dtype.kind not in type_tags or arr.dtype.itemsize not in type_tags[arr.dtype.kind]:
        raise cbor2.CBOREncodeTypeError(
            f"Serialization of numpy arrays with element type {arr.dtype} is not implemented"
        )
    type_tag = type_tags[arr.dtype.kind][arr.dtype.itemsize]
    # add 4 to type tag if little endian if sizeof(type) > 1
    if arr.dtype.itemsize > 1 and (
        arr.dtype.byteorder == "<" or (arr.dtype.byteorder == "=" and sys.byteorder == "little")
    ):
        type_tag += 4

    if arr.flags.c_contiguous:
        md_tag = 40  # row-major
    elif arr.flags.f_contiguous:
        md_tag = 1040  # column-major
    else:
        raise cbor2.CBOREncodeTypeError("Serialization of non-contiguous numpy arrays is not implemented")

    enc.encode_length(6, md_tag)  # multi-dimensional array header, a tag (type 6) of the correct type
    enc.encode_length(4, 2)  # payload of the m-d array is always an array (type 4) of length 2
    enc.encode_length(4, len(arr.shape))  # the first item in the outer array is the array of extents
    for extent in arr.shape:
        enc.encode_int(extent)
    # the second item in the outer array is the array entries, for which we use a typed array
    enc.encode_length(6, type_tag)
    # the typed array payload is a bytestring (type 2)
    enc.encode_length(2, arr.nbytes)
    enc.write(arr.tobytes())


def cbor_augment_encode(enc, obj):
    if isinstance(obj, numpy.ndarray):
        cbor_encode_ndarray(enc, obj)
        return
    raise cbor2.CBOREncodeTypeError(f"Unsupported object for CBOR encoding {type(obj)}")


# cbor2 6.0 made several breaking changes:
#   - dropped CBORDecodeValueError (use the parent CBORDecodeError instead)
#   - changed tag_hook from (decoder, tag) to (tag, immutable)
#   - changed object_hook from (decoder, dict) to (dict, immutable)
#   - enc.fp is None during dumps() (use enc.write() instead, which exists in both)
_CBOR2_V6 = have_cbor and _cbor2_version >= (6, 0)
_CBORDecodeValueError = cbor2.CBORDecodeError if _CBOR2_V6 else cbor2.CBORDecodeValueError if have_cbor else Exception


def cbor_tag_decode(tag):
    if have_numpy and tag.tag >= 64 and tag.tag <= 87 and tag.tag != 76:  # Typed arrays
        is_float = tag.tag & 0x10
        is_signed = tag.tag & 0x8
        is_le = tag.tag & 0x4
        ll = tag.tag & 0x3
        element_size = 1 << ll
        if is_float:  # floats are one power of two larger
            element_size <<= 1
        # due to the cap of 87 on the tag, we will never see invalid 'signed' float combinations
        dt = numpy.dtype(f"{'<' if is_le else '>'}{'f' if is_float else 'i' if is_signed else 'u'}{element_size}")
        if len(tag.value) % element_size != 0:
            raise _CBORDecodeValueError(
                f"Invalid data size ({len(tag.value)}) for typed array with tag {tag.tag}, interpreted as {dt}"
            )
        # create a 1-D, row-major array to contain all of the data, which can have more detailed
        # shape and ordering information applied later
        arr = numpy.zeros(len(tag.value) // element_size, dtype=dt, order="C")
        # splat the data into the array's memory
        arr.data.cast("B")[:] = tag.value
        return arr
    if have_numpy and (tag.tag == 40 or tag.tag == 1040):
        if not isinstance(tag.value, Sequence):
            raise _CBORDecodeValueError(f"Invalid raw data for multi-dimensional array tag ({tag.tag})")
        if len(tag.value) != 2:
            raise _CBORDecodeValueError(f"Invalid raw array length for multi-dimensional array tag ({tag.tag})")
        if not isinstance(tag.value[0], Sequence) or not isinstance(tag.value[1], numpy.ndarray):
            raise _CBORDecodeValueError(f"Invalid raw data for multi-dimensional array tag ({tag.tag})")
        arr = tag.value[1].reshape(tag.value[0], order="C" if tag.tag == 40 else "F")
        return arr
    return None


# This variable is used to track the media types we are able to decode, mapping their names to
# decoding functions. The interface of the decoding function is to take two arguments: a bytes-like
# object containing the encoded data, and an encoding name (which may be None) given by the
# character set information (if any) included in the Content-Type header attached to the data.
AcceptTypes = {}

# This variable is used to track the codecs enabled on the server, mapping their names to
# decoding and encoding functions.  The interface for each should match that of json.loads() and
# json.dumps(), respectively.
Codecs = {}


class Codec:
    """
    A decode/encode function pair, with default keyword options bound to each.

    The two callables follow the interfaces of ``json.loads()`` and ``json.dumps()``.
    Bound options are supplied to every call; keywords given at the call site take
    precedence over them.
    """

    def __init__(self, decode, encode, decode_options=None, encode_options=None):
        self._decode = decode
        self._encode = encode
        self.decode_options = dict(decode_options or {})
        self.encode_options = dict(encode_options or {})

    def decode(self, data, **kwargs):
        return self._decode(data, **{**self.decode_options, **kwargs})

    def encode(self, obj, **kwargs):
        return self._encode(obj, **{**self.encode_options, **kwargs})

    def with_options(self, decode=None, encode=None):
        """
        Return a copy of this codec with additional default options bound to it.

        Arguments
        ---------
        decode : dict
            Keyword options to supply to the decode function.
        encode : dict
            Keyword options to supply to the encode function.
        """
        return Codec(
            self._decode,
            self._encode,
            {**self.decode_options, **(decode or {})},
            {**self.encode_options, **(encode or {})},
        )


def parse_codec_options(options):
    """
    Parse a sequence of ``KEY=VALUE`` strings into a codec options dictionary.

    Keys may be prefixed with ``encode:`` or ``decode:`` to bind the option in a
    single direction; unprefixed options are bound to both.  Values are parsed as
    JSON where possible and left as plain strings otherwise, so that
    ``allow_nan=true`` yields ``True`` and ``indent=2`` yields ``2``.

    Returns a dictionary suitable for passing to ``Codec.with_options()``.
    """
    parsed = {"decode": {}, "encode": {}}

    for option in options:
        key, sep, value = option.partition("=")
        if not sep:
            raise ValueError(f"Invalid codec option {option!r}, expected KEY=VALUE")

        target, tsep, name = key.partition(":")
        if tsep:
            if target not in parsed:
                raise ValueError(f"Invalid codec option target {target!r} in {option!r}")
            targets = [target]
        else:
            name, targets = key, list(parsed)

        try:
            value = json.loads(value)
        except ValueError:
            pass

        for target in targets:
            parsed[target][name] = value

    return parsed


def decode_json(response_data, **kwargs):
    return json.loads(response_data, **kwargs)


def encode_json(obj, **kwargs):
    return json.dumps(obj, default=wrap_bytes_for_json, **kwargs)


Codecs["json"] = Codec(decode=decode_json, encode=encode_json)


if have_simplejson:
    # Unlike the standard library, simplejson defaults to allow_nan=False for both
    # loads() and dumps(), so non-finite floats raise unless the caller binds
    # allow_nan=True - e.g. Codecs["simplejson"].with_options(...), or the
    # --json-option command line argument.

    def decode_simplejson(response_data, **kwargs):
        return simplejson.loads(response_data, **kwargs)

    def encode_simplejson(obj, **kwargs):
        return simplejson.dumps(obj, default=wrap_bytes_for_json, **kwargs)

    Codecs["simplejson"] = Codec(
        decode=decode_simplejson,
        encode=encode_simplejson,
        encode_options={"encoding": None},
    )

if have_orjson:
    # If using orjson with NumPy, overload dumps with the right magic

    def decode_orjson(response_data, **kwargs):
        return orjson.loads(response_data, **kwargs)

    def encode_orjson(obj, **kwargs):
        if have_numpy:
            kwargs["option"] = kwargs.get("option", 0) | orjson.OPT_SERIALIZE_NUMPY
        return orjson.dumps(obj, default=wrap_bytes_for_json, **kwargs)

    Codecs["orjson"] = Codec(decode=decode_orjson, encode=encode_orjson)


def make_json_decoder(codec):
    """
    Build a client-side decode function for JSON responses from the given codec.
    """

    def decode_json_client(response_data, encoding, convert=True):
        if encoding is None:  # guess the typical default if unspecified
            encoding = "utf-8"

        def ohook(obj):
            if isinstance(obj, Mapping) and "bytes" in obj and (len(obj) == 1 or (len(obj) == 2 and "subtype" in obj)):
                try:
                    return bytes(obj["bytes"])
                except ValueError:
                    pass
            return TuberResult(**obj) if convert else obj

        return codec.decode(response_data.decode(encoding), object_hook=ohook)

    return decode_json_client


decode_json_client = make_json_decoder(Codecs["json"])

AcceptTypes["application/json"] = decode_json_client


# Use cbor2 to handle CBOR, if available
if have_cbor:

    # Adapt the (tag,) -> value tag handler to the version-specific tag_hook signature.
    if _CBOR2_V6:
        _tag_hook = lambda tag, immutable: cbor_tag_decode(tag)
        _obj_hook_convert = lambda data, immutable: TuberResult(**data)
    else:
        _tag_hook = lambda dec, tag: cbor_tag_decode(tag)
        _obj_hook_convert = lambda dec, data: TuberResult(**data)

    def decode_cbor(response_data, **kwargs):
        return cbor2.loads(response_data, tag_hook=_tag_hook, **kwargs)

    def encode_cbor(obj, **kwargs):
        return cbor2.dumps(obj, default=cbor_augment_encode, **kwargs)

    Codecs["cbor"] = Codec(decode=decode_cbor, encode=encode_cbor)

    def decode_cbor_client(response_data, encoding, convert=True):
        if not convert:
            return decode_cbor(response_data)
        return decode_cbor(response_data, object_hook=_obj_hook_convert)

    AcceptTypes["application/cbor"] = decode_cbor_client
