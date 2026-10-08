/* ============================================================
 * 中国金融 AI 合规政策库 —— 首页筛选与检索
 * ------------------------------------------------------------
 * 为什么用这个「土办法」而不是 MiniSearch / FlexSearch
 * ----------------------------------------------------
 * 库里目前是 20 条政策、全量约 379 KB。在这个量级上，
 * 任何索引结构带来的收益都小于它引入的成本：
 *
 * 1. **多一个外部依赖就多一个失效点。** 站点是纯静态的，脚本一旦引 CDN，
 *    网络不通时搜索框会静默失效——使用者会以为「库里没有这条政策」，
 *    而不是「搜索坏了」。这正是本项目一直在对抗的静默失败。
 * 2. **DOM 里已经有全部文本。** 卡片正文就是索引本身，
 *    再单独拉一份 JSON 建索引，等于把同一份数据维护两遍。
 * 3. **中文不需要分词也能用。** 子串匹配对中文天然友好
 *    （「数据安全」是「违反数据安全管理规定」的子串），
 *    而词干化、同义词这些西文检索的核心问题在中文里根本不存在。
 *
 * 因此这里的策略是：**查询按空格切成词，每个词都必须作为子串出现**。
 * 数据量涨到几百条以上时再换索引方案——那时才真的有收益。
 * ============================================================ */

/* 用 IIFE 包起来，避免污染全局命名空间 */
(function () {
  /* 严格模式：把拼写错误、意外全局变量这类问题变成显式报错 */
  "use strict";

  /* 取出卡片容器。首页才有，其他页面为 null */
  var container = document.getElementById("cards");
  /* 容器不存在时直接返回，让本脚本可以安全地挂到每个页面上 */
  if (!container) {
    return;
  }

  /* 一次性取出所有卡片，避免每次筛选都重新查询 DOM */
  var cards = Array.prototype.slice.call(container.querySelectorAll(".card"));
  /* 搜索框 */
  var searchInput = document.getElementById("q");
  /* 状态筛选下拉 */
  var statusSelect = document.getElementById("f-status");
  /* 机构筛选下拉 */
  var issuerSelect = document.getElementById("f-issuer");
  /* 相关度筛选下拉 */
  var relevanceSelect = document.getElementById("f-relevance");
  /* 重置按钮 */
  var resetButton = document.getElementById("reset");
  /* 结果计数所在元素 */
  var resultCount = document.getElementById("result-count");

  /* 为每条卡片建立一份「可搜索文本」缓存。
     文本取自 data-search 属性，而该属性在生成时已经把
     标题、机构、义务概括、主题、领域都拼了进去——
     因此搜索能命中义务内容，而不只是标题。
     缓存的原因是：每次按键都读一遍属性会触发重复的字符串解析。 */
  var haystacks = cards.map(function (card) {
    /* 取属性并把大小写统一，使英文关键词不区分大小写 */
    return (card.getAttribute("data-search") || "").toLowerCase();
  });

  /* 无结果时的提示元素，首次需要时惰性创建 */
  var emptyNotice = null;

  /**
   * 按当前控件状态筛选卡片。
   */
  function applyFilters() {
    /* 取查询串并统一大小写 */
    var query = (searchInput ? searchInput.value : "").toLowerCase().trim();
    /* 按空白切成词，并丢掉空串（连续空格、首尾空格都会产生空串） */
    var terms = query.split(/\s+/).filter(function (term) {
      /* 只保留非空词 */
      return term.length > 0;
    });
    /* 取三个下拉的当前值 */
    var wantedStatus = statusSelect ? statusSelect.value : "";
    /* 机构 */
    var wantedIssuer = issuerSelect ? issuerSelect.value : "";
    /* 相关度 */
    var wantedRelevance = relevanceSelect ? relevanceSelect.value : "";

    /* 命中计数 */
    var visible = 0;

    /* 逐条判断 */
    cards.forEach(function (card, index) {
      /* 先看三个枚举筛选，任一不符即淘汰 */
      if (wantedStatus && card.getAttribute("data-status") !== wantedStatus) {
        /* 隐藏该卡片 */
        card.hidden = true;
        /* 提前结束本条 */
        return;
      }
      /* 机构筛选 */
      if (wantedIssuer && card.getAttribute("data-issuer") !== wantedIssuer) {
        /* 隐藏 */
        card.hidden = true;
        /* 结束 */
        return;
      }
      /* 相关度筛选 */
      if (wantedRelevance && card.getAttribute("data-relevance") !== wantedRelevance) {
        /* 隐藏 */
        card.hidden = true;
        /* 结束 */
        return;
      }

      /* 再看待搜索文本：所有词都必须出现（AND 语义）。
         用 AND 而非 OR 是刻意的：OR 会让「数据 安全」把只含「数据」的
         一大堆结果也捞出来，使用者要的是收窄，不是扩张。 */
      var text = haystacks[index];
      /* 逐个词检查 */
      for (var i = 0; i < terms.length; i += 1) {
        /* 有一个词没出现就淘汰 */
        if (text.indexOf(terms[i]) === -1) {
          /* 隐藏 */
          card.hidden = true;
          /* 结束本条 */
          return;
        }
      }

      /* 全部通过则显示 */
      card.hidden = false;
      /* 计数加一 */
      visible += 1;
    });

    /* 更新计数文案。
       必须显示「共 N 条」而不是只显示命中数——否则使用者无法区分
       「筛出来 3 条」和「原本就只有 3 条」，会误判为筛太严。 */
    if (resultCount) {
      /* 有筛选条件时同时给出总数，使筛选的收窄幅度可见 */
      resultCount.textContent =
        visible === cards.length
          ? "共 " + cards.length + " 条政策"
          : "筛选出 " + visible + " 条 / 共 " + cards.length + " 条";
    }

    /* 无结果时插入提示。这一步不能省：
       一片空白会让使用者怀疑页面坏了，而不是「没有匹配项」。 */
    if (visible === 0) {
      /* 首次需要时创建提示元素 */
      if (!emptyNotice) {
        /* 创建元素 */
        emptyNotice = document.createElement("p");
        /* 套用与其它空状态一致的样式 */
        emptyNotice.className = "empty";
        /* 说明原因与下一步动作，而不是只说「没有结果」 */
        emptyNotice.textContent =
          "没有匹配的政策。可以试着减少关键词，或点击「重置」清除筛选条件。";
      }
      /* 已存在则确保它在 DOM 里（重置时可能已被移除） */
      if (!emptyNotice.parentNode) {
        /* 追加到卡片容器之后 */
        container.parentNode.appendChild(emptyNotice);
      }
    } else if (emptyNotice && emptyNotice.parentNode) {
      /* 有结果时移除提示 */
      emptyNotice.parentNode.removeChild(emptyNotice);
    }
  }

  /* 重置全部筛选条件 */
  function resetFilters() {
    /* 清空搜索框 */
    if (searchInput) {
      /* 置空 */
      searchInput.value = "";
    }
    /* 清空三个下拉 */
    if (statusSelect) {
      /* 回到「全部」 */
      statusSelect.value = "";
    }
    /* 机构 */
    if (issuerSelect) {
      /* 回到「全部」 */
      issuerSelect.value = "";
    }
    /* 相关度 */
    if (relevanceSelect) {
      /* 回到「全部」 */
      relevanceSelect.value = "";
    }
    /* 立即重算 */
    applyFilters();
  }

  /* 给搜索框绑定输入事件。
     直接监听 input 而不做防抖：20 条卡片的筛选是微秒级的，
     加防抖只会引入输入延迟，得不偿失。卡片数上千时再补防抖。 */
  if (searchInput) {
    /* 每次输入即筛选 */
    searchInput.addEventListener("input", applyFilters);
  }
  /* 状态下拉变更 */
  if (statusSelect) {
    /* 绑定 change 事件 */
    statusSelect.addEventListener("change", applyFilters);
  }
  /* 机构下拉变更 */
  if (issuerSelect) {
    /* 绑定 change 事件 */
    issuerSelect.addEventListener("change", applyFilters);
  }
  /* 相关度下拉变更 */
  if (relevanceSelect) {
    /* 绑定 change 事件 */
    relevanceSelect.addEventListener("change", applyFilters);
  }
  /* 重置按钮 */
  if (resetButton) {
    /* 绑定 click 事件 */
    resetButton.addEventListener("click", resetFilters);
  }

  /* 键盘快捷键：按「/」聚焦搜索框。
     这是检索类界面的通行惯例，能让常用操作少一次鼠标移动。
     必须排除输入框内触发的情况，否则在搜索框里打斜杠会被吞掉。 */
  document.addEventListener("keydown", function (event) {
    /* 只在按下 / 且未搭配修饰键时触发 */
    if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) {
      /* 不满足条件则放行 */
      return;
    }
    /* 焦点已在输入类元素内时不劫持按键 */
    var tag = event.target && event.target.tagName;
    /* 排除输入框、下拉与可编辑元素 */
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") {
      /* 放行，让斜杠正常输入 */
      return;
    }
    /* 阻止浏览器默认的「快速查找」行为 */
    event.preventDefault();
    /* 聚焦搜索框 */
    if (searchInput) {
      /* 聚焦 */
      searchInput.focus();
    }
  });

  /* 首屏先算一次：浏览器可能因为后退/刷新而保留了控件状态，
     若不初始化，页面会显示「全部政策」但控件却是上次的值。 */
  applyFilters();
})();
