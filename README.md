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

## Missing release assets

When a newer release lacks its configured asset, has an incomplete upload, or
publishes an empty asset, no macOS update job is created. The detector opens one
issue in your updater fork for that package and version. That issue remains the
durable suppression record even if manually closed.

When a newer release appears, the workflow comments on and closes the previous
open issue as superseded. It evaluates the new release independently. Failure
to record a blocked release fails detection rather than silently retrying it
every six hours.

## Generic verification

The macOS job uses only nixpkgs-native mechanisms:

1. Run the package's declared update script.
2. Require exactly one clean update commit changing only `package_file` to the
   detected version.
3. Build the package, including its normal check and install-check hooks.
4. Recursively discover and build every derivation exposed through the
   package's `passthru.tests`.

There are no Harper-, WhatCable-, or other package-specific verifier scripts.
Generated pull requests remain drafts and always require manual review.

## Optional nixpkgs-review-gha

To dispatch Darwin-only review runs after upstream nixpkgs CI passes:

1. Fork or configure `OWNER/nixpkgs-review-gha`.
2. Create a fine-grained token limited to that repository with **Actions: Read
   and write**, and save it as `NIXPKGS_REVIEW_GHA_TOKEN`.
3. Set `NIXPKGS_REVIEW_GHA_ENABLED=true`.
4. If needed, set `NIXPKGS_REVIEW_GHA_REPOSITORY` to another repository.
5. Run `preflight` again.

The review gate waits up to three hours and requires the complete upstream
check set to remain passed or skipped for three observations. It dispatches
only `x86_64-darwin` and `aarch64-darwin`, suppresses duplicate review runs,
and never marks a PR ready, merges it, or invokes the merge bot.

## Security and recovery

Pull-request CI is secret-free. The detector alone receives same-repository
Issues write permission. PATs are exposed only to the steps that require them,
and checkouts do not persist credentials. Review all workflow changes before
merging them into a token-bearing fork.

Disable package polling immediately by changing `UPDATER_ENABLED` to any value
other than `true`. Disable fork synchronization from **Actions > self-update**.
Open draft PRs and pushed nixpkgs branches remain available for inspection.
