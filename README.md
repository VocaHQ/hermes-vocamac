# VocaMac for Hermes

On-device speech-to-text for [Hermes Agent](https://github.com/NousResearch/hermes-agent) on Apple Silicon Macs, powered by [VocaMac](https://vocamac.com).

With this plugin, Hermes transcribes voice messages on your Mac with the speech model you picked in VocaMac (Parakeet, Whisper, Apple Speech, SenseVoice and others). Audio never leaves the computer, and you don't need a cloud API key.

- **Voice messages:** voice notes from the Hermes desktop app, Telegram, WhatsApp, Discord, Slack and Signal are transcribed by VocaMac.
- **Files:** the agent gets a `vocamac_transcribe` tool for audio and video files ("transcribe ~/Downloads/interview.m4a") and a `vocamac_list_models` tool.

## Requirements

- An Apple Silicon Mac on macOS 14 or later
- [VocaMac](https://github.com/VocaHQ/vocamac) with at least one speech model downloaded. Open the app once and pick a model in **Settings → Speech Model**.
- [ffmpeg](https://ffmpeg.org) for WebM and Ogg/Opus voice notes, which are the formats the Hermes desktop app, Telegram and WhatsApp send: `brew install ffmpeg`

```bash
brew tap vocahq/vocamac && brew trust vocahq/vocamac
brew install --cask vocamac
brew install ffmpeg
```

## Install

```bash
hermes plugins install VocaHQ/hermes-vocamac --enable
hermes config set stt.provider vocamac
```

Start a new session (`/reset`) or restart the gateway so the plugin loads. Then send a voice message.

To check it from a terminal:

```bash
hermes plugins list            # vocamac should show as enabled
/Applications/VocaMac.app/Contents/MacOS/VocaMac --list-models --json
```

## Settings

Everything is optional. Set these in the desktop app under **Settings → Plugins → vocamac**, or from a terminal:

| Setting | Default | Meaning |
|---|---|---|
| `binary` | auto | Path to the VocaMac executable. By default the plugin looks at `$VOCAMAC_BINARY`, `/Applications/VocaMac.app`, `~/Applications/VocaMac.app`, then `vocamac` on `PATH`. |
| `speech_model` | the app's model | A downloaded VocaMac model id, such as `parakeet-tdt-0.6b-v2`. `vocamac_list_models` shows them. |
| `timeout` | `600` | Seconds one transcription may take. |

```bash
hermes config set plugins.entries.vocamac.settings.speech_model parakeet-tdt-0.6b-v2
```

The language hint comes from Hermes (`stt.language`, or `stt.vocamac.language`). Leave it blank, or set it to `auto`, to let VocaMac detect the language.

## How it works

The plugin runs VocaMac's [headless command-line transcriber](https://github.com/VocaHQ/vocamac/blob/main/docs/CLI.md) once per request:

```bash
VocaMac --transcribe-file <audio> --json [--model <id>] [--language <code>]
```

This runs separately from the menu bar app, so it doesn't interrupt dictation, and it never downloads a model or opens a window. Formats that AVFoundation can't read (WebM, Ogg, Opus, MKV, AMR) are first converted with ffmpeg to 16 kHz mono WAV. Any other unreadable file gets one retry through ffmpeg. VocaMac's error categories, such as `model_not_downloaded` and `invalid_audio`, are passed on to Hermes as they are. VocaMac runs with a minimal environment, so your Hermes API keys are not passed to it.

Each request starts a new VocaMac process, so a large model takes a moment to load the first time.

### Without the plugin

If you'd rather not install a plugin, a Hermes command provider can call VocaMac directly. This uses VocaMac's `--text` and `--output` options, which arrive in the release after 1.2.0. On 1.2.0, use `--transcribe-file {input_path} --json | jq -r .text > {output_path}` instead:

```yaml
stt:
  provider: vocamac-cli
  providers:
    vocamac-cli:
      type: command
      normalize: true   # converts WebM/Ogg voice notes with ffmpeg
      command: "/Applications/VocaMac.app/Contents/MacOS/VocaMac --transcribe-file {input_path} --text --output {output_path}"
```

## Development

```bash
python -m pytest -c tests/pytest.ini --rootdir=tests tests
hermes plugins doctor . --ci
hermes plugins validate .
```

The tests use a fake VocaMac executable, so they don't need the app or a model.

## License

MIT. VocaMac itself is AGPL-3.0 and is installed separately.
