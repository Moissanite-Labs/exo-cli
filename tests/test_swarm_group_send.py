"""Tests for `exo swarm-group send` (src/exo/swarm_group_cmd.py).

Part 1, offline: the core client is replaced by the recording fake from test_swarm_group_cmd, so no socket opens.
Part 2, integration (needs a swarm core on PYTHONPATH, skips honestly otherwise): a real ``exo swarm --start``
with the offline fake provider, served on a REAL Unix socket and called through the REAL protocol Client and the
core's REAL ``send`` handler.

Wire contract (swarm core ``group`` command, group_commands._op_send):
- send: CommandRun(name='group', args='send ' + JSON{id, text[, audience]});
- ``audience`` is left out when the user gave none (the core then addresses every member);
- the owner is never sent; the core answers with one stored human message.
"""
import json
import os
import signal
import sys
import threading
import time
import types

import pytest

import test_swarm_group_cmd as base  # also sets the exo import path
from test_swarm_group_cmd import (
    BOUND, CLI_SRC, FOREIGN_TEXT, G, OWN_TEXTS, OWNER, PROFILES, msg, ns, ok, refuse, req, run, run_cli)
from exo import swarm_group_cmd as m  # noqa: E402

# Reuse the fixtures of test_swarm_group_cmd under their own names. A plain module-level alias, not an import,
# so a test argument of the same name is not a redefinition.
fake = base.fake
climod = base.climod
home = base.home
cli = base.cli
rig = base.rig

SECRET = 'sk-test-SECRET-0123456789'


def send_ns(text='hello', audience=None, group=G):
    return ns('send', group=group, text=text, audience=audience)


def stored(text='hello', audience=('builder',), n=1, **kw):
    return msg(n, text=text, audience=list(audience), **kw)


# ---- payloads ---------------------------------------------------------------

def test_send_without_audience_omits_the_key(fake, capsys):
    fake.responses = [ok(stored(audience=('builder', 'checker')))]
    rc, out, err = run(send_ns(), capsys)
    assert rc == 0 and err == ''
    assert req(fake.requests[0]) == ('send', {'id': G, 'text': 'hello'})
    assert json.loads(out)['audience'] == ['builder', 'checker']


def test_send_payload_never_carries_an_owner(fake, capsys):
    fake.responses = [ok(stored()), ok(stored(audience=('checker',)))]
    run(send_ns(), capsys)
    run(send_ns(audience=['checker']), capsys)
    for r in fake.requests:
        assert set(req(r)[1]) <= {'id', 'text', 'audience'}
        assert 'owner' not in r.args


def test_send_audience_is_deduplicated_in_first_seen_order(fake, capsys):
    fake.responses = [ok(stored(audience=('checker', 'builder')))]
    rc, _, _ = run(send_ns(audience=['checker', 'builder', 'checker', 'builder']), capsys)
    assert rc == 0
    assert req(fake.requests[0]) == ('send', {'id': G, 'text': 'hello', 'audience': ['checker', 'builder']})


def test_send_text_is_passed_through_unchanged(fake, capsys):
    text = '  line one\n\tline "two" \u00fc  '
    fake.responses = [ok(stored(text=text))]
    rc, _, _ = run(send_ns(text=text), capsys)
    assert rc == 0 and req(fake.requests[0])[1]['text'] == text


def test_send_boundary_sizes_are_accepted(fake, capsys):
    fake.responses = [ok(stored(text='x' * 16000))]
    assert run(send_ns(text='x' * 16000), capsys)[0] == 0
    targets = [f't{i}' for i in range(64)]
    fake.responses = [ok(stored(audience=targets))]
    assert run(send_ns(audience=targets), capsys)[0] == 0
    one = 'a' * 128
    fake.responses = [ok(stored(audience=(one,)))]
    assert run(send_ns(audience=[one]), capsys)[0] == 0


def test_send_parser_dispatches_with_repeated_audience(fake, capsys, climod):
    fake.responses = [ok(stored(audience=('builder', 'checker')))]
    rc = climod.main(['swarm-group', 'send', G, '--text', 'hello',
                      '--audience', 'builder', '--audience', 'checker', '--audience', 'builder'])
    assert rc == 0
    assert req(fake.requests[0]) == ('send', {'id': G, 'text': 'hello', 'audience': ['builder', 'checker']})
    assert json.loads(capsys.readouterr().out)['text'] == 'hello'


# ---- invalid input never reaches the transport --------------------------------

def no_transport(fake):
    assert fake.requests == [] and fake.entered == 0


@pytest.mark.parametrize('text', ['', '   ', '\n\t ', 'x' * 16001, None, 5])
def test_send_rejects_bad_text_before_transport(text, fake, capsys):
    rc, out, err = run(send_ns(text=text), capsys)
    assert rc == 1 and out == '' and err.startswith('exo swarm-group: --text')
    no_transport(fake)


@pytest.mark.parametrize('audience', [
    [''], ['  '], ['a\nb'], ['a\x1bb'], ['x' * 129], [5], [None], [], 'builder',
    ['ok', ''], [f't{i}' for i in range(65)]])
def test_send_rejects_bad_audience_before_transport(audience, fake, capsys):
    rc, out, err = run(send_ns(audience=audience), capsys)
    assert rc == 1 and out == '' and err.startswith('exo swarm-group: --audience')
    no_transport(fake)


@pytest.mark.parametrize('group', ['', '  ', 'a\nb', 'x' * 129, None, 3])
def test_send_rejects_bad_group_before_transport(group, fake, capsys):
    rc, out, err = run(send_ns(group=group), capsys)
    assert rc == 1 and out == '' and err.startswith('exo swarm-group: GROUP')
    no_transport(fake)


def test_send_duplicates_do_not_count_toward_the_target_limit(fake, capsys):
    targets = [f't{i}' for i in range(64)]
    fake.responses = [ok(stored(audience=targets))]
    assert run(send_ns(audience=targets + targets), capsys)[0] == 0


@pytest.mark.parametrize('argv, bad', [
    (['send', G], '--text'), (['send', '--text', 'x'], 'group'), (['send', G, '--text', ' '], '--text'),
    (['send', G, '--text', 'x' * 16001], '--text'), (['send', G, '--text', 'x', '--audience', ''], '--audience'),
    (['send', G, '--text', 'x', '--audience', 'a\tb'], '--audience'),
    (['send', G, '--text', 'x', '--audience', 'y' * 129], '--audience'),
    (['send', G, '--text', 'x', '--owner', 'me'], '--owner'),
    (['send', G, '--text', 'x', '--retry'], '--retry'),
])
def test_send_parser_rejects_before_dispatch(argv, bad, capsys, monkeypatch, climod, fake):
    called = []
    monkeypatch.setattr(m, 'cmd_swarm_group', lambda a: called.append(a) or 0)
    with pytest.raises(SystemExit) as e:
        climod.main(['swarm-group', *argv])
    assert e.value.code == 2 and called == []
    assert bad in capsys.readouterr().err
    no_transport(fake)


def test_send_parser_rejects_65_distinct_targets_before_transport(capsys, climod, fake):
    argv = ['swarm-group', 'send', G, '--text', 'x']
    for i in range(65):
        argv += ['--audience', f't{i}']
    assert climod.main(argv) == 1
    assert 'at most 64' in capsys.readouterr().err
    no_transport(fake)


def test_send_error_lines_do_not_echo_the_input(fake, capsys):
    rc, out, err = run(send_ns(text=SECRET + ' ' * 16000), capsys)
    assert rc == 1 and SECRET not in err
    rc, out, err = run(send_ns(audience=[SECRET + '\n']), capsys)
    assert rc == 1 and SECRET not in err
    rc, out, err = run(send_ns(group=SECRET + '\n'), capsys)
    assert rc == 1 and SECRET not in err
    no_transport(fake)


# ---- strict reply validation ----------------------------------------------------

def mutated(**kw):
    row = stored(audience=('builder', 'checker'))
    for k, v in kw.items():
        if v is KeyError:
            del row[k]
        else:
            row[k] = v
    return row


BAD_REPLIES = [
    mutated(seq=True), mutated(seq=0), mutated(seq=-1), mutated(seq=1.0), mutated(seq='1'), mutated(seq=2 ** 63),
    mutated(seq=KeyError), mutated(id=''), mutated(id=7), mutated(sender=''), mutated(sender=None),
    mutated(sender_kind='member'), mutated(sender_kind=KeyError), mutated(audience=[]), mutated(audience='builder'),
    mutated(audience=['builder', 'builder']), mutated(audience=['builder', 3]), mutated(audience=['builder']),
    mutated(audience=['checker', 'builder']), mutated(audience=KeyError), mutated(text='other'),
    mutated(text=None), mutated(created=5), mutated(created=KeyError), {**mutated(), 'extra': 1}, {},
    {'messages': [], 'next_after': 0},
]


@pytest.mark.parametrize('reply', BAD_REPLIES)
def test_send_rejects_malformed_reply(reply, fake, capsys):
    fake.responses = [ok(reply)]
    rc, out, err = run(send_ns(audience=['builder', 'checker']), capsys)
    assert rc == 1 and out == ''
    assert err == 'exo swarm-group: malformed send response from the swarm core\n'


@pytest.mark.parametrize('raw', [
    'not json', '[]', '"x"', 'null', '5', '',
    '{"seq": 1, "seq": 2}', '{"seq": NaN}', '{"seq": Infinity}'])
def test_send_rejects_unparseable_reply(raw, fake, capsys):
    fake.responses = [ok(raw)]
    rc, out, err = run(send_ns(), capsys)
    assert rc == 1 and out == '' and 'malformed send response' in err


def test_send_rejects_reply_without_output(fake, capsys):
    from exo.protocol import schema as s
    for result in (None, {}, {'output': None}, {'output': 3}):
        fake.responses = [s.Response(id='r', ok=True, result=result)]
        rc, out, err = run(send_ns(), capsys)
        assert rc == 1 and out == '' and 'malformed send response' in err


def test_send_reply_audience_may_be_anything_valid_when_none_was_given(fake, capsys):
    fake.responses = [ok(stored(audience=('checker', 'builder', 'third')))]
    rc, out, _ = run(send_ns(), capsys)
    assert rc == 0 and json.loads(out)['audience'] == ['checker', 'builder', 'third']


def test_send_accepts_a_json_string_reply(fake, capsys):
    fake.responses = [ok(json.dumps(stored()))]
    rc, out, err = run(send_ns(), capsys)
    assert rc == 0 and err == '' and json.loads(out)['seq'] == 1


# ---- output escaping and secret-safe errors -------------------------------------

def test_send_output_is_escaped_json(fake, capsys):
    text = 'red \x1b[31m alarm\nsecond line \u202e done'
    fake.responses = [ok(stored(text=text))]
    rc, out, err = run(send_ns(text=text), capsys)
    assert rc == 0 and err == ''
    assert '\x1b' not in out and '\u202e' not in out and out.count('\n') == 1
    assert json.loads(out)['text'] == text


def test_send_transport_failure_is_static_and_hides_the_exception(fake, capsys):
    fake.responses = [OSError(f'connect failed /secret/path {SECRET}')]
    rc, out, err = run(send_ns(text=f'hello {SECRET}'), capsys)
    assert rc == 1 and out == ''
    assert err == 'exo swarm-group: transport: cannot reach the swarm core on the local socket\n'
    assert SECRET not in err and '/secret' not in err


def test_send_refusal_is_one_clean_line(fake, capsys):
    fake.responses = [refuse('bad_request', 'no such group\x1b[31m\nnext')]
    rc, out, err = run(send_ns(), capsys)
    assert rc == 1 and out == ''
    assert err == 'exo swarm-group: bad_request: no such group [31m next\n'
    assert '\x1b' not in err and err.count('\n') == 1


def test_send_does_not_retry_after_a_failure(fake, capsys):
    fake.responses = [refuse('bad_request', 'invalid audience')]
    assert run(send_ns(), capsys)[0] == 1
    assert len(fake.requests) == 1


# ---- integration: real socket, real core, offline fake provider -------------------

def test_cli_swarm_group_send_socket_roundtrip(home, rig, cli):
    pytest.importorskip('exo.core.group_commands')
    sock = home / 'run' / 'exod.sock'
    sock.parent.mkdir(parents=True)
    ev = types.SimpleNamespace(done=False, error=None, results={}, lock=threading.Lock())

    def client_side():
        deadline = time.monotonic() + BOUND
        try:
            while rig.launch is None and time.monotonic() < deadline:
                time.sleep(0.02)
            while not sock.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            gid = rig.launch.group.id
            r = ev.results
            r['gid'] = gid
            r['broadcast'] = run_cli(cli, 'swarm-group', 'send', gid, '--text', 'to everyone')
            r['target'] = run_cli(cli, 'swarm-group', 'send', gid, '--text', 'to checker only',
                                  '--audience', 'checker', '--audience', 'checker')
            r['big'] = run_cli(cli, 'swarm-group', 'send', gid, '--text', '\U0001F600' * 16000)
            r['unknown_target'] = run_cli(cli, 'swarm-group', 'send', gid, '--text', 'x', '--audience', 'nobody')
            r['foreign'] = run_cli(cli, 'swarm-group', 'send', rig.foreign_gid, '--text', 'sneak in')
            r['ghost'] = run_cli(cli, 'swarm-group', 'send', 'no-such-group', '--text', 'x')
            r['blank'] = run_cli(cli, 'swarm-group', 'send', gid, '--text', '   ')
            r['observe'] = run_cli(cli, 'swarm-group', 'observe', gid, '--limit', '1000')
        except BaseException as exc:  # noqa: BLE001 - reported on the main thread
            ev.error = exc
        finally:
            with ev.lock:
                if not ev.done:
                    os.kill(os.getpid(), signal.SIGINT)

    worker = threading.Thread(target=client_side, name='swarm-group-send-client', daemon=True)
    argv = ['swarm', '--profiles', ','.join(PROFILES), '--seat', OWNER, '--mode', 'socratic', '--start',
            '--socket', str(sock)]
    worker.start()
    try:
        rc = cli.main(argv)
    finally:
        with ev.lock:
            ev.done = True
        worker.join(BOUND + 5)
    assert not worker.is_alive()
    if ev.error is not None:
        raise ev.error

    r = ev.results
    assert rc == 0
    assert sys.modules['exo.swarm_group_cmd'].__file__ == str(CLI_SRC / 'swarm_group_cmd.py')
    assert rig.closed and not sock.exists()

    code, out, err = r['broadcast']
    assert code == 0 and err == ''
    body = json.loads(out)
    assert body['text'] == 'to everyone' and body['audience'] == PROFILES
    assert body['sender'] == OWNER and body['sender_kind'] == 'human'

    code, out, err = r['target']
    assert code == 0 and err == ''
    target = json.loads(out)
    assert target['text'] == 'to checker only' and target['audience'] == ['checker']
    assert target['seq'] > body['seq']

    code, out, err = r['big']  # the largest accepted text, widest escaping on the wire
    assert code == 0 and err == '' and json.loads(out)['text'] == '\U0001F600' * 16000

    for key in ('unknown_target', 'blank'):
        code, out, err = r[key]
        assert out == '' and code in (1, 2)
    assert r['unknown_target'][0] == 1
    assert r['unknown_target'][2] == 'exo swarm-group: bad_request: invalid audience\n'
    assert r['blank'][0] == 2

    for key in ('foreign', 'ghost'):
        code, out, err = r[key]
        assert code == 1 and out == ''
        assert err == 'exo swarm-group: bad_request: no such group\n'
        assert rig.foreign_gid not in err and 'sneak in' not in err

    # the stored messages are really in the group, and nothing reached the other owner's group
    code, out, err = r['observe']
    assert code == 0 and err == ''
    seen = {x['seq']: x for x in json.loads(out)['messages']}
    assert seen[body['seq']] == body and seen[target['seq']] == target
    assert [x['text'] for x in seen.values()][:len(OWN_TEXTS)] == OWN_TEXTS
    assert 'sneak in' not in out and FOREIGN_TEXT not in out
    texts = [x['text'] for x in seen.values() if x['sender_kind'] == 'human']
    assert texts.count('x') == 0 and texts.count('sneak in') == 0
