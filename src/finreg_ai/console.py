"""本地核验台界面 —— ``finreg verify --serve`` 的网页工作台。

为什么是一个本地网页而不是 TUI
------------------------------
核验动作要求人「逐字段看证据、点按钮判断」。证据是成段的法律原文，
终端里翻页比对的体验足以劝退任何使用者——而工具一旦难用，
人就会回到「直接手改 YAML」的老路上，台账与状态机全部落空。

技术约束（与全仓库一致的硬性约定）
----------------------------------
- **标准库 http.server**，只监听 127.0.0.1——核验台能写 data/policies/，
  绝不能暴露到局域网；绑定 0.0.0.0 在这里不是便利，是事故。
- **零框架零构建**：HTML 由本模块直接渲染，CSS 复用 site/assets/style.css，
  键盘流是一小段手写 JS（无 npm / CDN / 框架）。
- **机器不做认定**：界面上唯一能让记录变成 verified_by=human 的元素
  是「核验通过并入库」按钮，且它在决策状态机判定「全部待判断项已处理」
  之前保持置灰；服务端用同一份 blocked_reasons 再拦一次。

界面测试不测像素：只测决策状态机（ConsoleState）与一次
本地 HTTP 冒烟（页面能渲染、判断能提交）。
"""

# 导入 html 用于转义一切进入页面的文本（证据摘录含原文，必须防注入）
import html
# 导入 mimetypes 用于静态资源的 Content-Type
import mimetypes
# 导入 ThreadingHTTPServer 与请求处理器基类（标准库，零新增依赖）
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
# 导入 Path 用于定位静态资源与数据目录
from pathlib import Path
# 导入 url 解析工具：路由与查询串解析，quote 用于重定向地址编码
from urllib.parse import parse_qs, quote, urlparse
# 导入 Any 用于类型标注
from typing import Any

# 从存取层复用项目根（静态资源在 site/assets 下）
from finreg_ai.store import POLICIES_DIR, PROJECT_ROOT
# 从抓取器基类复用北京时间工具
from finreg_ai.fetchers.base import today_china
# 从取证层复用证据卡构建与草稿加载
from finreg_ai.verify import (
    DRAFTS_DIR,              # 草稿目录（默认数据源）
    STATUS_ERROR,            # 红：证据冲突
    STATUS_MANUAL,           # 需人工核
    STATUS_OK,               # 绿：机器通过
    STATUS_WARNING,          # 黄：待判断
    CheckResult,             # 检查结果（类型标注用）
    EvidenceCard,            # 证据卡
    build_evidence_card,     # 单条出证（重新取证用）
    load_drafts,             # 草稿加载
)
# 从入库模块复用决策状态机与入库动作
from finreg_ai.promote import (
    VERIFICATION_LOG_DIR,    # 台账目录
    Decision,                # 一次判断
    PromoteResult,           # 入库结果
    blocked_reasons,         # 决策状态机（界面置灰与服务端防线共用）
    log_decision,            # 判断留痕
    promote_draft,           # 入库动作
)

# 静态资源目录（复用对外站点的样式与图标，不维护第二份 CSS）
SITE_ASSETS_DIR = PROJECT_ROOT / "site" / "assets"

# 默认监听端口。8765 是冷门端口，避开常见开发服务（3000/8000/8080）。
DEFAULT_PORT = 8765

# 绑定地址：只允许回环——见模块文档的安全说明
BIND_HOST = "127.0.0.1"


# ============================================================
# 决策状态机（界面与服务端共用的会话状态）
# ============================================================

class ConsoleState:
    """一次核验台会话的全部状态：草稿、证据卡、判断、已入库记录。

    刻意做成与 HTTP 无关的纯 Python 对象：界面层（BaseHTTPRequestHandler）
    只做「解析请求 → 调这里 → 渲染结果」，所有决策逻辑都能离线测试。
    """

    def __init__(
        self,
        drafts_dir: Path | None = None,         # 草稿目录（测试注入临时目录）
        policies_dir: Path | None = None,       # 政策目录（测试注入临时目录）
        log_dir: Path | None = None,            # 台账目录（测试注入临时目录）
        session: Any | None = None,             # HTTP 会话（测试注入 FakeSession）
    ) -> None:
        """加载草稿；证据卡延迟到首次需要时再构建（抓取有网络成本）。"""
        # 保存目录配置
        self.drafts_dir = drafts_dir or DRAFTS_DIR
        # 政策目录
        self.policies_dir = policies_dir or POLICIES_DIR
        # 台账目录
        self.log_dir = log_dir or VERIFICATION_LOG_DIR
        # 保存会话（None 时真实抓取会自建）
        self.session = session
        # 加载草稿
        self.drafts, self.load_errors = load_drafts(self.drafts_dir)
        # 证据卡缓存：id -> card（首次访问时构建）
        self.cards: dict[str, EvidenceCard] = {}
        # 判断记录：id -> 判断列表。只在内存中——点击瞬间已写入 JSONL 台账，
        # 内存态只是「本次会话的决策进度」，服务重启后进度清零但台账仍在。
        # 这是有意的：台账是持久真相，内存态只是工作台面上的草稿纸。
        self.decisions: dict[str, list[Decision]] = {}
        # 本次会话已入库的 id 列表（左侧清单的 ✓）
        self.done: list[str] = []

    def get_card(self, policy_id: str) -> EvidenceCard | None:
        """取一张证据卡；未构建时现场构建（一次 HTTP 抓取）。"""
        # 草稿不存在时返回 None（调用方渲染 404 提示）
        if policy_id not in self.drafts:
            # 返回空
            return None
        # 未缓存时构建
        if policy_id not in self.cards:
            # 对单条出证并缓存
            self.cards[policy_id] = build_evidence_card(self.drafts[policy_id], self.session)
        # 返回缓存
        return self.cards[policy_id]

    def refetch(self, policy_id: str) -> EvidenceCard | None:
        """重新取证（丢弃缓存重新抓取）。用于网络抖动后的人工重试。"""
        # 草稿不存在时返回 None
        if policy_id not in self.drafts:
            # 返回空
            return None
        # 重新出证并覆盖缓存
        self.cards[policy_id] = build_evidence_card(self.drafts[policy_id], self.session)
        # 返回新卡
        return self.cards[policy_id]

    def decide(self, policy_id: str, check_id: str, option_key: str) -> Decision:
        """记录一次判断：校验选项合法 → 存内存 → 写台账。

        非法的 check_id / option_key 直接抛 ValueError——
        「点了不存在的按钮」只可能来自手工构造的请求，
        静默忽略它等于假装判断发生过。
        """
        # 取证据卡
        card = self.get_card(policy_id)
        # 草稿不存在
        if card is None:
            # 明确报错
            raise ValueError(f"未找到草稿：{policy_id}")
        # 找检查项
        check = next((c for c in card.checks if c.check_id == check_id), None)
        # 检查项不存在
        if check is None:
            # 明确报错
            raise ValueError(f"证据卡中没有检查项：{check_id}")
        # 找选项
        option = next((o for o in check.options if o.key == option_key), None)
        # 选项不存在
        if option is None:
            # 明确报错
            raise ValueError(f"检查项 {check_id} 没有选项：{option_key}")
        # 构造判断
        decision = Decision(
            check_id=check_id,          # 检查项
            option_key=option_key,      # 选项键
            label=option.label,         # 判断表述
            action=option.action,       # 结构化动作
        )
        # 同一检查项的重复判断以最后一次为准（人改主意是常态，
        # 台账里两次点击都在，内存态取最新——审计序列与当前状态不矛盾）
        existing = self.decisions.setdefault(policy_id, [])
        # 移除该检查项的旧判断
        existing[:] = [d for d in existing if d.check_id != check_id]
        # 追加新判断
        existing.append(decision)
        # 写台账：证据摘要 = 机器结论 + 首条证据
        evidence_summary = check.summary + (f"｜{check.evidence[0][:150]}" if check.evidence else "")
        # 追加台账
        log_decision(self.log_dir, policy_id, decision, evidence_summary, today_china())
        # 返回判断
        return decision

    def pending_reasons(self, policy_id: str) -> list[str]:
        """该草稿还不能入库的原因列表（空 = 可以入库）。"""
        # 取证据卡
        card = self.get_card(policy_id)
        # 草稿不存在时返回一个明确原因
        if card is None:
            # 返回原因
            return [f"未找到草稿：{policy_id}"]
        # 委托给决策状态机（与入库动作共用同一份逻辑）
        return blocked_reasons(card, self.decisions.get(policy_id, []))

    def promote(self, policy_id: str) -> PromoteResult:
        """执行入库。决策状态机判定不可入库时，promote_draft 会拒绝。"""
        # 取证据卡（不存在时构造一个失败结果）
        card = self.get_card(policy_id)
        # 草稿不存在
        if card is None:
            # 返回失败结果
            return PromoteResult(ok=False, policy_id=policy_id, messages=[f"未找到草稿：{policy_id}"])
        # 调入库动作（状态机在其中再拦一次）
        result = promote_draft(
            policy_id,                              # 草稿 id
            self.decisions.get(policy_id, []),      # 本次会话的判断
            card,                                   # 证据卡
            drafts_dir=self.drafts_dir,             # 草稿目录
            policies_dir=self.policies_dir,         # 政策目录
            log_dir=self.log_dir,                   # 台账目录
        )
        # 成功时更新会话状态
        if result.ok:
            # 登记已入库
            self.done.append(policy_id)
            # 从待核验清单移除
            self.drafts.pop(policy_id, None)
            # 清缓存与判断进度
            self.cards.pop(policy_id, None)
            # 清判断
            self.decisions.pop(policy_id, None)
        # 返回结果
        return result

    def list_rows(self) -> list[dict[str, Any]]:
        """左侧清单的行数据：待核验草稿 + 本次已入库记录。"""
        # 行容器
        rows: list[dict[str, Any]] = []
        # 待核验草稿（按 id 排序，稳定）
        for pid in sorted(self.drafts):
            # 取证据卡（可能尚未构建——未构建时不显示计数，避免为渲染清单
            # 而抓取全部 11 个官方页面；访问过的行会显示真实计数）
            card = self.cards.get(pid)
            # 计数（未构建为 None）
            counts = card.count_by_status() if card else None
            # 组装行
            rows.append({
                "id": pid,                          # 草稿 id
                "title": self.drafts[pid].title,    # 标题
                "done": False,                      # 未入库
                "error_count": counts.get(STATUS_ERROR, 0) if counts else None,      # 红项数
                "warning_count": counts.get(STATUS_WARNING, 0) if counts else None,  # 黄项数
                "manual_count": counts.get(STATUS_MANUAL, 0) if counts else None,    # 需人工核数
            })
        # 已入库记录排在待核验之后
        for pid in self.done:
            # 组装行
            rows.append({
                "id": pid,          # id
                "title": pid,       # 已入库的记录标题不再重要，id 即可
                "done": True,       # 已入库
                "error_count": None, "warning_count": None, "manual_count": None,
            })
        # 返回
        return rows


# ============================================================
# HTML 渲染
# ============================================================

def _esc(value: Any) -> str:
    """转义进入 HTML 的文本。证据摘录是网络取回的原文，必须视为不可信。"""
    # 转义
    return html.escape(str(value), quote=True)


# 状态徽章的 CSS 类（复用 style.css 的既有徽章样式）
_STATUS_BADGE_CLASS = {
    STATUS_OK: "badge-ok",          # 绿
    STATUS_WARNING: "badge-warn",   # 黄
    STATUS_ERROR: "badge-dead",     # 红（style.css 中红色徽章的既有类名）
    STATUS_MANUAL: "badge-pending", # 灰
}

# 状态徽章的中文文字
_STATUS_BADGE_TEXT = {
    STATUS_OK: "机器通过",      # 绿
    STATUS_WARNING: "待判断",   # 黄
    STATUS_ERROR: "证据冲突",   # 红
    STATUS_MANUAL: "需人工核",  # 灰
}


def _render_badge(status: str) -> str:
    """渲染一个状态徽章。"""
    # 取类名与文字
    css = _STATUS_BADGE_CLASS.get(status, "badge-quiet")
    # 取文字
    text = _STATUS_BADGE_TEXT.get(status, status)
    # 渲染
    return f'<span class="badge {css}">{_esc(text)}</span>'


def _render_check_row(check: CheckResult, policy_id: str, decided: Decision | None, is_first_undecided: bool) -> str:
    """渲染一个检查项行：徽章 + 结论 + 证据（可展开）+ 判断按钮。"""
    # 判断按钮区
    buttons_html = ""
    # 已判断：显示判断结果，不再显示按钮（改主意可以刷新后重新点——
    # 内存态以最后一次为准，见 ConsoleState.decide 的说明）
    if decided is not None:
        # 显示判断结果
        buttons_html = f'<p class="muted">✓ 已判断：{_esc(decided.label)}</p>'
    # 未判断且非绿：渲染选项按钮
    elif check.options:
        # 逐个渲染按钮（POST 表单，303 重定向回本页）
        parts: list[str] = []
        # 选项序号（键盘流：数字键触发）
        for idx, option in enumerate(check.options, start=1):
            # 渲染一个按钮表单
            parts.append(
                f'<form method="post" action="/decide" class="opt-form">'
                f'<input type="hidden" name="id" value="{_esc(policy_id)}">'
                f'<input type="hidden" name="check" value="{_esc(check.check_id)}">'
                f'<input type="hidden" name="option" value="{_esc(option.key)}">'
                f'<button type="submit" data-opt-index="{idx}">{_esc(option.label)}</button>'
                f'</form>'
            )
        # 合并
        buttons_html = '<div class="opt-row">' + "".join(parts) + "</div>"
    # 证据摘录（details/summary 原生折叠，零 JS）
    evidence_html = ""
    # 有证据时渲染
    if check.evidence:
        # 逐条渲染
        items = "".join(f"<li>{_esc(e)}</li>" for e in check.evidence)
        # 折叠块
        evidence_html = f"<details><summary>证据摘录（{len(check.evidence)} 条）</summary><ul>{items}</ul></details>"
    # 首个未判断项的标记（键盘流的数字键作用于它）
    undecided_attr = ' data-undecided="1"' if is_first_undecided else ""
    # 组装行
    return (
        f'<div class="check-row pending-item" data-status="{_esc(check.status)}"{undecided_attr}>'
        f'<p>{_render_badge(check.status)} <b>{_esc(check.layer)}</b> —— {_esc(check.summary)}</p>'
        f"{evidence_html}{buttons_html}</div>"
    )


def _render_card_panel(state: ConsoleState, policy_id: str) -> str:
    """渲染右侧证据卡面板。"""
    # 取证据卡（现场构建）
    card = state.get_card(policy_id)
    # 草稿不存在
    if card is None:
        # 渲染提示
        return f'<div class="card"><p class="muted">未找到草稿：{_esc(policy_id)}</p></div>'
    # 已做的判断（按检查项索引）
    decided = {d.check_id: d for d in state.decisions.get(policy_id, [])}
    # 找出「第一个未判断的待办项」（键盘流数字键的目标）
    first_undecided: str | None = None
    # 逐项扫描
    for check in card.pending_checks():
        # 未判断的第一个
        if check.check_id not in decided:
            # 记录
            first_undecided = check.check_id
            # 停止
            break
    # 绿项与待办项分组：绿项折叠为徽章，默认不展开、不要求点击
    green_checks = [c for c in card.checks if c.status == STATUS_OK]
    # 待办项
    pending_checks = card.pending_checks()
    # 绿项折叠块
    green_html = ""
    # 有绿项时渲染
    if green_checks:
        # 逐条渲染（折叠内）
        rows = "".join(f"<li>{_esc(c.layer)}：{_esc(c.summary)}</li>" for c in green_checks)
        # 折叠块
        green_html = (
            f'<details class="green-fold"><summary>'
            f'<span class="badge badge-ok">机器通过 {len(green_checks)} 项</span></summary>'
            f"<ul>{rows}</ul></details>"
        )
    # 待办项逐行渲染
    pending_html = "".join(
        _render_check_row(check, policy_id, decided.get(check.check_id), check.check_id == first_undecided)
        for check in pending_checks
    )
    # 阻断原因（入库按钮置灰的依据）
    reasons = state.pending_reasons(policy_id)
    # 入库按钮：有阻断原因时置灰并列出原因
    if reasons:
        # 原因列表
        reason_items = "".join(f"<li>{_esc(r)}</li>" for r in reasons)
        # 置灰按钮 + 原因
        promote_html = (
            f'<button id="promote-btn" type="button" disabled>核验通过并入库（{len(reasons)} 项待处理）</button>'
            f"<ul class=\"muted\">{reason_items}</ul>"
        )
    else:
        # 可入库：渲染 POST 表单按钮
        promote_html = (
            f'<form method="post" action="/promote">'
            f'<input type="hidden" name="id" value="{_esc(policy_id)}">'
            f'<button id="promote-btn" type="submit">核验通过并入库</button></form>'
        )
    # 重新取证表单
    refetch_html = (
        f'<form method="post" action="/refetch" class="opt-form">'
        f'<input type="hidden" name="id" value="{_esc(policy_id)}">'
        f'<button type="submit">重新取证</button></form>'
    )
    # 组装面板
    return (
        f'<div class="card">'
        f"<h2>{_esc(card.title)}</h2>"
        f'<p class="muted">{_esc(card.policy_id)} · 取证于 {_esc(card.generated_at)}</p>'
        f'<p><a href="{_esc(card.url)}">{_esc(card.url)}</a></p>'
        f"{refetch_html}{green_html}{pending_html}<hr>{promote_html}</div>"
    )


def _render_page(state: ConsoleState, current_id: str | None, notice: str = "") -> str:
    """渲染整页：左侧清单 + 右侧证据卡。"""
    # 清单行
    rows = state.list_rows()
    # 当前选中的 id：未指定时取第一条待核验草稿
    if current_id is None and state.drafts:
        # 取第一条
        current_id = sorted(state.drafts)[0]
    # 渲染清单项
    items: list[str] = []
    # 逐行渲染
    for row in rows:
        # 已入库标记
        mark = "✓ " if row["done"] else ("▶ " if row["id"] == current_id else "")
        # 计数徽章（未构建证据卡的行不显示计数——清单渲染不该触发全网抓取）
        count_html = ""
        # 有计数时渲染
        if row["error_count"] is not None:
            # 红/黄/核计数
            count_html = (
                f' <span class="badge badge-dead">{row["error_count"]}</span>'
                f' <span class="badge badge-warn">{row["warning_count"]}</span>'
                f' <span class="badge badge-pending">{row["manual_count"]}</span>'
            )
        # 渲染一行
        items.append(
            f'<li><a href="/?id={_esc(row["id"])}">{mark}{_esc(row["title"])}</a>{count_html}</li>'
        )
    # 清单 HTML
    list_html = f"<ul>{''.join(items)}</ul>" if items else '<p class="muted">没有待核验的草稿。</p>'
    # 右侧主面板
    main_html = _render_card_panel(state, current_id) if current_id else '<div class="card"><p class="muted">没有待核验的草稿。</p></div>'
    # 通知条（判断/入库后的结果回显）
    notice_html = f'<p class="block">{_esc(notice)}</p>' if notice else ""
    # 加载错误的可见化（草稿加载问题绝不静默）
    load_error_html = ""
    # 有加载错误时渲染
    if state.load_errors:
        # 逐条渲染
        errs = "".join(f"<li>{_esc(e)}</li>" for e in state.load_errors)
        # 警示块
        load_error_html = f'<div class="block-warn"><b>草稿加载问题：</b><ul>{errs}</ul></div>'
    # 键盘提示
    keyboard_hint = (
        '<p class="muted">键盘：数字键 = 对当前待判断项做第 N 个判断；'
        "Enter = 入库（可入库时）；j/k = 切换下一条/上一条记录。</p>"
    )
    # 页面骨架（复用站点样式与图标）
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>本地核验台 — finreg-ai-cn</title>
<link rel="stylesheet" href="/assets/style.css">
<link rel="icon" href="/assets/favicon.ico" sizes="16x16">
<style>
/* 核验台专属布局（仅本页使用，不进站点公共样式） */
.verify-layout {{ display: flex; gap: 1.5rem; align-items: flex-start; }}
/* 左侧清单固定宽度 */
.verify-layout aside {{ flex: 0 0 17rem; }}
/* 右侧主面板占满剩余宽度 */
.verify-layout main {{ flex: 1; min-width: 0; }}
/* 判断按钮横向排列 */
.opt-row {{ display: flex; gap: .5rem; flex-wrap: wrap; margin: .4rem 0 .8rem; }}
/* 内联表单不产生额外间距 */
.opt-form {{ display: inline; }}
/* 检查项行之间的分隔 */
.check-row {{ border-top: 1px solid var(--line, #e5e2da); padding: .6rem 0; }}
</style>
</head>
<body>
<main class="wrap">
<h1>本地核验台</h1>
<p class="muted">待核验 {len(state.drafts)} 条 · 本次会话已入库 {len(state.done)} 条 ·
台账目录 data/verification-log/（只增不改）</p>
{keyboard_hint}
{notice_html}
{load_error_html}
<div class="verify-layout">
<aside><div class="card">{list_html}</div></aside>
<main>{main_html}</main>
</div>
</main>
<script>
// 键盘流：数字键做判断、Enter 入库、j/k 切换记录
document.addEventListener('keydown', function (e) {{
  // 输入框聚焦时不劫持按键（虽然本页没有输入框，防御性保留）
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  // 数字键 1-9：点击「第一个未判断项」的第 N 个选项按钮
  if (e.key >= '1' && e.key <= '9') {{
    // 找到第一个未判断项里对应序号的按钮
    var btn = document.querySelector('.pending-item[data-undecided="1"] button[data-opt-index="' + e.key + '"]');
    // 存在则点击
    if (btn) btn.click();
  }}
  // Enter：入库（按钮置灰时不触发——disabled 按钮 click 无效，双重保险）
  if (e.key === 'Enter') {{
    // 找入库按钮
    var promote = document.getElementById('promote-btn');
    // 可点击时触发
    if (promote && !promote.disabled) promote.click();
  }}
  // j：切换到下一条记录（清单中当前项的下一个链接）
  if (e.key === 'j') {{
    // 找到清单里的当前项
    var current = document.querySelectorAll('.verify-layout aside li');
    // 逐个查找
    for (var i = 0; i < current.length; i++) {{
      // 当前项的标记是 ▶
      if (current[i].textContent.indexOf('▶') === 0 && current[i + 1]) {{
        // 跳转到下一条
        current[i + 1].querySelector('a').click();
        // 停止
        break;
      }}
    }}
  }}
  // k：切换到上一条记录
  if (e.key === 'k') {{
    // 找到清单里的当前项
    var items = document.querySelectorAll('.verify-layout aside li');
    // 逐个查找
    for (var i = 0; i < items.length; i++) {{
      // 当前项的前一个存在时跳转
      if (items[i].textContent.indexOf('▶') === 0 && items[i - 1]) {{
        // 跳转到上一条
        items[i - 1].querySelector('a').click();
        // 停止
        break;
      }}
    }}
  }}
}});
</script>
</body>
</html>"""


def _render_promote_result(result: PromoteResult) -> str:
    """渲染入库结果页。"""
    # 标题按结果区分
    if result.ok:
        # 成功
        heading = "入库成功"
    elif result.rolled_back:
        # 回滚
        heading = "入库失败，已整体回滚"
    else:
        # 被拒绝
        heading = "入库被拒绝"
    # 消息列表
    items = "".join(f"<li>{_esc(m)}</li>" for m in result.messages)
    # 阻断原因
    blocked = "".join(f"<li>{_esc(r)}</li>" for r in result.blocked_reasons)
    # 组装页面（极简骨架，复用站点样式）
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{_esc(heading)} — 本地核验台</title>
<link rel="stylesheet" href="/assets/style.css"></head>
<body><main class="wrap">
<h1>{_esc(heading)}</h1>
<ul>{items}</ul>
{f"<ul>{blocked}</ul>" if blocked else ""}
<p><a href="/">返回核验台</a></p>
</main></body></html>"""


# ============================================================
# HTTP 层
# ============================================================

def _make_handler(state: ConsoleState) -> type[BaseHTTPRequestHandler]:
    """构造绑定到指定会话状态的请求处理器类。

    用工厂函数而非模块级全局状态：测试可以并发起多个互不影响的服务。
    """

    class VerifyHandler(BaseHTTPRequestHandler):
        """核验台的请求处理器。"""

        # 关闭默认的请求日志（每个请求两行 stderr 会淹没用户的终端）
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """静默访问日志。核验台是单人本地工具，日志没有读者。"""
            # 什么都不输出
            return

        def _send_html(self, body: str, status: int = 200) -> None:
            """发送 HTML 响应。"""
            # 编码为 UTF-8 字节
            payload = body.encode("utf-8")
            # 状态行
            self.send_response(status)
            # 内容类型
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # 长度
            self.send_header("Content-Length", str(len(payload)))
            # 结束头
            self.end_headers()
            # 写正文
            self.wfile.write(payload)

        def _redirect(self, location: str) -> None:
            """303 重定向（POST 后重定向，防刷新重复提交）。

            Location 头按 RFC 7230 只能承载 latin-1 字节，
            而重定向地址里的通知文字是中文——必须 percent-encode，
            否则 send_header 会抛 UnicodeEncodeError。
            """
            # 状态行
            self.send_response(303)
            # 目标（中文部分 percent-encode；?&=/:= 保留原样——
            # 少了 = 会把查询串里的键值分隔符也编码掉，重定向后解析不出参数）
            self.send_header("Location", quote(location, safe="?&=/:="))
            # 结束头
            self.end_headers()

        def _read_form(self) -> dict[str, str]:
            """读取 POST 表单 body 并解析为字典。"""
            # 读长度
            length = int(self.headers.get("Content-Length") or 0)
            # 读 body
            raw = self.rfile.read(length).decode("utf-8")
            # 解析查询串格式
            parsed = parse_qs(raw)
            # 拍平为单值字典
            return {key: values[0] for key, values in parsed.items() if values}

        def do_GET(self) -> None:  # noqa: N802（标准库要求的命名）
            """处理 GET：页面与静态资源。"""
            # 解析路径
            path = urlparse(self.path).path
            # 静态资源
            if path.startswith("/assets/"):
                # 资源名（防目录穿越：只取文件名部分）
                name = Path(path).name
                # 只允许白名单内的资源（核验台只需要这两样）
                if name not in ("style.css", "favicon.ico"):
                    # 404
                    self._send_html("<p>not found</p>", status=404)
                    # 返回
                    return
                # 读文件
                asset = SITE_ASSETS_DIR / name
                # 不存在时 404
                if not asset.exists():
                    # 404
                    self._send_html("<p>not found</p>", status=404)
                    # 返回
                    return
                # 读取字节
                payload = asset.read_bytes()
                # 状态行
                self.send_response(200)
                # 内容类型（favicon 的 mime 猜测在 Windows 上可能为空，显式兜底）
                self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
                # 长度
                self.send_header("Content-Length", str(len(payload)))
                # 结束头
                self.end_headers()
                # 写正文
                self.wfile.write(payload)
                # 返回
                return
            # 主页
            if path == "/":
                # 解析查询串
                query = parse_qs(urlparse(self.path).query)
                # 当前草稿 id
                current_id = query.get("id", [None])[0]
                # 通知（303 重定向带回的一句话）
                notice = query.get("notice", [""])[0]
                # 渲染整页
                self._send_html(_render_page(state, current_id, notice))
                # 返回
                return
            # 其它路径 404
            self._send_html("<p>not found</p>", status=404)

        def do_POST(self) -> None:  # noqa: N802（标准库要求的命名）
            """处理 POST：判断、入库、重新取证。"""
            # 解析路径
            path = urlparse(self.path).path
            # 读表单
            form = self._read_form()
            # 路由：判断
            if path == "/decide":
                try:
                    # 记录判断（非法输入抛 ValueError）
                    state.decide(form.get("id", ""), form.get("check", ""), form.get("option", ""))
                except ValueError as exc:
                    # 明确报错，不静默吞掉
                    self._send_html(f"<p>判断被拒绝：{_esc(exc)}</p>", status=400)
                    # 返回
                    return
                # 303 回列表（带一句话通知）
                self._redirect(f"/?id={form.get('id', '')}&notice=判断已记录并写入台账")
                # 返回
                return
            # 路由：入库
            if path == "/promote":
                # 执行入库
                result = state.promote(form.get("id", ""))
                # 渲染结果页
                self._send_html(_render_promote_result(result), status=200 if result.ok else 409)
                # 返回
                return
            # 路由：重新取证
            if path == "/refetch":
                # 重新抓取
                card = state.refetch(form.get("id", ""))
                # 不存在时 404
                if card is None:
                    # 404
                    self._send_html("<p>not found</p>", status=404)
                    # 返回
                    return
                # 303 回列表
                self._redirect(f"/?id={form.get('id', '')}&notice=已重新取证")
                # 返回
                return
            # 其它路径 404
            self._send_html("<p>not found</p>", status=404)

    # 返回处理器类
    return VerifyHandler


def create_server(state: ConsoleState, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """创建绑定 127.0.0.1 的核验台服务器（尚未启动）。

    拆出这个函数是为了测试：测试传 port=0 让操作系统分配空闲端口，
    避免与真实会话或其他测试撞端口。
    """
    # 构造服务器：绑定回环地址，处理器绑定到会话状态
    return ThreadingHTTPServer((BIND_HOST, port), _make_handler(state))


def serve(port: int = DEFAULT_PORT, session: Any | None = None) -> None:
    """启动核验台（阻塞，直到 Ctrl+C）。"""
    # 构造会话状态（真实目录 + 真实抓取）
    state = ConsoleState(session=session)
    # 构造服务器
    server = create_server(state, port)
    # 打印访问地址（用户需要知道去哪里打开）
    print(f"本地核验台已启动：http://{BIND_HOST}:{port}/")
    # 打印安全说明
    print("仅监听 127.0.0.1（本机）；按 Ctrl+C 停止。")
    try:
        # 阻塞服务
        server.serve_forever()
    except KeyboardInterrupt:
        # 正常退出
        print("\n已停止。")
    finally:
        # 释放端口
        server.server_close()


# 公开接口
__all__ = [
    "BIND_HOST",            # 绑定地址（测试断言用）
    "DEFAULT_PORT",         # 默认端口
    "ConsoleState",         # 决策状态机
    "create_server",        # 服务器工厂（测试用）
    "serve",                # 启动入口
]
