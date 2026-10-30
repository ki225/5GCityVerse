"""LLM roles with stage-specific, independently enforced tool permissions."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from threading import Lock
import time
import urllib.request

from backend.onprem.agents.contracts import AgentLoopBlocked, _digest
from backend.onprem.agents.tool_execution import ParallelReadTools


def object_schema(properties=None):
    properties = properties or {}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def check_arguments(value, schema):
    """Small closed JSON schema subset used by the public tool interfaces."""
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                check_arguments(value, option)
                return
            except AgentLoopBlocked:
                pass
        raise AgentLoopBlocked("tool.invalid_arguments")
    kind = schema.get("type")
    valid = {"object": type(value) is dict, "string": type(value) is str,
             "integer": type(value) is int, "number": type(value) in (int, float),
             "null": value is None, "boolean": type(value) is bool}.get(kind, False)
    if not valid or ("enum" in schema and value not in schema["enum"]):
        raise AgentLoopBlocked("tool.invalid_arguments")
    if kind == "object":
        if set(value) != set(schema["properties"]):
            raise AgentLoopBlocked("tool.invalid_arguments")
        for key, item in value.items():
            check_arguments(item, schema["properties"][key])
    if kind in {"integer", "number"} and (not math.isfinite(value)
            or value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf)):
        raise AgentLoopBlocked("tool.invalid_arguments")
    if kind == "string" and (len(value) > schema.get("maxLength", 256)
                             or len(value) < schema.get("minLength", 0)):
        raise AgentLoopBlocked("tool.invalid_arguments")


@dataclass(frozen=True)
class StageTool:
    name: str
    description: str
    schema: dict
    handler: object
    terminal: bool = False
    writes: bool = False
    parallel_safe: bool = False

    def definition(self):
        return {"type": "function", "name": self.name, "description": self.description,
                "parameters": self.schema, "strict": True}


PHASE_ROLES = {"plan": "planner", "precheck": "validator", "apply": "executor",
               "postcheck": "validator", "restore": "executor", "restoration": "validator",
               "learn": "validator"}
PARALLEL_READ_PHASES = frozenset({'precheck'})


def _shape_summary(value):
    """Only report shape; never copy tool keys, identifiers or free text."""
    if isinstance(value, dict):
        return {"type": "object", "fieldCount": min(len(value), 1000000)}
    if isinstance(value, (list, tuple)):
        return {"type": "array", "itemCount": min(len(value), 1000000)}
    return {"type": ("null" if value is None else "boolean" if isinstance(value, bool)
                     else "number" if isinstance(value, (int, float)) else "string")}


class SafeModelFailure(AgentLoopBlocked):
    """Fixed public diagnostic fields; no provider body, prompt or exception text."""
    reasons=frozenset({'transport','timeout','incomplete','refusal','tool','schema','validation','cancelled'})
    stages=frozenset({'transport','response','tool','proposal_schema','validation'})
    def __init__(self,reason,stage,*,http_status=None,response_id=None,field_path=None):
        if reason not in self.reasons or stage not in self.stages:
            raise ValueError('Invalid safe model diagnostic')
        super().__init__('model.response_unavailable')
        self.safe_evidence={'status':'unavailable','reason':reason,'stage':stage}
        if type(http_status) is int and 400<=http_status<=599:
            self.safe_evidence['http_status']=http_status
        if isinstance(response_id,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',response_id):
            self.safe_evidence['response_id']=response_id
        if isinstance(field_path,str) and re.fullmatch(r'root(?:\.(?:field|item)\[[0-9]+\])*',field_path):
            self.safe_evidence['field_path']=field_path[:256]


def safe_transport_failure(error):
    current=error;seen=set();status=None;reason='transport'
    for _ in range(6):
        if current is None or id(current) in seen:break
        seen.add(id(current))
        if isinstance(current,TimeoutError) or isinstance(getattr(current,'reason',None),TimeoutError) or type(current).__name__ in {'APITimeoutError','ReadTimeout','ConnectTimeout','WriteTimeout','PoolTimeout'}:
            reason='timeout'
        code=getattr(current,'code',None)
        if type(code) is int and 400<=code<=599:status=code
        # The existing client replaces HTTPError with this exact local template.
        if type(current).__name__=='OpenAIError':
            match=re.fullmatch(r'OpenAI API returned HTTP ([0-9]{3})',str(current))
            if match:status=int(match.group(1))
        current=current.__cause__ or current.__context__
    return SafeModelFailure(reason,'transport',http_status=status)


class ResponsesToolModel:
    """Use the existing private OpenAI transport; prompts contain role views only."""

    def __init__(self, client, *, model=None):
        self.client = client
        self.model = model or client.model
        if not isinstance(self.model, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.model):
            raise ValueError("Invalid role model configuration")

    def complete(self, *, role, phase, messages, tools, instruction):
        from backend.onprem.intents.model_contracts import strict_schema,validate,SchemaMismatch,schema_hash
        try:
            for tool in tools:
                if tool.get('strict') is not True:raise ValueError
                strict_schema(tool['parameters'])
        except (ValueError,KeyError,TypeError):raise SafeModelFailure('schema','tool') from None
        payload = {"model": self.model, "store": False, "input": messages,
                   "instructions": instruction, "tools": tools, "tool_choice": "required",
                   "parallel_tool_calls": role == 'validator' and phase in PARALLEL_READ_PHASES,
                   "max_output_tokens": self.client.max_output_tokens}
        request = urllib.request.Request(self.client.API_URL, data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + self.client._api_key, "Content-Type": "application/json"}, method="POST")
        response_id=None
        try:
            raw = self.client._send_with_retry(request)
        except Exception as error:
            raise safe_transport_failure(error) from None
        try:
            if len(raw) > 2 * 1024 * 1024:
                raise SafeModelFailure('validation','response')
            body = json.loads(raw)
            response_id=body.get('id')
            if isinstance(body,dict) and body.get('status')=='incomplete':
                raise SafeModelFailure('incomplete','response',response_id=response_id)
            if any(part.get('type')=='refusal' for item in body.get('output',[]) if isinstance(item,dict)
                    for part in item.get('content',[]) if isinstance(part,dict)):
                raise SafeModelFailure('refusal','response',response_id=response_id)
            if (body.get("status") != "completed" or not isinstance(body.get("id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", body["id"])):
                raise SafeModelFailure('validation','response')
        except SafeModelFailure:
            raise
        except Exception:
            raise SafeModelFailure('schema','response') from None
        try:
            calls = [{"id": item["call_id"], "name": item["name"], "arguments": json.loads(item["arguments"])}
                     for item in body["output"] if item.get("type") == "function_call"]
            if not calls:raise ValueError
            schemas={t['name']:t['parameters'] for t in tools}
            for call in calls:
                if call['name'] not in schemas:raise ValueError
                validate(call['arguments'],schemas[call['name']])
            return {"response_id": body["id"], "calls": calls, "continuation": body["output"],
                'tool_schema_sha256':{name:schema_hash(s) for name,s in schemas.items()}}
        except SchemaMismatch as error:
            raise SafeModelFailure('schema','tool',response_id=response_id,field_path=error.path) from None
        except Exception:
            raise SafeModelFailure('tool','tool',response_id=response_id) from None


class RoleAgent:
    """The model selects tools. Only this dispatcher can execute permitted calls."""

    def __init__(self, role, model, *, maximum_turns=8, maximum_calls=24, audit=None, evidence=None, targets=None,
                 maximum_parallel_reads=4, knowledge=None):
        if role not in set(PHASE_ROLES.values()) or maximum_turns < 1 or maximum_calls < 1:
            raise ValueError("Invalid role budget")
        self.role, self.model = role, model
        self.maximum_turns, self.maximum_calls = maximum_turns, maximum_calls
        self.audit = audit or (lambda **_: None)
        self.evidence = evidence
        self.targets = targets
        self.parallel_reads = ParallelReadTools(maximum_parallel_reads)
        # Graph evidence callbacks share display/target state. Keep those
        # writes ordered while independent handlers run outside this lock.
        self._callback_lock = Lock()
        self.knowledge = knowledge if knowledge is not None else (Path(__file__).parent / "knowledge" / (role + ".md")).read_text(encoding="utf-8")

    def run(self, phase, view, tools):
        started = time.time()
        clock = time.monotonic()
        metadata = {"role": self.role, "phase": phase, "kind": "agent", "started_at": started}
        self.audit(**metadata, status="entered")
        try:
            result = self._run(phase, view, tools)
        except Exception:
            self.audit(**metadata, status="failed", ended_at=time.time(),
                       duration_ms=round((time.monotonic() - clock) * 1000, 3))
            raise
        self.audit(**metadata, status="finished", ended_at=time.time(),
                   duration_ms=round((time.monotonic() - clock) * 1000, 3))
        return result

    def _run(self, phase, view, tools):
        if PHASE_ROLES.get(phase) != self.role:
            raise AgentLoopBlocked("tool.role_phase_denied")
        registry = {tool.name: tool for tool in tools}
        if (len(registry) != len(tools) or not any(tool.terminal for tool in tools)
                or "decline_stage" in registry
                or any(tool.parallel_safe and (tool.writes or tool.terminal) for tool in tools)):
            raise ValueError("Invalid stage tool registry")
        def decline(reason):
            # A model may refuse to act, including when its only other terminal
            # performs a write. No policy/default decision is synthesized here.
            self.audit(role=self.role, phase=phase, kind='agent', status='failed',
                       code='model.stage_declined.'+reason)
            raise AgentLoopBlocked("model.stage_declined")
        tools = [*tools, StageTool("decline_stage",
            "Decline this stage without applying or restoring policy; fail closed.",
            object_schema({"reason": {"type": "string", "enum": [
                "insufficient_evidence", "unconfirmed_authority", "insufficient_budget"]}}),
            decline, terminal=True)]
        messages = [{"role": "user", "content": json.dumps(view, sort_keys=True, allow_nan=False)}]
        used = 0
        for turn in range(self.maximum_turns):
            remaining_calls = self.maximum_calls - used
            if remaining_calls < 1:
                raise AgentLoopBlocked("model.stage_budget_exhausted")
            terminal_required = turn == self.maximum_turns - 1 or remaining_calls == 1
            offered = [tool for tool in tools if not terminal_required or tool.terminal]
            registry = {tool.name: tool for tool in offered}
            budget = {"turns_remaining": self.maximum_turns - turn,
                "tool_calls_remaining": remaining_calls,
                "terminal_tools": [tool.name for tool in tools if tool.terminal],
                "terminal_required": terminal_required,
                "maximum_calls_this_response": 1 if terminal_required else remaining_calls - 1}
            parallel_names = [tool.name for tool in offered if tool.parallel_safe
                              and not tool.writes and not tool.terminal] if phase in PARALLEL_READ_PHASES else []
            budget['parallel_read_tools'] = parallel_names
            budget['maximum_parallel_reads'] = self.parallel_reads.maximum_concurrency if parallel_names else 1
            instruction = (self.knowledge + "\nCurrent phase: " + phase
                + "\nStage budget: " + json.dumps(budget, sort_keys=True)
                + "\nRemaining turns include this response. Prioritize necessary reads; do not exhaust the budget "
                  "enumerating every tool. Finish with one terminal tool. If evidence or authority is insufficient, "
                  "choose decline_stage; never invent a proposal, validation, or write to meet the budget.")
            if parallel_names:
                instruction += ("\nRequest independent tools from parallel_read_tools together in one response; "
                    "their results arrive together before your next response. Use the supplied view directly "
                    "when it already answers a question. Request dependent reads only after their inputs arrive. "
                    "Never mix a terminal or write tool with another call. The terminal validation rechecks "
                    "required live authority; do not enumerate optional reads as a mandatory checklist.")
            model_started, model_clock = time.time(), time.monotonic()
            model_metadata = {"role": self.role, "phase": phase, "kind": "model",
                              "turn": turn, "started_at": model_started}
            self.audit(**model_metadata, status="waiting")
            try:
                response = self.model.complete(role=self.role, phase=phase, messages=messages,
                    tools=[tool.definition() for tool in offered], instruction=instruction)
            except Exception:
                self.audit(**model_metadata, status="failed", ended_at=time.time(),
                           duration_ms=round((time.monotonic() - model_clock) * 1000, 3))
                raise
            self.audit(**model_metadata, status="received", ended_at=time.time(),
                       duration_ms=round((time.monotonic() - model_clock) * 1000, 3))
            calls, response_id = response.get("calls"), response.get("response_id")
            if (not isinstance(calls, list) or not calls or len(calls) > self.maximum_calls - used
                    or not isinstance(response_id, str) or not response_id):
                raise AgentLoopBlocked("model.tool_call_required")
            # Validate the ENTIRE batch before any side effect, including hidden
            # stage/role injection and a second write in the same model response.
            ids = set()
            for call in calls:
                if (not isinstance(call, dict) or set(call) != {"id", "name", "arguments"}
                        or not isinstance(call["id"], str) or not call["id"] or call["id"] in ids
                        or call.get("name") not in registry):
                    raise AgentLoopBlocked("tool.not_allowed")
                ids.add(call["id"])
                check_arguments(call["arguments"], registry[call["name"]].schema)
            if len(calls) > 1 and any(registry[c["name"]].writes or registry[c["name"]].terminal for c in calls):
                raise AgentLoopBlocked("tool.serial_stage_required")
            if len(calls) == remaining_calls and not any(registry[c["name"]].terminal for c in calls):
                raise AgentLoopBlocked("model.terminal_budget_reserved")
            messages.extend(response.get("continuation") or [
                {"type": "function_call", "call_id": c["id"], "name": c["name"], "arguments": json.dumps(c["arguments"])} for c in calls])
            used += len(calls)
            def execute(call):
                return self._execute(phase, turn, response_id, registry[call['name']], call)
            if len(calls) > 1 and all(call['name'] in parallel_names for call in calls):
                results = self.parallel_reads.run(calls, execute)
            else:
                results = [execute(call) for call in calls]
            for call, result in zip(calls, results):
                tool = registry[call['name']]
                if tool.terminal:
                    return {"result": result, "response_id": response_id}
                messages.append({"type": "function_call_output", "call_id": call["id"],
                                 "output": json.dumps(result, sort_keys=True, allow_nan=False)})
        raise AgentLoopBlocked("model.stage_budget_exhausted")

    def _execute(self, phase, turn, response_id, tool, call):
        with self._callback_lock:
            metadata = {"role": self.role, "phase": phase, "tool": tool.name, "response_id": response_id,
                        "model": getattr(self.model, "model", "configured-model"), "kind": "tool",
                        "turn": turn, "call_id": _digest([self.role, phase, response_id, call['id'], turn]),
                        "started_at": time.time(), "input_summary": _shape_summary(call['arguments'])}
            if self.targets:
                metadata['targetRefs'] = self.targets(metadata, call['arguments'])
            tool_clock = time.monotonic()
            self.audit(**metadata, status='started')
        try:
            result = tool.handler(**call['arguments'])
        except Exception:
            with self._callback_lock:
                self.audit(**metadata, status='failed', ended_at=time.time(),
                           duration_ms=round((time.monotonic() - tool_clock) * 1000, 3))
            raise
        with self._callback_lock:
            evidence_ref = self.evidence(metadata, result, call['arguments']) if self.evidence else None
            self.audit(**metadata, status='completed', ended_at=time.time(),
                       duration_ms=round((time.monotonic() - tool_clock) * 1000, 3),
                       output_summary=_shape_summary(result),
                       **({'display_ref': evidence_ref} if evidence_ref else {}))
        return result
