/* AI Agent 评测工作台前端（原生 JS 单页，无构建） */
(function () {
  "use strict";

  // ---------- 工具 ----------
  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtTime(s) { return s ? String(s).slice(0, 19) : "-"; }
  function fmtDur(s) { var d = Number(s); return isFinite(d) ? d.toFixed(1) + "s" : "-"; }

  function api(path, opts) {
    opts = opts || {};
    var cfg = { method: opts.method || "GET", headers: { "Content-Type": "application/json" } };
    if (opts.body) cfg.body = JSON.stringify(opts.body);
    return fetch(path, cfg).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
        return data;
      });
    });
  }

  // ---------- 状态 ----------
  var meta = null;
  var tasksCache = null;
  var backendsCache = null;
  var pollTimer = null;

  function loadMeta() {
    return api("/api/meta").then(function (m) {
      meta = m;
      var box = el("side-meta");
      if (box) box.innerHTML = "v" + esc(m.version) + "<br>任务: " + esc(m.tasks_dir) + "<br>结果: " + esc(m.results_dir);
    });
  }

  function loadTasks() {
    return api("/api/tasks").then(function (d) { tasksCache = d.tasks; return d.tasks; });
  }
  function loadBackends() {
    return api("/api/backends").then(function (d) { backendsCache = d.backends; return d.backends; });
  }

  // ---------- 渲染容器 ----------
  function view() { return el("view"); }

  function renderHTML(html) { view().innerHTML = html; }

  function renderErr(msg) {
    renderHTML('<div class="err-banner">' + esc(msg) + "</div>");
  }

  // ---------- 视图：工作台 ----------
  function viewDashboard() {
    Promise.all([loadTasks(), loadBackends()]).then(function () {
      var taskOpts = tasksCache.map(function (t) {
        return '<option value="' + esc(t.id) + '">' + esc(t.id + " · " + t.title) + "</option>";
      }).join("");
      var backendOpts = backendsCache.map(function (b) {
        return '<option value="' + esc(b.id) + '">' + esc(b.id + " (" + b.version + ")") + "</option>";
      }).join("");
      renderHTML(
        '<h2 class="page-title">工作台 · 运行控制台</h2>' +
        '<div class="card">' +
          '<h3>新建评测运行</h3>' +
          '<div class="form-row">' +
            '<div class="field"><label>任务</label><select id="f-task">' + taskOpts + "</select></div>" +
            '<div class="field"><label>Agent 后端</label><select id="f-agent">' + backendOpts + "</select></div>" +
          "</div>" +
          '<div class="form-row">' +
            '<div class="field"><label>模型（如 deepseek-chat）</label><input id="f-model" value="deepseek-chat"></div>' +
            '<div class="field"><label>超时（秒，留空用任务默认）</label><input id="f-timeout" type="number" placeholder="300"></div>' +
            '<div class="field"><label>采样次数 --runs</label><input id="f-runs" type="number" value="1" min="1" max="20"></div>' +
          "</div>" +
          '<button class="btn" id="btn-run">开始运行</button> <span class="muted">多 run 采样用于对抗 LLM 非确定性</span>' +
        "</div>" +
        '<div id="run-result"></div>'
      );
      el("btn-run").onclick = startRun;
    }).catch(function (e) { renderErr(e.message); });
  }

  function startRun() {
    var btn = el("btn-run");
    var body = {
      task_id: el("f-task").value,
      agent_id: el("f-agent").value,
      model: el("f-model").value || undefined,
      timeout_s: el("f-timeout").value ? Number(el("f-timeout").value) : undefined,
      runs: Number(el("f-runs").value || 1)
    };
    btn.disabled = true;
    el("run-result").innerHTML = '<div class="card"><span class="spin"></span>正在启动运行…</div>';
    api("/api/runs", { method: "POST", body: body }).then(function (res) {
      pollRun(res.last_run_id, res.run_ids);
    }).catch(function (e) {
      btn.disabled = false;
      el("run-result").innerHTML = '<div class="err-banner">' + esc(e.message) + "</div>";
    });
  }

  function pollRun(lastRunId, runIds) {
    var box = el("run-result");
    var tries = 0;
    if (pollTimer) clearInterval(pollTimer);
    function tick() {
      api("/api/runs/" + lastRunId).then(function (r) {
        tries++;
        if (r.running) {
          box.innerHTML = '<div class="card"><span class="spin"></span>运行中… status=' + esc(r.status) + "（第 " + tries + " 次轮询）</div>";
          return;
        }
        clearInterval(pollTimer);
        pollTimer = null;
        var btn = el("btn-run"); if (btn) btn.disabled = false;
        renderRunResult(runIds, r);
      }).catch(function (e) {
        clearInterval(pollTimer); pollTimer = null;
        var btn = el("btn-run"); if (btn) btn.disabled = false;
        box.innerHTML = '<div class="err-banner">' + esc(e.message) + "</div>";
      });
    }
    tick();
    pollTimer = setInterval(tick, 2000);
  }

  function renderRunResult(runIds, first) {
    var box = el("run-result");
    var head = "";
    if (first.status === "error") {
      head = '<div class="err-banner">运行失败：' + esc(first.error || first.status) + "</div>";
    }
    var rows = "";
    var tasks = Promise.all(runIds.map(function (rid) { return api("/api/runs/" + rid).catch(function () { return null; }); }));
    tasks.then(function (recs) {
      var scoreList = [];
      recs.forEach(function (rec, i) {
        if (!rec || rec.running) return;
        var sc = Number((rec.metrics && rec.metrics.score) || 0);
        scoreList.push(sc);
        var ok = (rec.verdicts || []).filter(function (v) { return v.passed; }).length;
        rows +=
          '<div class="verdict-item">' +
          "<b>" + esc(rec.run_id) + "</b> · " + statusBadge(rec.status) +
          " · score <b>" + sc.toFixed(3) + "</b> / " + esc((rec.metrics && rec.metrics.weight) || 0) +
          " · " + ok + "/" + (rec.verdicts || []).length + " 判定通过 · " + fmtDur(rec.duration_s) +
          ' <a href="#/run/' + esc(rec.run_id) + '">详情 →</a>' +
          (rec.error ? ' <span class="err-banner" style="display:inline-block;margin:0 0 0 8px;padding:2px 8px;">' + esc(rec.error) + "</span>" : "") +
          "</div>";
      });
      var stats = "";
      if (scoreList.length > 1) {
        var mean = scoreList.reduce(function (a, b) { return a + b; }, 0) / scoreList.length;
        var best = Math.max.apply(null, scoreList);
        var varr = scoreList.reduce(function (a, s) { return a + (s - mean) * (s - mean); }, 0) / scoreList.length;
        var std = Math.sqrt(varr);
        var pass = scoreList.filter(function (s) { return s > 0; }).length / scoreList.length;
        stats =
          '<div class="kpi-row">' +
          '<div class="kpi"><b>' + best.toFixed(3) + "</b><span>best</span></div>" +
          '<div class="kpi"><b>' + mean.toFixed(3) + "</b><span>mean</span></div>" +
          '<div class="kpi"><b>σ ' + std.toFixed(3) + "</b><span>std</span></div>" +
          '<div class="kpi"><b>' + Math.round(pass * 100) + "%</b><span>pass_rate</span></div>" +
          "</div>";
      }
      box.innerHTML = head + stats +
        '<div class="card"><h3>运行结果（N=' + runIds.length + "）</h3>" + rows + "</div>";
    });
  }

  function statusBadge(st) {
    var cls = ["completed"].indexOf(st) >= 0 ? "completed" : (["max_steps", "timeout", "error"].indexOf(st) >= 0 ? st : "running");
    return '<span class="badge ' + cls + '">' + esc(st) + "</span>";
  }

  // ---------- 视图：任务管理 ----------
  function viewTasks() {
    loadTasks().then(function () {
      var rows = tasksCache.map(function (t) {
        return "<tr><td>" + esc(t.id) + "</td><td>" + esc(t.title) + "</td><td>" + esc(t.level) +
          "</td><td>" + esc(t.verifier) + "</td><td>" + esc(t.weight) + "</td><td>" +
          (t.checkpoints ? t.checkpoints.length : 0) + " 个</td><td>" + esc(t.timeout_s) + "s</td></tr>";
      }).join("");
      renderHTML(
        '<h2 class="page-title">任务管理</h2>' +
        '<div class="card"><h3>现有任务（' + tasksCache.length + "）</h3>" +
          '<table><tr><th>ID</th><th>标题</th><th>级别</th><th>判定</th><th>权重</th><th>校验点</th><th>超时</th></tr>' +
          rows + "</table></div>" +
        '<div class="card"><h3>新建任务</h3>' + taskFormHTML() + "</div>" +
        '<div id="task-result"></div>'
      );
      el("btn-gen").onclick = genTask;
      el("btn-add-cp").onclick = addCheckpointRow;
      addCheckpointRow();
    }).catch(function (e) { renderErr(e.message); });
  }

  function taskFormHTML() {
    var lv = ["L1", "L2", "L3", "L4", "L5"].map(function (l) { return "<option>" + l + "</option>"; }).join("");
    return (
      '<div class="form-row">' +
        '<div class="field"><label>任务 ID（3-20 位字母/数字/_）</label><input id="g-id" placeholder="T601"></div>' +
        '<div class="field"><label>标题</label><input id="g-title"></div>' +
        '<div class="field"><label>级别</label><select id="g-level">' + lv + "</select></div>" +
      "</div>" +
      '<div class="form-row">' +
        '<div class="field"><label>判定器</label><select id="g-verifier"><option>deterministic</option><option>llm_judge</option></select></div>' +
        '<div class="field"><label>权重</label><input id="g-weight" type="number" step="0.1" value="1.0"></div>' +
        '<div class="field"><label>超时（秒）</label><input id="g-timeout" type="number" value="300"></div>' +
        '<div class="field"><label>标签（逗号分隔）</label><input id="g-tags" placeholder="file,text"></div>' +
      "</div>" +
      '<div class="field" style="margin-bottom:12px;"><label>任务描述</label><textarea id="g-desc" placeholder="说明 Agent 需要在工作目录中完成什么、产出什么文件"></textarea></div>' +
      '<div class="field" style="margin-bottom:12px;"><label>校验点（ground_truth.checkpoints）</label></div>' +
      '<div id="cp-list"></div>' +
      '<button class="btn secondary" id="btn-add-cp" type="button">+ 添加校验点</button> ' +
      '<button class="btn" id="btn-gen" type="button">生成任务包</button>'
    );
  }

  var CP_TYPES = ["file_exists", "file_not_exists", "content_contains", "content_not_contains", "cmd_exit_zero"];
  var CP_TYPES_ZH = {
    "file_exists": "文件存在", "file_not_exists": "文件不存在",
    "content_contains": "内容包含(正则)", "content_not_contains": "内容不含(正则)", "cmd_exit_zero": "命令退出码=0"
  };

  function cpTypeOptions(sel) {
    return CP_TYPES.map(function (t) {
      return '<option value="' + t + '"' + (t === sel ? " selected" : "") + ">" + esc(CP_TYPES_ZH[t] || t) + "</option>";
    }).join("");
  }

  function addCheckpointRow(sel) {
    var list = el("cp-list");
    var div = document.createElement("div");
    div.className = "cp-row";
    div.style.cssText = "border:1px dashed #D8D6CF;border-radius:8px;padding:10px;margin-bottom:10px;";
    div.innerHTML =
      '<div class="form-row">' +
        '<div class="field" style="flex:0 0 80px;"><label>ID</label><input class="cp-id" value="c' + (list.children.length + 1) + '"></div>' +
        '<div class="field"><label>类型</label><select class="cp-type">' + cpTypeOptions(sel) + "</select></div>" +
        '<div class="field"><label>描述</label><input class="cp-desc"></div>' +
        '<div class="field" style="flex:0 0 90px;"><label>&nbsp;</label><button class="btn secondary" type="button" style="padding:7px 12px;">删除</button></div>' +
      "</div>" +
      '<div class="field" style="margin-bottom:6px;"><label>path（相对工作目录，支持 glob）</label><input class="cp-path" placeholder="output/report.md"></div>' +
      '<div class="field" style="margin-bottom:6px;"><label>pattern（正则，内容类必填）</label><input class="cp-pattern" placeholder="2026-\\d{2}-\\d{2}"></div>' +
      '<div class="field"><label>cmd（cmd_exit_zero 必填，如 python @scripts/verify_t102.py .）</label><input class="cp-cmd" placeholder=""></div>';
    list.appendChild(div);
    div.querySelector(".btn").onclick = function () { list.removeChild(div); };
  }

  function collectCheckpoints() {
    var out = [];
    var rows = document.querySelectorAll("#cp-list .cp-row");
    rows.forEach(function (r) {
      var cp = {
        id: r.querySelector(".cp-id").value.trim(),
        type: r.querySelector(".cp-type").value,
        desc: r.querySelector(".cp-desc").value.trim(),
        path: r.querySelector(".cp-path").value.trim(),
        pattern: r.querySelector(".cp-pattern").value.trim(),
        cmd: r.querySelector(".cp-cmd").value.trim()
      };
      if (cp.id) out.push(cp);
    });
    return out;
  }

  function genTask() {
    var body = {
      id: el("g-id").value.trim(),
      title: el("g-title").value.trim(),
      level: el("g-level").value,
      verifier: el("g-verifier").value,
      weight: Number(el("g-weight").value || 1),
      timeout_s: Number(el("g-timeout").value || 300),
      tags: el("g-tags").value.trim(),
      description: el("g-desc").value.trim(),
      checkpoints: collectCheckpoints()
    };
    el("btn-gen").disabled = true;
    api("/api/tasks/generate", { method: "POST", body: body }).then(function (r) {
      el("btn-gen").disabled = false;
      el("task-result").innerHTML =
        '<div class="card"><h3>任务包已生成</h3>' +
        "<div>任务目录：<code>" + esc(r.task_dir) + "</code></div>" +
        "<div>spec 文件：<code>" + esc(r.spec_path) + "</code></div>" +
        '<div class="muted">已写入 manifest.tasks' + (r.manifest_updated ? "（已存在）" : "（追加）") + '。可回到工作台运行该任务。</div>' +
        "</div>";
      loadTasks();
    }).catch(function (e) {
      el("btn-gen").disabled = false;
      el("task-result").innerHTML = '<div class="err-banner">' + esc(e.message) + "</div>";
    });
  }

  // ---------- 视图：运行历史 ----------
  function viewHistory() {
    Promise.all([loadTasks(), loadBackends()]).then(function () {
      var taskOpts = '<option value="">全部任务</option>' + tasksCache.map(function (t) {
        return '<option value="' + esc(t.id) + '">' + esc(t.id) + "</option>";
      }).join("");
      var agentOpts = '<option value="">全部后端</option>' + backendsCache.map(function (b) {
        return '<option value="' + esc(b.id) + '">' + esc(b.id) + "</option>";
      }).join("");
      renderHTML(
        '<h2 class="page-title">运行历史</h2>' +
        '<div class="card">' +
          '<div class="form-row">' +
            '<div class="field"><label>任务</label><select id="h-task">' + taskOpts + "</select></div>" +
            '<div class="field"><label>后端</label><select id="h-agent">' + agentOpts + "</select></div>" +
            '<div class="field"><label>状态</label><select id="h-status"><option value="">全部</option><option>completed</option><option>max_steps</option><option>timeout</option><option>error</option></select></div>' +
            '<div class="field" style="flex:0 0 100px;"><label>&nbsp;</label><button class="btn secondary" id="h-filter">筛选</button></div>' +
          "</div>" +
          '<div id="h-list"><div class="empty">加载中…</div></div>' +
        "</div>"
      );
      el("h-filter").onclick = loadHistory;
      loadHistory();
    }).catch(function (e) { renderErr(e.message); });
  }

  function loadHistory() {
    var q = [];
    var tv = el("h-task") && el("h-task").value;
    var av = el("h-agent") && el("h-agent").value;
    var sv = el("h-status") && el("h-status").value;
    if (tv) q.push("task_id=" + encodeURIComponent(tv));
    if (av) q.push("agent_id=" + encodeURIComponent(av));
    if (sv) q.push("status=" + encodeURIComponent(sv));
    api("/api/runs?" + q.join("&")).then(function (d) {
      var list = el("h-list");
      if (!d.runs || !d.runs.length) { list.innerHTML = '<div class="empty">暂无运行记录</div>'; return; }
      var rows = d.runs.map(function (r) {
        return '<tr class="clickable" data-rid="' + esc(r.run_id) + '">' +
          "<td>" + esc(fmtTime(r.created_at)) + "</td>" +
          "<td><b>" + esc(r.run_id) + "</b></td>" +
          "<td>" + esc(r.task_id) + "</td><td>" + esc(r.agent_id) + "</td>" +
          "<td>" + statusBadge(r.status) + "</td>" +
          "<td>" + esc(r.score) + "</td><td>" + fmtDur(r.duration_s) + "</td><td>" + esc(r.steps) + "</td>" +
          "</tr>";
      }).join("");
      list.innerHTML = '<table><tr><th>时间</th><th>run_id</th><th>任务</th><th>后端</th><th>状态</th><th>score</th><th>时长</th><th>步数</th></tr>' + rows + "</table>";
      list.querySelectorAll("tr.clickable").forEach(function (tr) {
        tr.onclick = function () { location.hash = "#/run/" + tr.getAttribute("data-rid"); };
      });
    }).catch(function (e) { el("h-list").innerHTML = '<div class="err-banner">' + esc(e.message) + "</div>"; });
  }

  // ---------- 视图：运行详情 ----------
  function viewRunDetail(runId) {
    api("/api/runs/" + runId).then(function (r) {
      if (r.running) {
        renderHTML('<h2 class="page-title">运行详情</h2><div class="card"><span class="spin"></span>正在运行，请稍候刷新…</div>');
        setTimeout(function () { viewRunDetail(runId); }, 2000);
        return;
      }
      var v = r.verdicts || [];
      var vRows = v.map(function (v) {
        return '<div class="verdict-item"><span class="badge ' + (v.passed ? "pass" : "fail") + '">' +
          (v.passed ? "PASS" : "FAIL") + "</span> <b>" + esc(v.id) + "</b> · " + esc(v.type) +
          " · " + esc(v.detail) + "</div>";
      }).join("");
      var steps = (r.steps || []).map(function (s, i) {
        return '<div class="step-item"><b>#' + (i + 1) + "</b> " + esc(s.action || s.step || "") +
          (s.args ? " <code>" + esc(String(s.args).slice(0, 120)) + "</code>" : "") +
          (s.observation ? '<div class="muted">→ ' + esc(String(s.observation).slice(0, 160)) + "</div>" : "") +
          "</div>";
      }).join("") || '<div class="muted">（黑盒后端无可视化轨迹）</div>';
      var sc = (r.metrics && r.metrics.score) || 0;
      var wt = (r.metrics && r.metrics.weight) || 0;
      renderHTML(
        '<h2 class="page-title">运行详情 <a class="btn secondary" style="float:right;" href="#/history">← 返回历史</a></h2>' +
        '<div class="kpi-row">' +
          '<div class="kpi"><b>' + esc(r.run_id) + "</b><span>run_id</span></div>" +
          '<div class="kpi"><b>' + esc(r.task_id) + " / " + esc(r.agent_id) + "</b><span>任务 / 后端</span></div>" +
          '<div class="kpi"><b>' + sc.toFixed(3) + " / " + esc(wt) + "</b><span>score / 权重</span></div>" +
          '<div class="kpi"><b>' + fmtDur(r.duration_s) + "</b><span>时长</span></div>" +
        "</div>" +
        (r.error ? '<div class="err-banner">' + esc(r.error) + "</div>" : "") +
        '<div class="card"><h3>判定结果（' + v.length + " 个校验点）</h3>" + (vRows || '<div class="empty">无判定</div>') + "</div>" +
        '<div class="card"><h3>执行轨迹（' + (r.steps || []).length + " 步）</h3>" + steps + "</div>" +
        '<div class="card"><h3>产物文件</h3><div id="file-list"><div class="empty">加载中…</div></div></div>' +
        '<div class="card" id="file-view" style="display:none;"><h3>文件预览</h3><pre class="code" id="file-content"></pre></div>'
      );
      loadFiles(runId);
    }).catch(function (e) { renderErr(e.message); });
  }

  function loadFiles(runId) {
    api("/api/runs/" + runId + "/files").then(function (d) {
      var list = el("file-list");
      if (!d.files || !d.files.length) { list.innerHTML = '<div class="empty">无产物文件</div>'; return; }
      var rows = d.files.map(function (f) {
        return '<div class="file-row" data-path="' + esc(f.path) + '">' +
          esc(f.path) + '<span class="sz">' + fmtSize(f.size) + "</span></div>";
      }).join("");
      list.innerHTML = rows;
      list.querySelectorAll(".file-row").forEach(function (r) {
        r.onclick = function () { readFile(runId, r.getAttribute("data-path")); };
      });
    }).catch(function (e) { el("file-list").innerHTML = '<div class="err-banner">' + esc(e.message) + "</div>"; });
  }

  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  function readFile(runId, path) {
    api("/api/runs/" + runId + "/file?path=" + encodeURIComponent(path)).then(function (d) {
      var fv = el("file-view");
      fv.style.display = "";
      el("file-content").textContent = d.content;
    }).catch(function (e) {
      var fv = el("file-view");
      fv.style.display = "";
      el("file-content").textContent = "读取失败：" + e.message;
    });
  }

  // ---------- 视图：对比 ----------
  function viewCompare() {
    api("/api/summary").then(function (s) {
      if (!s.total_runs) {
        renderHTML('<h2 class="page-title">对比</h2><div class="empty">暂无运行数据，先去工作台跑一次评测。</div>');
        return;
      }
      var agents = s.agents || [];
      var tasks = s.tasks || [];
      var head = tasks.map(function (t) { return "<th>" + esc(t) + "</th>"; }).join("");
      var matrix = agents.map(function (a) {
        var cells = (s.agent_rows[a] || []).map(function (row) {
          var cls = row.pass_rate >= 0.999 ? "ok" : (row.pass_rate >= 0.5 ? "half" : "bad");
          return '<td class="matrix-cell ' + cls + '">' + esc(row.score) + "<br><span class='sub'>" +
            Math.round(row.pass_rate * 100) + "%</span></td>";
        }).join("");
        return "<tr><td style='font-weight:600;'>" + esc(a) + "</td>" + cells + "</tr>";
      }).join("");
      var sHead = tasks.map(function (t) { return "<th>" + esc(t) + "</th>"; }).join("");
      var sRows = agents.map(function (a) {
        var cells = tasks.map(function (t) {
          var st = s.sample_stats[a + "|" + t];
          return st ? '<td class="matrix-cell sub">N=' + st.n + "<br>mean " + st.mean + " · best " + st.best + " · σ " + st.std + "</td>" : "<td class='matrix-cell sub'>—</td>";
        }).join("");
        return "<tr><td style='font-weight:600;'>" + esc(a) + "</td>" + cells + "</tr>";
      }).join("");
      renderHTML(
        '<h2 class="page-title">Agent × 任务 对比</h2>' +
        '<div class="card"><h3>得分矩阵（取各组合最好成绩，颜色=通过率）</h3>' +
          '<table><tr><th>后端</th>' + head + "</tr>" + matrix + "</table></div>" +
        '<div class="card"><h3>采样统计（对抗非确定性）</h3>' +
          '<table><tr><th>后端</th>' + sHead + "</tr>" + sRows + "</table></div>" +
        '<div class="card"><h3>任务通过率（全部 run 平均）</h3>' +
          tasks.map(function (t) {
            var rate = s.task_pass[t] || 0;
            var color = rate >= 0.999 ? "#52C41A" : (rate >= 0.5 ? "#FAAD14" : "#EA6668");
            return '<div style="display:flex;align-items:center;gap:12px;margin:8px 0;">' +
              '<div style="flex:0 0 60px;">' + esc(t) + "</div>" +
              '<div style="flex:1;height:8px;border-radius:4px;background:rgba(0,0,0,0.06);">' +
              '<div style="height:8px;border-radius:4px;width:' + Math.max(2, Math.round(rate * 100)) + "%;background:" + color + ';"></div></div>' +
              '<div style="flex:0 0 50px;text-align:right;font-size:12px;">' + Math.round(rate * 100) + "%</div></div>";
          }).join("") +
        "</div>"
      );
    }).catch(function (e) { renderErr(e.message); });
  }

  // ---------- 视图：报告 ----------
  function viewReport() {
    renderHTML(
      '<h2 class="page-title">评测报告</h2>' +
      '<div class="card"><button class="btn" id="btn-gen-report">生成报告</button> ' +
      '<span class="muted">复用引擎 reporter 生成自包含 HTML（离线可看）</span></div>' +
      '<iframe id="report-frame" style="width:100%;height:70vh;border:1px solid #E4E3DD;border-radius:12px;background:#fff;"></iframe>'
    );
    var frame = el("report-frame");
    frame.src = "/reports/report.html";
    el("btn-gen-report").onclick = function () {
      var b = el("btn-gen-report");
      b.disabled = true; b.textContent = "生成中…";
      api("/api/report", { method: "POST", body: { out_name: "report.html" } }).then(function (r) {
        b.disabled = false; b.textContent = "生成报告";
        frame.src = r.url + "?t=" + Date.now();
      }).catch(function (e) {
        b.disabled = false; b.textContent = "生成报告";
        alert("生成失败：" + e.message);
      });
    };
  }

  // ---------- 视图：设置 ----------
  function viewSettings() {
    loadMeta().then(function () {
      renderHTML(
        '<h2 class="page-title">设置</h2>' +
        '<div class="card"><h3>目录与版本</h3>' +
          '<table class="env-table"><tr><td>框架版本</td><td>' + esc(meta.version) + "</td></tr>" +
          "<tr><td>任务目录 tasks_dir</td><td>" + esc(meta.tasks_dir) + "</td></tr>" +
          "<tr><td>结果目录 results_dir</td><td>" + esc(meta.results_dir) + "</td></tr>" +
          "<tr><td>报告目录 report_dir</td><td>" + esc(meta.report_dir) + "</td></tr></table></div>" +
        '<div class="card"><h3>环境变量（后端 Agent 使用）</h3>' +
          '<table class="env-table"><tr><td>DEEPSEEK_API_KEY</td><td>DeepSeek API 密钥（minimal-react 默认读取）</td></tr>' +
          "<tr><td>LLM_API_KEY / LLM_BASE_URL</td><td>自定义 OpenAI 兼容端点（可选覆盖）</td></tr>" +
          "<tr><td>AGENT_EVAL_TASKS</td><td>任务包目录覆盖（默认 ./tasks）</td></tr></table></div>" +
        '<div class="card"><h3>启动方式</h3><pre class="code">pip install -e ".[web]"&#10;python -m agent_eval.web --port 8000&#10;# 浏览器打开 http://127.0.0.1:8000</pre></div>'
      );
    }).catch(function (e) { renderErr(e.message); });
  }

  // ---------- 路由 ----------
  function router() {
    var h = location.hash || "#/dashboard";
    var parts = h.replace(/^#\//, "").split("/");
    var name = parts[0] || "dashboard";
    var nav = document.querySelectorAll(".nav a");
    nav.forEach(function (a) {
      a.classList.toggle("active", a.getAttribute("data-view") === name);
    });
    if (name === "run") { viewRunDetail(parts[1]); return; }
    if (name === "dashboard") viewDashboard();
    else if (name === "tasks") viewTasks();
    else if (name === "history") viewHistory();
    else if (name === "compare") viewCompare();
    else if (name === "report") viewReport();
    else if (name === "settings") viewSettings();
    else viewDashboard();
  }

  // ---------- 启动 ----------
  loadMeta().catch(function () {});
  window.addEventListener("hashchange", router);
  router();
})();
