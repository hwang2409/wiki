# reviewer verdict contract

At the end of each review, write the machine-readable verdict to
`/tmp/<TICKET>-review<n>-verdict.json`. The file must validate with:

```bash
./wiki graph lint /tmp/<TICKET>-review<n>-verdict.json --schema verdict
```

Use the review worker id and the exact commit SHA under review. Every finding
must use that same SHA in `source_sha`; this is the v1 verdict binding.

```json
{
  "worker": "WIKI-162-REVIEW1",
  "sha": "0123456789abcdef0123456789abcdef01234567",
  "state": "MERGE-READY",
  "findings": [],
  "summary": "reviewed the requested change and found no actionable issues",
  "created_at": "2026-07-22T18:00:00Z"
}
```

For a non-clean verdict, set `state` to `NOT-MERGE-READY` or `NO-GO` and add
one complete finding object per issue. Finding ids use the form `F-` followed
by six lowercase letters or digits. Include `file` and `line` when applicable,
and keep each finding actionable with `observed`, `why_wrong`, and
`do_instead`.
