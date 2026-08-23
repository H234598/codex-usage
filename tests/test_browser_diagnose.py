from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

import codex_usage.browser as browser_module
from codex_usage.browser import (
    DIAGNOSTIC_MAX_RESPONSES,
    _capture_diagnostic_response,
    _detect_page_state,
    _diagnose_auth_json,
    _diagnostic_keys,
    _prepare_private_output_dir,
    _redact_url,
    _safe_body_text,
    _safe_html_text,
    _save_diagnostic_screenshot,
    _save_probe_payloads,
    _status_for_result,
    _top_level_keys,
)
from codex_usage.direct import MAX_AUTH_JSON_BYTES
from codex_usage.extractor import JsonCandidate
from codex_usage.models import Account, AccountStatus, LimitWindow


class FakeScreenshotPage:
    def screenshot(self, *, path: str, full_page: bool) -> None:
        assert full_page is True
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("fake-png")


class _BrokenInt(int):
    def __lt__(self, _other):
        raise RuntimeError("synthetic browser status comparison marker")

    def __ge__(self, _other):
        raise RuntimeError("synthetic browser status comparison marker")


class _BrokenFloat(float):
    def __float__(self):
        raise RuntimeError("synthetic browser diagnostic conversion marker")


def test_browser_numeric_boundaries_reject_subclasses_before_operations():
    assert browser_module._diagnostic_value(_BrokenFloat(50.0)) == "_BrokenFloat"
    assert browser_module._main_response_failed(_BrokenInt(200)) is True


def test_diagnose_auth_json_redacts_token_values(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": "sk-secret",
                "auth_mode": "chatgpt",
                "last_refresh": "2026-06-08T00:00:00Z",
                "tokens": {
                    "access_token": "access-secret",
                    "id_token": "id-secret",
                    "refresh_token": "refresh-secret",
                    "account_id": "acct-secret",
                },
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    result = _diagnose_auth_json(auth_path)
    serialized = json.dumps(result)

    assert result["exists"] is True
    assert result["auth_mode"] == "chatgpt"
    assert result["has_openai_api_key"] is True
    assert result["token_fields"] == ["access_token", "account_id", "id_token", "refresh_token"]
    assert result["token_presence"] == {
        "access_token": True,
        "id_token": True,
        "refresh_token": True,
        "account_id": True,
    }
    assert result["has_browser_storage_state"] is False
    assert "access-secret" not in serialized
    assert "refresh-secret" not in serialized
    assert "sk-secret" not in serialized


def test_diagnostic_response_window_keeps_newest_entries():
    responses = []

    for index in range(DIAGNOSTIC_MAX_RESPONSES + 2):
        _capture_diagnostic_response(
            SimpleNamespace(
                url=f"https://chatgpt.com/backend-api/usage/{index}",
                status=200,
                headers={"content-type": "application/json"},
            ),
            responses,
        )

    assert len(responses) == DIAGNOSTIC_MAX_RESPONSES
    assert responses[0]["url"].endswith("/2")
    assert responses[-1]["url"].endswith("/101")


def test_diagnostic_key_lists_are_bounded_and_sorted():
    payload = {f"key-{index:04d}": index for index in range(100)}

    assert _diagnostic_keys(payload) == [f"key-{index:04d}" for index in range(40)]
    assert _top_level_keys(payload) == [f"key-{index:04d}" for index in range(30)]


def test_diagnostic_response_window_ignores_irrelevant_entries_without_eviction():
    responses = []

    for index in range(DIAGNOSTIC_MAX_RESPONSES):
        _capture_diagnostic_response(
            SimpleNamespace(
                url=f"https://chatgpt.com/backend-api/usage/{index}",
                status=200,
                headers={"content-type": "application/json"},
            ),
            responses,
        )

    _capture_diagnostic_response(
        SimpleNamespace(
            url="https://example.test/irrelevant",
            status=200,
            headers={"content-type": "application/json"},
        ),
        responses,
    )

    assert responses[0]["url"].endswith("/0")
    assert responses[-1]["url"].endswith("/99")


@pytest.mark.parametrize(
    "url",
    [
        "https://evilchatgpt.com/backend-api/usage",
        "https://chatgpt.com.evil.example/backend-api/usage",
        "https://notopenai.com/rate_limit",
        "https://chatgpt.com@evil.example/rate_limit",
        "http://chatgpt.com/rate_limit",
        "https://chatgpt.com:8443/rate_limit",
    ],
)
def test_diagnostic_response_window_rejects_untrusted_urls(url: str):
    responses = []

    _capture_diagnostic_response(
        SimpleNamespace(
            url=url,
            status=200,
            headers={"content-type": "application/json"},
        ),
        responses,
    )

    assert responses == []


def test_diagnostic_response_window_accepts_openai_subdomain():
    responses = []

    _capture_diagnostic_response(
        SimpleNamespace(
            url="https://api.openai.com/rate_limit",
            status=200,
            headers={"content-type": "application/json"},
        ),
        responses,
    )

    assert responses[0]["url"] == "https://api.openai.com/rate_limit"


def test_diagnose_auth_json_ignores_relative_codex_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    relative_home = cwd / "relative"
    relative_home.mkdir(parents=True)
    (relative_home / "auth.json").write_text('{"tokens": {}}', encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("CODEX_HOME", "relative")

    result = _diagnose_auth_json(None)

    assert result["path"] == str(home / ".codex" / "auth.json")
    assert result["exists"] is False


def test_diagnose_auth_json_ignores_unknown_user_codex_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("CODEX_HOME", "~definitely-no-such-user-zzzz/.codex")

    result = _diagnose_auth_json(None)

    assert result["path"] == str(home / ".codex" / "auth.json")
    assert result["exists"] is False


def test_diagnose_auth_json_rejects_unknown_user_auth_path():
    result = _diagnose_auth_json(
        Path("~definitely-no-such-user-zzzz/auth.json")
    )

    assert result == {
        "path": "~definitely-no-such-user-zzzz/auth.json",
        "exists": False,
        "readable": False,
        "error": "auth.json path is invalid",
    }


def test_diagnose_auth_json_rejects_symlink_auth_file(tmp_path):
    target = tmp_path / "target-auth.json"
    target.write_text(
        json.dumps({"tokens": {"access_token": "access-secret"}}),
        encoding="utf-8",
    )
    target.chmod(0o600)
    auth_path = tmp_path / "auth.json"
    auth_path.symlink_to(target)

    result = _diagnose_auth_json(auth_path)
    serialized = json.dumps(result)

    assert result["exists"] is True
    assert result["readable"] is False
    assert "auth.json is not a regular file" in result["error"]
    assert "access-secret" not in serialized


def test_diagnose_auth_json_rejects_oversized_auth_file(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(" " * (MAX_AUTH_JSON_BYTES + 1), encoding="utf-8")
    auth_path.chmod(0o600)

    result = _diagnose_auth_json(auth_path)

    assert result["exists"] is True
    assert result["readable"] is False
    assert "auth.json too large" in result["error"]


@pytest.mark.parametrize("error", [OSError("read failed"), ValueError("parse failed")])
def test_diagnose_auth_json_maps_unexpected_read_errors(tmp_path, monkeypatch, error):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{}", encoding="utf-8")
    auth_path.chmod(0o600)
    monkeypatch.setattr(
        browser_module,
        "read_auth_json_file",
        lambda _path: (_ for _ in ()).throw(error),
    )

    result = _diagnose_auth_json(auth_path)

    assert result == {
        "path": str(auth_path),
        "exists": True,
        "readable": False,
        "error": type(error).__name__,
    }


def test_diagnose_auth_json_reports_non_object_payload(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("[]", encoding="utf-8")
    auth_path.chmod(0o600)

    result = _diagnose_auth_json(auth_path)

    assert result["readable"] is True
    assert result["type"] == "list"
    assert "top_level_keys" not in result


def test_diagnostic_value_preserves_safe_scalars():
    assert browser_module._diagnostic_value(None) is None
    assert browser_module._diagnostic_value(True) is True
    assert browser_module._diagnostic_value(5) == 5
    assert browser_module._diagnostic_value(5.5) == 5.5


def test_safe_html_text_maps_missing_evaluator():
    assert _safe_body_text(object()) == ""
    assert _safe_html_text(object()) == ""


def test_browser_url_and_excerpt_helpers_fail_closed():
    assert browser_module._is_trusted_browser_url(None) is False
    assert browser_module._is_trusted_browser_url("https://[::1") is False
    assert browser_module._safe_excerpt(" \n\t") == ""
    assert browser_module._safe_excerpt("visible text") == "visible text"


def test_detect_page_state_prioritizes_status_and_challenge_signals():
    analytics_url = "https://chatgpt.com/codex/cloud/settings/analytics"
    assert _detect_page_state(analytics_url, "Analytics", "", main_status=403) == "cloudflare"
    assert _detect_page_state(analytics_url, "Analytics", "turnstile") == "cloudflare"
    assert _detect_page_state(
        analytics_url,
        "Analytics",
        "",
        [{"url": "https://chatgpt.com/cdn-cgi/challenge-platform/token"}],
    ) == "cloudflare"


def test_save_diagnostic_screenshot_writes_private_file(tmp_path):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    screenshot_dir = tmp_path / "screens"

    assert _save_diagnostic_screenshot(FakeScreenshotPage(), account, None) is None
    path = _save_diagnostic_screenshot(FakeScreenshotPage(), account, screenshot_dir)

    assert path == str(screenshot_dir / "privat-diagnose.png")
    assert (screenshot_dir / "privat-diagnose.png").stat().st_mode & 0o777 == 0o600


def test_candidate_summary_and_top_level_key_shapes():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/usage",
        payload={"z": 1, "a": 2},
    )
    assert browser_module._summarize_candidate(candidate) == {
        "url": "https://chatgpt.com/backend-api/usage",
        "top_level_keys": ["a", "z"],
    }
    assert _top_level_keys([1, 2, 3]) == ["list[3]"]
    assert _top_level_keys("text") == ["str"]


def test_private_helpers_map_chmod_and_redaction_edges(tmp_path):
    class BrokenPath:
        def chmod(self, _mode):
            raise OSError("chmod failed")

    with pytest.raises(ValueError, match="could not secure private path"):
        browser_module._chmod_private(BrokenPath())
    assert _redact_url("https:///missing-host") == ""


@pytest.mark.parametrize("browser", ["firefox", "chromium"])
def test_launch_persistent_context_selects_configured_engine(browser):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile", browser=browser)
    calls = []

    class Engine:
        def launch_persistent_context(self, **kwargs):
            calls.append(kwargs)
            return "context"

    class Playwright:
        firefox = Engine()
        chromium = Engine()

    assert browser_module._launch_persistent_context(
        Playwright(), account, Path("/tmp/profile"), headless=True
    ) == "context"
    assert calls == [{"user_data_dir": "/tmp/profile", "headless": True}]


def test_launch_persistent_context_rejects_unknown_engine():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile", browser="vivaldi")

    with pytest.raises(RuntimeError, match="unsupported browser"):
        browser_module._launch_persistent_context(
            object(), account, Path("/tmp/profile"), headless=False
        )


def test_close_context_ignores_close_error():
    class BrokenContext:
        def close(self):
            raise RuntimeError("close failed")

    browser_module._close_context(BrokenContext())


def test_safe_html_text_does_not_clone_unbounded_dom():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        page = browser.new_page()
        try:
            page.set_content(
                "<main><div style='width: 97%'>5-hour usage</div>"
                + ("<p>padding</p>" * 200_000)
                + "</main>"
            )
            page.evaluate(
                """(() => {
                    const original = Node.prototype.cloneNode;
                    Node.prototype.cloneNode = function(...args) {
                        if (this === document.documentElement) {
                            throw new Error("cloneNode called");
                        }
                        return original.apply(this, args);
                    };
                })()"""
            )

            html_text = _safe_html_text(page)

            assert html_text.startswith("<html")
            assert "width: 97%" in html_text
            assert len(html_text) == 2_000_000
        finally:
            browser.close()


def test_safe_body_text_does_not_materialize_inner_text_before_limit():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        page = browser.new_page()
        try:
            page.set_content(
                "<main><div>5-hour usage 97%</div>"
                + "<div style='display:none'>hidden stale usage 1%</div>"
                + ("<div>x</div>" * 100)
                + ("x" * 3_000_000)
                + "</main>"
            )
            page.evaluate(
                """Object.defineProperty(HTMLElement.prototype, "innerText", {
                    get() { throw new Error("innerText materialized"); }
                })"""
            )

            body_text = _safe_body_text(page)

            assert "5-hour usage 97%" in body_text[:100]
            assert "hidden stale usage" not in body_text
            assert len(body_text) == 2_000_000
        finally:
            browser.close()


def test_diagnose_detects_cloudflare_challenge_and_redacts_url():
    challenge_url = "https://chatgpt.com/cdn-cgi/challenge-platform/h/g/flow/secret-token"

    assert _redact_url(challenge_url) == "https://chatgpt.com/cdn-cgi/challenge-platform/..."
    assert (
        _detect_page_state(
            "https://chatgpt.com/codex/cloud/settings/analytics",
            "Just a moment...",
            "",
            [{"status": 200, "url": challenge_url}],
        )
        == "cloudflare"
    )


@pytest.mark.parametrize(
    ("url", "title", "body_text", "responses"),
    [
        (None, None, None, None),
        ([], {}, object(), "invalid"),
        ("", "", "", [None, [], "invalid"]),
    ],
)
def test_detect_page_state_rejects_malformed_diagnostic_inputs(
    url, title, body_text, responses
):
    assert _detect_page_state(url, title, body_text, responses) == "unknown"  # type: ignore[arg-type]


def test_status_for_result_rejects_malformed_usage_windows():
    assert (
        _status_for_result(
            body_text=None,  # type: ignore[arg-type]
            current_url=None,  # type: ignore[arg-type]
            five_hour=[],  # type: ignore[arg-type]
            weekly={},  # type: ignore[arg-type]
            main_status=200,
        )
        == AccountStatus.PARTIAL
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://user:secret@chatgpt.com/path?token=value#fragment",
            "https://chatgpt.com/path",
        ),
        (
            "https://user:secret@[2001:db8::1]:8443/path?token=value",
            "https://[2001:db8::1]:8443/path",
        ),
    ],
)
def test_browser_redact_url_removes_userinfo(url, expected):
    assert _redact_url(url) == expected


def test_browser_redact_url_rejects_invalid_port():
    assert _redact_url("https://user:secret@chatgpt.com:invalid/path") == ""


def test_status_for_result_marks_reset_only_windows_partial():
    window = LimitWindow(name="5h", reset_at=None)

    assert (
        _status_for_result(
            body_text="Codex analytics",
            current_url="https://chatgpt.com/codex/cloud/settings/analytics",
            five_hour=window,
            weekly=window,
            main_status=200,
        )
        == AccountStatus.PARTIAL
    )


def test_status_for_result_prioritizes_logged_out_page_over_stale_usage_values():
    assert (
        _status_for_result(
            body_text="Log in to get answers based on saved chats Sign up for free",
            current_url="https://chatgpt.com/",
            five_hour=LimitWindow(name="5h", remaining=97),
            weekly=LimitWindow(name="weekly", remaining=55),
            main_status=200,
        )
        == AccountStatus.LOGIN_REQUIRED
    )


@pytest.mark.parametrize(
    ("main_status", "expected"),
    [
        (None, AccountStatus.ERROR),
        (401, AccountStatus.LOGIN_REQUIRED),
        (403, AccountStatus.ERROR),
        (429, AccountStatus.ERROR),
        (500, AccountStatus.ERROR),
        (302, AccountStatus.ERROR),
        (304, AccountStatus.ERROR),
        (_BrokenInt(200), AccountStatus.ERROR),
    ],
)
def test_status_for_result_rejects_failed_main_response_with_usage_values(
    main_status, expected
):
    assert (
        _status_for_result(
            body_text="5-hour usage limit 97% Weekly usage limit 55%",
            current_url="https://chatgpt.com/codex/cloud/settings/analytics",
            five_hour=LimitWindow(name="5h", remaining=97),
            weekly=LimitWindow(name="weekly", remaining=55),
            main_status=main_status,
        )
        == expected
    )


def test_save_diagnostic_screenshot_rejects_symlink_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    screenshot_link = tmp_path / "screens"
    screenshot_link.symlink_to(outside, target_is_directory=True)
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")

    with pytest.raises(ValueError, match="diagnose screenshot directory"):
        _save_diagnostic_screenshot(FakeScreenshotPage(), account, screenshot_link)

    assert not (outside / "privat-diagnose.png").exists()


def test_private_output_directory_rejects_failed_permission_hardening(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "screens"
    output_dir.mkdir()

    def fail_private_directory(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(
        "codex_usage.browser.ensure_private_directory",
        fail_private_directory,
    )

    with pytest.raises(ValueError, match="could not secure private path"):
        _prepare_private_output_dir(output_dir, label="diagnose screenshot directory")


def test_save_diagnostic_screenshot_rejects_symlink_output_file(tmp_path):
    screenshot_dir = tmp_path / "screens"
    screenshot_dir.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_text("keep", encoding="utf-8")
    (screenshot_dir / "privat-diagnose.png").symlink_to(outside)
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")

    with pytest.raises(ValueError, match="diagnose screenshot path"):
        _save_diagnostic_screenshot(FakeScreenshotPage(), account, screenshot_dir)

    assert outside.read_text(encoding="utf-8") == "keep"


def test_save_diagnostic_screenshot_rejects_path_traversal_account_id(tmp_path):
    account = Account(
        id="../escape",
        label="Escape",
        profile_dir=str(tmp_path / "profile"),
    )

    with pytest.raises(ValueError, match="account id"):
        _save_diagnostic_screenshot(FakeScreenshotPage(), account, tmp_path)


def test_save_probe_payloads_rejects_symlink_save_dir(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    save_link = tmp_path / "probe"
    save_link.symlink_to(outside, target_is_directory=True)
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")

    with pytest.raises(ValueError, match="probe save directory"):
        _save_probe_payloads(
            save_link,
            account,
            [JsonCandidate(url="https://chatgpt.com/backend-api/wham/usage", payload={})],
            "visible body",
        )

    assert not (outside / "privat-01.json").exists()
    assert not (outside / "privat-body.txt").exists()


def test_save_probe_payloads_rejects_symlink_output_file(tmp_path):
    save_dir = tmp_path / "probe"
    save_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    (save_dir / "privat-body.txt").symlink_to(outside)
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")

    with pytest.raises(ValueError, match="probe output path"):
        _save_probe_payloads(save_dir, account, [], "visible body")

    assert outside.read_text(encoding="utf-8") == "keep"


def test_save_probe_payloads_rejects_path_traversal_account_id(tmp_path):
    account = Account(
        id="../escape",
        label="Escape",
        profile_dir=str(tmp_path / "profile"),
    )

    with pytest.raises(ValueError, match="account id"):
        _save_probe_payloads(tmp_path, account, [], "visible body")
