# History

Superseded design documents, kept for the reasoning rather than the designs.

These were written when nixadmin was a set of modules inside the
[nixlap](https://github.com/hinstef/nixlap) NixOS config repo, before it became a
flake of its own. They moved here so nixlap holds only its own configuration —
none of them describe how nixadmin works today.

| Document | Superseded by | Still worth reading for |
|---|---|---|
| [`nixadmin-v2-spec.md`](nixadmin-v2-spec.md) | [`../nixadmin-v3-spec.md`](../nixadmin-v3-spec.md) | Why the agent is not in a container, and the gfx1103 ROCm-vs-Vulkan findings |
| [`nixadmin-product.md`](nixadmin-product.md) | [`../vision.md`](../vision.md), [`../ux.md`](../ux.md) | The original framing of the non-technical user the project is for |
| [`approach.md`](approach.md) | [`../PROGRESS.md`](../PROGRESS.md), [`../adr/`](../adr/) | The v1→v2 decision log, and the spec-first workflow the project still uses |

Nothing here is maintained. Where it contradicts the current docs, the current
docs win.
