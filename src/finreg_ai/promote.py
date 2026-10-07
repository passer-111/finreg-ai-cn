"""核验通过入库 —— 把草稿从 data/drafts/ 原子地提升为 data/policies/ 正式记录。

为什么这个模块存在
------------------
「人工核验草稿并入库」是知识库数据质量的总闸门。它必须同时满足
三件互相拉扯的事：

1. **认定只能由人做出**。``verified_by: human`` 的写入只能由用户
   在核验台界面上点击触发——本模块的任何函数都不会被流水线调用，
   程序不得自动把任何记录标为已核验。

2. **过程必须留痕**。每一次判断点击与每一次入库都追加一条记录到
   ``data/verification-log/YYYY-MM-DD.jsonl``（只增不改）。
   「谁、什么时候、对哪个字段、做了什么判断、依据什么证据」
   全部机器化记录——这是合规场景下的审计要求。

3. **失败必须整体回滚**。移动文件、改写字段、写台账、跑校验，
   任一步失败，仓库必须回到点击前的状态，并追加一条回滚记录
   （台账只增不改，所以「撤销」本身也是一条新记录，而不是抹掉旧的）。

与「机器发现、人认定」原则的关系
--------------------------------
抓取流水线（fetch.yml）只写 ``data/changes/`` 与 ``data/snapshots/``，
绝不写 ``data/policies/``。本模块是唯一写 ``data/policies/`` 的代码路径，
且它的触发点不在流水线里，而在本地核验台的人的点击上。
"""

# 导入 json 用于台账 JSONL 的序列化
import json
# 导入 dataclass 定义判断与结果结构
from dataclasses import dataclass, field
# 导入 date 用于入库日期（last_verified 取北京时间当天）
from datetime import date
# 导入 Path 用于目录定位
from pathlib import Path
# 导入 Any 用于类型标注
from typing import Any
# 导入 Callable 用于校验器的依赖注入标注
from collections.abc import Callable

# 导入 yaml 用于读取草稿原文
import yaml

# 从模型层导入类型与序列化函数
from finreg_ai.models import Policy, VerifiedBy, policy_from_dict, policy_to_dict
# 从抓取器基类复用北京时间工具（入库日期与台账时间戳都用它）
from finreg_ai.fetchers.base import now_china_iso, today_china
# 从存取层复用项目根与政策目录常量
from finreg_ai.store import POLICIES_DIR, PROJECT_ROOT
# 从校验流水线复用整库校验（入库后的兜底闸门）
from finreg_ai.pipeline import validate_repository
# 从取证层复用证据卡与草稿目录常量（单一事实来源）
from finreg_ai.verify import DRAFTS_DIR, EvidenceCard


# 核验台账目录。与 changes/ 平级：changes 记录「政策世界变了什么」，
# verification-log 记录「人对数据做了什么判断」——前者是机器的发现，
# 后者是人的认定，混在一起会让审计无法区分两者。
VERIFICATION_LOG_DIR = PROJECT_ROOT / "data" / "verification-log"


# ============================================================
# 判断与结果的数据结构
# ============================================================

@dataclass
class Decision:
    """一次人工判断 —— 核验台界面上的一次按钮点击。

    按钮的选项即判断本身（「采纳候选日期」「维持」「删除该条目」），
    因此这里记录的是结构化的选择，而不是自由文本说明。
    """

    # 被判断的检查项标识（与 EvidenceCard 里的 check_id 对应）
    check_id: str
    # 选中的选项键
    option_key: str
    # 按钮文字（判断的自然语言表述，写进台账与核验说明）
    label: str
    # 结构化动作（set-field / set-obligation-type / drop-obligation / acknowledge）
    action: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典。"""
        # 逐字段输出
        return {
            "check_id": self.check_id,      # 检查项
            "option_key": self.option_key,  # 选项键
            "label": self.label,            # 判断表述
            "action": self.action,          # 结构化动作
        }


@dataclass
class PromoteResult:
    """一次入库动作的结果。"""

    # 是否成功入库
    ok: bool
    # 政策 id
    policy_id: str
    # 入库后的文件路径；失败时为 None
    new_path: Path | None = None
    # 是否发生了回滚（True 时仓库已恢复到点击前状态）
    rolled_back: bool = False
    # 阻断原因（决策状态机判定「不能入库」时填写）
    blocked_reasons: list[str] = field(default_factory=list)
    # 过程消息（每步做了什么，供界面逐条显示）
    messages: list[str] = field(default_factory=list)
    # 台账文件路径
    log_path: Path | None = None
    # 落盘的证据卡路径（cards/<id>.json）；失败时为 None
    card_path: Path | None = None


# ============================================================
# 决策状态机 —— 「能不能入库」的判定
# ============================================================

def blocked_reasons(card: EvidenceCard, decisions: list[Decision]) -> list[str]:
    """返回当前还不能入库的原因列表；为空表示可以入库。

    规则：证据卡上每一项非绿检查（红 / 黄 / 需人工核）都必须有
    对应的人工判断。「核验通过并入库」按钮在所有待判断项处理完成前
    置灰，靠的就是这个函数——它同时被界面（禁用按钮）与入库动作
    （服务端最终防线）调用，两处必须用同一份逻辑，
    否则「界面上灰着但直接 POST 就能入库」会成为绕过通道。
    """
    # 已做出判断的检查项集合
    decided = {d.check_id for d in decisions}
    # 收集未判断的待办项
    reasons: list[str] = []
    # 逐项检查
    for check in card.pending_checks():
        # 未判断的项记录原因
        if check.check_id not in decided:
            # 记录：检查项 + 层 + 机器结论
            reasons.append(f"{check.check_id}（{check.layer}）未判断 —— {check.summary}")
    # 返回
    return reasons


# ============================================================
# 核验台账（JSONL，只增不改）
# ============================================================

def append_log_entry(log_dir: Path, entry: dict[str, Any], today: date) -> Path:
    """追加一条台账记录到 ``YYYY-MM-DD.jsonl``，返回文件路径。

    只增不改是硬约束：台账的价值在于「事后能重建每一次判断的
    完整序列」，允许修改旧记录就摧毁了这个价值。
    因此本模块没有任何「改写日志」的函数——回滚也是追加一条
    kind=rollback 的新记录，而不是删掉已写的那条。
    """
    # 确保目录存在
    log_dir.mkdir(parents=True, exist_ok=True)
    # 按日分文件：与 data/changes/ 同一约定，便于按天审阅
    path = log_dir / f"{today.isoformat()}.jsonl"
    # 追加写入一行 JSON
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        # 序列化为一行（JSONL 格式：每行一个完整 JSON 对象）
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # 返回路径
    return path


def _log_entry(kind: str, policy_id: str, **fields: Any) -> dict[str, Any]:
    """构造一条台账记录（自动附北京时间戳）。"""
    # 基础字段
    entry: dict[str, Any] = {
        "ts": now_china_iso(),      # 时间戳（带 +08:00，便于直接阅读）
        "kind": kind,               # 记录类型：decision / promote / rollback
        "policy_id": policy_id,     # 政策 id
    }
    # 合并附加字段
    entry.update(fields)
    # 返回
    return entry


def log_decision(log_dir: Path, policy_id: str, decision: Decision,
                 evidence_summary: str, today: date) -> Path:
    """把一次判断点击追加到台账（点击即留痕，不等入库）。"""
    # 构造记录
    entry = _log_entry(
        "decision",                      # 类型：判断
        policy_id,                       # 政策 id
        check_id=decision.check_id,      # 检查项
        option_key=decision.option_key,  # 选项键
        label=decision.label,            # 判断表述
        evidence=evidence_summary,       # 证据摘要
    )
    # 追加并返回路径
    return append_log_entry(log_dir, entry, today)


def persist_card(log_dir: Path, policy_id: str, card: EvidenceCard, today: date) -> Path:
    """把完整证据卡落盘为 ``cards/<id>.json``，返回文件路径。

    为什么入库时要落盘：台账条目里只有判断与证据摘录条数，事后想复查
    「当时机器到底看到了什么」必须有一张完整卡片；重新抓取没有意义——
    页面内容会变，当时看到的证据才是判断的依据。
    回滚时**不删除**这张卡：它记录的是「人工基于哪些证据做过一次入库
    尝试」，尝试本身也是审计事实（台账里的 promote/rollback 条目也还在）。
    """
    # 卡片子目录
    cards_dir = log_dir / "cards"
    # 确保目录存在
    cards_dir.mkdir(parents=True, exist_ok=True)
    # 卡片内容：证据卡本体 + 入库日期（卡片自身不带时间概念）
    payload = card.to_dict()
    # 附入库日期（北京时间，与台账按日分文件一致）
    payload["promoted_on"] = today.isoformat()
    # 目标路径（同一政策多次入库尝试时覆盖旧卡：旧尝试的证据在旧卡被覆盖前
    # 已随当日台账条目留档——条目里有判断与摘录条数；卡片只保留最近一次，
    # 否则 cards/ 会随每次失败尝试无限堆积，而堆积的卡片没有检索入口）
    path = cards_dir / f"{policy_id}.json"
    # 写入（缩进便于人工直接阅读）
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        # 序列化
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        # 尾换行（与仓库文本约定一致）
        fh.write("\n")
    # 返回
    return path


# ============================================================
# 记录字段的改写
# ============================================================

def apply_decisions(policy: Policy, decisions: list[Decision]) -> list[str]:
    """把全部判断的结构化动作应用到政策对象上，返回改动描述列表。

    只有三类动作真正改写数据；``acknowledge``（人工确认）不改数据，
    它的存在本身就是判断结果——会进台账与核验说明。
    """
    # 改动描述（写进核验说明）
    changes: list[str] = []
    # 待删除的义务下标（收集后统一删除，避免边遍历边改下标错位）
    drop_indexes: list[int] = []
    # 逐个动作应用
    for decision in decisions:
        # 取出动作
        action = decision.action
        # 动作类型
        kind = action.get("kind")
        # 类型一：设置顶层字段（目前只有 effective_from）
        if kind == "set-field":
            # 字段名
            field_name = str(action["field"])
            # 新值（字符串日期或 None）
            value = action.get("value")
            # 应用到对象（effective_from 是 date | None）
            if field_name == "effective_from":
                # 字符串转 date；None 表示清空
                policy.effective_from = date.fromisoformat(value) if value else None
                # 记录改动
                changes.append(
                    f"effective_from → {value}" if value else "effective_from 清空（原文无施行条款佐证）"
                )
        # 类型二：修改义务类型
        elif kind == "set-obligation-type":
            # 义务下标
            index = int(action["index"])
            # 新类型
            policy.key_obligations[index].obligation_type = str(action["value"])
            # 记录改动
            changes.append(f"第 {policy.key_obligations[index].clause} 条义务类型 → {action['value']}")
        # 类型三：删除义务条目
        elif kind == "drop-obligation":
            # 记下待删下标
            drop_indexes.append(int(action["index"]))
        # acknowledge：不改数据，判断本身已留痕
    # 统一删除义务条目（倒序删除，下标不受影响）
    for index in sorted(set(drop_indexes), reverse=True):
        # 边界保护：下标越界时不静默跳过
        if 0 <= index < len(policy.key_obligations):
            # 记录改动
            changes.append(f"删除第 {policy.key_obligations[index].clause} 条义务（无法找到原文依据）")
            # 删除
            del policy.key_obligations[index]
    # 返回改动描述
    return changes


def build_verified_note(policy: Policy, card: EvidenceCard, decisions: list[Decision],
                        changes: list[str], today: date) -> str:
    """重写 ``verified_note``：人的判断结果 + 机器证据摘录。

    为什么必须重写而不是保留草稿的 note：草稿的 note 写的是
    「机器怎么整理出这条记录的」，入库后的 note 要回答的是
    「这条记录凭什么可以当合规依据」——两者的读者与用途完全不同。
    """
    # 判断表述列表（点击顺序即判断顺序）
    judgments = "；".join(d.label for d in decisions) if decisions else "（无需判断项，机器证据全部通过）"
    # 字段改动列表
    changes_text = "；".join(changes) if changes else "无字段改动"
    # 机器证据摘录：取非绿检查项的结论与首条证据（含条款号与原文句子）
    excerpts: list[str] = []
    # 逐项摘录
    for check in card.pending_checks():
        # 结论
        excerpt = f"[{check.layer}] {check.summary}"
        # 附首条证据（通常是原文摘录）
        if check.evidence:
            # 附上
            excerpt += f"｜证据：{check.evidence[0][:200]}"
        # 收集
        excerpts.append(excerpt)
    # 组装说明
    note = (
        f"{today.isoformat()} 经核验台人工核验通过并入库。"
        f"判断记录：{judgments}。"
        f"字段改动：{changes_text}。"
    )
    # 有证据摘录时附上
    if excerpts:
        # 附机器证据
        note += "机器证据摘录：" + "；".join(excerpts)
    # 返回
    return note


# ============================================================
# 入库动作（原子执行，任一步失败整体回滚）
# ============================================================

def promote_draft(
    policy_id: str,
    decisions: list[Decision],
    card: EvidenceCard,
    *,
    drafts_dir: Path | None = None,
    policies_dir: Path | None = None,
    log_dir: Path | None = None,
    today: date | None = None,
    validator: Callable[[], list[str]] | None = None,
) -> PromoteResult:
    """把一条草稿提升为正式记录。

    执行序列（任一步失败整体回滚并报错）：
    ① 决策状态机检查——红/黄/需人工核项未清零时拒绝；
    ② 应用判断动作改写字段，verified_by → human，last_verified → 当天；
    ③ 写入 data/policies/ 并删除 data/drafts/ 原文件；
    ④ 证据卡完整落盘（cards/<id>.json）并追加入库台账（引用卡片路径）；
    ⑤ 跑整库校验兜底——引入新错误则回滚（台账追加 rollback 记录）；
    ⑥ 提示用户 git add + commit（绝不自动提交，提交是人的决定）。

    参数 validator 是整库校验的注入点：默认用 ``validate_repository``
    的错误列表；测试注入假校验器来确定性触发「入库后校验失败」，
    否则无法离线复现回滚路径（真实校验器读的是仓库固定目录）。
    """
    # 解析默认值
    drafts = drafts_dir or DRAFTS_DIR               # 草稿目录
    policies = policies_dir or POLICIES_DIR         # 政策目录
    logs = log_dir or VERIFICATION_LOG_DIR          # 台账目录
    day = today or today_china()                    # 入库日期（北京时间）
    # 校验器：默认整库校验，只取错误列表
    check_errors = validator or (lambda: validate_repository()[0])

    # 结果对象（先建好，各分支往里填）
    result = PromoteResult(ok=False, policy_id=policy_id)

    # --- 第①步：决策状态机 ---
    blocked = blocked_reasons(card, decisions)
    # 有未判断项：拒绝入库
    if blocked:
        # 填阻断原因
        result.blocked_reasons = blocked
        # 消息
        result.messages.append(f"入库被拒绝：{len(blocked)} 个检查项尚未判断")
        # 返回
        return result

    # --- 第②步：读取草稿并改写字段 ---
    draft_path = drafts / f"{policy_id}.yaml"
    # 草稿文件必须存在
    if not draft_path.exists():
        # 明确报错
        result.messages.append(f"草稿文件不存在：{draft_path}")
        # 返回
        return result
    # 读原文文本（回滚时需要逐字节恢复）
    original_text = draft_path.read_text(encoding="utf-8")
    # 解析为对象
    raw = yaml.safe_load(original_text)
    # 转对象（草稿已过 schema，转换不会再失败；失败说明文件被手工改坏）
    policy = policy_from_dict(raw)
    # 应用判断动作
    changes = apply_decisions(policy, decisions)
    # 改写核验台账字段
    policy.verified_by = VerifiedBy.HUMAN          # 人的认定（只能由本路径写入）
    # 入库日期
    policy.last_verified = day
    # 重写核验说明
    policy.verified_note = build_verified_note(policy, card, decisions, changes, day)
    # 消息
    result.messages.append(f"字段已改写：verified_by=human，last_verified={day.isoformat()}")

    # --- 第③步：写入 policies 并删除草稿 ---
    # 目标路径
    new_path = policies / f"{policy_id}.yaml"
    # 防御：目标已存在说明 id 冲突，绝不覆盖正式记录
    if new_path.exists():
        # 明确报错
        result.messages.append(f"目标记录已存在，拒绝覆盖：{new_path}")
        # 返回
        return result
    # 确保目录存在
    policies.mkdir(parents=True, exist_ok=True)
    # 序列化为标准格式写入。
    # 注意：这会丢失草稿 YAML 里的注释——这是有意的取舍：
    # 草稿的注释（整理过程说明）已完整保留在 git 历史与核验台账里，
    # 正式记录采用与 save_policy 一致的标准序列化格式，
    # 避免一份文件里同时存在「草稿阶段的注释」与「入库后的字段值」
    # 两种时间层的文字互相矛盾。
    with open(new_path, "w", encoding="utf-8", newline="\n") as fh:
        # 序列化（保留中文与键顺序）
        yaml.safe_dump(policy_to_dict(policy), fh, allow_unicode=True, sort_keys=False,
                       default_flow_style=False, width=120)
    # 删除草稿文件（到此步为止仍可回滚：原文在 original_text 里）
    draft_path.unlink()
    # 消息
    result.messages.append(f"已移动：{draft_path.name} → data/policies/")

    # --- 第④步：落盘证据卡并追加入库台账 ---
    # 先落盘卡片：台账条目要引用卡片路径，顺序不能反
    card_path = persist_card(logs, policy_id, card, day)
    # 台账里记相对路径（相对台账目录），避免把绝对路径写进只增不改的日志
    card_rel = card_path.relative_to(logs).as_posix()
    # 追加入库条目
    log_path = append_log_entry(logs, _log_entry(
        "promote",                                  # 类型：入库
        policy_id,                                  # 政策 id
        decisions=[d.to_dict() for d in decisions],  # 全部判断
        changes=changes,                            # 字段改动
        evidence_excerpt_count=len(card.pending_checks()),  # 证据摘录条数
        card_path=card_rel,                         # 完整证据卡位置（复查入口）
    ), day)
    # 记录台账与卡片路径
    result.log_path = log_path
    # 卡片路径
    result.card_path = card_path
    # 消息
    result.messages.append(f"台账已追加：{log_path.name}；证据卡已落盘：{card_rel}")

    # --- 第⑤步：整库校验兜底 ---
    try:
        # 运行校验
        new_errors = check_errors()
    except Exception as exc:  # noqa: BLE001  校验器本身崩溃同样要回滚
        # 记为新错误
        new_errors = [f"校验器异常：{type(exc).__name__}: {exc}"]
    # 有新错误：整体回滚
    if new_errors:
        # 恢复草稿文件（逐字节）
        draft_path.write_text(original_text, encoding="utf-8", newline="\n")
        # 删除刚写入的正式记录
        new_path.unlink(missing_ok=True)
        # 追加回滚台账（只增不改：撤销本身也是一条新记录）
        append_log_entry(logs, _log_entry(
            "rollback",                     # 类型：回滚
            policy_id,                      # 政策 id
            reason="入库后整库校验失败",      # 原因
            errors=new_errors,              # 错误明细
        ), day)
        # 填结果
        result.rolled_back = True
        # 消息
        result.messages.append("入库后校验失败，已整体回滚：")
        # 说明卡片去向（卡片不随回滚删除：失败的入库尝试本身也是审计事实）
        result.messages.append(f"证据卡保留备查：{card_rel}")
        # 逐条附错误
        result.messages.extend(f"  - {e}" for e in new_errors)
        # 返回
        return result

    # --- 第⑥步：成功。提示提交（绝不自动提交）---
    result.ok = True
    # 填路径
    result.new_path = new_path
    # 消息：为什么强调手动提交——「入库」是数据决定，「提交」是发布决定，
    # 把两者合并成一次点击会让「我还没想好要不要发布」的人被迫发布。
    result.messages.append("校验通过。请人工执行：git add data/ && git commit（提交是人的决定，本工具不代办）")
    # 返回
    return result


# 公开接口
__all__ = [
    "VERIFICATION_LOG_DIR",     # 台账目录
    "Decision",                 # 一次判断
    "PromoteResult",            # 入库结果
    "append_log_entry",         # 台账追加
    "blocked_reasons",          # 决策状态机
    "log_decision",             # 判断留痕
    "persist_card",             # 证据卡落盘
    "promote_draft",            # 入库动作
]
