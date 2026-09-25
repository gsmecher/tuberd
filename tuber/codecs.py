from collections.abc import Sequence, Mapping
from collections import namedtuple
import io
import sys
import types
import json

try:
    import numpy

    have_numpy = True
except ImportError:
    have_numpy = False

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


def json_default(obj):
    """
    Fall-back hook for objects the JSON encoders cannot serialize natively.

    JSON cannot (natively) encode bytes, so we provide a simple encoding for them.
    This allows uniformity when using either JSON or binary formats (CBOR, etc.)
    which do have native binary support. The JSON encoding is not meant to be
    especially efficient, since anyone wanting seriously move around significant
    amounts of binary data should use another format, but it provides a
    consistent, readable/debuggable, fall-back.

    Any other unsupported object is rejected here.  This hook is only called for
    objects the encoder cannot serialize natively, and must either return a
    substitute or raise; returning the object unchanged makes the encoder recurse
    on it, reporting an unhelpful "Circular reference detected" instead.
    """
    if isinstance(obj, bytes):
        data = [int(v) for v in obj]
        return {"bytes": data}

    # This message is handed back to the client, so keep the detail bounded, and
    # tolerate objects whose repr() raises.
    try:
        detail = repr(obj)
    except Exception:
        detail = "<unrepresentable>"
    if len(detail) > 80:
        detail = detail[:77] + "..."

    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable: {detail}")


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
Codec = namedtuple("Codec", ["decode", "encode", "join_encoded"])


def decode_json(response_data, **kwargs):
    return json.loads(response_data, **kwargs)


def encode_json(obj, **kwargs):
    return json.dumps(obj, default=json_default, **kwargs)


def join_encoded_json(encoded_items):
    """Assemble a JSON array from individually encoded item strings.

    The separator matches the default json.dumps() item separator, so the
    result is byte-identical to encoding the list in one pass.
    """
    return "[" + ", ".join(encoded_items) + "]"


Codecs["json"] = Codec(decode=decode_json, encode=encode_json, join_encoded=join_encoded_json)

if have_orjson:
    # If using orjson with NumPy, overload dumps with the right magic

    def decode_orjson(response_data, **kwargs):
        return orjson.loads(response_data, **kwargs)

    def encode_orjson(obj, **kwargs):
        if have_numpy:
            kwargs["option"] = kwargs.get("option", 0) | orjson.OPT_SERIALIZE_NUMPY
        return orjson.dumps(obj, default=json_default, **kwargs)

    def join_encoded_orjson(encoded_items):
        """Assemble a JSON array from individually encoded item byte strings."""
        return b"[" + b",".join(encoded_items) + b"]"

    Codecs["orjson"] = Codec(decode=decode_orjson, encode=encode_orjson, join_encoded=join_encoded_orjson)


def decode_json_client(response_data, encoding, convert=True):
    if encoding is None:  # guess the typical default if unspecified
        encoding = "utf-8"

    def ohook(obj):
        if isinstance(obj, Mapping) and "bytes" in obj and (len(obj) == 1 or (len(obj) == 2 and "subtype" in obj)):
            try:
                return bytes(obj["bytes"])
            except ValueError as e:
                pass
        return TuberResult(**obj) if convert else obj

    return decode_json(response_data.decode(encoding), object_hook=ohook)


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

    def join_encoded_cbor(encoded_items):
        """Assemble a CBOR array from individually encoded item byte strings.

        Uses CBOREncoder to write the definite-length array header, then appends each
        pre-encoded item's bytes directly. This is valid CBOR: a definite-length array
        header followed by N complete CBOR data items.
        """
        buf = io.BytesIO()
        enc = cbor2.CBOREncoder(buf)
        enc.encode_length(4, len(encoded_items))  # CBOR major type 4 = array
        for item_bytes in encoded_items:
            enc.write(item_bytes)
        return buf.getvalue()

    Codecs["cbor"] = Codec(decode=decode_cbor, encode=encode_cbor, join_encoded=join_encoded_cbor)

    def decode_cbor_client(response_data, encoding, convert=True):
        if not convert:
            return decode_cbor(response_data)
        return decode_cbor(response_data, object_hook=_obj_hook_convert)

    AcceptTypes["application/cbor"] = decode_cbor_client
