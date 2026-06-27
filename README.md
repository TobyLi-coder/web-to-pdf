# web-to-pdf

A Claude Code skill + standalone Python tool that converts any webpage to PDF via browser screenshots — no download button needed. Handles document viewers (Studocu, Scribd), paywalled content via Chrome cookies, and generic articles with auto ad removal. Runs off-screen, verifies output automatically.

## What it handles

| Scenario | How |
|----------|-----|
| 📄 **Document viewers** (Studocu, Scribd, etc.) | Detects DOM page boundaries to reconstruct the original page layout |
| 📚 **Paywalled study materials / textbooks** | Reads your existing Chrome login session automatically — no re-login needed |
| 📖 **Online-only novels & articles** | Strips ads, sidebars, and site chrome; repaginates at A4 proportions |
| 🌐 **Any other webpage** | Falls back to A4 scroll mode when no page structure is detected |

## Requirements

- macOS (Chrome cookie path is macOS-specific)
- Python 3 + pip3
- Google Chrome (logged in to any paywalled sites you want to access)

Dependencies are installed automatically on first run:
```bash
pip3 install playwright browser-cookie3 Pillow
python3 -m playwright install chromium
```

## Usage

### Option 1 — Standalone CLI

```bash
python3 web_to_pdf.py <URL> [output_dir] [filename.pdf] [max_pages]
```

**Examples:**
```bash
# Scribd document
python3 web_to_pdf.py https://www.scribd.com/document/378841629/...

# Studocu exam paper → save to ~/Documents
python3 web_to_pdf.py https://www.studocu.com/de/document/.../98164929 ~/Documents exam.pdf

# Test first 5 pages only
python3 web_to_pdf.py https://kanunu8.com/book3/6425/44976.html ~/Desktop chapter.pdf 5
```

### Option 2 — Claude Code Skill

Copy `web-to-pdf.md` to your `~/.claude/commands/` folder, then use it directly in Claude Code:

```
/web-to-pdf <URL>
/web-to-pdf <URL> ~/Desktop filename.pdf
/web-to-pdf <URL> ~/Desktop filename.pdf 5
```

Claude will install dependencies, run the script, and report the output path.

## How it works

1. **Cookie injection** — reads Chrome's local cookie store for the target domain, so paywalled content loads as if you're browsing normally
2. **Ad removal** — detects and hides ad elements by class name keywords and standard ad dimensions (728×90, 300×250, etc.)
3. **Mode detection** (in order):
   - **Studocu mode** — uses `viewer.scrollHeight / page_count` for uniform page heights
   - **DOM mode** — finds page elements (`[id^="outer_page_"]`, `.page-content`, etc.) and reads their bounding rects
   - **Scroll mode** — A4-proportioned steps (1400 × 297/210 ≈ 1980 px) with content-area detection and site chrome removal
4. **UI hiding** — removes all `fixed`/`sticky` elements (toolbars, navbars) while preserving document page ancestors
5. **Verification** — re-visits the page and takes a second set of screenshots; compares each page (< 5% pixel difference = pass)
6. **Off-screen browser** — Chromium launches at `--window-position=9999,0`, completely out of your way

## File structure

```
web_to_pdf.py      # standalone Python script
web-to-pdf.md      # Claude Code skill (drop into ~/.claude/commands/)
README.md
```

## Limitations

- Requires an active Chrome session for paywalled sites (the tool reads cookies, it does not bypass authentication)
- macOS only (Chrome cookie decryption uses macOS Keychain)
- Very long documents (100+ pages) may take several minutes due to per-page screenshot overhead
