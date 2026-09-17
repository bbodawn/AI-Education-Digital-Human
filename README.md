# AI 数字人教育教练

一个面向教育场景的本地 AI 数字人问答 MVP。用户可以键入文字，也可以通过浏览器麦克风提问；问题由本地 Qwen 生成回答，再由远程数字人同步播报声音与口型。

当前版本：**V0.3（功能已完成并冻结）**

当前测试基线：**67 passed**

## V0.3 能力

- 文字提问 → Qwen2.5:7b → 数字人回答。
- 浏览器麦克风 → faster-whisper 中文 ASR → 自动提交给现有问答链路。
- 数字人声音播报与 Wav2Lip 口型同步。
- 连续多轮问答与显式 Interrupt。
- 显示本轮 ASR、LLM、数字人起播确认和总响应耗时。

V0.3 不包含 RAG、Agent、数据库、长期记忆、多用户系统、VAD 或流式 ASR/LLM/TTS。

## 完整链路

```text
Browser microphone
→ MediaRecorder
→ FastAPI
→ faster-whisper small / CPU / INT8 / zh
→ transcript
→ existing /api/v1/chat/ask
→ Ollama / Qwen2.5:7b
→ LiveTalking
→ Edge TTS
→ Wav2Lip
→ MediaMTX
→ WebRTC
→ Browser
```

FastAPI 只负责业务控制、音频文件上传和 HTTP 调用，**不作为 WebRTC 音视频流的 relay**。浏览器直接通过 MediaMTX 获取数字人媒体流。

## 技术选型

| 模块 | 选型 |
|---|---|
| 本地业务层 | FastAPI |
| 前端 | 原生 HTML + JavaScript + MediaRecorder |
| ASR | faster-whisper 1.2.1，small，CPU INT8，中文 |
| LLM | Ollama + Qwen2.5:7b，非流式 |
| 数字人 | LiveTalking |
| TTS | Edge TTS |
| 口型 | Wav2Lip |
| 媒体服务 | MediaMTX |
| 传输 | WebRTC over TCP |

## 目录结构

```text
app/main.py                    FastAPI 入口、异常映射与页面入口
app/config.py                  环境变量配置
app/api/asr.py                 音频上传与转写 API
app/api/chat.py                问答、播报、打断与 latency metrics
app/api/avatar.py              保留的 WebRTC 信令代理 API
app/services/asr.py            ASR 抽象与 faster-whisper 实现
app/services/llm.py            LLM 抽象与 Ollama/Qwen 实现
app/services/digital_human.py  数字人抽象与 LiveTalking 实现
static/index.html              麦克风、问答、数字人与耗时 UI
scripts/                       一键启停与 SSH 隧道
tests/                         自动化测试
models/                        本地 ASR 模型缓存（Git ignored）
temp/                          本地测试素材（Git ignored）
```

## 快速开始

### 1. 创建项目虚拟环境

在项目根目录执行：

```powershell
D:\Anaconda3_2024\Anaconda3\python.exe -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

项目启动与测试固定使用 `.venv`，不依赖 Windows user-site。

### 2. 准备本地 Qwen

```powershell
ollama pull qwen2.5:7b
```

启动器会检查 Ollama 和模型，但不会自动下载 Qwen。

### 3. 配置 AutoDL

按 [scripts/README.md](scripts/README.md) 在用户目录创建 `~/.autodl_env`。凭据不得放入项目或提交到 Git。

### 4. 启动与停止

先在 AutoDL 控制台启动实例，再执行：

```powershell
scripts\start.bat
```

启动成功后会自动打开 <http://localhost:8000/>。

停止项目记录的进程：

```powershell
scripts\stop.bat
```

`stop.bat` 不会停止 Ollama，也不会关闭 AutoDL 实例。

### 5. 运行测试

```powershell
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m pytest -q
```

当前基线：`67 passed`。

## API

| 方法 | 路径 | 职责 |
|---|---|---|
| GET | `/` | 前端页面 |
| GET | `/health` | FastAPI 健康检查 |
| POST | `/api/v1/avatar/webrtc/offer` | 保留的 WebRTC 信令代理 |
| POST | `/api/v1/chat/say` | 直接让数字人朗读文本 |
| POST | `/api/v1/chat/interrupt` | 显式打断播报 |
| POST | `/api/v1/chat/ask` | Qwen 生成回答并交给数字人 |
| POST | `/api/v1/asr/transcribe` | 上传音频并返回 ASR 文本 |

## 响应耗时指标

页面显示本轮：

- **语音识别**：用户停止录音到 transcript 在前端可用。
- **AI 回答生成**：后端调用 `llm.chat()` 到完整 answer 返回。
- **数字人起播确认**：Qwen answer ready 到后端首次观察到 `speaking=true`。
- **总响应**：用户停止录音（文字场景为提交）到起播已确认的前端体感耗时。

Total 是独立的用户体验指标，不强制等于三个阶段数值之和。上一轮仍在播报或本轮无法确认起播时，起播与 Total 显示 `—`。

## Known Limitations

1. faster-whisper small 在中文场景中仍会偶发误识别。
2. ASR 偶发输出繁体中文，当前没有 OpenCC 规范化。
3. 静音或环境噪声可能生成非空 hallucinated transcript；当前没有 VAD/no-speech filtering。
4. 使用电脑扬声器外放时存在 speaker-to-microphone echo/crosstalk 风险，可能降低 ASR 准确率。
5. Interrupt 最终有效，但实际停止播报可能存在约 2 秒残余延迟。
6. `speaking=true` 只是服务端起播确认 proxy，不等于浏览器或声卡真正开始发声的时刻。
7. 当前是 single-browser/single-session MVP，没有多用户会话隔离。
8. Phase 4 Case 7 异常/失败输入尚未完成人工覆盖。

## 已知内部技术债

- `session_id` 为单进程全局单例；本地无缓存时依赖 LiveTalking sessions 最后一项。
- Interrupt 不会真正取消已经开始的 Ollama 推理。
- Ollama 当前为非流式调用。
- 没有真实浏览器、Ollama、LiveTalking 和 MediaMTX 的端到端自动化测试；完整链路依赖人工验收。

## 版本路线

| 版本 | 内容 | 状态 |
|---|---|---|
| V0.1 | 文字朗读、数字人音视频与浏览器播放 | 已完成 |
| V0.2 | 本地 Ollama/Qwen 问答 | 已完成 |
| V0.3 | 麦克风、ASR、自动问答闭环与 latency metrics | 已完成并冻结 |

后续阶段只在明确任务要求后开始，不在 V0.3 封板中扩展 RAG、Agent、数据库或其他能力。
