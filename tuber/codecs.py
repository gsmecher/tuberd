from collections.abc import Sequence, Mapping
import functools
import importlib.metadata
import io
import sys
import types
import json

__all__ = ["TuberResult", "Codecs", "AcceptTypes"]

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

    have_cbor = True
except ImportError:
    have_cbor = False


class TuberResult(types.SimpleNamespace):
    """
    Attribute-access container for a decoded dict.

    When a client decodes a response with convert=True, each dict in the
    payload is converted to a TuberResult, so that fields are accessed as
    attributes (result.name) rather than keys (result["name"]).
    """


class Codec:
    """
    Base class for a single wire format.

    The server uses encode() and decode(), whose interfaces match json.dumps()
    and json.loads(), along with join_encoded() to assemble batch responses.
    The client uses decode_response() to decode response bodies.
    """

    # Key in the Codecs registry
    name = None
    # Key in the AcceptTypes registry
    content_type = None
    # False if the codec's backing library is not installed
    available = True

    def encode(self, obj, **kwargs):
        raise NotImplementedError

    def decode(self, data, **kwargs):
        raise NotImplementedError

    def join_encoded(self, encoded_items):
        """
        Assemble an encoded array from individually encoded items.

        The result must decode to the same list as encoding the items in one pass.
        """
        raise NotImplementedError

    def decode_response(self, data, encoding, convert=True):
        """
        Decode a response body on the client.

        data is a bytes-like object containing the encoded data, and encoding
        is the character set (which may be None) given in the Content-Type
        header.  If convert is True, dicts are converted to TuberResult objects.
        """
        raise NotImplementedError


class JsonCodec(Codec):
    name = "json"
    content_type = "application/json"

    @staticmethod
    def default(obj):
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

    @staticmethod
    def object_hook(obj, convert=True):
        """
        Inverse of default(): restore bytes, and optionally convert dicts to TuberResult.
        """
        if isinstance(obj, Mapping) and "bytes" in obj and (len(obj) == 1 or (len(obj) == 2 and "subtype" in obj)):
            try:
                return bytes(obj["bytes"])
            except ValueError:
                pass
        return TuberResult(**obj) if convert else obj

    def encode(self, obj, **kwargs):
        return json.dumps(obj, default=self.default, **kwargs)

    def decode(self, data, **kwargs):
        return json.loads(data, **kwargs)

    def join_encoded(self, encoded_items):
        # The separator matches the default json.dumps() item separator, so the
        # result is byte-identical to encoding the list in one pass.
        return "[" + ", ".join(encoded_items) + "]"

    def decode_response(self, data, encoding, convert=True):
        if encoding is None:  # guess the typical default if unspecified
            encoding = "utf-8"
        hook = functools.partial(self.object_hook, convert=convert)
        return self.decode(data.decode(encoding), object_hook=hook)


class OrjsonCodec(JsonCodec):
    name = "orjson"
    available = have_orjson

    def encode(self, obj, **kwargs):
        # If using orjson with NumPy, overload dumps with the right magic
        if have_numpy:
            kwargs["option"] = kwargs.get("option", 0) | orjson.OPT_SERIALIZE_NUMPY
        return orjson.dumps(obj, default=self.default, **kwargs)

    def decode(self, data, **kwargs):
        return orjson.loads(data, **kwargs)

    def join_encoded(self, encoded_items):
        return b"[" + b",".join(encoded_items) + b"]"

    def decode_response(self, data, encoding, convert=True):
        # orjson.loads() has no object_hook, so this codec is only used server-side.
        raise NotImplementedError("orjson codec does not support client-side decoding")


class CborCodec(Codec):
    name = "cbor"
    content_type = "application/cbor"
    available = have_cbor

    # cbor2 6.0 made several breaking changes:
    #   - dropped CBORDecodeValueError (use the parent CBORDecodeError instead)
    #   - changed tag_hook from (decoder, tag) to (tag, immutable)
    #   - changed object_hook from (decoder, dict) to (dict, immutable)
    #   - enc.fp is None during dumps() (use enc.write() instead, which exists in both)
    version = tuple(int(p) for p in importlib.metadata.version("cbor2").split(".")[:2]) if have_cbor else (0, 0)
    V6 = version >= (6, 0)
    DecodeValueError = (cbor2.CBORDecodeError if V6 else cbor2.CBORDecodeValueError) if have_cbor else Exception

    # RFC 8746 multi-dimensional array tags
    TAG_MD_ROW_MAJOR = 40
    TAG_MD_COL_MAJOR = 1040

    # RFC 8746 typed array tags, by numpy dtype kind and itemsize.  These are the
    # big endian tags; add 4 for little endian if itemsize > 1.
    TYPED_ARRAY_TAGS = {
        "u": {1: 64, 2: 65, 4: 66, 8: 67},
        "i": {1: 72, 2: 73, 4: 74, 8: 75},
        "f": {2: 80, 4: 81, 8: 82, 16: 83},
    }

    @classmethod
    def encode_ndarray(cls, enc, arr):
        # At the moment, this handles only contiguous arrays of data types which can be represented
        # as CBOR typed arrays, as these can be handled with a singleblock copy of the underlying data,
        # with no per-element handling.
        type_tags = cls.TYPED_ARRAY_TAGS
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
            md_tag = cls.TAG_MD_ROW_MAJOR
            order = "C"
        elif arr.flags.f_contiguous:
            md_tag = cls.TAG_MD_COL_MAJOR
            order = "F"
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
        # tobytes() defaults to row-major, so the memory order must match the tag
        enc.write(arr.tobytes(order=order))

    @classmethod
    def default(cls, enc, obj):
        if have_numpy and isinstance(obj, numpy.ndarray):
            cls.encode_ndarray(enc, obj)
            return
        raise cbor2.CBOREncodeTypeError(f"Unsupported object for CBOR encoding {type(obj)}")

    @classmethod
    def decode_tag(cls, tag):
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
                raise cls.DecodeValueError(
                    f"Invalid data size ({len(tag.value)}) for typed array with tag {tag.tag}, interpreted as {dt}"
                )
            # create a 1-D, row-major array to contain all of the data, which can have more detailed
            # shape and ordering information applied later
            arr = numpy.zeros(len(tag.value) // element_size, dtype=dt, order="C")
            # splat the data into the array's memory
            arr.data.cast("B")[:] = tag.value
            return arr
        if have_numpy and tag.tag in (cls.TAG_MD_ROW_MAJOR, cls.TAG_MD_COL_MAJOR):
            if not isinstance(tag.value, Sequence):
                raise cls.DecodeValueError(f"Invalid raw data for multi-dimensional array tag ({tag.tag})")
            if len(tag.value) != 2:
                raise cls.DecodeValueError(f"Invalid raw array length for multi-dimensional array tag ({tag.tag})")
            if not isinstance(tag.value[0], Sequence) or not isinstance(tag.value[1], numpy.ndarray):
                raise cls.DecodeValueError(f"Invalid raw data for multi-dimensional array tag ({tag.tag})")
            order = "C" if tag.tag == cls.TAG_MD_ROW_MAJOR else "F"
            return tag.value[1].reshape(tag.value[0], order=order)
        return None

    # Adapt decode_tag() and TuberResult conversion to the version-specific hook signatures.
    if V6:

        def tag_hook(self, tag, immutable):
            return self.decode_tag(tag)

        def result_hook(self, data, immutable):
            return TuberResult(**data)

    else:

        def tag_hook(self, decoder, tag):
            return self.decode_tag(tag)

        def result_hook(self, decoder, data):
            return TuberResult(**data)

    def encode(self, obj, **kwargs):
        return cbor2.dumps(obj, default=self.default, **kwargs)

    def decode(self, data, **kwargs):
        return cbor2.loads(data, tag_hook=self.tag_hook, **kwargs)

    def join_encoded(self, encoded_items):
        # Use CBOREncoder to write the definite-length array header, then append each
        # pre-encoded item's bytes directly. This is valid CBOR: a definite-length array
        # header followed by N complete CBOR data items.
        buf = io.BytesIO()
        enc = cbor2.CBOREncoder(buf)
        enc.encode_length(4, len(encoded_items))  # CBOR major type 4 = array
        for item_bytes in encoded_items:
            enc.write(item_bytes)
        return buf.getvalue()

    def decode_response(self, data, encoding, convert=True):
        if not convert:
            return self.decode(data)
        return self.decode(data, object_hook=self.result_hook)


# This variable is used to track the codecs enabled on the server, mapping their names to
# codec objects.  See Codec for the interface.  Only codecs whose backing library is
# installed are instantiated.
Codecs = {cls.name: cls() for cls in (JsonCodec, OrjsonCodec, CborCodec) if cls.available}

# This variable is used to track the media types we are able to decode on the client, mapping
# their names to decoding functions.  See Codec.decode_response() for the interface.  The order
# of this dict is the client's default order of preference.  The orjson codec is server-only.
AcceptTypes = {Codecs[n].content_type: Codecs[n].decode_response for n in ("json", "cbor") if n in Codecs}
