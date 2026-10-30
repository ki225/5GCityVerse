"""Join an authenticated current SMF/kernel census to commissioned UE classification."""
from __future__ import annotations

import math
import time

from .domain import ContractError, SessionKey
from .service_classification import matches_service_types
from backend.onprem.runtime.current_sessions import V2, session_rows


class NativeBindingBatch(dict):
    def __init__(self, proofs, target_session_keys, census_revision,registry_revision=None):
        super().__init__(proofs)
        self.census_revision = census_revision
        self.registry_revision=registry_revision
        self.target_session_keys = tuple((target, tuple(sorted(keys)))
                                        for target, keys in sorted(target_session_keys.items()))


class NativeSessionBindingReader:
    """One native census per city snapshot, without historical reports or SUPI-only joins."""

    def __init__(self, api, *, network_scope, smf_id, upf_bindings, catalogue=(),
                 classification_reader=None, maximum_age_seconds=15, clock=time.time,current_upfs=None):
        self.api, self.scope, self.smf_id = api, network_scope, smf_id
        self.upfs = dict(upf_bindings)
        self.catalogue = tuple(catalogue)
        self.classification_reader = classification_reader
        self.maximum_age, self.clock = maximum_age_seconds, clock
        self.current_upfs=current_upfs
        if not network_scope or not smf_id or len(self.catalogue) > 10000 or not self.upfs or len(self.upfs) > 64:
            raise ContractError('invalid_native_session_inventory_configuration')
        addresses = [row.get('native_node_ip') for row in self.upfs.values()]
        if any(not address for address in addresses) or len(set(addresses)) != len(addresses):
            raise ContractError('ambiguous_commissioned_native_upf_identity')

    def _classification(self, owner, raw, native):
        if self.classification_reader is not None:
            row = self.classification_reader(owner, raw, native)
        else:
            rows = [r for r in self.catalogue if r.get('supi') == owner.get('supi')
                    and str(r.get('pdu_session_id')) == str(owner.get('sessionId'))
                    and r.get('native_generation') == raw.get('generation')]
            if len(rows) != 1:
                raise ContractError('native_session_classification_not_commissioned')
            row = rows[0]
        return self._validate_classification(row, raw, native)

    @staticmethod
    def _validate_classification(row, raw, native):
        matches_service_types((), row.get('service_type'), row.get('service_profile_id', ''))
        snssai = f"{native['sst']}-{native['sd']}"
        if (row.get('source') not in {'ue-configuration', 'ursp'} or not row.get('configuration_id')
                or row.get('supi') != native['supi']
                or str(row.get('pdu_session_id')) != str(native['sessionId'])
                or row.get('native_generation') != raw.get('generation')
                or row.get('s_nssai') != snssai or row.get('dnn') != native['dnn']
                or not row.get('region') or not row.get('service_type')):
            raise ContractError('native_session_classification_not_commissioned')
        return row

    def current_bindings(self, raw,*,upf_scope=None):
        try:
            scope=upf_scope or (self.current_upfs.proof() if self.current_upfs is not None else None)
            upfs=scope.bindings() if scope is not None else self.upfs
            owners = session_rows(raw)
            inventory = self.api.active_inventory()
            if scope is not None:scope.validate_census(inventory)
            at = inventory.get('observed_at')
            if (inventory.get('source') != 'native-smf-active-sessions'
                    or inventory.get('complete') is not True
                    or inventory.get('network_scope') != self.scope
                    or not inventory.get('smf_boot_epoch')
                    or type(inventory.get('census_revision')) is not int or inventory['census_revision'] < 0
                    or type(at) not in (int, float) or not math.isfinite(at)
                    or not 0 <= self.clock() - at <= self.maximum_age
                    or not isinstance(inventory.get('sessions'), list)
                    or len(inventory['sessions']) > 4096):
                raise ValueError('Incomplete native census')
            upf_rows=inventory.get('upfs')
            if not isinstance(upf_rows,list) or not 1<=len(upf_rows)<=64:
                raise ValueError('Missing full native UPF census')
            native_upfs={};upf_ids=set()
            for upf in upf_rows:
                stamp=upf.get('observed_at')
                if (not upf.get('target_id') or not upf.get('node_ip') or not upf.get('upf_id')
                    or not upf.get('upf_epoch') or upf['node_ip'] in native_upfs or upf['upf_id'] in upf_ids
                    or type(stamp) not in (int,float) or not math.isfinite(stamp)
                    or not at<=stamp<=self.clock() or not 0<=self.clock()-stamp<=self.maximum_age):
                    raise ValueError('Ambiguous native UPF census')
                native_upfs[upf['node_ip']]=upf;upf_ids.add(upf['upf_id'])
            for commissioned in upfs.values():
                native_upf=native_upfs[commissioned['native_node_ip']]
                if commissioned.get('native_upf_id',native_upf['upf_id'])!=native_upf['upf_id']:
                    raise ValueError('Commissioned UPF changed')
            native_rows, refs = {}, set()
            for native in inventory['sessions']:
                key = SessionKey(self.scope, native['supi'], native['sessionId'], native['smContextRef'])
                identity = native['supi'], str(native['sessionId'])
                created = native.get('created_at')
                native_upf=native_upfs[native['upf_node_ip']]
                if (native.get('networkScope') != self.scope or native.get('session_key') != key.token
                        or native.get('establishedEpoch') != key.established_epoch
                        or identity in native_rows or key.established_epoch in refs
                        or not native.get('upf_id') or not native.get('upf_node_ip')
                        or native['upf_id']!=native_upf['upf_id'] or native.get('upfEpoch')!=native_upf['upf_epoch']
                        or native.get('smf_boot_epoch')!=inventory['smf_boot_epoch']
                        or not native.get('upfEpoch') or not str(native.get('pfcpSeid', '')).isdigit()
                        or not 0 < int(native['pfcpSeid']) < 2**64
                        or type(created) not in (int, float) or not math.isfinite(created)
                        or not 0 < created <= native_upf['observed_at']):
                    raise ValueError('Ambiguous native session generation')
                native_rows[identity] = native
                refs.add(key.established_epoch)
            cohorts = {target: [] for target in upfs}
            for native in native_rows.values():
                for target, configuration in upfs.items():
                    if configuration['native_node_ip'] == native['upf_node_ip']:
                        cohorts[target].append(native['session_key'])
            result = {}
            classification_batch = None
            if owners and hasattr(self.classification_reader, 'current_classifications'):
                classification_batch = self.classification_reader.current_classifications(raw, native_rows)
            for owner in owners:
                identity = owner.get('supi'), str(owner.get('sessionId'))
                native = native_rows[identity]
                target = upfs[owner['upf']]
                if (target.get('native_node_ip') != native['upf_node_ip']
                        or target.get('native_upf_id', native['upf_id']) != native['upf_id']
                        or owner.get('upfEpoch') != native['upfEpoch']
                        or not target.get('slice_instance_id') or identity in result):
                    raise ValueError('Native UPF identity changed')
                if raw.get('schemaVersion') == V2 and (
                        owner['sessionKey'] != native['session_key']
                        or owner['smContextRef'] != native['smContextRef']
                        or owner['establishedEpoch'] != native['establishedEpoch']
                        or owner['nativeUpfId'] != native['upf_id']
                        or owner['dnn'] != native['dnn']
                        or owner['snssai'] != f"{native['sst']}-{native['sd']}"):
                    raise ValueError('Native device session identity changed')
                classification = (classification_batch[identity] if classification_batch is not None
                                  else self._classification(owner, raw, native))
                if classification_batch is not None:
                    # Batch readers provide native proofs, but the same identity
                    # and classification contract applies to every result.
                    classification = self._validate_classification(classification, raw, native)
                key = SessionKey(self.scope, native['supi'], native['sessionId'], native['smContextRef'])
                # Application isolation is not asserted by a session-level census.
                binding = dict(key=dict(network_scope=key.network_scope, supi=key.supi,
                    pdu_session_id=key.pdu_session_id, established_epoch=key.established_epoch),
                    s_nssai=f"{native['sst']}-{native['sd']}", dnn=native['dnn'], smf_id=self.smf_id,
                    upf_id=owner['upf'], slice_instance_id=target['slice_instance_id'],
                    region=classification['region'], service_type=classification['service_type'],
                    path_revision=f"{inventory['smf_boot_epoch']}:{native['upf_id']}:{native['upfEpoch']}:{native['pfcpSeid']}",
                    flows=[], service_profile_id=classification.get('service_profile_id', ''))
                proof_at=min(at,scope.snapshot.observed_at,*[b.observed_at for b in scope.snapshot.observation_bindings()]) if scope is not None else at
                result[identity] = dict(source='native-smf-session-inventory', observed_at=proof_at,
                    native_supi=native['supi'], native_session_id=native['sessionId'],
                    native_generation=raw['generation'], upf_epoch=native['upfEpoch'],
                    native_upf_id=native['upf_id'],
                    sm_context_ref=native['smContextRef'], smf_boot_epoch=inventory['smf_boot_epoch'],
                    session_created_at=native['created_at'], binding=binding)
            if scope is not None and upf_scope is None:scope.finish()
            return NativeBindingBatch(result, cohorts,
                                      (inventory['smf_boot_epoch'], inventory['census_revision']),
                                      scope.revision if scope is not None else None)
        except ContractError:
            raise
        except Exception:
            raise ContractError('native_session_inventory_binding_unavailable') from None
