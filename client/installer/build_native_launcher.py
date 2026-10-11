"""Build the windowless .NET Framework launcher without a downloaded SDK."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import tempfile


def _normalize_pe(data: bytes) -> bytes:
    """Framework csc has no /deterministic: normalize its timestamp and MVID.

    No PDB, signing, or wildcard assembly version is used. The GUID heap in this
    single-module assembly contains only the MVID. Reject other shapes.
    """
    result = bytearray(data)
    u16 = lambda offset: struct.unpack_from("<H", result, offset)[0]
    u32 = lambda offset: struct.unpack_from("<I", result, offset)[0]
    pe = u32(0x3C)
    if result[pe:pe + 4] != b"PE\0\0":
        raise ValueError("NATIVE_LAUNCHER_INVALID_PE")
    result[pe + 8:pe + 12] = b"\0" * 4
    optional = pe + 24
    sections = optional + u16(pe + 20)

    def file_offset(rva):
        for index in range(u16(pe + 6)):
            section = sections + 40 * index
            address, size, raw = u32(section + 12), u32(section + 16), u32(section + 20)
            if address <= rva < address + size:
                return raw + rva - address
        raise ValueError("NATIVE_LAUNCHER_INVALID_RVA")

    directory = optional + (96 if u16(optional) == 0x10B else 112)
    cli = file_offset(u32(directory + 14 * 8))
    metadata = file_offset(u32(cli + 8))
    cursor = metadata + 16 + u32(metadata + 12)
    streams = u16(cursor + 2)
    cursor += 4
    for _ in range(streams):
        offset, size = u32(cursor), u32(cursor + 4)
        name_end = result.index(0, cursor + 8)
        name = bytes(result[cursor + 8:name_end])
        cursor = (name_end + 4) & ~3
        if name == b"#GUID":
            if size != 16:
                raise ValueError("NATIVE_LAUNCHER_UNEXPECTED_GUID_HEAP")
            start = metadata + offset
            result[start:start + 16] = b"\0" * 16
            result[start:start + 16] = hashlib.sha256(result).digest()[:16]
            return bytes(result)
    raise ValueError("NATIVE_LAUNCHER_MVID_MISSING")


def build_native_launcher(source: Path, output: Path, *, csc: Path | None = None) -> Path:
    """source is the installer directory; all temporary writes stay by output."""
    compiler = csc or Path(os.environ.get("OLIVIA_CSC", str(
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    )))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".native-build-", dir=output.parent) as temporary:
        target = Path(temporary) / "Olivia.exe"
        subprocess.run([
            str(compiler), "/nologo", "/target:winexe", "/platform:anycpu",
            "/optimize+", "/debug-", "/reference:System.Windows.Forms.dll",
            "/out:" + str(target.resolve()),
            "/win32manifest:" + str((source / "olivia_launcher/app.manifest").resolve()),
            "/win32icon:" + str((source / "assets/olivia.ico").resolve()),
            str((source / "olivia_launcher/Program.cs").resolve()),
        ], check=True, capture_output=True, timeout=60,
           env={**os.environ, "TEMP": temporary, "TMP": temporary})
        output.write_bytes(_normalize_pe(target.read_bytes()))
    output.with_suffix(".exe.sha256").write_text(hashlib.sha256(output.read_bytes()).hexdigest() + "\n", encoding="ascii")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).parent)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--csc", type=Path)
    args = parser.parse_args()
    build_native_launcher(args.source, args.output, csc=args.csc)
