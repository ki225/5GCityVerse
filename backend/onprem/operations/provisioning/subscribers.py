"""Idempotent four-slice subscriber pool provisioning through WebConsole API.

Run explicitly as an installation operation, never from a browser request.
Credentials come from a mounted Secret JSON file, never command-line flags.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from backend.onprem.runtime.topology import BASE_IMSI, SLICES


def credentials(path: Path):
    groups = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(groups, list) or len(groups) != 4 or any(
        not isinstance(group, dict) or any(not isinstance(group.get(key), str) or not re.fullmatch(r"[0-9a-fA-F]{32}", group[key]) for key in ("key", "opc"))
        for group in groups
    ):
        raise ValueError("Credential Secret must contain four key/opc groups, each 32 hex digits")
    return groups


def subscriber(index: int, groups: list[dict]):
    if not 0 <= index < 1000:
        raise ValueError("Subscriber index out of range")
    credential = groups[index // 250]
    snssais = [{"sst": value["sst"], "sd": value["sd"]} for value in SLICES.values()]
    dnn = {"sscModes": {"defaultSscMode": "SSC_MODE_1", "allowedSscModes": ["SSC_MODE_1"]},
           "pduSessionTypes": {"defaultSessionType": "IPV4", "allowedSessionTypes": ["IPV4"]},
           "sessionAmbr": {"uplink": "100 Mbps", "downlink": "100 Mbps"},
           "5gQosProfile": {"5qi": 9, "arp": {"priorityLevel": 8, "preemptCap": "NOT_PREEMPT", "preemptVuln": "NOT_PREEMPTABLE"}}}
    return {"plmnID": "20893", "ueId": f"imsi-{BASE_IMSI + index}",
            "AuthenticationSubscription": {"authenticationManagementField": "8000", "authenticationMethod": "5G_AKA",
                "opc": {"opcValue": credential["opc"]}, "permanentKey": {"permanentKeyValue": credential["key"]}, "sequenceNumber": "000000000000"},
            "AccessAndMobilitySubscriptionData": {"gpsis": [f"msisdn-8869{index:08d}"],
                "nssai": {"defaultSingleNssais": [snssais[0]], "singleNssais": snssais},
                "subscribedUeAmbr": {"uplink": "100 Mbps", "downlink": "100 Mbps"}},
            "SessionManagementSubscriptionData": [{"singleNssai": snssai, "dnnConfigurations": {"internet": deepcopy(dnn)}} for snssai in snssais],
            "SmfSelectionSubscriptionData": {"subscribedSnssaiInfos": {f"{snssai['sst']:02d}{snssai['sd']}": {"dnnInfos": [{"dnn": "internet"}]} for snssai in snssais}},
            "AmPolicyData": {"subscCats": ["5GCityVerse-onprem"]},
            "SmPolicyData": {"smPolicySnssaiData": {f"{snssai['sst']:02d}{snssai['sd']}": {"snssai": snssai, "smPolicyDnnData": {"internet": {"dnn": "internet"}}} for snssai in snssais}}}


def managed_projection(body, template=None):
    """Compare managed fields, ignoring server defaults without hiding drift.

    The pinned SubsData Go field WebAuthenticationSubscription has JSON tag
    AuthenticationSubscription. Its SQN is a web copy, NOT live AKA state.
    """
    if template is None:
        result = deepcopy(body)
        result.pop("_id", None)
        result.get("AuthenticationSubscription", {}).pop("sequenceNumber", None)
        return result

    def project(value, expected, key=""):
        if isinstance(expected, dict):
            if not isinstance(value, dict):
                return None
            if key in {"subscribedSnssaiInfos", "smPolicySnssaiData", "dnnConfigurations", "smPolicyDnnData"} and set(value) != set(expected):
                return None
            return {name: project(value.get(name), child, name) for name, child in expected.items()
                    if name != "sequenceNumber"}
        if isinstance(expected, list):
            if not isinstance(value, list) or len(value) != len(expected):
                return None
            if not expected:
                return []
            # Slice order and MongoDB document order are not identity.
            order = lambda item: json.dumps(item.get("singleNssai", item) if isinstance(item, dict) else item, sort_keys=True)
            left, right = sorted(value, key=order), sorted(expected, key=order)
            return [project(item, schema) for item, schema in zip(left, right)]
        return value if type(value) is type(expected) else None
    return project(body, template)


class WebConsole:
    def __init__(self, url, token="", opener=urlopen):
        parsed = urlsplit(url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Explicit on-prem WebConsole URL is required, without embedded credentials")
        self.url, self.token = url.rstrip("/"), token
        self.opener = opener

    @classmethod
    def from_env(cls):
        client = cls(os.environ.get("CITYVERSE_ONPREM_WEBCONSOLE_URL"), os.environ.get("CITYVERSE_ONPREM_WEBCONSOLE_TOKEN", ""))
        if not client.token:
            auth_file = os.environ.get("CITYVERSE_ONPREM_WEBCONSOLE_CREDENTIALS_FILE")
            auth = json.loads(Path(auth_file).read_text(encoding="utf-8")) if auth_file else {
                "username": os.environ.get("CITYVERSE_ONPREM_WEBCONSOLE_USERNAME"),
                "password": os.environ.get("CITYVERSE_ONPREM_WEBCONSOLE_PASSWORD")}
            client.login(auth.get("username"), auth.get("password"))
        return client

    def login(self, username, password):
        if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
            raise ValueError("WebConsole login credentials are not configured")
        code, body = self.request("POST", "/api/login", {"username": username, "password": password})
        if code != 200 or not isinstance(body, dict) or not isinstance(body.get("access_token"), str) or not body["access_token"]:
            raise RuntimeError("WebConsole login failed")
        self.token = body["access_token"]

    def request(self, method, path, body=None):
        request = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                          method=method, headers={"Token": self.token, "Content-Type": "application/json"})
        try:
            # The full 1000-subscriber list took about 9 seconds on the real
            # lab and crossed the former 10-second bound intermittently.
            # Only this read gets extra time; writes are never auto-retried.
            timeout = 30 if method == "GET" and path == "/api/subscriber" else 10
            with self.opener(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except HTTPError as exc:
            return exc.code, {}  # Do not return/log subscriber credentials.
        except Exception:
            raise RuntimeError("On-prem WebConsole request failed") from None


def provision(client, groups):
    if not isinstance(groups, list) or len(groups) != 4 or any(
        not isinstance(group, dict) or any(not isinstance(group.get(key), str) or not re.fullmatch(r"[0-9a-fA-F]{32}", group[key]) for key in ("key", "opc"))
        for group in groups
    ):
        raise ValueError("Invalid credential groups")
    # GetSubscriberByID returns HTTP200 zero-value SubsData for an absent UE.
    # GetSubscribers is the authoritative WebConsole existence/uniqueness list.
    code, listed = client.request("GET", "/api/subscriber")
    if code != 200 or not isinstance(listed, list) or any(not isinstance(item, dict) for item in listed):
        raise RuntimeError("Subscriber list API is unavailable or malformed")
    existing_ids = [item.get("ueId") for item in listed]
    if any(not isinstance(value, str) for value in existing_ids) or len(existing_ids) != len(set(existing_ids)):
        raise RuntimeError("Subscriber list contains missing or duplicate identities")
    existing = set(existing_ids)
    changed = 0
    digests = []
    for index in range(1000):
        expected = subscriber(index, groups)
        path = f"/api/subscriber/{expected['ueId']}/20893"
        if expected["ueId"] not in existing:
            status, _ = client.request("POST", path + "/1", expected)
            if not 200 <= status < 300:
                raise RuntimeError("Subscriber write failed; provisioning stopped")
            changed += 1
        status, actual = client.request("GET", path)
        if status != 200 or managed_projection(actual, expected) != managed_projection(expected, expected):
            # Pinned WebConsole PUT/PATCH rebuild runtime AuthenticationSubscription
            # from stale web SQN. Refuse rather than resetting live AKA state.
            raise RuntimeError("Subscriber drift or incompatible read-back; refusing unsafe WebConsole overwrite")
        digests.append(hashlib.sha256(json.dumps(managed_projection(actual, expected), sort_keys=True).encode()).hexdigest())
    status, listed = client.request("GET", "/api/subscriber")
    expected_ids = {f"imsi-{BASE_IMSI + index}" for index in range(1000)}
    if status != 200 or not isinstance(listed, list) or any(not isinstance(item, dict) for item in listed):
        raise RuntimeError("Subscriber uniqueness verification unavailable")
    ids = [item.get("ueId") for item in listed if item.get("ueId") in expected_ids]
    if len(ids) != 1000 or set(ids) != expected_ids:
        raise RuntimeError("Subscriber pool is missing identities or contains duplicates")
    return {"verifiedCount": 1000, "changed": changed, "webCredentialsMatched": True,
            "runtimeCredentialsVerified": False, "verificationSource": "free5gc-v4.2.2-webconsole",
            "contentChecksum": hashlib.sha256("".join(digests).encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Explicitly provision the installation subscriber pool")
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required for subscriber database writes")
    try:
        result = provision(WebConsole.from_env(), credentials(args.credentials_file))
    except Exception:
        raise SystemExit("Subscriber provisioning failed; no credentials emitted. Check API capability and mounted Secret.") from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
