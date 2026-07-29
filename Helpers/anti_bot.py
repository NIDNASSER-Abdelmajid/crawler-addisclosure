"""Anti-bot initialization script for Playwright pages.

This module supplies a JavaScript snippet that is injected into every new
page via ``page.add_init_script``. The script patches a variety of
``navigator`` and ``window`` properties so that headless Chromium looks
more like a real Chrome browser. It is based on
duckduckgo/tracker-radar-collector/helpers/notABot.js with a few extra
fields (``hardwareConcurrency``/``deviceMemory``) and simplified behavior.
"""

from __future__ import annotations


def anti_bot_script() -> str:
    """Return the JS code that performs anti-bot patches.

    The returned string can be passed directly to
    ``page.add_init_script``.
    """

    return r"""
// tweaks taken from DDG tracker-radar-collector/helpers/notABot.js

if (window.Notification && Notification.permission === 'denied') {
    Reflect.defineProperty(window.Notification, 'permission', { get: () => 'default' });
}

if (window.Navigator) {
    Reflect.defineProperty(window.Navigator.prototype, 'webdriver', { get: () => undefined });
    Reflect.defineProperty(window.Navigator.prototype, 'languages', { get: () => ['en-US', 'en'] });
    Reflect.defineProperty(window.Navigator.prototype, 'hardwareConcurrency', { get: () => 8 });
    Reflect.defineProperty(window.Navigator.prototype, 'deviceMemory', { get: () => 8 });
    Reflect.defineProperty(window.Navigator.prototype, 'platform', { get: () => 'Win32' });
    Reflect.defineProperty(window.Navigator.prototype, 'vendor', { get: () => 'Google Inc.' });
    Reflect.defineProperty(window.Navigator.prototype, 'userAgent', {
        get: () => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36'
    });

    if (!window.Navigator.prototype.userAgentData) {
        Reflect.defineProperty(window.Navigator.prototype, 'userAgentData', {
            get: () => ({
                brands: [
                    { brand: 'Google Chrome', version: '133' },
                    { brand: 'Chromium', version: '133' },
                    { brand: 'Not=A?Brand', version: '24' },
                ],
                mobile: false,
                platform: 'Windows',
                getHighEntropyValues: async () => ({
                    architecture: 'x86',
                    bitness: '64',
                    mobile: false,
                    model: '',
                    platform: 'Windows',
                    platformVersion: '10.0.0',
                    uaFullVersion: '133.0.0.0',
                    fullVersionList: [
                        { brand: 'Google Chrome', version: '133.0.0.0' },
                        { brand: 'Chromium', version: '133.0.0.0' },
                        { brand: 'Not=A?Brand', version: '24.0.0.0' },
                    ],
                    wow64: false,
                }),
                toJSON: () => ({
                    brands: [
                        { brand: 'Google Chrome', version: '133' },
                        { brand: 'Chromium', version: '133' },
                        { brand: 'Not=A?Brand', version: '24' },
                    ],
                    mobile: false,
                    platform: 'Windows',
                }),
            }),
        });
    }

    if (window.navigator.plugins.length === 0) {
        Reflect.defineProperty(window.Navigator.prototype, 'plugins', {
            get: () => [{
                description: 'Portable Document Format',
                filename: 'internal-pdf-viewer',
                name: 'Chrome PDF Plugin',
                0: { type: 'application/pdf' },
            }],
        });
    }
}

if (!window.chrome || !window.chrome.runtime) {
    window.chrome = {
        app: {
            isInstalled: false,
            InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
            RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
        },
        runtime: {
            OnInstalledReason: {
                CHROME_UPDATE: 'chrome_update', INSTALL: 'install',
                SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update',
            },
            OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
            PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
            PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
            PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
            RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' },
        },
        csi: function() {},
        loadTimes: function() {},
    };
}
"""