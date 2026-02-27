VENV_DIR   := $(HOME)/.local/share/mnemon/venv
BIN_DIR    := $(HOME)/.local/bin
DRAWIO     := /snap/bin/drawio
DIAGRAMS   := docs/diagrams
DRAWIO_SRC := $(wildcard $(DIAGRAMS)/*.drawio)
DRAWIO_PNG := $(DRAWIO_SRC:.drawio=.drawio.png)

.PHONY: install uninstall test e2e clean dev diagrams

install:
	python3 -m venv $(VENV_DIR)
	$(VENV_DIR)/bin/pip install --quiet .
	@mkdir -p $(BIN_DIR)
	ln -sf $(VENV_DIR)/bin/mnemon $(BIN_DIR)/mnemon
	@echo "Installed: $(BIN_DIR)/mnemon"

uninstall:
	rm -f $(BIN_DIR)/mnemon
	rm -rf $(VENV_DIR)
	@echo "Uninstalled mnemon"

dev:
	poetry install

test:
	poetry run pytest tests/ -v

e2e:
	poetry run bash scripts/e2e_test.sh

clean:
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true

diagrams: $(DRAWIO_PNG)

$(DIAGRAMS)/%.drawio.png: $(DIAGRAMS)/%.drawio
	xvfb-run $(DRAWIO) -x -f png -e -b 10 -o $@ $<
