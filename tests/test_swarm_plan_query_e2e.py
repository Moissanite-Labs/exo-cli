"""exo swarm-plan list/status through the CLI ``main`` entry down to the REAL core ``group_commands``.

Needs a core that ships ``exo.core.group_commands`` and ``exo.core.group_plan_execution`` (the swarm core);
the module skips honestly when they are unavailable. Run it with the core ``src`` on PYTHONPATH.
``exo.cli`` and ``exo.swarm_plan_cmd`` are loaded from THIS checkout, the rest of ``exo.*`` resolves to the core.

What is real: ``cli.main(argv)`` parsing and dispatch, ``cmd_swarm_plan`` (prefix resolution, shape checks,
printing), the core ``group_commands.command`` handlers (``plan_list``/``plan_status``, owner derivation, caps,
refusals), and a temporary SQLite ``SessionStore``/``GroupStore``/``PlanExecutions`` whose plans are created
through ``validate_plan`` + ``PlanExecutions.begin/mark/finish``.
What is substituted, and only that: the Unix-socket TRANSPORT (``exo.protocol.client.Client``). The shim runs
the core handler in-process with the caller stamped as the interactive principal and maps ``CommandFailed``
to an error ``Response`` as ``exo.server.app`` does. No socket, token or exod is exercised, and the engine
handed to the handler is a minimal ``agent``/``store`` namespace (what the core's own command tests use).
Hermetic: temp EXO_HOME, EXO_NO_KEYCHAIN=1, no network, no provider.
"""
import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

group_commands = pytest.importorskip('exo.core.group_commands')
plan_exec = pytest.importorskip('exo.core.group_plan_execution')
contract = pytest.importorskip('exo.core.group_task_contract')

BOUND = 10
CLI_SRC = Path(__file__).resolve().parents[1] / 'src' / 'exo'
OWNER, FOREIGN = 'host', 'intruder'
PROFILES = ['builder', 'checker']
SECRET = 'SECRET-RESULT-TEXT'
ERR = 'SECRET-ERROR-TEXT'


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """This checkout's ``exo.cli`` (real ``main``), with its lazy ``exo.swarm_plan_cmd`` from this checkout."""
    import exo
    if Path(exo.__file__).resolve().parent == CLI_SRC or not (
            Path(exo.__file__).resolve().parent / 'core' / 'group_commands.py').exists():
        pytest.skip('exo.* does not resolve to a swarm core')
    monkeypatch.setenv('EXO_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('EXO_NO_KEYCHAIN', '1')
    for name in ('swarm_plan_cmd', 'cli_under_test'):
        monkeypatch.delitem(sys.modules, f'exo.{name}', raising=False)
    monkeypatch.setattr(exo, '__path__', [*exo.__path__, str(CLI_SRC)])  # core first, CLI fills the gaps
    spec = importlib.util.spec_from_file_location('exo.cli_under_test', CLI_SRC / 'cli.py')
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'exo.cli_under_test', mod)
    spec.loader.exec_module(mod)
    return mod


class Rig:
    """A real temp SessionStore/GroupStore/PlanExecutions plus the transport shim that records each request."""

    def __init__(self, tmp_path):
        from exo.core.groups import GroupStore
        from exo.core.sessions import SessionStore
        self.store = SessionStore(tmp_path / 'sessions.sqlite')
        self.groups = GroupStore(self.store)
        self.plans = plan_exec.PlanExecutions(self.groups)
        self.requests = []      # (op, payload) as the core received them
        self.agent = OWNER      # the principal the core derives the owner from
        self.mine = self.group(OWNER)
        self.gid = self.mine.id

    def group(self, owner):
        return self.groups.create(owner=owner, mode='socratic', members=PROFILES, known_profiles=PROFILES)

    def plan(self, group, goal, owner=OWNER, settle=True):
        """A real validated plan, begun through PlanExecutions; settled tasks carry SECRET result/error text."""
        from exo.core.agents.route_config import RouteConfig
        grants = contract.HostGrants(
            owner=owner, profiles=frozenset(PROFILES), permits={'builder': frozenset({'read_repo'})},
            write_roots={}, routes=RouteConfig.of({'backend': 'model_map', 'routes': {'cheap': 'fake:fast'}}),
            reviewer_profiles=frozenset({'checker'}))
        raw = {'schema': contract.SCHEMA_VERSION, 'tasks': [
            {'task_id': 'build', 'role': 'builder', 'goal': goal, 'profile': 'builder', 'acceptance': ['done']},
            {'task_id': 'check', 'role': 'builder', 'goal': 'check ' + goal, 'profile': 'checker',
             'acceptance': ['done'], 'depends_on': ['build']}]}
        plan = contract.validate_plan(raw, grants, group=group)
        self.plans.begin(plan, owner=owner)
        if settle:
            kw = dict(plan_id=plan.plan_id, group_id=group.id, owner=owner)
            self.plans.mark(**kw, task_id='build', state='running')
            self.plans.mark(**kw, task_id='build', state='completed', result=SECRET)
            self.plans.mark(**kw, task_id='check', state='running')
            self.plans.mark(**kw, task_id='check', state='failed', error=ERR)
            self.plans.finish(**kw, states={'build': 'completed', 'check': 'failed'})
        return plan.plan_id

    def dump(self):
        names = [r[0] for r in self.store._q("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: [tuple(r) for r in self.store._q(f'SELECT * FROM {n} ORDER BY 1,2')] for n in names}


@pytest.fixture
def rig(tmp_path, cli, monkeypatch):
    import exo.protocol.client as client_mod
    from exo import principal
    from exo.server.engine import CommandFailed
    from exo.protocol import schema as s

    r = Rig(tmp_path)

    class ShimClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, cmd):
            assert isinstance(cmd, s.CommandRun) and cmd.name == 'group'
            op, _, rest = cmd.args.partition(' ')
            r.requests.append((op, json.loads(rest)))
            eng = types.SimpleNamespace(agent=r.agent, store=r.store)
            token = principal.CALLER.set(principal.Caller(principal.principal_as('chat')))
            try:
                out = await asyncio.wait_for(group_commands.command(eng, cmd), BOUND)
            except CommandFailed as exc:
                return s.Response(id='r', ok=False, error=s.ErrorInfo(code=exc.code, message=exc.message))
            finally:
                principal.CALLER.reset(token)
            return s.Response(id='r', ok=True, result={'output': out})

    monkeypatch.setattr(client_mod, 'Client', ShimClient)
    yield r
    r.store.close()


def run(cli, capsys, *argv):
    rc = cli.main(['swarm-plan', *argv])
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_owner_list_and_status_full_and_prefix_reach_real_handlers(rig, cli, capsys):
    group = rig.mine
    p1, p2 = rig.plan(group, 'first'), rig.plan(group, 'second', settle=False)
    before = rig.dump()

    rc, out, err = run(cli, capsys, 'list', rig.gid)
    listed = json.loads(out)
    assert rc == 0 and err == '' and rig.requests == [('plan_list', {'id': rig.gid, 'limit': 50})]
    assert listed['id'] == rig.gid and listed['plans_truncated'] is False
    by_id = {p['plan_id']: p for p in listed['plans']}
    assert set(by_id) == {p1, p2}
    assert by_id[p1]['state'] == 'settled' and by_id[p2]['state'] == 'running'
    assert by_id[p1]['tasks_by_state'] == {'pending': 0, 'running': 0, 'completed': 1, 'failed': 1, 'stopped': 0}

    rig.requests.clear()
    rc, full_out, err = run(cli, capsys, 'status', rig.gid, p1)           # full id: exactly one request
    assert rc == 0 and err == ''
    assert rig.requests == [('plan_status', {'id': rig.gid, 'plan_id': p1, 'limit': 50})]
    status = json.loads(full_out)
    assert status['plan_id'] == p1 and status['state'] == 'settled'
    assert {t['task_id']: t['state'] for t in status['tasks']} == {'build': 'completed', 'check': 'failed'}
    assert SECRET in full_out and ERR in full_out          # the owner sees its own task data, as JSON data

    rig.requests.clear()
    prefix = next(p1[:n] for n in range(8, 64) if not p2.startswith(p1[:n]))   # unique among both plans
    rc, pre_out, err = run(cli, capsys, 'status', rig.gid, prefix, '--limit', '1')
    assert rc == 0 and err == ''
    assert [op for op, _ in rig.requests] == ['plan_list', 'plan_status']        # list, then one status
    assert rig.requests[0][1] == {'id': rig.gid, 'limit': 200}
    assert rig.requests[1][1] == {'id': rig.gid, 'plan_id': p1, 'limit': 1}
    pre = json.loads(pre_out)
    assert pre['plan_id'] == p1 and pre['tasks_returned'] == 1 and pre['tasks_truncated'] is True
    assert all('owner' not in payload for _, payload in rig.requests)
    assert rig.dump() == before                                           # read-only end to end


def test_unmatched_and_ambiguous_prefix_fail_closed_without_status(rig, cli, capsys):
    group = rig.mine
    rig.plan(group, 'first')
    rc, out, err = run(cli, capsys, 'status', rig.gid, 'ffffffff')
    assert (rc, out) == (1, '') and 'no such plan' in err
    assert [op for op, _ in rig.requests] == ['plan_list']
    # digests cannot be ground to collide in a test, so a colliding twin is seeded in the real table
    (row,) = rig.store._q('SELECT * FROM swarm_plans')
    twin = row['plan_id'][:8] + '0' * 56
    rig.store._q('INSERT INTO swarm_plans(plan_id,group_id,owner,execution_id,state,created,finished) '
                 "VALUES (?,?,?,'execution','settled','2026-01-01T00:00:00.000Z','2026-01-01T00:00:01.000Z')",
                 (twin, rig.gid, OWNER))
    rig.requests.clear()
    rc, out, err = run(cli, capsys, 'status', rig.gid, row['plan_id'][:8])
    assert (rc, out) == (1, '') and 'ambiguous' in err
    assert [op for op, _ in rig.requests] == ['plan_list']


def test_truncated_list_never_resolves_a_prefix_even_with_one_match(rig, cli, capsys):
    group = rig.mine
    ids = [rig.plan(group, f'plan {i}', settle=False) for i in range(201)]   # one more than the 200 cap
    page, truncated = rig.plans.list_plans(group_id=rig.gid, owner=OWNER, limit=200)
    assert truncated is True and len(page) == 200
    target = page[0].plan_id
    prefix = next(target[:n] for n in range(8, 64)
                  if sum(i.startswith(target[:n]) for i in ids) == 1)        # exactly one match, in the page
    rc, out, err = run(cli, capsys, 'status', rig.gid, prefix)
    assert (rc, out) == (1, '') and 'truncated' in err
    assert rig.requests == [('plan_list', {'id': rig.gid, 'limit': 200})]    # no status request was made
    rig.requests.clear()
    rc, out, err = run(cli, capsys, 'status', rig.gid, target)               # the full id still works
    assert rc == 0 and json.loads(out)['plan_id'] == target
    assert [op for op, _ in rig.requests] == ['plan_status']


def test_foreign_owner_is_refused_identically_with_no_leakage(rig, cli, capsys):
    mine, theirs = rig.mine, rig.group(FOREIGN)
    foreign_plan = rig.plan(theirs, 'foreign work', owner=FOREIGN)
    rig.plan(mine, 'mine')
    before = rig.dump()
    ghost, ghost_plan = 'no-such-group', '0' * 64
    attempts = [('list', theirs.id), ('status', theirs.id, foreign_plan), ('status', theirs.id, foreign_plan[:12]),
                ('status', rig.gid, foreign_plan)]                          # foreign plan via MY group id
    for argv in attempts:
        rc, out, err = run(cli, capsys, *argv)
        assert rc == 1 and out == ''
        for leaked in (foreign_plan, foreign_plan[:12], SECRET, ERR, FOREIGN, 'foreign work'):
            assert leaked not in err, (argv, leaked)
    # indistinguishable from nothing existing: same refusal as an unknown group / plan id
    rc, _, foreign_err = run(cli, capsys, 'list', theirs.id)
    _, _, ghost_err = run(cli, capsys, 'list', ghost)
    assert foreign_err.replace(theirs.id, 'X') == ghost_err.replace(ghost, 'X')
    _, _, a = run(cli, capsys, 'status', rig.gid, foreign_plan)
    _, _, b = run(cli, capsys, 'status', rig.gid, ghost_plan)
    assert a.replace(foreign_plan, 'X') == b.replace(ghost_plan, 'X')
    assert rig.dump() == before                                             # nothing was written either
    rig.agent = FOREIGN                                                     # same CLI, the other principal
    rc, out, err = run(cli, capsys, 'status', theirs.id, foreign_plan)
    assert rc == 0 and json.loads(out)['plan_id'] == foreign_plan           # it was the owner check, not the ids
    rc, out, err = run(cli, capsys, 'list', rig.gid)
    assert rc == 1 and out == '' and 'mine' not in err
