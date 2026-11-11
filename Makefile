.PHONY: lint test ci

lint:
	.venv/bin/ruff check src tests discuss_agent.py discuss_driver.py discuss_common.py

test:
	.venv/bin/python -m pytest tests/ -q

ci: lint test
