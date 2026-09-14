# AI 数字人教育教练

一个面向教育场景的实时数字人问答项目。用户通过文字（后续支持语音）与数字人对话，数字人实时给出带口型同步的音视频回答。

当前进度：**V0.1（文字 → TTS → Avatar → WebRTC → 浏览器播放）**

## 架构

```
本地 Windows                       数字人服务（先本地后云端）
┌──────────────────────┐          ┌──────────────────────────┐
│ FastAPI（业务控制层） │  HTTP    │ LiveTalking :8010        │
│  · 信令代理           │─────────>│  · TTS（内置 EdgeTTS）    │
│  · 会话控制           │          │  · Avatar（wav2lip256）   │
│  · 静态页             │          │  · WebRTC 推流            │
└──────────────────────┘          └───────────┬──────────────┘
        浏览器 <──────── WebRTC 媒体流 ────────┘
```

**核心原则**：本地负责业务逻辑（后续 Qwen / ASR / RAG / Agent），云端只负责 GPU 密集型推理（TTS / Avatar / Lip Sync）。二者通过 `DigitalHumanService` 抽象层解耦——更换 Avatar 技术不影响业务代码。

## 技术选型

| 模块 | 选型 |
|---|---|
| 数字人服务 | LiveTalking（独立部署，通过 HTTP 调用） |
| Avatar 模型 | wav2lip256 |
| TTS | LiveTalking 内置（默认 EdgeTTS） |
| 传输 | WebRTC（浏览器 ↔ LiveTalking 直连，媒体流不经过 FastAPI） |
| 本地控制层 | FastAPI |
| 本地前端 | 原生 HTML + JavaScript |

## 目录结构

```
app/main.py                FastAPI 入口、/health、静态页挂载
app/config.py              配置（环境变量）
app/schemas.py             请求 / 响应模型
app/api/avatar.py          信令代理与会话控制路由
app/services/digital_human.py   DigitalHumanService 抽象 + LiveTalkingService 实现
static/index.html          原生前端（视频区、输入框、打断按钮、状态显示）
tests/                     测试
docs/                      设计文档
```

## 快速开始

（待 Step 1 完成后补充）

## 开发路线

| 版本 | 内容 | 状态 |
|---|---|---|
| V0.1 | 文字 → LiveTalking → 数字人音视频 → 浏览器 | 进行中 |
| V0.2 | 接入本地 Qwen / Ollama | 未开始 |
| V0.3 | 接入 ASR（语音输入） | 未开始 |

RAG、Agent、数据库、用户系统等暂不纳入计划。
