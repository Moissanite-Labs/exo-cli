"""exo swarm: launch one swarm group and serve it as an exod.

  exo swarm --profiles seat1,seat2 [--seat owner] [--socket PATH | --stdio] [--start]
             [--mode conductor|socratic] [--conductor PROFILE] [--conversation]
             [--max-turns N] [--idle-timeout SECONDS]

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

Principal group controls (/group start and friends) require a client on the
Unix socket; the stdio protocol alone cannot grant principal status. This
CLI does not relax authenticated principal handling.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import signal
import sys
from pathlib import Path


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


def cmd_swarm(a) -> int:
    try:
        profiles = _parse_profiles(a.profiles)
    except ValueError as exc:
        return _err(str(exc))

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

    # Fail closed before any allocation: group_launch.create validates profile IDs, >= 2 distinct
    # members, the owner seat ID, ownership and the mode/conductor pair.
    try:
        launch = create(a.seat, profiles=profiles, mode=a.mode, conductor=a.conductor)
    except LaunchError as exc:
        return _err(str(exc))

    async def run() -> None:
        """launch.start, serve and aclose share this one loop so runtime tasks never cross loops."""
        try:
            if a.start:
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
            if a.conversation:
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
