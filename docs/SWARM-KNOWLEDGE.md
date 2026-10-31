# Swarm knowledge

`exo swarm-knowledge` uses the local core's `group` command. It requires
shared-knowledge support from core PR #1024. Authorization stays in the core.
The client does not open the knowledge store or supply an owner identity.

## Commands

```sh
exo swarm-knowledge publish GROUP --key KEY --text TEXT [--source SRC] [--expected-revision N]
exo swarm-knowledge read GROUP --member MEMBER --key KEY
exo swarm-knowledge list GROUP --member MEMBER [--limit N]
exo swarm-knowledge history GROUP --member MEMBER --key KEY [--limit N]
```

`--text` supplies one string. Repeat `--source` to record multiple sources.
`--expected-revision` defaults to 0. The core rejects stale revisions.
`--limit` ranges from 1 to 1000 and defaults to 100.

```sh
exo swarm-knowledge publish grp-7 --key doc.guide --text "Intro" --source docs/guide.md --expected-revision 0
exo swarm-knowledge read grp-7 --member researcher --key doc.guide
exo swarm-knowledge list grp-7 --member researcher --limit 50
exo swarm-knowledge history grp-7 --member researcher --key doc.guide --limit 20
```

Read, list and history require an explicit member. These are owner inspections,
not member execution. The core returns the `human_owner_view` label and checks
stored group ownership and membership. Inspection does not acknowledge messages.

Successful commands print the core's output. Knowledge objects are printed as
JSON. Errors go to stderr with a nonzero exit code. The connection uses the
local Unix socket, not a remote token or a client-side store.

This slice adds CLI access to shared knowledge (M02 and U01). It does not add
automatic knowledge injection into member turns, publication workflows, or
desktop controls. The full Swarm build remains incomplete.
