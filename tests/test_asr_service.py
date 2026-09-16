"""验证 faster-whisper 适配层，不加载或下载真实模型。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.services as service_registry
from app.services.asr import (
    ASRTranscriptionError,
    ASRUnavailable,
    FasterWhisperService,
)


class FakeWhisperModel:
    def __init__(self, texts=None, error: Exception | None = None) -> None:
        self.texts = texts if texts is not None else ["你好"]
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def transcribe(self, audio_path: str, language: str):
        self.calls.append((audio_path, language))
        if self.error is not None:
            raise self.error
        return (iter(SimpleNamespace(text=text) for text in self.texts), object())


def build_service(fake_model: FakeWhisperModel) -> FasterWhisperService:
    return FasterWhisperService(model_factory=lambda *_args, **_kwargs: fake_model)


def test_transcribe_returns_text_and_uses_chinese() -> None:
    model = FakeWhisperModel(["你好"])
    service = build_service(model)

    assert service.transcribe(Path("question.wav")) == "你好"
    assert model.calls == [("question.wav", "zh")]


def test_transcribe_merges_multiple_segments() -> None:
    service = build_service(FakeWhisperModel([" 你好", "，世界。 "]))

    assert service.transcribe("question.wav") == "你好，世界。"


def test_empty_transcription_raises_explicit_error() -> None:
    service = build_service(FakeWhisperModel([" ", "\n"]))

    with pytest.raises(ASRTranscriptionError, match="未识别到"):
        service.transcribe("silent.wav")


def test_backend_error_is_converted() -> None:
    service = build_service(FakeWhisperModel(error=RuntimeError("decoder failed")))

    with pytest.raises(ASRTranscriptionError, match="音频识别失败"):
        service.transcribe("broken.wav")


def test_model_initialization_error_is_converted() -> None:
    def failing_factory(*_args, **_kwargs):
        raise RuntimeError("model missing")

    with pytest.raises(ASRUnavailable, match="模型初始化失败"):
        FasterWhisperService(model_factory=failing_factory)


def test_service_registry_reuses_one_model_instance(monkeypatch) -> None:
    created = []
    fake_service = object()

    def fake_factory(**kwargs):
        created.append(kwargs)
        return fake_service

    service_registry.get_asr_service.cache_clear()
    monkeypatch.setattr(service_registry, "FasterWhisperService", fake_factory)
    try:
        first = service_registry.get_asr_service()
        second = service_registry.get_asr_service()
    finally:
        service_registry.get_asr_service.cache_clear()

    assert first is second is fake_service
    assert len(created) == 1
    assert created[0]["model"] == "small"
    assert created[0]["device"] == "cpu"
    assert created[0]["compute_type"] == "int8"
    assert created[0]["language"] == "zh"
