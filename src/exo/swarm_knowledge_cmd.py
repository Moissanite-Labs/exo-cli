"""exo swarm-knowledge: publish, read, list and history a swarm group knowledge.

  exo swarm-knowledge publish GROUP --key KEY --text TEXT
                       [--source SRC ...] [--expected-revision N]
  exo swarm-knowledge read GROUP --member MEMBER --key KEY
  exo swarm-knowledge list GROUP --member MEMBER [--limit N]
  exo swarm-knowledge history GROUP --member MEMBER --key KEY [--limit N]

Each command talks to the swarm core over its local Unix socket (exod) as an
interactive principal, and hands the request to the core's ``group`` command:

  name="group", args="<op> <json payload>"

The payload carries ``id`` = GROUP and the op-specific fields. The operation
name is only the leading ``args`` token (e.g. ``knowledge_publish``), never a
JSON key in the payload. The owner is never placed in the payload: the core
derives it from the connection's principal (D-039), so it sees the caller's
own view. The core answers with a JSON result whose ``output`` is printed as
compact JSON, preserving exactly what the server returned.

This CLI needs the updated core (swarm PR #1024). It fakes nothing here: an
unreachable socket, a missing core capability, or a refusal is an explicit
error. Nothing touches principals, keys, auth config, or the ledger.

"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from exo.protocol import schema as s


def register(sp, common) -> None:
    p = sp.add_parser(
        'swarm-knowledge',
        help='group knowledge: publish, read, list, history [experimental]')
    sub = p.add_subparsers(dest='swarm_knowledge_cmd', required=True)

    c = sub.add_parser('publish', parents=[common],
                       help='publish a knowledge item to the group')
    c.add_argument('group', help='swarm group id')
    c.add_argument('--key', required=True, help='knowledge item key')
    c.add_argument('--text', required=True,
                   help='content text (single string)')
    c.add_argument('--source', action='append', default=[],
                   help='attribution source (repeatable)')
    c.add_argument('--expected-revision', type=_nonnegative_int, default=0,
                   help='revision the caller believes is current (default: 0)')

    r = sub.add_parser('read', parents=[common],
                       help='read one knowledge item')
    r.add_argument('group', help='swarm group id')
    r.add_argument('--member', required=True, help='member seat whose view to read')
    r.add_argument('--key', required=True, help='knowledge item key')

    l = sub.add_parser('list', parents=[common],
                       help='list the group knowledge items')
    l.add_argument('group', help='swarm group id')
    l.add_argument('--member', required=True, help='member seat whose view to list')
    l.add_argument('--limit', type=_limit, default=100,
                   help='max items (1..1000, default 100)')

    h = sub.add_parser('history', parents=[common],
                       help='history of one knowledge item')
    h.add_argument('group', help='swarm group id')
    h.add_argument('--member', required=True, help='member seat whose view to read')
    h.add_argument('--key', required=True, help='knowledge item key')
    h.add_argument('--limit', type=_limit, default=100,
                   help='max revisions (1..1000, default 100)')


def _nonnegative_int(raw: str) -> int:
    """argparse type: a non-negative integer."""
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f'--expected-revision must be a non-negative integer, got {raw!r}') from exc
    if value < 0:
        raise argparse.ArgumentTypeError(
            f'--expected-revision must be a non-negative integer, got {raw!r}')
    return value


def _limit(raw: str) -> int:
    """argparse type: an integer in the list/history range (1..1000)."""
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f'--limit must be an integer in 1..1000, got {raw!r}') from exc
    if not 1 <= value <= 1000:
        raise argparse.ArgumentTypeError(
            f'--limit must be an integer in 1..1000, got {raw!r}')
    return value


def _err(msg: str) -> int:
    """Print one clean line to stderr and return rc 1 (never a traceback, never a raw exception)."""
    print(f'exo swarm-knowledge: {msg}', file=sys.stderr)
    return 1


def _fail(msg: str) -> None:
    """A validation failure: say it on stderr and return None so the caller stops before transport."""
    _err(msg)
    return None


def _build_payload(a) -> dict[str, Any] | None:
    """Validate the parsed args and build the knowledge op payload.

    Validation happens here, before any transport: a malformed request never
    touches the socket. ``id`` is the group; the owner is never included (the
    core derives it from the connection's principal).
    """
    action = a.swarm_knowledge_cmd
    base: dict[str, Any] = {'id': a.group}

    if action == 'publish':
        if not a.key or not a.key.strip():
            return _fail('--key must not be empty')
        if not isinstance(a.text, str) or not a.text.strip():
            return _fail('--text is required and must be non-empty')
        if getattr(a, 'expected_revision', 0) < 0:
            return _fail('--expected-revision must be a non-negative integer')
        base.update({'key': a.key, 'text': a.text, 'sources': list(a.source or []),
                     'expected_revision': a.expected_revision})
        return base

    if not getattr(a, 'member', None) or not a.member.strip():
        return _fail('--member is required')
    base['member'] = a.member

    if action == 'list':
        if not 1 <= a.limit <= 1000:
            return _fail('--limit must be an integer in 1..1000')
        base['limit'] = a.limit
        return base

    if action in ('read', 'history'):
        if not a.key or not a.key.strip():
            return _fail('--key must not be empty')
        base['key'] = a.key
        if action == 'history':
            if not 1 <= a.limit <= 1000:
                return _fail('--limit must be an integer in 1..1000')
            base['limit'] = a.limit
        return base

    return _fail(f'unknown knowledge action {action!r}')


def _call(action: str, payload: dict[str, Any]) -> s.Response:
    """One request to the local core: CommandRun(name='group', args=op + json payload).

    The client is imported here so this module loads nothing heavy until a
    command actually runs. Local Unix socket only: no token file, no auth reads,
    no TCP. ``principal='chat'`` marks an interactive principal (D-039).
    """
    from exo.protocol.client import Client, ClientError
    from .cli_chat import default_socket

    async def _go() -> s.Response:
        args = f'knowledge_{action} ' + json.dumps(payload, sort_keys=True)
        async with Client(socket=str(default_socket()), principal='chat') as client:
            return await client.send(s.CommandRun(name='group', args=args, session=None))

    try:
        return asyncio.run(_go())
    except (ClientError, OSError):
        return s.Response(id='', ok=False, error=s.ErrorInfo(
            code='transport', message='cannot reach the swarm core on the local socket'))


def cmd_swarm_knowledge(a) -> int:
    payload = _build_payload(a)
    if payload is None:
        return 1
    resp = _call(a.swarm_knowledge_cmd, payload)
    if not resp.ok:
        code = (resp.error.code if resp.error else '') or 'error'
        message = (resp.error.message if resp.error else '') or ''
        return _err(f'{code}: {message}' if message else code)
    out = resp.result.get('output') if isinstance(resp.result, dict) else None
    if out is not None:
        print(json.dumps(out) if not isinstance(out, str) else out)
    return 0
