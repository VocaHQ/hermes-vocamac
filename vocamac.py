"""Thin client for VocaMac's headless command-line transcriber.

Talks to ``VocaMac --transcribe-file <path> --json`` and ``VocaMac --list-models --json``,
the stable contract documented in VocaMac's docs/CLI.md. It has no Hermes imports, so it
can be tested on its own.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

APP_BINARY_SUFFIX = "VocaMac.app/Contents/MacOS/VocaMac"
DEFAULT_TIMEOUT_SECONDS = 600.0

# Containers AVFoundation cannot read. Voice notes from the Hermes desktop app (WebM),
# Telegram and WhatsApp (Ogg/Opus) arrive in these, so they are converted with ffmpeg
# before VocaMac sees them. Any other file is tried as-is first.
UNREADABLE_BY_AVFOUNDATION = frozenset({
    ".webm", ".weba", ".ogg", ".oga", ".opus", ".mkv", ".amr", ".spx",
})

# Only what VocaMac needs to find its preferences and models. Hermes API keys stay out.
_ENV_PASSTHROUGH = ("HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")


class VocaMacError(Exception):
    """A failure with VocaMac's stable error category (``invalid_audio``, ``model_not_downloaded``, ...)."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category
        self.message = message

    def __str__(self) -> str:
        return f"{self.category}: {self.message}"


@dataclass(frozen=True)
class Transcript:
    text: str
    model: str
    engine: str
    detected_language: str
    duration_seconds: float
    audio_length_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "engine": self.engine,
            "detected_language": self.detected_language,
            "duration_seconds": self.duration_seconds,
            "audio_length_seconds": self.audio_length_seconds,
        }


def find_binary(configured: Optional[str] = None) -> Optional[str]:
    """Locate the VocaMac executable.

    Order: the ``binary`` plugin setting, ``$VOCAMAC_BINARY``, ``/Applications``,
    ``~/Applications``, then a ``vocamac`` command on ``PATH`` (``make install-cli``).
    """
    candidates = [
        configured,
        os.environ.get("VOCAMAC_BINARY"),
        f"/Applications/{APP_BINARY_SUFFIX}",
        str(Path.home() / "Applications" / APP_BINARY_SUFFIX),
    ]
    for candidate in candidates:
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    return shutil.which("vocamac")


def _child_env() -> dict[str, str]:
    return {key: os.environ[key] for key in _ENV_PASSTHROUGH if key in os.environ}


def _run(binary: str, args: list[str], timeout: float) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_child_env(),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise VocaMacError("timeout", f"VocaMac did not finish within {timeout:g}s") from exc
    except OSError as exc:
        raise VocaMacError("launch_failed", f"Could not run {binary}: {exc}") from exc

    if completed.returncode != 0:
        raise _error_from(completed.stderr, completed.returncode)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise VocaMacError("bad_output", f"VocaMac printed something that is not JSON: {completed.stdout[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise VocaMacError("bad_output", "VocaMac printed JSON that is not an object")
    return payload


def _error_from(stderr: str, returncode: int) -> VocaMacError:
    # VocaMac writes exactly one JSON error object on stderr; take the last JSON line in
    # case anything else slipped through.
    for line in reversed((stderr or "").strip().splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "error" in payload:
            return VocaMacError(str(payload["error"]), str(payload.get("message") or payload["error"]))
    detail = (stderr or "").strip()[:300] or "no error output"
    return VocaMacError("exit_code", f"VocaMac exited with code {returncode}: {detail}")


def _convert_with_ffmpeg(source: Path, directory: str, ffmpeg: Optional[str] = None) -> Path:
    ffmpeg = ffmpeg or shutil.which("ffmpeg") or next(
        (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if os.access(p, os.X_OK)), None
    )
    if not ffmpeg:
        raise VocaMacError(
            "ffmpeg_missing",
            f"{source.suffix or 'This'} audio needs ffmpeg to convert it for VocaMac. Install it with `brew install ffmpeg`.",
        )
    target = Path(directory) / "audio.wav"
    try:
        subprocess.run(
            [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target)],
            check=True, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        raise VocaMacError("invalid_audio", f"ffmpeg could not read the audio: {(exc.stderr or '').strip()[:300]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VocaMacError("timeout", "ffmpeg took longer than 300s to convert the audio") from exc
    return target


def transcribe(
    file_path: str,
    *,
    binary: Optional[str] = None,
    model: Optional[str] = None,
    language: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Transcript:
    """Transcribe one audio or video file. Raises :class:`VocaMacError` on any failure."""
    if sys.platform != "darwin":
        raise VocaMacError("unsupported_platform", "VocaMac runs only on macOS (Apple Silicon)")
    executable = find_binary(binary)
    if not executable:
        raise VocaMacError(
            "not_installed",
            "VocaMac is not installed. Install it with `brew install --cask vocahq/vocamac/vocamac` "
            "or from https://vocamac.com, then download a speech model in the app.",
        )
    source = Path(file_path).expanduser()
    if not source.is_file():
        raise VocaMacError("invalid_audio", f"Audio file not found: {file_path}")

    def run_on(path: Path) -> dict[str, Any]:
        args = ["--transcribe-file", str(path), "--json"]
        if model:
            args += ["--model", model]
        if language:
            args += ["--language", language]
        return _run(executable, args, timeout)

    with tempfile.TemporaryDirectory(prefix="hermes-vocamac-") as workdir:
        if source.suffix.lower() in UNREADABLE_BY_AVFOUNDATION:
            payload = run_on(_convert_with_ffmpeg(source, workdir))
        else:
            try:
                payload = run_on(source)
            except VocaMacError as exc:
                # Unknown container: retry once through ffmpeg before giving up.
                if exc.category != "invalid_audio":
                    raise
                try:
                    converted = _convert_with_ffmpeg(source, workdir)
                except VocaMacError:
                    raise exc from None
                payload = run_on(converted)

    return Transcript(
        text=str(payload.get("text") or "").strip(),
        model=str(payload.get("model") or ""),
        engine=str(payload.get("engine") or ""),
        detected_language=str(payload.get("detected_language") or ""),
        duration_seconds=float(payload.get("duration_seconds") or 0),
        audio_length_seconds=float(payload.get("audio_length_seconds") or 0),
    )


def list_models(*, binary: Optional[str] = None, timeout: float = 60.0) -> list[dict[str, Any]]:
    """Every model VocaMac knows, with ``selected`` / ``downloaded`` / ``supported`` flags."""
    executable = find_binary(binary)
    if not executable:
        raise VocaMacError("not_installed", "VocaMac is not installed.")
    models = _run(executable, ["--list-models", "--json"], timeout).get("models")
    return [m for m in models if isinstance(m, dict)] if isinstance(models, list) else []
