"""REAL-socket ``exo swarm-group pause|resume|stop --member`` against a real group_launch/GroupHost.

Closes two documented gaps of ``test_swarm_group_controls_socket``. (1) ``--member`` controls run against the real
core and touch ONLY the targeted member. (2) Provider completion after a resume is actually observed.

Semantics checked first in the core (``group_runtime``): ``pause`` only clears that member's gate, so a turn already
in flight is NOT cancelled and the member reads ``pausing``. ``resume`` re-sets the gate. ``stop`` cancels only the
targeted member task (its pending provider call gets CancelledError).

Fixture pattern is the one of ``test_swarm_group_controls_socket`` / ``test_swarm_socket_integration``: needs a swarm
core on PYTHONPATH (skips otherwise), real ``cli.main`` -> launch -> serve -> protocol Client. Only the provider
TRANSPORT is wrapped: each member's call blocks on its own ``asyncio.Event`` (keyed by the plan marker in the prompt),
then the real offline RuleFake answers. No host/runtime state is faked. Hermetic: temp EXO_HOME, EXO_NO_KEYCHAIN=1,
no network, every wait <= BOUND seconds.

Limits: one config (socratic, two independent tasks, builder/checker). The pause is applied while a turn is in
flight, so "pause blocks the NEXT turn" (pause before a turn starts) is not covered.
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
MARKS = {'builder': 'MARK-B1', 'checker': 'MARK-C1'}
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
    """Real create and real start/serve. The provider transport holds each member's call until released."""
    from exo.core.models.fake import RuleFake

    rec = types.SimpleNamespace(launch=None, closed=False, loop=None, gates={}, lock=threading.Lock(),
                                entered={p: threading.Event() for p in PROFILES},
                                n={p: dict(started=0, active=0, finished=0, cancelled=0) for p in PROFILES})
    real_create, original_stream = group_launch.create, RuleFake.stream

    def create(*args, **kw):
        launch = rec.launch = real_create(*args, **kw)
        real_aclose = launch.aclose

        async def aclose():
            await real_aclose()
            rec.closed = True
        launch.aclose = aclose        # observe the close only. Behaviour is the real one.
        return launch

    def gate(profile):                # only ever called on the launch's loop
        return rec.gates.setdefault(profile, asyncio.Event())

    async def stream(this, messages, tools, **opts):
        text = next((m.text() for m in reversed(messages) if m.role == 'user'), '')
        profile = next((p for p, mark in MARKS.items() if mark in text), None)
        assert profile is not None, text
        rec.loop = asyncio.get_running_loop()
        with rec.lock:
            rec.n[profile]['started'] += 1
            rec.n[profile]['active'] += 1
        rec.entered[profile].set()
        cancelled, yielded = False, 0
        try:
            await gate(profile).wait()                 # provider call pending until released
            async for event in original_stream(this, messages, tools, **opts):
                yielded += 1
                yield event
        except asyncio.CancelledError:
            cancelled = True
            with rec.lock:
                rec.n[profile]['cancelled'] += 1
            raise
        finally:
            with rec.lock:
                # the engine may close the stream (GeneratorExit) right after the last event: that is completion
                # as long as the released provider produced events and nothing cancelled the call
                rec.n[profile]['finished'] += int(yielded > 0 and not cancelled)
                rec.n[profile]['active'] -= 1

    def release(profile):
        if rec.loop is not None:
            with contextlib.suppress(RuntimeError):    # loop already closed
                rec.loop.call_soon_threadsafe(gate(profile).set)

    rec.release = release
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


def drive(home, rig, cli, script):
    """Run the real ``swarm --start`` on the main thread. ``script(ctl)`` drives real CLI calls on a worker."""
    sock = home / 'run' / 'exod.sock'
    sock.parent.mkdir(parents=True)
    ev = types.SimpleNamespace(done=False, error=None, lock=threading.Lock())

    def snap():
        with rig.lock:
            return {p: dict(v) for p, v in rig.n.items()}

    def wait_for(pred, what):
        deadline = time.monotonic() + BOUND
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(0.02)
        raise AssertionError(f'bounded wait expired: {what}')

    def group(op, *extra):
        rc, out, err = run_cli(cli, 'swarm-group', op, rig.launch.group.id, *extra, '--socket', str(sock))
        assert rc == 0 and 'Traceback' not in err, (op, extra, out, err)
        return json.loads(out)

    def states():
        return {m['profile']: m['state'] for m in group('status')['members']}

    ctl = types.SimpleNamespace(group=group, states=states, snap=snap, wait_for=wait_for, sock=sock,
                                run=lambda *a: run_cli(cli, *a))

    def client_side():
        try:
            wait_for(lambda: rig.launch is not None and sock.exists(), 'socket serving')
            for p in PROFILES:
                assert rig.entered[p].wait(BOUND), f'{p} provider turn never became pending'
            script(ctl)
        except BaseException as exc:  # noqa: BLE001
            ev.error = exc
        finally:
            for p in PROFILES:                         # always free the provider before the launch is torn down
                rig.release(p)
            with ev.lock:
                if not ev.done:
                    os.kill(os.getpid(), signal.SIGINT)   # cmd_swarm's own handler cancels the serve task

    worker = threading.Thread(target=client_side, daemon=True)
    argv = ['swarm', '--profiles', ','.join(PROFILES), '--seat', OWNER, '--mode', 'socratic', '--start',
            '--socket', str(sock), *write_plan(home.parent)]
    worker.start()
    try:
        rc = cli.main(argv)
    finally:
        with ev.lock:
            ev.done = True
        for p in PROFILES:
            rig.release(p)
        worker.join(BOUND + 5)
    assert not worker.is_alive()
    if ev.error is not None:
        raise ev.error
    assert rc == 0 and rig.closed


def test_member_pause_resume_targets_one_member_and_completion_is_observed(home, rig, cli):
    seen = {}

    def script(ctl):
        gid = rig.launch.group.id
        seen['before'] = ctl.states()
        seen['pause'] = ctl.group('pause', '--member', 'builder')
        seen['paused'] = ctl.states()
        seen['paused_snap'] = ctl.snap()
        # the other member is untouched: it finishes its held provider call while builder stays paused
        rig.release('checker')
        ctl.wait_for(lambda: ctl.states()['checker'] == 'completed', 'checker completed while builder paused')
        ctl.wait_for(lambda: ctl.snap()['checker']['active'] == 0, 'checker provider stream closed')
        seen['mid'] = ctl.states()
        seen['mid_snap'] = ctl.snap()
        seen['resume'] = ctl.group('resume', '--member', 'builder')
        seen['resumed'] = ctl.states()
        seen['resumed_snap'] = ctl.snap()
        rig.release('builder')                         # provider completion AFTER the resume
        ctl.wait_for(lambda: ctl.states()['builder'] == 'completed', 'builder completed after resume')
        ctl.wait_for(lambda: ctl.snap()['builder']['active'] == 0, 'builder provider stream closed')
        seen['end'] = ctl.states()
        seen['end_snap'] = ctl.snap()
        ctl.wait_for(lambda: ctl.group('status')['receipt']['state'] == 'settled', 'receipt settled')
        pid = json.loads(ctl.run('swarm-plan', 'list', gid)[1])['plans'][0]['plan_id']
        seen['plan'] = ctl.run('swarm-plan', 'status', gid, pid)

    drive(home, rig, cli, script)
    gid = rig.launch.group.id
    assert seen['pause'] == {'id': gid, 'paused': True, 'member': 'builder'}
    assert seen['resume'] == {'id': gid, 'resumed': True, 'member': 'builder'}
    assert not set(seen['before'].values()) & {'pausing', 'paused', 'completed', 'stopped', 'failed'}
    # pause does NOT cancel the in-flight turn: builder reads 'pausing', checker is not paused
    assert seen['paused'] == {'builder': 'pausing', 'checker': seen['paused']['checker']}
    assert seen['paused']['checker'] not in {'pausing', 'paused', 'completed', 'stopped', 'failed'}
    assert seen['paused_snap']['builder'] == dict(started=1, active=1, finished=0, cancelled=0)
    # checker completed while builder was still paused and held, untouched
    assert seen['mid'] == {'builder': 'pausing', 'checker': 'completed'}
    assert seen['mid_snap']['checker']['finished'] == 1 and seen['mid_snap']['checker']['cancelled'] == 0
    assert seen['mid_snap']['builder'] == dict(started=1, active=1, finished=0, cancelled=0)
    # resume re-opens builder's gate. It is running again, not stopped or completed, provider still pending.
    assert seen['resumed']['builder'] not in {'pausing', 'paused', 'completed', 'stopped', 'failed'}
    assert seen['resumed']['checker'] == 'completed'
    assert seen['resumed_snap']['builder'] == dict(started=1, active=1, finished=0, cancelled=0)
    # provider completion after resume is really observed
    assert seen['end'] == {'builder': 'completed', 'checker': 'completed'}
    assert seen['end_snap']['builder'] == dict(started=1, active=0, finished=1, cancelled=0)
    assert seen['end_snap']['checker'] == dict(started=1, active=0, finished=1, cancelled=0)
    rc_p, out_p, err_p = seen['plan']
    assert rc_p == 0 and 'Traceback' not in err_p, (out_p, err_p)
    tasks = {t['task_id']: t for t in json.loads(out_p)['tasks']}
    assert 'B1-REPLY' in (tasks['b1']['result'] or '') and 'C1-REPLY' in (tasks['c1']['result'] or '')


def test_member_stop_cancels_only_targeted_member_other_completes(home, rig, cli):
    seen = {}

    def script(ctl):
        seen['stop'] = ctl.group('stop', '--member', 'builder')
        ctl.wait_for(lambda: ctl.states()['builder'] == 'stopped' and ctl.snap()['builder']['cancelled'] == 1,
                     'builder stopped and its provider call cancelled')
        seen['mid'] = ctl.states()
        seen['mid_snap'] = ctl.snap()
        rig.release('checker')                         # the untouched member still completes its provider call
        ctl.wait_for(lambda: ctl.states()['checker'] == 'completed', 'checker completed after builder stop')
        ctl.wait_for(lambda: ctl.snap()['checker']['active'] == 0, 'checker provider stream closed')
        seen['end'] = ctl.states()
        seen['end_snap'] = ctl.snap()
        ctl.wait_for(lambda: ctl.group('status')['receipt']['state'] == 'settled', 'receipt settled')
        seen['receipt'] = ctl.group('status')['receipt']

    drive(home, rig, cli, script)
    gid = rig.launch.group.id
    assert seen['stop'] == {'id': gid, 'stopped': True, 'member': 'builder'}
    assert seen['mid']['builder'] == 'stopped' and seen['mid']['checker'] not in {'stopped', 'completed', 'failed'}
    assert seen['mid_snap']['builder'] == dict(started=1, active=0, finished=0, cancelled=1)
    assert seen['mid_snap']['checker'] == dict(started=1, active=1, finished=0, cancelled=0)   # not stopped
    assert seen['end'] == {'builder': 'stopped', 'checker': 'completed'}
    assert seen['end_snap']['checker'] == dict(started=1, active=0, finished=1, cancelled=0)
    assert seen['end_snap']['builder']['finished'] == 0
    assert seen['receipt']['members'] == {'builder': 'stopped', 'checker': 'completed'}
