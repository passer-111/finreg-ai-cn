"""入库动作与核验台账的离线测试 —— ``promote`` 模块。

覆盖矩阵（来自需求规格的「原子执行」要求）
------------------------------------------
- 决策状态机：红/黄/需人工核项未清零时入库被拒绝（界面置灰与服务端
  防线共用同一份逻辑，绕过界面直接调用也必须被拦下）；
- 成功路径：文件移动、verified_by/last_verified/verified_note 改写、
  台账追加（判断与入库各一条）；
- 校验失败回滚：草稿逐字节恢复、正式记录删除、台账追加 rollback
  （只增不改——撤销本身也是一条新记录，而不是抹掉旧的）；
- 字段动作：采纳候选日期 / 删除义务条目 / 修改义务类型。

全部离线：草稿写在临时目录，校验器用注入的假实现
（真实校验器读仓库固定目录，无法离线复现「入库后校验失败」）。
"""

# 导入 json：读台账 JSONL 行
import json
# 导入 date：固定的入库日期（测试绝不依赖「今天」）
from datetime import date

# 导入 yaml：把测试草稿写成 YAML
import yaml

# 导入被测对象
from finreg_ai.promote import (
    Decision,               # 一次判断
    blocked_reasons,        # 决策状态机
    log_decision,           # 判断留痕
    promote_draft,          # 入库动作
    reject_draft,           # 退回动作
)
# 导入模型类型与序列化函数
from finreg_ai.models import VerifiedBy, policy_from_dict, policy_to_dict
# 导入取证层类型：构造证据卡
from finreg_ai.verify import (
    STATUS_ERROR,           # 红
    STATUS_OK,              # 绿
    STATUS_WARNING,         # 黄
    CheckOption,            # 判断选项
    CheckResult,            # 检查结果
    EvidenceCard,           # 证据卡
)
# 复用政策工厂
from tests.test_models import make_policy

# 固定的入库日期。写死而非用「今天」，测试才不与运行日期耦合。
FIXED_DAY = date(2026, 10, 8)


def _write_draft(drafts_dir, policy) -> None:
    """把一条政策对象写成草稿 YAML 文件。"""
    # 确保目录存在
    drafts_dir.mkdir(parents=True, exist_ok=True)
    # 写入文件（与真实草稿同构）
    (drafts_dir / f"{policy.id}.yaml").write_text(
        yaml.safe_dump(policy_to_dict(policy), allow_unicode=True), encoding="utf-8"
    )


def _make_card(policy_id: str, checks: list[CheckResult]) -> EvidenceCard:
    """构造一张只含指定检查项的证据卡。"""
    # 组装
    return EvidenceCard(
        policy_id=policy_id,                        # 草稿 id
        title="某测试政策",                          # 标题
        url="https://example.gov.cn/a.html",        # 官方链接
        checks=checks,                              # 检查项
    )


def _date_warning_check() -> CheckResult:
    """构造一个「日期可补」的黄项（含采纳/留空两个选项）。"""
    # 组装黄项
    return CheckResult(
        check_id="date",                            # 检查项标识
        layer="日期与状态",                          # 检查层
        status=STATUS_WARNING,                      # 黄
        summary="记录未填写生效日期，但原文存在施行表述 —— 可补",  # 结论
        evidence=["第十八条 本办法自 2026 年 7 月 1 日起施行"],   # 证据摘录
        options=[                                   # 判断选项
            CheckOption(
                key="adopt:2026-07-01",
                label="采纳候选日期 2026-07-01",
                action={"kind": "set-field", "field": "effective_from", "value": "2026-07-01"},
            ),
            CheckOption(
                key="keep-blank",
                label="维持留空（我已人工核对）",
                action={"kind": "acknowledge", "field": "date"},
            ),
        ],
    )


# ============================================================
# 决策状态机
# ============================================================

def test_blocked_reasons_lists_undecided_pending_checks() -> None:
    """验证未判断的红/黄项被逐条列为阻断原因。"""
    # 构造含一黄一红的卡
    card = _make_card("test-2026-demo", [
        _date_warning_check(),
        CheckResult(check_id="title", layer="标题比对", status=STATUS_ERROR, summary="页面标题与记录不符"),
    ])
    # 无判断
    reasons = blocked_reasons(card, [])
    # 两条阻断原因
    assert len(reasons) == 2
    # 含检查项标识
    assert any("date" in r for r in reasons)
    # 含另一个检查项
    assert any("title" in r for r in reasons)


def test_blocked_reasons_empty_when_all_decided() -> None:
    """反向测试：全部待判断项都有判断时可以入库（夹逼上一条）。"""
    # 构造含一黄的卡
    card = _make_card("test-2026-demo", [_date_warning_check()])
    # 做出判断
    decision = Decision(
        check_id="date", option_key="keep-blank", label="维持留空",
        action={"kind": "acknowledge", "field": "date"},
    )
    # 应无阻断
    assert blocked_reasons(card, [decision]) == []


def test_blocked_reasons_ignores_green_checks() -> None:
    """验证绿项不要求判断（机器通过的项不该增加人的点击负担）。"""
    # 构造全绿卡
    card = _make_card("test-2026-demo", [
        CheckResult(check_id="reachability", layer="可达性", status=STATUS_OK, summary="页面可访问"),
    ])
    # 无任何判断也可入库
    assert blocked_reasons(card, []) == []


# ============================================================
# 成功路径
# ============================================================

def test_promote_moves_file_and_rewrites_fields(tmp_path) -> None:
    """验证成功入库：文件移动、三个台账字段改写、日志追加。"""
    # 目录布局
    drafts = tmp_path / "drafts"
    # 政策目录
    policies = tmp_path / "policies"
    # 台账目录
    logs = tmp_path / "logs"
    # 写草稿（生效日期留空，待采纳）
    policy = make_policy(effective_from=None, verified_by=VerifiedBy.AUTOMATED)
    # 写文件
    _write_draft(drafts, policy)
    # 构造证据卡与判断
    card = _make_card(policy.id, [_date_warning_check()])
    # 判断：采纳候选日期
    decision = Decision(
        check_id="date", option_key="adopt:2026-07-01", label="采纳候选日期 2026-07-01",
        action={"kind": "set-field", "field": "effective_from", "value": "2026-07-01"},
    )
    # 入库（校验器注入空错误列表 = 校验通过）
    result = promote_draft(
        policy.id, [decision], card,
        drafts_dir=drafts, policies_dir=policies, log_dir=logs,
        today=FIXED_DAY, validator=lambda: [],
    )
    # 成功
    assert result.ok
    # 未回滚
    assert not result.rolled_back
    # 草稿已删除
    assert not (drafts / f"{policy.id}.yaml").exists()
    # 正式记录已写入
    new_path = policies / f"{policy.id}.yaml"
    # 存在
    assert new_path.exists()
    # 读回验证字段
    promoted = policy_from_dict(yaml.safe_load(new_path.read_text(encoding="utf-8")))
    # verified_by 已改为 human——这个值只能由本路径写入
    assert promoted.verified_by is VerifiedBy.HUMAN
    # last_verified 为入库当天
    assert promoted.last_verified == FIXED_DAY
    # 采纳的日期已生效
    assert promoted.effective_from == date(2026, 7, 1)
    # 核验说明含判断记录与机器证据摘录
    assert "采纳候选日期" in promoted.verified_note
    # 证据摘录含原文句子（条款号与原文）
    assert "第十八条" in promoted.verified_note
    # 台账文件存在且含入库记录
    log_lines = (logs / f"{FIXED_DAY.isoformat()}.jsonl").read_text(encoding="utf-8").splitlines()
    # 至少一条入库记录
    kinds = [json.loads(line)["kind"] for line in log_lines]
    # 含 promote
    assert "promote" in kinds
    # 入库记录携带判断明细
    promote_entry = next(json.loads(line) for line in log_lines if json.loads(line)["kind"] == "promote")
    # 判断明细
    assert promote_entry["decisions"][0]["option_key"] == "adopt:2026-07-01"


def test_promote_persists_evidence_card(tmp_path) -> None:
    """验证证据卡落盘：cards/<id>.json 存在、内容完整、台账引用路径。

    复查入口是这张卡的全部意义：台账条目里只有判断与摘录条数，
    事后要回答「当时机器看到了什么」只能靠落盘的完整卡片——
    重新抓取没有意义，页面内容会变。
    """
    # 目录布局
    drafts = tmp_path / "drafts"
    # 政策目录
    policies = tmp_path / "policies"
    # 台账目录
    logs = tmp_path / "logs"
    # 写草稿
    policy = make_policy(effective_from=None, verified_by=VerifiedBy.AUTOMATED)
    # 写文件
    _write_draft(drafts, policy)
    # 构造证据卡与判断
    card = _make_card(policy.id, [_date_warning_check()])
    # 判断
    decision = Decision(
        check_id="date", option_key="adopt:2026-07-01", label="采纳候选日期 2026-07-01",
        action={"kind": "set-field", "field": "effective_from", "value": "2026-07-01"},
    )
    # 入库
    result = promote_draft(
        policy.id, [decision], card,
        drafts_dir=drafts, policies_dir=policies, log_dir=logs,
        today=FIXED_DAY, validator=lambda: [],
    )
    # 成功
    assert result.ok
    # 结果对象携带卡片路径
    assert result.card_path == logs / "cards" / f"{policy.id}.json"
    # 卡片文件存在
    assert result.card_path is not None and result.card_path.exists()
    # 读回验证内容完整
    payload = json.loads(result.card_path.read_text(encoding="utf-8"))
    # 政策 id 一致
    assert payload["policy_id"] == policy.id
    # 检查项完整保留（这是「当时机器看到了什么」的全部证据）
    assert len(payload["checks"]) == len(card.checks)
    # 附入库日期
    assert payload["promoted_on"] == FIXED_DAY.isoformat()
    # 台账条目引用卡片相对路径（相对路径：日志只增不改，不能写死绝对路径）
    log_lines = (logs / f"{FIXED_DAY.isoformat()}.jsonl").read_text(encoding="utf-8").splitlines()
    # 取 promote 条目
    promote_entry = next(json.loads(line) for line in log_lines if json.loads(line)["kind"] == "promote")
    # 引用路径
    assert promote_entry["card_path"] == f"cards/{policy.id}.json"


def test_promote_refuses_when_checks_undecided(tmp_path) -> None:
    """验证状态机拒绝：红项未清零时不动任何文件、不写台账。"""
    # 目录布局
    drafts = tmp_path / "drafts"
    # 政策目录
    policies = tmp_path / "policies"
    # 台账目录
    logs = tmp_path / "logs"
    # 写草稿
    policy = make_policy(verified_by=VerifiedBy.AUTOMATED)
    # 写文件
    _write_draft(drafts, policy)
    # 构造含红项的卡，不做任何判断
    card = _make_card(policy.id, [
        CheckResult(check_id="title", layer="标题比对", status=STATUS_ERROR, summary="页面标题与记录不符"),
    ])
    # 入库
    result = promote_draft(
        policy.id, [], card,
        drafts_dir=drafts, policies_dir=policies, log_dir=logs,
        today=FIXED_DAY, validator=lambda: [],
    )
    # 被拒绝
    assert not result.ok
    # 阻断原因非空
    assert result.blocked_reasons
    # 草稿原样保留
    assert (drafts / f"{policy.id}.yaml").exists()
    # 正式记录未写入
    assert not (policies / f"{policy.id}.yaml").exists()
    # 台账未创建（一次点击都没有，就没有记录）
    assert not logs.exists() or not list(logs.glob("*.jsonl"))


# ============================================================
# 校验失败回滚
# ============================================================

def test_promote_rolls_back_when_validation_fails(tmp_path) -> None:
    """验证校验失败整体回滚：草稿恢复、正式记录删除、台账记 rollback。

    台账只增不改：回滚不是抹掉 promote 记录，而是追加一条 rollback——
    事后审计要能看到「曾经入过库、又被校验挡回来了」的完整序列。
    """
    # 目录布局
    drafts = tmp_path / "drafts"
    # 政策目录
    policies = tmp_path / "policies"
    # 台账目录
    logs = tmp_path / "logs"
    # 写草稿
    policy = make_policy(verified_by=VerifiedBy.AUTOMATED)
    # 写文件
    _write_draft(drafts, policy)
    # 记下草稿原文（回滚后必须逐字节一致）
    original_text = (drafts / f"{policy.id}.yaml").read_text(encoding="utf-8")
    # 构造全绿卡（状态机不拦，失败发生在校验兜底）
    card = _make_card(policy.id, [])
    # 入库：校验器注入「引入新错误」
    result = promote_draft(
        policy.id, [], card,
        drafts_dir=drafts, policies_dir=policies, log_dir=logs,
        today=FIXED_DAY,
        validator=lambda: ["[test-2026-demo] superseded_by 指向不存在的记录：ghost"],
    )
    # 失败
    assert not result.ok
    # 已回滚
    assert result.rolled_back
    # 草稿逐字节恢复
    assert (drafts / f"{policy.id}.yaml").read_text(encoding="utf-8") == original_text
    # 正式记录已删除
    assert not (policies / f"{policy.id}.yaml").exists()
    # 台账含 promote 与 rollback 两条（只增不改）
    log_lines = (logs / f"{FIXED_DAY.isoformat()}.jsonl").read_text(encoding="utf-8").splitlines()
    # 类型序列
    kinds = [json.loads(line)["kind"] for line in log_lines]
    # promote 在前 rollback 在后——完整保留「入过又回滚」的序列
    assert kinds == ["promote", "rollback"]
    # rollback 记录含错误明细
    rollback_entry = json.loads(log_lines[1])
    # 含错误
    assert "ghost" in str(rollback_entry["errors"])
    # 证据卡保留备查——回滚撤销的是「入库」这个结果，
    # 不是「人工基于这些证据做过一次尝试」这个事实
    assert (logs / "cards" / f"{policy.id}.json").exists()


def test_promote_refuses_to_overwrite_existing_policy(tmp_path) -> None:
    """验证目标记录已存在时拒绝覆盖（id 冲突防御）。"""
    # 目录布局
    drafts = tmp_path / "drafts"
    # 政策目录
    policies = tmp_path / "policies"
    # 台账目录
    logs = tmp_path / "logs"
    # 写草稿
    policy = make_policy(verified_by=VerifiedBy.AUTOMATED)
    # 写文件
    _write_draft(drafts, policy)
    # 在政策目录预置同名记录
    _write_draft(policies, policy)
    # 构造全绿卡
    card = _make_card(policy.id, [])
    # 入库
    result = promote_draft(
        policy.id, [], card,
        drafts_dir=drafts, policies_dir=policies, log_dir=logs,
        today=FIXED_DAY, validator=lambda: [],
    )
    # 被拒绝
    assert not result.ok
    # 草稿未动
    assert (drafts / f"{policy.id}.yaml").exists()
    # 消息说明拒绝覆盖
    assert any("拒绝覆盖" in m for m in result.messages)


# ============================================================
# 字段动作与判断留痕
# ============================================================

def test_drop_obligation_action(tmp_path) -> None:
    """验证「删除义务条目」动作生效且写进核验说明。"""
    # 导入义务类型
    from finreg_ai.models import KeyObligation

    # 目录布局
    drafts = tmp_path / "drafts"
    # 政策目录
    policies = tmp_path / "policies"
    # 台账目录
    logs = tmp_path / "logs"
    # 写草稿：含两条义务
    policy = make_policy(verified_by=VerifiedBy.AUTOMATED)
    # 两条义务
    policy.key_obligations = [
        KeyObligation(clause="三", summary="应当建立管理制度"),
        KeyObligation(clause="九十九", summary="定位不到的条目"),
    ]
    # 写文件
    _write_draft(drafts, policy)
    # 构造卡：obligation:1 定位失败（红）
    card = _make_card(policy.id, [
        CheckResult(
            check_id="obligation:1", layer="义务核对", status=STATUS_ERROR,
            summary="第 九十九 条：未能在原文中定位到该条款",
            evidence=["义务概括：定位不到的条目"],
        ),
    ])
    # 判断：删除该条目
    decision = Decision(
        check_id="obligation:1", option_key="drop-obligation", label="删除该义务条目",
        action={"kind": "drop-obligation", "index": 1},
    )
    # 入库
    result = promote_draft(
        policy.id, [decision], card,
        drafts_dir=drafts, policies_dir=policies, log_dir=logs,
        today=FIXED_DAY, validator=lambda: [],
    )
    # 成功
    assert result.ok
    # 读回
    promoted = policy_from_dict(yaml.safe_load((policies / f"{policy.id}.yaml").read_text(encoding="utf-8")))
    # 只剩一条义务
    assert len(promoted.key_obligations) == 1
    # 留下的是第三条
    assert promoted.key_obligations[0].clause == "三"
    # 核验说明含删除记录
    assert "删除" in promoted.verified_note


def test_log_decision_appends_jsonl(tmp_path) -> None:
    """验证判断点击即留痕（不等入库）。"""
    # 台账目录
    logs = tmp_path / "logs"
    # 判断
    decision = Decision(
        check_id="date", option_key="keep-blank", label="维持留空",
        action={"kind": "acknowledge", "field": "date"},
    )
    # 留痕
    path = log_decision(logs, "test-2026-demo", decision, "证据：原文无施行条款", FIXED_DAY)
    # 文件存在
    assert path.exists()
    # 读一行
    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    # 类型
    assert entry["kind"] == "decision"
    # 检查项
    assert entry["check_id"] == "date"
    # 判断表述
    assert entry["label"] == "维持留空"
    # 证据摘要
    assert "无施行条款" in entry["evidence"]
    # 时间戳带时区
    assert "+08:00" in entry["ts"]


# ============================================================
# 退回草稿（出队但留档）
# ============================================================

def test_reject_moves_draft_and_logs(tmp_path) -> None:
    """验证退回：草稿移入 _rejected/、理由入台账、原位置不再存在。"""
    # 目录布局
    drafts = tmp_path / "drafts"
    # 台账目录
    logs = tmp_path / "logs"
    # 写草稿
    policy = make_policy(verified_by=VerifiedBy.AUTOMATED)
    # 写文件
    _write_draft(drafts, policy)
    # 记下原文（移动必须逐字节保留）
    original_text = (drafts / f"{policy.id}.yaml").read_text(encoding="utf-8")
    # 退回
    result = reject_draft(
        policy.id, "原文已废止，官网跳转到期满失效公告",
        drafts_dir=drafts, log_dir=logs, today=FIXED_DAY,
    )
    # 成功
    assert result.ok
    # 原位置不再存在
    assert not (drafts / f"{policy.id}.yaml").exists()
    # 已移入 _rejected/ 且逐字节一致
    target = drafts / "_rejected" / f"{policy.id}.yaml"
    # 存在
    assert target.exists()
    # 逐字节
    assert target.read_text(encoding="utf-8") == original_text
    # 台账记 kind=reject 且带理由（理由是审计核心字段）
    log_lines = (logs / f"{FIXED_DAY.isoformat()}.jsonl").read_text(encoding="utf-8").splitlines()
    # 取记录
    entry = json.loads(log_lines[0])
    # 类型
    assert entry["kind"] == "reject"
    # 理由
    assert "废止" in entry["reason"]


def test_reject_requires_reason(tmp_path) -> None:
    """验证理由必填：空理由拒绝退回，草稿与台账都不动。"""
    # 目录布局
    drafts = tmp_path / "drafts"
    # 台账目录
    logs = tmp_path / "logs"
    # 写草稿
    policy = make_policy(verified_by=VerifiedBy.AUTOMATED)
    # 写文件
    _write_draft(drafts, policy)
    # 空白理由（纯空格也算空——strip 后为空）
    result = reject_draft(policy.id, "   ", drafts_dir=drafts, log_dir=logs, today=FIXED_DAY)
    # 被拒绝
    assert not result.ok
    # 草稿原样保留
    assert (drafts / f"{policy.id}.yaml").exists()
    # 台账未创建
    assert not logs.exists() or not list(logs.glob("*.jsonl"))


def test_reject_refuses_overwrite(tmp_path) -> None:
    """验证同名已退回过时拒绝覆盖（覆盖会抹掉上一次退回的原文）。"""
    # 目录布局
    drafts = tmp_path / "drafts"
    # 台账目录
    logs = tmp_path / "logs"
    # 写草稿
    policy = make_policy(verified_by=VerifiedBy.AUTOMATED)
    # 写文件
    _write_draft(drafts, policy)
    # 预先制造同名已退回文件
    rejected_dir = drafts / "_rejected"
    # 建目录
    rejected_dir.mkdir(parents=True)
    # 写同名文件
    (rejected_dir / f"{policy.id}.yaml").write_text("# 上一次退回的原文\n", encoding="utf-8")
    # 退回
    result = reject_draft(policy.id, "重复退回", drafts_dir=drafts, log_dir=logs, today=FIXED_DAY)
    # 被拒绝
    assert not result.ok
    # 原草稿未被移动
    assert (drafts / f"{policy.id}.yaml").exists()
    # 旧退回文件未被覆盖
    assert (rejected_dir / f"{policy.id}.yaml").read_text(encoding="utf-8") == "# 上一次退回的原文\n"


def test_reject_missing_draft(tmp_path) -> None:
    """验证草稿不存在时明确报错（不静默成功）。"""
    # 退回不存在的草稿
    result = reject_draft("ghost-2026-x", "不存在的草稿",
                          drafts_dir=tmp_path / "drafts", log_dir=tmp_path / "logs",
                          today=FIXED_DAY)
    # 被拒绝
    assert not result.ok
    # 报错信息含「不存在」
    assert any("不存在" in m for m in result.messages)
