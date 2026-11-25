/* Requirements Validator — одностраничный интерфейс.
   Все данные приходят из API сервера (web.py), который вызывает то же ядро, что и CLI.
   Текст из документа и ответы LLM выводятся только через textContent (защита от XSS). */
"use strict";

const app = document.getElementById("app");
let CONFIG = null;
let JOB = null;          // последний ответ /api/jobs/<id>
let pollTimer = null;
const state = { sev: new Set(), crit: "", src: "", q: "", selReq: null };

// ------------------------------------------------------------------ утилиты

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const svgNS = "http://www.w3.org/2000/svg";
function s(tag, attrs, ...children) {
  const el = document.createElementNS(svgNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  for (const c of children) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}
function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  const w = m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && !(m100 >= 12 && m100 <= 14) ? few : many;
  return `${n} ${w}`;
}
function toast(msg) {
  const t = h("div", { class: "toast", role: "status", text: msg });
  document.body.append(t);
  setTimeout(() => t.remove(), 3500);
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (_) { /* не JSON */ }
  if (!r.ok) throw new Error((data && data.error) || `Ошибка сервера (${r.status})`);
  return data;
}
const SEV = { critical: "Критично", major: "Существенно", minor: "Незначительно", ok: "Без замечаний" };
const SEV_ORDER = { critical: 0, major: 1, minor: 2 };
const SOURCE = { rule: "Правило", llm: "LLM", "rule+llm": "Правило + LLM" };
const SEV_SHORT = { critical: "Крит.", major: "Сущ.", minor: "Незнач.", ok: "OK" };
function sevBadge(sev, short) {
  return h("span", { class: `badge sev-${sev}`, title: SEV[sev] || sev, text: (short ? SEV_SHORT : SEV)[sev] || sev });
}
function sourceTag(src) {
  const cls = src === "rule" ? "tag tag-rule" : "tag tag-llm";
  return h("span", { class: cls, title: "Кто нашёл", text: SOURCE[src] || src });
}
function critName(key) {
  const c = JOB && JOB.report.stats.criteria[key];
  return c ? c.name : key;
}
function critRef(key) {
  const c = JOB && JOB.report.stats.criteria[key];
  return c ? c.source : "";
}

// ------------------------------------------------------------------ маршрутизация

function route() {
  clearTimeout(pollTimer);
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  if (parts[0] === "job" && parts[1]) return showJob(parts[1], parts[2] || "overview", parts[3]);
  JOB = null;
  renderUpload();
}
window.addEventListener("hashchange", route);

async function init() {
  try {
    CONFIG = await api("/api/config");
    const chip = document.getElementById("mode-chip");
    chip.textContent = CONFIG.llm_enabled ? `Модель: ${CONFIG.model}` : "Режим: только правила";
    chip.title = CONFIG.llm_enabled ? "LLM подключена" : "Ключ LLM не найден в .env — смысловые проверки отключены";
  } catch (e) {
    CONFIG = { llm_enabled: false, formats: [".md", ".pdf", ".docx", ".txt"], max_mb: 10, samples: [] };
  }
  route();
}

// ------------------------------------------------------------------ экран загрузки

function renderUpload(error) {
  let file = null;
  const input = h("input", { type: "file", class: "sr", id: "file", accept: CONFIG.formats.join(",") });
  const picked = h("div");
  const offline = h("input", { type: "checkbox", id: "offline", disabled: !CONFIG.llm_enabled, checked: !CONFIG.llm_enabled });
  const noCritic = h("input", { type: "checkbox", id: "nocritic" });
  const errBox = h("div", { role: "alert" });
  const go = h("button", { class: "btn btn-lg", disabled: true, onclick: start }, "Проверить документ");

  function pick(f) {
    if (!f) return;
    const ext = "." + f.name.split(".").pop().toLowerCase();
    errBox.replaceChildren();
    if (!CONFIG.formats.includes(ext)) {
      errBox.append(h("div", { class: "alert alert-err", text: `Формат ${ext} не поддерживается. Нужен: ${CONFIG.formats.join(", ")}.` }));
      return;
    }
    if (f.size > CONFIG.max_mb * 1024 * 1024) {
      errBox.append(h("div", { class: "alert alert-err", text: `Файл больше ${CONFIG.max_mb} МБ.` }));
      return;
    }
    file = f;
    picked.replaceChildren(h("div", { class: "file-picked" },
      h("span", { class: "tag", text: ext.slice(1).toUpperCase() }),
      h("span", { class: "name", text: f.name }),
      h("span", { class: "muted small", text: `${Math.max(1, Math.round(f.size / 1024))} КБ` }),
      h("span", { class: "spacer" }),
      h("button", { class: "btn btn-ghost btn-sm", onclick: () => { file = null; picked.replaceChildren(); go.disabled = true; } }, "Убрать")));
    go.disabled = false;
  }
  input.addEventListener("change", () => pick(input.files[0]));
  const drop = h("label", { class: "drop", for: "file", tabindex: "0",
    onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } } },
    svgIcon(), h("b", { text: "Перетащите документ сюда или выберите файл" }),
    h("span", { class: "muted small", text: `${CONFIG.formats.join(", ")} · до ${CONFIG.max_mb} МБ` }));
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => pick(e.dataTransfer.files[0]));

  async function start() {
    go.disabled = true;
    go.textContent = "Загружаю…";
    const fd = new FormData();
    fd.append("file", file);
    if (offline.checked) fd.append("offline", "1");
    if (noCritic.checked) fd.append("no_critic", "1");
    try {
      const r = await api("/api/analyze", { method: "POST", body: fd });
      location.hash = `#/job/${r.id}`;
    } catch (e) {
      go.disabled = false;
      go.textContent = "Проверить документ";
      errBox.replaceChildren(h("div", { class: "alert alert-err", text: e.message }));
    }
  }
  async function sample(id) {
    try {
      const r = await api(`/api/sample/${id}`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ offline: offline.checked ? "1" : "", no_critic: noCritic.checked ? "1" : "" }) });
      location.hash = `#/job/${r.id}`;
    } catch (e) { errBox.replaceChildren(h("div", { class: "alert alert-err", text: e.message })); }
  }

  const SAMPLE_NAMES = { requirements: "ТЗ «Уведомление о расходах по карте» (задание хакатона)", cashback: "ТЗ «Кешбэк в кафе» (тестовое)" };
  app.replaceChildren(
    h("section", { class: "hero" },
      h("h1", { text: "Проверка технического задания до начала разработки" }),
      h("p", { text: "Агент разбивает документ на требования, проверяет каждое и весь набор по ISO/IEC/IEEE 29148 и IEEE 830, находит противоречия, подтверждает каждую цитату и готовит вопросы автору. Итог — заключение о готовности к разработке и тестированию." })),
    h("div", { class: "upload-grid" },
      h("section", { class: "card card-pad" },
        h("h2", { text: "Документ" }),
        h("div", { class: "stack", style: "margin-top:14px" }, input, drop, picked,
          h("div", { class: "opts" },
            h("label", { class: "opt", for: "offline" }, offline, h("span", {},
              h("b", { text: "Только правила, без LLM" }), h("br"),
              h("span", { class: "muted small", text: CONFIG.llm_enabled ? "Быстро и без обращения к внешнему API. Смысловые проверки и противоречия не выполняются." : "Ключ LLM не найден в .env, доступна только проверка правилами." }))),
            h("label", { class: "opt", for: "nocritic" }, noCritic, h("span", {},
              h("b", { text: "Без критика" }), h("br"),
              h("span", { class: "muted small", text: "На один запрос к LLM меньше. Критик отсеивает ложные срабатывания — оставьте его включённым для сдачи." })))),
          errBox,
          h("div", { class: "row" }, go),
          CONFIG.samples.length ? h("div", { class: "row small muted" }, "Или запустите на примере:",
            CONFIG.samples.map((sm) => h("button", { class: "btn btn-ghost btn-sm", onclick: () => sample(sm.id) }, SAMPLE_NAMES[sm.id] || sm.name))) : null,
          error ? h("div", { class: "alert alert-err", text: error }) : null)),
      h("aside", { class: "card card-pad" },
        h("h2", { text: "Как работает проверка" }),
        h("ol", { class: "steps-list", style: "margin-top:16px" },
          stepItem("Чтение и нарезка", "PDF, DOCX, Markdown → отдельные требования с номерами строк"),
          stepItem("Правила", "Размытые слова, заглушки, HTTP-контракт, типы, единицы — одинаково при каждом запуске"),
          stepItem("LLM-аналитик", "Каждое требование и набор целиком: полнота, однозначность, противоречия"),
          stepItem("Проверка цитат кодом", "Замечание без найденной в документе цитаты отбрасывается"),
          stepItem("Критик и вопросы автору", "Отсев ложных срабатываний, уточняющие вопросы с вариантами ответа"),
          stepItem("Заключение", "Можно ли начинать разработку и тестирование, что исправить первым")))));
}
function stepItem(t, d) { return h("li", {}, h("b", { text: t }), h("span", { text: d })); }
function svgIcon() {
  return s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" },
    s("path", { d: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" }),
    s("path", { d: "M14 3v5h5M12 17v-6M9.5 13.5L12 11l2.5 2.5" }));
}

// ------------------------------------------------------------------ задача: прогресс или отчёт

async function showJob(id, tab, arg) {
  try {
    const data = await api(`/api/jobs/${id}`);
    const prev = JOB;
    JOB = data;
    if (data.status === "running") {
      renderProgress(data);
      pollTimer = setTimeout(() => showJob(id, tab, arg), 1000);
      return;
    }
    if (data.status === "error") return renderError(data);
    if (data.status === "applying") pollTimer = setTimeout(() => showJob(id, tab, arg), 1500);
    renderReport(tab, arg, prev && prev.id === id);
  } catch (e) {
    app.replaceChildren(h("div", { class: "card card-pad", style: "margin:36px 0" },
      h("h1", { text: "Проверка не найдена" }),
      h("p", { class: "muted", text: "Сервер перезапускался или ссылка устарела. Результаты хранятся в памяти сервера до его остановки." }),
      h("a", { class: "btn", href: "#/" }, "Новая проверка")));
  }
}

function renderProgress(d) {
  const total = d.stages.length;
  const done = Math.max(0, d.stage - 1);
  app.replaceChildren(h("div", { class: "progress-wrap" },
    h("div", { class: "card card-pad" },
      h("div", { class: "row" }, h("h1", { text: "Анализ документа" }), h("span", { class: "spacer" }),
        h("span", { class: "chip mono", text: `${Math.round(d.elapsed)} с` })),
      h("p", { class: "muted", style: "margin:6px 0 16px" }, h("b", { text: d.name }), " · с LLM проверка занимает 1–3 минуты: бесплатный тариф ограничивает частоту запросов."),
      h("div", { class: "bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(total), "aria-valuenow": String(done) },
        h("i", { style: `width:${Math.round((done / total) * 100)}%` })),
      h("ol", { class: "stages", style: "margin-top:12px" },
        d.stages.map((name, i) => {
          const n = i + 1;
          const st = n < d.stage ? "done" : n === d.stage ? "active" : "pending";
          return h("li", { class: `stage ${st}` }, h("span", { class: "dot", text: st === "done" ? "✓" : st === "active" ? "" : String(n) }),
            h("span", { class: "label", text: name }),
            h("span", { class: "small faint", text: st === "done" ? "готово" : st === "active" ? "выполняется" : "" }));
        })),
      h("h3", { style: "margin:18px 0 8px", text: "Журнал" }),
      h("div", { class: "log" }, d.log.map((l) => h("div", { text: `[${l.t.toFixed(1).padStart(5)} с] ${l.msg}` }))))));
}

function renderError(d) {
  app.replaceChildren(h("div", { class: "progress-wrap" }, h("div", { class: "card card-pad stack" },
    h("h1", { text: "Не удалось проверить документ" }),
    h("div", { class: "alert alert-err", text: d.error }),
    h("p", { class: "muted", text: "Проверьте, что файл не повреждён и содержит текст (сканы без текстового слоя не поддерживаются)." }),
    h("a", { class: "btn", href: "#/" }, "Загрузить другой документ"))));
}

// ------------------------------------------------------------------ отчёт

const TABS = [
  ["overview", "Обзор"], ["requirements", "Требования"], ["issues", "Дефекты"], ["contradictions", "Противоречия"],
  ["questions", "Вопросы автору"], ["document", "Документ"], ["passport", "Паспорт"],
];

function resolvedSet() {
  return new Set(JOB.report.resolutions.flatMap((r) => r.resolved_issue_ids));
}

function renderReport(tab, arg, keepScroll) {
  const R = JOB.report, S = R.stats, V = R.verdict;
  const y = keepScroll ? window.scrollY : 0;
  const status = V.ready_for_dev ? (S.issues_by_severity.major ? "warn" : "ok") : "bad";
  const title = V.ready_for_dev ? (status === "ok" ? "Документ готов к разработке" : "Можно начинать после согласования замечаний")
    : "Документ не готов к разработке";
  const counts = {
    requirements: S.requirements_total, issues: S.issues_total, contradictions: S.contradictions,
    questions: S.questions_total,
  };
  const content = h("div");
  app.replaceChildren(
    h("section", { class: `card verdict ${status}` },
      h("div", { class: "verdict-icon", "aria-hidden": "true", text: status === "ok" ? "✓" : status === "warn" ? "!" : "✕" }),
      h("div", {},
        h("div", { class: "small muted", text: R.run.doc_name }),
        h("h1", { text: title }),
        h("p", { text: V.summary }),
        h("div", { class: "flags" },
          flag(V.ready_for_dev, "Разработка"), flag(V.ready_for_test, "Тестирование"),
          JOB.status === "applying" ? h("span", { class: "chip", text: "Учитываю ответы автора…" }) : null,
          S.issues_resolved ? h("span", { class: "chip", text: `Закрыто ответами: ${S.issues_resolved}` }) : null)),
      h("div", { class: "verdict-actions" }, exportMenu(), h("a", { class: "btn btn-ghost", href: "#/" }, "Новая проверка"))),
    h("nav", { class: "tabs", "aria-label": "Разделы отчёта" },
      TABS.map(([key, label]) => h("a", { href: `#/job/${JOB.id}/${key}`, "aria-current": key === tab ? "page" : null },
        label, counts[key] !== undefined ? h("span", { class: `count${key === "contradictions" && counts[key] ? " hot" : ""}`, text: counts[key] }) : null))),
    content);
  const views = { overview: viewOverview, requirements: viewRequirements, issues: viewIssues, contradictions: viewContradictions,
    questions: viewQuestions, document: viewDocument, passport: viewPassport };
  (views[tab] || viewOverview)(content, arg);
  if (keepScroll) window.scrollTo(0, y);
}
function flag(ok, label) {
  return h("span", { class: `badge ${ok ? "sev-ok" : "sev-critical"}`, text: `${label}: ${ok ? "можно" : "нельзя"}` });
}
function exportMenu() {
  const list = h("div", { class: "menu-list", hidden: true },
    [["md", "Markdown", "основной отчёт"], ["html", "HTML", "для печати и PDF"], ["json", "JSON", "для интеграций"]].map(([ext, name, hint]) =>
      h("a", { href: `/api/jobs/${JOB.id}/export.${ext}${ext === "html" ? "?download=1" : ""}`, download: "" }, h("span", { text: name }), h("small", { text: hint }))),
    h("a", { href: `/api/jobs/${JOB.id}/export.html`, target: "_blank", rel: "noopener" }, h("span", { text: "Открыть отчёт" }), h("small", { text: "в новой вкладке" })));
  const btn = h("button", { class: "btn", "aria-haspopup": "true", "aria-expanded": "false",
    onclick: (e) => { e.stopPropagation(); list.hidden = !list.hidden; btn.setAttribute("aria-expanded", String(!list.hidden)); } }, "Экспорт отчёта ▾");
  document.addEventListener("click", () => { list.hidden = true; btn.setAttribute("aria-expanded", "false"); }, { once: true });
  return h("div", { class: "menu" }, btn, list);
}

// ---- Обзор
function viewOverview(root) {
  const R = JOB.report, S = R.stats;
  const sev = S.issues_by_severity;
  const rs = S.requirements_by_status;
  const q = S.quality_index;
  const color = q >= 80 ? "var(--ok)" : q >= 60 ? "var(--major)" : "var(--crit)";
  const C = 2 * Math.PI * 27;
  const ring = s("svg", { class: "ring", viewBox: "0 0 64 64", role: "img", "aria-label": `Индекс качества ${q} из 100` },
    s("circle", { class: "bg", cx: 32, cy: 32, r: 27 }),
    s("circle", { class: "fg", cx: 32, cy: 32, r: 27, stroke: color, "stroke-dasharray": `${(C * q) / 100} ${C}` }),
    s("text", { x: 32, y: 32 }, String(q)));

  const kpis = h("div", { class: "kpis" },
    h("div", { class: "card kpi kpi-quality" }, ring, h("div", {},
      h("div", { class: "k-label", text: "Индекс качества" }),
      h("div", { class: "k-sub", text: "доля требований без критичных и существенных замечаний" }))),
    kpi("Требований", S.requirements_total, `${rs.ok} без замечаний`),
    kpi("Дефектов", S.issues_total, `🔴 ${sev.critical} · 🟠 ${sev.major} · 🟡 ${sev.minor}`),
    kpi("Противоречий", S.contradictions, "между требованиями и частями документа", `#/job/${JOB.id}/contradictions`),
    kpi("Вопросов автору", S.questions_total, `${S.questions_blocking} блокирующих`, `#/job/${JOB.id}/questions`));

  const crits = Object.entries(S.issues_by_criterion);
  const maxC = Math.max(1, ...crits.map(([, n]) => n));
  const byCrit = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Дефекты по критериям ISO 29148 / IEEE 830" })),
    h("div", { class: "card-body" }, crits.length ? crits.map(([k, n]) => h("div", { class: "hbar" },
      h("button", { title: `${critName(k)} (${critRef(k)}) — показать дефекты`, onclick: () => { state.crit = k; state.sev.clear(); state.src = ""; state.q = ""; location.hash = `#/job/${JOB.id}/issues`; } }, critName(k)),
      h("div", { class: "track" }, h("i", { style: `width:${(n / maxC) * 100}%` })),
      h("span", { class: "n", text: n }))) : h("p", { class: "muted", text: "Дефектов нет." })));

  const total = Math.max(1, S.requirements_total);
  const seg = (n, c) => h("i", { style: `width:${(n / total) * 100}%;background:${c}`, title: String(n) });
  const reqHealth = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Состояние требований" })),
    h("div", { class: "card-body" },
      h("div", { class: "stackbar", role: "img", "aria-label": "Распределение требований по состоянию" },
        seg(rs.critical, "var(--crit)"), seg(rs.major, "var(--major)"), seg(rs.minor, "var(--minor)"), seg(rs.ok, "var(--ok)")),
      h("div", { class: "legend" },
        legend("var(--crit)", `критичные — ${rs.critical}`), legend("var(--major)", `существенные — ${rs.major}`),
        legend("var(--minor)", `незначительные — ${rs.minor}`), legend("var(--ok)", `без замечаний — ${rs.ok}`)),
      h("div", { class: "legend", style: "margin-top:14px" },
        h("span", { text: `Кто нашёл: правила — ${S.issues_by_source.rule}, LLM — ${S.issues_by_source.llm}, правило + LLM — ${S.issues_by_source.both}` }))));

  const fixes = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Что исправить в первую очередь" })),
    h("div", { class: "card-body" }, R.verdict.top_fixes.length ? h("ul", { class: "fixlist" }, R.verdict.top_fixes.map((f) => {
      const id = (f.match(/^D-\d+/) || [""])[0];
      const iss = R.issues.find((i) => i.iid === id);
      return h("li", {}, iss ? sevBadge(iss.severity) : h("span"),
        h("div", {}, h("a", { href: `#/job/${JOB.id}/issues/${id}`, text: id }), " ", f.replace(/^D-\d+\s*/, "")));
    })) : h("p", { class: "muted", text: "Критичных и существенных замечаний нет." })));

  const overall = R.run.overall ? h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Оценка документа (LLM)" })),
    h("div", { class: "card-body" }, h("p", { style: "margin:0", text: R.run.overall }))) : null;

  root.append(kpis, h("div", { class: "overview-grid" }, h("div", { class: "grid" }, fixes, overall), h("div", { class: "grid" }, reqHealth, byCrit)));
  if (R.run.llm_errors.length) root.prepend(h("div", { class: "alert alert-warn", style: "margin-bottom:14px" },
    h("b", { text: "Часть проверок LLM не выполнена. " }), `${R.run.llm_errors.length} шаг(ов): подробности во вкладке «Паспорт». Проверки правилами выполнены полностью.`));
}
function kpi(label, value, sub, href) {
  const inner = [h("div", { class: "k-label", text: label }), h("div", { class: "k-value", text: value }), h("div", { class: "k-sub", text: sub })];
  return href ? h("a", { class: "card kpi", href, style: "text-decoration:none;color:inherit" }, inner) : h("div", { class: "card kpi" }, inner);
}
function legend(c, t) { return h("span", {}, h("i", { style: `background:${c}` }), t); }

// ---- Требования
function issuesFor(rid) {
  const res = resolvedSet();
  return JOB.report.issues.filter((i) => i.req_ids.includes(rid) && !res.has(i.iid));
}
function viewRequirements(root, arg) {
  const R = JOB.report;
  const reqs = R.requirements.filter((r) => r.kind !== "context");
  const reqCrit = Object.keys(R.stats.criteria).filter((k) => R.stats.criteria[k].level === "requirement");
  if (arg) state.selReq = arg;
  if (!state.selReq || !reqs.some((r) => r.rid === state.selReq)) state.selReq = reqs[0] && reqs[0].rid;
  const filter = h("input", { type: "search", placeholder: "Поиск по тексту или ID…", "aria-label": "Поиск требований" });
  const onlyBad = h("input", { type: "checkbox", id: "onlybad" });
  const tbody = h("tbody");
  const detail = h("div", { class: "card sticky" });

  function rows() {
    const q = filter.value.trim().toLowerCase();
    tbody.replaceChildren(...reqs.filter((r) => {
      const st = R.stats.requirement_status[r.rid];
      if (onlyBad.checked && st === "ok") return false;
      return !q || `${r.rid} ${r.doc_id} ${r.text} ${r.category}`.toLowerCase().includes(q);
    }).map((r) => {
      const iss = issuesFor(r.rid);
      const st = R.stats.requirement_status[r.rid];
      return h("tr", { class: r.rid === state.selReq ? "sel" : "", tabindex: "0",
        onclick: () => select(r.rid), onkeydown: (e) => { if (e.key === "Enter") select(r.rid); } },
        h("td", { class: "mono", text: r.rid }), h("td", { class: "small", text: r.doc_id }),
        h("td", {}, h("div", { class: "req-text", text: shortText(r.text, 150) }), h("div", { class: "small faint", text: `${r.category} · ${loc(r)}` })),
        h("td", { class: "mono", text: iss.filter((i) => i.req_ids.length <= 3).length || "—" }),
        h("td", {}, sevBadge(st, true)));
    }));
    if (!tbody.children.length) tbody.append(h("tr", {}, h("td", { colspan: "5", class: "empty", text: "Ничего не найдено." })));
  }
  function select(rid) {
    state.selReq = rid;
    history.replaceState(null, "", `#/job/${JOB.id}/requirements/${rid}`);
    rows();
    renderReqDetail(detail, reqs.find((r) => r.rid === rid), reqCrit);
  }
  filter.addEventListener("input", rows);
  onlyBad.addEventListener("change", rows);
  rows();
  if (state.selReq) renderReqDetail(detail, reqs.find((r) => r.rid === state.selReq), reqCrit);
  root.append(h("div", { class: "split" },
    h("div", { class: "card" },
      h("div", { class: "card-head" }, filter, h("label", { class: "opt small", for: "onlybad" }, onlyBad, "Только с замечаниями")),
      h("div", { class: "table-wrap" }, h("table", { class: "t" },
        h("thead", {}, h("tr", {}, h("th", { text: "ID" }), h("th", { text: "Док." , title: "ID в документе" }), h("th", { text: "Формулировка" }),
          h("th", { text: "Дефекты" }), h("th", { text: "Статус" }))),
        tbody))),
    detail));
}
function renderReqDetail(box, r, reqCrit) {
  if (!r) { box.replaceChildren(h("div", { class: "empty", text: "Выберите требование." })); return; }
  const iss = issuesFor(r.rid).sort((a, b) => SEV_ORDER[a.severity] - SEV_ORDER[b.severity]);
  box.replaceChildren(
    h("div", { class: "card-head" }, h("div", {}, h("h2", { text: `${r.rid}${r.doc_id !== "—" ? " · " + r.doc_id : ""}` }),
      h("div", { class: "small muted", text: `${r.category} · ${r.section}` })), sevBadge(JOB.report.stats.requirement_status[r.rid])),
    h("div", { class: "card-body stack" },
      h("div", { class: "quote" }, h("q", { text: r.text }), h("a", { href: `#/job/${JOB.id}/document/${r.line_start}`, text: loc(r) })),
      h("h3", { text: "Критерии ISO/IEC/IEEE 29148, п. 5.2.5" }),
      h("div", { class: "crit-grid" }, reqCrit.map((k) => {
        const hit = iss.filter((i) => i.criterion === k)[0];
        return h("div", { class: `crit-cell ${hit ? hit.severity : ""}`, title: critRef(k) },
          h("b", { text: hit ? "!" : "✓" }), h("span", { text: critName(k) }));
      })),
      h("h3", { text: iss.length ? `Дефекты (${iss.length})` : "Дефектов нет" }),
      iss.map((i) => issueCard(i, { compact: true }))));
}
function loc(r) { return r.line_start === r.line_end ? `строка ${r.line_start}` : `строки ${r.line_start}–${r.line_end}`; }
function shortText(t, n) { t = t.replace(/\s+/g, " ").trim(); return t.length > n ? t.slice(0, n - 1) + "…" : t; }

// ---- карточка дефекта
function issueCard(i, opts = {}) {
  const resolved = resolvedSet().has(i.iid);
  const reqLinks = i.req_ids.slice(0, 8).map((rid, n) => [n ? ", " : "", h("a", { href: `#/job/${JOB.id}/requirements/${rid}`, text: rid })]);
  const quotes = i.contradiction && i.quotes.length >= 2 && !opts.compact
    ? h("div", { class: "vs" }, quoteEl(i.quotes[0]), h("span", { class: "x", "aria-label": "против", text: "≠" }), quoteEl(i.quotes[1]))
    : i.quotes.map(quoteEl);
  return h("article", { class: `card issue${resolved ? " resolved" : ""}`, id: `issue-${i.iid}` },
    h("div", { class: "issue-head" }, h("span", { class: "issue-id", text: i.iid }), h("span", { class: "issue-title", text: i.title }), sevBadge(i.severity)),
    h("div", { class: "issue-meta" },
      h("span", { class: "tag", title: critRef(i.criterion), text: `${critName(i.criterion)} · ${critRef(i.criterion)}` }),
      sourceTag(i.source), i.scope === "set" ? h("span", { class: "tag", text: "Весь набор" }) : null,
      i.contradiction ? h("span", { class: "tag sev-critical", text: "Противоречие" }) : null,
      resolved ? h("span", { class: "badge sev-ok", text: "Закрыт ответом автора" }) : null),
    h("p", { text: i.explanation }),
    quotes,
    i.suggestion ? h("div", { class: "fix" }, h("b", { text: "Как исправить: " }), i.suggestion) : null,
    !opts.compact && i.question ? h("div", { class: "ask" }, h("b", { text: "Вопрос автору: " }), i.question) : null,
    !opts.compact && i.req_ids.length ? h("div", { class: "small muted", style: "margin-top:8px" }, "Требования: ", reqLinks, i.req_ids.length > 8 ? ` и ещё ${i.req_ids.length - 8}` : "") : null);
}
function quoteEl(q) {
  return h("div", { class: "quote" }, h("q", { text: q.text }),
    q.line ? h("a", { href: `#/job/${JOB.id}/document/${q.line}`, title: "Показать в документе", text: `стр. ${q.line}` }) : h("span"));
}

// ---- Дефекты
function viewIssues(root, arg) {
  const R = JOB.report;
  const list = h("div");
  const search = h("input", { type: "search", placeholder: "Поиск по дефектам…", value: state.q, "aria-label": "Поиск дефектов" });
  const critSel = h("select", { "aria-label": "Критерий" }, h("option", { value: "", text: "Все критерии" }),
    Object.keys(R.stats.issues_by_criterion).map((k) => h("option", { value: k, text: critName(k), selected: state.crit === k })));
  const srcSel = h("select", { "aria-label": "Источник" }, [["", "Все источники"], ["rule", "Правила"], ["llm", "LLM"], ["rule+llm", "Правило + LLM"]]
    .map(([v, t]) => h("option", { value: v, text: t, selected: state.src === v })));
  const sevBtns = ["critical", "major", "minor"].map((sv) => h("button", { class: "chip", "aria-pressed": String(state.sev.has(sv)),
    onclick: (e) => { state.sev.has(sv) ? state.sev.delete(sv) : state.sev.add(sv); e.currentTarget.setAttribute("aria-pressed", String(state.sev.has(sv))); draw(); } },
    `${SEV[sv]} · ${R.stats.issues_by_severity[sv]}`));
  function draw() {
    state.q = search.value.trim().toLowerCase(); state.crit = critSel.value; state.src = srcSel.value;
    const items = R.issues.filter((i) => (!state.sev.size || state.sev.has(i.severity)) && (!state.crit || i.criterion === state.crit)
      && (!state.src || i.source === state.src)
      && (!state.q || `${i.iid} ${i.title} ${i.explanation} ${i.quotes.map((q) => q.text).join(" ")}`.toLowerCase().includes(state.q)));
    list.replaceChildren(...(items.length ? items.map((i) => issueCard(i)) : [h("div", { class: "card empty", text: "Под фильтр ничего не попало." })]));
  }
  [search, critSel, srcSel].forEach((el) => el.addEventListener(el === search ? "input" : "change", draw));
  draw();
  root.append(h("div", { class: "filters" }, sevBtns, critSel, srcSel, search,
    h("button", { class: "btn btn-ghost btn-sm", onclick: () => { state.sev.clear(); state.crit = ""; state.src = ""; state.q = ""; renderReport("issues"); } }, "Сбросить")), list);
  if (arg) setTimeout(() => { const el = document.getElementById(`issue-${arg}`); if (el) { el.scrollIntoView({ block: "start" }); el.style.outline = "2px solid var(--focus)"; } }, 50);
}

// ---- Противоречия
function viewContradictions(root) {
  const items = JOB.report.issues.filter((i) => i.contradiction);
  root.append(h("p", { class: "muted", style: "margin:0 0 14px", text: "Требования, части документа или контракт API, которые нельзя выполнить одновременно, а также несогласованные типы, единицы и коды. Каждая сторона противоречия подтверждена цитатой с номером строки." }));
  root.append(...(items.length ? items.map((i) => issueCard(i))
    : [h("div", { class: "card empty", text: JOB.report.run.mode.startsWith("LLM") ? "Противоречий не найдено." : "Противоречий правилами не найдено. Смысловые противоречия ищет LLM — запустите проверку с подключённой моделью." })]));
}

// ---- Вопросы
function viewQuestions(root) {
  const R = JOB.report;
  if (!R.questions.length) { root.append(h("div", { class: "card empty", text: "Вопросов к автору нет." })); return; }
  const areas = {};
  const items = R.questions.map((q) => {
    const ta = h("textarea", { "aria-label": `Ответ на ${q.qid}`, placeholder: "Ответ автора (можно оставить пустым)" });
    ta.value = q.answer || "";
    areas[q.qid] = ta;
    return h("article", { class: "card question" },
      h("div", { class: "issue-head" }, h("span", { class: "issue-id", text: q.qid }), h("span", { class: "issue-title", text: q.text }),
        q.blocking ? h("span", { class: "badge sev-critical", text: "Блокирующий" }) : null),
      h("div", { class: "issue-meta" }, h("span", { class: "tag", text: `Кому: ${q.addressee}` }),
        ...q.issue_ids.map((id) => h("a", { class: "tag", href: `#/job/${JOB.id}/issues/${id}`, text: id }))),
      q.why ? h("p", { class: "small muted", text: q.why }) : null,
      q.options.length ? h("div", { class: "opts-row" }, q.options.map((o) => h("button", { class: "chip", onclick: () => { ta.value = o; } }, o))) : null,
      ta);
  });
  const applying = JOB.status === "applying";
  const btn = h("button", { class: "btn", disabled: applying, onclick: async () => {
    btn.disabled = true; btn.textContent = "Учитываю ответы…";
    const answers = Object.fromEntries(Object.entries(areas).map(([k, ta]) => [k, ta.value]));
    try {
      await api(`/api/jobs/${JOB.id}/answers`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ answers }) });
      toast("Ответы отправлены. Агент пересчитает дефекты и заключение.");
      showJob(JOB.id, "questions");
    } catch (e) { btn.disabled = false; btn.textContent = "Учесть ответы"; toast(e.message); }
  } }, applying ? "Учитываю ответы…" : "Учесть ответы");
  const res = R.resolutions.length ? h("div", { class: "card", style: "margin-bottom:14px" },
    h("div", { class: "card-head" }, h("h2", { text: "Как учтены ответы" })),
    h("div", { class: "card-body stack" }, R.resolutions.map((r) => h("div", {},
      h("b", { text: `${r.qid}: ` }), r.answer,
      r.resolved_issue_ids.length ? h("div", { class: "small", text: `Закрыто: ${r.resolved_issue_ids.join(", ")}` }) : null,
      r.proposed_text ? h("div", { class: "fix" }, h("b", { text: `Новая формулировка${r.req_id ? " " + r.req_id : ""}: ` }), r.proposed_text) : null,
      r.comment ? h("div", { class: "small muted", text: r.comment }) : null)))) : null;
  root.append(
    h("div", { class: "alert alert-info", style: "margin-bottom:14px" },
      "Ответьте на вопросы от имени автора документа — агент отметит закрытые дефекты, предложит новые формулировки и пересчитает заключение. Пустые ответы пропускаются."),
    res, ...items, h("div", { class: "row", style: "margin-top:16px" }, btn,
      h("span", { class: "small muted", text: `${R.stats.questions_blocking} из ${R.questions.length} — блокирующие` })));
}

// ---- Документ
function viewDocument(root, arg) {
  const R = JOB.report, lines = JOB.lines || [];
  const res = resolvedSet();
  const byLine = {};
  for (const i of R.issues) {
    if (res.has(i.iid)) continue;
    for (const q of i.quotes) if (q.line) (byLine[q.line] = byLine[q.line] || []).push(i);
  }
  const side = h("div", { class: "card sticky" }, h("div", { class: "empty", text: "Выделенные строки содержат цитаты дефектов. Нажмите на строку, чтобы увидеть замечания." }));
  const target = Number(arg) || 0;
  function showLine(n) {
    const iss = (byLine[n] || []).filter((v, k, a) => a.indexOf(v) === k);
    side.replaceChildren(h("div", { class: "card-head" }, h("h2", { text: `Строка ${n}` }), h("span", { class: "small muted", text: plural(iss.length, "дефект", "дефекта", "дефектов") })),
      h("div", { class: "card-body" }, iss.length ? iss.map((i) => issueCard(i, { compact: true })) : h("p", { class: "muted", text: "На этой строке замечаний нет." })));
  }
  const doc = h("div", { class: "doc" }, lines.map((t, k) => {
    const n = k + 1;
    const iss = byLine[n];
    const sev = iss ? iss.map((i) => i.severity).sort((a, b) => SEV_ORDER[a] - SEV_ORDER[b])[0] : "";
    return h("div", { class: `ln ${sev}${n === target ? " target" : ""}`, id: `L${n}`,
      onclick: iss ? () => { history.replaceState(null, "", `#/job/${JOB.id}/document/${n}`); showLine(n); } : null,
      title: iss ? `${plural(iss.length, "дефект", "дефекта", "дефектов")}: ${iss.map((i) => i.iid).join(", ")}` : null },
      h("span", { class: "no", text: n }), h("span", { class: "tx", text: t || " " }));
  }));
  const wrap = h("div", { class: "card doc-wrap" }, doc);
  root.append(h("div", { class: "split" }, wrap, side));
  if (target) {
    showLine(target);
    setTimeout(() => { const el = document.getElementById(`L${target}`); if (el) wrap.scrollTop = el.offsetTop - wrap.clientHeight / 3; }, 30);
  }
}

// ---- Паспорт
function viewPassport(root) {
  const R = JOB.report, run = R.run;
  const rows = [
    ["Документ", `${run.doc_name} (${R.document.name}, ${R.document.fmt})`], ["Дата и время", run.started_at],
    ["Модель", run.model], ["Версия агента", run.agent_version], ["Режим", run.mode],
    ["Вызовов LLM", run.llm_calls], ["Длительность", `${run.duration_sec} с`],
    ["Отброшено без подтверждённой цитаты", run.dropped_unverified], ["Слито дублей", run.merged_duplicates],
    ["Отклонено критиком", run.rejected_by_critic], ["Стандарты", "ISO/IEC/IEEE 29148:2018 п. 5.2.5–5.2.6; IEEE Std 830-1998 п. 4.3"],
  ];
  root.append(h("div", { class: "grid" },
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Паспорт прогона" })),
      h("div", { class: "card-body" }, h("dl", { class: "kv" }, rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: String(v) })])))),
    run.llm_errors.length ? h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Шаги LLM, которые не выполнены" })),
      h("div", { class: "card-body" }, h("ul", {}, run.llm_errors.map((e) => h("li", { text: e }))))) : null,
    R.rejected.length ? h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: `Отклонено критиком (${R.rejected.length})` })),
      h("div", { class: "card-body" }, h("ul", {}, R.rejected.map((x) => h("li", {}, h("b", { text: x.issue.title }), " — ", x.reason))))) : null,
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Журнал анализа" })),
      h("div", { class: "card-body" }, h("div", { class: "log" }, JOB.log.map((l) => h("div", { text: `[${l.t.toFixed(1).padStart(5)} с] ${l.msg}` })))))));
}

init();
