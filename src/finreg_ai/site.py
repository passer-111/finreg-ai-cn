"""静态站点生成器 —— 把 ``data/`` 下的政策数据渲染成一个可直接发布的只读网站。

为什么是静态站，而不是「后端 + 数据库」
--------------------------------------
本项目的数据量极小（20 条政策合计约 168 KB），而它的核心卖点是**版本溯源**：
``data/changes/*.json`` 就是「哪条政策哪天变了」的台账，其可信度恰恰来自
「它就是 git 历史」。引入数据库会制造第二个真相源，一旦与 git 分叉，
「可核验、可举证」这句话立刻失去依据。

因此本站点的数据流是单向的：

    data/*.yaml                              ← 机器取证入库的记录（verified_by 分级，人工级按需升级）
    data/changes/*.json                      ← 机器发现的变更
            │
            ▼   finreg build-site
    docs/*.html + docs/data/*.json           ← 构建产物，不入库

**git 是唯一真相源，站点只是它的一个视图。** 站点里没有任何数据是单独维护的：
删掉整个输出目录重新生成，结果逐字节一致。

输出物为什么不提交进仓库
------------------------
``docs/`` 是**构建产物**（见 ``.gitignore``）。仓库里已经有 ``fetch.yml``
会自动提交数据变更，若再把生成物一并提交，每次数据变化都会同时产生两份 diff，
两个自动提交互相竞争的概率成倍上升——本项目已经处理过一次这类分叉（见
``MEMORY.md`` 里「工作流的自动提交会与本地分叉」）。发布改走
``.github/workflows/pages.yml``：CI 里构建、以 Pages 产物形式上传。

样式与检索脚本为什么是独立文件
------------------------------
本模块只生成 HTML 与 JSON——它们需要与数据插值，必须由代码产生。
而 CSS 与 JS 是纯静态的，放进 ``site/assets/`` 作为普通源文件维护：
在 Python 字符串里写 CSS 会让语法高亮、括号匹配与注释全部失效，
也会把大括号与 f-string 的插值语法搅在一起。
"""

# 导入 Any 类型标注：变更文件的原始结构来自 JSON，形状不必在此重新建模
from typing import Any
# 导入 dataclass：构建结果需要一个结构化的返回值，便于 CLI 渲染与测试断言
from dataclasses import dataclass, field
# 导入 date 与 datetime：日期用于比较与展示，datetime 用于生成页脚时间戳
from datetime import date, datetime
# 导入 html 模块：所有插入 HTML 的数据都必须转义，这是本项目唯一防注入的手段
import html
# 导入 json：输出机器可读的 API 文件
import json
# 导入 Path：所有目录参数都用 Path 表达，避免字符串拼接导致的跨平台路径问题
from pathlib import Path
# 导入 shutil：复制 site/assets 下的静态资源
import shutil

# 导入 Policy 类型标注：渲染函数接收的是已加载的模型对象，而非原始字典
from finreg_ai.models import Policy, policy_to_dict
# 导入中国大陆时区常量：页脚时间戳必须与抓取层（now_china_iso）同一口径
from finreg_ai.fetchers.base import CHINA_TZ
# 导入项目根目录与政策加载函数
from finreg_ai.store import PROJECT_ROOT, load_all_policies

# 默认输出目录。取名 docs 是沿用 GitHub Pages 的历史惯例，
# 只是因为「一眼能看出这是站点」；实际发布走 Actions 产物，不依赖这个目录名。
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "docs"
# 静态源文件目录（CSS / JS）。与输出目录分开：这里是手写维护的源，那里是生成物。
SITE_SOURCE_DIR = PROJECT_ROOT / "site"
# 变更文件目录
CHANGES_DIR = PROJECT_ROOT / "data" / "changes"

# 站点标题
SITE_TITLE = "中国金融 AI 合规政策库"
# 站点副标题：一句话说清「这是什么」，且必须点出与竞品的差别
SITE_TAGLINE = "机器可读 · 版本溯源 · 变更流"

# 免责声明。README 顶部有同一段话，此处是站点侧的唯一入口——
# 使用者从搜索引擎直接落到某条政策页时，不会经过 README，
# 因此免责声明必须出现在**每一个**页面上，而不只是首页。
DISCLAIMER_HTML = (
    "本站是对<b>官方公开发布信息</b>的整理与索引，<b>不构成法律意见</b>，"
    "亦不能替代执业律师或监管机构的正式答复。所载内容以官方原文为准；"
    "每条记录均附官方链接与核验台账，请在使用前自行回源核对。"
)

# ============================================================
# 枚举的中文标签与视觉色调
# ============================================================
#
# 为什么要在站点层再做一份标签映射，而不是直接用 schema 里的枚举值：
# 站点面向的是合规从业者，不是读 schema 的工程师。
# 「effective」对工程师无歧义，但对使用者必须写成「现行有效」——
# 而「现行有效」这四个字正是使用者来这个站要问的问题。
# 注意：标签只做**翻译**，绝不改写语义；状态值本身的置信等级见各记录的核验说明。

# 法律状态 → 中文标签
STATUS_LABELS = {
    "draft": "起草中",              # 尚未公开征求意见
    "consultation": "征求意见",      # 已发布征求意见稿，未定稿
    "published": "已公布未生效",     # 生效日尚未到达
    "effective": "现行有效",         # 处于生效期内
    "partially_effective": "部分有效",  # 部分条款生效或已部分失效
    "amended": "已修订",             # 修订后版本有效
    "repealed": "已废止",            # 明文废止
    "superseded": "已被取代",        # 被新文件取代但未明文废止
    "expired": "已失效",             # 有效期届满
    "unknown": "状态待核验",         # 无法确认，须看核验说明
}

# 法律状态 → 徽章色调。
# 分三档而非两档：把「已废止」和「尚未生效」都涂成同一种颜色是错误的——
# 前者意味着「不要再据此办事」，后者意味着「现在还不能据此办事」，
# 处置方向完全不同，视觉上必须能一眼分开。
_STATUS_TONE_OK = "ok"            # 有效
_STATUS_TONE_PENDING = "pending"  # 尚未生效
_STATUS_TONE_DEAD = "dead"        # 已退出法律生命
_STATUS_TONE_WARN = "warn"        # 状态不明，需要人去看核验说明

# 法律状态 → 色调
STATUS_TONES = {
    "draft": _STATUS_TONE_PENDING,        # 起草中
    "consultation": _STATUS_TONE_PENDING,  # 征求意见
    "published": _STATUS_TONE_PENDING,     # 已公布未生效
    "effective": _STATUS_TONE_OK,          # 现行有效
    "partially_effective": _STATUS_TONE_OK,  # 部分有效
    "amended": _STATUS_TONE_OK,            # 已修订但有效
    "repealed": _STATUS_TONE_DEAD,         # 已废止
    "superseded": _STATUS_TONE_DEAD,       # 已被取代
    "expired": _STATUS_TONE_DEAD,          # 已失效
    "unknown": _STATUS_TONE_WARN,          # 状态待核验
}

# 约束力 → 中文标签。
# 这一项与「文件层级」正交，是合规压力判断的关键，因此标签要写足含义，
# 不能简写成「强制 / 非强制」——soft-law 在实务中是有事实约束力的。
BINDINGNESS_LABELS = {
    "binding": "强制义务",                    # 违反面临行政处罚或监管措施
    "soft-law": "软法（有事实约束力）",         # 构成监管预期
    "guidance": "倡导性",                     # 无强制力
    "consultation": "征求意见（无义务）",       # 尚未生效
}

# AI 相关度 → 中文标签
AI_RELEVANCE_LABELS = {
    "core": "核心",        # 主体内容直接规范 AI
    "related": "相关",     # 部分条款涉及 AI
    "background": "背景",  # 仅提供上位法背景
}

# 义务类型 → 中文标签
OBLIGATION_TYPE_LABELS = {
    "prohibition": "禁止",            # 不得做
    "mandatory": "强制要求",          # 必须做
    "requires-approval": "需审批",     # 事前批准
    "requires-reporting": "需报告",    # 事后/事前报送
    "requires-recordkeeping": "需留痕",  # 记录保存
    "requires-assessment": "需评估",   # 评估义务
    "encouraged": "鼓励",             # 倡导性
}

# 变更类型 → 中文标签。取值来自 diff.py 的 CHANGE_TYPE_* 常量。
CHANGE_TYPE_LABELS = {
    "new": "首次收录",                      # 从无到有
    "content_changed": "正文变更",           # 需重做合规评估
    "metadata_changed": "元数据变更",         # 不需重新评估
    "status_changed": "状态变更",            # 最需关注
    "discovered": "待录入",                 # 机器发现，人尚未认定
}

# 重要度 → 中文标签
SIGNIFICANCE_LABELS = {
    "high": "重要",      # 需要尽快处理
    "medium": "一般",    # 例行关注
    "low": "次要",       # 仅存档
}

# 核验方式 → 中文标签。
# 这个标签直接决定使用者该给这条记录多少信任，因此不能用「自动 / 人工」这样的
# 技术词，必须写清「自动」意味着**未经人工逐项核对**。
# 注意：库里 automated 记录有两种来历——抓取流水线整理（早期 9 条）与
# 四层机器取证后自动入库（2026-10-08 起的 11 条），两者的共同点是
# 「没有人工逐项核对」，标签只承诺这个共同点，细节在各记录的核验说明里。
VERIFIED_BY_LABELS = {
    "human": "人工逐项核对",              # 已打开官方页面核对状态字段
    "automated": "机器核验（未经人工逐项核对）",  # 未人工核验，需谨慎
}

# 来源等级 → 中文标签
SOURCE_TIER_LABELS = {
    "primary": "官方原始渠道",   # 唯一可作为记录来源的等级
    "secondary": "第三方转载",   # 仅用于发现线索
    "unverified": "来源不明",    # 一律不采用
}

# 「现行有效族」的状态集合，用于首页统计。
# 与 models.PolicyStatus.is_currently_valid 保持一致：
# 「已修订」也算有效——它是「有效但需注意版本」，不是「已失效」。
CURRENTLY_VALID_STATUSES = {"effective", "partially_effective", "amended"}


# ============================================================
# 渲染小工具
# ============================================================

def _esc(value: Any) -> str:
    """把任意值转成可安全插入 HTML 的字符串。

    两个细节是刻意的：

    1. ``None`` 一律渲染成空串而不是 ``"None"``。本项目的字段大量可空
       （``doc_number`` / ``effective_from`` / ``note`` 都是），若让 ``None``
       以字面量漏到页面上，读者会以为原文里真写了这四个字母。
    2. ``quote=True`` 是默认值但这里显式说明：数据既会进文本节点，也会进
       ``data-*`` 属性与 ``href``，属性场景下不转义引号就等于给了注入开口。
       站点是纯静态的，转义是唯一的防线。
    """
    # 空值统一渲染为空串，避免 None 字面量出现在页面上
    if value is None:
        # 返回空串
        return ""
    # 其余一律先转字符串再转义（数字、日期、枚举都可能传进来）
    return html.escape(str(value), quote=True)


def _or_placeholder(value: Any, placeholder: str = "—") -> str:
    """有值就转义输出，无值就给出显式占位符。

    为什么不能一律用空串代替缺失值：在**表格**里，空格与「这一栏没有数据」
    看起来完全一样，读者会以为自己漏看了。显式的「—」才是诚实的沉默。
    这与本项目「明确失败优于静默错误」的原则是同一条。
    """
    # 空字符串、空列表、None 都视为「没有值」
    if value is None or value == "" or value == []:
        # 返回占位符（占位符本身是纯符号，无需转义，但仍走同一路径保持一致）
        return _esc(placeholder)
    # 有值时正常转义输出
    return _esc(value)


def _badge(label: str, tone: str = "") -> str:
    """渲染一个徽章元素。

    ``tone`` 取空串时只是一个中性徽章（如「部门规章」这类纯粹的层级信息），
    取 ``ok`` / ``pending`` / ``dead`` / ``warn`` 时才着色——
    颜色是稀缺资源，只有需要读者**立刻做判断**的字段才配得上它。
    若所有字段都上色，等于所有字段都没有颜色。
    """
    # 组装 class 列表：基础类 + 可选色调类
    classes = "badge" + (f" badge-{tone}" if tone else "")
    # 标签内容必须转义；class 名来自代码内部的常量，不含外部输入
    return f'<span class="{classes}">{_esc(label)}</span>'


def _status_badge(status_value: str) -> str:
    """渲染状态徽章——全站最重要的一个视觉元素。"""
    # 查中文标签，未知取值回退为原值（宁可显示英文也不隐藏信息）
    label = STATUS_LABELS.get(status_value, status_value)
    # 查色调，未知取值不着色（回退到中性）
    tone = STATUS_TONES.get(status_value, "")
    # 渲染
    return _badge(label, tone)


def _date_text(value: date | None, placeholder: str = "—") -> str:
    """把日期渲染成 ``YYYY-MM-DD``，空值给出占位符。"""
    # 空日期返回占位符
    if value is None:
        # 显式占位，避免读者误以为漏看
        return _esc(placeholder)
    # 有日期则格式化为 ISO 形式（本项目全库统一使用这一种日期格式）
    return _esc(value.isoformat())


def _label_of(mapping: dict[str, str], value: Any) -> str:
    """从标签映射里取值，缺失时回退为原始值。

    回退到原始值而不是「未知」：新增枚举取值时若回退成「未知」，
    页面上会同时出现好几处无法区分的「未知」，反而掩盖了真正的问题。
    显示原始英文值至少能让人看出「这是一个还没配标签的新取值」。
    """
    # 空值直接返回占位符
    if value is None:
        # 用统一占位符
        return "—"
    # 转成字符串后查表，查不到就用原值
    key = str(value)
    # 返回标签或原值（原值同样需要转义）
    return mapping.get(key, key)


def _chips(values: list[str], css_class: str = "chip") -> str:
    """把一组标签渲染成小圆片。空列表渲染成占位符。"""
    # 无内容时给出占位符，而不是留白
    if not values:
        # 返回占位符
        return '<span class="muted">—</span>'
    # 逐个转义并包裹成 span
    return "".join(f'<span class="{css_class}">{_esc(v)}</span>' for v in values)


def _external_link(url: Any, text: str) -> str:
    """渲染一个指向官方站点的外链。

    ``rel="noopener noreferrer"`` 是必须的：新窗口打开的页面能通过
    ``window.opener`` 反向操作本页。虽然本站是纯静态、无登录态，
    但这是零成本的正确默认，没有理由省掉。
    """
    # 无链接时给出明确提示，而不是渲染一个点不开的空链接
    if not url:
        # 提示缺失，这正是本项目 `source.url 必须指向文件本身` 那条待办的可见化
        return '<span class="muted">（无官方链接）</span>'
    # 转义后的地址同时用作 href 与显示文本
    safe = _esc(url)
    # 组装外链
    return f'<a href="{safe}" target="_blank" rel="noopener noreferrer">{_esc(text)}</a>'


# ============================================================
# 数据装载
# ============================================================

def load_change_documents(directory: Path | None = None) -> list[dict[str, Any]]:
    """读取 ``data/changes/`` 下的全部变更文件，按日期倒序返回。

    容错策略与 ``pipeline.write_change_set`` 一致但方向相反：
    那里遇到损坏文件要保护写入，这里遇到损坏文件要**跳过并在页面上记一笔**。
    理由相同——产物损坏不该让整件事失败，但它也绝不能被静默忽略。
    因此坏文件不会抛异常，而是返回一条 ``_unreadable`` 标记记录，
    由页面渲染层显式展示出来。
    """
    # 确定目录，默认取项目内的变更目录
    target = directory if directory is not None else CHANGES_DIR
    # 目录不存在时返回空列表（尚未抓取过是合法状态）
    if not target.exists():
        # 无变更文件
        return []

    # 收集结果
    documents: list[dict[str, Any]] = []
    # 按文件名排序保证输出稳定（文件名即日期，字典序等于时间序）
    for path in sorted(target.glob("*.json")):
        # 读取失败不抛出，转为一条可展示的错误记录
        try:
            # 读取并解析
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 记录一条不可读标记，页面上会显式提示，绝不静默跳过
            documents.append({"_unreadable": path.name, "_error": str(exc), "changes": []})
            # 继续处理下一个文件
            continue
        # 顶层不是对象说明文件结构已被破坏，同样转为标记记录
        if not isinstance(data, dict):
            # 记录类型错误
            documents.append({"_unreadable": path.name, "_error": "顶层不是 JSON 对象", "changes": []})
            # 继续
            continue
        # 补上文件名，便于页面标注来源
        data.setdefault("_file", path.name)
        # 收进结果
        documents.append(data)

    # 按检测日期倒序：最新的变更排最前
    documents.sort(key=lambda d: str(d.get("detected_on") or ""), reverse=True)
    # 返回
    return documents


def _policy_to_site_dict(policy: Policy) -> dict[str, Any]:
    """把政策对象转成 JSON API 里的一条记录。

    结构上刻意分成两层：

    - **顶层保持与 ``policy.schema.json`` 完全一致**，直接来自 ``policy_to_dict``。
      这样消费者可以拿 schema 去校验它，也可以把它当作「从 YAML 里读出来的同一份东西」，
      不需要为「站点版」单独写一套解析。
    - **派生字段全部收进 ``derived`` 子对象**，包括中文状态标签、是否现行有效、
      详情页地址。这些是**站点的视图属性**，不是政策本身的事实——
      混进顶层会让「哪些字段来自人工核验」变得无法分辨，
      而这条界线正是本项目全部可信度的来源。
    """
    # 基础字典：日期已转成 ISO 字符串，枚举已转成取值字符串
    data = policy_to_dict(policy)
    # 派生层
    data["derived"] = {
        # 状态的中文标签，省得每个消费者各自维护一份映射
        "status_label": STATUS_LABELS.get(policy.status.value, policy.status.value),
        # 是否现行有效：与 models.PolicyStatus.is_currently_valid 同一口径
        "is_currently_valid": policy.status.value in CURRENTLY_VALID_STATUSES,
        # 该记录在本站点内的详情页地址（相对站点根目录）
        "page": f"policies/{policy.id}.html",
    }
    # 返回
    return data


# ============================================================
# 页面骨架
# ============================================================

def _page_shell(*, title: str, body: str, asset_prefix: str, generated_at: str) -> str:
    """渲染页面的公共外壳（head、导航、页脚、免责声明）。

    ``asset_prefix`` 是资源路径前缀：首页在根目录下传空串，
    详情页在 ``policies/`` 子目录下传 ``"../"``。
    子目录页面的相对路径是最容易出错的地方——少一个 ``../`` 会让样式在
    详情页上整体失效，而首页看起来完全正常，极易漏测。
    """
    # 页面标题：详情页标题在前，站点名在后，便于浏览器多标签页区分
    full_title = f"{_esc(title)} · {_esc(SITE_TITLE)}" if title else _esc(SITE_TITLE)
    # 返回完整 HTML 文档
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{_esc(SITE_TITLE)} —— {_esc(SITE_TAGLINE)}。收录中国金融领域人工智能合规相关政策，含生效状态、版本链与变更流。">
<title>{full_title}</title>
<link rel="stylesheet" href="{asset_prefix}assets/style.css">
<link rel="icon" href="{asset_prefix}assets/favicon.ico" sizes="16x16">
</head>
<body>
<header class="site-header">
  <div class="wrap">
    <a class="brand" href="{asset_prefix}index.html">{_esc(SITE_TITLE)}</a>
    <span class="tagline">{_esc(SITE_TAGLINE)}</span>
    <nav class="site-nav">
      <a href="{asset_prefix}index.html">政策列表</a>
      <a href="{asset_prefix}changes.html">变更流</a>
      <a href="https://github.com/passer-111/finreg-ai-cn" target="_blank" rel="noopener noreferrer">GitHub</a>
    </nav>
  </div>
</header>
<main class="wrap">
{body}
</main>
<footer class="site-footer">
  <div class="wrap">
    <p class="disclaimer">{DISCLAIMER_HTML}</p>
    <p class="muted">页面生成于 {_esc(generated_at)}。数据以 git 仓库为准，本站仅为只读视图；若与官方原文不符，以官方原文为准。</p>
  </div>
</footer>
<script src="{asset_prefix}assets/app.js"></script>
</body>
</html>
"""


# ============================================================
# 首页
# ============================================================

def _render_stats(policies: list[Policy]) -> str:
    """渲染首页顶部的统计条。

    只放四个数字，且每一个都是**使用者会真正据此做决定**的：
    总数、现行有效数、覆盖机构数、超期未核验数。
    刻意不放「关键词命中数」「抓取条数」这类运维指标——
    那是给维护者看的，属于 CI 报告，不属于对外站点。
    """
    # 政策总数
    total = len(policies)
    # 现行有效数
    valid = sum(1 for p in policies if p.status.value in CURRENTLY_VALID_STATUSES)
    # 覆盖机构数：按机构代码去重（同一机构的不同文件只算一个）
    issuers = len({p.issuer_code for p in policies if p.issuer_code})
    # 状态待核验数：这是最重要的一个「坏数字」，必须显式暴露
    unknown = sum(1 for p in policies if p.status.value == "unknown")
    # 组装统计卡片
    return f"""      <div class="stats">
        <div class="stat"><span class="stat-num">{total}</span><span class="stat-label">收录政策</span></div>
        <div class="stat"><span class="stat-num">{valid}</span><span class="stat-label">现行有效</span></div>
        <div class="stat"><span class="stat-num">{issuers}</span><span class="stat-label">覆盖机构</span></div>
        <div class="stat{' stat-warn' if unknown else ''}"><span class="stat-num">{unknown}</span><span class="stat-label">状态待核验</span></div>
      </div>"""


def _render_filters(policies: list[Policy]) -> str:
    """渲染筛选控件。

    筛选项的取值从**当前数据**里动态汇总，而不是写死枚举全集。
    理由：写死会把库里根本没有的机构也列出来，读者选中后得到空结果，
    会以为站点坏了；从数据汇总则保证「列出来的选项一定筛得出东西」。
    """
    # 汇总出现过的机构代码，按字母序保证输出稳定
    issuer_codes = sorted({p.issuer_code for p in policies if p.issuer_code})
    # 机构代码 → 中文名：同一代码可能对应多个机构名（联合发布），取任意一个即可
    issuer_names: dict[str, str] = {}
    # 逐个政策填充机构名
    for policy in policies:
        # 机构代码在模型里可为 None，这类记录无法进机构筛选，直接跳过。
        # 不跳过会让下面的 setdefault 收到 None 键，类型检查也会报错。
        if not policy.issuer_code:
            # 跳过没有机构代码的记录
            continue
        # 已有则跳过，保留首次出现的名称
        issuer_names.setdefault(policy.issuer_code, policy.issuer)
    # 汇总出现过的状态，按标签序输出
    statuses = sorted({p.status.value for p in policies})
    # 汇总出现过的相关度
    relevances = sorted({p.ai_relevance.value for p in policies})

    # 机构下拉选项
    issuer_options = "".join(
        f'<option value="{_esc(code)}">{_esc(issuer_names.get(code, code))}</option>'
        for code in issuer_codes
    )
    # 状态下拉选项
    status_options = "".join(
        f'<option value="{_esc(s)}">{_esc(STATUS_LABELS.get(s, s))}</option>'
        for s in statuses
    )
    # 相关度下拉选项
    relevance_options = "".join(
        f'<option value="{_esc(r)}">{_esc(AI_RELEVANCE_LABELS.get(r, r))}</option>'
        for r in relevances
    )

    # 组装筛选区。
    # noscript 提示不可省：筛选与结果计数都由客户端脚本驱动，
    # 禁用 JavaScript 时控件会「看起来能用、实际不动」——
    # 用户会以为是筛选坏了或库是空的。显式说明「当前显示全部记录」，
    # 与「明确失败优于静默错误」是同一条原则。
    return f"""      <div class="filters">
        <input type="search" id="q" class="search" placeholder="搜索标题、机构、义务内容、主题…" aria-label="搜索政策">
        <select id="f-status" aria-label="按状态筛选">
          <option value="">全部状态</option>
{status_options}
        </select>
        <select id="f-issuer" aria-label="按发布机构筛选">
          <option value="">全部机构</option>
{issuer_options}
        </select>
        <select id="f-relevance" aria-label="按 AI 相关度筛选">
          <option value="">全部相关度</option>
{relevance_options}
        </select>
        <button type="button" id="reset" class="reset">重置</button>
      </div>
      <noscript><p class="muted">搜索与筛选需要启用 JavaScript；当前显示全部记录。</p></noscript>
      <p class="result-line"><span id="result-count"></span></p>"""


def _render_policy_card(policy: Policy) -> str:
    """渲染一张政策卡片。

    ``data-*`` 属性承载筛选所需的机器可读取值，而不是让 JS 去解析中文文本。
    中文标签可能随措辞调整而变化，用它做筛选条件会让 JS 在改文案时静默失效。
    """
    # 状态标签与色调
    status_label = STATUS_LABELS.get(policy.status.value, policy.status.value)
    # 状态色调
    status_tone = STATUS_TONES.get(policy.status.value, "")
    # 生效日期一栏要区分三种情况。
    # 「有日期」「尚未生效」「现行有效但日期待核验」是三个不同的状态，
    # 一律显示成「未生效」会把一条现行有效的政策说成还没生效——这是会被误读的严重错误。
    if policy.effective_from is not None:
        # 有明确生效日期
        effective_text = policy.effective_from.isoformat()
    elif policy.status.value in CURRENTLY_VALID_STATUSES:
        # 现行有效却查不到施行日期：显式标注，这正是「待核验」的可见化
        effective_text = "日期待核验"
    else:
        # 确实尚未生效
        effective_text = "尚未生效"

    # 义务条数
    obligation_count = len(policy.key_obligations)
    # 版本数
    version_count = len(policy.versions)

    # 组装检索文本。
    #
    # 覆盖范围是刻意的，且与抓取层的 `filter_haystack` 保持同一原则：
    # **数据层匹配的范围要覆盖全部相关文本，不能只看标题**。
    # 使用者输入「提示词注入」时，应该能命中那条义务里写了提示词注入、
    # 而标题完全没提的政策——这是本站相对纯目录页的增值点。
    #
    # `issuer_code` 在模型里是可选字段（可以为 None），因此必须先滤掉空值再 join。
    # 直接 join 会在遇到 None 时抛 TypeError，让整个站点构建崩掉——
    # 而「一个可选字段没填」绝不该是这个后果。
    search_parts = (
        [policy.title, policy.issuer, policy.issuer_code]      # 标题与机构
        + [o.summary for o in policy.key_obligations]          # 义务正文
        + list(policy.topics)                                  # 主题标签
        + list(policy.domain)                                  # 所属领域
    )
    # 转字符串并丢弃空值（None 与空串都不会贡献可检索内容）
    search_blob = " ".join(str(part) for part in search_parts if part)
    # 返回卡片 HTML
    return f"""      <article class="card"
               data-status="{_esc(policy.status.value)}"
               data-issuer="{_esc(policy.issuer_code)}"
               data-relevance="{_esc(policy.ai_relevance.value)}"
               data-search="{_esc(search_blob)}">
        <div class="card-head">
          <h3><a href="policies/{_esc(policy.id)}.html">{_esc(policy.title)}</a></h3>
          {_badge(status_label, status_tone)}
        </div>
        <p class="card-meta">
          <span>{_esc(policy.issuer)}</span>
          <span class="sep">·</span>
          <span>{_esc(policy.instrument_type.value)}</span>
          <span class="sep">·</span>
          <span>{_esc(_label_of(BINDINGNESS_LABELS, policy.bindingness.value))}</span>
        </p>
        <p class="card-dates">公布 {_date_text(policy.published_on)}　生效 {_esc(effective_text)}　最近核验 {_date_text(policy.last_verified)}</p>
        <p class="card-chips">
          {_badge(_label_of(AI_RELEVANCE_LABELS, policy.ai_relevance.value), "quiet")}
          {_chips(policy.topics)}
        </p>
        <p class="card-foot muted">
          {obligation_count} 条关键义务 · {version_count} 个版本
          {_external_link(policy.source.url, "官方原文 ↗")}
        </p>
      </article>"""


def render_index(policies: list[Policy], change_documents: list[dict[str, Any]], generated_at: str) -> str:
    """渲染首页：统计条 + 筛选器 + 政策卡片列表。"""
    # 按公布日期倒序，最新的排最前
    ordered = sorted(policies, key=lambda p: p.published_on, reverse=True)
    # 渲染全部卡片
    cards = "\n".join(_render_policy_card(p) for p in ordered)
    # 无政策时给出明确说明，而不是留一片空白。
    # 「空」和「坏了」在页面上必须能区分开。
    if not cards:
        # 空状态提示
        cards = '      <p class="empty">当前库中还没有政策记录。</p>'

    # 计算「真实待办量」：变更流里尚未被人工录入为政策记录的唯一条目数。
    #
    # 为什么不能用「全部变更条目累加」
    # ------------------------------
    # 历史变更文件是**按天累积**的：一条三天前被发现、今天仍未录入的条目
    # 在每一份文件里都躺着，累加会把它数上好几遍；而已经被人工录入为
    # 正式记录的条目，继续算作「待处理」则是直接说谎。
    # 两种口径问题都会让首页数字失去意义——维护者看到「累计 500 条待处理」
    # 时无法判断到底还有多少事要做。
    #
    # 口径：只数 ``discovered``（待录入）类型、且链接未出现在政策记录中的条目，
    # 按链接去重。其它变更类型（首次收录、状态变更等）描述的是已录入记录
    # 自身的事件，不属于「待处理发现」。
    known_urls = {p.source.url for p in policies}
    # 待办条目的链接集合（去重）
    pending_urls: set[str] = set()
    # 逐份变更文件统计
    for doc in change_documents:
        # 逐条检查
        for change in doc.get("changes") or []:
            # 跳过形状不对的条目（与变更流页的容忍策略一致）
            if not isinstance(change, dict):
                # 下一条
                continue
            # 只统计待录入类型
            if change.get("change_type") != "discovered":
                # 下一条
                continue
            # 取出链接
            url = change.get("url")
            # 已录入为正式记录的不再是待办；无链接的无法对账，不算入待办量
            if url and str(url) not in known_urls:
                # 登记待办
                pending_urls.add(str(url))
    # 待办量
    change_total = len(pending_urls)
    # 最新一次变更的日期
    latest_change_day = ""
    # 逐份查找第一个有日期的
    for doc in change_documents:
        # 取检测日期
        day = doc.get("detected_on")
        # 找到即停止
        if day:
            # 记录
            latest_change_day = str(day)
            # 跳出
            break
    # 变更流摘要的后半句。
    # 【为什么在这里先算好再插值】f-string 的表达式部分若再嵌一层同类引号，
    # 在 Python 3.12 之前会直接语法报错，而本项目 mypy 目标版本是 3.10、
    # CI 还要跑多个解释器。把条件表达式提到 f-string 之外，是唯一跨版本安全的写法。
    latest_text = f"，最近一次在 {_esc(latest_change_day)}" if latest_change_day else ""

    # 组装主体。
    # 覆盖局限声明刻意不写具体条数：统计条里的数字由数据实时汇总，
    # 声明里再硬编码一份，两边必然在将来某天对不上——
    # 「文档承诺 = 实际行为」这条纪律对页面文案同样成立。
    body = f"""  <section class="hero">
    <h1>{_esc(SITE_TITLE)}</h1>
    <p class="lede">中国金融领域人工智能合规政策的<b>活</b>知识库。与静态法规汇编的区别在于：每条记录都带生效状态、版本链与官方原文链接，并且我们持续跟踪它<b>什么时候变了</b>。</p>
{_render_stats(policies)}
    <p class="muted">覆盖范围：本站是持续建设中的专题知识库，只收录「金融领域 × 人工智能合规」交叉处的监管文件，<b>不构成对监管要求的完整枚举</b>。是否存在某项合规义务，请以官方原文与专业机构意见为准。</p>
  </section>

  <section class="panel">
    <h2>变更流</h2>
    <p class="muted">抓取器每日扫描官方站点，把「新出现的条目」写进变更流。这些条目<b>尚未经人工认定</b>，因此不在上方政策列表中——它们需要人打开原文核对后才能提升为正式记录。</p>
    <p>当前 <b>{change_total}</b> 条待处理发现（已按链接去重，不含已录入为正式记录的条目）{latest_text}。 <a href="changes.html">查看完整变更流 →</a></p>
  </section>

  <section class="panel">
    <h2>政策列表</h2>
{_render_filters(policies)}
    <div class="cards" id="cards">
{cards}
    </div>
  </section>"""

    # 返回完整页面
    return _page_shell(title="", body=body, asset_prefix="", generated_at=generated_at)


# ============================================================
# 政策详情页
# ============================================================

def _render_timeline(policy: Policy) -> str:
    """渲染版本链。

    这是本站区别于任何静态法规汇编的地方，因此单独成块、放在显眼位置。
    版本按序号升序排列——读版本链的正确方向是从旧到新，
    倒序会让人把最新版误当成起点。
    """
    # 无版本信息时直接说明，不渲染空列表
    if not policy.versions:
        # 提示缺失
        return '<p class="muted">该记录尚未登记版本信息。</p>'

    # 逐版本渲染
    items: list[str] = []
    # 按版本号升序
    for version in sorted(policy.versions, key=lambda v: v.version):
        # 该版本的时间范围文本
        span = f"{_date_text(version.published_on)} 公布"
        # 生效日存在时补上
        if version.effective_from is not None:
            # 追加生效日
            span += f"，{_date_text(version.effective_from)} 生效"
        # 失效日存在时补上
        if version.effective_until is not None:
            # 追加失效日
            span += f"，{_date_text(version.effective_until)} 失效"
        # 变更说明：人工提炼的字段，缺失时明确说明「未填写」
        note = version.change_note or "（未填写变更说明）"
        # 组装单条
        items.append(f"""        <li>
          <div class="tl-head">
            <span class="tl-version">v{_esc(version.version)}</span>
            {_status_badge(version.status.value)}
            <span class="muted">{_esc(span)}</span>
          </div>
          <p class="tl-note">{_esc(note)}</p>
          <p class="muted">{_external_link(version.url, "该版本官方原文 ↗")}</p>
        </li>""")

    # 组装时间轴
    return '      <ol class="timeline">\n' + "\n".join(items) + "\n      </ol>"


def _render_obligations(policy: Policy) -> str:
    """渲染关键义务清单。

    这是本项目的核心增值内容：爬虫只能给原文，提炼才能给可执行的义务清单。
    因此每条都必须显示**条款定位**——使用者要能拿着它回原文核对；
    没有条款定位的概括，在合规场景里是不可举证的。
    """
    # 无义务时说明情况
    if not policy.key_obligations:
        # 提示
        return '<p class="muted">该记录尚未提炼关键义务。</p>'

    # 逐条渲染
    rows: list[str] = []
    # 按原始顺序输出：义务清单的顺序通常对应原文条款顺序，重排会破坏可核对性
    for obligation in policy.key_obligations:
        # 义务类型徽章（可能为空）
        type_badge = ""
        # 有类型时渲染
        if obligation.obligation_type:
            # 取中文标签
            type_badge = _badge(_label_of(OBLIGATION_TYPE_LABELS, obligation.obligation_type), "quiet")
        # 组装单条
        rows.append(f"""        <li class="obligation">
          <div class="obligation-clause">{_esc(obligation.clause)} {type_badge}</div>
          <div class="obligation-summary">{_esc(obligation.summary)}</div>
          <div class="obligation-tags">{_chips(obligation.tags)}</div>
        </li>""")

    # 组装列表
    return '      <ul class="obligations">\n' + "\n".join(rows) + "\n      </ul>"


def _render_meta_table(policy: Policy) -> str:
    """渲染元信息表。

    字段顺序按「使用者判断是否需要关心」的优先级排列，而不是按 schema 顺序：
    先适用主体（要不要看）→ 再效力（有多大压力）→ 最后是标识类信息。
    """
    # 逐行构造「名称 / 值」对
    rows: list[tuple[str, str]] = [
        # 发布机构
        ("发布机构", _esc(policy.issuer)),
        # 发文字号
        ("发文字号", _or_placeholder(policy.doc_number)),
        # 文件层级
        ("文件层级", _esc(policy.instrument_type.value)),
        # 约束力
        ("约束力", _esc(_label_of(BINDINGNESS_LABELS, policy.bindingness.value))),
        # AI 相关度
        ("AI 相关度", _esc(_label_of(AI_RELEVANCE_LABELS, policy.ai_relevance.value))),
        # 所属领域
        ("所属领域", _chips(policy.domain)),
        # 主题标签
        ("主题标签", _chips(policy.topics)),
        # 适用主体：最重要的一项，放在显眼位置
        ("适用主体", _render_applicable(policy)),
        # 官方链接
        ("官方原文", _external_link(policy.source.url, policy.source.url or "")),
        # 来源站点与等级
        ("来源站点", f"{_esc(policy.source.site)}（{_esc(_label_of(SOURCE_TIER_LABELS, policy.source.tier.value))}）"),
    ]
    # 文档编号之外的标识信息单独一组，放在最后
    rows.append(("记录标识", f'<code>{_esc(policy.id)}</code>'))
    # 拼接表格行
    body = "\n".join(
        f'        <tr><th>{name}</th><td>{value}</td></tr>' for name, value in rows
    )
    # 返回表格
    return f'      <table class="meta">\n{body}\n      </table>'


def _render_applicable(policy: Policy) -> str:
    """渲染适用主体。

    适用主体常常是一长串官方表述，直接铺开会淹没其他字段，
    因此包在 ``details`` 里折起来；但**关键字眼不能折**——
    折叠的目的是省空间，不是让人少看到关键信息。
    """
    # 无内容时占位
    if not policy.applicable_to:
        # 占位
        return '<span class="muted">—</span>'
    # 组装可折叠列表
    items = "".join(f"<li>{_esc(item)}</li>" for item in policy.applicable_to)
    # 返回
    return f'<details class="applicable"><summary>共 {len(policy.applicable_to)} 类主体</summary><ul>{items}</ul></details>'


def _render_ledger(policy: Policy) -> str:
    """渲染核验台账。

    这一段存在的意义是让「这条记录有多可信」变成可判断的，
    而不是让人凭感觉相信。因此 `verified_by` 的中文标签必须写足含义——
    「机器核验」四个字必须带上「未经人工逐项核对」这个信任边界。
    """
    # 核验方式
    verified_label = _label_of(VERIFIED_BY_LABELS, policy.verified_by.value)
    # 核验方式为自动时给出警示色调：这不是错误，但使用者有权知道
    verified_tone = "warn" if policy.verified_by.value == "automated" else "ok"
    # 核验说明：可能为空
    note_html = (
        f'<p class="ledger-note">{_esc(policy.verified_note)}</p>'
        if policy.verified_note
        else '<p class="muted">（核验说明未填写）</p>'
    )
    # 组装
    return f"""      <div class="ledger">
        <p>最近核验：<b>{_date_text(policy.last_verified)}</b> {_badge(verified_label, verified_tone)}</p>
        {note_html}
      </div>"""


def _render_version_links(policy: Policy) -> str:
    """渲染取代与被取代关系（版本链的横向指针）。"""
    # 收集非空关系
    parts: list[str] = []
    # 本条取代了哪些
    if policy.supersedes:
        # 带上 id，便于使用者据此查找
        parts.append("取代：" + _chips(policy.supersedes, "chip ref"))
    # 本条被谁取代
    if policy.superseded_by:
        # 单值
        parts.append("被取代：" + _chips([policy.superseded_by], "chip ref"))
    # 本条修订了哪些
    if policy.amends:
        # 列表
        parts.append("修订：" + _chips(policy.amends, "chip ref"))
    # 本条被谁修订
    if policy.amended_by:
        # 列表
        parts.append("被修订：" + _chips(policy.amended_by, "chip ref"))
    # 全部为空说明这条记录是版本链上的孤立节点，明确说明而非留白
    if not parts:
        # 提示
        return '<p class="muted">该记录在版本链上无关联文件。</p>'
    # 组装
    return "".join(f'<p class="ref-line">{part}</p>' for part in parts)


def render_policy_page(policy: Policy, generated_at: str) -> str:
    """渲染单条政策的详情页。"""
    # 页头
    header = f"""  <nav class="breadcrumb"><a href="../index.html">← 返回政策列表</a></nav>
  <article class="detail">
    <header class="detail-head">
      <div class="detail-title">
        <h1>{_esc(policy.title)}</h1>
        {_status_badge(policy.status.value)}
      </div>
      <p class="detail-sub">{_esc(policy.issuer)}</p>
    </header>"""

    # 时效性区块。放在最前，因为「这条现在还有效吗」是使用者第一个问题。
    timing = f"""    <section class="block">
      <h2>时效性</h2>
      <table class="meta">
        <tr><th>公布日期</th><td>{_date_text(policy.published_on)}</td></tr>
        <tr><th>生效日期</th><td>{_effective_row(policy)}</td></tr>
        <tr><th>失效日期</th><td>{_date_text(policy.effective_until, "（无固定期限）")}</td></tr>
      </table>
      <h3>版本链</h3>
{_render_timeline(policy)}
      <h3>关联文件</h3>
{_render_version_links(policy)}
    </section>"""

    # 义务区块
    obligations = f"""    <section class="block">
      <h2>关键义务（{len(policy.key_obligations)} 条）</h2>
      <p class="muted">以下为人工提炼的义务清单，每条均标注原文条款位置，可据此回源核对。</p>
{_render_obligations(policy)}
    </section>"""

    # 元信息区块
    meta = f"""    <section class="block">
      <h2>基本信息</h2>
{_render_meta_table(policy)}
    </section>"""

    # 备注区块
    notes = ""
    # 有备注才渲染
    if policy.notes:
        # 备注是人工撰写的解释性文字，往往包含「适用性判断」这类最关键的内容
        notes = f"""    <section class="block">
      <h2>备注</h2>
      <div class="notes">{_esc(policy.notes)}</div>
    </section>"""

    # 核验台账区块
    ledger = f"""    <section class="block">
      <h2>核验台账</h2>
{_render_ledger(policy)}
    </section>"""

    # 溯源区块
    provenance = f"""    <section class="block">
      <h2>来源溯源</h2>
      <table class="meta">
        <tr><th>抓取时间</th><td>{_or_placeholder(policy.source.fetched_at)}</td></tr>
        <tr><th>HTTP 状态</th><td>{_or_placeholder(policy.source.http_status, "未执行抓取")}</td></tr>
        <tr><th>内容摘要</th><td>{_hash_cell(policy.source.content_hash)}</td></tr>
        <tr><th>原始快照</th><td>{_or_placeholder(policy.source.snapshot_path, "未保存快照")}</td></tr>
      </table>
    </section>"""

    # 组装主体
    body = "\n\n".join([header, timing, obligations, meta, notes, ledger, provenance])
    # 收尾
    body += "\n  </article>"
    # 返回完整页面。标题用政策全称，便于标签页与分享预览
    return _page_shell(title=policy.title, body=body, asset_prefix="../", generated_at=generated_at)


def _effective_row(policy: Policy) -> str:
    """渲染详情页里「生效日期」一栏。

    与列表页一样必须区分三种情况，且这里要把「待核验」的原因指出来——
    详情页有足够空间，不该只给一个占位符了事。
    """
    # 有日期直接用
    if policy.effective_from is not None:
        # 正常情况
        return _date_text(policy.effective_from)
    # 现行有效但缺日期：明确标注待核验
    if policy.status.value in CURRENTLY_VALID_STATUSES:
        # 给出提示并指向核验说明
        return '<span class="warn-text">待核验</span><span class="muted">（该政策现行有效，但施行日期尚未确认，详见核验台账）</span>'
    # 尚未生效
    return '<span class="muted">尚未生效</span>'


def _hash_cell(value: Any) -> str:
    """渲染内容哈希。哈希很长，显示完整值会破坏表格排版，因此用等宽短写。"""
    # 无哈希时占位
    if not value:
        # 占位
        return '<span class="muted">未计算</span>'
    # 有哈希时用等宽字体展示，允许横向滚动
    return f'<code class="hash">{_esc(value)}</code>'


# ============================================================
# 变更流页
# ============================================================

def render_changes_page(change_documents: list[dict[str, Any]], generated_at: str) -> str:
    """渲染变更流页面。

    这一页的存在本身就是对竞品的差异点：静态法规汇编只能告诉你「现在有什么」，
    变更流能告诉你「什么时候变的、变成了什么」。
    """
    # 无变更文件时给出明确说明
    if not change_documents:
        # 空状态
        body = """  <nav class="breadcrumb"><a href="index.html">← 返回政策列表</a></nav>
  <h1>变更流</h1>
  <p class="empty">尚无变更记录。抓取流水线每次运行会把「新发现的条目」写入 data/changes/YYYY-MM-DD.json。</p>"""
        # 返回
        return _page_shell(title="变更流", body=body, asset_prefix="", generated_at=generated_at)

    # 逐日渲染
    sections: list[str] = []
    # 遍历变更文件（已按日期倒序）
    for doc in change_documents:
        # 先把这一天文件里的值全部取成普通局部变量，再拼 HTML。
        # 【为什么必须提前取值】变更文件是外部 JSON，字段要经过 dict.get() 访问，
        # 而 dict.get("x") 里带引号，直接塞进 f-string 的表达式部分会在
        # Python 3.12 之前报语法错（本项目 mypy 目标 3.10、CI 跑多解释器）。
        # 提前取值同时也让「缺字段时渲染成什么样」有唯一一处可改的地方。
        file_name = doc.get("_unreadable")
        file_error = doc.get("_error")
        day_text = doc.get("detected_on")
        scanned = doc.get("policies_scanned")
        changes = doc.get("changes") or []

        # 该文件不可读时显式报警，绝不静默跳过——
        # 「读不出来」与「当天没有变化」在页面上必须能区分开。
        if file_name:
            # 渲染警告块
            sections.append(f"""    <section class="block block-warn">
      <h2>{_esc(file_name)}</h2>
      <p class="warn-text">该变更文件无法解析，已跳过：{_esc(file_error)}</p>
      <p class="muted">这不是「当天没有变化」，而是「这一天的记录读不出来」。请在仓库中检查该文件。</p>
    </section>""")
            # 继续下一个文件
            continue

        # 逐条渲染
        rows: list[str] = []
        # 遍历
        for change in changes:
            # 同样先取值后拼串：dict 访问带引号，不能直接写进 f-string 表达式
            raw_type = change.get("change_type")
            # 变更类型中文标签：未知取值回退为原值，宁可显示英文也不隐藏信息
            type_label = CHANGE_TYPE_LABELS.get(str(raw_type), str(raw_type))
            # 重要度中文标签
            significance_label = _label_of(SIGNIFICANCE_LABELS, change.get("significance"))
            # 该条目的检测日期
            change_day = change.get("detected_on")
            # 条目标题（机器抄录的官方标题）
            change_title = change.get("title")
            # 明细说明
            change_detail = change.get("detail")
            # 官方链接
            change_url = change.get("url")
            # 组装一条
            rows.append(f"""      <li class="change">
        <div class="change-head">
          {_badge(type_label, "quiet")}
          <span class="muted">{_esc(significance_label)}</span>
          <span class="muted">{_esc(change_day)}</span>
        </div>
        <div class="change-title">{_esc(change_title)}</div>
        <div class="change-detail muted">{_esc(change_detail)}</div>
        <div class="change-foot">{_external_link(change_url, "查看原文 ↗")}</div>
      </li>""")
        # 无明细时的说明
        list_html = (
            '      <ul class="changes">\n' + "\n".join(rows) + "\n      </ul>"
            if rows
            else '      <p class="muted">这一天没有发现新的条目。</p>'
        )
        # 组装当天的区块
        sections.append(f"""    <section class="block">
      <h2>{_esc(day_text)}</h2>
      <p class="muted">扫描 {_esc(scanned)} 条已收录政策，发现 {len(changes)} 条待处理条目。</p>
{list_html}
    </section>""")

    # 组装主体
    body = f"""  <nav class="breadcrumb"><a href="index.html">← 返回政策列表</a></nav>
  <h1>变更流</h1>
  <p class="lede">抓取器每日扫描官方站点列出栏，把<b>此前未见过</b>的条目写入这里。它们<b>尚未经人工认定</b>，因此不计入政策列表——需要人打开原文核对后，才能提升为正式记录。这条界线是刻意的：<b>机器负责发现，人负责认定</b>。</p>

{chr(10).join(sections)}"""
    # 返回
    return _page_shell(title="变更流", body=body, asset_prefix="", generated_at=generated_at)


# ============================================================
# 构建入口
# ============================================================

@dataclass
class SiteBuildResult:
    """站点构建结果，供 CLI 渲染与测试断言。"""

    # 输出目录
    output_dir: str
    # 写入的页面数量（含首页与变更流页）
    page_count: int
    # 收录的政策数
    policy_count: int
    # 汇总的变更条目数
    change_count: int
    # 已写入的相对路径清单（相对输出目录），便于测试逐项断言
    written: list[str] = field(default_factory=list)
    # **构建本身**的问题：变更文件损坏、静态资源目录缺失等。
    # 这一类问题会让产出物不完整，必须显式报出。
    problems: list[str] = field(default_factory=list)
    # **数据记录**层面的校验提示条数（如「现行有效但尚未人工核验」）。
    #
    # 【为什么不逐条列出】这些提示本来就由 `finreg validate` 负责呈现，
    # 且已经被 CI 的 validate.yml 挡住。若在每次构建时把它们全打出来，
    # 十几行告警会把上面那几行真正的构建问题淹没——
    # 告警太多，人就会习惯性忽略，这比不告警更糟。
    # 因此只给**条数**，把「看详情」的动作导回它本该在的地方。
    record_issue_count: int = 0
    # 生成时间戳（ISO 8601 带时区）
    generated_at: str = ""


def build_site(
    output_dir: Path | None = None,
    *,
    site_source_dir: Path | None = None,
    policies: dict[str, Policy] | None = None,
    change_documents: list[dict[str, Any]] | None = None,
    generated_at: datetime | None = None,
) -> SiteBuildResult:
    """生成静态站点。

    所有外部输入都可注入（``policies`` / ``change_documents`` / ``generated_at``），
    这是刻意的：站点构建必须能在**完全离线**的情况下被测试。
    本项目的抓取器已经全部支持离线 fixtures，站点构建若反过来依赖网络或
    依赖「今天是哪天」，就会引入本项目的头号大敌——会自行变红的测试。

    参数 ``generated_at`` 同理：默认取当前北京时间，但测试必须能钉死它，
    否则页脚里那句「页面生成于…」会让每次构建的产物都不一致。
    """
    # 确定输出目录
    out = DEFAULT_OUTPUT_DIR if output_dir is None else output_dir
    # 统一成 Path，允许调用方传字符串
    out = Path(out)
    # 确定静态源目录
    src = SITE_SOURCE_DIR if site_source_dir is None else Path(site_source_dir)

    # 收集构建本身的问题清单
    problems: list[str] = []
    # 数据记录层面的校验提示（不逐条输出，只计数）
    record_issue_count = 0

    # 加载政策：未注入时从仓库读取
    if policies is None:
        # 加载并保留加载期问题
        loaded, load_errors = load_all_policies()
        # 收进结果
        policies = loaded
        # 加载问题不阻断构建——站点要能展示「已被承认的那部分」，
        # 而不是因为一条记录有问题就什么都不发布。
        # 但也绝不能丢弃：计数后由 CLI 提示「去看 finreg validate」。
        record_issue_count = len(load_errors)

    # 加载变更：未注入时从仓库读取
    if change_documents is None:
        # 读取
        change_documents = load_change_documents()

    # 检查不可读标记。
    # 【为什么放在 if 外面】这一步属于**构建**的职责，不属于**加载**的职责：
    # 调用方（含测试）可以直接注入一份变更文档，而构建同样必须如实报告
    # 「这份文档读不出来」。若把检查塞进上面的分支，注入路径就会静默绕过它——
    # 那等于让「出错时会不会报警」取决于数据从哪来，这是最不该有的不确定性。
    for doc in change_documents:
        # 有不可读标记
        if doc.get("_unreadable"):
            # 记录问题
            problems.append(f"变更文件 {doc['_unreadable']} 无法解析：{doc.get('_error')}")

    # 生成时间戳：默认取当前北京时间。
    # 【为什么不能用本机时区】astimezone() 会跟着构建机的时区走：
    # GitHub 运行器在 UTC，页脚会显示 +0000，与抓取层时间戳（一律 +08:00，
    # 见 fetchers.base.now_china_iso）口径不一，读者无从判断两个时间是否同一时刻。
    # 因此这里与抓取层共用 CHINA_TZ 常量，而不是各自取本地时区。
    if generated_at is None:
        # 用中国大陆时区的当前时刻
        stamp = datetime.now(CHINA_TZ)
    else:
        # 使用注入的时间
        stamp = generated_at
    # 格式化为「YYYY-MM-DD HH:MM（时区）」，只到分钟以减少无意义的差异
    generated_text = stamp.strftime("%Y-%m-%d %H:%M %z")

    # 排序后的政策列表
    policy_list = sorted(policies.values(), key=lambda p: p.published_on, reverse=True)

    # 记录写入过的相对路径
    written: list[str] = []

    # 写入文本文件的内部小工具：统一编码与换行，并登记路径
    def _write(relative: str, content: str) -> None:
        """写入一个文本文件并登记。"""
        # 计算完整路径
        target = out / relative
        # 确保父目录存在
        target.parent.mkdir(parents=True, exist_ok=True)
        # 显式用 UTF-8 且固定换行为 LF。
        # 站点是机器生成的，统一 LF 才能保证「同一份数据在 Windows 与 Linux 上
        # 生成的产物逐字节一致」——否则 diff 里会混进整文件的换行差异。
        target.write_text(content, encoding="utf-8", newline="\n")
        # 登记
        written.append(relative)

    # 首页
    _write("index.html", render_index(policy_list, change_documents, generated_text))
    # 变更流页
    _write("changes.html", render_changes_page(change_documents, generated_text))
    # 逐条政策详情页
    for policy in policy_list:
        # 写入 policies/<id>.html
        _write(f"policies/{policy.id}.html", render_policy_page(policy, generated_text))

    # 机器可读的 JSON API。
    # 这一份是给程序用的，因此保留原始枚举取值（不复用中文标签），
    # 顺序与字段名与 schema 一致，使消费者不必解析 HTML。
    api = {
        # 生成时间
        "generated_at": generated_text,
        # 记录条数
        "count": len(policy_list),
        # 记录本体
        "policies": [_policy_to_site_dict(p) for p in policy_list],
    }
    # 写入，缩进 2 空格便于人工 diff（缩进 2 会让文件大一些，但可读性优先）
    _write("data/policies.json", json.dumps(api, ensure_ascii=False, indent=2) + "\n")

    # 变更流 API
    changes_api = {
        # 生成时间
        "generated_at": generated_text,
        # 变更文件原样透传（保持与仓库内文件同构，消费者可复用同一套解析）
        "days": change_documents,
    }
    # 写入
    _write("data/changes.json", json.dumps(changes_api, ensure_ascii=False, indent=2) + "\n")

    # 复制静态资源。
    # 目录不存在时记一个问题但不中断——CSS 缺失只会让页面变丑，不会让内容丢失，
    # 而内容才是这个站的全部价值。
    assets_src = src / "assets"
    # 检查源目录
    if assets_src.is_dir():
        # 逐个文件复制
        for asset in sorted(assets_src.iterdir()):
            # 只复制文件，跳过子目录（当前没有，但避免将来静默漏拷）
            if not asset.is_file():
                # 跳过
                continue
            # 目标路径
            target = out / "assets" / asset.name
            # 建目录
            target.parent.mkdir(parents=True, exist_ok=True)
            # 复制
            shutil.copyfile(asset, target)
            # 登记
            written.append(f"assets/{asset.name}")
    else:
        # 记录缺失，明确报出而不是静默产出无样式页面
        problems.append(f"静态资源目录不存在：{assets_src}（页面将没有样式与检索脚本）")

    # 写入 .nojekyll。
    # GitHub Pages 默认会把下划线开头的文件与目录交给 Jekyll 处理并直接忽略，
    # 这个空文件让 Pages 跳过 Jekyll。走 Actions 产物发布时它不是必需的，
    # 但只要有人把发布方式改成「直接读分支目录」，少了它就会静默丢文件。
    _write(".nojekyll", "")

    # 汇总变更条目数
    change_total = sum(len(d.get("changes") or []) for d in change_documents)
    # 返回结果
    return SiteBuildResult(
        output_dir=str(out),                       # 输出目录
        page_count=2 + len(policy_list),           # 首页 + 变更流 + 各详情页
        policy_count=len(policy_list),             # 政策数
        change_count=change_total,                 # 变更条目数
        written=written,                           # 写入清单
        problems=problems,                         # 构建本身的问题
        record_issue_count=record_issue_count,     # 数据记录校验提示数
        generated_at=generated_text,               # 时间戳
    )


# 模块对外接口。显式声明是为了让「这个模块是干什么的」一眼可见，
# 也避免调用方误用下划线开头的内部渲染函数。
__all__ = [
    "DEFAULT_OUTPUT_DIR",     # 默认输出目录
    "SITE_SOURCE_DIR",        # 静态源目录
    "SiteBuildResult",        # 构建结果类型
    "build_site",             # 构建入口
    "load_change_documents",  # 变更文件加载
    "render_changes_page",    # 变更流页渲染
    "render_index",           # 首页渲染
    "render_policy_page",     # 详情页渲染
]
