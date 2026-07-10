const Applet = imports.ui.applet;
const Gio = imports.gi.Gio;
const GLib = imports.gi.GLib;
const Lang = imports.lang;
const Main = imports.ui.main;
const Mainloop = imports.mainloop;
const PopupMenu = imports.ui.popupMenu;
const Settings = imports.ui.settings;
const St = imports.gi.St;

const UUID = "codex-usage@H234598";
const ANALYTICS_URL = "https://chatgpt.com/codex/cloud/settings/analytics";
const MAX_JSON_CHARS = 262144;
const MAX_STDERR_CHARS = 4096;
const MAX_ACCOUNTS = 100;
const MAX_TEXT_CHARS = 500;
const COMMAND_TIMEOUT_MS = 120000;
const AUX_COMMAND_TIMEOUT_MS = 30000;
const REACTIVATION_TIMEOUT_MS = 900000;
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
        this.panelHeight = panelHeight;
        this.commandPath = "codex-usage";
        this.configPath = "";
        this.autoRefresh = true;
        this.pollOwner = "auto";
        this.refreshInterval = 300;
        this.refreshOnOpen = true;
        this.panelAccountMode = "combined";
        this.panelPercentSource = "average";
        this.warningThreshold = 20;
        this.notifyWarnings = false;
        this.notifyErrors = false;
        this.showReactivationActions = true;
        this.reactivationBrowser = "auto";
        this.accountBackends = [];
        this.accountPercentStyles = [];
        this.accountDateStyles = [];
        this.accountTimeStyles = [];
        this.accountStyleTargets = [];

        this._removed = false;
        this._generation = 0;
        this._timerId = 0;
        this._timeoutId = 0;
        this._process = null;
        this._refreshing = false;
        this._usages = [];
        this._warningState = {};
        this._errorState = {};
        this._reactivations = {};
        this._reactivationErrors = {};
        this._auxProcess = null;
        this._auxTimeoutId = 0;
        this._auxGeneration = 0;
        this._backendRowsReady = false;
        this._syncingBackendRows = false;
        this._backendAccounts = {};
        this._syncingStyleRows = false;
        this._percentStyles = {};
        this._dateStyles = {};
        this._timeStyles = {};
        this._styleTargets = {};
        this._systemdActive = false;
        this._serviceChecked = false;

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
        bind("poll-owner", "pollOwner", this._onPollOwnerChanged);
        bind("refresh-interval", "refreshInterval", this._onRefreshSettingsChanged);
        bind("refresh-on-open", "refreshOnOpen", null);
        bind("panel-account-mode", "panelAccountMode", this._updatePanel);
        bind("panel-percent-source", "panelPercentSource", this._updatePanel);
        bind("warning-threshold", "warningThreshold", this._updatePanel);
        bind("notify-warnings", "notifyWarnings", null);
        bind("notify-errors", "notifyErrors", null);
        bind(
            "show-reactivation-actions",
            "showReactivationActions",
            this._rebuildMenu
        );
        bind("reactivation-browser", "reactivationBrowser", null);
        bind("account-backends", "accountBackends", this._onAccountBackendsChanged);
        bind("account-percent-styles", "accountPercentStyles", this._onPercentStylesChanged);
        bind("account-date-styles", "accountDateStyles", this._onDateStylesChanged);
        bind("account-time-styles", "accountTimeStyles", this._onTimeStylesChanged);
        bind("account-style-targets", "accountStyleTargets", this._onStyleTargetsChanged);
    },

    _onCommandSettingsChanged: function() {
        this._loadCached(true);
    },

    _onRefreshSettingsChanged: function() {
        this._scheduleTimer();
    },

    _onPollOwnerChanged: function() {
        this._refreshAuxiliaryState();
        this._scheduleTimer();
    },

    _rebuildMenu: function() {
        this._buildUsageMenu();
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
            if (this._usesAppletPolling()) {
                this._refreshFresh(false);
            } else {
                this._loadCached(false);
            }
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
                if (this._usesAppletPolling()) {
                    this._refreshFresh(false);
                }
            }
            this._refreshAuxiliaryState();
        }));
    },

    _usesAppletPolling: function() {
        if (this.pollOwner === "applet") {
            return true;
        }
        if (this.pollOwner === "systemd") {
            return false;
        }
        return this._serviceChecked && !this._systemdActive;
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

    _baseCommandArgv: function() {
        let argv = [this._resolveCommand()];
        let config = String(this.configPath || "").trim();
        if (config) {
            if (config.length > 1024 || config.indexOf("\u0000") !== -1) {
                throw new Error(_("Ungültiger Config-Pfad"));
            }
            argv.push("--config", config);
        }
        return argv;
    },

    _refreshAuxiliaryState: function() {
        if (this._removed) {
            return;
        }
        this._checkServiceStatus(Lang.bind(this, function() {
            this._loadAccountBackends();
        }));
    },

    _checkServiceStatus: function(callback) {
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            this._serviceChecked = true;
            this._systemdActive = false;
            callback();
            return;
        }
        argv.push("service", "status", "--format", "json");
        this._spawnAuxJson(argv, Lang.bind(this, function(payload) {
            let wasChecked = this._serviceChecked;
            this._serviceChecked = true;
            this._systemdActive = Boolean(payload && payload.enabled && payload.active);
            this._scheduleTimer();
            if (
                !wasChecked &&
                this.pollOwner === "auto" &&
                !this._systemdActive &&
                this.autoRefresh
            ) {
                this._refreshFresh(false);
            }
            callback();
        }));
    },

    _loadAccountBackends: function() {
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            return;
        }
        argv.push("account", "overview", "--format", "json");
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            if (error || !payload || !Array.isArray(payload.accounts)) {
                return;
            }
            let rows = [];
            let accounts = {};
            for (let i = 0; i < payload.accounts.length && i < MAX_ACCOUNTS; i++) {
                let item = payload.accounts[i];
                if (!item || typeof item !== "object" || Array.isArray(item)) {
                    continue;
                }
                let account = this._safeText(item.id, 64);
                let label = this._safeText(item.label, 120);
                let backend = this._safeBackend(item.backend);
                if (!account || !/^[A-Za-z0-9_.-]{1,64}$/.test(account) || !backend) {
                    continue;
                }
                let row = {
                    account: account,
                    label: label || account,
                    backend: backend === "app-server" ? 1 : 0
                };
                rows.push(row);
                accounts[account] = row;
            }
            this._backendAccounts = accounts;
            this._backendRowsReady = true;
            this._syncingBackendRows = true;
            this.accountBackends = rows;
            try {
                this.settings.setValue("account-backends", rows);
            } catch (e) {
                global.log("[" + UUID + "] backend settings sync failed: " + String(e));
            }
            this._syncStyleRows(rows);
            Mainloop.idle_add(Lang.bind(this, function() {
                this._syncingBackendRows = false;
                return false;
            }));
        }));
    },

    _syncStyleRows: function(accounts) {
        let percentRows = this._mergedStyleRows(accounts, this.accountPercentStyles, "percent");
        let dateRows = this._mergedStyleRows(accounts, this.accountDateStyles, "date");
        let timeRows = this._mergedStyleRows(accounts, this.accountTimeStyles, "time");
        let targetRows = this._mergedTargetRows(accounts, this.accountStyleTargets);
        let percentChanged = !this._styleRowsEqual(this.accountPercentStyles, percentRows);
        let dateChanged = !this._styleRowsEqual(this.accountDateStyles, dateRows);
        let timeChanged = !this._styleRowsEqual(this.accountTimeStyles, timeRows);
        let targetsChanged = !this._styleRowsEqual(this.accountStyleTargets, targetRows);
        this._percentStyles = this._styleMap(percentRows);
        this._dateStyles = this._styleMap(dateRows);
        this._timeStyles = this._styleMap(timeRows);
        this._styleTargets = this._targetMap(targetRows);
        this._syncingStyleRows = true;
        this.accountPercentStyles = percentRows;
        this.accountDateStyles = dateRows;
        this.accountTimeStyles = timeRows;
        this.accountStyleTargets = targetRows;
        try {
            if (percentChanged) {
                this.settings.setValue("account-percent-styles", percentRows);
            }
            if (dateChanged) {
                this.settings.setValue("account-date-styles", dateRows);
            }
            if (timeChanged) {
                this.settings.setValue("account-time-styles", timeRows);
            }
            if (targetsChanged) {
                this.settings.setValue("account-style-targets", targetRows);
            }
        } catch (e) {
            global.log("[" + UUID + "] formatting settings sync failed: " + String(e));
        }
        Mainloop.idle_add(Lang.bind(this, function() {
            this._syncingStyleRows = false;
            return false;
        }));
    },

    _styleRowsEqual: function(left, right) {
        if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) {
            return false;
        }
        return JSON.stringify(left) === JSON.stringify(right);
    },

    _mergedStyleRows: function(accounts, currentRows, kind) {
        let current = {};
        if (Array.isArray(currentRows)) {
            for (let i = 0; i < currentRows.length; i++) {
                let account = this._safeText(
                    currentRows[i] && currentRows[i].account,
                    64
                );
                if (!account || current[account] || !this._backendAccounts[account]) {
                    continue;
                }
                let normalized = this._normalizeStyleRow(currentRows[i], account, kind);
                if (normalized) {
                    current[account] = normalized;
                }
            }
        }
        let rows = [];
        for (let i = 0; i < accounts.length; i++) {
            let account = accounts[i].account;
            rows.push(current[account] || this._defaultStyleRow(account, kind));
        }
        return rows;
    },

    _defaultStyleRow: function(account, kind) {
        let row = {
            account: account,
            conditional: false,
            threshold: 20,
            font: 0,
            size: 0,
            bold: false,
            italic: false,
            background: 0
        };
        if (kind !== "percent") {
            row.format = 0;
            return {
                account: row.account,
                format: row.format,
                conditional: row.conditional,
                threshold: row.threshold,
                font: row.font,
                size: row.size,
                bold: row.bold,
                italic: row.italic,
                background: row.background
            };
        }
        return row;
    },

    _normalizeStyleRow: function(row, account, kind) {
        if (!row || typeof row !== "object" || Array.isArray(row)) {
            return null;
        }
        let format = kind === "percent" ? 0 : Number(row.format);
        let conditional = row.conditional === undefined ? false : row.conditional;
        let threshold = row.threshold === undefined ? 20 : Number(row.threshold);
        let font = Number(row.font);
        let size = Number(row.size);
        let background = Number(row.background);
        let maxFormat = kind === "date" ? 3 : 2;
        if (
            (kind !== "percent" && (
                !Number.isInteger(format) || format < 0 || format > maxFormat
            )) ||
            typeof conditional !== "boolean" ||
            !Number.isInteger(threshold) || threshold < 0 || threshold > 100 ||
            !Number.isInteger(font) || font < 0 || font > 3 ||
            !Number.isInteger(size) || size < 0 || size > 48 ||
            !Number.isInteger(background) || background < 0 || background > 6 ||
            typeof row.bold !== "boolean" || typeof row.italic !== "boolean"
        ) {
            return null;
        }
        let normalized = {
            account: account,
            conditional: conditional,
            threshold: threshold,
            font: font,
            size: size,
            bold: row.bold,
            italic: row.italic,
            background: background
        };
        if (kind === "percent") {
            return normalized;
        }
        return {
            account: normalized.account,
            format: format,
            conditional: normalized.conditional,
            threshold: normalized.threshold,
            font: normalized.font,
            size: normalized.size,
            bold: normalized.bold,
            italic: normalized.italic,
            background: normalized.background
        };
    },

    _styleMap: function(rows) {
        let result = {};
        for (let i = 0; i < rows.length; i++) {
            result[rows[i].account] = rows[i];
        }
        return result;
    },

    _mergedTargetRows: function(accounts, currentRows) {
        let current = {};
        if (Array.isArray(currentRows)) {
            for (let i = 0; i < currentRows.length; i++) {
                let account = this._safeText(currentRows[i] && currentRows[i].account, 64);
                let element = Number(currentRows[i] && currentRows[i].element);
                let key = account + ":" + element;
                if (!account || current[key] || !this._backendAccounts[account]) {
                    continue;
                }
                let normalized = this._normalizeTargetRow(currentRows[i], account);
                if (normalized) {
                    current[key] = normalized;
                }
            }
        }
        let rows = [];
        for (let i = 0; i < accounts.length; i++) {
            for (let element = 0; element < 3; element++) {
                let key = accounts[i].account + ":" + element;
                rows.push(current[key] || this._defaultTargetRow(accounts[i].account, element));
            }
        }
        return rows;
    },

    _defaultTargetRow: function(account, element) {
        let isPercent = element === 0;
        return {
            account: account,
            element: element,
            panel: isPercent,
            hover: isPercent,
            click: true
        };
    },

    _normalizeTargetRow: function(row, account) {
        if (!row || typeof row !== "object" || Array.isArray(row)) {
            return null;
        }
        let element = Number(row.element);
        if (
            !Number.isInteger(element) || element < 0 || element > 2 ||
            typeof row.panel !== "boolean" || typeof row.hover !== "boolean" ||
            typeof row.click !== "boolean"
        ) {
            return null;
        }
        return {
            account: account,
            element: element,
            panel: row.panel,
            hover: row.hover,
            click: row.click
        };
    },

    _targetMap: function(rows) {
        let result = {};
        for (let i = 0; i < rows.length; i++) {
            result[rows[i].account + ":" + rows[i].element] = rows[i];
        }
        return result;
    },

    _onPercentStylesChanged: function() {
        this._onStyleRowsChanged("percent");
    },

    _onDateStylesChanged: function() {
        this._onStyleRowsChanged("date");
    },

    _onTimeStylesChanged: function() {
        this._onStyleRowsChanged("time");
    },

    _onStyleRowsChanged: function(kind) {
        if (!this._backendRowsReady || this._syncingStyleRows || this._removed) {
            return;
        }
        let rows = kind === "percent"
            ? this.accountPercentStyles
            : (kind === "date" ? this.accountDateStyles : this.accountTimeStyles);
        let expected = Object.keys(this._backendAccounts).length;
        if (!Array.isArray(rows) || rows.length !== expected) {
            this._loadAccountBackends();
            return;
        }
        let normalized = [];
        let seen = {};
        for (let i = 0; i < rows.length; i++) {
            let account = this._safeText(rows[i] && rows[i].account, 64);
            if (!account || seen[account] || !this._backendAccounts[account]) {
                this._loadAccountBackends();
                return;
            }
            let item = this._normalizeStyleRow(rows[i], account, kind);
            if (!item) {
                this._loadAccountBackends();
                return;
            }
            seen[account] = true;
            normalized.push(item);
        }
        if (kind === "percent") {
            this._percentStyles = this._styleMap(normalized);
        } else if (kind === "date") {
            this._dateStyles = this._styleMap(normalized);
        } else {
            this._timeStyles = this._styleMap(normalized);
        }
        this._refreshFormattedSurfaces();
    },

    _onStyleTargetsChanged: function() {
        if (!this._backendRowsReady || this._syncingStyleRows || this._removed) {
            return;
        }
        let rows = this.accountStyleTargets;
        let expected = Object.keys(this._backendAccounts).length * 3;
        if (!Array.isArray(rows) || rows.length !== expected) {
            this._loadAccountBackends();
            return;
        }
        let normalized = [];
        let seen = {};
        for (let i = 0; i < rows.length; i++) {
            let account = this._safeText(rows[i] && rows[i].account, 64);
            let item = this._normalizeTargetRow(rows[i], account);
            let key = item ? account + ":" + item.element : "";
            if (!item || seen[key] || !this._backendAccounts[account]) {
                this._loadAccountBackends();
                return;
            }
            seen[key] = true;
            normalized.push(item);
        }
        this._styleTargets = this._targetMap(normalized);
        this._refreshFormattedSurfaces();
    },

    _refreshFormattedSurfaces: function() {
        this._buildUsageMenu();
        this._updatePanel();
    },

    _onAccountBackendsChanged: function() {
        if (!this._backendRowsReady || this._syncingBackendRows || this._removed) {
            return;
        }
        let rows = this.accountBackends;
        if (!Array.isArray(rows) || rows.length !== Object.keys(this._backendAccounts).length) {
            this._loadAccountBackends();
            return;
        }
        let changed = null;
        for (let i = 0; i < rows.length; i++) {
            let row = rows[i];
            if (!row || typeof row !== "object" || Array.isArray(row)) {
                this._loadAccountBackends();
                return;
            }
            let account = this._safeText(row.account, 64);
            let canonical = this._backendAccounts[account];
            if (!canonical || this._safeText(row.label, 120) !== canonical.label) {
                this._loadAccountBackends();
                return;
            }
            let backendValue = Number(row.backend);
            if (backendValue !== 0 && backendValue !== 1) {
                this._loadAccountBackends();
                return;
            }
            if (backendValue !== canonical.backend && !changed) {
                changed = {
                    account: account,
                    backend: backendValue === 1 ? "app-server" : "direct"
                };
            }
        }
        if (!changed) {
            return;
        }
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            this._loadAccountBackends();
            return;
        }
        argv.push(
            "account",
            "backend",
            changed.account,
            changed.backend,
            "--format",
            "json"
        );
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            if (error || !payload || payload.ok !== true || payload.account !== changed.account) {
                this._showCommandError(error || _("Abrufweg konnte nicht gespeichert werden"));
            } else {
                this._refreshFresh(false);
            }
            this._loadAccountBackends();
        }));
    },

    _enableBackgroundService: function() {
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            this._showCommandError(String(e));
            return;
        }
        argv.push("service", "enable", "--format", "json");
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            if (error || !payload || !payload.enabled || !payload.active) {
                this._showCommandError(error || _("systemd-Timer konnte nicht aktiviert werden"));
                return;
            }
            this._serviceChecked = true;
            this._systemdActive = true;
            this._scheduleTimer();
            this._buildUsageMenu();
        }));
    },

    _spawnAuxJson: function(argv, callback) {
        this._cancelAuxProcess();
        let generation = ++this._auxGeneration;
        let process = null;
        let done = false;
        let finish = Lang.bind(this, function(payload, error) {
            if (done) {
                return;
            }
            done = true;
            if (this._auxTimeoutId) {
                Mainloop.source_remove(this._auxTimeoutId);
                this._auxTimeoutId = 0;
            }
            if (this._removed || generation !== this._auxGeneration) {
                return;
            }
            this._auxProcess = null;
            callback(payload, error);
        });
        try {
            let launcher = Gio.SubprocessLauncher.new(
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
            );
            launcher.setenv("PYTHONUNBUFFERED", "1", true);
            process = launcher.spawnv(argv);
            this._auxProcess = process;
            this._auxTimeoutId = Mainloop.timeout_add(
                AUX_COMMAND_TIMEOUT_MS,
                Lang.bind(this, function() {
                    try {
                        process.force_exit();
                    } catch (e) {
                        global.log("[" + UUID + "] auxiliary cleanup failed: " + String(e));
                    }
                    finish(null, _("Hilfsbefehl nach 30 Sekunden abgebrochen"));
                    return false;
                })
            );
            process.communicate_utf8_async(null, null, Lang.bind(this, function(proc, result) {
                let stdout = "";
                let stderr = "";
                try {
                    let response = proc.communicate_utf8_finish(result);
                    stdout = String(response[1] || "");
                    stderr = String(response[2] || "");
                } catch (e) {
                    finish(null, _("Hilfsprozessfehler: ") + String(e));
                    return;
                }
                if (!stdout.trim() || stdout.length > MAX_JSON_CHARS) {
                    finish(null, this._shortText(stderr || _("Ungültige Hilfsausgabe"), 240));
                    return;
                }
                try {
                    let payload = JSON.parse(stdout);
                    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
                        throw new Error("invalid auxiliary result");
                    }
                    finish(payload, null);
                } catch (e) {
                    finish(null, this._shortText(stderr || _("Ungültige Hilfsausgabe"), 240));
                }
            }));
        } catch (e) {
            finish(null, _("Hilfsbefehl konnte nicht gestartet werden: ") + String(e));
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
                backend_configured: this._safeBackend(item.backend_configured),
                backend_used: this._safeBackend(item.backend_used, true),
                fallback_reason: this._safeText(item.fallback_reason, MAX_TEXT_CHARS),
                values_captured_at: this._safeText(item.values_captured_at, 80),
                stale: item.stale === true
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

    _safeBackend: function(value, allowBrowser) {
        let backend = this._safeText(value, 32);
        let allowed = allowBrowser
            ? ["direct", "app-server", "browser"]
            : ["direct", "app-server"];
        return allowed.indexOf(backend) !== -1 ? backend : "";
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
        let five = this._percentParts(usage.five_hour, usage.account, "click");
        let week = this._percentParts(usage.weekly, usage.account, "click");
        let severity = this._usageSeverity(usage);
        let summary = usage.label + "     5h " + five.plain + "     Woche " + week.plain;
        let summaryMarkup = this._escapeMarkup(usage.label + "     5h ") + five.markup +
            this._escapeMarkup("     Woche ") + week.markup;
        let summaryItem = this._addDisabled(
            this.menu,
            summary,
            "codex-usage-account " + severity
        );
        this._setItemMarkup(summaryItem, summaryMarkup);
        this._addResetDetail(usage);
        let status = this._statusLabel(usage.status);
        if (usage.stale) {
            status += " · gespeichert vom " + this._formatDate(
                usage.values_captured_at || usage.captured_at
            );
        }
        let detail = usage.status === "login_required"
            ? "Token abgelaufen · codex-usage reactivate " + usage.account
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
        if (usage.status === "login_required" && this.showReactivationActions) {
            this._addReactivationAction(usage);
        }
    },

    _addResetDetail: function(usage) {
        let five = this._windowResetParts(usage.five_hour, usage.account, "click", true);
        let week = this._windowResetParts(usage.weekly, usage.account, "click", true);
        let backend = this._backendSummary(usage);
        let plain = "5h Reset " + five.plain +
            "     Woche Reset " + week.plain +
            "     Abruf " + backend;
        let markup = this._escapeMarkup("5h Reset ") + five.markup +
            this._escapeMarkup("     Woche Reset ") + week.markup +
            this._escapeMarkup("     Abruf " + backend);
        let item = this._addDisabled(this.menu, plain, "codex-usage-detail");
        this._setItemMarkup(item, markup);
    },

    _backendSummary: function(usage) {
        let configured = usage.backend_configured || "direct";
        let used = usage.backend_used || configured;
        let labels = {
            "direct": "Direkt",
            "app-server": "App Server",
            "browser": "Browser"
        };
        let text = labels[used] || used;
        if (used !== configured) {
            text = (labels[configured] || configured) + " → " + text;
        }
        return text;
    },

    _addReactivationAction: function(usage) {
        let running = Boolean(this._reactivations[usage.account]);
        if (running) {
            this._addDisabled(
                this.menu,
                usage.label + ": Login läuft im isolierten Browser …",
                "codex-usage-warning"
            );
            return;
        }
        let item = new PopupMenu.PopupIconMenuItem(
            usage.label + " reaktivieren",
            "system-log-in-symbolic",
            St.IconType.SYMBOLIC
        );
        item.connect("activate", Lang.bind(this, function() {
            this._reactivateAccount(usage);
        }));
        this.menu.addMenuItem(item);
        if (this._reactivationErrors[usage.account]) {
            this._addDisabled(
                this.menu,
                this._shortText(this._reactivationErrors[usage.account], 140),
                "codex-usage-error"
            );
        }
    },

    _reactivateAccount: function(usage) {
        if (this._reactivations[usage.account] || this._removed) {
            return;
        }
        let executable;
        try {
            executable = this._resolveCommand();
        } catch (e) {
            this._reactivationErrors[usage.account] = String(e);
            this._buildUsageMenu();
            return;
        }
        let argv = [executable];
        let config = String(this.configPath || "").trim();
        if (config) {
            if (config.length > 1024 || config.indexOf("\u0000") !== -1) {
                this._reactivationErrors[usage.account] = _("Ungültiger Config-Pfad");
                this._buildUsageMenu();
                return;
            }
            argv.push("--config", config);
        }
        argv.push(
            "reactivate",
            usage.account,
            "--browser",
            this.reactivationBrowser || "auto",
            "--format",
            "json"
        );
        this._spawnReactivation(usage, argv);
    },

    _spawnReactivation: function(usage, argv) {
        let record = { process: null, timeoutId: 0, done: false };
        this._reactivations[usage.account] = record;
        delete this._reactivationErrors[usage.account];
        this._buildUsageMenu();
        let finish = Lang.bind(this, function(payload, error) {
            if (record.done) {
                return;
            }
            record.done = true;
            if (record.timeoutId) {
                Mainloop.source_remove(record.timeoutId);
                record.timeoutId = 0;
            }
            delete this._reactivations[usage.account];
            if (this._removed) {
                return;
            }
            if (error || !payload || payload.ok !== true || payload.account !== usage.account) {
                this._reactivationErrors[usage.account] = this._shortText(
                    error || (payload && payload.error) || _("Reaktivierung fehlgeschlagen"),
                    240
                );
                this._buildUsageMenu();
                return;
            }
            delete this._reactivationErrors[usage.account];
            this._refreshFresh(false);
        });
        try {
            let launcher = Gio.SubprocessLauncher.new(
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
            );
            launcher.setenv("PYTHONUNBUFFERED", "1", true);
            record.process = launcher.spawnv(argv);
            record.timeoutId = Mainloop.timeout_add(
                REACTIVATION_TIMEOUT_MS,
                Lang.bind(this, function() {
                    try {
                        record.process.force_exit();
                    } catch (e) {
                        global.log("[" + UUID + "] reactivation cleanup failed: " + String(e));
                    }
                    finish(null, _("Login nach 15 Minuten abgebrochen"));
                    return false;
                })
            );
            record.process.communicate_utf8_async(
                null,
                null,
                Lang.bind(this, function(proc, result) {
                    let stdout = "";
                    let stderr = "";
                    try {
                        let response = proc.communicate_utf8_finish(result);
                        stdout = String(response[1] || "");
                        stderr = String(response[2] || "");
                    } catch (e) {
                        finish(null, _("Login-Prozessfehler: ") + String(e));
                        return;
                    }
                    if (stdout.length > MAX_JSON_CHARS) {
                        finish(null, _("Login-Ausgabe ist zu groß"));
                        return;
                    }
                    try {
                        let payload = JSON.parse(stdout);
                        if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
                            throw new Error("invalid login result");
                        }
                        finish(payload, null);
                    } catch (e) {
                        finish(
                            null,
                            this._shortText(stderr || _("Ungültige Login-Ausgabe"), 240)
                        );
                    }
                })
            );
        } catch (e) {
            finish(null, _("Login konnte nicht gestartet werden: ") + String(e));
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
        if (this.pollOwner === "systemd" && this._serviceChecked && !this._systemdActive) {
            this.menu.addAction(
                _("Hintergrunddienst aktivieren"),
                Lang.bind(this, this._enableBackgroundService)
            );
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

    _setItemMarkup: function(item, markup) {
        try {
            let text = item && item.label && (
                item.label.clutter_text || item.label.get_clutter_text()
            );
            if (text && text.set_markup) {
                text.set_markup(markup);
            }
        } catch (e) {
            global.log("[" + UUID + "] menu markup failed: " + String(e));
        }
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
        let panel = this._panelContent(selected, worst);
        this.set_applet_label(panel.plain);
        this._setPanelMarkup(panel.markup);
        if (hasError) {
            this.actor.add_style_class_name("codex-usage-panel-error");
        } else if (worst !== null && worst <= 5) {
            this.actor.add_style_class_name("codex-usage-panel-critical");
        } else if (worst !== null && worst <= this._boundedInteger(this.warningThreshold, 0, 100, 20)) {
            this.actor.add_style_class_name("codex-usage-panel-warning");
        }
        let tooltip = this._tooltipContent();
        if (this._refreshing) {
            let prefix = _("Aktualisiere …");
            tooltip = {
                plain: prefix + (tooltip.plain ? "\n" + tooltip.plain : ""),
                markup: this._escapeMarkup(prefix) +
                    (tooltip.markup ? "\n" + tooltip.markup : "")
            };
        }
        let emptyTooltip = _("Keine Codex-Nutzungswerte");
        this.set_applet_tooltip(
            tooltip.markup || this._escapeMarkup(emptyTooltip),
            true
        );
    },

    _setPanelMarkup: function(markup) {
        try {
            if (this._applet_label && this._applet_label.clutter_text) {
                this._applet_label.clutter_text.set_markup(markup);
            }
        } catch (e) {
            global.log("[" + UUID + "] panel markup failed: " + String(e));
        }
    },

    _panelContent: function(selected, combinedValue) {
        if (this.panelAccountMode === "per-account") {
            if (!selected.length) {
                return { plain: "--", markup: "--" };
            }
            let parts = selected.map(Lang.bind(this, function(item) {
                return this._panelAccountContent(item);
            }));
            return {
                plain: parts.map(function(part) { return part.plain; }).join(" · "),
                markup: parts.map(function(part) { return part.markup; }).join(" · ")
            };
        }
        if (combinedValue === null) {
            return { plain: "--", markup: "--" };
        }
        let selectedItem = null;
        for (let i = 0; i < selected.length; i++) {
            if (selected[i].value !== null && selected[i].value === combinedValue) {
                selectedItem = selected[i];
                break;
            }
        }
        if (!selectedItem) {
            let fallback = Math.round(combinedValue) + "%";
            return { plain: fallback, markup: this._escapeMarkup(fallback) };
        }
        let percent = this._percentPartsFromValue(
            selectedItem.value,
            selectedItem.usage.account,
            "panel"
        );
        let reset = this._panelResetParts(selectedItem.usage);
        return {
            plain: percent.plain + (reset.plain ? " " + reset.plain : ""),
            markup: percent.markup + (reset.markup ? " " + reset.markup : "")
        };
    },

    _panelAccountContent: function(item) {
        let tag = this._accountTag(item.usage.label);
        let percent = this._percentPartsFromValue(item.value, item.usage.account, "panel");
        let reset = this._panelResetParts(item.usage);
        return {
            plain: tag + " " + percent.plain + (reset.plain ? " " + reset.plain : ""),
            markup: this._escapeMarkup(tag + " ") + percent.markup +
                (reset.markup ? " " + reset.markup : "")
        };
    },

    _panelResetParts: function(usage) {
        return this._windowResetParts(
            this._selectedWindow(usage),
            usage.account,
            "panel",
            false
        );
    },

    _selectedWindow: function(usage) {
        if (this.panelPercentSource === "five-hour") {
            return usage.five_hour;
        }
        if (this.panelPercentSource === "weekly") {
            return usage.weekly;
        }
        let candidates = [usage.five_hour, usage.weekly].filter(Lang.bind(this, function(window) {
            return this._remainingPercent(window) !== null;
        }));
        if (!candidates.length) {
            return null;
        }
        candidates.sort(Lang.bind(this, function(left, right) {
            return this._remainingPercent(left) - this._remainingPercent(right);
        }));
        return candidates[0];
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

    _tooltipContent: function() {
        let plainLines = [];
        let markupLines = [];
        for (let i = 0; i < this._usages.length; i++) {
            let usage = this._usages[i];
            let five = this._percentParts(usage.five_hour, usage.account, "hover");
            let week = this._percentParts(usage.weekly, usage.account, "hover");
            let stale = usage.stale ? " (gespeichert)" : "";
            plainLines.push(
                usage.label + ": 5h " + five.plain + ", Woche " + week.plain + stale
            );
            markupLines.push(
                this._escapeMarkup(usage.label + ": 5h ") + five.markup +
                    this._escapeMarkup(", Woche ") + week.markup +
                    this._escapeMarkup(stale)
            );
            let fiveReset = this._windowResetParts(
                usage.five_hour,
                usage.account,
                "hover",
                false
            );
            let weekReset = this._windowResetParts(
                usage.weekly,
                usage.account,
                "hover",
                false
            );
            if (fiveReset.plain || weekReset.plain) {
                let resetPlain = "  Reset 5h " + (fiveReset.plain || "–") +
                    ", Woche " + (weekReset.plain || "–");
                let resetMarkup = this._escapeMarkup("  Reset 5h ") +
                    (fiveReset.markup || "–") + this._escapeMarkup(", Woche ") +
                    (weekReset.markup || "–");
                plainLines.push(resetPlain);
                markupLines.push(resetMarkup);
            }
        }
        return {
            plain: plainLines.join("\n"),
            markup: markupLines.join("\n")
        };
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
                        ? "Token abgelaufen · codex-usage reactivate " + usage.account
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
        return remaining === null ? "–" : Math.round(remaining) + "%";
    },

    _percentParts: function(window, account, surface) {
        return this._percentPartsFromValue(this._remainingPercent(window), account, surface);
    },

    _percentPartsFromValue: function(value, account, surface) {
        let plain = value === null || !Number.isFinite(value)
            ? "–"
            : Math.round(value) + "%";
        let style = this._percentStyles[account] || this._defaultStyleRow(account, "percent");
        let markup = this._targetEnabled(account, "percent", surface)
            ? this._styleSpan(plain, style, value, surface)
            : this._escapeMarkup(plain);
        return { plain: plain, markup: markup };
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

    _windowResetParts: function(window, account, surface, includeUnselected) {
        let showDate = includeUnselected || this._targetEnabled(account, "date", surface);
        let showTime = includeUnselected || this._targetEnabled(account, "time", surface);
        if (!showDate && !showTime) {
            return { plain: "", markup: "" };
        }
        if (!window || !window.reset_at) {
            return { plain: "–", markup: this._escapeMarkup("–") };
        }
        let millis = this._dateMillis(window.reset_at);
        if (!millis) {
            return { plain: "–", markup: this._escapeMarkup("–") };
        }
        let date = new Date(millis);
        let dateStyle = this._dateStyles[account] || this._defaultStyleRow(account, "date");
        let timeStyle = this._timeStyles[account] || this._defaultStyleRow(account, "time");
        let remaining = this._remainingPercent(window);
        let dateText = this._formatDatePart(date, dateStyle.format);
        let timeText = this._formatTimePart(date, timeStyle.format);
        let plainParts = [];
        let markupParts = [];
        if (showDate) {
            plainParts.push(dateText);
            markupParts.push(this._targetEnabled(account, "date", surface)
                ? this._styleSpan(dateText, dateStyle, remaining, surface)
                : this._escapeMarkup(dateText));
        }
        if (showTime) {
            plainParts.push(timeText);
            markupParts.push(this._targetEnabled(account, "time", surface)
                ? this._styleSpan(timeText, timeStyle, remaining, surface)
                : this._escapeMarkup(timeText));
        }
        return {
            plain: plainParts.join(" "),
            markup: markupParts.join(" ")
        };
    },

    _targetEnabled: function(account, element, surface) {
        let elements = { percent: 0, date: 1, time: 2 };
        let elementId = elements[element];
        let target = this._styleTargets[account + ":" + elementId];
        if (!target) {
            return element === "percent" || surface === "click";
        }
        return surface === "panel"
            ? target.panel
            : (surface === "hover" ? target.hover : target.click);
    },

    _formatDatePart: function(date, format) {
        let pad = function(number) { return String(number).padStart(2, "0"); };
        let day = pad(date.getDate());
        let month = pad(date.getMonth() + 1);
        let year = date.getFullYear();
        if (format === 1) {
            return year + "-" + month + "-" + day;
        }
        if (format === 2) {
            return day + "." + month + "." + pad(year % 100);
        }
        if (format === 3) {
            let months = [
                "Januar", "Februar", "März", "April", "Mai", "Juni",
                "Juli", "August", "September", "Oktober", "November", "Dezember"
            ];
            return Number(day) + ". " + months[date.getMonth()] + " " + year;
        }
        return day + "." + month + "." + year;
    },

    _formatTimePart: function(date, format) {
        let pad = function(number) { return String(number).padStart(2, "0"); };
        let hours = date.getHours();
        let minutes = pad(date.getMinutes());
        if (format === 1) {
            return pad(hours) + ":" + minutes + ":" + pad(date.getSeconds());
        }
        if (format === 2) {
            let suffix = hours >= 12 ? "PM" : "AM";
            let twelveHour = hours % 12 || 12;
            return pad(twelveHour) + ":" + minutes + " " + suffix;
        }
        return pad(hours) + ":" + minutes;
    },

    _styleSpan: function(text, style, remaining, surface) {
        let escaped = this._escapeMarkup(text);
        if (!this._styleIsActive(style, remaining)) {
            return escaped;
        }
        let attrs = [];
        let fonts = [null, "Sans", "Serif", "Monospace"];
        let font = fonts[style.font] || null;
        if (font) {
            attrs.push('font_family="' + font + '"');
        }
        if (style.size > 0) {
            let maximum = surface === "panel"
                ? Math.max(8, Math.floor(this.panelHeight * 0.55))
                : 48;
            let size = Math.max(6, Math.min(maximum, style.size));
            attrs.push('size="' + size + 'pt"');
        }
        if (style.bold) {
            attrs.push('weight="bold"');
        }
        if (style.italic) {
            attrs.push('style="italic"');
        }
        let backgrounds = [
            null,
            { background: "#202020", foreground: "#ffffff" },
            { background: "#f5f5f5", foreground: "#111111" },
            { background: "#b91c1c", foreground: "#ffffff" },
            { background: "#15803d", foreground: "#ffffff" },
            { background: "#1d4ed8", foreground: "#ffffff" },
            { background: "#facc15", foreground: "#111111" }
        ];
        let colors = backgrounds[style.background] || null;
        if (colors) {
            attrs.push('background="' + colors.background + '"');
            attrs.push('foreground="' + colors.foreground + '"');
        }
        return attrs.length ? "<span " + attrs.join(" ") + ">" + escaped + "</span>" : escaped;
    },

    _styleIsActive: function(style, remaining) {
        if (!style.conditional) {
            return true;
        }
        return remaining !== null && Number.isFinite(remaining) &&
            remaining < style.threshold;
    },

    _escapeMarkup: function(value) {
        return String(value || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
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

    _cancelAuxProcess: function() {
        this._auxGeneration += 1;
        if (this._auxTimeoutId) {
            Mainloop.source_remove(this._auxTimeoutId);
            this._auxTimeoutId = 0;
        }
        if (this._auxProcess) {
            try {
                this._auxProcess.force_exit();
            } catch (e) {
                global.log("[" + UUID + "] auxiliary process cleanup failed: " + String(e));
            }
            this._auxProcess = null;
        }
    },

    _cancelReactivations: function() {
        let accounts = Object.keys(this._reactivations);
        for (let i = 0; i < accounts.length; i++) {
            let record = this._reactivations[accounts[i]];
            if (record.timeoutId) {
                Mainloop.source_remove(record.timeoutId);
            }
            if (record.process) {
                try {
                    record.process.force_exit();
                } catch (e) {
                    global.log("[" + UUID + "] reactivation process cleanup failed: " + String(e));
                }
            }
        }
        this._reactivations = {};
    },

    on_applet_clicked: function() {
        this.menu.toggle();
        if (this.refreshOnOpen) {
            if (this._usesAppletPolling()) {
                this._refreshFresh(false);
            } else {
                this._loadCached(false);
            }
        }
    },

    on_applet_removed_from_panel: function() {
        this._removed = true;
        if (this._timerId) {
            Mainloop.source_remove(this._timerId);
            this._timerId = 0;
        }
        this._cancelProcess();
        this._cancelAuxProcess();
        this._cancelReactivations();
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
