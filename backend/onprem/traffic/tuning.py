"""Bounded execution settings frozen into each detached reservation."""
import json
import socket

from backend.onprem.policy.errors import PolicyBlocked

TUNED_SCHEMA = "cityverse.detached-shard/v2"
# The pinned iperf3 3.21 GRO path can busy-poll. Local loopback A/B measured
# higher total CPU with --gsro; retain explicit opt-in until path-level evidence.
DEFAULT_TUNING = {"udpGsro": False, "socketBufferBytes": 1048576}


def validate_tuning(value):
    if (not isinstance(value, dict) or set(value) != {"udpGsro", "socketBufferBytes"}
            or type(value["udpGsro"]) is not bool
            or type(value["socketBufferBytes"]) is not int
            or value["socketBufferBytes"] != 0 and not 65536 <= value["socketBufferBytes"] <= 1048576):
        raise PolicyBlocked("Invalid detached traffic tuning")
    return dict(value)


def tuning_from_environment(environment):
    gsro = environment.get("CITYVERSE_ONPREM_TRAFFIC_UDP_GSRO", "false")
    size = environment.get("CITYVERSE_ONPREM_TRAFFIC_SOCKET_BUFFER_BYTES", "1048576")
    if gsro not in {"true", "false"} or not isinstance(size, str) or not size.isascii() or not size.isdecimal():
        raise PolicyBlocked("Invalid detached traffic tuning environment")
    return validate_tuning({"udpGsro": gsro == "true", "socketBufferBytes": int(size)})


def tuning_manifest(manifest, tuning):
    return manifest + "trafficTuning\t" + json.dumps(validate_tuning(tuning), sort_keys=True, separators=(",", ":")) + "\n"


def verify_host_socket_tuning(tuning):
    """No bind/send: prove this kernel accepts requested socket settings.

    This is host capability, not evidence of offload on the GTP/TUN path.
    Each role's actual iperf socket buffers are checked again in its JSON.
    """
    tuning = validate_tuning(tuning)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            size = tuning["socketBufferBytes"]
            if size:
                for option in (socket.SO_SNDBUF, socket.SO_RCVBUF):
                    probe.setsockopt(socket.SOL_SOCKET, option, size)
                    if probe.getsockopt(socket.SOL_SOCKET, option) < 2 * size:
                        raise PolicyBlocked("Detached host socket buffer request was clamped")
            if tuning["udpGsro"]:
                # Linux UDP_SEGMENT / UDP_GRO, often absent from Python constants.
                for option, requested in ((103, 1200), (104, 1)):
                    probe.setsockopt(socket.IPPROTO_UDP, option, requested)
                    if probe.getsockopt(socket.IPPROTO_UDP, option) != requested:
                        raise PolicyBlocked("Detached host UDP offload capability is unknown")
    except OSError as exc:
        raise PolicyBlocked("Detached host socket tuning is unsupported") from exc


def verify_report_tuning(report, tuning):
    """Require both roles' actual UDP socket buffer readback; no defaulting."""
    tuning = validate_tuning(tuning)
    size = tuning["socketBufferBytes"]
    start = report.get("start") if isinstance(report, dict) else None
    if size and (not isinstance(start, dict) or type(start.get("sock_bufsize")) is not int
                 or start["sock_bufsize"] != size
                 or any(type(start.get(key)) is not int or start[key] < 2 * size
                        for key in ("sndbuf_actual", "rcvbuf_actual"))):
        raise PolicyBlocked("Detached actual socket buffer tuning is unproven")
