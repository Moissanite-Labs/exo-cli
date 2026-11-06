# exo swarm --plan / --grants

`exo swarm --profiles a,b --start --plan PLAN.json --grants GRANTS.json`

- Both files are required together and only with `--start`; incompatible with `--conversation`, `--max-turns`, `--idle-timeout`.
- Grants are the sole authority: nothing is inferred from plan metadata. Owner is the launch principal, never read from a file.
- Files: regular, UTF-8 JSON, size/depth bounded, no duplicate keys, no NaN/Infinity, unknown fields rejected. Errors never echo file content.
- Grants schema: `profiles` (exactly the `--profiles` members), `permits`, `write_roots`, `reviewer_profiles`, `routes`. Authority-transferring capabilities (the core's `ELEVATION_CAPABILITIES`) are refused; the list lives in the core only.
- Order: shape check -> create group -> core `validate_plan` -> `launch.start(plan=, grants=)` -> serve; the launch is always closed. Rejections report only the core rule id and target.
- The legacy path (no `--plan`) is unchanged.

## `--durable-attempts`

`exo swarm --profiles a,b --start --plan PLAN.json --grants GRANTS.json --durable-attempts`

- Opt in. It asks the core to record each task attempt in its durable attempt ledger (a table saved with the group store). Without the flag nothing changes: the CLI calls `launch.start(plan=, grants=)` exactly as before.
- With the flag the CLI calls `launch.start(plan=, grants=, durable_attempts=True)`. It passes only that one boolean. The core decides what it records.
- It needs `--plan`, `--grants` and `--start`. Any other use is refused before the core is imported, a file is read, or a group is created. It cannot be combined with `--conversation`, `--max-turns` or `--idle-timeout`.
- It adds no grants, no defaults and no permissions. Grants still come only from the grants file.
- Minimum core: a core whose `GroupLaunch.start` accepts `durable_attempts`. That is the durable dispatch change (core commit `dcf84474`) plus the launch change that forwards the argument. The CLI does not check the core version and does not emulate the feature. If the core is older, `launch.start` raises `TypeError` and the CLI prints `group plan start refused (TypeError)`.
- Limits: no retries and no crash recovery. The ledger has no terminal result record. A start that fails after the plan id is reserved uses up that plan id.
- Do not release this flag before the core change ships.

## `--knowledge`

`exo swarm --profiles a,b --start --knowledge` (also with `--conversation`, or with `--plan`/`--grants` and `--durable-attempts`)

- Opt in. It asks the core to use the group's owner-published knowledge snapshot for the start. Without the flag nothing changes: the CLI omits the `knowledge` keyword entirely, so `launch.start()`, `launch.start(conversation=)` and `launch.start(plan=, grants=[, durable_attempts=True])` are called exactly as before.
- With the flag the CLI adds `knowledge=True` to whichever start call it makes. It passes only that one boolean. The core decides what members see.
- It needs `--start`. Without it the CLI refuses before the core is imported, a file is read, or a group is created.
- It adds no grants, no defaults and no permissions. Grants still come only from the grants file.
- Minimum core: a core whose `GroupLaunch.start` accepts `knowledge` (core PR 1209). The CLI does not check the core version and does not emulate the feature. If the core is older, a plan start prints `group plan start refused (TypeError)` and the other starts print `group start failed: ...`.

## exo swarm-plan list / status (read-only)

`exo swarm-plan list GROUP [--limit N]` and `exo swarm-plan status GROUP PLAN [--limit N]`

- Read-only. Each call is one `CommandRun name=group` request over the local socket as principal `chat`. It never sends `owner`, grants, or any mutating subcommand. The core derives ownership from the connection principal.
- `list` sends `plan_list {"id": GROUP, "limit": N}`. `--limit` is 1..200, default 50.
- `status` sends `plan_status {"id": GROUP, "plan_id": FULL_ID, "limit": N}`. `--limit` is 1..200 (task rows), default 50.
- `PLAN` is either a full 64-character lowercase hex id or a lowercase hex prefix of 8..63 characters. Uppercase, non-hex, whitespace or shorter prefixes are refused before any transport.
- A full id makes exactly one request (`plan_status`).
- A prefix first sends `plan_list {"id": GROUP, "limit": 200}`, then:
  - if `plans_truncated` is true the CLI refuses, whatever the list holds (even zero or one match): a truncated list cannot prove a prefix is unique. Pass the full 64-hex id.
  - zero matches: `no such plan`; two or more: `ambiguous`;
  - exactly one valid full id in a complete list: one `plan_status` request with that full id.
  No `plan_status` request is sent in any refused case.
- Responses are shape-checked and fail closed: a malformed `plan_list` or `plan_status` reply (wrong types, missing fields, duplicate or invalid ids, wrong group or plan id echoed, too many rows for the limit) is an error, never treated as empty.
- Output is the core's JSON object printed as JSON only. Task `result`/`error` fields are untrusted engine output and are not interpreted. Errors are one line on stderr (`exo swarm-plan: ...`, rc 1) with control characters stripped and length bounded: transport failures print a fixed `transport` message (no exception text), core refusals print the core's code and message.
- Dependency: needs a core with the read-only `plan_list` and `plan_status` group subcommands (core PR 1210). `plan_status` only answers for plans the caller owns that have started with durable plan tables. Otherwise the core refuses. Older cores refuse the unknown subcommand and the CLI reports that refusal. The CLI does not check the core version.
- Tests are offline with a fake client. No live core integration is claimed.
