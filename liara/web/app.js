// Liara deploy + SRE assistant — one screen: home card, chat feed, diagnosis boards, approval cards, verify results.
// Everything that came from Liara or from logs is put on the page as text (textContent), never as HTML.

const $ = (id) => document.getElementById(id);
const fa = (n, d = 0) => (n == null ? "—" : Number(n).toLocaleString("fa-IR", { maximumFractionDigits: d }));
const FINAL = new Set(["done", "failed", "rejected", "expired", "stale", "refused"]);
const state = { app: null, cards: new Map(), boards: new Map(), verifies: new Set(), verifiedIncidents: new Set(),
                es: null, timer: null, overviewErrors: 0, connected: false, appsReady: null, jobs: new Map(), domain: null, domainCard: null };

function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}

async function api(path, opts = {}) {
  let r;
  try {
    r = await fetch(path, { headers: { "content-type": "application/json" }, credentials: "same-origin", ...opts });
  } catch {
    banner("سرور دستیار الان جواب نمی‌ده؛ دوباره امتحان می‌کنم.");    // a blip is not a logout
    return [0, {}];
  }
  const body = await r.json().catch(() => ({}));
  if (r.status === 401 && body.error === "no_session" && state.connected) {
    disconnected("اتصال با دستیار قطع شد (مثلاً سرور دوباره راه‌اندازی شد). دوباره وصل شو؛ کارت‌های در انتظار پاک شده‌اند.");
  } else if (r.status) {
    clearBanner();
  }
  return [r.status, body];
}

function banner(msg) { $("banner").textContent = msg; $("banner").classList.remove("hidden"); }
function clearBanner() { if (state.connected) $("banner").classList.add("hidden"); }

function disconnected(msg) {
  state.connected = false;
  banner(msg);
  $("live").classList.add("hidden"); $("composer").classList.add("hidden"); $("connect").classList.remove("hidden");
  $("conn").textContent = "وصل نیست"; $("conn").className = "pill bad";
  if (state.es) { state.es.close(); state.es = null; }
  clearInterval(state.timer);
}

function say(node) { $("feed").append(node); node.scrollIntoView({ behavior: "smooth", block: "nearest" }); return node; }
function note(text, cls = "muted") { return say(h("div", { class: `box ${cls}`, text })); }
let statusOwner = 0;
function status(text) { const id = ++statusOwner; $("statusLine").textContent = text || ""; return id; }
function clearStatus(id) { if (id === statusOwner) $("statusLine").textContent = ""; }

// ---------- connect ----------

$("connectForm").onsubmit = async (e) => {
  e.preventDefault();
  $("connectMsg").textContent = "در حال اتصال…";
  const [code, res] = await api("/api/session", { method: "POST", body: JSON.stringify({ token: $("token").value }) });
  $("token").value = "";
  if (code !== 200) { $("connectMsg").textContent = res.message || "اتصال نشد."; return; }
  $("connectMsg").textContent = "";
  start(res);
};

async function start(res) {
  // a fresh session: whatever the old one showed (cards, boards) no longer exists on the server
  $("feed").textContent = ""; state.cards.clear(); state.boards.clear(); state.verifies.clear(); state.verifiedIncidents.clear();
  state.jobs.clear(); state.domain = null; state.domainCard = null;
  state.connected = true;
  $("banner").classList.add("hidden");
  $("connect").classList.add("hidden"); $("live").classList.remove("hidden"); $("composer").classList.remove("hidden");
  $("conn").textContent = res.team ? `وصل به لیارا · تیم ${res.team}` : "وصل به لیارا";
  $("conn").className = "pill ok";
  if (res.replay) $("conn").textContent += " · نمایش ضبط‌شده";
  listen(); refreshLog(); restoreJob();
  state.appsReady = loadApps();                       // a message sent meanwhile waits for this (ask())
  await state.appsReady;
  clearInterval(state.timer);
  state.timer = setInterval(() => { if (!document.hidden && state.overviewErrors < 5) loadOverview(); }, 60000);
}

function setComposer(enabled, hint) {
  $("askText").disabled = !enabled; $("askBtn").disabled = !enabled; $("checkBtn").disabled = !enabled;
  if (hint) $("askText").placeholder = hint;
}

async function loadApps() {
  const [code, res] = await api("/api/apps");
  const box = $("apps"); box.textContent = "";
  if (code !== 200 || !res.apps) {
    setComposer(false, "اول باید فهرست برنامه‌ها خونده بشه.");
    box.append(h("span", { class: "muted small", text: (res.message || "فهرست برنامه‌ها خوانده نشد.") + " " }),
               h("button", { class: "ghost", text: "دوباره", onclick: loadApps }));
    return;
  }
  if (!res.apps.length) {
    setComposer(false, "در این حساب هنوز برنامه‌ای نیست.");
    box.append(h("span", { class: "muted small", text: "در این حساب هنوز برنامه‌ای نیست؛ با «پروژه‌ی تازه» یکی آنلاین کن. " }),
               h("button", { class: "ghost", text: "دوباره نگاه کن", onclick: loadApps }));
    return;
  }
  setComposer(true, "مشکل رو با زبان خودت بگو؛ مثلاً «صفحه‌ی پرداخت خطا می‌ده» یا «سایتم کند شده»");
  if (res.apps.length > 1) {
    res.apps.forEach((a) => box.append(h("button", { class: "chip", "data-app": a.name, onclick: () => pickApp(a.name) }, a.name)));
  }
  pickApp(state.app && res.apps.some((a) => a.name === state.app) ? state.app : res.apps[0].name);
}

function pickApp(name) {
  state.app = name; state.overviewErrors = 0;
  document.querySelectorAll("#apps .chip").forEach((c) => c.classList.toggle("sel", c.dataset.app === name));
  loadOverview();
}

// ---------- home card ----------

function setBar(id, pct) {
  const bar = $(id);
  bar.querySelector("i").style.width = pct == null ? "0" : `${Math.min(100, pct)}%`;
  bar.classList.toggle("warn", pct != null && pct >= 70 && pct < 90);
  bar.classList.toggle("hot", pct != null && pct >= 90);
  bar.setAttribute("aria-valuenow", pct == null ? "" : String(Math.round(pct)));
}

function ago(s) {
  if (s == null) return "";
  if (s < 90) return `${fa(s)} ثانیه پیش`;
  if (s < 5400) return `${fa(Math.round(s / 60))} دقیقه پیش`;
  return `${fa(Math.round(s / 3600))} ساعت پیش`;
}

async function loadOverview() {
  const asked = state.app;
  if (!asked) return;
  const [code, o] = await api(`/api/apps/${encodeURIComponent(asked)}/overview`);
  if (asked !== state.app) return;                      // the user picked another app meanwhile
  $("home").classList.remove("hidden");
  if (code !== 200 || o.status === "error" || o.name !== asked) {
    state.overviewErrors++;
    $("home").classList.add("stale");
    $("hState").textContent = o.message || "وضعیت خوانده نشد";
    return;
  }
  state.overviewErrors = 0;
  $("home").classList.remove("stale");
  $("hName").textContent = o.name;
  $("hDot").className = "dot " + (o.on && o.state === "ACTIVE" ? "on" : "off");
  $("hState").textContent = o.on ? (o.state === "ACTIVE" ? "روشن" : o.state) : "خاموش";
  if (/^https:\/\//.test(o.url || "")) $("hLink").href = o.url; else $("hLink").removeAttribute("href");
  $("hLink").classList.toggle("hidden", !/^https:\/\//.test(o.url || ""));
  $("hRam").textContent = o.ram_pct == null ? "نامعلوم" : `${fa(o.ram_mb)} مگ از ${fa(o.plan_ram_gb * 1024)} (${fa(o.ram_pct)}٪)`;
  $("hCpu").textContent = o.cpu_pct == null ? "نامعلوم" : `${fa(o.cpu_pct)}٪ از ${fa(o.plan_cpu, 1)} هسته`;
  setBar("hRamBar", o.ram_pct); setBar("hCpuBar", o.cpu_pct);
  $("hPlan").textContent = `پلن ${o.plan} · بسته‌ی ${o.bundle}` + (o.price_month ? ` · حدود ${fa(o.price_month)} تومان در ماه` : "");
  $("hZd").textContent = o.zero_downtime == null ? "" : o.zero_downtime ? "انتشار بدون قطعی: روشن"
    : o.zero_downtime_allowed ? "انتشار بدون قطعی: خاموش" : "انتشار بدون قطعی: در بسته‌ی پایه نیست (فقط با ارتقای بسته از کنسول)";
  $("zdBtn").classList.toggle("hidden", !(o.zero_downtime === false && o.zero_downtime_allowed));
  const r = o.last_release;
  $("hRel").textContent = r ? `آخرین نسخه: ${r.tag} (${r.type === "ENV_UPDATE" ? "تغییر تنظیمات" : "دیپلوی کد"}) ${ago(r.ago_s)}` : "";
}

$("checkBtn").onclick = () => ask("یه بررسی کلی کن؛ همه‌چیز سالمه؟");
$("zdBtn").onclick = async () => {
  const [code, res] = await api("/api/apps/" + encodeURIComponent(state.app) + "/zero-downtime", { method: "POST" });
  if (res.status !== "waiting_for_user") note(res.message || (code ? "انجام نشد." : "سرور جواب نداد."), "bad");
};

// ---------- chat → diagnosis ----------

$("ask").onsubmit = (e) => { e.preventDefault(); const t = $("askText").value.trim(); if (t) ask(t); };

async function ask(text) {
  if (!state.app && state.appsReady) await state.appsReady;
  if (!state.app) { note("اول یک برنامه انتخاب کن."); return; }
  $("askText").value = ""; $("askBtn").disabled = true;
  say(h("div", { class: "box you", text }));
  const sid = status("دارم شواهد رو از لیارا جمع می‌کنم…");
  const [code, board] = await api("/api/troubleshoot", { method: "POST", body: JSON.stringify({ app: state.app, text }) });
  $("askBtn").disabled = false; clearStatus(sid);
  if (code !== 200 || !board.id) { note(board.message || "بررسی انجام نشد.", "bad"); return; }
  renderBoard(board);
}

function retireBoards(incident, exceptId) {
  for (const [id, el] of state.boards) {
    if (id === exceptId || el.dataset.incident !== incident || el.dataset.retired) continue;
    el.dataset.retired = "1";
    el.classList.add("stale");
    el.querySelectorAll("button").forEach((b) => (b.disabled = true));
    el.prepend(h("div", { class: "muted small", text: "این تشخیص قدیمیه؛ آخرین تشخیص پایین‌تره." }));
  }
}

function renderBoard(b) {
  if (!b || !b.id || state.boards.has(b.id)) return;
  const top = b.hypotheses[0];
  const rec = b.recommended ? b.hypotheses.find((x) => x.id === b.recommended.hypothesis)?.fixes[b.recommended.option] : null;
  const card = h("div", { class: "box" },
    h("p", { class: "headline", text: b.headline }),
    h("div", { class: "conf", text: `اطمینان: ${fa(Math.round(top.confidence * 100))}٪ · ${fa(b.footer.seconds, 1)} ثانیه · ${fa(b.footer.liara_calls)} تماس با API لیارا · مدل زبانی ${b.footer.model === "llm" ? "روشن" : "خاموش"}` }));
  card.dataset.incident = b.incident;
  if (b.question) card.append(h("div", { class: "muted", text: b.question }));
  const actions = h("div", { class: "row" });
  // "nothing to fix" (healthy) is a question for the user, not a suggestion button
  if (rec && rec.kind === "wait") { if (rec.question && !b.question) card.append(h("div", { class: "muted", text: rec.question })); }
  else if (rec) actions.append(fixButton(b, b.recommended.hypothesis, b.recommended.option, rec, true));
  b.hypotheses.forEach((hy) => hy.fixes.forEach((fx, i) => {
    if (rec && hy.id === b.recommended.hypothesis && i === b.recommended.option) return;
    if (fx.kind === "wait" && !fx.label) return;
    actions.append(fixButton(b, hy.id, i, fx, false));
  }));
  card.append(actions);
  const why = h("details", {}, h("summary", { text: "چرا؟" }));
  const list = h("ul", { class: "ev" });
  b.hypotheses.forEach((hy) => list.append(h("li", {}, h("b", { text: `${fa(Math.round(hy.confidence * 100))}٪ — ` }), hy.title)));
  b.evidence.forEach((e) => list.append(h("li", {}, e.text, " ", h("span", { class: "q", text: e.quality === "unavailable" ? "(نتونستم ببینم)" : `(${e.source})` }))));
  b.ruled_out.forEach((r) => list.append(h("li", { class: "good", text: "رد شد: " + r.text })));
  (b.limits || []).forEach((l) => list.append(h("li", { class: "q", text: l })));
  why.append(list);
  card.append(why);
  if (b.ai) card.append(aiPanel(b.ai));
  state.boards.set(b.id, card);
  retireBoards(b.incident, b.id);
  say(card);
}

const KIND_FA = { error: "خطا", slow: "کندی", down: "از دسترس خارج", other: "نامشخص" };

// What the language model did on this diagnosis, next to what the fixed rules alone said. With the model off,
// the same board comes from the rules (that path always works); the panel says so instead of hiding it.
function aiPanel(ai) {
  const on = ai.symptom_source === "llm" || ai.explain_source === "llm";
  const who = ai.provider === "recorded" ? "پاسخ ضبط‌شده‌ی Claude برای همین شواهد (نمایشی؛ بدون هزینه)" : on ? `مدل ${ai.provider} (زنده)` : "خاموش";
  const box = h("details", on ? { open: "" } : {}, h("summary", { text: `هوش مصنوعی چه کرد؟ (${on ? who : "مدل خاموش؛ تشخیص با قواعد"})` }));
  const u = ai.understood || {}, r = ai.rules_understood || {};
  const list = h("ul", { class: "ev" },
    h("li", { text: `۱. فهم حرف تو — ${ai.symptom_source === "llm" ? "مدل" : "قواعد"}: نوع «${KIND_FA[u.kind] || u.kind}»، صفحه ${u.route || "نامعلوم"}` +
      (ai.symptom_source === "llm" && (r.kind !== u.kind || r.route !== u.route) ? ` (قواعد به‌تنهایی: «${KIND_FA[r.kind] || r.kind}»، ${r.route || "نامعلوم"})` : "") }),
    h("li", { text: "۲. قواعد ثابت (بدون مدل) از روی شواهد این علت‌ها رو دادن: " +
      ai.rules_order.map((x) => `${x.id} ${fa(Math.round(x.confidence * 100))}٪`).join("، ") }),
    h("li", { text: ai.explain_source === "llm"
      ? `۳. مدل شواهد رو خوند و ترتیب رو ${ai.final_order.join(" ← ") === ai.rules_order.map((x) => x.id).join(" ← ") ? "تأیید کرد" : "عوض کرد: " + ai.final_order.join(" ← ")}` +
        (ai.added ? `؛ و یک علت تازه با استناد به دو شاهد اضافه کرد: «${ai.added}»` : "") + "، و جمله‌ی بالا رو به زبان ساده نوشت."
      : "۳. مدل خاموشه؛ جمله‌ی بالا از الگوی ثابت همون علت اول ساخته شد." }),
    h("li", { class: "q", text: "مدل فقط پیشنهاد و توضیح می‌ده: هر چیزی که به شاهد واقعی استناد نکنه حذف می‌شه، علت اول قواعد رو بیشتر از ۱۵٪ پایین نمی‌بره، و هیچ ابزاری برای تغییر یا تأیید نداره." }));
  if (ai.explain_source === "llm" && ai.rules_headline) list.append(h("li", { class: "q", text: `جمله‌ی قواعد به‌تنهایی: «${ai.rules_headline}»` }));
  box.append(list);
  return box;
}

function fixButton(b, hid, option, fx, primary) {
  const label = fx.label || ({ coding_agent: "درست کردن کد", manual: "راهنمای دستی", wait: "بررسی دوباره", handoff: "گزارش برای پشتیبانی" })[fx.kind] || "انجام بده";
  const btn = h("button", { class: primary ? "" : "ghost", text: primary ? `پیشنهاد من: ${label}` : label });
  btn.onclick = () => chooseFix(b, hid, option, fx, btn);
  return btn;
}

async function chooseFix(b, hid, option, fx, btn, values) {
  if (fx.kind === "wait") {
    if (fx.question) { $("askText").focus(); $("askText").placeholder = fx.question; } else ask("دوباره بررسی کن");
    return;
  }
  if (fx.kind === "handoff") return showTicket(b.app);
  btn.disabled = true;
  const [code, res] = await api(`/api/incidents/${encodeURIComponent(b.incident)}/fix`, { method: "POST",
    body: JSON.stringify({ hypothesis: hid, option, values: values || null, board_id: b.id }) });
  btn.disabled = false;
  if (res.status === "needs_values") return askValues(b, hid, option, fx, res);
  if (res.status === "handoff") return showBrief(res);
  if (res.status === "manual") return showManual(res, fx);
  if (res.status === "waiting_for_user") { status("کارت تأیید پایین‌تر اومد."); return; }
  if (res.status === "done") { note("انجام شد."); return; }
  if (res.error === "diagnose_first") { ask("دوباره بررسی کن"); return; }
  note(res.message || (code ? "انجام نشد." : "سرور جواب نداد؛ دوباره امتحان کن."), "bad");
}

function askValues(b, hid, option, fx, res) {
  const keys = res.keys && res.keys.length ? res.keys : [null];
  const rows = keys.map((k) => ({
    key: k ? null : h("input", { class: "ltr", "aria-label": "اسم متغیر", placeholder: "اسم متغیر، مثلاً PAYMENT_API_URL" }),
    name: k, val: h("input", { class: "ltr", type: "password", autocomplete: "off", "aria-label": `مقدار ${k || "متغیر"}`, placeholder: "مقدار" }) }));
  const send = h("button", { text: "ثبت و نشون دادن کارت تأیید" });
  const box = h("div", { class: "box" }, h("div", { text: res.message }),
    ...rows.map((r) => h("div", { class: "row" }, r.name ? h("b", { class: "ltr", text: r.name }) : r.key, r.val)), h("div", { class: "row" }, send));
  send.onclick = () => {
    const values = {};
    rows.forEach((r) => { const k = r.name || r.key.value.trim(); if (k && r.val.value) values[k] = r.val.value; });
    rows.forEach((r) => (r.val.value = ""));
    if (!Object.keys(values).length) { send.textContent = "اسم و مقدار رو وارد کن"; return; }
    box.remove();
    chooseFix(b, hid, option, fx, send, values);
  };
  say(box);
}

function copyButton(text, label) {
  const copy = h("button", { class: "ghost", text: label });
  copy.onclick = async () => { try { await navigator.clipboard.writeText(text); copy.textContent = "کپی شد ✓"; } catch { copy.textContent = "کپی نشد"; } };
  return copy;
}

function showBrief(res) {
  const brief = res.brief || "";
  say(h("div", { class: "box" }, h("div", { text: res.message }),
    h("div", { class: "muted small", text: $("agentState").classList.contains("ok")
      ? "ایجنت کدنویسی‌ات وصله؛ ازش بخواه این مشکل رو درست کنه و منتشر کنه."
      : "ایجنت کدنویسی‌ات (Claude Code، Cline…) رو با دکمه‌ی «وصل کردن به ایجنت کدنویسی» وصل کن، یا این بریف رو بهش بده:" }),
    brief ? h("pre", { text: brief }) : null, brief ? h("div", { class: "row" }, copyButton(brief, "کپی بریف")) : null));
}

async function showTicket(app) {
  const [code, res] = await api(`/api/apps/${encodeURIComponent(app)}/ticket`);
  if (code !== 200 || res.status !== "ok") { note(res.message || "گزارش آماده نشد."); return; }
  say(h("div", { class: "box" }, h("b", { text: "گزارش برای پشتیبانی لیارا (رمزها و کلیدها حذف شده‌اند)" }),
    h("pre", { text: res.text }), h("div", { class: "row" }, copyButton(res.text, "کپی گزارش"),
      h("a", { href: "https://console.liara.ir/tickets", target: "_blank", rel: "noopener noreferrer", class: "small", text: "باز کردن تیکت در کنسول لیارا ↗" }))));
}

function showManual(res, fx) {
  const cap = res.capability || fx.capability || {};
  say(h("div", { class: "box" }, h("b", { text: cap.fa || res.label || "" }),
    h("ol", {}, ...(cap.steps || []).map((s) => h("li", { text: s })))));
}

// ---------- approval cards ----------

function renderAction(a) {
  let el = state.cards.get(a.id);
  if (!el) { el = h("div"); state.cards.set(a.id, el); say(el); }
  el.className = `box card t${a.tier}`;
  el.dataset.incident = a.incident_id || "";
  el.dataset.caller = a.caller || "";
  el.dataset.tool = a.tool || "";
  el.dataset.demo = a.demo ? "1" : "";
  el.textContent = "";
  const c = a.card;
  el.append(h("h2", { text: c.title }));
  if (a.why) el.append(h("div", { class: "muted small", text: (a.caller === "mcp" ? "ایجنت کدنویسی: " : "") + a.why }));
  el.append(h("dl", { class: "kv" },
    h("dt", { text: "ریسک" }), h("dd", { text: c.risk }), h("dt", { text: "هزینه" }), h("dd", { text: c.cost }),
    h("dt", { text: "قطعی" }), h("dd", { text: c.downtime }), h("dt", { text: "برگشت" }), h("dd", { text: c.undo }),
    h("dt", { text: "چطور می‌فهمم درست شد؟" }), h("dd", { text: c.success || "—" })));
  if (a.demo) el.append(h("div", { class: "banner small", text: "نمایشی: با تأیید تو این تغییر فقط شبیه‌سازی می‌شه و چیزی روی لیارا عوض نمی‌شه؛ " +
    "جواب لیارا و وضعیت «بعد» از ضبط واقعی همین برنامه روی لیارا میاد." }));
  el.append(h("div", { class: "muted small", text: a.tier >= 3 ? "🔒 فقط با دو تأیید تو اجرا می‌شه؛ ایجنت کدنویسی و مدل زبانی دکمه‌ی تأیید ندارن."
                                                               : "🔒 فقط با تأیید تو اجرا می‌شه؛ ایجنت کدنویسی و مدل زبانی دکمه‌ی تأیید ندارن." }));
  el.append(h("div", { class: "row" }), h("div", { class: "status st", role: "status" }));
  setStatus(a.id, a.status);
}

function statusText(s) {
  return { pending: "منتظر تأیید تو", executing: "در حال انجام…", done: "انجام شد", failed: "ناموفق",
           rejected: "رد شد", expired: "منقضی شد", stale: "برنامه تغییر کرده؛ کارت تازه پایین‌تره", refused: "انجام نشد" }[s] || s;
}

function setStatus(id, s, message) {
  const el = state.cards.get(id); if (!el) return;
  const row = el.querySelector(".row"); row.textContent = "";
  if (s === "pending") {
    row.append(h("button", { text: el.className.includes("t3") ? "انجامش بده" : "تأیید", onclick: () => approve(id, false) }),
               h("button", { class: "ghost", text: "نه", onclick: () => api(`/api/actions/${id}/reject`, { method: "POST" }) }));
  } else if (s === "done" && el.dataset.incident && el.dataset.caller !== "mcp" && !state.verifiedIncidents.has(el.dataset.incident)) {
    row.append(h("button", { class: "ghost", text: "بررسی نتیجه", onclick: () => runVerify(el.dataset.incident) }));
  }
  const st = el.querySelector(".st");
  st.textContent = message || statusText(s) + (s === "done" && el.dataset.demo ? " (شبیه‌سازی)" : "");
  st.className = "status st " + (s === "done" ? "good" : ["failed", "refused", "expired"].includes(s) ? "bad" : "");
  el.dataset.status = s;
}

async function approve(id, sure) {
  const el = state.cards.get(id);
  const a = el.__action;
  el.querySelectorAll("button").forEach((b) => (b.disabled = true));
  const [code, res] = await api(`/api/actions/${id}/approve`, { method: "POST", body: JSON.stringify({ args_hash: a.args_hash, sure }) });
  if (res.status === "needs_second_tap") {
    const row = el.querySelector(".row"); row.textContent = "";
    row.append(h("button", { class: "danger", text: "مطمئنم", onclick: () => approve(id, true) }),
               h("button", { class: "ghost", text: "نه", onclick: () => api(`/api/actions/${id}/reject`, { method: "POST" }) }));
    el.querySelector(".st").textContent = res.message;
    return;
  }
  const final = res.final || FINAL.has(res.status) || ["not_found", "args_mismatch"].includes(res.error);
  if (final) setStatus(id, FINAL.has(res.status) ? res.status : el.dataset.status, res.message);
  else { el.querySelectorAll("button").forEach((b) => (b.disabled = false)); if (res.message) el.querySelector(".st").textContent = res.message; }
  if (!code) el.querySelector(".st").textContent = "سرور جواب نداد؛ دوباره امتحان کن.";
  refreshLog();
}

// ---------- verify ----------

async function runVerify(incident) {
  state.verifiedIncidents.add(incident);
  const sid = status("دارم بررسی می‌کنم که درست شد یا نه… (تا ۲ دقیقه)");
  const [code, res] = await api(`/api/incidents/${encodeURIComponent(incident)}/verify`, { method: "POST" });
  clearStatus(sid);
  if (res && res.checks) renderVerify({ incident, ...res });
  else { state.verifiedIncidents.delete(incident); note(res.message || (code ? "بررسی انجام نشد." : "سرور جواب نداد؛ دوباره امتحان کن."), "bad"); }
}

function renderVerify(v) {
  const key = v.id || `${v.incident}:${v.status}:${JSON.stringify(v.after || {})}`;
  if (state.verifies.has(key)) return;
  state.verifies.add(key);
  state.verifiedIncidents.add(v.incident);
  const b = v.before || {}, a = v.after || {};
  const codes = (x) => (x || []).map((c) => (c == null ? "×" : fa(c))).join("، ");
  const icon = v.ok && !v.provisional ? "✓" : v.ok ? "…" : "✗";
  const card = h("div", { class: "box" }, h("p", { class: "headline " + (v.ok ? "good" : "bad"), text: `${icon} ${v.summary}` }));
  const ba = h("div", { class: "ba" }, h("span"), h("span", { class: "h", text: "قبل" }), h("span", { class: "h", text: "بعد" }),
    h("span", { text: a.route || b.route || "/" }), h("span", { class: "bad", text: codes(b.codes) }), h("span", { class: v.ok ? "good" : "bad", text: codes(a.codes) }));
  if (b.median_ms != null || a.median_ms != null) ba.append(h("span", { text: "زمان پاسخ" }), h("span", { text: `${fa(b.median_ms)} ms` }), h("span", { text: `${fa(a.median_ms)} ms` }));
  if (b.slope_mb_min != null && a.slope_mb_min != null) ba.append(h("span", { text: "روند حافظه" }), h("span", { text: `${fa(b.slope_mb_min, 1)} مگ/دقیقه` }), h("span", { text: `${fa(a.slope_mb_min, 1)} مگ/دقیقه` }));
  if (v.recorded) card.append(h("div", { class: "banner small", text: `نمایشی: تغییر روی لیارا انجام نشد. «بعد» = ${v.recorded}.` }));
  card.append(ba, h("ul", { class: "ev" }, ...(v.checks || []).map((c) => h("li", { class: c.ok === true ? "good" : c.ok === false ? "bad" : "", text: `${c.ok === true ? "✓" : c.ok === false ? "✗" : "…"} ${c.text}` }))));
  if (v.report) card.append(h("details", {}, h("summary", { text: "گزارش کوتاه" }), h("ul", { class: "ev" }, ...v.report.map((l) => h("li", { text: l })))));
  const row = h("div", { class: "row" });
  if (v.provisional) row.append(h("button", { class: "ghost", text: "چند دقیقه بعد دوباره بررسی کن", onclick: () => runVerify(v.incident) }));
  else if (v.status === "handed_to_support") row.append(h("button", { text: "گزارش برای پشتیبانی لیارا", onclick: () => showTicket(state.app) }));
  else if (!v.ok) row.append(h("button", { class: "ghost", text: "دوباره تشخیص بده", onclick: () => ask("هنوز درست نشده؛ دوباره بررسی کن") }));
  card.append(row);
  if (v.ok && !v.provisional) retireBoards(v.incident, null);
  say(card);
  loadOverview();
}

// ---------- watcher (background health + resource checks, liara/sre/watcher.py) ----------

const clock = (t) => new Date(t * 1000).toLocaleTimeString("fa-IR", { hour: "2-digit", minute: "2-digit" });

function renderWatch(w) {
  if (!w) return;
  const apps = Object.entries(w.apps || {});
  const bad = apps.filter(([, r]) => r.problems.length);
  $("watchState").textContent = w.error ? "خطا در خواندن" : !w.last_run ? "در حال شروع…" : bad.length ? `${fa(bad.length)} مشکل` : "همه سالم";
  $("watchState").className = "pill" + (w.error || bad.length ? " bad" : w.last_run ? " ok" : "");
  $("watchWhen").textContent = !w.last_run ? "" : w.demo ? "نمایشی: روی داده‌ی ضبط‌شده، هر دقیقه"
    : `هر ${fa(Math.round(w.interval_s / 60) || 1)} دقیقه · آخرین بررسی ${clock(w.last_run)}`;
  if (w.last_run) state.watchSeen = true;
  $("watchApps").textContent = "";
  apps.forEach(([name, r]) => $("watchApps").append(h("li", { class: r.problems.length ? "bad" : "good",
    text: `${r.problems.length ? "✗" : "✓"} \u2066${name}\u2069: ${r.problems.length ? r.problems.join(" ") : r.summary}` })));
  $("watchAlerts").textContent = "";
  (w.alerts || []).slice(-5).reverse().forEach((a) => $("watchAlerts").append(alertLine(a)));
}

function alertLine(a) {
  const li = h("li", { class: a.ok ? "good" : "bad", text: `${clock(a.at)} · ${a.text} ` });
  if (!a.ok) li.append(h("button", { class: "ghost", text: "بررسی کن", onclick: () => { pickApp(a.app); ask(a.text); } }));
  return li;
}

// ---------- coding agent ----------

function agentPaired(on) {
  $("agentState").textContent = on ? "وصل" : "وصل نیست";
  $("agentState").className = "pill" + (on ? " ok" : "");
}

$("pairBtn").onclick = async () => {
  const [code, res] = await api("/api/pair", { method: "POST" });
  if (code !== 200) { note(res.message || "کد ساخته نشد."); return; }
  $("pairOut").classList.remove("hidden");
  $("pairCode").textContent = res.code;
  $("pairMsg").textContent = `به ایجنت کدنویسی‌ات بگو: «با کد ${res.code} به دستیار لیارا وصل شو». ۱۰ دقیقه اعتبار داره و یک‌بار مصرفه.`;
};

// ---------- first deploy: zip → analysis → create (approval card) → build / heal progress → live ----------

const KIND_ICON = { build: "⏳", ready: "✓", probe: "…", crash: "✗", heal: "🔧", build_failed: "✗", created: "✓",
                    waiting: "…", live: "✓", stopped: "■" };

$("zip").onchange = () => { $("zipName").textContent = $("zip").files[0]?.name || "فایلی انتخاب نشده"; };

$("zipBtn").onclick = async () => {
  const f = $("zip").files[0];
  if (!f) { $("zip").focus(); return; }
  if (f.size > 50 * 1024 * 1024) { note("حجم فایل zip از ۵۰ مگابایت بیشتره.", "bad"); return; }
  $("zipBtn").disabled = true;
  const sid = status("دارم پروژه رو نگاه می‌کنم…");
  let r, res = {};
  try {
    r = await fetch(`/api/projects?filename=${encodeURIComponent(f.name)}`, { method: "POST", credentials: "same-origin",
      headers: { "content-type": "application/zip" }, body: f });
    res = await r.json().catch(() => ({}));
  } catch { /* shown below */ }
  $("zipBtn").disabled = false; clearStatus(sid); $("zip").value = ""; $("zipName").textContent = "فایلی انتخاب نشده";
  if (!r || r.status !== 200) { note(res.message || "پروژه بررسی نشد.", "bad"); return; }
  renderProject(res);
};

$("ghForm").onsubmit = async (e) => {
  e.preventDefault();
  const url = $("ghUrl").value.trim();
  if (!url) { $("ghUrl").focus(); return; }
  const btn = $("ghForm").querySelector("button"); btn.disabled = true;
  const sid = status("دارم مخزن رو از گیت‌هاب می‌گیرم و نگاهش می‌کنم…");
  const [code, res] = await api("/api/projects/github", { method: "POST", body: JSON.stringify({ url }) });
  btn.disabled = false; clearStatus(sid);
  if (code !== 200) { note(res.message || "پروژه بررسی نشد.", "bad"); return; }
  $("ghUrl").value = "";
  renderProject(res);
};

function renderProject(job) {
  try { sessionStorage.setItem("job", job.id); } catch { /* private mode */ }
  const a = job.analysis, p = job.plan || {};
  const card = h("div", { class: "box" }, h("p", { class: "headline", text: `پروژه‌ی ${a.framework}` }),
    h("dl", { class: "kv" },
      h("dt", { text: "چی دیدم" }), h("dd", { text: a.summary }),
      h("dt", { text: "روش" }), h("dd", { text: a.method }),
      h("dt", { text: "پلن پیشنهادی" }), h("dd", { text: p.plan_id ? `${p.reason} (پلن ⁦${p.plan_id}⁩)` : p.reason || "—" }),
      h("dt", { text: "هزینه" }), h("dd", { text: p.month ? `حدود ${fa(p.month)} تومان در ماه (ساعتی از اعتبار کم می‌شه)` : "—" })));
  if (a.preflight.length) card.append(h("div", { class: "small", text: "قبل از ارسال درست کردم:" }),
    h("ul", { class: "steps" }, ...a.preflight.map((x) => h("li", { text: "🔧 " + x.title }))));
  a.warnings.forEach((w) => card.append(h("div", { class: "muted small", text: "⚠ " + w })));
  if (job.state === "analyzed" || (job.state === "stopped" && !job.created)) {
    const name = h("input", { class: "ltr", value: job.suggested_app, "aria-label": "اسم برنامه روی لیارا" });
    const go = h("button", { text: "بساز و آنلاین کن" });
    const msg = h("div", { class: "muted small", role: "status" });
    go.onclick = async () => {
      go.disabled = true;
      const [code, res] = await api(`/api/projects/${job.id}/create`, { method: "POST", body: JSON.stringify({ app: name.value }) });
      if (res.status === "waiting_for_user") { msg.textContent = "کارت ساخت برنامه پایین‌تر اومد؛ با تأیید تو ساخته می‌شه و بعدش همه‌چیز خودکاره."; name.disabled = true; return; }
      go.disabled = false;
      msg.textContent = res.message || (code ? "انجام نشد." : "سرور جواب نداد.");
    };
    card.append(h("div", { class: "small mt", text: "اسم برنامه روی لیارا (آدرسش می‌شه اسم.liara.run)" }),
      h("div", { class: "row" }, name, go), msg);
  }
  say(card);
  state.jobs.set(job.id, { card: null, seen: new Set() });
  (job.events || []).forEach((ev) => renderDeployEvent(ev));
}

function jobCard(id) {
  const j = state.jobs.get(id) || { seen: new Set() };
  state.jobs.set(id, j);
  if (!j.card) {
    j.list = h("ul", { class: "steps" });
    j.card = say(h("div", { class: "box" }, h("b", { text: "آنلاین کردن" }), j.list));
  }
  return j;
}

function renderDeployEvent(ev) {
  const j = jobCard(ev.job);
  const key = `${ev.t}:${ev.kind}:${ev.text}`;
  if (j.seen.has(key)) return;
  j.seen.add(key);
  const li = h("li", { class: ["crash", "build_failed", "stopped"].includes(ev.kind) ? "bad" : ev.kind === "live" ? "good" : "" },
    `${KIND_ICON[ev.kind] || "•"} ${ev.text}`);
  if (ev.line) li.append(h("span", { class: "ln", text: ev.line }));
  if (ev.diff) li.append(h("details", {}, h("summary", { text: `تغییر در ${ev.file}` }), h("pre", { text: ev.diff })));
  j.list.append(li);
  if (ev.kind === "live") renderLive(ev);
  else if (ev.kind === "stopped" && ev.app) j.card.append(h("div", { class: "row" },
    h("button", { class: "ghost", text: "گزارش برای پشتیبانی", onclick: () => showTicket(ev.app) })));
  else if (ev.kind === "stopped") j.card.append(h("div", { class: "muted small", text: "برای دوباره امتحان کردن، دکمه‌ی «بساز و آنلاین کن» در کارت پروژه هنوز کار می‌کنه." }));
  li.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderLive(ev) {
  const j = state.jobs.get(ev.job);
  const card = h("div", { class: "box" }, h("p", { class: "headline good", text: "✓ آنلاین شد" }),
    h("a", { href: ev.url, target: "_blank", rel: "noopener noreferrer", class: "ltr", text: ev.url }));
  if ((ev.changes || []).length) card.append(h("div", { class: "small mt", text: "کارهایی که روی پروژه‌ات انجام دادم:" }),
    h("ul", { class: "steps" }, ...ev.changes.map((c) => h("li", { text: "🔧 " + c }))));
  card.append(h("div", { class: "row" },
    h("a", { href: `/api/projects/${encodeURIComponent(ev.job)}/patch`, class: "small", text: "دانلود تغییرها (patch) برای مخزن خودت" }),
    h("a", { href: `/api/projects/${encodeURIComponent(ev.job)}/liara.json`, class: "small", text: "دانلود liara.json" })),
    h("details", {}, h("summary", { text: "دیپلوی خودکار با هر push (GitHub Actions)" }),
      h("ol", { class: "small" },
        h("li", { text: "فایل workflow رو دانلود کن و در مخزنت در مسیر .github/workflows/liara.yml بذار." }),
        h("li", { text: "در console.liara.ir یک کلید API با کوتاه‌ترین اعتبار ممکن بساز (کلیدها به همه‌چیز دسترسی دارن)." }),
        h("li", { text: "در گیت‌هاب: Settings ← Secrets and variables ← Actions ← New repository secret، با اسم LIARA_API_TOKEN." }),
        h("li", { text: "push کن و نتیجه رو در تب Actions ببین؛ اگر بیلد یا اجرا خراب بشه، کار قرمز می‌شه." })),
      h("div", { class: "row" }, h("a", { href: `/api/projects/${encodeURIComponent(ev.job)}/workflow.yml`, class: "small", text: "دانلود فایل workflow" })),
      h("div", { class: "muted small", text: "کلید رو هیچ‌وقت داخل فایل یا به من نده؛ فقط در secret گیت‌هاب. راه دیگه، اتصال مستقیم گیت‌هاب از کنسول لیاراست." })),
    h("div", { class: "muted small", text: "این تغییرها رو در مخزن خودت هم اعمال کن، وگرنه دیپلوی بعدی از کد اصلی دوباره خراب می‌شه." }));
  j.card.after(card);
  loadApps();
}

async function restoreJob() {
  let id = null;
  try { id = sessionStorage.getItem("job"); } catch { /* private mode */ }
  if (!id) return;
  const [code, job] = await api(`/api/projects/${encodeURIComponent(id)}`);
  if (code === 200 && job.id) renderProject(job);
}

// ---------- custom domain ----------

$("domainBtn").onclick = () => { $("domainForm").classList.toggle("hidden"); $("domainText").focus(); };
$("domainForm").onsubmit = async (e) => {
  e.preventDefault();
  const domain = $("domainText").value.trim();
  if (!domain || !state.app) return;
  const [code, res] = await api("/api/domains", { method: "POST", body: JSON.stringify({ app: state.app, domain }) });
  if (res.status === "waiting_for_user") { state.domain = { app: state.app, domain }; status("کارت وصل کردن دامنه پایین‌تر اومد."); return; }
  if (res.status === "exists") { state.domain = { app: state.app, domain }; renderDomain(res.domain_status); return; }
  note(res.message || (code ? "انجام نشد." : "سرور جواب نداد."), "bad");
};

async function checkDomain() {
  if (!state.domain) return;
  const sid = status("دارم وضعیت دامنه رو از لیارا می‌پرسم…");
  const [, res] = await api("/api/domains/check", { method: "POST", body: JSON.stringify(state.domain) });
  clearStatus(sid);
  renderDomain(res);
}

function renderDomain(d) {
  if (state.domainCard) state.domainCard.remove();
  if (!d || !d.domain || ["error", "not_added"].includes(d.state)) { note(d?.message || "دامنه پیدا نشد.", "bad"); return; }
  const head = { waiting_dns: "منتظر تنظیم DNS", verified: "DNS درسته؛ حالا SSL", ssl_active: "دامنه و SSL فعاله" }[d.state] || d.state;
  const card = h("div", { class: "box" }, h("p", { class: "headline " + (d.state === "ssl_active" ? "good" : ""), text: `${d.domain} — ${head}` }),
    h("div", { class: "muted small", text: `وضعیت در لیارا: ${d.status || "—"} · گواهی: ${d.certificates || "—"}` }));
  if (d.records.length && d.state !== "ssl_active") {
    const rec = h("div", { class: "rec" }, h("span", { class: "muted small", text: "نوع" }), h("span", { class: "muted small", text: "نام" }),
      h("span", { class: "muted small", text: "مقدار" }), h("span"));
    d.records.forEach((r) => rec.append(h("span", { text: r.type }), h("span", { class: "v", text: r.name }), h("span", { class: "v", text: r.value }), copyButton(r.value, "کپی")));
    card.append(h("div", { class: "small", text: "این رکوردها رو لیارا می‌خواد:" }), rec);
    d.records.filter((r) => r.note).forEach((r) => card.append(h("div", { class: "muted small", text: `${r.type}: ${r.note}` })));
    card.append(h("details", { open: "" }, h("summary", { text: `قدم‌ها — ${d.guide.title} (راهنمای خود ما؛ پنل ممکنه کمی فرق کنه)` }),
      h("ol", { class: "small" }, ...d.guide.steps.map((s) => h("li", { text: s })))),
      h("div", { class: "muted small", text: d.limit.fa }));
  }
  if (d.https) card.append(h("div", { class: d.https.status === 200 ? "good" : "bad",
    text: d.https.status === 200 ? `https://${d.domain} باز شد (${fa(d.https.ms)} ms)` : `https://${d.domain} هنوز باز نمی‌شه (${d.https.status || "بدون پاسخ"})` }));
  const row = h("div", { class: "row" });
  if (d.state === "verified") row.append(h("button", { text: "گرفتن SSL رایگان", onclick: async () => {
    const [, res] = await api("/api/domains/ssl", { method: "POST", body: JSON.stringify(state.domain) });
    if (res.status !== "waiting_for_user") note(res.message || "انجام نشد.");
  } }));
  row.append(h("button", { class: d.state === "verified" ? "ghost" : "", text: d.state === "waiting_dns" ? "انجام دادم، بررسی کن" : "دوباره بررسی کن", onclick: checkDomain }));
  card.append(row);
  state.domainCard = say(card);
}

// ---------- live events ----------

async function resync() {
  // after (re)connecting the stream: actions that changed while it was down are re-drawn from the server
  const [code, res] = await api("/api/actions");
  if (code !== 200) return;
  agentPaired(!!res.bridge);
  (res.actions || []).forEach((a) => { renderActionKeep(a); });
}

function renderActionKeep(a) {
  renderAction(a);
  state.cards.get(a.id).__action = a;
}

function listen() {
  if (state.es) state.es.close();
  const es = state.es = new EventSource("/api/stream");
  es.addEventListener("hello", (e) => { resync(); renderWatch(JSON.parse(e.data).watch); });
  es.addEventListener("watch", (e) => renderWatch(JSON.parse(e.data)));
  es.addEventListener("alert", (e) => { const a = JSON.parse(e.data); if (!a.ok && state.watchSeen) say(h("div", { class: "box" }, h("ul", { class: "ev" }, alertLine(a)))); });
  es.addEventListener("status", (e) => status(JSON.parse(e.data).text));
  es.addEventListener("diagnosis", (e) => { status(""); renderBoard(JSON.parse(e.data)); });
  es.addEventListener("verify", (e) => { status(""); renderVerify(JSON.parse(e.data)); });
  es.addEventListener("action_proposed", (e) => renderActionKeep(JSON.parse(e.data)));
  es.addEventListener("action_running", (e) => setStatus(JSON.parse(e.data).id, "executing"));
  es.addEventListener("action_stale", (e) => setStatus(JSON.parse(e.data).id, "stale"));
  es.addEventListener("action_result", (e) => {
    const d = JSON.parse(e.data);
    const s = d.rejected ? "rejected" : d.ok ? "done" : "failed";
    const card = state.cards.get(d.id);
    // a fix chosen on this page is verified right away; a coding-agent deploy is verified by the agent's verify_fix
    const autoVerify = d.ok && card?.dataset.incident && card.dataset.caller !== "mcp" && !state.verifiedIncidents.has(card.dataset.incident);
    if (autoVerify) state.verifiedIncidents.add(card.dataset.incident);
    setStatus(d.id, s, d.message);
    refreshLog(); loadOverview();
    if (autoVerify) runVerify(card.dataset.incident);
    if (d.ok && ["add_domain", "provision_ssl"].includes(card?.dataset.tool)) checkDomain();
  });
  es.addEventListener("deploy", (e) => renderDeployEvent(JSON.parse(e.data)));
  es.addEventListener("bridge_paired", (e) => {
    agentPaired(true);
    $("pairCode").textContent = "✓"; $("pairMsg").textContent = JSON.parse(e.data).text;
  });
  es.onerror = async () => {
    const [code] = await api("/api/actions");
    if (code === 401) es.close();               // the session is gone: api() already switched to the connect screen
  };
}

async function refreshLog() {
  const [code, res] = await api("/api/log");
  if (code === 200) $("log").textContent = res.log.map((e) => JSON.stringify(e)).join("\n") + `\n— تماس‌های API با لیارا: ${fa(res.api_calls)}`;
}

// already connected in this tab's cookie?
fetch("/api/actions", { credentials: "same-origin" }).then((r) => { if (r.status === 200) start({}); }).catch(() => {});
fetch("/health").then((r) => r.json()).then((h) => {
  if (h.demo) $("demoHint").classList.remove("hidden");
  if (h.both) $("bothHint").classList.remove("hidden");
}).catch(() => {});
