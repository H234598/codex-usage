const Applet = imports.ui.applet;
const ByteArray = imports.byteArray;
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
const MAX_DEFERRED_AUX_REQUESTS = 8;
const REACTIVATION_TIMEOUT_MS = 900000;
const CIRCUIT_BREAKER_MS = 900000;
const INTERNAL_FAILURE_WINDOW_MS = 300000;
const INTERNAL_FAILURE_LIMIT = 3;
const REFRESH_FAILURE_LIMIT = 3;
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
        this.panelPercentSource = "average";
        this.panelAccountSeparator = "bar";
        this.warningThreshold = 20;
        this.notifyWarnings = false;
        this.notifyErrors = false;
        this.showReactivationActions = true;
        this.reactivationBrowser = "auto";
        this.accountBackends = [];
        this.accountPanelSettings = [];
        this.accountAlertSettings = [];
        this.accountPercentStyles = [];
        this.accountDateStyles = [];
        this.accountTimeStyles = [];
        this.accountDurationStyles = [];
        this.accountStyleTargets = [];

        this._removed = false;
        this._sources = {};
        this._idleSources = {};
        this._safeMode = false;
        this._safeModeReason = "";
        this._internalFailures = [];
        this._refreshFailures = 0;
        this._circuitOpenUntil = 0;
        this._lastRefreshError = "";
        this._lastGoodPanel = { plain: "--", markup: "--" };
        this._lastGoodTooltip = "";
        this._generation = 0;
        this._primaryRequest = null;
        this._primaryCachePending = false;
        this._primaryCacheRefreshAfter = false;
        this._primaryFreshPending = false;
        this._primaryFreshOpenAfter = false;
        this._timerId = 0;
        this._displayTimerId = 0;
        this._timeoutId = 0;
        this._process = null;
        this._refreshing = false;
        this._usages = [];
        this._warningState = Object.create(null);
        this._errorState = Object.create(null);
        this._reactivations = Object.create(null);
        this._reactivationErrors = Object.create(null);
        this._reactivationRefreshPending = false;
        this._auxProcess = null;
        this._auxCommand = "";
        this._auxTimeoutId = 0;
        this._auxGeneration = 0;
        this._healthProcess = null;
        this._healthTimeoutId = 0;
        this._healthGeneration = 0;
        this._lastHealthReportAt = 0;
        this._backendRowsReady = false;
        this._syncingBackendRows = false;
        this._backendAccounts = Object.create(null);
        this._backendChangeQueue = [];
        this._backendChangeCurrent = null;
        this._backendAuxQueue = [];
        this._syncingAccountSettings = false;
        this._panelSettings = Object.create(null);
        this._alertSettings = Object.create(null);
        this._syncingStyleRows = false;
        this._percentStyles = Object.create(null);
        this._dateStyles = Object.create(null);
        this._timeStyles = Object.create(null);
        this._durationStyles = Object.create(null);
        this._styleTargets = Object.create(null);
        this._systemdActive = false;
        this._serviceChecked = false;
        this._serviceStatus = {};
        this._serviceAutoAttempted = false;
        this._serviceRepairAt = 0;
        this._staleFallbackAt = 0;
        this._staleCheckId = 0;

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

        try {
            this.settings = new Settings.AppletSettings(this, UUID, instanceId);
            this._bindSettings();
        } catch (e) {
            this._enterSafeMode("Settings konnten nicht initialisiert werden");
            return;
        }
        try {
            this._buildLoadingMenu(_("Lade gespeicherte Werte …"));
            this._scheduleTimer();
            this._loadCached(true);
        } catch (e) {
            this._enterSafeMode("Applet-Start fehlgeschlagen");
        }
    },

    _bindSettings: function() {
        let bind = Lang.bind(this, function(key, property, callback) {
            let safeCallback = callback ? Lang.bind(this, function() {
                let args = Array.prototype.slice.call(arguments);
                this._runSafely("settings:" + key, Lang.bind(this, function() {
                    return callback.apply(this, args);
                }));
            }) : null;
            this.settings.bindProperty(
                Settings.BindingDirection.IN,
                key,
                property,
                safeCallback,
                null
            );
        });
        bind("command-path", "commandPath", this._onCommandSettingsChanged);
        bind("config-path", "configPath", this._onCommandSettingsChanged);
        bind("auto-refresh", "autoRefresh", this._onRefreshSettingsChanged);
        bind("poll-owner", "pollOwner", this._onPollOwnerChanged);
        bind("refresh-interval", "refreshInterval", this._onRefreshSettingsChanged);
        bind("refresh-on-open", "refreshOnOpen", null);
        bind("panel-percent-source", "panelPercentSource", this._onPanelDefaultsChanged);
        bind("panel-account-separator", "panelAccountSeparator", this._updatePanel);
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
        bind("account-panel-settings", "accountPanelSettings", this._onPanelSettingsChanged);
        bind("account-alert-settings", "accountAlertSettings", this._onAlertSettingsChanged);
        bind("account-percent-styles", "accountPercentStyles", this._onPercentStylesChanged);
        bind("account-date-styles", "accountDateStyles", this._onDateStylesChanged);
        bind("account-time-styles", "accountTimeStyles", this._onTimeStylesChanged);
        bind("account-duration-styles", "accountDurationStyles", this._onDurationStylesChanged);
        bind("account-style-targets", "accountStyleTargets", this._onStyleTargetsChanged);
    },

    _runSafely: function(context, callback, fallback) {
        if (this._removed) {
            return fallback;
        }
        try {
            return callback();
        } catch (e) {
            let message = this._shortText(e, 240);
            global.log("[" + UUID + "] " + context + " failed: " + message);
            this._recordInternalFailure(context, e);
            return fallback;
        }
    },

    _recordInternalFailure: function(context, error) {
        let now = Date.now();
        this._internalFailures = this._internalFailures.filter(function(timestamp) {
            return now - timestamp < INTERNAL_FAILURE_WINDOW_MS;
        });
        this._internalFailures.push(now);
        this._recordHealthEvent("applet", "internal_error", null, error);
        if (this._internalFailures.length >= INTERNAL_FAILURE_LIMIT && !this._safeMode) {
            this._enterSafeMode(context + ": " + this._shortText(error, 160));
        }
    },

    _recordRefreshSuccess: function() {
        this._refreshFailures = 0;
        this._lastRefreshError = "";
    },

    _recordRefreshFailure: function(error) {
        this._refreshFailures += 1;
        this._lastRefreshError = this._shortText(error || _("Abruf fehlgeschlagen"), 240);
        this._recordHealthEvent("applet", "refresh_error", null, error);
        if (this._refreshFailures >= REFRESH_FAILURE_LIMIT) {
            this._circuitOpenUntil = Date.now() + CIRCUIT_BREAKER_MS;
            this._updatePanel();
        }
    },

    _recordHealthEvent: function(component, event, account, error) {
        let now = Date.now();
        if (this._removed || now - this._lastHealthReportAt < 60000 || this._healthProcess) {
            return;
        }
        this._lastHealthReportAt = now;
        try {
            let argv = this._baseCommandArgv();
            argv.push(
                "health",
                "--record-component",
                component,
                "--record-event",
                event
            );
            if (account && /^[A-Za-z0-9_.-]{1,64}$/.test(account)) {
                argv.push("--account", account);
            }
            if (error) {
                argv.push("--error-class", typeof error === "string" ? "Error" : (error.name || "Error"));
            }
            this._spawnHealthEvent(argv);
        } catch (e) {
            return;
        }
    },

    _spawnHealthEvent: function(argv) {
        let generation = ++this._healthGeneration;
        let done = false;
        let process = null;
        let finish = Lang.bind(this, function() {
            if (done) {
                return;
            }
            done = true;
            if (generation === this._healthGeneration) {
                this._removeSource("_healthTimeoutId");
            }
            if (this._removed || generation !== this._healthGeneration) {
                return;
            }
            this._healthProcess = null;
        });
        try {
            let launcher = Gio.SubprocessLauncher.new(
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
            );
            launcher.setenv("PYTHONUNBUFFERED", "1", true);
            process = launcher.spawnv(argv);
            this._healthProcess = process;
            this._setSource("_healthTimeoutId", Mainloop.timeout_add(5000, Lang.bind(this, function() {
                this._clearSource("_healthTimeoutId");
                try {
                    process.force_exit();
                } catch (e) {
                    global.log("[" + UUID + "] health process cleanup failed: " + this._shortText(e, 180));
                }
                finish();
                return false;
            })));
            this._readBoundedProcessOutput(process, Lang.bind(this, function() {
                finish();
            }));
        } catch (e) {
            finish();
        }
    },

    _circuitOpen: function() {
        return this._circuitOpenUntil > Date.now();
    },

    _enterSafeMode: function(reason) {
        if (this._safeMode || this._removed) {
            return;
        }
        this._safeMode = true;
        this._safeModeReason = this._shortText(reason || _("Interner Appletfehler"), 240);
        this._refreshing = false;
        this._primaryCachePending = false;
        this._primaryCacheRefreshAfter = false;
        this._primaryFreshPending = false;
        this._primaryFreshOpenAfter = false;
        this._reactivationRefreshPending = false;
        this._backendChangeQueue = [];
        this._backendChangeCurrent = null;
        this._backendAuxQueue = [];
        this._cancelProcess();
        this._cancelAuxProcess();
        this._cancelReactivations();
        this._clearPanelClasses();
        try {
            this.actor.add_style_class_name("codex-usage-panel-error");
        } catch (e) {
            global.log("[" + UUID + "] safe mode style failed: " + this._shortText(e, 180));
        }
        try {
            this.set_applet_label(this._lastGoodPanel.plain);
            this._setPanelMarkup(this._lastGoodPanel.markup);
            this.set_applet_tooltip(
                this._escapeMarkup(_("Codex Usage Safe-Modus: ") + this._safeModeReason),
                true
            );
        } catch (e) {
            global.log("[" + UUID + "] safe mode display failed: " + this._shortText(e, 180));
        }
        this._buildSafeMenu();
    },

    _buildSafeMenu: function() {
        if (this._removed || !this.menu) {
            return;
        }
        try {
            this.menu.removeAll();
            this._addDisabled(this.menu, _("Safe-Modus: letzte gültige Werte"), "codex-usage-error");
            if (this._safeModeReason) {
                this._addDisabled(this.menu, this._safeModeReason, "codex-usage-detail");
            }
            this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
            this.menu.addAction(_("Erneut versuchen"), Lang.bind(this, function() {
                this._runSafely("safe retry", Lang.bind(this, function() {
                    this._leaveSafeModeAndRetry();
                }));
            }));
            this._addHealthAction(this.menu);
            this.menu.addAction(_("Codex Analytics öffnen"), Lang.bind(this, function() {
                this._runSafely("safe analytics action", Lang.bind(this, this._openAnalytics));
            }));
            this.menu.addAction(_("Einstellungen"), Lang.bind(this, function() {
                this._runSafely("safe settings action", Lang.bind(this, this._openSettings));
            }));
        } catch (e) {
            global.log("[" + UUID + "] safe menu failed: " + this._shortText(e, 180));
        }
    },

    _leaveSafeModeAndRetry: function() {
        this._safeMode = false;
        this._safeModeReason = "";
        this._internalFailures = [];
        this._refreshFailures = 0;
        this._circuitOpenUntil = 0;
        this._lastRefreshError = "";
        this._refreshAuxiliaryState();
        this._refreshFresh(false);
    },

    _removeSource: function(property) {
        let id = this[property] || 0;
        this[property] = 0;
        delete this._sources[property];
        if (!id) {
            return;
        }
        try {
            Mainloop.source_remove(id);
        } catch (e) {
            global.log("[" + UUID + "] source cleanup failed: " + this._shortText(e, 180));
        }
    },

    _setSource: function(property, id) {
        this[property] = id || 0;
        if (id) {
            this._sources[property] = id;
        } else {
            delete this._sources[property];
        }
        return id;
    },

    _clearSource: function(property) {
        this[property] = 0;
        delete this._sources[property];
    },

    _addIdle: function(callback) {
        let id = 0;
        id = Mainloop.idle_add(Lang.bind(this, function() {
            delete this._idleSources[id];
            if (this._removed) {
                return false;
            }
            this._runSafely("idle callback", callback);
            return false;
        }));
        this._idleSources[id] = true;
        return id;
    },

    _removeIdleSources: function() {
        let ids = Object.keys(this._idleSources);
        this._idleSources = {};
        for (let i = 0; i < ids.length; i++) {
            try {
                Mainloop.source_remove(Number(ids[i]));
            } catch (e) {
                global.log("[" + UUID + "] idle cleanup failed: " + this._shortText(e, 180));
            }
        }
    },

    _onCommandSettingsChanged: function() {
        this._loadCached(true);
    },

    _onRefreshSettingsChanged: function() {
        this._scheduleTimer();
        if (this.autoRefresh && this.pollOwner === "auto") {
            this._refreshAuxiliaryState();
        }
    },

    _onPollOwnerChanged: function() {
        this._refreshAuxiliaryState();
        this._scheduleTimer();
    },

    _rebuildMenu: function() {
        this._buildUsageMenu();
    },

    _scheduleTimer: function() {
        this._removeSource("_timerId");
        this._scheduleDisplayTimer();
        if (!this.autoRefresh || this._removed) {
            return;
        }
        let seconds = this._boundedInteger(this.refreshInterval, 60, 3600, 300);
        this._setSource("_timerId", Mainloop.timeout_add_seconds(seconds, Lang.bind(this, function() {
            if (this._removed) {
                this._clearSource("_timerId");
                return false;
            }
            this._runSafely("refresh timer", Lang.bind(this, function() {
                if (this._usesAppletPolling()) {
                    this._refreshFresh(false);
                } else {
                    this._loadCached(false);
                }
            }));
            return true;
        })));
    },

    _scheduleDisplayTimer: function() {
        this._removeSource("_displayTimerId");
        if (this._removed) {
            return;
        }
        this._setSource("_displayTimerId", Mainloop.timeout_add_seconds(60, Lang.bind(this, function() {
            if (this._removed) {
                this._clearSource("_displayTimerId");
                return false;
            }
            this._runSafely("display timer", Lang.bind(this, function() {
                if (this._safeMode) {
                    return;
                }
                this._updatePanel();
                if (this.menu && this.menu.isOpen) {
                    this._buildUsageMenu();
                }
            }));
            return true;
        })));
    },

    _loadCached: function(refreshAfter) {
        if (this._removed || this._safeMode) {
            return;
        }
        if (this._refreshing || this._primaryRequest) {
            this._primaryCachePending = true;
            this._primaryCacheRefreshAfter = this._primaryCacheRefreshAfter || Boolean(refreshAfter);
            return;
        }
        this._spawnUsageCommand("latest", Lang.bind(this, function(payload, error) {
            if (this._safeMode) {
                return;
            }
            if (payload) {
                this._applyPayload(payload, false);
            } else if (!this._usages.length && error) {
                this._showCommandError(error);
            }
            if (refreshAfter && this.autoRefresh && this._usesAppletPolling()) {
                this._primaryFreshPending = true;
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
        if (this._refreshing || this._removed || this._safeMode) {
            return;
        }
        if (this._primaryRequest) {
            this._primaryFreshPending = true;
            this._primaryFreshOpenAfter = this._primaryFreshOpenAfter || Boolean(openAfter);
            return;
        }
        if (this._circuitOpen()) {
            this._loadCached(false);
            return;
        }
        if (this._circuitOpenUntil && !this._circuitOpen()) {
            this._circuitOpenUntil = 0;
            this._refreshFailures = 0;
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
            let refreshAfterReactivation = this._reactivationRefreshPending;
            this._reactivationRefreshPending = false;
            if (payload) {
                this._recordRefreshSuccess();
                this._applyPayload(payload, true);
            } else {
                this._recordRefreshFailure(error || _("Abruf fehlgeschlagen"));
                this._showCommandError(this._lastRefreshError);
            }
            if (openAfter && !this.menu.isOpen) {
                this.menu.toggle();
            }
            if (refreshAfterReactivation && !this._removed && !this._safeMode) {
                this._refreshFresh(false);
            }
        }));
    },

    _drainPrimaryRequests: function() {
        if (
            this._removed ||
            this._safeMode ||
            this._refreshing ||
            this._primaryRequest
        ) {
            return;
        }
        if (this._primaryCachePending) {
            let refreshAfter = this._primaryCacheRefreshAfter;
            this._primaryCachePending = false;
            this._primaryCacheRefreshAfter = false;
            this._loadCached(refreshAfter);
            return;
        }
        if (this._primaryFreshPending) {
            let openAfter = this._primaryFreshOpenAfter;
            this._primaryFreshPending = false;
            this._primaryFreshOpenAfter = false;
            this._refreshFresh(openAfter);
        }
    },

    _spawnUsageCommand: function(subcommand, callback) {
        let guardedCallback = Lang.bind(this, function(payload, error) {
            try {
                callback(payload, error);
            } finally {
                this._drainPrimaryRequests();
            }
        });
        let executable;
        try {
            executable = this._resolveCommand();
        } catch (e) {
            guardedCallback(null, String(e));
            return;
        }
        let argv = [executable];
        let config = String(this.configPath || "").trim();
        if (config) {
            if (config.length > 1024 || config.indexOf("\u0000") !== -1) {
                guardedCallback(null, _("Ungültiger Config-Pfad"));
                return;
            }
            argv.push("--config", config);
        }
        argv.push(subcommand, "--format", "json");
        let request = { subcommand: subcommand };
        this._spawnJsonArray(argv, guardedCallback, request);
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

    _readBoundedProcessOutput: function(process, callback) {
        let output = { stdout: "", stderr: "" };
        let completed = 0;
        let stopped = false;
        let complete = Lang.bind(this, function(stdout, stderr, error) {
            if (stopped) {
                return;
            }
            stopped = true;
            this._runSafely("bounded output callback", Lang.bind(this, function() {
                callback(stdout, stderr, error);
            }));
        });
        let read = Lang.bind(this, function(name, stream, maximum) {
            if (!stream) {
                completed += 1;
                if (completed === 2) {
                    complete(output.stdout, output.stderr, null);
                }
                return;
            }
            let chunks = [];
            let total = 0;
            let next = Lang.bind(this, function() {
                if (stopped) {
                    return;
                }
                try {
                    stream.read_bytes_async(
                        8192,
                        GLib.PRIORITY_DEFAULT,
                        null,
                        Lang.bind(this, function(source, result) {
                            if (stopped) {
                                return;
                            }
                            try {
                                let bytes = source.read_bytes_finish(result);
                                let size = bytes.get_size();
                                if (size === 0) {
                                    output[name] = chunks.join("");
                                    completed += 1;
                                    if (completed === 2) {
                                        complete(output.stdout, output.stderr, null);
                                    }
                                    return;
                                }
                                total += size;
                                if (total > maximum) {
                                    try {
                                        process.force_exit();
                                    } catch (e) {
                                        global.log("[" + UUID + "] oversized process cleanup failed: " + this._shortText(e, 180));
                                    }
                                    complete(null, null, name === "stdout"
                                        ? _("JSON-Ausgabe ist zu groß")
                                        : _("Fehlerausgabe ist zu groß"));
                                    return;
                                }
                                chunks.push(ByteArray.toString(bytes.get_data()));
                                next();
                            } catch (e) {
                                try {
                                    process.force_exit();
                                } catch (forceError) {
                                    global.log("[" + UUID + "] output process cleanup failed: " + this._shortText(forceError, 180));
                                }
                                complete(null, null, _("Prozessausgabe konnte nicht gelesen werden"));
                            }
                        })
                    );
                } catch (e) {
                    try {
                        process.force_exit();
                    } catch (forceError) {
                        global.log("[" + UUID + "] output process cleanup failed: " + this._shortText(forceError, 180));
                    }
                    complete(null, null, _("Prozessausgabe konnte nicht gelesen werden"));
                }
            });
            next();
        });
        read("stdout", process.get_stdout_pipe(), MAX_JSON_CHARS);
        read("stderr", process.get_stderr_pipe(), MAX_STDERR_CHARS);
    },

    _spawnJsonArray: function(argv, callback, request) {
        this._cancelProcess();
        let generation = ++this._generation;
        this._primaryRequest = request || null;
        let done = false;
        let process = null;
        let finish = Lang.bind(this, function(payload, error) {
            if (done) {
                return;
            }
            done = true;
            if (generation === this._generation) {
                this._removeSource("_timeoutId");
                if (this._primaryRequest === request) {
                    this._primaryRequest = null;
                }
            }
            if (this._removed || generation !== this._generation) {
                return;
            }
            this._process = null;
            this._runSafely("primary callback", Lang.bind(this, function() {
                callback(payload, error);
            }));
        });

        try {
            let launcher = Gio.SubprocessLauncher.new(
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
            );
            launcher.setenv("PYTHONUNBUFFERED", "1", true);
            process = launcher.spawnv(argv);
            this._process = process;
            this._setSource("_timeoutId", Mainloop.timeout_add(COMMAND_TIMEOUT_MS, Lang.bind(this, function() {
                this._clearSource("_timeoutId");
                try {
                    process.force_exit();
                } catch (e) {
                    global.log("[" + UUID + "] force_exit failed: " + String(e));
                }
                finish(null, _("Abruf nach 120 Sekunden abgebrochen"));
                return false;
            })));
            this._readBoundedProcessOutput(process, Lang.bind(this, function(stdout, stderr, outputError) {
                if (outputError) {
                    finish(null, outputError);
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
            let wasChecked = this._serviceChecked;
            this._serviceChecked = true;
            if (!wasChecked) {
                this._serviceStatus = {};
                this._systemdActive = false;
            }
            callback();
            return;
        }
        argv.push("service", "status", "--format", "json");
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            let wasChecked = this._serviceChecked;
            let validStatus = !error && payload && typeof payload === "object" && !Array.isArray(payload);
            this._serviceChecked = true;
            if (validStatus) {
                this._serviceStatus = payload;
                this._systemdActive = Boolean(payload.enabled && payload.active);
            } else if (!wasChecked) {
                this._serviceStatus = {};
                this._systemdActive = false;
            }
            this._scheduleTimer();
            if (
                this.pollOwner === "auto" &&
                !this._systemdActive &&
                this.autoRefresh &&
                !this._serviceAutoAttempted
            ) {
                this._serviceAutoAttempted = true;
                this._enableBackgroundService(callback);
                return;
            } else if (
                !wasChecked &&
                this.pollOwner === "auto" &&
                !this._systemdActive &&
                this.autoRefresh
            ) {
                this._refreshFresh(false);
            } else if (this._systemdActive && this._cacheIsStale()) {
                this._repairStaleService();
            }
            callback();
        }));
    },

    _cacheIsStale: function() {
        if (!this._usages.length) {
            return false;
        }
        let captured = this._dateMillis(this._newestCapture());
        if (!captured) {
            return true;
        }
        let grace = Math.max(60000, this._boundedInteger(this.refreshInterval, 60, 3600, 300) * 2000);
        return Date.now() - captured > grace;
    },

    _repairStaleService: function() {
        let now = Date.now();
        if (now - this._serviceRepairAt < CIRCUIT_BREAKER_MS) {
            return;
        }
        this._serviceRepairAt = now;
        this._enableBackgroundService();
        this._removeSource("_staleCheckId");
        this._setSource("_staleCheckId", Mainloop.timeout_add(60000, Lang.bind(this, function() {
            this._clearSource("_staleCheckId");
            if (this._removed || !this._cacheIsStale()) {
                return false;
            }
            if (Date.now() - this._staleFallbackAt >= CIRCUIT_BREAKER_MS) {
                this._staleFallbackAt = Date.now();
                this._refreshFresh(false);
            }
            return false;
        })));
    },

    _loadAccountBackends: function() {
        if (this._backendChangeCurrent || this._backendChangeQueue.length) {
            return;
        }
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
            let accounts = Object.create(null);
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
                if (Object.prototype.hasOwnProperty.call(accounts, account)) {
                    global.log("[" + UUID + "] duplicate account in backend overview");
                    return;
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
            let usageRowsChanged = this._ensureBackendUsageRows();
            this._syncingBackendRows = true;
            this.accountBackends = rows;
            try {
                this.settings.setValue("account-backends", rows);
            } catch (e) {
                global.log("[" + UUID + "] backend settings sync failed: " + String(e));
            }
            this._syncAccountSettings(rows);
            this._syncStyleRows(rows);
            if (this._usages.length || usageRowsChanged) {
                this._refreshFormattedSurfaces();
            }
            this._addIdle(Lang.bind(this, function() {
                this._syncingBackendRows = false;
                return false;
            }));
        }));
    },

    _ensureBackendUsageRows: function() {
        if (!this._backendRowsReady) {
            return false;
        }
        let known = Object.create(null);
        let filtered = [];
        let changed = false;
        for (let i = 0; i < this._usages.length; i++) {
            let usage = this._usages[i];
            let account = usage && usage.account;
            if (!account || !this._backendAccounts[account] || known[account]) {
                changed = true;
                continue;
            }
            known[account] = true;
            filtered.push(usage);
        }
        let accounts = Object.keys(this._backendAccounts);
        for (let i = 0; i < accounts.length; i++) {
            let account = accounts[i];
            if (known[account]) {
                continue;
            }
            let backend = this._backendAccounts[account].backend === 1
                ? "app-server"
                : "direct";
            filtered.push({
                account: account,
                label: this._backendAccounts[account].label || account,
                captured_at: "",
                five_hour: null,
                weekly: null,
                status: "partial",
                error: _("Noch keine gespeicherten Nutzungswerte"),
                blocked_until: "",
                blocked_reason: "",
                auth_access_expires_at: "",
                backend_configured: backend,
                backend_used: "",
                fallback_reason: "",
                values_captured_at: "",
                stale: true
            });
            changed = true;
        }
        if (changed) {
            this._usages = filtered;
        }
        return changed;
    },

    _syncAccountSettings: function(accounts) {
        let panelRows = this._mergedPanelRows(accounts, this.accountPanelSettings);
        let alertRows = this._mergedAlertRows(accounts, this.accountAlertSettings);
        let panelChanged = !this._styleRowsEqual(this.accountPanelSettings, panelRows);
        let alertChanged = !this._styleRowsEqual(this.accountAlertSettings, alertRows);
        this._panelSettings = this._panelSettingsMap(panelRows);
        this._alertSettings = this._alertSettingsMap(alertRows);
        this._syncingAccountSettings = true;
        this.accountPanelSettings = panelRows;
        this.accountAlertSettings = alertRows;
        try {
            if (panelChanged) {
                this.settings.setValue("account-panel-settings", panelRows);
            }
            if (alertChanged) {
                this.settings.setValue("account-alert-settings", alertRows);
            }
        } catch (e) {
            global.log("[" + UUID + "] account settings sync failed: " + String(e));
        }
        this._addIdle(Lang.bind(this, function() {
            this._syncingAccountSettings = false;
            return false;
        }));
    },

    _mergedPanelRows: function(accounts, currentRows) {
        let current = Object.create(null);
        if (Array.isArray(currentRows)) {
            for (let i = 0; i < currentRows.length; i++) {
                let account = this._safeText(currentRows[i] && currentRows[i].account, 64);
                if (!account || current[account] || !this._backendAccounts[account]) {
                    continue;
                }
                let normalized = this._normalizePanelRow(currentRows[i], account);
                if (normalized) {
                    current[account] = normalized;
                }
            }
        }
        let rows = [];
        for (let i = 0; i < accounts.length; i++) {
            let account = accounts[i].account;
            rows.push(current[account] || this._defaultPanelRow(account, i + 1));
        }
        return rows;
    },

    _defaultPanelRow: function(account, order) {
        return {
            account: account,
            tag: "",
            order: order,
            muted: false,
            slot1: this._panelSourceValue(this.panelPercentSource),
            slot2: 0
        };
    },

    _normalizePanelRow: function(row, account) {
        if (!row || typeof row !== "object" || Array.isArray(row)) {
            return null;
        }
        let tag = this._safeText(row.tag, 8);
        let order = Number(row.order);
        let slot1 = Number(row.slot1);
        let slot2 = Number(row.slot2);
        if (
            !Number.isInteger(order) || order < 1 || order > 100 ||
            typeof row.muted !== "boolean" ||
            !Number.isInteger(slot1) || slot1 < 0 || slot1 > 3 ||
            !Number.isInteger(slot2) || slot2 < 0 || slot2 > 3
        ) {
            return null;
        }
        if (slot1 !== 0 && slot1 === slot2) {
            slot2 = 0;
        }
        return {
            account: account,
            tag: tag,
            order: order,
            muted: row.muted,
            slot1: slot1,
            slot2: slot2
        };
    },

    _mergedAlertRows: function(accounts, currentRows) {
        let current = Object.create(null);
        if (Array.isArray(currentRows)) {
            for (let i = 0; i < currentRows.length; i++) {
                let account = this._safeText(currentRows[i] && currentRows[i].account, 64);
                if (!account || current[account] || !this._backendAccounts[account]) {
                    continue;
                }
                let normalized = this._normalizeAlertRow(currentRows[i], account);
                if (normalized) {
                    current[account] = normalized;
                }
            }
        }
        let rows = [];
        for (let i = 0; i < accounts.length; i++) {
            let account = accounts[i].account;
            rows.push(current[account] || this._defaultAlertRow(account));
        }
        return rows;
    },

    _defaultAlertRow: function(account) {
        let threshold = this._boundedInteger(this.warningThreshold, 0, 100, 20);
        return {
            account: account,
            "five-threshold": threshold,
            "weekly-threshold": threshold,
            warnings: true,
            errors: true
        };
    },

    _normalizeAlertRow: function(row, account) {
        if (!row || typeof row !== "object" || Array.isArray(row)) {
            return null;
        }
        let five = Number(row["five-threshold"]);
        let weekly = Number(row["weekly-threshold"]);
        if (
            !Number.isInteger(five) || five < 0 || five > 100 ||
            !Number.isInteger(weekly) || weekly < 0 || weekly > 100 ||
            typeof row.warnings !== "boolean" || typeof row.errors !== "boolean"
        ) {
            return null;
        }
        return {
            account: account,
            "five-threshold": five,
            "weekly-threshold": weekly,
            warnings: row.warnings,
            errors: row.errors
        };
    },

    _panelSettingsMap: function(rows) {
        let result = Object.create(null);
        for (let i = 0; i < rows.length; i++) {
            result[rows[i].account] = rows[i];
        }
        return result;
    },

    _alertSettingsMap: function(rows) {
        let result = Object.create(null);
        for (let i = 0; i < rows.length; i++) {
            result[rows[i].account] = rows[i];
        }
        return result;
    },

    _onPanelDefaultsChanged: function() {
        if (this._backendRowsReady && !this._syncingAccountSettings) {
            this._syncAccountSettings(Object.keys(this._backendAccounts).map(Lang.bind(this, function(account) {
                return this._backendAccounts[account];
            })));
        }
        this._updatePanel();
    },

    _onPanelSettingsChanged: function() {
        if (!this._backendRowsReady || this._syncingAccountSettings || this._removed) {
            return;
        }
        let expected = Object.keys(this._backendAccounts).length;
        if (!Array.isArray(this.accountPanelSettings) || this.accountPanelSettings.length !== expected) {
            this._loadAccountBackends();
            return;
        }
        let normalized = [];
        let seen = Object.create(null);
        for (let i = 0; i < this.accountPanelSettings.length; i++) {
            let account = this._safeText(this.accountPanelSettings[i] && this.accountPanelSettings[i].account, 64);
            let row = this._normalizePanelRow(this.accountPanelSettings[i], account);
            if (!row || seen[account] || !this._backendAccounts[account]) {
                this._loadAccountBackends();
                return;
            }
            seen[account] = true;
            normalized.push(row);
        }
        this._panelSettings = this._panelSettingsMap(normalized);
        this.accountPanelSettings = normalized;
        this._refreshFormattedSurfaces();
    },

    _onAlertSettingsChanged: function() {
        if (!this._backendRowsReady || this._syncingAccountSettings || this._removed) {
            return;
        }
        let expected = Object.keys(this._backendAccounts).length;
        if (!Array.isArray(this.accountAlertSettings) || this.accountAlertSettings.length !== expected) {
            this._loadAccountBackends();
            return;
        }
        let normalized = [];
        let seen = Object.create(null);
        for (let i = 0; i < this.accountAlertSettings.length; i++) {
            let account = this._safeText(this.accountAlertSettings[i] && this.accountAlertSettings[i].account, 64);
            let row = this._normalizeAlertRow(this.accountAlertSettings[i], account);
            if (!row || seen[account] || !this._backendAccounts[account]) {
                this._loadAccountBackends();
                return;
            }
            seen[account] = true;
            normalized.push(row);
        }
        this._alertSettings = this._alertSettingsMap(normalized);
        this.accountAlertSettings = normalized;
        this._refreshFormattedSurfaces();
    },

    _panelSourceValue: function(source) {
        return {
            "five-hour": 1,
            weekly: 2,
            average: 3
        }[source] || 3;
    },

    _syncStyleRows: function(accounts) {
        let percentRows = this._mergedStyleRows(accounts, this.accountPercentStyles, "percent");
        let dateRows = this._mergedStyleRows(accounts, this.accountDateStyles, "date");
        let timeRows = this._mergedStyleRows(accounts, this.accountTimeStyles, "time");
        let durationRows = this._mergedStyleRows(accounts, this.accountDurationStyles, "duration");
        let targetRows = this._mergedTargetRows(accounts, this.accountStyleTargets);
        let percentChanged = !this._styleRowsEqual(this.accountPercentStyles, percentRows);
        let dateChanged = !this._styleRowsEqual(this.accountDateStyles, dateRows);
        let timeChanged = !this._styleRowsEqual(this.accountTimeStyles, timeRows);
        let durationChanged = !this._styleRowsEqual(this.accountDurationStyles, durationRows);
        let targetsChanged = !this._styleRowsEqual(this.accountStyleTargets, targetRows);
        this._percentStyles = this._styleMap(percentRows);
        this._dateStyles = this._styleMap(dateRows);
        this._timeStyles = this._styleMap(timeRows);
        this._durationStyles = this._styleMap(durationRows);
        this._styleTargets = this._targetMap(targetRows);
        this._syncingStyleRows = true;
        this.accountPercentStyles = percentRows;
        this.accountDateStyles = dateRows;
        this.accountTimeStyles = timeRows;
        this.accountDurationStyles = durationRows;
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
            if (durationChanged) {
                this.settings.setValue("account-duration-styles", durationRows);
            }
            if (targetsChanged) {
                this.settings.setValue("account-style-targets", targetRows);
            }
        } catch (e) {
            global.log("[" + UUID + "] formatting settings sync failed: " + String(e));
        }
        this._addIdle(Lang.bind(this, function() {
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
        let current = Object.create(null);
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
            mode: 0,
            threshold: kind === "duration" ? 120 : 20,
            font: 0,
            size: 0,
            bold: false,
            italic: false,
            color: 0,
            "below-font": 0,
            "below-size": 0,
            "below-bold": true,
            "below-italic": false,
            "below-color": 3,
            "below-background": 0,
            background: 0
        };
        if (kind !== "percent") {
            row.format = 0;
            return {
                account: row.account,
                format: row.format,
                mode: row.mode,
                threshold: row.threshold,
                font: row.font,
                size: row.size,
                bold: row.bold,
                italic: row.italic,
                color: row.color,
                "below-font": row["below-font"],
                "below-size": row["below-size"],
                "below-bold": row["below-bold"],
                "below-italic": row["below-italic"],
                "below-color": row["below-color"],
                "below-background": row["below-background"],
                background: row.background
            };
        }
        return row;
    },

    _normalizeStyleRow: function(row, account, kind) {
        if (!row || typeof row !== "object" || Array.isArray(row)) {
            return null;
        }
        let format = kind === "percent"
            ? 0
            : (row.format === undefined ? 0 : Number(row.format));
        let mode = row.mode === undefined
            ? (row.conditional === true ? 1 : 0)
            : Number(row.mode);
        let threshold = row.threshold === undefined
            ? (kind === "duration" ? 120 : 20)
            : Number(row.threshold);
        let font = row.font === undefined ? 0 : Number(row.font);
        let size = row.size === undefined ? 0 : Number(row.size);
        let bold = row.bold === undefined ? false : row.bold;
        let italic = row.italic === undefined ? false : row.italic;
        let color = row.color === undefined ? 0 : Number(row.color);
        let background = row.background === undefined ? 0 : Number(row.background);
        let belowFont = row["below-font"] === undefined ? 0 : Number(row["below-font"]);
        let belowSize = row["below-size"] === undefined ? 0 : Number(row["below-size"]);
        let belowBold = row["below-bold"] === undefined ? true : row["below-bold"];
        let belowItalic = row["below-italic"] === undefined ? false : row["below-italic"];
        let belowColor = row["below-color"] === undefined ? 3 : Number(row["below-color"]);
        let belowBackground = row["below-background"] === undefined
            ? 0
            : Number(row["below-background"]);
        let maxFormat = kind === "date" ? 3 : (kind === "duration" ? 3 : 2);
        let maxThreshold = kind === "duration" ? 10080 : 100;
        if (
            (kind !== "percent" && (
                !Number.isInteger(format) || format < 0 || format > maxFormat
            )) ||
            !Number.isInteger(mode) || mode < 0 || mode > 3 ||
            !Number.isInteger(threshold) || threshold < 0 || threshold > maxThreshold ||
            !Number.isInteger(font) || font < 0 || font > 3 ||
            !Number.isInteger(size) || size < 0 || size > 48 ||
            !Number.isInteger(color) || color < 0 || color > 7 ||
            !Number.isInteger(background) || background < 0 || background > 6 ||
            typeof bold !== "boolean" || typeof italic !== "boolean" ||
            !Number.isInteger(belowFont) || belowFont < 0 || belowFont > 3 ||
            !Number.isInteger(belowSize) || belowSize < 0 || belowSize > 48 ||
            typeof belowBold !== "boolean" || typeof belowItalic !== "boolean" ||
            !Number.isInteger(belowColor) || belowColor < 0 || belowColor > 7 ||
            !Number.isInteger(belowBackground) || belowBackground < 0 || belowBackground > 6
        ) {
            return null;
        }
        let normalized = {
            account: account,
            mode: mode,
            threshold: threshold,
            font: font,
            size: size,
            bold: bold,
            italic: italic,
            color: color,
            "below-font": belowFont,
            "below-size": belowSize,
            "below-bold": belowBold,
            "below-italic": belowItalic,
            "below-color": belowColor,
            "below-background": belowBackground,
            background: background
        };
        if (kind === "percent") {
            return normalized;
        }
        return {
            account: normalized.account,
            format: format,
            mode: normalized.mode,
            threshold: normalized.threshold,
            font: normalized.font,
            size: normalized.size,
            bold: normalized.bold,
            italic: normalized.italic,
            color: normalized.color,
            "below-font": normalized["below-font"],
            "below-size": normalized["below-size"],
            "below-bold": normalized["below-bold"],
            "below-italic": normalized["below-italic"],
            "below-color": normalized["below-color"],
            "below-background": normalized["below-background"],
            background: normalized.background
        };
    },

    _styleMap: function(rows) {
        let result = Object.create(null);
        for (let i = 0; i < rows.length; i++) {
            result[rows[i].account] = rows[i];
        }
        return result;
    },

    _mergedTargetRows: function(accounts, currentRows) {
        let current = Object.create(null);
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
            for (let element = 0; element < 4; element++) {
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
            !Number.isInteger(element) || element < 0 || element > 3 ||
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
        let result = Object.create(null);
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

    _onDurationStylesChanged: function() {
        this._onStyleRowsChanged("duration");
    },

    _onStyleRowsChanged: function(kind) {
        if (!this._backendRowsReady || this._syncingStyleRows || this._removed) {
            return;
        }
        let rows = kind === "percent"
            ? this.accountPercentStyles
            : (kind === "date"
                ? this.accountDateStyles
                : (kind === "time" ? this.accountTimeStyles : this.accountDurationStyles));
        let expected = Object.keys(this._backendAccounts).length;
        if (!Array.isArray(rows) || rows.length !== expected) {
            this._loadAccountBackends();
            return;
        }
        let normalized = [];
        let seen = Object.create(null);
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
        } else if (kind === "time") {
            this._timeStyles = this._styleMap(normalized);
        } else {
            this._durationStyles = this._styleMap(normalized);
        }
        this._refreshFormattedSurfaces();
    },

    _onStyleTargetsChanged: function() {
        if (!this._backendRowsReady || this._syncingStyleRows || this._removed) {
            return;
        }
        let rows = this.accountStyleTargets;
        let expected = Object.keys(this._backendAccounts).length * 4;
        if (!Array.isArray(rows) || rows.length !== expected) {
            this._loadAccountBackends();
            return;
        }
        let normalized = [];
        let seen = Object.create(null);
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
        if (this._safeMode) {
            this._buildSafeMenu();
            return;
        }
        this._buildUsageMenu();
        this._updatePanel();
    },

    _reconcileBackendChanges: function(rows) {
        let desired = Object.create(null);
        for (let i = 0; i < rows.length; i++) {
            desired[rows[i].account] = rows[i].backend === 1 ? "app-server" : "direct";
        }
        let queue = [];
        let accounts = Object.keys(this._backendAccounts);
        for (let i = 0; i < accounts.length; i++) {
            let account = accounts[i];
            let target = desired[account];
            let current = this._backendChangeCurrent;
            if (current && current.account === account) {
                if (current.backend !== target) {
                    queue.push({ account: account, backend: target });
                }
                continue;
            }
            let canonical = this._backendAccounts[account].backend === 1
                ? "app-server"
                : "direct";
            if (target !== canonical) {
                queue.push({ account: account, backend: target });
            }
        }
        this._backendChangeQueue = queue;
        this._drainBackendChanges();
    },

    _drainBackendChanges: function() {
        if (
            this._removed || this._backendChangeCurrent || this._auxProcess ||
            !this._backendChangeQueue.length
        ) {
            return;
        }
        let changed = this._backendChangeQueue.shift();
        this._backendChangeCurrent = changed;
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            this._backendChangeCurrent = null;
            this._backendChangeQueue = [];
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
            try {
                if (error || !payload || payload.ok !== true || payload.account !== changed.account) {
                    this._showCommandError(error || _("Abrufweg konnte nicht gespeichert werden"));
                } else {
                    this._refreshFresh(false);
                }
            } finally {
                this._backendChangeCurrent = null;
                if (this._backendChangeQueue.length) {
                    this._drainBackendChanges();
                } else {
                    this._loadAccountBackends();
                }
            }
        }), true);
    },

    _drainDeferredAuxRequests: function() {
        if (
            this._removed || this._safeMode || this._backendChangeCurrent ||
            this._backendChangeQueue.length || this._auxProcess ||
            !this._backendAuxQueue.length
        ) {
            return;
        }
        let request = this._backendAuxQueue.shift();
        this._spawnAuxJson(request.argv, request.callback);
    },

    _auxRequestKey: function(argv) {
        let parts = [];
        for (let i = 0; i < argv.length; i++) {
            parts.push(String(argv[i]));
        }
        return parts.join("\u0000");
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
        let desiredRows = [];
        let seen = Object.create(null);
        for (let i = 0; i < rows.length; i++) {
            let row = rows[i];
            if (!row || typeof row !== "object" || Array.isArray(row)) {
                this._loadAccountBackends();
                return;
            }
            let account = this._safeText(row.account, 64);
            let canonical = this._backendAccounts[account];
            if (!account || seen[account] || !canonical || this._safeText(row.label, 120) !== canonical.label) {
                this._loadAccountBackends();
                return;
            }
            seen[account] = true;
            let backendValue = Number(row.backend);
            if (backendValue !== 0 && backendValue !== 1) {
                this._loadAccountBackends();
                return;
            }
            desiredRows.push({ account: account, backend: backendValue });
        }
        this._reconcileBackendChanges(desiredRows);
    },

    _enableBackgroundService: function(after) {
        let continueAfter = Lang.bind(this, function() {
            if (after) {
                this._runSafely("service continuation", after);
            }
        });
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            this._serviceAutoAttempted = false;
            this._showCommandError(String(e));
            continueAfter();
            return;
        }
        argv.push("service", "enable", "--format", "json");
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            if (error || !payload || !payload.enabled || !payload.active) {
                this._serviceAutoAttempted = false;
                this._showCommandError(error || _("systemd-Timer konnte nicht aktiviert werden"));
                if (this.pollOwner === "auto" && this.autoRefresh) {
                    this._systemdActive = false;
                    this._refreshFresh(false);
                }
                continueAfter();
                return;
            }
            this._serviceChecked = true;
            this._systemdActive = true;
            this._serviceStatus = payload;
            this._scheduleTimer();
            this._buildUsageMenu();
            continueAfter();
        }));
    },

    _spawnAuxJson: function(argv, callback, backendRequest) {
        if (
            !backendRequest &&
            (this._backendChangeCurrent || this._backendChangeQueue.length)
        ) {
            let key = this._auxRequestKey(argv);
            for (let i = 0; i < this._backendAuxQueue.length; i++) {
                if (this._backendAuxQueue[i].key === key) {
                    this._backendAuxQueue[i].callback = callback;
                    return;
                }
            }
            if (this._backendAuxQueue.length >= MAX_DEFERRED_AUX_REQUESTS) {
                this._runSafely("auxiliary queue overflow", Lang.bind(this, function() {
                    callback(null, _("Zu viele wartende Hilfsanfragen"));
                }));
                return;
            }
            this._backendAuxQueue.push({ argv: argv, callback: callback, key: key });
            return;
        }
        this._cancelAuxProcess();
        let generation = ++this._auxGeneration;
        let serviceIndex = argv.indexOf("service");
        this._auxCommand = serviceIndex !== -1 && argv[serviceIndex + 1] === "enable"
            ? "service-enable"
            : "";
        let process = null;
        let done = false;
        let finish = Lang.bind(this, function(payload, error) {
            if (done) {
                return;
            }
            done = true;
            if (generation === this._auxGeneration) {
                this._removeSource("_auxTimeoutId");
                this._auxCommand = "";
            }
            if (this._removed || generation !== this._auxGeneration) {
                return;
            }
            this._auxProcess = null;
            this._runSafely("auxiliary callback", Lang.bind(this, function() {
                callback(payload, error);
            }));
            this._drainBackendChanges();
            this._drainDeferredAuxRequests();
        });
        try {
            let launcher = Gio.SubprocessLauncher.new(
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
            );
            launcher.setenv("PYTHONUNBUFFERED", "1", true);
            process = launcher.spawnv(argv);
            this._auxProcess = process;
            this._setSource("_auxTimeoutId", Mainloop.timeout_add(
                AUX_COMMAND_TIMEOUT_MS,
                Lang.bind(this, function() {
                    this._clearSource("_auxTimeoutId");
                    try {
                        process.force_exit();
                    } catch (e) {
                        global.log("[" + UUID + "] auxiliary cleanup failed: " + String(e));
                    }
                    finish(null, _("Hilfsbefehl nach 30 Sekunden abgebrochen"));
                    return false;
                })
            ));
            this._readBoundedProcessOutput(process, Lang.bind(this, function(stdout, stderr, outputError) {
                if (outputError) {
                    finish(null, outputError);
                    return;
                }
                if (!stdout.trim()) {
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
        let seenAccounts = Object.create(null);
        for (let i = 0; i < payload.length; i++) {
            let item = payload[i];
            if (!item || typeof item !== "object" || Array.isArray(item)) {
                throw new Error("invalid account entry");
            }
            let account = this._safeText(item.account, 64);
            if (!account) {
                throw new Error("account id missing");
            }
            if (seenAccounts[account]) {
                throw new Error("duplicate account id");
            }
            seenAccounts[account] = true;
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
        let previous = Object.create(null);
        for (let i = 0; i < this._usages.length; i++) {
            previous[this._usages[i].account] = this._usages[i];
        }
        let merged = [];
        let freshAccounts = Object.create(null);
        for (let j = 0; j < fresh.length; j++) {
            let item = fresh[j];
            freshAccounts[item.account] = true;
            let old = previous[item.account];
            if (old && item.status !== "ok") {
                let hadFreshWindow = Boolean(item.five_hour || item.weekly);
                let usedCachedWindow = false;
                if (!item.five_hour && old.five_hour) {
                    item.five_hour = old.five_hour;
                    usedCachedWindow = true;
                }
                if (!item.weekly && old.weekly) {
                    item.weekly = old.weekly;
                    usedCachedWindow = true;
                }
                if (usedCachedWindow) {
                    item.values_captured_at = item.values_captured_at ||
                        old.values_captured_at || old.captured_at;
                    item.stale = true;
                }
                if (usedCachedWindow && !hadFreshWindow) {
                    item.captured_at = old.captured_at;
                }
                if (usedCachedWindow && !item.captured_at) {
                    item.captured_at = old.captured_at;
                }
            }
            merged.push(item);
        }
        for (let k = 0; k < this._usages.length; k++) {
            let old = this._usages[k];
            if (
                freshAccounts[old.account] ||
                (this._backendRowsReady && !this._backendAccounts[old.account])
            ) {
                continue;
            }
            let stale = {};
            for (let key in old) {
                if (Object.prototype.hasOwnProperty.call(old, key)) {
                    stale[key] = old[key];
                }
            }
            stale.stale = true;
            stale.values_captured_at = stale.values_captured_at || stale.captured_at;
            if (stale.status === "ok") {
                stale.status = "partial";
            }
            merged.push(stale);
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
        if (this._safeMode) {
            this._buildSafeMenu();
            return;
        }
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
        this._addAccountControls(usage);
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

    _addAccountControls: function(usage) {
        let panel = this._panelSettings[usage.account] || this._defaultPanelRow(usage.account, 1);
        let alert = this._alertSettings[usage.account] || this._defaultAlertRow(usage.account);
        let submenu = new PopupMenu.PopupSubMenuMenuItem(usage.label + " steuern");
        let visible = new PopupMenu.PopupSwitchMenuItem("Statusleiste anzeigen", !panel.muted);
        visible.connect("toggled", Lang.bind(this, function() {
            this._runSafely("panel visibility toggle", Lang.bind(this, function() {
                this._updateAccountPanelSetting(usage.account, { muted: !visible.state });
            }));
        }));
        submenu.menu.addMenuItem(visible);
        let warnings = new PopupMenu.PopupSwitchMenuItem("Warnungen", alert.warnings);
        warnings.connect("toggled", Lang.bind(this, function() {
            this._runSafely("warning toggle", Lang.bind(this, function() {
                this._updateAccountAlertSetting(usage.account, { warnings: warnings.state });
            }));
        }));
        submenu.menu.addMenuItem(warnings);
        let errors = new PopupMenu.PopupSwitchMenuItem("Fehler", alert.errors);
        errors.connect("toggled", Lang.bind(this, function() {
            this._runSafely("error toggle", Lang.bind(this, function() {
                this._updateAccountAlertSetting(usage.account, { errors: errors.state });
            }));
        }));
        submenu.menu.addMenuItem(errors);
        this.menu.addMenuItem(submenu);
    },

    _updateAccountPanelSetting: function(account, changes) {
        let current = this._panelSettings[account] || this._defaultPanelRow(account, 1);
        let candidate = {};
        Object.keys(current).forEach(function(key) { candidate[key] = current[key]; });
        Object.keys(changes).forEach(function(key) { candidate[key] = changes[key]; });
        let normalized = this._normalizePanelRow(candidate, account);
        if (!normalized) {
            return;
        }
        let rows = this.accountPanelSettings.map(function(row) {
            return row.account === account ? normalized : row;
        });
        this.accountPanelSettings = rows;
        this._panelSettings = this._panelSettingsMap(rows);
        try {
            this.settings.setValue("account-panel-settings", rows);
        } catch (e) {
            global.log("[" + UUID + "] panel account setting failed: " + String(e));
        }
        this._updatePanel();
    },

    _updateAccountAlertSetting: function(account, changes) {
        let current = this._alertSettings[account] || this._defaultAlertRow(account);
        let candidate = {};
        Object.keys(current).forEach(function(key) { candidate[key] = current[key]; });
        Object.keys(changes).forEach(function(key) { candidate[key] = changes[key]; });
        let normalized = this._normalizeAlertRow(candidate, account);
        if (!normalized) {
            return;
        }
        let rows = this.accountAlertSettings.map(function(row) {
            return row.account === account ? normalized : row;
        });
        this.accountAlertSettings = rows;
        this._alertSettings = this._alertSettingsMap(rows);
        try {
            this.settings.setValue("account-alert-settings", rows);
        } catch (e) {
            global.log("[" + UUID + "] alert account setting failed: " + String(e));
        }
        this._updatePanel();
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
            this._runSafely("reactivation action", Lang.bind(this, function() {
                this._reactivateAccount(usage);
            }));
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
            let current = this._reactivations[usage.account] === record;
            let timeoutId = record.timeoutId;
            record.timeoutId = 0;
            if (timeoutId) {
                try {
                    Mainloop.source_remove(timeoutId);
                } catch (e) {
                    global.log("[" + UUID + "] reactivation source cleanup failed: " + this._shortText(e, 180));
                }
            }
            if (!current || this._removed) {
                return;
            }
            delete this._reactivations[usage.account];
            if (error || !payload || payload.ok !== true || payload.account !== usage.account) {
                this._reactivationErrors[usage.account] = this._shortText(
                    error || (payload && payload.error) || _("Reaktivierung fehlgeschlagen"),
                    240
                );
                this._buildUsageMenu();
                return;
            }
            delete this._reactivationErrors[usage.account];
            if (this._refreshing) {
                this._reactivationRefreshPending = true;
            } else {
                this._refreshFresh(false);
            }
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
                    record.timeoutId = 0;
                    try {
                        record.process.force_exit();
                    } catch (e) {
                        global.log("[" + UUID + "] reactivation cleanup failed: " + String(e));
                    }
                    finish(null, _("Login nach 15 Minuten abgebrochen"));
                    return false;
                })
            );
            this._readBoundedProcessOutput(record.process, Lang.bind(this, function(stdout, stderr, outputError) {
                    if (outputError) {
                        finish(null, outputError);
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
            }));
        } catch (e) {
            finish(null, _("Login konnte nicht gestartet werden: ") + String(e));
        }
    },

    _addActions: function() {
        let refreshLabel = this._refreshing ? _("Aktualisierung läuft …") : _("Jetzt aktualisieren");
        let refreshItem = this.menu.addAction(refreshLabel, Lang.bind(this, function() {
            this._runSafely("manual refresh action", Lang.bind(this, function() {
                this._refreshFresh(false);
            }));
        }));
        if (this._refreshing && refreshItem && refreshItem.setSensitive) {
            refreshItem.setSensitive(false);
        }
        if (this.pollOwner === "systemd" && this._serviceChecked && !this._systemdActive) {
            this.menu.addAction(
                _("Hintergrunddienst aktivieren"),
                Lang.bind(this, function() {
                    this._runSafely("service activation action", Lang.bind(this, this._enableBackgroundService));
                })
            );
        }
        this.menu.addAction(_("Codex Analytics öffnen"), Lang.bind(this, function() {
            this._runSafely("analytics action", Lang.bind(this, this._openAnalytics));
        }));
        this.menu.addAction(_("Einstellungen"), Lang.bind(this, function() {
            this._runSafely("settings action", Lang.bind(this, this._openSettings));
        }));
    },

    _addHealthAction: function(menu) {
        menu.addAction(_("Health anzeigen"), Lang.bind(this, function() {
            this._runSafely("health action", Lang.bind(this, function() {
                let argv;
                try {
                    argv = this._baseCommandArgv();
                } catch (e) {
                    this._showCommandError(String(e));
                    return;
                }
                argv.push("health", "--format", "json");
                this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
                    if (error || !payload) {
                        this._showCommandError(error || _("Health konnte nicht gelesen werden"));
                        return;
                    }
                    this._addDisabled(this.menu, this._shortText(JSON.stringify(payload), 240), "codex-usage-detail");
                }));
            }));
        }));
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
        if (this._safeMode) {
            return;
        }
        this._clearPanelClasses();
        let selected = this._panelItems().filter(function(item) {
            return item.visible;
        });
        let hasError = false;
        let values = [];
        let hasWarning = false;
        for (let i = 0; i < selected.length; i++) {
            let item = selected[i];
            let usage = item.usage;
            if (["error", "login_required"].indexOf(usage.status) !== -1) {
                hasError = true;
            }
            for (let j = 0; j < item.slots.length; j++) {
                let slot = item.slots[j];
                if (slot.value !== null) {
                    values.push(slot.value);
                    if (slot.value <= this._panelThreshold(item, slot.source)) {
                        hasWarning = true;
                    }
                }
            }
        }
        let worst = values.length ? Math.min.apply(Math, values) : null;
        let panel = this._panelContent(selected);
        this.set_applet_label(panel.plain);
        this._setPanelMarkup(panel.markup);
        if (hasError) {
            this.actor.add_style_class_name("codex-usage-panel-error");
        } else if (worst !== null && worst <= 5) {
            this.actor.add_style_class_name("codex-usage-panel-critical");
        } else if (hasWarning) {
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
        this._lastGoodPanel = panel;
        this._lastGoodTooltip = tooltip.plain;
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

    _panelItems: function() {
        let items = [];
        for (let i = 0; i < this._usages.length; i++) {
            let usage = this._usages[i];
            let fallback = this._defaultPanelRow(usage.account, i + 1);
            let settings = this._panelSettings[usage.account] || fallback;
            let sources = [settings.slot1, settings.slot2].filter(function(source, index, all) {
                return source > 0 && all.indexOf(source) === index;
            });
            let slots = sources.map(Lang.bind(this, function(source) {
                let window = this._panelWindowForSource(usage, source);
                return {
                    source: source,
                    window: window,
                    value: this._panelValueForSource(usage, source)
                };
            }));
            items.push({
                usage: usage,
                settings: settings,
                slots: slots,
                visible: !settings.muted && slots.length > 0
            });
        }
        items.sort(function(left, right) {
            return left.settings.order - right.settings.order;
        });
        return items;
    },

    _panelContent: function(selected) {
        if (!selected.length) {
            return this._usages.length
                ? { plain: "", markup: "" }
                : { plain: "--", markup: "--" };
        }
        let parts = selected.map(Lang.bind(this, function(item) {
            return this._panelAccountContent(item);
        }));
        let separator = this._panelSeparator();
        let plain = parts.map(function(part) { return part.plain; }).join(separator.plain);
        let markup = parts.map(function(part) { return part.markup; }).join(separator.markup);
        return { plain: plain, markup: markup };
    },

    _panelAccountContent: function(item) {
        let tag = this._panelTag(item);
        let slots = item.slots.map(Lang.bind(this, function(slot) {
            return this._panelSlotContent(item, slot);
        }));
        let plain = tag + " " + slots.map(function(slot) { return slot.plain; }).join(" / ");
        let markup = this._escapeMarkup(tag + " ") +
            slots.map(function(slot) { return slot.markup; }).join(" / ");
        if (this.panelAccountSeparator === "brackets") {
            return {
                plain: "[" + plain + "]",
                markup: "[" + markup + "]"
            };
        }
        return { plain: plain, markup: markup };
    },

    _panelSlotContent: function(item, slot) {
        let percent = this._percentPartsFromValue(slot.value, item.usage.account, "panel");
        let reset = this._windowResetParts(slot.window, item.usage.account, "panel", false);
        let label = this._panelSourceLabel(slot.source);
        return {
            plain: label + " " + percent.plain + (reset.plain ? " " + reset.plain : ""),
            markup: this._escapeMarkup(label + " ") + percent.markup +
                (reset.markup ? " " + reset.markup : "")
        };
    },

    _panelTag: function(item) {
        let custom = this._safeText(item.settings.tag, 8);
        return custom || this._accountTag(item.usage.label);
    },

    _panelSeparator: function() {
        let separators = {
            bar: { plain: " | ", markup: " | " },
            dot: { plain: " · ", markup: " · " },
            slash: { plain: " // ", markup: " // " },
            brackets: { plain: " ", markup: " " }
        };
        return separators[this.panelAccountSeparator] || separators.bar;
    },

    _panelSourceLabel: function(source) {
        return { 1: "5h", 2: "W", 3: "Ø" }[source] || "?";
    },

    _panelValueForSource: function(usage, source) {
        let five = this._remainingPercent(usage.five_hour);
        let week = this._remainingPercent(usage.weekly);
        if (source === 1) {
            return five;
        }
        if (source === 2) {
            return week;
        }
        let values = [five, week].filter(function(value) { return value !== null; });
        if (!values.length) {
            return null;
        }
        return values.reduce(function(total, value) { return total + value; }, 0) / values.length;
    },

    _panelWindowForSource: function(usage, source) {
        if (source === 1) {
            return usage.five_hour;
        }
        if (source === 2) {
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

    _panelThreshold: function(item, source) {
        let alert = this._alertSettings[item.usage.account] || this._defaultAlertRow(item.usage.account);
        let five = Number(alert["five-threshold"]);
        let weekly = Number(alert["weekly-threshold"]);
        if (source === 1) {
            return five;
        }
        if (source === 2) {
            return weekly;
        }
        let values = [];
        if (this._remainingPercent(item.usage.five_hour) !== null) {
            values.push(five);
        }
        if (this._remainingPercent(item.usage.weekly) !== null) {
            values.push(weekly);
        }
        return values.length
            ? values.reduce(function(total, value) { return total + value; }, 0) / values.length
            : 100;
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
        let currentWarnings = Object.create(null);
        let currentErrors = Object.create(null);
        for (let i = 0; i < this._usages.length; i++) {
            let usage = this._usages[i];
            let alert = this._alertSettings[usage.account] || this._defaultAlertRow(usage.account);
            if (["error", "login_required"].indexOf(usage.status) !== -1) {
                let errorKey = usage.account + ":" + usage.status;
                currentErrors[errorKey] = true;
                if (this.notifyErrors && alert.errors && !this._errorState[errorKey]) {
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
                ["5h", usage.five_hour, "five-threshold"],
                ["Woche", usage.weekly, "weekly-threshold"]
            ];
            for (let j = 0; j < windows.length; j++) {
                let remaining = this._remainingPercent(windows[j][1]);
                let threshold = Number(alert[windows[j][2]]);
                if (remaining !== null && remaining <= threshold) {
                    let warningKey = usage.account + ":" + windows[j][0];
                    currentWarnings[warningKey] = true;
                    if (this.notifyWarnings && alert.warnings && !this._warningState[warningKey]) {
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
        let showDuration = includeUnselected || this._targetEnabled(account, "duration", surface);
        if (!showDate && !showTime && !showDuration) {
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
        let durationStyle = this._durationStyles[account] || this._defaultStyleRow(account, "duration");
        let remaining = this._remainingPercent(window);
        let durationMinutes = this._durationMinutes(window);
        let dateText = this._formatDatePart(date, dateStyle.format);
        let timeText = this._formatTimePart(date, timeStyle.format);
        let durationText = this._formatDurationPart(durationMinutes, durationStyle.format);
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
        if (showDuration) {
            let labeledDuration = "Rest " + durationText;
            plainParts.push(labeledDuration);
            markupParts.push(this._targetEnabled(account, "duration", surface)
                ? this._escapeMarkup("Rest ") + this._styleSpan(durationText, durationStyle, durationMinutes, surface)
                : this._escapeMarkup(labeledDuration));
        }
        return {
            plain: plainParts.join(" "),
            markup: markupParts.join(" ")
        };
    },

    _targetEnabled: function(account, element, surface) {
        let elements = { percent: 0, date: 1, time: 2, duration: 3 };
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

    _durationMinutes: function(window) {
        if (!window || !window.reset_at) {
            return null;
        }
        let millis = this._dateMillis(window.reset_at);
        if (!millis) {
            return null;
        }
        return Math.max(0, Math.ceil((millis - Date.now()) / 60000));
    },

    _formatDurationPart: function(minutes, format) {
        if (minutes === null || !Number.isFinite(minutes)) {
            return "–";
        }
        let total = Math.max(0, Math.round(minutes));
        let days = Math.floor(total / 1440);
        let hours = Math.floor((total % 1440) / 60);
        let rest = total % 60;
        let pad = function(number) { return String(number).padStart(2, "0"); };
        if (format === 1) {
            return (days ? days + "d " : "") + pad(hours) + ":" + pad(rest);
        }
        if (format === 2) {
            let parts = [];
            if (days) {
                parts.push(days + (days === 1 ? " Tag" : " Tage"));
            }
            if (hours || days) {
                parts.push(hours + (hours === 1 ? " Stunde" : " Stunden"));
            }
            if (rest || !parts.length) {
                parts.push(rest + (rest === 1 ? " Minute" : " Minuten"));
            }
            return parts.join(" ");
        }
        if (format === 3) {
            return Math.floor(total / 60) + "h " + pad(rest) + "m";
        }
        if (days) {
            return days + "d " + hours + "h" + (rest ? " " + rest + "m" : "");
        }
        if (hours) {
            return hours + "h" + (rest ? " " + rest + "m" : "");
        }
        return rest + "m";
    },

    _styleSpan: function(text, style, remaining, surface) {
        let escaped = this._escapeMarkup(text);
        if (!this._styleIsActive(style, remaining)) {
            return escaped;
        }
        let mode = this._styleMode(style);
        let below = remaining !== null && Number.isFinite(remaining) &&
            remaining < Number(style.threshold);
        let useBelow = mode === 2 && below;
        let fontValue = useBelow ? style["below-font"] : style.font;
        let sizeValue = useBelow ? style["below-size"] : style.size;
        let boldValue = useBelow ? style["below-bold"] : style.bold;
        let italicValue = useBelow ? style["below-italic"] : style.italic;
        let colorValue = useBelow ? style["below-color"] : style.color;
        let backgroundValue = useBelow ? style["below-background"] : style.background;
        if (fontValue === undefined) {
            fontValue = style.font;
        }
        if (sizeValue === undefined) {
            sizeValue = style.size;
        }
        if (boldValue === undefined) {
            boldValue = style.bold;
        }
        if (italicValue === undefined) {
            italicValue = style.italic;
        }
        if (colorValue === undefined) {
            colorValue = style.color === undefined ? 0 : style.color;
        }
        if (backgroundValue === undefined) {
            backgroundValue = style.background === undefined ? 0 : style.background;
        }
        let attrs = [];
        let fonts = [null, "Sans", "Serif", "Monospace"];
        let font = fonts[fontValue] || null;
        if (font) {
            attrs.push('font_family="' + font + '"');
        }
        if (sizeValue > 0) {
            let maximum = surface === "panel"
                ? Math.max(8, Math.floor(this.panelHeight * 0.55))
                : 48;
            let size = Math.max(6, Math.min(maximum, sizeValue));
            attrs.push('size="' + size + 'pt"');
        }
        if (boldValue) {
            attrs.push('weight="bold"');
        }
        if (italicValue) {
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
        let fontColors = [
            null,
            "#111111",
            "#ffffff",
            "#dc2626",
            "#16a34a",
            "#2563eb",
            "#ca8a04",
            "#6b7280"
        ];
        let colors = backgrounds[backgroundValue] || null;
        if (colors) {
            attrs.push('background="' + colors.background + '"');
        }
        let foreground = fontColors[colorValue] || (colors ? colors.foreground : null);
        if (foreground) {
            attrs.push('foreground="' + foreground + '"');
        }
        return attrs.length ? "<span " + attrs.join(" ") + ">" + escaped + "</span>" : escaped;
    },

    _styleMode: function(style) {
        if (style.mode !== undefined) {
            let mode = Number(style.mode);
            if (Number.isInteger(mode) && mode >= 0 && mode <= 3) {
                return mode;
            }
        }
        return style.conditional === true ? 1 : 0;
    },

    _styleIsActive: function(style, remaining) {
        let mode = this._styleMode(style);
        if (mode === 3) {
            return false;
        }
        if (mode !== 1) {
            return true;
        }
        return remaining !== null && Number.isFinite(remaining) &&
            remaining < Number(style.threshold);
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
        let alert = this._alertSettings[usage.account] || this._defaultAlertRow(usage.account);
        let fiveThreshold = Number(alert["five-threshold"]);
        let weeklyThreshold = Number(alert["weekly-threshold"]);
        let critical = values.some(function(value) { return value <= 5; });
        let warning = (five !== null && five <= fiveThreshold) ||
            (week !== null && week <= weeklyThreshold);
        if (critical) {
            return "codex-usage-critical";
        }
        if (warning) {
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
        this._primaryRequest = null;
        this._removeSource("_timeoutId");
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
        if (this._auxCommand === "service-enable" && !this._systemdActive) {
            this._serviceAutoAttempted = false;
        }
        this._auxCommand = "";
        this._auxGeneration += 1;
        this._removeSource("_auxTimeoutId");
        if (this._auxProcess) {
            try {
                this._auxProcess.force_exit();
            } catch (e) {
                global.log("[" + UUID + "] auxiliary process cleanup failed: " + String(e));
            }
            this._auxProcess = null;
        }
    },

    _cancelHealthProcess: function() {
        this._healthGeneration += 1;
        this._removeSource("_healthTimeoutId");
        if (this._healthProcess) {
            try {
                this._healthProcess.force_exit();
            } catch (e) {
                global.log("[" + UUID + "] health process cleanup failed: " + this._shortText(e, 180));
            }
            this._healthProcess = null;
        }
    },

    _cancelReactivations: function() {
        let accounts = Object.keys(this._reactivations);
        for (let i = 0; i < accounts.length; i++) {
            let record = this._reactivations[accounts[i]];
            record.done = true;
            let timeoutId = record.timeoutId;
            record.timeoutId = 0;
            if (timeoutId) {
                try {
                    Mainloop.source_remove(timeoutId);
                } catch (e) {
                    global.log("[" + UUID + "] reactivation source cleanup failed: " + this._shortText(e, 180));
                }
            }
            if (record.process) {
                try {
                    record.process.force_exit();
                } catch (e) {
                    global.log("[" + UUID + "] reactivation process cleanup failed: " + String(e));
                }
            }
        }
        this._reactivations = Object.create(null);
    },

    on_applet_clicked: function() {
        this._runSafely("applet click", Lang.bind(this, function() {
            if (this._removed || !this.menu) {
                return;
            }
            let wasOpen = this.menu.isOpen;
            this.menu.toggle();
            if (this.refreshOnOpen && !wasOpen) {
                if (this._usesAppletPolling()) {
                    this._refreshFresh(false);
                } else {
                    this._loadCached(false);
                }
            }
        }));
    },

    on_applet_removed_from_panel: function() {
        this._removed = true;
        this._backendChangeQueue = [];
        this._backendChangeCurrent = null;
        this._backendAuxQueue = [];
        this._primaryCachePending = false;
        this._primaryCacheRefreshAfter = false;
        this._primaryFreshPending = false;
        this._primaryFreshOpenAfter = false;
        this._removeSource("_timerId");
        this._removeSource("_displayTimerId");
        this._removeSource("_staleCheckId");
        this._removeIdleSources();
        this._cancelProcess();
        this._cancelAuxProcess();
        this._cancelHealthProcess();
        this._cancelReactivations();
        if (this.settings && this.settings.finalize) {
            try {
                this.settings.finalize();
            } catch (e) {
                global.log("[" + UUID + "] settings finalize failed: " + this._shortText(e, 180));
            }
        }
        if (this.menu) {
            try {
                this.menu.destroy();
            } catch (e) {
                global.log("[" + UUID + "] menu destroy failed: " + this._shortText(e, 180));
            }
            this.menu = null;
        }
    }
};

function main(metadata, orientation, panelHeight, instanceId) {
    return new CodexUsageApplet(metadata, orientation, panelHeight, instanceId);
}
