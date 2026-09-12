/* 原始 JSON 可折叠树。 */
import { esc } from "../core/util.js";

export function jsonTree(v, depth) {
  depth = depth || 0;
  if (v === null || v === undefined) return '<span class="jnull">null</span>';
  if (Array.isArray(v)) {
    if (!v.length) return '<span class="jnull">[]</span>';
    const open = depth < 2 ? " open" : "";
    return "<details" + open + '><summary>Array(' + v.length + ")</summary>" +
      v.map((x, i) => '<div><span class="jk">' + i + "</span>: " + jsonTree(x, depth + 1) + "</div>").join("") +
      "</details>";
  }
  if (typeof v === "object") {
    const keys = Object.keys(v);
    if (!keys.length) return '<span class="jnull">{}</span>';
    const open = depth < 2 ? " open" : "";
    return "<details" + open + '><summary>{' + keys.length + "}</summary>" +
      keys.map(k => '<div><span class="jk">' + esc(k) + "</span>: " + jsonTree(v[k], depth + 1) + "</div>").join("") +
      "</details>";
  }
  if (typeof v === "string") return '<span class="js">"' + esc(v) + '"</span>';
  if (typeof v === "number") return '<span class="jn">' + v + "</span>";
  if (typeof v === "boolean") return '<span class="jb">' + v + "</span>";
  return esc(String(v));
}
