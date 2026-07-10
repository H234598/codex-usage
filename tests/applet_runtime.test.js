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
    timeoutAdd: () => 2,
    launcherFactory: () => { throw new Error("launcher not configured"); },
  };
  const mainloop = {
    idle_add: () => 1,
    source_remove: () => {},
    timeout_add: (...args) => runtime.timeoutAdd(...args),
    timeout_add_seconds: () => 3,
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
  applet._reactivationRefreshPending = false;
  applet._process = null;
  applet._auxProcess = null;
  applet._healthProcess = null;
  applet._healthGeneration = 0;
  applet._timeoutId = 0;
  applet._auxTimeoutId = 0;
  applet._healthTimeoutId = 0;
  applet._timerId = 0;
  applet._displayTimerId = 0;
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

test("cleanup is idempotent across 100 applet removals", () => {
  for (let index = 0; index < 100; index += 1) {
    const applet = makeApplet();
    applet.menu = { destroy() {} };
    applet.settings = { finalize() {} };
    applet._displayTimerId = 77;
    applet._sources._displayTimerId = 77;
    assert.doesNotThrow(() => applet.on_applet_removed_from_panel());
    assert.equal(applet._removed, true);
    assert.equal(applet._displayTimerId, 0);
    assert.equal(applet.menu, null);
  }
});
