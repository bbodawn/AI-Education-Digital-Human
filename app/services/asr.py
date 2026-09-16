"""本地语音识别抽象，以及基于 faster-whisper 的 CPU 实现。

API 层只依赖 ASRService，不直接接触 WhisperModel。这样测试可以注入假服务，
无需加载或下载真实模型。
"""

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ASRError(Exception):
    """语音识别失败的基类。"""


class ASRUnavailable(ASRError):
    """ASR 引擎或模型无法初始化。"""


class ASRTranscriptionError(ASRError):
    """音频无法被识别，或识别结果为空。"""


class ASRService(ABC):
    """语音识别服务接口。"""

    @abstractmethod
    def transcribe(self, audio_path: str | Path) -> str:
        """把一个本地音频文件转成非空文本。"""


class FasterWhisperService(ASRService):
    """使用 faster-whisper 完成本地 CPU 语音识别。"""

    def __init__(
        self,
        model: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "zh",
        download_root: str | Path | None = None,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._language = language

        if model_factory is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise ASRUnavailable("faster-whisper 未安装或无法导入") from exc
            model_factory = WhisperModel

        model_kwargs: dict[str, Any] = {
            "device": device,
            "compute_type": compute_type,
        }
        if download_root is not None:
            model_kwargs["download_root"] = str(download_root)

        try:
            self._model = model_factory(model, **model_kwargs)
        except Exception as exc:
            logger.warning("ASR 模型初始化失败：%s", exc.__class__.__name__)
            raise ASRUnavailable("ASR 模型初始化失败") from exc

    def transcribe(self, audio_path: str | Path) -> str:
        try:
            segments, _info = self._model.transcribe(
                str(audio_path),
                language=self._language,
            )
            text = "".join(segment.text for segment in segments).strip()
        except Exception as exc:
            logger.warning("音频识别失败：%s", exc.__class__.__name__)
            raise ASRTranscriptionError("音频识别失败") from exc

        if not text:
            raise ASRTranscriptionError("未识别到语音文本")
        return text
