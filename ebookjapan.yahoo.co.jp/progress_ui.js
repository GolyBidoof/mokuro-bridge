/**
 * Single in-place multi-title progress board for the terminal.
 * New frames erase the previous board (TTY). Sticky logs clear+repaint around it.
 */
const progressByTitle = new Map();
let drawnLines = 0;
let paintTimer = null;
let dashboardTimer = null;
// Cursor/some hosts leave isTTY undefined; still allow in-place if TERM is set.
const isTTY =
    process.stdout.isTTY === true ||
    process.env.PROGRESS_IN_PLACE === '1' ||
    (process.stdout.isTTY !== false && Boolean(process.env.TERM));

function shortTitle(title, max = 28) {
    const t = String(title || '');
    return t.length <= max ? t : t.slice(0, max - 1) + '…';
}

function setProgress(title, patch) {
    const prev = progressByTitle.get(title) || {
        scraped: 0,
        ocrDone: 0,
        ocrTotal: 0,
        phase: 'starting',
        detail: '',
    };
    progressByTitle.set(title, { ...prev, ...patch, updatedAt: Date.now() });
    schedulePaint();
}

function clearDrawn() {
    if (!isTTY || drawnLines <= 0) return;
    // Move to start of board, clear downward
    process.stdout.write(`\x1b[${drawnLines}A`);
    for (let i = 0; i < drawnLines; i++) {
        process.stdout.write('\x1b[2K\n');
    }
    process.stdout.write(`\x1b[${drawnLines}A`);
    drawnLines = 0;
}

function buildLines() {
    if (progressByTitle.size === 0) return [];
    const lines = ['── progress ──────────────────────────────────────────'];
    // Stable order: insertion order of Map
    for (const [title, p] of progressByTitle) {
        const ocr =
            p.ocrTotal > 0
                ? `OCR ${String(p.ocrDone).padStart(3)}/${String(p.ocrTotal).padStart(3)}`
                : 'OCR   —/  —';
        const detail = p.detail ? `  ${p.detail}` : '';
        lines.push(
            `  ${shortTitle(title).padEnd(28)}  scrape ${String(p.scraped).padStart(3)}  ${ocr}  [${p.phase}]${detail}`
        );
    }
    lines.push('────────────────────────────────────────────────────');
    return lines;
}

function paintProgress({ force = false } = {}) {
    paintTimer = null;
    const lines = buildLines();
    if (lines.length === 0) {
        clearDrawn();
        return;
    }
    if (isTTY) {
        clearDrawn();
        process.stdout.write(lines.join('\n') + '\n');
        drawnLines = lines.length;
    } else if (force) {
        // Non-TTY (piped logs): only emit when forced (interval / stop)
        process.stdout.write(lines.join('\n') + '\n');
    }
}

function schedulePaint() {
    if (paintTimer) return;
    paintTimer = setTimeout(() => paintProgress(), 80);
    if (paintTimer.unref) paintTimer.unref();
}

/** Permanent one-line message above the live board. */
function logSticky(message) {
    clearDrawn();
    console.log(message);
    paintProgress({ force: true });
}

function logError(message) {
    clearDrawn();
    console.error(message);
    paintProgress({ force: true });
}

function startProgressDashboard(intervalMs = 1000) {
    stopProgressDashboard(false);
    // Keep board fresh even if setProgress stops briefly; also for non-TTY
    dashboardTimer = setInterval(() => paintProgress({ force: !isTTY }), intervalMs);
    if (dashboardTimer.unref) dashboardTimer.unref();
    paintProgress({ force: true });
}

function stopProgressDashboard(finalPaint = true) {
    if (paintTimer) {
        clearTimeout(paintTimer);
        paintTimer = null;
    }
    if (dashboardTimer) {
        clearInterval(dashboardTimer);
        dashboardTimer = null;
    }
    if (finalPaint && progressByTitle.size) {
        paintProgress({ force: true });
    }
}

function clearProgressState() {
    clearDrawn();
    progressByTitle.clear();
}

module.exports = {
    setProgress,
    logSticky,
    logError,
    startProgressDashboard,
    stopProgressDashboard,
    clearProgressState,
    paintProgress,
    progressByTitle,
};
