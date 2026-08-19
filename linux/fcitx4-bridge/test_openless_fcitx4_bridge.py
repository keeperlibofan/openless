#!/usr/bin/env python3

import asyncio
import unittest

from openless_fcitx4_bridge import (
    HotkeySpec,
    HotkeyState,
    OpenLessInterface,
    StatusOverlay,
    dispatch_hotkey_edge,
    dictation_signal_edges,
)
from openless_x11_insert import (
    BLOCKING_PASTE_MODIFIER_MASK,
    paste_key_sequence,
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


class DualSourceHotkeyDispatchTest(unittest.TestCase):
    class FakeInterface:
        def __init__(self, hotkeys: HotkeyState) -> None:
            self.hotkeys = hotkeys
            self.dictation_events: list[tuple[int, int, bool]] = []
            self.recording_cleanup_requests = 0

        def DictationKeyEvent(self, sym: int, states: int, is_press: bool) -> None:
            self.dictation_events.append((sym, states, is_press))

        def QaShortcutEvent(self, _sym: int, _states: int, _is_press: bool) -> None:
            return None

        def TranslationModifierEvent(
            self,
            _sym: int,
            _states: int,
            _is_press: bool,
        ) -> None:
            return None

        def schedule_recording_release_cleanup(self) -> None:
            self.recording_cleanup_requests += 1

    def test_raw_and_grab_sources_emit_each_physical_edge_once(self) -> None:
        hotkeys = HotkeyState(FakeKeymap())
        hotkeys.dictation = HotkeySpec(sym=0xFFEA, primary_keycode=108)
        interface = self.FakeInterface(hotkeys)

        self.assertTrue(dispatch_hotkey_edge(interface, 108, True, mode="hold"))
        self.assertFalse(dispatch_hotkey_edge(interface, 108, True, mode="hold"))
        self.assertTrue(dispatch_hotkey_edge(interface, 108, False, mode="hold"))
        self.assertFalse(dispatch_hotkey_edge(interface, 108, False, mode="hold"))

        self.assertEqual(
            interface.dictation_events,
            [(0xFFEA, 0, True), (0xFFEA, 0, False)],
        )
        self.assertEqual(interface.recording_cleanup_requests, 1)

    def test_toggle_release_does_not_schedule_hold_overlay_cleanup(self) -> None:
        hotkeys = HotkeyState(FakeKeymap())
        hotkeys.dictation = HotkeySpec(sym=0xFFEA, primary_keycode=108)
        hotkeys.update_pressed(108, True)
        interface = self.FakeInterface(hotkeys)

        self.assertTrue(dispatch_hotkey_edge(interface, 108, False, mode="toggle"))

        self.assertEqual(
            interface.dictation_events,
            [(0xFFEA, 0, True), (0xFFEA, 0, False)],
        )
        self.assertEqual(interface.recording_cleanup_requests, 0)


class StatusOverlayStateTest(unittest.TestCase):
    def test_background_success_keeps_active_recording_for_release_cleanup(
        self,
    ) -> None:
        overlay = StatusOverlay()
        overlay._send = lambda _payload: None

        overlay.set_text("🎤 收音中...")
        overlay.set_text("✅ 已插入")

        self.assertTrue(overlay.recording_active)
        self.assertTrue(overlay.clear_if_recording())
        self.assertFalse(overlay.recording_active)

    def test_recognizing_status_ends_recording_state(self) -> None:
        overlay = StatusOverlay()
        overlay._send = lambda _payload: None

        overlay.set_text("🎤 收音中...")
        overlay.set_text("🔄 识别中...")

        self.assertFalse(overlay.recording_active)
        self.assertFalse(overlay.clear_if_recording())


class RecordingOverlayCleanupTest(unittest.IsolatedAsyncioTestCase):
    class FakeStatusOverlay:
        def __init__(self) -> None:
            self.recording_active = True
            self.clear_calls = 0

        def set_text(self, text: str) -> None:
            if "收音中" in text:
                self.recording_active = True
            elif "已插入" not in text:
                self.recording_active = False

        def clear(self) -> None:
            self.recording_active = False
            self.clear_calls += 1

        def clear_if_recording(self) -> bool:
            if not self.recording_active:
                return False
            self.clear()
            return True

    async def test_hold_release_clears_recording_overlay_if_app_stops_responding(
        self,
    ) -> None:
        hotkeys = HotkeyState(FakeKeymap())
        hotkeys.dictation = HotkeySpec(sym=0xFFEA, primary_keycode=108)
        hotkeys.update_pressed(108, True)
        overlay = self.FakeStatusOverlay()
        interface = OpenLessInterface(
            hotkeys=hotkeys,
            status_overlay=overlay,
            recording_release_cleanup_delay=0.01,
        )

        dispatch_hotkey_edge(interface, 108, False, mode="hold")
        await asyncio.sleep(0.03)

        self.assertEqual(overlay.clear_calls, 1)

    async def test_normal_post_release_status_cancels_overlay_cleanup(self) -> None:
        hotkeys = HotkeyState(FakeKeymap())
        hotkeys.dictation = HotkeySpec(sym=0xFFEA, primary_keycode=108)
        hotkeys.update_pressed(108, True)
        overlay = self.FakeStatusOverlay()
        interface = OpenLessInterface(
            hotkeys=hotkeys,
            status_overlay=overlay,
            recording_release_cleanup_delay=0.02,
        )

        dispatch_hotkey_edge(interface, 108, False, mode="hold")
        interface.SetAuxDown("🔄 识别中...")
        await asyncio.sleep(0.04)

        self.assertEqual(overlay.clear_calls, 0)


class FakeDictationSuppressor:
    def __init__(self, active: bool = False) -> None:
        self.active = active
        self.configured: list[HotkeySpec] = []

    def configure(self, spec: HotkeySpec) -> bool:
        self.configured.append(spec)
        return self.active

    def is_suppressing(self, _spec: HotkeySpec) -> bool:
        return self.active


class CommitTextTest(unittest.IsolatedAsyncioTestCase):
    async def test_commit_text_delegates_to_the_x11_inserter(self) -> None:
        class FakeInserter:
            def __init__(self) -> None:
                self.received: list[tuple[str, bool]] = []

            def insert(self, text: str, dismiss_alt_menu: bool = False) -> None:
                self.received.append((text, dismiss_alt_menu))

        inserter = FakeInserter()
        interface = OpenLessInterface(hotkeys=None, text_inserter=inserter)

        await interface.commit_text_when_safe("测试文字")

        self.assertEqual(inserter.received, [("测试文字", False)])

    async def test_commit_text_waits_for_dictation_primary_release(self) -> None:
        class FakeInserter:
            def __init__(self) -> None:
                self.received: list[tuple[str, bool]] = []

            def insert(self, text: str, dismiss_alt_menu: bool = False) -> None:
                self.received.append((text, dismiss_alt_menu))

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

        self.assertEqual(inserter.received, [("第一段完整文字", False)])

    async def test_commit_text_waits_for_combo_modifier_release(self) -> None:
        class FakeInserter:
            def __init__(self) -> None:
                self.received: list[tuple[str, bool]] = []

            def insert(self, text: str, dismiss_alt_menu: bool = False) -> None:
                self.received.append((text, dismiss_alt_menu))

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

        self.assertEqual(inserter.received, [("组合键录音结果", False)])

    async def test_alt_dictation_requests_menu_dismissal_before_paste(self) -> None:
        class FakeInserter:
            def __init__(self) -> None:
                self.received: list[tuple[str, bool]] = []

            def insert(self, text: str, dismiss_alt_menu: bool = False) -> None:
                self.received.append((text, dismiss_alt_menu))

        hotkeys = HotkeyState(FakeKeymap())
        hotkeys.dictation = HotkeySpec(sym=0xFFEA, primary_keycode=108)
        inserter = FakeInserter()
        interface = OpenLessInterface(hotkeys=hotkeys, text_inserter=inserter)

        await interface.commit_text_when_safe("Zotero 语音输入")

        self.assertEqual(inserter.received, [("Zotero 语音输入", True)])

    async def test_non_alt_dictation_does_not_send_escape(self) -> None:
        class FakeInserter:
            def __init__(self) -> None:
                self.received: list[tuple[str, bool]] = []

            def insert(self, text: str, dismiss_alt_menu: bool = False) -> None:
                self.received.append((text, dismiss_alt_menu))

        hotkeys = HotkeyState(FakeKeymap())
        hotkeys.dictation = HotkeySpec(sym=0xFFE4, primary_keycode=105)
        inserter = FakeInserter()
        interface = OpenLessInterface(hotkeys=hotkeys, text_inserter=inserter)

        await interface.commit_text_when_safe("右 Ctrl 输入")

        self.assertEqual(inserter.received, [("右 Ctrl 输入", False)])

    async def test_suppressed_alt_dictation_keeps_editor_focus_without_escape(
        self,
    ) -> None:
        class FakeInserter:
            def __init__(self) -> None:
                self.received: list[tuple[str, bool]] = []

            def insert(self, text: str, dismiss_alt_menu: bool = False) -> None:
                self.received.append((text, dismiss_alt_menu))

        hotkeys = HotkeyState(FakeKeymap())
        hotkeys.dictation = HotkeySpec(sym=0xFFEA, primary_keycode=108)
        suppressor = FakeDictationSuppressor(active=True)
        inserter = FakeInserter()
        interface = OpenLessInterface(
            hotkeys=hotkeys,
            text_inserter=inserter,
            dictation_suppressor=suppressor,
        )

        await interface.commit_text_when_safe("VS Code 语音输入")

        self.assertEqual(inserter.received, [("VS Code 语音输入", False)])


class DictationSuppressionTest(unittest.TestCase):
    def test_custom_alt_binding_configures_the_x11_suppressor(self) -> None:
        class ConfigurableFakeKeymap:
            def name_to_keysym(self, name: str) -> int:
                return {"rightoption": 0xFFEA}[name]

            def keysym_to_keycode(self, sym: int) -> int:
                return {0xFFEA: 108}[sym]

            def modifier_keycodes(self, _name: str) -> set[int]:
                return set()

        hotkeys = HotkeyState(ConfigurableFakeKeymap())
        suppressor = FakeDictationSuppressor(active=True)
        interface = OpenLessInterface(
            hotkeys=hotkeys,
            dictation_suppressor=suppressor,
        )

        interface.SetCustomDictationTrigger("rightoption")

        self.assertEqual(len(suppressor.configured), 1)
        self.assertEqual(suppressor.configured[0].sym, 0xFFEA)
        self.assertEqual(suppressor.configured[0].primary_keycode, 108)

    def test_alt_used_as_part_of_a_combo_is_not_modifier_only(self) -> None:
        hotkeys = HotkeyState(FakeKeymap())
        hotkeys.dictation = HotkeySpec(
            sym=0xFFEA,
            primary_keycode=108,
            required_modifier_groups=[{37, 105}],
        )

        self.assertFalse(hotkeys.dictation_primary_is_alt())


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

    def test_alt_menu_escape_precedes_ctrl_v(self) -> None:
        self.assertEqual(
            paste_key_sequence(
                control_keycode=37,
                v_keycode=55,
                escape_keycode=9,
                dismiss_alt_menu=True,
            ),
            ((9, 1), (9, 0), (37, 1), (55, 1), (55, 0), (37, 0)),
        )

    def test_regular_paste_does_not_send_escape(self) -> None:
        self.assertEqual(
            paste_key_sequence(
                control_keycode=37,
                v_keycode=55,
                escape_keycode=9,
                dismiss_alt_menu=False,
            ),
            ((37, 1), (55, 1), (55, 0), (37, 0)),
        )


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
