# Outputs Directory

`outputs/` is split into two zones:

- `outputs/exports/`: keep generated deliverables you may want to retain.
- `outputs/tmp/`: disposable previews, debug dumps, and session-only intermediates.

Rules:

- New scripts should write durable user-facing artifacts to `outputs/exports/`.
- New scripts should write previews, screenshots, temporary JSON, and debug material to `outputs/tmp/`.
- `outputs/tmp/` is ignored by Git and is safe to clean.
- `outputs/node_modules/` is local tooling state and is ignored by Git.
