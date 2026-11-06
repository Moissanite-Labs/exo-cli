"""exo swarm-group: list, observe and send to the caller's swarm groups.

  exo swarm-group list
  exo swarm-group observe GROUP [--after SEQ] [--limit N]
  exo swarm-group send GROUP --text TEXT [--audience PROFILE ...]
  exo swarm-group status GROUP --socket PATH
  exo swarm-group pause|resume|stop GROUP [--member PROFILE] --socket PATH

Each command talks to the swarm core over its local Unix socket (exod) as an
interactive principal and hands the request to the core's ``group`` command:

  name="group", args="<op> <json payload>"

  list:     op ``list``     payload {}
  observe:  op ``observe``  payload {"id": GROUP, "after": SEQ, "limit": N}
  send:     op ``send``     payload {"id": GROUP, "text": TEXT[, "audience": [PROFILE, ...]]}

Bounds are the core's own (``exo.core.group_commands``): ``list`` takes no
fields and has no limit, so this CLI has no limit flag for it. ``observe`` takes
``after`` (0..2**63-1, default 0) and ``limit`` (1..1000, default 100).
GROUP is a non-empty string; the core accepts any such string, and this CLI also
requires at most 128 printable characters (the core's id bound for plan calls).
The core's ``observe`` also accepts a ``member`` filter. It is not exposed here.

``send`` stores one human message. The core only requires TEXT to be non-blank;
the CLI additionally limits it to 16000 characters (a CLI bound, not a core
guarantee). ``--audience`` may repeat; each target is a printable string of 1..128
characters, duplicates are dropped keeping the first occurrence, and at most 64
distinct targets are accepted. When ``--audience`` is absent the key is left out of
the payload and the core addresses every member. Whether each target is a member is
the core's decision (a wrong target and a wrong group give the core's own refusal).
A host in the core's process may wake a running conversation for the stored message;
the CLI makes no claim that anything processes it.

``status``, ``pause``, ``resume`` and ``stop`` are the core's lifecycle ops. They
require an explicit ``--socket``, and no default socket is ever used for them. They
act only through a trusted host injected in that core. A core without one refuses
(``forbidden``) and the CLI reports the refusal. ``--member`` is accepted by
pause/resume/stop only. ``--socket`` is optional for list/observe/send.
Payloads: status {"id"}, and pause/resume/stop {"id"} plus "member" only when given.

The owner is never placed in the payload (the core derives it from the
connection's principal). There is no create, start or retry.
Output is the core's JSON answer, re-serialised as JSON after its shape was
checked. A response of an unexpected shape is an error, never an empty result.
Message text in the answer is untrusted engine output: it is printed as JSON
data and never interpreted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from exo.protocol import schema as s

LIMIT_MIN, LIMIT_MAX, LIMIT_DEFAULT = 1, 1000, 100
AFTER_MAX = 2 ** 63 - 1
GROUP_MAX = 128
_MSG_MAX = 200
TEXT_MAX = 16000
TARGET_MAX = 128
TARGETS_MAX = 64


def register(sp, common) -> None:
    p = sp.add_parser(
        'swarm-group',
        help='swarm groups: list, observe, send, status, pause, resume, stop [experimental]')
    sub = p.add_subparsers(dest='swarm_group_cmd', required=True)

    def _opt_socket(parser):
        parser.add_argument('--socket', type=_socket,
                            help='Unix socket path of the core (default: $EXO_HOME/run/exod.sock)')

    lp = sub.add_parser('list', parents=[common], help='list the swarm groups you own')
    _opt_socket(lp)

    o = sub.add_parser('observe', parents=[common],
                       help='read the message history of an owned group')
    o.add_argument('group', help='swarm group id')
    o.add_argument('--after', type=_after, default=0,
                   help='only messages with a sequence number above this (default 0)')
    o.add_argument('--limit', type=_limit, default=LIMIT_DEFAULT,
                   help='max messages (1..1000, default 100)')
    _opt_socket(o)

    sd = sub.add_parser('send', parents=[common],
                        help='store a human message in an owned group (not an edit of a running prompt)')
    sd.add_argument('group', help='swarm group id')
    sd.add_argument('--text', required=True, type=_text,
                    help=f'message text, not blank, at most {TEXT_MAX} characters')
    sd.add_argument('--audience', action='append', type=_target, metavar='PROFILE',
                    help='member profile to address; repeat for several (default: every member)')
    _opt_socket(sd)

    st = sub.add_parser('status', parents=[common],
                        help="show an owned group's member states and execution receipt")
    st.add_argument('group', help='swarm group id')
    st.add_argument('--socket', required=True, type=_socket, help='Unix socket path of the core (required)')
    for name, what in (('pause', 'pause'), ('resume', 'resume'), ('stop', 'stop')):
        c = sub.add_parser(name, parents=[common], help=f'{what} a running group (or one member)')
        c.add_argument('group', help='swarm group id')
        c.add_argument('--member', type=_member, metavar='PROFILE',
                       help=f'{what} only this member profile (default: the whole group)')
        c.add_argument('--socket', required=True, type=_socket, help='Unix socket path of the core (required)')


def _int_ok(value: Any, lo: int, hi: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _limit_ok(value: Any) -> bool:
    return _int_ok(value, LIMIT_MIN, LIMIT_MAX)


def _after_ok(value: Any) -> bool:
    return _int_ok(value, 0, AFTER_MAX)


def _parse_int(raw: str, message: str) -> int:
    text = raw if isinstance(raw, str) else ''
    if not text.isascii() or not text.isdigit():
        raise argparse.ArgumentTypeError(message)
    return int(text)


def _limit(raw: str) -> int:
    """argparse type: an integer in 1..1000."""
    msg = '--limit must be an integer in 1..1000'
    value = _parse_int(raw, msg)
    if not _limit_ok(value):
        raise argparse.ArgumentTypeError(msg)
    return value


def _after(raw: str) -> int:
    """argparse type: an integer in 0..2**63-1."""
    msg = '--after must be an integer in 0..9223372036854775807'
    value = _parse_int(raw, msg)
    if not _after_ok(value):
        raise argparse.ArgumentTypeError(msg)
    return value


def _text_ok(text: Any) -> bool:
    return isinstance(text, str) and bool(text.strip()) and len(text) <= TEXT_MAX


def _target_ok(target: Any) -> bool:
    return (isinstance(target, str) and bool(target.strip()) and len(target) <= TARGET_MAX
            and target.isprintable())


def _text(raw: str) -> str:
    """argparse type: non-blank text of at most 16000 characters."""
    if not _text_ok(raw):
        raise argparse.ArgumentTypeError(f'--text must be non-blank and at most {TEXT_MAX} characters')
    return raw


def _target(raw: str) -> str:
    """argparse type: one printable audience profile of 1..128 characters."""
    if not _target_ok(raw):
        raise argparse.ArgumentTypeError(
            f'--audience must be a non-blank printable string of at most {TARGET_MAX} characters')
    return raw


def _targets(raw: Any) -> list[str] | None:
    """Distinct targets in first-occurrence order, None when absent; ValueError when invalid."""
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or not raw or not all(_target_ok(x) for x in raw):
        raise ValueError('--audience must be a non-blank printable string of at most '
                         f'{TARGET_MAX} characters')
    out = list(dict.fromkeys(raw))
    if len(out) > TARGETS_MAX:
        raise ValueError(f'--audience accepts at most {TARGETS_MAX} distinct targets')
    return out


SOCKET_MAX = 4096
CONTROL_OPS = ('status', 'pause', 'resume', 'stop')
_DONE_KEY = {'pause': 'paused', 'resume': 'resumed', 'stop': 'stopped'}


def _socket_ok(value: Any) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= SOCKET_MAX
            and value.isprintable())


def _socket(raw: str) -> str:
    """argparse type: a non-blank printable socket path."""
    if not _socket_ok(raw):
        raise argparse.ArgumentTypeError('--socket must be a non-blank printable path')
    return raw


def _member_ok(member: Any) -> bool:
    return _target_ok(member)


def _member(raw: str) -> str:
    """argparse type: one printable member profile of 1..128 characters."""
    if not _member_ok(raw):
        raise argparse.ArgumentTypeError(
            f'--member must be a non-blank printable string of at most {TARGET_MAX} characters')
    return raw


def _group_ok(group: Any) -> bool:
    return (isinstance(group, str) and bool(group.strip()) and len(group) <= GROUP_MAX
            and group.isprintable())


def _clean(text: Any) -> str:
    """Printable, bounded text for stderr: control characters never reach the terminal."""
    text = text if isinstance(text, str) else ''
    text = ''.join(ch if ch.isprintable() else ' ' for ch in text).strip()
    return text if len(text) <= _MSG_MAX else text[:_MSG_MAX] + '...'


def _err(msg: str) -> int:
    """One clean line on stderr and rc 1 (never a traceback, never a raw exception)."""
    print(f'exo swarm-group: {msg}', file=sys.stderr)
    return 1


def _call(op: str, payload: dict[str, Any], socket: str | None = None) -> s.Response:
    """One request to the local core: CommandRun(name='group', args=op + json payload).

    The client is imported here so this module loads nothing heavy until a
    command actually runs. Local Unix socket only: no token file, no auth reads,
    no TCP. ``principal='chat'`` marks an interactive principal (D-039).
    ``socket`` is the explicit ``--socket`` path, and None means the default core socket
    (only list/observe/send may omit it). Any transport failure becomes one static
    message, and the exception text is dropped.
    """
    async def _go() -> s.Response:
        from exo.protocol.client import Client
        from pathlib import Path
        path = Path(socket).expanduser() if socket is not None else None
        if path is None:
            from .cli_chat import default_socket
            path = default_socket()
        args = op if not payload else f'{op} ' + json.dumps(payload, sort_keys=True)
        async with Client(socket=str(path), principal='chat') as client:
            return await client.send(s.CommandRun(name='group', args=args, session=None))

    try:
        return asyncio.run(_go())
    except Exception:  # noqa: BLE001 - static message only, nothing from the exception is shown
        return s.Response(id='', ok=False, error=s.ErrorInfo(
            code='transport', message='cannot reach the swarm core on the local socket'))


class _Refused(Exception):
    """A failure to report as one clean line."""


def _no_dupes(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError('duplicate key')
        out[k] = v
    return out


def _no_const(name):
    raise ValueError('non-finite number')


def _request(op: str, payload: dict[str, Any], socket: str | None = None) -> dict[str, Any]:
    """Send one request and return the core's answer as a dict, or raise _Refused."""
    resp = _call(op, payload, socket)
    if not resp.ok:
        code = _clean(resp.error.code if resp.error else '') or 'error'
        message = _clean(resp.error.message if resp.error else '')
        raise _Refused(f'{code}: {message}' if message else code)
    out = resp.result.get('output') if isinstance(resp.result, dict) else None
    if isinstance(out, str):
        try:
            out = json.loads(out, object_pairs_hook=_no_dupes, parse_constant=_no_const)
        except (ValueError, RecursionError):
            out = None
    if not isinstance(out, dict):
        raise _Refused(f'malformed {op} response from the swarm core')
    return out


def _is_str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _check_list(out: dict[str, Any]) -> None:
    """The list answer: fail closed on any deviation from the core's schema."""
    bad = _Refused('malformed list response from the swarm core')
    groups = out.get('groups')
    if set(out) != {'groups'} or not isinstance(groups, list):
        raise bad
    seen = set()
    for g in groups:
        if not isinstance(g, dict):
            raise bad
        members = g.get('members')
        gid = g.get('id')
        if (not isinstance(gid, str) or not gid or gid in seen
                or not isinstance(g.get('owner'), str) or not isinstance(g.get('mode'), str)
                or not (g.get('conductor') is None or isinstance(g.get('conductor'), str))
                or not isinstance(g.get('created'), str)
                or not isinstance(members, list)
                or not all(isinstance(m, dict) and isinstance(m.get('profile'), str)
                           and isinstance(m.get('session'), str) for m in members)):
            raise bad
        seen.add(gid)


def _check_observe(out: dict[str, Any], after: int, limit: int) -> None:
    """The observe answer: fail closed on any deviation from the core's schema."""
    bad = _Refused('malformed observe response from the swarm core')
    msgs = out.get('messages')
    if set(out) != {'messages', 'next_after'} or not isinstance(msgs, list) or len(msgs) > limit:
        raise bad
    last = after
    for m in msgs:
        if (not isinstance(m, dict) or not _int_ok(m.get('seq'), last + 1, AFTER_MAX)
                or not isinstance(m.get('id'), str) or not isinstance(m.get('sender'), str)
                or not isinstance(m.get('sender_kind'), str) or not _is_str_list(m.get('audience'))
                or not isinstance(m.get('text'), str) or not isinstance(m.get('created'), str)):
            raise bad
        last = m['seq']
    nxt = out.get('next_after')
    if not _int_ok(nxt, 0, AFTER_MAX) or nxt != (msgs[-1]['seq'] if msgs else 0):
        raise bad


def _check_send(out: dict[str, Any], text: str, audience: list[str] | None) -> None:
    """The send answer: one stored human message, matching what was sent."""
    bad = _Refused('malformed send response from the swarm core')
    aud = out.get('audience')
    if (set(out) != {'seq', 'id', 'sender', 'sender_kind', 'audience', 'text', 'created'}
            or not _int_ok(out.get('seq'), 1, AFTER_MAX)
            or not isinstance(out.get('id'), str) or not out['id']
            or not isinstance(out.get('sender'), str) or not out['sender']
            or out.get('sender_kind') != 'human'
            or not _is_str_list(aud) or not aud or len(set(aud)) != len(aud)
            or out.get('text') != text
            or not isinstance(out.get('created'), str)
            or (audience is not None and aud != audience)):
        raise bad


def _nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v)


def _check_status(out: dict[str, Any], group: str) -> None:
    """The status answer (core _status_view): fail closed on any deviation."""
    bad = _Refused('malformed status response from the swarm core')
    members, receipt = out.get('members'), out.get('receipt')
    if (set(out) != {'id', 'owner', 'members', 'local_runtime', 'reconciliation_required', 'receipt'}
            or out.get('id') != group or not _nonempty_str(out.get('owner'))
            or not isinstance(out.get('local_runtime'), bool)
            or not isinstance(out.get('reconciliation_required'), bool)
            or not isinstance(members, list)):
        raise bad
    seen = set()
    for m in members:
        if (not isinstance(m, dict) or set(m) != {'profile', 'state'}
                or not _nonempty_str(m['profile']) or not _nonempty_str(m['state'])
                or m['profile'] in seen):
            raise bad
        seen.add(m['profile'])
    if receipt is None:
        return
    if (not isinstance(receipt, dict)
            or set(receipt) != {'id', 'state', 'created', 'finished', 'members'}
            or not _nonempty_str(receipt['id']) or not _nonempty_str(receipt['state'])
            or not _nonempty_str(receipt['created'])
            or not (receipt['finished'] is None or _nonempty_str(receipt['finished']))
            or not isinstance(receipt['members'], dict)
            or not all(_nonempty_str(k) and _nonempty_str(v) for k, v in receipt['members'].items())):
        raise bad


def _check_control(out: dict[str, Any], op: str, group: str, member: str | None) -> None:
    """The pause/resume/stop answer: {id, <done>: True, member}, echoing what was asked."""
    bad = _Refused(f'malformed {op} response from the swarm core')
    if (set(out) != {'id', _DONE_KEY[op], 'member'} or out.get('id') != group
            or out.get(_DONE_KEY[op]) is not True or out.get('member') != member):
        raise bad


def cmd_swarm_group(a) -> int:
    action = getattr(a, 'swarm_group_cmd', None)
    if action not in ('list', 'observe', 'send', *CONTROL_OPS):
        return _err(f'unknown group action {_clean(repr(action))}')
    sock = getattr(a, 'socket', None)
    if action in CONTROL_OPS and not _socket_ok(sock):
        return _err('--socket is required and must be a non-blank printable path')
    if sock is not None and not _socket_ok(sock):
        return _err('--socket must be a non-blank printable path')
    member = None
    if action in CONTROL_OPS:
        group = getattr(a, 'group', None)
        if not _group_ok(group):
            return _err(f'GROUP must be a non-empty printable string of at most {GROUP_MAX} characters')
        if action != 'status':
            member = getattr(a, 'member', None)
            if member is not None and not _member_ok(member):
                return _err(f'--member must be a non-blank printable string of at most {TARGET_MAX} characters')
    if action == 'observe':
        group = getattr(a, 'group', None)
        after = getattr(a, 'after', 0)
        limit = getattr(a, 'limit', LIMIT_DEFAULT)
        if not _group_ok(group):
            return _err(f'GROUP must be a non-empty printable string of at most {GROUP_MAX} characters')
        if not _after_ok(after):
            return _err('--after must be an integer in 0..9223372036854775807')
        if not _limit_ok(limit):
            return _err('--limit must be an integer in 1..1000')
    if action == 'send':
        group = getattr(a, 'group', None)
        text = getattr(a, 'text', None)
        if not _group_ok(group):
            return _err(f'GROUP must be a non-empty printable string of at most {GROUP_MAX} characters')
        if not _text_ok(text):
            return _err(f'--text must be non-blank and at most {TEXT_MAX} characters')
        try:
            audience = _targets(getattr(a, 'audience', None))
        except ValueError as exc:
            return _err(str(exc))
        payload = {'id': group, 'text': text}
        if audience is not None:
            payload['audience'] = audience
    try:
        if action == 'list':
            out = _request('list', {}, sock)
            _check_list(out)
        elif action == 'send':
            out = _request('send', payload, sock)
            _check_send(out, text, audience)
        elif action == 'status':
            out = _request('status', {'id': group}, sock)
            _check_status(out, group)
        elif action in CONTROL_OPS:
            body = {'id': group} if member is None else {'id': group, 'member': member}
            out = _request(action, body, sock)
            _check_control(out, action, group, member)
        else:
            out = _request('observe', {'id': group, 'after': after, 'limit': limit}, sock)
            _check_observe(out, after, limit)
    except _Refused as exc:
        return _err(str(exc))
    print(json.dumps(out))
    return 0
