"""Opt-in, fixed namespace Pod/Service/workload GETs using kubeconfig X509.

This is deliberately not a general Kubernetes client. Mutations and exec keep
their existing transport. Responses are never cached.
"""
from __future__ import annotations

import base64
from http.client import HTTPSConnection
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import ssl
import stat
from threading import Condition, TIMEOUT_MAX
import time
from urllib.parse import quote, urlsplit

import yaml

from backend.onprem.runtime.kubeadm import LABEL
from backend.onprem.runtime.ue import EvidenceUnavailable

MAX_BODY_BYTES = 1024 * 1024
_FAILURE = 'Explicit-target Kubernetes Pod read unavailable'
_LOG = logging.getLogger(__name__)


def _one(values, name, field):
    if not isinstance(values, list):
        raise ValueError()
    matches = [item[field] for item in values if isinstance(item, dict)
               and item.get('name') == name and isinstance(item.get(field), dict)]
    if len(matches) != 1:
        raise ValueError()
    return matches[0]


def _pem(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 * 1024:
        raise ValueError()
    decoded = base64.b64decode(value, validate=True)
    if not decoded or len(decoded) > 96 * 1024:
        raise ValueError()
    return decoded


def _tls_context(ca, certificate, key):
    """OpenSSL reads anonymous Linux files; no persistent key file is created."""
    context = ssl.create_default_context(cadata=ca.decode('ascii'))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    descriptors = []
    try:
        for value in (certificate, key):
            fd = os.memfd_create('cityverse-k8s-x509', os.MFD_CLOEXEC)
            descriptors.append(fd)
            os.fchmod(fd, 0o600)
            with os.fdopen(os.dup(fd), 'wb') as stream:
                stream.write(value)
        def no_password():
            raise ValueError()
        context.load_cert_chain(f'/proc/self/fd/{descriptors[0]}',
                                f'/proc/self/fd/{descriptors[1]}', password=no_password)
    finally:
        for fd in descriptors:
            os.close(fd)
    return context


class KubernetesPodRead:
    def __init__(self, context, namespace, *, kubeconfig=None, capacity=1):
        try:
            if (type(capacity) is not int or not 1 <= capacity <= 4
                    or not isinstance(context, str) or not context
                    or not isinstance(namespace, str)
                    or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', namespace)):
                raise ValueError()
            raw_path = kubeconfig if kubeconfig is not None else os.environ.get('KUBECONFIG')
            if not isinstance(raw_path, (str, Path)) or not raw_path:
                raise ValueError()
            path = Path(raw_path)
            if not path.is_absolute():
                raise ValueError()
            with path.open('rb') as stream:
                metadata = os.fstat(stream.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_BODY_BYTES
                        or metadata.st_mode & 0o022 or metadata.st_uid not in {0, os.geteuid()}):
                    raise ValueError()
                raw = stream.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                raise ValueError()
            document = yaml.safe_load(raw)
            selected = _one(document['contexts'], context, 'context')
            if (set(selected) - {'cluster', 'user', 'namespace'}
                    or selected.get('namespace', namespace) != namespace):
                raise ValueError()
            cluster = _one(document['clusters'], selected['cluster'], 'cluster')
            user = _one(document['users'], selected['user'], 'user')
            # No exec/token providers, insecure TLS, proxy, or implicit context.
            if (set(cluster) != {'server', 'certificate-authority-data'}
                    or set(user) != {'client-certificate-data', 'client-key-data'}):
                raise ValueError()
            raw_server = cluster['server']
            if not isinstance(raw_server, str) or any(ord(c) <= 32 or ord(c) >= 127 for c in raw_server):
                raise ValueError()
            server = urlsplit(raw_server)
            if (server.scheme != 'https' or not server.hostname or server.username is not None
                    or server.password is not None or server.path not in {'', '/'}
                    or server.query or server.fragment):
                raise ValueError()
            host = server.hostname
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                if (len(host) > 253 or not re.fullmatch(r'[A-Za-z0-9.-]+', host)
                        or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?',
                                              part) for part in host.rstrip('.').split('.'))):
                    raise ValueError()
            else:
                if address.is_unspecified or address.is_multicast:
                    raise ValueError()
            self.host, self.port = host, server.port or 443
            self.context = _tls_context(_pem(cluster['certificate-authority-data']),
                                        _pem(user['client-certificate-data']),
                                        _pem(user['client-key-data']))
            self.path = '/api/v1/namespaces/' + namespace + '/pods'
            self.namespace = namespace
            self._initialize_pool(capacity)
        except Exception:
            raise EvidenceUnavailable(_FAILURE) from None

    def _initialize_pool(self, capacity):
        self.capacity = capacity
        self._pool = Condition()
        self._slots = [{'connection': None, 'busy': False} for _ in range(capacity)]
        self._closed = False

    @staticmethod
    def _discard(slot):
        connection, slot['connection'] = slot['connection'], None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    @staticmethod
    def _remaining(deadline):
        try:
            left = deadline - time.monotonic()
        except OverflowError:
            raise TimeoutError() from None
        if not 0 < left <= TIMEOUT_MAX:
            raise TimeoutError()
        return left

    def _lease(self, deadline):
        if not self._pool.acquire(timeout=self._remaining(deadline)):
            raise TimeoutError()
        try:
            while not self._closed:
                self._remaining(deadline)
                for slot in self._slots:
                    if not slot['busy']:
                        slot['busy'] = True
                        return slot
                self._pool.wait(timeout=self._remaining(deadline))
            raise EvidenceUnavailable(_FAILURE)
        finally:
            self._pool.release()

    def _release(self, slot):
        with self._pool:
            closing = self._closed
            if not closing:
                slot['busy'] = False
                self._pool.notify_all()
                return
        # Keep the lease marked busy until its actual I/O and close have ended.
        self._discard(slot)
        with self._pool:
            slot['busy'] = False
            self._pool.notify_all()

    def close(self):
        """Stop admission and drain original bounded reads before closing FDs."""
        with self._pool:
            idle = []
            if not self._closed:
                self._closed = True
                for slot in self._slots:
                    if not slot['busy']:
                        slot['busy'] = True
                        idle.append(slot)
                self._pool.notify_all()
        for slot in idle:
            self._release(slot)
        with self._pool:
            while any(slot['busy'] for slot in self._slots):
                self._pool.wait()

    def _failure(self, diagnostic, error, started, deadline):
        """One failure-only, fixed-field record; never log target or error text."""
        try:
            allowed = {'TimeoutError', 'OSError', 'ConnectionError', 'ConnectionResetError',
                       'ConnectionRefusedError', 'RemoteDisconnected', 'BadStatusLine',
                       'IncompleteRead', 'SSLError', 'SSLCertVerificationError',
                       'JSONDecodeError', 'UnicodeDecodeError', 'ValueError',
                       'EvidenceUnavailable'}
            def milliseconds(value):
                return round(min(1e9, max(0., value * 1000)), 3) if math.isfinite(value) else None
            now = time.monotonic()
            record = {**diagnostic, 'capacity': self.capacity,
                      'elapsedMs': milliseconds(now - started),
                      'remainingMs': milliseconds(deadline - now),
                      'errorClass': type(error).__name__ if type(error).__name__ in allowed else 'Other'}
            _LOG.warning('kubernetes_pod_read_failed %s', record)
        except Exception:
            pass  # Diagnostics cannot replace or disclose the original failure.

    def pods(self, *, deadline, managed=False):
        """Only all namespace Pods or the controller's fixed managed selector."""
        return self._read(deadline=deadline, managed=managed, namespace=False)

    def namespace_record(self, *, deadline):
        """Only the configured namespace; no caller-provided resource path."""
        return self._read(deadline=deadline, managed=False, namespace=True)

    def pod(self, name, *, deadline):
        if (type(name) is not str or len(name) > 253
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                       for label in name.split('.'))):
            raise EvidenceUnavailable(_FAILURE)
        return self._read(deadline=deadline, managed=False, namespace=False, pod_name=name)

    def workloads(self, resource, *, deadline):
        """Only native deployments/statefulsets for city bootstrap readiness."""
        if type(resource) is not str or resource not in {'deployments', 'statefulsets'}:
            raise EvidenceUnavailable(_FAILURE)
        return self._read(deadline=deadline, managed=False, namespace=False, resource=resource)

    def services(self, *, deadline):
        """Fixed namespace ServiceList for commissioned gNB selector checks."""
        return self._read(deadline=deadline, managed=False, namespace=False, resource='services')

    def traffic_logs(self, name, *, deadline, since_time):
        """One bounded text GET for the fixed current traffic container only."""
        if (type(name) is not str or len(name) > 253
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                       for label in name.split('.'))
                or type(deadline) not in {int, float}
                or type(since_time) is not str or not re.fullmatch(
                    r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z', since_time)):
            raise EvidenceUnavailable(_FAILURE)
        slot, response, transport_socket = None, None, None
        io_complete = False
        try:
            slot = self._lease(deadline)
            def budget():
                remaining = self._remaining(deadline)
                connection = slot['connection']
                if connection is not None:
                    connection.timeout = remaining
                    active = transport_socket or connection.sock
                    if active is not None and not io_complete:
                        active.settimeout(remaining)
                return remaining
            if slot['connection'] is None:
                slot['connection'] = HTTPSConnection(self.host, self.port,
                    context=self.context, timeout=budget())
            connection = slot['connection']
            path = (self.path + '/' + quote(name, safe='') + '/log?container=traffic'
                    '&follow=false&previous=false&timestamps=true&tailLines=128&limitBytes=32768'
                    '&sinceTime=' + quote(since_time, safe=''))
            budget()
            connection.request('GET', path, headers={'Accept': 'text/plain'})
            transport_socket = connection.sock
            budget()
            response = connection.getresponse()
            if (response.status != 200 or response.length is not None
                    and (type(response.length) is not int or not 0 <= response.length <= 65536)):
                raise ValueError()
            body = bytearray()
            while response.length != 0:
                budget()
                chunk = response.read1(min(16384, 65537 - len(body)))
                if not chunk:
                    if response.length not in {None, 0}:
                        raise ValueError()
                    break
                body.extend(chunk)
                if len(body) > 65536:
                    raise ValueError()
            io_complete = True
            budget()
            response.close()
            response = None
            budget()
            return bytes(body)
        except Exception:
            if slot is not None:
                self._discard(slot)
            raise EvidenceUnavailable(_FAILURE) from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            if slot is not None:
                self._release(slot)

    def _read(self, *, deadline, managed, namespace, resource=None, pod_name=None):
        if type(managed) is not bool or type(deadline) not in {int, float}:
            raise EvidenceUnavailable(_FAILURE)
        started = time.monotonic()
        diagnostic = {'stage': 'pool_wait', 'attempts': 0, 'reused': False,
                      'httpStatus': None, 'receivedBytes': 0, 'declaredBytes': None}
        slot = None
        error = None
        succeeded = False
        try:
            slot = self._lease(deadline)
            path = ('/api/v1/namespaces/' + self.namespace if namespace else
                    self.path + ('?labelSelector=' + quote(LABEL, safe='') if managed else ''))
            if resource is not None:
                path = ('/api/v1/namespaces/' if resource == 'services' else '/apis/apps/v1/namespaces/') + self.namespace + '/' + resource
            elif pod_name is not None:
                path = self.path + '/' + quote(pod_name, safe='')
            for attempt in range(2):
                # Pool wait and both existing GET attempts share caller deadline.
                self._remaining(deadline)
                diagnostic.update(attempts=attempt + 1, reused=slot['connection'] is not None,
                                  httpStatus=None, receivedBytes=0, declaredBytes=None)
                try:
                    result = self._read_attempt(slot, path, min(deadline, time.monotonic() + 5),
                                                diagnostic, namespace, resource, pod_name)
                    succeeded = True
                    break
                except Exception as exc:
                    error = exc
                    self._discard(slot)  # Only this lease's transport is poisoned.
                if time.monotonic() >= deadline:
                    break
        except Exception as exc:
            error = exc
        finally:
            if slot is not None:
                self._release(slot)
        if succeeded:
            try:
                self._remaining(deadline)
                return result
            except Exception as exc:
                error = exc
        self._failure(diagnostic, error, started, deadline)
        raise EvidenceUnavailable(_FAILURE) from None

    def _read_attempt(self, slot, path, attempt_deadline, diagnostic, namespace, resource, pod_name):
        response = None
        transport_socket = None
        try:
            def budget():
                left = self._remaining(attempt_deadline)
                connection = slot['connection']
                if connection is not None:
                    active_socket = transport_socket or connection.sock
                    if active_socket is not None:
                        active_socket.settimeout(left)
                    connection.timeout = left
                return left

            diagnostic['stage'] = 'connection'
            left = budget()
            if slot['connection'] is None:
                slot['connection'] = HTTPSConnection(self.host, self.port,
                    context=self.context, timeout=left)
            connection = slot['connection']
            diagnostic['stage'] = 'request'
            budget()
            connection.request('GET', path, headers={'Accept': 'application/json'})
            transport_socket = connection.sock
            diagnostic['stage'] = 'headers'
            budget()
            response = connection.getresponse()
            diagnostic['httpStatus'] = response.status if type(response.status) is int and 100 <= response.status <= 599 else None
            diagnostic['declaredBytes'] = min(response.length, MAX_BODY_BYTES + 1) if type(response.length) is int and response.length >= 0 else None
            if response.status != 200 or (response.length is not None
                                         and response.length > MAX_BODY_BYTES):
                raise ValueError()
            diagnostic['stage'] = 'body'
            body = bytearray()
            while response.length != 0:
                budget()
                chunk = response.read1(min(65536, MAX_BODY_BYTES + 1 - len(body)))
                if not chunk:
                    if response.length not in {None, 0}:
                        raise ValueError()
                    break
                body.extend(chunk)
                diagnostic['receivedBytes'] = min(len(body), MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    raise ValueError()
            diagnostic['stage'] = 'decode'
            self._remaining(attempt_deadline)
            data = json.loads(body)
            diagnostic['stage'] = 'shape'
            self._remaining(attempt_deadline)
            if (not isinstance(data, dict)
                    or data.get('apiVersion') != ('apps/v1' if resource and resource != 'services' else 'v1')):
                raise ValueError()
            if namespace:
                valid = (data.get('kind') == 'Namespace'
                         and data.get('metadata', {}).get('name') == self.namespace)
            elif pod_name is not None:
                valid = (data.get('kind') == 'Pod'
                         and data.get('metadata', {}).get('name') == pod_name
                         and data.get('metadata', {}).get('namespace') == self.namespace)
            else:
                expected_kind = ({'deployments': 'DeploymentList', 'statefulsets': 'StatefulSetList', 'services': 'ServiceList'}[resource]
                                 if resource else 'PodList')
                valid = (data.get('kind') == expected_kind and isinstance(data.get('items'), list)
                         and all(isinstance(item, dict)
                                 and item.get('metadata', {}).get('namespace') == self.namespace
                                 for item in data['items']))
            if not valid:
                raise ValueError()
            self._remaining(attempt_deadline)
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
        # Include response drain/close and validation in this attempt's budget.
        self._remaining(attempt_deadline)
        return data
