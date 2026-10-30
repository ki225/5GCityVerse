"""Bounded read-only transport shared by RAN observation and release checks."""
from http.client import HTTPConnection, HTTPException
import ipaddress
import time

from backend.onprem.runtime.ue import EvidenceUnavailable


class RanEvidenceClient:
    @staticmethod
    def address(pod):
        raw = pod.get('status', {}).get('podIP')
        try:
            address = ipaddress.IPv4Address(raw) if type(raw) is str else None
            if (address is None or str(address) != raw or not address.is_private
                    or address.is_loopback or address.is_unspecified or address.is_multicast
                    or address.is_link_local or address.is_reserved):
                raise ValueError()
        except (ValueError, TypeError):
            raise EvidenceUnavailable('RAN evidence Pod address unavailable') from None
        return str(address)

    def read(self, pod, deadline):
        """Fixed endpoint, no proxies/redirects; each response has a byte/time bound."""
        address = self.address(pod)
        for _attempt in range(2):
            attempt_deadline = min(deadline, time.monotonic() + 5)
            remaining = attempt_deadline - time.monotonic()
            if remaining <= 0:
                break
            connection = HTTPConnection(address, 9083, timeout=remaining)
            response = None
            try:
                connection.connect()
                stream = connection.sock

                def budget():
                    remaining = attempt_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    stream.settimeout(remaining)

                budget()
                connection.request('GET', '/v1/evidence')
                budget()
                response = connection.getresponse()
                if response.status != 200:
                    raise EvidenceUnavailable('RAN evidence HTTP response unavailable')
                parts, size = [], 0
                while True:
                    budget()
                    chunk = response.read1(min(65536, 1024 * 1024 + 1 - size))
                    if not chunk:
                        break
                    parts.append(chunk)
                    size += len(chunk)
                    if size > 1024 * 1024:
                        raise EvidenceUnavailable('RAN evidence response exceeds bound')
                    if response.length == 0:
                        break
                if (time.monotonic() >= attempt_deadline
                        or response.length is not None and response.length != 0):
                    raise EvidenceUnavailable('RAN evidence HTTP response incomplete')
                return b''.join(parts).decode('utf-8')
            except (OSError, HTTPException, ValueError, EvidenceUnavailable):
                pass
            finally:
                if response is not None:
                    response.close()
                connection.close()
        raise EvidenceUnavailable('Free-RAN gNB observation transport unavailable')
