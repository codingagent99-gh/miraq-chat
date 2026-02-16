#!/usr/bin/env bash
# ─────────────────────────────────────────────
# WGC Tiles Store Intent Classifier — Setup & Run
# Usage: chmod +x setup.sh && ./setup.sh
# ─────────────────────────────────────────────

set -e  # Exit on any error

PROJECT_NAME="wgc-intent-classifier"
PYTHON_MIN_VERSION="3.10"
VENV_DIR=".venv"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🏗️  $PROJECT_NAME — Setup Script"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ─── Step 1: Check Python version ───
echo ""
echo "📌 Step 1: Checking Python version..."

if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 not found. Please install Python $PYTHON_MIN_VERSION+"
    echo "   → https://www.python.org/downloads/"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "   Found Python $PYTHON_VERSION"

# Compare versions
REQUIRED_MAJOR=3
REQUIRED_MINOR=10
ACTUAL_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
ACTUAL_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$ACTUAL_MAJOR" -lt "$REQUIRED_MAJOR" ] || \
   ([ "$ACTUAL_MAJOR" -eq "$REQUIRED_MAJOR" ] && [ "$ACTUAL_MINOR" -lt "$REQUIRED_MINOR" ]); then
    echo "❌ Python $PYTHON_MIN_VERSION+ required, found $PYTHON_VERSION"
    exit 1
fi
echo "   ✅ Python version OK"

# ─── Step 2: Create project structure ───
echo ""
echo "📌 Step 2: Creating project structure..."

mkdir -p config core services training tests

# Create __init__.py files
for dir in config core services training tests; do
    touch "$dir/__init__.py"
done
echo "   ✅ Directories created"

# ─── Step 3: Create virtual environment ───
echo ""
echo "📌 Step 3: Setting up virtual environment..."

if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    echo "   ✅ Virtual environment created at $VENV_DIR/"
else
    echo "   ⏭️  Virtual environment already exists"
fi

# Activate virtual environment
source "$VENV_DIR/bin/activate"
echo "   ✅ Virtual environment activated"

# ─── Step 4: Upgrade pip ───
echo ""
echo "📌 Step 4: Upgrading pip..."
pip install --upgrade pip --quiet
echo "   ✅ pip upgraded"

# ─── Step 5: Install dependencies ───
echo ""
echo "📌 Step 5: Installing dependencies..."

if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt --quiet
    echo "   ✅ Dependencies installed"
else
    echo "   ❌ requirements.txt not found!"
    exit 1
fi

# ─── Step 6: Create .env if not exists ───
echo ""
echo "📌 Step 6: Checking .env file..."

if [ ! -f ".env" ]; then
    cat > .env << 'EOF'
# WooCommerce REST API Credentials
# Generate at: WordPress Admin → WooCommerce → Settings → Advanced → REST API
WOO_BASE_URL=https://wgc.net.in/hn/wp-json/wc/v3
WOO_CONSUMER_KEY=ck_your_consumer_key_here
WOO_CONSUMER_SECRET=cs_your_consumer_secret_here

# App Settings
DEBUG=true
LOG_LEVEL=INFO
EOF
    echo "   ✅ .env file created (⚠️  UPDATE WITH YOUR API KEYS!)"
else
    echo "   ⏭️  .env file already exists"
fi

# ─── Step 7: Create .gitignore ───
echo ""
echo "📌 Step 7: Checking .gitignore..."

if [ ! -f ".gitignore" ]; then
    cat > .gitignore << 'EOF'
# Virtual environment
.venv/
venv/
env/

# Environment variables (NEVER COMMIT)
.env

# Python
__pycache__/
*.py[cod]
*.egg-info/
dist/
build/

# IDE
.vscode/
.idea/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db

# Testing
.pytest_cache/
htmlcov/
.coverage
EOF
    echo "   ✅ .gitignore created"
else
    echo "   ⏭️  .gitignore already exists"
fi

# ─── Step 8: Verify installation ───
echo ""
echo "📌 Step 8: Verifying installation..."

python3 -c "
import requests
from dotenv import load_dotenv
print('   ✅ requests:', requests.__version__)
print('   ✅ python-dotenv: OK')
"

# Check optional deps
python3 -c "
try:
    from thefuzz import fuzz
    print('   ✅ thefuzz: OK')
except ImportError:
    print('   ⚠️  thefuzz: not installed (optional)')
"

# ─── Step 9: Run evaluation ───
echo ""
echo "📌 Step 9: Running classifier evaluation..."
echo ""

python3 -c "
import sys
sys.path.insert(0, '.')

try:
    from training.evaluate import evaluate
    evaluate()
except ImportError as e:
    print(f'   ⚠️  Skipping evaluation (missing module: {e})')
    print('   Run manually after creating all files: python -m training.evaluate')
"

# ─── Step 10: Run main test ───
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━��━━━━━━━━━━━━━━━━━━━"
echo "  ✅ Setup Complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  📋 Next steps:"
echo ""
echo "  1. Update .env with your WooCommerce API keys:"
echo "     nano .env"
echo ""
echo "  2. Activate the virtual environment:"
echo "     source $VENV_DIR/bin/activate"
echo ""
echo "  3. Run the classifier:"
echo "     python main.py"
echo ""
echo "  4. Run tests:"
echo "     pytest tests/ -v"
echo ""
echo "  5. Evaluate accuracy:"
echo "     python -m training.evaluate"
echo ""
echo "  6. Interactive mode:"
echo "     python -c \"from main import process; process(input('Ask: '))\""
echo ""