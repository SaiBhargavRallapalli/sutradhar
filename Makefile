.DEFAULT_GOAL := help
PYTHON ?= python3

.PHONY: help install test cov lint fmt typecheck check demo web clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install package + dev tooling (editable)
	$(PYTHON) -m pip install -e ".[dev]"

test: ## Run the unit test suite
	$(PYTHON) -m pytest

cov: ## Run tests with a coverage report
	$(PYTHON) -m pytest --cov=sutradhar --cov-report=term-missing

lint: ## Lint with ruff
	$(PYTHON) -m ruff check src tests

fmt: ## Format with ruff
	$(PYTHON) -m ruff format src tests

typecheck: ## Static type checking with mypy (strict)
	$(PYTHON) -m mypy

check: lint typecheck test ## Lint + typecheck + test

demo: ## Generate a full demo plan in the terminal
	$(PYTHON) -m sutradhar.cli demo --no-color

web: ## Serve the accessible web UI at http://127.0.0.1:8787
	$(PYTHON) -m sutradhar.cli web --port 8787

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info src/*.egg-info
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
