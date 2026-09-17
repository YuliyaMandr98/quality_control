.PHONY: help setup dev stop clean triage-bugs review-pr review-comment-fixes upload-test-cases audit-skipped-tests review-test-cases audit-bug-backlog uat-bug-test-cases

help:
	@echo "Triage Bugs Tool (Claude edition) - Developer Makefile"
	@echo ""
	@echo "  make setup  - Create venv, install deps, copy .env.example -> .env"
	@echo "  make dev    - Start the app on http://localhost:8001 (Ctrl-C to stop)"
	@echo "  make stop   - Kill a background dev process (if started with 'make dev &')"
	@echo "  make clean  - Remove caches"
	@echo ""
	@echo "Run a workflow directly from the CLI (no UI/server needed) - pass its"
	@echo "arguments via ARGS. All read .env the same way 'make dev' does."
	@echo "  make triage-bugs ARGS=\"--apply --add-comment\""
	@echo "  make review-pr ARGS=\"--repo my-repo --pr 1234\""
	@echo "  make review-comment-fixes ARGS=\"--repo my-repo --pr 1234\""
	@echo "  make upload-test-cases ARGS=\"--us 20.1.1 --plan web --csv path/to/cases.csv\""
	@echo "  make audit-skipped-tests ARGS=\"--tests-root /path/to/tests\""
	@echo "  make review-test-cases ARGS=\"--us 20.1.1 --test-type web --spec-url ... --test-cases-file cases.txt\""
	@echo "  make audit-bug-backlog ARGS=\"--max-results 50\""
	@echo "  make uat-bug-test-cases ARGS=\"--sprint 23 --apply\""
	@echo "  (add --help to ARGS on any of the above for the full list of options)"

VENV := venv
PIP := $(VENV)/bin/pip
UVICORN := $(VENV)/bin/uvicorn
PYTHON := $(VENV)/bin/python
# Requires Python 3.11+ (the codebase uses `X | None` union syntax evaluated
# at import time). Prefer a versioned interpreter over a bare `python3`,
# which on macOS often resolves to the older system Python.
PYTHON_BIN := $(shell command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3)

setup:
	@echo "==> Using interpreter: $(PYTHON_BIN) ($$($(PYTHON_BIN) --version))"
	@$(PYTHON_BIN) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || \
		(echo "ERROR: Python 3.11+ required. Install it (e.g. 'brew install python@3.11') and re-run make setup." && exit 1)
	@echo "==> Creating virtual environment..."
	$(PYTHON_BIN) -m venv $(VENV)
	@echo "==> Installing dependencies..."
	$(PIP) install --upgrade pip
	$(PIP) install -e .
	@[ -f .env ] || cp .env.example .env
	mkdir -p data/artifacts
	@echo ""
	@echo "Setup complete!"
	@echo ""
	@echo "Next steps:"
	@echo "  1. Edit .env and fill in your Jira/Confluence/Anthropic credentials"
	@echo "     (or leave them blank and configure via the Integrations page instead)"
	@echo "  2. Run: make dev"

dev:
	@mkdir -p data
	@echo "==> Starting Triage Bugs Tool (Claude edition)..."
	@echo "    App  -> http://localhost:8001"
	@echo "    Docs -> http://localhost:8001/docs"
	@echo "    Press Ctrl-C to stop."
	PYTHONPATH=$(PWD) $(UVICORN) apps.app.main:app --reload --port 8001

stop:
	-pkill -f "uvicorn apps.app.main"
	@echo "Dev process stopped"

triage-bugs:
	PYTHONPATH=$(PWD) $(PYTHON) scripts/triage_bugs.py $(ARGS)

review-pr:
	PYTHONPATH=$(PWD) $(PYTHON) scripts/review_pull_request.py $(ARGS)

review-comment-fixes:
	PYTHONPATH=$(PWD) $(PYTHON) scripts/review_comment_fixes.py $(ARGS)

upload-test-cases:
	PYTHONPATH=$(PWD) $(PYTHON) scripts/upload_test_cases.py $(ARGS)

audit-skipped-tests:
	PYTHONPATH=$(PWD) $(PYTHON) scripts/audit_skipped_tests.py $(ARGS)

review-test-cases:
	PYTHONPATH=$(PWD) $(PYTHON) scripts/review_test_cases.py $(ARGS)

audit-bug-backlog:
	PYTHONPATH=$(PWD) $(PYTHON) scripts/bug_backlog_audit.py $(ARGS)

uat-bug-test-cases:
	PYTHONPATH=$(PWD) $(PYTHON) scripts/uat_bug_test_cases.py $(ARGS)

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null; true
	@echo "Cache cleaned"

.DEFAULT_GOAL := help
