"""Private, sequential read-only probe transport; batches are not snapshots."""
import json
import time

from backend.onprem.runtime.ue import EvidenceUnavailable


# The pinned UERANSIM image already provides Python. No remote YAML dependency.
# Temporary files cap what is retained/read in memory, and each child is waited
# for (including on timeout) before the outer exec exits.
BATCH_SCRIPT = r'''
import json,os,re,subprocess,sys,tempfile,time
if os.name=='posix':
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE,(65537,65537))
request=json.loads(sys.argv[1])
# Include transport/startup delay, rather than starting a fresh full budget
# after kubectl eventually reaches the container. Never extend the local cap.
deadline=time.monotonic()+min(request['timeout'],request['wall_deadline']-time.time())
out={}
for name,command in request['probes']:
    args=[]
    for arg in command:
        if arg=='@probe-node':
            arg=out['node'].strip()
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',arg):
                raise ValueError('invalid node')
        args.append(arg)
    remaining=deadline-time.monotonic()
    if remaining<=0: raise TimeoutError('batch deadline')
    with tempfile.TemporaryFile() as output:
        result=subprocess.run(args,stdout=output,stderr=subprocess.DEVNULL,
            timeout=remaining,check=False)
        if result.returncode: raise ValueError('probe failed')
        output.seek(0); raw=output.read(65537)
        if len(raw)>65536: raise ValueError('probe output exceeds limit')
        out[name]=raw.decode('utf-8',errors='strict')
if time.monotonic()>deadline: raise TimeoutError('batch deadline')
frame=json.dumps({'schema':'cityverse.probe-batch/v1','outputs':out},separators=(',',':'))
if len(frame.encode())>1048576: raise ValueError('frame exceeds limit')
print(frame)
'''


def read_probe_batch(execute, probes, *, timeout=6, outer_timeout=8):
    """Run trusted argv probes; reject partial, duplicate or oversized frames."""
    names=[name for name,_ in probes]
    if (not names or len(names)>32 or len(set(names))!=len(names)
            or any(not isinstance(name,str) or not name for name in names)
            or not 0<timeout<=10 or outer_timeout-timeout<2):
        raise EvidenceUnavailable('Invalid native probe batch')
    request=json.dumps({'probes':probes,'timeout':timeout,'wall_deadline':time.time()+timeout},separators=(',',':'))
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise ValueError('duplicate frame key')
            result[key]=value
        return result
    try:
        raw=execute('python','-c',BATCH_SCRIPT,request)
        if len(raw.encode())>1024*1024: raise ValueError('frame exceeds limit')
        frame=json.loads(raw,object_pairs_hook=unique)
        outputs=frame['outputs']
        if (set(frame)!={'schema','outputs'} or frame['schema']!='cityverse.probe-batch/v1'
                or not isinstance(outputs,dict) or set(outputs)!=set(names)
                or any(not isinstance(value,str) or len(value.encode())>65536 for value in outputs.values())):
            raise ValueError('invalid frame')
        return outputs
    except EvidenceUnavailable:
        raise
    except Exception:
        raise EvidenceUnavailable('Native probe batch failed or incomplete') from None
