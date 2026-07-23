#!/usr/bin/env python3

import unittest

from openless_fcitx4_bridge import OpenLessInterface, dictation_signal_edges


class DictationSignalEdgesTest(unittest.TestCase):
    def test_hold_preserves_physical_press_and_release_edges(self) -> None:
        self.assertEqual(dictation_signal_edges("hold", True), (True,))
        self.assertEqual(dictation_signal_edges("hold", False), (False,))

    def test_toggle_waits_for_physical_key_release(self) -> None:
        self.assertEqual(dictation_signal_edges("toggle", True), ())
        self.assertEqual(dictation_signal_edges("toggle", False), (True, False))


class CommitTextTest(unittest.TestCase):
    def test_commit_text_delegates_to_the_x11_inserter(self) -> None:
        class FakeInserter:
            def __init__(self) -> None:
                self.received: list[str] = []

            def insert(self, text: str) -> None:
                self.received.append(text)

        inserter = FakeInserter()
        interface = OpenLessInterface(hotkeys=None, text_inserter=inserter)

        interface.CommitText("测试文字")

        self.assertEqual(inserter.received, ["测试文字"])


class StatusOverlayTest(unittest.TestCase):
    def test_aux_status_is_forwarded_and_cleared(self) -> None:
        class FakeOverlay:
            def __init__(self) -> None:
                self.events: list[tuple[str, str | None]] = []

            def set_text(self, text: str) -> None:
                self.events.append(("set", text))

            def clear(self) -> None:
                self.events.append(("clear", None))

        overlay = FakeOverlay()
        interface = OpenLessInterface(hotkeys=None, status_overlay=overlay)

        interface.SetAuxDown("✨ 润色中...")
        interface.ClearAuxDown()

        self.assertEqual(
            overlay.events,
            [("set", "✨ 润色中..."), ("clear", None)],
        )

    def test_success_status_refreshes_history_once(self) -> None:
        class FakeOverlay:
            def set_text(self, _text: str) -> None:
                return None

            def clear(self) -> None:
                return None

        class FakeHistoryRefresher:
            def __init__(self) -> None:
                self.calls = 0

            def refresh(self) -> None:
                self.calls += 1

        refresher = FakeHistoryRefresher()
        interface = OpenLessInterface(
            hotkeys=None,
            status_overlay=FakeOverlay(),
            history_refresher=refresher,
        )

        interface.SetAuxDown("🎤 收音中...")
        interface.SetAuxDown("✨ 润色中...")
        interface.SetAuxDown("✅ 已插入")

        self.assertEqual(refresher.calls, 1)


if __name__ == "__main__":
    unittest.main()
