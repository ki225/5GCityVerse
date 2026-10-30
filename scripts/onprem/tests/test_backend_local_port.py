"""Listener selection never claims another process's API port."""
import pytest
from test_native_backend_launcher import launcher, setup


def sockets(monkeypatch, *, forbidden=None):
    bound=[]
    class Socket:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def setsockopt(self,*args):pass
        def bind(self,address):
            assert address[0]=='127.0.0.1'
            assert address[1]!=forbidden, 'Unused API listener must not be probed'
            bound.append(address[1])
    monkeypatch.setattr(launcher.socket,'socket',Socket)
    return bound


def test_default_api_port_remains_8000(monkeypatch,tmp_path):
    args,_,_=setup(monkeypatch,tmp_path)
    bound=sockets(monkeypatch)
    launcher.main(args)
    assert 8000 in bound


def test_custom_api_port_selects_uvicorn_and_does_not_probe_8000(monkeypatch,tmp_path,capsys):
    args,_,_=setup(monkeypatch,tmp_path)
    bound=sockets(monkeypatch,forbidden=8000)
    class Process:
        def __init__(self):self.returncode=None
        def poll(self):return self.returncode
        def terminate(self):self.returncode=0
        def wait(self,timeout=None):return self.returncode
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    commands=[]
    class ApiProcess(Process):
        def poll(self):
            if self.returncode is None:
                self.returncode=0
                raise KeyboardInterrupt
            return self.returncode
    def popen(command,**kwargs):
        commands.append(command)
        return ApiProcess() if 'uvicorn' in command else Process()
    monkeypatch.setattr(launcher.socket,'create_connection',lambda *a,**k:Connection())
    monkeypatch.setattr(launcher.subprocess,'Popen',popen)
    launcher.main([a for a in args if a!='--check-only']+['--backend-local-port','28000'])
    backend=next(cmd for cmd in commands if 'uvicorn' in cmd)
    assert backend[backend.index('--port')+1]=='28000'
    assert 28000 in bound and 8000 not in bound
    assert '127.0.0.1:28000' in capsys.readouterr().out


@pytest.mark.parametrize('port',['0','1023','65536','18081','18080'])
def test_invalid_or_forward_conflicting_api_port_refused(monkeypatch,tmp_path,port):
    args,calls,_=setup(monkeypatch,tmp_path)
    with pytest.raises(SystemExit,match='Backend loopback port'):
        launcher.main(args+['--backend-local-port',port])
    assert calls==[]


def test_capacity_observer_does_not_probe_unused_api_port(monkeypatch,tmp_path):
    args,_,_=setup(monkeypatch,tmp_path)
    bound=sockets(monkeypatch,forbidden=8000)
    launcher.main(args+['--capacity-observe-output',str(tmp_path/'native.json')])
    assert 8000 not in bound
