"""exo swarm-plan: read-only discovery and status of a swarm group's durable plans.

  exo swarm-plan list GROUP [--limit N]
  exo swarm-plan status GROUP PLAN [--limit N]

Each command talks to the swarm core over its local Unix socket (exod) as an
interactive principal and hands the request to the core's ``group`` command:

  name="group", args="<op> <json payload>"

  list:   op ``plan_list``    payload {"id": GROUP, "limit": N}
  status: op ``plan_status``  payload {"id": GROUP, "plan_id": FULL_ID, "limit": N}

``--limit`` is 1..200 (default 50) for both. PLAN is either the full 64 character
lowercase hex plan id (one request) or a lowercase hex prefix of 8..63 characters.
A prefix is resolved with one ``plan_list`` request at the maximum limit (200)
and only when the answer is complete (``plans_truncated`` is false) and exactly
one listed plan id starts with the prefix. A truncated list can never prove that
a prefix is unique, so every truncated list is refused, whatever it contains,
and the caller must pass the full id. No match is "no such plan", two or more
matches are "ambiguous".

The owner is never placed in the payload (the core derives it from the
connection's principal) and nothing here can mutate: no grants, no owner, no
pause/resume/stop. Output is the core's JSON answer, re-serialised as JSON after
its shape was checked. A response of an unexpected shape is an error, never an
empty result. Text inside the answer (task results and errors) is untrusted
engine output: it is printed as JSON data and never interpreted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from typing import Any

from exo.protocol import schema as s

LIMIT_MIN, LIMIT_MAX, LIMIT_DEFAULT = 1, 200, 50
GROUP_MAX = 128  # the core's group id bound
_FULL_ID = re.compile(r'[0-9a-f]{64}')
_PREFIX = re.compile(r'[0-9a-f]{8,63}')
_MSG_MAX = 200


def register(sp, common) -> None:
    p = sp.add_parser(
        'swarm-plan',
        help='group plans: list, status (read-only) [experimental]')
    sub = p.add_subparsers(dest='swarm_plan_cmd', required=True)

    l = sub.add_parser('list', parents=[common],
                       help='list the durable plans of an owned group')
    l.add_argument('group', help='swarm group id')
    l.add_argument('--limit', type=_limit, default=LIMIT_DEFAULT,
                   help='max plans (1..200, default 50)')

    st = sub.add_parser('status', parents=[common],
                        help='durable task state of one plan')
    st.add_argument('group', help='swarm group id')
    st.add_argument('plan', type=_plan_ref,
                    help='full 64 hex plan id, or a lowercase hex prefix of 8..63 characters')
    st.add_argument('--limit', type=_limit, default=LIMIT_DEFAULT,
                    help='max task rows (1..200, default 50)')


def _limit_ok(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and LIMIT_MIN <= value <= LIMIT_MAX


def _limit(raw: str) -> int:
    """argparse type: an integer in 1..200."""
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError('--limit must be an integer in 1..200') from exc
    if not _limit_ok(value):
        raise argparse.ArgumentTypeError('--limit must be an integer in 1..200')
    return value


def _plan_ok(value: Any) -> bool:
    return isinstance(value, str) and bool(_FULL_ID.fullmatch(value) or _PREFIX.fullmatch(value))


def _plan_ref(raw: str) -> str:
    """argparse type: a full lowercase hex plan id or a lowercase hex prefix (8..63)."""
    if not _plan_ok(raw):
        raise argparse.ArgumentTypeError(
            'PLAN must be 64 lowercase hex characters, or a lowercase hex prefix of 8..63 characters')
    return raw


def _clean(text: Any) -> str:
    """Printable, bounded text for stderr: control characters never reach the terminal."""
    text = text if isinstance(text, str) else ''
    text = ''.join(ch if ch.isprintable() else ' ' for ch in text).strip()
    return text if len(text) <= _MSG_MAX else text[:_MSG_MAX] + '...'


def _err(msg: str) -> int:
    """One clean line on stderr and rc 1 (never a traceback, never a raw exception)."""
    print(f'exo swarm-plan: {msg}', file=sys.stderr)
    return 1


def _call(payload: dict[str, Any], op: str) -> s.Response:
    """One request to the local core: CommandRun(name='group', args=op + json payload).

    The client is imported here so this module loads nothing heavy until a
    command actually runs. Local Unix socket only: no token file, no auth reads,
    no TCP. ``principal='chat'`` marks an interactive principal (D-039).
    """
    from exo.protocol.client import Client, ClientError
    from .cli_chat import default_socket

    async def _go() -> s.Response:
        args = f'{op} ' + json.dumps(payload, sort_keys=True)
        async with Client(socket=str(default_socket()), principal='chat') as client:
            return await client.send(s.CommandRun(name='group', args=args, session=None))

    try:
        return asyncio.run(_go())
    except (ClientError, OSError):
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
    resp = _call(payload, op)
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


def _is_count(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _check_list(out: dict[str, Any], group: str, limit: int) -> list[dict[str, Any]]:
    """The plan_list answer: fail closed on any deviation from the core's schema."""
    bad = _Refused('malformed plan_list response from the swarm core')
    if out.get('id') != group or not isinstance(out.get('plans_truncated'), bool):
        raise bad
    plans = out.get('plans')
    if not isinstance(plans, list) or len(plans) > limit:
        raise bad
    seen = set()
    for p in plans:
        if not isinstance(p, dict):
            raise bad
        pid = p.get('plan_id')
        if not isinstance(pid, str) or not _FULL_ID.fullmatch(pid) or pid in seen:
            raise bad
        seen.add(pid)
        by_state = p.get('tasks_by_state')
        if (not isinstance(p.get('state'), str) or not isinstance(p.get('created'), str)
                or not (p.get('finished') is None or isinstance(p.get('finished'), str))
                or not _is_count(p.get('task_count'))
                or not isinstance(by_state, dict)
                or not all(isinstance(k, str) and _is_count(v) for k, v in by_state.items())):
            raise bad
    return plans


def _check_status(out: dict[str, Any], group: str, plan_id: str, limit: int) -> None:
    """The plan_status answer: fail closed on any deviation from the core's schema."""
    bad = _Refused('malformed plan_status response from the swarm core')
    if out.get('id') != group or out.get('plan_id') != plan_id:
        raise bad
    tasks = out.get('tasks')
    if (not isinstance(out.get('state'), str) or not isinstance(out.get('durable'), dict)
            or not isinstance(tasks, list) or len(tasks) > limit
            or not all(isinstance(t, dict) for t in tasks)
            or not isinstance(out.get('tasks_truncated'), bool)
            or not _is_count(out.get('task_count')) or not _is_count(out.get('tasks_returned'))
            or out['tasks_returned'] != len(tasks) or out['task_count'] < len(tasks)):
        raise bad


def _group_ok(group: Any) -> bool:
    return isinstance(group, str) and bool(group.strip()) and len(group) <= GROUP_MAX


def _status(group: str, plan_id: str, limit: int) -> dict[str, Any]:
    payload = {'id': group, 'plan_id': plan_id, 'limit': limit}
    out = _request('plan_status', payload)
    _check_status(out, group, plan_id, limit)
    return out


def _resolve(group: str, prefix: str) -> str:
    """One full plan id from a prefix, or raise. Only a complete list can prove uniqueness."""
    out = _request('plan_list', {'id': group, 'limit': LIMIT_MAX})
    plans = _check_list(out, group, LIMIT_MAX)
    if out['plans_truncated']:
        raise _Refused('plan list is truncated, so a prefix cannot be resolved; pass the full 64 hex plan id')
    matches = [p['plan_id'] for p in plans if p['plan_id'].startswith(prefix)]
    if not matches:
        raise _Refused('no such plan')
    if len(matches) > 1:
        raise _Refused('ambiguous plan prefix; pass more characters or the full 64 hex plan id')
    return matches[0]


def cmd_swarm_plan(a) -> int:
    action = getattr(a, 'swarm_plan_cmd', None)
    group = getattr(a, 'group', None)
    limit = getattr(a, 'limit', LIMIT_DEFAULT)
    if action not in ('list', 'status'):
        return _err(f'unknown plan action {action!r}')
    if not _group_ok(group):
        return _err(f'GROUP must be a non-empty string of at most {GROUP_MAX} characters')
    if not _limit_ok(limit):
        return _err('--limit must be an integer in 1..200')
    plan = getattr(a, 'plan', None)
    if action == 'status' and not _plan_ok(plan):
        return _err('PLAN must be 64 lowercase hex characters, '
                    'or a lowercase hex prefix of 8..63 characters')
    try:
        if action == 'list':
            out = _request('plan_list', {'id': group, 'limit': limit})
            _check_list(out, group, limit)
        else:
            plan_id = plan if _FULL_ID.fullmatch(plan) else _resolve(group, plan)
            out = _status(group, plan_id, limit)
    except _Refused as exc:
        return _err(str(exc))
    print(json.dumps(out))
    return 0
