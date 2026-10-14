"""Ed25519 (RFC 8032) in plain Python: the client checks the signed update manifest with it.

The bundled runtime has no crypto library. Verifying one signature takes a few tens of milliseconds;
nothing here handles secrets on the client (sign() is for the release tools).
"""
import hashlib

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _inv(x):
    return pow(x, P - 2, P)


def _recover_x(y, sign):
    if y >= P:
        return None
    x2 = (y * y - 1) * _inv(D * y * y + 1) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P:
        return None
    return P - x if (x & 1) != sign else x


_GY = 4 * _inv(5) % P
_GX = _recover_x(_GY, 0)
G = (_GX, _GY, 1, _GX * _GY % P)


def _add(a, b):
    e1 = (a[1] - a[0]) * (b[1] - b[0]) % P
    e2 = (a[1] + a[0]) * (b[1] + b[0]) % P
    c = 2 * a[3] * b[3] * D % P
    d = 2 * a[2] * b[2] % P
    e, f, g, h = e2 - e1, d - c, d + c, e2 + e1
    return e * f % P, g * h % P, f * g % P, e * h % P


def _mul(s, point):
    result = (0, 1, 1, 0)
    while s:
        if s & 1:
            result = _add(result, point)
        point = _add(point, point)
        s >>= 1
    return result


def _same(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


def _compress(point):
    zi = _inv(point[2])
    x, y = point[0] * zi % P, point[1] * zi % P
    return (y | ((x & 1) << 255)).to_bytes(32, 'little')


def _decompress(data):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, 'little')
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


def _hash(*parts):
    return int.from_bytes(hashlib.sha512(b''.join(parts)).digest(), 'little')


def _expand(seed):
    digest = hashlib.sha512(seed).digest()
    a = int.from_bytes(digest[:32], 'little')
    return (a & ((1 << 254) - 8)) | (1 << 254), digest[32:]


def public_key(seed):
    return _compress(_mul(_expand(seed)[0], G))


def sign(seed, message):
    a, prefix = _expand(seed)
    public = _compress(_mul(a, G))
    r = _hash(prefix, message) % L
    rs = _compress(_mul(r, G))
    s = (r + _hash(rs, public, message) % L * a) % L
    return rs + s.to_bytes(32, 'little')


def verify(public, message, signature):
    if len(public) != 32 or len(signature) != 64:
        return False
    a, r = _decompress(public), _decompress(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], 'little')
    if s >= L:
        return False
    h = _hash(signature[:32], public, message) % L
    return _same(_mul(s, G), _add(r, _mul(h, a)))
