"""数据模型 —— 政策记录、版本、来源、数据源的带类型表示。

为什么不用裸字典
----------------
政策记录有 20 多个字段，其中一半是时效性字段（status / effective_from /
effective_until / supersedes / superseded_by / last_verified 等）。
如果用裸字典，一处 ``rec["effectiv_from"]`` 的拼写错误要到运行时才暴露，
而且 YAML 里多写的字段会被静默忽略——这对合规数据是不可接受的。
因此这里用 dataclass + Enum 把结构固化，配合 JSON Schema 做双重校验：

- JSON Schema 校验 YAML 文件的**形状**（类型、枚举、格式）
- dataclass 提供**代码层的类型安全**与**业务规则校验**（如版本链一致性）

两者职责不重复：schema 保证「文件长得对」，dataclass 保证「业务上讲得通」。
"""

# 导入 dataclass 相关工具：dataclass 装饰器、字段默认值标记、字典转对象
from dataclasses import dataclass, field
# 导入日期与时间类型，用于类型标注与归一化。
# 需要 datetime 的原因是 PyYAML 会把不带引号的 ISO 时间戳解析成 datetime，
# 而归一化函数必须先识别出这种类型（datetime 是 date 的子类，判断顺序有讲究）。
from datetime import date, datetime
# 导入枚举基类，用于定义受控取值
from enum import Enum
# 导入正则，用于按句读切分核验说明（判断「同一句内」是否自相矛盾）
import re
# 导入任意类型标注，用于承载自由格式字段
from typing import Any


# ============================================================
# 枚举定义 —— 受控取值，防止自由发挥导致数据在半年后退化
# ============================================================

class PolicyStatus(str, Enum):
    """政策的法律状态。

    这是整个模型中最重要的枚举：用户来查知识库，第一个要问的就是
    「这条现在还有效吗」。因此取值必须由核验得出，严禁推测。
    """

    # 起草阶段，尚未公开征求意见
    DRAFT = "draft"
    # 已发布征求意见稿，尚未定稿生效
    CONSULTATION = "consultation"
    # 已正式公布，但生效日期尚未到达
    PUBLISHED = "published"
    # 现行有效，处于生效期内
    EFFECTIVE = "effective"
    # 部分条款生效，或已部分失效
    PARTIALLY_EFFECTIVE = "partially_effective"
    # 已被修订，修订后版本现行有效（须通过 amended_by 指向新版本）
    AMENDED = "amended"
    # 已被明文废止
    REPEALED = "repealed"
    # 已被新文件取代但未明文废止（新文件通过 supersedes 反向指向本条）
    SUPERSEDED = "superseded"
    # 有效期届满自动失效
    EXPIRED = "expired"
    # 无法确认状态，须在 verified_note 中说明核验过程与障碍
    UNKNOWN = "unknown"

    @property
    def is_currently_valid(self) -> bool:
        """判断该状态是否表示「当前具有法律效力」。

        用途：流水线筛出「当前有效」的政策子集，供合规自查工具消费。
        注意 ``AMENDED`` 与 ``PARTIALLY_EFFECTIVE`` 也算有效——
        它们是「有效但需注意版本」，与「已废止」有本质区别。
        """
        # 这些状态都意味着政策当前仍在发挥作用
        return self in {
            PolicyStatus.EFFECTIVE,            # 完整有效
            PolicyStatus.PARTIALLY_EFFECTIVE,  # 部分有效
            PolicyStatus.AMENDED,              # 已修订，新版有效
        }

    @property
    def is_terminal(self) -> bool:
        """判断该状态是否为「终态」——即该文件已彻底退出法律生命。

        用途：变更检测时判断是否需要为这条记录继续寻找取代者。
        已废止的文件不需要再追踪后续变化。
        """
        # 这些状态表示文件已不再产生任何法律效果
        return self in {
            PolicyStatus.REPEALED,    # 已废止
            PolicyStatus.SUPERSEDED,  # 已被取代
            PolicyStatus.EXPIRED,     # 已过期
        }


class Bindingness(str, Enum):
    """约束力性质。

    与 ``InstrumentType`` 正交：``InstrumentType`` 回答「这是什么级别的文件」，
    ``Bindingness`` 回答「它是否产生强制义务」。

    这个区分在实务中极其重要：一份「指导意见」在文件层级上只是部门规范性文件，
    但其正文中的「须」「不得」「严禁」等表述对应实质义务，
    违反同样会招致监管措施。只看文件层级会严重低估合规压力。
    """

    # 产生强制法律义务，违反面临行政处罚或监管措施
    BINDING = "binding"
    # 不直接设定义务，但构成监管预期，实践中具有事实约束力
    SOFT_LAW = "soft-law"
    # 鼓励性与倡导性表述，无强制力
    GUIDANCE = "guidance"
    # 征求意见阶段，尚未生效，不产生任何义务
    CONSULTATION = "consultation"


class InstrumentType(str, Enum):
    """文件效力层级，由高到低排列。

    这是判断约束力的第一依据，也决定了它能否作为合规主张的法律基础。
    """

    # 全国人大及其常委会制定，效力最高
    LAW = "法律"
    # 国务院制定
    ADMINISTRATIVE_REGULATION = "行政法规"
    # 国务院部门制定，经部门首长签署公布
    DEPARTMENT_RULE = "部门规章"
    # 国务院部门制定的具有普遍约束力的文件，程序要求低于部门规章
    DEPARTMENT_NORMATIVE = "部门规范性文件"
    # 交易所、行业协会等自律组织制定
    INDUSTRY_SELF_REGULATION = "行业自律规则"
    # 国家标准，本身不设法律义务
    NATIONAL_STANDARD = "国家标准"
    # 地方人大或地方政府制定
    LOCAL_DOCUMENT = "地方性文件"
    # 无法归入上述类别
    OTHER = "其他"


class AIRelevance(str, Enum):
    """与 AI 合规主题的相关度，用于筛选与排序。

    这个字段决定了一条记录在检索结果中的权重：
    ``CORE`` 是用户真正需要的，``BACKGROUND`` 只提供上下文。
    """

    # 主体内容直接规范 AI 的开发或应用
    CORE = "core"
    # 部分条款涉及 AI
    RELATED = "related"
    # 仅提供上位法背景（如网络安全法之于 AI 合规）
    BACKGROUND = "background"


class SourceTier(str, Enum):
    """来源权威等级。

    ``PRIMARY`` 是唯一允许写入政策记录的等级。
    ``SECONDARY`` 仅可用于发现线索，其内容不得进入任何记录字段——
    这是为了规避第三方汇编作品的著作权风险，也为了保证可追溯性。
    """

    # 官方原始发布渠道
    PRIMARY = "primary"
    # 第三方转载、律所解读等
    SECONDARY = "secondary"
    # 来源不明，一律不采用
    UNVERIFIED = "unverified"


class VerifiedBy(str, Enum):
    """最近一次核验的执行方式。

    这个字段的存在是为了让使用者能判断记录可信度：
    ``AUTOMATED`` 表示仅由爬虫确认页面可访问且内容哈希未变，
    ``HUMAN`` 表示已由人工打开官方页面逐项核对状态字段。
    """

    # 人工核验
    HUMAN = "human"
    # 自动核验
    AUTOMATED = "automated"


# ============================================================
# 问题严重级别
# ============================================================
#
# 校验器需要区分「错误」与「警告」，因为对合规数据而言，
# 「不知道」与「写错了」是两种完全不同的问题：
#
# - **错误**：数据自相矛盾或违反硬性约束（如失效日期早于生效日期）。
#   这类问题会让使用者得出错误结论，必须阻断入库。
#
# - **警告**：数据本身可信，但存在已知的不完整或待办事项
#   （如现行有效但施行日期无法确认）。
#   这类问题不应阻断入库——否则维护者会被迫编造一个日期来让校验通过，
#   那反而制造了虚假信息。
#
# 实现方式用字符串前缀而非自定义异常类，是为了让问题列表保持
# 可读的纯文本形式，便于直接打印到 CI 日志与终端。
WARNING_PREFIX = "[警告]"


def is_warning(issue: str) -> bool:
    """判断一条校验问题是否为警告级别。

    供调用方决定是阻断流程还是仅提示。
    """
    # 检查前缀
    return issue.startswith(WARNING_PREFIX)


def strip_warning_prefix(issue: str) -> str:
    """去掉警告前缀，返回可读的问题描述。"""
    # 有前缀则去掉，无前缀原样返回
    if issue.startswith(WARNING_PREFIX):
        # 去掉前缀并清理多余空格
        return issue[len(WARNING_PREFIX) :].strip()
    # 原样返回
    return issue


def with_location(issue: str, location: str) -> str:
    """给问题描述附上位置信息（如文件名），并保证警告标记始终在最前。

    为什么需要这个专门的函数：警告级别是靠字符串前缀识别的，
    如果简单地把位置信息拼在最前面（``f"{location}: {issue}"``），
    警告标记就会被挤到中间，``is_warning()`` 便无法识别，
    结果是一条「警告」被当成「错误」处理——整个分级机制静默失效。

    这类「因为字符串拼接顺序导致逻辑失效」的问题很难通过肉眼 review 发现，
    因此把它收进一个函数，让正确做法成为唯一做法。
    """
    # 警告级问题：保持前缀在最前，位置信息插在其后
    if is_warning(issue):
        # 剥离原前缀后重新组合
        return f"{WARNING_PREFIX} {location}: {strip_warning_prefix(issue)}"
    # 错误级问题：位置信息直接前置
    return f"{location}: {issue}"


# ============================================================
# 「生效日期是否真的已核验」的检测
# ============================================================
#
# 为什么需要它
# ------------
# 本项目有一条明确规则：施行日期无法从原文确认时，``effective_from``
# 应当留空并在 ``verified_note`` 中说明原因。
#
# 但早期实测发现，规则写了却没有机制执行，于是出现了最坏的一种数据：
# **字段填了日期，核验说明却承认该日期未确认。** 记录同时说「是 2026-06-18」
# 和「不知道是不是」，而下游只能看到那个日期，会把它当事实用。
#
# 这不是个例。实测 9 条记录中有 3 条犯此错，共同成因是：
# 自动化整理（``verified_by: automated``）的产出逻辑是「生成一条完整记录」，
# 而正确做法是「不确定就留空」。填满字段的倾向是结构性的，不会自己消失，
# 因此必须由程序来强制。

# 出现这些词，说明该句在谈论生效/施行日期
_DATE_TOPIC_WORDS = (
    "生效日",             # 生效日期
    "施行日",             # 施行日期
    "effective_from",    # 字段名本身，贡献者常在说明里直接引用
    "生效时间",           # 同义表述
    "施行时间",           # 同义表述
)

# 出现这些表述，说明该句承认了「不知道」
_DATE_UNCERTAIN_WORDS = (
    "需人工", "待核验", "待确认", "待核实", "待补充", "待定",
    "未确认", "尚未确认", "未明确", "尚未明确", "未予明确", "不明确",
    "无法确认", "难以确认", "不能确认", "需确认", "需核实", "尚未核验",
    "未载明", "未查到", "未获", "有待核实", "存疑", "尚需", "未知",
)

# 中文里否定词与动词之间常插入状语，例如
# 「未**在公开摘录中**明确写出施行日期条款」「尚未**经人工**确认」。
# 单纯匹配「未明确」这样的连续词会漏掉它们——这一点在实测中真实漏过一次：
# nfra-2026-ai-guidance 的核验说明用的正是这种句式，第一版检测逻辑没识别出来。
# 因此补一条允许中间有间隔的正则，把「否定 + 若干字 + 认知动词」的形态一并覆盖。
_DATE_NEGATION_GAP = re.compile(
    r"(尚未|还未|仍未|未曾|未经|未|没有|无)[^，。；]{0,12}(载明|明确|确认|核验|核实|写明|注明|查证|取得|掌握)"
)

# 出现这些词，说明该说明交代了日期的出处，而不只是复述日期
_DATE_BASIS_HINTS = (
    "依据", "出处", "来源", "条款", "载明", "核验", "原文", "核对",
    "确认", "明确", "施行", "生效", "第",
)


def _split_sentences(text: str | None) -> list[str]:
    """按中文句读切分文本。

    切分的目的是做「同一句内」的绑定判断：只有当「生效日期」这个话题
    与「不确定」这种表态出现在同一句话里，才算自相矛盾。
    """
    # 以句号、分号、换行为界切分，并丢弃空白片段
    return [s for s in re.split(r"[。；;\n]", text or "") if s.strip()]


def admits_effective_date_unconfirmed(note: str | None) -> str | None:
    """判断核验说明是否承认「生效日期尚未确认」；是则返回该句，否则返回 None。

    为什么按句绑定，而不是见到「未确认」四个字就报警
    ------------------------------------------------
    简单关键词会大量误报。一条说明完全可能写
    「已核验生效日期为 2023-08-15；另有实施细则尚未明确」——
    句中的「尚未明确」与生效日期无关，不该触发告警。
    要求话题词与不确定表述同句出现，可以滤掉绝大多数这类噪声。

    局限（必须说清，不要高估它）
    ----------------------------
    这是**启发式护栏，不是证明**。它只能拦住「承认了却仍填值」这一种形态；
    拦不住「悄悄编一个日期且全文不提任何不确定」——那种情况与诚实填写的记录
    在字面上无法区分，只能靠人工核验原文，这也正是 ``last_verified`` 台账
    和 ``verified_by`` 字段存在的理由。因此本函数的定位是**兜底网**：
    它把最危险、最容易发生的一类矛盾变成可见的失败，而不是宣称日期都被验证过。
    """
    # 逐句检查
    for sentence in _split_sentences(note):
        # 先判断这一句是否在谈生效日期，无关的句子直接跳过
        if not any(t in sentence for t in _DATE_TOPIC_WORDS):
            # 与本函数无关，继续下一句
            continue
        # 再判断这一句是否承认了「不知道」，两种形态并列检查：
        #   ① 连续的不确定表述（如「需人工核验」「尚未确认」）
        #   ② 「否定词 + 状语 + 认知动词」（如「未在公开摘录中明确写出」）
        if any(u in sentence for u in _DATE_UNCERTAIN_WORDS) or _DATE_NEGATION_GAP.search(sentence):
            # 返回该句供报错信息引用，便于作者直接定位
            return sentence.strip()
    # 未发现矛盾
    return None


# ============================================================
# 值对象 —— 嵌套结构
# ============================================================

@dataclass
class Version:
    """政策的一个版本。

    为什么需要版本链而不是只保留最新版
    ----------------------------------
    「政策被修订」在合规场景中是高频事件，但绝大多数知识库只保留最新版，
    导致无法追溯历史义务。而合规审查恰恰经常需要判断
    「当时适用的是哪一版，当时的义务是什么」。
    因此每个版本都独立记录，并在顶层通过 supersedes/superseded_by 串联。
    """

    # 版本序号，从 1 开始递增，不可跳号
    version: int
    # 该版本公布日期
    published_on: date
    # 该版本自身在其存续期内的状态；历史版本通常为 superseded
    status: PolicyStatus
    # 该版本官方原文链接，必须指向官方站点
    url: str
    # 该版本生效起始日期；征求意见稿版本为 None
    effective_from: date | None = None
    # 该版本失效日期；下一个版本生效之日或废止之日
    effective_until: date | None = None
    # 官方页面已不可访问时的存档链接
    archived_url: str | None = None
    # 正文 SHA-256 摘要，格式 sha256:<64位十六进制>
    content_hash: str | None = None
    # 相对上一版本的主要变化，人工提炼，禁止 LLM 生成后不经复核写入
    change_note: str | None = None


@dataclass
class SourceRef:
    """记录中的来源溯源信息。

    每条记录都必须能追溯到官方页面，且带有可复核的时间戳与内容指纹。
    没有这一层，知识库就退化为不可验证的传言集合。
    """

    # 官方原文链接（最新版本的页面地址）
    url: str
    # 来源站点域名，用于按源统计与失效排查
    site: str
    # 抓取时间，ISO 8601 带时区偏移
    fetched_at: str
    # 来源权威等级，默认 primary
    tier: SourceTier = SourceTier.PRIMARY
    # 抓取时的 HTTP 状态码；非 200 说明官方页面可能异常
    http_status: int | None = None
    # 抓取到的正文 SHA-256 摘要
    content_hash: str | None = None
    # 仓库内原始快照的相对路径
    snapshot_path: str | None = None


@dataclass
class KeyObligation:
    """从政策原文中人工提炼的一条关键义务。

    这是本项目与纯爬虫项目的核心区别：爬虫只能给你原文，
    提炼才能给你可执行的义务清单。也因此每条义务都必须能对应到原文的具体条款，
    禁止无出处的概括。
    """

    # 原文条款定位，如「十六」「第 16 条」，必须与原文标号一致
    clause: str
    # 义务内容概括，一句话说清「谁必须做什么」；人工撰写，非 AI 摘要
    summary: str
    # 义务类型，用于聚合统计；为 None 表示尚未归类
    obligation_type: str | None = None
    # 该义务涉及的主题标签
    tags: list[str] = field(default_factory=list)


@dataclass
class EnforcementRecord:
    """一条执法记录（罚单），本项目区别于政策文件的第二种数据类型。

    为什么它值得单列一个类型，而不是当作「另一种政策」
    --------------------------------------------------
    政策文件回答的是「应该怎么做」，执法记录回答的是「没这么做会怎样」。
    两者的字段结构、来源形态、更新节奏、可信度判断方式都不同：
    政策靠人工提炼义务，罚单靠机器逐行抄录公示表格。
    硬塞进同一个类型，会逼着两边共用一套校验规则，结果是两边都约束不好。

    本项目的立场：**罚单是政策的校验器，不是政策的补充**
    ----------------------------------------------------
    政策记录里的 ``key_obligations`` 是人工从原文提炼的，提炼得对不对
    无从验证；而罚单里「违反数据安全管理规定」这样的表述，直接说明
    监管实际在查什么。两者对照，才能发现「义务清单写漏了」或
    「写了但监管根本不查」这两类问题。

    因此 ``related_policies`` 是本类里**最重要的一个字段**：
    它把「违规事实」与「对应义务」连起来。空着也能用（罚单本身仍可检索），
    但只有填了，知识库才真正具备「反向校验义务清单」的能力。

    字段填写纪律：不知道就留空，不要为了完整而填
    --------------------------------------------
    官方行政处罚公示**不含「处罚依据」条款**（实测人民银行公示表格只有
    违法行为类型、处罚内容、决定机关、决定日期等列）。因此 ``legal_basis``
    默认就是空的，这不是缺失而是事实。绝不从「违反数据安全管理规定」倒推
    出「违反了《数据安全法》第 X 条」——那种倒推看起来很有价值，
    但它是一条**没有出处的推断**，一旦写进记录就会和官方原文混在一起，
    而使用者无法分辨哪条是抄的、哪条是猜的。
    """

    # ---- 第 1 组：身份（抄自公示原文） ----

    # 记录唯一标识，格式 <issuer_code>-<年份>-<文号短名>，如 pbc-2026-yinfa-104
    id: str
    # 行政处罚决定书文号，原样抄录，如「银罚决字〔2026〕104号」
    decision_no: str
    # 被处罚当事人名称，原样抄录。个人当事人通常已被官方脱敏为「蒋某」
    party: str

    # ---- 第 2 组：违规事实与处罚（抄自公示原文，逐字不改） ----

    # 违法行为类型。原文为多条时用换行或分号保留原貌，不要合并改写
    violation_type: str
    # 行政处罚内容，原样抄录，如「警告，没收违法所得1.621747万元，罚款1712.4万元」。
    # 刻意不解析成金额数字：原文的表述方式（警告/没收/罚款组合、
    # 是否写明具体数额）本身就是信息，解析会把它压平。
    penalty_content: str
    # 作出行政处罚决定的机关名称
    authority: str
    # 处罚依据条款。**官方公示通常不提供，留空即为正确**，理由见类文档
    legal_basis: str | None = None

    # ---- 第 3 组：时间 ----

    # 作出决定日期，保留原文的中文写法（如「2026年9月8日」）。
    # 刻意不转成 date：一是决定日期与公示日期语义不同、混用会让时间线错乱，
    # 二是官方页面本身用中文写法，转格式等于替官方改了一遍原文。
    decision_date: str | None = None
    # 公示日期。来自列表页，是**该记录在官方渠道可见的时间**，与决定日期不同
    published_on: date | None = None
    # 公示期限，如「五年」
    publicity_period: str | None = None

    # ---- 第 4 组：归类（人工填写） ----

    # 当事人类型：机构 / 个人 / 未知。个人处罚与机构处罚的意义不同
    # （个人处罚多为对责任人追责，机构处罚才反映制度性要求）
    party_type: str | None = None
    # 涉及领域，如 数据安全 / 征信管理 / 支付清算
    domain: list[str] = field(default_factory=list)
    # 对应到本库中的政策记录 id 列表。**填了它，这份罚单才真正有用**——
    # 它建立了「监管实际处罚的行为」与「我们提炼出的义务」之间的对应关系
    related_policies: list[str] = field(default_factory=list)

    # ---- 第 5 组：溯源 ----

    # 所属法域，MVP 阶段固定为 CN
    jurisdiction: str = "CN"
    # 来源信息。url 必须是**该当事人所在的那份公示文书的地址**，不是机构首页
    source: SourceRef | None = None
    # 最近一次核验日期
    last_verified: date | None = None
    # 核验方式。自动化抄录的记录此处为 automated
    verified_by: VerifiedBy = VerifiedBy.AUTOMATED
    # 备注：核验过程、存疑之处、字段留空的原因
    notes: str | None = None

    def validate_semantics(self) -> list[str]:
        """校验记录的业务规则，返回问题列表（空列表表示通过）。

        为什么这些规则值得写进代码而不是靠自觉
        --------------------------------------
        罚单字段多、且大部分是抄录的，最容易出的错不是「抄错」而是
        「补全」——人看到 legal_basis 空着会本能地想填上。
        把「空着是正常的」和「填了必须有出处」写成可执行的校验，
        才能让下一个人不必读完整篇设计理由就知道该怎么做。
        """
        # 问题收集
        problems: list[str] = []

        # 规则 1：身份字段不得为空。这两项是记录的主键与主体，缺了就无法引用
        if not self.decision_no.strip():
            # 决定书文号为空
            problems.append(f"[{self.id}] decision_no 为空 —— 决定书文号是罚单的唯一标识，必须填写")
        # 当事人必须填写
        if not self.party.strip():
            # 当事人为空
            problems.append(f"[{self.id}] party 为空 —— 没有当事人就无法判断这是对谁的处理")

        # 规则 2：违规事实必须填写。罚单若没有违规事实，就只剩下一个文号和金额，
        # 对「校准判定规则」毫无用处——而校准判定规则正是本项目收录罚单的理由
        if not self.violation_type.strip():
            # 违规事实为空
            problems.append(
                f"[{self.id}] violation_type 为空 —— 违规事实是罚单的核心价值所在，"
                f"缺了它这份记录只剩下文号与金额"
            )

        # 规则 3：自动化抄录的记录必须说明「未逐项核对」以及抄录范围。
        # 这与政策记录的同类规则（automated 填了生效日期就必须交代出处）同一思路：
        # 让使用者一眼看出这条记录的可信度边界在哪里。
        if self.verified_by == VerifiedBy.AUTOMATED:
            # 备注中应出现说明抄录性质的字样
            if not any(hint in (self.notes or "") for hint in _DATE_BASIS_HINTS):
                # 生成警告级问题（不阻断，但必须提示）
                problems.append(
                    f"{WARNING_PREFIX} [{self.id}] verified_by=automated，但 notes 未说明"
                    f"抄录范围与核验方式 —— 请写明「字段抄自官方公示原文、未逐项人工核对」"
                )

        # 规则 4：涉及数据/信息/网络等技术类违规，却没有任何领域归类时提醒。
        # 这条不判为错误：归类是人工工作，尚未完成是正常状态；
        # 但它是一个明确的待办信号——本类记录的价值正是靠归类与关联建立起来的。
        tech_hints = ("数据", "信息", "网络", "科技", "算法", "智能", "技术")
        # 命中技术类词但未归类
        if any(h in self.violation_type for h in tech_hints) and not self.domain:
            # 提示归类
            problems.append(
                f"{WARNING_PREFIX} [{self.id}] 违规事实涉及技术类管理要求，但 domain 为空"
                f" —— 建议归类，否则无法按领域聚合出「监管实际在查什么」"
            )

        # 规则 5：法律依据若填写，必须交代出处，禁止倒推。
        # 官方公示不含处罚依据，因此这个字段一旦有值，必然是有人查了别的材料。
        # 要求写明出处，是为了防止「从违法行为类型倒推条款」这种无出处的推断
        # 混进记录——它看起来很有价值，但使用者无法分辨它是抄的还是猜的。
        #
        # 词表里**刻意没有「原文」**。实测发现：automated 记录必须写的抄录范围
        # 说明（「字段抄自官方公示原文，未逐项人工核对」）里天然含「原文」，
        # 于是这条规则对所有合规填写的记录一律不触发——规则形同虚设，
        # 而校验输出依旧是「通过」。这类「因为撞词而永不触发」的规则
        # 比没有规则更危险，它会让人以为这一项已经被守住了。
        if self.legal_basis and not any(h in (self.notes or "") for h in ("依据", "出处", "来源", "条款")):
            # 缺出处
            problems.append(
                f"{WARNING_PREFIX} [{self.id}] 填写了 legal_basis，但 notes 未说明该依据的出处"
                f" —— 官方公示表格不含处罚依据，此值必然是另查所得，请写明来源"
            )

        # 返回问题列表
        return problems


@dataclass
class Issuer:
    """发布机构。

    这个模型的存在是为了消除同一机构的多种写法。
    如果不同贡献者分别写成「国家金融监督管理总局」和「金融监管总局」，
    检索就会漏结果——这是知识库最常见的静默失效。
    """

    # 机构短代码，用作政策 id 前缀
    code: str
    # 机构官方全称，写入政策记录的 issuer 字段时须使用此值
    name: str
    # 机构官方网站域名，用于校验政策记录的 source.site
    domain: str
    # 机构官网首页地址
    homepage: str
    # 机构在监管体系中的层级，决定其发布文件的效力上限
    authority_level: str
    # 常用简称，仅用于显示与检索，不得写入政策记录
    abbr: str | None = None
    # 机构备注，例如改名历史、合并情况、网站改版记录
    notes: str | None = None


@dataclass
class Source:
    """数据源配置。

    每个数据源对应一个可抓取的官方列表页或栏目。
    把源当作一等公民单独建模，而不是把 URL 硬编码在抓取器里，
    是因为政府网站会改版、会下架、会合并——
    当某个源失效时，我们希望只改 YAML，不改代码、不发版。
    """

    # 数据源唯一标识，用于变更流与日志中定位问题源
    id: str
    # 所属机构代码，必须存在于 issuers 列表中
    issuer_code: str
    # 数据源的中文名称，说明这是哪个栏目
    name: str
    # 来源权威等级
    tier: SourceTier
    # 是否启用；置为 false 时流水线跳过并记录，而不是反复失败
    enabled: bool
    # 使用的抓取器类型
    fetcher: str
    # 列表页地址；manual 类型可为 None
    list_url: str | None = None
    # 建议抓取频率，cron 表达式，供流水线调度参考
    schedule: str = "0 6 * * *"
    # 同一源内请求之间的最小间隔秒数，对监管机构服务器的基本礼貌
    min_interval_seconds: float = 3
    # 分页配置；None 表示不分页
    pagination: dict[str, Any] | None = None
    # CSS 选择器配置，供通用抓取器使用。
    # 值可以是 None：实测配置里 ``date: null`` 是有意义的写法，
    # 表示「该列表页不显示日期」。若把值类型写成 str，
    # 这个合法用法就会被类型系统误判为非法。
    selectors: dict[str, str | None] | None = None
    # JSON 接口配置，供 json_search_list 抓取器使用。
    # 实测发现中国政府监管网站的列表页绝大多数为 JS 渲染，
    # HTML 选择器方案拿不到条目，必须走内部 JSON 接口。
    # 因此这个配置项的实际使用频率高于 selectors。
    api: dict[str, Any] | None = None
    # 详情页表格解析配置，供 penalty_table 抓取器使用。
    #
    # 为什么单开一个键而不塞进 selectors：
    # selectors 描述的是**列表页**的定位方式，而行政处罚这类源的
    # 关键信息在**详情页的表格**里——列表页只给出一个文号。
    # 两者作用在不同页面上，混在一个键里会让「改选择器时该改哪一项」
    # 变得需要读代码才能判断。本项目已有 api 与 selectors 分列的先例，
    # 这里沿用同一风格：按抓取器族划分配置键。
    penalty: dict[str, Any] | None = None
    # 关键词过滤规则
    filters: dict[str, Any] | None = None
    # 数据源备注：已知的网络可达性问题、反爬情况、结构变更历史
    notes: str | None = None


# ============================================================
# 聚合根 —— 政策记录
# ============================================================

@dataclass
class Policy:
    """一条完整的金融 AI 合规政策记录。

    字段分成四组，分组顺序反映了重要性：
    1. **身份**（id / title / issuer / doc_number）—— 这是什么文件
    2. **效力**（instrument_type / bindingness / ai_relevance）—— 有多大约束力
    3. **时效**（status / effective_from / effective_until / 版本链）—— 现在是否有效
    4. **溯源**（source / last_verified / verified_by）—— 凭什么这么说

    第 3、4 组是本项目的立身之本，也是竞品普遍缺失的部分。
    """

    # ---- 第 1 组：身份 ----

    # 全局唯一稳定标识符，格式 <issuer_code>-<年份>-<语义化短名>
    id: str
    # 政策的中文官方全称，必须与官方发布页标题完全一致
    title: str
    # 发布机构的官方全称；多部门联合发布时用顿号分隔
    issuer: str
    # 所属法域，MVP 阶段固定为 CN
    jurisdiction: str
    # 文件效力层级
    instrument_type: InstrumentType
    # 约束力性质
    bindingness: Bindingness
    # 与 AI 合规主题的相关度
    ai_relevance: AIRelevance
    # 所属领域，至少一项
    domain: list[str]
    # 官方公布日期
    published_on: date
    # 来源溯源信息
    source: SourceRef
    # 最近一次核验该记录状态的日期
    last_verified: date
    # 最近一次核验的执行方式
    verified_by: VerifiedBy
    # 版本历史，至少一条
    versions: list[Version]

    # ---- 第 2 组：效力 ----

    # 当前法律状态
    status: PolicyStatus = PolicyStatus.UNKNOWN
    # 发布机构短代码
    issuer_code: str | None = None
    # 官方发文字号，如「银发〔2023〕89号」
    doc_number: str | None = None
    # 英文标题，仅在中国官方发布英文版时填写官方译名
    title_en: str | None = None
    # 法域细分层级，用于地方性文件
    jurisdiction_detail: str | None = None
    # 主题标签，取值来自受控词表
    topics: list[str] = field(default_factory=list)
    # 适用主体清单
    applicable_to: list[str] = field(default_factory=list)
    # 关键义务条目
    key_obligations: list[KeyObligation] = field(default_factory=list)

    # ---- 第 3 组：时效 ----

    # 生效起始日期；尚未定稿的文件为 None
    effective_from: date | None = None
    # 失效日期；多数文件为 None，需靠主动核验确认状态
    effective_until: date | None = None
    # 本条取代了哪些先前文件（向后指针）
    supersedes: list[str] = field(default_factory=list)
    # 本条被哪个文件取代（向前指针）
    superseded_by: str | None = None
    # 本条修订了哪些文件
    amends: list[str] = field(default_factory=list)
    # 本条被哪些文件修订过
    amended_by: list[str] = field(default_factory=list)

    # ---- 第 4 组：其它 ----

    # 核验过程说明，status 为 unknown 或核验遇阻时必须填写
    verified_note: str | None = None
    # 正文语言，BCP 47 标签
    language: str = "zh-CN"
    # 自由标签，与受控词表 topics 分开管理
    tags: list[str] = field(default_factory=list)
    # 补充说明
    notes: str | None = None

    # --------------------------------------------------------
    # 业务规则校验 —— 这些规则是 JSON Schema 表达不了的
    # --------------------------------------------------------

    def validate_semantics(self) -> list[str]:
        """校验业务语义规则，返回问题描述列表（空列表表示通过）。

        为什么要单独做语义校验：JSON Schema 能验证「字段类型对不对」，
        但验证不了「状态与日期的组合讲不讲得通」。例如：
        - 一条已废止的文件不应该有未来的生效日期
        - 征求意见稿不应该有生效日期
        - 声称被某文件取代，就必须能在库中找到那个文件（跨记录校验）

        前两类在此实现，跨记录校验由 pipeline 统一执行。
        """
        # 收集所有问题描述
        problems: list[str] = []

        # 规则 1：征求意见稿与草稿不应有生效日期
        if self.status in (PolicyStatus.CONSULTATION, PolicyStatus.DRAFT) and self.effective_from is not None:
            # 记录违规：未定稿却填了生效日
            problems.append(
                f"[{self.id}] status={self.status.value} 表示尚未定稿，但 effective_from={self.effective_from} 已填写"
            )

        # 规则 2：生效日期不应早于公布日期（例外：追溯生效，但必须说明）
        if self.effective_from is not None and self.effective_from < self.published_on:
            # 追溯生效在实务中存在，因此只提示而非判错，要求补充说明
            if not self.notes or "追溯" not in (self.notes or ""):
                problems.append(
                    f"[{self.id}] effective_from({self.effective_from}) 早于 published_on({self.published_on})，"
                    f"若属追溯生效请在 notes 中说明"
                )

        # 规则 3：失效日期不应早于生效日期
        if (
            self.effective_from is not None
            and self.effective_until is not None
            and self.effective_until < self.effective_from
        ):
            # 这是明确的逻辑矛盾，不可能出现
            problems.append(
                f"[{self.id}] effective_until({self.effective_until}) 早于 effective_from({self.effective_from})，逻辑矛盾"
            )

        # 规则 4：status 与 effective_until 应一致
        if self.status == PolicyStatus.EXPIRED and self.effective_until is None:
            # 声称已过期，却没说什么时候过期——无法核验
            problems.append(f"[{self.id}] status=expired 但未填写 effective_until，无法核验失效时点")

        # 规则 5：status=unknown 时必须说明原因
        if self.status == PolicyStatus.UNKNOWN and not self.verified_note:
            # 这个字段的价值就在于「把未知显式化」，而不是让未知看起来像已知
            problems.append(f"[{self.id}] status=unknown 但未填写 verified_note 说明核验障碍")

        # 规则 6：被取代就必须指明取代者
        if self.status == PolicyStatus.SUPERSEDED and not self.superseded_by:
            # 没有取代者的「已被取代」是自相矛盾的表述
            problems.append(f"[{self.id}] status=superseded 但未填写 superseded_by")

        # 规则 7：版本链必须从 1 开始且连续
        if self.versions:
            # 提取所有版本号并排序
            numbers = sorted(v.version for v in self.versions)
            # 期望的版本号序列
            expected = list(range(1, len(numbers) + 1))
            # 比对实际与期望
            if numbers != expected:
                # 跳号或重复会导致版本链断裂，无法追溯
                problems.append(f"[{self.id}] 版本号不连续或重复：实际 {numbers}，期望 {expected}")

        # 规则 8：非终态、非未定稿的政策，应有生效日期。
        # 这条是**警告**而非错误。原因：现实工作中确实存在「已确认政策现行有效，
        # 但无法取得确切施行日期」的情况（官方原文未载明施行条款、
        # 或原文页面不可访问）。若判为错误，维护者会被迫编造一个日期来让校验通过——
        # 那才是对数据质量真正的伤害。因此这里要求的是「显式承认不知道」，
        # 而非「必须知道」。
        if self.status in (PolicyStatus.EFFECTIVE, PolicyStatus.PARTIALLY_EFFECTIVE) and self.effective_from is None:
            # 提示补充日期，或说明为何无法确认
            problems.append(
                f"{WARNING_PREFIX} [{self.id}] status={self.status.value}（现行有效）但未填写 effective_from"
                f" —— 请在 verified_note 中说明为何无法确认施行日期"
            )

        # 规则 9：未标明核验方式为人工时，提示该记录尚未经人工复核。
        # 这同样是警告：自动化整理的记录有价值，但使用者需要知道
        # 它与人工核验过的记录在可信度上存在差异。
        if self.verified_by == VerifiedBy.AUTOMATED and self.status.is_currently_valid:
            # 提示记录尚未人工核验
            problems.append(
                f"{WARNING_PREFIX} [{self.id}] 现行有效但尚未经人工核验（verified_by=automated）"
                f" —— 建议打开官方页面逐项核对状态字段"
            )

        # 规则 10：source.tier 必须为 primary
        if self.source.tier != SourceTier.PRIMARY:
            # 本项目只允许官方原始来源进入记录
            problems.append(f"[{self.id}] source.tier={self.source.tier.value}，本项目只允许 primary 来源")

        # 规则 11：生效日期与核验说明不得自相矛盾。
        #
        # 若 effective_from 填了值，而 verified_note 承认该日期尚未确认，
        # 这条记录就同时主张了「施行日期是 X」与「不知道是不是 X」。
        # 这比「缺日期」危险得多：缺日期是显式的空白，使用者会去问；
        # 自相矛盾却会让下游把那个未经确认的日期直接当事实使用。
        # 因此本条判为**错误**（阻断）而非警告——警告不足以阻止它入库。
        #
        # 实测背景：2026-10-05 复核 9 条记录，其中 3 条犯此错
        # （nfra-2026-ai-guidance、pbc-2021-open-source-tech、
        # cac-2023-genai-interim-measures）。规则一直写在 README 里，
        # 但此前没有任何机制执行它。
        #
        # 处置方式二选一：①回原文核验后把依据写进 verified_note
        # （此时说明中不再出现不确定表述）；②清空 effective_from 并说明原因。
        contradiction = admits_effective_date_unconfirmed(self.verified_note)
        if self.effective_from is not None and contradiction:
            # 报错并引用原句，便于作者直接定位
            problems.append(
                f"[{self.id}] effective_from={self.effective_from} 已填写，"
                f"但 verified_note 承认该日期尚未确认，自相矛盾："
                f"「{contradiction[:80]}」"
                f" —— 请二选一：核验原文后写明依据，或清空 effective_from 并说明原因"
            )

        # 规则 12：自动化整理填了生效日期时，必须交代该日期的出处。
        #
        # 为什么只对 automated 记录提这条要求：自动化整理的产出逻辑是
        # 「生成一条完整记录」，天然倾向于把字段填满；而人工核验过的记录，
        # 核验行为本身就是依据，再逐条要求说明只会制造无意义的噪声。
        #
        # 本条是警告而非错误：说明文字写得简略不应阻断入库，
        # 但使用者有权知道这个日期有没有交代来源。
        if (
            self.verified_by == VerifiedBy.AUTOMATED
            and self.effective_from is not None
            and not any(hint in (self.verified_note or "") for hint in _DATE_BASIS_HINTS)
        ):
            # 提示补充日期依据
            problems.append(
                f"{WARNING_PREFIX} [{self.id}] verified_by=automated 且已填写 "
                f"effective_from={self.effective_from}，但 verified_note 未说明该日期的出处"
                f" —— 请写明原文条款或核验过程，或清空该字段"
            )

        # 返回全部问题
        return problems

    @property
    def latest_version(self) -> Version | None:
        """返回版本号最大的版本；无版本时返回 None。

        用途：生成变更流时对比的是最新版本，而非任意版本。
        """
        # 无版本记录时直接返回 None，避免 max() 抛异常
        if not self.versions:
            return None
        # 按版本号取最大者
        return max(self.versions, key=lambda v: v.version)

    def days_since_verified(self, today: date) -> int:
        """计算距上次核验已过去多少天。

        参数 today 由调用方传入而非内部取 ``date.today()``，
        目的是让测试可以注入固定日期，避免测试结果随时间漂移。
        """
        # 用传入的基准日期减去最近核验日期
        return (today - self.last_verified).days


# ============================================================
# 字典 <-> 对象的转换工具
# ============================================================

def _parse_date(value: Any) -> date | None:
    """把 YAML 中读出的日期值统一转成 ``date`` 对象。

    为什么需要这个函数：PyYAML 会自动把 ``2026-06-18`` 解析为 ``datetime.date``，
    但把 ``2026-6-18`` 解析为 ``str``，把空值解析为 ``None``。
    如果不做归一化，下游代码就必须到处写 isinstance 判断。
    """
    # datetime 必须先行处理，因为它是 date 的子类（isinstance 判断会误命中）。
    # 场景：贡献者写 ``effective_from: 2026-07-01T09:00:00+08:00``，
    # PyYAML 会给出一个 datetime。而本项目这些字段的语义是「日期」而非
    # 「时刻」——政策在一天之内生效，不存在按小时生效的情形。
    # 因此这里取日期部分。若不处理，datetime 会被原样存进对象，
    # 之后导出 JSON 时既无法序列化，也会让日期字段混入时间部分。
    if isinstance(value, datetime):
        # 取日期部分
        return value.date()
    # 已经是 date 对象则直接返回（PyYAML 的常见行为）
    if isinstance(value, date):
        return value
    # 空值返回 None
    if value is None:
        return None
    # 字符串则按 ISO 格式解析
    if isinstance(value, str):
        # 按 YYYY-MM-DD 解析；解析失败抛出明确错误而非静默返回 None
        try:
            # 使用 fromisoformat 并要求恰好 10 位，避免接受模糊格式
            return date.fromisoformat(value.strip()[:10])
        except ValueError as exc:
            # 抛出带上下文信息的异常，便于定位是哪条数据出错
            raise ValueError(f"无法解析日期 {value!r}：{exc}") from exc
    # 其它类型明确报错，不猜测
    raise TypeError(f"不支持的日期类型 {type(value).__name__}：{value!r}")


def _parse_text(value: Any) -> str | None:
    """把 YAML 中读出的值统一转成字符串（或 None）。

    为什么需要这个函数：YAML 的隐式类型推断会「好心办坏事」。
    ``fetched_at: 2026-10-03T12:00:00+08:00`` 不加引号时，PyYAML 会
    把它解析成 ``datetime`` 对象，而字段声明的类型是 ``str``——
    于是对象里存着一个类型不符的值。

    这个问题曾经真实发生过，且很难发现：JSON Schema 校验会因为
    ``normalize_for_validation`` 提前把日期转成字符串而通过，
    但对象内部的类型仍然是错的，直到某天有人导出 JSON 才崩在
    ``TypeError: Object of type datetime is not JSON serializable``。

    处理方式与日期归一化一致：与其要求贡献者记得给时间戳加引号
    （反直觉、必然遗忘），不如在转换层统一处理。
    """
    # 空值保持 None
    if value is None:
        # 无值
        return None
    # 已经是字符串则原样返回
    if isinstance(value, str):
        # 直接返回
        return value
    # 日期与时间转成 ISO 字符串（保留时区偏移）
    if isinstance(value, datetime | date):
        # 格式化输出
        return value.isoformat()
    # 其它标量类型（如数字）转字符串，避免类型不符
    return str(value)


def _parse_date_required(value: Any, field: str) -> date:
    """解析**必填**日期字段，无法解析时抛异常而非返回 None。

    为什么需要单独一个函数：``_parse_date`` 的返回类型是 ``date | None``，
    因为 ``effective_until`` 这类字段「没有值」是完全正常的状态。
    但 ``published_on``、``last_verified`` 不是——一条政策如果连
    「什么时候发布的」「上次核验是在什么时候」都不知道，
    它在本项目里的存在意义就为零。

    早期实现让这几个必填字段共用 ``_parse_date``，于是类型上
    允许 ``None`` 漏进来。JSON Schema 虽然有 required 约束，
    但校验发生在对象构造之后；对象内部先存了一个 None，
    这个「先污染后检查」的顺序很危险。

    因此这里的选择是：**在构造对象的那一刻就失败**，
    并且报错信息里带上字段名，让人一眼知道该去改哪个 YAML 字段。
    异常由 ``store.load_all_policies`` 捕获并归入该文件的错误列表，
    不会让整批数据加载中断。
    """
    # 复用通用解析逻辑
    parsed = _parse_date(value)
    # 解析不出结果说明字段缺失或格式非法，明确报错
    if parsed is None:
        # 抛出带字段名的错误，便于定位到具体 YAML 行
        raise ValueError(f"必填日期字段 {field} 缺失或无法解析：{value!r}")
    # 返回确认非空的日期
    return parsed


def policy_from_dict(data: dict[str, Any]) -> Policy:
    """把 YAML 读出的字典转换成 ``Policy`` 对象。

    转换过程中会做类型归一化（日期、枚举、嵌套对象）。
    任何缺失的必填字段都会抛出异常，而不是静默取默认值——
    因为对合规数据而言，「字段缺失」比「程序崩溃」危险得多。
    具体而言：字段整个不存在时抛 ``KeyError``；
    字段存在但内容无法解析（如日期写成「待定」）时抛 ``ValueError``。
    两者都由 ``store.load_all_policies`` 捕获并记入该文件的错误列表。
    """
    # 版本列表：逐项转成 Version 对象
    versions = [
        Version(
            version=int(v["version"]),                          # 版本号转整数
            published_on=_parse_date_required(v["published_on"], "versions[].published_on"),  # 公布日期（必填）
            status=PolicyStatus(v["status"]),                  # 状态转枚举
            url=v["url"],                                      # 原文链接
            effective_from=_parse_date(v.get("effective_from")),   # 生效日
            effective_until=_parse_date(v.get("effective_until")), # 失效日
            archived_url=v.get("archived_url"),                # 存档链接
            content_hash=v.get("content_hash"),                # 内容哈希
            change_note=v.get("change_note"),                  # 变更说明
        )
        for v in data.get("versions", [])                          # 缺省为空列表
    ]

    # 来源溯源信息：从嵌套字典构造
    src = data["source"]                                                    # 必填字段，缺失即报错
    source_ref = SourceRef(
        url=_parse_text(src["url"]) or "",                                  # 官方链接
        site=_parse_text(src["site"]) or "",                                # 站点域名
        # 抓取时间经 _parse_text 归一化为字符串：YAML 中不带引号的
        # 2026-10-03T12:00:00+08:00 会被解析成 datetime 对象，
        # 若不归一化，对象里就会存一个类型与声明不符的值（见 _parse_text 说明）
        fetched_at=_parse_text(src["fetched_at"]) or "",                    # 抓取时间
        tier=SourceTier(src.get("tier", "primary")),                        # 来源等级，默认 primary
        http_status=src.get("http_status"),                                 # HTTP 状态码
        content_hash=_parse_text(src.get("content_hash")),                  # 内容哈希
        snapshot_path=_parse_text(src.get("snapshot_path")),                # 快照路径
    )

    # 关键义务列表：逐项转换
    obligations = [
        KeyObligation(
            clause=o["clause"],                      # 条款定位
            summary=o["summary"],                    # 义务概括
            obligation_type=o.get("obligation_type"), # 义务类型
            tags=list(o.get("tags", [])),            # 主题标签
        )
        for o in data.get("key_obligations", [])      # 缺省为空列表
    ]

    # 构造 Policy 对象，逐个字段做类型转换
    return Policy(
        # ---- 身份 ----
        id=data["id"],                                          # 唯一标识
        title=data["title"],                                    # 中文官方全称
        issuer=data["issuer"],                                  # 发布机构全称
        jurisdiction=data["jurisdiction"],                      # 法域
        instrument_type=InstrumentType(data["instrument_type"]),# 文件层级转枚举
        bindingness=Bindingness(data["bindingness"]),           # 约束力转枚举
        ai_relevance=AIRelevance(data["ai_relevance"]),         # AI 相关度转枚举
        domain=list(data.get("domain", [])),                    # 所属领域
        published_on=_parse_date_required(data["published_on"], "published_on"),  # 公布日期（必填）
        source=source_ref,                                      # 来源溯源
        last_verified=_parse_date_required(data["last_verified"], "last_verified"),  # 最近核验日期（必填）
        verified_by=VerifiedBy(data["verified_by"]),            # 核验方式
        versions=versions,                                      # 版本链
        # ---- 效力 ----
        status=PolicyStatus(data.get("status", "unknown")),     # 当前状态，缺省 unknown
        issuer_code=data.get("issuer_code"),                    # 机构短代码
        doc_number=data.get("doc_number"),                      # 发文字号
        title_en=data.get("title_en"),                          # 英文标题
        jurisdiction_detail=data.get("jurisdiction_detail"),    # 地方层级
        topics=list(data.get("topics", [])),                    # 主题标签（受控词表）
        applicable_to=list(data.get("applicable_to", [])),      # 适用主体
        key_obligations=obligations,                            # 关键义务
        # ---- 时效 ----
        effective_from=_parse_date(data.get("effective_from")), # 生效日
        effective_until=_parse_date(data.get("effective_until")),# 失效日
        supersedes=list(data.get("supersedes", [])),            # 取代了哪些
        superseded_by=data.get("superseded_by"),                # 被谁取代
        amends=list(data.get("amends", [])),                    # 修订了哪些
        amended_by=list(data.get("amended_by", [])),            # 被谁修订
        # ---- 其它 ----
        verified_note=data.get("verified_note"),                # 核验说明
        language=data.get("language", "zh-CN"),                 # 语言
        tags=list(data.get("tags", [])),                        # 自由标签
        notes=data.get("notes"),                                # 补充说明
    )


def policy_to_dict(policy: Policy) -> dict[str, Any]:
    """把 ``Policy`` 对象转回可序列化为 YAML 的字典。

    转换时把 date 与 Enum 转为字符串，因为 YAML 序列化器无法直接处理它们。
    字段顺序与 policy.schema.json 的 required 列表保持一致，
    这样生成的 YAML 文件在 diff 时更易读。
    """
    # 内部小工具：把 date 或 None 转成 ISO 字符串或 None
    def _iso(value: date | None) -> str | None:
        """日期转 ISO 格式字符串；None 原样返回。"""
        # 空值直接返回
        if value is None:
            return None
        # 否则格式化为 YYYY-MM-DD
        return value.isoformat()

    # 构造结果字典，键顺序即为 YAML 输出顺序
    result: dict[str, Any] = {
        # ---- 身份 ----
        "id": policy.id,                                                       # 唯一标识
        "title": policy.title,                                                 # 中文标题
        "title_en": policy.title_en,                                           # 英文标题
        "issuer": policy.issuer,                                               # 发布机构
        "issuer_code": policy.issuer_code,                                     # 机构代码
        "doc_number": policy.doc_number,                                        # 发文字号
        # ---- 法域 ----
        "jurisdiction": policy.jurisdiction,                                    # 法域
        "jurisdiction_detail": policy.jurisdiction_detail,                      # 地方层级
        # ---- 效力 ----
        "instrument_type": policy.instrument_type.value,                        # 文件层级
        "bindingness": policy.bindingness.value,                                # 约束力
        "ai_relevance": policy.ai_relevance.value,                              # AI 相关度
        "domain": policy.domain,                                                # 所属领域
        "topics": policy.topics,                                                # 主题标签
        # ---- 时效 ----
        "status": policy.status.value,                                          # 当前状态
        "published_on": _iso(policy.published_on),                              # 公布日期
        "effective_from": _iso(policy.effective_from),                          # 生效日期
        "effective_until": _iso(policy.effective_until),                        # 失效日期
        "supersedes": policy.supersedes,                                        # 取代了哪些
        "superseded_by": policy.superseded_by,                                  # 被谁取代
        "amends": policy.amends,                                                # 修订了哪些
        "amended_by": policy.amended_by,                                        # 被谁修订
        # ---- 版本链 ----
        "versions": [
            {
                "version": v.version,                                           # 版本号
                "published_on": _iso(v.published_on),                           # 该版公布日
                "effective_from": _iso(v.effective_from),                       # 该版生效日
                "effective_until": _iso(v.effective_until),                     # 该版失效日
                "status": v.status.value,                                       # 该版状态
                "url": v.url,                                                   # 该版原文链接
                "archived_url": v.archived_url,                                 # 存档链接
                "content_hash": v.content_hash,                                 # 内容哈希
                "change_note": v.change_note,                                   # 变更说明
            }
            for v in policy.versions                                                 # 遍历所有版本
        ],
        # ---- 溯源 ----
        "source": {
            "url": policy.source.url,                                           # 官方链接
            "site": policy.source.site,                                         # 站点域名
            "tier": policy.source.tier.value,                                   # 来源等级
            "fetched_at": policy.source.fetched_at,                             # 抓取时间
            "http_status": policy.source.http_status,                           # HTTP 状态
            "content_hash": policy.source.content_hash,                         # 内容哈希
            "snapshot_path": policy.source.snapshot_path,                       # 快照路径
        },
        "last_verified": _iso(policy.last_verified),                             # 最近核验日期
        "verified_by": policy.verified_by.value,                                # 核验方式
        "verified_note": policy.verified_note,                                  # 核验说明
        # ---- 应用 ----
        "applicable_to": policy.applicable_to,                                  # 适用主体
        "key_obligations": [
            {
                "clause": o.clause,                                             # 条款定位
                "summary": o.summary,                                           # 义务概括
                "obligation_type": o.obligation_type,                           # 义务类型
                "tags": o.tags,                                                 # 主题标签
            }
            for o in policy.key_obligations                                          # 遍历所有义务
        ],
        # ---- 其它 ----
        "language": policy.language,                                            # 语言
        "tags": policy.tags,                                                    # 自由标签
        "notes": policy.notes,                                                  # 补充说明
    }

    # 返回构造好的字典
    return result


def issuer_from_dict(data: dict[str, Any]) -> Issuer:
    """把字典转成 ``Issuer`` 对象。"""
    # 逐字段映射，可选字段用 get 提供默认值
    return Issuer(
        code=data["code"],                                  # 机构短代码
        name=data["name"],                                  # 官方全称
        domain=data["domain"],                              # 官网域名
        homepage=data["homepage"],                          # 官网首页
        authority_level=data["authority_level"],            # 机构层级
        abbr=data.get("abbr"),                              # 常用简称
        notes=data.get("notes"),                            # 备注
    )


def source_from_dict(data: dict[str, Any]) -> Source:
    """把字典转成 ``Source`` 对象。"""
    # 逐字段映射，可选字段用 get 提供默认值
    return Source(
        id=data["id"],                                                  # 源标识
        issuer_code=data["issuer_code"],                                # 关联机构
        name=data["name"],                                              # 源名称
        tier=SourceTier(data.get("tier", "primary")),                   # 来源等级
        enabled=bool(data.get("enabled", False)),                       # 是否启用
        fetcher=data["fetcher"],                                        # 抓取器类型
        list_url=data.get("list_url"),                                  # 列表页地址
        schedule=data.get("schedule", "0 6 * * *"),                     # 抓取频率
        min_interval_seconds=float(data.get("min_interval_seconds", 3)),# 请求间隔
        pagination=data.get("pagination"),                              # 分页配置
        selectors=data.get("selectors"),                                # 选择器配置
        api=data.get("api"),                                            # JSON 接口配置
        penalty=data.get("penalty"),                                    # 详情页表格解析配置（罚单专用）
        filters=data.get("filters"),                                    # 过滤规则
        notes=data.get("notes"),                                        # 备注
    )


def enforcement_from_dict(data: dict[str, Any]) -> EnforcementRecord:
    """把字典转成 ``EnforcementRecord`` 对象。

    与 ``policy_from_dict`` 保持同一套约定：字段整体缺失时抛 ``KeyError``，
    字段存在但取值无法解释时抛 ``ValueError``，
    两者都由 ``store.load_all_enforcement`` 捕获并归入该文件的错误列表，
    不会让整批数据加载中断。

    ``violation_type`` / ``penalty_content`` 等「逐字抄录」字段刻意保持
    与 ``policy_from_dict`` 相同的处理方式（走 ``_parse_text`` 归一化），
    而不是在这里做更强的类型检查——因为**「处罚内容必须是字符串」这条约束
    已经在 schema 层拦下了**（``enforcement.schema.json`` 里这些字段
    声明为 ``"type": "string"``），而 ``load_enforcement_file`` 会先跑 schema。
    在模型层重复一遍只会造成两处约束各自演化。
    """
    # 来源溯源：schema 把它定为必填，因此这里用下标取值，缺失即报错
    src = data["source"]
    # 构造来源对象
    source_ref = SourceRef(
        url=_parse_text(src["url"]) or "",              # 公示文书地址（必须是该文书，不是机构首页）
        site=_parse_text(src["site"]) or "",            # 站点域名
        # fetched_at 同样要过 _parse_text 归一化：YAML 里不带引号的
        # ISO 时间戳会被 PyYAML 解析成 datetime 对象
        fetched_at=_parse_text(src["fetched_at"]) or "",# 抓取时间
        tier=SourceTier(src.get("tier", "primary")),    # 来源等级，默认 primary
        http_status=src.get("http_status"),             # HTTP 状态码
    )

    # 构造记录对象，逐字段转换
    return EnforcementRecord(
        # ---- 身份 ----
        id=data["id"],                                                  # 唯一标识
        decision_no=_parse_text(data["decision_no"]) or "",              # 决定书文号
        party=_parse_text(data["party"]) or "",                          # 当事人
        # ---- 违规事实与处罚 ----
        violation_type=_parse_text(data["violation_type"]) or "",        # 违法行为类型（原文）
        penalty_content=_parse_text(data["penalty_content"]) or "",      # 处罚内容（原文）
        authority=_parse_text(data["authority"]) or "",                  # 决定机关
        legal_basis=_parse_text(data.get("legal_basis")),                # 处罚依据，默认空
        # ---- 时间 ----
        decision_date=_parse_text(data.get("decision_date")),            # 决定日期（保留中文写法）
        published_on=_parse_date(data.get("published_on")),              # 公示日期
        publicity_period=_parse_text(data.get("publicity_period")),      # 公示期限
        # ---- 归类（人工） ----
        party_type=data.get("party_type"),                               # 当事人类型
        domain=list(data.get("domain", [])),                             # 涉及领域
        related_policies=list(data.get("related_policies", [])),         # 关联政策 id
        # ---- 溯源 ----
        jurisdiction=data.get("jurisdiction", "CN"),                     # 法域
        source=source_ref,                                               # 来源
        last_verified=_parse_date(data.get("last_verified")),            # 最近核验日期
        verified_by=VerifiedBy(data["verified_by"]),                     # 核验方式（必填）
        notes=_parse_text(data.get("notes")),                            # 备注
    )


def enforcement_to_dict(record: EnforcementRecord) -> dict[str, Any]:
    """把 ``EnforcementRecord`` 转回可序列化为 YAML 的字典。

    字段顺序与 ``enforcement.schema.json`` 的 required 列表一致，
    这样生成的 YAML 在 diff 时更易读。date 与 Enum 需转成字符串，
    因为 YAML 序列化器无法直接处理它们。
    """
    # 构造结果字典，键顺序即为 YAML 输出顺序
    return {
        # ---- 身份 ----
        "id": record.id,                                        # 唯一标识
        "decision_no": record.decision_no,                      # 决定书文号
        "party": record.party,                                  # 当事人
        "party_type": record.party_type,                        # 当事人类型
        # ---- 违规事实与处罚（逐字抄录） ----
        "violation_type": record.violation_type,                # 违法行为类型
        "penalty_content": record.penalty_content,              # 处罚内容
        "authority": record.authority,                          # 决定机关
        "legal_basis": record.legal_basis,                      # 处罚依据（通常为空）
        # ---- 时间 ----
        "decision_date": record.decision_date,                  # 决定日期（中文原文）
        # date 对象需转 ISO 字符串；None 原样保留
        "published_on": record.published_on.isoformat() if record.published_on else None,
        "publicity_period": record.publicity_period,            # 公示期限
        # ---- 归类 ----
        "domain": list(record.domain),                          # 涉及领域
        "related_policies": list(record.related_policies),      # 关联政策 id
        # ---- 溯源 ----
        "jurisdiction": record.jurisdiction,                    # 法域
        "source": {
            "url": record.source.url if record.source else "",           # 公示文书地址
            "site": record.source.site if record.source else "",         # 站点域名
            "fetched_at": record.source.fetched_at if record.source else "",  # 抓取时间
            # 枚举转字符串；无来源时回落到 primary（schema 只允许这个默认值）
            "tier": (record.source.tier.value if record.source else "primary"),
            "http_status": record.source.http_status if record.source else None,  # HTTP 状态码
        },
        "last_verified": record.last_verified.isoformat() if record.last_verified else None,  # 核验日期
        "verified_by": record.verified_by.value,               # 核验方式
        "notes": record.notes,                                 # 备注
    }
