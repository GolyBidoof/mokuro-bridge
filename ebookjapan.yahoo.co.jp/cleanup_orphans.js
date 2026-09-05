const fs = require('fs');
const path = require('path');

const ARCHIVE_DIR = path.join(__dirname, 'manga_archives');

if (!fs.existsSync(ARCHIVE_DIR)) {
    console.error(`Directory not found: ${ARCHIVE_DIR}`);
    process.exit(1);
}

const items = fs.readdirSync(ARCHIVE_DIR);
let removedCount = 0;

items.forEach(item => {
    const itemPath = path.join(ARCHIVE_DIR, item);

    // We are looking for .mokuro and .html files
    if (fs.statSync(itemPath).isFile() && (item.endsWith('.mokuro') || item.endsWith('.html'))) {
        const baseName = path.basename(item, path.extname(item));
        const correspondingDir = path.join(ARCHIVE_DIR, baseName);

        // Check if the corresponding directory exists
        if (!fs.existsSync(correspondingDir)) {
            console.log(`[Orphan Found] ${item} has no corresponding directory. Removing...`);
            fs.unlinkSync(itemPath);
            removedCount++;
        } else {
            // Check if directory is empty or has no images (optional, based on "missing image files")
            // The user said "completely missing image files", implying the directory might be gone or empty.
            // If the directory exists, we should check if it has content.
            const dirContents = fs.readdirSync(correspondingDir);
            if (dirContents.length === 0) {
                console.log(`[Empty Dir Found] ${baseName} directory is empty. Removing ${item} and directory...`);
                fs.unlinkSync(itemPath);
                fs.rmdirSync(correspondingDir);
                removedCount++;
            }
        }
    }
});

console.log(`\nCleanup complete. Removed ${removedCount} orphaned/empty items.`);
