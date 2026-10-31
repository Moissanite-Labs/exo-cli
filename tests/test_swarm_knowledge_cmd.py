"""Tests for `exo swarm-knowledge` (src/exo/swarm_knowledge_cmd.py).

Covers the four knowledge payloads, server/transport error reporting, local
validation that never touches the socket, and the lazy import registration.

Wire contract (swarm core `group` allowlists):
- payload keys are exactly ``id`` plus the op-specific fields; the operation
  name is only the leading ``args`` token (``knowledge_publish`` etc.), never a
  JSON key in the payload;
- publish: ``{id, key, text (str), sources (list), expected_revision (int)}``;
- read: ``{id, member, key}``; list: ``{id, member, limit}``;
  history: ``{id, member, key, limit}``;
- ``member`` is required for read/list/history;
- printed output is JSON that ``json.loads`` can parse.
"""
import argparse
import json
import subprocess
import sys

import pytest

from exo.protocol import schema as s
from exo import swarm_knowledge_cmd as m

COMMAND_MODULE = 'exo.swarm_knowledge_cmd'


class FakeClient:
    """Replaces exo.protocol.client.Client; records every request sent."""

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.requests = []
        self.responses = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, body):
        self.requests.append(body)
        if self.responses:
            return self.responses.pop(0)
        return s.Response(id='r1', ok=True, result={'output': '{"ok": true}'})


def _patch_client(monkeypatch, client):
    """Point ``_call``'s lazy ``Client`` import at one recorded instance.

    ``_call`` builds the client itself, so the patch is a factory that returns
    this instance every time; the test then reads the request it recorded.
    """
    monkeypatch.setattr('exo.protocol.client.Client', lambda *a, **kw: client)
    from exo.protocol.client import ClientError  # keep _call's import resolvable
    monkeypatch.setattr('exo.protocol.client.ClientError', ClientError)
    return client


@pytest.fixture
def fake_client(monkeypatch):
    return _patch_client(monkeypatch, FakeClient())


def ns(**kw):
    """An argparse.Namespace as the parser builds it (defaults per register())."""
    base = dict(swarm_knowledge_cmd='publish', group='g1', key=None, text=None,
                source=None, expected_revision=0, member=None, limit=100)
    base.update(kw)
    return argparse.Namespace(**base)


def _payload(req, action):
    assert isinstance(req, s.CommandRun)
    assert req.name == 'group'
    assert req.session is None
    prefix = f'knowledge_{action} '
    assert req.args.startswith(prefix)
    return json.loads(req.args[len(prefix):])


def test_publish_payload(fake_client):
    a = ns(swarm_knowledge_cmd='publish', group='grp-7', key='k1',
            text='hello world', source=['doc.md', 'rfc'], expected_revision=3)
    assert m.cmd_swarm_knowledge(a) == 0
    assert _payload(fake_client.requests[0], 'publish') == {
        'id': 'grp-7', 'key': 'k1',
        'text': 'hello world', 'sources': ['doc.md', 'rfc'],
        'expected_revision': 3}


def test_publish_payload_has_no_op_key(fake_client):
    a = ns(group='g', key='k', text='t')
    m.cmd_swarm_knowledge(a)
    payload = _payload(fake_client.requests[0], 'publish')
    assert 'op' not in payload
    assert set(payload) == {'id', 'key', 'text', 'sources', 'expected_revision'}


def test_publish_text_is_single_string(fake_client):
    a = ns(group='g', key='k', text='one string')
    m.cmd_swarm_knowledge(a)
    payload = _payload(fake_client.requests[0], 'publish')
    assert isinstance(payload['text'], str)
    assert payload['text'] == 'one string'
    assert isinstance(payload['sources'], list)


def test_publish_defaults(fake_client):
    a = ns(group='g', key='k', text='t')
    m.cmd_swarm_knowledge(a)
    payload = _payload(fake_client.requests[0], 'publish')
    assert payload['sources'] == []
    assert payload['expected_revision'] == 0


def test_read_payload(fake_client):
    a = ns(swarm_knowledge_cmd='read', group='g', key='k', member='alice')
    assert m.cmd_swarm_knowledge(a) == 0
    assert _payload(fake_client.requests[0], 'read') == {
        'id': 'g', 'key': 'k', 'member': 'alice'}


def test_read_payload_without_member_blocks(fake_client, capsys):
    a = ns(swarm_knowledge_cmd='read', group='g', key='k')
    assert m.cmd_swarm_knowledge(a) == 1
    assert fake_client.requests == []
    assert '--member is required' in capsys.readouterr().err


def test_list_payload(fake_client):
    a = ns(swarm_knowledge_cmd='list', group='g', member='alice', limit=42)
    m.cmd_swarm_knowledge(a)
    assert _payload(fake_client.requests[0], 'list') == {
        'id': 'g', 'member': 'alice', 'limit': 42}


def test_history_payload(fake_client):
    a = ns(swarm_knowledge_cmd='history', group='g', key='k',
            member='bob', limit=7)
    m.cmd_swarm_knowledge(a)
    assert _payload(fake_client.requests[0], 'history') == {
        'id': 'g', 'key': 'k', 'member': 'bob', 'limit': 7}


def test_output_is_json_parsable(fake_client, capsys):
    fake_client.responses.append(
        s.Response(id='r2', ok=True, result={'output': '{"key": "k", "rev": 1}'}))
    a = ns(swarm_knowledge_cmd='read', group='g', key='k', member='alice')
    assert m.cmd_swarm_knowledge(a) == 0
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == {'key': 'k', 'rev': 1}


def test_output_dict_is_serialized_as_json(fake_client, capsys):
    fake_client.responses.append(
        s.Response(id='r3', ok=True, result={'output': {'key': 'k', 'rev': 2}}))
    a = ns(swarm_knowledge_cmd='read', group='g', key='k', member='alice')
    assert m.cmd_swarm_knowledge(a) == 0
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == {'key': 'k', 'rev': 2}


def test_server_refusal_is_clean_stderr(fake_client, capsys):
    fake_client.responses.append(s.Response(
        id='r9', ok=False,
        error=s.ErrorInfo(code='knowledge_not_found', message='no such item')))
    a = ns(swarm_knowledge_cmd='read', group='g', key='missing', member='alice')
    assert m.cmd_swarm_knowledge(a) == 1
    err = capsys.readouterr().err
    assert 'exo swarm-knowledge: knowledge_not_found: no such item' in err
    assert 'Traceback' not in err


class FailingClient(FakeClient):
    async def send(self, body):
        self.requests.append(body)
        raise OSError('never reveal this: /private/tmp/core.socket')


def test_transport_error_is_sanitized(monkeypatch, capsys):
    _patch_client(monkeypatch, FailingClient())
    a = ns(swarm_knowledge_cmd='list', group='g', member='alice')
    assert m.cmd_swarm_knowledge(a) == 1
    err = capsys.readouterr().err
    assert 'cannot reach the swarm core on the local socket' in err
    assert 'never reveal this' not in err
    assert 'Traceback' not in err


@pytest.mark.parametrize('args,msg', [
    (dict(swarm_knowledge_cmd='publish', key='', text='x'), '--key must not be empty'),
    (dict(swarm_knowledge_cmd='publish', key='k', text=''), '--text is required and must be non-empty'),
    (dict(swarm_knowledge_cmd='publish', key='k', text=' '), '--text is required and must be non-empty'),
    (dict(swarm_knowledge_cmd='publish', key='k', text='x', expected_revision=-1),
     '--expected-revision must be a non-negative integer'),
    (dict(swarm_knowledge_cmd='read', key='k'), '--member is required'),
    (dict(swarm_knowledge_cmd='list', member='alice', limit=0), '--limit must be an integer in 1..1000'),
    (dict(swarm_knowledge_cmd='list', member='alice', limit=1001), '--limit must be an integer in 1..1000'),
    (dict(swarm_knowledge_cmd='list'), '--member is required'),
    (dict(swarm_knowledge_cmd='history', key='', member='alice', limit=5), '--key must not be empty'),
    (dict(swarm_knowledge_cmd='history', key='k', member='alice', limit=0), '--limit must be an integer in 1..1000'),
    (dict(swarm_knowledge_cmd='history', key='k', limit=5), '--member is required'),
    (dict(swarm_knowledge_cmd='read', key='', member='alice'), '--key must not be empty'),
])
def test_validation_blocks_before_transport(fake_client, capsys, args, msg):
    a = ns(**args)
    assert m.cmd_swarm_knowledge(a) == 1
    assert fake_client.requests == []
    assert msg in capsys.readouterr().err


def test_old_fakeclient_regression_publish(fake_client):
    """Old contract put the op in the payload and used plural-less fields; must never regress."""
    a = ns(group='g', key='k', text='t')
    m.cmd_swarm_knowledge(a)
    payload = _payload(fake_client.requests[0], 'publish')
    assert 'op' not in payload
    assert 'text' in payload and isinstance(payload['text'], str)
    assert 'sources' in payload
    assert 'source' not in payload


def test_old_fakeclient_regression_read_requires_member(fake_client, capsys):
    a = ns(swarm_knowledge_cmd='read', group='g', key='k')
    assert m.cmd_swarm_knowledge(a) == 1
    assert fake_client.requests == []
    assert '--member is required' in capsys.readouterr().err


def _loaded_after(code):
    probe = (f'import json, sys\n{code}\n'
             f'print(json.dumps({COMMAND_MODULE!r} in sys.modules))')
    out = subprocess.run([sys.executable, '-c', probe], capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.splitlines()[-1])


def test_import_loads_no_knowledge_module():
    assert _loaded_after('import exo.cli') is False


def test_swarm_knowledge_help_loads_its_module():
    code = ('import contextlib, io\nfrom exo import cli\n'
            'with contextlib.redirect_stdout(io.StringIO()), contextlib.suppress(SystemExit):\n'
            '    cli.main(["swarm-knowledge", "--help"])')
    assert _loaded_after(code) is True


def test_lazy_registration_matches_register():
    from exo import cli
    mod, handler, help_line = cli.LAZY['swarm-knowledge']
    assert (mod, handler) == ('swarm_knowledge_cmd', 'cmd_swarm_knowledge')
    assert help_line == 'group knowledge: publish, read, list, history [experimental]'
    # the lazy getattr must resolve the handler (module imported on access)
    assert callable(getattr(cli, 'cmd_swarm_knowledge'))
