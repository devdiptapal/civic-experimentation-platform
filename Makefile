# Developer entry points. Everything here runs with the standard library
# alone except `lint`, so a contributor without network access can still
# test their change.

PYTHON ?= python3
export PYTHONPATH := src

.PHONY: help test lint example clean install check

help:
	@echo "make test     - run the full test suite (no dependencies required)"
	@echo "make example  - regenerate the worked example and its readouts"
	@echo "make lint     - run ruff (requires: pip install -e '.[dev]')"
	@echo "make check    - test, then confirm the committed example is current"
	@echo "make install  - install the package in editable mode"
	@echo "make clean    - remove caches and build artifacts"

test:
	$(PYTHON) -m unittest discover -s tests -t . -v

lint:
	ruff check src tests examples
	ruff format --check src tests

example:
	$(PYTHON) examples/sf-hsa-document-upload/run_example.py

# Every file written by run_example.py. All of them are byte-for-byte
# reproducible: the simulation is seeded and the example pins the audit
# clock, so any difference here means the code changed and the committed
# evidence did not.
GENERATED := examples/sf-hsa-document-upload

check: test
	@echo "Confirming the committed example is reproducible..."
	@$(PYTHON) examples/sf-hsa-document-upload/run_example.py > /dev/null
	@git diff --exit-code --stat -- $(GENERATED) \
		|| (echo "ERROR: committed example artifacts are stale; run 'make example' and commit them" && exit 1)
	@echo "Example artifacts are current."

install:
	$(PYTHON) -m pip install -e ".[dev]"

clean:
	rm -rf build dist .pytest_cache .ruff_cache htmlcov .coverage
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.egg-info' -type d -prune -exec rm -rf {} +
