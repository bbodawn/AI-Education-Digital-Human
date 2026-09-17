"""FastAPI 应用入口。

本地控制层对外只暴露这一个服务：前端页面、业务接口都在这里。
数字人能力由 services 层代理到远端 LiveTalking，业务代码不直接接触它的 HTTP API。
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api import asr, avatar, chat
from app.config import STATIC_DIR
from app.services.asr import ASRTranscriptionError, ASRUnavailable
from app.services.digital_human import DigitalHumanBadResponse, DigitalHumanUnavailable
from app.services.llm import LLMBadResponse, LLMUnavailable

logger = logging.getLogger(__name__)

app = FastAPI(title="AI 数字人教育教练", version="0.3.0")

app.include_router(avatar.router)
app.include_router(chat.router)
app.include_router(asr.router)


@app.get("/health")
async def health() -> dict:
    """健康检查，用于确认本地控制层已正常启动。"""
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """返回前端页面，使用户只需访问本地 FastAPI 这一个入口。"""
    return FileResponse(STATIC_DIR / "index.html")


# --------------------------------------------------------------------------
# 把服务层的错误类型翻译成 HTTP 状态码。
# 这样浏览器看到的是可读的 JSON，而不是 Python traceback；
# 详细原因记在服务端日志里。
# --------------------------------------------------------------------------


@app.exception_handler(DigitalHumanUnavailable)
async def handle_unavailable(request: Request, exc: DigitalHumanUnavailable) -> JSONResponse:
    logger.warning("数字人服务不可用：%s", exc)
    return JSONResponse(status_code=502, content={"detail": "LiveTalking service unavailable"})


@app.exception_handler(DigitalHumanBadResponse)
async def handle_bad_response(request: Request, exc: DigitalHumanBadResponse) -> JSONResponse:
    logger.warning("数字人服务返回异常：%s", exc)
    detail = "LiveTalking returned an error"
    if exc.status_code is not None:
        detail = f"{detail} (HTTP {exc.status_code})"
    return JSONResponse(status_code=502, content={"detail": detail})


# 大模型侧的失败单独成一类：它和数字人是两个独立的服务，
# 报错时必须能一眼看出是哪一边挂了。


@app.exception_handler(LLMUnavailable)
async def handle_llm_unavailable(request: Request, exc: LLMUnavailable) -> JSONResponse:
    logger.warning("大模型服务不可用：%s", exc)
    return JSONResponse(status_code=502, content={"detail": "Ollama service unavailable"})


@app.exception_handler(LLMBadResponse)
async def handle_llm_bad_response(request: Request, exc: LLMBadResponse) -> JSONResponse:
    logger.warning("大模型返回异常：%s", exc)
    detail = "Ollama returned an error"
    if exc.status_code is not None:
        detail = f"{detail} (HTTP {exc.status_code})"
    return JSONResponse(status_code=502, content={"detail": detail})


@app.exception_handler(ASRUnavailable)
async def handle_asr_unavailable(request: Request, exc: ASRUnavailable) -> JSONResponse:
    logger.warning("语音识别服务不可用：%s", exc)
    return JSONResponse(status_code=503, content={"detail": "ASR service unavailable"})


@app.exception_handler(ASRTranscriptionError)
async def handle_asr_transcription_error(
    request: Request, exc: ASRTranscriptionError
) -> JSONResponse:
    logger.warning("音频识别失败：%s", exc)
    return JSONResponse(status_code=422, content={"detail": "Audio transcription failed"})
