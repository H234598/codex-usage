const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../files/codex-usage@H234598/applet.js"),
  "utf8"
);

function loadPrototype(onReady) {
  const runtime = {
    idleAdd: () => 1,
    timeoutAdd: () => 2,
    timeoutAddSeconds: () => 3,
    launcherFactory: () => { throw new Error("launcher not configured"); },
  };
  const mainloop = {
    idle_add: (...args) => runtime.idleAdd(...args),
    source_remove: () => {},
    timeout_add: (...args) => runtime.timeoutAdd(...args),
    timeout_add_seconds: (...args) => runtime.timeoutAddSeconds(...args),
  };
  const gio = {
    SubprocessFlags: { STDOUT_PIPE: 1, STDERR_PIPE: 2 },
    SubprocessLauncher: {
      new: (...args) => runtime.launcherFactory(...args),
    },
  };
  class PopupItem {
    constructor() {
      this.actor = { add_style_class_name() {} };
      this.label = { clutter_text: { set_markup() {} } };
    }
  }
  const sandbox = {
    imports: {
      byteArray: { toString: (value) => Buffer.from(value).toString("utf8") },
      gi: { Gio: gio, GLib: {}, St: {} },
      lang: { bind: (object, callback) => callback.bind(object) },
      mainloop,
      ui: {
        applet: { TextIconApplet: function TextIconApplet() {} },
        main: { notify() {} },
        popupMenu: {
          PopupMenuItem: PopupItem,
          PopupSeparatorMenuItem: PopupItem,
          PopupSwitchMenuItem: PopupItem,
          PopupSubMenuMenuItem: PopupItem,
        },
        settings: {},
      },
    },
    global: { log() {} },
    console,
    Date,
    JSON,
    Math,
    Number,
    String,
    Object,
    Array,
    Boolean,
    Error,
    RegExp,
  };
  vm.runInNewContext(
    `${source}\nglobalThis.__CodexUsageApplet = CodexUsageApplet;`,
    sandbox
  );
  if (onReady) {
    onReady(runtime);
  }
  return sandbox.__CodexUsageApplet.prototype;
}

function makeApplet(onReady) {
  const prototype = loadPrototype(onReady);
  const applet = Object.create(prototype);
  applet._removed = false;
  applet._sources = {};
  applet._idleSources = {};
  applet._reactivations = {};
  applet._reactivationErrors = {};
  applet._reactivationRefreshPending = false;
  applet._backendChangeQueue = [];
  applet._backendChangeCurrent = null;
  applet._backendAuxQueue = [];
  applet._generation = 0;
  applet._process = null;
  applet._primaryRequest = null;
  applet._primaryCachePending = false;
  applet._primaryCacheRefreshAfter = false;
  applet._primaryFreshPending = false;
  applet._primaryFreshOpenAfter = false;
  applet._auxProcess = null;
  applet._auxCommand = "";
  applet._auxGeneration = 0;
  applet._healthProcess = null;
  applet._healthGeneration = 0;
  applet._timeoutId = 0;
  applet._auxTimeoutId = 0;
  applet._healthTimeoutId = 0;
  applet._timerId = 0;
  applet._displayTimerId = 0;
  applet._timerGeneration = 0;
  applet._displayTimerGeneration = 0;
  applet._staleCheckGeneration = 0;
  applet._lastGoodPanel = { plain: "--", markup: "--" };
  applet._lastGoodTooltip = "";
  applet._internalFailures = [];
  applet._refreshFailures = 0;
  applet._circuitOpenUntil = 0;
  applet._safeMode = false;
  applet._safeModeReason = "";
  applet._panelSettings = {};
  applet._alertSettings = {};
  applet._percentStyles = {};
  applet._dateStyles = {};
  applet._timeStyles = {};
  applet._durationStyles = {};
  applet._styleTargets = {};
  applet.panelHeight = 24;
  applet.panelAccountSeparator = "bar";
  applet.set_applet_label = () => {};
  applet.set_applet_tooltip = () => {};
  applet._setPanelMarkup = () => {};
  applet._clearPanelClasses = () => {};
  applet.actor = { add_style_class_name() {}, remove_style_class_name() {} };
  applet._usages = [
    {
      account: "alpha",
      label: "Alpha",
      status: "ok",
      five_hour: { remaining: 80, reset_at: "2026-07-10T15:00:00+00:00" },
      weekly: { remaining: 60, reset_at: "2026-07-11T15:00:00+00:00" },
    },
    {
      account: "beta",
      label: "Beta",
      status: "ok",
      five_hour: { remaining: 40, reset_at: "2026-07-10T16:00:00+00:00" },
      weekly: { remaining: 90, reset_at: "2026-07-12T16:00:00+00:00" },
    },
  ];
  applet._panelSettings = {
    alpha: { account: "alpha", tag: "A", order: 2, muted: false, slot1: 1, slot2: 2 },
    beta: { account: "beta", tag: "B", order: 1, muted: true, slot1: 3, slot2: 3 },
  };
  applet._backendAccounts = { alpha: {}, beta: {} };
  return applet;
}

test("panel slots honor ordering, mute and duplicate-source normalization", () => {
  const applet = makeApplet();
  const items = applet._panelItems();
  assert.deepEqual(Array.from(items, (item) => item.usage.account), ["beta", "alpha"]);
  assert.deepEqual(
    Array.from(items, (item) => Array.from(item.slots, (slot) => slot.source)),
    [[3], [1, 2]]
  );
  assert.equal(items[0].visible, false);
  assert.deepEqual(
    applet._panelContent(items.filter((item) => item.visible)).plain,
    "A 5h 80% / W 60%"
  );
});

test("remaining percentage prefers absolute used and limit values", () => {
  const applet = makeApplet();
  assert.equal(
    applet._remainingPercent({ used: 8, limit: 40, remaining: 32, percent: 20 }),
    80
  );
  assert.equal(applet._remainingPercent({ remaining: undefined, percent: undefined }), null);
});

test("epoch reset timestamps remain valid and report zero duration", () => {
  const applet = makeApplet();
  const epoch = "1970-01-01T00:00:00.000Z";
  assert.equal(applet._dateMillis(epoch), 0);
  assert.notEqual(applet._formatDate(epoch), "–");
  assert.equal(applet._durationMinutes({ reset_at: epoch }), 0);
});

test("idle scheduling does not retain an invalid zero source", () => {
  const applet = makeApplet((runtime) => {
    runtime.idleAdd = () => 0;
  });
  assert.equal(applet._addIdle(() => {}), 0);
  assert.deepEqual(applet._idleSources, {});
});

test("internal failures enter safe mode after the configured limit", () => {
  const applet = makeApplet();
  applet._enterSafeMode = function(reason) {
    this._safeMode = true;
    this._safeModeReason = reason;
  };
  applet._recordInternalFailure("test", new Error("broken"));
  applet._recordInternalFailure("test", new Error("broken"));
  applet._recordInternalFailure("test", new Error("broken"));
  assert.equal(applet._safeMode, true);
  assert.match(applet._safeModeReason, /test/);
});

test("safe mode cancels reactivation processes and pending refreshes", () => {
  const applet = makeApplet();
  let forced = 0;
  let healthForced = 0;
  applet._healthProcess = { force_exit() { healthForced += 1; } };
  applet._reactivations = {
    alpha: {
      done: false,
      timeoutId: 0,
      process: { force_exit() { forced += 1; } },
    },
  };
  applet._reactivationRefreshPending = true;
  applet._serviceAutoAttempted = true;
  applet._primaryCachePending = true;
  applet._primaryCacheRefreshAfter = true;
  applet._primaryFreshPending = true;
  applet._primaryFreshOpenAfter = true;
  applet._timerId = 11;
  applet._displayTimerId = 12;
  applet._staleCheckId = 13;
  applet._sources._timerId = 11;
  applet._sources._displayTimerId = 12;
  applet._sources._staleCheckId = 13;
  applet._enterSafeMode("reactivation test");
  assert.equal(forced, 1);
  assert.equal(healthForced, 1);
  assert.equal(applet._healthProcess, null);
  assert.equal(Object.keys(applet._reactivations).length, 0);
  assert.equal(applet._reactivationRefreshPending, false);
  assert.equal(applet._serviceAutoAttempted, false);
  assert.equal(applet._primaryCachePending, false);
  assert.equal(applet._primaryCacheRefreshAfter, false);
  assert.equal(applet._primaryFreshPending, false);
  assert.equal(applet._primaryFreshOpenAfter, false);
  assert.equal(applet._timerId, 0);
  assert.equal(applet._displayTimerId, 0);
  assert.equal(applet._staleCheckId, 0);
  assert.deepEqual(applet._sources, {});
});

test("safe mode retry reinstates the refresh timer", () => {
  const applet = makeApplet();
  let scheduled = 0;
  let auxiliaryRefreshes = 0;
  let freshRefreshes = 0;
  applet._safeMode = true;
  applet._scheduleTimer = () => { scheduled += 1; };
  applet._refreshAuxiliaryState = () => { auxiliaryRefreshes += 1; };
  applet._refreshFresh = () => { freshRefreshes += 1; };
  applet._leaveSafeModeAndRetry();
  assert.equal(scheduled, 1);
  assert.equal(auxiliaryRefreshes, 1);
  assert.equal(freshRefreshes, 1);
  assert.equal(applet._safeMode, false);
});

test("safe mode retry does not start auxiliary work after timer recovery fails", () => {
  const applet = makeApplet((runtime) => {
    runtime.timeoutAddSeconds = () => 0;
  });
  let auxiliaryRefreshes = 0;
  let freshRefreshes = 0;
  applet._safeMode = true;
  applet._refreshAuxiliaryState = () => { auxiliaryRefreshes += 1; };
  applet._refreshFresh = () => { freshRefreshes += 1; };

  applet._leaveSafeModeAndRetry();

  assert.equal(applet._safeMode, true);
  assert.equal(auxiliaryRefreshes, 0);
  assert.equal(freshRefreshes, 0);
});

test("safe mode ignores settings callbacks that could start background work", () => {
  const applet = makeApplet();
  let schedules = 0;
  let auxiliaryRefreshes = 0;
  let backendLoads = 0;
  let accountSyncs = 0;
  let styleSyncs = 0;
  applet._safeMode = true;
  applet._backendRowsReady = true;
  applet._backendAccounts = { alpha: { account: "alpha" } };
  applet._scheduleTimer = () => { schedules += 1; };
  applet._refreshAuxiliaryState = () => { auxiliaryRefreshes += 1; };
  applet._loadAccountBackends = () => { backendLoads += 1; };
  applet._syncAccountSettings = () => { accountSyncs += 1; };
  applet._syncStyleRows = () => { styleSyncs += 1; };

  applet._onRefreshSettingsChanged();
  applet._onPollOwnerChanged();
  applet._onPanelDefaultsChanged();
  applet._onPanelSettingsChanged();
  applet._onAlertSettingsChanged();
  applet._onPercentStylesChanged();
  applet._onStyleTargetsChanged();
  applet._onAccountBackendsChanged();

  assert.equal(schedules, 0);
  assert.equal(auxiliaryRefreshes, 0);
  assert.equal(backendLoads, 0);
  assert.equal(accountSyncs, 0);
  assert.equal(styleSyncs, 0);
});

test("service status recovery stops when timer setup enters safe mode", () => {
  const applet = makeApplet((runtime) => {
    runtime.timeoutAddSeconds = () => 0;
  });
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._spawnAuxJson = (_argv, callback) => callback(
    { installed: true, enabled: true, active: true },
    null
  );
  let continuations = 0;

  applet._checkServiceStatus(() => { continuations += 1; });

  assert.equal(applet._safeMode, true);
  assert.equal(continuations, 0);
});

test("service enable does not invoke its continuation after timer setup fails", () => {
  const applet = makeApplet((runtime) => {
    runtime.timeoutAddSeconds = () => 0;
  });
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._spawnAuxJson = (_argv, callback) => callback(
    { installed: true, enabled: true, active: true },
    null
  );
  let continuations = 0;

  applet._enableBackgroundService(() => { continuations += 1; });

  assert.equal(applet._safeMode, true);
  assert.equal(continuations, 0);
});

test("refresh circuit opens after three failures and leaves the last panel intact", () => {
  const applet = makeApplet();
  applet._recordRefreshFailure(new Error("first"));
  applet._recordRefreshFailure(new Error("second"));
  applet._recordRefreshFailure(new Error("third"));
  assert.equal(applet._refreshFailures, 3);
  assert.equal(applet._circuitOpen(), true);
  assert.equal(applet._lastGoodPanel.plain, "A 5h 80% / W 60%");
  assert.equal(applet._lastGoodPanel.markup, "A 5h 80% / W 60%");
});

test("oversized process output force-stops the child and reports a bounded error", () => {
  const applet = makeApplet();
  let forced = 0;
  let result = null;
  const oversized = {
    get_size: () => 262145,
    get_data: () => Buffer.alloc(0),
  };
  const stream = {
    read_bytes_async: (_size, _priority, _cancellable, callback) => callback(stream, {}),
    read_bytes_finish: () => oversized,
  };
  const process = {
    get_stdout_pipe: () => stream,
    get_stderr_pipe: () => stream,
    force_exit: () => { forced += 1; },
  };
  applet._readBoundedProcessOutput(process, (stdout, stderr, error) => {
    result = { stdout, stderr, error };
  });
  assert.equal(forced, 1);
  assert.equal(result.stdout, null);
  assert.match(result.error, /zu groß/);
});

test("safe menu construction contains menu failures", () => {
  const applet = makeApplet();
  applet.menu = { removeAll() { throw new Error("menu broken"); } };
  assert.doesNotThrow(() => applet._buildSafeMenu());
});

test("command error handling survives menu failures", () => {
  const applet = makeApplet();
  applet.menu = { removeAll() { throw new Error("menu broken"); } };
  assert.doesNotThrow(() => applet._showCommandError("backend failed"));
});

test("restlaufzeit is rendered, styled and uses the per-surface target", () => {
  const applet = makeApplet();
  applet._durationStyles = {
    alpha: {
      account: "alpha",
      format: 3,
      mode: 2,
      threshold: 120,
      font: 0,
      size: 0,
      bold: true,
      italic: false,
      color: 5,
      background: 0,
      "below-bold": true,
      "below-color": 3,
    },
  };
  applet._styleTargets = {
    "alpha:3": { panel: true, hover: true, click: true },
  };
  applet._usages[0].five_hour.reset_at = new Date(
    Date.now() + 124 * 60000 + 30000
  ).toISOString();
  const parts = applet._windowResetParts(
    applet._usages[0].five_hour,
    "alpha",
    "panel",
    false
  );
  assert.match(parts.plain, /^Rest 2h 05m$/);
  assert.match(parts.markup, /weight="bold"/);
  assert.match(parts.markup, /foreground="#2563eb"/);
  assert.equal(applet._formatDurationPart(150, 0), "2h 30m");
  assert.equal(applet._formatDurationPart(150, 1), "02:30");
  assert.equal(applet._formatDurationPart(150, 2), "2 Stunden 30 Minuten");
  assert.equal(applet._formatDurationPart(150, 3), "2h 30m");
});

test("style modes control normal, threshold and disabled formatting", () => {
  const applet = makeApplet();
  const style = {
    mode: 0,
    threshold: 20,
    font: 0,
    size: 0,
    bold: false,
    italic: false,
    color: 4,
    background: 0,
    "below-bold": true,
    "below-color": 3,
    "below-background": 0,
  };
  assert.match(applet._styleSpan("80%", style, 80, "panel"), /foreground="#16a34a"/);

  style.mode = 1;
  assert.equal(applet._styleSpan("80%", style, 80, "panel"), "80%");
  assert.match(applet._styleSpan("10%", style, 10, "panel"), /foreground="#16a34a"/);

  style.mode = 2;
  assert.match(applet._styleSpan("80%", style, 80, "panel"), /foreground="#16a34a"/);
  assert.match(applet._styleSpan("10%", style, 10, "panel"), /foreground="#dc2626"/);
  assert.match(applet._styleSpan("10%", style, 10, "panel"), /weight="bold"/);

  style.mode = 3;
  assert.equal(applet._styleSpan("<80%>", style, 10, "panel"), "&lt;80%&gt;");
});

test("date, time and restlaufzeit styles honor all modes and font colors", () => {
  const applet = makeApplet();
  const kinds = ["date", "time", "duration"];
  for (const kind of kinds) {
    const style = {
      mode: 0,
      threshold: kind === "duration" ? 120 : 20,
      format: 0,
      font: 0,
      size: 0,
      bold: false,
      italic: false,
      color: 4,
      background: 0,
      "below-font": 0,
      "below-size": 0,
      "below-bold": true,
      "below-italic": false,
      "below-color": 3,
      "below-background": 0,
    };
    const high = kind === "duration" ? 180 : 80;
    const low = kind === "duration" ? 60 : 10;
    assert.match(applet._styleSpan("value", style, high, "panel"), /foreground="#16a34a"/);
    style.mode = 1;
    assert.equal(applet._styleSpan("value", style, high, "panel"), "value");
    assert.match(applet._styleSpan("value", style, low, "panel"), /foreground="#16a34a"/);
    style.mode = 2;
    assert.match(applet._styleSpan("value", style, high, "panel"), /foreground="#16a34a"/);
    assert.match(applet._styleSpan("value", style, low, "panel"), /foreground="#dc2626"/);
    style.mode = 3;
    assert.equal(applet._styleSpan("value", style, low, "panel"), "value");
  }
});

test("primary cache and fresh requests are queued instead of cancelling each other", () => {
  const applet = makeApplet();
  applet._updatePanel = () => {};
  applet._buildUsageMenu = () => {};
  applet._buildLoadingMenu = () => {};
  applet._primaryRequest = { subcommand: "latest" };
  let calls = [];
  applet._spawnUsageCommand = (subcommand, callback) => {
    calls.push({ subcommand, callback });
  };

  applet._refreshFresh(true);
  applet._loadCached(true);
  assert.equal(calls.length, 0);
  assert.equal(applet._primaryFreshPending, true);
  assert.equal(applet._primaryFreshOpenAfter, true);
  assert.equal(applet._primaryCachePending, true);
  assert.equal(applet._primaryCacheRefreshAfter, true);

  applet._primaryRequest = null;
  applet._drainPrimaryRequests();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].subcommand, "latest");
  assert.equal(applet._primaryFreshPending, true);
  assert.equal(applet._primaryCachePending, false);
});

test("primary request queue drains even when payload handling throws", () => {
  const applet = makeApplet();
  applet._resolveCommand = () => "/usr/bin/codex-usage";
  applet._updatePanel = () => {};
  applet._buildUsageMenu = () => {};
  applet._buildLoadingMenu = () => {};
  applet._applyPayload = () => { throw new Error("payload handler failed"); };
  applet._primaryFreshPending = true;
  const callbacks = [];
  applet._spawnJsonArray = (_argv, callback, request) => {
    applet._primaryRequest = request;
    callbacks.push(callback);
  };

  applet._loadCached(false);
  assert.equal(callbacks.length, 1);
  applet._primaryRequest = null;
  assert.throws(() => callbacks[0]([{ account: "alpha" }], null), /payload handler failed/);
  assert.equal(callbacks.length, 2);
  assert.equal(applet._primaryRequest.subcommand, "once");
  assert.equal(applet._refreshing, true);
});

test("cache refresh intent survives a payload handling failure", () => {
  const applet = makeApplet();
  applet.autoRefresh = true;
  applet._usesAppletPolling = () => true;
  applet._resolveCommand = () => "/usr/bin/codex-usage";
  applet._applyPayload = () => { throw new Error("payload handler failed"); };
  applet._refreshAuxiliaryState = () => {};
  let freshRequests = 0;
  applet._refreshFresh = () => { freshRequests += 1; };
  const callbacks = [];
  applet._spawnJsonArray = (_argv, callback, request) => {
    applet._primaryRequest = request;
    callbacks.push(callback);
  };

  applet._loadCached(true);
  applet._primaryRequest = null;
  assert.throws(() => callbacks[0]([{ account: "alpha" }], null), /payload handler failed/);
  assert.equal(freshRequests, 1);
});

test("fresh reactivation refresh survives a payload handling failure", () => {
  const applet = makeApplet();
  applet._applyPayload = () => { throw new Error("fresh payload failed"); };
  applet._updatePanel = () => {};
  applet._buildUsageMenu = () => {};
  applet._buildLoadingMenu = () => {};
  applet._reactivationRefreshPending = true;
  const callbacks = [];
  applet._spawnUsageCommand = (_subcommand, callback) => {
    callbacks.push(callback);
  };

  applet._refreshFresh(false);
  assert.throws(() => callbacks[0]([{ account: "alpha" }], null), /fresh payload failed/);

  assert.equal(callbacks.length, 2);
  assert.equal(applet._refreshing, true);
  assert.equal(applet._reactivationRefreshPending, false);
});

test("refresh setup failures do not leave the refreshing flag set", () => {
  const applet = makeApplet();
  applet._updatePanel = () => { throw new Error("panel update failed"); };

  assert.throws(() => applet._refreshFresh(false), /panel update failed/);
  assert.equal(applet._refreshing, false);
});

test("legacy conditional style rows migrate to the corresponding mode", () => {
  const applet = makeApplet();
  const migrated = applet._normalizeStyleRow(
    {
      account: "alpha",
      conditional: true,
      threshold: 20,
      font: 0,
      size: 0,
      bold: false,
      italic: false,
      background: 0,
    },
    "alpha",
    "percent"
  );
  assert.equal(migrated.mode, 1);
  assert.equal(migrated.color, 0);
  assert.equal(migrated["below-color"], 3);
});

test("alert setting changes refresh the panel immediately", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._syncingAccountSettings = false;
  applet.accountAlertSettings = [
    {
      account: "alpha",
      "five-threshold": 20,
      "weekly-threshold": 20,
      warnings: true,
      errors: true,
    },
    {
      account: "beta",
      "five-threshold": 20,
      "weekly-threshold": 20,
      warnings: true,
      errors: true,
    },
  ];
  let refreshed = 0;
  applet._refreshFormattedSurfaces = () => { refreshed += 1; };
  applet._onAlertSettingsChanged();
  assert.equal(refreshed, 1);
});

test("account alert toggles rebuild an open menu immediately", () => {
  const applet = makeApplet();
  applet.accountAlertSettings = [
    {
      account: "alpha",
      "five-threshold": 20,
      "weekly-threshold": 20,
      warnings: true,
      errors: true,
    },
    {
      account: "beta",
      "five-threshold": 20,
      "weekly-threshold": 20,
      warnings: true,
      errors: true,
    },
  ];
  applet.settings = { setValue() {} };
  applet.menu = { isOpen: true };
  let rebuilds = 0;
  applet._buildUsageMenu = () => { rebuilds += 1; };
  applet._updateAccountAlertSetting("alpha", { warnings: false });
  assert.equal(rebuilds, 1);
  assert.equal(applet._alertSettings.alpha.warnings, false);
});

test("account controls preserve changes before backend settings synchronize", () => {
  const applet = makeApplet();
  applet.accountPanelSettings = [];
  applet.accountAlertSettings = [];
  applet.settings = { setValue() {} };
  applet._updatePanel = () => {};

  applet._updateAccountPanelSetting("alpha", { muted: true });
  applet._updateAccountAlertSetting("alpha", { warnings: false });

  assert.equal(applet.accountPanelSettings.length, 1);
  assert.equal(applet.accountPanelSettings[0].muted, true);
  assert.equal(applet.accountAlertSettings.length, 1);
  assert.equal(applet.accountAlertSettings[0].warnings, false);

  applet._backendAccounts = {
    alpha: { account: "alpha" },
    beta: { account: "beta" },
  };
  const panelRows = applet._mergedPanelRows(
    [applet._backendAccounts.alpha, applet._backendAccounts.beta],
    applet.accountPanelSettings
  );
  const alertRows = applet._mergedAlertRows(
    [applet._backendAccounts.alpha, applet._backendAccounts.beta],
    applet.accountAlertSettings
  );
  assert.equal(panelRows[0].muted, true);
  assert.equal(alertRows[0].warnings, false);
});

test("account synchronization refreshes cached values immediately", () => {
  const applet = makeApplet();
  applet._baseCommandArgv = () => ["codex-usage"];
  applet.settings = { setValue() {} };
  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [
      { id: "alpha", label: "Alpha", backend: "direct" },
      { id: "beta", label: "Beta", backend: "app-server" },
    ],
  }, null);
  applet._syncAccountSettings = () => {};
  applet._syncStyleRows = () => {};
  applet._addIdle = () => {};
  let refreshed = 0;
  applet._refreshFormattedSurfaces = () => { refreshed += 1; };
  applet._loadAccountBackends();
  assert.equal(refreshed, 1);
});

test("backend synchronization adds placeholders for accounts without cached values", () => {
  const applet = makeApplet();
  applet._usages = [];
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
    beta: { account: "beta", label: "Beta", backend: 1 },
  };
  assert.equal(applet._ensureBackendUsageRows(), true);
  assert.deepEqual(Array.from(applet._usages, (item) => item.account), ["alpha", "beta"]);
  assert.equal(applet._usages[0].status, "partial");
  assert.equal(applet._usages[0].stale, true);
  assert.equal(applet._usages[0].five_hour, null);
  assert.equal(applet._usages[1].backend_configured, "app-server");
});

test("backend synchronization removes cache rows for deleted accounts", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = { alpha: { account: "alpha", label: "Alpha", backend: 0 } };
  assert.equal(applet._ensureBackendUsageRows(), true);
  assert.deepEqual(Array.from(applet._usages, (item) => item.account), ["alpha"]);
});

test("backend synchronization cancels reactivation for removed accounts only", () => {
  const applet = makeApplet();
  let removedForced = 0;
  let retainedForced = 0;
  applet._reactivations = {
    removed: { process: { force_exit() { removedForced += 1; } }, timeoutId: 11, done: false },
    retained: { process: { force_exit() { retainedForced += 1; } }, timeoutId: 12, done: false },
  };
  applet._baseCommandArgv = () => ["codex-usage"];
  applet.settings = { setValue() {} };
  applet._syncAccountSettings = () => {};
  applet._syncStyleRows = () => {};
  applet._addIdle = () => {};
  applet._refreshFormattedSurfaces = () => {};
  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [{ id: "retained", label: "Retained", backend: "direct" }],
  }, null);

  applet._loadAccountBackends();
  assert.equal(removedForced, 1);
  assert.equal(retainedForced, 0);
  assert.equal(applet._reactivations.removed, undefined);
  assert.notEqual(applet._reactivations.retained, undefined);
});

test("account severity honors the threshold belonging to each limit", () => {
  const applet = makeApplet();
  applet._alertSettings = {
    alpha: {
      account: "alpha",
      "five-threshold": 50,
      "weekly-threshold": 5,
      warnings: true,
      errors: true,
    },
  };
  assert.equal(
    applet._usageSeverity({
      account: "alpha",
      status: "ok",
      stale: false,
      five_hour: { remaining: 30 },
      weekly: { remaining: 10 },
    }),
    "codex-usage-warning"
  );
});

test("refresh-on-open does not refresh when the menu is closed", () => {
  const applet = makeApplet();
  applet.refreshOnOpen = true;
  applet._usesAppletPolling = () => true;
  let refreshes = 0;
  applet._refreshFresh = () => { refreshes += 1; };
  applet.menu = {
    isOpen: false,
    toggle() { this.isOpen = !this.isOpen; },
  };
  applet.on_applet_clicked();
  assert.equal(refreshes, 1);
  applet.on_applet_clicked();
  assert.equal(refreshes, 1);
});

test("enabling automatic refresh rechecks the automatic poll owner", () => {
  const applet = makeApplet();
  applet.autoRefresh = false;
  applet.pollOwner = "auto";
  let scheduled = 0;
  let auxiliaryRefreshes = 0;
  applet._scheduleTimer = () => { scheduled += 1; };
  applet._refreshAuxiliaryState = () => { auxiliaryRefreshes += 1; };

  applet._onRefreshSettingsChanged();
  assert.equal(scheduled, 1);
  assert.equal(auxiliaryRefreshes, 0);

  applet.autoRefresh = true;
  applet._onRefreshSettingsChanged();
  assert.equal(scheduled, 2);
  assert.equal(auxiliaryRefreshes, 1);
});

test("health timeout clears the process even when force_exit fails", () => {
  let timeout = null;
  const process = { force_exit() { throw new Error("already exited"); } };
  const applet = makeApplet((runtime) => {
    runtime.timeoutAdd = (_ms, callback) => { timeout = callback; return 17; };
    runtime.launcherFactory = () => ({
      setenv() {},
      spawnv() { return process; },
    });
  });
  applet._readBoundedProcessOutput = () => {};
  applet._spawnHealthEvent([]);
  assert.equal(applet._healthProcess, process);
  assert.equal(typeof timeout, "function");
  timeout();
  assert.equal(applet._healthProcess, null);
});

test("stale process timeouts cannot clear newer request timers", () => {
  const cases = [
    {
      start(applet) {
        applet._spawnJsonArray(
          ["codex-usage", "once"],
          () => {},
          { subcommand: "once" }
        );
        applet._spawnJsonArray(
          ["codex-usage", "once"],
          () => {},
          { subcommand: "once" }
        );
      },
      property: "_timeoutId",
      generation: "_generation",
    },
    {
      start(applet) {
        applet._spawnAuxJson(["codex-usage", "health"], () => {});
        applet._spawnAuxJson(["codex-usage", "health"], () => {});
      },
      property: "_auxTimeoutId",
      generation: "_auxGeneration",
    },
    {
      start(applet) {
        applet._spawnHealthEvent(["codex-usage", "health"]);
        applet._cancelHealthProcess();
        applet._spawnHealthEvent(["codex-usage", "health"]);
      },
      property: "_healthTimeoutId",
      generation: "_healthGeneration",
    },
  ];

  for (const scenario of cases) {
    const timeouts = [];
    const processes = [];
    const applet = makeApplet((runtime) => {
      runtime.timeoutAdd = (_ms, callback) => {
        timeouts.push(callback);
        return timeouts.length;
      };
      runtime.launcherFactory = () => ({
        setenv() {},
        spawnv() {
          const process = { force_exit() {} };
          processes.push(process);
          return process;
        },
      });
    });
    applet._readBoundedProcessOutput = () => {};
    scenario.start(applet);
    assert.equal(timeouts.length, 2);
    const currentTimer = applet[scenario.property];
    assert.equal(currentTimer, 2);
    timeouts[0]();
    assert.equal(applet[scenario.property], currentTimer);
    assert.ok(applet[scenario.generation] > 1);
    assert.equal(processes.length, 2);
  }
});

test("stale periodic timer callbacks stop without touching newer timers", () => {
  const callbacks = [];
  const applet = makeApplet((runtime) => {
    runtime.timeoutAddSeconds = (_seconds, callback) => {
      callbacks.push(callback);
      return callbacks.length;
    };
  });
  applet.autoRefresh = true;
  applet._usesAppletPolling = () => true;
  let refreshes = 0;
  let displayUpdates = 0;
  applet._refreshFresh = () => { refreshes += 1; };
  applet._updatePanel = () => { displayUpdates += 1; };

  applet._scheduleTimer();
  applet._scheduleTimer();
  assert.equal(callbacks.length, 4);
  assert.equal(applet._displayTimerId, 3);
  assert.equal(applet._timerId, 4);

  assert.equal(callbacks[0](), false);
  assert.equal(callbacks[1](), false);
  assert.equal(refreshes, 0);
  assert.equal(displayUpdates, 0);
  assert.equal(applet._displayTimerId, 3);
  assert.equal(applet._timerId, 4);

  assert.equal(callbacks[2](), true);
  assert.equal(callbacks[3](), true);
  assert.equal(refreshes, 1);
  assert.equal(displayUpdates, 1);
});

test("safe mode invalidates already queued periodic timer callbacks", () => {
  const callbacks = [];
  const applet = makeApplet((runtime) => {
    runtime.timeoutAddSeconds = (_seconds, callback) => {
      callbacks.push(callback);
      return callbacks.length;
    };
  });
  applet.autoRefresh = true;
  applet._usesAppletPolling = () => true;
  let refreshes = 0;
  let displayUpdates = 0;
  applet._refreshFresh = () => { refreshes += 1; };
  applet._updatePanel = () => { displayUpdates += 1; };
  applet._scheduleTimer();
  applet._enterSafeMode("timer test");

  assert.equal(applet._displayTimerId, 0);
  assert.equal(applet._timerId, 0);
  assert.equal(callbacks[0](), false);
  assert.equal(callbacks[1](), false);
  assert.equal(refreshes, 0);
  assert.equal(displayUpdates, 0);
});

test("stale service checks cannot clear newer checks", () => {
  const callbacks = [];
  const applet = makeApplet((runtime) => {
    runtime.timeoutAdd = (_ms, callback) => {
      callbacks.push(callback);
      return callbacks.length;
    };
  });
  applet._enableBackgroundService = () => {};
  applet._cacheIsStale = () => true;
  applet._repairStaleService(() => {});
  applet._serviceRepairAt = 0;
  applet._repairStaleService(() => {});
  assert.equal(callbacks.length, 2);
  assert.equal(applet._staleCheckId, 2);
  assert.equal(callbacks[0](), false);
  assert.equal(applet._staleCheckId, 2);
});

test("stale service repair does not schedule after safe mode starts", () => {
  const applet = makeApplet();
  applet._enableBackgroundService = () => { applet._safeMode = true; };
  applet._repairStaleService(() => {});
  assert.equal(applet._staleCheckId, 0);
});

test("reactivation setup failure does not leave a phantom running account", () => {
  const applet = makeApplet();
  applet._buildUsageMenu = () => { throw new Error("menu unavailable"); };
  applet._spawnReactivation(
    { account: "alpha", label: "Alpha" },
    ["codex-usage", "reactivate", "alpha"]
  );
  assert.equal(applet._reactivations.alpha, undefined);
  assert.match(applet._reactivationErrors.alpha, /nicht angezeigt/);
});

test("startup failures and missing timeout sources terminate every spawned child process", () => {
  const invoke = [
    (applet) => {
      let callbacks = 0;
      applet._spawnJsonArray(
        ["codex-usage", "once"],
        () => { callbacks += 1; },
        { subcommand: "once" }
      );
      assert.equal(callbacks, 1);
      assert.equal(applet._process, null);
    },
    (applet) => {
      let callbacks = 0;
      applet._spawnAuxJson(["codex-usage", "health"], () => { callbacks += 1; });
      assert.equal(callbacks, 1);
      assert.equal(applet._auxProcess, null);
    },
    (applet) => {
      applet._spawnHealthEvent(["codex-usage", "health"]);
      assert.equal(applet._healthProcess, null);
    },
    (applet) => {
      applet._buildUsageMenu = () => {};
      applet._spawnReactivation(
        { account: "alpha", label: "Alpha" },
        ["codex-usage", "reactivate", "alpha"]
      );
      assert.equal(Object.keys(applet._reactivations).length, 0);
    },
  ];

  for (const timeoutFailure of [
    () => { throw new Error("timer setup failed"); },
    () => 0,
  ]) {
    for (const start of invoke) {
      let forced = 0;
      const applet = makeApplet((runtime) => {
        runtime.timeoutAdd = timeoutFailure;
        runtime.launcherFactory = () => ({
          setenv() {},
          spawnv() { return { force_exit() { forced += 1; } }; },
        });
      });
      start(applet);
      assert.equal(forced, 1);
    }
  }
});

test("refresh and display timers enter safe mode when a timeout source is unavailable", () => {
  for (const sequence of [[0], [31, 0]]) {
    let calls = 0;
    const applet = makeApplet((runtime) => {
      runtime.timeoutAddSeconds = () => sequence[calls++] || 0;
    });
    applet.autoRefresh = true;
    applet._scheduleTimer();
    assert.equal(applet._safeMode, true);
    assert.equal(applet._timerId, 0);
    assert.equal(applet._displayTimerId, 0);
  }
});

test("stale service repair enters safe mode when its timeout source is unavailable", () => {
  const applet = makeApplet((runtime) => {
    runtime.timeoutAdd = () => 0;
  });
  applet._enableBackgroundService = () => {};
  applet._repairStaleService(() => {});
  assert.equal(applet._safeMode, true);
  assert.equal(applet._staleCheckId, 0);
});

test("successful reactivation queues a refresh behind an active refresh", () => {
  const applet = makeApplet();
  applet._reactivationRefreshPending = true;
  applet._refreshing = false;
  let requests = 0;
  applet._updatePanel = () => {};
  applet._buildUsageMenu = () => {};
  applet._buildLoadingMenu = () => {};
  applet._applyPayload = () => {};
  applet._spawnUsageCommand = (_subcommand, callback) => {
    requests += 1;
    callback([], null);
  };
  applet._refreshFresh(false);
  assert.equal(requests, 2);
  assert.equal(applet._reactivationRefreshPending, false);
});

test("partial fresh payload preserves each missing window from stale cache", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    status: "partial",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: { remaining: 70 },
    weekly: null,
    stale: false,
  }]);
  assert.equal(merged[0].five_hour.remaining, 70);
  assert.equal(merged[0].weekly.remaining, 60);
  assert.equal(merged[0].stale, true);
  assert.equal(merged[0].values_captured_at, "2026-07-10T10:00:00.000Z");
});

test("fresh payload preserves configured accounts omitted from the response", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha" },
    beta: { account: "beta" },
  };
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    status: "ok",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: { remaining: 70 },
    weekly: { remaining: 50 },
    stale: false,
  }]);
  assert.deepEqual(Array.from(merged, (item) => item.account), ["alpha", "beta"]);
  assert.equal(merged[1].status, "partial");
  assert.equal(merged[1].stale, true);
  assert.equal(merged[1].values_captured_at, merged[1].captured_at);

  applet._backendAccounts = { alpha: { account: "alpha" } };
  const filtered = applet._mergeFreshPayload([{
    account: "alpha",
    status: "ok",
    captured_at: "2026-07-10T10:06:00.000Z",
    five_hour: { remaining: 69 },
    weekly: { remaining: 49 },
    stale: false,
  }]);
  assert.deepEqual(Array.from(filtered, (item) => item.account), ["alpha"]);
});

test("fresh payload rejects accounts absent from synchronized backend state", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = { alpha: { account: "alpha" } };
  const merged = applet._mergeFreshPayload([
    {
      account: "alpha",
      status: "ok",
      captured_at: "2026-07-10T10:05:00.000Z",
      five_hour: { remaining: 70 },
      weekly: { remaining: 50 },
      stale: false,
    },
    {
      account: "removed",
      status: "ok",
      captured_at: "2026-07-10T10:05:00.000Z",
      five_hour: { remaining: 10 },
      weekly: { remaining: 20 },
      stale: false,
    },
  ]);

  assert.deepEqual(Array.from(merged, (item) => item.account), ["alpha"]);
});

test("payload validation rejects duplicate account identities", () => {
  const applet = makeApplet();
  assert.equal(applet._validatePayload([{ account: "constructor" }])[0].account, "constructor");
  assert.throws(
    () => applet._validatePayload([{ account: "constructor" }, { account: "constructor" }]),
    /duplicate account id/
  );
});

test("backend account maps preserve prototype-like account ids", () => {
  const applet = makeApplet();
  applet._baseCommandArgv = () => ["codex-usage"];
  applet.settings = { setValue() {} };
  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [{ id: "__proto__", label: "Prototype", backend: "direct" }],
  }, null);
  applet._syncAccountSettings = () => {};
  applet._syncStyleRows = () => {};
  applet._addIdle = () => {};
  applet._refreshFormattedSurfaces = () => {};
  applet._loadAccountBackends();
  assert.equal(
    Object.prototype.hasOwnProperty.call(applet._backendAccounts, "__proto__"),
    true
  );
  assert.equal(applet._backendAccounts["__proto__"].label, "Prototype");
});

test("backend overview rejects duplicate account ids without replacing state", () => {
  const applet = makeApplet();
  applet._backendAccounts = { alpha: { account: "alpha", label: "Alpha", backend: 0 } };
  applet._backendRowsReady = true;
  applet.accountBackends = [{ account: "alpha", label: "Alpha", backend: 0 }];
  applet._baseCommandArgv = () => ["codex-usage"];
  let settingsWrites = 0;
  applet.settings = { setValue() { settingsWrites += 1; } };
  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [
      { id: "beta", label: "Beta", backend: "direct" },
      { id: "beta", label: "Beta duplicate", backend: "app-server" },
    ],
  }, null);
  applet._syncAccountSettings = () => { throw new Error("must not sync"); };
  applet._syncStyleRows = () => { throw new Error("must not sync"); };
  applet._loadAccountBackends();
  assert.deepEqual(applet._backendAccounts, {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
  });
  assert.deepEqual(applet.accountBackends, [
    { account: "alpha", label: "Alpha", backend: 0 },
  ]);
  assert.equal(settingsWrites, 0);
});

test("backend overview rejects invalid rows without replacing state", () => {
  const applet = makeApplet();
  applet._backendAccounts = { alpha: { account: "alpha", label: "Alpha", backend: 0 } };
  applet._backendRowsReady = true;
  applet.accountBackends = [{ account: "alpha", label: "Alpha", backend: 0 }];
  applet._baseCommandArgv = () => ["codex-usage"];
  applet.settings = { setValue() { throw new Error("must not write"); } };
  applet._syncAccountSettings = () => { throw new Error("must not sync"); };
  applet._syncStyleRows = () => { throw new Error("must not sync"); };
  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [{ id: "alpha", label: "Alpha", backend: "unsupported" }],
  }, null);

  assert.doesNotThrow(() => applet._loadAccountBackends());
  assert.deepEqual(applet._backendAccounts, {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
  });
  assert.deepEqual(applet.accountBackends, [
    { account: "alpha", label: "Alpha", backend: 0 },
  ]);
});

test("backend synchronization clears its guard after a settings exception", () => {
  const applet = makeApplet();
  applet._baseCommandArgv = () => ["codex-usage"];
  applet.settings = { setValue() {} };
  let idleCallback = null;
  applet._addIdle = (callback) => {
    idleCallback = callback;
    return 1;
  };
  applet._syncAccountSettings = () => { throw new Error("settings broken"); };
  applet._syncStyleRows = () => {};
  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [{ id: "alpha", label: "Alpha", backend: "direct" }],
  }, null);

  assert.throws(() => applet._loadAccountBackends(), /settings broken/);
  assert.equal(applet._syncingBackendRows, true);
  assert.equal(typeof idleCallback, "function");
  idleCallback();
  assert.equal(applet._syncingBackendRows, false);
});

test("backend synchronization releases its guard when idle scheduling fails", () => {
  const applet = makeApplet();
  applet._baseCommandArgv = () => ["codex-usage"];
  applet.settings = { setValue() {} };
  applet._addIdle = () => { throw new Error("idle broken"); };
  applet._syncAccountSettings = () => { throw new Error("settings broken"); };
  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [{ id: "alpha", label: "Alpha", backend: "direct" }],
  }, null);

  assert.throws(() => applet._loadAccountBackends(), /settings broken/);
  assert.equal(applet._syncingBackendRows, false);
});

test("backend synchronization releases its guard when idle scheduling returns zero", () => {
  const applet = makeApplet();
  applet._baseCommandArgv = () => ["codex-usage"];
  applet.settings = { setValue() {} };
  applet._addIdle = () => 0;
  applet._syncAccountSettings = () => { throw new Error("settings broken"); };
  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [{ id: "alpha", label: "Alpha", backend: "direct" }],
  }, null);

  assert.throws(() => applet._loadAccountBackends(), /settings broken/);
  assert.equal(applet._syncingBackendRows, false);
});

test("account and style synchronization release their guards when idle scheduling fails", () => {
  const applet = makeApplet();
  applet.settings = { setValue() {} };
  applet.accountPanelSettings = [];
  applet.accountAlertSettings = [];
  applet.accountPercentStyles = [];
  applet.accountDateStyles = [];
  applet.accountTimeStyles = [];
  applet.accountDurationStyles = [];
  applet.accountStyleTargets = [];
  applet._addIdle = () => { throw new Error("idle broken"); };
  const accounts = [{ account: "alpha" }];

  applet._syncAccountSettings(accounts);
  assert.equal(applet._syncingAccountSettings, false);
  applet._syncStyleRows(accounts);
  assert.equal(applet._syncingStyleRows, false);
});

test("account and style synchronization release their guards when idle scheduling returns zero", () => {
  const applet = makeApplet();
  applet.settings = { setValue() {} };
  applet.accountPanelSettings = [];
  applet.accountAlertSettings = [];
  applet.accountPercentStyles = [];
  applet.accountDateStyles = [];
  applet.accountTimeStyles = [];
  applet.accountDurationStyles = [];
  applet.accountStyleTargets = [];
  applet._addIdle = () => 0;
  const accounts = [{ account: "alpha" }];

  applet._syncAccountSettings(accounts);
  assert.equal(applet._syncingAccountSettings, false);
  applet._syncStyleRows(accounts);
  assert.equal(applet._syncingStyleRows, false);
});

test("stale synchronization idle callbacks cannot clear a newer guard", () => {
  const applet = makeApplet();
  const callbacks = [];
  applet._addIdle = (callback) => {
    callbacks.push(callback);
    return callbacks.length;
  };
  const guards = [
    "_syncingBackendRows",
    "_syncingAccountSettings",
    "_syncingStyleRows",
  ];

  for (const guard of guards) {
    applet[guard] = true;
    applet._deferGuardRelease(guard, "test guard cleanup");
    applet[guard] = true;
    applet._deferGuardRelease(guard, "test guard cleanup");
  }

  for (let index = 0; index < guards.length; index += 1) {
    callbacks[index * 2]();
    assert.equal(applet[guards[index]], true);
    callbacks[index * 2 + 1]();
    assert.equal(applet[guards[index]], false);
  }
});

test("backend setting changes reject duplicate account rows", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._syncingBackendRows = false;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
    beta: { account: "beta", label: "Beta", backend: 0 },
  };
  applet.accountBackends = [
    { account: "alpha", label: "Alpha", backend: 0 },
    { account: "alpha", label: "Alpha", backend: 1 },
  ];
  let reloads = 0;
  applet._loadAccountBackends = () => { reloads += 1; };
  applet._onAccountBackendsChanged();
  assert.equal(reloads, 1);
});

test("backend setting changes apply every changed account serially", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
    beta: { account: "beta", label: "Beta", backend: 0 },
  };
  applet.accountBackends = [
    { account: "alpha", label: "Alpha", backend: 1 },
    { account: "beta", label: "Beta", backend: 1 },
  ];
  applet._baseCommandArgv = () => ["codex-usage"];
  const calls = [];
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv[3]);
    callback({ ok: true, account: argv[3] }, null);
  };
  applet._refreshFresh = () => {};
  let reloads = 0;
  applet._loadAccountBackends = () => { reloads += 1; };
  applet._onAccountBackendsChanged();
  assert.deepEqual(calls, ["alpha", "beta"]);
  assert.equal(reloads, 1);
  assert.equal(applet._backendChangeCurrent, null);
  assert.equal(JSON.stringify(applet._backendChangeQueue), "[]");
});

test("backend queue advances when backend result handling throws", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
    beta: { account: "beta", label: "Beta", backend: 0 },
  };
  applet._baseCommandArgv = () => ["codex-usage"];
  const calls = [];
  const callbacks = [];
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv[3]);
    callbacks.push(callback);
  };
  applet._showCommandError = () => { throw new Error("menu failed"); };
  applet._loadAccountBackends = () => {};

  applet._backendChangeQueue = [
    { account: "alpha", backend: "app-server" },
    { account: "beta", backend: "app-server" },
  ];
  applet._drainBackendChanges();
  assert.deepEqual(calls, ["alpha"]);
  assert.throws(() => callbacks[0](null, "backend failed"), /menu failed/);
  assert.deepEqual(calls, ["alpha", "beta"]);
  assert.equal(applet._backendChangeCurrent.account, "beta");
  assert.equal(applet._backendChangeQueue.length, 0);
});

test("backend setting queue follows reverted rows while a command is running", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
    beta: { account: "beta", label: "Beta", backend: 0 },
  };
  applet.accountBackends = [
    { account: "alpha", label: "Alpha", backend: 1 },
    { account: "beta", label: "Beta", backend: 1 },
  ];
  applet._baseCommandArgv = () => ["codex-usage"];
  const calls = [];
  const callbacks = [];
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv[3]);
    callbacks.push(callback);
  };
  applet._refreshFresh = () => {};
  let reloads = 0;
  applet._loadAccountBackends = () => { reloads += 1; };
  applet._onAccountBackendsChanged();
  assert.deepEqual(calls, ["alpha"]);
  assert.equal(
    JSON.stringify(applet._backendChangeQueue),
    JSON.stringify([{ account: "beta", backend: "app-server" }])
  );

  applet.accountBackends = [
    { account: "alpha", label: "Alpha", backend: 1 },
    { account: "beta", label: "Beta", backend: 0 },
  ];
  applet._onAccountBackendsChanged();
  assert.equal(JSON.stringify(applet._backendChangeQueue), "[]");
  callbacks[0]({ ok: true, account: "alpha" }, null);
  assert.deepEqual(calls, ["alpha"]);
  assert.equal(reloads, 1);
});

test("backend queue waits for another auxiliary process instead of canceling it", () => {
  const applet = makeApplet();
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._backendChangeQueue = [{ account: "alpha", backend: "app-server" }];
  applet._auxProcess = {};
  let started = 0;
  applet._spawnAuxJson = () => { started += 1; };
  applet._drainBackendChanges();
  assert.equal(started, 0);
  assert.equal(applet._backendChangeCurrent, null);
  assert.equal(applet._backendChangeQueue.length, 1);

  applet._auxProcess = null;
  applet._drainBackendChanges();
  assert.equal(started, 1);
  assert.equal(applet._backendChangeCurrent.account, "alpha");
});

test("auxiliary requests defer while backend changes are active", () => {
  const applet = makeApplet();
  applet._backendChangeCurrent = { account: "alpha", backend: "app-server" };
  let called = 0;
  applet._spawnAuxJson(["codex-usage", "health"], () => { called += 1; });
  assert.equal(called, 0);
  assert.equal(applet._backendAuxQueue.length, 1);
  assert.equal(applet._backendAuxQueue[0].argv[1], "health");
});

test("deferred auxiliary requests coalesce and stay bounded", () => {
  const applet = makeApplet();
  applet._backendChangeCurrent = { account: "alpha", backend: "app-server" };
  applet._runSafely = (_context, callback) => callback();
  let overflowError = "";
  for (let index = 0; index < 8; index += 1) {
    applet._spawnAuxJson(
      ["codex-usage", "health", String(index)],
      () => {}
    );
  }
  applet._spawnAuxJson(
    ["codex-usage", "health", "duplicate"],
    () => {}
  );
  applet._spawnAuxJson(
    ["codex-usage", "health", "overflow"],
    (_payload, error) => { overflowError = error; }
  );
  assert.equal(applet._backendAuxQueue.length, 8);
  assert.match(overflowError, /wartende Hilfsanfragen/);

  const latestCallback = () => {};
  applet._spawnAuxJson(["codex-usage", "health", "0"], latestCallback);
  assert.equal(applet._backendAuxQueue.length, 8);
  assert.equal(applet._backendAuxQueue[0].callback, latestCallback);
});

test("old three-surface target rows migrate with a duration row", () => {
  const applet = makeApplet();
  const rows = applet._mergedTargetRows(
    [{ account: "alpha" }, { account: "beta" }],
    [
      { account: "alpha", element: 0, panel: true, hover: true, click: true },
      { account: "alpha", element: 1, panel: false, hover: false, click: true },
      { account: "alpha", element: 2, panel: false, hover: false, click: true },
    ]
  );
  assert.equal(rows.length, 8);
  assert.equal(rows[3].element, 3);
  assert.equal(rows[3].click, true);
  assert.equal(rows[3].panel, false);
});

test("automatic service activation finishes before the next auxiliary request", () => {
  const applet = makeApplet();
  const calls = [];
  applet.pollOwner = "auto";
  applet.autoRefresh = true;
  applet._serviceChecked = false;
  applet._systemdActive = false;
  applet._serviceAutoAttempted = false;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._scheduleTimer = () => {};
  applet._buildUsageMenu = () => {};
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv.slice(1).join(" "));
    if (argv.includes("status")) {
      callback({ installed: true, enabled: false, active: false }, null);
      return;
    }
    callback({ installed: true, enabled: true, active: true }, null);
  };
  applet._checkServiceStatus(() => calls.push("account overview"));
  assert.deepEqual(calls, [
    "service status --format json",
    "service enable --format json",
    "account overview",
  ]);
});

test("service status errors preserve a previously active systemd owner", () => {
  const applet = makeApplet();
  applet.pollOwner = "auto";
  applet.autoRefresh = true;
  applet._serviceChecked = true;
  applet._systemdActive = true;
  applet._serviceStatus = { enabled: true, active: true };
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._scheduleTimer = () => {};
  applet._cacheIsStale = () => false;
  applet._spawnAuxJson = (_argv, callback) => callback(null, "status unavailable");
  let continuationCalls = 0;

  applet._checkServiceStatus(() => { continuationCalls += 1; });
  assert.equal(applet._systemdActive, true);
  assert.deepEqual(applet._serviceStatus, { enabled: true, active: true });
  assert.equal(continuationCalls, 1);
});

test("a valid inactive service status retries after a previous activation", () => {
  const applet = makeApplet();
  applet.pollOwner = "auto";
  applet.autoRefresh = true;
  applet._serviceChecked = true;
  applet._systemdActive = true;
  applet._serviceAutoAttempted = true;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._scheduleTimer = () => {};
  applet._cacheIsStale = () => false;
  const calls = [];
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv.slice(1).join(" "));
    if (argv.includes("status")) {
      callback({ installed: true, enabled: false, active: false }, null);
    }
  };

  applet._checkServiceStatus(() => {});
  assert.deepEqual(calls, [
    "service status --format json",
    "service enable --format json",
  ]);
  assert.equal(applet._serviceAutoAttempted, true);
});

test("an active unmanaged timer is not treated as the poll owner", () => {
  const applet = makeApplet();
  applet.pollOwner = "auto";
  applet.autoRefresh = true;
  applet._serviceChecked = true;
  applet._systemdActive = true;
  applet._serviceAutoAttempted = false;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._scheduleTimer = () => {};
  applet._cacheIsStale = () => false;
  const calls = [];
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv.slice(1).join(" "));
    if (argv.includes("status")) {
      callback({ installed: false, enabled: true, active: true }, null);
    }
  };

  applet._checkServiceStatus(() => {});
  assert.equal(applet._systemdActive, false);
  assert.deepEqual(calls, [
    "service status --format json",
    "service enable --format json",
  ]);
});

test("malformed service status values do not become the poll owner", () => {
  const applet = makeApplet();
  applet.pollOwner = "auto";
  applet.autoRefresh = true;
  applet._serviceChecked = false;
  applet._systemdActive = false;
  applet._serviceAutoAttempted = false;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._scheduleTimer = () => {};
  applet._cacheIsStale = () => false;
  const calls = [];
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv.slice(1).join(" "));
    if (argv.includes("status")) {
      callback({ installed: "false", enabled: true, active: true }, null);
    }
  };

  applet._checkServiceStatus(() => {});
  assert.equal(applet._systemdActive, false);
  assert.deepEqual(calls, [
    "service status --format json",
    "service enable --format json",
  ]);
});

test("service enable requires strict ownership booleans", () => {
  const applet = makeApplet();
  applet.pollOwner = "auto";
  applet.autoRefresh = true;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._scheduleTimer = () => {};
  applet._buildUsageMenu = () => {};
  applet._refreshFresh = () => {};
  applet._spawnAuxJson = (_argv, callback) => {
    callback({ installed: true, enabled: "true", active: true }, null);
  };
  let error = "";
  applet._showCommandError = (value) => { error = value; };

  applet._enableBackgroundService();
  assert.equal(applet._systemdActive, false);
  assert.equal(applet._serviceAutoAttempted, false);
  assert.notEqual(error, "");
});

test("stale service repair finishes before the auxiliary continuation", () => {
  const applet = makeApplet();
  const calls = [];
  let enableCallback = null;
  applet.pollOwner = "auto";
  applet.autoRefresh = true;
  applet._serviceChecked = true;
  applet._systemdActive = true;
  applet._serviceRepairAt = 0;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._scheduleTimer = () => {};
  applet._buildUsageMenu = () => {};
  applet._cacheIsStale = () => true;
  applet._loadAccountBackends = () => calls.push("account overview");
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv.slice(1).join(" "));
    if (argv.includes("status")) {
      callback({ installed: true, enabled: true, active: true }, null);
    } else if (argv.includes("enable")) {
      enableCallback = callback;
    }
  };

  applet._checkServiceStatus(applet._loadAccountBackends);
  assert.deepEqual(calls, [
    "service status --format json",
    "service enable --format json",
  ]);
  assert.equal(enableCallback !== null, true);

  enableCallback({ installed: true, enabled: true, active: true }, null);
  assert.deepEqual(calls, [
    "service status --format json",
    "service enable --format json",
    "account overview",
  ]);
});

test("service argv errors preserve a previously active systemd owner", () => {
  const applet = makeApplet();
  applet._serviceChecked = true;
  applet._systemdActive = true;
  applet._serviceStatus = { enabled: true, active: true };
  applet._baseCommandArgv = () => { throw new Error("command unavailable"); };
  let continuationCalls = 0;

  applet._checkServiceStatus(() => { continuationCalls += 1; });
  assert.equal(applet._systemdActive, true);
  assert.deepEqual(applet._serviceStatus, { enabled: true, active: true });
  assert.equal(continuationCalls, 1);
});

test("cancelling service enable allows automatic activation to retry", () => {
  let forced = 0;
  const process = { force_exit() { forced += 1; } };
  const applet = makeApplet((runtime) => {
    runtime.launcherFactory = () => ({
      setenv() {},
      spawnv() { return process; },
    });
  });
  applet._readBoundedProcessOutput = () => {};
  applet._serviceAutoAttempted = true;
  applet._systemdActive = false;
  applet._spawnAuxJson(
    ["codex-usage", "--config", "service", "service", "enable", "--format", "json"],
    () => {}
  );
  assert.equal(applet._auxCommand, "service-enable");
  applet._cancelAuxProcess();
  assert.equal(forced, 1);
  assert.equal(applet._auxCommand, "");
  assert.equal(applet._serviceAutoAttempted, false);
});

test("service enable argv errors release the automatic activation attempt", () => {
  const applet = makeApplet();
  applet._serviceAutoAttempted = true;
  applet._baseCommandArgv = () => { throw new Error("command unavailable"); };
  applet._showCommandError = () => {};
  applet._enableBackgroundService();
  assert.equal(applet._serviceAutoAttempted, false);
});

test("service error display failures do not block continuation", () => {
  const applet = makeApplet();
  applet.pollOwner = "auto";
  applet.autoRefresh = true;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._showCommandError = () => { throw new Error("menu failed"); };
  applet._refreshFresh = () => {};
  applet._spawnAuxJson = (_argv, callback) => callback(null, "service failed");
  let continued = 0;
  assert.doesNotThrow(() => applet._enableBackgroundService(() => { continued += 1; }));
  assert.equal(continued, 1);
  assert.equal(applet._serviceAutoAttempted, false);
});

test("cleanup is idempotent across 100 applet removals", () => {
  for (let index = 0; index < 100; index += 1) {
    const applet = makeApplet();
    applet.menu = { destroy() {} };
    applet.settings = { finalize() {} };
    applet._displayTimerId = 77;
    applet._sources._displayTimerId = 77;
    applet._backendChangeQueue = [{ account: "alpha", backend: "direct" }];
    applet._backendChangeCurrent = { account: "beta", backend: "app-server" };
    applet._backendAuxQueue = [{ argv: ["codex-usage", "health"], callback() {} }];
    assert.doesNotThrow(() => applet.on_applet_removed_from_panel());
    assert.equal(applet._removed, true);
    assert.equal(applet._displayTimerId, 0);
    assert.equal(JSON.stringify(applet._backendChangeQueue), "[]");
    assert.equal(applet._backendChangeCurrent, null);
    assert.equal(JSON.stringify(applet._backendAuxQueue), "[]");
    assert.equal(applet.menu, null);
  }
});
