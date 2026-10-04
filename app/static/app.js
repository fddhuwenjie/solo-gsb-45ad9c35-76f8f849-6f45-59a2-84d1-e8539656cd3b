/* 条款引用重排台 —— 前端逻辑（原生 JS） */
"use strict";

const $ = (sel) => document.querySelector(sel);

const els = {
  input: $("#contract-input"),
  btnParse: $("#btn-parse"),
  btnExample: $("#btn-example"),
  btnSave: $("#btn-save"),
  draftSelect: $("#draft-select"),
  status: $("#parse-status"),
  workspace: $("#workspace"),
  sortable: $("#sortable"),
  tray: $("#tray"),
  trayList: $("#tray-list"),
  issuesBar: $("#issues-bar"),
  before: $("#before-view"),
  after: $("#after-view"),
  toast: $("#toast"),
  tabRefs: $("#tab-refs"),
  tabTerms: $("#tab-terms"),
  paneRefs: $("#pane-refs"),
  paneTerms: $("#pane-terms"),
  tabReview: $("#tab-review"),
  paneReview: $("#pane-review"),
  reviewEnabled: $("#review-enabled"),
  btnReviewRefresh: $("#btn-review-refresh"),
  reviewRevision: $("#review-revision"),
  btnReviewConfirmPending: $("#btn-review-confirm-pending"),
  reviewDirty: $("#review-dirty"),
  btnReviewSave: $("#btn-review-save"),
  reviewOff: $("#review-off"),
  reviewSummary: $("#review-summary"),
  reviewList: $("#review-list"),
  termIssues: $("#term-issues"),
  termDefs: $("#term-defs"),
  termIgnored: $("#term-ignored"),
  termIgnoredList: $("#term-ignored-list"),
  termsView: $("#terms-view"),
  previewBar: $("#preview-bar"),
  btnPreview: $("#btn-preview"),
  previewCount: $("#preview-count"),
  previewPane: $("#preview-pane"),
  previewStale: $("#preview-stale"),
  btnRePreview: $("#btn-repreview"),
  previewSummary: $("#preview-summary"),
  previewDelete: $("#preview-delete"),
  previewImpacts: $("#preview-impacts"),
  btnConfirmDelete: $("#btn-confirm-delete"),
  btnCancelPreview: $("#btn-cancel-preview"),
  tabVersions: $("#tab-versions"),
  paneVersions: $("#pane-versions"),
  btnIssue: $("#btn-issue"),
  issueRevision: $("#issue-revision"),
  issueBlockers: $("#issue-blockers"),
  issueSuccess: $("#issue-success"),
  versionList: $("#version-list"),
  versionViewer: $("#version-viewer"),
  auditOld: $("#audit-old"),
  auditNew: $("#audit-new"),
  btnAudit: $("#btn-audit"),
  auditStatus: $("#audit-status"),
  auditResult: $("#audit-result"),
  applyPane: $("#apply-pane"),
  applyBanner: $("#apply-banner"),
  applySelectAll: $("#apply-selectall"),
  applySummary: $("#apply-summary"),
  btnApplyFetch: $("#btn-apply-fetch"),
  btnApplySubmit: $("#btn-apply-submit"),
  applyItems: $("#apply-items"),
  applySides: $("#apply-sides"),
};

// 运行时状态
const state = {
  text: "",
  order: [],        // 存活条款 id 的先序（深度优先）扁平序列
  deleted: [],      // 已删除条款 id（含级联删除的后代）
  data: null,       // 最近一次 /api/analyze 响应
  clauseMap: new Map(),
  currentDraftId: null,
  lastParsedText: null,
  dragCid: null,    // 拖动中的条款 id（整棵子树随之移动）
  mode: "refs",     // refs | terms
  terms: null,      // 最近一次 /api/terms 响应
  termIgnores: [],  // 已忽略的术语问题 occ_id（随草稿保存）
  // 删除影响预演（纯预览；确认前不进入 order/deleted，不改草稿）
  previewSel: [],       // 勾选的存活条款 id（仅存活项有效）
  previewResult: null,  // 最近一次预演响应
  previewStale: false,  // 正文/排序/删除变化后旧结果过期，不可确认
  previewBusy: false,
  // 逐条复核
  review: {
    status: null,   // 最近一次 GET review 响应
    busy: false,
  },
  // 生效版本签发
  savedRevision: null,  // 服务器上当前草稿的修订号（未保存草稿为 null）
  savedSnapshot: null,  // 上次从服务器读取 / 保存成功时的快照签名（脏检查用）
  versions: [],         // 当前草稿的已签发版本列表
  versionsLoadedFor: null,
  activeVersion: null,  // 正在查看的版本详情
  issueBusy: false,
  // 版本差异审计（只读不可变快照；结果缓存于前端，不随后续草稿被改而失效）
  audit: { oldId: null, newId: null, result: null, busy: false },
  // 应用到当前草稿（旧版基线 → 新版候选的三方合并）
  apply: {
    busy: false,          // 预览 / 提交请求进行中
    preview: null,        // 最近一次 apply-preview 响应
    selections: new Map(), // cid -> Set(dimension)，勾选状态在重新预览间保留
    seen: false,
  },
};

const LS_KEY = "clause-rebench-session";

// ---------------------------------------------------------------- 工具

function toast(msg) {
  els.toast.textContent = msg;
  els.toast.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => els.toast.classList.add("hidden"), 2200);
}

function esc(s) {
  return s.replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    let payload = null;
    try {
      payload = await res.json();
      detail = (payload && payload.detail && typeof payload.detail === "object"
        ? payload.detail.message
        : payload && payload.detail) || detail;
    } catch (_) {}
    const err = new Error(detail);
    err.status = res.status;
    err.payload = payload;
    throw err;
  }
  return res.json();
}

// ---------------------------------------------------------------- 层级树

// state.order 是树的先序扁平序列；以下工具按后端返回的 path 还原父子树，
// 排序只允许发生在同一父级的兄弟之间，移动节点时整棵子树一起移动。

function buildTree() {
  const root = { path: [], key: "", children: new Map(), clauseIds: [] };
  const at = new Map([["", root]]);
  state.data.clauses.forEach((c) => {
    let node = root;
    (c.path || []).forEach((seg, depth) => {
      const pref = c.path.slice(0, depth + 1);
      const key = pref.join(".");
      let nxt = at.get(key);
      if (!nxt) {
        nxt = { path: pref, key, children: new Map(), clauseIds: [] };
        at.set(key, nxt);
        node.children.set(key, nxt);
      }
      node = nxt;
    });
    if (!node.clauseIds.includes(c.id)) node.clauseIds.push(c.id);
  });
  return { root, at };
}

function isDeletedId(cid) {
  return state.deleted.includes(cid);
}

function nodeAlive(node) {
  if (node.clauseIds.some((id) => !isDeletedId(id))) return true;
  for (const ch of node.children.values()) {
    if (nodeAlive(ch)) return true;
  }
  return false;
}

// state.order 始终是「合法树先序」（后端会规整，本地 moveSibling 只做同层
// 整子树移动）。布局完全由「原始路径深度 + 当前先序」推得，与原编号数值无关：
// 用深度栈跟踪父级，节点（含原文未单列的虚祖先）在其父级的兄弟序列中首次
// 出现时取下一个连续编号。
function computeLayout() {
  const { root, at } = buildTree();
  const newPathByKey = new Map([["", []]]);
  const seqByKey = new Map();   // 原 key -> 直接子节点原 key 的新次序
  const stack = [];             // { key, depth }

  const ensureNode = (op, depth) => {
    const key = op.slice(0, depth).join(".");
    if (newPathByKey.has(key)) return;
    const parentKey = depth === 1 ? "" : op.slice(0, depth - 1).join(".");
    if (depth > 1 && !newPathByKey.has(parentKey)) ensureNode(op, depth - 1);
    let seq = seqByKey.get(parentKey);
    if (!seq) { seq = []; seqByKey.set(parentKey, seq); }
    if (!seq.includes(key)) seq.push(key);
    const parentPath = newPathByKey.get(parentKey);
    newPathByKey.set(key, parentPath.concat(seq.indexOf(key) + 1));
  };

  for (const cid of state.order) {
    const c = state.clauseMap.get(cid);
    const op = c ? (c.path || []) : [];
    while (stack.length && stack[stack.length - 1].depth >= op.length) stack.pop();
    for (let d = 1; d <= op.length; d++) ensureNode(op, d);
    stack.push({ key: op.join("."), depth: op.length });
  }

  const kidsByKey = new Map();
  for (const [key, seq] of seqByKey) {
    kidsByKey.set(key, seq.map((k) => at.get(k)).filter(Boolean).filter(nodeAlive));
  }
  for (const key of newPathByKey.keys()) {
    if (!kidsByKey.has(key)) kidsByKey.set(key, []);
  }
  return { newPathByKey, kidsByKey, root };
}

// 条款当前的「新路径」。优先用本地布局（拖动后、后端响应回来前也准确），
// 布局尚不可用时回退到后端 new_label / 原始 path。
function newPathOf(cid) {
  try {
    const c = state.clauseMap.get(cid);
    if (!c) return [];
    const { newPathByKey } = computeLayout();
    // 找到该条款所属节点（其原始路径对应的 key）
    const op = c.path || [];
    for (let d = op.length; d >= 1; d--) {
      const key = op.slice(0, d).join(".");
      if (newPathByKey.has(key)) return newPathByKey.get(key);
    }
  } catch (_) { /* 布局未就绪 */ }
  const c = state.clauseMap.get(cid);
  return c && c.new_label
    ? c.new_label.slice(1, -1).split(".").map(Number)
    : (c ? (c.path || []) : []);
}

// 按「新路径」前缀求子树区段（用于本地换序，后端尚未返回前）
function subtreeSpanByNewPath(path) {
  let start = -1;
  let end = state.order.length;
  state.order.forEach((cid, i) => {
    const p = newPathOf(cid);
    const under = p.length >= path.length
      && path.every((s, k) => s === p[k]);
    if (under) {
      if (start === -1) start = i;
      end = i + 1;
    }
  });
  return [start, end];
}

// ---------------------------------------------------------------- 分析

async function runAnalyze(structureChanged = false) {
  state.text = els.input.value;
  if (!state.text.trim()) {
    els.workspace.classList.add("hidden");
    state.lastParsedText = null;
    return;
  }
  try {
    const data = await api("/api/analyze", {
      method: "POST",
      body: JSON.stringify({
        text: state.text,
        order: state.order,
        deleted: state.deleted,
      }),
    });
    state.data = data;
    // 以后端规整结果为准：顺序、删除集合（含级联后代）前后端保持一致
    state.order = data.order;
    state.deleted = data.deleted_clauses.map((d) => d.id);
    state.clauseMap = new Map(data.clauses.map((c) => [c.id, c]));
    if (structureChanged) renderSortable();
    renderAll();
    els.workspace.classList.remove("hidden");
    state.lastParsedText = state.text;
    saveSession();
    // 预演选择只保留仍存活条款；正文/排序/删除变化后旧结果标记过期
    state.previewSel = state.previewSel.filter(
      (id) => state.clauseMap.has(id) && !state.deleted.includes(id));
    invalidatePreview();
    fetchTerms();
    if (state.mode === "versions") renderIssuePanel();
    if (state.mode === "review" && state.review.status
        && state.review.status.enabled) {
      renderReview();
    }
    if (state.apply.preview) {
      renderApplyBanner(state.apply.preview);
      updateApplySummary();
    }
  } catch (e) {
    els.status.textContent = "分析失败：" + e.message;
    throw e;
  }
}

const scheduleAnalyze = debounce(() => runAnalyze(false), 200);

// ---------------------------------------------------------------- 可排序（层级）列表

function renderSortable() {
  els.sortable.innerHTML = "";
  const { root, kidsByKey: kids, newPathByKey: newPaths } = computeLayout();

  const labelOf = (path) => path.length ? "第" + path.join(".") + "条" : "";

  const renderNode = (node, depth) => {
    // 节点自身的真实条款卡片（重复路径时只有首张可拖动）
    const own = node.clauseIds.filter((id) => !isDeletedId(id));
    if (own.length) {
      own.forEach((cid, dupIdx) => {
        els.sortable.appendChild(
          makeCard(cid, depth, dupIdx === 0, labelOf(newPaths.get(node.key))));
      });
    } else if (depth > 0) {
      // 原文未单列的虚父节点：展示占位，仅在顶层允许作为同层落点
      const newP = newPaths.get(node.key);
      els.sortable.appendChild(makeVirtualRow(node, depth, labelOf(newP), newP.join(".")));
    }
    // 子节点按新兄弟顺序递归渲染（整棵子树连续显示）
    (kids.get(node.key) || []).forEach((ch) => renderNode(ch, depth + 1));
  };
  renderNode(root, 0);

  // 删除托盘
  if (state.deleted.length) {
    els.tray.classList.remove("hidden");
    els.trayList.innerHTML = "";
    state.deleted.forEach((cid) => {
      const c = state.clauseMap.get(cid);
      if (!c) return;
      const chip = document.createElement("span");
      chip.className = "tray-item";
      chip.textContent = `${c.label} ＋`;
      chip.title = "点击恢复「" + c.label + "」（连同其后代）";
      chip.addEventListener("click", () => restoreClause(cid));
      els.trayList.appendChild(chip);
    });
  } else {
    els.tray.classList.add("hidden");
  }
  updatePreviewBar();
}

function makeCard(cid, depth, draggable, liveLabel) {
  const c = state.clauseMap.get(cid);
  const li = document.createElement("li");
  li.className = "clause-card";
  li.draggable = draggable;
  li.dataset.cid = cid;
  li.style.paddingLeft = (12 + depth * 22) + "px";

  li.innerHTML = `
    <span class="drag-handle" title="${draggable ? "拖动排序（仅同一父级内）" : "重复编号，不可拖动"}">⠿</span>
    <input type="checkbox" class="preview-check" data-cid="${cid}"
      title="加入删除影响预演（不立即删除）" />
    <span class="new-badge">${esc(liveLabel || c.new_label || "—")}</span>
    <span class="old-label">${esc(c.label)}</span>
    <span class="card-heading">${esc(c.heading || "（无正文）")}</span>
    <span class="card-actions">
      <button type="button" class="btn-danger-mini" title="删除该条款（含全部子条款）">✕</button>
    </span>`;

  const check = li.querySelector(".preview-check");
  check.checked = state.previewSel.includes(cid);
  check.addEventListener("click", (e) => e.stopPropagation());
  check.addEventListener("change", () => {
    const set = new Set(state.previewSel);
    if (check.checked) set.add(cid); else set.delete(cid);
    state.previewSel = [...set];
    if (state.previewResult) invalidatePreview();
    updatePreviewBar();
    saveSession();
  });

  li.querySelector(".btn-danger-mini").addEventListener("click", (e) => {
    e.stopPropagation();
    deleteClause(cid);
  });

  if (draggable) {
    li.addEventListener("dragstart", (e) => {
      if (e.target && e.target.closest && e.target.closest("input,button")) {
        e.preventDefault();
        return;
      }
      state.dragCid = cid;
      li.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", cid); } catch (_) {}
    });
    li.addEventListener("dragend", () => {
      state.dragCid = null;
      li.classList.remove("dragging");
      document.querySelectorAll(".drag-over,.drag-denied")
        .forEach((n) => n.classList.remove("drag-over", "drag-denied"));
    });
    attachDropZone(li, cid);
  }
  return li;
}

function makeVirtualRow(node, depth, newLabel, newKey) {
  const li = document.createElement("li");
  li.className = "clause-card virtual";
  li.dataset.vkey = newKey;
  li.style.paddingLeft = (12 + depth * 22) + "px";
  li.innerHTML = `
    <span class="drag-handle virtual-handle">•</span>
    <span class="new-badge virtual-badge">${esc(newLabel)}</span>
    <span class="virtual-hint">（原文未单列，仅含子条款）</span>`;
  // 虚节点只在顶层作为「同层兄弟」的落点（如 4.1/4.2 与第5条之间调整顺序）
  if (depth === 1) attachDropZone(li, null, newKey);
  return li;
}

// 同层兄弟换序：把 srcCid 整棵子树移动到目标子树之前/之后（虚节点则插到
// 其整组子树之前）。返回 true 表示发生了移动；同父级校验不通过返回 false。
// 注意：state.order 是最近一次后端返回的规范先序，一切定位都基于「新路径」。
function moveSibling(srcCid, targetCid, { before = false, virtualKey = null } = {}) {
  const srcPath = newPathOf(srcCid);
  let dstPath;
  if (targetCid) {
    dstPath = newPathOf(targetCid);
  } else {
    dstPath = virtualKey == null || virtualKey === ""
      ? [] : virtualKey.split(".").map(Number);
  }
  const srcParent = srcPath.slice(0, -1);
  const dstParent = dstPath.slice(0, -1);
  if (srcParent.length !== dstParent.length
      || !srcParent.every((s, k) => s === dstParent[k])) {
    return false;
  }

  const [srcStart, srcEnd] = subtreeSpanByNewPath(srcPath);
  const [dstStart] = subtreeSpanByNewPath(dstPath);
  if (targetCid && srcStart === dstStart) return true;

  const block = state.order.slice(srcStart, srcEnd);
  const rest = state.order.slice(0, srcStart)
    .concat(state.order.slice(srcEnd));
  let anchor;
  if (targetCid) {
    anchor = rest.indexOf(targetCid);
    if (anchor === -1) return false; // 拖入自身子树内部
    if (!before) anchor += 1;
  } else {
    // 虚节点：始终插到其整组存活子条款之前。删除源区段后，虚组起点
    // 只可能因「源在其之前」而后移，减去源区段长度即可。
    anchor = srcStart < dstStart
      ? dstStart - (srcEnd - srcStart)
      : dstStart;
  }
  const nextOrder = rest.slice(0, anchor).concat(block, rest.slice(anchor));
  if (nextOrder.every((id, i) => id === state.order[i])
      && nextOrder.length === state.order.length) {
    return true; // 位置无变化
  }
  state.order = nextOrder;
  return true;
}

// 落点处理：只接受同一父级下的兄弟；before=true 插到目标子树之前
function attachDropZone(li, targetCid, virtualKey = null) {
  li.addEventListener("dragover", (e) => {
    const srcId = state.dragCid;
    if (!srcId) return;
    const srcParent = newPathOf(srcId).slice(0, -1);
    let dstParent;
    if (targetCid) {
      dstParent = newPathOf(targetCid).slice(0, -1);
    } else {
      dstParent = virtualKey === "" ? [] : virtualKey.split(".").map(Number);
    }
    const sameParent = srcParent.length === dstParent.length
      && srcParent.every((s, k) => s === dstParent[k]);
    e.preventDefault();
    e.dataTransfer.dropEffect = sameParent ? "move" : "none";
    li.classList.toggle("drag-denied", !sameParent);
    li.classList.toggle("drag-over", sameParent);
  });
  li.addEventListener("dragleave", () =>
    li.classList.remove("drag-over", "drag-denied"));
  li.addEventListener("drop", (e) => {
    e.preventDefault();
    li.classList.remove("drag-over", "drag-denied");
    const srcId = state.dragCid;
    state.dragCid = null;
    if (!srcId) return;
    if (targetCid && srcId === targetCid) return;

    const before = targetCid ? e.offsetY < li.offsetHeight / 2 : true;
    const ok = moveSibling(srcId, targetCid,
      { before, virtualKey: targetCid ? null : virtualKey });
    if (!ok) {
      toast("只能在同一父级下调整顺序（子条款会随父条款整棵移动）");
      return;
    }
    if (state.previewResult) invalidatePreview();
    scheduleAnalyze();
  });
}

function deleteClause(cid) {
  // 直接删除走现有流程；任何打开的预演立即作废（删除状态已变）
  state.previewResult = null;
  state.previewStale = false;
  state.previewSel = [];
  els.previewPane.classList.add("hidden");
  // 连同整棵子树一起删除
  const c = state.clauseMap.get(cid);
  const pref = c.path || [];
  state.order = state.order.filter((x) => {
    const p = state.clauseMap.get(x).path || [];
    return !(p.length >= pref.length && pref.every((s, k) => s === p[k]));
  });
  state.data.clauses.forEach((x) => {
    const p = x.path || [];
    if (p.length >= pref.length && pref.every((s, k) => s === p[k])
        && !state.deleted.includes(x.id)) {
      state.deleted.push(x.id);
    }
  });
  runAnalyze().catch(() => {});
}

function restoreClause(cid) {
  // 恢复父条款时一并恢复其全部后代；顺序按解析顺序回到同级原位
  state.previewResult = null;
  state.previewStale = false;
  state.previewSel = [];
  els.previewPane.classList.add("hidden");
  const c = state.clauseMap.get(cid);
  const pref = c.path || [];
  state.data.clauses.forEach((x) => {
    const p = x.path || [];
    if (p.length >= pref.length && pref.every((s, k) => s === p[k])) {
      state.deleted = state.deleted.filter((d) => d !== x.id);
    }
  });
  // 交给后端按解析顺序把恢复的子树归位
  runAnalyze().catch(() => {});
}

// ---------------------------------------------------------------- 删除影响预演

// 正文 / 排序 / 删除状态变化后：旧结果保留展示但不得确认，必须重新预演
function invalidatePreview() {
  if (!state.previewResult) return;
  state.previewStale = true;
  els.previewStale.classList.remove("hidden");
  els.btnConfirmDelete.disabled = true;
  updatePreviewBar();
}

function updatePreviewBar() {
  const n = state.previewSel.length;
  els.previewCount.textContent = String(n);
  els.btnPreview.disabled = n === 0 || state.previewBusy;
  if (state.previewResult && state.previewStale) {
    els.btnPreview.textContent = "🔍 重新预演";
  } else {
    els.btnPreview.textContent = `🔍 删除影响预演（已选 ${n} 项）`;
  }
}

async function openPreview() {
  if (!state.previewSel.length || state.previewBusy) return;
  const sigBefore = JSON.stringify({
    text: els.input.value,
    order: state.order,
    deleted: state.deleted,
    sel: [...state.previewSel].sort(),
  });
  state.previewBusy = true;
  els.btnPreview.disabled = true;
  els.btnPreview.textContent = "预演中…";
  try {
    const res = await api("/api/delete-preview", {
      method: "POST",
      body: JSON.stringify({
        text: els.input.value,
        order: state.order,
        deleted: state.deleted,
        candidates: state.previewSel,
      }),
    });
    state.previewResult = res;
    // 服务端候选可能与本地选择有出入（异常 id 被过滤），以返回为准
    state.previewSel = res.candidates.slice();
    const sigAfter = JSON.stringify({
      text: els.input.value,
      order: state.order,
      deleted: state.deleted,
      sel: [...state.previewSel].sort(),
    });
    state.previewStale = sigBefore !== sigAfter;
    renderPreview();
  } catch (e) {
    toast("预演分析失败：" + e.message);
    updatePreviewBar();
  } finally {
    state.previewBusy = false;
    updatePreviewBar();
  }
}

const PREVIEW_KIND_META = {
  ref_broken: { tag: "引用失效", cls: "deleted" },
  term_lost_unique: { tag: "失去唯一定义", cls: "warn" },
  term_ambiguous: { tag: "变歧义", cls: "purple" },
  term_rebound: { tag: "改绑定义", cls: "cyan" },
};

function renderPreview() {
  const res = state.previewResult;
  if (!res) return;
  els.previewPane.classList.remove("hidden");
  els.previewStale.classList.toggle("hidden", !state.previewStale);
  els.btnConfirmDelete.disabled = state.previewStale;

  const c = res.counts;
  els.previewSummary.innerHTML =
    `<span class="pv-stat"><b>${c.selected}</b> 项勾选</span>` +
    `<span class="pv-stat">+ <b>${c.cascade}</b> 项级联后代</span>` +
    `<span class="pv-stat">拟删除共 <b>${c.to_delete}</b> 项</span>` +
    `<span class="pv-stat ${c.impacts ? "bad" : ""}">存活但受影响 <b>${c.impacts}</b> 项` +
    `（直接 ${c.direct} / 反向连锁 ${c.transitive}，循环已合并）</span>` +
    `<span class="pv-stat">引用失效 <b>${c.ref}</b></span>` +
    `<span class="pv-stat">术语绑定变化 <b>${c.term}</b></span>` +
    (c.cycles ? `<span class="pv-stat bad">↺ 含循环链 <b>${c.cycles}</b></span>` : "");

  els.previewDelete.innerHTML = "";
  res.to_delete.forEach((d) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "pv-del-chip" + (d.selected ? " picked" : " cascade");
    chip.textContent = (d.selected ? "☑ " : "⤷ ") + d.label;
    chip.title = d.heading || (d.selected ? "用户勾选" : "随父条款级联删除");
    chip.addEventListener("click", () => locate(d.id));
    els.previewDelete.appendChild(chip);
  });

  els.previewImpacts.innerHTML = "";
  if (!res.impacts.length) {
    const ok = document.createElement("div");
    ok.className = "preview-ok";
    ok.textContent = "✓ 删除后其余存活条款的引用与术语绑定均不受影响";
    els.previewImpacts.appendChild(ok);
  }
  res.impacts.forEach((row, idx) => {
    const div = document.createElement("div");
    div.className = "pv-row" + (row.cycle_group ? " cycle-group" : "");
    const head = document.createElement("div");
    head.className = "pv-row-head";
    const kindCls = (row.details[0]
      && PREVIEW_KIND_META[row.details[0].kind].cls) || "muted";
    head.innerHTML =
      `<span class="pv-idx">${idx + 1}</span>`;
    // 循环组：每个存活成员都是可单独点击定位的按钮；普通行沿用单条款按钮
    const members = row.member_ids || [row.clause_id];
    members.forEach((mid) => {
      const mc = state.clauseMap.get(mid);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "pv-clause " + kindCls +
        (members.length > 1 ? " in-group" : "");
      btn.textContent = mc
        ? `${mc.label} ${mc.heading || ""}`
        : (mid === "preamble" ? "前置说明" : mid);
      btn.title = members.length > 1
        ? "循环组成员，点击定位（组内成员互相引用，合并为一个影响项）"
        : "点击定位";
      btn.addEventListener("click", () => locate(mid));
      head.appendChild(btn);
    });
    head.insertAdjacentHTML("beforeend",
      (row.distance > 0
        ? `<span class="pv-hop">连锁 ${row.distance} 跳</span>`
        : `<span class="pv-hop direct">直接</span>`) +
      (row.cycle ? `<span class="pv-cycle" title="该条款处于互相指回的引用循环中（已按强连通分量合并为一个影响项）">↺ 循环</span>` : ""));
    div.appendChild(head);

    row.details.forEach((d) => {
      const meta = PREVIEW_KIND_META[d.kind] || { tag: d.kind, cls: "muted" };
      const line = document.createElement("div");
      line.className = `pv-detail ${meta.cls}`;
      // 循环组内不同成员上的影响各自保留（术语按出现处）；标注所在成员
      let prefix = "";
      if (row.cycle_group && d.owner_id && d.owner_id !== row.clause_id) {
        const oc = state.clauseMap.get(d.owner_id);
        prefix = `<span class="pv-owner">${oc ? oc.label : d.owner_id}：</span>`;
      }
      line.innerHTML =
        `<span class="chip-tag">${meta.tag}</span>` +
        prefix +
        `<span class="pv-msg">${esc(d.message)}</span>`;
      line.addEventListener("click", () =>
        locate(d.owner_id || row.clause_id, d.target_id));
      div.appendChild(line);
    });

    // 原因链：每个节点可能是单个条款或一个强连通分量（循环组）；
    // 循环组的每个成员与根因（terminal）均可点击定位。
    const chain = document.createElement("div");
    chain.className = "pv-chain";
    row.chain.forEach((node, i) => {
      if (i > 0) chain.appendChild(document.createTextNode(" ▸ "));
      const wrap = document.createElement("span");
      wrap.className = "pv-chain-step" +
        (node.kind === "group" ? " is-group" : "") +
        (node.cycle ? " cyclic" : "");
      if (node.cycle) {
        const badge = document.createElement("span");
        badge.className = "pv-chain-cycle";
        badge.textContent = "↺";
        badge.title = "引用循环（强连通分量）：组内成员互相指回";
        wrap.appendChild(badge);
      }
      (node.members || []).forEach((m, j) => {
        if (j > 0) wrap.appendChild(document.createTextNode("、"));
        const b = document.createElement("button");
        b.type = "button";
        b.className = "pv-chain-node" + (node.terminal ? " terminal" : "");
        b.textContent = m.label + (m.heading ? ` ${m.heading}` : "");
        b.title = node.terminal
          ? "根因（被删除目标 / 被移除定义）"
          : (node.kind === "group" ? "循环组成员，点击定位" : "点击定位");
        b.addEventListener("click", () => locate(m.id));
        wrap.appendChild(b);
      });
      chain.appendChild(wrap);
    });
    div.appendChild(chain);
    els.previewImpacts.appendChild(div);
  });

  els.previewPane.scrollIntoView({ behavior: "smooth", block: "nearest" });
  updatePreviewBar();
}

function closePreview() {
  state.previewResult = null;
  state.previewStale = false;
  state.previewSel = [];
  els.previewPane.classList.add("hidden");
  renderSortable(); // 清空卡片勾选
  updatePreviewBar();
}

async function confirmPreview() {
  // 确认前再次校验：正文 / 排序 / 删除状态与预演签名一致才允许套用
  if (!state.previewResult || state.previewStale) {
    toast("预演结果已过期，请重新预演后再确认");
    return;
  }
  const picked = state.previewResult.to_delete.map((d) => d.id);
  const snapshot = {
    order: state.order.slice(), deleted: state.deleted.slice(),
    data: state.data,
  };
  els.btnConfirmDelete.disabled = true;
  els.btnConfirmDelete.textContent = "套用中…";

  // 一次性套用现有级联删除（前端按路径前缀展开，与后端 cascade_deleted 一致）
  picked.forEach((cid) => {
    const c = state.clauseMap.get(cid);
    if (!c) return;
    const pref = c.path || [];
    state.order = state.order.filter((x) => {
      const p = state.clauseMap.get(x).path || [];
      return !(p.length >= pref.length && pref.every((s, k) => s === p[k]));
    });
    state.data.clauses.forEach((x) => {
      const p = x.path || [];
      if (p.length >= pref.length && pref.every((s, k) => s === p[k])
          && !state.deleted.includes(x.id)) {
        state.deleted.push(x.id);
      }
    });
  });

  try {
    await runAnalyze(true); // 重排、引用改写、术语审校一并刷新
    // 成功：关闭预演，清空勾选
    state.previewResult = null;
    state.previewStale = false;
    state.previewSel = [];
    els.previewPane.classList.add("hidden");
    renderSortable();
    updatePreviewBar();
    toast(`已套用删除（${picked.length} 项），引用重写与术语审校已刷新`);
  } catch (e) {
    // 分析失败：草稿回滚到确认前，预演结果不得继续确认
    state.order = snapshot.order;
    state.deleted = snapshot.deleted;
    state.data = snapshot.data;
    state.previewStale = true;
    state.previewSel = state.previewSel.filter(
      (id) => state.clauseMap.has(id) && !state.deleted.includes(id));
    renderSortable();
    renderAll();
    renderPreview();
    toast("确认失败，草稿未改变：" + e.message);
  } finally {
    els.btnConfirmDelete.textContent = "确认删除（一次性套用级联删除并重排 / 改写 / 术语审校）";
  }
}

// ---------------------------------------------------------------- 问题条

const ISSUE_META = {
  unparsable: { tag: "无法解析" },
  deleted: { tag: "目标已删除" },
  circular: { tag: "循环引用" },
};

function renderIssues() {
  const issues = state.data.issues;
  els.issuesBar.innerHTML = "";
  if (!issues.length) {
    const ok = document.createElement("div");
    ok.className = "issues-ok";
    ok.textContent = "✓ 所有引用均已成功绑定并重写";
    els.issuesBar.appendChild(ok);
    els.issuesBar.classList.remove("hidden");
    return;
  }
  issues.forEach((it) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `issue-chip ${it.type}`;
    const meta = ISSUE_META[it.type] || { tag: it.type };
    chip.innerHTML =
      `<span class="chip-tag">${meta.tag}</span>` +
      `<span class="chip-msg">${esc(it.message)}</span>`;
    chip.addEventListener("click", () => {
      locate(it.clause_id, it.target_id);
    });
    els.issuesBar.appendChild(chip);
  });
  els.issuesBar.classList.remove("hidden");
}

// ---------------------------------------------------------------- 对照视图

function makeMark(m, mode, ownerId) {
  const span = document.createElement("span");
  span.className = `mark ${m.status}`;
  span.textContent = mode === "before" ? m.text : m.new_text;
  const titles = {
    ok: "引用已按新层级顺序重写（点击定位目标条款）",
    unparsable: "无法解析或目标不存在（点击定位本条款）",
    deleted: "引用的目标条款已删除（点击查看目标）",
    circular: "循环引用：条款间相互指回（点击定位）",
  };
  span.title = titles[m.status] || "";
  span.addEventListener("click", (e) => {
    e.stopPropagation();
    locate(m.target_id || ownerId);
  });
  return span;
}

function appendBody(container, clause, mode) {
  const text = clause.before_text;
  const marks = clause.refs || [];
  let cursor = 0;
  marks.forEach((m) => {
    if (m.start > cursor) {
      container.appendChild(document.createTextNode(text.slice(cursor, m.start)));
    }
    container.appendChild(makeMark(m, mode, clause.id));
    cursor = m.end;
  });
  if (cursor < text.length) {
    container.appendChild(document.createTextNode(text.slice(cursor)));
  }
}

function makeBlock(clause, mode, isDeleted) {
  const block = document.createElement("div");
  block.className = "clause-block" + (isDeleted ? " deleted-clause" : "");
  block.dataset.cid = clause.id;
  // 用层级缩进体现父子树
  const depth = clause.depth != null ? clause.depth : (clause.path || []).length;
  block.style.paddingLeft = (12 + (depth - 1) * 18) + "px";

  const label = document.createElement("span");
  label.className = "block-label" + (mode === "after" ? " new" : "");
  label.textContent = mode === "after"
    ? (clause.new_label + " ")
    : (clause.label + " ");
  block.appendChild(label);

  if (isDeleted) {
    const ribbon = document.createElement("span");
    ribbon.className = "deleted-ribbon";
    ribbon.textContent = "已删除";
    block.appendChild(ribbon);
  }

  appendBody(block, clause, mode);
  return block;
}

function renderCompare() {
  const d = state.data;

  // 改动前：原解析顺序（含已删除条款，置灰）
  els.before.innerHTML = "";
  if (d.preamble) {
    const p = document.createElement("div");
    p.className = "preamble-block clause-block";
    p.dataset.cid = "preamble";
    p.textContent = d.preamble;
    els.before.appendChild(p);
  }
  d.clauses.forEach((c) => {
    els.before.appendChild(makeBlock(c, "before", d.deleted_clauses.some((x) => x.id === c.id)));
  });

  // 改动后：新顺序，只含存活条款
  els.after.innerHTML = "";
  if (d.preamble) {
    const p = document.createElement("div");
    p.className = "preamble-block clause-block";
    p.dataset.cid = "preamble";
    p.textContent = d.preamble;
    els.after.appendChild(p);
  }
  d.result.forEach((c) => els.after.appendChild(makeBlock(c, "after", false)));

  const c = d.counts;
  els.status.textContent =
    `共 ${d.clauses.length} 条，识别引用 ${d.total_refs} 处，已重写 ` +
    `${d.total_refs - c.unparsable - c.deleted - c.circular} 处` +
    (c.unparsable + c.deleted + c.circular
      ? `；异常 ${c.unparsable + c.deleted + c.circular} 处`
      : "，无异常");
}

function renderAll() {
  renderSortable();
  renderIssues();
  renderCompare();
}

// ---------------------------------------------------------------- 定位

function locate(...ids) {
  let scrolled = false;
  ids.filter(Boolean).forEach((cid) => {
    document.querySelectorAll(`.clause-block[data-cid="${CSS.escape(cid)}"]`)
      .forEach((node) => {
        if (!scrolled) {
          node.scrollIntoView({ behavior: "smooth", block: "center" });
          scrolled = true;
        }
        node.classList.remove("flash");
        void node.offsetWidth; // 重置动画
        node.classList.add("flash");
      });
    // 排序列表中的卡片也高亮
    const card = els.sortable.querySelector(`[data-cid="${CSS.escape(cid)}"]`);
    if (card) {
      card.classList.remove("flash");
      void card.offsetWidth;
      card.classList.add("flash");
    }
  });
}// ---------------------------------------------------------------- 定义术语审校

async function fetchTerms() {
  if (!els.input.value.trim()) {
    state.terms = null;
    return;
  }
  try {
    state.terms = await api("/api/terms", {
      method: "POST",
      body: JSON.stringify({ text: els.input.value, deleted: state.deleted }),
    });
    // 清理已失效（正文中不再存在）的忽略项
    const live = new Set(state.terms.issues.map((it) => it.occ_id));
    state.termIgnores = state.termIgnores.filter((id) => live.has(id));
    if (state.mode === "terms") renderTerms();
  } catch (e) {
    state.terms = null;
  }
}

const TERM_ISSUE_META = {
  undefined: { tag: "未定义" },
  duplicate: { tag: "重复定义" },
  ambiguous: { tag: "歧义绑定" },
  unused: { tag: "未使用" },
};

function termBlockLabel(b) {
  return b.kind === "preamble" ? "前置说明" : (b.label || ("第" + b.path.join(".") + "条"));
}

function activeTermIssues() {
  const t = state.terms;
  if (!t) return [];
  return t.issues.filter((it) => !state.termIgnores.includes(it.occ_id));
}

function renderTermIssues() {
  const t = state.terms;
  els.termIssues.innerHTML = "";
  const issues = activeTermIssues();
  if (!issues.length) {
    const ok = document.createElement("div");
    ok.className = "term-ok";
    ok.textContent = "✓ 未发现术语问题" +
      (state.termIgnores.length ? `（已忽略 ${state.termIgnores.length} 项）` : "");
    els.termIssues.appendChild(ok);
  } else {
    issues.forEach((it) => {
      const b = t.blocks.find((x) => x.id === it.block_id);
      const where = b ? termBlockLabel(b) : "";
      const row = document.createElement("div");
      row.className = `term-issue-row ${it.type}`;
      row.innerHTML =
        `<span class="chip-tag">${TERM_ISSUE_META[it.type].tag}</span>` +
        `<span class="chip-body">` +
        `<span class="chip-msg">${esc(it.message)}</span>` +
        `<span class="chip-where">${esc(where)}</span></span>` +
        `<button type="button" class="chip-ignore" title="忽略该问题（随草稿保存）">忽略</button>`;
      row.addEventListener("click", () => locateTerm(it.occ_id));
      row.querySelector(".chip-ignore").addEventListener("click", (e) => {
        e.stopPropagation();
        ignoreTerm(it.occ_id);
      });
      els.termIssues.appendChild(row);
    });
  }

  // 已忽略区
  const ignored = t.issues.filter((it) => state.termIgnores.includes(it.occ_id));
  if (ignored.length) {
    els.termIgnored.classList.remove("hidden");
    els.termIgnoredList.innerHTML = "";
    ignored.forEach((it) => {
      const chip = document.createElement("span");
      chip.className = "tray-item";
      chip.textContent = `${TERM_ISSUE_META[it.type].tag}：${it.name} ↺`;
      chip.title = "点击恢复该问题";
      chip.addEventListener("click", () => unignoreTerm(it.occ_id));
      els.termIgnoredList.appendChild(chip);
    });
  } else {
    els.termIgnored.classList.add("hidden");
  }
}

function renderTermDefs() {
  const t = state.terms;
  els.termDefs.innerHTML = "";
  const defs = [...t.definitions].sort((a, b) =>
    a.name.localeCompare(b.name, "zh") || a.scope_label.localeCompare(b.scope_label));
  defs.forEach((d) => {
    const row = document.createElement("div");
    row.className = `term-def-row ${d.status}`;
    const statusText = d.status === "duplicate" ? "重复"
      : d.status === "unused" ? "未使用" : `使用 ${d.usage_count}`;
    row.innerHTML =
      `<span class="td-name">${esc(d.name)}</span>` +
      `<span class="td-scope">${esc(d.scope_label === "全文" ? "全文定义" : d.scope_label + " 局部")}</span>` +
      `<span class="td-meta">${statusText}</span>`;
    row.title = `点击定位定义处（${d.block_id === "preamble" ? "前置说明" : (d.label || "所在条款")}）`;
    row.addEventListener("click", () => locateTerm(d.id));
    els.termDefs.appendChild(row);
  });
}

function makeTermMark(block, m) {
  const span = document.createElement("span");
  span.className = `tmark ${m.kind} ${m.status}` + (state.termIgnores.includes(m.occ_id) ? " ignored" : "");
  span.textContent = block.text.slice(m.start, m.end);
  const titles = {
    def: `定义（${m.scope_label}）——点击定位其全部使用处`,
    "use-ok": `使用：绑定到${m.scope_label}定义 ——点击定位定义处`,
    "use-undefined": "未定义术语——点击定位本处",
    "use-ambiguous": "歧义绑定：同一作用域有多个同名定义",
  };
  span.title = titles[`${m.kind}-${m.status}`] || titles[m.kind] || "";
  span.dataset.occ = m.occ_id;
  span.addEventListener("click", (e) => {
    e.stopPropagation();
    locateTerm(m.occ_id);
  });
  return span;
}

function renderTermBlock(b) {
  const div = document.createElement("div");
  div.className = "term-block clause-block" +
    (b.kind === "preamble" ? " preamble-block" : "");
  div.dataset.bid = b.id;
  div.dataset.cid = b.id;
  if (b.kind === "clause") {
    div.style.paddingLeft = (12 + Math.max(0, b.depth - 1) * 18) + "px";
    const label = document.createElement("span");
    label.className = "block-label";
    label.textContent = b.label + " ";
    div.appendChild(label);
  }
  let cursor = 0;
  (b.marks || []).forEach((m) => {
    if (m.start > cursor) div.appendChild(document.createTextNode(b.text.slice(cursor, m.start)));
    div.appendChild(makeTermMark(b, m));
    cursor = m.end;
  });
  if (cursor < b.text.length) div.appendChild(document.createTextNode(b.text.slice(cursor)));
  return div;
}

function renderTerms() {
  const t = state.terms;
  if (!t) return;
  renderTermIssues();
  renderTermDefs();
  els.termsView.innerHTML = "";
  t.blocks.forEach((b) => {
    if (!b.text.trim() && !(b.marks || []).length) return;
    els.termsView.appendChild(renderTermBlock(b));
  });
  const c = t.counts;
  const active = activeTermIssues().length;
  els.status.textContent =
    `术语审校：定义 ${t.total_definitions} 个，使用 ${t.total_usages} 处；` +
    (active ? `问题 ${active} 项（未定义 ${c.undefined} / 重复 ${c.duplicate} / 歧义 ${c.ambiguous} / 未使用 ${c.unused}）`
      : "未发现问题");
}

// 双向定位：定义 ↔ 其全部使用；歧义/未定义问题定位自身
function locateTerm(occId) {
  // 找到该 occ 的标记（定义或使用）
  const t = state.terms;
  let focus = null;
  const related = new Set([occId]);
  for (const b of t.blocks) {
    for (const m of b.marks || []) {
      if (m.occ_id === occId) {
        focus = m;
        (m.target_ids || []).forEach((id) => related.add(id));
      }
    }
  }
  // 若 occId 是使用，其 target_ids 已包含候选定义；再补上「指向该使用」的对端
  if (focus && focus.kind === "use") {
    for (const b of t.blocks) {
      for (const m of b.marks || []) {
        if (m.kind === "def" && (m.target_ids || []).includes(occId)) {
          related.add(m.occ_id);
        }
      }
    }
  }

  let scrolled = false;
  els.termsView.querySelectorAll(".tmark").forEach((node) => {
    const on = related.has(node.dataset.occ);
    node.classList.toggle("flash-pair", on);
    if (on && !scrolled) {
      node.scrollIntoView({ behavior: "smooth", block: "center" });
      scrolled = true;
    }
  });
  setTimeout(() => els.termsView.querySelectorAll(".tmark.flash-pair")
    .forEach((n) => n.classList.remove("flash-pair")), 2400);
}

function ignoreTerm(occId) {
  if (!state.termIgnores.includes(occId)) state.termIgnores.push(occId);
  saveSession();
  renderTerms();
  if (state.mode === "versions") renderIssuePanel();
  toast("已忽略该问题（保存草稿后随草稿保留，签发时一并固定）");
}

function unignoreTerm(occId) {
  state.termIgnores = state.termIgnores.filter((x) => x !== occId);
  saveSession();
  renderTerms();
}

async function switchMode(mode) {
  state.mode = mode;
  els.tabRefs.classList.toggle("active", mode === "refs");
  els.tabTerms.classList.toggle("active", mode === "terms");
  els.tabReview.classList.toggle("active", mode === "review");
  els.tabVersions.classList.toggle("active", mode === "versions");
  els.paneRefs.classList.toggle("hidden", mode !== "refs");
  els.paneTerms.classList.toggle("hidden", mode !== "terms");
  els.paneReview.classList.toggle("hidden", mode !== "review");
  els.paneVersions.classList.toggle("hidden", mode !== "versions");
  if (mode === "terms") {
    if (!state.terms) await fetchTerms();
    if (state.terms) renderTerms();
  } else if (mode === "review") {
    await fetchReview();
  } else if (mode === "versions") {
    renderIssuePanel();
    if (state.currentDraftId
        && (!state.review.status
            || state.review.status.revision !== state.savedRevision)) {
      await fetchReview({ silent: true });
      renderIssuePanel();
    }
    await refreshVersionList();
    if (state.activeVersion) {
      renderVersionViewer(state.activeVersion);
    } else {
      els.versionViewer.classList.add("hidden");
    }
  }
}

// ================================================================ 逐条复核

const REVIEW_STATE_META = {
  pending: { tag: "待确认", cls: "warn" },
  valid: { tag: "有效", cls: "ok" },
  expired: { tag: "过期", cls: "danger" },
  deleted: { tag: "已删除", cls: "muted" },
};

async function fetchReview({ silent = false } = {}) {
  if (!state.currentDraftId) {
    state.review.status = null;
    renderReview();
    return null;
  }
  state.review.busy = true;
  try {
    const st = await api(`/api/drafts/${state.currentDraftId}/review`);
    state.review.status = st;
    renderReview();
    return st;
  } catch (e) {
    if (!silent) toast("复核状态读取失败：" + e.message);
    return null;
  } finally {
    state.review.busy = false;
  }
}

function reviewDirty() {
  return !state.currentDraftId || isDraftDirty();
}

function renderReview() {
  const st = state.review.status;
  const hasDraft = !!state.currentDraftId;
  const enabled = !!(st && st.enabled);
  els.reviewOff.classList.toggle("hidden", !hasDraft || enabled);
  els.reviewSummary.classList.toggle("hidden", !hasDraft || !enabled);
  els.reviewList.classList.toggle("hidden", !hasDraft || !enabled);
  els.reviewDirty.classList.toggle("hidden", !hasDraft || !enabled || !reviewDirty());
  els.reviewEnabled.disabled = !hasDraft || state.review.busy;
  els.btnReviewRefresh.disabled = !hasDraft || state.review.busy;
  if (!hasDraft) els.reviewEnabled.checked = false;
  else if (st) els.reviewEnabled.checked = enabled;
  els.reviewOff.textContent = hasDraft
    ? "本草稿尚未开启逐条复核。勾选上方「开启逐条复核」后即可按稳定条款 ID 逐条确认存活条款；未开启复核的草稿仍按原规则签发。"
    : "当前内容尚未保存为草稿：请先在顶部「保存草稿」，保存后即可开启逐条复核并按稳定条款 ID 确认存活条款。";

  if (!hasDraft) {
    els.reviewRevision.textContent = "当前内容尚未保存为草稿，请先保存草稿";
    els.btnReviewConfirmPending.disabled = true;
    return;
  }
  if (!st) {
    els.reviewRevision.textContent = "";
    els.btnReviewConfirmPending.disabled = true;
    return;
  }
  els.reviewRevision.textContent =
    `服务器修订号 ${st.revision}` +
    (reviewDirty() ? "（本地有未保存修改）" : "（与服务器一致）");

  if (!st.enabled) {
    els.reviewSummary.innerHTML = "";
    els.reviewList.innerHTML = "";
    els.btnReviewConfirmPending.disabled = true;
    return;
  }

  const c = st.counts;
  els.reviewSummary.innerHTML =
    `<span class="rv-stat"><b>${st.total_alive}</b> 条存活条款</span>` +
    `<span class="rv-stat ok">有效 <b>${c.valid}</b></span>` +
    `<span class="rv-stat warn">待确认 <b>${c.pending}</b></span>` +
    `<span class="rv-stat bad">过期 <b>${c.expired}</b></span>` +
    (st.deleted_count
      ? `<span class="rv-stat muted">已删除 <b>${st.deleted_count}</b>（恢复后须重新确认）</span>`
      : "");

  renderReviewList(st);
  const pendingIds = st.clauses
    .filter((x) => x.state === "pending")
    .map((x) => x.clause_id);
  els.btnReviewConfirmPending.disabled =
    state.review.busy || reviewDirty() || pendingIds.length === 0;
  els.btnReviewConfirmPending.textContent =
    pendingIds.length
      ? `✓ 确认全部待确认条款（${pendingIds.length} 条）`
      : "✓ 已无待确认条款";
}

function renderReviewList(st) {
  els.reviewList.innerHTML = "";
  const alive = st.clauses.filter((x) => x.state !== "deleted");
  const deleted = st.clauses.filter((x) => x.state === "deleted");

  const makeRow = (c) => {
    const meta = REVIEW_STATE_META[c.state] || REVIEW_STATE_META.pending;
    const row = document.createElement("div");
    row.className = `rv-row ${c.state}`;
    const num = c.new_label || c.label || c.clause_id;
    row.innerHTML =
      `<span class="rv-tag ${meta.cls}">${meta.tag}</span>` +
      `<button type="button" class="rv-clause" title="在引用重排页定位该条款">` +
      `<b>${esc(num)}</b> ${esc(c.heading || "（无正文）")}` +
      `<span class="rv-id">${esc(c.clause_id)}</span></button>` +
      `<span class="rv-meta"></span>`;
    const metaBox = row.querySelector(".rv-meta");
    if (c.state === "valid") {
      metaBox.textContent = c.confirmed_revision
        ? `确认于修订 ${c.confirmed_revision}` : "已确认";
    } else if (c.state === "expired") {
      const reasons = (c.reason_texts || []).join("、")
        || "正文 / 父级 / 引用目标 / 术语绑定已变化";
      const span = document.createElement("span");
      span.className = "rv-reasons";
      span.textContent = "过期原因：" + reasons
        + (c.expire_revision ? `（自修订 ${c.expire_revision} 起）` : "");
      metaBox.appendChild(span);
    } else {
      metaBox.textContent = "尚未确认";
    }
    row.querySelector(".rv-clause").addEventListener("click", () => {
      switchMode("refs").then(() => locate(c.clause_id));
    });
    if (c.state === "pending" || c.state === "expired") {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-primary btn-sm rv-confirm";
      btn.textContent = c.state === "pending" ? "确认" : "重新确认";
      btn.disabled = reviewDirty();
      btn.title = reviewDirty() ? "请先保存草稿再确认" : "按当前已保存修订确认该条款";
      btn.addEventListener("click", () => confirmReview([c.clause_id]));
      row.appendChild(btn);
    }
    return row;
  };

  alive.forEach((c) => els.reviewList.appendChild(makeRow(c)));

  if (deleted.length) {
    const sec = document.createElement("div");
    sec.className = "rv-deleted-sec";
    sec.innerHTML =
      `<div class="rv-deleted-title">已删除条款（含级联；恢复存活后须重新确认）</div>`;
    deleted.forEach((c) => sec.appendChild(makeRow(c)));
    els.reviewList.appendChild(sec);
  }
}

async function toggleReview(enabled) {
  if (!state.currentDraftId) return;
  state.review.busy = true;
  try {
    const st = await api(`/api/drafts/${state.currentDraftId}/review`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    state.review.status = st;
    renderReview();
    renderIssuePanel();
    toast(enabled ? "已开启逐条复核" : "已关闭逐条复核（未开启时按原规则签发）");
  } catch (e) {
    toast("切换复核开关失败：" + e.message);
    els.reviewEnabled.checked = !enabled;
  } finally {
    state.review.busy = false;
  }
}

async function confirmReview(ids) {
  if (!state.currentDraftId || state.review.busy) return;
  const st = state.review.status;
  if (!st) return;
  if (reviewDirty()) {
    toast("有未保存修改，请先保存草稿再确认");
    return;
  }
  state.review.busy = true;
  renderReview();
  try {
    const res = await api(
      `/api/drafts/${state.currentDraftId}/review/confirm`, {
        method: "POST",
        body: JSON.stringify({ revision: st.revision, clause_ids: ids }),
      });
    state.review.status = res;
    renderReview();
    renderIssuePanel();
    toast(`已确认 ${ids.length} 条条款（修订 ${res.revision}）`);
  } catch (e) {
    if (e.status === 409) {
      const cur = e.payload && e.payload.detail && e.payload.detail.current_revision;
      toast(`修订号已过期（服务器最新修订 ${cur ?? "?"}），正在重新载入…`);
      await loadDraft(state.currentDraftId);
      await fetchReview();
    } else {
      toast("确认失败：" + e.message);
    }
  } finally {
    state.review.busy = false;
    renderReview();
  }
}

// ---------------------------------------------------------------- 生效版本签发

function renderIssuePanel() {
  const dirty = state.currentDraftId ? isDraftDirty() : true;
  if (!state.currentDraftId) {
    els.issueRevision.textContent = "当前内容尚未保存为草稿（签发时会先提示保存）";
  } else {
    els.issueRevision.textContent =
      `当前草稿修订号 ${state.savedRevision ?? "?"}` +
      (dirty ? "；有未保存修改，签发时将先保存并形成新修订号" : "；与服务器一致");
  }
  els.btnIssue.disabled = state.issueBusy;
  renderIssueReviewHint();
}

function renderIssueReviewHint() {
  const box = document.getElementById("issue-review-hint");
  if (!box) return;
  const st = state.review.status;
  if (!state.currentDraftId || !st || !st.enabled) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");
  const c = st.counts;
  const ready = c.pending === 0 && c.expired === 0;
  box.className = "issue-review-hint " + (ready ? "ok" : "bad");
  box.innerHTML =
    `<span class="rv-tag ${ready ? "ok" : "warn"}">逐条复核已开启</span>` +
    `存活 ${st.total_alive} 条 · 有效 <b>${c.valid}</b>` +
    ` · 待确认 <b>${c.pending}</b>` +
    ` · 过期 <b>${c.expired}</b>` +
    (ready
      ? " — 全部有效，可随终审一并签发"
      : " — 存在待确认 / 过期条款，签发将被整体拒绝") +
    ` <button type="button" class="btn btn-sm" id="issue-review-goto">前往逐条复核 →</button>`;
  box.querySelector("#issue-review-goto").addEventListener("click",
    () => switchMode("review"));
}

function renderBlockers(payload) {
  const detail = payload && payload.detail;
  const blockers = detail && detail.blockers;
  els.issueSuccess.classList.add("hidden");
  els.issueBlockers.classList.remove("hidden");
  els.issueBlockers.innerHTML = "";

  const head = document.createElement("div");
  head.className = "issue-blockers-head";
  head.textContent = (detail && detail.message)
    || "存在未通过的引用或术语问题，已拒绝签发（草稿未修改）";
  els.issueBlockers.appendChild(head);

  const refs = blockers ? blockers.reference_issues : [];
  const terms = blockers ? blockers.term_issues : [];

  // 逐条复核未完成（缺失 / 过期）
  const review = detail && detail.review;
  if (review) {
    const sec = document.createElement("div");
    sec.className = "issue-blocker-sec";
    const missing = review.missing || [];
    const expired = review.expired || [];
    sec.innerHTML =
      `<div class="issue-blocker-title">逐条复核未通过：待确认 ${missing.length} 条`
      + ` · 已过期 ${expired.length} 条（全部存活条款须为有效确认，草稿未修改）</div>`;
    [...expired, ...missing].forEach((c) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "issue-blocker-row review-row "
        + (missing.includes(c) ? "pending" : "expired");
      const tag = missing.includes(c) ? "待确认" : "过期";
      const why = (c.reason_texts || []).length
        ? `（${c.reason_texts.join("、")}）` : "";
      row.innerHTML =
        `<span class="chip-tag">${tag}</span>`
        + `<span class="chip-msg">${esc(c.new_label || c.label || c.clause_id)}`
        + ` ${esc(c.heading || "")}${esc(why)}</span>`
        + `<span class="chip-goto">前往逐条复核页定位 →</span>`;
      row.addEventListener("click", () => switchMode("review"));
      sec.appendChild(row);
    });
    els.issueBlockers.appendChild(sec);
  }

  if (refs.length) {
    const sec = document.createElement("div");
    sec.className = "issue-blocker-sec";
    sec.innerHTML = `<div class="issue-blocker-title">引用问题 ${refs.length} 项</div>`;
    refs.forEach((it) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = `issue-blocker-row ref ${it.type}`;
      const tag = ISSUE_META[it.type] ? ISSUE_META[it.type].tag : it.type;
      row.innerHTML =
        `<span class="chip-tag">${tag}</span>` +
        `<span class="chip-msg">${esc(it.message)}</span>` +
        `<span class="chip-goto">前往引用重排页定位 →</span>`;
      row.addEventListener("click", () => {
        switchMode("refs").then(() => locate(it.clause_id, it.target_id));
      });
      sec.appendChild(row);
    });
    els.issueBlockers.appendChild(sec);
  }

  if (terms.length) {
    const sec = document.createElement("div");
    sec.className = "issue-blocker-sec";
    sec.innerHTML = `<div class="issue-blocker-title">术语问题 ${terms.length} 项（已扣除固定忽略项）</div>`;
    terms.forEach((it) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = `issue-blocker-row term ${it.type}`;
      row.innerHTML =
        `<span class="chip-tag">${TERM_ISSUE_META[it.type].tag}</span>` +
        `<span class="chip-msg">${esc(it.message)}</span>` +
        `<span class="chip-goto">前往术语审校页定位 →</span>`;
      row.addEventListener("click", () => {
        switchMode("terms").then(() => locateTerm(it.occ_id));
      });
      sec.appendChild(row);
    });
    els.issueBlockers.appendChild(sec);
  }
}

async function issueVersion() {
  if (state.issueBusy) return;
  return doIssue(false);
}

async function doIssue(retriedAfterStale) {
  state.issueBusy = true;
  els.btnIssue.disabled = true;
  els.btnIssue.textContent = "签发校验中…";
  els.issueBlockers.classList.add("hidden");
  els.issueSuccess.classList.add("hidden");
  // 首次进入：有未保存修改（或尚未保存）时先落草稿，取得最新修订号再签发。
  // 409 自动重试路径已在调用前完成保存，这里不再重复保存。
  if (!retriedAfterStale
      && (!state.currentDraftId || isDraftDirty())) {
    const saved = await ensureDraftSaved();
    if (!saved) {
      state.issueBusy = false;
      els.btnIssue.disabled = false;
      els.btnIssue.textContent = "🔖 从当前草稿签发生效版本";
      return;
    }
    state.savedSnapshot = draftSnapshot();
    renderIssuePanel();
  }
  try {
    const res = await api(`/api/drafts/${state.currentDraftId}/issue`, {
      method: "POST",
      body: JSON.stringify({ revision: state.savedRevision }),
    });
    els.issueBlockers.classList.add("hidden");
    els.issueSuccess.classList.remove("hidden");
    els.issueSuccess.innerHTML = res.reused
      ? `✓ 该草稿快照此前已签发：<b>v${res.version_no}</b>（不可变版本号不变，未产生新版本）`
      : `✓ 已签发生效版本 <b>v${res.version_no}</b>，编号、引用改写与术语绑定已固化，后续编辑不会覆盖`;
    toast(res.reused ? `同一草稿重复提交，返回原版本 v${res.version_no}`
      : `生效版本 v${res.version_no} 已签发`);
    await refreshVersionList();
    renderAuditForm();
    await openVersion(res.id);
    refreshDrafts(state.currentDraftId);
    fetchReview({ silent: true });
  } catch (e) {
    if (e.status === 422) {
      renderBlockers(e.payload);
    } else if (e.status === 409) {
      const cur = e.payload && e.payload.detail && e.payload.detail.current_revision;
      // 常见情形：本地内容早已改过但未重新保存，所持修订号过期。
      // 若服务器草稿仍是另一份内容，则把当前编辑保存为新修订号后自动重试一次；
      // 若服务器内容与本地快照一致（真正的并发改动），交用户确认后重新载入。
      if (!retriedAfterStale) {
        let serverDraft = null;
        try {
          serverDraft = await api(`/api/drafts/${state.currentDraftId}`);
        } catch (_) { /* 草稿可能已被删除 */ }
        const localSnapshot = draftSnapshot();
        const serverSnapshot = JSON.stringify({
          content: serverDraft ? serverDraft.content : "",
          order: serverDraft ? serverDraft.order : [],
          deleted: serverDraft ? serverDraft.deleted : [],
          term_ignores: serverDraft ? serverDraft.term_ignores : [],
        });
        if (serverDraft && serverSnapshot !== localSnapshot) {
          const saved = await ensureDraftSaved();
          if (!saved) return;
          state.savedSnapshot = draftSnapshot();
          renderIssuePanel();
          return doIssue(true);
        }
      }
      els.issueBlockers.classList.remove("hidden");
      els.issueBlockers.innerHTML = "";
      const head = document.createElement("div");
      head.className = "issue-blockers-head";
      head.textContent = "草稿在签发校验期间已被修改（修订号变化），已拒绝且未产生版本。";
      els.issueBlockers.appendChild(head);
      const reload = document.createElement("button");
      reload.type = "button";
      reload.className = "btn btn-sm";
      reload.textContent = `载入服务器最新草稿（修订号 ${cur ?? "?"}）后重试`;
      reload.addEventListener("click", async () => {
        await loadDraft(state.currentDraftId);
        renderIssuePanel();
      });
      els.issueBlockers.appendChild(reload);
    } else if (e.status === 404) {
      toast("草稿不存在，请重新选择");
    } else {
      toast("签发失败：" + e.message);
    }
  } finally {
    state.issueBusy = false;
    els.btnIssue.disabled = false;
    els.btnIssue.textContent = "🔖 从当前草稿签发生效版本";
  }
}

async function refreshVersionList() {
  if (!state.currentDraftId) {
    state.versions = [];
    state.versionsLoadedFor = null;
    renderVersionList();
    renderAuditForm();
    return;
  }
  try {
    state.versions = await api(
      `/api/drafts/${state.currentDraftId}/versions`);
    state.versionsLoadedFor = state.currentDraftId;
  } catch (e) {
    state.versions = [];
  }
  renderVersionList();
  renderAuditForm();
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString();
}

function renderVersionList() {
  els.versionList.innerHTML = "";
  if (!state.currentDraftId) {
    const empty = document.createElement("div");
    empty.className = "version-empty";
    empty.textContent = "请先在顶部选择或保存草稿；签发后不可变版本会列在这里。";
    els.versionList.appendChild(empty);
    return;
  }
  if (!state.versions.length) {
    const empty = document.createElement("div");
    empty.className = "version-empty";
    empty.textContent = "该草稿尚无生效版本。通过上方终审后即可签发。";
    els.versionList.appendChild(empty);
    return;
  }
  state.versions.forEach((v) => {
    const s = v.summary;
    const row = document.createElement("button");
    row.type = "button";
    row.className = "version-row"
      + (state.activeVersion && state.activeVersion.id === v.id ? " active" : "");
    row.innerHTML =
      `<span class="version-no">v${v.version_no}</span>` +
      `<span class="version-meta">` +
      `<span class="version-time">${fmtTime(v.issued_at)}</span>` +
      `<span class="version-stats">条款 ${s.clause_count}` +
      (s.deleted_count ? `（删除 ${s.deleted_count}）` : "") +
      ` · 引用改写 ${s.reference_rewritten}/${s.reference_total}` +
      ` · 术语绑定 ${s.term_binding_count}` +
      (s.ignored_count ? ` · 已忽略 ${s.ignored_count}` : "") +
      ` · 签发于修订 ${v.revision}</span></span>` +
      `<span class="version-open">查看 / 定位 →</span>`;
    row.addEventListener("click", () => openVersion(v.id));
    els.versionList.appendChild(row);
  });
}

async function openVersion(id) {
  try {
    const v = await api(`/api/versions/${id}`);
    state.activeVersion = v;
    renderVersionList();
    renderVersionViewer(v);
  } catch (e) {
    toast("版本读取失败：" + e.message);
  }
}

// 版本查看器内的定位（不与编辑区联动；支持条款 id 与术语 occ_id）
function vLocate(ids, occIds = []) {
  const viewer = els.versionViewer;
  let scrolled = false;
  ids.forEach((cid) => {
    viewer.querySelectorAll(`.clause-block[data-cid="${CSS.escape(cid)}"]`)
      .forEach((node) => {
        if (!scrolled) {
          node.scrollIntoView({ behavior: "smooth", block: "center" });
          scrolled = true;
        }
        node.classList.remove("flash");
        void node.offsetWidth;
        node.classList.add("flash");
      });
  });
  const occSet = new Set(occIds);
  viewer.querySelectorAll(".vtmark").forEach((node) => {
    if (occSet.has(node.dataset.occ)) {
      node.classList.remove("flash-pair");
      void node.offsetWidth;
      node.classList.add("flash-pair");
      if (!scrolled) {
        node.scrollIntoView({ behavior: "smooth", block: "center" });
        scrolled = true;
      }
    }
  });
  setTimeout(() => viewer.querySelectorAll(".vtmark.flash-pair")
    .forEach((n) => n.classList.remove("flash-pair")), 2400);
}

function vAppendBody(container, clause, mode) {
  const text = clause.before_text;
  let cursor = 0;
  (clause.refs || []).forEach((m) => {
    if (m.start > cursor) {
      container.appendChild(document.createTextNode(text.slice(cursor, m.start)));
    }
    const span = document.createElement("span");
    span.className = `mark ${m.status}`;
    span.textContent = mode === "before" ? m.text : m.new_text;
    span.title = mode === "after"
      ? "签发时的引用改写结果（点击定位目标条款）"
      : "签发时的原文引用（点击定位条款）";
    span.addEventListener("click", (e) => {
      e.stopPropagation();
      vLocate([m.target_id || clause.id]);
    });
    container.appendChild(span);
    cursor = m.end;
  });
  if (cursor < text.length) {
    container.appendChild(document.createTextNode(text.slice(cursor)));
  }
}

function vMakeBlock(clause, mode, isDeleted) {
  const block = document.createElement("div");
  block.className = "clause-block" + (isDeleted ? " deleted-clause" : "");
  block.dataset.cid = clause.id;
  const depth = clause.depth != null ? clause.depth : (clause.path || []).length;
  block.style.paddingLeft = (12 + Math.max(0, depth - 1) * 18) + "px";
  const label = document.createElement("span");
  label.className = "block-label" + (mode === "after" ? " new" : "");
  label.textContent = (mode === "after" ? clause.new_label : clause.label) + " ";
  block.appendChild(label);
  if (isDeleted) {
    const ribbon = document.createElement("span");
    ribbon.className = "deleted-ribbon";
    ribbon.textContent = "已删除";
    block.appendChild(ribbon);
  }
  vAppendBody(block, clause, mode);
  return block;
}

function vTermMark(block, m, related) {
  const span = document.createElement("span");
  span.className = `vtmark ${m.kind} ${m.status}` +
    (related.has(m.occ_id) ? " flash-pair" : "");
  span.textContent = block.text.slice(m.start, m.end);
  span.dataset.occ = m.occ_id;
  span.title = m.kind === "def"
    ? `定义（${m.scope_label}）——点击定位其使用处`
    : "使用——点击定位其绑定的定义";
  span.addEventListener("click", (e) => {
    e.stopPropagation();
    const ids = new Set([m.occ_id]);
    (m.target_ids || []).forEach((t) => ids.add(t));
    vLocate([], [...ids]);
  });
  return span;
}

function renderVersionViewer(v) {
  const s = v.summary;
  const refs = v.ref_result;
  const terms = v.term_result;
  const byId = new Map(refs.clauses.map((c) => [c.id, c]));
  const deletedIds = new Set(refs.deleted_clauses.map((c) => c.id));
  const termBlockById = new Map(terms.blocks.map((b) => [b.id, b]));
  const defById = new Map(terms.definitions.map((d) => [d.id, d]));

  els.versionViewer.classList.remove("hidden");
  els.versionViewer.innerHTML = "";

  const head = document.createElement("div");
  head.className = "panel-head vv-head";
  head.innerHTML =
    `<h2>生效版本 v${v.version_no}（不可变快照）</h2>` +
    `<div class="hint">${esc(v.title)} · 签发于 ${fmtTime(v.issued_at)}` +
    ` · 对应草稿修订号 ${v.revision}` +
    ` · 条款 ${s.clause_count}（删除 ${s.deleted_count}）` +
    ` · 引用改写 ${s.reference_rewritten}/${s.reference_total}` +
    ` · 术语定义 ${s.term_definition_count} / 绑定 ${s.term_binding_count}` +
    (s.ignored_count ? ` · 已忽略术语 ${s.ignored_count}` : "") + `</div>`;
  els.versionViewer.appendChild(head);

  // —— 引用：签发时的编号与改写（并排） ——
  const refSec = document.createElement("div");
  refSec.className = "vv-section";
  refSec.innerHTML =
    `<div class="vv-section-title">签发正文与条款编号（左：原文；右：签发时重编号与引用改写；点击彩色引用可定位目标条款）</div>`;
  const cols = document.createElement("div");
  cols.className = "compare-cols";
  const colBefore = document.createElement("div");
  colBefore.className = "col";
  colBefore.innerHTML = `<div class="col-title">原文（已删除条款置灰）</div>`;
  const beforeView = document.createElement("div");
  beforeView.className = "doc-view";
  if (refs.preamble) {
    const p = document.createElement("div");
    p.className = "preamble-block clause-block";
    p.dataset.cid = "preamble";
    p.textContent = refs.preamble;
    beforeView.appendChild(p);
  }
  refs.clauses.forEach((c) =>
    beforeView.appendChild(vMakeBlock(c, "before", deletedIds.has(c.id))));
  colBefore.appendChild(beforeView);

  const colAfter = document.createElement("div");
  colAfter.className = "col";
  colAfter.innerHTML = `<div class="col-title">签发版本（新编号）</div>`;
  const afterView = document.createElement("div");
  afterView.className = "doc-view";
  if (refs.preamble) {
    const p = document.createElement("div");
    p.className = "preamble-block clause-block";
    p.dataset.cid = "preamble";
    p.textContent = refs.preamble;
    afterView.appendChild(p);
  }
  refs.result.forEach((c) => afterView.appendChild(vMakeBlock(c, "after", false)));
  colAfter.appendChild(afterView);
  cols.appendChild(colBefore);
  cols.appendChild(colAfter);
  refSec.appendChild(cols);

  // 引用改写清单
  const rewriteList = document.createElement("div");
  rewriteList.className = "vv-binding-list";
  s.rewrites.forEach((r) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "vv-binding-row";
    row.innerHTML =
      `<span class="vv-bw">${esc(r.clause_label)}</span>` +
      `<span class="vv-bt">${esc(r.text)} ⇒ <b>${esc(r.new_text)}</b></span>` +
      `<span class="chip-goto">定位 →</span>`;
    row.addEventListener("click", () => vLocate([r.clause_id, r.target_id]));
    rewriteList.appendChild(row);
  });
  if (s.rewrites.length) {
    const t = document.createElement("div");
    t.className = "vv-section-title";
    t.textContent = `引用改写记录（${s.rewrites.length} 处，签发时固定）`;
    refSec.appendChild(t);
    refSec.appendChild(rewriteList);
  }
  els.versionViewer.appendChild(refSec);

  // —— 术语：绑定快照 ——
  const termSec = document.createElement("div");
  termSec.className = "vv-section";
  termSec.innerHTML =
    `<div class="vv-section-title">术语绑定快照（点击术语可在定义与使用处之间定位）</div>`;

  const bindList = document.createElement("div");
  bindList.className = "vv-binding-list";
  s.term_bindings.forEach((b) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "vv-binding-row";
    row.innerHTML =
      `<span class="vv-bw">${esc(b.where || "前置说明")}</span>` +
      `<span class="vv-bt">“${esc(b.name)}” ⇒ ` +
      `${esc(b.scope_label || "")}定义「${esc(b.def_name)}」` +
      (b.def_where ? `（${esc(b.def_where)}）` : "") + `</span>` +
      `<span class="chip-goto">定位 →</span>`;
    row.addEventListener("click", () =>
      vLocate([b.block_id, (defById.get(b.def_id) || {}).block_id]
        .filter(Boolean), [b.use_id, b.def_id].filter(Boolean)));
    bindList.appendChild(row);
  });
  termSec.appendChild(bindList);

  if (s.ignored_issues.length) {
    const ignored = document.createElement("div");
    ignored.className = "vv-ignored";
    ignored.textContent = "签发时固定忽略的术语问题："
      + s.ignored_issues.map((i) => i.message).join("；");
    termSec.appendChild(ignored);
  }

  const tview = document.createElement("div");
  tview.className = "doc-view vv-term-view";
  terms.blocks.forEach((b) => {
    if (!b.text.trim() && !(b.marks || []).length) return;
    const div = document.createElement("div");
    div.className = "term-block clause-block"
      + (b.kind === "preamble" ? " preamble-block" : "");
    div.dataset.cid = b.id;
    if (b.kind === "clause") {
      div.style.paddingLeft = (12 + Math.max(0, b.depth - 1) * 18) + "px";
      const label = document.createElement("span");
      label.className = "block-label";
      label.textContent = b.label + " ";
      div.appendChild(label);
    }
    // 初始无关联高亮，关联集合由点击时按 occ 计算
    let cursor = 0;
    (b.marks || []).forEach((m) => {
      if (m.start > cursor) {
        div.appendChild(document.createTextNode(b.text.slice(cursor, m.start)));
      }
      div.appendChild(vTermMark(b, m, new Set()));
      cursor = m.end;
    });
    if (cursor < b.text.length) {
      div.appendChild(document.createTextNode(b.text.slice(cursor)));
    }
    tview.appendChild(div);
  });
  termSec.appendChild(tview);
  els.versionViewer.appendChild(termSec);

  els.versionViewer.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ================================================================ 版本差异审计

const AUDIT_KIND_LABELS = {
  body_changed: ["正文改变", "warn"],
  reorder: ["层级 / 顺序调整", "primary"],
  numbering_only: ["仅编号改写", "muted"],
  removed: ["删除", "danger"],
  lifecycle_deleted: ["删除", "danger"],
  added: ["新增", "ok"],
  lifecycle_restored: ["恢复存活", "ok"],
  ref_changed: ["引用改写", "warn"],
  ref_added: ["新增引用", "ok"],
  ref_removed: ["引用消失", "danger"],
  term_use_added: ["术语使用新增", "ok"],
  term_use_removed: ["术语使用消失", "muted"],
  term_binding_lost: ["术语绑定丢失", "danger"],
  term_binding_ambiguous: ["术语绑定变歧义", "purple"],
  term_binding_changed: ["术语改绑", "cyan"],
};

function auditKindTag(kind) {
  const [label, cls] = AUDIT_KIND_LABELS[kind] || [kind, "muted"];
  return `<span class="audit-tag tag-${cls}">${label}</span>`;
}

function renderAuditForm() {
  const opts = (sel, keepId) => {
    const prev = keepId;
    sel.innerHTML = `<option value="">— 选择版本 —</option>` +
      state.versions.map((v) =>
        `<option value="${v.id}"${String(v.id) === String(prev) ? " selected" : ""}>`
        + `v${v.version_no}（修订 ${v.revision} · ${fmtTime(v.issued_at)}）</option>`)
        .join("");
  };
  opts(els.auditOld, state.audit.oldId);
  opts(els.auditNew, state.audit.newId);
  const ok = els.auditOld.value && els.auditNew.value
    && els.auditOld.value !== els.auditNew.value;
  els.btnAudit.disabled = !ok || state.audit.busy;
}

async function runAudit() {
  const oldId = Number(els.auditOld.value);
  const newId = Number(els.auditNew.value);
  if (!oldId || !newId || oldId === newId) return;
  state.audit.busy = true;
  els.btnAudit.disabled = true;
  els.btnAudit.textContent = "审计中…";
  els.auditStatus.textContent = "";
  try {
    const res = await api("/api/versions/audit", {
      method: "POST",
      body: JSON.stringify({ old_version_id: oldId, new_version_id: newId }),
    });
    state.audit = { oldId, newId, result: res, busy: false };
    renderAuditResult(res);
    toast(`v${res.old.version_no} → v${res.new.version_no} 差异审计已生成`);
    // 同步拉取「应用到当前草稿」预览（只读；草稿不在该版本所属草稿时由后端拒绝）
    fetchApplyPreview();
  } catch (e) {
    state.audit.busy = false;
    els.auditStatus.textContent = "审计失败：" + e.message;
    els.btnAudit.textContent = "🔍 生成版本差异审计";
    renderAuditForm();
  }
}

function auditLoc(loc) {
  if (!loc || !loc.present) return `<span class="audit-loc missing">不存在</span>`;
  const num = loc.issued_label || loc.label || "";
  const stateTxt = loc.alive ? "" : ` · <em>该版已删除</em>`;
  return `<span class="audit-loc${loc.alive ? "" : " dead"}">`
    + `<b>${esc(num)}</b>${stateTxt}</span>`;
}

function renderAuditResult(a) {
  els.btnAudit.textContent = "🔍 生成版本差异审计";
  const box = els.auditResult;
  box.classList.remove("hidden");
  box.innerHTML = "";
  const c = a.counts;

  const head = document.createElement("div");
  head.className = "audit-head";
  head.innerHTML =
    `<div class="audit-title">差异审计：<b>v${a.old.version_no}</b>（修订 ${a.old.revision}）`
    + ` → <b>v${a.new.version_no}</b>（修订 ${a.new.revision}）</div>`
    + `<div class="audit-counts">`
    + `<span>正文改变 <b>${c.clause_body}</b></span>`
    + `<span>层级/顺序 <b>${c.clause_reorder}</b></span>`
    + `<span>仅编号改写 <b>${c.clause_numbering_only}</b></span>`
    + `<span class="tc-danger">删除 <b>${c.clause_removed}</b></span>`
    + `<span class="tc-ok">新增 <b>${c.clause_added}</b></span>`
    + `<span>引用差异 <b>${c.ref_changed}</b></span>`
    + `<span>术语差异 <b>${c.term_changed}</b></span>`
    + (c.preamble_changed ? `<span class="tc-warn">前置说明已改</span>` : "")
    + `</div>`;
  box.appendChild(head);

  // 条款差异
  box.appendChild(auditSection(
    "条款差异（按原条款稳定 ID 对齐；点击行在下方两版快照中定位该条款）",
    a.clause_diffs, (d) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "audit-row";
      const oldLoc = d.old ? auditLoc(d.old) : `<span class="audit-loc missing">无</span>`;
      const newLoc = d.new ? auditLoc(d.new) : `<span class="audit-loc missing">无</span>`;
      row.innerHTML =
        auditKindTag(d.kind) +
        `<span class="audit-locs">${oldLoc}<span class="audit-arrow">→</span>${newLoc}</span>`
        + `<span class="audit-msg">${esc(d.message)}</span>`;
      row.addEventListener("click", () =>
        auditLocate(d.clause_id, d.clause_id));
      return row;
    }));

  // 引用差异
  box.appendChild(auditSection(
    "引用差异（逐条比较目标集合、区间表达与原文；点击定位引用所在条款 / 目标）",
    a.ref_diffs, (d) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "audit-row";
      const detail = document.createElement("span");
      detail.className = "audit-msg";
      detail.textContent = d.message;
      const refs = document.createElement("span");
      refs.className = "audit-refcmp";
      refs.innerHTML = d.old
        ? `<span class="rc-old">${esc(d.old.expr)}</span><span class="audit-arrow">→</span>`
        : "";
      refs.innerHTML += d.new
        ? `<span class="rc-new">${esc(d.new.expr)}</span>`
        : "";
      row.innerHTML = auditKindTag(d.kind)
        + `<span class="audit-locs"><span class="audit-loc">`
        + `${esc(d.owner_old_label || "")}<span class="audit-arrow">→</span>${esc(d.owner_new_label || "")}`
        + `</span></span>`;
      row.appendChild(refs);
      row.appendChild(detail);
      const targetIds = (d.targets || []).map((t) => t.clause_id);
      row.addEventListener("click", () =>
        auditLocate(d.clause_id, d.clause_id, targetIds));
      return row;
    }));

  // 术语差异
  box.appendChild(auditSection(
    "术语绑定差异（按出现处对齐，比较每处使用绑定的定义）",
    a.term_diffs, (d) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "audit-row";
      const where = d.in_preamble ? "前置说明" : "条款内";
      row.innerHTML = auditKindTag(d.kind)
        + `<span class="audit-locs"><span class="audit-loc">${esc(where)}</span></span>`
        + `<span class="audit-msg">${esc(d.message)}</span>`;
      row.addEventListener("click", () => auditLocate(
        d.in_preamble ? "preamble" : d.block_id,
        d.in_preamble ? "preamble" : d.block_id));
      return row;
    }));

  // 最短受影响链
  const impacts = a.impacts || [];
  box.appendChild(auditSection(
    "最短受影响链（基于新版存活引用图：SCC 合并循环组，沿反向引用 BFS；链上节点、根因均可点击定位两版条款）",
    impacts, (im) => {
      const wrap = document.createElement("div");
      wrap.className = "audit-impact" + (im.cycle ? " is-cycle" : "");
      const top = document.createElement("button");
      top.type = "button";
      top.className = "audit-row impact-top";
      top.innerHTML =
        `<span class="audit-tag tag-${im.cycle ? "purple" : "danger"}">`
        + `${im.distance === 0 ? "直接影响" : `传导 ${im.distance} 跳`}</span>`
        + `<span class="audit-locs"><span class="audit-loc">${im.cycle ? "↺ " : ""}${esc(im.label)}</span></span>`
        + `<span class="audit-msg">${im.details.length} 项直接差异</span>`;
      top.addEventListener("click", () =>
        auditLocate(im.clause_id, im.clause_id, im.member_ids));
      wrap.appendChild(top);

      const chain = document.createElement("div");
      chain.className = "audit-chain";
      im.chain.forEach((step, idx) => {
        if (idx > 0) chain.appendChild(arrowNode("▸"));
        const node = document.createElement("button");
        node.type = "button";
        node.className = "audit-chain-node"
          + (step.kind === "group" ? " is-group" : "")
          + (step.terminal ? " terminal" : "");
        node.textContent = (step.cycle ? "↺ " : "")
          + step.members.map((m) => m.label).join("、")
          + (step.terminal && step.members[0] && step.members[0].alive_new === false
            ? "（新版已删除）" : "");
        const ids = step.members.map((m) => m.id).filter((x) => x !== "preamble");
        node.addEventListener("click", () => auditLocate(
          step.kind === "preamble" ? "preamble" : (ids[0] || null),
          step.kind === "preamble" ? "preamble" : (ids[0] || null),
          ids));
        chain.appendChild(node);
      });
      wrap.appendChild(chain);

      (im.details || []).forEach((det) => {
        const d = document.createElement("div");
        d.className = "audit-detail";
        d.textContent = "· " + det.message;
        d.addEventListener("click", () =>
          auditLocate(det.owner_id, det.owner_id));
        wrap.appendChild(d);
      });
      return wrap;
    }));

  renderAuditSides(a);
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

function arrowNode(txt) {
  const s = document.createElement("span");
  s.className = "audit-arrow sep";
  s.textContent = txt;
  return s;
}

function auditSection(title, items, render) {
  const sec = document.createElement("div");
  sec.className = "audit-section";
  const t = document.createElement("div");
  t.className = "audit-section-title";
  t.textContent = `${title}（${items.length}）`;
  sec.appendChild(t);
  if (!items.length) {
    const e = document.createElement("div");
    e.className = "audit-empty";
    e.textContent = "无差异";
    sec.appendChild(e);
    return sec;
  }
  items.forEach((item) => sec.appendChild(render(item)));
  return sec;
}

// ---- 两版并排快照（点击差异行在两侧条款间定位）----

function renderAuditSides(a) {
  const sec = document.createElement("div");
  sec.className = "audit-sides";
  sec.innerHTML = `<div class="audit-section-title">两版条款快照（左：旧版原文；右：新版签发正文，已删除置灰；点击上方差异行可在两侧定位）</div>`;
  const cols = document.createElement("div");
  cols.className = "compare-cols";
  const mkCol = (sideKey, side, mode) => {
    const col = document.createElement("div");
    col.className = "col";
    col.innerHTML =
      `<div class="col-title">${sideKey === "old" ? "旧版 v" : "新版 v"}${side.version_no}</div>`;
    const view = document.createElement("div");
    view.className = "doc-view audit-doc";
    view.dataset.side = sideKey;
    if (side.preamble) {
      const p = document.createElement("div");
      p.className = "preamble-block clause-block";
      p.dataset.cid = "preamble";
      p.textContent = side.preamble;
      view.appendChild(p);
    }
    const deletedIds = new Set((side.deleted_clauses || []).map((c) => c.id));
    if (mode === "issued") {
      side.result.forEach((c) => view.appendChild(vMakeBlock(c, "after", false)));
      (side.deleted_clauses || []).forEach((mini) => {
        const full = (side.clauses || []).find((c) => c.id === mini.id);
        if (full) view.appendChild(vMakeBlock(full, "before", true));
      });
    }
    col.appendChild(view);
    return col;
  };
  cols.appendChild(mkCol("old", a.sides.old, "issued"));
  cols.appendChild(mkCol("new", a.sides.new, "issued"));
  sec.appendChild(cols);
  els.auditResult.appendChild(sec);
}

function auditLocate(oldCid, newCid, alsoIds = []) {
  const box = els.auditResult;
  const sides = state.audit.result
    ? [state.audit.result.sides.old, state.audit.result.sides.new] : [];
  let scrolled = false;
  const flash = (view, ids) => {
    const all = new Set(ids.filter(Boolean));
    view.querySelectorAll(".clause-block").forEach((node) => {
      if (all.has(node.dataset.cid)) {
        if (!scrolled) {
          node.scrollIntoView({ behavior: "smooth", block: "center" });
          scrolled = true;
        }
        node.classList.remove("flash");
        void node.offsetWidth;
        node.classList.add("flash");
      }
    });
  };
  const oldView = box.querySelector('.audit-doc[data-side="old"]');
  const newView = box.querySelector('.audit-doc[data-side="new"]');
  if (oldView) {
    const valid = new Set(sides[0] ? [
      ...sides[0].order,
      ...(sides[0].deleted_clauses || []).map((c) => c.id),
      "preamble",
    ] : []);
    const ids = [oldCid, ...alsoIds].filter((x) => valid.has(x));
    flash(oldView, ids);
    if (!scrolled && ids.length) {
      const n = oldView.querySelector(`[data-cid="${CSS.escape(ids[0])}"]`);
      if (n) n.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }
  if (newView) {
    const valid = new Set(sides[1] ? [
      ...sides[1].order,
      ...(sides[1].deleted_clauses || []).map((c) => c.id),
      "preamble",
    ] : []);
    const ids = [newCid, ...alsoIds].filter((x) => valid.has(x));
    const n = ids.length && newView.querySelector(`[data-cid="${CSS.escape(ids[0])}"]`);
    if (n && !scrolled) {
      n.scrollIntoView({ behavior: "smooth", block: "center" });
      scrolled = true;
    }
    flash(newView, ids);
  }
  setTimeout(() => box.querySelectorAll(".clause-block.flash")
    .forEach((n) => n.classList.remove("flash")), 2400);
}

// ================================================================ 应用到当前草稿

const APPLY_DIM_LABELS = {
  body: "正文",
  structure: "父子顺序 / 层级",
  lifecycle: "删除 / 恢复",
  presence: "新增 / 移除",
};

function applyStale() {
  const p = state.apply.preview;
  if (!p) return false;
  return p.draft_revision !== state.savedRevision || isDraftDirty();
}

function applySelectedPairs() {
  const out = [];
  for (const [cid, dims] of state.apply.selections) {
    if (dims.size) out.push({ clause_id: cid, dimensions: [...dims] });
  }
  return out;
}

async function fetchApplyPreview() {
  const { oldId, newId } = state.audit;
  if (!state.currentDraftId || !oldId || !newId) {
    els.applyPane.classList.add("hidden");
    return;
  }
  state.apply.busy = true;
  els.btnApplyFetch.disabled = true;
  els.btnApplyFetch.textContent = "载入中…";
  try {
    const res = await api(
      `/api/drafts/${state.currentDraftId}/apply-preview`, {
        method: "POST",
        body: JSON.stringify({ old_version_id: oldId, new_version_id: newId }),
      });
    state.apply.preview = res;
    state.apply.seen = true;
    pruneApplySelections();
    els.applyPane.classList.remove("hidden");
    renderApply();
  } catch (e) {
    els.applyPane.classList.add("hidden");
    els.auditStatus.textContent = "合并预览不可用：" + e.message;
  } finally {
    state.apply.busy = false;
    els.btnApplyFetch.disabled = false;
    els.btnApplyFetch.textContent = "重新载入合并预览";
  }
}

function pruneApplySelections() {
  const p = state.apply.preview;
  const allowed = new Map();
  p.items.forEach((it) => {
    allowed.set(it.clause_id, new Set(
      it.dimensions.filter((d) => !d.conflict).map((d) => d.dim)));
  });
  for (const [cid, dims] of [...state.apply.selections]) {
    const ok = allowed.get(cid);
    if (!ok) { state.apply.selections.delete(cid); continue; }
    const kept = [...dims].filter((d) => ok.has(d));
    if (kept.length) state.apply.selections.set(cid, new Set(kept));
    else state.apply.selections.delete(cid);
  }
}

function renderApply() {
  const p = state.apply.preview;
  if (!p) { els.applyPane.classList.add("hidden"); return; }
  els.applyPane.classList.remove("hidden");
  renderApplyBanner(p);
  renderApplyItems(p);
  renderApplySides(p);
  updateApplySummary();
}

function renderApplyBanner(p) {
  els.applyBanner.className = "apply-banner";
  const stale = applyStale();
  if (stale) {
    els.applyBanner.classList.add("stale");
    els.applyBanner.innerHTML =
      "⚠ 当前草稿与生成预览时的修订号 "
      + `<b>${p.draft_revision}</b>`
      + " 不一致（已保存为其他修订或存在未保存修改），请先「重新载入合并预览」"
      + (state.savedRevision == null ? "" : `（服务器最新修订号 ${state.savedRevision}）`);
  } else if (p.counts.conflict_items) {
    els.applyBanner.classList.add("has-conflict");
    els.applyBanner.innerHTML =
      `🟣 有 <b>${p.counts.conflict_items}</b> 项差异与当前草稿冲突（已改动同一条款或其父级 / 子树），`
      + "对应勾选项已禁用；冲突项只能在当前草稿中人工处理。";
  } else {
    els.applyBanner.classList.add("clean");
    els.applyBanner.textContent = "无冲突：可勾选需要带入的变更后提交。";
  }
}

function renderApplyItems(p) {
  els.applyItems.innerHTML = "";
  if (!p.items.length) {
    const empty = document.createElement("div");
    empty.className = "audit-empty";
    empty.textContent = "旧版与新版之间没有可应用的条款差异（仅编号改写会在合并后自动重算）。";
    els.applyItems.appendChild(empty);
    return;
  }
  p.items.forEach((it) => {
    const wrap = document.createElement("div");
    wrap.className = "apply-item" + (it.conflict ? " is-conflict" : "");

    const top = document.createElement("button");
    top.type = "button";
    top.className = "audit-row apply-top";
    const oldLoc = it.locs.old ? auditLoc(it.locs.old)
      : `<span class="audit-loc missing">无</span>`;
    const newLoc = it.locs.new ? auditLoc(it.locs.new)
      : `<span class="audit-loc missing">无</span>`;
    const dLoc = it.locs.draft ? auditLoc(it.locs.draft)
      : `<span class="audit-loc missing">无</span>`;
    top.innerHTML =
      auditKindTag(it.kind) +
      `<span class="audit-locs">`
      + `<span class="loc-col" title="旧版（基线）">${oldLoc}</span>`
      + `<span class="audit-arrow">→</span>`
      + `<span class="loc-col" title="新版（候选）">${newLoc}</span>`
      + `<span class="audit-arrow">⇔</span>`
      + `<span class="loc-col" title="当前草稿">${dLoc}</span>`
      + `</span><span class="audit-msg">${esc(it.message)}</span>`;
    top.addEventListener("click", () =>
      applyLocate(it.clause_id, [it.clause_id]));
    wrap.appendChild(top);

    const checks = document.createElement("div");
    checks.className = "apply-dims";
    const sel = state.apply.selections.get(it.clause_id) || new Set();
    it.dimensions.forEach((dim) => {
      const lab = document.createElement("label");
      lab.className = "apply-dim" + (dim.conflict ? " blocked" : "");
      lab.title = dim.conflict
        ? dim.conflict_reasons.join("；")
        : `带入「${dim.label}」变更`;
      lab.innerHTML =
        `<input type="checkbox" data-cid="${it.clause_id}" `
        + `data-dim="${dim.dim}"${dim.conflict ? " disabled" : ""}`
        + `${sel.has(dim.dim) ? " checked" : ""} />`
        + `<span>${esc(dim.label)}</span>`;
      lab.querySelector("input").addEventListener("change", (e) => {
        e.stopPropagation();
        let cur = state.apply.selections.get(it.clause_id);
        if (!cur) { cur = new Set(); state.apply.selections.set(it.clause_id, cur); }
        if (e.target.checked) cur.add(dim.dim); else cur.delete(dim.dim);
        if (!cur.size) state.apply.selections.delete(it.clause_id);
        updateApplySummary();
      });
      checks.appendChild(lab);
    });
    wrap.appendChild(checks);

    if (it.conflict) {
      const reasons = document.createElement("div");
      reasons.className = "apply-conflict-reasons";
      reasons.textContent = "⛔ " + [...new Set(
        it.dimensions.flatMap((d) => d.conflict_reasons))].join("；");
      wrap.appendChild(reasons);
    }
    els.applyItems.appendChild(wrap);
  });
}

function updateApplySummary() {
  const p = state.apply.preview;
  if (!p) return;
  const pairs = applySelectedPairs();
  const nDims = pairs.reduce((n, x) => n + x.dimensions.length, 0);
  const blocked = p.counts.conflict_items;
  els.applySummary.textContent =
    `已勾选 ${nDims} 项变更（${pairs.length} 条条款）`
    + (blocked ? ` · 冲突条目 ${blocked}` : "");
  const selectable = [];
  p.items.forEach((it) => {
    const dims = it.dimensions.filter((d) => !d.conflict).map((d) => d.dim);
    if (dims.length) selectable.push([it.clause_id, dims]);
  });
  const allOn = selectable.length > 0 && selectable.every(
    ([cid, dims]) => {
      const cur = state.apply.selections.get(cid);
      return cur && dims.every((d) => cur.has(d));
    });
  els.applySelectAll.checked = allOn;
  els.btnApplySubmit.disabled = state.apply.busy || nDims === 0 || applyStale();
  els.btnApplySubmit.textContent = state.apply.busy
    ? "合并校验中…"
    : "✓ 应用勾选项到当前草稿（一次事务保存新修订）";
}

function renderApplySides(p) {
  els.applySides.innerHTML = "";
  const sec = document.createElement("div");
  sec.className = "audit-sides";
  sec.innerHTML =
    `<div class="audit-section-title">三侧条款快照（旧版基线 → 新版候选 ⇔ 当前草稿；`
    + `点击上方条目可定位三侧对应条款；删除条款置灰）</div>`;
  const cols = document.createElement("div");
  cols.className = "compare-cols apply-cols";
  const mkCol = (sideKey, title, side) => {
    const col = document.createElement("div");
    col.className = "col";
    col.innerHTML = `<div class="col-title">${esc(title)}</div>`;
    const view = document.createElement("div");
    view.className = "doc-view audit-doc apply-doc";
    view.dataset.side = sideKey;
    if (side.preamble) {
      const pre = document.createElement("div");
      pre.className = "preamble-block clause-block";
      pre.dataset.cid = "preamble";
      pre.textContent = side.preamble;
      view.appendChild(pre);
    }
    side.result.forEach((c) => view.appendChild(vMakeBlock(c, "after", false)));
    (side.deleted_clauses || []).forEach((mini) => {
      const full = (side.clauses || []).find((c) => c.id === mini.id);
      if (full) view.appendChild(vMakeBlock(full, "before", true));
    });
    col.appendChild(view);
    return col;
  };
  cols.appendChild(mkCol("old", `旧版 v${p.sides.old.version_no}（基线）`, p.sides.old));
  cols.appendChild(mkCol("new", `新版 v${p.sides.new.version_no}（候选）`, p.sides.new));
  cols.appendChild(mkCol("draft", "当前草稿", p.sides.draft));
  sec.appendChild(cols);
  els.applySides.appendChild(sec);
}

function applyLocate(cid, alsoIds = []) {
  const box = els.applySides;
  const sides = state.apply.preview
    ? [state.apply.preview.sides.old, state.apply.preview.sides.new,
       state.apply.preview.sides.draft] : [];
  let scrolled = false;
  box.querySelectorAll(".apply-doc").forEach((view, i) => {
    const valid = new Set(sides[i] ? [
      ...sides[i].order,
      ...(sides[i].deleted_clauses || []).map((c) => c.id),
      "preamble",
    ] : []);
    const ids = [cid, ...alsoIds].filter((x) => valid.has(x));
    view.querySelectorAll(".clause-block").forEach((node) => {
      if (ids.includes(node.dataset.cid)) {
        if (!scrolled) {
          node.scrollIntoView({ behavior: "smooth", block: "center" });
          scrolled = true;
        }
        node.classList.remove("flash");
        void node.offsetWidth;
        node.classList.add("flash");
      }
    });
  });
  setTimeout(() => box.querySelectorAll(".clause-block.flash")
    .forEach((n) => n.classList.remove("flash")), 2400);
}

function renderApplyBlockers(payload) {
  const detail = payload && payload.detail;
  const banner = els.applyBanner;
  banner.className = "apply-banner rejected";
  banner.innerHTML = "";
  const head = document.createElement("div");
  head.className = "apply-banner-head";
  head.textContent = (detail && detail.message)
    || "应用被拒绝（草稿未修改）";
  banner.appendChild(head);

  (detail.conflicts || []).forEach((cf) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "issue-blocker-row term";
    row.innerHTML =
      `<span class="chip-tag">冲突</span>`
      + `<span class="chip-msg">${esc(cf.clause_id)} · `
      + `${esc(APPLY_DIM_LABELS[cf.dimension] || cf.dimension)}：${esc(cf.message)}</span>`
      + `<span class="chip-goto">定位 →</span>`;
    row.addEventListener("click", () => applyLocate(cf.clause_id));
    banner.appendChild(row);
  });

  const blockers = detail.blockers;
  if (blockers) {
    (blockers.reference_issues || []).forEach((it) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = `issue-blocker-row ref ${it.type}`;
      row.innerHTML =
        `<span class="chip-tag">${(ISSUE_META[it.type] || {}).tag || it.type}</span>`
        + `<span class="chip-msg">${esc(it.message)}</span>`
        + `<span class="chip-goto">前往引用重排页定位 →</span>`;
      row.addEventListener("click", () => {
        switchMode("refs").then(() => locate(it.clause_id, it.target_id));
      });
      banner.appendChild(row);
    });
    (blockers.term_issues || []).forEach((it) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = `issue-blocker-row term ${it.type}`;
      row.innerHTML =
        `<span class="chip-tag">${TERM_ISSUE_META[it.type].tag}</span>`
        + `<span class="chip-msg">${esc(it.message)}</span>`
        + `<span class="chip-goto">前往术语审校页定位 →</span>`;
      row.addEventListener("click", () => {
        switchMode("terms").then(() => locateTerm(it.occ_id));
      });
      banner.appendChild(row);
    });
  }
  banner.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function submitApply() {
  const p = state.apply.preview;
  if (!p || state.apply.busy) return;
  const pairs = applySelectedPairs();
  if (!pairs.length) { toast("请先勾选要带入的变更"); return; }
  if (applyStale()) { toast("预览已过期，请先重新载入合并预览"); return; }
  state.apply.busy = true;
  updateApplySummary();
  try {
    const res = await api(
      `/api/drafts/${state.currentDraftId}/apply-audit`, {
        method: "POST",
        body: JSON.stringify({
          old_version_id: state.audit.oldId,
          new_version_id: state.audit.newId,
          revision: p.draft_revision,
          old_fingerprint: p.old.fingerprint,
          new_fingerprint: p.new.fingerprint,
          selections: pairs,
        }),
      });
    // 通过：一次事务已保存新修订，用返回的草稿刷新工作区（不再另行 PUT）
    els.input.value = res.draft.content;
    state.order = res.draft.order;
    state.deleted = res.draft.deleted;
    state.termIgnores = res.draft.term_ignores || [];
    state.savedRevision = res.revision;
    state.previewSel = [];
    state.previewResult = null;
    state.previewStale = false;
    els.previewPane.classList.add("hidden");
    await runAnalyze(true);
    state.savedSnapshot = draftSnapshot();
    refreshDrafts(state.currentDraftId);
    renderIssuePanel();
    await refreshVersionList();
    await fetchReview({ silent: true });
    toast(`已应用 ${pairs.length} 条条款的勾选变更，草稿保存为修订 ${res.revision}`);
    await fetchApplyPreview();
  } catch (e) {
    if (e.status === 409) {
      const cur = e.payload && e.payload.detail && e.payload.detail.current_revision;
      const banner = els.applyBanner;
      banner.className = "apply-banner rejected";
      banner.innerHTML =
        `<div class="apply-banner-head">${esc(e.message)}</div>`;
      const reload = document.createElement("button");
      reload.type = "button";
      reload.className = "btn btn-sm";
      reload.textContent = `载入服务器最新草稿（修订号 ${cur ?? "?"}）后重试`;
      reload.addEventListener("click", async () => {
        await loadDraft(state.currentDraftId);
        await fetchApplyPreview();
      });
      banner.appendChild(reload);
      banner.scrollIntoView({ behavior: "smooth", block: "center" });
    } else if (e.status === 422) {
      renderApplyBlockers(e.payload);
      // 冲突 / 阻塞均不写库：重新拉预览确认服务端草稿修订未变
      await fetchApplyPreview();
    } else {
      toast("应用失败：" + e.message);
      updateApplySummary();
    }
  } finally {
    state.apply.busy = false;
    updateApplySummary();
  }
}
async function refreshDrafts(selectId) {
  try {
    const drafts = await api("/api/drafts");
    els.draftSelect.innerHTML =
      `<option value="">— 已保存草稿（${drafts.length}）—</option>` +
      drafts.map((d) =>
        `<option value="${d.id}" data-title="${esc(d.title)}">`
        + `${esc(d.title)}（修订 ${d.revision}`
        + (d.version_count ? ` / ${d.version_count} 个生效版本` : "")
        + "）</option>").join("");
    if (selectId) els.draftSelect.value = String(selectId);
  } catch (_) { /* 后端不可用时静默 */ }
}

function selectedDraftTitle() {
  const opt = [...els.draftSelect.options]
    .find((o) => o.value === String(state.currentDraftId));
  return opt ? (opt.dataset.title || opt.textContent) : "";
}

// 当前编辑内容相对服务器草稿是否有改动（标题不参与签发快照）
function draftSnapshot() {
  return JSON.stringify({
    content: els.input.value,
    order: state.order,
    deleted: state.deleted,
    term_ignores: state.termIgnores,
  });
}

function isDraftDirty() {
  return state.savedSnapshot !== draftSnapshot();
}

// 返回 true 表示已保存且拿到最新修订号；创建新草稿时先向用户要标题
async function ensureDraftSaved() {
  if (state.currentDraftId && !isDraftDirty()) {
    return true;
  }
  let title;
  if (state.currentDraftId) {
    title = selectedDraftTitle();
    if (!title) return false;
  } else {
    title = prompt("签发前需先保存当前草稿，请输入标题：",
      "合同草稿 " + new Date().toLocaleString());
    if (!title) return false;
  }
  const payload = {
    title, content: els.input.value, order: state.order,
    deleted: state.deleted, term_ignores: state.termIgnores,
  };
  try {
    if (state.currentDraftId) {
      const r = await api(`/api/drafts/${state.currentDraftId}`, {
        method: "PUT", body: JSON.stringify(payload),
      });
      state.savedRevision = r.revision;
    } else {
      const r = await api("/api/drafts", {
        method: "POST", body: JSON.stringify(payload),
      });
      state.currentDraftId = r.id;
      state.savedRevision = r.revision;
    }
    refreshDrafts(state.currentDraftId);
    fetchReview({ silent: true });
    return true;
  } catch (e) {
    toast("保存失败：" + e.message);
    return false;
  }
}

async function saveDraft() {
  const text = els.input.value;
  if (!text.trim()) {
    toast("请先粘贴并解析合同文本");
    return;
  }
  let title;
  if (state.currentDraftId) {
    title = selectedDraftTitle() || prompt("草稿标题：");
    if (!title) return;
  } else {
    title = prompt("给草稿起个标题：", "合同草稿 " + new Date().toLocaleString());
    if (!title) return;
  }
  const payload = {
    title, content: text, order: state.order, deleted: state.deleted,
    term_ignores: state.termIgnores,
  };
  try {
    if (state.currentDraftId) {
      const r = await api(`/api/drafts/${state.currentDraftId}`, {
        method: "PUT", body: JSON.stringify(payload),
      });
      state.savedRevision = r.revision;
    } else {
      const r = await api("/api/drafts", {
        method: "POST", body: JSON.stringify(payload),
      });
      state.currentDraftId = r.id;
      state.savedRevision = r.revision;
    }
    state.savedSnapshot = draftSnapshot();
    toast("草稿已保存");
    refreshDrafts(state.currentDraftId);
    renderIssuePanel();
    fetchReview({ silent: true });
  } catch (e) {
    toast("保存失败：" + e.message);
  }
}

async function loadDraft(id) {
  const d = await api(`/api/drafts/${id}`);
  state.currentDraftId = d.id;
  els.input.value = d.content;
  state.order = d.order;
  state.deleted = d.deleted;
  state.termIgnores = d.term_ignores || [];
  state.savedRevision = d.revision;
  state.savedSnapshot = draftSnapshot();
  state.activeVersion = null;
  state.versionsLoadedFor = null;
  state.audit = { oldId: null, newId: null, result: null, busy: false };
  state.apply = { busy: false, preview: null, selections: new Map(), seen: false };
  state.review = { status: null, busy: false };
  els.versionViewer.classList.add("hidden");
  els.auditResult.classList.add("hidden");
  els.applyPane.classList.add("hidden");
  els.auditStatus.textContent = "";
  els.issueBlockers.classList.add("hidden");
  els.issueSuccess.classList.add("hidden");
  state.previewSel = [];
  state.previewResult = null;
  state.previewStale = false;
  els.previewPane.classList.add("hidden");
  await runAnalyze(true);
  await fetchReview({ silent: true });
  toast(`已载入草稿：${d.title}`);
}

// ---------------------------------------------------------------- 本地会话（localStorage 自动暂存）

function saveSession() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({
      text: els.input.value, order: state.order, deleted: state.deleted,
      term_ignores: state.termIgnores,
    }));
  } catch (_) {}
}

async function restoreSession() {
  let s;
  try { s = JSON.parse(localStorage.getItem(LS_KEY) || "null"); } catch (_) {}
  if (s && s.text) {
    els.input.value = s.text;
    state.order = s.order || [];
    state.deleted = s.deleted || [];
    state.termIgnores = s.term_ignores || [];
    state.previewSel = [];
    state.previewResult = null;
    state.previewStale = false;
    await runAnalyze(true);
  }
}

// ---------------------------------------------------------------- 示例

const EXAMPLE = `鉴于甲方拟采购乙方的系统集成服务，双方经平等协商，就本合同项下条款达成如下约定：

第1条 定义
本合同下列用语具有如下含义：“服务”指乙方按照本合同约定向甲方提供的系统集成及运维支持。
“关联方”是指直接或间接控制甲方或乙方的主体。
本合同所称“工作日”为除法定节假日外的自然日。
第1.1条 术语来源
本合同术语以第1条解释为准，详见第1.1.1条的举例。
第1.1.1条 术语举例
本条所称“交付物”是指乙方按约提交的文档与系统；交付物应随验收移交。

第2条 服务内容
乙方应按照第1条的定义提供完整“服务”，并保证“最终用户”能够正常使用；
“最终用户”应配合乙方工作。服务范围详见第3条至第5条的约定，验收标准见第9条。
如第若干条所述之特殊情形发生，乙方有权顺延工期。

第3条 服务期限
本条所称“服务期”是指自合同生效之日起十二个月；服务期内乙方应持续履约，
每个“工作日”均须安排值班。违约责任适用第7条。
第3.1条 期限计算
服务期按自然日计算；起算日见第3条，续约见第3.2条。
第3.2条 续约安排
续约应提前三十日书面通知；计算方式同第3.1条。

第4条 费用与支付
本条所称“工作日”是指银行对公营业日；
合同总金额及支付安排由本条各款约定。

第4.1条 支付方式
甲方应在收到乙方付款申请后十五日内付款，付款币种为人民币。

第4.2条 发票
乙方应开具合规增值税发票；第4.1条至第4.2条共同构成支付条款。

第5条 验收
服务期届满前，甲方应在“工作日”内组织验收；逾期未提出异议的，视为验收合格。

第6条 保密
双方对在履约过程中知悉的商业秘密负有保密义务，违反保密义务的责任按第7条处理。

第7条 违约责任
任何一方违反本合同约定的，应承担违约责任；保密义务相关违约适用第6条，
且本条责任范围涵盖第5条至第7条项下的全部义务。
`;

function loadExample() {
  els.input.value = EXAMPLE;
  state.order = [];
  state.deleted = [];
  state.termIgnores = [];
  state.currentDraftId = null;
  state.savedRevision = null;
  state.savedSnapshot = null;
  state.versions = [];
  state.versionsLoadedFor = null;
  state.activeVersion = null;
  state.audit = { oldId: null, newId: null, result: null, busy: false };
  state.apply = { busy: false, preview: null, selections: new Map(), seen: false };
  state.review = { status: null, busy: false };
  state.previewSel = [];
  state.previewResult = null;
  state.previewStale = false;
  els.previewPane.classList.add("hidden");
  els.versionViewer.classList.add("hidden");
  els.auditResult.classList.add("hidden");
  els.applyPane.classList.add("hidden");
  els.auditStatus.textContent = "";
  renderAuditForm();
  renderReview();
  els.draftSelect.value = "";
  if (state.mode === "versions") renderIssuePanel();
  runAnalyze(true).then(() => {
    toast("示例已载入：可切换到「定义术语审校」查看未定义/未使用等问题；试删第3条观察局部定义随删除失效");
  }).catch(() => {});
}

// ---------------------------------------------------------------- 事件绑定

els.btnParse.addEventListener("click", () => {
  // 重新解析：若文本变化则重置顺序
  const structureChanged = state.lastParsedText !== els.input.value;
  if (structureChanged) {
    state.order = [];
    state.deleted = [];
  }
  runAnalyze(true).catch(() => {});
});
els.btnExample.addEventListener("click", loadExample);
els.btnSave.addEventListener("click", saveDraft);
els.tabRefs.addEventListener("click", () => switchMode("refs"));
els.tabTerms.addEventListener("click", () => switchMode("terms"));
els.tabReview.addEventListener("click", () => switchMode("review"));
els.tabVersions.addEventListener("click", () => switchMode("versions"));
els.btnIssue.addEventListener("click", issueVersion);
els.btnReviewRefresh.addEventListener("click", () => fetchReview());
els.reviewEnabled.addEventListener("change", () =>
  toggleReview(els.reviewEnabled.checked));
els.btnReviewConfirmPending.addEventListener("click", () => {
  const st = state.review.status;
  if (!st) return;
  const ids = st.clauses
    .filter((x) => x.state === "pending")
    .map((x) => x.clause_id);
  if (ids.length) confirmReview(ids);
});
els.btnReviewSave.addEventListener("click", async () => {
  await saveDraft();
  await fetchReview();
  renderIssuePanel();
});
els.btnAudit.addEventListener("click", runAudit);
els.btnApplyFetch.addEventListener("click", fetchApplyPreview);
els.btnApplySubmit.addEventListener("click", submitApply);
els.applySelectAll.addEventListener("change", () => {
  const p = state.apply.preview;
  if (!p) return;
  if (els.applySelectAll.checked) {
    p.items.forEach((it) => {
      const dims = it.dimensions.filter((d) => !d.conflict).map((d) => d.dim);
      if (dims.length) state.apply.selections.set(it.clause_id, new Set(dims));
    });
  } else {
    state.apply.selections.clear();
  }
  renderApplyItems(p);
  updateApplySummary();
});
els.auditOld.addEventListener("change", () => {
  state.audit.oldId = Number(els.auditOld.value) || null;
  renderAuditForm();
});
els.auditNew.addEventListener("change", () => {
  state.audit.newId = Number(els.auditNew.value) || null;
  renderAuditForm();
});
els.draftSelect.addEventListener("change", async (e) => {
  if (e.target.value) {
    await loadDraft(Number(e.target.value));
    if (state.mode === "versions") {
      renderIssuePanel();
      await refreshVersionList();
    }
  }
});
els.input.addEventListener("input", debounce(saveSession, 500));
els.input.addEventListener("input", () => {
  // 正文变化：旧预演结果不得直接确认（预演签名含正文）
  if (state.previewResult) invalidatePreview();
  // 生效版本页的修订号提示同步更新（未保存修改）
  if (state.mode === "versions") renderIssuePanel();
  // 复核页：有未保存修改时禁用确认并提示先保存
  if (state.mode === "review" && state.review.status
      && state.review.status.enabled) {
    renderReview();
  }
  // 合并应用预览：草稿变脏后旧结果标记过期，禁止提交
  if (state.apply.preview) {
    renderApplyBanner(state.apply.preview);
    updateApplySummary();
  }
});

// 删除影响预演
els.btnPreview.addEventListener("click", openPreview);
els.btnRePreview.addEventListener("click", openPreview);
els.btnConfirmDelete.addEventListener("click", confirmPreview);
els.btnCancelPreview.addEventListener("click", closePreview);

// 启动
refreshDrafts();
restoreSession();
