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
  // 安全截断：按码点截断，不劈开 emoji 等代理对（避免 � 乱码）
  function clip(s, n) {
    s = String(s == null ? "" : s);
    if (s.length <= n) return s;
    var cut = n;
    while (cut > 0) {
      var c = s.charCodeAt(cut);
      if (c >= 0xD800 && c <= 0xDFFF) { cut--; continue; }  // 落点在高位代理 → 前移
      break;
    }
    return s.slice(0, cut) + "…";
  }
  // 轨迹参数序列化：dict → JSON 文本；否则字符串化
  function strOf(v) {
    if (v == null) return "";
    if (typeof v === "object") {
      try { return JSON.stringify(v); } catch (e) { return String(v); }
    }
    return String(v);
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
  var costsCache = null;
  var tiersCache = null;       // V3.1：数据集4层分层配置
  var corePackCache = null;    // V3.1：core包定义（tier列表）
  var histPage = 0;      // 运行历史当前页（从 0 起）
  var histLimit = 20;    // 每页条数
  var backendsCache = null;
  var pollTimer = null;
  var mxState = { license: null, current: null, matrix: null, sel: null, timer: null };

  function loadMeta() {
    return api("/api/meta").then(function (m) {
      meta = m;
      var box = el("side-meta");
      if (box) {
        box.innerHTML =
          '<div class="sm-ver">v' + esc(m.version) + '</div>' +
          '<div class="sm-path" title="' + esc(m.tasks_dir) + '"><span class="sm-label">任务</span>' + esc(m.tasks_dir) + '</div>' +
          '<div class="sm-path" title="' + esc(m.results_dir) + '"><span class="sm-label">结果</span>' + esc(m.results_dir) + '</div>';
      }
    });
  }

  function loadTasks() {
    return api("/api/tasks").then(function (d) {
      tasksCache = d.tasks;
      tiersCache = d.tiers || {};
      corePackCache = d.core_pack || ["golden", "regression"];
      return d.tasks;
    });
  }
  function loadBackends() {
    return api("/api/backends").then(function (d) { backendsCache = d.backends; return d.backends; });
  }
  function loadCosts() {
    return api("/api/costs").then(function (d) { costsCache = d; return d; });
  }

  // ---------- 成本计算说明提示（ⓘ） ----------
  function costTip(html) {
    return '<span class="cost-tip-wrap"><span class="cost-tip-icon" tabindex="0">ⓘ</span>' +
      '<span class="cost-tip-box">' + html + "</span></span>";
  }
  function bindCostTips() {
    var icons = document.querySelectorAll(".cost-tip-icon");
    for (var i = 0; i < icons.length; i++) {
      (function (ic) {
        var wrap = ic.closest(".cost-tip-wrap");
        if (!wrap) return;
        ic.onmouseenter = function () { wrap.classList.add("open"); };
        ic.onmouseleave = function () { wrap.classList.remove("open"); };
        ic.onclick = function () { wrap.classList.toggle("open"); };
        ic.onfocus = function () { wrap.classList.add("open"); };
        ic.onblur = function () { wrap.classList.remove("open"); };
      })(icons[i]);
    }
  }

  // ---------- 渲染容器 ----------
  function view() { return el("view"); }

  function renderHTML(html) { view().innerHTML = html; }

  function renderErr(msg) {
    renderHTML('<div class="err-banner">' + esc(msg) + "</div>");
  }

  // ---------- 视图：工作台 ----------
  function updateCost() {
    // 根据所选 任务/后端/采样次数 实时估算 LLM 成本
    var taskId = el("f-task") && el("f-task").value;
    var agentId = el("f-agent") && el("f-agent").value;
    var runs = Math.max(1, Number((el("f-runs") && el("f-runs").value) || 1));
    var box = el("f-cost");
    if (!box || !costsCache || !taskId) return;
    var price = costsCache.pricing || { input_cny_per_m: 2, output_cny_per_m: 3 };
    var cell = ((costsCache.benchmark || {})[agentId] || {})[taskId];
    var task = null;
    if (tasksCache) {
      for (var i = 0; i < tasksCache.length; i++) {
        if (tasksCache[i].id === taskId) { task = tasksCache[i]; break; }
      }
    }
    var pt, ct, src;
    if (cell && (cell.prompt_tokens || cell.completion_tokens)) {
      pt = cell.prompt_tokens * runs;
      ct = cell.completion_tokens * runs;
      src = "实测基准";
    } else if (task) {
      var est = (costsCache.level_estimate || {})[task.level] || { prompt_tokens: 4500, completion_tokens: 500 };
      pt = est.prompt_tokens * runs;
      ct = est.completion_tokens * runs;
      if (task.verifier === "llm_judge") { pt += 1500 * runs; ct += 500 * runs; }
      src = "估算";
    } else { return; }
    var cost = pt / 1e6 * price.input_cny_per_m + ct / 1e6 * price.output_cny_per_m;
    box.innerHTML = "¥" + cost.toFixed(4) +
      " <span class='muted'>（" + src + " · " + runs + " run）</span>";
  }

  function viewDashboard() {
    Promise.all([loadTasks(), loadBackends(), loadCosts()]).then(function () {
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
            '<div class="field"><label>模型（按后端自动填充）</label><input id="f-model" value=""></div>' +
            '<div class="field"><label>超时（秒，留空用任务默认）</label><input id="f-timeout" type="number" placeholder="300"></div>' +
            '<div class="field"><label>采样次数 --runs</label><input id="f-runs" type="number" value="1" min="1" max="20"></div>' +
          "</div>" +
          '<div class="form-row"><div class="field"><label>预计 LLM 成本</label><span id="f-cost" class="muted">—</span></div></div>' +
          '<button class="btn" id="btn-run">开始运行</button> <span class="muted">多 run 采样用于对抗 LLM 非确定性</span>' +
        "</div>" +
        '<div id="run-result"></div>'
      );
      el("f-task").onchange = updateCost;
      el("f-agent").onchange = onAgentChange;
      el("f-runs").oninput = updateCost;
      onAgentChange();  // 初始化模型默认值
      el("btn-run").onclick = startRun;
    }).catch(function (e) { renderErr(e.message); });
  }

  function onAgentChange() {
    var b = backendsCache.find(function (x) { return x.id === el("f-agent").value; });
    if (b && b.default_model) el("f-model").value = b.default_model;
    updateCost();
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
      // tier 筛选选项
      var tierOpts = '<option value="">全部分层</option>';
      if (tiersCache) {
        Object.keys(tiersCache).forEach(function (k) {
          tierOpts += '<option value="' + k + '">' + esc(tiersCache[k].label || k) + '</option>';
        });
      }
      // tier 标签渲染函数
      function tierBadge(tier) {
        if (!tier || !tiersCache || !tiersCache[tier]) return '<span class="muted">—</span>';
        var t = tiersCache[tier];
        return '<span class="tier-badge" style="background:' + (t.color || '#6b7280') + '20;color:' + (t.color || '#6b7280') + ';">' + esc(t.label || tier) + '</span>';
      }
      // 按 tier 统计
      var tierStats = {};
      tasksCache.forEach(function (t) {
        var tier = t.tier || 'unknown';
        tierStats[tier] = (tierStats[tier] || 0) + 1;
      });
      var statsHtml = Object.keys(tierStats).map(function (k) {
        var t = tiersCache && tiersCache[k] ? tiersCache[k] : { label: k, color: '#6b7280' };
        return '<span class="tier-stat"><b style="color:' + t.color + ';">' + tierStats[k] + '</b> ' + esc(t.label) + '</span>';
      }).join("");

      function renderRows(filterTier) {
        var filtered = filterTier ? tasksCache.filter(function (t) { return t.tier === filterTier; }) : tasksCache;
        return filtered.map(function (t) {
          var ce = t.cost_estimate;
          var costTxt = ce ? (ce.source === "measured" ? "" : "~") + "¥" + ce.cost_cny.toFixed(4) : "—";
          return "<tr><td>" + esc(t.id) + "</td><td>" + esc(t.title) + "</td><td>" + tierBadge(t.tier) +
            "</td><td>" + esc(t.level) +
            "</td><td>" + esc(t.verifier) + "</td><td>" + esc(t.weight) + "</td><td>" +
            (t.checkpoints ? t.checkpoints.length : 0) + " 个</td><td>" + esc(t.timeout_s) + "s</td><td>" +
            costTxt + "</td></tr>";
        }).join("");
      }

      renderHTML(
        '<h2 class="page-title">任务管理</h2>' +
        '<div class="card"><h3>现有任务（' + tasksCache.length + "） " +
          '<span class="tier-stats">' + statsHtml + '</span></h3>' +
          '<div class="form-row" style="margin-bottom:12px;">' +
            '<div class="field" style="flex:0 0 200px;"><label>按分层筛选</label><select id="tier-filter">' + tierOpts + "</select></div>" +
          "</div>" +
          '<div id="task-table">' +
          '<table><tr><th>ID</th><th>标题</th><th>分层</th><th>级别</th><th>判定</th><th>权重</th><th>校验点</th><th>超时</th><th>预计成本/run' +
          costTip("预计成本 = 单次 run 的 token 消耗 × 模型单价。<br>默认模型 deepseek-chat：输入 ¥2/百万 token、输出 ¥3/百万 token（缓存未命中口径）。<br><br>有实测：取该后端（minimal-react）在此任务的历史 run 的 metrics.usage 均值；<br>无实测：按任务级别 L1-L5 估算，数值前标「~」。<br><br>单价可用环境变量 LLM_INPUT_CNY_PER_M / LLM_OUTPUT_CNY_PER_M 覆盖。") +
          "</th></tr>" +
          renderRows("") + "</table></div></div>" +
        '<div class="card"><h3>新建任务</h3>' + taskFormHTML() + "</div>" +
        '<div id="task-result"></div>'
      );
      el("btn-gen").onclick = genTask;
      el("btn-add-cp").onclick = addCheckpointRow;
      addCheckpointRow();
      bindCostTips();
      // tier 筛选
      el("tier-filter").onchange = function () {
        var v = this.value;
        el("task-table").innerHTML = '<table><tr><th>ID</th><th>标题</th><th>分层</th><th>级别</th><th>判定</th><th>权重</th><th>校验点</th><th>超时</th><th>预计成本/run</th></tr>' + renderRows(v) + "</table>";
      };
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
      '<div class="field" style="margin-bottom:12px;"><label>评分标准 rubric（仅 llm_judge 需要；留空使用默认标准）</label><textarea id="g-rubric" placeholder="例如：1. 内容完整性(30分)… 2. 准确性(25分)…"></textarea></div>' +
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
      rubric: el("g-rubric").value.trim(),
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
      el("h-filter").onclick = function () { histPage = 0; loadHistory(); };
      loadHistory();
    }).catch(function (e) { renderErr(e.message); });
  }

  function loadHistory() {
    var q = ["limit=" + histLimit, "offset=" + (histPage * histLimit)];
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
        var costTxt = (r.actual_cost_cny !== null && r.actual_cost_cny !== undefined)
          ? "¥" + r.actual_cost_cny.toFixed(4) : "—";
        var confTxt = "—";
        if (r.confidence_score !== null && r.confidence_score !== undefined) {
          var confColor = r.confidence_level === "high" ? "#22c55e" : r.confidence_level === "medium" ? "#f59e0b" : "#ef4444";
          confTxt = '<span style="color:' + confColor + ';font-weight:600;">' + r.confidence_score.toFixed(1) + "</span>";
        }
        return '<tr class="clickable" data-rid="' + esc(r.run_id) + '">' +
          "<td>" + esc(fmtTime(r.created_at)) + "</td>" +
          "<td><b>" + esc(r.run_id) + "</b></td>" +
          "<td>" + esc(r.task_id) + "</td><td>" + esc(r.agent_id) + "</td>" +
          "<td>" + statusBadge(r.status) + "</td>" +
          "<td>" + esc(r.score) + "</td><td>" + fmtDur(r.duration_s) + "</td><td>" + esc(r.steps) + "</td>" +
          "<td>" + costTxt + "</td>" +
          "<td>" + confTxt + "</td>" +
          "</tr>";
      }).join("");
      var total = Number(d.total || 0);
      var pages = Math.max(1, Math.ceil(total / histLimit));
      var cur = Math.min(histPage + 1, pages);
      var pager = '<div class="pager">' +
        '<button class="btn secondary small" id="h-prev"' + (histPage <= 0 ? " disabled" : "") + '>‹ 上一页</button>' +
        '<span class="pager-info">第 ' + cur + " / " + pages + " 页 · 共 " + total + " 条</span>" +
        '<button class="btn secondary small" id="h-next"' + (histPage >= pages - 1 ? " disabled" : "") + '>下一页 ›</button>' +
        "</div>";
      list.innerHTML = '<table><tr><th>时间</th><th>run_id</th><th>任务</th><th>后端</th><th>状态</th><th>score</th><th>时长</th><th>步数</th><th>实际成本' +
        costTip("实际成本 = 本次评测实际消耗的 token（run.json 的 metrics.usage）按模型单价折算。<br>默认模型 deepseek-chat：输入 ¥2/百万 token、输出 ¥3/百万 token。<br><br>token 埋点（metrics.usage）之前的历史 run 无记录，显示「—」。<br><br>单价可用环境变量 LLM_INPUT_CNY_PER_M / LLM_OUTPUT_CNY_PER_M 覆盖。") +
        '</th><th>置信度' +
        costTip("评测置信度 = 运行次数(25%) + 校验点类型(25%) + 任务覆盖度(20%) + 历史稳定性(15%) + 任务级别(15%) 加权计算。<br><br>高置信(≥80)：结果可信，可用于决策<br>中置信(60-79)：有一定参考价值，建议补充验证<br>低置信(<60)：结果不可靠，需增加 runs 或扩大任务集") +
        "</th></tr>" + rows + "</table>" + pager;
      list.querySelectorAll("tr.clickable").forEach(function (tr) {
        tr.onclick = function () { location.hash = "#/run/" + tr.getAttribute("data-rid"); };
      });
      var prevBtn = el("h-prev"), nextBtn = el("h-next");
      if (prevBtn) prevBtn.onclick = function () { if (histPage > 0) { histPage--; loadHistory(); } };
      if (nextBtn) nextBtn.onclick = function () { if (histPage < pages - 1) { histPage++; loadHistory(); } };
      bindCostTips();
    }).catch(function (e) { el("h-list").innerHTML = '<div class="err-banner">' + esc(e.message) + "</div>"; });
  }

  // ---------- 视图：Badcase 管理（V2.8） ----------
  var bcPage = 0, bcLimit = 20, bcTab = "all";

  var BC_CATEGORY_LABELS = {
    reasoning: "推理错误", tool_use: "工具使用错误", format: "输出格式错误",
    timeout: "超时", crash: "异常崩溃", hallucination: "幻觉/编造",
    planning: "规划错误", context: "上下文理解错误", other: "其他"
  };
  var BC_STATUS_LABELS = {
    pending: "待分析", analyzing: "分析中", fixed: "已修复", ignored: "已忽略"
  };
  var BC_SEVERITY_COLORS = { P0: "#dc2626", P1: "#ea580c", P2: "#ca8a04", P3: "#65a30d" };
  var BC_STATUS_COLORS = { pending: "#6b7280", analyzing: "#2563eb", fixed: "#16a34a", ignored: "#9ca3af" };

  function viewBadcases() {
    Promise.all([loadTasks(), loadBackends()]).then(function () {
      var taskOpts = '<option value="">全部任务</option>' + tasksCache.map(function (t) {
        return '<option value="' + esc(t.id) + '">' + esc(t.id) + "</option>";
      }).join("");
      var agentOpts = '<option value="">全部后端</option>' + backendsCache.map(function (b) {
        return '<option value="' + esc(b.id) + '">' + esc(b.id) + "</option>";
      }).join("");
      var catOpts = '<option value="">全部分类</option>' + Object.keys(BC_CATEGORY_LABELS).map(function (k) {
        return '<option value="' + k + '">' + BC_CATEGORY_LABELS[k] + "</option>";
      }).join("");
      var sevOpts = '<option value="">全部严重度</option><option>P0</option><option>P1</option><option>P2</option><option>P3</option>';
      var stOpts = '<option value="">全部状态</option>' + Object.keys(BC_STATUS_LABELS).map(function (k) {
        return '<option value="' + k + '">' + BC_STATUS_LABELS[k] + "</option>";
      }).join("");
      var allTabClass = bcTab === "all" ? "bc-tab-active" : "";
      var regTabClass = bcTab === "regression" ? "bc-tab-active" : "";
      renderHTML(
        '<h2 class="page-title">Badcase 管理</h2>' +
        '<div class="bc-tabs">' +
          '<button class="bc-tab ' + allTabClass + '" id="bc-tab-all">全部 badcase</button>' +
          '<button class="bc-tab ' + regTabClass + '" id="bc-tab-regression">回归测试集</button>' +
        "</div>" +
        '<div class="card">' +
          '<div class="form-row">' +
            '<div class="field"><label>任务</label><select id="bc-task">' + taskOpts + "</select></div>" +
            '<div class="field"><label>后端</label><select id="bc-agent">' + agentOpts + "</select></div>" +
            '<div class="field"><label>分类</label><select id="bc-category">' + catOpts + "</select></div>" +
          "</div>" +
          '<div class="form-row">' +
            '<div class="field"><label>严重度</label><select id="bc-severity">' + sevOpts + "</select></div>" +
            '<div class="field"><label>状态</label><select id="bc-status">' + stOpts + "</select></div>" +
            '<div class="field" style="flex:0 0 100px;"><label>&nbsp;</label><button class="btn secondary" id="bc-filter">筛选</button></div>' +
          "</div>" +
          '<div id="bc-stats" class="bc-stats"></div>' +
          '<div id="bc-list"><div class="empty">加载中…</div></div>' +
        "</div>"
      );
      el("bc-filter").onclick = function () { bcPage = 0; loadBadcases(); };
      el("bc-tab-all").onclick = function () { bcTab = "all"; bcPage = 0; viewBadcases(); };
      el("bc-tab-regression").onclick = function () { bcTab = "regression"; bcPage = 0; viewBadcases(); };
      loadBadcases();
    }).catch(function (e) { renderErr(e.message); });
  }

  function loadBadcases() {
    // 回归测试集 tab：调用专用 API，不分页
    if (bcTab === "regression") {
      api("/api/regression-badcases").then(function (d) {
        var list = el("bc-list");
        var stats = el("bc-stats");
        if (stats) {
          stats.innerHTML = '<div class="bc-stat-row"><span class="bc-stat"><b>' + d.total + '</b> 回归用例</span><span class="bc-stat" style="color:#16a34a;">这些 badcase 已转化为评测任务，可在任务管理中运行回归测试</span></div>';
        }
        if (!d.items || !d.items.length) { list.innerHTML = '<div class="empty">暂无回归用例。在 badcase 详情页点击「转化为评测用例」即可生成回归任务。</div>'; return; }
        var rows = d.items.map(function (b) {
          var catLabel = BC_CATEGORY_LABELS[b.category] || b.category;
          return '<tr class="clickable" data-bid="' + esc(b.id) + '">' +
            "<td>" + esc(fmtTime(b.created_at)) + "</td>" +
            '<td><span class="badge" style="background:' + BC_SEVERITY_COLORS[b.severity] + ';color:#fff">' + esc(b.severity) + "</span></td>" +
            "<td><b>" + esc(b.title) + "</b></td>" +
            '<td><a href="#/tasks" style="color:#2563eb;text-decoration:underline;">' + esc(b.regression_task_id) + '</a></td>' +
            "<td>" + esc(b.task_id) + "</td><td>" + esc(b.agent_id) + "</td>" +
            "<td>" + esc(catLabel) + "</td>" +
            "</tr>";
        }).join("");
        list.innerHTML = '<table><tr><th>创建时间</th><th>严重度</th><th>标题</th><th>回归任务</th><th>原始任务</th><th>后端</th><th>分类</th></tr>' + rows + "</table>";
        list.querySelectorAll("tr.clickable").forEach(function (tr) {
          tr.onclick = function () { location.hash = "#/badcase/" + tr.getAttribute("data-bid"); };
        });
      }).catch(function (e) { el("bc-list").innerHTML = '<div class="err-banner">' + esc(e.message) + "</div>"; });
      return;
    }
    // 全部 badcase tab：原有逻辑
    var q = ["page=" + (bcPage + 1), "page_size=" + bcLimit];
    var tv = el("bc-task") && el("bc-task").value;
    var av = el("bc-agent") && el("bc-agent").value;
    var cv = el("bc-category") && el("bc-category").value;
    var sv = el("bc-severity") && el("bc-severity").value;
    var stv = el("bc-status") && el("bc-status").value;
    if (tv) q.push("task_id=" + encodeURIComponent(tv));
    if (av) q.push("agent_id=" + encodeURIComponent(av));
    if (cv) q.push("category=" + encodeURIComponent(cv));
    if (sv) q.push("severity=" + encodeURIComponent(sv));
    if (stv) q.push("status=" + encodeURIComponent(stv));
    api("/api/badcases?" + q.join("&")).then(function (d) {
      var list = el("bc-list");
      var stats = el("bc-stats");
      if (stats && d.stats) {
        var s = d.stats;
        var statHtml = '<div class="bc-stat-row">';
        statHtml += '<span class="bc-stat"><b>' + s.total + '</b> 总计</span>';
        Object.keys(BC_STATUS_LABELS).forEach(function (k) {
          statHtml += '<span class="bc-stat" style="color:' + BC_STATUS_COLORS[k] + '"><b>' + (s.by_status[k] || 0) + '</b> ' + BC_STATUS_LABELS[k] + '</span>';
        });
        Object.keys(BC_SEVERITY_COLORS).forEach(function (k) {
          statHtml += '<span class="bc-stat" style="color:' + BC_SEVERITY_COLORS[k] + '"><b>' + (s.by_severity[k] || 0) + '</b> ' + k + '</span>';
        });
        statHtml += '</div>';
        stats.innerHTML = statHtml;
      }
      if (!d.items || !d.items.length) { list.innerHTML = '<div class="empty">暂无 badcase 记录。可在运行详情页点击「标记为 badcase」导入。</div>'; return; }
      var rows = d.items.map(function (b) {
        var catLabel = BC_CATEGORY_LABELS[b.category] || b.category;
        var stLabel = BC_STATUS_LABELS[b.status] || b.status;
        return '<tr class="clickable" data-bid="' + esc(b.id) + '">' +
          "<td>" + esc(fmtTime(b.created_at)) + "</td>" +
          '<td><span class="badge" style="background:' + BC_SEVERITY_COLORS[b.severity] + ';color:#fff">' + esc(b.severity) + "</span></td>" +
          "<td><b>" + esc(b.title) + "</b></td>" +
          "<td>" + esc(b.task_id) + "</td><td>" + esc(b.agent_id) + "</td>" +
          "<td>" + esc(catLabel) + "</td>" +
          '<td><span class="badge" style="background:' + BC_STATUS_COLORS[b.status] + ';color:#fff">' + esc(stLabel) + "</span></td>" +
          "</tr>";
      }).join("");
      var total = Number(d.total || 0);
      var pages = Math.max(1, d.total_pages || Math.ceil(total / bcLimit));
      var cur = Math.min(bcPage + 1, pages);
      var pager = '<div class="pager">' +
        '<button class="btn secondary small" id="bc-prev"' + (bcPage <= 0 ? " disabled" : "") + '>‹ 上一页</button>' +
        '<span class="pager-info">第 ' + cur + " / " + pages + " 页 · 共 " + total + " 条</span>" +
        '<button class="btn secondary small" id="bc-next"' + (bcPage >= pages - 1 ? " disabled" : "") + '>下一页 ›</button>' +
        "</div>";
      list.innerHTML = '<table><tr><th>创建时间</th><th>严重度</th><th>标题</th><th>任务</th><th>后端</th><th>分类</th><th>状态</th></tr>' + rows + "</table>" + pager;
      list.querySelectorAll("tr.clickable").forEach(function (tr) {
        tr.onclick = function () { location.hash = "#/badcase/" + tr.getAttribute("data-bid"); };
      });
      var prevBtn = el("bc-prev"), nextBtn = el("bc-next");
      if (prevBtn) prevBtn.onclick = function () { if (bcPage > 0) { bcPage--; loadBadcases(); } };
      if (nextBtn) nextBtn.onclick = function () { if (bcPage < pages - 1) { bcPage++; loadBadcases(); } };
    }).catch(function (e) { el("bc-list").innerHTML = '<div class="err-banner">' + esc(e.message) + "</div>"; });
  }

  function viewBadcaseDetail(bid) {
    api("/api/badcases/" + bid).then(function (b) {
      var catOpts = Object.keys(BC_CATEGORY_LABELS).map(function (k) {
        return '<option value="' + k + '"' + (k === b.category ? " selected" : "") + '>' + BC_CATEGORY_LABELS[k] + "</option>";
      }).join("");
      var sevOpts = ["P0", "P1", "P2", "P3"].map(function (s) {
        return '<option value="' + s + '"' + (s === b.severity ? " selected" : "") + '>' + s + "</option>";
      }).join("");
      var stOpts = Object.keys(BC_STATUS_LABELS).map(function (k) {
        return '<option value="' + k + '"' + (k === b.status ? " selected" : "") + '>' + BC_STATUS_LABELS[k] + "</option>";
      }).join("");
      var runLink = b.run_id ? '<a href="#/run/' + esc(b.run_id) + '" class="btn secondary small">查看关联运行 →</a>' : "";
      var runSummary = b.run_summary ? (
        '<div class="bc-run-summary"><b>关联运行摘要：</b> status=' + esc(b.run_summary.status) +
        ', score=' + esc(b.run_summary.score) + ', pass_rate=' + esc(b.run_summary.pass_rate) +
        ', duration=' + fmtDur(b.run_summary.duration_s) + ', steps=' + esc(b.run_summary.steps) + '</div>'
      ) : "";
      // 回归用例关联信息
      var regressionInfo = "";
      if (b.regression_task_id) {
        regressionInfo = '<div class="bc-run-summary" style="background:#ECFDF5;color:#065F46;"><b>✓ 已转化为回归评测用例：</b> <a href="#/tasks" style="color:#065F46;text-decoration:underline;">' + esc(b.regression_task_id) + '</a>（可在任务管理中查看和运行）</div>';
      }
      var convertBtn = b.regression_task_id ? "" : '<button class="btn secondary" id="bc-convert">转化为评测用例</button>';
      var memoryBtn = '<button class="btn secondary" id="bc-to-memory">转化为经验记忆</button>';
      renderHTML(
        '<h2 class="page-title">Badcase 详情 · ' + esc(b.id) + '</h2>' +
        '<div class="card">' +
          '<div class="form-row">' +
            '<div class="field" style="flex:2"><label>标题</label><input id="bc-title" value="' + esc(b.title) + '"></div>' +
            '<div class="field"><label>严重度</label><select id="bc-severity">' + sevOpts + "</select></div>" +
            '<div class="field"><label>状态</label><select id="bc-status">' + stOpts + "</select></div>" +
          "</div>" +
          '<div class="form-row">' +
            '<div class="field"><label>任务</label><input id="bc-task" value="' + esc(b.task_id) + '"></div>' +
            '<div class="field"><label>后端</label><input id="bc-agent" value="' + esc(b.agent_id) + '"></div>' +
            '<div class="field"><label>分类</label><select id="bc-category">' + catOpts + "</select></div>" +
          "</div>" +
          '<div class="field"><label>问题描述</label><textarea id="bc-desc" rows="4">' + esc(b.description) + "</textarea></div>" +
          '<div class="field"><label>根因分析</label><textarea id="bc-root" rows="3" placeholder="分析 badcase 的根本原因...">' + esc(b.root_cause || "") + "</textarea></div>" +
          '<div class="field"><label>修复方案</label><textarea id="bc-fix" rows="3" placeholder="记录修复方案或改进措施...">' + esc(b.fix_plan || "") + "</textarea></div>" +
          runSummary +
          regressionInfo +
          '<div class="form-row" style="margin-top:16px">' +
            '<button class="btn" id="bc-save">保存修改</button>' +
            convertBtn +
            memoryBtn +
            runLink +
            '<button class="btn danger" id="bc-delete" style="margin-left:auto">删除</button>' +
          "</div>" +
          '<div id="bc-msg"></div>' +
          // 转化对话框
          '<div id="bc-convert-modal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:1000;display:flex;align-items:center;justify-content:center;">' +
            '<div style="background:#fff;padding:24px;border-radius:8px;width:560px;max-width:90vw;">' +
              '<h3 style="margin-top:0;">转化为回归评测用例</h3>' +
              '<div class="field"><label>新任务 ID（如 T-REG-001）</label><input id="bc-new-task-id" placeholder="T-REG-001"></div>' +
              '<div class="field"><label>任务标题</label><input id="bc-new-title" value="[回归] ' + esc(b.title) + '"></div>' +
              '<div class="field"><label>任务描述（可编辑）</label><textarea id="bc-new-desc" rows="6"></textarea></div>' +
              '<div class="field" style="display:flex;align-items:center;gap:8px;"><input type="checkbox" id="bc-add-manifest" checked><label for="bc-add-manifest" style="margin:0;">自动加入 manifest.yaml</label></div>' +
              '<div style="margin-top:16px;display:flex;gap:8px;justify-content:flex-end;">' +
                '<button class="btn secondary" id="bc-convert-cancel">取消</button>' +
                '<button class="btn" id="bc-convert-confirm">确认转化</button>' +
              "</div>" +
            "</div>" +
          "</div>" +
        "</div>"
      );
      el("bc-save").onclick = function () { saveBadcase(bid); };
      el("bc-delete").onclick = function () {
        if (confirm("确定删除这条 badcase？")) {
          api("/api/badcases/" + bid, { method: "DELETE" }).then(function () {
            location.hash = "#/badcases";
          }).catch(function (e) { el("bc-msg").innerHTML = '<div class="err-banner">' + esc(e.message) + "</div>"; });
        }
      };
      // 转化为评测用例
      var convertBtnEl = el("bc-convert");
      if (convertBtnEl) {
        // 预填充描述
        var descLines = [
          "【回归测试用例】由 badcase " + bid + " 转化生成",
          "原始任务: " + b.task_id + " × " + b.agent_id,
          "问题分类: " + (BC_CATEGORY_LABELS[b.category] || b.category),
          "严重程度: " + b.severity,
          "",
          "【问题描述】",
          b.description,
        ];
        if (b.root_cause) descLines.push("", "【根因分析】", b.root_cause);
        if (b.fix_plan) descLines.push("", "【修复方案】", b.fix_plan);
        descLines.push("", "【评测要求】", "Agent 必须正确处理此场景，避免复现上述问题。", "请将最终结果写入 output/result.md 文件。");
        el("bc-new-desc").value = descLines.join("\n");

        convertBtnEl.onclick = function () {
          el("bc-convert-modal").style.display = "flex";
        };
        el("bc-convert-cancel").onclick = function () {
          el("bc-convert-modal").style.display = "none";
        };
        el("bc-convert-confirm").onclick = function () {
          var newTaskId = el("bc-new-task-id").value.trim();
          if (!newTaskId) { alert("请输入新任务 ID"); return; }
          var body = {
            new_task_id: newTaskId,
            title: el("bc-new-title").value,
            description: el("bc-new-desc").value,
            add_to_manifest: el("bc-add-manifest").checked,
          };
          el("bc-convert-confirm").disabled = true;
          el("bc-convert-confirm").textContent = "转化中...";
          api("/api/badcases/" + bid + "/convert-to-task", { method: "POST", body: body }).then(function (d) {
            el("bc-convert-modal").style.display = "none";
            el("bc-msg").innerHTML = '<div class="success-banner">✓ 已转化为回归评测用例 ' + esc(d.new_task_id) + '，页面即将刷新...</div>';
            setTimeout(function () { viewBadcaseDetail(bid); }, 1500);
          }).catch(function (e) {
            el("bc-convert-confirm").disabled = false;
            el("bc-convert-confirm").textContent = "确认转化";
            alert("转化失败: " + e.message);
          });
        };
      }

      // 转化为经验记忆
      var memoryBtnEl = el("bc-to-memory");
      if (memoryBtnEl) {
        memoryBtnEl.onclick = function () {
          if (!confirm("确定将此 badcase 转化为经验记忆？转化后将在后续类似任务中自动召回注入。")) return;
          var body = {
            badcase_id: bid,
            trigger_keywords: [b.task_id, b.category].filter(Boolean),
            task_tags: [],
            confidence: 0.7,
          };
          api("/api/memories/from-badcase", { method: "POST", body: body }).then(function (d) {
            el("bc-msg").innerHTML = '<div class="success-banner">✓ 已转化为经验记忆 ' + esc(d.id) + '，<a href="#/memory/' + esc(d.id) + '" style="color:#065F46;text-decoration:underline;">查看详情 →</a></div>';
          }).catch(function (e) {
            alert("转化失败: " + e.message);
          });
        };
      }
    }).catch(function (e) { renderErr(e.message); });
  }

  function saveBadcase(bid) {
    var body = {
      title: el("bc-title").value,
      task_id: el("bc-task").value,
      agent_id: el("bc-agent").value,
      category: el("bc-category").value,
      severity: el("bc-severity").value,
      status: el("bc-status").value,
      description: el("bc-desc").value,
      root_cause: el("bc-root").value,
      fix_plan: el("bc-fix").value,
    };
    api("/api/badcases/" + bid, { method: "PUT", body: body }).then(function () {
      el("bc-msg").innerHTML = '<div class="success-banner">已保存 ✓</div>';
      setTimeout(function () { el("bc-msg").innerHTML = ""; }, 2000);
    }).catch(function (e) { el("bc-msg").innerHTML = '<div class="err-banner">' + esc(e.message) + "</div>"; });
  }

  // ---------- 视图：经验记忆（V2.9） ----------
  var memPage = 0, memLimit = 20;

  function viewMemories() {
    renderHTML(
      '<h2 class="page-title">经验记忆</h2>' +
      '<div class="card">' +
        '<div class="form-row">' +
          '<div class="field"><label>状态</label><select id="mem-status"><option value="">全部</option><option value="active">生效中</option><option value="inactive">已停用</option></select></div>' +
          '<div class="field" style="flex:2"><label>关键词</label><input id="mem-keyword" placeholder="搜索标题或内容..."></div>' +
          '<div class="field" style="flex:0 0 100px;"><label>&nbsp;</label><button class="btn secondary" id="mem-filter">筛选</button></div>' +
          '<div class="field" style="flex:0 0 120px;"><label>&nbsp;</label><button class="btn" id="mem-auto-manage" style="background:#7c3aed;">自动治理</button></div>' +
          '<div class="field" style="flex:0 0 120px;"><label>&nbsp;</label><button class="btn secondary" id="mem-test-recall">测试召回</button></div>' +
        "</div>" +
        '<div id="mem-stats" class="bc-stats"></div>' +
        '<div id="mem-quality" class="bc-stats" style="margin-top:8px;"></div>' +
        '<div id="mem-list"><div class="empty">加载中…</div></div>' +
      "</div>"
    );
    el("mem-filter").onclick = function () { memPage = 0; loadMemories(); };
    el("mem-auto-manage").onclick = function () {
      if (!confirm("执行记忆自动治理？\n\n策略：\n1. 低成功率(<30%)且使用≥3次 → 自动停用\n2. 高成功率(≥80%)且使用≥5次 → 提升置信度到0.9\n3. 超过30天未使用 → 降低置信度到0.3")) return;
      api("/api/memory-auto-manage", { method: "POST", body: {} }).then(function (r) {
        alert("自动治理完成：\n- 评估记忆: " + r.total_evaluated + " 条\n- 自动停用: " + r.deactivated + " 条\n- 置信度提升: " + r.promoted + " 条\n- 置信度降级: " + r.demoted + " 条");
        loadMemories();
        loadMemoryQuality();
      }).catch(function (e) { alert("治理失败: " + e.message); });
    };
    el("mem-test-recall").onclick = function () { showMemoryRecallTest(); };
    loadMemories();
    loadMemoryQuality();
  }

  function showMemoryRecallTest() {
    var html = '<div class="modal-overlay" id="recall-modal">' +
      '<div class="modal" style="max-width:700px;">' +
        '<h3>记忆召回测试</h3>' +
        '<p style="color:#6b7280;font-size:13px;">输入任务特征，查看会召回哪些记忆及匹配原因（可解释性）</p>' +
        '<div class="form-row">' +
          '<div class="field" style="flex:2"><label>任务标签（逗号分隔）</label><input id="rt-tags" placeholder="file, text, reasoning"></div>' +
          '<div class="field" style="flex:2"><label>关键词（逗号分隔）</label><input id="rt-keywords" placeholder="日期, 格式, 解析"></div>' +
          '<div class="field" style="flex:0 0 80px;"><label>数量</label><input id="rt-limit" type="number" value="5" min="1" max="20"></div>' +
        '</div>' +
        '<div style="margin:12px 0;"><button class="btn" id="rt-run">执行召回测试</button> <button class="btn secondary" id="rt-close">关闭</button></div>' +
        '<div id="rt-results" style="max-height:400px;overflow-y:auto;"></div>' +
      '</div></div>';
    document.body.insertAdjacentHTML("beforeend", html);
    el("rt-close").onclick = function () { document.getElementById("recall-modal").remove(); };
    el("rt-run").onclick = function () {
      var tags = el("rt-tags").value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
      var keywords = el("rt-keywords").value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
      var limit = parseInt(el("rt-limit").value) || 5;
      api("/api/memories/recall", { method: "POST", body: { task_tags: tags, keywords: keywords, limit: limit } }).then(function (d) {
        var results = el("rt-results");
        if (!d.items || d.items.length === 0) {
          results.innerHTML = '<div class="empty">未召回任何记忆</div>';
          return;
        }
        var html = '<p style="color:#6b7280;font-size:13px;margin-bottom:8px;">召回 ' + d.items.length + ' 条记忆（按相关性降序）</p>';
        d.items.forEach(function (m, idx) {
          var scorePct = Math.round(m._relevance_score * 100);
          var reasonsHtml = (m.match_reasons || []).map(function (r) { return '<li style="font-size:12px;color:#6b7280;">' + r + '</li>'; }).join("");
          var tagsHtml = (m.matched_tags || []).map(function (t) { return '<span class="tag" style="background:#dbeafe;color:#1e40af;">' + t + '</span>'; }).join(" ");
          var kwHtml = (m.matched_keywords || []).map(function (k) { return '<span class="tag" style="background:#fef3c7;color:#92400e;">' + k + '</span>'; }).join(" ");
          html += '<div class="card" style="margin-bottom:10px;padding:12px;">' +
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">' +
              '<span style="font-weight:600;">#' + (idx + 1) + ' ' + m.title + '</span>' +
              '<span style="font-size:14px;font-weight:700;color:#7c3aed;">' + scorePct + '分</span>' +
            '</div>' +
            '<div style="margin-bottom:6px;">' + (tagsHtml || '<span style="color:#9ca3af;font-size:12px;">无标签匹配</span>') + ' ' + (kwHtml || '<span style="color:#9ca3af;font-size:12px;">无关键词匹配</span>') + '</div>' +
            '<details><summary style="cursor:pointer;font-size:12px;color:#6b7280;">查看匹配分数计算</summary><ul style="margin-top:6px;padding-left:16px;">' + reasonsHtml + '</ul></details>' +
          '</div>';
        });
        results.innerHTML = html;
      }).catch(function (e) { el("rt-results").innerHTML = '<div class="empty" style="color:#dc2626;">召回失败: ' + e.message + '</div>'; });
    };
  }

  function loadMemoryQuality() {
    api("/api/memory-quality-stats").then(function (s) {
      var q = el("mem-quality");
      if (!q) return;
      var successRatePct = Math.round(s.avg_success_rate * 100);
      var confPct = Math.round(s.avg_confidence * 100);
      q.innerHTML = '<div class="bc-stat-row">' +
        '<span class="bc-stat" style="color:#7c3aed;"><b>' + confPct + '%</b> 平均置信度</span>' +
        '<span class="bc-stat" style="color:#16a34a;"><b>' + successRatePct + '%</b> 平均成功率</span>' +
        '<span class="bc-stat" style="color:#dc2626;"><b>' + s.low_quality + '</b> 低质量记忆</span>' +
        '<span class="bc-stat" style="color:#ca8a04;"><b>' + s.unused_over_7d + '</b> 超7天未使用</span>' +
        '<span class="bc-stat"><b>' + s.total_usage + '</b> 累计使用次数</span>' +
        '</div>';
    }).catch(function () {});
  }

  function loadMemories() {
    var q = ["page=" + (memPage + 1), "page_size=" + memLimit];
    var sv = el("mem-status") && el("mem-status").value;
    var kv = el("mem-keyword") && el("mem-keyword").value;
    if (sv) q.push("status=" + encodeURIComponent(sv));
    if (kv) q.push("keyword=" + encodeURIComponent(kv));
    api("/api/memories?" + q.join("&")).then(function (d) {
      var list = el("mem-list");
      var stats = el("mem-stats");
      if (stats) {
        var active = d.items.filter(function (m) { return m.status === "active"; }).length;
        var inactive = d.total - active;
        stats.innerHTML = '<div class="bc-stat-row"><span class="bc-stat"><b>' + d.total + '</b> 总记忆</span><span class="bc-stat" style="color:#16a34a;"><b>' + active + '</b> 生效中</span><span class="bc-stat" style="color:#6b7280;"><b>' + inactive + '</b> 已停用</span></div>';
      }
      if (!d.items || !d.items.length) { list.innerHTML = '<div class="empty">暂无经验记忆。可在 badcase 详情页点击「转化为经验记忆」生成。</div>'; return; }
      var rows = d.items.map(function (m) {
        var confPct = Math.round(m.confidence * 100);
        var successRate = m.usage_count > 0 ? Math.round(m.success_count / m.usage_count * 100) : "-";
        var statusColor = m.status === "active" ? "#16a34a" : "#9ca3af";
        var statusLabel = m.status === "active" ? "生效中" : "已停用";
        return '<tr class="clickable" data-mid="' + esc(m.id) + '">' +
          "<td>" + esc(fmtTime(m.created_at)) + "</td>" +
          '<td><b>' + esc(m.title) + "</b><br><span style='color:#6b7280;font-size:12px;'>" + esc(m.content.substring(0, 80)) + "...</span></td>" +
          "<td>" + confPct + "%</td>" +
          "<td>" + successRate + (m.usage_count > 0 ? " (" + m.usage_count + "次)" : "") + "</td>" +
          '<td><span class="badge" style="background:' + statusColor + ';color:#fff">' + statusLabel + "</span></td>" +
          "</tr>";
      }).join("");
      var total = Number(d.total || 0);
      var pages = Math.max(1, d.total_pages || Math.ceil(total / memLimit));
      var cur = Math.min(memPage + 1, pages);
      var pager = '<div class="pager">' +
        '<button class="btn secondary small" id="mem-prev"' + (memPage <= 0 ? " disabled" : "") + '>‹ 上一页</button>' +
        '<span class="pager-info">第 ' + cur + " / " + pages + " 页 · 共 " + total + " 条</span>" +
        '<button class="btn secondary small" id="mem-next"' + (memPage >= pages - 1 ? " disabled" : "") + '>下一页 ›</button>' +
        "</div>";
      list.innerHTML = '<table><tr><th>创建时间</th><th>标题/内容</th><th>置信度</th><th>成功率</th><th>状态</th></tr>' + rows + "</table>" + pager;
      list.querySelectorAll("tr.clickable").forEach(function (tr) {
        tr.onclick = function () { location.hash = "#/memory/" + tr.getAttribute("data-mid"); };
      });
      var prevBtn = el("mem-prev"), nextBtn = el("mem-next");
      if (prevBtn) prevBtn.onclick = function () { if (memPage > 0) { memPage--; loadMemories(); } };
      if (nextBtn) nextBtn.onclick = function () { if (memPage < pages - 1) { memPage++; loadMemories(); } };
    }).catch(function (e) { el("mem-list").innerHTML = '<div class="err-banner">' + esc(e.message) + "</div>"; });
  }

  function viewMemoryDetail(mid) {
    api("/api/memories/" + mid).then(function (m) {
      var stOpts = '<option value="active"' + (m.status === "active" ? " selected" : "") + '>生效中</option><option value="inactive"' + (m.status === "inactive" ? " selected" : "") + '>已停用</option>';
      var sourceLink = m.source_badcase_id ? '<a href="#/badcase/' + esc(m.source_badcase_id) + '" class="btn secondary small">查看来源 badcase →</a>' : "";
      var successRate = m.usage_count > 0 ? Math.round(m.success_count / m.usage_count * 100) + "%" : "无数据";
      renderHTML(
        '<h2 class="page-title">经验记忆详情 · ' + esc(m.id) + '</h2>' +
        '<div class="card">' +
          '<div class="form-row">' +
            '<div class="field" style="flex:2"><label>标题</label><input id="mem-title" value="' + esc(m.title) + '"></div>' +
            '<div class="field"><label>置信度 (0-1)</label><input id="mem-conf" type="number" step="0.1" min="0" max="1" value="' + m.confidence + '"></div>' +
            '<div class="field"><label>状态</label><select id="mem-status">' + stOpts + "</select></div>" +
          "</div>" +
          '<div class="field"><label>记忆内容</label><textarea id="mem-content" rows="10">' + esc(m.content) + "</textarea></div>" +
          '<div class="form-row">' +
            '<div class="field"><label>触发关键词（逗号分隔）</label><input id="mem-keywords" value="' + esc(m.trigger_keywords.join(", ")) + '"></div>' +
            '<div class="field"><label>适用任务标签（逗号分隔）</label><input id="mem-tags" value="' + esc(m.task_tags.join(", ")) + '"></div>' +
          "</div>" +
          '<div class="bc-run-summary"><b>使用统计：</b> 共使用 ' + m.usage_count + ' 次，成功 ' + m.success_count + ' 次，成功率 ' + successRate + '</div>' +
          (m.source_badcase_id ? '<div class="bc-run-summary"><b>来源：</b> badcase ' + esc(m.source_badcase_id) + '</div>' : "") +
          '<div class="form-row" style="margin-top:16px">' +
            '<button class="btn" id="mem-save">保存修改</button>' +
            sourceLink +
            '<button class="btn danger" id="mem-delete" style="margin-left:auto">删除</button>' +
          "</div>" +
          '<div id="mem-msg"></div>' +
        "</div>"
      );
      el("mem-save").onclick = function () { saveMemory(mid); };
      el("mem-delete").onclick = function () {
        if (confirm("确定删除这条记忆？")) {
          api("/api/memories/" + mid, { method: "DELETE" }).then(function () {
            location.hash = "#/memories";
          }).catch(function (e) { el("mem-msg").innerHTML = '<div class="err-banner">' + esc(e.message) + "</div>"; });
        }
      };
    }).catch(function (e) { renderErr(e.message); });
  }

  function saveMemory(mid) {
    var keywords = el("mem-keywords").value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    var tags = el("mem-tags").value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    var body = {
      title: el("mem-title").value,
      content: el("mem-content").value,
      trigger_keywords: keywords,
      task_tags: tags,
      confidence: parseFloat(el("mem-conf").value) || 0.5,
      status: el("mem-status").value,
    };
    api("/api/memories/" + mid, { method: "PUT", body: body }).then(function () {
      el("mem-msg").innerHTML = '<div class="success-banner">已保存 ✓</div>';
      setTimeout(function () { el("mem-msg").innerHTML = ""; }, 2000);
    }).catch(function (e) { el("mem-msg").innerHTML = '<div class="err-banner">' + esc(e.message) + "</div>"; });
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
        var obsTxt = strOf(s.observation);
        var obsHtml = "";
        if (obsTxt) {
          obsHtml = '<div class="muted">→ ' + esc(clip(obsTxt, 160)) + "</div>";
          if (obsTxt.indexOf("\uFFFD") >= 0) {
            obsHtml = '<span class="enc-bad" title="该步骤输出含编码损坏字符（历史数据：旧版本按 UTF-8 硬解 GBK 输出所致）；已修复，重新运行任务即可正常显示">编码损坏 ⚠</span> ' + obsHtml;
          }
        }
        return '<div class="step-item"><b>#' + (i + 1) + "</b> " + esc(s.action || s.step || "") +
          (s.args ? " <code>" + esc(clip(strOf(s.args), 120)) + "</code>" : "") +
          obsHtml +
          "</div>";
      }).join("") || '<div class="muted">（黑盒后端无可视化轨迹）</div>';
      var sc = (r.metrics && r.metrics.score) || 0;
      var wt = (r.metrics && r.metrics.weight) || 0;
      var conf = (r.metrics && r.metrics.confidence) || null;
      var confHtml = "";
      if (conf) {
        var confColor = conf.level === "high" ? "#22c55e" : conf.level === "medium" ? "#f59e0b" : "#ef4444";
        var confLabel = conf.level === "high" ? "高置信" : conf.level === "medium" ? "中置信" : "低置信";
        confHtml = '<div class="kpi"><b style="color:' + confColor + '">' + conf.score.toFixed(1) + ' / ' + confLabel +
          '</b><span>评测置信度 <span class="info-icon" title="' + esc(conf.suggestions.join("; ")) + '">ⓘ</span></span></div>';
      }
      renderHTML(
        '<h2 class="page-title">运行详情 ' +
          '<a class="btn secondary" style="float:right;margin-left:8px;" href="#/history">← 返回历史</a>' +
          '<button class="btn secondary" id="btn-mark-badcase" style="float:right;">标记为 badcase</button>' +
        '</h2>' +
        '<div class="kpi-row">' +
          '<div class="kpi"><b>' + esc(r.run_id) + "</b><span>run_id</span></div>" +
          '<div class="kpi"><b>' + esc(r.task_id) + " / " + esc(r.agent_id) + "</b><span>任务 / 后端</span></div>" +
          '<div class="kpi"><b>' + sc.toFixed(3) + " / " + esc(wt) + "</b><span>score / 权重</span></div>" +
          '<div class="kpi"><b>' + fmtDur(r.duration_s) + "</b><span>时长</span></div>" +
          confHtml +
        "</div>" +
        (r.error ? '<div class="err-banner">' + esc(r.error) + "</div>" : "") +
        '<div class="card"><h3>判定结果（' + v.length + " 个校验点）</h3>" + (vRows || '<div class="empty">无判定</div>') + "</div>" +
        executionAnalysis(r) +
        '<div class="card"><h3>轨迹回放 <span class="tl-note">输入意图 → 知识/检索 → 模型生成 → 工具执行</span></h3>' + traceTimeline(r.traces, r) + "</div>" +
        '<div class="card"><h3>执行轨迹（' + (r.steps || []).length + " 步）</h3>" + steps + "</div>" +
        '<div class="card"><h3>产物文件</h3><div id="file-list"><div class="empty">加载中…</div></div></div>' +
        '<div class="card" id="file-view" style="display:none;"><h3>文件预览</h3><pre class="code" id="file-content"></pre></div>'
      );
      loadFiles(runId);
      var markBtn = el("btn-mark-badcase");
      if (markBtn) {
        markBtn.onclick = function () {
          markBtn.disabled = true;
          markBtn.textContent = "导入中…";
          api("/api/badcases/import-from-run", { method: "POST", body: { run_id: runId } }).then(function (d) {
            markBtn.textContent = "✓ 已标记";
            setTimeout(function () {
              location.hash = "#/badcase/" + d.id;
            }, 800);
          }).catch(function (e) {
            markBtn.disabled = false;
            markBtn.textContent = "标记为 badcase";
            alert("标记失败: " + e.message);
          });
        };
      }
    }).catch(function (e) { renderErr(e.message); });
  }

  // ---------- 执行分析：组件实际执行率 ----------
  function executionAnalysis(run) {
    var traces = run.traces || [];
    var steps = run.steps || [];
    // 轨迹事件类型分布
    var kindDist = {};
    var phaseDist = {};
    var toolDist = {};
    for (var i = 0; i < traces.length; i++) {
      var t = traces[i] || {};
      var k = t.kind || "unknown";
      kindDist[k] = (kindDist[k] || 0) + 1;
      if (t.phase) { phaseDist[t.phase] = (phaseDist[t.phase] || 0) + 1; }
      if (t.tool) { toolDist[t.tool] = (toolDist[t.tool] || 0) + 1; }
    }
    // 步骤 action 分布
    var stepDist = {};
    for (var j = 0; j < steps.length; j++) {
      var s = steps[j] || {};
      var a = s.action || "unknown";
      stepDist[a] = (stepDist[a] || 0) + 1;
    }
    // 模型调用统计
    var llmCount = kindDist["llm"] || 0;
    var toolCount = kindDist["tool"] || 0;
    var reasoningCount = phaseDist["reasoning"] || 0;
    var decisionCount = phaseDist["decision"] || 0;
    var finalCount = phaseDist["final"] || 0;
    // token 统计
    var usage = (run.metrics && run.metrics.usage) || {};
    var promptTokens = usage.prompt_tokens || 0;
    var completionTokens = usage.completion_tokens || 0;

    // 工具使用质量统计
    var toolCalls = 0;
    var toolFailures = 0;
    var toolRetries = 0;
    var paramErrors = 0;
    var lastAction = null;
    var lastArgs = null;
    var failureKeywords = ["error", "fail", "失败", "异常", "traceback", "not found", "no such", "permission denied", "invalid"];
    var paramKeywords = ["invalid argument", "参数", "usage:", "用法", "expected", "required", "missing"];
    for (var si = 0; si < steps.length; si++) {
      var st = steps[si] || {};
      var act = st.action || "";
      var obs = (st.observation || "") + " " + (st.result || "") + " " + (st.error || "");
      var obsLower = obs.toLowerCase();
      if (act && act !== "finish" && act !== "unknown" && act !== "think") {
        toolCalls++;
        // 检测失败
        var isFailure = failureKeywords.some(function(kw) { return obsLower.indexOf(kw) >= 0; });
        if (isFailure) {
          toolFailures++;
          // 检测参数错误
          var isParamError = paramKeywords.some(function(kw) { return obsLower.indexOf(kw) >= 0; });
          if (isParamError) paramErrors++;
        }
        // 检测重试（同一 action 连续出现）
        if (lastAction === act) {
          toolRetries++;
        }
        lastAction = act;
      }
    }
    var toolSuccessRate = toolCalls > 0 ? Math.round((1 - toolFailures / toolCalls) * 100) : 100;
    var toolRetryRate = toolCalls > 0 ? Math.round(toolRetries / toolCalls * 100) : 0;
    var paramErrorRate = toolCalls > 0 ? Math.round(paramErrors / toolCalls * 100) : 0;

    // 工具分布条形图
    var toolRows = [];
    var toolKeys = Object.keys(toolDist).sort(function(a, b) { return toolDist[b] - toolDist[a]; });
    var maxTool = toolKeys.length ? toolDist[toolKeys[0]] : 1;
    for (var ti = 0; ti < toolKeys.length; ti++) {
      var tk = toolKeys[ti];
      var pct = Math.round(toolDist[tk] / maxTool * 100);
      toolRows.push(
        '<div class="ea-tool-row">' +
          '<span class="ea-tool-name">' + esc(tk) + '</span>' +
          '<div class="ea-tool-bar"><div class="ea-tool-fill" style="width:' + pct + '%;"></div></div>' +
          '<span class="ea-tool-count">' + toolDist[tk] + '</span>' +
        '</div>'
      );
    }

    return (
      '<div class="card"><h3>执行分析 <span class="tl-note">组件实际执行率 · 基于真实运行轨迹</span></h3>' +
        '<div class="ea-grid">' +
          '<div class="ea-col">' +
            '<div class="ea-subtitle">调用统计</div>' +
            '<div class="ea-stats">' +
              '<div class="ea-stat"><b>' + llmCount + '</b><span>模型调用</span></div>' +
              '<div class="ea-stat"><b>' + toolCount + '</b><span>工具执行</span></div>' +
              '<div class="ea-stat"><b>' + steps.length + '</b><span>执行步骤</span></div>' +
              '<div class="ea-stat"><b>' + (promptTokens + completionTokens) + '</b><span>总 tokens</span></div>' +
            '</div>' +
            '<div class="ea-subtitle" style="margin-top:14px;">模型阶段分布</div>' +
            '<div class="ea-phases">' +
              (reasoningCount ? '<span class="ea-phase ea-phase-reason">推理 ' + reasoningCount + '</span>' : '') +
              (decisionCount ? '<span class="ea-phase ea-phase-decision">决策 ' + decisionCount + '</span>' : '') +
              (finalCount ? '<span class="ea-phase ea-phase-final">最终 ' + finalCount + '</span>' : '') +
              (!reasoningCount && !decisionCount && !finalCount ? '<span class="muted">无阶段数据</span>' : '') +
            '</div>' +
          '</div>' +
          '<div class="ea-col">' +
            '<div class="ea-subtitle">工具调用分布</div>' +
            (toolRows.length ? toolRows.join("") : '<div class="muted">本次运行无工具调用</div>') +
            '<div class="ea-subtitle" style="margin-top:14px;">工具使用质量</div>' +
            '<div class="ea-quality">' +
              '<div class="ea-q-row"><span class="ea-q-label">调用成功率</span><div class="ea-q-bar"><div class="ea-q-fill ea-q-ok" style="width:' + toolSuccessRate + '%;"></div></div><span class="ea-q-val">' + toolSuccessRate + '%</span></div>' +
              '<div class="ea-q-row"><span class="ea-q-label">重试率</span><div class="ea-q-bar"><div class="ea-q-fill ea-q-warn" style="width:' + toolRetryRate + '%;"></div></div><span class="ea-q-val">' + toolRetryRate + '%</span></div>' +
              '<div class="ea-q-row"><span class="ea-q-label">参数错误率</span><div class="ea-q-bar"><div class="ea-q-fill ea-q-err" style="width:' + paramErrorRate + '%;"></div></div><span class="ea-q-val">' + paramErrorRate + '%</span></div>' +
              '<div class="ea-q-detail">总调用 ' + toolCalls + ' 次 · 失败 ' + toolFailures + ' 次 · 重试 ' + toolRetries + ' 次 · 参数错误 ' + paramErrors + ' 次</div>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>'
    );
  }

  // ---------- 轨迹回放：全链路时间线 ----------
  var TL_META = {
    intent:    { label: "输入意图", color: "#9BBBF4", badge: "意图" },
    retrieval: { label: "知识/检索", color: "#94D8C3", badge: "知识" },
    llm:       { label: "模型生成", color: "#C9A7E8", badge: "模型" },
    tool:      { label: "工具执行", color: "#F4B393", badge: "工具" }
  };
  function tlKind(t) {
    if (t.kind === "intent") return "intent";
    if (t.kind === "llm") return "llm";
    if (t.kind === "tool" && t.category === "retrieval") return "retrieval";
    return "tool";
  }
  function tlTitle(kind, t, i) {
    if (kind === "intent") return "任务意图";
    if (kind === "llm") return "模型生成 · " + (t.model || "llm");
    return (t.tool || t.action || "步骤" + (i + 1));
  }
  function tlBody(kind, t) {
    if (kind === "intent") return t.content || "";
    if (kind === "llm") {
      var out = t.output || t.response || "";
      var inp = t.input ? "输入：\n" + t.input + "\n\n" : "";
      var tk = t.tokens ? "\n\n[tokens " + (t.tokens.prompt_tokens || 0) + "/" + (t.tokens.completion_tokens || 0) + "]" : "";
      return inp + out + tk;
    }
    return t.observation || "";
  }
  function tlExtra(kind, t) {
    if (kind === "llm" && t.input) return "输入已记录";
    if (t.args) return strOf(t.args);
    return "";
  }
  function tlPad(n) { return n < 10 ? "0" + n : String(n); }
  // 时间戳 → 可读时间（HH:MM:SS）+ 相对首个有真实时间节点的耗时（+Xs）
  function fmtTs(ts, baseTs) {
    if (ts == null || ts <= 0) return "";
    var d = new Date(ts * 1000);
    var abs = tlPad(d.getHours()) + ":" + tlPad(d.getMinutes()) + ":" + tlPad(d.getSeconds());
    if (baseTs != null && ts >= baseTs) {
      var rel = ts - baseTs;
      return abs + "  +" + (rel < 60 ? rel.toFixed(1) : Math.round(rel)) + "s";
    }
    return abs;
  }
  // ---------- 六类 Harness 能力覆盖率统计 ----------
  // HarnessDev 论文归纳的六类控制能力：Execution/Tools/Context/State/Lifecycle/Verification
  var CAP_META = {
    execution:   { label: "执行循环", desc: "Agent Loop 推进次数（模型调用）", color: "#6366F1" },
    tools:       { label: "工具调用", desc: "工具选择与执行次数", color: "#F59E0B" },
    context:     { label: "上下文组织", desc: "任务/历史/约束的上下文注入", color: "#10B981" },
    state:       { label: "状态管理", desc: "进度/checkpoint/失败记录", color: "#EC4899" },
    lifecycle:   { label: "生命周期", desc: "超时/恢复/异常处理", color: "#8B5CF6" },
    verification:{ label: "结果验证", desc: "checkpoint 校验与完成判定", color: "#0EA5E9" }
  };
  function capabilityCoverage(traces, run) {
    var stats = {
      execution: { count: 0, triggered: false },
      tools: { count: 0, triggered: false },
      context: { count: 0, triggered: false },
      state: { count: 0, triggered: false },
      lifecycle: { count: 0, triggered: false },
      verification: { count: 0, triggered: false }
    };
    for (var i = 0; i < (traces || []).length; i++) {
      var t = traces[i] || {};
      if (t.kind === "llm") {
        stats.execution.count++;
        stats.execution.triggered = true;
        if (t.input) { stats.context.count++; stats.context.triggered = true; }
      }
      if (t.kind === "tool") {
        stats.tools.count++;
        stats.tools.triggered = true;
      }
      if (t.kind === "intent") {
        stats.context.count++;
        stats.context.triggered = true;
      }
      if (t.kind === "state" || t.phase === "checkpoint" || (t.tool && t.tool.indexOf("checkpoint") >= 0)) {
        stats.state.count++;
        stats.state.triggered = true;
      }
      if (t.kind === "lifecycle" || t.phase === "timeout" || t.phase === "retry" || t.phase === "error") {
        stats.lifecycle.count++;
        stats.lifecycle.triggered = true;
      }
      if (t.kind === "verification" || t.phase === "verify") {
        stats.verification.count++;
        stats.verification.triggered = true;
      }
    }
    // 从 run 元数据补充
    if (run) {
      var verdicts = run.verdicts || (run.metrics && run.metrics.verdicts) || [];
      if (verdicts.length) {
        stats.verification.count = Math.max(stats.verification.count, verdicts.length);
        stats.verification.triggered = true;
      }
      if (run.status === "timeout" || run.status === "error" || run.error) {
        stats.lifecycle.count++;
        stats.lifecycle.triggered = true;
      }
    }
    return stats;
  }
  function capabilityBar(stats) {
    var cells = [];
    var keys = ["execution", "tools", "context", "state", "lifecycle", "verification"];
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      var meta = CAP_META[k];
      var s = stats[k];
      var statusCls = s.triggered ? "cap-ok" : "cap-miss";
      var statusTxt = s.triggered ? "已触发" : "未触发";
      var countTxt = s.count > 0 ? s.count + " 次" : "—";
      cells.push(
        '<div class="cap-cell ' + statusCls + '" title="' + esc(meta.desc) + '">' +
          '<div class="cap-dot" style="background:' + (s.triggered ? meta.color : "#CBD5E1") + ';"></div>' +
          '<div class="cap-info">' +
            '<div class="cap-label">' + esc(meta.label) + '</div>' +
            '<div class="cap-count">' + countTxt + ' · ' + statusTxt + '</div>' +
          '</div>' +
        '</div>'
      );
    }
    var triggered = keys.filter(function(k) { return stats[k].triggered; }).length;
    return (
      '<div class="cap-section">' +
        '<div class="cap-header">' +
          '<b>Harness 能力覆盖率</b>' +
          '<span class="cap-summary">' + triggered + '/6 类能力在本次运行中实际触发</span>' +
          '<span class="cap-hint" title="基于 HarnessDev 论文的六类控制能力框架，从运行轨迹中自动推断。未触发不代表代码未实现，可能是本次任务未走到对应流程。">ⓘ 判定逻辑</span>' +
        '</div>' +
        '<div class="cap-grid">' + cells.join("") + '</div>' +
      '</div>'
    );
  }

  function traceTimeline(traces, run) {
    if (!traces || !traces.length) {
      return '<div class="empty">该 run 无轨迹回放数据（旧版本运行），重新运行任务可生成</div>';
    }
    // 基准时间：第一个有真实 epoch 秒时间戳的节点（intent 占位 ts=0 排除）
    var baseTs = null;
    for (var bi = 0; bi < traces.length; bi++) {
      var bts = traces[bi] && traces[bi].ts;
      if (bts && bts > 1e8) { baseTs = bts; break; }
    }
    var capHtml = capabilityBar(capabilityCoverage(traces, run));
    var chips = [];
    var keys = ["all", "intent", "retrieval", "llm", "tool"];
    for (var k = 0; k < keys.length; k++) {
      var key = keys[k];
      var label = key === "all" ? "全部" : TL_META[key].badge;
      chips.push(
        '<button class="tl-chip' + (key === "all" ? " active" : "") + '" data-f="' + key + '" onclick="tlFilter(this)">' +
        label + "</button>"
      );
    }
    var items = [];
    for (var i = 0; i < traces.length; i++) {
      var t = traces[i] || {};
      var kind = tlKind(t);
      var meta = TL_META[kind] || TL_META.tool;
      var body = tlBody(kind, t);
      var extra = tlExtra(kind, t);
      var tsTxt = kind === "intent" ? "开始" : fmtTs(t.ts, baseTs);
      var hasMore = body.length > 240;
      var bodyHtml = hasMore
        ? '<div class="tl-body" data-full="' + esc(body) + '">' + esc(clip(body, 240)) +
          '<button class="tl-more" onclick="tlToggle(this)">展开</button></div>'
        : '<div class="tl-body">' + esc(body) + "</div>";
      items.push(
        '<div class="tl-item" data-kind="' + kind + '">' +
          '<div class="tl-line"><span class="tl-dot" style="background:' + meta.color + ';"></span>' +
          '<span class="tl-time">' + esc(tsTxt) + "</span></div>" +
          '<div class="tl-card" style="border-left:3px solid ' + meta.color + ';">' +
            '<div class="tl-head"><span class="tl-badge" style="background:' + meta.color + ';">' + meta.badge + "</span>" +
            "<b>" + esc(tlTitle(kind, t, i)) + "</b>" +
            (extra ? ' <code class="tl-extra">' + esc(clip(extra, 80)) + "</code>" : "") +
            "</div>" +
            (body ? bodyHtml : '<div class="muted">（无文本内容）</div>') +
          "</div>" +
        "</div>"
      );
    }
    return (
      capHtml +
      '<div class="tl-filter">' + chips.join("") + "</div>" +
      '<div class="tl-list">' + items.join("") + "</div>"
    );
  }
  function tlFilter(btn) {
    var list = btn.parentNode.nextElementSibling;
    if (!list) return;
    var f = btn.getAttribute("data-f");
    var chips = btn.parentNode.querySelectorAll(".tl-chip");
    for (var i = 0; i < chips.length; i++) chips[i].classList.remove("active");
    btn.classList.add("active");
    var items = list.querySelectorAll(".tl-item");
    for (var j = 0; j < items.length; j++) {
      items[j].style.display = (f === "all" || items[j].getAttribute("data-kind") === f) ? "" : "none";
    }
  }
  function tlToggle(btn) {
    var body = btn.parentNode;
    if (body.classList.contains("open")) {
      body.classList.remove("open");
      body.innerHTML = esc(clip(body.getAttribute("data-full"), 240)) + '<button class="tl-more" onclick="tlToggle(this)">展开</button>';
    } else {
      body.classList.add("open");
      body.innerHTML = esc(body.getAttribute("data-full")) + '<button class="tl-more" onclick="tlToggle(this)">收起</button>';
    }
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
  // ---------- 视图：多 Agent 对比矩阵（V2.7） ----------
  function viewCompare() {
    if (mxState.timer) { clearInterval(mxState.timer); mxState.timer = null; }
    mxState.current = null; mxState.matrix = null; mxState.sel = null;
    Promise.all([
      api("/api/license"),
      loadBackends(),
      api("/api/batches")
    ]).then(function (rs) {
      mxState.license = rs[0];
      var batches = rs[2].batches || [];
      renderCompareShell(batches);
      if (batches.length) loadBatch(batches[0].batch_id);
    }).catch(function (e) { renderErr(e.message); });
  }

  function renderCompareShell(batches) {
    var ent = mxState.license || {};
    var isPro = ent.plan === "pro";
    var cap = ent.max_compare_agents || 2;
    // 默认勾选顺序：稳定可用的内置后端优先，其余按原顺序
    var PREF = ["minimal-react", "deepseek-harness", "aider"];
    var ordered = (backendsCache || []).slice().sort(function (x, y) {
      var ix = PREF.indexOf(x.id), iy = PREF.indexOf(y.id);
      return (ix < 0 ? 99 : ix) - (iy < 0 ? 99 : iy);
    });
    var agentChips = ordered.map(function (b, i) {
      // 社区版默认勾前 cap 个；Pro 默认全选
      var checked = isPro ? "checked" : (i < cap ? "checked" : "");
      return '<label data-agent="' + esc(b.id) + '"><input type="checkbox" class="mx-agent" value="' +
        esc(b.id) + '" ' + checked + '> ' + esc(b.id) + '</label>';
    }).join("");
    var histOpts = batches.map(function (b) {
      return '<option value="' + esc(b.batch_id) + '">' + esc(b.label) +
        "（" + esc(b.status) + " · " + fmtTime(b.created_at) + "）</option>";
    }).join("");
    var planBadge = isPro
      ? '<span class="plan-badge pro">PRO</span>'
      : '<span class="plan-badge community">社区版</span>';
    var planNote = isPro ? "" : (
      '<div class="plan-note">社区版对比矩阵最多选 ' + cap + ' 个 Agent、不保留历史批次、' +
      '隐藏成本/稳定性列、不支持 CSV 导出；导入 Pro License 解锁。</div>'
    );
    renderHTML(
      '<h2 class="page-title">多 Agent 对比矩阵' + planBadge + '</h2>' +
      planNote +
      '<div class="card"><h3>发起对比</h3>' +
        '<div class="matrix-toolbar">' +
          '<div class="field" style="flex:2 1 320px;"><label>选择 Agent（同任务集横向对比）</label>' +
            '<div class="agent-pick" id="mx-agents">' + agentChips + '</div></div>' +
          '<div class="field" style="flex:0 0 150px;"><label>任务集</label>' +
            '<select id="mx-scope"><option value="core">core 核心卡口包</option>' +
            '<option value="full">full 全量任务</option></select></div>' +
          '<div class="field" style="flex:0 0 110px;"><label>每格 runs</label>' +
            '<input id="mx-runs" type="number" value="1" min="1" max="10"></div>' +
          '<div class="field" style="flex:0 0 auto;"><button class="btn" id="mx-start">开始对比</button></div>' +
        '</div>' +
        (batches.length ?
          '<div style="margin-top:12px;"><label class="muted">历史批次：</label> ' +
          '<select id="mx-hist" style="max-width:420px;padding:6px 10px;border:1px solid #D8D6CF;border-radius:8px;">' +
          histOpts + '</select></div>' : "") +
      '</div>' +
      '<div id="mx-result"></div>'
    );
    // 选中态样式
    document.querySelectorAll("#mx-agents label").forEach(function (lab) {
      var cb = lab.querySelector("input");
      if (cb.checked) lab.classList.add("on");
      cb.onchange = function () {
        var picked = pickedAgents();
        if (!isPro && cb.checked && picked.length > cap) {
          cb.checked = false;
          alert("社区版最多选择 " + cap + " 个 Agent 对比，导入 Pro License 后可不限数量。");
          return;
        }
        lab.classList.toggle("on", cb.checked);
      };
    });
    el("mx-start").onclick = startBatch;
    if (el("mx-hist")) el("mx-hist").onchange = function () { loadBatch(this.value); };
  }

  function pickedAgents() {
    return Array.prototype.slice.call(document.querySelectorAll(".mx-agent:checked"))
      .map(function (c) { return c.value; });
  }

  function startBatch() {
    var agents = pickedAgents();
    if (!agents.length) { alert("请至少选择一个 Agent"); return; }
    var scope = el("mx-scope").value;
    var runs = Math.max(1, Math.min(10, parseInt(el("mx-runs").value || "1", 10)));
    var btn = el("mx-start");
    btn.disabled = true; btn.textContent = "已提交…";
    api("/api/batches", { method: "POST", body: { agents: agents, scope: scope, runs: runs } })
      .then(function (r) {
        btn.disabled = false; btn.textContent = "开始对比";
        pollBatch(r.batch_id);
      })
      .catch(function (e) {
        btn.disabled = false; btn.textContent = "开始对比";
        alert("发起失败：" + e.message);
      });
  }

  function loadBatch(bid) {
    if (mxState.timer) { clearInterval(mxState.timer); mxState.timer = null; }
    api("/api/batches/" + bid).then(function (b) {
      mxState.current = b;
      if (b.status === "done") {
        mxState.matrix = b.summary || null;
        mxState.sel = null;
        renderBatchResult();
      } else {
        pollBatch(bid);
      }
    }).catch(function (e) { renderErr(e.message); });
  }

  function pollBatch(bid) {
    if (mxState.timer) clearInterval(mxState.timer);
    function tick() {
      api("/api/batches/" + bid).then(function (b) {
        mxState.current = b;
        if (b.status === "done") {
          clearInterval(mxState.timer); mxState.timer = null;
          mxState.matrix = b.summary || null;
          // 刷新历史下拉
          api("/api/batches").then(function (d) {
            var cur = el("mx-hist");
            if (cur) {
              var opts = d.batches.map(function (x) {
                return '<option value="' + esc(x.batch_id) + '">' + esc(x.label) +
                  "（" + esc(x.status) + " · " + fmtTime(x.created_at) + "）</option>";
              }).join("");
              cur.innerHTML = opts; cur.value = bid;
            }
          });
          renderBatchResult();
        } else {
          renderProgress(b);
        }
      }).catch(function () { /* 轮询偶发失败忽略，下轮重试 */ });
    }
    tick();
    mxState.timer = setInterval(tick, 1500);
  }

  function renderProgress(b) {
    var total = b.total_runs || 0, done = b.done_runs || 0;
    var pct = total ? Math.round(done / total * 100) : 0;
    el("mx-result").innerHTML =
      '<div class="card"><h3>对比进行中 <span class="muted">' + esc(b.label) + '</span></h3>' +
      '<div class="batch-progress"><i style="width:' + pct + '%"></i></div>' +
      '<div class="muted">已完成 ' + done + " / " + total + " 次运行（" + pct + '%）· Agent：' +
        (b.agents || []).map(esc).join("、") + '</div></div>';
  }

  function pctClass(rate) { return rate >= 0.999 ? "ok" : (rate >= 0.5 ? "half" : "bad"); }
  function pctText(rate) { return Math.round((rate || 0) * 100) + "%"; }

  function renderBatchResult() {
    var b = mxState.current, m = mxState.matrix;
    if (!m) { el("mx-result").innerHTML = '<div class="empty">该批次暂无结果数据。</div>'; return; }
    var ent = mxState.license || {};
    var showCost = ent.show_cost_stability !== false;
    var canExport = !!ent.export_csv;
    var agents = m.agents || [], tasks = m.tasks || [], cells = m.cells || {}, totals = m.totals || {};

    var head = '<tr><th class="ag-head">Agent ＼ 任务</th>' +
      tasks.map(function (t) { return "<th>" + esc(t) + "</th>"; }).join("") + "</tr>";
    var rows = agents.map(function (a) {
      var tds = tasks.map(function (t) {
        var c = cells[a + "|" + t];
        if (!c || !c.n) return '<td class="mx-cell empty">—</td>';
        var sub = showCost ? ("<span class='r'>" + pctText(c.pass_rate) + " · σ" + c.std + "</span>")
                           : ("<span class='r'>" + pctText(c.pass_rate) + "</span>");
        return '<td class="mx-cell ' + pctClass(c.pass_rate) + '" data-agent="' + esc(a) +
          '" data-task="' + esc(t) + '"><span class="v">' + c.best + "</span>" + sub + "</td>";
      }).join("");
      return "<tr><td class='ag-head'>" + esc(a) + "</td>" + tds + "</tr>";
    }).join("");

    var concl = (m.conclusion || []).filter(function (l) {
      // 社区版收费墙：结论里同样隐藏成本与稳定性（σ/波动）信息
      if (!showCost && /成本|¥|波动|σ/.test(l)) return false;
      return true;
    }).map(function (l) { return '<div class="line">' + esc(l) + "</div>"; }).join("");

    // 批次置信度（V3.0）
    var confHtml = "";
    if (m.confidence) {
      var c = m.confidence;
      var confColor = c.level === "high" ? "#22c55e" : c.level === "medium" ? "#f59e0b" : "#ef4444";
      var confLabel = c.level === "high" ? "高置信" : c.level === "medium" ? "中置信" : "低置信";
      var dimsHtml = "";
      if (c.dimensions) {
        dimsHtml = Object.keys(c.dimensions).map(function (k) {
          var d = c.dimensions[k];
          return '<div class="conf-dim"><span>' + esc(d.detail) + '</span><b>' + d.score.toFixed(0) + '</b></div>';
        }).join("");
      }
      confHtml = '<div class="confidence-box" style="border-left:4px solid ' + confColor + ';">' +
        '<div class="conf-header"><b style="color:' + confColor + ';">评测置信度 ' + c.score.toFixed(1) + ' / ' + confLabel + '</b>' +
        '<span class="info-icon" title="' + esc((c.suggestions || []).join("; ")) + '">ⓘ</span></div>' +
        dimsHtml + '</div>';
    }

    // V3.2 P2：6个blocking指标门禁
    var gateHtml = "";
    if (m.gate_evaluation) {
      var ge = m.gate_evaluation;
      var gateColor = ge.passed ? "#22c55e" : "#ef4444";
      var gateLabel = ge.passed ? "门禁 PASS" : "门禁 FAIL（阻断）";
      var gateMetricsHtml = "";
      (ge.per_agent || []).forEach(function (ag) {
        gateMetricsHtml += '<div style="margin-bottom:8px;"><b>' + esc(ag.agent) + '</b>：' +
          (ag.passed ? '<span style="color:#22c55e;">PASS</span>' : '<span style="color:#ef4444;">FAIL</span>') +
          '（' + ag.passed_count + '/' + ag.total_count + '指标达标）</div>';
        (ag.metrics || []).forEach(function (m2) {
          var mc = m2.passed ? "#22c55e" : "#ef4444";
          var actualTxt = m2.unit === "%" ? (m2.actual * 100).toFixed(1) + "%" :
                          m2.unit === "s" ? m2.actual.toFixed(1) + "s" :
                          m2.unit === "tokens" ? m2.actual.toFixed(0) :
                          m2.unit === "分" ? m2.actual.toFixed(1) + "分" : m2.actual;
          var thresholdTxt = m2.unit === "%" ? "≥" + (m2.threshold * 100).toFixed(0) + "%" :
                             m2.unit === "s" ? "<" + m2.threshold + "s" :
                             m2.unit === "tokens" ? "≤" + m2.threshold :
                             m2.unit === "分" ? "≥" + m2.threshold + "分" : m2.threshold;
          gateMetricsHtml += '<div class="conf-dim"><span>' +
            (m2.passed ? "✓" : "✗") + ' ' + esc(m2.label) + '：' + actualTxt +
            '（阈值' + thresholdTxt + '）</span><b style="color:' + mc + ';">' +
            (m2.passed ? "达标" : "未达标") + '</b></div>';
        });
      });
      gateHtml = '<div class="confidence-box" style="border-left:4px solid ' + gateColor + ';margin-top:12px;">' +
        '<div class="conf-header"><b style="color:' + gateColor + ';">' + gateLabel + '</b>' +
        '<span class="info-icon" title="6个blocking指标全部达标=PASS，任一未达标=FAIL（阻断代码合并）">ⓘ</span></div>' +
        gateMetricsHtml + '</div>';
    }

    var totHead = showCost
      ? "<tr><th>Agent</th><th class='num'>加权总分</th><th class='num'>任务通过</th><th class='num'>总成本</th><th class='num'>总耗时</th><th class='num'>平均波动σ</th></tr>"
      : "<tr><th>Agent</th><th class='num'>加权总分</th><th class='num'>任务通过</th><th class='num'>总耗时</th></tr>";
    var totRows = agents.map(function (a) {
      var t = totals[a] || {};
      var base = "<tr><td>" + esc(a) + "</td>" +
        "<td class='num'><b>" + t.weighted_score + "</b></td>" +
        "<td class='num'>" + (t.tasks_passed || 0) + "/" + (t.tasks_total || 0) +
        "（" + pctText(t.task_pass_rate) + "）</td>";
      if (showCost) {
        base += "<td class='num'>¥" + t.cost_cny + "</td>" +
                "<td class='num'>" + fmtDur(t.duration_s) + "</td>" +
                "<td class='num'>" + t.avg_std + "</td></tr>";
      } else {
        base += "<td class='num'>" + fmtDur(t.duration_s) + "</td></tr>";
      }
      return base;
    }).join("");

    var exportBtn = canExport
      ? '<button class="btn small" id="mx-export">导出 CSV</button>'
      : '<button class="btn small secondary" disabled title="Pro 功能">导出 CSV 🔒</button> <span class="lock-tag">Pro 功能</span>';

    el("mx-result").innerHTML =
      '<div class="card"><h3>对比结论 <span class="muted">' + esc(b.label) + " · runs=" + (b.runs || 1) +
        " · " + fmtTime(b.finished_at || b.created_at) + '</span></h3><div class="mx-concl">' + concl + "</div>" + confHtml + gateHtml + "</div>" +
      '<div class="card"><h3>得分矩阵 <span class="muted">格内=最好成绩，颜色=通过率；点击单元格下钻每次运行</span></h3>' +
        '<div class="matrix-scroll"><table class="matrix">' + head + rows + "</table></div>" +
        '<div style="margin-top:12px;">' + exportBtn + "</div></div>" +
      '<div class="card totals-card"><h3>Agent 汇总</h3>' +
        '<table class="totals-table">' + totHead + totRows + "</table></div>" +
      '<div class="card mx-drill" id="mx-drill"><h3>单元格下钻</h3>' +
        '<div class="muted">点击上方矩阵中的单元格，查看该 Agent 在该任务上的每次运行。</div></div>';

    document.querySelectorAll(".mx-cell[data-agent]").forEach(function (td) {
      td.onclick = function () {
        document.querySelectorAll(".mx-cell.sel").forEach(function (x) { x.classList.remove("sel"); });
        td.classList.add("sel");
        renderDrill(td.getAttribute("data-agent"), td.getAttribute("data-task"));
      };
    });
    if (el("mx-export")) el("mx-export").onclick = function () {
      window.open("/api/matrix/export?batch_id=" + encodeURIComponent(b.batch_id), "_blank");
    };
  }

  function renderDrill(agent, task) {
    var m = mxState.matrix, ent = mxState.license || {};
    var showCost = ent.show_cost_stability !== false;
    var c = (m.cells || {})[agent + "|" + task];
    var box = el("mx-drill");
    if (!c || !c.n) { box.innerHTML = "<h3>单元格下钻</h3><div class='empty'>无运行记录</div>"; return; }
    var st = { best: c.best, mean: c.mean, std: c.std, pass_rate: c.pass_rate };
    var head = showCost
      ? "<tr><th>run</th><th>状态</th><th>得分</th><th>通过率</th><th>耗时</th><th>成本</th><th></th></tr>"
      : "<tr><th>run</th><th>状态</th><th>得分</th><th>通过率</th><th>耗时</th><th></th></tr>";
    var rows = c.runs.map(function (r) {
      var line = "<tr><td>" + esc(r.run_id) + "</td><td>" + esc(r.status) + "</td><td>" + r.score +
        "</td><td>" + pctText(r.pass_rate) + "</td><td>" + fmtDur(r.duration_s) + "</td>";
      if (showCost) line += "<td>¥" + r.cost_cny + "</td>";
      line += '<td><a href="#/run/' + esc(r.run_id) + '">轨迹详情 →</a></td></tr>';
      return line;
    }).join("");
    box.innerHTML =
      "<h3>单元格下钻 · " + esc(agent) + " × " + esc(task) +
      ' <span class="muted">N=' + c.n + " · best " + st.best + " · mean " + st.mean +
      " · σ " + st.std + " · 通过率 " + pctText(st.pass_rate) + "</span></h3>" +
      "<table>" + head + rows + "</table>";
  }

  // ---------- 8 维度 Agent 能力雷达图 ----------
  function capabilityRadar(runs) {
    var completed = (runs || []).filter(function(r) { return r.status === "completed"; });
    if (!completed.length) {
      return '<div class="empty">暂无足够的运行数据绘制能力雷达图（需要 completed run）</div>';
    }

    // 兼容两种数据格式：列表接口（r.score/r.tokens/r.steps数字）和详情接口（r.metrics.score/r.metrics.usage/r.steps数组）
    var getScore = function(r) { return r.score !== undefined ? r.score : ((r.metrics && r.metrics.score) || 0); };
    var getTokens = function(r) {
      if (r.tokens) return (r.tokens.prompt_tokens || 0) + (r.tokens.completion_tokens || 0);
      var u = (r.metrics && r.metrics.usage) || {};
      return (u.prompt_tokens || 0) + (u.completion_tokens || 0);
    };
    var getStepsArr = function(r) { return Array.isArray(r.steps) ? r.steps : []; };
    var getStepCount = function(r) { return Array.isArray(r.steps) ? r.steps.length : (r.steps || 0); };

    // 8 维度计算（0-100 分）
    var dims = [
      { key: "completion", label: "任务完成率", score: 0, desc: "" },
      { key: "tool",       label: "工具使用",   score: 0, desc: "" },
      { key: "reasoning",  label: "推理规划",   score: 0, desc: "" },
      { key: "fault",      label: "容错纠错",   score: 0, desc: "" },
      { key: "efficiency", label: "效率",       score: 0, desc: "" },
      { key: "stability",  label: "稳定性",     score: 0, desc: "" },
      { key: "security",   label: "安全性",     score: 0, desc: "" },
      { key: "cost",       label: "成本效益",   score: 0, desc: "" },
    ];

    // 1. 任务完成率：平均 score * 100（score 可能 >1，归一化到 pass_rate）
    var avgScore = completed.reduce(function(s, r) { return s + (r.pass_rate !== undefined ? r.pass_rate : Math.min(1, getScore(r))); }, 0) / completed.length;
    dims[0].score = Math.min(100, Math.round(avgScore * 100));
    dims[0].desc = "平均通过率 " + (avgScore * 100).toFixed(0) + "% · " + completed.length + " 次运行";

    // 2. 工具使用：工具调用数 + 工具多样性（从 steps 数组提取，列表接口降级为步骤数估算）
    var totalToolCalls = 0, toolFailures = 0, toolSet = {};
    for (var i = 0; i < completed.length; i++) {
      var steps = getStepsArr(completed[i]);
      if (steps.length) {
        for (var j = 0; j < steps.length; j++) {
          var st = steps[j] || {};
          if (st.action && st.action !== "finish" && st.action !== "think") {
            totalToolCalls++;
            toolSet[st.action] = true;
            var obs = ((st.observation || "") + " " + (st.error || "")).toLowerCase();
            if (obs.indexOf("error") >= 0 || obs.indexOf("fail") >= 0 || obs.indexOf("失败") >= 0) toolFailures++;
          }
        }
      } else {
        // 列表接口降级：用步骤计数作为工具调用近似
        var sc = getStepCount(completed[i]);
        totalToolCalls += Math.max(0, sc - 1); // 减去 finish 步
      }
    }
    var toolSuccessRate = totalToolCalls > 0 ? (1 - toolFailures / totalToolCalls) * 70 : 60;
    var toolDiversity = Math.min(30, Object.keys(toolSet).length * 5);
    dims[1].score = Math.round(Math.min(100, toolSuccessRate + toolDiversity));
    dims[1].desc = totalToolCalls + " 次工具调用 · " + (Object.keys(toolSet).length || "N/A") + " 种工具";

    // 3. 推理规划：平均步骤数作为推理深度近似
    var avgSteps = completed.reduce(function(s, r) { return s + getStepCount(r); }, 0) / completed.length;
    dims[2].score = Math.min(100, Math.round(30 + avgSteps * 8));
    dims[2].desc = "平均 " + avgSteps.toFixed(1) + " 步推理";

    // 4. 容错纠错：error 状态 run 的比例（越低越好）+ 有 steps 数组时检测失败恢复
    var allRuns = runs || [];
    var errorRuns = allRuns.filter(function(r) { return r.status === "error" || r.status === "timeout"; }).length;
    var faultRate = allRuns.length > 0 ? errorRuns / allRuns.length : 0;
    dims[3].score = Math.max(0, Math.round(100 - faultRate * 200));
    dims[3].desc = errorRuns + "/" + allRuns.length + " 次运行出错/超时";

    // 5. 效率：平均步骤数 + 平均 token（越低越好）
    var avgTokens = completed.reduce(function(s, r) { return s + getTokens(r); }, 0) / completed.length;
    var stepScore = Math.max(0, 100 - avgSteps * 3);
    var tokenScore = avgTokens > 0 ? Math.max(0, 100 - avgTokens / 200) : 50;
    dims[4].score = Math.round((stepScore + tokenScore) / 2);
    dims[4].desc = "平均 " + avgSteps.toFixed(1) + " 步 · " + Math.round(avgTokens) + " tokens";

    // 6. 稳定性：得分标准差（越低越稳定）
    var scores = completed.map(function(r) { return r.pass_rate !== undefined ? r.pass_rate : Math.min(1, getScore(r)); });
    var mean = scores.reduce(function(a, b) { return a + b; }, 0) / scores.length;
    var variance = scores.reduce(function(s, v) { return s + Math.pow(v - mean, 2); }, 0) / scores.length;
    var std = Math.sqrt(variance);
    dims[5].score = Math.max(0, Math.round(100 - std * 150));
    dims[5].desc = "通过率 σ=" + std.toFixed(3) + " · " + completed.length + " 次采样";

    // 7. 安全性：安全测试任务（T703/T704）通过率
    var securityRuns = completed.filter(function(r) { return r.task_id === "T703" || r.task_id === "T704"; });
    if (securityRuns.length) {
      var secPass = securityRuns.filter(function(r) { return (r.pass_rate !== undefined ? r.pass_rate : getScore(r)) >= 1.0; }).length;
      dims[6].score = Math.round(secPass / securityRuns.length * 100);
      dims[6].desc = secPass + "/" + securityRuns.length + " 安全测试通过";
    } else {
      dims[6].score = 50;
      dims[6].desc = "暂无安全测试数据（跑 T703/T704）";
    }

    // 8. 成本效益：通过率 / 千 token 比率
    var costEff = avgTokens > 0 ? Math.min(100, avgScore / avgTokens * 30000) : 50;
    dims[7].score = Math.round(costEff);
    dims[7].desc = "每万 token 通过率 " + (avgTokens > 0 ? (avgScore / avgTokens * 10000).toFixed(2) : "N/A");

    // SVG 雷达图绘制
    var cx = 200, cy = 200, R = 140;
    var n = dims.length;
    var angle = function(i) { return (Math.PI * 2 * i / n) - Math.PI / 2; };
    var point = function(i, r) { return [cx + r * Math.cos(angle(i)), cy + r * Math.sin(angle(i))]; };

    var svg = '<svg viewBox="0 0 400 420" style="width:100%;max-width:400px;height:auto;display:block;margin:0 auto;">';
    // 背景网格（4 层）
    for (var ring = 1; ring <= 4; ring++) {
      var r = R * ring / 4;
      var pts = [];
      for (var d = 0; d < n; d++) { var p = point(d, r); pts.push(p[0].toFixed(1) + "," + p[1].toFixed(1)); }
      svg += '<polygon points="' + pts.join(" ") + '" fill="none" stroke="#E5E7EB" stroke-width="1"/>';
    }
    // 轴线
    for (var a = 0; a < n; a++) { var ap = point(a, R); svg += '<line x1="' + cx + '" y1="' + cy + '" x2="' + ap[0].toFixed(1) + '" y2="' + ap[1].toFixed(1) + '" stroke="#E5E7EB" stroke-width="1"/>'; }
    // 数据多边形
    var dataPts = [];
    for (var e = 0; e < n; e++) { var ep = point(e, R * dims[e].score / 100); dataPts.push(ep[0].toFixed(1) + "," + ep[1].toFixed(1)); }
    svg += '<polygon points="' + dataPts.join(" ") + '" fill="rgba(31,58,95,0.2)" stroke="#1F3A5F" stroke-width="2"/>';
    // 数据点
    for (var f = 0; f < n; f++) { var fp = point(f, R * dims[f].score / 100); svg += '<circle cx="' + fp[0].toFixed(1) + '" cy="' + fp[1].toFixed(1) + '" r="4" fill="#1F3A5F"><title>' + esc(dims[f].label) + ': ' + dims[f].score + '分 · ' + esc(dims[f].desc) + '</title></circle>'; }
    // 标签
    for (var g = 0; g < n; g++) {
      var lp = point(g, R + 28);
      var anchor = Math.abs(lp[0] - cx) < 10 ? "middle" : (lp[0] > cx ? "start" : "end");
      svg += '<text x="' + lp[0].toFixed(1) + '" y="' + lp[1].toFixed(1) + '" text-anchor="' + anchor + '" font-size="12" font-weight="600" fill="#374151">' + esc(dims[g].label) + '</text>';
      svg += '<text x="' + lp[0].toFixed(1) + '" y="' + (lp[1] + 14).toFixed(1) + '" text-anchor="' + anchor + '" font-size="10" fill="#9CA3AF">' + dims[g].score + '分</text>';
    }
    svg += '</svg>';

    // 维度详情表
    var detailRows = dims.map(function(d) {
      var color = d.score >= 70 ? "#10B981" : (d.score >= 40 ? "#F59E0B" : "#EF4444");
      return '<tr><td style="text-align:left;font-weight:600;">' + esc(d.label) + '</td>' +
        '<td style="color:' + color + ';font-weight:700;">' + d.score + '</td>' +
        '<td style="text-align:left;color:#6B7280;font-size:11px;">' + esc(d.desc) + '</td></tr>';
    }).join("");

    return '<div class="radar-wrap">' + svg +
      '<table class="radar-detail"><tr><th style="text-align:left;">维度</th><th>得分</th><th style="text-align:left;">说明</th></tr>' + detailRows + '</table></div>';
  }

  // ---------- 性能-成本散点图（帕累托前沿） ----------
  function costPerformanceScatter(runs) {
    // 按 任务+后端 聚合：平均得分、平均 token、运行次数
    var groups = {};
    for (var i = 0; i < (runs || []).length; i++) {
      var r = runs[i] || {};
      if (r.status !== "completed") continue;
      var key = (r.task_id || "?") + "|" + (r.agent_id || "?");
      if (!groups[key]) groups[key] = { task: r.task_id, agent: r.agent_id, scores: [], tokens: [], n: 0 };
      var g = groups[key];
      // 兼容列表接口（r.score/r.tokens）和详情接口（r.metrics.score/r.metrics.usage）
      var score = r.score !== undefined ? r.score : ((r.metrics && r.metrics.score) || 0);
      var tokens = 0;
      if (r.tokens) {
        tokens = (r.tokens.prompt_tokens || 0) + (r.tokens.completion_tokens || 0);
      } else {
        var usage = (r.metrics && r.metrics.usage) || {};
        tokens = (usage.prompt_tokens || 0) + (usage.completion_tokens || 0);
      }
      g.scores.push(score);
      g.tokens.push(tokens);
      g.n++;
    }
    var points = [];
    var keys = Object.keys(groups);
    for (var j = 0; j < keys.length; j++) {
      var g = groups[keys[j]];
      if (g.n < 1) continue;
      var avgScore = g.scores.reduce(function(a, b) { return a + b; }, 0) / g.scores.length;
      var avgTokens = g.tokens.reduce(function(a, b) { return a + b; }, 0) / g.tokens.length;
      if (avgTokens <= 0) continue; // 无 token 数据的点不展示
      points.push({ task: g.task, agent: g.agent, score: avgScore, tokens: avgTokens, n: g.n });
    }
    if (!points.length) {
      return '<div class="empty">暂无足够的运行数据绘制性能-成本散点图（需要含 token 记录的 completed run）</div>';
    }
    // 帕累托前沿：按 token 升序，维护最高得分
    points.sort(function(a, b) { return a.tokens - b.tokens; });
    var frontier = [];
    var maxScore = -1;
    for (var k = 0; k < points.length; k++) {
      if (points[k].score > maxScore) {
        frontier.push(points[k]);
        maxScore = points[k].score;
      }
    }
    // SVG 绘制
    var W = 700, H = 380, padL = 60, padR = 20, padT = 20, padB = 50;
    var plotW = W - padL - padR, plotH = H - padT - padB;
    var maxTokens = Math.max.apply(null, points.map(function(p) { return p.tokens; })) * 1.1;
    var maxScore = Math.max.apply(null, points.map(function(p) { return p.score; })) * 1.15;
    if (maxScore < 1) maxScore = 1;
    var x = function(t) { return padL + (t / maxTokens) * plotW; };
    var y = function(s) { return padT + plotH - (s / maxScore) * plotH; };
    // 坐标轴
    var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;max-width:' + W + 'px;height:auto;">';
    svg += '<line x1="' + padL + '" y1="' + (padT + plotH) + '" x2="' + (padL + plotW) + '" y2="' + (padT + plotH) + '" stroke="#CBD5E1" stroke-width="1"/>';
    svg += '<line x1="' + padL + '" y1="' + padT + '" x2="' + padL + '" y2="' + (padT + plotH) + '" stroke="#CBD5E1" stroke-width="1"/>';
    // 刻度
    for (var ti = 0; ti <= 4; ti++) {
      var tv = (maxTokens / 4) * ti;
      var tx = x(tv);
      svg += '<line x1="' + tx + '" y1="' + (padT + plotH) + '" x2="' + tx + '" y2="' + (padT + plotH + 5) + '" stroke="#CBD5E1"/>';
      svg += '<text x="' + tx + '" y="' + (padT + plotH + 18) + '" text-anchor="middle" font-size="10" fill="#6B7280">' + Math.round(tv / 1000) + 'k</text>';
    }
    for (var si = 0; si <= 4; si++) {
      var sv = (maxScore / 4) * si;
      var sy = y(sv);
      svg += '<line x1="' + (padL - 5) + '" y1="' + sy + '" x2="' + padL + '" y2="' + sy + '" stroke="#CBD5E1"/>';
      svg += '<text x="' + (padL - 8) + '" y="' + (sy + 3) + '" text-anchor="end" font-size="10" fill="#6B7280">' + sv.toFixed(1) + '</text>';
    }
    // 轴标签
    svg += '<text x="' + (padL + plotW / 2) + '" y="' + (H - 10) + '" text-anchor="middle" font-size="12" fill="#374151" font-weight="600">Token 用量（成本）→</text>';
    svg += '<text x="15" y="' + (padT + plotH / 2) + '" text-anchor="middle" font-size="12" fill="#374151" font-weight="600" transform="rotate(-90 15 ' + (padT + plotH / 2) + ')">得分（性能）→</text>';
    // 帕累托前沿线
    if (frontier.length > 1) {
      var pathD = frontier.map(function(p, idx) { return (idx === 0 ? "M" : "L") + x(p.tokens) + "," + y(p.score); }).join(" ");
      svg += '<path d="' + pathD + '" fill="none" stroke="#F59E0B" stroke-width="2" stroke-dasharray="6,3" opacity="0.7"/>';
    }
    // 数据点
    var agentColors = {};
    var colorPalette = ["#6366F1", "#10B981", "#F59E0B", "#EC4899", "#8B5CF6", "#0EA5E9", "#EF4444", "#14B8A6"];
    var colorIdx = 0;
    for (var pi = 0; pi < points.length; pi++) {
      var p = points[pi];
      if (!agentColors[p.agent]) { agentColors[p.agent] = colorPalette[colorIdx % colorPalette.length]; colorIdx++; }
      var isFrontier = frontier.some(function(f) { return f.task === p.task && f.agent === p.agent; });
      var cx = x(p.tokens), cy = y(p.score);
      svg += '<circle cx="' + cx + '" cy="' + cy + '" r="' + (isFrontier ? 7 : 5) + '" fill="' + agentColors[p.agent] + '" opacity="0.8" stroke="' + (isFrontier ? "#F59E0B" : "none") + '" stroke-width="2">';
      svg += '<title>' + esc(p.task) + ' / ' + esc(p.agent) + '&#10;得分: ' + p.score.toFixed(2) + '&#10;Token: ' + Math.round(p.tokens) + '&#10;运行次数: ' + p.n + (isFrontier ? '&#10;★ 帕累托最优' : '') + '</title>';
      svg += '</circle>';
    }
    // 图例
    var legendX = padL + 10, legendY = padT + 10;
    var agentKeys = Object.keys(agentColors);
    for (var li = 0; li < agentKeys.length; li++) {
      svg += '<rect x="' + legendX + '" y="' + (legendY + li * 18) + '" width="12" height="12" rx="2" fill="' + agentColors[agentKeys[li]] + '"/>';
      svg += '<text x="' + (legendX + 18) + '" y="' + (legendY + li * 18 + 10) + '" font-size="11" fill="#374151">' + esc(agentKeys[li]) + '</text>';
    }
    svg += '</svg>';
    var summary = '<div class="scatter-summary">共 ' + points.length + ' 个 任务×后端 数据点 · 帕累托前沿 ' + frontier.length + ' 个点（同等成本下得分最高）</div>';
    return '<div class="scatter-wrap">' + svg + summary + '</div>';
  }

  // ---------- 视图：监控中心（V3.3 P3） ----------
  function viewMonitor() {
    renderHTML(
      '<h2 class="page-title">监控中心 <span class="tl-note">在线监控 · 熔断降级 · 灰度发布 · 采样审计</span></h2>' +
      '<div id="monitor-content"><div class="empty">加载中…</div></div>'
    );
    loadMonitor();
  }

  function loadMonitor() {
    Promise.all([
      fetch("/api/circuit-status").then(function (r) { return r.json(); }),
      fetch("/api/flywheel-stats").then(function (r) { return r.json(); }).catch(function () { return null; })
    ]).then(function (results) {
      var data = results[0];
      var flywheel = results[1];
      var cb = data.circuit_breaker || {};
      var sampler = data.daily_sampler || {};
      var gray = data.gray_release || {};
      var recent = data.recent_stats || {};

      var stateColor = cb.state === "closed" ? "#22c55e" : cb.state === "open" ? "#ef4444" : "#f59e0b";
      var stateLabel = cb.state === "closed" ? "正常（closed）" : cb.state === "open" ? "已熔断（open）" : "半开（half_open）";

      // V3.4 P4：数据飞轮面板
      var flywheelHtml = "";
      if (flywheel && flywheel.stats) {
        var fs = flywheel.stats;
        var effColor = fs.flywheel_efficiency >= 0.3 ? "#22c55e" : fs.flywheel_efficiency >= 0.1 ? "#f59e0b" : "#ef4444";
        var reportHtml = (flywheel.report || []).map(function (r) {
          return '<div class="line" style="font-size:13px;margin:4px 0;">' + esc(r) + '</div>';
        }).join("");
        flywheelHtml =
          '<div class="card"><h3>数据飞轮 <span class="tl-note">采样→标注→回流闭环，线上badcase自动沉淀为回归任务</span></h3>' +
          '<div class="kpi-row">' +
            '<div class="kpi-card"><div class="kpi-label">今日采样</div><div class="kpi-value">' + fs.sampled_today + '</div>' +
            '<div class="kpi-sub">累计 ' + fs.sampled_total + ' 条</div></div>' +
            '<div class="kpi-card"><div class="kpi-label">待标注</div><div class="kpi-value" style="color:#f59e0b;">' + fs.pending_annotation + '</div>' +
            '<div class="kpi-sub">标注率 ' + (fs.annotation_rate * 100).toFixed(0) + '%</div></div>' +
            '<div class="kpi-card"><div class="kpi-label">已回流任务</div><div class="kpi-value" style="color:#22c55e;">' + fs.converted_to_task + '</div>' +
            '<div class="kpi-sub">回流率 ' + (fs.conversion_rate * 100).toFixed(0) + '%</div></div>' +
            '<div class="kpi-card"><div class="kpi-label">飞轮效率</div><div class="kpi-value" style="color:' + effColor + ';">' + (fs.flywheel_efficiency * 100).toFixed(0) + '%</div>' +
            '<div class="kpi-sub">经验记忆 ' + fs.memories_active + '/' + fs.memories_total + ' 活跃</div></div>' +
          '</div>' +
          '<div style="margin-top:12px;"><b>健康度报告：</b>' + reportHtml + '</div>' +
          '<div style="margin-top:12px;"><a href="#/badcases" class="btn small">进入 Badcase 管理 →</a></div>' +
          '</div>';
      }

      var html =
        // 概览卡片
        '<div class="kpi-row">' +
          '<div class="kpi-card"><div class="kpi-label">熔断器状态</div>' +
          '<div class="kpi-value" style="color:' + stateColor + ';">' + stateLabel + '</div>' +
          '<div class="kpi-sub">错误率 ' + (cb.error_rate * 100).toFixed(1) + '% / 阈值 ' + (cb.config.error_rate_threshold * 100).toFixed(0) + '%</div></div>' +
          '<div class="kpi-card"><div class="kpi-label">P99 延迟</div>' +
          '<div class="kpi-value">' + cb.p99_latency + 's</div>' +
          '<div class="kpi-sub">阈值 ' + cb.config.p99_latency_threshold_s + 's</div></div>' +
          '<div class="kpi-card"><div class="kpi-label">总请求数</div>' +
          '<div class="kpi-value">' + cb.total_requests + '</div>' +
          '<div class="kpi-sub">成功 ' + cb.success_count + ' / 错误 ' + cb.error_count + '</div></div>' +
          '<div class="kpi-card"><div class="kpi-label">今日采样</div>' +
          '<div class="kpi-value">' + sampler.sampled_count + '/' + sampler.target_count + '</div>' +
          '<div class="kpi-sub">人工复核队列</div></div>' +
        '</div>' +

        // 数据飞轮（V3.4 P4）
        flywheelHtml +

        // 熔断器详情
        '<div class="card"><h3>熔断器详情 <span class="tl-note">错误率>20% 或 P99>30s 自动熔断，冷却5分钟后半开恢复</span></h3>' +
        '<table class="data-table"><thead><tr><th>指标</th><th>当前值</th><th>阈值</th><th>状态</th></tr></thead><tbody>' +
        '<tr><td>错误率</td><td>' + (cb.error_rate * 100).toFixed(1) + '%</td><td>≤' + (cb.config.error_rate_threshold * 100).toFixed(0) + '%</td>' +
        '<td>' + (cb.error_rate <= cb.config.error_rate_threshold ? '<span style="color:#22c55e;">正常</span>' : '<span style="color:#ef4444;">超阈</span>') + '</td></tr>' +
        '<tr><td>P99 延迟</td><td>' + cb.p99_latency + 's</td><td><' + cb.config.p99_latency_threshold_s + 's</td>' +
        '<td>' + (cb.p99_latency < cb.config.p99_latency_threshold_s ? '<span style="color:#22c55e;">正常</span>' : '<span style="color:#ef4444;">超阈</span>') + '</td></tr>' +
        '<tr><td>超时次数</td><td>' + cb.timeout_count + '</td><td>-</td><td>-</td></tr>' +
        '<tr><td>步数超限次数</td><td>' + cb.step_overflow_count + '</td><td>≤' + cb.config.max_steps_per_task + '步/任务</td><td>-</td></tr>' +
        '<tr><td>熔断冷却剩余</td><td>' + (cb.cooldown_remaining || 0) + 's</td><td>' + cb.config.cooldown_seconds + 's</td>' +
        '<td>' + (cb.state === "open" ? '<span style="color:#f59e0b;">冷却中</span>' : '<span style="color:#22c55e;">-</span>') + '</td></tr>' +
        '</tbody></table></div>' +

        // 最近运行统计
        '<div class="card"><h3>最近运行统计 <span class="tl-note">最近100次运行</span></h3>' +
        '<div class="kpi-row">' +
          '<div class="kpi-card"><div class="kpi-label">运行总数</div><div class="kpi-value">' + recent.recent_runs + '</div></div>' +
          '<div class="kpi-card"><div class="kpi-label">错误数</div><div class="kpi-value" style="color:#ef4444;">' + recent.recent_errors + '</div></div>' +
          '<div class="kpi-card"><div class="kpi-label">最近错误率</div><div class="kpi-value">' + (recent.recent_error_rate * 100).toFixed(1) + '%</div></div>' +
        '</div></div>' +

        // 灰度发布
        '<div class="card"><h3>灰度发布 <span class="tl-note">1% → 5% → 50% → 100%，每阶段最少48小时</span></h3>' +
        '<div class="kpi-row">' +
          '<div class="kpi-card"><div class="kpi-label">当前阶段</div><div class="kpi-value">' + (gray.enabled ? gray.stage : "未启用") + '</div>' +
          '<div class="kpi-sub">已运行 ' + (gray.elapsed_hours || 0) + ' 小时</div></div>' +
          '<div class="kpi-card"><div class="kpi-label">最短阶段时长</div><div class="kpi-value">' + gray.min_stage_duration_hours + 'h</div></div>' +
          '<div class="kpi-card"><div class="kpi-label">可晋级</div>' +
          '<div class="kpi-value" style="color:' + (gray.can_advance ? "#22c55e" : "#94a3b8") + ';">' + (gray.can_advance ? "是" : "否") + '</div></div>' +
        '</div></div>' +

        // 每日采样
        '<div class="card"><h3>每日采样审计 <span class="tl-note">每日自动采样' + sampler.target_count + '条运行记录供人工复核</span></h3>' +
        '<div class="muted">今日已采样 ' + sampler.sampled_count + ' 条，日期：' + sampler.date + '</div>' +
        (sampler.samples && sampler.samples.length ?
          '<div style="margin-top:12px;"><b>采样的 run_id：</b><br/>' +
          sampler.samples.slice(0, 20).map(function (s) {
            return '<a href="#/run/' + s + '" style="font-family:monospace;margin-right:12px;">' + s + '</a>';
          }).join("") +
          (sampler.samples.length > 20 ? '<br/><span class="muted">... 共 ' + sampler.samples.length + ' 条</span>' : "") +
          '</div>' : '<div class="muted">暂无采样记录</div>') +
        '</div>';

      el("monitor-content").innerHTML = html;
    }).catch(function (e) {
      el("monitor-content").innerHTML = '<div class="empty">加载失败：' + esc(e.message) + '</div>';
    });
  }

  // ---------- 视图：报告 ----------
  function viewReport() {
    renderHTML(
      '<h2 class="page-title">评测报告</h2>' +
      '<div class="card"><h3>Agent 能力雷达图 <span class="tl-note">8 维度能力画像：完成率/工具/推理/容错/效率/稳定性/安全/成本</span></h3>' +
      '<div id="radar-container"><div class="empty">加载中…</div></div></div>' +
      '<div class="card"><h3>性能-成本分析 <span class="tl-note">帕累托前沿：同等成本下得分最高的配置</span></h3>' +
      '<div id="scatter-container"><div class="empty">加载中…</div></div></div>' +
      '<div class="card"><button class="btn" id="btn-gen-report">生成报告</button> ' +
      '<span class="muted">复用引擎 reporter 生成自包含 HTML（离线可看）</span></div>' +
      '<iframe id="report-frame" style="width:100%;height:70vh;border:1px solid #E4E3DD;border-radius:12px;background:#fff;"></iframe>'
    );
    // 加载雷达图和散点图数据
    api("/api/runs?limit=500").then(function(data) {
      var runs = data.runs || data || [];
      el("radar-container").innerHTML = capabilityRadar(runs);
      el("scatter-container").innerHTML = costPerformanceScatter(runs);
    }).catch(function() {
      el("radar-container").innerHTML = '<div class="empty">加载运行数据失败</div>';
      el("scatter-container").innerHTML = '<div class="empty">加载运行数据失败</div>';
    });
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

  // ---------- 任务包市场（M3 Web 集成） ----------
  function viewPackages() {
    renderHTML(
      '<h2 class="page-title">任务包市场</h2>' +
      '<div class="card">' +
        '<h3>安装新任务包</h3>' +
        '<div style="display:flex;gap:8px;align-items:center;">' +
          '<input type="text" id="pkg-source" placeholder="git 仓库 URL 或本地目录路径" style="flex:1;padding:6px 10px;border:1px solid #ddd;border-radius:4px;">' +
          '<input type="text" id="pkg-name" placeholder="包名（可选，覆盖默认）" style="width:180px;padding:6px 10px;border:1px solid #ddd;border-radius:4px;">' +
          '<button onclick="installPkg()" style="padding:6px 16px;background:#2563eb;color:#fff;border:none;border-radius:4px;cursor:pointer;">安装</button>' +
        '</div>' +
        '<p style="color:#666;font-size:12px;margin-top:8px;">支持 git 仓库 URL（如 https://github.com/user/taskpack.git）或本地目录路径。安装后可在 tasks/manifest.yaml 中添加 includes: [包名] 启用。</p>' +
      '</div>' +
      '<div id="pkg-list"><p style="color:#999;">加载中...</p></div>'
    );
    loadPackages();
  }

  function loadPackages() {
    fetch("/api/packages").then(function (r) { return r.json(); }).then(function (data) {
      var html = '<h3 style="margin-top:24px;">已安装任务包（' + data.total + '）</h3>';
      if (data.total === 0) {
        html += '<div class="card"><p style="color:#999;">尚未安装任何任务包。使用上方表单安装，或通过 CLI：agent-eval taskpack install &lt;source&gt;</p></div>';
      } else {
        html += '<div class="card"><table class="env-table" style="width:100%;">';
        html += '<tr><th>包名</th><th>版本</th><th>作者</th><th>描述</th><th>任务数</th><th>操作</th></tr>';
        data.packages.forEach(function (p) {
          html += '<tr>' +
            '<td><strong>' + esc(p.name) + '</strong></td>' +
            '<td>' + esc(p.version) + '</td>' +
            '<td>' + esc(p.author || '-') + '</td>' +
            '<td>' + esc(p.description || '-') + '</td>' +
            '<td>' + p.task_count + '</td>' +
            '<td><button onclick="removePkg(\'' + esc(p.name) + '\')" style="padding:4px 10px;background:#dc2626;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">卸载</button></td>' +
            '</tr>';
          if (p.tasks && p.tasks.length > 0) {
            html += '<tr><td colspan="6" style="background:#f9fafb;padding:8px 12px;font-size:12px;color:#666;">包含任务: ' + p.tasks.map(esc).join(', ') + '</td></tr>';
          }
        });
        html += '</table></div>';
      }
      document.getElementById("pkg-list").innerHTML = html;
    }).catch(function (e) {
      document.getElementById("pkg-list").innerHTML = '<p style="color:#dc2626;">加载失败: ' + esc(e.message) + '</p>';
    });
  }

  window.installPkg = function () {
    var source = document.getElementById("pkg-source").value.trim();
    var name = document.getElementById("pkg-name").value.trim();
    if (!source) { alert("请输入任务包来源（git URL 或本地路径）"); return; }
    var body = { source: source };
    if (name) body.name = name;
    fetch("/api/packages/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || "安装失败"); });
      return r.json();
    }).then(function (data) {
      alert(data.message);
      document.getElementById("pkg-source").value = "";
      document.getElementById("pkg-name").value = "";
      loadPackages();
    }).catch(function (e) {
      alert("安装失败: " + e.message);
    });
  };

  window.removePkg = function (name) {
    if (!confirm("确定要卸载任务包 '" + name + "' 吗？")) return;
    fetch("/api/packages/" + encodeURIComponent(name), { method: "DELETE" })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || "卸载失败"); });
        return r.json();
      })
      .then(function (data) {
        alert(data.message);
        loadPackages();
      })
      .catch(function (e) { alert("卸载失败: " + e.message); });
  };

  // ---------- 路由 ----------
  function router() {
    var h = location.hash || "#/dashboard";
    var parts = h.replace(/^#\//, "").split("/");
    var name = parts[0] || "dashboard";
    // 离开对比矩阵视图时停止批次轮询，避免后台空转
    if (name !== "compare" && mxState.timer) { clearInterval(mxState.timer); mxState.timer = null; }
    var nav = document.querySelectorAll(".nav a");
    nav.forEach(function (a) {
      a.classList.toggle("active", a.getAttribute("data-view") === name);
    });
    if (name === "run") { viewRunDetail(parts[1]); return; }
    if (name === "badcase") { viewBadcaseDetail(parts[1]); return; }
    if (name === "memory") { viewMemoryDetail(parts[1]); return; }
    if (name === "dashboard") viewDashboard();
    else if (name === "tasks") viewTasks();
    else if (name === "packages") viewPackages();
    else if (name === "history") viewHistory();
    else if (name === "badcases") viewBadcases();
    else if (name === "memories") viewMemories();
    else if (name === "compare") viewCompare();
    else if (name === "monitor") viewMonitor();
    else if (name === "report") viewReport();
    else if (name === "settings") viewSettings();
    else viewDashboard();
  }

  // ---------- 启动 ----------
  loadMeta().catch(function () {});
  window.addEventListener("hashchange", router);
  // 轨迹回放时间线：inline onclick 需要全局可达（IIFE 作用域内不可达）
  window.tlFilter = tlFilter;
  window.tlToggle = tlToggle;
  router();
})();
