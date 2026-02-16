@echo off
REM Quick run script for Windows
REM Usage: run.bat [mode]
REM   run.bat              → Run all test utterances
REM   run.bat interactive  → Interactive chat mode
REM   run.bat evaluate     → Accuracy evaluation
REM   run.bat test         → Run pytest

if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat

if "%1"=="interactive" goto interactive
if "%1"=="i" goto interactive
if "%1"=="evaluate" goto evaluate
if "%1"=="e" goto evaluate
if "%1"=="test" goto test
if "%1"=="t" goto test
goto default

:interactive
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo   🗣️  WGC Tiles — Interactive Mode
echo   Type 'quit' to stop
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
python -c "import sys; sys.path.insert(0,'.'); exec(open('main.py').read()); [process(input('\n💬 You: ')) for _ in iter(int, 1)]"
goto end

:evaluate
echo 📊 Running evaluation...
python -m training.evaluate
goto end

:test
echo 🧪 Running tests...
pytest tests\ -v --tb=short
goto end

:default
echo 🏃 Running all test utterances...
python main.py
goto end

:end
pause