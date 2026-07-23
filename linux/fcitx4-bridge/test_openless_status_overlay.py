#!/usr/bin/env python3

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from openless_status_overlay import (
    MicrophoneSnapshot,
    Monitor,
    ScreenRect,
    clamp_overlay_position,
    classify_status,
    classify_recording_error,
    db_to_level,
    load_saved_position,
    position_near_anchor,
    rounded_shape_primitives,
    save_position,
    should_replace_visible_status,
)


class AudioLevelTest(unittest.TestCase):
    def test_db_level_is_clamped_and_speech_is_visible(self) -> None:
        self.assertEqual(db_to_level(-90.0), 0.0)
        self.assertEqual(db_to_level(-60.0), 0.0)
        self.assertGreater(db_to_level(-30.0), 0.65)
        self.assertLess(db_to_level(-30.0), 0.8)
        self.assertEqual(db_to_level(0.0), 1.0)


class StatusClassificationTest(unittest.TestCase):
    def test_recording_and_polishing_states_have_dedicated_kinds(self) -> None:
        self.assertEqual(classify_status("🎤 收音中...").kind, "recording")
        self.assertEqual(classify_status("🔄 识别中...").kind, "recognizing")
        self.assertEqual(classify_status("✨ 润色中...").kind, "polishing")
        self.assertEqual(classify_status("✅ 已插入").kind, "success")
        self.assertEqual(classify_status("❌ 出错").kind, "error")

    def test_generic_error_does_not_claim_a_microphone_or_network_failure(self) -> None:
        style = classify_status("❌ 出错")

        self.assertNotIn("网络", style.subtitle)
        self.assertNotIn("麦克风", style.subtitle)
        self.assertIn("稍后", style.subtitle)

    def test_all_zero_input_is_reported_as_a_disconnected_microphone(self) -> None:
        style = classify_recording_error(
            MicrophoneSnapshot(
                stream_started=True,
                stream_failed=False,
                bytes_seen=3200,
                nonzero_samples=0,
                peak_db=float("-inf"),
            )
        )

        self.assertEqual(style.label, "麦克风未连接")
        self.assertIn("发射器", style.subtitle)

    def test_connected_microphone_without_speech_is_reported_separately(self) -> None:
        style = classify_recording_error(
            MicrophoneSnapshot(
                stream_started=True,
                stream_failed=False,
                bytes_seen=3200,
                nonzero_samples=1200,
                peak_db=-67.0,
            )
        )

        self.assertEqual(style.label, "未检测到语音")
        self.assertIn("右 Alt", style.subtitle)

    def test_detected_voice_keeps_a_later_failure_generic(self) -> None:
        style = classify_recording_error(
            MicrophoneSnapshot(
                stream_started=True,
                stream_failed=False,
                bytes_seen=3200,
                nonzero_samples=2400,
                peak_db=-24.0,
            )
        )

        self.assertEqual(style.label, "处理失败")
        self.assertIn("稍后", style.subtitle)


class RoundedWindowShapeTest(unittest.TestCase):
    def test_shape_has_symmetric_rounding_on_all_four_corners(self) -> None:
        shape = rounded_shape_primitives(width=332, height=92, radius=22)

        self.assertEqual(
            shape.rectangles,
            ((22, 0, 288, 92), (0, 22, 332, 48)),
        )
        self.assertEqual(
            shape.arcs,
            (
                (0, 0, 44, 44),
                (288, 0, 44, 44),
                (0, 48, 44, 44),
                (288, 48, 44, 44),
            ),
        )


class ConcurrentStatusTest(unittest.TestCase):
    def test_background_completion_does_not_cover_active_recording(self) -> None:
        self.assertFalse(should_replace_visible_status("recording", "success"))
        self.assertTrue(should_replace_visible_status("recording", "recognizing"))
        self.assertTrue(should_replace_visible_status("recognizing", "success"))


class OverlayPositionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.monitors = [Monitor(0, 0, 1920, 1080, True)]

    def test_default_position_is_above_and_left_of_caret(self) -> None:
        position = position_near_anchor(
            ScreenRect(1000, 600, 2, 24),
            self.monitors,
            width=332,
            height=92,
        )

        self.assertEqual(position, (654, 494))

    def test_position_flips_below_and_right_near_top_left_corner(self) -> None:
        position = position_near_anchor(
            ScreenRect(10, 10, 2, 24),
            self.monitors,
            width=332,
            height=92,
        )

        self.assertEqual(position, (26, 48))

    def test_drag_position_is_clamped_inside_monitor(self) -> None:
        self.assertEqual(
            clamp_overlay_position(1900, 1060, self.monitors[0], 332, 92),
            (1580, 980),
        )

    def test_manual_position_round_trips(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "overlay-position.json"
            save_position(321, 654, path)

            self.assertEqual(load_saved_position(path), (321, 654))


if __name__ == "__main__":
    unittest.main()
