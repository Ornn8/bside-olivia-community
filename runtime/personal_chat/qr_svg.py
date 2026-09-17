"""Small dependency-free QR encoder for short local login payloads.

This intentionally supports only UTF-8 byte mode, error-correction level L, and
QR versions 1 through 9. That covers up to 230 payload bytes and keeps the
personal-chat setup path inside the existing offline runtime closure.
"""
from __future__ import annotations

from collections.abc import Sequence


# QR Code Model 2, error-correction level L. Each entry is
# (block_count, total_codewords_per_block, data_codewords_per_block).
_BLOCKS_L: dict[int, tuple[tuple[int, int, int], ...]] = {
    1: ((1, 26, 19),),
    2: ((1, 44, 34),),
    3: ((1, 70, 55),),
    4: ((1, 100, 80),),
    5: ((1, 134, 108),),
    6: ((2, 86, 68),),
    7: ((2, 98, 78),),
    8: ((2, 121, 97),),
    9: ((2, 146, 116),),
}

_ALIGNMENT: dict[int, tuple[int, ...]] = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
}


class QRPayloadTooLong(ValueError):
    pass


def _gf_multiply(left: int, right: int) -> int:
    result = 0
    for _ in range(8):
        if right & 1:
            result ^= left
        carry = left & 0x80
        left = (left << 1) & 0xFF
        if carry:
            left ^= 0x1D
        right >>= 1
    return result


def _reed_solomon_divisor(degree: int) -> list[int]:
    result = [0] * degree
    result[-1] = 1
    root = 1
    for _ in range(degree):
        for index in range(degree):
            result[index] = _gf_multiply(result[index], root)
            if index + 1 < degree:
                result[index] ^= result[index + 1]
        root = _gf_multiply(root, 0x02)
    return result


def _reed_solomon_remainder(data: Sequence[int], divisor: Sequence[int]) -> list[int]:
    result = [0] * len(divisor)
    for value in data:
        factor = value ^ result.pop(0)
        result.append(0)
        for index, coefficient in enumerate(divisor):
            result[index] ^= _gf_multiply(coefficient, factor)
    return result


def _append_bits(target: list[int], value: int, count: int) -> None:
    if count < 0 or value >> count:
        raise ValueError("QR_BITS_INVALID")
    target.extend((value >> shift) & 1 for shift in range(count - 1, -1, -1))


def _codewords(payload: bytes) -> tuple[int, list[int]]:
    version = 0
    data_capacity = 0
    for candidate, groups in _BLOCKS_L.items():
        data_capacity = sum(count * data for count, _total, data in groups)
        # Versions 1..9 use an 8-bit byte-mode character count.
        if 4 + 8 + len(payload) * 8 <= data_capacity * 8:
            version = candidate
            break
    if not version:
        raise QRPayloadTooLong("QR_PAYLOAD_TOO_LONG")

    bits: list[int] = []
    _append_bits(bits, 0b0100, 4)  # Byte mode.
    _append_bits(bits, len(payload), 8)
    for value in payload:
        _append_bits(bits, value, 8)

    capacity_bits = data_capacity * 8
    _append_bits(bits, 0, min(4, capacity_bits - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    padding = (0xEC, 0x11)
    pad_index = 0
    while len(bits) < capacity_bits:
        _append_bits(bits, padding[pad_index % 2], 8)
        pad_index += 1

    packed = [
        sum(bits[offset + index] << (7 - index) for index in range(8))
        for offset in range(0, len(bits), 8)
    ]

    blocks: list[tuple[list[int], list[int]]] = []
    cursor = 0
    for count, total, data_count in _BLOCKS_L[version]:
        ecc_count = total - data_count
        divisor = _reed_solomon_divisor(ecc_count)
        for _ in range(count):
            data = packed[cursor : cursor + data_count]
            cursor += data_count
            blocks.append((data, _reed_solomon_remainder(data, divisor)))

    interleaved: list[int] = []
    for index in range(max(len(data) for data, _ecc in blocks)):
        for data, _ecc in blocks:
            if index < len(data):
                interleaved.append(data[index])
    for index in range(max(len(ecc) for _data, ecc in blocks)):
        for _data, ecc in blocks:
            if index < len(ecc):
                interleaved.append(ecc[index])
    return version, interleaved


def _bit(value: int, index: int) -> bool:
    return ((value >> index) & 1) != 0


def matrix(text: str) -> list[list[bool]]:
    """Return a valid QR module matrix for a short UTF-8 text payload."""
    payload = text.encode("utf-8")
    version, codewords = _codewords(payload)
    size = version * 4 + 17
    modules = [[False] * size for _ in range(size)]
    function = [[False] * size for _ in range(size)]

    def set_function(x: int, y: int, dark: bool) -> None:
        if 0 <= x < size and 0 <= y < size:
            modules[y][x] = dark
            function[y][x] = True

    for index in range(size):
        set_function(6, index, index % 2 == 0)
        set_function(index, 6, index % 2 == 0)

    def finder(center_x: int, center_y: int) -> None:
        for delta_y in range(-4, 5):
            for delta_x in range(-4, 5):
                distance = max(abs(delta_x), abs(delta_y))
                set_function(
                    center_x + delta_x,
                    center_y + delta_y,
                    distance not in {2, 4},
                )

    finder(3, 3)
    finder(size - 4, 3)
    finder(3, size - 4)

    def alignment(center_x: int, center_y: int) -> None:
        for delta_y in range(-2, 3):
            for delta_x in range(-2, 3):
                set_function(
                    center_x + delta_x,
                    center_y + delta_y,
                    max(abs(delta_x), abs(delta_y)) != 1,
                )

    positions = _ALIGNMENT[version]
    skip = {(0, 0), (0, len(positions) - 1), (len(positions) - 1, 0)}
    for x_index, x in enumerate(positions):
        for y_index, y in enumerate(positions):
            if (x_index, y_index) not in skip:
                alignment(x, y)

    # Level L has format bits 01. We deliberately use mask 0; any defined mask
    # is valid, and a fixed mask avoids adding heuristic code to the setup path.
    format_data = (1 << 3) | 0
    remainder = format_data
    for _ in range(10):
        remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
    format_bits = ((format_data << 10) | remainder) ^ 0x5412

    for index in range(6):
        set_function(8, index, _bit(format_bits, index))
    set_function(8, 7, _bit(format_bits, 6))
    set_function(8, 8, _bit(format_bits, 7))
    set_function(7, 8, _bit(format_bits, 8))
    for index in range(9, 15):
        set_function(14 - index, 8, _bit(format_bits, index))
    for index in range(8):
        set_function(size - 1 - index, 8, _bit(format_bits, index))
    for index in range(8, 15):
        set_function(8, size - 15 + index, _bit(format_bits, index))
    set_function(8, size - 8, True)

    if version >= 7:
        remainder = version
        for _ in range(12):
            remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
        version_bits = (version << 12) | remainder
        for index in range(18):
            dark = _bit(version_bits, index)
            a = size - 11 + index % 3
            b = index // 3
            set_function(a, b, dark)
            set_function(b, a, dark)

    data_bits: list[int] = []
    for value in codewords:
        data_bits.extend((value >> shift) & 1 for shift in range(7, -1, -1))

    cursor = 0
    right = size - 1
    upward = True
    while right >= 1:
        if right == 6:
            right -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for y in rows:
            for x in (right, right - 1):
                if function[y][x]:
                    continue
                value = data_bits[cursor] if cursor < len(data_bits) else 0
                cursor += 1
                if (x + y) % 2 == 0:  # Mask 0.
                    value ^= 1
                modules[y][x] = bool(value)
        upward = not upward
        right -= 2
    return modules


def svg_bytes(text: str, *, border: int = 4, scale: int = 8) -> bytes:
    """Render the QR matrix as a self-contained black/white SVG."""
    if border < 4 or scale < 1 or scale > 32:
        raise ValueError("QR_RENDER_INVALID")
    modules = matrix(text)
    side = (len(modules) + border * 2) * scale
    path: list[str] = []
    for y, row in enumerate(modules):
        for x, dark in enumerate(row):
            if dark:
                left = (x + border) * scale
                top = (y + border) * scale
                path.append(f"M{left},{top}h{scale}v{scale}h-{scale}z")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" '
        f'width="{side}" height="{side}">'
        '<rect width="100%" height="100%" fill="#fff"/>'
        f'<path d="{"".join(path)}" fill="#000"/></svg>'
    ).encode("ascii")


__all__ = ["QRPayloadTooLong", "matrix", "svg_bytes"]
