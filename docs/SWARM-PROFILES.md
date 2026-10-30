# Swarm profiles

`exo profile` creates named profiles (seats) on the core's profile store. A
profile is the principal identity a swarm seat runs as, so it is the first step
before launching a group with `exo swarm`.

## create

```
exo profile create NAME [--role ROLE] [--description TEXT] [--home PATH]
```

Creates a profile named `NAME` through the core's `profile_management.create_profile`
and prints the seat path it allocated, e.g.:

```
created profile seat1 at /Users/you/.exo/seats/seat1.toml
```

Options:

- `--role ROLE` — role for the profile (default: empty string). Typical values
  used by `exo swarm` are `conductor` and `member`.
- `--description TEXT` — free-form description (default: empty string).
- `--home PATH` — profile store root (default: `$EXO_HOME` or `~/.exo`).

Errors:

- If the core does not ship `profile_management` (it is part of the core
  runtime, PR #1021), the command prints a clear error and exits 1.
- If the core raises `ProfileError` (for example a duplicate name), the command
  prints the message to stderr and exits 1.

## Requirements

The core modules `exo.profile_management` must be importable — this command is a
thin CLI wrapper over the core's profile store. The `profile` command module is
loaded lazily, only when `exo profile` runs, and does not load the core profile
code itself.
