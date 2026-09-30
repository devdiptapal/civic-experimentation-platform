# Roadmap

This roadmap describes a conservative, staged path from documentation scaffold to reusable public-service experimentation practice.

The timeline and targets below are planning assumptions, not commitments.

## Phase 1: Pilot-Ready Release

**Goal:** Provide a practical package that one agency team could use to run a low-risk pilot.

**Focus areas:**
- Documentation completeness
- Basic governance process
- Clear privacy and data-handling rules
- Reusable evaluation and approval templates

**Outputs:**
- [x] Stable `v0.x` documentation set
- [x] Pilot deployment guide
- [x] Experiment approval checklist
- [x] Evaluation template and sample config
- [x] Working reference implementation of all five components (`v0.2.0`)
- [x] Worked example, reproducible end to end on synthetic data
- [ ] Pilot with a public benefits agency

**KPIs:**
- Pilots with a numeric effect of interest fixed before launch: **100%**
  (enforced — a config without one does not load)
- Pilots with explicit rollback criteria: **100%** (enforced — guardrails
  require numeric tolerances)
- Pilot setup time: to be measured against a first agency pilot
- Share of pilot artifacts completed before launch: to be measured

## Phase 2: Multi-Agency Reuse

**Goal:** Support reuse across multiple agencies or jurisdictions with minimal rework.

**Focus areas:**
- Configuration and terminology portability
- Better onboarding materials
- Shared metrics vocabulary
- Documentation for adaptation across legal/policy contexts

**Planned outputs:**
- Versioned template bundles for agency adaptation
- Cross-jurisdiction implementation notes
- Expanded glossary and examples

**Example KPIs (placeholders):**
- Number of agencies reusing templates with local adaptation: `__`
- Time to adapt core docs for a new jurisdiction: `__` days
- Percentage of reuse cases requiring only docs/config changes (no code): `__%`

## Phase 3: Replication & Sustainability

**Goal:** Make replication and long-term maintenance feasible for public-interest teams.

**Focus areas:**
- Transparent maintenance model
- Training and handoff guidance
- Versioning and release discipline
- Sustainable contributor workflows

**Planned outputs:**
- Maintainer playbook
- Replication guide and implementation case templates
- Regular release cadence and changelog norms

**Example KPIs (placeholders):**
- Documentation update turnaround time: `__` days
- Number of active maintainers per quarter: `__`
- Percentage of releases with complete governance/privacy review notes: `__%`

## Assumptions and Boundaries

- This roadmap does not assume any specific funding, procurement path, or production deployment.
- Agencies remain responsible for legal, policy, and procurement review in their jurisdiction.
- Metrics should be interpreted with operational context, not as standalone success claims.
