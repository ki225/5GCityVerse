#!/usr/bin/env python3
"""Capture read-only Pod CPU and TCP counters around an on-prem batch run."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

SCHEMA = "cityverse.batch-start-counters/v1"
MAX_OUTPUT = 262_144
NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
CONTEXT = re.compile(r"[A-Za-z0-9_.@/-]{1,253}")
UID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


class CaptureBlocked(RuntimeError): pass


def _run(argv, *, runner, timeout):
    result=runner(argv,text=True,capture_output=True,timeout=timeout,check=False)
    if result.returncode or len(result.stdout.encode())>MAX_OUTPUT:
        raise CaptureBlocked("Bounded kubectl read failed")
    return result.stdout


def _identity(context,namespace,pod,*,runner,timeout):
    raw=_run(["kubectl","--context",context,"--namespace",namespace,
              "get","pod",pod,"-o","json"],runner=runner,timeout=timeout)
    try: value=json.loads(raw);meta=value["metadata"]
    except (KeyError,TypeError,ValueError,json.JSONDecodeError):
        raise CaptureBlocked("Pod identity response is invalid") from None
    return {"name":meta.get("name"),"uid":meta.get("uid")}


def _pairs(raw, heading):
    lines=[line.split() for line in raw.splitlines() if line.startswith(heading+":")]
    if len(lines)!=2 or lines[0][0]!=lines[1][0] or len(lines[0])!=len(lines[1]):
        raise CaptureBlocked(f"{heading} counters are malformed")
    try: return {key:int(value) for key,value in zip(lines[0][1:],lines[1][1:])}
    except ValueError: raise CaptureBlocked(f"{heading} counters are non-numeric") from None


def _cpu(raw):
    try: values={parts[0]:int(parts[1]) for line in raw.splitlines()
                if len(parts:=line.split())==2}
    except ValueError: raise CaptureBlocked("cpu.stat is non-numeric") from None
    if not values or any(value<0 for value in values.values()):
        raise CaptureBlocked("cpu.stat is incomplete")
    return values


def _pod(context,namespace,pod,uid,container,*,runner,timeout):
    before=_identity(context,namespace,pod,runner=runner,timeout=timeout)
    if before!={"name":pod,"uid":uid}: raise CaptureBlocked("Pod UID fence failed before capture")
    base=["kubectl","--context",context,"--namespace",namespace,"exec",pod,
          "-c",container,"--","cat"]
    cpu_path="/sys/fs/cgroup/cpu.stat"
    try: cpu=_cpu(_run(base+[cpu_path],runner=runner,timeout=timeout))
    except CaptureBlocked:
        cpu_path="/sys/fs/cgroup/cpu/cpu.stat"
        cpu=_cpu(_run(base+[cpu_path],runner=runner,timeout=timeout))
    netstat=_pairs(_run(base+["/proc/net/netstat"],runner=runner,timeout=timeout),"TcpExt")
    tcp=_pairs(_run(base+["/proc/net/snmp"],runner=runner,timeout=timeout),"Tcp")
    after=_identity(context,namespace,pod,runner=runner,timeout=timeout)
    if after!=before: raise CaptureBlocked("Pod UID fence failed after capture")
    if "RetransSegs" not in tcp: raise CaptureBlocked("TCP RetransSegs is unavailable")
    return {"pod":pod,"podUid":uid,"container":container,"cpuStatPath":cpu_path,"cpuStat":cpu,
            "tcpExt":netstat,"tcp":{"RetransSegs":tcp["RetransSegs"]}}


def capture(*,context,namespace,ue_pod,ue_uid,n6_pod,n6_uid,label,
            runner=subprocess.run,timeout=10,clock=time.time):
    if (not CONTEXT.fullmatch(context or "")
            or not all(NAME.fullmatch(value or "") for value in (namespace,n6_pod))
            or not UID.fullmatch(n6_uid or "")
            or ((ue_pod is None)!=(ue_uid is None))
            or (ue_pod is not None and (not NAME.fullmatch(ue_pod) or not UID.fullmatch(ue_uid)))
            or label not in ("before","after") or not 1<=timeout<=30):
        raise CaptureBlocked("Capture scope is invalid")
    started=float(clock())
    pods={"n6":_pod(context,namespace,n6_pod,n6_uid,"traffic",runner=runner,timeout=timeout)}
    if ue_pod is not None:
        pods["ue"]=_pod(context,namespace,ue_pod,ue_uid,"traffic",runner=runner,timeout=timeout)
    finished=float(clock())
    return {"schema":SCHEMA,"label":label,"context":context,"namespace":namespace,
            "pods":pods,"startedAt":started,"finishedAt":finished,
            "mutationAuthorized":False}


def write_private(value,path):
    target=Path(path)
    if not target.is_absolute() or target.exists() or target.is_symlink():
        raise CaptureBlocked("Output must be a new absolute path")
    target.parent.mkdir(mode=0o700,parents=True,exist_ok=True);info=target.parent.lstat()
    if target.parent.is_symlink() or (os.name=="posix" and (info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o700)):
        raise CaptureBlocked("Output directory must be root-owned mode 0700")
    payload=json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    fd,name=tempfile.mkstemp(prefix=target.name+".",dir=target.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,"wb") as stream: stream.write(payload);stream.flush();os.fsync(stream.fileno())
        os.replace(name,target)
        if os.name=="posix":
            directory=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)
    return {"privatePath":str(target),"sha256":hashlib.sha256(payload).hexdigest(),"schema":SCHEMA}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("context","namespace","n6-pod","n6-uid","label","output"):
        p.add_argument("--"+name,required=True)
    p.add_argument("--ue-pod");p.add_argument("--ue-uid")
    p.add_argument("--timeout",type=int,default=10);a=p.parse_args(argv)
    if os.name!="posix" or os.geteuid()!=0: p.error("Run inside WSL as root")
    value=capture(context=a.context,namespace=a.namespace,ue_pod=a.ue_pod,ue_uid=a.ue_uid,
        n6_pod=a.n6_pod,n6_uid=a.n6_uid,label=a.label,timeout=a.timeout)
    print(json.dumps(write_private(value,a.output),sort_keys=True));return 0


if __name__=="__main__": raise SystemExit(main())
