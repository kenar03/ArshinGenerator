"""HTTPS transport using Windows proxy and certificate policy."""

from contextlib import ExitStack
import ctypes
from ctypes import wintypes
from functools import lru_cache
from urllib.parse import urlsplit


AUTOMATIC_PROXY = 4
SECURE = 0x00800000
REDIRECT_POLICY = 88
REDIRECT_NEVER = 0
STATUS_NUMBER = 19 | 0x20000000
CUSTOM_HEADER = 65535
HEADER_NOT_FOUND = 12150
MAX_RESPONSE = 2_000_000


@lru_cache(maxsize=1)
def _library():
    handle, dword, wide = wintypes.HANDLE, wintypes.DWORD, wintypes.LPCWSTR
    pointer = ctypes.c_void_p
    lib = ctypes.WinDLL("winhttp.dll", use_last_error=True, winmode=0x800)
    signatures = {
        "WinHttpOpen": (handle, (wide, dword, wide, wide, dword)),
        "WinHttpConnect": (handle, (handle, wide, wintypes.WORD, dword)),
        "WinHttpOpenRequest": (handle, (handle, wide, wide, wide, wide, pointer, dword)),
        "WinHttpSetTimeouts": (wintypes.BOOL, (handle, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)),
        "WinHttpSetOption": (wintypes.BOOL, (handle, dword, pointer, dword)),
        "WinHttpSendRequest": (wintypes.BOOL, (handle, wide, dword, pointer, dword, dword, ctypes.c_size_t)),
        "WinHttpReceiveResponse": (wintypes.BOOL, (handle, pointer)),
        "WinHttpQueryHeaders": (wintypes.BOOL, (handle, dword, wide, pointer,
                                               ctypes.POINTER(dword), ctypes.POINTER(dword))),
        "WinHttpReadData": (wintypes.BOOL, (handle, pointer, dword, ctypes.POINTER(dword))),
        "WinHttpCloseHandle": (wintypes.BOOL, (handle,)),
    }
    for name, (result, args) in signatures.items():
        function = getattr(lib, name)
        function.argtypes = args
        function.restype = result
    return lib


def _required(result, operation):
    if not result:
        code = ctypes.get_last_error()
        raise OSError(None, f"{operation}: {ctypes.FormatError(code).strip()}", None, code)
    return result


def _retry_after(lib, request):
    value = ctypes.create_unicode_buffer(96)
    size = wintypes.DWORD(ctypes.sizeof(value))
    if lib.WinHttpQueryHeaders(request, CUSTOM_HEADER, "Retry-After", value, ctypes.byref(size), None):
        return value.value
    if ctypes.get_last_error() != HEADER_NOT_FOUND:
        _required(False, "WinHttpQueryHeaders/Retry-After")
    return ""


def windows_get(url: str, user_agent: str, timeout: float):
    target = urlsplit(url)
    if target.scheme != "https" or target.netloc != "fgis.gost.ru" or not target.path.startswith("/fundmetrology/eapi/"):
        raise ValueError("Недопустимый адрес реестра.")
    lib = _library()
    path = target.path + ("?" + target.query if target.query else "")
    millis = max(1, int(timeout * 1000))
    with ExitStack() as held:
        def acquire(result, name):
            handle = _required(result, name)
            held.callback(lib.WinHttpCloseHandle, handle)
            return handle

        session = acquire(lib.WinHttpOpen(user_agent, AUTOMATIC_PROXY, None, None, 0), "WinHttpOpen")
        _required(lib.WinHttpSetTimeouts(session, min(8000, millis), min(12000, millis), millis, millis),
                  "WinHttpSetTimeouts")
        connection = acquire(lib.WinHttpConnect(session, target.hostname, 443, 0), "WinHttpConnect")
        request = acquire(lib.WinHttpOpenRequest(connection, "GET", path, None, None, None, SECURE), "WinHttpOpenRequest")
        policy = wintypes.DWORD(REDIRECT_NEVER)
        _required(lib.WinHttpSetOption(request, REDIRECT_POLICY, ctypes.byref(policy), ctypes.sizeof(policy)),
                  "WinHttpSetOption")
        _required(lib.WinHttpSendRequest(request, "Accept: application/json\r\n", -1, None, 0, 0, 0),
                  "WinHttpSendRequest")
        _required(lib.WinHttpReceiveResponse(request, None), "WinHttpReceiveResponse")
        status = wintypes.DWORD()
        size = wintypes.DWORD(ctypes.sizeof(status))
        _required(lib.WinHttpQueryHeaders(request, STATUS_NUMBER, None, ctypes.byref(status), ctypes.byref(size), None),
                  "WinHttpQueryHeaders/status")
        if status.value != 200:
            return status.value, b"", _retry_after(lib, request)
        output = bytearray()
        chunk = ctypes.create_string_buffer(16384)
        amount = wintypes.DWORD()
        while len(output) <= MAX_RESPONSE:
            _required(lib.WinHttpReadData(request, chunk, len(chunk), ctypes.byref(amount)), "WinHttpReadData")
            if not amount.value:
                break
            output.extend(chunk.raw[:amount.value])
        return status.value, bytes(output), ""
