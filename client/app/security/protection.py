"""Windows DPAPI protection for local client credentials and state."""

from __future__ import annotations

import ctypes
import sys


class StateProtector:
    """Protect and unprotect local state bytes."""

    name = "plain"

    def protect(self, data: bytes) -> bytes:
        raise NotImplementedError

    def unprotect(self, data: bytes) -> bytes:
        raise NotImplementedError


class WindowsDpapiProtector(StateProtector):
    """Protect local state with the current Windows user profile."""

    name = "windows-dpapi"

    @classmethod
    def is_available(cls) -> bool:
        return sys.platform == "win32"

    def protect(self, data: bytes) -> bytes:
        return _crypt_data(data, protect=True)

    def unprotect(self, data: bytes) -> bytes:
        return _crypt_data(data, protect=False)


def default_state_protector() -> StateProtector | None:
    """Return the best local state protector available on this platform."""
    return WindowsDpapiProtector() if WindowsDpapiProtector.is_available() else None


def state_protector_from_name(name: str) -> StateProtector | None:
    """Return a protector able to read a saved protected state payload."""
    if (
        str(name or "").strip().casefold() == WindowsDpapiProtector.name
        and WindowsDpapiProtector.is_available()
    ):
        return WindowsDpapiProtector()
    return None


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _crypt_data(data: bytes, *, protect: bool) -> bytes:
    if not WindowsDpapiProtector.is_available():
        raise RuntimeError("Windows DPAPI is not available")
    buffer = ctypes.create_string_buffer(data)
    data_in = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    data_out = _DataBlob()
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    if protect:
        success = crypt32.CryptProtectData(
            ctypes.byref(data_in),
            "EVE Sentry client state",
            None,
            None,
            None,
            0,
            ctypes.byref(data_out),
        )
    else:
        success = crypt32.CryptUnprotectData(
            ctypes.byref(data_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(data_out),
        )
    if not success:
        raise ctypes.WinError()  # type: ignore[attr-defined]
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(data_out.pbData)
