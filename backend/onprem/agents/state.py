"""Checkpoint state contains references and bounded summaries, never UE rows."""
from typing import TypedDict


class AgentGraphState(TypedDict, total=False):
    thread_id: str
    intent_id: str
    intent_revision: int
    operation_id: str
    spatial_context_ref: str
    membership_ref: str
    execution_mode: str
    round_id: str
    generation: int
    contract_digest: str
    cycle: int
    before_ref: str
    before_seal_ref: str
    proposal_ref: str
    verdict_ref: str
    restoration_ref: str
    restore_request_ref: str
    memory_context_key: str
    lessons_ref: str
    lesson_ref: str
    validation_phase: str
    operation: str
    active_action_ref: str
    changes_used: int
    last_change_cycle: int
    restore_required: bool
    proposal_attempt: int
    proposal_feedback_ref: str
    after_ref: str
    plan_ref: str
    validated_ref: str
    action_ref: str
    verification_ref: str
    outcome_ref: str
    active_ref: str
    phase: str
    route: str
    stop_reason: str
    error_code: str
    changes: int
    last_change: int
    baseline_ref: str

