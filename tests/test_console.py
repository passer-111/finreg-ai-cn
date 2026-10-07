"""核验台界面的测试 —— ``console`` 模块。

测什么、不测什么
----------------
不测像素、不测样式——那些靠人眼看。
只测两件事：

1. **决策状态机**（``ConsoleState``）：判断被记录、非法选项被明确拒绝、
   红项未清零时入库被拒绝（界面置灰与服务端防线是同一份逻辑，
   绕过界面直接调状态机也必须被拦下）。
2. **HTTP 冒烟**：页面能渲染、判断能提交、入库能触发——
   绑 127.0.0.1 临时端口，全部离线（官方页面用 FakeSession 伪造）。
"""

# 导入 json：读台账 JSONL
import json
# 导入 date：工厂函数的默认值参数（必须在函数定义之前导入）
from datetime import date
# 导入 threading：在测试线程里跑真实 HTTP 服务
import threading
# 导入 urllib：对本地服务发请求（标准库，离线）
import urllib.error
# 导入 urllib.request：发起本地请求
import urllib.request

# 导入 yaml：写测试草稿
import yaml

# 导入被测对象
from finreg_ai.console import BIND_HOST, ConsoleState, create_server
# 导入模型类型
from finreg_ai.models import VerifiedBy, policy_to_dict
# 复用伪造会话与响应
from tests.conftest import FakeResponse, FakeSession
# 复用政策工厂与测试页面
from tests.test_models import make_policy
# 复用取证层测试的「一切正常」页面（含施行条款与十六条义务段落）
from tests.test_verify import GOOD_PAGE_HTML


def _make_state(tmp_path, *, effective_from=date(2026, 7, 1)) -> ConsoleState:
    """构造一个指向临时目录的会话状态（草稿一条，页面用伪造会话）。"""
    # 草稿目录
    drafts = tmp_path / "drafts"
    # 确保存在
    drafts.mkdir(parents=True, exist_ok=True)
    # 写草稿（domain/issuer_code 覆盖为 schema 受控词表内的值）
    policy = make_policy(
        title="某测试政策",                     # 标题（与测试页面 <title> 一致）
        verified_by=VerifiedBy.AUTOMATED,       # 草稿形态
        domain=["银行", "人工智能"],            # 受控词表
        issuer_code="nfra",                     # 受控词表
        effective_from=effective_from,          # 生效日期（参数化，便于制造黄项）
    )
    # 写文件
    (drafts / f"{policy.id}.yaml").write_text(
        yaml.safe_dump(policy_to_dict(policy), allow_unicode=True), encoding="utf-8"
    )
    # 伪造会话：任何 URL 都返回测试页面
    session = FakeSession(lambda url: FakeResponse(text=GOOD_PAGE_HTML, status_code=200))
    # 构造状态
    return ConsoleState(
        drafts_dir=drafts,                      # 草稿目录
        policies_dir=tmp_path / "policies",     # 政策目录
        log_dir=tmp_path / "logs",              # 台账目录
        session=session,                        # 伪造会话
    )


# ============================================================
# 决策状态机
# ============================================================

def test_state_builds_card_lazily(tmp_path) -> None:
    """验证证据卡延迟构建：清单渲染不触发抓取，取卡时才抓。"""
    # 构造状态
    state = _make_state(tmp_path)
    # 初始无缓存
    assert state.cards == {}
    # 清单行不触发构建（counts 为 None）
    rows = state.list_rows()
    # 计数为空
    assert rows[0]["error_count"] is None
    # 取卡触发构建
    card = state.get_card("test-2026-demo")
    # 卡已构建
    assert card is not None
    # 有缓存了
    assert "test-2026-demo" in state.cards


def test_state_decide_records_and_logs(tmp_path) -> None:
    """验证判断被记录到内存并写入台账。"""
    # 构造状态：生效日期留空，页面有施行条款 → 日期层出黄项「可补」
    state = _make_state(tmp_path, effective_from=None)
    # 取卡确认黄项存在
    card = state.get_card("test-2026-demo")
    # 找日期检查项
    date_check = next(c for c in card.checks if c.check_id == "date")
    # 有选项
    assert date_check.options
    # 做判断：采纳候选日期
    state.decide("test-2026-demo", "date", "adopt:2026-07-01")
    # 判断被记录
    assert state.decisions["test-2026-demo"][0].option_key == "adopt:2026-07-01"
    # 台账已写（点击即留痕）
    log_files = list((tmp_path / "logs").glob("*.jsonl"))
    # 有台账文件
    assert log_files
    # 读一条
    entry = json.loads(log_files[0].read_text(encoding="utf-8").splitlines()[0])
    # 是判断记录
    assert entry["kind"] == "decision"
    # 入库不再被阻断（黄项已判断）
    assert state.pending_reasons("test-2026-demo") == []


def test_state_decide_replaces_same_check_decision(tmp_path) -> None:
    """验证同一检查项重复判断以最后一次为准（改主意是常态）。"""
    # 构造状态
    state = _make_state(tmp_path, effective_from=None)
    # 第一次判断：采纳
    state.decide("test-2026-demo", "date", "adopt:2026-07-01")
    # 第二次判断：改主意，维持留空
    state.decide("test-2026-demo", "date", "keep-blank")
    # 内存态只有一条
    assert len(state.decisions["test-2026-demo"]) == 1
    # 是最后一次
    assert state.decisions["test-2026-demo"][0].option_key == "keep-blank"


def test_state_decide_rejects_invalid_option(tmp_path) -> None:
    """验证点了不存在的选项时明确报错（手工构造的请求不得静默通过）。"""
    # 构造状态
    state = _make_state(tmp_path, effective_from=None)
    # 非法选项
    try:
        # 触发
        state.decide("test-2026-demo", "date", "no-such-option")
        # 不应到达
        raise AssertionError("非法选项未被拒绝")
    except ValueError as exc:
        # 报错内容指向选项
        assert "no-such-option" in str(exc)


def test_state_promote_refused_with_undecided_pending(tmp_path) -> None:
    """核心状态机断言：红/黄项未判断时，绕过界面直接调状态机也被拒绝。"""
    # 构造状态（留空日期 → 黄项）
    state = _make_state(tmp_path, effective_from=None)
    # 不做任何判断直接入库
    result = state.promote("test-2026-demo")
    # 被拒绝
    assert not result.ok
    # 阻断原因非空
    assert result.blocked_reasons
    # 草稿未被移动
    assert (tmp_path / "drafts" / "test-2026-demo.yaml").exists()
    # 政策目录无文件
    assert not (tmp_path / "policies" / "test-2026-demo.yaml").exists()


def test_state_promote_success_after_decisions(tmp_path) -> None:
    """反向测试：判断完成后入库成功（夹逼上一条）。"""
    # 构造状态
    state = _make_state(tmp_path, effective_from=None)
    # 做判断：采纳候选日期
    state.decide("test-2026-demo", "date", "adopt:2026-07-01")
    # 入库
    result = state.promote("test-2026-demo")
    # 成功
    assert result.ok
    # 草稿消失、正式记录出现
    assert not (tmp_path / "drafts" / "test-2026-demo.yaml").exists()
    # 正式记录存在
    assert (tmp_path / "policies" / "test-2026-demo.yaml").exists()
    # 清单里它变成「已入库」
    rows = state.list_rows()
    # 该行标记 done
    assert rows[0]["done"] is True


# ============================================================
# HTTP 冒烟
# ============================================================

def _run_server(state: ConsoleState):
    """在后台线程启动绑定临时端口的服务器，返回 (server, base_url)。"""
    # 创建服务器（port=0 让操作系统分配空闲端口）
    server = create_server(state, 0)
    # 后台线程跑服务
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    # 启动
    thread.start()
    # 返回服务器与基地址
    return server, f"http://{BIND_HOST}:{server.server_address[1]}"


def test_http_index_renders(tmp_path) -> None:
    """冒烟：首页能渲染，含清单与证据卡，静态样式可访问。"""
    # 构造状态
    state = _make_state(tmp_path)
    # 启动服务
    server, base = _run_server(state)
    try:
        # 请求首页
        with urllib.request.urlopen(f"{base}/?id=test-2026-demo") as resp:
            # 状态 200
            assert resp.status == 200
            # 读页面
            body = resp.read().decode("utf-8")
        # 含工作台标题
        assert "本地核验台" in body
        # 含草稿标题
        assert "某测试政策" in body
        # 引用了复用的站点样式
        assert "/assets/style.css" in body
        # 静态样式可访问
        with urllib.request.urlopen(f"{base}/assets/style.css") as resp:
            # 状态 200
            assert resp.status == 200
    finally:
        # 关闭服务
        server.shutdown()
        # 释放端口
        server.server_close()


def test_http_decide_then_promote_flow(tmp_path) -> None:
    """冒烟：POST 判断 → 入库按钮解除置灰 → POST 入库成功。"""
    # 构造状态（留空日期 → 有一个黄项）
    state = _make_state(tmp_path, effective_from=None)
    # 启动服务
    server, base = _run_server(state)
    try:
        # 未判断时：页面上的入库按钮是置灰的
        with urllib.request.urlopen(f"{base}/?id=test-2026-demo") as resp:
            # 读页面
            body = resp.read().decode("utf-8")
        # 置灰按钮
        assert 'id="promote-btn" type="button" disabled' in body
        # POST 判断（urllib 会自动跟随 303 重定向）
        with urllib.request.urlopen(
            f"{base}/decide",
            data=b"id=test-2026-demo&check=date&option=adopt%3A2026-07-01",
        ) as resp:
            # 最终 200（重定向后的页面）
            assert resp.status == 200
        # 判断后：入库按钮不再置灰
        with urllib.request.urlopen(f"{base}/?id=test-2026-demo") as resp:
            # 读页面
            body = resp.read().decode("utf-8")
        # 不再置灰（出现可提交的入库表单）
        assert 'action="/promote"' in body
        # POST 入库
        with urllib.request.urlopen(f"{base}/promote", data=b"id=test-2026-demo") as resp:
            # 成功 200
            assert resp.status == 200
            # 读结果页
            result_body = resp.read().decode("utf-8")
        # 结果页显示成功
        assert "入库成功" in result_body
        # 文件真的移动了
        assert (tmp_path / "policies" / "test-2026-demo.yaml").exists()
    finally:
        # 关闭服务
        server.shutdown()
        # 释放端口
        server.server_close()


def test_http_promote_blocked_returns_409(tmp_path) -> None:
    """冒烟：红/黄项未判断时直接 POST 入库返回 409（服务端防线）。"""
    # 构造状态
    state = _make_state(tmp_path, effective_from=None)
    # 启动服务
    server, base = _run_server(state)
    try:
        # 直接 POST 入库（不做判断）
        try:
            # 触发
            urllib.request.urlopen(f"{base}/promote", data=b"id=test-2026-demo")
            # 不应到达
            raise AssertionError("未判断时入库未被拒绝")
        except urllib.error.HTTPError as exc:
            # 409 冲突
            assert exc.code == 409
        # 草稿未被移动
        assert (tmp_path / "drafts" / "test-2026-demo.yaml").exists()
    finally:
        # 关闭服务
        server.shutdown()
        # 释放端口
        server.server_close()
