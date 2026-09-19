# 项目目标

本项目是“AI数字人教育教练”。

项目以 MVP 优先方式逐步完成：

- V0.2：文字提问 → Qwen → TTS → 数字人回答（已完成）
- V0.3：语音输入 → ASR → Qwen → TTS → 数字人回答（已完成并冻结）
- V0.3 Phase 4：ASR / LLM / 数字人起播与总响应耗时展示（已完成）
- V0.4：最近 5 轮短期多轮上下文与新对话（已完成并冻结）
- V0.5：Tutor 教学模式、文本/语音教学与可滚动 Teaching Timeline（已完成，发布候选）
- 未经明确任务指示，不得进入 RAG、Agent、学习记录等后续功能

# 当前版本状态

- 当前稳定候选基线为 V0.5；功能验收已通过，发布提交与标签由明确指令执行。
- 已发布稳定标识：`v0.4.0-stable`；历史回滚基线：`v0.2.0-stable` / `bb493512744ad42183b4936b19589ba6c17f2a11`。
- 当前测试基线：133 passed。

# 开发原则

1. MVP 优先，不做过度设计。
2. 每个阶段只解决当前阶段的问题。
3. 不主动扩展 RAG、Agent、数据库、多用户系统等功能。
4. 修改前先理解现有实现，不重复造轮子。
5. 优先复用现有抽象和模块。
6. 不为了“代码更漂亮”进行无关重构。
7. 每次修改后必须运行相关测试。
8. 重要改动完成后运行完整 `pytest -q`。

# 架构边界

当前主链路：

Browser  
→ FastAPI  
→ Ollama/Qwen  
→ LiveTalking  
→ Edge TTS  
→ Wav2Lip  
→ MediaMTX  
→ WebRTC  
→ Browser

业务控制与音视频媒体传输保持分离。

FastAPI 不负责转发 WebRTC 音视频流。

# 禁止事项

除非任务明确要求，否则禁止：

- 修改 LiveTalking 源码
- 修改 MediaMTX
- 修改现有 WebRTC 传输方案
- 修改 Wav2Lip 模型
- 修改 Edge TTS
- 删除或移动 `models/`
- 将 `models/` 提交 Git
- 修改 AutoDL 系统配置
- 引入 LangGraph
- 引入向量数据库
- 引入 RAG
- 引入 Agent
- 引入数据库
- 大规模重构
- 主动优化现有数字人播报或打断延迟
- 在 V0.6 之前实现自定义 Avatar 或 Voice Clone；V0.6 的声音方案须经真实 benchmark 选型，不预设 GPT-SoVITS

# models 目录

`models/` 为本地/外部模型文件目录。

必须：

- 保持 Git ignore
- 不删除
- 不移动
- 不提交 Git

# Git 规则

- `main` 作为稳定基线分支。
- V0.3 发布分支为 `v0.3`，功能冻结后不得继续添加新功能。
- V0.5 的提交、stable tag 与推送均须遵循明确发布指令。
- 不允许未经明确指示直接修改 stable tag。
- 不允许自动执行 `git reset --hard`、`git clean -fd`、强制 push 等破坏性操作。
- Commit 前必须先查看 `git diff` 和测试结果。

# 当前技术债

当前已知但暂不主动解决：

- session_id 为单进程单例
- session 选择依赖 sessions 最后一项
- interrupt 不真正取消 Ollama 推理
- Ollama 非流式
- 无多用户隔离
- 无完整端到端自动化测试
- launcher 存在环境和远端状态管理技术债
- ASR 偶发误识别或输出繁体中文
- 静音/噪声可能生成非空 transcript，当前没有 VAD/no-speech filtering
- 外放场景存在 echo/crosstalk 风险
- Interrupt 实际停止播报存在约 2～3 秒残余延迟
- `speaking=true` 是服务端起播确认 proxy，不等同于浏览器真实发声时刻
- “新对话”主要 reset 状态，不保证立即停止已经开始的数字人口播
- Tutor → Chat 后，普通 Qwen 在无历史时可能虚构过去学习内容；这是模型幻觉，不是 Tutor history 泄漏
- Tutor Timeline 仅为浏览器会话级临时状态，刷新页面后不持久化
- ConversationStore 只向 LLM 提供最近 5 个完整 turn；前端 Tutor Timeline 可展示当前页面会话的完整教学记录
- 当前仅为单浏览器 / 单 session MVP，不属于生产级多用户隔离

这些问题除非当前阶段直接受到影响，否则不得顺手重构。

# V0.5 范围与回归边界

V0.3 已完成并冻结的语音主链路：

用户麦克风语音  
→ ASR  
→ 文本  
→ 复用现有 `/api/v1/chat/ask`  
→ Qwen  
→ LiveTalking  
→ 数字人回答

V0.5 在此基础上增加短期多轮上下文、TutorState、Tutor API、文本/语音教学与可滚动 Teaching Timeline。后续修改不得破坏 ConversationStore、TutorState、Tutor API 的成功后提交语义、现有 interrupt epoch，以及 LiveTalking / MediaMTX / WebRTC 的业务与媒体边界。

除非后续阶段明确授权，仍不补入：

- RAG
- Agent
- 长期记忆
- 数据库
- 流式 TTS
- 新 Avatar 模型
- WebRTC 架构改造

V0.6 仅规划探索自定义 Avatar 与 Voice Clone；Edge TTS、CosyVoice 3、GPT-SoVITS 应先做真实 benchmark，再决定声音克隆方案，不得默认采用 GPT-SoVITS。

任何后续修改都必须运行相关测试；重要改动必须运行完整 `pytest -q`。
