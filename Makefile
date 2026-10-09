# Porthole developer tasks. PYTHON defaults to .venv/bin/python when that exists, else python3.
PYTHON ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
PY := $(if $(findstring /,$(PYTHON)),$(abspath $(PYTHON)),$(PYTHON))

.PHONY: help dev test lint smoke vendor leak-sweep check-names audit

help:
	@echo "make dev        docker compose up: head, two tentacles, fake Insights"
	@echo "make test       the four test suites (head, infra, harness, tentacle)"
	@echo "make lint       ruff over the whole repo"
	@echo "make smoke      start head/dev/run_local.py and fetch every page and route"
	@echo "make vendor     download uPlot only if its files are missing"
	@echo "make leak-sweep secret shapes, lab names and public IPs"
	@echo "make check-names every metric name in the repo is in watcher/catalog"
	@echo "make audit      pip-audit over the pinned requirements"

dev:
	@test -f head/.env || cp head/.env.example head/.env
	docker compose up --build

test:
	cd head && $(PY) -m pytest -q
	cd infra && $(PY) -m pytest -q
	cd harness && $(PY) -m pytest -q
	cd tentacle && $(PY) -m pytest -q

lint:
	$(PY) -m ruff check .

smoke:
	PYTHON=$(PY) bash head/dev/smoke.sh

vendor:
	$(PY) scripts/vendor_uplot.py

leak-sweep:
	$(PY) scripts/leak_sweep.py

check-names:
	$(PY) scripts/check_metric_names.py

audit:
	$(PY) -m pip_audit -r head/requirements.txt -r tentacle/requirements.txt -r infra/requirements.txt
