"""Opt-in local CRI for immutable result collection and terminal cancellation."""
import re
import subprocess

from backend.onprem.policy.errors import PolicyBlocked
from backend.onprem.traffic.worker import command as worker_command
from backend.onprem.traffic.transports.cri_read import (
    CRI_BINARY, ENVIRONMENT_KEY as READ_ENVIRONMENT_KEY, configured_read_transport,
)

ENVIRONMENT_KEY = "CITYVERSE_ONPREM_DETACHED_POSTFLIGHT_TRANSPORT"


class LocalCriPostflightTransport:
    """Caller retains durable reservation, owner, epoch and receipt verification."""

    def __init__(self, runner):
        self.runner = runner

    def execute(self, action, container_id, argv, *, reservation, timeout, maximum):
        from backend.onprem.traffic.batch import (
            DETACHED_COLLECT_WIRE_MAX, DETACHED_RECORD_WIRE_MAX,
        )
        if action not in {"collect", "cancel"}:
            raise PolicyBlocked("Detached postflight CRI action is invalid")
        if (not isinstance(container_id, str)
                or re.fullmatch(r"containerd://[0-9a-f]{64}", container_id) is None):
            raise PolicyBlocked("Detached postflight CRI container is invalid")
        expected = worker_command(action, reservation)
        selected = next(row for row in reservation["roles"]
                        if row["role"] == reservation["selectedRole"]
                        and row["podUid"] == reservation["selectedPodUid"])
        if (selected["containerID"] != container_id or not isinstance(argv, list)
                or argv != expected):
            raise PolicyBlocked("Detached postflight CRI immutable identity changed")
        count = sum(job["receiverUid" if reservation["selectedRole"] == "server" else "ueUid"]
                    == reservation["selectedPodUid"] for job in reservation["jobs"])
        bound = min(DETACHED_COLLECT_WIRE_MAX, count * DETACHED_RECORD_WIRE_MAX + 512) if action == "collect" else 1024
        if (type(timeout) is not int or timeout != (10 if action == "collect" else 5)
                or type(maximum) is not int or maximum != bound):
            raise PolicyBlocked("Detached postflight CRI bounds are invalid")
        command = [CRI_BINARY.as_posix(), "--runtime-endpoint",
                   "unix:///run/containerd/containerd.sock", "exec", "--sync", "--timeout",
                   str(timeout), container_id[len("containerd://"):], *expected]
        try:
            result = self.runner(command, capture_output=True, text=False, timeout=timeout,
                                 check=False, shell=False)
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired("detached-cri-postflight", timeout) from None
        except OSError:
            raise PolicyBlocked("Detached postflight CRI dispatch failed") from None
        if type(getattr(result, "returncode", None)) is not int or result.returncode != 0:
            raise PolicyBlocked("Detached postflight CRI failed; no retry or fallback")
        raw = result.stdout
        if (not isinstance(raw, bytes) or not isinstance(result.stderr, bytes)
                or len(result.stderr) > 1024 or len(raw) > maximum + 2
                or not raw.endswith(b"\n\n")):
            raise PolicyBlocked("Detached postflight CRI response framing is invalid")
        wire = raw[:-2]
        prefix = b"SEALEDv1 " if action == "collect" else b"ABSENTv2 "
        if (not wire.startswith(prefix) or not wire.endswith(b"\n")
                or any(c not in {9, 10} and (c < 32 or c > 126) for c in wire)):
            raise PolicyBlocked("Detached postflight CRI response is invalid")
        # Exact token, seal digest, record count and closure are parsed by caller.
        return wire.decode("ascii")


def configured_postflight_transport(environ, driver, *, detached_enabled):
    mode = environ.get(ENVIRONMENT_KEY, "kubectl")
    if mode == "kubectl":
        return None
    if mode != "local-cri":
        raise PolicyBlocked("Detached postflight transport configuration is invalid")
    checked = configured_read_transport(
        {**environ, READ_ENVIRONMENT_KEY: "local-cri"}, driver,
        detached_enabled=detached_enabled)
    return LocalCriPostflightTransport(checked.runner)
