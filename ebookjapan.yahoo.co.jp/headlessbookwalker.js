const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');
const archiver = require('archiver');
const { PNG } = require('pngjs');
const sharp = require('sharp'); // Use sharp for fast WebP conversion
const { spawn } = require('child_process');

// Configuration
const CONCURRENCY_LIMIT = parseInt(process.env.CONCURRENCY_LIMIT) || 12;
const OUTPUT_BASE_DIR = process.env.OUTPUT_BASE_DIR || path.join(__dirname, 'manga_archives');
const DEVICE_SCALE_FACTOR = parseInt(process.env.DEVICE_SCALE_FACTOR) || 2;
const VIEWPORT_WIDTH = parseInt(process.env.VIEWPORT_WIDTH) || 1414;
const VIEWPORT_HEIGHT = parseInt(process.env.VIEWPORT_HEIGHT) || 1000;
const PUPPETEER_EXECUTABLE_PATH = require('./resolve_browser').resolveBrowserExecutable();



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

const discoverVolumes = async (seriesUrl) => {
    const browser = await puppeteer.launch({
        headless: "new",
        executablePath: PUPPETEER_EXECUTABLE_PATH,
        defaultViewport: null,
        args: [
            '--start-maximized',
            '--disable-web-security',
            '--disable-features=IsolateOrigins,site-per-process',
            '--disable-blink-features=AutomationControlled'
        ]
    });
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

    await browser.close();
    return Array.from(allLinks);
};

const captureManga = async (bookUrl) => {
    const browser = await puppeteer.launch({
        headless: "new",
        executablePath: PUPPETEER_EXECUTABLE_PATH,
        defaultViewport: null,
        args: [
            '--start-maximized',
            '--disable-web-security',
            '--disable-features=IsolateOrigins,site-per-process',
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
            '--disable-blink-features=AutomationControlled'
        ]
    });

    try {
        const page = await browser.newPage();
        await page.setViewport({ width: VIEWPORT_WIDTH, height: VIEWPORT_HEIGHT, deviceScaleFactor: DEVICE_SCALE_FACTOR });
        await page.setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
        await page.bringToFront();

        console.log(`[Start] Book Page: ${bookUrl}`);
        await page.goto(bookUrl, { waitUntil: 'networkidle0' });

        // 1. Extract Title
        let rawTitle = await page.evaluate(() => {
            const h1 = document.querySelector('h1.book-main__heading');
            return h1 ? h1.innerText.trim() : document.title;
        });

        let cleanTitle = rawTitle
            .replace(/ebookjapan|無料|まんが|電子書籍/gi, '')
            .replace(/[|\-]/g, ' ')
            .replace(/[\/\\?%*:|"<>]/g, '')
            .replace(/\s+/g, ' ')
            .trim();

        if (!cleanTitle) cleanTitle = `manga_${Date.now()}`;
        console.log(`[Title] "${rawTitle}" -> "${cleanTitle}"`);

        const tempOutputDir = path.join(OUTPUT_BASE_DIR, cleanTitle);
        const outputZipPath = path.join(OUTPUT_BASE_DIR, `${cleanTitle}.zip`);

        // Check if Zip exists (Overwrite logic)
        if (fs.existsSync(outputZipPath)) {
            console.log(`[Overwrite] Removing existing ${cleanTitle}.zip`);
            fs.unlinkSync(outputZipPath);
        }

        // Clean/Create Temp Dir
        if (fs.existsSync(tempOutputDir)) fs.rmSync(tempOutputDir, { recursive: true, force: true });
        fs.mkdirSync(tempOutputDir);

        // 2. Click "Read"
        const viewerTargetPromise = new Promise(resolve => browser.once('targetcreated', resolve));

        const clicked = await page.evaluate(() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const readBtn = buttons.find(b => b.innerText.includes('無料で読む') || b.innerText.includes('読む') || b.innerText.includes('試し読み'));
            if (readBtn) {
                readBtn.click();
                return true;
            }
            return false;
        });

        if (!clicked) {
            console.error(`[Error] Could not find 'Read' button on ${bookUrl}`);
            await browser.close();
            return null;
        }

        // 3. Handle Navigation
        let viewerPage = page;
        const newTarget = await Promise.race([
            viewerTargetPromise,
            new Promise(resolve => setTimeout(() => resolve(null), 2000))
        ]);

        if (newTarget && newTarget.type() === 'page') {
            viewerPage = await newTarget.page();
            await viewerPage.setViewport({ width: VIEWPORT_WIDTH, height: VIEWPORT_HEIGHT, deviceScaleFactor: DEVICE_SCALE_FACTOR });
            await viewerPage.bringToFront();
        }

        // 4. Wait for Load
        console.log(`[Viewer] Waiting for load...`);
        try {
            await viewerPage.waitForFunction(() => {
                return !document.title.includes('漫画・試し読みが豊富');
            }, { timeout: 10000 });
        } catch (e) { }
        await new Promise(r => setTimeout(r, 1000));

        // 5. Scrape Pages
        let pageCount = 1;
        let previousScreenshotBuffer = await viewerPage.screenshot({ fullPage: false });
        let isFinished = false;
        let globalCropMargins = null;
        let isFirstSpread = true;

        while (!isFinished) {
            // End Detection
            const isEnd = await viewerPage.evaluate(() => {
                const t = document.body.innerText;
                return t.includes('読み終わりました') || t.includes('次の巻') || t.includes('レビュー投稿') || !!document.querySelector('div[class*="end"]');
            });
            if (isEnd) {
                isFinished = true;
                break;
            }

            const fullScreenshotBuffer = previousScreenshotBuffer;
            const width = VIEWPORT_WIDTH;
            const height = VIEWPORT_HEIGHT;

            const leftBuffer = await viewerPage.screenshot({ clip: { x: 0, y: 0, width: width / 2, height: height } });
            const rightBuffer = await viewerPage.screenshot({ clip: { x: width / 2, y: 0, width: width / 2, height: height } });

            if (!globalCropMargins) {
                let candidate = null;
                if (!await isBufferBlank(leftBuffer)) candidate = leftBuffer;
                else if (!await isBufferBlank(rightBuffer)) candidate = rightBuffer;

                if (candidate) {
                    globalCropMargins = await calculateCropMargins(candidate);
                    console.log(`[${cleanTitle}] Crop Margins: T:${globalCropMargins.top} B:${globalCropMargins.bottom} L:${globalCropMargins.left} R:${globalCropMargins.right}`);
                }
            }

            const isRightBlank = await isBufferBlank(rightBuffer);
            if (isFirstSpread && isRightBlank) {
                // Skip first right blank page
            } else {
                const p = path.join(tempOutputDir, `page_${String(pageCount).padStart(3, '0')}.webp`);
                await saveImage(rightBuffer, p, globalCropMargins, true);
                pageCount++;
            }

            const p = path.join(tempOutputDir, `page_${String(pageCount).padStart(3, '0')}.webp`);
            await saveImage(leftBuffer, p, globalCropMargins, false);
            pageCount++;

            isFirstSpread = false;

            await viewerPage.mouse.click(width * 0.05, height * 0.5);

            const nextBuffer = await waitForContentLoad(viewerPage, fullScreenshotBuffer);

            if (nextBuffer.equals(fullScreenshotBuffer)) {
                await new Promise(r => setTimeout(r, 1000));
                const retryBuffer = await viewerPage.screenshot({ fullPage: false });
                if (retryBuffer.equals(fullScreenshotBuffer)) {
                    console.log(`[${cleanTitle}] Visual end detected.`);
                    isFinished = true;
                    break;
                }
                previousScreenshotBuffer = retryBuffer;
            } else {
                previousScreenshotBuffer = nextBuffer;
            }
        }

        console.log(`[Scraped] ${cleanTitle}`);
        return { path: tempOutputDir, title: cleanTitle, url: bookUrl };

    } catch (e) {
        console.error(`[Error] ${bookUrl}:`, e);
        return null;
    } finally {
        await browser.close();
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

const uploadMokuro = async (processedItems) => {
    console.log('\n[Upload] === Starting Upload Process ===');
    const cookiesPath = path.join(__dirname, 'cookies.json');
    let cookies = {};
    if (fs.existsSync(cookiesPath)) {
        try {
            cookies = JSON.parse(fs.readFileSync(cookiesPath, 'utf8'));
        } catch (e) {
            console.error('[Upload] Failed to parse cookies.json:', e.message);
        }
    } else {
        console.warn('[Upload] cookies.json not found. Upload might fail if authentication is required.');
    }

    const cookieString = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; ');

    // 1. Get CSRF Token
    let csrfToken = '';
    try {
        console.log('[Upload] Fetching CSRF token...');
        const response = await fetch('https://manga-kotoba.com/contribute', {
            headers: {
                'Cookie': cookieString,
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
        });
        const html = await response.text();
        const match = html.match(/<meta name="csrf-token" content="([^"]+)">/);
        if (match && match[1]) {
            csrfToken = match[1];
            console.log('[Upload] CSRF Token retrieved.');
        } else {
            console.error('[Upload] Could not find CSRF token in page.');
        }
    } catch (e) {
        console.error('[Upload] Error fetching page for CSRF token:', e.message);
        return;
    }

    // 2. Prepare Upload Queue
    const uploadQueue = [];
    for (const item of processedItems) {
        const dir = item.path;
        const mokuroPath = `${dir}.mokuro`;

        if (fs.existsSync(mokuroPath)) {
            try {
                const content = fs.readFileSync(mokuroPath, 'utf8');
                JSON.parse(content); // Validate JSON
                uploadQueue.push({
                    filePath: mokuroPath,
                    filename: path.basename(mokuroPath),
                    url: item.url
                });
            } catch (e) {
                console.error(`[Upload] Invalid .mokuro file at ${mokuroPath}: ${e.message}`);
            }
        } else {
            console.log(`[Upload] No .mokuro file found at ${mokuroPath}`);
        }
    }

    console.log(`[Upload] Found ${uploadQueue.length} files to upload.`);
    let successCount = 0;
    let failCount = 0;

    // 3. Upload Files
    for (let i = 0; i < uploadQueue.length; i++) {
        const { filePath, filename, url } = uploadQueue[i];

        console.log(`[Upload] [${i + 1}/${uploadQueue.length}] Uploading: ${filename}...`);

        try {
            const content = fs.readFileSync(filePath, 'utf8');

            const payload = {
                userComments: url || "",
                mokuroFiles: [
                    {
                        name: filename,
                        content: content
                    }
                ]
            };

            const uploadRes = await fetch('https://manga-kotoba.com/api/contribute', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Cookie': cookieString,
                    'X-CSRF-Token': csrfToken,
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Origin': 'https://manga-kotoba.com',
                    'Referer': 'https://manga-kotoba.com/contribute'
                },
                body: JSON.stringify(payload)
            });

            if (uploadRes.ok) {
                console.log(`[Upload] ✅ Success: ${filename}`);
                successCount++;
            } else {
                console.error(`[Upload] ❌ Failed: ${filename} (Status: ${uploadRes.status} ${uploadRes.statusText})`);
                const errText = await uploadRes.text();
                console.error(`[Upload] Server Response: ${errText}`);
                failCount++;
            }

        } catch (e) {
            console.error(`[Upload] ❌ Error processing ${filename}:`, e.message);
            failCount++;
        }
    }

    console.log('\n[Upload] === Summary ===');
    console.log(`Total: ${uploadQueue.length}`);
    console.log(`Success: ${successCount}`);
    console.log(`Failed: ${failCount}`);
    console.log('=======================\n');
};

(async () => {
    const args = process.argv.slice(2);
    const shouldUpload = !args.includes('--no-upload');
    const inputUrls = args.filter(arg => arg !== '--no-upload');

    if (inputUrls.length === 0) {
        console.log('Usage: node headlessscript.js <url1> <url2> ... [--no-upload]');
        return;
    }

    let allBookUrls = [];
    for (const url of inputUrls) {
        if (/\/books\/\d+\/A\d+/.test(url)) {
            console.log(`[Input] Identified as Book: ${url}`);
            allBookUrls.push(url);
        } else {
            console.log(`[Input] Identified as Series: ${url}`);
            const discovered = await discoverVolumes(url);
            allBookUrls = allBookUrls.concat(discovered);
        }
    }

    allBookUrls = [...new Set(allBookUrls)];
    if (allBookUrls.length === 0) {
        console.log('No volumes found to process.');
        return;
    }

    console.log(`Starting processing for ${allBookUrls.length} volumes...`);

    const chunks = [];
    for (let i = 0; i < allBookUrls.length; i += CONCURRENCY_LIMIT) {
        chunks.push(allBookUrls.slice(i, i + CONCURRENCY_LIMIT));
    }

    const processedItems = [];

    for (const chunk of chunks) {
        const results = await Promise.all(chunk.map(url => captureManga(url)));
        results.forEach(r => {
            if (r) processedItems.push(r);
        });
    }

    if (processedItems.length > 0) {
        await runMokuro(processedItems.map(i => i.path));
        if (shouldUpload) {
            await uploadMokuro(processedItems);
        } else {
            console.log('\n[Upload] Skipped due to --no-upload flag.');
        }
    }

    console.log('All tasks completed.');
})();