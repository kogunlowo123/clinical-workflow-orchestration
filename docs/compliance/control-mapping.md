# Compliance control mapping

The evidence column names the artifact an auditor can inspect. Mappings show
how the service supports each control; they do not by themselves make a
deployment compliant.

| Control objective | HIPAA (45 CFR 164) | HITRUST CSF v11 | SOC 2 (TSC 2017) | ISO/IEC 27001:2022 | NIST SP 800-53 r5 | NIST AI RMF 1.0 | Evidence |
|---|---|---|---|---|---|---|---|
| Unique identification of people and agents | 164.312(a)(2)(i) | 01.q | CC6.1 | 5.16 | IA-2, IA-9 | GOVERN 1.4 | OIDC subject + `fhirUser`; SPIFFE agent ids; audit `actor` |
| Access control and least privilege | 164.308(a)(4), 164.312(a)(1) | 01.c, 01.v | CC6.3 | 5.15, 8.2 | AC-3, AC-6 | MANAGE 2.4 | `policies/clinical-workflow.yaml`, policy decisions with digest |
| Minimum necessary use of PHI | 164.502(b), 164.514(d) | Privacy Practices (category 13) | C1.1 | 5.34 | AC-21, PT-2 | MAP 4.1 | De-identified model view; minimum-necessary agent summaries; purpose of use required |
| Human oversight of automated output | — | — | PI1.3 | 5.37 | SA-8, CA-7 | MANAGE 1.3, GOVERN 3.2 | Mandatory signed sign-off (ADR-0001); acknowledgements with reasons |
| Integrity of clinical records | 164.312(c)(1), 164.312(c)(2) | 09.ab | PI1.4 | 8.15 | SI-7, AU-10 | MEASURE 2.6 | JWS sign-off, FHIR Provenance with verifier and signature, digests of sources |
| Audit controls | 164.312(b) | 09.aa, 09.ac | CC7.2 | 8.15 | AU-2, AU-3, AU-9, AU-10 | MEASURE 2.8 | Signed hash-chained audit in immutable storage, `verify_chain` |
| Transmission security | 164.312(e)(1) | 09.m | CC6.7 | 8.24 | SC-8, SC-13 | — | TLS to EHR, models, database (require_secure_transport) |
| Encryption at rest and key management | 164.312(a)(2)(iv) | 10.g | CC6.1 | 8.24 | SC-12, SC-28 | — | Key Vault premium, KMS/CMEK, keys outside Terraform state |
| Authentication of services | 164.312(d) | 01.j | CC6.1 | 8.5 | IA-3, IA-5 | — | SMART backend services (signed JWT), workload identity, fabric credentials |
| Validation of AI output | — | 10.b, 10.e | PI1.2 | 8.29 | SI-10, SA-11 | MEASURE 2.3, MEASURE 2.5 | Schema validation, deterministic rules, `ruleset_version`, test suites |
| AI risk management and documentation | — | 03.b | CC3.2 | 6.1.2 | RA-3, PM-9 | GOVERN 1.1, MAP 1.1, MAP 5.1 | Threat model, risk register, agent specification, ADRs |
| Third-party model risk | 164.308(b)(1) (BAA) | 05.k | CC9.2 | 5.19, 5.21 | SA-9, SR-6 | GOVERN 6.1 | Provider config limited to BAA/in-tenant providers at RESTRICTED |
| Change management | 164.308(a)(8) | 09.b | CC8.1 | 8.32 | CM-3, CM-5 | GOVERN 1.4 | Git history, CODEOWNERS on policies and clinical rules, CI gates |
| Incident response | 164.308(a)(6) | 11.a | CC7.3, CC7.4 | 5.24–5.26 | IR-4, IR-6 | MANAGE 4.1, MANAGE 4.3 | Alert rules, incident runbook |
| Contingency and availability | 164.308(a)(7) | 12.c | A1.2, A1.3 | 5.30, 8.14 | CP-9, CP-10 | — | Zone-redundant DB, PITR, DR runbook, resumable workflows |
| Retention of documentation | 164.316(b)(2) | 06.c | — | 5.33 | AU-11 | — | Immutable audit storage, 7-year retention default |

## FDA clinical decision support

The care plan is a recommendation presented to a clinician who sees the basis
for it (the evidence, the rule findings and the model rationale) and makes the
decision independently. The design follows the criteria in the FDA guidance
"Clinical Decision Support Software" (September 2022) for transparency of
basis and independent review; whether a given deployment is a device is a
regulatory determination made by the deploying organization.

## 21 CFR Part 11 (where records support regulated activities)

| Requirement | Support |
|---|---|
| 11.10(e) audit trails | Time-stamped, signed, hash-chained audit events |
| 11.50 signature manifestations | Provenance signature carries signer, time and meaning (Verification Signature) |
| 11.70 signature/record linking | JWS binds the signer to the draft digest; Provenance targets the written resources |
| 11.10(d) limiting system access | OIDC roles and policy |
