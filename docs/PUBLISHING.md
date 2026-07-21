# Publishing Guide

## Automated releases (CI)

`.github/workflows/workflow.yml` handles publishing automatically:

| Trigger | What happens | Where |
|---------|--------------|-------|
| Merge/push to `develop` | `scripts/bump_beta.py` bumps the beta segment (`X.Y.ZbN` → `X.Y.Zb(N+1)`), commits it back to `develop` as `github-actions[bot]`, then builds & uploads | **TestPyPI** |
| GitHub Release *published* | Builds & uploads the version as-is (no bump) | **PyPI** (via Trusted Publishing / OIDC) |

Pushing to `main` does **not** trigger this workflow — `main` neither bumps nor
publishes. TestPyPI is fed only by `develop` betas; PyPI only by a published
GitHub Release.

So **you no longer edit the beta number by hand** — every merge to `develop`
gets its own `bN` on TestPyPI. The auto-bump only touches the `bN` counter;
advancing the *base* release (e.g. `0.5.1` → `0.6.0`) stays a deliberate manual
edit of `src/aqua/__init__.py`.

### Cutting a final (non-beta) release

`develop` is the beta channel: **any** push there gets `bN` appended
(`0.5.1` → `0.5.1b1`), so you cannot land a clean version on `develop`. Cut the
real release from `main`, where this workflow does not run:

1. Merge `develop` → `main` (main will carry the last beta, e.g. `0.5.1b7`).
2. On `main`, edit `src/aqua/__init__.py` to the clean version (`0.5.1`) and commit.
3. Create a **GitHub Release** (tag on `main`). The release job builds the tagged
   commit as-is and publishes the clean `0.5.1` to **PyPI**.

> ⚠️ Skip step 2 and the Release publishes a **beta string to PyPI** — the build
> uses whatever `__init__.py` holds at the tagged commit.

> **Note — `develop` branch protection.** `develop` requires the `Run tests`
> status check. The auto-bump push uses the default `GITHUB_TOKEN` (no PAT). If
> that push is ever rejected by the required check, fix it in `develop`'s
> protection settings — let GitHub Actions bypass the rule (Settings →
> Rules/branch protection) — rather than adding a personal token. The bump commit
> carries `[skip ci]` so it never re-triggers the workflow.

The manual steps below remain valid for one-off local publishes.

## Prerequisites

 **Create API Token**
   - You can find Pypi credentials at Bitwarden
   - Go to https://pypi.org/manage/account/token/
   - Create a new API token with scope: "Entire account"
   - Save the token (starts with `pypi-`)

 **Configure uv with your token**
   ```bash
   # Set PyPI token
   export UV_PUBLISH_TOKEN="pypi-YOUR_TOKEN_HERE"

   # Or create ~/.pypirc
   cat > ~/.pypirc << EOF
   [pypi]
   username = __token__
   password = pypi-YOUR_TOKEN_HERE
   EOF
   ```

## Publishing Steps

### 1. Update Version

Edit `pyproject.toml` and `src/aqua/__init__.py`:
```python
__version__ = "0.1.1"  # Increment version
```

### 2. Build the Package

```bash
# Clean previous builds
rm -rf dist/

# Build
uv build
```

This creates:
- `dist/agentic_aqua-0.1.1-py3-none-any.whl`
- `dist/agentic_aqua-0.1.1.tar.gz`

### 3. Test Locally (Optional)

```bash
# Install from local build
uv pip install dist/agentic_aqua-0.1.1-py3-none-any.whl

# Test the command
aqua --help
```

### 4. Publish to PyPI

```bash
# Publish
uv publish

# Or with explicit token
uv publish --token pypi-YOUR_TOKEN_HERE
```

### 5. Verify Installation

```bash
# Test with uvx
uvx agentic-aqua
```

## Quick Publish Script

For convenience, use the provided script:

```bash
./scripts/publish.sh
```

## Version Numbering

Follow semantic versioning:
- `0.1.0` - Initial release
- `0.1.1` - Bug fixes
- `0.2.0` - New features (backwards compatible)
- `1.0.0` - Stable release

## After Publishing

Users can install with:

```bash
# With uvx (recommended)
uvx agentic-aqua

# With pip
pip install agentic-aqua

# With uv
uv pip install agentic-aqua
```

## Troubleshooting

### "Package already exists"
- You need to increment the version number
- PyPI doesn't allow re-uploading the same version

### "Invalid credentials"
- Check your token is correct
- Make sure token starts with `pypi-`
- Verify token has "upload" permission
