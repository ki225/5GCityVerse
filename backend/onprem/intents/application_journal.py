"""Durable application command claims; unknown native writes are never replayed."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .actuators import ActuationBlocked


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def command_key(target, action, authority, parameters):
    identity = [target, action, authority['owner_id'], authority['operation_id']]
    if action == 'restore':
        identity.append(parameters['before_version'])
    return digest(identity)


class ApplicationJournal:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.is_symlink():
            raise ValueError('Private absolute application journal required')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(descriptor)
        if os.name != 'nt' and self.path.stat().st_mode & 0o077:
            raise ValueError('Private application journal permissions required')
        with self.connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS applications (id TEXT PRIMARY KEY,value TEXT NOT NULL);'
                'CREATE TABLE IF NOT EXISTS application_commands (id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,value TEXT NOT NULL);')

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def target(self, target, db=None):
        if db is None:
            with self.connect() as connection:
                return self.target(target, connection)
        row = db.execute('SELECT value FROM applications WHERE id=?', (target,)).fetchone()
        return json.loads(row[0]) if row else {'revision': 0, 'owner_id': None, 'operation_id': None, 'resources': [], 'busy': None}

    def save(self, target, record, db):
        db.execute('INSERT OR REPLACE INTO applications VALUES (?,?)', (target, json.dumps(record, allow_nan=False)))

    def claim(self, target, action, authority, parameters, before):
        key = command_key(target, action, authority, parameters)
        fingerprint = digest({'authority': authority, 'parameters': parameters})
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT fingerprint,value FROM application_commands WHERE id=?', (key,)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise ActuationBlocked('Application command replay differs')
                return key, json.loads(old[1]), False
            record = self.target(target, db)
            if (record['busy'] or record['owner_id'] not in (None, authority['owner_id'])
                    or record['revision'] != before.get('journal_revision')):
                raise ActuationBlocked('Application command or another owner holds target')
            command = dict(target_id=target, action=action, authority=authority, parameters=parameters,
                           before=before, phase='prepared', resources=[], result=None)
            record.update(busy=key, owner_id=authority['owner_id'], operation_id=authority['operation_id'], revision=record['revision'] + 1)
            self.save(target, record, db)
            db.execute('INSERT INTO application_commands VALUES (?,?,?)', (key, fingerprint, json.dumps(command)))
        return key, command, True

    def advance(self, key, phase, *, resources=None, result=None, finish=False):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM application_commands WHERE id=?', (key,)).fetchone()
            command = json.loads(row[0])
            record = self.target(command['target_id'], db)
            if record['busy'] != key:
                if finish and command['phase'] == phase:
                    return command
                raise ActuationBlocked('Application command ownership changed')
            command['phase'] = phase
            if resources is not None:
                command['resources'] = resources
                record['resources'] = resources
            if result is not None:
                command['result'] = result
            record.update(revision=record['revision'] + 1, busy=None if finish else key)
            if finish and phase == 'restored':
                record.update(owner_id=command['before'].get('owner_id'), operation_id=command['before'].get('operation_id'), node_id=command['before'].get('node_id'))
            self.save(command['target_id'], record, db)
            db.execute('UPDATE application_commands SET value=? WHERE id=?', (json.dumps(command), key))
        return command

    def commands(self):
        with self.connect() as db:
            return [(key, json.loads(value)) for key, value in db.execute('SELECT id,value FROM application_commands')]

    def release_for_compensation(self, target, authority, expected_revision):
        """Transfer an uncertain own deployment to an explicit restore claim."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record = self.target(target, db)
            if (record['revision'] != expected_revision or not record['busy']
                    or record['owner_id'] != authority['owner_id'] or record['operation_id'] != authority['operation_id']):
                raise ActuationBlocked('Application recovery owner changed')
            row = db.execute('SELECT value FROM application_commands WHERE id=?', (record['busy'],)).fetchone()
            command = json.loads(row[0])
            if command['action'] != 'deploy' or command['phase'] != 'recovery_required':
                raise ActuationBlocked('Only uncertain owned deployment permits compensation transfer')
            command['phase'] = 'compensation_claimed'
            db.execute('UPDATE application_commands SET value=? WHERE id=?', (json.dumps(command), record['busy']))
            record.update(busy=None, revision=record['revision'] + 1)
            self.save(target, record, db)
            return record['revision']
