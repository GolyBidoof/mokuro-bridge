# BookWalker headless → mokuro bridge → MEGA

Same OCR + MEGA pipeline as ebookjapan (`mokuro_bridge` → local FastAPI → series folder on MEGA).

## Setup

```bash
# bridge
~/Projects/bw-mokuro-bridge/run.sh

# cookies: export from Chrome (EditThisCookie / DevTools) while logged into bookwalker.jp
cp cookies.example.json cookies.json
# edit cookies.json
```

Cookies must include your BookWalker session for `*.bookwalker.jp`.

## Usage

```bash
cd ~/Projects/bw-mokuro-bridge/bookwalker

# one volume (viewer URL)
node headless.js --cookies cookies.json \
  'https://viewer.bookwalker.jp/#!...'

# several volumes (default concurrency=2 — safer for BW session locks)
node headless.js --cookies cookies.json \
  'https://viewer.bookwalker.jp/#!vol1' \
  'https://viewer.bookwalker.jp/#!vol2'

# resume scrape from manga_archives pages + finish OCR/MEGA
node headless.js --cookies cookies.json --resume \
  'https://viewer.bookwalker.jp/#!...'

# OCR+MEGA only (no browser) for titles already on disk
node headless.js --resume 'メダリスト 1巻' 'メダリスト 2巻'
```

Flags: `--local-only`, `--no-mega`, `--keep-local`, `--concurrency N`

Env: `CONCURRENCY_LIMIT` (default **2**), `MOKURO_BRIDGE_URL`, `MAX_PAGES`, `MIN_PAGES_FOR_MEGA`

## Notes

- BookWalker often **locks concurrent viewer sessions** on one account. Start with concurrency 1–2.
- Pages land in `manga_archives/<title>/`, OCR workdir `~/mokuro-input/<title>/`, MEGA `/Root/mokuro-reader/<series>/`.
- Requires Puppeteer from `../ebookjapan.yahoo.co.jp/node_modules` (already installed there).
