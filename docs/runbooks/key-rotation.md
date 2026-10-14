# Runbook: key rotation

## Platform signing key (every 90 days, or immediately on suspicion)

The key signs sign-off attestations and audit events. Signatures already in
FHIR Provenance and audit chains stay verifiable with the retired public key,
so archive every retired public JWK with the audit evidence.

1. Generate `cwo-<yyyy-mm>.pem` (Ed25519) offline and store it in Key Vault as
   `clinical-workflow-orchestration-signing-key-cwo-<yyyy-mm>`.
2. Export the current public JWK (`GET /.well-known/jwks.json`) to the audit
   evidence archive.
3. Set `secrets.signingKeyFile: cwo-<yyyy-mm>.pem` and upgrade the release.
   Each replica's chain continues unbroken; new events carry the new kid, so
   `verify_chain` needs both public keys for the period spanning the rotation.
4. Verify `/.well-known/jwks.json` shows the new kid; delete the old secret
   version only after the archive is confirmed.

## SMART backend-services key (annually)

1. Generate a new RSA (3072) or EC P-384 key; register its public JWK with
   the EHR alongside the current key (EHRs accept a JWKS with several keys).
2. Store it in Key Vault as `clinical-workflow-orchestration-smart-key`, set
   `cwo.fhir.smartKeyId` to the new kid, upgrade.
3. After a successful token request with the new kid
   (`cwo_fhir_requests_total` healthy), remove the old key from the EHR.

## Azure OpenAI key

Prefer rotating with the account's secondary key: regenerate, update the Key
Vault secret, let External Secrets sync (≤ 1 h) and restart pods.
