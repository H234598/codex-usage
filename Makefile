.PHONY: check applet-check test install-local uninstall-local

PYTHON ?= python3
UUID := codex-usage@H234598
APPLET_DIR := files/$(UUID)

check: applet-check test

applet-check:
	$(PYTHON) -m json.tool $(APPLET_DIR)/metadata.json >/dev/null
	$(PYTHON) -m json.tool $(APPLET_DIR)/settings-schema.json >/dev/null
	@if command -v gjs >/dev/null 2>&1; then \
		gjs -c 'const GLib = imports.gi.GLib; const ByteArray = imports.byteArray; const path = ARGV[ARGV.length - 1]; const result = GLib.file_get_contents(path); if (!result[0]) throw new Error("read failed"); new Function(ByteArray.toString(result[1]));' -- $(APPLET_DIR)/applet.js; \
	fi

test:
	$(PYTHON) -m pytest

install-local: applet-check
	$(PYTHON) scripts/install_cinnamon_applet.py

uninstall-local:
	$(PYTHON) scripts/uninstall_cinnamon_applet.py
