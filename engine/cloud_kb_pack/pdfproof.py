"""Compare complete page payloads while ignoring storage-specific object IDs/encoding."""
from decimal import Decimal
import hashlib
import json

from pypdf.generic import (ArrayObject, DictionaryObject, IndirectObject, StreamObject,
                          BooleanObject, NullObject, NumberObject, FloatObject, ByteStringObject)


def stream_proof(value):
    filters = value.get("/Filter", [])
    filters = [filters] if not isinstance(filters, ArrayObject) else filters
    if "/JBIG2Decode" in filters:
        # Native cloning preserves JBIG2 encoded bytes and its decoder/global
        # dictionaries exactly. No lossy decode/re-encode or external binary is needed.
        return {"/Length"}, "encoded_stream_sha256", hashlib.sha256(value._data).hexdigest()
    return {"/Length", "/Filter", "/DecodeParms"}, "decoded_stream_sha256", hashlib.sha256(value.get_data()).hexdigest()


def page_fingerprint(page):
    stack = set()
    def canonical(value):
        if isinstance(value, IndirectObject):
            value = value.get_object()
        if isinstance(value, (DictionaryObject, ArrayObject)):
            if id(value) in stack:
                return ["cycle"]
            stack.add(id(value))
            try:
                if isinstance(value, ArrayObject):
                    return [canonical(item) for item in value]
                keys = sorted(key for key in value if key != "/Parent")
                if isinstance(value, StreamObject):
                    ignored, proof_key, proof_value = stream_proof(value)
                    keys = [k for k in keys if k not in ignored]
                result = {str(key): canonical(value[key]) for key in keys}
                if isinstance(value, StreamObject):
                    result[proof_key] = proof_value
                if value.get("/Type") == "/Page":
                    media = value.get("/MediaBox")
                    crop = value.get("/CropBox", media)
                    for box in ("/CropBox", "/BleedBox", "/TrimBox", "/ArtBox"):
                        result[box] = canonical(value.get(box, crop))
                    result["/Rotate"] = ["number", str(int(value.get("/Rotate", 0)) % 360)]
                    result["/Resources"] = canonical(value.get("/Resources", DictionaryObject()))
                    if not value.get("/Annots"):
                        result.pop("/Annots", None)
                return result
            finally:
                stack.remove(id(value))
        if isinstance(value, ByteStringObject):
            return ["bytes", value.hex()]
        if isinstance(value, BooleanObject):
            return ["bool", value.value]
        if isinstance(value, NullObject) or value is None:
            return None
        if isinstance(value, (NumberObject, FloatObject)):
            number = Decimal(str(value)).normalize()
            return ["number", str(number if number else Decimal(0))]
        return str(value)
    return hashlib.sha256(json.dumps(canonical(page), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
