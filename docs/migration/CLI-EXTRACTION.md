# CLI extraction inventory and proposed sequence

This is a migration proposal, not a claim that the repositories are already separated. On 2026-10-29,
`Moissanite-Labs/exo-cli` was forked from `Moissanite-Labs/exo` at
`69e33d5018fa50e0a75c44e967b251742db13d61`. The requested direction is for Exo to own the core engine and
for the CLI, desktop app and Exo OS to consume it. The interface and release strategy below need review by
the affected owners before files are removed or consumers are switched.

## What exists at the fork point

- The root `pyproject.toml` builds the `exo` distribution with both `src/exo` and `clients/tui/exo_tui` in
  one wheel. It registers `exo = exo.cli:main` and `exo-witness`.
- `clients/tui/pyproject.toml` also defines an `exo-tui` distribution and entry point. Its comment says to
  install the root package first. It does not declare a core dependency. The two manifests both include
  `exo_tui`, so independently installing both distributions can put the same package under two owners.
- `src/exo/cli.py` dispatches many commands and starts the TUI. Tests and integrations import `exo.cli`
  directly. Moving it requires a compatibility and ownership plan for commands, not just copying the TUI.
- Core startup also depends on this module: `exo.ensure_core._daemonize` executes `python -P -m exo.cli serve`.
  The baseline CLI imports `resolve_store` from `exo.cli`, `update_cmd` self-checks its parser and module
  entry point, and `gate.py` registers `exo.cli`. Until those dependencies change, the core needs the server
  entry point or this dependency; removing `exo.cli` would break background startup, including the web path.
- The TUI uses `exo.protocol.schema` and `exo.protocol.client`, but also imports configuration, engine,
  server and other core modules. `exo_tui.conn` can create an in-process engine and server, or connect to
  an existing core. Treat the local startup and writer-lock behavior as part of the migration surface.
- Desktop currently starts `exo web` and consumes the `exo-command` web plugin. That web surface and the
  CLI launcher need an explicit home and supported launch contract before the core repository is trimmed.
  Exo OS's actual integration must be inventoried with its owner. This fork does not establish it.

## Proposed sequence

1. Inventory CLI commands, imports, packaging assets and consumers with their owners. Agree on the web
   launcher and Exo OS boundaries, distribution names, version compatibility and a stable Python import API
   and cross-distribution dependency. The wire protocol already declares version 1 and additive-only changes
   within a version; that existing contract is distinct from a stable Python import API.
2. Add compatibility and integration tests for command entry points, schema/client behavior, local core
   startup, writer-lock handoff, and desktop launch. Include mismatched versions and missing optional
   dependencies. Run them with independently built artifacts in clean environments.
3. Establish a single distribution owner for each Python package and a versioned core dependency. Test
   installing, upgrading and uninstalling each artifact in both orders without file collisions. Keep old
   commands and imports working through a documented transition until consumers migrate.
4. Port CLI-only changes with their authors and exact source commits, then switch consumers in reviewed
   steps. Only after the compatibility tests and downstream integrations pass should redundant code be
   removed from the core tree. No branch merge or repository transfer implies a deployed runtime update.

## Existing work at the snapshot

Classification describes the touched behavior, not permission to move or close a PR. Heads and state can
change. Recheck them and coordinate with each author before porting or retargeting.

| Work | Initial classification | Evidence |
| --- | --- | --- |
| [Exo #666](https://github.com/Moissanite-Labs/exo/pull/666) | CLI/TUI | `clients/tui/exo_tui` and TUI tests |
| [Exo #778](https://github.com/Moissanite-Labs/exo/pull/778) | Core | `src/exo/core/loop.py` and core tests |
| [Exo #886](https://github.com/Moissanite-Labs/exo/pull/886) | Web/shared consumer | `plugins/exo-command`. Final web ownership needs agreement |
| [Exo #755](https://github.com/Moissanite-Labs/exo/pull/755) | Web/shared consumer with desktop counterpart | Web UI files. [Desktop #83](https://github.com/Moissanite-Labs/exo-desktop/pull/83) is paired work |
| [Desktop #93](https://github.com/Moissanite-Labs/exo-desktop/pull/93) | Desktop sync maintenance | Transfer repair, separate from CLI extraction |

This is a small sample, not a complete inventory of open work. Existing PR numbers, review evidence and
owner branches remain intact.

## Issue routing

Issues are enabled in `Moissanite-Labs/exo-cli`. File and implement newly identified CLI-only terminal work
there. Coordinate a linked, reviewed Exo integration while its root wheel still ships the TUI; see the
[local routing](../REPOSITORY-WORKFLOW.md). Shared engine and web work stays in Exo; split cross-cutting work
into linked changes for its distinct parts, preserving current authors and branches.

## Related records

[D-010 and D-020](../../DECISIONS.md) describe the standalone protocol and Exo OS. See also
[ENGINE-SPLIT](../ENGINE-SPLIT.md), [CORE-V2-INTERFACES](../CORE-V2-INTERFACES.md), and
[STANDALONE §5.1](../STANDALONE.md#51-shape) for the existing core and versioned wire-protocol boundaries.

[Exo #665](https://github.com/Moissanite-Labs/exo/issues/665), the terminal reasoning display, is a
CLI-only transfer candidate. Its implementation [#666](https://github.com/Moissanite-Labs/exo/pull/666)
is still open in Exo, so coordinate with that author and preserve the PR and review links before moving
or duplicating anything. [#677](https://github.com/Moissanite-Labs/exo/issues/677) spans terminal, web,
core stream recovery and replay safety. [#259](https://github.com/Moissanite-Labs/exo/issues/259) spans
TUI and core refactoring. Keep both in Exo unless owners agree on a split. This is a bounded candidate
list, not a bulk-transfer instruction.
