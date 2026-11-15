const next = new URLSearchParams(location.search).get("next") || "/";
const safeNext = next.startsWith("/") && !next.startsWith("//") ? next : "/";
document.getElementById("f").addEventListener("submit", async e => {
  e.preventDefault();
  const b = document.getElementById("b"), err = document.getElementById("err");
  b.disabled = true; err.textContent = "";
  try {
    const r = await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: document.getElementById("u").value,
                             password: document.getElementById("p").value }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `login failed (${r.status})`);
    localStorage.setItem("boilerAuth", JSON.stringify({
      token: d.token, user: d.user, role: d.role,
      expiresAt: Date.now() + d.expiresIn * 1000, lifetime: d.expiresIn * 1000,
    }));
    location.replace(safeNext);
  } catch (ex) {
    err.textContent = ex.message === "Failed to fetch" ? "cannot reach the boiler Pi" : ex.message;
    b.disabled = false;
  }
});
