// Sev One front end. Plain JavaScript, no build step.
// Left side: the on-call engineer's view. Right side: the operator console, kept live with server-sent events.

const app = {
  boot: null,
  persona: "qa-dana",
  stream: null,
  serving: null,          // last "serving now" state for the selected persona
  operator: null,         // operator bootstrap (null until the console is unlocked)
  engineerTurns: {},      // persona key -> last answer the engineer received
  operatorTurns: {},      // persona key -> last full turn from the operator channel
  lastQuestion: {},       // persona key -> last question
  incidents: {},          // persona key -> {incident, steps}
  connectionSimulated: "valid",
};

const $ = (id) => document.getElementById(id);

const CHIPS = [
  "What should I do first?",
  "Was it the last deploy?",
  "Should I fail over the database?",
  "Can I flush the cache?",
]

const REASONS = {
  released: "Sent to the engineer",
  feature_off: "AI guidance is off for this engineer. Standard message sent.",
  blocked_by_checks: "Blocked by the code checks. Standard message sent.",
  blocked_by_judge: "Blocked by the judge. Standard message sent.",
  judge_unavailable: "Blocked: the judge gave no score. Standard message sent.",
  ai_error: "Blocked: the AI call failed. Standard message sent.",
  ai_timeout: "Blocked: the AI call timed out. Standard message sent.",
  rolled_back_during_answer: "Held back: rolled back while answering. Standard message sent.",
  tool_failure: "Blocked: an incident lookup failed or never ran. Standard message sent.",
};
const BLOCKED = new Set(["blocked_by_checks", "blocked_by_judge", "judge_unavailable", "ai_error", "ai_timeout", "rolled_back_during_answer", "tool_failure"]);

// ------------------------------------------------------------------ helpers

async function api(path, options = {}) {
  const response = await fetch(path, {
    // X-Sev-One marks requests from this page. A form posted from another site can't add it.
    headers: { "Content-Type": "application/json", "X-Sev-One": "1" },
    ...options,
  });
  let body = null;
  try { body = await response.json(); } catch { /* empty body */ }
  if (!response.ok) {
    const detail = body && body.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : response.statusText;
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return body;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clock(seconds) {
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

// "Tonight 8:40 PM" -> "tonight 8:40 PM"
function dayLower(display) {
  const [day, ...rest] = display.split(" ");
  return [day.toLowerCase(), ...rest].join(" ");
}

function flash(node) {
  node.classList.remove("changed");
  void node.offsetWidth; // restart the animation
  node.classList.add("changed");
}

function setText(id, text, cls) {
  const node = $(id);
  const changed = node.textContent !== text;
  node.textContent = text;
  if (cls !== undefined) node.className = cls;
  return changed;
}

function persona(key = app.persona) {
  return app.boot.personas.find((p) => p.key === key);
}

// ------------------------------------------------------------------ start up

async function init() {
  app.boot = await api("/api/bootstrap");
  const modes = app.boot.modes;
  $("mode-ld").textContent = modes.launchdarkly === "live" ? "Live account" : "Local test flags";
  $("mode-llm").textContent = modes.llm === "openai" ? "OpenAI" : "Mock model";

  renderPersonaButtons();
  renderChips();

  $("ask").addEventListener("submit", (event) => {
    event.preventDefault();
    ask($("question").value);
  });
  $("ask-again").addEventListener("click", () => ask(app.lastQuestion[app.persona] || $("question").value));
  $("rollback").addEventListener("click", () => trigger("rollback"));
  $("restore").addEventListener("click", () => trigger("restore"));
  $("fixture").addEventListener("click", armFixture);
  $("operator-signin").addEventListener("submit", signIn);

  await selectPersona(app.persona);
  await initOperator();
}

// The operator console is a separate audience. Its data comes from /api/operator/*, which is
// open from this computer by default, or with OPERATOR_TOKEN when set.
async function initOperator() {
  try {
    app.operator = await api("/api/operator/bootstrap");
  } catch (error) {
    app.operator = null;
    $("operator-body").hidden = true;
    $("operator-locked").hidden = false;
    $("operator-locked-text").textContent = error.message;
    $("operator-signin").hidden = error.status !== 401;
    setText("mode-stream", "operator only", "stream-pending");
    return;
  }
  $("operator-locked").hidden = true;
  $("operator-body").hidden = false;
  const op = app.operator;
  $("flag-key").textContent = op.keys.flag;
  renderIncidents(op.operator);
  if (op.local) renderLocal(op.local);
  if (!op.trigger.off_configured) {
    $("rollback").disabled = true;
    $("rollback-note").textContent = "No trigger URL set. Add LD_TRIGGER_OFF_URL to .env to enable rollback from here.";
    $("rollback-note").classList.add("error");
  }
  if (!op.trigger.on_configured) {
    $("restore").disabled = true;
    $("restore").title = "Set LD_TRIGGER_ON_URL, or turn targeting back on in LaunchDarkly.";
  }
  connectStream(app.persona);
  const last = app.operatorTurns[app.persona];
  if (last) renderOperatorTurn(last);
}

async function signIn(event) {
  event.preventDefault();
  try {
    await api("/api/operator/session", { method: "POST", body: JSON.stringify({ token: $("operator-token").value }) });
    $("operator-token").value = "";
    await initOperator();
  } catch (error) {
    $("operator-locked-text").textContent = error.message;
  }
}

function renderPersonaButtons() {
  const wrap = $("persona-buttons");
  wrap.replaceChildren(
    ...app.boot.personas.map((p) =>
      el("button", {
        type: "button",
        class: "persona",
        "aria-pressed": String(p.key === app.persona),
        "data-key": p.key,
        title: p.blurb,
        onclick: () => selectPersona(p.key),
      }, el("strong", { text: p.name }), el("span", { text: p.label }), el("span", { class: "ctx", text: `key ${p.key}, service_tier ${p.service_tier}` })),
    ),
  );
}

function renderChips() {
  $("chips").replaceChildren(
    ...CHIPS.map((q) => el("button", { type: "button", class: "chip", onclick: () => { $("question").value = q; ask(q); } }, q)),
  );
}

async function selectPersona(key) {
  app.persona = key;
  for (const button of document.querySelectorAll(".persona")) {
    button.setAttribute("aria-pressed", String(button.dataset.key === key));
  }
  const p = persona();
  $("serving-name").textContent = p.name;
  $("last-name").textContent = p.name;

  if (!app.incidents[key]) app.incidents[key] = await api(`/api/incident?persona=${encodeURIComponent(key)}`);
  renderIncident(app.incidents[key]);

  const said = app.engineerTurns[key];
  if (said) renderEngineerTurn(said);
  else $("answer").hidden = true;

  if (app.operator) {
    const last = app.operatorTurns[key];
    if (last) renderOperatorTurn(last);
    else {
      $("last-empty").hidden = false;
      $("last-body").hidden = true;
    }
    connectStream(key);
  }
}

// ------------------------------------------------------------------ live updates

function connectStream(key) {
  if (app.stream) app.stream.close();
  setText("mode-stream", "connecting", "stream-pending");
  const stream = new EventSource(`/api/operator/events?persona=${encodeURIComponent(key)}`);
  app.stream = stream;

  stream.onopen = () => setText("mode-stream", "connected", "stream-ok");
  stream.onerror = () => setText("mode-stream", "reconnecting", "stream-down");

  stream.addEventListener("snapshot", (event) => {
    const data = JSON.parse(event.data);
    app.serving = null;
    renderServing(data.state);
    $("timeline").replaceChildren();
    if (!data.timeline.length) $("timeline").append(el("li", { class: "empty", text: "Waiting for changes." }));
    for (const entry of data.timeline) addTimeline(entry);
  });
  stream.addEventListener("state", (event) => renderServing(JSON.parse(event.data)));
  stream.addEventListener("timeline", (event) => addTimeline(JSON.parse(event.data)));
  stream.addEventListener("operator", (event) => renderIncidents(JSON.parse(event.data)));
  stream.addEventListener("turn", (event) => {
    const turn = JSON.parse(event.data);
    app.operatorTurns[turn.persona.key] = turn;
    if (turn.persona.key === app.persona) renderOperatorTurn(turn);
  });
}

function renderServing(state) {
  if (state.persona_key !== app.persona) return;
  const before = app.serving;
  app.serving = state;
  const animate = before !== null;

  const pill = $("feature-pill");
  const pillText = state.feature_on ? "On" : "Off, standard message";
  if (setText("feature-pill", pillText, `pill ${state.feature_on ? "on" : "off"}`) && animate) flash(pill.parentElement);

  const cfg = state.config_available || {};
  const variation = cfg.enabled ? `${cfg.variation_key} (version ${cfg.version})` : "Config off or not reachable";
  if (setText("avail-variation", variation, cfg.enabled ? "value-sel" : "value-caution") && animate) flash($("avail-variation"));
  if (setText("avail-model", cfg.model || "None") && animate) flash($("avail-model"));
  const tools = cfg.tools && cfg.tools.length ? cfg.tools.join(", ") : "None attached";
  setText("avail-tools", tools, cfg.tools && cfg.tools.length ? "" : "value-caution");

  const c = state.connection || {};
  let connection;
  let cls = "value-ok";
  if (!c.initialized) { connection = "Not initialized. Using app defaults."; cls = "value-warn"; }
  else if (c.state === "valid") connection = "Streaming";
  else if (c.state === "interrupted") { connection = "Interrupted. Serving cached flags."; cls = "value-caution"; }
  else { connection = c.state || "Unknown"; cls = "value-caution"; }
  if (setText("connection", connection, cls) && animate) flash($("connection"));
}

function addTimeline(entry) {
  const list = $("timeline");
  const placeholder = list.querySelector(".empty");
  if (placeholder) placeholder.remove();
  let text;
  let cls = "";
  switch (entry.kind) {
    case "flag_changed": text = `LaunchDarkly pushed a change to ${entry.flag_key}`; cls = "t-flag"; break;
    case "connection": text = `LaunchDarkly connection ${entry.state}${entry.error ? `: ${entry.error}` : ""}`; cls = entry.state === "valid" ? "t-ok" : "t-warn"; break;
    case "trigger_fired":
      text = entry.ok ? `Trigger sent: ${entry.action} (${entry.via})` : `Trigger failed: ${entry.error}`;
      cls = entry.ok ? "" : "t-warn"; break;
    case "answer_blocked": text = `Blocked an answer for ${entry.persona} (${entry.variation_key || "no variation"})`; cls = "t-warn"; break;
    case "answer_released": text = `Sent an AI answer to ${entry.persona} (${entry.variation_key}, version ${entry.version})`; cls = "t-ok"; break;
    case "fixture_armed": text = "Test fixture armed: the next AI answer is a known bad reply, not a model answer"; cls = "t-warn"; break;
    case "local_stage": text = `Local test change: ${entry.description}`; break;
    case "local_edit": text = `Local test change: edited ${entry.variation_key} instructions (now version ${entry.version})`; break;
    default: text = entry.kind;
  }
  list.prepend(el("li", {}, el("time", { text: clock(entry.at) }), el("span", { class: cls, text })));
  while (list.children.length > 30) list.lastElementChild.remove();
}

function renderIncidents(snapshot) {
  const count = $("blocked-count");
  count.textContent = String(snapshot.blocked_count);
  count.className = snapshot.blocked_count ? "count hot" : "count";
  const list = $("incidents");
  if (!snapshot.incidents.length) {
    list.replaceChildren(el("li", { class: "empty", text: "None yet." }));
    return;
  }
  list.replaceChildren(
    ...snapshot.incidents.map((i) =>
      el("li", {},
        el("div", { class: "incident-head" }, `${clock(i.at)}  ${i.persona}, ${i.variation_key || "no variation"}: `, el("span", { class: "incident-why", text: REASONS[i.reason] ? REASONS[i.reason].split(".")[0] : i.reason })),
        el("div", { class: "incident-text", text: i.detail || "" }),
      ),
    ),
  );
}

// ------------------------------------------------------------------ engineer view

function renderIncident(view) {
  const i = view.incident;
  const card = $("incident");
  if (i) {
    card.className = `incident ${i.severity.toLowerCase()}`;
    $("incident-sev").textContent = i.severity;
    $("incident-id").textContent = i.incident_id;
    $("incident-where").textContent = `${i.service}, ${i.environment}`;
    $("incident-title").textContent = i.title;
    $("incident-alert").textContent = `Alert: ${i.alert}. Started ${i.started_display}.`;
    const m = i.metrics;
    const tile = (label, value, was, hot) =>
      el("div", {}, el("dt", { text: label }), el("dd", { class: hot ? "hot" : "" }, value, el("span", { class: "was", text: was })));
    $("incident-tiles").replaceChildren(
      tile("Error rate", `${m.error_rate_pct}%`, `normal ${m.baseline_error_rate_pct}%`, m.error_rate_pct > 5),
      tile("p95 latency", `${m.p95_latency_ms.toLocaleString("en-US")} ms`, `normal ${m.baseline_p95_latency_ms.toLocaleString("en-US")} ms`, m.p95_latency_ms > 1000),
      tile("Last deploy", i.last_deploy.version, `at ${i.last_deploy.at_display}`, false),
      tile("Previous", i.last_deploy.previous_version, "rollback target", false),
    );
  } else {
    card.className = "incident";
    $("incident-title").textContent = "Incident unavailable";
    $("incident-alert").textContent = "";
    $("incident-tiles").replaceChildren();
  }
  renderSteps(view.steps, null, !i);
}

// The steps come only from runbook data. If that lookup failed, say so plainly instead of
// showing an empty list (and never show steps from anywhere else).
function renderSteps(steps, recommendedId = null, unavailable = false) {
  const none = unavailable || !steps || !steps.length;
  $("steps-wrap").hidden = none;
  $("steps-unavailable").hidden = !none;
  if (none) return;
  $("steps").replaceChildren(...steps.map((s) => renderStep(s, s.step_id === recommendedId)));
}

function renderStep(s, recommended = false) {
  const copy = el("button", {
    type: "button",
    class: "copy",
    "aria-label": `Copy the command for step ${s.number}`,
    onclick: async () => {
      try { await navigator.clipboard.writeText(s.command); copy.textContent = "Copied"; }
      catch { copy.textContent = "Select and copy"; }
      setTimeout(() => { copy.textContent = "Copy"; }, 1500);
    },
  }, "Copy");
  return el("li", { class: `step${recommended ? " recommended" : ""}${s.needs_approval ? " gated" : ""}` },
    el("span", { class: "step-num", text: String(s.number) }),
    el("div", { class: "step-top" },
      el("span", { class: "step-title", text: s.title }),
      recommended ? el("span", { class: "tag tag-rec", text: "Recommended" }) : null,
      s.needs_approval ? el("span", { class: "tag tag-gate", text: "Needs approval" }) : null,
      el("span", { class: "tag tag-risk", text: `risk: ${s.risk}` }),
    ),
    el("div", { class: "cmd" }, el("code", { text: s.command }), copy),
    s.needs_approval ? el("p", { class: "step-gate-note", text: "Only the incident commander can approve this step." }) : null,
  );
}

async function ask(question) {
  question = (question || "").trim();
  if (!question) return;
  const key = app.persona;
  app.lastQuestion[key] = question;
  setAsking(true);
  $("answer").hidden = true;
  $("answer-wait").hidden = false;
  try {
    const result = await api("/api/chat", { method: "POST", body: JSON.stringify({ persona: key, question }) });
    app.engineerTurns[key] = result;
    if (key === app.persona) renderEngineerTurn(result);
  } catch (error) {
    $("answer-kind").textContent = "Something went wrong";
    $("answer-text").textContent = `The request failed: ${error.message}. Try again.`;
    $("answer").hidden = false;
  } finally {
    $("answer-wait").hidden = true;
    setAsking(false);
  }
}

function setAsking(busy) {
  $("ask-button").disabled = busy;
  $("ask-again").disabled = busy;
  for (const chip of document.querySelectorAll(".chip")) chip.disabled = busy;
}

// Engineer side: only the answer they are allowed to see.
function renderEngineerTurn(r) {
  const fromAI = r.answer.source === "ai";
  const box = $("answer");
  box.className = fromAI ? "answer" : "answer standard";
  $("answer-kind").textContent = fromAI ? "From the on-call assistant" : "Standard runbook message";
  // Commands in `backticks` render as code. Text only: nothing from the model becomes HTML.
  $("answer-text").replaceChildren(
    ...r.answer.text.split("`").map((part, i) => (i % 2 ? el("code", { text: part }) : document.createTextNode(part))),
  );
  box.hidden = false;
  renderSteps(r.steps, r.recommended_step_id, !r.incident);
}

// Operator side: everything, including what was blocked and why. Arrives on the operator channel.
function renderOperatorTurn(r) {
  const fromAI = r.answer.source === "ai";
  $("last-empty").hidden = true;
  $("last-body").hidden = false;
  const reason = r.decision.reason;
  const decision = $("decision");
  decision.className = `decision ${fromAI ? "released" : BLOCKED.has(reason) ? "blocked" : "standard"}`;
  decision.replaceChildren(REASONS[reason] || reason);
  if (r.decision.detail && !fromAI) decision.append(el("span", { class: "decision-detail", text: r.decision.detail }));

  const used = r.config_used;
  const usedEl = $("used-variation");
  if (r.fixture) usedEl.replaceChildren("test-fixture", el("span", { class: "fixture-tag", text: "TEST FIXTURE, not a model answer" }));
  else usedEl.textContent = used ? `${used.variation_key} (version ${used.version})` : "Not called";
  $("used-model").textContent = used ? used.model : "Not called";
  const t = r.timing || {};
  const parts = [`${t.total_ms ?? 0} ms total`];
  if (t.agent_ms !== undefined) parts.push(`agent ${t.agent_ms} ms`);
  if (t.judge_ms !== undefined) parts.push(`judge ${t.judge_ms} ms`);
  $("used-time").textContent = parts.join(", ");
  $("used-tokens").textContent = r.usage ? `${r.usage.input} in, ${r.usage.output} out` : "None";

  const showBlocked = !fromAI && r.candidate_text && reason !== "feature_off";
  $("blocked-text").hidden = !showBlocked;
  $("blocked-quote").textContent = showBlocked ? r.candidate_text : "";

  const checks = $("checks");
  if (!r.checks.length) checks.replaceChildren(el("li", { class: "empty" }, "Not run (no AI answer to check)."));
  else {
    checks.replaceChildren(
      ...r.checks.map((c) =>
        el("li", {},
          markIcon(c.passed),
          el("span", { text: c.label }),
          el("span", { class: "detail", text: c.detail }),
        ),
      ),
    );
  }

  renderJudge(r.judge);

  const trace = $("trace");
  if (!r.tool_trace.length) trace.replaceChildren(el("li", { class: "empty" }, "No tool calls."));
  else {
    trace.replaceChildren(
      ...r.tool_trace.map((call) =>
        el("li", {},
          el("span", { class: "tool", text: `${call.tool}(${call.arguments.incident_id ?? ""})` }),
          el("span", { class: "ms", text: `${call.elapsed_ms} ms${call.attempts > 1 ? `, ${call.attempts} tries` : ""}` }),
          el("span", { class: `summary${call.ok ? "" : " err"}`, text: call.result_summary }),
        ),
      ),
    );
  }
}

function markIcon(passed) {
  const span = el("span", { class: `mark ${passed ? "mark-ok" : "mark-bad"}`, role: "img", "aria-label": passed ? "Passed" : "Failed" });
  span.innerHTML = passed
    ? '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    : '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>';
  return span;
}

function renderJudge(judge) {
  const wrap = $("judge");
  if (!judge.enabled) { wrap.replaceChildren(el("p", { class: "empty", text: "Judge is turned off (JUDGE_CONFIG_KEY=off)." })); return; }
  if (!judge.ran) { wrap.replaceChildren(el("p", { class: "empty", text: judge.reasoning || "Not run. The answer was decided before the judge step." })); return; }
  if (judge.error) { wrap.replaceChildren(el("p", { class: "value-warn", text: judge.error })); return; }
  const threshold = app.operator.judge_threshold;
  const color = judge.passed ? "var(--ok)" : "var(--warn)";
  wrap.replaceChildren(
    el("div", { class: "judge-score" },
      el("span", { class: "num", style: `color:${color}`, text: judge.score.toFixed(2) }),
      el("span", { text: judge.passed ? `passes (needs ${threshold.toFixed(2)})` : `below ${threshold.toFixed(2)}` }),
    ),
    el("div", { class: "meter", role: "img", "aria-label": `Score ${judge.score.toFixed(2)} out of 1, threshold ${threshold.toFixed(2)}` },
      el("span", { class: "meter-fill", style: `width:${Math.round(judge.score * 100)}%;background:${color}` }),
      el("span", { class: "meter-threshold", style: `left:${Math.round(threshold * 100)}%` }),
    ),
    el("p", { class: "judge-reason", text: judge.reasoning }),
  );
}

// ------------------------------------------------------------------ operator actions

async function armFixture() {
  const note = $("fixture-note");
  try {
    await api("/api/operator/fixture", { method: "POST" });
    note.textContent = `Armed at ${clock(Date.now() / 1000)}. Ask any question as an engineer whose AI guidance is on.`;
    note.classList.remove("error");
  } catch (error) {
    note.textContent = `Could not arm the fixture: ${error.message}`;
    note.classList.add("error");
  }
}

async function trigger(action) {
  const button = $(action === "rollback" ? "rollback" : "restore");
  const note = $("rollback-note");
  button.disabled = true;
  try {
    const outcome = await api(`/api/operator/${action}`, { method: "POST" });
    note.classList.toggle("error", !outcome.ok);
    note.textContent = outcome.ok
      ? `${action === "rollback" ? "Rollback" : "Restore"} sent at ${clock(Date.now() / 1000)} through ${outcome.via}. The console updates when LaunchDarkly pushes the change.`
      : outcome.error;
  } catch (error) {
    note.classList.add("error");
    note.textContent = `Request failed: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

// ------------------------------------------------------------------ local test mode

function renderLocal(local) {
  $("local-block").hidden = false;
  $("local-stage").textContent = `Current: ${local.stage_description}`;
  const stages = Object.entries(local.stages);
  $("stages").replaceChildren(
    ...stages.map(([key, description], i) =>
      el("li", {},
        el("button", {
          type: "button",
          class: "btn btn-ghost",
          "aria-pressed": String(local.stage === key),
          onclick: async () => renderLocal(await api("/api/local/stage", { method: "POST", body: JSON.stringify({ stage: key }) })),
        }, el("span", { class: "n", text: String(i + 1) }), el("span", {}, el("strong", { text: stageName(key) }), ` ${description}`)),
      ),
    ),
  );
  app.connectionSimulated = local.connection;
  $("toggle-connection").textContent = local.connection === "interrupted" ? "Restore connection" : "Simulate connection loss";
  $("toggle-connection").onclick = async () => {
    const next = app.connectionSimulated === "interrupted" ? "valid" : "interrupted";
    renderLocal(await api("/api/local/connection", { method: "POST", body: JSON.stringify({ state: next }) }));
  };
  $("edit-instructions").onclick = async () => {
    await api("/api/local/edit", { method: "POST", body: JSON.stringify({ variation_key: "candidate", sentence: "End every answer with: Post updates in #inc-checkout." }) });
  };
}

function stageName(key) {
  return { baseline: "Baseline.", "qa-preview": "QA preview.", "on-call": "On-call teams.", "fast-fix": "Fast-fix prompt." }[key] || key;
}

init().catch((error) => {
  document.body.prepend(el("p", { class: "value-warn", style: "padding:1rem", text: `Could not start: ${error.message}` }));
});
