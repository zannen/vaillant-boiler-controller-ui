// Runs before anything else: no valid token, no page. The server also checks every API call.
const AUTH_KEY = "boilerAuth";
function getAuth() {
  try {
    const a = JSON.parse(localStorage.getItem(AUTH_KEY));
    return a && a.token && Date.now() < a.expiresAt ? a : null;
  } catch (e) { return null; }
}
function toLogin() {
  localStorage.removeItem(AUTH_KEY);
  location.replace("/login?next=" + encodeURIComponent(location.pathname + location.search));
}
if (!getAuth()) toLogin();
