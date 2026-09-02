# nixpkgs-darwin-updater

Poll a small, reviewed list of Darwin-only nixpkgs packages for stable GitHub
releases. An actionable release is updated and built on a macOS GitHub runner
before the workflow pushes a branch to your nixpkgs fork and opens a draft pull
request against `NixOS/nixpkgs` under your account.

Nothing is copied into your nixpkgs fork. The schedule, package manifest,
blocked-release notifications, and optional `nixpkgs-review-gha` integration
live in your fork of this repository.

## Fork owner setup

1. Fork this repository and enable GitHub Actions and Issues in the fork.
2. Edit [`updater/packages.json`](updater/packages.json) on your default branch
   so it contains only the packages you maintain and want checked. Keep other
   updater files unchanged so automatic rebases remain conflict-free.
3. Ensure `OWNER/nixpkgs` is your fork of `NixOS/nixpkgs`. If it has another
   name or owner, set `NIXPKGS_FORK_REPOSITORY` to its `OWNER/REPOSITORY` name.
4. Create a classic personal access token with only the `public_repo` scope.
   Save it as the Actions secret `NIXPKGS_PR_TOKEN`. It pushes to your nixpkgs
   fork and opens upstream draft PRs as you.
5. Run **Actions > Darwin nixpkgs updater > Run workflow**, choose `preflight`,
   and confirm the read-only access and manifest checks pass.
6. Run the workflow again with `full`. A run that finds no update validates
   detection, but not branch push or PR creation.
7. Set `UPDATER_ENABLED=true` to activate the six-hour schedule.

The updater runs at minute 17 every six hours. Manual `preflight` and `full`
runs work while `UPDATER_ENABLED` is unset. GitHub may disable scheduled
workflows in inactive public forks.

## Package manifest

Canonical `main` deliberately contains an empty manifest. Replace it in your
fork with a non-empty JSON array such as:

```json
[
  {
    "attr": "example-app",
    "package_file": "pkgs/by-name/ex/example-app/package.nix",
    "upstream": "example/example-app",
    "maintainer": "example",
    "platform_pattern": "platforms\\s*=\\s*lib\\.platforms\\.darwin\\s*;",
    "required_asset": "Example_{version}_universal.dmg"
  }
]
```

Every entry has these fields:

- `attr`: the nixpkgs attribute updated by its declared `passthru.updateScript`.
- `package_file`: the single `pkgs/.../*.nix` file the update may change.
- `upstream`: the `OWNER/REPOSITORY` GitHub project providing releases.
- `maintainer`: the nixpkgs maintainer handle expected in the package source.
- `platform_pattern`: a regular expression matching its Darwin platform
  declaration.
- `required_asset`: an optional exact asset template. Use `{version}` once to
  interpolate the release version, or use `null` when no asset gate is needed.

Only stable numeric tags such as `v2.1.0` or `2.1.0` are accepted. Missing,
empty, malformed, or unsafe manifests fail before a macOS job starts.

### Same-repository manifest branch

The optional variable `UPDATER_MANIFEST_REF` selects another branch, tag, or
commit in the same updater repository. The workflow resolves that ref to an
exact commit and fetches only `updater/packages.json`; it never checks out or
executes code from the selected ref. When the variable is unset, the checked-out
default-branch manifest is used.

This mode lets the canonical maintainer run the public repository directly
while keeping canonical `main` generic. Ordinary fork owners should edit their
default-branch manifest instead.

## Optional automatic self-update

The `self-update` workflow follows the `nixpkgs-review-gha` fork model. At 03:43
UTC daily, or when manually dispatched, it rebases your default branch onto the
canonical parent, validates your manifest, runs the complete test suite, and
pushes with `--force-with-lease`.

To enable it in a downstream fork:

1. Create a fine-grained token limited to this updater fork with **Contents:
   Read and write** and **Workflows: Read and write**.
2. Save it as the Actions secret `GH_SELF_UPDATE_TOKEN`.
3. Open **Actions > self-update** and enable the workflow.

The token is provided only to the final credential-and-push step, which runs on
a fresh runner. Newly rebased code is tested on a separate runner with token and
runner-command-file variables removed, then transferred as a Git bundle. A
conflict, invalid manifest, failing test, or lease race leaves the remote fork
untouched. If the token is missing, the workflow fails and disables itself.

If a rebase conflicts, resolve it locally while retaining your manifest:

```console
git remote add upstream https://github.com/TyceHerrman/nixpkgs-darwin-updater.git
git fetch upstream main
git rebase upstream/main
python3 updater/manifest_source.py --repository OWNER/nixpkgs-darwin-updater \
  --output-manifest /private/tmp/codex-updater-packages.json
python3 -m unittest discover -s updater -p 'test_*.py' -v
git push --force-with-lease origin main
```

## Canonical maintainer installation

`TyceHerrman/nixpkgs-darwin-updater` uses canonical `main` as its trusted code
and workflow source. Its live package list is stored in the public, data-only
`instance/tyce` branch at `updater/packages.json`, selected with:

```text
UPDATER_MANIFEST_REF=instance/tyce
```

The canonical repository keeps `self-update` disabled and does not define
`GH_SELF_UPDATE_TOKEN`, because it is the parent rather than a fork. After any
manifest-branch edit, run `preflight` before the next `full` or scheduled run.

### Contributing to canonical main

Changes to `TyceHerrman/nixpkgs-darwin-updater` on `main` go through pull
requests. The GitHub Actions `test` check compiles the Python sources and runs
the unit and Nix expression tests; it must pass before merging. Use squash or
rebase merging. An approving review is not required, but review conversations
must be resolved. Downstream forks choose their own branch policies.

## Known-release availability and missing assets

When a newer release lacks its configured asset, has an incomplete upload, or
publishes an empty asset, no macOS update job is created. The detector opens one
issue in your updater fork for that package and version. On later runs, the
detector rechecks the release assets while reusing that issue, even if it was
manually closed. It does not create duplicate issues or start a macOS job while
the asset remains unavailable.

The detector also discovers versions named by its open update PRs and existing
blocked-release issues. It checks each exact GitHub release before reporting a
package as having no newer release. This protects against an upstream project
deleting, drafting, or otherwise making a release unavailable after an update PR
has already been opened—even when GitHub's `latest` endpoint falls back to an
older version. A surviving Git tag alone is never treated as an available
release because it does not provide the release assets.

An unavailable known release is recorded in the same package/version issue,
including the affected PR when there is one. Closed issues continue to suppress
duplicate notifications. The detector rechecks on every run, reopens a recovered
issue if the release disappears again, and does not recreate an existing PR.
Authentication, rate-limit, network, malformed-response, and GitHub server
failures fail detection; they are not mislabeled as withdrawn releases.

If the asset later becomes ready for the same version, the workflow comments on
the blocked-release issue, closes it when still open, and resumes the normal
Darwin update. If an update PR already exists, it remains the sole PR. This
catches releases or assets restored after the initial GitHub release.

When a newer release appears, the workflow comments on and closes the previous
open issue as superseded and evaluates the new release independently. Failure
to create or update a blocked-release issue fails detection rather than losing
the notification or recovery record.

## Generic verification

The macOS job uses only nixpkgs-native mechanisms:

1. Run the package's declared update script.
2. Require exactly one clean update commit directly on the inspected upstream
   base, changing only `package_file` from the inspected to the detected version.
   Validate its subject and add its changelog reference when missing.
3. Build the package, including its normal check and install-check hooks.
4. Recursively discover and build every derivation exposed through the
   package's `passthru.tests`.

There are no Harper-, WhatCable-, or other package-specific verifier scripts.
Generated pull requests remain drafts and always require manual review.

The updater enforces the mechanical parts of the nixpkgs
[commit conventions](https://github.com/NixOS/nixpkgs/blob/master/pkgs/README.md#commit-conventions):
the subject must be exactly `attribute: old-version -> new-version` (no trailing
period), with a blank line before any body. It preserves the update script's
explanation and existing trailers, adds `Changelog: <upstream release URL>` if
missing, and checks the commit again after verification and before the push
step. Preparation changes
only commit metadata, preserving the package tree, parent, and original author.
Any mismatch fails before publication; an invalid subject is not silently
rewritten.

The nixpkgs
[automation policy](https://github.com/NixOS/nixpkgs/blob/master/CONTRIBUTING.md#automationai-policy)
exempts maintainer-approved bots that run update scripts. This updater is
intended for that use: maintainers configure their packages and it runs the
declared update scripts. It does not use an LLM at runtime and does not add an
`Assisted-by:` trailer or an AI disclosure to deterministic updates. If you use
an AI tool to change a package or its commit/PR text manually,
add the actual tool and model/version to an `Assisted-by:` commit trailer and
disclose that assistance separately in the PR description. Existing trailers
are preserved. These checks do not replace human responsibility for correctness,
licensing, or the other nixpkgs contribution requirements.

Each PR body uses `.github/PULL_REQUEST_TEMPLATE.md` from the exact upstream
nixpkgs commit inspected by the detector—not a copied or abbreviated template.
The updater adds the version/release and verification details in its description
area and preserves the template's comments, headings, checklist wording, and
reference links. It checks `aarch64-darwin` after a successful build, and package
tests only when derivation-valued `passthru.tests` were actually built. Other
items, including `nixpkgs-review`, binary functionality, and policy attestations,
remain unchecked for human review. Missing verification data or an unrecognized
template layout stops the job before pushing a branch or opening a PR.

## Optional nixpkgs review

Set `NIXPKGS_REVIEW_GHA_ENABLED=true` to start Darwin review only after the
complete upstream nixpkgs check set has remained passed or skipped for three
observations. The updater never marks a PR ready, merges it, or invokes the
merge bot.

The shared controller is the recommended mode. Set
`NIXPKGS_REVIEW_DISPATCH_REPOSITORY` to the controller repository, usually
`OWNER/nixpkgs-contribution-workflows`, and create
`NIXPKGS_REVIEW_DISPATCH_TOKEN`: a fine-grained token limited to that
repository with **Actions: Read and write**. The gate sends the pull request
number, package attribute, `platform-scope=darwin`, and `force=false` to
`review.yml` on `main`. Its summary links the controller request; the actual
downstream build is owned and deduplicated by that controller.

Existing direct `nixpkgs-review-gha` operation remains available when
`NIXPKGS_REVIEW_DISPATCH_REPOSITORY` is unset. It uses
`NIXPKGS_REVIEW_GHA_TOKEN` with **Actions: Read and write**, and defaults to
`OWNER/nixpkgs-review-gha` unless `NIXPKGS_REVIEW_GHA_REPOSITORY` is set.
Direct mode dispatches only `x86_64-darwin` and `aarch64-darwin` and retains
its existing PR-number duplicate suppression.

Run `preflight` after selecting either mode. In controller mode only the
controller token is required for review integration; in direct mode only the
direct runner token is required. The updater validates that the selected
repository's `review.yml` workflow is active before the full run.

## Security and recovery

Pull-request CI is secret-free. The detector alone receives same-repository
Issues write permission. PATs are exposed only to the steps that require them,
and checkouts do not persist credentials. Review all workflow changes before
merging them into a token-bearing fork.

Disable package polling immediately by changing `UPDATER_ENABLED` to any value
other than `true`. Disable fork synchronization from **Actions > self-update**.
Open draft PRs and pushed nixpkgs branches remain available for inspection.
