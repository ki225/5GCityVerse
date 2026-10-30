"""Bounded actual UDP iperf3 UL/reverse-DL sampling in the owned UE netns.

Requires optional pinned `traffic` sidecars in UE and N6 Pods. No synthetic
rates, persistent background jobs, Pod creation, interface changes or retries.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import ipaddress
import json
import math
import os
import re
import subprocess
import time

from backend.onprem.policy.errors import PolicyBlocked


# Inner IPv4/UDP is 28 bytes; 1200 + 28 stays below the current upfgtp MTU 1464.
UDP_PAYLOAD_BYTES = 1200
COMMAND_DIAGNOSTIC_SCHEMA = 'cityverse.traffic-command-failure/v1'
COMMAND_OUTPUT_LIMIT = 512
COMMAND_STAGES = frozenset({'scoped_command', 'pod_read', 'ue_address_read',
    'ue_iperf_version', 'receiver_iperf_version', 'receiver_server', 'ue_client'})


def _command_stage(args):
    """Classify fixed operations without retaining a command or its arguments."""
    if args[:2] == ['get', 'pod']:
        return 'pod_read'
    if args[:1] != ['exec'] or '--' not in args:
        return 'scoped_command'
    command = args[args.index('--') + 1:]
    if command[:3] == ['ip', '-j', 'addr']:
        return 'ue_address_read'
    if command[:2] == ['iperf3', '--version']:
        return 'ue_iperf_version' if str(args[1]).startswith('cv-ue-') else 'receiver_iperf_version'
    if command[:1] == ['timeout'] and command[2:3] == ['iperf3']:
        if '-s' in command:
            return 'receiver_server'
        if '-c' in command:
            return 'ue_client'
    return 'scoped_command'


def _redacted_output(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    if not isinstance(value, str):
        value = ''
    # Bound sanitization work as well as the final public excerpt. Redaction
    # precedes output truncation so a long credential cannot leak its prefix.
    input_truncated = len(value) > 65536
    text = value[:65536]
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)
    text = re.sub(r'-----BEGIN [^-]*(?:KEY|CERTIFICATE)-----.*?(?:-----END [^-]+-----|$)',
                  '[REDACTED]', text, flags=re.S)
    text = re.sub(r'(?i)(https?://)[^/@\s]+@', r'\1[REDACTED]@', text)
    text = re.sub(r'(?i)\bbearer\s+[^\s,\"\'}]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'(?i)(\bauthorization\b[\"\']?\s*[:=]\s*)[^\r\n,}]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(\b(?:authorization|api[-_ ]?key|token|password|secret|credential|client-key-data|cookie)'
                  r'\b[\"\']?\s*[:=]\s*)(?:\"[^\"\r\n]*\"?|\'[^\'\r\n]*\'?|[^\s,}\]]+)',
                  r'\1[REDACTED]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}\b', '[REDACTED]', text)
    text = re.sub(r'[A-Za-z0-9_+/=-]{24,}', '[REDACTED]', text)
    return text[:COMMAND_OUTPUT_LIMIT], input_truncated or len(text) > COMMAND_OUTPUT_LIMIT


def _command_diagnostic(args, *, returncode=None, timeout=None, timed_out=False, stdout='', stderr=''):
    out, out_cut = _redacted_output(stdout)
    err, err_cut = _redacted_output(stderr)
    return {'schemaVersion': COMMAND_DIAGNOSTIC_SCHEMA, 'commandStage': _command_stage(args),
        'returnCode': returncode if type(returncode) is int and -255 <= returncode <= 255 else None,
        'timedOut': timed_out is True,
        'timeoutSeconds': timeout if type(timeout) in (int, float) and math.isfinite(timeout) and 0 <= timeout <= 300 else None,
        'stdout': out, 'stderr': err, 'stdoutTruncated': out_cut, 'stderrTruncated': err_cut}


def native_command_diagnostic(error):
    """Accept only our exact local exception types and bounded scalar schema."""
    if type(error) not in (PolicyBlocked, subprocess.TimeoutExpired):
        return None
    data = getattr(error, 'traffic_command_diagnostic', None)
    fields = {'schemaVersion', 'commandStage', 'returnCode', 'timedOut', 'timeoutSeconds',
              'stdout', 'stderr', 'stdoutTruncated', 'stderrTruncated'}
    if (not isinstance(data, dict) or set(data) != fields
            or data['schemaVersion'] != COMMAND_DIAGNOSTIC_SCHEMA
            or not isinstance(data['commandStage'], str) or data['commandStage'] not in COMMAND_STAGES
            or any(type(data[k]) is not bool for k in ('timedOut', 'stdoutTruncated', 'stderrTruncated'))
            or any(not isinstance(data[k], str) or len(data[k]) > COMMAND_OUTPUT_LIMIT for k in ('stdout', 'stderr'))
            or data['returnCode'] is not None and (type(data['returnCode']) is not int or not -255 <= data['returnCode'] <= 255)
            or data['timeoutSeconds'] is not None and (type(data['timeoutSeconds']) not in (int, float)
                or not math.isfinite(data['timeoutSeconds']) or not 0 <= data['timeoutSeconds'] <= 300)):
        return None
    return dict(data)


class ReceiverWindowBlocked(PolicyBlocked):
    """Fixed failure with bounded numeric readout; never retain raw iperf JSON."""
    def __init__(self, diagnostic):
        super().__init__("Empty or inconsistent receiver window")
        self.diagnostic = diagnostic


def receiver_window_diagnostic(result):
    def bounded(value, maximum):
        return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum else None
    return {
        "secondsNonpositive": result["seconds"] <= 0,
        "packetsNonpositive": result["packets"] <= 0,
        "lossExceedsPackets": result["lost_packets"] > result["packets"],
        "noDeliveredPackets": result["lost_packets"] >= result["packets"],
        "bytesNonpositive": result["bytes"] <= 0,
        "rateNonpositive": result["bits_per_second"] <= 0,
        "seconds": bounded(result["seconds"], 120),
        "packets": bounded(result["packets"], 10_000_000),
        "lostPackets": bounded(result["lost_packets"], 10_000_000),
        "receivedBytes": bounded(result["bytes"], 1_000_000_000),
        "bitsPerSecond": bounded(result["bits_per_second"], 1_000_000_000),
    }


def sender_window_diagnostic(result):
    """Only bounded numeric iperf server sender counters; no peer or raw output."""
    def bounded(value, maximum):
        return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum else None
    return {"seconds": bounded(result["seconds"], 120),
            "packets": bounded(result["packets"], 10_000_000),
            "sentBytes": bounded(result["bytes"], 1_000_000_000),
            "bitsPerSecond": bounded(result["bits_per_second"], 1_000_000_000)}


def native_qos_enabled(value):
    """Parse the actual pinned gtp5g procfs ABI, not a guessed bare integer."""
    match = re.fullmatch(r"QoS Enable: ([01])", value.strip())
    if match is None:
        raise PolicyBlocked("Unrecognized native gtp5g QoS readback")
    return match[1] == "1"


def receiver_summary(report):
    if report.get("error"):
        raise PolicyBlocked("iperf receiver reported an error")
    end = report.get("end", {})
    result = end.get("sum_received") or end.get("sum")
    if not isinstance(result, dict) or result.get("sender") is not False:
        raise PolicyBlocked("Missing actual receiver summary")
    for key in ("bits_per_second", "seconds", "bytes", "packets", "lost_packets"):
        value = result.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise PolicyBlocked("Invalid receiver measurements")
    if (result["seconds"] <= 0 or result["packets"] <= 0 or result["lost_packets"] >= result["packets"]
            or result["bytes"] <= 0 or result["bits_per_second"] <= 0):
        raise ReceiverWindowBlocked(receiver_window_diagnostic(result))
    return result


def sender_summary(report):
    result = report.get("end", {}).get("sum_sent")
    if report.get("error") or not isinstance(result, dict) or result.get("sender") is not True:
        raise PolicyBlocked("Missing actual sender summary")
    for key in ("bits_per_second", "seconds", "bytes", "packets"):
        value = result.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise PolicyBlocked("Invalid actual offered-load measurements")
    return result


class IperfCapabilityTraffic:
    def __init__(self, kubectl, kubeconfig, context, namespace="free5gc-onprem", runner=subprocess.run):
        if not context or any(v in context.lower() for v in ("eks", "arn:", "kind", "minikube")):
            raise PolicyBlocked("Explicit dedicated kubeadm context required")
        self.prefix = [str(kubectl), "--kubeconfig", str(kubeconfig), "--context", context, "-n", namespace]
        self.runner = runner
        transport = os.environ.get('CITYVERSE_ONPREM_K8S_POD_READ_TRANSPORT', 'kubectl')
        if transport not in {'kubectl', 'api-https'}:
            raise PolicyBlocked('Unsupported trusted Kubernetes Pod read transport')
        self._metadata_pod_reader = None
        if transport == 'api-https':
            from backend.onprem.evidence.kubernetes_pod_read import KubernetesPodRead
            self._metadata_pod_reader = KubernetesPodRead(context, namespace, kubeconfig=kubeconfig, capacity=4)

    def close(self):
        reader = getattr(self, '_metadata_pod_reader', None)
        if reader is not None:
            reader.close()

    def _run(self, args, timeout=10):
        try:
            result = self.runner(self.prefix + args, text=True, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            error.traffic_command_diagnostic = _command_diagnostic(args, timeout=timeout,
                timed_out=True, stdout=error.output, stderr=error.stderr)
            raise
        if result.returncode:
            error = PolicyBlocked("Scoped traffic command failed; no retry")
            error.traffic_command_diagnostic = _command_diagnostic(args, returncode=result.returncode,
                timeout=timeout, stdout=result.stdout, stderr=result.stderr)
            raise error
        return result.stdout

    def pod(self, name, uid, *, required_container="traffic"):
        reader = getattr(self, '_metadata_pod_reader', None)
        if reader is None:
            pod = json.loads(self._run(["get", "pod", name, "-o", "json"]))
        else:
            from backend.onprem.runtime.ue import EvidenceUnavailable
            try:
                pod = reader.pod(name, deadline=time.monotonic() + 5)
            except EvidenceUnavailable:
                error = PolicyBlocked('Scoped traffic command failed; no retry')
                error.traffic_command_diagnostic = _command_diagnostic(['get', 'pod'], timeout=5,
                    stderr='Native Pod metadata read unavailable')
                raise error from None
        if pod["metadata"].get("uid") != uid or pod["metadata"].get("deletionTimestamp"):
            raise PolicyBlocked("Traffic Pod UID changed or is terminating")
        if required_container is not None and not any(c["name"] == required_container for c in pod["spec"]["containers"]):
            raise PolicyBlocked("Pinned traffic sidecar is not available")
        return pod

    def sample(self, binding, ue_pod, receiver_pod, receiver_uid, endpoint, port,
               *, direction, offered_mbps=4, duration=10, guard, service_route=None):
        if service_route is not None:
            return self._sample_service(binding, ue_pod, receiver_pod, receiver_uid, endpoint, port,
                direction=direction, offered_mbps=offered_mbps, duration=duration, guard=guard, route=service_route)
        if direction not in {"ul", "dl"} or type(duration) is not int or not 5 <= duration <= 30:
            raise PolicyBlocked("Invalid fixed measurement window")
        if (type(offered_mbps) not in (int,float) or not .001 <= offered_mbps <= 20
                or not math.isfinite(offered_mbps)
                or type(port) is not int or not 1024 <= port <= 65535):
            raise PolicyBlocked("Invalid bounded traffic request")
        # Shared workload contracts use integer kbps; avoid float Mbps on the CLI.
        bitrate=Decimal(str(offered_mbps))*1_000_000
        if bitrate % 1000:
            raise PolicyBlocked("Traffic rate must be representable as integer kbps")
        ipaddress.IPv4Address(endpoint)
        ipaddress.IPv4Address(binding["ip"])
        if not re.fullmatch(r"(?:uesimtun|ueTun)[0-9]+", binding["tun"]) or not re.fullmatch(r"cv-ue-g[0-9]+-[0-9]+", ue_pod) or not re.fullmatch(r"n6-[a-z0-9-]+", receiver_pod):
            raise PolicyBlocked("Invalid traffic resource identity")
        before = self.pod(ue_pod, binding["podUid"])
        self.pod(receiver_pod, receiver_uid)
        labels = before["metadata"].get("labels", {})
        supis = json.loads(before["metadata"].get("annotations", {}).get("cityverse.io/supis", "[]"))
        if (labels.get("cityverse.io/managed-by") != "onprem-ue-controller" or
                labels.get("cityverse.io/generation") != str(binding["generation"]) or binding["supi"] not in supis):
            raise PolicyBlocked("UE traffic ownership mismatch")
        addresses = json.loads(self._run(["exec", ue_pod, "-c", "ue", "--", "ip", "-j", "addr", "show"]))
        matches = [a["ifname"] for a in addresses if any(v.get("local") == binding["ip"] for v in a.get("addr_info", []))]
        if matches != [binding["tun"]] or guard(binding) is not True:
            raise PolicyBlocked("Fresh native PDU/TUN binding required")
        # Verify the executable on both targets before starting any listener.
        for name in (ue_pod, receiver_pod):
            self._run(["exec", name, "-c", "traffic", "--", "iperf3", "--version"])
        server = ["exec", receiver_pod, "-c", "traffic", "--", "timeout", str(duration + 15),
                  "iperf3", "-s", "-1", "-B", endpoint, "-p", str(port), "-J"]
        client = ["exec", ue_pod, "-c", "traffic", "--", "timeout", str(duration + 10), "iperf3",
                  "-c", endpoint, "-p", str(port), "-u", "-b", str(int(bitrate)), "-t", str(duration),
                  "-l", str(UDP_PAYLOAD_BYTES),
                  "-B", binding["ip"], "--bind-dev", binding["tun"], "-J", "--get-server-output"]
        if direction == "dl":
            client.append("-R")
        started = time.time()
        with ThreadPoolExecutor(max_workers=1) as executor:
            server_future = executor.submit(self._run, server, duration + 18)
            # Listener startup delay is not success evidence; any race fails,
            # and timeout owns termination of this one-shot server process.
            time.sleep(0.5)
            client_raw = json.loads(self._run(client, duration + 13))
            server_raw = json.loads(server_future.result(timeout=duration + 18))
        self.pod(ue_pod, binding["podUid"])
        self.pod(receiver_pod, receiver_uid)
        if guard(binding) is not True or client_raw.get("error") or server_raw.get("error"):
            raise PolicyBlocked("Traffic sample lost native ownership or failed")
        connected = client_raw.get("start", {}).get("connected", [])
        if len(connected) != 1 or connected[0].get("local_host") != binding["ip"] or connected[0].get("remote_host") != endpoint:
            raise PolicyBlocked("iperf did not use the exact UE/N6 binding")
        test = client_raw.get("start", {}).get("test_start", {})
        if (test.get("protocol") != "UDP" or test.get("reverse") != int(direction == "dl")
                or test.get("blksize") != UDP_PAYLOAD_BYTES):
            raise PolicyBlocked("Unexpected traffic direction/protocol")
        try:
            receiver = receiver_summary(server_raw if direction == "ul" else client_raw)
        except ReceiverWindowBlocked as exc:
            if direction == "dl":
                try:
                    exc.diagnostic["serverSender"] = sender_window_diagnostic(sender_summary(server_raw))
                except PolicyBlocked:
                    pass
            raise
        sender = sender_summary(client_raw if direction == "ul" else server_raw)
        if not duration - 2 <= receiver["seconds"] <= duration + 3:
            raise PolicyBlocked("Incomplete receiver measurement window")
        return {"binding": binding, "receiverPodUid": receiver_uid, "endpoint": endpoint, "port": port,
                "direction": direction, "startedAt": started, "finishedAt": time.time(), "durationSeconds": duration,
                "offeredTargetMbps": offered_mbps, "receivedMbps": receiver["bits_per_second"] / 1_000_000,
                "offeredObservedMbps": sender["bits_per_second"] / 1_000_000,
                "receivedBytes": receiver["bytes"], "packets": receiver["packets"], "lostPackets": receiver["lost_packets"],
                "client": client_raw, "server": server_raw, "source": "native-iperf3-receiver"}

    def _sample_service(self, binding, ue_pod, receiver_pod, receiver_uid, endpoint, port,
                        *, direction, offered_mbps, duration, guard, route):
        """Measure an existing commissioned persistent application iperf server."""
        if (direction not in {'ul','dl'} or type(duration) is not int or not 5<=duration<=30
                or type(offered_mbps) not in (int,float) or not math.isfinite(offered_mbps) or not .001<=offered_mbps<=20
                or type(port) is not int or not 1024<=port<=65535
                or endpoint!=route['address'] or port!=route['port']
                or receiver_uid!=route['application_pod_uid'] or receiver_pod!=route['receiver_pod']
                or route['generation']!=binding['generation'] or route['upf_epoch']!=binding['upfEpoch']
                or route['upf_target_id']!=binding['upf'] or str(route['pfcp_seid'])!=str(binding['pfcpSeid'])):
            raise PolicyBlocked('Invalid commissioned persistent application flow')
        for address in (endpoint, route['receiver_address'], route['receiver_peer'], binding['ip']):
            ipaddress.IPv4Address(address)
        if (re.fullmatch(r'cv-ue-g[0-9]+-[0-9]+',ue_pod) is None
                or re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}',receiver_pod) is None
                or re.fullmatch(r'(?:uesimtun|ueTun)[0-9]+',binding['tun']) is None):
            raise PolicyBlocked('Invalid native service flow Pod/TUN identity')
        before=self.pod(ue_pod,binding['podUid'])
        self.pod(receiver_pod,receiver_uid,required_container=None)
        labels=before['metadata'].get('labels',{})
        supis=json.loads(before['metadata'].get('annotations',{}).get('cityverse.io/supis','[]'))
        if (labels.get('cityverse.io/managed-by')!='onprem-ue-controller'
                or labels.get('cityverse.io/generation')!=str(binding['generation']) or binding['supi'] not in supis):
            raise PolicyBlocked('UE service flow ownership mismatch')
        addresses=json.loads(self._run(['exec',ue_pod,'-c','ue','--','ip','-j','addr','show']))
        matches=[a['ifname'] for a in addresses if any(v.get('local')==binding['ip'] for v in a.get('addr_info',[]))]
        if matches!=[binding['tun']] or guard(binding) is not True:
            raise PolicyBlocked('Fresh native service PDU/TUN route required')
        bitrate=Decimal(str(offered_mbps))*1_000_000
        if bitrate%1000:raise PolicyBlocked('Service offered load must use integer kbps')
        client=['exec',ue_pod,'-c','traffic','--','timeout',str(duration+10),'iperf3',
            '-c',endpoint,'-p',str(port),'-u','-b',str(int(bitrate)),'-t',str(duration),
            '-l',str(UDP_PAYLOAD_BYTES),'-B',binding['ip'],'--bind-dev',binding['tun'],'-J','--get-server-output']
        if direction=='dl':client.append('-R')
        started=time.time()
        client_raw=json.loads(self._run(client,duration+13))
        server_raw=client_raw.get('server_output_json')
        self.pod(ue_pod,binding['podUid']);self.pod(receiver_pod,receiver_uid,required_container=None)
        if (guard(binding) is not True or client_raw.get('error') or not isinstance(server_raw,dict) or server_raw.get('error')):
            raise PolicyBlocked('Persistent application receiver evidence unavailable or ownership changed')
        connected=client_raw.get('start',{}).get('connected',[])
        peer=server_raw.get('start',{}).get('connected',[])
        cookie=client_raw.get('start',{}).get('cookie')
        if (len(connected)!=1 or connected[0].get('local_host')!=binding['ip'] or connected[0].get('remote_host')!=endpoint
                or connected[0].get('remote_port')!=port or len(peer)!=1
                or peer[0].get('local_host')!=route['receiver_address'] or peer[0].get('local_port')!=port
                or peer[0].get('remote_host')!=route['receiver_peer'] or not isinstance(cookie,str) or not cookie
                or server_raw.get('start',{}).get('cookie')!=cookie):
            raise PolicyBlocked('Native application receiver socket/cookie route mismatch')
        test=client_raw.get('start',{}).get('test_start',{})
        if test.get('protocol')!='UDP' or test.get('reverse')!=int(direction=='dl') or test.get('blksize')!=UDP_PAYLOAD_BYTES:
            raise PolicyBlocked('Unexpected native service flow protocol or direction')
        receiver=receiver_summary(server_raw if direction=='ul' else client_raw)
        sender=sender_summary(client_raw if direction=='ul' else server_raw)
        if not duration-2<=receiver['seconds']<=duration+3:
            raise PolicyBlocked('Incomplete native application receiver window')
        return {'binding':binding,'receiverPodUid':receiver_uid,'endpoint':endpoint,'port':port,'direction':direction,
            'startedAt':started,'finishedAt':time.time(),'durationSeconds':duration,'offeredTargetMbps':offered_mbps,
            'receivedMbps':receiver['bits_per_second']/1_000_000,'offeredObservedMbps':sender['bits_per_second']/1_000_000,
            'receivedBytes':receiver['bytes'],'packets':receiver['packets'],'lostPackets':receiver['lost_packets'],
            'client':client_raw,'server':server_raw,'source':'native-iperf3-receiver','serviceRoute':route,
            'flow_id':cookie+':'+direction}
