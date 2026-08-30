// Every request goes through here. Reading the body as text first means an
// HTML error page or a dead server produces a readable message instead of
// "Unexpected token '<'" or an unhandled "Failed to fetch".
export async function api(url, opts) {
  let r;
  try {
    r = await fetch(url, opts);
  } catch (e) {
    throw new Error("Cannot reach the server — is app.py still running? "
                    + "Restart it with: .venv/bin/python app.py");
  }
  const body = await r.text();
  let data = null;
  try { data = body ? JSON.parse(body) : null; } catch (e) { /* not JSON */ }
  if (!r.ok) {
    throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  }
  return data;
}

export function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

export const esc = s => String(s ?? "");
