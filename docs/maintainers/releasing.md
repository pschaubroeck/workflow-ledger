---
type: guide
title: Build and release package artifacts
description: Build versioned Workflow Ledger wheel and source archives and attach them to a GitHub Release.
---

# Build and release package artifacts

This guide releases a Python wheel and source archive through GitHub Releases.
PyPI publishing is not configured. The `v0.3.0` release was published
manually; subsequent releases use the automated workflow below.

## Before a release

- Choose a version and confirm the target changes are on `main`.
- Review the [compatibility reference](../reference/compatibility.md) and
  update it if supported behavior or dependencies changed.
- Confirm the project license and package metadata are correct.

## Create a draft release

Use GitHub's Releases page to create the next release and tag:

1. Choose **Draft a new release**.
2. Enter the version tag, such as `v0.3.1`, and choose **Create new tag**.
3. Set the target to `main`, add release notes, and choose **Save draft**.

Creating the tag starts GitHub Actions. The workflow runs the test matrix,
Ruff, and the installed-package smoke checks. After those jobs pass, it builds
the wheel and source archive, validates them with Twine, attaches both files
to the draft, and publishes the release.

## Monitor the release

Follow the run in the repository's **Actions** tab. If a check fails, the
release remains a draft. Fix the issue and rerun the failed workflow from
Actions; publish only after the checks and artifact upload finish successfully.

Users can download the wheel from the published release and install it with
`python -m pip install /path/to/downloaded-wheel.whl`. Package-index
publication remains a separate decision.
