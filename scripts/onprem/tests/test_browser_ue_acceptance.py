import pytest
from scripts.onprem.run_browser_ue_acceptance import APPLY_SETTINGS_LABEL, exact_ue_payload, main, validate_submission, wait_for_initial_ue_draft


def test_wait_for_initial_status_poll_before_zero_fill():
    from playwright.sync_api import sync_playwright, expect
    before = {'generation': 7, 'slices': {name: {'requested': int(name == 'embb')}
                                          for name in ('embb', 'urllc', 'mmtc', 'v2x')}}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            responses = []
            page.on('console', lambda item: responses.append(item) if item.text == 'ui-status-get-complete' else None)
            page.set_content('''<p role="status">Generation 6 · stopped</p>
                <input id="ue-embb" value="0"><input id="ue-urllc" value="0">
                <input id="ue-mmtc" value="0"><input id="ue-v2x" value="0">
                <script>setTimeout(() => {
                  document.querySelector('[role=status]').textContent = 'Generation 7 · established';
                  document.querySelector('#ue-embb').value = '1';
                  console.log('ui-status-get-complete');
                }, 100)</script>''')
            wait_for_initial_ue_draft(page, before, expect, responses)
            page.locator('#ue-embb').fill('0')
            expect(page.locator('#ue-embb')).to_have_value('0')
        finally:
            browser.close()


def test_zero_draft_waits_for_ui_get_even_when_default_inputs_already_zero():
    from playwright.sync_api import sync_playwright, expect
    before = {'generation': 7, 'slices': {name: {'requested': 0}
                                          for name in ('embb', 'urllc', 'mmtc', 'v2x')}}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            responses = []
            page.on('console', lambda item: responses.append(item) if item.text == 'ui-status-get-complete' else None)
            page.set_content('''<p role="status">Generation 7 · stopped</p>
                <input id="ue-embb" value="0"><input id="ue-urllc" value="0">
                <input id="ue-mmtc" value="0"><input id="ue-v2x" value="0">
                <script>setTimeout(() => console.log('ui-status-get-complete'), 150)</script>''')
            started = page.evaluate('performance.now()')
            wait_for_initial_ue_draft(page, before, expect, responses)
            assert page.evaluate('performance.now()') - started >= 100
        finally:
            browser.close()


def test_apply_settings_locator_matches_current_ui_only():
    assert APPLY_SETTINGS_LABEL.fullmatch('Apply persistent UE settings')
    assert APPLY_SETTINGS_LABEL.fullmatch('套用常駐 UE 設定')
    assert APPLY_SETTINGS_LABEL.fullmatch('Apply UE counts') is None


def test_ui_request_payload_must_match_owned_zero_exactly():
    zero=dict(embb=0,urllc=0,mmtc=0,v2x=0)
    assert exact_ue_payload(zero,zero)
    assert not exact_ue_payload(dict(embb=1,urllc=0,mmtc=0,v2x=0),zero)
    assert not exact_ue_payload(dict(zero,extra=0),zero)
    assert not exact_ue_payload(dict(zero,embb=False),zero)

DESIRED=dict(embb=13,urllc=13,mmtc=12,v2x=12)

def test_actual_async_api_202_contract():
    assert validate_submission(202,DESIRED,DESIRED,{'generation':4},3)==4

@pytest.mark.parametrize('status,counts,generation',[(200,DESIRED,4),(409,DESIRED,4),(202,dict(embb=50,urllc=0,mmtc=0,v2x=0),4),(202,DESIRED,3),(202,DESIRED,True)])
def test_invalid_submission_cannot_start_observation(status,counts,generation):
    with pytest.raises(RuntimeError): validate_submission(status,counts,DESIRED,{'generation':generation},3)

@pytest.mark.parametrize('args',[
    ['--ui-url','http://example.com'],['--api-url','https://127.0.0.1'],
    ['--timeout','0'],['--execute','--resume-generation','4'],
    ['--set-zero','--execute'],['--set-zero','--resume-generation','4'],
])
def test_unsafe_arguments_rejected_before_browser_or_output(args,tmp_path):
    target=tmp_path/'result'
    with pytest.raises(SystemExit):main(['--output',str(target),*args])
    assert not target.exists()
