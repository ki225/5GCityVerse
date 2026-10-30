"""Validated aggregate memory for adaptive policy decisions."""
from __future__ import annotations

from pathlib import Path
from contextlib import closing
import math
import json
import sqlite3

from backend.onprem.agents.contracts import ScenarioContract, _digest


class ValidatedMemory:
    """Local episodic memory; stores aggregate outcomes, never UE identities."""

    def __init__(self, path: Path):
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("Memory requires an absolute non-symlink path")
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS adaptive_lessons ("
                       "scenario_id TEXT NOT NULL, contract_digest TEXT NOT NULL, "
                       "iteration INTEGER NOT NULL, observation_digest TEXT NOT NULL, "
                       "candidate_id TEXT, outcome TEXT NOT NULL, improvement REAL, response_id TEXT NOT NULL, "
                       "recorded_at REAL NOT NULL, PRIMARY KEY (scenario_id, iteration))")
            db.execute("CREATE TABLE IF NOT EXISTS adaptive_contexts (scenario_id TEXT, contract_digest TEXT, "
                       "context_key TEXT NOT NULL, PRIMARY KEY(scenario_id, contract_digest))")
            db.execute("CREATE TABLE IF NOT EXISTS adaptive_sla_lessons (scenario_id TEXT, contract_digest TEXT, "
                       "iteration INTEGER, summary TEXT NOT NULL, PRIMARY KEY(scenario_id, contract_digest, iteration))")
        path.chmod(0o600)

    @staticmethod
    def _context(contract):
        return _digest({"groups": contract.group_counts, "metric": contract.metric,
                        "direction": contract.direction, "profiles": {key: dict(value) for key, value in contract.profiles.items()},
                        "minimum_improvement_percent": contract.minimum_improvement_percent,
                        "holdout_tolerance_percent": contract.holdout_tolerance_percent,
                        **({"trafficMode": "qos"} if contract.traffic_mode == "qos" else {}),
                        **({"serviceRequirementsRevision": contract.service_requirements_revision} if contract.service_requirements_revision else {})})

    @staticmethod
    def context_key(contract: ScenarioContract) -> str:
        """Public identity for matching aggregate lessons, never write authority."""
        return ValidatedMemory._context(contract)

    def record(self, contract: ScenarioContract, iteration: int, observation: dict,
               candidate_id: str | None, outcome: str, improvement: float | None,
               response_id: str,
               *, now: float) -> None:
        if (type(iteration) is not int or iteration < 0
                or outcome not in {"no_op", "no_holdout", "effect_verified", "no_effect", "regressed", "action_failed"}
                or not isinstance(response_id, str) or not response_id
                or (improvement is not None and (type(improvement) not in (int, float)
                    or not math.isfinite(improvement)))):
            raise ValueError("Only validated bounded outcomes enter memory")
        # Raw SUPIs and packet records must not enter the planner's memory.
        identity = {key: observation[key] for key in ("roundId", "generation", "sequence", "observedAt")}
        values = (contract.scenario_id, contract.digest, iteration, _digest(identity),
                  candidate_id, outcome, improvement, response_id)
        with closing(sqlite3.connect(self.path)) as db, db:
            prior = db.execute("SELECT scenario_id, contract_digest, iteration, observation_digest, "
                               "candidate_id, outcome, improvement, response_id FROM adaptive_lessons "
                               "WHERE scenario_id=? AND iteration=?", (contract.scenario_id, iteration)).fetchone()
            if prior and prior != values:
                raise ValueError("Validated memory changed on replay")
            db.execute("INSERT OR IGNORE INTO adaptive_lessons VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (*values, now))
            db.execute("INSERT OR IGNORE INTO adaptive_contexts VALUES (?,?,?)",
                       (contract.scenario_id, contract.digest, self._context(contract)))
            if contract.service_requirements_revision:
                sla = observation.get("sla", {})
                if sla.get("revision") != contract.service_requirements_revision:
                    raise ValueError("Memory SLA revision differs from the frozen contract")
                latest = sla.get("latestWindow", {})
                fields = ("scenario", "slice", "scope", "status", "reason", "expectedUes", "passedUes", "failedUes", "unknownUes")
                summary = json.dumps({"revision": sla["revision"], "status": sla.get("status"),
                    "services": [{k: r[k] for k in fields if k in r} for r in latest.get("services", [])],
                    "slices": [{k: r[k] for k in fields if k in r} for r in latest.get("slices", [])]}, sort_keys=True)
                key = (contract.scenario_id, contract.digest, iteration)
                prior_sla = db.execute("SELECT summary FROM adaptive_sla_lessons WHERE scenario_id=? AND contract_digest=? AND iteration=?", key).fetchone()
                if prior_sla and prior_sla[0] != summary:
                    raise ValueError("Validated SLA memory changed on replay")
                db.execute("INSERT OR IGNORE INTO adaptive_sla_lessons VALUES (?,?,?,?)", (*key, summary))

    def recent(self, contract: ScenarioContract, *, limit: int = 5) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Invalid memory read limit")
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT l.iteration, l.candidate_id, l.outcome, l.improvement, s.summary FROM adaptive_lessons l "
                              "LEFT JOIN adaptive_sla_lessons s ON s.scenario_id=l.scenario_id AND s.contract_digest=l.contract_digest AND s.iteration=l.iteration "
                              "WHERE l.scenario_id=? AND l.contract_digest=? ORDER BY l.iteration DESC LIMIT ?",
                              (contract.scenario_id, contract.digest, limit)).fetchall()
        return [{"iteration": row[0], "candidateId": row[1], "outcome": row[2],
                 "improvementPercent": row[3], **({"sla": json.loads(row[4])} if row[4] else {})} for row in rows]

    def search(self, contract: ScenarioContract, *, limit: int = 5) -> list[dict]:
        """Aggregate lessons from rounds with the same demand and policy context."""
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Invalid memory read limit")
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT l.iteration,l.candidate_id,l.outcome,l.improvement,s.summary FROM adaptive_lessons l "
                              "JOIN adaptive_contexts c ON c.scenario_id=l.scenario_id "
                              "AND c.contract_digest=l.contract_digest "
                              "LEFT JOIN adaptive_sla_lessons s ON s.scenario_id=l.scenario_id AND s.contract_digest=l.contract_digest AND s.iteration=l.iteration "
                              "WHERE c.context_key=? "
                              "ORDER BY l.recorded_at DESC,l.iteration DESC LIMIT ?",
                              (self._context(contract), limit)).fetchall()
        return [{"iteration": row[0], "candidateId": row[1], "outcome": row[2],
                 "improvementPercent": row[3], **({"sla": json.loads(row[4])} if row[4] else {})} for row in rows]
