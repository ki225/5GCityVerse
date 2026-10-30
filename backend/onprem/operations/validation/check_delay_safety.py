"""Exercise the privileged Bash boundary in a disposable network namespace."""
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[4]


def main(script=None):
    script = script or ROOT / "infrastructure/onprem/scripts/n3-delay.sh"
    worker = subprocess.Popen(["unshare", "--net", "--", "sleep", "90"])
    checks = []
    try:
        # Wait for unshare to move the worker out of the host namespace.
        import time
        for _ in range(100):
            if Path(f"/proc/{worker.pid}/ns/net").readlink() != Path("/proc/1/ns/net").readlink():
                break
            time.sleep(.01)
        ns = ["nsenter", "-t", str(worker.pid), "-n", "--"]

        def run(*args, check=True):
            return subprocess.run(list(map(str,args)), text=True, capture_output=True, check=check)

        run(*ns, "ip", "link", "add", "n3", "type", "dummy")
        run(*ns, "ip", "link", "set", "n3", "up")
        with tempfile.TemporaryDirectory(prefix="city-delay-safety-") as directory:
            plan = Path(directory) / "endpoint.json"
            plan.write_text(json.dumps({"interface":"n3", "peers":[{"ip":"172.22.31.10", "delayUs":1000}]}))
            assert run("bash", script, "apply", "1", plan, check=False).returncode != 0
            checks.append("host_namespace_refused")
            run(*ns,"tc","qdisc","add","dev","n3","root","handle","beef:","netem","delay","1ms")
            assert run("bash",script,"apply",worker.pid,plan,check=False).returncode != 0
            assert "beef:" in run(*ns,"tc","qdisc","show","dev","n3").stdout
            checks.append("foreign_qdisc_preserved")
            run(*ns,"tc","qdisc","del","dev","n3","root","handle","beef:")
            run("bash",script,"apply",worker.pid,plan)
            filters = run(*ns,"tc","filter","show","dev","n3","parent","c170:").stdout
            assert "flowid c170:1" in filters
            checks.append("peer_filter_installed")
            run(*ns,"ip","addr","add","172.22.31.1/24","dev","n3")
            run(*ns,"python3","-c", "import socket,time; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.setsockopt(socket.IPPROTO_IP,10,0); s.sendto(b'x'*2000,('172.22.31.10',2152)); time.sleep(.05)")
            queues=json.loads(run(*ns,"tc","-s","-j","qdisc","show","dev","n3").stdout)
            forwarded=sum(q.get('packets',0) for q in queues if q['kind']=='netem')
            assert forwarded==2, f"expected both IPv4 fragments in netem, got {forwarded}"
            checks.append("both_ipv4_fragments_delayed")
            run("bash",script,"apply",worker.pid,plan)
            checks.append("owned_reapply_succeeds")
            run("bash",script,"remove",worker.pid,plan)
            restored = run(*ns,"tc","qdisc","show","dev","n3").stdout
            assert "noqueue" in restored, restored
            checks.append("owned_remove_restores_noqueue")
            plan.write_text(json.dumps({"interface":"n3", "peers":[{"ip":"172.22.31.10", "delayUs":-1}]}))
            assert run("bash",script,"apply",worker.pid,plan,check=False).returncode != 0
            checks.append("invalid_delay_refused")
        print(json.dumps({"status":"passed", "checks":checks},indent=2))
    finally:
        worker.terminate()
        worker.wait(timeout=10)


if __name__ == "__main__":
    main()
