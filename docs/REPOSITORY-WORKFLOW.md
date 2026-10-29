# Repository work routing during CLI extraction

The [canonical workflow in Exo](https://github.com/Moissanite-Labs/exo/blob/main/docs/REPOSITORY-WORKFLOW.md) governs shared changes. This fork is a snapshot. The presence of shared source here does not transfer ownership.

| Change | Implementation home |
| --- | --- |
| Core, providers, memory, permissions, protocol, shared plugins and web UI | `Moissanite-Labs/exo` |
| Native desktop shell, packaging and bridges | `Moissanite-Labs/exo-desktop` |
| New CLI-only terminal work, including TUI and CLI-only command UX, and CLI extraction design | `Moissanite-Labs/exo-cli` |

The [extraction inventory](migration/CLI-EXTRACTION.md) describes current imports, package coupling and the proposed transition. This third-repository routing is a proposed [D-089 amendment](../DECISIONS.md), pending cross-repository review; the canonical Exo workflow continues to govern shared work. CLI-only command UX does not include the shared core server entry point, even where both currently live in `exo.cli`.

File new CLI-only issues and implement their fixes here. Until extraction completes, Exo's root distribution still ships its own TUI copy: link the CLI PR to an Exo counterpart, identify the author and exact source commit, and coordinate a reviewed integration PR so the fix reaches that shipped copy. Verify the resulting Exo behavior and preserve author-owned work; a CLI merge alone does not update Exo or any installation. Cross-cutting changes have an Exo implementation for the shared part and linked client work for the distinct CLI part. Do not move existing issues or PRs without coordinating with their owners.

Preserve existing authors, branches, tests and review findings. Link related work before changing an existing terminal PR's home. Implement a shared fix once in Exo. Bring reviewed upstream changes into downstream repositories without discarding their unique work.

Use isolated branches, independent review of the exact head and relevant verification. Source integration, downstream sync and live installation are separate states. A repository change does not authorize restarting an active installation or changing credentials, models or permissions.
