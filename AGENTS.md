# Repository work routing

Before opening or changing a PR, read [the canonical repository workflow](https://github.com/Moissanite-Labs/exo/blob/main/docs/REPOSITORY-WORKFLOW.md) (D-089).

This repository currently contains a fork snapshot, including shared code. Shared core, plugins and web UI belong in `Moissanite-Labs/exo`. Native desktop shell and packaging belong in `Moissanite-Labs/exo-desktop`. Do not implement a second shared fix here.

The [CLI extraction proposal](docs/migration/CLI-EXTRACTION.md) has not changed package ownership or established a separate core API. Preserve existing terminal PR ownership and link related work. Do not remove shared files, move issues, or change consumers on the assumption that extraction is complete.

Preserve existing author ownership and unique work during the transition. A feature-branch merge, main integration, downstream sync and live installation are separate states. Follow the existing exact-head review and verification requirements. Do not treat this policy as authorization to change an active installation or credentials.
