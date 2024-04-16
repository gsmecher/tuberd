from collections.abc import Sequence
import sys

try:
    import numpy
    have_numpy = True
except ImportError:
    have_numpy = False
    
try:
    import cbor2
    have_cbor = True
except ImportError:
    have_cbor = False

def wrap_bytes_for_json(obj):
    '''
    JSON cannot (natively) encode bytes, so we provide a simple encoding for them.
    This allows uniformity when using either JSON or binary formats (CBOR, etc.)
    which do have native binary support. The JSON encoding is not meant to be
    especially efficient, since anyone wanting seriously move around significant
    amounts of binary data should use another format, but it provides a
    consistent, readable/debuggable, fall-back.
    '''
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
        'u': {
            1: 64,
            2: 65,
            4: 66,
            8: 67,
        },
        'i': {
            1: 72,
            2: 73,
            4: 74,
            8: 75,
        },
        'f': {
            2:  80,
            4:  81,
            8:  82,
            16: 83,
        },
    }
    if arr.dtype.kind not in type_tags or arr.dtype.itemsize not in type_tags[arr.dtype.kind]:
        raise cbor2.CBOREncodeTypeError(f"Serialization of numpy arrays with element type {arr.dtype} is not implemented")
    type_tag = type_tags[arr.dtype.kind][arr.dtype.itemsize]
    # add 4 to type tag if little endian if sizeof(type) > 1
    if arr.dtype.itemsize > 1 and (arr.dtype.byteorder == '<'
          or (arr.dtype.byteorder == '=' and sys.byteorder == "little")):
        type_tag += 4

    if arr.flags.c_contiguous:
        md_tag = 40  # row-major
    elif arr.flags.f_contiguous:
        md_tag = 1040  # column-major
    else:
        raise cbor2.CBOREncodeTypeError("Serialization of non-contiguous numpy arrays is not implemented")

    enc.encode_length(6, md_tag) # multi-dimensional array header, a tag (type 6) of the correct type
    enc.encode_length(4, 2) # payload of the m-d array is always an array (type 4) of length 2
    enc.encode_length(4, len(arr.shape)) # the first item in the outer array is the array of extents
    for extent in arr.shape:
        enc.encode_int(extent)
    # the second item in the outer array is the array entries, for which we use a typed array
    enc.encode_length(6, type_tag)
    # the typed array payload is a bytestring (type 2)
    enc.encode_length(2, arr.nbytes)
    # call write directly on the stream object to avoid unnecessary copies
    enc.fp.write(arr.data)


def cbor_augment_encode(enc, obj):
    if isinstance(obj, numpy.ndarray):
        cbor_encode_ndarray(enc, obj)
        return
    raise cbor2.CBOREncodeTypeError(f"Unsupported object for CBOR encoding {type(obj)}")


def cbor_tag_decode(dec, tag):
    if have_numpy and tag.tag>=64 and tag.tag<=87 and tag.tag!=76: # Typed arrays
        is_float = tag.tag & 0x10
        is_signed = tag.tag & 0x8
        is_le = tag.tag & 0x4
        ll = tag.tag & 0x3
        element_size = 1<<ll
        if is_float: # floats are one power of two larger
            element_size <<= 1
        # due to the cap of 87 on the tag, we will never see invalid 'signed' float combinations
        dt = numpy.dtype(f"{'<' if is_le else '>'}{'f' if is_float else 'i' if is_signed else 'u'}{element_size}")
        if len(tag.value) % element_size != 0:
            raise cbor2.CBORDecodeValueError(f"Invalid data size ({len(tag.value)}) for typed array with tag {tag.tag}, interpreted as {dt}")
        # create a 1-D, row-major array to contain all of the data, which can have more detailed
        # shape and ordering information applied later
        arr = numpy.zeros(len(tag.value) // element_size, dtype=dt, order='C')
        # splat the data into the array's memory
        arr.data.cast('B')[:] = tag.value
        return arr
    if have_numpy and (tag.tag == 40 or tag.tag == 1040):
        if not isinstance(tag.value, Sequence):
            raise cbor2.CBORDecodeValueError(f"Invalid raw data for multi-dimensional array tag ({tag.tag})")
        if len(tag.value) != 2:
            raise cbor2.CBORDecodeValueError(f"Invalid raw array length for multi-dimensional array tag ({tag.tag})")
        if not isinstance(tag.value[0], Sequence) or not isinstance(tag.value[1], numpy.ndarray):
            raise cbor2.CBORDecodeValueError(f"Invalid raw data for multi-dimensional array tag ({tag.tag})")
        arr = tag.value[1].reshape(tag.value[0], order='C' if tag.tag == 40 else 'F')
        return arr
    return None
