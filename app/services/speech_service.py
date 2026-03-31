"""Speech-to-text helpers for short Telegram voice messages."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from app.config import (
    VOICE_MAX_DURATION_SECONDS,
    VOICE_TRANSCRIPTION_COMPUTE_TYPE,
    VOICE_TRANSCRIPTION_DEVICE,
    VOICE_TRANSCRIPTION_ENABLED,
    VOICE_TRANSCRIPTION_LANGUAGE,
    VOICE_TRANSCRIPTION_MODEL,
)
from app.core.observability import log_event, timed_event

logger = logging.getLogger(__name__)


class SpeechTranscriptionError(RuntimeError):
    """Raised when short voice transcription cannot be completed."""


@lru_cache(maxsize=1)
def _load_model():
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - depends on optional runtime dependency
        raise SpeechTranscriptionError(
            "小灰毛这边还没装好语音转写依赖。先执行 `pip install -r requirements.txt`，再重启我就能听语音了。"
        ) from exc

    log_event(
        logger,
        "speech.model_load",
        model=VOICE_TRANSCRIPTION_MODEL,
        device=VOICE_TRANSCRIPTION_DEVICE,
        compute_type=VOICE_TRANSCRIPTION_COMPUTE_TYPE,
    )
    with timed_event(
        logger,
        "speech.model_loaded",
        model=VOICE_TRANSCRIPTION_MODEL,
        device=VOICE_TRANSCRIPTION_DEVICE,
        compute_type=VOICE_TRANSCRIPTION_COMPUTE_TYPE,
    ):
        return WhisperModel(
            VOICE_TRANSCRIPTION_MODEL,
            device=VOICE_TRANSCRIPTION_DEVICE,
            compute_type=VOICE_TRANSCRIPTION_COMPUTE_TYPE,
        )


def transcribe_short_voice(audio_path: str, *, duration_seconds: int | None = None) -> dict[str, Any]:
    if not VOICE_TRANSCRIPTION_ENABLED:
        raise SpeechTranscriptionError("小灰毛这边暂时没有打开语音转写。")
    if duration_seconds and duration_seconds > VOICE_MAX_DURATION_SECONDS:
        raise SpeechTranscriptionError(
            f"小灰毛这边现在只接短语音，尽量控制在 {VOICE_MAX_DURATION_SECONDS} 秒内。"
        )

    model = _load_model()
    log_event(
        logger,
        "speech.transcribe_start",
        path=audio_path,
        duration_seconds=duration_seconds or 0,
        language=VOICE_TRANSCRIPTION_LANGUAGE,
    )
    with timed_event(
        logger,
        "speech.transcribe_complete",
        path=audio_path,
        duration_seconds=duration_seconds or 0,
        language=VOICE_TRANSCRIPTION_LANGUAGE,
    ):
        segments, info = model.transcribe(
            audio_path,
            language=VOICE_TRANSCRIPTION_LANGUAGE or None,
            vad_filter=True,
            beam_size=1,
            best_of=1,
            condition_on_previous_text=False,
        )
        text = "".join(segment.text for segment in segments).strip()

    if not text:
        raise SpeechTranscriptionError("小灰毛这次没稳稳听清，可以换个更短、更清楚的语音再试一下。")

    result = {
        "text": text,
        "language": getattr(info, "language", VOICE_TRANSCRIPTION_LANGUAGE),
        "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
    }
    log_event(
        logger,
        "speech.transcribe_result",
        text_preview=text[:80],
        language=result["language"],
        language_probability=result["language_probability"],
    )
    return result
