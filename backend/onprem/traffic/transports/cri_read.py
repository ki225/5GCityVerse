"""Explicit WSL-local CRI transport for three immutable detached read commands."""
import logging
import os
from pathlib import Path
import re
import stat
import subprocess

from backend.onprem.policy.errors import PolicyBlocked
from backend.onprem.traffic.worker import command as worker_command


READ_ACTIONS = frozenset(("identity", "capability", "status"))
ENVIRONMENT_KEY = "CITYVERSE_ONPREM_DETACHED_READ_TRANSPORT"
CRI_BINARY = Path("/usr/bin/crictl")
CRI_SOCKET = Path("/run/containerd/containerd.sock")
WSL_RELEASE = Path("/proc/sys/kernel/osrelease")
STDERR_WIRE_MAX = 1024
logger = logging.getLogger(__name__)


class LocalCriReadTransport:
    """No caller-defined program, mutation, retry, or transport fallback."""

    def __init__(self, runner):
        self.runner = runner

    def execute(self, action, container_id, argv, *, reservation=None, timeout, maximum):
        if action not in READ_ACTIONS:
            raise PolicyBlocked("Detached CRI action is not a pure read")
        bound = 5 if action == "status" else 2
        output_bound = 128 if action == "identity" else 1024
        if (type(timeout) is not int or timeout != bound
                or type(maximum) is not int or maximum != output_bound
                or not isinstance(container_id, str)
                or re.fullmatch(r"containerd://[0-9a-f]{64}", container_id) is None):
            raise PolicyBlocked("Detached CRI identity or bounds are invalid")
        expected = worker_command(action, reservation)
        if not isinstance(argv, list) or argv != expected:
            raise PolicyBlocked("Detached CRI command differs from the immutable builder")
        if action != "identity":
            selected = next(row for row in reservation["roles"]
                            if row["role"] == reservation["selectedRole"]
                            and row["podUid"] == reservation["selectedPodUid"])
            if selected["containerID"] != container_id:
                raise PolicyBlocked("Detached CRI container differs from the reservation")
        command = [CRI_BINARY.as_posix(), "--runtime-endpoint",
                   "unix:///run/containerd/containerd.sock", "exec", "--sync", "--timeout",
                   str(timeout), container_id[len("containerd://"):], *expected]
        try:
            result = self.runner(command, capture_output=True, text=False, timeout=timeout,
                                 check=False, shell=False)
        except subprocess.TimeoutExpired:
            # Never retain the private program/reservation in this exception.
            raise subprocess.TimeoutExpired("detached-cri-read", timeout) from None
        except OSError:
            raise PolicyBlocked("Detached CRI local dispatch failed") from None
        if type(getattr(result, "returncode", None)) is not int or result.returncode != 0:
            raise PolicyBlocked("Detached CRI command failed; no fallback")
        raw = result.stdout
        if (not isinstance(raw, bytes) or not isinstance(result.stderr, bytes)
                or len(result.stderr) > STDERR_WIRE_MAX
                or len(raw) > maximum + 2 or not raw.endswith(b"\n\n")):
            raise PolicyBlocked("Detached CRI response framing is invalid")
        # This installed ExecSync CLI emits stdout + LF, then stderr + LF.
        # With an empty remote stderr, remove exactly these two framing bytes.
        wire = raw[:-2]
        prefix = {"identity": b"IDENTITYv1 ", "capability": b"CAPABLEv1 ",
                  "status": b"STATUSv1 "}[action]
        if (not wire.startswith(prefix) or not wire.endswith(b"\n")
                or not wire[:-1] or any(c < 32 or c > 126 for c in wire[:-1])):
            raise PolicyBlocked("Detached CRI response is not one ASCII protocol line")
        return wire.decode("ascii")


def configured_read_transport(environ, driver, *, detached_enabled):
    """Validate only the explicitly selected local profile, before serving."""
    mode = environ.get(ENVIRONMENT_KEY, "kubectl")
    if mode == "kubectl":
        return None
    if mode != "local-cri":
        raise PolicyBlocked("Detached read transport configuration is invalid")
    if (environ.get("CITYVERSE_ONPREM_PROFILE") != "wsl-kubeadm"
            or detached_enabled is not True or os.name != "posix"
            or not hasattr(os, "geteuid") or os.geteuid() != 0
            or not callable(getattr(driver, "runner", None))):
        raise PolicyBlocked("Detached local CRI requires the explicit root WSL detached profile")
    try:
        if ("microsoft" not in WSL_RELEASE.read_text(encoding="ascii").lower()
                or not CRI_BINARY.is_file() or not os.access(CRI_BINARY, os.X_OK)
                or not stat.S_ISSOCK(CRI_SOCKET.stat().st_mode)
                or not os.access(CRI_SOCKET, os.R_OK | os.W_OK)):
            raise PolicyBlocked("Detached local CRI requires the fixed WSL runtime configuration")
    except (OSError, UnicodeError):
        raise PolicyBlocked("Detached local CRI configuration is unavailable") from None
    logger.warning("Detached read transport configured transport=local-cri actions=identity,capability,status")
    return LocalCriReadTransport(driver.runner)
