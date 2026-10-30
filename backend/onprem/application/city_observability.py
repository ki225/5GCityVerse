"""Coalesced city view. GETs never launch traffic, sample receivers, or renew proof."""
from copy import deepcopy
import hashlib
import json
import os
import threading
import time

import yaml

from .city_health import project_health, unknown_health
from .city_infra import configured_infra, VALUES
from .city_paths import project_paths


class CityObservabilityReader:
    def __init__(self, context, *, inventory_reader=None, nodes_reader=None, config=None, topology_revision=None, clock=time.time):
        self.context, self.clock = context, clock
        path = os.environ.get('CITYVERSE_ONPREM_CITY_VALUES') or VALUES
        self.config = config if config is not None else configured_infra(path)
        if config is not None:
            self.revision = topology_revision
        else:
            from pathlib import Path
            self.revision = yaml.safe_load(Path(path).read_text(encoding='utf-8'))['city'].get('topologyRevision', 1)
        self.topology_digest = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()
        self.inventory_reader = inventory_reader or (lambda: self._read('pods,deployments,statefulsets'))
        self.nodes_reader = nodes_reader or (lambda: self._read('nodes', namespaced=False))
        self.lock, self.cached, self.expires = threading.Lock(), None, 0
        self.health_expires, self.health = 0, None

    def _read(self, resource, *, namespaced=True):
        driver = self.context.ue_controller.driver
        if driver is None:
            raise ValueError('Cluster not configured')
        command = ['kubectl']
        if os.environ.get('KUBECONFIG'):
            command += ['--kubeconfig', os.environ['KUBECONFIG']]
        command += ['--context', driver.context]
        if namespaced:
            command += ['--namespace', driver.namespace]
        command += ['get', resource, '--request-timeout=8s', '-o', 'json']
        result = driver.runner(command, text=True, capture_output=True, timeout=10, check=False)
        if result.returncode or len(result.stdout.encode()) > 4 * 1024 * 1024:
            raise ValueError('Inventory unavailable')
        return json.loads(result.stdout)

    def snapshot(self):
        with self.lock:
            if self.cached is not None and time.monotonic() < self.expires:
                return deepcopy(self.cached)
            if self.health is None or time.monotonic() >= self.health_expires:
                try:
                    observed = self.clock()  # Start of collection, never response time.
                    inventory = self.inventory_reader()
                    try:
                        nodes = self.nodes_reader()
                    except Exception:
                        nodes = None  # Workload readiness still useful without node-list RBAC.
                    namespace = self.context.ue_controller.driver.namespace
                    self.health = project_health(inventory, nodes, self.config, namespace, observed)
                except Exception:
                    self.health = (unknown_health(self.config), [], {})
                self.health_expires = time.monotonic() + 15
            infra, pods, node_sites = self.health
            paths, scope, reason = [], None, 'round_observation_unavailable'
            try:
                raw = self.context.round_ledger.live_snapshot()
                paths, scope, reason = project_paths(raw, self.config, pods, node_sites, self.clock(), topology_revision=self.revision)
                current = self.context.round_ledger.control_snapshot()
                if any(current.get(k) != raw.get(k) for k in ('roundId', 'generation', 'populationRevision', 'stage')):
                    paths, scope, reason = [], None, 'round_scope_changed'
            except Exception:
                paths, scope, reason = [], None, 'round_observation_unavailable'
            self.cached = dict(schemaVersion='cityverse.city-observability/v1',
                topologyRevision=self.revision, topologyDigest=self.topology_digest,
                scope=scope, infra=infra, paths=paths, pathStatus=reason, usageAvailable=False)
            self.expires = time.monotonic() + 5
            return deepcopy(self.cached)
