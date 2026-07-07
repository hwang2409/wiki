---
type: til
tags: [phoebe, ci, aws, terraform]
created: 2026-07-07
updated: 2026-07-07
---

# CodeBuild Secrets Manager JSON key drift

**Symptom:** staging deploy `Run DB migrations (staging)` fails before
migrations run with exact CodeBuild log: `Phase context status code: Secrets
Manager Error Message: The json key does not exist in this secret`
(`actions/runs/28895344980`).
**Cause:** CodeBuild resolves all `type = "SECRETS_MANAGER"` project env vars
during `DOWNLOAD_SOURCE`; if Terraform adds an env var like
`APP_SERVICE_NO_RLS_PASSWORD = <role-password-secret>:app_service_no_rls::`
before the referenced secret version contains that JSON key, the build dies
while processing environment variables.
**Fix:** refresh/apply the source Secrets Manager JSON so it contains the
referenced key, or remove/avoid projecting unused secret env vars into the
migrate project. Confirm by rerunning staging deploy; later run
`28896281179` got past the failure and the migration job succeeded.
