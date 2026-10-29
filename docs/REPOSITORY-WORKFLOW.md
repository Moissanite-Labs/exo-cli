# Repository work routing during CLI extraction

The [canonical workflow in Exo](https://github.com/Moissanite-Labs/exo/blob/main/docs/REPOSITORY-WORKFLOW.md) governs shared changes. This fork is a snapshot. The presence of shared source here does not transfer ownership.

| Change | Implementation home |
| --- | --- |
| Core, providers, memory, permissions, protocol, shared plugins and web UI | `Moissanite-Labs/exo` |
| Native desktop shell, packaging and bridges | `Moissanite-Labs/exo-desktop` |
| CLI extraction design and terminal work explicitly assigned here | `Moissanite-Labs/exo-cli` |

The [extraction inventory](migration/CLI-EXTRACTION.md) describes current imports, package coupling and the proposed transition. It does not establish a separate engine API or authorize removing shared files, moving issues or switching consumers.

Preserve existing authors, branches, tests and review findings. Link related work before changing an existing terminal PR's home. Implement a shared fix once in Exo. Bring reviewed upstream changes into downstream repositories without discarding their unique work.

Use isolated branches, independent review of the exact head and relevant verification. Source integration, downstream sync and live installation are separate states. A repository change does not authorize restarting an active installation or changing credentials, models or permissions.
