# exo swarm --plan / --grants

`exo swarm --profiles a,b --start --plan PLAN.json --grants GRANTS.json`

- Both files are required together and only with `--start`; incompatible with `--conversation`, `--max-turns`, `--idle-timeout`.
- Grants are the sole authority: nothing is inferred from plan metadata. Owner is the launch principal, never read from a file.
- Files: regular, UTF-8 JSON, size/depth bounded, no duplicate keys, no NaN/Infinity, unknown fields rejected. Errors never echo file content.
- Grants schema: `profiles` (exactly the `--profiles` members), `permits`, `write_roots`, `reviewer_profiles`, `routes`. Authority-transferring capabilities (the core's `ELEVATION_CAPABILITIES`) are refused; the list lives in the core only.
- Order: shape check -> create group -> core `validate_plan` -> `launch.start(plan=, grants=)` -> serve; the launch is always closed. Rejections report only the core rule id and target.
- The legacy path (no `--plan`) is unchanged.
