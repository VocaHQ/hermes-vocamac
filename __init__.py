"""VocaMac for Hermes: on-device speech-to-text on Apple Silicon Macs.

Registers:
- a ``vocamac`` transcription provider (select it with ``stt.provider: vocamac``), so voice
  messages from the desktop app, Telegram, WhatsApp, Discord, Slack and Signal are
  transcribed on the Mac by the model selected in VocaMac;
- ``vocamac_transcribe`` and ``vocamac_list_models`` tools, so the agent can transcribe
  audio and video files on request.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from . import vocamac

logger = logging.getLogger(__name__)

PROVIDER_NAME = "vocamac"

TRANSCRIBE_SCHEMA = {
    "name": "vocamac_transcribe",
    "description": (
        "Transcribe an audio or video file to text on this Mac with VocaMac (private, on-device "
        "speech recognition: Parakeet, Whisper, Apple Speech and others). Accepts wav, m4a, mp3, "
        "aiff, caf, mp4, mov and, with ffmpeg installed, webm, ogg and opus. Up to 30 minutes "
        "and 500 MB per file. Uses the model selected in the VocaMac app unless `model` is given."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the audio or video file."},
            "model": {
                "type": "string",
                "description": "Optional VocaMac model id (see vocamac_list_models). It must already be downloaded.",
            },
            "language": {
                "type": "string",
                "description": "Optional language code such as 'en' or 'de', or 'auto' to detect it.",
            },
        },
        "required": ["path"],
    },
}

LIST_MODELS_SCHEMA = {
    "name": "vocamac_list_models",
    "description": (
        "List the speech models VocaMac can use on this Mac, showing which one is selected "
        "and which are downloaded. Use before passing `model` to vocamac_transcribe."
    ),
    "parameters": {"type": "object", "properties": {}},
}


class _Settings:
    """Plugin settings (``plugins.entries.vocamac.settings``), read fresh on every call."""

    def __init__(self, ctx: Any = None):
        self._ctx = ctx

    def get(self, key: str, default: Any = None) -> Any:
        if self._ctx is None:
            return default
        try:
            value = self._ctx.get_config(key, default)
        except Exception:  # settings must never break transcription
            return default
        return default if value in (None, "") else value

    @property
    def binary(self) -> Optional[str]:
        return self.get("binary")

    @property
    def model(self) -> Optional[str]:
        return self.get("model")

    @property
    def timeout(self) -> float:
        try:
            return float(self.get("timeout", vocamac.DEFAULT_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            return vocamac.DEFAULT_TIMEOUT_SECONDS


def _make_provider_class():
    from agent.transcription_provider import TranscriptionProvider

    class VocaMacTranscriptionProvider(TranscriptionProvider):
        def __init__(self, settings: _Settings):
            self._settings = settings

        @property
        def name(self) -> str:
            return PROVIDER_NAME

        @property
        def display_name(self) -> str:
            return "VocaMac (on-device)"

        def is_available(self) -> bool:
            import sys
            return sys.platform == "darwin" and vocamac.find_binary(self._settings.binary) is not None

        def default_model(self) -> Optional[str]:
            # None follows the model selected in the VocaMac app.
            return self._settings.model

        def list_models(self) -> list[dict[str, Any]]:
            try:
                models = vocamac.list_models(binary=self._settings.binary)
            except vocamac.VocaMacError:
                return []
            return [
                {"id": m.get("id"), "display": m.get("name") or m.get("id")}
                for m in models
                if m.get("downloaded") and m.get("supported", True)
            ]

        def get_setup_schema(self) -> dict[str, Any]:
            return {"name": "VocaMac", "badge": "local", "tag": "On-device speech-to-text on Apple Silicon", "env_vars": []}

        def transcribe(self, file_path, *, model=None, language=None, **extra) -> dict[str, Any]:
            try:
                result = vocamac.transcribe(
                    file_path,
                    binary=self._settings.binary,
                    model=model or self._settings.model,
                    language=language or None,
                    timeout=self._settings.timeout,
                )
            except vocamac.VocaMacError as exc:
                return {"success": False, "transcript": "", "provider": PROVIDER_NAME, "error": f"VocaMac {exc}"}
            except Exception as exc:  # the provider contract forbids raising
                logger.exception("VocaMac transcription failed")
                return {"success": False, "transcript": "", "provider": PROVIDER_NAME, "error": f"VocaMac failed: {exc}"}
            return {"success": True, "transcript": result.text, "provider": PROVIDER_NAME}

    return VocaMacTranscriptionProvider


def _transcribe_tool(settings: _Settings):
    def handler(args: dict, **kwargs) -> str:
        path = str((args or {}).get("path") or "").strip()
        if not path:
            return json.dumps({"error": "invalid_arguments", "message": "path is required"})
        try:
            result = vocamac.transcribe(
                path,
                binary=settings.binary,
                model=(args.get("model") or settings.model or None),
                language=(args.get("language") or None),
                timeout=settings.timeout,
            )
        except vocamac.VocaMacError as exc:
            return json.dumps({"error": exc.category, "message": exc.message})
        except Exception as exc:
            return json.dumps({"error": "transcription_failed", "message": str(exc)})
        return json.dumps(result.as_dict(), ensure_ascii=False)

    return handler


def _list_models_tool(settings: _Settings):
    def handler(args: dict, **kwargs) -> str:
        try:
            return json.dumps({"models": vocamac.list_models(binary=settings.binary)}, ensure_ascii=False)
        except vocamac.VocaMacError as exc:
            return json.dumps({"error": exc.category, "message": exc.message})
        except Exception as exc:
            return json.dumps({"error": "list_failed", "message": str(exc)})

    return handler


def register(ctx) -> None:
    settings = _Settings(ctx)
    ctx.register_transcription_provider(_make_provider_class()(settings))

    def available() -> bool:
        import sys
        return sys.platform == "darwin" and vocamac.find_binary(settings.binary) is not None

    ctx.register_tool(
        name="vocamac_transcribe", toolset="vocamac", schema=TRANSCRIBE_SCHEMA,
        handler=_transcribe_tool(settings), check_fn=available,
        description="Transcribe audio/video files on-device with VocaMac", emoji="🎙️",
    )
    ctx.register_tool(
        name="vocamac_list_models", toolset="vocamac", schema=LIST_MODELS_SCHEMA,
        handler=_list_models_tool(settings), check_fn=available,
        description="List VocaMac speech models", emoji="🎙️",
    )
