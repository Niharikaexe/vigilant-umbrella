.PHONY: help install run demo test lint fmt docker clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Create the venv and install dependencies
	python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements-dev.txt

run:  ## Start the service and dashboard on http://localhost:8000
	.venv/bin/python -m uvicorn agentflow.api:app --reload --port 8000

demo:  ## Same, paced so the agent graph is watchable
	AGENTFLOW_STEP_DELAY_MS=450 .venv/bin/python -m uvicorn agentflow.api:app --port 8000

test:  ## Run the test suite
	AGENTFLOW_STEP_DELAY_MS=0 .venv/bin/python -m pytest

lint:  ## Lint and format-check
	.venv/bin/ruff check agentflow tests
	.venv/bin/ruff format --check agentflow tests

fmt:  ## Autoformat
	.venv/bin/ruff format agentflow tests
	.venv/bin/ruff check --fix agentflow tests

docker:  ## Build and run in Docker
	docker compose up --build

clean:
	rm -rf .pytest_cache .ruff_cache data/runs **/__pycache__
