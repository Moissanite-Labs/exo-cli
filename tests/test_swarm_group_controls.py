"""Offline unit tests for `exo swarm-group status|pause|resume|stop` (src/exo/swarm_group_cmd.py).

The core client is replaced by the recording fake from test_swarm_group_cmd, so no socket
opens here. The real-socket tests live in tests/test_swarm_group_controls_socket.py.

Wire contract (swarm core group_commands, read-only authority):
- status: args 'status {"id": G}' -> {id, owner, members[{profile,state}], local_runtime,
  reconciliation_required, receipt: None | {id,state,created,finished,members{profile:state}}}
- pause/resume/stop: 'op {"id": G[, "member": P]}' -> {id, paused|resumed|stopped: true, member: P|None}
"""
import json

import pytest

import test_swarm_group_cmd as base  # also sets the exo import path
from test_swarm_group_cmd import G, ns, ok, refuse, req, run
from exo import swarm_group_cmd as m  # noqa: E402

fake = base.fake
climod = base.climod

SOCK = '/tmp/some-core/exod.sock'
DONE = {'pause': 'paused', 'resume': 'resumed', 'stop': 'stopped'}


def status_out(**kw):
    out = {'id': G, 'owner': 'owner',
           'members': [{'profile': 'builder', 'state': 'idle'}, {'profile': 'checker', 'state': 'idle'}],
           'local_runtime': False, 'reconciliation_required': False, 'receipt': None}
    out.update(kw)
    return out


def receipt(**kw):
    row = {'id': 'gx_1', 'state': 'settled', 'created': '2026-01-01T00:00:00.000Z',
           'finished': '2026-01-01T00:01:00.000Z', 'members': {'builder': 'completed', 'checker': 'stopped'}}
    row.update(kw)
    return row


def ctl_out(op, member=None, **kw):
    out = {'id': G, DONE[op]: True, 'member': member}
    out.update(kw)
    return out


def ctl_ns(op, member=None, socket=SOCK, group=G):
    return ns(op, group=group, member=member, socket=socket)


def status_ns(socket=SOCK, group=G):
    return ns('status', group=group, socket=socket)


# ---- payloads and transport ---------------------------------------------------

def test_status_payload_and_output(fake, capsys):
    out = status_out(receipt=receipt(), local_runtime=True)
    fake.responses = [ok(json.dumps(out))]
    rc, stdout, err = run(status_ns(), capsys)
    assert rc == 0 and err == ''
    assert req(fake.requests[0]) == ('status', {'id': G})
    assert json.loads(stdout) == out


@pytest.mark.parametrize('op', ['pause', 'resume', 'stop'])
def test_control_without_member_omits_the_key(op, fake, capsys):
    fake.responses = [ok(ctl_out(op))]
    rc, stdout, err = run(ctl_ns(op), capsys)
    assert rc == 0 and err == ''
    assert req(fake.requests[0]) == (op, {'id': G})
    assert json.loads(stdout) == ctl_out(op)


@pytest.mark.parametrize('op', ['pause', 'resume', 'stop'])
def test_control_with_member_sends_it(op, fake, capsys):
    fake.responses = [ok(json.dumps(ctl_out(op, 'builder')))]
    rc, stdout, _ = run(ctl_ns(op, 'builder'), capsys)
    assert rc == 0
    assert req(fake.requests[0]) == (op, {'id': G, 'member': 'builder'})
    assert json.loads(stdout)['member'] == 'builder'


@pytest.mark.parametrize('op', ['status', 'pause', 'resume', 'stop'])
def test_payload_never_carries_owner_or_socket(op, fake, capsys):
    fake.responses = [ok(status_out() if op == 'status' else ctl_out(op))]
    run(status_ns() if op == 'status' else ctl_ns(op), capsys)
    r = fake.requests[0]
    assert set(req(r)[1]) <= {'id', 'member'} and 'owner' not in r.args and 'sock' not in r.args


def test_client_gets_explicit_socket_and_chat_principal(monkeypatch, capsys):
    seen = []
    client = base.FakeClient()
    client.responses = [ok(status_out())]
    monkeypatch.setattr('exo.protocol.client.Client', lambda *a, **kw: seen.append((a, kw)) or client)
    assert run(status_ns(), capsys)[0] == 0
    assert seen == [((), {'socket': SOCK, 'principal': 'chat'})]


def test_socket_is_user_expanded(monkeypatch, capsys):
    seen = []
    client = base.FakeClient()
    client.responses = [ok(ctl_out('pause'))]
    monkeypatch.setenv('HOME', '/tmp/fakehome')
    monkeypatch.setattr('exo.protocol.client.Client', lambda *a, **kw: seen.append(kw) or client)
    assert run(ctl_ns('pause', socket='~/x.sock'), capsys)[0] == 0
    assert seen[0]['socket'] == '/tmp/fakehome/x.sock'


def test_default_socket_never_used_for_lifecycle(fake, capsys, monkeypatch):
    monkeypatch.setattr('exo.cli_chat.default_socket',
                        lambda: (_ for _ in ()).throw(AssertionError('default socket used')))
    fake.responses = [ok(status_out())]
    assert run(status_ns(), capsys)[0] == 0


@pytest.mark.parametrize('op', ['status', 'pause', 'resume', 'stop'])
@pytest.mark.parametrize('sock', [None, '', '  ', 'a\nb', 'x' * 4097, 5])
def test_lifecycle_requires_valid_socket_before_dispatch(op, sock, fake, capsys):
    a = status_ns(socket=sock) if op == 'status' else ctl_ns(op, socket=sock)
    rc, out, err = run(a, capsys)
    assert rc == 1 and out == '' and err.startswith('exo swarm-group: ') and '--socket' in err
    assert fake.requests == [] and fake.entered == 0


def test_existing_ops_accept_optional_socket(monkeypatch, capsys):
    seen = []
    client = base.FakeClient()
    client.responses = [ok({'groups': []}), ok({'groups': []})]
    monkeypatch.setattr('exo.protocol.client.Client', lambda *a, **kw: seen.append(kw['socket']) or client)
    monkeypatch.setattr('exo.cli_chat.default_socket', lambda: __import__('pathlib').Path('/dflt/exod.sock'))
    assert run(ns('list'), capsys)[0] == 0
    assert run(ns('list', socket='/explicit.sock'), capsys)[0] == 0
    assert seen == ['/dflt/exod.sock', '/explicit.sock']


# ---- input checks ---------------------------------------------------------------

@pytest.mark.parametrize('group', ['', '   ', 'a\x1bb', 'x' * 129, None, 7])
@pytest.mark.parametrize('op', ['status', 'pause', 'resume', 'stop'])
def test_bad_group_never_dispatches(op, group, fake, capsys):
    a = status_ns(group=group) if op == 'status' else ctl_ns(op, group=group)
    rc, out, err = run(a, capsys)
    assert rc == 1 and out == '' and 'GROUP' in err and fake.requests == []


@pytest.mark.parametrize('member', ['', '  ', 'a\x00b', 'x' * 129, 5])
@pytest.mark.parametrize('op', ['pause', 'resume', 'stop'])
def test_bad_member_never_dispatches(op, member, fake, capsys):
    rc, out, err = run(ctl_ns(op, member), capsys)
    assert rc == 1 and out == '' and '--member' in err and fake.requests == []


def test_status_ignores_a_stray_member_attribute(fake, capsys):
    fake.responses = [ok(status_out())]
    rc, _, _ = run(ns('status', group=G, socket=SOCK, member='builder'), capsys)
    assert rc == 0 and req(fake.requests[0]) == ('status', {'id': G})


# ---- argparse wiring -------------------------------------------------------------

@pytest.mark.parametrize('argv, bad', [
    (['status', G], '--socket'), (['pause', G], '--socket'), (['resume', G], '--socket'),
    (['stop', G], '--socket'), (['status'], 'group'), (['stop'], 'group'),
    (['status', G, '--socket', SOCK, '--member', 'x'], '--member'),
    (['pause', G, '--socket', SOCK, '--owner', 'x'], '--owner'),
    (['pause', G, '--socket', SOCK, '--member', ''], '--member'),
    (['pause', G, '--socket', ''], '--socket'),
    (['observe', G, '--member', 'x'], '--member'),
    (['list', '--member', 'x'], '--member'),
])
def test_parser_rejects_before_dispatch(argv, bad, capsys, monkeypatch, climod):
    called = []
    monkeypatch.setattr(m, 'cmd_swarm_group', lambda a: called.append(a) or 0)
    with pytest.raises(SystemExit) as exc:
        climod.main(['swarm-group', *argv])
    assert exc.value.code == 2 and called == [] and bad in capsys.readouterr().err


@pytest.mark.parametrize('op', ['pause', 'resume', 'stop'])
def test_parser_dispatches_control(op, fake, capsys, climod):
    fake.responses = [ok(ctl_out(op, 'builder'))]
    rc = climod.main(['swarm-group', op, G, '--member', 'builder', '--socket', SOCK])
    assert rc == 0 and req(fake.requests[0]) == (op, {'id': G, 'member': 'builder'})


def test_parser_dispatches_status(fake, capsys, climod):
    fake.responses = [ok(status_out())]
    assert climod.main(['swarm-group', 'status', G, '--socket', SOCK]) == 0
    assert req(fake.requests[0]) == ('status', {'id': G})


def test_parser_optional_socket_on_existing_ops(monkeypatch, capsys, climod):
    got = []
    monkeypatch.setattr(m, 'cmd_swarm_group', lambda a: got.append(a) or 0)
    assert climod.main(['swarm-group', 'list', '--socket', SOCK]) == 0
    assert climod.main(['swarm-group', 'list']) == 0
    assert got[0].socket == SOCK and got[1].socket is None


# ---- strict response checking ---------------------------------------------------------

BAD_STATUS = [
    status_out(id='g_other'), status_out(owner=''), status_out(owner=None),
    status_out(local_runtime=1), status_out(reconciliation_required='no'), status_out(members={}),
    status_out(members=[{'profile': 'a', 'state': 'idle'}, {'profile': 'a', 'state': 'idle'}]),
    status_out(members=[{'profile': 'a'}]), status_out(members=[{'profile': 'a', 'state': 'idle', 'x': 1}]),
    status_out(members=[{'profile': 'a', 'state': ''}]), status_out(members=['a']),
    status_out(receipt=[]), status_out(receipt=receipt(id='')), status_out(receipt=receipt(finished=5)),
    status_out(receipt=receipt(members=[])), status_out(receipt=receipt(members={'a': 3})),
    status_out(receipt={k: v for k, v in receipt().items() if k != 'created'}),
    status_out(receipt=dict(receipt(), extra=1)), dict(status_out(), extra=1),
    {k: v for k, v in status_out().items() if k != 'receipt'},
]


@pytest.mark.parametrize('out', BAD_STATUS)
def test_status_rejects_malformed(out, fake, capsys):
    fake.responses = [ok(out)]
    rc, stdout, err = run(status_ns(), capsys)
    assert rc == 1 and stdout == '' and err == 'exo swarm-group: malformed status response from the swarm core\n'


def test_status_accepts_active_receipt_without_finish(fake, capsys):
    fake.responses = [ok(status_out(receipt=receipt(state='active', finished=None),
                                    members=[{'profile': 'builder', 'state': 'unknown'}],
                                    reconciliation_required=True))]
    assert run(status_ns(), capsys)[0] == 0


@pytest.mark.parametrize('op', ['pause', 'resume', 'stop'])
@pytest.mark.parametrize('out, member', [
    (lambda op: ctl_out(op, id='g_other'), None),
    (lambda op: ctl_out(op, member='builder'), None),     # member echoed that was never asked
    (lambda op: ctl_out(op), 'builder'),                  # member asked, answer omits it
    (lambda op: ctl_out(op, 'other'), 'builder'),
    (lambda op: {**ctl_out(op), DONE[op]: False}, None),
    (lambda op: {**ctl_out(op), DONE[op]: 1}, None),
    (lambda op: {**ctl_out(op), DONE[op]: 'true'}, None),
    (lambda op: dict(ctl_out(op), extra=1), None),
    (lambda op: {k: v for k, v in ctl_out(op).items() if k != 'member'}, None),
    (lambda op: {k: v for k, v in ctl_out(op).items() if k != 'id'}, None),
    (lambda op: {'id': G, 'member': None, 'ok': True}, None),
    (lambda op: ctl_out({'pause': 'stop', 'resume': 'pause', 'stop': 'resume'}[op]), None),  # wrong verb
])
def test_control_rejects_malformed(op, out, member, fake, capsys):
    fake.responses = [ok(out(op))]
    rc, stdout, err = run(ctl_ns(op, member), capsys)
    assert rc == 1 and stdout == '' and err == f'exo swarm-group: malformed {op} response from the swarm core\n'


@pytest.mark.parametrize('raw', ['', 'null', '[]', '"x"', '{', 'NaN',
                                 '{"id": "%s", "paused": true, "paused": true, "member": null}' % G])
def test_control_rejects_non_object_or_duplicate_json(raw, fake, capsys):
    fake.responses = [ok(raw)]
    rc, _, err = run(ctl_ns('pause'), capsys)
    assert rc == 1 and err == 'exo swarm-group: malformed pause response from the swarm core\n'


@pytest.mark.parametrize('op', ['status', 'pause', 'resume', 'stop'])
def test_core_refusal_is_cleaned_and_passed_through(op, fake, capsys):
    fake.responses = [refuse('forbidden', 'no trusted group host\x1b[31m\nnext')]
    rc, out, err = run(status_ns() if op == 'status' else ctl_ns(op), capsys)
    assert rc == 1 and out == ''
    assert err == 'exo swarm-group: forbidden: no trusted group host [31m next\n'
    assert len(fake.requests) == 1  # never retried


@pytest.mark.parametrize('exc', [OSError('/private/socket'), TimeoutError('SECRET'), RuntimeError('TOKEN=abc')])
def test_transport_failure_is_static_and_not_retried(exc, fake, capsys):
    fake.responses = [exc]
    rc, out, err = run(ctl_ns('stop'), capsys)
    assert rc == 1 and out == ''
    assert err == 'exo swarm-group: transport: cannot reach the swarm core on the local socket\n'
    assert len(fake.requests) == 1


def test_connect_failure_with_explicit_socket_is_static(monkeypatch, capsys):
    def boom(*a, **kw):
        raise OSError('/secret/socket TOKEN=abc')
    monkeypatch.setattr('exo.protocol.client.Client', boom)
    rc, out, err = run(status_ns(), capsys)
    assert rc == 1 and 'secret' not in err and 'TOKEN' not in err and out == ''
