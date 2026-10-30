"""Query scope for one exact, durably released operator recovery.

The historical receipt grants a bounded GET inventory, never PCF absence.
Changed snapshots and future operator schemas must undergo their own review.
"""
import hashlib
import json


ROUND_ID = '180e7088-7180-4410-b4d4-9fc11dac6b20'
SNAPSHOT_SHA256 = 'c34fe9b60eedf6eee5a86c420ffb5d6ed1777ef3819794eab50cded1c3ddb6b5'


def recovered_allocation_scope(snapshot, *, released=False):
    if (released is not True or not isinstance(snapshot, dict)
            or snapshot.get('roundId') != ROUND_ID or type(snapshot.get('generation')) is not int
            or snapshot['generation'] != 246):
        return None
    try:
        raw = json.dumps(snapshot, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (ValueError, TypeError):
        return None
    if hashlib.sha256(raw).hexdigest() != SNAPSHOT_SHA256:
        return None
    from backend.onprem.runtime.ue import allocate
    from backend.onprem.scenarios.contracts import slice_counts
    # No PDU binding was published. Inventory the complete legal PDU-ID range
    # for every SUPI in the original deterministic allocation, then require
    # generation_reader to independently match that durable allocation.
    return {'kind': 'allocated_supi_pdu_inventory', 'roundId': ROUND_ID,
            'generation': 246, 'bindings': [
                {'supi': row['supi'], 'sessionId': str(sid)}
                for row in allocate(slice_counts(snapshot['allocation'])) for sid in range(1, 16)]}
