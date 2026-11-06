# exo swarm-group (experimental)

`exo swarm-group` works with swarm groups you own. It can list them, read one
group's message history, and store a human message in a group. It cannot create,
start, pause, resume or stop a group.

## Commands

```
exo swarm-group list
exo swarm-group observe <group-id> [--after N] [--limit N]
exo swarm-group send <group-id> --text TEXT [--audience PROFILE ...]
```

- `list` prints the groups owned by the caller. It takes no options.
- `observe` prints messages from one owned group, oldest first.
  - `--after N` returns only messages with a sequence number above `N`.
    Range 0 to 9223372036854775807. Default 0.
  - `--limit N` caps the number of messages. Range 1 to 1000. Default 100.

- `send` stores one human message in an owned group and prints the stored
  message.
  - `--text` is required. It must not be blank and may be at most 16000
    characters. This is a CLI limit. The core itself only requires non-blank.
  - `--audience PROFILE` names a member to address. Repeat it for several. Each
    target is printable, 1 to 128 characters. Duplicates are dropped, keeping
    the first occurrence and the order. At most 64 distinct targets.
  - Without `--audience`, the CLI leaves the field out and the core addresses
    every member. The CLI never sends an owner as the audience.

To page through a long history, pass the last `seq` you saw as `--after`.

### What `send` does and does not promise

`send` stores a message. It does not promise that any member processes it. It
does not rewrite a prompt that is already in flight. If the core process has a
host for this same seat and store, that host may also wake a matching running
conversation; the CLI cannot tell whether that happened.

Warning: a failed response can follow persistence. The core may have stored the
message even when `send` exits non-zero, and the CLI does not retry. Repeating
the command by hand may duplicate the message.

## How it talks to the core

Each command sends one request over the local Unix socket of the running core,
using the same authenticated client as `exo swarm-plan` and
`exo swarm-knowledge`:

| CLI | Request |
| --- | --- |
| `list` | `CommandRun(name='group', args='list')` |
| `observe G` | `CommandRun(name='group', args='observe {"after": N, "id": "G", "limit": N}')` |
| `send G` | `CommandRun(name='group', args='send {"id": "G", "text": "T"}')`, plus `"audience": [...]` only when `--audience` is given |

There is no HTTP route and no TCP connection. The CLI reads no token file.

## Ownership

The CLI never sends an owner. The core works out the owner from the connection
and only returns that owner's groups. A group that belongs to someone else
looks the same as a group that does not exist. A target that is not a member
is refused by the core. The CLI passes the core's
refusal through as `code: message` on stderr, with control characters
replaced. No flag overrides this.

## Input checks

The CLI checks arguments before it opens the socket:

- the group id must be non-empty, printable, and at most 128 characters
  (the core's own limit);
- `--after` and `--limit` must be plain decimal integers in range;
- `--text` and each `--audience` must meet the `send` bounds above.

A bad `--after`, `--limit`, `--text` or `--audience` exits with code 2
(argparse). A bad group id exits with code 1. Either way nothing is sent.

## Output and errors

On success the command prints the core's answer as JSON on stdout and exits 0.
The CLI checks the answer's shape first. For `send` it also checks that the
stored message is a human message with the text it sent (and the audience, when
one was given). A boolean or non-positive `seq` is rejected. An answer with an unexpected shape is
an error, never an empty result. Message text is printed as JSON data and is
never interpreted.

On failure the command prints one line on stderr, starting with
`exo swarm-group:`, and exits 1. If the core cannot be reached the message is
always `cannot reach the swarm core on the local socket`. Exception text,
paths and credentials are never shown. Control characters in core messages are
replaced before printing.

## Limits of this slice

- Only `send` writes. There is no create, start, pause, resume, stop or retry,
  and a failed `send` is never retried: the CLI cannot tell whether the core
  stored the message before the failure.
- The core's `member` filter for `observe` is not exposed.
- The command needs a running swarm core with the `group` command. Older cores
  answer with their own refusal, which is reported the same way.
- This is one slice of the swarm CLI, not the full feature.
