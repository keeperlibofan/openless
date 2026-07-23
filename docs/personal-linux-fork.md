# keeperlibofan Linux/X11 fork notes

This document records the behavior that must survive a machine loss or a clean
reinstallation. It intentionally contains no credential value.

## Source baseline

- Official project: <https://github.com/Open-Less/openless>
- License: MIT
- Fork: <https://github.com/keeperlibofan/openless>
- Custom branch: `codex/linux-continuous-dictation`
- Baseline release: `v1.3.15-Beta.1-tauri`

## Dictation behavior

- Trigger: Right Alt / Right Option.
- Mode: hold to record; releasing the key ends the current capture.
- Releasing a capture detaches it from the recording lane immediately.
- A second capture may start while the first is still finalizing ASR or being
  polished.
- Every completed capture owns an immutable snapshot of its ASR session, focus
  target, foreground application, translation flag, recording archive state,
  and cancellation token.
- One ordered background worker performs ASR finalization, DeepSeek polishing,
  insertion, and history persistence.
- Results are always inserted in capture order.
- Starting a later capture never cancels or overwrites an older queued capture.
- Recognition and polishing continue while a later hold-to-record capture is
  active, but the Linux synthetic paste is deferred until the recording
  shortcut is fully released. This prevents a physical Right Alt from turning
  the bridge's `Ctrl+V` into `Ctrl+Alt+V` and silently dropping text.
- Modifier-only Alt triggers can leave Firefox/XUL applications such as Zotero
  focused on their menu accelerator layer. For Alt-based dictation only, the
  bridge sends Escape after release and before `Ctrl+V`, returning focus to the
  previous LM/chat editor instead of opening Zotero's View menu.

## Linux desktop integration

The target desktop is Ubuntu X11 with Fcitx 4 and Sogou. The compatibility
bridge deliberately preserves that session and does not replace it with Fcitx5.

The bridge exports the DBus surface OpenLess expects under
`org.fcitx.Fcitx5/openless`, while Fcitx 4 keeps its own
`org.fcitx.Fcitx` service.

Text insertion uses a short-lived X11 clipboard owner plus XTest-generated
`Ctrl+V`. Text is sent to the helper on stdin so it does not appear in process
arguments.

## Status overlay

The Python/Tk X11 overlay provides:

- rounded four-corner window shape;
- recording, recognizing, polishing, success, and error states;
- live microphone volume bars and percentage;
- separate messages for a disconnected microphone and connected silence;
- protection against an older success state covering a newer recording;
- left-button dragging with persistent position;
- right-click reset to automatic positioning;
- best-effort AT-SPI caret positioning, with pointer fallback;
- multi-monitor clamping so the complete overlay remains visible.

## History refresh

After a successful insertion, the compatibility bridge invokes an AT-SPI
helper that activates the OpenLess History page's refresh action without
stealing focus from the user's current application.

## Model profile

The working profile used while developing this fork is:

| Setting | Value |
| --- | --- |
| ASR provider | `bailian` |
| ASR model | `fun-asr-realtime` |
| LLM provider | `deepseek` |
| LLM model | `deepseek-v4-flash` |
| Style | `builtin.structured` |
| Output language | `auto` |
| Streaming insertion | enabled |

API keys are stored in the OS credential vault and must be entered again after
a clean installation. They are never part of Git.

## Private backup boundary

Git preserves the application and integration code. It does not preserve the
following personal runtime data:

- API keys and credential-vault entries;
- `~/.local/share/OpenLess/history.json`;
- `~/.local/share/OpenLess/recordings/`;
- personal dictionary entries;
- local preferences and unpublished custom prompts.

Back up `~/.local/share/OpenLess/` separately to encrypted private storage if
those records are also required.

## Regression gates

The Linux overlay and bridge tests are:

```bash
cd linux/fcitx4-bridge
~/.local/opt/openless-fcitx4-bridge/venv/bin/python -m unittest -v \
  test_openless_status_overlay.py \
  test_openless_fcitx4_bridge.py
```

The Rust queue tests include:

- `finishing_capture_releases_recording_lane_before_background_processing`
- `ordered_processing_queue_has_one_worker_and_preserves_capture_order`

Use an isolated HOME/XDG directory for Rust tests so they cannot rewrite the
real `~/.local/share/OpenLess/preferences.json`.
