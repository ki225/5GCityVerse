"""Actual launcher child-env seam with explicit subprocess/native doubles."""
import os
from types import SimpleNamespace

import pytest

from scripts.onprem.tests.test_native_backend_launcher import launcher, setup


KEYS = ('CITYVERSE_ONPREM_UE_BATCH_INTERVAL', 'CITYVERSE_ONPREM_TYPED_UE_LAUNCH_BURST',
        'CITYVERSE_ONPREM_TYPED_UE_MAX_PENDING')


@pytest.mark.parametrize('flags,inherited,expected', [
    ([], {}, ('30','1','8')),
    ([], dict(zip(KEYS, ('2.5','5','10'))), ('2.5','5','10')),
    (['--ue-batch-interval-seconds','2', '--typed-ue-launch-burst','4', '--typed-ue-max-pending','12'],
     dict(zip(KEYS, ('30','1','8'))), ('2','4','12')),
])
def test_spawned_backend_receives_exact_pacing_and_computes_same_runtime_seal(monkeypatch, tmp_path, flags, inherited, expected):
    args, _, _ = setup(monkeypatch, tmp_path)
    for key in KEYS:monkeypatch.delenv(key, raising=False)
    for key,value in inherited.items():monkeypatch.setenv(key,value)
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*_):pass
    class Process:
        def poll(self):return None
        def terminate(self):pass
        def wait(self,timeout=None):return 0
    captured=[]
    def spawn(command, **kwargs):
        captured.append((command, dict(kwargs['env'])))
        if 'uvicorn' in command:raise KeyboardInterrupt
        return Process()
    monkeypatch.setattr(launcher.socket, 'create_connection', lambda *_args,**_kwargs:Connection())
    monkeypatch.setattr(launcher.subprocess, 'Popen', spawn)
    launcher.main([arg for arg in args if arg != '--check-only'] + ['--enable-mutations'] + flags)
    assert 'uvicorn' in captured[-1][0]
    assert all(tuple(env[key] for key in KEYS) == expected for _,env in captured)
    environment = captured[-1][1]
    # The real configured_driver constructor is pure here: transport is not
    # called, and no controller is created/acquired or mutation requested.
    from backend.onprem.runtime.kubeadm import configured_driver
    from backend.onprem.runtime.free_ran_ue import FreeRanKubeadmDriver
    monkeypatch.setattr(os, 'environ', environment)
    driver = configured_driver()
    assert (driver.batch_interval, driver.typed_launch_burst, driver.typed_launch_max_pending) == (float(expected[0]),int(expected[1]),int(expected[2]))
    equivalent = FreeRanKubeadmDriver(driver.context, namespace=driver.namespace,
        image=driver.image, init_image=driver.init_image, traffic_enabled=driver.traffic_enabled,
        batch_interval=float(expected[0]), typed_launch_burst=int(expected[1]), typed_launch_max_pending=int(expected[2]))
    assert driver.runtime_policy_digest == equivalent.runtime_policy_digest


@pytest.mark.parametrize('flags', [
    ['--ue-batch-interval-seconds','1.99'], ['--ue-batch-interval-seconds','nan'],
    ['--ue-batch-interval-seconds','inf'], ['--typed-ue-launch-burst','0'],
    ['--typed-ue-launch-burst','11'], ['--typed-ue-max-pending','21'],
    ['--typed-ue-launch-burst','10','--typed-ue-max-pending','9'],
])
def test_invalid_pacing_stops_before_native_reads_or_child_process(monkeypatch,tmp_path,flags):
    args,calls,_ = setup(monkeypatch,tmp_path)
    with pytest.raises(SystemExit):launcher.main(args+flags)
    assert calls == []


def test_invalid_inherited_pacing_does_not_become_a_silent_default():
    for key,value in ((KEYS[0],'not-a-clock'),(KEYS[1],'1.5'),(KEYS[2],'0')):
        with pytest.raises(SystemExit):launcher.launch_pacing_environment(SimpleNamespace(),{key:value})
