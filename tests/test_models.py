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
