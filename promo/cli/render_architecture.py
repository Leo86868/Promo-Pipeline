"""Render architecture.md to a self-contained architecture.html.

The .md stays the source of truth; this script generates a browser-viewable
HTML with all Mermaid diagrams rendered client-side via CDN-loaded
marked.js + mermaid.js. No server required, no npm install, no build step
beyond running this Python module.

Usage:
    python3 -m promo.cli.render_architecture

Output:
    architecture.html at the repo root.

Regen discipline: any commit that edits architecture.md should also re-run
this script. See CLAUDE.md for the enforcement rule.
"""
# dev utility

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Promo Lab — Architecture</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #14100d;
      --bg-panel: #1d1814;
      --bg-panel-raised: #241d18;
      --bg-diagram: #f8f5f0;
      --border: #3a2f26;
      --border-strong: #5a4938;
      --text: #e6dfd4;
      --text-dim: #a79a89;
      --text-headings: #f5ede0;
      --accent: #d4a55c;
      --accent-hover: #e8b86d;
      --code-bg: #2a221c;
      --code-text: #f0b060;
      --mono: "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
    }
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", sans-serif;
      max-width: 1080px;
      margin: 2rem auto;
      padding: 2.5rem 2.5rem 5rem;
      line-height: 1.65;
      color: var(--text);
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      box-shadow: 0 4px 24px rgba(0, 0, 0, 0.3);
    }
    html { background: #0d0a08; min-height: 100vh; padding: 1.5rem; }
    h1, h2, h3, h4 { color: var(--text-headings); line-height: 1.3; font-weight: 600; }
    h1 {
      font-size: 2.1rem;
      margin: 0 0 0.3em;
      padding-bottom: 0.35em;
      border-bottom: 2px solid var(--border);
    }
    h2 {
      font-size: 1.45rem;
      margin-top: 2.2em;
      padding-bottom: 0.3em;
      border-bottom: 1px solid var(--border);
    }
    h3 { font-size: 1.15rem; margin-top: 1.8em; color: var(--accent); }
    h4 { font-size: 1rem; margin-top: 1.5em; color: var(--text-dim); }
    p { margin: 0.85em 0; }
    strong { color: var(--text-headings); }
    em { color: var(--text-dim); }
    hr { border: none; border-top: 1px solid var(--border); margin: 2.5em 0; }
    ul, ol { padding-left: 1.6em; margin: 0.8em 0; }
    li { margin: 0.3em 0; }
    a { color: var(--accent); text-decoration: none; border-bottom: 1px dotted var(--accent); }
    a:hover { color: var(--accent-hover); border-bottom-style: solid; }
    code {
      background: var(--code-bg);
      color: var(--code-text);
      padding: 0.14em 0.42em;
      border-radius: 4px;
      font-size: 0.88em;
      font-family: var(--mono);
    }
    pre {
      background: var(--bg-panel);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 0.9em 1.1em;
      border-radius: 6px;
      overflow-x: auto;
      font-size: 0.88em;
      line-height: 1.55;
    }
    pre code { background: transparent; padding: 0; color: inherit; font-size: 1em; }
    pre.mermaid {
      background: var(--bg-diagram);
      border: 1px solid var(--border-strong);
      border-radius: 10px;
      padding: 2em 1.5em;
      text-align: center;
      margin: 1.8em 0;
      color: #222;
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.02);
      min-height: 480px;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    pre.mermaid svg {
      min-height: 440px;
      max-width: 100%;
      width: 100%;
      height: auto;
      font-size: 15px !important;
    }
    pre.mermaid svg .nodeLabel,
    pre.mermaid svg .edgeLabel {
      font-size: 15px !important;
    }
    table {
      border-collapse: collapse;
      margin: 1.2em 0;
      width: 100%;
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-radius: 6px;
      overflow: hidden;
    }
    th, td {
      border: 1px solid var(--border);
      padding: 0.6em 0.9em;
      text-align: left;
      vertical-align: top;
    }
    th { background: var(--bg-panel-raised); color: var(--text-headings); font-weight: 600; }
    tr:nth-child(even) td { background: rgba(255, 255, 255, 0.015); }
    blockquote {
      border-left: 3px solid var(--accent);
      margin: 1.2em 0;
      padding: 0.5em 1.1em;
      color: var(--text-dim);
      background: var(--bg-panel);
      border-radius: 0 6px 6px 0;
    }
    .meta {
      font-family: var(--mono);
      font-size: 0.78em;
      color: var(--text-dim);
      margin: 0 0 2em;
      padding-bottom: 0.9em;
      border-bottom: 1px dashed var(--border);
      letter-spacing: 0.02em;
    }
    .meta strong { color: var(--accent); font-weight: 600; }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/marked@4/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
</head>
<body>
  <div class="meta">
    Rendered from <code>architecture.md</code> at <strong>__TIMESTAMP__</strong>.
    Regenerate: <code>python3 -m promo.cli.render_architecture</code>.
  </div>
  <article id="content"></article>
  <script id="md-source" type="text/markdown">__MD_CONTENT__</script>
  <script>
    (function() {
      var src = document.getElementById('md-source').textContent;
      var renderer = new marked.Renderer();
      renderer.code = function(code, infostring) {
        var lang = (infostring || '').match(/\S*/)[0];
        if (lang === 'mermaid') {
          return '<pre class="mermaid">' + code + '</pre>';
        }
        var escaped = code
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;');
        return '<pre><code class="language-' + (lang || '') + '">' + escaped + '</code></pre>';
      };
      marked.setOptions({ renderer: renderer, gfm: true, breaks: false });
      document.getElementById('content').innerHTML = marked.parse(src);
      mermaid.initialize({
        startOnLoad: false,
        theme: 'base',
        securityLevel: 'loose',
        themeVariables: {
          fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
          fontSize: '14px',
          primaryColor: '#fff4e0',
          primaryTextColor: '#2a1f15',
          primaryBorderColor: '#8b6d3f',
          lineColor: '#5a4938',
          secondaryColor: '#e8d5a8',
          tertiaryColor: '#f5ebd4',
          clusterBkg: '#faf4e6',
          clusterBorder: '#8b6d3f',
          edgeLabelBackground: '#faf4e6'
        },
        flowchart: { useMaxWidth: true, htmlLabels: true, curve: 'basis', padding: 20, nodeSpacing: 50, rankSpacing: 70 }
      });
      mermaid.run({ querySelector: '.mermaid' });
    })();
  </script>
</body>
</html>
"""


def render(md_path: Path, html_path: Path) -> Path:
    md_content = md_path.read_text(encoding="utf-8")
    safe = md_content.replace("</script>", r"<\/script>")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = HTML_TEMPLATE.replace("__MD_CONTENT__", safe).replace("__TIMESTAMP__", timestamp)
    html_path.write_text(html, encoding="utf-8")
    return html_path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    md_path = repo_root / "architecture.md"
    html_path = repo_root / "architecture.html"
    if not md_path.exists():
        print(f"architecture.md not found at {md_path}", file=sys.stderr)
        return 1
    out = render(md_path, html_path)
    print(f"Wrote {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
