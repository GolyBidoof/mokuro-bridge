const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const archiver = require('archiver');
const { PNG } = require('pngjs');
const sharp = require('sharp'); // Use sharp for fast WebP conversion
const { spawn } = require('child_process');
const bridge = require('./mokuro_bridge');
const { resolveBrowserExecutable, browserLabel, launchOptions, cleanupLaunchProfile } = require('./resolve_browser');
const {
    setProgress,
    logSticky,
    logError,
    startProgressDashboard,
    stopProgressDashboard,
} = require('./progress_ui');

// Configuration
const CONCURRENCY_LIMIT = parseInt(process.env.CONCURRENCY_LIMIT) || 10;
const OUTPUT_BASE_DIR = process.env.OUTPUT_BASE_DIR || path.join(__dirname, 'manga_archives');
const DEVICE_SCALE_FACTOR = parseInt(process.env.DEVICE_SCALE_FACTOR) || 2;
const VIEWPORT_WIDTH = parseInt(process.env.VIEWPORT_WIDTH) || 1414;
const VIEWPORT_HEIGHT = parseInt(process.env.VIEWPORT_HEIGHT) || 1000;
const PUPPETEER_EXECUTABLE_PATH = resolveBrowserExecutable();
// Bridge OCR + MEGA (default on). Disable with --local-only or MOKURO_BRIDGE=0
const USE_MOKURO_BRIDGE = !['0', 'false', 'no'].includes(
    String(process.env.MOKURO_BRIDGE || '1').toLowerCase()
);

function pageFileName(n) {
    return `page_${String(n).padStart(3, '0')}.webp`;
}

/** Prefer manga_archives, fall back to ~/mokuro-input/<title>. */
function findExistingPagePath(dirs, n) {
    const name = pageFileName(n);
    for (const dir of dirs) {
        if (!dir) continue;
        const p = path.join(dir, name);
        if (fs.existsSync(p)) return p;
    }
    return null;
}

/** First missing page index (1-based), scanning contiguous from page_001. */
function nextPageToScrape(dirs) {
    let n = 1;
    while (findExistingPagePath(dirs, n)) n++;
    return n;
}



/** Remove manga_archives/<title> only after a confirmed successful MEGA upload. */
function removeArchiveAfterMegaSuccess(title, megaResult, { keepLocal = false } = {}) {
    if (keepLocal) return false;
    if (!megaResult || megaResult.status !== 'success') return false;
    // Bridge "done" without MEGA still returns status success — require mega_path/uploads.
    const uploaded =
        Boolean(megaResult.mega_path) ||
        (Array.isArray(megaResult.uploads) &&
            megaResult.uploads.length > 0 &&
            megaResult.uploads.every((u) => u && u.success));
    if (!uploaded) return false;

    const dir = path.join(OUTPUT_BASE_DIR, title);
    const zip = path.join(OUTPUT_BASE_DIR, `${title}.zip`);
    let removed = false;
    try {
        if (fs.existsSync(dir)) {
            fs.rmSync(dir, { recursive: true, force: true });
            removed = true;
        }
        if (fs.existsSync(zip)) {
            fs.unlinkSync(zip);
            removed = true;
        }
        if (removed) logSticky(`[${title}] removed manga_archives after MEGA upload`);
    } catch (e) {
        logError(`[${title}] archive cleanup failed: ${e.message}`);
    }
    return removed;
}

function applyPageSnap(title, pageCount, snap) {
    if (snap && snap.__error) {
        setProgress(title, {
            scraped: pageCount,
            phase: 'scraping',
            detail: `push err: ${snap.__error}`.slice(0, 40),
        });
        return;
    }
    if (snap) {
        // scraped = manga_archives index. Do not use bridge pages_received —
        // ~/mokuro-input can still hold overrun pages from an earlier run.
        const ocrDone = Math.min(Number(snap.pages_ocr_done) || 0, pageCount);
        setProgress(title, {
            scraped: pageCount,
            ocrDone,
            ocrTotal: pageCount,
            phase: 'scraping',
            detail: '',
        });
    } else {
        setProgress(title, { scraped: pageCount, phase: 'scraping' });
    }
}

if (!fs.existsSync(OUTPUT_BASE_DIR)) {
    fs.mkdirSync(OUTPUT_BASE_DIR);
}

// --- Helpers ---

const isBufferBlank = (buffer) => {
    return new Promise((resolve) => {
        new PNG().parse(buffer, (error, data) => {
            if (error) return resolve(false);
            const { width, height, data: pixels } = data;
            const totalPixels = width * height;
            const step = 50;
            let rSum = 0, gSum = 0, bSum = 0;
            let count = 0;
            for (let i = 0; i < totalPixels; i += step) {
                const idx = i * 4;
                rSum += pixels[idx]; gSum += pixels[idx + 1]; bSum += pixels[idx + 2];
                count++;
            }
            const rAvg = rSum / count, gAvg = gSum / count, bAvg = bSum / count;
            let variance = 0;
            for (let i = 0; i < totalPixels; i += step) {
                const idx = i * 4;
                variance += Math.abs(pixels[idx] - rAvg) + Math.abs(pixels[idx + 1] - gAvg) + Math.abs(pixels[idx + 2] - bAvg);
            }
            resolve((variance / count) < 5);
        });
    });
};

const calculateCropMargins = (buffer) => {
    return new Promise((resolve, reject) => {
        new PNG().parse(buffer, (error, data) => {
            if (error) return reject(error);
            const { width, height, data: pixels } = data;
            const isColBlank = (x) => {
                let variance = 0, rSum = 0, gSum = 0, bSum = 0;
                for (let y = 0; y < height; y += 10) {
                    const idx = (width * y + x) * 4;
                    rSum += pixels[idx]; gSum += pixels[idx + 1]; bSum += pixels[idx + 2];
                }
                const count = Math.ceil(height / 10);
                const rAvg = rSum / count, gAvg = gSum / count, bAvg = bSum / count;
                for (let y = 0; y < height; y += 10) {
                    const idx = (width * y + x) * 4;
                    variance += Math.abs(pixels[idx] - rAvg) + Math.abs(pixels[idx + 1] - gAvg) + Math.abs(pixels[idx + 2] - bAvg);
                }
                return (variance / count) < 5;
            };
            const isRowBlank = (y) => {
                let variance = 0, rSum = 0, gSum = 0, bSum = 0;
                for (let x = 0; x < width; x += 10) {
                    const idx = (width * y + x) * 4;
                    rSum += pixels[idx]; gSum += pixels[idx + 1]; bSum += pixels[idx + 2];
                }
                const count = Math.ceil(width / 10);
                const rAvg = rSum / count, gAvg = gSum / count, bAvg = bSum / count;
                for (let x = 0; x < width; x += 10) {
                    const idx = (width * y + x) * 4;
                    variance += Math.abs(pixels[idx] - rAvg) + Math.abs(pixels[idx + 1] - gAvg) + Math.abs(pixels[idx + 2] - bAvg);
                }
                return (variance / count) < 5;
            };
            let top = 0, bottom = 0, left = 0, right = 0;
            for (let y = 0; y < height; y++) { if (!isRowBlank(y)) { top = y; break; } }
            for (let y = height - 1; y >= 0; y--) { if (!isRowBlank(y)) { bottom = height - 1 - y; break; } }
            for (let x = 0; x < width; x++) { if (!isColBlank(x)) { left = x; break; } }
            for (let x = width - 1; x >= 0; x--) { if (!isColBlank(x)) { right = width - 1 - x; break; } }

            if (left > 0) {
                top = 0;
            }

            resolve({ top, bottom, left, right, width, height });
        });
    });
};

const saveImage = async (buffer, outputPath, margins, isRightPage) => {
    let pipeline = sharp(buffer);
    if (margins) {
        const width = margins.width;
        const height = margins.height;
        let extractOptions = { left: 0, top: margins.top, width: width, height: height - margins.top - margins.bottom };
        if (isRightPage) {
            extractOptions.left = margins.right;
            extractOptions.width = width - margins.right - margins.left;
        } else {
            extractOptions.left = margins.left;
            extractOptions.width = width - margins.left - margins.right;
        }
        if (extractOptions.width > 0 && extractOptions.height > 0) {
            pipeline = pipeline.extract(extractOptions);
        }
    }
    await pipeline.webp({ quality: 95 }).toFile(outputPath);
};



// Wait for screen to change AND stabilize (no spinner)
const waitForContentLoad = async (page, previousBuffer) => {
    const maxTotalWait = 30000; // 30 seconds total timeout
    const startTime = Date.now();

    // 1. Wait for Change (Navigation started)
    let currentBuffer = previousBuffer;
    let changed = false;

    while (Date.now() - startTime < maxTotalWait) {
        const buffer = await page.screenshot({ fullPage: false });
        if (!buffer.equals(previousBuffer)) {
            currentBuffer = buffer;
            changed = true;
            break;
        }
        await new Promise(r => setTimeout(r, 50));
    }

    if (!changed) return previousBuffer; // Timed out waiting for change

    // 2. Wait for Stability (Spinner gone)
    const stabilityWait = 200; // Wait 200ms between checks to ensure spinner rotation is caught

    while (Date.now() - startTime < maxTotalWait) {
        await new Promise(r => setTimeout(r, stabilityWait));
        const nextBuffer = await page.screenshot({ fullPage: false });

        if (currentBuffer.equals(nextBuffer)) {
            return nextBuffer;
        } else {
            currentBuffer = nextBuffer;
        }
    }

    return currentBuffer;
};

// --- Core Logic ---

/** Same title cleaning used by captureManga (must match disk folder names). */
function cleanBookTitle(rawTitle) {
    let cleanTitle = String(rawTitle || '')
        .replace(/ebookjapan|無料|まんが|電子書籍/gi, '')
        .replace(/[|\-]/g, ' ')
        .replace(/[\/\\?%*:|"<>]/g, '')
        .replace(/\s+/g, ' ')
        .trim();
    if (!cleanTitle) cleanTitle = `manga_${Date.now()}`;
    return cleanTitle;
}

const discoverVolumes = async (seriesUrl) => {
    const opts = launchOptions({
        defaultViewport: null,
        args: ['--start-maximized'],
    });
    const browser = await puppeteer.launch(opts);
    try {
    const page = await browser.newPage();
    await page.setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');

    const allLinks = new Set();
    const urlsToCheck = [seriesUrl];
    if (!seriesUrl.includes('type=serialStory')) {
        urlsToCheck.push(`${seriesUrl}?type=serialStory`);
    }

    for (const url of urlsToCheck) {
        console.log(`[Discovery] Loading: ${url}`);
        try {
            await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
        } catch (e) {
            console.error(`[Discovery] Error loading ${url}: ${e.message}. Retrying...`);
            try {
                await page.goto(url, { waitUntil: 'load', timeout: 60000 });
            } catch (e2) {
                console.error(`[Discovery] Failed to load ${url}: ${e2.message}`);
                continue;
            }
        }
        await new Promise(r => setTimeout(r, 2000));

        // 1. Handle Popovers
        try {
            await page.evaluate(async () => {
                const buttons = Array.from(document.querySelectorAll('button, span, div'));
                const closeBtn = buttons.find(el => el.innerText && el.innerText.includes('閉じる'));
                if (closeBtn) closeBtn.click();
                const overlays = document.querySelectorAll('.modal-overlay, .popup-overlay');
                overlays.forEach(el => el.click());
            });
            await new Promise(r => setTimeout(r, 1000));
        } catch (e) { }

        // 2. Expand "More"
        try {
            await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
            await new Promise(r => setTimeout(r, 1000));
            const expanded = await page.evaluate(() => {
                const btns = document.querySelectorAll('.contents-free__more, .contents-more-toggle');
                let clicked = false;
                btns.forEach(b => { b.click(); clicked = true; });
                return clicked;
            });
            if (expanded) {
                console.log('[Discovery] Expanded list.');
                await new Promise(r => setTimeout(r, 2000));
            }
        } catch (e) { }

        // 3. Extract Links
        const links = await page.evaluate(() => {
            const found = [];
            const listItems = document.querySelectorAll('.free-list__item a, .serial-list__item a, .book-item');
            listItems.forEach(a => {
                if (a.href && a.href.includes('/books/')) found.push(a.href);
            });
            const buttons = document.querySelectorAll('button, span');
            buttons.forEach(el => {
                const text = el.innerText || '';
                if (text.includes('無料で読む') || text.includes('読む') || text.includes('試し読み')) {
                    const anchor = el.closest('a');
                    if (anchor && anchor.href && anchor.href.includes('/books/')) {
                        found.push(anchor.href);
                    }
                }
            });
            return found;
        });

        console.log(`[Discovery] Found ${links.length} candidates on ${url}`);
        links.forEach(l => allLinks.add(l));
    }

    // Debug Screenshot
    // await page.screenshot({ path: `discovery_debug_${Date.now()}.png` }); // Removed as per instruction, not in new block

    return Array.from(allLinks);
    } finally {
        await browser.close().catch(() => {});
        cleanupLaunchProfile(browser, opts);
    }
};

const captureManga = async (bookUrl, { useBridge = USE_MOKURO_BRIDGE, uploadToMega = true, keepLocal = false } = {}) => {
    const opts = launchOptions({
        defaultViewport: null,
        args: [
            '--start-maximized',
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
        ],
    });
    const browser = await puppeteer.launch(opts);

    try {
        const page = await browser.newPage();
        await page.setViewport({ width: VIEWPORT_WIDTH, height: VIEWPORT_HEIGHT, deviceScaleFactor: DEVICE_SCALE_FACTOR });
        await page.setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
        await page.bringToFront();

        console.log(`[Start] Book Page: ${bookUrl}`);
        await page.goto(bookUrl, { waitUntil: 'domcontentloaded', timeout: 90000 });

        // Resolve free-viewer URL from page payload (A00→B0016 string replace is WRONG —
        // ebookjapan A-codes and B-codes are different catalog IDs).
        let directViewerUrl = await page.evaluate(() => {
            const el = document.getElementById('__NUXT_DATA__');
            if (!el || !el.textContent) return null;
            let data;
            try {
                data = JSON.parse(el.textContent);
            } catch (e) {
                return null;
            }
            if (!Array.isArray(data)) return null;
            const resolve = (v) =>
                typeof v === 'number' && v >= 0 && v < data.length ? data[v] : v;
            for (const item of data) {
                if (!item || typeof item !== 'object' || !('bookCd' in item)) continue;
                const isFree =
                    resolve(item.isFree) === true || resolve(item.isBrowserSpecialFree) === true;
                if (!isFree) continue;
                const bookCd = resolve(item.bookCd);
                if (typeof bookCd === 'string' && /^B00\d+$/.test(bookCd) && bookCd.length <= 14) {
                    return `https://ebookjapan.yahoo.co.jp/viewer/free/${bookCd}`;
                }
            }
            return null;
        });
        if (directViewerUrl) {
            logSticky(`[Viewer] from page bookCd → ${directViewerUrl}`);
        } else {
            logSticky(`[Viewer] no bookCd in page payload — will click 無料で読む`);
        }

        // 1. Extract Title
        let rawTitle = await page.evaluate(() => {
            const h1 = document.querySelector('h1.book-main__heading');
            return h1 ? h1.innerText.trim() : document.title;
        });

        let cleanTitle = cleanBookTitle(rawTitle);

        if (!cleanTitle) cleanTitle = `manga_${Date.now()}`;
        setProgress(cleanTitle, { scraped: 0, ocrDone: 0, ocrTotal: 0, phase: 'starting' });

        const sessionRef = { id: null };
        if (useBridge) {
            try {
                const session = await bridge.startSession(cleanTitle, { reuseExisting: true });
                sessionRef.id = session.session_id;
                if (session.reused) {
                    logSticky(`[${cleanTitle}] reusing bridge volume ${session.safe_title || cleanTitle}`);
                }
            } catch (e) {
                logError(`[${cleanTitle}] bridge session failed, scrape-only: ${e.message}`);
            }
        }
        setProgress(cleanTitle, { phase: 'scraping' });

        const tempOutputDir = path.join(OUTPUT_BASE_DIR, cleanTitle);
        const outputZipPath = path.join(OUTPUT_BASE_DIR, `${cleanTitle}.zip`);

        // Check if Zip exists (Overwrite logic)
        if (fs.existsSync(outputZipPath)) {
            fs.unlinkSync(outputZipPath);
        }

        // Keep existing archive pages (resume / re-runs must not wipe).
        if (!fs.existsSync(tempOutputDir)) {
            fs.mkdirSync(tempOutputDir, { recursive: true });
        }

        // Scrape-resume keys off manga_archives only (not ~/mokuro-input).
        const pageDirs = [tempOutputDir];
        const resumeFrom = nextPageToScrape(pageDirs);
        const existingOnDisk = resumeFrom - 1;
        const bridgeInputDir = path.join(os.homedir(), 'mokuro-input', cleanTitle);
        const inputPageCount = (() => {
            try {
                return fs.readdirSync(bridgeInputDir).filter((f) => /\.webp$/i.test(f)).length;
            } catch (e) {
                return 0;
            }
        })();
        if (existingOnDisk > 0) {
            logSticky(
                `[${cleanTitle}] ${existingOnDisk} page(s) in manga_archives — will skip to page ${resumeFrom}`
            );
            if (inputPageCount > existingOnDisk) {
                logSticky(
                    `[${cleanTitle}] note: ~/mokuro-input still has ${inputPageCount} pages ` +
                        `(prior run); progress tracks manga_archives`
                );
            }
            setProgress(cleanTitle, {
                scraped: existingOnDisk,
                ocrDone: 0,
                ocrTotal: existingOnDisk,
                phase: 'skipping',
                detail: `skip→${resumeFrom}`,
            });
            // Sync existing archive pages into the bridge OCR queue before we continue scraping.
            if (useBridge) {
                try {
                    const r = await bridge.resumeSession(cleanTitle, tempOutputDir);
                    sessionRef.id = r.session_id;
                    // Keep scrape/OCR totals aligned to archives, not the whole mokuro-input dir.
                    setProgress(cleanTitle, {
                        scraped: existingOnDisk,
                        ocrDone: Math.min(r.pages_ocr_done || 0, existingOnDisk),
                        ocrTotal: existingOnDisk,
                        phase: 'skipping',
                        detail: `skip→${resumeFrom}`,
                    });
                } catch (e) {
                    logError(`[${cleanTitle}] pre-scrape resume failed: ${e.message}`);
                }
            }
        }

        const recoverBridgeSession = async () => {
            if (!useBridge) return null;
            try {
                const r = await bridge.resumeSession(cleanTitle, tempOutputDir);
                sessionRef.id = r.session_id;
                const scrapedNow = nextPageToScrape(pageDirs) - 1;
                setProgress(cleanTitle, {
                    scraped: scrapedNow,
                    ocrDone: Math.min(r.pages_ocr_done || 0, scrapedNow || 1),
                    ocrTotal: Math.max(scrapedNow, 1),
                    phase: 'scraping',
                    detail: 'session recovered',
                });
                logSticky(
                    `[${cleanTitle}] recovered bridge session ${r.session_id.slice(0, 6)} ` +
                        `(cached=${r.ocr_cached} queued=${r.queued_for_ocr})`
                );
                return r;
            } catch (e) {
                logError(`[${cleanTitle}] session recover failed: ${e.message}`);
                sessionRef.id = null;
                return null;
            }
        };

        const pushPage = async (filePath, pageNum) => {
            if (!sessionRef.id) return null;
            let snap = await bridge.safePushPage(sessionRef.id, filePath, pageNum);
            if (snap && snap.__unknownSession) {
                await recoverBridgeSession();
                if (sessionRef.id) {
                    snap = await bridge.safePushPage(sessionRef.id, filePath, pageNum);
                }
            }
            applyPageSnap(cleanTitle, pageNum, snap);
            return snap;
        };

        // 2. Navigate directly to viewer if we have a direct URL
        // This is more reliable than trying to click buttons through popups
        if (directViewerUrl) {
            await page.goto(directViewerUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
            await new Promise(r => setTimeout(r, 2000));
        } else {
            // Fallback: Try to click the read button
            await new Promise(r => setTimeout(r, 2000));

            // Try to dismiss popups
            try {
                await page.keyboard.press('Escape');
                await new Promise(r => setTimeout(r, 500));
            } catch (e) { }

            // 3. Click "Read" button and wait for navigation (only if no direct URL)
            const viewerTargetPromise = new Promise(resolve => browser.once('targetcreated', resolve));

            // Find the read button and get its position for native click
            const buttonInfo = await page.evaluate(() => {
                const buttons = Array.from(document.querySelectorAll('button'));
                const readBtn = buttons.find(b => {
                    const text = b.innerText || '';
                    return text.includes('無料で読む') || text.includes('試し読み');
                });
                if (readBtn) {
                    readBtn.scrollIntoView({ behavior: 'instant', block: 'center' });
                    const rect = readBtn.getBoundingClientRect();
                    return {
                        found: true,
                        x: rect.x + rect.width / 2,
                        y: rect.y + rect.height / 2,
                        text: readBtn.innerText
                    };
                }
                return { found: false };
            });

            if (!buttonInfo.found) {
                logError(`[Error] Could not find 'Read' button on ${bookUrl}`);
                setProgress(cleanTitle, { phase: 'error', detail: 'no read button' });
                return null;
            }

            await new Promise(r => setTimeout(r, 500));

            try {
                await Promise.all([
                    page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 15000 }).catch(() => { }),
                    page.mouse.click(buttonInfo.x, buttonInfo.y)
                ]);
            } catch (e) { }

            await new Promise(r => setTimeout(r, 2000));
        }

        // 4. Handle viewer - find the bviewer iframe and navigate to it
        let viewerPage = page;

        // If we navigated to the /viewer/ page, find the iframe
        const currentUrl = await page.url();
        if (currentUrl.includes('/viewer/')) {
            // Try to get iframe URL from the page
            let viewerUrl = await page.evaluate(() => {
                const iframe = document.querySelector('iframe#viewer, iframe.viewer__iframe, iframe[src*="bviewer"]');
                if (iframe && iframe.src) {
                    return iframe.src;
                }
                return null;
            });

            if (viewerUrl) {
                // Remove trailing slash if present - bviewer returns 404 with trailing slash
                viewerUrl = viewerUrl.replace(/\/$/, '');
                await page.goto(viewerUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
            } else if (directViewerUrl) {
                // Construct bviewer URL from directViewerUrl
                let bviewerUrl = directViewerUrl.replace('/viewer/', '/bviewer/').replace(/\/$/, '');
                await page.goto(bviewerUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
            }
        } else if (!currentUrl.includes('/bviewer/')) {
            // We're not on a viewer page at all - navigation failed
            if (directViewerUrl) {
                let bviewerUrl = directViewerUrl.replace('/viewer/', '/bviewer/').replace(/\/$/, '');
                await page.goto(bviewerUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
            }
        }

        // 4. Wait for viewer content to load
        await new Promise(r => setTimeout(r, 3000)); // Give viewer time to render

        // Wait for canvas with actual rendered content
        try {
            await viewerPage.waitForFunction(() => {
                // Check for canvas with content
                const canvases = document.querySelectorAll('canvas');
                for (const canvas of canvases) {
                    if (canvas.width > 100 && canvas.height > 100) {
                        return true;
                    }
                }
                // Also check for shadow root elements
                return Array.from(document.querySelectorAll('*')).some(el => el.shadowRoot);
            }, { timeout: 20000 });
        } catch (e) { }

        // Extra wait for the first page image to fully load
        await new Promise(r => setTimeout(r, 8000));

        // 5. Scrape Pages (skip indices that already exist on disk)
        let pageCount = 1;
        let previousScreenshotBuffer = await viewerPage.screenshot({ fullPage: false });
        let isFinished = false;
        let globalCropMargins = null;
        let isFirstSpread = true;
        const seenPageHashes = new Set();
        const recentSpreadHashes = [];
        const hashBuf = (buf) => crypto.createHash('md5').update(buf).digest('hex');
        const MAX_PAGES = parseInt(process.env.MAX_PAGES || '400', 10);
        let announcedResume = existingOnDisk === 0;

        while (!isFinished) {
            if (pageCount > MAX_PAGES) {
                logSticky(`[${cleanTitle}] hit MAX_PAGES=${MAX_PAGES}, stopping`);
                break;
            }

            // End Detection - check for actual viewer end screens, not parent page content
            const viewerState = await viewerPage.evaluate(() => {
                const bodyText = (document.body && document.body.innerText) || '';
                if (
                    bodyText.includes('エラーが発生') ||
                    bodyText.includes('前のページに戻') ||
                    bodyText.includes('エラーが発生したため')
                ) {
                    return { kind: 'error', detail: 'ebookjapan error dialog' };
                }
                if (bodyText.includes('読み終わりました') || bodyText.includes('レビュー投稿')) {
                    return { kind: 'end' };
                }
                const endOverlay = document.querySelector('[class*="finish"], [class*="complete"], [class*="end-screen"]');
                if (endOverlay) return { kind: 'end' };

                const modals = document.querySelectorAll('[class*="modal"], [class*="overlay"], [class*="popup"]');
                for (const modal of modals) {
                    const style = window.getComputedStyle(modal);
                    if (style.display !== 'none' && style.visibility !== 'hidden') {
                        const text = modal.innerText || '';
                        if (text.includes('エラーが発生') || text.includes('前のページに戻')) {
                            return { kind: 'error', detail: 'ebookjapan error dialog' };
                        }
                        if (text.includes('読み終わりました') || text.includes('レビュー投稿')) {
                            return { kind: 'end' };
                        }
                    }
                }
                return { kind: 'ok' };
            });
            if (viewerState.kind === 'error') {
                logError(`[${cleanTitle}] viewer error dialog — aborting scrape (${viewerState.detail})`);
                // Dump a debug screenshot so we can see what the viewer showed.
                try {
                    const dump = path.join(tempOutputDir, `_viewer_error_${Date.now()}.png`);
                    await viewerPage.screenshot({ path: dump, fullPage: false });
                    logSticky(`[${cleanTitle}] saved ${dump}`);
                } catch (e) { /* ignore */ }
                setProgress(cleanTitle, { phase: 'error', detail: 'viewer error dialog' });
                return null;
            }
            if (viewerState.kind === 'end') {
                isFinished = true;
                break;
            }

            const skipping = pageCount < resumeFrom;
            if (!skipping && !announcedResume) {
                logSticky(`[${cleanTitle}] continuing scrape from page ${pageCount}`);
                setProgress(cleanTitle, { scraped: pageCount - 1, phase: 'scraping', detail: '' });
                announcedResume = true;
            }

            const fullScreenshotBuffer = previousScreenshotBuffer;
            const spreadHash = hashBuf(fullScreenshotBuffer);
            // Only treat loops as end once we're past already-scraped pages.
            if (!skipping && recentSpreadHashes.includes(spreadHash) && pageCount > resumeFrom + 3) {
                logSticky(`[${cleanTitle}] end: repeated spread (loop) at page ${pageCount}`);
                isFinished = true;
                break;
            }
            if (!skipping) {
                recentSpreadHashes.push(spreadHash);
                if (recentSpreadHashes.length > 12) recentSpreadHashes.shift();
            }

            const width = VIEWPORT_WIDTH;
            const height = VIEWPORT_HEIGHT;

            const leftBuffer = await viewerPage.screenshot({ clip: { x: 0, y: 0, width: width / 2, height: height } });
            const rightBuffer = await viewerPage.screenshot({ clip: { x: width / 2, y: 0, width: width / 2, height: height } });

            if (!skipping && !globalCropMargins) {
                let candidate = null;
                if (!await isBufferBlank(leftBuffer)) candidate = leftBuffer;
                else if (!await isBufferBlank(rightBuffer)) candidate = rightBuffer;

                if (candidate) {
                    globalCropMargins = await calculateCropMargins(candidate);
                }
            }

            const saveHalf = async (buffer, isRightPage) => {
                if (await isBufferBlank(buffer) && !isFirstSpread) {
                    return false; // skip blank trailing halves
                }

                // Already on disk (or still before resume index): advance without rewriting.
                if (pageCount < resumeFrom || findExistingPagePath(pageDirs, pageCount)) {
                    const existing = findExistingPagePath(pageDirs, pageCount);
                    if (existing && sessionRef.id && pageCount >= resumeFrom) {
                        await pushPage(existing, pageCount);
                    } else {
                        applyPageSnap(cleanTitle, pageCount, null);
                    }
                    pageCount++;
                    return 'skipped';
                }

                const pageHash = hashBuf(buffer);
                if (seenPageHashes.has(pageHash) && pageCount > resumeFrom + 1) {
                    logSticky(`[${cleanTitle}] end: duplicate page content at ${pageCount}`);
                    return 'dup';
                }
                const p = path.join(tempOutputDir, pageFileName(pageCount));
                await saveImage(buffer, p, globalCropMargins, isRightPage);
                seenPageHashes.add(pageHash);
                if (sessionRef.id) {
                    await pushPage(p, pageCount);
                } else {
                    applyPageSnap(cleanTitle, pageCount, null);
                }
                pageCount++;
                return true;
            };

            const isRightBlank = await isBufferBlank(rightBuffer);
            if (!(isFirstSpread && isRightBlank)) {
                const r = await saveHalf(rightBuffer, true);
                if (r === 'dup') {
                    isFinished = true;
                    break;
                }
            }

            const l = await saveHalf(leftBuffer, false);
            if (l === 'dup') {
                isFinished = true;
                break;
            }

            isFirstSpread = false;

            if (pageCount < resumeFrom) {
                setProgress(cleanTitle, {
                    scraped: Math.min(pageCount - 1, existingOnDisk),
                    phase: 'skipping',
                    detail: `→${resumeFrom}`,
                });
            }

            await viewerPage.mouse.click(width * 0.05, height * 0.5);

            const nextBuffer = await waitForContentLoad(viewerPage, fullScreenshotBuffer);
            const settleMs = pageCount < resumeFrom ? 400 : 1000;

            if (nextBuffer.equals(fullScreenshotBuffer)) {
                await new Promise(r => setTimeout(r, settleMs));
                const retryBuffer = await viewerPage.screenshot({ fullPage: false });
                if (retryBuffer.equals(fullScreenshotBuffer)) {
                    if (pageCount < resumeFrom) {
                        logSticky(
                            `[${cleanTitle}] viewer ended while skipping ` +
                                `(at ${pageCount}, wanted ${resumeFrom})`
                        );
                    }
                    isFinished = true;
                    break;
                }
                previousScreenshotBuffer = retryBuffer;
            } else {
                previousScreenshotBuffer = nextBuffer;
            }
        }

        setProgress(cleanTitle, {
            scraped: Math.max(0, pageCount - 1),
            phase: sessionRef.id || useBridge ? 'finalizing' : 'done',
            detail: '',
        });

        const scrapedPages = Math.max(0, pageCount - 1);
        const MIN_PAGES_FOR_MEGA = parseInt(process.env.MIN_PAGES_FOR_MEGA || '10', 10);
        if (useBridge && uploadToMega && scrapedPages < MIN_PAGES_FOR_MEGA) {
            logError(
                `[${cleanTitle}] refusing finalize/MEGA: only ${scrapedPages} page(s) ` +
                    `(min ${MIN_PAGES_FOR_MEGA}) — keeping manga_archives`
            );
            setProgress(cleanTitle, {
                scraped: scrapedPages,
                phase: 'error',
                detail: `too few pages (${scrapedPages})`,
            });
            return {
                path: tempOutputDir,
                title: cleanTitle,
                url: bookUrl,
                sessionId: sessionRef.id,
                mega: null,
                aborted: 'too_few_pages',
            };
        }

        let megaResult = null;
        if (useBridge) {
            try {
                if (!sessionRef.id) {
                    await recoverBridgeSession();
                }
                if (!sessionRef.id) {
                    throw new Error('No bridge session available to finalize');
                }
                megaResult = await bridge.finalizeSession(sessionRef.id, {
                    uploadToMega,
                    deleteAfterUpload: uploadToMega && !keepLocal,
                    onProgress: (msg) => {
                        const phase =
                            msg.stage === 'upload'
                                ? 'uploading'
                                : msg.stage === 'wait_ocr'
                                  ? 'ocr'
                                  : msg.stage === 'assemble' || msg.stage === 'pack'
                                    ? 'packing'
                                    : msg.stage || 'finalizing';
                        setProgress(cleanTitle, {
                            phase,
                            ocrDone: msg.pages_ocr_done,
                            ocrTotal: msg.pages_received || msg.pages,
                            detail: (msg.message || '').slice(0, 36),
                        });
                    },
                });
                setProgress(cleanTitle, {
                    ocrDone: megaResult.pages_ocr_done ?? megaResult.pages,
                    ocrTotal: megaResult.pages,
                    phase: uploadToMega ? 'uploaded' : 'ocr-done',
                    detail: '',
                });
                if (uploadToMega) removeArchiveAfterMegaSuccess(cleanTitle, megaResult, { keepLocal });
            } catch (e) {
                if (bridge.isUnknownSessionError(e)) {
                    try {
                        logSticky(`[${cleanTitle}] finalize session lost — resuming from disk…`);
                        const { final } = await bridge.resumeAndFinalize(cleanTitle, tempOutputDir, {
                            uploadToMega,
                            keepLocal,
                            onProgress: (msg) => {
                                setProgress(cleanTitle, {
                                    phase: msg.stage || 'finalizing',
                                    ocrDone: msg.pages_ocr_done,
                                    ocrTotal: msg.pages_received || msg.pages,
                                    detail: (msg.message || '').slice(0, 36),
                                });
                            },
                        });
                        megaResult = final;
                        setProgress(cleanTitle, {
                            phase: uploadToMega ? 'uploaded' : 'ocr-done',
                            ocrDone: final.pages_ocr_done ?? final.pages,
                            ocrTotal: final.pages,
                            detail: '',
                        });
                        if (uploadToMega) removeArchiveAfterMegaSuccess(cleanTitle, megaResult, { keepLocal });
                    } catch (e2) {
                        logError(`[${cleanTitle}] resume finalize failed: ${e2.message}`);
                        setProgress(cleanTitle, { phase: 'error', detail: e2.message.slice(0, 36) });
                    }
                } else {
                    logError(`[${cleanTitle}] bridge finalize failed: ${e.message}`);
                    setProgress(cleanTitle, { phase: 'error', detail: e.message.slice(0, 36) });
                }
            }
        }

        return {
            path: tempOutputDir,
            title: cleanTitle,
            url: bookUrl,
            sessionId: sessionRef.id,
            mega: megaResult,
        };

    } catch (e) {
        logError(`[Error] ${bookUrl}: ${e.message || e}`);
        return null;
    } finally {
        await browser.close().catch(() => {});
        cleanupLaunchProfile(browser, opts);
    }
};

const runMokuro = (directories) => {
    return new Promise((resolve, reject) => {
        if (directories.length === 0) return resolve();

        console.log(`[Mokuro] Starting processing for ${directories.length} volumes...`);
        // mokuro arg1 arg2 arg3 ...
        const mokuro = spawn('mokuro', directories);

        mokuro.stdout.on('data', (data) => {
            process.stdout.write(data); // Stream output to show progress bar
            const output = data.toString();
            if (output.includes('? [y/N]') || output.includes('confirm')) {
                mokuro.stdin.write('y\n');
            }
        });

        mokuro.stderr.on('data', (data) => {
            process.stderr.write(data);
        });

        mokuro.on('close', (code) => {
            if (code === 0) {
                console.log('\n[Mokuro] Finished successfully.');
                resolve();
            } else {
                console.error(`\n[Mokuro] Exited with code ${code}`);
                // Resolve anyway
                resolve();
            }
        });

        // Pre-emptively send 'y' just in case
        mokuro.stdin.write('y\n');
    });
};

(async () => {
    const args = process.argv.slice(2);
    const flags = new Set(args.filter((a) => a.startsWith('--')));
    const positional = args.filter((a) => !a.startsWith('--'));
    const localOnly = flags.has('--local-only') || !USE_MOKURO_BRIDGE;
    const noMega = flags.has('--no-upload') || flags.has('--no-mega');
    const keepLocal = flags.has('--keep-local');
    const legacyCliMokuro = flags.has('--legacy-mokuro');
    const doResume = flags.has('--resume');

    if (!doResume && positional.length === 0) {
        console.log(`Usage:
  node headlessscript.js <url1> <url2> ... [flags]
  node headlessscript.js --resume <url1> <url2> ...
  node headlessscript.js --resume [title1] [title2] ...

  Scrape ebookjapan → mokuro bridge OCR → MEGA.

  --resume with URLs: continue scraping from existing pages, then OCR + MEGA
  --resume with titles (or no args): finish OCR + MEGA only (no browser)

Flags:
  --resume          continue incomplete volumes (see above)
  --local-only      scrape only (no bridge / OCR / MEGA)
  --no-mega         OCR via bridge, skip MEGA upload
  --keep-local      keep manga_archives + bridge work dir after MEGA upload
  --legacy-mokuro   after all scrapes, run CLI \`mokuro\` instead of bridge

Env:
  CONCURRENCY_LIMIT=10
  MOKURO_BRIDGE_URL=http://127.0.0.1:8765
  MOKURO_BRIDGE=0
  MAX_PAGES=400
`);
        return;
    }

    const captureOpts = {
        useBridge: !localOnly && !legacyCliMokuro,
        uploadToMega: !noMega && !localOnly,
        keepLocal,
    };

    const isUrl = (t) => /^https?:\/\//i.test(t) || /ebookjapan\.yahoo\.co\.jp/i.test(t);
    const resumeUrls = doResume ? positional.filter(isUrl) : [];
    const resumeTitles = doResume ? positional.filter((t) => !isUrl(t)) : [];

    // --resume + URLs → full pipeline (skip existing pages, then OCR + MEGA)
    // --resume + titles / no args → OCR + MEGA only
    const ocrOnlyResume = doResume && resumeUrls.length === 0;

    if (captureOpts.useBridge || doResume) {
        const h = await bridge.ensureBridge();
        logSticky(
            `[Bridge] ready (mokuro=${h.mokuro_installed} mega=${h.mega_configured}` +
                `${h.mokuro_custom_fork ? ' · custom fork' : ''})`
        );
    }

    if (ocrOnlyResume) {
        let titles = resumeTitles.map((t) => path.basename(t.replace(/\/$/, '')));

        if (titles.length === 0) {
            titles = fs.readdirSync(OUTPUT_BASE_DIR).filter((name) => {
                const full = path.join(OUTPUT_BASE_DIR, name);
                if (!fs.statSync(full).isDirectory()) return false;
                if (name.startsWith('_') || name === 'node_modules') return false;
                if (/_[0-9a-f]{6}$/.test(name)) return false;
                return fs.readdirSync(full).some((f) => /\.(webp|jpg|jpeg|png)$/i.test(f));
            });

            const homeInput = path.join(os.homedir(), 'mokuro-input');
            if (fs.existsSync(homeInput)) {
                for (const name of fs.readdirSync(homeInput)) {
                    if (name.startsWith('_') || name.startsWith('.')) continue;
                    const full = path.join(homeInput, name);
                    try {
                        if (!fs.statSync(full).isDirectory()) continue;
                        if (fs.readdirSync(full).some((f) => /\.(webp|jpg|jpeg|png)$/i.test(f))) {
                            if (!titles.includes(name) && !/_[0-9a-f]{6}$/.test(name)) {
                                titles.push(name);
                            }
                        }
                    } catch (e) { /* ignore */ }
                }
            }
        }

        if (titles.length === 0) {
            console.log('Nothing to resume. Pass titles/URLs or keep pages in manga_archives / ~/mokuro-input.');
            return;
        }

        const countImages = (dir) => {
            try {
                return fs.readdirSync(dir).filter((f) => /\.(webp|jpg|jpeg|png)$/i.test(f)).length;
            } catch (e) {
                return 0;
            }
        };
        const homeInput = path.join(os.homedir(), 'mokuro-input');

        logSticky(`Resuming ${titles.length} volume(s) — OCR + MEGA only (no scrape)`);
        startProgressDashboard(1000);
        const results = [];
        try {
            const chunks = [];
            for (let i = 0; i < titles.length; i += CONCURRENCY_LIMIT) {
                chunks.push(titles.slice(i, i + CONCURRENCY_LIMIT));
            }
            for (const chunk of chunks) {
                const part = await Promise.all(chunk.map(async (title) => {
                    const archiveDir = path.join(OUTPUT_BASE_DIR, title);
                    const inputDir = path.join(homeInput, title);
                    const archiveN = countImages(archiveDir);
                    const inputN = countImages(inputDir);
                    const sourceDir =
                        archiveN > inputN && fs.existsSync(archiveDir) ? archiveDir : '';
                    setProgress(title, { scraped: 0, ocrDone: 0, ocrTotal: 0, phase: 'resuming' });
                    try {
                        const { resumed, final } = await bridge.resumeAndFinalize(title, sourceDir, {
                            uploadToMega: captureOpts.uploadToMega,
                            keepLocal: keepLocal,
                            onProgress: (msg) => {
                                setProgress(title, {
                                    phase: msg.stage === 'resume' ? 'resuming' : (msg.stage || 'ocr'),
                                    scraped: msg.pages_received || msg.pages || 0,
                                    ocrDone: msg.pages_ocr_done ?? msg.ocr_cached,
                                    ocrTotal: msg.pages_received || msg.pages,
                                    detail: (msg.message || '').slice(0, 40),
                                });
                            },
                        });
                        setProgress(title, {
                            scraped: final.pages || resumed.pages_received || 0,
                            ocrDone: final.pages_ocr_done ?? final.pages,
                            ocrTotal: final.pages,
                            phase: captureOpts.uploadToMega ? 'uploaded' : 'ocr-done',
                            detail: '',
                        });
                        if (captureOpts.uploadToMega) {
                            removeArchiveAfterMegaSuccess(title, final, { keepLocal });
                        }
                        logSticky(
                            `[${title}] ${final.message || 'done'} ` +
                                `(was cached=${resumed.ocr_cached} queued=${resumed.queued_for_ocr})`
                        );
                        return { title, final, resumed };
                    } catch (e) {
                        logError(`[${title}] resume failed: ${e.message}`);
                        setProgress(title, { phase: 'error', detail: e.message.slice(0, 36) });
                        return null;
                    }
                }));
                results.push(...part.filter(Boolean));
            }
        } finally {
            stopProgressDashboard();
        }
        console.log(`Resume done. ok=${results.length}/${titles.length}`);
        return;
    }

    // Full pipeline: scrape (skip existing pages) → OCR → MEGA
    const scrapeInputs = resumeUrls.length ? resumeUrls : positional;
    if (doResume && resumeUrls.length) {
        logSticky('Continuing scrape from existing pages, then OCR + MEGA');
        if (resumeTitles.length) {
            logSticky(`(ignoring bare titles with URL resume: ${resumeTitles.join(', ')})`);
        }
    }

    logSticky(`[Browser] ${browserLabel(PUPPETEER_EXECUTABLE_PATH)}`);

    if (!captureOpts.useBridge) {
        logSticky('[Bridge] skipped (--local-only / --legacy-mokuro / MOKURO_BRIDGE=0)');
    }

    let allBookUrls = [];
    for (const url of scrapeInputs) {
        if (/\/books\/\d+\/A\d+/.test(url)) {
            allBookUrls.push(url);
        } else if (isUrl(url) || /ebookjapan/.test(url)) {
            logSticky(`[Input] Series: ${url}`);
            const discovered = await discoverVolumes(url);
            allBookUrls = allBookUrls.concat(discovered);
        } else {
            logError(`[Input] Not a URL (did you mean --resume '${url}' for OCR-only?): ${url}`);
        }
    }

    allBookUrls = [...new Set(allBookUrls)];
    if (allBookUrls.length === 0) {
        console.log('No volumes found to process.');
        return;
    }

    logSticky(
        `Starting ${allBookUrls.length} volumes (concurrency=${CONCURRENCY_LIMIT}` +
            `, bridge=${captureOpts.useBridge}, mega=${captureOpts.uploadToMega}` +
            `${doResume ? ', resume-scrape' : ''})`
    );

    startProgressDashboard(1000);

    const chunks = [];
    for (let i = 0; i < allBookUrls.length; i += CONCURRENCY_LIMIT) {
        chunks.push(allBookUrls.slice(i, i + CONCURRENCY_LIMIT));
    }

    const processedItems = [];

    try {
        for (const chunk of chunks) {
            const results = await Promise.all(chunk.map((url) => captureManga(url, captureOpts)));
            results.forEach((r) => {
                if (r) processedItems.push(r);
            });
        }
    } finally {
        stopProgressDashboard();
    }

    if (legacyCliMokuro && processedItems.length > 0) {
        await runMokuro(processedItems.map((i) => i.path));
    }

    const uploaded = processedItems.filter((i) => i.mega && i.mega.status === 'success').length;
    console.log(
        `Done. scraped=${processedItems.length}` +
            (captureOpts.useBridge ? ` mega_ok=${uploaded}` : '')
    );
})();