#!/usr/bin/env python3
"""
web-to-pdf: 通用网页截图转 PDF
- 自动检测并隐藏 fixed/sticky UI 元素
- 自动识别并移除内联广告（类名 + 标准尺寸匹配）
- Studocu / Scribd / 通用 DOM 分页 / 滚动模式自动切换
- 无分页结构时按 A4 比例自动排版
- 浏览器在屏幕外后台运行，不遮挡当前工作
用法: python3 web_to_pdf.py <URL> [输出目录] [文件名.pdf] [最大页数]
"""
import asyncio, os, sys, re, shutil
from urllib.parse import urlparse
from PIL import Image, ImageChops
Image.MAX_IMAGE_PIXELS = None

try:
    import browser_cookie3
    from playwright.async_api import async_playwright
except ImportError as e:
    print(f"缺少依赖: {e}\n请运行: pip3 install playwright browser-cookie3 Pillow")
    sys.exit(1)

# ── 参数 ──────────────────────────────────────────────────────────────────────
URL       = sys.argv[1] if len(sys.argv) > 1 else ""
OUT_DIR   = os.path.expanduser(sys.argv[2]) if len(sys.argv) > 2 else os.getcwd()
FILENAME  = sys.argv[3] if len(sys.argv) > 3 else ""
MAX_PAGES = int(sys.argv[4]) if len(sys.argv) > 4 else 0

if not URL:
    print("用法: python3 web_to_pdf.py <URL> [输出目录] [文件名.pdf] [最大页数]")
    sys.exit(1)

if not FILENAME:
    slug = re.sub(r'[^\w-]', '_', urlparse(URL).path.strip('/').split('/')[-1]
                  or urlparse(URL).netloc)
    FILENAME = f"{slug[:60]}.pdf"
if not FILENAME.endswith(".pdf"):
    FILENAME += ".pdf"

os.makedirs(OUT_DIR, exist_ok=True)
PDF_PATH = os.path.join(OUT_DIR, FILENAME)
SCRATCH  = f"/tmp/web_to_pdf_{os.getpid()}"
os.makedirs(SCRATCH, exist_ok=True)

SCALE      = 2
VIEWPORT_W = 1400
VIEWPORT_H = 900
# A4 比例页高（用于无分页结构的滚动模式），1400 * 297/210 ≈ 1980
A4_PAGE_H  = int(VIEWPORT_W * 297 / 210)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ── Cookie ────────────────────────────────────────────────────────────────────
def get_cookies(url):
    parts  = urlparse(url).netloc.split('.')
    domain = '.' + '.'.join(parts[-2:]) if len(parts) >= 2 else urlparse(url).netloc
    try:
        jar = browser_cookie3.chrome(domain_name=domain)
        cookies = [{"name": c.name, "value": c.value,
                    "domain": c.domain, "path": c.path} for c in jar]
        print(f"✓ Cookie：{domain} → {len(cookies)} 条")
        return cookies
    except Exception:
        return []

# ── JS：隐藏 fixed/sticky 非内容 UI ──────────────────────────────────────────
HIDE_UI_JS = """(pageSelector) => {
    const pageEls  = new Set(document.querySelectorAll(pageSelector || '__none__'));
    const ancestors = new Set();
    for (const el of pageEls) {
        let e = el.parentElement;
        while (e) { ancestors.add(e); e = e.parentElement; }
    }
    let hidden = 0;
    for (const el of document.querySelectorAll('*')) {
        if (pageEls.has(el) || ancestors.has(el)) continue;
        const s = getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') continue;
        if (s.position === 'fixed' || s.position === 'sticky') {
            el.style.setProperty('display', 'none', 'important');
            hidden++;
        }
    }
    return hidden;
}"""

# ── JS：移除内联广告（类名 + 标准广告尺寸匹配）──────────────────────────────
AD_REMOVE_JS = """() => {
    let removed = 0;

    // 1. 按类名/ID 关键词删除
    const kw = ['advert','advertisement','adsense','adsbygoogle','ad-container',
                 'ad-wrapper','ad-block','ad-banner','banner-ad','sponsor',
                 'promoted','outbrain','taboola','doubleclick','prebid',
                 'dfp-ad','div-gpt-ad','baidu_ad','bdsyn','tg_ad','gg_ad',
                 'tuiguang','guanggao'];
    const adSels = [
        'ins.adsbygoogle',
        'iframe[src*="doubleclick"]','iframe[src*="googlesyndication"]',
        'iframe[src*="adnxs"]','iframe[src*="amazon-adsystem"]',
        ...kw.flatMap(k => [`[class*="${k}"]`, `[id*="${k}"]`]),
    ];
    for (const sel of adSels) {
        try {
            for (const el of document.querySelectorAll(sel)) {
                if (getComputedStyle(el).display === 'none') continue;
                el.style.setProperty('display','none','important'); removed++;
            }
        } catch(_) {}
    }

    // 2. 按标准广告尺寸删除（含 2× Retina 尺寸）
    const AD_SIZES = [
        [728,90],[970,90],[970,250],[336,280],[300,250],[300,600],
        [160,600],[468,60],[320,50],[320,100],[300,50],
    ];
    for (const el of document.querySelectorAll('div,iframe,ins,aside')) {
        const s = getComputedStyle(el);
        if (s.display === 'none') continue;
        const r = el.getBoundingClientRect();
        const w = Math.round(r.width), h = Math.round(r.height);
        const match = AD_SIZES.some(([aw,ah]) =>
            (Math.abs(w-aw)<8 && Math.abs(h-ah)<8) ||
            (Math.abs(w-aw*2)<16 && Math.abs(h-ah*2)<16)
        );
        if (match) { el.style.setProperty('display','none','important'); removed++; }
    }
    return removed;
}"""

# ── JS：Studocu viewer 专用检测 ───────────────────────────────────────────────
STUDOCU_DETECT_JS = """() => {
    const pageEls = [...document.querySelectorAll('.page-content')];
    if (pageEls.length < 2) return null;
    const viewer = document.querySelector('[class*="Viewer_viewer-wrapper"],[class*="viewer-wrapper"]');
    if (!viewer) return null;
    const zeroH = pageEls.filter(el => el.offsetHeight < 10).length;
    if (zeroH < pageEls.length * 0.8) return null;
    const pageH = Math.round(viewer.scrollHeight / pageEls.length);
    const cr = viewer.getBoundingClientRect();
    let docStart = 0, e = pageEls[0];
    while (e && e !== document.body) { docStart += (e.offsetTop||0); e = e.offsetParent; }
    return {
        pageCount: pageEls.length, pageH, docStart,
        contentX: Math.round(Math.max(0, cr.left + window.scrollX)),
        contentW: Math.round(cr.width),
    };
}"""

# ── JS：通用 DOM 页面结构检测 ─────────────────────────────────────────────────
PAGE_DETECT_JS = """() => {
    const candidates = [
        '[id^="outer_page_"]','[id^="page_"]','.outer_page',
        '.page-content','[data-page-number]','[data-page]',
        '[class*="Page_page"]','[class*="document-page"]',
        '[class*="pdf-page"]','[class*="PageWrapper"]',
        '[class*="page-content"]','.page',
    ];
    for (const sel of candidates) {
        const els = [...document.querySelectorAll(sel)]
            .filter(el => el.offsetHeight > 100 && el.offsetWidth > 100);
        if (els.length >= 2) {
            const pages = els.map(el => {
                let top=0, e=el;
                while (e && e!==document.body) { top+=(e.offsetTop||0); e=e.offsetParent; }
                const r = el.getBoundingClientRect();
                return { top, height: el.offsetHeight, width: el.offsetWidth,
                         left: Math.round(Math.max(0, r.left+window.scrollX)) };
            });
            return { selector: sel, pages };
        }
    }
    return { selector: null, pages: [] };
}"""

# ── 浏览器启动（窗口推到屏幕外，不遮挡当前工作）─────────────────────────────
async def launch_browser(p):
    return await p.chromium.launch(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-remote-fonts",
            "--window-position=9999,0",   # 推到屏幕右侧边缘以外
            f"--window-size={VIEWPORT_W},{VIEWPORT_H}",
        ]
    )

# ── 截图核心 ──────────────────────────────────────────────────────────────────
async def screenshot_pages(cookies, out_dir, label, max_pages=0):
    os.makedirs(out_dir, exist_ok=True)

    async with async_playwright() as p:
        browser = await launch_browser(p)
        ctx = await browser.new_context(
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            device_scale_factor=SCALE, user_agent=UA,
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        if cookies:
            await ctx.add_cookies(cookies)

        page = await ctx.new_page()
        print(f"\n[{label}] 加载页面...")
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(4000)

        # 关闭弹窗
        for sel in ["#onetrust-accept-btn-handler",
                    "button:has-text('Accept All')", "button:has-text('Accept')",
                    "button:has-text('Akzeptieren')", "[aria-label='Close']",
                    "button:has-text('关闭')", "button:has-text('我知道了')"]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=300):
                    await btn.click(); await page.wait_for_timeout(300)
            except Exception:
                pass
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)

        # 移除内联广告
        ads = await page.evaluate(AD_REMOVE_JS)
        if ads:
            print(f"[{label}] 移除内联广告 {ads} 个")

        # 预滚动触发懒加载
        print(f"[{label}] 预滚动触发懒加载...")
        await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        await page.wait_for_timeout(2000)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(1000)

        # 再次移除广告（滚动可能触发新广告）
        ads2 = await page.evaluate(AD_REMOVE_JS)
        if ads2:
            print(f"[{label}] 滚动后再次移除广告 {ads2} 个")

        # ── 检测页面结构 ──
        studocu = await page.evaluate(STUDOCU_DETECT_JS)
        mode = None

        if studocu and studocu['pageCount'] >= 2:
            pages_list = [
                {"top": studocu['docStart'] + i * studocu['pageH'],
                 "height": studocu['pageH'],
                 "width":  studocu['contentW'],
                 "left":   studocu['contentX']}
                for i in range(studocu['pageCount'])
            ]
            sel_css = ".page-content"
            mode = "studocu"
            print(f"[{label}] ✓ Studocu viewer：{len(pages_list)} 页，每页≈{studocu['pageH']}px")
        else:
            result = await page.evaluate(PAGE_DETECT_JS)
            pages_list, sel_css = result['pages'], result['selector']
            if pages_list:
                avg_h = sum(p['height'] for p in pages_list) / len(pages_list)
                if avg_h > 5000:
                    print(f"[{label}] ⚠️  {sel_css} 页高异常（均值{avg_h:.0f}px），降级滚动模式")
                    pages_list = []
                else:
                    mode = "dom"
                    print(f"[{label}] ✓ DOM 分页（{sel_css}）：{len(pages_list)} 页，均高{avg_h:.0f}px")

        if not pages_list:
            # 滚动模式：先尝试定位主内容区域，移除站点 chrome
            content_info = await page.evaluate("""() => {
                // 常见主内容选择器
                const selectors = [
                    'article', 'main', '[role="main"]',
                    '.readcontent','.chapter-content','.article-content',
                    '.post-content','.entry-content','.content-body',
                    '.novel-content','.book-content','.read-content',
                    '#content','#article','#chapter','#reader','#main',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetHeight > 300 && el.offsetWidth > 200) {
                        let top=0, e=el;
                        while (e && e!==document.body){top+=(e.offsetTop||0);e=e.offsetParent;}
                        const r = el.getBoundingClientRect();
                        return { top, height: el.offsetHeight,
                                 width: el.offsetWidth, left: Math.round(r.left),
                                 selector: sel };
                    }
                }
                return null;
            }""")

            # 移除站点非内容 chrome（header/nav/footer，排除 article/main 内的）
            site_chrome_removed = await page.evaluate("""() => {
                let n = 0;
                const keep = new Set();
                for (const el of document.querySelectorAll('article,main,[role="main"],.readcontent,.article-content,.post-content,.entry-content,#content,#article,#reader,#main')) {
                    let e = el; while (e) { keep.add(e); e = e.parentElement; }
                }
                const chromeSelectors = [
                    'header','nav','footer','.header','.nav','.navbar',
                    '.navigation','.nav-bar','.footer','.site-header',
                    '.site-nav','.site-footer','#header','#nav','#footer',
                    '#top-bar','.top-bar','.breadcrumb','.breadcrumbs',
                    '.sidebar','.side-bar','#sidebar','aside',
                ];
                for (const sel of chromeSelectors) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (!keep.has(el) && getComputedStyle(el).display!=='none') {
                            el.style.setProperty('display','none','important'); n++;
                        }
                    }
                }
                return n;
            }""")
            if site_chrome_removed:
                print(f"[{label}] 移除站点导航/页眉/侧边栏 {site_chrome_removed} 个")

            if content_info:
                print(f"[{label}] ✓ 检测到主内容区：{content_info['selector']}，"
                      f"高度={content_info['height']}px，left={content_info['left']}px")
                step = A4_PAGE_H
                n    = (content_info['height'] + step - 1) // step
                pages_list = [
                    {"top":    content_info['top'] + i * step,
                     "height": step,
                     "width":  content_info['width'],
                     "left":   content_info['left']}
                    for i in range(n)
                ]
                # 最后一页高度调整为剩余内容
                last_remaining = content_info['height'] - (n-1) * step
                if n > 0 and last_remaining > 0:
                    pages_list[-1]['height'] = last_remaining
            else:
                total_h = await page.evaluate("document.documentElement.scrollHeight")
                step    = A4_PAGE_H
                n       = (total_h + step - 1) // step
                pages_list = [{"top": i*step, "height": step,
                               "width": VIEWPORT_W, "left": 0} for i in range(n)]

            sel_css = None
            mode    = "scroll"
            print(f"[{label}] 滚动模式（A4 比例，每页{A4_PAGE_H}px），共{len(pages_list)}段")

        # ── 隐藏 fixed/sticky UI ──
        hidden = await page.evaluate(HIDE_UI_JS, sel_css or "__none__")
        if hidden:
            print(f"[{label}] 隐藏 {hidden} 个 fixed/sticky UI 元素")

        await page.wait_for_timeout(500)

        # 隐藏 UI 后重新探测（保持对应模式）
        if mode == "studocu":
            s2 = await page.evaluate(STUDOCU_DETECT_JS)
            if s2 and s2['pageCount'] >= 2:
                pages_list = [
                    {"top": s2['docStart'] + i * s2['pageH'],
                     "height": s2['pageH'],
                     "width":  s2['contentW'],
                     "left":   s2['contentX']}
                    for i in range(s2['pageCount'])
                ]
                print(f"[{label}] 重探（Studocu）：{len(pages_list)} 页，每页≈{s2['pageH']}px")
        elif mode == "dom":
            r2 = await page.evaluate(PAGE_DETECT_JS)
            if r2['pages']:
                avg2 = sum(p['height'] for p in r2['pages']) / len(r2['pages'])
                if avg2 <= 5000:
                    pages_list = r2['pages']
                    print(f"[{label}] 重探（DOM）：{len(pages_list)} 页")

        if max_pages:
            pages_list = pages_list[:max_pages]
            print(f"[{label}] 限制前 {max_pages} 页")

        # ── 逐页截图 ──
        shots = []
        for i, pg in enumerate(pages_list):
            ph = max(pg['height'], 100)
            await page.set_viewport_size({"width": VIEWPORT_W, "height": ph})
            await page.evaluate(f"window.scrollTo(0, {pg['top']})")
            await page.wait_for_timeout(900)

            # 每页截图前清理残留广告和遮挡
            await page.evaluate(AD_REMOVE_JS)
            await page.evaluate(HIDE_UI_JS, sel_css or "__none__")
            await page.mouse.move(5, 5)
            await page.wait_for_timeout(200)

            raw  = os.path.join(out_dir, f"raw_{i:04d}.png")
            shot = os.path.join(out_dir, f"page_{i:04d}.png")

            for attempt in range(3):
                try:
                    await page.screenshot(path=raw, timeout=60000, animations="disabled")
                    break
                except Exception:
                    if attempt == 2: raise
                    await page.wait_for_timeout(2000)

            img = Image.open(raw)
            rw, rh = img.size
            x1 = int(pg['left'] * SCALE)
            x2 = min(x1 + int(pg['width'] * SCALE), rw) if pg['width'] < VIEWPORT_W else rw
            if x2 > x1:
                img = img.crop((x1, 0, x2, rh))
            img.save(shot)
            shots.append(shot)
            print(f"  [{label}] 页 {i+1:3d}/{len(pages_list)}: {img.size[0]}×{img.size[1]}px")

        await browser.close()
    return shots, mode

# ── 对比验证 ──────────────────────────────────────────────────────────────────
def compare_shots(shots_a, shots_b):
    print(f"\n[验证] 对比 {min(len(shots_a), len(shots_b))} 页...")
    issues = []
    for i, (a, b) in enumerate(zip(shots_a, shots_b)):
        im_a = Image.open(a).convert("RGB").resize((400,300))
        im_b = Image.open(b).convert("RGB").resize((400,300))
        diff = ImageChops.difference(im_a, im_b)
        avg  = sum(sum(px) for px in diff.getdata()) / (400*300*3)
        pct  = avg / 255 * 100
        ok   = pct < 5
        print(f"  页 {i+1:3d}: 差异 {pct:5.1f}%  {'✅' if ok else '❌'}")
        if not ok: issues.append(i+1)
    return issues

# ── 合并 PDF ──────────────────────────────────────────────────────────────────
def make_pdf(shots, path):
    print(f"\n→ 合并 {len(shots)} 页为 PDF...")
    images = [Image.open(s).convert("RGB") for s in shots if os.path.exists(s)]
    if not images: print("❌ 无有效截图"); sys.exit(1)
    images[0].save(path, save_all=True, append_images=images[1:], resolution=150)

# ── 主流程 ────────────────────────────────────────────────────────────────────
async def main():
    print(f"URL:  {URL}\n输出: {PDF_PATH}")
    if MAX_PAGES: print(f"限制: 前 {MAX_PAGES} 页")

    cookies = get_cookies(URL)

    dir_main = os.path.join(SCRATCH, "main")
    shots, mode = await screenshot_pages(cookies, dir_main, "截图", max_pages=MAX_PAGES)
    make_pdf(shots, PDF_PATH)
    size_mb = os.path.getsize(PDF_PATH) / 1024 / 1024
    print(f"\n✅ PDF：{PDF_PATH}  ({size_mb:.1f} MB，{len(shots)} 页，{mode} 模式)")

    print("\n" + "="*55)
    dir_v = os.path.join(SCRATCH, "verify")
    shots_v, _ = await screenshot_pages(cookies, dir_v, "验证", max_pages=len(shots))
    issues = compare_shots(shots, shots_v)

    if not issues:
        print("\n✅ 验证通过：内容一致")
        shutil.rmtree(SCRATCH, ignore_errors=True)
    else:
        print(f"\n❌ 第 {issues} 页差异过大")
        print(f"   原始: {dir_main}\n   验证: {dir_v}")
        sys.exit(2)

asyncio.run(main())
