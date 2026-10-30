"""Real, non-mocked browser commissioning for the four-slice 50-UE scenario.

Default mode only inspects the running UI. --execute submits exactly one UI
intent after an operator has verified old-scope cleanup and native readiness.
This is positive commissioning, NOT a complete zero-inventory acceptance gate.
Requires Python Playwright and its Chromium browser. Servers stay operator-owned.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.onprem.run_ue_acceptance import evidence_errors


APPLY_SETTINGS_LABEL = re.compile(r'^(Apply persistent UE settings|套用常駐 UE 設定)$')


def exact_ue_payload(payload, desired):
    return isinstance(payload, dict) and payload == desired and all(type(value) is int for value in payload.values())


def validate_submission(http_status, submitted, desired, intent, previous_generation):
    if http_status != 202 or submitted != desired:
        raise RuntimeError('UI intent failed or did not match50 UE distribution')
    generation=intent.get('generation')
    if type(generation) is not int or generation <= previous_generation:
        raise RuntimeError('A new commissioning generation is required')
    return generation


def wait_for_initial_ue_draft(page, before, expect, ui_status_responses):
    """Wait for the first UI status poll to initialize its editable draft."""
    deadline = time.monotonic() + 20
    while not ui_status_responses:
        if time.monotonic() >= deadline:
            raise RuntimeError('UI initial UE status GET did not complete')
        page.wait_for_timeout(100)
    generation = before['generation']
    expect(page.locator('[role="status"]').filter(has_text=f'Generation {generation} ·')).to_be_visible(timeout=20000)
    for name in ('embb', 'urllc', 'mmtc', 'v2x'):
        expect(page.locator('#ue-'+name)).to_have_value(str(before['slices'][name]['requested']), timeout=20000)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ui-url',default='http://127.0.0.1:5173')
    parser.add_argument('--api-url',default='http://127.0.0.1:8000')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--set-zero',action='store_true',help='Submit zero via UI only; does not claim complete SMF inventory or stopped proof')
    parser.add_argument('--resume-generation',type=int,help='Observe an already-submitted generation without another UI POST')
    parser.add_argument('--timeout',type=int,default=900)
    args=parser.parse_args(argv)
    if sum((args.execute,args.set_zero,args.resume_generation is not None))>1: parser.error('execute, set-zero and resume-generation are mutually exclusive')
    for value in (args.ui_url,args.api_url):
        if urlsplit(value).scheme!='http' or urlsplit(value).hostname not in {'127.0.0.1','localhost'}:
            parser.error('This local commissioning helper only permits loopback HTTP URLs')
    if not 120<=args.timeout<=1800: parser.error('timeout must be120..1800 seconds')
    from playwright.sync_api import sync_playwright, expect, TimeoutError as PlaywrightTimeoutError
    args.output.mkdir(parents=True,exist_ok=False)
    events=[]
    ui_status_responses=[]
    desired=dict(embb=0,urllc=0,mmtc=0,v2x=0) if args.set_zero else dict(embb=13,urllc=13,mmtc=12,v2x=12)
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1100})
        page.on('console',lambda item:events.append({'kind':'console','type':item.type,'text':item.text}))
        page.on('pageerror',lambda item:events.append({'kind':'pageerror','text':str(item)}))
        def record_response(item):
            if '/api/' in item.url:
                events.append({'kind':'response','url':item.url,'status':item.status})
            if item.url == args.api_url+'/api/onprem/ue' and item.request.method == 'GET' and item.status == 200:
                ui_status_responses.append(item)
        page.on('response',record_response)
        try:
            page.goto(args.ui_url)
            page.wait_for_load_state('networkidle')
            language=page.get_by_role('button',name=re.compile('English'))
            if language.count(): language.click();page.wait_for_load_state('networkidle')
            page.screenshot(path=str(args.output/'before.png'),full_page=True)
            (args.output/'before.html').write_text(page.content(),encoding='utf-8')
            if not args.execute and not args.set_zero and args.resume_generation is None:
                print('Read-only browser inspection saved; no UE intent submitted.')
                return
            before=page.request.get(args.api_url+'/api/onprem/ue').json()
            health=page.request.get(args.api_url+'/health').json()
            if health.get('runtimeSelector')!='kubeadm' or (not args.set_zero and health.get('kubernetes',{}).get('mutationsEnabled') is not True):
                raise RuntimeError('Explicit kubeadm mutation-enabled runtime is required')
            if args.execute or args.set_zero:
                wait_for_initial_ue_draft(page, before, expect, ui_status_responses)
                for name,value in desired.items(): page.locator('#ue-'+name).fill(str(value))
                for name,value in desired.items(): expect(page.locator('#ue-'+name)).to_have_value(str(value))
                expect(page.locator('#ue-total')).to_contain_text(re.compile(rf'(Desired total|期望總數): {sum(desired.values())} /'))
                blocked=[]
                def guard_ue_post(route):
                    if route.request.method=='POST' and not exact_ue_payload(route.request.post_data_json,desired):
                        blocked.append(route.request.post_data_json)
                        (args.output/'blocked-ui-intent.json').write_text(json.dumps({'desired':desired,'blocked':blocked},indent=2),encoding='utf-8')
                        route.abort()
                    else: route.continue_()
                page.route(re.compile(r'/api/onprem/ue$'),guard_ue_post)
                try:
                    with page.expect_response(lambda item:item.url==args.api_url+'/api/onprem/ue' and item.request.method=='POST') as pending:
                        page.get_by_role('button',name=APPLY_SETTINGS_LABEL).click()
                except PlaywrightTimeoutError:
                    if blocked: raise RuntimeError('UI attempted a mismatched UE payload; request blocked before backend') from None
                    raise
                response=pending.value
                intent=response.json();generation=intent.get('generation')
                (args.output/'intent.json').write_text(json.dumps({'before':before,'desired':desired,'intent':intent,'httpStatus':response.status,'request':response.request.post_data_json},indent=2),encoding='utf-8')
                generation=validate_submission(response.status,response.request.post_data_json,desired,intent,before['generation'])
                if args.set_zero:
                    page.screenshot(path=str(args.output/'zero-requested.png'),full_page=True)
                    print(f'Zero intent accepted as generation{generation}; no complete-zero proof inferred.')
                    return
            else:
                generation=args.resume_generation
                if before.get('generation')!=generation or any(before['slices'][name]['requested']!=count for name,count in desired.items()): raise RuntimeError('Resume generation or50 UE distribution mismatch')
                (args.output/'resume.json').write_text(json.dumps({'generation':generation,'before':before,'noNewIntent':True},indent=2),encoding='utf-8')
            deadline=time.monotonic()+args.timeout;stable=None;observations=set();sequence=0
            while time.monotonic()<deadline:
                status=page.request.get(args.api_url+'/api/onprem/ue').json();sequence+=1
                if status.get('generation')!=generation: raise RuntimeError('Intent superseded; no further mutations')
                errors=evidence_errors(status,desired,generation)
                (args.output/f'sample-{sequence:05d}.json').write_text(json.dumps({'capturedAt':time.time(),'status':status,'validationErrors':errors},indent=2),encoding='utf-8')
                print(json.dumps({'generation':generation,'status':status.get('status'),'registered':status.get('registered'),'pdu':status.get('pduEstablished'),'traffic':status.get('trafficVerified'),'validationErrors':len(errors)}),flush=True)
                if errors:stable=None;observations.clear()
                else:
                    stable=stable or time.monotonic();observations.add(status['observedAt'])
                    if time.monotonic()-stable>=60 and len(observations)>=5:break
                page.wait_for_timeout(2000)
            else:raise RuntimeError('50 UE did not achieve60 seconds of fresh per-UE evidence')
            expect(page.get_by_text(f'Generation {generation} · established',exact=True)).to_be_visible(timeout=15000)
            expect(page.get_by_role('row',name=re.compile(r'^Total\b'))).to_have_text(re.compile(r'^Total\s*50\s*50\s*50\s*50\s*50\s*0$'),timeout=15000)
            page.screenshot(path=str(args.output/'established.png'),full_page=True)
            if any(item['kind']=='pageerror' for item in events):raise RuntimeError('Browser JavaScript error recorded')
            print('PASS: real UI intent and50 per-UE native proof records continuously60 seconds. Workload remains running.')
        finally:
            (args.output/'network-console.json').write_text(json.dumps(events,indent=2),encoding='utf-8')
            browser.close()


if __name__=='__main__':main()
