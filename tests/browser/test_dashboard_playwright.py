"""Reusable Playwright browser tests for the v0.6 dashboard."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import expect

from tests.helpers.dashboard_lab import PASSWORD, start_dashboard_lab

EVIDENCE = Path("/tmp/backuplint-browser-qa")
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "laptop": {"width": 1280, "height": 720},
    "tablet": {"width": 768, "height": 1024},
    "mobile": {"width": 390, "height": 844},
}


@pytest.fixture(scope="module")
def lab(tmp_path_factory: pytest.TempPathFactory):
    data = tmp_path_factory.mktemp("dash-browser")
    controller, base, ids = start_dashboard_lab(data, populated=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    yield {"controller": controller, "base": base, "ids": ids, "password": PASSWORD}
    controller.close()


@pytest.fixture(scope="module")
def empty_lab(tmp_path_factory: pytest.TempPathFactory):
    data = tmp_path_factory.mktemp("dash-empty")
    controller, base, ids = start_dashboard_lab(data, populated=False)
    yield {"controller": controller, "base": base, "ids": ids, "password": PASSWORD}
    controller.close()


@pytest.fixture
def page(browser, lab):
    context = browser.new_context(
        ignore_https_errors=True,
        viewport=VIEWPORTS["desktop"],
    )
    console: list[str] = []
    failed: list[str] = []

    def on_console(msg) -> None:
        if msg.type in {"error", "warning"}:
            console.append(f"{msg.type}: {msg.text}")

    def on_request_failed(req) -> None:
        failed.append(f"{req.method} {req.url} {req.failure}")

    page = context.new_page()
    page.on("console", on_console)
    page.on("requestfailed", on_request_failed)
    page._bl_console = console  # type: ignore[attr-defined]
    page._bl_failed = failed  # type: ignore[attr-defined]
    yield page
    context.close()


def _login(page, lab, *, password: str | None = None) -> None:
    page.goto(f"{lab['base']}/dashboard/login", wait_until="networkidle")
    page.fill('input[name="password"]', password or lab["password"])
    page.click('button[type="submit"]')
    page.wait_for_url(re.compile(r".*/dashboard/?$"))


def _shot(page, name: str) -> Path:
    path = EVIDENCE / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    return path


def test_login_logout_and_failed_login(page, lab) -> None:
    page.goto(f"{lab['base']}/dashboard/", wait_until="networkidle")
    assert "/dashboard/login" in page.url
    _shot(page, "01-login")

    page.fill('input[name="password"]', "wrong-password-xx")
    page.click('button[type="submit"]')
    expect(page.locator(".err")).to_contain_text("Invalid")
    _shot(page, "02-login-failed")

    page.fill('input[name="password"]', lab["password"])
    page.click('button[type="submit"]')
    page.wait_for_url(re.compile(r".*/dashboard/?$"))
    expect(page.locator("h1")).to_contain_text("Fleet overview")
    cookies = page.context.cookies()
    names = {c["name"] for c in cookies}
    assert "bl_dashboard_session" in names
    session = next(c for c in cookies if c["name"] == "bl_dashboard_session")
    assert session.get("httpOnly") is True
    assert session.get("secure") is True
    assert str(session.get("sameSite", "")).lower() in {"strict", "lax", "none"}

    page.click('button:has-text("Log out")')
    page.wait_for_url(re.compile(r".*/dashboard/login"))


def test_fleet_overview_filters_and_semantics(page, lab) -> None:
    _login(page, lab)
    expect(page.locator(".card .l", has_text="Agents")).to_be_visible()
    expect(page.locator("text=DATA_GAP / overflow")).to_be_visible()
    expect(page.locator("text=Protocol warnings")).to_be_visible()
    expect(page.locator("text=Recent alerts")).to_be_visible()
    expect(page.locator("text=Audit results over time")).to_be_visible()
    expect(page.locator("select[name='time_range']")).to_be_visible()
    _shot(page, "03-fleet-populated")

    # XSS escaped in table (search to ensure the XSS agent is on-page)
    page.fill('input[name="q"]', "script")
    page.click('button:has-text("Filter")')
    page.wait_for_load_state("networkidle")
    html = page.content()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    page.click("a:has-text('Clear')")
    page.wait_for_load_state("networkidle")

    page.fill('input[name="q"]', "OFFLINE")
    page.click('button:has-text("Filter")')
    page.wait_for_load_state("networkidle")
    expect(page.locator("text=OFFLINE old PASS")).to_be_visible()
    body = page.inner_text("main")
    assert "offline" in body.lower()
    assert "PASS" in body
    _shot(page, "04-fleet-offline-filter")

    page.goto(f"{lab['base']}/dashboard/?presence=stale", wait_until="networkidle")
    expect(page.locator(".pill.stale")).to_be_visible()
    _shot(page, "05-fleet-stale")

    page.goto(f"{lab['base']}/dashboard/?audit_status=FAIL", wait_until="networkidle")
    expect(page.locator("text=ONLINE FAIL")).to_be_visible()
    _shot(page, "06-fleet-failures")

    page.goto(f"{lab['base']}/dashboard/?has_data_gap=true", wait_until="networkidle")
    expect(page.locator("a", has_text="DATA_GAP agent")).to_be_visible()
    _shot(page, "07-fleet-data-gap")

    page.goto(
        f"{lab['base']}/dashboard/?protocol_version=1", wait_until="networkidle"
    )
    expect(page.locator("text=Protocol v1")).to_be_visible()

    page.goto(
        f"{lab['base']}/dashboard/?capability=restore_verification",
        wait_until="networkidle",
    )
    assert page.locator("table tbody tr").count() >= 1

    page.goto(f"{lab['base']}/dashboard/?limit=5&offset=0", wait_until="networkidle")
    expect(page.locator("a:has-text('Next')")).to_be_visible()
    page.click("a:has-text('Next')")
    page.wait_for_load_state("networkidle")
    assert "offset=5" in page.url

    page.click("a:has-text('Clear')")
    page.wait_for_load_state("networkidle")
    assert page.url.rstrip("/").endswith("/dashboard")


def test_agent_detail_offline_pass_and_capabilities(page, lab) -> None:
    _login(page, lab)
    offline_id = lab["ids"]["offline_pass"]
    page.goto(f"{lab['base']}/dashboard/agents/{offline_id}", wait_until="networkidle")
    expect(page.locator(".pill.offline")).to_be_visible()
    expect(page.locator(".card .pill.PASS")).to_be_visible()
    expect(page.locator("text=independent of presence")).to_be_visible()
    _shot(page, "08-agent-offline-pass")

    fail_id = lab["ids"]["online_fail"]
    page.goto(f"{lab['base']}/dashboard/agents/{fail_id}", wait_until="networkidle")
    expect(page.locator(".pill.FAIL").first).to_be_visible()
    expect(page.locator("text=Latest by check type")).to_be_visible()
    _shot(page, "09-agent-fail")

    pass_id = lab["ids"]["online_pass"]
    page.goto(f"{lab['base']}/dashboard/agents/{pass_id}", wait_until="networkidle")
    expect(page.locator(".pill.PASS").first).to_be_visible()
    _shot(page, "10-agent-pass")

    caps_id = lab["ids"]["caps_missing"]
    page.goto(f"{lab['base']}/dashboard/agents/{caps_id}", wait_until="networkidle")
    expect(page.locator("text=BINARY_MISSING")).to_be_visible()
    expect(page.locator("text=PLATFORM_UNSUPPORTED")).to_be_visible()
    expect(page.locator("text=TEMPORARILY_UNAVAILABLE")).to_be_visible()
    _shot(page, "11-agent-capabilities")

    nohist = lab["ids"]["no_history"]
    page.goto(f"{lab['base']}/dashboard/agents/{nohist}", wait_until="networkidle")
    expect(page.locator("text=No events")).to_be_visible()


def test_history_filters_and_failures(page, lab) -> None:
    _login(page, lab)
    page.goto(f"{lab['base']}/dashboard/history", wait_until="networkidle")
    expect(page.locator("h1")).to_contain_text("History")
    _shot(page, "12-history")

    page.check('input[name="failures_only"]')
    page.click('button:has-text("Filter")')
    page.wait_for_load_state("networkidle")
    _shot(page, "13-history-failures")

    aid = lab["ids"]["online_pass"]
    page.fill('input[name="agent_id"]', aid)
    page.fill('input[name="event_type"]', "schedule.run")
    page.click('button:has-text("Filter")')
    page.wait_for_load_state("networkidle")
    assert aid in page.content()


def test_empty_fleet(page, empty_lab, browser) -> None:
    context = browser.new_context(ignore_https_errors=True, viewport=VIEWPORTS["desktop"])
    page2 = context.new_page()
    try:
        _login(page2, empty_lab)
        expect(page2.locator("text=No agents")).to_be_visible()
        _shot(page2, "14-fleet-empty")
        page2.goto(f"{empty_lab['base']}/dashboard/history", wait_until="networkidle")
        expect(page2.locator("text=No events")).to_be_visible()
    finally:
        context.close()


def test_unauthorized_api_and_no_secrets(page, lab) -> None:
    resp = page.goto(f"{lab['base']}/v1/dashboard/overview", wait_until="networkidle")
    assert resp is not None
    assert resp.status == 401
    _login(page, lab)
    html = page.content()
    assert "BEGIN PRIVATE KEY" not in html
    assert "client_key" not in html
    assert "password.scrypt" not in html


@pytest.mark.parametrize("name,size", list(VIEWPORTS.items()))
def test_responsive_viewports(browser, lab, name: str, size: dict) -> None:
    context = browser.new_context(ignore_https_errors=True, viewport=size)
    page = context.new_page()
    try:
        _login(page, lab)
        expect(page.locator("a.brand")).to_be_visible()
        expect(page.locator("a:has-text('Fleet')")).to_be_visible()
        expect(page.locator("h1")).to_contain_text("Fleet overview")
        # filters should remain in DOM
        expect(page.locator("#fleet-filters")).to_be_visible()
        _shot(page, f"20-viewport-{name}-fleet")
        aid = lab["ids"]["online_fail"]
        page.goto(f"{lab['base']}/dashboard/agents/{aid}", wait_until="networkidle")
        expect(page.locator(".card .l", has_text="Presence")).to_be_visible()
        _shot(page, f"21-viewport-{name}-agent")
    finally:
        context.close()


def test_console_and_network_clean_on_happy_path(page, lab) -> None:
    _login(page, lab)
    page.goto(f"{lab['base']}/dashboard/", wait_until="networkidle")
    page.goto(
        f"{lab['base']}/dashboard/agents/{lab['ids']['online_pass']}",
        wait_until="networkidle",
    )
    page.goto(f"{lab['base']}/dashboard/history", wait_until="networkidle")
    console = page._bl_console  # type: ignore[attr-defined]
    failed = page._bl_failed  # type: ignore[attr-defined]
    # Persist for the QA report
    (EVIDENCE / "console.json").write_text(
        json.dumps({"console": console, "failed": failed}, indent=2) + "\n",
        encoding="utf-8",
    )
    assert not any("error:" in c.lower() and "favicon" not in c.lower() for c in console)
    assert not failed
