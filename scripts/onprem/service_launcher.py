"""Linux process management shared by the Bash entry points (stdlib only)."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]


def dotenv(path: Path) -> dict[str, str]:
    """Read literal, single-line dotenv values. Never evaluate shell code."""
    values = {}
    if not path.exists():
        return values
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid dotenv assignment at line {number}")
        if value.startswith(("'", '"')):
            parts = shlex.split(value, comments=True)
            if len(parts) != 1:
                raise ValueError(f"Invalid dotenv value at line {number}")
            value = parts[0]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values


def identity(pid: int) -> dict | None:
    try:
        # comm may contain spaces/parentheses; fields after the closing ')' are fixed.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "start_ticks": fields[19], "pgrp": int(fields[2]),
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    except (FileNotFoundError, ProcessLookupError):
        return None


def owned(record: dict | None) -> bool:
    return bool(record and identity(record["pid"]) == record and record["pgrp"] == record["pid"])


def group_alive(pgid: int) -> bool:
    for proc in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = proc.read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] != "Z":
                return True
        except (FileNotFoundError, ProcessLookupError):
            pass
    return False


def stop_record(record: dict | None) -> None:
    if not owned(record):
        return  # PID reuse, reboot or an external service never grants ownership.
    pgid = record["pid"]
    try:
        os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while group_alive(pgid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if group_alive(pgid):
            os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def write_state(path: Path, state: dict) -> None:
    pending = path.with_name(path.name + ".pending")
    pending.write_text(json.dumps(state, indent=2) + "\n")
    pending.chmod(0o600)
    pending.replace(path)


def free_port(host: str, port: int) -> None:
    with socket.socket() as sock:
        # A recently stopped HTTP listener leaves accepted connections in TIME_WAIT.
        # Match the server's address reuse; this still rejects an active listener.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))


def launch(command: str, cwd: Path, env: dict, logs: Path, name: str) -> tuple:
    with (logs / f"{name}.out.log").open("ab") as out, (logs / f"{name}.err.log").open("ab") as err:
        process = subprocess.Popen(["bash", "-c", command], cwd=cwd, env=env,
                                   stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                   start_new_session=True)
    record = identity(process.pid)
    if record is None:
        raise RuntimeError(f"{name} exited during startup; see logs")
    return process, record


def wait_ready(url: str, timeout: float, processes: list) -> None:
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            raise RuntimeError("A managed service exited; see logs")
        try:
            with opener.open(url, timeout=min(2, max(0.1, deadline - time.monotonic()))) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError, OSError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Readiness timed out: {url}")


def start(args: argparse.Namespace, state_path: Path) -> None:
    if state_path.exists():
        raise RuntimeError("Runtime state exists; run stop-onprem.sh with the same --state-file first")
    env = {**dotenv(resolve(args.env_file)), **os.environ}
    if not args.use_existing_backend and not env.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError("Missing OPENAI_API_KEY (environment or --env-file)")
    for tool in ("node", "npm"):
        executable = shutil.which(tool, path=env.get("PATH"))
        if not executable or Path(executable).resolve().as_posix().startswith("/mnt/"):
            raise RuntimeError(f"Install Linux {tool} inside Ubuntu/WSL; Windows executables are unsupported")
    node_check = subprocess.run(["node", "-e", "const [a,b]=process.versions.node.split('.').map(Number);process.exit(a<22||(a===22&&b<12)?1:0)"], env=env)
    if node_check.returncode:
        raise RuntimeError("Linux Node.js >=22.12 is required")
    free_port(args.frontend_host, args.frontend_port)
    if not args.use_existing_backend:
        free_port(args.backend_host, args.backend_port)
    if not (ROOT / "frontend/node_modules").is_dir():
        raise RuntimeError("Missing frontend dependencies; run npm ci in frontend using Linux Node.js")
    if args.preflight_only:
        print("Preflight passed (configuration/tools/ports only; infra and HTTP readiness not checked)")
        return

    backend_url = f"http://{args.backend_host}:{args.backend_port}"
    frontend_url = f"http://{args.frontend_host}:{args.frontend_port}"
    logs = state_path.parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    state = {"version": 1, "status": "starting", "backend": None, "frontend": None,
             "backend_url": backend_url, "frontend_url": frontend_url, "logs": str(logs)}
    processes = []
    try:
        write_state(state_path, state)
        if not args.use_existing_backend:
            backend_env = {**env, "CITYVERSE_HOST": args.backend_host, "CITYVERSE_PORT": str(args.backend_port),
                           "CITYVERSE_CORS_ORIGINS": f"http://localhost:{args.frontend_port},{frontend_url}",
                           "OPENAI_MODEL": env.get("OPENAI_MODEL") or "gpt-5.5"}
            command = args.backend_command or env.get("ONPREM_BACKEND_COMMAND")
            if not command:
                backend_env["CITYVERSE_LOCAL_KUBERNETES_ENABLED"] = "false"
                command = f"{shlex.quote(sys.executable)} -m backend.onprem.api.server"
            process, state["backend"] = launch(command, ROOT, backend_env, logs, "backend")
            processes.append(process)
            write_state(state_path, state)
        wait_ready(backend_url + "/health", args.readiness_timeout, processes)

        # REQ-002: only public build settings reach the frontend, never dotenv secrets.
        frontend_env = {key: env[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if key in env}
        frontend_env.update({"PORT": str(args.frontend_port), "FRONTEND_HOST": args.frontend_host,
                             "CITYVERSE_BACKEND_URL": backend_url, "VITE_API_URL": backend_url,
                             "VITE_WS_URL": backend_url.replace("http://", "ws://") + "/ws",
                             "VITE_RUNTIME_PROFILE": "onprem"})
        if env.get("VITE_FREE5GC_WEBUI_URL"):
            frontend_env["VITE_FREE5GC_WEBUI_URL"] = env["VITE_FREE5GC_WEBUI_URL"]
        command = args.frontend_command or env.get("ONPREM_FRONTEND_COMMAND") or "npm run build && exec npm run start"
        process, state["frontend"] = launch(command, ROOT / "frontend", frontend_env, logs, "frontend")
        processes.append(process)
        write_state(state_path, state)
        wait_ready(frontend_url + "/", args.readiness_timeout, processes)
        state["status"] = "ready"
        write_state(state_path, state)
        print(f"Backend: {backend_url}\nFrontend: {frontend_url}\nState: {state_path}\nLogs: {logs}")
    except BaseException:
        for name in ("frontend", "backend"):
            stop_record(state[name])
        for process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        state_path.unlink(missing_ok=True)
        print(f"Startup failed; logs: {logs}", file=sys.stderr)
        raise


def resolve(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def main() -> int:
    if sys.platform != "linux":
        raise RuntimeError("Run these launchers inside Ubuntu/WSL")
    os.umask(0o077)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description="Ubuntu/WSL frontend/backend launcher; does not deploy infra")
    parser.add_argument("operation", choices=("start", "stop"))
    parser.add_argument("--state-file", default=".tmp/onprem/runtime.json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--backend-host", default="127.0.0.1")
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--frontend-host", default="127.0.0.1")
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument("--backend-command")
    parser.add_argument("--frontend-command")
    parser.add_argument("--use-existing-backend", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--readiness-timeout", type=float, default=120)
    args = parser.parse_args()
    if not (1 <= args.backend_port <= 65535 and 1 <= args.frontend_port <= 65535 and args.readiness_timeout > 0):
        parser.error("Ports must be 1..65535; readiness timeout must be positive")
    state_path = resolve(args.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_name(state_path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.operation == "start":
            start(args, state_path)
        elif state_path.exists():
            state = json.loads(state_path.read_text())
            for name in ("frontend", "backend"):
                stop_record(state.get(name))
            state_path.unlink()
            print("Stopped launcher-owned services; infra and external backend unchanged")
        else:
            print("No runtime state; nothing to stop")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Launcher interrupted", file=sys.stderr)
        raise SystemExit(130)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        # Do not print commands or dotenv content, which can contain credentials.
        detail = f" (errno={exc.errno})" if isinstance(exc, OSError) else ""
        print(f"Launcher failed: {type(exc).__name__}{detail}" if not isinstance(exc, RuntimeError) else str(exc), file=sys.stderr)
        raise SystemExit(1)
