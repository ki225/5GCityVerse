"""Validate operator-owned provider composition before constructing native adapters.

This checks configuration, not infrastructure readiness or permission to mutate.
No credential value or cluster state is read during validation.
"""
from copy import deepcopy
from pathlib import Path
import re
from urllib.parse import urlsplit


_FIELDS = {
    'qos': ({'origin', 'token_file'}, {'controller_origin', 'timeout'}),
    'kubernetes': ({'context', 'namespace', 'pod_allowlist'},
                   {'in_place_resize', 'observer_origin', 'token_file', 'timeout'}),
    'upf': ({'origin', 'token_file', 'templates'}, {'timeout'}),
    'applications': ({'origin', 'token_file', 'templates'}, {'timeout'}),
    'nodes': ({'origin', 'token_file', 'pools'}, {'timeout'}),
    'service_routes': ({'journal_file', 'context', 'namespace', 'session_targets'}, set()),
}


def _name(value):
    return isinstance(value, str) and re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', value)


def validate_provider_configuration(config):
    if config is None:
        return {}
    if not isinstance(config, dict) or set(config) - set(_FIELDS):
        raise ValueError('Unknown or invalid native provider configuration')
    for name, values in config.items():
        required, optional = _FIELDS[name]
        if not isinstance(values, dict) or not required <= set(values) or set(values) - required - optional:
            raise ValueError('Incomplete or unknown native provider fields: ' + name)
        for key in ('origin', 'controller_origin', 'observer_origin'):
            if key not in values:
                continue
            parsed = urlsplit(values[key])
            if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                    or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment):
                raise ValueError('Fixed native provider origin required: ' + name)
        for key in ('token_file', 'journal_file'):
            if key in values and (not isinstance(values[key], str) or not Path(values[key]).is_absolute()):
                raise ValueError('Absolute operator-owned native provider path required: ' + name)
        if 'timeout' in values and (type(values['timeout']) not in (int, float)
                                    or not 0 < values['timeout'] <= 30):
            raise ValueError('Bounded native provider timeout required: ' + name)
        if 'context' in values and (not isinstance(values['context'], str) or not values['context'].strip()):
            raise ValueError('Exact native Kubernetes context required')
        if 'namespace' in values and not _name(values['namespace']):
            raise ValueError('Exact native Kubernetes namespace required')
        if 'in_place_resize' in values and type(values['in_place_resize']) is not bool:
            raise ValueError('Explicit boolean native resize capability required')
        if name == 'kubernetes' and bool(values.get('observer_origin')) != bool(values.get('token_file')):
            raise ValueError('Remote Kubernetes observer requires origin and credential together')
        for key in ('templates', 'pools', 'pod_allowlist'):
            if key in values:
                rows = values[key]
                if (not isinstance(rows, (list, tuple)) or (not rows and not (name == 'upf' and key == 'templates')) or len(rows) > 64
                        or not all(_name(row) for row in rows) or len(set(rows)) != len(rows)):
                    raise ValueError('Unique nonempty bounded provider scope required: ' + key)
        if name == 'service_routes' and (not isinstance(values['session_targets'], dict)
                                         or not values['session_targets']):
            raise ValueError('Exact native service route session targets required')
    return deepcopy(config)
