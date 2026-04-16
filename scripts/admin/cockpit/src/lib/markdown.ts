/**
 * Mini Markdown renderer (ported from legacy scripts/cockpit_shell.html).
 * No external deps; escapes HTML where the legacy path did.
 */
function escHtml(s: string): string {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function inlineMarkdown(s: string): string {
  return s
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, t: string, u: string) => {
      return `<a href="${escHtml(u)}" target="_blank" rel="noopener">${escHtml(t)}</a>`;
    })
    .replace(/\*\*([^*]+)\*\*/g, (_, t: string) => `<strong>${escHtml(t)}</strong>`)
    .replace(/\*([^*]+)\*/g, (_, t: string) => `<em>${escHtml(t)}</em>`)
    .replace(/&(?![a-z#\d]+;)/g, '&amp;');
}

export function renderMarkdown(md: string | null | undefined): string {
  if (!md) {
    return '';
  }
  const codeBlocks: string[] = [];
  let s = md.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang: string, code: string) => {
    const idx = codeBlocks.length;
    codeBlocks.push(
      `<pre><code class="lang-${escHtml(lang)}">${escHtml(code.replace(/\n$/, ''))}</code></pre>`
    );
    return `\0CODE${idx}\0`;
  });
  const inlineCodes: string[] = [];
  s = s.replace(/`([^`]+)`/g, (_, c: string) => {
    const idx = inlineCodes.length;
    inlineCodes.push(`<code>${escHtml(c)}</code>`);
    return `\0IC${idx}\0`;
  });
  const lines = s.split('\n');
  const out: string[] = [];
  let inUl = false;
  let inOl = false;
  const flushList = (): void => {
    if (inUl) {
      out.push('</ul>');
      inUl = false;
    }
    if (inOl) {
      out.push('</ol>');
      inOl = false;
    }
  };
  lines.forEach((line) => {
    const hm = line.match(/^(#{1,3})\s+(.+)/);
    if (hm) {
      flushList();
      out.push(`<h${hm[1].length}>${inlineMarkdown(hm[2])}</h${hm[1].length}>`);
      return;
    }
    if (/^[-*_]{3,}$/.test(line.trim())) {
      flushList();
      out.push('<hr>');
      return;
    }
    const bq = line.match(/^>\s*(.*)/);
    if (bq) {
      flushList();
      out.push(`<blockquote>${inlineMarkdown(bq[1])}</blockquote>`);
      return;
    }
    const ul = line.match(/^[-*+]\s+(.*)/);
    if (ul) {
      if (!inUl) {
        if (inOl) {
          out.push('</ol>');
          inOl = false;
        }
        out.push('<ul>');
        inUl = true;
      }
      out.push(`<li>${inlineMarkdown(ul[1])}</li>`);
      return;
    }
    const ol = line.match(/^\d+\.\s+(.*)/);
    if (ol) {
      if (!inOl) {
        if (inUl) {
          out.push('</ul>');
          inUl = false;
        }
        out.push('<ol>');
        inOl = true;
      }
      out.push(`<li>${inlineMarkdown(ol[1])}</li>`);
      return;
    }
    if (/^\0CODE\d+\0$/.test(line.trim())) {
      flushList();
      out.push(line.trim());
      return;
    }
    if (!line.trim()) {
      flushList();
      out.push('');
      return;
    }
    flushList();
    out.push(`<p>${inlineMarkdown(line)}</p>`);
  });
  flushList();
  let result = out.join('\n');
  codeBlocks.forEach((block, i) => {
    result = result.replace(`\0CODE${i}\0`, block);
  });
  inlineCodes.forEach((ic, i) => {
    result = result.replace(new RegExp(`\0IC${i}\0`, 'g'), ic);
  });
  return result;
}
