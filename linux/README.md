# Linux/X11 personal-fork installation

This fork contains the Linux behavior used on the author's Ubuntu X11 desktop:

- Fcitx 4 + Sogou is preserved; Fcitx5 is not required.
- Right Alt is a hold-to-record hotkey.
- Bailian ASR and DeepSeek polishing are supported.
- Finished recordings enter an ordered background queue, so a new recording can
  start immediately without cancelling the previous segment.
- Results are inserted strictly in capture order.
- The status overlay shows microphone level, ASR, polishing, success, and
  differentiated microphone errors.
- The overlay is draggable, remembers its position, and can automatically
  appear above-left of an accessible text caret.
- Successful insertion refreshes the visible history page immediately.

## Build dependencies on Ubuntu 24.04

```bash
sudo apt update
sudo apt install \
  build-essential curl file pkg-config libssl-dev libasound2-dev libdbus-1-dev \
  libgtk-3-dev libwebkit2gtk-4.1-dev libjavascriptcoregtk-4.1-dev \
  libsoup-3.0-dev libxdo-dev libx11-dev libxext-dev libxtst-dev \
  libayatana-appindicator3-dev librsvg2-dev patchelf \
  python3 python3-venv python3-tk python3-gi gir1.2-atspi-2.0 \
  xinput x11-xserver-utils pulseaudio-utils
```

Install current Node.js LTS and Rust through rustup before building.

## Clone and install

```bash
git clone https://github.com/keeperlibofan/openless.git
cd openless
git switch codex/linux-continuous-dictation
git submodule update --init --recursive
./linux/install-user-build.sh
```

Everything is installed under `~/.local`; the build/install script itself does
not use `sudo`.

## Model configuration

Configure credentials from the OpenLess settings UI. Never put API keys in this
repository or in shell history.

- ASR provider: `bailian`
- Bailian model: `fun-asr-realtime`
- LLM provider: `deepseek`
- DeepSeek model: `deepseek-v4-flash`
- Default style mode: `structured`
- Dictation hotkey: `rightOption`, mode `hold`
- Streaming insertion: enabled
- Output language: `auto`

Credentials are stored through the desktop keyring and are intentionally not
part of the Git backup. Re-enter them in the settings UI after reinstalling a
machine.

## User data that is intentionally not published

The following directory may contain recordings, history, personal dictionary
entries, prompts, and preferences:

```text
~/.local/share/OpenLess/
```

Back it up privately if you need the personal data as well as the source code.
Do not publish it in a public repository.

## Verification

```bash
systemctl --user status openless-fcitx4-bridge.service
pgrep -af '/openless$|openless_fcitx4_bridge|openless_status_overlay'

cd linux/fcitx4-bridge
~/.local/opt/openless-fcitx4-bridge/venv/bin/python -m unittest -v \
  test_openless_status_overlay.py \
  test_openless_fcitx4_bridge.py
```

For the ordered-queue behavior, record two segments back to back: release Right
Alt after the first segment and press it again while the first segment is still
being recognized or polished. The first and second results must be inserted in
that order, with neither job cancelled.
