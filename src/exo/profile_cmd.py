"""exo profile: create a profile (a named seat) on the core's profile store.

  exo profile create NAME [--role ROLE] [--description TEXT] [--home PATH]

Creates a profile through the core's ``profile_management.create_profile``, which
allocates a seat under the profile store and returns it. The profile's seat path
is printed on success. ``--home`` selects the profile store root; the default is
``$EXO_HOME`` (``~/.exo``). The role and description are free-form metadata the
core records on the seat.

This CLI needs the updated core (swarm PR #1021): ``exo.profile_management``
with ``create_profile`` and ``ProfileError``. It does not fake either behavior;
the missing-feature error is explicit. Profile creation is a metadata write to
the profile store; it does not touch principals, keys, the ledger, or config.
"""
from __future__ import annotations

import sys
from pathlib import Path


def register(sp, common) -> None:
    p = sp.add_parser(
        'profile',
        help='create profiles (seats) on the profile store [experimental]')
    sub = p.add_subparsers(dest='profile_cmd', required=True)
    c = sub.add_parser('create', parents=[common], help='create a new profile (seat)')
    c.add_argument('name', help='profile/seat id')
    c.add_argument('--role', default='', help='role for the profile (default: none)')
    c.add_argument('--description', default='',
                   help='human-readable description (default: none)')
    c.add_argument('--home',
                   help='profile store root (default: $EXO_HOME, then ~/.exo)')


def _err(msg: str) -> int:
    print(f'exo profile: {msg}', file=sys.stderr)
    return 1


def cmd_profile(a) -> int:
    try:
        from exo.profile_management import ProfileError, create_profile
    except ModuleNotFoundError as exc:
        if exc.name != 'exo.profile_management':
            raise
        return _err('this exo core has no profile management support (need core PR #1021); '
                    'upgrade the exo package')
    if a.home:
        home = Path(a.home).expanduser()
    else:
        from .store import exo_home
        home = Path(exo_home()).expanduser()
    try:
        seat = create_profile(home, a.name, role=a.role, description=a.description)
    except ProfileError as exc:
        return _err(str(exc))
    print(f'exo profile: created profile {a.name} at {seat.path}')
    return 0
