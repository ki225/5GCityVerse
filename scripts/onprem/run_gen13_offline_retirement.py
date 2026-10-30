"""Collect fresh offline gen14 evidence and immediately rehearse or run retirement.

Run as WSL root with the native backend stopped. The collector and operator
each acquire the backend lifetime lock; no stale evidence is reused.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


ROUND_DB = "/home/kiki/.local/state/5gcityverse/onprem/onprem-rounds.sqlite3"
UE_DB = "/home/kiki/.local/state/5gcityverse/onprem/ue-state.sqlite3"
RUNTIME_DIR = "/home/kiki/.local/state/5gcityverse/onprem"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-live", action="store_true")
    args = parser.parse_args(argv)
    if os.name != "posix" or os.geteuid() != 0:
        raise SystemExit("WSL root is required")
    collect = subprocess.run(
        [sys.executable, "-m", "archive.legacy.operations.recovery.wsl_gen14_offline_phase_a_collector",
         "--round-db", ROUND_DB, "--ue-db", UE_DB, "--runtime-dir", RUNTIME_DIR],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if collect.returncode != 0:
        print(collect.stdout.strip() or collect.stderr.strip())
        return 2
    receipt = json.loads(collect.stdout)
    path = receipt["privateEvidencePath"]
    command = [sys.executable, "-m", "archive.legacy.operations.recovery.gen13_receiver_retirement_cli",
               "--execute", "--zero", path, "--round-db", ROUND_DB,
               "--ue-db", UE_DB, "--runtime-dir", RUNTIME_DIR]
    if not args.execute_live:
        command.append("--rehearse")
    run = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    print(run.stdout.strip() or run.stderr.strip())
    return run.returncode


if __name__ == "__main__":
    raise SystemExit(main())
