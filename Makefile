SHELL := /bin/bash
HERMES_PLUGINS := $(HOME)/.hermes/plugins/hermes-discord-quota

.PHONY: install uninstall test

install:
	mkdir -p $(HOME)/.hermes/plugins
	ln -sfn $(CURDIR) $(HERMES_PLUGINS)
	@echo "Linked $(CURDIR) -> $(HERMES_PLUGINS)"

uninstall:
	rm -f $(HERMES_PLUGINS)
	@echo "Removed $(HERMES_PLUGINS)"

test:
	uv run pytest tests/
