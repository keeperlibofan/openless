#!/usr/bin/env python3
"""Refresh OpenLess' visible history page without changing window focus."""

from __future__ import annotations

import os
import re
import subprocess
import time


def configure_accessibility_bus() -> None:
    if os.environ.get("AT_SPI_BUS_ADDRESS"):
        return
    try:
        output = subprocess.check_output(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.a11y.Bus",
                "--object-path",
                "/org/a11y/bus",
                "--method",
                "org.a11y.Bus.GetAddress",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return
    match = re.search(r"'([^']+)'", output)
    if match:
        os.environ["AT_SPI_BUS_ADDRESS"] = match.group(1)


configure_accessibility_bus()

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402


def find_refresh_button(node: object, depth: int = 0) -> object | None:
    try:
        if node.get_role_name() == "push button" and (node.get_name() or "") == "刷新":
            return node
    except Exception:
        return None
    if depth >= 10:
        return None
    try:
        child_count = node.get_child_count()
    except Exception:
        return None
    for index in range(child_count):
        try:
            child = node.get_child_at_index(index)
        except Exception:
            continue
        if child is None:
            continue
        result = find_refresh_button(child, depth + 1)
        if result is not None:
            return result
    return None


def main() -> int:
    # The history record is written immediately before OpenLess publishes its
    # success status.  A tiny delay lets the WebKit page finish the same event
    # turn before the accessible button action reloads the data.
    time.sleep(0.06)
    desktop = Atspi.get_desktop(0)
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app is None:
            continue
        try:
            app_name = (app.get_name() or "").lower()
        except Exception:
            continue
        if app_name != "openless":
            continue
        button = find_refresh_button(app)
        if button is None:
            return 0
        try:
            action = button.get_action_iface()
            if action is not None and action.get_n_actions() > 0:
                action.do_action(0)
        except Exception:
            return 0
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
