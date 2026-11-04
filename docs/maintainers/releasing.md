---
type: guide
title: Release the Python package
description: Prepare a versioned Workflow Ledger source release and verify its GitHub Actions checks.
---

# Release the Python package

This guide covers source releases for the public Workflow Ledger repository.
Automated package-index publishing is not configured.

## Before a release

- Choose a version and confirm the target changes are on `main`.
- Confirm the GitHub Actions checks pass for the release commit.
- Review the [compatibility reference](../reference/compatibility.md) and
  update it if supported behavior or dependencies changed.
- Check the package metadata and generated distribution contents locally with
  `python -m build`.

## Publish a GitHub release

Create a GitHub Release for the selected version tag and include concise release
notes. The source archive is available from the GitHub release page. Do not
describe the package as installable from a package index until publishing to
that index has been configured and verified.

Package-index publication, signing, provenance attestations, and release
automation require their own setup and validation. Keep credentials out of
repository files, workflow definitions, and shell history if those features
are introduced.
