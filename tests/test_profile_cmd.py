"""exo profile create/digest/update: parser registration, lazy core import, fail-closed errors."""
import argparse
import subprocess
import sys
import types
from pathlib import Path

import pytest

from exo import cli, profile_cmd

DIGEST = 'sha256:' + 'ab' * 32


def ns(**over):
    a = argparse.Namespace(cmd='profile', profile_cmd='create', name='seat1',
                           role='', description='', home=None)
    return argparse.Namespace(**{**vars(a), **over})


def uns(**over):
    a = argparse.Namespace(cmd='profile', profile_cmd='update', name='seat1',
                           expected_digest=DIGEST, role=None, description='', home=None)
    return argparse.Namespace(**{**vars(a), **over})


def parse(*argv):
    p = cli.build_parser('profile')
    return p.parse_args(['profile', *argv])


# --- parser / registration ----------------------------------------------------

def test_registered_in_lazy():
    assert cli.LAZY['profile'][:2] == ('profile_cmd', 'cmd_profile')
    assert 'profile' not in cli.STORE_CMDS


def test_profile_in_lazy_command_modules_probe():
    from tests.test_cli_lazy import COMMAND_MODULES
    assert 'exo.profile_cmd' in COMMAND_MODULES


def test_parser_requires_nested_subcommand():
    with pytest.raises(SystemExit):
        parse()
    with pytest.raises(SystemExit):
        parse('--role', 'owner')  # a flag without the create subcommand still fails


def test_parser_create_defaults():
    a = parse('create', 'seat1')
    assert a.profile_cmd == 'create' and a.name == 'seat1'
    assert a.role == '' and a.description == '' and a.home is None


def test_parser_create_overrides():
    a = parse('create', 'seat1', '--role', 'owner', '--description', 'the owner',
              '--home', '/tmp/profiles')
    assert a.role == 'owner' and a.description == 'the owner' and a.home == '/tmp/profiles'


def test_parser_digest():
    a = parse('digest', 'seat1')
    assert a.profile_cmd == 'digest' and a.name == 'seat1' and a.home is None
    a = parse('digest', 'seat1', '--home', '/tmp/profiles')
    assert a.home == '/tmp/profiles'


def test_parser_update_defaults_and_overrides():
    a = parse('update', 'seat1', '--expected-digest', DIGEST)
    assert a.profile_cmd == 'update' and a.name == 'seat1'
    assert a.expected_digest == DIGEST
    assert a.role is None and a.description is None and a.home is None
    a = parse('update', 'seat1', '--expected-digest', DIGEST, '--role', 'owner',
              '--description', '', '--home', '/tmp/profiles')
    assert a.role == 'owner' and a.description == '' and a.home == '/tmp/profiles'


def test_parser_update_requires_expected_digest():
    with pytest.raises(SystemExit):
        parse('update', 'seat1', '--role', 'owner')


def test_create_help_shows_options_once(capsys):
    with pytest.raises(SystemExit):
        parse('create', '--help')
    help_text = capsys.readouterr().out
    assert '--role' in help_text and '--description' in help_text and '--home' in help_text
    # common args come from parents=[common] exactly once each (no duplicate definitions).
    # Count definition lines, not raw substrings: --root's help text mentions --principal.
    defs = [ln.strip().split()[0] for ln in help_text.splitlines() if ln.startswith('  --')]
    assert defs.count('--principal') == 1
    assert defs.count('--root') == 1


def test_update_help_lists_expected_digest_once(capsys):
    with pytest.raises(SystemExit):
        parse('update', '--help')
    help_text = capsys.readouterr().out
    assert '--expected-digest' in help_text
    defs = [ln.strip().split()[0] for ln in help_text.splitlines() if ln.startswith('  --')]
    assert defs.count('--expected-digest') == 1
    assert defs.count('--role') == 1
    assert defs.count('--root') == 1


def test_profile_help_matches_full_parser(capsys):
    from tests.test_cli_lazy import FULL, walk

    with pytest.raises(SystemExit):
        parse('--help')
    assert capsys.readouterr().out == walk(FULL, ('profile',)).format_help()


@pytest.mark.parametrize('sub', ['create', 'digest', 'update'])
def test_subcommand_help_matches_full_parser(capsys, sub):
    from tests.test_cli_lazy import FULL, walk

    with pytest.raises(SystemExit):
        parse(sub, '--help')
    assert capsys.readouterr().out == walk(FULL, ('profile', sub)).format_help()


def test_import_loads_no_core_profile_module():
    """The core profile module must load only when a subcommand runs, not on import."""
    probe = ('import json, sys\nimport exo.profile_cmd\n'
             'print(json.dumps("exo.profile_management" in sys.modules))')
    out = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[-1] == 'false'


def test_help_needs_no_core_import():
    """`--help` for every profile subcommand must work without the core module."""
    code = ('import sys\n'
            'class Block:\n'
            '    def find_module(self, name, path=None):\n'
            '        return self if name == "exo.profile_management" else None\n'
            '    def find_spec(self, name, path=None, target=None):\n'
            '        if name == "exo.profile_management":\n'
            '            raise ModuleNotFoundError("blocked", name=name)\n'
            '        return None\n'
            'sys.meta_path.insert(0, Block())\n'
            'from exo import cli\n'
            'for sub in ((), ("create",), ("digest",), ("update",)):\n'
            '    try:\n'
            '        cli.build_parser("profile").parse_args(["profile", *sub, "--help"])\n'
            '    except SystemExit as e:\n'
            '        assert e.code == 0, e.code\n'
            'print("ok")\n')
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == 'ok'


def test_docstring_documents_core_dependency():
    doc = profile_cmd.__doc__ or ''
    assert '#1021' in doc and '#1022' in doc


# --- fake core via sys.modules ------------------------------------------------

class FakeProfileError(RuntimeError):
    pass


class FakeProfileConflict(FakeProfileError):
    pass


class FakeSeat:
    def __init__(self, path):
        self.path = path


def install_fake_core(monkeypatch, calls, error=None, names=('create_profile',)):
    pm = types.ModuleType('exo.profile_management')

    def create_profile(home, name, *, role='', description=''):
        calls.append(('create_profile', home, name, role, description))
        if error:
            raise error
        return FakeSeat(home / 'seats' / f'{name}.toml')

    def profile_digest(home, name):
        calls.append(('profile_digest', home, name))
        if error:
            raise error
        return DIGEST

    def update_profile(home, name, *, expected_digest, role=None, description=None):
        calls.append(('update_profile', home, name, expected_digest, role, description))
        if error:
            raise error
        return FakeSeat(home / 'seats' / f'{name}.toml')

    pm.ProfileError = FakeProfileError
    pm.ProfileConflict = FakeProfileConflict
    for attr in names:
        setattr(pm, attr, {'create_profile': create_profile, 'profile_digest': profile_digest,
                           'update_profile': update_profile}[attr])
    monkeypatch.setitem(sys.modules, 'exo.profile_management', pm)


def test_create_forwarded_defaults(monkeypatch, capsys):
    from exo.store import exo_home
    calls = []
    install_fake_core(monkeypatch, calls)
    assert profile_cmd.cmd_profile(ns()) == 0
    assert calls == [('create_profile', Path(exo_home()).expanduser(), 'seat1', '', '')]
    assert 'created profile seat1 at' in capsys.readouterr().out


def test_create_forwarded_overrides(monkeypatch, capsys):
    calls = []
    install_fake_core(monkeypatch, calls)
    assert profile_cmd.cmd_profile(
        ns(role='owner', description='the owner', home='/tmp/profiles')) == 0
    assert calls == [('create_profile', Path('/tmp/profiles'), 'seat1', 'owner', 'the owner')]


def test_create_success_prints_seat_path(monkeypatch, capsys):
    calls = []
    install_fake_core(monkeypatch, calls)
    assert profile_cmd.cmd_profile(ns(home='/tmp/profiles')) == 0
    out = capsys.readouterr().out
    assert '/tmp/profiles/seats/seat1.toml' in out


def test_profile_error_fails_closed(monkeypatch, capsys):
    calls = []
    install_fake_core(monkeypatch, calls, error=FakeProfileError('seat already exists'))
    assert profile_cmd.cmd_profile(ns()) == 1
    assert 'seat already exists' in capsys.readouterr().err


# --- digest -------------------------------------------------------------------

def test_digest_prints_only_the_digest(monkeypatch, capsys):
    from exo.store import exo_home
    calls = []
    install_fake_core(monkeypatch, calls, names=('profile_digest',))
    assert profile_cmd.cmd_profile(ns(profile_cmd='digest')) == 0
    captured = capsys.readouterr()
    assert captured.out == DIGEST + '\n'
    assert captured.err == ''
    assert calls == [('profile_digest', Path(exo_home()).expanduser(), 'seat1')]


def test_digest_forwarded_home(monkeypatch, capsys):
    calls = []
    install_fake_core(monkeypatch, calls, names=('profile_digest',))
    assert profile_cmd.cmd_profile(
        ns(profile_cmd='digest', home='/tmp/profiles')) == 0
    assert calls == [('profile_digest', Path('/tmp/profiles'), 'seat1')]


def test_digest_error_forwarded(monkeypatch, capsys):
    calls = []
    install_fake_core(monkeypatch, calls, error=FakeProfileError('no profile seat1'),
                      names=('profile_digest',))
    assert profile_cmd.cmd_profile(ns(profile_cmd='digest')) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'exo profile: no profile seat1\n'


# --- update -------------------------------------------------------------------

def test_update_forwarded(monkeypatch, capsys):
    calls = []
    install_fake_core(monkeypatch, calls, names=('update_profile',))
    assert profile_cmd.cmd_profile(uns(home='/tmp/profiles')) == 0
    assert calls == [('update_profile', Path('/tmp/profiles'), 'seat1', DIGEST, None, '')]
    assert capsys.readouterr().out == 'exo profile: updated profile seat1 at /tmp/profiles/seats/seat1.toml\n'


def test_update_keep_current_fields_are_none(monkeypatch, capsys):
    calls = []
    install_fake_core(monkeypatch, calls, names=('update_profile',))
    assert profile_cmd.cmd_profile(uns(role='owner', description=None, home='/tmp/profiles')) == 0
    assert calls == [('update_profile', Path('/tmp/profiles'), 'seat1', DIGEST, 'owner', None)]


def test_update_clear_field_is_empty_string(monkeypatch, capsys):
    calls = []
    install_fake_core(monkeypatch, calls, names=('update_profile',))
    assert profile_cmd.cmd_profile(uns(role='', description=None, home='/tmp/profiles')) == 0
    assert calls == [('update_profile', Path('/tmp/profiles'), 'seat1', DIGEST, '', None)]


def test_update_stale_digest_is_rc1_with_prefix(monkeypatch, capsys):
    calls = []
    install_fake_core(monkeypatch, calls,
                      error=FakeProfileConflict('profile seat1 has changed since expected_digest'),
                      names=('update_profile',))
    assert profile_cmd.cmd_profile(uns(home='/tmp/profiles')) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == ('exo profile: profile seat1 has changed since expected_digest\n')


def test_update_conflict_subclasses_profile_error(monkeypatch, capsys):
    """A real core's ProfileConflict is a ProfileError, so it is caught by the same handler."""
    calls = []
    install_fake_core(monkeypatch, calls, error=FakeProfileConflict('locked'),
                      names=('update_profile',))
    assert issubclass(FakeProfileConflict, FakeProfileError)
    assert profile_cmd.cmd_profile(uns()) == 1
    assert 'locked' in capsys.readouterr().err


def test_update_needs_a_field_and_touches_nothing(monkeypatch, capsys):
    """No --role/--description: refused before the core is imported or --home resolved."""
    calls = []
    install_fake_core(monkeypatch, calls, names=('update_profile',))

    def boom(*_a, **_k):
        raise AssertionError('the core must not be called')

    # Guard installed before the call: a field-less update must never resolve _home.
    monkeypatch.setattr(profile_cmd, '_home', boom)

    assert profile_cmd.cmd_profile(uns(role=None, description=None, home='/nonexistent')) == 1
    assert calls == []
    assert '--role' in capsys.readouterr().err


def test_update_no_field_error_works_without_core(monkeypatch, capsys):
    monkeypatch.delitem(sys.modules, 'exo.profile_management', raising=False)
    assert profile_cmd.cmd_profile(uns(role=None, description=None)) == 1
    assert 'update needs' in capsys.readouterr().err


def test_malformed_expected_digest_is_not_validated_locally(monkeypatch, capsys):
    """Form validation is the core's job; the CLI forwards and reports its refusal."""
    calls = []
    install_fake_core(monkeypatch, calls,
                      error=FakeProfileError("expected_digest must look like 'sha256:<64 lowercase hex>'"),
                      names=('update_profile',))
    assert profile_cmd.cmd_profile(uns(expected_digest='nope', home='/tmp/profiles')) == 1
    assert calls == [('update_profile', Path('/tmp/profiles'), 'seat1', 'nope', None, '')]
    assert 'sha256' in capsys.readouterr().err


# --- missing core / missing API -----------------------------------------------

def test_missing_core_module_is_a_useful_error(monkeypatch, capsys):
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == 'exo.profile_management':
            raise ModuleNotFoundError("No module named 'exo.profile_management'",
                                      name='exo.profile_management')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr('builtins.__import__', fake_import)
    monkeypatch.delitem(sys.modules, 'exo.profile_management', raising=False)
    assert profile_cmd.cmd_profile(ns()) == 1
    err = capsys.readouterr().err
    assert 'no profile management support' in err
    assert '#1021' in err


@pytest.mark.parametrize('sub', ['digest', 'update'])
def test_missing_core_module_for_revision_commands(monkeypatch, capsys, sub):
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == 'exo.profile_management':
            raise ModuleNotFoundError("No module named 'exo.profile_management'",
                                      name='exo.profile_management')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr('builtins.__import__', fake_import)
    monkeypatch.delitem(sys.modules, 'exo.profile_management', raising=False)
    a = ns(profile_cmd=sub) if sub == 'digest' else uns()
    assert profile_cmd.cmd_profile(a) == 1
    err = capsys.readouterr().err
    assert 'digest/update support' in err and '#1022' in err


def test_other_module_not_found_reraises(monkeypatch):
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == 'exo.profile_management':
            raise ModuleNotFoundError('No module named missing_dep', name='missing_dep')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr('builtins.__import__', fake_import)
    monkeypatch.delitem(sys.modules, 'exo.profile_management', raising=False)
    with pytest.raises(ModuleNotFoundError):
        profile_cmd.cmd_profile(ns())


def test_core_without_capability_is_an_explicit_error(monkeypatch, capsys):
    """A core that only ships create_profile must not look like one that can update."""
    calls = []
    install_fake_core(monkeypatch, calls, names=('create_profile',))
    assert profile_cmd.cmd_profile(uns(home='/tmp/profiles')) == 1
    err = capsys.readouterr().err
    assert '#1022' in err and 'update_profile' in err
    assert profile_cmd.cmd_profile(ns(profile_cmd='digest', home='/tmp/profiles')) == 1
    err = capsys.readouterr().err
    assert '#1022' in err and 'profile_digest' in err
    assert calls == []


def test_create_still_works_on_a_create_only_core(monkeypatch, capsys):
    calls = []
    install_fake_core(monkeypatch, calls, names=('create_profile',))
    assert profile_cmd.cmd_profile(ns(home='/tmp/profiles')) == 0
    assert 'created profile seat1' in capsys.readouterr().out


def test_no_config_or_store_auth_is_read(monkeypatch, capsys):
    """Neither subcommand imports the store's config/auth surface beyond exo_home()."""
    calls = []
    install_fake_core(monkeypatch, calls, names=('create_profile', 'profile_digest', 'update_profile'))
    monkeypatch.setenv('EXO_HOME', '/tmp/profiles')
    assert profile_cmd.cmd_profile(ns(profile_cmd='digest')) == 0
    assert profile_cmd.cmd_profile(uns(description=None, role='owner')) == 0
    assert profile_cmd.cmd_profile(ns()) == 0
    homes = {call[1] for call in calls}
    assert homes == {Path('/tmp/profiles')}
