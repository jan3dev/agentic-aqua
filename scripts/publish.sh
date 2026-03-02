#!/bin/bash
set -e

echo "🚀 AQUA MCP Publishing Script"
echo "=============================="

# Check if UV_PUBLISH_TOKEN is set
if [ -z "$UV_PUBLISH_TOKEN" ]; then
    echo "❌ Error: UV_PUBLISH_TOKEN not set"
    echo "   Run: export UV_PUBLISH_TOKEN='pypi-YOUR_TOKEN_HERE'"
    exit 1
fi

# Get current version
VERSION=$(grep -E "^version = " pyproject.toml | sed 's/version = "\(.*\)"/\1/')
echo "📦 Current version: $VERSION"

# Confirm
read -p "Publish version $VERSION to PyPI? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "❌ Aborted"
    exit 1
fi

# Clean previous builds
echo "🧹 Cleaning previous builds..."
rm -rf dist/ build/ *.egg-info

# Run tests
echo "🧪 Running tests..."
uv run pytest tests/ || {
    echo "❌ Tests failed"
    exit 1
}

# Build
echo "🔨 Building package..."
uv build

# List built files
echo "📂 Built files:"
ls -lh dist/

# Publish
echo "📤 Publishing to PyPI..."
uv publish

echo "✅ Published successfully!"
echo ""
echo "Users can now install with:"
echo "  uvx aqua-mcp"
echo ""
echo "View on PyPI: https://pypi.org/project/aqua-mcp/$VERSION/"
