# Contributing

## Setup

```bash
uv sync --all-extras
make check
```

## Requirements for every change

- `make check` passes: ruff, black, mypy `--strict`, bandit, tests with at
  least 90% branch coverage.
- New behaviour has tests. Security controls need adversarial cases in
  `tests/security`.
- Public API changes are recorded in `CHANGELOG.md`.
- Architecturally significant decisions get an ADR in
  `docs/architecture/adr/`.
- No credentials, customer data or PHI in code, tests or fixtures. Use
  synthetic data only (see `deployments/docker/fhir-seed/`).
- Published contracts are regenerated with
  `uv run python -m clinical_workflow_orchestration.contracts` and committed.

## Commit messages

Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
`chore:`). Breaking changes use `!` and a `BREAKING CHANGE:` footer.

## Review

Changes to `src/clinical_workflow_orchestration/security`, `policies/` or `deployments/` need approval
from a security code owner; changes to `src/clinical_workflow_orchestration/clinical/` (knowledge
tables and safety rules) need approval from a clinical code owner and must bump
`RULESET_VERSION` (see `CODEOWNERS`).
