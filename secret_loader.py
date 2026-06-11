"""
Secret loading helpers — cross-platform.

우선순위:
  클라우드/Linux : 플랫폼 환경변수(Railway·Render 대시보드) → key.env 폴백
  Windows 로컬   : key.env.enc (DPAPI 복호화) → key.env 폴백
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

# Windows DPAPI — 윈도우에서만 임포트
if os.name == "nt":
    import base64
    import ctypes
    from ctypes import wintypes


WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent.parent.parent
PLAINTEXT_ENV = WORKSPACE_DIR / "key.env"
ENCRYPTED_ENV = WORKSPACE_DIR / "key.env.enc"
PACKAGE_ENV = PACKAGE_DIR / ".env"
CRYPTPROTECT_LOCAL_MACHINE = 0x4  # Windows DPAPI flag (값 자체는 모든 OS에서 정의 무해)


def enable_system_cert_store() -> None:
    """Let Python HTTPS clients trust the OS certificate store when available."""
    if os.getenv("DISABLE_SYSTEM_CERT_STORE", "").lower() == "true":
        return
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        pass


if os.name == "nt":
    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]


def _dpapi_available() -> bool:
    return os.name == "nt"


def _crypt_protect(data: bytes, local_machine: bool = False) -> bytes:
    if not _dpapi_available():
        raise RuntimeError("DPAPI encryption is only available on Windows.")

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    in_buffer = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DataBlob()
    flags = CRYPTPROTECT_LOCAL_MACHINE if local_machine else 0
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        flags,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _crypt_unprotect(data: bytes) -> bytes:
    if not _dpapi_available():
        raise RuntimeError("DPAPI decryption is only available on Windows.")

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    in_buffer = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def encrypt_key_env(src: Path = PLAINTEXT_ENV, dst: Path = ENCRYPTED_ENV, local_machine: bool = True) -> bool:
    """Encrypt key.env to key.env.enc with Windows DPAPI."""
    if not src.exists() or src.stat().st_size == 0:
        return False

    plaintext = src.read_bytes()
    encrypted = base64.b64encode(_crypt_protect(plaintext, local_machine=local_machine))
    dst.write_bytes(encrypted)
    return True


def rekey_encrypted_env(path: Path = ENCRYPTED_ENV, local_machine: bool = True) -> bool:
    """Decrypt an existing key.env.enc and re-encrypt it with the requested scope."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    plaintext = _crypt_unprotect(base64.b64decode(path.read_bytes()))
    path.write_bytes(base64.b64encode(_crypt_protect(plaintext, local_machine=local_machine)))
    return True


def _load_encrypted_env(path: Path = ENCRYPTED_ENV) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False

    plaintext = _crypt_unprotect(base64.b64decode(path.read_bytes()))
    for raw_line in plaintext.decode("utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    return True


def load_secrets() -> None:
    """Load secrets.

    클라우드(Linux): 플랫폼 환경변수가 이미 os.environ에 있으므로 override=False 로
                     key.env 폴백만 시도 (파일 없으면 그냥 스킵).
    Windows 로컬:    key.env.enc(DPAPI) → key.env 순으로 로드.
    """
    enable_system_cert_store()
    load_dotenv(PACKAGE_ENV, override=False)

    encrypted_loaded = False
    if _dpapi_available():          # Windows 전용 경로
        try:
            encrypted_loaded = _load_encrypted_env()
            if encrypted_loaded:
                os.environ.pop("FIGURE_PLANNER_SECRET_ERROR", None)
        except Exception as exc:
            os.environ["FIGURE_PLANNER_SECRET_ERROR"] = str(exc)

    if not encrypted_loaded:
        load_dotenv(PLAINTEXT_ENV, override=False)   # 클라우드: 파일 없으면 스킵됨

    if not os.getenv("LLM_PROVIDER"):
        os.environ["LLM_PROVIDER"] = "openai"
    if not os.getenv("OPENAI_MODEL"):
        os.environ["OPENAI_MODEL"] = "gpt-4.1"
    if os.getenv("LLM_PROVIDER", "").lower() in ("gemini", "google"):
        if not os.getenv("GEMINI_MODEL"):
            os.environ["GEMINI_MODEL"] = "gemini-2.5-flash"
        if not os.getenv("GEMINI_IMAGE_MODEL"):
            os.environ["GEMINI_IMAGE_MODEL"] = "gemini-2.5-flash-image"


def plaintext_key_names(path: Path = PLAINTEXT_ENV) -> list[str]:
    """Return env var names without exposing secret values."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    return sorted(key.lstrip("\ufeff") for key in dotenv_values(path).keys())
