# Repository work routing

Before opening or changing a PR, read [the canonical repository workflow](https://github.com/Moissanite-Labs/exo/blob/main/docs/REPOSITORY-WORKFLOW.md) (D-089) for shared work and [the local CLI routing](docs/REPOSITORY-WORKFLOW.md) for this fork's proposed transition.

This repository currently contains a fork snapshot, including shared code. New CLI-only terminal work belongs in `Moissanite-Labs/exo-cli`; shared core, plugins and web UI belong in `Moissanite-Labs/exo`. Native desktop shell and packaging belong in `Moissanite-Labs/exo-desktop`. Do not implement a second shared fix here; a temporary carry of a canonical Exo change follows the canonical carry rule.

The [CLI extraction proposal](docs/migration/CLI-EXTRACTION.md) has not changed package ownership or established a stable Python import API. Preserve existing terminal PR ownership and link related work. Do not remove shared files, move issues, or change consumers on the assumption that extraction is complete.

Preserve existing author ownership and unique work during the transition. A feature-branch merge, main integration, downstream sync and live installation are separate states. Follow the existing exact-head review and verification requirements. Do not treat this policy as authorization to change an active installation or credentials.
