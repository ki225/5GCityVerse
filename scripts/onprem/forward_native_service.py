"""Loopback TCP relay for a WSL host that can route Kubernetes ClusterIPs."""
from __future__ import annotations

import argparse
import ipaddress
import logging
import socket
import socketserver
import sys
import time
from threading import Lock, Thread


logger = logging.getLogger(__name__)
_ERROR_PHASES = frozenset({"connect", "configure", "close", "handler"} | {
    direction + "_" + operation
    for direction in ("upload", "download") for operation in ("recv", "send", "halfclose")})
_ERROR_TYPES = {kind: kind.__name__ for kind in (
    OSError, TimeoutError, ConnectionRefusedError, ConnectionResetError,
    ConnectionAbortedError, BrokenPipeError)}


def _report_error(phase, error, elapsed):
    """Log only fixed categories and numeric diagnostics, never exception text."""
    category = _ERROR_TYPES.get(type(error), "OSError" if isinstance(error, OSError) else "UnexpectedError")
    number = getattr(error, "errno", None) if isinstance(error, OSError) else None
    try:
        logger.warning("Native relay failed phase=%s errorType=%s errno=%s elapsedSeconds=%s",
                       phase if phase in _ERROR_PHASES else "handler", category,
                       number if type(number) is int else None,
                       None if elapsed is None else f"{max(0., elapsed):.6f}")
    except Exception:
        # Diagnostics must not interrupt socket cleanup or trigger a raw
        # threading/socketserver traceback when a logging handler fails.
        pass


class NativeServiceForward(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    # Absorb a short native-OAM connection burst. This bounds the listener
    # queue, not active handlers or the host's total connection capacity.
    request_queue_size = 128

    def __init__(self, port, target):
        self.target = target
        super().__init__(("127.0.0.1", port), _Relay)

    def handle_error(self, request, client_address):
        # BaseServer's default prints the peer address and raw traceback.
        _report_error("handler", sys.exc_info()[1], None)


class _Relay(socketserver.BaseRequestHandler):
    def setup(self):
        self._started = time.monotonic()
        self._error_lock = Lock()
        self._error_recorded = False

    def _error(self, phase, error):
        # Shutting down both directions can produce secondary socket errors.
        # Keep the first observed failure instead of replacing its diagnosis.
        with self._error_lock:
            if self._error_recorded:
                return
            self._error_recorded = True
        _report_error(phase, error, time.monotonic() - self._started)

    def copy(self, source, destination, direction):
        phase = direction + "_recv"
        try:
            while True:
                phase = direction + "_recv"
                data = source.recv(65536)
                if not data:
                    phase = direction + "_halfclose"
                    destination.shutdown(socket.SHUT_WR)
                    return
                phase = direction + "_send"
                destination.sendall(data)
        except Exception as error:
            # Unexpected upload-thread errors must not use threading's raw
            # traceback hook either; classify them without application text.
            self._error(phase, error)
            for stream in (source, destination):
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def connect_upstream(self):
        """Retry only before any request bytes can leave this relay.

        A failed ClusterIP connect can close the accepted client and surface as
        RemoteDisconnected even though no HTTP request reached the Service.
        The upload thread is created only after this method succeeds.
        """
        import errno
        retryable = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.ECONNREFUSED}
        started = time.monotonic()
        deadline = started + 5
        failure_errno = None
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Native upstream connection deadline expired')
            try:
                upstream = socket.create_connection(self.server.target, timeout=remaining)
                if time.monotonic() >= deadline:
                    upstream.close()
                    raise TimeoutError('Native upstream connection completed too late')
            except OSError as error:
                failure_errno = error.errno
                delay = .05 * (attempt + 1)
                if attempt == 2 or error.errno not in retryable or time.monotonic() + delay >= deadline:
                    raise
                time.sleep(delay)
                continue
            if attempt:
                try:
                    logger.warning('Native relay recovered before forwarding attempts=%s errno=%s elapsedSeconds=%.6f',
                                   attempt + 1, failure_errno, time.monotonic() - started)
                except Exception:
                    pass  # Diagnostics cannot discard an established socket.
            return upstream

    def handle(self):
        upstream = None
        phase = "connect"
        try:
            upstream = self.connect_upstream()
            phase = "configure"
            upstream.settimeout(None)
            # Each direction can make progress under TCP backpressure.
            # Half-close preserves responses after the request has ended.
            upload = Thread(target=self.copy, args=(self.request, upstream, "upload"), daemon=True)
            upload.start()
            self.copy(upstream, self.request, "download")
            upload.join()
        except OSError as error:
            self._error(phase, error)
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError as error:
                    self._error("close", error)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--target-ip", required=True)
    parser.add_argument("--target-port", type=int, required=True)
    args = parser.parse_args(argv)
    address = ipaddress.IPv4Address(args.target_ip)
    if (not address.is_private or address.is_unspecified or address.is_multicast
            or not 1 <= args.listen_port <= 65535 or not 1 <= args.target_port <= 65535
            or (address.is_loopback and args.listen_port == args.target_port)):
        parser.error("A private IPv4 target and valid distinct TCP endpoints are required")
    with NativeServiceForward(args.listen_port, (str(address), args.target_port)) as server:
        print(f"Native service relay listening on 127.0.0.1:{args.listen_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
