PY ?= python3
VENV := .venv
BIN := $(VENV)/bin
SITL_OUT ?= build/sitl
AP_META := build/copter-4.6.3.pdef.xml
AP_META_URL := https://autotest.ardupilot.org/Parameters/versioned/Copter/stable-4.6.3/apm.pdef.xml

.PHONY: install test lint check ap-meta sitl-image sitl demo clean

$(BIN)/paramguard: pyproject.toml
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q -e '.[dev]'
	touch $@

install: $(BIN)/paramguard

test: install
	$(BIN)/pytest -q

lint: install
	$(BIN)/ruff check src tests sitl
	$(BIN)/ruff format --check src tests sitl
	$(BIN)/mypy src

check: lint test

# The ArduCopter 4.6.3 metadata, to check the built-in ArduCopter rules against.
ap-meta: $(AP_META)
	PARAMGUARD_AP_META=$(AP_META) $(BIN)/pytest -q tests/test_rules.py -k ardupilot

$(AP_META):
	mkdir -p build
	curl -sSfL -o $@ $(AP_META_URL)

sitl-image:
	docker build -t paramguard-sitl:dev sitl/

# Fly the link-loss run with the vehicle's file and with the cleanup, then
# measure both from telemetry and redraw docs/sitl.{svg,gif}.
sitl: sitl-image
	sitl/run.sh $(SITL_OUT) before=examples/survey-quad/vehicle.params after=examples/survey-quad/cleanup.params
	docker run --rm -u $$(id -u):$$(id -g) -e HOME=/tmp -e MPLCONFIGDIR=/tmp -v $(CURDIR):/repo \
	  -v $(CURDIR)/$(SITL_OUT):/out paramguard-sitl:dev \
	  python3 /repo/sitl/analyze.py /out --check --json /repo/docs/sitl-results.json \
	  --svg /repo/docs/sitl.svg --gif /repo/docs/sitl.gif

# Re-record docs/demo.gif. The VHS image has Python 3.11, enough to run paramguard from source.
demo:
	docker run --rm -u $$(id -u):$$(id -g) -e HOME=/tmp -v $(CURDIR):/vhs ghcr.io/charmbracelet/vhs:v0.10.0 docs/demo.tape

clean:
	rm -rf build $(VENV) .pytest_cache .mypy_cache .ruff_cache .hypothesis
