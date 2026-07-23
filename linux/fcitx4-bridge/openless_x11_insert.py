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
