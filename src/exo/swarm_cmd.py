"""exo swarm: launch one swarm group and serve it as an exod.

  exo swarm --profiles seat1,seat2 [--seat owner] [--socket PATH | --stdio] [--start]
             [--mode conductor|socratic] [--conductor PROFILE] [--conversation]
             [--max-turns N] [--idle-timeout SECONDS]
  exo swarm --profiles seat1,seat2 --start --plan PLAN.json --grants GRANTS.json [...]

Launches one group through the core's group_launch.create, binds the member
engines, and serves the owner engine on a Unix socket or stdio so the user
can drive it with exo chat and /group commands. The whole lifecycle
(launch.start, serve, launch.aclose) runs in ONE asyncio.run coroutine so
the group's runtime tasks stay on the same event loop.

Modes follow the core runtime: ``conductor`` (default; the first profile is
the conductor unless --conductor names another member) and ``socratic`` (no
conductor; an explicit one is rejected by the core). The mode and conductor
are forwarded to group_launch.create, which validates them before anything is
allocated.

With ``--start`` alone each member immediately runs one model turn (the
one-shot start). Adding ``--conversation`` (which requires ``--start``) starts
the bounded reactive conversation instead: members stay alive and answer as
messages arrive, under explicit ``--max-turns``/``--idle-timeout`` bounds.
Defaults for omitted bounds come from the core (the ConversationOptions
dataclass), not this CLI; the bound flags are meaningless without
``--conversation`` and are rejected. Without ``--start`` no run occurs until
an explicit principal /group start command from the connected client.

This CLI needs the updated core (swarm PR #1019): group_launch.create accepts
mode/conductor and GroupHost.start accepts ConversationOptions. It does not
fake either behavior; the missing-feature error is explicit. Group membership
is the explicit --profiles list, never inferred from model output.

A plan start (``--plan FILE --grants FILE``, both required, plus ``--start``)
runs a task plan instead of the one-shot or conversation start; it cannot be
combined with ``--conversation``, ``--max-turns`` or ``--idle-timeout``. Both
files are strict, bounded JSON with unknown fields rejected; the grants file
is the only source of authority and nothing is defaulted from the plan, the
profiles or any config. The owner is never read from the file: it is the
launch principal of the created group. File shape is checked before the group
is created; the plan is bound to the group by the core validate_plan after
create and before any member engine or model call, and the core host gate
still checks every plan start. See docs/SWARM-PLAN-CLI.md.

Principal group controls (/group start and friends) require a client on the
Unix socket; the stdio protocol alone cannot grant principal status. This
CLI does not relax authenticated principal handling.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import signal
import stat
import sys
from pathlib import Path

PLAN_MAX_BYTES = 256 * 1024
GRANTS_MAX_BYTES = 64 * 1024
GRANTS_SCHEMA = 'exo.swarm.cli-grants.v1'
GRANTS_KEYS = frozenset({'schema', 'profiles', 'permits', 'write_roots', 'reviewer_profiles', 'routes'})
_JSON_MAX_DEPTH = 16


def register(sp, common) -> None:
    p = sp.add_parser(
        'swarm',
        help='launch a swarm group and serve it (exo chat connects to it) [experimental]')
    p.add_argument('--profiles', required=True,
                   help='comma-separated member seat/profile IDs (>= 2, distinct; never the owner seat)')
    p.add_argument('--seat', help='owner seat name (default: $EXO_SEAT, then "default")')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--socket', help='Unix socket path to serve on (default: stdio)')
    g.add_argument('--stdio', action='store_true',
                   help='speak the JSON-lines protocol on stdin/stdout; no human banner on stdout')
    p.add_argument('--mode', choices=('conductor', 'socratic'), default='conductor',
                   help='group mode: conductor (default; first profile conducts) or socratic (no conductor)')
    p.add_argument('--conductor',
                   help='conducting member profile (conductor mode; default: the first profile)')
    p.add_argument('--conversation', action='store_true',
                   help='opt in to the bounded reactive conversation (requires --start)')
    p.add_argument('--max-turns', type=_positive_int, default=None,
                   help='bounded conversation: max engine turns per member (requires --conversation; core default if omitted)')
    p.add_argument('--idle-timeout', type=_positive_finite_float, default=None,
                   help='bounded conversation: idle wait in seconds (requires --conversation; core default if omitted)')
    p.add_argument('--plan', metavar='FILE', default=None,
                   help='JSON task plan to run at start (requires --grants and --start; not with --conversation)')
    p.add_argument('--grants', metavar='FILE', default=None,
                   help='JSON host grants for --plan; explicit, never defaulted (requires --plan and --start)')
    p.add_argument('--start', action='store_true',
                   help='run one model turn per member immediately; without it no run occurs until a principal /group start. '
                        'With --conversation, start the bounded reactive conversation instead.')


def _positive_int(raw: str) -> int:
    """argparse type: an integer in the core conversation range (1..10000)."""
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f'--max-turns must be a positive integer up to 10000, got {raw!r}') from exc
    if value < 1 or value > 10000:
        raise argparse.ArgumentTypeError(
            f'--max-turns must be a positive integer up to 10000, got {raw!r}')
    return value


def _positive_finite_float(raw: str) -> float:
    """argparse type: a positive finite number of seconds, up to the core limit."""
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f'--idle-timeout must be a positive finite number up to 86400, got {raw!r}') from exc
    if not math.isfinite(value) or value <= 0.0 or value > 86400:
        raise argparse.ArgumentTypeError(
            f'--idle-timeout must be a positive finite number up to 86400, got {raw!r}')
    return value


def _err(msg: str) -> int:
    print(f'exo swarm: {msg}', file=sys.stderr)
    return 1


def _parse_profiles(raw: str) -> list[str]:
    """Comma-separated profile IDs; fail closed on empties before any core call."""
    names = [w.strip() for w in raw.split(',')]
    if any(not n for n in names):
        raise ValueError('--profiles has an empty entry (trailing or doubled comma)')
    return names


class _InputError(ValueError):
    """A sanitized plan/grants file problem: the message never quotes file contents."""


class _DuplicateKey(ValueError):
    pass


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey()
        out[key] = value
    return out


def _no_constant(_name):
    raise _InputError('NaN and Infinity are not JSON')


def _depth(value, level=0) -> int:
    if level > _JSON_MAX_DEPTH:
        return level
    if isinstance(value, dict):
        return max((_depth(v, level + 1) for v in value.values()), default=level)
    if isinstance(value, list):
        return max((_depth(v, level + 1) for v in value), default=level)
    return level


def _read_json_file(path: str, flag: str, limit: int):
    """One bounded, strict JSON document from a regular file; errors never echo the contents."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    except OSError as exc:
        raise _InputError(f'{flag}: cannot open file ({exc.strerror or type(exc).__name__})') from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _InputError(f'{flag}: not a regular file')
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, 'rb') as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise _InputError(f'{flag}: file exceeds {limit} bytes')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise _InputError(f'{flag}: file is not UTF-8') from None
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except _DuplicateKey:
        raise _InputError(f'{flag}: duplicate key in a JSON object') from None
    except _InputError as exc:
        raise _InputError(f'{flag}: {exc}') from None
    except json.JSONDecodeError as exc:
        raise _InputError(f'{flag}: not valid JSON (line {exc.lineno}, column {exc.colno})') from None
    except RecursionError:
        raise _InputError(f'{flag}: JSON nests too deeply') from None
    if _depth(value) > _JSON_MAX_DEPTH:
        raise _InputError(f'{flag}: JSON nests too deeply')
    return value


def _check_plan_shape(raw, schema: str, plan_keys) -> dict:
    """Shape only, before create: the core validate_plan does the real binding after create."""
    if not isinstance(raw, dict):
        raise _InputError('--plan: top level must be a JSON object')
    unknown = set(raw) - set(plan_keys)
    if unknown:
        raise _InputError(f'--plan: {len(unknown)} unknown top-level key(s); '
                          f'allowed: {", ".join(sorted(plan_keys))}')
    if raw.get('schema') != schema:
        raise _InputError(f'--plan: "schema" must be "{schema}"')
    tasks = raw.get('tasks')
    if not isinstance(tasks, list) or not tasks or not all(isinstance(t, dict) for t in tasks):
        raise _InputError('--plan: "tasks" must be a non-empty list of objects')
    if 'review_of' in raw and not isinstance(raw['review_of'], dict):
        raise _InputError('--plan: "review_of" must be an object')
    return raw


def _string_list(value, what: str) -> list:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise _InputError(f'--grants: {what} must be a list of non-empty strings')
    if len(set(value)) != len(value):
        raise _InputError(f'--grants: {what} has a duplicate entry')
    return value


def _profile_map(value, what: str, profiles: frozenset) -> dict:
    if not isinstance(value, dict):
        raise _InputError(f'--grants: "{what}" must be an object of profile -> list')
    for profile in value:
        if profile not in profiles:
            raise _InputError(f'--grants: "{what}" names a profile outside "profiles"')
    return {profile: _string_list(items, f'"{what}" entry') for profile, items in value.items()}


def _check_grants_shape(raw, members: list, elevation, route_config, route_error) -> dict:
    """Every HostGrants field explicit and allowlisted; owner is never accepted from the file."""
    if not isinstance(raw, dict):
        raise _InputError('--grants: top level must be a JSON object')
    if 'owner' in raw:
        raise _InputError('--grants: "owner" is not accepted; the owner is the launch principal')
    unknown, missing = set(raw) - GRANTS_KEYS, GRANTS_KEYS - set(raw)
    if unknown:
        raise _InputError(f'--grants: {len(unknown)} unknown key(s); allowed: {", ".join(sorted(GRANTS_KEYS))}')
    if missing:
        raise _InputError(f'--grants: missing required key(s): {", ".join(sorted(missing))}')
    if raw['schema'] != GRANTS_SCHEMA:
        raise _InputError(f'--grants: "schema" must be "{GRANTS_SCHEMA}"')
    profiles = frozenset(_string_list(raw['profiles'], '"profiles"'))
    if profiles != frozenset(members):
        raise _InputError('--grants: "profiles" must list exactly the --profiles members')
    permits = _profile_map(raw['permits'], 'permits', profiles)
    if any(cap in elevation for caps in permits.values() for cap in caps):
        raise _InputError('--grants: "permits" names an authority-transferring capability; '
                          'these can never be granted')
    write_roots = _profile_map(raw['write_roots'], 'write_roots', profiles)
    reviewers = frozenset(_string_list(raw['reviewer_profiles'], '"reviewer_profiles"'))
    if not reviewers <= profiles:
        raise _InputError('--grants: "reviewer_profiles" must be a subset of "profiles"')
    if not isinstance(raw['routes'], dict):
        raise _InputError('--grants: "routes" must be an object with "backend" and optional "routes"')
    try:
        routes = route_config.of(raw['routes'])
    except route_error:
        raise _InputError('--grants: "routes" rejected by the core route contract '
                          '(backend "default", or "model_map" with name -> "provider:model")') from None
    return {'profiles': profiles,
            'permits': {p: frozenset(caps) for p, caps in permits.items()},
            'write_roots': {p: tuple(roots) for p, roots in write_roots.items()},
            'reviewer_profiles': reviewers,
            'routes': routes}


_CONTRACT_MSG = re.compile(r'^(I[0-9A-Za-z-]*) (plan|grants|tasks\[\d+\]|[A-Za-z][A-Za-z0-9_.-]{0,63}):')


def _contract_error(exc: Exception) -> str:
    """Rule id and target only; the core reason can quote plan values, so it is not shown."""
    m = _CONTRACT_MSG.match(str(exc))
    if not m:
        return 'plan rejected by the core contract; see docs/SWARM-PLAN-CLI.md'
    return f'plan rejected by the core contract (rule {m.group(1)} at {m.group(2)}); see docs/SWARM-PLAN-CLI.md'


def cmd_swarm(a) -> int:
    try:
        profiles = _parse_profiles(a.profiles)
    except ValueError as exc:
        return _err(str(exc))

    plan_path, grants_path = getattr(a, 'plan', None), getattr(a, 'grants', None)
    plan_mode = plan_path is not None or grants_path is not None
    if plan_mode:
        # Explicit pair, explicit start, never mixed with the conversation start:
        # all rejected before any core import, file read or allocation.
        if plan_path is None or grants_path is None:
            return _err('--plan and --grants must be given together')
        if not a.start:
            return _err('--plan/--grants require --start')
        if a.conversation or a.max_turns is not None or a.idle_timeout is not None:
            return _err('--plan/--grants cannot be combined with --conversation, --max-turns or --idle-timeout')

    # The bounds mean nothing without the conversation opt-in, and the opt-in
    # means nothing without --start: reject both before any core import or
    # allocation so a flag is never silently ignored.
    if (a.max_turns is not None or a.idle_timeout is not None) and not a.conversation:
        return _err('--max-turns/--idle-timeout require --conversation')
    if a.conversation and not a.start:
        return _err('--conversation requires --start')
    # Bounds must sit inside the core conversation range before anything is
    # created: values above the core maxima fail closed here, never later.
    if a.max_turns is not None and not 1 <= a.max_turns <= 10000:
        return _err('--max-turns must be a positive integer up to 10000')
    if a.idle_timeout is not None and not 0 < a.idle_timeout <= 86400:
        return _err('--idle-timeout must be a positive finite number up to 86400')

    try:
        from exo.group_launch import LaunchError, create
        from exo.core.group_host import ConversationOptions
    except ImportError as exc:
        return _err(f'this exo core has no swarm group launch support ({exc}); upgrade the exo package')

    plan_raw = grants_spec = None
    if plan_mode:
        try:
            from exo.core.agents.route_config import RouteConfig, RouteConfigError
            from exo.core.group_task_contract import (
                ELEVATION_CAPABILITIES, PLAN_KEYS, SCHEMA_VERSION, ContractError, HostGrants, validate_plan)
        except ImportError as exc:
            return _err(f'this exo core has no swarm plan support ({exc}); upgrade the exo package')
        # Shape before create: a malformed file never allocates a group or touches the store.
        try:
            plan_raw = _check_plan_shape(_read_json_file(plan_path, '--plan', PLAN_MAX_BYTES),
                                         SCHEMA_VERSION, PLAN_KEYS)
            grants_spec = _check_grants_shape(_read_json_file(grants_path, '--grants', GRANTS_MAX_BYTES),
                                              profiles, ELEVATION_CAPABILITIES, RouteConfig, RouteConfigError)
        except _InputError as exc:
            return _err(str(exc))

    # Fail closed before any allocation: group_launch.create validates profile IDs, >= 2 distinct
    # members, the owner seat ID, ownership and the mode/conductor pair.
    try:
        launch = create(a.seat, profiles=profiles, mode=a.mode, conductor=a.conductor)
    except LaunchError as exc:
        return _err(str(exc))

    async def run() -> None:
        """launch.start, serve and aclose share this one loop so runtime tasks never cross loops."""
        try:
            plan_id = None
            if plan_mode:
                # Bind after create, before start: owner comes from the created group (the
                # launch principal), never the file; the core contract and then the host
                # gate decide. No member engine or model call exists before this passes.
                try:
                    grants = HostGrants(owner=launch.group.owner, **grants_spec)
                    validated = validate_plan(plan_raw, grants, group=launch.group)
                except ContractError as exc:
                    raise LaunchError(_contract_error(exc)) from None
                try:
                    launch.start(plan=validated, grants=grants)
                except Exception as exc:
                    raise LaunchError(f'group plan start refused ({type(exc).__name__}); '
                                      'the core host gate rejected it') from None
                plan_id = validated.plan_id
                plan_tasks = len(validated.tasks)
            elif a.start:
                try:
                    if a.conversation:
                        kwargs = {}
                        if a.max_turns is not None:
                            kwargs['max_turns'] = a.max_turns
                        if a.idle_timeout is not None:
                            kwargs['idle_timeout'] = a.idle_timeout
                        launch.start(conversation=ConversationOptions(**kwargs))
                    else:
                        launch.start()
                except Exception as exc:
                    raise LaunchError(f'group start failed: {exc}') from exc
            from exo.server.app import serve
            where = 'stdio' if a.stdio or not a.socket else f'unix:{a.socket}'
            if plan_id is not None:
                state = f'started: validated plan {plan_id[:12]} with {plan_tasks} task(s)'
            elif a.conversation:
                state = 'started: bounded reactive conversation running'
                if a.max_turns is not None or a.idle_timeout is not None:
                    parts = []
                    if a.max_turns is not None:
                        parts.append(f'max {a.max_turns} turns per member')
                    if a.idle_timeout is not None:
                        parts.append(f'idle timeout {a.idle_timeout:g}s')
                    state += ' (' + ', '.join(parts) + ')'
                else:
                    state += ' (core default bounds)'
            elif a.start:
                state = 'started: one model turn per member scheduled (one-shot)'
            else:
                state = 'not started: waiting for an explicit principal /group start'
            print(f'exo swarm: group {launch.group.id} ({launch.group.mode}) serving on {where}; '
                  f'{state}', file=sys.stderr)
            task = asyncio.create_task(
                serve(launch.engine, stdio=True) if a.stdio or not a.socket
                else serve(launch.engine, socket=Path(a.socket).expanduser()))
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, task.cancel)
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            await launch.aclose()  # idempotent: settles the host even if serve already closed the engine

    try:
        asyncio.run(run())
    except LaunchError as exc:
        return _err(str(exc))
    except OSError as exc:  # from serve only (socket in use, unwritable path); never BaseException
        return _err(str(exc))
    except KeyboardInterrupt:
        pass
    return 0
