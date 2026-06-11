"""OLE2 compound-document reader for HWP5 binary files.

Dependency: pip install olefile
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Dict, List

try:
    import olefile
    _OLE_OK = True
except ImportError:
    _OLE_OK = False

HWP_SIGNATURE = b"HWP Document File"
_HWP_SIG_LEN  = len(HWP_SIGNATURE)

# Streams that are *never* zlib-compressed even when the compressed flag is set
_RAW_STREAMS = {"FileHeader", "PrvImage", "PrvText", "\x05HwpSummaryInformation"}

# Bit positions in the FileHeader flags DWORD (offset 36)
_FLAG_BITS: Dict[str, int] = {
    "compressed":    0,
    "password":      1,
    "distributed":   2,
    "script":        3,
    "drm":           4,
    "xml_template":  5,
    "history":       6,
    "sign":          7,
    "cert_encrypt":  8,
    "ccl":           9,
    "mobile":        10,
    "track_changes": 11,
}


class HwpOleDocument:
    """Thin wrapper around olefile that understands HWP5 stream layout."""

    def __init__(self, path: str | Path) -> None:
        if not _OLE_OK:
            raise RuntimeError("olefile missing — run: pip install olefile")
        self.path = Path(path)
        self._ole  = olefile.OleFileIO(str(self.path))
        self._flags: Dict[str, bool] | None = None

    # ── stream enumeration ─────────────────────────────────────────────
    def list_streams(self) -> List[str]:
        return ["/".join(e) for e in self._ole.listdir(streams=True, storages=False)]

    def has_stream(self, path: str) -> bool:
        return self._ole.exists(path.split("/"))

    def section_streams(self) -> List[str]:
        return sorted(
            s for s in self.list_streams() if s.startswith("BodyText/Section")
        )

    def masterpage_streams(self) -> List[str]:
        return [s for s in self.list_streams() if s.startswith("MasterPage")]

    # ── raw / decompressed read ────────────────────────────────────────
    def read_raw(self, path: str) -> bytes:
        with self._ole.openstream(path.split("/")) as f:
            return f.read()

    def read_stream(self, path: str) -> bytes:
        """Return decompressed bytes for *path*."""
        raw = self.read_raw(path)
        top = path.split("/")[0]
        if top in _RAW_STREAMS:
            return raw
        flags = self.flags()
        if not flags.get("compressed"):
            return raw
        # HWP5 zlib compression: first 4 bytes = uncompressed size
        if len(raw) < 4:
            return raw
        try:
            return zlib.decompress(raw[4:])
        except zlib.error:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return raw          # return as-is if decompression fails

    # ── FileHeader helpers ─────────────────────────────────────────────
    def validate_signature(self) -> tuple[bool, str]:
        if not self.has_stream("FileHeader"):
            return False, "FileHeader stream missing"
        data = self.read_raw("FileHeader")
        if not data.startswith(HWP_SIGNATURE):
            return False, f"HWP signature absent (got {data[:20]!r})"
        return True, "OK"

    def hwp_version(self) -> tuple[int, int, int, int]:
        """Return (major, minor, micro, build) from FileHeader offset 32."""
        data = self.read_raw("FileHeader")
        if len(data) < 36:
            return (0, 0, 0, 0)
        b = data[32:36]
        return b[3], b[2], b[1], b[0]   # big-endian packed in 4 bytes

    def flags(self) -> Dict[str, bool]:
        if self._flags is not None:
            return self._flags
        data = self.read_raw("FileHeader")
        if len(data) < 40:
            self._flags = {}
            return self._flags
        word = struct.unpack_from("<I", data, 36)[0]
        self._flags = {name: bool(word & (1 << bit)) for name, bit in _FLAG_BITS.items()}
        return self._flags

    # ── context manager ────────────────────────────────────────────────
    def __enter__(self):  return self
    def __exit__(self, *_): self._ole.close()
    def close(self):        self._ole.close()
