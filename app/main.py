"""FastAPI 应用入口。

本地控制层对外只暴露这一个服务；数字人能力全部由 DigitalHumanService 代理到
远端的 LiveTalking，业务代码不直接接触其 HTTP API。
"""

from fastapi import FastAPI

app = FastAPI(title="AI 数字人教育教练", version="0.1.0")


@app.get("/health")
async def health() -> dict:
    """健康检查，用于确认本地控制层已正常启动。"""
    return {"status": "ok"}
