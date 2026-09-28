---
type: reference
tags: [phoebe, sms]
created: 2026-09-28
updated: 2026-09-28
---

# Caregiver SMS agent mode

Production: Henry approved a one-off SQL update on 2026-09-28. The primary changed 546 `app.organizations` rows. A primary read then showed 546 `off` and zero `on` or `shadow`.

- `features->>'caregiver_sms_agent_mode'` accepts `off`, `shadow`, or `on`; unset resolves to `off` in `libraries/python/organization_feature_settings/__init__.py`.
- `services/worker/handlers/conversations/caregiver_admission.py` reads the organization for each new caregiver SMS turn. `off` routes away from the caregiver agent. Already admitted runs can finish because delivery completion does not recheck the mode.
- Before [PR #18399](https://github.com/phoebe-health/phoebe/pull/18399) deploys, `default_new_organization_features()` still creates new organizations with `on`. The review-ready PR changes that default to `off` in both creation paths. Five real Twilio SMS cases passed on its commit; the one failing hosted model eval also failed 0/3 on the unchanged base commit.
- The one-off SQL changed only `features`. It bypassed the admin route's event and PostHog metadata sync; these are separate from runtime gating.
