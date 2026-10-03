"""中国证监会（CSRC）数据源 —— JSON 接口抓取 + 站点自检。

为什么最终走了 JSON 接口方案
---------------------------
初版实现用的是 HTML CSS 选择器，实测失败。原因（2026-10-03 实测）：

- ``common_list.shtml`` 页面**只有约 10 KB**，其中没有任何列表项。
  真正的数据由 ``/searchList/{channelId}`` 接口返回，共 234 条规章。
- 更糟的是，用宽泛选择器（如 ``li``）在空壳页面上仍能选到一些元素，
  结果抓到了页脚备案号、统计数据链接、导航栏目当成了政策条目，
  而流水线状态却是 ``ok``。这是本项目遇到的最典型的「静默污染」。

因此改为走接口，并保留一项 HTML 方案做不到的能力：**站点自检**。

站点自检的价值
--------------
csrc.gov.cn 的页面 ``<head>`` 中有标准化的 meta 标签
（SiteName / ColumnName / SiteDomain），这是政府网站普查要求带来的副产品。
它们提供了一个与被抓页面结构解耦的信号，用于验证「我们抓到的确实是
证监会的内容页，而不是被重定向到了首页或错误页」。

政府网站改版或改路由后，旧 URL 常被重定向到首页而非返回 404。
此时抓取器会「成功地」抓到首页导航文字。站点自检能在这一步就拦住，
把状态标为 failed 而不是 ok —— **明确失败远优于静默错误**。
"""

# 导入正则用于从 meta 标签中提取内容
import re

# 从基类导入条目类型，用于给 _collect 的返回值标注泛型参数
from finreg_ai.fetchers.base import RawDoc
# 从 JSON 接口抓取器继承，复用其分页、字段映射与质量自检能力
from finreg_ai.fetchers.json_search_list import JsonSearchListFetcher

# 从 HTML 列表抓取器导入日期解析工具（本站点用不到，但保持导入以便未来回退）
from finreg_ai.fetchers.html_list import parse_chinese_date  # noqa: F401  保留供未来 HTML 回退路径使用

# 从 HTML 抓取器导入标题清理工具
from finreg_ai.fetchers.html_list import clean_title  # noqa: F401  保留供未来使用


# 从 HTML 中提取指定 meta 标签 content 属性的正则模板。
# 用正则而非 DOM 解析：meta 位于 <head>，正则更轻量，
# 且不受页面主体结构变化影响——而 meta 恰恰是用来对抗结构变化的信号，
# 二者应当解耦，否则信号本身就跟着一起坏了。
_META_TEMPLATE = r'<meta\s+name=["\']{name}["\']\s+content=["\']([^"\']*)["\']'


def extract_meta(html: str, name: str) -> str | None:
    """从 HTML 中提取指定 meta 标签的 content 值；未找到返回 None。

    参数 html 应当是**列表页**的 HTML（即空壳页），而非接口响应。
    """
    # 构造针对该 meta 名的正则，忽略大小写以容忍网站写法差异
    pattern = re.compile(_META_TEMPLATE.format(name=re.escape(name)), re.IGNORECASE)
    # 执行搜索
    match = pattern.search(html)
    # 命中则返回去除首尾空白的内容
    if match:
        # 返回提取值
        return match.group(1).strip()
    # 未命中返回 None
    return None


class CsrcFetcher(JsonSearchListFetcher):
    """证监会抓取器：JSON 接口取数 + 列表页站点自检。

    继承 ``JsonSearchListFetcher`` 获得完整的分页、字段映射、
    质量自检能力，仅覆写 ``_collect`` 以插入一步站点自检。
    """

    # 抓取器名称，用于工厂匹配
    name = "csrc"

    def _collect(self) -> tuple[list[RawDoc], str]:
        """先对列表页做站点自检，再走 JSON 接口取数。

        自检失败会抛异常，由基类的 ``fetch`` 捕获并标记为 failed。
        这是刻意的选择：宁可让这个源显示为失败，也不要让它带着
        错误的页面内容「成功」返回。
        """
        # 抓取列表页以提取 meta 信号
        # 注意：这里请求的是空壳页面，只为拿 meta 标签，不指望其中有条目
        response = self.get(self.source.list_url or "")
        # 列表页请求失败时抛异常
        if response is None:
            # 抛出带上下文异常，交由基类标记 failed
            raise RuntimeError(f"列表页请求失败：{self.source.list_url}（{self._last_error or '未知原因'}）")
        # 执行站点自检
        self._assert_column(response.text)
        # 自检通过后走父类的 JSON 接口逻辑取数
        return super()._collect()

    @staticmethod
    def _assert_column(html: str) -> None:
        """校验抓到的页面确实是证监会站点的内容页。

        抓不到 SiteName 时抛异常，让该源标记为 failed。
        代价是：如果证监会将来移除 meta 标签，这个源会失败。
        但这是刻意选择——失败会触发人工看一眼，
        而静默污染会让错误数据长期存在且无人察觉。
        """
        # 提取站点名 meta
        site_name = extract_meta(html, "SiteName")
        # 提取失败说明页面结构已变，或被抓到了非预期页面（如 CDN 错误页）
        if not site_name:
            # 抛出异常
            raise RuntimeError("未找到 SiteName meta 标签，页面结构可能已变更或被重定向到非预期页面")
        # 站点名不含「证券」说明抓错了站点
        if "证券" not in site_name:
            # 抛出异常并附上实际抓到的站点名，便于定位
            raise RuntimeError(f"页面站点自检失败：期望含「证券」，实际 SiteName={site_name!r}")
