"""Committee agents and the orchestration that runs them.

Mirrors the role split used by the TradingAgents style multi-agent pipeline:
four parallel analysts feed a structured bull/bear debate, which feeds a risk
officer and finally a trade executive. The last word belongs to the human.

Every agent runs against the live market snapshot. When no API key is present
the rule engine below takes over so the whole pipeline still produces a
coherent, data-grounded result instead of failing.
"""

from __future__ import annotations

import time

import news as news_module

# ------------------------------------------------------------------ roles --

ANALYSTS = [
    {
        "id": "fundamental",
        "name": "基本面分析师",
        "expertise": "财务 · 估值 · 竞争力",
        "focus": "估值水平（PE/PB）、市值规模、盈利能力与中长期成长性",
    },
    {
        "id": "technical",
        "name": "技术分析师",
        "expertise": "K线 · 均线 · 技术指标",
        "focus": "趋势结构、均线排列、MACD/RSI/布林带、成交量与波动率",
    },
    {
        "id": "news",
        "name": "新闻分析师",
        "expertise": "宏观 · 行业 · 事件",
        "focus": "宏观环境、行业景气度、公司事件对价格的影响",
    },
    {
        "id": "sentiment",
        "name": "情绪分析师",
        "expertise": "资金 · 市场热度",
        "focus": "资金流向、市场参与热度、短期超买超卖情绪",
    },
]

BULL = {
    "id": "bull",
    "name": "多头研究员",
    "expertise": "看涨逻辑 · 机会挖掘",
    "angle": "寻找被低估的机会与上行驱动",
}
BEAR = {
    "id": "bear",
    "name": "空头研究员",
    "expertise": "看跌逻辑 · 风险揭示",
    "angle": "寻找定价过高的风险与下行触发",
}

RISK_OFFICER = {
    "id": "risk",
    "name": "风险管理官",
    "expertise": "仓位 · 波动 · 回撤控制",
}
TRADER = {
    "id": "trader",
    "name": "交易执行官",
    "expertise": "交易策略 · 执行方案",
}

ALL_AGENTS = ANALYSTS + [BULL, BEAR, RISK_OFFICER, TRADER]

# ------------------------------------------------------------------ utils --


def fmt(value, digits=2, suffix="", dash="—"):
    """Format a number, tolerating None. The suffix is appended rather than
    interpolated so a literal '%' cannot break the format string."""
    if value is None:
        return dash
    if isinstance(value, (int, float)):
        try:
            return (("%." + str(int(digits)) + "f") % value) + suffix
        except (TypeError, ValueError):
            return dash
    return "%s%s" % (value, suffix)


def money(value, currency=""):
    if value is None:
        return "—"
    unit = "亿"
    if abs(value) >= 1e12:
        return "%s%.2f 万亿%s" % ("", value / 1e12, currency)
    if abs(value) >= 1e8:
        return "%.2f 亿%s" % (value / 1e8, currency)
    return "%.2f%s" % (value, currency)


def _pick(data, *path, default=None):
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def _sector_block(snapshot: dict) -> str:
    """The extra context a board carries that a single stock does not."""
    target = snapshot.get("target") or {}
    breadth = snapshot.get("breadth") or {}
    members = snapshot.get("members") or []
    moneyflow = snapshot.get("moneyflow") or {}

    kind_label = {
        "sector_cn": "A股板块", "sector_us": "美股行业ETF",
    }.get(target.get("kind"), "板块")
    board_label = {
        "industry": "行业板块", "concept": "概念板块", "etf": "行业ETF",
    }.get(target.get("board"), "")

    lines = ["【标的类型】%s %s" % (kind_label, board_label)]

    if breadth and breadth.get("total"):
        ratio = breadth.get("ratio")
        lines.append(
            "【涨跌家数】上涨 %s 家，下跌 %s 家，平盘 %s 家，涨停 %s 家，跌停 %s 家"
            "（共 %s 家，上涨占比 %s）" % (
                breadth.get("up"), breadth.get("down"), breadth.get("flat"),
                breadth.get("limit_up"), breadth.get("limit_down"),
                breadth.get("total"),
                fmt(ratio * 100.0, 1, "%") if ratio is not None else "—"))
    elif target.get("kind") == "sector_us":
        lines.append("【说明】美股行业ETF无成分股涨跌家数，请用ETF自身的量价与相对表现判断")

    if members:
        leaders = members[:5]
        laggards = members[-3:]
        lines.append("【领涨成分股】" + "；".join(
            "%s %s" % (m.get("name"), fmt(m.get("change_pct"), 2, "%")) for m in leaders))
        lines.append("【领跌成分股】" + "；".join(
            "%s %s" % (m.get("name"), fmt(m.get("change_pct"), 2, "%")) for m in laggards))

    if moneyflow.get("main") is not None:
        lines.append("【主力资金】当日净流入 %s（%s）" % (
            money(moneyflow.get("main"), "元"), moneyflow.get("date")))

    return "\n".join(lines)


def build_brief(snapshot: dict) -> str:
    """Render the market snapshot as a compact text brief for the agents."""
    q = snapshot["quote"]
    ind = snapshot["indicators"]
    lv = snapshot["levels"]
    macd_v = ind.get("macd") or {}
    boll = ind.get("boll") or {}
    cur = q.get("currency", "")

    lines = [
        "【标的】%s（%s）  市场：%s" % (
            q.get("name"), q.get("code"),
            "A股" if q.get("kind") == "A" else "美股"),
        "【行情】最新价 %s%s  涨跌幅 %s  今开 %s  最高 %s  最低 %s" % (
            fmt(q.get("price")), cur, fmt(q.get("change_pct"), 2, "%"),
            fmt(q.get("open")), fmt(q.get("high")), fmt(q.get("low"))),
        "【规模】总市值 %s  市盈率(TTM) %s  市净率 %s" % (
            money(q.get("market_cap"), cur), fmt(q.get("pe")), fmt(q.get("pb"))),
        "【区间表现】5日 %s  20日 %s  60日 %s" % (
            fmt(ind.get("change_5d_pct"), 2, "%"),
            fmt(ind.get("change_20d_pct"), 2, "%"),
            fmt(ind.get("change_60d_pct"), 2, "%")),
        "【均线】MA5 %s  MA10 %s  MA20 %s  MA60 %s" % (
            fmt(ind.get("ma5")), fmt(ind.get("ma10")),
            fmt(ind.get("ma20")), fmt(ind.get("ma60"))),
        "【MACD】DIF %s  DEA %s  柱 %s" % (
            fmt(macd_v.get("dif"), 3), fmt(macd_v.get("dea"), 3),
            fmt(macd_v.get("hist"), 3)),
        "【RSI14】%s" % fmt(ind.get("rsi14"), 1),
        "【布林带】下轨 %s  中轨 %s  上轨 %s" % (
            fmt(boll.get("lower")), fmt(boll.get("mid")), fmt(boll.get("upper"))),
        "【波动】ATR14 %s  5日/20日均量比 %s" % (
            fmt(ind.get("atr14")), fmt(ind.get("vol_ratio_5_20"))),
        "【区间极值】20日高 %s  20日低 %s  60日高 %s  60日低 %s" % (
            fmt(ind.get("high_20d")), fmt(ind.get("low_20d")),
            fmt(ind.get("high_60d")), fmt(ind.get("low_60d"))),
        "【程序测算价位】支撑 %s  压力 %s  止损参考 %s" % (
            fmt(lv.get("support")), fmt(lv.get("resistance")),
            fmt(lv.get("stop_loss"))),
        "【样本】共 %s 根日K，区间 %s 至 %s" % (
            snapshot.get("bars"), snapshot.get("first_date"), snapshot.get("last_date")),
    ]

    target = snapshot.get("target") or {}
    if str(target.get("kind", "")).startswith("sector"):
        lines.append(_sector_block(snapshot))

    if snapshot.get("kline_source") == "synthetic":
        lines.append("【数据说明】该板块的历史K线由市值最大的成分股加权合成，"
                     "趋势形态与当前点位可靠，但历史绝对点位与官方指数存在偏差。"
                     "引用均线或MACD数值时须说明这是合成序列，不要当作官方指数点位。")
    if snapshot.get("kline_error"):
        lines.append("【数据限制】%s。只能基于现有数据分析，禁止臆测均线或MACD的具体数值。"
                     % snapshot["kline_error"])
    elif not snapshot.get("indicators"):
        lines.append("【数据限制】缺失K线，无法计算均线与MACD，请改用其他维度判断。")

    return "\n".join(lines)


def build_news_block(news_data: dict) -> str:
    """Render the matched Jin10 bulletins for the news analyst."""
    if not news_data or not news_data.get("items"):
        return "【新闻源】本次未能获取到新闻快讯，请明确说明数据缺失，不要编造事件。"

    lines = ["【新闻源】金十数据快讯，扫描 %s 条，时间范围 %s" % (
        news_data.get("scanned"), news_data.get("span") or "未知")]

    if news_data.get("matched"):
        lines.append("【与标的直接相关】")
        for item in news_data["matched"][:6]:
            lines.append("- [%s]%s %s" % (
                item.get("time"), "（重要）" if item.get("important") else "",
                item.get("content")))
    else:
        lines.append("【与标的直接相关】本轮快讯中未检索到直接相关条目。")

    if news_data.get("macro"):
        lines.append("【宏观与市场要闻】")
        for item in news_data["macro"][:6]:
            lines.append("- [%s]%s %s" % (
                item.get("time"), "（重要）" if item.get("important") else "",
                item.get("content")))

    return "\n".join(lines)


def _clamp(value, low, high):
    return max(low, min(high, value))


def _trim(text, limit=34):
    """Shorten a sentence for quoting, marking the cut with an ellipsis."""
    body = (text or "").strip().rstrip("。")
    return body[:limit] + "…" if len(body) > limit else body


# ------------------------------------------------------------- rule engine --


def _rule_fundamental(snap):
    target = snap.get("target") or {}
    if str(target.get("kind", "")).startswith("sector"):
        return _rule_sector_fundamental(snap)

    q = snap["quote"]
    ind = snap.get("indicators") or {}
    pe, pb = q.get("pe"), q.get("pb")
    c20, c60 = ind.get("change_20d_pct"), ind.get("change_60d_pct")
    points, concerns = [], []
    score = 0

    if pe is None:
        concerns.append("市盈率数据缺失，无法判断估值高低")
    elif pe < 0:
        concerns.append("市盈率为负，公司当前处于亏损状态，估值参考意义有限")
        score -= 2
    elif pe < 20:
        points.append("市盈率 %s，处于相对温和的估值区间" % fmt(pe))
        score += 2
    elif pe < 45:
        points.append("市盈率 %s，估值中性，需要增长来消化" % fmt(pe))
        score += 0
    else:
        concerns.append("市盈率 %s 偏高，已包含较多增长预期" % fmt(pe))
        score -= 2

    if pb is not None:
        if pb < 3:
            points.append("市净率 %s，账面价值支撑相对扎实" % fmt(pb))
            score += 1
        elif pb > 10:
            concerns.append("市净率 %s 处于高位，对净资产的溢价明显" % fmt(pb))
            score -= 1

    if c60 is not None:
        if c60 > 20:
            points.append("近 60 日累计上涨 %s，中期动能向上" % fmt(c60, 1, "%"))
            score += 1
        elif c60 < -20:
            concerns.append("近 60 日累计下跌 %s，中期趋势走弱" % fmt(c60, 1, "%"))
            score -= 1
    if c20 is not None and c20 < -8:
        concerns.append("近 20 日回撤 %s，短期动能转弱" % fmt(c20, 1, "%"))
        score -= 1

    # Scale is context, not a directional argument, so it is listed last.
    cap = q.get("market_cap")
    if cap:
        points.append("总市值 %s，规模%s，流动性具备基础" % (
            money(cap, q.get("currency", "")),
            "较大" if cap > 5e10 else "中等"))

    stance, conf = _stance_from_score(score, 3)
    summary = _summarize("基本面", stance, q, ind)
    if not points:
        points.append("可用财务维度有限，主要依据估值与规模推断")
    if not concerns:
        concerns.append("缺少更细的财报科目，盈利质量需另行核实")
    return {
        "stance": stance, "confidence": conf, "summary": summary,
        "points": points[:3], "concerns": concerns[:2], "engine": "rule",
    }


def _rule_sector_fundamental(snap):
    """A board has no financial statements, so it is judged on internal structure:
    how broad the move is, where the money is going, and whether a few leaders
    are carrying the whole index."""
    q = snap["quote"]
    ind = snap.get("indicators") or {}
    members = snap.get("members") or []
    breadth = snap.get("breadth") or {}
    moneyflow = snap.get("moneyflow") or {}
    points, concerns = [], []
    score = 0

    pcts = [m.get("change_pct") for m in members
            if isinstance(m.get("change_pct"), (int, float))]
    if len(pcts) >= 6:
        lead = sum(pcts[:3]) / 3.0
        rest = sum(pcts[3:]) / float(len(pcts) - 3)
        points.append("成分股 %d 只，前三涨幅均值 %s，其余均值 %s" % (
            breadth.get("total") or len(pcts), fmt(lead, 2, "%"), fmt(rest, 2, "%")))
        if lead - rest > 4:
            concerns.append("涨幅高度集中在少数龙头，板块广度不足")
            score -= 1
        else:
            points.append("涨跌分布较均匀，非个别股拉动的虚涨")
            score += 1

    ratio = breadth.get("ratio")
    if ratio is not None:
        if ratio > 0.6:
            points.append("上涨家数占比 %s，内部共振向上" % fmt(ratio * 100.0, 1, "%"))
            score += 1
        elif ratio < 0.4:
            concerns.append("上涨家数占比仅 %s，多数个股承压" % fmt(ratio * 100.0, 1, "%"))
            score -= 1

    if moneyflow.get("main") is not None:
        main = moneyflow["main"]
        if main > 0:
            points.append("主力资金净流入 %s，资金面形成支撑" % money(abs(main), "元"))
            score += 1
        else:
            concerns.append("主力资金净流出 %s，资金面偏弱" % money(abs(main), "元"))
            score -= 1

    c60 = ind.get("change_60d_pct")
    if c60 is not None:
        if c60 > 10:
            points.append("板块指数近 60 日上涨 %s，中期趋势向上" % fmt(c60, 1, "%"))
            score += 1
        elif c60 < -10:
            concerns.append("板块指数近 60 日下跌 %s，中期趋势向下" % fmt(c60, 1, "%"))
            score -= 1
        else:
            points.append("板块指数近 60 日 %s，中期横盘震荡" % fmt(c60, 1, "%"))

    stance, conf = _stance_from_score(score, 3)
    return {
        "stance": stance, "confidence": conf,
        "summary": "板块结构%s，指数 %s" % (
            {"偏多": "内部共振向上", "偏空": "内部分化承压", "中性": "中性"}.get(stance, "中性"),
            fmt(q.get("price"))),
        "points": points[:4],
        "concerns": concerns[:2] or ["板块无财报维度，具体标的质量需回到个股层面确认"],
        "engine": "rule",
    }


def _rule_technical(snap):
    ind = snap.get("indicators") or {}
    if not ind:
        return {
            "stance": "中性", "confidence": 40,
            "summary": "缺少K线数据，无法进行技术面分析",
            "points": ["板块K线暂不可用，均线、MACD、RSI 均无法计算"],
            "concerns": ["技术面维度缺失，本次判断须依赖资金与内部结构数据"],
            "engine": "rule",
        }

    q = snap["quote"]
    lv = snap.get("levels") or {}
    price = q.get("price") or ind.get("last_close")
    ma5, ma20, ma60 = ind.get("ma5"), ind.get("ma20"), ind.get("ma60")
    macd_v = ind.get("macd") or {}
    boll = ind.get("boll") or {}
    rsi_v = ind.get("rsi14")
    points, concerns = [], []
    score = 0

    if price and ma20:
        if price > ma20:
            points.append("股价站上 MA20（%s），中期结构偏强" % fmt(ma20))
            score += 2
        else:
            concerns.append("股价跌破 MA20（%s），中期结构承压" % fmt(ma20))
            score -= 2
    if price and ma60 and ma20 and ma20 > ma60:
        points.append("MA20 位于 MA60 上方，均线呈多头排列")
        score += 1
    elif ma20 and ma60 and ma20 < ma60:
        concerns.append("MA20 位于 MA60 下方，均线仍偏空")
        score -= 1

    if macd_v:
        if macd_v.get("hist", 0) > 0:
            points.append("MACD 柱状值为正，动能偏多")
            score += 1
        else:
            concerns.append("MACD 柱状值为负，动能偏空")
            score -= 1
        if macd_v.get("golden_cross"):
            points.append("MACD 刚刚形成金叉，短线或有反弹")
            score += 1

    if rsi_v is not None:
        if rsi_v >= 70:
            concerns.append("RSI14 达 %s，进入超买区间，追高风险上升" % fmt(rsi_v, 1))
            score -= 2
        elif rsi_v <= 30:
            points.append("RSI14 仅 %s，处于超卖区间，存在修复空间" % fmt(rsi_v, 1))
            score += 2
        else:
            points.append("RSI14 为 %s，多空力量相对均衡" % fmt(rsi_v, 1))

    if price and boll.get("upper") and boll.get("lower"):
        if price > boll["upper"]:
            concerns.append("股价触及布林上轨，短期偏离度偏大")
            score -= 1
        elif price < boll["lower"]:
            points.append("股价触及布林下轨，超跌反弹概率上升")
            score += 1

    atr_v, vr = ind.get("atr14"), ind.get("vol_ratio_5_20")
    if atr_v and price:
        atr_pct = atr_v / price * 100.0
        concerns.append("ATR14 占股价 %.1f%%，日内波动较大，需控制仓位" % atr_pct)
    if vr:
        points.append("近 5 日量能为 20 日均量的 %s 倍" % fmt(vr, 2))

    stance, conf = _stance_from_score(score, 4)
    return {
        "stance": stance, "confidence": conf,
        "summary": "技术面%s：现价 %s，MA20 %s，RSI %s" % (
            {"偏多": "偏强", "偏空": "偏弱", "中性": "震荡"}.get(stance, "中性"),
            fmt(price), fmt(ma20), fmt(rsi_v, 1)),
        "points": points[:3], "concerns": concerns[:2], "engine": "rule",
    }


def _rule_news(snap, news_data=None):
    """News desk, driven by the live Jin10 flash feed."""
    ind = snap.get("indicators") or {}
    news_data = news_data or {}
    items = news_data.get("items") or []

    if not items:
        return {
            "stance": "中性", "confidence": 45,
            "summary": "本次未能获取新闻快讯，消息面无法判断",
            "points": ["新闻源不可用，未取到任何快讯"],
            "concerns": ["缺少消息面输入，事件驱动因素完全未知"],
            "engine": "rule",
        }

    matched = news_data.get("matched") or []
    macro = news_data.get("macro") or []
    important_macro = [m for m in macro if m.get("important")]

    points, concerns = [], []
    if matched:
        points.append("检索到 %d 条与标的直接相关的快讯" % len(matched))
        for item in matched[:2]:
            stamp = (item.get("time") or "")[11:16]
            points.append("%s%s" % ("[%s] " % stamp if stamp else "",
                                    item.get("content", "")[:56]))
    else:
        concerns.append("快讯中未检索到与标的直接相关的条目，缺乏直接催化")

    if macro:
        points.append("宏观要闻 %d 条，其中重要 %d 条" % (len(macro), len(important_macro)))
        if important_macro:
            points.append("最受关注：%s" % important_macro[0].get("content", "")[:56])

    # Direction still comes from price action; news volume only firms up
    # confidence, because a headline feed alone cannot score direction.
    c20 = ind.get("change_20d_pct")
    stance = "中性"
    if c20 is not None:
        stance = "偏多" if c20 > 5 else ("偏空" if c20 < -5 else "中性")

    confidence = 45 + min(15, len(matched) * 3) + min(10, len(important_macro) * 2)

    return {
        "stance": stance, "confidence": _clamp(confidence, 40, 80),
        "summary": "快讯扫描 %d 条，直接相关 %d 条，消息面%s" % (
            len(items), len(matched),
            "偏正面" if stance == "偏多" else ("偏负面" if stance == "偏空" else "中性")),
        "points": points[:4],
        "concerns": concerns[:2] or ["消息面暂无明显风险信号"],
        "engine": "rule",
    }


def _rule_sentiment(snap):
    ind = snap.get("indicators") or {}
    breadth = snap.get("breadth") or {}
    moneyflow = snap.get("moneyflow") or {}
    rsi_v, vr = ind.get("rsi14"), ind.get("vol_ratio_5_20")
    c5 = ind.get("change_5d_pct")
    points, concerns = [], []
    score = 0

    # For a board, the advance/decline split is the most direct sentiment read
    # there is - far more telling than the index move alone.
    if breadth.get("total"):
        up = breadth.get("up") or 0
        down = breadth.get("down") or 0
        ratio = breadth.get("ratio")
        ratio = 0.0 if ratio is None else ratio
        if ratio >= 0.7:
            points.append("板块内 %d 家上涨 / %d 家下跌，赚钱效应明显" % (up, down))
            score += 2
        elif ratio <= 0.3:
            concerns.append("板块内仅 %d 家上涨 / %d 家下跌，普跌格局" % (up, down))
            score -= 2
        else:
            points.append("板块内 %d 家上涨 / %d 家下跌，多空相对均衡" % (up, down))
        if breadth.get("limit_up"):
            points.append("其中涨停 %d 家，短线资金仍在活跃" % breadth["limit_up"])
            score += 1

    if moneyflow.get("main") is not None:
        main = moneyflow["main"]
        points.append("主力资金当日%s %s" % (
            "净流入" if main > 0 else "净流出", money(abs(main), "元")))
        score += 1 if main > 0 else -1

    if rsi_v is not None:
        if rsi_v > 65:
            concerns.append("RSI %s 显示情绪偏热，短期追涨意愿可能透支" % fmt(rsi_v, 1))
            score -= 1
        elif rsi_v < 35:
            points.append("RSI %s 显示情绪偏冷，悲观预期已有所释放" % fmt(rsi_v, 1))
            score += 1
        else:
            points.append("RSI %s 显示情绪中性，未出现极端状态" % fmt(rsi_v, 1))

    if vr is not None:
        if vr > 1.3:
            points.append("近 5 日量能为 20 日均量的 %s 倍，关注度上升" % fmt(vr, 2))
            score += 1
        elif vr < 0.7:
            concerns.append("近 5 日量能萎缩至均量的 %s 倍，人气偏低" % fmt(vr, 2))
            score -= 1
        else:
            points.append("成交量维持常态水平（%s 倍均量）" % fmt(vr, 2))

    if c5 is not None:
        points.append("近 5 日涨跌 %s，短期资金态度%s" % (
            fmt(c5, 1, "%"), "积极" if c5 > 0 else "谨慎"))

    stance, conf = _stance_from_score(score, 2)
    if not points:
        points.append("可用情绪指标有限，暂难判断资金态度")
    if not concerns:
        concerns.append("缺少龙虎榜与融资融券数据，情绪判断精度有限")
    return {
        "stance": stance, "confidence": conf,
        "summary": "情绪面%s，量能%s" % (
            "偏暖" if stance == "偏多" else ("偏冷" if stance == "偏空" else "中性"),
            fmt(vr, 2)),
        "points": points[:4], "concerns": concerns[:2], "engine": "rule",
    }


def _stance_from_score(score, scale):
    """Map a signed score onto a stance plus a believable confidence."""
    if score >= scale:
        return "偏多", _clamp(62 + score * 4, 55, 88)
    if score <= -scale:
        return "偏空", _clamp(62 + abs(score) * 4, 55, 88)
    return "中性", _clamp(52 + abs(score) * 3, 45, 68)


def _summarize(kind, stance, q, ind):
    return "%s%s，现价 %s" % (
        kind,
        {"偏多": "偏乐观", "偏空": "偏谨慎", "中性": "中性"}.get(stance, "中性"),
        fmt(q.get("price")))


def _rule_researcher(side, reports, snap, opponent=None):
    """Bull/bear derived from the analyst reports.

    Confidence is anchored to the team's net lean and reflected around the
    midpoint, so the two sides stay balanced - counting raw evidence bullets
    would hand the bull a structural advantage. The news desk is excluded from
    the evidence pool because it has no live feed behind it.
    """
    positive, negative = [], []
    for rid, rep in reports.items():
        if rid == "news":
            continue
        positive.extend(rep.get("points") or [])
        negative.extend(rep.get("concerns") or [])

    # Rank evidence: hard data first, caveats and size descriptors last.
    def _rank(text):
        caveat = any(k in text for k in ("缺失", "缺少", "未接入", "无法判断", "另行核实"))
        context = any(k in text for k in ("规模较大", "规模中等", "流动性具备基础"))
        return (caveat, context)

    negative.sort(key=_rank)
    positive.sort(key=_rank)

    bias = sum(
        {"偏多": 1, "中性": 0, "偏空": -1}.get((rep or {}).get("stance"), 0)
        for rep in reports.values()
    )

    ind, lv = snap["indicators"], snap["levels"]
    price = snap["quote"].get("price") or ind.get("last_close")

    if side == "bull":
        picks = positive[:3] or ["估值与趋势未出现明显恶化，具备修复基础"]
        counter = negative[:2]
        conf = _clamp(60 + bias * 6, 42, 86)
        argument = "%s，上行空间看向压力位 %s" % (
            _trim(picks[0]) if picks else "估值具备修复基础", fmt(lv.get("resistance")))
        rebuttal = "对空头的回应：%s，但当前位置距支撑位 %s 不远，风险收益比仍可接受" % (
            _trim(counter[0], 26) if counter else "风险因素确实存在", fmt(lv.get("support")))
        stance = "偏多"
    else:
        picks = negative[:3] or ["缺少明确的向上驱动，性价比一般"]
        counter = positive[:2]
        conf = _clamp(60 - bias * 6, 42, 86)
        argument = "%s，下行风险指向支撑位 %s" % (
            _trim(picks[0]) if picks else "估值与波动风险仍需消化", fmt(lv.get("support")))
        rebuttal = "对多头的回应：%s，但压力位 %s 未突破前，追高风险大于收益" % (
            _trim(counter[0], 26) if counter else "上行逻辑成立", fmt(lv.get("resistance")))
        stance = "偏空"

    if opponent:
        prev = (opponent.get("argument") or "").strip()
        if prev:
            rebuttal = "对方主张「%s」；我方认为%s" % (
                _trim(prev, 28),
                "该逻辑未充分计价下行风险" if side == "bear" else "该担忧已被当前价格部分消化")
            # Later rounds must move the argument, not just the rebuttal.
            counter_point = "回应「%s」：该因素%s" % (
                _trim(prev, 20),
                "已基本被当前价格反映" if side == "bull" else "尚未被市场充分定价")
            picks = (picks[:2] + [counter_point])[:3]

    return {
        "stance": stance, "confidence": conf, "argument": argument,
        "points": picks, "rebuttal": rebuttal, "engine": "rule",
    }


def _rule_risk(snap, reports, bull, bear):
    ind, lv = snap["indicators"], snap["levels"]
    price = snap["quote"].get("price") or ind.get("last_close")
    atr_v = ind.get("atr14") or (price * 0.02 if price else None)
    atr_pct = (atr_v / price * 100.0) if (atr_v and price) else 3.0

    # Position sizing shrinks as volatility rises.
    max_pos = _clamp(int(45 - atr_pct * 2.5), 10, 40)
    stop = lv.get("stop_loss")
    warnings = []
    if atr_pct > 4:
        warnings.append("日均波动 %.1f%% 偏高，单一标的仓位不宜超过 %d%%" % (atr_pct, max_pos))
    if _pick(ind, "rsi14") and ind["rsi14"] > 68:
        warnings.append("RSI 处于高位，需警惕短期回撤")
    if bear.get("confidence", 0) >= 70:
        warnings.append("空头研究员置信度达 %s，分歧较大" % bear.get("confidence"))
    if stop:
        warnings.append("跌破 %s 应严格止损，避免亏损扩大" % fmt(stop))
    else:
        warnings.append("缺少K线无法测算止损位，建议按固定比例（如 -8%）作为风控上限")

    gap = abs(bull.get("confidence", 50) - bear.get("confidence", 50))
    conf = _clamp(70 - gap // 3, 45, 78)

    if stop:
        advice = "建议单标的上限 %d%%，止损设于 %s，分批建仓降低择时风险" % (
            max_pos, fmt(stop))
    else:
        advice = ("建议单标的上限 %d%%；技术数据缺失，先以仓位控制为主，"
                  "暂不设精确止损价" % max_pos)

    return {
        "stance": "中性", "confidence": conf,
        "max_position_pct": max_pos,
        "stop_loss": round(stop, 2) if stop else None,
        "warnings": warnings[:3],
        "advice": advice,
        "engine": "rule",
    }


def _rule_trader(snap, reports, bull, bear, risk):
    q, ind, lv = snap["quote"], snap["indicators"], snap["levels"]
    price = q.get("price") or ind.get("last_close")

    bull_c = bull.get("confidence", 50)
    bear_c = bear.get("confidence", 50)
    tech = reports.get("technical") or {}
    fund = reports.get("fundamental") or {}

    net = bull_c - bear_c
    tech_bias = {"偏多": 8, "中性": 0, "偏空": -8}.get(tech.get("stance"), 0)
    fund_bias = {"偏多": 6, "中性": 0, "偏空": -6}.get(fund.get("stance"), 0)
    score = net + tech_bias + fund_bias

    if score >= 14:
        rating = "BUY"
    elif score <= -14:
        rating = "SELL"
    else:
        rating = "HOLD"

    consensus = _clamp(int(50 + abs(score) * 1.4), 40, 88)
    position = risk.get("max_position_pct", 20)
    lo = max(5, int(position * 0.6))
    hi = int(position)

    thesis = []
    thesis.append((tech.get("summary") or "技术面数据不足，趋势结构无法判断")[:42])
    thesis.append((fund.get("summary") or "基本面数据不足，估值参考有限")[:42])
    thesis.append("多空分歧度 %d 点，方向%s" % (
        abs(net), "偏多" if net > 0 else ("偏空" if net < 0 else "均衡")))

    risks = list(bear.get("points") or [])[:2]
    risks += list(risk.get("warnings") or [])[:1]
    if not risks:
        risks = ["估值波动风险", "市场系统性风险"]

    has_levels = all(isinstance(lv.get(k), (int, float))
                     for k in ("support", "target_low", "stop_loss"))

    if has_levels:
        conditions = [
            "站上 %s 且量能配合，视为策略有效" % fmt(lv.get("support")),
            "跌破 %s 则策略失效，执行止损" % fmt(lv.get("stop_loss")),
            "宏观或行业出现重大不利变化时重新评估",
        ]
    else:
        conditions = [
            "板块资金由净流出转为持续净流入，视为结构改善",
            "板块内上涨家数占比回升至 60% 以上，视为共振确认",
            "宏观或行业出现重大不利变化时重新评估",
        ]

    if not has_levels:
        action = ("缺少K线数据，无法测算关键价位。建议先跟踪板块资金流向与内部结构变化，"
                  "待历史数据恢复后再制定具体交易计划")
    elif rating == "BUY":
        action = "现价附近分批建仓，回踩 %s 加仓，目标 %s，跌破 %s 止损" % (
            fmt(lv.get("support")), fmt(lv.get("target_low")), fmt(lv.get("stop_loss")))
    elif rating == "SELL":
        action = "建议回避或减仓，反弹至 %s 附近减持，跌破 %s 加速离场" % (
            fmt(lv.get("resistance")), fmt(lv.get("stop_loss")))
    else:
        action = "暂不追高，等待回踩 %s 区间分批低吸，跌破 %s 止损，目标 %s" % (
            fmt(lv.get("support")), fmt(lv.get("stop_loss")), fmt(lv.get("target_low")))

    return {
        "rating": rating, "consensus": consensus,
        "thesis": thesis, "risks": risks[:3], "conditions": conditions,
        "levels": {
            "support": _round(lv.get("support")),
            "resistance": _round(lv.get("resistance")),
            "support_far": _round(lv.get("support_far")),
            "target_low": _round(lv.get("target_low")),
            "target_high": _round(lv.get("target_high")),
            "position_pct": [lo, hi],
            "stop_loss": _round(lv.get("stop_loss")),
        },
        "action": action,
        "engine": "rule",
    }


def _round(value, digits=2):
    return round(value, digits) if isinstance(value, (int, float)) else None


RULE_ANALYSTS = {
    "fundamental": _rule_fundamental,
    "technical": _rule_technical,
    "news": _rule_news,
    "sentiment": _rule_sentiment,
}


# ----------------------------------------------------------- llm prompts --

ANALYST_SYSTEM = """你是投资委员会里的{name}，专长是{focus}。

请基于给定的市场数据，给出你的专业判断。要求：
1. 只输出一个 JSON 对象，不要输出任何解释性文字或代码块标记。
2. 所有数字必须来自给定数据或由其直接计算得出，禁止编造。
3. 数据缺失时在 points 中写明「数据缺失」，不要猜测。
4. 使用简体中文，结论先行，不写套话。

JSON 结构：
{{"stance": "偏多 或 偏空 或 中性", "confidence": 0 到 100 的整数,
"summary": "一句话核心结论，40字以内",
"points": ["论据1", "论据2", "论据3"],
"concerns": ["主要顾虑1", "主要顾虑2"]}}"""

RESEARCHER_SYSTEM = """你是投资委员会里的{name}，职责是从「{angle}」的角度审视分析师团队的报告，
并与另一位持相反立场的研究员进行辩论。

要求：
1. 只输出一个 JSON 对象，不要输出任何其他文字。
2. 必须直接回应对方的观点，写进 rebuttal 字段。
3. 承认对方最强的一点，再说明为什么你的判断依然成立。
4. 数字必须来自给定数据或由其直接计算，禁止编造。
5. 使用简体中文，观点鲜明，不写套话。

JSON 结构：
{{"stance": "偏多 或 偏空", "confidence": 0 到 100 的整数,
"argument": "你的核心论点，80字以内",
"points": ["支撑论据1", "支撑论据2", "支撑论据3"],
"rebuttal": "对对手观点的直接回应，80字以内"}}"""

RISK_SYSTEM = """你是投资委员会的风险管理官，负责在交易方案成型前做独立的风险审查。

要求：
1. 只输出一个 JSON 对象。
2. 仓位建议要基于波动率给出，波动越大仓位越小。
3. 数字必须来自给定数据，禁止编造。
4. 使用简体中文。

JSON 结构：
{{"stance": "中性 或 偏空", "confidence": 0 到 100 的整数,
"max_position_pct": 建议的单一标的仓位上限整数百分比,
"stop_loss": 止损价数字,
"warnings": ["风险提示1", "风险提示2", "风险提示3"],
"advice": "整体风控建议，80字以内"}}"""

TRADER_SYSTEM = """你是投资委员会的交易执行官，负责综合全部分析与辩论结果，输出最终可执行的交易方案。

要求：
1. 只输出一个 JSON 对象。
2. rating 只能是 BUY、HOLD、SELL 三者之一。
3. 所有价位必须来自给定数据中的支撑/压力/止损参考，可以取整但不得编造。
4. 使用简体中文，措辞像真实交易指令。

JSON 结构：
{{"rating": "BUY 或 HOLD 或 SELL",
"consensus": 0 到 100 的整数，表示委员会共识度,
"thesis": ["核心逻辑1", "核心逻辑2", "核心逻辑3"],
"risks": ["主要风险1", "主要风险2", "主要风险3"],
"levels": {{"support": 数字, "resistance": 数字, "target_low": 数字,
"target_high": 数字, "position_pct": [下限整数, 上限整数], "stop_loss": 数字}},
"conditions": ["策略有效性条件1", "策略有效性条件2", "策略有效性条件3"],
"action": "一句话可执行指令，80字以内"}}"""


def _run_llm_agent(llm, system, user, fallback, emitter=None):
    """Call the model; on any failure fall back to the rule engine result."""
    try:
        data = llm.chat_json(system, user)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        data["engine"] = "llm"
        return data
    except Exception as exc:
        if emitter:
            emitter({"type": "warn", "message": "模型调用失败，已降级到规则引擎：%s" % exc})
        return fallback


# ------------------------------------------------------------ orchestration --


def _collect_news(snapshot: dict, emit=None) -> dict:
    """Gather the Jin10 stream and match it against the target.

    Never fatal: if the feed is down the news analyst is told so explicitly
    rather than being handed nothing and left to improvise.
    """
    try:
        items = news_module.fetch_flash()
        result = news_module.related_news(snapshot, items)
        result["items"] = items
        if items:
            result["span"] = "%s ~ %s" % (items[-1].get("time"), items[0].get("time"))
        if emit:
            emit({"type": "news", "count": len(items),
                  "matched": len(result.get("matched") or []),
                  "keywords": (result.get("keywords") or [])[:6]})
        return result
    except Exception as exc:
        if emit:
            emit({"type": "warn", "message": "新闻源暂时不可用：%s" % exc})
        return {"items": [], "matched": [], "macro": [], "keywords": [], "span": None}


def run_committee(snapshot: dict, llm, cfg: dict, emit, should_stop=None):
    """Execute the whole committee. Returns the consensus payload."""
    committee_cfg = (cfg or {}).get("committee") or {}
    debate_rounds = max(1, int(committee_cfg.get("debate_rounds", 2)))
    use_llm = llm.ready
    brief = build_brief(snapshot)
    news_data = _collect_news(snapshot, emit)
    news_block = build_news_block(news_data)

    def stop_requested():
        return bool(should_stop and should_stop())

    def say(agent, text, data=None, kind="speech"):
        emit({
            "type": kind,
            "agent": agent["id"],
            "name": agent["name"],
            "title": agent.get("expertise", ""),
            "text": text,
            "data": data or {},
            "ts": time.strftime("%H:%M:%S"),
        })

    target_kind = str((snapshot.get("target") or {}).get("kind") or "stock")
    note = "板块行情、成分股与资金流数据已就绪" if target_kind.startswith("sector") \
        else "行情、财务与K线数据已就绪"
    if news_data.get("items"):
        note += "；金十快讯 %d 条，其中直接相关 %d 条" % (
            len(news_data["items"]), len(news_data.get("matched") or []))
    else:
        note += "；新闻源暂不可用"
    emit({"type": "stage", "stage": "collect", "status": "done", "note": note})

    # ---------------------------------------------------- stage: analysts --
    emit({"type": "stage", "stage": "analyze", "status": "running"})
    reports = {}
    for agent in ANALYSTS:
        if stop_requested():
            emit({"type": "stopped"})
            return None
        emit({"type": "agent_start", "agent": agent["id"]})

        if agent["id"] == "news":
            fallback = _rule_news(snapshot, news_data)
        else:
            fallback = RULE_ANALYSTS[agent["id"]](snapshot)

        if use_llm:
            system = ANALYST_SYSTEM.format(name=agent["name"], focus=agent["focus"])
            if agent["id"] == "news":
                user = ("以下是该标的的实时市场数据：\n\n%s\n\n%s\n\n"
                        "请结合上述真实快讯给出消息面判断；"
                        "若没有直接相关快讯，就明确说明并以宏观要闻为背景。"
                        % (brief, news_block))
            else:
                user = "以下是该标的的实时市场数据：\n\n%s\n\n请给出你的分析。" % brief
            result = _run_llm_agent(llm, system, user, fallback, emit)
        else:
            result = fallback

        reports[agent["id"]] = result
        say(agent, result.get("summary") or "", result)

    emit({"type": "stage", "stage": "analyze", "status": "done"})

    # ----------------------------------------------------- stage: debate --
    emit({"type": "stage", "stage": "debate", "status": "running"})
    reports_text = "\n".join(
        "【%s】立场：%s（置信度 %s）\n结论：%s\n论据：%s" % (
            next(a["name"] for a in ANALYSTS if a["id"] == rid),
            rep.get("stance"), rep.get("confidence"), rep.get("summary"),
            "；".join(rep.get("points") or []))
        for rid, rep in reports.items()
    )

    bull_result = bear_result = None
    for round_index in range(debate_rounds):
        if stop_requested():
            emit({"type": "stopped"})
            return None
        for agent, opponent in ((BULL, bear_result), (BEAR, bull_result)):
            emit({"type": "agent_start", "agent": agent["id"]})
            fallback = _rule_researcher(
                "bull" if agent["id"] == "bull" else "bear",
                reports, snapshot, opponent)

            if use_llm:
                system = RESEARCHER_SYSTEM.format(name=agent["name"], angle=agent["angle"])
                parts = [
                    "市场数据：\n%s" % brief,
                    "分析师团队报告：\n%s" % reports_text,
                    "当前是第 %d 轮辩论。" % (round_index + 1),
                ]
                if opponent:
                    parts.append("对手上一轮的核心论点：%s" % opponent.get("argument"))
                    parts.append("对手的回应：%s" % opponent.get("rebuttal"))
                user = "\n\n".join(parts) + "\n\n请给出你的论点。"
                result = _run_llm_agent(llm, system, user, fallback, emit)
            else:
                result = fallback

            if agent["id"] == "bull":
                bull_result = result
            else:
                bear_result = result

            prefix = "第%d轮 " % (round_index + 1) if debate_rounds > 1 else ""
            say(agent, prefix + (result.get("argument") or ""), result)

    emit({"type": "stage", "stage": "debate", "status": "done"})

    # ------------------------------------------------------- stage: risk --
    emit({"type": "stage", "stage": "risk", "status": "running"})
    emit({"type": "agent_start", "agent": "risk"})
    fallback_risk = _rule_risk(snapshot, reports, bull_result or {}, bear_result or {})
    if use_llm:
        user = "\n\n".join([
            "市场数据：\n%s" % brief,
            "多头论述（置信度 %s）：%s" % (
                (bull_result or {}).get("confidence"), (bull_result or {}).get("argument")),
            "空头论述（置信度 %s）：%s" % (
                (bear_result or {}).get("confidence"), (bear_result or {}).get("argument")),
            "程序测算的止损参考价：%s，建议仓位上限参考：%s%%" % (
                fmt(_pick(snapshot, "levels", "stop_loss")),
                fallback_risk.get("max_position_pct")),
            "请给出你的风险审查意见。",
        ])
        risk_result = _run_llm_agent(llm, RISK_SYSTEM, user, fallback_risk, emit)
    else:
        risk_result = fallback_risk
    say(RISK_OFFICER, risk_result.get("advice") or "", risk_result)
    emit({"type": "stage", "stage": "risk", "status": "done"})

    # ----------------------------------------------------- stage: trader --
    emit({"type": "stage", "stage": "trader", "status": "running"})
    emit({"type": "agent_start", "agent": "trader"})
    fallback_trader = _rule_trader(
        snapshot, reports, bull_result or {}, bear_result or {}, risk_result)
    if use_llm:
        user = "\n\n".join([
            "市场数据：\n%s" % brief,
            "分析师团队报告：\n%s" % reports_text,
            "多头研究员：%s（置信度 %s）" % (
                (bull_result or {}).get("argument"), (bull_result or {}).get("confidence")),
            "空头研究员：%s（置信度 %s）" % (
                (bear_result or {}).get("argument"), (bear_result or {}).get("confidence")),
            "风险管理官：%s（仓位上限 %s%%）" % (
                risk_result.get("advice"), risk_result.get("max_position_pct")),
            "请综合以上内容，输出最终交易方案。",
        ])
        trader_result = _run_llm_agent(llm, TRADER_SYSTEM, user, fallback_trader, emit)
    else:
        trader_result = fallback_trader

    # Guarantee the machine-computed levels survive whatever the model said.
    trader_result = _sanitize_levels(trader_result, fallback_trader, snapshot)
    say(TRADER, trader_result.get("action") or "", trader_result)
    emit({"type": "stage", "stage": "trader", "status": "done"})

    consensus = {
        "symbol": snapshot["quote"],
        "rating": trader_result.get("rating", "HOLD"),
        "consensus": trader_result.get("consensus", 50),
        "thesis": trader_result.get("thesis") or [],
        "risks": trader_result.get("risks") or [],
        "levels": trader_result.get("levels") or {},
        "conditions": trader_result.get("conditions") or [],
        "action": trader_result.get("action") or "",
        "debate_rounds": debate_rounds,
        "engine": "llm" if use_llm else "rule",
        "members": [
            {"id": a["id"], "name": a["name"], "expertise": a["expertise"],
             "stance": (reports.get(a["id"]) or {}).get("stance"),
             "confidence": (reports.get(a["id"]) or {}).get("confidence")}
            for a in ANALYSTS
        ] + [
            {"id": BULL["id"], "name": BULL["name"], "expertise": BULL["expertise"],
             "stance": (bull_result or {}).get("stance"),
             "confidence": (bull_result or {}).get("confidence")},
            {"id": BEAR["id"], "name": BEAR["name"], "expertise": BEAR["expertise"],
             "stance": (bear_result or {}).get("stance"),
             "confidence": (bear_result or {}).get("confidence")},
            {"id": RISK_OFFICER["id"], "name": RISK_OFFICER["name"],
             "expertise": RISK_OFFICER["expertise"],
             "stance": risk_result.get("stance"),
             "confidence": risk_result.get("confidence")},
            {"id": TRADER["id"], "name": TRADER["name"], "expertise": TRADER["expertise"],
             "stance": "中性", "confidence": trader_result.get("consensus")},
        ],
    }

    emit({"type": "stage", "stage": "human", "status": "waiting",
          "note": "等待你的最终决策"})
    emit({"type": "consensus", "data": consensus})
    return consensus


def _sanitize_levels(trader, fallback, snapshot):
    """Keep model prose but restore machine-computed price levels if the model
    returned missing or nonsensical numbers."""
    out = dict(trader or {})
    levels = dict(out.get("levels") or {})
    ref = fallback.get("levels") or {}
    anchor = _pick(snapshot, "quote", "price")

    for key in ("support", "resistance", "target_low", "target_high", "stop_loss"):
        value = levels.get(key)
        if not isinstance(value, (int, float)) or value <= 0:
            levels[key] = ref.get(key)

    # Enforce a sane ordering around the current price.
    support, resistance = levels.get("support"), levels.get("resistance")
    if anchor and isinstance(support, (int, float)) and support >= anchor:
        levels["support"] = ref.get("support")
    if anchor and isinstance(resistance, (int, float)) and resistance <= anchor:
        levels["resistance"] = ref.get("resistance")
    if not isinstance(levels.get("support_far"), (int, float)):
        levels["support_far"] = ref.get("support_far")

    pos = levels.get("position_pct")
    if (not isinstance(pos, list) or len(pos) != 2
            or not all(isinstance(x, (int, float)) for x in pos)):
        levels["position_pct"] = ref.get("position_pct")

    out["levels"] = levels
    if out.get("rating") not in ("BUY", "HOLD", "SELL"):
        out["rating"] = fallback.get("rating", "HOLD")
    try:
        out["consensus"] = int(_clamp(float(out.get("consensus", 50)), 0, 100))
    except (TypeError, ValueError):
        out["consensus"] = fallback.get("consensus", 50)
    return out
