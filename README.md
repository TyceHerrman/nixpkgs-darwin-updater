# nixpkgs-darwin-updater

Fork this repository to poll a small, reviewed list of Darwin-only nixpkgs
packages for stable GitHub releases. An actionable release is updated and built
on a macOS GitHub runner before the workflow pushes a branch to your nixpkgs
fork and opens a draft pull request against `NixOS/nixpkgs` under your account.

Nothing is copied into your nixpkgs fork. The schedule, package manifest,
blocked-release notifications, and optional `nixpkgs-review-gha` integration
all live in your fork of this repository.

## Setup

1. Fork this repository and enable GitHub Actions and Issues in the fork.
2. Ensure `OWNER/nixpkgs` is your fork of `NixOS/nixpkgs`. If it has another
   name or owner, set the repository variable `NIXPKGS_FORK_REPOSITORY` to its
   `OWNER/REPOSITORY` name.
3. Create a classic personal access token with the `public_repo` scope. Add it
   to this updater fork as the Actions secret `NIXPKGS_PR_TOKEN`. The token is
   needed because the workflow pushes to a different repository and opens the
   upstream PR as you.
4. Edit [`updater/packages.json`](updater/packages.json) to contain only the
   packages you maintain and want this fork to check.
5. Run **Actions > Darwin nixpkgs updater > Run workflow**, choose `preflight`,
   and confirm the read-only access check passes.
6. Run the workflow again with `full`. A run that finds no update validates
   release detection, but not branch push or PR creation.
7. Set the repository variable `UPDATER_ENABLED` to `true` to activate the
   six-hour schedule. Manual `preflight` and `full` runs work without it.

The workflow runs at minute 17 every six hours. GitHub executes scheduled
workflows only from the repository's default branch and may disable schedules
in inactive public forks.

## Package manifest

Every manifest entry has these fields:

- `attr`: the nixpkgs attribute updated by its declared `passthru.updateScript`.
- `package_file`: the single `pkgs/.../*.nix` file the update may change.
- `upstream`: the `OWNER/REPOSITORY` GitHub project providing releases.
- `maintainer`: the nixpkgs maintainer handle expected in the package source.
- `platform_pattern`: a regular expression that must match its Darwin platform
  declaration.
- `required_asset`: an optional exact asset template. Use `{version}` once to
  interpolate the release version, or use `null` when no asset gate is needed.

Only stable numeric tags such as `v2.1.0` or `2.1.0` are accepted. Adding
another nixpkgs maintainership does not automatically add the package here.

## Missing release assets

When a newer release lacks its configured asset, has an incomplete upload, or
publishes an empty asset, no macOS update job is created. The detector opens one
issue in your updater fork for that package and version. That issue is the
notification and the durable suppression record, even if you close it manually.

When a newer release appears, the workflow comments on and closes the previous
open issue as superseded. It then evaluates the new release, opening a new issue
if that release is also blocked. Failure to record the issue fails detection
rather than silently retrying the package every six hours.

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
   and write**, and save it here as `NIXPKGS_REVIEW_GHA_TOKEN`.
3. Set `NIXPKGS_REVIEW_GHA_ENABLED` to `true`.
4. If needed, set `NIXPKGS_REVIEW_GHA_REPOSITORY` to another
   `OWNER/REPOSITORY`.
5. Run `preflight` again.

The review gate waits up to three hours and requires the complete upstream
check set to remain passed or skipped for three observations. It dispatches
only `x86_64-darwin` and `aarch64-darwin`, suppresses duplicate review runs,
and never marks a PR ready, merges it, or invokes the merge bot.

## Security and recovery

Pull-request CI is secret-free. The detector alone receives same-repository
Issues write permission. The classic and fine-grained tokens are exposed only
to the steps that require them, and checkouts do not persist credentials.

Review action updates before merging them into your default branch. Disable the
schedule immediately by setting `UPDATER_ENABLED` to any value other than
`true`; open draft PRs and pushed nixpkgs branches are left intact for manual
inspection.
