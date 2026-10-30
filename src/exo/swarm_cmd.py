"""exo swarm: launch one swarm group and serve it as an exod.

  exo swarm --profiles seat1,seat2 [--seat owner] [--socket PATH | --stdio] [--start]

Launches one group through the core's group_launch.create, binds the member
engines, and serves the owner engine on a Unix socket or stdio so the user
can drive it with exo chat and /group commands. The whole lifecycle
(launch.start, serve, launch.aclose) runs in ONE asyncio.run coroutine so
the group's runtime tasks stay on the same event loop.

With ``--start`` each member immediately runs one model turn (the core start
adapter starts the run right away). Without ``--start`` no run occurs until
an explicit principal /group start command from the connected client.

The core group_launch.create hardcodes conductor mode with the first profile
as conductor, so --mode/--conductor and the conversation bounds (socratic,
--max-turns, --idle-timeout) wait for group_launch to accept a mode instead
of the CLI faking one. Group membership is the explicit --profiles list,
never inferred from model output.

Principal group controls (/group start and friends) require a client on the
Unix socket; the stdio protocol alone cannot grant principal status. This
CLI does not relax authenticated principal handling.
"""

from __future__ import annotations

import asyncio
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
    p.add_argument('--start', action='store_true',
                   help='run one model turn per member immediately; without it no run occurs until a principal /group start')


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

    try:
        from exo.group_launch import LaunchError, create
    except ImportError as exc:
        return _err(f'this exo core has no swarm group launch support ({exc}); upgrade the exo package')

    # Fail closed before any allocation: group_launch.create validates profile IDs, >= 2 distinct
    # members, the owner seat ID and that the owner is not a member.
    try:
        launch = create(a.seat, profiles=profiles)
    except LaunchError as exc:
        return _err(str(exc))

    async def run() -> None:
        """launch.start, serve and aclose share this one loop so runtime tasks never cross loops."""
        try:
            if a.start:
                try:
                    launch.start()
                except Exception as exc:
                    raise LaunchError(f'group start failed: {exc}') from exc
            from exo.server.app import serve
            where = 'stdio' if a.stdio or not a.socket else f'unix:{a.socket}'
            if a.start:
                state = 'started: one model turn per member already run'
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
