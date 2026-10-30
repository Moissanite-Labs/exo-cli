# Swarm profiles

`exo profile` creates named profiles (seats) on the core's profile store, reads
and updates their `role`/`description` revision. A profile is the principal
identity a swarm seat runs as, so it is the first step before launching a group
with `exo swarm`.

Two commands are the canonical way to change a profile: `exo profile digest`
prints the revision fingerprint, and `exo profile update` presents it back. The
CLI never captures a digest automatically and applies it in one step — the value
has to reach the shell first, so an update always says which revision the caller
saw.

## create

```
exo profile create NAME [--role ROLE] [--description TEXT] [--home PATH]
```

Creates a profile named `NAME` through the core's `profile_management.create_profile`
and prints the seat path it allocated, e.g.:

```
exo profile: created profile seat1 at /Users/you/.exo/seats/seat1.toml
```

Options:

- `--role ROLE` — role for the profile (default: empty string). Typical values
  used by `exo swarm` are `conductor` and `member`.
- `--description TEXT` — free-form description (default: empty string).
- `--home PATH` — profile store root (default: `$EXO_HOME` or `~/.exo`).

## digest

```
exo profile digest NAME [--home PATH]
```

Prints the core's `profile_management.profile_digest` fingerprint for the seat's
exact bytes and nothing else — a single `sha256:<64 lowercase hex>` line — so it
can be captured directly:

```
digest=$(exo profile digest seat1)
```

Reading a digest writes nothing: it takes no lock, creates no directory, and
reads no key, principal store or `config.json`. Only a regular, owned
`<home>/seats/<name>.toml` that `update` could rewrite has a digest; a missing
profile, the `<name>.json` form, the `<name>/seat.*` form, a symlink or a
non-canonical file is refused instead of being given one.

## update

```
exo profile update NAME --expected-digest sha256:... [--role ROLE] [--description TEXT] [--home PATH]
```

Rewrites `role` and/or `description` of that one canonical seat file and prints
where it now is:

```
exo profile: updated profile seat1 at /Users/you/.exo/seats/seat1.toml
```

A round trip is explicit:

```
digest=$(exo profile digest seat1)
exo profile update seat1 --expected-digest "$digest" --role member
```

Options:

- `--expected-digest DIGEST` — required. The fingerprint `exo profile digest`
  printed for the revision being changed. The core compares it to the file's
  current bytes and refuses the update if they differ, so a revision edited in
  the meantime is never silently overwritten.
- `--role ROLE` — new role. Omitted means "keep the current value"; `--role ""`
  clears it.
- `--description TEXT` — new description, same rule (`--description ""` clears).
- `--home PATH` — profile store root (default: `$EXO_HOME` or `~/.exo`).

At least one of `--role`/`--description` is required. The command never creates
the home or the seats directory, never creates a profile, and writes only those
two fields: neither principals, keys, the ledger nor `config.json` are touched.

### What the digest check is not

`--expected-digest` is a fingerprint check, not an atomic compare-and-swap. The
core serializes cooperative updaters with an exclusive lock on a sibling
`.<name>.lock` file and re-checks the bytes just before replacing the file, but
the re-check and the replace are still two steps: an external process that
writes `<name>.toml` without taking that lock, in that window, can still have
its edit overwritten (or be the writer that lands after you). Use one writer per
profile, or a lock-aware tool, if that race matters. The `.<name>.lock` file is
permanent metadata and is the only extra file a refused update may leave behind.

Errors:

- Arguments are parsed before any command logic runs: omitting a required value —
  e.g. `--expected-digest` for `update`, or the profile name — makes argparse print
  usage to stderr and exit 2. The runtime refusals below are distinct and exit 1.
- If the core does not ship `profile_management` (part of the core runtime,
  PR #1021), or ships it without `profile_digest`/`update_profile` (PR #1022),
  the command prints a clear error and exits 1 — create-only cores keep working
  for `create`.
- If the core raises `ProfileError` — a duplicate name, a malformed digest, a
  stale digest or a profile locked by another updater (`ProfileConflict` is a
  `ProfileError`) — the command prints the message to stderr as
  `exo profile: ...` and exits 1.

## Requirements

The core module `exo.profile_management` must be importable — these commands are
thin CLI wrappers over the core's profile store. The `profile` command module is
loaded lazily, only when `exo profile` runs; it does not load the core profile
code itself, so `exo --help` and `exo profile <cmd> --help` work without it.
