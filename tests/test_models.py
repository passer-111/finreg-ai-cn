"""数据模型层测试 —— 枚举语义、问题分级、业务规则校验、字典转换。

为什么重点测「问题分级」与「业务规则」
------------------------------------
这两处是纯逻辑，出错不会崩溃，只会让错误数据悄悄通过校验。
尤其是 ``with_location``：它看起来只是拼个字符串，但拼错顺序会让
``is_warning()`` 失效，从而把「已知的待办」变成「阻断性错误」——
维护者最终会去删掉那条提示，技术债从此不可见。
"""

# 导入日期类型
from datetime import date

# 导入 pytest 以使用参数化
import pytest

# 导入待测模型
from finreg_ai.models import (
    AIRelevance,
    Bindingness,
    EnforcementRecord,
    InstrumentType,
    KeyObligation,
    Policy,
    PolicyStatus,
    SourceRef,
    SourceTier,
    VerifiedBy,
    Version,
    admits_effective_date_unconfirmed,
    enforcement_from_dict,
    enforcement_to_dict,
    is_warning,
    policy_from_dict,
    policy_to_dict,
    strip_warning_prefix,
    with_location,
)
# 导入存储层的校验工具与 schema 加载器
from finreg_ai.store import (
    load_enforcement_schema,
    normalize_for_validation,
    validate_against_schema,
)


# ============================================================
# 枚举语义
# ============================================================

@pytest.mark.parametrize("status", [PolicyStatus.EFFECTIVE, PolicyStatus.PARTIALLY_EFFECTIVE, PolicyStatus.AMENDED])
def test_is_currently_valid_true_for_valid_statuses(status: PolicyStatus) -> None:
    """验证「有效」的三种状态被正确识别。

    AMENDED（已修订）与 PARTIALLY_EFFECTIVE（部分有效）常被误判为失效，
    但它们在法律上仍然产生义务，只是需要注意版本。
    """
    # 断言判断为有效
    assert status.is_currently_valid is True


@pytest.mark.parametrize(
    "status",
    [
        PolicyStatus.DRAFT,       # 起草中
        PolicyStatus.CONSULTATION,  # 征求意见
        PolicyStatus.PUBLISHED,   # 已公布未生效
        PolicyStatus.REPEALED,    # 已废止
        PolicyStatus.SUPERSEDED,  # 已被取代
        PolicyStatus.EXPIRED,     # 已过期
        PolicyStatus.UNKNOWN,     # 未知
    ],
)
def test_is_currently_valid_false_for_other_statuses(status: PolicyStatus) -> None:
    """验证其余状态不被判为有效。"""
    # 断言判断为无效
    assert status.is_currently_valid is False


@pytest.mark.parametrize("status", [PolicyStatus.REPEALED, PolicyStatus.SUPERSEDED, PolicyStatus.EXPIRED])
def test_is_terminal_true_for_terminal_statuses(status: PolicyStatus) -> None:
    """验证终态被正确识别——这类文件不需要继续追踪后续变化。"""
    # 断言为终态
    assert status.is_terminal is True


def test_is_terminal_false_for_effective() -> None:
    """验证现行有效的文件不是终态。"""
    # 现行有效仍需持续跟踪可能的修订
    assert PolicyStatus.EFFECTIVE.is_terminal is False


# ============================================================
# 问题分级：警告前缀
# ============================================================

def test_is_warning_detects_prefix() -> None:
    """验证警告前缀能被识别。"""
    # 带前缀的为警告
    assert is_warning("[警告] 某问题") is True
    # 不带前缀的为错误
    assert is_warning("某问题") is False


def test_strip_warning_prefix_removes_prefix() -> None:
    """验证前缀被正确剥离。"""
    # 带前缀时剥离后不留多余空格
    assert strip_warning_prefix("[警告] 某问题") == "某问题"
    # 无前缀时原样返回
    assert strip_warning_prefix("某问题") == "某问题"


def test_with_location_keeps_warning_prefix_at_front() -> None:
    """验证拼接位置信息后，警告前缀仍在最前。

    这条测试对应一个真实缺陷：早期实现用 ``f"{path.name}: {msg}"`` 拼接，
    把 ``[警告]`` 挤到了中间，导致 ``is_warning()`` 失效，
    一条警告被当成错误处理——整个分级机制静默失效。
    """
    # 拼接位置
    result = with_location("[警告] 缺少生效日期", "some-file.yaml")
    # 前缀必须仍在最前，否则 is_warning 会失效
    assert is_warning(result) is True
    # 位置信息应出现
    assert "some-file.yaml" in result
    # 剥离前缀后应得到「位置: 描述」
    assert strip_warning_prefix(result) == "some-file.yaml: 缺少生效日期"


def test_with_location_prefixes_error_without_warning_marker() -> None:
    """验证错误级问题拼接位置后仍不含警告前缀。"""
    # 拼接
    result = with_location("生效日期早于公布日期", "some-file.yaml")
    # 不应被误判为警告
    assert is_warning(result) is False
    # 位置信息应出现
    assert result == "some-file.yaml: 生效日期早于公布日期"


# ============================================================
# schema 校验前的日期归一化
# ============================================================

def test_normalize_for_validation_converts_date_to_iso_string() -> None:
    """验证 date 被转成 ISO 字符串。

    这条对应一个真实缺陷：PyYAML 会把未加引号的 ``2026-06-18`` 解析成
    ``datetime.date``，与 JSON Schema 的 ``"type": "string"`` 冲突，
    导致 48 条校验错误。与其要求贡献者给日期加引号（反直觉、必然遗忘），
    不如在校验前统一归一化。
    """
    # 单个日期
    assert normalize_for_validation(date(2026, 6, 18)) == "2026-06-18"


def test_normalize_for_validation_recurses_into_containers() -> None:
    """验证嵌套结构中的日期也被归一化。"""
    # 嵌套字典与列表
    payload = {"versions": [{"effective_from": date(2026, 6, 18)}], "tags": ["a"]}
    # 执行归一化
    normalized = normalize_for_validation(payload)
    # 深层日期应变成字符串
    assert normalized["versions"][0]["effective_from"] == "2026-06-18"
    # 非日期值保持不变
    assert normalized["tags"] == ["a"]


def test_normalize_for_validation_leaves_other_types_untouched() -> None:
    """验证非日期类型原样返回。"""
    # 字符串、整数、None、布尔
    assert normalize_for_validation("x") == "x"
    # 整数
    assert normalize_for_validation(7) == 7
    # None
    assert normalize_for_validation(None) is None
    # 布尔（注意布尔是 int 的子类，不应被误转）
    assert normalize_for_validation(True) is True


# ============================================================
# 业务规则校验
# ============================================================

def make_policy(**overrides: object) -> Policy:
    """构造一条最小可用的 ``Policy``，可用关键字覆盖任意字段。"""
    # 默认值刻意取「一切正常」的形态，便于每个测试只改动要验证的那一项
    defaults: dict = {
        "id": "test-2026-demo",                       # 标识
        "title": "某测试政策",                          # 标题
        "issuer": "某发布机构",                         # 机构
        "jurisdiction": "CN",                          # 法域
        "instrument_type": InstrumentType.DEPARTMENT_NORMATIVE,  # 层级
        "bindingness": Bindingness.SOFT_LAW,           # 约束力
        "ai_relevance": AIRelevance.CORE,              # AI 相关度
        "domain": ["banking"],                         # 领域
        "published_on": date(2026, 6, 18),             # 公布日期
        "source": SourceRef(                           # 溯源信息
            url="https://example.gov.cn/a.html",       # 官方链接
            site="example.gov.cn",                     # 站点
            fetched_at="2026-10-03T12:00:00+08:00",    # 抓取时间
            tier=SourceTier.PRIMARY,                   # 来源等级
        ),
        "last_verified": date(2026, 10, 3),            # 最近核验日期
        "verified_by": VerifiedBy.HUMAN,               # 核验方式
        "versions": [                                  # 版本链
            Version(
                version=1,
                published_on=date(2026, 6, 18),
                status=PolicyStatus.EFFECTIVE,
                url="https://example.gov.cn/a.html",
                effective_from=date(2026, 7, 1),
            )
        ],
        "status": PolicyStatus.EFFECTIVE,              # 状态：现行有效
        "effective_from": date(2026, 7, 1),            # 生效日期
    }
    # 应用覆盖项
    defaults.update(overrides)
    # 构造对象
    return Policy(**defaults)  # type: ignore[arg-type]


def test_validate_semantics_passes_for_clean_record() -> None:
    """验证形态正常的记录不产生任何问题。"""
    # 全字段正常，应无问题
    assert make_policy().validate_semantics() == []


def test_validate_semantics_flags_consultation_with_effective_date() -> None:
    """验证征求意见稿填了生效日期被判为错误。"""
    # 征求意见阶段不可能已有生效日期
    problems = make_policy(status=PolicyStatus.CONSULTATION, effective_from=date(2026, 7, 1)).validate_semantics()
    # 应产生问题
    assert any("尚未定稿" in p for p in problems)


def test_validate_semantics_flags_effective_until_before_effective_from() -> None:
    """验证失效日期早于生效日期（逻辑矛盾）被判为错误。"""
    # 构造矛盾
    problems = make_policy(
        effective_from=date(2026, 7, 1), effective_until=date(2026, 6, 1)
    ).validate_semantics()
    # 应产生问题
    assert any("逻辑矛盾" in p for p in problems)


def test_validate_semantics_flags_expired_without_effective_until() -> None:
    """验证声称已过期却未填失效时点被判为错误。"""
    # 无法核验的「已过期」
    problems = make_policy(status=PolicyStatus.EXPIRED, effective_until=None).validate_semantics()
    # 应产生问题
    assert any("无法核验失效时点" in p for p in problems)


def test_validate_semantics_flags_unknown_without_note() -> None:
    """验证状态为 unknown 但未说明原因被判为错误。"""
    # 这个字段的价值就在于把未知显式化
    problems = make_policy(status=PolicyStatus.UNKNOWN, verified_note=None).validate_semantics()
    # 应产生问题
    assert any("verified_note" in p for p in problems)


def test_validate_semantics_flags_superseded_without_superseded_by() -> None:
    """验证「已被取代」却没写取代者被判为错误。"""
    # 自相矛盾的表述
    problems = make_policy(status=PolicyStatus.SUPERSEDED, superseded_by=None).validate_semantics()
    # 应产生问题
    assert any("superseded_by" in p for p in problems)


def test_validate_semantics_flags_discontinuous_version_numbers() -> None:
    """验证版本号跳号被判为错误——版本链断裂会导致无法追溯历史义务。"""
    # 版本号为 1 和 3，缺 2
    versions = [
        Version(version=1, published_on=date(2026, 1, 1), status=PolicyStatus.SUPERSEDED, url="https://a.cn/1"),
        Version(version=3, published_on=date(2026, 6, 1), status=PolicyStatus.EFFECTIVE, url="https://a.cn/3"),
    ]
    # 应产生问题
    problems = make_policy(versions=versions).validate_semantics()
    # 断言版本链问题
    assert any("版本号不连续" in p for p in problems)


def test_validate_semantics_flags_non_primary_source_as_error() -> None:
    """验证非官方来源被判为错误。

    本项目只允许官方原始来源进入记录，这既是为了可追溯，
    也是为了避免第三方汇编作品的著作权风险。
    """
    # 来源等级改为 secondary
    ref = SourceRef(
        url="https://third-party.example/a.html",
        site="third-party.example",
        fetched_at="2026-10-03T12:00:00+08:00",
        tier=SourceTier.SECONDARY,
    )
    # 应产生问题
    problems = make_policy(source=ref).validate_semantics()
    # 断言包含来源等级问题
    assert any("只允许 primary 来源" in p for p in problems)


def test_validate_semantics_emits_warning_for_missing_effective_from() -> None:
    """验证「现行有效但缺生效日期」为警告而非错误。

    这条规则的分级是刻意的：现实中确实存在「已确认有效但拿不到确切施行日期」
    的情况。若判为错误，维护者会被迫编造日期来让校验通过，
    那才是对数据质量的真正伤害。
    """
    # 现行有效但未填生效日期
    problems = make_policy(effective_from=None).validate_semantics()
    # 应产出警告级问题
    assert any(is_warning(p) and "未填写 effective_from" in p for p in problems)


def test_validate_semantics_emits_warning_for_auto_verified() -> None:
    """验证自动核验的记录带警告提示。"""
    # 核验方式为自动
    problems = make_policy(verified_by=VerifiedBy.AUTOMATED).validate_semantics()
    # 应产出警告级问题
    assert any(is_warning(p) and "尚未经人工核验" in p for p in problems)


# ============================================================
# 规则 11 / 12：填了生效日期就必须说得清依据
# ============================================================
#
# 这两条规则的由来：项目 README 一直写着「施行日期无法从原文确认时，
# effective_from 应当留空并在 verified_note 说明原因」，但没有任何机制执行它。
# 结果实测 9 条记录里有 3 条违反了它——字段填了具体日期，
# 核验说明却承认该日期未确认。下面第一个测试用的就是当初真正漏过去的原文。

# nfra-2026-ai-guidance 在 2026-10-05 修正前使用的核验说明原文（逐字复制）
_NFRA_OLD_VERIFIED_NOTE = (
    "初始种子数据。政策存在与发布日期已通过金融监管总局官网及多家媒体报道交叉确认；"
    "但 effective_from 未在公开摘录中明确写出施行日期条款，需人工打开原文确认是否为「自发布之日起施行」。"
)


def test_validate_semantics_flags_effective_date_contradiction() -> None:
    """验证「填了生效日期却承认该日期未确认」被判为错误。

    这是本规则存在的原因：这类记录同时主张「施行日期是 X」与「不知道是不是 X」，
    比单纯缺日期危险得多——缺日期是显式的空白，使用者会去问；
    自相矛盾却会让下游直接把那个未经确认的日期当事实使用。

    特别注意断言的是**非警告**（即错误），因为警告不足以阻止它入库。
    """
    # 用当初真实漏过去的那段说明
    problems = make_policy(verified_note=_NFRA_OLD_VERIFIED_NOTE).validate_semantics()
    # 应产出错误级问题（不带警告前缀）
    assert any((not is_warning(p)) and "自相矛盾" in p for p in problems)


def test_validate_semantics_accepts_effective_date_with_stated_basis() -> None:
    """反向测试：给出了原文依据的记录不应被判为矛盾。

    若这条测试失败，说明规则退化成「见到任何不确定字眼就报错」，
    那会逼着贡献者删掉说明文字，反而降低数据质量。
    """
    # 说明中明确交代了依据条款
    note = "已核验官方全文，第二十四条载明「本办法自2026年7月1日起施行」，公布文本开头亦作同样声明。"
    # 执行校验
    problems = make_policy(verified_note=note).validate_semantics()
    # 不应出现矛盾告警
    assert not any("自相矛盾" in p for p in problems)


def test_validate_semantics_ignores_uncertainty_about_unrelated_topic() -> None:
    """反向测试：不确定表述与该记录的其他事项有关时，不应误判为日期矛盾。

    真实场景：一条记录的说明常常既讲生效日期，又讲配套细则。
    若「尚未明确」出现在讲细则的那一句里，不能算作对生效日期的承认。
    这条测试守住「按句绑定」这一设计，防止实现被简化成全文关键词匹配。
    """
    # 前半句讲日期（已确认），后半句讲细则（未明确）
    note = "生效日期已核验官方全文，第二十四条载明自2026年7月1日起施行；另有配套实施细则尚未明确。"
    # 执行校验
    problems = make_policy(verified_note=note).validate_semantics()
    # 不应误报
    assert not any("自相矛盾" in p for p in problems)


def test_validate_semantics_requires_basis_from_automated_record() -> None:
    """验证自动核验记录填了生效日期却未交代出处时，产出警告。

    为什么只对 automated 记录提这条要求：自动化整理的产出逻辑是
    「生成一条完整记录」，天然倾向于把字段填满，因此需要额外约束。
    """
    # 自动核验、填了日期、无任何核验说明
    problems = make_policy(verified_by=VerifiedBy.AUTOMATED, verified_note=None).validate_semantics()
    # 应产出警告级问题
    assert any(is_warning(p) and "未说明该日期的出处" in p for p in problems)


def test_validate_semantics_does_not_demand_basis_from_human_record() -> None:
    """反向测试：人工核验的记录不因缺少日期出处而被警告。

    核验行为本身就是依据。若对人工记录也逐条要求说明，
    只会制造大量无意义告警，最终让人习惯性忽略告警——那比没有告警更糟。
    """
    # 人工核验、填了日期、无说明
    problems = make_policy(verified_by=VerifiedBy.HUMAN, verified_note=None).validate_semantics()
    # 不应出现「未说明出处」的告警
    assert not any("未说明该日期的出处" in p for p in problems)


@pytest.mark.parametrize(
    "note,should_flag",
    [
        # 承认未确认的常见写法，都应命中
        ("生效日期需人工核验官方原文", True),
        ("施行日期尚未确认", True),
        ("effective_from 未在公开摘录中明确写出施行日期条款", True),
        ("生效日期未在原文中载明，暂按落款日期填写", True),
        ("生效日期无法确认", True),
        # 已交代依据的写法，不应命中
        ("生效日期依据第二十四条「自2026年7月1日起施行」", False),
        ("已核验官方全文并确认施行日期", False),
        # 谈的是别的事，不应命中
        ("生效日期已核验；配套细则尚未明确", False),
        ("条款数量已核对无误", False),
    ],
)
def test_admits_effective_date_unconfirmed(note: str, should_flag: bool) -> None:
    """逐例验证「是否承认生效日期未确认」的判定。

    其中「未在公开摘录中明确写出」一例是真实缺陷的复现——
    第一版实现只匹配连续词「未明确」，漏掉了这种插入状语的句式。
    """
    # 判定结果与预期一致
    assert (admits_effective_date_unconfirmed(note) is not None) is should_flag


def test_admits_effective_date_unconfirmed_returns_none_for_empty_note() -> None:
    """验证空说明不产生误报。"""
    # None 与空串都应返回 None
    assert admits_effective_date_unconfirmed(None) is None
    # 空字符串同理
    assert admits_effective_date_unconfirmed("") is None


# ============================================================
# 其它属性与方法
# ============================================================

def test_latest_version_returns_highest_version_number() -> None:
    """验证返回版本号最大的版本。"""
    # 两个版本
    versions = [
        Version(version=1, published_on=date(2026, 1, 1), status=PolicyStatus.SUPERSEDED, url="https://a.cn/1"),
        Version(version=2, published_on=date(2026, 6, 1), status=PolicyStatus.EFFECTIVE, url="https://a.cn/2"),
    ]
    # 应返回版本 2
    assert make_policy(versions=versions).latest_version is not None
    # 断言版本号
    assert make_policy(versions=versions).latest_version.version == 2


def test_latest_version_returns_none_when_no_versions() -> None:
    """验证无版本时返回 None 而非抛异常。"""
    # 空版本列表
    assert make_policy(versions=[]).latest_version is None


def test_days_since_verified_uses_injected_today() -> None:
    """验证天数计算使用注入的基准日期，保证测试不随时间漂移。"""
    # 核验日期为 2026-10-03
    policy = make_policy(last_verified=date(2026, 10, 3))
    # 以 2026-12-01 为基准应为 59 天
    assert policy.days_since_verified(date(2026, 12, 1)) == 59


# ============================================================
# 字典转换往返
# ============================================================

def test_policy_dict_roundtrip_preserves_key_fields() -> None:
    """验证对象转字典再转回对象后，关键字段保持一致。

    这个往返很重要：变更流与导出功能都依赖它，
    若字段在转换中丢失，数据会在不被察觉的情况下退化。
    """
    # 构造带义务与主题的记录
    original = make_policy(
        topics=["algorithm", "data-governance"],
        applicable_to=["商业银行"],
        key_obligations=[KeyObligation(clause="第十六条", summary="应建立模型风险管理机制")],
        doc_number="测试发〔2026〕1号",
    )
    # 转字典再转回
    restored = policy_from_dict(policy_to_dict(original))

    # 标识一致
    assert restored.id == original.id
    # 标题一致
    assert restored.title == original.title
    # 状态一致
    assert restored.status == original.status
    # 生效日期一致
    assert restored.effective_from == original.effective_from
    # 义务条数一致
    assert len(restored.key_obligations) == 1
    # 义务内容一致
    assert restored.key_obligations[0].clause == "第十六条"
    # 主题一致
    assert restored.topics == original.topics
    # 发文字号一致
    assert restored.doc_number == "测试发〔2026〕1号"


def test_policy_from_dict_raises_on_missing_required_field() -> None:
    """验证缺少必填字段时明确报错，而不是静默取默认值。

    对合规数据而言，「字段缺失」比「程序崩溃」危险得多——
    崩溃看得见，缺失看不见。
    """
    # 构造缺 id 的字典
    data = policy_to_dict(make_policy())
    # 删除必填字段
    data.pop("id")
    # 应抛出 KeyError
    with pytest.raises(KeyError):
        policy_from_dict(data)


# ============================================================
# 执法记录（罚单）—— 语义规则与字典转换
# ------------------------------------------------------------
# 这一组测试的存在理由：EnforcementRecord 的字段大多是从官方公示
# 逐字抄录的，最容易出的错不是「抄错」而是「补全」——
# 人看到 legal_basis 空着会本能地想填上。因此每条规则都要有一条
# 测试证明它**真的会被触发**，否则规则只是写在文档里的一句愿望。
# ============================================================

def make_enforcement(**overrides: object) -> EnforcementRecord:
    """构造一条最小可用的 ``EnforcementRecord``，可用关键字覆盖任意字段。"""
    # 默认值取「一切正常」的形态，便于每个测试只改动要验证的那一项
    defaults: dict = {
        "id": "pbc-2026-yinfa-104",                       # 标识
        "decision_no": "银罚决字〔2026〕104号",            # 决定书文号
        "party": "某某银行股份有限公司",                    # 当事人
        "violation_type": "违反金融统计管理规定",           # 违规事实
        "penalty_content": "警告，罚款100万元",            # 处罚内容
        "authority": "中国人民银行",                       # 决定机关
        "decision_date": "2026年9月8日",                   # 决定日期
        "published_on": date(2026, 9, 24),                # 公示日期
        "publicity_period": "五年",                        # 公示期限
        "party_type": "institution",                       # 当事人类型
        "domain": ["支付清算"],                            # 涉及领域
        "source": SourceRef(                               # 溯源信息
            url="https://www.pbc.gov.cn/x/index.html",     # 公示文书地址
            site="pbc.gov.cn",                             # 站点
            fetched_at="2026-10-05T22:31:00+08:00",        # 抓取时间
            tier=SourceTier.PRIMARY,                       # 来源等级
            http_status=200,                               # 状态码
        ),
        "last_verified": None,                             # 尚未人工核验
        "verified_by": VerifiedBy.AUTOMATED,                # 核验方式：机器抄录
        # automated 记录必须交代抄录范围，因此默认值里就写上——
        # 否则每条正常记录都会产生一条警告，规则会因此被无视
        "notes": "字段抄自官方公示原文，未逐项人工核对",      # 备注
    }
    # 应用覆盖项
    defaults.update(overrides)
    # 构造对象
    return EnforcementRecord(**defaults)  # type: ignore[arg-type]


def test_enforcement_clean_record_produces_no_problems() -> None:
    """验证形态正常的罚单不产生任何问题。

    这条是其余规则测试的对照物：若默认值本身就触发警告，
    后面那些「规则 X 会触发」的断言就无法区分是规则生效还是默认值有问题。
    """
    # 全字段正常，应无问题
    assert make_enforcement().validate_semantics() == []


def test_enforcement_flags_empty_decision_no() -> None:
    """验证文号为空时报错——文号是罚单的唯一标识。"""
    # 清空文号
    problems = make_enforcement(decision_no="   ").validate_semantics()
    # 应有一条问题
    assert len(problems) == 1
    # 且是错误而非警告：没有文号的记录无法被引用、无法回查原文
    assert not is_warning(problems[0])
    # 信息里点明是哪个字段
    assert "decision_no" in problems[0]


def test_enforcement_flags_empty_party() -> None:
    """验证当事人为空时报错——不知道对谁的处理，记录没有意义。"""
    # 清空当事人
    problems = make_enforcement(party="").validate_semantics()
    # 应有一条错误
    assert len(problems) == 1
    # 是错误
    assert not is_warning(problems[0])
    # 字段名出现
    assert "party" in problems[0]


def test_enforcement_flags_empty_violation_type() -> None:
    """验证违规事实为空时报错。

    这是本类型最关键的一条规则：罚单的全部价值在于「监管实际在查什么」，
    缺了违规事实，记录只剩下一个文号和一个金额——
    而金额对我们的用途（校准判定规则）毫无帮助。
    """
    # 清空违规事实
    problems = make_enforcement(violation_type=" ").validate_semantics()
    # 应有一条错误
    assert len(problems) == 1
    # 是错误而非警告：没有违规事实的罚单在库里等于噪声
    assert not is_warning(problems[0])
    # 字段名出现
    assert "violation_type" in problems[0]


def test_enforcement_warns_when_automated_record_lacks_transcription_scope() -> None:
    """验证机器抄录的记录未交代抄录范围时给出警告（而非错误）。

    为什么是警告：一条字段正确、只是没写说明的记录仍然可用，
    把它判为错误会逼着维护者随便补一句说明来让校验通过，
    那反而把「说明」变成了走过场。
    """
    # 抹掉抄录范围说明
    problems = make_enforcement(notes=None).validate_semantics()
    # 应有一条问题
    assert len(problems) == 1
    # 且必须是警告
    assert is_warning(problems[0])
    # 提示里要说清该补什么
    assert "notes" in problems[0]


def test_enforcement_does_not_warn_about_scope_for_human_verified_record() -> None:
    """验证人工核验过的记录不因「缺抄录说明」被警告。

    反向测试：这条规则的对象是 automated。若把判断写成了
    「notes 里没有出处就警告」而不看 verified_by，人工核过的记录
    也会被无差别提示，规则就失去了分辨能力。
    """
    # 人工核验，且不写 notes
    problems = make_enforcement(verified_by=VerifiedBy.HUMAN, notes=None).validate_semantics()
    # 不应有任何问题
    assert problems == []


def test_enforcement_warns_when_tech_violation_not_classified() -> None:
    """验证违规事实涉及技术类要求却没归类时给出警告。

    这条规则的定位是**待办信号**而不是错误：归类是人工工作，
    尚未完成是正常状态。但它必须可见，因为本类记录的价值
    正是靠归类与关联建立起来的——未归类的记录无法参与任何聚合，
    而未归类又不可见时，库里会攒下一批「看起来正常但查不出东西」的记录。
    """
    # 违规含技术类词，但 domain 空着
    problems = make_enforcement(violation_type="违反数据安全管理规定", domain=[]).validate_semantics()
    # 应有一条警告
    assert len(problems) == 1
    # 是警告
    assert is_warning(problems[0])
    # 点明是 domain 的问题
    assert "domain" in problems[0]


@pytest.mark.parametrize(
    "violation",
    [
        "违反数据安全管理规定",   # 数据
        "违反信用信息采集管理规定", # 信息
        "违反网络安全管理规定",   # 网络
        "违反金融科技管理规定",   # 科技
        "违反算法推荐管理规定",   # 算法
        "违反智能投顾管理规定",   # 智能
        "违反信息系统管理规定",   # 技术
    ],
)
def test_enforcement_tech_hint_words_all_take_effect(violation: str) -> None:
    """逐词验证技术类关键词表确实生效，而不是只有第一个词能用。

    参数化的必要性：这类关键词表最容易出的错是「写了几个词，
    但只有前一个能让断言通过」——测试看起来覆盖了七种情况，
    实际只验证了一种。逐词传入才能真正钉住整张表。
    """
    # 每条都只改违规事实，其余保持正常
    problems = make_enforcement(violation_type=violation, domain=[]).validate_semantics()
    # 都必须产生归类提示
    assert len(problems) == 1, f"关键词未生效：{violation}"
    # 且是警告
    assert is_warning(problems[0])


def test_enforcement_does_not_warn_when_domain_filled() -> None:
    """验证已归类的技术类违规不再提示——否则提示会退化成噪声。"""
    # 已归类
    problems = make_enforcement(violation_type="违反数据安全管理规定", domain=["数据安全"]).validate_semantics()
    # 无问题
    assert problems == []


def test_enforcement_warns_when_legal_basis_has_no_stated_source() -> None:
    """验证填写了处罚依据却没说出处时给出警告。

    官方公示不含处罚依据，因此这个字段一旦有值，必然是另查所得。
    要求写明出处，是为了挡住「从违法行为类型倒推条款」这类无出处的推断——
    它看起来很有价值，但使用者无法分辨它是抄的还是猜的。
    """
    # 填了依据，notes 里只有抄录范围、没有出处信息
    problems = make_enforcement(legal_basis="《数据安全法》第 27 条").validate_semantics()
    # 应有一条警告
    assert len(problems) == 1
    # 是警告
    assert is_warning(problems[0])
    # 点明 legal_basis
    assert "legal_basis" in problems[0]


@pytest.mark.parametrize(
    "hint",
    [
        "依据见《数据安全法》第 27 条",   # 依据
        "出处：人民银行官网处罚决定书",     # 出处
        "来源为行政处罚决定书扫描件",       # 来源
        "对应条款为《数据安全法》第 27 条", # 条款
    ],
)
def test_enforcement_legal_basis_source_hints_all_accepted(hint: str) -> None:
    """逐词验证「已交代出处」的四种说法都能被接受。

    这四个词是给使用者写 notes 用的词表。若某个词其实不生效，
    使用者按词表写好了说明却仍被警告，最可能的反应是把警告当噪声忽略，
    而不是回来读代码——因此必须逐个验证。
    """
    # 填了依据并在 notes 中交代出处
    problems = make_enforcement(
        legal_basis="《数据安全法》第 27 条",
        notes=f"字段抄自官方公示原文，未逐项人工核对。{hint}",
    ).validate_semantics()
    # 不应产生任何问题
    assert problems == [], f"出处词未被接受：{hint}"


def test_enforcement_legal_basis_rule_is_not_silenced_by_transcription_boilerplate() -> None:
    """反向测试：抄录范围说明不能顶替「处罚依据的出处」。

    这条测试对应一个真实撞过的坑：规则 5 原本把「原文」也算作
    「已交代出处」的词，而 automated 记录必写的抄录范围说明
    （「字段抄自官方公示原文，未逐项人工核对」）里天然含「原文」。
    结果是这条规则对**每一份合规填写的记录**都不触发——
    它从未拦住任何一条无出处的依据，但校验输出一直是「通过」。

    这类「因为撞词而永不触发」的规则比没有规则更危险：
    它让人以为这一项已经被守住了，于是没人再去人工检查。
    因此这里显式断言「只写抄录范围说明 + 填了依据」必须产生警告。
    """
    # 依据填了，notes 只有抄录范围说明（含「原文」二字）
    problems = make_enforcement(
        legal_basis="《数据安全法》第 27 条",
        notes="字段抄自官方公示原文，未逐项人工核对",
    ).validate_semantics()
    # 必须产生警告——否则规则被这行样板文字静默消音了
    assert len(problems) == 1, "抄录范围说明把「依据出处」这条规则消音了"
    # 且必须是关于 legal_basis 的警告
    assert is_warning(problems[0])
    assert "legal_basis" in problems[0]


def test_enforcement_does_not_warn_when_legal_basis_empty() -> None:
    """验证 legal_basis 留空不会产生任何提示。

    这是本类最重要的一条「沉默」：官方公示本来就不含处罚依据，
    因此留空是**事实的正确描述**而不是缺失。若留空被警告，
    维护者会被推着去倒推一个条款填上——那正是本项目最想避免的事。
    """
    # 依据留空（默认值即如此）
    problems = make_enforcement().validate_semantics()
    # 无任何问题
    assert problems == []
    # 显式再确认一次：留空 + 已有的 notes 不构成问题
    assert make_enforcement(legal_basis=None).validate_semantics() == []


def test_enforcement_round_trip_preserves_verbatim_fields() -> None:
    """验证字典转换往返后，逐字抄录的字段一字不差。

    这些字段是本类记录的全部价值所在，被格式化、被解析都会造成信息损失：
    「2026年9月8日」变成 2026-09-08 会与公示日期混同；
    「警告，罚款100万元」被拆成数字会丢掉「警告」这个处罚种类。
    """
    # 构造一条记录
    original = make_enforcement()
    # 转字典再转回
    restored = enforcement_from_dict(enforcement_to_dict(original))

    # 文号逐字一致（含中文括号）
    assert restored.decision_no == "银罚决字〔2026〕104号"
    # 处罚内容逐字一致，未被拆解
    assert restored.penalty_content == "警告，罚款100万元"
    # 决定日期保留中文写法，未被转成 ISO
    assert restored.decision_date == "2026年9月8日"
    # 公示日期是 date 对象
    assert restored.published_on == date(2026, 9, 24)
    # 当事人类型保留
    assert restored.party_type == "institution"
    # 核验方式保留为枚举
    assert restored.verified_by == VerifiedBy.AUTOMATED
    # 来源中的状态码保留为整数而非字符串
    assert restored.source is not None
    assert restored.source.http_status == 200


def test_enforcement_from_dict_raises_on_missing_required_field() -> None:
    """验证缺少必填字段时明确报错，而不是静默取默认值。"""
    # 构造缺 id 的字典
    data = enforcement_to_dict(make_enforcement())
    # 删除必填字段
    data.pop("id")
    # 应抛出 KeyError
    with pytest.raises(KeyError):
        enforcement_from_dict(data)


def test_enforcement_schema_rejects_non_text_penalty_content() -> None:
    """反向测试：处罚内容写成数字时必须被 schema 拦下。

    「罚款1712.4万元」与「1712.4」在合规语境里不是同一条信息——
    后者丢掉了币种、数量级和处罚种类。

    这条约束放在 schema 层而不是模型层，是因为 ``_parse_text`` 的职责是
    「把 YAML 的隐式类型差异抹平」（如把 datetime 归一成字符串），
    它必然要接受数字；若同时要求它拒绝数字，这两个目标会互相冲突。
    因此「必须是字符串」由 schema 声明，模型层只负责取值可解释。
    这条测试盯住的正是这个分工——若哪天有人把 schema 里的
    ``"type": "string"`` 放宽，它会立刻失败。
    """
    # 构造一份合法字典后把处罚内容改成数字
    data = enforcement_to_dict(make_enforcement())
    # 篡改类型
    data["penalty_content"] = 1712.4
    # 结构校验应报错
    errors = validate_against_schema(data, load_enforcement_schema())
    # 必须至少有一条错误
    assert errors, "数字类型的处罚内容未被 schema 拦下"
    # 且错误指向 penalty_content 这个字段
    assert any("penalty_content" in err for err in errors)
