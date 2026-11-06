"""exo swarm --plan/--grants against the REAL core contract (and one real CoreEngine run).

These tests need a core that ships ``exo.core.group_task_contract`` and ``exo.group_launch``
(the swarm plan core). The CLI fork does not vendor them, so the module skips there; run it with
the core ``src`` on PYTHONPATH. ``swarm_cmd`` is loaded from THIS checkout by path so the CLI under
test is this repo's file, while ``exo.*`` resolves to the core.

Two layers, kept apart on purpose:
* contract tests: the real ``HostGrants``/``validate_plan``/``RouteConfig``, with ``group_launch.create``
  and ``serve`` replaced by recorders (no engines, no store);
* one offline end-to-end run: the real ``group_launch.create`` -> ``GroupLaunch.start(plan=, grants=)``
  on real CoreEngine members with the offline ``RuleFake`` provider; only ``serve`` is replaced
  (it waits for the plan to settle instead of serving a socket). No client connects.
"""
import asyncio
import importlib.util
import io
import json
import sys
import types
from contextlib import redirect_stderr
from pathlib import Path

import pytest

contract = pytest.importorskip('exo.core.group_task_contract')
pytest.importorskip('exo.group_launch')

_SRC = Path(__file__).resolve().parents[1] / 'src' / 'exo' / 'swarm_cmd.py'
_spec = importlib.util.spec_from_file_location('exo_cli_swarm_cmd_under_test', _SRC)
swarm_cmd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(swarm_cmd)

SECRET = 'SECRET-PAYLOAD-7f3a'
BOUND = 10


def ns(**over):
    import argparse
    a = argparse.Namespace(cmd='swarm', profiles='builder,checker', seat=None, socket=None,
                           stdio=True, mode='socratic', conductor=None, conversation=False,
                           start=True, max_turns=None, idle_timeout=None, plan=None, grants=None)
    return argparse.Namespace(**{**vars(a), **over})


def task(task_id, profile='builder', **extra):
    return {'task_id': task_id, 'role': 'builder', 'goal': f'MARK-{task_id.upper()} do it',
            'profile': profile, 'acceptance': ['done'], **extra}


def plan_doc(tasks=None, **extra):
    return {'schema': contract.SCHEMA_VERSION, 'tasks': tasks or [task('b1')], **extra}


def grants_doc(**over):
    g = {'schema': swarm_cmd.GRANTS_SCHEMA, 'profiles': ['builder', 'checker'],
         'permits': {'builder': ['read_repo']}, 'write_roots': {},
         'reviewer_profiles': ['checker'], 'routes': {'backend': 'default'}}
    g.update(over)
    return g


def write(tmp_path, name, value):
    path = tmp_path / name
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding='utf-8')
    return str(path)


def run_cmd(a):
    err = io.StringIO()
    with redirect_stderr(err):
        rc = swarm_cmd.cmd_swarm(a)
    return rc, err.getvalue()


# --- recorder core: real contract, fake create/serve ---------------------------------

class Group:
    id = 'g-1'
    owner = 'owner'
    mode = 'socratic'
    members = (types.SimpleNamespace(profile='builder'), types.SimpleNamespace(profile='checker'))


class Launch:
    def __init__(self, calls, start_exc=None):
        self.calls, self.start_exc = calls, start_exc
        self.group, self.engine = Group(), object()

    def start(self, **kw):
        self.calls.append(('launch.start', kw))
        if self.start_exc:
            raise self.start_exc

    async def aclose(self):
        await asyncio.sleep(0)
        self.calls.append(('launch.aclose',))


@pytest.fixture
def core(monkeypatch):
    calls = []
    state = types.SimpleNamespace(calls=calls, start_exc=None)

    class LaunchError(RuntimeError):
        pass

    def create(seat, *, profiles, mode='conductor', conductor=None):
        calls.append(('create', seat, list(profiles), mode))
        return Launch(calls, state.start_exc)

    gl = types.ModuleType('exo.group_launch')
    gl.LaunchError, gl.create = LaunchError, create

    async def serve(engine, **kw):
        calls.append(('serve', kw))

    srv = types.ModuleType('exo.server.app')
    srv.serve = serve
    monkeypatch.setitem(sys.modules, 'exo.group_launch', gl)
    monkeypatch.setitem(sys.modules, 'exo.server.app', srv)
    return state


def names(calls):
    return [c[0] for c in calls]


# --- file reading: bounded, strict, sanitized, all before create ----------------------

@pytest.mark.parametrize('which', ['plan', 'grants'])
@pytest.mark.parametrize('content, msg', [
    (b'{"schema": "' + SECRET.encode() + b'", ', 'not valid JSON (line 1'),
    (b'\xff\xfe' + SECRET.encode(), 'not UTF-8'),
    (('{"a": "' + SECRET + '", "a": 1}').encode(), 'duplicate key'),
    (b'{"x": NaN}', 'NaN and Infinity'),
    (b'[' * 40 + b']' * 40, 'nests too deeply'),
    (('["' + SECRET + '"]').encode(), 'must be a JSON object'),
])
def test_bad_json_files_rejected_before_create(tmp_path, core, which, content, msg):
    good = {'plan': plan_doc(), 'grants': grants_doc()}
    paths = {k: write(tmp_path, f'{k}.json', v) for k, v in good.items()}
    paths[which] = write(tmp_path, f'bad-{which}.json', content)
    rc, err = run_cmd(ns(**paths))
    assert rc == 1 and msg in err and f'--{which}' in err
    assert SECRET not in err
    assert core.calls == []


def test_oversize_missing_and_non_regular_files(tmp_path, core):
    g = write(tmp_path, 'g.json', grants_doc())
    big = write(tmp_path, 'big.json', b'{"x": "' + b'a' * swarm_cmd.PLAN_MAX_BYTES + b'"}')
    for path, msg in [(big, 'exceeds'), (str(tmp_path / 'nope.json'), 'cannot open file'),
                      (str(tmp_path), 'not a regular file')]:
        rc, err = run_cmd(ns(plan=path, grants=g))
        assert rc == 1 and msg in err, err
    assert core.calls == []


# --- schema: unknown fields, owner, elevation, routes -- all before create -------------

@pytest.mark.parametrize('plan, msg', [
    (plan_doc(**{SECRET: 1}), '1 unknown top-level key'),
    ({**plan_doc(), 'schema': 'v0'}, '"schema" must be'),
    ({**plan_doc(), 'tasks': []}, '"tasks" must be a non-empty list'),
    ({**plan_doc(), 'review_of': [SECRET]}, '"review_of" must be an object'),
])
def test_plan_shape_rejected_before_create(tmp_path, core, plan, msg):
    rc, err = run_cmd(ns(plan=write(tmp_path, 'p.json', plan),
                         grants=write(tmp_path, 'g.json', grants_doc())))
    assert rc == 1 and msg in err and SECRET not in err
    assert core.calls == []


@pytest.mark.parametrize('grants, msg', [
    ({**grants_doc(), 'owner': SECRET}, '"owner" is not accepted'),
    ({**grants_doc(), SECRET: True}, '1 unknown key'),
    ({k: v for k, v in grants_doc().items() if k != 'routes'}, 'missing required key(s): routes'),
    ({k: v for k, v in grants_doc().items() if k != 'permits'}, 'missing required key(s): permits'),
    (grants_doc(schema='v0'), '"schema" must be'),
    (grants_doc(profiles=['builder']), 'exactly the --profiles members'),
    (grants_doc(profiles=['builder', 'checker', 'intruder']), 'exactly the --profiles members'),
    (grants_doc(profiles=['builder', 'checker', 'checker']), 'duplicate entry'),
    (grants_doc(permits={'builder': ['spawn']}), 'authority-transferring'),
    (grants_doc(permits={'builder': ['approve']}), 'authority-transferring'),
    (grants_doc(permits={SECRET: ['read_repo']}), 'outside "profiles"'),
    (grants_doc(permits={'builder': 'read_repo'}), 'list of non-empty strings'),
    (grants_doc(write_roots={'builder': [1]}), 'list of non-empty strings'),
    (grants_doc(reviewer_profiles=['other']), 'subset of "profiles"'),
    (grants_doc(routes={'backend': SECRET}), 'rejected by the core route contract'),
    (grants_doc(routes='default'), '"routes" must be an object'),
])
def test_grants_shape_rejected_before_create(tmp_path, core, grants, msg):
    rc, err = run_cmd(ns(plan=write(tmp_path, 'p.json', plan_doc()),
                         grants=write(tmp_path, 'g.json', grants)))
    assert rc == 1 and msg in err, err
    assert SECRET not in err
    assert core.calls == []


def test_elevation_list_comes_from_the_core():
    # The CLI never keeps its own copy of the authority-transferring set.
    assert {'spawn', 'approve'} <= contract.ELEVATION_CAPABILITIES


# --- binding after create: real validate_plan; aclose always ------------------------------

def test_binding_failure_after_create_closes_and_never_starts(tmp_path, core):
    plan = plan_doc([task('b1', goal=f'{SECRET} goal', permits=['write_scope'])])
    rc, err = run_cmd(ns(plan=write(tmp_path, 'p.json', plan),
                         grants=write(tmp_path, 'g.json', grants_doc())))
    assert rc == 1 and 'plan rejected by the core contract (rule I' in err
    assert SECRET not in err
    assert names(core.calls) == ['create', 'launch.aclose']


def test_task_on_non_member_profile_rejected_after_create(tmp_path, core):
    plan = plan_doc([task('b1', profile='outsider')])
    rc, err = run_cmd(ns(plan=write(tmp_path, 'p.json', plan),
                         grants=write(tmp_path, 'g.json', grants_doc())))
    assert rc == 1 and 'core contract' in err and 'outsider' not in err.split('(rule')[0]
    assert names(core.calls) == ['create', 'launch.aclose']


def test_success_forwards_validated_plan_and_launch_owner_grants(tmp_path, core):
    grants = grants_doc(permits={'builder': ['read_repo', 'write_scope']},
                        write_roots={'builder': [str(tmp_path / 'w')]},
                        routes={'backend': 'model_map', 'routes': {'cheap': 'fake:fast'}})
    plan = plan_doc([task('b1', permits=['write_scope'], write_scopes=[str(tmp_path / 'w')]),
                     task('c1', profile='checker', role='reviewer', depends_on=['b1'])],
                    review_of={'b1': 'c1'})
    rc, err = run_cmd(ns(plan=write(tmp_path, 'p.json', plan),
                         grants=write(tmp_path, 'g.json', grants)))
    assert rc == 0, err
    assert names(core.calls) == ['create', 'launch.start', 'serve', 'launch.aclose']
    assert core.calls[0] == ('create', None, ['builder', 'checker'], 'socratic')
    kw = core.calls[1][1]
    assert set(kw) == {'plan', 'grants'}  # no conversation, audiences or other defaults
    g, p = kw['grants'], kw['plan']
    assert isinstance(g, contract.HostGrants) and isinstance(p, contract.ValidatedPlan)
    assert g.owner == Group.owner == p.owner  # from the launch principal, never the file
    assert p.group_id == Group.id
    assert g.profiles == frozenset({'builder', 'checker'})
    assert g.permits == {'builder': frozenset({'read_repo', 'write_scope'})}
    assert g.write_roots == {'builder': (str(tmp_path / 'w'),)}
    assert g.reviewer_profiles == frozenset({'checker'})
    assert [t.task_id for t in p.tasks] == ['b1', 'c1']
    assert 'validated plan' in err and '2 task(s)' in err


def test_host_start_refusal_is_sanitized_and_closes(tmp_path, core):
    core.start_exc = ValueError(f'host says {SECRET}')
    rc, err = run_cmd(ns(plan=write(tmp_path, 'p.json', plan_doc()),
                         grants=write(tmp_path, 'g.json', grants_doc())))
    assert rc == 1 and 'group plan start refused (ValueError)' in err
    assert SECRET not in err
    assert names(core.calls) == ['create', 'launch.start', 'launch.aclose']


# --- one offline end-to-end run on real CoreEngine members --------------------------------

MODELS = {'fake': {'rules': [{'match': f'MARK-{t.upper()}', 'steps': [f'{t.upper()}-REPLY']}
                             for t in ('b1', 'c1')]},
          'roles': {'main': ['fake:x']}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / 'seats').mkdir(parents=True)
    for name in ('builder', 'checker', 'owner'):
        (home / 'seats' / f'{name}.toml').write_text(f'name = "{name}"\n', encoding='utf-8')
    (home / 'config.json').write_text(json.dumps({'models': MODELS}), encoding='utf-8')
    monkeypatch.setenv('EXO_HOME', str(home))
    return home


@pytest.fixture
def real_core(monkeypatch):
    """Real create and engines; record the launch and model calls; serve waits for the plan."""
    from exo import group_launch
    from exo.core.group_plan_execution import PlanExecutions
    from exo.core.models.fake import RuleFake
    import exo.server.app as app

    rec = types.SimpleNamespace(launch=None, model_calls=0, plan_state=None, closed=False)
    real_create, original_stream = group_launch.create, RuleFake.stream

    def create(*args, **kw):
        rec.launch = real_create(*args, **kw)
        real_aclose = rec.launch.aclose

        async def aclose():
            await real_aclose()
            rec.closed = True
        rec.launch.aclose = aclose
        return rec.launch

    def stream(this, messages, tools, **opts):
        rec.model_calls += 1
        return original_stream(this, messages, tools, **opts)

    async def serve(engine, **kw):
        launch = rec.launch
        execs = PlanExecutions(launch.host._groups)
        group, owner = launch.group.id, launch.group.owner

        async def settled():
            while True:
                pid = rec.plan_id
                if pid and execs.plan_state(pid, group_id=group, owner=owner) == 'settled':
                    return
                await asyncio.sleep(0.01)
        await asyncio.wait_for(settled(), BOUND)
        rec.plan_state = 'settled'

    monkeypatch.setattr(group_launch, 'create', create)
    monkeypatch.setattr(RuleFake, 'stream', stream)
    monkeypatch.setattr(app, 'serve', serve)
    rec.plan_id = None
    return rec


def test_real_core_end_to_end_plan_runs_and_closes(tmp_path, home, real_core, monkeypatch):
    plan = plan_doc([task('b1'), task('c1', profile='checker', depends_on=['b1'])])
    real_plan_id = []
    real_validate = contract.validate_plan

    def validate(raw, grants, **kw):  # observe only: the real contract decides
        out = real_validate(raw, grants, **kw)
        real_core.plan_id = out.plan_id
        real_plan_id.append(out.plan_id)
        return out
    monkeypatch.setattr(contract, 'validate_plan', validate)

    rc, err = run_cmd(ns(seat='owner', plan=write(tmp_path, 'p.json', plan),
                         grants=write(tmp_path, 'g.json', grants_doc(permits={}))))
    assert rc == 0, err
    assert real_core.plan_state == 'settled'
    assert real_core.model_calls >= 2  # both tasks reached the offline provider
    assert real_core.closed
    from exo.core.group_plan_execution import PlanExecutions
    launch = real_core.launch
    rows = PlanExecutions(launch.host._groups).tasks(real_plan_id[0], group_id=launch.group.id,
                                                    owner=launch.group.owner)
    assert {r.task_id: r.state for r in rows} == {'b1': 'completed', 'c1': 'completed'}


def test_real_core_refused_binding_makes_no_model_call(tmp_path, home, real_core):
    plan = plan_doc([task('b1', permits=['write_scope'])])  # not granted
    rc, err = run_cmd(ns(seat='owner', plan=write(tmp_path, 'p.json', plan),
                         grants=write(tmp_path, 'g.json', grants_doc())))
    assert rc == 1 and 'core contract' in err
    assert real_core.model_calls == 0
    assert real_core.closed
