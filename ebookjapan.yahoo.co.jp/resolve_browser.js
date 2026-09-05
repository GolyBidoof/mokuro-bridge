/**
 * Resolve a Chromium binary for Puppeteer automation.
 *
 * Prefer Puppeteer's bundled "Chrome for Testing" so we never fight Arc/Chrome
 * single-instance locks on the user's daily browser.
 */
const fs = require('fs');
const path = require('path');
const os = require('os');

const APP_CANDIDATES = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    // Arc last — only one Arc instance can run system-wide
    '/Applications/Arc.app/Contents/MacOS/Arc',
];

function bundledChromePath() {
    const cacheRoots = [
        process.env.PUPPETEER_CACHE_DIR,
        path.join(os.homedir(), '.cache', 'puppeteer'),
    ].filter(Boolean);

    try {
        // Prefer puppeteer from this package's node_modules
        const puppeteer = require('puppeteer');
        // Point cache at a durable home dir when Cursor sandboxes temp caches
        if (!process.env.PUPPETEER_CACHE_DIR && fs.existsSync(path.join(os.homedir(), '.cache', 'puppeteer'))) {
            process.env.PUPPETEER_CACHE_DIR = path.join(os.homedir(), '.cache', 'puppeteer');
        }
        const p = puppeteer.executablePath();
        if (p && fs.existsSync(p)) {
            // Reject broken sandbox-cache installs missing Frameworks
            const fw = path.join(path.dirname(p), '..', 'Frameworks');
            if (fs.existsSync(fw)) return p;
        }
    } catch (e) {
        /* not installed / not downloaded */
    }

    // Fall back: scan ~/.cache/puppeteer for Chrome for Testing
    for (const root of cacheRoots) {
        try {
            const chromeRoot = path.join(root, 'chrome');
            if (!fs.existsSync(chromeRoot)) continue;
            const versions = fs.readdirSync(chromeRoot).sort().reverse();
            for (const ver of versions) {
                const candidate = path.join(
                    chromeRoot,
                    ver,
                    'chrome-mac-arm64',
                    'Google Chrome for Testing.app',
                    'Contents',
                    'MacOS',
                    'Google Chrome for Testing'
                );
                const fw = path.join(path.dirname(candidate), '..', 'Frameworks');
                if (fs.existsSync(candidate) && fs.existsSync(fw)) return candidate;
            }
        } catch (e) { /* ignore */ }
    }
    return null;
}

function resolveBrowserExecutable() {
    if (process.env.PUPPETEER_EXECUTABLE_PATH) {
        const forced = process.env.PUPPETEER_EXECUTABLE_PATH;
        if (!fs.existsSync(forced)) {
            throw new Error(`PUPPETEER_EXECUTABLE_PATH not found: ${forced}`);
        }
        return forced;
    }

    const bundled = bundledChromePath();
    if (bundled) return bundled;

    for (const candidate of APP_CANDIDATES) {
        if (fs.existsSync(candidate)) return candidate;
    }

    throw new Error(
        'No Chromium browser found. Run: npx puppeteer browsers install chrome\n' +
            'Or set PUPPETEER_EXECUTABLE_PATH to a Chrome/Edge binary.'
    );
}

function browserLabel(executablePath) {
    if (!executablePath) return '?';
    if (executablePath.includes('Chrome for Testing')) return 'Chrome for Testing';
    if (executablePath.includes('chrome-mac') || executablePath.includes('chrome/mac')) {
        return 'Chrome for Testing';
    }
    if (executablePath.includes('Arc.app')) return 'Arc';
    if (executablePath.includes('Google Chrome')) return 'Chrome';
    if (executablePath.includes('Microsoft Edge')) return 'Edge';
    if (executablePath.includes('Brave')) return 'Brave';
    if (executablePath.includes('Chromium')) return 'Chromium';
    return path.basename(executablePath);
}

function isArc(executablePath) {
    return String(executablePath || '').includes('Arc.app');
}

/**
 * Puppeteer launch options with an isolated profile so we don't attach to
 * (or collide with) the user's open Arc/Chrome window.
 */
function launchOptions(extra = {}) {
    const executablePath = resolveBrowserExecutable();
    const profileDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ej-puppeteer-'));
    const args = [
        '--disable-web-security',
        '--disable-features=IsolateOrigins,site-per-process',
        '--disable-blink-features=AutomationControlled',
        '--no-first-run',
        '--no-default-browser-check',
        ...(extra.args || []),
    ];

    if (isArc(executablePath)) {
        console.warn(
            '[Browser] Using Arc — close other Arc windows if launch fails (single-instance). Prefer Chrome for Testing.'
        );
    }

    return {
        headless: extra.headless !== undefined ? extra.headless : 'new',
        executablePath,
        userDataDir: profileDir,
        defaultViewport: extra.defaultViewport !== undefined ? extra.defaultViewport : null,
        args,
        // Clean up temp profile when browser closes (puppeteer does not always)
        _ejProfileDir: profileDir,
    };
}

function cleanupLaunchProfile(browser, options) {
    const dir = options && options._ejProfileDir;
    if (!dir) return;
    try {
        fs.rmSync(dir, { recursive: true, force: true });
    } catch (e) {
        /* ignore */
    }
}

module.exports = {
    resolveBrowserExecutable,
    browserLabel,
    isArc,
    launchOptions,
    cleanupLaunchProfile,
    APP_CANDIDATES,
};
