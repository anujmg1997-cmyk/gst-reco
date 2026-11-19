"""Encrypt saved portal passwords with Windows DPAPI.

The encrypted text can only be decrypted by the same Windows user on the same PC, so copying the
settings file to another computer or user account does not reveal the password.
"""
import base64
import os

PREFIX = "dpapi:"


class SecretError(Exception):
    pass


def available() -> bool:
    return os.name == "nt"


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data: bytes):
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _protect(data: bytes) -> bytes:
        src, _keep = _blob(data)
        out = _Blob()
        if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), "GST ReconX", None, None, None, 0x1,
                                                      ctypes.byref(out)):
            raise SecretError("Windows could not encrypt the password.")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)

    def _unprotect(data: bytes) -> bytes:
        src, _keep = _blob(data)
        out = _Blob()
        if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0x1,
                                                        ctypes.byref(out)):
            raise SecretError("The saved password can't be read on this PC/user – enter it again.")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
else:
    def _protect(data: bytes) -> bytes:
        raise SecretError("Saving passwords is only supported on Windows.")

    def _unprotect(data: bytes) -> bytes:
        raise SecretError("Saving passwords is only supported on Windows.")


def protect(text: str) -> str:
    if not text:
        return ""
    return PREFIX + base64.b64encode(_protect(text.encode("utf-8"))).decode("ascii")


def unprotect(token: str) -> str:
    if not token:
        return ""
    if not token.startswith(PREFIX):
        raise SecretError("Unknown password format – enter it again.")
    return _unprotect(base64.b64decode(token[len(PREFIX):])).decode("utf-8")
