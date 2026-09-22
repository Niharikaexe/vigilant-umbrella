/* AgentFlow console.
   Plain ES modules-free JS on purpose: no build step, no toolchain, no lockfile
   to rot. The page is a thin view over the API -- every bit of logic that
   matters lives in the service, which is also what Copilot Studio, Power
   Automate and n8n call. */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { config: null, runId: null, run: null, stream: null, p1: 0 };

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const api = async (path, opts) => {
    const res = await fetch(path, opts);
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.status === 204 ? null : res.json();
  };

  /* ---------------- theme ---------------- */

  const initTheme = () => {
    let saved = null;
    try { saved = localStorage.getItem("agentflow-theme"); } catch { /* private mode */ }
    if (saved) document.documentElement.dataset.theme = saved;
    $("theme-toggle").addEventListener("click", () => {
      const dark = getComputedStyle(document.body).backgroundColor === "rgb(13, 13, 13)";
      const next = dark ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("agentflow-theme", next); } catch { /* ignore */ }
    });
  };

  /* ---------------- boot ---------------- */

  const boot = async () => {
    state.config = await api("/api/config");
    $("pipeline-name").textContent = state.config.pipeline.name;
    renderServiceChips();
    renderGraph();
    renderSamples();
    await refreshRuns();
    await refreshStats();
    openStream(null);
  };

  const renderServiceChips = () => {
    const chips = state.config.azure_services.map((s) =>
      `<span class="chip ${s.mode === "live" ? "live" : ""}" title="${esc(s.purpose)}">${esc(s.service.replace("Azure AI ", "").replace("Azure ", ""))}: ${s.mode}</span>`);
    chips.push(`<span class="chip" title="LLM provider">LLM: ${esc(state.config.llm_provider)}</span>`);
    $("service-chips").innerHTML = chips.join("");
  };

  const renderGraph = () => {
    $("graph").innerHTML = state.config.pipeline.nodes.map((n) => `
      <div class="node" data-node="${esc(n.id)}" data-status="pending" title="${esc(n.description)}">
        <div class="nl"><span class="dot"></span>${esc(n.label)}</div>
        ${n.azure_service ? `<span class="ns">${esc(n.azure_service)}</span>` : `<span class="ns">deterministic</span>`}
        <div class="nd" data-dur></div>
      </div>`).join("");
  };

  const renderSamples = () => {
    $("samples").innerHTML = state.config.samples.map((s, i) => `
      <button class="sample" data-sample="${i}">
        <b>${esc(s.name)}</b><i>${esc(s.expect)}</i>
      </button>`).join("");
    $("samples").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-sample]");
      if (!btn) return;
      const s = state.config.samples[Number(btn.dataset.sample)];
      $("signal-text").value = s.text;
      $("signal-kind").value = s.kind || "operator_request";
      $("signal-channel").value = s.channel || "dashboard";
      submit(s.readings || {});
    });
  };

  /* ---------------- running ---------------- */

  const resetGraph = () => {
    document.querySelectorAll(".node").forEach((n) => {
      n.dataset.status = "pending";
      n.querySelector("[data-dur]").textContent = "";
    });
    $("trace").innerHTML = "";
  };

  const submit = async (readings) => {
    const text = $("signal-text").value.trim();
    if (!text) { $("signal-text").focus(); return; }

    $("run-btn").disabled = true;
    resetGraph();
    $("result").innerHTML = "<h2>Result</h2><div class='empty'>Running…</div>";

    try {
      const accepted = await api("/api/signals", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text,
          kind: $("signal-kind").value,
          channel: $("signal-channel").value,
          readings: readings || {},
        }),
      });
      state.runId = accepted.run_id;
      openStream(accepted.run_id);
    } catch (err) {
      $("result").innerHTML = `<h2>Result</h2><div class="banner bad">Could not start the run: ${esc(err.message)}</div>`;
      $("run-btn").disabled = false;
    }
  };

  /* ---------------- live stream ---------------- */

  const openStream = (runId) => {
    if (state.stream) state.stream.close();
    const url = runId ? `/api/stream?run_id=${encodeURIComponent(runId)}` : "/api/stream";
    const es = new EventSource(url);
    state.stream = es;

    es.onmessage = (msg) => {
      let ev;
      try { ev = JSON.parse(msg.data); } catch { return; }
      if (state.runId && ev.run_id !== state.runId) return;
      applyEvent(ev);
    };
    // EventSource reconnects on its own; nothing to do but stop shouting.
    es.onerror = () => {};
  };

  const applyEvent = (ev) => {
    if (ev.node !== "run" && ev.node !== "log") {
      const node = document.querySelector(`[data-node="${CSS.escape(ev.node)}"]`);
      if (node) {
        node.dataset.status = ev.status;
        if (ev.duration_ms != null) node.querySelector("[data-dur]").textContent = `${ev.duration_ms} ms`;
      }
    }
    appendTrace(ev);

    const terminal = ev.node === "run" && ["ok", "failed", "waiting"].includes(ev.status);
    if (terminal) {
      $("run-btn").disabled = false;
      loadRun(ev.run_id);
      refreshRuns();
      refreshStats();
    }
  };

  const appendTrace = (ev) => {
    const trace = $("trace");
    if (trace.querySelector(".empty")) trace.innerHTML = "";
    const label = state.config.pipeline.nodes.find((n) => n.id === ev.node);
    const row = document.createElement("div");
    row.className = "trace-row";
    row.innerHTML =
      `<span class="t-st st-${esc(ev.status)}">${esc(ev.status)}</span>` +
      `<span class="t-nd">${esc(label ? label.label : ev.node)}</span>` +
      `<span class="t-ms">${esc(ev.message || summariseData(ev))}</span>` +
      `<span class="t-du">${ev.duration_ms != null ? ev.duration_ms + " ms" : ""}</span>`;
    trace.appendChild(row);
    trace.scrollTop = trace.scrollHeight;
  };

  /* One line of the most decision-relevant fields, so the trace tells the story
     without anyone opening a JSON viewer. */
  const summariseData = (ev) => {
    const d = ev.data || {};
    const bits = [];
    if (d.asset_id) bits.push(`${d.asset_id} @ ${Math.round((d.confidence ?? 0) * 100)}%`);
    if (d.redactions != null && d.redactions > 0) bits.push(`${d.redactions} PII redacted`);
    if (d.detected_language) bits.push(`lang=${d.detected_language}`);
    if (d.count != null && d.sections) bits.push(`${d.count} sections, top ${d.top_score}`);
    if (d.severity && d.confidence != null) bits.push(`${d.severity} @ ${Math.round(d.confidence * 100)}%`);
    if (d.provider) bits.push(d.provider);
    if (d.priority && d.sla_hours) bits.push(`${d.priority}, SLA ${d.sla_hours}h`);
    if (d.findings != null) bits.push(`${d.findings} findings, ${d.blockers || 0} blockers`);
    if (d.planned) bits.push(`${d.planned.length} actions`);
    if (d.rejected_not_allowed && d.rejected_not_allowed.length) bits.push(`${d.rejected_not_allowed.length} blocked by allow-list`);
    if (d.requires_approval != null) bits.push(d.requires_approval ? "human approval required" : "auto-dispatch");
    if (d.dispatched != null) bits.push(`${d.dispatched} dispatched, ${d.failed} failed`);
    return bits.join(" · ");
  };

  /* ---------------- results ---------------- */

  const loadRun = async (runId) => {
    state.runId = runId;
    try { state.run = await api(`/api/runs/${encodeURIComponent(runId)}`); }
    catch { return; }
    renderResult(state.run);
    document.querySelectorAll(".runrow").forEach((r) =>
      r.classList.toggle("active", r.dataset.run === runId));
  };

  const badge = (cls, text) => `<span class="badge ${esc(cls)}">${esc(text)}</span>`;

  const renderResult = (run) => {
    const d = run.diagnosis;
    const parts = ["<h2>Result</h2>"];

    if (run.status === "awaiting_approval") {
      parts.push(`<div class="banner hold"><b>Held for human approval.</b><br>${esc(run.approval_reason)}</div>`);
    } else if (run.status === "failed") {
      parts.push(`<div class="banner bad"><b>Run halted (fail closed).</b><br>${esc(run.error || "")}<br>Nothing was dispatched.</div>`);
    } else if (run.status === "rejected") {
      parts.push(`<div class="banner bad"><b>Plan rejected by ${esc(run.approved_by)}.</b> No actions were dispatched.</div>`);
    } else if (run.status === "completed") {
      parts.push(`<div class="banner done"><b>Completed.</b> ${run.approved_by ? `Approved by ${esc(run.approved_by)}.` : "Auto-dispatched within policy."}</div>`);
    }

    parts.push(`<div class="badge-row">
      ${badge(run.priority.toLowerCase(), run.priority)}
      ${d ? badge(d.severity, d.severity) : ""}
      ${d && d.safety_risk ? badge("p1", "safety risk") : ""}
      ${d && d.environmental_risk ? badge("p2", "environmental") : ""}
      ${d && d.injury_reported ? badge("p1", "injury") : ""}
      ${run.redactions ? badge("plain", `${run.redactions} PII redacted`) : ""}
      ${run.language && run.language !== "en" ? badge("plain", `lang ${run.language}`) : ""}
    </div>`);

    parts.push(`<dl class="kv">
      <dt>Asset</dt><dd>${run.asset ? esc(run.asset.name) : "<span class='muted'>unresolved</span>"}</dd>
      <dt>Asset id</dt><dd class="mono">${run.asset ? esc(run.asset.id) : "—"}</dd>
      <dt>Match confidence</dt><dd>${Math.round((run.asset_confidence || 0) * 100)}%</dd>
      <dt>Criticality</dt><dd>${run.asset ? run.asset.criticality + " / 5" : "—"}</dd>
      <dt>Diagnosis confidence</dt><dd>${d ? Math.round(d.confidence * 100) + "%" : "—"}</dd>
      <dt>Respond by</dt><dd>${run.sla_due_at ? esc(new Date(run.sla_due_at).toLocaleString()) : "—"}</dd>
      <dt>Suggested spare</dt><dd class="mono">${d && d.recommended_spare ? esc(d.recommended_spare) : "—"}</dd>
    </dl>`);

    if (d && d.summary) parts.push(`<p class="summary">${esc(d.summary)}</p>`);

    if (d && d.failure_modes.length) {
      parts.push("<h3>Probable failure modes</h3><div class='scroll'>");
      d.failure_modes.forEach((m) => {
        parts.push(`<div class="item info">
          <div class="head"><span class="lbl">${esc(m.name)}</span>${badge("plain", Math.round(m.likelihood * 100) + "%")}</div>
          <div class="msg">${esc(m.rationale)}</div>
          ${m.citations.map((c) => `<span class="cite">${esc(c.doc_id)} · ${esc(c.section)}</span>`).join("")}
        </div>`);
      });
      parts.push("</div>");
    }

    if (run.findings.length) {
      parts.push("<h3>Policy findings</h3>");
      run.findings.forEach((f) => {
        parts.push(`<div class="item ${esc(f.severity)}">
          <div class="head"><span class="rule">${esc(f.rule_id)}</span>${badge(f.priority.toLowerCase(), f.priority)}</div>
          <div class="msg">${esc(f.message)}</div>
          <span class="cite">${esc(f.citation)}</span>
        </div>`);
      });
    }

    if (run.actions.length) {
      parts.push("<h3>Proposed actions</h3>");
      const pending = run.status === "awaiting_approval";
      run.actions.forEach((a) => {
        const st = a.status === "dispatched" ? badge("p4", "dispatched")
          : a.status === "failed" ? badge("p1", "failed")
          : a.status === "rejected" ? badge("plain", "rejected") : "";
        parts.push(`<div class="item">
          <div class="action">
            ${pending ? `<input type="checkbox" checked data-action="${esc(a.id)}" id="act-${esc(a.id)}">` : ""}
            <div>
              <label class="lbl" for="act-${esc(a.id)}" style="margin:0">${esc(a.label)} ${st}</label>
              <div class="why">${esc(a.rationale)}</div>
              <span class="cite">via ${esc(a.connector)}${a.source_rules.length ? " · " + esc(a.source_rules.join(", ")) : ""}${a.result && a.result.reference ? " · " + esc(a.result.reference) : ""}${a.result && a.result.mode ? " (" + esc(a.result.mode) + ")" : ""}</span>
            </div>
          </div>
        </div>`);
      });

      if (pending) {
        parts.push(`<div class="btn-row">
          <input type="text" id="approver" placeholder="Your name" style="flex:1;min-width:120px">
          <button class="btn btn-good" id="approve-btn">Approve &amp; dispatch</button>
          <button class="btn btn-bad" id="reject-btn">Reject</button>
        </div>`);
      }
    }

    if (run.citations.length) {
      parts.push("<h3>Sources</h3>");
      run.citations.forEach((c) =>
        parts.push(`<div class="item"><div class="rule">${esc(c.doc_id)} · ${esc(c.section)}</div><div class="why">${esc(c.snippet)}</div></div>`));
    }

    $("result").innerHTML = parts.join("");

    const approve = $("approve-btn");
    if (approve) {
      approve.addEventListener("click", () => decide(true));
      $("reject-btn").addEventListener("click", () => decide(false));
    }
  };

  const decide = async (approved) => {
    const approver = ($("approver").value || "").trim();
    if (!approver) { $("approver").focus(); return; }
    const rejected = [...document.querySelectorAll("[data-action]")]
      .filter((cb) => !cb.checked).map((cb) => cb.dataset.action);

    $("approve-btn").disabled = $("reject-btn").disabled = true;
    try {
      const run = await api(`/api/runs/${encodeURIComponent(state.runId)}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved, approver, rejected_actions: rejected }),
      });
      state.run = run;
      renderResult(run);
      refreshRuns(); refreshStats();
    } catch (err) {
      $("result").insertAdjacentHTML("afterbegin", `<div class="banner bad">${esc(err.message)}</div>`);
    }
  };

  /* ---------------- lists ---------------- */

  const refreshRuns = async () => {
    const { runs } = await api("/api/runs?limit=25");
    state.p1 = runs.filter((r) => r.priority === "P1").length;
    $("runs").innerHTML = runs.length
      ? runs.map((r) => `
        <button class="runrow ${r.run_id === state.runId ? "active" : ""}" data-run="${esc(r.run_id)}">
          <span class="badge ${esc(r.priority.toLowerCase())}">${esc(r.priority)}</span>
          <span class="txt">${esc(r.asset_name || r.text)}</span>
        </button>`).join("")
      : "<div class='empty'>No runs yet.</div>";
  };

  const refreshStats = async () => {
    const s = await api("/api/stats");
    $("stat-runs").textContent = s.runs;
    $("stat-hold").textContent = s.awaiting_approval;
    $("stat-p1").textContent = state.p1;
    $("stat-actions").textContent = s.actions_dispatched;
  };

  /* ---------------- wiring ---------------- */

  document.addEventListener("click", (e) => {
    const row = e.target.closest("[data-run]");
    if (row) loadRun(row.dataset.run);
  });
  $("run-btn").addEventListener("click", () => submit({}));
  $("clear-btn").addEventListener("click", () => { $("signal-text").value = ""; $("signal-text").focus(); });
  $("signal-text").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit({});
  });

  initTheme();
  boot().catch((err) => {
    document.body.insertAdjacentHTML("afterbegin",
      `<div class="banner bad" style="margin:16px">Failed to load: ${esc(err.message)}</div>`);
  });
})();
