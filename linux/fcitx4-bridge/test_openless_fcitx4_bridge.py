#!/usr/bin/env python3

import asyncio
import unittest

from openless_fcitx4_bridge import (
    HotkeySpec,
    HotkeyState,
    OpenLessInterface,
    dictation_signal_edges,
)
from openless_x11_insert import (
    BLOCKING_PASTE_MODIFIER_MASK,
    wait_for_paste_modifiers_released,
)


class DictationSignalEdgesTest(unittest.TestCase):
    def test_hold_preserves_physical_press_and_release_edges(self) -> None:
        self.assertEqual(dictation_signal_edges("hold", True), (True,))
        self.assertEqual(dictation_signal_edges("hold", False), (False,))

    def test_toggle_waits_for_physical_key_release(self) -> None:
        self.assertEqual(dictation_signal_edges("toggle", True), ())
        self.assertEqual(dictation_signal_edges("toggle", False), (True, False))


class FakeKeymap:
    pass


class CommitTextTest(unittest.IsolatedAsyncioTestCase):
    async def test_commit_text_delegates_to_the_x11_inserter(self) -> None:
        class FakeInserter:
            def __init__(self) -> None:
                self.received: list[str] = []

            def insert(self, text: str) -> None:
                self.received.append(text)

        inserter = FakeInserter()
        interface = OpenLessInterface(hotkeys=None, text_inserter=inserter)

        await interface.commit_text_when_safe("测试文字")

        self.assertEqual(inserter.received, ["测试文字"])

    async def test_commit_text_waits_for_dictation_primary_release(self) -> None:
        class FakeInserter:
            def __init__(self) -> None:
                self.received: list[str] = []

            def insert(self, text: str) -> None:
                self.received.append(text)

        hotkeys = HotkeyState(FakeKeymap())
        hotkeys.dictation = HotkeySpec(primary_keycode=108)
        hotkeys.update_pressed(108, True)
        inserter = FakeInserter()
        interface = OpenLessInterface(hotkeys=hotkeys, text_inserter=inserter)

        insertion = asyncio.create_task(
            interface.commit_text_when_safe("第一段完整文字")
        )
        await asyncio.sleep(0.01)
        self.assertEqual(inserter.received, [])

        hotkeys.update_pressed(108, False)
        await asyncio.wait_for(insertion, timeout=1)

        self.assertEqual(inserter.received, ["第一段完整文字"])

    async def test_commit_text_waits_for_combo_modifier_release(self) -> None:
        class FakeInserter:
            def __init__(self) -> None:
                self.received: list[str] = []

            def insert(self, text: str) -> None:
                self.received.append(text)

        hotkeys = HotkeyState(FakeKeymap())
        hotkeys.dictation = HotkeySpec(
            primary_keycode=65,
            required_modifier_groups=[{37, 105}],
        )
        hotkeys.update_pressed(37, True)
        hotkeys.update_pressed(65, True)
        inserter = FakeInserter()
        interface = OpenLessInterface(hotkeys=hotkeys, text_inserter=inserter)

        insertion = asyncio.create_task(
            interface.commit_text_when_safe("组合键录音结果")
        )
        await asyncio.sleep(0.01)
        hotkeys.update_pressed(65, False)
        await asyncio.sleep(0.01)
        self.assertEqual(inserter.received, [])

        hotkeys.update_pressed(37, False)
        await asyncio.wait_for(insertion, timeout=1)

        self.assertEqual(inserter.received, ["组合键录音结果"])


class PasteModifierGateTest(unittest.TestCase):
    def test_waits_until_blocking_modifier_mask_clears(self) -> None:
        masks = iter(
            [
                BLOCKING_PASTE_MODIFIER_MASK,
                BLOCKING_PASTE_MODIFIER_MASK,
                0,
            ]
        )
        sleeps: list[float] = []

        wait_for_paste_modifiers_released(
            lambda: next(masks),
            timeout=1,
            poll_interval=0.01,
            sleep=sleeps.append,
        )

        self.assertEqual(sleeps, [0.01, 0.01])


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
