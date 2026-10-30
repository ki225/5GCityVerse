"""Read-only real-browser smoke for the shared on-prem scenario UI.

Inspects the actual HTTP snapshot and rendered controls; never submits a UE or
policy request and never mocks routes. Servers/forwards remain operator-owned.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import urlopen


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ui-url', default='http://127.0.0.1:5173')
    parser.add_argument('--api-url', default='http://127.0.0.1:8000')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    for value in (args.ui_url, args.api_url):
        if urlsplit(value).scheme != 'http' or urlsplit(value).hostname not in {'127.0.0.1', 'localhost'}:
            parser.error('Only loopback HTTP servers are permitted')
    from playwright.sync_api import sync_playwright, expect
    args.output.mkdir(parents=True, exist_ok=False)
    with urlopen(args.api_url + '/api/onprem/scenario-rounds/current', timeout=10) as response:
        snapshot = json.load(response)
    (args.output / 'snapshot.json').write_text(json.dumps(snapshot, indent=2), encoding='utf-8')
    if snapshot['capabilities']['start'] is not False:
        raise RuntimeError('This smoke checks the capability-blocked stage only; no intent submitted')
    observations = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1560, 'height': 1200}, reduced_motion='reduce')
        page.on('console', lambda item: observations.append({'kind': 'console', 'type': item.type, 'text': item.text}))
        page.on('pageerror', lambda item: observations.append({'kind': 'pageerror', 'text': str(item)}))
        page.on('request', lambda item: observations.append({'kind': 'request', 'method': item.method, 'url': item.url}) if '/api/' in item.url else None)
        try:
            page.goto(args.ui_url)
            page.wait_for_load_state('networkidle')
            (args.output / 'initial.html').write_text(page.content(), encoding='utf-8')
            language = page.get_by_role('button', name=re.compile('English'))
            if language.count():
                language.click()
                page.wait_for_load_state('networkidle')
            # A released historical round may correctly open the Outcomes step.
            # Navigate through the same enabled control a user would use; never
            # assert against or manipulate the mounted-but-hidden setup form.
            journey = page.get_by_role('navigation', name='Experiment steps')
            scenario_step = journey.get_by_role('button', name=re.compile(r'^1\s*Scenario$'))
            expect(scenario_step).to_be_enabled()
            scenario_step.click()
            console = page.locator('[data-testid="onprem-scenario-console"]:visible')
            expect(console).to_have_count(1)
            expect(console.locator('#scenario-baseline-ues')).to_be_visible()
            expect(console.get_by_role('button', name='Submit actual UE scenarios')).to_be_disabled()
            expect(page.get_by_text(snapshot['capabilities']['reason'], exact=True).first).to_be_visible()
            expect(page.locator('[data-testid="scenario-flow-particle"]')).to_have_count(0)
            page.screenshot(path=str(args.output / 'idle.png'), full_page=True)
            console.locator('#scenario-baseline-ues').fill('50')
            expect(console.locator('#scenario-budget')).to_contain_text('Requested round participants 50 / 50')
            console.get_by_label('Include AR Concert', exact=True).check()
            console.locator('#scenario-ues-video_streaming').fill('1')
            expect(console.locator('#scenario-budget')).to_contain_text('must total at most 50')
            expect(console.get_by_role('button', name='Submit actual UE scenarios')).to_be_disabled()
            page.screenshot(path=str(args.output / 'budget-rejected.png'), full_page=True)
            console.locator('#scenario-baseline-ues').fill('0')
            console.locator('#scenario-ues-video_streaming').fill('0')
            console.get_by_label('Include AR Concert', exact=True).uncheck()
            console.get_by_role('radio', name=re.compile('AI dynamic policy')).check()
            expect(console.get_by_role('button', name='Submit actual UE scenarios')).to_be_disabled()
            page.screenshot(path=str(args.output / 'ai-unavailable.png'), full_page=True)
            errors = [row for row in observations if row['kind'] == 'pageerror' or (row['kind'] == 'console' and row['type'] == 'error')]
            writes = [row for row in observations if row['kind'] == 'request' and row['method'] not in {'GET', 'HEAD', 'OPTIONS'}]
            if errors or writes:
                raise RuntimeError(f'Read-only browser smoke failed: errors={len(errors)}, mutationRequests={len(writes)}')
            print('PASS: shared UI, 50+1 budget rejection, unsupported AI, no traffic animation, no console errors, no mutation requests.')
        finally:
            (args.output / 'network-console.json').write_text(json.dumps(observations, indent=2), encoding='utf-8')
            browser.close()


if __name__ == '__main__':
    main()
