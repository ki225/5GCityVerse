"""Replay original-rate compensation without rewriting failed install history."""
from backend.onprem.validation import normal as common


TABLE = '''CREATE TABLE IF NOT EXISTS fixture_compensations (
id TEXT PRIMARY KEY, request_id TEXT NOT NULL, supi TEXT NOT NULL,
record TEXT NOT NULL, UNIQUE(request_id,supi))'''
STATES = frozenset(('reconciled', 'reconciled_released'))


def compensation_rows(db, request_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fixture_compensations'").fetchone():
        return []
    return list(db.execute('SELECT id,request_id,supi,record FROM fixture_compensations WHERE request_id=? ORDER BY rowid',
                           (request_id,)))


def validate_compensations(record, rows):
    """Only complete accepted original-rate compensations close a partial arm."""
    from backend.onprem.validation import constrained as state
    if record['state'] not in STATES:
        if rows and record['state'] != 'active':
            raise state.ConstrainedFixtureBlocked('Compensation belongs to a partial active fixture')
        return
    originals = {r['supi']: r for r in record['originalNative']['native']['perUe']}
    if (len(rows) != len(originals) or {r[2] for r in rows} != set(originals)
            or record['fixtureInstalledNative'] is not None or record['innerRestoredNative'] is not None
            or record['modelActionId'] is not None or record['modelPolicyActionIds']):
        raise state.ConstrainedFixtureBlocked('Partial fixture compensation is incomplete')
    fields = {'intentId','requestId','roundId','generation','manifestDigest','supi','rates',
              'state','httpStatus','pfcpAccepted','observedAt','uncertain','errorCategory'}
    for identifier, request_id, supi, raw in rows:
        item = common._read_json(raw)
        if (type(item) is not dict or set(item) != fields or item['intentId'] != identifier
                or item['requestId'] != request_id or item['supi'] != supi
                or type(item['generation']) is not int or not common._uuid(item['requestId'])
                or request_id != record['claim']['requestId']
                or any(item[k] != record['claim'][k] for k in ('roundId','generation'))
                or item['manifestDigest'] != record['manifestDigest']
                or item['rates'] != state._original_rates(record, supi)
                or item['state'] != 'accepted' or item['httpStatus'] != 200
                or item['pfcpAccepted'] is not True or item['uncertain'] is not False
                or not isinstance(item['observedAt'], str) or item['errorCategory'] is not None):
            raise state.ConstrainedFixtureBlocked('Partial fixture original-rate receipt is invalid')
