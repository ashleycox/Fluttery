"""Coordinate the frozen Windows process with its native installer."""

from contextlib import contextmanager


@contextmanager
def hold_installation(folder):
    """Hold the same shared marker as the portable Windows launcher."""
    from ctypes import WinDLL, WinError, get_last_error
    from ctypes.wintypes import BOOL, DWORD, HANDLE, LPCWSTR, LPVOID

    kernel = WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (LPCWSTR, DWORD, DWORD, LPVOID, DWORD, DWORD, HANDLE)
    create.restype = HANDLE
    close = kernel.CloseHandle
    close.argtypes = (HANDLE,)
    close.restype = BOOL
    # Read access, other readers allowed, open the existing installer marker.
    handle = create(str(folder / '.running'), 0x80000000, 1, None, 3, 0, None)
    if handle == HANDLE(-1).value:
        error = get_last_error()
        if error in (2, 3):  # An unpacked build has no installer marker.
            yield
            return
        if error in (32, 33):
            raise OSError('Fluttery is being installed, updated or removed. Try again when setup is finished.')
        raise WinError(error)
    try:
        yield
    finally:
        close(handle)
