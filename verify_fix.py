"""两处错判修复的端到端核验（不依赖 FastAPI，直连复核核心 + SQLite 层）。

运行：python3 verify_fix.py
场景：
A. 只有 3.1/3.2 无第3条：确认子条款后补入真实第3条 → 子条款过期、
   新第3条待确认、签发关卡拒绝且不产生版本。
B. 定义条款内在既有定义前插入普通说明：定义与使用语义不变 →
   使用条款确认保持有效；删除定义 / 改绑 → 使用条款过期。
C. 历史确认（无 sig_version，只存哈希）→ 保守过期，重确认后方可生效。
D. 过期粘性：改正文过期后即使文本改回，确认也不自动复活。
E. 纯编号重排（同父换序）→ 确认保持有效。
F. HTTP 全链路（FastAPI TestClient 不可用时跳过）：签发 422、无版本行。
"""
import copy
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.review import (  # noqa: E402
    SIG_VERSION,
    build_parts,
    build_status,
    evaluate_gate,
    migrate_confirmations,
)

FAILS = []


def check(name, cond, detail=""):
    print(("  ✅" if cond else "  ❌"), name, ("— " + detail if detail else ""))
    if not cond:
        FAILS.append(name)


def state_map(status):
    return {c["clause_id"]: c for c in status["clauses"]}


def confirm(parts, cids, revision=1):
    """模拟 /review/confirm 写入的新口径记录。"""
    return [{
        "clause_id": cid,
        "revision": revision,
        "confirmed_at": 1000,
        "sig": copy.deepcopy(parts[cid]["sig"]),
        "sig_version": SIG_VERSION,
        "expired": False,
    } for cid in cids]


def save_migrate(content, records, revision, order=None, deleted=None):
    _p, _c, _r, _t, parts = build_parts(content, order or [], deleted or [])
    migrated = migrate_confirmations(records, parts, revision)
    return parts, build_status(True, migrated, parts, revision), migrated


# ---------------------------------------------------------------- A 虚父级
print("A. 只有 3.1/3.2（虚父级）→ 补入真实第3条")
text_no3 = "第1条 甲方义务。\n第3.1条 子项一，按第1条执行。\n第3.2条 子项二。\n"
_p, clauses, _r, _t, parts = build_parts(text_no3, [], [])
ids = [c.id for c in clauses]
check("解析出 c-1/c-3-1/c-3-2 而无 c-3",
      ids == ["c-1", "c-3-1", "c-3-2"], str(ids))
recs = confirm(parts, ids, revision=1)
st = build_status(True, recs, parts, 1)
check("全部确认后均 valid", st["counts"]["valid"] == 3, str(st["counts"]))

text_with3 = ("第1条 甲方义务。\n第3条 新增的真实父条款。\n"
              "第3.1条 子项一，按第1条执行。\n第3.2条 子项二。\n")
parts2, st2, recs2 = save_migrate(text_with3, recs, revision=2)
m2 = state_map(st2)
check("c-3-1 父级绑定变化 → expired", m2["c-3-1"]["state"] == "expired"
      and m2["c-3-1"]["reasons"] == ["parent"], str(m2["c-3-1"]["reasons"]))
check("c-3-2 父级绑定变化 → expired", m2["c-3-2"]["state"] == "expired"
      and m2["c-3-2"]["reasons"] == ["parent"], str(m2["c-3-2"]["reasons"]))
check("c-1 父级不受影响 → valid", m2["c-1"]["state"] == "valid")
check("新 c-3 为 pending", m2["c-3"]["state"] == "pending")
gate = evaluate_gate(recs2, parts2)
check("签发关卡拒绝", not gate["ok"]
      and [c["clause_id"] for c in gate["missing"]] == ["c-3"]
      and {c["clause_id"] for c in gate["expired"]} == {"c-3-1", "c-3-2"},
      f"missing={[c['clause_id'] for c in gate['missing']]} "
      f"expired={[c['clause_id'] for c in gate['expired']]}")

# 重新确认全部存活条款后放行
alive = [cid for cid, p in parts2.items() if p["alive"]]
recs3 = confirm(parts2, alive, revision=2)
gate2 = evaluate_gate(recs3, parts2)
check("重新确认后关卡通过", gate2["ok"])


# ---------------------------------------------------------------- B 术语
print("B. 定义前插入普通说明（仅字符位移）vs 删除/改绑定义")
text_def = (
    "第1条 定义条款。\n“服务”是指乙方提供的运维支持。\n"
    "第2条 使用条款。\n乙方应按约定提供服务，不得中断。\n"
    "第3条 无关条款。\n其他事项另行约定。\n"
)
_p, _c, _r, tr, pdef = build_parts(text_def, [], [])
# 第2条的无引号「服务」唯一绑定 c-1 的定义
m_c2 = next(m for b in tr["blocks"] if b["id"] == "c-2"
            for m in b["marks"] if m["kind"] == "use")
check("c-2 的「服务」唯一绑定定义",
      m_c2["status"] == "ok" and len(m_c2["target_ids"]) == 1,
      f"{m_c2['status']} {m_c2['target_ids']}")
recs = confirm(pdef, ["c-1", "c-2", "c-3"], revision=1)

# 在定义之前插入普通说明（不增删定义/使用，仅偏移漂移）
text_shift = (
    "第1条 定义条款。\n本条为普通说明文字，仅作背景介绍，不含任何定义。\n"
    "“服务”是指乙方提供的运维支持。\n"
    "第2条 使用条款。\n乙方应按约定提供服务，不得中断。\n"
    "第3条 无关条款。\n其他事项另行约定。\n"
)
_p, cs_shift, _r, tr2, pshift = build_parts(text_shift, [], [])
check("定义 occ_id 确实随偏移漂移（旧 id 已不存在）",
      m_c2["target_ids"][0] not in {d["id"] for d in tr2["definitions"]})
terms_same = pdef["c-2"]["sig"]["terms"] == pshift["c-2"]["sig"]["terms"]
check("c-2 术语签名不随字符位移变化", terms_same)
body_changed_c1 = pdef["c-1"]["sig"]["body"] != pshift["c-1"]["sig"]["body"]
check("c-1 正文签名变化（其自身确认过期）", body_changed_c1)
parts_s, st_s, recs_s = save_migrate(text_shift, recs, revision=2)
ms = state_map(st_s)
check("c-2 使用条款保持 valid", ms["c-2"]["state"] == "valid",
      str(ms["c-2"].get("reasons")))
check("c-1 定义条款因正文变化 expired", ms["c-1"]["state"] == "expired")
check("c-3 无关条款保持 valid", ms["c-3"]["state"] == "valid")

# 删除定义 → c-2 术语绑定丢失，过期
text_del = (
    "第1条 定义条款。\n本条不再给出术语定义。\n"
    "第2条 使用条款。\n乙方应按约定提供服务，不得中断。\n"
    "第3条 无关条款。\n其他事项另行约定。\n"
)
# 先按当前文本重新确认 c-1（模拟用户在上一修订重确认），c-2 仍是跨修订有效
recs_ok = [r for r in recs_s if r["clause_id"] != "c-1"]
recs_ok += confirm(pshift, ["c-1"], revision=2)
parts_d, st_d, recs_d = save_migrate(text_del, recs_ok, revision=3)
md = state_map(st_d)
check("删除定义后 c-2 变 expired（terms）",
      md["c-2"]["state"] == "expired" and "terms" in md["c-2"]["reasons"],
      str(md["c-2"]["reasons"]))
gate = evaluate_gate(recs_d, parts_d)
check("删除定义后签发关卡拒绝", not gate["ok"])

# 改绑：新增更近的局部定义（本条所称）使 c-2 绑定改变
text_rebind = (
    "第1条 定义条款。\n“服务”是指乙方提供的运维支持。\n"
    "第2条 使用条款。\n本条所称“服务”为甲方自研平台。\n"
    "乙方应按约定提供服务，不得中断。\n"
    "第3条 无关条款。\n其他事项另行约定。\n"
)
# 以 text_def 的有效确认为基线
recs_base = confirm(pdef, ["c-1", "c-2", "c-3"], revision=1)
parts_rb, st_rb, recs_rb = save_migrate(text_rebind, recs_base, revision=2)
mr = state_map(st_rb)
check("局部定义改绑后 c-2 expired（terms）",
      mr["c-2"]["state"] == "expired" and "terms" in mr["c-2"]["reasons"],
      str(mr["c-2"]["reasons"]))


# ---------------------------------------------------------------- C 历史记录
print("C. 历史确认只存哈希（无 sig_version）→ 保守过期")
legacy = [{
    "clause_id": cid,
    "revision": 1,
    "confirmed_at": 900,
    "sig": copy.deepcopy(pdef[cid]["sig"]),   # 哈希碰巧与当前完全一致
    "expired": False,
} for cid in ("c-1", "c-2", "c-3")]
st_l = build_status(True, legacy, pdef, 1)
ml = state_map(st_l)
check("即使哈希一致，旧记录一律 expired",
      all(ml[c]["state"] == "expired" and ml[c]["reasons"] == ["legacy_sig"]
          for c in ("c-1", "c-2", "c-3")),
      str([(c, ml[c]["state"], ml[c]["reasons"]) for c in ("c-1", "c-2", "c-3")]))
gate_l = evaluate_gate(legacy, pdef)
check("旧记录无法通过签发关卡", not gate_l["ok"]
      and len(gate_l["expired"]) == 3)
migrated_l = migrate_confirmations(legacy, pdef, 2)
check("迁移后旧记录落库为 expired + legacy_sig",
      all(r["expired"] and r["expire_reasons"] == ["legacy_sig"]
          for r in migrated_l))
reconfirmed = confirm(pdef, ("c-1", "c-2", "c-3"), revision=2)
check("重新确认后关卡通过", evaluate_gate(reconfirmed, pdef)["ok"])


# ---------------------------------------------------------------- D 粘性
print("D. 过期粘性：文本改回原样不自动复活")
_p, _c, _r, _t, p0 = build_parts(text_def, [], [])
r0 = confirm(p0, ["c-2"], revision=1)
text_ed = text_def.replace("不得中断", "应持续可用")
p1, s1, r1 = save_migrate(text_ed, r0, revision=2)
m1 = state_map(s1)
check("改正文后 c-2 expired(body)",
      m1["c-2"]["state"] == "expired" and m1["c-2"]["reasons"] == ["body"])
p2, s2, r2 = save_migrate(text_def, r1, revision=3)
m2b = state_map(s2)
check("文本改回后 c-2 仍 expired（粘性）",
      m2b["c-2"]["state"] == "expired"
      and m2b["c-2"]["expire_revision"] == 2,
      str((m2b["c-2"]["state"], m2b["c-2"]["expire_revision"])))


# ---------------------------------------------------------------- E 纯重排
print("E. 同父兄弟换序（纯编号变化）→ 确认保持有效")
text_order = ("第1条 甲方按第2条付款。\n第2条 乙方交付。\n"
              "第3条 验收安排。\n")
_p, _c, _r, _t, po = build_parts(text_order, [], [])
ro = confirm(po, ["c-1", "c-2", "c-3"], revision=1)
# 把 c-1 与 c-2 在同父（顶层）换序：order 为先序
_pn, sn, rn = save_migrate(text_order, ro, revision=2,
                           order=["c-2", "c-1", "c-3"])
mn = state_map(sn)
check("换序后三条仍 valid",
      all(mn[c]["state"] == "valid" for c in ("c-1", "c-2", "c-3")),
      str([(c, mn[c]["state"]) for c in ("c-1", "c-2", "c-3")]))
check("换序后引用目标签名不变（仍绑 c-2）",
      po["c-1"]["sig"]["refs"] ==
      build_parts(text_order, ["c-2", "c-1", "c-3"], [])[4]["c-1"]["sig"]["refs"])


# ---------------------------------------------------------------- F HTTP 全链路
print("F. HTTP 全链路（保存/复核/签发）")
try:
    from fastapi.testclient import TestClient  # noqa: F401
except Exception as exc:  # 环境无 fastapi/httpx
    print("  ⏭️ 跳过（", type(exc).__name__, "，本地无 FastAPI 运行时）")
else:
    import json
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    from fastapi.testclient import TestClient
    from app.main import app, init_db
    init_db()
    client = TestClient(app)

    def draft(content, revision, title="t"):
        return client.post("/api/drafts", json={
            "title": title, "content": content}) if revision == 1 else None

    # 场景 A 全链路
    r = client.post("/api/drafts", json={"title": "A", "content": text_no3})
    did = r.json()["id"]
    client.post(f"/api/drafts/{did}/review", json={"enabled": True})
    rev = client.get(f"/api/drafts/{did}").json()["revision"]
    r = client.post(f"/api/drafts/{did}/review/confirm",
                    json={"revision": rev,
                          "clause_ids": ["c-1", "c-3-1", "c-3-2"]})
    check("HTTP 确认成功（valid=3）",
          r.json()["counts"]["valid"] == 3, str(r.json()["counts"]))
    before_versions = len(client.get(f"/api/drafts/{did}/versions").json())
    r = client.put(f"/api/drafts/{did}",
                   json={"title": "A", "content": text_with3})
    rev = r.json()["revision"]
    r = client.post(f"/api/drafts/{did}/issue", json={"revision": rev})
    check("HTTP 补入真实第3条后签发 422", r.status_code == 422,
          str(r.status_code))
    after_versions = len(client.get(f"/api/drafts/{did}/versions").json())
    check("422 未产生任何版本", before_versions == after_versions == 0)
    body = r.json()["detail"]
    check("422 明细含 missing c-3 与 expired c-3-1/c-3-2",
          [c["clause_id"] for c in body["review"]["missing"]] == ["c-3"]
          and {c["clause_id"] for c in body["review"]["expired"]}
          == {"c-3-1", "c-3-2"},
          json.dumps(body["review"], ensure_ascii=False)[:300])
    # 重确认全部后签发成功并产生 1 个版本
    r = client.post(f"/api/drafts/{did}/review/confirm", json={
        "revision": rev,
        "clause_ids": ["c-1", "c-3", "c-3-1", "c-3-2"]})
    check("HTTP 重确认成功", r.status_code == 200
          and r.json()["counts"]["valid"] == 4)
    r = client.post(f"/api/drafts/{did}/issue", json={"revision": rev})
    check("HTTP 重确认后签发 201", r.status_code == 201, str(r.status_code))
    check("签发产生恰好 1 个版本",
          len(client.get(f"/api/drafts/{did}/versions").json()) == 1)

    # 场景 B 全链路
    r = client.post("/api/drafts", json={"title": "B", "content": text_def})
    did = r.json()["id"]
    client.post(f"/api/drafts/{did}/review", json={"enabled": True})
    rev = 1
    client.post(f"/api/drafts/{did}/review/confirm", json={
        "revision": rev, "clause_ids": ["c-1", "c-2", "c-3"]})
    # 第一次保存：c-1 插说明
    r = client.put(f"/api/drafts/{did}",
                   json={"title": "B", "content": text_shift})
    rev = r.json()["revision"]
    st = client.get(f"/api/drafts/{did}/review").json()
    mm = state_map(st)
    check("HTTP c-2 在仅偏移漂移后仍 valid",
          mm["c-2"]["state"] == "valid", mm["c-2"]["state"])
    # 重确认 c-1 后，c-2/c-3 跨修订有效
    client.post(f"/api/drafts/{did}/review/confirm",
                json={"revision": rev, "clause_ids": ["c-1"]})
    r = client.post(f"/api/drafts/{did}/issue", json={"revision": rev})
    check("HTTP 偏移漂移场景签发 201", r.status_code == 201, str(r.status_code))
    # 删除定义 → 422
    r = client.put(f"/api/drafts/{did}",
                   json={"title": "B", "content": text_del})
    rev = r.json()["revision"]
    r = client.post(f"/api/drafts/{did}/issue", json={"revision": rev})
    check("HTTP 删除定义后签发 422（review.expired 含 c-2）",
          r.status_code == 422
          and any(c["clause_id"] == "c-2"
                  for c in r.json()["detail"]["review"]["expired"]),
          str(r.status_code))

    # 未开启复核的签发流程保持不变
    r = client.post("/api/drafts", json={"title": "C", "content": text_no3})
    did = r.json()["id"]
    r = client.post(f"/api/drafts/{did}/issue", json={"revision": 1})
    check("未开启复核照常签发 201", r.status_code == 201, str(r.status_code))

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("✅ 全部核验通过")
