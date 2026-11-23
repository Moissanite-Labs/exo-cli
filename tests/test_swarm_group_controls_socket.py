"""REAL-socket integration for ``exo swarm-group`` lifecycle commands against a real group_launch/GroupHost.

Covers three cases: ``status GROUP --socket``, a sequential group-wide pause/resume/stop, and the same
controls while one offline provider call is held pending (status, pause and resume answer with the call
still in flight, and stop cancels it). The tests use the same fixture pattern as
``test_swarm_socket_integration``. That means they need a swarm core on ``PYTHONPATH`` and skip
otherwise. Only the provider transport is substituted (offline RuleFake).

Limits: one fixed config (mode ``socratic``, two tasks, group-wide only). No ``--member`` control runs
against the real core, and nothing proves a member completes work after a resume. The held provider
call is released only after ``stop``.
"""
import asyncio
import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

group_launch = pytest.importorskip('exo.group_launch')
group_commands = pytest.importorskip('exo.core.group_commands')
plan_exec = pytest.importorskip('exo.core.group_plan_execution')
contract = pytest.importorskip('exo.core.group_task_contract')

BOUND = 10
CLI_SRC = Path(__file__).resolve().parents[1] / 'src' / 'exo'
PROFILES = ['builder', 'checker']
OWNER = 'owner'
MODELS = {'fake': {'rules': [{'match': 'MARK-B1', 'steps': ['B1-REPLY']},
                             {'match': 'MARK-C1', 'steps': ['C1-REPLY']}]},
          'roles': {'main': ['fake:x']}}


@pytest.fixture
def home(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix='exs', dir='/tmp'))      # short: AF_UNIX paths cap at ~104 bytes
    home = root / 'h'
    (home / 'seats').mkdir(parents=True)
    for name in (*PROFILES, OWNER):
        (home / 'seats' / f'{name}.toml').write_text(f'name = "{name}"\n', encoding='utf-8')
    (home / 'config.json').write_text(json.dumps({'models': MODELS}), encoding='utf-8')
    monkeypatch.setenv('EXO_HOME', str(home))
    monkeypatch.setenv('EXO_NO_KEYCHAIN', '1')
    try:
        yield home
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def cli(monkeypatch):
    import exo
    if (CLI_SRC / 'group_launch.py').exists() or Path(exo.__file__).resolve().parent == CLI_SRC:
        pytest.skip('exo.* resolves to the CLI checkout, not a swarm core')
    for name in ('swarm_cmd', 'swarm_plan_cmd', 'cli_under_test'):
        monkeypatch.delitem(sys.modules, f'exo.{name}', raising=False)
    monkeypatch.setattr(exo, '__path__', [*exo.__path__, str(CLI_SRC)])  # core first, CLI fills the gaps
    spec = importlib.util.spec_from_file_location('exo.cli_under_test', CLI_SRC / 'cli.py')
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'exo.cli_under_test', mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def rig(home, cli, monkeypatch):
    """Real create and real start/serve. Only the provider text is fake.

    ``create`` is wrapped so the test can seed a foreign group/plan and watch the close.
    """
    from exo.core.models.fake import RuleFake

    rec = types.SimpleNamespace(launch=None, prompts=[], closed=False, foreign_gid=None, foreign_plan=None)
    real_create, original_stream = group_launch.create, RuleFake.stream

    def create(*args, **kw):
        launch = rec.launch = real_create(*args, **kw)
        real_aclose = launch.aclose

        async def aclose():
            await real_aclose()
            rec.closed = True
        launch.aclose = aclose        # observe the close only. Behaviour is the real one.
        return launch

    async def stream(this, messages, tools, **opts):
        rec.prompts.append(next((m.text() for m in reversed(messages) if m.role == 'user'), ''))
        async for event in original_stream(this, messages, tools, **opts):
            yield event

    monkeypatch.setattr(group_launch, 'create', create)
    monkeypatch.setattr(RuleFake, 'stream', stream)
    return rec


def write_plan(tmp):
    plan = {'schema': contract.SCHEMA_VERSION, 'tasks': [
        {'task_id': 'b1', 'role': 'builder', 'goal': 'MARK-B1 write it', 'profile': 'builder',
         'acceptance': ['done']},
        {'task_id': 'c1', 'role': 'builder', 'goal': 'MARK-C1 check it', 'profile': 'checker',
         'acceptance': ['done']}]}
    grants = {'schema': 'exo.swarm.cli-grants.v1', 'profiles': PROFILES, 'permits': {'builder': ['read_repo']},
              'write_roots': {}, 'reviewer_profiles': ['checker'], 'routes': {'backend': 'default'}}
    p, g = tmp / 'plan.json', tmp / 'grants.json'
    p.write_text(json.dumps(plan), encoding='utf-8')
    g.write_text(json.dumps(grants), encoding='utf-8')
    return ['--plan', str(p), '--grants', str(g)]


def run_cli(cli, *argv):
    """One real ``cli.main`` call on the worker thread: (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def test_swarm_group_status_over_real_socket(home, rig, cli):
    sock = home / 'run' / 'exod.sock'
    sock.parent.mkdir(parents=True)
    ev = types.SimpleNamespace(done=False, error=None, res=None, gid=None, lock=threading.Lock())

    def client_side():
        deadline = time.monotonic() + BOUND
        try:
            while (rig.launch is None or not sock.exists()) and time.monotonic() < deadline:
                time.sleep(0.02)
            ev.gid = rig.launch.group.id
            while True:
                ev.res = run_cli(cli, 'swarm-group', 'status', ev.gid, '--socket', str(sock))
                if ev.res[0] == 0 or time.monotonic() > deadline:
                    break
                time.sleep(0.05)
        except BaseException as exc:  # noqa: BLE001
            ev.error = exc
        finally:
            with ev.lock:
                if not ev.done:
                    os.kill(os.getpid(), signal.SIGINT)

    worker = threading.Thread(target=client_side, daemon=True)
    argv = ['swarm', '--profiles', ','.join(PROFILES), '--seat', OWNER, '--mode', 'socratic', '--start',
            '--socket', str(sock), *write_plan(home.parent)]
    worker.start()
    try:
        rc = cli.main(argv)
    finally:
        with ev.lock:
            ev.done = True
        worker.join(BOUND + 5)
    assert not worker.is_alive()
    if ev.error is not None:
        raise ev.error
    assert rc == 0 and rig.closed
    rc_s, out_s, err_s = ev.res
    assert rc_s == 0, (out_s, err_s)
    assert ev.gid in out_s and OWNER not in err_s and 'Traceback' not in err_s
    assert 'builder' in out_s and 'checker' in out_s


def test_swarm_group_pause_resume_stop_over_real_socket(home, rig, cli):
    sock = home / 'run' / 'exod.sock'
    sock.parent.mkdir(parents=True)
    ev = types.SimpleNamespace(done=False, error=None, steps=[], gid=None, closed_during=None, lock=threading.Lock())

    def call(deadline, *argv):
        """Real CLI call. It retries only while the socket is not yet serving (rc != 0), bounded."""
        while True:
            res = run_cli(cli, 'swarm-group', *argv, ev.gid, '--socket', str(sock))
            if res[0] == 0 or time.monotonic() > deadline:
                return res
            time.sleep(0.05)

    def client_side():
        deadline = time.monotonic() + BOUND
        try:
            while (rig.launch is None or not sock.exists()) and time.monotonic() < deadline:
                time.sleep(0.02)
            ev.gid = rig.launch.group.id
            for op in ('status', 'pause', 'status', 'resume', 'status', 'stop', 'status'):
                ev.steps.append((op, call(deadline, op)))
            ev.closed_during = rig.closed
        except BaseException as exc:  # noqa: BLE001
            ev.error = exc
        finally:
            with ev.lock:
                if not ev.done:
                    os.kill(os.getpid(), signal.SIGINT)

    worker = threading.Thread(target=client_side, daemon=True)
    argv = ['swarm', '--profiles', ','.join(PROFILES), '--seat', OWNER, '--mode', 'socratic', '--start',
            '--socket', str(sock), *write_plan(home.parent)]
    worker.start()
    try:
        rc = cli.main(argv)
    finally:
        with ev.lock:
            ev.done = True
        worker.join(BOUND + 5)
    assert not worker.is_alive()
    if ev.error is not None:
        raise ev.error
    assert rc == 0 and rig.closed
    assert ev.closed_during is False                   # launch kept serving until the fixture shutdown
    assert [op for op, _ in ev.steps] == ['status', 'pause', 'status', 'resume', 'status', 'stop', 'status']
    parsed = {}
    for i, (op, (rc_o, out_o, err_o)) in enumerate(ev.steps):
        assert rc_o == 0 and 'Traceback' not in err_o, (op, out_o, err_o)
        parsed[i] = json.loads(out_o)
        assert parsed[i]['id'] == ev.gid
    for i, key in ((1, 'paused'), (3, 'resumed'), (5, 'stopped')):
        assert parsed[i] == {'id': ev.gid, key: True, 'member': None}
    for i in (0, 2, 4, 6):
        assert parsed[i]['local_runtime'] is True and parsed[i]['owner'] == OWNER
        assert sorted(m['profile'] for m in parsed[i]['members']) == sorted(PROFILES)
        # offline members finish at once: readback after each op is the settled truth, not a faked pause
        assert {m['state'] for m in parsed[i]['members']} == {'completed'}
        assert parsed[i]['receipt']['state'] == 'settled' and parsed[i]['reconciliation_required'] is False


def test_swarm_group_controls_while_provider_turn_is_held_over_real_socket(home, rig, cli, monkeypatch):
    """Active-turn proof: status/pause/resume/stop over the real socket while the provider call is PENDING.

    The offline provider transport blocks on an ``asyncio.Event`` (created on the launch's loop). Documented core
    semantics: pause only clears the member gate. A turn in flight is not cancelled, and status reads ``pausing``.
    resume re-sets it, stop cancels the member task (the held provider call receives CancelledError).
    """
    from exo.core.models.fake import RuleFake

    sock = home / 'run' / 'exod.sock'
    sock.parent.mkdir(parents=True)
    held = types.SimpleNamespace(loop=None, gate=None, entered=threading.Event(), active=0, started=0,
                                 finished=0, cancelled=0, lock=threading.Lock())
    rig_stream = RuleFake.stream                       # the rig's recording stream (itself wrapping RuleFake)

    async def held_stream(this, messages, tools, **opts):
        if held.gate is None:
            held.loop, held.gate = asyncio.get_running_loop(), asyncio.Event()
        with held.lock:
            held.started += 1
            held.active += 1
        held.entered.set()
        try:
            await held.gate.wait()                     # provider call pending until released
            async for event in rig_stream(this, messages, tools, **opts):
                yield event
            with held.lock:
                held.finished += 1
        except asyncio.CancelledError:
            with held.lock:
                held.cancelled += 1
            raise
        finally:
            with held.lock:
                held.active -= 1

    monkeypatch.setattr(RuleFake, 'stream', held_stream)

    def release():
        if held.loop is not None and held.gate is not None:
            with contextlib.suppress(RuntimeError):    # loop already closed
                held.loop.call_soon_threadsafe(held.gate.set)

    ev = types.SimpleNamespace(done=False, error=None, steps=[], gid=None, snap=[], lock=threading.Lock())

    def call(op):
        t0 = time.monotonic()
        res = run_cli(cli, 'swarm-group', op, ev.gid, '--socket', str(sock))
        elapsed = time.monotonic() - t0
        with held.lock:
            snap = (held.started, held.active, held.cancelled, held.finished)
        ev.steps.append((op, res, elapsed, snap))
        return res

    def wait_for(pred):
        deadline = time.monotonic() + BOUND
        while not pred() and time.monotonic() < deadline:
            time.sleep(0.02)
        return pred()

    def client_side():
        try:
            deadline = time.monotonic() + BOUND
            while (rig.launch is None or not sock.exists()) and time.monotonic() < deadline:
                time.sleep(0.02)
            ev.gid = rig.launch.group.id
            assert held.entered.wait(BOUND), 'provider turn never became pending'
            assert wait_for(lambda: call('status')[0] == 0)          # socket serving while provider is held
            ev.steps.clear()
            for op in ('status', 'pause', 'status', 'resume', 'status'):
                call(op)
            ev.snap.append(('before_stop_released', held.gate.is_set()))
            call('stop')
            ev.snap.append(('cancelled_seen', wait_for(lambda: held.cancelled >= 1 and held.active == 0)))
            call('status')
        except BaseException as exc:  # noqa: BLE001
            ev.error = exc
        finally:
            release()                                  # always free the provider before the launch is torn down
            with ev.lock:
                if not ev.done:
                    os.kill(os.getpid(), signal.SIGINT)

    worker = threading.Thread(target=client_side, daemon=True)
    argv = ['swarm', '--profiles', ','.join(PROFILES), '--seat', OWNER, '--mode', 'socratic', '--start',
            '--socket', str(sock), *write_plan(home.parent)]
    worker.start()
    try:
        rc = cli.main(argv)
    finally:
        with ev.lock:
            ev.done = True
        release()
        worker.join(BOUND + 5)
    assert not worker.is_alive()
    if ev.error is not None:
        raise ev.error
    assert rc == 0 and rig.closed

    assert [op for op, *_ in ev.steps] == ['status', 'pause', 'status', 'resume', 'status', 'stop', 'status']
    assert ev.snap[0] == ('before_stop_released', False)           # provider was still held across the controls
    parsed = []
    for op, (rc_o, out_o, err_o), elapsed, snap in ev.steps:
        assert rc_o == 0 and 'Traceback' not in err_o, (op, out_o, err_o)
        assert elapsed <= BOUND, (op, elapsed)                     # returned while the provider was still pending
        parsed.append(json.loads(out_o))
        assert parsed[-1]['id'] == ev.gid
    # status/pause/resume happened with a provider call pending and NOT cancelled (pause does not cancel it)
    for i in range(5):
        started, active, cancelled, finished = ev.steps[i][3]
        assert started >= 1 and active >= 1 and cancelled == 0 and finished == 0, (ev.steps[i][0], ev.steps[i][3])
    assert parsed[1] == {'id': ev.gid, 'paused': True, 'member': None}
    assert parsed[3] == {'id': ev.gid, 'resumed': True, 'member': None}
    assert parsed[5] == {'id': ev.gid, 'stopped': True, 'member': None}
    pre = parsed[0]
    assert pre['local_runtime'] is True and pre['receipt']['state'] == 'active'     # not settled: turn in flight
    assert not {m['state'] for m in pre['members']} & {'completed', 'stopped', 'failed'}
    paused_states = {m['state'] for m in parsed[2]['members']}
    assert paused_states == {'pausing'}                            # gate closed, in-flight turn not at a checkpoint
    assert not {m['state'] for m in parsed[4]['members']} & {'pausing', 'paused', 'completed', 'stopped'}
    # stop has the documented effect: member tasks cancelled -> the held provider call gets CancelledError
    assert ev.snap[1] == ('cancelled_seen', True)
    assert held.cancelled >= 1 and held.finished == 0
    final = parsed[6]
    assert {m['state'] for m in final['members']} == {'stopped'}
