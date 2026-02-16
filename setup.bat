@echo off
REM ─────────────────────────────────────────────
REM WGC Tiles Store Intent Classifier — Windows Setup
REM Usage: setup.bat
REM ───────────────────────��─────────────────────

setlocal enabledelayedexpansion

set PROJECT_NAME=wgc-intent-classifier
set VENV_DIR=.venv

echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo   🏗️  %PROJECT_NAME% — Windows Setup
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REM ─── Step 1: Check Python ───
echo.
echo 📌 Step 1: Checking Python...
python --version 2>nul
if errorlevel 1 (
    echo ❌ Python not found. Install Python 3.10+ from https://www.python.org/downloads/
    pause
    exit /b 1
)
echo    ✅ Python found

REM ─── Step 2: Create directories ───
echo.
echo 📌 Step 2: Creating project structure...
if not exist config mkdir config
if not exist core mkdir core
if not exist services mkdir services
if not exist training mkdir training
if not exist tests mkdir tests

REM Create __init__.py files
for %%d in (config core services training tests) do (
    if not exist %%d\__init__.py type nul > %%d\__init__.py
)
echo    ✅ Directories created

REM ─── Step 3: Virtual environment ───
echo.
echo 📌 Step 3: Setting up virtual environment...
if not exist %VENV_DIR% (
    python -m venv %VENV_DIR%
    echo    ✅ Virtual environment created
) else (
    echo    ⏭️  Virtual environment already exists
)

REM Activate
call %VENV_DIR%\Scripts\activate.bat
echo    ✅ Virtual environment activated

REM ─── Step 4: Upgrade pip ───
echo.
echo 📌 Step 4: Upgrading pip...
python -m pip install --upgrade pip --quiet
echo    ✅ pip upgraded

REM ─── Step 5: Install dependencies ───
echo.
echo 📌 Step 5: Installing dependencies...
if exist requirements.txt (
    pip install -r requirements.txt --quiet
    echo    ✅ Dependencies installed
) else (
    echo    ❌ requirements.txt not found!
    pause
    exit /b 1
)

REM ─── Step 6: Create .env ───
echo.
echo 📌 Step 6: Checking .env file...
if not exist .env (
    (
        echo # WooCommerce REST API Credentials
        echo WOO_BASE_URL=https://wgc.net.in/hn/wp-json/wc/v3
        echo WOO_CONSUMER_KEY=ck_your_consumer_key_here
        echo WOO_CONSUMER_SECRET=cs_your_consumer_secret_here
        echo.
        echo # App Settings
        echo DEBUG=true
        echo LOG_LEVEL=INFO
    ) > .env
    echo    ✅ .env created (⚠️  UPDATE WITH YOUR API KEYS!)
) else (
    echo    ⏭️  .env already exists
)

REM ─── Step 7: Create .gitignore ───
echo.
echo 📌 Step 7: Checking .gitignore...
if not exist .gitignore (
    (
        echo .venv/
        echo .env
        echo __pycache__/
        echo *.py[cod]
        echo .pytest_cache/
        echo .coverage
        echo .idea/
        echo .vscode/
    ) > .gitignore
    echo    ✅ .gitignore created
) else (
    echo    ⏭️  .gitignore already exists
)

REM ─── Step 8: Verify ───
echo.
echo 📌 Step 8: Verifying installation...
python -c "import requests; print('   ✅ requests:', requests.__version__)"
python -c "from dotenv import load_dotenv; print('   ✅ python-dotenv: OK')"

REM ─── Done ───
echo.
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━���━━━━━━━━━━
echo   ✅ Setup Complete!
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo.
echo   Next steps:
echo.
echo   1. Update .env with your WooCommerce API keys:
echo      notepad .env
echo.
echo   2. Activate virtual environment:
echo      %VENV_DIR%\Scripts\activate.bat
echo.
echo   3. Run the classifier:
echo      python main.py
echo.
echo   4. Run tests:
echo      pytest tests\ -v
echo.
pause