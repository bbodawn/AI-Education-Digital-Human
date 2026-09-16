# 项目目标

本项目是“AI数字人教育教练”。

当前目标是以 MVP 优先方式逐步完成：

- V0.2：文字提问 → Qwen → TTS → 数字人回答（已完成）
- V0.3：语音输入 → ASR → Qwen → TTS → 数字人回答
- 后续阶段才考虑 RAG、Agent、学习记录等功能

# 当前稳定基线

- stable tag：`v0.2.0-stable`
- baseline commit：`a6191be9b380bfd44c6149726e4aa15648802bf5`
- 基线测试：53 passed

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
- 主动优化现有 1～2 秒数字人播报延迟

# models 目录

`models/` 为本地/外部模型文件目录。

必须：

- 保持 Git ignore
- 不删除
- 不移动
- 不提交 Git

# Git 规则

- `main` 作为稳定基线分支。
- 当前开发分支为 `v0.3`。
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
- README 中部分版本描述过时

这些问题除非当前阶段直接受到影响，否则不得顺手重构。

# V0.3 范围

V0.3 唯一目标：

用户麦克风语音  
→ ASR  
→ 文本  
→ 复用现有 `/api/v1/chat/ask`  
→ Qwen  
→ LiveTalking  
→ 数字人回答

V0.3 不允许同时加入：

- 对话历史
- RAG
- Agent
- 长期记忆
- 数据库
- 流式 TTS
- 新 Avatar 模型
- WebRTC 架构改造

完成后运行 `pytest -q`。
