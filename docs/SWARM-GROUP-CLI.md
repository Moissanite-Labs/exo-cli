# exo swarm-group (read-only, experimental)

`exo swarm-group` lets you look at swarm groups you own. It can list them and
read one group's message history. It cannot change anything.

## Commands

```
exo swarm-group list
exo swarm-group observe <group-id> [--after N] [--limit N]
```

- `list` prints the groups owned by the caller. It takes no options.
- `observe` prints messages from one owned group, oldest first.
  - `--after N` returns only messages with a sequence number above `N`.
    Range 0 to 9223372036854775807. Default 0.
  - `--limit N` caps the number of messages. Range 1 to 1000. Default 100.

To page through a long history, pass the last `seq` you saw as `--after`.

## How it talks to the core

Each command sends one request over the local Unix socket of the running core,
using the same authenticated client as `exo swarm-plan` and
`exo swarm-knowledge`:

| CLI | Request |
| --- | --- |
| `list` | `CommandRun(name='group', args='list')` |
| `observe G` | `CommandRun(name='group', args='observe {"after": N, "id": "G", "limit": N}')` |

There is no HTTP route and no TCP connection. The CLI reads no token file.

## Ownership

The CLI never sends an owner. The core works out the owner from the connection
and only returns that owner's groups. A group that belongs to someone else
looks the same as a group that does not exist. The CLI passes the core's
refusal through as `code: message` on stderr, with control characters
replaced. No flag overrides this.

## Input checks

The CLI checks arguments before it opens the socket:

- the group id must be non-empty, printable, and at most 128 characters
  (the core's own limit);
- `--after` and `--limit` must be plain decimal integers in range.

A bad `--after` or `--limit` exits with code 2 (argparse); a bad group id
exits with code 1. Either way nothing is sent.

## Output and errors

On success the command prints the core's answer as JSON on stdout and exits 0.
The CLI checks the answer's shape first. An answer with an unexpected shape is
an error, never an empty result. Message text is printed as JSON data and is
never interpreted.

On failure the command prints one line on stderr, starting with
`exo swarm-group:`, and exits 1. If the core cannot be reached the message is
always `cannot reach the swarm core on the local socket`. Exception text,
paths and credentials are never shown. Control characters in core messages are
replaced before printing.

## Limits of this slice

- Read-only. There is no send, create, start, pause, resume or stop.
- The core's `member` filter for `observe` is not exposed.
- The command needs a running swarm core with the `group` command. Older cores
  answer with their own refusal, which is reported the same way.
- This is one slice of the swarm CLI, not the full feature.
