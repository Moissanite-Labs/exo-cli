"""exo profile: create, update and fingerprint profiles (seats) on the core's profile store.

  exo profile create NAME [--role ROLE] [--description TEXT] [--home PATH]
  exo profile digest NAME [--home PATH]
  exo profile update NAME --expected-digest sha256:... [--role ROLE] [--description TEXT]
                         [--home PATH]

Creates a profile through the core's ``profile_management.create_profile``, which
allocates a seat under the profile store and returns it. The profile's seat path
is printed on success. ``--home`` selects the profile store root; the default is
``$EXO_HOME`` (``~/.exo``). The role and description are free-form metadata the
core records on the seat.

``digest`` prints the core's ``profile_digest`` fingerprint (``sha256:<hex>``)
and nothing else, so it can be captured directly; ``update`` presents that
fingerprint back as ``--expected-digest`` and rewrites ``role``/``description``
of the same seat. The digest is never captured automatically and then fed to an
update in one shot: the user has to read the revision first and say which one
they saw, so a revision that moved in between is refused (rc 1) instead of being
silently overwritten. ``--role``/``--description`` default to "keep the current
value", and an explicitly empty string clears the field; at least one of them
must be given. A refusal from the core (including a stale digest or a locked
profile) is reported with the same ``exo profile:`` prefix and rc 1.

This CLI needs the updated core (swarm PR #1021 for create, PR #1022 for digest
and update): ``exo.profile_management`` with ``create_profile``, ``profile_digest``,
``update_profile``, ``ProfileError`` and ``ProfileConflict``. It does not fake any
of that behavior; a missing module or a module without the requested API is an
explicit error. Profiles are metadata on the profile store: nothing here touches
principals, keys, the ledger, or config.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

_CORE = 'exo.profile_management'

_MISSING_CREATE = ('this exo core has no profile management support (need core PR #1021); '
                   'upgrade the exo package')

_MISSING_REVISION = ('this exo core has no profile digest/update support (need core PR #1022); '
                     'upgrade the exo package')


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
    d = sub.add_parser('digest', parents=[common],
                       help='print the revision fingerprint of an existing profile')
    d.add_argument('name', help='profile/seat id')
    d.add_argument('--home',
                   help='profile store root (default: $EXO_HOME, then ~/.exo)')
    u = sub.add_parser('update', parents=[common],
                       help='rewrite role/description of an existing profile revision')
    u.add_argument('name', help='profile/seat id')
    u.add_argument('--expected-digest', required=True,
                   help='digest printed by `exo profile digest NAME`')
    u.add_argument('--role', default=None,
                   help='new role (default: keep the current one; "" clears it)')
    u.add_argument('--description', default=None,
                   help='new description (default: keep the current one; "" clears it)')
    u.add_argument('--home',
                   help='profile store root (default: $EXO_HOME, then ~/.exo)')


def _err(msg: str) -> int:
    print(f'exo profile: {msg}', file=sys.stderr)
    return 1


def _core(*names: str, missing: str) -> tuple[Any, ...] | None:
    """Import the requested core names lazily; a missing module or API is a printed error, not a fake.

    ``missing`` is the message for *this* command's missing capability, so an older core that
    only ships ``create_profile`` says so for digest/update instead of claiming nothing exists.
    A ``ModuleNotFoundError`` for some other module is a real failure and is re-raised.
    """
    try:
        module = __import__(_CORE, fromlist=list(names))
    except ModuleNotFoundError as exc:
        if exc.name != _CORE:
            raise
        _err(missing)
        return None
    except ImportError:
        _err(missing)
        return None
    absent = [name for name in names if not hasattr(module, name)]
    if absent:
        _err(f'{missing} (missing {", ".join(absent)})')
        return None
    return tuple(getattr(module, name) for name in names)


def _home(a) -> Path:
    """The profile store root: ``--home``, else the shared ``exo_home()`` default."""
    if a.home:
        return Path(a.home).expanduser()
    from .store import exo_home
    return Path(exo_home()).expanduser()


def _create(a) -> int:
    core = _core('create_profile', 'ProfileError', missing=_MISSING_CREATE)
    if core is None:
        return 1
    create_profile, profile_error = core
    home = _home(a)
    try:
        seat = create_profile(home, a.name, role=a.role, description=a.description)
    except profile_error as exc:
        return _err(str(exc))
    print(f'exo profile: created profile {a.name} at {seat.path}')
    return 0


def _digest(a) -> int:
    core = _core('profile_digest', 'ProfileError', missing=_MISSING_REVISION)
    if core is None:
        return 1
    profile_digest, profile_error = core
    home = _home(a)
    try:
        digest = profile_digest(home, a.name)
    except profile_error as exc:
        return _err(str(exc))
    print(digest)  # the digest alone, so `$(exo profile digest NAME)` is usable as-is
    return 0


def _update(a) -> int:
    # Checked before any core import or store resolution: a bare --expected-digest changes nothing.
    if a.role is None and a.description is None:
        return _err('update needs --role and/or --description: pass "" to clear a field')
    core = _core('update_profile', 'ProfileError', missing=_MISSING_REVISION)
    if core is None:
        return 1
    update_profile, profile_error = core
    home = _home(a)
    try:
        # ProfileConflict is a ProfileError, so a stale digest or a held lock lands here too.
        seat = update_profile(home, a.name, expected_digest=a.expected_digest,
                              role=a.role, description=a.description)
    except profile_error as exc:
        return _err(str(exc))
    print(f'exo profile: updated profile {a.name} at {seat.path}')
    return 0


_HANDLERS: dict[str, Callable[[Any], int]] = {
    'create': _create,
    'digest': _digest,
    'update': _update,
}


def cmd_profile(a) -> int:
    return _HANDLERS[a.profile_cmd](a)
