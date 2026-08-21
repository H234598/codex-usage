from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Error as PlaywrightError

import codex_usage.browser as browser_module
from codex_usage.browser import (
    _capture_diagnostic_response,
    _capture_json_response,
    _detect_page_state,
    _format_datetime,
    _prepare_profile,
    _profile_lock,
    _safe_page_text_sources,
    _save_probe_payloads,
    diagnose_account,
    fetch_account_usage,
    login_account,
    probe_account,
)
from codex_usage.config import AppConfig
from codex_usage.extractor import JsonCandidate
from codex_usage.models import Account, LimitWindow


@pytest.mark.parametrize("entrypoint", ("fetch", "diagnose"))
@pytest.mark.parametrize(
    "timeout_ms",
    (
        pytest.param(True, id="bool"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(3_600_001, id="above-maximum"),
        pytest.param(1.0, id="float"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
        pytest.param("1000", id="string"),
        pytest.param(10**10_000, id="huge-int"),
    ),
)
def test_browser_entrypoints_reject_invalid_timeout_before_profile_creation(
    tmp_path, monkeypatch, entrypoint, timeout_ms
):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
    )
    config = AppConfig(accounts=(account,))

    def fail_prepare(_account):
        pytest.fail("browser profile must not be prepared")

    monkeypatch.setattr(browser_module, "_prepare_profile", fail_prepare)

    with pytest.raises(ValueError, match="browser timeout is invalid"):
        if entrypoint == "fetch":
            fetch_account_usage(account, config, timeout_ms=timeout_ms)
        else:
            diagnose_account(account, config, timeout_ms=timeout_ms)


@pytest.mark.parametrize("entrypoint", ("login", "fetch", "probe", "diagnose"))
@pytest.mark.parametrize("account", [None, [], "invalid", 1, True, object()])
def test_browser_entrypoints_reject_non_account_input(entrypoint, account):
    config = AppConfig(accounts=())

    with pytest.raises(ValueError, match="account is invalid"):
        if entrypoint == "login":
            login_account(account, config)  # type: ignore[arg-type]
        elif entrypoint == "fetch":
            fetch_account_usage(account, config)  # type: ignore[arg-type]
        elif entrypoint == "probe":
            probe_account(account, config)  # type: ignore[arg-type]
        else:
            diagnose_account(account, config)  # type: ignore[arg-type]


@pytest.mark.parametrize("entrypoint", ("login", "fetch", "probe", "diagnose"))
@pytest.mark.parametrize("config", [None, [], "invalid", 1, True, object()])
def test_browser_entrypoints_reject_non_config_input(entrypoint, config, tmp_path):
    account = Account(id="work", label="Work", profile_dir=str(tmp_path / "profile"))

    with pytest.raises(ValueError, match="config is invalid"):
        if entrypoint == "login":
            login_account(account, config)  # type: ignore[arg-type]
        elif entrypoint == "fetch":
            fetch_account_usage(account, config)  # type: ignore[arg-type]
        elif entrypoint == "probe":
            probe_account(account, config)  # type: ignore[arg-type]
        else:
            diagnose_account(account, config)  # type: ignore[arg-type]


def test_combined_page_text_sources_uses_one_html_evaluation() -> None:
    selectors = []
    evaluations = []

    class FakeLocator:
        def evaluate(self, expression):
            evaluations.append(expression)
            return {"bodyText": "body text", "htmlText": "<html>html</html>"}

    class FakePage:
        def locator(self, selector):
            selectors.append(selector)
            return FakeLocator()

    body_text, sources = _safe_page_text_sources(FakePage())

    assert body_text == "body text"
    assert sources == (
        ("bodyText", "body text"),
        ("htmlText", "<html>html</html>"),
    )
    assert selectors == ["html"]
    assert len(evaluations) == 1
    assert "maxNodes = 1000000" in evaluations[0]
    assert "innerHTML" not in evaluations[0]
    assert "Array.from(node.childNodes" not in evaluations[0]
    assert "Array.from(node.attributes" not in evaluations[0]
    assert "Array.from(document.querySelectorAll" not in evaluations[0]


def test_combined_page_text_sources_caps_and_falls_back_safely() -> None:
    class FakeHtmlLocator:
        def evaluate(self, _expression):
            return {"bodyText": "bad", "htmlText": None}

    class FakeBodyLocator:
        def inner_text(self, *, timeout):
            assert timeout == 10_000
            return "fallback body"

    class FakePage:
        def locator(self, selector):
            return FakeBodyLocator() if selector == "body" else FakeHtmlLocator()

    body_text, sources = _safe_page_text_sources(FakePage())

    assert body_text == "fallback body"
    assert sources == (("bodyText", "fallback body"),)


def test_combined_page_text_sources_caps_each_output() -> None:
    class FakeLocator:
        def evaluate(self, _expression):
            size = browser_module.BROWSER_TEXT_MAX_CHARS + 100
            return {"bodyText": "ä" * size, "htmlText": "ß" * size}

    class FakePage:
        def locator(self, _selector):
            return FakeLocator()

    body_text, sources = _safe_page_text_sources(FakePage())

    assert len(body_text) == browser_module.BROWSER_TEXT_MAX_CHARS
    assert sources == (
        ("bodyText", body_text),
        ("htmlText", "ß" * browser_module.BROWSER_TEXT_MAX_CHARS),
    )


@pytest.mark.parametrize("content_length", ["unknown", "-1", " ", 123])
def test_capture_json_response_rejects_unbounded_body_size(
    content_length: object,
) -> None:
    candidates = []
    read = False

    class FakeResponse:
        url = "https://chatgpt.com/backend-api/wham/usage"
        headers: ClassVar[dict[str, object]] = {"content-type": "application/json"}

        def finished(self):
            return None

        def text(self):
            nonlocal read
            read = True
            return "{}"

    response = FakeResponse()
    if content_length is not None:
        response.headers["content-length"] = content_length
    _capture_json_response(response, candidates)

    assert read is False
    assert candidates == []


@pytest.mark.parametrize("content_length", [None, ""])
def test_capture_json_response_accepts_missing_or_empty_body_size(
    content_length: str | None,
) -> None:
    candidates = []

    class FakeResponse:
        url = "https://chatgpt.com/backend-api/wham/usage"
        headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}

        def finished(self):
            return None

        def text(self):
            return "{}"

    response = FakeResponse()
    if content_length is not None:
        response.headers["content-length"] = content_length
    _capture_json_response(response, candidates)

    assert len(candidates) == 1


def test_capture_json_response_keeps_known_small_body() -> None:
    candidates = []

    class FakeResponse:
        url = "https://chatgpt.com/backend-api/wham/usage"
        headers: ClassVar[dict[str, str]] = {
            "content-type": "application/json",
            "content-length": "2",
        }

        def finished(self):
            return None

        def text(self):
            return "{}"

    _capture_json_response(FakeResponse(), candidates)

    assert len(candidates) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://evilchatgpt.com/backend-api/wham/usage",
        "https://chatgpt.com.evil.example/backend-api/wham/usage",
        "https://notopenai.com/backend-api/wham/usage",
        "https://chatgpt.com@evil.example/backend-api/wham/usage",
        "http://chatgpt.com/backend-api/wham/usage",
        "https://chatgpt.com:8443/backend-api/wham/usage",
    ],
)
def test_capture_json_response_rejects_untrusted_url(url: str) -> None:
    candidates = []
    read = False

    class FakeResponse:
        headers: ClassVar[dict[str, str]] = {
            "content-type": "application/json",
            "content-length": "2",
        }

        def finished(self):
            return None

        def text(self):
            nonlocal read
            read = True
            return "{}"

    response = FakeResponse()
    response.url = url
    _capture_json_response(response, candidates)

    assert read is False
    assert candidates == []


def test_capture_json_response_accepts_openai_subdomain() -> None:
    candidates = []

    class FakeResponse:
        url = "https://api.openai.com/backend-api/wham/usage"
        headers: ClassVar[dict[str, str]] = {
            "content-type": "application/json",
            "content-length": "2",
        }

        def finished(self):
            return None

        def text(self):
            return "{}"

    _capture_json_response(FakeResponse(), candidates)

    assert len(candidates) == 1


def test_capture_json_response_enforces_aggregate_candidate_budget() -> None:
    candidates = []
    body = json.dumps({"value": "x" * 1_100_000})

    class FakeResponse:
        headers: dict[str, str]

        def __init__(self, index: int) -> None:
            self.url = f"https://chatgpt.com/backend-api/wham/usage/{index}"
            self.headers = {
                "content-type": "application/json",
                "content-length": str(len(body.encode("utf-8"))),
            }

        def finished(self):
            return None

        def text(self):
            return body

    for index in range(4):
        _capture_json_response(FakeResponse(index), candidates)

    assert len(candidates) == 3


def test_capture_json_response_updates_external_candidate_byte_budget() -> None:
    candidates = []
    candidate_bytes = [0]
    body = json.dumps({"value": "x" * 100})

    class FakeResponse:
        url = "https://chatgpt.com/backend-api/wham/usage"
        headers: ClassVar[dict[str, str]] = {
            "content-type": "application/json",
            "content-length": str(len(body.encode("utf-8"))),
        }

        def finished(self):
            return None

        def text(self):
            return body

    _capture_json_response(FakeResponse(), candidates, candidate_bytes)

    assert candidate_bytes == [len(json.dumps({"value": "x" * 100}).encode("utf-8"))]


def test_save_probe_payloads_stages_on_save_directory_filesystem(tmp_path, monkeypatch):
    save_dir = tmp_path / "probe"
    save_dir.mkdir()
    seen = {}
    original_temporary_directory = browser_module.tempfile.TemporaryDirectory

    def temporary_directory(*args, **kwargs):
        seen["dir"] = kwargs["dir"]
        return original_temporary_directory(*args, **kwargs)

    monkeypatch.setattr(
        browser_module.tempfile,
        "TemporaryDirectory",
        temporary_directory,
    )

    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    _save_probe_payloads(save_dir, account, [], "visible body")

    assert seen["dir"] == str(save_dir)


def test_save_probe_payloads_serializes_output_transaction(tmp_path, monkeypatch):
    save_dir = tmp_path / "probe"
    lock_events = []

    class FakeLock:
        def __enter__(self):
            lock_events.append("enter")

        def __exit__(self, exc_type, exc_value, traceback):
            lock_events.append("exit")

    def fake_lock(path, **kwargs):
        assert path == save_dir / ".codex-usage-probe-write"
        assert kwargs["label"] == "probe output lock"
        return FakeLock()

    original_write = browser_module._write_bounded_private_text

    def observe_stage_write(path, text, *, label):
        assert lock_events == ["enter"]
        return original_write(path, text, label=label)

    monkeypatch.setattr(browser_module, "private_path_lock", fake_lock)
    monkeypatch.setattr(
        browser_module,
        "_write_bounded_private_text",
        observe_stage_write,
    )

    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    _save_probe_payloads(save_dir, account, [], "visible body")

    assert lock_events == ["enter", "exit"]


def test_save_probe_payloads_rolls_back_when_staging_fails(tmp_path, monkeypatch):
    save_dir = tmp_path / "probe"
    save_dir.mkdir()
    old_files = {
        "privat-01.json": "old one",
        "privat-02.json": "old two",
        "privat-body.txt": "old body",
    }
    for filename, content in old_files.items():
        (save_dir / filename).write_text(content, encoding="utf-8")
    keep = save_dir / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
    )

    original_write = browser_module._write_private_text

    def fail_second(path, text, *, label):
        if path.name == "privat-02.json":
            raise OSError("simulated probe staging failure")
        return original_write(path, text, label=label)

    monkeypatch.setattr(browser_module, "_write_private_text", fail_second)

    candidates = [
        JsonCandidate(url="https://chatgpt.com/one", payload={"value": 1}),
        JsonCandidate(url="https://chatgpt.com/two", payload={"value": 2}),
    ]
    with pytest.raises(OSError, match="simulated probe staging failure"):
        _save_probe_payloads(save_dir, account, candidates, "new body")

    for filename, content in old_files.items():
        assert (save_dir / filename).read_text(encoding="utf-8") == content
    assert keep.read_text(encoding="utf-8") == "keep"


def test_save_probe_payloads_rolls_back_when_commit_fails(tmp_path, monkeypatch):
    save_dir = tmp_path / "probe"
    save_dir.mkdir()
    old_files = {
        "privat-01.json": "old one",
        "privat-body.txt": "old body",
    }
    for filename, content in old_files.items():
        (save_dir / filename).write_text(content, encoding="utf-8")
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
    )
    original_replace = Path.replace

    def fail_body_commit(source, target):
        if source.parent.name == "stage" and target.name == "privat-body.txt":
            raise OSError("simulated probe commit failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_body_commit)

    candidates = [
        JsonCandidate(url="https://chatgpt.com/one", payload={"value": 1}),
    ]
    with pytest.raises(OSError, match="simulated probe commit failure"):
        _save_probe_payloads(save_dir, account, candidates, "new body")

    for filename, content in old_files.items():
        assert (save_dir / filename).read_text(encoding="utf-8") == content


def test_capture_diagnostic_response_bounds_response_count() -> None:
    responses = []

    class FakeResponse:
        status = 200
        headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}

        def __init__(self, index: int) -> None:
            self.url = f"https://chatgpt.com/backend-api/wham/usage/{index}"

    for index in range(101):
        _capture_diagnostic_response(FakeResponse(index), responses)

    assert len(responses) == 100


def test_browser_diagnostic_datetime_uses_dst_aware_local_timezone(monkeypatch):
    berlin = ZoneInfo("Europe/Berlin")
    value = datetime(2026, 1, 15, 0, 15, tzinfo=ZoneInfo("UTC"))

    monkeypatch.setattr("codex_usage.browser.LOCAL_TZ", berlin)

    assert _format_datetime(value) == "2026-01-15T01:15:00+01:00"


def test_diagnose_prioritizes_login_page_over_cloudflare_challenge_assets():
    assert _detect_page_state(
        "https://chatgpt.com/",
        "ChatGPT",
        "Log in to get answers based on saved chats",
        [{"status": 200, "url": "https://chatgpt.com/cdn-cgi/challenge-platform/x"}],
    ) == "login_required"


def test_diagnose_does_not_treat_api_403_as_cloudflare_for_loaded_page():
    analytics_url = "https://chatgpt.com/codex/cloud/settings/analytics"
    api_url = "https://chatgpt.com/backend-api/wham/usage"

    assert (
        _detect_page_state(
            analytics_url,
            "Analytics",
            "5-hour usage limit Weekly usage limit",
            [{"status": 403, "url": api_url}],
            main_status=200,
        )
        == "analytics_page"
    )
    assert (
        _detect_page_state(
            analytics_url,
            "Just a moment...",
            "",
            [{"status": 403, "url": analytics_url}],
            main_status=403,
        )
        == "cloudflare"
    )


def test_prepare_profile_rejects_symlink_root_without_marking_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    profile_link = tmp_path / "profile"
    profile_link.symlink_to(target, target_is_directory=True)
    account = Account(id="privat", label="Privat", profile_dir=str(profile_link))

    with pytest.raises(ValueError, match="profile directory"):
        _prepare_profile(account)

    assert not (target / ".codex-usage-profile").exists()


def test_prepare_profile_rejects_symlink_browser_dir_without_marking_target(tmp_path):
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    target = tmp_path / "firefox-target"
    target.mkdir()
    (profile_root / "firefox").symlink_to(target, target_is_directory=True)
    account = Account(id="privat", label="Privat", profile_dir=str(profile_root))

    with pytest.raises(ValueError, match="browser profile directory"):
        _prepare_profile(account)

    assert not (target / ".codex-usage-browser-profile").exists()


def test_prepare_profile_rejects_symlink_root_marker_without_overwriting_target(tmp_path):
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    target = tmp_path / "outside-marker"
    target.write_text("keep", encoding="utf-8")
    (profile_root / ".codex-usage-profile").symlink_to(target)
    account = Account(id="privat", label="Privat", profile_dir=str(profile_root))

    with pytest.raises(ValueError, match="profile marker path"):
        _prepare_profile(account)

    assert target.read_text(encoding="utf-8") == "keep"


def test_prepare_profile_rejects_symlink_browser_marker_without_overwriting_target(tmp_path):
    browser_dir = tmp_path / "profile" / "firefox"
    browser_dir.mkdir(parents=True)
    target = tmp_path / "outside-browser-marker"
    target.write_text("keep", encoding="utf-8")
    (browser_dir / ".codex-usage-browser-profile").symlink_to(target)
    account = Account(id="privat", label="Privat", profile_dir=str(tmp_path / "profile"))

    with pytest.raises(ValueError, match="browser profile marker path"):
        _prepare_profile(account)

    assert target.read_text(encoding="utf-8") == "keep"


def test_profile_lock_rejects_symlink_lock_without_overwriting_target(tmp_path):
    profile_dir = tmp_path / "profile" / "firefox"
    profile_dir.mkdir(parents=True)
    target = tmp_path / "outside-lock"
    target.write_text("keep", encoding="utf-8")
    (profile_dir / ".codex-usage.lock").symlink_to(target)

    with pytest.raises(ValueError, match="profile lock path"):
        with _profile_lock(profile_dir):
            pass

    assert target.read_text(encoding="utf-8") == "keep"


def test_profile_lock_names_do_not_collide_on_component_periods(tmp_path):
    lock_root = tmp_path / "profiles"
    first = lock_root / "a.b"
    second = lock_root / "a" / "b"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    with _profile_lock(first, lock_root=lock_root):
        pass
    with _profile_lock(second, lock_root=lock_root):
        pass

    assert len(list(lock_root.parent.glob(".*.codex-usage.lock"))) == 2


def test_fetch_closes_context_when_navigation_fails(tmp_path, monkeypatch):
    account = Account(id="privat", label="Privat", profile_dir=str(tmp_path / "profile"))
    context_state = {"closed": False}

    class FakePage:
        def on(self, *_args):
            return None

        def goto(self, *_args, **_kwargs):
            raise PlaywrightError("navigation failed")

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            context_state["closed"] = True

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    context = FakeContext()
    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: context,
    )

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status.value == "error"
    assert context_state["closed"] is True


def test_fetch_cloudflare_clears_values_and_invalidates_cache(tmp_path, monkeypatch):
    analytics_url = "https://chatgpt.com/codex/cloud/settings/analytics"
    account = Account(id="privat", label="Privat", profile_dir=str(tmp_path / "profile"))

    class FakeResponse:
        status = 403

    class FakePage:
        url = analytics_url

        def on(self, *_args):
            return None

        def goto(self, *_args, **_kwargs):
            return FakeResponse()

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def title(self):
            return "Just a moment..."

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    monkeypatch.setattr(
        "codex_usage.browser._safe_page_text_sources",
        lambda _page: ("", ()),
    )

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status.value == "error"
    assert usage.error == "browser page blocked by cloudflare"
    assert usage.cache_invalidated is True
    assert usage.five_hour is None
    assert usage.weekly is None


def test_diagnose_uses_configured_account_auth_json_by_default(tmp_path, monkeypatch):
    auth_path = tmp_path / "accounts" / "privat" / "auth.json"
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
    )
    captured = {}

    def fake_diagnose_auth(path):
        captured["path"] = path
        return {"path": str(path)}

    monkeypatch.setattr(
        "codex_usage.browser._diagnose_auth_json",
        fake_diagnose_auth,
    )
    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, *_args):
            return None

        def goto(self, *_args, **_kwargs):
            return type("Response", (), {"status": 200})()

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    monkeypatch.setattr("codex_usage.browser._safe_body_text", lambda _page: "")
    monkeypatch.setattr("codex_usage.browser._safe_title", lambda _page: "Analytics")
    monkeypatch.setattr(
        "codex_usage.browser._capture_diagnostic_response",
        lambda _response, _responses: None,
    )
    monkeypatch.setattr(
        "codex_usage.browser._detect_page_state",
        lambda *_args, **_kwargs: "ok",
    )
    monkeypatch.setattr(
        "codex_usage.browser._save_diagnostic_screenshot",
        lambda *_args: None,
    )

    result = diagnose_account(account, AppConfig(accounts=(account,)))

    assert result["codex_auth"]["path"] == str(auth_path)
    assert captured["path"] == auth_path


def test_fetch_rejects_ambiguous_browser_identity_from_configured_auth(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )

    class FakeLocator:
        def inner_text(self, *, timeout):
            return "5-hour usage limit 97% Weekly usage limit 55%"

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, *_args):
            return None

        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, *_args):
            return FakeLocator()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    extract_kwargs = {}

    def fake_extract_windows(**kwargs):
        extract_kwargs.update(kwargs)
        return (
            LimitWindow(name="5h", remaining=97),
            LimitWindow(name="weekly", remaining=55),
        )

    monkeypatch.setattr("codex_usage.browser.extract_windows", fake_extract_windows)
    monkeypatch.setattr(
        "codex_usage.browser.backend_identity_from_candidates",
        lambda _candidates: ("user-test", "user-test"),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: ("user-test", "account-uuid"),
    )
    monkeypatch.setattr("codex_usage.browser.auth_plan_type_for_account", lambda _account: None)

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status.value == "error"
    assert usage.error == "backend response has ambiguous account identity"
    assert extract_kwargs["now"] == usage.captured_at


def test_fetch_rejects_shared_user_id_account_alias(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )

    class FakeResponse:
        url = "https://chatgpt.com/backend-api/wham/usage"
        headers: ClassVar[dict[str, str]] = {
            "content-type": "application/json",
            "content-length": "1000",
        }

        def finished(self):
            return None

        def text(self):
            return (
                '{"user_id":"user-test","account_id":"user-test",'
                '"rate_limit":{"primary_window":{"used_percent":3,'
                '"limit_window_seconds":18000},"secondary_window":'
                '{"used_percent":45,"limit_window_seconds":604800}}}'
            )

    class FakeLocator:
        def inner_text(self, *, timeout):
            return "5-hour limit 97% remaining Weekly limit 55% remaining"

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, event, callback):
            if event == "response":
                callback(FakeResponse())

        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, *_args):
            return FakeLocator()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: ("user-test", "account-uuid"),
    )
    monkeypatch.setattr("codex_usage.browser.auth_plan_type_for_account", lambda _account: None)

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status.value == "error"
    assert usage.error == "backend response has ambiguous account identity"


def test_fetch_rejects_limit_values_without_backend_account_id(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )

    class FakeResponse:
        url = "https://chatgpt.com/backend-api/wham/usage"
        headers: ClassVar[dict[str, str]] = {
            "content-type": "application/json",
            "content-length": "1000",
        }

        def finished(self):
            return None

        def text(self):
            return (
                '{"user_id":"user-test",'
                '"rate_limit":{"primary_window":{"used_percent":3,'
                '"limit_window_seconds":18000},"secondary_window":'
                '{"used_percent":45,"limit_window_seconds":604800}}}'
            )

    class FakeLocator:
        def inner_text(self, *, timeout):
            return "5-hour limit 97% remaining Weekly limit 55% remaining"

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, event, callback):
            if event == "response":
                callback(FakeResponse())

        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, *_args):
            return FakeLocator()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: ("user-test", "account-uuid"),
    )
    monkeypatch.setattr("codex_usage.browser.auth_plan_type_for_account", lambda _account: None)

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status.value == "error"
    assert usage.error == "backend response has ambiguous account identity"


def test_fetch_fills_missing_window_from_confirmed_dom_usage(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )

    class FakeResponse:
        def __init__(self):
            self.url = "https://chatgpt.com/backend-api/wham/usage"
            self.headers = {
                "content-type": "application/json",
                "content-length": "1000",
            }

        def finished(self):
            return None

        def text(self):
            return (
                '{"user_id":"user-test","account_id":"account-uuid",'
                '"rate_limit":{"primary_window":{"used_percent":3,'
                '"limit_window_seconds":18000}}}'
            )

    class FakeLocator:
        def inner_text(self, *, timeout):
            return "5-hour limit 97% remaining Weekly limit 55% remaining"

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, event, callback):
            if event == "response":
                callback(FakeResponse())

        def goto(self, *_args, **_kwargs):
            return type("Response", (), {"status": 200})()

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, *_args):
            return FakeLocator()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: ("user-test", "account-uuid"),
    )
    monkeypatch.setattr("codex_usage.browser.auth_plan_type_for_account", lambda _account: None)

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status == "ok"
    assert usage.five_hour is not None and usage.five_hour.remaining == 97
    assert usage.weekly is not None and usage.weekly.remaining == 55
    assert usage.main is not None
    assert usage.main.availability_sources == ("usage", "browser")


def test_fetch_reads_rendered_html_progress_bars(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )

    class FakeLocator:
        def inner_text(self, *, timeout):
            return "5-hour limit Weekly usage limit"

        def evaluate(self, _expression):
            return """
            <html><body>
              <section>
                <h2>5-hour limit</h2>
                <div class="transition-[width]" style="width: 97%;"></div>
              </section>
              <section>
                <h2>Weekly usage limit</h2>
                <div class="transition-[width]" style="width: 55%;"></div>
              </section>
            </body></html>
            """

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, *_args):
            return None

        def goto(self, *_args, **_kwargs):
            return type("Response", (), {"status": 200})()

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, *_args):
            return FakeLocator()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    monkeypatch.setattr(
        "codex_usage.browser.backend_identity_from_candidates",
        lambda _candidates: ("user-test", "account-uuid"),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: ("user-test", "account-uuid"),
    )
    monkeypatch.setattr("codex_usage.browser.auth_plan_type_for_account", lambda _account: None)

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status == "ok"
    assert usage.five_hour is not None and usage.five_hour.remaining == 97
    assert usage.weekly is not None and usage.weekly.remaining == 55


def test_fetch_reports_missing_paid_five_hour_window_from_json(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )

    class FakeResponse:
        url = "https://chatgpt.com/backend-api/wham/usage"
        headers: ClassVar[dict[str, str]] = {
            "content-type": "application/json",
            "content-length": "1000",
        }

        def finished(self):
            return None

        def text(self):
            return (
                '{"user_id":"user-test","account_id":"account-uuid",'
                '"plan_type":"pro","rate_limit":{"primary_window":'
                '{"used_percent":10,"limit_window_seconds":604800,'
                '"reset_at":"2026-07-19T20:59:30+02:00"},'
                '"secondary_window":null}}'
            )

    class FakeLocator:
        def inner_text(self, *, timeout):
            return "Codex analytics page"

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, event, callback):
            if event == "response":
                callback(FakeResponse())

        def goto(self, *_args, **_kwargs):
            return type("Response", (), {"status": 200})()

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, *_args):
            return FakeLocator()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: ("user-test", "account-uuid"),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_plan_type_for_account",
        lambda _account: "pro",
    )

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status == "partial"
    assert usage.five_hour is None
    assert usage.weekly is not None and usage.weekly.remaining == 90
    assert usage.error is None


def test_fetch_rejects_browser_auth_identity_changed_during_request(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )

    class FakeResponse:
        url = "https://chatgpt.com/backend-api/wham/usage"
        headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}

        def finished(self):
            return None

        def text(self):
            return (
                '{"user_id":"old-user","account_id":"old-account",'
                '"rate_limit":{"primary_window":{"used_percent":3,'
                '"limit_window_seconds":18000}}}'
            )

    class FakeLocator:
        def inner_text(self, *, timeout):
            return "5-hour usage limit 97% Weekly usage limit 55%"

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, event, callback):
            if event == "response":
                callback(FakeResponse())

        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, *_args):
            return FakeLocator()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    monkeypatch.setattr(
        "codex_usage.browser.extract_windows",
        lambda **_kwargs: (
            LimitWindow(name="5h", remaining=97),
            LimitWindow(name="weekly", remaining=55),
        ),
    )
    monkeypatch.setattr(
        "codex_usage.browser.backend_identity_from_candidates",
        lambda _candidates: ("old-user", "old-account"),
    )
    identities = iter(
        [("old-user", "old-account"), ("new-user", "new-account")]
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: next(identities),
    )
    monkeypatch.setattr("codex_usage.browser.auth_plan_type_for_account", lambda _account: None)

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status == "login_required"
    assert usage.error == "auth.json identity changed during browser request"
    assert usage.cache_invalidated is True


def test_fetch_accepts_browser_pro_plus_plan_alias_transition(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )

    class FakeLocator:
        def inner_text(self, *, timeout):
            return "5-hour usage limit 97% Weekly usage limit 55%"

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, *_args):
            return None

        def goto(self, *_args, **_kwargs):
            return type("Response", (), {"status": 200})()

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, *_args):
            return FakeLocator()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: ("user-test", "account-uuid"),
    )
    plan_types = iter(("pro", "plus"))
    monkeypatch.setattr(
        "codex_usage.browser.auth_plan_type_for_account",
        lambda _account: next(plan_types),
    )
    monkeypatch.setattr(
        "codex_usage.browser.backend_identity_from_candidates",
        lambda _candidates: ("user-test", "account-uuid"),
    )
    monkeypatch.setattr(
        "codex_usage.browser.extract_windows",
        lambda **_kwargs: (
            LimitWindow(name="5h", remaining=97),
            LimitWindow(name="weekly", remaining=55),
        ),
    )

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status == "ok"
    assert usage.error is None
    assert usage.five_hour is not None and usage.five_hour.remaining == 97
    assert usage.weekly is not None and usage.weekly.remaining == 55


def test_fetch_rejects_browser_values_without_backend_identity(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )

    class FakeLocator:
        def inner_text(self, *, timeout):
            return "5-hour usage limit 97% Weekly usage limit 55%"

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, *_args):
            return None

        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, *_args):
            return FakeLocator()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    monkeypatch.setattr(
        "codex_usage.browser.extract_windows",
        lambda **_kwargs: (
            LimitWindow(name="5h", remaining=97),
            LimitWindow(name="weekly", remaining=55),
        ),
    )
    monkeypatch.setattr(
        "codex_usage.browser.backend_identity_from_candidates",
        lambda _candidates: (None, None),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: ("user-test", "account-uuid"),
    )
    monkeypatch.setattr("codex_usage.browser.auth_plan_type_for_account", lambda _account: None)

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status == "error"
    assert usage.error == "backend response has no account identity"
