.PHONY: lint test ci

lint:
	.venv/bin/ruff check src tests

test:
	.venv/bin/python -m pytest tests/ -q

ci: lint test
