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
