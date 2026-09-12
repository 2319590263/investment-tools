/* 轻量 Markdown → HTML（报告正文渲染）。 */
import { $, esc } from "../core/util.js";

export function mdInline(s) {
  let t = esc(s);
  const codes = [];
  t = t.replace(/`([^`]+)`/g, (m, c) => { codes.push(c); return "\u0001C" + (codes.length - 1) + "\u0001"; });
  t = t.replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, (m, txt, url) =>
    '<a href="' + url + '" target="_blank" rel="noopener">' + txt + "</a>");
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  t = t.replace(/\u0001C(\d+)\u0001/g, (m, i) => "<code>" + codes[+i] + "</code>");
  return t;
}

export function splitRow(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  const cells = [];
  let cur = "";
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (ch === "\\" && s[i + 1] === "|") { cur += "|"; i++; continue; }
    if (ch === "|") { cells.push(cur.trim()); cur = ""; continue; }
    cur += ch;
  }
  cells.push(cur.trim());
  return cells;
}

export const isSepRow = line => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);

export const isTableRow = line => /^\s*\|/.test(line);

export function mdTable(lines) {
  const head = splitRow(lines[0]);
  const body = lines.slice(1).map(splitRow);
  let html = "<table><thead><tr>";
  head.forEach(c => { html += "<th>" + mdInline(c) + "</th>"; });
  html += "</tr></thead><tbody>";
  body.forEach(r => {
    html += "<tr>";
    for (let i = 0; i < head.length; i++) html += "<td>" + mdInline(r[i] === undefined ? "" : r[i]) + "</td>";
    html += "</tr>";
  });
  return html + "</tbody></table>";
}

export function mdList(items) {
  function build(start, indent) {
    const tag = items[start].ordered ? "ol" : "ul";
    let html = "<" + tag + ">";
    let i = start;
    while (i < items.length && items[i].indent >= indent) {
      const it = items[i];
      if (it.indent > indent) {
        const sub = build(i, it.indent);
        html += sub.html;
        i = sub.next;
        continue;
      }
      if (i + 1 < items.length && items[i + 1].indent > indent) {
        const sub = build(i + 1, items[i + 1].indent);
        html += "<li>" + mdInline(it.text) + sub.html + "</li>";
        i = sub.next;
      } else {
        html += "<li>" + mdInline(it.text) + "</li>";
        i++;
      }
    }
    return { html: html + "</" + tag + ">", next: i };
  }
  return build(0, items[0].indent).html;
}

export function mdToHtml(src) {
  if (!src) return "";
  let text = String(src).replace(/\r\n?/g, "\n");
  const codes = [];
  text = text.replace(/```[^\n`]*\n([\s\S]*?)```/g, (m, body) => {
    codes.push(body.replace(/\n$/, ""));
    return "\u0000CODE" + (codes.length - 1) + "\u0000";
  });
  const lines = text.split("\n");
  const out = [];
  let i = 0, secNo = 0;

  const collectList = () => {
    const items = [];
    while (i < lines.length) {
      const ln = lines[i];
      if (/^\s*$/.test(ln)) {
        let k = i + 1;
        while (k < lines.length && /^\s*$/.test(lines[k])) k++;
        if (k < lines.length && /^\s+([-*+]|\d+[.)])\s+/.test(lines[k])) { i = k; continue; }
        break;
      }
      const m = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(ln);
      if (m) { items.push({ indent: m[1].length, ordered: /\d/.test(m[2][0]), text: m[3] }); i++; continue; }
      const t = ln.trim();
      if (items.length && /^\s+\S/.test(ln) && !t.startsWith("|") && !t.startsWith("#") &&
          !t.startsWith(">") && !t.startsWith("\u0000CODE")) {
        items[items.length - 1].text += " " + t;
        i++;
        continue;
      }
      break;
    }
    return items;
  };

  while (i < lines.length) {
    const line = lines[i];
    const t = line.trim();

    if (!t) { i++; continue; }

    let m = /^\u0000CODE(\d+)\u0000$/.exec(t);
    if (m) { out.push("<pre><code>" + esc(codes[+m[1]]) + "</code></pre>"); i++; continue; }

    m = /^(#{1,6})\s+(.*)$/.exec(t);
    if (m) {
      const lvl = m[1].length;
      let id = "";
      if (lvl === 2) { secNo++; id = ' id="sec-' + secNo + '"'; }
      out.push("<h" + lvl + id + ">" + mdInline(m[2]) + "</h" + lvl + ">");
      i++;
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(t)) { out.push("<hr>"); i++; continue; }

    if (isTableRow(line) && i + 1 < lines.length && isSepRow(lines[i + 1])) {
      const rows = [lines[i]];
      i += 2;
      while (i < lines.length && isTableRow(lines[i]) && !isSepRow(lines[i])) { rows.push(lines[i]); i++; }
      out.push(mdTable(rows));
      continue;
    }

    if (t.startsWith(">")) {
      const buf = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        buf.push(lines[i].trim().replace(/^>\s?/, ""));
        i++;
      }
      out.push("<blockquote>" + mdToHtml(buf.join("\n")) + "</blockquote>");
      continue;
    }

    if (/^(\s*)([-*+]|\d+[.)])\s+/.test(line)) {
      const items = collectList();
      if (items.length) { out.push(mdList(items)); continue; }
    }

    const buf = [];
    while (i < lines.length) {
      const l = lines[i];
      const lt = l.trim();
      if (!lt) break;
      if (/^(#{1,6})\s/.test(lt) || lt.startsWith(">") || lt.startsWith("|") ||
          /^(-{3,}|\*{3,}|_{3,})$/.test(lt) || /^\u0000CODE\d+\u0000$/.test(lt)) break;
      if (/^(\s*)([-*+]|\d+[.)])\s+/.test(l) && buf.length) break;
      buf.push(lt);
      i++;
    }
    if (buf.length) out.push("<p>" + mdInline(buf.join(" ")) + "</p>");
    else i++;
  }
  return out.join("\n");
}


/* JSON 树 */
export function withToc(html) {
  const heads = [];
  const re = /<h2 id="(sec-\d+)">([\s\S]*?)<\/h2>/g;
  let m;
  while ((m = re.exec(html))) heads.push({ id: m[1], text: m[2].replace(/<[^>]+>/g, "") });
  if (heads.length < 2) return html;
  const toc = '<div class="toc"><b>章节目录</b>' +
    heads.map(h => '<a href="#' + h.id + '">' + esc(h.text) + "</a>").join("") + "</div>";
  const first = html.indexOf('<h2 id="sec-');
  return first < 0 ? toc + html : html.slice(0, first) + toc + html.slice(first);
}
