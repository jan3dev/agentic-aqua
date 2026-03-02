# Publishing Guide

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

Edit `pyproject.toml` and `src/aqua_mcp/__init__.py`:
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
- `dist/aqua_mcp-0.1.1-py3-none-any.whl`
- `dist/aqua_mcp-0.1.1.tar.gz`

### 3. Test Locally (Optional)

```bash
# Install from local build
uv pip install dist/aqua_mcp-0.1.1-py3-none-any.whl

# Test the command
aqua-mcp --help
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
uvx aqua-mcp
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
uvx aqua-mcp

# With pip
pip install aqua-mcp

# With uv
uv pip install aqua-mcp
```

## Troubleshooting

### "Package already exists"
- You need to increment the version number
- PyPI doesn't allow re-uploading the same version

### "Invalid credentials"
- Check your token is correct
- Make sure token starts with `pypi-`
- Verify token has "upload" permission
