"""Typed owned-generation stop journals; never a public resize bypass.

The exceptional port admits ONLY native-zero, terminal/absent owned Pods. The
separate healthy port retains the ordinary mutation guard and atomically binds
its zero-generation write to the immutable pre-NAS native baseline. Both still
require ordinary Pod retirement and independent final native release proof.
"""
import hashlib
import json
import time

from .errors import EvidenceUnavailable


def sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def authority(lease,permit,expected_generation,original_allocation,snapshot,now):
    from backend.onprem.scenarios.intent_traffic import ServiceIntentTrafficLease,IntentTrafficDrainPermit
    if type(lease) is not ServiceIntentTrafficLease or type(permit) is not IntentTrafficDrainPermit:
        raise EvidenceUnavailable('Typed owned-generation drain authority required')
    permit.validate(lease,now)
    if (type(expected_generation) is not int or expected_generation<=0 or not isinstance(original_allocation,list)
            or not original_allocation or not isinstance(snapshot,dict)
            or snapshot.get('generation')!=expected_generation or snapshot.get('requestId')!=lease.request_id):
        raise EvidenceUnavailable('Owned drain original round/generation mismatch')
    receipts=[a for a in snapshot.get('actions',[]) if a.get('kind')=='ue_allocation'
              and a.get('accepted') is True and a.get('reused') is False]
    if len(receipts)!=1 or receipts[0].get('generation')!=expected_generation:
        raise EvidenceUnavailable('Owned drain needs original non-reused allocation receipt')
    return sha(original_allocation)


def native_zero(evidence,generation,allocation,now=None):
    from .ue import seconds,SLICES
    now=time.time() if now is None else now
    if (not isinstance(evidence,dict) or evidence.get('schemaVersion')!='cityverse.evidence/v1'
            or evidence.get('generation')!=generation or not -1<=now-seconds(evidence.get('observedAt'))<=15):
        raise EvidenceUnavailable('Owned drain requires fresh native generation evidence')
    expected_sources={'amf':'free5gc-v4.2.2:namf-oam/v1','smf':'free5gc-v4.2.2:nsmf-oam/v1',
                      'upf':'gtp5g-netlink:four-upf'}
    clocks=[]
    for name in ('ue','amf','smf','upf'):
        row=evidence.get('sources',{}).get(name,{})
        valid_source=(row.get('source') in {'kubernetes-uid:free-ran-ue-tun','kubernetes-uid:ueransim-nr-cli'}
                      if name=='ue' else row.get('source')==expected_sources[name])
        if row.get('available') is not True or not valid_source or not -1<=now-seconds(row.get('observedAt'))<=15:
            raise EvidenceUnavailable('Owned drain native source unavailable or stale')
        clocks.append(seconds(row['observedAt']))
    if evidence['sources']['smf'].get('inventoryComplete') is not True:
        raise EvidenceUnavailable('Owned drain requires complete native SMF inventory')
    for name in ('amf','smf'):
        count=evidence['sources'][name].get('contextCount')
        if type(count) is not int or count!=0:
            raise EvidenceUnavailable('Owned quarantine drain requires explicit zero native AMF/SMF census')
    if evidence['sources']['amf'].get('scope')!='all-native-amf-registered-contexts':
        raise EvidenceUnavailable('Owned drain requires full native AMF census scope')
    try:
        epochs=json.loads(evidence['sources']['upf']['epoch'])
    except (KeyError,TypeError,ValueError):
        raise EvidenceUnavailable('Owned drain complete kernel epochs unavailable') from None
    if set(epochs)!=set(SLICES) or any(not isinstance(v,str) or not v for v in epochs.values()):
        raise EvidenceUnavailable('Owned drain complete kernel epochs unavailable')
    inventories=evidence['sources']['upf'].get('inventories')
    if not isinstance(inventories,dict) or set(inventories)!=set(SLICES):
        raise EvidenceUnavailable('Owned drain requires all native kernel census counts')
    for name,row in inventories.items():
        if (row.get('epoch')!=epochs[name] or any(type(row.get(k)) is not int or row[k]!=0 for k in ('sessionCount','residualCount'))
                or not -1<=now-seconds(row.get('observedAt'))<=15):
            raise EvidenceUnavailable('Owned quarantine drain refuses native kernel sessions/residuals')
        clocks.append(seconds(row['observedAt']))
    supis={r['supi'] for r in allocation}
    rows=evidence.get('ues')
    if not isinstance(rows,list) or len({r.get('supi') for r in rows})!=len(rows):
        raise EvidenceUnavailable('Owned drain native identities malformed')
    for row in rows:
        if (row.get('supi') not in supis or row.get('generation')!=generation
                or row.get('amf',{}).get('registered') not in (None,False)
                or row.get('smf',{}).get('established') not in (None,False)
                or row.get('smf',{}).get('sessionCount',0)!=0
                or row.get('smf',{}).get('smContextRef') or row.get('smf',{}).get('sessionId')
                or row.get('upf') or row.get('ue',{}).get('running') is True
                or row.get('traffic',{}).get('success') is True):
            raise EvidenceUnavailable('Owned quarantine drain refuses active or unattributed native state')
    return min(clocks+[seconds(evidence['observedAt'])])


class OwnedDrainRepository:
    """One bounded current command, committed atomically with its zero generation."""
    def __init__(self,store):
        self.store=store

    @staticmethod
    def initialize(db):
        db.execute('CREATE TABLE IF NOT EXISTS ue_owned_drain_current(slot INTEGER PRIMARY KEY CHECK(slot=1),value TEXT NOT NULL)')

    @staticmethod
    def _read(db):
        row=db.execute('SELECT value FROM ue_owned_drain_current WHERE slot=1').fetchone()
        return json.loads(row[0]) if row else None

    def replay(self,lease,generation,allocation_sha):
        from backend.onprem.scenarios.intent_traffic import lease_identity,ServiceIntentTrafficLease,lease_renewal_follows
        with self.store.connect() as db:
            self.initialize(db)
            row=self._read(db)
            if not row or row['original_generation']!=generation:
                return None
            if (not lease_renewal_follows(ServiceIntentTrafficLease.from_dict(row['lease']),lease)
                    or row['allocation_sha256']!=allocation_sha):
                raise EvidenceUnavailable('Foreign or stale owned drain journal')
            current=self.store.decode(db.execute('SELECT * FROM generations ORDER BY id DESC LIMIT 1').fetchone())
            if not current or current['generation']!=row['zero_generation'] or current['allocation'] or any(current['desired'].values()):
                raise EvidenceUnavailable('Owned drain zero generation has changed')
            return current['result']

    def stop(self,lease,permit,generation,allocation_sha,proof_clock,pods):
        from .ue import blank_status,SLICES
        from .population import PopulationRepository
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.initialize(db)
            self.store._assert_recovery_clear(db,require_fresh=True)
            PopulationRepository.assert_no_pending(db,allow_stop=True)
            current=self.store.decode(db.execute('SELECT * FROM generations ORDER BY id DESC LIMIT 1').fetchone())
            if not current or current['generation']!=generation or sha(current['allocation'])!=allocation_sha:
                raise EvidenceUnavailable('Owned drain generation/allocation CAS changed')
            if any(key in current['result'] for key in ('recoveryMode','exceptionalRecovery','hostStateLoss')):
                raise EvidenceUnavailable('Exceptional recovery cannot use owned quarantine drain')
            if not -1<=time.time()-proof_clock<=15:
                raise EvidenceUnavailable('Owned drain native evidence expired before commit')
            counts=dict.fromkeys(SLICES,0)
            cursor=db.execute('INSERT INTO generations(desired,allocation,created,status) VALUES (?,?,?,?)',
                (json.dumps(counts),'[]',time.time(),'{}'))
            result=blank_status(cursor.lastrowid,counts)
            db.execute('UPDATE generations SET status=? WHERE id=?',(json.dumps(result),cursor.lastrowid))
            record={'lease':lease.to_dict(),'release_operation_id':permit.release_operation_id,
                'original_generation':generation,'allocation_sha256':allocation_sha,'zero_generation':cursor.lastrowid,
                'native_zero_observed_at':proof_clock,'owned_terminal_pods':pods,'native_release_verified':False}
            db.execute('INSERT OR REPLACE INTO ue_owned_drain_current VALUES(1,?)',(json.dumps(record),))
            return result


class OwnedNormalStopRepository:
    """One current healthy owned stop; receipt and generation share one commit."""
    def __init__(self,store):self.store=store

    @staticmethod
    def initialize(db):
        db.execute('CREATE TABLE IF NOT EXISTS ue_owned_normal_stop(slot INTEGER PRIMARY KEY CHECK(slot=1),value TEXT NOT NULL,completed INTEGER NOT NULL DEFAULT 0)')
        if 'completed' not in {r[1] for r in db.execute('PRAGMA table_info(ue_owned_normal_stop)')}:
            db.execute('ALTER TABLE ue_owned_normal_stop ADD COLUMN completed INTEGER NOT NULL DEFAULT 0')

    def read(self):
        from .owned_native_teardown import OwnedZeroGenerationStopReceipt
        with self.store.connect() as db:
            self.initialize(db)
            row=db.execute('SELECT value FROM ue_owned_normal_stop WHERE slot=1').fetchone()
        return OwnedZeroGenerationStopReceipt.from_dict(json.loads(row[0])) if row else None

    def replay(self,baseline):
        receipt=self.read()
        if receipt is None:return None
        if receipt.baseline.permit['release_operation_id']!=baseline.permit['release_operation_id']:
            return None
        if receipt.baseline!=baseline:
            raise EvidenceUnavailable('Owned normal stop baseline changed')
        self.validate(receipt)
        return receipt

    def active(self):
        with self.store.connect() as db:
            self.initialize(db)
            row=db.execute('SELECT completed FROM ue_owned_normal_stop WHERE slot=1').fetchone()
        return bool(row and row[0]==0)

    def complete(self,receipt):
        self.validate(receipt)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current=self.store.decode(db.execute('SELECT * FROM generations ORDER BY id DESC LIMIT 1').fetchone())
            row=db.execute('SELECT value FROM ue_owned_normal_stop WHERE slot=1').fetchone()
            if (not current or current['generation']!=receipt.zero_generation or current['allocation']
                    or any(current['desired'].values()) or not row or json.loads(row[0])!=receipt.to_dict()):
                raise EvidenceUnavailable('Owned stop completion CAS changed')
            db.execute('UPDATE ue_owned_normal_stop SET completed=1 WHERE slot=1')

    def validate(self,receipt):
        recorded=self.read()
        current=self.store.current()
        original=self.store.read_generation(receipt.baseline.original_generation)
        if (recorded!=receipt or not current or current['generation']!=receipt.zero_generation
                or current['allocation'] or any(current['desired'].values()) or not original
                or sha(original['allocation'])!=receipt.baseline.allocation_sha256):
            raise EvidenceUnavailable('Exact owned normal stop receipt no longer owns current zero generation')
        return current

    def stop(self,baseline):
        from .ue import blank_status,SLICES
        from .population import PopulationRepository
        from .ue_reservation import HeldUERepository
        from .owned_native_teardown import OwnedDrainBaseline,OwnedZeroGenerationStopReceipt
        if type(baseline) is not OwnedDrainBaseline:
            raise EvidenceUnavailable('Typed pre-stop baseline required')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.initialize(db)
            self.store._assert_recovery_clear(db,require_fresh=True)
            HeldUERepository.assert_no_held(db)
            PopulationRepository.assert_no_pending(db,allow_stop=True)
            current=self.store.decode(db.execute('SELECT * FROM generations ORDER BY id DESC LIMIT 1').fetchone())
            if (not current or current['generation']!=baseline.original_generation
                    or sha(current['allocation'])!=baseline.allocation_sha256
                    or not current['allocation'] or not any(current['desired'].values())):
                raise EvidenceUnavailable('Healthy owned stop original allocation CAS changed')
            if db.execute("SELECT 1 FROM ue_exceptional_recoveries WHERE generation_id=? AND phase!='completed'",(current['generation'],)).fetchone():
                raise EvidenceUnavailable('Exceptional recovery cannot use healthy owned stop')
            counts=dict.fromkeys(SLICES,0)
            cursor=db.execute('INSERT INTO generations(desired,allocation,created,status) VALUES (?,?,?,?)',
                (json.dumps(counts),'[]',time.time(),'{}'))
            result=blank_status(cursor.lastrowid,counts)
            db.execute('UPDATE generations SET status=? WHERE id=?',(json.dumps(result),cursor.lastrowid))
            receipt=OwnedZeroGenerationStopReceipt(baseline,cursor.lastrowid)
            db.execute('INSERT OR REPLACE INTO ue_owned_normal_stop(slot,value,completed) VALUES(1,?,0)',(json.dumps(receipt.to_dict()),))
            return result,receipt
