#!/usr/bin/env python3
"""Insert stdin text into the currently focused X11 application.

The process owns CLIPBOARD long enough to serve the focused application's
paste request, then exits.  Text is passed over stdin so it is never exposed
in argv or process listings.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import time
import tkinter as tk


# X11 core modifier masks. LockMask (Caps Lock) and Mod2Mask (normally Num
# Lock) do not alter Ctrl+V, so they are intentionally excluded. Right Alt is
# commonly Mod1 or Mod5 depending on the keyboard layout; both are blocked.
BLOCKING_PASTE_MODIFIER_MASK = (
    (1 << 0)  # ShiftMask
    | (1 << 2)  # ControlMask
    | (1 << 3)  # Mod1Mask (usually Alt)
    | (1 << 5)  # Mod3Mask
    | (1 << 6)  # Mod4Mask (usually Super)
    | (1 << 7)  # Mod5Mask (often AltGr / Right Alt)
)


def wait_for_paste_modifiers_released(
    query_modifier_mask,
    *,
    timeout: float = 180.0,
    poll_interval: float = 0.01,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> None:
    """Wait until physical modifiers can no longer turn Ctrl+V into another chord."""
    deadline = monotonic() + timeout
    while query_modifier_mask() & BLOCKING_PASTE_MODIFIER_MASK:
        if monotonic() >= deadline:
            raise RuntimeError("timed out waiting for physical modifier release")
        sleep(poll_interval)


def pump_events(root: tk.Tk, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)


def send_ctrl_v() -> None:
    x11_path = ctypes.util.find_library("X11")
    xtst_path = ctypes.util.find_library("Xtst")
    if not x11_path or not xtst_path:
        raise RuntimeError("libX11 or libXtst was not found")

    x11 = ctypes.CDLL(x11_path)
    xtst = ctypes.CDLL(xtst_path)
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
    x11.XStringToKeysym.restype = ctypes.c_ulong
    x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XKeysymToKeycode.restype = ctypes.c_uint
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XQueryPointer.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint),
    ]
    x11.XQueryPointer.restype = ctypes.c_int
    x11.XFlush.argtypes = [ctypes.c_void_p]
    xtst.XTestFakeKeyEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    xtst.XTestFakeKeyEvent.restype = ctypes.c_int

    display = x11.XOpenDisplay(None)
    if not display:
        raise RuntimeError("cannot open the X11 display")
    try:
        control = x11.XKeysymToKeycode(display, 0xFFE3)  # Control_L
        v_key = x11.XKeysymToKeycode(display, x11.XStringToKeysym(b"v"))
        if not control or not v_key:
            raise RuntimeError("cannot resolve Ctrl+V keycodes")

        root_window = x11.XDefaultRootWindow(display)

        def query_modifier_mask() -> int:
            root_return = ctypes.c_ulong()
            child_return = ctypes.c_ulong()
            root_x = ctypes.c_int()
            root_y = ctypes.c_int()
            win_x = ctypes.c_int()
            win_y = ctypes.c_int()
            mask = ctypes.c_uint()
            if not x11.XQueryPointer(
                display,
                root_window,
                ctypes.byref(root_return),
                ctypes.byref(child_return),
                ctypes.byref(root_x),
                ctypes.byref(root_y),
                ctypes.byref(win_x),
                ctypes.byref(win_y),
                ctypes.byref(mask),
            ):
                raise RuntimeError("XQueryPointer failed")
            return int(mask.value)

        # Close the race where the recording key is pressed after the bridge's
        # async safety check but before the helper actually emits Ctrl+V.
        wait_for_paste_modifiers_released(query_modifier_mask)
        for keycode, pressed in (
            (control, 1),
            (v_key, 1),
            (v_key, 0),
            (control, 0),
        ):
            if not xtst.XTestFakeKeyEvent(display, keycode, pressed, 0):
                raise RuntimeError("XTestFakeKeyEvent failed")
        x11.XFlush(display)
    finally:
        x11.XCloseDisplay(display)


def main() -> int:
    text = sys.stdin.read()
    if not text:
        return 0

    root = tk.Tk()
    root.withdraw()
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        pump_events(root, 0.08)
        send_ctrl_v()
        # Keep ownership while Chromium/GTK completes its asynchronous
        # clipboard request.  The helper remains hidden and never takes focus.
        pump_events(root, 1.0)
    finally:
        root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
