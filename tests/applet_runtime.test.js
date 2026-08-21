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
    subprocessFactory: () => ({}),
    appInfoFactory: () => {},
    fileUriCalls: 0,
  };
  const mainloop = {
    idle_add: (...args) => runtime.idleAdd(...args),
    source_remove: () => {},
    timeout_add: (...args) => runtime.timeoutAdd(...args),
    timeout_add_seconds: (...args) => runtime.timeoutAddSeconds(...args),
  };
  const gio = {
    SubprocessFlags: { STDOUT_PIPE: 1, STDERR_PIPE: 2 },
    file_new_for_path: (localPath) => ({
      get_uri: () => `file://${encodeURI(localPath)}`,
    }),
    file_new_for_uri: (uri) => {
      runtime.fileUriCalls += 1;
      return {
        get_path: () => {
          const raw = uri.replace(/^file:\/\//i, "");
          if (raw.startsWith("/")) {
            return decodeURIComponent(raw);
          }
          const separator = raw.indexOf("/");
          return separator > 0 ? decodeURIComponent(raw.slice(separator)) : null;
        },
      };
    },
    SubprocessLauncher: {
      new: (...args) => runtime.launcherFactory(...args),
    },
    SubprocessFlags: { NONE: 0, STDOUT_SILENCE: 4, STDERR_SILENCE: 8 },
    Subprocess: {
      new: (...args) => runtime.subprocessFactory(...args),
    },
    AppInfo: {
      launch_default_for_uri: (...args) => runtime.appInfoFactory(...args),
    },
  };
  class PopupItem {
    constructor(text) {
      this.actor = { add_style_class_name() {} };
      this.label = { text: text || "", clutter_text: { set_markup() {} } };
      this._signals = {};
      this.menu = {
        items: [],
        addMenuItem: (item) => this.menu.items.push(item),
        addAction: (label, callback) => {
          const item = new PopupItem(label);
          item.connect("activate", callback);
          this.menu.items.push(item);
          return item;
        },
      };
    }
    connect(signal, callback) {
      this._signals[signal] = callback;
      return signal;
    }
    emit(signal, ...args) {
      if (typeof this._signals[signal] === "function") {
        return this._signals[signal](this, ...args);
      }
      return undefined;
    }
    setSensitive(value) {
      this.sensitive = value;
    }
  }
  class PopupSeparatorItem extends PopupItem {
    constructor() {
      super();
      this.isSeparator = true;
    }
  }
  class PopupSwitchItem extends PopupItem {
    constructor(text, state) {
      super(text);
      this.state = state === true;
    }
  }
  const sandbox = {
    imports: {
      byteArray: { toString: (value) => Buffer.from(value).toString("utf8") },
    gi: {
      Gio: gio,
      GLib: {},
      St: {
        ClipboardType: { CLIPBOARD: 1 },
        IconType: { SYMBOLIC: 1 },
        Clipboard: { get_default: () => runtime.clipboard || null },
      },
    },
      lang: { bind: (object, callback) => callback.bind(object) },
      mainloop,
      ui: {
        applet: { TextIconApplet: function TextIconApplet() {} },
        main: {
          notify: (...args) => {
            if (runtime.onNotify) {
              runtime.onNotify(...args);
            }
          },
        },
        popupMenu: {
          PopupMenuItem: PopupItem,
          PopupIconMenuItem: PopupItem,
          PopupSeparatorMenuItem: PopupSeparatorItem,
          PopupSwitchMenuItem: PopupSwitchItem,
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
  applet._accountChangeQueue = [];
  applet._accountChangeCurrent = null;
  applet._accountChangePendingRows = null;
  applet._accountDeleteWaitingForProfileJob = {};
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
  applet._deviceLoginActive = {};
  applet._deviceLoginJobs = {};
  applet._deviceLoginErrors = {};
  applet._deviceLoginEvents = {};
  applet._deviceLoginLiveText = {};
  applet._deviceLoginLiveAccount = "";
  applet._profileJobResumeQueue = [];
  applet._profileJobPollingAccount = "";
  applet._deviceLoginPollId = 0;
  applet._deviceLoginPollGeneration = 0;
  applet._profilePendingAccounts = {};
  applet._warningState = {};
  applet._errorState = {};
  applet.errorNotificationState = "{}";
  applet.settings = { setValue() {} };
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
  applet._lastCacheSyncAt = 0;
  applet._lastGoodPanel = { plain: "--", markup: "--" };
  applet._lastGoodTooltip = "";
  applet._internalFailures = [];
  applet._refreshFailures = 0;
  applet._circuitOpenUntil = 0;
  applet._safeMode = false;
  applet._safeModeReason = "";
  applet._panelSettings = {};
  applet._consumptionSettings = {};
  applet._resetSettings = {};
  applet._consumptionQueue = [];
  applet._consumptionCurrent = null;
  applet._consumptionGeneration = 0;
  applet._alertSettings = {};
  applet._percentStyles = {};
  applet._dateStyles = {};
  applet._timeStyles = {};
  applet._durationStyles = {};
  applet._styleTargets = {};
  applet._routingPolicy = null;
  applet._routingDecisions = {};
  applet._routingSettingsReady = false;
  applet._syncingRoutingSettings = false;
  applet._routingPolicyApplying = false;
  applet.panelHeight = 24;
  applet.refreshOnOpen = true;
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
      backend_configured: "direct",
      backend_used: "direct",
      status: "ok",
      five_hour: { remaining: 80, reset_at: "2026-07-10T15:00:00+00:00" },
      weekly: { remaining: 60, reset_at: "2026-07-11T15:00:00+00:00" },
    },
    {
      account: "beta",
      label: "Beta",
      backend_configured: "direct",
      backend_used: "direct",
      status: "ok",
      five_hour: { remaining: 40, reset_at: "2026-07-10T16:00:00+00:00" },
      weekly: { remaining: 90, reset_at: "2026-07-12T16:00:00+00:00" },
    },
  ];
  applet._panelSettings = {
    alpha: { account: "alpha", tag: "A", order: 2, muted: false, slot1: 1, slot2: 2 },
    beta: { account: "beta", tag: "B", order: 1, muted: true, slot1: 3, slot2: 3 },
  };
  applet._displaySettings = {
    alpha: { account: "alpha", tag: "A", panel: 2, hover: 1, click: 1 },
    beta: { account: "beta", tag: "B", panel: 2, hover: 1, click: 1 },
  };
  applet.accountDisplaySettings = [
    applet._displaySettings.alpha,
    applet._displaySettings.beta,
  ];
  applet._backendAccounts = { alpha: {}, beta: {} };
  return applet;
}

function makeAccountSettingsApplet() {
  const applet = makeApplet();
  applet._baseCommandArgv = () => ["codex-usage"];
  applet.settings = { setValue() {} };
  applet._cancelRemovedReactivations = () => {};
  applet._ensureBackendUsageRows = () => false;
  applet._syncAccountSettings = () => {};
  applet._syncStyleRows = () => {};
  applet._loadRoutingState = () => {};
  applet._refreshFormattedSurfaces = () => {};
  applet._refreshFresh = () => {};
  applet._accountChangeQueue = [];
  applet._accountChangeCurrent = null;
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: {
      account: "alpha",
      label: "Alpha",
      "auth-json": "",
      "profile-dir": "/tmp/alpha",
      browser: 0,
      "reactivation-browser": 0,
      series: "",
      "series-active": false,
      backend: 0,
    },
  };
  return applet;
}

test("custom settings read current values and react to changed signals", () => {
  const applet = makeApplet();
  let value = [{ account: "alpha" }];
  const connections = [];
  const callbackValues = [];
  applet.settings = {
    getValue: (key) => {
      assert.equal(key, "account-backends");
      return value;
    },
    connect: (signal, callback) => {
      connections.push({ signal, callback });
      return connections.length;
    },
  };

  applet._bindCustomSetting(
    "account-backends",
    "accountBackends",
    (nextValue) => callbackValues.push(nextValue)
  );

  assert.deepEqual(applet.accountBackends, [{ account: "alpha" }]);
  assert.equal(callbackValues.length, 0);
  assert.equal(connections.length, 1);
  assert.equal(connections[0].signal, "changed::account-backends");

  value = [{ account: "beta" }];
  connections[0].callback("account-backends", [{ account: "alpha" }], value);

  assert.deepEqual(applet.accountBackends, [{ account: "beta" }]);
  assert.deepEqual(callbackValues, [[{ account: "beta" }]]);
});

function usageWithoutSparkLimit(account) {
  return {
    account,
    label: account,
    status: "ok",
    main: { available: true, windows: [] },
    models: {},
  };
}

function usageWithSparkWindows(account, values) {
  return {
    account,
    label: account,
    status: "ok",
    main: { available: true, windows: [] },
    models: {
      "gpt-5.3-codex-spark": {
        available: true,
        windows: [
          { name: "5h", duration_seconds: 18000, remaining: values.five },
          { name: "weekly", duration_seconds: 604800, remaining: values.weekly },
        ],
      },
    },
  };
}

test("device login live parser exposes only bounded URL and code events", () => {
  const applet = makeApplet();

  assert.equal(JSON.stringify(applet._deviceLoginEventsFromText(
    "Visit https://auth.example/device and enter device code: ABCD-1234. secret=hidden"
  )), JSON.stringify([
    { kind: "url", value: "https://auth.example/device" },
    { kind: "code", value: "ABCD-1234" },
  ]));
});

test("device login live parser defers a URL split across chunks", () => {
  const applet = makeApplet();
  applet._buildUsageMenu = () => {};
  applet._deviceLoginLiveAccount = "alpha";
  applet._deviceLoginLiveText.alpha = { stdout: "", stderr: "" };

  applet._recordDeviceLoginChunk(
    "stdout",
    "Open https://auth.example/dev",
  );

  assert.equal(applet._deviceLoginEvents.alpha, undefined);

  applet._recordDeviceLoginChunk("stdout", "ice\n");

  assert.equal(JSON.stringify(applet._deviceLoginEvents.alpha), JSON.stringify([
    { kind: "url", value: "https://auth.example/device" },
  ]));
});

test("device login live parser defers URL punctuation at a chunk boundary", () => {
  const applet = makeApplet();
  applet._buildUsageMenu = () => {};
  applet._deviceLoginLiveAccount = "alpha";
  applet._deviceLoginLiveText.alpha = { stdout: "", stderr: "" };

  applet._recordDeviceLoginChunk(
    "stdout",
    "Open https://auth.example/device.",
  );

  assert.equal(applet._deviceLoginEvents.alpha, undefined);

  applet._recordDeviceLoginChunk("stdout", "well-known\n");

  assert.equal(JSON.stringify(applet._deviceLoginEvents.alpha), JSON.stringify([
    { kind: "url", value: "https://auth.example/device.well-known" },
  ]));
});

test("device login live parser emits a delimited URL before EOF", () => {
  const applet = makeApplet();
  applet._buildUsageMenu = () => {};
  applet._deviceLoginLiveAccount = "alpha";
  applet._deviceLoginLiveText.alpha = { stdout: "", stderr: "" };

  applet._recordDeviceLoginChunk(
    "stdout",
    "Open https://auth.example/device. Continue\n",
  );

  assert.equal(JSON.stringify(applet._deviceLoginEvents.alpha), JSON.stringify([
    { kind: "url", value: "https://auth.example/device" },
  ]));
});

test("device login live parser defers a code split across chunks", () => {
  const applet = makeApplet();
  applet._buildUsageMenu = () => {};
  applet._deviceLoginLiveAccount = "alpha";
  applet._deviceLoginLiveText.alpha = { stdout: "", stderr: "" };

  applet._recordDeviceLoginChunk("stdout", "Enter device code: ABCD");

  assert.equal(applet._deviceLoginEvents.alpha, undefined);

  applet._recordDeviceLoginChunk("stdout", "-1234\n");

  assert.equal(JSON.stringify(applet._deviceLoginEvents.alpha), JSON.stringify([
    { kind: "code", value: "ABCD-1234" },
  ]));
});

test("device login live parser never joins tokens across output streams", () => {
  const applet = makeApplet();
  applet._buildUsageMenu = () => {};
  applet._deviceLoginLiveAccount = "alpha";
  applet._deviceLoginLiveText.alpha = { stdout: "", stderr: "" };

  applet._recordDeviceLoginChunk("stderr", "Enter device code: ABCD");
  applet._recordDeviceLoginChunk("stdout", "-1234\n");

  assert.equal(applet._deviceLoginEvents.alpha, undefined);
});

test("device login parser accepts the current ANSI Codex prompt", () => {
  const applet = makeApplet();
  const output =
    "1. Open this link in your browser and sign in to your account\n" +
    "   \u001b[34mhttps://auth.openai.com/codex/device\u001b[0m\n" +
    "2. Enter this one-time code \u001b[2m(expires in 15 minutes)\u001b[0m\n" +
    "   \u001b[34mABCD-1234\u001b[0m\n";

  assert.equal(JSON.stringify(applet._deviceLoginEventsFromText(output)),
    JSON.stringify([
      { kind: "url", value: "https://auth.openai.com/codex/device" },
      { kind: "code", value: "ABCD-1234" },
    ]));
});

test("device login parser stops URLs before control characters", () => {
  const applet = makeApplet();
  const controls = ["\u0007", "\u001b", "\u007f", "\u0085", "\u009f"];

  for (const control of controls) {
    assert.equal(JSON.stringify(applet._deviceLoginEventsFromText(
      `Open https://auth.openai.com/codex/device${control}hidden\n`
    )), JSON.stringify([
      { kind: "url", value: "https://auth.openai.com/codex/device" },
    ]));
  }
});

test("device login parser rejects overlong URLs without a prefix match", () => {
  const applet = makeApplet();

  assert.equal(JSON.stringify(applet._deviceLoginEventsFromText(
    `Open https://${"a".repeat(481)}\n`
  )), "[]");
});

test("device login parser deduplicates before the eight event cap", () => {
  const applet = makeApplet();
  const url = "https://auth.openai.com/codex/device";
  const repeated = `${Array(9).fill(url).join(" ")} device code: ABCD-1234`;
  const unique = [
    "https://auth.example/device/0",
    "https://auth.example/device/1",
    "https://auth.example/device/2",
    "https://auth.example/device/3",
    "https://auth.example/device/4",
    "https://auth.example/device/5",
    "https://auth.example/device/6",
    "https://auth.example/device/7",
    "https://auth.example/device/8",
  ].join(" ");

  assert.equal(JSON.stringify(applet._deviceLoginEventsFromText(repeated)), JSON.stringify([
    { kind: "url", value: url },
    { kind: "code", value: "ABCD-1234" },
  ]));
  assert.equal(JSON.stringify(applet._deviceLoginEventsFromText(unique)), JSON.stringify([
    { kind: "url", value: "https://auth.example/device/0" },
    { kind: "url", value: "https://auth.example/device/1" },
    { kind: "url", value: "https://auth.example/device/2" },
    { kind: "url", value: "https://auth.example/device/3" },
    { kind: "url", value: "https://auth.example/device/4" },
    { kind: "url", value: "https://auth.example/device/5" },
    { kind: "url", value: "https://auth.example/device/6" },
    { kind: "url", value: "https://auth.example/device/7" },
  ]));
});

test("device login event boundary rejects malformed values before normalization", () => {
  const applet = makeApplet();
  const events = [
    { kind: "url", value: "https://auth.openai.com/codex/device" },
    { kind: "url", value: "https://auth.openai.com/codex/device\u0007hidden" },
    { kind: "url", value: "http://auth.openai.com/codex/device" },
    { kind: "url", value: `https://${"a".repeat(481)}` },
    { kind: "code", value: "ABCD-1234" },
    { kind: "code", value: "ABCD\n1234" },
    { kind: "code", value: "A".repeat(129) },
  ];

  assert.equal(JSON.stringify(applet._safeDeviceLoginEvents(events)), JSON.stringify([
    { kind: "url", value: "https://auth.openai.com/codex/device" },
    { kind: "code", value: "ABCD-1234" },
  ]));
});

test("device login parser ignores generic and malformed diagnostic codes", () => {
  const applet = makeApplet();
  const output =
    "error code: E1234\n" +
    "exit code: EXIT-7\n" +
    "code: ABCD-1234\n" +
    "kode: WXYZ-9876\n" +
    "device\ncode: SPLIT-1\n" +
    "one-time code\n\nABCD-1234\n" +
    `device code: ${"A".repeat(129)}\n`;

  assert.equal(JSON.stringify(applet._deviceLoginEventsFromText(output)), "[]");
});

test("device login event copy writes only the ephemeral event value", () => {
  const copied = [];
  const applet = makeApplet((runtime) => {
    runtime.clipboard = {
      set_text(type, value) {
        copied.push({ type, value });
      },
    };
  });

  applet._copyDeviceLoginEvent({ kind: "code", value: "ABCD-1234" });

  assert.deepEqual(copied, [{ type: 1, value: "ABCD-1234" }]);
});

test("device login clears URL and code events after successful finalize", () => {
  const applet = makeApplet();
  let callback;
  let timeoutMs;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._buildUsageMenu = () => {};
  applet._refreshFresh = () => {};
  applet._deviceLoginEvents.alpha = [{ kind: "code", value: "ABCD-1234" }];
  applet._spawnAuxJson = (_argv, handler, _backendRequest, selectedTimeout) => {
    callback = handler;
    timeoutMs = selectedTimeout;
  };

  applet._startDeviceLogin({ account: "alpha" });
  callback({
    account: "alpha",
    ok: true,
    events: [{ kind: "code", value: "ABCD-1234" }],
  }, null);

  assert.equal(applet._deviceLoginEvents.alpha, undefined);
  assert.equal(timeoutMs, 910000);
});

test("persistent profile job resumes and exposes polled events", () => {
  const applet = makeApplet();
  const calls = [];
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._buildUsageMenu = () => {};
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv);
    if (calls.length === 1) {
      callback({
        ok: true,
        jobs: [{
          account: "alpha",
          job_id: "job-1234567890abcdef1234567890abcdef",
          status: "running",
        }],
      }, null);
      return;
    }
    callback({
      account: "alpha",
      job_id: "job-1234567890abcdef1234567890abcdef",
      ok: true,
      status: "running",
      events: [{ kind: "code", value: "ABCD-1234" }],
    }, null);
  };

  applet._loadProfileJobs();

  assert.deepEqual(calls, [
    ["codex-usage", "profile", "jobs", "--json"],
    ["codex-usage", "profile", "job-status", "job-1234567890abcdef1234567890abcdef", "--json"],
  ]);
  assert.equal(applet._deviceLoginActive.alpha, true);
  assert.equal(applet._deviceLoginJobs.alpha, "job-1234567890abcdef1234567890abcdef");
  assert.deepEqual(
    JSON.parse(JSON.stringify(applet._deviceLoginEvents.alpha)),
    [{ kind: "code", value: "ABCD-1234" }]
  );
});

test("persistent profile job resume drains every active job", () => {
  const applet = makeApplet();
  const calls = [];
  const jobs = [
    {
      account: "alpha",
      job_id: "job-1234567890abcdef1234567890abcdef",
      status: "running",
    },
    {
      account: "beta",
      job_id: "job-abcdef1234567890abcdef1234567890",
      status: "queued",
    },
  ];
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._buildUsageMenu = () => {};
  applet._refreshFresh = () => {};
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv);
    if (calls.length === 1) {
      callback({ ok: true, jobs }, null);
      return;
    }
    const jobId = argv[3];
    const account = jobId === jobs[0].job_id ? "alpha" : "beta";
    callback({
      account,
      job_id: jobId,
      ok: true,
      status: account === "alpha" ? "failed" : "completed",
      error: account === "alpha" ? "synthetic failure" : undefined,
    }, null);
  };

  applet._loadProfileJobs();

  assert.deepEqual(calls, [
    ["codex-usage", "profile", "jobs", "--json"],
    ["codex-usage", "profile", "job-status", jobs[0].job_id, "--json"],
    ["codex-usage", "profile", "job-status", jobs[1].job_id, "--json"],
  ]);
  assert.equal(applet._profilePendingAccounts.alpha, undefined);
  assert.equal(applet._profilePendingAccounts.beta, undefined);
  assert.equal(applet._deviceLoginJobs.alpha, undefined);
  assert.equal(applet._deviceLoginJobs.beta, undefined);
  assert.equal(applet._deviceLoginErrors.alpha, "synthetic failure");
});

test("persistent profile job cancellation uses job contract", () => {
  const applet = makeApplet();
  let command;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._buildUsageMenu = () => {};
  applet._spawnAuxJson = (argv, callback) => {
    command = argv;
    callback({
      account: "alpha",
      job_id: "job-1234567890abcdef1234567890abcdef",
      ok: false,
      status: "cancelled",
    }, null);
  };
  applet._deviceLoginActive.alpha = true;
  applet._deviceLoginJobs.alpha = "job-1234567890abcdef1234567890abcdef";
  applet._deviceLoginEvents.alpha = [{ kind: "code", value: "ABCD-1234" }];

  applet._cancelDeviceLogin("alpha");

  assert.deepEqual(command, [
    "codex-usage", "profile", "cancel", "job-1234567890abcdef1234567890abcdef", "--json",
  ]);
  assert.equal(applet._deviceLoginActive.alpha, undefined);
  assert.equal(applet._deviceLoginJobs.alpha, undefined);
  assert.equal(applet._deviceLoginEvents.alpha, undefined);
  assert.equal(applet._deviceLoginErrors.alpha, "Device-Login abgebrochen");
});

test("completed persistent profile job clears pending account and refreshes usage", () => {
  const applet = makeApplet();
  const jobId = "job-1234567890abcdef1234567890abcdef";
  let refreshes = 0;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._buildUsageMenu = () => {};
  applet._refreshFresh = () => { refreshes += 1; };
  applet._deviceLoginJobs.alpha = jobId;
  applet._deviceLoginActive.alpha = true;
  applet._profilePendingAccounts.alpha = true;
  applet._spawnAuxJson = (_argv, callback) => callback({
    account: "alpha",
    job_id: jobId,
    ok: true,
    status: "completed",
  }, null);

  applet._pollProfileJob("alpha");

  assert.equal(refreshes, 1);
  assert.equal(applet._profilePendingAccounts.alpha, undefined);
  assert.equal(applet._deviceLoginJobs.alpha, undefined);
});

test("profile job resume starts after account overview completes", () => {
  const applet = makeAccountSettingsApplet();
  const calls = [];
  applet._profileJobsResumeRequested = true;
  applet._loadProfileJobs = () => calls.push("jobs");
  applet._spawnAuxJson = (_argv, callback) => {
    calls.push("accounts");
    callback({ accounts: [] }, null);
  };

  applet._loadAccountBackends();

  assert.deepEqual(calls, ["accounts", "jobs"]);
  assert.equal(applet._profileJobsResumeRequested, false);
});

test("device login cancel clears active process and drains queued work", () => {
  const applet = makeApplet();
  let cancelled = 0;
  let rebuilt = 0;
  let drained = 0;
  applet._deviceLoginActive = { alpha: true };
  applet._auxCommand = "device-login";
  applet._deviceLoginLiveAccount = "alpha";
  applet._cancelAuxProcess = () => { cancelled += 1; };
  applet._buildUsageMenu = () => { rebuilt += 1; };
  applet._drainBackendChanges = () => { drained += 1; };
  applet._drainAccountChanges = () => { drained += 1; };
  applet._drainDeferredAuxRequests = () => { drained += 1; };
  applet._drainConsumptionRequests = () => { drained += 1; };

  applet._cancelDeviceLogin("alpha");

  assert.equal(cancelled, 1);
  assert.equal(applet._deviceLoginActive.alpha, undefined);
  assert.equal(applet._deviceLoginErrors.alpha, "Device-Login abgebrochen");
  assert.equal(rebuilt, 1);
  assert.equal(drained, 4);
});

test("live device login cleanup preserves persistent profile job state", () => {
  const applet = makeApplet();
  applet._deviceLoginActive = { alpha: true, beta: true };
  applet._deviceLoginJobs = { beta: "job-beta" };
  applet._deviceLoginLiveAccount = "alpha";
  applet._auxCommand = "device-login";

  applet._cancelAuxProcess();

  assert.deepEqual(applet._deviceLoginActive, { beta: true });
  assert.deepEqual(applet._deviceLoginJobs, { beta: "job-beta" });
  assert.equal(applet._deviceLoginLiveAccount, "");
});

test("device login cancel clears ephemeral URL and code events", () => {
  const applet = makeApplet();
  applet._deviceLoginActive = { alpha: true };
  applet._deviceLoginEvents.alpha = [{ kind: "code", value: "ABCD-1234" }];
  applet._auxCommand = "device-login";
  applet._deviceLoginLiveAccount = "alpha";
  applet._cancelAuxProcess = () => {};
  applet._buildUsageMenu = () => {};
  applet._drainBackendChanges = () => {};
  applet._drainAccountChanges = () => {};
  applet._drainDeferredAuxRequests = () => {};
  applet._drainConsumptionRequests = () => {};

  applet._cancelDeviceLogin("alpha");

  assert.equal(applet._deviceLoginEvents.alpha, undefined);
});

test("queued device login cancellation removes only its deferred request", () => {
  const applet = makeApplet();
  let cancelled = 0;
  let drained = 0;
  applet._deviceLoginActive = { alpha: true };
  applet._auxCommand = "service-enable";
  applet._backendAuxQueue = [
    { argv: ["codex-usage", "profile", "device-login", "--account", "alpha"] },
    { argv: ["codex-usage", "health"] },
  ];
  applet._cancelAuxProcess = () => { cancelled += 1; };
  applet._buildUsageMenu = () => {};
  applet._drainBackendChanges = () => { drained += 1; };
  applet._drainAccountChanges = () => { drained += 1; };
  applet._drainDeferredAuxRequests = () => { drained += 1; };
  applet._drainConsumptionRequests = () => { drained += 1; };

  applet._cancelDeviceLogin("alpha");

  assert.equal(cancelled, 0);
  assert.equal(applet._backendAuxQueue.length, 1);
  assert.deepEqual(applet._backendAuxQueue[0].argv, ["codex-usage", "health"]);
  assert.equal(applet._deviceLoginActive.alpha, undefined);
  assert.equal(applet._deviceLoginErrors.alpha, "Device-Login abgebrochen");
  assert.equal(drained, 4);
});

test("device login does not replace another active account login", () => {
  const applet = makeApplet();
  let spawned = 0;
  let rebuilt = 0;
  applet._deviceLoginActive = { alpha: true };
  applet._buildUsageMenu = () => { rebuilt += 1; };
  applet._spawnAuxJson = () => { spawned += 1; };

  applet._startDeviceLogin({ account: "beta" });

  assert.equal(spawned, 0);
  assert.equal(applet._deviceLoginActive.alpha, true);
  assert.equal(applet._deviceLoginActive.beta, undefined);
  assert.equal(applet._deviceLoginErrors.beta, "Es läuft bereits ein Anmelde- oder Profiljob");
  assert.equal(rebuilt, 1);
});

test("Manage Account opens the account in the isolated reactivation browser", () => {
  const applet = makeApplet();
  let command = null;
  let rebuilt = 0;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._buildUsageMenu = () => { rebuilt += 1; };
  applet._spawnAuxJson = (argv, callback) => {
    command = argv;
    callback({
      ok: true,
      account: "alpha",
      url: "https://chatgpt.com/codex/cloud/settings/analytics#usage",
    }, null);
  };

  applet._manageAccount({ account: "alpha" });

  assert.deepEqual(command, [
    "codex-usage", "account", "manage", "alpha", "--format", "json",
  ]);
  assert.equal(rebuilt, 0);
});

test("Start Terminal as User opens Codex in the account profile", () => {
  const applet = makeApplet();
  let command = null;
  let rebuilt = 0;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._buildUsageMenu = () => { rebuilt += 1; };
  applet._spawnAuxJson = (argv, callback) => {
    command = argv;
    callback({
      ok: true,
      account: "alpha",
      profile_dir: "/tmp/alpha",
    }, null);
  };

  applet._startAccountTerminal({ account: "alpha" });

  assert.deepEqual(command, [
    "codex-usage", "account", "terminal", "alpha", "--format", "json",
  ]);
  assert.equal(rebuilt, 0);
});

test("account overview rows expose editable account settings", () => {
  const applet = makeAccountSettingsApplet();
  applet._spawnAuxJson = (argv, callback) => {
    assert.deepEqual(
      argv.slice(-4),
      ["overview", "--format", "json", "--config-only"]
    );
    callback({
      accounts: [{
        id: "alpha",
        label: "Alpha",
        profile_dir: "/tmp/alpha",
        auth_json_path: null,
        browser: "chromium",
        reactivation_browser: "vivaldi",
        backend: "app-server",
      }],
    }, null);
  };

  applet._loadAccountBackends();

  assert.deepEqual(JSON.parse(JSON.stringify(applet.accountBackends[0])), {
    account: "alpha",
    label: "Alpha",
    tag: "",
    "auth-json": null,
    "profile-dir": "file:///tmp/alpha",
    "test-home": false,
    browser: 1,
    "reactivation-browser": 1,
    series: "",
    "series-active": false,
    backend: 1,
  });
  assert.equal(applet._backendAccounts.alpha["profile-dir"], "/tmp/alpha");
});

test("account overview leaves unset file chooser values unset", () => {
  const applet = makeAccountSettingsApplet();
  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [{ id: "alpha", label: "Alpha", backend: "direct" }],
  }, null);

  applet._loadAccountBackends();

  assert.equal(applet.accountBackends[0]["auth-json"], null);
  assert.equal(applet.accountBackends[0]["profile-dir"], null);
});

test("legacy absolute account settings are migrated to file URIs", () => {
  const applet = makeAccountSettingsApplet();
  const writes = [];
  applet.settings = { setValue: (key, value) => writes.push([key, value]) };
  applet.accountBackends = [{
    account: "alpha",
    label: "Alpha",
    "auth-json": "/tmp/alpha auth.json",
    "profile-dir": "/tmp/alpha profile",
    browser: 0,
    "reactivation-browser": 0,
    series: "",
    "series-active": false,
    backend: 0,
  }, {
    account: "beta",
    label: "Beta",
    "auth-json": null,
    "profile-dir": "",
    browser: 0,
    "reactivation-browser": 0,
    series: "",
    "series-active": false,
    backend: 0,
  }];

  applet._normalizeAccountBackendSettingPaths();

  assert.deepEqual(JSON.parse(JSON.stringify(applet.accountBackends)), [{
    account: "alpha",
    label: "Alpha",
    "auth-json": "file:///tmp/alpha%20auth.json",
    "profile-dir": "file:///tmp/alpha%20profile",
    browser: 0,
    "reactivation-browser": 0,
    series: "",
    "series-active": false,
    backend: 0,
  }, {
    account: "beta",
    label: "Beta",
    "auth-json": null,
    "profile-dir": "",
    browser: 0,
    "reactivation-browser": 0,
    series: "",
    "series-active": false,
    backend: 0,
  }]);
  assert.deepEqual(JSON.parse(JSON.stringify(writes)), [[
    "account-backends",
    [{
      account: "alpha",
      label: "Alpha",
      "auth-json": "file:///tmp/alpha%20auth.json",
      "profile-dir": "file:///tmp/alpha%20profile",
      browser: 0,
      "reactivation-browser": 0,
      series: "",
      "series-active": false,
      backend: 0,
    }, {
      account: "beta",
      label: "Beta",
      "auth-json": null,
      "profile-dir": "",
      browser: 0,
      "reactivation-browser": 0,
      series: "",
      "series-active": false,
      backend: 0,
    }],
  ]]);
});

test("account path columns leave optional values unset for new rows", () => {
  const schema = JSON.parse(fs.readFileSync(
    path.join(__dirname, "../files/codex-usage@H234598/settings-schema.json"),
    "utf8"
  ));
  const columns = schema["account-backends"].columns;
  const auth = columns.find((column) => column.id === "auth-json");
  const profile = columns.find((column) => column.id === "profile-dir");
  assert.equal(Object.prototype.hasOwnProperty.call(auth, "default"), false);
  assert.equal(Object.prototype.hasOwnProperty.call(profile, "default"), false);
});

test("bounded process output decodes UTF-8 split across chunks", () => {
  const applet = makeApplet();
  const makeStream = (parts) => ({
    read_bytes_async(_size, _priority, _cancellable, callback) {
      callback(this, parts.shift());
    },
    read_bytes_finish(result) {
      return {
        get_size: () => result.length,
        get_data: () => result,
      };
    },
  });
  const encoded = Buffer.from('{"label":"Ä"}\n', "utf8");
  const split = encoded.indexOf(0xc3) + 1;
  const stdout = makeStream([
    encoded.subarray(0, split),
    encoded.subarray(split),
    new Uint8Array(0),
  ]);
  const stderr = makeStream([new Uint8Array(0)]);
  const process = {
    get_stdout_pipe: () => stdout,
    get_stderr_pipe: () => stderr,
    force_exit() {},
  };
  let result;
  let liveChunks = [];

  applet._readBoundedProcessOutput(process, (output, _stderr, error) => {
    result = { output, error };
  }, (_name, chunk, final) => {
    liveChunks.push({ chunk, final: final === true });
  });

  assert.deepEqual(result, {
    output: '{"label":"Ä"}\n',
    error: null,
  });
  assert.equal(liveChunks.length, 3);
  assert.equal(liveChunks.slice(0, 2).map((item) => item.chunk).join(""),
    '{"label":"Ä"}\n');
  assert.deepEqual(liveChunks[2], { chunk: "", final: true });
});

test("bounded reader finalizes a trailing device login token only at EOF", () => {
  const applet = makeApplet();
  applet._buildUsageMenu = () => {};
  applet._deviceLoginLiveAccount = "alpha";
  applet._deviceLoginLiveText.alpha = { stdout: "", stderr: "" };
  const makeStream = (parts) => ({
    read_bytes_async(_size, _priority, _cancellable, callback) {
      callback(this, parts.shift());
    },
    read_bytes_finish(result) {
      return {
        get_size: () => result.length,
        get_data: () => result,
      };
    },
  });
  const stdout = makeStream([
    Buffer.from("Open https://auth.example/device", "utf8"),
    new Uint8Array(0),
  ]);
  const stderr = makeStream([new Uint8Array(0)]);
  const process = {
    get_stdout_pipe: () => stdout,
    get_stderr_pipe: () => stderr,
    force_exit() {},
  };
  const snapshots = [];
  let result = null;

  applet._readBoundedProcessOutput(process, (output, errorOutput, error) => {
    result = { output, errorOutput, error };
  }, (name, chunk, final) => {
    applet._recordDeviceLoginChunk(name, chunk, final);
    snapshots.push({
      final: final === true,
      events: JSON.parse(JSON.stringify(applet._deviceLoginEvents.alpha || [])),
    });
  });

  assert.deepEqual(result, {
    output: "Open https://auth.example/device",
    errorOutput: "",
    error: null,
  });
  assert.deepEqual(snapshots, [
    { final: false, events: [] },
    {
      final: true,
      events: [{ kind: "url", value: "https://auth.example/device" }],
    },
  ]);
});

test("legacy account overview rows receive editable defaults", () => {
  const applet = makeAccountSettingsApplet();
  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [{ id: "alpha", label: "Alpha", backend: "direct" }],
  }, null);

  applet._loadAccountBackends();

  assert.deepEqual(JSON.parse(JSON.stringify(applet.accountBackends[0])), {
    account: "alpha",
    label: "Alpha",
    tag: "",
    "auth-json": null,
    "profile-dir": null,
    "test-home": false,
    browser: 0,
    "reactivation-browser": 0,
    series: "",
    "series-active": false,
    backend: 0,
  });
});

test("legacy global reactivation browser migrates to account rows once", () => {
  const applet = makeAccountSettingsApplet();
  applet.reactivationBrowser = "vivaldi";
  const writes = [];
  applet.settings = { setValue: (key, value) => writes.push([key, value]) };
  const commands = [];
  let migrated = false;
  applet._spawnAuxJson = (argv, callback) => {
    commands.push(argv.slice());
    if (argv.includes("add")) {
      migrated = true;
      callback({ ok: true, account: "alpha" }, null);
      return;
    }
    callback({
      accounts: [{
        id: "alpha",
        label: "Alpha",
        backend: "direct",
        reactivation_browser: migrated ? "vivaldi" : "auto",
      }],
    }, null);
  };

  applet._loadAccountBackends();

  assert.equal(commands.some((argv) => (
    argv.includes("add") &&
    argv.includes("--reactivation-browser") &&
    argv[argv.indexOf("--reactivation-browser") + 1] === "firefox"
  )), true);
  assert.deepEqual(writes.find(([key]) => key === "reactivation-browser-migrated"), [
    "reactivation-browser-migrated",
    true,
  ]);
});

test("legacy reactivation migration stays pending when account update fails", () => {
  const applet = makeAccountSettingsApplet();
  applet.reactivationBrowser = "vivaldi";
  applet._showCommandError = () => {};
  applet._loadAccountBackends = () => {};
  const writes = [];
  applet.settings = { setValue: (key, value) => writes.push([key, value]) };
  applet._spawnAuxJson = (_argv, callback) => callback(
    { ok: false, account: { id: "alpha" } },
    null
  );
  applet._reconcileAccountChanges = (rows) => {
    applet._accountChangeQueue = rows;
    applet._drainAccountChanges();
  };

  applet._migrateLegacyReactivationBrowser([{
    account: "alpha",
    label: "Alpha",
    "auth-json": "",
    "profile-dir": "/tmp/alpha",
    browser: 0,
    "reactivation-browser": 0,
    backend: 0,
  }]);

  assert.equal(writes.some(([key, value]) => (
    key === "reactivation-browser-migrated" && value === true
  )), false);
  assert.notEqual(applet.reactivationBrowserMigrated, true);
});

test("legacy migration marker stays pending when settings write fails", () => {
  const applet = makeAccountSettingsApplet();
  applet.settings = {
    setValue: () => { throw new Error("settings write failed"); },
  };

  applet._markLegacyReactivationBrowserMigrated();

  assert.notEqual(applet.reactivationBrowserMigrated, true);
  assert.notEqual(applet._legacyReactivationMigrationStarted, true);
});

test("account table changes produce complete account add data", () => {
  const applet = makeAccountSettingsApplet();
  const calls = [];
  applet._reconcileAccountChanges = (rows) => calls.push(rows);
  applet.accountBackends = [{
    account: "alpha",
    label: "Renamed",
    "auth-json": "",
    "profile-dir": "/tmp/alpha",
    browser: 1,
    "reactivation-browser": 2,
    series: "",
    "series-active": false,
    backend: 0,
  }];

  applet._onAccountBackendsChanged();

  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [[{
    account: "alpha",
    label: "Renamed",
    "auth-json": null,
    "profile-dir": "/tmp/alpha",
    "test-home": false,
    browser: 1,
    "reactivation-browser": 2,
    series: "",
    "series-active": false,
    backend: 0,
  }]]);
});

test("removing account table row queues account delete without profile deletion", () => {
  const applet = makeAccountSettingsApplet();
  applet._backendAccounts.alpha["auth-json"] = null;
  applet._backendAccounts.beta = {
    account: "beta",
    label: "Beta",
    "auth-json": null,
    "profile-dir": "/tmp/beta",
    browser: 0,
    "reactivation-browser": 0,
    series: "",
    "series-active": false,
    backend: 0,
  };
  applet._drainAccountChanges = () => {};
  applet.accountBackends = [{
    account: "alpha",
    label: "Alpha",
    "auth-json": "",
    "profile-dir": "/tmp/alpha",
    browser: 0,
    "reactivation-browser": 0,
    series: "",
    "series-active": false,
    backend: 0,
  }];

  applet._onAccountBackendsChanged();

  assert.deepEqual(JSON.parse(JSON.stringify(applet._accountChangeQueue)), [{
    action: "delete",
    account: "beta",
  }]);
});

test("account delete drains through structured CLI command", () => {
  const applet = makeAccountSettingsApplet();
  const calls = [];
  let reloads = 0;
  applet._accountChangeQueue = [{ action: "delete", account: "alpha" }];
  applet._loadAccountBackends = () => { reloads += 1; };
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv);
    callback({ ok: true, account: "alpha", label: "Alpha", profile_deleted: false }, null);
  };

  applet._drainAccountChanges();

  assert.deepEqual(calls, [["codex-usage", "account", "delete", "alpha", "--format", "json"]]);
  assert.equal(reloads, 1);
});

test("account delete cancels persistent profile job before deleting account", () => {
  const applet = makeAccountSettingsApplet();
  const calls = [];
  const jobId = "job-1234567890abcdef1234567890abcdef";
  applet._deviceLoginJobs.alpha = jobId;
  applet._deviceLoginActive.alpha = true;
  applet._accountChangeQueue = [{ action: "delete", account: "alpha" }];
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._buildUsageMenu = () => {};
  applet._loadAccountBackends = () => {};
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv);
    if (argv.includes("cancel")) {
      callback({
        account: "alpha",
        job_id: jobId,
        ok: false,
        status: "cancelled",
      }, null);
      return;
    }
    callback({ ok: true, account: "alpha" }, null);
  };

  applet._drainAccountChanges();

  assert.deepEqual(calls, [
    ["codex-usage", "profile", "cancel", jobId, "--json"],
    ["codex-usage", "account", "delete", "alpha", "--format", "json"],
  ]);
  assert.equal(applet._deviceLoginJobs.alpha, undefined);
  assert.equal(applet._accountDeleteWaitingForProfileJob.alpha, undefined);
});

test("account delete forces status poll after cancel request", () => {
  const applet = makeAccountSettingsApplet();
  const calls = [];
  const jobId = "job-1234567890abcdef1234567890abcdef";
  applet._deviceLoginJobs.alpha = jobId;
  applet._deviceLoginActive.alpha = true;
  applet._accountChangeQueue = [{ action: "delete", account: "alpha" }];
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._buildUsageMenu = () => {};
  applet._loadAccountBackends = () => {};
  applet._spawnAuxJson = (argv, callback, backendRequest) => {
    calls.push({ argv, backendRequest });
    if (argv.includes("cancel")) {
      callback({
        account: "alpha",
        job_id: jobId,
        ok: true,
        status: "cancel_requested",
      }, null);
      return;
    }
    if (argv.includes("job-status")) {
      callback({
        account: "alpha",
        job_id: jobId,
        ok: true,
        status: "cancelled",
      }, null);
      return;
    }
    callback({ ok: true, account: "alpha" }, null);
  };

  applet._drainAccountChanges();

  assert.deepEqual(calls.map((call) => call.argv), [
    ["codex-usage", "profile", "cancel", jobId, "--json"],
    ["codex-usage", "profile", "job-status", jobId, "--json"],
    ["codex-usage", "account", "delete", "alpha", "--format", "json"],
  ]);
  assert.equal(calls[1].backendRequest, true);
  assert.equal(applet._accountDeleteWaitingForProfileJob.alpha, undefined);
});

test("account delete clears stale profile job wait marker", () => {
  const applet = makeAccountSettingsApplet();
  const calls = [];
  applet._accountDeleteWaitingForProfileJob.alpha = true;
  applet._accountChangeQueue = [{ action: "delete", account: "alpha" }];
  applet._loadAccountBackends = () => {};
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv);
    callback({ ok: true, account: "alpha" }, null);
  };

  applet._drainAccountChanges();

  assert.deepEqual(calls, [[
    "codex-usage", "account", "delete", "alpha", "--format", "json",
  ]]);
  assert.equal(applet._accountDeleteWaitingForProfileJob.alpha, undefined);
});

test("account edits during reconcile are retained for the next pass", () => {
  const applet = makeAccountSettingsApplet();
  applet._accountChangeCurrent = { account: "alpha" };
  applet.accountBackends = [{
    account: "alpha",
    label: "Latest label",
    "auth-json": "",
    "profile-dir": "/tmp/alpha",
    browser: 0,
    "reactivation-browser": 0,
    series: "",
    "series-active": false,
    backend: 0,
  }];

  applet._onAccountBackendsChanged();

  assert.deepEqual(JSON.parse(JSON.stringify(applet._accountChangePendingRows)), [[{
    account: "alpha",
    label: "Latest label",
    "auth-json": null,
    "profile-dir": "/tmp/alpha",
    "test-home": false,
    browser: 0,
    "reactivation-browser": 0,
    series: "",
    "series-active": false,
    backend: 0,
  }]][0]);
});

test("queued account writes drain before pending rows reload", () => {
  const applet = makeAccountSettingsApplet();
  const row = (account) => ({
    account,
    label: account,
    "auth-json": "",
    "profile-dir": "/tmp/" + account,
    browser: 0,
    "reactivation-browser": 0,
    backend: 0,
  });
  const processed = [];
  let reloads = 0;
  applet._accountChangeQueue = [row("alpha"), row("beta")];
  applet._accountChangePendingRows = [row("latest")];
  applet._refreshFresh = () => {};
  applet._buildUsageMenu = () => {};
  applet._loadAccountBackends = () => { reloads += 1; };
  applet._spawnAuxJson = (argv, callback) => {
    if (argv.includes("profile") && argv.includes("create")) {
      callback({
        ok: true,
        account: "beta",
        job_id: "job-1234567890abcdef1234567890abcdef",
        status: "queued",
      }, null);
      return;
    }
    if (argv.includes("profile") && argv.includes("job-status")) {
      callback({
        ok: true,
        account: "beta",
        job_id: "job-1234567890abcdef1234567890abcdef",
        status: "running",
        events: [],
      }, null);
      return;
    }
    const account = argv[argv.indexOf("add") + 1];
    processed.push(account);
    callback({ ok: true, account: { id: account } }, null);
  };

  applet._drainAccountChanges();

  assert.deepEqual(processed, ["alpha", "beta"]);
  assert.equal(reloads, 1);
});

test("account add converts file URIs before spawning CLI", () => {
  const applet = makeAccountSettingsApplet();
  applet._backendAccounts = {};
  applet._loadAccountBackends = () => {};
  applet._accountChangeQueue = [{
    account: "new-account",
    label: "New",
    "auth-json": "file:///tmp/auth%20new.json",
    "profile-dir": "file:///tmp/profile%20new",
    browser: 0,
    "reactivation-browser": 0,
    backend: 0,
  }];
  applet._spawnAuxJson = (argv, callback) => {
    assert.equal(argv[argv.indexOf("--auth-json") + 1], "/tmp/auth new.json");
    assert.equal(argv[argv.indexOf("--profile-dir") + 1], "/tmp/profile new");
    callback({ ok: true, account: { id: "new-account" } }, null);
  };

  applet._drainAccountChanges();
});

test("new account starts persistent profile job after account config", () => {
  const applet = makeAccountSettingsApplet();
  const calls = [];
  const jobId = "job-1234567890abcdef1234567890abcdef";
  applet._backendAccounts = {};
  applet._accountChangeQueue = [{
    account: "new-account",
    label: "New",
    "auth-json": null,
    "profile-dir": "/tmp/profile-new",
    browser: 1,
    "reactivation-browser": 2,
    backend: 1,
  }];
  applet._buildUsageMenu = () => {};
  applet._loadAccountBackends = () => {};
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv);
    if (argv.includes("account") && argv.includes("add")) {
      callback({
        ok: true,
        account: { id: "new-account", profile_dir: "/tmp/profile-new" },
      }, null);
      return;
    }
    if (argv.includes("profile") && argv.includes("create")) {
      callback({
        ok: true,
        account: "new-account",
        job_id: jobId,
        status: "queued",
      }, null);
      return;
    }
    callback({
      ok: true,
      account: "new-account",
      job_id: jobId,
      status: "running",
      events: [],
    }, null);
  };

  applet._drainAccountChanges();

  assert.deepEqual(calls[1], [
    "codex-usage", "profile", "create",
    "--account-id", "new-account",
    "--label", "New",
    "--browser", "chromium",
    "--backend", "app-server",
    "--profile-dir", "/tmp/profile-new",
    "--reactivation-browser", "chromium",
    "--json-events",
  ]);
  assert.equal(applet._deviceLoginJobs["new-account"], jobId);
  assert.equal(applet._deviceLoginActive["new-account"], true);
  assert.equal(applet._profilePendingAccounts["new-account"], true);
});

test("account file chooser URIs become local paths before account add", () => {
  const applet = makeAccountSettingsApplet();
  const calls = [];
  applet._reconcileAccountChanges = (rows) => calls.push(rows);
  applet.accountBackends = [{
    account: "alpha",
    label: "Alpha",
    "auth-json": "file:///tmp/alpha%20auth.json",
    "profile-dir": "file:///tmp/alpha%20profile",
    browser: 0,
    "reactivation-browser": 0,
    backend: 0,
  }];

  applet._onAccountBackendsChanged();

  assert.equal(calls[0][0]["auth-json"], "/tmp/alpha auth.json");
  assert.equal(calls[0][0]["profile-dir"], "/tmp/alpha profile");
});

test("file URIs without a path are rejected before conversion", () => {
  let runtime;
  const applet = makeApplet((currentRuntime) => { runtime = currentRuntime; });

  assert.throws(
    () => applet._localAccountPath("file://"),
    /invalid local account path/
  );
  assert.throws(
    () => applet._localAccountPath("file://localhost"),
    /invalid local account path/
  );
  assert.equal(runtime.fileUriCalls, 0);
});

test("local account path helpers accept local paths and localhost URIs only", () => {
  let runtime;
  const applet = makeApplet((currentRuntime) => { runtime = currentRuntime; });

  assert.equal(applet._localAccountPath("/tmp/alpha auth.json"), "/tmp/alpha auth.json");
  assert.equal(applet._localAccountPath("file:///tmp/alpha%20auth.json"), "/tmp/alpha auth.json");
  assert.equal(applet._localAccountPath("file://LOCALHOST/tmp/profile"), "/tmp/profile");
  assert.equal(applet._accountSettingPath(null), null);
  assert.equal(applet._accountSettingPath(""), "");
  assert.match(applet._accountSettingPath("/tmp/profile"), /^file:\/\/\/tmp\/profile$/);
  assert.throws(() => applet._localAccountPath("file://remote/tmp/profile"), /invalid local account path/);
  assert.throws(() => applet._accountSettingPath("relative/profile"), /invalid local account path/);
  assert.ok(runtime.fileUriCalls >= 2);
});

test("date, time, duration and style helpers cover all supported display modes", () => {
  const applet = makeApplet();
  const date = new Date(2026, 0, 2, 13, 4, 5);

  assert.equal(applet._formatDatePart(date, 0), "02.01.2026");
  assert.equal(applet._formatDatePart(date, 1), "2026-01-02");
  assert.equal(applet._formatDatePart(date, 2), "02.01.26");
  assert.equal(applet._formatDatePart(date, 3), "2. Januar 2026");
  assert.equal(applet._formatDatePart(date, 99), "02.01.2026");
  assert.equal(applet._formatTimePart(date, 0), "13:04");
  assert.equal(applet._formatTimePart(date, 1), "13:04:05");
  assert.equal(applet._formatTimePart(date, 2), "01:04 PM");
  assert.equal(applet._formatDurationPart(null, 0), "–");
  assert.equal(applet._formatDurationPart(150, 0), "2h 30m");
  assert.equal(applet._formatDurationPart(150, 0, true), "2,5h");
  assert.equal(applet._formatDurationPart(150, 1), "02:30");
  assert.equal(applet._formatDurationPart(150, 2), "2 Stunden 30 Minuten");
  assert.equal(applet._formatDurationPart(150, 3), "2h 30m");
  assert.equal(applet._formatDurationPart(150, 4), "2h 30m");
  assert.equal(applet._durationMinutes(null), null);
  assert.equal(applet._durationMinutes({reset_at: "not-a-date"}), null);
  assert.equal(applet._styleMode({mode: 2}), 2);
  assert.equal(applet._styleMode({mode: "2"}), 2);
  assert.equal(applet._styleMode({mode: 9, conditional: true}), 1);
  assert.equal(applet._styleMode({conditional: false}), 0);
  assert.equal(applet._styleIsActive({mode: 0, threshold: 100}, null), true);
  assert.equal(applet._styleIsActive({mode: 1, threshold: 50}, 49), true);
  assert.equal(applet._styleIsActive({mode: 1, threshold: 50}, 50), false);
  assert.equal(applet._styleIsActive({mode: 1, threshold: 50}, null), false);
  assert.equal(applet._styleIsActive({mode: 3, threshold: 0}, 0), false);
});

test("markup escaping preserves zero and false while escaping untrusted text", () => {
  const applet = makeApplet();

  assert.equal(applet._escapeMarkup(null), "");
  assert.equal(applet._escapeMarkup(undefined), "");
  assert.equal(applet._escapeMarkup(0), "0");
  assert.equal(applet._escapeMarkup(false), "false");
  assert.equal(applet._escapeMarkup("<&>"), "&lt;&amp;&gt;");
  assert.equal(applet._styleSpan("0", {mode: 0}, 0, "panel"), "0");
});

test("status and capture timestamp helpers reject invalid or future provenance", () => {
  const applet = makeApplet();
  const now = Date.now();
  const recent = new Date(now - 1000).toISOString();
  const older = new Date(now - 2000).toISOString();
  const future = new Date(now + 60 * 1000).toISOString();
  const farFuture = new Date(now + 3 * 60 * 60 * 1000).toISOString();

  assert.equal(applet._statusLabel("ok"), "ok");
  assert.equal(applet._statusLabel("partial"), "unvollständig");
  assert.equal(applet._statusLabel("login_required"), "Anmeldung erforderlich");
  assert.equal(applet._statusLabel("unknown"), "Fehler");
  assert.equal(applet._dateMillis(recent) > 0, true);
  assert.equal(applet._dateMillis("bad"), null);
  assert.equal(applet._captureTimestampUsable(recent), true);
  assert.equal(applet._captureTimestampUsable(farFuture), false);
  assert.equal(applet._captureIsTooFarInFuture(farFuture, now), true);
  assert.equal(applet._captureIsTooFarInFuture(future, now), false);
  assert.equal(applet._captureIsOlder(older, recent), true);
  assert.equal(applet._captureIsOlder(recent, older), false);
  assert.equal(applet._captureIsOlder(farFuture, recent), true);
  assert.equal(applet._captureIsOlder(recent, farFuture), false);
});

test("usage severity ignores Spark windows from an unavailable model pool", () => {
  const applet = makeApplet();
  const sparkWindow = {
    name: "5h", limit_window_seconds: 18000, remaining: 1, limit: 100,
  };
  const base = {
    account: "alpha", status: "ok", stale: false,
    five_hour: {remaining: 50}, weekly: {remaining: 50},
    models: {
      "gpt-5.3-codex-spark": {
        available: false, allowed: true, limit_reached: false, exhausted: false,
        windows: [sparkWindow],
      },
    },
  };
  assert.equal(applet._usageSeverity(base), "");
  base.models["gpt-5.3-codex-spark"].available = true;
  assert.equal(applet._usageSeverity(base), "codex-usage-critical");
});

test("window identity helpers distinguish aliases, conflicts, duplicates and pool selection", () => {
  const applet = makeApplet();
  const five = {name: "5h", limit_window_seconds: 18000, remaining: 80, limit: 100};
  const week = {name: "weekly", limit_window_seconds: 604800, remaining: 40, limit: 100};
  const month = {name: "30d", limit_window_seconds: 2592000, remaining: 60, limit: 100};
  const pool = {
    available: true, allowed: true, limit_reached: false, exhausted: false,
    windows: [five, week, month],
  };

  assert.equal(applet._windowIdentityIsKnown(five), true);
  assert.equal(applet._windowIdentityKey({name: "w"}), 604800);
  assert.equal(applet._windowIdentityIsKnown({name: "5h", duration_seconds: 604800}), false);
  assert.equal(applet._windowIdentityKey({name: "5h", duration_seconds: 604800}), null);
  assert.equal(applet._hasUniqueWindowIdentities(pool.windows), true);
  assert.equal(applet._hasDuplicateWindowIdentities(pool.windows), false);
  assert.equal(applet._hasUniqueWindowIdentities([five, {name: "5_hour"}]), false);
  assert.equal(applet._hasDuplicateWindowIdentities([five, {name: "5_hour"}]), true);
  assert.equal(applet._poolWindowForDuration(pool, 604800), week);
  assert.equal(applet._poolWindowForDuration(pool, 123), null);
  assert.equal(applet._poolAverage(pool), 60);
  assert.equal(applet._poolOtherWindow(pool), month);
  assert.equal(applet._poolIsUsable(pool), true);
  pool.exhausted = true;
  assert.equal(applet._poolIsUsable(pool), false);
});

test("monthly panel source cannot bypass an unusable main pool", () => {
  const applet = makeApplet();
  const usage = applet._usages[0];
  usage.main = {
    available: false, allowed: true, limit_reached: false, exhausted: false,
    windows: [{name: "30d", limit_window_seconds: 2592000, remaining: 80, limit: 100}],
  };

  assert.equal(applet._panelValueForSource(usage, 8), null);
  assert.equal(applet._panelWindowForSource(usage, 8), null);
});

test("panel source labels map known settings and use the documented average fallback", () => {
  const applet = makeApplet();
  for (const [name, value] of Object.entries({
    "five-hour": 1, weekly: 2, average: 3, "spark-five-hour": 4,
    "spark-weekly": 5, "spark-average": 6, "spark-other": 7, "thirty-day": 8,
  })) {
    assert.equal(applet._panelSourceValue(name), value);
  }
  assert.equal(applet._panelSourceValue("unknown"), 3);
});

test("credit custom formats reject non-text values instead of silently becoming empty", () => {
  const applet = makeApplet();
  const valid = {
    account: "alpha", "show-panel": false, "show-tooltip": true,
    format: "compact", "custom-format": "", "hide-when-zero": false,
    smoothing: "ema-20", "show-coverage-marker": true,
    "baseline-enabled": false, "baseline-minutes": 60,
    "consumption-show-panel": false, "consumption-show-tooltip": true,
    "consumption-amount": 1, "consumption-unit": "hours",
    "consumption-format": "compact", "consumption-custom-format": "",
    "consumption-smoothing": "ema-20", "consumption-hide-when-zero": false,
    "consumption-show-coverage-marker": true,
    "consumption-baseline-enabled": false, "consumption-baseline-minutes": 60,
  };
  assert.ok(applet._normalizeCreditRow(valid, "alpha"));
  assert.equal(applet._normalizeCreditRow(Object.assign({}, valid, {"custom-format": 0}), "alpha"), null);
  assert.equal(applet._normalizeCreditRow(Object.assign({}, valid, {"consumption-custom-format": false}), "alpha"), null);
  assert.equal(applet._normalizeCreditRow(Object.assign({}, valid, {"custom-format": "{value}%"}), "alpha")["custom-format"], "{value}%");
});

test("legacy consumption and forecast fields do not coerce falsey invalid values to defaults", () => {
  const applet = makeApplet();
  assert.throws(() => applet._normalizeForecastRow({
    account: "alpha", "forecast-format": 0,
  }, "alpha"), /invalid text value/);
  assert.throws(() => applet._normalizeForecastRow({
    account: "alpha", "forecast-limit-window": false,
  }, "alpha"), /invalid text value/);
  assert.throws(() => applet._normalizeCreditConsumptionRow({
    account: "alpha", "show-panel": false, "show-tooltip": true,
    "consumption-unit": 0,
  }, "alpha"), /invalid text value/);
  assert.throws(() => applet._normalizeCreditConsumptionRow({
    account: "alpha", "show-panel": false, "show-tooltip": true,
    "consumption-format": false,
  }, "alpha"), /invalid text value/);
});

test("DTO sanitizers retain valid fields and fail closed on contradictory usage metadata", () => {
  const applet = makeApplet();
  const window = applet._safeWindow({
    name: "5h", duration_seconds: 18000, used: 20, limit: 100, remaining: 80,
    percent: 80, reset_at: "2026-08-19T12:00:00Z", raw: "raw", source: "api",
  });
  assert.equal(window.name, "5h");
  assert.equal(window.duration_seconds, 18000);
  assert.equal(window.used, 20);
  assert.equal(window.limit, 100);
  assert.equal(window.remaining, 80);
  assert.equal(window.percent, 80);
  assert.equal(window.source, "api");
  const contradictory = applet._safeWindow({
    name: "5h", used: 20, limit: 100, remaining: 80, percent: 101,
  });
  assert.equal(contradictory.used, null);
  assert.equal(contradictory.limit, null);
  assert.equal(contradictory.remaining, null);
  assert.equal(contradictory.percent, null);
  assert.throws(() => applet._safeWindow({name: "5h", duration_seconds: 0}), /invalid limit duration/);

  const consumption = applet._safeConsumptionWindows([{
    pool: "main", lookback_seconds: 600, limit_window_seconds: 18000,
    consumed_percentage_points: 2.5, estimated_seconds_to_exhaustion: 3600,
    baseline_used_percent: 20, coverage: "complete", sample_count: 4,
  }]);
  assert.equal(consumption.length, 1);
  assert.equal(consumption[0].estimated_seconds_to_exhaustion, 3600);
  assert.throws(() => applet._safeConsumptionWindows([{
    pool: "main", lookback_seconds: 0, limit_window_seconds: 18000,
    consumed_percentage_points: 1, coverage: "complete", sample_count: 1,
  }]), /invalid consumption window/);

  const unknownResets = applet._safeUsageResets(null);
  assert.equal(unknownResets.available, null);
  assert.equal(unknownResets.known, false);
  assert.equal(unknownResets.redeem_capability, false);
  const resets = applet._safeUsageResets({known: true, available: 12, redeem_capability: true});
  assert.equal(resets.available, 12);
  assert.equal(resets.known, true);
  assert.equal(applet._safeUsageResets({known: true, available: 10001, redeem_capability: false}).known, false);
});

test("payload usage aggregation requires known windows and usable model pools", () => {
  const applet = makeApplet();
  const five = {name: "5h", limit_window_seconds: 18000, remaining: 80, limit: 100};
  const week = {name: "weekly", limit_window_seconds: 604800, remaining: 60, limit: 100};
  const usablePool = {
    available: true, allowed: true, limit_reached: false, exhausted: false,
    windows: [five, week],
  };
  assert.equal(applet._windowHasUsageValue(five), true);
  assert.equal(applet._windowHasUsageValue({name: "5h"}), false);
  assert.equal(applet._hasPayloadUsageValue(null, null, usablePool, {}), true);
  assert.equal(applet._hasPayloadUsageValue(five, null, null, {}), true);
  assert.equal(applet._hasPayloadUsageValue({name: "unknown", remaining: 80}, null, null, {}), false);
  assert.equal(applet._hasModelPayloadUsageValue({spark: usablePool}), true);
  assert.equal(applet._hasModelPayloadUsageValue({spark: Object.assign({}, usablePool, {available: false})}), false);
  assert.equal(applet._poolExhaustedByFields(true, true, false, [five]), false);
  assert.equal(applet._poolExhaustedByFields(true, true, false, [{name: "5h", remaining: 0, limit: 100}]), true);
  assert.equal(applet._poolExhaustedByFields(false, true, false, [five]), true);
});

test("cache window matching and expiry use the declared kind and duration", () => {
  const applet = makeApplet();
  const current = {name: "5h", duration_seconds: 18000, remaining: 80};
  const cached = {name: "five_hour", remaining: 70};
  assert.equal(applet._windowDisplayLabel(current), "5h");
  assert.equal(applet._windowDisplayLabel({name: "custom"}), "custom");
  assert.equal(applet._windowDisplayLabel({}), "Limit");
  assert.equal(applet._windowDurationMatches(current, cached, "five_hour"), true);
  assert.equal(applet._windowDurationMatches(current, {name: "weekly"}, "five_hour"), false);
  assert.equal(applet._windowDurationMatches(current, {name: "5h", duration_seconds: 604800}, null), false);
  assert.equal(applet._windowCacheExpired(null, "bad", "bad"), false);
  assert.equal(applet._windowCacheExpired({name: "weekly", duration_seconds: 604800,
    reset_at: "2026-08-18T00:00:00Z"}, "2026-08-17T00:00:00Z", "2026-08-19T00:00:00Z"), true);
  assert.equal(applet._windowCacheExpired({name: "weekly", duration_seconds: 604800,
    reset_at: "2026-08-20T00:00:00Z"}, "2026-08-17T00:00:00Z", "2026-08-19T00:00:00Z"), false);
  assert.equal(applet._windowCacheExpired({name: "weekly", duration_seconds: 604800},
    "2026-08-17T00:00:00Z", "2026-08-19T00:00:00Z"), false);
  assert.equal(applet._windowCacheExpired({name: "weekly", duration_seconds: 604800},
    "2026-08-17T00:00:00Z", "2026-08-25T00:00:00Z"), true);
});

test("window identity helpers fail closed on malformed duration and reset metadata", () => {
  const applet = makeApplet();

  assert.equal(applet._windowDurationSeconds({
    duration_seconds: 18000,
    raw: '{"limit_window_seconds":604800}'
  }), 18000);
  assert.equal(applet._windowDurationSeconds({
    duration_seconds: 0,
    raw: '{"limit_window_seconds":604800}'
  }), 604800);
  assert.equal(applet._windowDurationSeconds({
    raw: '{"limit_window_seconds":604800.5}'
  }), null);
  assert.equal(applet._windowDurationSeconds({ raw: "not-json" }), null);

  assert.equal(applet._windowResetExpired({
    reset_at: "2026-08-19T12:00:00Z"
  }, "2026-08-19T11:59:59Z"), false);
  assert.equal(applet._windowResetExpired({
    reset_at: "2026-08-19T12:00:00Z"
  }, "2026-08-19T12:00:00Z"), true);
  assert.equal(applet._windowResetExpired({
    reset_at: "invalid"
  }, "2026-08-19T11:59:59Z"), true);
  assert.equal(applet._windowResetExpired({ name: "weekly" }, "invalid"), false);
});

test("display and formatting helpers cover separators, source labels and account tags", () => {
  const applet = makeApplet();

  for (const [mode, expected] of [
    ["bar", " | "],
    ["dot", " · "],
    ["slash", " // "],
    ["brackets", " "],
  ]) {
    applet.panelAccountSeparator = mode;
    assert.equal(applet._panelSeparator().plain, expected);
    assert.equal(applet._panelSeparator().markup, expected);
  }
  applet.panelAccountSeparator = "unknown";
  assert.equal(applet._panelSeparator().plain, " | ");

  for (const [source, label] of Object.entries({
    1: "5h", 2: "W", 3: "Ø", 4: "S5h", 5: "SW",
    6: "SØ", 7: "S+", 8: "30d", 9: "CR", 10: "CV",
  })) {
    assert.equal(applet._panelSourceLabel(Number(source)), label);
  }
  assert.equal(applet._panelSourceLabel(0), "?");
  assert.equal(applet._panelSourceLabel("1"), "5h");

  assert.equal(applet._accountTag("Bernie Privat"), "BP");
  assert.equal(applet._accountTag("A B C D"), "ABC");
  assert.equal(applet._accountTag("Nufker"), "Nu");
  assert.equal(applet._accountTag("---"), "?");
  assert.equal(applet._accountTag("Ähre Öko"), "ÄÖ");
});

test("display separator and consumption selection helpers keep their scopes independent", () => {
  const applet = makeApplet();
  applet._displaySettings.alpha = {
    account: "alpha",
    hover: 1,
    click: 1,
    "hover-separator": true,
    "click-separator": false,
  };
  assert.equal(applet._displaySeparatorEnabled("alpha", "hover"), true);
  assert.equal(applet._displaySeparatorEnabled("alpha", "click"), false);
  assert.equal(applet._displaySeparatorEnabled("alpha", "panel"), false);
  assert.equal(applet._displaySeparatorEnabled("unknown", "hover"), false);

  const windows = [
    { pool: "main", limit_window_seconds: 18000, consumed_percentage_points: 1 },
    { pool: "main", limit_window_seconds: 604800, consumed_percentage_points: 2 },
    { pool: "main", limit_window_seconds: 2592000, consumed_percentage_points: 3 },
    { pool: "spark", limit_window_seconds: 18000, consumed_percentage_points: 4 },
    { pool: "main", limit_window_seconds: 18000, consumed_percentage_points: 5,
      _consumption_query_key: "main|1|hours|ema-10|60" },
  ];
  assert.equal(applet._selectConsumptionWindows(windows, "short", "main").length, 2);
  assert.equal(applet._selectConsumptionWindows(windows, "weekly", "main").length, 1);
  assert.equal(applet._selectConsumptionWindows(windows, "monthly", "main").length, 1);
  assert.equal(applet._selectConsumptionWindows(windows, "all", "main").length, 4);
  assert.equal(applet._selectConsumptionWindows(windows, "spark", "spark").length, 1);
  assert.equal(applet._selectConsumptionWindows(windows, "invalid", "main").length, 0);
  assert.equal(applet._selectConsumptionWindows(
    windows, "short", "main", "main|1|hours|ema-10|60"
  ).length, 1);
  assert.equal(applet._selectConsumptionWindows(
    windows, "short", "main", "main|9|hours|ema-10|60"
  ).length, 1);
});

test("mapping and primitive helpers are bounded and prototype-safe", () => {
  const applet = makeApplet();
  const rows = [
    {account: "__proto__", value: 1},
    {account: "toString", value: 2},
  ];
  for (const map of [
    applet._panelSettingsMap(rows),
    applet._alertSettingsMap(rows),
    applet._displaySettingsMap(rows),
    applet._styleMap(rows),
    applet._consumptionSettingsMap(rows),
    applet._creditSettingsMap(rows),
    applet._resetSettingsMap(rows),
  ]) {
    assert.equal(Object.prototype.hasOwnProperty.call(map, "__proto__"), true);
    assert.equal(Object.prototype.hasOwnProperty.call(map, "toString"), true);
    assert.equal(map["__proto__"].value, 1);
    assert.equal(map.toString.value, 2);
  }
  const targetMap = applet._targetMap([
    {account: "__proto__", element: 4, value: 1},
    {account: "toString", element: 4, value: 2},
  ]);
  assert.equal(Object.prototype.hasOwnProperty.call(targetMap, "__proto__:4"), true);
  assert.equal(Object.prototype.hasOwnProperty.call(targetMap, "toString:4"), true);
  assert.equal(targetMap["__proto__:4"].value, 1);
  assert.equal(targetMap["toString:4"].value, 2);
  assert.equal(applet._styleRowsEqual(rows, rows.map((row) => Object.assign({}, row))), true);
  assert.equal(applet._styleRowsEqual(rows, rows.slice(0, 1)), false);
  assert.equal(applet._boundedInteger("2.6", 0, 10, 4), 3);
  assert.equal(applet._boundedInteger("not-a-number", 0, 10, 4), 4);
  assert.equal(applet._boundedInteger(99, 0, 10, 4), 10);
  assert.equal(applet._strictIntegerSetting(3), 3);
  assert.equal(applet._strictIntegerSetting("3"), null);
  assert.equal(applet._shortText("  a\n b  ", 20), "a  b");
  assert.match(applet._shortText("abcdefgh", 5), /…$/);
  assert.equal(applet._accountTag("Bernie Second_Privat"), "BSP");
  assert.equal(applet._accountTag(""), "?");
  assert.equal(applet._statusLabel("unknown"), "Fehler");
  assert.equal(applet._dateMillis("not-a-date"), null);
  assert.equal(applet._dateMillis("2026-08-19T12:00:00Z"), 1787140800000);
});

test("baseline, custom credit and percent helpers preserve independent display states", () => {
  const applet = makeApplet();
  applet._elementTargetEnabled = () => true;
  applet._styleSpan = (text) => text;

  assert.equal(applet._baselineParts("alpha", {
    "baseline-enabled": false,
    "baseline-minutes": 60,
  }, "panel", null), null);
  assert.equal(applet._baselineParts("alpha", {
    "baseline-enabled": true,
    "baseline-minutes": 60,
  }, "panel", null).plain, "AW60m=—");
  assert.equal(applet._baselineParts("alpha", {
    "baseline-enabled": true,
    "baseline-minutes": 60,
  }, "panel", { baseline_used_percent: 33.34 }).plain, "AW60m=33,3%");

  assert.equal(applet._customCreditConsumptionText("{period}:{value}:{coverage}", {
    period: "1 h", value: "2,5", coverage: "vollständig"
  }), "1 h:2,5:vollständig");
  assert.equal(applet._customCreditConsumptionText("", {
    period: "1 h", value: "2,5", coverage: "vollständig"
  }), "Δ1 h 2,5 Credit-%");
  assert.equal(applet._customCreditText("{remaining}/{used}/{limit}/{percent}/{reset}", {
    remaining: "8", used: "2", limit: "10", percent: "80", reset: "morgen"
  }), "8/2/10/80/morgen");
  assert.equal(applet._customCreditText("", { remaining: "8" }), "Credits 8");

  assert.equal(applet._windowValue({ remaining: 80.4 }), "80%");
  assert.equal(applet._windowValue({ remaining: null }), "–");
  const missingPercent = applet._percentPartsFromValue(null, "alpha", "panel");
  assert.equal(missingPercent.plain, "–");
  assert.equal(missingPercent.markup, "–");
  applet._elementTargetEnabled = () => false;
  const hiddenPercent = applet._percentPartsFromValue(80, "alpha", "panel");
  assert.equal(hiddenPercent.plain, "");
  assert.equal(hiddenPercent.markup, "");
});

test("relative account paths are rejected before spawning CLI", () => {
  const applet = makeAccountSettingsApplet();
  let reloads = 0;
  applet._loadAccountBackends = () => { reloads += 1; };
  applet._reconcileAccountChanges = () => {
    throw new Error("must not spawn account update");
  };
  applet.accountBackends = [{
    account: "alpha",
    label: "Alpha",
    "auth-json": null,
    "profile-dir": "relative/profile",
    browser: 0,
    "reactivation-browser": 0,
    backend: 0,
  }];

  applet._onAccountBackendsChanged();

  assert.equal(reloads, 1);
});

test("invalid account file URI reloads settings instead of escaping callback", () => {
  const applet = makeAccountSettingsApplet();
  let reloads = 0;
  applet._loadAccountBackends = () => { reloads += 1; };
  applet._reconcileAccountChanges = () => {};
  applet.accountBackends = [{
    account: "alpha",
    label: "Alpha",
    "auth-json": "file://relative",
    "profile-dir": "/tmp/alpha",
    browser: 0,
    "reactivation-browser": 0,
    backend: 0,
  }];

  applet._onAccountBackendsChanged();

  assert.equal(reloads, 1);
});

test("remote file URI authority is rejected as a non-local account path", () => {
  const applet = makeAccountSettingsApplet();
  let reloads = 0;
  applet._loadAccountBackends = () => { reloads += 1; };
  applet._reconcileAccountChanges = () => {};
  applet.accountBackends = [{
    account: "alpha",
    label: "Alpha",
    "auth-json": "file://server/tmp/alpha.json",
    "profile-dir": "/tmp/alpha",
    browser: 0,
    "reactivation-browser": 0,
    backend: 0,
  }];

  applet._onAccountBackendsChanged();

  assert.equal(reloads, 1);
});

test("legacy panel tags migrate to central display settings", () => {
  const applet = makeAccountSettingsApplet();
  delete applet._syncStyleRows;
  delete applet._syncAccountSettings;
  applet.accountPanelSettings = [{
    account: "alpha",
    tag: "A",
    order: 1,
    muted: false,
    slot1: 3,
    slot2: 0,
  }];
  applet.accountDisplaySettings = [];
  const writes = [];
  applet.settings = { setValue: (key, value) => writes.push([key, value]) };

  applet._syncStyleRows([applet._backendAccounts.alpha]);
  applet._syncAccountSettings([applet._backendAccounts.alpha]);

  assert.deepEqual(JSON.parse(JSON.stringify(applet.accountDisplaySettings[0])), {
    account: "alpha",
    tag: "A",
      panel: 2,
      hover: 1,
      click: 1,
      "hover-separator": false,
      "click-separator": false,
    });
  assert.equal(
    Object.prototype.hasOwnProperty.call(
      writes.find(([key]) => key === "account-panel-settings")[1][0],
      "tag"
    ),
    false
  );
});

test("display targets resolve account id, label and tag per surface", () => {
  const applet = makeApplet();
  const item = { usage: { account: "alpha", label: "Private Account" } };

  assert.equal(applet._accountDisplayText(item, "panel"), "A");
  assert.equal(applet._accountDisplayText(item, "hover"), "Private Account");
  assert.equal(applet._accountDisplayText(item, "click"), "Private Account");

  applet._displaySettings.alpha = {
    account: "alpha",
    tag: "Priv",
    panel: 0,
    hover: 2,
    click: 0,
  };
  assert.equal(applet._accountDisplayText(item, "panel"), "alpha");
  assert.equal(applet._accountDisplayText(item, "hover"), "Priv");
  assert.equal(applet._accountDisplayText(item, "click"), "alpha");

  applet._backendAccounts.alpha = { tag: "BACKEND" };
  applet._styleTargets["alpha:9"] = {panel: true, hover: false, click: false};
  applet._displaySettings.alpha.panel = 1;
  assert.equal(applet._accountDisplayText(item, "panel"), "BACKEND");
});

test("account identity elements can be disabled per surface", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:8"] = {panel: true, hover: false, click: true};

  assert.equal(applet._accountDisplayText({usage: {
    account: "alpha",
    label: "Private Account",
  }}, "hover"), "");
  assert.equal(applet._accountDisplayText({usage: {
    account: "alpha",
    label: "Private Account",
  }}, "panel"), "A");
});

test("percent target hides remaining value per surface", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:0"] = {panel: false, hover: true, click: true};

  assert.deepEqual(JSON.parse(JSON.stringify(applet._percentParts(
    {remaining: 80}, "alpha", "panel"
  ))), {plain: "", markup: ""});
  assert.equal(applet._percentParts({remaining: 80}, "alpha", "hover").plain, "80%");
});

test("display rows normalize hover and click separators", () => {
  const applet = makeApplet();

  assert.deepEqual(
    JSON.parse(JSON.stringify(applet._normalizeDisplayRow({
      account: "alpha",
      tag: "A",
      panel: 2,
      hover: 1,
      click: 1,
      "hover-separator": true,
      "click-separator": true,
    }, "alpha"))),
    {
      account: "alpha",
      tag: "A",
      panel: 2,
      hover: 1,
      click: 1,
      "hover-separator": true,
      "click-separator": true,
    }
  );
});

test("hover separator is inserted before marked account", () => {
  const applet = makeApplet();
  applet._displaySettings.beta["hover-separator"] = true;

  assert.match(applet._tooltipContent().plain, /────────\nBeta:[^\n]+\nAlpha:/);
});

test("hover tooltip puts reset duration on its own line", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:1"] = {panel: false, hover: true, click: true};
  applet._styleTargets["alpha:2"] = {panel: false, hover: true, click: true};
  applet._styleTargets["alpha:3"] = {panel: false, hover: true, click: true};

  const lines = applet._tooltipContent().plain.split("\n");
  const resetIndex = lines.findIndex((line) => line.indexOf("Reset 5h") >= 0);
  const durationIndex = lines.findIndex((line) => line.indexOf("Restzeit 5h") >= 0);
  assert.ok(resetIndex >= 0);
  assert.equal(durationIndex, resetIndex + 1);
});

test("click separator is inserted before marked account", () => {
  const applet = makeApplet();
  applet.menu = {
    items: [],
    removeAll() { this.items = []; },
    addMenuItem(item) { this.items.push(item); },
    addAction() {},
  };
  applet._addAccount = (usage) => applet.menu.items.push({ account: usage.account });
  applet._addDisabled = () => ({ });

  applet._displaySettings.beta["click-separator"] = true;
  applet._buildUsageMenu();

  assert.deepEqual(
    applet.menu.items.map((item) => item.account || (item.isSeparator ? "separator" : "other")),
    ["separator", "alpha", "separator", "beta", "separator"]
  );
});

test("device login live events survive output buffer truncation", () => {
  const applet = makeApplet();
  applet._buildUsageMenu = () => {};
  applet._deviceLoginLiveAccount = "alpha";
  applet._deviceLoginEvents.alpha = [
    { kind: "url", value: "https://auth.example/device" },
  ];
  applet._deviceLoginLiveText.alpha = {
    stdout: "x".repeat(4090),
    stderr: "",
  };

  applet._recordDeviceLoginChunk("stdout", " device code: ABCD-1234\n");

  assert.equal(JSON.stringify(applet._deviceLoginEvents.alpha), JSON.stringify([
    { kind: "url", value: "https://auth.example/device" },
    { kind: "code", value: "ABCD-1234" },
  ]));
  assert.equal(applet._deviceLoginLiveText.alpha.stdout.length, 4096);
  assert.equal(applet._deviceLoginLiveText.alpha.stderr, "");
});

test("accounts without a Spark limit show no Spark and ignore edits", () => {
  const applet = makeAccountSettingsApplet();
  applet._usages = [usageWithoutSparkLimit("alpha")];

  const normalized = applet._normalizeAlertRow({
    account: "alpha",
    "five-threshold": 20,
    "weekly-threshold": 30,
    "spark-threshold": "45",
    warnings: true,
    errors: true,
  }, "alpha");

  assert.equal(normalized["spark-threshold"], "no Spark");
});

test("alert helper matrix distinguishes missing, monthly and Spark windows", () => {
  const applet = makeAccountSettingsApplet();
  const monthly = {
    name: "30d",
    duration_seconds: 2592000,
    remaining: 74,
  };
  applet._usages = [{
    account: "alpha",
    status: "ok",
    stale: false,
    five_hour: { name: "5h", duration_seconds: 18000, remaining: 61 },
    weekly: { name: "weekly", duration_seconds: 604800, remaining: 52 },
    main: { available: true, windows: [monthly] },
    models: {
      "gpt-5.3-codex-spark": {
        available: true,
        windows: [{
          name: "5h",
          duration_seconds: 18000,
          remaining: 44,
        }],
      },
    },
  }];

  assert.equal(applet._usageForAccount("alpha").account, "alpha");
  assert.equal(applet._usageForAccount("missing"), null);
  assert.equal(applet._alertWindowAvailable(applet._usages[0], "five"), true);
  assert.equal(applet._alertWindowAvailable(applet._usages[0], "weekly"), true);
  assert.equal(applet._alertWindowAvailable(applet._usages[0], "monthly"), true);
  assert.equal(applet._alertWindowAvailable(null, "monthly"), false);
  assert.equal(applet._sparkLimitState(applet._usages[0]), "present");
  assert.equal(applet._sparkLimitState({account: "alpha", status: "partial", models: {}}), "unknown");
  assert.equal(applet._sparkLimitState({account: "alpha", status: "ok", models: {}}), "none");

  assert.equal(applet._alertThresholdValue(40, true, "missing"), "40");
  assert.equal(applet._alertThresholdValue(40, false, "missing"), "missing");
  assert.equal(applet._normalizeAlertThreshold(undefined, true, "missing", 23), "23");
  assert.equal(applet._normalizeAlertThreshold(101, true, "missing", 23), null);
  assert.equal(applet._normalizeAlertThreshold(101, false, "missing", 23), "missing");
  assert.equal(applet._normalizeSparkThreshold("45", "present"), "45");
  assert.equal(applet._normalizeSparkThreshold("45", "unknown"), "45");
  assert.equal(applet._normalizeSparkThreshold("45", "none"), "no Spark");
  assert.equal(applet._normalizeSparkThreshold("101", "present"), "20");
});

test("account and cache helpers keep identity and byte boundaries strict", () => {
  const applet = makeApplet();

  const account = {
    account: "alpha",
    label: "Alpha",
    tag: "A",
    "auth-json": "file:///tmp/auth",
    "profile-dir": "file:///tmp/profile",
    "test-home": true,
    browser: 0,
    "reactivation-browser": 0,
    series: "B",
    "series-active": true,
    backend: 0,
  };
  assert.equal(applet._accountRowsEqual(account, Object.assign({}, account)), true);
  assert.equal(applet._accountRowsEqual(
    account,
    Object.assign({}, account, {"test-home": 1})
  ), false);
  assert.equal(applet._accountRowsEqual(
    account,
    Object.assign({}, account, {"series-active": "true"})
  ), false);
  assert.equal(applet._isTestHomeProfile(""), false);
  assert.equal(applet._isTestHomeProfile(42), false);

  assert.equal(applet._staleAfterMs(), 360000);
  applet.refreshInterval = 60;
  assert.equal(applet._staleAfterMs(), 120000);
  applet.refreshInterval = 9999;
  assert.equal(applet._staleAfterMs(), 3660000);

  assert.equal(applet._auxRequestKey(["codex-usage", "account", "alpha"]),
    "codex-usage\u0000account\u0000alpha");
  const euro = new Uint8Array(Buffer.from("€", "utf8"));
  const first = applet._decodeLiveUtf8Chunk(new Uint8Array(0), euro.subarray(0, 2));
  assert.equal(first.text, "");
  const second = applet._decodeLiveUtf8Chunk(first.pending, euro.subarray(2));
  assert.equal(second.text, "€");
  assert.equal(second.pending.length, 0);
});

test("consumption window rendering keeps delta, coverage, baseline and token end independent", () => {
  const applet = makeApplet();
  const row = applet._defaultConsumptionRow("alpha");
  row["show-panel"] = true;
  row["show-tooltip"] = true;
  row.format = "compact-token";
  row["baseline-enabled"] = true;
  row["baseline-minutes"] = 60;
  row["show-coverage-marker"] = true;
  row["forecast-show-panel"] = true;
  row["forecast-show-tooltip"] = true;
  applet.showConsumptionDelta = true;

  const rendered = applet._consumptionWindowPart({
    pool: "main",
    limit_window_seconds: 18000,
    consumed_percentage_points: 3.25,
    coverage: "complete",
    baseline_used_percent: 2.5,
    estimated_seconds_to_exhaustion: 3540,
  }, row, "panel", null);

  assert.match(rendered.plain, /Δ1S3,3P/);
  assert.match(rendered.plain, /vollständig/);
  assert.match(rendered.plain, /TE=/);

  applet._consumptionSettings.alpha = row;
  const usage = {
    account: "alpha",
    cost_windows: [{
      pool: "main",
      limit_window_seconds: 18000,
      consumed_percentage_points: 3.25,
      coverage: "complete",
      baseline_used_percent: 2.5,
      estimated_seconds_to_exhaustion: 3540,
    }],
  };
  const parentRendered = applet._consumptionParts(usage, "panel");
  assert.match(parentRendered.plain, /AW60m=2,5%/);

  applet.showConsumptionDelta = false;
  const withoutDelta = applet._consumptionWindowPart(usage.cost_windows[0], row, "panel", null);
  assert.doesNotMatch(withoutDelta.plain, /Δ1S/);
  row.format = "custom";
  row["custom-format"] = "C {value} {window} {coverage}";
  const custom = applet._consumptionWindowPart(usage.cost_windows[0], row, "panel", null);
  assert.match(custom.plain, /^C 3,3 5h vollständig/);

  row["hide-when-zero"] = true;
  assert.equal(applet._consumptionWindowPart({
    pool: "main",
    limit_window_seconds: 18000,
    consumed_percentage_points: 0,
    coverage: "complete",
  }, row, "panel", null), null);

  row["hide-when-zero"] = false;
  const insufficient = applet._consumptionWindowPart({
    pool: "main",
    limit_window_seconds: 18000,
    consumed_percentage_points: 3,
    coverage: "insufficient",
  }, row, "panel", null);
  assert.match(insufficient.plain, /nicht genügend Messdaten/);
});

test("process cleanup clears generations, timers, live login state and reactivations", () => {
  const applet = makeApplet();
  let primaryForced = 0;
  applet._process = { force_exit: () => { primaryForced += 1; } };
  applet._primaryRequest = {kind: "fresh"};
  applet._generation = 4;
  applet._setSource("_timeoutId", 10);
  applet._cancelProcess();
  assert.equal(primaryForced, 1);
  assert.equal(applet._generation, 5);
  assert.equal(applet._process, null);
  assert.equal(applet._primaryRequest, null);
  assert.equal(applet._timeoutId, 0);

  let auxForced = 0;
  applet._auxProcess = { force_exit: () => { auxForced += 1; } };
  applet._auxCommand = "device-login";
  applet._auxGeneration = 2;
  applet._deviceLoginLiveAccount = "alpha";
  applet._deviceLoginActive.alpha = true;
  applet._deviceLoginJobs = {};
  applet._deviceLoginLiveText.alpha = {stdout: "partial", stderr: "error"};
  applet._setSource("_auxTimeoutId", 11);
  applet._cancelAuxProcess();
  assert.equal(auxForced, 1);
  assert.equal(applet._auxGeneration, 3);
  assert.equal(applet._auxProcess, null);
  assert.equal(applet._auxCommand, "");
  assert.equal(applet._deviceLoginLiveAccount, "");
  assert.deepEqual(JSON.parse(JSON.stringify(applet._deviceLoginLiveText)), {});
  assert.equal(applet._deviceLoginActive.alpha, undefined);
  assert.equal(applet._auxTimeoutId, 0);

  let reactivationForced = 0;
  applet._reactivations.alpha = {
    done: false,
    timeoutId: 12,
    process: {force_exit: () => { reactivationForced += 1; }},
  };
  applet._reactivationErrors.alpha = "old error";
  applet._cancelReactivation("alpha");
  assert.equal(reactivationForced, 1);
  assert.equal(applet._reactivations.alpha, undefined);
  assert.equal(applet._reactivationErrors.alpha, undefined);
  applet._cancelReactivations();
  assert.deepEqual(JSON.parse(JSON.stringify(applet._reactivations)), {});
});

test("usage refresh re-normalizes Spark threshold after backend overview", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = { alpha: { account: "alpha" } };
  applet.accountAlertSettings = [{
    account: "alpha",
    "five-threshold": 20,
    "weekly-threshold": 30,
    "spark-threshold": "45",
    warnings: true,
    errors: true,
  }];
  applet._mergeFreshPayload = () => [usageWithoutSparkLimit("alpha")];
  applet._buildUsageMenu = () => {};
  applet._updatePanel = () => {};
  applet._refreshConsumption = () => {};
  applet._notifyForPayload = () => {};

  applet._applyPayload([], true);

  assert.equal(applet.accountAlertSettings[0]["spark-threshold"], "no Spark");
});

test("usage refresh restores editable Spark threshold when limit returns", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = { alpha: { account: "alpha" } };
  applet.accountAlertSettings = [{
    account: "alpha",
    "five-threshold": 20,
    "weekly-threshold": 30,
    "spark-threshold": "no Spark",
    warnings: true,
    errors: true,
  }];
  applet._mergeFreshPayload = () => [usageWithSparkWindows("alpha", { five: 80, weekly: 70 })];
  applet._buildUsageMenu = () => {};
  applet._updatePanel = () => {};
  applet._refreshConsumption = () => {};
  applet._notifyForPayload = () => {};

  applet._applyPayload([], true);

  assert.equal(applet.accountAlertSettings[0]["spark-threshold"], "20");
});

test("Spark notification uses dedicated Spark threshold", () => {
  const applet = makeAccountSettingsApplet();
  applet._alertSettings = { alpha: {
    account: "alpha",
    "five-threshold": 20,
    "weekly-threshold": 20,
    "spark-threshold": "40",
    warnings: true,
    errors: true,
  } };
  applet._usages = [usageWithSparkWindows("alpha", { five: 35, weekly: 90 })];
  applet.notifyWarnings = false;

  applet._notifyForPayload();

  assert.equal(applet._warningState["alpha:Spark 5h"], true);
});

test("Spark panel sources use dedicated threshold", () => {
  const applet = makeAccountSettingsApplet();
  applet._alertSettings = { alpha: {
    account: "alpha",
    "five-threshold": 10,
    "weekly-threshold": 15,
    "spark-threshold": "40",
    warnings: true,
    errors: true,
  } };
  const usage = usageWithSparkWindows("alpha", { five: 35, weekly: 90 });

  assert.equal(applet._panelThreshold({ usage }, 4), 40);
  assert.equal(applet._panelThreshold({ usage }, 6), 40);
});

test("unknown Spark data preserves numeric threshold", () => {
  const applet = makeAccountSettingsApplet();
  applet._usages = [{
    account: "alpha",
    status: "partial",
    models: {
      "gpt-5.3-codex-spark": { available: false, windows: [] },
    },
  }];

  const normalized = applet._normalizeAlertRow({
    account: "alpha",
    "five-threshold": 20,
    "weekly-threshold": 30,
    "spark-threshold": "45",
    warnings: true,
    errors: true,
  }, "alpha");

  assert.equal(normalized["spark-threshold"], "45");
});

test("legacy alert rows receive Spark state without changing other thresholds", () => {
  const applet = makeApplet();
  applet._backendAccounts = { alpha: { account: "alpha" } };
  applet._usages = [usageWithoutSparkLimit("alpha")];

  const rows = applet._mergedAlertRows([applet._backendAccounts.alpha], [{
    account: "alpha",
    "five-threshold": 12,
    "weekly-threshold": 34,
    warnings: true,
    errors: false,
  }]);

  assert.deepEqual(JSON.parse(JSON.stringify(rows[0])), {
    account: "alpha",
    "five-threshold": "no 5h",
    "weekly-threshold": "no Woche",
    "monthly-threshold": "no 30d",
    "spark-threshold": "no Spark",
    warnings: true,
    errors: false,
  });
});

test("legacy panel tag is removed from stored panel row after display migration", () => {
  const applet = makeApplet();
  const rows = applet._mergedPanelRows([applet._backendAccounts.alpha], [{
    account: "alpha",
    tag: "AA",
    order: 1,
    muted: false,
    slot1: 3,
    slot2: 0,
  }]);

  assert.equal(Object.prototype.hasOwnProperty.call(rows[0], "tag"), false);
});

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

test("panel settings follow the Abrufwege account order", () => {
  const applet = makeApplet();
  applet.accountBackends = [
    {account: "alpha"},
    {account: "beta"},
  ];
  const rows = applet._mergedPanelRows([
    {account: "beta"},
    {account: "alpha"},
  ], []);
  assert.equal(Array.from(rows, (row) => row.account).join(","), "alpha,beta");
});

test("panel slots can render credits and calculated credit consumption", () => {
  const applet = makeApplet();
  const alpha = applet._usages[0];
  alpha.credits = { name: "credits", used: null, limit: null, remaining: 794, percent: null };
  alpha.cost_windows = [{
    pool: "credits",
    lookback_seconds: 3600,
    limit_window_seconds: 2592000,
    consumed_percentage_points: 12.3,
    coverage: "complete",
    sample_count: 3,
    estimated_seconds_to_exhaustion: null,
  }];
  applet._panelSettings.alpha = {
    account: "alpha", order: 1, muted: false, slot1: 9, slot2: 10, slot3: 0, slot4: 0,
  };
  applet._creditSettings = {
    alpha: {
      account: "alpha", "show-panel": false, "show-tooltip": true,
      format: "compact", "custom-format": "", "hide-when-zero": false,
      "consumption-show-panel": false, "consumption-show-tooltip": true,
      "consumption-amount": 1, "consumption-unit": "hours",
      "consumption-format": "compact", "consumption-custom-format": "",
      "consumption-hide-when-zero": false,
      "consumption-show-coverage-marker": true,
    },
  };

  const items = applet._panelItems();
  assert.equal(
    applet._panelContent(items.filter((item) => item.visible)).plain,
    "A CR 794 · Verbrauch – / CV Δ1 h 12,3 Credit-% (vollständig)"
  );
});

test("custom credit consumption retains the enabled coverage marker", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:12"] = {panel: true, hover: true, click: true};
  applet._creditSettings = {alpha: {
    account: "alpha", "consumption-show-panel": true,
    "consumption-show-tooltip": true, "consumption-amount": 1,
    "consumption-unit": "hours", "consumption-format": "custom",
    "consumption-custom-format": "CV {period}: {value}%",
    "consumption-hide-when-zero": false,
    "consumption-show-coverage-marker": true,
    "consumption-baseline-enabled": false,
  }};
  applet._usages[0].cost_windows = [{
    pool: "credits", lookback_seconds: 3600, limit_window_seconds: 2592000,
    consumed_percentage_points: 12.3, coverage: "partial", sample_count: 2,
  }];
  const rendered = applet._creditConsumptionParts(applet._usages[0], "panel");
  assert.equal(rendered.plain, "CV 1 h: 12,3% (mindestens)");
});

test("credit balances are displayed as whole numbers", () => {
  const applet = makeApplet();
  const alpha = applet._usages[0];
  alpha.credits = {
    name: "credits", used: 19.6, limit: 999.4, remaining: 794.4, percent: null,
  };
  applet._panelSettings.alpha = {
    account: "alpha", order: 1, muted: false, slot1: 9, slot2: 0, slot3: 0, slot4: 0,
  };
  applet._creditSettings = {
    alpha: {
      account: "alpha", "show-panel": true, "show-tooltip": true,
      format: "verbose", "custom-format": "", "hide-when-zero": false,
    },
  };

  const items = applet._panelItems();
  assert.equal(
    applet._panelContent(items.filter((item) => item.visible)).plain,
    "A CR: 794 / 999 (Verbrauch 20, –%)"
  );
});

test("credit hide-when-zero does not hide a positive balance with zero usage", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:11"] = {panel: true, hover: true, click: true};
  applet._creditSettings = {alpha: {
    account: "alpha", "show-panel": true, "show-tooltip": true,
    format: "compact", "hide-when-zero": true,
    "show-coverage-marker": false, "baseline-enabled": false,
  }};
  applet._usages[0].credits = {remaining: 794, limit: 1000, used: 0, percent: 0};
  const rendered = applet._creditParts(applet._usages[0], "panel");
  assert.ok(rendered);
  assert.equal(rendered.plain, "CR 794");
});

test("credit hover omits credit consumption when its hover setting is disabled", () => {
  const applet = makeApplet();
  applet._usages[0].credits = {
    name: "credits", used: null, limit: null, remaining: 794, percent: null,
  };
  applet._usages[1].credits = {
    name: "credits", used: null, limit: null, remaining: 321, percent: null,
  };
  applet._creditSettings = {
    alpha: {
      account: "alpha", "show-panel": false, "show-tooltip": true,
      format: "compact", "custom-format": "", "hide-when-zero": false,
      "consumption-show-panel": false, "consumption-show-tooltip": false,
    },
    beta: {
      account: "beta", "show-panel": false, "show-tooltip": true,
      format: "compact", "custom-format": "", "hide-when-zero": false,
      "consumption-show-panel": false, "consumption-show-tooltip": false,
    },
  };
  assert.equal(applet._creditParts(applet._usages[0], "hover").plain, "Credits 794");
  assert.equal(applet._creditParts(applet._usages[1], "hover").plain, "Credits 321");
});

test("panel identity target keeps account visible when all value slots are off", () => {
  const applet = makeApplet();
  applet._panelSettings.alpha = {
    account: "alpha",
    tag: "A",
    order: 1,
    muted: false,
    slot1: 0,
    slot2: 0,
  };
  applet._displaySettings.alpha = {
    account: "alpha",
    tag: "A",
    panel: 0,
    hover: 1,
    click: 1,
  };
  applet._styleTargets["alpha:7"] = {
    panel: true,
    hover: false,
    click: false,
  };

  const items = applet._panelItems();
  const alpha = items.find((item) => item.usage.account === "alpha");

  assert.equal(alpha.visible, true);
  assert.equal(
    applet._panelContent(items.filter((item) => item.visible)).plain,
    "alpha"
  );
});

test("consumption DTO is validated and rendered with coverage marker", () => {
  const applet = makeApplet();
  const [usage] = applet._validatePayload([{
    account: "alpha",
    captured_at: new Date().toISOString(),
    backend_configured: "direct",
    backend_used: "direct",
    five_hour: { name: "5h", remaining: 80 },
    weekly: { name: "weekly", remaining: 60 },
    status: "ok",
    stale: false,
    cache_invalidated: false,
    cost_windows: [{
      lookback_seconds: 3600,
      pool: "main",
      limit_window_seconds: 18000,
      consumed_percentage_points: 9.3,
      coverage: "partial",
      sample_count: 4,
    }],
  }]);
  applet._consumptionSettings.alpha = {
    account: "alpha",
    "show-panel": true,
    "show-tooltip": true,
    amount: 1,
    unit: "hours",
    "limit-window": "short",
    format: "compact",
    "custom-format": "",
    "hide-when-zero": false,
    "show-coverage-marker": true,
  };
  assert.equal(
    applet._consumptionParts(usage, "panel").plain,
    "Δ1 h 9,3% (mindestens) TE=—"
  );
  assert.throws(() => applet._safeConsumptionWindows([{
    lookback_seconds: 3600,
    pool: "main",
    limit_window_seconds: 18000,
    consumed_percentage_points: 10001,
    coverage: "complete",
    sample_count: 2,
  }]), /invalid consumption window/);
  assert.throws(() => applet._safeConsumptionWindows([{
    lookback_seconds: 3600,
    pool: "main",
    limit_window_seconds: 18000,
    consumed_percentage_points: 10,
    estimated_seconds_to_exhaustion: -1,
    coverage: "complete",
    sample_count: 2,
  }]), /invalid consumption window/);
});

test("consumption forecast is rendered from backend DTO", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:4"] = {panel: true, hover: true, click: true};
  applet._styleTargets["alpha:5"] = {panel: true, hover: true, click: true};
  applet._consumptionSettings.alpha = {
    account: "alpha",
    "show-panel": true,
    "show-tooltip": true,
    amount: 1,
    unit: "hours",
    "limit-window": "short",
    format: "compact",
    "custom-format": "",
    "hide-when-zero": false,
    "show-coverage-marker": true,
  };
  const usage = applet._usages[0];
  usage.cost_windows = [{
    lookback_seconds: 3600,
    pool: "main",
    limit_window_seconds: 18000,
    consumed_percentage_points: 30,
    estimated_seconds_to_exhaustion: 3000,
    coverage: "complete",
    sample_count: 3,
  }];

  const rendered = applet._consumptionParts(usage, "panel");

  assert.match(rendered.plain, /TE=/);
  assert.match(rendered.plain, /0,8h/);
});

test("own baseline does not hide delta or token end", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:4"] = {panel: true, hover: true, click: true};
  applet._styleTargets["alpha:5"] = {panel: true, hover: true, click: true};
  applet._consumptionSettings.alpha = {
    account: "alpha", "show-panel": true, "show-tooltip": true,
    amount: 1, unit: "hours", "limit-window": "short",
    format: "compact", "custom-format": "", "hide-when-zero": false,
    "show-coverage-marker": true, "baseline-enabled": true,
    "baseline-minutes": 60,
  };
  const usage = applet._usages[0];
  usage.cost_windows = applet._safeConsumptionWindows([{
    pool: "main", lookback_seconds: 3600, limit_window_seconds: 18000,
    consumed_percentage_points: 12.5, baseline_used_percent: 40,
    estimated_seconds_to_exhaustion: 600, coverage: "complete", sample_count: 3,
  }]);

  const rendered = applet._consumptionParts(usage, "panel");
  assert.match(rendered.plain, /Δ1 h 12,5%/);
  assert.match(rendered.plain, /TE=0,2h/);
  assert.match(rendered.plain, /AW60m=40,0%/);
});

test("enabled own baseline remains visible when the legacy baseline target is disabled", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:4"] = {panel: true, hover: true, click: true};
  applet._styleTargets["alpha:5"] = {panel: true, hover: true, click: true};
  applet._styleTargets["alpha:13"] = {panel: false, hover: false, click: false};
  applet._consumptionSettings.alpha = {
    account: "alpha", "show-panel": true, "show-tooltip": true,
    amount: 1, unit: "hours", "limit-window": "short", format: "compact",
    "custom-format": "", "hide-when-zero": false,
    "show-coverage-marker": false, "baseline-enabled": true,
    "baseline-minutes": 60,
  };
  applet._usages[0].cost_windows = [{
    pool: "main", lookback_seconds: 3600, limit_window_seconds: 18000,
    consumed_percentage_points: 12.5, baseline_used_percent: 40,
    estimated_seconds_to_exhaustion: 600, coverage: "complete", sample_count: 3,
  }];
  const rendered = applet._consumptionParts(applet._usages[0], "panel");
  assert.match(rendered.plain, /AW60m=40,0%/);
});

test("token end keeps its own baseline and compact duration is decimal hours", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:4"] = {panel: true, hover: true, click: true};
  applet._styleTargets["alpha:5"] = {panel: true, hover: true, click: true};
  applet._consumptionSettings.alpha = {
    account: "alpha", "show-panel": true, "show-tooltip": true,
    amount: 1, unit: "hours", "limit-window": "short",
    format: "compact", "custom-format": "", "hide-when-zero": false,
    "show-coverage-marker": false, "baseline-enabled": false,
    "baseline-minutes": 60, "forecast-show-panel": true,
    "forecast-show-tooltip": true, "forecast-limit-window": "short",
    "forecast-format": "compact", "forecast-custom-format": "",
    "forecast-show-coverage-marker": false, "forecast-baseline-enabled": true,
    "forecast-baseline-minutes": 30, "forecast-hide-when-zero": false,
    "forecast-smoothing": "ema-20", "forecast-warn-amount": 2,
    "forecast-warn-unit": "hours", "forecast-warn-format": "none"
  };
  const usage = applet._usages[0];
  usage.cost_windows = applet._safeConsumptionWindows([{
    pool: "main", lookback_seconds: 3600, limit_window_seconds: 18000,
    consumed_percentage_points: 12.56, baseline_used_percent: 33.3,
    estimated_seconds_to_exhaustion: 354 * 60, coverage: "complete", sample_count: 3,
  }]);

  const rendered = applet._consumptionParts(usage, "panel");
  assert.match(rendered.plain, /Δ1 h 12,6%/);
  assert.match(rendered.plain, /TE=5,9h/);
  assert.match(rendered.plain, /AW30m=33,3%/);
  assert.equal(applet._formatDurationPart(354, 0, true), "5,9h");
  assert.equal(applet._formatDurationPart(354, 0, false), "5h 54m");
  assert.equal(applet._formatDurationPart(150, 0), "2h 30m");
});

test("custom token-end format is preserved in visible markup", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:5"] = {panel: true, hover: true, click: true};
  const rendered = applet._forecastWindowPart({
    estimated_seconds_to_exhaustion: 354 * 60, coverage: "complete",
  }, {
    account: "alpha", "show-panel": true, "show-tooltip": true,
    format: "custom", "custom-format": "Ende in {duration}",
    "show-coverage-marker": false, "baseline-enabled": false,
    "forecast-warn-amount": 0, "forecast-warn-unit": "hours",
  }, "panel", 50);
  assert.equal(rendered.plain, "Ende in 5h 54m");
  assert.match(rendered.markup, /Ende in 5h 54m/);
  assert.doesNotMatch(rendered.markup, /Zeit bis Tokenende/);
});

test("missing token-end estimate does not duplicate its configured baseline in markup", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:5"] = {panel: true, hover: true, click: true};
  const rendered = applet._forecastWindowPart({
    estimated_seconds_to_exhaustion: null, coverage: "stale", baseline_used_percent: 42,
  }, {
    account: "alpha", "show-panel": true, "show-tooltip": true,
    format: "compact", "show-coverage-marker": false,
    "baseline-enabled": true, "baseline-minutes": 60,
    "forecast-warn-amount": 0, "forecast-warn-unit": "hours",
  }, "panel", 50);
  assert.equal(rendered.plain, "TE=— AW60m=42,0%");
  assert.equal((rendered.markup.match(/AW60m=42,0%/g) || []).length, 1);
});

test("emergency display override enables delta and selects its window without mutating settings", () => {
  const applet = makeApplet();
  applet.showConsumptionDelta = false;
  applet._readEmergencyDisplayOverride = () => ({limit_window: "weekly"});
  applet._styleTargets["alpha:4"] = {panel: true, hover: true, click: true};
  applet._consumptionSettings.alpha = {
    account: "alpha", "show-panel": false, "show-tooltip": false,
    amount: 1, unit: "hours", "limit-window": "short", format: "compact",
    "custom-format": "", "hide-when-zero": false,
    "show-coverage-marker": false, "baseline-enabled": false,
    "baseline-minutes": 60
  };
  const usage = applet._usages[0];
  usage.cost_windows = [{
    pool: "main", lookback_seconds: 3600, limit_window_seconds: 604800,
    consumed_percentage_points: 4.25, estimated_seconds_to_exhaustion: null,
    coverage: "complete", sample_count: 3
  }];
  const rendered = applet._consumptionParts(usage, "panel");
  assert.match(rendered.plain, /Δ1 h 4,3%/);
  assert.equal(applet._consumptionSettings.alpha["limit-window"], "short");
});

test("fast-mode state helpers report active accounts, flex events and safe defaults", () => {
  const applet = makeApplet();
  applet._fastModeState = {
    modes: {
      beta: {state: "active"},
      alpha: {state: "idle"},
      malformed: "active",
    },
    last_event: {mode: "flex", account: "old", reason: "Reset"},
  };
  assert.equal(applet._fastModeIsActive(), true);
  assert.equal(
    applet._fastModeStatusText(),
    "⚠ Fast-Modus aktiv · beta · Hinweis alle 15 Minuten"
  );

  applet._fastModeState = {
    modes: {alpha: {state: "idle"}},
    last_event: {mode: "flex", account: "alpha", reason: "Reset"},
  };
  assert.equal(applet._fastModeIsActive(), false);
  assert.equal(applet._fastModeStatusText(), "⚠ Flex-Modus · alpha · Reset");

  applet._fastModeState = null;
  assert.equal(applet._fastModeIsActive(), false);
  assert.equal(applet._fastModeStatusText(), "");
  applet._readFastModeState = () => ({modes: {gamma: {state: "active"}}, last_event: null});
  applet._refreshFastModeState();
  assert.equal(applet._fastModeIsActive(), true);
  assert.equal(applet._readEmergencyDisplayOverride(""), null);
  assert.equal(applet._readEmergencyDisplayOverride(42), null);
});

test("error notification persistence retries failed writes and menu markup stays bounded", () => {
  const applet = makeApplet();
  const writes = [];
  let fail = true;
  applet.settings = {
    setValue: (key, value) => {
      writes.push([key, value]);
      if (fail) {
        throw new Error("settings unavailable");
      }
    },
  };
  applet._persistErrorNotificationState("{\"x\":1}");
  assert.equal(applet._errorNotificationStateWritePending, "{\"x\":1}");
  assert.equal(writes.length, 1);
  fail = false;
  applet._retryErrorNotificationStateWrite();
  assert.equal(applet._errorNotificationStateWritePending, null);
  assert.deepEqual(JSON.parse(JSON.stringify(writes)), [
    ["error-notification-state", "{\"x\":1}"],
    ["error-notification-state", "{\"x\":1}"],
  ]);

  const menu = {items: [], addMenuItem: (item) => menu.items.push(item)};
  const disabled = applet._addDisabled(menu, "x".repeat(400), "codex-usage-detail");
  assert.equal(menu.items.length, 1);
  assert.equal(disabled.label.text.length, 240);
  assert.equal(disabled.label.text.endsWith("…"), true);
  let markup = null;
  applet._setItemMarkup({
    label: {clutter_text: {set_markup: (value) => { markup = value; }}},
  }, "<b>safe</b>");
  assert.equal(markup, "<b>safe</b>");
});

test("account control and terminal menu actions dispatch their guarded callbacks", () => {
  const applet = makeApplet();
  const usage = applet._usages[0];
  applet._panelSettings.alpha = {
    account: "alpha", muted: false, order: 1, slot1: 1, slot2: 0,
  };
  applet._alertSettings.alpha = {
    account: "alpha", warnings: true, errors: true,
    "five-threshold": "20", "weekly-threshold": "20",
    "monthly-threshold": "20", "spark-threshold": "no Spark",
  };
  const calls = [];
  applet._updateAccountPanelSetting = (account, changes) => calls.push([account, "panel", changes]);
  applet._updateAccountAlertSetting = (account, changes) => calls.push([account, "alert", changes]);
  applet._startDeviceLogin = (value) => calls.push([value.account, "login"]);
  applet._manageAccount = (value) => calls.push([value.account, "manage"]);
  applet._startAccountTerminal = (value) => calls.push([value.account, "terminal"]);

  const target = {items: [], addMenuItem: (item) => target.items.push(item)};
  applet._addAccountControls(usage, target);
  applet._addAccountTerminalAction(usage, target);
  const submenu = target.items[0];
  assert.equal(submenu.label.text, "Alpha steuern");

  const [visible, warnings, errors, login, manage] = submenu.menu.items;
  visible.state = false;
  visible.emit("toggled");
  warnings.state = false;
  warnings.emit("toggled");
  errors.state = false;
  errors.emit("toggled");
  login.emit("activate");
  manage.emit("activate");
  target.items[1].emit("activate");

  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    ["alpha", "panel", {muted: true}],
    ["alpha", "alert", {warnings: false}],
    ["alpha", "alert", {errors: false}],
    ["alpha", "login"],
    ["alpha", "manage"],
    ["alpha", "terminal"],
  ]);

  const activeTarget = {items: [], addMenuItem: (item) => activeTarget.items.push(item)};
  applet._deviceLoginActive.alpha = true;
  applet._cancelDeviceLogin = (account) => calls.push([account, "cancel"]);
  applet._addAccountControls(usage, activeTarget);
  const cancel = activeTarget.items[0].menu.items.find((item) => item.label.text === "Device-Login abbrechen");
  cancel.emit("activate");
  assert.deepEqual(calls[calls.length - 1], ["alpha", "cancel"]);
});

test("account menu adds reset and dynamic limit details without mixing windows", () => {
  const applet = makeApplet();
  const target = {items: [], addMenuItem: (item) => target.items.push(item)};
  const usage = {
    account: "alpha",
    five_hour: {name: "5h", duration_seconds: 18000, remaining: 88},
    weekly: {name: "weekly", duration_seconds: 604800, remaining: 77},
    main: {
      windows: [
        {name: "weekly", duration_seconds: 604800, remaining: 71},
        {name: "monthly", duration_seconds: 2592000, remaining: 63},
      ],
    },
    models: {
      "gpt-5.3-codex-spark": {
        windows: [
          {name: "5h", duration_seconds: 18000, remaining: 42},
          {name: "weekly", duration_seconds: 604800, remaining: 37},
        ],
      },
    },
  };
  applet._routingDecisions = {
    alpha: {decision: "spark", policy_source: "account"},
  };
  applet._backendSummary = () => "Direkt";
  applet._windowResetParts = (window) => window ? {
    plain: "in 1h",
    markup: "in 1h",
  } : {plain: "", markup: ""};
  applet._percentParts = (window) => window ? {
    plain: `${window.remaining}%`,
    markup: `${window.remaining}%`,
  } : {plain: "", markup: ""};

  applet._addResetDetail(usage, target);
  applet._addDynamicLimitDetails(usage, target);

  assert.equal(target.items.length, 4);
  assert.match(target.items[0].label.text, /5h Reset/);
  assert.match(target.items[0].label.text, /Woche Reset/);
  assert.match(target.items[0].label.text, /30d Reset/);
  assert.match(target.items[0].label.text, /Abruf Direkt/);
  assert.equal(target.items[1].label.text, "Monat 63% (in 1h)");
  assert.equal(target.items[2].label.text, "Spark 5h 42% (in 1h) · Woche 37% (in 1h)");
  assert.equal(target.items[3].label.text, "Routing Spark · Regel account");
});

test("reactivation menu reports running and failed states and starts only once", () => {
  const applet = makeApplet();
  const usage = {account: "alpha", label: "Alpha"};
  const target = {items: [], addMenuItem: (item) => target.items.push(item)};
  applet._reactivations.alpha = {process: {}};
  applet._addReactivationAction(usage, target);
  assert.equal(target.items.length, 1);
  assert.match(target.items[0].label.text, /Login läuft/);

  delete applet._reactivations.alpha;
  applet._reactivationErrors.alpha = "Browser nicht verfügbar";
  let starts = 0;
  applet._reactivateAccount = () => {
    if (applet._reactivations.alpha) {
      return;
    }
    starts += 1;
    applet._reactivations.alpha = {process: {}};
  };
  const retryTarget = {items: [], addMenuItem: (item) => retryTarget.items.push(item)};
  applet._addReactivationAction(usage, retryTarget);
  assert.equal(retryTarget.items.length, 2);
  assert.equal(retryTarget.items[0].label.text, "Alpha reaktivieren");
  assert.equal(retryTarget.items[1].label.text, "Browser nicht verfügbar");
  retryTarget.items[0].emit("activate");
  retryTarget.items[0].emit("activate");
  assert.equal(starts, 1);
});

test("health and common action menu callbacks use guarded commands", () => {
  const applet = makeApplet();
  const menu = {
    items: [],
    addMenuItem: (item) => menu.items.push(item),
    addAction: (label, callback) => {
      const item = {label: {text: label}, _signals: {}, connect(signal, handler) {
        this._signals[signal] = handler;
      }, emit(signal) {
        return this._signals[signal](this);
      }, setSensitive(value) { this.sensitive = value; }};
      menu.items.push(item);
      item.connect("activate", callback);
      return item;
    },
  };
  applet.menu = menu;
  applet._baseCommandArgv = () => ["codex-usage"];
  const calls = [];
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv.slice(1));
    callback({healthy: true}, null);
  };
  applet._addHealthAction(menu);
  menu.items[0].emit("activate");
  assert.deepEqual(calls, [["health", "--format", "json"]]);
  assert.equal(menu.items[1].label.text, '{"healthy":true}');

  let refreshes = 0;
  let analytics = 0;
  let settings = 0;
  applet._refreshFresh = () => { refreshes += 1; };
  applet._openAnalytics = () => { analytics += 1; };
  applet._openSettings = () => { settings += 1; };
  applet._refreshing = false;
  applet._addActions();
  menu.items.slice(2).forEach((item) => item.emit("activate"));
  assert.equal(refreshes, 1);
  assert.equal(analytics, 1);
  assert.equal(settings, 1);
});

test("settings launcher uses the applet instance and schedules bounded maximization", () => {
  const subprocessCalls = [];
  const scheduleCalls = [];
  const applet = makeApplet((runtime) => {
    runtime.subprocessFactory = (...args) => {
      subprocessCalls.push(args);
      return {};
    };
    runtime.timeoutAdd = (_milliseconds, callback) => {
      runtime.settingsCallbacks.push(callback);
      return runtime.settingsCallbacks.length;
    };
    runtime.settingsCallbacks = [];
  });
  const scheduleSettingsMaximize = applet._scheduleSettingsMaximize;
  applet._scheduleSettingsMaximize = () => { scheduleCalls.push("scheduled"); };
  applet.instanceId = 17;
  applet._openSettings();
  assert.deepEqual(scheduleCalls, ["scheduled"]);
  assert.equal(JSON.stringify(subprocessCalls[0]), JSON.stringify([
    ["xlet-settings", "applet", "codex-usage@H234598", "-i", "17"],
    0,
  ]));

  applet._scheduleSettingsMaximize = scheduleSettingsMaximize;
  applet._scheduleSettingsMaximize();
  assert.equal(applet._settingsMaximizeId, 1);
});

test("settings maximization retries up to twelve times and stops after removal", () => {
  const callbacks = [];
  const subprocessCalls = [];
  const applet = makeApplet((runtime) => {
    runtime.timeoutAdd = (_milliseconds, callback) => {
      callbacks.push(callback);
      return callbacks.length;
    };
    runtime.subprocessFactory = (...args) => {
      subprocessCalls.push(args);
      return {};
    };
  });
  applet._scheduleSettingsMaximize();
  assert.equal(callbacks.length, 1);
  for (let index = 0; index < 12; index += 1) {
    assert.equal(callbacks[0](), index < 11);
  }
  assert.equal(subprocessCalls.length, 12);
  assert.equal(subprocessCalls[0][0][0], "wmctrl");
  assert.deepEqual(
    Array.from(subprocessCalls[0][0].slice(1)),
    ["-r", "Codex Usage", "-b", "add,maximized_vert,maximized_horz"]
  );
  assert.equal(applet._settingsMaximizeId, 0);

  applet._removed = false;
  applet._scheduleSettingsMaximize();
  applet._removed = true;
  assert.equal(callbacks[1](), false);
  assert.equal(subprocessCalls.length, 12);
  assert.equal(applet._settingsMaximizeId, 0);
});

test("health action reports command and backend failures without retaining work", () => {
  const applet = makeApplet();
  const menu = {
    items: [],
    addMenuItem: (item) => menu.items.push(item),
    addAction: (label, callback) => {
      const item = {label: {text: label}, _signals: {}, connect(signal, handler) {
        this._signals[signal] = handler;
      }, emit(signal) {
        return this._signals[signal](this);
      }};
      menu.items.push(item);
      item.connect("activate", callback);
      return item;
    },
  };
  let error = "";
  applet._showCommandError = (value) => { error = value; };
  applet._baseCommandArgv = () => { throw new Error("health command missing"); };
  applet._addHealthAction(menu);
  menu.items[0].emit("activate");
  assert.match(error, /health command missing/);

  error = "";
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._spawnAuxJson = (_argv, callback) => callback(null, "health backend failed");
  menu.items[0].emit("activate");
  assert.equal(error, "health backend failed");
  assert.equal(menu.items.length, 1);
});

test("common actions disable refresh and expose systemd repair only when needed", () => {
  const applet = makeApplet();
  const menu = {
    items: [],
    addMenuItem: (item) => menu.items.push(item),
    addAction: (label, callback) => {
      const item = {label: {text: label}, _signals: {}, connect(signal, handler) {
        this._signals[signal] = handler;
      }, emit(signal) {
        return this._signals[signal](this);
      }, setSensitive(value) { this.sensitive = value; }};
      menu.items.push(item);
      item.connect("activate", callback);
      return item;
    },
  };
  applet.menu = menu;
  applet._refreshing = true;
  applet.pollOwner = "systemd";
  applet._serviceChecked = true;
  applet._systemdActive = false;
  let enabled = 0;
  applet._enableBackgroundService = () => { enabled += 1; };
  applet._addActions();
  assert.equal(menu.items[0].sensitive, false);
  assert.equal(menu.items[1].label.text, "Hintergrunddienst aktivieren");
  menu.items[1].emit("activate");
  assert.equal(enabled, 1);
});

test("panel click opens the menu and refreshes only when it was closed", () => {
  const applet = makeApplet();
  let toggles = 0;
  let builds = 0;
  let refreshes = 0;
  applet.menu = {
    isOpen: false,
    toggle() { toggles += 1; },
  };
  applet._menuDirty = true;
  applet._buildUsageMenu = () => { builds += 1; };
  applet._usesAppletPolling = () => true;
  applet._refreshFresh = () => { refreshes += 1; };
  applet.on_applet_clicked();
  assert.equal(toggles, 1);
  assert.equal(builds, 1);
  assert.equal(refreshes, 1);

  applet.menu.isOpen = true;
  applet._menuDirty = false;
  applet.on_applet_clicked();
  assert.equal(toggles, 2);
  assert.equal(builds, 1);
  assert.equal(refreshes, 1);

  applet._removed = true;
  applet.on_applet_clicked();
  assert.equal(toggles, 2);
});

test("analytics action uses the fixed URL and reports browser failures", () => {
  const calls = [];
  const applet = makeApplet((runtime) => {
    runtime.appInfoFactory = (...args) => { calls.push(args); };
  });
  applet._openAnalytics();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "https://chatgpt.com/codex/cloud/settings/analytics");
  assert.equal(calls[0][1], null);

  let error = "";
  applet._showCommandError = (value) => { error = value; };
  applet._openAnalytics = Object.getPrototypeOf(applet)._openAnalytics;
  applet._openAnalytics.call(Object.assign(applet, {
    _analyticsFailure: true,
  }));
  assert.equal(error, "");
});

test("forecast table coverage is not replaced by consumption defaults", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._syncingAccountSettings = false;
  applet._backendAccounts = {alpha: {account: "alpha"}};
  applet.accountForecastSettings = [{
    account: "alpha", "show-panel": true, "show-tooltip": true,
    "limit-window": "short", format: "compact", "custom-format": "",
    smoothing: "ema-20", "hide-when-zero": false,
    "show-coverage-marker": false, "baseline-enabled": false,
    "baseline-minutes": 60, "warn-amount": 2, "warn-unit": "hours",
    "warn-format": "red-yellow"
  }];
  applet.accountConsumptionSettings = [{
    account: "alpha", "show-panel": true, "show-tooltip": true,
    amount: 1, unit: "hours", "limit-window": "short", format: "compact",
    "custom-format": "", smoothing: "ema-10", "hide-when-zero": false,
    "show-coverage-marker": true, "baseline-enabled": false,
    "baseline-minutes": 60
  }];
  applet._refreshConsumption = () => {};
  applet._refreshFormattedSurfaces = () => {};
  applet._onForecastSettingsChanged();
  assert.equal(applet._consumptionSettings.alpha["show-coverage-marker"], true);
  assert.equal(applet._consumptionSettings.alpha["forecast-show-coverage-marker"], false);
});

test("incomplete forecast rows retain the disabled token-end panel default", () => {
  const applet = makeApplet();
  const forecast = applet._normalizeForecastRow({account: "alpha"}, "alpha");
  assert.ok(forecast);
  assert.equal(forecast["show-panel"], false);
  assert.equal(forecast["show-tooltip"], true);
});

test("combined token and credit rows round-trip without crossing table fields", () => {
  const applet = makeApplet();
  const accounts = [{account: "alpha"}];
  const consumption = applet._defaultConsumptionRow("alpha");
  const forecast = Object.assign(applet._defaultForecastRow("alpha"), {
    "show-panel": true,
    "limit-window": "monthly",
    format: "custom",
    "custom-format": "TE {duration}",
    smoothing: "ema-5",
    "baseline-enabled": true,
    "baseline-minutes": 90,
  });
  const combinedConsumption = applet._combineConsumptionRows([consumption], [forecast]);
  assert.equal(combinedConsumption[0]["forecast-show-panel"], true);
  assert.equal(combinedConsumption[0]["forecast-limit-window"], "monthly");
  assert.equal(combinedConsumption[0]["forecast-custom-format"], "TE {duration}");
  assert.equal(combinedConsumption[0].smoothing, "ema-10");
  const storedConsumption = applet._consumptionStorageRow(combinedConsumption[0]);
  assert.equal(storedConsumption["forecast-show-panel"], undefined);
  assert.equal(storedConsumption["forecast-format"], undefined);
  assert.equal(storedConsumption.smoothing, "ema-10");
  const restoredForecast = applet._mergedForecastRows(
    accounts,
    null,
    [combinedConsumption[0]]
  )[0];
  assert.equal(restoredForecast["show-panel"], true);
  assert.equal(restoredForecast["limit-window"], "monthly");
  assert.equal(restoredForecast["custom-format"], "TE {duration}");
  assert.equal(restoredForecast.smoothing, "ema-5");

  const credit = Object.assign(applet._defaultCreditRow("alpha"), {
    "show-panel": true,
    format: "custom",
    "custom-format": "CR {remaining}",
    "consumption-show-panel": true,
    "consumption-amount": 2,
    "consumption-format": "verbose",
    "consumption-smoothing": "ema-5",
  });
  const creditConsumption = Object.assign(applet._defaultCreditConsumptionRow("alpha"), {
    "show-panel": true,
    amount: 2,
    format: "verbose",
    smoothing: "ema-5",
  });
  const combinedCredit = applet._combineCreditRows([credit], [creditConsumption]);
  assert.equal(combinedCredit[0]["consumption-show-panel"], true);
  assert.equal(combinedCredit[0]["consumption-amount"], 2);
  assert.equal(combinedCredit[0]["consumption-smoothing"], "ema-5");
  assert.equal(combinedCredit[0].format, "custom");
  const storedCredit = applet._creditStorageRow(combinedCredit[0]);
  assert.equal(storedCredit["consumption-show-panel"], undefined);
  assert.equal(storedCredit["consumption-amount"], undefined);
  assert.equal(storedCredit.format, "custom");
  assert.equal(storedCredit["custom-format"], "CR {remaining}");
});

test("forecast merger preserves partial legacy fields beyond panel and format", () => {
  const applet = makeApplet();
  applet._backendAccounts = {alpha: {}, beta: {}};
  const rows = applet._mergedForecastRows(
    [{account: "alpha"}, {account: "beta"}],
    null,
    [{
      account: "alpha",
      "forecast-smoothing": "ema-5",
      "forecast-limit-window": "monthly",
      "forecast-show-coverage-marker": false,
      "forecast-baseline-enabled": true,
      "forecast-baseline-minutes": 90,
    }]
  );
  assert.equal(rows[0].smoothing, "ema-5");
  assert.equal(rows[0]["limit-window"], "monthly");
  assert.equal(rows[0]["show-coverage-marker"], false);
  assert.equal(rows[0]["baseline-enabled"], true);
  assert.equal(rows[0]["baseline-minutes"], 90);
  assert.equal(rows[1]["show-panel"], false);
});

test("credit-consumption merger preserves partial legacy fields beyond amount and format", () => {
  const applet = makeApplet();
  applet._backendAccounts = {alpha: {}, beta: {}};
  const rows = applet._mergedCreditConsumptionRows(
    [{account: "alpha"}, {account: "beta"}],
    null,
    [{
      account: "alpha",
      "consumption-smoothing": "ema-5",
      "consumption-hide-when-zero": true,
      "consumption-show-coverage-marker": false,
      "consumption-baseline-enabled": true,
      "consumption-baseline-minutes": 90,
    }]
  );
  assert.equal(rows[0].smoothing, "ema-5");
  assert.equal(rows[0]["hide-when-zero"], true);
  assert.equal(rows[0]["show-coverage-marker"], false);
  assert.equal(rows[0]["baseline-enabled"], true);
  assert.equal(rows[0]["baseline-minutes"], 90);
  assert.equal(rows[1]["show-panel"], false);
});

test("invalid current forecast rows do not fall back to stale legacy rows", () => {
  const applet = makeApplet();
  applet._backendAccounts = {alpha: {}};
  const rows = applet._mergedForecastRows(
    [{account: "alpha"}],
    [{account: "alpha", "show-panel": "yes"}],
    [{account: "alpha", "forecast-smoothing": "ema-5"}]
  );
  assert.equal(rows[0].smoothing, "ema-20");
  assert.equal(rows[0]["show-panel"], false);
});

test("invalid current credit-consumption rows do not fall back to stale legacy rows", () => {
  const applet = makeApplet();
  applet._backendAccounts = {alpha: {}};
  const rows = applet._mergedCreditConsumptionRows(
    [{account: "alpha"}],
    [{account: "alpha", "hide-when-zero": "yes"}],
    [{account: "alpha", "consumption-smoothing": "ema-5"}]
  );
  assert.equal(rows[0].smoothing, "ema-20");
  assert.equal(rows[0]["hide-when-zero"], false);
});

test("account row mergers fail closed on an invalid first duplicate", () => {
  const applet = makeApplet();
  applet._backendAccounts = {alpha: {}};
  const validPanel = applet._defaultPanelRow("alpha", 2);
  const panel = applet._mergedPanelRows(
    [{account: "alpha"}],
    [{account: "alpha", order: "bad"}, validPanel]
  );
  assert.equal(panel[0].order, 1);

  const validConsumption = Object.assign(applet._defaultConsumptionRow("alpha"), {amount: 2});
  const consumption = applet._mergedConsumptionRows(
    [{account: "alpha"}],
    [{account: "alpha", amount: 0}, validConsumption]
  );
  assert.equal(consumption[0].amount, 1);

  const validReset = Object.assign(applet._defaultResetRow("alpha"), {"show-panel": true});
  const reset = applet._mergedResetRows(
    [{account: "alpha"}],
    [{account: "alpha", "show-panel": "yes"}, validReset]
  );
  assert.equal(reset[0]["show-panel"], false);

  const validAlert = Object.assign(applet._defaultAlertRow("alpha"), {warnings: false});
  const alerts = applet._mergedAlertRows(
    [{account: "alpha"}],
    [{account: "alpha", warnings: "yes"}, validAlert]
  );
  assert.equal(alerts[0].warnings, true);

  const validDisplay = Object.assign(applet._defaultDisplayRow("alpha"), {panel: 0});
  const display = applet._mergedDisplayRows(
    [{account: "alpha"}],
    [{account: "alpha", panel: "bad"}, validDisplay]
  );
  assert.equal(display[0].panel, 2);

  const validStyle = Object.assign(applet._defaultStyleRow("alpha", "percent"), {threshold: 5});
  const styles = applet._mergedStyleRows(
    [{account: "alpha"}],
    [{account: "alpha", threshold: "bad"}, validStyle],
    "percent"
  );
  assert.equal(styles[0].threshold, 20);

  const validTarget = {account: "alpha", element: 4, panel: false, hover: true, click: true};
  const targets = applet._mergedTargetRows(
    [{account: "alpha"}],
    [{account: "alpha", element: 4, panel: "bad", hover: true, click: true}, validTarget]
  );
  assert.equal(targets.find((row) => row.element === 4).panel, false);
});

test("all four metric tables keep AW, token end, credits and coverage independent", () => {
  for (const coverageEnabled of [false, true]) {
    for (const baselineEnabled of [false, true]) {
      const applet = makeApplet();
      applet._styleTargets["alpha:4"] = {panel: true, hover: true, click: true};
      applet._styleTargets["alpha:5"] = {panel: true, hover: true, click: true};
      applet._styleTargets["alpha:11"] = {panel: true, hover: true, click: true};
      applet._styleTargets["alpha:12"] = {panel: true, hover: true, click: true};
      applet._styleTargets["alpha:13"] = {panel: true, hover: true, click: true};
      applet._creditSettings = {};
      applet._consumptionSettings.alpha = {
        account: "alpha", "show-panel": true, "show-tooltip": true,
        amount: 1, unit: "hours", "limit-window": "short",
        format: "compact", "custom-format": "", "hide-when-zero": false,
        "show-coverage-marker": coverageEnabled,
        "baseline-enabled": baselineEnabled, "baseline-minutes": 60,
        "forecast-show-panel": true, "forecast-show-tooltip": true,
        "forecast-format": "compact", "forecast-limit-window": "short",
        "forecast-show-coverage-marker": coverageEnabled,
        "forecast-baseline-enabled": baselineEnabled,
        "forecast-baseline-minutes": 60
      };
      applet._creditSettings.alpha = {
        account: "alpha", "show-panel": true, "show-tooltip": true,
        format: "compact", "custom-format": "", "hide-when-zero": false,
        "show-coverage-marker": coverageEnabled,
        "baseline-enabled": baselineEnabled, "baseline-minutes": 60,
        "consumption-show-panel": true, "consumption-show-tooltip": true,
        "consumption-format": "compact", "consumption-amount": 1,
        "consumption-unit": "hours", "consumption-show-coverage-marker": coverageEnabled,
        "consumption-baseline-enabled": baselineEnabled,
        "consumption-baseline-minutes": 60
      };
      const usage = applet._usages[0];
      usage.credits = {
        remaining: 794, limit: 1000, used: 206, percent: 20.6,
        coverage: "partial", baseline_used_percent: 40
      };
      usage.cost_windows = [
        {pool: "main", lookback_seconds: 3600, limit_window_seconds: 18000,
          consumed_percentage_points: 12.5, estimated_seconds_to_exhaustion: 600,
          baseline_used_percent: 40, coverage: "partial", sample_count: 3},
        {pool: "credits", lookback_seconds: 3600, limit_window_seconds: 2592000,
          consumed_percentage_points: 8.5, baseline_used_percent: 30,
          coverage: "partial", sample_count: 3}
      ];

      const token = applet._consumptionParts(usage, "panel").plain;
      const credits = applet._creditParts(usage, "panel", true, "CR").plain;
      const creditConsumption = applet._creditConsumptionParts(usage, "panel", true, "CV").plain;
      assert.match(token, /Δ1 h 12,5%/);
  assert.match(token, /TE=0,2h/);
      assert.equal(token.includes("AW60m="), baselineEnabled);
      assert.equal(token.includes("(mindestens)"), coverageEnabled);
      assert.equal(credits.includes("AW60m="), baselineEnabled);
      assert.equal(credits.includes("(mindestens)"), coverageEnabled);
      assert.equal(creditConsumption.includes("AW60m="), baselineEnabled);
      assert.equal(creditConsumption.includes("(mindestens)"), coverageEnabled);
    }
  }
});

test("every metric-table switch survives all boolean combinations", () => {
  const switches = [
    ["show-panel", true],
    ["show-tooltip", true],
    ["hide-when-zero", false],
    ["show-coverage-marker", true],
    ["baseline-enabled", false],
  ];
  const valuesFor = (mask, prefix = "") => {
    const row = {};
    for (let i = 0; i < switches.length; i++) {
      row[prefix + switches[i][0]] = Boolean(mask & (1 << i));
    }
    return row;
  };
  for (let mask = 0; mask < (1 << switches.length); mask++) {
    const applet = makeApplet();
    const token = Object.assign({
      account: "alpha", amount: 1, unit: "hours", "limit-window": "short",
      format: "compact", "custom-format": "", smoothing: "ema-10",
      "baseline-minutes": 60,
    }, valuesFor(mask));
    const tokenRow = applet._normalizeConsumptionRow(token, "alpha");
    assert.ok(tokenRow, `token row rejected for mask ${mask}`);
    for (const [key] of switches) assert.equal(tokenRow[key], token[key], `token ${key} mask ${mask}`);

    const forecast = Object.assign({
      account: "alpha", "limit-window": "weekly", format: "compact",
      "custom-format": "", smoothing: "ema-20", "baseline-minutes": 60,
      "warn-amount": 2, "warn-unit": "hours", "warn-format": "red-yellow",
    }, valuesFor(mask));
    const forecastRow = applet._normalizeForecastRow(forecast, "alpha");
    assert.ok(forecastRow, `forecast row rejected for mask ${mask}`);
    for (const [key] of switches) assert.equal(forecastRow[key], forecast[key], `forecast ${key} mask ${mask}`);

    const credit = Object.assign({
      account: "alpha", format: "compact", "custom-format": "", smoothing: "ema-20",
      "baseline-minutes": 60, "consumption-show-panel": false,
      "consumption-show-tooltip": true, "consumption-amount": 1,
      "consumption-unit": "hours", "consumption-format": "compact",
      "consumption-custom-format": "", "consumption-smoothing": "ema-20",
      "consumption-hide-when-zero": false, "consumption-show-coverage-marker": true,
      "consumption-baseline-enabled": false, "consumption-baseline-minutes": 60,
    }, valuesFor(mask));
    const creditRow = applet._normalizeCreditRow(credit, "alpha");
    assert.ok(creditRow, `credit row rejected for mask ${mask}`);
    for (const [key] of switches) assert.equal(creditRow[key], credit[key], `credit ${key} mask ${mask}`);

    const creditConsumption = Object.assign({
      account: "alpha", amount: 1, unit: "hours", format: "compact",
      "custom-format": "", smoothing: "ema-20", "baseline-minutes": 60,
    }, valuesFor(mask));
    const creditConsumptionRow = applet._normalizeCreditConsumptionRow(creditConsumption, "alpha");
    assert.ok(creditConsumptionRow, `credit consumption row rejected for mask ${mask}`);
    for (const [key] of switches) {
      assert.equal(creditConsumptionRow[key], creditConsumption[key], `credit consumption ${key} mask ${mask}`);
    }
  }
});

test("metric-table switches reject non-boolean values independently", () => {
  const applet = makeApplet();
  const base = {
    account: "alpha", amount: 1, unit: "hours", "limit-window": "short",
    format: "compact", "custom-format": "", smoothing: "ema-10", "baseline-minutes": 60,
  };
  for (const key of ["show-panel", "show-tooltip", "hide-when-zero", "show-coverage-marker", "baseline-enabled"]) {
    const row = Object.assign({}, base, {
      "show-panel": false, "show-tooltip": true, "hide-when-zero": false,
      "show-coverage-marker": true, "baseline-enabled": false,
    });
    row[key] = "false";
    assert.equal(applet._normalizeConsumptionRow(row, "alpha"), null, `token accepts ${key}`);
    assert.equal(applet._normalizeForecastRow(row, "alpha"), null, `forecast accepts ${key}`);
    assert.equal(applet._normalizeCreditConsumptionRow(row, "alpha"), null, `credit consumption accepts ${key}`);
  }
});

test("remaining boolean switch groups round-trip independently", () => {
  const applet = makeApplet();
  applet._backendAccounts = {alpha: {account: "alpha"}};
  applet._usages = [];
  const boolPairs = [
    ["bold", "italic"],
    ["below-bold", "below-italic"],
  ];
  for (const kind of ["percent", "date", "time", "duration"]) {
    for (let mask = 0; mask < 4; mask++) {
      const row = applet._defaultStyleRow("alpha", kind);
      row[boolPairs[0][0]] = Boolean(mask & 1);
      row[boolPairs[0][1]] = Boolean(mask & 2);
      row[boolPairs[1][0]] = Boolean(mask & 1);
      row[boolPairs[1][1]] = Boolean(mask & 2);
      const normalized = applet._normalizeStyleRow(row, "alpha", kind);
      assert.ok(normalized, `${kind} style mask ${mask}`);
      for (const key of boolPairs.flat()) assert.equal(normalized[key], row[key], `${kind} ${key} mask ${mask}`);
    }
  }
  for (let mask = 0; mask < 8; mask++) {
    const target = applet._defaultTargetRow("alpha", 4);
    target.panel = Boolean(mask & 1);
    target.hover = Boolean(mask & 2);
    target.click = Boolean(mask & 4);
    assert.deepEqual(applet._normalizeTargetRow(target, "alpha"), target, `target mask ${mask}`);
  }
  for (let mask = 0; mask < 16; mask++) {
    const reset = applet._defaultResetRow("alpha");
    reset["show-panel"] = Boolean(mask & 1);
    reset["show-tooltip"] = Boolean(mask & 2);
    reset["hide-when-zero"] = Boolean(mask & 4);
    reset["show-unknown"] = Boolean(mask & 8);
    assert.deepEqual(applet._normalizeResetRow(reset, "alpha"), reset, `reset mask ${mask}`);
  }
  for (let mask = 0; mask < 4; mask++) {
    const panel = applet._defaultPanelRow("alpha", 1);
    panel.muted = Boolean(mask & 1);
    assert.equal(applet._normalizePanelRow(panel, "alpha").muted, panel.muted, `panel mask ${mask}`);
    const display = applet._defaultDisplayRow("alpha");
    display["hover-separator"] = Boolean(mask & 1);
    display["click-separator"] = Boolean(mask & 2);
    assert.deepEqual(applet._normalizeDisplayRow(display, "alpha"), display, `display mask ${mask}`);
    const alert = applet._defaultAlertRow("alpha");
    alert.warnings = Boolean(mask & 1);
    alert.errors = Boolean(mask & 2);
    assert.equal(applet._normalizeAlertRow(alert, "alpha").warnings, alert.warnings, `alert warnings mask ${mask}`);
    assert.equal(applet._normalizeAlertRow(alert, "alpha").errors, alert.errors, `alert errors mask ${mask}`);
  }
  for (let mask = 0; mask < 4; mask++) {
    const routing = applet._normalizeRoutingRows([{
      scope: 0, identifier: "alpha", enabled: Boolean(mask & 1), allow: Boolean(mask & 2),
    }]);
    assert.equal(routing[0].enabled, Boolean(mask & 1), `routing enabled mask ${mask}`);
    assert.equal(routing[0].allow, Boolean(mask & 2), `routing allow mask ${mask}`);
  }
});

test("insufficient coverage does not suppress the independent token-end row", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:4"] = {panel: true, hover: true, click: true};
  applet._styleTargets["alpha:5"] = {panel: true, hover: true, click: true};
  applet._consumptionSettings.alpha = {
    account: "alpha", "show-panel": true, "show-tooltip": true,
    amount: 1, unit: "hours", "limit-window": "short", format: "compact",
    "custom-format": "", "hide-when-zero": false,
    "show-coverage-marker": true
  };
  const usage = applet._usages[0];
  usage.cost_windows = [{
    pool: "main", lookback_seconds: 3600, limit_window_seconds: 18000,
    consumed_percentage_points: 4, coverage: "insufficient", sample_count: 0
  }];
  const rendered = applet._consumptionParts(usage, "panel").plain;
  assert.match(rendered, /nicht genügend Messdaten/);
  assert.match(rendered, /TE=—/);
});

test("token end can use a different configured limit than token consumption", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:4"] = {panel: true, hover: true, click: true};
  applet._styleTargets["alpha:5"] = {panel: true, hover: true, click: true};
  applet._consumptionSettings.alpha = {
    account: "alpha", "show-panel": true, "show-tooltip": true,
    amount: 1, unit: "hours", "limit-window": "short",
    "forecast-limit-window": "weekly", format: "compact", "custom-format": "",
    "forecast-format": "compact", "forecast-custom-format": "",
    "hide-when-zero": false, "show-coverage-marker": true,
  };
  const usage = applet._usages[0];
  usage.cost_windows = [
    {pool: "main", lookback_seconds: 3600, limit_window_seconds: 18000,
      consumed_percentage_points: 10, estimated_seconds_to_exhaustion: 300,
      coverage: "complete", sample_count: 3},
    {pool: "main", lookback_seconds: 3600, limit_window_seconds: 604800,
      consumed_percentage_points: 20, estimated_seconds_to_exhaustion: 600,
      coverage: "complete", sample_count: 3},
  ];
  const rendered = applet._consumptionParts(usage, "panel");
  assert.match(rendered.plain, /Δ1 h 10,0%/);
  assert.match(rendered.plain, /TE=0,2h/);
});

test("separate same-pool refreshes preserve consumption and token-end windows", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:4"] = {panel: true, hover: true, click: true};
  applet._styleTargets["alpha:5"] = {panel: true, hover: true, click: true};
  applet._consumptionSettings.alpha = {
    account: "alpha", "show-panel": true, "show-tooltip": true,
    amount: 1, unit: "hours", "limit-window": "short",
    "forecast-limit-window": "weekly", format: "compact", "custom-format": "",
    "forecast-format": "compact", "forecast-custom-format": "",
    "hide-when-zero": false, "show-coverage-marker": true,
    "forecast-baseline-enabled": true, "forecast-baseline-minutes": 30,
  };
  applet._usages = [{account: "alpha", cost_windows: []}];
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._updatePanel = () => {};
  applet._spawnAuxJson = (argv, callback) => {
    const limitWindow = argv[argv.indexOf("--limit-window") + 1];
    const weekly = limitWindow === "weekly";
    callback({
      account_id: "alpha",
      windows: [{
        lookback_seconds: weekly ? 3600 : 1800,
        pool: "main",
        limit_window_seconds: weekly ? 604800 : 18000,
        consumed_percentage_points: weekly ? 20 : 10,
        estimated_seconds_to_exhaustion: weekly ? 600 : 300,
        baseline_used_percent: weekly ? 33.3 : 12.5,
        coverage: "complete",
        sample_count: 3,
      }],
    }, null);
  };

  applet._refreshConsumption();
  assert.equal(
    Array.from(applet._usages[0].cost_windows, window => window.limit_window_seconds).sort((a, b) => a - b).join(","),
    "18000,604800"
  );
  const rendered = applet._consumptionParts(applet._usages[0], "panel");
  assert.match(rendered.plain, /Δ1 h 10,0%/);
  assert.match(rendered.plain, /TE=0,2h/);
  assert.match(rendered.plain, /AW30m=33,3%/);
});

test("same-window consumption and token-end queries retain their own smoothing results", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:4"] = {panel: true, hover: true, click: true};
  applet._styleTargets["alpha:5"] = {panel: true, hover: true, click: true};
  applet._consumptionSettings.alpha = {
    account: "alpha", "show-panel": true, "show-tooltip": true,
    amount: 1, unit: "hours", "limit-window": "short", format: "compact",
    "custom-format": "", smoothing: "ema-10", "hide-when-zero": false,
    "show-coverage-marker": false, "baseline-enabled": true, "baseline-minutes": 60,
    "forecast-show-panel": true, "forecast-show-tooltip": true,
    "forecast-limit-window": "short", "forecast-format": "compact",
    "forecast-smoothing": "ema-20", "forecast-show-coverage-marker": false,
    "forecast-baseline-enabled": true, "forecast-baseline-minutes": 30,
  };
  applet._usages = [{account: "alpha", cost_windows: []}];
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._updatePanel = () => {};
  applet._spawnAuxJson = (argv, callback) => {
    const smoothing = argv[argv.indexOf("--smoothing") + 1];
    const forecast = smoothing === "ema-20";
    callback({account_id: "alpha", windows: [{
      pool: "main", lookback_seconds: 3600, limit_window_seconds: 18000,
      consumed_percentage_points: forecast ? 20 : 10,
      estimated_seconds_to_exhaustion: forecast ? 600 : 300,
      baseline_used_percent: forecast ? 33.3 : 12.5,
      coverage: "complete", sample_count: 3,
    }]}, null);
  };

  applet._refreshConsumption();
  const rendered = applet._consumptionParts(applet._usages[0], "panel");
  assert.match(rendered.plain, /Δ1 h 10,0%/);
  assert.match(rendered.plain, /TE=0,2h/);
  assert.match(rendered.plain, /AW30m=33,3%/);
});

test("failed consumption refresh preserves the last validated window", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:4"] = {panel: true, hover: true, click: true};
  applet._consumptionSettings.alpha = {
    account: "alpha", "show-panel": true, "show-tooltip": true,
    amount: 1, unit: "hours", "limit-window": "short", format: "compact",
    "custom-format": "", "hide-when-zero": false,
    "show-coverage-marker": true,
  };
  applet._usages[0].cost_windows = [{
    pool: "main", lookback_seconds: 3600, limit_window_seconds: 18000,
    consumed_percentage_points: 12.3, coverage: "complete", sample_count: 3,
  }];
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._updatePanel = () => {};
  applet._spawnAuxJson = (_argv, callback) => callback(null, "temporary failure");

  applet._refreshConsumption();
  assert.equal(applet._usages[0].cost_windows.length, 1);
  assert.equal(applet._usages[0].cost_windows[0].consumed_percentage_points, 12.3);
});

test("late consumption response from an older generation cannot replace newer settings", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:4"] = {panel: true, hover: true, click: true};
  applet._consumptionSettings.alpha = {
    account: "alpha", "show-panel": true, "show-tooltip": true,
    amount: 1, unit: "hours", "limit-window": "short", format: "compact",
    "custom-format": "", smoothing: "ema-10", "hide-when-zero": false,
    "show-coverage-marker": true,
  };
  applet._usages = [{account: "alpha", cost_windows: []}];
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._updatePanel = () => {};
  const callbacks = [];
  applet._spawnAuxJson = (_argv, callback) => callbacks.push(callback);

  applet._refreshConsumption();
  applet._consumptionSettings.alpha.smoothing = "ema-20";
  applet._refreshConsumption();
  callbacks.shift()({account_id: "alpha", windows: [{
    pool: "main", lookback_seconds: 3600, limit_window_seconds: 18000,
    consumed_percentage_points: 10, coverage: "complete", sample_count: 3,
  }]}, null);
  callbacks.shift()({account_id: "alpha", windows: [{
    pool: "main", lookback_seconds: 3600, limit_window_seconds: 18000,
    consumed_percentage_points: 20, coverage: "complete", sample_count: 3,
  }]}, null);

  assert.equal(applet._usages[0].cost_windows.length, 1);
  assert.equal(applet._usages[0].cost_windows[0].consumed_percentage_points, 20);
});

test("fully disabled credit-consumption targets do not start a consumption request", () => {
  const applet = makeApplet();
  applet._consumptionSettings = Object.create(null);
  applet._creditSettings = {alpha: {
    account: "alpha", "consumption-show-panel": true,
    "consumption-show-tooltip": true, "consumption-amount": 1,
    "consumption-unit": "hours", "consumption-smoothing": "ema-20",
    "consumption-baseline-enabled": false,
  }};
  applet._styleTargets["alpha:12"] = {panel: false, hover: false, click: false};
  applet._panelSettings.alpha = {account: "alpha", order: 1, muted: false, slot1: 0, slot2: 0, slot3: 0, slot4: 0};
  let requests = 0;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._spawnAuxJson = () => { requests += 1; };

  applet._refreshConsumption();
  assert.equal(requests, 0);
});

test("consumption refresh prunes obsolete tagged query results but retains legacy data", () => {
  const applet = makeApplet();
  applet._styleTargets["alpha:4"] = {panel: true, hover: true, click: true};
  applet._consumptionSettings.alpha = {
    account: "alpha", "show-panel": true, "show-tooltip": true,
    amount: 1, unit: "hours", "limit-window": "short", format: "compact",
    "custom-format": "", smoothing: "ema-10", "hide-when-zero": false,
    "show-coverage-marker": true,
  };
  applet._usages[0].cost_windows = [{
    pool: "main", lookback_seconds: 3600, limit_window_seconds: 18000,
    consumed_percentage_points: 1, coverage: "complete", sample_count: 1,
    _consumption_query_key: applet._consumptionQueryKey("main", 2, "hours", "ema-20", null),
  }, {
    pool: "main", lookback_seconds: 3600, limit_window_seconds: 604800,
    consumed_percentage_points: 2, coverage: "complete", sample_count: 1,
  }];
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._updatePanel = () => {};
  applet._spawnAuxJson = (_argv, callback) => callback(null, "temporary failure");

  applet._refreshConsumption();
  assert.equal(applet._usages[0].cost_windows.length, 1);
  assert.equal(applet._usages[0].cost_windows[0].consumed_percentage_points, 2);
});

test("token-end AW, coverage and delta remain independent across every TE format and surface", () => {
  const formats = ["compact", "compact-minutes", "verbose", "custom"];
  for (const format of formats) {
    for (const surface of ["panel", "hover"]) {
      for (const coverage of [false, true]) {
        for (const baseline of [false, true]) {
          const applet = makeApplet();
          applet.showConsumptionDelta = true;
          applet._styleTargets["alpha:4"] = {panel: true, hover: true, click: true};
          applet._styleTargets["alpha:5"] = {panel: true, hover: true, click: true};
          applet._consumptionSettings.alpha = {
            account: "alpha", "show-panel": true, "show-tooltip": true,
            amount: 1, unit: "hours", "limit-window": "short", format: "compact",
            "custom-format": "", "hide-when-zero": false, "show-coverage-marker": false,
            "forecast-show-panel": true, "forecast-show-tooltip": true,
            "forecast-limit-window": "short", "forecast-format": format,
            "forecast-custom-format": "TE={duration}",
            "forecast-show-coverage-marker": coverage,
            "forecast-baseline-enabled": baseline, "forecast-baseline-minutes": 30,
          };
          const usage = applet._usages[0];
          usage.cost_windows = applet._safeConsumptionWindows([{
            pool: "main", lookback_seconds: 3600, limit_window_seconds: 18000,
            consumed_percentage_points: 12.5, baseline_used_percent: 33.3,
            estimated_seconds_to_exhaustion: 354 * 60, coverage: "partial", sample_count: 3,
          }]);
          const rendered = applet._consumptionParts(usage, surface);
          assert.ok(rendered, `${format}/${surface}/${coverage}/${baseline} unexpectedly blank`);
          assert.match(rendered.plain, /Δ1 h 12,5%/);
          if (baseline) {
            assert.match(rendered.plain, /AW30m=33,3%/);
          } else {
            assert.doesNotMatch(rendered.plain, /AW30m=/);
          }
          assert.ok(rendered.plain.includes("TE=") || rendered.plain.includes("Zeit bis Tokenende"),
            `${format}/${surface}/${coverage}/${baseline} lost token end`);
          assert.equal(rendered.plain.includes("(mindestens)"), coverage);
        }
      }
    }
  }
});

test("consumption display asks CLI for configured account query", () => {
  const applet = makeApplet();
  applet._usages = [{ account: "alpha", cost_windows: [] }];
  applet._consumptionSettings = {
    alpha: {
      account: "alpha",
      "show-panel": false,
      "show-tooltip": true,
      amount: 2,
      unit: "days",
      "limit-window": "all",
      format: "verbose",
      "custom-format": "",
      "hide-when-zero": false,
      "show-coverage-marker": true,
    },
  };
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._updatePanel = () => {};
  applet._spawnAuxJson = (argv, callback) => {
    assert.deepEqual(argv, [
      "codex-usage", "consumption", "--account", "alpha", "--amount", "2",
      "--unit", "days", "--smoothing", "none", "--pool", "main", "--limit-window", "all", "--format", "json",
    ]);
    callback({
      account_id: "alpha",
      windows: [{
        lookback_seconds: 172800,
        pool: "main",
        limit_window_seconds: 18000,
        consumed_percentage_points: 3,
        coverage: "complete",
        sample_count: 8,
      }],
    }, null);
  };
  applet._refreshConsumption();
  assert.equal(applet._usages[0].cost_windows[0].consumed_percentage_points, 3);
});

test("usage reset payloads fail closed and distinguish unknown from zero", () => {
  const applet = makeApplet();

  assert.deepEqual(JSON.parse(JSON.stringify(applet._safeUsageResets(undefined))), {
    available: null,
    known: false,
    redeem_capability: false,
  });
  assert.deepEqual(JSON.parse(JSON.stringify(applet._safeUsageResets({
    available: 0,
    known: true,
    redeem_capability: false,
  }))), {
    available: 0,
    known: true,
    redeem_capability: false,
  });
  assert.deepEqual(JSON.parse(JSON.stringify(applet._safeUsageResets({
    available: true,
    known: true,
    redeem_capability: false,
  }))), {
    available: null,
    known: false,
    redeem_capability: false,
  });
});

test("usage reset display respects surface, zero and format settings", () => {
  const applet = makeApplet();
  const usage = applet._usages[0];
  usage.usage_resets = { available: 2, known: true, redeem_capability: false };
  applet._resetSettings.alpha = {
    account: "alpha",
    "show-panel": false,
    "show-tooltip": true,
    "hide-when-zero": true,
    "show-unknown": false,
    format: "readable",
  };

  assert.equal(applet._usageResetParts(usage, "panel"), null);
  assert.equal(applet._usageResetParts(usage, "hover").plain, "2 Resets");

  usage.usage_resets.available = 0;
  assert.equal(applet._usageResetParts(usage, "hover"), null);
  applet._resetSettings.alpha["hide-when-zero"] = false;
  assert.equal(applet._usageResetParts(usage, "hover").plain, "0 Resets");

  usage.usage_resets = { available: null, known: false, redeem_capability: false };
  assert.equal(applet._usageResetParts(usage, "hover"), null);
  applet._resetSettings.alpha["show-unknown"] = true;
  assert.equal(applet._usageResetParts(usage, "hover").plain, "Resets: —");
});

test("remaining percentage prefers absolute used and limit values", () => {
  const applet = makeApplet();
  assert.equal(
    applet._remainingPercent({ used: 8, limit: 40, remaining: 32, percent: 20 }),
    80
  );
  assert.equal(applet._remainingPercent({ remaining: undefined, percent: undefined }), null);
  assert.equal(applet._remainingPercent({ remaining: 690, limit: 1000 }), 69);
  assert.equal(applet._remainingPercent({ remaining: 690, percent: 69 }), 69);
  assert.equal(applet._remainingPercent({ remaining: 690 }), null);
  assert.equal(applet._remainingPercent({ remaining: 50, percent: 90 }), null);
  assert.equal(applet._remainingPercent({ remaining: 97, percent: 97 }), 97);
});

test("window identity text is not normalized into a known name", () => {
  const applet = makeApplet();

  assert.throws(
    () => applet._safeWindow({ name: "weekly\u0000", remaining: 90 }),
    /text value exceeds strict limit/
  );
  assert.throws(
    () => applet._safeWindow({ name: "weekly" + "x".repeat(40), remaining: 90 }),
    /text value exceeds strict limit/
  );
});

test("limit timestamps reject control-character normalization", () => {
  const applet = makeApplet();

  assert.throws(
    () => applet._safeWindow({ name: "weekly", reset_at: "2026-07-10T15:00:00Z\u0000" }),
    /text value exceeds strict limit/
  );
  assert.throws(
    () => applet._validatePayload([{
      account: "alpha",
      captured_at: "2026-07-10T15:00:00Z\u0000",
      five_hour: { name: "5h", remaining: 90 },
      status: "ok",
      stale: false,
      cache_invalidated: false,
      backend_configured: "direct",
      backend_used: "direct",
    }]),
    /text value exceeds strict limit/
  );
});

test("invalid absolute limit pairs cannot become visible usage", () => {
  const applet = makeApplet();
  const window = applet._safeWindow({
    name: "5h",
    used: 0,
    limit: 0,
    remaining: 0,
    percent: null,
  });

  assert.equal(window.used, null);
  assert.equal(window.limit, null);
  assert.equal(window.remaining, null);
  assert.equal(window.percent, null);
  assert.equal(applet._remainingPercent(window), null);
});

test("explicit percentages survive invalid absolute limit pairs", () => {
  const applet = makeApplet();
  const window = applet._safeWindow({
    name: "5h",
    used: 0,
    limit: 0,
    remaining: 690,
    percent: 69,
  });

  assert.equal(window.used, null);
  assert.equal(window.limit, null);
  assert.equal(window.remaining, null);
  assert.equal(window.percent, 69);
  assert.equal(applet._remainingPercent(window), 69);
});

test("out-of-range explicit percentages cannot become visible usage", () => {
  const applet = makeApplet();
  const window = applet._safeWindow({ name: "5h", percent: 101 });
  const mixedWindow = applet._safeWindow({
    name: "5h",
    remaining: 97,
    percent: 101,
  });

  assert.equal(window.percent, null);
  assert.equal(applet._remainingPercent(window), null);
  assert.equal(mixedWindow.remaining, null);
  assert.equal(applet._remainingPercent(mixedWindow), null);
});

test("invalid percentage cannot preserve an otherwise valid absolute pair", () => {
  const applet = makeApplet();
  const window = applet._safeWindow({
    name: "5h",
    used: 20,
    limit: 100,
    remaining: 80,
    percent: 101,
  });

  assert.equal(window.used, null);
  assert.equal(window.limit, null);
  assert.equal(window.remaining, null);
  assert.equal(window.percent, null);
  assert.equal(applet._remainingPercent(window), null);
});

test("invalid signed counters are sanitized before rendering", () => {
  const applet = makeApplet();
  const negativeUsed = applet._safeWindow({
    name: "5h",
    used: -1,
    limit: 100,
    remaining: 80,
    percent: 97,
  });
  const zeroLimit = applet._safeWindow({ name: "5h", limit: 0, remaining: 50 });
  const negativeRemaining = applet._safeWindow({ name: "5h", remaining: -1, percent: 97 });

  assert.equal(negativeUsed.used, null);
  assert.equal(negativeUsed.limit, 100);
  assert.equal(negativeUsed.remaining, null);
  assert.equal(negativeUsed.percent, null);
  assert.equal(applet._remainingPercent(negativeUsed), null);
  assert.equal(zeroLimit.limit, null);
  assert.equal(zeroLimit.remaining, null);
  assert.equal(applet._remainingPercent(zeroLimit), null);
  assert.equal(negativeRemaining.remaining, null);
  assert.equal(negativeRemaining.percent, null);
  assert.equal(applet._remainingPercent(negativeRemaining), null);
});

test("invalid absolute remaining cannot preserve absolute usage", () => {
  const applet = makeApplet();
  for (const remaining of [-1, 120]) {
    const window = applet._safeWindow({
      name: "5h",
      used: 20,
      limit: 100,
      remaining,
      percent: 97,
    });

    assert.equal(window.used, null);
    assert.equal(window.remaining, null);
    assert.equal(window.percent, null);
    assert.equal(applet._remainingPercent(window), null);
  }
});

test("remaining percentage rejects invalid raw fields", () => {
  const applet = makeApplet();
  for (const window of [
    { used: -1, percent: 97 },
    { limit: 0, percent: 97 },
    { used: 20, limit: 100, remaining: -1 },
    { used: 20, limit: 100, remaining: 120 },
    { used: 20, limit: 100, percent: 101 },
  ]) {
    assert.equal(applet._remainingPercent(window), null);
  }
});

test("out of range absolute remaining cannot preserve explicit percentage", () => {
  const applet = makeApplet();
  const window = applet._safeWindow({
    name: "5h",
    remaining: 101,
    limit: 100,
    percent: 97,
  });

  assert.equal(window.remaining, null);
  assert.equal(window.percent, null);
  assert.equal(applet._remainingPercent(window), null);
});

test("absolute remaining above limit cannot become safe percentage", () => {
  const applet = makeApplet();
  const window = applet._safeWindow({
    name: "5h",
    remaining: 120,
    limit: 100,
  });

  assert.equal(window.remaining, null);
  assert.equal(applet._remainingPercent(window), null);
});

test("average panel source requires both limit windows", () => {
  const applet = makeApplet();
  const usage = applet._usages[0];

  assert.equal(applet._panelValueForSource(usage, 3), 70);
  assert.equal(applet._panelWindowForSource(usage, 3), usage.weekly);

  usage.five_hour = null;

  assert.equal(applet._panelValueForSource(usage, 3), null);
  assert.equal(applet._panelWindowForSource(usage, 3), null);
});

test("legacy panel sources cannot bypass an unusable main pool", () => {
  const applet = makeApplet();
  const usage = applet._usages[0];
  usage.main = {
    available: true,
    allowed: true,
    limit_reached: false,
    exhausted: true,
    windows: [{ name: "weekly", duration_seconds: 604800, remaining: 0 }],
  };

  for (const source of [1, 2, 3]) {
    assert.equal(applet._panelValueForSource(usage, source), null);
    assert.equal(applet._panelWindowForSource(usage, source), null);
  }
});

test("dynamic pools survive validation and drive Spark panel slots", () => {
  const applet = makeApplet();
  const [usage] = applet._validatePayload([{
    account: "alpha",
    label: "Alpha",
    backend_configured: "direct",
    backend_used: "direct",
    captured_at: "2026-07-16T10:00:00+00:00",
    five_hour: null,
    weekly: null,
    main: {
      key: "main",
      display_name: "Codex",
      windows: [{ name: "30d", duration_seconds: 2592000, remaining: 72 }],
      available: true,
      allowed: true,
      limit_reached: false,
      exhausted: false,
      availability_sources: ["app_server"],
    },
    models: {
      "gpt-5.3-codex-spark": {
        key: "gpt-5.3-codex-spark",
        display_name: "GPT-5.3-Codex-Spark",
        windows: [
          { name: "5h", duration_seconds: 18000, remaining: 40 },
          { name: "weekly", duration_seconds: 604800, remaining: 80 },
          { name: "30d", duration_seconds: 2592000, remaining: 25 },
        ],
        available: true,
        allowed: true,
        limit_reached: false,
        exhausted: false,
        availability_sources: ["rate_limits"],
      },
    },
    status: "ok",
  }]);

  assert.equal(usage.main.windows[0].duration_seconds, 2592000);
  assert.equal(applet._panelValueForSource(usage, 4), 40);
  assert.equal(applet._panelValueForSource(usage, 5), 80);
  assert.equal(applet._panelValueForSource(usage, 6), 60);
  assert.equal(applet._panelValueForSource(usage, 7), 25);
  assert.equal(applet._panelWindowForSource(usage, 6).name, "5h");
});

test("valid legacy values survive an unusable main pool", () => {
  const applet = makeApplet();
  const [usage] = applet._validatePayload([{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    captured_at: "2026-07-16T10:00:00+00:00",
    stale: false,
    cache_invalidated: false,
    five_hour: { name: "5h", remaining: 97 },
    weekly: { name: "weekly", remaining: 55 },
    main: {
      key: "main",
      windows: [{ name: "30d", reset_at: "2026-08-15T15:20:23.000Z" }],
      available: true,
      allowed: true,
      limit_reached: false,
      exhausted: true,
      availability_sources: ["usage"],
    },
    status: "ok",
  }]);

  assert.equal(usage.status, "ok");
  assert.equal(usage.five_hour.remaining, 97);
  assert.equal(usage.weekly.remaining, 55);
  assert.equal(usage.main.available, true);
});

test("known exhausted main pool keeps its zero usage value", () => {
  const applet = makeApplet();
  const [usage] = applet._validatePayload([{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    captured_at: "2026-07-16T10:00:00+00:00",
    five_hour: null,
    weekly: null,
    main: {
      key: "main",
      windows: [{ name: "weekly", duration_seconds: 604800, remaining: 0 }],
      available: true,
      allowed: true,
      limit_reached: false,
      exhausted: true,
      availability_sources: ["app_server"],
    },
    status: "ok",
    stale: false,
    cache_invalidated: false,
  }]);

  assert.equal(usage.status, "ok");
  assert.equal(usage.main.available, true);
  assert.equal(applet._remainingPercent(usage.main.windows[0]), 0);
});

test("contradictory exhaustion metadata disables a pool", () => {
  const applet = makeApplet();
  const pool = applet._safePool({
    key: "main",
    windows: [{ name: "weekly", duration_seconds: 604800, remaining: 0 }],
    available: true,
    allowed: true,
    limit_reached: false,
    exhausted: false,
    availability_sources: [],
  }, "main");

  assert.equal(pool.available, false);
});

test("malformed pool source invalidates whole pool", () => {
  const applet = makeApplet();

  assert.throws(() => applet._safePool({
    key: "main",
    windows: [{ name: "weekly", duration_seconds: 604800, remaining: 90 }],
    available: true,
    allowed: true,
    limit_reached: false,
    exhausted: false,
    availability_sources: ["rate_limits", { source: "untrusted" }],
  }, "main"), /invalid availability sources/);
});

test("window sources are not normalized into trusted provenance", () => {
  const applet = makeApplet();

  assert.throws(() => applet._safeWindow({
    name: "5h",
    remaining: 100,
    source: " inferred:inactive-five-hour:direct",
  }));
  assert.equal(
    applet._isInferredInactiveFiveHour({
      source: "inferred:inactive-five-hour:unknown",
    }),
    false
  );
  assert.equal(
    applet._isInferredInactiveFiveHour({
      source: "inferred:inactive-five-hour:direct:extra",
    }),
    false
  );
});

test("pool identities are not normalized into trusted keys", () => {
  const applet = makeApplet();

  assert.throws(() => applet._safePool({
    key: " main",
    windows: [],
    available: false,
    availability_sources: [],
  }, "main"), /invalid usage pool key/);
  assert.throws(() => applet._safePools({
    "gpt-5.3-codex-spark ": {
      windows: [],
      available: false,
      availability_sources: [],
    },
  }), /invalid model usage pool key/);
  assert.throws(() => applet._safePools({
    "gpt-5.3-codex-spark": {
      windows: [],
      available: false,
      availability_sources: [],
    },
    "GPT-5.3-CODEX-SPARK": {
      windows: [],
      available: false,
      availability_sources: [],
    },
  }), /invalid model usage pool key/);
});

test("Spark pools without positive usage cannot drive panel sources", () => {
  const applet = makeApplet();
  const usage = {
    account: "alpha",
    models: {
      "gpt-5.3-codex-spark": {
        key: "gpt-5.3-codex-spark",
        windows: [{ name: "weekly", duration_seconds: 604800 }],
        available: true,
        allowed: true,
        limit_reached: false,
        availability_sources: ["rate_limits"],
      },
    },
  };

  const [pool] = Object.values(applet._safePools(usage.models));
  assert.equal(applet._poolIsUsable(pool), false);
});

test("Spark pools without a window identity cannot drive panel sources", () => {
  const applet = makeApplet();
  const pool = applet._safePool({
    key: "gpt-5.3-codex-spark",
    windows: [{ remaining: 90 }],
    available: true,
    allowed: true,
    limit_reached: false,
    exhausted: false,
    availability_sources: ["rate_limits"],
  }, "gpt-5.3-codex-spark");

  assert.equal(pool.available, true);
  assert.equal(applet._poolIsUsable(pool), false);
});

test("Spark pools with unknown window names cannot drive panel sources", () => {
  const applet = makeApplet();
  for (const name of ["unknown", "Limit", "5-hour", "untrusted-window"]) {
    const pool = applet._safePool({
      key: "gpt-5.3-codex-spark",
      windows: [{ name, remaining: 90 }],
      available: true,
      allowed: true,
      limit_reached: false,
      exhausted: false,
      availability_sources: ["rate_limits"],
    }, "gpt-5.3-codex-spark");

    assert.equal(applet._poolIsUsable(pool), false, name);
  }
  const conflicting = applet._safePool({
    key: "gpt-5.3-codex-spark",
    windows: [{ name: "5h", duration_seconds: 604800, remaining: 90 }],
    available: true,
    allowed: true,
    limit_reached: false,
    exhausted: false,
    availability_sources: ["rate_limits"],
  }, "gpt-5.3-codex-spark");

  assert.equal(applet._poolIsUsable(conflicting), false);
});

test("duplicate window identity disables pool usage", () => {
  const applet = makeApplet();
  const pool = applet._safePool({
    key: "gpt-5.3-codex-spark",
    windows: [
      { name: "weekly", remaining: 5 },
      { name: "w", remaining: 90 }
    ],
    available: true,
    allowed: true,
    limit_reached: false,
    exhausted: false,
    availability_sources: ["rate_limits"]
  }, "gpt-5.3-codex-spark");

  assert.equal(pool.available, false);
  assert.equal(applet._poolIsUsable(pool), false);
  assert.equal(applet._poolWindowForDuration(pool, 604800), null);
});

test("window name proves duration when serialized duration is absent", () => {
  const applet = makeApplet();
  const pool = applet._safePool({
    key: "gpt-5.3-codex-spark",
    windows: [{ name: "weekly", remaining: 90 }],
    available: true,
    allowed: true,
    limit_reached: false,
    exhausted: false,
    availability_sources: ["rate_limits"]
  }, "gpt-5.3-codex-spark");

  assert.equal(applet._poolWindowForDuration(pool, 604800).remaining, 90);
  assert.equal(applet._windowDisplayLabel(pool.windows[0]), "Woche");
});

test("named thirty-day window remains identifiable without serialized duration", () => {
  const applet = makeApplet();
  const window = {
    name: "30d",
    remaining: 68,
    reset_at: "2026-08-15T15:20:23.000Z"
  };

  assert.equal(applet._windowKind(window), "thirty_day");
  assert.equal(applet._windowDurationMatches(window, { name: "30d" }, null), true);
  assert.equal(
    applet._windowCacheExpired(
      window,
      "2026-07-17T15:20:23.000Z",
      "2026-07-18T15:20:23.000Z"
    ),
    false
  );
});

test("fresh dynamic pools preserve cached resets when response omits them", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    captured_at: "2026-07-17T10:00:00.000Z",
    main: {
      available: true,
      windows: [{
        name: "30d",
        duration_seconds: 2592000,
        remaining: 80,
        reset_at: "2026-08-15T10:00:00.000Z"
      }]
    },
    models: {
      "gpt-5.3-codex-spark": {
        available: true,
        windows: [{
          name: "weekly",
          duration_seconds: 604800,
          remaining: 70,
          reset_at: "2026-07-24T10:00:00.000Z"
        }]
      }
    }
  }];

  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    status: "ok",
    captured_at: "2026-07-17T10:05:00.000Z",
    main: {
      available: true,
      windows: [{ name: "30d", duration_seconds: 2592000, remaining: 75 }]
    },
    models: {
      "gpt-5.3-codex-spark": {
        available: true,
        windows: [{ name: "weekly", duration_seconds: 604800, remaining: 65 }]
      }
    },
    stale: false
  }]);

  assert.equal(merged[0].main.windows[0].remaining, 75);
  assert.equal(merged[0].main.windows[0].reset_at, "2026-08-15T10:00:00.000Z");
  assert.equal(
    merged[0].models["gpt-5.3-codex-spark"].windows[0].remaining,
    65,
  );
  assert.equal(
    merged[0].models["gpt-5.3-codex-spark"].windows[0].reset_at,
    "2026-07-24T10:00:00.000Z",
  );
  assert.equal(merged[0].stale, true);
  assert.equal(merged[0].values_captured_at, "2026-07-17T10:00:00.000Z");
});

test("unusable Spark pools cannot drive panel sources", () => {
  const applet = makeApplet();
  for (const control of [
    { available: false },
    { available: true, allowed: false },
    { available: true, limit_reached: true },
    { available: true, exhausted: true },
    { available: true, allowed: "false" },
    { available: true, exhausted: "false" },
  ]) {
    const [usage] = applet._validatePayload([{
      account: "alpha",
      label: "Alpha",
      backend_configured: "direct",
      backend_used: "direct",
      captured_at: "2026-07-16T10:00:00+00:00",
      five_hour: null,
      weekly: null,
      models: {
        "gpt-5.3-codex-spark": {
          key: "gpt-5.3-codex-spark",
          windows: [
            { name: "5h", duration_seconds: 18000, remaining: 40 },
            { name: "weekly", duration_seconds: 604800, remaining: 80 },
          ],
          ...control,
          availability_sources: ["rate_limits"],
        },
      },
      status: "ok",
    }]);

    assert.equal(usage.status, "error");
    for (const source of [4, 5, 6, 7]) {
      assert.equal(applet._panelValueForSource(usage, source), null);
      assert.equal(applet._panelWindowForSource(usage, source), null);
    }
  }
});

test("malformed cache controls fail closed during payload validation", () => {
  const applet = makeApplet();
  const [validated] = applet._validatePayload([{
    account: "alpha",
    captured_at: new Date().toISOString(),
    five_hour: { name: "5h", remaining: 80 },
    weekly: { name: "weekly", remaining: 60 },
    status: "error",
    stale: "false",
    cache_invalidated: "false",
  }]);

  assert.equal(validated.stale, true);
  assert.equal(validated.cache_invalidated, true);
  const [merged] = applet._mergeFreshPayload([validated]);
  assert.equal(merged.five_hour, null);
  assert.equal(merged.weekly, null);
});

test("terminal and invalidated payloads hide cached usage resets", () => {
  for (const [status, cacheInvalidated] of [
    ["error", false],
    ["login_required", false],
    ["ok", true]
  ]) {
    const applet = makeApplet();
    const [usage] = applet._validatePayload([{
      account: "alpha",
      captured_at: new Date().toISOString(),
      five_hour: { name: "5h", remaining: 80 },
      weekly: { name: "weekly", remaining: 60 },
      status,
      stale: cacheInvalidated,
      cache_invalidated: cacheInvalidated,
      usage_resets: { available: 2, known: true, redeem_capability: true }
    }]);

    assert.deepEqual(JSON.parse(JSON.stringify(usage.usage_resets)), {
      available: null,
      known: false,
      redeem_capability: false
    });
  }
});

test("ok payload without usage values fails closed", () => {
  const applet = makeApplet();
  const [usage] = applet._validatePayload([{
    account: "alpha",
    captured_at: new Date().toISOString(),
    five_hour: { name: "5h", reset_at: "2026-07-10T15:00:00+00:00" },
    weekly: { name: "weekly", reset_at: "2026-07-11T15:00:00+00:00" },
    status: "ok",
  }]);

  assert.equal(usage.status, "error");
  assert.equal(usage.error, "usage values missing");
  assert.equal(usage.stale, true);
  assert.equal(usage.cache_invalidated, true);
  assert.equal(usage.five_hour, null);
  assert.equal(usage.weekly, null);
  assert.equal(usage.main, null);
  assert.deepEqual(Object.keys(usage.models), []);
});

test("unknown window identity cannot mark payload as usable", () => {
  const applet = makeApplet();
  const [usage] = applet._validatePayload([{
    account: "alpha",
    captured_at: new Date().toISOString(),
    main: {
      key: "main",
      windows: [{ name: "untrusted-window", remaining: 97 }],
      available: true,
      exhausted: false,
      availability_sources: ["usage"],
    },
    status: "ok",
  }]);

  assert.equal(usage.status, "error");
  assert.equal(usage.error, "usage values missing");
  assert.equal(usage.cache_invalidated, true);
  assert.equal(usage.main, null);
});

test("missing freshness metadata cannot mark usage fresh", () => {
  const applet = makeApplet();
  const [usage] = applet._validatePayload([{
    account: "alpha",
    captured_at: new Date().toISOString(),
    five_hour: { name: "5h", remaining: 97 },
    weekly: { name: "weekly", remaining: 55 },
    backend_configured: "direct",
    backend_used: "direct",
    status: "ok",
  }]);

  assert.equal(usage.status, "partial");
  assert.equal(usage.error, "usage freshness metadata missing");
  assert.equal(usage.stale, true);
  assert.equal(usage.cache_invalidated, false);
  assert.equal(usage.five_hour.remaining, 97);
});

test("invalid usage status clears values before rendering", () => {
  for (const status of [undefined, "unknown"]) {
    const applet = makeApplet();
    const [usage] = applet._validatePayload([{
      account: "alpha",
      backend_configured: "direct",
      backend_used: "direct",
      five_hour: { name: "5h", remaining: 80 },
      weekly: { name: "weekly", remaining: 60 },
      status,
    }]);

    assert.equal(usage.status, "error");
    assert.equal(usage.error, "invalid usage status");
    assert.equal(usage.stale, true);
    assert.equal(usage.cache_invalidated, true);
    assert.equal(usage.five_hour, null);
    assert.equal(usage.weekly, null);
  }
});

test("login-required status clears limit values before rendering", () => {
  const applet = makeApplet();
  const [usage] = applet._validatePayload([{
    account: "alpha",
    captured_at: new Date().toISOString(),
    backend_configured: "direct",
    backend_used: "direct",
    five_hour: { name: "5h", remaining: 80 },
    weekly: { name: "weekly", remaining: 60 },
    status: "login_required",
    stale: false,
    cache_invalidated: false,
  }]);

  assert.equal(usage.status, "login_required");
  assert.equal(usage.error, "terminal usage status cannot carry limit values");
  assert.equal(usage.stale, true);
  assert.equal(usage.cache_invalidated, true);
  assert.equal(usage.five_hour, null);
  assert.equal(usage.weekly, null);
  assert.equal(usage.main, null);
  assert.deepEqual(Object.keys(usage.models), []);
});

test("error status clears limit values before rendering", () => {
  const applet = makeApplet();
  const [usage] = applet._validatePayload([{
    account: "alpha",
    captured_at: new Date().toISOString(),
    backend_configured: "direct",
    backend_used: "direct",
    five_hour: { name: "5h", remaining: 80 },
    weekly: { name: "weekly", remaining: 60 },
    status: "error",
    error: "backend failed",
    stale: false,
    cache_invalidated: false,
  }]);

  assert.equal(usage.status, "error");
  assert.equal(usage.error, "backend failed");
  assert.equal(usage.stale, true);
  assert.equal(usage.cache_invalidated, true);
  assert.equal(usage.five_hour, null);
  assert.equal(usage.weekly, null);
  assert.equal(usage.main, null);
  assert.deepEqual(Object.keys(usage.models), []);
});

test("invalid capture metadata clears usage values", () => {
  for (const metadata of [
    { captured_at: "invalid-capture" },
    { captured_at: "2099-01-01T00:00:00.000Z" },
    {
      captured_at: "2026-07-10T10:00:00.000Z",
      values_captured_at: "invalid-values-capture",
    },
  ]) {
    const applet = makeApplet();
    const [usage] = applet._validatePayload([{
      account: "alpha",
      backend_configured: "direct",
      backend_used: "direct",
      five_hour: { name: "5h", remaining: 80 },
      weekly: { name: "weekly", remaining: 60 },
      status: "ok",
      ...metadata,
    }]);

    assert.equal(usage.status, "error");
    assert.equal(usage.error, "invalid capture timestamp");
    assert.equal(usage.stale, true);
    assert.equal(usage.cache_invalidated, true);
    assert.equal(usage.five_hour, null);
    assert.equal(usage.weekly, null);
  }
});

test("invalid dynamic pool duration is rejected", () => {
  const applet = makeApplet();
  assert.throws(() => applet._safePool({
    key: "main",
    windows: [{ name: "bad", duration_seconds: -1 }],
    availability_sources: [],
  }, "main"), /invalid limit duration/);
});

test("oversized raw window duration cannot prove window identity", () => {
  const applet = makeApplet();
  const pool = applet._safePool({
    key: "gpt-5.3-codex-spark",
    windows: [{
      name: "untrusted-window",
      raw: '{"limit_window_seconds":315360001}',
      remaining: 90,
    }],
    available: true,
    allowed: true,
    limit_reached: false,
    exhausted: false,
    availability_sources: ["rate_limits"],
  }, "gpt-5.3-codex-spark");

  assert.equal(applet._poolIsUsable(pool), false);
});

test("pool availability must be an explicit boolean", () => {
  const applet = makeApplet();
  assert.throws(() => applet._safePool({
    key: "main",
    windows: [],
    availability_sources: [],
  }, "main"), /invalid usage pool availability/);
});

test("invalid pool control flags disable pool", () => {
  const applet = makeApplet();
  for (const field of ["allowed", "limit_reached", "exhausted"]) {
    const pool = applet._safePool({
      key: "main",
      windows: [],
      available: true,
      availability_sources: [],
      [field]: "false",
    }, "main");

    assert.equal(pool.available, false);
  }
});

test("missing derived exhaustion flag disables pool", () => {
  const applet = makeApplet();
  const pool = applet._safePool({
    key: "gpt-5.3-codex-spark",
    windows: [{ name: "weekly", duration_seconds: 604800, remaining: 90 }],
    available: true,
    allowed: true,
    limit_reached: false,
    availability_sources: ["rate_limits"],
  }, "gpt-5.3-codex-spark");

  assert.equal(pool.available, false);
  assert.equal(applet._poolIsUsable(pool), false);
});

test("exhausted pool details do not look available", () => {
  const applet = makeApplet();
  const parts = applet._poolDetailParts({
    available: true,
    allowed: true,
    limit_reached: true,
    exhausted: false,
    windows: [{ name: "weekly", remaining: 0 }],
  }, "alpha", "click", "Spark", []);

  assert.equal(parts.plain, "Spark erschöpft");
});

test("unknown pool windows never appear as numeric details", () => {
  const applet = makeApplet();
  const parts = applet._poolDetailParts({
    available: true,
    allowed: true,
    limit_reached: false,
    exhausted: false,
    windows: [{ name: "unknown", remaining: 90 }],
  }, "alpha", "click", "Spark", []);

  assert.equal(parts.plain, "Spark nicht verfügbar · Limit unbekannt");
  const mixed = applet._poolDetailParts({
    available: true,
    allowed: true,
    limit_reached: false,
    exhausted: false,
    windows: [
      { name: "weekly", remaining: 90 },
      { name: "unknown", remaining: 90 },
    ],
  }, "alpha", "click", "Spark", []);

  assert.equal(mixed.plain, "Spark nicht verfügbar · Limit unbekannt");
});

test("cache invalidation clears dynamic usage pools", () => {
  const applet = makeApplet();
  const invalidated = applet._clearInvalidatedUsage({
    status: "ok",
    main: { windows: [{ name: "weekly" }] },
    models: { "gpt-5.3-codex-spark": { windows: [{ name: "weekly" }] } },
  });

  assert.equal(invalidated.main, null);
  assert.equal(Object.keys(invalidated.models).length, 0);
  assert.equal(invalidated.status, "partial");
});

test("routing policy changes preserve scope precedence inputs", () => {
  const applet = makeApplet();
  const current = {
    schema_version: 1,
    global: false,
    account: { alpha: true },
    group: { build: false },
    agent: {},
    job: {},
  };
  const desired = {
    schema_version: 1,
    global: true,
    account: {},
    group: { build: true },
    agent: {},
    job: { release: false },
  };

  assert.deepEqual(
    Array.from(applet._routingPolicyCommands(current, desired), (command) => Array.from(command)),
    [
      ["global", "allow"],
      ["account", "inherit", "alpha"],
      ["group", "allow", "build"],
      ["job", "deny", "release"],
    ]
  );
});

test("routing policy validator preserves every scope and credit limit override", () => {
  const applet = makeApplet();
  const policy = applet._validateRoutingPolicy({
    schema_version: 1,
    global: true,
    account: {alpha: true},
    group: {build: false},
    agent: {},
    job: {release: true},
    credit_limits: {hourly: 1.5, weekly: 0, monthly: 30},
    credit_limit_overrides: {
      account: {alpha: {hourly: 2, weekly: null, monthly: 3}},
      group: {build: {hourly: null, weekly: 4, monthly: null}},
      agent: {},
      job: {},
    },
  });

  assert.equal(policy.global, true);
  assert.equal(policy.credit_limits.hourly, 1.5);
  assert.equal(policy.credit_limits.weekly, 0);
  assert.equal(policy.credit_limit_overrides.account.alpha.hourly, 2);
  assert.equal(policy.credit_limit_overrides.account.alpha.weekly, null);
  assert.equal(policy.credit_limit_overrides.group.build.weekly, 4);
  assert.equal(Object.getPrototypeOf(policy.account), null);
  assert.equal(Object.getPrototypeOf(policy.credit_limit_overrides.account), null);
});

test("routing policy validator rejects malformed scopes, identifiers and overrides", () => {
  const applet = makeApplet();
  const base = () => ({
    schema_version: 1,
    global: false,
    account: {},
    group: {},
    agent: {},
    job: {},
  });
  const invalidCases = [
    ["missing scope", (value) => { delete value.agent; }],
    ["invalid rule", (value) => { value.account = {alpha: "true"}; }],
    ["invalid identifier", (value) => { value.account = {"bad id": true}; }],
    ["negative global limit", (value) => { value.credit_limits = {hourly: -1}; }],
    ["invalid override value", (value) => {
      value.credit_limit_overrides = {account: {alpha: {hourly: "2", weekly: null, monthly: null}}};
    }],
    ["empty override", (value) => {
      value.credit_limit_overrides = {account: {alpha: {hourly: null, weekly: null, monthly: null}}};
    }],
  ];
  for (const [label, mutate] of invalidCases) {
    const value = base();
    mutate(value);
    assert.throws(() => applet._validateRoutingPolicy(value), label);
  }
});

test("routing limit helpers distinguish disabled values and produce scoped commands", () => {
  const applet = makeApplet();
  assert.equal(applet._routingLimitValue(null), null);
  assert.equal(applet._routingLimitValue(0), null);
  assert.equal(applet._routingLimitValue(-1), null);
  assert.equal(applet._routingLimitValue("2.5"), 2.5);
  assert.equal(applet._routingLimitValue("bad"), null);
  assert.equal(applet._routingLimitValue(Infinity), null);

  const current = {
    credit_limits: {hourly: 1, weekly: 2, monthly: 3},
    credit_limit_overrides: {
      account: {alpha: {hourly: 4, weekly: 0, monthly: 6}},
      group: {}, agent: {}, job: {},
    },
  };
  const desired = {
    credit_limits: {hourly: 1, weekly: 5, monthly: 0},
    credit_limit_overrides: {
      account: {alpha: {hourly: 4, weekly: 7, monthly: null}},
      group: {build: {hourly: null, weekly: 1, monthly: null}},
      agent: {}, job: {},
    },
  };
  assert.equal(JSON.stringify(applet._routingCreditLimitCommands(current, desired)), JSON.stringify([
    {scope: "global", identifier: null, limits: desired.credit_limits},
    {scope: "account", identifier: "alpha", limits: desired.credit_limit_overrides.account.alpha},
    {scope: "group", identifier: "build", limits: desired.credit_limit_overrides.group.build},
  ]));
  assert.equal(applet._routingCreditLimitCommandApplied(
    {credit_limits: desired.credit_limits},
    {scope: "global", identifier: null, limits: desired.credit_limits}
  ), true);
  assert.equal(applet._routingCreditLimitCommandApplied(
    {credit_limit_overrides: {account: {alpha: desired.credit_limit_overrides.account.alpha}}},
    {scope: "account", identifier: "alpha", limits: desired.credit_limit_overrides.account.alpha}
  ), true);
  assert.equal(applet._routingCreditLimitCommandApplied(
    {credit_limits: {hourly: 0, weekly: 0, monthly: 0}},
    {scope: "global", identifier: null, limits: {hourly: 1, weekly: 0, monthly: 0}}
  ), false);
});

test("routing policy command application recognizes allow, deny and inheritance", () => {
  const applet = makeApplet();
  const policy = {
    global: true,
    account: {alpha: true},
    group: {build: false},
    agent: {},
    job: {},
  };
  assert.equal(applet._routingPolicyCommandApplied(policy, ["global", "allow"]), true);
  assert.equal(applet._routingPolicyCommandApplied(policy, ["global", "deny"]), false);
  assert.equal(applet._routingPolicyCommandApplied(policy, ["account", "allow", "alpha"]), true);
  assert.equal(applet._routingPolicyCommandApplied(policy, ["group", "deny", "build"]), true);
  assert.equal(applet._routingPolicyCommandApplied(policy, ["account", "inherit", "missing"]), true);
  assert.equal(applet._routingPolicyCommandApplied(policy, ["account", "inherit", "alpha"]), false);
  for (const command of [null, [], ["unknown", "allow"], ["account", "maybe", "alpha"]]) {
    assert.equal(applet._routingPolicyCommandApplied(policy, command), false);
  }
});

test("malformed routing settings reload authoritative state instead of throwing", () => {
  const applet = makeApplet();
  applet._routingSettingsReady = true;
  applet.routingCreditOverrides = [{
    scope: 0,
    identifier: "bad id",
    enabled: true,
    allow: true,
  }];
  let reloads = 0;
  applet._loadRoutingState = () => { reloads += 1; };

  assert.doesNotThrow(() => applet._onRoutingSettingsChanged());
  assert.equal(reloads, 1);
  assert.equal(applet._routingPolicyApplying, false);
});

test("routing policy synchronization rebuilds sorted rows and clears stale state", () => {
  const applet = makeApplet();
  const writes = [];
  let guardReleases = 0;
  applet.settings = { setValue: (key, value) => writes.push([key, value]) };
  applet._deferGuardRelease = (property) => {
    assert.equal(property, "_syncingRoutingSettings");
    guardReleases += 1;
  };
  applet._syncRoutingSettings({
    schema_version: 1,
    global: true,
    credit_limits: { hourly: 3, weekly: 7, monthly: 11 },
    account: { beta: false, alpha: true },
    group: {},
    agent: {},
    job: {},
    credit_limit_overrides: {
      account: { alpha: { hourly: 0, weekly: null, monthly: 5 }, gamma: { hourly: 2 } },
      group: {},
      agent: {},
      job: {},
    },
  });

  assert.deepEqual(JSON.parse(JSON.stringify(applet.routingCreditOverrides)), [
    {scope: 0, identifier: "alpha", enabled: true, allow: true, "hourly-limit": 0, "weekly-limit": 0, "monthly-limit": 5},
    {scope: 0, identifier: "beta", enabled: true, allow: false, "hourly-limit": 0, "weekly-limit": 0, "monthly-limit": 0},
    {scope: 0, identifier: "gamma", enabled: false, allow: false, "hourly-limit": 2, "weekly-limit": 0, "monthly-limit": 0},
  ]);
  assert.equal(applet.routingGlobalPaidCredits, true);
  assert.equal(applet.routingCreditHourlyLimit, 3);
  assert.equal(applet.routingCreditWeeklyLimit, 7);
  assert.equal(applet.routingCreditMonthlyLimit, 11);
  assert.equal(guardReleases, 1);
  assert.equal(writes.length, 5);

  applet._routingPolicy = {schema_version: 1};
  applet._routingSettingsReady = true;
  applet._routingDecisions = {alpha: {decision: "main"}};
  let refreshed = 0;
  applet._refreshFormattedSurfaces = () => { refreshed += 1; };
  applet._clearRoutingState();
  assert.equal(applet._routingPolicy, null);
  assert.deepEqual(JSON.parse(JSON.stringify(applet._routingDecisions)), {});
  assert.equal(applet._routingSettingsReady, false);
  assert.equal(refreshed, 1);
  applet._clearRoutingState();
  assert.equal(refreshed, 1);
});

test("series validation allows inactive reservations but rejects active duplicates", () => {
  const applet = makeApplet();
  const inactiveReservation = [
    {account: "alpha", series: "C", "series-active": true},
    {account: "beta", series: "C", "series-active": false},
  ];
  assert.doesNotThrow(() => applet._validateSeriesAssignments(inactiveReservation));

  assert.throws(() => applet._validateSeriesAssignments([
    {account: "alpha", series: "C", "series-active": true},
    {account: "beta", series: "C", "series-active": true},
  ]), /Serie C ist bereits Account alpha zugeordnet/);
});

test("routing status validation keeps bounded decisions", () => {
  const applet = makeApplet();
  const state = applet._validateRoutingState({
    schema_version: 1,
    policy: {
      schema_version: 1,
      global: false,
      account: {},
      group: {},
      agent: {},
      job: {},
    },
    decisions: {
      alpha: {
        decision: "spark",
        model: "gpt-5.3-codex-spark",
        reason: "spark_available",
        paid_overage_allowed: false,
        policy_source: "global",
        usage_state: "known",
      },
    },
  });

  assert.equal(state.decisions.alpha.decision, "spark");
  assert.equal(applet._routingDecisionParts({ account: "alpha" }), null);
  applet._routingDecisions = state.decisions;
  assert.equal(applet._routingDecisionParts({ account: "alpha" }).plain, "Routing Spark · Regel global");
});

test("routing status rejects inconsistent credit decisions", () => {
  const applet = makeApplet();
  assert.throws(() => applet._validateRoutingState({
    schema_version: 1,
    policy: {
      schema_version: 1,
      global: false,
      account: {},
      group: {},
      agent: {},
      job: {},
    },
    decisions: {
      alpha: {
        decision: "credits",
        model: "gpt-5.4-mini",
        reason: "paid_overage_explicitly_allowed",
        paid_overage_allowed: false,
        policy_source: "global",
        usage_state: "known",
      },
    },
  }), /credits decision without paid-overage approval/);
});

test("routing status rejects normalized trusted identities", () => {
  const applet = makeApplet();
  const makePayload = () => ({
    schema_version: 1,
    policy: {
      schema_version: 1,
      global: false,
      account: {},
      group: {},
      agent: {},
      job: {},
    },
    decisions: {
      alpha: {
        decision: "spark",
        model: "gpt-5.3-codex-spark",
        reason: "spark_available",
        paid_overage_allowed: false,
        policy_source: "global",
        usage_state: "known",
      },
    },
  });

  const decisionCases = [
    ["account key", (payload) => {
      payload.decisions = { " alpha": payload.decisions.alpha };
    }],
    ["decision", (payload) => {
      payload.decisions.alpha.decision = "spark ";
    }],
    ["model", (payload) => {
      payload.decisions.alpha.model = "gpt-5.3-codex-spark ";
    }],
    ["usage state", (payload) => {
      payload.decisions.alpha.usage_state = "known ";
    }],
    ["policy key", (payload) => {
      payload.policy.account = { " alpha": true };
    }],
  ];
  decisionCases.forEach(([, mutate]) => {
    const payload = makePayload();
    mutate(payload);
    assert.throws(() => applet._validateRoutingState(payload));
  });
});

test("routing status rejects incomplete synchronized account decisions", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = { alpha: {}, beta: {} };

  assert.throws(() => applet._validateRoutingState({
    schema_version: 1,
    policy: {
      schema_version: 1,
      global: false,
      account: {},
      group: {},
      agent: {},
      job: {},
    },
    decisions: {
      alpha: {
        decision: "blocked",
        model: null,
        reason: "main_limit_unknown",
        paid_overage_allowed: false,
        policy_source: "global",
        usage_state: "unknown",
      },
    },
  }), /incomplete routing decisions/);
});

test("routing status errors clear old decisions", () => {
  const applet = makeApplet();
  applet._routingPolicy = { schema_version: 1, global: true };
  applet._routingDecisions = { alpha: { decision: "credits" } };
  applet._routingSettingsReady = true;
  let refreshes = 0;
  applet._refreshFormattedSurfaces = () => { refreshes += 1; };
  applet._baseCommandArgv = () => [];
  applet._spawnAuxJson = (_argv, callback) => callback(null, "bridge unavailable");

  applet._loadRoutingState();

  assert.equal(applet._routingPolicy, null);
  assert.deepEqual(Object.keys(applet._routingDecisions), []);
  assert.equal(applet._routingSettingsReady, false);
  assert.equal(refreshes, 1);
});

test("routing policy writes reject incomplete results", () => {
  const applet = makeApplet();
  applet._baseCommandArgv = () => ["codex-usage"];
  let calls = 0;
  applet._spawnAuxJson = (_argv, callback) => {
    calls += 1;
    callback({}, null);
  };
  let shown = 0;
  let reloads = 0;
  applet._showCommandError = () => { shown += 1; };
  applet._loadRoutingState = () => { reloads += 1; };

  applet._applyRoutingPolicyCommands([["global", "allow"], ["account", "allow", "alpha"]], 0);

  assert.equal(applet._routingPolicyApplying, false);
  assert.equal(shown, 1);
  assert.equal(reloads, 1);
  assert.equal(calls, 1);
});

test("routing policy writes reject results without requested mutation", () => {
  const applet = makeApplet();
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._spawnAuxJson = (_argv, callback) => callback({
    schema_version: 1,
    global: false,
    account: {},
    group: {},
    agent: {},
    job: {},
  }, null);
  let shown = 0;
  let reloads = 0;
  applet._showCommandError = () => { shown += 1; };
  applet._loadRoutingState = () => { reloads += 1; };

  applet._applyRoutingPolicyCommands([["global", "allow"], ["account", "allow", "alpha"]], 0);

  assert.equal(applet._routingPolicyApplying, false);
  assert.equal(shown, 1);
  assert.equal(reloads, 1);
});

test("browser values do not merge with unknown provenance", () => {
  const applet = makeApplet();
  const browser = { backend_used: "browser", backend_configured: "direct" };
  const unknown = { backend_used: "", backend_configured: "direct" };

  assert.equal(applet._backendProvenanceMatches(browser, unknown), false);
  assert.equal(applet._backendProvenanceMatches(unknown, browser), false);
  assert.equal(
    applet._backendProvenanceMatches(browser, {
      backend_used: "browser",
      backend_configured: "direct",
    }),
    true
  );
});

test("backend identity helpers distinguish incomplete, matching and compatible identities", () => {
  const applet = makeApplet();
  const known = { backend_user_id: "user-1", backend_account_id: "account-1" };

  assert.equal(applet._backendIdentityPresent(known), true);
  assert.equal(applet._backendIdentityPresent({}), false);
  assert.equal(applet._backendIdentityIsIncomplete({ backend_user_id: "user-1" }, known), true);
  assert.equal(applet._backendIdentityIsIncomplete({ backend_account_id: "account-1" }, known), false);
  assert.equal(applet._backendIdentityIsIncomplete({ backend_user_id: "other" }, known), false);
  assert.equal(applet._backendIdentityIsIncomplete({}, {}), false);

  assert.equal(applet._backendIdentityMatches(known, known), true);
  assert.equal(applet._backendIdentityMatches(
    { backend_account_id: "account-1" },
    known
  ), true);
  assert.equal(applet._backendIdentityMatches(
    { backend_account_id: "other" },
    known
  ), false);
  assert.equal(applet._backendIdentityMatches(
    { backend_user_id: "user-1" },
    { backend_user_id: "user-1" }
  ), true);
  assert.equal(applet._backendIdentityMatches(
    { backend_user_id: "user-1" },
    { backend_user_id: "other" }
  ), false);

  assert.equal(applet._backendIdentityCompatible(known, {
    backend_account_id: "account-1",
    backend_user_id: "user-2"
  }), false);
  assert.equal(applet._backendIdentityCompatible(known, {
    backend_account_id: "account-1",
    backend_user_id: "user-1"
  }), true);
  assert.equal(applet._backendIdentityCompatible(
    { backend_user_id: "user-1" },
    { backend_user_id: "user-1" }
  ), true);
  assert.equal(applet._backendIdentityCompatible(known, {
    backend_user_id: "user-1"
  }), false);
});

test("backend fallback proof requires a known reason and matching backend direction", () => {
  const applet = makeApplet();
  const fallback = {
    backend_configured: "app-server",
    backend_used: "direct",
    fallback_reason: "app-server unavailable: installed Codex does not support rate-limit RPC"
  };

  assert.equal(applet._hasBackendFallbackProof(fallback), true);
  assert.equal(applet._hasBackendFallbackProof({
    ...fallback,
    fallback_reason: "app-server unavailable: made-up reason"
  }), false);
  assert.equal(applet._hasBackendFallbackProof({
    ...fallback,
    backend_used: "browser"
  }), false);
  assert.equal(applet._hasBackendFallbackProof({
    backend_configured: "direct",
    backend_used: "direct",
    fallback_reason: fallback.fallback_reason
  }), false);
  assert.equal(applet._hasBackendFallbackProof({
    backend_configured: "direct",
    backend_used: "direct",
    fallback_reason: "previous authenticated limits retained after reset transition"
  }), true);
});

test("model pool lookup is own-property and object-only", () => {
  const applet = makeApplet();
  const pool = { available: true };

  assert.equal(applet._modelPool({ models: { main: pool } }, "main"), pool);
  assert.equal(applet._modelPool({ models: { main: null } }, "main"), null);
  assert.equal(applet._modelPool({ models: { main: [] } }, "main"), null);
  assert.equal(applet._modelPool({ models: {} }, "toString"), null);
  assert.equal(applet._modelPool({ models: Object.create(null) }, "toString"), null);
  assert.equal(applet._modelPool({}, "main"), null);
});

test("backend state helpers distinguish configured, cached and empty usage", () => {
  const applet = makeApplet();
  const direct = {
    backend_configured: "direct",
    backend_used: "direct",
    five_hour: { remaining: 80 },
  };
  const empty = {
    backend_configured: "direct",
    backend_used: "",
    five_hour: null,
    weekly: null,
    main: null,
    models: Object.create(null),
  };

  assert.equal(applet._backendMatchesConfigured(direct, "direct"), true);
  assert.equal(applet._backendMatchesConfigured(direct, "app-server"), false);
  assert.equal(applet._backendMatchesConfigured(empty, "app-server"), false);
  assert.equal(applet._backendMatchesConfigured({
    backend_configured: "app-server",
    backend_used: "direct",
    fallback_reason: "app-server unavailable: installed Codex does not support rate-limit RPC"
  }, "app-server"), true);
  assert.equal(applet._backendMatchesConfigured({
    backend_configured: "app-server",
    backend_used: "browser",
    five_hour: { remaining: 50 }
  }, "app-server"), false);

  assert.equal(applet._authoritativeEmptyLimits({
    status: "partial", backend_used: "direct",
    five_hour: null, weekly: null, main: null, models: {}
  }), true);
  assert.equal(applet._authoritativeEmptyLimits({
    status: "partial", backend_used: "browser",
  }), false);
  assert.equal(applet._authenticatedPartial({
    status: "partial", backend_used: "app-server"
  }), true);
  assert.equal(applet._authenticatedPartial({
    status: "partial", backend_used: "browser"
  }), false);
  assert.equal(applet._hasCachedWindows({ five_hour: {} }), true);
  assert.equal(applet._hasCachedWindows({ main: { windows: [] } }), false);
  assert.equal(applet._hasDynamicWindows({
    models: { spark: { windows: [{ name: "5h" }] } }
  }), true);
  assert.equal(applet._hasDynamicWindows({ models: { spark: { windows: [] } } }), false);
  assert.equal(applet._hasResetlessBrowserUsage({
    backend_used: "browser",
    main: { windows: [{ remaining: 80 }] }
  }), true);
  assert.equal(applet._hasResetlessBrowserUsage({
    backend_used: "direct",
    main: { windows: [{ remaining: 80 }] }
  }), false);
});

test("pool reset merge fills matching missing resets without touching invalid pools", () => {
  const applet = makeApplet();
  const freshPool = {
    available: true,
    windows: [{ name: "5h", duration_seconds: 18000, remaining: 80 }]
  };
  const cachedPool = {
    available: true,
    windows: [{
      name: "five_hour",
      duration_seconds: 18000,
      remaining: 70,
      reset_at: "2026-08-19T15:00:00Z"
    }]
  };
  assert.equal(applet._windowIdentityKey(freshPool.windows[0]), 18000);
  assert.equal(applet._windowIdentityKey(cachedPool.windows[0]), 18000);
  assert.equal(applet._hasUniqueWindowIdentities(freshPool.windows), true);
  assert.equal(applet._hasUniqueWindowIdentities(cachedPool.windows), true);
  assert.equal(applet._windowHasUsageValue(freshPool.windows[0]), true);
  assert.equal(applet._windowDurationMatches(
    freshPool.windows[0], cachedPool.windows[0], undefined
  ), true);
  assert.equal(applet._windowCacheExpired(
    cachedPool.windows[0], "2026-08-19T10:00:00Z", "2026-08-19T10:00:00Z"
  ), false);
  assert.equal(applet._mergeMissingPoolResetsForPool(
    freshPool, cachedPool,
    "2026-08-19T10:00:00Z",
    "2026-08-19T10:00:00Z"
  ), true);
  assert.equal(freshPool.windows[0].remaining, 80);
  assert.equal(freshPool.windows[0].reset_at, "2026-08-19T15:00:00Z");

  const unavailable = {
    available: false,
    windows: [{ name: "5h", duration_seconds: 18000, remaining: 80 }]
  };
  assert.equal(applet._mergeMissingPoolResetsForPool(
    unavailable, cachedPool,
    "2026-08-19T10:00:00Z",
    "2026-08-19T09:00:00Z"
  ), false);
  assert.equal(unavailable.windows[0].reset_at, undefined);
});

test("backend summary does not invent missing backend usage", () => {
  const applet = makeApplet();

  assert.equal(
    applet._backendSummary({ backend_configured: "direct", backend_used: "" }),
    "Unbekannt"
  );
  assert.equal(applet._backendSummary({}), "Unbekannt");
  assert.equal(
    applet._backendSummary({ backend_configured: "direct", backend_used: "direct" }),
    "Direkt"
  );
  assert.equal(
    applet._backendSummary({ backend_configured: "app-server", backend_used: "direct" }),
    "App Server → Direkt"
  );
});

test("fresh browser partial does not restore an unknown cached window", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    label: "Alpha",
    captured_at: new Date(Date.now() - 60 * 1000).toISOString(),
    status: "ok",
    backend_configured: "direct",
    backend_used: "",
    five_hour: { remaining: 80 },
    weekly: { remaining: 10 },
  }];

  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    label: "Alpha",
    captured_at: new Date().toISOString(),
    status: "partial",
    backend_configured: "direct",
    backend_used: "browser",
    five_hour: { remaining: 70 },
    weekly: null,
  }]);

  assert.equal(merged.length, 1);
  assert.equal(merged[0].five_hour.remaining, 70);
  assert.equal(merged[0].weekly, null);
});

test("incomplete provenance cannot restore cached usage during fresh merge", () => {
  const applet = makeApplet();
  const capturedAt = new Date();
  const resetAt = new Date(capturedAt.getTime() + 5 * 60 * 60 * 1000).toISOString();
  applet._usages = [{
    account: "alpha",
    label: "Alpha",
    captured_at: new Date(capturedAt.getTime() - 60 * 1000).toISOString(),
    status: "ok",
    backend_configured: "direct",
    backend_used: "",
    five_hour: { name: "5h", remaining: 80, reset_at: resetAt },
    weekly: null,
  }];

  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    label: "Alpha",
    captured_at: capturedAt.toISOString(),
    status: "partial",
    backend_configured: "direct",
    backend_used: "direct",
    five_hour: { name: "5h", reset_at: resetAt },
    weekly: null,
  }]);

  assert.equal(merged.length, 1);
  assert.equal(merged[0].five_hour.remaining, undefined);
  assert.equal(applet._remainingPercent(merged[0].five_hour), null);
});

test("a missed five-minute poll marks cached values stale after one grace minute", () => {
  const applet = makeApplet();
  applet.refreshInterval = 300;
  applet._usages = [{
    account: "alpha",
    captured_at: new Date(Date.now() - 5 * 60 * 1000 - 59 * 1000).toISOString(),
  }];
  assert.equal(applet._cacheIsStale(), false);

  applet._usages[0].captured_at = new Date(
    Date.now() - 5 * 60 * 1000 - 61 * 1000
  ).toISOString();
  assert.equal(applet._cacheIsStale(), true);
});

test("far-future captures are stale and cannot replace current usage", () => {
  const applet = makeApplet();
  const currentCapture = new Date(Date.now() - 60 * 1000).toISOString();
  const farFutureCapture = new Date(Date.now() + 10 * 60 * 1000).toISOString();
  applet._usages = [{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    captured_at: currentCapture,
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
    status: "ok",
    stale: false,
  }];

  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    captured_at: farFutureCapture,
    five_hour: { remaining: 10 },
    weekly: { remaining: 20 },
    status: "ok",
    stale: false,
  }]);

  assert.equal(merged[0].captured_at, currentCapture);
  assert.equal(merged[0].five_hour.remaining, 80);
  assert.equal(merged[0].weekly.remaining, 60);
  applet._usages = [{ account: "alpha", captured_at: farFutureCapture, stale: false }];
  assert.equal(applet._cacheIsStale(), true);
  assert.equal(applet._newestCapture(), "");
});

test("a fresh account cannot hide another account's stale cache", () => {
  const applet = makeApplet();
  applet.refreshInterval = 300;
  applet._usages = [
    {
      account: "alpha",
      captured_at: new Date(Date.now() - 7 * 60 * 1000).toISOString(),
      stale: false,
    },
    {
      account: "beta",
      captured_at: new Date(Date.now() - 30 * 1000).toISOString(),
      stale: false,
    },
  ];

  assert.equal(applet._cacheIsStale(), true);
});

test("systemd display cadence reloads a locally stale cache without fresh polling", () => {
  let displayCallback;
  const applet = makeApplet((runtime) => {
    runtime.timeoutAddSeconds = (_seconds, callback) => {
      displayCallback = callback;
      return 4;
    };
  });
  applet._systemdActive = true;
  applet.pollOwner = "systemd";
  applet._usages = [{
    account: "alpha",
    captured_at: new Date(Date.now() - 61 * 1000).toISOString(),
  }];
  let cachedLoads = 0;
  applet._loadCached = (refreshAfter, refreshAuxiliaryState) => {
    cachedLoads += 1;
    assert.equal(refreshAfter, false);
    assert.equal(refreshAuxiliaryState, false);
  };
  applet._updatePanel = () => {};
  applet._runSafely = (_context, callback) => callback();

  assert.equal(applet._scheduleDisplayTimer(), true);
  assert.ok(displayCallback);
  displayCallback();
  assert.equal(cachedLoads, 1);
});

test("cache sync cooldown does not repeat an unchanged reload immediately", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
    stale: false,
  }];
  applet._lastCacheSyncAt = Date.now();
  assert.equal(applet._cacheNeedsSync(), false);

  applet._lastCacheSyncAt = Date.now() - 61 * 1000;
  assert.equal(applet._cacheNeedsSync(), true);
});

test("cached load records a sync timestamp for an unchanged snapshot", () => {
  const applet = makeApplet();
  applet._applyPayload = () => {};
  applet._spawnUsageCommand = (_subcommand, callback) => callback([], null);
  const before = Date.now();

  applet._loadCached(false, false);

  assert.ok(applet._lastCacheSyncAt >= before);
  assert.equal(applet._cacheNeedsSync(), false);
});

test("failed cached payload handling does not arm the sync cooldown", () => {
  const applet = makeApplet();
  applet._applyPayload = () => { throw new Error("payload handler failed"); };
  applet._spawnUsageCommand = (_subcommand, callback) => callback([], null);

  assert.throws(() => applet._loadCached(false, false), /payload handler failed/);
  assert.equal(applet._lastCacheSyncAt, 0);
});

test("failed cached command does not arm the sync cooldown", () => {
  const applet = makeApplet();
  applet._showCommandError = () => {};
  applet._spawnUsageCommand = (_subcommand, callback) => callback(null, "cache failed");

  applet._loadCached(false, false);

  assert.equal(applet._lastCacheSyncAt, 0);
});

test("cached payloads preserve omitted accounts and newer values", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
    beta: { account: "beta", label: "Beta", backend: 0 },
    gamma: { account: "gamma", label: "Gamma", backend: 0 },
  };
  applet._usages = [
    {
      account: "alpha",
      label: "Alpha",
      backend_configured: "direct",
      backend_used: "direct",
      captured_at: new Date(Date.now() - 10 * 1000).toISOString(),
      status: "ok",
      five_hour: { remaining: 80 },
      weekly: { remaining: 60 },
    },
    {
      account: "beta",
      label: "Beta",
      backend_configured: "direct",
      backend_used: "direct",
      captured_at: new Date(Date.now() - 20 * 1000).toISOString(),
      status: "ok",
      five_hour: { remaining: 70 },
      weekly: { remaining: 50 },
    },
  ];
  applet._buildUsageMenu = () => {};
  applet._updatePanel = () => {};
  applet._notifyForPayload = () => {};

  applet._applyPayload([
    {
      account: "alpha",
      label: "Alpha",
      backend_configured: "direct",
      backend_used: "direct",
      captured_at: new Date(Date.now() - 30 * 1000).toISOString(),
      status: "ok",
      five_hour: { remaining: 20 },
      weekly: { remaining: 10 },
    },
  ], false);

  const byAccount = Object.fromEntries(applet._usages.map((usage) => [usage.account, usage]));
  assert.equal(byAccount.alpha.five_hour.remaining, 80);
  assert.equal(byAccount.beta.five_hour.remaining, 70);
  assert.equal(byAccount.beta.status, "partial");
  assert.equal(byAccount.beta.stale, true);
  assert.equal(byAccount.gamma.status, "partial");
  assert.equal(byAccount.gamma.stale, true);
});

test("cache invalidation clears old account values instead of preserving them", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
  };
  applet._usages = [{
    account: "alpha",
    label: "Alpha",
    captured_at: new Date().toISOString(),
    status: "ok",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
    backend_user_id: "old-user",
    backend_account_id: "old-account",
  }];

  const merged = applet._mergeCachedPayload([{
    account: "alpha",
    label: "Alpha",
    captured_at: new Date().toISOString(),
    status: "partial",
    five_hour: { remaining: 1 },
    weekly: { remaining: 2 },
    stale: true,
    cache_invalidated: true,
  }]);

  assert.equal(merged.length, 1);
  assert.equal(merged[0].cache_invalidated, true);
  assert.equal(merged[0].five_hour, null);
  assert.equal(merged[0].weekly, null);
});

test("cache invalidation clears embedded limit windows", () => {
  const applet = makeApplet();
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    captured_at: "2026-07-10T10:05:00.000Z",
    status: "ok",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
    values_captured_at: "2026-07-10T10:00:00.000Z",
    cache_invalidated: true,
  }]);

  assert.equal(merged[0].status, "partial");
  assert.equal(merged[0].five_hour, null);
  assert.equal(merged[0].weekly, null);
  assert.equal(merged[0].values_captured_at, null);
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

test("source and guard lifecycle helpers release stale references", () => {
  const callbacks = [];
  const applet = makeApplet((runtime) => {
    runtime.idleAdd = (callback) => {
      callbacks.push(callback);
      return callbacks.length;
    };
  });
  let called = 0;
  const idleId = applet._addIdle(() => { called += 1; });
  assert.equal(idleId, 1);
  assert.equal(applet._idleSources[1], true);
  callbacks[0]();
  assert.equal(called, 1);
  assert.equal(applet._idleSources[1], undefined);

  applet._setSource("_timerId", 17);
  assert.equal(applet._timerId, 17);
  assert.equal(applet._sources._timerId, 17);
  applet._clearSource("_timerId");
  assert.equal(applet._timerId, 0);
  assert.equal(applet._sources._timerId, undefined);
  applet._setSource("_timerId", 18);
  applet._removeSource("_timerId");
  assert.equal(applet._timerId, 0);
  assert.equal(applet._sources._timerId, undefined);

  applet._guard = true;
  applet._deferGuardRelease("_guard", "test guard");
  applet._deferGuardRelease("_guard", "test guard");
  assert.equal(applet._guard, true);
  callbacks[1]();
  assert.equal(applet._guard, true);
  callbacks[2]();
  assert.equal(applet._guard, false);

  applet._idleSources[99] = true;
  applet._removeIdleSources();
  assert.deepEqual(Object.keys(applet._idleSources), []);
});

test("safe execution and refresh circuit helpers have bounded failure state", () => {
  const applet = makeApplet();
  let failures = 0;
  applet._recordInternalFailure = () => { failures += 1; };
  assert.equal(applet._runSafely("test", () => { throw new Error("broken"); }, "fallback"), "fallback");
  assert.equal(failures, 1);
  applet._removed = true;
  assert.equal(applet._runSafely("removed", () => "wrong", "fallback"), "fallback");
  applet._removed = false;

  let panelUpdates = 0;
  applet._updatePanel = () => { panelUpdates += 1; };
  applet._recordRefreshFailure("one");
  applet._recordRefreshFailure("two");
  applet._recordRefreshFailure("three");
  assert.equal(applet._refreshFailures, 3);
  assert.equal(applet._circuitOpen(), true);
  assert.equal(panelUpdates, 1);
  applet._recordRefreshSuccess();
  assert.equal(applet._refreshFailures, 0);
  assert.equal(applet._lastRefreshError, "");
  assert.equal(applet._commandError, "");
  applet._circuitOpenUntil = Date.now() - 1;
  assert.equal(applet._circuitOpen(), false);
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
    { installed: true, enabled: true, active: true, service_result: "success" },
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
    { installed: true, enabled: true, active: true, service_result: "success" },
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

test("stale cached values mark the panel as a warning", () => {
  const applet = makeApplet();
  const classes = [];
  applet._clearPanelClasses = () => { classes.length = 0; };
  applet.actor = {
    add_style_class_name: (name) => classes.push(name),
    remove_style_class_name() {},
  };
  applet._usages[0].stale = true;

  applet._updatePanel();

  assert.ok(classes.includes("codex-usage-panel-warning"));
  assert.equal(classes.includes("codex-usage-panel-error"), false);
});

test("partial usage marks the panel and account row as incomplete", () => {
  const applet = makeApplet();
  const classes = [];
  applet._clearPanelClasses = () => { classes.length = 0; };
  applet.actor = {
    add_style_class_name: (name) => classes.push(name),
    remove_style_class_name() {},
  };
  applet._usages[0].status = "partial";

  applet._updatePanel();

  assert.ok(classes.includes("codex-usage-panel-warning"));
  assert.equal(
    applet._usageSeverity(applet._usages[0]),
    "codex-usage-warning"
  );
  applet._usages[0].five_hour = null;
  applet._usages[0].weekly = null;
  assert.equal(
    applet._usageSeverity(applet._usages[0]),
    "codex-usage-warning"
  );
});

test("blocked usage is never rendered as a normal empty account", () => {
  const applet = makeApplet();
  const classes = [];
  applet._clearPanelClasses = () => { classes.length = 0; };
  applet.actor = {
    add_style_class_name: (name) => classes.push(name),
    remove_style_class_name() {},
  };
  applet._usages[0].status = "blocked";
  applet._usages[0].five_hour = null;
  applet._usages[0].weekly = null;

  applet._updatePanel();

  assert.ok(classes.includes("codex-usage-panel-error"));
  assert.equal(applet._usageSeverity(applet._usages[0]), "codex-usage-error");
});

test("blocked usage enters the per-account error notification state", () => {
  const applet = makeApplet();
  applet.notifyErrors = false;
  applet._usages[0].status = "blocked";

  applet._notifyForPayload();

  assert.equal(applet._errorState["alpha:blocked"], true);
});

test("same error notification is suppressed for 48 hours and survives restart", () => {
  const notifications = [];
  let storedState = "{}";
  let now = 1000000;
  const applet = makeApplet((runtime) => {
    runtime.onNotify = (...args) => notifications.push(args);
  });
  applet.notifyErrors = true;
  applet._alertSettings = { alpha: { account: "alpha", errors: true } };
  applet._errorNotificationNow = () => now;
  applet.settings = {
    setValue(key, value) {
      assert.equal(key, "error-notification-state");
      storedState = value;
    },
  };
  applet._usages[0].status = "blocked";
  applet._usages[0].error = "backend failed";

  applet._notifyForPayload();
  assert.equal(notifications.length, 1);
  assert.doesNotMatch(storedState, /backend failed/);

  applet._usages[0].status = "ok";
  applet._notifyForPayload();
  applet._usages[0].status = "blocked";
  applet._notifyForPayload();
  assert.equal(notifications.length, 1);

  const restarted = makeApplet((runtime) => {
    runtime.onNotify = (...args) => notifications.push(args);
  });
  restarted.notifyErrors = true;
  restarted._alertSettings = { alpha: { account: "alpha", errors: true } };
  restarted.errorNotificationState = storedState;
  restarted._errorNotificationNow = () => now;
  restarted._usages[0].status = "blocked";
  restarted._usages[0].error = "backend failed";
  restarted._notifyForPayload();
  assert.equal(notifications.length, 1);

  now += 48 * 60 * 60 * 1000;
  restarted._usages[0].status = "ok";
  restarted._notifyForPayload();
  restarted._usages[0].status = "blocked";
  restarted._notifyForPayload();
  assert.equal(notifications.length, 2);
});

test("suppressed error persists pruned notification state", () => {
  const applet = makeApplet();
  const now = 200_000_000;
  const key = "persist-pruned";
  const current = applet._errorNotificationFingerprint(key);
  const expired = applet._errorNotificationFingerprint("expired");
  const future = applet._errorNotificationFingerprint("future");
  const writes = [];
  applet._errorNotificationNow = () => now;
  applet.errorNotificationState = JSON.stringify({
    [current]: now - 1,
    [expired]: now - 48 * 60 * 60 * 1000,
    invalid: "not-a-timestamp",
    [future]: now + 1,
  });
  applet.settings = {
    setValue(keyName, value) {
      writes.push([keyName, value]);
    },
  };

  assert.equal(applet._shouldNotifyError(key), false);
  assert.equal(writes.length, 1);
  assert.equal(writes[0][0], "error-notification-state");
  assert.deepEqual(JSON.parse(writes[0][1]), { [current]: now - 1 });
  assert.equal(applet.errorNotificationState, writes[0][1]);
});

test("suppressed error caps oversized valid notification state", () => {
  const applet = makeApplet();
  const now = 200_000_000;
  const key = "persist-bounded";
  const current = applet._errorNotificationFingerprint(key);
  const state = { [current]: now };
  for (let index = 0; index < 129; index++) {
    state[applet._errorNotificationFingerprint(`bounded-${index}`)] = now - index - 1;
  }
  const writes = [];
  applet._errorNotificationNow = () => now;
  applet.errorNotificationState = JSON.stringify(state);
  applet.settings = {
    setValue(keyName, value) {
      writes.push([keyName, value]);
    },
  };

  assert.equal(applet._shouldNotifyError(key), false);
  assert.equal(writes.length, 1);
  assert.equal(writes[0][0], "error-notification-state");
  assert.equal(Object.keys(JSON.parse(writes[0][1])).length, 128);
  assert.equal(JSON.parse(writes[0][1])[current], now);
  assert.equal(applet.errorNotificationState, writes[0][1]);
});

test("suppressed error retries failed prune persistence without unsuppressing", () => {
  const applet = makeApplet();
  const now = 200_000_000;
  const key = "persist-failure";
  const current = applet._errorNotificationFingerprint(key);
  const expired = applet._errorNotificationFingerprint("expired-failure");
  const attempts = [];
  applet._errorNotificationNow = () => now;
  applet.errorNotificationState = JSON.stringify({
    [current]: now - 1,
    [expired]: now - 48 * 60 * 60 * 1000,
  });
  applet.settings = {
    setValue(keyName, value) {
      attempts.push([keyName, value]);
      if (attempts.length === 1) {
        throw new Error("settings write failed");
      }
    },
  };

  assert.equal(applet._shouldNotifyError(key), false);
  assert.equal(applet._shouldNotifyError(key), false);
  assert.equal(applet._shouldNotifyError(key), false);

  assert.equal(attempts.length, 2);
  assert.equal(attempts[0][0], "error-notification-state");
  assert.deepEqual(JSON.parse(attempts[0][1]), { [current]: now - 1 });
  assert.deepEqual(attempts[1], attempts[0]);
  assert.equal(applet.errorNotificationState, attempts[1][1]);
});

test("active error retries failed notification persistence without duplicate", () => {
  const notifications = [];
  const attempts = [];
  const applet = makeApplet((runtime) => {
    runtime.onNotify = (...args) => notifications.push(args);
  });
  applet.notifyErrors = true;
  applet._alertSettings = { alpha: { account: "alpha", errors: true } };
  applet.settings = {
    setValue(keyName, value) {
      attempts.push([keyName, value]);
      if (attempts.length === 1) {
        throw new Error("settings write failed");
      }
    },
  };
  applet._usages[0].status = "blocked";
  applet._usages[0].error = "backend failed";

  applet._notifyForPayload();
  applet._notifyForPayload();
  applet._notifyForPayload();

  assert.equal(notifications.length, 1);
  assert.equal(attempts.length, 2);
  assert.equal(attempts[0][0], "error-notification-state");
  assert.deepEqual(attempts[1], attempts[0]);
  assert.doesNotMatch(attempts[1][1], /backend failed/);
});

test("error notification fingerprints distinguish old 32-bit collisions", () => {
  const applet = makeApplet();
  const first = applet._errorNotificationFingerprint("collision-1upg");
  const second = applet._errorNotificationFingerprint("collision-dca1");

  assert.notEqual(first, second);
  assert.match(first, /^[0-9a-f]{8}-[0-9a-f]{8}$/);
  assert.match(second, /^[0-9a-f]{8}-[0-9a-f]{8}$/);
});

test("future error notification timestamps do not suppress errors", () => {
  const applet = makeApplet();
  const now = 1_000_000;
  const fingerprint = applet._errorNotificationFingerprint("clock-skew");
  applet._errorNotificationNow = () => now;
  applet.errorNotificationState = JSON.stringify({
    [fingerprint]: now + 48 * 60 * 60 * 1000,
  });

  assert.equal(applet._shouldNotifyError("clock-skew"), true);
  assert.equal(
    JSON.parse(applet.errorNotificationState)[fingerprint],
    now,
  );
});

test("oversized error notification state is rejected before JSON parse", () => {
  const applet = makeApplet();
  const oversizedState = {};
  for (let index = 0; index < 2000; index++) {
    oversizedState["entry-" + index] = index;
  }
  const raw = JSON.stringify(oversizedState);
  assert.ok(raw.length > 16384);
  const originalParse = JSON.parse;
  let parsedOversized = false;
  JSON.parse = (value) => {
    if (typeof value === "string" && value.length > 16384) {
      parsedOversized = true;
    }
    return originalParse(value);
  };
  try {
    applet.errorNotificationState = raw;
    assert.equal(applet._shouldNotifyError("oversized-state"), true);
  } finally {
    JSON.parse = originalParse;
  }
  assert.equal(parsedOversized, false);
  assert.ok(applet.errorNotificationState.length < 16384);
});

test("partial usage enters the per-account warning notification state", () => {
  const applet = makeApplet();
  applet.notifyWarnings = false;
  applet._usages[0].status = "partial";
  applet._usages[0].error = "weekly window unavailable";

  applet._notifyForPayload();

  assert.equal(applet._warningState["alpha:partial"], true);
});

test("oversized process output force-stops the child and reports a bounded error", () => {
  const applet = makeApplet();
  let forced = 0;
  let result = null;
  const finalSignals = [];
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
  }, (_name, _chunk, final) => {
    finalSignals.push(final === true);
  });
  assert.equal(forced, 1);
  assert.equal(result.stdout, null);
  assert.match(result.error, /zu groß/);
  assert.deepEqual(finalSignals, []);
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

test("command errors remain visible when the display timer redraws the panel", () => {
  const applet = makeApplet();
  const classes = [];
  applet._clearPanelClasses = () => { classes.length = 0; };
  applet.actor = {
    add_style_class_name: (name) => classes.push(name),
    remove_style_class_name() {},
  };

  applet._showCommandError("backend failed");
  applet._updatePanel();
  assert.ok(classes.includes("codex-usage-panel-error"));

  applet._recordRefreshSuccess();
  applet._updatePanel();
  assert.equal(classes.includes("codex-usage-panel-error"), false);
});

test("restlaufzeit is rendered, styled and uses the per-surface target", () => {
  const applet = makeApplet();
  applet._durationStyles = {
    alpha: {
      account: "alpha",
      format: 3,
      mode: 2,
      threshold: 20,
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
    "hover-background": 5,
    "below-bold": true,
    "below-color": 3,
    "below-background": 0,
    "below-hover-background": 6,
  };
  assert.match(applet._styleSpan("80%", style, 80, "panel"), /foreground="#16a34a"/);
  assert.match(applet._styleSpan("80%", style, 80, "hover"), /background="#1d4ed8"/);

  style.mode = 1;
  assert.equal(applet._styleSpan("80%", style, 80, "panel"), "80%");
  assert.match(applet._styleSpan("10%", style, 10, "panel"), /foreground="#16a34a"/);

  style.mode = 2;
  assert.match(applet._styleSpan("80%", style, 80, "panel"), /foreground="#16a34a"/);
  assert.match(applet._styleSpan("10%", style, 10, "panel"), /foreground="#dc2626"/);
  assert.match(applet._styleSpan("10%", style, 10, "panel"), /weight="bold"/);
  assert.match(applet._styleSpan("10%", style, 10, "hover"), /background="#facc15"/);

  style.mode = 3;
  assert.equal(applet._styleSpan("<80%>", style, 10, "panel"), "&lt;80%&gt;");
});

test("date, time and restlaufzeit styles honor all modes and font colors", () => {
  const applet = makeApplet();
  const kinds = ["date", "time", "duration"];
  for (const kind of kinds) {
    const style = {
      mode: 0,
      threshold: 20,
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
    const high = 80;
    const low = 10;
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

test("restlaufzeit threshold uses remaining percentage, not minutes", () => {
  const applet = makeApplet();
  applet._durationStyles.alpha = {
    account: "alpha",
    format: 0,
    mode: 1,
    threshold: 20,
    font: 0,
    size: 0,
    bold: true,
    italic: false,
    color: 3,
    background: 0,
    "below-font": 0,
    "below-size": 0,
    "below-bold": true,
    "below-italic": false,
    "below-color": 3,
    "below-background": 0,
  };
  applet._styleTargets["alpha:3"] = {panel: true, hover: true, click: true};
  const window = {
    remaining: 80,
    reset_at: new Date(Date.now() + 5 * 60000).toISOString(),
  };

  const parts = applet._windowResetParts(window, "alpha", "panel", false);

  assert.equal(parts.plain, "Rest 5m");
  assert.equal(parts.markup, "Rest 5m");
});

test("reset targets control date, time and duration in click menu", () => {
  const applet = makeApplet();
  applet._styleTargets = {
    "alpha:1": {panel: false, hover: false, click: false},
    "alpha:2": {panel: false, hover: false, click: false},
    "alpha:3": {panel: false, hover: false, click: true},
  };
  const window = {
    remaining: 80,
    reset_at: new Date(Date.now() + 5 * 60000).toISOString(),
  };

  const durationOnly = applet._windowResetParts(window, "alpha", "click", false);

  assert.match(durationOnly.plain, /^Rest 5m$/);
  applet._styleTargets["alpha:3"].click = false;
  const hidden = applet._windowResetParts(window, "alpha", "click", false);
  assert.equal(hidden.plain, "");
  assert.equal(hidden.markup, "");
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
  assert.equal(migrated["hover-background"], migrated.background);
  assert.equal(migrated["below-hover-background"], migrated["below-background"]);
});

test("malformed numeric settings are rejected instead of coerced", () => {
  const applet = makeApplet();

  assert.equal(applet._normalizePanelRow({
    tag: "A",
    order: "1",
    muted: false,
    slot1: 1,
    slot2: 0,
  }, "alpha"), null);
  assert.equal(applet._normalizeAlertRow({
    "five-threshold": "20.5",
    "weekly-threshold": "20",
    warnings: true,
    errors: true,
  }, "alpha"), null);
  assert.equal(applet._normalizeStyleRow({
    mode: "1",
    threshold: 20,
    font: 0,
    size: 0,
    bold: false,
    italic: false,
    color: 0,
    background: 0,
    "below-font": 0,
    "below-size": 0,
    "below-bold": true,
    "below-italic": false,
    "below-color": 3,
    "below-background": 0,
  }, "alpha", "percent"), null);
  assert.equal(applet._normalizeTargetRow({
    element: "0",
    panel: true,
    hover: true,
    click: true,
  }, "alpha"), null);
  assert.equal(applet._normalizeTargetRow({
    element: 13,
    panel: true,
    hover: true,
    click: true,
  }, "alpha"), null);
});

test("legacy global baseline style targets are discarded during migration", () => {
  const applet = makeApplet();
  applet._backendAccounts = {alpha: {account: "alpha"}};
  const rows = applet._mergedTargetRows([{account: "alpha"}], [{
    account: "alpha", element: 13, panel: false, hover: false, click: false,
  }]);

  assert.equal(rows.some(row => row.element === 13), false);
  assert.equal(rows.length, 15);
});

test("style target rows cover exactly every editable element once per account", () => {
  const applet = makeApplet();
  applet._backendAccounts = {
    alpha: {account: "alpha"},
    beta: {account: "beta"},
  };
  const rows = applet._mergedTargetRows([
    {account: "alpha"},
    {account: "beta"},
  ], []);

  assert.equal(rows.length, 30);
  for (const account of ["alpha", "beta"]) {
    const elements = rows
      .filter(row => row.account === account)
      .map(row => row.element);
    assert.equal(
      JSON.stringify(elements),
      JSON.stringify([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15])
    );
  }
  assert.equal(rows.some(row => row.element === 13), false);
});

test("short and monthly consumption use independent style targets", () => {
  const applet = makeApplet();
  applet._styleTargets = {
    "alpha:14": {panel: false, hover: true, click: true},
    "alpha:15": {panel: true, hover: false, click: true},
  };

  assert.equal(applet._elementTargetEnabled("alpha", "consumption-short", "panel"), false);
  assert.equal(applet._elementTargetEnabled("alpha", "consumption-short", "hover"), true);
  assert.equal(applet._elementTargetEnabled("alpha", "consumption-monthly", "panel"), true);
  assert.equal(applet._elementTargetEnabled("alpha", "consumption-monthly", "hover"), false);
});

test("short and monthly targets keep consumption requests and panel visibility alive", () => {
  for (const [element, limitWindow] of [[14, "short"], [15, "monthly"]]) {
    const applet = makeApplet();
    applet._usages = [{account: "alpha", cost_windows: []}];
    applet._panelSettings = {
      alpha: {account: "alpha", muted: false, slot1: 0, slot2: 0, slot3: 0, slot4: 0},
    };
    applet._consumptionSettings = {
      alpha: {
        account: "alpha",
        amount: 1,
        unit: "hours",
        smoothing: "none",
        "limit-window": limitWindow,
        "show-panel": false,
        "show-tooltip": false,
        "baseline-enabled": false,
        "forecast-limit-window": limitWindow,
        "forecast-smoothing": "none",
        "forecast-baseline-enabled": false,
      },
    };
    applet._styleTargets = {
      "alpha:4": {panel: false, hover: false, click: false},
      "alpha:5": {panel: false, hover: false, click: false},
      "alpha:10": {panel: false, hover: false, click: false},
      [`alpha:${element}`]: {panel: true, hover: false, click: false},
    };
    applet._drainConsumptionRequests = () => {};

    applet._refreshConsumption();

    assert.equal(applet._consumptionQueue.length, 1);
    assert.equal(applet._consumptionQueue[0].limitWindow, limitWindow);
    assert.equal(applet._panelItems().find(item => item.usage.account === "alpha").visible, true);
  }
});

test("legacy global baseline style target is removed from persisted settings", () => {
  const applet = makeApplet();
  const writes = [];
  applet._backendAccounts = {alpha: {account: "alpha"}};
  applet.accountPercentStyles = [];
  applet.accountDateStyles = [];
  applet.accountTimeStyles = [];
  applet.accountDurationStyles = [];
  applet.accountDisplaySettings = [];
  applet.accountStyleTargets = [{
    account: "alpha", element: 13, panel: false, hover: false, click: false,
  }];
  applet.settings = {setValue: (key, value) => writes.push([key, value])};
  applet._addIdle = () => 0;

  applet._syncStyleRows([{account: "alpha"}]);

  assert.equal(applet.accountStyleTargets.length, 15);
  assert.equal(applet.accountStyleTargets.some(row => row.element === 13), false);
  const persisted = writes.find(([key]) => key === "account-style-targets");
  assert.ok(persisted);
  assert.equal(persisted[1].some(row => row.element === 13), false);
});

test("a legacy global baseline target cannot keep an otherwise empty panel row alive", () => {
  const applet = makeApplet();
  applet._panelSettings.alpha = {
    account: "alpha", order: 1, muted: false, slot1: 0, slot2: 0, slot3: 0, slot4: 0,
  };
  applet._styleTargets["alpha:13"] = {panel: true, hover: true, click: true};
  applet._consumptionSettings.alpha = {
    account: "alpha", "show-panel": false, "show-tooltip": false,
    "baseline-enabled": true,
  };

  const alpha = applet._panelItems().find(item => item.usage.account === "alpha");
  assert.equal(alpha.visible, false);
});

test("unknown display targets fail closed even when a legacy visibility value is true", () => {
  const applet = makeApplet();

  assert.equal(applet._elementTargetEnabled("alpha", "baseline", "panel", true), false);
  assert.equal(applet._targetEnabled("alpha", "baseline", "click"), false);
  assert.equal(applet._elementTargetEnabled("alpha", "unexpected", "hover", true), false);
});

test("duration, numeric and limit-window helpers enforce their documented bounds", () => {
  const applet = makeApplet();

  assert.equal(applet._safeDuration(null), null);
  assert.equal(applet._safeDuration(1), 1);
  assert.equal(applet._safeDuration(315360000), 315360000);
  for (const invalid of [0, -1, 1.5, Infinity, 315360001, "60"]) {
    assert.throws(() => applet._safeDuration(invalid), /invalid limit duration/);
  }
  assert.equal(applet._safeNumber(null), null);
  assert.equal(applet._safeNumber(-12.5), -12.5);
  for (const invalid of [Infinity, -Infinity, NaN, 1000000001, "12"]) {
    assert.throws(() => applet._safeNumber(invalid), /invalid numeric value/);
  }
  assert.equal(applet._limitWindowSeconds("short"), 18000);
  assert.equal(applet._limitWindowSeconds("weekly"), 604800);
  assert.equal(applet._limitWindowSeconds("monthly"), 2592000);
  assert.equal(applet._limitWindowSeconds("spark"), null);
  assert.equal(applet._boundedInteger("2.6", 0, 5, 0), 3);
  assert.equal(applet._boundedInteger(-1, 0, 5, 0), 0);
  assert.equal(applet._boundedInteger(Infinity, 0, 5, 2), 2);
});

test("consumption and forecast formatting helpers preserve their supported placeholders", () => {
  const applet = makeApplet();

  assert.equal(applet._coverageMarker("complete", true), " (vollständig)");
  assert.equal(applet._coverageMarker("partial", true), " (mindestens)");
  assert.equal(applet._coverageMarker("stale", true), " (veraltet)");
  assert.equal(applet._coverageMarker("insufficient", true), " (nicht genügend Messdaten)");
  assert.equal(applet._coverageMarker("future", true), " (unbekannt)");
  assert.equal(applet._coverageMarker("complete", false), "");
  assert.equal(applet._customConsumptionText("{period}|{value}|{window}|{coverage}|{other}", {
    period: "1 h", value: "2,5", window: "5h", coverage: "vollständig",
  }), "1 h|2,5|5h|vollständig|{other}");
  assert.equal(applet._customConsumptionText("", {
    period: "1 h", value: "2,5", window: "5h", coverage: "",
  }), "Δ1 h 2,5%");
  assert.equal(applet._customForecastText("{value}|{duration}|{coverage}|{period}", {
    value: "0,2h", duration: "12 Minuten", coverage: "", period: "ignored",
  }), "0,2h|12 Minuten||{period}");
  assert.equal(applet._customForecastText(null, {
    value: "0,2h", duration: "12 Minuten", coverage: "",
  }), "Zeit bis Tokenende 0,2h");
  assert.equal(applet._formatConsumptionValue(12.56), "12,6");
  assert.equal(applet._formatConsumptionValue(-0.04), "0,0");
  assert.equal(applet._consumptionPeriod(1, "hours"), "1 h");
  assert.equal(applet._consumptionPeriod(2, "days"), "2 Tage");
  assert.equal(applet._consumptionPeriod(3, "unknown"), "3 unknown");
});

test("forecast warning formats apply only their documented Pango attributes", () => {
  const applet = makeApplet();
  const markup = "<b>Rest</b>";

  assert.equal(applet._forecastWarningMarkup(markup, "none"), markup);
  assert.equal(applet._forecastWarningMarkup(markup, "unknown"), markup);
  assert.equal(applet._forecastWarningMarkup(markup, "red"),
    '<span foreground="#ff5555"><b>Rest</b></span>');
  assert.equal(applet._forecastWarningMarkup(markup, "red-yellow"),
    '<span foreground="#ff5555" background="#e5c07b"><b>Rest</b></span>');
  assert.equal(applet._forecastWarningMarkup(markup, "blink-red-yellow"),
    '<span foreground="#ff5555" background="#e5c07b" font_weight="bold"><b>Rest</b></span>');
  assert.equal(applet._forecastWarningMarkup(markup, "yellow"),
    '<span foreground="#e5c07b"><b>Rest</b></span>');
  assert.equal(applet._forecastWarningMarkup(markup, "red-green"),
    '<span foreground="#ff5555" background="#98c379"><b>Rest</b></span>');
  assert.equal(applet._forecastWarningMarkup(markup, "red-red"),
    '<span foreground="#ff5555" background="#a83232"><b>Rest</b></span>');
});

test("text and strict-integer helpers distinguish sanitizing display text from trusted settings", () => {
  const applet = makeApplet();

  assert.equal(applet._safeText(null, 10), "");
  assert.equal(applet._safeText("  a\u0000b\n  ", 10), "a b");
  assert.equal(applet._safeText("abcdef", 3), "abc");
  assert.throws(() => applet._safeText(4, 10), /invalid text value/);
  assert.equal(applet._strictText(null, 10), "");
  assert.equal(applet._strictText("exact", 5), "exact");
  for (const invalid of [" too", "too ", "a\nb", "abcdef", 7]) {
    assert.throws(() => applet._strictText(invalid, 5));
  }
  assert.equal(applet._strictIntegerSetting(3), 3);
  assert.equal(applet._strictIntegerSetting(-2), -2);
  assert.equal(applet._strictIntegerSetting(3.1), null);
  assert.equal(applet._strictIntegerSetting("3"), null);
  assert.equal(applet._shortText(" a\u0000b ", 10), "a b");
  assert.equal(applet._shortText("abcdef", 4), "abc…");
  assert.equal(applet._shortText("abcdef", 0), "…");
});

test("backend provenance and routing identifiers are validated without normalization", () => {
  const applet = makeApplet();
  applet._backendAccounts = {
    direct: {account: "direct", backend: 0, label: "Direct"},
    app: {account: "app", backend: 1, label: "App"},
  };

  assert.equal(applet._safeStatus(" ok "), "ok");
  assert.equal(applet._safeStatus("unknown"), "error");
  assert.equal(applet._safeStatus(null), "error");
  assert.equal(applet._safeBackend(" direct ", false), "direct");
  assert.equal(applet._safeBackend("browser", false), "");
  assert.equal(applet._safeBackend("browser", true), "browser");
  assert.equal(applet._validatedBackend(null, false), "");
  assert.equal(applet._validatedBackend("direct", false), "direct");
  assert.equal(applet._validatedBackend("browser", true), "browser");
  for (const invalid of [" direct", "browser", "unknown", "app\nserver"]) {
    assert.throws(() => applet._validatedBackend(invalid, false), /invalid backend provenance/);
  }
  assert.equal(applet._routingIdentifier("team:alpha+one@example.org"), "team:alpha+one@example.org");
  for (const invalid of [" team", "team space", "team/alpha", "", "x".repeat(129)]) {
    assert.throws(() => applet._routingIdentifier(invalid), /invalid routing policy identifier/);
  }
  assert.equal(applet._backendConfiguredForAccount("direct"), "direct");
  assert.equal(applet._backendConfiguredForAccount("app"), "app-server");
  assert.equal(applet._backendConfiguredForAccount("missing"), "direct");
  const fresh = applet._newBackendUsageRow("app", "app-server");
  assert.equal(fresh.account, "app");
  assert.equal(fresh.label, "App");
  assert.equal(fresh.backend_configured, "app-server");
  assert.equal(fresh.status, "partial");
  assert.equal(fresh.stale, true);
  assert.equal(fresh.five_hour, null);
  assert.equal(fresh.weekly, null);
  assert.equal(fresh.usage_resets.available, null);
  assert.equal(fresh.usage_resets.known, false);
  assert.equal(fresh.usage_resets.redeem_capability, false);
  assert.equal(Object.getPrototypeOf(fresh.models), null);
  assert.equal(Array.isArray(fresh.cost_windows), true);
  assert.equal(fresh.cost_windows.length, 0);
});

test("account setting identities are not normalized", () => {
  const applet = makeApplet();

  assert.equal(applet._configuredAccountId("alpha"), "alpha");
  assert.equal(applet._configuredAccountId(" alpha"), "");
  assert.equal(applet._configuredAccountId("alpha "), "");
  assert.equal(applet._configuredAccountId("alpha\u0000"), "");

  const rows = applet._mergedPanelRows(
    [{ account: "alpha" }],
    [{ account: " alpha", tag: "forged", order: 1, muted: false, slot1: 1, slot2: 0 }]
  );
  assert.equal(Object.prototype.hasOwnProperty.call(rows[0], "tag"), false);
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
  applet._refreshFresh = () => {};
  applet._loadAccountBackends();
  assert.equal(refreshed, 1);
});

test("backend synchronization refreshes after an external backend change", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    label: "Alpha",
    backend_configured: "direct",
    backend_used: "direct",
    status: "ok",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
  }];
  applet._baseCommandArgv = () => ["codex-usage"];
  applet.settings = { setValue() {} };
  applet._syncAccountSettings = () => {};
  applet._syncStyleRows = () => {};
  applet._addIdle = () => {};
  applet._refreshFormattedSurfaces = () => {};
  let freshRefreshes = 0;
  applet._refreshFresh = () => { freshRefreshes += 1; };
  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [{ id: "alpha", label: "Alpha", backend: "app-server" }],
  }, null);

  applet._loadAccountBackends();

  assert.equal(freshRefreshes, 1);
  assert.equal(applet._usages.length, 1);
  assert.equal(applet._usages[0].backend_configured, "app-server");
  assert.equal(applet._usages[0].backend_used, "");
  assert.equal(applet._usages[0].five_hour, null);
  assert.equal(applet._usages[0].stale, true);
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

test("backend synchronization clears values from the previous backend", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    label: "Alpha",
    backend_configured: "direct",
    backend_used: "direct",
    status: "ok",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
  }];
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 1 },
  };

  assert.equal(applet._ensureBackendUsageRows(), true);
  assert.equal(applet._usages.length, 1);
  assert.equal(applet._usages[0].backend_configured, "app-server");
  assert.equal(applet._usages[0].backend_used, "");
  assert.equal(applet._usages[0].five_hour, null);
  assert.equal(applet._usages[0].weekly, null);
  assert.equal(applet._usages[0].stale, true);
});

test("backend synchronization clears unknown values after a backend change", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    label: "Alpha",
    backend_configured: "",
    backend_used: "",
    status: "ok",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 }
  }];
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 0 }
  };
  applet._baseCommandArgv = () => ["codex-usage"];
  applet.settings = { setValue() {} };
  applet._syncAccountSettings = () => {};
  applet._syncStyleRows = () => {};
  applet._addIdle = () => {};
  applet._refreshFormattedSurfaces = () => {};
  applet._refreshFresh = () => {};
  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [{ id: "alpha", label: "Alpha", backend: "app-server" }]
  }, null);

  applet._loadAccountBackends();

  assert.equal(applet._usages.length, 1);
  assert.equal(applet._usages[0].backend_configured, "app-server");
  assert.equal(applet._usages[0].backend_used, "");
  assert.equal(applet._usages[0].five_hour, null);
  assert.equal(applet._usages[0].weekly, null);
  assert.equal(applet._usages[0].stale, true);
});

test("backend synchronization clears values without used provenance", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    label: "Alpha",
    backend_configured: "direct",
    backend_used: "",
    status: "ok",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
  }];
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
  };

  assert.equal(applet._ensureBackendUsageRows(), true);
  assert.equal(applet._usages[0].five_hour, null);
  assert.equal(applet._usages[0].weekly, null);
  assert.equal(applet._usages[0].stale, true);
});

test("backend synchronization clears browser values without configured provenance", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    label: "Alpha",
    backend_configured: "",
    backend_used: "browser",
    status: "partial",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 }
  }];
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 0 }
  };
  applet._baseCommandArgv = () => ["codex-usage"];
  applet.settings = { setValue() {} };
  applet._syncAccountSettings = () => {};
  applet._syncStyleRows = () => {};
  applet._addIdle = () => {};
  applet._refreshFormattedSurfaces = () => {};
  applet._refreshFresh = () => {};
  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [{ id: "alpha", label: "Alpha", backend: "app-server" }]
  }, null);

  applet._loadAccountBackends();

  assert.equal(applet._usages.length, 1);
  assert.equal(applet._usages[0].backend_configured, "app-server");
  assert.equal(applet._usages[0].backend_used, "");
  assert.equal(applet._usages[0].five_hour, null);
  assert.equal(applet._usages[0].weekly, null);
  assert.equal(applet._usages[0].stale, true);
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

test("auxiliary timeout message reports the selected duration", () => {
  const cases = [
    {
      argv: ["codex-usage", "health"],
      timeoutMs: 10000,
      expected: "Hilfsbefehl nach 10 Sekunden abgebrochen",
    },
    {
      argv: ["codex-usage", "health"],
      timeoutMs: 30000,
      expected: "Hilfsbefehl nach 30 Sekunden abgebrochen",
    },
    {
      argv: ["codex-usage", "account", "device-login"],
      timeoutMs: 910000,
      expected: "Device-Login nach 15 Minuten 10 Sekunden abgebrochen",
    },
  ];
  const results = [];

  for (const scenario of cases) {
    let timeout = null;
    let scheduledMs = null;
    let forced = 0;
    const process = { force_exit() { forced += 1; } };
    const applet = makeApplet((runtime) => {
      runtime.timeoutAdd = (milliseconds, callback) => {
        scheduledMs = milliseconds;
        timeout = callback;
        return 17;
      };
      runtime.launcherFactory = () => ({
        setenv() {},
        spawnv() { return process; },
      });
    });
    applet._readBoundedProcessOutput = () => {};
    let error = null;

    applet._spawnAuxJson(
      scenario.argv,
      (_payload, value) => { error = value; },
      false,
      scenario.timeoutMs
    );
    assert.equal(typeof timeout, "function");
    timeout();
    results.push({ scheduledMs, error, forced });
  }

  assert.deepEqual(results, cases.map((scenario) => ({
    scheduledMs: scenario.timeoutMs,
    error: scenario.expected,
    forced: 1,
  })));
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

test("timer scheduling separates display cadence from optional usage polling", () => {
  const noPollingCallbacks = [];
  const noPollingSeconds = [];
  const noPolling = makeApplet((runtime) => {
    runtime.timeoutAddSeconds = (seconds, callback) => {
      noPollingSeconds.push(seconds);
      noPollingCallbacks.push(callback);
      return noPollingCallbacks.length;
    };
  });
  noPolling.autoRefresh = false;
  noPolling.refreshInterval = 1;
  noPolling._refreshFastModeState = () => {};
  noPolling._updatePanel = () => {};
  noPolling._scheduleTimer();
  assert.deepEqual(noPollingSeconds, [60]);
  assert.equal(noPolling._displayTimerId, 1);
  assert.equal(noPolling._timerId, 0);
  assert.equal(noPollingCallbacks[0](), true);

  const pollingSeconds = [];
  const polling = makeApplet((runtime) => {
    runtime.timeoutAddSeconds = (seconds, callback) => {
      pollingSeconds.push(seconds);
      return pollingSeconds.length;
    };
  });
  polling.autoRefresh = true;
  polling.refreshInterval = 90;
  polling._scheduleDisplayTimer = () => true;
  polling._scheduleTimer();
  assert.deepEqual(pollingSeconds, [90]);
  assert.equal(polling._timerId, 1);
  polling._removed = true;
  polling._scheduleTimer();
  assert.equal(polling._timerId, 0);
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

test("reactivation uses browser configured for account", () => {
  const applet = makeApplet();
  applet._backendAccounts = {
    alpha: { account: "alpha", "reactivation-browser": 1 },
  };
  applet._resolveCommand = () => "codex-usage";
  let command = null;
  applet._spawnReactivation = (_usage, argv) => { command = argv; };

  applet._reactivateAccount({ account: "alpha" });

  assert.equal(
    command[command.indexOf("--browser") + 1],
    "vivaldi"
  );
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

test("partial authenticated payload does not restore missing window from stale cache", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { name: "5h", remaining: 80 },
    weekly: { name: "weekly", remaining: 60 },
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    status: "partial",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: { remaining: 70 },
    weekly: null,
    stale: false,
  }]);
  assert.equal(merged[0].five_hour.remaining, 70);
  assert.equal(merged[0].weekly, null);
  assert.equal(merged[0].stale, false);
  assert.equal(merged[0].values_captured_at, undefined);
});


test("browser fresh resetless usage does not restore an older counterpart", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    backend_configured: "direct",
    captured_at: "2026-07-10T10:00:00.000Z",
    backend_used: "browser",
    five_hour: { name: "5h", remaining: 80 },
    weekly: { name: "weekly", remaining: 60 }
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    backend_configured: "direct",
    status: "partial",
    backend_used: "browser",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: { name: "5h", remaining: 70 },
    weekly: null,
    stale: false
  }]);

  assert.equal(merged[0].five_hour.remaining, 70);
  assert.equal(merged[0].weekly, null);
  assert.equal(merged[0].stale, false);
  assert.equal(merged[0].values_captured_at, undefined);
});

test("browser dynamic resetless usage does not restore a cached reset", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "browser",
    captured_at: "2026-07-10T10:00:00.000Z",
    main: {
      key: "main",
      available: true,
      allowed: true,
      limit_reached: false,
      exhausted: false,
      windows: [{
        name: "5h",
        duration_seconds: 18000,
        remaining: 70,
        reset_at: "2026-07-10T15:00:00.000Z"
      }]
    },
    five_hour: null,
    weekly: null
  }];

  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "browser",
    status: "partial",
    captured_at: "2026-07-10T10:05:00.000Z",
    main: {
      key: "main",
      available: true,
      allowed: true,
      limit_reached: false,
      exhausted: false,
      windows: [{
        name: "5h",
        duration_seconds: 18000,
        remaining: 80
      }]
    },
    five_hour: null,
    weekly: null,
    stale: false
  }]);

  assert.equal(merged[0].main.windows[0].remaining, 80);
  assert.equal(merged[0].main.windows[0].reset_at, undefined);
  assert.equal(merged[0].values_captured_at, undefined);
});


test("partial fresh window does not inherit a cached value from another duration", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: {
      name: "5h",
      used: 5,
      limit: 100,
      remaining: 95,
      raw: '{"limit_window_seconds":2592000}'
    }
  }];

  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    status: "partial",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: {
      name: "5h",
      remaining: null,
      reset_at: "2026-07-10T15:00:00.000Z",
      raw: '{"limit_window_seconds":18000}'
    },
    weekly: null,
    stale: false
  }]);

  assert.equal(merged[0].five_hour.remaining, null);
  assert.equal(merged[0].five_hour.reset_at, "2026-07-10T15:00:00.000Z");
  assert.equal(merged[0].stale, false);
});

test("partial authenticated payload does not restore expired resetless cache", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { name: "5h", remaining: 80 },
    weekly: { name: "weekly", remaining: 60 }
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    status: "partial",
    captured_at: "2026-07-10T16:00:00.000Z",
    five_hour: null,
    weekly: null,
    stale: false
  }]);

  assert.equal(merged[0].five_hour, null);
  assert.equal(merged[0].weekly, null);
  assert.equal(merged[0].stale, false);
});

test("partial authenticated payload drops an old inferred five hour value", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    backend_configured: "direct",
    backend_used: "direct",
    five_hour: {
      name: "5h",
      used: 0,
      limit: 100,
      remaining: 100,
      percent: 100,
      source: "inferred:inactive-five-hour:direct"
    },
    weekly: { name: "weekly", remaining: 60 }
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    status: "partial",
    captured_at: "2026-07-10T16:00:00.000Z",
    backend_configured: "direct",
    backend_used: "direct",
    five_hour: null,
    weekly: null,
    stale: false
  }]);

  assert.equal(merged[0].five_hour, null);
  assert.equal(merged[0].weekly, null);
  assert.equal(merged[0].stale, false);
});

test("legacy inferred inactive five hour reset is still allowed to expire", () => {
  const applet = makeApplet();
  assert.equal(
    applet._windowCacheExpired(
      {
        name: "5h",
        remaining: 100,
        reset_at: "2026-07-10T11:00:00.000Z",
        source: "inferred:inactive-five-hour:direct"
      },
      "2026-07-10T10:00:00.000Z",
      "2026-07-10T12:00:00.000Z"
    ),
    true
  );
});

test("cache provenance helpers recognize only the two trusted inactive-five-hour sources", () => {
  const applet = makeApplet();

  assert.equal(applet._isInferredInactiveFiveHour({
    source: "inferred:inactive-five-hour:direct"
  }), true);
  assert.equal(applet._isInferredInactiveFiveHour({
    source: "inferred:inactive-five-hour:app-server"
  }), true);
  assert.equal(applet._isInferredInactiveFiveHour({
    source: "inferred:inactive-five-hour:browser"
  }), false);
  assert.equal(applet._isInferredInactiveFiveHour(null), false);
});

test("values capture timestamp is used only when it is valid and not newer than capture", () => {
  const applet = makeApplet();

  assert.equal(
    applet._valuesCaptureForExpiry({
      captured_at: "2026-07-10T10:05:00.000Z",
      values_captured_at: "2026-07-10T10:00:00.000Z"
    }),
    "2026-07-10T10:00:00.000Z"
  );
  assert.equal(
    applet._valuesCaptureForExpiry({
      captured_at: "2026-07-10T10:05:00.000Z",
      values_captured_at: "2026-07-10T10:06:00.000Z"
    }),
    "2026-07-10T10:05:00.000Z"
  );
  assert.equal(
    applet._valuesCaptureForExpiry({ captured_at: "invalid-capture", values_captured_at: "2026-07-10T10:00:00.000Z" }),
    "invalid-capture"
  );
});

test("cache merge preserves a valid cached value but adopts a fresh reset", () => {
  const applet = makeApplet();
  const cached = {
    name: "5h",
    duration_seconds: 18000,
    remaining: 70,
    reset_at: "2026-07-10T15:00:00.000Z",
    source: "cache"
  };
  const fresh = {
    name: "5h",
    duration_seconds: 18000,
    remaining: 80,
    reset_at: "2026-07-10T16:00:00.000Z",
    source: "direct"
  };

  const merged = applet._mergeCachedWindow(
    fresh,
    cached,
    "2026-07-10T10:05:00.000Z",
    "2026-07-10T10:00:00.000Z",
    "five_hour"
  );

  assert.equal(merged.remaining, 70);
  assert.equal(merged.source, "cache");
  assert.equal(merged.reset_at, "2026-07-10T16:00:00.000Z");
  assert.equal(cached.reset_at, "2026-07-10T15:00:00.000Z");
});

test("cache merge returns fresh data for expired or mismatched cached windows", () => {
  const applet = makeApplet();
  const fresh = {
    name: "5h",
    duration_seconds: 18000,
    remaining: 80,
    reset_at: "2026-07-10T16:00:00.000Z"
  };
  const expired = {
    name: "5h",
    duration_seconds: 18000,
    remaining: 70,
    reset_at: "2026-07-10T09:00:00.000Z"
  };
  const weekly = {
    name: "weekly",
    duration_seconds: 604800,
    remaining: 70,
    reset_at: "2026-07-16T10:00:00.000Z"
  };

  assert.equal(applet._mergeCachedWindow(
    fresh,
    expired,
    "2026-07-10T10:05:00.000Z",
    "2026-07-10T10:00:00.000Z",
    "five_hour"
  ), fresh);
  assert.equal(applet._mergeCachedWindow(
    fresh,
    weekly,
    "2026-07-10T10:05:00.000Z",
    "2026-07-10T10:00:00.000Z",
    "five_hour"
  ), fresh);
});

test("missing reset merge fills only a valid same-window fresh value", () => {
  const applet = makeApplet();
  const fresh = {
    name: "weekly",
    duration_seconds: 604800,
    remaining: 80
  };
  const cached = {
    name: "weekly",
    duration_seconds: 604800,
    remaining: 70,
    reset_at: "2026-07-16T10:00:00.000Z"
  };

  const merged = applet._mergeMissingReset(
    fresh,
    cached,
    "2026-07-10T10:05:00.000Z",
    "2026-07-10T10:00:00.000Z",
    "weekly"
  );

  assert.notEqual(merged, fresh);
  assert.equal(merged.remaining, 80);
  assert.equal(merged.reset_at, "2026-07-16T10:00:00.000Z");
  assert.equal(fresh.reset_at, undefined);
});

test("fresh inferred inactive five hour value does not inherit an old reset", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: {
      name: "5h",
      remaining: 80,
      reset_at: "2026-07-10T12:00:00.000Z"
    },
    weekly: null
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    status: "partial",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: {
      name: "5h",
      used: 0,
      limit: 100,
      remaining: 100,
      percent: 100,
      source: "inferred:inactive-five-hour:direct"
    },
    weekly: null,
    stale: false
  }]);

  assert.equal(merged[0].five_hour.reset_at, undefined);
  assert.equal(merged[0].five_hour.remaining, 100);
  assert.equal(merged[0].five_hour.source, "inferred:inactive-five-hour:direct");
  assert.equal(merged[0].stale, false);
});

test("invalid cached reset timestamps do not preserve old usage", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { name: "5h", remaining: 80, reset_at: "invalid-reset" },
    weekly: null,
  }];

  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    status: "partial",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: null,
    weekly: null,
    stale: false,
  }]);

  assert.equal(merged[0].five_hour, null);
  assert.equal(merged[0].stale, false);
});

test("reset timestamps beyond the window duration do not preserve old usage", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { name: "5h", remaining: 80, reset_at: "2026-07-10T20:00:00.000Z" },
    weekly: null,
  }];

  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    status: "partial",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: null,
    weekly: null,
    stale: false,
  }]);

  assert.equal(merged[0].five_hour, null);
  assert.equal(merged[0].stale, false);
});

test("future reset with invalid capture does not preserve old usage", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "invalid-capture",
    five_hour: {
      name: "5h",
      remaining: 80,
      reset_at: "2026-07-10T20:00:00.000Z"
    },
    weekly: null
  }];

  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    status: "partial",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: null,
    weekly: null,
    stale: false
  }]);

  assert.equal(merged[0].five_hour, null);
  assert.equal(merged[0].stale, false);
});

test("fresh merge expires resetless cached values from their values capture", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    backend_used: "browser",
    captured_at: "2026-07-10T15:00:00.000Z",
    values_captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { name: "5h", remaining: 80 },
    weekly: null
  }];

  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    status: "partial",
    backend_used: "browser",
    captured_at: "2026-07-10T16:00:00.000Z",
    five_hour: null,
    weekly: null,
    stale: false
  }]);

  assert.equal(merged[0].five_hour, null);
  assert.equal(merged[0].stale, false);
});

test("partial fresh payload rejects an unclassified cached window", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { name: "", remaining: 80 },
    weekly: null
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    status: "partial",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: null,
    weekly: null,
    stale: false
  }]);

  assert.equal(merged[0].five_hour, null);
  assert.equal(merged[0].stale, false);
});

test("payload validation keeps window provenance metadata for safe merges", () => {
  const applet = makeApplet();
  const validated = applet._validatePayload([{
    account: "alpha",
    status: "partial",
    five_hour: {
      name: "5h",
      raw: '{"limit_window_seconds":18000}',
      source: "json:usage"
    }
  }]);

  assert.equal(validated[0].five_hour.name, "5h");
  assert.equal(validated[0].five_hour.raw, '{"limit_window_seconds":18000}');
  assert.equal(validated[0].five_hour.source, "json:usage");
});

test("partial authenticated payload does not restore a missing window", () => {
  for (const backend of ["direct", "app-server"]) {
    const applet = makeApplet();
    applet._usages = [{
      account: "alpha",
      captured_at: "2026-07-10T10:00:00.000Z",
      backend_user_id: "user-alpha",
      backend_account_id: "account-alpha",
      backend_used: backend,
      five_hour: { remaining: 80 },
      weekly: { remaining: 60 },
      status: "ok",
      stale: false,
    }];
    const merged = applet._mergeFreshPayload([{
      account: "alpha",
      status: "partial",
      captured_at: "2026-07-10T10:05:00.000Z",
      backend_user_id: "user-alpha",
      backend_account_id: "account-alpha",
      backend_used: backend,
      five_hour: { remaining: 70 },
      weekly: null,
      stale: false,
    }]);
    assert.equal(merged[0].five_hour.remaining, 70);
    assert.equal(merged[0].weekly, null);
    assert.equal(merged[0].stale, false);
  }
});

test("expired cached windows are not reused after a fresh capture", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { remaining: 80, reset_at: "2026-07-10T09:59:00.000Z" },
    weekly: { remaining: 60, reset_at: "2026-07-10T09:59:00.000Z" },
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    status: "partial",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: null,
    weekly: null,
    stale: false,
  }]);

  assert.equal(merged[0].five_hour, null);
  assert.equal(merged[0].weekly, null);
  assert.equal(merged[0].captured_at, "2026-07-10T10:05:00.000Z");
  assert.equal(merged[0].values_captured_at, undefined);
  assert.equal(merged[0].stale, false);
});

test("fresh data from another backend account cannot reuse cached windows", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    backend_user_id: "user-shared",
    backend_account_id: "account-old",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    status: "partial",
    captured_at: "2026-07-10T10:05:00.000Z",
    backend_user_id: "user-shared",
    backend_account_id: "account-new",
    five_hour: null,
    weekly: null,
    stale: false,
  }]);

  assert.equal(merged[0].five_hour, null);
  assert.equal(merged[0].weekly, null);
  assert.equal(merged[0].stale, false);
});

test("fresh data from another backend cannot reuse cached windows", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    backend_configured: "direct",
    backend_used: "browser",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    status: "partial",
    captured_at: "2026-07-10T10:05:00.000Z",
    backend_configured: "direct",
    backend_used: "direct",
    five_hour: { remaining: 70 },
    weekly: null,
    stale: false,
  }]);

  assert.equal(merged[0].backend_used, "direct");
  assert.equal(merged[0].five_hour.remaining, 70);
  assert.equal(merged[0].weekly, null);
  assert.equal(merged[0].stale, false);
});

test("cached data from another backend does not replace a newer in-memory value", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:10:00.000Z",
    backend_configured: "direct",
    backend_used: "direct",
    five_hour: { remaining: 70 },
    weekly: { remaining: 50 },
  }];
  const merged = applet._mergeCachedPayload([{
    account: "alpha",
    status: "ok",
    captured_at: "2026-07-10T10:05:00.000Z",
    backend_configured: "direct",
    backend_used: "browser",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
    stale: false,
  }]);

  assert.equal(merged[0].backend_used, "direct");
  assert.equal(merged[0].five_hour.remaining, 70);
  assert.equal(merged[0].weekly.remaining, 50);
});

test("newer cached data from another backend cannot replace in-memory usage", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    backend_configured: "direct",
    backend_used: "direct",
    five_hour: { remaining: 70 },
    weekly: { remaining: 50 },
    status: "ok",
    stale: false,
  }];

  const merged = applet._mergeCachedPayload([{
    account: "alpha",
    status: "ok",
    captured_at: "2026-07-10T10:05:00.000Z",
    backend_configured: "direct",
    backend_used: "browser",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
    stale: false,
  }]);

  assert.equal(merged[0].backend_used, "direct");
  assert.equal(merged[0].five_hour.remaining, 70);
  assert.equal(merged[0].weekly.remaining, 50);
  assert.equal(merged[0].stale, true);
});

test("arbitrary fallback text cannot authorize cross-backend provenance", () => {
  const applet = makeApplet();
  const direct = {
    backend_configured: "app-server",
    backend_used: "direct",
    fallback_reason: "forged fallback",
  };
  const appServer = {
    backend_configured: "app-server",
    backend_used: "app-server",
    fallback_reason: null,
  };

  assert.equal(applet._backendProvenanceMatches(direct, appServer), false);
  assert.equal(
    applet._backendProvenanceMatches(
      {
        ...direct,
        fallback_reason: "app-server unavailable: installed Codex does not support rate-limit RPC",
      },
      appServer,
    ),
    true,
  );
  assert.equal(
    applet._backendProvenanceMatches(
      { ...direct, fallback_reason: "app-server unavailable: timeout" },
      appServer,
    ),
    false,
  );
});

test("overlong backend identity cannot be truncated into a valid value", () => {
  const applet = makeApplet();
  assert.throws(
    () => applet._validatePayload([{
      account: "alpha",
      status: "ok",
      captured_at: "2026-07-10T10:00:00.000Z",
      backend_configured: "direct",
      backend_used: "direct",
      backend_account_id: "account-alpha" + "x".repeat(300),
      main: {
        key: "main",
        available: true,
        allowed: null,
        limit_reached: null,
        exhausted: false,
        availability_sources: ["usage"],
        windows: [{ name: "weekly", remaining: 80 }],
      },
    }]),
    /text value exceeds strict limit/,
  );
});

test("mismatched cached data can fill an empty backend placeholder", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "",
    backend_configured: "direct",
    backend_used: "",
    five_hour: null,
    weekly: null,
    status: "partial",
    stale: true,
  }];

  const merged = applet._mergeCachedPayload([{
    account: "alpha",
    status: "partial",
    captured_at: "2026-07-10T10:05:00.000Z",
    backend_configured: "direct",
    backend_used: "browser",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
    stale: false,
  }]);

  assert.equal(merged[0].backend_used, "browser");
  assert.equal(merged[0].five_hour.remaining, 80);
  assert.equal(merged[0].weekly.remaining, 60);
  assert.equal(merged[0].stale, false);
});

test("configured authenticated cache replaces a stale browser snapshot", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
  };
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    backend_configured: "direct",
    backend_used: "browser",
    backend_user_id: "user-alpha",
    backend_account_id: "account-alpha",
    five_hour: { remaining: 70 },
    weekly: { remaining: 50 },
    status: "ok",
    stale: false,
  }];

  const merged = applet._mergeCachedPayload([{
    account: "alpha",
    captured_at: "2026-07-10T10:05:00.000Z",
    backend_configured: "direct",
    backend_used: "direct",
    backend_user_id: "user-alpha",
    backend_account_id: "account-alpha",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
    status: "ok",
    stale: false,
  }]);

  assert.equal(merged[0].backend_used, "direct");
  assert.equal(merged[0].five_hour.remaining, 80);
  assert.equal(merged[0].weekly.remaining, 60);
  assert.equal(merged[0].stale, false);
});

test("configured cache accepts a more complete matching identity", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
  };
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    backend_configured: "direct",
    backend_used: "browser",
    backend_account_id: "account-alpha",
    five_hour: { remaining: 70 },
    weekly: { remaining: 50 },
    status: "ok",
    stale: false,
  }];

  const merged = applet._mergeCachedPayload([{
    account: "alpha",
    captured_at: "2026-07-10T10:05:00.000Z",
    backend_configured: "direct",
    backend_used: "direct",
    backend_user_id: "user-alpha",
    backend_account_id: "account-alpha",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
    status: "ok",
    stale: false,
  }]);

  assert.equal(merged[0].backend_used, "direct");
  assert.equal(merged[0].backend_user_id, "user-alpha");
  assert.equal(merged[0].five_hour.remaining, 80);
  assert.equal(merged[0].weekly.remaining, 60);
});

test("configured authenticated cache replaces a browser snapshot from another identity", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
  };
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    backend_configured: "direct",
    backend_used: "browser",
    backend_user_id: "user-old",
    backend_account_id: "account-old",
    five_hour: { remaining: 70 },
    weekly: { remaining: 50 },
    status: "ok",
    stale: false,
  }];

  const merged = applet._mergeCachedPayload([{
    account: "alpha",
    captured_at: "2026-07-10T10:05:00.000Z",
    backend_configured: "direct",
    backend_used: "direct",
    backend_user_id: "user-new",
    backend_account_id: "account-new",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
    status: "ok",
    stale: false,
  }]);

  assert.equal(merged[0].backend_used, "direct");
  assert.equal(merged[0].backend_account_id, "account-new");
  assert.equal(merged[0].five_hour.remaining, 80);
  assert.equal(merged[0].weekly.remaining, 60);
  assert.equal(merged[0].stale, false);
});

test("identity-less fresh and cached payloads cannot replace identified values", () => {
  for (const mergeName of ["_mergeFreshPayload", "_mergeCachedPayload"]) {
    const applet = makeApplet();
    applet._usages = [{
      account: "alpha",
      captured_at: "2026-07-10T10:00:00.000Z",
      backend_user_id: "user-alpha",
      backend_account_id: "account-alpha",
      five_hour: { remaining: 80 },
      weekly: { remaining: 60 },
      status: "ok",
      stale: false,
    }];
    const merged = applet[mergeName]([{
      account: "alpha",
      captured_at: "2026-07-10T10:05:00.000Z",
      five_hour: { remaining: 10 },
      weekly: { remaining: 20 },
      status: "ok",
      stale: false,
    }]);

    assert.equal(merged[0].backend_account_id, "account-alpha");
    assert.equal(merged[0].five_hour.remaining, 80);
    assert.equal(merged[0].weekly.remaining, 60);
    assert.equal(merged[0].captured_at, "2026-07-10T10:00:00.000Z");
    assert.equal(merged[0].status, "partial");
    assert.equal(merged[0].stale, true);
    assert.equal(merged[0].values_captured_at, "2026-07-10T10:00:00.000Z");
  }
});

test("a matching partial identity cannot replace a complete cached identity", () => {
  for (const mergeName of ["_mergeFreshPayload", "_mergeCachedPayload"]) {
    const applet = makeApplet();
    applet._usages = [{
      account: "alpha",
      captured_at: "2026-07-10T10:00:00.000Z",
      backend_user_id: "user-alpha",
      backend_account_id: "account-alpha",
      five_hour: { remaining: 80 },
      weekly: { remaining: 60 },
      status: "ok",
      stale: false,
    }];
    const merged = applet[mergeName]([{
      account: "alpha",
      captured_at: "2026-07-10T10:05:00.000Z",
      backend_user_id: "user-alpha",
      five_hour: { remaining: 10 },
      weekly: { remaining: 20 },
      status: "ok",
      stale: false,
    }]);

    assert.equal(merged[0].backend_account_id, "account-alpha");
    assert.equal(merged[0].five_hour.remaining, 80);
    assert.equal(merged[0].weekly.remaining, 60);
    assert.equal(merged[0].status, "partial");
    assert.equal(merged[0].stale, true);
  }
});

test("same backend account id remains authoritative when user id is omitted", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    backend_user_id: "user-alpha",
    backend_account_id: "account-alpha",
    backend_used: "direct",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
    status: "ok",
    stale: false,
  }];

  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    captured_at: "2026-07-10T10:05:00.000Z",
    backend_account_id: "account-alpha",
    backend_used: "direct",
    five_hour: { remaining: 90 },
    weekly: { remaining: 70 },
    status: "ok",
    stale: false,
  }]);

  assert.equal(merged[0].backend_account_id, "account-alpha");
  assert.equal(merged[0].five_hour.remaining, 90);
  assert.equal(merged[0].weekly.remaining, 70);
  assert.equal(merged[0].stale, false);
});

test("configured authenticated cache replaces a browser user-only identity", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
  };
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    backend_configured: "direct",
    backend_used: "browser",
    backend_user_id: "shared-user",
    backend_account_id: "",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
    status: "ok",
    stale: false,
  }];

  const merged = applet._mergeCachedPayload([{
    account: "alpha",
    captured_at: "2026-07-10T10:05:00.000Z",
    backend_configured: "direct",
    backend_used: "direct",
    backend_user_id: "shared-user",
    backend_account_id: "account-new",
    five_hour: { remaining: 90 },
    weekly: { remaining: 70 },
    status: "ok",
    stale: false,
  }]);

  assert.equal(merged[0].backend_used, "direct");
  assert.equal(merged[0].backend_account_id, "account-new");
  assert.equal(merged[0].five_hour.remaining, 90);
  assert.equal(merged[0].weekly.remaining, 70);
  assert.equal(merged[0].stale, false);
});

test("authenticated empty cache clears foreign browser windows", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
  };
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    backend_configured: "direct",
    backend_used: "browser",
    backend_user_id: "user-old",
    backend_account_id: "account-old",
    five_hour: { remaining: 70 },
    weekly: { remaining: 50 },
    status: "ok",
    stale: false,
  }];

  const merged = applet._mergeCachedPayload([{
    account: "alpha",
    captured_at: "2026-07-10T10:05:00.000Z",
    backend_configured: "direct",
    backend_used: "direct",
    backend_user_id: "user-new",
    backend_account_id: "account-new",
    five_hour: null,
    weekly: null,
    status: "partial",
    stale: false,
  }]);

  assert.equal(merged[0].backend_used, "direct");
  assert.equal(merged[0].backend_account_id, "account-new");
  assert.equal(merged[0].five_hour, null);
  assert.equal(merged[0].weekly, null);
  assert.equal(merged[0].stale, false);
});

test("identity changes win over an older capture timestamp", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:10:00.000Z",
    backend_user_id: "user-shared",
    backend_account_id: "account-old",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    captured_at: "2026-07-10T10:05:00.000Z",
    backend_user_id: "user-shared",
    backend_account_id: "account-new",
    five_hour: { remaining: 95 },
    weekly: { remaining: 90 },
    status: "ok",
    stale: false,
  }]);

  assert.equal(merged[0].backend_account_id, "account-new");
  assert.equal(merged[0].five_hour.remaining, 95);
  assert.equal(merged[0].weekly.remaining, 90);
});

test("partial authenticated payload does not restore usage under reset-only windows", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { name: "5h", remaining: 80, reset_at: "2026-07-10T15:00:00.000Z" },
    weekly: { name: "weekly", remaining: 60, reset_at: "2026-07-11T15:00:00.000Z" },
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    status: "partial",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: { name: "5h", reset_at: "2026-07-10T16:00:00.000Z" },
    weekly: null,
    stale: false,
  }]);
  assert.equal(merged[0].five_hour.remaining, undefined);
  assert.equal(merged[0].five_hour.reset_at, "2026-07-10T16:00:00.000Z");
  assert.equal(merged[0].weekly, null);
  assert.equal(merged[0].stale, false);
  assert.equal(merged[0].values_captured_at, undefined);
});

test("expired reset-only fresh window does not restore old usage", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T15:00:00.000Z",
    five_hour: { remaining: 80, reset_at: "2026-07-10T17:00:00.000Z" },
    weekly: null,
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    status: "partial",
    captured_at: "2026-07-10T16:00:00.000Z",
    five_hour: { remaining: null, reset_at: "2026-07-10T15:30:00.000Z" },
    weekly: null,
    stale: false,
  }]);

  assert.equal(merged[0].five_hour.remaining, null);
  assert.equal(merged[0].five_hour.reset_at, "2026-07-10T15:30:00.000Z");
  assert.equal(merged[0].stale, false);
  assert.equal(merged[0].values_captured_at, undefined);
});

test("authoritative empty direct limits clear cached windows", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { remaining: 80 },
    weekly: { remaining: 60 },
    backend_used: "direct",
    status: "ok",
    stale: false,
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    status: "partial",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: null,
    weekly: null,
    backend_used: "direct",
    stale: false,
  }]);

  assert.equal(merged[0].five_hour, null);
  assert.equal(merged[0].weekly, null);
  assert.equal(merged[0].captured_at, "2026-07-10T10:05:00.000Z");
  assert.equal(merged[0].stale, false);
});

test("authoritative authenticated errors clear cached windows", () => {
  for (const mergeName of ["_mergeFreshPayload", "_mergeCachedPayload"]) {
    const applet = makeApplet();
    applet._usages = [{
      account: "alpha",
      captured_at: "2026-07-10T10:00:00.000Z",
      five_hour: { remaining: 80 },
      weekly: { remaining: 60 },
      backend_used: "direct",
      backend_user_id: "user-alpha",
      backend_account_id: "account-alpha",
      status: "ok",
      stale: false,
    }];
    const merged = applet[mergeName]([{
      account: "alpha",
      status: "error",
      error: "backend response has ambiguous account identity",
      captured_at: "2026-07-10T10:05:00.000Z",
      five_hour: null,
      weekly: null,
      backend_used: "direct",
      backend_user_id: "user-alpha",
      backend_account_id: "account-alpha",
      cache_invalidated: true,
      stale: false,
    }]);

    assert.equal(merged[0].five_hour, null);
    assert.equal(merged[0].weekly, null);
    assert.equal(merged[0].status, "error");
    assert.equal(merged[0].captured_at, "2026-07-10T10:05:00.000Z");
  }
});

test("transient authenticated errors preserve cached windows", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    backend_configured: "direct",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: {
      name: "5h",
      remaining: 80,
      reset_at: "2026-07-10T15:00:00.000Z",
    },
    weekly: {
      name: "weekly",
      remaining: 60,
      reset_at: "2026-07-17T10:00:00.000Z",
    },
    backend_used: "direct",
    backend_user_id: "user-alpha",
    backend_account_id: "account-alpha",
    status: "ok",
    stale: false,
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    backend_configured: "direct",
    status: "error",
    error: "direct fetch failed: network error",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: null,
    weekly: null,
    backend_used: "direct",
    backend_user_id: "user-alpha",
    backend_account_id: "account-alpha",
    stale: false,
  }]);

  assert.equal(merged[0].five_hour.remaining, 80);
  assert.equal(merged[0].weekly.remaining, 60);
  assert.equal(merged[0].stale, true);
});

test("fresh successful payload preserves cached reset under missing reset times", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    captured_at: "2026-07-10T10:00:00.000Z",
    five_hour: { name: "5h", remaining: 80, reset_at: "2026-07-10T15:00:00.000Z" },
    weekly: { name: "weekly", remaining: 60, reset_at: "2026-07-11T15:00:00.000Z" },
  }];
  const merged = applet._mergeFreshPayload([{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    status: "ok",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: { name: "5h", remaining: 70 },
    weekly: { name: "weekly", remaining: 50, reset_at: "2026-07-11T16:00:00.000Z" },
    stale: false,
  }]);
  assert.equal(merged[0].five_hour.remaining, 70);
  assert.equal(merged[0].five_hour.reset_at, "2026-07-10T15:00:00.000Z");
  assert.equal(merged[0].weekly.remaining, 50);
  assert.equal(merged[0].weekly.reset_at, "2026-07-11T16:00:00.000Z");
  assert.equal(merged[0].stale, true);
  assert.equal(merged[0].values_captured_at, "2026-07-10T10:00:00.000Z");
  assert.equal(merged[0].captured_at, "2026-07-10T10:05:00.000Z");
});

test("older or invalid fresh captures cannot regress newer cached usage", () => {
  const applet = makeApplet();
  applet._usages = [{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    captured_at: "2026-07-10T10:10:00.000Z",
    five_hour: { remaining: 80, reset_at: "2026-07-10T15:00:00.000Z" },
    weekly: { remaining: 60, reset_at: "2026-07-11T15:00:00.000Z" },
    status: "ok",
    stale: false,
  }];
  const older = applet._mergeFreshPayload([{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    captured_at: "2026-07-10T10:05:00.000Z",
    five_hour: { remaining: 20, reset_at: "2026-07-10T14:00:00.000Z" },
    weekly: { remaining: 30, reset_at: "2026-07-11T14:00:00.000Z" },
    status: "ok",
    stale: false,
  }]);
  assert.equal(older[0].captured_at, "2026-07-10T10:10:00.000Z");
  assert.equal(older[0].five_hour.remaining, 80);
  assert.equal(older[0].weekly.remaining, 60);

  const invalid = applet._mergeFreshPayload([{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "direct",
    captured_at: "not-a-timestamp",
    five_hour: { remaining: 10 },
    weekly: { remaining: 15 },
    status: "ok",
    stale: false,
  }]);
  assert.equal(invalid[0].captured_at, "2026-07-10T10:10:00.000Z");
  assert.equal(invalid[0].five_hour.remaining, 80);
  assert.equal(invalid[0].weekly.remaining, 60);
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
    () => applet._validatePayload([{ account: "alpha/other" }]),
    /account id missing/
  );
  assert.throws(
    () => applet._validatePayload([{ account: "constructor" }, { account: "constructor" }]),
    /duplicate account id/
  );
});

test("payload validation rejects unknown backend provenance", () => {
  const applet = makeApplet();
  for (const provenance of [
    { backend_configured: "browser" },
    { backend_used: "mystery" },
    { backend_configured: " direct" },
    { backend_used: "app-server " },
  ]) {
    assert.throws(
      () => applet._validatePayload([{ account: "alpha", ...provenance }]),
      /invalid backend provenance/
    );
  }
});

test("payload usage without complete backend provenance fails closed", () => {
  for (const provenance of [
    { backend_used: "direct" },
    { backend_configured: "direct" },
    {},
  ]) {
    const applet = makeApplet();
    const [usage] = applet._validatePayload([{
      account: "alpha",
      ...provenance,
      five_hour: { name: "5h", remaining: 80 },
      weekly: { name: "weekly", remaining: 60 },
      status: "ok",
    }]);

    assert.equal(usage.status, "error");
    assert.equal(usage.error, "backend provenance missing");
    assert.equal(usage.cache_invalidated, true);
    assert.equal(usage.five_hour, null);
    assert.equal(usage.weekly, null);
  }

  const applet = makeApplet();
  const [usage] = applet._validatePayload([{
    account: "alpha",
    backend_configured: "direct",
    backend_used: "",
    models: {
      "gpt-5.3-codex-spark": {
        key: "gpt-5.3-codex-spark",
        windows: [{ name: "weekly", duration_seconds: 604800, remaining: 80 }],
        available: true,
        allowed: true,
        limit_reached: false,
        exhausted: false,
        availability_sources: ["rate_limits"],
      },
    },
    status: "partial",
  }]);

  assert.equal(usage.status, "error");
  assert.equal(usage.error, "backend provenance missing");
  assert.deepEqual(Object.keys(usage.models), []);
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

test("backend synchronization requests configuration without a live usage poll", () => {
  const applet = makeApplet();
  applet._baseCommandArgv = () => ["codex-usage"];
  applet.settings = { setValue() {} };
  applet._syncAccountSettings = () => {};
  applet._syncStyleRows = () => {};
  applet._addIdle = () => {};
  applet._refreshFormattedSurfaces = () => {};
  const commands = [];
  applet._spawnAuxJson = (value, callback) => {
    commands.push(value);
    if (value[1] === "account") {
      callback({ accounts: [{ id: "alpha", label: "Alpha", backend: "direct" }] }, null);
    }
  };

  applet._loadAccountBackends();

  assert.deepEqual(commands[0], [
    "codex-usage",
    "account",
    "overview",
    "--format",
    "json",
    "--config-only",
  ]);
  assert.deepEqual(commands[1], [
    "codex-usage",
    "policy",
    "status",
    "--role",
    "arbeitsbiene",
    "--format",
    "json",
  ]);
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

test("backend overview rejects relative account paths without throwing", () => {
  const applet = makeApplet();
  applet._backendAccounts = { alpha: { account: "alpha", label: "Alpha", backend: 0 } };
  applet._backendRowsReady = true;
  applet.accountBackends = [{ account: "alpha", label: "Alpha", backend: 0 }];
  applet._baseCommandArgv = () => ["codex-usage"];
  applet.settings = { setValue() { throw new Error("must not write"); } };
  applet._syncAccountSettings = () => { throw new Error("must not sync"); };
  applet._syncStyleRows = () => { throw new Error("must not sync"); };
  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [{
      id: "alpha",
      label: "Alpha",
      profile_dir: "relative/profile",
      backend: "direct",
    }],
  }, null);

  assert.doesNotThrow(() => applet._loadAccountBackends());
  assert.deepEqual(applet._backendAccounts, {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
  });
});

test("backend overview rejects normalized account and backend identities", () => {
  const applet = makeApplet();
  applet._backendAccounts = { alpha: { account: "alpha", label: "Alpha", backend: 0 } };
  applet._backendRowsReady = true;
  applet.accountBackends = [{ account: "alpha", label: "Alpha", backend: 0 }];
  applet._baseCommandArgv = () => ["codex-usage"];
  applet.settings = { setValue() { throw new Error("must not write"); } };
  applet._syncAccountSettings = () => { throw new Error("must not sync"); };
  applet._syncStyleRows = () => { throw new Error("must not sync"); };
  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [{ id: " alpha", label: "Alpha", backend: "direct" }],
  }, null);

  assert.doesNotThrow(() => applet._loadAccountBackends());
  assert.deepEqual(applet._backendAccounts, {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
  });

  applet._spawnAuxJson = (_argv, callback) => callback({
    accounts: [{ id: "alpha", label: "Alpha", backend: " direct" }],
  }, null);
  assert.doesNotThrow(() => applet._loadAccountBackends());
  assert.deepEqual(applet._backendAccounts, {
    alpha: { account: "alpha", label: "Alpha", backend: 0 },
  });
});

test("backend setting rejects normalized account identity", () => {
  const applet = makeApplet();
  applet._backendRowsReady = true;
  applet._backendAccounts = { alpha: { account: "alpha", label: "Alpha", backend: 0 } };
  applet.accountBackends = [{ account: " alpha", backend: 0 }];
  let reloads = 0;
  applet._loadAccountBackends = () => { reloads += 1; };

  applet._onAccountBackendsChanged();

  assert.equal(reloads, 1);
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
  const commands = [];
  applet._spawnAuxJson = (argv, callback) => {
    calls.push(argv[3]);
    commands.push(argv);
    callback({ ok: true, account: argv[3] }, null);
  };
  applet._refreshFresh = () => {};
  let reloads = 0;
  applet._loadAccountBackends = () => { reloads += 1; };
  applet._onAccountBackendsChanged();
  assert.deepEqual(calls, ["alpha", "beta"]);
  assert.deepEqual(commands, [
    ["codex-usage", "account", "backend", "alpha", "app-server", "--format", "json"],
    ["codex-usage", "account", "backend", "beta", "app-server", "--format", "json"],
  ]);
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
      () => {},
      false,
      10000
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
  applet._spawnAuxJson(
    ["codex-usage", "health", "0"],
    latestCallback,
    false,
    910000
  );
  assert.equal(applet._backendAuxQueue.length, 8);
  assert.equal(applet._backendAuxQueue[0].callback, latestCallback);
  assert.equal(applet._backendAuxQueue[0].timeoutMs, 910000);
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
  assert.equal(rows.length, 30);
  assert.equal(rows[3].element, 3);
  assert.equal(rows[3].click, true);
  assert.equal(rows[3].panel, false);
  assert.equal(rows[4].element, 4);
  assert.equal(rows[4].panel, false);
  assert.equal(rows[4].hover, true);
  assert.equal(rows[6].element, 6);
  assert.equal(rows[7].element, 7);
  assert.equal(rows[9].element, 9);
  assert.equal(rows[10].element, 10);
  assert.equal(rows[11].element, 11);
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
      callback({ installed: true, enabled: false, active: false, service_result: "success" }, null);
      return;
    }
    callback({ installed: true, enabled: true, active: true, service_result: "success" }, null);
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

test("service status errors hand stale automatic polling back to the applet", () => {
  const applet = makeApplet();
  applet.pollOwner = "auto";
  applet.autoRefresh = true;
  applet._serviceChecked = true;
  applet._systemdActive = true;
  applet._serviceAutoAttempted = true;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._scheduleTimer = () => {};
  applet._cacheIsStale = () => true;
  applet._spawnAuxJson = (_argv, callback) => callback(null, "status unavailable");

  applet._checkServiceStatus(() => {});

  assert.equal(applet._systemdActive, false);
  assert.equal(applet._usesAppletPolling(), true);
});

test("service status errors hand an empty automatic cache back to the applet", () => {
  const applet = makeApplet();
  applet.pollOwner = "auto";
  applet.autoRefresh = true;
  applet._serviceChecked = true;
  applet._systemdActive = true;
  applet._serviceAutoAttempted = true;
  applet._usages = [];
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._scheduleTimer = () => {};
  applet._cacheIsStale = () => false;
  applet._spawnAuxJson = (_argv, callback) => callback(null, "status unavailable");

  applet._checkServiceStatus(() => {});

  assert.equal(applet._systemdActive, false);
  assert.equal(applet._usesAppletPolling(), true);
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
      callback({ installed: true, enabled: false, active: false, service_result: "success" }, null);
    }
  };

  applet._checkServiceStatus(() => {});
  assert.deepEqual(calls, [
    "service status --format json",
    "service enable --format json",
  ]);
  assert.equal(applet._serviceAutoAttempted, true);
});

test("a failed last service run never becomes the systemd poll owner", () => {
  const applet = makeApplet();
  applet.pollOwner = "auto";
  applet.autoRefresh = true;
  applet._serviceChecked = true;
  applet._systemdActive = true;
  applet._serviceAutoAttempted = true;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._scheduleTimer = () => {};
  applet._cacheIsStale = () => false;
  applet._spawnAuxJson = (_argv, callback) => callback({
    installed: true,
    enabled: true,
    active: true,
    service_result: "failed",
  }, null);

  applet._checkServiceStatus(() => {});

  assert.equal(applet._systemdActive, false);
  assert.equal(applet._serviceAutoAttempted, false);
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
      callback({ installed: false, enabled: true, active: true, service_result: "success" }, null);
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

test("service status without successful result stays fail-closed", () => {
  const applet = makeApplet();

  assert.equal(applet._serviceStatusIsHealthy({
    installed: true,
    enabled: true,
    active: true,
  }), false);
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

test("service enable rejects a timer with a failed last run", () => {
  const applet = makeApplet();
  applet.pollOwner = "auto";
  applet.autoRefresh = true;
  applet._baseCommandArgv = () => ["codex-usage"];
  applet._scheduleTimer = () => {};
  applet._buildUsageMenu = () => {};
  applet._refreshFresh = () => {};
  applet._spawnAuxJson = (_argv, callback) => {
    callback({
      installed: true,
      enabled: true,
      active: true,
      service_result: "failed",
    }, null);
  };
  let error = "";
  applet._showCommandError = (value) => { error = value; };

  applet._enableBackgroundService();

  assert.equal(applet._systemdActive, false);
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
      callback({ installed: true, enabled: true, active: true, service_result: "success" }, null);
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

  enableCallback({ installed: true, enabled: true, active: true, service_result: "success" }, null);
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
