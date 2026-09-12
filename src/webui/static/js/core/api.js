/* 后端接口封装：唯一一处 fetch。 */

export async function api(path, opts) {
  const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { ok: false, error: text }; }
  if (!res.ok && data && data.error) throw new Error(data.error);
  return data;
}
