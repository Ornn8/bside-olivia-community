"""WeChat's AES-128-ECB/PKCS7 wire format using Windows' built-in CNG.

https://learn.microsoft.com/windows/win32/api/bcrypt/nf-bcrypt-bcryptencrypt
The algorithm is required by the transport, not used for local secret storage.
"""
import ctypes as c
import os


def encrypt_image(raw: bytes, key: bytes) -> bytes:
    if len(key) != 16:
        raise ValueError('WECHAT_IMAGE_KEY_INVALID')
    if os.name != 'nt':
        raise RuntimeError('WECHAT_IMAGE_PLATFORM_UNSUPPORTED')
    api = c.WinDLL('bcrypt.dll', winmode=0x00000800)
    handle, size = c.c_void_p, c.c_ulong
    signatures = {
        'BCryptOpenAlgorithmProvider': [c.POINTER(handle), c.c_wchar_p, c.c_wchar_p, size],
        'BCryptSetProperty': [handle, c.c_wchar_p, c.c_void_p, size, size],
        'BCryptGenerateSymmetricKey': [handle, c.POINTER(handle), c.c_void_p, size, c.c_void_p, size, size],
        'BCryptEncrypt': [handle, c.c_void_p, size, c.c_void_p, c.c_void_p, size, c.c_void_p, size, c.POINTER(size), size],
        'BCryptDestroyKey': [handle],
        'BCryptCloseAlgorithmProvider': [handle, size],
    }
    for name, args in signatures.items():
        fn = getattr(api, name)
        fn.argtypes, fn.restype = args, c.c_long

    def check(status):
        if status < 0:
            raise RuntimeError('WECHAT_IMAGE_ENCRYPTION_FAILED')

    algorithm, secret = handle(), handle()
    try:
        check(api.BCryptOpenAlgorithmProvider(c.byref(algorithm), 'AES', None, 0))
        mode = c.create_unicode_buffer('ChainingModeECB')
        check(api.BCryptSetProperty(algorithm, 'ChainingMode', mode, c.sizeof(mode), 0))
        key_buffer = c.create_string_buffer(key)
        # Windows 7+ allocates and frees the opaque key object with the key handle.
        check(api.BCryptGenerateSymmetricKey(algorithm, c.byref(secret), None, 0, key_buffer, len(key), 0))
        padding = 16 - len(raw) % 16
        padded = raw + bytes([padding]) * padding
        source, output = c.create_string_buffer(padded), c.create_string_buffer(len(padded))
        written = size()
        check(api.BCryptEncrypt(secret, source, len(padded), None, None, 0,
                                output, len(padded), c.byref(written), 0))
        if written.value != len(padded):
            raise RuntimeError('WECHAT_IMAGE_ENCRYPTION_FAILED')
        return output.raw[:written.value]
    finally:
        if secret:
            api.BCryptDestroyKey(secret)
        if algorithm:
            api.BCryptCloseAlgorithmProvider(algorithm, 0)
