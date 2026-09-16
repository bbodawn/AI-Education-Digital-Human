"""AI数字人教育教练 —— 一键启动 / 停止。

职责只有一个：把「启动这套系统」这件事变成一条命令。

    本地 Ollama :11434      ← 本地大模型
    本地 FastAPI :8000      ← 业务控制层
    SSH 隧道 8010/8889/8189 ← 通往 AutoDL
    AutoDL LiveTalking :8010 + MediaMTX :8889/:8189

设计原则：
  * **先检查再启动**。任何一步已经就绪就跳过，绝不重复拉起第二个实例。
  * **凭据不落项目**。连接信息读 ~/.autodl_env（位于项目外，不进 Git）。
  * **只停自己启动的**。PID 记在 .runtime.json，stop 时只按记录杀，
    不会误伤用户自己的 Python / Ollama / SSH 进程。
  * **不碰浏览器**。开不开浏览器不该影响启动是否成功。

用法（用户只需要前两条，通常通过 start.bat / stop.bat 调用）：
    python scripts/launcher.py start
    python scripts/launcher.py stop
    python scripts/launcher.py tunnel     # 内部用：常驻的端口转发进程
"""

from __future__ import annotations

import json
import os
import pathlib
import select
import socket
import subprocess
import sys
import threading
import time

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
RUNTIME_FILE = SCRIPT_DIR / ".runtime.json"
LOG_DIR = SCRIPT_DIR / "logs"

# 连接信息放在项目之外：里面是 AutoDL 的 SSH 密码，绝不能进 Git
AUTODL_ENV_FILE = pathlib.Path.home() / ".autodl_env"

OLLAMA_HOST = "127.0.0.1"
OLLAMA_PORT = 11434
FASTAPI_PORT = 8000

# 三条 SSH 隧道，以及每条转发到的远端地址
FORWARDS = [
    (8010, "127.0.0.1", 8010),  # LiveTalking HTTP API（/whep、/human）
    (8889, "127.0.0.1", 8889),  # MediaMTX WHEP 信令 + 播放页
    (8189, "127.0.0.1", 8189),  # MediaMTX WebRTC over TCP 媒体口
]
LOCAL_PORTS = [local for local, _, _ in FORWARDS]

REMOTE_LIVETALKING_DIR = "/root/autodl-tmp/LiveTalking"
REMOTE_LIVETALKING_PY = "/root/miniconda3/envs/livetalking/bin/python"
REMOTE_LIVETALKING_LOG = "/root/livetalking.log"
REMOTE_MEDIAMTX_BIN = "/root/mediamtx/mediamtx"
REMOTE_MEDIAMTX_CFG = "/root/mediamtx/mediamtx-custom.yml"
REMOTE_MEDIAMTX_LOG = "/root/mediamtx.log"

WAIT_TIMEOUT = 90  # 单个服务等待就绪的上限（秒）

TUNNEL_BUFSIZE = 65536
TUNNEL_KEEPALIVE = 30
TUNNEL_RECONNECT_DELAY = 5


# --------------------------------------------------------------------------
# 终端输出
# --------------------------------------------------------------------------


def ok(msg: str) -> None:
    print(f"  [OK] {msg}", flush=True)


def info(msg: str) -> None:
    print(f"       {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"  [ERROR] {msg}", flush=True)


def title(text: str) -> None:
    print("=" * 44, flush=True)
    print(f" {text}", flush=True)
    print("=" * 44, flush=True)


class LauncherError(Exception):
    """启动过程中的可预期失败 —— 打印原因后直接停下，不继续往下走。"""


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """端口是否有人监听。只做 TCP 连接，不发任何数据。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host: str, port: int, timeout: int = WAIT_TIMEOUT) -> bool:
    """轮询等待端口就绪。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(host, port):
            return True
        time.sleep(1)
    return False


def http_get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    """发一个 GET，返回 (状态码, 正文)。失败时状态码为 0。"""
    import httpx

    try:
        # trust_env=False：绕开 Windows 系统代理，否则连 127.0.0.1 也会被送进代理
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.get(url)
            return response.status_code, response.text
    except Exception:
        return 0, ""


def spawn_detached(args: list[str], log_path: pathlib.Path, cwd: pathlib.Path | None = None) -> int:
    """以脱离父进程的方式启动一个子进程，返回 PID。

    DETACHED_PROCESS 让子进程不随本启动器退出而结束——
    否则用户关掉黑窗口，服务就全没了。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8", errors="replace")

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    process = subprocess.Popen(
        args,
        cwd=str(cwd) if cwd else None,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    return process.pid


def kill_pid(pid: int) -> bool:
    """只结束指定 PID。找不到或已退出都返回 False，不报错。"""
    if not pid:
        return False
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0
        os.kill(pid, 15)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# 运行状态（记录「哪些是本启动器拉起来的」，供 stop 精确回收）
# --------------------------------------------------------------------------


def load_runtime() -> dict:
    if RUNTIME_FILE.exists():
        try:
            return json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def save_runtime(state: dict) -> None:
    RUNTIME_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# AutoDL 连接
# --------------------------------------------------------------------------


def load_autodl_config() -> dict[str, str]:
    if not AUTODL_ENV_FILE.exists():
        raise LauncherError(
            f"缺少连接信息文件 {AUTODL_ENV_FILE}\n"
            "       请按 scripts/README.md 的说明配置 AutoDL 的 SSH 地址、端口与密码。"
        )

    config = {}
    for line in AUTODL_ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        config[key.strip()] = value.strip()

    missing = [k for k in ("AUTODL_HOST", "AUTODL_PORT", "AUTODL_PASSWORD") if not config.get(k)]
    if missing:
        raise LauncherError(f"{AUTODL_ENV_FILE} 缺少字段：{', '.join(missing)}")
    return config


def open_ssh_transport(config: dict[str, str]):
    """建立一条 SSH 连接。失败时给出「实例可能没启动」这种能行动的提示。"""
    import paramiko

    transport = paramiko.Transport((config["AUTODL_HOST"], int(config["AUTODL_PORT"])))
    try:
        transport.connect(
            username=config.get("AUTODL_USER", "root"),
            password=config["AUTODL_PASSWORD"],
        )
    except Exception as exc:
        raise LauncherError(
            f"无法连接 AutoDL（{type(exc).__name__}）\n"
            "       请确认实例已经开机，且控制台里的 SSH 地址/端口没有变化。"
        ) from exc
    return transport


def run_remote(transport, command: str, timeout: int = 120) -> tuple[str, int]:
    """在远端执行一条命令，返回 (输出, 退出码)。

    注意：这里用的是 Transport 而不是 SSHClient，所以得自己开 session 通道
    （SSHClient.exec_command 是对它的封装）。
    """
    channel = transport.open_session(timeout=timeout)
    try:
        channel.settimeout(timeout)
        channel.exec_command(command)
        out = channel.makefile("rb").read().decode("utf-8", errors="replace")
        err = channel.makefile_stderr("rb").read().decode("utf-8", errors="replace")
        code = channel.recv_exit_status()
    finally:
        channel.close()
    return out + err, code


def run_remote_detached(transport, command: str, settle: float = 3.0) -> None:
    """执行一条「拉起常驻进程」的命令，不等它结束。

    为什么不等：nohup 拉起的后台进程会一直持有 SSH 通道的输出管道，
    read() 要一直等到 EOF，而那要等到进程退出——等于永远等下去。
    （实测中这一步会卡满超时，看起来像启动失败，其实服务早就起来了。）

    所以启动是否成功，靠后续轮询端口来判断，不靠这条命令的返回值。
    """
    channel = transport.open_session(timeout=20)
    try:
        channel.exec_command(command)
        time.sleep(settle)  # 给它一点时间把进程拉起来
    finally:
        channel.close()


def remote_service_status(transport) -> dict[str, bool]:
    """检查 AutoDL 上两个服务是否在跑。

    只用 ssh / ps / curl / grep —— 刻意不调用 python。
    远端是非登录 shell，conda 的 PATH 不在其中（`python` 会 command not found），
    之前就是因为这个，端口检查静默失败、把在跑的服务误报成没跑。

    PID 必须取「真正的服务进程」，不能用 pgrep -f 直接抓命令行：
    拉起服务时外面还套了一层 `bash -c cd ... && nohup ... app.py ...`，
    它的命令行同样含有 app.py，pgrep 会先匹配到它。杀掉这层 shell 只会
    留下一个没人管的 python 子进程（实测中 stop 之后 LiveTalking 仍在跑）。
    因此用 comm 字段限定必须是 python / mediamtx 本体。
    """
    script = (
        "echo LTPID=$(ps -eo pid,comm,args --no-headers "
        "| awk '$2 ~ /python/ && /app[.]py/ {print $1; exit}'); "
        "curl -s -o /dev/null -w 'LTHTTP=%{http_code}\\n' --max-time 5 "
        "http://127.0.0.1:8010/api/admin/config; "
        "echo MTXPID=$(ps -eo pid,comm,args --no-headers "
        "| awk '$2 ~ /mediamtx/ {print $1; exit}'); "
        "curl -s -o /dev/null -w 'MTXHTTP=%{http_code}\\n' --max-time 5 "
        "http://127.0.0.1:8889/avatar/; "
        # 8189 是裸 ICE/TCP 端口，没有 HTTP 可说，只能从 /proc 看监听状态。
        # MediaMTX 是 Go 程序，默认绑双栈，所以 tcp6 也要查。
        "echo PORT8189="
        "$(grep -q ':1FFD ' /proc/net/tcp6 /proc/net/tcp 2>/dev/null && echo 1 || echo 0)"
    )
    out, _code = run_remote(transport, script)

    def field(name: str) -> str:
        for line in out.splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
        return ""

    return {
        "livetalking_pid": int(field("LTPID") or 0),
        "livetalking_http": field("LTHTTP") == "200",
        "mediamtx_pid": int(field("MTXPID") or 0),
        "mediamtx_8889": field("MTXHTTP") == "200",
        "mediamtx_8189": field("PORT8189") == "1",
    }


# --------------------------------------------------------------------------
# 步骤 1：Ollama
# --------------------------------------------------------------------------


def step_ollama(state: dict, model: str) -> None:
    print("[1/6] 检查 Ollama", end="", flush=True)

    if not port_open(OLLAMA_HOST, OLLAMA_PORT):
        info("未运行，尝试启动…")
        try:
            spawn_detached(["ollama", "serve"], LOG_DIR / "ollama.log")
        except FileNotFoundError as exc:
            raise LauncherError(
                "找不到 ollama 命令\n"
                "       请先安装 Ollama 并确保它在 PATH 中，或手动启动 Ollama 后重试。"
            ) from exc

        if not wait_for_port(OLLAMA_HOST, OLLAMA_PORT, timeout=30):
            raise LauncherError(
                "Ollama 启动后 30 秒内仍未监听 11434\n"
                "       请手动启动 Ollama（桌面应用或 `ollama serve`）后重试。"
            )

    status, body = http_get(f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/tags")
    if status != 200:
        raise LauncherError(f"Ollama 接口异常（HTTP {status}）")

    try:
        names = [m.get("name", "") for m in json.loads(body).get("models", [])]
    except ValueError as exc:
        raise LauncherError("Ollama 返回的不是合法 JSON") from exc

    if model not in names:
        raise LauncherError(
            f"模型 {model} 不存在（现有：{', '.join(names) or '无'}）\n"
            f"       启动器不会自动下载模型。请手动执行：ollama pull {model}"
        )

    print(" .............. OK", flush=True)
    info(f"模型 {model} 已就绪")


# --------------------------------------------------------------------------
# 步骤 2-5：AutoDL（连接、服务、隧道）
# --------------------------------------------------------------------------


def step_autodl_connect(state: dict) -> tuple[dict, object]:
    print("[2/6] 检查 AutoDL SSH", end="", flush=True)
    config = load_autodl_config()
    transport = open_ssh_transport(config)
    print(" ......... OK", flush=True)
    info(f"{config.get('AUTODL_USER', 'root')}@{config['AUTODL_HOST']}:{config['AUTODL_PORT']}")
    return config, transport


def step_remote_services(state: dict, transport) -> None:
    print("[3/6] 检查 LiveTalking", end="", flush=True)
    status = remote_service_status(transport)

    if status["livetalking_http"] and status["livetalking_pid"]:
        print(" ...... OK", flush=True)
        info(f"已在运行（PID {status['livetalking_pid']}），跳过启动")
    else:
        info("未运行，正在启动…")
        cmd = (
            f"cd {REMOTE_LIVETALKING_DIR} && "
            f"setsid nohup env OMP_NUM_THREADS=8 {REMOTE_LIVETALKING_PY} app.py "
            "--transport webrtc --model wav2lip --avatar_id wav2lip256_avatar1 "
            f"> {REMOTE_LIVETALKING_LOG} 2>&1 < /dev/null &"
        )
        run_remote_detached(transport, cmd)
        # 模型与素材加载要时间，逐步轮询而不是死等一个固定值
        for _ in range(WAIT_TIMEOUT):
            time.sleep(2)
            status = remote_service_status(transport)
            if status["livetalking_http"]:
                break
        else:
            raise LauncherError(
                "LiveTalking 启动后仍未就绪\n"
                f"       请查看远端日志：{REMOTE_LIVETALKING_LOG}"
            )
        print(" ...... OK", flush=True)
        info(f"已启动（PID {status['livetalking_pid']}）")
        state.setdefault("remote_started", []).append(
            {"name": "livetalking", "pid": status["livetalking_pid"]}
        )

    print("[4/6] 检查 MediaMTX", end="", flush=True)
    if status["mediamtx_pid"] and status["mediamtx_8889"] and status["mediamtx_8189"]:
        print(" ......... OK", flush=True)
        info(f"已在运行（PID {status['mediamtx_pid']}），跳过启动")
    else:
        info("未运行，正在启动…")
        cmd = (
            f"setsid nohup {REMOTE_MEDIAMTX_BIN} {REMOTE_MEDIAMTX_CFG} "
            f"> {REMOTE_MEDIAMTX_LOG} 2>&1 < /dev/null &"
        )
        run_remote_detached(transport, cmd)
        for _ in range(20):
            time.sleep(1)
            status = remote_service_status(transport)
            if status["mediamtx_8889"] and status["mediamtx_8189"]:
                break
        else:
            raise LauncherError(
                "MediaMTX 启动后 8889/8189 仍未监听\n"
                f"       请查看远端日志：{REMOTE_MEDIAMTX_LOG}"
            )
        print(" ......... OK", flush=True)
        info(f"已启动（PID {status['mediamtx_pid']}）")
        state.setdefault("remote_started", []).append(
            {"name": "mediamtx", "pid": status["mediamtx_pid"]}
        )


def tunnel_alive() -> bool:
    """三条隧道是否都已打通。

    只检查端口不够——端口可能被别的东西占着。这里额外要求 MediaMTX 的
    播放页能通过隧道取到，才算「隧道确实是通的」。
    """
    if not all(port_open("127.0.0.1", p) for p in LOCAL_PORTS):
        return False
    status, _ = http_get("http://127.0.0.1:8889/avatar/", timeout=5)
    return status == 200


def step_tunnel(state: dict, config: dict[str, str]) -> None:
    print("[5/6] 检查 SSH Tunnel", end="", flush=True)

    if tunnel_alive():
        print(" ...... OK", flush=True)
        info("三条隧道均可用，跳过建立")
        return

    # 端口被占但不通（比如上次残留的进程），先清掉再重建
    if any(port_open("127.0.0.1", p) for p in LOCAL_PORTS):
        info("发现残留隧道，正在重建…")
        kill_pid(state.get("tunnel_pid", 0))
        time.sleep(2)

    info("正在建立隧道…")
    pid = spawn_detached(
        [sys.executable, str(SCRIPT_DIR / "launcher.py"), "tunnel"],
        LOG_DIR / "tunnel.log",
        cwd=PROJECT_DIR,
    )
    state["tunnel_pid"] = pid

    if not wait_for_port("127.0.0.1", 8010, timeout=20):
        raise LauncherError(
            "SSH 隧道建立失败（localhost:8010 未就绪）\n"
            "       请查看日志：scripts/logs/tunnel.log"
        )

    for _ in range(15):
        if tunnel_alive():
            break
        time.sleep(1)
    else:
        raise LauncherError(
            "隧道端口已监听，但无法通过它访问 MediaMTX\n"
            "       请查看日志：scripts/logs/tunnel.log"
        )

    print(" ...... OK", flush=True)
    info(f"已建立（PID {pid}）")


# --------------------------------------------------------------------------
# 步骤 6：本地 FastAPI
# --------------------------------------------------------------------------


def fastapi_alive() -> bool:
    status, body = http_get(f"http://127.0.0.1:{FASTAPI_PORT}/health", timeout=3)
    if status != 200:
        return False
    try:
        return json.loads(body).get("status") == "ok"
    except ValueError:
        return False


def step_fastapi(state: dict) -> None:
    print("[6/6] 检查 FastAPI", end="", flush=True)

    if fastapi_alive():
        print(" .......... OK", flush=True)
        info("已在运行，跳过启动")
        return

    if port_open("127.0.0.1", FASTAPI_PORT):
        raise LauncherError(
            f"端口 {FASTAPI_PORT} 已被占用，但 /health 不是本项目的响应\n"
            f"       请先释放该端口，或修改 launcher.py 中的 FASTAPI_PORT。"
        )

    info("未运行，正在启动…")
    pid = spawn_detached(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(FASTAPI_PORT)],
        LOG_DIR / "fastapi.log",
        cwd=PROJECT_DIR,
    )
    state["fastapi_pid"] = pid

    for _ in range(30):
        time.sleep(1)
        if fastapi_alive():
            break
    else:
        raise LauncherError(
            "FastAPI 启动失败或 /health 无响应\n"
            "       请查看日志：scripts/logs/fastapi.log"
        )

    print(" .......... OK", flush=True)
    info(f"已启动（PID {pid}）")


# --------------------------------------------------------------------------
# 隧道守护进程（由 start 以独立进程拉起，用户不会直接用到）
# --------------------------------------------------------------------------


def tunnel_log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def tunnel_pump(chan, sock: socket.socket) -> None:
    """在 SSH 通道与本地 socket 之间双向搬运数据。"""
    try:
        while True:
            readable, _, _ = select.select([sock, chan], [], [])
            if sock in readable:
                data = sock.recv(TUNNEL_BUFSIZE)
                if not data:
                    break
                chan.sendall(data)
            if chan in readable:
                data = chan.recv(TUNNEL_BUFSIZE)
                if not data:
                    break
                sock.sendall(data)
    except (OSError, EOFError):
        pass
    finally:
        for closable in (chan, sock):
            try:
                closable.close()
            except Exception:
                pass


def tunnel_bind_listeners() -> list[socket.socket]:
    """占住三个本地端口。

    先绑定再连 SSH：端口被占（说明已有隧道在跑）时能立刻失败，
    而不是傻等一条永远用不上的 SSH 连接。
    """
    listeners = []
    for local_port, _, _ in FORWARDS:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", local_port))
            listener.listen(32)
        except OSError as exc:
            tunnel_log(f"端口 {local_port} 绑定失败：{exc}（可能已有隧道在运行）")
            for opened in listeners:
                opened.close()
            return []
        listeners.append(listener)
    return listeners


def tunnel_serve(listener: socket.socket, local_port: int, remote_host: str,
                 remote_port: int, transport) -> None:
    """从端口接收连接，每条都开一个 SSH 通道转发出去。"""
    while True:
        try:
            client, _addr = listener.accept()
        except OSError:
            return
        try:
            chan = transport.open_channel(
                "direct-tcpip", (remote_host, remote_port), client.getsockname()
            )
        except Exception as exc:
            tunnel_log(f"建立通道失败（{local_port}）：{exc}")
            client.close()
            continue
        threading.Thread(target=tunnel_pump, args=(chan, client), daemon=True).start()


def command_tunnel() -> int:
    """常驻的端口转发进程。SSH 断了会自己重连，不需要人工干预。"""
    config = load_autodl_config()
    listeners = tunnel_bind_listeners()
    if not listeners:
        return 2
    tunnel_log("本地端口已就绪：" + ", ".join(str(p) for p in LOCAL_PORTS))

    while True:
        try:
            transport = open_ssh_transport(config)
            transport.set_keepalive(TUNNEL_KEEPALIVE)
            tunnel_log(f"SSH 已连接：{config['AUTODL_HOST']}:{config['AUTODL_PORT']}")
        except LauncherError as exc:
            tunnel_log(f"{exc}，{TUNNEL_RECONNECT_DELAY} 秒后重试")
            time.sleep(TUNNEL_RECONNECT_DELAY)
            continue

        for listener, (local_port, remote_host, remote_port) in zip(listeners, FORWARDS):
            threading.Thread(
                target=tunnel_serve,
                args=(listener, local_port, remote_host, remote_port, transport),
                daemon=True,
            ).start()
        tunnel_log("三条隧道已建立")

        # SSH 断开就重新连——链路恢复后不需要人工重启隧道
        while transport.is_active():
            time.sleep(2)

        tunnel_log(f"SSH 断开，{TUNNEL_RECONNECT_DELAY} 秒后重连")
        try:
            transport.close()
        except Exception:
            pass
        time.sleep(TUNNEL_RECONNECT_DELAY)


# --------------------------------------------------------------------------
# 命令实现
# --------------------------------------------------------------------------


def open_browser(url: str) -> bool:
    """打开系统默认浏览器，返回是否成功。

    这是启动成功后的最后一个动作，纯属锦上添花：没有默认浏览器、被系统策略
    拦下、弹出「选择应用」对话框——这些都不代表项目启动失败。所以任何异常
    都在这里被吞掉，只留一行提示，绝不影响退出码。

    不等待浏览器返回，也不持有它的进程句柄。
    """
    try:
        import webbrowser

        return bool(webbrowser.open(url))
    except Exception as exc:
        print(f"       （自动打开浏览器失败：{type(exc).__name__}）", flush=True)
        return False


def read_configured_model() -> str:
    """取要用的模型名：优先 .env，其次默认值。"""
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
    env_file = PROJECT_DIR / ".env"
    try:
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("OLLAMA_MODEL="):
                    model = line.split("=", 1)[1].strip()
    except OSError:
        pass
    return model


def command_start() -> int:
    title("AI数字人教育教练 - 一键启动")
    print()

    state = load_runtime()
    model = read_configured_model()

    try:
        step_ollama(state, model)
        config, transport = step_autodl_connect(state)
        try:
            step_remote_services(state, transport)
        finally:
            transport.close()
        step_tunnel(state, config)
        step_fastapi(state)
    except LauncherError as exc:
        print()
        print()
        fail(str(exc))
        print()
        print("请不要继续后续步骤。修复上面的问题后重新运行 start.bat。", flush=True)
        save_runtime(state)
        return 1

    save_runtime(state)

    print()
    print("=" * 40)
    print(" AI数字人教育教练启动成功")
    print("=" * 40)
    print()
    print("FastAPI:")
    print(f"http://localhost:{FASTAPI_PORT}/")
    print()
    print("数字人页面：")
    print("http://localhost:8889/avatar/")
    print()

    print("正在打开浏览器…", flush=True)
    if not open_browser(f"http://localhost:{FASTAPI_PORT}/"):
        print(f"       浏览器未能自动打开，请手动访问 http://localhost:{FASTAPI_PORT}/", flush=True)

    print()
    print("=" * 40)
    return 0


def command_stop() -> int:
    title("AI数字人教育教练 - 停止")
    print()

    state = load_runtime()
    stopped_any = False

    # 只结束本启动器记录过的本地进程，绝不按名字批量杀——
    # 否则会误伤用户自己的 Python / SSH 进程。
    for key, label in (("fastapi_pid", "FastAPI"), ("tunnel_pid", "SSH 隧道")):
        pid = state.get(key, 0)
        if not pid:
            continue
        if kill_pid(pid):
            print(f"  [已停止] {label}（PID {pid}）", flush=True)
            stopped_any = True
        else:
            print(f"  [已退出] {label}（PID {pid} 未在运行）", flush=True)
        state[key] = 0

    # AutoDL 上的服务只按记录的 PID 停，不做名字匹配
    remote_started = state.get("remote_started", [])
    if remote_started:
        try:
            config = load_autodl_config()
            transport = open_ssh_transport(config)
            try:
                for item in remote_started:
                    run_remote(transport, f"kill {item['pid']} 2>/dev/null; echo done")
                    print(f"  [已停止] AutoDL {item['name']}（PID {item['pid']}）", flush=True)
                    stopped_any = True
            finally:
                transport.close()
        except LauncherError as exc:
            print(f"  [跳过] 无法连接 AutoDL：{exc}", flush=True)
        state["remote_started"] = []
    else:
        print("  [跳过] AutoDL 服务：本启动器没有记录到自己启动过它们", flush=True)
        print("         （LiveTalking / MediaMTX 未由 start.bat 启动，或记录已丢失）", flush=True)

    save_runtime(state)

    print()
    print("  已完成。" if stopped_any else "  没有需要停止的进程。", flush=True)
    print()
    print("  说明：", flush=True)
    print("    · Ollama 是共享服务，本脚本不会停止它", flush=True)
    print("    · AutoDL 实例不会被关机", flush=True)
    return 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "start"
    handlers = {"start": command_start, "stop": command_stop, "tunnel": command_tunnel}
    if command not in handlers:
        print(f"未知命令：{command}（可用：start / stop）")
        return 2
    return handlers[command]()


if __name__ == "__main__":
    raise SystemExit(main())
