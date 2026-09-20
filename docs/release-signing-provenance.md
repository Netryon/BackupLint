# Signing and provenance (design only)

This document prepares future BackupLint release signing **without** creating or
requiring long-lived private keys in the workshop.

## Goals

- Sign release artifacts (wheel/sdist/checksums) for authenticity
- Sign controller container images
- Attach build provenance/attestation to release candidates

## Preferred direction

Use short-lived, identity-based signing via **Sigstore** / **cosign** (keyless OIDC)
when the project is ready for public or customer-facing distribution:

```text
builder identity (CI OIDC) -> fulcio certificate -> cosign signature
provenance via SLSA / in-toto attestation referencing source commit + digests
```

Benefits:

- No long-lived release private key to store in the workshop
- Signatures bind to CI identity and source revision
- Fits container and blob signing with the same tooling family

## Artifact hooks (future)

1. After `meta/checksums.json` / `SHA256SUMS` are produced, generate a detached
   signature or Sigstore bundle over `SHA256SUMS`.
2. After the controller image is built, `cosign sign` the local image digest
   (still without pushing unless owner-approved).
3. Record signature/attestation paths in `release-evidence.json`.

## Explicit non-goals for this task

- Do not generate workshop PGP/SSH signing keys
- Do not publish signatures or attestations
- Do not push images to a registry for signing workflows
- Do not claim signed releases until an owner-approved pipeline exists

## Interim verification (today)

Consumers of a local RC can verify:

```bash
sha256sum -c dist-release/meta/SHA256SUMS
```

and confirm `source_commit` in `meta/build-manifest.json` matches the Git SHA used
to produce the candidate.
