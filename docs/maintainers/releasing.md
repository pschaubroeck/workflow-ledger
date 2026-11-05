---
type: guide
title: Build and release package artifacts
description: Build versioned Workflow Ledger wheel and source archives and attach them to a GitHub Release.
---

# Build and release package artifacts

This guide builds a Python wheel and source archive for download from GitHub
Releases. PyPI publishing is not configured.

## Before a release

- Choose a version and confirm the target changes are on `main`.
- Confirm the GitHub Actions checks pass for the release commit.
- Review the [compatibility reference](../reference/compatibility.md) and
  update it if supported behavior or dependencies changed.
- Confirm the project license and package metadata are correct.

## Build the artifacts

The package version comes from the Git tag. For a `0.3.0` release, create and
push the `v0.3.0` tag on the verified commit, then build from that tagged tree:

```text
git switch main
git pull --ff-only
git tag v0.3.0
git push origin v0.3.0
git switch --detach v0.3.0
python3 -m pip install build
python3 -m build
```

The build creates a `.whl` and `.tar.gz` file in `dist/`. Review the filenames
and verify the license is included in the source archive and package metadata.

## Publish a GitHub release

Create a GitHub Release for the existing version tag, include concise release
notes, and attach both files from `dist/`. GitHub also provides source-code
archives for the tag. Users can download the wheel and install it with
`python -m pip install /path/to/downloaded-wheel.whl`.

The current GitHub Actions workflow checks builds but does not publish or attach
release artifacts. Package-index publication and automated release uploads are
outside this process.
