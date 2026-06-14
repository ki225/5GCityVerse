#!/usr/bin/env python3
"""
Seed free5GC subscribers for every 5GCityVerse scenario.

Environment:
  FREE5GC_WEBUI_URL       default http://localhost:5000
  FREE5GC_WEBUI_USERNAME  default admin
  FREE5GC_WEBUI_PASSWORD  default free5gc
  FREE5GC_PLMN_ID         default 20893
"""

import json
import os
import urllib.error
import urllib.request


FREE5GC_WEBUI_URL = os.environ.get("FREE5GC_WEBUI_URL", "http://localhost:5000").rstrip("/")
USERNAME = os.environ.get("FREE5GC_WEBUI_USERNAME", "admin")
PASSWORD = os.environ.get("FREE5GC_WEBUI_PASSWORD", "free5gc")
PLMN_ID = os.environ.get("FREE5GC_PLMN_ID", "20893")

AUTH_KEY = "8baf473f2f8fd09487cccbd7097c6862"
AUTH_OPC = "8e27b6af0e692e750f32667a3b14605d"


SCENARIOS = {
    "concert": {
        "ue_ids": ["imsi-208930000000001"],
        "sst": 1,
        "sd": "000001",
        "dnn": "internet",
        "five_qi": 9,
        "ue_ambr": ("1 Gbps", "1 Gbps"),
        "session_ambr": ("1 Gbps", "1 Gbps"),
        "priority": 8,
        "preempt_cap": "NOT_PREEMPT",
        "preempt_vuln": "NOT_PREEMPTABLE",
    },
    "medical": {
        "ue_ids": ["imsi-208930000000002"],
        "sst": 2,
        "sd": "000002",
        "dnn": "internet",
        "five_qi": 1,
        "ue_ambr": ("50 Mbps", "50 Mbps"),
        "session_ambr": ("50 Mbps", "50 Mbps"),
        "gbr": ("10 Mbps", "10 Mbps"),
        "mbr": ("10 Mbps", "10 Mbps"),
        "priority": 1,
        "preempt_cap": "MAY_PREEMPT",
        "preempt_vuln": "NOT_PREEMPTABLE",
    },
    "typhoon": {
        "ue_ids": [f"imsi-20893000000{i:04d}" for i in range(10, 13)],
        "sst": 2,
        "sd": "000003",
        "dnn": "emergency",
        "five_qi": 2,
        "ue_ambr": ("20 Mbps", "20 Mbps"),
        "session_ambr": ("20 Mbps", "20 Mbps"),
        "gbr": ("5 Mbps", "5 Mbps"),
        "mbr": ("5 Mbps", "5 Mbps"),
        "priority": 1,
        "preempt_cap": "MAY_PREEMPT",
        "preempt_vuln": "NOT_PREEMPTABLE",
    },
    "iot_surge": {
        "ue_ids": [f"imsi-20893000000{i:04d}" for i in range(100, 150)],
        "sst": 3,
        "sd": "000004",
        "dnn": "iot",
        "five_qi": 79,
        "ue_ambr": ("1 Mbps", "1 Mbps"),
        "session_ambr": ("1 Mbps", "1 Mbps"),
        "priority": 15,
        "preempt_cap": "NOT_PREEMPT",
        "preempt_vuln": "PREEMPTABLE",
    },
    "accident": {
        "ue_ids": ["imsi-208930000000003"],
        "sst": 4,
        "sd": "000005",
        "dnn": "internet",
        "five_qi": 75,
        "ue_ambr": ("200 Mbps", "200 Mbps"),
        "session_ambr": ("200 Mbps", "200 Mbps"),
        "priority": 2,
        "preempt_cap": "MAY_PREEMPT",
        "preempt_vuln": "NOT_PREEMPTABLE",
    },
}


def http_json(method, url, body=None, headers=None):
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            raw = res.read().decode("utf-8")
            return res.status, json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return exc.code, parsed


def login():
    status, data = http_json(
        "POST",
        f"{FREE5GC_WEBUI_URL}/api/login",
        {"username": USERNAME, "password": PASSWORD},
    )
    token = data.get("access_token") or data.get("token")
    if status >= 300 or not token:
        raise RuntimeError(f"free5GC login failed: HTTP {status} {data}")
    return token


def subscriber_profile(imsi, scenario, cfg):
    sst = cfg["sst"]
    sd = cfg["sd"]
    dnn = cfg["dnn"]
    snssai_key = f"{sst:02d}{sd}"
    ue_ul, ue_dl = cfg["ue_ambr"]
    session_ul, session_dl = cfg["session_ambr"]
    dnn_config = {
        "pduSessionTypes": {"defaultSessionType": "IPV4", "allowedSessionTypes": ["IPV4"]},
        "sscModes": {"defaultSscMode": "SSC_MODE_1", "allowedSscModes": ["SSC_MODE_2", "SSC_MODE_3"]},
        "5gQosProfile": {
            "5qi": cfg["five_qi"],
            "arp": {
                "priorityLevel": cfg["priority"],
                "preemptCap": cfg["preempt_cap"],
                "preemptVuln": cfg["preempt_vuln"],
            },
            "priorityLevel": cfg["priority"],
        },
        "sessionAmbr": {"uplink": session_ul, "downlink": session_dl},
        "staticIpAddress": [],
    }
    if cfg.get("gbr"):
        gbr_ul, gbr_dl = cfg["gbr"]
        mbr_ul, mbr_dl = cfg["mbr"]
        dnn_config["gbrQosFlowInfo"] = {
            "maxFbrUplink": mbr_ul,
            "maxFbrDownlink": mbr_dl,
            "guaranteedFbrUplink": gbr_ul,
            "guaranteedFbrDownlink": gbr_dl,
        }

    return {
        "plmnID": PLMN_ID,
        "ueId": imsi,
        "AuthenticationSubscription": {
            "authenticationMethod": "5G_AKA",
            "permanentKey": {"permanentKeyValue": AUTH_KEY},
            "sequenceNumber": "000000000023",
            "authenticationManagementField": "8000",
            "milenage": {"op": {"opValue": AUTH_OPC}},
            "opc": {"opcValue": AUTH_OPC},
        },
        "AccessAndMobilitySubscriptionData": {
            "gpsis": [f"msisdn-{imsi[-10:]}"],
            "subscribedUeAmbr": {"uplink": ue_ul, "downlink": ue_dl},
            "nssai": {
                "defaultSingleNssais": [{"sst": sst, "sd": sd, "isDefault": True}],
                "singleNssais": [{"sst": sst, "sd": sd, "isDefault": True}],
            },
        },
        "SessionManagementSubscriptionData": [
            {
                "singleNssai": {"sst": sst, "sd": sd},
                "dnnConfigurations": {dnn: dnn_config},
            }
        ],
        "SmfSelectionSubscriptionData": {
            "subscribedSnssaiInfos": {snssai_key: {"dnnInfos": [{"dnn": dnn}]}}
        },
        "AmPolicyData": {"subscCats": ["5GCityVerse", scenario]},
        "SmPolicyData": {
            "smPolicySnssaiData": {
                snssai_key: {
                    "snssai": {"sst": sst, "sd": sd},
                    "smPolicyDnnData": {dnn: {"dnn": dnn}},
                }
            }
        },
    }


def create_subscriber(token, imsi, profile):
    headers = {"Token": token, "Content-Type": "application/json"}
    path = f"/api/subscriber/{imsi}/{PLMN_ID}"
    status, data = http_json("POST", f"{FREE5GC_WEBUI_URL}{path}", profile, headers)
    action = "created"
    if status >= 400:
        status, data = http_json("PUT", f"{FREE5GC_WEBUI_URL}{path}", profile, headers)
        action = "updated"
    if status < 300:
        print(f"  {action}: {imsi}")
    else:
        print(f"  failed:  {imsi} -> HTTP {status} {data}")
    return status


def main():
    print(f"Logging in to free5GC WebUI: {FREE5GC_WEBUI_URL}")
    token = login()
    created = 0
    failed = 0

    for scenario, cfg in SCENARIOS.items():
        print(f"\n[{scenario}] {len(cfg['ue_ids'])} subscriber(s)")
        for imsi in cfg["ue_ids"]:
            status = create_subscriber(token, imsi, subscriber_profile(imsi, scenario, cfg))
            if status < 300:
                created += 1
            else:
                failed += 1

    print(f"\nDone. success={created} failed={failed}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
