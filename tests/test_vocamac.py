"""Tests for the VocaMac client, using a fake VocaMac executable.

Run with:  python -m pytest tests
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("vocamac_client", ROOT / "vocamac.py")
vocamac = importlib.util.module_from_spec(spec)
sys.modules["vocamac_client"] = vocamac
spec.loader.exec_module(vocamac)

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="VocaMac runs only on macOS")

FAKE = textwrap.dedent("""\
    #!{python}
    import json, os, sys
    args = sys.argv[1:]
    # The client scrubs the environment, so log next to this script.
    with open(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "calls.jsonl"), "a") as log:
        log.write(json.dumps({{"args": args, "env": sorted(os.environ)}}) + "\\n")
    if "--list-models" in args:
        print(json.dumps({{"models": [
            {{"id": "parakeet-tdt-0.6b-v2", "name": "Parakeet v2", "engine": "parakeet",
              "selected": True, "downloaded": True, "supported": True, "system_managed": False}},
            {{"id": "small", "name": "Whisper Small", "engine": "whisperkit",
              "selected": False, "downloaded": False, "supported": True, "system_managed": False}}]}}))
        sys.exit(0)
    path = args[args.index("--transcribe-file") + 1]
    if path.endswith((".webm", ".ogg", ".xyz")):
        sys.stderr.write(json.dumps({{"error": "invalid_audio", "message": "unsupported format"}}) + "\\n")
        sys.exit(3)
    if "--model" in args and args[args.index("--model") + 1] == "missing":
        sys.stderr.write(json.dumps({{"error": "model_not_downloaded", "message": "Model is not downloaded: missing"}}) + "\\n")
        sys.exit(4)
    print(json.dumps({{"text": " hello from " + os.path.basename(path) + " ", "model": "parakeet-tdt-0.6b-v2",
                      "engine": "parakeet", "detected_language": "en",
                      "duration_seconds": 0.1, "audio_length_seconds": 2.0}}))
""")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    binary = tmp_path / "VocaMac"
    binary.write_text(FAKE.format(python=sys.executable))
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return binary, calls


def _audio(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"\0" * 64)
    return path


def test_transcribes_and_strips_text(fake, tmp_path):
    binary, calls = fake
    result = vocamac.transcribe(str(_audio(tmp_path, "a.wav")), binary=str(binary), language="en")
    assert result.text == "hello from a.wav"
    assert result.engine == "parakeet"
    args = calls()[0]["args"]
    assert args[args.index("--language") + 1] == "en"
    assert "--model" not in args  # follows the model selected in the app


def test_child_environment_excludes_secrets(fake, tmp_path):
    binary, calls = fake
    vocamac.transcribe(str(_audio(tmp_path, "a.wav")), binary=str(binary))
    assert "OPENAI_API_KEY" not in calls()[0]["env"]
    assert "HOME" in calls()[0]["env"]


def test_structured_error_is_surfaced(fake, tmp_path):
    binary, _ = fake
    with pytest.raises(vocamac.VocaMacError) as caught:
        vocamac.transcribe(str(_audio(tmp_path, "a.wav")), binary=str(binary), model="missing")
    assert caught.value.category == "model_not_downloaded"
    assert "missing" in caught.value.message


def test_missing_file(fake):
    binary, _ = fake
    with pytest.raises(vocamac.VocaMacError) as caught:
        vocamac.transcribe("/nope/a.wav", binary=str(binary))
    assert caught.value.category == "invalid_audio"


def test_not_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(vocamac, "find_binary", lambda configured=None: None)
    with pytest.raises(vocamac.VocaMacError) as caught:
        vocamac.transcribe(str(_audio(tmp_path, "a.wav")))
    assert caught.value.category == "not_installed"


def test_webm_is_converted_with_ffmpeg_first(fake, tmp_path, monkeypatch):
    binary, calls = fake
    converted = []

    def fake_convert(source, directory, ffmpeg=None):
        converted.append(source.name)
        return _audio(Path(directory), "audio.wav")

    monkeypatch.setattr(vocamac, "_convert_with_ffmpeg", fake_convert)
    result = vocamac.transcribe(str(_audio(tmp_path, "note.webm")), binary=str(binary))
    assert converted == ["note.webm"]
    assert result.text == "hello from audio.wav"
    assert len(calls()) == 1  # no doomed first attempt on the WebM file


def test_unknown_container_retries_through_ffmpeg(fake, tmp_path, monkeypatch):
    binary, calls = fake
    monkeypatch.setattr(
        vocamac, "_convert_with_ffmpeg", lambda source, directory, ffmpeg=None: _audio(Path(directory), "audio.wav")
    )
    result = vocamac.transcribe(str(_audio(tmp_path, "clip.xyz")), binary=str(binary))
    assert result.text == "hello from audio.wav"
    assert len(calls()) == 2


def test_missing_ffmpeg_keeps_the_original_error_for_unknown_containers(fake, tmp_path, monkeypatch):
    binary, _ = fake

    def no_ffmpeg(source, directory, ffmpeg=None):
        raise vocamac.VocaMacError("ffmpeg_missing", "install ffmpeg")

    monkeypatch.setattr(vocamac, "_convert_with_ffmpeg", no_ffmpeg)
    with pytest.raises(vocamac.VocaMacError) as caught:
        vocamac.transcribe(str(_audio(tmp_path, "clip.xyz")), binary=str(binary))
    assert caught.value.category == "invalid_audio"


def test_list_models(fake):
    binary, _ = fake
    models = vocamac.list_models(binary=str(binary))
    assert [m["id"] for m in models] == ["parakeet-tdt-0.6b-v2", "small"]
    assert models[0]["selected"] is True


def test_find_binary_prefers_configured_path(fake, monkeypatch):
    binary, _ = fake
    monkeypatch.delenv("VOCAMAC_BINARY", raising=False)
    assert vocamac.find_binary(str(binary)) == str(binary)
    monkeypatch.setenv("VOCAMAC_BINARY", str(binary))
    assert vocamac.find_binary(None) == str(binary)


def test_real_ffmpeg_conversion(tmp_path):
    ffmpeg = vocamac.shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not installed")
    source = tmp_path / "tone.ogg"
    os.system(f"'{ffmpeg}' -loglevel error -f lavfi -i sine=frequency=440:duration=1 -c:a libopus '{source}'")
    if not source.exists():
        pytest.skip("ffmpeg build lacks libopus")
    target = vocamac._convert_with_ffmpeg(source, str(tmp_path))
    assert target.suffix == ".wav" and target.stat().st_size > 1000
