"""Helpers/fake_location.py — Location sensor manipulation and geo simulation.

Simulates geographic location, GPS sensors, timezone, and language locales
without requiring a VPN. Provides preset options for common jurisdictions
(e.g., FR, NL, JP, US) and custom coordinate support.

Architecture:
1. Native Chromium Context: Configures Playwright's persistent context with
   geolocation coordinates, granted permissions, timezone ID, locale, and
   HTTP Accept-Language headers.
2. In-Page Sensor Manipulation: Injects a comprehensive init script into every
   page/frame that patches:
   - ``navigator.geolocation.getCurrentPosition`` and ``watchPosition``
   - ``navigator.permissions.query({ name: 'geolocation' })`` (returns 'granted')
   - ``navigator.language`` and ``navigator.languages``
   - W3C Generic Sensor API (``GeolocationSensor``)
   - Device orientation permission handlers
   - Realistic GPS micro-jitter to evade anti-bot/fraud heuristics.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class LocationPreset:
    """Immutable location preset configuration."""

    code: str
    country_name: str
    city: str
    latitude: float
    longitude: float
    accuracy: float = 15.0
    altitude: float | None = 35.0
    altitude_accuracy: float | None = 5.0
    heading: float | None = None
    speed: float | None = None
    timezone_id: str = "UTC"
    locale: str = "en-US"
    languages: tuple[str, ...] = ("en-US", "en")
    accept_language: str = "en-US,en;q=0.9"

    def to_dict(self) -> dict[str, Any]:
        """Convert preset to a serializable dictionary."""
        d = asdict(self)
        d["languages"] = list(self.languages)
        return d


# Presets catalog with accurate coordinates, timezones, locales, and languages
PRESETS: dict[str, LocationPreset] = {
    "FR": LocationPreset(
        code="FR",
        country_name="France",
        city="Paris",
        latitude=48.8566,
        longitude=2.3522,
        accuracy=15.0,
        altitude=35.0,
        altitude_accuracy=5.0,
        timezone_id="Europe/Paris",
        locale="fr-FR",
        languages=("fr-FR", "fr", "en-US", "en"),
        accept_language="fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    ),
    "NL": LocationPreset(
        code="NL",
        country_name="Netherlands",
        city="Amsterdam",
        latitude=52.3676,
        longitude=4.9041,
        accuracy=12.0,
        altitude=2.0,
        altitude_accuracy=3.0,
        timezone_id="Europe/Amsterdam",
        locale="nl-NL",
        languages=("nl-NL", "nl", "en-US", "en"),
        accept_language="nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    ),
    "JP": LocationPreset(
        code="JP",
        country_name="Japan",
        city="Tokyo",
        latitude=35.6762,
        longitude=139.6503,
        accuracy=10.0,
        altitude=40.0,
        altitude_accuracy=4.0,
        timezone_id="Asia/Tokyo",
        locale="ja-JP",
        languages=("ja-JP", "ja", "en-US", "en"),
        accept_language="ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    ),
    "US": LocationPreset(
        code="US",
        country_name="United States",
        city="New York",
        latitude=40.7128,
        longitude=-74.0060,
        accuracy=15.0,
        altitude=10.0,
        altitude_accuracy=5.0,
        timezone_id="America/New_York",
        locale="en-US",
        languages=("en-US", "en"),
        accept_language="en-US,en;q=0.9",
    ),
    "DE": LocationPreset(
        code="DE",
        country_name="Germany",
        city="Berlin",
        latitude=52.5200,
        longitude=13.4050,
        accuracy=15.0,
        altitude=34.0,
        altitude_accuracy=5.0,
        timezone_id="Europe/Berlin",
        locale="de-DE",
        languages=("de-DE", "de", "en-US", "en"),
        accept_language="de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    ),
    "UK": LocationPreset(
        code="UK",
        country_name="United Kingdom",
        city="London",
        latitude=51.5074,
        longitude=-0.1278,
        accuracy=14.0,
        altitude=25.0,
        altitude_accuracy=5.0,
        timezone_id="Europe/London",
        locale="en-GB",
        languages=("en-GB", "en", "en-US"),
        accept_language="en-GB,en;q=0.9,en-US;q=0.8",
    ),
    "GB": LocationPreset(
        code="GB",
        country_name="United Kingdom",
        city="London",
        latitude=51.5074,
        longitude=-0.1278,
        accuracy=14.0,
        altitude=25.0,
        altitude_accuracy=5.0,
        timezone_id="Europe/London",
        locale="en-GB",
        languages=("en-GB", "en", "en-US"),
        accept_language="en-GB,en;q=0.9,en-US;q=0.8",
    ),
    "CA": LocationPreset(
        code="CA",
        country_name="Canada",
        city="Toronto",
        latitude=43.6532,
        longitude=-79.3832,
        accuracy=15.0,
        altitude=76.0,
        altitude_accuracy=6.0,
        timezone_id="America/Toronto",
        locale="en-CA",
        languages=("en-CA", "en", "fr-CA", "fr"),
        accept_language="en-CA,en;q=0.9,fr-CA;q=0.8,en;q=0.7",
    ),
    "AU": LocationPreset(
        code="AU",
        country_name="Australia",
        city="Sydney",
        latitude=-33.8688,
        longitude=151.2093,
        accuracy=15.0,
        altitude=19.0,
        altitude_accuracy=5.0,
        timezone_id="Australia/Sydney",
        locale="en-AU",
        languages=("en-AU", "en"),
        accept_language="en-AU,en;q=0.9,en-US;q=0.8",
    ),
    "ES": LocationPreset(
        code="ES",
        country_name="Spain",
        city="Madrid",
        latitude=40.4168,
        longitude=-3.7038,
        accuracy=15.0,
        altitude=650.0,
        altitude_accuracy=10.0,
        timezone_id="Europe/Madrid",
        locale="es-ES",
        languages=("es-ES", "es", "en-US", "en"),
        accept_language="es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
    ),
    "IT": LocationPreset(
        code="IT",
        country_name="Italy",
        city="Rome",
        latitude=41.9028,
        longitude=12.4964,
        accuracy=15.0,
        altitude=21.0,
        altitude_accuracy=5.0,
        timezone_id="Europe/Rome",
        locale="it-IT",
        languages=("it-IT", "it", "en-US", "en"),
        accept_language="it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    ),
    "BR": LocationPreset(
        code="BR",
        country_name="Brazil",
        city="São Paulo",
        latitude=-23.5505,
        longitude=-46.6333,
        accuracy=20.0,
        altitude=760.0,
        altitude_accuracy=10.0,
        timezone_id="America/Sao_Paulo",
        locale="pt-BR",
        languages=("pt-BR", "pt", "en-US", "en"),
        accept_language="pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    ),
}

# Alias mapping for country names and common terms
_ALIASES: dict[str, str] = {
    "FRANCE": "FR",
    "PARIS": "FR",
    "NETHERLANDS": "NL",
    "HOLLAND": "NL",
    "AMSTERDAM": "NL",
    "JAPAN": "JP",
    "TOKYO": "JP",
    "UNITED STATES": "US",
    "USA": "US",
    "AMERICA": "US",
    "NEW YORK": "US",
    "GERMANY": "DE",
    "BERLIN": "DE",
    "UNITED KINGDOM": "UK",
    "GREAT BRITAIN": "UK",
    "BRITAIN": "UK",
    "ENGLAND": "UK",
    "LONDON": "UK",
    "CANADA": "CA",
    "TORONTO": "CA",
    "AUSTRALIA": "AU",
    "SYDNEY": "AU",
    "SPAIN": "ES",
    "MADRID": "ES",
    "ITALY": "IT",
    "ROME": "IT",
    "BRAZIL": "BR",
    "SAO PAULO": "BR",
}

PRESET_CHOICES: list[str] = sorted(list(PRESETS.keys()))


def resolve_location_preset(value: str | LocationPreset | Mapping[str, Any] | None) -> LocationPreset:
    """Resolve a user input into a validated LocationPreset.

    Accepts:
    - An existing LocationPreset instance
    - A dictionary of preset attributes
    - A preset code or alias (e.g. 'FR', 'nl', 'Japan', 'US')
    - Custom coordinates formatted as 'lat,lon' (e.g. '48.8566,2.3522')
    - Custom coordinates with timezone & locale: 'lat,lon,timezone,locale'

    Raises:
        ValueError: If the input cannot be resolved to a valid preset.
    """
    if value is None:
        raise ValueError("Location preset value cannot be None.")

    if isinstance(value, LocationPreset):
        return value

    if isinstance(value, Mapping):
        return LocationPreset(
            code=str(value.get("code", "CUSTOM")).upper(),
            country_name=str(value.get("country_name", "Custom Location")),
            city=str(value.get("city", "Custom City")),
            latitude=float(value["latitude"]),
            longitude=float(value["longitude"]),
            accuracy=float(value.get("accuracy", 15.0)),
            altitude=float(value["altitude"]) if value.get("altitude") is not None else None,
            altitude_accuracy=float(value["altitude_accuracy"]) if value.get("altitude_accuracy") is not None else None,
            heading=float(value["heading"]) if value.get("heading") is not None else None,
            speed=float(value["speed"]) if value.get("speed") is not None else None,
            timezone_id=str(value.get("timezone_id", "UTC")),
            locale=str(value.get("locale", "en-US")),
            languages=tuple(value.get("languages", ("en-US", "en"))),
            accept_language=str(value.get("accept_language", "en-US,en;q=0.9")),
        )

    val_str = str(value).strip()
    if not val_str:
        raise ValueError("Location preset string cannot be empty.")

    # 1. Direct code lookup (e.g. 'FR', 'NL')
    code_upper = val_str.upper()
    if code_upper in PRESETS:
        return PRESETS[code_upper]

    # 2. Alias lookup (e.g. 'France', 'japan')
    if code_upper in _ALIASES:
        return PRESETS[_ALIASES[code_upper]]

    # 3. Custom coordinate parsing: 'lat,lon' or 'lat,lon,timezone,locale'
    if "," in val_str:
        parts = [p.strip() for p in val_str.split(",")]
        if len(parts) >= 2:
            try:
                lat = float(parts[0])
                lon = float(parts[1])
                tz = parts[2] if len(parts) > 2 and parts[2] else "UTC"
                loc = parts[3] if len(parts) > 3 and parts[3] else "en-US"
                lang_code = loc.split("-")[0] if "-" in loc else loc
                return LocationPreset(
                    code="CUSTOM",
                    country_name="Custom",
                    city=f"{lat:.2f},{lon:.2f}",
                    latitude=lat,
                    longitude=lon,
                    accuracy=15.0,
                    timezone_id=tz,
                    locale=loc,
                    languages=(loc, lang_code, "en-US", "en"),
                    accept_language=f"{loc},{lang_code};q=0.9,en-US;q=0.8,en;q=0.7",
                )
            except ValueError as exc:
                raise ValueError(
                    f"Invalid custom coordinates '{val_str}': latitude and longitude must be numbers."
                ) from exc

    available = ", ".join(PRESET_CHOICES)
    raise ValueError(
        f"Unknown location preset '{val_str}'. Available presets: {available}, "
        "or pass custom coordinates as 'lat,lon'."
    )


def generate_fake_location_script(preset: LocationPreset, enable_jitter: bool = True) -> str:
    """Generate the in-page JavaScript sensor manipulation and spoofing script.

    This script hooks browser APIs at the prototype level to ensure all calls
    from any frame, inline script, or external SDK consistently see the fake location.
    """
    lat_val = float(preset.latitude)
    lon_val = float(preset.longitude)
    acc_val = float(preset.accuracy)
    alt_val = json.dumps(preset.altitude)
    alt_acc_val = json.dumps(preset.altitude_accuracy)
    head_val = json.dumps(preset.heading)
    spd_val = json.dumps(preset.speed)
    langs_val = json.dumps(list(preset.languages))
    lang_val = json.dumps(preset.locale)
    jitter_val = "true" if enable_jitter else "false"

    return f"""
(() => {{
    // Guard against multiple injections
    if (window.__fakeLocationActive__) return;
    window.__fakeLocationActive__ = true;

    const TARGET_LAT = {lat_val};
    const TARGET_LON = {lon_val};
    const TARGET_ACCURACY = {acc_val};
    const TARGET_ALTITUDE = {alt_val};
    const TARGET_ALTITUDE_ACCURACY = {alt_acc_val};
    const TARGET_HEADING = {head_val};
    const TARGET_SPEED = {spd_val};
    const TARGET_LANGUAGES = {langs_val};
    const TARGET_LANGUAGE = {lang_val};
    const ENABLE_JITTER = {jitter_val};

    // Realistic GPS micro-jitter (±0.00002 deg is approximately 2.2 meters)
    function getJitteredCoords() {{
        const jitterLat = ENABLE_JITTER ? (Math.random() - 0.5) * 0.00004 : 0;
        const jitterLon = ENABLE_JITTER ? (Math.random() - 0.5) * 0.00004 : 0;
        const jitterAcc = ENABLE_JITTER ? (Math.random() * 2 - 1) : 0;

        return {{
            latitude: TARGET_LAT + jitterLat,
            longitude: TARGET_LON + jitterLon,
            accuracy: Math.max(1.0, TARGET_ACCURACY + jitterAcc),
            altitude: TARGET_ALTITUDE !== null ? TARGET_ALTITUDE + (ENABLE_JITTER ? (Math.random() * 0.4 - 0.2) : 0) : null,
            altitudeAccuracy: TARGET_ALTITUDE_ACCURACY,
            heading: TARGET_HEADING,
            speed: TARGET_SPEED
        }};
    }}

    function createPositionObject() {{
        const coords = getJitteredCoords();
        return {{
            coords: coords,
            timestamp: Date.now()
        }};
    }}

    // ========================================================
    // 1. Geolocation API Manipulation
    // ========================================================
    const activeWatches = new Map();
    let nextWatchId = 1;

    const fakeGeolocation = {{
        getCurrentPosition: function(successCallback, errorCallback, options) {{
            if (typeof successCallback !== 'function') return;
            // Realistic sensor response delay (20ms - 80ms)
            const delay = Math.floor(Math.random() * 60) + 20;
            setTimeout(() => {{
                try {{
                    successCallback(createPositionObject());
                }} catch (err) {{
                    console.debug('[FakeLocation] Error in getCurrentPosition callback:', err);
                }}
            }}, delay);
        }},

        watchPosition: function(successCallback, errorCallback, options) {{
            if (typeof successCallback !== 'function') return 0;
            const watchId = nextWatchId++;

            // Initial position callback
            setTimeout(() => {{
                try {{
                    successCallback(createPositionObject());
                }} catch (err) {{}}
            }}, 25);

            // Periodic position update simulating sensor events
            const intervalId = setInterval(() => {{
                try {{
                    successCallback(createPositionObject());
                }} catch (err) {{}}
            }}, 4000);

            activeWatches.set(watchId, intervalId);
            return watchId;
        }},

        clearWatch: function(watchId) {{
            if (activeWatches.has(watchId)) {{
                clearInterval(activeWatches.get(watchId));
                activeWatches.delete(watchId);
            }}
        }}
    }};

    // Preserve native-like string representations
    try {{
        fakeGeolocation.getCurrentPosition.toString = () => 'function getCurrentPosition() {{ [native code] }}';
        fakeGeolocation.watchPosition.toString = () => 'function watchPosition() {{ [native code] }}';
        fakeGeolocation.clearWatch.toString = () => 'function clearWatch() {{ [native code] }}';
    }} catch (_) {{}}

    // Apply to Navigator prototype
    try {{
        if (typeof Navigator !== 'undefined' && Navigator.prototype) {{
            Object.defineProperty(Navigator.prototype, 'geolocation', {{
                get: () => fakeGeolocation,
                configurable: true,
                enumerable: true
            }});
        }}
    }} catch (_) {{}}

    try {{
        if (navigator) {{
            Object.defineProperty(navigator, 'geolocation', {{
                get: () => fakeGeolocation,
                configurable: true,
                enumerable: true
            }});
        }}
    }} catch (_) {{}}

    // ========================================================
    // 2. Permissions API Spoofing (report 'granted' for geolocation)
    // ========================================================
    if (typeof navigator !== 'undefined' && navigator.permissions && typeof navigator.permissions.query === 'function') {{
        const originalQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = function(param) {{
            if (param && param.name === 'geolocation') {{
                const status = {{
                    state: 'granted',
                    name: 'geolocation',
                    onchange: null,
                    addEventListener: function() {{}},
                    removeEventListener: function() {{}},
                    dispatchEvent: function() {{ return true; }}
                }};
                return Promise.resolve(status);
            }}
            return originalQuery(param);
        }};
        try {{
            navigator.permissions.query.toString = () => 'function query() {{ [native code] }}';
        }} catch (_) {{}}
    }}

    // ========================================================
    // 3. Locale & Language Sensors
    // ========================================================
    try {{
        if (typeof Navigator !== 'undefined' && Navigator.prototype) {{
            Object.defineProperty(Navigator.prototype, 'language', {{
                get: () => TARGET_LANGUAGE,
                configurable: true,
                enumerable: true
            }});
            Object.defineProperty(Navigator.prototype, 'languages', {{
                get: () => TARGET_LANGUAGES,
                configurable: true,
                enumerable: true
            }});
        }}
    }} catch (_) {{}}

    try {{
        if (navigator) {{
            Object.defineProperty(navigator, 'language', {{
                get: () => TARGET_LANGUAGE,
                configurable: true,
                enumerable: true
            }});
            Object.defineProperty(navigator, 'languages', {{
                get: () => TARGET_LANGUAGES,
                configurable: true,
                enumerable: true
            }});
        }}
    }} catch (_) {{}}

    // ========================================================
    // 4. W3C Generic Sensor API (GeolocationSensor mock)
    // ========================================================
    if (typeof window !== 'undefined') {{
        try {{
            class MockGeolocationSensor {{
                constructor(options = {{}}) {{
                    this.latitude = TARGET_LAT;
                    this.longitude = TARGET_LON;
                    this.accuracy = TARGET_ACCURACY;
                    this.altitude = TARGET_ALTITUDE;
                    this.altitudeAccuracy = TARGET_ALTITUDE_ACCURACY;
                    this.heading = TARGET_HEADING;
                    this.speed = TARGET_SPEED;
                    this.timestamp = Date.now();
                    this.activated = false;
                    this.hasReading = false;
                }}
                start() {{
                    this.activated = true;
                    this.hasReading = true;
                    if (typeof this.onreading === 'function') {{
                        this.onreading();
                    }}
                }}
                stop() {{
                    this.activated = false;
                }}
                addEventListener(type, listener) {{
                    if (type === 'reading') {{
                        setTimeout(() => listener({{ target: this }}), 40);
                    }}
                }}
                removeEventListener() {{}}
            }}
            window.GeolocationSensor = MockGeolocationSensor;
        }} catch (_) {{}}
    }}

    // ========================================================
    // 5. Device Orientation Permission Handlers
    // ========================================================
    if (typeof window !== 'undefined') {{
        if (window.DeviceOrientationEvent && typeof window.DeviceOrientationEvent.requestPermission === 'function') {{
            window.DeviceOrientationEvent.requestPermission = async () => 'granted';
        }}
        if (window.DeviceMotionEvent && typeof window.DeviceMotionEvent.requestPermission === 'function') {{
            window.DeviceMotionEvent.requestPermission = async () => 'granted';
        }}
    }}
}})();
"""


def get_playwright_context_options(preset: LocationPreset) -> dict[str, Any]:
    """Return dictionary of options to merge into Playwright context creation kwargs.

    Configures:
    - geolocation coordinates
    - granted permissions for geolocation
    - timezone ID
    - locale
    - Accept-Language HTTP headers
    """
    return {
        "geolocation": {
            "latitude": preset.latitude,
            "longitude": preset.longitude,
            "accuracy": preset.accuracy,
        },
        "permissions": ["geolocation"],
        "timezone_id": preset.timezone_id,
        "locale": preset.locale,
        "extra_http_headers": {
            "Accept-Language": preset.accept_language,
        },
    }


async def apply_fake_location_to_context(context: Any, preset: LocationPreset) -> None:
    """Apply fake location sensor settings to an active Playwright BrowserContext.

    Configures geolocation coordinates, permissions, HTTP headers, and registers
    the in-page sensor manipulation script for all future pages and child frames.
    """
    try:
        await context.grant_permissions(["geolocation"])
    except Exception:
        pass

    try:
        await context.set_geolocation({
            "latitude": preset.latitude,
            "longitude": preset.longitude,
            "accuracy": preset.accuracy,
        })
    except Exception:
        pass

    try:
        await context.set_extra_http_headers({
            "Accept-Language": preset.accept_language,
        })
    except Exception:
        pass

    try:
        script = generate_fake_location_script(preset)
        await context.add_init_script(script)
    except Exception:
        pass
