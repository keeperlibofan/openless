#!/usr/bin/env python3
"""Polished, focus-safe OpenLess status capsule for the X11 desktop.

While recording, a short-lived ``parec`` process reads PCM frames from the
default microphone. Frames are reduced immediately to RMS and all-zero signal
statistics; no audio samples are retained or written.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from array import array
from collections import deque
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Monitor:
    x: int
    y: int
    width: int
    height: int
    primary: bool = False


@dataclass(frozen=True)
class StatusStyle:
    kind: str
    label: str
    subtitle: str
    color: str


@dataclass(frozen=True)
class MicrophoneSnapshot:
    stream_started: bool
    stream_failed: bool
    bytes_seen: int
    nonzero_samples: int
    peak_db: float


@dataclass(frozen=True)
class RoundedShapePrimitives:
    rectangles: tuple[tuple[int, int, int, int], ...]
    arcs: tuple[tuple[int, int, int, int], ...]


@dataclass(frozen=True)
class ScreenRect:
    x: int
    y: int
    width: int
    height: int


POSITION_PATH = Path(
    os.environ.get(
        "OPENLESS_OVERLAY_POSITION_PATH",
        str(Path.home() / ".local/share/OpenLess/overlay-position.json"),
    )
)


def monitor_for_point(monitors: list[Monitor], x: int, y: int) -> Monitor:
    """Return the monitor containing a point, falling back to the primary."""
    if not monitors:
        return Monitor(0, 0, 1920, 1080, True)
    return next(
        (
            monitor
            for monitor in monitors
            if monitor.x <= x < monitor.x + monitor.width
            and monitor.y <= y < monitor.y + monitor.height
        ),
        next((monitor for monitor in monitors if monitor.primary), monitors[0]),
    )


def clamp_overlay_position(
    x: int,
    y: int,
    monitor: Monitor,
    width: int,
    height: int,
    margin: int = 8,
) -> tuple[int, int]:
    """Keep the complete overlay visible inside one monitor."""
    min_x = monitor.x + margin
    min_y = monitor.y + margin
    max_x = max(min_x, monitor.x + monitor.width - width - margin)
    max_y = max(min_y, monitor.y + monitor.height - height - margin)
    return max(min_x, min(int(x), max_x)), max(min_y, min(int(y), max_y))


def position_near_anchor(
    anchor: ScreenRect,
    monitors: list[Monitor],
    width: int,
    height: int,
    gap: int = 14,
    margin: int = 8,
) -> tuple[int, int]:
    """Place the overlay above-left of a caret/pointer without covering it."""
    monitor = monitor_for_point(monitors, anchor.x, anchor.y)
    x = anchor.x - width - gap
    y = anchor.y - height - gap

    # Near the left/top edge, flip to the other side of the anchor rather than
    # pinning a large part of the capsule against the screen edge.
    if x < monitor.x + margin:
        x = anchor.x + max(1, anchor.width) + gap
    if y < monitor.y + margin:
        y = anchor.y + max(1, anchor.height) + gap
    return clamp_overlay_position(x, y, monitor, width, height, margin)


def load_saved_position(path: Path = POSITION_PATH) -> tuple[int, int] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        x = payload.get("x")
        y = payload.get("y")
        if isinstance(x, int) and isinstance(y, int):
            return x, y
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return None


def save_position(x: int, y: int, path: Path = POSITION_PATH) -> None:
    """Atomically persist the user's manually selected overlay position."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"x": int(x), "y": int(y)}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def clear_saved_position(path: Path = POSITION_PATH) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def accessible_caret_rect(max_nodes: int = 420) -> ScreenRect | None:
    """Best-effort AT-SPI caret lookup without reading the field's text.

    Some Electron/Chromium applications expose the focused editable object and
    caret rectangle; others expose no usable text interface. Every failure is
    intentionally silent so positioning can fall back to the mouse pointer.
    """
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        if hasattr(Atspi, "set_timeout"):
            Atspi.set_timeout(120, 300)
        desktop = Atspi.get_desktop(0)
        active_roots: list[object] = []
        for app_index in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(app_index)
            if app is None:
                continue
            for window_index in range(min(app.get_child_count(), 80)):
                window = app.get_child_at_index(window_index)
                if window is None:
                    continue
                states = window.get_state_set()
                if states.contains(Atspi.StateType.ACTIVE) or states.contains(
                    Atspi.StateType.FOCUSED
                ):
                    active_roots.append(window)

        stack = list(reversed(active_roots))
        visited = 0
        while stack and visited < max_nodes:
            accessible = stack.pop()
            visited += 1
            try:
                states = accessible.get_state_set()
                focused = states.contains(Atspi.StateType.FOCUSED)
                if focused:
                    text = accessible.get_text_iface()
                    if text is not None:
                        offset = max(0, text.get_caret_offset())
                        count = max(0, text.get_character_count())
                        if count > 0:
                            probe = min(offset, count - 1)
                            rect = text.get_character_extents(
                                probe,
                                Atspi.CoordType.SCREEN,
                            )
                            x = int(rect.x)
                            if offset >= count:
                                x += max(1, int(rect.width))
                            result = ScreenRect(
                                x=x,
                                y=int(rect.y),
                                width=max(1, int(rect.width)),
                                height=max(1, int(rect.height)),
                            )
                            if result.x >= 0 and result.y >= 0:
                                return result

                        component = accessible.get_component_iface()
                        if component is not None:
                            rect = component.get_extents(Atspi.CoordType.SCREEN)
                            result = ScreenRect(
                                x=int(rect.x) + 8,
                                y=int(rect.y),
                                width=1,
                                height=max(1, int(rect.height)),
                            )
                            if result.x >= 0 and result.y >= 0:
                                return result

                child_count = min(accessible.get_child_count(), 180)
                children: list[tuple[bool, object]] = []
                for child_index in range(child_count):
                    child = accessible.get_child_at_index(child_index)
                    if child is None:
                        continue
                    try:
                        child_focused = child.get_state_set().contains(
                            Atspi.StateType.FOCUSED
                        )
                    except Exception:
                        child_focused = False
                    children.append((child_focused, child))
                # Focused branches are popped first; the cap prevents a very
                # large browser accessibility tree from delaying the overlay.
                children.sort(key=lambda item: item[0])
                stack.extend(child for _, child in children)
            except Exception:
                continue
    except Exception:
        return None
    return None


def rounded_shape_primitives(
    width: int,
    height: int,
    radius: int,
) -> RoundedShapePrimitives:
    """Return the X11 mask geometry for a four-corner rounded rectangle."""
    safe_width = max(1, int(width))
    safe_height = max(1, int(height))
    safe_radius = max(0, min(int(radius), safe_width // 2, safe_height // 2))
    if safe_radius == 0:
        return RoundedShapePrimitives(
            rectangles=((0, 0, safe_width, safe_height),),
            arcs=(),
        )

    diameter = safe_radius * 2
    right = safe_width - diameter
    bottom = safe_height - diameter
    return RoundedShapePrimitives(
        rectangles=(
            (safe_radius, 0, safe_width - diameter, safe_height),
            (0, safe_radius, safe_width, safe_height - diameter),
        ),
        arcs=(
            (0, 0, diameter, diameter),
            (right, 0, diameter, diameter),
            (0, bottom, diameter, diameter),
            (right, bottom, diameter, diameter),
        ),
    )


def apply_x11_rounded_shape(
    window_id: int,
    width: int,
    height: int,
    radius: int,
) -> bool:
    """Clip an X11 window to a rounded rectangle using the SHAPE extension."""
    x11_name = ctypes.util.find_library("X11")
    xext_name = ctypes.util.find_library("Xext")
    if not x11_name or not xext_name:
        return False

    try:
        x11 = ctypes.CDLL(x11_name)
        xext = ctypes.CDLL(xext_name)
    except OSError:
        return False

    display_pointer = ctypes.c_void_p
    drawable = ctypes.c_ulong
    graphics_context = ctypes.c_void_p

    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = display_pointer
    x11.XCloseDisplay.argtypes = [display_pointer]
    x11.XCreatePixmap.argtypes = [
        display_pointer,
        drawable,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_uint,
    ]
    x11.XCreatePixmap.restype = drawable
    x11.XFreePixmap.argtypes = [display_pointer, drawable]
    x11.XCreateGC.argtypes = [display_pointer, drawable, ctypes.c_ulong, ctypes.c_void_p]
    x11.XCreateGC.restype = graphics_context
    x11.XFreeGC.argtypes = [display_pointer, graphics_context]
    x11.XSetForeground.argtypes = [display_pointer, graphics_context, ctypes.c_ulong]
    x11.XFillRectangle.argtypes = [
        display_pointer,
        drawable,
        graphics_context,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
    ]
    x11.XFillArc.argtypes = [
        display_pointer,
        drawable,
        graphics_context,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_int,
    ]
    x11.XQueryTree.argtypes = [
        display_pointer,
        drawable,
        ctypes.POINTER(drawable),
        ctypes.POINTER(drawable),
        ctypes.POINTER(ctypes.POINTER(drawable)),
        ctypes.POINTER(ctypes.c_uint),
    ]
    x11.XQueryTree.restype = ctypes.c_int
    x11.XFree.argtypes = [ctypes.c_void_p]
    x11.XSync.argtypes = [display_pointer, ctypes.c_int]
    xext.XShapeCombineMask.argtypes = [
        display_pointer,
        drawable,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        drawable,
        ctypes.c_int,
    ]

    display = x11.XOpenDisplay(None)
    if not display:
        return False

    mask = drawable(0)
    gc = graphics_context()
    try:
        mask = x11.XCreatePixmap(display, window_id, width, height, 1)
        if not mask:
            return False
        gc = x11.XCreateGC(display, mask, 0, None)
        if not gc:
            return False

        x11.XSetForeground(display, gc, 0)
        x11.XFillRectangle(display, mask, gc, 0, 0, width, height)
        x11.XSetForeground(display, gc, 1)
        primitives = rounded_shape_primitives(width, height, radius)
        for x, y, item_width, item_height in primitives.rectangles:
            if item_width > 0 and item_height > 0:
                x11.XFillRectangle(
                    display,
                    mask,
                    gc,
                    x,
                    y,
                    item_width,
                    item_height,
                )
        for x, y, item_width, item_height in primitives.arcs:
            x11.XFillArc(
                display,
                mask,
                gc,
                x,
                y,
                item_width,
                item_height,
                0,
                360 * 64,
            )

        # Tk uses an outer wrapper around the widget window on X11. Shape both
        # layers so the wrapper cannot leave square pixels around the canvas.
        targets = [window_id]
        root_window = drawable()
        parent_window = drawable()
        children = ctypes.POINTER(drawable)()
        child_count = ctypes.c_uint()
        queried = x11.XQueryTree(
            display,
            window_id,
            ctypes.byref(root_window),
            ctypes.byref(parent_window),
            ctypes.byref(children),
            ctypes.byref(child_count),
        )
        if children:
            x11.XFree(children)
        if (
            queried
            and parent_window.value
            and parent_window.value != root_window.value
        ):
            targets.append(parent_window.value)

        # ShapeBounding=0 and ShapeSet=0.
        for target in targets:
            xext.XShapeCombineMask(display, target, 0, 0, 0, mask, 0)
        x11.XSync(display, 0)
        return True
    finally:
        if gc:
            x11.XFreeGC(display, gc)
        if mask:
            x11.XFreePixmap(display, mask)
        x11.XCloseDisplay(display)


def db_to_level(db_value: float) -> float:
    """Map microphone RMS dB to a perceptually useful 0..1 meter value."""
    if not math.isfinite(db_value):
        return 0.0
    normalized = max(0.0, min(1.0, (db_value + 60.0) / 50.0))
    return normalized**0.65


def classify_status(text: str) -> StatusStyle:
    if "收音" in text or "录音" in text:
        return StatusStyle("recording", "正在录音", "松开右 Alt 结束", "#ff5d68")
    if "识别" in text:
        return StatusStyle("recognizing", "正在识别", "阿里百炼正在转写", "#5da9ff")
    if "润色" in text:
        return StatusStyle("polishing", "正在润色", "DeepSeek 正在整理文字", "#b98cff")
    if "已插入" in text or "完成" in text:
        return StatusStyle("success", "已完成", "文字已插入当前光标", "#48d597")
    if "出错" in text or "失败" in text:
        return StatusStyle("error", "处理失败", "请稍后重试", "#ff6b6b")
    compact = re.sub(r"[.…]{2,}", "", text).strip()
    return StatusStyle("info", compact[:18] or "OpenLess", "语音输入", "#8aa4c7")


def classify_recording_error(snapshot: MicrophoneSnapshot) -> StatusStyle:
    """Distinguish disconnected input, connected silence, and later failures."""
    if snapshot.stream_failed or (
        snapshot.stream_started
        and (snapshot.bytes_seen == 0 or snapshot.nonzero_samples == 0)
    ):
        return StatusStyle(
            "error",
            "麦克风未连接",
            "请检查麦克风或发射器",
            "#ff6b6b",
        )
    if snapshot.stream_started and snapshot.peak_db < -50.0:
        return StatusStyle(
            "error",
            "未检测到语音",
            "请按住右 Alt 后说话",
            "#ffb15f",
        )
    return StatusStyle("error", "处理失败", "请稍后重试", "#ff6b6b")


def should_replace_visible_status(previous_kind: str, next_kind: str) -> bool:
    """Keep active recording feedback above completion from an older job."""
    return not (previous_kind == "recording" and next_kind == "success")


class MicrophoneLevelMeter:
    """Read PCM levels and connection evidence without retaining audio."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.reader_thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.generation = 0
        self.active = False
        self.stream_started = False
        self.stream_failed = False
        self.bytes_seen = 0
        self.nonzero_samples = 0
        self.peak_db = float("-inf")
        self.latest_db: float | None = None

    def start(self) -> None:
        self.stop()
        with self.lock:
            self.generation += 1
            generation = self.generation
            self.active = True
            self.stream_started = False
            self.stream_failed = False
            self.bytes_seen = 0
            self.nonzero_samples = 0
            self.peak_db = float("-inf")
            self.latest_db = None
        try:
            process = subprocess.Popen(
                [
                    "parec",
                    f"--server=/run/user/{os.getuid()}/pulse/native",
                    "--raw",
                    "--format=s16le",
                    "--rate=16000",
                    "--channels=1",
                    "--latency-msec=50",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            with self.lock:
                if generation == self.generation:
                    self.active = False
                    self.stream_failed = True
            return

        with self.lock:
            if generation != self.generation:
                process.terminate()
                return
            self.process = process
            self.stream_started = True
        self.reader_thread = threading.Thread(
            target=self._read_pcm,
            args=(process, generation),
            daemon=True,
        )
        self.reader_thread.start()

    def stop(self) -> None:
        with self.lock:
            self.active = False
            process = self.process
            self.process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.35)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.2)
        if process.stdout is not None:
            process.stdout.close()

    def _read_pcm(self, process: subprocess.Popen[bytes], generation: int) -> None:
        stream = process.stdout
        if stream is None:
            with self.lock:
                if generation == self.generation:
                    self.stream_failed = True
            return
        try:
            while True:
                chunk = stream.read(1600)
                if not chunk:
                    break
                usable = chunk[: len(chunk) - (len(chunk) % 2)]
                if not usable:
                    continue
                samples = array("h")
                samples.frombytes(usable)
                if sys.byteorder != "little":
                    samples.byteswap()
                nonzero = sum(1 for sample in samples if sample != 0)
                sum_squares = sum(sample * sample for sample in samples)
                if sum_squares:
                    rms = math.sqrt(sum_squares / len(samples)) / 32768.0
                    db_value = 20.0 * math.log10(rms)
                else:
                    db_value = float("-inf")
                with self.lock:
                    if generation != self.generation:
                        return
                    self.bytes_seen += len(usable)
                    self.nonzero_samples += nonzero
                    self.latest_db = db_value
                    if db_value > self.peak_db:
                        self.peak_db = db_value
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                if generation == self.generation and self.active:
                    self.stream_failed = True

    def poll_db(self) -> float | None:
        with self.lock:
            return self.latest_db

    def snapshot(self) -> MicrophoneSnapshot:
        with self.lock:
            return MicrophoneSnapshot(
                stream_started=self.stream_started,
                stream_failed=self.stream_failed,
                bytes_seen=self.bytes_seen,
                nonzero_samples=self.nonzero_samples,
                peak_db=self.peak_db,
            )


class StatusOverlayApp:
    WIDTH = 332
    HEIGHT = 92
    WINDOW_RADIUS = 22
    BOTTOM_MARGIN = 68
    BAR_COUNT = 28

    def __init__(self) -> None:
        self.root = tk.Tk(className="OpenLessStatus")
        self.root.title("OpenLess Status")
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.configure(background="#0c111a")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.96)
        try:
            self.root.attributes("-type", "notification")
        except tk.TclError:
            pass

        self.canvas = tk.Canvas(
            self.root,
            width=self.WIDTH,
            height=self.HEIGHT,
            background="#0c111a",
            highlightthickness=0,
            takefocus=False,
            cursor="fleur",
        )
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._begin_drag)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._end_drag)
        self.canvas.bind("<Button-3>", self._reset_position)
        self.messages: queue.Queue[dict[str, object]] = queue.Queue()
        self.style = classify_status("")
        self.frame_index = 0
        self.generation = 0
        self.shaped_window_id: int | None = None
        self.manual_position = load_saved_position()
        self.drag_offset: tuple[int, int] | None = None
        self.meter = MicrophoneLevelMeter()
        self.smoothed_level = 0.0
        self.level_history: deque[float] = deque(
            [0.0] * self.BAR_COUNT,
            maxlen=self.BAR_COUNT,
        )

        threading.Thread(target=self._read_stdin, daemon=True).start()
        self.root.after(30, self._poll_messages)
        self.root.after(50, self._animate)

    def _read_stdin(self) -> None:
        try:
            for line in sys.stdin:
                try:
                    message = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(message, dict):
                    self.messages.put(message)
        finally:
            self.messages.put({"op": "exit"})

    def _poll_messages(self) -> None:
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            operation = message.get("op")
            if operation == "set":
                self.show(str(message.get("text", "")))
            elif operation == "clear":
                self.clear()
            elif operation == "exit":
                self.meter.stop()
                self.root.destroy()
                return
        self.root.after(30, self._poll_messages)

    def show(self, text: str) -> None:
        was_hidden = self.root.state() == "withdrawn"
        previous_kind = self.style.kind
        next_style = classify_status(text)
        if next_style.kind == "error":
            next_style = classify_recording_error(self.meter.snapshot())
        if not should_replace_visible_status(previous_kind, next_style.kind):
            return
        self.style = next_style
        self.frame_index = 0
        self.generation += 1
        generation = self.generation

        if self.style.kind == "recording":
            if previous_kind != "recording":
                self.smoothed_level = 0.0
                self.level_history = deque([0.0] * self.BAR_COUNT, maxlen=self.BAR_COUNT)
                self.meter.start()
        else:
            self.meter.stop()

        if was_hidden:
            self._position_for_show()
        self._draw()
        self.root.deiconify()
        self.root.update_idletasks()
        self._apply_window_shape()
        self.root.lift()
        if self.style.kind in {"success", "error"}:
            self.root.after(2800, lambda: self._clear_if_current(generation))

    def clear(self) -> None:
        self.generation += 1
        self.meter.stop()
        self.root.withdraw()

    def _apply_window_shape(self) -> None:
        try:
            frame_id = int(str(self.root.tk.call("wm", "frame", self.root._w)), 0)
        except (tk.TclError, TypeError, ValueError):
            return
        if frame_id == self.shaped_window_id:
            return
        if apply_x11_rounded_shape(
            frame_id,
            self.WIDTH,
            self.HEIGHT,
            self.WINDOW_RADIUS,
        ):
            self.shaped_window_id = frame_id

    def _clear_if_current(self, generation: int) -> None:
        if generation == self.generation:
            self.clear()

    def _rounded_rectangle(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        radius: float,
        **kwargs: object,
    ) -> int:
        points = (
            x1 + radius,
            y1,
            x2 - radius,
            y1,
            x2,
            y1,
            x2,
            y1 + radius,
            x2,
            y2 - radius,
            x2,
            y2,
            x2 - radius,
            y2,
            x1 + radius,
            y2,
            x1,
            y2,
            x1,
            y2 - radius,
            x1,
            y1 + radius,
            x1,
            y1,
        )
        return self.canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)

    def _draw(self) -> None:
        self.canvas.delete("all")
        self._rounded_rectangle(
            3,
            4,
            self.WIDTH - 3,
            self.HEIGHT - 3,
            20,
            fill="#111824",
            outline="#344158",
            width=1,
        )
        self._rounded_rectangle(
            4,
            5,
            9,
            self.HEIGHT - 4,
            3,
            fill=self.style.color,
            outline=self.style.color,
        )
        self._draw_state_icon()
        self.canvas.create_text(
            62,
            25,
            anchor="w",
            text=self.style.label,
            fill="#f5f8fc",
            font=("Noto Sans CJK SC", 12, "bold"),
        )
        self.canvas.create_text(
            62,
            47,
            anchor="w",
            text=self.style.subtitle,
            fill="#93a0b5",
            font=("Noto Sans CJK SC", 9),
        )
        if self.style.kind == "recording":
            self.canvas.create_text(
                self.WIDTH - 18,
                25,
                anchor="e",
                text=f"{round(self.smoothed_level * 100):02d}%",
                fill="#ff9ca4",
                font=("DejaVu Sans Mono", 9, "bold"),
            )
            self._draw_live_waveform()
        elif self.style.kind in {"recognizing", "polishing"}:
            self._draw_processing_wave()
        else:
            self._draw_completion_line()
        self._draw_drag_handle()

    def _draw_drag_handle(self) -> None:
        """A quiet visual affordance showing that the capsule is draggable."""
        for x in (self.WIDTH - 22, self.WIDTH - 16):
            for y in (43, 49, 55):
                self.canvas.create_oval(
                    x - 1,
                    y - 1,
                    x + 1,
                    y + 1,
                    fill="#5f6c82",
                    outline="#5f6c82",
                )

    def _draw_state_icon(self) -> None:
        color = self.style.color
        if self.style.kind == "recording":
            pulse = 1 + (self.frame_index % 12) / 18
            self.canvas.create_oval(
                23 - pulse,
                14 - pulse,
                45 + pulse,
                44 + pulse,
                outline="#52252d",
                width=2,
            )
            self._rounded_rectangle(29, 15, 39, 34, 5, outline=color, width=2, fill="")
            self.canvas.create_arc(25, 24, 43, 42, start=180, extent=180, outline=color, width=2, style=tk.ARC)
            self.canvas.create_line(34, 42, 34, 47, fill=color, width=2)
            self.canvas.create_line(29, 47, 39, 47, fill=color, width=2)
        elif self.style.kind in {"recognizing", "polishing"}:
            start = (self.frame_index * 24) % 360
            self.canvas.create_arc(24, 16, 44, 36, start=start, extent=250, outline=color, width=3, style=tk.ARC)
            self.canvas.create_oval(32, 24, 36, 28, fill=color, outline=color)
        elif self.style.kind == "success":
            self.canvas.create_oval(24, 16, 44, 36, outline=color, width=2)
            self.canvas.create_line(28, 26, 32, 30, 40, 21, fill=color, width=3, capstyle=tk.ROUND, joinstyle=tk.ROUND)
        elif self.style.kind == "error":
            self.canvas.create_oval(24, 16, 44, 36, outline=color, width=2)
            self.canvas.create_line(34, 21, 34, 28, fill=color, width=2)
            self.canvas.create_oval(33, 31, 35, 33, fill=color, outline=color)
        else:
            self.canvas.create_oval(27, 19, 41, 33, fill=color, outline=color)

    def _draw_live_waveform(self) -> None:
        left = 18
        right = self.WIDTH - 18
        center_y = 73
        gap = 3
        bar_width = (right - left - gap * (self.BAR_COUNT - 1)) / self.BAR_COUNT
        for index, level in enumerate(self.level_history):
            height = 2.5 + 17.5 * level
            x1 = left + index * (bar_width + gap)
            x2 = x1 + bar_width
            if level > 0.82:
                color = "#ffbd66"
            elif level > 0.55:
                color = "#ff7b73"
            else:
                color = "#ff5d68"
            self.canvas.create_line(
                (x1 + x2) / 2,
                center_y - height / 2,
                (x1 + x2) / 2,
                center_y + height / 2,
                fill=color,
                width=max(2, round(bar_width)),
                capstyle=tk.ROUND,
            )

    def _draw_processing_wave(self) -> None:
        left = 62
        right = self.WIDTH - 20
        y = 73
        count = 20
        for index in range(count):
            phase = (index * 0.65) + (self.frame_index * 0.38)
            amplitude = (math.sin(phase) + 1) / 2
            height = 2 + amplitude * 10
            x = left + index * ((right - left) / (count - 1))
            self.canvas.create_line(
                x,
                y - height / 2,
                x,
                y + height / 2,
                fill=self.style.color,
                width=4,
                capstyle=tk.ROUND,
            )

    def _draw_completion_line(self) -> None:
        self._rounded_rectangle(
            62,
            69,
            self.WIDTH - 20,
            76,
            3,
            fill="#263244",
            outline="#263244",
        )
        fill_width = self.WIDTH - 82 if self.style.kind == "success" else 72
        self._rounded_rectangle(
            62,
            69,
            62 + fill_width,
            76,
            3,
            fill=self.style.color,
            outline=self.style.color,
        )

    def _animate(self) -> None:
        if self.root.state() != "withdrawn":
            self.frame_index += 1
            if self.style.kind == "recording":
                db_value = self.meter.poll_db()
                target = db_to_level(db_value) if db_value is not None else 0.0
                rate = 0.58 if target > self.smoothed_level else 0.14
                self.smoothed_level += (target - self.smoothed_level) * rate
                self.level_history.append(self.smoothed_level)
                self._draw()
            elif self.style.kind in {"recognizing", "polishing"}:
                self._draw()
        self.root.after(50, self._animate)

    def _monitors(self) -> list[Monitor]:
        try:
            output = subprocess.check_output(
                ["xrandr", "--listmonitors"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return [Monitor(0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight(), True)]
        monitors: list[Monitor] = []
        geometry_pattern = re.compile(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)")
        for line in output.splitlines()[1:]:
            match = geometry_pattern.search(line)
            if not match:
                continue
            width, height, x, y = (int(value) for value in match.groups())
            monitors.append(Monitor(x, y, width, height, "*" in line.split()[1]))
        return monitors or [Monitor(0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight(), True)]

    def _position_for_show(self) -> None:
        monitors = self._monitors()
        if self.manual_position is not None:
            saved_x, saved_y = self.manual_position
            monitor = monitor_for_point(
                monitors,
                saved_x + self.WIDTH // 2,
                saved_y + self.HEIGHT // 2,
            )
            x, y = clamp_overlay_position(
                saved_x,
                saved_y,
                monitor,
                self.WIDTH,
                self.HEIGHT,
            )
            self.manual_position = (x, y)
        else:
            anchor = accessible_caret_rect()
            if anchor is None:
                anchor = ScreenRect(
                    x=self.root.winfo_pointerx(),
                    y=self.root.winfo_pointery(),
                    width=1,
                    height=1,
                )
            x, y = position_near_anchor(
                anchor,
                monitors,
                self.WIDTH,
                self.HEIGHT,
            )
        self.root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

    def _begin_drag(self, event: tk.Event) -> None:
        self.drag_offset = (
            int(event.x_root) - self.root.winfo_x(),
            int(event.y_root) - self.root.winfo_y(),
        )
        self.root.lift()

    def _drag(self, event: tk.Event) -> None:
        if self.drag_offset is None:
            return
        offset_x, offset_y = self.drag_offset
        monitors = self._monitors()
        monitor = monitor_for_point(monitors, int(event.x_root), int(event.y_root))
        x, y = clamp_overlay_position(
            int(event.x_root) - offset_x,
            int(event.y_root) - offset_y,
            monitor,
            self.WIDTH,
            self.HEIGHT,
        )
        self.root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

    def _end_drag(self, _event: tk.Event) -> None:
        if self.drag_offset is None:
            return
        self.drag_offset = None
        self.manual_position = (self.root.winfo_x(), self.root.winfo_y())
        try:
            save_position(*self.manual_position)
        except OSError:
            pass

    def _reset_position(self, _event: tk.Event) -> None:
        """Right-click returns to automatic caret/pointer positioning."""
        self.drag_offset = None
        self.manual_position = None
        try:
            clear_saved_position()
        except OSError:
            pass
        self._position_for_show()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    StatusOverlayApp().run()
