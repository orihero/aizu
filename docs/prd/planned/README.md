# Planned platform PRDs

Product requirements for platforms with **no engine implementation yet** — specs only, not
shipped work.

The platform PRDs one level up in [`../`](../) are different: each has a real engine under
`engine/aizu/engines/` and appears in `SUPPORTED_PLATFORMS` (`engine/aizu/core/config.py`).

- [`facebook-lead-agent-PRD.md`](facebook-lead-agent-PRD.md)
- [`pinterest-lead-agent-PRD.md`](pinterest-lead-agent-PRD.md)
- [`quora-lead-agent-PRD.md`](quora-lead-agent-PRD.md)
- [`threads-lead-agent-PRD.md`](threads-lead-agent-PRD.md)
- [`tiktok-lead-agent-PRD.md`](tiktok-lead-agent-PRD.md)

When one of these gets a real engine, move its PRD back up to `../` and add the platform to
`SUPPORTED_PLATFORMS`. Note that `engine/aizu/dispatch.py`'s not-implemented error message
builds the path `docs/prd/{platform}-lead-agent-PRD.md` dynamically — it is dormant for every
platform in this folder (the message only fires for supported platforms), but it will need
the `planned/` segment handled the day one of these ships.
