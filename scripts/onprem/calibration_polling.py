"""Offline polling decisions, not an evidence validator or HTTP client.

The calibration runner uses these decisions to schedule full captures from
summary polls. Existing raw/native/UI/capacity/release gates remain mandatory.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import math


class PollingContractError(ValueError):
    pass


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise PollingContractError('Invalid observation clock')
    return value


def _identity(value):
    if not isinstance(value, dict) or not isinstance(value.get('roundId'), str) or not value['roundId']:
        raise PollingContractError('Owned round identity missing')
    for key in ('sequence', 'policyRevision'):
        if type(value.get(key)) is not int or value[key] < 0:
            raise PollingContractError('Invalid sequence/revision')
    if value.get('generation') is not None and (type(value['generation']) is not int or value['generation'] < 0):
        raise PollingContractError('Invalid generation')
    if not isinstance(value.get('stage'), str) or not value['stage']:
        raise PollingContractError('Stage missing')


def _fresh(stamp, received_at):
    try:
        parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return False
        return -1 <= received_at - parsed.timestamp() <= 15
    except (ValueError, TypeError, AttributeError):
        return False


@dataclass(frozen=True)
class PollDecision:
    persist_summary: bool
    full_boundary: str | None


class SummaryPollTracker:
    """One owner, bounded captures; use monotonic seconds for scheduling.

    Calling plan reserves an attempt even if the later full GET fails. It never
    authorizes mutations or declares successful evidence. The caller remains
    serialized and must preserve failed fetch/transition outcomes.
    """
    def __init__(self, round_id, heartbeat_seconds=5):
        if not isinstance(round_id, str) or not round_id:
            raise PollingContractError('Explicit owned round required')
        if not .25 <= _clock(heartbeat_seconds) <= 30:
            raise PollingContractError('Heartbeat must be bounded')
        self.round_id, self.heartbeat = round_id, heartbeat_seconds
        self.sequence, self.stage, self.last_saved, self.last_poll = -1, None, None, None
        self.failure_attempted = self.cleanup_attempted = False
        self.result_sequences = set()
        self.last_full = None

    def plan(self, summary, *, monotonic_now):
        _identity(summary); now = _clock(monotonic_now)
        if summary['roundId'] != self.round_id or summary['sequence'] < self.sequence:
            raise PollingContractError('Round changed or sequence regressed')
        if self.last_poll is not None and now < self.last_poll:
            raise PollingContractError('Polling clock regressed')
        persist = (summary['sequence'] != self.sequence or summary['stage'] != self.stage
                   or self.last_saved is None or now - self.last_saved >= self.heartbeat)
        self.sequence, self.stage, self.last_poll = summary['sequence'], summary['stage'], now
        if persist:
            self.last_saved = now
        boundary = None
        # Failure capture precedes cleanup capture if both first appear in a
        # completed response. The next poll still schedules cleanup separately.
        if summary.get('executionOutcome') == 'failed' and not self.failure_attempted:
            boundary, self.failure_attempted = 'failure', True
        elif summary['stage'] == 'completed' and not self.cleanup_attempted:
            boundary, self.cleanup_attempted = 'cleanup', True
        elif (summary['stage'] == 'results_ready' and summary['sequence'] not in self.result_sequences
              and len(self.result_sequences) < 3 and (self.last_full is None or now - self.last_full >= .5)):
            boundary = 'result'
            self.result_sequences.add(summary['sequence'])
        if boundary:
            self.last_full = now
        return PollDecision(persist, boundary)


def join_full_boundary(summary, full, *, boundary, received_at):
    """Bind full response to its trigger, NEVER turn summary into proof.

    received_at is captured AFTER full response parsing. Result/cleanup facts
    retain their original timestamps. A response that crossed a stage boundary
    cannot be retroactively called a live result. Failure receipts may be stale
    because they record a failure rather than certify native freshness.
    """
    _identity(summary); _identity(full); _clock(received_at)
    if boundary not in ('result', 'failure', 'cleanup'):
        raise PollingContractError('Unknown full capture boundary')
    if full.get('viewSchema') is not None:
        raise PollingContractError('Summary projection cannot replace full evidence')
    if any(full[k] != summary[k] or type(full[k]) is not type(summary[k])
           for k in ('roundId', 'generation', 'policyRevision', 'stage')) or full['sequence'] < summary['sequence']:
        raise PollingContractError('Full response crossed identity/revision/stage boundary')
    if boundary == 'result':
        if full['stage'] != 'results_ready' or full.get('executionOutcome') != 'passed' or not _fresh(full.get('observedAt'), received_at):
            raise PollingContractError('Live full result missing or expired')
    elif boundary == 'cleanup':
        proof = full.get('releaseProof')
        if full['stage'] != 'completed' or not isinstance(proof, dict) or not _fresh(proof.get('observedAt'), received_at):
            raise PollingContractError('Fresh full cleanup boundary required')
    elif full.get('executionOutcome') != 'failed':
        raise PollingContractError('Full failure receipt does not show failure')
    return {'snapshot': deepcopy(full), 'fullReceivedAt': received_at,
            'triggerSequence': summary['sequence'], 'boundary': boundary,
            'requiresFullGateValidation': True}
