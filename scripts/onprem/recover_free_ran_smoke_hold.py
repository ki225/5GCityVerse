"""Private exact-held recovery. Native reads only; optional local ledger release.

Run with the same verified launcher environment. Never registers an admission,
creates a UE, or treats HTTP409 as native absence.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from scripts.onprem.free_ran_device_smoke import sealed_json
from backend.onprem.intents.integration import load_configuration
from backend.onprem.intents.pending_admission import FixedOriginSmfAdmissionApi
from backend.onprem.intents.resource_adapters import FixedOriginSessionQerApi,_token_file_reader
from backend.onprem.runtime.kubeadm import configured_driver
from backend.onprem.runtime.ue import GenerationStore,UEController,EvidenceUnavailable,seconds
from backend.onprem.runtime.ue_reservation import HeldUERepository


def recover(controller,admission_api,census_api,expected,*,owner,operation,execute_release=False):
    """OS-lock protected exact CAS, native absence, then local release only."""
    controller.acquire()
    try:
        canonical=lambda value:json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
        repository=HeldUERepository(controller.store)
        held=repository.current()
        if (held is None or canonical(held.to_dict())!=canonical(expected) or held.owner_id!=owner
                or held.operation_id!=operation or held.state!='held' or held.consumed_generation is not None):
            raise EvidenceUnavailable('Exact unconsumed held reservation changed')
        generation=controller.store.current()
        if ((generation is not None and (generation['generation']!=held.baseline_generation
                or generation['allocation'] or any(generation['desired'].values())))
                or generation is None and held.baseline_generation!=0):
            raise EvidenceUnavailable('Held baseline generation changed')
        def verify_zero():
            pods=controller.driver.read_pods().get('items',[])
            if any(p.get('metadata',{}).get('labels',{}).get('cityverse.io/managed-by')=='onprem-ue-controller' for p in pods):
                raise EvidenceUnavailable('Managed native UE Pod exists')
            native=census_api.active_inventory()
            observed=native.get('observed_at')
            observed=observed if type(observed) in (int,float) else seconds(observed)
            if (native.get('source')!='native-smf-active-sessions' or native.get('complete') is not True
                    or native.get('network_scope')!=held.network_scope or native.get('sessions')!=[]
                    or not native.get('smf_boot_epoch') or not -1<=time.time()-observed<=15):
                raise EvidenceUnavailable('Fresh complete native zero census required')
            ran=controller.driver.observe_gnb_contexts()
            if (ran.get('source')!='free-ran-ue-gnb-api-ue-count' or set(ran.get('readouts',{}))!={'embb','urllc','mmtc','v2x'}
                    or any(r.get('ueCount')!=0 or r.get('supis') or not -1<=time.time()-seconds(r.get('observedAt'))<=15 for r in ran['readouts'].values())):
                raise EvidenceUnavailable('Native gNB zero contexts required')
            for row in held.allocation:
                nonce=row.nonce
                if admission_api.inspect(nonce) is not None:
                    raise EvidenceUnavailable('Native admission exists; preserve held reservation')
            return native['smf_boot_epoch']
        epoch=verify_zero()
        if verify_zero()!=epoch:raise EvidenceUnavailable('SMF boot changed during recovery')
        if canonical(repository.current().to_dict())!=canonical(expected):raise EvidenceUnavailable('Held reservation changed during recovery')
        result=repository.release(held) if execute_release else held
        return dict(status='released' if execute_release else 'absence_verified',reservation_id=result.reservation_id,
                    native_writes=False,local_ledger_released=execute_release)
    finally:controller.release()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reservation',type=Path,required=True);parser.add_argument('--reservation-sha256',required=True)
    parser.add_argument('--owner',required=True);parser.add_argument('--operation',required=True)
    parser.add_argument('--smf-origin',required=True);parser.add_argument('--release-local-held',action='store_true')
    args=parser.parse_args()
    expected=sealed_json(args.reservation,args.reservation_sha256)
    config=load_configuration();driver=configured_driver()
    token=_token_file_reader(config['providers']['qos']['token_file'])
    api=FixedOriginSmfAdmissionApi(args.smf_origin,token_reader=token)
    census=FixedOriginSessionQerApi(args.smf_origin,token_reader=token)
    controller=UEController(GenerationStore(Path(os.environ['CITYVERSE_ONPREM_UE_STATE_FILE'])),adapter=object(),driver=driver)
    print(json.dumps(recover(controller,api,census,expected,owner=args.owner,operation=args.operation,execute_release=args.release_local_held)))


if __name__=='__main__':main()
