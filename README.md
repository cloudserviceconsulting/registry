# Terraform Custom Provider Registry

Static registry served via GitHub Pages at `cloudserviceconsulting.github.io/terraform-registry`.

## Setup

1. Create the repo `cloudserviceconsulting/terraform-registry` on GitHub.
2. Push this directory's contents to the `main` branch.
3. Enable GitHub Pages in repo Settings > Pages:
   - Source: **Deploy from a branch**
   - Branch: `main`, folder: `/ (root)`
4. Wait for the Pages deployment to complete.

## Usage

In any Terraform configuration:

```hcl
terraform {
  required_providers {
    jira = {
      source  = "cloudserviceconsulting.github.io/terraform-registry/csc/jira"
      version = "0.1.0"
    }
  }
}
```

Then run `terraform init` — no `.terraformrc` needed.

## Adding a new release

After pushing a new tag (e.g. `v0.2.0`) to `terraform-provider-jira-assets`:

1. Wait for the GitHub Actions release workflow to complete.
2. Download the `terraform-provider-jira_<version>_SHA256SUMS` file from the release.
3. Create a new version directory: `v1/providers/csc/jira/<version>/download/<os>/<arch>`
4. Populate each platform file with the correct `download_url` and `shasum`.
5. Add the new version entry to `v1/providers/csc/jira/versions`.
6. Commit and push to this repo — GitHub Pages redeploys automatically.

## Adding a new provider

Create a new directory tree under `v1/providers/csc/<provider-name>/` following the same
structure as `jira/`. Add the provider to any Terraform config with:

```hcl
source = "cloudserviceconsulting.github.io/terraform-registry/csc/<provider-name>"
```
