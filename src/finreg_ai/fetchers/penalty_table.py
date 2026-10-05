"""行政处罚（罚单）抓取器 —— 列表页只给文号，真实内容在详情页表格里。

为什么需要单独一个抓取器
------------------------
人民银行「行政处罚公示」的形态与所有其它源都不同，通用抓取器处理不了：

1. **列表页的标题毫无信息量。** 它只有文号，如
   「银罚决字〔2026〕104-116号」——一份文书覆盖十几家机构。
   基于标题的关键词过滤在这里完全失效：没有任何标题会包含
   「数据安全」「人工智能」这类词，因此通用的「标题过滤」会把
   所有罚单一条不剩地滤掉，而抓取报告依旧显示 ok。

2. **真实内容在详情页的表格里，一行一家机构。** 实测（2026-10-05）详情页
   ``/index.html`` 内含完整 HTML 表格，列为：
   序号 / 当事人名称 / 行政处罚决定书文号 / 违法行为类型 /
   行政处罚内容 / 作出行政处罚决定机关名称 / 作出行政处罚决定日期 /
   公示期限 / 备注。全部是可抽取的文本，不是 PDF 也不是图片。

因此本抓取器做两件事：先在列表页拿到详情页地址，再逐个打开详情页把表格
**逐行**还原成条目。这样一条罚单对应一个条目，关键词过滤就能作用在
「违法行为类型」上——也就是真正有意义的那一栏。

一个关键设计：标题由「当事人 + 违规事由（截断）」拼成，而不是文号
----------------------------------------------------------------
``RawDoc.title`` 在本项目里承担着「让人一眼判断相关性」的职责。
若沿用文号，罚单在这一层就是不可见的：一条文号读起来与
「数据安全」「人工智能」毫无关系。因此本抓取器把标题重建为
``「当事人名称｜违法行为类型（截断）」``，既保留当事人（可检索），
又让「违反数据安全管理规定」这类字眼露出。

**截断不会造成漏检**——这是刻意保证的。标题截到
``VIOLATION_TITLE_MAX_LEN`` 字之后，被截掉的部分在标题里就看不见了；
若关键词过滤只看标题，一个正好落在截断点之后（甚至被切在词中间）的
关键词就会无声失效。因此过滤的匹配范围是「标题 + ``extra`` 里的
结构化字段值」（见 base.py 的 ``filter_haystack``），
``extra["violation_type"]`` 保存的是**未经截断的原文**，
过滤能看见它。截断只是排版决定，不参与正确性。

**文号并没有丢**——它在 ``extra["decision_no"]`` 里，
因为一条文书对应多家机构时，文号是唯一能把它们重新聚起来的线索。

本模块只做「原样抄录」，不做任何推断
----------------------------------
``extra`` 里的每个值都是表格单元格的原文，仅做空白归一化与首尾清理。
不猜测处罚金额（原文写「罚款1712.4万元」就照抄这句，不去解析数字），
不归类违规类型，不判断严重程度——那些属于人工复核环节。
"""

# 导入 re 用于空白归一化
import re
# 导入 Any 类型标注
from typing import Any

# 导入 BeautifulSoup 与 Tag 做容错解析
from bs4 import BeautifulSoup, Tag

# 从基类导入设施
from finreg_ai.fetchers.base import BaseFetcher, RawDoc, drop_probable_navigation
# 复用列表抓取器的列表解析逻辑：罚单的列表页与普通列表页结构一致，
# 没必要再写一份，否则两处的日期解析会各自演化、行为分叉。
from finreg_ai.fetchers.html_list import HtmlListFetcher


# 标题「违规事由」部分的最大长度。表格里的违法行为类型常含多条（用序号 1. 2. 3.
# 排列），本项目抓到的样本最长 158 字。全量拼进标题会让变更流无法逐行扫描。
# 取 60 字足以露出典型的头两条。
#
# 这个上限只影响**可读性**，不影响关键词过滤的正确性：
# 被截掉的部分仍完整保存在 ``extra["violation_type"]`` 里，
# 而过滤的匹配范围包含 extra（见 base.py 的 filter_haystack）。
# 改这个数字是排版调整，不会造成漏检——反之，若哪天有人把过滤改回
# 「只看标题」，这里就会立刻变成一个静默丢数据的开关。
VIOLATION_TITLE_MAX_LEN = 60

# 详情页表格默认的行选择器。人民银行详情页用的是无 class 的原生 table。
DEFAULT_ROW_SELECTOR = "table tr"

# 列名到「表头文字」的默认映射。
#
# 为什么按表头文字定位列，而不是按列序号
# --------------------------------------
# 列序号是排版细节，会随改版变化（本项目已在别处因依赖「第几列」而出错）；
# 而表头文字是这一列的**身份**。按文字匹配时，即使监管把「公示期限」挪到
# 「备注」之后，解析依然正确；按序号则会把公示期限读成备注，
# 而且这种错配不会抛异常，只是字段悄悄错位——正是本项目最警惕的静默错误。
DEFAULT_COLUMNS: dict[str, str] = {
    "party": "当事人名称",                    # 被处罚主体
    "decision_no": "行政处罚决定书文号",        # 决定书文号
    "violation_type": "违法行为类型",           # 违规事实（本项目最关心的一栏）
    "penalty_content": "行政处罚内容",          # 处罚种类与金额
    "authority": "作出行政处罚决定机关名称",     # 决定机关
    "decision_date": "作出行政处罚决定日期",     # 决定日期
    "publicity_period": "公示期限",             # 公示期限
}

# 表头文字在网页里常被换行与全角空格拆开（如「作出行政处罚\n决定机关名称」
# 或「当事人名称 」），因此匹配前必须归一化。这里去掉所有空白字符，
# 让「作出行政处罚决定机关名称」能与网页上的跨行写法对上。
_WHITESPACE = re.compile(r"\s+")


def normalize_header(text: str) -> str:
    """归一化表头文字，消除换行与全角空格带来的匹配失败。

    只删空白，不改动任何汉字——因此不会把两个不同的列名归一成同一个。
    这一步是必需的而非保守起见：实测人民银行的表头就是跨行的，
    不归一化会一列都匹配不上，而结果表现为「详情页解析出 0 条」，
    很容易被误判成「这一页没有表格」。
    """
    # 删除全部空白字符（含换行、制表、全角空格 U+3000）
    return _WHITESPACE.sub("", text or "")


class PenaltyTableFetcher(BaseFetcher):
    """行政处罚公示抓取器：列表页取地址，详情页表格逐行还原。"""

    # 抓取器名称，用于工厂匹配
    name = "penalty_table"

    # 列表页最多翻几页
    ABSOLUTE_MAX_PAGES = 5

    # 详情页最多打开几个。这是成本与覆盖的折中：
    # 每多开一个详情页就是一次对监管服务器的请求，而本抓取器一次运行
    # 可能面对几十个详情页。设上限既是对服务器的礼貌，
    # 也避免一次抓取耗时过长导致工作流超时。
    ABSOLUTE_MAX_DETAILS = 60

    # 在 _collect 中剔除的导航链接数。
    # 定义为类属性而不是仅靠 _collect 里的赋值：若哪天 _collect 提前抛异常，
    # 上层读取该属性时不应碰到 AttributeError——那会把一个本来清楚的
    # 「解析失败」变成一个更难懂的「属性不存在」。
    #
    # 命名带 ``_upstream_`` 前缀是因为基类会认这个名字：它把本值并入
    # ``FetchResult.dropped_navigation``，让「收集阶段剔除的导航」和
    # 「清洗阶段剔除的导航」在报告里是同一个数字，而不是后者显示 0、
    # 前者无人知晓。
    _upstream_dropped_navigation: int = 0

    # 详情页解析统计（打开数 / 未解析出表格数），供排查用
    _detail_stats: dict[str, int] = {}

    def parse(self, html: str) -> list[RawDoc]:
        """解析**列表页** HTML，返回「文书」级条目（标题即文号）。

        这个方法在本抓取器里不是主路径——真正的产出在 ``parse_detail`` 里，
        因为一条罚单的实质内容只存在于详情页。但基类把它定为抽象方法，
        且有实际用途：单独调用它可以只取「有哪些新文书」，
        不产生任何详情页请求。排查「列表页选择器是否还有效」时，
        这是唯一不需要打开几十个详情页就能验证的手段。

        实现委托给 HtmlListFetcher：罚单列表页与普通列表页结构一致，
        没有第二份实现的必要。
        """
        # 构造一个列表解析器（共用同一会话，复用连接与限速状态）
        lister = HtmlListFetcher(self.source, session=self.session)
        # 委托解析
        return lister.parse(html)

    def _collect(self) -> tuple[list[RawDoc], str]:
        """先解析列表页拿到详情页地址，再逐个打开详情页抽取表格行。"""
        # --- 第 1 步：用通用的列表解析逻辑取详情页地址 ---
        # 罚单的列表页与普通列表页结构相同，直接复用 HtmlListFetcher 的解析，
        # 避免同一套日期/标题清洗逻辑出现第二份实现。
        lister = HtmlListFetcher(self.source, session=self.session)
        # 复用其分页遍历能力，拿到全部列表项
        listed, first_html = lister._collect()

        # 【必须在打开详情页之前】剔除疑似栏目导航链接。
        #
        # 这一步踩过一次，代价是一次完整的失败运行：
        # 基类确实会在 _collect() 返回后调用 drop_probable_navigation，
        # 但那已经太晚——本抓取器会先为列表里的**每一条**打开详情页，
        # 于是导航项把 max_details 配额全部吃光，20 个详情页全是
        # 「政府信息公开年报」这类栏目页，一页表格也解析不出来。
        #
        # 教训具有一般性：**清洗必须发生在昂贵或不可逆的动作之前**。
        # 放在流水线后面固然也能让数据变干净，但它拦不住已经付出的代价。
        filtered_listed = drop_probable_navigation(listed, self.source.list_url or "")
        # 记录被剔除的数量。这个属性名会被基类识别并并入 FetchResult.dropped_navigation，
        # 因此「收集阶段剔除的导航」不会成为一笔无人知晓的账。
        self._upstream_dropped_navigation = len(listed) - len(filtered_listed)

        # 取出本抓取器的专属配置
        penalty_cfg = self._penalty_config()
        # 详情页数量上限
        max_details = min(
            int(penalty_cfg.get("max_details", self.ABSOLUTE_MAX_DETAILS) or self.ABSOLUTE_MAX_DETAILS),
            self.ABSOLUTE_MAX_DETAILS,
        )

        # --- 第 2 步：逐个打开详情页，把表格逐行还原 ---
        # 结果容器
        rows: list[RawDoc] = []
        # 详情页总数，用于计算「有多少页没能解析出表格」——这个数字必须可见
        opened = 0
        # 解析出 0 行的详情页数量。它若等于 opened，说明表格解析全面失效；
        # 若只是少数，说明部分文书用了别的排版。两种情况的处置方式不同，
        # 因此分开计数而不是只记一个「失败数」。
        empty_details = 0

        # 逐个详情页
        for doc in filtered_listed:
            # 到达上限则停止
            if opened >= max_details:
                # 结束
                break
            # 请求详情页
            response = self.get(doc.url)
            # 计数
            opened += 1
            # 请求失败时跳过这一页，不影响其余详情页
            if response is None:
                # 计入空解析
                empty_details += 1
                # 下一页
                continue
            # 解析表格
            page_rows = self.parse_detail(response.text, doc)
            # 未解析出行时计数
            if not page_rows:
                # 记录
                empty_details += 1
            # 合并
            rows.extend(page_rows)

        # 把「打开了几页、其中几页没解析出表格」记到实例上。
        # 不说去动 ``self._last_error``——那个字段记录的是最近一次请求的失败原因，
        # 由 get() 维护。在这里把它清成 None 会抹掉真正的网络错误线索。
        self._detail_stats = {"opened": opened, "empty": empty_details}
        # 全部详情页都没解析出表格：这是明确的结构变更，必须降级而不是静默返回空
        if opened and empty_details == opened:
            # 抛出异常由基类转为 failed，附上可执行的排查线索。
            # 这里刻意报错而非返回空列表：返回空会被基类判为「成功但 0 条」，
            # 于是「表格选择器失效」会伪装成「今天没有新罚单」——
            # 而罚单的更新频率本来就低，「今天没有」听起来完全合理。
            raise RuntimeError(
                f"打开了 {opened} 个处罚详情页，但没有一页解析出表格行 —— "
                f"详情页结构可能已变更，请检查 penalty.row / penalty.columns 配置"
            )

        # 「打开了若干页但只有部分解析出表格」也必须在结果里留下痕迹：
        # 它说明有一类文书的排版与主流不同，长期无视会形成稳定的覆盖缺口。
        if empty_details:
            # 记入最近错误说明（不阻断，只提示）
            self._last_error = (
                f"共打开 {opened} 个处罚详情页，其中 {empty_details} 个未解析出表格行"
                f"（这些文书可能使用了不同的排版，其处罚记录本次未被收录）"
            )

        # 返回结果
        return rows, first_html

    def _penalty_config(self) -> dict[str, Any]:
        """取出本抓取器的专属配置段（``Source.penalty``）。"""
        # 该配置与 api / selectors 平级，作用对象是详情页而非列表页，
        # 因此不混进 selectors —— 具体理由见 models.py 中 Source.penalty 的注释。
        return self.source.penalty or {}

    def parse_detail(self, html: str, list_doc: RawDoc) -> list[RawDoc]:
        """解析一个处罚详情页，返回逐行的条目。

        返回空列表表示这一页没有可解析的表格行（可能是排版不同、
        也可能确实是空页），由调用方决定如何计数与降级。
        """
        # 解析 HTML
        soup = BeautifulSoup(html, "lxml")
        # 取配置
        cfg = self._penalty_config()
        # 行选择器
        row_selector = cfg.get("row") or DEFAULT_ROW_SELECTOR
        # 列映射：允许配置覆盖默认值，便于不同站点/改版后只改 YAML
        columns: dict[str, str] = dict(DEFAULT_COLUMNS)
        # 应用配置里的覆盖项
        columns.update(cfg.get("columns") or {})

        # 建立「归一化表头文字 → 列索引」的映射。
        # 这一步是本抓取器正确性的关键：先认表头，再按表头取数据。
        header_index: dict[str, int] = {}
        # 表头所在行的行号；-1 表示尚未找到
        header_row = -1
        # 全部行
        all_rows = soup.select(row_selector)
        # 逐行扫描，找出表头行（含「当事人名称」的那一行）
        for index, tr in enumerate(all_rows):
            # 类型守卫
            if not isinstance(tr, Tag):
                # 跳过
                continue
            # 取该行全部单元格
            cells = tr.find_all(["th", "td"])
            # 归一化每个单元格的文字
            texts = [normalize_header(c.get_text(" ", strip=True)) for c in cells]
            # 判据：至少命中一半的期望列名，才算表头行。
            # 用「至少一半」而不是「命中全部」：表头可能新增列（如实测的「备注」），
            # 也可能某些列在个别站点缺失；只要多数对上，就足以确认这是表头。
            matched = sum(1 for want in columns.values() if want in texts)
            # 达到半数即认定为表头
            if matched >= max(2, len(columns) // 2):
                # 记录列索引
                header_index = {text: i for i, text in enumerate(texts)}
                # 记录表头行号
                header_row = index
                # 找到即可停止
                break

        # 没有找到表头行：无法安全地定位列，明确返回空而不是按序号硬取。
        # 按序号硬取的后果是字段错位且不报错，属于静默错误，宁可返回空。
        if header_row < 0:
            # 无表头无从解析
            return []

        # 逐行解析数据行（只取表头之后的行）
        docs: list[RawDoc] = []
        # 遍历表头之后的行
        for tr in all_rows[header_row + 1:]:
            # 类型守卫
            if not isinstance(tr, Tag):
                # 跳过
                continue
            # 取单元格
            cells = tr.find_all(["th", "td"])
            # 单元格数量不足以覆盖表头时跳过（可能是合并行、说明行）
            if len(cells) < 2:
                # 跳过
                continue
            # 归一化文字
            texts = [c.get_text(" ", strip=True) for c in cells]

            # 取「当事人名称」。取不到视为非数据行——
            # 本表里没有当事人就不是一条处罚记录。
            party = self._cell(texts, header_index, columns["party"])
            # 空则跳过该行
            if not party:
                # 跳过
                continue

            # 逐列抄录其余字段
            violation = self._cell(texts, header_index, columns["violation_type"])
            # 文号
            decision_no = self._cell(texts, header_index, columns["decision_no"])
            # 处罚内容
            penalty_content = self._cell(texts, header_index, columns["penalty_content"])
            # 决定机关
            authority = self._cell(texts, header_index, columns["authority"])
            # 决定日期（原文形如「2026年9月8日」，原样保留不解析成 date，
            # 因为它是「决定日期」而非「发布日期」，语义不同，混用会误导使用者）
            decision_date = self._cell(texts, header_index, columns["decision_date"])
            # 公示期限
            publicity_period = self._cell(texts, header_index, columns["publicity_period"])

            # 组装标题：当事人 + 违规事由截断。
            # 违规事由为空时只留当事人，不留一个孤零零的分隔符。
            title = f"{party}｜{self._shorten(violation)}" if violation else party

            # 结构化字段：原样抄录，不做任何解析或推断。
            # 注意 violation_type 这里是**未截断的原文**，而标题里是截断版。
            # 这个差异是刻意的：标题负责可读，extra 负责完整与过滤匹配。
            extra: dict[str, Any] = {
                "record_type": "penalty",          # 条目标记，供下游区分条目种类
                "party": party,                    # 当事人
                "decision_no": decision_no,        # 决定书文号
                "violation_type": violation,       # 违法行为类型（原文）
                "penalty_content": penalty_content,  # 处罚内容（原文，含金额表述）
                "authority": authority,            # 决定机关
                "decision_date": decision_date,    # 决定日期（原文中文格式）
                "publicity_period": publicity_period,  # 公示期限
                "document_url": list_doc.url,      # 所属公示文书的地址
            }

            # 日期取列表页的公示日期（这是该条在列表页可见的时间），
            # 而不是决定日期——两者语义不同，混用会让时间线错乱。
            docs.append(
                RawDoc(
                    title=title,                                        # 重建的标题
                    url=f"{list_doc.url}#{decision_no or party}",       # 行级唯一地址
                    source_id=self.source.id,                           # 来源标识
                    published_on=list_doc.published_on,                 # 公示日期
                    extra=extra,                                        # 结构化字段
                )
            )

        # 返回该页解析出的全部行
        return docs

    @staticmethod
    def _cell(texts: list[str], header_index: dict[str, int], column_name: str) -> str:
        """按列名从一行里取值；列不存在或越界时返回空字符串。

        返回空串而不是抛异常：某一天监管新增或删除一列时，
        我们希望这一列变成空值（可在校验环节被看见），
        而不是让整个源的抓取失败。
        """
        # 查该列在表头中的位置
        index = header_index.get(normalize_header(column_name))
        # 列不存在
        if index is None:
            # 返回空
            return ""
        # 越界保护：某些行的单元格数少于表头
        if index >= len(texts):
            # 返回空
            return ""
        # 归一化空白并去首尾
        return _WHITESPACE.sub(" ", texts[index]).strip()

    @staticmethod
    def _shorten(text: str, limit: int = VIOLATION_TITLE_MAX_LEN) -> str:
        """把违规事由截断到适合放进标题的长度，并显式标记截断。

        截断处加省略号是刻意的：不加的话，一个被截断的事由看起来
        像一条完整的短事由，人工复核时会以为原文就这么短。

        这是**纯展示层**的处理，被截掉的内容不会从数据里消失：
        调用方同时把未截断的原文放进 ``extra["violation_type"]``，
        而关键词过滤会匹配 extra。因此这里不存在「截断导致漏检」。
        """
        # 单行化：原文里的换行会破坏变更流的逐行排版
        flat = _WHITESPACE.sub(" ", text or "").strip()
        # 未超长则原样返回
        if len(flat) <= limit:
            # 返回
            return flat
        # 截断并标记
        return flat[:limit] + "…"
