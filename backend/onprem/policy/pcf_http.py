"""One bounded, GET-only PCF batch in the backend network namespace.

The disposable stdlib child preserves the original hard eight-second process
limit. Four lanes reuse TCP within one batch, never evidence or failed requests.
"""
import ipaddress
import json
import logging
import math
import os
import re
import subprocess
import sys
import time


MAX_OUTPUT = 8 * 1024 * 1024
MAX_QUERY_PATHS = 100 * (15 + 1)  # Original historical scope: AM plus every valid PDU ID.
CHILD_SCRIPT = f'MAX_QUERY_PATHS={MAX_QUERY_PATHS}\n' + r'''
import concurrent.futures,http.client,ipaddress,json,math,os,signal,sys,threading,time
watchdog=None
if hasattr(signal,'alarm'): signal.alarm(8)
else:
 watchdog=threading.Timer(8,lambda:os._exit(124)); watchdog.daemon=True; watchdog.start()
data=json.loads(sys.stdin.buffer.read(1024*1024+1))
ip=str(ipaddress.IPv4Address(data['ip'])); paths=data['paths']; deadline=data['deadline']
assert type(paths) is list and 0<len(paths)<=MAX_QUERY_PATHS
assert type(deadline) in (int,float) and math.isfinite(deadline)
rows=[None]*len(paths); totals={k:0.0 for k in ('connect','headers','body','decode','close')}
maxima=dict(totals); lock=threading.Lock(); used=8192; connections=0
first_request=None
def lane(indices):
 global used,connections,first_request
 connection=http.client.HTTPConnection(ip,8080)
 def budget():
  remaining=deadline-time.monotonic()
  if remaining<=0: raise TimeoutError()
  connection.timeout=min(3,remaining)
  if connection.sock is not None: connection.sock.settimeout(connection.timeout)
 def measure(name,start):
  elapsed=time.monotonic()-start
  with lock:
   totals[name]+=elapsed; maxima[name]=max(maxima[name],elapsed)
 try:
  for index in indices:
   budget(); observed=time.time()
   with lock:
    if first_request is None: first_request=time.monotonic()
   if connection.sock is None:
    start=time.monotonic(); connection.connect(); measure('connect',start)
    with lock: connections+=1
   budget(); start=time.monotonic()
   connection.request('GET',paths[index],headers={'Accept':'application/json'})
   response=connection.getresponse(); measure('headers',start)
   start=time.monotonic(); raw=bytearray()
   while True:
    budget(); block=response.read1(min(8192,65537-len(raw))); raw.extend(block)
    if len(raw)>65536: raise ValueError()
    if not block: break
   measure('body',start); budget(); start=time.monotonic()
   row={'path':paths[index],'status':response.status,'body':json.loads(raw or b'null'),'observedAt':observed}
   encoded=json.dumps(row,allow_nan=False).encode(); measure('decode',start); budget()
   with lock:
    used+=len(encoded)+2
    if used>8*1024*1024: raise ValueError()
    rows[index]=row
 finally:
  start=time.monotonic(); connection.close(); measure('close',start)
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
 list(pool.map(lane,[range(i,len(paths),4) for i in range(min(4,len(paths)))]))
finished=time.monotonic()
if finished>=deadline: raise TimeoutError()
result={'rows':rows,'timings':{'sumSeconds':totals,'maxSeconds':maxima,'connectionCount':connections,'firstRequestMonotonic':first_request,'batchFinishedMonotonic':finished}}
encoded=json.dumps(result,allow_nan=False).encode()
if len(encoded)>8*1024*1024: raise ValueError()
sys.stdout.buffer.write(encoded)
sys.stdout.buffer.flush()
if watchdog is not None: watchdog.cancel()
'''


class PcfHttpQueryReader:
    """Trusted adapter port: fixed literal Pod IP, original paths, no retries."""

    def __call__(self, owner, paths, *, deadline):
        began = time.monotonic()
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline <= began or type(paths) is not list or not 0 < len(paths) <= MAX_QUERY_PATHS
                or any(type(path) is not str or not re.fullmatch(
                    r'/npcf-(?:oam/v1/am-policy/imsi-[0-9]{5,15}|smpolicycontrol/v1/sm-policies/imsi-[0-9]{5,15}-[1-9][0-9]{0,2})', path)
                    for path in paths)):
            raise ValueError('Invalid PCF query batch')
        address = ipaddress.IPv4Address(owner['ip'])
        if (not address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_unspecified):
            raise ValueError('Invalid PCF Pod address')
        common_deadline = min(deadline, began + 7)
        payload = json.dumps({'ip': str(address), 'paths': paths, 'deadline': common_deadline}).encode()
        if len(payload) > 1024 * 1024:
            raise ValueError('Invalid PCF query bound')
        child = None
        launch = time.monotonic()
        child_environment = {'PATH': os.defpath}
        if os.name == 'nt' and 'SystemRoot' in os.environ:
            child_environment['SystemRoot'] = os.environ['SystemRoot']
        try:
            child = subprocess.Popen([sys.executable, '-S', '-c', CHILD_SCRIPT],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                close_fds=True, env=child_environment)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('PCF query expired')
            raw, _ = child.communicate(payload, timeout=remaining)
            drained = time.monotonic()
            if child.returncode != 0 or drained >= deadline or len(raw) > MAX_OUTPUT:
                raise ValueError('PCF query unavailable')
            value = json.loads(raw)
            rows, timings = value['rows'], value['timings']
            if (type(rows) is not list or len(rows) != len(paths)
                    or [r.get('path') for r in rows] != paths):
                raise ValueError('Incomplete PCF query batch')
            # Only our child's fixed numeric transport fields reach the logger.
            first = timings['firstRequestMonotonic']
            finished = timings['batchFinishedMonotonic']
            numbers = [first, finished, timings['connectionCount'], *timings['sumSeconds'].values(),
                       *timings['maxSeconds'].values()]
            if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in numbers):
                raise ValueError('Invalid PCF transport timing')
            if not launch <= first <= finished <= drained:
                raise ValueError('Invalid PCF transport ordering')
            try:
                logging.getLogger('uvicorn.error').info(
                    'pcf.native_query queryCount=%d connections=%d launchSeconds=%.6f '
                    'connectSeconds=%.6f headersSeconds=%.6f bodySeconds=%.6f '
                    'decodeSeconds=%.6f closeSeconds=%.6f connectMaxSeconds=%.6f '
                    'headersMaxSeconds=%.6f bodyMaxSeconds=%.6f decodeMaxSeconds=%.6f '
                    'closeMaxSeconds=%.6f queryWallSeconds=%.6f serializationExitSeconds=%.6f elapsedSeconds=%.6f',
                    len(paths), timings['connectionCount'], first-launch,
                    timings['sumSeconds']['connect'], timings['sumSeconds']['headers'],
                    timings['sumSeconds']['body'], timings['sumSeconds']['decode'],
                    timings['sumSeconds']['close'], timings['maxSeconds']['connect'],
                    timings['maxSeconds']['headers'], timings['maxSeconds']['body'],
                    timings['maxSeconds']['decode'], timings['maxSeconds']['close'],
                    finished-first, drained-finished, drained-began)
            except Exception:
                pass  # Numeric diagnostics cannot change the evidence outcome.
            if time.monotonic() >= deadline:
                raise TimeoutError('PCF query expired')
            return rows
        finally:
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.communicate()


def configured_pcf_query_reader(environment):
    """Native WSL profile opts in; explicit legacy keeps the prior exec port."""
    mode = environment.get('CITYVERSE_ONPREM_PCF_QUERY_TRANSPORT')
    if mode is None:
        mode = ('native' if environment.get('CITYVERSE_ONPREM_PROFILE') == 'wsl-kubeadm'
                and environment.get('CITYVERSE_ONPREM_EVIDENCE_MODE') == 'native' else 'legacy')
    if mode not in {'native', 'legacy'}:
        raise ValueError('Invalid PCF query transport')
    return PcfHttpQueryReader() if mode == 'native' else None
