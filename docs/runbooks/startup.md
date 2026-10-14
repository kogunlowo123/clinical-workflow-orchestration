# Runbook: first deployment and startup (Azure reference)

## Prerequisites

- AKS ≥ 1.27 with OIDC issuer and Workload Identity enabled, External Secrets
  Operator, and a `ClusterSecretStore` named `platform-secrets` bound to the
  Key Vault Terraform creates.
- Entra ID app registration for the API exposing app roles
  `care-coordinator`, `clinician-approver`, `clinical-auditor`, and the
  optional claim `fhirUser` mapped to each clinician's Practitioner reference.
- The agent identity fabric deployed, with the orchestrator registered:
  scopes `fhir:write:careplan-bundle`, risk HIGH, max classification
  RESTRICTED, workload binding to
  `system:serviceaccount:clinical-workflow:clinical-workflow-orchestration`.
- An EHR SMART backend-services client registered with the scopes in
  `fhir/smart.py` (`DEFAULT_SCOPES`).
- A vLLM deployment in the cluster serving the fallback model.

## 1. Provision cloud resources

```bash
cd deployments/terraform/azure
terraform init            # configure an encrypted remote backend first
terraform apply -var-file=<environment>.tfvars
```

Record the outputs: workload identity client id, Key Vault URI, Azure OpenAI
endpoint and deployment, audit container.

## 2. Create keys outside Terraform

```bash
# Platform signing key (Ed25519): sign-off attestations and the audit chain
openssl genpkey -algorithm ed25519 -out cwo-2026-10.pem
az keyvault secret set --vault-name <vault> \
  --name clinical-workflow-orchestration-signing-key-cwo-2026-10 --file cwo-2026-10.pem

# SMART backend-services key (RS384); register the public JWK with the EHR
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out smart.pem
az keyvault secret set --vault-name <vault> \
  --name clinical-workflow-orchestration-smart-key --file smart.pem
shred -u cwo-2026-10.pem smart.pem
```

## 3. Namespace and secret sync

```bash
kubectl apply -k deployments/kubernetes
kubectl -n clinical-workflow get externalsecret   # all READY=True
```

## 4. Install the chart

Create `<environment>.yaml` with every value marked REQUIRED in
`deployments/helm/clinical-workflow-orchestration/values.yaml` and the
workload identity annotation:

```bash
helm upgrade --install cwo oci://ghcr.io/kogunlowo123/charts/clinical-workflow-orchestration \
  --version 0.1.0 -n clinical-workflow -f <environment>.yaml --wait
```

## 5. Verify

```bash
kubectl -n clinical-workflow rollout status statefulset/cwo-clinical-workflow-orchestration
kubectl -n clinical-workflow port-forward svc/cwo-clinical-workflow-orchestration 8080:80
curl -sf localhost:8080/readyz
curl -s localhost:8080/.well-known/agent-card.json | jq '.skills[].id'
curl -s localhost:8080/.well-known/jwks.json | jq '.keys[].kid'
```

Expected: `{"status":"ready"}`, four skills, the active signing kid. Then run a
synthetic end-to-end check against a non-production FHIR endpoint seeded with
`deployments/docker/fhir-seed/synthetic-heart-failure-discharge.json`.

## Local stack

```bash
docker compose up --build      # PostgreSQL, HAPI FHIR (seeded), Ollama, OIDC, OTel
```

The compose file uses an in-memory signing key and the local model; it is for
workstations only.
