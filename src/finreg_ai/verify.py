"""本地核验取证层 —— 为人工核验草稿产出「证据卡」。

为什么需要这个模块
------------------
B 线建立了 ``data/drafts/``：一批未经人工核验的候选政策记录。
把草稿提升为正式记录（``data/policies/``）需要人逐项核对官方原文，
纯手工做法是在浏览器和 YAML 编辑器之间来回切换——既慢，又不留痕。

本模块把这件事拆成两半：

1. **机器取证**（本模块）：抓取 ``source.url`` 的官方页面，
   按四个检查层产出「核验证据卡」（``EvidenceCard``）。
   机器只做**取证**，绝不做**认定**——它回答「页面上写了什么」，
   不回答「这条记录对不对」。

2. **人工认定**（界面层，见 ``console.py``）：人看着证据卡点按钮做判断，
   每一次点击都留痕（见 ``promote.py`` 的核验台账）。

四个检查层
----------
① 可达性：状态码非 200 → 红；被重定向到站点首页（政府站删文的典型形态）
   → 红；正文哈希与记录不符 → 黄「页面已变」。
② 标题比对：页面 ``<title>`` 与记录标题做相似度比对，低于阈值 → 红。
③ 日期与状态取证：全文检索「施行/生效/废止/修订/之日起」并摘句；
   正则提取「自 X 年 X 月 X 日起施行」给出候选生效日期；
   草稿填了日期而全文 0 命中 → 红「疑似编造日期」；留空而有命中 → 黄「可补」。
④ 义务清单核对：按条款号定位原段落；定位失败 → 红；
   概括与原段重合度低于阈值 → 红；情态词漂移（原文「鼓励」却标 mandatory）
   → 红。

铁律：明确失败优于静默错误
--------------------------
任一检查无法执行（例如页面是 JS 渲染的空壳、取不到正文），
该层的结果必须标为 ``manual``（「无法自动取证，需人工核」），
**不得当作通过**。一张全绿的证据卡与一张「取不到证据」的卡，
在界面上必须一眼可辨——前者可以放心入库，后者每一层都要人表态。
"""

# 导入 difflib 用于标题与义务概括的相似度计算（标准库，零新增依赖）
import difflib
# 导入 re 用于日期正则与条款号定位
import re
# 导入 dataclass 定义证据卡数据结构
from dataclasses import dataclass, field
# 导入 date 用于候选生效日期的构造
from datetime import date
# 导入 Path 用于定位草稿目录
from pathlib import Path
# 导入 urlparse 用于判断「重定向到站点首页」
from urllib.parse import urlparse
# 导入 Any 用于类型标注
from typing import Any

# 导入 requests 发起真实 HTTP 请求（测试时注入 FakeSession 替代）
import requests
# 导入 requests 的异常基类以便精确捕获网络层错误
from requests.exceptions import RequestException
# 导入 BeautifulSoup 做 HTML 容错解析（与抓取器同一套技术栈，无新依赖）
from bs4 import BeautifulSoup

# 从模型层导入政策对象类型与警告级别判定
from finreg_ai.models import Policy, is_warning
# 从抓取器基类复用：默认 UA、请求超时、北京时间工具
from finreg_ai.fetchers.base import (
    DEFAULT_TIMEOUT,       # 请求超时秒数
    DEFAULT_USER_AGENT,    # 善意 UA（含项目主页联系方式）
    now_china_iso,         # 北京时间 ISO 时间戳
)
# 从存取层复用：正文归一化与哈希计算（与变更检测同一口径）
from finreg_ai.store import (
    PROJECT_ROOT,          # 项目根目录
    compute_hash,          # 正文 SHA-256
    load_policy_file,      # 加载并校验单个政策 YAML（草稿同构）
    normalize_text,        # 正文归一化
)

# 草稿目录。与 policies 平级而非放进去：草稿不是合规依据，
# 任何按 policies 目录遍历的代码都不应看到它们。
DRAFTS_DIR = PROJECT_ROOT / "data" / "drafts"


# ============================================================
# 检查结果的状态取值
# ============================================================
#
# 为什么用四个状态而不是布尔「过/不过」：
# 「机器确认没问题」与「机器取不到证据」是两种完全不同的东西。
# 若合并成「没有红项」，JS 空壳页会产出一张「全绿」的证据卡——
# 那正是本项目一直在对抗的静默错误。
STATUS_OK = "ok"            # 机器通过（有明确证据支持）
STATUS_WARNING = "warning"  # 黄：需要人判断（如页面已变、日期可补）
STATUS_ERROR = "error"      # 红：证据与记录冲突（如标题不符、疑似编造日期）
STATUS_MANUAL = "manual"    # 无法自动取证，需人工核（如 JS 空壳页）

# 状态的中文标签，供 CLI 与界面显示
STATUS_LABELS = {
    STATUS_OK: "机器通过",          # 绿
    STATUS_WARNING: "待判断",       # 黄
    STATUS_ERROR: "证据冲突",       # 红
    STATUS_MANUAL: "需人工核",      # 灰
}


# ============================================================
# 页面抓取结果
# ============================================================

@dataclass
class PageFetch:
    """一次官方页面抓取的结果，供四个检查层消费。

    刻意与 ``FetchResult`` 分开：抓取流水线的结果面向「列表页发现条目」，
    这里面向「单页取证」——关心的东西完全不同（最终 URL、是否被
    重定向回首页、正文文本），共用一套结构会两边都迁就。
    """

    # 请求是否拿到了可用响应（False 时各检查层应标 manual 或 error）
    ok: bool
    # HTTP 状态码；请求根本没发出时为 None
    status_code: int | None = None
    # 最终 URL（跟随重定向后）；与请求 URL 不同说明发生了跳转
    final_url: str | None = None
    # 是否被重定向到了站点首页——政府网站删文后的典型形态
    redirected_to_home: bool = False
    # 页面 HTML 文本（已按探测编码解码）；失败时为 None
    html: str | None = None
    # 失败原因说明；成功时为 None
    error: str | None = None


# 判断「最终 URL 是站点首页」的路径白名单。
# 政府网站把已删除的文章 301/302 回栏目首页或站点首页是常见做法；
# 这些路径形态表示「已经不在任何一篇文章页上了」。
_HOME_PATHS = {"", "/", "/index.html", "/index.htm", "/index.shtml", "/index.php"}


def _is_home_path(url: str) -> bool:
    """判断 URL 是否指向站点首页（而非具体内容页）。"""
    # 解析出路径部分并去掉查询串的影响（urlparse 已分离）
    path = urlparse(url).path or "/"
    # 归一化尾部斜杠后比对白名单
    return path.rstrip("/").lower() in {"", "index.html", "index.htm", "index.shtml", "index.php"} or path in _HOME_PATHS


def _build_session() -> requests.Session:
    """构造真实抓取用的会话。

    与 ``BaseFetcher._build_session`` 保持同一套 UA 与头——
    核验取证同样是「对监管机构服务器的善意低频访问」，
    UA 里的项目主页联系方式必须保留（爬虫礼仪，见 base.py 的说明）。
    """
    # 新建会话
    session = requests.Session()
    # 设置默认请求头
    session.headers.update(
        {
            # 浏览器标识 + 真实身份声明（必须纯 ASCII，原因见 base.py）
            "User-Agent": DEFAULT_USER_AGENT,
            # 声明接受 HTML
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            # 声明接受中文，避免返回英文版
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )
    # 返回配置好的会话
    return session


def fetch_page(url: str, session: Any | None = None, *, timeout: int = DEFAULT_TIMEOUT) -> PageFetch:
    """抓取官方页面一次，返回取证用的结构化结果。

    为什么不复用 ``BaseFetcher.get``：那个方法是「列表页抓取」的语义，
    绑在 ``Source`` 配置对象上（限速、重试、状态码处理都为流水线设计）。
    核验取证是单页、按需、由人触发的，重试与限速都应更简单——
    人坐在屏幕前等结果，静默重试三次 20 秒是不可接受的体验。

    参数 session 允许注入伪造会话（测试离线运行）；
    为 None 时构造真实会话。
    """
    # 未注入会话时构造真实会话
    client = session or _build_session()
    try:
        # 发起 GET，跟随重定向（requests 默认行为）——
        # 我们需要知道「最终落在了哪个 URL」来识别「被删文后跳回首页」
        response = client.get(url, timeout=timeout)
    except RequestException as exc:
        # 网络层失败：明确报错，绝不返回「空页面」冒充成功
        return PageFetch(ok=False, error=f"{type(exc).__name__}: {exc}")

    # 取出最终 URL。真实 requests.Response 一定有 .url；
    # 伪造响应可能没有，此时退回请求 URL（表示未发生跳转）
    final_url = getattr(response, "url", None) or url

    # 判断是否「被重定向回站点首页」。
    # 两个条件缺一不可：URL 确实变了（不是同源规范化），
    # 且终点是首页形态。只看「URL 变了」会把 http→https 这类
    # 无害跳转误报成删文。
    redirected_home = final_url != url and _is_home_path(final_url)

    # 显式设置编码：中国政府网站常不声明 charset 或声明错误，
    # 与抓取器一样优先信任内容探测（见 base.py 的说明）
    apparent = getattr(response, "apparent_encoding", None)
    # 探测到编码就用探测结果，否则用响应声明的编码
    response.encoding = apparent or getattr(response, "encoding", None) or "utf-8"

    # 组装成功结果（状态码是否 200 由检查层判断，这里如实记录）
    return PageFetch(
        ok=True,                                # 拿到了响应
        status_code=response.status_code,       # HTTP 状态码
        final_url=final_url,                    # 最终 URL
        redirected_to_home=redirected_home,     # 是否被跳回首页
        html=response.text,                     # 解码后的 HTML
    )


# ============================================================
# 页面文本提取
# ============================================================

# 判定「页面是 JS 渲染空壳」的正文长度下限。
# 一篇政策正文的纯文本至少几百字；取出的文本低于此值，
# 说明内容很可能要靠 JavaScript 渲染（本项目零浏览器依赖，取不到）。
# 此时各检查层必须标 manual，而不是对着一段导航文字「验证通过」。
MIN_BODY_TEXT_LEN = 200


@dataclass
class PageContent:
    """从 HTML 中提取出的取证素材。"""

    # 页面 <title> 文本；无 <title> 时为 None
    title: str | None
    # 页面全部可见文本（脚本与样式已剔除）
    body_text: str
    # 正文是否可用（长度达到下限）；False 时各检查层应标 manual
    usable: bool


def extract_content(html: str) -> PageContent:
    """从 HTML 中提取标题与正文文本。

    与抓取器共用 BeautifulSoup + lxml 技术栈（已是项目依赖，无新增）。
    刻意提取「全页可见文本」而不是按选择器找正文容器：
    各监管站点正文容器结构千差万别，为核验台维护一套选择器表
    是新的失效点；全文检索对「日期取证」「条款定位」已经足够，
    噪声（导航文字）只会稀释相似度，不会制造假命中——
    而相似度不足的后果是标 manual 让人核，方向是安全的。
    """
    # 用 lxml 后端解析（容错性好，与抓取器一致）
    soup = BeautifulSoup(html, "lxml")
    # 剔除脚本与样式节点——它们的文本不是正文，会污染关键词检索
    for tag in soup(["script", "style"]):
        # 从树中移除
        tag.decompose()
    # 提取 <title>；取不到时是 None（标题检查层会标 manual）
    title_tag = soup.find("title")
    # 取出标题文本并压平空白
    page_title = title_tag.get_text(strip=True) if title_tag else None
    # 提取全页可见文本，按换行连接，便于后续按句切分
    body_text = soup.get_text("\n")
    # 归一化（去空行、去行首尾空白），与哈希计算同口径
    body_text = normalize_text(body_text)
    # 组装结果
    return PageContent(
        title=page_title or None,                       # 空串视为无标题
        body_text=body_text,                            # 归一化后的正文
        usable=len(body_text) >= MIN_BODY_TEXT_LEN,     # 是否达到可用长度
    )


# ============================================================
# 中文数字处理 —— 法律文本的日期与条款号常写中文数字
# ============================================================

# 中文数字字符到数值的映射（〇与零都出现，都收）
_CN_DIGIT = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_to_int(text: str) -> int | None:
    """把中文数字串转成整数，支持「十」的组合（十一、二十、二十一）。

    覆盖到 99 已足够：条款号与月、日都不会超过它；
    年份是逐位写法（二〇二一），走另一条分支。
    无法解析时返回 None 而不是猜——猜错的条款号会让义务定位
    「看起来成功」，那比定位失败更糟。
    """
    # 去空白
    text = text.strip()
    # 空串无法解析
    if not text:
        # 返回失败
        return None
    # 含「十」的组合形式
    if "十" in text:
        # 按十分割为「几十」与「几」两段
        left, _, right = text.partition("十")
        # 十位：左侧为空表示「十几」（十 = 一十）
        tens = _CN_DIGIT.get(left, 1) if left else 1
        # 个位：右侧为空表示「几十」
        ones = _CN_DIGIT.get(right, 0) if right else 0
        # 任一段含非数字字符时 _CN_DIGIT.get 的默认值会掩盖错误，需显式校验
        if (left and left not in _CN_DIGIT) or (right and right not in _CN_DIGIT):
            # 有无法识别的字符
            return None
        # 组合求值
        return tens * 10 + ones
    # 逐位写法（如年份「二〇二一」）：每位独立
    digits: list[int] = []
    # 逐字符转换
    for ch in text:
        # 不在映射中即失败
        if ch not in _CN_DIGIT:
            # 返回失败
            return None
        # 收集该位
        digits.append(_CN_DIGIT[ch])
    # 逐位拼成整数
    value = 0
    # 逐位进位
    for d in digits:
        # 左移一位十进制并加上当前位
        value = value * 10 + d
    # 返回结果
    return value


def _int_to_cn(value: int) -> str | None:
    """把 1-99 的整数转成中文数字（条款号形态：一、十、十六、二十一）。

    只支持 1-99：条款号的现实范围。
    超出范围返回 None，调用方退化为只用阿拉伯数字匹配。
    """
    # 数字到字符的反向映射（不用「两」「零」，条款号不用它们）
    digits = "零一二三四五六七八九"
    # 范围校验
    if not 1 <= value <= 99:
        # 超出支持范围
        return None
    # 小于十：直接查表
    if value < 10:
        # 返回单字
        return digits[value]
    # 整十：十、二十……
    if value % 10 == 0:
        # 十位字 + 十；十本身不写「一十」
        return ("十" if value == 10 else digits[value // 10] + "十")
    # 十几：十六（不写「一十六」）
    if value < 20:
        # 十 + 个位
        return "十" + digits[value % 10]
    # 几十几：二十一
    return digits[value // 10] + "十" + digits[value % 10]


# ============================================================
# 检查层 ①：可达性
# ============================================================

def check_reachability(policy: Policy, page: PageFetch, content: PageContent | None) -> list["CheckResult"]:
    """检查层一：页面可达性与内容稳定性。

    产出一条检查结果。返回列表是为了与其他检查层统一接口
    （义务核对层一条义务产一条结果）。
    """
    # 检查项的稳定标识，供界面按钮与台账引用
    check_id = "reachability"
    # 请求本身失败：这不是「记录有错」，而是「取不到证据」——
    # 但页面打不开意味着无法核验任何字段，标红并给出人工确认选项
    if not page.ok:
        # 返回红项：无法访问
        return [CheckResult(
            check_id=check_id,                      # 检查项标识
            layer="可达性",                          # 所属检查层
            status=STATUS_ERROR,                    # 红
            summary=f"官方页面无法访问：{page.error}",  # 结论
            evidence=[],                            # 无页面证据可摘
            options=[CheckOption(                   # 判断选项：人工已打开页面确认
                key="confirm-accessible",           # 选项键
                label="我已人工打开页面，确认可以访问",  # 按钮文字
                action={"kind": "acknowledge", "field": "reachability"},  # 动作：人工确认
            )],
        )]
    # 状态码非 200：官方页面异常
    if page.status_code != 200:
        # 返回红项：状态码异常
        return [CheckResult(
            check_id=check_id,
            layer="可达性",
            status=STATUS_ERROR,
            summary=f"官方页面返回 HTTP {page.status_code}（期望 200）",
            evidence=[f"最终 URL：{page.final_url}"],
            options=[CheckOption(
                key="confirm-status",
                label="我已人工打开页面，确认内容存在",
                action={"kind": "acknowledge", "field": "reachability"},
            )],
        )]
    # 被重定向回站点首页：删文的典型形态，必须标红
    if page.redirected_to_home:
        # 返回红项：疑似已下架
        return [CheckResult(
            check_id=check_id,
            layer="可达性",
            status=STATUS_ERROR,
            summary="页面被重定向到站点首页 —— 这是政府网站删除文章后的典型形态，原文可能已下架",
            evidence=[f"请求：{policy.source.url}", f"落点：{page.final_url}"],
            options=[CheckOption(
                key="confirm-content-exists",
                label="我已人工打开页面，确认原文仍在",
                action={"kind": "acknowledge", "field": "reachability"},
            )],
        )]
    # 正文哈希比对：记录里存了哈希且页面正文可用时才比。
    # 记录没存哈希（草稿阶段普遍如此）不是「不一致」，跳过即可——
    # 把「没有基准」说成「哈希一致」是制造假证据。
    record_hash = policy.source.content_hash
    # 三个条件：有基准哈希、页面文本可用、两者不同 → 黄「页面已变」
    if record_hash and content is not None and content.usable:
        # 计算当前页面正文的哈希（与变更检测同一口径）
        current_hash = compute_hash(content.body_text)
        # 与记录基准比对
        if current_hash != record_hash:
            # 返回黄项：页面内容已变化
            return [CheckResult(
                check_id=check_id,
                layer="可达性",
                status=STATUS_WARNING,
                summary="页面正文与记录时的哈希不同 —— 官方可能在未发通知的情况下修改了内容",
                evidence=[f"记录哈希：{record_hash}", f"当前哈希：{current_hash}"],
                options=[CheckOption(
                    key="confirm-change",
                    label="我已人工比对，内容变化不影响本记录字段",
                    action={"kind": "acknowledge", "field": "reachability"},
                )],
            )]
    # 全部通过：绿
    return [CheckResult(
        check_id=check_id,
        layer="可达性",
        status=STATUS_OK,
        summary="页面可访问（HTTP 200，未发生异常重定向）",
        evidence=[f"最终 URL：{page.final_url}"],
    )]


# ============================================================
# 检查层 ②：标题比对
# ============================================================

# 标题相似度阈值。取 0.9 的理由：政府网站 <title> 常带站点后缀
# （「_中国政府网」「 - 中国人民银行」），清理后仍可能有少量差异；
# 但低于 0.9 的差异通常意味着「这根本不是同一篇文章」。
TITLE_SIMILARITY_THRESHOLD = 0.9

# 标题后缀的常见分隔符。政府网站的 <title> 几乎都是
# 「文章标题<分隔符>站点名」的形态。
_TITLE_SEPARATORS = ("_", "—", "-", "|", "－")

# 发文字号片段（如「〔2020〕第9号」「〔2026〕3号」）。
# 政府站 <title> 常把文号嵌在标题中间或后面，
# 不剥掉的话「同一篇文件」的相似度会被无谓拉低。
_DOC_NUMBER_PATTERN = re.compile(r"〔[^〕]*〕\s*第?\s*\d+\s*号?")


def _title_candidates(page_title: str) -> list[str]:
    """把页面 <title> 拆成候选标题（去站点后缀、书名号、文号的各种可能）。

    为什么候选要这么宽：实测 11 条真实草稿里 8 条标题层误红，
    绝大多数不是「链接指错了」，而是政府站 <title> 形态太多——
    「关于印发《X》的通知」「X（文号）_站点名」「站点名：X」。
    比对只看「记录标题是否作为整体出现」，形态差异不该变成红项。
    """
    # 基底变体：原始标题 + 剥掉书名号与文号的标题
    bases = [page_title.strip()]
    # 剥书名号与文号
    stripped = _DOC_NUMBER_PATTERN.sub("", page_title).replace("《", "").replace("》", "").strip()
    # 非空且不同才补进来
    if stripped and stripped != bases[0]:
        # 补一个基底
        bases.append(stripped)
    # 候选集合
    candidates: list[str] = []
    # 对每个基底再按分隔符取第一段
    for base in bases:
        # 基底本身总在列
        candidates.append(base)
        # 按各分隔符切分
        for sep in _TITLE_SEPARATORS:
            # 含分隔符时补「第一段」
            if sep in base:
                # 取分隔符前
                candidates.append(base.split(sep, 1)[0].strip())
    # 去掉空候选并去重（保持顺序）
    return list(dict.fromkeys(c for c in candidates if c))


def check_title(policy: Policy, content: PageContent | None) -> list["CheckResult"]:
    """检查层二：页面标题与记录标题的一致性。

    通过路径有三条（任一成立即绿，从严到宽）：
    1. 归一化后互相包含（剥掉文号/书名号/站点后缀后是一回事）；
    2. 归一化相似度达到阈值；
    3. 记录标题完整出现在页面正文里——<title> 写得再怪，
       正文含完整标题就是同一篇文件的铁证。
    """
    # 检查项标识
    check_id = "title"
    # 页面取不到或正文不可用：标题比对无法进行。
    # 注意：这里看的是 content 是否提取成功——<title> 缺失
    # 在技术上仍可比对（标 manual），混为「不一致」是冤枉记录。
    if content is None or content.title is None:
        # 标 manual：无法取证
        return [CheckResult(
            check_id=check_id,
            layer="标题比对",
            status=STATUS_MANUAL,
            summary="页面未提供可比对的 <title> —— 无法自动取证，需人工核",
            evidence=[],
            options=[CheckOption(
                key="confirm-title",
                label="我已人工核对，页面标题与记录一致",
                action={"kind": "acknowledge", "field": "title"},
            )],
        )]
    # 归一化记录标题（压掉空白与标点，见 _squash 的说明）
    record_core = _squash(policy.title)
    # 归一化各候选
    candidates = [_squash(c) for c in _title_candidates(content.title)]
    # 路径一：互相包含——「关于印发《X》的通知」「X_站点名」都走这里
    if record_core and any(record_core in cand or (cand and cand in record_core) for cand in candidates):
        # 返回绿项
        return [CheckResult(
            check_id=check_id,
            layer="标题比对",
            status=STATUS_OK,
            summary="页面标题经归一化后与记录一致（互相包含）",
            evidence=[f"页面 <title>：{content.title}"],
        )]
    # 路径二：归一化相似度达阈值
    best = max(
        (difflib.SequenceMatcher(None, cand, record_core).ratio() for cand in candidates),
        default=0.0,
    )
    # 达到阈值：绿
    if best >= TITLE_SIMILARITY_THRESHOLD:
        # 返回绿项
        return [CheckResult(
            check_id=check_id,
            layer="标题比对",
            status=STATUS_OK,
            summary=f"页面标题与记录一致（相似度 {best:.2f}）",
            evidence=[f"页面 <title>：{content.title}"],
        )]
    # 路径三：记录标题完整出现在正文里——<title> 是站名也别冤枉链接
    if record_core and record_core in _squash(content.body_text):
        # 返回绿项，但如实说明命中位置
        return [CheckResult(
            check_id=check_id,
            layer="标题比对",
            status=STATUS_OK,
            summary="页面 <title> 未命中，但记录标题完整出现在页面正文 —— 同一篇文件",
            evidence=[f"页面 <title>：{content.title}"],
        )]
    # 三条都不成立时，仍要先区分「取证失败」与「证据冲突」（与日期层同理）：
    # 页面不是全文页（空壳/栏目页），标题对不上只说明「取不到证据」，
    # 不能据此指控「指向了另一篇文章」。实测修复前两轮出证里
    # 6 条草稿的标题红全部属于这一类。
    if not _page_contains_document(policy, content):
        # 返回需人工核
        return [CheckResult(
            check_id=check_id,
            layer="标题比对",
            status=STATUS_MANUAL,
            summary=(
                "页面正文不含该文件的全文特征（可能为 JS 渲染或栏目页），"
                "无法比对标题 —— 无法自动取证，需人工核"
            ),
            evidence=[f"记录标题：{policy.title}", f"页面 <title>：{content.title}"],
            options=[CheckOption(
                key="confirm-title",
                label="我已人工核对，页面正文确为该文件",
                action={"kind": "acknowledge", "field": "title"},
            )],
        )]
    # 页面确为全文而标题对不上：红——很可能指向了另一篇文章。
    # 注意此时「全文」只可能由发文字号佐证（标题若已在正文出现，
    # 路径三就已通过），因此这个红同时提示「文号对但标题对不上」，
    # 通常意味着记录标题与公布名不一致，需要人改标题或改链接。
    return [CheckResult(
        check_id=check_id,
        layer="标题比对",
        status=STATUS_ERROR,
        summary=f"页面标题与记录不符（相似度 {best:.2f}，阈值 {TITLE_SIMILARITY_THRESHOLD}）—— 链接可能指向了另一篇文章，或记录标题与公布名不一致",
        evidence=[f"记录标题：{policy.title}", f"页面 <title>：{content.title}"],
        options=[
            CheckOption(
                key="confirm-title",
                label="我已人工核对，页面正文确为该文件",
                action={"kind": "acknowledge", "field": "title"},
            ),
        ],
    )]


# ============================================================
# 检查层 ③：日期与状态取证
# ============================================================

# 日期与状态的话题词。命中任一即把整句摘出作为证据。
_DATE_TOPIC_PATTERN = re.compile(r"施行|生效|废止|修订|之日起")

# 中文「月/日」的写法备选：阿拉伯数字、十以内单字（一）、
# 十几（十一）、几十（二十）、几十几（二十一）。
# 单独抽出来复用：早前「日」的备选漏了单字写法，
# 「十一月一日」因此整体匹配失败——中文数字的备选必须四种齐全。
_CN_NUM_FORM = r"(?:[0-9]{1,2}|十?[一二三四五六七八九]|[一二三]?十[一二三四五六七八九]?)"

# 候选生效日期正则：「自 X 年 X 月 X 日起施行/生效」。
# 年支持四位阿拉伯数字或四位中文数字（二〇二一）；
# 月、日支持阿拉伯数字或中文数字（十一、二十一）。
_EFFECTIVE_DATE_PATTERN = re.compile(
    r"自\s*"
    r"(?P<year>[0-9]{4}|[〇零一二三四五六七八九]{4})\s*年\s*"
    r"(?P<month>" + _CN_NUM_FORM + r")\s*月\s*"
    r"(?P<day>" + _CN_NUM_FORM + r")\s*日"
    r"\s*起?\s*(?:施行|生效)"
)

# 证据摘录的条数上限。全文检索在长篇法律（如个保法 74 条）上
# 可能命中几十句，全部放进证据卡会淹没真正重要的句子。
MAX_EVIDENCE_QUOTES = 8


def _squash(text: str) -> str:
    """压掉全部空白与标点，只留文字，用于「标题是否出现在正文」类包含判断。

    政府站正文里标题常被书名号、换行、空格拆开（《某办法》\n第一章……），
    不做这一步，「正文里明明有标题」会因为一个换行而判不出来。
    ``str.isalnum`` 对中日韩表意文字同样为真，因此中文被保留。
    """
    # 逐字符保留字母与数字（含中日韩文字）
    return "".join(ch for ch in text if ch.isalnum())


def _page_contains_document(policy: Policy, content: PageContent) -> bool:
    """判断页面正文是否确含该文件的全文特征（记录标题或发文字号）。

    为什么需要这个判断
    ------------------
    日期取证层的「全文 0 命中」只有在**页面真的是全文**时才有意义。
    实测踩过的坑：nfra 的 ItemDetail 页是 JS 空壳，提取出的「正文」
    是导航与页脚——长度足够通过 MIN_BODY_TEXT_LEN 的门槛，
    于是一份有充分原文依据的施行日期被标成红「疑似编造日期」。
    「取证失败」被偷换成了「证据冲突」，而这两者必须严格分开。

    判定依据取「标题或文号出现在正文里」：全文页必然包含它们，
    空壳页/栏目页几乎不可能恰好包含——方向是保守的
    （宁把真全文误判为「需人工核」，不把空壳误判为「全文」）。
    """
    # 压平后的正文（一次计算，两处复用）
    body = _squash(content.body_text)
    # 特征一：记录标题
    title_core = _squash(policy.title)
    # 标题出现在正文 → 全文页
    if title_core and title_core in body:
        # 判定为全文
        return True
    # 特征二：发文字号（如「主席令第九十一号」；无该字段时跳过）
    doc_number = getattr(policy, "doc_number", None)
    # 文号出现在正文 → 全文页
    if doc_number and _squash(doc_number) in body:
        # 判定为全文
        return True
    # 两个特征都不在 → 不能当作全文页
    return False


def _parse_year(text: str) -> int | None:
    """解析年份：四位阿拉伯数字或逐位中文数字。"""
    # 阿拉伯数字直接转
    if text.isdigit():
        # 返回整数年份
        return int(text)
    # 中文逐位写法（二〇二一）
    return _cn_to_int(text)


def _parse_month_day(text: str) -> int | None:
    """解析月或日：阿拉伯数字或中文组合数字。"""
    # 阿拉伯数字直接转
    if text.isdigit():
        # 返回整数
        return int(text)
    # 中文组合数字（十一、二十一）
    return _cn_to_int(text)


def find_effective_date_candidates(text: str) -> list[date]:
    """从全文中提取「自 X 年 X 月 X 日起施行」的候选生效日期。

    返回去重后的日期列表（按出现顺序）。只提取、不判断——
    「采纳哪一个」是人的决定，机器只负责把候选摆出来。
    """
    # 结果容器
    candidates: list[date] = []
    # 逐个匹配
    for match in _EFFECTIVE_DATE_PATTERN.finditer(text):
        # 解析年月日三段
        year = _parse_year(match.group("year"))
        # 月
        month = _parse_month_day(match.group("month"))
        # 日
        day = _parse_month_day(match.group("day"))
        # 任一段解析失败则跳过——宁可少一个候选，不造一个错日期
        if year is None or month is None or day is None:
            # 跳过该匹配
            continue
        try:
            # 构造真实日期（会拦住「2 月 30 日」这类不存在的日子）
            candidate = date(year, month, day)
        except ValueError:
            # 非法日期跳过
            continue
        # 去重追加
        if candidate not in candidates:
            # 追加
            candidates.append(candidate)
    # 返回候选列表
    return candidates


def find_status_sentences(text: str) -> list[str]:
    """把全文中含「施行/生效/废止/修订/之日起」的句子摘出。

    供人快速判断文件的状态线索（如「同时废止」「修订后重新公布」）。
    """
    # 按句切分（句号、分号、换行）
    sentences = [s.strip() for s in re.split(r"[。；\n]", text) if s.strip()]
    # 摘出命中话题词的句子，去重并限量
    hits: list[str] = []
    # 逐句检查
    for sentence in sentences:
        # 命中话题词
        if _DATE_TOPIC_PATTERN.search(sentence):
            # 去重追加
            if sentence not in hits:
                # 追加
                hits.append(sentence)
        # 达到上限即停——摘录的目的是让人扫一眼，不是替代原文
        if len(hits) >= MAX_EVIDENCE_QUOTES:
            # 停止收集
            break
    # 返回摘录
    return hits


def check_dates(policy: Policy, content: PageContent | None) -> list["CheckResult"]:
    """检查层三：生效日期与状态线索的取证。

    规则（来自需求规格，一字不改地实现）：
    - 草稿填了日期而全文 0 命中 → 红「疑似编造日期」
    - 草稿留空而有命中 → 黄「可补」
    """
    # 检查项标识
    check_id = "date"
    # 正文不可用：无法取证
    if content is None or not content.usable:
        # 标 manual
        return [CheckResult(
            check_id=check_id,
            layer="日期与状态",
            status=STATUS_MANUAL,
            summary="页面正文无法取得（可能为 JS 渲染）—— 无法自动取证，需人工核",
            evidence=[],
            options=[CheckOption(
                key="confirm-date",
                label="我已人工核对原文的施行/废止条款",
                action={"kind": "acknowledge", "field": "date"},
            )],
        )]
    # 取证：候选日期与状态句子
    candidates = find_effective_date_candidates(content.body_text)
    # 摘录状态句子
    quotes = find_status_sentences(content.body_text)
    # 话题词总命中数（用于「0 命中」判断，不受摘录上限影响）
    total_hits = len(_DATE_TOPIC_PATTERN.findall(content.body_text))

    # --- 情形一：草稿填了生效日期 ---
    if policy.effective_from is not None:
        # 全文 0 命中时分两种处置，区分「取证失败」与「证据冲突」：
        if total_hits == 0:
            # 页面不是全文页（空壳/栏目页）——0 命中只说明「取不到证据」，
            # 不能据此指控日期是编造的。降级为「需人工核」。
            # 这是实测踩过的坑：nfra 的 JS 空壳页导航文字凑够长度门槛，
            # 一份有原文依据的日期曾被误判为「疑似编造」。
            if not _page_contains_document(policy, content):
                # 返回需人工核
                return [CheckResult(
                    check_id=check_id,
                    layer="日期与状态",
                    status=STATUS_MANUAL,
                    summary=(
                        "页面正文不含该文件的全文特征（可能为 JS 渲染或栏目页），"
                        "无法确认日期表述 —— 无法自动取证，需人工核"
                    ),
                    evidence=[],
                    options=[CheckOption(
                        key="confirm-date",
                        label="我已人工核对原文的施行/废止条款",
                        action={"kind": "acknowledge", "field": "date"},
                    )],
                )]
            # 页面确为全文而 0 命中：填的日期在原文里找不到任何佐证
            # → 红「疑似编造日期」。这是本层最重要的一条规则：
            # 它拦的是「自动化整理倾向把字段填满」这一结构性风险
            # （本项目在 policies 数据上实测踩过，见 models.py 规则 11）。
            return [CheckResult(
                check_id=check_id,
                layer="日期与状态",
                status=STATUS_ERROR,
                summary=f"记录填写了生效日期 {policy.effective_from}，但官方页面全文检索不到任何施行/生效/废止表述 —— 疑似编造日期",
                evidence=[],
                options=[
                    CheckOption(
                        key="clear-date",
                        label="清空 effective_from（在核验说明中写明原因）",
                        action={"kind": "set-field", "field": "effective_from", "value": None},
                    ),
                    CheckOption(
                        key="keep-date",
                        label="保留日期（我已人工核对原文确有依据）",
                        action={"kind": "acknowledge", "field": "date"},
                    ),
                ],
            )]
        # 有命中：看候选日期里有没有与填写值一致的
        if policy.effective_from in candidates:
            # 绿：填写值有原文佐证
            return [CheckResult(
                check_id=check_id,
                layer="日期与状态",
                status=STATUS_OK,
                summary=f"生效日期 {policy.effective_from} 在原文「自 X 年 X 月 X 日起施行」句式中得到佐证",
                evidence=quotes,
            )]
        # 有命中但候选与填写值不一致：黄——可能是页面版本不同，也可能填错
        return [CheckResult(
            check_id=check_id,
            layer="日期与状态",
            status=STATUS_WARNING,
            summary=f"记录填写 {policy.effective_from}，但原文候选日期为 {', '.join(d.isoformat() for d in candidates) or '（未能解析）'}",
            evidence=quotes,
            options=[
                # 每个候选给一个采纳选项
                *[CheckOption(
                    key=f"adopt:{cand.isoformat()}",
                    label=f"改用候选日期 {cand.isoformat()}",
                    action={"kind": "set-field", "field": "effective_from", "value": cand.isoformat()},
                ) for cand in candidates],
                CheckOption(
                    key="keep-date",
                    label=f"维持 {policy.effective_from}（我已人工核对原文依据）",
                    action={"kind": "acknowledge", "field": "date"},
                ),
            ],
        )]

    # --- 情形二：草稿留空 ---
    # 留空而有命中：黄「可补」
    if candidates or total_hits > 0:
        # 返回黄项
        return [CheckResult(
            check_id=check_id,
            layer="日期与状态",
            status=STATUS_WARNING,
            summary="记录未填写生效日期，但原文存在施行/生效表述 —— 可补",
            evidence=quotes,
            options=[
                # 每个候选给一个采纳选项
                *[CheckOption(
                    key=f"adopt:{cand.isoformat()}",
                    label=f"采纳候选日期 {cand.isoformat()}",
                    action={"kind": "set-field", "field": "effective_from", "value": cand.isoformat()},
                ) for cand in candidates],
                CheckOption(
                    key="keep-blank",
                    label="维持留空（我已人工核对，原文无可确认的施行条款）",
                    action={"kind": "acknowledge", "field": "date"},
                ),
            ],
        )]
    # 留空且 0 命中，同样先区分「取证失败」与「确实没写」：
    # 页面不是全文页时，0 命中说明不了「原文没写」，
    # 若此时给绿「留空与原文一致」，等于空壳页自动放行——
    # 比误红更糟，这是静默通过。
    if not _page_contains_document(policy, content):
        # 返回需人工核
        return [CheckResult(
            check_id=check_id,
            layer="日期与状态",
            status=STATUS_MANUAL,
            summary=(
                "页面正文不含该文件的全文特征（可能为 JS 渲染或栏目页），"
                "无法确认是否存在施行条款 —— 无法自动取证，需人工核"
            ),
            evidence=[],
            options=[CheckOption(
                key="confirm-date",
                label="我已人工核对原文的施行/废止条款",
                action={"kind": "acknowledge", "field": "date"},
            )],
        )]
    # 页面确为全文且 0 命中：绿——「不知道」与「原文确实没写」一致
    return [CheckResult(
        check_id=check_id,
        layer="日期与状态",
        status=STATUS_OK,
        summary="记录未填写生效日期，原文亦未检索到施行/生效表述 —— 留空与原文一致",
        evidence=[],
    )]


# ============================================================
# 检查层 ④：义务清单核对
# ============================================================

# 概括与原段文本的重合度阈值。
# 义务概括是人工改写过的（「谁必须做什么」一句话），与法条原文的
# 字面重合天然不高——阈值的作用是拦住「概括与条款风马牛不相及」
# （比如条款号填错、定位到了别的段落），而不是评判文风。
# 定低了会漏掉错配，定高了会把所有合格概括全标红（告警疲劳），
# 实测 0.10-0.15 区间能分开「同段改写」与「定位错误」两种情形。
OBLIGATION_OVERLAP_THRESHOLD = 0.12

# 情态词表：鼓励性表述
_SOFT_MODAL_WORDS = ("鼓励", "支持", "提倡", "引导")
# 情态词表：强制性表述
_MANDATORY_MODAL_WORDS = ("应当", "必须", "不得", "禁止", "严禁")

# 属「强制类」的义务类型（原文应为强制性表述才一致）
_MANDATORY_TYPES = {
    "mandatory", "prohibition", "requires-approval",
    "requires-reporting", "requires-recordkeeping", "requires-assessment",
}


def _clause_pattern(clause: str) -> re.Pattern[str] | None:
    """把义务条目的 ``clause`` 字段编译成「第 X 条」定位正则。

    ``clause`` 的约定写法是「十六」「第 16 条」「16」——
    统一提取其中的数字语义，生成同时匹配阿拉伯与中文写法的正则。
    无法提取数字语义时返回 None（调用方退化为字面匹配）。
    """
    # 先尝试提取阿拉伯数字
    arabic = re.search(r"\d+", clause)
    # 命中时构造「阿拉伯|中文」二选一正则
    if arabic:
        # 数字值
        value = int(arabic.group(0))
        # 中文写法（可能为 None）
        cn = _int_to_cn(value)
        # 备选写法列表
        forms = [str(value)] + ([cn] if cn else [])
        # 编译「第（16|十六）条」
        return re.compile(r"第\s*(?:" + "|".join(forms) + r")\s*条")
    # 再尝试把整段当中文数字解析（如「十六」）。
    # 变量另起名字：上面的 value 已被 mypy 推断为 int，
    # 这里的结果是 int | None，复用同名变量会让类型推断冲突。
    cn_value = _cn_to_int(re.sub(r"[第条\s]", "", clause))
    # 解析成功时同样构造二选一
    if cn_value is not None:
        # 中文写法
        cn = _int_to_cn(cn_value)
        # 备选写法
        forms = [str(cn_value)] + ([cn] if cn else [])
        # 编译
        return re.compile(r"第\s*(?:" + "|".join(forms) + r")\s*条")
    # 提取不到数字语义
    return None


def _extract_clause_paragraph(text: str, match_start: int) -> str:
    """从条款匹配位置取出该条的完整段落（到下一个「第 X 条」为止）。"""
    # 从匹配位置向后找下一个条款起始
    next_match = re.search(r"第\s*[0-9一二三四五六七八九十]+\s*条", text[match_start + 3:])
    # 段落终点：下一个条款起点，或文本末尾
    end = match_start + 3 + next_match.start() if next_match else len(text)
    # 截出段落
    return text[match_start:end]


def check_obligations(policy: Policy, content: PageContent | None) -> list["CheckResult"]:
    """检查层四：关键义务条目与原文条款的核对。

    每条义务产一条检查结果（定位 / 重合度 / 情态词三者合并报告）。
    草稿阶段 ``key_obligations`` 通常为空——此时本层绿，
    并说明「无义务条目」（空的义务清单是草稿的合法状态）。
    """
    # 无义务条目：绿，如实说明
    if not policy.key_obligations:
        # 返回单条绿项
        return [CheckResult(
            check_id="obligations",
            layer="义务核对",
            status=STATUS_OK,
            summary="记录暂无关键义务条目（草稿阶段待人工提炼）",
            evidence=[],
        )]
    # 正文不可用：每条义务都标 manual，不能当作通过
    if content is None or not content.usable:
        # 逐条标 manual
        return [CheckResult(
            check_id=f"obligation:{index}",
            layer="义务核对",
            status=STATUS_MANUAL,
            summary=f"第 {obligation.clause} 条：页面正文无法取得 —— 无法自动取证，需人工核",
            evidence=[],
            options=[CheckOption(
                key="confirm-clause",
                label="我已人工核对原文该条款",
                action={"kind": "acknowledge", "field": f"obligation:{index}"},
            )],
        ) for index, obligation in enumerate(policy.key_obligations)]

    # 结果容器
    results: list[CheckResult] = []
    # 逐条义务核对
    for index, obligation in enumerate(policy.key_obligations):
        # 检查项标识
        check_id = f"obligation:{index}"
        # 编译条款定位正则
        pattern = _clause_pattern(obligation.clause)
        # 无法编译时退化为字面匹配（clause 本身就是段落标记）
        match = pattern.search(content.body_text) if pattern else None
        # 定位失败：红
        if match is None:
            # 追加红项
            results.append(CheckResult(
                check_id=check_id,
                layer="义务核对",
                status=STATUS_ERROR,
                summary=f"第 {obligation.clause} 条：未能在原文中定位到该条款 —— 条款号可能填写有误",
                evidence=[f"义务概括：{obligation.summary}"],
                options=[
                    CheckOption(
                        key="confirm-clause",
                        label="我已人工定位到原文条款，维持该条目",
                        action={"kind": "acknowledge", "field": f"obligation:{index}"},
                    ),
                    CheckOption(
                        key="drop-obligation",
                        label="删除该义务条目（无法找到原文依据）",
                        action={"kind": "drop-obligation", "index": index},
                    ),
                ],
            ))
            # 继续下一条
            continue
        # 取出该条款的完整段落
        paragraph = _extract_clause_paragraph(content.body_text, match.start())
        # 计算概括与原段的重合度（归一化后比对，消除排版差异）
        overlap = difflib.SequenceMatcher(None, obligation.summary, paragraph).ratio()
        # 情态词检查：段落里的表述与义务类型是否一致
        has_soft = any(word in paragraph for word in _SOFT_MODAL_WORDS)
        # 强制表述
        has_mandatory = any(word in paragraph for word in _MANDATORY_MODAL_WORDS)
        # 情态漂移判定：原文鼓励性却标强制类，或原文强制性却标鼓励类
        modal_conflict: str | None = None
        # 情形一：原文含鼓励性表述、不含强制表述，而类型为强制类
        if has_soft and not has_mandatory and obligation.obligation_type in _MANDATORY_TYPES:
            # 记录冲突
            modal_conflict = f"原文为鼓励性表述，但 obligation_type={obligation.obligation_type}（强制类）"
        # 情形二：原文含强制表述、不含鼓励表述，而类型为鼓励类
        if has_mandatory and not has_soft and obligation.obligation_type == "encouraged":
            # 记录冲突
            modal_conflict = "原文为强制性表述，但 obligation_type=encouraged（鼓励类）"
        # 重合度不足：红
        if overlap < OBLIGATION_OVERLAP_THRESHOLD:
            # 追加红项
            results.append(CheckResult(
                check_id=check_id,
                layer="义务核对",
                status=STATUS_ERROR,
                summary=(
                    f"第 {obligation.clause} 条：概括与原段文本重合度 {overlap:.2f} 低于阈值 "
                    f"{OBLIGATION_OVERLAP_THRESHOLD} —— 概括可能并非出自该条款"
                ),
                evidence=[f"义务概括：{obligation.summary}", f"原段摘录：{paragraph[:300]}"],
                options=[
                    CheckOption(
                        key="confirm-summary",
                        label="我已人工比对，概括与该条款一致",
                        action={"kind": "acknowledge", "field": f"obligation:{index}"},
                    ),
                    CheckOption(
                        key="drop-obligation",
                        label="删除该义务条目",
                        action={"kind": "drop-obligation", "index": index},
                    ),
                ],
            ))
            # 继续下一条
            continue
        # 情态漂移：红
        if modal_conflict:
            # 追加红项
            results.append(CheckResult(
                check_id=check_id,
                layer="义务核对",
                status=STATUS_ERROR,
                summary=f"第 {obligation.clause} 条：情态词漂移 —— {modal_conflict}",
                evidence=[f"原段摘录：{paragraph[:300]}"],
                options=[
                    CheckOption(
                        key="to-encouraged" if obligation.obligation_type in _MANDATORY_TYPES else "to-mandatory",
                        label=(
                            "改为 encouraged（按原文鼓励性表述）"
                            if obligation.obligation_type in _MANDATORY_TYPES
                            else "改为 mandatory（按原文强制性表述）"
                        ),
                        action={
                            "kind": "set-obligation-type",
                            "index": index,
                            "value": "encouraged" if obligation.obligation_type in _MANDATORY_TYPES else "mandatory",
                        },
                    ),
                    CheckOption(
                        key="keep-type",
                        label=f"维持 {obligation.obligation_type}（我已人工核对原文）",
                        action={"kind": "acknowledge", "field": f"obligation:{index}"},
                    ),
                ],
            ))
            # 继续下一条
            continue
        # 全部通过：绿
        results.append(CheckResult(
            check_id=check_id,
            layer="义务核对",
            status=STATUS_OK,
            summary=f"第 {obligation.clause} 条：条款定位成功，概括与原段一致（重合度 {overlap:.2f}）",
            evidence=[f"原段摘录：{paragraph[:200]}"],
        ))
    # 返回全部结果
    return results


# ============================================================
# 证据卡
# ============================================================

@dataclass
class CheckOption:
    """一个判断选项 —— 按钮的选项即判断本身。

    界面上不为「待判断」的项提供自由文本框：自由文本会把
    「判断」退化成「写说明」，而留痕要求的是结构化的选择
    （采纳 / 维持 / 清空 / 删除），每一种都对应 ``promote.py``
    里一个确定的动作。
    """

    # 选项键（界面回传给服务端）
    key: str
    # 按钮文字（判断的自然语言表述）
    label: str
    # 结构化动作（promote 据此改写记录字段）
    action: dict[str, Any]


@dataclass
class CheckResult:
    """一项检查的结果。"""

    # 检查项稳定标识（如 reachability / title / date / obligation:0）
    check_id: str
    # 所属检查层的中文名
    layer: str
    # 状态：ok / warning / error / manual
    status: str
    # 一句话结论
    summary: str
    # 证据摘录（原文字句、URL、哈希值等）
    evidence: list[str] = field(default_factory=list)
    # 判断选项（绿项为空——机器通过的项不要求人点击）
    options: list[CheckOption] = field(default_factory=list)

    # 转为可 JSON 序列化的字典
    def to_dict(self) -> dict[str, Any]:
        """转为字典，供证据卡 JSON 与界面渲染使用。"""
        # 逐字段输出
        return {
            "check_id": self.check_id,                                # 标识
            "layer": self.layer,                                      # 检查层
            "status": self.status,                                    # 状态
            "status_label": STATUS_LABELS.get(self.status, self.status),  # 中文标签
            "summary": self.summary,                                  # 结论
            "evidence": self.evidence,                                # 证据摘录
            "options": [                                              # 判断选项
                {"key": o.key, "label": o.label} for o in self.options
            ],
        }


@dataclass
class EvidenceCard:
    """一张核验证据卡 —— 一条草稿的全部机器取证结果。"""

    # 草稿（政策）id
    policy_id: str
    # 草稿标题
    title: str
    # 取证的官方链接
    url: str
    # 各检查项结果
    checks: list[CheckResult]
    # 取证时间（北京时间 ISO 字符串）
    generated_at: str = field(default_factory=now_china_iso)

    def pending_checks(self) -> list[CheckResult]:
        """返回需要人判断的检查项（黄 / 红 / 需人工核）。

        绿项不在其列：机器通过的项折叠为徽章即可，
        要求人对每一项都点一次「确认」是把留痕做成负担——
        负担一重，人就会无意识地连点，留痕反而失去意义。
        """
        # 过滤非绿项
        return [c for c in self.checks if c.status != STATUS_OK]

    def count_by_status(self) -> dict[str, int]:
        """按状态统计检查项数量，供清单行显示「含红项条数」。"""
        # 计数容器
        counts: dict[str, int] = {}
        # 逐项累计
        for check in self.checks:
            # 累计
            counts[check.status] = counts.get(check.status, 0) + 1
        # 返回
        return counts

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典。"""
        # 逐字段输出
        return {
            "policy_id": self.policy_id,                        # 草稿 id
            "title": self.title,                                # 标题
            "url": self.url,                                    # 官方链接
            "generated_at": self.generated_at,                  # 取证时间
            "pending_count": len(self.pending_checks()),        # 待判断项数
            "counts": self.count_by_status(),                   # 状态统计
            "checks": [c.to_dict() for c in self.checks],       # 检查项明细
        }


def build_evidence_card(policy: Policy, session: Any | None = None) -> EvidenceCard:
    """对一条草稿执行四个检查层，产出证据卡。

    抓取失败时 ``content`` 为 None，各层自行决定标 error 还是 manual——
    唯一不允许的是「当没发生」。
    """
    # 抓取官方页面
    page = fetch_page(policy.source.url, session)
    # 页面拿到时才提取内容；否则内容为 None（各层标 manual/error）
    content = extract_content(page.html) if page.ok and page.html is not None else None
    # 依次执行四个检查层并合并结果
    checks: list[CheckResult] = []
    # 层一：可达性
    checks.extend(check_reachability(policy, page, content))
    # 层二：标题比对
    checks.extend(check_title(policy, content))
    # 层三：日期与状态取证
    checks.extend(check_dates(policy, content))
    # 层四：义务清单核对
    checks.extend(check_obligations(policy, content))
    # 组装证据卡
    return EvidenceCard(
        policy_id=policy.id,          # 草稿 id
        title=policy.title,           # 标题
        url=policy.source.url,        # 官方链接
        checks=checks,                # 检查项
    )


# ============================================================
# 草稿加载
# ============================================================

def load_drafts(directory: Path | None = None) -> tuple[dict[str, Policy], list[str]]:
    """加载草稿目录下全部草稿，返回 ``(id 到 Policy 的映射, 错误列表)``。

    草稿与政策记录同构（都过 policy.schema.json 校验），
    因此直接复用 ``load_policy_file`` 的三层校验——
    草稿若连 schema 都过不了，核验台应当先报这个错，
    而不是对着一份坏数据做页面取证。
    """
    # 未指定目录时用默认草稿目录
    target = directory or DRAFTS_DIR
    # 结果映射
    drafts: dict[str, Policy] = {}
    # 错误收集
    errors: list[str] = []
    # 目录不存在：明确报错而不是返回空（空草稿目录在 B 线之后不该出现；
    # 真出现说明目录被误删或路径配错，静默返回空会让人以为「没有待核验的」）
    if not target.exists():
        # 返回错误
        return {}, [f"草稿目录不存在：{target}"]
    # 遍历 YAML 文件（排序保证输出稳定）
    for path in sorted(target.glob("*.yaml")):
        # 加载单个文件（复用三层校验）
        policy, file_errors = load_policy_file(path)
        # 只保留错误级问题，丢弃警告级。
        # 理由与 pipeline.detect_baseline_changes 相同：草稿的定义就是
        # 「verified_by=automated 的候选记录」，语义校验对每条草稿必然
        # 产出「尚未经人工核验」警告——11 条草稿每次出证都打印 12 行
        # 恒真警告，真正的加载错误（YAML 损坏、schema 不符）会被淹没。
        # 告警一多，人就会习惯性忽略——那比不告警更糟。
        # 注意这不等于丢弃信息：草稿入库后，同一警告会在 policies/
        # 的 validate 流程里照常出现；核验台界面也会逐条展示待判断项。
        errors.extend(msg for msg in file_errors if not is_warning(msg))
        # 加载成功则登记
        if policy is not None:
            # id 冲突检测
            if policy.id in drafts:
                # 记录冲突
                errors.append(f"{path.name}: id 重复，与 {drafts[policy.id].id} 冲突")
            else:
                # 登记
                drafts[policy.id] = policy
    # 返回结果
    return drafts, errors


def build_all_cards(
    drafts: dict[str, Policy], session: Any | None = None
) -> dict[str, EvidenceCard]:
    """对全部草稿逐一出证，返回 id 到证据卡的映射。

    单条失败不影响其他草稿：``build_evidence_card`` 内部已把
    网络失败转成红项，异常不会冒泡到这里——但万一出现
    代码级异常（而非网络异常），也要显式记为一张「取证据失败」的卡，
    而不是让这条草稿从清单里静默消失。
    """
    # 结果映射
    cards: dict[str, EvidenceCard] = {}
    # 逐条出证
    for pid, draft in drafts.items():
        try:
            # 正常出证
            cards[pid] = build_evidence_card(draft, session)
        except Exception as exc:  # noqa: BLE001  刻意捕获全部：单条失败不得拖垮整批
            # 造一张「取证据过程本身失败」的卡——全部检查项标 manual
            cards[pid] = EvidenceCard(
                policy_id=pid,
                title=draft.title,
                url=draft.source.url,
                checks=[CheckResult(
                    check_id="evidence-failure",
                    layer="取证过程",
                    status=STATUS_MANUAL,
                    summary=f"取证过程发生异常：{type(exc).__name__}: {exc} —— 无法自动取证，需人工核",
                    evidence=[],
                    options=[CheckOption(
                        key="confirm-manual",
                        label="我已人工核对该记录全部字段",
                        action={"kind": "acknowledge", "field": "evidence-failure"},
                    )],
                )],
            )
    # 返回
    return cards


# ============================================================
# 命令行渲染
# ============================================================

# 各状态的行首标记。用文字而非彩色符号：Windows 控制台对 Unicode
# 符号的支持不稳定，乱码的标记比没有标记更糟（本项目一贯做法）。
_STATUS_MARKERS = {
    STATUS_OK: "[通过]",        # 绿
    STATUS_WARNING: "[待判断]",  # 黄
    STATUS_ERROR: "[冲突]",      # 红
    STATUS_MANUAL: "[人工核]",   # 灰
}


def render_card_lines(card: EvidenceCard) -> list[str]:
    """把一张证据卡渲染成终端可读的文本行。"""
    # 输出行容器
    lines: list[str] = []
    # 卡头：id + 标题
    lines.append(f"■ {card.policy_id}  {card.title}")
    # 官方链接
    lines.append(f"  {card.url}")
    # 逐项输出
    for check in card.checks:
        # 状态标记
        marker = _STATUS_MARKERS.get(check.status, "[未知]")
        # 输出一行：标记 + 层 + 结论
        lines.append(f"  {marker} {check.layer} —— {check.summary}")
        # 非绿项附判断选项，让人知道下一步能做什么
        for option in check.options:
            # 输出选项
            lines.append(f"         ↳ 可判断：{option.label}")
    # 汇总行
    counts = card.count_by_status()
    # 拼装统计
    lines.append(
        f"  合计：通过 {counts.get(STATUS_OK, 0)} / 待判断 {counts.get(STATUS_WARNING, 0)} "
        f"/ 冲突 {counts.get(STATUS_ERROR, 0)} / 需人工核 {counts.get(STATUS_MANUAL, 0)}"
    )
    # 返回
    return lines


# 重导出 promote 所需的类型，避免界面层从两处导入（保持单一入口）
__all__ = [
    "DRAFTS_DIR",               # 草稿目录
    "STATUS_OK",                # 状态：机器通过
    "STATUS_WARNING",           # 状态：待判断
    "STATUS_ERROR",             # 状态：证据冲突
    "STATUS_MANUAL",            # 状态：需人工核
    "CheckOption",              # 判断选项
    "CheckResult",              # 检查结果
    "EvidenceCard",             # 证据卡
    "PageFetch",                # 页面抓取结果
    "build_all_cards",          # 批量出证
    "build_evidence_card",      # 单条出证
    "fetch_page",               # 页面抓取（测试注入点）
    "load_drafts",              # 草稿加载
    "render_card_lines",        # 终端渲染
]
