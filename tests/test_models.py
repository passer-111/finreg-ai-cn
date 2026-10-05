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
    InstrumentType,
    KeyObligation,
    Policy,
    PolicyStatus,
    SourceRef,
    SourceTier,
    VerifiedBy,
    Version,
    admits_effective_date_unconfirmed,
    is_warning,
    policy_from_dict,
    policy_to_dict,
    strip_warning_prefix,
    with_location,
)
# 导入存储层的校验归一化函数
from finreg_ai.store import normalize_for_validation


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
