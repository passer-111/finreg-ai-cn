"""数据存取层 —— 加载、保存、校验政策记录与数据源登记表。

本模块是数据进入仓库的唯一闸门。设计上做了**三重校验**：

1. **JSON Schema 校验**（``validate_against_schema``）
   保证 YAML 文件的形状正确：字段类型、枚举取值、日期格式、正则约束。

2. **数据模型语义校验**（``Policy.validate_semantics``）
   保证业务逻辑讲得通：如征求意见稿不应有生效日期。

3. **跨记录一致性校验**（``validate_cross_references``）
   保证版本链闭合：声称被 X 取代，X 就必须真的存在于库中。

三重都通过的数据才允许写入仓库。任一层失守都会让知识库静默退化——
用户看到一个「已被取代」的标记，却找不到取代者，就不会再信任这个库了。
"""

# 导入 hashlib 用于计算内容摘要
import hashlib
# 导入 json 用于读取 JSON Schema 文件
import json
# 导入 date 与 datetime 用于识别需要归一化的时间类型
from datetime import date, datetime
# 导入 Path 用于跨平台路径操作
from pathlib import Path
# 导入 Any 用于类型标注
from typing import Any

# 导入 yaml 用于读写 YAML
import yaml
# 导入 jsonschema 用于结构校验
from jsonschema import Draft202012Validator
# 说明：校验错误由 validator.iter_errors() 逐个产出，其类型无需在代码中引用，
# 因此不必导入 ValidationError——导入未使用的符号会让读者误以为它被用到。

# 从本包导入数据模型与转换函数
from finreg_ai.models import (
    Issuer,
    Policy,
    Source,
    issuer_from_dict,
    policy_from_dict,
    policy_to_dict,
    source_from_dict,
    with_location,
)

# 项目根目录：本文件位于 src/finreg_ai/store.py，因此上溯三级即项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 政策数据目录
POLICIES_DIR = PROJECT_ROOT / "data" / "policies"
# 变更流目录
CHANGES_DIR = PROJECT_ROOT / "data" / "changes"
# 原始快照目录（按源与日期分层存放）
SNAPSHOTS_DIR = PROJECT_ROOT / "data" / "snapshots"
# Schema 目录
SCHEMA_DIR = PROJECT_ROOT / "schema"
# 数据源登记表路径
SOURCES_FILE = PROJECT_ROOT / "data" / "sources.yaml"


# ============================================================
# 摘要计算 —— 变更检测的基础设施
# ============================================================

def compute_hash(text: str) -> str:
    """计算文本的 SHA-256 摘要，返回 ``sha256:<64位十六进制>`` 格式。

    为什么要带 ``sha256:`` 前缀：将来若要更换摘要算法（如迁移到 BLAKE3），
    带前缀的格式可以在同一份数据里共存新旧算法，便于平滑迁移。
    不带前缀的裸摘要在换算法时就无法区分了。
    """
    # 统一按 UTF-8 编码，保证跨平台结果一致（Windows 默认编码会算出不同哈希）
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    # 拼接算法前缀后返回
    return f"sha256:{digest}"


def normalize_text(text: str) -> str:
    """归一化文本以便稳定比对。

    为什么需要归一化：政府网站页面常含动态元素（访问计数、时间戳、
    无关的导航区块）。若直接对整页 HTML 做哈希，改一个计数器就会
    触发一次「政策变更」误报。归一化把这类噪声压掉，
    使哈希只在实质内容变化时才改变。
    """
    # 按行切分，便于逐行处理
    lines = text.splitlines()
    # 去掉每行首尾空白
    stripped = [line.strip() for line in lines]
    # 丢弃空行——空行数量变化不构成实质变更
    non_empty = [line for line in stripped if line]
    # 用单个换行重新拼接，消除原始排版差异
    return "\n".join(non_empty)


# ============================================================
# JSON Schema 校验
# ============================================================

# 模块级缓存：Schema 文件只需读取一次，避免批量校验时反复读盘
_POLICY_SCHEMA_CACHE: dict[str, Any] | None = None
# 数据源 Schema 的缓存
_SOURCE_SCHEMA_CACHE: dict[str, Any] | None = None


def load_policy_schema() -> dict[str, Any]:
    """加载并缓存政策记录 JSON Schema。"""
    # 声明要修改模块级变量
    global _POLICY_SCHEMA_CACHE
    # 首次调用时读盘
    if _POLICY_SCHEMA_CACHE is None:
        # 以 UTF-8 读取，避免中文描述乱码
        with open(SCHEMA_DIR / "policy.schema.json", encoding="utf-8") as fh:
            # 解析 JSON 并存入缓存
            _POLICY_SCHEMA_CACHE = json.load(fh)
    # 返回缓存的 schema
    return _POLICY_SCHEMA_CACHE


def load_source_schema() -> dict[str, Any]:
    """加载并缓存数据源登记表 JSON Schema。"""
    # 声明要修改模块级变量
    global _SOURCE_SCHEMA_CACHE
    # 首次调用时读盘
    if _SOURCE_SCHEMA_CACHE is None:
        # 以 UTF-8 读取
        with open(SCHEMA_DIR / "source.schema.json", encoding="utf-8") as fh:
            # 解析 JSON 并存入缓存
            _SOURCE_SCHEMA_CACHE = json.load(fh)
    # 返回缓存的 schema
    return _SOURCE_SCHEMA_CACHE


def normalize_for_validation(value: Any) -> Any:
    """递归地把 ``date`` / ``datetime`` 对象转成 ISO 字符串。

    为什么需要这一步
    ----------------
    PyYAML 遵循 YAML 1.1 的隐式类型解析规则：未加引号的 ``2026-06-18``
    会被解析为 ``datetime.date`` 对象，而 ``2026-10-03T12:00:00+08:00``
    会被解析为 ``datetime.datetime`` 对象。

    这会与 JSON Schema 的 ``"type": "string"`` 冲突，导致所有日期字段校验失败。

    有两种修法：
    1. 要求贡献者给所有日期加引号（``published_on: "2026-06-18"``）
    2. 在校验前做一次归一化

    本项目选 2。理由：给日期加引号是反直觉的写法，贡献者必然经常忘记，
    而每次忘记都会得到一条难以理解的报错（「datetime.date 不是 string 类型」）。
    与其让每个新贡献者都踩一次这个坑，不如在代码里统一处理，
    让「加引号」和「不加引号」两种写法都能正常工作。

    这个函数是递归的，因为日期可能出现在任意嵌套层级
    （如 ``versions[].published_on`` 与 ``source.fetched_at``）。
    """
    # 日期对象：转成 YYYY-MM-DD 字符串
    if isinstance(value, datetime):
        # 注意先判断 datetime 再判断 date——datetime 是 date 的子类，
        # 若顺序颠倒，带时间的字段会被误格式化为纯日期，丢失时间信息
        return value.isoformat()
    # 纯日期对象：转成 YYYY-MM-DD 字符串
    if isinstance(value, date):
        # 格式化输出
        return value.isoformat()
    # 字典：递归处理每个值
    if isinstance(value, dict):
        # 保持键顺序，逐项归一化
        return {key: normalize_for_validation(val) for key, val in value.items()}
    # 列表：递归处理每个元素
    if isinstance(value, list):
        # 逐项归一化
        return [normalize_for_validation(item) for item in value]
    # 其它类型原样返回（str / int / float / bool / None）
    return value


def validate_against_schema(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """用 JSON Schema 校验字典，返回易读的错误信息列表。

    校验前会自动做日期归一化，因此调用方无需关心 YAML 中
    日期是否加了引号。

    返回字符串列表而非抛异常，是为了让一次校验能报出全部问题，
    而不是修一个跑一次。批量校验数据文件时这一点很重要。
    """
    # 归一化日期类型，避免 PyYAML 的隐式类型解析与 schema 冲突
    normalized = normalize_for_validation(data)
    # 构造校验器；Draft202012 是当前标准
    validator = Draft202012Validator(schema)
    # 收集错误信息
    errors: list[str] = []
    # 按路径排序，让同一字段的错误聚在一起，便于阅读
    for error in sorted(validator.iter_errors(normalized), key=lambda e: list(e.absolute_path)):
        # 把路径列表拼成 a.b.c 形式，便于定位
        location = ".".join(str(p) for p in error.absolute_path) or "<根>"
        # 格式化单条错误：位置 + 说明
        errors.append(f"{location}: {error.message}")
    # 返回全部错误
    return errors


# ============================================================
# 政策记录读写
# ============================================================

def load_policy_file(path: Path) -> tuple[Policy | None, list[str]]:
    """加载单个政策 YAML 文件。

    返回 ``(policy, errors)`` 二元组：校验失败时 policy 为 None，
    errors 含全部问题描述。调用方据此决定是否阻断流程。
    """
    # 准备错误收集列表
    errors: list[str] = []
    # 以 UTF-8 读取 YAML，显式指定 encoding 避免 Windows 中文乱码
    with open(path, encoding="utf-8") as fh:
        # safe_load 只允许基础类型，防止 YAML 标签注入执行任意代码
        raw = yaml.safe_load(fh)

    # 空文件或只含注释的文件，视为错误而非跳过——静默跳过会让问题消失但没被修复
    if raw is None:
        # 返回明确的错误信息
        return None, [f"{path.name}: 文件为空或不含有效 YAML 内容"]

    # 顶层必须是映射，否则后续校验无意义
    if not isinstance(raw, dict):
        # 类型错误
        return None, [f"{path.name}: 顶层结构必须是映射（字典），实际为 {type(raw).__name__}"]

    # 第一层：JSON Schema 结构校验
    schema_errors = validate_against_schema(raw, load_policy_schema())
    # 任一结构错误都直接返回，因为后续对象构造可能因此失败
    if schema_errors:
        # 用 with_location 附加文件名——它会把警告标记保持在最前，
        # 保证严重级别分流逻辑不被破坏
        return None, [with_location(msg, path.name) for msg in schema_errors]

    # 第二层：构造数据模型对象（此步会把日期与枚举做类型归一化）
    try:
        # 调用转换函数
        policy = policy_from_dict(raw)
    except (KeyError, TypeError, ValueError) as exc:
        # 转换失败说明数据虽然形状对，但取值无法解释
        return None, [f"{path.name}: 数据模型转换失败 —— {exc}"]

    # 第三层：业务语义校验
    semantic_errors = policy.validate_semantics()
    # 用 with_location 附加文件名，保持警告标记在最前
    errors.extend(with_location(msg, path.name) for msg in semantic_errors)

    # 有语义问题时仍返回对象，让调用方可以决定是硬失败还是告警
    return policy, errors


def load_all_policies(directory: Path | None = None) -> tuple[dict[str, Policy], list[str]]:
    """加载目录下全部政策 YAML，返回 ``(id到policy的映射, 错误列表)``。

    以 id 为键而非文件名，是因为下游引用（supersedes/superseded_by）
    用的都是 id。这样即便文件被改名，引用关系依然成立。
    """
    # 未指定目录时使用默认政策目录
    target = directory or POLICIES_DIR
    # 结果映射：id -> Policy
    policies: dict[str, Policy] = {}
    # 错误收集
    errors: list[str] = []

    # 目录不存在时给出明确提示，而非返回空集合让人困惑
    if not target.exists():
        # 返回空结果与提示
        return {}, [f"政策目录不存在：{target}"]

    # 遍历目录下所有 YAML 文件，排序保证输出稳定（便于测试与 diff）
    for path in sorted(target.glob("*.yaml")):
        # 加载单个文件
        policy, file_errors = load_policy_file(path)
        # 收集错误
        errors.extend(file_errors)
        # 加载成功时加入映射
        if policy is not None:
            # 检测 id 重复——重复 id 会让引用产生歧义，必须拦截
            if policy.id in policies:
                # 记录冲突信息
                errors.append(f"{path.name}: id 重复，与 {policies[policy.id].id} 冲突")
            else:
                # 正常登记
                policies[policy.id] = policy

    # 返回结果
    return policies, errors


def save_policy(policy: Policy, directory: Path | None = None) -> Path:
    """把政策对象写入 YAML 文件，返回写入路径。

    文件名为 ``<id>.yaml``，与 id 一一对应，
    这样从文件名就能直接推断出 id，无需打开文件。
    """
    # 未指定目录时使用默认政策目录
    target = directory or POLICIES_DIR
    # 确保目录存在
    target.mkdir(parents=True, exist_ok=True)
    # 计算目标路径
    path = target / f"{policy.id}.yaml"
    # 转回字典
    data = policy_to_dict(policy)
    # 写入文件；allow_unicode=True 保证中文不被转义为 \uXXXX，便于人工阅读 diff
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        # 保留键顺序（sort_keys=False），让 YAML 的字段顺序与设计一致
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False, default_flow_style=False, width=120)
    # 返回写入路径
    return path


# ============================================================
# 跨记录一致性校验 —— 版本链闭合性
# ============================================================

def validate_cross_references(policies: dict[str, Policy]) -> list[str]:
    """校验所有跨记录引用是否闭合，返回问题列表。

    这是最容易被忽视、也最致命的一类校验。举例：
    某条记录标注 ``status: superseded`` 且 ``superseded_by: xyz-2026-new-rule``，
    但如果 ``xyz-2026-new-rule`` 并不在库中（可能漏抓、可能 id 写错），
    用户就会看到一个指向虚空的「已被取代」标记。

    检查四类引用：
    - ``superseded_by`` 指向的记录必须存在
    - ``supersedes`` 列表中的每个 id 必须存在
    - ``amended_by`` 列表中的每个 id 必须存在
    - ``amends`` 列表中的每个 id 必须存在

    另外检查双向一致性：A 说「我取代了 B」，B 就应该说「我被 A 取代」。
    这是最容易被人工编辑破坏的地方，必须机械化校验。
    """
    # 收集问题
    problems: list[str] = []
    # 已知的 id 集合，用于存在性判断
    known_ids = set(policies.keys())

    # 逐条记录检查
    for pid, policy in policies.items():
        # --- 检查 superseded_by 存在性 ---
        if policy.superseded_by and policy.superseded_by not in known_ids:
            # 指向了不存在的记录
            problems.append(f"[{pid}] superseded_by 指向不存在的记录：{policy.superseded_by}")

        # --- 检查 supersedes 列表存在性 ---
        for target in policy.supersedes:
            # 目标不存在
            if target not in known_ids:
                problems.append(f"[{pid}] supersedes 指向不存在的记录：{target}")

        # --- 检查 amended_by 列表存在性 ---
        for target in policy.amended_by:
            # 目标不存在
            if target not in known_ids:
                problems.append(f"[{pid}] amended_by 指向不存在的记录：{target}")

        # --- 检查 amends 列表存在性 ---
        for target in policy.amends:
            # 目标不存在
            if target not in known_ids:
                problems.append(f"[{pid}] amends 指向不存在的记录：{target}")

        # --- 检查取代关系的双向一致性 ---
        if policy.superseded_by:
            # 取出声称取代本条的那个记录
            successor = policies.get(policy.superseded_by)
            # 若存在但反向未声明，说明版本链有一侧没维护
            if successor is not None and pid not in successor.supersedes:
                # 提示补全反向指针
                problems.append(
                    f"[{pid}] 被 {policy.superseded_by} 取代，但后者未在 supersedes 中声明 {pid}（版本链双向不一致）"
                )

        # --- 检查本条是否误取代了自己 ---
        if pid in policy.supersedes:
            # 自我引用是明显的编辑错误
            problems.append(f"[{pid}] supersedes 中不应包含自身")

        # --- 检查 superseded_by 是否误指向自己 ---
        if policy.superseded_by == pid:
            # 同上
            problems.append(f"[{pid}] superseded_by 不应指向自身")

    # 返回全部问题
    return problems


def find_stale_policies(policies: dict[str, Policy], today: Any, threshold_days: int = 90) -> list[tuple[str, int]]:
    """找出超过阈值天数未核验的政策，返回 ``[(id, 天数)]``。

    这是「时效性治理」从口号变成机制的关键一步。
    竞品 delschlangen/ai-legislation-tracker 的设计本身是对的
    （它有 last_verified 字段），但因为没有配套的告警机制，
    数据静默陈旧了 9 个月也没人发现。

    参数 today 由调用方传入而非内部取当天，
    目的是让测试可以注入固定日期，避免结果随运行日期漂移。
    """
    # 结果列表
    stale: list[tuple[str, int]] = []
    # 逐条计算距上次核验的天数
    for pid, policy in policies.items():
        # 计算天数差
        days = policy.days_since_verified(today)
        # 超过阈值则记为陈旧
        if days > threshold_days:
            # 追加结果
            stale.append((pid, days))
    # 按陈旧程度降序排列，最陈旧的排在最前，便于优先处理
    return sorted(stale, key=lambda item: item[1], reverse=True)


# ============================================================
# 数据源登记表读写
# ============================================================

def load_sources(path: Path | None = None) -> tuple[dict[str, Issuer], dict[str, Source], list[str]]:
    """加载数据源登记表，返回 ``(机构映射, 数据源映射, 错误列表)``。"""
    # 未指定路径时使用默认位置
    target = path or SOURCES_FILE
    # 错误收集
    errors: list[str] = []

    # 文件不存在时明确报错
    if not target.exists():
        # 返回空结果与提示
        return {}, {}, [f"数据源登记表不存在：{target}"]

    # 读取 YAML
    with open(target, encoding="utf-8") as fh:
        # safe_load 防止 YAML 标签注入
        raw = yaml.safe_load(fh)

    # 空文件检查
    if raw is None:
        # 明确报错而非静默返回空
        return {}, {}, [f"{target.name}: 文件为空"]

    # JSON Schema 结构校验
    schema_errors = validate_against_schema(raw, load_source_schema())
    # 有结构错误时终止，因为后续构造可能失败
    if schema_errors:
        # 加上文件名前缀后返回
        return {}, {}, [f"{target.name}: {msg}" for msg in schema_errors]

    # 构造机构映射
    issuers: dict[str, Issuer] = {}
    # 遍历机构定义
    for item in raw.get("issuers", []):
        # 转为对象
        issuer = issuer_from_dict(item)
        # 检查 code 重复
        if issuer.code in issuers:
            # 重复机构代码会让 issuer_code 引用产生歧义
            errors.append(f"{target.name}: 机构代码重复：{issuer.code}")
            # 跳过该条
            continue
        # 登记
        issuers[issuer.code] = issuer

    # 构造数据源映射
    sources: dict[str, Source] = {}
    # 遍历数据源定义
    for item in raw.get("sources", []):
        # 转为对象
        source = source_from_dict(item)
        # 检查 id 重复
        if source.id in sources:
            # 重复源 id 会让变更流无法定位问题源
            errors.append(f"{target.name}: 数据源 id 重复：{source.id}")
            # 跳过
            continue
        # 校验 issuer_code 是否已登记——未登记的机构代码说明是拼写错误
        if source.issuer_code not in issuers:
            # 记录问题
            errors.append(f"{target.name}: 数据源 {source.id} 引用了未登记的机构代码：{source.issuer_code}")
        # 校验启用中的源必须提供列表页地址（manual 类型除外）
        if source.enabled and source.fetcher != "manual" and not source.list_url:
            # 启用了却没有地址，流水线会空跑
            errors.append(f"{target.name}: 数据源 {source.id} 已启用但未提供 list_url")
        # 登记
        sources[source.id] = source

    # 返回结果
    return issuers, sources, errors
