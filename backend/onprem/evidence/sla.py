"""Read-only adapter for exact-owner native receiver interval evidence.

No sparse average is expanded into time samples. Intervals are extracted only
from the same iperf report pair already accepted by the native reducer.
"""
from datetime import datetime
from bisect import bisect_left
import math


def seconds(value):
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) else None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if isinstance(value, str) else None
    except (ValueError, OverflowError, OSError):
        return None


def number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def native_intervals(client, server, direction, *, started_at, finished_at):
    """Bound actual receiver and sender bins to an independently verified window.

    Clock-incoherent headers produce no interval evidence. The existing native
    window remains usable as a sparse measurement, without a time-SLA claim.
    """
    receiver, sender = (server, client) if direction == "ul" else (client, server)
    raw_receiver = receiver.get("intervals", [])
    raw_sender = sender.get("intervals", [])
    timestamp = client.get("start", {}).get("timestamp", {}).get("timemillisecs")
    if type(timestamp) is not int or not isinstance(raw_receiver, list) or not isinstance(raw_sender, list):
        return []
    origin = timestamp / 1000
    if not started_at - 1 <= origin <= finished_at:
        return []
    output = []
    for interval in raw_receiver[:3600]:
        total = interval.get("sum", {}) if isinstance(interval, dict) else {}
        if not isinstance(total, dict):
            continue
        start, end = number(total.get("start")), number(total.get("end"))
        rate = number(total.get("bits_per_second"))
        if start is None or end is None or end <= start or rate is None or total.get("sender") is True:
            continue
        observed_start, observed_end = origin + start, origin + end
        if observed_start < started_at - .002 or observed_end > finished_at + .002:
            continue
        sent = [r.get("sum", {}) for r in raw_sender if isinstance(r, dict)
                and isinstance(r.get("sum"), dict)
                and number(r["sum"].get("start")) is not None and number(r["sum"].get("end")) is not None
                and abs(r["sum"]["start"] - start) <= .002 and abs(r["sum"]["end"] - end) <= .002]
        offered = number(sent[0].get("bits_per_second")) / 1e6 if len(sent) == 1 and number(sent[0].get("bits_per_second")) is not None else None
        packets, lost = total.get("packets"), total.get("lost_packets")
        if type(packets) is not int or type(lost) is not int or not 0 <= lost <= packets:
            packets = lost = None
        output.append({"startedAt": observed_start, "finishedAt": observed_end,
                       "receivedMbps": rate / 1e6, "offeredObservedMbps": offered,
                       "packets": packets, "lostPackets": lost,
                       "source": "native-iperf3-receiver-interval"})
    return output


class NativeSlaObservationAdapter:
    """Retain the frozen cohort even when reports/UEs disappear."""
    IDENTITY = ("supi", "scenario", "slice", "sessionId", "podUid", "upf", "upfEpoch")

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.owners = {r.get("supi"): r for r in snapshot.get("perUe", []) if isinstance(r, dict)}
        self.flows = {}
        self.spans = []
        actions = snapshot.get("actions", [])
        index = {a.get("id"): a for a in actions if isinstance(a, dict)}
        seen = set()
        self.windows = []
        self.latest_window_unavailable = False
        for action in actions:
            if (not isinstance(action, dict) or action.get("accepted") is not True
                    or action.get("stage") not in {"measuring_baseline", "measuring_result"}
                    or action.get("roundId", snapshot.get("roundId")) != snapshot.get("roundId")
                    or action.get("generation", snapshot.get("generation")) != snapshot.get("generation")):
                continue
            if action.get("kind") == "traffic_aggregate":
                self.latest_window_unavailable = True
                ids = action.get("shardActionIds", [])
                windows = [index.get(i, {}) for i in ids]
                if (not ids or len(ids) != len(set(ids)) or any(w.get("accepted") is not True
                        or w.get("kind") != "traffic_window" or w.get("stage") != action.get("stage")
                        or w.get("roundId", snapshot.get("roundId")) != snapshot.get("roundId")
                        or w.get("generation", snapshot.get("generation")) != snapshot.get("generation") for w in windows)):
                    continue
            elif action.get("kind") == "traffic_window" and action.get("shardCount", 1) == 1:
                self.latest_window_unavailable = True
                windows = [action]
            else:
                continue
            reports = [r for w in windows for r in w.get("reports", []) if isinstance(r, dict)]
            starts = [seconds(r.get("effectiveStartedAt", r.get("startedAt"))) for r in reports]
            ends = [seconds(r.get("effectiveFinishedAt", r.get("finishedAt"))) for r in reports]
            start = seconds(action.get("effectiveStartedAt"))
            end = seconds(action.get("effectiveFinishedAt"))
            start = start if start is not None else min((v for v in starts if v is not None), default=None)
            end = end if end is not None else max((v for v in ends if v is not None), default=None)
            if start is None or end is None or end <= start:
                continue
            self.latest_window_unavailable = False
            self.windows.append({"id": action["id"], "stage": action["stage"], "startedAt": start, "finishedAt": end})
            self.spans.append((start, end))
            for window in windows:
                if window.get("id") in seen:
                    continue
                seen.add(window.get("id"))
                grouped = {}
                for report in window.get("reports", []):
                    if not isinstance(report, dict):
                        continue
                    binding = report.get("binding", {})
                    owner = self.owners.get(binding.get("supi")) if isinstance(binding, dict) else None
                    if (not owner or any(not owner.get(k) or binding.get(k) != owner.get(k) for k in self.IDENTITY)
                            or binding.get("roundId", snapshot.get("roundId")) != snapshot.get("roundId")
                            or binding.get("generation", snapshot.get("generation")) != snapshot.get("generation")
                            or report.get("source") not in {"receiver", "native-iperf3-receiver"} or report.get("direction") not in {"ul", "dl"}):
                        continue
                    grouped.setdefault((binding["supi"], report["direction"]), []).append(report)
                for key, flow_reports in grouped.items():
                    if len(flow_reports) != 1:
                        continue  # Duplicate evidence cannot increase coverage.
                    report = flow_reports[0]
                    for interval in report.get("slaIntervals", []):
                        if not isinstance(interval, dict) or interval.get("source") != "native-iperf3-receiver-interval":
                            continue
                        a, b = seconds(interval.get("startedAt")), seconds(interval.get("finishedAt"))
                        if a is None or b is None or not start - .002 <= a < b <= end + .002:
                            continue
                        self.flows.setdefault(key, []).append(dict(interval))
        for key, rows in self.flows.items():
            rows.sort(key=lambda r: seconds(r["startedAt"]))
        self._starts = {key: [seconds(r["startedAt"]) for r in rows] for key, rows in self.flows.items()}

    def cohort(self, scenario=None, slice_name=None):
        return sorted(s for s, r in self.owners.items()
                      if (scenario is None or r.get("scenario") == scenario)
                      and (slice_name is None or r.get("slice") == slice_name))

    def flow(self, supi, direction, start, end, maximum_interval):
        rows = []
        key = (supi, direction)
        source = self.flows.get(key, [])
        offset = max(0, bisect_left(self._starts.get(key, []), start) - 1)
        for i in range(offset, len(source)):
            row = source[i]
            a, b = seconds(row["startedAt"]), seconds(row["finishedAt"])
            if a >= end:
                break
            if b > start and a < end:
                if b - a > maximum_interval + .002:
                    return None  # Never subdivide a coarse aggregate into finer samples.
                rows.append((max(a, start), min(b, end), row))
        rows.sort(key=lambda r: r[0])
        cursor = start
        if not rows:
            return None
        received = offered = packets = lost = 0.
        packet_valid = offered_valid = True
        for a, b, row in rows:
            if abs(a - cursor) > .002 or b <= a:
                return None  # Gap or overlapping/replayed evidence.
            rate = number(row.get("receivedMbps"))
            if rate is None:
                return None
            duration = b - a
            received += rate * duration
            source_offered = number(row.get("offeredObservedMbps"))
            offered_valid &= source_offered is not None
            offered += (source_offered or 0) * duration
            p, l = row.get("packets"), row.get("lostPackets")
            if type(p) is not int or type(l) is not int or not 0 <= l <= p:
                packet_valid = False
            else:
                # Packet-ratio evidence is exact only for complete source intervals.
                packet_valid &= abs(a - seconds(row["startedAt"])) <= .002 and abs(b - seconds(row["finishedAt"])) <= .002
                packets += p; lost += l
            cursor = b
        if abs(cursor - end) > .002:
            return None
        duration = end - start
        return {"receivedMbps": received / duration,
                "offeredObservedMbps": offered / duration if offered_valid else None,
                "packets": packets if packet_valid else None, "lostPackets": lost if packet_valid else None}

    def indices(self, supis, start, end, step):
        """Visit observed bins only; missing hours remain unknown without fake samples."""
        result = set()
        for supi in supis:
            for direction in ("ul", "dl"):
                for row in self.flows.get((supi, direction), []):
                    a, b = seconds(row["startedAt"]), seconds(row["finishedAt"])
                    if b <= start or a >= end or b - a > step + .002:
                        continue
                    first = max(0, math.floor((max(a, start) - start) / step))
                    last = max(first, math.ceil((min(b, end) - start) / step) - 1)
                    result.update(range(first, last + 1))
        return sorted(result)
