# 一键启动

把「启动这套系统」从一堆手工命令变成双击一次。

## 使用

**启动 AutoDL 实例后，双击 `scripts\start.bat`，服务启动成功后会自动打开浏览器进入 <http://localhost:8000/>。**

```
① 在 AutoDL 控制台启动实例
② 双击 scripts\start.bat
③ 等待六项检查全部通过（约 30 秒）
④ 看到「AI数字人教育教练启动成功」
⑤ 浏览器自动打开 http://localhost:8000/
```

关闭时双击 `scripts\stop.bat` 即可。

> 自动打开浏览器只是启动成功后的最后一个动作。**浏览器没打开、没有默认浏览器、
> 被系统策略拦下，都不算启动失败**——终端里始终会打印出地址，手动访问即可。

也可以在终端里用：

```powershell
.venv\Scripts\python.exe scripts\launcher.py start
.venv\Scripts\python.exe scripts\launcher.py stop
```

> `scripts/launcher.py` 同时承担「常驻的 SSH 端口转发进程」这一内部角色，
> 由 `start` 以独立进程拉起，不需要（也不应该）手动运行。

## 它会启动什么

```
Windows 本地                                   AutoDL
┌──────────────────────────────┐              ┌──────────────────────────┐
│ Ollama        :11434         │              │ LiveTalking    :8010     │
│ FastAPI       :8000          │              │ MediaMTX       :8889     │
│ SSH 隧道 8010/8889/8189 ─────┼─── SSH ─────>│                :8189     │
└──────────────────────────────┘              └──────────────────────────┘
                                                          │
浏览器 ←──── WebRTC over TCP (localhost:8189) ─────────────┘
```

启动顺序与检查项：

| 步骤 | 检查 | 未就绪时的行为 |
|---|---|---|
| 1/6 | Ollama `:11434` + 模型是否存在 | 尝试启动 Ollama；**不会自动下载模型** |
| 2/6 | AutoDL SSH 可连接 | 提示「请确认实例已开机」 |
| 3/6 | AutoDL LiveTalking `:8010` | 未运行则拉起，等待就绪 |
| 4/6 | AutoDL MediaMTX `:8889` `:8189` | 未运行则拉起，等待就绪 |
| 5/6 | SSH 隧道三条端口 | 未通则建立；有残留则先清理再重建 |
| 6/6 | FastAPI `:8000` + `/health` | 未运行则拉起，等待 `/health` 正常 |

任一步失败就**立刻停下**，不会带着半截状态继续往后跑。

## 前置条件

**1. AutoDL 连接信息**

启动器从 `~/.autodl_env`（**项目目录之外**）读取，脚本与仓库里不出现任何凭据：

```ini
# C:\Users\<你的用户名>\.autodl_env
AUTODL_HOST=connect.xxxx.seetacloud.com
AUTODL_PORT=12345
AUTODL_USER=root
AUTODL_PASSWORD=你的实例密码
```

> ⚠️ 这个文件**绝不能**放进项目目录，更不能提交到 Git。
> AutoDL 的 SSH 地址和端口**每次重建实例都会变**，换实例后需要更新这里。

**2. 本地 Python 环境（首次运行）**

在项目根目录执行：

```powershell
D:\Anaconda3_2024\Anaconda3\python.exe -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

此后日常启动只需双击 `scripts\start.bat`。启动器固定使用项目的
`.venv\Scripts\python.exe`；若 `.venv` 不存在，会明确报错，不会回退到 PATH 中的 Python。

运行测试：

```powershell
.venv\Scripts\python.exe -m pytest -q
```

**3. 本地模型**

```bash
ollama pull qwen2.5:7b
```

启动器只检查模型在不在，**不会替你下载**。

## 如何判断「已经在运行」

启动器不靠进程名判断，而是**问服务本身**：

| 服务 | 判据 |
|---|---|
| Ollama | `GET /api/tags` 返回 200，且目标模型在列表里 |
| AutoDL LiveTalking | `GET /api/admin/config` 返回 200，**且 `pgrep` 到进程** |
| AutoDL MediaMTX | `8889` 与 `8189` 都在监听，**且 `pgrep` 到进程** |
| SSH 隧道 | 三个端口都在，**且能通过 `localhost:8889/avatar/` 取到 MediaMTX 的页面** |
| FastAPI | `GET /health` 返回 `{"status": "ok"}` |

只看端口是不够的——端口可能被别的东西占着。所以每一条都额外要求**服务能正确响应**。

## 如何避免重复启动

- 每一项**先检查、再启动**：检查通过就跳过，不会拉起第二个实例。
- FastAPI 启动前额外判断：若 `:8000` 被占但 `/health` 不是本项目的响应，**直接报错**，不会盲目抢占端口。
- 隧道有残留（端口被占但不通）时，**先停掉再重建**，而不是硬起一个重复的。
- AutoDL 上的服务用 `pgrep` 判定，已经跑着就只检查、不重复拉起。

## stop 的安全边界

`stop.bat` **只结束启动器自己记录过的 PID**（记录在 `scripts/.runtime.json`）。它**不会**：

- 按进程名批量杀 Python / SSH（那会误伤你自己的进程）
- 停止 Ollama（那是共享服务，你可能还有别的用途）
- 关闭 AutoDL 实例
- 删除模型或任何文件

AutoDL 上的 LiveTalking / MediaMTX **只按启动时记录的 PID 停止**。如果它们不是由 `start.bat` 拉起的（或记录已丢失），启动器会**明确提示你手动停止**，而不是猜着杀。

## 排障

| 现象 | 看哪里 |
|---|---|
| 启动失败 | 终端里的 `[ERROR]` 说明 |
| FastAPI 起不来 | `scripts/logs/fastapi.log` |
| 隧道不通 | `scripts/logs/tunnel.log` |
| Ollama 起不来 | `scripts/logs/ollama.log` |
| AutoDL 服务起不来 | 远端 `/root/livetalking.log`、`/root/mediamtx.log` |

`scripts/logs/` 与 `scripts/.runtime.json` 都在 `.gitignore` 里，不会进仓库。
