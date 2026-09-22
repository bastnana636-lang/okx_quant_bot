#!/usr/bin/env python3
"""Cross-platform launcher used by the macOS and Windows installers."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

APP_NAME = "OKX Quant Trader"
RELEASE_VERSION = "1.1.0"
CONFIG = "conf_okx_multi.yml"
READY_TIMEOUT = 120
RUNTIME_DIRS = {"certs", "data", "gateway-files", "logs"}


class LauncherError(RuntimeError):
    """A user-facing launcher failure."""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def payload_root() -> Path:
    override = os.environ.get("OKX_TRADER_PAYLOAD")
    if override:
        return Path(override).expanduser().resolve()
    if not is_frozen():
        return Path(__file__).resolve().parents[1]
    executable = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        return executable.parents[1] / "payload"
    return executable.parent / "payload"


def workspace_root() -> Path:
    override = os.environ.get("OKX_TRADER_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if not is_frozen():
        return payload_root()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
    return Path.home() / ".local" / "share" / "okx-quant-trader"


def _is_protected(relative: Path) -> bool:
    parts = relative.parts
    if not parts:
        return False
    if parts[0] in RUNTIME_DIRS or relative.as_posix() == ".compose.env":
        return True
    if parts[:2] in (("conf", "connectors"), ("conf", "controllers")):
        return True
    return relative.as_posix() == "conf/.password_verification"


def sync_payload(source: Path, destination: Path, version: str = RELEASE_VERSION) -> Path:
    """Install/update packaged files while preserving credentials and runtime data."""
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        return destination
    if not (source / "docker-compose.yml").is_file():
        raise LauncherError(f"安装包内容不完整：找不到 {source / 'docker-compose.yml'}")
    marker = destination / ".native-version"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == version:
        return destination
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if "__pycache__" in relative.parts:
            continue
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if _is_protected(relative) and target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
    marker.write_text(version + "\n", encoding="utf-8")
    return destination


def configure_path() -> None:
    candidates: list[str] = []
    if sys.platform == "darwin":
        candidates.extend([
            "/Applications/Docker.app/Contents/Resources/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin",
        ])
    elif os.name == "nt":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidates.append(str(Path(program_files) / "Docker" / "Docker" / "resources" / "bin"))
    os.environ["PATH"] = os.pathsep.join(candidates + [os.environ.get("PATH", "")])


def docker_executable() -> str:
    configure_path()
    executable = shutil.which("docker")
    if not executable:
        raise LauncherError("未找到 Docker Desktop。请先安装 Docker Desktop，然后重新启动本程序。")
    return executable


def _docker_ready(docker: str) -> bool:
    return subprocess.run(
        [docker, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0


def ensure_docker(timeout: int = 90) -> str:
    docker = docker_executable()
    if _docker_ready(docker):
        return docker
    print("Docker Desktop 尚未运行，正在启动…", flush=True)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["/usr/bin/open", "-a", "Docker"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        elif os.name == "nt":
            desktop = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker" / "Docker" / "Docker Desktop.exe"
            subprocess.Popen([str(desktop)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _docker_ready(docker):
            print("Docker Desktop 已就绪。", flush=True)
            return docker
        time.sleep(2)
    raise LauncherError("Docker Desktop 启动超时。请确认 Docker 使用 Linux containers 后重试。")


def run(command: list[str], root: Path, *, input_text: str | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=root, input=input_text, text=input_text is not None,
                          check=check)


def prepare() -> Path:
    root = sync_payload(payload_root(), workspace_root())
    os.environ["OKX_TRADER_ROOT"] = str(root)
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def load_environment(root: Path) -> dict[str, str]:
    from scripts.ensure_setup import ensure
    return ensure()


def port_open(port: int = 8888) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _launcher_command(command: str) -> list[str]:
    if is_frozen():
        return [sys.executable, command]
    return [sys.executable, str(Path(__file__).resolve()), command]


def start_dashboard(root: Path) -> None:
    if port_open():
        print("Dashboard 已在运行：http://127.0.0.1:8888", flush=True)
        webbrowser.open("http://127.0.0.1:8888")
        return
    log_path = root / "dashboard" / "dashboard.log"
    pid_path = root / "dashboard" / "dashboard.pid"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a", encoding="utf-8")
    kwargs: dict = {"cwd": root, "stdout": log_handle, "stderr": subprocess.STDOUT,
                    "stdin": subprocess.DEVNULL, "env": os.environ.copy()}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(_launcher_command("dashboard"), **kwargs)
    log_handle.close()
    pid_path.write_text(str(process.pid), encoding="utf-8")
    for _ in range(30):
        if port_open():
            break
        if process.poll() is not None:
            raise LauncherError(f"Dashboard 启动失败，请查看 {log_path}")
        time.sleep(0.2)
    print("Dashboard 已启动：http://127.0.0.1:8888", flush=True)
    webbrowser.open("http://127.0.0.1:8888")


def stop_dashboard(root: Path) -> None:
    pid_path = root / "dashboard" / "dashboard.pid"
    if not pid_path.is_file():
        return
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass
    pid_path.unlink(missing_ok=True)


def start_bot() -> int:
    root = prepare()
    docker = ensure_docker()
    env = load_environment(root)
    password = env.get("HBOT_PASSWORD", "")
    if not password:
        raise LauncherError("本机配置缺少 HBOT_PASSWORD")
    run([docker, "compose", "--env-file", ".compose.env", "up", "-d", "hummingbot"], root)
    run([docker, "exec", "hummingbot", "python", "-m", "scripts.validate_mean_reversion",
         "--config", CONFIG], root)
    run([docker, "exec", "-e", f"HBOT_PASSWORD={password}", "hummingbot", "hbot", "start",
         CONFIG, "--v2-script", "--replace"], root)
    readiness = (root / "scripts" / "wait_for_bot_ready.py").read_text(encoding="utf-8")
    run([docker, "exec", "-i", "hummingbot", "python", "-", "--config", CONFIG,
         "--timeout", str(READY_TIMEOUT)], root, input_text=readiness)
    start_dashboard(root)
    print("OKX Quant Trader 已启动。", flush=True)
    return 0


def stop_bot() -> int:
    root = prepare()
    docker = ensure_docker()
    state = subprocess.run(
        [docker, "container", "ls", "-a", "--filter", "name=^/hummingbot$", "--format", "{{.State}}"],
        cwd=root, capture_output=True, text=True, check=False,
    ).stdout.strip()
    if state == "running":
        stopped = run([docker, "exec", "hummingbot", "hbot", "stop"], root, check=False)
        if stopped.returncode not in (0, 2, 3):
            raise LauncherError("机器人停止失败，请检查 Docker Desktop。")
    stop_dashboard(root)
    print("OKX Quant Trader 已停止。", flush=True)
    return 0


def status_bot() -> int:
    root = prepare()
    docker = ensure_docker()
    return run([docker, "exec", "hummingbot", "hbot", "status"], root, check=False).returncode


def logs_bot() -> int:
    root = prepare()
    docker = ensure_docker()
    return run([docker, "exec", "-it", "hummingbot", "hbot", "logs", "-f"],
               root, check=False).returncode


def replace_keys() -> int:
    root = prepare()
    ensure_docker()
    from scripts.ensure_setup import ensure
    ensure(replace_keys=True)
    print("OKX API 凭据已更新。", flush=True)
    return 0


def dashboard_server() -> int:
    prepare()
    from dashboard.dashboard import main as dashboard_main
    dashboard_main()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="okx-quant-trader")
    parser.add_argument("command", nargs="?", default="start",
                        choices=("start", "stop", "status", "logs", "replace-keys", "dashboard"))
    args = parser.parse_args(argv)
    commands = {
        "start": start_bot,
        "stop": stop_bot,
        "status": status_bot,
        "logs": logs_bot,
        "replace-keys": replace_keys,
        "dashboard": dashboard_server,
    }
    try:
        return commands[args.command]()
    except LauncherError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"命令执行失败（退出码 {exc.returncode}）：{' '.join(exc.cmd)}", file=sys.stderr)
        return exc.returncode or 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
