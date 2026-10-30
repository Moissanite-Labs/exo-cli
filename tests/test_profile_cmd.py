"""exo profile create: parser registration, lazy core import, fail-closed errors."""
import argparse
import subprocess
import sys
import types
from pathlib import Path

import pytest

from exo import cli, profile_cmd


def ns(**over):
    a = argparse.Namespace(cmd='profile', profile_cmd='create', name='seat1',
                           role='', description='', home=None)
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


def test_parser_requires_nested_create():
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


def test_profile_help_matches_full_parser(capsys):
    from tests.test_cli_lazy import FULL, walk

    with pytest.raises(SystemExit):
        parse('--help')
    assert capsys.readouterr().out == walk(FULL, ('profile',)).format_help()


def test_import_loads_no_core_profile_module():
    """The core profile module must load only when `create` runs, not on import."""
    probe = ('import json, sys\nimport exo.profile_cmd\n'
             'print(json.dumps("exo.profile_management" in sys.modules))')
    out = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[-1] == 'false'


def test_docstring_documents_core_dependency():
    assert '#1021' in (profile_cmd.__doc__ or '')


# --- fake core via sys.modules ------------------------------------------------

class FakeProfileError(RuntimeError):
    pass


class FakeSeat:
    def __init__(self, path):
        self.path = path


def install_fake_core(monkeypatch, calls, error=None):
    pm = types.ModuleType('exo.profile_management')

    def create_profile(home, name, *, role='', description=''):
        calls.append(('create_profile', home, name, role, description))
        if error:
            raise error
        return FakeSeat(home / 'seats' / f'{name}.toml')

    pm.create_profile = create_profile
    pm.ProfileError = FakeProfileError
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
