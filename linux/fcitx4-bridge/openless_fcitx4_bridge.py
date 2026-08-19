#!/usr/bin/env python3
"""Compatibility bridge for OpenLess on an Fcitx 4 / Sogou X11 desktop.

OpenLess 1.3.14 hard-requires the DBus surface normally exported by its
Fcitx5 plugin.  This bridge provides that surface without starting Fcitx5:

* global hotkey edges come from XInput2 raw key events;
* CommitText uses a focused-window X11 clipboard inserter because the
  official Linux clipboard fallback does not retain CLIPBOARD ownership on
  this Fcitx 4 / Sogou desktop;
* status-candidate methods are harmless no-ops because Fcitx 4 does not have
  the OpenLess auxiliary-text plugin.

The bridge owns only ``org.fcitx.Fcitx5``.  Fcitx 4 / Sogou continues to use
its original ``org.fcitx.Fcitx`` service, so the two names do not conflict.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.util
import json
import logging
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError
from dbus_next.service import ServiceInterface, method, signal as dbus_signal


BUS_NAME = "org.fcitx.Fcitx5"
OBJECT_PATH = "/openless"
OPENLESS_IFACE = "org.fcitx.Fcitx.OpenLess1"
ALT_KEYSYMS = {0xFFE9, 0xFFEA}  # Alt_L / Alt_R
X11_ANY_MODIFIER = 1 << 15
X11_GRAB_MODE_ASYNC = 1
X11_KEY_PRESS = 2
X11_KEY_RELEASE = 3

LOG = logging.getLogger("openless-fcitx4-bridge")
PREFERENCES_PATH = Path(
    os.environ.get(
        "OPENLESS_PREFERENCES_PATH",
        str(Path.home() / ".local/share/OpenLess/preferences.json"),
    )
)
X11_INSERTER_PATH = Path(__file__).with_name("openless_x11_insert.py")
STATUS_OVERLAY_PATH = Path(__file__).with_name("openless_status_overlay.py")
HISTORY_REFRESH_PATH = Path(__file__).with_name("openless_history_refresh.py")


def read_dictation_mode() -> str:
    """Read the live OpenLess hotkey mode, falling back safely to hold."""
    try:
        preferences = json.loads(PREFERENCES_PATH.read_text(encoding="utf-8"))
        mode = str(preferences.get("hotkey", {}).get("mode", "hold")).lower()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return "hold"
    return mode if mode in {"hold", "toggle"} else "hold"


def dictation_signal_edges(mode: str, is_physical_press: bool) -> tuple[bool, ...]:
    """Map physical key edges to the events consumed by OpenLess.

    Toggle mode must switch on physical key release.  If it switches on key
    press, a fast ASR response can make OpenLess synthesize Ctrl+V while the
    Right Alt trigger is still held, so the focused application never sees a
    plain paste shortcut.
    """
    if mode == "toggle":
        return () if is_physical_press else (True, False)
    return (is_physical_press,)


class X11Keymap:
    def __init__(self) -> None:
        x11_path = ctypes.util.find_library("X11")
        if not x11_path:
            raise RuntimeError("libX11 was not found")
        self._x11 = ctypes.CDLL(x11_path)
        self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._x11.XOpenDisplay.restype = ctypes.c_void_p
        self._x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._x11.XCloseDisplay.restype = ctypes.c_int
        self._x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._x11.XKeysymToKeycode.restype = ctypes.c_uint
        self._x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
        self._x11.XStringToKeysym.restype = ctypes.c_ulong

        self._display = self._x11.XOpenDisplay(None)
        if not self._display:
            raise RuntimeError(f"cannot open X display {os.environ.get('DISPLAY', '')!r}")

    def close(self) -> None:
        if self._display:
            self._x11.XCloseDisplay(self._display)
            self._display = None

    def keysym_to_keycode(self, keysym: int) -> int:
        if not keysym or not self._display:
            return 0
        return int(self._x11.XKeysymToKeycode(self._display, keysym))

    def name_to_keysym(self, name: str) -> int:
        aliases = {
            "alt": "Alt_L",
            "option": "Alt_L",
            "leftalt": "Alt_L",
            "leftoption": "Alt_L",
            "rightalt": "Alt_R",
            "rightoption": "Alt_R",
            "control": "Control_L",
            "ctrl": "Control_L",
            "leftcontrol": "Control_L",
            "leftctrl": "Control_L",
            "rightcontrol": "Control_R",
            "rightctrl": "Control_R",
            "shift": "Shift_L",
            "leftshift": "Shift_L",
            "rightshift": "Shift_R",
            "super": "Super_L",
            "meta": "Super_L",
            "command": "Super_L",
            "cmd": "Super_L",
            "win": "Super_L",
            "leftcommand": "Super_L",
            "leftsuper": "Super_L",
            "rightcommand": "Super_R",
            "rightsuper": "Super_R",
            "fn": "Control_R",
            "space": "space",
            "enter": "Return",
            "return": "Return",
            "escape": "Escape",
            "esc": "Escape",
            "semicolon": "semicolon",
        }
        candidate = aliases.get(name.strip().lower(), name.strip())
        keysym = int(self._x11.XStringToKeysym(candidate.encode("utf-8")))
        if not keysym and len(candidate) == 1:
            keysym = int(self._x11.XStringToKeysym(candidate.lower().encode("utf-8")))
        return keysym

    def modifier_keycodes(self, name: str) -> set[int]:
        normalized = name.strip().lower()
        pairs = {
            "control": (0xFFE3, 0xFFE4),
            "ctrl": (0xFFE3, 0xFFE4),
            "alt": (0xFFE9, 0xFFEA),
            "option": (0xFFE9, 0xFFEA),
            "shift": (0xFFE1, 0xFFE2),
            "super": (0xFFEB, 0xFFEC),
            "meta": (0xFFE7, 0xFFE8, 0xFFEB, 0xFFEC),
            "command": (0xFFEB, 0xFFEC),
            "cmd": (0xFFEB, 0xFFEC),
            "win": (0xFFEB, 0xFFEC),
        }
        return {
            code
            for sym in pairs.get(normalized, ())
            if (code := self.keysym_to_keycode(sym))
        }

@dataclass
class HotkeySpec:
    sym: int = 0
    states: int = 0
    primary_keycode: int = 0
    required_modifier_groups: list[set[int]] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.primary_keycode != 0

    def matches(self, keycode: int, pressed: set[int]) -> bool:
        if not self.enabled or keycode != self.primary_keycode:
            return False
        return all(group & pressed for group in self.required_modifier_groups)


class XErrorEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("resourceid", ctypes.c_ulong),
        ("serial", ctypes.c_ulong),
        ("error_code", ctypes.c_ubyte),
        ("request_code", ctypes.c_ubyte),
        ("minor_code", ctypes.c_ubyte),
    ]


class XKeyEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("keycode", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


class XEvent(ctypes.Union):
    _fields_ = [
        ("xkey", XKeyEvent),
        ("pad", ctypes.c_long * 24),
    ]


class X11DictationSuppressor:
    """Keep a dedicated modifier-only Alt trigger away from focused apps.

    XInput2 raw events remain visible to the bridge, while the passive X11
    grab prevents Electron/Firefox applications from activating their menu
    accelerator layer and losing the original editor or webview focus.
    """

    def __init__(self) -> None:
        x11_path = ctypes.util.find_library("X11")
        if not x11_path:
            raise RuntimeError("libX11 was not found")
        self._x11 = ctypes.CDLL(x11_path)
        self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._x11.XOpenDisplay.restype = ctypes.c_void_p
        self._x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._x11.XCloseDisplay.restype = ctypes.c_int
        self._x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self._x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self._x11.XGrabKey.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        self._x11.XUngrabKey.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_ulong,
        ]
        self._x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._x11.XPending.argtypes = [ctypes.c_void_p]
        self._x11.XPending.restype = ctypes.c_int
        self._x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(XEvent)]
        self._x11.XConnectionNumber.argtypes = [ctypes.c_void_p]
        self._x11.XConnectionNumber.restype = ctypes.c_int
        self._x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
        self._x11.XSetErrorHandler.restype = ctypes.c_void_p

        self._display = self._x11.XOpenDisplay(None)
        if not self._display:
            raise RuntimeError(f"cannot open X display {os.environ.get('DISPLAY', '')!r}")
        self._root = self._x11.XDefaultRootWindow(self._display)
        self._active_keycode = 0
        self._last_error_code = 0

        error_handler_type = ctypes.CFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.POINTER(XErrorEvent),
        )

        def capture_error(
            _display: ctypes.c_void_p,
            event: ctypes.POINTER(XErrorEvent),
        ) -> int:
            self._last_error_code = int(event.contents.error_code)
            return 0

        self._error_handler = error_handler_type(capture_error)

    @staticmethod
    def eligible(spec: HotkeySpec) -> bool:
        return (
            spec.enabled
            and spec.sym in ALT_KEYSYMS
            and spec.states == 0
            and not spec.required_modifier_groups
        )

    def configure(self, spec: HotkeySpec) -> bool:
        if self.is_suppressing(spec):
            return True
        self.clear()
        if not self.eligible(spec):
            return False

        # XGrabKey reports conflicts asynchronously as BadAccess. Flush older
        # errors, install a short-lived handler, and synchronise before
        # deciding whether the fallback Escape path is still required.
        self._x11.XSync(self._display, 0)
        self._last_error_code = 0
        previous_handler = self._x11.XSetErrorHandler(
            ctypes.cast(self._error_handler, ctypes.c_void_p)
        )
        try:
            self._x11.XGrabKey(
                self._display,
                spec.primary_keycode,
                X11_ANY_MODIFIER,
                self._root,
                0,
                X11_GRAB_MODE_ASYNC,
                X11_GRAB_MODE_ASYNC,
            )
            self._x11.XSync(self._display, 0)
        finally:
            self._x11.XSetErrorHandler(previous_handler)

        if self._last_error_code:
            LOG.warning(
                "cannot suppress dictation keycode=%s X11 error=%s; using focus-recovery fallback",
                spec.primary_keycode,
                self._last_error_code,
            )
            return False
        self._active_keycode = spec.primary_keycode
        LOG.info("suppressing modifier-only Alt keycode=%s", self._active_keycode)
        return True

    def is_suppressing(self, spec: HotkeySpec) -> bool:
        return self.eligible(spec) and self._active_keycode == spec.primary_keycode

    def connection_fd(self) -> int:
        if not self._display:
            return -1
        return int(self._x11.XConnectionNumber(self._display))

    def drain_key_events(self) -> tuple[tuple[int, bool], ...]:
        edges: list[tuple[int, bool]] = []
        event = XEvent()
        while self._display and self._x11.XPending(self._display):
            self._x11.XNextEvent(self._display, ctypes.byref(event))
            event_type = int(event.xkey.type)
            keycode = int(event.xkey.keycode)
            if (
                event_type in {X11_KEY_PRESS, X11_KEY_RELEASE}
                and keycode == self._active_keycode
            ):
                edges.append((keycode, event_type == X11_KEY_PRESS))
        return tuple(edges)

    def clear(self) -> None:
        if not self._display or not self._active_keycode:
            return
        self._x11.XUngrabKey(
            self._display,
            self._active_keycode,
            X11_ANY_MODIFIER,
            self._root,
        )
        self._x11.XSync(self._display, 0)
        self._active_keycode = 0

    def close(self) -> None:
        if not self._display:
            return
        self.clear()
        self._x11.XCloseDisplay(self._display)
        self._display = None


class HotkeyState:
    def __init__(self, keymap: X11Keymap) -> None:
        self.keymap = keymap
        self.dictation = HotkeySpec()
        self.qa = HotkeySpec()
        self.translation = HotkeySpec()
        self.pressed: set[int] = set()
        self._pressed_changed = asyncio.Event()

    def update_pressed(self, keycode: int, is_press: bool) -> bool:
        """Update physical key state and wake insertion waiters.

        Returns whether the key was already pressed before this edge, which is
        used by the XInput monitor to suppress auto-repeat presses.
        """
        was_pressed = keycode in self.pressed
        if is_press:
            self.pressed.add(keycode)
        else:
            self.pressed.discard(keycode)
        self._pressed_changed.set()
        return was_pressed

    def reset_pressed(self) -> None:
        self.pressed.clear()
        self._pressed_changed.set()

    def dictation_input_keys_pressed(self) -> bool:
        """Whether any physical key belonging to the dictation binding is held."""
        spec = self.dictation
        if not spec.enabled:
            return False
        if spec.primary_keycode in self.pressed:
            return True
        return any(group & self.pressed for group in spec.required_modifier_groups)

    def dictation_primary_is_alt(self) -> bool:
        """Whether the modifier-only trigger can leave an app menu focused."""
        return X11DictationSuppressor.eligible(self.dictation)

    async def wait_for_dictation_input_keys_released(self) -> None:
        """Pause synthetic paste until the recording shortcut is fully released."""
        while self.dictation_input_keys_pressed():
            self._pressed_changed.clear()
            # Close the clear/check race: an edge between the loop condition
            # and Event.clear() either makes the binding safe now or leaves the
            # Event set for the await below.
            if not self.dictation_input_keys_pressed():
                break
            await self._pressed_changed.wait()

    def from_raw(self, sym: int, states: int) -> HotkeySpec:
        return HotkeySpec(
            sym=sym,
            states=states,
            primary_keycode=self.keymap.keysym_to_keycode(sym),
        )

    def from_string(self, value: str) -> HotkeySpec:
        parts = [part.strip() for part in value.split("+") if part.strip()]
        if not parts:
            return HotkeySpec()
        modifiers = parts[:-1]
        primary = parts[-1]
        keysym = self.keymap.name_to_keysym(primary)
        return HotkeySpec(
            sym=keysym,
            primary_keycode=self.keymap.keysym_to_keycode(keysym),
            required_modifier_groups=[
                group
                for modifier in modifiers
                if (group := self.keymap.modifier_keycodes(modifier))
            ],
        )


def dispatch_hotkey_edge(
    interface: object,
    keycode: int,
    is_press: bool,
    mode: str,
) -> bool:
    """Merge raw-XInput and passive-grab edges without double dispatch."""
    hotkeys = interface.hotkeys
    was_pressed = hotkeys.update_pressed(keycode, is_press)
    if was_pressed == is_press:
        return False

    if hotkeys.dictation.matches(keycode, hotkeys.pressed):
        spec = hotkeys.dictation
        for signal_is_press in dictation_signal_edges(mode, is_press):
            interface.DictationKeyEvent(spec.sym, spec.states, signal_is_press)
        if mode == "hold" and not is_press:
            schedule_cleanup = getattr(
                interface,
                "schedule_recording_release_cleanup",
                None,
            )
            if callable(schedule_cleanup):
                schedule_cleanup()
    if hotkeys.qa.matches(keycode, hotkeys.pressed):
        spec = hotkeys.qa
        interface.QaShortcutEvent(spec.sym, spec.states, is_press)
    if hotkeys.translation.matches(keycode, hotkeys.pressed):
        spec = hotkeys.translation
        interface.TranslationModifierEvent(spec.sym, spec.states, is_press)
    return True


class X11TextInserter:
    def insert(self, text: str, dismiss_alt_menu: bool = False) -> None:
        env = os.environ.copy()
        if dismiss_alt_menu:
            env["OPENLESS_DISMISS_ALT_MENU"] = "1"
        else:
            env.pop("OPENLESS_DISMISS_ALT_MENU", None)
        try:
            result = subprocess.run(
                ["/usr/bin/python3", str(X11_INSERTER_PATH)],
                input=text,
                text=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=185,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(type(exc).__name__) from exc
        if result.returncode != 0:
            raise RuntimeError(f"helper exited with status {result.returncode}")


class StatusOverlay:
    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.recording_active = False

    def _ensure_process(self) -> subprocess.Popen[str]:
        if self.process is not None and self.process.poll() is None:
            return self.process
        self.process = subprocess.Popen(
            ["/usr/bin/python3", str(STATUS_OVERLAY_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        return self.process

    def _send(self, payload: dict[str, object]) -> None:
        for attempt in range(2):
            process = self._ensure_process()
            try:
                if process.stdin is None:
                    raise BrokenPipeError("overlay stdin is unavailable")
                process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                process.stdin.flush()
                return
            except (BrokenPipeError, OSError):
                self.process = None
                if attempt:
                    raise

    def set_text(self, text: str) -> None:
        if "收音中" in text:
            self.recording_active = True
        elif not (self.recording_active and "已插入" in text):
            self.recording_active = False
        self._send({"op": "set", "text": text})

    def clear(self) -> None:
        self.recording_active = False
        if self.process is not None and self.process.poll() is None:
            self._send({"op": "clear"})

    def clear_if_recording(self) -> bool:
        if not self.recording_active:
            return False
        self.clear()
        return True

    def close(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            self._send({"op": "exit"})
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self.process.terminate()


class HistoryRefresher:
    def refresh(self) -> None:
        for attempt in range(2):
            try:
                result = subprocess.run(
                    ["/usr/bin/python3", str(HISTORY_REFRESH_PATH)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                result = None
            if result is not None and result.returncode == 0:
                return
            if not attempt:
                time.sleep(0.12)


class OpenLessInterface(ServiceInterface):
    def __init__(
        self,
        hotkeys: HotkeyState | None,
        text_inserter: object | None = None,
        status_overlay: object | None = None,
        history_refresher: object | None = None,
        dictation_suppressor: object | None = None,
        recording_release_cleanup_delay: float = 1.0,
    ) -> None:
        super().__init__(OPENLESS_IFACE)
        self.hotkeys = hotkeys
        self.text_inserter = text_inserter or X11TextInserter()
        self.status_overlay = status_overlay or StatusOverlay()
        self.history_refresher = history_refresher or HistoryRefresher()
        self.dictation_suppressor = dictation_suppressor
        self.recording_release_cleanup_delay = recording_release_cleanup_delay
        self._recording_release_cleanup: asyncio.TimerHandle | None = None

    def cancel_recording_release_cleanup(self) -> None:
        cleanup = self._recording_release_cleanup
        self._recording_release_cleanup = None
        if cleanup is not None:
            cleanup.cancel()

    def schedule_recording_release_cleanup(self) -> None:
        self.cancel_recording_release_cleanup()
        loop = asyncio.get_running_loop()
        self._recording_release_cleanup = loop.call_later(
            self.recording_release_cleanup_delay,
            self.clear_stale_recording_status,
        )

    def clear_stale_recording_status(self) -> None:
        self._recording_release_cleanup = None
        try:
            if self.status_overlay.clear_if_recording():
                LOG.warning(
                    "cleared stale recording overlay after dictation-key release"
                )
        except Exception as exc:
            LOG.warning(
                "stale recording overlay cleanup failed: %s",
                type(exc).__name__,
            )

    def configure_dictation_suppression(self) -> bool:
        if self.hotkeys is None or self.dictation_suppressor is None:
            return False
        try:
            return bool(self.dictation_suppressor.configure(self.hotkeys.dictation))
        except Exception as exc:
            LOG.warning("dictation-key suppression failed: %s", type(exc).__name__)
            return False

    def dictation_alt_is_suppressed(self) -> bool:
        if self.hotkeys is None or self.dictation_suppressor is None:
            return False
        try:
            return bool(
                self.dictation_suppressor.is_suppressing(self.hotkeys.dictation)
            )
        except Exception:
            return False

    async def commit_text_when_safe(self, text: str) -> None:
        dismiss_alt_menu = (
            self.hotkeys is not None
            and self.hotkeys.dictation_primary_is_alt()
            and not self.dictation_alt_is_suppressed()
        )
        if self.hotkeys is not None and self.hotkeys.dictation_input_keys_pressed():
            LOG.info("deferring text insertion until dictation hotkey is fully released")
            await self.hotkeys.wait_for_dictation_input_keys_released()
        # The helper owns the clipboard for about one second. Run it outside
        # the event-loop thread so RawKeyRelease is consumed promptly;
        # otherwise the release needed to unblock insertion would deadlock
        # behind subprocess.run().
        await asyncio.to_thread(self.text_inserter.insert, text, dismiss_alt_menu)

    @method()
    async def CommitText(self, _text: "s") -> "":
        try:
            await self.commit_text_when_safe(_text)
        except Exception as exc:
            raise DBusError(
                "org.openless.Fcitx4Bridge.InsertionFailed",
                f"X11 focused-window insertion failed: {type(exc).__name__}",
            ) from exc
        return None

    @method()
    def SetAuxDown(self, _text: "s") -> "":
        try:
            self.status_overlay.set_text(_text)
            if not bool(getattr(self.status_overlay, "recording_active", False)):
                self.cancel_recording_release_cleanup()
        except Exception as exc:
            LOG.warning("status overlay update failed: %s", type(exc).__name__)
        if "已插入" in _text:
            try:
                self.history_refresher.refresh()
            except Exception as exc:
                LOG.warning("history auto-refresh failed: %s", type(exc).__name__)
        return None

    @method()
    def ClearAuxDown(self) -> "":
        self.cancel_recording_release_cleanup()
        try:
            self.status_overlay.clear()
        except Exception as exc:
            LOG.warning("status overlay clear failed: %s", type(exc).__name__)
        return None

    @method()
    def SetHotkey(self, keys: "as") -> "":
        value = keys[0] if keys else ""
        self.hotkeys.dictation = self.hotkeys.from_string(value)
        self.configure_dictation_suppression()
        LOG.info("dictation hotkey string=%s keycode=%s", value, self.hotkeys.dictation.primary_keycode)
        return None

    @method()
    def SetHotkeyRaw(self, sym: "u", states: "u") -> "":
        self.hotkeys.dictation = self.hotkeys.from_raw(sym, states)
        self.configure_dictation_suppression()
        LOG.info("dictation hotkey sym=%#x keycode=%s", sym, self.hotkeys.dictation.primary_keycode)
        return None

    @method()
    def SetCustomDictationTrigger(self, key_string: "s") -> "":
        self.hotkeys.dictation = self.hotkeys.from_string(key_string)
        self.configure_dictation_suppression()
        LOG.info(
            "custom dictation hotkey=%s keycode=%s",
            key_string,
            self.hotkeys.dictation.primary_keycode,
        )
        return None

    @method()
    def SetQaHotkeyRaw(self, sym: "u", states: "u") -> "":
        self.hotkeys.qa = self.hotkeys.from_raw(sym, states)
        return None

    @method()
    def SetTranslationHotkeyRaw(self, sym: "u", states: "u") -> "":
        self.hotkeys.translation = self.hotkeys.from_raw(sym, states)
        return None

    @dbus_signal()
    def DictationKeyEvent(self, sym: "u", states: "u", is_press: "b") -> "uub":
        return [sym, states, is_press]

    @dbus_signal()
    def QaShortcutEvent(self, sym: "u", states: "u", is_press: "b") -> "uub":
        return [sym, states, is_press]

    @dbus_signal()
    def TranslationModifierEvent(self, sym: "u", states: "u", is_press: "b") -> "uub":
        return [sym, states, is_press]


EVENT_RE = re.compile(r"EVENT type \d+ \((RawKeyPress|RawKeyRelease)\)")
DETAIL_RE = re.compile(r"\s*detail:\s*(\d+)")


async def monitor_xinput(interface: OpenLessInterface) -> None:
    hotkeys = interface.hotkeys
    while True:
        # A listener restart loses the previous stream's release edges. Clear
        # stale state so insertion waiters cannot remain blocked forever; the
        # X11 helper independently re-checks the real modifier mask immediately
        # before synthesizing Ctrl+V.
        hotkeys.reset_pressed()
        process = await asyncio.create_subprocess_exec(
            "stdbuf",
            "-oL",
            "xinput",
            "test-xi2",
            "--root",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        current_event: str | None = None
        assert process.stdout is not None
        while line_bytes := await process.stdout.readline():
            line = line_bytes.decode("utf-8", errors="replace")
            event_match = EVENT_RE.search(line)
            if event_match:
                current_event = event_match.group(1)
                continue
            detail_match = DETAIL_RE.match(line)
            if not detail_match or current_event is None:
                continue
            keycode = int(detail_match.group(1))
            is_press = current_event == "RawKeyPress"
            current_event = None

            dispatch_hotkey_edge(
                interface,
                keycode,
                is_press,
                mode=read_dictation_mode(),
            )

        stderr = b""
        if process.stderr is not None:
            stderr = await process.stderr.read()
        LOG.warning(
            "xinput listener exited with %s: %s; restarting",
            await process.wait(),
            stderr.decode("utf-8", errors="replace").strip(),
        )
        await asyncio.sleep(1)


async def monitor_suppressed_key_events(
    interface: OpenLessInterface,
    suppressor: X11DictationSuppressor,
) -> None:
    """Consume KeyRelease from the same X11 connection that owns XGrabKey."""
    connection_fd = suppressor.connection_fd()
    if connection_fd < 0:
        raise RuntimeError("X11 suppression connection is unavailable")

    loop = asyncio.get_running_loop()
    readable = asyncio.Event()
    loop.add_reader(connection_fd, readable.set)
    try:
        while True:
            await readable.wait()
            readable.clear()
            for keycode, is_press in suppressor.drain_key_events():
                dispatch_hotkey_edge(
                    interface,
                    keycode,
                    is_press,
                    mode=read_dictation_mode(),
                )
    finally:
        loop.remove_reader(connection_fd)


async def main() -> None:
    logging.basicConfig(level=logging.CRITICAL)
    for root_handler in logging.getLogger().handlers:
        root_handler.setLevel(logging.CRITICAL)
    bridge_handler = logging.StreamHandler()
    bridge_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    LOG.addHandler(bridge_handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    keymap = X11Keymap()
    hotkeys = HotkeyState(keymap)
    status_overlay = StatusOverlay()
    try:
        dictation_suppressor: X11DictationSuppressor | None = X11DictationSuppressor()
    except Exception as exc:
        LOG.warning("X11 dictation-key suppression unavailable: %s", type(exc).__name__)
        dictation_suppressor = None
    openless = OpenLessInterface(
        hotkeys,
        status_overlay=status_overlay,
        dictation_suppressor=dictation_suppressor,
    )
    bus = await MessageBus().connect()
    bus.export(OBJECT_PATH, openless)
    await bus.request_name(BUS_NAME)
    LOG.info("bridge ready on %s%s", BUS_NAME, OBJECT_PATH)

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: not stop.done() and stop.set_result(None))

    monitors = [asyncio.create_task(monitor_xinput(openless))]
    if dictation_suppressor is not None:
        monitors.append(
            asyncio.create_task(
                monitor_suppressed_key_events(openless, dictation_suppressor)
            )
        )
    try:
        await stop
    finally:
        for monitor in monitors:
            monitor.cancel()
        for monitor in monitors:
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
        status_overlay.close()
        if dictation_suppressor is not None:
            dictation_suppressor.close()
        bus.disconnect()
        keymap.close()


if __name__ == "__main__":
    asyncio.run(main())
