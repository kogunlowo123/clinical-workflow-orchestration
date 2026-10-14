# Runbook: clinical safety review (monthly)

Owner: clinical safety officer with pharmacy and care-management leads.

1. **Override rate.** From audit events `workflow.signoff.approve`, count
   acknowledged findings by check. A rising override rate for one check means
   the rule is miscalibrated or clinicians are rubber-stamping; review cases.
2. **Rejections and requested changes.** Sample rejected and returned plans;
   classify the cause (missing activity, wrong timing, unsafe medication
   proposal). Feed recurring causes into prompt or rule changes through code
   review.
3. **Model review usefulness.** Sample `model-review:*` warnings; if most are
   dismissed, tighten the review prompt.
4. **Knowledge tables.** Confirm `clinical/terminology.py` matches the local
   formulary and current guidance; changes go through CODEOWNERS review and
   bump `RULESET_VERSION`.
5. **Security violations.** Review every `security_violation` failure and its
   disposition.
6. Record the review outcome; it is evidence for NIST AI RMF MEASURE and
   MANAGE functions.
