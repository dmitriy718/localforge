PYTHON ?= ./venv/bin/python
CONFIG ?= localforge.yaml

.PHONY: doctor mcp-smoke test compile smoke docker-build docker-smoke run dry-run

doctor:
	$(PYTHON) -m localforge.cli doctor --config $(CONFIG)

mcp-smoke:
	$(PYTHON) -m localforge.cli mcp-smoke --config $(CONFIG)

test:
	$(PYTHON) -m unittest discover -s tests

compile:
	$(PYTHON) -m compileall localforge tests

smoke:
	./scripts/smoke.sh

docker-build:
	docker build -t localforge:0.1.0 .

docker-smoke:
	docker run --rm localforge:0.1.0 --help

run:
	$(PYTHON) -m localforge.cli run --config $(CONFIG) "$(PROMPT)"

dry-run:
	$(PYTHON) -m localforge.cli run --config $(CONFIG) --dry-run "$(PROMPT)"
