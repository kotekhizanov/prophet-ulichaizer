// Prophet updates: an Update button by Prophet's version (bottom left) when
// GitHub has a newer Prophet. Hover it for the release notes (the merged pull
// requests); click it to update the checkout and restart the core. The work
// is done by this plugin's server (server.py), at prophet.plugin.address.

const TAG = "[prophet-updates]";

export function activate(prophet) {
  try {
    start(prophet);
  } catch (e) {
    console.warn(TAG, "failed to start", e);
  }
}

function start(prophet) {
  const base = prophet.plugin.address;
  if (!base) {
    console.warn(TAG, "no server address in the manifest");
    return;
  }

  let data = null; // the server's /status
  let serverDown = false;
  let lastCheck = 0;
  let minutes = intervalOf(prophet.settings());
  let waitingForCore = false;

  // ---- elements
  // A round icon button laid over the footer, just left of the theme button:
  // it takes no room, so the version line never wraps or moves.
  if (!document.getElementById("prophet-updates-style")) {
    document.head.append(el("style", { id: "prophet-updates-style" },
      "@keyframes prophet-updates-spin{to{transform:rotate(360deg)}}" +
      ".prophet-updates-btn{display:grid;place-items:center;width:20px;height:20px;border-radius:999px;border:0;padding:0;cursor:pointer;color:#fff;background:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.18)}" +
      ".prophet-updates-btn:hover{background:#1d4ed8}" +
      ".prophet-updates-btn.failed{background:#dc2626;box-shadow:0 0 0 3px rgba(220,38,38,.18)}" +
      ".prophet-updates-btn.busy svg{animation:prophet-updates-spin 1s linear infinite}"));
  }
  const wrap = el("span", { style: "position:absolute;top:50%;right:calc(1.25rem + 2rem + 6px);transform:translateY(-50%);display:flex" });
  const button = el("button", {
    type: "button",
    class: "prophet-updates-btn",
    style: "display:none",
    "aria-haspopup": "dialog",
    "aria-label": "Update Prophet",
  });
  wrap.append(button);

  const pop = el("div", {
    class: "panel",
    role: "dialog",
    style: [
      "position:fixed", "z-index:1000", "display:none", "width:420px", "max-width:calc(100vw - 24px)",
      "max-height:min(70vh,560px)", "overflow:auto", "padding:16px",
      "box-shadow:0 10px 30px rgba(0,0,0,.35)",
    ].join(";"),
  });
  document.body.append(pop);

  // ---- placing the button by the version
  const place = () => {
    const footer = document.getElementById("health")?.parentElement;
    if (footer && wrap.parentElement !== footer) {
      if (getComputedStyle(footer).position === "static") footer.style.position = "relative";
      footer.append(wrap);
    }
    return !!footer;
  };
  if (!place()) {
    const obs = new MutationObserver(() => { if (place()) obs.disconnect(); });
    obs.observe(document.body, { childList: true, subtree: true });
  }

  // ---- hover popover, like an editor's "Restart to Update"
  let hideTimer = null;
  const show = () => {
    clearTimeout(hideTimer);
    render();
    pop.style.display = "block";
    const r = button.getBoundingClientRect();
    const w = pop.offsetWidth, h = pop.offsetHeight;
    let left = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
    let top = r.top - h - 8;
    if (top < 8) top = Math.min(r.bottom + 8, window.innerHeight - h - 8);
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
  };
  const hideSoon = () => { clearTimeout(hideTimer); hideTimer = setTimeout(() => { pop.style.display = "none"; }, 250); };
  button.addEventListener("mouseenter", show);
  button.addEventListener("mouseleave", hideSoon);
  button.addEventListener("focus", show);
  button.addEventListener("blur", hideSoon);
  pop.addEventListener("mouseenter", () => clearTimeout(hideTimer));
  pop.addEventListener("mouseleave", hideSoon);
  button.addEventListener("click", () => startUpdate());

  // ---- talking to the server
  async function call(path, method = "GET") {
    const r = await fetch(base + path, { method, headers: method === "POST" ? { "Content-Type": "application/json" } : {}, body: method === "POST" ? "{}" : undefined, cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
    return body;
  }

  async function refresh(doCheck) {
    try {
      data = await call(doCheck ? "/check" : "/status", doCheck ? "POST" : "GET");
      serverDown = false;
      if (doCheck || data.checked_at) lastCheck = Date.now();
      if (!doCheck && data.checked_at) lastCheck = Date.parse(data.checked_at) || Date.now();
    } catch (e) {
      serverDown = true;
    }
    paint();
  }

  async function startUpdate() {
    if (!data || busy()) return;
    const n = data.prs?.length || 0;
    if (!confirm(`Update Prophet to ${data.latest?.short} (${data.behind} commits${n ? `, ${n} pull requests` : ""}) and restart the core?`)) return;
    try {
      data = await call("/update", "POST");
    } catch (e) {
      alert(`Update did not start: ${e.message}`);
      return;
    }
    paint();
    follow();
  }

  // While an update runs: follow the server, then the core, then reload.
  let following = false;
  async function follow() {
    if (following) return;
    following = true;
    try { await followLoop(); } finally { following = false; }
  }
  async function followLoop() {
    const deadline = Date.now() + 15 * 60 * 1000;
    while (Date.now() < deadline) {
      await sleep(1500);
      let s = null;
      try { s = await call("/status"); data = s; serverDown = false; } catch { serverDown = true; }
      paint();
      if (s?.update?.state === "failed") return;
      if (s?.update?.state === "done") { await reloadWhenUp(); return; }
      const restarting = s?.update?.state === "restarting" || (serverDown && waitingForCore);
      if (restarting) {
        waitingForCore = true;
        // The node stops the server when the core is away for long; then the core decides.
        if (serverDown && (await coreUp())) { await reloadWhenUp(); return; }
      }
    }
  }

  async function coreUp() {
    try {
      const r = await fetch("/health", { cache: "no-store" });
      return r.ok && (await r.json()).status === "ok";
    } catch { return false; }
  }

  // Reload the page once the updated core answers, so it runs the new Omniscient.
  let reloading = false;
  async function reloadWhenUp() {
    if (reloading) return;
    reloading = true;
    waitingForCore = true;
    paint();
    for (let i = 0; i < 180 && !(await coreUp()); i++) await sleep(1000);
    await sleep(500);
    location.reload();
  }

  const busy = () => ["running", "restarting"].includes(data?.update?.state);

  // ---- drawing
  function paint() {
    const st = data?.update?.state;
    const show = busy() || st === "failed" || (data && !data.error && data.behind > 0) || waitingForCore;
    button.style.display = show ? "" : "none";
    const kind = busy() || waitingForCore ? "busy" : st === "failed" ? "failed" : "ready";
    if (button.dataset.kind !== kind) {
      button.dataset.kind = kind;
      button.className = `prophet-updates-btn ${kind}`;
      button.setAttribute("aria-label", { busy: "Updating Prophet", failed: "Prophet update failed", ready: "Update Prophet" }[kind]);
      button.replaceChildren(icon(kind));
    }
    if (pop.style.display === "block") render();
  }

  function render() {
    pop.replaceChildren();
    const head = el("div", { style: "display:flex;gap:14px;align-items:flex-start" },
      logo(),
      el("div", { style: "min-width:0;flex:1" },
        el("div", { class: "text-sm font-semibold" }, busy() || waitingForCore ? "Updating Prophet" : "Restart to Update"),
        el("div", { class: "text-sm font-medium", style: "margin-top:6px" }, "Prophet"),
        data?.current ? line("Current version", `${data.current.short} (${day(data.current.date)})`) : null,
        data?.latest ? line("Latest version", `${data.latest.short} (${day(data.latest.date)})`) : null,
        data?.latest ? line("Released", day(data.latest.date, true)) : null,
        data ? line("Behind", `${data.behind} commit${data.behind === 1 ? "" : "s"}${data.prs?.length ? `, ${data.prs.length} pull request${data.prs.length === 1 ? "" : "s"}` : ""}`) : null,
      ));
    pop.append(head);

    if (serverDown && !waitingForCore) pop.append(el("p", { class: "hint", style: "margin-top:10px" }, "The plugin's server does not answer. Is the plugin installed on a node and running?"));
    if (data?.error) pop.append(el("p", { class: "hint", style: "margin-top:10px;color:#dc2626" }, `Check failed: ${data.error}`));

    const u = data?.update;
    if (u && u.state !== "idle") {
      const box = el("pre", { class: "text-xs", style: "margin-top:10px;white-space:pre-wrap;max-height:160px;overflow:auto;opacity:.85" }, (u.log || []).join("\n") || "Starting…");
      if (waitingForCore && serverDown) box.textContent += "\nWaiting for the core to come back…";
      pop.append(el("div", { class: "section-title", style: "margin-top:12px" }, u.state === "failed" ? "Update failed" : u.state === "done" ? "Updated" : "Progress"), box);
      queueMicrotask(() => { box.scrollTop = box.scrollHeight; });
    }

    if (data?.prs?.length) {
      pop.append(el("div", { class: "section-title", style: "margin-top:14px" }, "Release notes"));
      const list = el("div", { style: "display:flex;flex-direction:column;gap:8px;margin-top:6px" });
      for (const p of data.prs) {
        const item = el("details", { class: "text-sm" },
          el("summary", { style: "cursor:pointer" },
            el("a", { href: p.url, target: "_blank", rel: "noopener", class: "font-medium", style: "text-decoration:none" }, `#${p.number}`),
            " ", p.title,
            el("span", { class: "hint", style: "margin-left:6px" }, [p.author, day(p.merged_at)].filter(Boolean).join(" · "))));
        if (p.body) item.append(el("div", { class: "hint", style: "white-space:pre-wrap;margin:6px 0 0 14px" }, tidy(p.body)));
        list.append(item);
      }
      pop.append(list);
    } else if (data?.commits?.length) {
      pop.append(el("div", { class: "section-title", style: "margin-top:14px" }, "Changes"));
      pop.append(el("ul", { class: "text-sm", style: "margin-top:6px;padding-left:16px;list-style:disc" },
        ...data.commits.slice(0, 30).map(c => el("li", {}, `${c.short} ${c.subject}`))));
    }

    const actions = el("div", { style: "display:flex;gap:8px;margin-top:14px;flex-wrap:wrap" });
    if (data?.behind > 0 && !busy() && !waitingForCore) {
      actions.append(el("button", { type: "button", class: "btn btn-primary btn-sm", onclick: startUpdate }, "Update and restart"));
    }
    if (data?.latest && data?.repo) {
      actions.append(el("a", { class: "btn btn-sm", href: `https://github.com/${data.repo}/compare/${data.current.sha}...${data.latest.sha}`, target: "_blank", rel: "noopener" }, "Release Notes"));
    }
    if (!busy() && !waitingForCore) {
      actions.append(el("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: async (e) => { e.target.textContent = "Checking…"; await refresh(true); render(); } }, "Check now"));
    }
    pop.append(actions);
    if (data?.checked_at) pop.append(el("p", { class: "hint", style: "margin-top:8px;font-size:11px" }, `Checked ${new Date(data.checked_at).toLocaleString()} · every ${minutes} min · ${data.branch || ""}`));
  }

  // ---- checking every N minutes (the plugin's setting)
  prophet.on("settings", (s) => { minutes = intervalOf(s); });
  setInterval(() => {
    if (busy() || waitingForCore) return;
    if (Date.now() - lastCheck >= minutes * 60 * 1000) refresh(true);
  }, 60 * 1000);

  // Every tab watches the server: an update started anywhere (another tab, another
  // browser) is followed here too, and a page still on the old version reloads.
  let seenUpdate;
  setInterval(async () => {
    if (following || reloading) return;
    let s;
    try { s = await call("/status"); } catch { return; }
    data = s;
    serverDown = false;
    paint();
    const u = s.update || {};
    if (seenUpdate === undefined) seenUpdate = u.state === "done" ? u.started_at : null; // the server was away at load
    if (["running", "restarting"].includes(u.state)) follow();
    else if (u.state === "done" && u.started_at && u.started_at !== seenUpdate) reloadWhenUp();
  }, 5000);

  (async () => {
    await refresh(false);
    seenUpdate = data?.update?.started_at; // an update finished before this page loaded is not news
    if (busy()) follow();
    else if (!data?.checked_at || Date.now() - lastCheck >= minutes * 60 * 1000) refresh(true);
  })();
}

// ---- helpers

function intervalOf(s) {
  const n = Number(s?.check_minutes);
  return Number.isFinite(n) && n >= 5 ? n : 60;
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function day(iso, long) {
  if (!iso) return "";
  const d = new Date(iso);
  return long ? d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
    : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// Markdown bodies as plain text: no comments, no heading marks, no huge gaps.
function tidy(md) {
  return md.replace(/<!--[\s\S]*?-->/g, "").replace(/^#{1,6}\s*/gm, "").replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/\n{3,}/g, "\n\n").trim();
}

function line(label, value) {
  return el("div", { class: "text-sm", style: "opacity:.75;margin-top:2px" }, `${label}: ${value}`);
}

// The button's icon: an arrow down to a line (ready), a circle arrow (busy), "!" (failed).
function icon(kind) {
  const paths = {
    ready: '<path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/>',
    busy: '<path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v4h-4"/>',
    failed: '<path d="M12 6v8"/><path d="M12 18h.01"/>',
  };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "14");
  svg.setAttribute("height", "14");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2.4");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.innerHTML = paths[kind];
  return svg;
}

function logo() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "56");
  svg.setAttribute("height", "56");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "#2563eb");
  svg.setAttribute("stroke-width", "1.6");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.style.flex = "none";
  svg.innerHTML = '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/><path d="M12 8v4l3 2"/>';
  return svg;
}

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null) continue;
    if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) n.append(k);
  return n;
}
