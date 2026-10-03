"""通用 HTML 列表抓取器 —— 配置驱动，网站改版时只改 YAML 不改代码。

中国政府网站的列表页结构可以归为两大类，本抓取器都支持：

1. **现代 div 布局**：列表项为 ``<div class="list-item">``，
   标题链接与日期是其后代节点。使用 CSS 选择器定位。

2. **传统 table 布局**：列表项为 ``<tr>``，标题在第一列，日期在末列。
   人民银行、部分交易所的历史页面大量使用这种结构。
   注意：table 布局中 ``td:last-child`` 常被误当作日期列，
   因此本实现会校验取到的文本是否真的像日期，不像则丢弃。

另外，政府网站的日期格式极其混乱，本模块的 ``parse_chinese_date``
覆盖了实测遇到的全部变体，并在无法解析时返回 None 而非猜一个。
"""

# 导入正则用于标题清洗与日期匹配
import re
# 导入 date 类型
from datetime import date
# 导入 Any 类型标注
from typing import Any

# 导入 BeautifulSoup 做容错解析
from bs4 import BeautifulSoup, Tag

# 从基类导入所需设施
from finreg_ai.fetchers.base import BaseFetcher, RawDoc, dedupe_by_url

# ------------------------------------------------------------------
# 日期解析：覆盖中国政务网站实测遇到的全部格式
# ------------------------------------------------------------------

# 匹配「2026-06-18」「2026/06/18」「2026.06.18」以及带时间的变体
_DATE_ISO_LIKE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
# 匹配「2026年6月18日」，这是中国政府网站最常用的格式
_DATE_CHINESE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
# 匹配「26-06-18」这类两位年份的简写
_DATE_SHORT_YEAR = re.compile(r"\b(\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b")


def parse_chinese_date(text: str | None) -> date | None:
    """从任意文本片段中提取日期。

    解析顺序按「越明确越优先」排列：先试完整中文格式（无歧义），
    再试 ISO 格式，最后才试两位年份简写（歧义最大）。

    关键设计：**无法解析时返回 None，绝不猜测**。
    如果猜错了日期，一条政策可能被归到错误的生效时间，
    在合规场景中这比缺少日期危险得多。
    """
    # 空值直接返回 None
    if not text:
        # 无内容无从解析
        return None
    # 去除首尾空白，避免因空格导致正则失配
    text = text.strip()

    # --- 第一种：中文年月日格式（最明确，优先） ---
    match = _DATE_CHINESE.search(text)
    # 命中则直接构造日期
    if match:
        # 解构出年、月、日
        year, month, day = (int(g) for g in match.groups())
        # 尝试构造，非法日期（如 2 月 30 日）会被拒绝
        try:
            # 返回构造出的日期
            return date(year, month, day)
        except ValueError:
            # 非法日期视为解析失败
            return None

    # --- 第二种：四位数年份的 ISO 类格式 ---
    match = _DATE_ISO_LIKE.search(text)
    # 命中则构造日期
    if match:
        # 解构出年、月、日
        year, month, day = (int(g) for g in match.groups())
        # 尝试构造
        try:
            # 返回日期
            return date(year, month, day)
        except ValueError:
            # 非法日期视为解析失败
            return None

    # --- 第三种：两位年份简写（歧义最大，最后尝试） ---
    match = _DATE_SHORT_YEAR.search(text)
    # 命中则构造日期
    if match:
        # 解构出两位年、月、日
        short_year, month, day = (int(g) for g in match.groups())
        # 两位年份的世纪归属：本项目只处理 2000 年后的政策
        # 因此 00-99 一律视为 20xx
        year = 2000 + short_year
        # 尝试构造
        try:
            # 返回日期
            return date(year, month, day)
        except ValueError:
            # 非法日期视为解析失败
            return None

    # 全部失败，返回 None 表示「无法确定」而非「无日期」的猜测
    return None


# 用于清理标题中的噪声。政府网站列表页常在标题后附加附件标记或状态标签。
_TITLE_NOISE = re.compile(r"(\[?附件\d*\]?|【.*?】|\(.*?\)|（.*?）)$")


def clean_title(raw: str) -> str:
    """清理标题中的常见噪声，返回干净标题。

    只做最小清理：去除首尾空白、合并连续空白、去掉末尾的括号备注。
    刻意不做「智能摘要」——标题必须与官方原文一致，
    任何改写都会破坏可核验性。这个函数只处理排版噪声，不改动语义。
    """
    # 空值返回空字符串
    if not raw:
        # 无内容
        return ""
    # 把换行、制表符、连续空格统一压成单个空格（政府网站常用大量空白对齐）
    text = re.sub(r"\s+", " ", raw)
    # 去除首尾空白
    text = text.strip()
    # 反复剥离末尾的噪声标记（可能叠加多层，如「标题【最新】(附件)」）
    while True:
        # 尝试剥离一层
        stripped = _TITLE_NOISE.sub("", text).strip()
        # 没有变化说明已剥离干净，跳出循环
        if stripped == text:
            # 结束循环
            break
        # 更新文本继续下一轮
        text = stripped
    # 返回清理后的标题
    return text


class HtmlListFetcher(BaseFetcher):
    """配置驱动的通用 HTML 列表抓取器。

    通过 data/sources.yaml 中的 ``selectors`` 与 ``pagination`` 配置工作，
    因此新增一个结构标准的数据源不需要写任何代码。
    """

    # 抓取器名称，用于工厂匹配
    name = "html_list"

    # 分页最多抓取的页数上限，防止配置错误导致无限翻页
    ABSOLUTE_MAX_PAGES = 10

    def _collect(self) -> tuple[list[RawDoc], str]:
        """遍历所有分页，收集全部条目。

        覆写基类方法以支持分页。设计上先抓首页，再按需抓后续页，
        并在后续页解析到 0 条时提前终止——这是对「已到最后一页」的常见处理，
        避免为不存在的页面反复发请求。
        """
        # 结果容器
        all_docs: list[RawDoc] = []
        # 首页 HTML，用于调试
        first_html = ""
        # 取出分页配置
        pagination = self.source.pagination or {}
        # 是否启用分页
        paging_enabled = bool(pagination.get("enabled"))
        # 计算实际抓取页数，受绝对上限约束
        max_pages = min(int(pagination.get("max_pages", 1) or 1), self.ABSOLUTE_MAX_PAGES)

        # 逐页抓取
        for page in range(1, max_pages + 1 if paging_enabled else 2):
            # 计算本页 URL：第一页用 list_url，后续页用模板生成
            url = self._page_url(page, pagination)
            # 请求页面
            response = self.get(url)
            # 请求失败：首页失败则整体失败，后续页失败则停止翻页但保留已有结果
            if response is None:
                # 首页失败说明源不可用，直接抛异常由 fetch 转为 failed
                if page == 1:
                    # 抛出带上下文异常
                    raise RuntimeError(f"列表页请求失败：{url}")
                # 后续页失败只记录并停止，不影响已收集的结果
                break
            # 记录首页 HTML 供调试
            if page == 1:
                # 保存首页 HTML
                first_html = response.text
            # 解析本页条目
            page_docs = self.parse(response.text)
            # 后续页解析到 0 条，说明已翻到末页，停止
            if page > 1 and not page_docs:
                # 结束翻页
                break
            # 合并结果
            all_docs.extend(page_docs)

        # 去重：分页边界上可能出现重复条目（政府网站翻页实现不严谨）
        deduped = self._dedupe(all_docs)
        # 返回去重结果与首页 HTML
        return deduped, first_html

    def _page_url(self, page: int, pagination: dict[str, Any]) -> str:
        """构造第 page 页的 URL。

        两种模式：
        - 第 1 页用 ``list_url`` 原值（这是列表页的真实地址）
        - 第 N 页用 ``url_pattern`` 模板生成，其中 ``{page}`` 被替换
          注意：很多网站的首页是无后缀的 ``index.html``，
          而第 2 页是 ``index1.html``，因此需要 ``{page}`` 支持
          正负偏移，这里用 ``page - 1`` 的语义由配置方在模板中体现。
        """
        # 第一页直接用配置的列表页地址
        if page == 1:
            # 返回原地址
            return self.source.list_url or ""
        # 取出 URL 模板
        pattern = pagination.get("url_pattern")
        # 未配置模板时无法翻页，退回第一页地址（避免拼出错误 URL）
        if not pattern:
            # 退回首地址
            return self.source.list_url or ""
        # 用基准地址作为前缀拼接
        base = (self.source.list_url or "").rsplit("/", 1)[0]
        # 替换占位符；大多数中国政务网站的页码从 1 开始对应第二页，故用 page-1
        concrete = pattern.replace("{page}", str(page - 1))
        # 拼成完整地址
        return f"{base}/{concrete}"

    def parse(self, html: str) -> list[RawDoc]:
        """按配置的选择器解析列表页，返回条目列表。"""
        # 用 lxml 后端解析，容错性更好且速度快
        soup = BeautifulSoup(html, "lxml")

        # 取出选择器配置
        item_sel = self._resolve_selector("item")
        # 未配置 item 选择器则无法解析，直接返回空
        if not item_sel:
            # 交由上层标记 degraded
            return []

        # 标题选择器，默认为 a
        title_sel = self._resolve_selector("title", "a")
        # 链接选择器，默认为 a
        link_sel = self._resolve_selector("link", "a")
        # 日期选择器，可能为 None（列表页无日期）
        date_sel = self._resolve_selector("date")

        # 结果容器
        docs: list[RawDoc] = []
        # 遍历所有列表项
        for item in soup.select(item_sel):
            # 类型守卫：soup.select 通常返回 Tag，但类型标注上是 Tag | NavigableString
            if not isinstance(item, Tag):
                # 跳过非标签节点
                continue
            # 在列表项内查找标题元素
            title_node = item.select_one(title_sel or "a")
            # 在列表项内查找链接元素
            link_node = item.select_one(link_sel or "a")
            # 缺少标题或链接的条目无法使用，跳过
            if title_node is None or link_node is None:
                # 跳过该条
                continue
            # 提取标题文本并清理排版噪声
            title = clean_title(title_node.get_text(" ", strip=True))
            # 标题过短说明解析到了非标题元素（如导航栏），跳过
            if len(title) < 4:
                # 跳过该条
                continue
            # 提取链接地址
            href = link_node.get("href")
            # href 可能为 None 或空
            if not href or not isinstance(href, str):
                # 跳过该条
                continue
            # 补全为绝对链接
            url = self._absolutize(href)
            # 只保留 http/https 链接，过滤 javascript: 与 mailto: 等
            if not url.startswith(("http://", "https://")):
                # 跳过该条
                continue

            # 提取日期
            published: date | None = None
            # 优先用配置的日期选择器
            if date_sel:
                # 查找日期元素
                date_node = item.select_one(date_sel)
                # 命中则解析
                if date_node is not None:
                    # 从文本中提取日期
                    published = parse_chinese_date(date_node.get_text(" ", strip=True))
            # 未取到日期时，退化为在整行文本中搜索日期
            # 这对 table 布局很有效——日期通常就在标题同一行的某个单元格
            if published is None:
                # 从整行文本中尝试提取
                published = parse_chinese_date(item.get_text(" ", strip=True))

            # 组装条目
            docs.append(
                RawDoc(
                    title=title,                    # 清理后的标题
                    url=url,                        # 绝对链接
                    source_id=self.source.id,       # 来源标识
                    published_on=published,         # 解析出的日期（可能为 None）
                )
            )

        # 返回解析结果
        return docs

    @staticmethod
    def _dedupe(docs: list[RawDoc]) -> list[RawDoc]:
        """按链接去重，保持首次出现的顺序。

        实现委托给基类的 ``dedupe_by_url``：去重是「每个抓取器都必须做的事」，
        把实现收敛到一处，避免各子类各写一份、行为逐渐分叉。
        这里保留同名方法只是为了不破坏既有调用点。
        """
        # 委托给基类的统一实现
        return dedupe_by_url(docs)
