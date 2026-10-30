"""Local socket tests; no Kubernetes requests or application credentials."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import errno
import logging
import os
import socket
import socketserver
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest

from scripts.onprem.forward_native_service import NativeServiceForward, _Relay, _report_error
from scripts.onprem.start_native_backend import native_service_targets


@contextmanager
def serving(server):
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        parts = []
        while data := self.request.recv(65536):
            parts.append(data)
        self.request.sendall(b"".join(parts))


def test_loopback_relay_preserves_concurrent_payloads_and_half_close():
    with serving(socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)) as target:
        with serving(NativeServiceForward(0, target)) as relay:
            assert relay[0] == "127.0.0.1"
            def exchange(index):
                payload = bytes([index]) * (512 * 1024)
                with socket.create_connection(relay, timeout=5) as client:
                    client.sendall(payload)
                    client.shutdown(socket.SHUT_WR)
                    received = bytearray()
                    while data := client.recv(65536):
                        received.extend(data)
                    assert received == payload
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(exchange, range(8)))


def test_bidirectional_backpressure_does_not_block_the_other_direction():
    payload = b"x" * (4 * 1024 * 1024)
    class Duplex(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(payload)
            digest = hashlib.sha256()
            while data := self.request.recv(65536):
                digest.update(data)
            self.request.sendall(digest.digest())
    with serving(socketserver.ThreadingTCPServer(("127.0.0.1", 0), Duplex)) as target:
        with serving(NativeServiceForward(0, target)) as relay:
            with socket.create_connection(relay, timeout=5) as client:
                def upload():
                    client.sendall(payload)
                    client.shutdown(socket.SHUT_WR)
                writer = Thread(target=upload, daemon=True)
                writer.start()
                received = bytearray()
                while data := client.recv(65536):
                    received.extend(data)
                writer.join(timeout=2)
                assert not writer.is_alive()
                assert received == payload + hashlib.sha256(payload).digest()


def test_upstream_half_close_keeps_client_upload_open():
    received = []; finished = Event()
    class HalfClosed(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(b'response')
            self.request.shutdown(socket.SHUT_WR)
            parts = []
            while data := self.request.recv(65536): parts.append(data)
            received.append(b''.join(parts)); finished.set()
    with serving(socketserver.ThreadingTCPServer(('127.0.0.1', 0), HalfClosed)) as target:
        with serving(NativeServiceForward(0, target)) as relay:
            with socket.create_connection(relay, timeout=3) as client:
                response = bytearray()
                while data := client.recv(65536): response.extend(data)
                assert response == b'response'
                client.sendall(b'upload-after-response-eof')
                client.shutdown(socket.SHUT_WR)
                assert finished.wait(2)
    assert received == [b'upload-after-response-eof']


@pytest.mark.skipif(os.name != 'posix', reason='Linux accept-queue behavior, independent of Windows backlog policy')
def test_paused_accept_loop_absorbs_32_connections_only_with_explicit_backlog():
    class OriginalBacklog(NativeServiceForward):
        request_queue_size = 5
    outcomes = {}
    for name, server_type in [('original5', OriginalBacklog), ('current128', NativeServiceForward)]:
        # Deliberately do NOT start serve_forever: isolate listener queue
        # capacity from handler scheduling, upstream latency and payloads.
        with server_type(0, ('127.0.0.1', 9)) as server:
            gate = Barrier(33)
            def connect(_):
                client = socket.socket(); client.settimeout(.5)
                gate.wait(timeout=3)
                try:
                    client.connect(server.server_address)
                    return client
                except OSError:
                    client.close()
                    return None
            with ThreadPoolExecutor(max_workers=32) as pool:
                futures = [pool.submit(connect, i) for i in range(32)]
                gate.wait(timeout=3)
                clients = [client for future in futures if (client := future.result()) is not None]
            accepted = []
            try:
                # A client-side connect can finish while its final ACK is
                # dropped by a full accept queue. Count the immediately queued
                # sockets too, without waiting for retransmission to refill it.
                # No handler/target connections are made in this experiment.
                server.socket.setblocking(False)
                while True:
                    try: accepted.append(server.socket.accept()[0])
                    except BlockingIOError: break
                outcomes[name] = {'connected': len(clients), 'accepted': len(accepted)}
            finally:
                for client in clients + accepted: client.close()
    assert 0 < outcomes['original5']['accepted'] < 32
    assert outcomes['current128'] == {'connected': 32, 'accepted': 32}
    assert NativeServiceForward.request_queue_size == 128
    print('paused_accept_32burst='+str(outcomes))


def test_relay_serves_32_simultaneous_clients_without_crossing_payloads():
    class BurstTarget(socketserver.ThreadingTCPServer):
        request_queue_size = 128
        daemon_threads = True
    with serving(BurstTarget(('127.0.0.1', 0), Echo)) as target:
        with serving(NativeServiceForward(0, target)) as relay:
            gate = Barrier(32)
            def exchange(index):
                payload = bytes([index])*8192
                gate.wait(timeout=3)
                with socket.create_connection(relay, timeout=3) as client:
                    client.sendall(payload); client.shutdown(socket.SHUT_WR)
                    received = bytearray()
                    while data := client.recv(65536): received.extend(data)
                    return bytes(received) == payload
            with ThreadPoolExecutor(max_workers=32) as pool:
                assert all(pool.map(exchange, range(32)))


class FaultSocket:
    def __init__(self, *, data=b'', recv_error=None, send_error=None, shutdown_error=None):
        self.data = data
        self.recv_error, self.send_error, self.shutdown_error = recv_error, send_error, shutdown_error
        self.shutdowns = []

    def recv(self, maximum):
        if self.recv_error is not None: raise self.recv_error
        data, self.data = self.data, b''
        return data

    def sendall(self, data):
        if self.send_error is not None: raise self.send_error

    def shutdown(self, mode):
        self.shutdowns.append(mode)
        if self.shutdown_error is not None: raise self.shutdown_error


def relay_handler():
    handler = _Relay.__new__(_Relay)
    handler.setup()
    return handler


@pytest.mark.parametrize('direction', ['upload', 'download'])
@pytest.mark.parametrize('operation', ['recv', 'send', 'halfclose'])
def test_copy_error_records_only_sanitized_first_phase(caplog, direction, operation):
    marker = 'SECRET-TEST /private?token=SECRET Authorization: Bearer SECRET 203.0.113.7'
    error = ConnectionResetError(errno.ECONNRESET, marker)
    source = FaultSocket(data=marker.encode() if operation == 'send' else b'',
                         recv_error=error if operation == 'recv' else None)
    destination = FaultSocket(send_error=error if operation == 'send' else None,
                              shutdown_error=error if operation == 'halfclose' else None)
    handler = relay_handler()
    with caplog.at_level(logging.WARNING):
        handler.copy(source, destination, direction)
        handler.copy(FaultSocket(recv_error=BrokenPipeError(errno.EPIPE, marker)), FaultSocket(), direction)
    records = [r for r in caplog.records if r.name == 'scripts.onprem.forward_native_service']
    assert len(records) == 1
    message = records[0].getMessage()
    assert 'phase='+direction+'_'+operation in message
    assert 'errorType=ConnectionResetError' in message and 'errno='+str(errno.ECONNRESET) in message
    assert float(message.split('elapsedSeconds=')[1]) >= 0
    assert records[0].exc_info is None and records[0].stack_info is None
    assert all(text not in caplog.text for text in ['SECRET', '/private', 'Authorization', '203.0.113.7'])
    assert socket.SHUT_RDWR in source.shutdowns and socket.SHUT_RDWR in destination.shutdowns


def test_connect_timeout_remains_five_seconds_and_has_no_retry(monkeypatch, caplog):
    calls = []
    def connect(target, timeout):
        calls.append((target, timeout))
        raise TimeoutError('SECRET-TEST 203.0.113.7 /private?token=SECRET')
    monkeypatch.setattr(socket, 'create_connection', connect)
    handler = relay_handler()
    handler.server = SimpleNamespace(target=('10.9.8.7', 8080))
    handler.request = FaultSocket()
    with caplog.at_level(logging.WARNING): handler.handle()
    assert calls == [(('10.9.8.7', 8080), 5)]
    assert 'phase=connect errorType=TimeoutError errno=None' in caplog.text
    assert all(text not in caplog.text for text in ['SECRET', '203.0.113.7', '10.9.8.7', '/private'])


def test_setup_failure_keeps_first_error_when_close_also_fails(monkeypatch, caplog):
    class Upstream:
        def settimeout(self, value): raise OSError(errno.EINVAL, 'SECRET-CONFIGURE')
        def close(self): raise OSError(errno.EIO, 'SECRET-CLOSE')
    monkeypatch.setattr(socket, 'create_connection', lambda target, timeout: Upstream())
    handler = relay_handler()
    handler.server = SimpleNamespace(target=('10.9.8.7', 8080))
    with caplog.at_level(logging.WARNING): handler.handle()
    assert len(caplog.records) == 1 and 'phase=configure' in caplog.text
    assert 'SECRET' not in caplog.text


def test_unknown_exception_names_and_errno_text_never_become_log_fields(caplog):
    def no_string(self): raise AssertionError('Exception text must never be read')
    private_type = type('SECRET_CLASS_NAME', (OSError,), {'__str__': no_string})
    error = private_type(); error.errno = 'SECRET_ERRNO'
    with caplog.at_level(logging.WARNING): _report_error('SECRET_PHASE', error, .125)
    assert 'phase=handler errorType=OSError errno=None elapsedSeconds=0.125000' in caplog.text
    assert 'SECRET' not in caplog.text


def test_unexpected_upload_error_is_sanitized_without_thread_traceback(caplog):
    handler = relay_handler()
    with caplog.at_level(logging.WARNING):
        handler.copy(FaultSocket(recv_error=RuntimeError('SECRET application error')), FaultSocket(), 'upload')
    assert 'phase=upload_recv errorType=UnexpectedError' in caplog.text
    assert 'SECRET' not in caplog.text


def test_server_fallback_never_prints_peer_address_or_raw_exception(caplog, capsys):
    class Broken(socketserver.BaseRequestHandler):
        def handle(self): raise RuntimeError('SECRET /private?token=SECRET')
    server = NativeServiceForward(0, ('127.0.0.1', 9))
    server.RequestHandlerClass = Broken
    with caplog.at_level(logging.WARNING):
        with serving(server) as relay:
            with socket.create_connection(relay, timeout=2) as client:
                assert client.recv(1) == b''
    assert 'phase=handler errorType=UnexpectedError errno=None elapsedSeconds=None' in caplog.text
    captured = capsys.readouterr()
    assert captured.err == ''
    assert all(text not in caplog.text+captured.out for text in ['SECRET', '127.0.0.1', 'Traceback', '/private'])


def test_logging_failure_does_not_interrupt_copy_shutdown_or_upstream_close(monkeypatch, capsys):
    calls = []
    def broken_warning(*args):
        calls.append(args)
        raise RuntimeError('SECRET logger /private?token=SECRET')
    monkeypatch.setattr('scripts.onprem.forward_native_service.logger.warning', broken_warning)
    class Upstream(FaultSocket):
        closed = False
        def settimeout(self, value): assert value is None
        def close(self): self.closed = True
    upstream = Upstream(recv_error=ConnectionResetError(errno.ECONNRESET, 'SECRET socket'))
    request = FaultSocket(recv_error=BrokenPipeError(errno.EPIPE, 'SECRET upload'))
    monkeypatch.setattr(socket, 'create_connection', lambda target, timeout: upstream)
    handler = relay_handler()
    handler.request = request
    handler.server = SimpleNamespace(target=('127.0.0.1', 9))
    handler.handle()
    assert upstream.closed
    assert socket.SHUT_RDWR in request.shutdowns and socket.SHUT_RDWR in upstream.shutdowns
    assert len(calls) == 1
    assert capsys.readouterr() == ('', '')


def test_logging_failure_in_server_fallback_does_not_emit_traceback_and_closes_client(monkeypatch, capsys):
    calls = []
    def broken_warning(*args):
        calls.append(args)
        raise RuntimeError('SECRET logger /private?token=SECRET')
    monkeypatch.setattr('scripts.onprem.forward_native_service.logger.warning', broken_warning)
    class Broken(socketserver.BaseRequestHandler):
        def handle(self): raise RuntimeError('SECRET handler /private?token=SECRET')
    server = NativeServiceForward(0, ('127.0.0.1', 9))
    server.RequestHandlerClass = Broken
    with serving(server) as relay:
        with socket.create_connection(relay, timeout=2) as client:
            assert client.recv(1) == b''
    assert len(calls) == 1
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize("fault", [None, "namespace", "duplicate", "public", "loopback", "headless", "udp", "port", "external"])
def test_native_targets_use_only_actual_scoped_tcp_clusterips(fault):
    service = {"metadata": {"namespace": "lab", "name": "amf"},
               "spec": {"clusterIP": "10.96.0.12", "type": "ClusterIP",
                        "ports": [{"port": 8080, "protocol": "TCP"}]}}
    services = {"items": [service]}
    if fault == "namespace": service["metadata"]["namespace"] = "foreign"
    if fault == "duplicate": services["items"].append(deepcopy(service))
    if fault == "public": service["spec"]["clusterIP"] = "8.8.8.8"
    if fault == "loopback": service["spec"]["clusterIP"] = "127.0.0.1"
    if fault == "headless": service["spec"]["clusterIP"] = "None"
    if fault == "udp": service["spec"]["ports"][0]["protocol"] = "UDP"
    if fault == "port": service["spec"]["ports"][0]["port"] = 8000
    if fault == "external": service["spec"]["type"] = "ExternalName"
    if fault:
        with pytest.raises(ValueError):
            native_service_targets(services, {"amf": ("amf", 8080, 18080)}, "lab")
    else:
        assert native_service_targets(services, {"amf": ("amf", 8080, 18080)}, "lab") == {
            "amf": ("10.96.0.12", 8080)}
