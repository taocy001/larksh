.PHONY: setup run dev lint clean install tag lock

PYTHON ?= python3
VENV   := .venv
PIP    := $(VENV)/bin/pip
PY     := $(VENV)/bin/python

setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

run: setup
	$(PY) main.py

dev:
	$(PY) main.py --config config.yaml

lint:
	$(VENV)/bin/ruff check . || true
	$(VENV)/bin/mypy . --ignore-missing-imports || true

DEPLOY_DIR := /opt/larksh
PIDFILE    := /tmp/larksh.pid

install:
	@test -d $(DEPLOY_DIR)/.venv || { echo "❌ $(DEPLOY_DIR)/.venv does not exist — run install.sh on the target machine first"; exit 1; }
	@echo "→ Syncing code to $(DEPLOY_DIR) ..."
	@for f in main.py requirements.txt larksh-client; do cp $$f $(DEPLOY_DIR)/$$f; done
	@find bot messaging shell security utils -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null; true
	@cp -r bot messaging shell security utils $(DEPLOY_DIR)/
	@chmod +x $(DEPLOY_DIR)/larksh-client
	@echo "→ Updating dependencies ..."
	@$(DEPLOY_DIR)/.venv/bin/pip install -q -r $(DEPLOY_DIR)/requirements.txt
	@echo "→ Restarting service ..."
	@$(MAKE) -s restart

restart:
	@# Prefer restarting via systemd (if sudo privileges are available)
	@if sudo -n systemctl restart larksh 2>/dev/null; then \
		echo "✅ systemd restart successful"; \
	else \
		echo "→ Restarting via process signal ..."; \
		if [ -f $(PIDFILE) ]; then kill $$(cat $(PIDFILE)) 2>/dev/null || true; rm -f $(PIDFILE); fi; \
		pgrep -f '$(DEPLOY_DIR)/.venv/bin/python main.py' | xargs kill 2>/dev/null || true; \
		sleep 1; \
		cd $(DEPLOY_DIR) && nohup .venv/bin/python main.py --config /etc/larksh/config.yaml >>/tmp/larksh.log 2>&1 & \
		echo $$! > $(PIDFILE); \
		sleep 2; \
		kill -0 $$(cat $(PIDFILE)) 2>/dev/null && echo "✅ Deploy complete (pid=$$(cat $(PIDFILE)))" || echo "❌ Startup failed, check: tail -50 /tmp/larksh.log"; \
	fi

clean:
	rm -rf $(VENV) __pycache__ **/__pycache__ *.pyc

# Generate dependency lock file (pins all transitive dependency versions)
lock:
	$(PIP) freeze > requirements.lock
	@echo "✅ requirements.lock updated"

# Create a version tag and trigger dist build
# Usage: make tag VERSION=v1.0.0
tag:
	@test -n "$(VERSION)" || { echo "❌ Usage: make tag VERSION=v1.0.0"; exit 1; }
	@git diff --quiet && git diff --cached --quiet || { echo "❌ Uncommitted changes detected — please commit first"; exit 1; }
	git tag $(VERSION)
	@echo "✅ Tagged $(VERSION) — run make dist to build the archive"

# ---------------------------------------------------------------------------
# Distribution packaging
# ---------------------------------------------------------------------------

DIST_DIR := dist
VERSION  := $(shell git describe --tags --always --dirty="-dirty" 2>/dev/null || echo "dev")

# Build source tarball with install.sh installer
# Targets: servers, OpenWrt (requires python3 + pip3), any Linux
#
# Installation on target machine:
#   tar xzf larksh-<ver>.tar.gz
#   cd larksh-<ver> && sh install.sh
#
dist:
	@echo "→ Building larksh-$(VERSION).tar.gz ..."
	@rm -rf $(DIST_DIR)
	@mkdir -p $(DIST_DIR)/larksh-$(VERSION)
	@cp -r bot messaging security shell utils $(DIST_DIR)/larksh-$(VERSION)/
	@find $(DIST_DIR)/larksh-$(VERSION) -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null; true
	@cp main.py requirements.txt requirements.lock README.md \
		larksh-client install.sh pyproject.toml $(DIST_DIR)/larksh-$(VERSION)/
	@cp -r deploy $(DIST_DIR)/larksh-$(VERSION)/
	@chmod +x $(DIST_DIR)/larksh-$(VERSION)/install.sh
	@tar -czf $(DIST_DIR)/larksh-$(VERSION).tar.gz -C $(DIST_DIR) larksh-$(VERSION)
	@rm -rf $(DIST_DIR)/larksh-$(VERSION)
	@echo "✅ $(DIST_DIR)/larksh-$(VERSION).tar.gz ($$(du -sh $(DIST_DIR)/larksh-$(VERSION).tar.gz | cut -f1))"
