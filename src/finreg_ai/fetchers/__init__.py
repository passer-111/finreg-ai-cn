"""抓取器包 —— 提供按配置构造抓取器的工厂函数。

对外只暴露两个东西：

- ``build_fetcher(source)``：根据数据源配置构造对应的抓取器实例
- ``BaseFetcher`` / ``FetchResult`` / ``RawDoc``：供调用方做类型标注

把「选哪个抓取器」的逻辑集中在这里，好处是新增数据源时只需在
``_FETCHER_REGISTRY`` 中注册一行，不必修改流水线代码。
"""

# 导入数据源模型用于类型标注
from finreg_ai.models import Source

# 导入基类与公共数据结构，一并对外导出
from finreg_ai.fetchers.base import (
    DROP_REASON_EXCLUDED,           # 丢弃原因：命中排除词
    DROP_REASON_NOT_MATCHED,        # 丢弃原因：未命中包含词
    BaseFetcher,                    # 抓取器基类
    FetchResult,                    # 抓取结果
    RawDoc,                         # 原始条目
    apply_filters,                  # 关键词过滤（只返回保留结果）
    filter_docs_with_reasons,       # 关键词过滤（附带丢弃原因计数）
    now_china_iso,                  # 当前北京时间 ISO 字符串
    today_china,                    # 当前北京日期
)
# 导入各专用抓取器
from finreg_ai.fetchers.cac import CacFetcher        # 网信办
from finreg_ai.fetchers.csrc import CsrcFetcher      # 证监会
from finreg_ai.fetchers.html_list import HtmlListFetcher  # 通用 HTML 列表抓取器
from finreg_ai.fetchers.json_search_list import JsonSearchListFetcher  # 通用 JSON 接口抓取器（主力）
from finreg_ai.fetchers.pbc import PbcFetcher        # 人民银行
from finreg_ai.fetchers.penalty_table import PenaltyTableFetcher  # 行政处罚（详情页表格逐行还原）

# 抓取器注册表：把 data/sources.yaml 中的 fetcher 字段映射到实现类。
# 新增数据源时在此加一行即可，流水线代码无需改动。
#
# 顺序说明（按推荐优先级）：
#   1. json_search_list —— 配置驱动，覆盖政府 CMS 接口族，首选
#   2. html_list        —— 配置驱动，用于仍采用服务端渲染的站点
#   3. 专用抓取器        —— 仅用于结构确实特殊的单站点
#
# 值得注意的一点：金融监管总局曾经是「专用抓取器」的代表（因为当时认为
# 它的接口有反爬保护、通用方案搞不定）。后来发现那只是接口地址写错了，
# 用对地址后它就是一个标准 JSON 接口，因此专用抓取器 nfra.py 已被删除，
# 该源改由 json_search_list 驱动。这个案例说明：**专用抓取器应当是
# 最后的选择**——大多数「这个站很特殊」的结论，追溯下去往往是
# 「我们还没找对接口」。
_FETCHER_REGISTRY: dict[str, type[BaseFetcher]] = {
    "json_search_list": JsonSearchListFetcher,  # 通用 JSON 接口（主力方案）
    "html_list": HtmlListFetcher,               # 通用 HTML 列表
    "pbc": PbcFetcher,                          # 人民银行专用（table 布局 + GB 编码）
    "cac": CacFetcher,                          # 网信办专用
    "csrc": CsrcFetcher,                        # 证监会专用（在其上增加了站点自检）
    "penalty_table": PenaltyTableFetcher,       # 行政处罚专用（列表页取文号，详情页表格逐行还原）
}


def build_fetcher(source: Source) -> BaseFetcher:
    """根据数据源配置构造抓取器实例。

    未知的 fetcher 类型会退回 json_search_list 或 html_list，而不是抛异常。
    这样做的理由：如果有人在 YAML 中写错了 fetcher 名称，
    使用通用抓取器至少还能尝试工作；若直接抛异常，
    整个流水线会因为一个配置拼写错误而中断——
    这与「单源失效不拖垮整体」的原则相冲突。
    """
    # 从注册表查找实现类
    fetcher_cls = _FETCHER_REGISTRY.get(source.fetcher)
    # 未命中时按配置内容推测合适的通用抓取器：
    # 配了 api 节说明该源需要走 JSON 接口，否则走 HTML 解析
    if fetcher_cls is None:
        # 有 api 配置就用 JSON 抓取器
        fetcher_cls = JsonSearchListFetcher if source.api else HtmlListFetcher
    # 构造并返回实例
    return fetcher_cls(source)


# 显式列出对外公开的符号
__all__ = [
    "DROP_REASON_EXCLUDED",       # 丢弃原因常量：命中排除词
    "DROP_REASON_NOT_MATCHED",    # 丢弃原因常量：未命中包含词
    "BaseFetcher",        # 抓取器基类
    "FetchResult",        # 抓取结果
    "PenaltyTableFetcher",  # 行政处罚抓取器
    "RawDoc",             # 原始条目
    "apply_filters",      # 关键词过滤（只返回保留结果）
    "build_fetcher",      # 工厂函数
    "filter_docs_with_reasons",  # 关键词过滤（附带丢弃原因计数）
    "now_china_iso",      # 当前北京时间 ISO 字符串
    "today_china",        # 当前北京日期
]
