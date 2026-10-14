# ADR-0001: Clinician sign-off is a hard gate, enforced three times

- Status: Accepted
- Date: 2026-10-14

## Context

The workflow writes CarePlans and Tasks into the medical record. A drafted
plan, however well validated, is a proposal; accountability for what reaches
the record belongs to a clinician. A single check in one place (a UI button,
one policy rule) can be bypassed by a bug, a misconfigured policy or a
replayed request.

## Decision

1. **State machine.** `committing` is reachable only from `awaiting_approval`
   through a clinician decision; the engine never advances past
   `awaiting_approval` on its own, even if policy were to return `allow` for
   an unsigned write.
2. **Signature.** Approval produces an Ed25519 JWS over the workflow id, the
   draft digest, the practitioner and the acknowledged findings. The commit
   step verifies the signature and that the draft being written has exactly the
   signed digest. The clinician must send the digest they reviewed, so a plan
   that changed underneath them cannot be approved.
3. **Policy.** `fhir:write:*` returns `require_approval` while unsigned and is
   denied while any error-level finding is unacknowledged; the decision is
   re-evaluated at commit time, so a policy change between approval and write
   takes effect.

The signature travels into the EHR inside FHIR Provenance, with the
practitioner as verifier, so the record itself shows who approved it.

## Consequences

- Three independent controls must all fail for an unsigned write; each has
  adversarial tests (`tests/security`).
- Clinicians must re-review after any redraft (the digest changes).
- The platform key becomes a high-value asset: it lives in Key Vault, is
  mounted read-only and rotated (`docs/runbooks/key-rotation.md`).
