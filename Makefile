PY := ./.venv/bin/python

.PHONY: sync run
sync:
	uv sync

# ex) make run SCRIPT=src/backtest/run_backtest.py
run:
	$(PY) $(SCRIPT)
