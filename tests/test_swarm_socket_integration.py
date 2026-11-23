"""exo swarm --start --plan served on a REAL Unix socket, queried by ``exo swarm-plan`` through the REAL protocol Client.

Needs a swarm core (``exo.group_launch``, ``exo.core.group_commands``, ``exo.core.group_plan_execution``);
the module skips honestly when they are unavailable. Run it with the core ``src`` on PYTHONPATH.
``exo.cli``, ``exo.swarm_cmd`` and ``exo.swarm_plan_cmd`` are loaded from THIS checkout, the rest of ``exo.*``
resolves to the core.

What is real, end to end: ``cli.main(['swarm', ...])`` -> ``cmd_swarm`` -> ``group_launch.create`` ->
``launch.start(plan=, grants=)`` (host gate, runtime, router, member CoreEngines) -> the core
``exo.server.app.serve`` on a Unix socket (principal credential issued beside it) -> the core
``exo.protocol.client.Client`` (``principal='chat'``, kernel peer check) -> the core ``group_commands``
handlers, driven by ``cli.main(['swarm-plan', 'list'|'status', ...])``.
What is substituted, and only that: the provider TRANSPORT (``RuleFake.stream`` records the prompt, then the real
offline rule engine answers). ``serve``, ``Client`` and ``launch.start`` are never replaced.
Shutdown goes through ``cmd_swarm``'s own SIGINT handler (cancels the serve task; ``aclose`` runs in its finally).
``cmd_swarm`` needs the main thread (signal handlers + ``asyncio.run``), so the CLI client calls run on a
worker thread and stdout/stderr are captured there with ``redirect_*``.
Hermetic: short temp EXO_HOME under /tmp (AF_UNIX path limit), EXO_NO_KEYCHAIN=1, no network, no real provider,
no production credential, every wait bounded by BOUND seconds.

Refusals follow the ACTUAL protocol: ``plan_list``/``plan_status`` for a group or plan this caller does not own
answer ``not_found`` / "no such plan" (no existence oracle, same as a ghost id), while a connection that does
not carry the principal credential is refused by ``require_principal`` with ``forbidden`` (a different code).
"""
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
OWNER, FOREIGN = 'owner', 'intruder'
FOREIGN_RESULT = 'FOREIGN-SECRET-RESULT'
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
    """Real create (wrapped only to seed a foreign group/plan and observe), real start/serve; fake provider text."""
    from exo.core.models.fake import RuleFake

    rec = types.SimpleNamespace(launch=None, prompts=[], closed=False, foreign_gid=None, foreign_plan=None)
    real_create, original_stream = group_launch.create, RuleFake.stream

    def create(*args, **kw):
        launch = rec.launch = real_create(*args, **kw)
        seed_foreign(launch, rec)                      # same DB, other owner: must stay invisible to the CLI
        real_aclose = launch.aclose

        async def aclose():
            await real_aclose()
            rec.closed = True
        launch.aclose = aclose                         # observe the close only; behaviour is the real one
        return launch

    async def stream(this, messages, tools, **opts):
        rec.prompts.append(next((m.text() for m in reversed(messages) if m.role == 'user'), ''))
        async for event in original_stream(this, messages, tools, **opts):
            yield event

    monkeypatch.setattr(group_launch, 'create', create)
    monkeypatch.setattr(RuleFake, 'stream', stream)
    return rec


def seed_foreign(launch, rec):
    from exo.core.agents.route_config import RouteConfig
    groups = launch.host._groups
    group = groups.create(owner=FOREIGN, mode='socratic', members=PROFILES, known_profiles=PROFILES)
    grants = contract.HostGrants(
        owner=FOREIGN, profiles=frozenset(PROFILES), permits={'builder': frozenset({'read_repo'})},
        write_roots={}, routes=RouteConfig.of({'backend': 'default'}), reviewer_profiles=frozenset({'checker'}))
    raw = {'schema': contract.SCHEMA_VERSION, 'tasks': [
        {'task_id': 'fb', 'role': 'builder', 'goal': 'foreign work', 'profile': 'builder', 'acceptance': ['done']}]}
    plan = contract.validate_plan(raw, grants, group=group)
    plans = plan_exec.PlanExecutions(groups)
    plans.begin(plan, owner=FOREIGN)
    kw = dict(plan_id=plan.plan_id, group_id=group.id, owner=FOREIGN)
    plans.mark(**kw, task_id='fb', state='running')
    plans.mark(**kw, task_id='fb', state='completed', result=FOREIGN_RESULT)
    plans.finish(**kw, states={'fb': 'completed'})
    rec.foreign_gid, rec.foreign_plan = group.id, plan.plan_id


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


def test_cli_swarm_socket_plan_list_status_roundtrip(home, rig, cli):
    sock = home / 'run' / 'exod.sock'                  # swarm-plan connects to the default socket of EXO_HOME
    sock.parent.mkdir(parents=True)
    ev = types.SimpleNamespace(done=False, error=None, results={}, lock=threading.Lock())

    def client_side():
        deadline = time.monotonic() + BOUND
        try:
            def until(pred, argv):
                while True:
                    rc, out, err = run_cli(cli, *argv)
                    if rc == 0 and pred(json.loads(out)):
                        return out
                    if time.monotonic() > deadline:
                        raise AssertionError(f'bounded wait expired for {argv}: rc={rc} err={err!r}')
                    time.sleep(0.05)

            while rig.launch is None and time.monotonic() < deadline:
                time.sleep(0.02)
            while not sock.exists() and time.monotonic() < deadline:   # server bound; avoids the no-daemon path
                time.sleep(0.02)
            gid = rig.launch.group.id
            ev.results['gid'] = gid
            out = until(lambda d: d['plans'] and all(p['state'] == 'settled' for p in d['plans']),
                        ['swarm-plan', 'list', gid])
            listed = json.loads(out)
            pid = listed['plans'][0]['plan_id']
            ev.results['list'] = listed
            ev.results['status'] = run_cli(cli, 'swarm-plan', 'status', gid, pid)
            ev.results['prefix'] = run_cli(cli, 'swarm-plan', 'status', gid, pid[:12])
            # refusals through the real socket and the real Client
            ev.results['foreign_list'] = run_cli(cli, 'swarm-plan', 'list', rig.foreign_gid)
            ev.results['foreign_status'] = run_cli(cli, 'swarm-plan', 'status', rig.foreign_gid, rig.foreign_plan)
            ev.results['foreign_via_mine'] = run_cli(cli, 'swarm-plan', 'status', gid, rig.foreign_plan)
            ev.results['ghost_list'] = run_cli(cli, 'swarm-plan', 'list', 'no-such-group')
            ev.results['ghost_status'] = run_cli(cli, 'swarm-plan', 'status', gid, '0' * 64)
            ev.results['local'] = local_client_refusal(gid)
        except BaseException as exc:                    # noqa: BLE001 - reported on the main thread
            ev.error = exc
        finally:
            with ev.lock:
                if not ev.done:
                    os.kill(os.getpid(), signal.SIGINT)  # cmd_swarm's own handler cancels the serve task

    def local_client_refusal(gid):
        """A REAL protocol Client with NO principal label: the connection is local, not the principal."""
        import asyncio
        from exo.protocol import schema as s
        from exo.protocol.client import Client

        async def go():
            async with Client(socket=str(sock), timeout=BOUND) as client:
                return await asyncio.wait_for(client.send(s.CommandRun(
                    name='group', args='plan_list ' + json.dumps({'id': gid}), session=None)), BOUND)
        return asyncio.run(go())

    worker = threading.Thread(target=client_side, name='swarm-plan-client', daemon=True)
    argv = ['swarm', '--profiles', ','.join(PROFILES), '--seat', OWNER, '--mode', 'socratic', '--start',
            '--socket', str(sock), *write_plan(home.parent)]
    worker.start()
    try:
        rc = cli.main(argv)      # cmd_swarm on the main thread; only the worker redirects stdout/stderr
    finally:
        with ev.lock:
            ev.done = True
        worker.join(BOUND + 5)
    assert not worker.is_alive()
    if ev.error is not None:
        raise ev.error

    r = ev.results
    gid = r['gid']
    assert rc == 0
    assert sys.modules['exo.swarm_cmd'].__file__ == str(CLI_SRC / 'swarm_cmd.py')       # the CLI under test
    assert sys.modules['exo.swarm_plan_cmd'].__file__ == str(CLI_SRC / 'swarm_plan_cmd.py')
    assert rig.closed and not sock.exists()                    # serve cancelled, socket released, launch closed
    assert rig.launch.store._conn is None                      # DB connection released by aclose (not reopened)
    assert 'MARK-B1' in ''.join(rig.prompts) and 'MARK-C1' in ''.join(rig.prompts)

    listed = r['list']
    assert listed['id'] == gid and listed['plans_truncated'] is False and len(listed['plans']) == 1
    (plan,) = listed['plans']
    assert plan['state'] == 'settled' and plan['task_count'] == 2
    assert plan['tasks_by_state']['completed'] == 2 and plan['tasks_by_state']['failed'] == 0

    rc_s, out_s, err_s = r['status']
    assert rc_s == 0 and err_s == ''
    status = json.loads(out_s)
    assert status['plan_id'] == plan['plan_id'] and status['state'] == 'settled' and status['id'] == gid
    tasks = {t['task_id']: t for t in status['tasks']}
    assert set(tasks) == {'b1', 'c1'} and all(t['state'] == 'completed' for t in tasks.values())
    assert tasks['b1']['profile'] == 'builder' and tasks['c1']['profile'] == 'checker'
    assert 'B1-REPLY' in (tasks['b1']['result'] or '') and 'C1-REPLY' in (tasks['c1']['result'] or '')
    assert all(t['error'] is None for t in tasks.values())
    assert FOREIGN_RESULT not in out_s
    rc_p, out_p, _ = r['prefix']
    assert rc_p == 0 and json.loads(out_p)['plan_id'] == plan['plan_id']                # prefix resolved via list

    # ownership: unknown/foreign group or plan -> the one not_found refusal, nothing leaked, indistinguishable
    for key in ('foreign_list', 'foreign_status', 'foreign_via_mine', 'ghost_list', 'ghost_status'):
        rc_f, out_f, err_f = r[key]
        assert rc_f == 1 and out_f == '', key
        assert 'not_found' in err_f and 'no such plan' in err_f, (key, err_f)
        for leaked in (FOREIGN_RESULT, FOREIGN, 'foreign work', rig.foreign_plan, rig.foreign_gid):
            assert leaked not in err_f, (key, leaked)
    assert r['foreign_list'][2].replace(rig.foreign_gid, 'X') == r['ghost_list'][2].replace('no-such-group', 'X')

    # permission: a connection without the principal credential is refused with a DIFFERENT code
    local = r['local']
    assert local.ok is False and local.error.code == 'forbidden'
    assert 'principal' in local.error.message and gid not in local.error.message
    assert local.error.code != 'not_found'


def test_swarm_plan_without_daemon_is_a_clean_refusal_not_a_traceback(home, cli):
    (home / 'run').mkdir(parents=True)
    rc, out, err = run_cli(cli, 'swarm-plan', 'list', 'g_absent')
    assert rc != 0 and 'Traceback' not in err
