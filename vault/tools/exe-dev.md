---
type: reference
tags: [tools, cloud, agents]
created: 2026-07-06
updated: 2026-07-06
---

# exe.dev

External cloud/tool. Current as of 2026-07-06; verify pricing/security
before operational decisions.

- **What:** Bold Software's SSH-first cloud for persistent Linux VMs.
  VMs are reachable over SSH and HTTPS via `*.exe.xyz`, with the HTTP
  proxy private by default and shareable later. Sources:
  https://exe.dev/docs/what-is-exe, https://exe.dev/vps.
- **Use cases:** AI sandboxes, remote devboxes, small VPS/prototype
  hosting. Default `exeuntu` VMs include `claude`, `codex`, `pi`, and
  Shelley at `https://vmname.shelley.exe.xyz/`.
- **Agent angle:** KVM-isolated VMs with persistent disks, root/systemd,
  public TLS hostnames, copy-on-write `cp` clones, expiring
  command-scoped SSH-signed API tokens, and HTTP integrations that keep
  secrets in exe.dev's proxy rather than on the VM.
- **API shapes (captured live 2026-07-06):** `ssh exe.dev billing plan --json` → `{"plan":"Basic","plan_id":"basic:monthly:20260106","pooled_disk_gb":25,"default_disk_gb":25,"bandwidth_gb":100,"max_vms":0,"paid":false,"shelley_credits_max":20,...}`. `ssh exe.dev ls --json` → `{"vms":[]}`. Henry's free Basic tier: 25 GB pooled disk — likely too small for a phoebe-base box with warm Bazel caches; PHO-12930 clone test probably needs the $20 Personal tier (100 GB) first.
- **Pricing snapshot:** Personal $20/mo for 2 vCPU / 8 GB RAM pool,
  50 VMs, 100 GB pooled disk, 200 GB transfer. Team $25/user/mo. Usage
  pricing advertised at $0.05/core-hour CPU, $0.016/GiB-hour active
  memory, $0.08/GiB-month disk. Source: https://exe.dev/pricing.
- **Credibility:** Founders include David Crawshaw (Tailscale co-founder,
  CTO 2019-2024), Josh Bleecher Snyder (card.io/Canopy Climate co-founder,
  early Tailscale, Go runtime/compiler contributor), and Philip Zeyliger
  (Airtable/Cloudera/Google/D. E. Shaw). Announced $35M total funding /
  Series A on 2026-04-22 from Amplify, CRV, and HeavyBit.
- **Phoebe fit:** Promising for isolated agent sandboxes, quick hosted
  prototypes, and shareable preview boxes. Do a hands-on benchmark before
  using it for full Phoebe development: the local stack is heavy
  (Bazel, Postgres, Vite, Python services, workers), and the Personal
  tier may be tight.
- **Risk posture:** Treat as an external vendor. Do not put PHI,
  production data, or broad GitHub credentials in it until DPA/security
  posture, subprocessors, support access, retention, and compliance status
  are reviewed. Product is young and the API shape is intentionally unusual:
  `ssh exe.dev` plus `POST https://exe.dev/exec` with the SSH command in
  the body.
