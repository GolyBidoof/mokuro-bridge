/**
 * BookWalker headless capture → local mokuro bridge OCR → MEGA.
 * Same bridge client / finalize path as ebookjapan headlessscript.js.
 *
 * Requires a logged-in cookie jar (cookies.json). Prefer low concurrency —
 * BookWalker often rejects parallel viewers on one account.
 */
const path = require('path');
const fs = require('fs');
const os = require('os');
const crypto = require('crypto');

// Reuse ebookjapan's node_modules + helpers
const EJ = path.join(__dirname, '..', 'ebookjapan.yahoo.co.jp');
module.paths.unshift(path.join(EJ, 'node_modules'));

const puppeteer = require('puppeteer');
const sharp = require('sharp');
const bridge = require(path.join(EJ, 'mokuro_bridge'));
const { resolveBrowserExecutable, browserLabel, launchOptions, cleanupLaunchProfile } = require(path.join(EJ, 'resolve_browser'));
const {
    setProgress,
    logSticky,
    logError,
    startProgressDashboard,
    stopProgressDashboard,
} = require(path.join(EJ, 'progress_ui'));

const CONCURRENCY_LIMIT = parseInt(process.env.CONCURRENCY_LIMIT || '2', 10);
const OUTPUT_BASE_DIR = process.env.OUTPUT_BASE_DIR || path.join(__dirname, 'manga_archives');
const MAX_PAGES = parseInt(process.env.MAX_PAGES || '800', 10);
const MIN_PAGES_FOR_MEGA = parseInt(process.env.MIN_PAGES_FOR_MEGA || '10', 10);
const USE_MOKURO_BRIDGE = !['0', 'false', 'no'].includes(
    String(process.env.MOKURO_BRIDGE || '1').toLowerCase()
);
const VIEWPORT = {
    width: parseInt(process.env.VIEWPORT_WIDTH || '1200', 10),
    height: parseInt(process.env.VIEWPORT_HEIGHT || '1700', 10),
    deviceScaleFactor: parseInt(process.env.DEVICE_SCALE_FACTOR || '2', 10),
};

if (!fs.existsSync(OUTPUT_BASE_DIR)) fs.mkdirSync(OUTPUT_BASE_DIR, { recursive: true });

function pageFileName(n) {
    return `page_${String(n).padStart(3, '0')}.webp`;
}

function findExistingPagePath(dirs, n) {
    const name = pageFileName(n);
    for (const dir of dirs) {
        if (!dir) continue;
        const p = path.join(dir, name);
        if (fs.existsSync(p)) return p;
    }
    return null;
}

function nextPageToScrape(dirs) {
    let n = 1;
    while (findExistingPagePath(dirs, n)) n++;
    return n;
}

function cleanBookTitle(raw) {
    let t = String(raw || '')
        .replace(/BOOK☆WALKER|BOOKWALKER|BookWalker/gi, '')
        .replace(/[|\-–—]/g, ' ')
        .replace(/[\/\\?%*:|"<>]/g, '')
        .replace(/\s+/g, ' ')
        .trim();
    return t || `manga_${Date.now()}`;
}

function hashBuf(buf) {
    return crypto.createHash('md5').update(buf).digest('hex');
}

/** Remove manga_archives/<title> only after confirmed MEGA success. */
function removeArchiveAfterMegaSuccess(title, megaResult, { keepLocal = false } = {}) {
    if (keepLocal) return false;
    if (!megaResult || megaResult.status !== 'success') return false;
    const uploaded =
        Boolean(megaResult.mega_path) ||
        (Array.isArray(megaResult.uploads) &&
            megaResult.uploads.length > 0 &&
            megaResult.uploads.every((u) => u && u.success));
    if (!uploaded) return false;
    const dir = path.join(OUTPUT_BASE_DIR, title);
    try {
        if (fs.existsSync(dir)) {
            fs.rmSync(dir, { recursive: true, force: true });
            logSticky(`[${title}] removed manga_archives after MEGA upload`);
            return true;
        }
    } catch (e) {
        logError(`[${title}] archive cleanup failed: ${e.message}`);
    }
    return false;
}

/**
 * Load cookies from JSON (Puppeteer / EditThisCookie / Chrome export).
 */
function loadCookies(cookiePath) {
    const raw = JSON.parse(fs.readFileSync(cookiePath, 'utf8'));
    let list = Array.isArray(raw) ? raw : raw.cookies || raw;
    if (!Array.isArray(list)) throw new Error('cookies file must be a JSON array');
    return list
        .map((c) => {
            const domain = c.domain || c.host || '.bookwalker.jp';
            const cookie = {
                name: c.name,
                value: String(c.value ?? ''),
                domain: domain.startsWith('.') ? domain : domain.includes('bookwalker') ? domain : `.${domain}`,
                path: c.path || '/',
                httpOnly: Boolean(c.httpOnly),
                secure: c.secure !== false,
            };
            if (c.expirationDate || c.expires) {
                const exp = c.expirationDate || c.expires;
                cookie.expires = typeof exp === 'number' && exp > 1e12 ? exp / 1000 : Number(exp);
            }
            if (c.sameSite) cookie.sameSite = c.sameSite;
            return cookie;
        })
        .filter((c) => c.name && c.value !== undefined);
}

async function applyCookies(page, cookies) {
    // Puppeteer needs a page on the right domain before setCookie sometimes;
    // setCookie with domain works without navigation in recent Puppeteer.
    const normalized = cookies.map((c) => {
        const out = { ...c };
        // CDP wants url OR domain; prefer domain for BW
        if (!out.domain && !out.url) out.domain = '.bookwalker.jp';
        return out;
    });
    await page.setCookie(...normalized);
}

async function waitForViewer(page, timeoutMs = 90000) {
    await page.waitForFunction(
        () => {
            const counter = document.querySelector('#pageSliderCounter');
            const canvas = document.querySelector('.currentScreen canvas');
            const bar = document.querySelector('#pageSliderBar');
            if (!counter || !canvas || !bar) return false;
            if (canvas.width < 50 || canvas.height < 50) return false;
            return /\d+\s*\/\s*\d+/.test(counter.textContent || '');
        },
        { timeout: timeoutMs }
    );
}

/** Click OneTrust / BW cookie + age gates; hide leftover banners. */
async function dismissOverlays(page) {
    await page.evaluate(() => {
        const clickMatchers = [
            /すべてのCookieを許可/,
            /必須のCookieのみ許可/,
            /Allow all/i,
            /Accept all/i,
            /同意する/,
            /同意/,
            /はじめる/,
            /開始/,
            /読む/,
            /^OK$/i,
            /^はい$/,
            /^Enter$/i,
        ];
        const nodes = Array.from(
            document.querySelectorAll('button, a, input[type="button"], [role="button"], .ot-sdk-container button')
        );
        for (const b of nodes) {
            const t = (b.innerText || b.value || b.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
            if (!t || t.length > 40) continue;
            if (clickMatchers.some((re) => re.test(t))) {
                try { b.click(); } catch (e) { /* ignore */ }
            }
        }
        // OneTrust close / accept ids
        for (const id of ['onetrust-accept-btn-handler', 'accept-recommended-btn-handler', 'onetrust-reject-all-handler']) {
            const el = document.getElementById(id);
            if (el) try { el.click(); } catch (e) { /* ignore */ }
        }
    }).catch(() => {});

    // Brief wait for banner animation, then force-hide anything cookie-like still covering the page
    await new Promise((r) => setTimeout(r, 400));
    await page.evaluate(() => {
        const kill = (el) => {
            if (!el || el.dataset.bwHidden === '1') return;
            el.dataset.bwHidden = '1';
            el.style.setProperty('display', 'none', 'important');
            el.style.setProperty('visibility', 'hidden', 'important');
            el.style.setProperty('pointer-events', 'none', 'important');
            el.style.setProperty('opacity', '0', 'important');
        };
        for (const id of [
            'onetrust-banner-sdk',
            'onetrust-consent-sdk',
            'ot-sdk-btn-floating',
            'onetrust-pc-sdk',
        ]) {
            kill(document.getElementById(id));
        }
        document.querySelectorAll('[id*="onetrust"], [class*="onetrust"], [class*="cookie"], [id*="cookie"]').forEach(kill);
        // Large bottom banners mentioning Cookie
        for (const el of document.querySelectorAll('div, section, aside, footer')) {
            const t = (el.innerText || '').slice(0, 400);
            if (!/Cookie|クッキー/.test(t)) continue;
            if (!/許可|同意|Accept|Allow/i.test(t)) continue;
            const r = el.getBoundingClientRect();
            if (r.height > 60 && r.width > 200) kill(el);
        }
    }).catch(() => {});
}

/**
 * Hide BookWalker chrome (top toolbar / bottom slider) so canvas screenshots stay clean.
 * Menus can reappear on tap — call again before each capture.
 */
async function hideViewerChrome(page) {
    await page.evaluate(() => {
        if (!document.getElementById('bw-headless-chrome-hide')) {
            const style = document.createElement('style');
            style.id = 'bw-headless-chrome-hide';
            style.textContent = `
                #pagetitle, #menu, #menubar, #toolBar, #toolbar, #header,
                #viewerHeader, #menuBar, #ctrl, #controls, #bottomMenu,
                #pageSlider, #pageSliderBar, #pageSliderCounter, #slider,
                #zoomSlider, #zoomSliderBar, .menuBar, .toolBar, .bottomBar,
                #onetrust-banner-sdk, #onetrust-consent-sdk, .onetrust-pc-dark-filter {
                    display: none !important;
                    visibility: hidden !important;
                    opacity: 0 !important;
                    pointer-events: none !important;
                }
            `;
            document.documentElement.appendChild(style);
        }
        const sels = [
            '#pagetitle', '#menu', '#menubar', '#toolBar', '#toolbar', '#header',
            '#viewerHeader', '#menuBar', '#ctrl', '#controls', '#bottomMenu',
            '#pageSlider', '#pageSliderBar', '#pageSliderCounter', '#slider',
            '#zoomSlider', '#zoomSliderBar', '.menuBar', '.toolBar', '.bottomBar',
        ];
        for (const s of sels) {
            for (const el of document.querySelectorAll(s)) {
                el.style.setProperty('display', 'none', 'important');
                el.style.setProperty('visibility', 'hidden', 'important');
                el.style.setProperty('opacity', '0', 'important');
                el.style.setProperty('pointer-events', 'none', 'important');
            }
        }
        // Top strip: fixed/absolute bars near the top edge
        for (const el of document.querySelectorAll('body *')) {
            const st = getComputedStyle(el);
            if (st.position !== 'fixed' && st.position !== 'absolute') continue;
            const r = el.getBoundingClientRect();
            if (r.width < 180 || r.height < 24 || r.height > 140) continue;
            if (r.top <= 8 && r.bottom < 140) {
                el.style.setProperty('display', 'none', 'important');
                el.style.setProperty('visibility', 'hidden', 'important');
            }
        }
    }).catch(() => {});
}

/** Center-tap toggles BW menus off when they are visible. */
async function tapCenterToDismissMenus(page) {
    await page.evaluate(() => {
        const canvas = document.querySelector('.currentScreen canvas');
        const r = canvas
            ? canvas.getBoundingClientRect()
            : { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
        const x = r.left + r.width / 2;
        const y = r.top + r.height / 2;
        for (const type of ['pointerdown', 'pointerup', 'mousedown', 'mouseup', 'click']) {
            document.elementFromPoint(x, y)?.dispatchEvent(
                new MouseEvent(type, { bubbles: true, clientX: x, clientY: y, view: window })
            );
        }
    }).catch(() => {});
    await new Promise((r) => setTimeout(r, 200));
}

async function readCanvasResolution(page) {
    return page.evaluate(() => {
        const canvas = document.querySelector('.currentScreen canvas');
        if (!canvas) return null;
        const r = canvas.getBoundingClientRect();
        return {
            width: canvas.width,
            height: canvas.height,
            cssWidth: Math.round(r.width),
            cssHeight: Math.round(r.height),
            dpr: window.devicePixelRatio || 1,
        };
    });
}

/** Same sources as tampermonkey: configuration_pack.json Page.Size / *.xhtml.region.json w×h */
function extractSizeFromConfigPack(configData) {
    if (!configData || typeof configData !== 'object') return null;
    const firstPageKey = Object.keys(configData).find((key) => key.includes('.xhtml'));
    if (!firstPageKey) return null;
    const pageInfo = configData[firstPageKey];
    const page = pageInfo?.FileLinkInfo?.PageLinkInfoList?.[0]?.Page;
    const w = page?.Size?.Width;
    const h = page?.Size?.Height;
    if (!w || !h) return null;
    return {
        width: Math.round(Number(w)),
        height: Math.round(Number(h)),
        title: pageInfo.Title || null,
        method: 'metadata',
        source: 'configuration_pack',
    };
}

function extractSizeFromRegion(regionData) {
    if (!regionData || typeof regionData !== 'object') return null;
    const w = regionData.w;
    const h = regionData.h;
    if (!w || !h) return null;
    return {
        width: Math.round(Number(w)),
        height: Math.round(Number(h)),
        method: 'metadata',
        source: 'region',
    };
}

/**
 * Listen for BW pack/region JSON (must be attached before goto).
 * Mutates `slot` when a native size is found; pack wins over region.
 */
function attachNativeResolutionListener(page, slot) {
    page.on('response', async (res) => {
        try {
            const url = res.url();
            const isPack = url.includes('configuration_pack.json');
            const isRegion = url.includes('.xhtml.region') && url.includes('.json');
            if (!isPack && !isRegion) return;
            if (res.status() !== 200) return;
            const data = await res.json().catch(() => null);
            if (!data) return;
            if (isPack) {
                const parsed = extractSizeFromConfigPack(data);
                if (parsed) {
                    slot.width = parsed.width;
                    slot.height = parsed.height;
                    slot.method = parsed.method;
                    slot.source = parsed.source;
                    if (parsed.title) slot.title = parsed.title;
                }
            } else if (!slot.width || slot.source === 'region') {
                const parsed = extractSizeFromRegion(data);
                if (parsed) {
                    slot.width = parsed.width;
                    slot.height = parsed.height;
                    slot.method = parsed.method;
                    slot.source = parsed.source;
                }
            }
        } catch (e) {
            /* ignore parse/body races */
        }
    });
}

async function waitForNativeResolution(slot, timeoutMs = 15000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
        if (slot.width > 50 && slot.height > 50) return true;
        await new Promise((r) => setTimeout(r, 100));
    }
    return Boolean(slot.width > 50 && slot.height > 50);
}

/** Fallback when pack/region never arrives — mirror TM detectMangaResolution. */
async function detectCanvasApparentResolution(page) {
    return page.evaluate(() => {
        const viewerCanvas = document.querySelector('.currentScreen canvas');
        if (!viewerCanvas) return null;
        const dpr = window.devicePixelRatio || 1;
        const apparentWidth = Math.round(viewerCanvas.width / dpr);
        const apparentHeight = Math.round(viewerCanvas.height / dpr);
        if (!apparentWidth || !apparentHeight) return null;
        const matchesWindow =
            Math.abs(apparentWidth - window.innerWidth) < 20 &&
            Math.abs(apparentHeight - window.innerHeight) < 20;
        const aspect = apparentWidth / apparentHeight;
        const suspicious = matchesWindow || aspect > 0.95 || aspect < 0.4;
        return {
            width: apparentWidth,
            height: apparentHeight,
            method: suspicious ? 'canvas_pending' : 'canvas',
            source: 'canvas',
            suspicious,
        };
    });
}

/**
 * Resize viewer DOM to native page size (same idea as TM applyTargetResolutionToViewer)
 * so the canvas is content-sized instead of letterboxed into the browser viewport.
 */
async function applyNativeResolution(page, width, height) {
    // Capture at 1× CSS pixels so output matches metadata (e.g. 1441×2048), not viewport×dpr.
    await page.setViewport({
        width: Math.max(320, Math.round(width)),
        height: Math.max(480, Math.round(height)),
        deviceScaleFactor: 1,
    });

    await page.evaluate(
        ({ targetWidth, targetHeight }) => {
            const dpr = window.devicePixelRatio || 1;
            const exactW = Math.round(targetWidth * dpr);
            const exactH = Math.round(targetHeight * dpr);

            const viewerElement = document.getElementById('viewer');
            if (viewerElement) {
                viewerElement.style.setProperty('width', targetWidth + 'px', 'important');
                viewerElement.style.setProperty('height', targetHeight + 'px', 'important');
            }
            const rendererElement = document.querySelector('#renderer, .renderer');
            if (rendererElement) {
                rendererElement.style.setProperty('width', targetWidth + 'px', 'important');
                rendererElement.style.setProperty('height', targetHeight + 'px', 'important');
            }

            document.querySelectorAll('[id^="viewport"]').forEach((viewport) => {
                viewport.style.setProperty('width', targetWidth + 'px', 'important');
                viewport.style.setProperty('height', targetHeight + 'px', 'important');
                viewport.style.setProperty('overflow', 'visible', 'important');
                viewport.querySelectorAll('canvas').forEach((canvas) => {
                    canvas.style.setProperty('width', targetWidth + 'px', 'important');
                    canvas.style.setProperty('height', targetHeight + 'px', 'important');
                    canvas.width = exactW;
                    canvas.height = exactH;
                });
            });

            const frontScreenElement = document.getElementById('frontScreen');
            if (frontScreenElement) {
                const frontCanvas = frontScreenElement.querySelector('canvas');
                if (frontCanvas) {
                    frontCanvas.style.setProperty('width', targetWidth + 'px', 'important');
                    frontCanvas.style.setProperty('height', targetHeight + 'px', 'important');
                    frontCanvas.width = exactW;
                    frontCanvas.height = exactH;
                }
            }

            const pageHighlight = document.getElementById('pageHighlight');
            if (pageHighlight) {
                pageHighlight.style.setProperty('width', targetWidth + 'px', 'important');
                pageHighlight.style.setProperty('height', targetHeight + 'px', 'important');
            }

            window.dispatchEvent(new Event('resize'));
        },
        { targetWidth: Math.round(width), targetHeight: Math.round(height) }
    );

    await new Promise((r) => setTimeout(r, 350));
    await page
        .waitForFunction(
            (w, h) => {
                const c = document.querySelector('.currentScreen canvas');
                return c && c.width >= w * 0.9 && c.height >= h * 0.9;
            },
            { timeout: 8000 },
            Math.round(width),
            Math.round(height)
        )
        .catch(() => {});
}

async function readViewerMeta(page) {
    return page.evaluate(() => {
        const counter = document.querySelector('#pageSliderCounter');
        const m = (counter && counter.textContent || '').match(/(\d+)\s*\/\s*(\d+)/);
        const current = m ? parseInt(m[1], 10) : 1;
        const total = m ? parseInt(m[2], 10) : 0;
        let title = '';
        const titleEl = document.querySelector('#pagetitle .titleText, #pagetitle');
        if (titleEl) title = (titleEl.textContent || titleEl.getAttribute('title') || '').trim();
        if (!title) title = (document.title || '').trim();

        let isRtl = true;
        try {
            const handle = document.querySelector('#pageSliderBar .ui-slider-handle');
            if (handle && handle.style.left) {
                isRtl = parseFloat(handle.style.left) > 50;
            }
        } catch (e) { /* ignore */ }

        return { current, total, title, isRtl, hasJquery: typeof window.$ === 'function' };
    });
}

async function goToPage(page, targetPage, isRtl) {
    return page.evaluate(
        ({ targetPage, isRtl }) => {
            const bar = document.querySelector('#pageSliderBar');
            if (!bar || typeof window.$ !== 'function') return false;
            const $ = window.$;
            const max = $(bar).slider('option', 'max');
            const min = $(bar).slider('option', 'min');
            const value = isRtl ? max - (targetPage - 1) : min + (targetPage - 1);
            $(bar).slider('value', value);
            return true;
        },
        { targetPage, isRtl }
    );
}

async function waitPageSettled(page, prevHash, timeoutMs = 12000) {
    const start = Date.now();
    let lastHash = prevHash;
    while (Date.now() - start < timeoutMs) {
        const info = await page.evaluate(() => {
            const canvas = document.querySelector('.currentScreen canvas');
            if (!canvas || canvas.width < 50) return { ok: false, hash: null, busy: true };
            const busy = (() => {
                const sels = ['.loading', '.loadingImage', '#pageLoading', '.pageLoading', '#frontScreen'];
                for (const s of sels) {
                    for (const el of document.querySelectorAll(s)) {
                        const st = getComputedStyle(el);
                        if (st.display !== 'none' && st.visibility !== 'hidden' && parseFloat(st.opacity || '1') > 0.15) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 20 && r.height > 20) return true;
                        }
                    }
                }
                return false;
            })();
            return { ok: !busy, hash: `${canvas.width}x${canvas.height}`, busy };
        });
        if (info.ok && info.hash && info.hash !== prevHash) {
            // brief settle
            await new Promise((r) => setTimeout(r, 180));
            return info.hash;
        }
        if (info.ok && prevHash && Date.now() - start > 400) {
            await new Promise((r) => setTimeout(r, 120));
            return info.hash || lastHash;
        }
        lastHash = info.hash || lastHash;
        await new Promise((r) => setTimeout(r, 60));
    }
    return lastHash;
}

async function captureCanvasWebp(page, outPath) {
    await hideViewerChrome(page);
    const canvas = await page.$('.currentScreen canvas');
    if (!canvas) throw new Error('No viewer canvas');
    // CDP element screenshot — chrome must be hidden so overlays aren't in the clip.
    // Output matches canvas layout × deviceScaleFactor (BW's internal bitmap size).
    const pngBuf = await canvas.screenshot({ type: 'png' });
    await sharp(pngBuf).webp({ quality: 95 }).toFile(outPath);
    return pngBuf;
}

/**
 * Capture one BookWalker viewer URL end-to-end.
 */
async function captureBookwalker(viewerUrl, cookies, opts = {}) {
    const {
        useBridge = USE_MOKURO_BRIDGE,
        uploadToMega = true,
        keepLocal = false,
    } = opts;

    const launchOpts = launchOptions({
        defaultViewport: VIEWPORT,
        args: [
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
        ],
    });
    const browser = await puppeteer.launch(launchOpts);

    try {
        const page = await browser.newPage();
        await page.setUserAgent(
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        );
        await applyCookies(page, cookies);

        // Intercept pack/region JSON before navigation (same as TM metadata hooks).
        const nativeRes = { width: 0, height: 0, method: 'none', source: null, title: null };
        attachNativeResolutionListener(page, nativeRes);

        logSticky(`[Start] ${viewerUrl}`);
        await page.goto(viewerUrl, { waitUntil: 'domcontentloaded', timeout: 120000 });

        await dismissOverlays(page);
        await waitForViewer(page);
        await dismissOverlays(page);
        await new Promise((r) => setTimeout(r, 800));

        // Toggle menus off, then hard-hide chrome so canvas clips stay clean.
        await tapCenterToDismissMenus(page);
        await hideViewerChrome(page);
        await dismissOverlays(page);

        let meta = await readViewerMeta(page);
        let cleanTitle = cleanBookTitle(nativeRes.title || meta.title);
        setProgress(cleanTitle, { scraped: 0, ocrDone: 0, ocrTotal: 0, phase: 'starting' });

        await waitForNativeResolution(nativeRes, 12000);
        if (!(nativeRes.width > 50 && nativeRes.height > 50)) {
            const fallback = await detectCanvasApparentResolution(page);
            if (fallback && !fallback.suspicious) {
                nativeRes.width = fallback.width;
                nativeRes.height = fallback.height;
                nativeRes.method = fallback.method;
                nativeRes.source = fallback.source;
            }
        }

        if (nativeRes.width > 50 && nativeRes.height > 50) {
            logSticky(
                `[${cleanTitle}] native ${nativeRes.width}x${nativeRes.height}` +
                    ` via ${nativeRes.source || nativeRes.method}`
            );
            await applyNativeResolution(page, nativeRes.width, nativeRes.height);
            await hideViewerChrome(page);
            await dismissOverlays(page);
            // Re-read title/page count after resize (DOM still valid; slider hidden but readable).
            meta = await readViewerMeta(page);
            if (!cleanTitle || cleanTitle.startsWith('manga_')) {
                cleanTitle = cleanBookTitle(nativeRes.title || meta.title);
            }
        } else {
            logSticky(`[${cleanTitle}] no pack/region size — keeping viewport canvas`);
        }

        const canvasRes = await readCanvasResolution(page);
        if (canvasRes) {
            logSticky(
                `[${cleanTitle}] canvas ${canvasRes.width}x${canvasRes.height}` +
                    ` (css ${canvasRes.cssWidth}x${canvasRes.cssHeight} @${canvasRes.dpr}x)`
            );
        }

        if (!meta.total || meta.total < 1) {
            throw new Error('Could not read page count from #pageSliderCounter — cookie/session may be invalid');
        }
        if (!meta.hasJquery) {
            throw new Error('BookWalker jQuery UI slider not found — viewer not ready');
        }

        const tempOutputDir = path.join(OUTPUT_BASE_DIR, cleanTitle);
        if (!fs.existsSync(tempOutputDir)) fs.mkdirSync(tempOutputDir, { recursive: true });

        const pageDirs = [tempOutputDir];
        const resumeFrom = nextPageToScrape(pageDirs);
        const existingOnDisk = resumeFrom - 1;
        if (existingOnDisk > 0) {
            logSticky(`[${cleanTitle}] ${existingOnDisk} page(s) in manga_archives — skip to ${resumeFrom}`);
        }

        const sessionRef = { id: null };
        if (useBridge) {
            try {
                const session = await bridge.startSession(cleanTitle, { reuseExisting: true });
                sessionRef.id = session.session_id;
                if (existingOnDisk > 0) {
                    const r = await bridge.resumeSession(cleanTitle, tempOutputDir);
                    sessionRef.id = r.session_id;
                    setProgress(cleanTitle, {
                        scraped: existingOnDisk,
                        ocrDone: Math.min(r.pages_ocr_done || 0, existingOnDisk),
                        ocrTotal: existingOnDisk,
                        phase: 'skipping',
                        detail: `skip→${resumeFrom}`,
                    });
                }
            } catch (e) {
                logError(`[${cleanTitle}] bridge session failed: ${e.message}`);
            }
        }

        const pushPage = async (filePath, pageNum) => {
            if (!sessionRef.id) return null;
            let snap = await bridge.safePushPage(sessionRef.id, filePath, pageNum);
            if (snap && snap.__unknownSession) {
                try {
                    const r = await bridge.resumeSession(cleanTitle, tempOutputDir);
                    sessionRef.id = r.session_id;
                    snap = await bridge.safePushPage(sessionRef.id, filePath, pageNum);
                } catch (e) {
                    return { __error: e.message };
                }
            }
            if (snap && !snap.__error) {
                setProgress(cleanTitle, {
                    scraped: pageNum,
                    ocrDone: Math.min(snap.pages_ocr_done || 0, pageNum),
                    ocrTotal: pageNum,
                    phase: 'scraping',
                });
            } else if (snap && snap.__error) {
                setProgress(cleanTitle, {
                    scraped: pageNum,
                    phase: 'scraping',
                    detail: `push err: ${snap.__error}`.slice(0, 40),
                });
            }
            return snap;
        };

        const total = Math.min(meta.total, MAX_PAGES);
        logSticky(`[${cleanTitle}] ${total} pages · rtl=${meta.isRtl} · resumeFrom=${resumeFrom}`);

        // Go to resume page (or 1)
        const startAt = Math.min(Math.max(1, resumeFrom), total);
        await goToPage(page, startAt === 1 ? 1 : startAt, meta.isRtl);
        await new Promise((r) => setTimeout(r, 800));
        let prevHash = await waitPageSettled(page, null, 8000);

        const seenHashes = new Set();
        let pageCount = startAt;
        let consecutiveDup = 0;

        while (pageCount <= total) {
            meta = await readViewerMeta(page);
            // Stay in sync with slider counter when possible
            if (meta.current && Math.abs(meta.current - pageCount) > 1) {
                await goToPage(page, pageCount, meta.isRtl);
                prevHash = await waitPageSettled(page, prevHash, 8000);
            }

            if (pageCount < resumeFrom) {
                pageCount++;
                await goToPage(page, pageCount, meta.isRtl);
                prevHash = await waitPageSettled(page, prevHash, 6000);
                setProgress(cleanTitle, {
                    scraped: Math.min(pageCount - 1, existingOnDisk),
                    phase: 'skipping',
                    detail: `→${resumeFrom}`,
                });
                continue;
            }

            setProgress(cleanTitle, { phase: 'scraping', scraped: pageCount - 1 });

            const outPath = path.join(tempOutputDir, pageFileName(pageCount));
            if (findExistingPagePath(pageDirs, pageCount)) {
                const existing = findExistingPagePath(pageDirs, pageCount);
                if (sessionRef.id) await pushPage(existing, pageCount);
                pageCount++;
                if (pageCount <= total) {
                    await goToPage(page, pageCount, meta.isRtl);
                    prevHash = await waitPageSettled(page, prevHash, 8000);
                }
                continue;
            }

            let pngBuf;
            try {
                pngBuf = await captureCanvasWebp(page, outPath);
            } catch (e) {
                logError(`[${cleanTitle}] capture p${pageCount} failed: ${e.message}`);
                // try advance anyway
                pageCount++;
                if (pageCount <= total) {
                    await goToPage(page, pageCount, meta.isRtl);
                    prevHash = await waitPageSettled(page, prevHash, 8000);
                }
                continue;
            }

            const h = hashBuf(pngBuf);
            if (seenHashes.has(h)) {
                consecutiveDup++;
                if (consecutiveDup >= 2 && pageCount > resumeFrom + 2) {
                    logSticky(`[${cleanTitle}] end: repeated page content at ${pageCount}`);
                    try { fs.unlinkSync(outPath); } catch (e) { /* ignore */ }
                    break;
                }
            } else {
                consecutiveDup = 0;
                seenHashes.add(h);
            }

            if (sessionRef.id) await pushPage(outPath, pageCount);
            else setProgress(cleanTitle, { scraped: pageCount, phase: 'scraping' });

            pageCount++;
            if (pageCount > total) break;

            await goToPage(page, pageCount, meta.isRtl);
            prevHash = await waitPageSettled(page, prevHash, 10000);
        }

        const scrapedPages = nextPageToScrape(pageDirs) - 1;
        setProgress(cleanTitle, { scraped: scrapedPages, phase: useBridge ? 'finalizing' : 'done' });

        if (useBridge && uploadToMega && scrapedPages < MIN_PAGES_FOR_MEGA) {
            logError(
                `[${cleanTitle}] refusing MEGA: only ${scrapedPages} page(s) (min ${MIN_PAGES_FOR_MEGA})`
            );
            setProgress(cleanTitle, { phase: 'error', detail: `too few pages (${scrapedPages})` });
            return { title: cleanTitle, path: tempOutputDir, mega: null, aborted: 'too_few_pages' };
        }

        let megaResult = null;
        if (useBridge && sessionRef.id) {
            try {
                megaResult = await bridge.finalizeSession(sessionRef.id, {
                    uploadToMega,
                    deleteAfterUpload: uploadToMega && !keepLocal,
                    onProgress: (msg) => {
                        setProgress(cleanTitle, {
                            phase: msg.stage === 'upload' ? 'uploading' : msg.stage || 'finalizing',
                            ocrDone: msg.pages_ocr_done,
                            ocrTotal: msg.pages_received || msg.pages,
                            detail: (msg.message || '').slice(0, 36),
                        });
                    },
                });
                setProgress(cleanTitle, {
                    phase: uploadToMega ? 'uploaded' : 'ocr-done',
                    ocrDone: megaResult.pages_ocr_done ?? megaResult.pages,
                    ocrTotal: megaResult.pages,
                });
                if (uploadToMega) removeArchiveAfterMegaSuccess(cleanTitle, megaResult, { keepLocal });
                logSticky(`[${cleanTitle}] ${megaResult.message || 'done'}`);
            } catch (e) {
                if (bridge.isUnknownSessionError(e)) {
                    try {
                        const { final } = await bridge.resumeAndFinalize(cleanTitle, tempOutputDir, {
                            uploadToMega,
                            keepLocal,
                        });
                        megaResult = final;
                        if (uploadToMega) removeArchiveAfterMegaSuccess(cleanTitle, megaResult, { keepLocal });
                    } catch (e2) {
                        logError(`[${cleanTitle}] finalize failed: ${e2.message}`);
                        setProgress(cleanTitle, { phase: 'error', detail: e2.message.slice(0, 36) });
                    }
                } else {
                    logError(`[${cleanTitle}] finalize failed: ${e.message}`);
                    setProgress(cleanTitle, { phase: 'error', detail: e.message.slice(0, 36) });
                }
            }
        }

        return { title: cleanTitle, path: tempOutputDir, mega: megaResult, pages: scrapedPages };
    } finally {
        await browser.close().catch(() => {});
        cleanupLaunchProfile(browser, launchOpts);
    }
}

async function ocrOnlyResume(titles, { uploadToMega, keepLocal }) {
    const homeInput = path.join(os.homedir(), 'mokuro-input');
    const countImages = (dir) => {
        try {
            return fs.readdirSync(dir).filter((f) => /\.(webp|jpg|jpeg|png)$/i.test(f)).length;
        } catch (e) {
            return 0;
        }
    };

    logSticky(`Resuming ${titles.length} volume(s) — OCR + MEGA only`);
    startProgressDashboard(1000);
    const results = [];
    try {
        for (let i = 0; i < titles.length; i += CONCURRENCY_LIMIT) {
            const chunk = titles.slice(i, i + CONCURRENCY_LIMIT);
            const part = await Promise.all(
                chunk.map(async (title) => {
                    const archiveDir = path.join(OUTPUT_BASE_DIR, title);
                    const inputN = countImages(path.join(homeInput, title));
                    const archiveN = countImages(archiveDir);
                    const sourceDir = archiveN > inputN && fs.existsSync(archiveDir) ? archiveDir : '';
                    setProgress(title, { phase: 'resuming' });
                    try {
                        const { resumed, final } = await bridge.resumeAndFinalize(title, sourceDir, {
                            uploadToMega,
                            keepLocal,
                            onProgress: (msg) => {
                                setProgress(title, {
                                    phase: msg.stage || 'ocr',
                                    ocrDone: msg.pages_ocr_done ?? msg.ocr_cached,
                                    ocrTotal: msg.pages_received || msg.pages,
                                    scraped: msg.pages_received || msg.pages || 0,
                                });
                            },
                        });
                        if (uploadToMega) removeArchiveAfterMegaSuccess(title, final, { keepLocal });
                        setProgress(title, { phase: uploadToMega ? 'uploaded' : 'ocr-done' });
                        logSticky(`[${title}] cached=${resumed.ocr_cached} queued=${resumed.queued_for_ocr}`);
                        return { title, final };
                    } catch (e) {
                        logError(`[${title}] ${e.message}`);
                        setProgress(title, { phase: 'error', detail: e.message.slice(0, 36) });
                        return null;
                    }
                })
            );
            results.push(...part.filter(Boolean));
        }
    } finally {
        stopProgressDashboard();
    }
    return results;
}

(async () => {
    const args = process.argv.slice(2);
    const flags = new Set(args.filter((a) => a.startsWith('--') && !a.includes('=')));
    const kv = {};
    for (const a of args) {
        const m = a.match(/^--([^=]+)=(.*)$/);
        if (m) kv[m[1]] = m[2];
    }
    const positional = args.filter((a) => !a.startsWith('--'));

    const cookiePath = kv.cookies || (flags.has('--cookies') ? null : path.join(__dirname, 'cookies.json'));
    // support: --cookies path
    let cookiesFile = kv.cookies;
    for (let i = 0; i < args.length; i++) {
        if (args[i] === '--cookies' && args[i + 1] && !args[i + 1].startsWith('--')) {
            cookiesFile = args[i + 1];
        }
    }
    const posNoCookiePath = positional.filter((p) => p !== cookiesFile);

    const localOnly = flags.has('--local-only') || !USE_MOKURO_BRIDGE;
    const noMega = flags.has('--no-upload') || flags.has('--no-mega');
    const keepLocal = flags.has('--keep-local');
    const doResume = flags.has('--resume');
    const concurrency = parseInt(kv.concurrency || process.env.CONCURRENCY_LIMIT || String(CONCURRENCY_LIMIT), 10);

    const isUrl = (t) => /^https?:\/\//i.test(t) || /bookwalker\.jp/i.test(t);
    const urls = posNoCookiePath.filter(isUrl);
    const titles = posNoCookiePath.filter((t) => !isUrl(t));

    if (!doResume && urls.length === 0 && titles.length === 0) {
        console.log(`Usage:
  node headless.js --cookies cookies.json <viewerUrl...> [flags]
  node headless.js --resume [title...]          # OCR+MEGA only
  node headless.js --cookies cookies.json --resume <viewerUrl...>

Flags:
  --cookies PATH     BookWalker session cookies (JSON)
  --resume           with URLs: continue scrape; with titles: OCR+MEGA only
  --local-only       scrape only
  --no-mega          OCR, skip MEGA
  --keep-local       keep archives + bridge dir after MEGA
  --concurrency N    parallel viewers (default ${CONCURRENCY_LIMIT} — keep low)

Env: MOKURO_BRIDGE_URL CONCURRENCY_LIMIT MAX_PAGES MIN_PAGES_FOR_MEGA
`);
        return;
    }

    const captureOpts = {
        useBridge: !localOnly,
        uploadToMega: !noMega && !localOnly,
        keepLocal,
    };

    if (captureOpts.useBridge || (doResume && urls.length === 0)) {
        const h = await bridge.ensureBridge();
        logSticky(
            `[Bridge] ready (mokuro=${h.mokuro_installed} mega=${h.mega_configured}` +
                `${h.mokuro_custom_fork ? ' · custom fork' : ''})`
        );
    }

    // OCR-only resume (titles, no URLs)
    if (doResume && urls.length === 0) {
        let list = titles.map((t) => path.basename(t.replace(/\/$/, '')));
        if (list.length === 0) {
            list = fs.readdirSync(OUTPUT_BASE_DIR).filter((name) => {
                const full = path.join(OUTPUT_BASE_DIR, name);
                return (
                    fs.statSync(full).isDirectory() &&
                    !name.startsWith('_') &&
                    fs.readdirSync(full).some((f) => /\.webp$/i.test(f))
                );
            });
        }
        if (!list.length) {
            console.log('Nothing to resume.');
            return;
        }
        await ocrOnlyResume(list, captureOpts);
        return;
    }

    if (!cookiesFile || !fs.existsSync(cookiesFile)) {
        console.error(
            `Need cookies JSON. Export while logged into bookwalker.jp, then:\n` +
                `  node headless.js --cookies cookies.json <viewerUrl>\n` +
                `See cookies.example.json`
        );
        process.exit(1);
    }

    const cookies = loadCookies(cookiesFile);
    logSticky(`[Cookies] loaded ${cookies.length} from ${cookiesFile}`);
    logSticky(`[Browser] ${browserLabel(resolveBrowserExecutable())}`);
    logSticky(
        `[Concurrency] ${concurrency} (BookWalker may lock parallel sessions — lower if errors)`
    );

    if (concurrency > 3) {
        logSticky('[Warn] concurrency > 3 is risky for BookWalker account locks');
    }

    startProgressDashboard(1000);
    const results = [];
    try {
        for (let i = 0; i < urls.length; i += concurrency) {
            const chunk = urls.slice(i, i + concurrency);
            const part = await Promise.all(
                chunk.map((url) =>
                    captureBookwalker(url, cookies, captureOpts).catch((e) => {
                        logError(`[Fail] ${url}: ${e.message}`);
                        return null;
                    })
                )
            );
            results.push(...part.filter(Boolean));
        }
    } finally {
        stopProgressDashboard();
    }

    const ok = results.filter((r) => r && !r.aborted).length;
    const megaOk = results.filter((r) => r && r.mega && r.mega.status === 'success').length;
    console.log(`Done. volumes=${ok}/${urls.length}` + (captureOpts.useBridge ? ` mega_ok=${megaOk}` : ''));
})().catch((e) => {
    console.error(e);
    process.exit(1);
});
