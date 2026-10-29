"""Resolve only the server's commissioned traffic-manager SecretRef credential.

Never follows a Pod-provided file path or retrieves arbitrary Kubernetes Secrets.
The bootstrap owns the private token file and the matching Secret/key.
"""
import os
from pathlib import Path
import re
import stat

from backend.onprem.policy.errors import PolicyBlocked


def traffic_manager_token(container, namespace, environment=None):
    environment = os.environ if environment is None else environment
    rows = [row for row in container.get('env', ()) if row.get('name') == 'CITYVERSE_TRAFFIC_MANAGER_TOKEN']
    if len(rows) != 1:
        raise PolicyBlocked('Traffic manager credential unavailable')
    row = rows[0]
    # Existing privately generated UE/traffic Pod credentials remain compatible.
    if set(row) == {'name', 'value'}:
        token = row['value']
    else:
        expected_secret = environment.get('CITYVERSE_ONPREM_TRAFFIC_MANAGER_TOKEN_SECRET', 'cityverse-traffic-manager')
        expected_namespace = environment.get('CITYVERSE_ONPREM_NAMESPACE', 'free5gc-onprem')
        if (set(row) != {'name', 'valueFrom'} or not isinstance(row['valueFrom'], dict)
                or set(row['valueFrom']) != {'secretKeyRef'}):
            raise PolicyBlocked('Traffic manager credential unavailable')
        ref = row['valueFrom']['secretKeyRef']
        if (not isinstance(ref, dict) or not {'name', 'key'} <= set(ref)
                or set(ref) - {'name', 'key', 'optional'} or (ref.get('optional') is not None and ref.get('optional') is not False)
                or ref['name'] != expected_secret or ref['key'] != 'token'
                or not expected_namespace or namespace != expected_namespace):
            raise PolicyBlocked('Traffic manager credential unavailable')
        token_path = Path(environment.get('CITYVERSE_ONPREM_TRAFFIC_MANAGER_TOKEN_FILE',
            '/var/lib/cityverse/operator-native/traffic-manager-token'))
        if not token_path.is_absolute():
            raise PolicyBlocked('Traffic manager credential unavailable')
        try:
            if token_path.is_symlink():
                raise ValueError()
            descriptor = os.open(token_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(descriptor, 'rb') as stream:
                metadata = os.fstat(stream.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 128
                        or (os.name == 'posix' and stat.S_IMODE(metadata.st_mode) & 0o077)):
                    raise ValueError()
                token = stream.read(129).decode('ascii').strip()
        except Exception:
            raise PolicyBlocked('Traffic manager credential unavailable') from None
    if not isinstance(token, str) or re.fullmatch(r'[a-f0-9]{64}', token) is None:
        raise PolicyBlocked('Traffic manager credential unavailable')
    return token
