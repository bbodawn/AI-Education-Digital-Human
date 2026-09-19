# AI 数字人教育教练

一个面向教育场景的本地 AI 数字人教育教练 MVP。用户可以通过文字或浏览器麦克风进行普通聊天，也可以选择主题进入引导问答教学；本地 Qwen 生成内容，远程数字人同步播报声音与口型。

当前稳定目标版本：**V0.5（功能验收已通过）**

当前测试基线：**133 passed**

## V0.5 能力

- 文字聊天 → Qwen2.5:7b → 数字人回答；浏览器麦克风 → faster-whisper 中文 ASR → 自动提交到当前模式。
- 本地 Ollama / Qwen2.5:7b；ConversationStore 提供最近 5 个完整 turn 的短期多轮上下文。
- 教学模式以 TutorState 维护主题、当前问题和题号，支持文本或语音回答、评价、解释与下一题。
- Tutor Teaching Timeline 保留当前页面会话中的教学轮次；Scrollable Classroom 让教学记录独立滚动、数字人播放器保持可见。
- 数字人声音播报与 Wav2Lip 口型同步。
- 新对话、Tutor reset、普通聊天与教学模式切换，以及显式 Interrupt。
- 显示本轮 ASR、LLM、数字人起播确认和总响应耗时。

V0.5 不包含 RAG、Agent、数据库、长期记忆、生产级多用户系统、VAD 或流式 ASR/LLM/TTS。

## 完整链路

```text
Browser microphone
→ MediaRecorder
→ FastAPI
→ faster-whisper small / CPU / INT8 / zh
→ transcript
→ /api/v1/chat/ask（普通聊天）或 /api/v1/tutor/answer（教学回答）
→ Ollama / Qwen2.5:7b
→ LiveTalking
→ Edge TTS
→ Wav2Lip
→ MediaMTX
→ WebRTC
→ Browser
```

FastAPI 只负责业务控制、音频文件上传和 HTTP 调用，**不作为 WebRTC 音视频流的 relay**。浏览器直接通过 MediaMTX 获取数字人媒体流。

教学模式由 `/api/v1/tutor/start` 生成第一题；`/api/v1/tutor/answer` 使用 TutorState 和短期上下文评价回答并提出下一题。Tutor Timeline 是前端展示状态，不是持久化学习记录。

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
app/api/tutor.py               教学开始、回答与重置 API
app/api/avatar.py              保留的 WebRTC 信令代理 API
app/conversation.py            最近 5 个完整 turn 的进程内对话上下文
app/tutor.py                   TutorState、教学 Prompt 与解析器
app/services/asr.py            ASR 抽象与 faster-whisper 实现
app/services/llm.py            LLM 抽象与 Ollama/Qwen 实现
app/services/digital_human.py  数字人抽象与 LiveTalking 实现
static/index.html              麦克风、教学时间线、数字人与耗时 UI
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

当前基线：`133 passed`。

## API

| 方法 | 路径 | 职责 |
|---|---|---|
| GET | `/` | 前端页面 |
| GET | `/health` | FastAPI 健康检查 |
| POST | `/api/v1/avatar/webrtc/offer` | 保留的 WebRTC 信令代理 |
| POST | `/api/v1/chat/say` | 直接让数字人朗读文本 |
| POST | `/api/v1/chat/interrupt` | 显式打断播报 |
| POST | `/api/v1/chat/ask` | Qwen 生成回答并交给数字人 |
| POST | `/api/v1/chat/reset` | 清空当前会话短期聊天上下文 |
| POST | `/api/v1/tutor/start` | 选择主题并生成第一道教学问题 |
| POST | `/api/v1/tutor/answer` | 评价回答、解释并提出下一题 |
| POST | `/api/v1/tutor/reset` | 清空当前教学状态及其对话上下文 |
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
5. Interrupt 最终有效，但实际停止播报可能存在约 2～3 秒残余延迟。
6. `speaking=true` 只是服务端起播确认 proxy，不等于浏览器或声卡真正开始发声的时刻。
7. “新对话”主要负责 reset 状态，不保证立即停止已经开始的数字人口播。
8. Tutor → Chat reset 后，普通 Qwen 在没有历史时可能虚构过去学习内容；这是模型幻觉，不是 Tutor history 泄漏。
9. Tutor Timeline 是浏览器会话级临时状态，页面刷新后不持久化。
10. ConversationStore 只向 LLM 提供最近 5 个完整 turn；前端 Tutor Timeline 可显示本次页面会话中的完整教学记录。
11. 当前是 single-browser/single-session MVP，不属于生产级多用户隔离。
12. Phase 4 Case 7 异常/失败输入尚未完成人工覆盖。

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
| V0.4 | 最近 5 轮短期多轮上下文与新对话 | 已完成并冻结 |
| V0.5 | 教学模式、TutorState、文本/语音教学与可滚动教学时间线 | 功能验收已通过 |
| V0.6 | 探索自定义 Avatar、Voice Clone；对 Edge TTS、CosyVoice 3、GPT-SoVITS 做真实 benchmark 后选型 | 规划中，未实现 |

后续阶段只在明确任务要求后开始；V0.6 的 Avatar 与 Voice Clone 当前均未实现。
