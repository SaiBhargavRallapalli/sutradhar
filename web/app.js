/* ==========================================================================
   Sutradhar web UI controller
   --------------------------------------------------------------------------
   Two rules drive this file:

   1. **No innerHTML with data.** Every value that originates from the
      request, the catalogue or a model is written with textContent. That
      makes cross-site scripting structurally impossible rather than
      something we have to remember to escape in 40 places.
   2. **Announce everything.** Results, errors and copy confirmations all go
      through live regions, so the UI is usable without sight of it.
   ========================================================================== */
"use strict";

const API = "/api/plan";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Create an element with optional text and className. Never parses HTML. */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

const money = (minor, currency) => {
  const sym = { INR: "₹", USD: "$", EUR: "€", GBP: "£", SGD: "S$", AUD: "A$", JPY: "¥", AED: "AED " }[currency] || "";
  const negative = minor < 0;
  const abs = Math.abs(minor);
  const whole = Math.floor(abs / 100);
  const frac = String(abs % 100).padStart(2, "0");
  // Indian lakh/crore grouping for INR, thousands elsewhere.
  const s = currency === "INR" ? groupIndian(whole) : whole.toLocaleString();
  return `${negative ? "−" : ""}${sym}${s}.${frac}`;
};

function groupIndian(n) {
  const s = String(n);
  if (s.length <= 3) return s;
  const head = s.slice(0, -3);
  const tail = s.slice(-3);
  const grouped = head.replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  return `${grouped},${tail}`;
}

const shortDate = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso + (iso.length === 10 ? "T00:00:00" : ""));
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" });
};

/* ---------------------------------------------------------------- state -- */
let lastPlan = null;

/* ------------------------------------------------------------ validation -- */
function clearErrors() {
  $("#error-box").hidden = true;
  $("#error-list").replaceChildren();
  $$("[aria-invalid]").forEach((n) => n.removeAttribute("aria-invalid"));
  $$(".field-error").forEach((n) => n.remove());
}

function showError(messages) {
  const list = $("#error-list");
  list.replaceChildren();
  (Array.isArray(messages) ? messages : [messages]).forEach((m) => list.appendChild(el("li", null, m)));
  $("#error-box").hidden = false;
  // Move focus to the alert so keyboard and screen-reader users land on it.
  $("#error-box").focus?.();
  $("#error-box").scrollIntoView({ block: "nearest", behavior: prefersReducedMotion() ? "auto" : "smooth" });
}

function markInvalid(input, message) {
  input.setAttribute("aria-invalid", "true");
  const id = `${input.id}-err`;
  input.setAttribute("aria-describedby", id);
  const note = el("span", "field-error", message);
  note.id = id;
  input.closest(".field").appendChild(note);
}

function validate(form) {
  clearErrors();
  const problems = [];
  const date = $("#event_date").value;
  const guests = Number($("#guest_count").value);
  const budget = $("#budget").value.trim();

  if (!date) { markInvalid($("#event_date"), "Pick a date."); problems.push("A date is required."); }
  else if (new Date(date + "T00:00:00") < startOfToday()) {
    markInvalid($("#event_date"), "That date has already passed.");
    problems.push("The event date must be in the future.");
  }
  if (!Number.isFinite(guests) || guests < 1) {
    markInvalid($("#guest_count"), "Enter at least 1 guest.");
    problems.push("Guest count must be 1 or more.");
  }
  if (!budget || !parseMoney(budget)) {
    markInvalid($("#budget"), "Enter a total budget, e.g. 800000.");
    problems.push("A total budget is required.");
  }
  return problems;
}

function startOfToday() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
}

/** Parse "15 lakh", "2,50,000", "$3000", "800000" into minor units. */
function parseMoney(raw) {
  if (!raw) return null;
  let text = String(raw).toLowerCase().replace(/[₹$€£,\s]/g, "");
  const multipliers = {
    lakh: 1e5, lac: 1e5, lakhs: 1e5, l: 1e5,
    crore: 1e7, cr: 1e7, crores: 1e7,
    k: 1e3, thousand: 1e3, m: 1e6, million: 1e6, bn: 1e9, b: 1e9,
  };
  let value = 1;
  for (const [suffix, mult] of Object.entries(multipliers)) {
    if (text.endsWith(suffix)) {
      value = mult;
      text = text.slice(0, -suffix.length);
      break;
    }
  }
  const num = Number(text);
  if (!Number.isFinite(num) || num <= 0) return null;
  return Math.round(num * value * 100);
}

/* --------------------------------------------------------------- collect -- */
function collectBrief() {
  const priorities = {};
  $$(".priority").forEach((sel) => { priorities[sel.dataset.cat] = sel.value; });

  const split = (id) => $(id).value.split(",").map((s) => s.trim()).filter(Boolean);

  return {
    event_type: $("#event_type").value,
    title: $("#title").value.trim(),
    event_date: $("#event_date").value,
    guest_count: Number($("#guest_count").value),
    budget_total_minor: parseMoney($("#budget").value),
    currency: $("#currency").value,
    location: $("#location").value.trim(),
    city_tier: $("#city_tier").value,
    setting: $("#setting").value,
    priorities,
    taste: {
      vibe: $("#vibe").value.trim(),
      dietary: split("#dietary"),
      accessibility_notes: split("#accessibility"),
      avoid: split("#avoid"),
    },
  };
}

/* ----------------------------------------------------------------- plan -- */
async function makePlan(brief) {
  const btn = $("#plan-btn");
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  clearErrors();

  try {
    const res = await fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(brief),
    });

    let payload = null;
    try { payload = await res.json(); } catch { /* non-JSON error body */ }

    if (!res.ok || !payload || payload.ok !== true) {
      const msg = payload && payload.error ? payload.error : `The server returned ${res.status}.`;
      const hint = payload && payload.hint ? ` ${payload.hint}` : "";
      showError([msg + hint]);
      return;
    }
    render(payload.plan);
  } catch (err) {
    showError([`Could not reach the planner: ${err.message}. Is the server still running?`]);
  } finally {
    btn.disabled = false;
    btn.removeAttribute("aria-busy");
  }
}

/* --------------------------------------------------------------- render -- */
function render(plan) {
  lastPlan = plan;
  const currency = plan.brief.currency;
  const results = $("#results");
  results.hidden = false;

  renderSummary(plan, currency);
  renderToc();
  renderBudget(plan, currency);
  renderTimeline(plan, currency);
  renderVendors(plan, currency);
  renderRisks(plan);
  renderIdeas(plan, currency);
  renderWarnings(plan);

  $("#raw-json").textContent = JSON.stringify(plan, null, 2);

  // Move focus to the results heading: a keyboard user's next Tab starts
  // inside the plan rather than back at the top of the form.
  const heading = $("#results-heading");
  heading.focus();
  heading.scrollIntoView({ block: "start", behavior: prefersReducedMotion() ? "auto" : "smooth" });
}

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function renderSummary(plan, currency) {
  const b = plan.brief;
  const rows = [
    ["Event", `${b.event_type} — ${b.display_title || b.title || "Untitled"}`],
    ["Date", shortDate(b.event_date)],
    ["Guests", b.guest_count.toLocaleString()],
    ["Location", b.location || "to be decided"],
    ["Total budget", money(b.budget_total_minor, currency)],
    ["Per guest", money(plan.budget.per_guest_minor, currency)],
    ["Contingency", `${money(plan.budget.contingency_minor, currency)} (${plan.budget.contingency_pct}%)`],
    ["Planning window", `${plan.timeline.lead_days} days`],
    ["Critical path", `${plan.timeline.min_required_lead_days} days that cannot slip`],
    ["Source of ideas", plan.ai_used ? `model (${plan.ai_provider})` : "built-in catalogue"],
  ];
  const dl = $("#summary");
  dl.replaceChildren();
  rows.forEach(([term, value]) => {
    const box = el("div");
    box.appendChild(el("dt", null, term));
    box.appendChild(el("dd", null, value));
    dl.appendChild(box);
  });
}

function renderToc() {
  const ul = $("#toc-list");
  ul.replaceChildren();
  $$("main#main section[aria-labelledby] h3").forEach((h) => {
    if (!h.closest("#results")) return;
    if (!h.id) h.id = `h-${h.textContent.toLowerCase().replace(/\W+/g, "-")}`;
    const li = el("li");
    const a = el("a", null, h.textContent);
    a.href = `#${h.id}`;
    li.appendChild(a);
    ul.appendChild(li);
  });
}

function renderBudget(plan, currency) {
  const budget = plan.budget;
  $("#budget-explainer").textContent =
    `Every rupee is assigned: ${money(budget.spendable_minor, currency)} spendable, ` +
    `${money(budget.contingency_minor, currency)} held back as float, and ` +
    `${money(budget.unallocated_minor, currency)} left unallocated (this is always zero by construction).`;

  const tbody = $("#budget-table tbody");
  tbody.replaceChildren();
  const lines = [...budget.lines].sort((a, b) => b.amount_minor - a.amount_minor);
  const max = lines.length ? lines[0].amount_minor : 1;

  lines.forEach((line) => {
    const tr = el("tr");

    const th = el("th", null, line.label);
    th.scope = "row";
    if (!line.in_band) {
      const flag = el("span", "flag warn", "outside benchmark");
      th.appendChild(flag);
    }
    tr.appendChild(th);

    const shareCell = el("td", null, `${line.pct.toFixed(1)}%`);
    const bar = el("span", "bar");
    const fill = el("span");
    fill.style.width = `${Math.max(2, (line.amount_minor / max) * 100)}%`;
    bar.appendChild(fill);
    shareCell.appendChild(bar);
    tr.appendChild(shareCell);

    tr.appendChild(el("td", "num", money(line.amount_minor, currency)));
    tr.appendChild(el("td", "num", money(line.per_guest_minor, currency)));
    tbody.appendChild(tr);
  });

  const foot = el("tr");
  const fth = el("th", null, "Spendable"); fth.scope = "row";
  foot.appendChild(fth);
  foot.appendChild(el("td", null, ""));
  foot.appendChild(el("td", "num", money(budget.spendable_minor, currency)));
  foot.appendChild(el("td", "num", money(budget.per_guest_minor, currency)));
  tbody.appendChild(foot);

  const box = $("#tradeoffs");
  box.replaceChildren();
  if (budget.trade_offs && budget.trade_offs.length) {
    const h = el("h4", null, "If you need to free up cash");
    box.appendChild(h);
    const ul = el("ul");
    budget.trade_offs.slice(0, 4).forEach((t) => {
      const li = el("li");
      const strong = el("strong", null, `Save ${money(t.saving_minor, currency)} — `);
      li.appendChild(strong);
      li.appendChild(document.createTextNode(`${t.action} ${t.category_label} `));
      li.appendChild(el("em", null, `(impact: ${t.impact})`));
      if (t.consequence) li.appendChild(el("p", "hint", t.consequence));
      ul.appendChild(li);
    });
    box.appendChild(ul);
  }
}

function renderTimeline(plan) {
  const tl = plan.timeline;
  const feas = $("#feasibility");
  if (tl.feasible) {
    feas.className = "callout good";
    feas.textContent =
      `Feasible. You have ${tl.lead_days} days, and the critical path needs ` +
      `${tl.min_required_lead_days}. Everything below the chain has slack.`;
  } else {
    feas.className = "callout bad";
    feas.textContent =
      `Not feasible as dated. The critical path needs ${tl.min_required_lead_days} days ` +
      `but only ${tl.lead_days} remain. Move the date later, or cut scope on the chain below.`;
  }

  const chain = $("#critical-chain");
  chain.replaceChildren();
  (tl.critical_path || []).forEach((taskId) => {
    const task = (tl.tasks || []).find((t) => t.id === taskId);
    chain.appendChild(el("li", null, task ? task.title : taskId));
  });

  const tbody = $("#task-table tbody");
  tbody.replaceChildren();
  [...(tl.tasks || [])].sort((a, b) => a.due.localeCompare(b.due)).forEach((task) => {
    const tr = el("tr");
    tr.appendChild(el("td", null, shortDate(task.due)));
    const nameCell = el("td");
    nameCell.appendChild(document.createTextNode(task.title));
    if (task.critical) nameCell.appendChild(el("span", "flag", "critical"));
    tr.appendChild(nameCell);
    tr.appendChild(el("td", null, task.owner));
    tr.appendChild(el("td", "num", task.slack_days === 0 ? "0 — cannot slip" : `${task.slack_days} d`));
    tbody.appendChild(tr);
  });

  const ros = $("#run-of-show");
  ros.replaceChildren();
  (tl.run_of_show || []).forEach((slot) => {
    const li = el("li");
    const day = slot.day_offset === 0 ? "" : ` (day ${slot.day_offset > 0 ? "+" : ""}${slot.day_offset})`;
    li.appendChild(el("span", "t", `${slot.start}–${slot.end}${day}`));
    li.appendChild(document.createTextNode(`  ${slot.title}`));
    ros.appendChild(li);
  });
  $("#ros-heading").hidden = !(tl.run_of_show || []).length;
}

function renderVendors(plan, currency) {
  const ul = $("#vendor-list");
  ul.replaceChildren();
  [...(plan.vendors.items || [])].sort((a, b) => a.book_by.localeCompare(b.book_by)).forEach((v) => {
    const li = el("li");
    li.appendChild(el("h4", null, v.label));
    li.appendChild(el("p", "hint", `Book by ${shortDate(v.book_by)} · ${v.deposit_pct}% deposit · priority: ${v.priority}`));
    if (v.why) li.appendChild(el("p", null, v.why));
    if (v.contract_musts && v.contract_musts.length) {
      const dl = el("dl");
      dl.appendChild(el("dt", null, "Must be in the contract"));
      v.contract_musts.forEach((m) => dl.appendChild(el("dd", null, `• ${m}`)));
      li.appendChild(dl);
    }
    if (v.negotiation_tip) li.appendChild(el("p", "hint", `Tip: ${v.negotiation_tip}`));
    ul.appendChild(li);
  });

  const tbody = $("#payment-table tbody");
  tbody.replaceChildren();
  [...(plan.vendors.milestones || [])].sort((a, b) => a.due.localeCompare(b.due)).forEach((m) => {
    const tr = el("tr");
    tr.appendChild(el("td", null, shortDate(m.due)));
    tr.appendChild(el("td", null, m.label));
    tr.appendChild(el("td", "num", money(m.amount_minor, currency)));
    tbody.appendChild(tr);
  });
  $("#pay-heading").hidden = !(plan.vendors.milestones || []).length;
}

function renderRisks(plan) {
  const ul = $("#risk-list");
  ul.replaceChildren();
  (plan.risks.items || []).forEach((r) => {
    const li = el("li");
    li.appendChild(el("h4", null, `${r.title}  [${r.severity}]`));
    li.appendChild(el("p", "hint", `Likelihood ${r.likelihood}/5 × impact ${r.impact}/5 = score ${r.score}`));
    if (r.mitigation) li.appendChild(el("p", null, r.mitigation));
    if (r.trigger_sign) li.appendChild(el("p", "hint", `Watch for: ${r.trigger_sign}`));
    ul.appendChild(li);
  });
}

function renderIdeas(plan, currency) {
  const ul = $("#idea-list");
  ul.replaceChildren();
  (plan.ideas || []).forEach((idea) => {
    const li = el("li");
    li.appendChild(el("h4", null, idea.name));
    li.appendChild(el("p", "hint", `${idea.tagline}  [${idea.source}]`));
    const dl = el("dl");
    const add = (label, value) => { if (value) { dl.appendChild(el("dt", null, label)); dl.appendChild(el("dd", null, value)); } };
    add("Palette", (idea.palette || []).join(", "));
    add("Decor", idea.decor);
    add("Menu", idea.menu);
    add("Attire", idea.attire);
    add("Music", idea.music);
    add("Activities", idea.activities);
    add("Favors", idea.favors);
    li.appendChild(dl);
    const delta = idea.estimated_delta_pct || 0;
    li.appendChild(el("p", null, delta === 0
      ? "Cost: no change against the baseline plan."
      : `Cost: ${delta > 0 ? "+" : ""}${delta.toFixed(1)}% — ${idea.cost_fit || ""}`));
    if (idea.rationale) li.appendChild(el("p", "hint", `Why this fits you: ${idea.rationale}`));
    ul.appendChild(li);
  });
  $("#idea-heading").hidden = !(plan.ideas || []).length;
}

function renderWarnings(plan) {
  const ul = $("#warning-list");
  ul.replaceChildren();
  // `all_warnings` merges plan-, budget- and timeline-level warnings; the bare
  // `warnings` field only carries plan-level ones and would miss e.g. the
  // "categories starved" budget warning. Risks already have their own
  // section, so skip the mirrored risk warnings rather than repeating them.
  const source = plan.all_warnings || plan.warnings || [];
  const warnings = source.filter((w) => w.section !== "risks");
  warnings.forEach((w) => {
    const li = el("li");
    li.appendChild(document.createTextNode(w.message));
    if (w.suggestion) li.appendChild(el("p", "hint", w.suggestion));
    ul.appendChild(li);
  });
  $("#warn-heading").hidden = warnings.length === 0;
}

/* ----------------------------------------------------------------- wire -- */
async function copyJson() {
  if (!lastPlan) return;
  const text = JSON.stringify(lastPlan, null, 2);
  const status = $("#copy-status");
  try {
    await navigator.clipboard.writeText(text);
    status.textContent = "Copied to clipboard.";
  } catch {
    // Clipboard access is often denied; fall back to selecting the text.
    const pre = $("#raw-json");
    const range = document.createRange();
    range.selectNodeContents(pre);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    status.textContent = "Clipboard blocked — the JSON below is selected, press Ctrl/Cmd+C.";
  }
}

function defaultDate() {
  // Nine months out: enough runway that the sample plan is feasible.
  const d = startOfToday();
  d.setMonth(d.getMonth() + 9);
  $("#event_date").value = d.toISOString().slice(0, 10);
  $("#event_date").min = startOfToday().toISOString().slice(0, 10);
}

async function init() {
  defaultDate();

  $("#plan-btn").addEventListener("click", () => {
    const problems = validate($("#plan-form"));
    if (problems.length) { showError(problems); return; }
    makePlan(collectBrief());
  });

  $("#demo-btn").addEventListener("click", () => {
    $("#event_type").value = "wedding";
    $("#title").value = "Aarav & Meera";
    $("#guest_count").value = "250";
    $("#budget").value = "1500000";
    $("#location").value = "Pune";
    $("#city_tier").value = "tier_2";
    $("#setting").value = "open_air";
    $("#vibe").value = "Warm, close-knit, outdoors under old trees. Local food, no flashy decor.";
    $("#dietary").value = "vegetarian, jain options";
    $("#accessibility").value = "step-free access, seating for elders";
    $("#p_catering").value = "high";
    $("#p_photography").value = "high";
    clearErrors();
    makePlan(collectBrief());
  });

  $("#reset-btn").addEventListener("click", () => { setTimeout(defaultDate, 0); });
  $("#copy-json").addEventListener("click", copyJson);
  $("#print-btn").addEventListener("click", () => window.print());

  // Enter in a text field submits nothing, but Ctrl/Cmd+Enter plans.
  $("#plan-form").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      $("#plan-btn").click();
    }
  });

  // Report which mode the server is in so the user knows whether a model is live.
  try {
    const res = await fetch("/api/health");
    if (res.ok) {
      const info = await res.json();
      $("#mode-line").textContent = `Connected · server v${info.version} · planning locally`;
    }
  } catch {
    $("#mode-line").textContent = "Not connected to a planner server — the form will not work.";
  }
}

document.addEventListener("DOMContentLoaded", init);
