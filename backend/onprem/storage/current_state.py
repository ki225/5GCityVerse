"""Runtime current-state ports; archive readers are never selected for real ledgers."""
from copy import deepcopy


def current_reader(ledger):
    # Minimal legacy test doubles predate the explicit current-state port.
    reader = getattr(ledger, 'live_snapshot', None)
    return reader if callable(reader) else ledger.current


def read_current(ledger):
    return current_reader(ledger)()


def read_cleanup(ledger):
    reader = getattr(ledger, 'control_snapshot', None)
    return reader() if callable(reader) else read_current(ledger)


def mutate_current(ledger, patch=None, *, append=None, action_id=None, action_patch=None):
    """Preserve archived references while changing only explicitly owned state.

    Full reports are never reconstructed for a control mutation. The repository
    still validates bindings/references and its ordinary sequence/release fence.
    """
    snapshot = read_cleanup(ledger)
    changes = deepcopy(patch or {})
    if append is not None or action_id is not None:
        if 'actions' in changes:
            raise ValueError('Action mutation cannot replace the action journal')
        actions = deepcopy(snapshot['actions'])
        if append is not None:
            if any(a.get('id') == append.get('id') for a in actions):
                raise ValueError('Duplicate action identity')
            actions.append(deepcopy(append))
        else:
            matches = [a for a in actions if a.get('id') == action_id]
            if len(matches) != 1:
                raise ValueError('Action mutation requires an exact owned identity')
            matches[0].update(deepcopy(action_patch or {}))
        changes['actions'] = actions
    if callable(getattr(ledger, 'control_snapshot', None)):
        return ledger.update(snapshot['roundId'], snapshot['sequence'], changes, current_only=True)
    return ledger.update(snapshot['roundId'], snapshot['sequence'], changes)


def complete_current(ledger, round_id, sequence, proof):
    if callable(getattr(ledger, 'control_snapshot', None)):
        return ledger.complete(round_id, sequence, proof, current_only=True)
    return ledger.complete(round_id, sequence, proof)


def replay_current(ledger, body):
    if callable(getattr(ledger, 'control_snapshot', None)):
        return ledger.replay_request(body, current_only=True)
    return ledger.replay_request(body)


def reserve_current(ledger, body, **kwargs):
    if callable(getattr(ledger, 'control_snapshot', None)):
        kwargs['current_only'] = True
    return ledger.reserve(body, **kwargs)
