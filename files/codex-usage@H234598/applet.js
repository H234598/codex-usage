const Applet = imports.ui.applet;
const Gio = imports.gi.Gio;
const GLib = imports.gi.GLib;
const Lang = imports.lang;
const Main = imports.ui.main;
const Mainloop = imports.mainloop;
const PopupMenu = imports.ui.popupMenu;
const Settings = imports.ui.settings;

const UUID = "codex-usage@H234598";
const ANALYTICS_URL = "https://chatgpt.com/codex/cloud/settings/analytics";
const MAX_JSON_CHARS = 262144;
const MAX_STDERR_CHARS = 4096;
const MAX_ACCOUNTS = 100;
const MAX_TEXT_CHARS = 500;
const COMMAND_TIMEOUT_MS = 120000;
const PANEL_CLASSES = [
    "codex-usage-panel-warning",
    "codex-usage-panel-critical",
    "codex-usage-panel-error"
];

function _(text) {
    return String(text || "");
}

function CodexUsageApplet(metadata, orientation, panelHeight, instanceId) {
    this._init(metadata, orientation, panelHeight, instanceId);
}

CodexUsageApplet.prototype = {
    __proto__: Applet.TextIconApplet.prototype,

    _init: function(metadata, orientation, panelHeight, instanceId) {
        Applet.TextIconApplet.prototype._init.call(this, orientation, panelHeight, instanceId);

        this.metadata = metadata || {};
        this.instanceId = instanceId;
        this.commandPath = "codex-usage";
        this.configPath = "";
        this.autoRefresh = true;
        this.refreshInterval = 300;
        this.refreshOnOpen = true;
        this.showPanelLabel = true;
        this.panelAccountMode = "combined";
        this.panelPercentSource = "average";
        this.warningThreshold = 20;
        this.notifyWarnings = false;
        this.notifyErrors = false;

        this._removed = false;
        this._generation = 0;
        this._timerId = 0;
        this._timeoutId = 0;
        this._process = null;
        this._refreshing = false;
        this._usages = [];
        this._warningState = {};
        this._errorState = {};

        this.set_applet_icon_symbolic_name("view-statistics-symbolic");
        this.set_applet_label("--");
        this.set_applet_tooltip(_("Codex-Nutzung wird geladen"));

        this.menuManager = new PopupMenu.PopupMenuManager(this);
        this.menu = new Applet.AppletPopupMenu(this, orientation);
        this.menuManager.addMenu(this.menu);
        try {
            this.menu.box.style = "min-width: 38em;";
        } catch (e) {
            global.log("[" + UUID + "] menu width unavailable: " + String(e));
        }

        this.settings = new Settings.AppletSettings(this, UUID, instanceId);
        this._bindSettings();
        this._buildLoadingMenu(_("Lade gespeicherte Werte …"));
        this._scheduleTimer();
        this._loadCached(true);
    },

    _bindSettings: function() {
        let bind = Lang.bind(this, function(key, property, callback) {
            this.settings.bindProperty(
                Settings.BindingDirection.IN,
                key,
                property,
                callback,
                null
            );
        });
        bind("command-path", "commandPath", this._onCommandSettingsChanged);
        bind("config-path", "configPath", this._onCommandSettingsChanged);
        bind("auto-refresh", "autoRefresh", this._onRefreshSettingsChanged);
        bind("refresh-interval", "refreshInterval", this._onRefreshSettingsChanged);
        bind("refresh-on-open", "refreshOnOpen", null);
        bind("show-panel-label", "showPanelLabel", this._updatePanel);
        bind("panel-account-mode", "panelAccountMode", this._updatePanel);
        bind("panel-percent-source", "panelPercentSource", this._updatePanel);
        bind("warning-threshold", "warningThreshold", this._updatePanel);
        bind("notify-warnings", "notifyWarnings", null);
        bind("notify-errors", "notifyErrors", null);
    },

    _onCommandSettingsChanged: function() {
        this._loadCached(true);
    },

    _onRefreshSettingsChanged: function() {
        this._scheduleTimer();
    },

    _scheduleTimer: function() {
        if (this._timerId) {
            Mainloop.source_remove(this._timerId);
            this._timerId = 0;
        }
        if (!this.autoRefresh || this._removed) {
            return;
        }
        let seconds = this._boundedInteger(this.refreshInterval, 60, 3600, 300);
        this._timerId = Mainloop.timeout_add_seconds(seconds, Lang.bind(this, function() {
            this._refreshFresh(false);
            return true;
        }));
    },

    _loadCached: function(refreshAfter) {
        this._spawnUsageCommand("latest", Lang.bind(this, function(payload, error) {
            if (payload) {
                this._applyPayload(payload, false);
            } else if (!this._usages.length && error) {
                this._showCommandError(error);
            }
            if (refreshAfter && this.autoRefresh) {
                this._refreshFresh(false);
            }
        }));
    },

    _refreshFresh: function(openAfter) {
        if (this._refreshing || this._removed) {
            return;
        }
        this._refreshing = true;
        this._updatePanel();
        if (this._usages.length) {
            this._buildUsageMenu();
        } else {
            this._buildLoadingMenu(_("Aktualisiere Accounts …"));
        }
        this._spawnUsageCommand("once", Lang.bind(this, function(payload, error) {
            this._refreshing = false;
            if (payload) {
                this._applyPayload(payload, true);
            } else {
                this._showCommandError(error || _("Abruf fehlgeschlagen"));
            }
            if (openAfter && !this.menu.isOpen) {
                this.menu.toggle();
            }
        }));
    },

    _spawnUsageCommand: function(subcommand, callback) {
        let executable;
        try {
            executable = this._resolveCommand();
        } catch (e) {
            callback(null, String(e));
            return;
        }
        let argv = [executable];
        let config = String(this.configPath || "").trim();
        if (config) {
            if (config.length > 1024 || config.indexOf("\u0000") !== -1) {
                callback(null, _("Ungültiger Config-Pfad"));
                return;
            }
            argv.push("--config", config);
        }
        argv.push(subcommand, "--format", "json");
        this._spawnJsonArray(argv, callback);
    },

    _resolveCommand: function() {
        let configured = String(this.commandPath || "codex-usage").trim();
        if (!configured || configured.length > 1024 || configured.indexOf("\u0000") !== -1) {
            throw new Error(_("Ungültiger codex-usage-Pfad"));
        }
        if (configured.indexOf("/") !== -1) {
            let expanded = configured;
            if (configured.indexOf("~/") === 0) {
                expanded = GLib.build_filenamev([GLib.get_home_dir(), configured.slice(2)]);
            }
            if (!GLib.file_test(expanded, GLib.FileTest.IS_EXECUTABLE)) {
                throw new Error(_("codex-usage ist nicht ausführbar: ") + expanded);
            }
            return expanded;
        }
        let found = GLib.find_program_in_path(configured);
        if (found) {
            return found;
        }
        let localBin = GLib.build_filenamev([GLib.get_home_dir(), ".local", "bin", configured]);
        if (GLib.file_test(localBin, GLib.FileTest.IS_EXECUTABLE)) {
            return localBin;
        }
        throw new Error(_("codex-usage wurde nicht gefunden"));
    },

    _spawnJsonArray: function(argv, callback) {
        this._cancelProcess();
        let generation = ++this._generation;
        let done = false;
        let process = null;
        let finish = Lang.bind(this, function(payload, error) {
            if (done) {
                return;
            }
            done = true;
            if (this._timeoutId) {
                Mainloop.source_remove(this._timeoutId);
                this._timeoutId = 0;
            }
            if (this._removed || generation !== this._generation) {
                return;
            }
            this._process = null;
            callback(payload, error);
        });

        try {
            let launcher = Gio.SubprocessLauncher.new(
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
            );
            launcher.setenv("PYTHONUNBUFFERED", "1", true);
            process = launcher.spawnv(argv);
            this._process = process;
            this._timeoutId = Mainloop.timeout_add(COMMAND_TIMEOUT_MS, Lang.bind(this, function() {
                try {
                    process.force_exit();
                } catch (e) {
                    global.log("[" + UUID + "] force_exit failed: " + String(e));
                }
                finish(null, _("Abruf nach 120 Sekunden abgebrochen"));
                return false;
            }));
            process.communicate_utf8_async(null, null, Lang.bind(this, function(proc, result) {
                let stdout = "";
                let stderr = "";
                try {
                    let response = proc.communicate_utf8_finish(result);
                    stdout = String(response[1] || "");
                    stderr = String(response[2] || "");
                } catch (e) {
                    finish(null, _("Prozessfehler: ") + String(e));
                    return;
                }
                if (stdout.length > MAX_JSON_CHARS) {
                    finish(null, _("JSON-Ausgabe ist zu groß"));
                    return;
                }
                if (!stdout.trim()) {
                    finish(null, this._shortText(stderr || _("Keine JSON-Ausgabe"), MAX_STDERR_CHARS));
                    return;
                }
                try {
                    let parsed = JSON.parse(stdout);
                    finish(this._validatePayload(parsed), null);
                } catch (e) {
                    let detail = stderr ? ": " + this._shortText(stderr, 240) : "";
                    finish(null, _("Ungültige JSON-Ausgabe") + detail);
                }
            }));
        } catch (e) {
            finish(null, _("codex-usage konnte nicht gestartet werden: ") + String(e));
        }
    },

    _validatePayload: function(payload) {
        if (!Array.isArray(payload)) {
            throw new Error("JSON root must be an array");
        }
        if (payload.length > MAX_ACCOUNTS) {
            throw new Error("too many accounts");
        }
        let result = [];
        for (let i = 0; i < payload.length; i++) {
            let item = payload[i];
            if (!item || typeof item !== "object" || Array.isArray(item)) {
                throw new Error("invalid account entry");
            }
            let account = this._safeText(item.account, 64);
            if (!account) {
                throw new Error("account id missing");
            }
            result.push({
                account: account,
                label: this._safeText(item.label, 120) || account,
                captured_at: this._safeText(item.captured_at, 80),
                five_hour: this._safeWindow(item.five_hour),
                weekly: this._safeWindow(item.weekly),
                status: this._safeStatus(item.status),
                error: this._safeText(item.error, MAX_TEXT_CHARS),
                blocked_until: this._safeText(item.blocked_until, 80),
                blocked_reason: this._safeText(item.blocked_reason, MAX_TEXT_CHARS),
                auth_access_expires_at: this._safeText(item.auth_access_expires_at, 80),
                stale: false
            });
        }
        return result;
    },

    _safeWindow: function(value) {
        if (value === null || value === undefined) {
            return null;
        }
        if (typeof value !== "object" || Array.isArray(value)) {
            throw new Error("invalid limit window");
        }
        return {
            used: this._safeNumber(value.used),
            limit: this._safeNumber(value.limit),
            remaining: this._safeNumber(value.remaining),
            percent: this._safeNumber(value.percent),
            reset_at: this._safeText(value.reset_at, 80)
        };
    },

    _safeNumber: function(value) {
        if (value === null || value === undefined) {
            return null;
        }
        if (typeof value !== "number" || !Number.isFinite(value) || Math.abs(value) > 1000000000) {
            throw new Error("invalid numeric value");
        }
        return value;
    },

    _safeStatus: function(value) {
        let status = this._safeText(value, 32) || "error";
        if (["ok", "partial", "error", "login_required", "blocked"].indexOf(status) === -1) {
            return "error";
        }
        return status;
    },

    _safeText: function(value, limit) {
        if (value === null || value === undefined) {
            return "";
        }
        if (typeof value !== "string") {
            throw new Error("invalid text value");
        }
        let text = value.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
        if (text.length > limit) {
            text = text.slice(0, limit);
        }
        return text;
    },

    _applyPayload: function(payload, fresh) {
        let usages = fresh ? this._mergeFreshPayload(payload) : payload;
        let nowMs = Date.now();
        let staleAfterMs = this._boundedInteger(this.refreshInterval, 60, 3600, 300) * 2000;
        for (let i = 0; i < usages.length; i++) {
            let capturedMs = this._dateMillis(usages[i].captured_at);
            usages[i].stale = usages[i].stale || !capturedMs || nowMs - capturedMs > staleAfterMs;
        }
        this._usages = usages;
        this._buildUsageMenu();
        this._updatePanel();
        this._notifyForPayload();
    },

    _mergeFreshPayload: function(fresh) {
        let previous = {};
        for (let i = 0; i < this._usages.length; i++) {
            previous[this._usages[i].account] = this._usages[i];
        }
        let merged = [];
        for (let j = 0; j < fresh.length; j++) {
            let item = fresh[j];
            let old = previous[item.account];
            if (old && item.status !== "ok" && !item.five_hour && !item.weekly) {
                item.five_hour = old.five_hour;
                item.weekly = old.weekly;
                item.captured_at = old.captured_at;
                item.stale = true;
            }
            merged.push(item);
        }
        return merged;
    },

    _buildLoadingMenu: function(message) {
        this.menu.removeAll();
        this._addDisabled(this.menu, message || _("Lade …"), "codex-usage-stale");
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addActions();
    },

    _buildUsageMenu: function() {
        this.menu.removeAll();
        if (!this._usages.length) {
            this._addDisabled(this.menu, _("Keine Accounts oder Snapshots vorhanden"), "codex-usage-stale");
        } else {
            let newest = this._newestCapture();
            this._addDisabled(
                this.menu,
                _("Codex-Nutzung · Stand ") + (newest ? this._formatDate(newest) : "–"),
                "codex-usage-detail"
            );
            this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
            for (let i = 0; i < this._usages.length; i++) {
                this._addAccount(this._usages[i]);
                if (i < this._usages.length - 1) {
                    this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
                }
            }
        }
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addActions();
    },

    _addAccount: function(usage) {
        let five = this._windowValue(usage.five_hour);
        let week = this._windowValue(usage.weekly);
        let severity = this._usageSeverity(usage);
        let summary = usage.label + "     5h " + five + "     Woche " + week;
        this._addDisabled(this.menu, summary, "codex-usage-account " + severity);
        this._addDisabled(
            this.menu,
            "5h Reset " + this._windowReset(usage.five_hour) +
                "     Woche Reset " + this._windowReset(usage.weekly),
            "codex-usage-detail"
        );
        let status = this._statusLabel(usage.status);
        if (usage.stale) {
            status += " · gespeichert vom " + this._formatDate(usage.captured_at);
        }
        let detail = usage.status === "login_required"
            ? "Token abgelaufen · codex-usage login " + usage.account
            : usage.error || usage.blocked_reason;
        if (detail) {
            status += " · " + this._shortText(detail, 100);
        }
        if (usage.status !== "ok" || usage.stale) {
            this._addDisabled(
                this.menu,
                status,
                usage.status === "ok" ? "codex-usage-stale" : "codex-usage-error"
            );
        }
    },

    _addActions: function() {
        let refreshLabel = this._refreshing ? _("Aktualisierung läuft …") : _("Jetzt aktualisieren");
        let refreshItem = this.menu.addAction(refreshLabel, Lang.bind(this, function() {
            this._refreshFresh(false);
        }));
        if (this._refreshing && refreshItem && refreshItem.setSensitive) {
            refreshItem.setSensitive(false);
        }
        this.menu.addAction(_("Codex Analytics öffnen"), Lang.bind(this, this._openAnalytics));
        this.menu.addAction(_("Einstellungen"), Lang.bind(this, this._openSettings));
    },

    _addDisabled: function(menu, label, styleClasses) {
        let item = new PopupMenu.PopupMenuItem(this._shortText(label, 240), {
            reactive: false
        });
        let classes = String(styleClasses || "").split(/\s+/);
        for (let i = 0; i < classes.length; i++) {
            if (classes[i]) {
                try {
                    item.actor.add_style_class_name(classes[i]);
                } catch (e) {
                    global.log("[" + UUID + "] style class failed: " + String(e));
                }
            }
        }
        menu.addMenuItem(item);
        return item;
    },

    _updatePanel: function() {
        this._clearPanelClasses();
        let selected = [];
        let hasError = false;
        for (let i = 0; i < this._usages.length; i++) {
            let usage = this._usages[i];
            if (["error", "login_required"].indexOf(usage.status) !== -1) {
                hasError = true;
            }
            selected.push({
                usage: usage,
                value: this._selectedPercent(usage)
            });
        }
        let available = selected
            .map(function(item) { return item.value; })
            .filter(function(value) { return value !== null; });
        let worst = available.length ? Math.min.apply(Math, available) : null;
        if (this.showPanelLabel) {
            this.set_applet_label(this._panelLabel(selected, worst));
        } else {
            this.set_applet_label("");
        }
        if (hasError) {
            this.actor.add_style_class_name("codex-usage-panel-error");
        } else if (worst !== null && worst <= 5) {
            this.actor.add_style_class_name("codex-usage-panel-critical");
        } else if (worst !== null && worst <= this._boundedInteger(this.warningThreshold, 0, 100, 20)) {
            this.actor.add_style_class_name("codex-usage-panel-warning");
        }
        let tooltip = this._tooltipText();
        if (this._refreshing) {
            tooltip = _("Aktualisiere …") + (tooltip ? "\n" + tooltip : "");
        }
        this.set_applet_tooltip(tooltip || _("Keine Codex-Nutzungswerte"));
    },

    _panelLabel: function(selected, combinedValue) {
        if (this.panelAccountMode === "per-account") {
            if (!selected.length) {
                return "--";
            }
            return selected.map(Lang.bind(this, function(item) {
                let value = item.value === null ? "--" : Math.round(item.value) + "%";
                return this._accountTag(item.usage.label) + " " + value;
            })).join(" · ");
        }
        return combinedValue === null ? "--" : Math.round(combinedValue) + "%";
    },

    _selectedPercent: function(usage) {
        let five = this._remainingPercent(usage.five_hour);
        let week = this._remainingPercent(usage.weekly);
        if (this.panelPercentSource === "five-hour") {
            return five;
        }
        if (this.panelPercentSource === "weekly") {
            return week;
        }
        let values = [five, week].filter(function(value) { return value !== null; });
        if (!values.length) {
            return null;
        }
        return values.reduce(function(total, value) { return total + value; }, 0) / values.length;
    },

    _accountTag: function(label) {
        let text = String(label || "?").trim();
        let parts = text.split(/[^A-Za-z0-9ÄÖÜäöüß]+/).filter(function(part) {
            return part.length > 0;
        });
        if (parts.length >= 2) {
            return parts.slice(0, 3).map(function(part) {
                return part.slice(0, 1).toUpperCase();
            }).join("");
        }
        if (!parts.length) {
            return "?";
        }
        let word = parts[0];
        return word.slice(0, Math.min(2, word.length));
    },

    _clearPanelClasses: function() {
        for (let i = 0; i < PANEL_CLASSES.length; i++) {
            try {
                this.actor.remove_style_class_name(PANEL_CLASSES[i]);
            } catch (e) {
                global.log("[" + UUID + "] panel style cleanup failed: " + String(e));
            }
        }
    },

    _tooltipText: function() {
        let lines = [];
        for (let i = 0; i < this._usages.length; i++) {
            let usage = this._usages[i];
            lines.push(
                usage.label + ": 5h " + this._windowValue(usage.five_hour) +
                    ", Woche " + this._windowValue(usage.weekly) +
                    (usage.stale ? " (gespeichert)" : "")
            );
        }
        return lines.join("\n");
    },

    _notifyForPayload: function() {
        let threshold = this._boundedInteger(this.warningThreshold, 0, 100, 20);
        let currentWarnings = {};
        let currentErrors = {};
        for (let i = 0; i < this._usages.length; i++) {
            let usage = this._usages[i];
            if (["error", "login_required"].indexOf(usage.status) !== -1) {
                let errorKey = usage.account + ":" + usage.status;
                currentErrors[errorKey] = true;
                if (this.notifyErrors && !this._errorState[errorKey]) {
                    let errorMessage = usage.status === "login_required"
                        ? "Token abgelaufen · codex-usage login " + usage.account
                        : usage.error || this._statusLabel(usage.status);
                    Main.notify(
                        _("Codex Usage: ") + usage.label,
                        errorMessage
                    );
                }
            }
            let windows = [
                ["5h", usage.five_hour],
                ["Woche", usage.weekly]
            ];
            for (let j = 0; j < windows.length; j++) {
                let remaining = this._remainingPercent(windows[j][1]);
                if (remaining !== null && remaining <= threshold) {
                    let warningKey = usage.account + ":" + windows[j][0];
                    currentWarnings[warningKey] = true;
                    if (this.notifyWarnings && !this._warningState[warningKey]) {
                        Main.notify(
                            _("Codex-Limit: ") + usage.label,
                            windows[j][0] + ": " + Math.round(remaining) + _("% verbleibend")
                        );
                    }
                }
            }
        }
        this._warningState = currentWarnings;
        this._errorState = currentErrors;
    },

    _showCommandError: function(message) {
        let text = this._shortText(message || _("Unbekannter Fehler"), 240);
        if (this._usages.length) {
            this._buildUsageMenu();
            this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
            this._addDisabled(this.menu, text, "codex-usage-error");
        } else {
            this.menu.removeAll();
            this._addDisabled(this.menu, _("Codex Usage konnte nicht geladen werden"), "codex-usage-error");
            this._addDisabled(this.menu, text, "codex-usage-detail");
            this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
            this._addActions();
        }
        this._clearPanelClasses();
        this.actor.add_style_class_name("codex-usage-panel-error");
        this.set_applet_tooltip(text);
        if (this.notifyErrors) {
            let key = "command:" + text;
            if (!this._errorState[key]) {
                Main.notify(_("Codex Usage"), text);
                this._errorState[key] = true;
            }
        }
    },

    _windowValue: function(window) {
        let remaining = this._remainingPercent(window);
        if (remaining !== null) {
            return Math.round(remaining) + "%";
        }
        return "–";
    },

    _remainingPercent: function(window) {
        if (!window) {
            return null;
        }
        if (window.remaining !== null) {
            return Math.max(0, Math.min(100, window.remaining));
        }
        if (window.used !== null && window.limit !== null && window.limit > 0) {
            return Math.max(0, Math.min(100, 100 - (window.used / window.limit * 100)));
        }
        if (window.percent !== null) {
            return Math.max(0, Math.min(100, window.percent));
        }
        return null;
    },

    _windowReset: function(window) {
        if (!window || !window.reset_at) {
            return "–";
        }
        return this._formatDate(window.reset_at);
    },

    _usageSeverity: function(usage) {
        if (["error", "login_required"].indexOf(usage.status) !== -1) {
            return "codex-usage-error";
        }
        let five = this._remainingPercent(usage.five_hour);
        let week = this._remainingPercent(usage.weekly);
        let values = [five, week].filter(function(value) { return value !== null; });
        if (!values.length) {
            return usage.stale ? "codex-usage-stale" : "";
        }
        let worst = Math.min.apply(Math, values);
        if (worst <= 5) {
            return "codex-usage-critical";
        }
        if (worst <= this._boundedInteger(this.warningThreshold, 0, 100, 20)) {
            return "codex-usage-warning";
        }
        return usage.stale ? "codex-usage-stale" : "";
    },

    _statusLabel: function(status) {
        let labels = {
            ok: "ok",
            partial: "unvollständig",
            error: "Fehler",
            login_required: "Anmeldung erforderlich",
            blocked: "Limit erreicht"
        };
        return labels[status] || "Fehler";
    },

    _newestCapture: function() {
        let newest = "";
        let newestMs = 0;
        for (let i = 0; i < this._usages.length; i++) {
            let value = this._usages[i].captured_at;
            let millis = this._dateMillis(value);
            if (millis > newestMs) {
                newestMs = millis;
                newest = value;
            }
        }
        return newest;
    },

    _dateMillis: function(value) {
        if (!value) {
            return 0;
        }
        let parsed = Date.parse(value);
        return Number.isFinite(parsed) ? parsed : 0;
    },

    _formatDate: function(value) {
        let millis = this._dateMillis(value);
        if (!millis) {
            return "–";
        }
        let date = new Date(millis);
        let pad = function(number) { return String(number).padStart(2, "0"); };
        return pad(date.getDate()) + "." + pad(date.getMonth() + 1) + "." +
            date.getFullYear() + " " + pad(date.getHours()) + ":" + pad(date.getMinutes());
    },

    _boundedInteger: function(value, minimum, maximum, fallback) {
        let parsed = Number(value);
        if (!Number.isFinite(parsed)) {
            return fallback;
        }
        return Math.max(minimum, Math.min(maximum, Math.round(parsed)));
    },

    _shortText: function(value, limit) {
        let text = String(value || "").replace(/[\u0000-\u001f\u007f]/g, " ").trim();
        if (text.length <= limit) {
            return text;
        }
        return text.slice(0, Math.max(0, limit - 1)) + "…";
    },

    _openAnalytics: function() {
        try {
            Gio.AppInfo.launch_default_for_uri(ANALYTICS_URL, null);
        } catch (e) {
            this._showCommandError(_("Browser konnte nicht geöffnet werden: ") + String(e));
        }
    },

    _openSettings: function() {
        try {
            Gio.Subprocess.new(
                ["xlet-settings", "applet", UUID, "-i", String(this.instanceId)],
                Gio.SubprocessFlags.NONE
            );
        } catch (e) {
            this._showCommandError(_("Einstellungen konnten nicht geöffnet werden: ") + String(e));
        }
    },

    _cancelProcess: function() {
        this._generation += 1;
        if (this._timeoutId) {
            Mainloop.source_remove(this._timeoutId);
            this._timeoutId = 0;
        }
        if (this._process) {
            try {
                this._process.force_exit();
            } catch (e) {
                global.log("[" + UUID + "] process cleanup failed: " + String(e));
            }
            this._process = null;
        }
    },

    on_applet_clicked: function() {
        this.menu.toggle();
        if (this.refreshOnOpen) {
            this._refreshFresh(false);
        }
    },

    on_applet_removed_from_panel: function() {
        this._removed = true;
        if (this._timerId) {
            Mainloop.source_remove(this._timerId);
            this._timerId = 0;
        }
        this._cancelProcess();
        if (this.settings && this.settings.finalize) {
            this.settings.finalize();
        }
        if (this.menu) {
            this.menu.destroy();
        }
    }
};

function main(metadata, orientation, panelHeight, instanceId) {
    return new CodexUsageApplet(metadata, orientation, panelHeight, instanceId);
}
