"""Internal AF notification receiver; never an SMF policy callback.

Internal management guard only when NRF OAuth is disabled. Operator must supply
two secret env values and exact PCF source IPs; no public listener assumption.
"""
import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class CallbackStore:
    def __init__(self, path, signing_secret, pcf_ips, resource_base=None, *, recovery_fence=False, pod_uid=None):
        if len(signing_secret) < 32 or not pcf_ips:
            raise ValueError("Callback secret and exact PCF IP allowlist required")
        self.path, self.secret = str(path), signing_secret.encode()
        self.peers = {str(ipaddress.ip_address(v)) for v in pcf_ips}
        self.resource_base = resource_base.rstrip("/") if resource_base else None
        self.recovery_fence = recovery_fence
        self._fence_lock = threading.RLock()
        self._fence_db = None
        self._fence_epoch = None
        self.pod_uid = pod_uid
        if recovery_fence:
            if not pod_uid or not re.fullmatch(r"[a-f0-9-]{36}", pod_uid):
                raise ValueError("Recovery fence requires an exact Pod UID")
            if not Path(path).is_file():
                raise ValueError("Recovery fence requires an existing callback database")
            db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True,
                                 timeout=0.2, check_same_thread=False)
            try:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                schema = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
                if schema != [("table", "actions"), ("table", "receipts")]:
                    raise ValueError("Recovery fence requires the exact callback tables")
                expected = {"actions": ["id", "fingerprint", "uri"],
                            "receipts": ["id", "digest", "body", "observed"]}
                for table, columns in expected.items():
                    if [row[1] for row in db.execute(f"PRAGMA table_info({table})")] != columns:
                        raise ValueError("Recovery fence requires the existing callback schema")
                self._fence_db = db
                self._fence_epoch = str(uuid.uuid4())
            except BaseException:
                db.rollback()
                db.close()
                raise
        else:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as db:
                db.execute("CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, uri TEXT)")
                db.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT, digest TEXT, body TEXT, observed REAL, UNIQUE(id,digest))")

    def fence_receipt(self):
        if not self.recovery_fence:
            raise ValueError("Recovery fence is not enabled")
        with self._fence_lock:
            db = self._fence_db
            if db is None or not db.in_transaction:
                raise RuntimeError("Recovery fence transaction is lost")
            # A second SQLite connection must be unable to reserve the writer slot.
            probe = sqlite3.connect(Path(self.path).resolve().as_uri() + "?mode=rw", uri=True, timeout=0)
            try:
                try:
                    probe.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
                else:
                    probe.rollback()
                    raise RuntimeError("Recovery fence writer lock is lost")
            finally:
                probe.close()
            actions = db.execute("SELECT id,fingerprint,uri FROM actions ORDER BY id").fetchall()
            receipts = db.execute("SELECT id,digest,body,observed FROM receipts ORDER BY id,digest").fetchall()
            digest = hashlib.sha256(json.dumps([actions, receipts], sort_keys=True,
                                               separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            return {"source": "native-callback-current-epoch/v1", "podUid": self.pod_uid,
                    "fenceEpoch": self._fence_epoch, "observedAt": time.time(),
                    "readWriteFence": "active", "inFlight": 0,
                    "actions": len(actions), "receipts": len(receipts),
                    "journalSha256": digest, "complete": True, "atomicSnapshot": True}

    def close(self):
        with self._fence_lock:
            if self._fence_db is not None:
                self._fence_db.rollback()
                self._fence_db.close()
                self._fence_db = None

    def connect(self):
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def token(self, action_id, fingerprint):
        return hmac.new(self.secret, (action_id + ":" + fingerprint).encode(), hashlib.sha256).hexdigest()

    def register(self, action_id, fingerprint, uri=None):
        if self.recovery_fence:
            raise RecoveryFenced("Callback registration is fenced")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", action_id) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("Invalid owned callback action")
        if uri is not None and not re.fullmatch(r"https?://[A-Za-z0-9.:-]+/npcf-policyauthorization/v1/app-sessions/[A-Za-z0-9_.:-]+", uri):
            raise ValueError("Invalid official owned URI")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT fingerprint,uri FROM actions WHERE id=?", (action_id,)).fetchone()
            if old and (old[0] != fingerprint or (old[1] and uri and old[1] != uri)):
                raise ValueError("Callback ownership cannot be rebound")
            db.execute("INSERT INTO actions VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET uri=COALESCE(excluded.uri,actions.uri)", (action_id, fingerprint, uri))
        return self.token(action_id, fingerprint)

    def receive(self, peer, action_id, token, body):
        if self.recovery_fence:
            return 503
        if peer not in self.peers or len(body) > 65536:
            return 403
        with self.connect() as db:
            row = db.execute("SELECT fingerprint,uri FROM actions WHERE id=?", (action_id,)).fetchone()
            if not row or not hmac.compare_digest(token, self.token(action_id, row[0])):
                return 403
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    return 400
                if payload.get("termCause") == "PDU_SESSION_TERMINATION":
                    uri = payload.get("resUri", "")
                    if (not self.resource_base or not isinstance(uri, str) or not
                            re.fullmatch(re.escape(self.resource_base) + r"/app-sessions/[A-Za-z0-9_.:-]+", uri)
                            or (row[1] and row[1] != uri)):
                        return 409
                    # Official PCF termination carries the server-assigned URI.
                    # This is correlation only; GET404/native absence still needed.
                    db.execute("UPDATE actions SET uri=? WHERE id=?", (uri, action_id))
                elif not row[1] or payload.get("evSubsUri") != row[1] + "/events-subscription":
                    return 409
            except (ValueError, UnicodeDecodeError):
                return 400
            digest = hashlib.sha256(body).hexdigest()
            db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?,?,?)", (action_id, digest, body.decode(), time.time()))
        return 204  # Notification receipt only, NEVER installation/effect proof.

    def read(self, action_id):
        with self.connect() as db:
            row = db.execute("SELECT fingerprint,uri FROM actions WHERE id=?", (action_id,)).fetchone()
            if not row:
                return None
            receipts = [{"body": json.loads(body), "observedAt": at} for body, at in
                        db.execute("SELECT body,observed FROM receipts WHERE id=? ORDER BY observed", (action_id,))]
        return {"actionId": action_id, "fingerprint": row[0], "uri": row[1], "receipts": receipts}


class RecoveryFenced(RuntimeError):
    pass


def recovery_fence_enabled():
    value = os.environ.get("CITYVERSE_AF_RECOVERY_FENCE", "0")
    if value not in {"0", "1"}:
        raise ValueError("CITYVERSE_AF_RECOVERY_FENCE must be 0 or 1")
    return value == "1"


def handler(store, admin_token):
    if len(admin_token) < 32:
        raise ValueError("Management secret required")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Paths contain capability tokens.

        def reply(self, code, payload=None):
            data = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                return self.reply(200, {"service": "onprem-af-callback", "auth": "internal-management-guard", "oauthClaimed": False})
            if self.path == "/internal/recovery-fence" and store.recovery_fence:
                if self.client_address[0] not in {"127.0.0.1", "::1"}:
                    return self.reply(403)
                try:
                    return self.reply(200, store.fence_receipt())
                except (RuntimeError, sqlite3.Error):
                    return self.reply(503)
            parts = self.path.split("/")
            if len(parts) == 4 and parts[1:3] == ["admin", "actions"]:
                if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + admin_token):
                    return self.reply(403)
                record = store.read(parts[3])
                return self.reply(200 if record else 404, record)
            self.reply(404)

        def do_POST(self):
            if store.recovery_fence:
                return self.reply(503)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    return self.reply(413)
                body = self.rfile.read(size)
                parts = self.path.split("/")
                if len(parts) == 4 and parts[1:3] == ["admin", "actions"]:
                    if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + admin_token):
                        return self.reply(403)
                    value = json.loads(body)
                    token = store.register(parts[3], value["fingerprint"], value.get("uri"))
                    return self.reply(200, {"path": "/notifications/" + parts[3] + "/" + token})
                if len(parts) == 4 and parts[1] == "notifications":
                    return self.reply(store.receive(self.client_address[0], parts[2], parts[3], body))
                return self.reply(404)
            except (ValueError, KeyError, TypeError):
                self.reply(400)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9082)
    parser.add_argument("--state", default="/state/callback.sqlite")
    args = parser.parse_args()
    store = CallbackStore(args.state, os.environ.get("CITYVERSE_AF_CALLBACK_SECRET", ""),
                          os.environ.get("CITYVERSE_AF_PCF_IPS", "").split(","),
                          os.environ.get("CITYVERSE_AF_PCF_RESOURCE_BASE"),
                          recovery_fence=recovery_fence_enabled(), pod_uid=os.environ.get("CITYVERSE_AF_POD_UID"))
    server = ThreadingHTTPServer((args.host, args.port), handler(store, os.environ.get("CITYVERSE_AF_ADMIN_TOKEN", "")))
    server.serve_forever()


if __name__ == "__main__":
    main()
