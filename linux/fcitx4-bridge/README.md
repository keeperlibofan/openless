# OpenLess Fcitx4 / Sogou compatibility bridge

This directory contains the Linux/X11 compatibility layer used by this fork.
It lets OpenLess keep the user's existing Fcitx 4 + Sogou input-method session
instead of requiring a desktop migration to Fcitx5.

## What it provides

- Right-side modifier-aware global hotkeys from XInput2 raw key events.
- Hold-to-record and toggle modes without losing physical release edges.
- Focused-window text insertion through an X11 clipboard owner and XTest.
- Modifier-safe insertion: recognition/polishing may run during the next
  recording, while the actual synthetic paste waits for the dictation keys to
  be released so overlapping Right Alt cannot corrupt `Ctrl+V`.
- Focus-preserving Alt suppression: a dedicated modifier-only left/right Alt
  trigger is passively grabbed by the bridge. XInput2 still supplies recording
  edges, but Zotero, VS Code/Electron, and other focused applications never see
  the Alt key, so their menu layer cannot steal the editor/webview focus.
- Zotero/Firefox fallback: if another client prevents the passive Alt grab,
  Escape is still emitted after release and before paste to leave the menu
  accelerator layer before the `V` in `Ctrl+V` is sent.
- A rounded status overlay for recording, ASR, polishing, success, and errors.
- Live microphone level visualization while recording.
- Ordered-background-completion protection: an older result never covers a new
  active recording state.
- A draggable overlay whose position is persisted in
  `~/.local/share/OpenLess/overlay-position.json`.
- Best-effort AT-SPI caret positioning, with pointer-based fallback.
- Immediate history-page refresh after successful insertion.

The bridge owns only `org.fcitx.Fcitx5`. The existing Fcitx 4/Sogou process
continues to own `org.fcitx.Fcitx`, so the two services do not conflict.

## Ubuntu dependencies

```bash
sudo apt install \
  python3 python3-venv python3-tk python3-gi gir1.2-atspi-2.0 \
  xinput x11-xserver-utils pulseaudio-utils \
  libx11-6 libxext6 libxtst6
```

## Install

```bash
./linux/fcitx4-bridge/install.sh
```

The installer is user-local and does not need `sudo`. It installs into
`~/.local/opt/openless-fcitx4-bridge` and registers a user systemd service.

## Overlay controls

- Hold Right Alt: record.
- Left-drag the visible capsule: move it and remember the position.
- Right-click the capsule: clear the saved position and return to automatic
  caret/pointer positioning.

## Tests

```bash
cd linux/fcitx4-bridge
~/.local/opt/openless-fcitx4-bridge/venv/bin/python -m unittest -v \
  test_openless_status_overlay.py \
  test_openless_fcitx4_bridge.py
```
