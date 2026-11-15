"""稳定的有类型身份域。"""
import hashlib
import json
VERSION = 'typed-json-sha256-64-v1'

def canonical(value):
    if value is None:
        return b'null'
    if type(value) not in (str, int, bool):
        raise ValueError('身份仅允许整数、文本和布尔值')
    return json.dumps([type(value).__name__, value], ensure_ascii=False, separators=(',', ':')).encode()

def hash_identity(value, seed):
    if value is None:
        return None
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ValueError('种子超出uint64范围')
    body = seed.to_bytes(8, 'big') + canonical(value)
    return int.from_bytes(hashlib.sha256(body).digest()[:8], 'big')
