# Account- und Schwellen-Einstellungen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Account-Verwaltung, Account-Anzeige, Schwellenformatierung, Spark-Warnungen und Reaktivierung vollständig in Applet-Einstellungen abbilden, mit CLI als Konfigurationsquelle.

**Architecture:** Cinnamon-Listen bleiben UI-Primitiv. Das Applet liest Accountdaten über `account overview --format json --config-only` und schreibt Änderungen über `account add --format json`; direkte TOML-Schreibzugriffe entfallen. Bestehende Nutzungs-, Warnungs- und Formatierungspfade werden um normalisierte per-Account-Einstellungen erweitert.

**Tech Stack:** Python 3.11, `argparse`, TOML über `tomllib`, Cinnamon JSON settings schema, Cinnamon/GJS JavaScript, Node `node:test`, pytest, Makefile.

## Global Constraints

- `auth.json` bleibt optional.
- Fehlender Profilordner wird automatisch mit bestehender privater Verzeichnislogik angelegt.
- `no Spark` bedeutet nur sicher fehlendes echtes Spark-Limit; fehlerhafte oder unbekannte Spark-Daten bleiben unbekannt.
- CLI bleibt Quelle für Account-Konfiguration; Applet schreibt nicht direkt in `config.toml`.
- Bestehende Account-ID bleibt Identität; neue Accounts entstehen über `+`.
- Kürzel und Anzeigeziel werden zentral unter `Formatierungsorte` verwaltet.
- Formatierungsreihenfolge: Account-ID, unabhängige Felder, `Über der Schwelle`, Schwelle, `Unter der Schwelle`.
- Keine neue Abhängigkeit und keine eigene GTK-Dialogschicht.
- Jeder nichttriviale Änderungspfad erhält mindestens einen automatisierten Test.
- Vollständige Plan-, Spezifikations- und Abschlussdokumente; Kurzfassung höchstens zusätzlich.

---

## Datei- und Verantwortungsübersicht

| Datei | Verantwortung |
|---|---|
| `src/codex_usage/models.py` | Account-Datenmodell inklusive Reaktivierungsbrowser |
| `src/codex_usage/config.py` | TOML-Laden, -Speichern, Defaults und Validierung |
| `src/codex_usage/reactivate.py` | Browserauswahl und Account-Default für OAuth-Reaktivierung |
| `src/codex_usage/cli.py` | strukturierte Account-Anlage/-Aktualisierung und Übersicht |
| `files/codex-usage@H234598/settings-schema.json` | Seiten, Tabellen, Feldtypen, Optionen und sichtbare Beschriftungen |
| `files/codex-usage@H234598/applet.js` | Settings-Bindings, Migrationen, Reconcile, Anzeige und Warnlogik |
| `tests/test_config.py` | Account-Konfigurations- und Profilordner-Verhalten |
| `tests/test_cli.py` | CLI-Parser und JSON-Ausgaben |
| `tests/test_reactivate.py` | Reaktivierungsbrowser-Auflösung |
| `tests/test_applet.py` | statische Schema-/Binding-Prüfungen |
| `tests/applet_runtime.test.js` | Laufzeitverhalten von Applet-Settings, Migration und Warnungen |
| `docs/superpowers/specs/2026-08-11-account-settings-parity-design.md` | freigegebene Spezifikation |
| `/home/teladi/Dokumente/Obsidian_Vaults/Teladi_Programming/Projekte/codex-usage/Baupläne/2026-08-11-account-settings-parity.md` | vollständige Vault-Kopie dieses Plans |
| `/home/teladi/Dokumente/Obsidian_Vaults/Teladi_Programming/Projekte/codex-usage/Berichte/2026-08-11-CLI-Applet-Feature-Parität.md` | finaler CLI/Applet-Vergleich |

---

### Task 1: Account-Modell und Konfigurationsspeicherung erweitern

**Files:**
- Modify: `src/codex_usage/models.py:77-86`
- Modify: `src/codex_usage/config.py:18-30,126-170,220-270,379-478`
- Modify: `src/codex_usage/reactivate.py:24-50`
- Test: `tests/test_config.py`
- Test: `tests/test_reactivate.py`

**Interfaces:**
- Consumes: bestehendes `Account`, `add_or_update_account`, TOML-Parser und `_select_browser`.
- Produces: `Account.reactivation_browser: str`, `SUPPORTED_REACTIVATION_BROWSERS`, und `add_or_update_account(..., reactivation_browser: str | None = None, clear_auth_json: bool = False)`.

- [ ] **Step 1: Write failing config tests**

```python
def test_config_round_trip_reactivation_browser(tmp_path):
    config_path = tmp_path / "config.toml"
    _, account = add_or_update_account(
        "privat",
        reactivation_browser="vivaldi",
        path=config_path,
    )

    loaded = load_config(config_path)

    assert account.reactivation_browser == "vivaldi"
    assert loaded.accounts[0].reactivation_browser == "vivaldi"
    assert 'reactivation_browser = "vivaldi"' in config_path.read_text(encoding="utf-8")


def test_config_defaults_reactivation_browser_for_legacy_account(tmp_path):
    config_path = tmp_path / "legacy.toml"
    config_path.write_text('[[accounts]]\nid = "legacy"\n', encoding="utf-8")

    assert load_config(config_path).accounts[0].reactivation_browser == "auto"


def test_config_rejects_unknown_reactivation_browser(tmp_path):
    with pytest.raises(ValueError, match="reactivation browser must be one of"):
        add_or_update_account(
            "privat",
            reactivation_browser="netscape",
            path=tmp_path / "config.toml",
        )
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/test_config.py -k reactivation_browser -v`

Expected: FAIL because `Account` and `add_or_update_account` do not yet accept or persist `reactivation_browser`.

- [ ] **Step 3: Add model field and config validation**

Implement:

```python
SUPPORTED_REACTIVATION_BROWSERS = ("auto", "vivaldi", "chromium", "firefox")

@dataclass(frozen=True)
class Account:
    id: str
    label: str
    profile_dir: str
    browser: str = "firefox"
    auth_json_path: str | None = None
    backend: str = "direct"
    reactivation_browser: str = "auto"
```

Extend `_account_from_data`, `add_or_update_account`, `_validate_account` and `_to_toml`. Missing TOML values resolve to `auto`; invalid values raise the same style of `ValueError` as normal browser/backend validation. Implement `clear_auth_json=True` as the explicit way to remove an existing auth path; reject it when `auth_json_path` is also supplied. Preserve existing profile creation, auth-path validation, account locking and state invalidation.

- [ ] **Step 4: Make reactivation use account default only when no CLI override exists**

Change `reactivate_account` to accept `browser: str | None = None`. Resolve `browser or account.reactivation_browser` before `_select_browser`; an explicit CLI value including `auto` remains an override. Keep direct callers without a configured value compatible with `auto`.

Add a failing test using the existing subprocess/browser-helper fixture:

```python
def test_reactivate_uses_account_browser_when_override_is_missing(monkeypatch, account):
    captured = {}
    monkeypatch.setattr(
        "codex_usage.reactivate._reactivate_account_unlocked",
        lambda _account, **kwargs: captured.update(kwargs) or {},
    )

    reactivate_account(account, browser=None)

    assert captured["browser"] == account.reactivation_browser
```

Run: `pytest tests/test_reactivate.py -k account_browser -v`

Expected before implementation: FAIL because the function always defaults to `auto`.

- [ ] **Step 5: Run focused tests**

Run: `pytest tests/test_config.py tests/test_reactivate.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/codex_usage/models.py src/codex_usage/config.py src/codex_usage/reactivate.py tests/test_config.py tests/test_reactivate.py
git commit -m "feat: persist per-account reactivation browser"
```

---

### Task 2: CLI-Account-API für vollständige Applet-Reconcile erweitern

**Files:**
- Modify: `src/codex_usage/cli.py:76-158,214-279,480-570`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1 Account-Felder und `add_or_update_account`.
- Produces: `account add --format table|json`, `--reactivation-browser`, `--clear-auth-json`, sowie vollständige Felder in `account overview --format json --config-only`.

**JSON-Vertrag:**

```json
{
  "ok": true,
  "account": {
    "id": "privat",
    "label": "Privat",
    "profile_dir": "/abs/profile",
    "auth_json_path": null,
    "browser": "firefox",
    "reactivation_browser": "auto",
    "backend": "direct"
  }
}
```

- [ ] **Step 1: Write failing CLI tests**

```python
def test_account_add_json_returns_all_editable_fields(tmp_path, capsys):
    assert cli.main([
        "--config", str(tmp_path / "config.toml"),
        "account", "add", "privat",
        "--label", "Privat",
        "--profile-dir", str(tmp_path / "profile"),
        "--browser", "chromium",
        "--reactivation-browser", "firefox",
        "--backend", "app-server",
        "--format", "json",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["account"]["label"] == "Privat"
    assert payload["account"]["browser"] == "chromium"
    assert payload["account"]["reactivation_browser"] == "firefox"
    assert payload["account"]["backend"] == "app-server"
    assert payload["account"]["auth_json_path"] is None


def test_account_overview_json_exposes_paths_and_reactivation_browser(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    add_or_update_account(
        "privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
        reactivation_browser="vivaldi",
        path=config_path,
    )

    assert cli.main([
        "--config", str(config_path),
        "account", "overview", "--format", "json", "--config-only",
    ]) == 0

    item = json.loads(capsys.readouterr().out)["accounts"][0]
    assert item["profile_dir"] == str(tmp_path / "profile")
    assert item["auth_json_path"] == str(tmp_path / "auth.json")
    assert item["reactivation_browser"] == "vivaldi"


def test_account_add_clear_auth_json(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    add_or_update_account("privat", auth_json_path=str(tmp_path / "auth.json"), path=config_path)

    assert cli.main([
        "--config", str(config_path),
        "account", "add", "privat", "--clear-auth-json", "--format", "json",
    ]) == 0

    assert json.loads(capsys.readouterr().out)["account"]["auth_json_path"] is None
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `pytest tests/test_cli.py -k 'account_add_json or overview_json or clear_auth' -v`

Expected: FAIL because the parser lacks `--format`, `--reactivation-browser`, and `--clear-auth-json`, and overview omits editable paths.

- [ ] **Step 3: Implement parser and JSON serialization**

Add account-add arguments:

```python
add.add_argument("--reactivation-browser", choices=SUPPORTED_REACTIVATION_BROWSERS)
add.add_argument("--clear-auth-json", action="store_true")
add.add_argument("--format", choices=("table", "json"), default="table")
```

Reject `--clear-auth-json` together with `--auth-json`. Pass `clear_auth_json=True` for clearing, omit both flags for unchanged auth JSON, and pass the new reactivation browser to `add_or_update_account`. Emit the JSON contract above without token content. Keep table output compatible with current users.

Add `profile_dir`, `auth_json_path`, and `reactivation_browser` to each config-only overview account. Keep live usage fields and exit codes unchanged.

- [ ] **Step 4: Run focused tests**

Run: `pytest tests/test_cli.py -k 'account_add_json or overview_json or clear_auth' -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/codex_usage/cli.py tests/test_cli.py
git commit -m "feat: expose complete account JSON API"
```

---

### Task 3: Account-Tabelle, Profilhinweis und Reaktivierungs-UI umsetzen

**Files:**
- Modify: `files/codex-usage@H234598/settings-schema.json:1-145,176-205,1136-1155`
- Modify: `files/codex-usage@H234598/applet.js:190-215,1181-1260,1700-1970,2435-2505,4300-4410`
- Test: `tests/test_applet.py`
- Test: `tests/applet_runtime.test.js`

**Interfaces:**
- Consumes: Task 2 JSON contract.
- Produces: central row shape `{account, label, auth-json, profile-dir, browser, reactivation-browser, backend}` and `_reconcileAccountChanges(rows)` using `account add --format json`.

- [ ] **Step 1: Write failing schema tests**

```python
def test_account_table_contains_all_editable_fields(settings):
    table = settings["account-backends"]
    assert settings["layout"]["backend-section"]["title"] == "Abrufwege und Accounts"
    assert [column["id"] for column in table["columns"]] == [
        "account", "label", "auth-json", "profile-dir", "browser",
        "reactivation-browser", "backend",
    ]
    assert table["show-buttons"] is True
    assert set(table["hidden-buttons"]) == {"-", "up", "down"}
    assert "automatisch angelegt" in table["description"]


def test_reactivation_page_is_removed_and_switch_is_on_codex_usage(settings):
    assert "login-page" not in settings["layout"]["pages"]
    assert "show-reactivation-actions" in settings["layout"]["reactivation-options-section"]["keys"]
    assert "reactivation-browser" not in settings["layout"]["reactivation-options-section"]["keys"]
```

In `tests/test_applet.py`, obtain `settings` inside each test with the existing
`json.loads((APPLET_DIR / "settings-schema.json").read_text(encoding="utf-8"))`
pattern already used by `test_applet_metadata_and_settings_are_consistent`.

- [ ] **Step 2: Run schema tests and verify failure**

Run: `pytest tests/test_applet.py -k 'account_table or reactivation_page' -v`

Expected: FAIL because the current table has only account, label and backend, and the old Reaktivierung page still exists.

- [ ] **Step 3: Change schema layout and table fields**

Rename `backend-section` title. Extend `account-backends` with:

- `auth-json`: `file`, optional empty default
- `profile-dir`: `file`, `select-dir: true`
- `browser`: integer options `firefox` and `chromium`
- `reactivation-browser`: integer options `auto`, `vivaldi`, `chromium`, `firefox`
- existing backend integer options

Set `show-buttons: true` and `hidden-buttons: ["-", "up", "down"]`, so `+` and edit remain available. Put the exact profile-folder hint in the visible list description. Move `show-reactivation-actions` into a new section on the `Codex Usage` page. Keep the old `reactivation-browser` definition as an unlisted migration key and add hidden boolean `reactivation-browser-migrated` with default `false`.

- [ ] **Step 4: Write failing runtime tests for account JSON loading and reconcile**

```javascript
function makeAccountApplet() {
  const applet = makeApplet();
  applet._backendAccounts = {
    alpha: {
      account: "alpha", label: "Alpha", "auth-json": "",
      "profile-dir": "/tmp/alpha", browser: 0,
      "reactivation-browser": 0, backend: 0,
    },
  };
  applet._backendRowsReady = true;
  return applet;
}

test("account overview rows expose editable account settings", () => {
  const applet = makeApplet();
  applet._spawnAuxJson = (argv, callback) => {
    assert.deepEqual(argv.slice(-4), ["overview", "--format", "json", "--config-only"]);
    callback({accounts: [{
      id: "alpha", label: "Alpha", profile_dir: "/tmp/alpha",
      auth_json_path: null, browser: "chromium",
      reactivation_browser: "vivaldi", backend: "app-server",
    }]}, null);
  };

  applet._loadAccountBackends();

  assert.deepEqual(applet.accountBackends[0], {
    account: "alpha", label: "Alpha", "auth-json": "",
    "profile-dir": "/tmp/alpha", browser: 1,
    "reactivation-browser": 1, backend: 1,
  });
});


test("account table changes call account add JSON API", () => {
  const applet = makeAccountApplet();
  const calls = [];
  applet._spawnAuxJson = (argv, callback) => { calls.push(argv); callback({ok: true, account: {}}, null); };
  applet.accountBackends = [{
    account: "alpha", label: "Renamed", "auth-json": "",
    "profile-dir": "/tmp/alpha", browser: 1,
    "reactivation-browser": 2, backend: 0,
  }];

  applet._onAccountBackendsChanged();

  assert.equal(calls[0].includes("account"), true);
  assert.equal(calls[0].includes("add"), true);
  assert.equal(calls[0].includes("--format"), true);
  assert.equal(calls[0].includes("--reactivation-browser"), true);
});
```

- [ ] **Step 5: Run runtime tests and verify failure**

Run: `node --test tests/applet_runtime.test.js --test-name-pattern='account overview rows|account table changes'`

Expected: FAIL because `_loadAccountBackends` discards paths/browser data and `_onAccountBackendsChanged` only reconciles backend.

- [ ] **Step 6: Implement account row normalization and reconcile**

Extend `_loadAccountBackends` canonical rows with all JSON fields. Keep internal backend integer mapping compatible with existing panel usage rows. Replace backend-only validation with full-row validation:

```javascript
const ACCOUNT_BACKEND_VALUES = {0: "direct", 1: "app-server"};
const ACCOUNT_BROWSER_VALUES = {0: "firefox", 1: "chromium"};
const ACCOUNT_REACTIVATION_BROWSER_VALUES = {0: "auto", 1: "vivaldi", 2: "chromium", 3: "firefox"};
```

Reject malformed paths, duplicate IDs, invalid choices and edits to an existing Account-ID. Permit a new row from `+`; require its ID and use CLI defaults for blank optional values. Build one `account add` command per changed row, queue them through existing auxiliary process serialization, require `ok: true` JSON, then reload overview. On any error show existing command error and reload without claiming success.

Implement one-time legacy migration: while `reactivation-browser-migrated` is false, apply old global `reactivation-browser` to existing accounts that still have `auto`, use the same account-add queue, then set the marker. Do not reapply after marker is true. New accounts default to `auto`. Change `_reactivateAccount` to use the selected account row's reactivation browser instead of the removed global visible setting.

- [ ] **Step 7: Run account and applet checks**

Run: `pytest tests/test_applet.py -k 'account_table or reactivation_page' -v && node --test tests/applet_runtime.test.js --test-name-pattern='account overview rows|account table changes'`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add files/codex-usage@H234598/settings-schema.json files/codex-usage@H234598/applet.js tests/test_applet.py tests/applet_runtime.test.js
git commit -m "feat: manage accounts from applet settings"
```

---

### Task 4: Zentrale Account-Anzeige für Kürzel, ID und Label

**Files:**
- Modify: `files/codex-usage@H234598/settings-schema.json:80-125,883-983`
- Modify: `files/codex-usage@H234598/applet.js:203-214,1710-1795,1890-2060,4010-4095,4680-4725,4860-4895`
- Test: `tests/test_applet.py`
- Test: `tests/applet_runtime.test.js`

**Interfaces:**
- Consumes: Task 3 canonical account map.
- Produces: `account-display-settings` rows `{account, tag, panel, hover, click}` and `_accountDisplayText(item, surface)`.

- [ ] **Step 1: Write failing schema and migration tests**

```python
def test_display_table_replaces_panel_tag_column(settings):
    assert [column["id"] for column in settings["account-panel-settings"]["columns"]] == [
        "account", "order", "muted", "slot1", "slot2",
    ]
    table = settings["account-display-settings"]
    assert [column["id"] for column in table["columns"]] == [
        "account", "tag", "panel", "hover", "click",
    ]
    for column in table["columns"][2:]:
        assert set(column["options"].values()) == {0, 1, 2}
```

```javascript
test("legacy panel tags migrate to central display settings", () => {
  const applet = makeAccountApplet();
  applet.accountPanelSettings = [{account: "alpha", tag: "A", order: 1, muted: false, slot1: 3, slot2: 0}];

  applet._syncStyleRows([applet._backendAccounts.alpha]);

  assert.equal(applet.accountStyleTargets[0].tag, "A");
  assert.equal(applet.accountStyleTargets[0].panel, 2);
  assert.equal(applet.accountStyleTargets[0].hover, 1);
  assert.equal(applet.accountStyleTargets[0].click, 1);
});
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/test_applet.py -k display_table -v && node --test tests/applet_runtime.test.js --test-name-pattern='legacy panel tags'`

Expected: FAIL because the central table does not exist and panel rows still own `tag`.

- [ ] **Step 3: Add schema and normalized display rows**

Add `account-display-settings` to `style-target-section` with columns:

- `account`: string
- `tag`: string, maximum 8 characters
- `panel`, `hover`, `click`: integer options `Account-ID: 0`, `Label: 1`, `Kürzel: 2`

Use `show-buttons: true` with only edit available for generated account rows. Remove `tag` from `account-panel-settings` columns. Keep row object keys for panel order, mute and slots unchanged.

- [ ] **Step 4: Implement migration and display resolver**

Add `_defaultDisplayRow`, `_normalizeDisplayRow`, `_mergedDisplayRows`, `_displaySettingsMap`, and `_accountDisplayText(item, surface)`. Defaults are `panel: 2`, `hover: 1`, `click: 1`; `tag` comes from legacy panel row when present, otherwise empty. During one guarded settings sync, write migrated central rows and write panel rows without `tag`.

Replace account-heading uses in `_panelTag`, `_addAccount`, `_addAccountControls`, and `_tooltipContent` with the appropriate surface resolver. Keep notification titles based on the configured label because notifications are not one of the three display surfaces. Escape resolved text in markup exactly as current label text is escaped.

- [ ] **Step 5: Run focused tests**

Run: `pytest tests/test_applet.py -k display_table -v && node --test tests/applet_runtime.test.js --test-name-pattern='legacy panel tags|display target|tooltip|panel tag'`

Expected: PASS, with panel showing Kürzel by default and hover/click showing Label by default.

- [ ] **Step 6: Commit**

```bash
git add files/codex-usage@H234598/settings-schema.json files/codex-usage@H234598/applet.js tests/test_applet.py tests/applet_runtime.test.js
git commit -m "feat: centralize account display labels"
```

---

### Task 5: Schwellenfelder in gewünschter Reihenfolge und Beschriftung

**Files:**
- Modify: `files/codex-usage@H234598/settings-schema.json:206-881`
- Test: `tests/test_applet.py`

**Interfaces:**
- Consumes: bestehende Row-IDs und Normalisierung in `applet.js`.
- Produces: schema-only ordering and titles; stored row values remain keyed by existing IDs, so no runtime migration is required for style data.

- [ ] **Step 1: Write failing schema order/title tests**

```python
def test_style_tables_group_threshold_fields(settings):
    expected = {
        "account-percent-styles": [
            "account", "mode", "font", "size", "bold", "italic",
            "color", "background", "threshold", "below-font", "below-size",
            "below-bold", "below-italic", "below-color", "below-background",
        ],
        "account-date-styles": [
            "account", "format", "mode", "font", "size", "bold", "italic",
            "color", "background", "threshold", "below-font", "below-size",
            "below-bold", "below-italic", "below-color", "below-background",
        ],
    }
    for name, ids in expected.items():
        assert [column["id"] for column in settings[name]["columns"]] == ids
        columns = {column["id"]: column for column in settings[name]["columns"]}
        assert columns["font"]["title"].startswith("Über der Schwelle")
        assert columns["below-font"]["title"].startswith("Unter der Schwelle")
        assert columns["threshold"]["title"] == "Schwelle %"
```

Add equivalent assertions for time and duration; duration threshold remains `Schwelle Minuten`.

- [ ] **Step 2: Run test and verify failure**

Run: `pytest tests/test_applet.py -k style_tables_group -v`

Expected: FAIL because threshold currently precedes above-threshold style fields and above fields have no prefix.

- [ ] **Step 3: Reorder schema columns without changing IDs**

For percent, date, time and duration lists use this order:

```text
account,
independent fields (format/mode where present),
font, size, bold, italic, color, background,
threshold,
below-font, below-size, below-bold, below-italic, below-color, below-background
```

Rename normal style titles to `Über der Schwelle ...`; rename below titles to `Unter der Schwelle ...`. Keep all option maps, defaults, ranges and row IDs unchanged. Cinnamon inserts separators between columns, so this ordering produces the requested dialog grouping without a separator pseudo-field.

- [ ] **Step 4: Run schema and JSON validation**

Run: `pytest tests/test_applet.py -k style_tables_group -v && python3 -m json.tool files/codex-usage@H234598/settings-schema.json >/dev/null`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add files/codex-usage@H234598/settings-schema.json tests/test_applet.py
git commit -m "feat: group threshold formatting fields"
```

---

### Task 6: Separate Spark-Warnschwelle und `no Spark`

**Files:**
- Modify: `files/codex-usage@H234598/settings-schema.json:1084-1125`
- Modify: `files/codex-usage@H234598/applet.js:1820-1880,4740-4830,4930-4995`
- Test: `tests/test_applet.py`
- Test: `tests/applet_runtime.test.js`

**Interfaces:**
- Consumes: usage pools from `_modelPool`, existing `five-threshold`/`weekly-threshold`, and account rows from Task 3.
- Produces: alert row field `spark-threshold` stored as display text: decimal string `"0"`–`"100"` or sentinel `"no Spark"`; `_sparkLimitState(usage)` returns `"present"`, `"none"`, or `"unknown"`.

- [ ] **Step 1: Write failing schema and normalization tests**

```python
def test_alert_table_has_editable_spark_column(settings):
    table = settings["account-alert-settings"]
    assert [column["id"] for column in table["columns"]] == [
        "account", "five-threshold", "weekly-threshold", "spark-threshold",
        "warnings", "errors",
    ]
    assert table["columns"][3]["title"] == "Spark %"
    assert table["show-buttons"] is True
    assert set(table["hidden-buttons"]) == {"+", "-", "up", "down"}
```

```javascript
function usageWithoutSparkLimit(account) {
  return {
    account,
    label: account,
    status: "ok",
    main: {available: true, windows: []},
    models: {},
  };
}

function usageWithSparkWindows(account, values) {
  return {
    account,
    label: account,
    status: "ok",
    main: {available: true, windows: []},
    models: {
      "gpt-5.3-codex-spark": {
        available: true,
        windows: [
          {name: "5h", duration_seconds: 18000, remaining: values.five},
          {name: "weekly", duration_seconds: 604800, remaining: values.weekly},
        ],
      },
    },
  };
}

test("accounts without a Spark limit show no Spark and ignore edits", () => {
  const applet = makeAccountApplet();
  const usage = usageWithoutSparkLimit("alpha");
  applet._usages = [usage];

  const normalized = applet._normalizeAlertRow({
    account: "alpha", "five-threshold": 20, "weekly-threshold": 30,
    "spark-threshold": "45", warnings: true, errors: true,
  }, "alpha");

  assert.equal(normalized["spark-threshold"], "no Spark");
});


test("Spark notification uses dedicated Spark threshold", () => {
  const applet = makeAccountApplet();
  applet._alertSettings = {alpha: {
    account: "alpha", "five-threshold": 20, "weekly-threshold": 20,
    "spark-threshold": "40", warnings: true, errors: true,
  }};
  applet._usages = [usageWithSparkWindows("alpha", {five: 35, weekly: 90})];
  applet.notifyWarnings = false;

  applet._notifyForPayload();

  assert.equal(applet._warningState["alpha:Spark 5h"], true);
});
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/test_applet.py -k spark_column -v && node --test tests/applet_runtime.test.js --test-name-pattern='no Spark|dedicated Spark threshold'`

Expected: FAIL because alert rows have no Spark field and Spark windows currently select five-hour or weekly threshold.

- [ ] **Step 3: Add schema field and editable display representation**

Add `spark-threshold` as a string column with default `"20"`, description `Spark-Schwelle; bei fehlendem Limit no Spark`. Use `show-buttons: true` and hide add/remove/up/down so only edit remains. String is required because the same visible cell must render a number and the literal `no Spark`.

- [ ] **Step 4: Implement explicit Spark state classification**

Implement `_sparkLimitState(usage)` with these rules:

1. `present`: canonical Spark pool exists with valid usage evidence/windows.
2. `none`: a completed valid usage result has no Spark pool at all.
3. `unknown`: partial/stale/error data, catalog-only Spark, unavailable pool, malformed pool, or missing usage object.

Do not classify `_poolIsUsable(spark) === false` alone as `none`; that would hide API failures as entitlement absence.

Normalize `spark-threshold` to `"no Spark"` for `none`, preserve a valid numeric string for `present` and `unknown`, and use the global warning default for missing/invalid values. Accept legacy integer values 0–100 and convert them to strings. When state changes from `none` to `present`, replace `"no Spark"` with the global numeric default and allow later edits.

- [ ] **Step 5: Apply Spark threshold to notifications and panel severity**

In `_notifyForPayload`, add every known Spark window with key `spark-threshold`. Parse the field only when numeric; skip Spark warnings for `no Spark`. Keep 5h and week entries mapped to their existing keys.

In `_panelThreshold`, return the Spark threshold for sources 4–7. For `no Spark`, return `100` only as a defensive value because `_panelValueForSource` already returns `null`; preserve existing main-window averaging for sources 1–3. This makes panel warning state and notifications use the same account threshold.

- [ ] **Step 6: Run focused tests**

Run: `pytest tests/test_applet.py -k spark_column -v && node --test tests/applet_runtime.test.js --test-name-pattern='no Spark|dedicated Spark threshold|Spark panel threshold|weekly threshold'`

Expected: PASS. Existing 5h/week tests must remain green.

- [ ] **Step 7: Commit**

```bash
git add files/codex-usage@H234598/settings-schema.json files/codex-usage@H234598/applet.js tests/test_applet.py tests/applet_runtime.test.js
git commit -m "feat: add per-account Spark warning threshold"
```

---

### Task 7: Vollständige Migration, Regressionstests und CLI/Applet-Paritätsbericht

**Files:**
- Modify: `files/codex-usage@H234598/applet.js` only where integration guards expose stale row shapes
- Modify: `tests/test_applet.py` all expected schema row IDs and titles
- Modify: `tests/applet_runtime.test.js` all account-panel/alert fixtures missing migrated fields
- Create: `/home/teladi/Dokumente/Obsidian_Vaults/Teladi_Programming/Projekte/codex-usage/Berichte/2026-08-11-CLI-Applet-Feature-Parität.md`

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces: clean migration from old settings, complete automated regression coverage, and a full parity matrix.

- [ ] **Step 1: Write migration regression tests**

```javascript
test("legacy alert rows receive Spark default without changing other thresholds", () => {
  const applet = makeAccountApplet();
  applet.accountAlertSettings = [{
    account: "alpha", "five-threshold": 12, "weekly-threshold": 34,
    warnings: true, errors: false,
  }];
  applet.warningThreshold = 20;

  const rows = applet._mergedAlertRows([applet._backendAccounts.alpha], applet.accountAlertSettings);

  assert.deepEqual(rows[0], {
    account: "alpha", "five-threshold": 12, "weekly-threshold": 34,
    "spark-threshold": "20", warnings: true, errors: false,
  });
});


test("legacy panel tag is removed from stored panel row after display migration", () => {
  const applet = makeAccountApplet();
  const rows = applet._mergedPanelRows([applet._backendAccounts.alpha], [{
    account: "alpha", tag: "AA", order: 1, muted: false, slot1: 3, slot2: 0,
  }]);

  assert.equal(Object.prototype.hasOwnProperty.call(rows[0], "tag"), false);
});
```

- [ ] **Step 2: Run migration tests and verify failure**

Run: `node --test tests/applet_runtime.test.js --test-name-pattern='legacy alert rows|legacy panel tag'`

Expected: FAIL because old row normalizers currently reject missing Spark values and retain panel tags.

- [ ] **Step 3: Implement guarded migrations and update all fixtures**

Ensure `_syncAccountSettings` and `_syncStyleRows` are idempotent: old rows are normalized once, account order stays stable, unknown/duplicate accounts are discarded safely, and settings writes occur only when row data differs. Update test fixtures to assert persisted object keys rather than column position where runtime object shape is independent from schema order.

- [ ] **Step 4: Run complete verification**

Run:

```bash
make check
python3 -m pytest
git diff --check
```

Expected: JSON validation, optional GJS syntax validation, all Node applet tests, all Python tests and no whitespace errors.

- [ ] **Step 5: Build full CLI/Applet parity report**

Inspect final CLI parser and final schema. Write a complete matrix in the Vault report with one row per CLI capability:

- account add/update, label, auth JSON, profile directory, normal browser, reactivation browser, backend
- account overview, backend command and account delete
- login and reactivate, including explicit CLI browser override
- once, watch, watchdog, refresh ownership and config/command paths
- policy/routing and paid-credit settings
- Spark health, diagnostics, ingest/latest/values, health
- browser bridge commands and service commands

For each row record: CLI command/options, Applet control/action, shared source of truth, and intentional gap. Mark account deletion, diagnostic/service/bridge commands and one-time CLI overrides as CLI-only unless final implementation exposes them. Keep report complete, not a summary.

- [ ] **Step 6: Commit final integration and report**

```bash
git add files/codex-usage@H234598/applet.js tests/test_applet.py tests/applet_runtime.test.js
git commit -m "test: verify account settings migration and parity"
```

The Vault parity report is outside the repository commit and remains as the complete project artifact.

---

## Final handoff checks

- [ ] `Abrufwege und Accounts` visible with `+`, edit dialog, optional Auth JSON and profile hint.
- [ ] Missing profile directory is actually created.
- [ ] Existing account labels and all account options round-trip through CLI JSON.
- [ ] Reaktivierung page gone; switch visible in `Codex Usage`; browser selectable per account.
- [ ] Central display table controls ID/Label/Kürzel independently for panel, hover and click.
- [ ] Old `Leiste` Kürzel column gone; old values migrated.
- [ ] Formatting dialog order and prefixes match specification.
- [ ] `Spark %` editable for Spark accounts and exactly `no Spark` for accounts without real Spark limit.
- [ ] Unknown Spark/API state does not erase numeric threshold.
- [ ] CLI/Applet parity report complete in Obsidian Vault.
