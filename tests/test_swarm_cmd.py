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
                           stdio=False, mode=None, conductor=None, start=False,
                           max_turns=None, idle_timeout=None)
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


def test_no_mode_flags_until_core_supports_mode():
    """group_launch.create hardcodes conductor mode; --mode is an argparse error, never a fake."""
    with pytest.raises(SystemExit):
        parse('--profiles', 'a,b', '--mode', 'socratic')


def test_start_help_says_one_turn_per_member_immediately(capsys):
    with pytest.raises(SystemExit):
        parse('--help')
    help_text = capsys.readouterr().out
    assert 'run one model turn per member immediately' in help_text
    assert 'no run occurs until a principal /group start' in help_text


def test_docstring_has_no_em_dashes():
    assert '\u2014' not in (swarm_cmd.__doc__ or '')


def test_docstring_documents_principal_controls_and_first_profile_conductor():
    doc = swarm_cmd.__doc__ or ''
    assert 'first profile' in doc and 'conductor' in doc
    assert 'principal' in doc and 'Unix socket' in doc
    assert 'stdio protocol alone cannot grant principal' in doc


# --- fail-closed validation (no core imported, nothing allocated) -------------

def test_empty_profile_entry_fails_closed(capsys):
    assert swarm_cmd.cmd_swarm(ns(profiles='seat1,,seat2')) == 1
    assert 'empty entry' in capsys.readouterr().err


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
    def __init__(self, *, max_turns, idle_timeout):
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

    def start(self):
        self.calls.append(('launch.start',))
        # GroupLaunch.start delegates to the host with audiences derived from the stored mode.
        self.host.start(self.group.id, audiences={'seat1': ['seat2'], 'seat2': ['seat1']})

    async def aclose(self):
        await asyncio.sleep(0)  # prove we are on the running loop
        self.calls.append(('launch.aclose',))


class FakeCore:
    """A fake exo.group_launch + exo.core.group_host + exo.server.app."""
    def __init__(self, monkeypatch, calls, create_exc=None, serve_exc=None, start_exc=None):
        import types
        self.launch = FakeLaunch(calls, start_exc)
        self.create_exc = create_exc

        def create(seat, *, profiles):
            calls.append(('create', seat, list(profiles)))
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
    assert calls[0] == ('create', None, ['seat1', 'seat2'])
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
    assert 'already run' not in err


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
    assert 'started: one model turn per member already run' in err
    assert 'waiting for an explicit principal /group start' not in err


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
