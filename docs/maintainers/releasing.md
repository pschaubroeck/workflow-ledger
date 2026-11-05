---
type: guide
title: Build and release package artifacts
description: Build versioned Workflow Ledger wheel and source archives and attach them to a GitHub Release.
---

# Build and release package artifacts

This guide releases a Python wheel and source archive through GitHub Releases.
PyPI publishing is not configured. The `v0.3.0` release was published
manually; subsequent releases use the GitHub Actions workflow below.

## Before a release

- Choose a version and confirm the target changes are on `main`.
- Review the [compatibility reference](../reference/compatibility.md) and
  update it if supported behavior or dependencies changed.
- Confirm the project license and package metadata are correct.

## Run a release from GitHub Actions

Start the release from **Actions**, not from **Releases**:

1. Open the repository's **Actions** tab and select the **CI** workflow.
2. Click **Run workflow**.
3. Choose `main` under **Use workflow from**.
4. Enter a new version such as `v0.3.1` in the **Version** field.
5. Click **Run workflow**.

The workflow runs the Python test matrix, Ruff, and the installed-package
smoke checks first. It then validates the `vMAJOR.MINOR.PATCH` version, creates
the tag, builds the wheel and source archive, validates them with Twine, and
publishes the release with both package files attached. The tag and release
are created only after verification succeeds.

## Monitor the release

Follow the run in the repository's **Actions** tab. If a check or package
validation fails, no tag or release is created. Fix the issue on `main` and
start a new workflow run. If the version already exists, choose a new one.

Users can download the wheel from the published release and install it with
`python -m pip install /path/to/downloaded-wheel.whl`. Package-index
publication remains a separate decision.
