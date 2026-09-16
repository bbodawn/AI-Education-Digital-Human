"""验证 ASR 上传接口、错误映射和临时文件清理。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import get_asr_service
from app.services.asr import ASRService, ASRTranscriptionError, ASRUnavailable


class StubASR(ASRService):
    def __init__(self, text: str = "什么是光合作用", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.paths: list[Path] = []
        self.contents: list[bytes] = []

    def transcribe(self, audio_path: str | Path) -> str:
        path = Path(audio_path)
        assert path.exists()
        self.paths.append(path)
        self.contents.append(path.read_bytes())
        if self.error is not None:
            raise self.error
        return self.text


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def build_client(service: ASRService) -> TestClient:
    app.dependency_overrides[get_asr_service] = lambda: service
    return TestClient(app)


def upload(client: TestClient):
    return client.post(
        "/api/v1/asr/transcribe",
        files={"file": ("question.wav", b"fake audio bytes", "audio/wav")},
    )


def test_transcribe_endpoint_returns_text() -> None:
    service = StubASR()
    response = upload(build_client(service))

    assert response.status_code == 200
    assert response.json() == {"text": "什么是光合作用"}
    assert service.contents == [b"fake audio bytes"]


def test_transcribe_endpoint_requires_file() -> None:
    response = build_client(StubASR()).post("/api/v1/asr/transcribe")

    assert response.status_code == 422


def test_transcription_error_returns_clear_json() -> None:
    service = StubASR(error=ASRTranscriptionError("bad audio"))
    response = upload(build_client(service))

    assert response.status_code == 422
    assert response.json() == {"detail": "Audio transcription failed"}


def test_unavailable_service_returns_clear_json() -> None:
    service = StubASR(error=ASRUnavailable("model unavailable"))
    response = upload(build_client(service))

    assert response.status_code == 503
    assert response.json() == {"detail": "ASR service unavailable"}


@pytest.mark.parametrize(
    "error",
    [None, ASRTranscriptionError("bad audio")],
)
def test_temporary_file_is_removed_after_request(error: Exception | None) -> None:
    service = StubASR(error=error)
    upload(build_client(service))

    assert len(service.paths) == 1
    assert not service.paths[0].exists()
