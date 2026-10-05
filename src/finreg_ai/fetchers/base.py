"""抓取器基类与公共设施。

设计要点
--------
监管网站会超时、会改版、会反爬。基类把所有这类风险收拢到一处统一处理：

- **超时与重试**：政府网站响应慢是常态，必须设置合理超时与有限重试
- **限速**：对监管机构服务器保持礼貌，也避免自身被临时封禁
- **单源隔离**：任何异常都被捕获并转成 ``FetchResult(status="failed")``，
  绝不让异常向上冒泡打断整个流水线
- **结构变更检测**：抓到的条目数为 0 时明确标记为 degraded，
  而不是当作「今天没有新政策」——这两者必须区分，否则漏抓会伪装成正常

最后一条尤其重要。竞品失败的典型方式不是崩溃，而是静默返回空结果。
"""

# 导入 abc 用于定义抽象基类
import abc
# 导入 dataclass 用于定义数据结构
from dataclasses import dataclass, field
# 导入 date 类型
from datetime import date, datetime, timezone, timedelta
# 导入 time 用于限速休眠
import time
# 导入 Iterable 类型标注（来源为 collections.abc 而非 typing，理由见 cli.py）
from collections.abc import Iterable
# 导入 Any 类型标注
from typing import Any
# 导入 urljoin 用于把相对链接补全为绝对链接
from urllib.parse import urljoin

# 导入 requests 发起 HTTP 请求
import requests
# 导入 requests 的异常类以便精确捕获
from requests.exceptions import RequestException

# 从本包导入数据源模型
from finreg_ai.models import Source


# 中国大陆时区（UTC+8）。政府网站的发布时间均为此时区，
# 若用 UTC 记录会导致跨日边界上的政策被归到错误的日期。
CHINA_TZ = timezone(timedelta(hours=8))

# 默认请求超时秒数。取 20 秒是因为实测部分监管网站在慢速网络下需要 10 秒以上，
# 但也不能无限等待，否则一个卡死的源会拖住整条流水线。
DEFAULT_TIMEOUT = 20

# 默认重试次数。3 次是经验值：能覆盖瞬时抖动，又不会在源彻底不可用时浪费太久。
DEFAULT_RETRIES = 3

# 默认 User-Agent。使用浏览器标识而非脚本标识，是因为部分政府网站
# 会拒绝未知 UA。但同时在括号内声明真实身份与用途，保持善意。
#
# 【重要】此字符串必须只含 ASCII 字符。
# HTTP 请求头按 RFC 7230 只能承载 latin-1 可编码的字节，
# 一旦混入中文，requests 在发送时会抛出
# 「UnicodeEncodeError: 'latin-1' codec can't encode characters」，
# 而且是所有请求一起失败——本项目的注释风格是中文，
# 极易在写 UA 时顺手带上中文说明，这个坑已经踩过一次。
# 【这个 URL 不是装饰】User-Agent 里的联系方式是爬虫礼仪的一部分：
# 站点运营方会顺着它找到项目主页。若指向一个不存在的地址，
# 对方无法判断我们是善意低频抓取还是恶意爬虫，也无法在有问题时联系我们。
# 因此它必须指向真实可访问的仓库地址，仓库改名或迁移时要同步更新这一行。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 "
    "finreg-ai-cn/0.1 (+https://github.com/passer-111/finreg-ai-cn; compliance policy KB; low-frequency crawl)"
)


def now_china_iso() -> str:
    """返回当前中国大陆时间的 ISO 8601 字符串（带 +08:00 偏移）。

    为什么不用 UTC：记录抓取时间是为了让使用者判断数据新鲜度。
    中国使用者看到 UTC 时间需要心算换算，而带 +08:00 的时间戳一看就懂。
    关键是必须带时区偏移——不带时区的时间戳在跨时区协作时无法比较。
    """
    # 取当前 UTC 时间并转换为北京时间
    return datetime.now(CHINA_TZ).replace(microsecond=0).isoformat()


def today_china() -> date:
    """返回中国大陆时区的今天。

    为什么不用 ``date.today()``：服务器可能部署在非中国时区，
    用本地日期会导致「今天抓到的政策」被记录成昨天或明天。
    """
    # 取北京时间后只保留日期部分
    return datetime.now(CHINA_TZ).date()


# ============================================================
# 数据结构
# ============================================================

@dataclass
class RawDoc:
    """从列表页解析出的一条尚未结构化的政策条目。

    注意这是「中间产物」——它只包含列表页能提供的信息
    （标题、链接、日期）。要成为 ``Policy`` 记录，还需要人工提炼
    关键义务、判断约束力、核验状态。这个区分是刻意的：
    机器负责发现，人负责定性。
    """

    # 标题原文，不做任何清洗或截断，便于人工比对
    title: str
    # 详情页绝对链接
    url: str
    # 来源数据源 id，用于回溯是哪次抓取发现的
    source_id: str
    # 列表页显示的发布日期；列表页无日期时为 None
    published_on: date | None = None
    # 抓取该条目时所处列表页的原始 HTML（可选，用于事后调试解析逻辑）
    raw_html: str | None = None
    # 抓取时间
    fetched_at: str = field(default_factory=now_china_iso)


@dataclass
class FetchResult:
    """单次抓取的结果。

    状态取值刻意区分了四种情况，因为「没抓到」有四种完全不同的含义，
    混在一起会让运维失去判断依据：

    - ``ok``：正常抓到条目
    - ``empty``：请求成功但列表为空（可能是真的没有政策，也可能是选择器失效）
    - ``degraded``：请求成功但结果可疑（如条目数为 0，需要人工确认）
    - ``failed``：请求失败（超时、连接错误、HTTP 错误码）
    - ``skipped``：源被禁用或为 manual 类型，本次未执行
    """

    # 来源数据源 id
    source_id: str
    # 抓取状态
    status: str
    # 解析出的条目
    docs: list[RawDoc] = field(default_factory=list)
    # 失败或降级时的原因说明
    error: str | None = None
    # 本次抓取的完成时间
    fetched_at: str = field(default_factory=now_china_iso)
    # 实际发出的 HTTP 请求数，用于观察抓取成本
    request_count: int = 0
    # 被判定为「栏目导航链接」而剔除的条目数。
    #
    # 为什么要把这个数字暴露出来，而不是内部悄悄删掉：
    # 清洗操作只要不可见，就会变成新的静默风险——某天一个改版让
    # 大量真实条目被误判为导航，而报告依旧显示 ok，没有人会发现。
    # 把剔除数量打印出来，运维看到异常大的数字就能立刻警觉。
    dropped_navigation: int = 0
    # 被关键词过滤丢弃的条目总数。
    #
    # 与 dropped_navigation 是同一类问题的两个面：导航剔除会误杀「无日期但标题长」
    # 的条目，关键词过滤则会误杀「标题不含 AI 字样但正文含 AI 条款」的文件。
    # 两者都必须可见，否则误杀都是静默的。
    #
    # 实测背景：2026-10-05 复核时发现关键词过滤的丢弃数此前完全未被记录——
    # 一个源可能每天都在丢十几条，而报告始终显示 ok。
    dropped_by_filter: int = 0
    # 关键词丢弃的原因细分，形如 {"excluded": 2, "not_matched": 12}。
    #
    # 为什么要细分：两种原因要采取的行动完全相反——excluded 多说明排除词
    # 写得太宽（误伤真实政策），not_matched 多说明包含词写得太窄（漏检）。
    # 只看一个总数无法判断该调哪一边。
    filter_drop_reasons: dict[str, int] = field(default_factory=dict)
    # 被关键词过滤丢弃的**条目本身**，供人工复核。
    #
    # 为什么光有计数不够：`已滤除 151 条` 是一个无法行动的数字——
    # 维护者看到它，既不知道被丢的是《银行业保险业数字金融高质量发展
    # 实施方案》还是某场表彰大会的新闻稿，也就无从判断该不该改关键词。
    # 把条目留下来，「可疑的没有被丢」这件事才可被检查。
    #
    # 内存代价：RawDoc 只含标题、链接、日期等短字段
    # （raw_html 字段在本项目中从未被任何抓取器赋值），
    # 每个源留几条到上百条都在可忽略量级。
    dropped_filter_docs: list[RawDoc] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        """判断本次结果是否可用于后续流水线。

        只有 ``ok`` 与 ``empty`` 可用于写库；
        ``degraded`` 与 ``failed`` 必须阻断该源，等待人工介入。
        """
        # ok 表示正常；empty 表示确实没内容（后续会与上次结果比对确认）
        return self.status in ("ok", "empty")


# ============================================================
# 抽象抓取器
# ============================================================

class BaseFetcher(abc.ABC):
    """抓取器抽象基类。

    子类只需实现 ``parse``（如何从 HTML 中解析条目），
    其余网络、限速、重试、异常处理全部由基类承担。
    这样新增一个数据源的成本很低——通常只需写十几行解析逻辑。
    """

    # 子类可覆盖的类属性：标识抓取器名称，用于日志与工厂匹配
    name: str = "base"

    def __init__(self, source: Source, session: requests.Session | None = None) -> None:
        """初始化抓取器。

        参数 source 是该抓取器负责的数据源配置；
        参数 session 允许注入自定义会话（测试时用于注入 mock）。
        """
        # 保存数据源配置
        self.source = source
        # 使用注入的会话，或新建一个
        self.session = session or self._build_session()
        # 记录上次请求时间，用于限速
        self._last_request_at: float = 0.0
        # 记录本次已发出的请求数
        self._request_count: int = 0
        # 记录最近一次请求的失败原因，供上层构造可诊断的错误信息。
        # 初始化为 None 而非不定义，避免在 get() 从未被调动时
        # 访问该属性抛出 AttributeError。
        self._last_error: str | None = None
        # 记录最近一次请求的 HTTP 状态码，供诊断反爬（如 403）使用。
        # 同样先初始化，避免属性未定义错误。
        self._last_http_status: int | None = None

    def _build_session(self) -> requests.Session:
        """构造带默认请求头与重试策略的会话。"""
        # 新建会话以复用 TCP 连接
        session = requests.Session()
        # 设置默认请求头
        session.headers.update(
            {
                # 伪装为浏览器 UA，部分政府网站会拒绝脚本 UA
                "User-Agent": DEFAULT_USER_AGENT,
                # 声明接受 HTML
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                # 声明接受中文，避免网站返回英文版或乱码
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                # 声明接受压缩以节省带宽
                "Accept-Encoding": "gzip, deflate",
                # 保持连接，减少握手开销
                "Connection": "keep-alive",
            }
        )
        # 返回配置好的会话
        return session

    def _throttle(self) -> None:
        """按配置的最小间隔限速。

        实现方式：记录上次请求时刻，若距现在不足间隔则休眠补足。
        这样做既保护对方服务器，也降低自己被临时封禁的概率——
        对于一个需要长期每日运行的项目，被封禁的代价远高于多等几秒。
        """
        # 取出配置的最小间隔
        interval = float(self.source.min_interval_seconds or 0)
        # 间隔为 0 或负值时不需要限速
        if interval <= 0:
            return
        # 计算距离上次请求已过去多久
        elapsed = time.monotonic() - self._last_request_at
        # 不足间隔时补足休眠
        if elapsed < interval:
            # 休眠剩余时间
            time.sleep(interval - elapsed)

    def get(self, url: str, *, retries: int = DEFAULT_RETRIES, timeout: int = DEFAULT_TIMEOUT) -> requests.Response | None:
        """发起 GET 请求，带限速、重试与异常吞没。

        为什么吞掉异常而不往上抛：本项目的核心稳定性要求是
        「单源失效不拖垮整体」。抛异常会让调用方必须写 try/except，
        而漏写一处就会让整个流水线中断。这里统一返回 None 表示失败，
        由调用方检查即可。
        """
        # 上一次的异常信息，用于最终报告失败原因
        last_error: str | None = None
        # 按重试次数循环。循环变量本身不参与计算（只是计数），
        # 因此以下划线开头命名，明确表示「这是有意不使用的变量」，
        # 避免读者以为漏写了什么。
        for _attempt in range(1, retries + 1):
            # 请求前限速
            self._throttle()
            try:
                # 发起请求
                response = self.session.get(url, timeout=timeout)
                # 记录请求时刻，供下次限速计算
                self._last_request_at = time.monotonic()
                # 累计请求数
                self._request_count += 1
                # HTTP 状态码非 2xx 视为失败
                if response.status_code >= 400:
                    # 记录错误信息
                    last_error = f"HTTP {response.status_code}"
                    # 4xx 是客户端问题，重试无意义，直接返回 None
                    if 400 <= response.status_code < 500:
                        return None
                    # 5xx 是服务端问题，继续重试
                    continue
                # 成功时显式设置编码，避免 requests 依据 Content-Type 猜测出错
                # 中国政府网站常不声明 charset 或声明为 ISO-8859-1，导致中文乱码
                response.encoding = self._detect_encoding(response)
                # 返回响应
                return response
            except RequestException as exc:
                # 网络层异常（超时、连接失败、DNS 失败等）
                last_error = f"{type(exc).__name__}: {exc}"
                # 更新限速计时，避免失败后立即重试造成连环冲击
                self._last_request_at = time.monotonic()
                # 次数用尽前继续重试
                continue
        # 全部重试失败时记录失败原因，供上层构造 FetchResult
        self._last_error = last_error
        # 返回 None 表示失败
        return None

    @staticmethod
    def _detect_encoding(response: requests.Response) -> str:
        """推断响应编码，优先信任页面内的声明。

        政府网站常见的编码问题：HTTP 头声明 ISO-8859-1，
        但页面 meta 声明 gb2312 或 utf-8。此时若信任 HTTP 头，中文会全部乱码。
        因此优先从 HTML 的 meta 标签或内容探测中取编码。
        """
        # 从响应内容的前若干字节猜测编码（requests 会解析 meta charset）
        apparent = response.apparent_encoding
        # 猜测到有效编码就用它
        if apparent:
            # 归一化为小写并返回
            return apparent.lower()
        # 猜不到时退回 HTTP 头声明的编码
        return response.encoding or "utf-8"

    def fetch(self) -> FetchResult:
        """执行一次完整抓取：请求列表页 → 解析 → 过滤。

        这是对外的主入口。所有异常都在此被捕获并转成 FetchResult，
        保证调用方永远拿到结果对象而非异常。
        """
        # 源被禁用时直接跳过
        if not self.source.enabled:
            # 返回 skipped 状态，说明原因
            return FetchResult(source_id=self.source.id, status="skipped", error="数据源已禁用")
        # manual 类型不抓取
        if self.source.fetcher == "manual":
            # 返回 skipped 状态
            return FetchResult(source_id=self.source.id, status="skipped", error="人工录入源，不执行抓取")
        # 缺少列表页地址时无法抓取
        if not self.source.list_url:
            # 返回 failed 状态，这是配置错误，需要人修
            return FetchResult(source_id=self.source.id, status="failed", error="未配置 list_url")

        # 解析前先记录本次抓取时间
        fetched_at = now_china_iso()
        try:
            # 调用子类实现的解析逻辑收集条目
            docs, page_html = self._collect()
        except Exception as exc:  # noqa: BLE001  刻意捕获全部异常以保证单源隔离
            # 解析过程中的任何异常都转为 failed，附上异常类型便于定位
            return FetchResult(
                source_id=self.source.id,      # 源标识
                status="failed",               # 失败状态
                error=f"{type(exc).__name__}: {exc}",  # 错误说明
                fetched_at=fetched_at,         # 抓取时间
                request_count=self._request_count,  # 请求数
            )

        # 拿到条目后依次做三步处理：去重 → 剔除导航链接 → 关键词过滤。
        #
        # 这三步刻意「上收到基类」而不是留给各子类自行处理，原因是一个
        # 真实踩过的坑：PbcFetcher 直接继承 BaseFetcher 而未复用
        # HtmlListFetcher 的 _collect，结果既没有分页也没有去重——
        # 同一条公告被收录了三次，而没有任何地方会报警。
        # 把公共步骤放在基类，才能让「正确的做法」变成「默认的做法」。

        # 第 1 步：按链接去重。分页边界与页面内的重复链接都会产生重复条目。
        deduped = dedupe_by_url(docs)

        # 第 2 步：剔除疑似栏目导航链接（如人行列表页里的「金融科技」栏目入口）。
        # 这类链接不是政策文件，但标题里可能恰好含有关键词，
        # 从而骗过关键词过滤被当作政策收录——这是最隐蔽的一种污染。
        cleaned = drop_probable_navigation(deduped, self.source.list_url or "")
        # 记录剔除数量，供上层在报告中显示
        dropped_nav = len(deduped) - len(cleaned)

        # 第 3 步：应用关键词过滤，并记录丢弃原因计数。
        # 这里用 filter_docs_with_reasons 而非 apply_filters——后者只返回保留结果，
        # 会让「标题不含 AI 字样、正文却含 AI 条款」的文件被静默丢掉且无迹可查。
        filtered, filter_reasons = filter_docs_with_reasons(cleaned, self.source.filters)
        # 丢弃总数取各原因之和，保证与原因明细始终一致
        dropped_filter = sum(filter_reasons.values())

        # 把被丢弃的条目本身也留下来，供人工复核。
        #
        # 这里用「URL 差集」而不是让 filter_docs_with_reasons 一并返回，
        # 是为了让该函数的返回值保持稳定（两个元素），不因新增展示需求而改签名。
        # 差集在此处是精确的：cleaned 已经过 dedupe_by_url 去重，URL 唯一；
        # 且 filtered 必然是 cleaned 的子序列。二者相减不会多也不会少。
        # 「被丢弃的条目数 == dropped_by_filter」这条不变式由测试钉住。
        kept_urls = {doc.url for doc in filtered}
        # 保序输出：按 cleaned 的原始顺序（即列表页顺序，通常由新到旧）
        dropped_docs = [doc for doc in cleaned if doc.url not in kept_urls]

        # 判断结果状态：解析到条目为 ok，未解析到条目为 degraded
        # 这里刻意不把「0 条」当作 empty——因为无法区分
        # 「该栏目确实没有新政策」与「选择器失效了」。
        # 标记为 degraded 会触发人工确认，避免漏抓被伪装成正常。
        #
        # 注意判断用的是 cleaned 而非 docs：如果所有条目都被判为导航链接，
        # 那同样说明抓取逻辑出了问题（正常栏目页不可能全是导航），
        # 应当降级而不是「成功地」返回空结果。
        if not filtered and not cleaned:
            # 收集具体的失败原因。
            # 子类在解析过程中可能已经写入了更精确的说明（如「结果质量自检未通过：
            # 日期字段映射可能已失效」），这类信息对排查问题极有价值。
            # 如果被通用文案覆盖掉，运维就只能看到一个模糊的「疑似页面结构变更」，
            # 不得不再花时间复现一次问题——这是本项目的实际教训。
            detail = getattr(self, "_last_error", None) or "未解析到任何条目，疑似页面结构变更或页面为 JS 渲染"
            # 返回降级结果，附上具体原因
            return FetchResult(
                source_id=self.source.id,                                        # 源标识
                status="degraded",                                               # 降级状态
                docs=[],                                                         # 空结果
                error=detail,                                                    # 具体原因说明
                fetched_at=fetched_at,                                           # 抓取时间
                request_count=self._request_count,                               # 请求数
                dropped_navigation=dropped_nav,                                  # 剔除的导航链接数
                dropped_by_filter=dropped_filter,                                # 关键词过滤丢弃数
                filter_drop_reasons=dict(filter_reasons),                        # 丢弃原因明细
                dropped_filter_docs=dropped_docs,                                # 被丢弃条目明细（供复核）
            )

        # 正常情况
        return FetchResult(
            source_id=self.source.id,        # 源标识
            status="ok",                     # 正常状态
            docs=filtered,                   # 处理后的条目
            fetched_at=fetched_at,           # 抓取时间
            request_count=self._request_count,  # 请求数
            dropped_navigation=dropped_nav,  # 剔除的导航链接数
            dropped_by_filter=dropped_filter,  # 关键词过滤丢弃数
            filter_drop_reasons=dict(filter_reasons),  # 丢弃原因明细
            dropped_filter_docs=dropped_docs,  # 被丢弃条目明细（供复核）
        )

    def _collect(self) -> tuple[list[RawDoc], str]:
        """请求并解析列表页，返回 ``(条目列表, 首个列表页HTML)``。

        基类实现只抓第一页；有分页需求的子类覆写此方法。
        """
        # 请求列表页
        response = self.get(self.source.list_url or "")
        # 请求失败时抛出异常，由 fetch 统一转成 failed
        if response is None:
            # 抛出带上下文的异常
            raise RuntimeError(f"列表页请求失败：{self.source.list_url}（{getattr(self, '_last_error', '未知原因')}）")
        # 解析条目
        docs = self.parse(response.text)
        # 返回条目与原始 HTML
        return docs, response.text

    @abc.abstractmethod
    def parse(self, html: str) -> list[RawDoc]:
        """从列表页 HTML 中解析出条目。由子类实现。"""
        # 抽象方法，子类必须实现
        raise NotImplementedError

    # --------------------------------------------------------
    # 供子类使用的解析辅助工具
    # --------------------------------------------------------

    def _absolutize(self, href: str) -> str:
        """把可能为相对路径的链接补全为绝对链接。

        政府网站的列表页常常返回 ``/2026-06/18/content_123.htm`` 这类相对路径，
        若不做补全，存进数据库的链接就是无法访问的。
        """
        # 以列表页地址为基准补齐
        return urljoin(self.source.list_url or "", href)

    def _resolve_selector(self, key: str, default: str | None = None) -> str | None:
        """从数据源配置中取出指定选择器。"""
        # selectors 可能为 None
        selectors = self.source.selectors or {}
        # 取出对应键的值，缺失时用默认值
        return selectors.get(key, default)


# ============================================================
# 条目清洗公共工具
# ============================================================

def dedupe_by_url(docs: Iterable[RawDoc]) -> list[RawDoc]:
    """按链接去重，保持首次出现的顺序。

    为什么按链接而非标题去重：政府网站常有同名文件（如多个年份的
    「通知」），但链接是唯一的。按标题去重会误删真实的不同文件。

    为什么放在基类而不是各子类：去重是「每个抓取器都必须做的事」。
    曾经 PbcFetcher 因为直接继承基类、复用了基类的单页 _collect，
    又不带子类的去重逻辑，导致同一条公告在结果里出现三次。
    把它上收为基类行为，就不会再有子类忘记实现。
    """
    # 已见过的链接集合，用于 O(1) 判重
    seen: set[str] = set()
    # 结果容器
    unique: list[RawDoc] = []
    # 逐条处理，顺序遍历保证「首次出现」的顺序优先
    for doc in docs:
        # 已见过该链接则跳过
        if doc.url in seen:
            # 跳过重复项
            continue
        # 登记链接
        seen.add(doc.url)
        # 保留该条
        unique.append(doc)
    # 返回去重结果
    return unique


def _url_path_depth(url: str) -> int:
    """返回 URL 的路径层数，用于判断链接是否比列表页更深。

    例：``http://a.cn/x/y/index.html`` 的路径层数为 3（x / y / index.html）。

    为什么要「层数」而不是直接比较字符串：政府网站的链接有绝对与相对
    两种写法，层数是一个与写法无关的稳定信号。
    """
    # 空值没有层数
    if not url:
        # 返回 0
        return 0
    # 去掉查询串与锚点，它们不影响路径层数
    path = url.split("?", 1)[0].split("#", 1)[0]
    # 切掉协议头，避免 ``http:`` 里的双斜杠被算成层
    if "://" in path:
        # 只保留域名之后的部分
        path = path.split("://", 1)[1]
    # 去掉域名部分
    path = path.split("/", 1)[1] if "/" in path else ""
    # 按斜杠切分并去掉空片段（连续的斜杠会产生空片段）
    segments = [seg for seg in path.split("/") if seg]
    # 返回层数
    return len(segments)


# 判定为栏目导航链接的标题长度上限。
# 取 12 是因为政府文件标题极少短于这个长度：
# 常见的栏目名如「金融科技」「政策法规」「统计数据」都在 4-6 字，
# 而最短的真实政策名（如「中国人民银行公告〔2026〕第24号」）也有 16 字以上。
NAV_TITLE_MAX_LEN = 12


def drop_probable_navigation(docs: Iterable[RawDoc], list_url: str) -> list[RawDoc]:
    """剔除疑似「栏目导航链接」的条目。

    为什么需要这一步
    ----------------
    列表页里常混有指向其他栏目的导航链接（如人行「规范性文件」列表页里的
    「金融科技」栏目入口）。这类链接有三个特征：没有发布日期、
    链接层数不比列表页更深、标题很短。

    危险之处在于：如果某个栏目名恰好含有关键词（如「金融科技」含「科技」），
    它就会骗过关键词过滤被当成一条政策收录，而且流水线报告 status=ok。
    这是本项目实测遇到的第二种「静默污染」，第一种见 json_search_list.py 的说明。

    判定规则（三个条件同时满足才剔除）
    ----------------------------------
    1. **没有发布日期**。政策条目几乎总有日期，这是最强的否定信号。
    2. **链接不比列表页更深**。栏目导航指向的是另一棵目录树的入口，
       而政策详情页必然比列表页多一层文档标识。
    3. **标题较短**（不超过 ``NAV_TITLE_MAX_LEN`` 字）。政策标题通常是
       十几个字的长句，栏目名则简短。

    三条同时满足才剔除，是刻意保守的设计：宁可漏掉一两个导航链接
    留给人工筛，也不要因为规则过宽而误删真实政策——后者是不可逆的损失。
    """
    # 列表页自身的路径层数，作为「更深」的比较基准
    base_depth = _url_path_depth(list_url)
    # 结果容器
    kept: list[RawDoc] = []
    # 逐条判断
    for doc in docs:
        # 三个条件同时满足即判定为栏目导航链接
        is_navigation = (
            doc.published_on is None                      # 条件 1：没有发布日期
            and _url_path_depth(doc.url) <= base_depth    # 条件 2：链接不比列表页更深
            and len(doc.title) <= NAV_TITLE_MAX_LEN       # 条件 3：标题较短
        )
        # 是导航链接则剔除
        if is_navigation:
            # 跳过该条，不加入结果
            continue
        # 保留该条
        kept.append(doc)
    # 返回保留结果
    return kept


# ============================================================
# 关键词过滤
# ============================================================

# 丢弃原因的稳定键名。用常量而非裸字符串，是为了让统计与测试断言有确定的取值。
DROP_REASON_EXCLUDED = "excluded"        # 命中 exclude_keywords 被排除
DROP_REASON_NOT_MATCHED = "not_matched"  # 未命中任何 include_keywords


def filter_docs_with_reasons(
    docs: Iterable[RawDoc], filters: dict[str, Any] | None
) -> tuple[list[RawDoc], dict[str, int]]:
    """按关键词规则过滤条目，返回「保留结果」与「丢弃原因计数」。

    为什么要把丢弃数量单独返回
    --------------------------
    本函数的前身只返回保留结果，被丢弃的条目数无从知晓。而「关键词没命中」
    正是本项目最需要警惕的一类静默丢失：一份标题里没有「人工智能」「数据」
    等词、正文却含 AI 条款的文件，会被这里悄悄丢掉，抓取报告依旧显示 ok，
    没有任何地方会提示有人被丢掉了。

    这与 ``dropped_navigation`` 是同一条原则：**清洗只要不可见，
    就会变成新的静默风险。** 把丢弃数与原因暴露出来，运维看到异常数字
    （如某源今天丢了 80 条）才能立刻警觉。

    区分两种丢弃原因的价值：``excluded`` 多说明排除词写得太宽，
    ``not_matched`` 多说明包含词写得太窄——两者要采取的行动完全相反。

    过滤规则的设计原则（沿用原实现）：
    1. **包含规则宽松**：宁可多留几条让人工筛掉，也不要漏掉重要政策
    2. **排除规则精确**：只排除确定无关的类别（如行政处罚决定书）

    返回 ``(保留的条目, {原因: 条数})``；未配置过滤规则时原因字典为空。
    """
    # 丢弃原因计数，形如 {"not_matched": 12}
    reasons: dict[str, int] = {}

    # 无过滤配置时原样返回，不做任何丢弃
    if not filters:
        # 转成列表后返回，原因字典保持为空
        return list(docs), reasons

    # 取出包含关键词列表。过滤掉空值——空字符串会让 `"" in title` 恒为真，
    # 导致整源全量保留，是最容易被忽略的一种配置失误。
    include = [k for k in (filters.get("include_keywords") or []) if k]
    # 取出排除关键词列表，同样过滤空值
    exclude = [k for k in (filters.get("exclude_keywords") or []) if k]

    # 结果容器
    kept: list[RawDoc] = []
    # 逐条判断
    for doc in docs:
        # 标题文本；标题为 None 时按空串处理，避免在其上做子串判断
        title = doc.title or ""
        # 排除规则优先：命中任一排除词即丢弃
        if any(word in title for word in exclude):
            # 累计「被排除」计数后跳过该条
            reasons[DROP_REASON_EXCLUDED] = reasons.get(DROP_REASON_EXCLUDED, 0) + 1
            # 进入下一条
            continue
        # 未配置包含规则时全部保留
        if not include:
            # 保留
            kept.append(doc)
            # 进入下一条
            continue
        # 命中任一包含词即保留
        if any(word in title for word in include):
            # 保留
            kept.append(doc)
            # 进入下一条
            continue
        # 既未被排除、也未命中任何包含词——这是最需要警惕的一类丢弃，
        # 因为它可能丢掉标题不含 AI 字样但正文含 AI 条款的文件
        reasons[DROP_REASON_NOT_MATCHED] = reasons.get(DROP_REASON_NOT_MATCHED, 0) + 1

    # 返回保留结果与丢弃原因计数
    return kept, reasons


def apply_filters(docs: Iterable[RawDoc], filters: dict[str, Any] | None) -> list[RawDoc]:
    """按关键词规则过滤条目，只返回保留结果。

    保留这个薄封装是为了不破坏既有调用方；实现委托给
    ``filter_docs_with_reasons``，避免同一套规则存在两份实现各自演化。
    需要知道丢弃了多少条、为什么丢时，请改用后者。
    """
    # 只取保留结果，忽略原因计数
    kept, _reasons = filter_docs_with_reasons(docs, filters)
    # 返回保留结果
    return kept
