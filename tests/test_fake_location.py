"""tests/test_fake_location.py — Tests for fake location sensor manipulation and presets."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright
from Helpers.fake_location import (
    PRESETS,
    PRESET_CHOICES,
    LocationPreset,
    apply_fake_location_to_context,
    generate_fake_location_script,
    get_playwright_context_options,
    resolve_location_preset,
)


def test_presets_catalog():
    """Verify that all required presets (FR, NL, JP, US) exist and have valid coordinates."""
    required = ["FR", "NL", "JP", "US"]
    for code in required:
        assert code in PRESETS, f"Required preset '{code}' missing from catalog"
        preset = PRESETS[code]
        assert -90.0 <= preset.latitude <= 90.0
        assert -180.0 <= preset.longitude <= 180.0
        assert preset.timezone_id
        assert preset.locale
        assert len(preset.languages) > 0
        assert "Accept-Language" in get_playwright_context_options(preset)["extra_http_headers"]


def test_resolve_preset_case_insensitive_and_aliases():
    """Verify case-insensitivity and alias resolution for presets."""
    # Direct code case-insensitivity
    assert resolve_location_preset("fr").code == "FR"
    assert resolve_location_preset("FR").code == "FR"
    assert resolve_location_preset("nl").code == "NL"
    assert resolve_location_preset("NL").code == "NL"
    assert resolve_location_preset("jp").code == "JP"
    assert resolve_location_preset("JP").code == "JP"
    assert resolve_location_preset("us").code == "US"
    assert resolve_location_preset("US").code == "US"

    # Aliases
    assert resolve_location_preset("France").code == "FR"
    assert resolve_location_preset("paris").code == "FR"
    assert resolve_location_preset("Netherlands").code == "NL"
    assert resolve_location_preset("Amsterdam").code == "NL"
    assert resolve_location_preset("Japan").code == "JP"
    assert resolve_location_preset("Tokyo").code == "JP"
    assert resolve_location_preset("United States").code == "US"
    assert resolve_location_preset("USA").code == "US"


def test_resolve_custom_coordinates():
    """Verify custom lat,lon coordinate resolution."""
    preset = resolve_location_preset("48.8566, 2.3522")
    assert preset.code == "CUSTOM"
    assert abs(preset.latitude - 48.8566) < 1e-4
    assert abs(preset.longitude - 2.3522) < 1e-4

    # Custom with timezone and locale
    preset2 = resolve_location_preset("35.6762, 139.6503, Asia/Tokyo, ja-JP")
    assert preset2.timezone_id == "Asia/Tokyo"
    assert preset2.locale == "ja-JP"
    assert "ja-JP" in preset2.languages


def test_resolve_invalid_presets():
    """Verify informative errors on invalid input."""
    with pytest.raises(ValueError, match="Unknown location preset 'XYZ'"):
        resolve_location_preset("XYZ")

    with pytest.raises(ValueError, match="cannot be empty"):
        resolve_location_preset("   ")

    with pytest.raises(ValueError, match="must be numbers"):
        resolve_location_preset("abc,def")


def test_script_generation():
    """Verify that generate_fake_location_script outputs valid JavaScript containing preset values."""
    preset = PRESETS["FR"]
    script = generate_fake_location_script(preset)

    assert str(preset.latitude) in script
    assert str(preset.longitude) in script
    assert "fakeGeolocation" in script
    assert "getCurrentPosition" in script
    assert "watchPosition" in script
    assert "clearWatch" in script
    assert "geolocation" in script
    assert preset.locale in script


@pytest.mark.asyncio
async def test_playwright_e2e_france_preset():
    """End-to-end test verifying France (FR) location sensor spoofing in Chromium."""
    preset = PRESETS["FR"]
    context_options = get_playwright_context_options(preset)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(**context_options)
        await apply_fake_location_to_context(context, preset)

        page = await context.new_page()
        res = await page.evaluate(r"""() => {
            return new Promise(async (resolve) => {
                const perm = await navigator.permissions.query({ name: 'geolocation' });
                navigator.geolocation.getCurrentPosition(
                    (pos) => {
                        resolve({
                            latitude: pos.coords.latitude,
                            longitude: pos.coords.longitude,
                            accuracy: pos.coords.accuracy,
                            permState: perm.state,
                            timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                            locale: Intl.DateTimeFormat().resolvedOptions().locale,
                            language: navigator.language,
                            languages: navigator.languages,
                        });
                    },
                    (err) => resolve({ error: err.message })
                );
            });
        }""")

        await browser.close()

    assert "error" not in res, f"Geolocation error occurred: {res.get('error')}"
    assert abs(res["latitude"] - preset.latitude) < 0.01
    assert abs(res["longitude"] - preset.longitude) < 0.01
    assert res["permState"] == "granted"
    assert res["timeZone"] == "Europe/Paris"
    assert res["locale"] == "fr-FR"
    assert res["language"] == "fr-FR"
    assert "fr-FR" in res["languages"]


@pytest.mark.asyncio
async def test_playwright_e2e_japan_preset():
    """End-to-end test verifying Japan (JP) location sensor spoofing in Chromium."""
    preset = PRESETS["JP"]
    context_options = get_playwright_context_options(preset)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(**context_options)
        await apply_fake_location_to_context(context, preset)

        page = await context.new_page()
        res = await page.evaluate(r"""() => {
            return new Promise((resolve) => {
                navigator.geolocation.getCurrentPosition((pos) => {
                    resolve({
                        latitude: pos.coords.latitude,
                        longitude: pos.coords.longitude,
                        timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                        language: navigator.language,
                    });
                });
            });
        }""")
        await browser.close()

    assert abs(res["latitude"] - preset.latitude) < 0.01
    assert abs(res["longitude"] - preset.longitude) < 0.01
    assert res["timeZone"] == "Asia/Tokyo"
    assert res["language"] == "ja-JP"


@pytest.mark.asyncio
async def test_playwright_watch_position_and_clear():
    """Verify that navigator.geolocation.watchPosition and clearWatch work properly."""
    preset = PRESETS["NL"]
    context_options = get_playwright_context_options(preset)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(**context_options)
        await apply_fake_location_to_context(context, preset)

        page = await context.new_page()
        res = await page.evaluate(r"""() => {
            return new Promise((resolve) => {
                let callCount = 0;
                const id = navigator.geolocation.watchPosition((pos) => {
                    callCount++;
                    if (callCount === 1) {
                        navigator.geolocation.clearWatch(id);
                        resolve({
                            watchId: id,
                            latitude: pos.coords.latitude,
                            longitude: pos.coords.longitude,
                        });
                    }
                });
            });
        }""")
        await browser.close()

    assert res["watchId"] >= 1
    assert abs(res["latitude"] - preset.latitude) < 0.01
    assert abs(res["longitude"] - preset.longitude) < 0.01
