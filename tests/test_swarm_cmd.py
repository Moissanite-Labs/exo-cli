"""exo swarm: parser registration, fail-closed validation, single-loop lifecycle, clean stdio."""
import argparse
import asyncio
import io
import sys
from contextlib import redirect_stderr

import pytest

from exo import cli, swarm_cmd


def ns(**over):
    a = argparse.Namespace(cmd='swarm', profiles='seat1,seat2', seat=None, socket=None,
                           stdio=False, mode='conductor', conductor=None, conversation=False,
                           start=False, max_turns=None, idle_timeout=None,
                           plan=None, grants=None)
    return argparse.Namespace(**{**vars(a), **over})


def parse(*argv):
    p = cli.build_parser('swarm')
    return p.parse_args(['swarm', *argv])


# --- parser / registration ----------------------------------------------------

def test_registered_in_lazy():
    assert cli.LAZY['swarm'][:2] == ('swarm_cmd', 'cmd_swarm')


def test_swarm_in_lazy_command_modules_probe():
    from tests.test_cli_lazy import COMMAND_MODULES
    assert 'exo.swarm_cmd' in COMMAND_MODULES


def test_parser_requires_profiles():
    with pytest.raises(SystemExit):
        parse()


def test_parser_flags():
    a = parse('--profiles', 'a,b', '--seat', 'owner', '--socket', '/tmp/x.sock',
              '--start')
    assert a.profiles == 'a,b' and a.seat == 'owner' and a.start is True
    with pytest.raises(SystemExit):  # --socket and --stdio are exclusive
        parse('--profiles', 'a,b', '--socket', '/tmp/x.sock', '--stdio')


def test_parser_accepts_mode_and_conductor():
    a = parse('--profiles', 'a,b')
    assert a.mode == 'conductor' and a.conductor is None
    a = parse('--profiles', 'a,b', '--mode', 'socratic')
    assert a.mode == 'socratic'
    a = parse('--profiles', 'a,b', '--mode', 'conductor', '--conductor', 'seat2')
    assert a.mode == 'conductor' and a.conductor == 'seat2'
    a = parse('--profiles', 'a,b', '--conductor', 'seat1')
    assert a.conductor == 'seat1'


def test_parser_rejects_unknown_mode():
    with pytest.raises(SystemExit):
        parse('--profiles', 'a,b', '--mode', 'weird')


def test_parser_bounds_default_to_none():
    a = parse('--profiles', 'a,b', '--conversation', '--start')
    assert a.max_turns is None and a.idle_timeout is None


def test_parser_rejects_nonpositive_max_turns():
    with pytest.raises(SystemExit):
        parse('--profiles', 'a,b', '--max-turns', '0')
    with pytest.raises(SystemExit):
        parse('--profiles', 'a,b', '--max-turns=-1')
    with pytest.raises(SystemExit):
        parse('--profiles', 'a,b', '--max-turns', 'x')


def test_parser_rejects_nonfinite_idle_timeout():
    for bad in ('0', '-1', 'nan', 'inf', '-inf', 'abc'):
        with pytest.raises(SystemExit):
            parse('--profiles', 'a,b', '--idle-timeout', bad)


def test_parser_accepts_core_boundary_bounds():
    a = parse('--profiles', 'a,b', '--max-turns', '10000')
    assert a.max_turns == 10000
    a = parse('--profiles', 'a,b', '--idle-timeout', '86400')
    assert a.idle_timeout == 86400.0


def test_parser_rejects_bounds_above_core_maxima(capsys):
    with pytest.raises(SystemExit):
        parse('--profiles', 'a,b', '--max-turns', '10001')
    assert '10000' in capsys.readouterr().err
    with pytest.raises(SystemExit):
        parse('--profiles', 'a,b', '--idle-timeout', '86401')
    assert '86400' in capsys.readouterr().err


def test_start_help_says_one_turn_per_member_immediately(capsys):
    with pytest.raises(SystemExit):
        parse('--help')
    help_text = capsys.readouterr().out
    assert 'run one model turn per member immediately' in help_text
    assert 'no run occurs until a principal /group start' in help_text
    assert '--conversation' in help_text and 'bounded' in help_text
    assert '--mode' in help_text and '--conductor' in help_text
    assert '--max-turns' in help_text and '--idle-timeout' in help_text


def test_docstring_has_no_em_dashes():
    assert '\u2014' not in (swarm_cmd.__doc__ or '')


def test_docstring_documents_principal_controls_modes_and_conversation():
    doc = swarm_cmd.__doc__ or ''
    assert 'first profile' in doc and 'conductor' in doc and 'socratic' in doc
    assert 'bounded' in doc and 'conversation' in doc and 'one-shot' in doc
    assert 'principal' in doc and 'Unix socket' in doc
    assert 'stdio protocol alone cannot grant principal' in doc
    assert '#1019' in doc  # the required core dependency is documented


# --- fail-closed validation (no core imported, nothing allocated) -------------

def test_empty_profile_entry_fails_closed(capsys):
    assert swarm_cmd.cmd_swarm(ns(profiles='seat1,,seat2')) == 1
    assert 'empty entry' in capsys.readouterr().err


def test_bounds_without_conversation_fail_closed(capsys):
    assert swarm_cmd.cmd_swarm(ns(max_turns=3)) == 1
    assert '--max-turns/--idle-timeout require --conversation' in capsys.readouterr().err
    assert swarm_cmd.cmd_swarm(ns(idle_timeout=30.0)) == 1
    assert '--max-turns/--idle-timeout require --conversation' in capsys.readouterr().err


def test_conversation_without_start_fails_closed(capsys):
    assert swarm_cmd.cmd_swarm(ns(conversation=True)) == 1
    assert '--conversation requires --start' in capsys.readouterr().err
    assert swarm_cmd.cmd_swarm(ns(conversation=True, max_turns=3)) == 1
    assert '--conversation requires --start' in capsys.readouterr().err


def test_bounds_above_core_maxima_fail_closed(capsys):
    assert swarm_cmd.cmd_swarm(ns(start=True, conversation=True, max_turns=10001)) == 1
    assert '10000' in capsys.readouterr().err
    assert swarm_cmd.cmd_swarm(ns(start=True, conversation=True, idle_timeout=86401.0)) == 1
    assert '86400' in capsys.readouterr().err


def test_missing_core_feature_is_a_useful_error(monkeypatch, capsys):
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == 'exo.group_launch' or name == 'exo.core.group_host':
            raise ImportError('No module named exo.group_launch')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr('builtins.__import__', fake_import)
    for mod in ('exo.group_launch', 'exo.core.group_host'):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    assert swarm_cmd.cmd_swarm(ns()) == 1
    assert 'no swarm group launch support' in capsys.readouterr().err


# --- lifecycle: one loop, ordered start -> serve -> aclose ---------------------

class FakeConversationOptions:
    def __init__(self, *, max_turns=8, idle_timeout=300.0):
        self.max_turns, self.idle_timeout = max_turns, idle_timeout


class FakeLaunchError(RuntimeError):
    pass


class FakeHost:
    def __init__(self, calls, start_exc=None):
        self.calls, self.start_exc = calls, start_exc

    def start(self, group_id, **kw):
        self.calls.append(('host.start', group_id, kw))
        if self.start_exc:
            raise self.start_exc


class FakeGroup:
    id = 'g1'
    mode = 'conductor'


class FakeLaunch:
    def __init__(self, calls, start_exc=None):
        self.host = FakeHost(calls, start_exc)
        self.group = FakeGroup()
        self.engine = object()  # GroupLaunch is not an Engine; serve takes launch.engine
        self.calls = calls

    def start(self, *, conversation=None, **kw):
        if kw:
            self.calls.append(('launch.start.kw', kw))
        if conversation is None:
            self.calls.append(('launch.start', None))
            # one-shot: GroupLaunch.start derives audiences from the stored mode
            self.host.start(self.group.id, audiences={'seat1': ['seat2'], 'seat2': ['seat1']})
        else:
            self.calls.append(('launch.start', conversation))
            self.host.start(self.group.id, conversation=conversation)

    async def aclose(self):
        await asyncio.sleep(0)  # prove we are on the running loop
        self.calls.append(('launch.aclose',))


class FakeCore:
    """A fake exo.group_launch + exo.core.group_host + exo.server.app."""
    def __init__(self, monkeypatch, calls, create_exc=None, serve_exc=None, start_exc=None):
        import types
        self.launch = FakeLaunch(calls, start_exc)
        self.create_exc = create_exc

        def create(seat, *, profiles, mode='conductor', conductor=None):
            calls.append(('create', seat, list(profiles), mode, conductor))
            if self.create_exc:
                raise self.create_exc
            return self.launch

        gl = types.ModuleType('exo.group_launch')
        gl.LaunchError, gl.create = FakeLaunchError, create
        gh = types.ModuleType('exo.core.group_host')
        gh.ConversationOptions = FakeConversationOptions

        async def serve(engine, **kw):
            calls.append(('serve', kw))
            await asyncio.sleep(0)
            if serve_exc:
                raise serve_exc

        srv = types.ModuleType('exo.server.app')
        srv.serve = serve
        monkeypatch.setitem(sys.modules, 'exo.group_launch', gl)
        monkeypatch.setitem(sys.modules, 'exo.core.group_host', gh)
        monkeypatch.setitem(sys.modules, 'exo.server.app', srv)


def run_cmd(capsys, a):
    err = io.StringIO()
    with redirect_stderr(err):
        rc = swarm_cmd.cmd_swarm(a)
    return rc, err.getvalue(), capsys.readouterr().out


def test_defaults_do_not_start_or_serve_socket(monkeypatch, capsys):
    calls = []
    FakeCore(monkeypatch, calls)
    rc, err, out = run_cmd(capsys, ns(stdio=True))
    assert rc == 0
    assert calls[0] == ('create', None, ['seat1', 'seat2'], 'conductor', None)
    assert ('serve', {'stdio': True}) in calls
    assert calls[-1] == ('launch.aclose',)
    assert not any(c[0] in ('launch.start', 'host.start') for c in calls)  # no default-on
    assert out == ''  # stdio protocol keeps stdout clean


def test_banner_without_start_waits_for_explicit_group_start(monkeypatch, capsys):
    calls = []
    FakeCore(monkeypatch, calls)
    rc, err, _ = run_cmd(capsys, ns(stdio=True))
    assert rc == 0
    assert 'not started: waiting for an explicit principal /group start' in err
    assert 'one model turn per member scheduled' not in err


def test_start_flag_binds_engines_before_serving(monkeypatch, capsys):
    calls = []
    FakeCore(monkeypatch, calls)
    rc, _, _ = run_cmd(capsys, ns(stdio=True, start=True))
    order = [c[0] for c in calls]
    assert order == ['create', 'launch.start', 'host.start', 'serve', 'launch.aclose']


def test_banner_when_started_reports_one_turn_per_member(monkeypatch, capsys):
    calls = []
    FakeCore(monkeypatch, calls)
    rc, err, _ = run_cmd(capsys, ns(stdio=True, start=True))
    assert rc == 0
    assert 'started: one model turn per member scheduled (one-shot)' in err
    assert 'waiting for an explicit principal /group start' not in err


def test_mode_and_conductor_are_forwarded_to_create(monkeypatch, capsys):
    calls = []
    FakeCore(monkeypatch, calls)
    rc, _, _ = run_cmd(capsys, ns(stdio=True, mode='socratic'))
    assert rc == 0
    assert calls[0] == ('create', None, ['seat1', 'seat2'], 'socratic', None)
    calls.clear()
    run_cmd(capsys, ns(stdio=True, mode='conductor', conductor='seat2'))
    assert calls[0] == ('create', None, ['seat1', 'seat2'], 'conductor', 'seat2')


def test_conversation_start_forwards_bound_options(monkeypatch, capsys):
    calls = []
    FakeCore(monkeypatch, calls)
    rc, err, _ = run_cmd(capsys, ns(stdio=True, start=True, conversation=True,
                                    max_turns=4, idle_timeout=120.0))
    assert rc == 0
    # create keeps the default mode/conductor pair
    assert calls[0] == ('create', None, ['seat1', 'seat2'], 'conductor', None)
    # launch.start got a ConversationOptions carrying exactly the CLI bounds
    assert any(c[0] == 'launch.start' and isinstance(c[1], FakeConversationOptions)
               and c[1].max_turns == 4 and c[1].idle_timeout == 120.0 for c in calls)
    # and the host received that same conversation, not derived audiences
    host_start = [c for c in calls if c[0] == 'host.start']
    assert len(host_start) == 1 and 'conversation' in host_start[0][2]
    assert 'audiences' not in host_start[0][2]
    assert 'started: bounded reactive conversation running' in err
    assert 'max 4 turns per member' in err and 'idle timeout 120s' in err


def test_conversation_start_uses_core_defaults_when_bounds_omitted(monkeypatch, capsys):
    calls = []
    FakeCore(monkeypatch, calls)
    rc, err, _ = run_cmd(capsys, ns(stdio=True, start=True, conversation=True))
    assert rc == 0
    conv = [c[1] for c in calls if c[0] == 'launch.start' and isinstance(c[1], FakeConversationOptions)]
    assert conv and conv[0].max_turns == 8 and conv[0].idle_timeout == 300.0
    assert '(core default bounds)' in err


def test_socket_transport(monkeypatch, capsys):
    from pathlib import Path
    calls = []
    FakeCore(monkeypatch, calls)
    rc, err, _ = run_cmd(capsys, ns(socket='/tmp/swarm-test.sock'))
    assert rc == 0
    assert ('serve', {'socket': Path('/tmp/swarm-test.sock')}) in calls
    assert 'unix:/tmp/swarm-test.sock' in err


def test_create_failure_fails_closed_without_serving(monkeypatch, capsys):
    calls = []
    FakeCore(monkeypatch, calls, create_exc=FakeLaunchError('owner must not be a member'))
    rc, err, out = run_cmd(capsys, ns(stdio=True, seat='seat1'))
    assert rc == 1 and 'owner must not be a member' in err and out == ''
    assert [c[0] for c in calls] == ['create']


def test_serve_failure_still_closes_launch(monkeypatch, capsys):
    calls = []
    FakeCore(monkeypatch, calls, serve_exc=OSError('socket in use'))
    rc, err, _ = run_cmd(capsys, ns(stdio=True))
    assert rc == 1 and 'socket in use' in err
    assert calls[-1] == ('launch.aclose',)


def test_cancellation_still_closes_launch(monkeypatch, capsys):
    calls = []
    FakeCore(monkeypatch, calls, serve_exc=asyncio.CancelledError())
    rc, _, _ = run_cmd(capsys, ns(stdio=True, start=True))
    assert rc == 0
    assert [c[0] for c in calls] == ['create', 'launch.start', 'host.start', 'serve', 'launch.aclose']


def test_start_failure_never_serves(monkeypatch, capsys):
    calls = []
    FakeCore(monkeypatch, calls, start_exc=RuntimeError('engine factory blew up'))
    rc, err, _ = run_cmd(capsys, ns(stdio=True, start=True))
    assert rc == 1 and 'group start failed' in err
    assert [c[0] for c in calls] == ['create', 'launch.start', 'host.start', 'launch.aclose']


# --- plan flags: parser and fail-closed pairing (no core import, no create) -------

def test_parser_plan_flags_default_none():
    a = parse('--profiles', 'a,b')
    assert a.plan is None and a.grants is None


def test_parser_plan_flags_accept_paths():
    a = parse('--profiles', 'a,b', '--start', '--plan', 'p.json', '--grants', 'g.json')
    assert (a.plan, a.grants, a.start) == ('p.json', 'g.json', True)


@pytest.mark.parametrize('over, msg', [
    ({'plan': 'p.json', 'start': True}, '--plan and --grants must be given together'),
    ({'grants': 'g.json', 'start': True}, '--plan and --grants must be given together'),
    ({'plan': 'p.json', 'grants': 'g.json'}, '--plan/--grants require --start'),
    ({'plan': 'p.json', 'grants': 'g.json', 'start': True, 'conversation': True}, 'cannot be combined'),
    ({'plan': 'p.json', 'grants': 'g.json', 'start': True, 'max_turns': 3}, 'cannot be combined'),
    ({'plan': 'p.json', 'grants': 'g.json', 'start': True, 'idle_timeout': 5.0}, 'cannot be combined'),
])
def test_plan_flag_combinations_rejected_before_core(monkeypatch, capsys, over, msg):
    calls = []
    FakeCore(monkeypatch, calls)
    rc, err, out = run_cmd(capsys, ns(**over))
    assert rc == 1 and msg in err
    assert calls == []  # nothing created, no file read, nothing started
    assert out == ''


def test_legacy_start_never_passes_plan_kwargs(monkeypatch, capsys):
    calls = []
    FakeCore(monkeypatch, calls)
    rc, err, out = run_cmd(capsys, ns(stdio=True, start=True))
    assert rc == 0
    assert ('launch.start', None) in calls
    assert 'plan' not in err


# --- --knowledge: opt-in forwarding, omitted when false --------------------------------

def _start_kw(calls):
    return [c[1] for c in calls if c[0] == 'launch.start.kw']


def test_parser_knowledge_defaults_false_and_is_a_switch():
    assert parse('--profiles', 'a,b').knowledge is False
    assert parse('--profiles', 'a,b', '--start', '--knowledge').knowledge is True


def test_knowledge_one_shot_passes_true(monkeypatch, capsys):
    calls = []
    FakeCore(monkeypatch, calls)
    rc, err, _ = run_cmd(capsys, ns(stdio=True, start=True, knowledge=True))
    assert rc == 0, err
    assert _start_kw(calls) == [{'knowledge': True}]
    assert ('launch.start', None) in calls
    assert 'one-shot' in err and '(knowledge)' in err


def test_knowledge_conversation_passes_true_with_bounds(monkeypatch, capsys):
    calls = []
    FakeCore(monkeypatch, calls)
    rc, err, _ = run_cmd(capsys, ns(stdio=True, start=True, conversation=True, max_turns=4,
                                    knowledge=True))
    assert rc == 0, err
    assert _start_kw(calls) == [{'knowledge': True}]
    conv = [c[1] for c in calls if c[0] == 'launch.start']
    assert len(conv) == 1 and isinstance(conv[0], FakeConversationOptions) and conv[0].max_turns == 4
    assert '(knowledge)' in err


@pytest.mark.parametrize('over', [dict(), dict(conversation=True)])
@pytest.mark.parametrize('flagged', ['absent', 'false'])
def test_knowledge_false_omits_keyword(monkeypatch, capsys, over, flagged):
    calls = []
    FakeCore(monkeypatch, calls)
    a = ns(stdio=True, start=True, **over)
    if flagged == 'false':
        a.knowledge = False  # 'absent' keeps the Namespace without the attribute (older callers)
    rc, err, _ = run_cmd(capsys, a)
    assert rc == 0, err
    assert _start_kw(calls) == []
    assert 'knowledge' not in err


@pytest.mark.parametrize('over', [dict(), dict(conversation=True), dict(stdio=True)])
def test_knowledge_without_start_fails_before_core(monkeypatch, capsys, over):
    calls = []
    FakeCore(monkeypatch, calls)
    monkeypatch.setitem(sys.modules, 'exo.group_launch', None)
    rc, err, out = run_cmd(capsys, ns(knowledge=True, start=False, **over))
    assert rc == 1 and '--knowledge requires --start' in err
    assert calls == [] and out == ''
    assert 'no swarm group launch' not in err


def test_knowledge_start_failure_never_serves(monkeypatch, capsys):
    calls = []
    FakeCore(monkeypatch, calls, start_exc=RuntimeError('boom'))
    rc, err, _ = run_cmd(capsys, ns(stdio=True, start=True, knowledge=True))
    assert rc == 1 and 'group start failed' in err
    assert 'serve' not in [c[0] for c in calls] and calls[-1] == ('launch.aclose',)


def test_docstring_and_help_document_knowledge(capsys):
    assert '--knowledge' in swarm_cmd.__doc__ and '\u2014' not in swarm_cmd.__doc__
    with pytest.raises(SystemExit):
        parse('--help')
    assert '--knowledge' in capsys.readouterr().out
