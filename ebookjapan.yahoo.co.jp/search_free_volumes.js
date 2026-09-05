const puppeteer = require('puppeteer');
const fs = require('fs');

const PUPPETEER_EXECUTABLE_PATH = require('./resolve_browser').resolveBrowserExecutable();

// Manga titles with authors (parsed from title/author pairs)
const MANGA_LIST = [
    { title: "おままごとのおわり。　さかさな短編集", author: "さかさな" },
    { title: "女ともだちと結婚してみた。", author: "雨水汐" },
    { title: "欠けた月とドーナッツ", author: "雨水汐" },
    { title: "カナリアは綺羅星の夢をみる", author: "sheepD" },
    { title: "カヌレ　スール百合アンソロジー", author: "アンソロジー" },
    { title: "崖っぷち令嬢は黒騎士様を惚れさせたい！", author: "漫画：そめちめ　原作：洲央" },
    { title: "ガラスの靴を脱ぎ捨てて", author: "桐山はるか" },
    { title: "伽藍の姫-がらんのひめ-", author: "こるせ" },
    { title: "きたない君がいちばんかわいい", author: "まにお" },
    { title: "昨日シたのに覚えてないの？ 百合えっち短編集", author: "焼肉定食" },
    { title: "きみが死ぬまで恋をしたい", author: "あおのなち" },
    { title: "キミが吠えるための歌を、", author: "樫風" },
    { title: "君としらない夏になる", author: "きぃやん" },
    { title: "きみと世界の終りを訪ねて", author: "こるせ" },
    { title: "君と綴るうたかた", author: "ゆあま" },
    { title: "君のせいなんだから、責任とってよね。", author: "当麻" },
    { title: "きみのために世界はある", author: "雨水汐" },
    { title: "教師×生徒の百合アンソロジーコミック", author: "アンソロジー" },
    { title: "今日はカノジョがいないから", author: "岩見樹代子" },
    { title: "今日はまだフツーになれない", author: "U-temo" },
    { title: "今日もひとつ屋根の下", author: "犬井あゆ" },
    { title: "嫌われ魔女令嬢と男装皇子の婚約", author: "散葉ちんみ" },
    { title: "ギャルメイドと悪役令嬢 ～おじょーさまのハッピーエンドしか勝たん！～", author: "鍵穴" },
    { title: "クダンノフォークロア", author: "漫画：煮汁　原作：志水はつみ　原案監修：SukeraSparo" },
    { title: "グッバイ・ディストピア", author: "ひそな" },
    { title: "行進子犬に恋文を", author: "玉崎たま" },
    { title: "コスプレ百合えっちアンソロジー", author: "アンソロジー" },
    { title: "この世で一番素敵な終わり方", author: "シクシク" },
    { title: "小春と湊", author: "ひあるろん＆達磨" },
    { title: "ささやくように恋を唄う", author: "竹嶋えく" },
    { title: "ささやくように恋を唄う　公式コミックアンソロジー", author: "アンソロジー" },
    { title: "サボりなら保健室でどうぞ？", author: "あおと響" },
    { title: "サラダボウル", author: "きぃやん" },
    { title: "サルビアのブーケ", author: "漫画：古賀由人　原作：4kaえんぴつ" },
    { title: "しかばね少女と愛が重い聖騎士の討伐学園ライフ", author: "日野アラシ" },
    { title: "citrus+", author: "サブロウタ" },
    { title: "SHIBUYA　ギャル百合アンソロジー", author: "アンソロジー" },
    { title: "終末世界百合アンソロジー", author: "アンソロジー" },
    { title: "ショコラ2 社会人百合アンソロジー", author: "アンソロジー" },
    { title: "女子高生と王子ちゃん", author: "くうねりん" },
    { title: "スカーレット", author: "結野ちり" },
    { title: "好きだからHしてます。", author: "コダマナオコ" },
    { title: "ストレンジベイビーズ 完全版", author: "大沢やよい" },
    { title: "ストロベリーパルフェ おねロリ百合アンソロジー", author: "アンソロジー" },
    { title: "スピカをつかまえて", author: "織日ちひろ" },
    { title: "すれ違い巨大感情百合アンソロジー", author: "アンソロジー" },
    { title: "セメルパルス　semelparous", author: "荻野純" },
    { title: "ぜんぶ壊して地獄で愛して", author: "くわばらたもつ" },
    { title: "それは、春の嵐のように", author: "くるくる姫" },
    { title: "橘館Ce Lebらいふ", author: "merryhachi" },
    { title: "立花館To Lieあんぐる", author: "merryhachi" },
    { title: "たとえとどかぬ糸だとしても", author: "tMnR" },
    { title: "たゆたう恋の散り際に", author: "ゆあま" },
    { title: "誰かオオカミさんのしつけ方知りませんか！？", author: "餡実ツキ" },
    { title: "超深宇宙より愛をこめて", author: "アシダカヲズ" },
    { title: "月が綺麗ですね", author: "伊藤ハチ" },
    { title: "月と恋は満ちれば欠ける。", author: "トクヲツム" },
    { title: "徒然日和", author: "土室圭" },
    { title: "ツン姫さまとダメ王子ちゃん", author: "ヨウハ" },
    { title: "転生したらあかりだけスライムだった件", author: "漫画：水鳥なや　原作：なもり" },
    { title: "遠山えま百合集　センセイとの時間。", author: "遠山えま" },
    { title: "隣の席が好きな人だった　学生百合アンソロジー", author: "アンソロジー" },
    { title: "同人女百合アンソロジー", author: "アンソロジー" },
    { title: "泣き顔百合アンソロジー", author: "アンソロジー" },
    { title: "夏とレモンとオーバーレイ", author: "漫画：宮原都　原作：Ru" },
    { title: "ナメられたくないナメカワさん", author: "阿東里枝" },
    { title: "奈落の花園", author: "さかさな" },
    { title: "2DK、Gペン、目覚まし時計。", author: "大沢やよい" },
    { title: "捏造トラップ-NTR-", author: "コダマナオコ" },
    { title: "NTR　寝取られ百合アンソロジー", author: "アンソロジー" },
    { title: "羽山先生と寺野先生は付き合っている", author: "黄井ぴかち" },
    { title: "春夏秋冬 完全版", author: "蔵王大志：作画　影木栄貴：原作" },
    { title: "春の光に呑まれても", author: "仁科" },
    { title: "晴れた日のドレスコード", author: "あげはる" },
    { title: "ハロー、メランコリック！", author: "大沢やよい" },
    { title: "ばけーしょん魔王とペット", author: "寝路" },
    { title: "パルフェ3 おねロリ百合アンソロジー", author: "アンソロジー" },
    { title: "ひなちゃんが生きてるなら", author: "紬めめ" },
    { title: "昼下がりに、また。", author: "片倉アコ" },
    { title: "ヒーローさんと元女幹部さん", author: "そめちめ" },
    { title: "双子百合えっちアンソロジー", author: "アンソロジー" },
    { title: "ふたごわずらい", author: "桜野いつき" },
    { title: "ふたりエスケープ", author: "田口囁一" },
    { title: "ふたりエスケープ　公式コミックアンソロジー", author: "アンソロジー" },
    { title: "監獄街へようこそ！", author: "寝路" },
    { title: "平良深姉妹はどっちもヤんでる", author: "金子ある" },
    { title: "へんたいよくできました", author: "雪尾ゆき" },
    { title: "骨に願いを、星に呪いを", author: "いくたはな" },
    { title: "ぼくは、百合なお姉ちゃんを応援しています", author: "あおと響" },
    { title: "僕らのアイは気持ち悪い", author: "雨水汐" },
    { title: "ぽちゃクライム！", author: "みんたろう" },
    { title: "マカロン　アイドル百合アンソロジー", author: "アンソロジー" },
    { title: "魔女が恋する5秒前", author: "澄谷ゼニコ" },
    { title: "マッチングアプリ百合アンソロジー", author: "アンソロジー" },
    { title: "マユノウタ", author: "シクシク" },
    { title: "マーメイドライン 完全版", author: "金田一蓮十郎" },
    { title: "三日月のカルテ", author: "七坂なな" },
    { title: "みらいのふうふですけど？", author: "野中友" },
    { title: "無力聖女と無能王女～魔力ゼロで召喚された聖女の異世界救国記～", author: "玉崎たま" },
    { title: "メイドさんと百合についてのアンソロジー", author: "アンソロジー" },
    { title: "モデルちゃんと地味マネさん", author: "たねこ" },
    { title: "やわらかな命日", author: "はにみ" },
    { title: "ゆめぐりゆりめぐり", author: "はづき" },
    { title: "夢と恋ではつり合わない", author: "とりいしづく" },
    { title: "夢の中で君を探して", author: "織生あや" },
    { title: "ユリキュール　アルコール百合アンソロジー", author: "アンソロジー" },
    { title: "ゆりこん", author: "久川はる" },
    { title: "ゆりづくしの教室で", author: "しーめ" },
    { title: "yrhm 百合姫20thアンソロジー", author: "アンソロジー" },
    { title: "百合姫表紙集 2011-2025", author: "百合姫編集部・編" },
    { title: "ユリビュート 百合姫読切再録集", author: "アンソロジー" },
    { title: "ユリビュート2 百合姫読切作品集", author: "アンソロジー" },
    { title: "ゆりゆり", author: "なもり" },
    { title: "ゆるゆり", author: "なもり" },
    { title: "ゆるゆりアンソロジー　10周年記念ver.", author: "アンソロジー" },
    { title: "ゆるゆり資料集", author: "ゆるゆり資料集編纂室" },
    { title: "ゆるゆり10周年記念本　ゆるゆりX", author: "百合姫編集部・編" },
    { title: "現実世界でも幸せにしてくださいね？", author: "しぼりかすこ" },
    { title: "リフレクション！！", author: "merryhachi" },
    { title: "リリウム・テラリウム", author: "ED" },
    { title: "凛としてカレンな花のように", author: "ヒロアキ" },
    { title: "ルミナス＝ブルー", author: "岩見樹代子" },
    { title: "レズ風俗アンソロジー", author: "アンソロジー" },
    { title: "レズ風俗アンソロジー プレミアム", author: "アンソロジー" },
    { title: "レズ風俗アンソロジー リピーター", author: "アンソロジー" },
    { title: "恋愛遺伝子XX 完全版", author: "蔵王大志：作画　影木栄貴：原作" },
    { title: "Roid-ロイド-", author: "しろし" },
    { title: "ロンリーガールに逆らえない", author: "樫風" },
    { title: "ロンリーガールに花束を 樫風短編集", author: "樫風" },
    { title: "私だって青春したいですよ、本当は。", author: "尾野凛" },
    { title: "私に天使が舞い降りた!", author: "椋木ななつ" },
    { title: "私の推しは悪役令嬢。", author: "漫画：青乃下　原作：いのり。　キャラクターデザイン原案：花ヶ田" },
    { title: "私の推しは悪役令嬢。メイドキッチン", author: "漫画：tsuke　原作：いのり。　キャラクターデザイン原案：花ヶ田" },
    { title: "私の百合はお仕事です！", author: "未幡" },
    { title: "私の百合はお仕事です！　公式コミックアンソロジー", author: "アンソロジー" },
    { title: "割り切った関係ですから。", author: "FLOWERCHILD" },
    { title: "ワンナイトフレンド", author: "かやこ" },
    { title: "ワンナイト百合アンソロジー", author: "アンソロジー" }
];

const delay = (ms) => new Promise(r => setTimeout(r, ms));

// Search ebookjapan for free volumes - use the free filter in search
async function searchEbookJapan(browser, title, author) {
    const page = await browser.newPage();
    await page.setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');

    try {
        // Search with free filter - filter=2 means free only
        const searchUrl = `https://ebookjapan.yahoo.co.jp/search/?keyword=${encodeURIComponent(title)}&filter=2`;

        await page.goto(searchUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
        await delay(2000);

        // Check if the search returned actual results for THIS title (not random free books)
        const result = await page.evaluate((searchTitle) => {
            // Check if there are search results at all
            const noResults = document.body.innerText.includes('検索結果がありません') ||
                document.body.innerText.includes('0件');

            if (noResults) {
                return { found: false, freeVolumes: 0, matchingResults: [] };
            }

            // Look for book cards in results
            const bookCards = document.querySelectorAll('.book-thumbnail, .book-item, [data-cl-params*="books"]');
            const matchingResults = [];

            // Get the first few result titles to check if they match our search
            const titles = document.querySelectorAll('.book-title, h2, h3');
            titles.forEach(t => {
                const text = t.innerText?.trim();
                if (text && text.includes(searchTitle.substring(0, Math.min(10, searchTitle.length)))) {
                    matchingResults.push(text);
                }
            });

            // Also check for any result links that go to book pages
            const bookLinks = document.querySelectorAll('a[href*="/books/"]');

            return {
                found: bookCards.length > 0 || bookLinks.length > 0,
                freeVolumes: Math.max(bookCards.length, bookLinks.length),
                matchingResults: matchingResults.slice(0, 3),
                url: window.location.href
            };
        }, title);

        await page.close();

        // Only count as found if there are actual results
        if (result.found && result.freeVolumes > 0) {
            return {
                platform: 'ebookjapan',
                title,
                found: true,
                url: result.url,
                freeVolumes: result.freeVolumes,
                matchingResults: result.matchingResults
            };
        }

        return { platform: 'ebookjapan', title, found: false, freeVolumes: 0 };

    } catch (e) {
        await page.close();
        return { platform: 'ebookjapan', title, found: false, freeVolumes: 0, error: e.message };
    }
}

// Search BookWalker free page directly
async function searchBookWalker(browser, title, author) {
    const page = await browser.newPage();
    await page.setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');

    try {
        // Search on BookWalker - go directly to search page
        const searchUrl = `https://bookwalker.jp/search/?word=${encodeURIComponent(title)}`;

        await page.goto(searchUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
        await delay(2500);

        // Navigate to the first exact match and check its price
        const result = await page.evaluate((searchTitle, searchAuthor) => {
            // Find the FIRST search result that matches our title
            // Look in the main results section only - not "related" sections
            const mainResults = document.querySelector('.o-search-result') || document;

            // Find book items in main results
            const bookItems = mainResults.querySelectorAll('.o-tile, .m-tile, [class*="tile"]');

            let foundFree = false;
            let freeCount = 0;
            let matchUrl = null;
            let matchTitle = null;

            for (const item of bookItems) {
                const titleEl = item.querySelector('.o-tile-title, .m-tile-title, [class*="title"]');
                const itemTitle = titleEl?.innerText?.trim() || '';

                // Check if this result matches our search (first few characters should match)
                const searchPrefix = searchTitle.substring(0, Math.min(8, searchTitle.length));
                if (!itemTitle.includes(searchPrefix)) {
                    continue; // Skip non-matching results
                }

                matchTitle = itemTitle;

                // Check for FREE indicators on this specific item
                // 1. Check for --free class on price
                const freePrice = item.querySelector('[class*="--free"], .o-tile-free, [class*="free"]');

                // 2. Check price text for "無料" or "0円" 
                const priceEl = item.querySelector('[class*="price"]');
                const priceText = priceEl?.innerText || '';
                const isFreePrice = priceText === '無料' || priceText === '0円' ||
                    (priceText.startsWith('0円') && !priceText.includes('～'));

                // 3. Check for free label/badge
                const freeBadge = item.querySelector('[class*="label"][class*="free"], .a-badge--free');

                // 4. Check if title contains free trial indicators
                const isTrial = itemTitle.includes('無料お試し') || itemTitle.includes('【期間限定無料】');

                // Get the book URL
                const linkEl = item.querySelector('a[href*="/de"]');
                matchUrl = linkEl?.href;

                if (freePrice || isFreePrice || freeBadge || isTrial) {
                    foundFree = true;
                    freeCount++;
                }
            }

            return {
                found: foundFree,
                freeVolumes: freeCount,
                matchTitle,
                url: matchUrl
            };
        }, title, author);

        await page.close();

        if (result.found && result.freeVolumes > 0) {
            return {
                platform: 'BookWalker',
                title,
                found: true,
                url: result.url,
                freeVolumes: result.freeVolumes,
                matchTitle: result.matchTitle
            };
        }

        return { platform: 'BookWalker', title, found: false, freeVolumes: 0 };

    } catch (e) {
        await page.close();
        return { platform: 'BookWalker', title, found: false, freeVolumes: 0, error: e.message };
    }
}

// Main execution
(async () => {
    console.log('=== Searching for Free Manga Volumes (Strict Mode) ===\n');
    console.log(`Total titles to search: ${MANGA_LIST.length}`);
    console.log('Platforms: ebookjapan (free filter), BookWalker\n');

    const browser = await puppeteer.launch({
        headless: "new",
        executablePath: PUPPETEER_EXECUTABLE_PATH,
        defaultViewport: { width: 1280, height: 800 },
        args: [
            '--disable-web-security',
            '--disable-blink-features=AutomationControlled'
        ]
    });

    const results = [];

    for (let i = 0; i < MANGA_LIST.length; i++) {
        const manga = MANGA_LIST[i];
        console.log(`[${i + 1}/${MANGA_LIST.length}] ${manga.title.substring(0, 40)}...`);

        // Search both platforms
        const [ebookResult, bwResult] = await Promise.all([
            searchEbookJapan(browser, manga.title, manga.author),
            searchBookWalker(browser, manga.title, manga.author)
        ]);

        results.push({
            title: manga.title,
            author: manga.author,
            ebookjapan: ebookResult,
            bookwalker: bwResult
        });

        // Log findings
        if (ebookResult.found || bwResult.found) {
            if (ebookResult.found) {
                console.log(`  ✅ ebookjapan: ${ebookResult.freeVolumes} free`);
            }
            if (bwResult.found) {
                console.log(`  ✅ BookWalker: ${bwResult.freeVolumes} free`);
            }
        } else {
            console.log(`  ❌ No free volumes`);
        }

        await delay(300);
    }

    await browser.close();

    // Filter results
    const freeOnEbook = results.filter(r => r.ebookjapan.found);
    const freeOnBW = results.filter(r => r.bookwalker.found);
    const freeOnEither = results.filter(r => r.ebookjapan.found || r.bookwalker.found);

    // Generate Markdown table
    console.log('\n\n' + '='.repeat(100));
    console.log('                                    RESULTS TABLE                                    ');
    console.log('='.repeat(100) + '\n');

    console.log('| # | Title | Author | ebookjapan | BookWalker |');
    console.log('|---|-------|--------|------------|------------|');

    for (let i = 0; i < results.length; i++) {
        const r = results[i];
        const shortTitle = r.title.length > 35 ? r.title.substring(0, 32) + '...' : r.title;
        const shortAuthor = r.author.length > 15 ? r.author.substring(0, 12) + '...' : r.author;
        const ebookStatus = r.ebookjapan.found ? `✅ ${r.ebookjapan.freeVolumes}巻` : '—';
        const bwStatus = r.bookwalker.found ? `✅ ${r.bookwalker.freeVolumes}巻` : '—';
        console.log(`| ${i + 1} | ${shortTitle} | ${shortAuthor} | ${ebookStatus} | ${bwStatus} |`);
    }

    // Summary
    console.log('\n\n' + '='.repeat(100));
    console.log('                                      SUMMARY                                        ');
    console.log('='.repeat(100) + '\n');

    console.log(`📚 Total titles searched: ${MANGA_LIST.length}`);
    console.log(`✅ Free on ebookjapan: ${freeOnEbook.length} titles`);
    console.log(`✅ Free on BookWalker: ${freeOnBW.length} titles`);
    console.log(`✅ Free on either platform: ${freeOnEither.length} titles\n`);

    // Print only titles with free volumes with URLs
    if (freeOnEither.length > 0) {
        console.log('\n--- TITLES WITH FREE VOLUMES ---\n');
        for (const r of freeOnEither) {
            console.log(`📖 ${r.title}`);
            console.log(`   Author: ${r.author}`);
            if (r.ebookjapan.found) {
                console.log(`   ebookjapan: ${r.ebookjapan.freeVolumes} free volume(s)`);
                console.log(`     → ${r.ebookjapan.url}`);
            }
            if (r.bookwalker.found) {
                console.log(`   BookWalker: ${r.bookwalker.freeVolumes} free volume(s)`);
                console.log(`     → ${r.bookwalker.url}`);
            }
            console.log('');
        }
    } else {
        console.log('\n❌ No titles with free volumes found on either platform.\n');
    }

    // Save to JSON
    const outputPath = './free_volumes_results.json';
    fs.writeFileSync(outputPath, JSON.stringify({
        searchDate: new Date().toISOString(),
        totalSearched: MANGA_LIST.length,
        freeOnEbookjapan: freeOnEbook.length,
        freeOnBookwalker: freeOnBW.length,
        freeOnEither: freeOnEither.length,
        results
    }, null, 2));
    console.log(`📄 Full results saved to: ${outputPath}`);

    console.log('\n=== Search Complete ===');
})();
