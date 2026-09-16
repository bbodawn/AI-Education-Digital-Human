"""音频文件转写接口。"""

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile
from starlette.concurrency import run_in_threadpool

from app.schemas import ASRTranscriptionResponse
from app.services import get_asr_service
from app.services.asr import ASRService

router = APIRouter(prefix="/api/v1/asr", tags=["asr"])


@router.post("/transcribe", response_model=ASRTranscriptionResponse)
async def transcribe_audio(
    file: UploadFile = File(...),
    service: ASRService = Depends(get_asr_service),
) -> ASRTranscriptionResponse:
    """暂存上传音频，在线程池中识别，并始终清理临时文件。"""
    suffix = Path(file.filename or "").suffix
    temp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_path = Path(temp_file.name)
            while chunk := await file.read(1024 * 1024):
                temp_file.write(chunk)

        text = await run_in_threadpool(service.transcribe, temp_path)
        return ASRTranscriptionResponse(text=text)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        await file.close()
