#!/bin/bash
# Setup script for local development workspace
# This script initializes all dependencies for the family-assistant project

set -e

echo "🚀 Starting workspace setup..."

# Check if we're in the project root
if [ ! -f "pyproject.toml" ]; then
    echo "❌ Error: Must be run from the project root directory"
    echo "   (Directory containing pyproject.toml)"
    exit 1
fi

# Step 1: Create virtual environment
if [ ! -d ".venv" ] || [ ! -f ".venv/bin/python" ]; then
    echo "📦 Creating fresh virtual environment..."
    rm -rf .venv
    uv venv .venv
else
    echo "✓ Virtual environment already exists"
    # Verify the venv works
    if ! .venv/bin/python --version >/dev/null 2>&1; then
        echo "⚠️  Existing virtual environment is broken, recreating..."
        rm -rf .venv
        uv venv .venv
    fi
fi

# Step 2: Activate virtual environment
echo "🔧 Activating virtual environment..."
source .venv/bin/activate

# Step 3: Install Python dependencies
echo "📥 Installing Python dependencies..."
uv sync --extra dev --extra pgserver

# Install additional dev tools that might not be in pyproject.toml
echo "📥 Installing additional development tools..."
uv pip install poethepoet pytest-xdist pre-commit

# Step 4: Install pre-commit hooks
if [ -f ".pre-commit-config.yaml" ]; then
    echo "🪝 Installing pre-commit hooks..."
    .venv/bin/pre-commit install || {
        echo "⚠️  Warning: Failed to install pre-commit hooks"
        echo "   You may need to run: .venv/bin/pre-commit install manually"
    }
else
    echo "⚠️  No .pre-commit-config.yaml found, skipping pre-commit setup"
fi

# Step 5: Install frontend dependencies
if [ -f "frontend/package.json" ]; then
    echo "🎨 Installing frontend dependencies..."
    npm ci --prefix frontend || npm install --prefix frontend
else
    echo "⚠️  No frontend/package.json found, skipping frontend setup"
fi

# Step 6: Install shellcheck
echo "🐚 Installing shellcheck..."
./scripts/install-shellcheck.sh .venv/bin || {
    echo "⚠️  Warning: Failed to install shellcheck"
    echo "   You may need to run: ./scripts/install-shellcheck.sh manually"
}

# Step 7: Install Playwright browsers
if grep -q "playwright" pyproject.toml 2>/dev/null; then
    echo "🎭 Installing Playwright browsers..."
    .venv/bin/playwright install chromium || {
        echo "⚠️  Warning: Failed to install Playwright browsers"
        echo "   You may need to run: .venv/bin/playwright install chromium manually"
    }
fi

# Step 8: Update Claude plugin marketplaces
if command -v claude >/dev/null 2>&1; then
    echo "🔄 Updating Claude plugin marketplaces..."
    claude plugin marketplace update || {
        echo "⚠️  Warning: Failed to update Claude plugin marketplaces"
        echo "   You may need to run: claude plugin marketplace update manually"
    }
else
    echo "⚠️  Claude CLI not found, skipping plugin marketplace update"
fi

# Step 9: Verify setup
echo ""
echo "✅ Workspace setup complete!"
echo ""
echo "Next steps:"
echo "  1. Activate the virtual environment:"
echo "     source .venv/bin/activate"
echo ""
echo "  2. Run tests:"
echo "     poe test"
echo ""
echo "  3. Start development server:"
echo "     poe dev"
echo ""
echo "  4. Run linting:"
echo "     poe lint"
echo ""

