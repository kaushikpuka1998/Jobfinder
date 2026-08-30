// Visiting /?admin=<token> once unlocks the scrape log for this browser —
// there's no login system here, just a shared secret matched against
// ADMIN_TOKEN on the server (see app/config.py).
const ADMIN_KEY = "adminToken";
const urlToken = new URLSearchParams(window.location.search).get("admin");
if (urlToken) {
  localStorage.setItem(ADMIN_KEY, urlToken);
  const url = new URL(window.location.href);
  url.searchParams.delete("admin");
  window.history.replaceState({}, "", url);
}
export const isAdmin = () => !!localStorage.getItem(ADMIN_KEY);

// Every request goes through here. Reading the body as text first means an
// HTML error page or a dead server produces a readable message instead of
// "Unexpected token '<'" or an unhandled "Failed to fetch".
export async function api(url, opts) {
  let r;
  const adminToken = localStorage.getItem(ADMIN_KEY);
  if (adminToken) {
    opts = { ...opts, headers: { ...(opts && opts.headers), "X-Admin-Token": adminToken } };
  }
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
