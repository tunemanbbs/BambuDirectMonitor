# SignPath Automation

This repo is prepared for automated Windows code signing through SignPath and
GitHub Actions.

## What Is Already In The Repo

- `.github/workflows/build-sign-release.yml` builds the Windows EXE, submits it
  to SignPath, downloads the signed EXE, packages it, and publishes a GitHub
  Release when a `v*` tag is pushed.
- `.signpath/artifact-configurations/windows-release.xml` signs
  `BambuDirectMonitor.exe` with Authenticode.
- `sign-release.ps1` is an optional local signing helper for a future `.pfx`
  certificate. It is not used by the SignPath workflow.

## SignPath Setup

1. Apply to SignPath Foundation:

   ```text
   https://signpath.org/
   ```

2. Use this public GitHub repository in the application:

   ```text
   https://github.com/tunemanbbs/BambuDirectMonitor
   ```

3. In SignPath, create or request a project for this repository.

   Recommended values:

   ```text
   Project slug: BambuDirectMonitor
   Signing policy slug: release-signing
   Artifact configuration slug: windows-release
   ```

4. Add the artifact configuration XML from:

   ```text
   .signpath/artifact-configurations/windows-release.xml
   ```

5. Create a SignPath API token for a user that can submit signing requests for
   the project and signing policy.

## GitHub Setup

Add this repository secret:

```text
SIGNPATH_API_TOKEN
```

Add these repository variables:

```text
SIGNPATH_ORGANIZATION_ID
SIGNPATH_PROJECT_SLUG
SIGNPATH_SIGNING_POLICY_SLUG
SIGNPATH_ARTIFACT_CONFIGURATION_SLUG
```

For the recommended SignPath values above:

```text
SIGNPATH_PROJECT_SLUG=BambuDirectMonitor
SIGNPATH_SIGNING_POLICY_SLUG=release-signing
SIGNPATH_ARTIFACT_CONFIGURATION_SLUG=windows-release
```

`SIGNPATH_ORGANIZATION_ID` comes from the SignPath organization page.

## Publishing A Signed Release

After SignPath approves the project and the GitHub secret/variables are set:

```powershell
git tag v0.1.1
git push origin v0.1.1
```

The GitHub workflow will build, sign, package, and publish the release.

You can also run the workflow manually from the GitHub Actions tab to produce a
signed workflow artifact without publishing a release.
