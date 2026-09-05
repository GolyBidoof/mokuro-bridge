/**
 * Client for the local bw-mokuro-bridge (http://127.0.0.1:8765).
 * Used by ebookjapan headless captures so OCR + MEGA run in parallel
 * across many titles (shared OCR queue on the bridge).
 */
const fs = require('fs');
const path = require('path');

const BRIDGE_URL = process.env.MOKURO_BRIDGE_URL || 'http://127.0.0.1:8765';

function isUnknownSessionError(err) {
    const msg = String((err && err.message) || err || '');
    return /unknown session/i.test(msg) || /finalize HTTP 404/i.test(msg);
}

async function health() {
    const res = await fetch(`${BRIDGE_URL}/health`, { cache: 'no-store' });
    if (!res.ok) throw new Error(`Bridge health HTTP ${res.status}`);
    return res.json();
}

async function ensureBridge({ timeoutMs = 10000 } = {}) {
    const start = Date.now();
    let lastErr;
    while (Date.now() - start < timeoutMs) {
        try {
            const h = await health();
            if (h.status === 'ok' && h.mokuro_installed) return h;
            lastErr = new Error(`Bridge up but mokuro missing: ${JSON.stringify(h)}`);
        } catch (e) {
            lastErr = e;
        }
        await new Promise((r) => setTimeout(r, 500));
    }
    throw new Error(
        `Mokuro bridge not reachable at ${BRIDGE_URL}. Start it with ~/Projects/bw-mokuro-bridge/run.sh\n` +
            `Last error: ${lastErr && lastErr.message}`
    );
}

async function startSession(title, { reuseExisting = false } = {}) {
    const form = new FormData();
    form.append('title', title || 'manga');
    form.append('reuse_existing', reuseExisting ? 'true' : 'false');
    const res = await fetch(`${BRIDGE_URL}/session/start`, { method: 'POST', body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `session/start HTTP ${res.status}`);
    return data;
}

/**
 * Resume a partially scraped/OCR'd volume.
 * sourceDir: optional manga_archives path to sync newer pages from.
 */
async function resumeSession(title, sourceDir = '') {
    const form = new FormData();
    form.append('title', title || 'manga');
    if (sourceDir) form.append('source_dir', path.resolve(sourceDir));
    const res = await fetch(`${BRIDGE_URL}/session/resume`, { method: 'POST', body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `session/resume HTTP ${res.status}`);
    return data;
}

/** Same-machine: tell the bridge to copy a local image and queue OCR. */
async function pushPageLocal(sessionId, filePath, pageNum) {
    const form = new FormData();
    form.append('path', path.resolve(filePath));
    form.append('filename', path.basename(filePath));
    form.append('page_num', String(pageNum));
    const res = await fetch(`${BRIDGE_URL}/session/${sessionId}/page-local`, {
        method: 'POST',
        body: form,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `page-local HTTP ${res.status}`);
    return data;
}

async function sessionStatus(sessionId) {
    const res = await fetch(`${BRIDGE_URL}/session/${sessionId}/status`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `status HTTP ${res.status}`);
    return data;
}

async function listSessions() {
    const res = await fetch(`${BRIDGE_URL}/sessions`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `sessions HTTP ${res.status}`);
    return data;
}

async function finalizeSession(
    sessionId,
    { uploadToMega = true, deleteAfterUpload = true, onProgress = null } = {}
) {
    const form = new FormData();
    form.append('upload_to_mega', uploadToMega ? 'true' : 'false');
    form.append('delete_after_upload', deleteAfterUpload ? 'true' : 'false');

    const res = await fetch(`${BRIDGE_URL}/session/${sessionId}/finalize`, {
        method: 'POST',
        body: form,
    });
    if (!res.ok) {
        const text = await res.text();
        throw new Error(`finalize HTTP ${res.status}: ${text.slice(0, 300)}`);
    }
    if (!res.body) throw new Error('No finalize response body');

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let final = null;

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
            if (!line.trim()) continue;
            let msg;
            try {
                msg = JSON.parse(line);
            } catch {
                continue;
            }
            if (typeof onProgress === 'function') onProgress(msg);
            if (msg.stage === 'done') final = msg;
            if (msg.stage === 'error') {
                throw new Error(msg.message || 'Bridge finalize error');
            }
        }
    }
    if (buffer.trim()) {
        const msg = JSON.parse(buffer);
        if (typeof onProgress === 'function') onProgress(msg);
        if (msg.stage === 'done') final = msg;
        if (msg.stage === 'error') throw new Error(msg.message || 'Bridge finalize error');
    }
    if (!final) throw new Error('Bridge finalize ended without done/error');
    return final;
}

/**
 * After a page is written to disk, push it into the mokuro session.
 * On unknown-session, caller should recreate via resumeSession.
 */
async function safePushPage(sessionId, filePath, pageNum) {
    if (!sessionId || !fs.existsSync(filePath)) return null;
    try {
        return await pushPageLocal(sessionId, filePath, pageNum);
    } catch (e) {
        return { __error: e.message, __unknownSession: isUnknownSessionError(e) };
    }
}

/**
 * Full resume: sync archives → bridge, finish OCR, optional MEGA.
 */
async function resumeAndFinalize(title, sourceDir, { uploadToMega = true, keepLocal = false, onProgress = null } = {}) {
    const resumed = await resumeSession(title, sourceDir || '');
    if (onProgress) {
        onProgress({
            stage: 'resume',
            message: `synced=${resumed.synced_from_source} cached=${resumed.ocr_cached} queued=${resumed.queued_for_ocr}`,
            ...resumed,
        });
    }
    const final = await finalizeSession(resumed.session_id, {
        uploadToMega,
        deleteAfterUpload: uploadToMega && !keepLocal,
        onProgress,
    });
    return { resumed, final };
}

module.exports = {
    BRIDGE_URL,
    health,
    ensureBridge,
    startSession,
    resumeSession,
    resumeAndFinalize,
    pushPageLocal,
    sessionStatus,
    listSessions,
    finalizeSession,
    safePushPage,
    isUnknownSessionError,
};
