"""exo swarm-group: read-only listing and observation of the caller's swarm groups.

  exo swarm-group list
  exo swarm-group observe GROUP [--after SEQ] [--limit N]

Each command talks to the swarm core over its local Unix socket (exod) as an
interactive principal and hands the request to the core's ``group`` command:

  name="group", args="<op> <json payload>"

  list:     op ``list``     payload {}
  observe:  op ``observe``  payload {"id": GROUP, "after": SEQ, "limit": N}

Bounds are the core's own (``exo.core.group_commands``): ``list`` takes no
fields and has no limit, so this CLI has no limit flag for it. ``observe`` takes
``after`` (0..2**63-1, default 0) and ``limit`` (1..1000, default 100).
GROUP is a non-empty string; the core accepts any such string, and this CLI also
requires at most 128 printable characters (the core's id bound for plan calls).
The core's ``observe`` also accepts a ``member`` filter. It is not exposed here.

The owner is never placed in the payload (the core derives it from the
connection's principal) and nothing here can mutate: no send, no create, no
start, no pause/resume/stop. Output is the core's JSON answer, re-serialised as
JSON after its shape was checked. A response of an unexpected shape is an error,
never an empty result. Message text in the answer is untrusted engine output: it
is printed as JSON data and never interpreted.
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


def register(sp, common) -> None:
    p = sp.add_parser(
        'swarm-group',
        help='swarm groups: list, observe (read-only) [experimental]')
    sub = p.add_subparsers(dest='swarm_group_cmd', required=True)

    sub.add_parser('list', parents=[common], help='list the swarm groups you own')

    o = sub.add_parser('observe', parents=[common],
                       help='read the message history of an owned group')
    o.add_argument('group', help='swarm group id')
    o.add_argument('--after', type=_after, default=0,
                   help='only messages with a sequence number above this (default 0)')
    o.add_argument('--limit', type=_limit, default=LIMIT_DEFAULT,
                   help='max messages (1..1000, default 100)')


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


def _call(op: str, payload: dict[str, Any]) -> s.Response:
    """One request to the local core: CommandRun(name='group', args=op + json payload).

    The client is imported here so this module loads nothing heavy until a
    command actually runs. Local Unix socket only: no token file, no auth reads,
    no TCP. ``principal='chat'`` marks an interactive principal (D-039).
    Any transport failure becomes one static message; the exception text is dropped.
    """
    async def _go() -> s.Response:
        from exo.protocol.client import Client
        from .cli_chat import default_socket
        args = op if not payload else f'{op} ' + json.dumps(payload, sort_keys=True)
        async with Client(socket=str(default_socket()), principal='chat') as client:
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


def _request(op: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one request and return the core's answer as a dict, or raise _Refused."""
    resp = _call(op, payload)
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


def cmd_swarm_group(a) -> int:
    action = getattr(a, 'swarm_group_cmd', None)
    if action not in ('list', 'observe'):
        return _err(f'unknown group action {_clean(repr(action))}')
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
    try:
        if action == 'list':
            out = _request('list', {})
            _check_list(out)
        else:
            out = _request('observe', {'id': group, 'after': after, 'limit': limit})
            _check_observe(out, after, limit)
    except _Refused as exc:
        return _err(str(exc))
    print(json.dumps(out))
    return 0
