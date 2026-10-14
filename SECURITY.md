# Security Policy

## Supported versions

Security fixes are released for the latest minor version.

## Reporting a vulnerability

Do not open a public issue. Use GitHub private vulnerability reporting
(Security tab, "Report a vulnerability"). Include affected version, a
description of the issue, reproduction steps and impact. Never include real
patient data in a report; use the synthetic dataset in
`deployments/docker/fhir-seed/`.

Expected handling:

| Stage | Target |
|---|---|
| Acknowledgement | 3 business days |
| Triage and severity (CVSS v4) | 10 business days |
| Fix for critical/high | 30 days from triage |
| Coordinated disclosure | After a fixed release is available |

## Scope

In scope: code in this repository, including the sign-off and EHR write
path, clinical validation rules, agent authorization (A2A, MCP), model input
and output guarding, and the deployment artifacts. Issues that could cause an
unsigned or altered write to a patient record, PHI disclosure, or a bypass of
a deterministic safety rule are treated as critical.
