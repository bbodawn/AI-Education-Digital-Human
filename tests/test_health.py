"""验证 /health 可用。

这是本地控制层第一个可观测信号：只要它能返回 200，就说明 FastAPI 装配正确、
应用可以启动，后续所有接口才有讨论的基础。
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
