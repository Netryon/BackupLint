# Central policy and GitOps (v0.8)

Operator guide for BackupLint central policy. This is **not** a claim that every
platform below has been revalidated for v1 — see the support matrix draft.

## Concepts

- **Policy snapshot** — immutable revision with content SHA-256
- **Assignment** — binds an agent (or group) to a revision; bumps assignment generation
- **Desired state** — what the agent should apply (`/v1/policy/desired`)
- **Applied state** — what the agent reported (`/v1/policy/applied`)
- **Drift** — mismatch between desired and applied (or pending apply)
- **Rollout** — staged assignment with batching, pause/abort, failure threshold
- **GitOps files** — YAML/JSON policy documents validated offline, then imported

## Local GitOps file utilities

```bash
backuplint policy validate path/to/policy.yaml
backuplint policy diff old.yaml new.yaml
backuplint policy export --help    # export helpers
backuplint policy import --help    # import helpers
```

Always validate before import. Imports create new immutable revisions; they do
not silently mutate history.

## Controller policy administration

With a controller data-dir:

```bash
backuplint controller policy --help
```

Typical operator flow:

1. Create or import a policy revision
2. Assign to an agent or group
3. Agents poll desired policy and apply locally
4. Agents acknowledge applied revision/generation/hash
5. Watch drift / rollout status in CLI or dashboard Policy views

## Rollout / rollback / drift

- Prefer staged rollout for large fleets; set batch size and failure threshold
- Pause or abort if apply failures spike
- Rollback by assigning a previous known-good revision (new assignment generation)
- Drift `IN_SYNC` means desired and applied generation/revision/hash agree
- `policy_audit` records operator actions and is **not** removed by operational retention prune

## Agent behavior

```bash
# Agents pull + apply + ack (also done inside long-running agent loops)
# See fleet.md for enrollment and mTLS prerequisites.
```

Agents must use the same controller CA. Protocol v1 and v2 agents are accepted
during mixed rolling upgrades; outside that window enrollment/submit fails closed.

## Retention interaction

```bash
backuplint controller prune-history --help
```

Default operational history retention is conservative (90 days). Latest
per-agent audit/submission rows are kept. **Never** deletes `policy_audit`.

## Related docs

- [fleet.md](fleet.md) — controller/agent enrollment
- [dashboard.md](dashboard.md) — policy/drift UI
- [controller-recovery.md](controller-recovery.md) — backup/restore including policy state
- [security.md](security.md) — trust boundaries
