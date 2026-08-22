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
const MAX_JSON_CHARS = 65536;
const MAX_STDERR_CHARS = 8192;
const MAX_CLEANUP_LOGS = 16;
const MAX_ACCOUNTS = 100;
const MAX_PROFILE_JOBS = 64;
const MAX_USAGE_POOLS = 20;
const MAX_POOL_WINDOWS = 8;
const MAX_CONSUMPTION_WINDOWS = 64;
const MAX_TEXT_CHARS = 500;
const COMMAND_TIMEOUT_MS = 120000;
const AUX_COMMAND_TIMEOUT_MS = 30000;
const DEVICE_LOGIN_TIMEOUT_MS = 910000;
const MAX_DEFERRED_AUX_REQUESTS = 8;
const REACTIVATION_TIMEOUT_MS = 900000;
const CIRCUIT_BREAKER_MS = 900000;
const INTERNAL_FAILURE_WINDOW_MS = 300000;
const INTERNAL_FAILURE_LIMIT = 3;
const CACHE_SYNC_INTERVAL_MS = 60000;
const REFRESH_FAILURE_LIMIT = 3;
const ERROR_NOTIFICATION_SUPPRESSION_MS = 48 * 60 * 60 * 1000;
const MAX_ERROR_NOTIFICATION_STATES = 128;
const MAX_ERROR_NOTIFICATION_STATE_CHARS = 16 * 1024;
const FAST_MODE_STATE_PATH = "/home/teladi/.local/state/codex-master-mcp/fast-mode.json";
const EMERGENCY_DISPLAY_OVERRIDE_PATH = "/home/teladi/.local/state/codex-master-mcp/codex-usage-emergency-overrides.json";
const FAST_MODE_ICON = "fast-mode-warning-shield-outline.svg";
const MAX_CAPTURE_FUTURE_MS = 5 * 60 * 1000;
const SETTINGS_WINDOW_LOOKUP_MAX_ATTEMPTS = 40;
const MENU_SPACER = "────────";
const REACTIVATION_BROWSER_NAMES = ["auto", "vivaldi", "chromium", "firefox"];
const PANEL_VALUE_DEFAULT_COUNT = 20;
const PANEL_VALUE_MAX_COUNT = 64;
const PANEL_SOURCE_LABELS = {
    0: "?",
    1: "5h",
    2: "W",
    3: "Ø",
    4: "S5h",
    5: "SW",
    6: "SØ",
    7: "S+",
    8: "30d",
    9: "CR",
    10: "CV",
    11: "Resets",
    12: "TE",
    13: "Δ",
    14: "Kürzel",
    15: "Label",
    16: "Acc ID",
    17: "Abrufweg",
    18: "sonstiges",
    19: "S sonst.",
    20: "Rest 5h",
    21: "Rest W",
    22: "Rest M",
    23: "Rest S5h",
    24: "Rest SW",
    25: "Rest S+",
    26: "Reset 5h",
    27: "Reset W",
    28: "Reset M",
    29: "Reset S5h",
    30: "Reset SW",
    31: "Reset S+",
    32: "Δ5h",
    33: "ΔW",
    34: "ΔM",
    35: "ΔSpark",
    36: "Δsonst.",
    37: "Limit 5h",
    38: "Limit W",
    39: "Limit M",
    40: "Limit S5h",
    41: "Limit SW",
    42: "Limit S+",
    43: "Routing",
    44: "CV aktiv",
    45: "Credit h",
    46: "Credit W",
    47: "Credit M",
    48: "Warnung",
    49: "Fehler",
    50: "Login",
    51: "Status"
};
const PANEL_LIMIT_SOURCE_MAP = {
    37: 1,
    38: 2,
    39: 8,
    40: 4,
    41: 5,
    42: 7
};
const PANEL_FORMATTING_TARGETS = {
    11: {key: "account-panel-resets-styles", property: "accountPanelResetsStyles"},
    14: {key: "account-panel-tag-styles", property: "accountPanelTagStyles"},
    15: {key: "account-panel-label-styles", property: "accountPanelLabelStyles"},
    16: {key: "account-panel-id-styles", property: "accountPanelIdStyles"},
    17: {key: "account-panel-backend-styles", property: "accountPanelBackendStyles"},
    43: {key: "account-panel-routing-styles", property: "accountPanelRoutingStyles"},
    44: {key: "account-panel-credit-active-styles", property: "accountPanelCreditActiveStyles"},
    45: {key: "account-panel-credit-hourly-styles", property: "accountPanelCreditHourlyStyles"},
    46: {key: "account-panel-credit-weekly-styles", property: "accountPanelCreditWeeklyStyles"},
    47: {key: "account-panel-credit-monthly-styles", property: "accountPanelCreditMonthlyStyles"},
    48: {key: "account-panel-warning-styles", property: "accountPanelWarningStyles"},
    49: {key: "account-panel-error-styles", property: "accountPanelErrorStyles"},
    50: {key: "account-panel-login-styles", property: "accountPanelLoginStyles"},
    51: {key: "account-panel-status-styles", property: "accountPanelStatusStyles"}
};
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
        this.panelValueCount = String(PANEL_VALUE_DEFAULT_COUNT);
        this.hideAccountWhenLongLimitExhausted = false;
        this.fastModeIcon = "fast-mode-warning-shield-outline.svg";
        this.warningThreshold = 20;
        this.notifyWarnings = false;
        this.notifyErrors = false;
        this.errorNotificationState = "{}";
        this.showReactivationActions = true;
        this.reactivationBrowser = "auto";
        this.reactivationBrowserMigrated = false;
        this.accountBackends = [];
        this.accountPanelSettings = [];
        this.accountConsumptionSettings = [];
        this.accountForecastSettings = [];
        this.accountCreditSettings = [];
        this.accountCreditConsumptionSettings = [];
        this.showConsumptionDelta = true;
        this.accountResetDisplaySettings = [];
        this.accountAlertSettings = [];
        this.accountPercentStyles = [];
        this.accountDateStyles = [];
        this.accountTimeStyles = [];
        this.accountDurationStyles = [];
        this.accountDeltaStyles = [];
        for (let source in PANEL_FORMATTING_TARGETS) {
            if (Object.prototype.hasOwnProperty.call(PANEL_FORMATTING_TARGETS, source)) {
                this[PANEL_FORMATTING_TARGETS[source].property] = [];
            }
        }
        this.accountDisplaySettings = [];
        this.accountStyleTargets = [];
        this.routingGlobalPaidCredits = false;
        this.routingCreditOverrides = [];
        this.routingCreditHourlyLimit = 0;
        this.routingCreditWeeklyLimit = 0;
        this.routingCreditMonthlyLimit = 0;

        this._removed = false;
        this._sources = {};
        this._signalConnections = [];
        this._cleanupLogCount = 0;
        this._idleSources = {};
        this._safeMode = false;
        this._safeModeReason = "";
        this._internalFailures = [];
        this._refreshFailures = 0;
        this._circuitOpenUntil = 0;
        this._lastRefreshError = "";
        this._commandError = "";
        this._lastGoodPanel = { plain: "--", markup: "--" };
        this._lastGoodTooltip = "";
        this._panelSurfaceState = {
            plain: null,
            markup: null,
            tooltip: null,
            icon: null,
        };
        this._generation = 0;
        this._primaryRequest = null;
        this._primaryCachePending = false;
        this._primaryCacheRefreshAfter = false;
        this._primaryFreshPending = false;
        this._primaryFreshOpenAfter = false;
        this._timerId = 0;
        this._displayTimerId = 0;
        this._timerGeneration = 0;
        this._displayTimerGeneration = 0;
        this._timeoutId = 0;
        this._process = null;
        this._refreshing = false;
        this._usages = [];
        this._warningState = Object.create(null);
        this._errorState = Object.create(null);
        this._errorNotificationStateWritePending = null;
        this._reactivations = Object.create(null);
        this._reactivationErrors = Object.create(null);
        this._reactivationRefreshPending = false;
        this._deviceLoginActive = Object.create(null);
        this._deviceLoginJobs = Object.create(null);
        this._deviceLoginErrors = Object.create(null);
        this._accountManageErrors = Object.create(null);
        this._accountTerminalErrors = Object.create(null);
        this._deviceLoginEvents = Object.create(null);
        this._deviceLoginLiveText = Object.create(null);
        this._deviceLoginLiveAccount = "";
        this._profileJobsLoaded = false;
        this._profileJobsResumeRequested = false;
        this._profileJobResumeQueue = [];
        this._profileJobPollingAccount = "";
        this._profileJobCommandAccount = "";
        this._deviceLoginPollId = 0;
        this._deviceLoginPollGeneration = 0;
        this._profilePendingAccounts = Object.create(null);
        this._auxProcess = null;
        this._auxCommand = "";
        this._auxTimeoutId = 0;
        this._auxGeneration = 0;
        this._healthProcess = null;
        this._healthTimeoutId = 0;
        this._settingsMaximizeId = 0;
        this._settingsMaximizeGeneration = 0;
        this._settingsPlacementProcess = null;
        this._settingsWindowLookupProcess = null;
        this._healthGeneration = 0;
        this._lastHealthReportAt = 0;
        this._backendRowsReady = false;
        this._syncingBackendRows = false;
        this._backendAccounts = Object.create(null);
        this._backendChangeQueue = [];
        this._backendChangeCurrent = null;
        this._accountChangeQueue = [];
        this._accountChangeCurrent = null;
        this._accountChangePendingRows = null;
        this._accountDeleteWaitingForProfileJob = Object.create(null);
        this._legacyReactivationMigrationPending = 0;
        this._legacyReactivationMigrationStarted = false;
        this._backendAuxQueue = [];
        this._syncingAccountSettings = false;
        this._panelSettings = Object.create(null);
        this._consumptionSettings = Object.create(null);
        this._creditSettings = Object.create(null);
        this._resetSettings = Object.create(null);
        this._consumptionQueue = [];
        this._consumptionCurrent = null;
        this._consumptionGeneration = 0;
        this._alertSettings = Object.create(null);
        this._syncingStyleRows = false;
        this._percentStyles = Object.create(null);
        this._dateStyles = Object.create(null);
        this._timeStyles = Object.create(null);
        this._durationStyles = Object.create(null);
        this._deltaStyles = Object.create(null);
        this._panelValueStyles = Object.create(null);
        this._displaySettings = Object.create(null);
        this._styleTargets = Object.create(null);
        this._routingPolicy = null;
        this._routingDecisions = Object.create(null);
        this._routingSettingsReady = false;
        this._syncingRoutingSettings = false;
        this._routingPolicyApplying = false;
        this._systemdActive = false;
        this._serviceChecked = false;
        this._serviceStatus = {};
        this._serviceAutoAttempted = false;
        this._serviceRepairAt = 0;
        this._staleFallbackAt = 0;
        this._staleCheckId = 0;
        this._staleCheckGeneration = 0;
        this._lastCacheSyncAt = 0;
        this._fastModeState = { modes: {}, last_event: null };
        this._fastModeIconPath = (this.metadata.path || "") + "/icons/" + FAST_MODE_ICON;

        this.set_applet_icon_symbolic_name("view-statistics-symbolic");
        this.set_applet_label("--");
        this.set_applet_tooltip(_("Codex-Nutzung wird geladen"));
        this._refreshFastModeState();

        this.menuManager = new PopupMenu.PopupMenuManager(this);
        this.menu = new Applet.AppletPopupMenu(this, orientation);
        this.menuManager.addMenu(this.menu);
        this._menuDirty = false;
        try {
            this._connectTrackedSignal(this.menu, "open-state-changed", Lang.bind(this, function(_menu, open) {
                if (!open && this._menuDirty && !this._removed) {
                    this._buildUsageMenu();
                }
            }));
        } catch (e) {
            global.log("[" + UUID + "] menu state binding unavailable: " + this._shortText(e, 180));
        }
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

    _bindCustomSetting: function(key, property, callback) {
        let readValue = Lang.bind(this, function(invokeCallback) {
            let value;
            try {
                value = this.settings.getValue(key);
            } catch (e) {
                global.log("[" + UUID + "] custom setting read failed: " + key);
                return;
            }
            this[property] = value;
            if (invokeCallback && callback) {
                this._runSafely("settings:" + key, Lang.bind(this, function() {
                    return callback.call(this, value);
                }));
            }
        });
        readValue(false);
        try {
            this._connectTrackedSignal(
                this.settings,
                "changed::" + key,
                Lang.bind(this, function() {
                    readValue(true);
                })
            );
        } catch (e) {
            global.log("[" + UUID + "] custom setting signal unavailable: " + key);
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
        bind("panel-value-count", "panelValueCount", this._updatePanel);
        bind(
            "hide-5h-when-long-limit-exhausted",
            "hideFiveHourWhenLongLimitExhausted",
            this._refreshFormattedSurfaces
        );
        bind(
            "hide-account-when-long-limit-exhausted",
            "hideAccountWhenLongLimitExhausted",
            this._refreshFormattedSurfaces
        );
        this._bindCustomSetting("fast-mode-icon", "fastModeIcon", this._updatePanel);
        bind("warning-threshold", "warningThreshold", this._updatePanel);
        bind("notify-warnings", "notifyWarnings", null);
        bind("notify-errors", "notifyErrors", null);
        bind("error-notification-state", "errorNotificationState", null);
        bind(
            "show-reactivation-actions",
            "showReactivationActions",
            this._rebuildMenu
        );
        bind("reactivation-browser", "reactivationBrowser", null);
        bind("reactivation-browser-migrated", "reactivationBrowserMigrated", null);
        this._bindCustomSetting(
            "account-backends",
            "accountBackends",
            this._onAccountBackendsChanged
        );
        this._bindCustomSetting(
            "account-panel-settings",
            "accountPanelSettings",
            this._onPanelSettingsChanged
        );
        bind(
            "account-consumption-settings",
            "accountConsumptionSettings",
            this._onConsumptionSettingsChanged
        );
        bind("account-forecast-settings", "accountForecastSettings", this._onForecastSettingsChanged);
        bind("account-credit-settings", "accountCreditSettings", this._onCreditSettingsChanged);
        bind("account-credit-consumption-settings", "accountCreditConsumptionSettings", this._onCreditConsumptionSettingsChanged);
        bind("show-consumption-delta", "showConsumptionDelta", this._refreshFormattedSurfaces);
        bind(
            "account-reset-display-settings",
            "accountResetDisplaySettings",
            this._onResetDisplaySettingsChanged
        );
        bind("account-alert-settings", "accountAlertSettings", this._onAlertSettingsChanged);
        bind("account-percent-styles", "accountPercentStyles", this._onPercentStylesChanged);
        bind("account-date-styles", "accountDateStyles", this._onDateStylesChanged);
        bind("account-time-styles", "accountTimeStyles", this._onTimeStylesChanged);
        bind("account-duration-styles", "accountDurationStyles", this._onDurationStylesChanged);
        bind("account-delta-styles", "accountDeltaStyles", this._onDeltaStylesChanged);
        for (let source in PANEL_FORMATTING_TARGETS) {
            if (!Object.prototype.hasOwnProperty.call(PANEL_FORMATTING_TARGETS, source)) {
                continue;
            }
            let target = PANEL_FORMATTING_TARGETS[source];
            this._bindCustomSetting(
                target.key,
                target.property,
                Lang.bind(this, function() {
                    this._onPanelValueStylesChanged(Number(source));
                })
            );
        }
        bind("account-display-settings", "accountDisplaySettings", this._onDisplaySettingsChanged);
        bind("account-style-targets", "accountStyleTargets", this._onStyleTargetsChanged);
        bind(
            "routing-global-paid-credits",
            "routingGlobalPaidCredits",
            this._onRoutingSettingsChanged
        );
        bind(
            "routing-credit-overrides",
            "routingCreditOverrides",
            this._onRoutingSettingsChanged
        );
        bind("routing-credit-hourly-limit", "routingCreditHourlyLimit", this._onRoutingSettingsChanged);
        bind("routing-credit-weekly-limit", "routingCreditWeeklyLimit", this._onRoutingSettingsChanged);
        bind("routing-credit-monthly-limit", "routingCreditMonthlyLimit", this._onRoutingSettingsChanged);
        this._normalizeAccountBackendSettingPaths();
    },

    _normalizeAccountBackendSettingPaths: function() {
        let rows = this.accountBackends;
        if (!Array.isArray(rows)) {
            return;
        }
        let normalized = [];
        let changed = false;
        let pathKeys = ["auth-json", "profile-dir"];
        for (let i = 0; i < rows.length; i++) {
            let row = rows[i];
            if (!row || typeof row !== "object" || Array.isArray(row)) {
                return;
            }
            let normalizedRow = {};
            for (let key in row) {
                if (Object.prototype.hasOwnProperty.call(row, key)) {
                    normalizedRow[key] = row[key];
                }
            }
            for (let j = 0; j < pathKeys.length; j++) {
                let key = pathKeys[j];
                if (!Object.prototype.hasOwnProperty.call(row, key)) {
                    continue;
                }
                if (row[key] === null || row[key] === undefined) {
                    continue;
                }
                try {
                    normalizedRow[key] = this._accountSettingPath(
                        this._localAccountPath(row[key])
                    );
                } catch (e) {
                    global.log("[" + UUID + "] legacy account path migration skipped");
                    return;
                }
                if (normalizedRow[key] !== row[key]) {
                    changed = true;
                }
            }
            normalized.push(normalizedRow);
        }
        if (!changed) {
            return;
        }
        try {
            this.settings.setValue("account-backends", normalized);
        } catch (e) {
            global.log("[" + UUID + "] account path migration write failed: " +
                this._shortText(e, 180));
            return;
        }
        this.accountBackends = normalized;
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
        this._commandError = "";
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
            let timeoutId = Mainloop.timeout_add(5000, Lang.bind(this, function() {
                if (generation === this._healthGeneration) {
                    this._clearSource("_healthTimeoutId");
                }
                try {
                    process.force_exit();
                } catch (e) {
                    this._cleanupLog("health process cleanup failed: " + e);
                }
                finish();
                return false;
            }));
            if (!timeoutId) {
                throw new Error("health timeout source unavailable");
            }
            this._setSource("_healthTimeoutId", timeoutId);
            this._readBoundedProcessOutput(process, Lang.bind(this, function() {
                finish();
            }));
        } catch (e) {
            this._terminateChild(process, "health process startup cleanup");
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
        this._serviceAutoAttempted = false;
        this._primaryCachePending = false;
        this._primaryCacheRefreshAfter = false;
        this._primaryFreshPending = false;
        this._primaryFreshOpenAfter = false;
        this._reactivationRefreshPending = false;
        this._consumptionGeneration = (this._consumptionGeneration || 0) + 1;
        this._consumptionCurrent = null;
        if (!Array.isArray(this._consumptionQueue)) {
            this._consumptionQueue = [];
        }
        this._consumptionQueue.length = 0;
        this._routingPolicyApplying = false;
        if (!Array.isArray(this._pendingRoutingLimitCommands)) {
            this._pendingRoutingLimitCommands = [];
        }
        this._pendingRoutingLimitCommands.length = 0;
        this._timerGeneration = (this._timerGeneration || 0) + 1;
        this._displayTimerGeneration = (this._displayTimerGeneration || 0) + 1;
        this._staleCheckGeneration = (this._staleCheckGeneration || 0) + 1;
        this._deviceLoginPollGeneration = (this._deviceLoginPollGeneration || 0) + 1;
        this._profileJobsLoaded = false;
        this._profileJobsResumeRequested = false;
        if (!Array.isArray(this._profileJobResumeQueue)) {
            this._profileJobResumeQueue = [];
        }
        this._profileJobResumeQueue.length = 0;
        if (
            !this._profilePendingAccounts ||
            typeof this._profilePendingAccounts !== "object" ||
            Array.isArray(this._profilePendingAccounts)
        ) {
            this._profilePendingAccounts = Object.create(null);
        } else {
            let pendingAccounts = Object.keys(this._profilePendingAccounts);
            for (let index = 0; index < pendingAccounts.length; index++) {
                delete this._profilePendingAccounts[pendingAccounts[index]];
            }
        }
        this._profileJobPollingAccount = "";
        this._profileJobCommandAccount = "";
        this._removeSource("_timerId");
        this._removeSource("_displayTimerId");
        this._removeSource("_staleCheckId");
        this._removeSource("_deviceLoginPollId");
        this._backendChangeQueue = [];
        this._backendChangeCurrent = null;
        this._accountChangeCurrent = null;
        if (!Array.isArray(this._accountChangeQueue)) {
            this._accountChangeQueue = [];
        }
        this._accountChangeQueue.length = 0;
        this._accountChangePendingRows = null;
        this._backendAuxQueue = [];
        this._cancelProcess();
        this._cancelAuxProcess();
        this._cancelHealthProcess();
        this._cancelReactivations();
        this._panelSurfaceState = {
            plain: null,
            markup: null,
            tooltip: null,
            icon: null,
        };
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
        this._scheduleTimer();
        if (this._safeMode || this._removed) {
            return;
        }
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
            this._cleanupLog("source cleanup failed: " + e);
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
        if (id) {
            this._idleSources[id] = true;
        }
        return id;
    },

    _deferGuardRelease: function(property, context) {
        this._guardReleaseTokens = this._guardReleaseTokens || Object.create(null);
        let token = (this._guardReleaseTokens[property] || 0) + 1;
        this._guardReleaseTokens[property] = token;
        let release = Lang.bind(this, function() {
            if (this._guardReleaseTokens[property] === token) {
                this[property] = false;
            }
            return false;
        });
        try {
            let idleId = this._addIdle(release);
            if (!idleId) {
                release();
            }
        } catch (e) {
            global.log("[" + UUID + "] " + context + " failed: " + String(e));
            release();
        }
    },

    _removeIdleSources: function() {
        let ids = Object.keys(this._idleSources);
        this._idleSources = {};
        for (let i = 0; i < ids.length; i++) {
            try {
                Mainloop.source_remove(Number(ids[i]));
            } catch (e) {
                this._cleanupLog("idle cleanup failed: " + e);
            }
        }
    },

    _onCommandSettingsChanged: function() {
        if (this._removed || this._safeMode) {
            return;
        }
        this._loadCached(true);
    },

    _onRefreshSettingsChanged: function() {
        if (this._removed || this._safeMode) {
            return;
        }
        this._scheduleTimer();
        if (this.autoRefresh && this.pollOwner === "auto") {
            this._refreshAuxiliaryState();
        }
    },

    _onPollOwnerChanged: function() {
        if (this._removed || this._safeMode) {
            return;
        }
        this._refreshAuxiliaryState();
        this._scheduleTimer();
    },

    _rebuildMenu: function() {
        this._buildUsageMenu();
    },

    _scheduleTimer: function() {
        let generation = (this._timerGeneration || 0) + 1;
        this._timerGeneration = generation;
        this._removeSource("_timerId");
        if (!this._scheduleDisplayTimer()) {
            return;
        }
        if (!this.autoRefresh || this._removed) {
            return;
        }
        let seconds = this._boundedInteger(this.refreshInterval, 60, 3600, 300);
        try {
            let timerId = Mainloop.timeout_add_seconds(seconds, Lang.bind(this, function() {
                if (this._removed) {
                    if (generation === this._timerGeneration) {
                        this._clearSource("_timerId");
                    }
                    return false;
                }
                if (generation !== this._timerGeneration) {
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
            }));
            if (!timerId) {
                throw new Error("refresh timer source unavailable");
            }
            this._setSource("_timerId", timerId);
        } catch (e) {
            global.log("[" + UUID + "] refresh timer setup failed: " + this._shortText(e, 180));
            this._enterSafeMode("Refresh-Timer konnte nicht eingerichtet werden");
        }
    },

    _scheduleDisplayTimer: function() {
        let generation = (this._displayTimerGeneration || 0) + 1;
        this._displayTimerGeneration = generation;
        this._removeSource("_displayTimerId");
        if (this._removed) {
            return false;
        }
        try {
            let timerId = Mainloop.timeout_add_seconds(60, Lang.bind(this, function() {
                if (this._removed) {
                    if (generation === this._displayTimerGeneration) {
                        this._clearSource("_displayTimerId");
                    }
                    return false;
                }
                if (generation !== this._displayTimerGeneration) {
                    return false;
                }
                this._runSafely("display timer", Lang.bind(this, function() {
                    if (this._safeMode) {
                        return;
                    }
                    if (
                        this._systemdActive &&
                        !this._usesAppletPolling() &&
                        this._cacheNeedsSync()
                    ) {
                        this._loadCached(false, false);
                    }
                    this._refreshFastModeState();
                    this._updatePanel();
                    if (this.menu && this.menu.isOpen) {
                        this._buildUsageMenu();
                    }
                }));
                return true;
            }));
            if (!timerId) {
                throw new Error("display timer source unavailable");
            }
            this._setSource("_displayTimerId", timerId);
            return true;
        } catch (e) {
            global.log("[" + UUID + "] display timer setup failed: " + this._shortText(e, 180));
            this._enterSafeMode("Anzeige-Timer konnte nicht eingerichtet werden");
            return false;
        }
    },

    _loadCached: function(refreshAfter, refreshAuxiliaryState) {
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
            try {
                if (payload) {
                    this._applyPayload(payload, false);
                    this._lastCacheSyncAt = Date.now();
                } else if (!this._usages.length && error) {
                    this._showCommandError(error);
                }
            } finally {
                if (refreshAfter && this.autoRefresh && this._usesAppletPolling()) {
                    this._primaryFreshPending = true;
                }
                if (refreshAuxiliaryState !== false) {
                    this._refreshAuxiliaryState();
                }
            }
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
        try {
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
                try {
                    if (payload) {
                        this._recordRefreshSuccess();
                        this._applyPayload(payload, true);
                    } else {
                        this._recordRefreshFailure(error || _("Abruf fehlgeschlagen"));
                        this._showCommandError(this._lastRefreshError);
                    }
                } finally {
                    if (
                        openAfter &&
                        !this._removed &&
                        !this._safeMode &&
                        this.menu &&
                        !this.menu.isOpen
                    ) {
                        this._runSafely("open menu after refresh", Lang.bind(this, function() {
                            this.menu.toggle();
                        }));
                    }
                    if (refreshAfterReactivation && !this._removed && !this._safeMode) {
                        this._refreshFresh(false);
                    }
                }
            }));
        } catch (e) {
            this._refreshing = false;
            throw e;
        }
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

    _readBoundedProcessOutput: function(process, callback, onChunk) {
        let output = { stdout: "", stderr: "" };
        let completed = 0;
        let stopped = false;
        let cancellable = typeof Gio.Cancellable === "function"
            ? new Gio.Cancellable() : null;
        let complete = Lang.bind(this, function(stdout, stderr, error) {
            if (stopped) {
                return;
            }
            stopped = true;
            try {
                if (cancellable) {
                    cancellable.cancel();
                }
            } catch (e) {
                // Cancellation is best-effort after the child has exited.
            }
            this._runSafely("bounded output callback", Lang.bind(this, function() {
                callback(stdout, stderr, error);
            }));
        });
        let emitLiveChunk = Lang.bind(this, function(name, text, final) {
            if (typeof onChunk !== "function") {
                return;
            }
            try {
                onChunk(name, text, final === true);
            } catch (chunkError) {
                global.log("[" + UUID + "] live output callback failed: " +
                    this._shortText(chunkError, 180));
            }
        });
        let finishStream = Lang.bind(this, function() {
            completed += 1;
            if (completed === 2) {
                emitLiveChunk("", "", true);
                complete(output.stdout, output.stderr, null);
            }
        });
        let read = Lang.bind(this, function(name, stream, maximum) {
            if (!stream) {
                finishStream();
                return;
            }
            let chunks = [];
            let total = 0;
            let livePending = new Uint8Array(0);
            let clearBuffers = Lang.bind(this, function() {
                chunks = [];
                livePending = new Uint8Array(0);
                output[name] = "";
            });
            let next = Lang.bind(this, function() {
                if (stopped) {
                    return;
                }
                try {
                    stream.read_bytes_async(
                        8192,
                        GLib.PRIORITY_DEFAULT,
                        cancellable,
                        Lang.bind(this, function(source, result) {
                            if (stopped) {
                                return;
                            }
                            try {
                                let bytes = source.read_bytes_finish(result);
                                let size = bytes.get_size();
                                if (size === 0) {
                                    let raw = new Uint8Array(total);
                                    let offset = 0;
                                    for (let index = 0; index < chunks.length; index++) {
                                        raw.set(chunks[index], offset);
                                        offset += chunks[index].length;
                                    }
                                    if (livePending.length) {
                                        emitLiveChunk(
                                            name,
                                            ByteArray.toString(livePending),
                                            false
                                        );
                                    }
                                    output[name] = ByteArray.toString(raw);
                                    chunks = [];
                                    livePending = new Uint8Array(0);
                                    finishStream();
                                    return;
                                }
                                total += size;
                                if (total > maximum) {
                                    clearBuffers();
                                    try {
                                        process.force_exit();
                                    } catch (e) {
                                        this._cleanupLog("oversized process cleanup failed: " + e);
                                    }
                                    complete(null, null, name === "stdout"
                                        ? _("JSON-Ausgabe ist zu groß")
                                        : _("Fehlerausgabe ist zu groß"));
                                    return;
                                }
                                let chunk = new Uint8Array(bytes.get_data());
                                chunks.push(chunk);
                                if (typeof onChunk === "function") {
                                    try {
                                        let decoded = this._decodeLiveUtf8Chunk(livePending, chunk);
                                        livePending = decoded.pending;
                                        if (decoded.text) {
                                            emitLiveChunk(name, decoded.text, false);
                                        }
                                    } catch (chunkError) {
                                        global.log("[" + UUID + "] live output callback failed: " + this._shortText(chunkError, 180));
                                    }
                                }
                                next();
                            } catch (e) {
                                clearBuffers();
                                try {
                                    process.force_exit();
                                } catch (forceError) {
                                    this._cleanupLog("output process cleanup failed: " + forceError);
                                }
                                complete(null, null, _("Prozessausgabe konnte nicht gelesen werden"));
                            }
                        })
                    );
                } catch (e) {
                    clearBuffers();
                    try {
                        process.force_exit();
                    } catch (forceError) {
                        this._cleanupLog("output process cleanup failed: " + forceError);
                    }
                    complete(null, null, _("Prozessausgabe konnte nicht gelesen werden"));
                }
            });
            next();
        });
        read("stdout", process.get_stdout_pipe(), MAX_JSON_CHARS);
        read("stderr", process.get_stderr_pipe(), MAX_STDERR_CHARS);
    },

    _decodeLiveUtf8Chunk: function(pending, chunk) {
        let combined = new Uint8Array(pending.length + chunk.length);
        combined.set(pending, 0);
        combined.set(chunk, pending.length);
        let completeLength = combined.length;
        let leadIndex = combined.length - 1;
        while (leadIndex >= 0 && (combined[leadIndex] & 0xc0) === 0x80) {
            leadIndex -= 1;
        }
        if (leadIndex >= 0) {
            let lead = combined[leadIndex];
            let expectedLength = lead < 0x80
                ? 1
                : (lead >= 0xc2 && lead <= 0xdf
                    ? 2
                    : (lead >= 0xe0 && lead <= 0xef
                        ? 3
                        : (lead >= 0xf0 && lead <= 0xf4 ? 4 : 1)));
            if (combined.length - leadIndex < expectedLength) {
                completeLength = leadIndex;
            }
        }
        return {
            text: completeLength ? ByteArray.toString(combined.subarray(0, completeLength)) : "",
            pending: combined.subarray(completeLength),
        };
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
            let timeoutId = Mainloop.timeout_add(COMMAND_TIMEOUT_MS, Lang.bind(this, function() {
                if (generation === this._generation) {
                    this._clearSource("_timeoutId");
                }
                try {
                    process.force_exit();
                } catch (e) {
                    global.log("[" + UUID + "] force_exit failed: " + String(e));
                }
                finish(null, _("Abruf nach 120 Sekunden abgebrochen"));
                return false;
            }));
            if (!timeoutId) {
                throw new Error("primary timeout source unavailable");
            }
            this._setSource("_timeoutId", timeoutId);
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
            this._terminateChild(process, "primary process startup cleanup");
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
        if (this._removed || this._safeMode) {
            return;
        }
        this._checkServiceStatus(Lang.bind(this, function() {
            this._profileJobsResumeRequested = true;
            this._loadAccountBackends();
        }));
    },

    _checkServiceStatus: function(callback) {
        if (this._removed || this._safeMode) {
            return;
        }
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
            let validStatus = !error && payload && typeof payload === "object" &&
                !Array.isArray(payload) &&
                typeof payload.installed === "boolean" &&
                typeof payload.enabled === "boolean" &&
                typeof payload.active === "boolean" &&
                typeof payload.service_result === "string";
            this._serviceChecked = true;
            if (validStatus) {
                this._serviceStatus = payload;
                this._systemdActive = this._serviceStatusIsHealthy(payload);
                if (!this._systemdActive) {
                    this._serviceAutoAttempted = false;
                }
            } else {
                if (!wasChecked || !this._usages.length || this._cacheIsStale()) {
                    this._systemdActive = false;
                }
                if (!wasChecked) {
                    this._serviceStatus = {};
                }
            }
            this._scheduleTimer();
            if (this._removed || this._safeMode) {
                return;
            }
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
                this._repairStaleService(callback);
                return;
            }
            callback();
        }));
    },

    _serviceStatusIsHealthy: function(payload) {
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
            return false;
        }
        if (
            typeof payload.installed !== "boolean" ||
            typeof payload.enabled !== "boolean" ||
            typeof payload.active !== "boolean"
        ) {
            return false;
        }
        if (payload.service_result !== "success") {
            return false;
        }
        return Boolean(payload.installed && payload.enabled && payload.active);
    },

    _cacheIsStale: function() {
        if (!this._usages.length) {
            return false;
        }
        let nowMs = Date.now();
        let staleAfterMs = this._staleAfterMs();
        for (let i = 0; i < this._usages.length; i++) {
            let usage = this._usages[i];
            if (usage.stale) {
                return true;
            }
            let captured = this._dateMillis(usage.captured_at);
            if (
                captured === null ||
                this._captureIsTooFarInFuture(usage.captured_at, nowMs) ||
                nowMs - captured > staleAfterMs
            ) {
                return true;
            }
        }
        return false;
    },

    _cacheNeedsSync: function() {
        if (!this._usages.length) {
            return true;
        }
        let nowMs = Date.now();
        if (
            this._lastCacheSyncAt > 0 &&
            nowMs >= this._lastCacheSyncAt &&
            nowMs - this._lastCacheSyncAt < CACHE_SYNC_INTERVAL_MS
        ) {
            return false;
        }
        for (let i = 0; i < this._usages.length; i++) {
            let usage = this._usages[i];
            if (usage.stale) {
                return true;
            }
            let captured = this._dateMillis(usage.captured_at);
            if (
                captured === null ||
                this._captureIsTooFarInFuture(usage.captured_at, nowMs) ||
                nowMs - captured > CACHE_SYNC_INTERVAL_MS
            ) {
                return true;
            }
        }
        return false;
    },

    _staleAfterMs: function() {
        let interval = this._boundedInteger(this.refreshInterval, 60, 3600, 300);
        return (interval + 60) * 1000;
    },

    _repairStaleService: function(after) {
        if (this._removed || this._safeMode) {
            return;
        }
        let now = Date.now();
        if (now - this._serviceRepairAt < CIRCUIT_BREAKER_MS) {
            if (after) {
                this._runSafely("stale service continuation", after);
            }
            return;
        }
        this._serviceRepairAt = now;
        let generation = (this._staleCheckGeneration || 0) + 1;
        this._staleCheckGeneration = generation;
        this._removeSource("_staleCheckId");
        this._enableBackgroundService(after);
        if (this._removed || this._safeMode) {
            return;
        }
        try {
            let staleCheckId = Mainloop.timeout_add(60000, Lang.bind(this, function() {
                if (generation !== this._staleCheckGeneration) {
                    return false;
                }
                this._clearSource("_staleCheckId");
                if (this._removed || !this._cacheIsStale()) {
                    return false;
                }
                if (Date.now() - this._staleFallbackAt >= CIRCUIT_BREAKER_MS) {
                    this._staleFallbackAt = Date.now();
                    this._refreshFresh(false);
                }
                return false;
            }));
            if (!staleCheckId) {
                throw new Error("stale check source unavailable");
            }
            this._setSource("_staleCheckId", staleCheckId);
        } catch (e) {
            global.log("[" + UUID + "] stale check setup failed: " + this._shortText(e, 180));
            this._enterSafeMode("Stale-Prüfung konnte nicht eingerichtet werden");
        }
    },

    _loadAccountBackends: function() {
        if (
            this._removed || this._safeMode || this._backendChangeCurrent ||
            this._backendChangeQueue.length || this._accountChangeCurrent ||
            this._accountChangeQueue.length
        ) {
            return;
        }
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            return;
        }
        argv.push("account", "overview", "--format", "json", "--config-only");
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            if (error || !payload || !Array.isArray(payload.accounts)) {
                return;
            }
            if (payload.accounts.length > MAX_ACCOUNTS) {
                global.log("[" + UUID + "] too many accounts in backend overview");
                return;
            }
            let rows = [];
            let settingRows = [];
            let accounts = Object.create(null);
            for (let i = 0; i < payload.accounts.length; i++) {
                let item = payload.accounts[i];
                if (!item || typeof item !== "object" || Array.isArray(item)) {
                    global.log("[" + UUID + "] invalid account in backend overview");
                    return;
                }
                let account;
                let backend;
                let tag;
                let browser;
                let reactivationBrowser;
                let series;
                let seriesActive;
                let profileDir;
                let authJsonPath;
                let label;
                try {
                    account = this._strictText(item.id, 64);
                    tag = item.tag === undefined || item.tag === null
                        ? ""
                        : this._strictText(item.tag, 8);
                    backend = this._strictText(item.backend, 32);
                    browser = item.browser === undefined || item.browser === null
                        ? "firefox"
                        : this._strictText(item.browser, 32);
                    reactivationBrowser = item.reactivation_browser === undefined ||
                        item.reactivation_browser === null
                        ? "auto"
                        : this._strictText(item.reactivation_browser, 32);
                    series = item.series === undefined || item.series === null
                        ? ""
                        : this._strictText(item.series, 16).toUpperCase();
                    seriesActive = item.series_active === true;
                    profileDir = item.profile_dir === undefined || item.profile_dir === null
                        ? null
                        : this._strictText(item.profile_dir, 4096);
                    authJsonPath = item.auth_json_path === null || item.auth_json_path === undefined
                        ? null
                        : this._strictText(item.auth_json_path, 4096);
                    label = this._safeText(item.label, 120);
                    if (
                        item.series_active !== undefined &&
                        typeof item.series_active !== "boolean"
                    ) {
                        global.log("[" + UUID + "] invalid account in backend overview");
                        return;
                    }
                } catch (e) {
                    global.log("[" + UUID + "] invalid account in backend overview");
                    return;
                }
                if (!account || !/^[A-Za-z0-9_.-]{1,64}$/.test(account) ||
                    ["direct", "app-server"].indexOf(backend) === -1 ||
                    ["firefox", "chromium"].indexOf(browser) === -1 ||
                    ["auto", "vivaldi", "chromium", "firefox"].indexOf(reactivationBrowser) === -1 ||
                    (series && !/^[A-Z][A-Z0-9_-]{0,15}$/.test(series)) ||
                    (profileDir !== null && profileDir.length > 4096)) {
                    global.log("[" + UUID + "] invalid account in backend overview");
                    return;
                }
                if (Object.prototype.hasOwnProperty.call(accounts, account)) {
                    global.log("[" + UUID + "] duplicate account in backend overview");
                    return;
                }
                let row = {
                    account: account,
                    label: label || account,
                    ...(tag ? { tag: tag } : {}),
                    "auth-json": authJsonPath,
                    "profile-dir": profileDir,
                    "test-home": this._isTestHomeProfile(profileDir),
                    browser: browser === "chromium" ? 1 : 0,
                    "reactivation-browser": REACTIVATION_BROWSER_NAMES.indexOf(reactivationBrowser),
                    series: series,
                    "series-active": seriesActive,
                    backend: backend === "app-server" ? 1 : 0
                };
                let settingRow;
                try {
                    settingRow = {
                        account: row.account,
                        label: row.label,
                        tag: tag,
                        "auth-json": this._accountSettingPath(row["auth-json"]),
                        "profile-dir": this._accountSettingPath(row["profile-dir"]),
                        "test-home": row["test-home"],
                        browser: row.browser,
                        "reactivation-browser": row["reactivation-browser"],
                        series: row.series,
                        "series-active": row["series-active"],
                        backend: row.backend
                    };
                } catch (e) {
                    global.log("[" + UUID + "] invalid account path in backend overview");
                    return;
                }
                rows.push(row);
                settingRows.push(settingRow);
                accounts[account] = row;
            }
            let backendRefreshNeeded = false;
            let backendRefreshAccounts = Object.create(null);
            let previousAccounts = this._backendAccounts;
            for (let i = 0; i < this._usages.length; i++) {
                let usage = this._usages[i];
                let account = usage && accounts[usage.account];
                if (!account || !usage) {
                    continue;
                }
                let hasState = Boolean(
                    usage.backend_configured ||
                    usage.backend_used ||
                    usage.five_hour ||
                    usage.weekly
                );
                let expectedBackend = account.backend === 1 ? "app-server" : "direct";
                if (hasState && !this._backendMatchesConfigured(usage, expectedBackend)) {
                    backendRefreshNeeded = true;
                }
                let previous = previousAccounts && previousAccounts[usage.account];
                let previousBackend = previous && (
                    previous.backend === 1 ? "app-server" :
                    previous.backend === 0 ? "direct" : ""
                );
                let configured = this._safeBackend(usage.backend_configured);
                let used = this._safeBackend(usage.backend_used, true);
                let authenticated = used === "direct" || used === "app-server";
                if (
                    hasState &&
                    !configured &&
                    !authenticated &&
                    previousBackend &&
                    previousBackend !== expectedBackend
                ) {
                    backendRefreshNeeded = true;
                    backendRefreshAccounts[usage.account] = true;
                }
            }
            this._backendAccounts = accounts;
            this._backendRowsReady = true;
            this._cancelRemovedReactivations(accounts);
            let usageRowsChanged = this._ensureBackendUsageRows(backendRefreshAccounts);
            this._syncingBackendRows = true;
            try {
                this.accountBackends = settingRows;
                try {
                    this.settings.setValue("account-backends", settingRows);
                } catch (e) {
                    global.log("[" + UUID + "] backend settings sync failed: " + String(e));
                }
                this._syncStyleRows(rows);
                this._syncAccountSettings(rows);
                if (this._usages.length || usageRowsChanged) {
                    this._refreshFormattedSurfaces();
                }
            } finally {
                this._deferGuardRelease(
                    "_syncingBackendRows",
                    "backend sync guard cleanup"
                );
            }
            if (backendRefreshNeeded && !this._removed && !this._safeMode) {
                if (this._refreshing) {
                    this._primaryFreshPending = true;
                } else {
                    this._refreshFresh(false);
                }
            }
            this._loadRoutingState();
            this._migrateLegacyReactivationBrowser(rows);
            this._reconcilePendingAccountChanges();
            if (this._profileJobsResumeRequested) {
                this._profileJobsResumeRequested = false;
                this._loadProfileJobs();
            }
        }));
    },

    _loadRoutingState: function() {
        if (this._removed || this._safeMode || this._routingPolicyApplying) {
            return;
        }
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            return;
        }
        argv.push("policy", "status", "--role", "arbeitsbiene", "--format", "json");
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            if (error) {
                global.log("[" + UUID + "] routing status failed: " + this._shortText(error, 180));
                this._clearRoutingState();
                return;
            }
            let state;
            try {
                state = this._validateRoutingState(payload);
            } catch (e) {
                global.log("[" + UUID + "] invalid routing status: " + this._shortText(e, 180));
                this._clearRoutingState();
                return;
            }
            this._routingPolicy = state.policy;
            this._routingDecisions = state.decisions;
            this._syncRoutingSettings(state.policy);
            this._routingSettingsReady = true;
            this._refreshFormattedSurfaces();
        }));
    },

    _clearRoutingState: function() {
        let hadState = Boolean(
            this._routingPolicy !== null ||
            this._routingSettingsReady ||
            (this._routingDecisions && Object.keys(this._routingDecisions).length)
        );
        this._routingPolicy = null;
        this._routingDecisions = Object.create(null);
        this._routingSettingsReady = false;
        if (hadState) {
            this._refreshFormattedSurfaces();
        }
    },

    _validateRoutingState: function(payload) {
        if (!payload || payload.schema_version !== 1) {
            throw new Error("unsupported routing status");
        }
        let policy = this._validateRoutingPolicy(payload.policy);
        if (!payload.decisions || typeof payload.decisions !== "object" || Array.isArray(payload.decisions)) {
            throw new Error("invalid routing decisions");
        }
        let decisions = Object.create(null);
        let keys = Object.keys(payload.decisions);
        if (keys.length > MAX_ACCOUNTS) {
            throw new Error("too many routing decisions");
        }
        if (this._backendRowsReady) {
            let configuredAccounts = Object.keys(this._backendAccounts || {});
            if (keys.length !== configuredAccounts.length) {
                throw new Error("incomplete routing decisions");
            }
            for (let configuredIndex = 0; configuredIndex < configuredAccounts.length; configuredIndex++) {
                let configuredAccount = configuredAccounts[configuredIndex];
                if (!Object.prototype.hasOwnProperty.call(payload.decisions, configuredAccount)) {
                    throw new Error("incomplete routing decisions");
                }
            }
        }
        for (let i = 0; i < keys.length; i++) {
            let account = this._strictText(keys[i], 64);
            let value = payload.decisions[keys[i]];
            if (!account || !/^[A-Za-z0-9_.-]{1,64}$/.test(account) ||
                !value || typeof value !== "object" || Array.isArray(value)) {
                throw new Error("invalid routing decision");
            }
            if (this._backendRowsReady && !this._backendAccounts[account]) {
                throw new Error("unknown routing decision account");
            }
            let decision = this._strictText(value.decision, 32);
            if (["spark", "main", "credits", "blocked", "unchanged"].indexOf(decision) === -1) {
                throw new Error("invalid routing decision value");
            }
            let model = this._strictText(value.model, 120);
            let paidOverageAllowed = value.paid_overage_allowed;
            if (typeof paidOverageAllowed !== "boolean") {
                throw new Error("invalid routing credit flag");
            }
            if (decision === "credits" && paidOverageAllowed !== true) {
                throw new Error("credits decision without paid-overage approval");
            }
            if (
                (decision === "spark" && model !== "gpt-5.3-codex-spark") ||
                ((decision === "main" || decision === "credits") && model !== "gpt-5.4-mini") ||
                ((decision === "blocked" || decision === "unchanged") && model)
            ) {
                throw new Error("routing decision model mismatch");
            }
            let usageState = this._strictText(value.usage_state, 32);
            if (["known", "unknown", "not_applicable"].indexOf(usageState) === -1) {
                throw new Error("invalid routing usage state");
            }
            decisions[account] = {
                decision: decision,
                model: model,
                reason: this._safeText(value.reason, 120),
                paid_overage_allowed: paidOverageAllowed,
                policy_source: this._safeText(value.policy_source, 160),
                usage_state: usageState
            };
        }
        return { policy: policy, decisions: decisions };
    },

    _validateRoutingPolicy: function(value) {
        if (!value || value.schema_version !== 1 || typeof value.global !== "boolean") {
            throw new Error("invalid routing policy");
        }
        let policy = {
            schema_version: 1,
            global: value.global,
            credit_limits: { hourly: 0, weekly: 0, monthly: 0 },
            credit_limit_overrides: { account: Object.create(null), group: Object.create(null), agent: Object.create(null), job: Object.create(null) }
        };
        if (value.credit_limits !== undefined) {
            if (!value.credit_limits || typeof value.credit_limits !== "object" || Array.isArray(value.credit_limits)) {
                throw new Error("invalid routing credit limits");
            }
            ["hourly", "weekly", "monthly"].forEach(Lang.bind(this, function(key) {
                let number = value.credit_limits[key];
                if (number === null || number === undefined) number = 0;
                if (typeof number !== "number" || !Number.isFinite(number) || number < 0) {
                    throw new Error("invalid routing credit limit");
                }
                policy.credit_limits[key] = number;
            }));
        }
        ["account", "group", "agent", "job"].forEach(Lang.bind(this, function(scope) {
            let source = value[scope];
            if (!source || typeof source !== "object" || Array.isArray(source)) {
                throw new Error("invalid routing policy scope");
            }
            let keys = Object.keys(source);
            if (keys.length > 500) {
                throw new Error("too many routing policy rules");
            }
            policy[scope] = Object.create(null);
            for (let i = 0; i < keys.length; i++) {
                let identifier = this._routingIdentifier(keys[i]);
                if (typeof source[keys[i]] !== "boolean") {
                    throw new Error("invalid routing policy rule");
                }
                policy[scope][identifier] = source[keys[i]];
            }
            let limitSource = value.credit_limit_overrides && value.credit_limit_overrides[scope];
            if (limitSource === undefined) {
                limitSource = {};
            }
            if (!limitSource || typeof limitSource !== "object" || Array.isArray(limitSource) ||
                Object.keys(limitSource).length > 500) {
                throw new Error("invalid routing credit limit overrides");
            }
            Object.keys(limitSource).forEach(Lang.bind(this, function(identifier) {
                let normalizedIdentifier = this._routingIdentifier(identifier);
                let limits = limitSource[identifier];
                if (!limits || typeof limits !== "object" || Array.isArray(limits)) {
                    throw new Error("invalid routing credit limit override");
                }
                let normalized = {};
                ["hourly", "weekly", "monthly"].forEach(function(key) {
                    let number = limits[key];
                    if (number === null || number === undefined) {
                        normalized[key] = null;
                    } else if (typeof number === "number" && Number.isFinite(number) && number >= 0) {
                        normalized[key] = number;
                    } else {
                        throw new Error("invalid routing credit limit override");
                    }
                });
                if (normalized.hourly === null && normalized.weekly === null && normalized.monthly === null) {
                    throw new Error("empty routing credit limit override");
                }
                policy.credit_limit_overrides[scope][normalizedIdentifier] = normalized;
            }));
        }));
        return policy;
    },

    _routingIdentifier: function(value) {
        let identifier;
        try {
            identifier = this._strictText(value, 128);
        } catch (e) {
            throw new Error("invalid routing policy identifier");
        }
        if (!/^[A-Za-z0-9_.:@+\-]{1,128}$/.test(identifier)) {
            throw new Error("invalid routing policy identifier");
        }
        return identifier;
    },

    _syncRoutingSettings: function(policy) {
        let scopes = ["account", "group", "agent", "job"];
        let rows = [];
        for (let scopeIndex = 0; scopeIndex < scopes.length; scopeIndex++) {
            let scope = scopes[scopeIndex];
            let identifiers = Object.create(null);
            Object.keys(policy[scope]).forEach(function(identifier) { identifiers[identifier] = true; });
            Object.keys((policy.credit_limit_overrides || {})[scope] || {}).forEach(function(identifier) {
                identifiers[identifier] = true;
            });
            identifiers = Object.keys(identifiers).sort();
            for (let i = 0; i < identifiers.length; i++) {
                let identifier = identifiers[i];
                let limits = policy.credit_limit_overrides && policy.credit_limit_overrides[scope] &&
                    policy.credit_limit_overrides[scope][identifier] || {};
                rows.push({
                    scope: scopeIndex,
                    identifier: identifier,
                    enabled: Object.prototype.hasOwnProperty.call(policy[scope], identifier),
                    allow: policy[scope][identifier] === true,
                    "hourly-limit": Number(limits.hourly) > 0 ? Number(limits.hourly) : 0,
                    "weekly-limit": Number(limits.weekly) > 0 ? Number(limits.weekly) : 0,
                    "monthly-limit": Number(limits.monthly) > 0 ? Number(limits.monthly) : 0
                });
            }
        }
        this._syncingRoutingSettings = true;
        this.routingGlobalPaidCredits = policy.global;
        let limits = policy.credit_limits || {};
        this.routingCreditHourlyLimit = Number(limits.hourly) > 0 ? Number(limits.hourly) : 0;
        this.routingCreditWeeklyLimit = Number(limits.weekly) > 0 ? Number(limits.weekly) : 0;
        this.routingCreditMonthlyLimit = Number(limits.monthly) > 0 ? Number(limits.monthly) : 0;
        this.routingCreditOverrides = rows;
        try {
            this.settings.setValue("routing-global-paid-credits", policy.global);
            this.settings.setValue("routing-credit-overrides", rows);
            this.settings.setValue("routing-credit-hourly-limit", this.routingCreditHourlyLimit);
            this.settings.setValue("routing-credit-weekly-limit", this.routingCreditWeeklyLimit);
            this.settings.setValue("routing-credit-monthly-limit", this.routingCreditMonthlyLimit);
        } catch (e) {
            global.log("[" + UUID + "] routing settings sync failed: " + String(e));
        }
        this._deferGuardRelease("_syncingRoutingSettings", "routing settings guard cleanup");
    },

    _onRoutingSettingsChanged: function() {
        if (
            !this._routingSettingsReady || this._syncingRoutingSettings ||
            this._routingPolicyApplying || this._removed || this._safeMode
        ) {
            return;
        }
        let rows;
        try {
            rows = this._normalizeRoutingRows(this.routingCreditOverrides);
        } catch (e) {
            // Settings are externally writable. Never let a malformed row
            // escape from the callback; restore the authoritative MCP state.
            global.log("[" + UUID + "] invalid routing settings: " + this._shortText(e, 180));
            this._loadRoutingState();
            return;
        }
        let desired = {
            schema_version: 1,
            global: this.routingGlobalPaidCredits === true,
            account: Object.create(null),
            group: Object.create(null),
            agent: Object.create(null),
            job: Object.create(null)
        };
        desired.credit_limits = {
            hourly: this._routingLimitValue(this.routingCreditHourlyLimit),
            weekly: this._routingLimitValue(this.routingCreditWeeklyLimit),
            monthly: this._routingLimitValue(this.routingCreditMonthlyLimit)
        };
        desired.credit_limit_overrides = {
            account: Object.create(null),
            group: Object.create(null),
            agent: Object.create(null),
            job: Object.create(null)
        };
        let scopes = ["account", "group", "agent", "job"];
        for (let i = 0; i < rows.length; i++) {
            if (rows[i].enabled) {
                desired[scopes[rows[i].scope]][rows[i].identifier] = rows[i].allow;
            }
            let limits = {
                hourly: this._routingLimitValue(rows[i].hourly),
                weekly: this._routingLimitValue(rows[i].weekly),
                monthly: this._routingLimitValue(rows[i].monthly)
            };
            if (limits.hourly !== null || limits.weekly !== null || limits.monthly !== null) {
                desired.credit_limit_overrides[scopes[rows[i].scope]][rows[i].identifier] = limits;
            }
        }
        let commands = this._routingPolicyCommands(this._routingPolicy, desired);
        let limitCommands = this._routingCreditLimitCommands(this._routingPolicy, desired);
        if (!commands.length && !limitCommands.length) {
            return;
        }
        this._pendingRoutingLimitCommands = limitCommands;
        this._routingPolicyApplying = true;
        this._applyRoutingPolicyCommands(commands, 0);
    },

    _routingLimitValue: function(value) {
        let number = Number(value);
        return Number.isFinite(number) && number > 0 ? number : null;
    },

    _normalizeRoutingRows: function(rows) {
        if (!Array.isArray(rows) || rows.length > 500) {
            throw new Error("invalid routing policy rows");
        }
        let result = [];
        let seen = Object.create(null);
        for (let i = 0; i < rows.length; i++) {
            let row = rows[i];
            let scope = this._strictIntegerSetting(row && row.scope);
            if (!Number.isInteger(scope) || scope < 0 || scope > 3 ||
                typeof row.enabled !== "boolean" || typeof row.allow !== "boolean") {
                throw new Error("invalid routing policy row");
            }
            let identifier = this._routingIdentifier(row.identifier);
            let key = scope + ":" + identifier;
            if (seen[key]) {
                throw new Error("duplicate routing policy row");
            }
            seen[key] = true;
            result.push({
                scope: scope,
                identifier: identifier,
                enabled: row.enabled,
                allow: row.allow,
                hourly: this._routingLimitValue(row["hourly-limit"]),
                weekly: this._routingLimitValue(row["weekly-limit"]),
                monthly: this._routingLimitValue(row["monthly-limit"])
            });
        }
        return result;
    },

    _routingPolicyCommands: function(current, desired) {
        current = current || { global: false, account: {}, group: {}, agent: {}, job: {} };
        let commands = [];
        if (current.global !== desired.global) {
            commands.push(["global", desired.global ? "allow" : "deny"]);
        }
        ["account", "group", "agent", "job"].forEach(function(scope) {
            let identifiers = Object.create(null);
            Object.keys(current[scope] || {}).forEach(function(identifier) {
                identifiers[identifier] = true;
            });
            Object.keys(desired[scope]).forEach(function(identifier) {
                identifiers[identifier] = true;
            });
            Object.keys(identifiers).sort().forEach(function(identifier) {
                let before = current[scope] && current[scope][identifier];
                let after = desired[scope][identifier];
                if (after === undefined && before !== undefined) {
                    commands.push([scope, "inherit", identifier]);
                } else if (after !== undefined && after !== before) {
                    commands.push([scope, after ? "allow" : "deny", identifier]);
                }
            });
        });
        return commands;
    },

    _routingPolicyCommandApplied: function(policy, command) {
        if (!policy || !Array.isArray(command) || command.length < 2) {
            return false;
        }
        let scope = command[0];
        let action = command[1];
        let identifier = command[2];
        if (scope === "global") {
            if (action !== "allow" && action !== "deny" && action !== "inherit") {
                return false;
            }
            return policy.global === (action === "allow");
        }
        if (["account", "group", "agent", "job"].indexOf(scope) === -1 || !identifier) {
            return false;
        }
        if (action === "inherit") {
            return !Object.prototype.hasOwnProperty.call(policy[scope], identifier);
        }
        if (action !== "allow" && action !== "deny") {
            return false;
        }
        return policy[scope][identifier] === (action === "allow");
    },

    _applyRoutingPolicyCommands: function(commands, index) {
        if (this._removed || this._safeMode) {
            this._routingPolicyApplying = false;
            return;
        }
        if (index >= commands.length) {
            this._applyRoutingLimitCommands(this._pendingRoutingLimitCommands || [], 0);
            return;
        }
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            this._routingPolicyApplying = false;
            return;
        }
        argv.push("policy", "set", commands[index][0], commands[index][1]);
        if (commands[index][2]) {
            argv.push("--id", commands[index][2]);
        }
        argv.push("--format", "json");
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            let applied = false;
            if (!error) {
                try {
                    let policy = this._validateRoutingPolicy(payload);
                    applied = this._routingPolicyCommandApplied(policy, commands[index]);
                } catch (e) {
                    global.log("[" + UUID + "] invalid routing policy write result: " + this._shortText(e, 180));
                }
            }
            if (error || !applied) {
                this._routingPolicyApplying = false;
                this._showCommandError(
                    _("Routing-Richtlinie konnte nicht gespeichert werden: ") +
                    (error || _("ungültige Antwort"))
                );
                this._loadRoutingState();
                return;
            }
            this._applyRoutingPolicyCommands(commands, index + 1);
        }));
    },

    _routingCreditLimitCommands: function(current, desired) {
        current = current || { credit_limits: {}, credit_limit_overrides: {} };
        let commands = [];
        let currentGlobal = current.credit_limits || {};
        let desiredGlobal = desired.credit_limits || {};
        if (["hourly", "weekly", "monthly"].some(function(key) {
            return Number(currentGlobal[key] || 0) !== Number(desiredGlobal[key] || 0);
        })) {
            commands.push({ scope: "global", identifier: null, limits: desiredGlobal });
        }
        ["account", "group", "agent", "job"].forEach(function(scope) {
            let before = current.credit_limit_overrides && current.credit_limit_overrides[scope] || {};
            let after = desired.credit_limit_overrides && desired.credit_limit_overrides[scope] || {};
            let identifiers = Object.create(null);
            Object.keys(before).forEach(function(identifier) { identifiers[identifier] = true; });
            Object.keys(after).forEach(function(identifier) { identifiers[identifier] = true; });
            Object.keys(identifiers).sort().forEach(function(identifier) {
                let beforeLimits = before[identifier] || {};
                let afterLimits = after[identifier] || { hourly: null, weekly: null, monthly: null };
                if (["hourly", "weekly", "monthly"].some(function(key) {
                    return Number(beforeLimits[key] || 0) !== Number(afterLimits[key] || 0);
                })) {
                    commands.push({ scope: scope, identifier: identifier, limits: afterLimits });
                }
            });
        });
        return commands;
    },

    _routingCreditLimitCommandApplied: function(policy, command) {
        if (!policy || !command) {
            return false;
        }
        let actual = command.scope === "global"
            ? policy.credit_limits
            : policy.credit_limit_overrides && policy.credit_limit_overrides[command.scope] &&
                policy.credit_limit_overrides[command.scope][command.identifier];
        let expected = command.limits || {};
        return ["hourly", "weekly", "monthly"].every(function(key) {
            return Number((actual && actual[key]) || 0) === Number(expected[key] || 0);
        });
    },

    _applyRoutingLimitCommands: function(commands, index) {
        if (this._removed || this._safeMode) {
            this._routingPolicyApplying = false;
            return;
        }
        if (index >= commands.length) {
            this._routingPolicyApplying = false;
            this._pendingRoutingLimitCommands = [];
            this._loadRoutingState();
            return;
        }
        let command = commands[index];
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            this._routingPolicyApplying = false;
            return;
        }
        argv.push("policy", "set-limits", "--scope", command.scope);
        if (command.identifier) {
            argv.push("--id", command.identifier);
        }
        ["hourly", "weekly", "monthly"].forEach(function(key) {
            argv.push("--" + key, String(command.limits[key] === null || command.limits[key] === undefined ? 0 : command.limits[key]));
        });
        argv.push("--format", "json");
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            let applied = false;
            if (!error) {
                try {
                    let policy = this._validateRoutingPolicy(payload);
                    applied = this._routingCreditLimitCommandApplied(policy, command);
                } catch (e) {
                    global.log("[" + UUID + "] invalid credit limit write result: " + this._shortText(e, 180));
                }
            }
            if (error || !applied) {
                this._routingPolicyApplying = false;
                this._showCommandError(_("Credit-Limits konnten nicht gespeichert werden: ") +
                    (error || _("ungültige Antwort")));
                this._loadRoutingState();
                return;
            }
            this._applyRoutingLimitCommands(commands, index + 1);
        }));
    },

    _applyRoutingCreditLimits: function(limits) {
        this._applyRoutingLimitCommands([
            { scope: "global", identifier: null, limits: limits }
        ], 0);
    },

    _ensureBackendUsageRows: function(resetAccounts) {
        if (!this._backendRowsReady) {
            return false;
        }
        resetAccounts = resetAccounts || Object.create(null);
        let known = Object.create(null);
        let filtered = [];
        let changed = false;
        for (let i = 0; i < this._usages.length; i++) {
            let usage = this._usages[i];
            let account = usage && usage.account;
            if (
                !account || !this._backendAccounts[account] ||
                this._profilePendingAccounts[account] || known[account]
            ) {
                changed = true;
                continue;
            }
            known[account] = true;
            let expectedBackend = this._backendConfiguredForAccount(account);
            if (resetAccounts[account] || !this._backendMatchesConfigured(usage, expectedBackend)) {
                filtered.push(this._newBackendUsageRow(account, expectedBackend));
                changed = true;
                continue;
            }
            filtered.push(usage);
        }
        let accounts = Object.keys(this._backendAccounts);
        for (let i = 0; i < accounts.length; i++) {
            let account = accounts[i];
            if (known[account] || this._profilePendingAccounts[account]) {
                continue;
            }
            filtered.push(this._newBackendUsageRow(
                account,
                this._backendConfiguredForAccount(account)
            ));
            changed = true;
        }
        if (changed) {
            this._usages = filtered;
        }
        return changed;
    },

    _backendConfiguredForAccount: function(account) {
        return this._backendAccounts[account] && this._backendAccounts[account].backend === 1
            ? "app-server"
            : "direct";
    },

    _newBackendUsageRow: function(account, backend) {
        return {
            account: account,
            label: this._backendAccounts[account].label || account,
            captured_at: "",
            five_hour: null,
            weekly: null,
            credits: null,
            main: null,
            models: Object.create(null),
            cost_windows: [],
            usage_resets: { available: null, known: false, redeem_capability: false },
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
        };
    },

    _backendMatchesConfigured: function(usage, configuredBackend) {
        let configured = this._safeBackend(usage && usage.backend_configured);
        if (!configured) {
            return !this._hasCachedWindows(usage);
        }
        if (configured && configured !== configuredBackend) {
            return false;
        }
        let used = this._safeBackend(usage && usage.backend_used, true);
        if (["direct", "app-server"].indexOf(used) === -1) {
            return !this._hasCachedWindows(usage);
        }
        if (used === configuredBackend) {
            return true;
        }
        return configuredBackend === "app-server" && this._hasBackendFallbackProof(usage);
    },

    _syncAccountSettings: function(accounts) {
        let panelRows = this._mergedPanelRows(accounts, this.accountPanelSettings);
        let consumptionRows = this._mergedConsumptionRows(
            accounts,
            this.accountConsumptionSettings
        );
        let forecastRows = this._mergedForecastRows(
            accounts,
            this.accountForecastSettings,
            this.accountConsumptionSettings
        );
        let creditRows = this._mergedCreditRows(accounts, this.accountCreditSettings);
        let creditConsumptionRows = this._mergedCreditConsumptionRows(
            accounts,
            this.accountCreditConsumptionSettings,
            this.accountCreditSettings
        );
        let resetRows = this._mergedResetRows(accounts, this.accountResetDisplaySettings);
        let alertRows = this._mergedAlertRows(accounts, this.accountAlertSettings);
        let panelChanged = !this._styleRowsEqual(this.accountPanelSettings, panelRows);
        let consumptionChanged = !this._styleRowsEqual(
            this.accountConsumptionSettings,
            consumptionRows.map(Lang.bind(this, this._consumptionStorageRow))
        );
        let forecastChanged = !this._styleRowsEqual(this.accountForecastSettings, forecastRows);
        let creditChanged = !this._styleRowsEqual(
            this.accountCreditSettings,
            creditRows.map(Lang.bind(this, this._creditStorageRow))
        );
        let creditConsumptionChanged = !this._styleRowsEqual(
            this.accountCreditConsumptionSettings,
            creditConsumptionRows
        );
        let resetChanged = !this._styleRowsEqual(
            this.accountResetDisplaySettings,
            resetRows
        );
        let alertChanged = !this._styleRowsEqual(this.accountAlertSettings, alertRows);
        this._panelSettings = this._panelSettingsMap(panelRows);
        this._consumptionSettings = this._consumptionSettingsMap(
            this._combineConsumptionRows(consumptionRows, forecastRows)
        );
        this._creditSettings = this._creditSettingsMap(
            this._combineCreditRows(creditRows, creditConsumptionRows)
        );
        this._resetSettings = this._resetSettingsMap(resetRows);
        this._alertSettings = this._alertSettingsMap(alertRows);
        this._syncingAccountSettings = true;
        this.accountPanelSettings = panelRows;
        this.accountConsumptionSettings = consumptionRows.map(
            Lang.bind(this, this._consumptionStorageRow)
        );
        this.accountForecastSettings = forecastRows;
        this.accountCreditSettings = creditRows.map(
            Lang.bind(this, this._creditStorageRow)
        );
        this.accountCreditConsumptionSettings = creditConsumptionRows;
        this.accountResetDisplaySettings = resetRows;
        this.accountAlertSettings = alertRows;
        try {
            if (panelChanged) {
                this.settings.setValue("account-panel-settings", panelRows);
            }
            if (consumptionChanged) {
                this.settings.setValue("account-consumption-settings", this.accountConsumptionSettings);
            }
            if (forecastChanged) {
                this.settings.setValue("account-forecast-settings", forecastRows);
            }
            if (creditChanged) {
                this.settings.setValue("account-credit-settings", this.accountCreditSettings);
            }
            if (creditConsumptionChanged) {
                this.settings.setValue("account-credit-consumption-settings", creditConsumptionRows);
            }
            if (resetChanged) {
                this.settings.setValue("account-reset-display-settings", resetRows);
            }
            if (alertChanged) {
                this.settings.setValue("account-alert-settings", alertRows);
            }
        } catch (e) {
            global.log("[" + UUID + "] account settings sync failed: " + String(e));
        }
        this._deferGuardRelease(
            "_syncingAccountSettings",
            "account settings guard cleanup"
        );
    },

    _mergedPanelRows: function(accounts, currentRows) {
        let current = Object.create(null);
        let seen = Object.create(null);
        if (Array.isArray(currentRows)) {
            for (let i = 0; i < currentRows.length; i++) {
                let account = this._configuredAccountId(currentRows[i] && currentRows[i].account);
                if (!account || seen[account] || !this._backendAccounts[account]) {
                    continue;
                }
                seen[account] = true;
                let normalized = this._normalizePanelRow(currentRows[i], account);
                if (normalized) {
                    current[account] = normalized;
                }
            }
        }
        let configuredOrder = Object.create(null);
        if (Array.isArray(this.accountBackends)) {
            for (let i = 0; i < this.accountBackends.length; i++) {
                let configuredAccount = this._configuredAccountId(
                    this.accountBackends[i] && this.accountBackends[i].account
                );
                if (configuredAccount && configuredOrder[configuredAccount] === undefined) {
                    configuredOrder[configuredAccount] = i;
                }
            }
        }
        let orderedAccounts = accounts.slice().sort(function(left, right) {
            let leftOrder = configuredOrder[left.account];
            let rightOrder = configuredOrder[right.account];
            if (leftOrder === undefined && rightOrder === undefined) return 0;
            if (leftOrder === undefined) return 1;
            if (rightOrder === undefined) return -1;
            return leftOrder - rightOrder;
        });
        let rows = [];
        for (let i = 0; i < orderedAccounts.length; i++) {
            let account = orderedAccounts[i].account;
            rows.push(current[account] || this._defaultPanelRow(account, i + 1));
        }
        return rows;
    },

    _defaultPanelRow: function(account, order) {
        return {
            account: account,
            order: order,
            muted: false,
            slot1: this._panelSourceValue(this.panelPercentSource),
            slot2: 0,
            slot3: 0,
            slot4: 0
        };
    },

    _mergedConsumptionRows: function(accounts, currentRows) {
        let current = Object.create(null);
        let seen = Object.create(null);
        if (Array.isArray(currentRows)) {
            for (let i = 0; i < currentRows.length; i++) {
                let account = this._configuredAccountId(
                    currentRows[i] && currentRows[i].account
                );
                if (!account || seen[account] || !this._backendAccounts[account]) {
                    continue;
                }
                seen[account] = true;
                let normalized = this._normalizeConsumptionRow(currentRows[i], account);
                if (normalized) {
                    current[account] = normalized;
                }
            }
        }
        return accounts.map(Lang.bind(this, function(account) {
            return current[account.account] || this._defaultConsumptionRow(account.account);
        }));
    },

    _mergedForecastRows: function(accounts, currentRows, legacyRows) {
        let current = Object.create(null);
        let seen = Object.create(null);
        let legacyForecastFields = [
            "forecast-show-panel", "forecast-show-tooltip", "forecast-limit-window",
            "forecast-format", "forecast-custom-format", "forecast-smoothing",
            "forecast-hide-when-zero", "forecast-warn-amount", "forecast-warn-unit",
            "forecast-warn-format", "forecast-show-coverage-marker",
            "forecast-baseline-enabled", "forecast-baseline-minutes"
        ];
        let sources = [];
        if (Array.isArray(currentRows)) sources.push(currentRows);
        if (Array.isArray(legacyRows)) sources.push(legacyRows);
        for (let source of sources) {
            for (let row of source) {
                let account = this._configuredAccountId(row && row.account);
                if (!account || seen[account] || !this._backendAccounts[account]) continue;
                let isCurrentForecastTable = source === currentRows;
                if (isCurrentForecastTable ||
                    legacyForecastFields.some(function(key) { return row[key] !== undefined; })) {
                    seen[account] = true;
                    let normalizationRow = row;
                    if (!isCurrentForecastTable) {
                        normalizationRow = {account: account};
                        legacyForecastFields.forEach(function(key) {
                            if (row[key] !== undefined) {
                                normalizationRow[key.slice("forecast-".length)] = row[key];
                            }
                        });
                    }
                    current[account] = this._normalizeForecastRow(normalizationRow, account);
                }
            }
        }
        return accounts.map(Lang.bind(this, function(account) {
            return current[account.account] || this._defaultForecastRow(account.account);
        }));
    },

    _defaultForecastRow: function(account) {
        return { account: account, "show-panel": false, "show-tooltip": true,
            "limit-window": "short", format: "compact", "custom-format": "",
            smoothing: "ema-20", "hide-when-zero": false,
            "warn-amount": 2, "warn-unit": "hours", "warn-format": "red-yellow",
            "show-coverage-marker": true, "baseline-enabled": false, "baseline-minutes": 60 };
    },

    _normalizeForecastRow: function(row, account) {
        let fallback = this._defaultForecastRow(account);
        let source = row || {};
        let format = source.format === undefined
            ? (source["forecast-format"] === undefined ? fallback.format : source["forecast-format"])
            : source.format;
        let customFormat = source["custom-format"] === undefined
            ? (source["forecast-custom-format"] === undefined ? "" : source["forecast-custom-format"])
            : source["custom-format"];
        let smoothing = source.smoothing === undefined
            ? (source["forecast-smoothing"] === undefined ? fallback.smoothing : source["forecast-smoothing"])
            : source.smoothing;
        let limitWindow = source["limit-window"] === undefined
            ? (source["forecast-limit-window"] === undefined
                ? fallback["limit-window"] : source["forecast-limit-window"])
            : source["limit-window"];
        let showPanel = source["show-panel"] === undefined
            ? (source["forecast-show-panel"] === undefined ? fallback["show-panel"] : source["forecast-show-panel"]) : source["show-panel"];
        let showTooltip = source["show-tooltip"] === undefined
            ? (source["forecast-show-tooltip"] === undefined ? true : source["forecast-show-tooltip"]) : source["show-tooltip"];
        let hideWhenZero = source["hide-when-zero"] === undefined
            ? (source["forecast-hide-when-zero"] === undefined ? false : source["forecast-hide-when-zero"]) : source["hide-when-zero"];
        let coverage = source["show-coverage-marker"] === undefined
            ? (source["forecast-show-coverage-marker"] === undefined ? true : source["forecast-show-coverage-marker"]) : source["show-coverage-marker"];
        let baselineEnabled = source["baseline-enabled"] === undefined
            ? (source["forecast-baseline-enabled"] === undefined ? false : source["forecast-baseline-enabled"]) : source["baseline-enabled"];
        let baselineMinutes = source["baseline-minutes"] === undefined
            ? (source["forecast-baseline-minutes"] === undefined ? fallback["baseline-minutes"] : source["forecast-baseline-minutes"])
            : source["baseline-minutes"];
        let warnAmount = source["warn-amount"] === undefined
            ? (source["forecast-warn-amount"] === undefined ? 2 : source["forecast-warn-amount"])
            : source["warn-amount"];
        let warnUnit = source["warn-unit"] === undefined
            ? (source["forecast-warn-unit"] === undefined ? "hours" : source["forecast-warn-unit"])
            : source["warn-unit"];
        let warnFormat = source["warn-format"] === undefined
            ? (source["forecast-warn-format"] === undefined ? "red-yellow" : source["forecast-warn-format"])
            : source["warn-format"];
        format = this._strictText(format, 16);
        customFormat = this._strictText(customFormat, 160);
        smoothing = this._strictText(smoothing, 16);
        limitWindow = this._strictText(limitWindow, 16);
        warnUnit = this._strictText(warnUnit, 16);
        warnFormat = this._strictText(warnFormat, 32);
        baselineMinutes = this._strictIntegerSetting(baselineMinutes);
        warnAmount = this._strictIntegerSetting(warnAmount);
        if (
            !account || typeof showPanel !== "boolean" || typeof showTooltip !== "boolean" ||
            typeof hideWhenZero !== "boolean" || typeof coverage !== "boolean" ||
            typeof baselineEnabled !== "boolean" || !Number.isInteger(baselineMinutes) ||
            baselineMinutes < 0 || baselineMinutes > 9999 || !Number.isInteger(warnAmount) ||
            warnAmount < 0 || warnAmount > 365 ||
            ["short", "weekly", "monthly", "spark"].indexOf(limitWindow) === -1 ||
            ["compact", "compact-minutes", "verbose", "custom"].indexOf(format) === -1 ||
            ["none", "ema-5", "ema-10", "ema-20", "ema-40", "ema-80", "ema-160", "ema-320", "ema-640"].indexOf(smoothing) === -1 ||
            ["minutes", "hours", "days", "weeks"].indexOf(warnUnit) === -1 ||
            ["none", "red", "red-yellow", "blink-red-yellow", "yellow", "red-green", "red-red"].indexOf(warnFormat) === -1
        ) {
            return null;
        }
        return { account: account, "show-panel": showPanel, "show-tooltip": showTooltip,
            "limit-window": limitWindow, format: format, "custom-format": customFormat,
            smoothing: smoothing, "hide-when-zero": hideWhenZero,
            "warn-amount": warnAmount, "warn-unit": warnUnit, "warn-format": warnFormat,
            "show-coverage-marker": coverage, "baseline-enabled": baselineEnabled,
            "baseline-minutes": baselineMinutes };
    },

    _mergedCreditConsumptionRows: function(accounts, currentRows, legacyRows) {
        let current = Object.create(null);
        let seen = Object.create(null);
        let legacyConsumptionFields = [
            "consumption-show-panel", "consumption-show-tooltip", "consumption-amount",
            "consumption-unit", "consumption-format", "consumption-custom-format",
            "consumption-smoothing", "consumption-hide-when-zero",
            "consumption-show-coverage-marker", "consumption-baseline-enabled",
            "consumption-baseline-minutes"
        ];
        let sources = [];
        if (Array.isArray(currentRows)) sources.push(currentRows);
        if (Array.isArray(legacyRows)) sources.push(legacyRows);
        for (let source of sources) {
            for (let row of source) {
                let account = this._configuredAccountId(row && row.account);
                if (!account || seen[account] || !this._backendAccounts[account]) continue;
                if (currentRows === source ||
                    legacyConsumptionFields.some(function(key) { return row[key] !== undefined; })) {
                    seen[account] = true;
                    let normalizationRow = row;
                    if (currentRows !== source) {
                        normalizationRow = {account: account};
                        legacyConsumptionFields.forEach(function(key) {
                            if (row[key] !== undefined) {
                                normalizationRow[key.slice("consumption-".length)] = row[key];
                            }
                        });
                    }
                    current[account] = this._normalizeCreditConsumptionRow(normalizationRow, account);
                }
            }
        }
        return accounts.map(Lang.bind(this, function(account) {
            return current[account.account] || this._defaultCreditConsumptionRow(account.account);
        }));
    },

    _defaultCreditConsumptionRow: function(account) {
        return { account: account, "show-panel": false, "show-tooltip": true,
            amount: 1, unit: "hours", format: "compact", "custom-format": "",
            smoothing: "ema-20",
            "hide-when-zero": false, "show-coverage-marker": true,
            "baseline-enabled": false, "baseline-minutes": 60 };
    },

    _normalizeCreditConsumptionRow: function(row, account) {
        let fallback = this._defaultCreditConsumptionRow(account);
        let source = row || {};
        let showPanel = source["show-panel"] === undefined
                ? (source["consumption-show-panel"] === undefined ? false : source["consumption-show-panel"]) : source["show-panel"],
            showTooltip = source["show-tooltip"] === undefined
                ? (source["consumption-show-tooltip"] === undefined ? true : source["consumption-show-tooltip"]) : source["show-tooltip"];
        let amount = source.amount === undefined ? (source["consumption-amount"] === undefined ? 1 : source["consumption-amount"]) : source.amount;
        let unit = source.unit === undefined
            ? (source["consumption-unit"] === undefined ? fallback.unit : source["consumption-unit"])
            : source.unit;
        let format = source.format === undefined
            ? (source["consumption-format"] === undefined ? fallback.format : source["consumption-format"])
            : source.format;
        let customFormat = source["custom-format"] === undefined
            ? (source["consumption-custom-format"] === undefined ? "" : source["consumption-custom-format"])
            : source["custom-format"];
        let smoothing = source.smoothing === undefined
            ? (source["consumption-smoothing"] === undefined
                ? fallback.smoothing : source["consumption-smoothing"])
            : source.smoothing;
        let hideWhenZero = source["hide-when-zero"] === undefined
            ? (source["consumption-hide-when-zero"] === undefined ? false : source["consumption-hide-when-zero"]) : source["hide-when-zero"];
        let coverage = source["show-coverage-marker"] === undefined
            ? (source["consumption-show-coverage-marker"] === undefined ? true : source["consumption-show-coverage-marker"]) : source["show-coverage-marker"];
        let baselineEnabled = source["baseline-enabled"] === undefined
            ? (source["consumption-baseline-enabled"] === undefined ? false : source["consumption-baseline-enabled"]) : source["baseline-enabled"];
        let baselineMinutes = source["baseline-minutes"] === undefined
            ? (source["consumption-baseline-minutes"] === undefined ? fallback["baseline-minutes"] : source["consumption-baseline-minutes"])
            : source["baseline-minutes"];
        amount = this._strictIntegerSetting(amount);
        unit = this._strictText(unit, 16);
        format = this._strictText(format, 16);
        customFormat = this._strictText(customFormat, 200);
        smoothing = this._strictText(smoothing, 16);
        baselineMinutes = this._strictIntegerSetting(baselineMinutes);
        if (
            !account || typeof showPanel !== "boolean" || typeof showTooltip !== "boolean" ||
            !Number.isInteger(amount) || amount < 1 || amount > 365 ||
            ["minutes", "hours", "days", "weeks"].indexOf(unit) === -1 ||
            ["compact", "verbose", "custom"].indexOf(format) === -1 ||
            ["none", "ema-5", "ema-10", "ema-20", "ema-40", "ema-80", "ema-160", "ema-320", "ema-640"].indexOf(smoothing) === -1 ||
            typeof hideWhenZero !== "boolean" || typeof coverage !== "boolean" ||
            typeof baselineEnabled !== "boolean" || !Number.isInteger(baselineMinutes) ||
            baselineMinutes < 0 || baselineMinutes > 9999
        ) {
            return null;
        }
        return { account: account, "show-panel": showPanel,
            "show-tooltip": showTooltip, amount: amount, unit: unit, format: format,
            "custom-format": customFormat, smoothing: smoothing,
            "hide-when-zero": hideWhenZero, "show-coverage-marker": coverage,
            "baseline-enabled": baselineEnabled, "baseline-minutes": baselineMinutes };
    },

    _combineConsumptionRows: function(rows, forecasts) {
        let byAccount = Object.create(null);
        for (let row of forecasts) byAccount[row.account] = row;
        return rows.map(function(row) {
            let forecast = byAccount[row.account] || {};
            return Object.assign({}, row, {
                "forecast-show-panel": forecast["show-panel"],
                "forecast-show-tooltip": forecast["show-tooltip"],
                "forecast-limit-window": forecast["limit-window"],
                "forecast-format": forecast.format,
                "forecast-custom-format": forecast["custom-format"],
                "forecast-smoothing": forecast.smoothing,
                "forecast-hide-when-zero": forecast["hide-when-zero"],
                "forecast-warn-amount": forecast["warn-amount"],
                "forecast-warn-unit": forecast["warn-unit"],
                "forecast-warn-format": forecast["warn-format"],
                "forecast-show-coverage-marker": forecast["show-coverage-marker"],
                "forecast-baseline-enabled": forecast["baseline-enabled"],
                "forecast-baseline-minutes": forecast["baseline-minutes"]
            });
        });
    },

    _combineCreditRows: function(rows, consumptions) {
        let byAccount = Object.create(null);
        for (let row of consumptions) byAccount[row.account] = row;
        return rows.map(function(row) {
            let consumption = byAccount[row.account] || {};
            return Object.assign({}, row, {
                "consumption-show-panel": consumption["show-panel"],
                "consumption-show-tooltip": consumption["show-tooltip"],
                "consumption-amount": consumption.amount,
                "consumption-unit": consumption.unit,
                "consumption-format": consumption.format,
                "consumption-custom-format": consumption["custom-format"],
                "consumption-smoothing": consumption.smoothing,
                "consumption-hide-when-zero": consumption["hide-when-zero"],
                "consumption-show-coverage-marker": consumption["show-coverage-marker"],
                "consumption-baseline-enabled": consumption["baseline-enabled"],
                "consumption-baseline-minutes": consumption["baseline-minutes"]
            });
        });
    },

    _consumptionStorageRow: function(row) {
        let copy = Object.assign({}, row);
        ["forecast-show-panel", "forecast-show-tooltip", "forecast-limit-window", "forecast-format",
            "forecast-custom-format", "forecast-smoothing", "forecast-hide-when-zero", "forecast-warn-amount", "forecast-warn-unit", "forecast-warn-format",
            "forecast-show-coverage-marker", "forecast-baseline-enabled", "forecast-baseline-minutes"].forEach(function(key) {
            delete copy[key];
        });
        return copy;
    },

    _creditStorageRow: function(row) {
        let copy = Object.assign({}, row);
        ["consumption-show-panel", "consumption-show-tooltip", "consumption-amount", "consumption-unit",
            "consumption-format", "consumption-custom-format", "consumption-smoothing", "consumption-hide-when-zero",
            "consumption-show-coverage-marker", "consumption-baseline-enabled",
            "consumption-baseline-minutes"].forEach(function(key) { delete copy[key]; });
        return copy;
    },

    _defaultConsumptionRow: function(account) {
        return {
            account: account,
            "show-panel": false,
            "show-tooltip": true,
            "forecast-show-panel": false,
            "forecast-show-tooltip": true,
            amount: 1,
            unit: "hours",
            "baseline-enabled": false,
            "baseline-minutes": 60,
            smoothing: "ema-10",
            "limit-window": "short",
            format: "compact",
            "custom-format": "",
            "forecast-limit-window": "short",
            "forecast-format": "compact",
            "forecast-custom-format": "",
            "forecast-smoothing": "ema-20",
            "forecast-warn-amount": 2,
            "forecast-warn-unit": "hours",
            "forecast-warn-format": "red-yellow",
            "hide-when-zero": false,
            "show-coverage-marker": true
        };
    },

    _mergedCreditRows: function(accounts, currentRows) {
        let current = Object.create(null);
        let seen = Object.create(null);
        if (Array.isArray(currentRows)) {
            for (let i = 0; i < currentRows.length; i++) {
                let account = this._configuredAccountId(currentRows[i] && currentRows[i].account);
                if (!account || seen[account] || !this._backendAccounts[account]) {
                    continue;
                }
                seen[account] = true;
                let normalized = this._normalizeCreditRow(currentRows[i], account);
                if (normalized) {
                    current[account] = normalized;
                }
            }
        }
        return accounts.map(Lang.bind(this, function(account) {
            return current[account.account] || this._defaultCreditRow(account.account);
        }));
    },

    _defaultCreditRow: function(account) {
        return { account: account, "show-panel": false, "show-tooltip": true,
            format: "compact", "custom-format": "", "hide-when-zero": false,
            smoothing: "ema-20",
            "show-coverage-marker": true, "baseline-enabled": false, "baseline-minutes": 60,
            "consumption-show-panel": false, "consumption-show-tooltip": true,
            "consumption-amount": 1, "consumption-unit": "hours",
            "consumption-format": "compact", "consumption-custom-format": "",
            "consumption-smoothing": "ema-20",
            "consumption-hide-when-zero": false,
            "consumption-show-coverage-marker": true,
            "consumption-baseline-enabled": false,
            "consumption-baseline-minutes": 60 };
    },

    _normalizeCreditRow: function(row, account) {
        if (!row || typeof row !== "object" || !account ||
            typeof row["show-tooltip"] !== "boolean" ||
            typeof row["hide-when-zero"] !== "boolean") {
            return null;
        }
        let showPanel = row["show-panel"] === undefined ? false : row["show-panel"];
        let format = this._strictText(row.format, 16);
        let smoothing = row.smoothing === undefined ? "ema-20" : this._strictText(row.smoothing, 16);
        let baselineMinutes = row["baseline-minutes"] === undefined
            ? 60 : this._strictIntegerSetting(row["baseline-minutes"]);
        let consumptionAmount = row["consumption-amount"] === undefined
            ? 1 : this._strictIntegerSetting(row["consumption-amount"]);
        let consumptionUnit = row["consumption-unit"] === undefined
            ? "hours" : this._strictText(row["consumption-unit"], 16);
        let consumptionFormat = row["consumption-format"] === undefined
            ? "compact" : this._strictText(row["consumption-format"], 16);
        let customFormat = row["custom-format"] === undefined ? "" : row["custom-format"];
        let consumptionCustomFormat = row["consumption-custom-format"] === undefined
            ? "" : row["consumption-custom-format"];
        if (["compact", "verbose", "custom"].indexOf(format) === -1 ||
            typeof showPanel !== "boolean" ||
            ["none", "ema-5", "ema-10", "ema-20", "ema-40", "ema-80", "ema-160", "ema-320", "ema-640"].indexOf(smoothing) === -1 ||
            !Number.isInteger(consumptionAmount) || consumptionAmount < 1 || consumptionAmount > 365 ||
            (row["show-coverage-marker"] !== undefined && typeof row["show-coverage-marker"] !== "boolean") ||
            (row["baseline-enabled"] !== undefined && typeof row["baseline-enabled"] !== "boolean") ||
            !Number.isInteger(baselineMinutes) || baselineMinutes < 0 || baselineMinutes > 9999 ||
            ["minutes", "hours", "days", "weeks"].indexOf(consumptionUnit) === -1 ||
            ["compact", "verbose", "custom"].indexOf(consumptionFormat) === -1 ||
            (row["consumption-show-panel"] !== undefined && typeof row["consumption-show-panel"] !== "boolean") ||
            (row["consumption-show-tooltip"] !== undefined && typeof row["consumption-show-tooltip"] !== "boolean") ||
            (row["consumption-hide-when-zero"] !== undefined && typeof row["consumption-hide-when-zero"] !== "boolean") ||
            (row["consumption-show-coverage-marker"] !== undefined && typeof row["consumption-show-coverage-marker"] !== "boolean") ||
            (row["consumption-baseline-enabled"] !== undefined && typeof row["consumption-baseline-enabled"] !== "boolean") ||
            (row["consumption-baseline-minutes"] !== undefined && (!Number.isInteger(row["consumption-baseline-minutes"]) || row["consumption-baseline-minutes"] < 0 || row["consumption-baseline-minutes"] > 9999)) ||
            typeof customFormat !== "string" || customFormat.length > 200 ||
            typeof consumptionCustomFormat !== "string" || consumptionCustomFormat.length > 200) {
            return null;
        }
        return { account: account, "show-panel": showPanel, "show-tooltip": row["show-tooltip"],
            format: format, "custom-format": this._strictText(customFormat, 200),
            smoothing: smoothing,
            "hide-when-zero": row["hide-when-zero"],
            "show-coverage-marker": row["show-coverage-marker"] !== false,
            "baseline-enabled": row["baseline-enabled"] === true,
            "baseline-minutes": baselineMinutes,
            "consumption-show-panel": row["consumption-show-panel"] === true,
            "consumption-show-tooltip": row["consumption-show-tooltip"] !== false,
            "consumption-amount": consumptionAmount,
            "consumption-unit": consumptionUnit,
            "consumption-format": consumptionFormat,
            "consumption-custom-format": this._strictText(consumptionCustomFormat, 200),
            "consumption-hide-when-zero": row["consumption-hide-when-zero"] === true,
            "consumption-show-coverage-marker": row["consumption-show-coverage-marker"] !== false,
            "consumption-baseline-enabled": row["consumption-baseline-enabled"] === true,
            "consumption-baseline-minutes": row["consumption-baseline-minutes"] === undefined ? 60 : row["consumption-baseline-minutes"] };
    },

    _creditSettingsMap: function(rows) {
        let result = Object.create(null);
        for (let i = 0; i < rows.length; i++) result[rows[i].account] = rows[i];
        return result;
    },

    _normalizeConsumptionRow: function(row, account) {
        if (!row || typeof row !== "object" || Array.isArray(row)) {
            return null;
        }
        let amount = this._strictIntegerSetting(row.amount);
        let unit = this._strictText(row.unit, 16);
        let baselineEnabled = row["baseline-enabled"] === undefined
            ? false : row["baseline-enabled"];
        let baselineMinutes = row["baseline-minutes"] === undefined
            ? 60 : this._strictIntegerSetting(row["baseline-minutes"]);
        let smoothing = row.smoothing === undefined ? "ema-10" : this._strictText(row.smoothing, 16);
        let limitWindow = this._strictText(row["limit-window"], 16);
        let format = this._strictText(row.format, 16);
        let customFormat = row["custom-format"] === undefined
            ? ""
            : this._strictText(row["custom-format"], 160);
        let showPanel = row["show-panel"] === undefined ? false : row["show-panel"];
        let forecastShowPanel = row["forecast-show-panel"] === undefined ? showPanel : row["forecast-show-panel"];
        let forecastShowTooltip = row["forecast-show-tooltip"] === undefined ? row["show-tooltip"] : row["forecast-show-tooltip"];
        let forecastFormat = row["forecast-format"] === undefined ? "compact" : this._strictText(row["forecast-format"], 16);
        let forecastCustomFormat = row["forecast-custom-format"] === undefined ? "" : this._strictText(row["forecast-custom-format"], 160);
        let forecastSmoothing = row["forecast-smoothing"] === undefined ? "ema-20" : this._strictText(row["forecast-smoothing"], 16);
        let forecastHideWhenZero = row["forecast-hide-when-zero"] === undefined ? false : row["forecast-hide-when-zero"];
        let forecastWarnAmount = row["forecast-warn-amount"] === undefined
            ? 2 : this._strictIntegerSetting(row["forecast-warn-amount"]);
        let forecastWarnUnit = row["forecast-warn-unit"] === undefined
            ? "hours" : this._strictText(row["forecast-warn-unit"], 16);
        let forecastWarnFormat = row["forecast-warn-format"] === undefined
            ? "red-yellow" : this._strictText(row["forecast-warn-format"], 32);
        let forecastLimitWindow = row["forecast-limit-window"] === undefined
            ? limitWindow
            : this._strictText(row["forecast-limit-window"], 16);
        let forecastBaselineEnabled = row["forecast-baseline-enabled"] === undefined
            ? false : row["forecast-baseline-enabled"];
        let forecastBaselineMinutes = row["forecast-baseline-minutes"] === undefined
            ? 60 : this._strictIntegerSetting(row["forecast-baseline-minutes"]);
        let forecastShowCoverage = row["forecast-show-coverage-marker"] === undefined
            ? true : row["forecast-show-coverage-marker"];
        if (
            typeof showPanel !== "boolean" ||
            typeof row["show-tooltip"] !== "boolean" ||
            typeof baselineEnabled !== "boolean" ||
            !Number.isInteger(baselineMinutes) || baselineMinutes < 0 || baselineMinutes > 9999 ||
            typeof forecastBaselineEnabled !== "boolean" ||
            !Number.isInteger(forecastBaselineMinutes) ||
            forecastBaselineMinutes < 0 || forecastBaselineMinutes > 9999 ||
            typeof forecastShowCoverage !== "boolean" ||
            ["none", "ema-5", "ema-10", "ema-20", "ema-40", "ema-80", "ema-160", "ema-320", "ema-640"].indexOf(smoothing) === -1 ||
            typeof forecastShowPanel !== "boolean" ||
            typeof forecastShowTooltip !== "boolean" ||
            typeof forecastHideWhenZero !== "boolean" ||
            !Number.isInteger(amount) || amount < 1 || amount > 365 ||
            ["minutes", "hours", "days", "weeks"].indexOf(unit) === -1 ||
            ["short", "weekly", "monthly", "spark", "all"].indexOf(limitWindow) === -1 ||
            ["short", "weekly", "monthly", "spark"].indexOf(forecastLimitWindow) === -1 ||
            ["compact", "compact-token", "verbose", "custom"].indexOf(format) === -1 ||
            ["compact", "compact-minutes", "verbose", "custom"].indexOf(forecastFormat) === -1 ||
            ["none", "ema-5", "ema-10", "ema-20", "ema-40", "ema-80", "ema-160", "ema-320", "ema-640"].indexOf(forecastSmoothing) === -1 ||
            !Number.isInteger(forecastWarnAmount) || forecastWarnAmount < 0 || forecastWarnAmount > 365 ||
            ["minutes", "hours", "days", "weeks"].indexOf(forecastWarnUnit) === -1 ||
            ["none", "red", "red-yellow", "blink-red-yellow", "yellow", "red-green", "red-red"].indexOf(forecastWarnFormat) === -1 ||
            typeof row["hide-when-zero"] !== "boolean" ||
            typeof row["show-coverage-marker"] !== "boolean"
        ) {
            return null;
        }
        return {
            account: account,
            "show-panel": showPanel,
            "show-tooltip": row["show-tooltip"],
            "forecast-show-panel": forecastShowPanel,
            "forecast-show-tooltip": forecastShowTooltip,
            amount: amount,
            unit: unit,
            "baseline-enabled": baselineEnabled,
            "baseline-minutes": baselineMinutes,
            smoothing: smoothing,
            "limit-window": limitWindow,
            format: format,
            "custom-format": customFormat,
            "forecast-limit-window": forecastLimitWindow,
            "forecast-format": forecastFormat,
            "forecast-custom-format": forecastCustomFormat,
            "forecast-hide-when-zero": forecastHideWhenZero,
            "forecast-smoothing": forecastSmoothing,
            "forecast-warn-amount": forecastWarnAmount,
            "forecast-warn-unit": forecastWarnUnit,
            "forecast-warn-format": forecastWarnFormat,
            "hide-when-zero": row["hide-when-zero"],
            "show-coverage-marker": row["show-coverage-marker"],
            "forecast-show-coverage-marker": forecastShowCoverage,
            "forecast-baseline-enabled": forecastBaselineEnabled,
            "forecast-baseline-minutes": forecastBaselineMinutes
        };
    },

    _consumptionSettingsMap: function(rows) {
        let result = Object.create(null);
        for (let i = 0; i < rows.length; i++) {
            result[rows[i].account] = rows[i];
        }
        return result;
    },

    _mergedResetRows: function(accounts, currentRows) {
        let current = Object.create(null);
        let seen = Object.create(null);
        if (Array.isArray(currentRows)) {
            for (let i = 0; i < currentRows.length; i++) {
                let account = this._configuredAccountId(
                    currentRows[i] && currentRows[i].account
                );
                if (!account || seen[account] || !this._backendAccounts[account]) {
                    continue;
                }
                seen[account] = true;
                let normalized = this._normalizeResetRow(currentRows[i], account);
                if (normalized) {
                    current[account] = normalized;
                }
            }
        }
        return accounts.map(Lang.bind(this, function(account) {
            return current[account.account] || this._defaultResetRow(account.account);
        }));
    },

    _defaultResetRow: function(account) {
        return {
            account: account,
            "show-panel": false,
            "show-tooltip": true,
            "hide-when-zero": true,
            "show-unknown": false,
            format: "compact"
        };
    },

    _normalizeResetRow: function(row, account) {
        if (!row || typeof row !== "object" || Array.isArray(row)) {
            return null;
        }
        let showPanel = row["show-panel"] === undefined ? false : row["show-panel"];
        let format = this._strictText(row.format, 16);
        if (
            typeof showPanel !== "boolean" ||
            typeof row["show-tooltip"] !== "boolean" ||
            typeof row["hide-when-zero"] !== "boolean" ||
            typeof row["show-unknown"] !== "boolean" ||
            ["compact", "readable", "verbose"].indexOf(format) === -1
        ) {
            return null;
        }
        return {
            account: account,
            "show-panel": showPanel,
            "show-tooltip": row["show-tooltip"],
            "hide-when-zero": row["hide-when-zero"],
            "show-unknown": row["show-unknown"],
            format: format
        };
    },

    _resetSettingsMap: function(rows) {
        let result = Object.create(null);
        for (let i = 0; i < rows.length; i++) {
            result[rows[i].account] = rows[i];
        }
        return result;
    },

    _normalizePanelRow: function(row, account) {
        if (!row || typeof row !== "object" || Array.isArray(row)) {
            return null;
        }
        let order = this._strictIntegerSetting(row.order);
        let slots = [];
        for (let index = 1; index <= PANEL_VALUE_MAX_COUNT; index++) {
            let key = "slot" + index;
            let present = index <= 4 || Object.prototype.hasOwnProperty.call(row, key);
            let value = index <= 2 || present
                ? (row[key] === undefined && index >= 3 ? 0 : this._strictIntegerSetting(row[key]))
                : 0;
            if (present && (!Number.isInteger(value) || value < 0 || value > 51)) {
                return null;
            }
            slots.push({key: key, present: present, value: value});
        }
        if (
            !Number.isInteger(order) || order < 1 || order > 100 ||
            typeof row.muted !== "boolean"
        ) {
            return null;
        }
        let seen = Object.create(null);
        let normalized = {
            account: account,
            order: order,
            muted: row.muted,
        };
        for (let i = 0; i < slots.length; i++) {
            let slot = slots[i];
            if (!slot.present) {
                continue;
            }
            let value = slot.value;
            if (value !== 0 && seen[value]) {
                value = 0;
            }
            if (value !== 0) {
                seen[value] = true;
            }
            normalized[slot.key] = value;
        }
        return normalized;
    },

    _mergedAlertRows: function(accounts, currentRows) {
        let current = Object.create(null);
        let seen = Object.create(null);
        if (Array.isArray(currentRows)) {
            for (let i = 0; i < currentRows.length; i++) {
                let account = this._configuredAccountId(currentRows[i] && currentRows[i].account);
                if (!account || seen[account] || !this._backendAccounts[account]) {
                    continue;
                }
                seen[account] = true;
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
        let usage = this._usageForAccount(account);
        return {
            account: account,
            "five-threshold": this._alertThresholdValue(
                threshold, this._alertWindowAvailable(usage, "five"), "no 5h"
            ),
            "weekly-threshold": this._alertThresholdValue(
                threshold, this._alertWindowAvailable(usage, "weekly"), "no Woche"
            ),
            "monthly-threshold": this._alertThresholdValue(
                threshold, this._alertWindowAvailable(usage, "monthly"), "no 30d"
            ),
            "spark-threshold": this._normalizeSparkThreshold(
                String(threshold), this._sparkLimitState(usage)
            ),
            warnings: true,
            errors: true
        };
    },

    _alertWindowAvailable: function(usage, kind) {
        if (!usage) {
            return false;
        }
        if (usage.main && !this._poolIsUsable(usage.main)) {
            return false;
        }
        let window = kind === "five"
            ? usage.five_hour
            : (kind === "weekly"
                ? usage.weekly
                : this._poolWindowForDuration(usage.main, 2592000));
        return this._remainingPercent(window) !== null;
    },

    _alertThresholdValue: function(value, available, missingLabel) {
        if (!available) {
            return missingLabel;
        }
        let number = typeof value === "number" ? value : Number(value);
        return Number.isInteger(number) && number >= 0 && number <= 100
            ? String(number)
            : String(this._boundedInteger(this.warningThreshold, 0, 100, 20));
    },

    _usageForAccount: function(account) {
        if (!Array.isArray(this._usages)) {
            return null;
        }
        for (let i = 0; i < this._usages.length; i++) {
            if (this._usages[i] && this._usages[i].account === account) {
                return this._usages[i];
            }
        }
        return null;
    },

    _sparkLimitState: function(usage) {
        if (!usage || usage.status !== "ok" || usage.stale === true) {
            return "unknown";
        }
        if (!usage.models || typeof usage.models !== "object" || Array.isArray(usage.models)) {
            return "unknown";
        }
        let key = "gpt-5.3-codex-spark";
        if (!Object.prototype.hasOwnProperty.call(usage.models, key)) {
            return "none";
        }
        let pool = usage.models[key];
        if (
            !pool || typeof pool !== "object" || Array.isArray(pool) ||
            pool.available !== true || !Array.isArray(pool.windows) ||
            !pool.windows.length || !this._hasUniqueWindowIdentities(pool.windows) ||
            !this._poolIsUsable(pool) ||
            !pool.windows.every(Lang.bind(this, function(window) {
                return this._remainingPercent(window) !== null;
            }))
        ) {
            return "unknown";
        }
        return "present";
    },

    _normalizeSparkThreshold: function(value, state) {
        let defaultValue = String(this._boundedInteger(this.warningThreshold, 0, 100, 20));
        if (state === "none") {
            return "no Spark";
        }
        if (typeof value === "number" && Number.isInteger(value)) {
            return value >= 0 && value <= 100 ? String(value) : defaultValue;
        }
        if (typeof value !== "string" || value.trim() !== value || !value) {
            return defaultValue;
        }
        let parsed = Number(value);
        return Number.isFinite(parsed) && parsed >= 0 && parsed <= 100
            ? value
            : defaultValue;
    },

    _normalizeAlertRow: function(row, account) {
        if (!row || typeof row !== "object" || Array.isArray(row)) {
            return null;
        }
        let usage = this._usageForAccount(account);
        let fallback = this._boundedInteger(this.warningThreshold, 0, 100, 20);
        let five = this._normalizeAlertThreshold(
            row["five-threshold"], this._alertWindowAvailable(usage, "five"), "no 5h", fallback
        );
        let weekly = this._normalizeAlertThreshold(
            row["weekly-threshold"], this._alertWindowAvailable(usage, "weekly"), "no Woche", fallback
        );
        let monthly = this._normalizeAlertThreshold(
            row["monthly-threshold"], this._alertWindowAvailable(usage, "monthly"), "no 30d", fallback
        );
        let spark = this._normalizeSparkThreshold(
            row["spark-threshold"],
            this._sparkLimitState(this._usageForAccount(account))
        );
        if (
            five === null || weekly === null || monthly === null ||
            typeof five !== "string" || typeof weekly !== "string" ||
            typeof monthly !== "string" ||
            typeof row.warnings !== "boolean" || typeof row.errors !== "boolean"
        ) {
            return null;
        }
        return {
            account: account,
            "five-threshold": five,
            "weekly-threshold": weekly,
            "monthly-threshold": monthly,
            "spark-threshold": spark,
            warnings: row.warnings,
            errors: row.errors
        };
    },

    _normalizeAlertThreshold: function(value, available, missingLabel, fallback) {
        if (!available) {
            return missingLabel;
        }
        if (value === undefined || value === null || value === "") {
            return String(fallback);
        }
        let number = typeof value === "number" ? value : Number(value);
        return Number.isInteger(number) && number >= 0 && number <= 100
            ? String(number)
            : null;
    },

    _panelSettingsMap: function(rows) {
        let result = Object.create(null);
        if (!Array.isArray(rows)) {
            return result;
        }
        for (let i = 0; i < rows.length; i++) {
            let row = rows[i];
            if (!row || typeof row !== "object" || Array.isArray(row) ||
                typeof row.account !== "string" || !row.account) {
                continue;
            }
            result[row.account] = row;
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
        if (this._removed || this._safeMode) {
            return;
        }
        if (this._backendRowsReady && !this._syncingAccountSettings) {
            this._syncAccountSettings(Object.keys(this._backendAccounts).map(Lang.bind(this, function(account) {
                return this._backendAccounts[account];
            })));
        }
        this._updatePanel();
    },

    _onPanelSettingsChanged: function() {
        if (!this._backendRowsReady || this._syncingAccountSettings || this._removed || this._safeMode) {
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
            let account = this._configuredAccountId(
                this.accountPanelSettings[i] && this.accountPanelSettings[i].account
            );
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

    _onConsumptionSettingsChanged: function() {
        if (!this._backendRowsReady || this._syncingAccountSettings || this._removed || this._safeMode) {
            return;
        }
        let expected = Object.keys(this._backendAccounts).length;
        if (
            !Array.isArray(this.accountConsumptionSettings) ||
            this.accountConsumptionSettings.length !== expected
        ) {
            this._loadAccountBackends();
            return;
        }
        let normalized = [];
        let seen = Object.create(null);
        for (let i = 0; i < this.accountConsumptionSettings.length; i++) {
            let account = this._configuredAccountId(
                this.accountConsumptionSettings[i] && this.accountConsumptionSettings[i].account
            );
            let row = this._normalizeConsumptionRow(
                this.accountConsumptionSettings[i],
                account
            );
            if (!row || seen[account] || !this._backendAccounts[account]) {
                this._loadAccountBackends();
                return;
            }
            seen[account] = true;
            normalized.push(row);
        }
        let forecasts = this._mergedForecastRows(
            Object.keys(this._backendAccounts).map(function(account) { return {account: account}; }),
            this.accountForecastSettings,
            this.accountConsumptionSettings
        );
        this._consumptionSettings = this._consumptionSettingsMap(
            this._combineConsumptionRows(normalized, forecasts)
        );
        this.accountConsumptionSettings = normalized.map(Lang.bind(this, this._consumptionStorageRow));
        this._refreshConsumption();
        this._refreshFormattedSurfaces();
    },

    _onForecastSettingsChanged: function() {
        if (!this._backendRowsReady || this._syncingAccountSettings || this._removed || this._safeMode) return;
        let accounts = Object.keys(this._backendAccounts).map(function(account) { return {account: account}; });
        let rows = this._mergedForecastRows(accounts, this.accountForecastSettings, null);
        this.accountForecastSettings = rows;
        let consumption = this._mergedConsumptionRows(accounts, this.accountConsumptionSettings);
        this._consumptionSettings = this._consumptionSettingsMap(this._combineConsumptionRows(consumption, rows));
        this._refreshConsumption();
        this._refreshFormattedSurfaces();
    },

    _onCreditSettingsChanged: function() {
        if (!this._backendRowsReady || this._syncingAccountSettings || this._removed || this._safeMode) {
            return;
        }
        let expected = Object.keys(this._backendAccounts).length;
        if (!Array.isArray(this.accountCreditSettings) || this.accountCreditSettings.length !== expected) {
            this._loadAccountBackends();
            return;
        }
        let normalized = [], seen = Object.create(null);
        for (let i = 0; i < this.accountCreditSettings.length; i++) {
            let account = this._configuredAccountId(this.accountCreditSettings[i] && this.accountCreditSettings[i].account);
            let row = this._normalizeCreditRow(this.accountCreditSettings[i], account);
            if (!row || seen[account] || !this._backendAccounts[account]) {
                this._loadAccountBackends();
                return;
            }
            seen[account] = true;
            normalized.push(row);
        }
        let accounts = Object.keys(this._backendAccounts).map(function(account) { return {account: account}; });
        let consumptions = this._mergedCreditConsumptionRows(accounts, this.accountCreditConsumptionSettings, normalized);
        this._creditSettings = this._creditSettingsMap(this._combineCreditRows(normalized, consumptions));
        this.accountCreditSettings = normalized.map(Lang.bind(this, this._creditStorageRow));
        this._refreshFormattedSurfaces();
    },

    _onCreditConsumptionSettingsChanged: function() {
        if (!this._backendRowsReady || this._syncingAccountSettings || this._removed || this._safeMode) return;
        let accounts = Object.keys(this._backendAccounts).map(function(account) { return {account: account}; });
        let rows = this._mergedCreditConsumptionRows(accounts, this.accountCreditConsumptionSettings, null);
        this.accountCreditConsumptionSettings = rows;
        let credits = this._mergedCreditRows(accounts, this.accountCreditSettings);
        this._creditSettings = this._creditSettingsMap(this._combineCreditRows(credits, rows));
        this._refreshFormattedSurfaces();
    },

    _onResetDisplaySettingsChanged: function() {
        if (!this._backendRowsReady || this._syncingAccountSettings || this._removed || this._safeMode) {
            return;
        }
        let expected = Object.keys(this._backendAccounts).length;
        if (
            !Array.isArray(this.accountResetDisplaySettings) ||
            this.accountResetDisplaySettings.length !== expected
        ) {
            this._loadAccountBackends();
            return;
        }
        let normalized = [];
        let seen = Object.create(null);
        for (let i = 0; i < this.accountResetDisplaySettings.length; i++) {
            let account = this._configuredAccountId(
                this.accountResetDisplaySettings[i] && this.accountResetDisplaySettings[i].account
            );
            let row = this._normalizeResetRow(
                this.accountResetDisplaySettings[i],
                account
            );
            if (!row || seen[account] || !this._backendAccounts[account]) {
                this._loadAccountBackends();
                return;
            }
            seen[account] = true;
            normalized.push(row);
        }
        this._resetSettings = this._resetSettingsMap(normalized);
        this.accountResetDisplaySettings = normalized;
        this._refreshFormattedSurfaces();
    },

    _onAlertSettingsChanged: function() {
        if (!this._backendRowsReady || this._syncingAccountSettings || this._removed || this._safeMode) {
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
            let account = this._configuredAccountId(
                this.accountAlertSettings[i] && this.accountAlertSettings[i].account
            );
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
            average: 3,
            "spark-five-hour": 4,
            "spark-weekly": 5,
            "spark-average": 6,
            "spark-other": 7,
            "thirty-day": 8
        }[source] || 3;
    },

    _panelValueCount: function() {
        let raw = typeof this.panelValueCount === "string"
            ? this.panelValueCount.trim() : this.panelValueCount;
        if (typeof raw === "boolean") {
            return PANEL_VALUE_DEFAULT_COUNT;
        }
        if (typeof raw === "string" && !/^[0-9]+$/.test(raw)) {
            return PANEL_VALUE_DEFAULT_COUNT;
        }
        let value = Number(raw);
        return Number.isInteger(value) && value >= 1 && value <= PANEL_VALUE_MAX_COUNT
            ? value
            : PANEL_VALUE_DEFAULT_COUNT;
    },

    _syncStyleRows: function(accounts) {
        let percentRows = this._mergedStyleRows(accounts, this.accountPercentStyles, "percent");
        let dateRows = this._mergedStyleRows(accounts, this.accountDateStyles, "date");
        let timeRows = this._mergedStyleRows(accounts, this.accountTimeStyles, "time");
        let durationRows = this._mergedStyleRows(accounts, this.accountDurationStyles, "duration");
        let deltaRows = this._mergedStyleRows(accounts, this.accountDeltaStyles, "delta");
        let panelRows = Object.create(null);
        let panelChanged = Object.create(null);
        for (let source in PANEL_FORMATTING_TARGETS) {
            if (!Object.prototype.hasOwnProperty.call(PANEL_FORMATTING_TARGETS, source)) {
                continue;
            }
            let target = PANEL_FORMATTING_TARGETS[source];
            let rows = this._mergedStyleRows(accounts, this[target.property], "percent");
            panelRows[source] = rows;
            panelChanged[source] = !this._styleRowsEqual(this[target.property], rows);
        }
        let displayRows = this._mergedDisplayRows(accounts, this.accountDisplaySettings);
        let targetRows = this._mergedTargetRows(accounts, this.accountStyleTargets);
        let percentChanged = !this._styleRowsEqual(this.accountPercentStyles, percentRows);
        let dateChanged = !this._styleRowsEqual(this.accountDateStyles, dateRows);
        let timeChanged = !this._styleRowsEqual(this.accountTimeStyles, timeRows);
        let durationChanged = !this._styleRowsEqual(this.accountDurationStyles, durationRows);
        let deltaChanged = !this._styleRowsEqual(this.accountDeltaStyles, deltaRows);
        let displayChanged = !this._styleRowsEqual(this.accountDisplaySettings, displayRows);
        let targetsChanged = !this._styleRowsEqual(this.accountStyleTargets, targetRows);
        this._percentStyles = this._styleMap(percentRows);
        this._dateStyles = this._styleMap(dateRows);
        this._timeStyles = this._styleMap(timeRows);
        this._durationStyles = this._styleMap(durationRows);
        this._deltaStyles = this._styleMap(deltaRows);
        this._panelValueStyles = Object.create(null);
        for (let source in PANEL_FORMATTING_TARGETS) {
            if (Object.prototype.hasOwnProperty.call(PANEL_FORMATTING_TARGETS, source)) {
                this._panelValueStyles[source] = this._styleMap(panelRows[source]);
            }
        }
        this._displaySettings = this._displaySettingsMap(displayRows);
        this._styleTargets = this._targetMap(targetRows);
        this._syncingStyleRows = true;
        this.accountPercentStyles = percentRows;
        this.accountDateStyles = dateRows;
        this.accountTimeStyles = timeRows;
        this.accountDurationStyles = durationRows;
        this.accountDeltaStyles = deltaRows;
        for (let source in PANEL_FORMATTING_TARGETS) {
            if (Object.prototype.hasOwnProperty.call(PANEL_FORMATTING_TARGETS, source)) {
                this[PANEL_FORMATTING_TARGETS[source].property] = panelRows[source];
            }
        }
        this.accountDisplaySettings = displayRows;
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
            if (deltaChanged) {
                this.settings.setValue("account-delta-styles", deltaRows);
            }
            for (let source in PANEL_FORMATTING_TARGETS) {
                if (!Object.prototype.hasOwnProperty.call(PANEL_FORMATTING_TARGETS, source) ||
                    !panelChanged[source]) {
                    continue;
                }
                this.settings.setValue(
                    PANEL_FORMATTING_TARGETS[source].key,
                    panelRows[source]
                );
            }
            if (displayChanged) {
                this.settings.setValue("account-display-settings", displayRows);
            }
            if (targetsChanged) {
                this.settings.setValue("account-style-targets", targetRows);
            }
        } catch (e) {
            global.log("[" + UUID + "] formatting settings sync failed: " + String(e));
        }
        this._deferGuardRelease(
            "_syncingStyleRows",
            "formatting settings guard cleanup"
        );
    },

    _defaultDisplayRow: function(account) {
        return {
            account: account,
            tag: "",
            panel: 2,
            hover: 1,
            click: 1,
            "hover-separator": false,
            "click-separator": false
        };
    },

    _normalizeDisplayRow: function(row, account) {
        if (!row || typeof row !== "object" || Array.isArray(row)) {
            return null;
        }
        let tag = this._safeText(row.tag, 8);
        let panel = this._strictIntegerSetting(row.panel);
        let hover = this._strictIntegerSetting(row.hover);
        let click = this._strictIntegerSetting(row.click);
        let hoverSeparator = row["hover-separator"] === undefined
            ? false
            : row["hover-separator"];
        let clickSeparator = row["click-separator"] === undefined
            ? false
            : row["click-separator"];
        if (
            !Number.isInteger(panel) || panel < 0 || panel > 2 ||
            !Number.isInteger(hover) || hover < 0 || hover > 2 ||
            !Number.isInteger(click) || click < 0 || click > 2 ||
            typeof hoverSeparator !== "boolean" ||
            typeof clickSeparator !== "boolean"
        ) {
            return null;
        }
        return {
            account: account,
            tag: tag,
            panel: panel,
            hover: hover,
            click: click,
            "hover-separator": hoverSeparator,
            "click-separator": clickSeparator
        };
    },

    _mergedDisplayRows: function(accounts, currentRows) {
        let current = Object.create(null);
        let seen = Object.create(null);
        if (Array.isArray(currentRows)) {
            for (let i = 0; i < currentRows.length; i++) {
                let account = this._configuredAccountId(
                    currentRows[i] && currentRows[i].account
                );
                if (!account || seen[account] || !this._backendAccounts[account]) {
                    continue;
                }
                seen[account] = true;
                let normalized = this._normalizeDisplayRow(currentRows[i], account);
                if (normalized) {
                    current[account] = normalized;
                }
            }
        }
        let legacyTags = Object.create(null);
        if (Array.isArray(this.accountPanelSettings)) {
            for (let i = 0; i < this.accountPanelSettings.length; i++) {
                let row = this.accountPanelSettings[i];
                let account = this._configuredAccountId(row && row.account);
                if (account && !Object.prototype.hasOwnProperty.call(legacyTags, account)) {
                    legacyTags[account] = this._safeText(row && row.tag, 8);
                }
            }
        }
        let rows = [];
        for (let i = 0; i < accounts.length; i++) {
            let account = accounts[i].account;
            let row = current[account] || this._defaultDisplayRow(account);
            if (!current[account] && legacyTags[account]) {
                row.tag = legacyTags[account];
            }
            rows.push(row);
        }
        return rows;
    },

    _displaySettingsMap: function(rows) {
        let result = Object.create(null);
        for (let i = 0; i < rows.length; i++) {
            result[rows[i].account] = rows[i];
        }
        return result;
    },

    _styleRowsEqual: function(left, right) {
        if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) {
            return false;
        }
        return JSON.stringify(left) === JSON.stringify(right);
    },

    _mergedStyleRows: function(accounts, currentRows, kind) {
        let current = Object.create(null);
        let seen = Object.create(null);
        if (Array.isArray(currentRows)) {
            for (let i = 0; i < currentRows.length; i++) {
                let account = this._configuredAccountId(
                    currentRows[i] && currentRows[i].account
                );
                if (!account || seen[account] || !this._backendAccounts[account]) {
                    continue;
                }
                seen[account] = true;
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
            threshold: 20,
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
            "below-hover-background": 0,
            background: 0,
            "hover-background": 0
        };
        if (kind === "delta") {
            row.dynamic = false;
            return row;
        }
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
                "below-hover-background": row["below-hover-background"],
                background: row.background,
                "hover-background": row["hover-background"]
            };
        }
        return row;
    },

    _normalizeStyleRow: function(row, account, kind) {
        if (!row || typeof row !== "object" || Array.isArray(row)) {
            return null;
        }
        let format = kind === "percent" || kind === "delta"
            ? 0
            : (row.format === undefined ? 0 : this._strictIntegerSetting(row.format));
        let dynamic = kind === "delta" && row.dynamic === undefined ? false : row.dynamic;
        let mode = row.mode === undefined
            ? (row.conditional === true ? 1 : 0)
            : this._strictIntegerSetting(row.mode);
        let threshold = row.threshold === undefined
            ? 20
            : this._strictIntegerSetting(row.threshold);
        let font = row.font === undefined ? 0 : this._strictIntegerSetting(row.font);
        let size = row.size === undefined ? 0 : this._strictIntegerSetting(row.size);
        let bold = row.bold === undefined ? false : row.bold;
        let italic = row.italic === undefined ? false : row.italic;
        let color = row.color === undefined ? 0 : this._strictIntegerSetting(row.color);
        let background = row.background === undefined ? 0 : this._strictIntegerSetting(row.background);
        let hoverBackground = row["hover-background"] === undefined
            ? background
            : this._strictIntegerSetting(row["hover-background"]);
        let belowFont = row["below-font"] === undefined
            ? 0
            : this._strictIntegerSetting(row["below-font"]);
        let belowSize = row["below-size"] === undefined
            ? 0
            : this._strictIntegerSetting(row["below-size"]);
        let belowBold = row["below-bold"] === undefined ? true : row["below-bold"];
        let belowItalic = row["below-italic"] === undefined ? false : row["below-italic"];
        let belowColor = row["below-color"] === undefined
            ? 3
            : this._strictIntegerSetting(row["below-color"]);
        let belowBackground = row["below-background"] === undefined
            ? 0
            : this._strictIntegerSetting(row["below-background"]);
        let belowHoverBackground = row["below-hover-background"] === undefined
            ? belowBackground
            : this._strictIntegerSetting(row["below-hover-background"]);
        let maxFormat = kind === "date" ? 3 : (kind === "duration" ? 3 : 2);
        let maxThreshold = 100;
        if (
            (kind !== "percent" && kind !== "delta" && (
                !Number.isInteger(format) || format < 0 || format > maxFormat
            )) ||
            !Number.isInteger(mode) || mode < 0 || mode > 3 ||
            !Number.isInteger(threshold) || threshold < 0 || threshold > maxThreshold ||
            !Number.isInteger(font) || font < 0 || font > 3 ||
            !Number.isInteger(size) || size < 0 || size > 48 ||
            !Number.isInteger(color) || color < 0 || color > 7 ||
            !Number.isInteger(background) || background < 0 || background > 6 ||
            !Number.isInteger(hoverBackground) || hoverBackground < 0 || hoverBackground > 6 ||
            typeof bold !== "boolean" || typeof italic !== "boolean" ||
            (kind === "delta" && typeof dynamic !== "boolean") ||
            !Number.isInteger(belowFont) || belowFont < 0 || belowFont > 3 ||
            !Number.isInteger(belowSize) || belowSize < 0 || belowSize > 48 ||
            typeof belowBold !== "boolean" || typeof belowItalic !== "boolean" ||
            !Number.isInteger(belowColor) || belowColor < 0 || belowColor > 7 ||
            !Number.isInteger(belowBackground) || belowBackground < 0 || belowBackground > 6 ||
            !Number.isInteger(belowHoverBackground) || belowHoverBackground < 0 || belowHoverBackground > 6
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
            "below-hover-background": belowHoverBackground,
            background: background,
            "hover-background": hoverBackground
        };
        if (kind === "percent") {
            return normalized;
        }
        if (kind === "delta") {
            normalized.dynamic = dynamic;
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
            "below-hover-background": normalized["below-hover-background"],
            background: normalized.background,
            "hover-background": normalized["hover-background"]
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
        let seen = Object.create(null);
        if (Array.isArray(currentRows)) {
            for (let i = 0; i < currentRows.length; i++) {
                let account = this._configuredAccountId(currentRows[i] && currentRows[i].account);
                let element = this._strictIntegerSetting(
                    currentRows[i] && currentRows[i].element
                );
                let key = account + ":" + element;
                if (!account || seen[key] || !this._backendAccounts[account]) {
                    continue;
                }
                seen[key] = true;
                let normalized = this._normalizeTargetRow(currentRows[i], account);
                if (normalized) {
                    current[key] = normalized;
                }
            }
        }
        let rows = [];
        for (let i = 0; i < accounts.length; i++) {
            // Element 13 (the old global "baseline" target) deliberately has
            // no replacement here.  The own-baseline switch belongs to each
            // metric table and must not be masked by a second style target.
            for (let element of [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15]) {
                let key = accounts[i].account + ":" + element;
                rows.push(
                    current[key] ||
                    this._legacyTargetRow(accounts[i].account, element) ||
                    this._defaultTargetRow(accounts[i].account, element)
                );
            }
        }
        return rows;
    },

    _legacyTargetRow: function(account, element) {
        let sourceRows = [];
        if (element === 4 || element === 5) {
            sourceRows = Array.isArray(this.accountConsumptionSettings)
                ? this.accountConsumptionSettings
                : [];
        } else if (element === 6) {
            sourceRows = Array.isArray(this.accountResetDisplaySettings)
                ? this.accountResetDisplaySettings
                : [];
        }
        for (let i = 0; i < sourceRows.length; i++) {
            let row = sourceRows[i];
            if (row && row.account === account) {
                return {
                    account: account,
                    element: element,
                    panel: row["show-panel"] === true,
                    hover: row["show-tooltip"] === true,
                    click: true
                };
            }
        }
        return null;
    },

    _defaultTargetRow: function(account, element) {
        let isPercent = element === 0;
        let isSupplemental = element >= 4;
        let isIdentity = element >= 7 && element <= 9;
        return {
            account: account,
            element: element,
            panel: isPercent || isIdentity,
            hover: isPercent || isSupplemental,
            click: true
        };
    },

    _normalizeTargetRow: function(row, account) {
        if (!row || typeof row !== "object" || Array.isArray(row)) {
            return null;
        }
        let element = this._strictIntegerSetting(row.element);
        if (
            !Number.isInteger(element) ||
            [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15].indexOf(element) === -1 ||
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

    _onDeltaStylesChanged: function() {
        this._onStyleRowsChanged("delta");
    },

    _onPanelValueStylesChanged: function(source) {
        let target = PANEL_FORMATTING_TARGETS[source];
        if (!target || !this._backendRowsReady || this._syncingStyleRows ||
            this._removed || this._safeMode) {
            return;
        }
        let rows = this[target.property];
        let expected = Object.keys(this._backendAccounts).length;
        if (!Array.isArray(rows) || rows.length !== expected) {
            this._loadAccountBackends();
            return;
        }
        let normalized = [];
        let seen = Object.create(null);
        for (let i = 0; i < rows.length; i++) {
            let account = this._configuredAccountId(rows[i] && rows[i].account);
            if (!account || seen[account] || !this._backendAccounts[account]) {
                this._loadAccountBackends();
                return;
            }
            let item = this._normalizeStyleRow(rows[i], account, "percent");
            if (!item) {
                this._loadAccountBackends();
                return;
            }
            seen[account] = true;
            normalized.push(item);
        }
        this._panelValueStyles[source] = this._styleMap(normalized);
        this[target.property] = normalized;
        this._refreshFormattedSurfaces();
    },

    _onStyleRowsChanged: function(kind) {
        if (!this._backendRowsReady || this._syncingStyleRows || this._removed || this._safeMode) {
            return;
        }
        let rows = kind === "percent"
            ? this.accountPercentStyles
            : (kind === "date"
                ? this.accountDateStyles
                : (kind === "time"
                    ? this.accountTimeStyles
                    : (kind === "duration" ? this.accountDurationStyles : this.accountDeltaStyles)));
        let expected = Object.keys(this._backendAccounts).length;
        if (!Array.isArray(rows) || rows.length !== expected) {
            this._loadAccountBackends();
            return;
        }
        let normalized = [];
        let seen = Object.create(null);
        for (let i = 0; i < rows.length; i++) {
            let account = this._configuredAccountId(rows[i] && rows[i].account);
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
        } else if (kind === "duration") {
            this._durationStyles = this._styleMap(normalized);
        } else {
            this._deltaStyles = this._styleMap(normalized);
        }
        this._refreshFormattedSurfaces();
    },

    _onDisplaySettingsChanged: function() {
        if (!this._backendRowsReady || this._syncingStyleRows || this._removed || this._safeMode) {
            return;
        }
        let rows = this.accountDisplaySettings;
        let expected = Object.keys(this._backendAccounts).length;
        if (!Array.isArray(rows) || rows.length !== expected) {
            this._loadAccountBackends();
            return;
        }
        let normalized = [];
        let seen = Object.create(null);
        for (let i = 0; i < rows.length; i++) {
            let account = this._configuredAccountId(rows[i] && rows[i].account);
            let item = this._normalizeDisplayRow(rows[i], account);
            if (!item || seen[account] || !this._backendAccounts[account]) {
                this._loadAccountBackends();
                return;
            }
            seen[account] = true;
            normalized.push(item);
        }
        this._displaySettings = this._displaySettingsMap(normalized);
        this.accountDisplaySettings = normalized;
        this._refreshFormattedSurfaces();
    },

    _onStyleTargetsChanged: function() {
        if (!this._backendRowsReady || this._syncingStyleRows || this._removed || this._safeMode) {
            return;
        }
        let rows = this.accountStyleTargets;
        let expected = Object.keys(this._backendAccounts).length * 15;
        if (!Array.isArray(rows) || rows.length !== expected) {
            this._loadAccountBackends();
            return;
        }
        let normalized = [];
        let seen = Object.create(null);
        for (let i = 0; i < rows.length; i++) {
            let account = this._configuredAccountId(rows[i] && rows[i].account);
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

    _accountRowsEqual: function(left, right) {
        return Boolean(
            left && right &&
            left.account === right.account &&
            left.label === right.label &&
            (left.tag || "") === (right.tag || "") &&
            left["auth-json"] === right["auth-json"] &&
            left["profile-dir"] === right["profile-dir"] &&
            (left["test-home"] === undefined ? false : left["test-home"]) ===
                (right["test-home"] === undefined ? false : right["test-home"]) &&
            left.browser === right.browser &&
            left["reactivation-browser"] === right["reactivation-browser"] &&
            left.series === right.series &&
            (left["series-active"] === undefined ? false : left["series-active"]) ===
                (right["series-active"] === undefined ? false : right["series-active"]) &&
            left.backend === right.backend
        );
    },

    _isTestHomeProfile: function(profileDir) {
        if (typeof profileDir !== "string" || !profileDir) {
            return false;
        }
        let home = typeof GLib.get_home_dir === "function" ? GLib.get_home_dir() : "";
        return Boolean(home) && (
            profileDir === home + "/.codex-test" ||
            profileDir.indexOf(home + "/.codex-test/") === 0
        );
    },

    _reactivationBrowserName: function(value) {
        if (
            !Number.isInteger(value) ||
            value < 0 ||
            value >= REACTIVATION_BROWSER_NAMES.length
        ) {
            return "auto";
        }
        return REACTIVATION_BROWSER_NAMES[value];
    },

    _markLegacyReactivationBrowserMigrated: function() {
        try {
            this.settings.setValue("reactivation-browser-migrated", true);
        } catch (e) {
            global.log("[" + UUID + "] reactivation migration marker failed: " + this._shortText(e, 180));
            return false;
        }
        this._legacyReactivationMigrationStarted = true;
        this.reactivationBrowserMigrated = true;
        return true;
    },

    _migrateLegacyReactivationBrowser: function(rows) {
        if (
            this.reactivationBrowserMigrated ||
            this._legacyReactivationMigrationStarted ||
            !Array.isArray(rows)
        ) {
            return;
        }
        let browser = this.reactivationBrowser;
        let browserIndex = ["auto", "vivaldi", "chromium", "firefox"].indexOf(browser);
        if (browserIndex <= 0) {
            this._markLegacyReactivationBrowserMigrated();
            return;
        }
        let migratedRows = [];
        let changed = 0;
        for (let i = 0; i < rows.length; i++) {
            let row = rows[i];
            if (
                row["series-active"] !== undefined &&
                typeof row["series-active"] !== "boolean"
            ) {
                global.log("[" + UUID + "] legacy migration ignored malformed series-active");
                return;
            }
            let migrated = {
                account: row.account,
                label: row.label,
                tag: row.tag || "",
                "auth-json": row["auth-json"],
                "profile-dir": row["profile-dir"],
                "test-home": row["test-home"],
                browser: row.browser,
                "reactivation-browser": row["reactivation-browser"],
                series: row.series || "",
                "series-active": row["series-active"] === undefined
                    ? false
                    : row["series-active"],
                backend: row.backend
            };
            if (migrated["reactivation-browser"] === 0) {
                migrated["reactivation-browser"] = browserIndex;
                changed += 1;
            }
            migratedRows.push(migrated);
        }
        if (!changed) {
            this._markLegacyReactivationBrowserMigrated();
            return;
        }
        this._legacyReactivationMigrationPending = changed;
        this._reconcileAccountChanges(migratedRows);
    },

    _reconcileAccountChanges: function(rows) {
        let queue = [];
        let desired = Object.create(null);
        for (let i = 0; i < rows.length; i++) {
            desired[rows[i].account] = true;
        }
        let configuredAccounts = Object.keys(this._backendAccounts);
        for (let i = 0; i < configuredAccounts.length; i++) {
            let account = configuredAccounts[i];
            if (!desired[account]) {
                queue.push({ action: "delete", account: account });
            }
        }
        for (let i = 0; i < rows.length; i++) {
            let row = rows[i];
            let canonical = this._backendAccounts[row.account];
            if (!canonical || !this._accountRowsEqual(row, canonical)) {
                queue.push(row);
            }
        }
        this._accountChangeQueue = queue;
        this._drainAccountChanges();
    },

    _reconcilePendingAccountChanges: function() {
        if (
            !this._accountChangePendingRows || this._accountChangeCurrent ||
            this._accountChangeQueue.length
        ) {
            return;
        }
        let rows = this._accountChangePendingRows;
        this._accountChangePendingRows = null;
        this._reconcileAccountChanges(rows);
    },

    _validateSeriesAssignments: function(rows) {
        let owners = Object.create(null);
        for (let i = 0; i < rows.length; i++) {
            let row = rows[i];
            let series = typeof row.series === "string" ? row.series.trim().toUpperCase() : "";
            if (!row["series-active"] || !series) continue;
            if (owners[series] && owners[series] !== row.account) {
                throw new Error("Serie " + series + " ist bereits Account " + owners[series] + " zugeordnet");
            }
            owners[series] = row.account;
        }
    },

    _drainAccountChanges: function() {
        if (
            this._removed || this._safeMode || this._accountChangeCurrent ||
            this._backendChangeCurrent || this._backendChangeQueue.length ||
            this._auxProcess || !this._accountChangeQueue.length
        ) {
            return;
        }
        let changed = this._accountChangeQueue.shift();
        this._accountChangeCurrent = changed;
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            this._accountChangeCurrent = null;
            this._accountChangeQueue = [];
            this._loadAccountBackends();
            return;
        }
        let canonical = this._backendAccounts[changed.account];
        if (changed.action === "delete") {
            if (this._accountDeleteWaitingForProfileJob[changed.account]) {
                if (this._deviceLoginJobs[changed.account]) {
                    this._accountChangeCurrent = null;
                    this._accountChangeQueue.unshift(changed);
                    return;
                }
                delete this._accountDeleteWaitingForProfileJob[changed.account];
            }
            if (this._deviceLoginJobs[changed.account]) {
                this._accountDeleteWaitingForProfileJob[changed.account] = true;
                this._accountChangeCurrent = null;
                this._accountChangeQueue.unshift(changed);
                this._cancelProfileJob(changed.account, true);
                return;
            }
            argv.push("account", "delete", changed.account, "--format", "json");
            this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
                try {
                    if (
                        error || !payload || payload.ok !== true ||
                        payload.account !== changed.account
                    ) {
                        this._showCommandError(error || _("Account konnte nicht gelöscht werden"));
                    } else {
                        this._refreshFresh(false);
                    }
                } finally {
                    this._accountChangeCurrent = null;
                    if (this._accountChangeQueue.length) {
                        this._drainAccountChanges();
                    } else {
                        this._loadAccountBackends();
                    }
                }
            }), true);
            return;
        }
        let authJson;
        let profileDir;
        try {
            authJson = this._localAccountPath(changed["auth-json"]);
            profileDir = this._localAccountPath(changed["profile-dir"]);
        } catch (e) {
            this._accountChangeCurrent = null;
            this._accountChangeQueue = [];
            this._loadAccountBackends();
            return;
        }
        let startProfile = !canonical && !authJson;
        if (startProfile) {
            this._profilePendingAccounts[changed.account] = true;
        }
        argv.push("account", "add", changed.account);
        if (changed.label) {
            argv.push("--label", changed.label);
        }
        if (Object.prototype.hasOwnProperty.call(changed, "tag")) {
            argv.push("--tag", changed.tag);
        } else if (canonical && canonical.tag) {
            argv.push("--tag", canonical.tag);
        }
        if (profileDir) {
            argv.push("--profile-dir", profileDir);
        }
        if (changed["test-home"]) {
            argv.push("--test-home");
        }
        argv.push(
            "--browser",
            changed.browser === 1 ? "chromium" : "firefox",
            "--reactivation-browser",
            this._reactivationBrowserName(changed["reactivation-browser"]),
            "--backend",
            changed.backend === 1 ? "app-server" : "direct"
        );
        if (Object.prototype.hasOwnProperty.call(changed, "series")) {
            argv.push("--series", changed.series);
        }
        argv.push(changed["series-active"] ? "--series-active" : "--no-series-active");
        if (authJson) {
            argv.push("--auth-json", authJson);
        } else if (canonical && canonical["auth-json"]) {
            argv.push("--clear-auth-json");
        }
        argv.push("--format", "json");
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            try {
                if (
                    error || !payload || payload.ok !== true ||
                    !payload.account || payload.account.id !== changed.account
                ) {
                    delete this._profilePendingAccounts[changed.account];
                    this._legacyReactivationMigrationPending = 0;
                    this._showCommandError(error || _("Account konnte nicht gespeichert werden"));
                } else {
                    if (this._legacyReactivationMigrationPending > 0) {
                        this._legacyReactivationMigrationPending -= 1;
                        if (this._legacyReactivationMigrationPending === 0) {
                            this._markLegacyReactivationBrowserMigrated();
                        }
                    }
                    if (startProfile) {
                        let createdProfileDir = profileDir;
                        if (!createdProfileDir) {
                            try {
                                createdProfileDir = this._localAccountPath(
                                    payload.account.profile_dir
                                );
                            } catch (e) {
                                createdProfileDir = null;
                            }
                        }
                        if (!createdProfileDir) {
                            delete this._profilePendingAccounts[changed.account];
                            this._showCommandError(_("Profilordner konnte nicht bestimmt werden"));
                        } else {
                            this._startProfileCreation(changed, createdProfileDir);
                        }
                    } else {
                        this._refreshFresh(false);
                    }
                }
            } finally {
                this._accountChangeCurrent = null;
                if (this._accountChangeQueue.length) {
                    this._drainAccountChanges();
                } else {
                    this._loadAccountBackends();
                }
            }
        }), true);
    },

    _startProfileCreation: function(row, profileDir) {
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            delete this._profilePendingAccounts[row.account];
            this._deviceLoginErrors[row.account] = String(e);
            this._buildUsageMenu();
            return;
        }
        argv.push(
            "profile", "create",
            "--account-id", row.account,
            "--label", row.label || row.account,
            ...(row.tag ? ["--tag", row.tag] : []),
            "--browser", row.browser === 1 ? "chromium" : "firefox",
            "--backend", row.backend === 1 ? "app-server" : "direct",
            "--profile-dir", profileDir,
            "--reactivation-browser",
            this._reactivationBrowserName(row["reactivation-browser"]),
            ...(row.series ? ["--series", row.series] : []),
            ...(row["series-active"] ? ["--series-active"] : []),
            "--json-events"
        );
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            if (
                error || !payload || payload.ok !== true ||
                payload.account !== row.account ||
                !/^job-[0-9a-f]{32}$/.test(payload.job_id || "") ||
                ["queued", "running", "cancel_requested"].indexOf(payload.status) === -1
            ) {
                delete this._profilePendingAccounts[row.account];
                this._deviceLoginErrors[row.account] = this._shortText(
                    error || "Profiljob konnte nicht gestartet werden",
                    200
                );
                this._buildUsageMenu();
                return;
            }
            this._deviceLoginJobs[row.account] = payload.job_id;
            this._deviceLoginActive[row.account] = true;
            delete this._deviceLoginErrors[row.account];
            this._buildUsageMenu();
            this._pollProfileJob(row.account, true);
        }), true, 10000);
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
            this._removed || this._safeMode || this._backendChangeCurrent || this._auxProcess ||
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
            this._backendChangeQueue.length || this._accountChangeCurrent ||
            this._accountChangeQueue.length || this._auxProcess ||
            !this._backendAuxQueue.length
        ) {
            return;
        }
        let request = this._backendAuxQueue.shift();
        this._spawnAuxJson(request.argv, request.callback, false, request.timeoutMs);
    },

    _auxRequestKey: function(argv) {
        let parts = [];
        for (let i = 0; i < argv.length; i++) {
            parts.push(String(argv[i]));
        }
        return parts.join("\u0000");
    },

    _onAccountBackendsChanged: function() {
        if (
            !this._backendRowsReady || this._syncingBackendRows || this._removed ||
            this._safeMode
        ) {
            return;
        }
        let rows = this.accountBackends;
        if (!Array.isArray(rows) || rows.length > MAX_ACCOUNTS) {
            this._loadAccountBackends();
            return;
        }
        let desiredRows = [];
        let seen = Object.create(null);
        let legacyBackendOnly = true;
        for (let i = 0; i < rows.length; i++) {
            let row = rows[i];
            if (!row || typeof row !== "object" || Array.isArray(row)) {
                this._loadAccountBackends();
                return;
            }
            let account;
            try {
                account = this._strictText(row.account, 64);
            } catch (e) {
                this._loadAccountBackends();
                return;
            }
            let canonical = this._backendAccounts[account];
            let hasEditableFields = Object.prototype.hasOwnProperty.call(row, "auth-json") ||
                Object.prototype.hasOwnProperty.call(row, "tag") ||
                Object.prototype.hasOwnProperty.call(row, "profile-dir") ||
                Object.prototype.hasOwnProperty.call(row, "test-home") ||
                Object.prototype.hasOwnProperty.call(row, "browser") ||
                Object.prototype.hasOwnProperty.call(row, "reactivation-browser") ||
                Object.prototype.hasOwnProperty.call(row, "series") ||
                Object.prototype.hasOwnProperty.call(row, "series-active");
            legacyBackendOnly = legacyBackendOnly && !hasEditableFields;
            let label;
            try {
                label = row.label === undefined
                    ? this._safeText(canonical && canonical.label, 120)
                    : this._safeText(row.label, 120);
            } catch (e) {
                this._loadAccountBackends();
                return;
            }
            let canonicalTag;
            let tag;
            let authJson;
            let profileDir;
            let series;
            try {
                canonicalTag = canonical && typeof canonical.tag === "string"
                    ? canonical.tag : "";
                tag = row.tag === undefined
                    ? canonicalTag
                    : this._strictText(row.tag, 8);
                authJson = row["auth-json"] === undefined
                    ? this._safeText(canonical && canonical["auth-json"], 4096)
                    : this._safeText(row["auth-json"], 4096);
                profileDir = row["profile-dir"] === undefined
                    ? this._safeText(canonical && canonical["profile-dir"], 4096)
                    : this._safeText(row["profile-dir"], 4096);
                series = row.series === undefined
                    ? (canonical && typeof canonical.series === "string" ? canonical.series : "")
                    : this._strictText(row.series, 16).toUpperCase();
            } catch (e) {
                this._loadAccountBackends();
                return;
            }
            let testHome = row["test-home"] === undefined
                ? Boolean(canonical && canonical["test-home"])
                : row["test-home"];
            try {
                authJson = this._localAccountPath(authJson);
                profileDir = this._localAccountPath(profileDir);
            } catch (e) {
                this._loadAccountBackends();
                return;
            }
            authJson = authJson || null;
            profileDir = profileDir || null;
            let browser = row.browser === undefined
                ? (canonical && canonical.browser === 1 ? 1 : 0)
                : this._strictIntegerSetting(row.browser);
            let reactivationBrowser = row["reactivation-browser"] === undefined
                ? (canonical && Number.isInteger(canonical["reactivation-browser"])
                    ? canonical["reactivation-browser"] : 0)
                : this._strictIntegerSetting(row["reactivation-browser"]);
            let backendValue = row.backend === undefined
                ? (canonical && canonical.backend === 1 ? 1 : 0)
                : this._strictIntegerSetting(row.backend);
            let seriesActive;
            if (row["series-active"] === undefined) {
                seriesActive = Boolean(canonical && canonical["series-active"]);
            } else if (typeof row["series-active"] !== "boolean") {
                this._loadAccountBackends();
                return;
            } else {
                seriesActive = row["series-active"];
            }
            if (
                !account || seen[account] ||
                (!canonical && !/^[A-Za-z0-9_.-]{1,64}$/.test(account)) ||
                tag.length > 8 ||
                (browser !== 0 && browser !== 1) ||
                typeof testHome !== "boolean" ||
                !Number.isInteger(reactivationBrowser) ||
                (reactivationBrowser < 0 || reactivationBrowser > 3) ||
                (series && !/^[A-Z][A-Z0-9_-]{0,15}$/.test(series)) ||
                (seriesActive && !series) ||
                (backendValue !== 0 && backendValue !== 1)
            ) {
                this._loadAccountBackends();
                return;
            }
            seen[account] = true;
            desiredRows.push({
                account: account,
                label: label,
                ...((Object.prototype.hasOwnProperty.call(row, "tag") ||
                    (canonical && canonical.tag)) ? { tag: tag } : {}),
                "auth-json": authJson,
                "profile-dir": profileDir,
                "test-home": testHome,
                browser: browser,
                "reactivation-browser": reactivationBrowser,
                series: series,
                "series-active": seriesActive,
                backend: backendValue
            });
        }
        try {
            this._validateSeriesAssignments(desiredRows);
        } catch (e) {
            this._showCommandError(e);
            this._loadAccountBackends();
            return;
        }
        if (this._accountChangeCurrent || this._accountChangeQueue.length) {
            this._accountChangePendingRows = desiredRows;
            return;
        }
        let removedAccount = Object.keys(this._backendAccounts).some(function(account) {
            return !seen[account];
        });
        if (legacyBackendOnly && !removedAccount) {
            this._reconcileBackendChanges(desiredRows);
            return;
        }
        this._reconcileAccountChanges(desiredRows);
    },

    _enableBackgroundService: function(after) {
        let continueAfter = Lang.bind(this, function() {
            if (after && !this._removed && !this._safeMode) {
                this._runSafely("service continuation", after);
            }
        });
        if (this._removed || this._safeMode) {
            return;
        }
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
            if (
                error ||
                !payload ||
                !this._serviceStatusIsHealthy(payload)
            ) {
                this._serviceAutoAttempted = false;
                try {
                    this._showCommandError(error || _("systemd-Timer konnte nicht aktiviert werden"));
                } catch (e) {
                    global.log("[" + UUID + "] service error display failed: " + this._shortText(e, 180));
                }
                if (this.pollOwner === "auto" && this.autoRefresh) {
                    this._systemdActive = false;
                    this._runSafely("service fallback refresh", Lang.bind(this, function() {
                        this._refreshFresh(false);
                    }));
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

    _spawnAuxJson: function(argv, callback, backendRequest, timeoutMs) {
        if (
            !backendRequest &&
            (
                this._backendChangeCurrent || this._backendChangeQueue.length ||
                this._accountChangeCurrent || this._accountChangeQueue.length
            )
        ) {
            let key = this._auxRequestKey(argv);
            for (let i = 0; i < this._backendAuxQueue.length; i++) {
                if (this._backendAuxQueue[i].key === key) {
                    this._backendAuxQueue[i].callback = callback;
                    this._backendAuxQueue[i].timeoutMs = timeoutMs;
                    return;
                }
            }
            if (this._backendAuxQueue.length >= MAX_DEFERRED_AUX_REQUESTS) {
                this._runSafely("auxiliary queue overflow", Lang.bind(this, function() {
                    callback(null, _("Zu viele wartende Hilfsanfragen"));
                }));
                return;
            }
            this._backendAuxQueue.push({
                argv: argv,
                callback: callback,
                key: key,
                timeoutMs: timeoutMs
            });
            return;
        }
        this._cancelAuxProcess();
        let generation = ++this._auxGeneration;
        let serviceEnable = false;
        for (let index = argv.length - 2; index >= 0; index--) {
            if (argv[index] === "service" && argv[index + 1] === "enable") {
                serviceEnable = true;
                break;
            }
        }
        let deviceLogin = false;
        let profileJobs = false;
        let profileJobStatus = false;
        let profileJobCancel = false;
        for (let index = 0; index < argv.length - 1; index++) {
            if (argv[index] === "profile" && argv[index + 1] === "device-login") {
                deviceLogin = true;
            } else if (argv[index] === "profile" && argv[index + 1] === "jobs") {
                profileJobs = true;
            } else if (argv[index] === "profile" && argv[index + 1] === "job-status") {
                profileJobStatus = true;
            } else if (argv[index] === "profile" && argv[index + 1] === "cancel") {
                profileJobCancel = true;
            }
        }
        this._auxCommand = serviceEnable
            ? "service-enable"
            : (deviceLogin
                ? "device-login"
                : (profileJobs
                    ? "profile-jobs"
                    : (profileJobStatus
                        ? "profile-job-status"
                        : (profileJobCancel ? "profile-job-cancel" : ""))));
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
            this._drainAccountChanges();
            this._drainDeferredAuxRequests();
            this._drainConsumptionRequests();
            if (!this._auxProcess && this._profileJobResumeQueue.length) {
                this._pollNextProfileJob();
            }
        });
        try {
            let launcher = Gio.SubprocessLauncher.new(
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
            );
            launcher.setenv("PYTHONUNBUFFERED", "1", true);
            process = launcher.spawnv(argv);
            this._auxProcess = process;
            let selectedTimeout = timeoutMs === undefined ? AUX_COMMAND_TIMEOUT_MS : timeoutMs;
            if (!Number.isInteger(selectedTimeout) || selectedTimeout < 1000 || selectedTimeout > 1800000) {
                throw new Error("invalid auxiliary timeout");
            }
            let timeoutSeconds = Math.ceil(selectedTimeout / 1000);
            let timeoutMinutes = Math.floor(timeoutSeconds / 60);
            let remainingSeconds = timeoutSeconds % 60;
            let timeoutParts = [];
            if (timeoutMinutes) {
                timeoutParts.push(timeoutMinutes +
                    (timeoutMinutes === 1 ? " Minute" : " Minuten"));
            }
            if (remainingSeconds || !timeoutParts.length) {
                timeoutParts.push(remainingSeconds +
                    (remainingSeconds === 1 ? " Sekunde" : " Sekunden"));
            }
            let timeoutDuration = timeoutParts.join(" ");
            let timeoutId = Mainloop.timeout_add(
                selectedTimeout,
                Lang.bind(this, function() {
                    if (generation === this._auxGeneration) {
                        this._clearSource("_auxTimeoutId");
                    }
                    try {
                        process.force_exit();
                    } catch (e) {
                        global.log("[" + UUID + "] auxiliary cleanup failed: " + String(e));
                    }
                    finish(null, (deviceLogin
                        ? _("Device-Login nach ")
                        : _("Hilfsbefehl nach ")) + timeoutDuration + _(" abgebrochen"));
                    return false;
                })
            );
            if (!timeoutId) {
                throw new Error("auxiliary timeout source unavailable");
            }
            this._setSource("_auxTimeoutId", timeoutId);
            let liveChunk = deviceLogin
                ? Lang.bind(this, function(name, chunk, final) {
                    if (
                        this._removed || generation !== this._auxGeneration ||
                        this._auxCommand !== "device-login"
                    ) {
                        return;
                    }
                    this._recordDeviceLoginChunk(name, chunk, final);
                })
                : null;
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
            }), liveChunk);
        } catch (e) {
            this._terminateChild(process, "auxiliary process startup cleanup");
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
            let account = this._strictText(item.account, 64);
            if (!account || !/^[A-Za-z0-9_.-]{1,64}$/.test(account)) {
                throw new Error("account id missing");
            }
            if (seenAccounts[account]) {
                throw new Error("duplicate account id");
            }
            seenAccounts[account] = true;
            let staleFlagInvalid = item.stale !== undefined && typeof item.stale !== "boolean";
            let cacheInvalidatedFlagInvalid = item.cache_invalidated !== undefined &&
                typeof item.cache_invalidated !== "boolean";
            let staleFlagMissing = item.stale === undefined;
            let cacheInvalidatedFlagMissing = item.cache_invalidated === undefined;
            let statusValid = typeof item.status === "string" &&
                ["ok", "partial", "error", "login_required", "blocked"].indexOf(item.status) !== -1;
            let status = this._safeStatus(item.status);
            let error = this._safeText(item.error, MAX_TEXT_CHARS);
            let capturedAt = this._strictText(item.captured_at, 80);
            let valuesCapturedAt = this._strictText(item.values_captured_at, 80);
            let fiveHour = this._safeWindow(item.five_hour);
            let weekly = this._safeWindow(item.weekly);
            let credits = this._safeWindow(item.credits);
            let main = this._safePool(item.main, "main");
            let models = this._safePools(item.models);
            let usageResets = this._safeUsageResets(item.usage_resets);
            let costWindows = item.cost_windows === undefined
                ? []
                : this._safeConsumptionWindows(item.cost_windows);
            let backendConfigured = this._validatedBackend(item.backend_configured);
            let backendUsed = this._validatedBackend(item.backend_used, true);
            let stale = item.stale === true || staleFlagInvalid;
            let cacheInvalidated = item.cache_invalidated === true || cacheInvalidatedFlagInvalid;
            let hasPayloadUsageValue = this._hasPayloadUsageValue(
                fiveHour,
                weekly,
                main,
                models
            ) || this._hasModelPayloadUsageValue(models);
            let freshnessMetadataMissing = hasPayloadUsageValue &&
                (staleFlagMissing || cacheInvalidatedFlagMissing);
            let captureMetadataInvalid = hasPayloadUsageValue && (
                !this._captureTimestampUsable(capturedAt) ||
                Boolean(valuesCapturedAt) && !this._captureTimestampUsable(valuesCapturedAt)
            );
            if (!statusValid) {
                status = "error";
                error = error || "invalid usage status";
                stale = true;
                cacheInvalidated = true;
                if (hasPayloadUsageValue) {
                    fiveHour = null;
                    weekly = null;
                    main = null;
                    models = Object.create(null);
                }
            }
            if (hasPayloadUsageValue && (!backendConfigured || !backendUsed)) {
                status = "error";
                error = error || "backend provenance missing";
                stale = true;
                cacheInvalidated = true;
                fiveHour = null;
                weekly = null;
                main = null;
                models = Object.create(null);
            }
            if (captureMetadataInvalid) {
                status = "error";
                error = error || "invalid capture timestamp";
                stale = true;
                cacheInvalidated = true;
                fiveHour = null;
                weekly = null;
                main = null;
                models = Object.create(null);
            }
            if (freshnessMetadataMissing) {
                status = status === "ok" ? "partial" : status;
                error = error || "usage freshness metadata missing";
                stale = true;
            }
            if (status === "login_required") {
                if (hasPayloadUsageValue || fiveHour || weekly || main ||
                    Object.keys(models).length) {
                    error = error || "terminal usage status cannot carry limit values";
                }
                fiveHour = null;
                weekly = null;
                main = null;
                models = Object.create(null);
                stale = true;
                cacheInvalidated = true;
            }
            if (status === "error") {
                if (hasPayloadUsageValue || fiveHour || weekly || main ||
                    Object.keys(models).length) {
                    error = error || "error status cannot carry limit values";
                }
                fiveHour = null;
                weekly = null;
                main = null;
                models = Object.create(null);
                stale = true;
                cacheInvalidated = true;
            }
            if (
                status === "ok" &&
                !this._hasPayloadUsageValue(fiveHour, weekly, main, models) &&
                !this._hasModelPayloadUsageValue(models)
            ) {
                status = "error";
                error = error || "usage values missing";
                stale = true;
                cacheInvalidated = true;
                fiveHour = null;
                weekly = null;
                main = null;
                models = Object.create(null);
            }
            if (cacheInvalidated || status === "error" || status === "login_required") {
                usageResets = this._safeUsageResets(null);
                credits = null;
                costWindows = [];
            }
            result.push({
                account: account,
                label: this._safeText(item.label, 120) || account,
                captured_at: capturedAt,
                five_hour: fiveHour,
                weekly: weekly,
                credits: credits,
                main: main,
                models: models,
                cost_windows: costWindows,
                usage_resets: usageResets,
                status: status,
                error: error,
                blocked_until: this._safeText(item.blocked_until, 80),
                blocked_reason: this._safeText(item.blocked_reason, MAX_TEXT_CHARS),
                auth_access_expires_at: this._safeText(item.auth_access_expires_at, 80),
                backend_configured: backendConfigured,
                backend_used: backendUsed,
                backend_user_id: this._strictText(item.backend_user_id, 256),
                backend_account_id: this._strictText(item.backend_account_id, 256),
                fallback_reason: this._strictText(item.fallback_reason, MAX_TEXT_CHARS),
                values_captured_at: valuesCapturedAt,
                stale: stale,
                cache_invalidated: cacheInvalidated
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
        let isAbsoluteCredit = value.name === "credits";
        let used = this._safeNumber(value.used);
        let limit = this._safeNumber(value.limit);
        let remaining = this._safeNumber(value.remaining);
        let percent = this._safeNumber(value.percent);
        let percentInvalid = percent !== null && (percent < 0 || percent > 100);
        if (percentInvalid) {
            // Any invalid usage field invalidates this window; do not let a
            // valid absolute pair hide contradictory percentage metadata.
            used = null;
            limit = null;
            percent = null;
            remaining = null;
        }
        if (used !== null && used < 0) {
            // A remaining counter from the same invalid absolute pair is not
            // safe to render as a plausible percentage.
            used = null;
            remaining = null;
            percent = null;
        }
        if (limit !== null && limit <= 0) {
            used = null;
            limit = null;
            remaining = null;
        }
        if (remaining !== null && limit !== null && limit > 0 && remaining > limit) {
            used = null;
            remaining = null;
            percent = null;
        }
        if (remaining !== null && remaining < 0) {
            used = null;
            remaining = null;
            percent = null;
        }
        if (
            (limit === null || limit <= 0) &&
            remaining !== null &&
            (remaining < 0 || (!isAbsoluteCredit && remaining > 100))
        ) {
            // A denominatorless absolute counter is not a percentage.
            remaining = null;
        }
        return {
            name: this._strictText(value.name, 40),
            duration_seconds: this._safeDuration(value.duration_seconds),
            used: used,
            limit: limit,
            remaining: remaining,
            percent: percent,
            reset_at: this._strictText(value.reset_at, 80),
            raw: this._safeText(value.raw, 500),
            source: this._strictText(value.source, 120)
        };
    },

    _safeDuration: function(value) {
        if (value === null || value === undefined) {
            return null;
        }
        if (typeof value !== "number" || !Number.isInteger(value) || value <= 0 || value > 315360000) {
            throw new Error("invalid limit duration");
        }
        return value;
    },

    _safeConsumptionWindows: function(value) {
        if (!Array.isArray(value) || value.length > MAX_CONSUMPTION_WINDOWS) {
            throw new Error("invalid consumption windows");
        }
        let result = [];
        for (let i = 0; i < value.length; i++) {
            let item = value[i];
            if (!item || typeof item !== "object" || Array.isArray(item)) {
                throw new Error("invalid consumption window");
            }
            let lookback = item.lookback_seconds;
            let limitWindow = item.limit_window_seconds;
            let consumed = item.consumed_percentage_points;
            let estimate = item.estimated_seconds_to_exhaustion;
            let baselineUsed = item.baseline_used_percent;
            let sampleCount = item.sample_count;
            let pool = this._strictText(item.pool, 64);
            if (estimate === undefined) {
                estimate = null;
            }
            if (baselineUsed === undefined) {
                baselineUsed = null;
            }
            let coverageSampleMismatch = item.coverage === "insufficient"
                ? sampleCount >= 2
                : sampleCount < 2;
            if (
                typeof lookback !== "number" || !Number.isInteger(lookback) ||
                lookback <= 0 || lookback > 31536000 ||
                !/^[\x21-\x7e]+$/.test(pool) ||
                typeof limitWindow !== "number" || !Number.isInteger(limitWindow) ||
                limitWindow <= 0 || limitWindow > 31536000 ||
                typeof consumed !== "number" || !Number.isFinite(consumed) ||
                consumed < 0 || consumed > 10000 ||
                (estimate !== null && (
                    typeof estimate !== "number" || !Number.isInteger(estimate) ||
                    estimate < 0 || estimate > 31536000
                )) ||
                (baselineUsed !== null && (
                    typeof baselineUsed !== "number" || !Number.isFinite(baselineUsed) ||
                    baselineUsed < 0 || baselineUsed > 100
                )) ||
                typeof sampleCount !== "number" || !Number.isInteger(sampleCount) ||
                sampleCount < 0 || sampleCount > 500000 ||
                coverageSampleMismatch ||
                ["complete", "partial", "stale", "insufficient"].indexOf(item.coverage) === -1
            ) {
                throw new Error("invalid consumption window");
            }
            result.push({
                lookback_seconds: lookback,
                pool: pool,
                limit_window_seconds: limitWindow,
                consumed_percentage_points: consumed,
                estimated_seconds_to_exhaustion: estimate,
                baseline_used_percent: baselineUsed,
                coverage: item.coverage,
                sample_count: sampleCount
            });
        }
        return result;
    },

    _safeUsageResets: function(value) {
        let unknown = { available: null, known: false, redeem_capability: false };
        if (value === null || value === undefined) {
            return unknown;
        }
        if (typeof value !== "object" || Array.isArray(value)) {
            return unknown;
        }
        if (typeof value.known !== "boolean" ||
            typeof value.redeem_capability !== "boolean") {
            return unknown;
        }
        if (value.known !== true) {
            return value.available === null
                ? {
                    available: null,
                    known: false,
                    redeem_capability: value.redeem_capability
                }
                : unknown;
        }
        if (
            typeof value.available !== "number" ||
            !Number.isInteger(value.available) ||
            value.available < 0 ||
            value.available > 10000
        ) {
            return unknown;
        }
        return {
            available: value.available,
            known: true,
            redeem_capability: value.redeem_capability
        };
    },

    _safePool: function(value, expectedKey) {
        if (value === null || value === undefined) {
            return null;
        }
        if (typeof value !== "object" || Array.isArray(value)) {
            throw new Error("invalid usage pool");
        }
        let key = "";
        try {
            key = value.key === undefined
                ? expectedKey || ""
                : this._strictText(value.key, 80);
        } catch (e) {
            throw new Error("invalid usage pool key");
        }
        if (!key || (expectedKey && key !== expectedKey)) {
            throw new Error("invalid usage pool key");
        }
        if (!Array.isArray(value.windows) || value.windows.length > MAX_POOL_WINDOWS) {
            throw new Error("invalid usage pool windows");
        }
        let sources = value.availability_sources;
        if (!Array.isArray(sources) || sources.length > MAX_POOL_WINDOWS ||
            sources.some(Lang.bind(this, function(source) {
                try {
                    return !this._strictText(source, 120);
                } catch (e) {
                    return true;
                }
            }))) {
            throw new Error("invalid availability sources");
        }
        let allowedValid = value.allowed === null || value.allowed === undefined ||
            typeof value.allowed === "boolean";
        let limitReachedValid = value.limit_reached === null || value.limit_reached === undefined ||
            typeof value.limit_reached === "boolean";
        let exhaustedValid = value.exhausted === null || value.exhausted === undefined ||
            typeof value.exhausted === "boolean";
        let windows = value.windows.map(Lang.bind(this, function(window) {
            return this._safeWindow(window);
        }));
        let duplicateWindowIdentities = this._hasDuplicateWindowIdentities(windows);
        if (typeof value.available !== "boolean") {
            throw new Error("invalid usage pool availability");
        }
        let allowed = allowedValid && typeof value.allowed === "boolean"
            ? value.allowed
            : null;
        let limitReached = limitReachedValid && typeof value.limit_reached === "boolean"
            ? value.limit_reached
            : null;
        let available = value.available && !duplicateWindowIdentities && allowedValid && limitReachedValid &&
            exhaustedValid && typeof value.exhausted === "boolean";
        if (
            exhaustedValid &&
            typeof value.exhausted === "boolean" &&
            value.exhausted !== this._poolExhaustedByFields(
                value.available,
                allowed,
                limitReached,
                windows
            )
        ) {
            // Derived exhaustion must agree with serialized control flags.
            available = false;
        }
        return {
            key: key,
            display_name: this._safeText(value.display_name, 120) || key,
            windows: windows,
            available: available,
            allowed: allowed,
            limit_reached: limitReached,
            metered_feature: this._safeText(value.metered_feature, 120),
            availability_sources: sources.map(Lang.bind(this, function(source) {
                return this._strictText(source, 120);
            })).filter(function(source) { return Boolean(source); }),
            exhausted: exhaustedValid && typeof value.exhausted === "boolean"
                ? value.exhausted
                : null
        };
    },

    _safePools: function(value) {
        if (value === null || value === undefined) {
            return Object.create(null);
        }
        if (typeof value !== "object" || Array.isArray(value)) {
            throw new Error("invalid model usage pools");
        }
        let keys = Object.keys(value);
        if (keys.length > MAX_USAGE_POOLS) {
            throw new Error("too many model usage pools");
        }
        let result = Object.create(null);
        let normalizedKeys = Object.create(null);
        for (let i = 0; i < keys.length; i++) {
            let key = "";
            try {
                key = this._strictText(keys[i], 80);
            } catch (e) {
                throw new Error("invalid model usage pool key");
            }
            let normalizedKey = key.toLowerCase();
            if (!key || Object.prototype.hasOwnProperty.call(result, key) ||
                Object.prototype.hasOwnProperty.call(normalizedKeys, normalizedKey)) {
                throw new Error("invalid model usage pool key");
            }
            normalizedKeys[normalizedKey] = true;
            result[key] = this._safePool(value[keys[i]], key);
        }
        return result;
    },

    _poolExhaustedByFields: function(available, allowed, limitReached, windows) {
        if (available !== true || allowed === false || limitReached === true) {
            return true;
        }
        if (!Array.isArray(windows) || !windows.length) {
            return false;
        }
        return windows.some(Lang.bind(this, function(window) {
            let value = this._remainingPercent(window);
            return value === null || value === 0;
        }));
    },

    _hasPayloadUsageValue: function(fiveHour, weekly, main, models) {
        let hasMainValue = Boolean(
            main &&
            main.available === true &&
            Array.isArray(main.windows) &&
            main.windows.length > 0 &&
            main.windows.every(Lang.bind(this, function(window) {
                return this._windowHasUsageValue(window) &&
                    this._windowIdentityIsKnown(window);
            }))
        );
        let hasLegacyValue = [fiveHour, weekly].some(Lang.bind(this, function(window) {
            return this._windowHasUsageValue(window) &&
                this._windowIdentityIsKnown(window);
        }));
        return hasMainValue || hasLegacyValue;
    },

    _hasModelPayloadUsageValue: function(models) {
        if (!models || typeof models !== "object") {
            return false;
        }
        return Object.keys(models).some(Lang.bind(this, function(key) {
            return this._poolIsUsable(models[key]);
        }));
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

    _validatedBackend: function(value, allowBrowser) {
        if (value === null || value === undefined || value === "") {
            return "";
        }
        let backend;
        try {
            backend = this._strictText(value, 32);
        } catch (e) {
            throw new Error("invalid backend provenance");
        }
        let allowed = allowBrowser
            ? ["direct", "app-server", "browser"]
            : ["direct", "app-server"];
        if (!backend) {
            throw new Error("invalid backend provenance");
        }
        if (allowed.indexOf(backend) === -1) {
            throw new Error("invalid backend provenance");
        }
        return backend;
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

    _localAccountPath: function(value) {
        let text = this._safeText(value, 4096);
        if (!text) {
            return text;
        }
        if (!/^file:\/\//i.test(text)) {
            if (text.charAt(0) !== "/") {
                throw new Error("invalid local account path");
            }
            return text;
        }
        let uriMatch = text.match(/^file:\/\/([^\/]*)(\/.*)$/i);
        if (!uriMatch) {
            throw new Error("invalid local account path");
        }
        let authority = uriMatch[1];
        if (authority && authority.toLowerCase() !== "localhost") {
            throw new Error("invalid local account path");
        }
        let path;
        try {
            path = Gio.file_new_for_uri(text).get_path();
        } catch (e) {
            throw new Error("invalid local account path");
        }
        if (!path || path.charAt(0) !== "/") {
            throw new Error("invalid local account path");
        }
        return path;
    },

    _accountSettingPath: function(value) {
        if (value === null || value === undefined) {
            return null;
        }
        let path = this._localAccountPath(value);
        if (!path) {
            return "";
        }
        try {
            let uri = Gio.file_new_for_path(path).get_uri();
            if (!uri || !/^file:\/\//i.test(uri)) {
                throw new Error("invalid local account path");
            }
            return uri;
        } catch (e) {
            throw new Error("invalid local account path");
        }
    },

    _strictText: function(value, limit) {
        if (value === null || value === undefined) {
            return "";
        }
        if (typeof value !== "string") {
            throw new Error("invalid text value");
        }
        if (value.length > limit || /[\u0000-\u001f\u007f]/.test(value) ||
            value.trim() !== value) {
            throw new Error("text value exceeds strict limit");
        }
        return value;
    },

    _configuredAccountId: function(value) {
        let account;
        try {
            account = this._strictText(value, 64);
        } catch (e) {
            return "";
        }
        return /^[A-Za-z0-9_.-]{1,64}$/.test(account) ? account : "";
    },

    _applyPayload: function(payload, fresh) {
        let usages = fresh ? this._mergeFreshPayload(payload) : this._mergeCachedPayload(payload);
        this._usages = usages;
        if (this._backendRowsReady) {
            this._ensureBackendUsageRows();
            this._syncAccountSettings(Object.keys(this._backendAccounts).map(Lang.bind(this, function(account) {
                return this._backendAccounts[account];
            })));
        }
        let nowMs = Date.now();
        let staleAfterMs = this._staleAfterMs();
        for (let i = 0; i < this._usages.length; i++) {
            let capturedMs = this._dateMillis(this._usages[i].captured_at);
            this._usages[i].stale = this._usages[i].stale ||
                capturedMs === null ||
                this._captureIsTooFarInFuture(this._usages[i].captured_at, nowMs) ||
                nowMs - capturedMs > staleAfterMs;
        }
        this._buildUsageMenu();
        this._updatePanel();
        this._refreshConsumption();
        this._notifyForPayload();
    },

    _refreshConsumption: function() {
        if (!this._consumptionSettings || typeof this._consumptionSettings !== "object") {
            this._consumptionSettings = Object.create(null);
        }
        if (!Array.isArray(this._consumptionQueue)) {
            this._consumptionQueue = [];
        }
        if (!Number.isInteger(this._consumptionGeneration)) {
            this._consumptionGeneration = 0;
        }
        let generation = ++this._consumptionGeneration;
        this._consumptionQueue = [];
        for (let i = 0; i < this._usages.length; i++) {
            let usage = this._usages[i];
            if (usage.cache_invalidated === true) {
                continue;
            }
            let row = this._consumptionSettings[usage.account];
            let creditRow = this._creditSettings && this._creditSettings[usage.account];
            let panelRow = this._panelSettings[usage.account] ||
                this._defaultPanelRow(usage.account, i + 1);
            let panelSources = [];
            for (let slotIndex = 1; slotIndex <= this._panelValueCount(); slotIndex++) {
                panelSources.push(panelRow["slot" + slotIndex]);
            }
            let panelNeedsToken = false;
            let panelNeedsAllWindows = false;
            let panelNeedsMainDelta = false;
            let panelNeedsSparkDelta = false;
            for (let sourceIndex = 0; sourceIndex < panelSources.length; sourceIndex++) {
                let source = panelSources[sourceIndex];
                if (source === 12 || source === 13 || (source >= 32 && source <= 36)) {
                    panelNeedsToken = true;
                }
                if (source === 32 || source === 33 || source === 34 || source === 35 || source === 36) {
                    panelNeedsAllWindows = true;
                }
                if (source === 32 || source === 33 || source === 34 || source === 36) {
                    panelNeedsMainDelta = true;
                }
                if (source === 35) {
                    panelNeedsSparkDelta = true;
                }
            }
            let activeQueryKeys = Object.create(null);
            // Keep the last validated windows until the matching request has
            // succeeded.  Clearing them here made one failed consumption
            // refresh erase an otherwise usable panel/hover value.
            let tokenNeeded = row && (panelNeedsToken ||
                this._elementTargetEnabled(usage.account, "consumption", "panel", row["show-panel"]) ||
                this._elementTargetEnabled(usage.account, "consumption", "hover", row["show-tooltip"]) ||
                this._elementTargetEnabled(usage.account, "consumption", "click", true) ||
                this._elementTargetEnabled(usage.account, "consumption-weekly", "panel", row["show-panel"]) ||
                this._elementTargetEnabled(usage.account, "consumption-weekly", "hover", row["show-tooltip"]) ||
                this._elementTargetEnabled(usage.account, "consumption-weekly", "click", true) ||
                this._elementTargetEnabled(usage.account, "consumption-short", "panel", row["show-panel"]) ||
                this._elementTargetEnabled(usage.account, "consumption-short", "hover", row["show-tooltip"]) ||
                this._elementTargetEnabled(usage.account, "consumption-short", "click", true) ||
                this._elementTargetEnabled(usage.account, "consumption-monthly", "panel", row["show-panel"]) ||
                this._elementTargetEnabled(usage.account, "consumption-monthly", "hover", row["show-tooltip"]) ||
                this._elementTargetEnabled(usage.account, "consumption-monthly", "click", true) ||
                this._elementTargetEnabled(usage.account, "forecast", "panel", row["show-panel"]) ||
                this._elementTargetEnabled(usage.account, "forecast", "hover", row["show-tooltip"]) ||
                this._elementTargetEnabled(usage.account, "forecast", "click", true)
            );
            let creditNeeded = creditRow && (
                panelSources.indexOf(10) !== -1 ||
                this._elementTargetEnabled(
                    usage.account, "credit-consumption", "panel",
                    creditRow["consumption-show-panel"]
                ) ||
                this._elementTargetEnabled(
                    usage.account, "credit-consumption", "hover",
                    creditRow["consumption-show-tooltip"]
                ) ||
                this._elementTargetEnabled(usage.account, "credit-consumption", "click", true)
            );
            if (tokenNeeded) {
                let consumptionPool = row["limit-window"] === "spark"
                    ? "gpt-5.3-codex-spark" : "main";
                let forecastLimitWindow = row["forecast-limit-window"] || row["limit-window"];
                let forecastSmoothing = row["forecast-smoothing"] || row.smoothing || "none";
                let forecastPool = forecastLimitWindow === "spark"
                    ? "gpt-5.3-codex-spark" : "main";
                let forecastBaselineEnabled = row["forecast-baseline-enabled"] === true;
                let forecastBaselineMinutes = forecastBaselineEnabled
                    ? row["forecast-baseline-minutes"] : null;
                let forecastNeedsSeparate = consumptionPool !== forecastPool ||
                    row["limit-window"] !== forecastLimitWindow ||
                    row.smoothing !== forecastSmoothing ||
                    row["baseline-enabled"] !== forecastBaselineEnabled ||
                    (row["baseline-enabled"] && row["baseline-minutes"] !== forecastBaselineMinutes);
                let consumptionQueryKey = this._consumptionQueryKey(
                    consumptionPool, row.amount, row.unit, row.smoothing,
                    row["baseline-enabled"] ? row["baseline-minutes"] : null
                );
                activeQueryKeys[consumptionQueryKey] = true;
                this._consumptionQueue.push({
                    account: usage.account,
                    amount: row.amount,
                    unit: row.unit,
                    baselineMinutes: null,
                    baselineValueMinutes: row["baseline-enabled"] ? row["baseline-minutes"] : null,
                    smoothing: row.smoothing || "none",
                    limitWindow: consumptionPool === forecastPool &&
                        row["limit-window"] !== forecastLimitWindow
                        ? "all" : (panelNeedsAllWindows ? "all" : row["limit-window"]),
                    pool: consumptionPool,
                    queryKey: consumptionQueryKey,
                    generation: generation
                });
                if (forecastNeedsSeparate) {
                    let forecastQueryKey = this._consumptionQueryKey(
                        forecastPool, row.amount, row.unit, forecastSmoothing,
                        forecastBaselineMinutes
                    );
                    activeQueryKeys[forecastQueryKey] = true;
                    this._consumptionQueue.push({
                        account: usage.account,
                        amount: row.amount,
                        unit: row.unit,
                        baselineMinutes: null,
                        baselineValueMinutes: forecastBaselineMinutes,
                        smoothing: forecastSmoothing,
                        limitWindow: (panelNeedsMainDelta && forecastPool === "main") ||
                            (panelNeedsSparkDelta && forecastPool === "gpt-5.3-codex-spark")
                            ? "all" : forecastLimitWindow,
                        pool: forecastPool,
                        queryKey: forecastQueryKey,
                        generation: generation
                    });
                }
                if (panelNeedsSparkDelta && consumptionPool !== "gpt-5.3-codex-spark" &&
                    forecastPool !== "gpt-5.3-codex-spark") {
                    let panelSparkQueryKey = this._consumptionQueryKey(
                        "gpt-5.3-codex-spark", row.amount, row.unit, row.smoothing,
                        row["baseline-enabled"] ? row["baseline-minutes"] : null
                    );
                    if (!activeQueryKeys[panelSparkQueryKey]) {
                        activeQueryKeys[panelSparkQueryKey] = true;
                        this._consumptionQueue.push({
                            account: usage.account,
                            amount: row.amount,
                            unit: row.unit,
                            baselineMinutes: null,
                            baselineValueMinutes: row["baseline-enabled"]
                                ? row["baseline-minutes"] : null,
                            smoothing: row.smoothing || "none",
                            limitWindow: "all",
                            pool: "gpt-5.3-codex-spark",
                            queryKey: panelSparkQueryKey,
                            generation: generation
                        });
                    }
                }
                if (panelNeedsMainDelta && consumptionPool !== "main" && forecastPool !== "main") {
                    let panelMainQueryKey = this._consumptionQueryKey(
                        "main", row.amount, row.unit, row.smoothing,
                        row["baseline-enabled"] ? row["baseline-minutes"] : null
                    );
                    if (!activeQueryKeys[panelMainQueryKey]) {
                        activeQueryKeys[panelMainQueryKey] = true;
                        this._consumptionQueue.push({
                            account: usage.account,
                            amount: row.amount,
                            unit: row.unit,
                            baselineMinutes: null,
                            baselineValueMinutes: row["baseline-enabled"]
                                ? row["baseline-minutes"] : null,
                            smoothing: row.smoothing || "none",
                            limitWindow: "all",
                            pool: "main",
                            queryKey: panelMainQueryKey,
                            generation: generation
                        });
                    }
                }
            }
            if (creditNeeded) {
                let creditQueryKey = this._consumptionQueryKey(
                    "credits", creditRow["consumption-amount"], creditRow["consumption-unit"],
                    creditRow["consumption-smoothing"] || "none",
                    creditRow["consumption-baseline-enabled"]
                        ? creditRow["consumption-baseline-minutes"] : null
                );
                activeQueryKeys[creditQueryKey] = true;
                this._consumptionQueue.push({
                    account: usage.account,
                    amount: creditRow["consumption-amount"],
                    unit: creditRow["consumption-unit"],
                    baselineMinutes: null,
                    baselineValueMinutes: creditRow["consumption-baseline-enabled"]
                        ? creditRow["consumption-baseline-minutes"] : null,
                    smoothing: creditRow["consumption-smoothing"] || "none",
                    limitWindow: "monthly",
                    pool: "credits",
                    queryKey: creditQueryKey,
                    generation: generation
                });
            }
            if (Array.isArray(usage.cost_windows)) {
                usage.cost_windows = usage.cost_windows.filter(function(window) {
                    return !window || window._consumption_query_key === undefined ||
                        activeQueryKeys[window._consumption_query_key] === true;
                });
            }
        }
        this._drainConsumptionRequests();
    },

    _drainConsumptionRequests: function() {
        if (!Array.isArray(this._consumptionQueue)) {
            return;
        }
        if (
            this._removed || this._safeMode || this._consumptionCurrent ||
            this._auxProcess || this._backendChangeCurrent ||
            this._backendChangeQueue.length || this._accountChangeCurrent ||
            this._accountChangeQueue.length || !this._consumptionQueue.length
        ) {
            return;
        }
        let request = this._consumptionQueue.shift();
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            this._drainConsumptionRequests();
            return;
        }
        argv.push(
            "consumption",
            "--account",
            request.account,
            "--amount",
            String(request.amount),
            "--unit",
            request.unit,
            ...(request.baselineMinutes === null || request.baselineMinutes === undefined
                ? [] : ["--baseline-minutes", String(request.baselineMinutes)]),
            ...(request.baselineValueMinutes === null || request.baselineValueMinutes === undefined
                ? [] : ["--baseline-value-minutes", String(request.baselineValueMinutes)]),
            "--smoothing", request.smoothing || "none",
            "--pool",
            request.pool,
            "--limit-window",
            request.limitWindow,
            "--format",
            "json"
        );
        this._consumptionCurrent = request;
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            try {
                if (
                    !error && payload && payload.account_id === request.account &&
                    Array.isArray(payload.windows) && request.generation === this._consumptionGeneration
                ) {
                    let usage = this._usageForAccount(request.account);
                    if (usage && usage.cache_invalidated !== true) {
                        let windows = this._safeConsumptionWindows(payload.windows);
                        windows.forEach(function(window) {
                            window._consumption_query_key = request.queryKey;
                        });
                        let incomingLimitWindows = Object.create(null);
                        windows.forEach(function(window) {
                            incomingLimitWindows[String(window.limit_window_seconds)] = true;
                        });
                        let existing = Array.isArray(usage.cost_windows)
                            ? usage.cost_windows.filter(function(window) {
                                // A second query for the same pool (for example
                                // TE=Woche after TV=5h) must replace only its
                                // matching windows, never the complete pool.
                                return !(window.pool === request.pool &&
                                    incomingLimitWindows[String(window.limit_window_seconds)] === true &&
                                    (window._consumption_query_key === undefined ||
                                        window._consumption_query_key === request.queryKey));
                            })
                            : [];
                        usage.cost_windows = existing.concat(windows);
                    }
                }
            } catch (e) {
                global.log("[" + UUID + "] consumption payload rejected: " + this._shortText(e, 180));
            } finally {
                this._consumptionCurrent = null;
                this._updatePanel();
                if (this.menu && this.menu.isOpen) {
                    this._buildUsageMenu();
                }
                this._drainConsumptionRequests();
            }
        }), true);
    },

    _mergeCachedPayload: function(cached) {
        let previous = Object.create(null);
        for (let i = 0; i < this._usages.length; i++) {
            previous[this._usages[i].account] = this._usages[i];
        }
        let merged = [];
        let seen = Object.create(null);
        for (let i = 0; i < cached.length; i++) {
            let item = cached[i];
            if (this._backendRowsReady && !this._backendAccounts[item.account]) {
                continue;
            }
            if (item.cache_invalidated === true) {
                merged.push(this._clearInvalidatedUsage(item));
                seen[item.account] = true;
                continue;
            }
            let old = previous[item.account];
            if (old && this._backendIdentityIsIncomplete(item, old)) {
                merged.push(this._markUsageStale(old));
            } else if (old && !this._backendProvenanceMatches(item, old)) {
                let candidateBackend = this._safeBackend(item.backend_used, true);
                let candidateMatchesConfigured = Boolean(
                    this._backendRowsReady &&
                    ["direct", "app-server"].indexOf(candidateBackend) !== -1 &&
                    this._backendMatchesConfigured(
                        item,
                        this._backendConfiguredForAccount(item.account)
                    )
                );
                let oldBackend = this._safeBackend(old.backend_used, true);
                // `latest` has already revalidated authenticated identities against the configured credentials.
                let candidateAuthenticated =
                    ["direct", "app-server"].indexOf(candidateBackend) !== -1;
                let oldMatchesConfigured = Boolean(
                    this._backendRowsReady &&
                    this._backendMatchesConfigured(
                        old,
                        this._backendConfiguredForAccount(old.account)
                    )
                );
                let identityMatches = this._backendIdentityMatches(item, old) ||
                    this._backendIdentityCompatible(item, old);
                let oldIdentityPresent = this._backendIdentityPresent(old);
                let oldCanBeReplaced = !oldMatchesConfigured ||
                    ["browser", ""].indexOf(oldBackend) !== -1;
                if (
                    candidateMatchesConfigured && oldCanBeReplaced
                ) {
                    merged.push(
                        (identityMatches || !oldIdentityPresent || candidateAuthenticated) ? item :
                            (this._hasCachedWindows(old) ? this._markUsageStale(old) : item)
                    );
                } else {
                    merged.push(this._hasCachedWindows(old) ? this._markUsageStale(old) : item);
                }
            } else if (old && this._backendIdentityMatches(item, old) &&
                this._captureIsOlder(item.captured_at, old.captured_at)) {
                merged.push(old);
            } else {
                merged.push(item);
            }
            seen[item.account] = true;
        }
        for (let i = 0; i < this._usages.length; i++) {
            let old = this._usages[i];
            if (seen[old.account] ||
                (this._backendRowsReady && !this._backendAccounts[old.account])) {
                continue;
            }
            merged.push(this._markUsageStale(old));
        }
        return merged;
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
            if (this._backendRowsReady && !this._backendAccounts[item.account]) {
                continue;
            }
            freshAccounts[item.account] = true;
            if (item.cache_invalidated === true) {
                merged.push(this._clearInvalidatedUsage(item));
                continue;
            }
            let old = previous[item.account];
            if (old && this._backendIdentityIsIncomplete(item, old)) {
                merged.push(this._markUsageStale(old));
                continue;
            }
            if (old && !this._backendIdentityMatches(item, old)) {
                merged.push(item);
                continue;
            }
            if (old && !this._backendProvenanceMatches(item, old)) {
                merged.push(item);
                continue;
            }
            if (old && this._captureIsOlder(item.captured_at, old.captured_at)) {
                merged.push(old);
                continue;
            }
            if (
                old &&
                this._backendIdentityMatches(item, old) &&
                !this._authoritativeEmptyLimits(item)
            ) {
                let authenticatedPartial = this._authenticatedPartial(item);
                let resetlessBrowserUsage = this._hasResetlessBrowserUsage(item);
                let hadFreshWindow = Boolean(
                    item.five_hour || item.weekly || this._hasDynamicWindows(item)
                );
                let usedCachedWindow = false;
                let itemValuesCapturedAt = this._valuesCaptureForExpiry(item);
                let oldValuesCapturedAt = this._valuesCaptureForExpiry(old);
                if (
                    !authenticatedPartial &&
                    !resetlessBrowserUsage &&
                    !this._windowHasUsageValue(item.five_hour) &&
                    old.five_hour
                ) {
                    let mergedFive = this._mergeCachedWindow(
                        item.five_hour,
                        old.five_hour,
                        itemValuesCapturedAt,
                        oldValuesCapturedAt,
                        "five_hour"
                    );
                    usedCachedWindow = usedCachedWindow || mergedFive !== item.five_hour;
                    item.five_hour = mergedFive;
                } else if (
                    this._windowHasUsageValue(item.five_hour) &&
                    item.five_hour &&
                    !item.five_hour.reset_at &&
                    old.five_hour &&
                    old.five_hour.reset_at
                ) {
                    let mergedFive = this._mergeMissingReset(
                        item.five_hour,
                        old.five_hour,
                        item.captured_at,
                        oldValuesCapturedAt,
                        "five_hour"
                    );
                    usedCachedWindow = usedCachedWindow || mergedFive !== item.five_hour;
                    item.five_hour = mergedFive;
                }
                if (
                    !authenticatedPartial &&
                    !resetlessBrowserUsage &&
                    !this._windowHasUsageValue(item.weekly) &&
                    old.weekly
                ) {
                    let mergedWeekly = this._mergeCachedWindow(
                        item.weekly,
                        old.weekly,
                        itemValuesCapturedAt,
                        oldValuesCapturedAt,
                        "weekly"
                    );
                    usedCachedWindow = usedCachedWindow || mergedWeekly !== item.weekly;
                    item.weekly = mergedWeekly;
                } else if (
                    this._windowHasUsageValue(item.weekly) &&
                    item.weekly &&
                    !item.weekly.reset_at &&
                    old.weekly &&
                    old.weekly.reset_at
                ) {
                    let mergedWeekly = this._mergeMissingReset(
                        item.weekly,
                        old.weekly,
                        item.captured_at,
                        oldValuesCapturedAt,
                        "weekly"
                    );
                    usedCachedWindow = usedCachedWindow || mergedWeekly !== item.weekly;
                    item.weekly = mergedWeekly;
                }
                if (
                    !resetlessBrowserUsage &&
                    this._mergeMissingPoolResets(
                        item,
                        old,
                        item.captured_at,
                        oldValuesCapturedAt
                    )
                ) {
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
            merged.push(this._markUsageStale(old));
        }
        return merged;
    },

    _mergeMissingPoolResets: function(fresh, cached, referenceAt, cachedCapturedAt) {
        let usedCachedWindow = false;
        let pairs = [{
            fresh: fresh && fresh.main,
            cached: cached && cached.main
        }];
        let freshModels = fresh && fresh.models && typeof fresh.models === "object"
            ? fresh.models
            : {};
        let cachedModels = cached && cached.models && typeof cached.models === "object"
            ? cached.models
            : {};
        let modelKeys = Object.keys(freshModels);
        for (let i = 0; i < modelKeys.length; i++) {
            let key = modelKeys[i];
            pairs.push({ fresh: freshModels[key], cached: cachedModels[key] });
        }
        for (let j = 0; j < pairs.length; j++) {
            if (this._mergeMissingPoolResetsForPool(
                pairs[j].fresh,
                pairs[j].cached,
                referenceAt,
                cachedCapturedAt
            )) {
                usedCachedWindow = true;
            }
        }
        return usedCachedWindow;
    },

    _mergeMissingPoolResetsForPool: function(freshPool, cachedPool, referenceAt, cachedCapturedAt) {
        if (
            !freshPool ||
            !cachedPool ||
            freshPool.available !== true ||
            cachedPool.available !== true ||
            !Array.isArray(freshPool.windows) ||
            !Array.isArray(cachedPool.windows) ||
            !this._hasUniqueWindowIdentities(freshPool.windows) ||
            !this._hasUniqueWindowIdentities(cachedPool.windows)
        ) {
            return false;
        }
        let cachedByIdentity = Object.create(null);
        let duplicateCachedIdentity = Object.create(null);
        for (let i = 0; i < cachedPool.windows.length; i++) {
            let identity = this._windowIdentityKey(cachedPool.windows[i]);
            if (identity === null) {
                continue;
            }
            if (Object.prototype.hasOwnProperty.call(cachedByIdentity, identity)) {
                duplicateCachedIdentity[identity] = true;
            } else {
                cachedByIdentity[identity] = cachedPool.windows[i];
            }
        }
        let mergedWindows = freshPool.windows.slice();
        let usedCachedWindow = false;
        for (let j = 0; j < freshPool.windows.length; j++) {
            let freshWindow = freshPool.windows[j];
            let identity = this._windowIdentityKey(freshWindow);
            if (
                identity === null ||
                duplicateCachedIdentity[identity] ||
                !Object.prototype.hasOwnProperty.call(cachedByIdentity, identity) ||
                !this._windowHasUsageValue(freshWindow) ||
                freshWindow.reset_at
            ) {
                continue;
            }
            let mergedWindow = this._mergeMissingReset(
                freshWindow,
                cachedByIdentity[identity],
                referenceAt,
                cachedCapturedAt
            );
            if (mergedWindow !== freshWindow) {
                mergedWindows[j] = mergedWindow;
                usedCachedWindow = true;
            }
        }
        if (usedCachedWindow) {
            freshPool.windows = mergedWindows;
        }
        return usedCachedWindow;
    },

    _markUsageStale: function(usage) {
        let stale = {};
        for (let key in usage) {
            if (Object.prototype.hasOwnProperty.call(usage, key)) {
                stale[key] = usage[key];
            }
        }
        stale.stale = true;
        stale.values_captured_at = stale.values_captured_at || stale.captured_at;
        if (stale.status === "ok") {
            stale.status = "partial";
        }
        return stale;
    },

    _clearInvalidatedUsage: function(usage) {
        let invalidated = this._markUsageStale(usage);
        invalidated.five_hour = null;
        invalidated.weekly = null;
        invalidated.credits = null;
        invalidated.main = null;
        invalidated.models = Object.create(null);
        invalidated.cost_windows = [];
        invalidated.usage_resets = this._safeUsageResets(null);
        invalidated.values_captured_at = null;
        if (invalidated.status === "ok") {
            invalidated.status = "partial";
        }
        return invalidated;
    },

    _backendIdentityPresent: function(value) {
        return Boolean(
            this._strictText(value && value.backend_user_id, 256) ||
            this._strictText(value && value.backend_account_id, 256)
        );
    },

    _backendIdentityIsIncomplete: function(candidate, known) {
        let candidateUser = this._strictText(candidate && candidate.backend_user_id, 256);
        let candidateAccount = this._strictText(candidate && candidate.backend_account_id, 256);
        let knownUser = this._strictText(known && known.backend_user_id, 256);
        let knownAccount = this._strictText(known && known.backend_account_id, 256);
        if (!this._backendIdentityPresent(known)) {
            return false;
        }
        if (candidateUser && knownUser && candidateUser !== knownUser) {
            return false;
        }
        if (candidateAccount && knownAccount && candidateAccount !== knownAccount) {
            return false;
        }
        if (candidateAccount && knownAccount && candidateAccount === knownAccount) {
            return false;
        }
        return Boolean(
            (knownUser && !candidateUser) ||
            (knownAccount && !candidateAccount)
        );
    },

    _backendIdentityMatches: function(left, right) {
        let leftUser = this._strictText(left && left.backend_user_id, 256);
        let rightUser = this._strictText(right && right.backend_user_id, 256);
        let leftAccount = this._strictText(left && left.backend_account_id, 256);
        let rightAccount = this._strictText(right && right.backend_account_id, 256);
        if (Boolean(leftAccount) !== Boolean(rightAccount)) {
            return false;
        }
        if (leftAccount) {
            if (leftAccount !== rightAccount) {
                return false;
            }
            return !leftUser || !rightUser || leftUser === rightUser;
        }
        return Boolean(leftUser) === Boolean(rightUser) &&
            (!leftUser || leftUser === rightUser);
    },

    _backendIdentityCompatible: function(left, right) {
        let leftUser = this._strictText(left && left.backend_user_id, 256);
        let rightUser = this._strictText(right && right.backend_user_id, 256);
        let leftAccount = this._strictText(left && left.backend_account_id, 256);
        let rightAccount = this._strictText(right && right.backend_account_id, 256);
        if (leftAccount || rightAccount) {
            return Boolean(
                leftAccount &&
                rightAccount &&
                leftAccount === rightAccount &&
                (!leftUser || !rightUser || leftUser === rightUser)
            );
        }
        return Boolean(leftUser && rightUser && leftUser === rightUser);
    },

    _backendProvenanceMatches: function(left, right) {
        let leftConfigured = this._safeBackend(left && left.backend_configured);
        let rightConfigured = this._safeBackend(right && right.backend_configured);
        let leftUsed = this._safeBackend(left && left.backend_used, true);
        let rightUsed = this._safeBackend(right && right.backend_used, true);
        if (!leftConfigured || !rightConfigured || !leftUsed || !rightUsed) {
            return false;
        }
        if (leftConfigured && rightConfigured && leftConfigured !== rightConfigured) {
            return false;
        }
        let authenticated = ["direct", "app-server"];
        if (leftUsed === "browser" || rightUsed === "browser") {
            return leftUsed === "browser" && rightUsed === "browser";
        }
        let leftAuthenticated = authenticated.indexOf(leftUsed) !== -1;
        let rightAuthenticated = authenticated.indexOf(rightUsed) !== -1;
        if (!leftAuthenticated || !rightAuthenticated) {
            return true;
        }
        if (leftUsed === rightUsed) {
            return true;
        }
        return this._hasBackendFallbackProof(left) || this._hasBackendFallbackProof(right);
    },

    _hasBackendFallbackProof: function(usage) {
        let backendUsed = this._safeBackend(usage && usage.backend_used, true);
        if (["direct", "app-server"].indexOf(backendUsed) === -1) {
            return false;
        }
        let reason = this._strictText(usage && usage.fallback_reason, MAX_TEXT_CHARS);
        if (
            reason === "previous direct limits retained after reset transition" ||
            reason === "previous authenticated limits retained after reset transition"
        ) {
            return true;
        }
        let fallbackPrefix = "app-server unavailable: ";
        let knownUnavailableDetails = [
            "codex command was not found",
            "codex command is not executable",
            "could not start codex app server",
            "codex app server exited unexpectedly",
            "installed Codex does not support rate-limit RPC"
        ];
        return backendUsed === "direct" &&
            this._safeBackend(usage && usage.backend_configured) === "app-server" &&
            reason.indexOf(fallbackPrefix) === 0 &&
            knownUnavailableDetails.indexOf(reason.slice(fallbackPrefix.length)) !== -1;
    },

    _authoritativeEmptyLimits: function(item) {
        return Boolean(
            item &&
            (item.status === "partial" ||
                (item.status === "error" && item.cache_invalidated === true)) &&
            !item.five_hour &&
            !item.weekly &&
            !this._hasDynamicWindows(item) &&
            ["direct", "app-server"].indexOf(item.backend_used) !== -1
        );
    },

    _authenticatedPartial: function(item) {
        return Boolean(
            item &&
            item.status === "partial" &&
            ["direct", "app-server"].indexOf(item.backend_used) !== -1
        );
    },

    _hasCachedWindows: function(usage) {
        return Boolean(usage && (usage.five_hour || usage.weekly || this._hasDynamicWindows(usage)));
    },

    _hasDynamicWindows: function(usage) {
        if (!usage) {
            return false;
        }
        if (usage.main && Array.isArray(usage.main.windows) && usage.main.windows.length) {
            return true;
        }
        let models = usage.models && typeof usage.models === "object" ? usage.models : {};
        return Object.keys(models).some(function(key) {
            return models[key] && Array.isArray(models[key].windows) && models[key].windows.length;
        });
    },

    _windowHasUsageValue: function(window) {
        return this._remainingPercent(window) !== null;
    },

    _hasResetlessBrowserUsage: function(usage) {
        if (this._safeBackend(usage && usage.backend_used, true) !== "browser") {
            return false;
        }
        let windows = [usage && usage.five_hour, usage && usage.weekly];
        if (usage && usage.main && Array.isArray(usage.main.windows)) {
            windows = windows.concat(usage.main.windows);
        }
        let models = usage && usage.models && typeof usage.models === "object"
            ? usage.models
            : {};
        let modelKeys = Object.keys(models);
        for (let j = 0; j < modelKeys.length; j++) {
            let pool = models[modelKeys[j]];
            if (pool && Array.isArray(pool.windows)) {
                windows = windows.concat(pool.windows);
            }
        }
        for (let i = 0; i < windows.length; i++) {
            if (windows[i] && this._windowHasUsageValue(windows[i]) && !windows[i].reset_at) {
                return true;
            }
        }
        return false;
    },

    _windowResetExpired: function(window, referenceAt) {
        if (!window || !window.reset_at) {
            return false;
        }
        let resetMs = this._dateMillis(window.reset_at);
        let referenceMs = this._dateMillis(referenceAt);
        return resetMs === null || referenceMs === null || resetMs <= referenceMs;
    },

    _windowKind: function(window) {
        let name = this._safeText(window && window.name, 40).toLowerCase();
        name = name.replace(/[-\s]+/g, "_");
        if (["5h", "5_hour", "five_hour"].indexOf(name) !== -1) {
            return "five_hour";
        }
        if (["w", "week", "weekly"].indexOf(name) !== -1) {
            return "weekly";
        }
        if (["30d", "30_day", "month", "monthly"].indexOf(name) !== -1) {
            return "thirty_day";
        }
        return "";
    },

    _isInferredInactiveFiveHour: function(window) {
        let source = this._strictText(window && window.source, 120);
        return [
            "inferred:inactive-five-hour:direct",
            "inferred:inactive-five-hour:app-server"
        ].indexOf(source) !== -1;
    },

    _windowDurationSeconds: function(window) {
        if (window && Number.isInteger(window.duration_seconds) && window.duration_seconds > 0) {
            return window.duration_seconds;
        }
        let raw = this._safeText(window && window.raw, 500);
        let match = /"limit_window_seconds"\s*:\s*([0-9]+(?:\.[0-9]+)?)/.exec(raw);
        if (!match) {
            return null;
        }
        let value = Number(match[1]);
        if (!Number.isFinite(value) || value <= 0 || value > 315360000 || !Number.isInteger(value)) {
            return null;
        }
        return value;
    },

    _modelPool: function(usage, key) {
        if (!usage || !usage.models || typeof usage.models !== "object") {
            return null;
        }
        if (!Object.prototype.hasOwnProperty.call(usage.models, key)) {
            return null;
        }
        let pool = usage.models[key];
        return pool && typeof pool === "object" && !Array.isArray(pool)
            ? pool
            : null;
    },

    _poolIsUsable: function(pool) {
        if (!pool || pool.available !== true) {
            return false;
        }
        if (!Array.isArray(pool.windows) || !pool.windows.length ||
            !this._hasUniqueWindowIdentities(pool.windows) ||
            !pool.windows.every(Lang.bind(this, function(window) {
                let value = this._remainingPercent(window);
                return value !== null && value > 0 && this._windowIdentityIsKnown(window);
            }))) {
            return false;
        }
        if (pool.allowed !== null && pool.allowed !== undefined && pool.allowed !== true) {
            return false;
        }
        if (pool.limit_reached !== null && pool.limit_reached !== undefined && pool.limit_reached !== false) {
            return false;
        }
        if (pool.exhausted !== false) {
            return false;
        }
        return true;
    },

    _windowIdentityIsKnown: function(window) {
        let duration = this._windowDurationSeconds(window);
        if (duration !== null) {
            let namedDuration = {
                "5h": 18000,
                "5_hour": 18000,
                "five_hour": 18000,
                "w": 604800,
                "week": 604800,
                "weekly": 604800,
                "30d": 2592000,
                "30_day": 2592000,
                "month": 2592000,
                "monthly": 2592000
            };
            let named = this._strictText(window && window.name, 40).toLowerCase();
            if (!named) {
                return true;
            }
            if (Object.prototype.hasOwnProperty.call(namedDuration, named)) {
                return duration === namedDuration[named];
            }
            let canonical = duration % 86400 === 0
                ? (duration / 86400) + "d"
                : (duration % 3600 === 0
                    ? (duration / 3600) + "h"
                    : duration + "s");
            return named === canonical;
        }
        let name = this._strictText(window && window.name, 40).toLowerCase();
        return [
            "5h", "5_hour", "five_hour",
            "w", "week", "weekly",
            "30d", "30_day", "month", "monthly"
        ].indexOf(name) !== -1;
    },

    _windowIdentityKey: function(window) {
        if (!this._windowIdentityIsKnown(window)) {
            return null;
        }
        let duration = this._windowDurationSeconds(window);
        if (duration !== null) {
            return duration;
        }
        let name = this._strictText(window && window.name, 40).toLowerCase();
        let namedDuration = {
            "5h": 18000,
            "5_hour": 18000,
            "five_hour": 18000,
            "w": 604800,
            "week": 604800,
            "weekly": 604800,
            "30d": 2592000,
            "30_day": 2592000,
            "month": 2592000,
            "monthly": 2592000
        };
        return Object.prototype.hasOwnProperty.call(namedDuration, name)
            ? namedDuration[name]
            : null;
    },

    _hasUniqueWindowIdentities: function(windows) {
        if (!Array.isArray(windows)) {
            return false;
        }
        let seen = Object.create(null);
        for (let i = 0; i < windows.length; i++) {
            let identity = this._windowIdentityKey(windows[i]);
            if (identity === null || Object.prototype.hasOwnProperty.call(seen, identity)) {
                return false;
            }
            seen[identity] = true;
        }
        return true;
    },

    _hasDuplicateWindowIdentities: function(windows) {
        if (!Array.isArray(windows)) {
            return true;
        }
        let seen = Object.create(null);
        for (let i = 0; i < windows.length; i++) {
            let identity = this._windowIdentityKey(windows[i]);
            if (identity === null) {
                continue;
            }
            if (Object.prototype.hasOwnProperty.call(seen, identity)) {
                return true;
            }
            seen[identity] = true;
        }
        return false;
    },

    _poolWindowForDuration: function(pool, durationSeconds) {
        if (!pool || !Array.isArray(pool.windows)) {
            return null;
        }
        let matches = [];
        for (let i = 0; i < pool.windows.length; i++) {
            if (this._windowIdentityKey(pool.windows[i]) === durationSeconds) {
                matches.push(pool.windows[i]);
            }
        }
        return matches.length === 1 ? matches[0] : null;
    },

    _poolAverage: function(pool) {
        let five = this._remainingPercent(this._poolWindowForDuration(pool, 18000));
        let week = this._remainingPercent(this._poolWindowForDuration(pool, 604800));
        return five === null || week === null ? null : (five + week) / 2;
    },

    _poolOtherWindow: function(pool, excludeMonthly) {
        if (!pool || !Array.isArray(pool.windows)) {
            return null;
        }
        if (!this._hasUniqueWindowIdentities(pool.windows)) {
            return null;
        }
        let excludedDurations = [18000, 604800];
        if (excludeMonthly === true) {
            excludedDurations.push(2592000);
        }
        let selected = null;
        let selectedValue = null;
        for (let i = 0; i < pool.windows.length; i++) {
            let window = pool.windows[i];
            if (excludedDurations.indexOf(this._windowIdentityKey(window)) !== -1) {
                continue;
            }
            let value = this._remainingPercent(window);
            if (!selected || (value !== null && (selectedValue === null || value < selectedValue))) {
                selected = window;
                selectedValue = value;
            }
        }
        return selected;
    },

    _windowDisplayLabel: function(window) {
        let duration = this._windowIdentityKey(window);
        if (duration === 18000) {
            return "5h";
        }
        if (duration === 604800) {
            return "Woche";
        }
        if (duration === 2592000) {
            return "30d";
        }
        return this._safeText(window && window.name, 40) || "Limit";
    },

    _windowDurationMatches: function(current, cached, expectedKind) {
        let currentKind = this._windowKind(current);
        let cachedKind = this._windowKind(cached);
        let currentDuration = this._windowDurationSeconds(current);
        let cachedDuration = this._windowDurationSeconds(cached);
        if (expectedKind) {
            if (current && !currentKind && currentDuration === null) {
                return false;
            }
            if (cached && !cachedKind && cachedDuration === null) {
                return false;
            }
            if (currentKind && currentKind !== expectedKind) {
                return false;
            }
            if (cachedKind && cachedKind !== expectedKind) {
                return false;
            }
        }
        if (current && cached && Boolean(currentKind) !== Boolean(cachedKind)) {
            return false;
        }
        if (current && cached && currentKind && cachedKind && currentKind !== cachedKind) {
            return false;
        }
        if (!expectedKind && current && cached && !currentKind && !cachedKind &&
            currentDuration === null && cachedDuration === null) {
            return false;
        }
        let expected = {
            five_hour: 18000,
            weekly: 604800,
            thirty_day: 2592000
        }[expectedKind || currentKind || cachedKind] || null;
        if (expected !== null &&
            ((currentDuration !== null && currentDuration !== expected) ||
             (cachedDuration !== null && cachedDuration !== expected))) {
            return false;
        }
        return currentDuration === null || cachedDuration === null ||
            currentDuration === cachedDuration;
    },

    _windowCacheExpired: function(window, capturedAt, referenceAt) {
        if (!window) {
            return false;
        }
        if (this._isInferredInactiveFiveHour(window) && !window.reset_at) {
            return false;
        }
        if (window.reset_at) {
            let resetMs = this._dateMillis(window.reset_at);
            let referenceMs = this._dateMillis(referenceAt);
            let capturedMs = this._dateMillis(capturedAt);
            let duration = this._windowDurationSeconds(window);
            if (duration === null) {
                duration = {
                    five_hour: 18000,
                    weekly: 604800,
                    thirty_day: 2592000
                }[this._windowKind(window)] || null;
            }
            if (resetMs === null || referenceMs === null || capturedMs === null) {
                return true;
            }
            if (
                duration !== null &&
                capturedMs !== null &&
                resetMs > capturedMs + duration * 1000 + MAX_CAPTURE_FUTURE_MS
            ) {
                return true;
            }
            return resetMs <= referenceMs;
        }
        let duration = this._windowDurationSeconds(window);
        if (duration === null) {
            duration = {
                five_hour: 18000,
                weekly: 604800,
                thirty_day: 2592000
            }[this._windowKind(window)] || null;
        }
        let capturedMs = this._dateMillis(capturedAt);
        let referenceMs = this._dateMillis(referenceAt);
        if (duration === null || capturedMs === null || referenceMs === null) {
            return true;
        }
        return capturedMs + duration * 1000 <= referenceMs;
    },

    _valuesCaptureForExpiry: function(usage) {
        let capturedAt = this._safeText(usage && usage.captured_at, 80);
        let valuesCapturedAt = this._safeText(usage && usage.values_captured_at, 80);
        let capturedMs = this._dateMillis(capturedAt);
        let valuesCapturedMs = this._dateMillis(valuesCapturedAt);
        if (valuesCapturedMs !== null &&
            capturedMs !== null &&
            valuesCapturedMs <= capturedMs) {
            return valuesCapturedAt;
        }
        return capturedAt;
    },

    _mergeCachedWindow: function(fresh, cached, referenceAt, cachedCapturedAt, expectedKind) {
        if (this._isInferredInactiveFiveHour(fresh)) {
            return fresh;
        }
        if (fresh && fresh.reset_at && this._windowResetExpired(fresh, referenceAt)) {
            return fresh;
        }
        if (!this._windowDurationMatches(fresh, cached, expectedKind)) {
            return fresh;
        }
        if (this._windowCacheExpired(cached, cachedCapturedAt, referenceAt)) {
            return fresh;
        }
        if (!fresh || !fresh.reset_at) {
            return cached;
        }
        let merged = {};
        let keys = Object.keys(cached);
        for (let i = 0; i < keys.length; i++) {
            merged[keys[i]] = cached[keys[i]];
        }
        merged.reset_at = fresh.reset_at;
        return merged;
    },

    _mergeMissingReset: function(fresh, cached, referenceAt, cachedCapturedAt, expectedKind) {
        if (this._isInferredInactiveFiveHour(fresh)) {
            return fresh;
        }
        if (!this._windowDurationMatches(fresh, cached, expectedKind)) {
            return fresh;
        }
        if (this._windowCacheExpired(cached, cachedCapturedAt, referenceAt)) {
            return fresh;
        }
        if (!fresh || fresh.reset_at || !cached || !cached.reset_at) {
            return fresh;
        }
        let merged = {};
        let keys = Object.keys(fresh);
        for (let i = 0; i < keys.length; i++) {
            merged[keys[i]] = fresh[keys[i]];
        }
        merged.reset_at = cached.reset_at;
        return merged;
    },

    _connectTrackedSignal: function(target, signal, callback) {
        if (!target || typeof target.connect !== "function") {
            throw new Error("menu signal target unavailable");
        }
        let id = target.connect(signal, callback);
        if (id) {
            this._signalConnections.push({ target: target, id: id });
        }
        return id;
    },

    _disconnectTrackedSignals: function() {
        let connections = Array.isArray(this._signalConnections)
            ? this._signalConnections : [];
        this._signalConnections = [];
        for (let i = 0; i < connections.length; i++) {
            let connection = connections[i];
            try {
                if (connection.target && typeof connection.target.disconnect === "function") {
                    connection.target.disconnect(connection.id);
                }
            } catch (e) {
                if (!this._removed) {
                    global.log("[" + UUID + "] menu signal cleanup failed: " + this._shortText(e, 160));
                }
            }
        }
    },

    _buildLoadingMenu: function(message) {
        if (this._removed || !this.menu) {
            return;
        }
        this.menu.removeAll();
        this._addDisabled(this.menu, message || _("Lade …"), "codex-usage-stale");
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addActions();
    },

    _buildUsageMenu: function() {
        if (this._removed || !this.menu) {
            return;
        }
        if (this.menu && this.menu.isOpen) {
            this._menuDirty = true;
            return;
        }
        this._menuDirty = false;
        if (this._safeMode) {
            this._buildSafeMenu();
            return;
        }
        this.menu.removeAll();
        let fastStatus = this._fastModeStatusText();
        if (fastStatus) {
            this._addDisabled(this.menu, fastStatus, "codex-usage-panel-warning");
            this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        }
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
                if (
                    i === 0 &&
                    this._displaySeparatorEnabled(this._usages[i].account, "click")
                ) {
                    this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
                }
                this._addAccount(this._usages[i]);
                if (i < this._usages.length - 1) {
                    this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
                }
            }
        }
        if (this._commandError) {
            this._addDisabled(this.menu, this._commandError, "codex-usage-error");
        }
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addActions();
    },

    _addAccount: function(usage) {
        let five = this._percentParts(
            this._fiveHourDisplayWindow(usage), usage.account, "click"
        );
        let week = this._percentParts(usage.weekly, usage.account, "click");
        let monthlyWindow = this._poolIsUsable(usage.main)
            ? this._poolWindowForDuration(usage.main, 2592000)
            : null;
        let monthly = this._percentParts(monthlyWindow, usage.account, "click");
        let severity = this._usageSeverity(usage);
        let display = this._accountDisplayText({ usage: usage }, "click");
        let summaryParts = display ? [display] : [];
        let summaryMarkupParts = display ? [this._escapeMarkup(display)] : [];
        if (five.plain) {
            summaryParts.push("5h " + five.plain);
            summaryMarkupParts.push(this._escapeMarkup("5h ") + five.markup);
        }
        if (week.plain) {
            summaryParts.push("Woche " + week.plain);
            summaryMarkupParts.push(this._escapeMarkup("Woche ") + week.markup);
        }
        if (monthly.plain) {
            summaryParts.push("Monat " + monthly.plain);
            summaryMarkupParts.push(this._escapeMarkup("Monat ") + monthly.markup);
        }
        let summary = summaryParts.join("     ");
        let summaryMarkup = summaryMarkupParts.join(this._escapeMarkup("     "));
        let accountGroup = new PopupMenu.PopupSubMenuMenuItem(summary);
        this._setItemMarkup(accountGroup, summaryMarkup);
        try {
            accountGroup.actor.add_style_class_name("codex-usage-account " + severity);
        } catch (e) {
            global.log("[" + UUID + "] account group style failed: " + String(e));
        }
        let accountMenu = accountGroup.menu;
        this.menu.addMenuItem(accountGroup);
        this._addResetDetail(usage, accountMenu);
        let credits = this._creditParts(usage, "click");
        if (credits) {
            this._addDisabled(accountMenu, credits.plain, "codex-usage-detail");
        }
        let creditConsumption = this._creditConsumptionParts(usage, "click");
        if (creditConsumption) {
            this._addDisabled(accountMenu, creditConsumption.plain, "codex-usage-detail");
        }
        let consumption = this._consumptionParts(usage, "click");
        if (consumption) {
            let consumptionItem = new PopupMenu.PopupMenuItem(consumption.plain);
            consumptionItem.connect("activate", Lang.bind(this, function() {
                this._runSafely("consumption delta toggle", Lang.bind(this, function() {
                    this.showConsumptionDelta = !this.showConsumptionDelta;
                    try {
                        this.settings.setValue("show-consumption-delta", this.showConsumptionDelta);
                    } catch (e) {
                        global.log("[" + UUID + "] consumption delta setting failed: " + String(e));
                    }
                    this._refreshFormattedSurfaces();
                }));
            }));
            try {
                consumptionItem.actor.add_style_class_name("codex-usage-detail");
            } catch (e) {
                global.log("[" + UUID + "] consumption item style failed: " + String(e));
            }
            this._setItemMarkup(consumptionItem, consumption.markup);
            accountMenu.addMenuItem(consumptionItem);
        }
        let resets = this._usageResetParts(usage, "click");
        if (resets) {
            let resetItem = this._addDisabled(accountMenu, resets.plain, "codex-usage-detail");
            this._setItemMarkup(resetItem, resets.markup);
        }
        this._addDynamicLimitDetails(usage, accountMenu);
        this._addAccountControls(usage, accountMenu);
        this._addAccountTerminalAction(usage, accountMenu);
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
                accountMenu,
                status,
                usage.status === "ok" ? "codex-usage-stale" : "codex-usage-error"
            );
        }
        if (usage.status === "login_required" && this.showReactivationActions) {
            this._addReactivationAction(usage, accountMenu);
        }
    },

    _addAccountControls: function(usage, targetMenu) {
        let target = targetMenu || this.menu;
        let panel = this._panelSettings[usage.account] || this._defaultPanelRow(usage.account, 1);
        let alert = this._alertSettings[usage.account] || this._defaultAlertRow(usage.account);
        let submenu = new PopupMenu.PopupSubMenuMenuItem(
            this._accountDisplayText({ usage: usage }, "click") + " steuern"
        );
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
        let loginActive = Boolean(this._deviceLoginActive[usage.account]);
        if (loginActive) {
            this._addDisabled(submenu.menu, "Device-Login läuft …", "codex-usage-warning");
            let cancelLogin = new PopupMenu.PopupMenuItem("Device-Login abbrechen");
            if (typeof cancelLogin.connect === "function") {
                cancelLogin.connect("activate", Lang.bind(this, function() {
                    this._runSafely("device login cancel", Lang.bind(this, function() {
                        this._cancelDeviceLogin(usage.account);
                    }));
                }));
            }
            submenu.menu.addMenuItem(cancelLogin);
        } else {
            let deviceLogin = new PopupMenu.PopupMenuItem("Device-Login starten");
            if (typeof deviceLogin.connect === "function") {
                deviceLogin.connect("activate", Lang.bind(this, function() {
                    this._runSafely("device login action", Lang.bind(this, function() {
                        this._startDeviceLogin(usage);
                    }));
                }));
            }
            submenu.menu.addMenuItem(deviceLogin);
        }
        let manageAccount = new PopupMenu.PopupMenuItem("Manage Account");
        if (typeof manageAccount.connect === "function") {
            manageAccount.connect("activate", Lang.bind(this, function() {
                this._runSafely("manage account action", Lang.bind(this, function() {
                    this._manageAccount(usage);
                }));
            }));
        }
        submenu.menu.addMenuItem(manageAccount);
        if (this._deviceLoginErrors[usage.account]) {
            this._addDisabled(
                submenu.menu,
                this._shortText(this._deviceLoginErrors[usage.account], 160),
                "codex-usage-error"
            );
        }
        if (this._accountManageErrors && this._accountManageErrors[usage.account]) {
            this._addDisabled(
                submenu.menu,
                "Manage Account: " + this._shortText(this._accountManageErrors[usage.account], 140),
                "codex-usage-error"
            );
        }
        let loginEvents = this._deviceLoginEvents[usage.account] || [];
        for (let eventIndex = 0; eventIndex < loginEvents.length; eventIndex++) {
            let event = loginEvents[eventIndex];
            this._addDisabled(
                submenu.menu,
                "Device-Login " + event.kind + ": " + event.value,
                "codex-usage-detail"
            );
            let copy = new PopupMenu.PopupMenuItem(
                event.kind === "url" ? "Device-Login URL kopieren" : "Device-Code kopieren"
            );
            if (typeof copy.connect === "function") {
                copy.connect("activate", Lang.bind(this, function() {
                    this._copyDeviceLoginEvent(event);
                }));
            }
            submenu.menu.addMenuItem(copy);
        }
        target.addMenuItem(submenu);
    },

    _addAccountTerminalAction: function(usage, targetMenu) {
        let target = targetMenu || this.menu;
        let startTerminal = new PopupMenu.PopupMenuItem("Start Terminal as User");
        if (typeof startTerminal.connect === "function") {
            startTerminal.connect("activate", Lang.bind(this, function() {
                this._runSafely("account terminal action", Lang.bind(this, function() {
                    this._startAccountTerminal(usage);
                }));
            }));
        }
        target.addMenuItem(startTerminal);
        if (this._accountTerminalErrors && this._accountTerminalErrors[usage.account]) {
            this._addDisabled(
                target,
                "Start Terminal as User: " + this._shortText(
                    this._accountTerminalErrors[usage.account],
                    140
                ),
                "codex-usage-error"
            );
        }
    },

    _loadProfileJobs: function() {
        if (this._profileJobsLoaded || this._removed || this._safeMode) {
            return;
        }
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            return;
        }
        argv.push("profile", "jobs", "--json");
        this._profileJobsLoaded = true;
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            if (
                error || !payload || payload.ok !== true ||
                !Array.isArray(payload.jobs) || payload.jobs.length > MAX_PROFILE_JOBS
            ) {
                this._profileJobsLoaded = false;
                return;
            }
            let jobs = [];
            let seenJobAccounts = Object.create(null);
            let seenJobIds = Object.create(null);
            for (let index = 0; index < payload.jobs.length; index++) {
                let job = payload.jobs[index];
                let account;
                let jobId;
                let status;
                try {
                    account = this._strictText(job && job.account, 64);
                    jobId = this._strictText(job && job.job_id, 64);
                    status = this._strictText(job && job.status, 32);
                } catch (e) {
                    this._profileJobsLoaded = false;
                    return;
                }
                if (
                    !account || !/^[A-Za-z0-9_.-]{1,64}$/.test(account) ||
                    !jobId || !/^job-[0-9a-f]{32}$/.test(jobId) ||
                    ["queued", "running", "cancel_requested"].indexOf(status) === -1
                ) {
                    this._profileJobsLoaded = false;
                    return;
                }
                if (seenJobAccounts[account] || seenJobIds[jobId]) {
                    this._profileJobsLoaded = false;
                    return;
                }
                seenJobAccounts[account] = true;
                seenJobIds[jobId] = true;
                jobs.push({ account: account, jobId: jobId, status: status });
            }
            let previousJobIds = Object.create(null);
            let previousJobAccounts = Object.keys(this._deviceLoginJobs);
            for (let index = 0; index < previousJobAccounts.length; index++) {
                let account = previousJobAccounts[index];
                previousJobIds[account] = this._deviceLoginJobs[account];
            }
            this._profileJobResumeQueue = [];
            let discoveredAccounts = Object.create(null);
            let profileStateChanged = false;
            for (let index = 0; index < jobs.length; index++) {
                let account = jobs[index].account;
                discoveredAccounts[account] = true;
                this._profilePendingAccounts[account] = true;
                this._deviceLoginJobs[account] = jobs[index].jobId;
                this._deviceLoginActive[account] = true;
                delete this._deviceLoginErrors[account];
                if (
                    previousJobIds[account] &&
                    previousJobIds[account] !== jobs[index].jobId
                ) {
                    delete this._deviceLoginEvents[account];
                    delete this._deviceLoginLiveText[account];
                }
                this._profileJobResumeQueue.push(account);
            }
            let knownJobAccounts = Object.keys(this._deviceLoginJobs);
            for (let index = 0; index < knownJobAccounts.length; index++) {
                let account = knownJobAccounts[index];
                if (discoveredAccounts[account]) {
                    continue;
                }
                profileStateChanged = true;
                delete this._deviceLoginJobs[account];
                delete this._profilePendingAccounts[account];
                delete this._accountDeleteWaitingForProfileJob[account];
                let liveLogin = this._deviceLoginLiveAccount === account;
                if (!liveLogin) {
                    delete this._deviceLoginActive[account];
                    delete this._deviceLoginEvents[account];
                    delete this._deviceLoginLiveText[account];
                }
            }
            if (
                this._profileJobPollingAccount &&
                !discoveredAccounts[this._profileJobPollingAccount]
            ) {
                profileStateChanged = true;
                this._profileJobPollingAccount = "";
                this._deviceLoginPollGeneration += 1;
                this._removeSource("_deviceLoginPollId");
            }
            if ((jobs.length > 0 || profileStateChanged) && this._ensureBackendUsageRows()) {
                this._refreshFormattedSurfaces();
            }
            if (jobs.length > 0) {
                this._pollNextProfileJob();
            }
            if (jobs.length > 0 || profileStateChanged) {
                this._buildUsageMenu();
            }
        }), false, 10000);
    },

    _pollNextProfileJob: function() {
        if (
            this._removed || this._safeMode || this._profileJobPollingAccount ||
            this._backendChangeCurrent || this._backendChangeQueue.length ||
            this._accountChangeCurrent || this._accountChangeQueue.length
        ) {
            return;
        }
        while (this._profileJobResumeQueue.length) {
            let account = this._profileJobResumeQueue.shift();
            if (!this._deviceLoginJobs[account]) {
                continue;
            }
            this._pollProfileJob(account);
            return;
        }
    },

    _pollProfileJob: function(account, force) {
        let jobId = this._deviceLoginJobs[account];
        if (!jobId || this._removed || this._safeMode) {
            return;
        }
        if (
            force !== true &&
            (
                this._backendChangeCurrent || this._backendChangeQueue.length ||
                this._accountChangeCurrent || this._accountChangeQueue.length
            )
        ) {
            if (this._profileJobResumeQueue.indexOf(account) === -1) {
                this._profileJobResumeQueue.unshift(account);
            }
            if (this._profileJobPollingAccount === account) {
                this._profileJobPollingAccount = "";
            }
            return;
        }
        if (
            this._profileJobPollingAccount &&
            this._profileJobPollingAccount !== account
        ) {
            this._profileJobResumeQueue.unshift(this._profileJobPollingAccount);
        }
        this._profileJobPollingAccount = account;
        this._profileJobCommandAccount = account;
        this._removeSource("_deviceLoginPollId");
        let generation = ++this._deviceLoginPollGeneration;
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            this._finishProfileJob(
                account,
                String(e),
                false,
                force === true ? "status-force" : "status"
            );
            return;
        }
        argv.push("profile", "job-status", jobId, "--json");
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            if (
                generation !== this._deviceLoginPollGeneration ||
                this._deviceLoginJobs[account] !== jobId
            ) {
                return;
            }
            if (this._profileJobCommandAccount === account) {
                this._profileJobCommandAccount = "";
            }
            if (
                error || !payload || payload.account !== account ||
                payload.job_id !== jobId ||
                ["queued", "running", "cancel_requested", "completed", "failed", "cancelled"]
                    .indexOf(payload.status) === -1
            ) {
                this._finishProfileJob(
                    account,
                    error || "Profiljobstatus ungültig",
                    false,
                    force === true ? "status-force" : "status"
                );
                return;
            }
            if (payload.events !== undefined && !Array.isArray(payload.events)) {
                this._finishProfileJob(
                    account,
                    "Device-Login-Events ungültig",
                    false,
                    force === true ? "status-force" : "status"
                );
                return;
            }
            let events = this._safeDeviceLoginEvents(payload.events || []);
            if (payload.events && events.length !== payload.events.length) {
                this._finishProfileJob(
                    account,
                    "Device-Login-Events ungültig",
                    false,
                    force === true ? "status-force" : "status"
                );
                return;
            }
            if (events.length) {
                this._deviceLoginEvents[account] = events;
                this._buildUsageMenu();
            }
            if (["queued", "running", "cancel_requested"].indexOf(payload.status) !== -1) {
                this._scheduleProfileJobPoll(account, generation, force);
                return;
            }
            if (payload.status === "completed") {
                this._finishProfileJob(account, null, true);
                return;
            }
            this._finishProfileJob(
                account,
                payload.status === "cancelled"
                    ? "Device-Login abgebrochen"
                    : this._shortText(payload.error || "Device-Login fehlgeschlagen", 200),
                false
            );
        }), force === true, 10000);
    },

    _scheduleProfileJobPoll: function(account, generation, force, retryAction) {
        if (
            generation !== this._deviceLoginPollGeneration ||
            !this._deviceLoginJobs[account]
        ) {
            return;
        }
        this._removeSource("_deviceLoginPollId");
        let pollId = Mainloop.timeout_add(1000, Lang.bind(this, function() {
            this._clearSource("_deviceLoginPollId");
            if (
                generation !== this._deviceLoginPollGeneration ||
                this._removed ||
                this._safeMode ||
                !this._deviceLoginJobs[account]
            ) {
                return false;
            }
            if (retryAction === "cancel") {
                this._cancelProfileJob(account, true);
            } else {
                this._pollProfileJob(account, force);
            }
            return false;
        }));
        if (!pollId) {
            if (retryAction) {
                this._profileJobsLoaded = false;
                this._profileJobsResumeRequested = true;
                return;
            }
            this._finishProfileJob(
                account,
                "Device-Login-Status konnte nicht weiter geprüft werden",
                false,
                force === true ? "status-force" : "status"
            );
            return;
        }
        this._setSource("_deviceLoginPollId", pollId);
    },

    _finishProfileJob: function(account, error, success, retryAction) {
        let retry = retryAction === "status" ||
            retryAction === "status-force" ||
            retryAction === "cancel";
        let deleteWaiting = Boolean(this._accountDeleteWaitingForProfileJob[account]);
        if (!retry) {
            delete this._accountDeleteWaitingForProfileJob[account];
            delete this._profilePendingAccounts[account];
            let remainingResumeJobs = [];
            for (let index = 0; index < this._profileJobResumeQueue.length; index++) {
                if (this._profileJobResumeQueue[index] !== account) {
                    remainingResumeJobs.push(this._profileJobResumeQueue[index]);
                }
            }
            this._profileJobResumeQueue = remainingResumeJobs;
        }
        if (this._profileJobPollingAccount === account) {
            this._profileJobPollingAccount = "";
        }
        if (this._profileJobCommandAccount === account) {
            this._profileJobCommandAccount = "";
        }
        this._deviceLoginPollGeneration += 1;
        this._removeSource("_deviceLoginPollId");
        if (retry) {
            this._deviceLoginActive[account] = true;
        } else {
            delete this._deviceLoginJobs[account];
            delete this._deviceLoginActive[account];
            delete this._deviceLoginEvents[account];
            delete this._deviceLoginLiveText[account];
            if (this._deviceLoginLiveAccount === account) {
                this._deviceLoginLiveAccount = "";
            }
        }
        if (error) {
            this._deviceLoginErrors[account] = this._shortText(error, 200);
        } else if (success) {
            delete this._deviceLoginErrors[account];
            this._profileJobsLoaded = false;
        }
        this._buildUsageMenu();
        if (retry) {
            if (!deleteWaiting && this._ensureBackendUsageRows()) {
                this._refreshFormattedSurfaces();
            }
            this._scheduleProfileJobPoll(
                account,
                this._deviceLoginPollGeneration,
                retryAction === "status-force",
                retryAction
            );
            return;
        }
        if (success) {
            this._refreshFresh(false);
        } else if (!deleteWaiting && this._ensureBackendUsageRows()) {
            this._refreshFormattedSurfaces();
        }
        if (deleteWaiting) {
            this._drainAccountChanges();
        }
        this._pollNextProfileJob();
    },

    _cancelProfileJob: function(account, force) {
        let jobId = this._deviceLoginJobs[account];
        if (!jobId) {
            return false;
        }
        if (
            this._profileJobPollingAccount &&
            this._profileJobPollingAccount !== account
        ) {
            this._profileJobResumeQueue.unshift(this._profileJobPollingAccount);
            this._profileJobPollingAccount = "";
        }
        this._deviceLoginPollGeneration += 1;
        this._removeSource("_deviceLoginPollId");
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            this._finishProfileJob(account, String(e), false, "cancel");
            return true;
        }
        argv.push("profile", "cancel", jobId, "--json");
        this._profileJobCommandAccount = account;
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            if (this._deviceLoginJobs[account] !== jobId) {
                return;
            }
            if (this._profileJobCommandAccount === account) {
                this._profileJobCommandAccount = "";
            }
            if (
                error || !payload || payload.account !== account ||
                payload.job_id !== jobId ||
                ["cancel_requested", "cancelled", "completed", "failed"].indexOf(payload.status) === -1
            ) {
                this._finishProfileJob(
                    account,
                    error || "Device-Login konnte nicht abgebrochen werden",
                    false,
                    "cancel"
                );
                return;
            }
            if (payload.status === "cancel_requested") {
                this._pollProfileJob(account, true);
                return;
            }
            if (payload.status === "completed") {
                this._finishProfileJob(account, null, true);
                return;
            }
            this._finishProfileJob(
                account,
                payload.status === "cancelled"
                    ? "Device-Login abgebrochen"
                    : this._shortText(payload.error || "Device-Login fehlgeschlagen", 200),
                false
            );
        }), force === true, 10000);
        return true;
    },

    _startDeviceLogin: function(usage) {
        if (!usage || !usage.account || this._removed) {
            return;
        }
        if (Object.keys(this._deviceLoginActive).length > 0) {
            this._deviceLoginErrors[usage.account] =
                "Es läuft bereits ein Anmelde- oder Profiljob";
            this._buildUsageMenu();
            return;
        }
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            this._deviceLoginErrors[usage.account] = String(e);
            this._buildUsageMenu();
            return;
        }
        argv.push(
            "profile", "device-login", "--account", usage.account,
            "--timeout", "900", "--format", "json"
        );
        this._deviceLoginActive[usage.account] = true;
        this._deviceLoginLiveAccount = usage.account;
        this._deviceLoginLiveText[usage.account] = {
            stdout: "",
            stderr: ""
        };
        delete this._deviceLoginErrors[usage.account];
        this._buildUsageMenu();
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            delete this._deviceLoginActive[usage.account];
            delete this._deviceLoginEvents[usage.account];
            delete this._deviceLoginLiveText[usage.account];
            if (this._deviceLoginLiveAccount === usage.account) {
                this._deviceLoginLiveAccount = "";
            }
            if (
                error || !payload || payload.account !== usage.account ||
                typeof payload.ok !== "boolean"
            ) {
                this._deviceLoginErrors[usage.account] = this._shortText(
                    error || "Device-Login fehlgeschlagen",
                    200
                );
                this._buildUsageMenu();
                return;
            }
            if (payload.ok !== true) {
                this._deviceLoginErrors[usage.account] = this._shortText(
                    payload.error || "Device-Login fehlgeschlagen",
                    200
                );
                this._buildUsageMenu();
                return;
            }
            delete this._deviceLoginErrors[usage.account];
            this._refreshFresh(false);
        }), false, DEVICE_LOGIN_TIMEOUT_MS);
    },

    _manageAccount: function(usage) {
        if (!usage || !usage.account || this._removed) {
            return;
        }
        if (!this._accountManageErrors) {
            this._accountManageErrors = Object.create(null);
        }
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            this._accountManageErrors[usage.account] = String(e);
            this._buildUsageMenu();
            return;
        }
        argv.push("account", "manage", usage.account, "--format", "json");
        delete this._accountManageErrors[usage.account];
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            if (
                error || !payload || payload.account !== usage.account ||
                payload.ok !== true ||
                payload.url !== "https://chatgpt.com/codex/cloud/settings/analytics#usage"
            ) {
                this._accountManageErrors[usage.account] = this._shortText(
                    error || (payload && payload.error) || "Account konnte nicht geöffnet werden",
                    200
                );
                this._buildUsageMenu();
            }
        }), false, AUX_COMMAND_TIMEOUT_MS);
    },

    _startAccountTerminal: function(usage) {
        if (!usage || !usage.account || this._removed) {
            return;
        }
        if (!this._accountTerminalErrors) {
            this._accountTerminalErrors = Object.create(null);
        }
        let argv;
        try {
            argv = this._baseCommandArgv();
        } catch (e) {
            this._accountTerminalErrors[usage.account] = String(e);
            this._buildUsageMenu();
            return;
        }
        argv.push("account", "terminal", usage.account, "--format", "json");
        delete this._accountTerminalErrors[usage.account];
        this._spawnAuxJson(argv, Lang.bind(this, function(payload, error) {
            if (
                error || !payload || payload.account !== usage.account ||
                payload.ok !== true || typeof payload.profile_dir !== "string" ||
                !payload.profile_dir
            ) {
                this._accountTerminalErrors[usage.account] = this._shortText(
                    error || (payload && payload.error) || "Terminal konnte nicht gestartet werden",
                    200
                );
                this._buildUsageMenu();
            }
        }), false, AUX_COMMAND_TIMEOUT_MS);
    },

    _cancelDeviceLogin: function(account) {
        if (!account || !this._deviceLoginActive[account]) {
            return;
        }
        if (this._deviceLoginJobs[account]) {
            this._cancelProfileJob(account);
            return;
        }
        let running = this._auxCommand === "device-login" &&
            this._deviceLoginLiveAccount === account;
        let removedQueued = false;
        if (!running && Array.isArray(this._backendAuxQueue)) {
            let remaining = [];
            for (let index = 0; index < this._backendAuxQueue.length; index++) {
                let request = this._backendAuxQueue[index];
                let argv = request && Array.isArray(request.argv) ? request.argv : [];
                let accountIndex = argv.indexOf("--account");
                let isDeviceLoginRequest = false;
                for (let token = 0; token < argv.length - 1; token++) {
                    if (argv[token] === "profile" && argv[token + 1] === "device-login") {
                        isDeviceLoginRequest = true;
                        break;
                    }
                }
                if (
                    isDeviceLoginRequest &&
                    accountIndex !== -1 &&
                    argv[accountIndex + 1] === account
                ) {
                    removedQueued = true;
                    continue;
                }
                remaining.push(request);
            }
            this._backendAuxQueue = remaining;
        }
        if (!running && !removedQueued) {
            return;
        }
        delete this._deviceLoginActive[account];
        delete this._deviceLoginEvents[account];
        delete this._deviceLoginLiveText[account];
        if (this._deviceLoginLiveAccount === account) {
            this._deviceLoginLiveAccount = "";
        }
        if (running) {
            this._cancelAuxProcess();
        }
        this._deviceLoginErrors[account] = "Device-Login abgebrochen";
        this._buildUsageMenu();
        this._runSafely("device login queue drain", Lang.bind(this, function() {
            this._drainBackendChanges();
            this._drainAccountChanges();
            this._drainDeferredAuxRequests();
            this._drainConsumptionRequests();
        }));
    },

    _recordDeviceLoginChunk: function(name, chunk, final) {
        let account = this._deviceLoginLiveAccount;
        if (!account || typeof chunk !== "string") {
            return;
        }
        let buffers = this._deviceLoginLiveText[account];
        if (!buffers || typeof buffers !== "object" || Array.isArray(buffers)) {
            buffers = { stdout: "", stderr: "" };
        }
        if (name === "stdout" || name === "stderr") {
            buffers[name] = (String(buffers[name] || "") + chunk).slice(-4096);
        } else if (final !== true) {
            return;
        }
        this._deviceLoginLiveText[account] = buffers;
        let parsedEvents = [];
        ["stdout", "stderr"].forEach(Lang.bind(this, function(streamName) {
            let streamEvents = this._deviceLoginEventsFromText(
                String(buffers[streamName] || ""),
                final === true
            );
            for (let i = 0; i < streamEvents.length; i++) {
                parsedEvents.push(streamEvents[i]);
            }
        }));
        let previous = this._deviceLoginEvents[account] || [];
        let merged = previous.slice();
        for (let index = 0; index < parsedEvents.length && merged.length < 8; index++) {
            let event = parsedEvents[index];
            let duplicate = merged.some(function(existing) {
                return existing.kind === event.kind && existing.value === event.value;
            });
            if (!duplicate) {
                merged.push(event);
            }
        }
        let events = this._safeDeviceLoginEvents(merged);
        if (JSON.stringify(events) !== JSON.stringify(previous)) {
            this._deviceLoginEvents[account] = events;
            this._buildUsageMenu();
        }
    },

    _deviceLoginEventsFromText: function(text, final) {
        if (typeof text !== "string" || !text) {
            return [];
        }
        let completeText = final !== false;
        let events = [];
        let seen = Object.create(null);
        let cleaned = text.replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, "");
        let urlPattern =
            /https:\/\/[^\s\u0000-\u001f\u007f-\u009f<>"']{1,481}/gi;
        let urlMatch;
        while (events.length < 8 &&
            (urlMatch = urlPattern.exec(cleaned)) !== null) {
            let rawValue = urlMatch[0];
            let value = rawValue.replace(/[),.;]+$/g, "");
            let trailingUnbounded = !completeText &&
                urlMatch.index + rawValue.length === cleaned.length;
            let identity = "url\u0000" + value;
            if (!trailingUnbounded &&
                value.length <= "https://".length + 480 && !seen[identity]) {
                seen[identity] = true;
                events.push({ kind: "url", value: value });
            }
        }
        let codePattern = /\b(?:device[ \t]+code[ \t]*[:\uFF1A][ \t]*|one-time[ \t]+code(?:[ \t]*\([^\r\n)]{0,80}\))?[ \t]*(?:[:\uFF1A][ \t]*|\r?\n[ \t]*))([A-Za-z0-9][A-Za-z0-9_-]{3,127})(?![A-Za-z0-9_-])/gi;
        let match;
        while (events.length < 8 && (match = codePattern.exec(cleaned)) !== null) {
            let trailingUnbounded = !completeText &&
                match.index + match[0].length === cleaned.length;
            if (trailingUnbounded) {
                continue;
            }
            let identity = "code\u0000" + match[1];
            if (!seen[identity]) {
                seen[identity] = true;
                events.push({ kind: "code", value: match[1] });
            }
        }
        return this._safeDeviceLoginEvents(events);
    },

    _safeDeviceLoginEvents: function(events) {
        if (!Array.isArray(events) || events.length > 8) {
            return [];
        }
        let result = [];
        let seen = Object.create(null);
        for (let i = 0; i < events.length; i++) {
            let event = events[i];
            if (!event || typeof event !== "object" || Array.isArray(event)) {
                continue;
            }
            let kind = this._safeText(event.kind, 16);
            let value = this._safeText(event.value, 512);
            let validValue = kind === "url"
                ? /^https:\/\/[^\s\u0000-\u001f\u007f-\u009f<>"']{1,480}$/i.test(event.value)
                : kind === "code" &&
                  /^[A-Za-z0-9][A-Za-z0-9_-]{3,127}$/.test(event.value);
            if (validValue && value) {
                let identity = kind + "\u0000" + value;
                if (!seen[identity]) {
                    seen[identity] = true;
                    result.push({ kind: kind, value: value });
                }
            }
        }
        return result;
    },

    _copyDeviceLoginEvent: function(event) {
        if (!event || (event.kind !== "url" && event.kind !== "code") ||
            typeof event.value !== "string" || !event.value) {
            return false;
        }
        try {
            let clipboard = St.Clipboard.get_default();
            if (!clipboard || typeof clipboard.set_text !== "function") {
                throw new Error("clipboard unavailable");
            }
            clipboard.set_text(St.ClipboardType.CLIPBOARD, event.value);
            return true;
        } catch (e) {
            this._showCommandError("Device-Login konnte nicht kopiert werden");
            return false;
        }
    },

    _updateAccountPanelSetting: function(account, changes) {
        if (typeof account !== "string" || !account ||
            !changes || typeof changes !== "object" || Array.isArray(changes)) {
            return;
        }
        let current = this._panelSettings[account] || this._defaultPanelRow(account, 1);
        let candidate = {};
        Object.keys(current).forEach(function(key) { candidate[key] = current[key]; });
        Object.keys(changes).forEach(function(key) { candidate[key] = changes[key]; });
        let normalized = this._normalizePanelRow(candidate, account);
        if (!normalized) {
            return;
        }
        let rows = Array.isArray(this.accountPanelSettings)
            ? this.accountPanelSettings.filter(function(row) {
                return row && typeof row === "object" && !Array.isArray(row) &&
                    typeof row.account === "string" && row.account;
            })
            : [];
        let found = false;
        rows = rows.map(function(row) {
            if (row.account !== account) {
                return row;
            }
            found = true;
            return normalized;
        });
        if (!found) {
            rows.push(normalized);
        }
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
        let rows = Array.isArray(this.accountAlertSettings)
            ? this.accountAlertSettings.slice()
            : [];
        let found = false;
        rows = rows.map(function(row) {
            if (row.account !== account) {
                return row;
            }
            found = true;
            return normalized;
        });
        if (!found) {
            rows.push(normalized);
        }
        this.accountAlertSettings = rows;
        this._alertSettings = this._alertSettingsMap(rows);
        try {
            this.settings.setValue("account-alert-settings", rows);
        } catch (e) {
            global.log("[" + UUID + "] alert account setting failed: " + String(e));
        }
        this._updatePanel();
        if (this.menu && this.menu.isOpen) {
            this._buildUsageMenu();
        }
    },

    _addResetDetail: function(usage, targetMenu) {
        let target = targetMenu || this.menu;
        let five = this._windowResetParts(usage.five_hour, usage.account, "click", false);
        let week = this._windowResetParts(usage.weekly, usage.account, "click", false);
        let monthlyWindow = this._poolIsUsable(usage.main)
            ? this._poolWindowForDuration(usage.main, 2592000)
            : null;
        let monthly = this._windowResetParts(monthlyWindow, usage.account, "click", false);
        let backend = this._backendSummary(usage);
        let plainParts = [];
        let markupParts = [];
        if (five.plain) {
            plainParts.push("5h Reset " + five.plain);
            markupParts.push(this._escapeMarkup("5h Reset ") + five.markup);
        }
        if (week.plain) {
            plainParts.push("Woche Reset " + week.plain);
            markupParts.push(this._escapeMarkup("Woche Reset ") + week.markup);
        }
        if (monthly.plain) {
            plainParts.push("30d Reset " + monthly.plain);
            markupParts.push(this._escapeMarkup("30d Reset ") + monthly.markup);
        }
        plainParts.push("Abruf " + backend);
        markupParts.push(this._escapeMarkup("Abruf " + backend));
        let plain = plainParts.join("     ");
        let markup = markupParts.join(this._escapeMarkup("     "));
        let item = this._addDisabled(target, plain, "codex-usage-detail");
        this._setItemMarkup(item, markup);
    },

    _addDynamicLimitDetails: function(usage, targetMenu) {
        let target = targetMenu || this.menu;
        let main = this._poolWindowForDuration(usage.main, 2592000)
            ? this._poolDetailParts(
            usage.main,
            usage.account,
            "click",
            "",
            [18000, 604800],
            "Monat"
        ) : null;
        let spark = this._poolDetailParts(
            this._modelPool(usage, "gpt-5.3-codex-spark"),
            usage.account,
            "click",
            "Spark",
            []
        );
        let routing = this._routingDecisionParts(usage);
        [main, spark, routing].forEach(Lang.bind(this, function(parts) {
            if (!parts) {
                return;
            }
            let item = this._addDisabled(target, parts.plain, "codex-usage-detail");
            this._setItemMarkup(item, parts.markup);
        }));
    },

    _poolDetailParts: function(pool, account, surface, prefix, excludedDurations, labelOverride) {
        if (!pool) {
            return null;
        }
        if (
            pool.available === false ||
            pool.allowed === false ||
            pool.limit_reached === true ||
            pool.exhausted === true
        ) {
            let unavailable = (prefix || labelOverride || "Limit") + (
                pool.allowed === false
                    ? " nicht freigegeben"
                    : (pool.limit_reached === true || pool.exhausted === true
                        ? " erschöpft"
                        : " nicht verfügbar")
            );
            return { plain: unavailable, markup: this._escapeMarkup(unavailable) };
        }
        let excluded = excludedDurations || [];
        let rawWindows = Array.isArray(pool.windows) ? pool.windows : [];
        if (rawWindows.some(Lang.bind(this, function(window) {
            return !this._windowIdentityIsKnown(window) ||
                this._remainingPercent(window) === null;
        }))) {
            let unavailable = (prefix || labelOverride || "Limit") + " nicht verfügbar · Limit unbekannt";
            return { plain: unavailable, markup: this._escapeMarkup(unavailable) };
        }
        let windows = rawWindows.filter(Lang.bind(this, function(window) {
            return excluded.indexOf(this._windowIdentityKey(window)) === -1 &&
                this._windowIdentityIsKnown(window);
        }));
        if (!windows.length) {
            if (excluded.length) {
                return null;
            }
            let unknown = (prefix || labelOverride || "Limit") + " nicht verfügbar · Limit unbekannt";
            return { plain: unknown, markup: this._escapeMarkup(unknown) };
        }
        let plain = [];
        let markup = [];
        for (let i = 0; i < windows.length; i++) {
            let label = labelOverride || this._windowDisplayLabel(windows[i]);
            let percent = this._percentParts(windows[i], account, surface);
            let reset = this._windowResetParts(windows[i], account, surface, false);
            plain.push(label + (percent.plain ? " " + percent.plain : "") +
                (reset.plain ? " (" + reset.plain + ")" : ""));
            markup.push(
                this._escapeMarkup(label + (percent.markup ? " " : "")) + percent.markup +
                (reset.markup ? " (" + reset.markup + ")" : "")
            );
        }
        return {
            plain: (prefix ? prefix + " " : "") + plain.join(" · "),
            markup: this._escapeMarkup(prefix ? prefix + " " : "") + markup.join(" · ")
        };
    },

    _routingDecisionParts: function(usage) {
        let decision = this._routingDecisions && this._routingDecisions[usage.account];
        if (!decision) {
            return null;
        }
        let labels = {
            spark: "Spark",
            main: "Hauptmodell",
            credits: "Credits",
            blocked: "blockiert",
            unchanged: "unverändert"
        };
        let text = "Routing " + (labels[decision.decision] || decision.decision);
        if (decision.decision === "credits") {
            text += " · bezahlte Nutzung erlaubt";
        } else if (decision.paid_overage_allowed) {
            text += " · Credits freigegeben";
        }
        if (decision.policy_source) {
            text += " · Regel " + decision.policy_source;
        }
        return { plain: text, markup: this._escapeMarkup(text) };
    },

    _backendSummary: function(usage) {
        let configured;
        let used;
        try {
            configured = this._safeBackend(usage && usage.backend_configured);
            used = this._safeBackend(usage && usage.backend_used, true);
        } catch (e) {
            return "Unbekannt";
        }
        if (!configured || !used) {
            return "Unbekannt";
        }
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

    _addReactivationAction: function(usage, targetMenu) {
        let target = targetMenu || this.menu;
        let running = Boolean(this._reactivations[usage.account]);
        if (running) {
            this._addDisabled(
                target,
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
        target.addMenuItem(item);
        if (this._reactivationErrors[usage.account]) {
            this._addDisabled(
                target,
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
        let configured = this._backendAccounts && this._backendAccounts[usage.account];
        let reactivationBrowser = this.reactivationBrowser || "auto";
        if (
            configured &&
            Number.isInteger(configured["reactivation-browser"]) &&
            configured["reactivation-browser"] >= 0 &&
            configured["reactivation-browser"] <= 3
        ) {
            reactivationBrowser = ["auto", "vivaldi", "chromium", "firefox"][
                configured["reactivation-browser"]
            ];
        }
        argv.push(
            "reactivate",
            usage.account,
            "--browser",
            reactivationBrowser,
            "--format",
            "json"
        );
        this._spawnReactivation(usage, argv);
    },

    _spawnReactivation: function(usage, argv) {
        let record = { process: null, timeoutId: 0, done: false };
        this._reactivations[usage.account] = record;
        delete this._reactivationErrors[usage.account];
        try {
            this._buildUsageMenu();
        } catch (e) {
            record.done = true;
            delete this._reactivations[usage.account];
            this._reactivationErrors[usage.account] = this._shortText(
                _("Reaktivierung konnte nicht angezeigt werden: ") + String(e),
                240
            );
            global.log("[" + UUID + "] reactivation loading menu failed: " + this._shortText(e, 180));
            return;
        }
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
            let timeoutId = Mainloop.timeout_add(
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
            if (!timeoutId) {
                throw new Error("reactivation timeout source unavailable");
            }
            record.timeoutId = timeoutId;
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
            this._terminateChild(record.process, "reactivation process startup cleanup");
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
        let hasError = Boolean(this._commandError);
        let values = [];
        let hasWarning = false;
        for (let i = 0; i < selected.length; i++) {
            let item = selected[i];
            let usage = item.usage;
            if (["error", "login_required", "blocked"].indexOf(usage.status) !== -1) {
                hasError = true;
            }
            if (usage.stale || usage.status === "partial") {
                hasWarning = true;
            }
            for (let j = 0; j < item.slots.length; j++) {
                let slot = item.slots[j];
                if (slot.value !== null) {
                    values.push(slot.value);
                    if (slot.source !== 9 && slot.source !== 10 &&
                        slot.value <= this._panelThreshold(item, slot.source)) {
                        hasWarning = true;
                    }
                }
            }
        }
        let worst = values.length ? Math.min.apply(Math, values) : null;
        let panel = this._panelContent(selected);
        let surface = this._panelSurfaceState || {
            plain: null,
            markup: null,
            tooltip: null,
            icon: null,
        };
        this._panelSurfaceState = surface;
        if (this._fastModeIsActive()) {
            try {
                let icon = String(this.fastModeIcon || FAST_MODE_ICON);
                if (!/^[A-Za-z0-9_.-]+\.svg$/.test(icon)) {
                    icon = FAST_MODE_ICON;
                }
                if (typeof this.set_applet_icon_path === "function") {
                    let iconPath = (this.metadata.path || "") + "/icons/" + icon;
                    let iconState = "path:" + iconPath;
                    if (surface.icon !== iconState) {
                        this.set_applet_icon_path(iconPath);
                        surface.icon = iconState;
                    }
                }
            } catch (e) {
                if (typeof this.set_applet_icon_symbolic_name === "function") {
                    this.set_applet_icon_symbolic_name("dialog-warning-symbolic");
                    surface.icon = "symbolic:dialog-warning-symbolic";
                }
            }
        } else {
            if (typeof this.set_applet_icon_symbolic_name === "function") {
                let iconState = "symbolic:view-statistics-symbolic";
                if (surface.icon !== iconState) {
                    this.set_applet_icon_symbolic_name("view-statistics-symbolic");
                    surface.icon = iconState;
                }
            }
        }
        if (surface.plain !== panel.plain) {
            this.set_applet_label(panel.plain);
            surface.plain = panel.plain;
        }
        if (surface.markup !== panel.markup) {
            this._setPanelMarkup(panel.markup);
            surface.markup = panel.markup;
        }
        if (hasError) {
            this.actor.add_style_class_name("codex-usage-panel-error");
        } else if (worst !== null && worst <= 5) {
            this.actor.add_style_class_name("codex-usage-panel-critical");
        } else if (hasWarning) {
            this.actor.add_style_class_name("codex-usage-panel-warning");
        }
        let tooltip = this._tooltipContent();
        if (this._commandError) {
            let errorText = _("Fehler: ") + this._commandError;
            tooltip = {
                plain: errorText + (tooltip.plain ? "\n" + tooltip.plain : ""),
                markup: this._escapeMarkup(errorText) +
                    (tooltip.markup ? "\n" + tooltip.markup : "")
            };
        }
        if (this._refreshing) {
            let prefix = _("Aktualisiere …");
            tooltip = {
                plain: prefix + (tooltip.plain ? "\n" + tooltip.plain : ""),
                markup: this._escapeMarkup(prefix) +
                    (tooltip.markup ? "\n" + tooltip.markup : "")
            };
        }
        let emptyTooltip = _("Keine Codex-Nutzungswerte");
        let tooltipMarkup = tooltip.markup || this._escapeMarkup(emptyTooltip);
        if (surface.tooltip !== tooltipMarkup) {
            this.set_applet_tooltip(tooltipMarkup, true);
            surface.tooltip = tooltipMarkup;
        }
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

    _readFastModeState: function() {
        try {
            let result = GLib.file_get_contents(FAST_MODE_STATE_PATH);
            if (!result || !result[0]) {
                return { modes: {}, last_event: null };
            }
            let parsed = JSON.parse(ByteArray.toString(result[1]));
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
                return { modes: {}, last_event: null };
            }
            return {
                modes: parsed.modes && typeof parsed.modes === "object" ? parsed.modes : {},
                last_event: parsed.last_event && typeof parsed.last_event === "object"
                    ? parsed.last_event : null
            };
        } catch (e) {
            return { modes: {}, last_event: null };
        }
    },

    _refreshFastModeState: function() {
        this._fastModeState = this._readFastModeState();
    },

    _readEmergencyDisplayOverride: function(account) {
        if (typeof account !== "string" || !account) {
            return null;
        }
        try {
            let result = GLib.file_get_contents(EMERGENCY_DISPLAY_OVERRIDE_PATH);
            if (!result || !result[0]) {
                return null;
            }
            let payload = JSON.parse(ByteArray.toString(result[1]));
            let value = payload && payload[account];
            if (!value || value.active !== true || value.delta_enabled !== true) {
                return null;
            }
            let window = ["short", "weekly", "monthly", "spark"].indexOf(value.limit_window) !== -1
                ? value.limit_window : "short";
            return {limit_window: window};
        } catch (e) {
            return null;
        }
    },

    _fastModeIsActive: function() {
        let modes = this._fastModeState && this._fastModeState.modes;
        return Boolean(modes && Object.keys(modes).some(function(account) {
            return modes[account] && modes[account].state === "active";
        }));
    },

    _fastModeStatusText: function() {
        let modes = this._fastModeState && this._fastModeState.modes;
        let accounts = modes ? Object.keys(modes).filter(function(account) {
            return modes[account] && modes[account].state === "active";
        }) : [];
        if (accounts.length) {
            return "⚠ Fast-Modus aktiv · " + accounts.join(", ") +
                " · Hinweis alle 15 Minuten";
        }
        let event = this._fastModeState && this._fastModeState.last_event;
        if (event && event.mode === "flex" && event.account) {
            return "⚠ Flex-Modus · " + event.account + " · " +
                String(event.reason || "Fast beendet");
        }
        return "";
    },

    _panelItems: function() {
        let items = [];
        for (let i = 0; i < this._usages.length; i++) {
            let usage = this._usages[i];
            let fallback = this._defaultPanelRow(usage.account, i + 1);
            let settings = this._panelSettings[usage.account] || fallback;
            let sources = [];
            let seenSources = Object.create(null);
            for (let slotIndex = 1; slotIndex <= this._panelValueCount(); slotIndex++) {
                let source = this._strictIntegerSetting(settings["slot" + slotIndex]);
                if (source === null || source <= 0 || seenSources[source]) {
                    continue;
                }
                seenSources[source] = true;
                sources.push(source);
            }
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
                visible: !settings.muted &&
                    !(this.hideAccountWhenLongLimitExhausted === true &&
                        this._longLimitExhausted(usage)) && (
                    slots.length > 0 ||
                    this._elementTargetEnabled(
                        usage.account,
                        "consumption",
                        "panel",
                        this._consumptionSettings[usage.account] &&
                            this._consumptionSettings[usage.account]["show-panel"]
                    ) ||
                    this._elementTargetEnabled(
                        usage.account, "consumption-weekly", "panel",
                        this._consumptionSettings[usage.account] && this._consumptionSettings[usage.account]["show-panel"]
                    ) ||
                    this._elementTargetEnabled(
                        usage.account, "consumption-short", "panel",
                        this._consumptionSettings[usage.account] && this._consumptionSettings[usage.account]["show-panel"]
                    ) ||
                    this._elementTargetEnabled(
                        usage.account, "consumption-monthly", "panel",
                        this._consumptionSettings[usage.account] && this._consumptionSettings[usage.account]["show-panel"]
                    ) ||
                    this._elementTargetEnabled(
                        usage.account, "credits", "panel",
                        this._creditSettings && this._creditSettings[usage.account] && this._creditSettings[usage.account]["show-panel"]
                    ) ||
                    this._elementTargetEnabled(
                        usage.account, "credit-consumption", "panel",
                        this._creditSettings && this._creditSettings[usage.account] && this._creditSettings[usage.account]["consumption-show-panel"]
                    ) ||
                    this._elementTargetEnabled(
                        usage.account,
                        "forecast",
                        "panel",
                        this._consumptionSettings[usage.account] &&
                            this._consumptionSettings[usage.account]["show-panel"]
                    ) ||
                    this._elementTargetEnabled(
                        usage.account,
                        "usage-resets",
                        "panel",
                        this._resetSettings[usage.account] &&
                            this._resetSettings[usage.account]["show-panel"]
                    ) ||
                    this._elementTargetEnabled(usage.account, "account-id", "panel") ||
                    this._elementTargetEnabled(usage.account, "label", "panel") ||
                    this._elementTargetEnabled(usage.account, "tag", "panel")
                )
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
        let legacyPanel = slots.length === 0;
        let hasCreditSlot = item.slots.some(function(slot) { return slot.source === 9; });
        let hasCreditConsumptionSlot = item.slots.some(function(slot) { return slot.source === 10; });
        let consumption = legacyPanel ? this._consumptionParts(item.usage, "panel") : null;
        let creditConsumption = legacyPanel ? this._creditConsumptionParts(item.usage, "panel") : null;
        let credits = legacyPanel ? this._creditParts(item.usage, "panel") : null;
        let resets = legacyPanel ? this._usageResetParts(item.usage, "panel") : null;
        let slotPlain = slots.map(function(slot) { return slot.plain; }).join(" / ");
        let slotMarkup = slots.map(function(slot) { return slot.markup; }).join(" / ");
        let plain = tag + (slotPlain ? " " + slotPlain : "") +
            (consumption ? " " + consumption.plain : "") +
            ((!hasCreditConsumptionSlot && creditConsumption) ? " " + creditConsumption.plain : "") +
            ((!hasCreditSlot && credits) ? " " + credits.plain : "") +
            (resets ? " " + resets.plain : "");
        let markup = this._escapeMarkup(tag) + (slotMarkup ? " " + slotMarkup : "") +
            (consumption ? " " + consumption.markup : "") +
            ((!hasCreditConsumptionSlot && creditConsumption) ? " " + creditConsumption.markup : "") +
            ((!hasCreditSlot && credits) ? " " + credits.markup : "") +
            (resets ? " " + resets.markup : "");
        if (this.panelAccountSeparator === "brackets") {
            return {
                plain: "[" + plain + "]",
                markup: "[" + markup + "]"
            };
        }
        return { plain: plain, markup: markup };
    },

    _panelSlotContent: function(item, slot) {
        let result = this._panelSlotContentRaw(item, slot);
        if (!result || !PANEL_FORMATTING_TARGETS[slot.source]) {
            return result;
        }
        let styles = this._panelValueStyles && this._panelValueStyles[slot.source];
        let style = styles && styles[item.usage && item.usage.account];
        if (!style) {
            return result;
        }
        // Text-only values have no remaining-percent metric.  Treat them as
        // below threshold so copied style modes remain useful and predictable.
        return {
            plain: result.plain,
            markup: this._styleSpan(result.plain, style, 0, "panel")
        };
    },

    _panelSlotContentRaw: function(item, slot) {
        if (slot.source === 9) {
            let credits = this._creditParts(item.usage, "panel", true, "CR");
            return credits || { plain: "CR –", markup: "CR –" };
        }
        if (slot.source === 10) {
            let consumption = this._creditConsumptionParts(item.usage, "panel", true, "CV");
            return consumption || { plain: "CV –", markup: "CV –" };
        }
        let usage = item.usage;
        let account = usage.account;
        let label = this._panelSourceLabel(slot.source);
        if (slot.source === 11) {
            return this._usageResetParts(usage, "panel", true) || {plain: label + " –", markup: label + " –"};
        }
        if (slot.source === 12) {
            return this._panelForecastPart(usage, "panel") || {plain: label + " –", markup: label + " –"};
        }
        if (slot.source === 13 || (slot.source >= 32 && slot.source <= 36)) {
            return this._panelDeltaPart(usage, slot.source, "panel");
        }
        if (slot.source === 14) {
            let value = this._panelAccountTag(usage) || "–";
            return {plain: label + " " + value, markup: this._escapeMarkup(label + " " + value)};
        }
        if (slot.source === 15) {
            let value = this._safeText(usage.label, 120) || "–";
            return {plain: label + " " + value, markup: this._escapeMarkup(label + " " + value)};
        }
        if (slot.source === 16) {
            let value = this._safeText(usage.account, 64) || "–";
            return {plain: label + " " + value, markup: this._escapeMarkup(label + " " + value)};
        }
        if (slot.source === 17) {
            let value = this._backendSummary(usage);
            return {plain: label + " " + value, markup: this._escapeMarkup(label + " " + value)};
        }
        if (slot.source === 43) {
            let routing = this._routingDecisionParts(usage);
            return routing || {plain: label + " –", markup: label + " –"};
        }
        if (slot.source === 44) {
            let decision = this._routingDecisions && this._routingDecisions[account];
            let active = decision && (decision.decision === "credits" || decision.paid_overage_allowed === true);
            let text = label + " " + (active ? "an" : "aus");
            return {plain: text, markup: this._escapeMarkup(text)};
        }
        if (slot.source >= 45 && slot.source <= 47) {
            return this._panelCreditLimitPart(slot.source, label);
        }
        if (slot.source === 48 || slot.source === 49) {
            let alerts = this._alertSettings[account] || this._defaultAlertRow(account);
            let enabled = slot.source === 48 ? alerts.warnings : alerts.errors;
            let text = label + " " + (enabled ? "an" : "aus");
            return {plain: text, markup: this._escapeMarkup(text)};
        }
        if (slot.source === 50) {
            let loginOk = usage.cache_invalidated !== true &&
                (usage.status === "ok" || usage.status === "partial");
            let text = label + " " + (loginOk ? "ja" : "nein");
            return {plain: text, markup: this._escapeMarkup(text)};
        }
        if (slot.source === 51) {
            let text = label + " " + this._statusLabel(usage.status);
            return {plain: text, markup: this._escapeMarkup(text)};
        }
        let percentSource = PANEL_LIMIT_SOURCE_MAP[slot.source] || slot.source;
        let percentWindow = slot.source >= 37 && slot.source <= 42
            ? this._panelWindowForSource(usage, percentSource)
            : slot.window;
        if (slot.source === 18 || slot.source === 19) {
            percentWindow = this._panelWindowForSource(usage, slot.source);
        }
        let percentValue = slot.source >= 1 && slot.source <= 8
            ? slot.value : this._remainingPercent(percentWindow);
        let percent = this._percentPartsFromValue(percentValue, account, "panel", true);
        let reset = this._windowResetParts(percentWindow, account, "panel", false);
        if (slot.source >= 20 && slot.source <= 25) {
            let resetWindow = this._panelWindowForKey(usage, this._panelResetKey(slot.source));
            return this._panelResetValue(resetWindow, label, account, "duration");
        }
        if (slot.source >= 26 && slot.source <= 31) {
            let resetWindow = this._panelWindowForKey(usage, this._panelResetKey(slot.source));
            return this._panelResetValue(resetWindow, label, account, "date-time");
        }
        return {
            plain: label + (percent.plain ? " " + percent.plain : "") +
                (reset.plain ? " " + reset.plain : ""),
            markup: this._escapeMarkup(label + (percent.markup ? " " : "")) + percent.markup +
                (reset.markup ? " " + reset.markup : "")
        };
    },

    _panelAccountTag: function(usage) {
        let account = usage && usage.account;
        let backend = this._backendAccounts && this._backendAccounts[account];
        let tag = backend && this._safeText(backend.tag, 8);
        if (!tag && Array.isArray(this.accountBackends)) {
            for (let i = 0; i < this.accountBackends.length; i++) {
                if (this.accountBackends[i] && this.accountBackends[i].account === account) {
                    tag = this._safeText(this.accountBackends[i].tag, 8);
                    break;
                }
            }
        }
        if (!tag) {
            let display = this._displaySettings && this._displaySettings[account];
            tag = display && this._safeText(display.tag, 8);
        }
        return tag || this._accountTag(this._safeText(usage && usage.label, 120) || account);
    },

    _panelWindowForKey: function(usage, key) {
        if (!usage || usage.cache_invalidated === true) {
            return null;
        }
        let spark = this._modelPool(usage, "gpt-5.3-codex-spark");
        let mainPool = usage && usage.main && usage.main.available === true &&
            usage.main.allowed !== false
            ? usage.main : null;
        let sparkPool = spark && spark.available === true && spark.allowed !== false
            ? spark : null;
        return {
            "main-5h": usage.five_hour,
            "main-weekly": usage.weekly,
            "main-monthly": this._poolWindowForDuration(mainPool, 2592000),
            "main-other": this._poolOtherWindow(mainPool, true),
            "spark-5h": this._poolWindowForDuration(sparkPool, 18000),
            "spark-weekly": this._poolWindowForDuration(sparkPool, 604800),
            "spark-other": this._poolOtherWindow(sparkPool)
        }[key] || null;
    },

    _panelResetKey: function(source) {
        return {
            20: "main-5h", 21: "main-weekly", 22: "main-monthly", 23: "spark-5h",
            24: "spark-weekly", 25: "spark-other", 26: "main-5h", 27: "main-weekly",
            28: "main-monthly", 29: "spark-5h", 30: "spark-weekly", 31: "spark-other"
        }[source] || "main-5h";
    },

    _panelResetValue: function(window, label, account, part) {
        let reset = this._windowResetParts(window, account, "panel", true, part);
        let text = label + (reset.plain ? " " + reset.plain : " –");
        let markup = this._escapeMarkup(label) + (reset.markup ? " " + reset.markup : " –");
        return {plain: text, markup: markup};
    },

    _panelDeltaPart: function(usage, source, surface) {
        if (!usage || usage.cache_invalidated === true) {
            let label = this._panelSourceLabel(source);
            return {plain: label + " –", markup: this._escapeMarkup(label + " –")};
        }
        let key = {13: undefined, 32: 18000, 33: 604800, 34: 2592000, 35: 18000, 36: null}[source];
        let pool = source === 35 ? "gpt-5.3-codex-spark" : "main";
        if (source === 13) {
            let row = this._consumptionSettings && this._consumptionSettings[usage.account] || {};
            let configured = row["limit-window"] || "short";
            key = {short: 18000, weekly: 604800, monthly: 2592000}[configured] || 18000;
            pool = configured === "spark" ? "gpt-5.3-codex-spark" : "main";
        }
        let windows = Array.isArray(usage.cost_windows) ? usage.cost_windows : [];
        let candidate = null;
        for (let i = windows.length - 1; i >= 0; i--) {
            let seconds = windows[i] && Number(windows[i].limit_window_seconds);
            let matchesOther = key === null && [18000, 604800, 2592000].indexOf(seconds) === -1;
            if (windows[i] && windows[i].pool === pool &&
                (key === null ? matchesOther : seconds === key)) {
                candidate = windows[i];
                break;
            }
        }
        if (!candidate && source === 13) {
            for (let i = windows.length - 1; i >= 0; i--) {
                if (windows[i] && windows[i].pool === pool) {
                    candidate = windows[i];
                    break;
                }
            }
        }
        let value = candidate && candidate.coverage !== "insufficient"
            ? Number(candidate.consumed_percentage_points) : null;
        let valueText = Number.isFinite(value) && value >= 0
            ? this._formatConsumptionValue(value) + "%" : "–";
        let label = this._panelSourceLabel(source);
        let text = label + " " + valueText;
        let style = this._deltaStyles && this._deltaStyles[usage.account];
        let dynamic = candidate ? this._panelDeltaIsDynamic(usage, candidate) : false;
        let markup = style && Number.isFinite(value)
            ? this._styleSpan(text, style, value, surface, dynamic)
            : this._escapeMarkup(text);
        return {plain: text, markup: markup};
    },

    _panelDeltaIsDynamic: function(usage, candidate) {
        if (!usage || typeof usage !== "object" || !candidate || typeof candidate !== "object") {
            return false;
        }
        let lookback = Number(candidate.lookback_seconds);
        let delta = Number(candidate.consumed_percentage_points);
        if (!Number.isFinite(lookback) || lookback <= 0 || !Number.isFinite(delta) || delta < 0) {
            return false;
        }
        if (candidate.coverage !== "complete" && candidate.coverage !== "partial") {
            return false;
        }
        let seconds = Number(candidate.limit_window_seconds);
        let pool = candidate.pool === "gpt-5.3-codex-spark"
            ? "spark"
            : (candidate.pool === "main" ? "main" : "");
        if (!pool) {
            return false;
        }
        let window;
        if (pool === "spark") {
            let sparkPool = this._modelPool(usage, "gpt-5.3-codex-spark");
            if (!this._poolIsUsable(sparkPool)) {
                return false;
            }
            window = this._poolWindowForDuration(sparkPool, seconds);
        } else if (seconds === 18000) {
            window = usage.five_hour;
        } else if (seconds === 604800) {
            window = usage.weekly;
        } else {
            if (!this._poolIsUsable(usage.main)) {
                return false;
            }
            window = this._poolWindowForDuration(usage.main, seconds);
        }
        if (!window) {
            return false;
        }
        let now = Date.now();
        let reset = window && this._dateMillis(window.reset_at);
        let duration = Number(window && (window.duration_seconds || window.duration) || seconds);
        if (!Number.isFinite(duration) || duration <= 0) {
            return false;
        }
        let horizon = duration;
        if (reset !== null) {
            if (reset <= now) {
                return false;
            }
            horizon = Math.min((reset - now) / 1000, duration);
        }
        if (!Number.isFinite(horizon) || horizon <= 0) {
            return false;
        }
        let projected = delta * horizon / lookback;
        if (!Number.isFinite(projected)) {
            return false;
        }
        let remaining = this._remainingPercent(window);
        return remaining !== null && projected >= remaining;
    },

    _panelForecastPart: function(usage, surface) {
        if (!usage || usage.cache_invalidated === true) {
            return null;
        }
        let row = this._consumptionSettings && this._consumptionSettings[usage.account] || {
            account: usage.account, "forecast-limit-window": "short", format: "compact",
            "forecast-format": "compact", "show-coverage-marker": true,
            "forecast-show-panel": true, "forecast-show-tooltip": true,
            "forecast-hide-when-zero": false, "forecast-baseline-enabled": false
        };
        let key = row["forecast-limit-window"] || row["limit-window"] || "short";
        let pool = key === "spark" ? "gpt-5.3-codex-spark" : "main";
        let seconds = {short: 18000, weekly: 604800, monthly: 2592000}[key] || 18000;
        let query = this._consumptionQueryKey(
            pool, row.amount || 1, row.unit || "hours",
            row["forecast-smoothing"] || row.smoothing || "none",
            row["forecast-baseline-enabled"] === true ? row["forecast-baseline-minutes"] : null
        );
        let windows = this._selectConsumptionWindows(usage.cost_windows, key, pool, query);
        let window = windows[0] || null;
        let forecastFormat = row["forecast-format"] === undefined
            ? (row.format || "compact") : row["forecast-format"];
        let forecastCustomFormat = row["forecast-custom-format"] === undefined
            ? (row["custom-format"] || "") : row["forecast-custom-format"];
        let forecastShowCoverage = row["forecast-show-coverage-marker"] !== false;
        let forecastRow = Object.assign({}, row, {
            account: usage.account, "forecast-show-panel": true,
            "forecast-show-tooltip": true, "show-panel": true,
            "show-tooltip": true, "forecast-limit-window": key,
            format: forecastFormat,
            "custom-format": forecastCustomFormat,
            "forecast-format": forecastFormat,
            "forecast-custom-format": forecastCustomFormat,
            "show-coverage-marker": forecastShowCoverage,
            "baseline-enabled": row["forecast-baseline-enabled"] === true,
            "baseline-minutes": row["forecast-baseline-minutes"] === undefined
                ? 60 : row["forecast-baseline-minutes"],
            "forecast-show-coverage-marker": forecastShowCoverage
        });
        return this._forecastWindowPart(window, forecastRow, surface, 100, true);
    },

    _panelCreditLimitPart: function(source, label) {
        let key = {45: "hourly", 46: "weekly", 47: "monthly"}[source];
        let policy = this._routingPolicy && this._routingPolicy.credit_limits;
        let configured = policy && policy[key] !== undefined
            ? policy[key]
            : ({hourly: this.routingCreditHourlyLimit, weekly: this.routingCreditWeeklyLimit,
                monthly: this.routingCreditMonthlyLimit}[key]);
        let text = label + " " + (Number(configured) > 0 ? String(configured) : "aus");
        return {plain: text, markup: this._escapeMarkup(text)};
    },

    _consumptionParts: function(usage, surface) {
        if (!usage || usage.cache_invalidated === true) {
            return null;
        }
        let row = this._consumptionSettings && this._consumptionSettings[usage.account];
        if (!row) {
            return null;
        }
        let emergencyOverride = this._readEmergencyDisplayOverride(usage.account);
        if (emergencyOverride) {
            row = Object.assign({}, row, {
                "show-panel": true,
                "show-tooltip": true,
                "limit-window": emergencyOverride.limit_window,
                "forecast-limit-window": emergencyOverride.limit_window,
                "forecast-show-panel": true,
                "forecast-show-tooltip": true,
                "emergency-delta": true
            });
        }
        let rawWindows = Array.isArray(usage.cost_windows) ? usage.cost_windows : [];
        let forecastLimitWindow = row["forecast-limit-window"] || row["limit-window"];
        let requestedPool = row["limit-window"] === "spark" ? "gpt-5.3-codex-spark" : "main";
        let consumptionQueryKey = this._consumptionQueryKey(
            requestedPool, row.amount, row.unit, row.smoothing,
            row["baseline-enabled"] ? row["baseline-minutes"] : null
        );
        let windows = this._selectConsumptionWindows(
            rawWindows, row["limit-window"], requestedPool, consumptionQueryKey
        );
        let forecastPool = forecastLimitWindow === "spark" ? "gpt-5.3-codex-spark" : "main";
        let forecastQueryKey = this._consumptionQueryKey(
            forecastPool, row.amount, row.unit, row["forecast-smoothing"] || row.smoothing,
            row["forecast-baseline-enabled"] === true ? row["forecast-baseline-minutes"] : null
        );
        let forecastWindow = this._selectConsumptionWindows(
            rawWindows,
            forecastLimitWindow,
            forecastPool,
            forecastQueryKey
        )[0] || null;
        let parts = [];
        for (let i = 0; i < windows.length; i++) {
            let part = this._consumptionWindowPart(windows[i], row, surface, forecastWindow);
            if (part) parts.push(part);
        }
        let baseline = this._baselineParts(usage.account, row, surface, windows[0] || null);
        if (baseline) parts.push(baseline);
        if (!parts.length) return null;
        return {
            plain: parts.map(function(part) { return part.plain; }).join(" · "),
            markup: parts.map(function(part) { return part.markup; }).join(" · ")
        };
    },

    _baselineParts: function(account, row, surface, window) {
        // The table-local "Setze eigenen AW" switch controls this output.
        // A separate formatting target used to hide an enabled AW in the
        // panel because supplemental targets default there to disabled.
        if (!row || row["baseline-enabled"] !== true) {
            return null;
        }
        let baselineValue = window && typeof window.baseline_used_percent === "number" &&
            Number.isFinite(window.baseline_used_percent)
            ? this._formatConsumptionValue(Number(window.baseline_used_percent)) + "%"
            : "—";
        let text = "AW" + String(row["baseline-minutes"]) + "m=" + baselineValue;
        return { plain: text, markup: this._escapeMarkup(text) };
    },

    _limitWindowSeconds: function(key) {
        return {
            short: 18000,
            weekly: 604800,
            monthly: 2592000
        }[key] || null;
    },

    _coverageMarker: function(coverage, enabled) {
        if (enabled !== true) {
            return "";
        }
        return {
            complete: " (vollständig)",
            partial: " (mindestens)",
            stale: " (veraltet)",
            insufficient: " (nicht genügend Messdaten)"
        }[coverage] || " (unbekannt)";
    },

    _consumptionQueryKey: function(pool, amount, unit, smoothing, baselineMinutes) {
        let baseline = baselineMinutes === null || baselineMinutes === undefined
            ? "-" : String(baselineMinutes);
        return [String(pool), String(amount), String(unit), String(smoothing || "none"), baseline].join("|");
    },

    _selectConsumptionWindows: function(windows, key, pool, queryKey) {
        if (!Array.isArray(windows)) return [];
        let candidates = windows.filter(function(window) {
            return window && window.pool === pool;
        });
        if (typeof queryKey === "string" && queryKey) {
            let matching = candidates.filter(function(window) {
                return window._consumption_query_key === queryKey;
            });
            if (matching.length) {
                candidates = matching;
            } else {
                // Legacy cached windows have no query identity. A tagged
                // result for a different query may never stand in for one.
                candidates = candidates.filter(function(window) {
                    return window._consumption_query_key === undefined;
                });
            }
        }
        if (key === "all") return candidates;
        if (key === "spark") return candidates;
        let seconds = this._limitWindowSeconds(key);
        return seconds === null ? [] : candidates.filter(function(window) {
            return Number(window.limit_window_seconds) === seconds;
        });
    },

    _usageResetParts: function(usage, surface, forceVisible) {
        if (!usage || usage.cache_invalidated === true) {
            return null;
        }
        let row = this._resetSettings && this._resetSettings[usage.account];
        if (!row) {
            row = this._defaultResetRow(usage.account);
        }
        let legacyVisible = surface === "panel"
            ? row["show-panel"]
            : (surface === "hover" ? row["show-tooltip"] : true);
        if (!forceVisible && !this._elementTargetEnabled(usage.account, "usage-resets", surface, legacyVisible)) {
            return null;
        }
        let state = this._safeUsageResets(usage.usage_resets);
        if (!state.known) {
            if (!row["show-unknown"]) {
                return null;
            }
            return { plain: "Resets: —", markup: "Resets: —" };
        }
        if (state.available === 0 && row["hide-when-zero"]) {
            return null;
        }
        let available = state.available;
        let text;
        if (row.format === "verbose") {
            text = available + (available === 1
                ? " Usage-Reset verfügbar"
                : " Usage-Resets verfügbar");
        } else if (row.format === "readable") {
            text = available + (available === 1 ? " Reset" : " Resets");
        } else {
            text = "↻" + available;
        }
        return { plain: text, markup: this._escapeMarkup(text) };
    },

    _consumptionWindowPart: function(window, row, surface, forecastWindow) {
        if (!window || typeof window !== "object") {
            return null;
        }
        let value = Number(window.consumed_percentage_points);
        if (!Number.isFinite(value) || value < 0) {
            return null;
        }
        let coverage = window.coverage;
        if (row["hide-when-zero"] && value === 0 && coverage !== "insufficient") {
            return null;
        }
        let valueText = this._formatConsumptionValue(value);
        let period = this._consumptionPeriod(row.amount, row.unit);
        let marker = this._coverageMarker(coverage, row["show-coverage-marker"] === true);
        let windowLabel = Number(window.limit_window_seconds) === 604800
            ? "Woche"
            : (Number(window.limit_window_seconds) === 2592000
                ? "30d"
                : (Number(window.limit_window_seconds) === 18000 ? "5h" : "sonstiges"));
        let compactUnit = {
            minutes: "m",
            hours: "S",
            days: "T",
            weeks: "W"
        }[row.unit] || "S";
        let compactPrefix = (row["emergency-delta"] === true || this.showConsumptionDelta !== false) ? "Δ" : "";
        let plain;
        if (coverage === "insufficient") {
            plain = "Limitverbrauch " + period + ": nicht genügend Messdaten" + marker;
        } else if (row.format === "verbose") {
            plain = "Limitverbrauch " + period + " (" + windowLabel + "): " +
                valueText + "%" + marker;
        } else if (row.format === "custom") {
            plain = this._customConsumptionText(row["custom-format"], {
                value: valueText,
                period: period,
                window: windowLabel,
                coverage: marker ? marker.slice(2, -1) : "vollständig"
            });
            if (marker && String(row["custom-format"] || "").indexOf("{coverage}") === -1) {
                plain += marker;
            }
        } else if (row.format === "compact-token") {
            plain = compactPrefix + String(row.amount) + compactUnit + valueText + "P" + marker;
        } else {
            plain = compactPrefix + period + " " + valueText + "%" + marker;
        }
        let account = row.account;
        let styleRow = this._percentStyles[account] || this._defaultStyleRow(account, "percent");
        let consumptionElement = Number(window.limit_window_seconds) === 604800
            ? "consumption-weekly"
            : (Number(window.limit_window_seconds) === 2592000
                ? "consumption-monthly"
                : (Number(window.limit_window_seconds) === 18000
                    ? "consumption-short" : "consumption"));
        let visible = this._elementTargetEnabled(
            account,
            consumptionElement,
            surface,
            surface === "panel" ? row["show-panel"] :
                (surface === "hover" ? row["show-tooltip"] : true)
        );
        let consumptionMarkup = visible
            ? this._styleSpan(plain, styleRow, Math.max(0, 100 - value), surface)
            : this._escapeMarkup(plain);
        let parts = [{ plain: plain, markup: consumptionMarkup }];
        let forecastRow = {
            account: account,
            "show-panel": row["forecast-show-panel"] === undefined
                ? row["show-panel"] : row["forecast-show-panel"],
            "show-tooltip": row["forecast-show-tooltip"] === undefined
                ? row["show-tooltip"] : row["forecast-show-tooltip"],
            "limit-window": row["forecast-limit-window"] || row["limit-window"],
            format: row["forecast-format"] || "compact",
            "custom-format": row["forecast-custom-format"] || "",
            smoothing: row["forecast-smoothing"] || row.smoothing,
            "hide-when-zero": row["forecast-hide-when-zero"] === true,
            "forecast-hide-when-zero": row["forecast-hide-when-zero"] === true,
            "show-coverage-marker": row["forecast-show-coverage-marker"] !== false,
            "baseline-enabled": row["forecast-baseline-enabled"] === true,
            "baseline-minutes": row["forecast-baseline-minutes"],
            "forecast-warn-amount": row["forecast-warn-amount"],
            "forecast-warn-unit": row["forecast-warn-unit"],
            "forecast-warn-format": row["forecast-warn-format"]
        };
        let forecastSharesConsumptionQuery =
            forecastRow["limit-window"] === row["limit-window"] &&
            forecastRow.smoothing === row.smoothing &&
            forecastRow["baseline-enabled"] === row["baseline-enabled"] &&
            (!forecastRow["baseline-enabled"] ||
                forecastRow["baseline-minutes"] === row["baseline-minutes"]);
        let consumptionWindowIsLegacy = window._consumption_query_key === undefined;
        let forecast = this._forecastWindowPart(
            forecastWindow ||
                (consumptionWindowIsLegacy || forecastSharesConsumptionQuery ? window : null),
            forecastRow,
            surface,
            Math.max(0, 100 - value)
        );
        if (forecast) {
            parts.push(forecast);
        }
        return {
            plain: parts.map(function(part) { return part.plain; }).join(" "),
            markup: parts.map(function(part) { return part.markup; }).join(" ")
        };
    },

    _creditParts: function(usage, surface, forceVisible, panelPrefix) {
        if (!usage) return null;
        let credit = usage.credits;
        let row = this._creditSettings && this._creditSettings[usage.account];
        if (usage.cache_invalidated === true || !credit || !row) return null;
        let creditText = function(value) {
            if (value === null || value === undefined || !Number.isFinite(Number(value))) return "–";
            return String(Math.round(Number(value)));
        };
        let remaining = creditText(credit.remaining);
        let limit = creditText(credit.limit);
        let usedValue = credit.used !== null && credit.used !== undefined
            ? credit.used
            : (credit.limit !== null && credit.limit !== undefined &&
                credit.remaining !== null && credit.remaining !== undefined
                ? Math.max(0, credit.limit - credit.remaining) : null);
        let used = creditText(usedValue);
        let percent = credit.percent !== null && credit.percent !== undefined
            ? String(Math.round(credit.percent * 10) / 10) : "–";
        if (row["hide-when-zero"] && credit.remaining === 0) return null;
        if (!forceVisible && !this._elementTargetEnabled(usage.account, "credits", surface,
            surface === "panel" ? row["show-panel"] : surface === "hover" ? row["show-tooltip"] : true)) {
            return null;
        }
        let reset = credit.reset_at ? this._formatDate(credit.reset_at) : "–";
        let label = panelPrefix || (surface === "panel" ? "CR" : "Credits");
        let coverageMarker = this._coverageMarker(
            credit.coverage || "complete",
            row["show-coverage-marker"] === true
        );
        let baselineText = row["baseline-enabled"] === true &&
            typeof credit.baseline_used_percent === "number" &&
            Number.isFinite(credit.baseline_used_percent)
            ? "AW" + String(row["baseline-minutes"]) + "m=" +
                this._formatConsumptionValue(Number(credit.baseline_used_percent)) + "%"
            : "";
        let showConsumption = forceVisible || (surface === "panel"
            ? row["consumption-show-panel"] === true
            : surface === "hover"
                ? row["consumption-show-tooltip"] === true
                : true);
        let usageSuffix = showConsumption ? " · Verbrauch " + used : "";
        let text = row.format === "custom"
            ? this._customCreditText(row["custom-format"], { remaining, used, limit, percent, reset })
            : (row.format === "verbose"
                ? label + ": " + remaining + " / " + limit +
                    (showConsumption
                        ? " (Verbrauch " + used + ", " + percent + "%)"
                        : " (" + percent + "%)")
                : label + " " + remaining + usageSuffix);
        text += coverageMarker;
        if (baselineText) {
            text += " " + baselineText;
        }
        return { plain: text, markup: this._escapeMarkup(text) };
    },

    _creditConsumptionParts: function(usage, surface, forceVisible, panelPrefix) {
        if (!usage || usage.cache_invalidated === true) {
            return null;
        }
        let row = this._creditSettings && this._creditSettings[usage.account];
        if (!row) {
            return null;
        }
        let configuredVisible = surface === "panel"
            ? row["consumption-show-panel"] === true
            : surface === "hover"
                ? row["consumption-show-tooltip"] === true
                : true;
        let visible = forceVisible || (configuredVisible && this._elementTargetEnabled(
            usage.account,
            "credit-consumption",
            surface,
            configuredVisible
        ));
        if (!visible) {
            return null;
        }
        let windows = this._selectConsumptionWindows(
            Array.isArray(usage.cost_windows) ? usage.cost_windows : [],
            "all",
            "credits",
            this._consumptionQueryKey(
                "credits", row["consumption-amount"], row["consumption-unit"],
                row["consumption-smoothing"], row["consumption-baseline-enabled"] === true
                    ? row["consumption-baseline-minutes"] : null
            )
        );
        if (!windows.length) {
            return null;
        }
        let parts = [];
        for (let i = 0; i < windows.length; i++) {
            let window = windows[i];
            let value = Number(window.consumed_percentage_points);
            if (!Number.isFinite(value) || value < 0) {
                continue;
            }
            if (row["consumption-hide-when-zero"] && value === 0 &&
                window.coverage !== "insufficient") {
                continue;
            }
            let valueText = this._formatConsumptionValue(value);
            let period = this._consumptionPeriod(row["consumption-amount"], row["consumption-unit"]);
            let marker = this._coverageMarker(
                window.coverage,
                row["consumption-show-coverage-marker"] === true
            );
            let baselineText = row["consumption-baseline-enabled"] === true &&
                typeof window.baseline_used_percent === "number" &&
                Number.isFinite(window.baseline_used_percent)
                ? "AW" + String(row["consumption-baseline-minutes"]) + "m=" +
                    this._formatConsumptionValue(Number(window.baseline_used_percent)) + "%"
                : "";
            let text;
            if (window.coverage === "insufficient") {
                text = (panelPrefix || "Creditverbrauch") + " " + period + ": nicht genügend Messdaten";
            } else if (row["consumption-format"] === "verbose") {
                text = (panelPrefix || "Creditverbrauch") + " " + period + ": " + valueText + "%" + marker;
            } else if (row["consumption-format"] === "custom") {
                text = this._customCreditConsumptionText(row["consumption-custom-format"], {
                    value: valueText,
                    period: period,
                    coverage: marker ? marker.slice(2, -1) : "vollständig"
                });
                if (marker && String(row["consumption-custom-format"] || "").indexOf("{coverage}") === -1) {
                    text += marker;
                }
            } else {
                let prefix = this.showConsumptionDelta !== false ? "Δ" : "";
                text = (panelPrefix || "") + (panelPrefix ? " " : "") + prefix +
                    period + " " + valueText + " Credit-%" + marker;
            }
            if (baselineText) {
                text += " " + baselineText;
            }
            parts.push({ plain: text, markup: this._escapeMarkup(text) });
        }
        if (!parts.length) {
            return null;
        }
        return {
            plain: parts.map(function(part) { return part.plain; }).join(" · "),
            markup: parts.map(function(part) { return part.markup; }).join(" · ")
        };
    },

    _customCreditConsumptionText: function(template, values) {
        let text = typeof template === "string" && template ? template :
            "Δ{period} {value} Credit-%";
        return text.replace(/\{(value|period|coverage)\}/g, function(_match, key) {
            return values[key];
        });
    },

    _customCreditText: function(template, values) {
        let text = typeof template === "string" && template ? template : "Credits {remaining}";
        return text.replace(/\{(remaining|used|limit|percent|reset)\}/g, function(_, key) {
            return values[key];
        });
    },

    _forecastWindowPart: function(window, row, surface, remaining, forceVisible) {
        if (!window || typeof window !== "object") {
            return null;
        }
        let estimate = window.coverage === "stale" || window.coverage === "insufficient"
            ? null
            : window.estimated_seconds_to_exhaustion;
        if (row["forecast-hide-when-zero"] && estimate === 0) {
            return null;
        }
        let forecastVisible = surface === "panel"
            ? (row["forecast-show-panel"] === undefined ? row["show-panel"] : row["forecast-show-panel"])
            : (surface === "hover"
                ? (row["forecast-show-tooltip"] === undefined ? row["show-tooltip"] : row["forecast-show-tooltip"])
                : true);
        let visible = forceVisible === true || (forecastVisible && this._elementTargetEnabled(
            row.account,
            "forecast",
            surface,
            true
        ));
        if (!visible) {
            return null;
        }
        let durationStyle = this._durationStyles[row.account] ||
            this._defaultStyleRow(row.account, "duration");
        let forecastFormat = row.format || row["forecast-format"] || "compact";
        let decimalCompact = forecastFormat === "compact";
        let duration = estimate === null || estimate === undefined
            ? "—"
            : this._formatDurationPart(Math.ceil(estimate / 60), durationStyle.format, decimalCompact);
        let marker = this._coverageMarker(
            window.coverage,
            row["show-coverage-marker"] === true
        );
        let forecastText = estimate === null || estimate === undefined
            ? "—"
            : "≈ " + duration + marker;
        let baselineText = row["baseline-enabled"] === true &&
            typeof window.baseline_used_percent === "number" &&
            Number.isFinite(window.baseline_used_percent)
            ? "AW" + String(row["baseline-minutes"]) + "m=" +
                this._formatConsumptionValue(Number(window.baseline_used_percent)) + "%"
            : "";
        let plainWithoutBaseline = forecastFormat === "verbose"
            ? "Zeit bis Tokenende: " + forecastText
            : (forecastFormat === "custom"
                ? this._customForecastText(row["custom-format"] || row["forecast-custom-format"], {
                    value: forecastText,
                    duration: duration,
                    coverage: marker ? marker.slice(2, -1) : "vollständig"
                }) + (marker && String(row["custom-format"] || row["forecast-custom-format"] || "").indexOf("{coverage}") === -1 ? marker : "")
                : "TE=" + (estimate === null || estimate === undefined ? "—" : duration + marker));
        let plain = plainWithoutBaseline;
        if (baselineText) {
            plain += " " + baselineText;
        }
        let markup;
        if (forecastFormat === "compact" || forecastFormat === "compact-minutes") {
            markup = estimate === null || estimate === undefined
                ? this._escapeMarkup(plainWithoutBaseline)
                : this._escapeMarkup("TE=") + this._styleSpan(duration + marker, durationStyle, remaining, surface);
        } else if (estimate === null || estimate === undefined) {
            markup = this._escapeMarkup(plainWithoutBaseline);
        } else if (forecastFormat === "custom") {
            markup = this._escapeMarkup(plainWithoutBaseline);
        } else {
            markup = this._escapeMarkup("Zeit bis Tokenende: ") +
                this._styleSpan(forecastText, durationStyle, remaining, surface);
        }
        if (baselineText) {
            markup += " " + this._escapeMarkup(baselineText);
        }
        let warnSeconds = Number(row["forecast-warn-amount"] || 0) * {
            minutes: 60, hours: 3600, days: 86400, weeks: 604800
        }[row["forecast-warn-unit"] || "hours"];
        if (estimate !== null && estimate !== undefined && warnSeconds > 0 && estimate <= warnSeconds) {
            markup = this._forecastWarningMarkup(markup, row["forecast-warn-format"] || "red-yellow");
        }
        return { plain: plain, markup: markup };
    },

    _forecastWarningMarkup: function(markup, format) {
        let colors = {
            red: {foreground: "#ff5555"},
            "red-yellow": {foreground: "#ff5555", background: "#e5c07b"},
            "blink-red-yellow": {foreground: "#ff5555", background: "#e5c07b", weight: "bold"},
            yellow: {foreground: "#e5c07b"},
            "red-green": {foreground: "#ff5555", background: "#98c379"},
            "red-red": {foreground: "#ff5555", background: "#a83232"}
        }[format];
        if (!colors || format === "none") return markup;
        let attributes = Object.keys(colors).map(function(key) {
            let value = key === "weight" ? "font_weight" : key;
            return value + "=\"" + colors[key] + "\"";
        }).join(" ");
        return "<span " + attributes + ">" + markup + "</span>";
    },

    _customConsumptionText: function(template, values) {
        let text = typeof template === "string" && template ? template :
            "Δ{period} {value}%";
        return text.replace(/\{(value|period|window|coverage)\}/g, function(_match, key) {
            return values[key];
        });
    },

    _customForecastText: function(template, values) {
        let text = typeof template === "string" && template ? template : "Zeit bis Tokenende {value}";
        return text.replace(/\{(value|duration|coverage)\}/g, function(_match, key) {
            return values[key];
        });
    },

    _formatConsumptionValue: function(value) {
        let rounded = Math.round(value * 10) / 10;
        return String(rounded.toFixed(1)).replace(".", ",");
    },

    _consumptionPeriod: function(amount, unit) {
        let labels = { minutes: "Min.", hours: "h", days: "Tage", weeks: "Wochen" };
        return String(amount) + " " + (labels[unit] || unit);
    },

    _panelTag: function(item) {
        return this._accountDisplayText(item, "panel");
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
        return PANEL_SOURCE_LABELS[source] || "?";
    },

    _fiveHourDisplayWindow: function(usage) {
        if (!usage || this.hideFiveHourWhenLongLimitExhausted !== true) {
            return usage && usage.five_hour;
        }
        let weekly = this._remainingPercent(usage.weekly);
        let monthly = usage.main && usage.main.available === true &&
            usage.main.allowed !== false
            ? this._remainingPercent(this._poolWindowForDuration(usage.main, 2592000))
            : null;
        return weekly === 0 || monthly === 0 ? null : usage.five_hour;
    },

    _longLimitExhausted: function(usage) {
        if (!usage || usage.status !== "ok" || usage.stale === true) {
            return false;
        }
        let weekly = this._remainingPercent(usage.weekly);
        let monthlyWindow = usage.main && usage.main.available === true &&
            usage.main.allowed !== false
            ? this._poolWindowForDuration(usage.main, 2592000)
            : null;
        let monthly = this._remainingPercent(monthlyWindow);
        return weekly === 0 || monthly === 0;
    },

    _panelValueForSource: function(usage, source) {
        if (!usage || usage.cache_invalidated === true) {
            return null;
        }
        if (source === 9) {
            return usage && usage.cache_invalidated !== true
                ? this._remainingPercent(usage.credits)
                : null;
        }
        if (source === 10) {
            return null;
        }
        if (source === 18) {
            if (!this._poolIsUsable(usage && usage.main)) {
                return null;
            }
            return this._remainingPercent(this._panelWindowForKey(usage, "main-other"));
        }
        if (source === 19) {
            let sparkPool = this._modelPool(usage, "gpt-5.3-codex-spark");
            if (!this._poolIsUsable(sparkPool)) {
                return null;
            }
            return this._remainingPercent(this._panelWindowForKey(usage, "spark-other"));
        }
        if (source >= 37 && source <= 42) {
            let mappedSource = PANEL_LIMIT_SOURCE_MAP[source];
            return this._panelValueForSource(usage, mappedSource);
        }
        if ((source >= 11 && source <= 36) || source >= 43) {
            return null;
        }
        let five = this._remainingPercent(usage.five_hour);
        let week = this._remainingPercent(usage.weekly);
        if ((source >= 1 && source <= 3 || source === 8) &&
            usage.main && !this._poolIsUsable(usage.main)) {
            return null;
        }
        if (source === 1) {
            return this._remainingPercent(this._fiveHourDisplayWindow(usage));
        }
        if (source === 2) {
            return week;
        }
        if (source === 8) {
            return this._remainingPercent(this._poolWindowForDuration(usage.main, 2592000));
        }
        let spark = this._modelPool(usage, "gpt-5.3-codex-spark");
        if (source >= 4 && source <= 7 && !this._poolIsUsable(spark)) {
            return null;
        }
        if (source === 4) {
            return this._remainingPercent(this._poolWindowForDuration(spark, 18000));
        }
        if (source === 5) {
            return this._remainingPercent(this._poolWindowForDuration(spark, 604800));
        }
        if (source === 6) {
            return this._poolAverage(spark);
        }
        if (source === 7) {
            return this._remainingPercent(this._poolOtherWindow(spark));
        }
        if (five === null || week === null) {
            return null;
        }
        return (five + week) / 2;
    },

    _panelWindowForSource: function(usage, source) {
        if (!usage || usage.cache_invalidated === true) {
            return null;
        }
        if (source === 9 || source === 10) {
            return null;
        }
        if (source === 18) {
            if (!this._poolIsUsable(usage && usage.main)) {
                return null;
            }
            return this._panelWindowForKey(usage, "main-other");
        }
        if (source === 19) {
            let sparkPool = this._modelPool(usage, "gpt-5.3-codex-spark");
            if (!this._poolIsUsable(sparkPool)) {
                return null;
            }
            return this._panelWindowForKey(usage, "spark-other");
        }
        if (source >= 37 && source <= 42) {
            return this._panelWindowForSource(usage, PANEL_LIMIT_SOURCE_MAP[source]);
        }
        if (source >= 1 && source <= 3 && usage.main && !this._poolIsUsable(usage.main)) {
            return null;
        }
        if (source === 1) {
            return usage.five_hour;
        }
        if (source === 2) {
            return usage.weekly;
        }
        if (source === 8) {
            if (!usage.main || !this._poolIsUsable(usage.main)) {
                return null;
            }
            return this._poolWindowForDuration(usage.main, 2592000);
        }
        let spark = this._modelPool(usage, "gpt-5.3-codex-spark");
        if (source >= 4 && source <= 7 && !this._poolIsUsable(spark)) {
            return null;
        }
        if (source === 4) {
            return this._poolWindowForDuration(spark, 18000);
        }
        if (source === 5) {
            return this._poolWindowForDuration(spark, 604800);
        }
        if (source === 6) {
            let sparkFive = this._poolWindowForDuration(spark, 18000);
            let sparkWeek = this._poolWindowForDuration(spark, 604800);
            let sparkFiveValue = this._remainingPercent(sparkFive);
            let sparkWeekValue = this._remainingPercent(sparkWeek);
            if (sparkFiveValue === null || sparkWeekValue === null) {
                return null;
            }
            return sparkFiveValue <= sparkWeekValue ? sparkFive : sparkWeek;
        }
        if (source === 7) {
            return this._poolOtherWindow(spark);
        }
        let five = this._remainingPercent(usage.five_hour);
        let week = this._remainingPercent(usage.weekly);
        if (five === null || week === null) {
            return null;
        }
        return five <= week ? usage.five_hour : usage.weekly;
    },

    _panelThreshold: function(item, source) {
        if (source >= 37 && source <= 42) {
            return this._panelThreshold(item, PANEL_LIMIT_SOURCE_MAP[source]);
        }
        let alert = this._alertSettings[item.usage.account] || this._defaultAlertRow(item.usage.account);
        let five = Number(alert["five-threshold"]);
        let weekly = Number(alert["weekly-threshold"]);
        if (source >= 4 && source <= 7) {
            let spark = Number(alert["spark-threshold"]);
            return Number.isFinite(spark) ? spark : 100;
        }
        if (source === 1) {
            return Number.isFinite(five) ? five : 100;
        }
        if (source === 2) {
            return Number.isFinite(weekly) ? weekly : 100;
        }
        if (source === 8) {
            let monthly = Number(alert["monthly-threshold"]);
            return Number.isFinite(monthly) ? monthly : 100;
        }
        let values = [];
        let pool = source === 6
            ? this._modelPool(item.usage, "gpt-5.3-codex-spark")
            : null;
        let fiveWindow = pool
            ? this._poolWindowForDuration(pool, 18000)
            : item.usage.five_hour;
        let weeklyWindow = pool
            ? this._poolWindowForDuration(pool, 604800)
            : item.usage.weekly;
        if (this._remainingPercent(fiveWindow) !== null && Number.isFinite(five)) {
            values.push(five);
        }
        if (this._remainingPercent(weeklyWindow) !== null && Number.isFinite(weekly)) {
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

    _accountDisplayText: function(item, surface) {
        let usage = item && item.usage ? item.usage : item;
        let account = this._safeText(usage && usage.account, 64) || "?";
        let label = this._safeText(usage && usage.label, 120) || account;
        let backendTag = this._backendAccounts && this._backendAccounts[account]
            ? this._safeText(this._backendAccounts[account].tag, 8) : "";
        if (!backendTag && Array.isArray(this.accountBackends)) {
            for (let i = 0; i < this.accountBackends.length; i++) {
                if (this.accountBackends[i] && this.accountBackends[i].account === account) {
                    backendTag = this._safeText(this.accountBackends[i].tag, 8);
                    break;
                }
            }
        }
        if (backendTag && this._elementTargetEnabled(account, "tag", surface, false)) {
            return backendTag;
        }
        let display = this._displaySettings && this._displaySettings[account];
        if (!display) {
            display = this._defaultDisplayRow(account);
        }
        let selection = display[surface];
        let identityElement = selection === 0
            ? "account-id"
            : (selection === 2 ? "tag" : "label");
        if (!this._elementTargetEnabled(account, identityElement, surface, true)) {
            return "";
        }
        if (selection === 0) {
            return account;
        }
        if (selection === 2) {
            return backendTag || this._safeText(display.tag, 8) || this._accountTag(label);
        }
        return label;
    },

    _displaySeparatorEnabled: function(account, surface) {
        let row = this._displaySettings && this._displaySettings[account];
        if (!row || (surface !== "hover" && surface !== "click")) {
            return false;
        }
        return row[surface + "-separator"] === true;
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
        let orderedUsages = this._usages.map(function(usage, index) {
            return { usage: usage, index: index };
        });
        orderedUsages.sort(Lang.bind(this, function(left, right) {
            let leftSettings = this._panelSettings && this._panelSettings[left.usage.account];
            let rightSettings = this._panelSettings && this._panelSettings[right.usage.account];
            let leftOrder = leftSettings && Number.isInteger(leftSettings.order)
                ? leftSettings.order : 1000000 + left.index;
            let rightOrder = rightSettings && Number.isInteger(rightSettings.order)
                ? rightSettings.order : 1000000 + right.index;
            return leftOrder - rightOrder || left.index - right.index;
        }));
        for (let i = 0; i < orderedUsages.length; i++) {
            let usage = orderedUsages[i].usage;
            if (this._displaySeparatorEnabled(usage.account, "hover")) {
                plainLines.push(MENU_SPACER);
                markupLines.push(MENU_SPACER);
            }
            let five = this._percentParts(
                this._fiveHourDisplayWindow(usage), usage.account, "hover"
            );
            let week = this._percentParts(usage.weekly, usage.account, "hover");
            let stale = usage.stale ? " (gespeichert)" : "";
            let display = this._accountDisplayText({ usage: usage }, "hover");
            let summaryParts = display ? [display + ":"] : [];
            let summaryMarkupParts = display ? [this._escapeMarkup(display + ":")] : [];
            if (five.plain) {
                summaryParts.push("5h " + five.plain);
                summaryMarkupParts.push(this._escapeMarkup("5h ") + five.markup);
            }
            if (week.plain) {
                summaryParts.push("Woche " + week.plain);
                summaryMarkupParts.push(this._escapeMarkup("Woche ") + week.markup);
            }
            if (stale) {
                summaryParts.push(stale);
                summaryMarkupParts.push(this._escapeMarkup(stale));
            }
            plainLines.push(summaryParts.join(", "));
            markupLines.push(summaryMarkupParts.join(this._escapeMarkup(", ")));
            let fiveReset = this._windowResetParts(
                usage.five_hour,
                usage.account,
                "hover",
                false,
                "date-time"
            );
            let weekReset = this._windowResetParts(
                usage.weekly,
                usage.account,
                "hover",
                false,
                "date-time"
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
            let fiveDuration = this._windowResetParts(
                usage.five_hour,
                usage.account,
                "hover",
                false,
                "duration"
            );
            let weekDuration = this._windowResetParts(
                usage.weekly,
                usage.account,
                "hover",
                false,
                "duration"
            );
            if (fiveDuration.plain || weekDuration.plain) {
                let durationPlain = "  Restzeit 5h " + (fiveDuration.plain || "–") +
                    ", Woche " + (weekDuration.plain || "–");
                let durationMarkup = this._escapeMarkup("  Restzeit 5h ") +
                    (fiveDuration.markup || "–") + this._escapeMarkup(", Woche ") +
                    (weekDuration.markup || "–");
                plainLines.push(durationPlain);
                markupLines.push(durationMarkup);
            }
            let consumption = this._consumptionParts(usage, "hover");
            if (consumption) {
                plainLines.push("  " + consumption.plain);
                markupLines.push(this._escapeMarkup("  ") + consumption.markup);
            }
            let credits = this._creditParts(usage, "hover");
            if (credits) {
                plainLines.push("  " + credits.plain);
                markupLines.push(this._escapeMarkup("  ") + credits.markup);
            }
            let creditConsumption = this._creditConsumptionParts(usage, "hover");
            if (creditConsumption) {
                plainLines.push("  " + creditConsumption.plain);
                markupLines.push(this._escapeMarkup("  ") + creditConsumption.markup);
            }
            let resets = this._usageResetParts(usage, "hover");
            if (resets) {
                plainLines.push("  " + resets.plain);
                markupLines.push(this._escapeMarkup("  ") + resets.markup);
            }
            let main = this._poolWindowForDuration(usage.main, 2592000)
                ? this._poolDetailParts(
                usage.main,
                usage.account,
                "hover",
                "  ",
                [18000, 604800],
                "Monat"
            ) : null;
            let spark = this._poolDetailParts(
                this._modelPool(usage, "gpt-5.3-codex-spark"),
                usage.account,
                "hover",
                "  Spark",
                []
            );
            let routing = this._routingDecisionParts(usage);
            [main, spark, routing].forEach(function(parts) {
                if (parts) {
                    plainLines.push(parts.plain);
                    markupLines.push(parts.markup);
                }
            });
        }
        return {
            plain: plainLines.join("\n"),
            markup: markupLines.join("\n")
        };
    },

    _errorNotificationNow: function() {
        return Date.now();
    },

    _errorNotificationFingerprint: function(key) {
        let text = String(key || "");
        let primary = 2166136261;
        let secondary = 3735928559;
        for (let i = 0; i < text.length; i++) {
            let code = text.charCodeAt(i);
            primary = Math.imul(primary ^ code, 16777619) >>> 0;
            secondary = Math.imul(secondary ^ (code + i), 2246822519) >>> 0;
        }
        return primary.toString(16).padStart(8, "0") + "-" +
            secondary.toString(16).padStart(8, "0");
    },

    _persistErrorNotificationState: function(serialized) {
        this._errorNotificationStateWritePending = serialized;
        try {
            if (this.settings && typeof this.settings.setValue === "function") {
                this.settings.setValue("error-notification-state", serialized);
                this._errorNotificationStateWritePending = null;
            }
        } catch (e) {
            global.log("[" + UUID + "] error notification state write failed: " +
                this._shortText(e, 180));
        }
    },

    _retryErrorNotificationStateWrite: function() {
        let pending = this._errorNotificationStateWritePending;
        if (typeof pending === "string") {
            this._persistErrorNotificationState(pending);
        }
    },

    _shouldNotifyError: function(key) {
        let now = this._errorNotificationNow();
        if (!Number.isFinite(now)) {
            now = Date.now();
        }
        let state = {};
        try {
            let raw = this.errorNotificationState;
            let parsed = typeof raw === "string" &&
                raw.length <= MAX_ERROR_NOTIFICATION_STATE_CHARS
                ? JSON.parse(raw || "{}")
                : {};
            if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
                state = parsed;
            }
        } catch (e) {
            state = {};
        }

        let fingerprint = this._errorNotificationFingerprint(key);
        let stateChanged = false;
        let keys = Object.keys(state);
        for (let i = keys.length - 1; i >= 0; i--) {
            let value = state[keys[i]];
            if (!Number.isFinite(value) || value > now ||
                now - value >= ERROR_NOTIFICATION_SUPPRESSION_MS) {
                delete state[keys[i]];
                stateChanged = true;
            }
        }
        let suppressed = Number.isFinite(state[fingerprint]) &&
            now - state[fingerprint] < ERROR_NOTIFICATION_SUPPRESSION_MS;
        if (!suppressed) {
            state[fingerprint] = now;
            stateChanged = true;
        }
        keys = Object.keys(state);
        if (keys.length > MAX_ERROR_NOTIFICATION_STATES) {
            keys.sort(function(left, right) {
                return state[right] - state[left];
            });
            for (let j = MAX_ERROR_NOTIFICATION_STATES; j < keys.length; j++) {
                delete state[keys[j]];
                stateChanged = true;
            }
        }
        let serialized = JSON.stringify(state);
        this.errorNotificationState = serialized;
        if (stateChanged ||
            typeof this._errorNotificationStateWritePending === "string") {
            this._persistErrorNotificationState(serialized);
        }
        return !suppressed;
    },

    _notifyForPayload: function() {
        this._retryErrorNotificationStateWrite();
        let currentWarnings = Object.create(null);
        let currentErrors = Object.create(null);
        for (let i = 0; i < this._usages.length; i++) {
            let usage = this._usages[i];
            let alert = this._alertSettings[usage.account] || this._defaultAlertRow(usage.account);
            if (["error", "login_required", "blocked"].indexOf(usage.status) !== -1) {
                let errorKey = usage.account + ":" + usage.status;
                currentErrors[errorKey] = true;
                if (this.notifyErrors && alert.errors && !this._errorState[errorKey]) {
                    let errorMessage = usage.status === "login_required"
                        ? "Token abgelaufen · codex-usage reactivate " + usage.account
                        : usage.error || this._statusLabel(usage.status);
                    if (this._shouldNotifyError(errorKey + ":" + errorMessage)) {
                        Main.notify(
                            _("Codex Usage: ") + usage.label,
                            errorMessage
                        );
                    }
                }
            }
            if (usage.status === "partial") {
                let partialKey = usage.account + ":partial";
                currentWarnings[partialKey] = true;
                if (this.notifyWarnings && alert.warnings && !this._warningState[partialKey]) {
                    Main.notify(
                        _("Codex-Daten unvollständig: ") + usage.label,
                        usage.error || _("Nicht alle Nutzungsfenster verfügbar")
                    );
                }
            }
            let legacyPoolUsable = !usage.main || this._poolIsUsable(usage.main);
            let windows = [
                ["5h", legacyPoolUsable ? usage.five_hour : null, "five-threshold"],
                ["Woche", legacyPoolUsable ? usage.weekly : null, "weekly-threshold"],
                ["30d", legacyPoolUsable
                    ? this._poolWindowForDuration(usage.main, 2592000)
                    : null, "monthly-threshold"]
            ];
            let spark = this._modelPool(usage, "gpt-5.3-codex-spark");
            if (this._poolIsUsable(spark) && Array.isArray(spark.windows)) {
                for (let sparkIndex = 0; sparkIndex < spark.windows.length; sparkIndex++) {
                    let sparkWindow = spark.windows[sparkIndex];
                    if (!this._windowIdentityIsKnown(sparkWindow)) {
                        continue;
                    }
                    windows.push([
                        "Spark " + this._windowDisplayLabel(sparkWindow),
                        sparkWindow,
                        "spark-threshold"
                    ]);
                }
            }
            for (let j = 0; j < windows.length; j++) {
                let remaining = this._remainingPercent(windows[j][1]);
                if (
                    windows[j][2] === "spark-threshold" &&
                    alert["spark-threshold"] === "no Spark"
                ) {
                    continue;
                }
                let threshold = Number(alert[windows[j][2]]);
                if (remaining !== null && Number.isFinite(threshold) && remaining <= threshold) {
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
        this._commandError = text;
        try {
            if (Array.isArray(this._usages) && this._usages.length) {
                this._buildUsageMenu();
            } else {
                this.menu.removeAll();
                this._addDisabled(this.menu, _("Codex Usage konnte nicht geladen werden"), "codex-usage-error");
                this._addDisabled(this.menu, text, "codex-usage-error");
                this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
                this._addActions();
            }
        } catch (e) {
            global.log("[" + UUID + "] command error display failed: " + this._shortText(e, 180));
        }
        try {
            this._clearPanelClasses();
            this.actor.add_style_class_name("codex-usage-panel-error");
        } catch (e) {
            global.log("[" + UUID + "] command error panel state failed: " + this._shortText(e, 180));
        }
        try {
            this.set_applet_tooltip(text);
        } catch (e) {
            global.log("[" + UUID + "] command error tooltip failed: " + this._shortText(e, 180));
        }
        if (this.notifyErrors) {
            try {
                let key = "command:" + text;
                if (!this._errorState[key] && this._shouldNotifyError(key)) {
                    Main.notify(_("Codex Usage"), text);
                    this._errorState[key] = true;
                }
            } catch (e) {
                global.log("[" + UUID + "] command error notification failed: " + this._shortText(e, 180));
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

    _percentPartsFromValue: function(value, account, surface, forceVisible) {
        if (!forceVisible && !this._elementTargetEnabled(account, "percent", surface)) {
            return { plain: "", markup: "" };
        }
        let plain = value === null || !Number.isFinite(value)
            ? "–"
            : Math.round(value) + "%";
        let style = this._percentStyles[account] || this._defaultStyleRow(account, "percent");
        let markup = this._styleSpan(plain, style, value, surface);
        return { plain: plain, markup: markup };
    },

    _remainingPercent: function(window) {
        if (!window) {
            return null;
        }
        let hasUsed = window.used !== null && window.used !== undefined;
        let hasLimit = window.limit !== null && window.limit !== undefined;
        let hasRemaining = window.remaining !== null && window.remaining !== undefined;
        let hasPercent = window.percent !== null && window.percent !== undefined;
        if (
            (hasUsed && (typeof window.used !== "number" || !Number.isFinite(window.used))) ||
            (hasLimit && (typeof window.limit !== "number" || !Number.isFinite(window.limit))) ||
            (hasRemaining && (
                typeof window.remaining !== "number" ||
                !Number.isFinite(window.remaining)
            )) ||
            (hasPercent && (typeof window.percent !== "number" || !Number.isFinite(window.percent)))
        ) {
            return null;
        }
        if (hasUsed && window.used < 0) {
            return null;
        }
        if (hasLimit && window.limit <= 0) {
            return null;
        }
        if (hasRemaining && window.remaining < 0) {
            return null;
        }
        if (
            hasRemaining &&
            hasLimit &&
            window.remaining > window.limit
        ) {
            return null;
        }
        if (hasPercent && (window.percent < 0 || window.percent > 100)) {
            return null;
        }
        if (typeof window.used === "number" && Number.isFinite(window.used) &&
            typeof window.limit === "number" && Number.isFinite(window.limit) &&
            window.limit > 0) {
            if (window.used < 0) {
                return null;
            }
            if (window.used >= window.limit) {
                return 0;
            }
            return 100 - (window.used / window.limit * 100);
        }
        if (typeof window.remaining === "number" && Number.isFinite(window.remaining)) {
            if (typeof window.limit === "number" && Number.isFinite(window.limit) &&
                window.limit > 0) {
                if (window.remaining < 0 || window.remaining > window.limit) {
                    return null;
                }
                return window.remaining / window.limit * 100;
            }
            if (typeof window.percent === "number" && Number.isFinite(window.percent)) {
                if (window.percent < 0 || window.percent > 100) {
                    return null;
                }
                if (window.remaining <= 100 &&
                    Math.abs(window.remaining - window.percent) >= 0.01) {
                    return null;
                }
                return window.percent;
            }
            if (window.remaining < 0 || window.remaining > 100) {
                return null;
            }
            return window.remaining;
        }
        if (typeof window.percent === "number" && Number.isFinite(window.percent)) {
            return window.percent >= 0 && window.percent <= 100 ? window.percent : null;
        }
        return null;
    },

    _windowResetParts: function(window, account, surface, includeUnselected, part) {
        let dateEnabled = this._elementTargetEnabled(account, "date", surface);
        let timeEnabled = this._elementTargetEnabled(account, "time", surface);
        let durationEnabled = this._elementTargetEnabled(account, "duration", surface);
        if (part === "date-time") {
            durationEnabled = false;
        } else if (part === "duration") {
            dateEnabled = false;
            timeEnabled = false;
        }
        let showDate = includeUnselected || dateEnabled;
        let showTime = includeUnselected || timeEnabled;
        let showDuration = includeUnselected || durationEnabled;
        if (!showDate && !showTime && !showDuration) {
            return { plain: "", markup: "" };
        }
        if (!window || !window.reset_at) {
            return { plain: "–", markup: this._escapeMarkup("–") };
        }
        let millis = this._dateMillis(window.reset_at);
        if (millis === null) {
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
            markupParts.push(dateEnabled
                ? this._styleSpan(dateText, dateStyle, remaining, surface)
                : this._escapeMarkup(dateText));
        }
        if (showTime) {
            plainParts.push(timeText);
            markupParts.push(timeEnabled
                ? this._styleSpan(timeText, timeStyle, remaining, surface)
                : this._escapeMarkup(timeText));
        }
        if (showDuration) {
            let labeledDuration = "Rest " + durationText;
            plainParts.push(labeledDuration);
            markupParts.push(durationEnabled
                ? this._escapeMarkup("Rest ") + this._styleSpan(durationText, durationStyle, remaining, surface)
                : this._escapeMarkup(labeledDuration));
        }
        return {
            plain: plainParts.join(" "),
            markup: markupParts.join(" ")
        };
    },

    _elementTargetEnabled: function(account, element, surface, legacyVisible) {
        let elements = {
            percent: 0,
            date: 1,
            time: 2,
            duration: 3,
            consumption: 4,
            "consumption-weekly": 10,
            "consumption-short": 14,
            "consumption-monthly": 15,
            forecast: 5,
            "usage-resets": 6,
            "account-id": 7,
            label: 8,
            tag: 9,
            credits: 11,
            "credit-consumption": 12
        };
        let elementId = elements[element];
        if (elementId === undefined) {
            return false;
        }
        let target = this._styleTargets[account + ":" + elementId];
        if (!target && legacyVisible !== undefined) {
            return legacyVisible === true;
        }
        return this._targetEnabled(account, element, surface);
    },

    _targetEnabled: function(account, element, surface) {
        let elements = {
            percent: 0,
            date: 1,
            time: 2,
            duration: 3,
            consumption: 4,
            "consumption-weekly": 10,
            "consumption-short": 14,
            "consumption-monthly": 15,
            forecast: 5,
            "usage-resets": 6,
            "account-id": 7,
            label: 8,
            tag: 9,
            credits: 11,
            "credit-consumption": 12
        };
        let elementId = elements[element];
        if (elementId === undefined) {
            return false;
        }
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
        if (millis === null) {
            return null;
        }
        return Math.max(0, Math.ceil((millis - Date.now()) / 60000));
    },

    _formatDurationPart: function(minutes, format, decimalCompact) {
        if (minutes === null || !Number.isFinite(minutes)) {
            return "–";
        }
        let total = Math.max(0, Math.round(minutes));
        let days = Math.floor(total / 1440);
        let hours = Math.floor((total % 1440) / 60);
        let rest = total % 60;
        let pad = function(number) { return String(number).padStart(2, "0"); };
        if (decimalCompact && format === 0) {
            return (total / 60).toFixed(1).replace(".", ",") + "h";
        }
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

    _styleSpan: function(text, style, remaining, surface, dynamic) {
        let escaped = this._escapeMarkup(text);
        if (!this._styleIsActive(style, remaining, dynamic)) {
            return escaped;
        }
        let mode = this._styleMode(style);
        let below = dynamic === true || (remaining !== null && Number.isFinite(remaining) &&
            remaining < Number(style.threshold));
        let useBelow = mode === 2 && below;
        let fontValue = useBelow ? style["below-font"] : style.font;
        let sizeValue = useBelow ? style["below-size"] : style.size;
        let boldValue = useBelow ? style["below-bold"] : style.bold;
        let italicValue = useBelow ? style["below-italic"] : style.italic;
        let colorValue = useBelow ? style["below-color"] : style.color;
        let backgroundValue = useBelow ? style["below-background"] : style.background;
        if (surface === "hover") {
            backgroundValue = useBelow
                ? style["below-hover-background"]
                : style["hover-background"];
        }
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
            if (useBelow) {
                backgroundValue = style["below-background"] === undefined
                    ? 0
                    : style["below-background"];
            } else {
                backgroundValue = style.background === undefined ? 0 : style.background;
            }
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

    _styleIsActive: function(style, remaining, dynamic) {
        let mode = this._styleMode(style);
        if (mode === 3) {
            return false;
        }
        if (mode !== 1) {
            return true;
        }
        return (style.dynamic === true && dynamic === true) ||
            (remaining !== null && Number.isFinite(remaining) &&
                remaining < Number(style.threshold));
    },

    _escapeMarkup: function(value) {
        return String(value === null || value === undefined ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
    },

    _usageSeverity: function(usage) {
        if (["error", "login_required", "blocked"].indexOf(usage.status) !== -1) {
            return "codex-usage-error";
        }
        let legacyPoolUsable = !usage.main || this._poolIsUsable(usage.main);
        let five = legacyPoolUsable ? this._remainingPercent(usage.five_hour) : null;
        let week = legacyPoolUsable ? this._remainingPercent(usage.weekly) : null;
        let monthly = this._poolIsUsable(usage.main)
            ? this._remainingPercent(this._poolWindowForDuration(usage.main, 2592000))
            : null;
        let sparkPool = this._modelPool(usage, "gpt-5.3-codex-spark");
        let sparkValues = this._poolIsUsable(sparkPool) && Array.isArray(sparkPool.windows)
            ? sparkPool.windows.map(Lang.bind(this, function(window) {
                return this._remainingPercent(window);
            })).filter(function(value) { return value !== null; })
            : [];
        let values = [five, week, monthly].concat(sparkValues).filter(function(value) { return value !== null; });
        if (!values.length) {
            if (usage.status === "partial") {
                return "codex-usage-warning";
            }
            return usage.stale ? "codex-usage-stale" : "";
        }
        let alert = this._alertSettings[usage.account] || this._defaultAlertRow(usage.account);
        let fiveThreshold = Number(alert["five-threshold"]);
        let weeklyThreshold = Number(alert["weekly-threshold"]);
        let monthlyThreshold = Number(alert["monthly-threshold"]);
        let sparkThreshold = Number(alert["spark-threshold"]);
        let critical = values.some(function(value) { return value <= 5; });
        let warning = usage.status === "partial" ||
            (five !== null && Number.isFinite(fiveThreshold) && five <= fiveThreshold) ||
            (week !== null && Number.isFinite(weeklyThreshold) && week <= weeklyThreshold);
        warning = warning || (monthly !== null && Number.isFinite(monthlyThreshold) && monthly <= monthlyThreshold);
        warning = warning || sparkValues.some(function(value) {
            return Number.isFinite(sparkThreshold) && value <= sparkThreshold;
        });
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
        let newestMs = null;
        for (let i = 0; i < this._usages.length; i++) {
            let value = this._usages[i].captured_at;
            let millis = this._dateMillis(value);
            if (
                millis !== null &&
                !this._captureIsTooFarInFuture(value, Date.now()) &&
                (newestMs === null || millis > newestMs)
            ) {
                newestMs = millis;
                newest = value;
            }
        }
        return newest;
    },

    _dateMillis: function(value) {
        if (typeof value !== "string" || value.length === 0) {
            return null;
        }
        let parsed = Date.parse(value);
        return Number.isFinite(parsed) ? parsed : null;
    },

    _captureTimestampUsable: function(value) {
        let millis = this._dateMillis(value);
        return millis !== null && !this._captureIsTooFarInFuture(value, Date.now());
    },

    _captureIsTooFarInFuture: function(value, referenceMs) {
        let millis = this._dateMillis(value);
        return millis !== null && millis > referenceMs + MAX_CAPTURE_FUTURE_MS;
    },

    _captureIsOlder: function(candidate, existing) {
        let candidateMs = this._dateMillis(candidate);
        let existingMs = this._dateMillis(existing);
        let nowMs = Date.now();
        if (this._captureIsTooFarInFuture(candidate, nowMs)) {
            return existingMs !== null;
        }
        if (this._captureIsTooFarInFuture(existing, nowMs)) {
            return false;
        }
        return existingMs !== null && (candidateMs === null || candidateMs < existingMs);
    },

    _formatDate: function(value) {
        let millis = this._dateMillis(value);
        if (millis === null) {
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

    _strictIntegerSetting: function(value) {
        return typeof value === "number" && Number.isInteger(value) ? value : null;
    },

    _shortText: function(value, limit) {
        let text = String(value || "").replace(/[\u0000-\u001f\u007f]/g, " ").trim();
        if (text.length <= limit) {
            return text;
        }
        return text.slice(0, Math.max(0, limit - 1)) + "…";
    },

    _cleanupLog: function(message) {
        if (this._cleanupLogCount >= MAX_CLEANUP_LOGS) {
            return;
        }
        this._cleanupLogCount += 1;
        global.log("[" + UUID + "] " + this._shortText(message, 180));
    },

    _openAnalytics: function() {
        try {
            Gio.AppInfo.launch_default_for_uri(ANALYTICS_URL, null);
        } catch (e) {
            this._showCommandError(_("Browser konnte nicht geöffnet werden: ") + String(e));
        }
    },

    _openSettings: function(tab) {
        let argv = ["xlet-settings", "applet", UUID];
        let instanceId = this.instanceId;
        if (
            (typeof instanceId === "number" && Number.isInteger(instanceId) && instanceId >= 0) ||
            (typeof instanceId === "string" && /^[0-9]+$/.test(instanceId))
        ) {
            argv.push("-i", String(instanceId));
        }
        if (typeof tab === "number" && Number.isInteger(tab) && tab >= 0) {
            argv.push("-t", String(tab));
        }
        let settingsProcess = null;
        try {
            // xlet-settings can block for tens of seconds while the broken AT-SPI
            // bridge is starting. Settings remain fully usable without that bridge.
            let launcher = null;
            if (Gio.SubprocessLauncher && typeof Gio.SubprocessLauncher.new === "function") {
                try {
                    launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE);
                } catch (e) {
                    this._cleanupLog("settings launcher unavailable: " +
                        this._shortText(e, 180));
                }
            }
            if (launcher && typeof launcher.spawnv === "function") {
                let launcherEnvironmentReady = false;
                if (typeof launcher.setenv === "function") {
                    try {
                        launcher.setenv("NO_AT_BRIDGE", "1", true);
                        launcherEnvironmentReady = true;
                    } catch (e) {
                        this._cleanupLog("settings AT-SPI bypass unavailable: " +
                            this._shortText(e, 180));
                    }
                }
                if (launcherEnvironmentReady) {
                    try {
                        settingsProcess = launcher.spawnv(argv);
                    } catch (e) {
                        this._cleanupLog("settings launcher spawn failed: " +
                            this._shortText(e, 180));
                    }
                } else {
                    this._cleanupLog("settings launcher cannot set child environment");
                }
            }
            if (!settingsProcess && Gio.Subprocess && typeof Gio.Subprocess.new === "function") {
                // Keep AT-SPI disabled even when the Launcher API is absent or
                // its spawn path fails. `Gio.Subprocess.new()` cannot change
                // the child environment, so use the system env wrapper.
                settingsProcess = Gio.Subprocess.new(
                    ["/usr/bin/env", "NO_AT_BRIDGE=1"].concat(argv),
                    Gio.SubprocessFlags.NONE
                );
            }
            if (!settingsProcess) {
                throw new Error("settings subprocess unavailable");
            }
        } catch (e) {
            this._showCommandError(_("Einstellungen konnten nicht geöffnet werden: ") + String(e));
            return;
        }
        let settingsPid = null;
        if (settingsProcess && typeof settingsProcess.get_identifier === "function") {
            try {
                let identifier = String(settingsProcess.get_identifier() || "");
                if (/^[1-9][0-9]*$/.test(identifier)) {
                    settingsPid = identifier;
                }
            } catch (e) {
                this._cleanupLog("settings process identifier unavailable: " + this._shortText(e, 180));
            }
        }
        try {
            this._scheduleSettingsMaximize(settingsPid);
        } catch (e) {
            this._cleanupLog("settings maximize scheduling failed: " + this._shortText(e, 180));
        }
    },

    configureApplet: function(tab) {
        this._openSettings(tab);
    },

    _settingsWindowIdForProcess: function(output, pid) {
        let targetPid = String(pid || "");
        if (!/^[1-9][0-9]*$/.test(targetPid)) {
            return null;
        }
        let lines = String(output || "").split(/\r?\n/);
        for (let index = 0; index < lines.length; index++) {
            let fields = lines[index].trim().split(/\s+/);
            if (
                fields.length >= 3 &&
                /^0x[0-9a-f]+$/i.test(fields[0]) &&
                fields[2] === targetPid
            ) {
                return fields[0];
            }
        }
        return null;
    },

    _scheduleSettingsMaximize: function(settingsPid) {
        this._removeSource("_settingsMaximizeId");
        this._terminateChild(this._settingsPlacementProcess, "settings placement restart");
        this._settingsPlacementProcess = null;
        this._terminateChild(this._settingsWindowLookupProcess, "settings window lookup restart");
        this._settingsWindowLookupProcess = null;
        let generation = (this._settingsMaximizeGeneration || 0) + 1;
        this._settingsMaximizeGeneration = generation;
        let targetPid = String(settingsPid || "");
        if (!/^[1-9][0-9]*$/.test(targetPid)) {
            targetPid = "";
        }
        let targetWindowId = null;
        let lookupPending = false;
        let lookupAttempts = 0;
        let targetUnavailable = false;
        let attempts = 0;
        let placementAttempts = 0;
        let positioned = false;
        let placementPending = false;
        let focused = false;
        let maximize = Lang.bind(this, function() {
            if (generation !== this._settingsMaximizeGeneration) {
                return false;
            }
            if (this._removed) {
                this._clearSource("_settingsMaximizeId");
                return false;
            }
            if (targetPid && !targetWindowId && targetUnavailable) {
                this._clearSource("_settingsMaximizeId");
                return false;
            }
            if (!positioned) {
                if (targetPid && !targetWindowId) {
                    lookupAttempts += 1;
                    if (lookupAttempts >= SETTINGS_WINDOW_LOOKUP_MAX_ATTEMPTS) {
                        targetUnavailable = true;
                        lookupPending = false;
                        this._terminateChild(
                            this._settingsWindowLookupProcess,
                            "settings window lookup timeout"
                        );
                        this._settingsWindowLookupProcess = null;
                        this._clearSource("_settingsMaximizeId");
                        return false;
                    }
                    if (!lookupPending) {
                        try {
                            let lookupProcess = Gio.Subprocess.new(
                                ["wmctrl", "-lp"],
                                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
                            );
                            if (!lookupProcess) {
                                throw new Error("settings window lookup unavailable");
                            }
                            lookupPending = true;
                            this._settingsWindowLookupProcess = lookupProcess;
                            this._readBoundedProcessOutput(
                                lookupProcess,
                                Lang.bind(this, function(stdout, _stderr, _error) {
                                    if (
                                        generation !== this._settingsMaximizeGeneration ||
                                        this._removed ||
                                        targetUnavailable
                                    ) {
                                        return;
                                    }
                                    lookupPending = false;
                                    if (this._settingsWindowLookupProcess === lookupProcess) {
                                        this._settingsWindowLookupProcess = null;
                                    }
                                    targetWindowId = this._settingsWindowIdForProcess(stdout, targetPid);
                                })
                            );
                        } catch (e) {
                            lookupPending = false;
                            this._settingsWindowLookupProcess = null;
                            this._cleanupLog("settings window lookup failed: " + this._shortText(e, 180));
                        }
                    }
                    return true;
                }
                if (placementPending) {
                    placementAttempts += 1;
                    if (placementAttempts < 12) {
                        return true;
                    }
                    positioned = true;
                    placementPending = false;
                    this._terminateChild(this._settingsPlacementProcess, "settings placement timeout");
                    this._settingsPlacementProcess = null;
                } else {
                    placementAttempts += 1;
                    try {
                        let monitor = null;
                        if (Main.layoutManager && typeof Main.layoutManager.findMonitorForActor === "function") {
                            monitor = Main.layoutManager.findMonitorForActor(this.actor);
                        }
                        if (!monitor && Main.layoutManager) {
                            monitor = Main.layoutManager.currentMonitor;
                        }
                        let monitorX = monitor && Number(monitor.x);
                        let monitorY = monitor && Number(monitor.y);
                        if (Number.isFinite(monitorX) && Number.isFinite(monitorY)) {
                            let target = targetWindowId
                                ? ["-i", "-r", targetWindowId]
                                : ["-r", "Codex Usage"];
                            let moveProcess = Gio.Subprocess.new(
                                ["wmctrl"].concat(target).concat(["-e",
                                    "0," + String(Math.round(monitorX)) + "," +
                                    String(Math.round(monitorY)) + ",-1,-1"]),
                                Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE
                            );
                            if (
                                moveProcess &&
                                typeof moveProcess.wait_check_async === "function" &&
                                typeof moveProcess.wait_check_finish === "function"
                            ) {
                                placementPending = true;
                                this._settingsPlacementProcess = moveProcess;
                                moveProcess.wait_check_async(null, Lang.bind(this, function(source, result) {
                                    if (generation !== this._settingsMaximizeGeneration || this._removed) {
                                        return;
                                    }
                                    placementPending = false;
                                    if (this._settingsPlacementProcess === source) {
                                        this._settingsPlacementProcess = null;
                                    }
                                    if (this._removed) {
                                        return;
                                    }
                                    try {
                                        if (
                                            source.wait_check_finish(result) === true ||
                                            placementAttempts >= 12
                                        ) {
                                            positioned = true;
                                        }
                                    } catch (e) {
                                        if (placementAttempts >= 12) {
                                            positioned = true;
                                        }
                                    }
                                }));
                                return true;
                            }
                            positioned = true;
                            return true;
                        }
                        if (placementAttempts < 12) {
                            return true;
                        }
                        positioned = true;
                    } catch (e) {
                        this._terminateChild(this._settingsPlacementProcess, "settings placement startup cleanup");
                        this._settingsPlacementProcess = null;
                        placementPending = false;
                        this._cleanupLog("settings window placement failed: " + this._shortText(e, 180));
                        positioned = true;
                    }
                    if (!positioned && placementAttempts < 12) {
                        return true;
                    }
                    positioned = true;
                }
            }
            try {
                let target = targetWindowId
                    ? ["-i", "-r", targetWindowId]
                    : ["-r", "Codex Usage"];
                Gio.Subprocess.new(
                    ["wmctrl"].concat(target).concat(["-b", "add,maximized_vert,maximized_horz"]),
                    Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE
                );
            } catch (e) {
                this._clearSource("_settingsMaximizeId");
                return false;
            }
            if (!focused) {
                try {
                    let focusTarget = targetWindowId
                        ? ["-i", "-a", targetWindowId]
                        : ["-a", "Codex Usage"];
                    Gio.Subprocess.new(
                        ["wmctrl"].concat(focusTarget),
                        Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE
                    );
                    focused = true;
                } catch (e) {
                    this._cleanupLog("settings window activation failed: " + this._shortText(e, 180));
                }
            }
            attempts += 1;
            if (attempts >= 12) {
                this._clearSource("_settingsMaximizeId");
                return false;
            }
            return true;
        });
        let id = Mainloop.timeout_add(250, maximize);
        if (id) {
            this._setSource("_settingsMaximizeId", id);
        }
    },

    _terminateChild: function(process, context) {
        if (!process) {
            return;
        }
        try {
            process.force_exit();
        } catch (e) {
            global.log("[" + UUID + "] " + context + " failed: " + this._shortText(e, 180));
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
                    this._cleanupLog("process cleanup failed: " + e);
            }
            this._process = null;
        }
    },

    _cancelAuxProcess: function() {
        if (
            this._auxCommand === "profile-job-status" ||
            this._auxCommand === "profile-job-cancel"
        ) {
            let account = this._profileJobCommandAccount || this._profileJobPollingAccount;
            if (
                account &&
                this._deviceLoginJobs[account] &&
                this._profileJobResumeQueue.indexOf(account) === -1
            ) {
                this._profileJobResumeQueue.unshift(account);
            }
            this._profileJobPollingAccount = "";
            this._profileJobCommandAccount = "";
            this._deviceLoginPollGeneration += 1;
            this._removeSource("_deviceLoginPollId");
        }
        if (this._auxCommand === "profile-jobs") {
            this._profileJobsLoaded = false;
        }
        if (this._auxCommand === "device-login") {
            let liveAccount = this._deviceLoginLiveAccount;
            if (liveAccount && !this._deviceLoginJobs[liveAccount]) {
                delete this._deviceLoginActive[liveAccount];
                delete this._deviceLoginEvents[liveAccount];
            }
            this._deviceLoginLiveText = Object.create(null);
            this._deviceLoginLiveAccount = "";
        }
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
                this._cleanupLog("auxiliary process cleanup failed: " + e);
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
                this._cleanupLog("health process cleanup failed: " + e);
            }
            this._healthProcess = null;
        }
    },

    _cancelRemovedReactivations: function(accounts) {
        let active = Object.keys(this._reactivations);
        for (let i = 0; i < active.length; i++) {
            if (!Object.prototype.hasOwnProperty.call(accounts, active[i])) {
                this._cancelReactivation(active[i]);
            }
        }
    },

    _cancelReactivation: function(account) {
        let record = this._reactivations[account];
        if (!record) {
            return;
        }
        record.done = true;
        let timeoutId = record.timeoutId;
        record.timeoutId = 0;
        if (timeoutId) {
            try {
                Mainloop.source_remove(timeoutId);
            } catch (e) {
                this._cleanupLog("reactivation source cleanup failed: " + e);
            }
        }
        if (record.process) {
            try {
                record.process.force_exit();
            } catch (e) {
                this._cleanupLog("reactivation process cleanup failed: " + e);
            }
        }
        delete this._reactivations[account];
        delete this._reactivationErrors[account];
    },

    _cancelReactivations: function() {
        let accounts = Object.keys(this._reactivations);
        for (let i = 0; i < accounts.length; i++) {
            this._cancelReactivation(accounts[i]);
        }
        this._reactivations = Object.create(null);
    },

    on_applet_clicked: function() {
        this._runSafely("applet click", Lang.bind(this, function() {
            if (this._removed || !this.menu) {
                return;
            }
            let wasOpen = this.menu.isOpen;
            if (!wasOpen && this._menuDirty) {
                this._buildUsageMenu();
            }
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
        this._settingsMaximizeGeneration = (this._settingsMaximizeGeneration || 0) + 1;
        this._refreshing = false;
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
        this._removeSource("_settingsMaximizeId");
        this._terminateChild(this._settingsPlacementProcess, "settings placement cleanup");
        this._settingsPlacementProcess = null;
        this._terminateChild(this._settingsWindowLookupProcess, "settings window lookup cleanup");
        this._settingsWindowLookupProcess = null;
        this._deviceLoginPollGeneration += 1;
        this._removeSource("_deviceLoginPollId");
        this._removeIdleSources();
        this._cancelProcess();
        this._cancelAuxProcess();
        this._cancelHealthProcess();
        this._cancelReactivations();
        this._disconnectTrackedSignals();
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
