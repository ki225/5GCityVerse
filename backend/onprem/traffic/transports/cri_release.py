"""Opt-in WSL-local dispatch of the immutable common-start release only."""
import re
import subprocess

from backend.onprem.policy.errors import PolicyBlocked
from backend.onprem.traffic.worker import command as worker_command
from backend.onprem.traffic.transports.cri_read import (
    CRI_BINARY, ENVIRONMENT_KEY as READ_ENVIRONMENT_KEY, configured_read_transport,
)

ENVIRONMENT_KEY = "CITYVERSE_ONPREM_DETACHED_RELEASE_TRANSPORT"


class LocalCriReleaseTransport:
    """The caller retains journal, Pod/container and ownership fences."""

    def __init__(self, runner):
        self.runner = runner

    def execute(self, action, container_id, argv, *, reservation, epoch, timeout, maximum):
        if (action != "release" or type(epoch) is not int or epoch <= 0
                or type(timeout) is not int or timeout != 5
                or type(maximum) is not int or maximum != 1024
                or not isinstance(container_id, str)
                or re.fullmatch(r"containerd://[0-9a-f]{64}", container_id) is None):
            raise PolicyBlocked("Detached release CRI action or bounds are invalid")
        expected = worker_command("release", reservation, epoch=epoch)
        selected = next(row for row in reservation["roles"]
                        if row["role"] == reservation["selectedRole"]
                        and row["podUid"] == reservation["selectedPodUid"])
        if (reservation["selectedRole"] != "client" or selected["containerID"] != container_id
                or not isinstance(argv, list) or argv != expected):
            raise PolicyBlocked("Detached release CRI immutable identity changed")
        command = [CRI_BINARY.as_posix(), "--runtime-endpoint",
                   "unix:///run/containerd/containerd.sock", "exec", "--sync", "--timeout",
                   "5", container_id[len("containerd://"):], *expected]
        try:
            result = self.runner(command, capture_output=True, text=False, timeout=timeout,
                                 check=False, shell=False)
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired("detached-cri-release", timeout) from None
        except OSError:
            raise PolicyBlocked("Detached release CRI dispatch failed") from None
        if type(getattr(result, "returncode", None)) is not int or result.returncode != 0:
            raise PolicyBlocked("Detached release CRI failed; no retry or fallback")
        raw = result.stdout
        if (not isinstance(raw, bytes) or not isinstance(result.stderr, bytes)
                or len(result.stderr) > 1024 or len(raw) > maximum + 2
                or not raw.endswith(b"\n\n")):
            raise PolicyBlocked("Detached release CRI response framing is invalid")
        wire = raw[:-2]
        if (not wire.startswith(b"RELEASEDv1 ") or not wire.endswith(b"\n")
                or any(c < 32 or c > 126 for c in wire[:-1])):
            raise PolicyBlocked("Detached release CRI response is invalid")
        return wire.decode("ascii")


def configured_release_transport(environ, driver, *, detached_enabled):
    mode = environ.get(ENVIRONMENT_KEY, "kubectl")
    if mode == "kubectl":
        return None
    if mode != "local-cri":
        raise PolicyBlocked("Detached release transport configuration is invalid")
    checked = configured_read_transport(
        {**environ, READ_ENVIRONMENT_KEY: "local-cri"}, driver,
        detached_enabled=detached_enabled)
    return LocalCriReleaseTransport(checked.runner)
