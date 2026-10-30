"""Shared lab topology defaults, also mounted by the subscriber provisioner.

These values describe the deployed network. They do not contain UE demand,
observations or references to any historical test generation.
"""
SLICES = {
    "embb": {"sst": 1, "sd": "000001", "pool": "10.101.0.0/21"},
    "urllc": {"sst": 2, "sd": "000002", "pool": "10.102.0.0/21"},
    "mmtc": {"sst": 3, "sd": "000004", "pool": "10.103.0.0/21"},
    "v2x": {"sst": 4, "sd": "000005", "pool": "10.104.0.0/21"},
}
BASE_IMSI = 208930100000001
ENDPOINTS = {name: f"172.22.{61 + index}.20" for index, name in enumerate(SLICES)}
