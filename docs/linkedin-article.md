Discharge is where care plans fall apart: a follow-up never booked, a potassium nobody rechecks, an antibiotic that collides with a penicillin allergy.

The problem: coordinators rebuild plans from the chart by hand. Models draft fast, but an ungrounded draft written straight into the record is a liability.

Why it matters: the record is the source of truth. Whatever reaches it must be traceable to evidence and to an accountable clinician.

Our flow: Encounter → FHIR evidence (with provenance digests) → care-coordination agent → validation agent → policy gate → clinician sign-off → CarePlan, Tasks and signed Provenance in the EHR. Agents reach it over MCP and A2A.

How we built it:
1. A persisted state machine with leases, so every step resumes after a crash.
2. SMART backend services with signed client assertions for FHIR access.
3. Deterministic rules for allergies, duplicate therapy, follow-up timing and renal dosing; the model review can only warn.
4. Sign-off signs the draft digest; the commit re-verifies it.

Practices we hold to: de-identified model input, injection screening on chart text, default-deny policy, idempotent writes, a signed audit chain, and metrics for failures, overrides and spend.

Tradeoffs: two model calls add latency and cost. Clinicians must acknowledge every error, which adds clicks, and approval fatigue is a real risk we track. Portable code costs us some managed conveniences.

The value: coordinators review instead of assembling, and every plan carries its reasoning and its signer.

Looking ahead: as more agents propose changes to clinical records, provenance and human sign-off become the interface, not an afterthought.

Question for you: should clinicians acknowledge every safety finding, or only the ones a rule marks critical?

Code: https://github.com/kogunlowo123/clinical-workflow-orchestration

#AI #ArtificialIntelligence #GenerativeAI #AgenticAI #LLMOps #EnterpriseAI #AISecurity #RAG #MachineLearning #DataScience #CloudComputing #Kubernetes #MLOps #DigitalTransformation #Innovation #FHIR #HealthcareAI #ClinicalSafety #Azure
