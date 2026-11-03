#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""review.py — a-share-review 编排入口 v2 (手动运行, 本机)。

v2 变更(2026-11-02 设计升级: 从"日报快照"到"连续复盘系统"):
- 时间序列: outputs/machine.jsonl 每日 append 全模块结构化快照
  → 板块资金流连续性标注(第N日流入/流出)、量能趋势、watchlist 触发位收敛追踪
- 昨日观察回检: 解析昨日 review.md 的"明日观察" + 昨日 machine 快照,
  机检项(触发位/MA5)自动判定, 生成 ⟦CLAUDE⟧ 回检段 —— 判断积累信用
- watchlist 分组双卡: 带 cost = holding 卡(距成本/E1三线预警/回撤裁定)
  无 cost = watching 卡(触发位收敛%/R2新高进度/披露倒计时)
- 事件区: 解禁日历(东财, 免费) + 下期预约披露日(东财, 免费)
- 财报三线: 问财 1 次批量(额度预算 3-5 → 4-6 次/日)
- 判断素材包: outputs/{date}/notes_for_claude.md (昨日资金 diff/连板对比/量能/回检),
  Claude 润色前必读, 内容不进正稿
- 降级可见性: manifest.json 记录 degraded 清单, 草稿尾部自动列"本次数据降级项"
- 两市成交口径: 上证+深证综指(旧版五指数加总重复计算); M判定分层 MA5/20 + MA250年线β

用法:
  python3 review.py                          # 全自动: 取数→图表→markdown→飞书发布
  python3 review.py --no-publish             # 只产出 outputs/{date}/review.md + 图表(Claude润色后再发)
  python3 review.py --publish                # 发布已存在的 outputs/{date}/review.md
  python3 review.py --set 603268:194.67,603259:159.39   # holding(带成本)
  python3 review.py --add 300750             # watching(无成本) 追加
  python3 review.py --remove 603268 --list --clear

隐私: watchlist.json 与 outputs/ 均在 .gitignore 中, 永不进仓库。
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import data  # noqa: E402
import charts  # noqa: E402

WATCHLIST = os.path.join(ROOT, "watchlist.json")
OUTROOT = os.path.join(ROOT, "outputs")
FEISHU_CACHE = os.path.join(OUTROOT, "feishu_folder.json")
MACHINE = os.path.join(OUTROOT, "machine.jsonl")   # 时间序列: 一日一行
FOLDER_NAME = "A股复盘"
PASS_LINE = 40.0  # 静水增速线(E1预警用)


# ---------------- watchlist ----------------

def load_watchlist():
    if os.path.exists(WATCHLIST):
        try:
            return json.load(open(WATCHLIST)).get("stocks", [])
        except Exception:
            return []
    return []


def save_watchlist(stocks):
    os.makedirs(OUTROOT, exist_ok=True)
    json.dump({"stocks": stocks}, open(WATCHLIST, "w"), ensure_ascii=False, indent=1)


def parse_entry(tok):
    if ":" in tok:
        c, cost = tok.split(":", 1)
        try:
            return {"code": c.strip(), "cost": float(cost)}
        except ValueError:
            return {"code": tok.strip()}
    return {"code": tok.strip()}


def watchlist_ops(args):
    stocks = load_watchlist()
    changed = False
    if args.clear:
        stocks, changed = [], True
    if args.set:
        stocks = []
        for tok in args.set.split(","):
            tok = tok.strip()
            if tok:
                stocks.append(parse_entry(tok))
        changed = True
    for tok in args.add or []:
        e = parse_entry(tok)
        if not any(s["code"] == e["code"] for s in stocks):
            stocks.append(e)
        changed = True
    for c in args.remove or []:
        stocks = [s for s in stocks if s["code"] != c]
        changed = True
    if changed:
        q = data.stock_quotes_batch([s["code"] for s in stocks])
        for s in stocks:
            s.setdefault("name", (q.get(s["code"]) or {}).get("name") or s["code"])
        save_watchlist(stocks)
    return stocks, changed


# ---------------- 时间序列(昨日快照) ----------------

def load_machine(n=10):
    """读最近 n 日机器快照(旧→新)。损坏行跳过。"""
    out = []
    if not os.path.exists(MACHINE):
        return out
    for line in open(MACHINE):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out[-n:]


def prev_day(days_list):
    return days_list[-2] if len(days_list) >= 2 else None


def flow_streak(hist, name, sign):
    """板块资金流连续性: 从最近一日往前数 name 连续 sign(净入/净出) 的天数。"""
    n = 0
    for day in reversed(hist):
        names = set()
        for r in (day.get("flows") or {}).get("inflow" if sign > 0 else "outflow") or []:
            names.add(r.get("name"))
        if name in names:
            n += 1
        else:
            break
    return n


def parse_yesterday_watch(prev_dir):
    """解析昨日 review.md 的「明日观察」行(自由文本, 供回检与素材包)。"""
    md = os.path.join(prev_dir, "review.md")
    if not os.path.exists(md):
        return None
    for line in open(md):
        if "明日观察" in line and "⟦" not in line:
            return line.split("明日观察：", 1)[-1].strip()
    return None


# ---------------- 数据与渲染 ----------------

def cn_date(iso):
    y, m, d = iso.split("-")
    return "%d年%d月%d日" % (int(y), int(m), int(d))


def fmt(x, f="%.2f", na="—"):
    return (f % x) if isinstance(x, (int, float)) else na


def build(args):
    degraded = []
    hist = load_machine(10)

    # 交易日 = 上证K线最后一根(三级降级; 全失败用自然日)
    sh_rows = data.index_closes("sh000001", 300)
    trade_date = sh_rows[-1]["date"] if sh_rows else datetime.date.today().isoformat()
    prev = next((d for d in reversed(hist) if d.get("date") != trade_date), None)
    outdir = os.path.join(OUTROOT, trade_date)
    os.makedirs(outdir, exist_ok=True)
    import glob as _glob
    for _old in _glob.glob(os.path.join(outdir, "chart*.png")):
        os.remove(_old)
    lines, imgs = [], []

    # ===== 模块一 市场总览 =====
    try:
        ov = data.index_overview()
    except Exception:
        ov = [{"name": n, "close": None, "chg": None, "amount_yi": None} for _, n in data.INDICES]
        degraded.append("指数总览")
    br = data.breadth_today()
    if not br:
        degraded.append("涨跌家数")
    msh = data.ma_status(sh_rows) if len(sh_rows) >= 20 else None
    if not msh:
        degraded.append("M判定")
    mcyb = data.ma_status(data.index_closes("sz399006", 300))

    amt_series = data.market_total_amount(10)
    total_amt = amt_series[-1]["amount_yi"] if amt_series else None
    amt_fallback = None
    if total_amt is None and ov:
        # 快照后备: 腾讯实时(上证+深成指, 口径略窄于深综, 好过没有)
        d = {x["name"]: x.get("amount_yi") for x in ov}
        if d.get("上证指数") and d.get("深证成指"):
            amt_fallback = round(d["上证指数"] + d["深证成指"], 0)
            total_amt = amt_fallback
    amt5 = (sum(x["amount_yi"] for x in amt_series[-6:-1]) / 5) if len(amt_series) >= 6 else None
    prev_amt = amt_series[-2]["amount_yi"] if len(amt_series) >= 2 else None
    amt_delta = round(total_amt - prev_amt) if (total_amt and prev_amt) else None
    amt_state = ("放量" if (total_amt and amt5 and total_amt > amt5 * 1.05)
                 else "缩量" if (total_amt and amt5 and total_amt < amt5 * 0.95) else "常量")
    if amt_series:
        amt_series[-1]["amount_state"] = amt_state

    lines += ["# %s A股复盘" % cn_date(trade_date), ""]
    lines += ["## 一、市场总览", "",
              "| 指数 | 收盘 | 涨跌幅 |",
              "|---|---:|---:|"]
    for x in ov:
        lines.append("| %s | %s | %s |" % (
            x["name"], fmt(x["close"]), ("%+.2f%%" % x["chg"]) if x["chg"] is not None else "—"))
    br_txt = ("涨%d家 / 跌%d家 / 平%d家" % (br["up"], br["down"], br["flat"])) if br else "未获取"
    m_txt = "M短线: %s(上证MA5 %s vs MA20 %s, 创业板MA5 %s vs MA20 %s)" % (
        "多头" if (msh or {}).get("trend") == "多头" else "空头",
        (msh or {}).get("ma5"), (msh or {}).get("ma20"),
        (mcyb or {}).get("ma5"), (mcyb or {}).get("ma20")) if msh else "M判定未获取"
    if (msh or {}).get("ma250"):
        m_txt += " | 年线β: %s(收盘%s vs MA250 %s)" % (
            "on" if msh.get("beta") == "on" else "off", fmt(sh_rows[-1]["close"], "%.0f"), msh["ma250"])
    amt_txt = ("**两市成交 %s 亿**" % "{:,.0f}".format(total_amt)) if total_amt else "两市成交未获取"
    if amt_fallback:
        amt_txt += "(快照口径:上证+深成指)"
    if amt_delta is not None:
        amt_txt += "（较昨日%+d亿）" % amt_delta
    if amt5:
        amt_txt += "，%s(5日均量{:,.0f}亿)".format(round(amt5)) % amt_state
    lines += ["", amt_txt + " | " + br_txt + " | " + m_txt, ""]
    lines += ["〖图1〗近10日两市成交额(沪+深综)与上证走势", ""]
    if len(amt_series) >= 5 and sh_rows:
        n = min(len(amt_series), 10)
        dates = [x["date"][5:] for x in amt_series[-n:]]
        close_map = {r["date"]: r["close"] for r in sh_rows}
        closes = [close_map.get(x["date"]) for x in amt_series[-n:]]
        try:
            p = charts.chart_volume(dates, [x["amount_yi"] for x in amt_series[-n:]], closes,
                                    os.path.join(outdir, "chart1_volume.png"))
            imgs.append((p, "〖图1〗"))
        except Exception:
            lines.append("(图1生成失败)")
            degraded.append("图1")
    else:
        lines.append("(图1数据不足)")
        degraded.append("图1数据")

    # ===== 模块二 大涨标的 =====
    lines += ["## 二、大涨标的雷达（近20日）", ""]
    gainers = data.top_gainers_20d(15)
    if not gainers:
        degraded.append("涨幅榜")
    jx, jx_err = data.jingxuan_members()
    if jx_err:
        degraded.append("精选表联动(%s)" % jx_err)
    final_pool = _load_final_pool()
    if gainers:
        lines += ["| 标的 | 近20日涨幅 | 板块 | 总市值 | 精选表/终选池 |",
                  "|---|---:|---|---:|---|"]
        for g in gainers:
            tags = []
            if g["code"] in jx:
                tags.append("★精选%s分" % (jx[g["code"]].get("score") or "?"))
            if final_pool and g["name"] in final_pool:
                tags.append("◎终选池")
            lines.append("| %s(%s) | +%d%% | %s | %s亿 | %s |" % (
                g["name"], g["code"], g["pct"], g["industry"],
                "{:,}".format(g["mcap_yi"]) if g["mcap_yi"] else "—", " ".join(tags)))
    else:
        lines.append("(近20日涨幅榜未获取)")
    lb = data.lianban_ladder()
    if lb:
        lines += ["", "**连板梯队**（≥2连板 %d 只）：" % len(lb)]
        lines.append("、".join("%s(%d板)" % (x["name"], x["days"]) for x in lb[:14]))
    lines += ["", "〖图2〗近20日涨幅榜 Top15（★=L2精选表 ◎=十倍股终选池）", ""]
    try:
        p = charts.chart_gainers(gainers, os.path.join(outdir, "chart2_gainers.png"))
        imgs.append((p, "〖图2〗"))
    except Exception:
        lines.append("(图2生成失败)")

    # ===== 模块三 当前热点(带连续性) =====
    fl = data.sector_flows(8, 6)
    if not fl["inflow"] and not fl["outflow"]:
        degraded.append("板块资金流")
    def flow_txt(rows, sign):
        out = []
        for r in rows[:8]:
            streak = flow_streak(hist + [{"flows": fl}], r["name"], sign) if hist else 1
            tag = "·连续%d日" % streak if streak >= 2 else ""
            out.append("%s(%+.1f亿, %+.1f%%%s)" % (r["name"], r["amount"], r.get("pct") or 0, tag))
        return "、".join(out)
    lines += ["## 三、当前热点", "",
              "**主力净流入**：" + (flow_txt(fl["inflow"], 1) or "未获取"), "",
              "**主力净流出**：" + (flow_txt(fl["outflow"], -1) or "未获取"), ""]
    lines += ["〖图3〗行业主力资金净额（红入绿出）", ""]
    try:
        p = charts.chart_flow(fl["inflow"], fl["outflow"], os.path.join(outdir, "chart3_flow.png"))
        if p:
            imgs.append((p, "〖图3〗"))
    except Exception:
        lines.append("(图3生成失败)")
    lines += ["⟦CLAUDE: 今日主线一句话——先读同目录 notes_for_claude.md 素材包(昨日vs今日资金/连板/量能对比), 再结合上方数据写1-2句：主线是延续/切换/新起⟧", ""]

    # ===== 模块四 关注标的(分组双卡) =====
    wl = load_watchlist()
    lines += ["## 四、关注标的全息卡", ""]
    if not wl:
        lines += ["（未设定关注列表。`--set 代码:成本` 为持仓组, `--add 代码` 为观察组。）", ""]
    batch = data.stock_quotes_batch([s["code"] for s in wl]) if wl else {}
    if wl and not batch:
        degraded.append("批量行情(ulist)")
    fund = data.fundamentals_threeline([s["code"] for s in wl]) if wl else {}
    if wl and not fund:
        degraded.append("财报三线(问财)")
    holdings = [s for s in wl if s.get("cost")]
    watchings = [s for s in wl if not s.get("cost")]
    if holdings:
        lines += ["### 持仓组（track 退出信号）", ""]
    elif watchings:
        lines += ["### 观察组（watch 确认信号）", ""]
    wl_snap = {}
    for s in holdings + watchings:
        code = s["code"].split(".")[0]
        q = batch.get(code) or {}
        if not q:
            try:
                q = data.stock_quote(code)
            except Exception:
                lines += ["### %s（行情获取失败）" % code, ""]
                degraded.append("行情:%s" % code)
                continue
        k = data.stock_kline(code, 250)
        closes = [r["close"] for r in k]
        vols = [r.get("volume") for r in k]
        hi52 = max(closes) if len(closes) >= 60 else None
        trig = max(closes[-20:]) if len(closes) >= 20 else None
        off52 = round((q["price"] / hi52 - 1) * 100, 1) if (q.get("price") and hi52) else None
        cost = s.get("cost")
        group_tag = "·持仓" if cost else "·观察"
        if cost:
            pnl = round((q["price"] / cost - 1) * 100, 1) if (cost and q.get("price")) else None
            tail = ("｜距成本 %+.1f%%" % pnl) if pnl is not None else ""
        else:
            conv = round((trig / q["price"] - 1) * 100, 1) if (trig and q.get("price")) else None
            tail = ("｜触发位还需 +%s%%" % conv) if conv is not None else ""
        lines += ["### %s（%s）%s" % (q.get("name") or s.get("name") or code, code, group_tag), "",
                  "现价 **%s**（%s%%）｜换手 %s%%｜市值 %s亿｜PE %s｜主力净流入 **%s万**" % (
                      fmt(q.get("price")), fmt(q.get("chg"), "%+.2f"), fmt(q.get("turnover_pct")),
                      "{:,}".format(q["mktcap_yi"]) if q.get("mktcap_yi") else "—",
                      fmt(q.get("pe_ttm"), "%.1f"),
                      ("{:+,.0f}".format(q["main_inflow_wan"]).replace(",", "")) if q.get("main_inflow_wan") is not None else "—"),
                  "距一年新高 **%s%%**%s｜右侧触发位(20日高) %.2f%s" % (
                      off52 if off52 is not None else "—", tail, trig or 0,
                      " ←已进入触发区" if (trig and q.get("price") and q["price"] >= trig * 0.995) else "")]
        # 财报三线(E1预警)
        f3 = fund.get(code)
        if f3 and any(v is not None for v in f3.values()):
            vals = (f3.get("rev_yoy"), f3.get("np_yoy"), f3.get("ded_yoy"))
            flags = [("✓" if (v is not None and v >= PASS_LINE) else "✗") for v in vals]
            e1 = " ⚠E1增速预警" if all(v is not None and v < PASS_LINE for v in vals) else ""
            lines.append("财报三线(最新报告期): 营收%s%% 归母%s%% 扣非%s%% %s%s" % (
                fmt(vals[0], "%.0f"), fmt(vals[1], "%.0f"), fmt(vals[2], "%.0f"),
                "/".join(flags), e1))
        # 事件区: 解禁 + 披露
        unl = data.unlock_calendar(code, 90)
        if unl:
            u = unl[0]
            lines.append("⏰解禁: %s %s万股(约%s亿, 占总股本%s%%)%s" % (
                u["date"], "{:,.0f}".format(u["shares_wan"]), u["mcap_yi"], u["ratio_pct"],
                " ⚠占流通盘比例高" if u["ratio_pct"] >= 5 else ""))
        nrd = data.next_report_date(code)
        if nrd:
            days_to = (datetime.date.fromisoformat(nrd["date"]) - datetime.date.today()).days
            lines.append("📅下期披露: %s(%s) 预约 %s, 还有 %d 天" % (
                nrd["year"], {"1": "一季报", "2": "中报", "3": "三季报", "4": "年报"}.get(nrd["type"], "报告"),
                nrd["date"], days_to))
        if code in jx:
            j = jx[code]
            lines.append("精选表: %s分 · %s · %s" % (j.get("score"), j.get("stage"), j.get("action")))
        anns = data.announcements_recent(code, days=7, limit=5)
        if anns:
            lines += ["近7日公告：" + "；".join("%s(%s)" % (a["title"][:28], a["date"]) for a in anns)]
        news = data.stock_news(q.get("name") or "", 3)
        if news:
            lines += ["相关新闻：" + "；".join("%s" % n["title"][:30] for n in news[:3])]
        lines.append("")
        wl_snap[code] = {"price": q.get("price"), "main_inflow_wan": q.get("main_inflow_wan"), "trig": trig}
        mf60 = []
        # 持仓组专属: 近N日主力资金流序列 + 吸筹/出逃形态标注(2026-11-03新增)
        if cost:
            try:
                mf = data.stock_moneyflow_daily(code, 10)
                if len(mf) >= 3:
                    close_by_date = {r0["date"]: r0["close"] for r0 in k}
                    rows_txt = []
                    absorb = panic = 0   # 吸筹=跌日主力买; 出逃=涨日主力卖
                    streak = 0
                    for i, m in enumerate(mf):
                        cl = close_by_date.get(m["date"])
                        chg = None
                        if cl and i > 0:
                            prev_close = close_by_date.get(mf[i - 1]["date"])
                            if prev_close:
                                chg = (cl / prev_close - 1) * 100
                        if chg is not None:
                            if chg < 0 and m["main_yi"] > 0:
                                absorb += 1
                            elif chg > 0 and m["main_yi"] < 0:
                                panic += 1
                        rows_txt.append("%s %+0.2f亿" % (m["date"][5:], m["main_yi"]))
                    for m in reversed(mf):
                        if m["main_yi"] > 0:
                            streak += 1
                        else:
                            break
                    out_streak = 0
                    for m in reversed(mf):
                        if m["main_yi"] < 0:
                            out_streak += 1
                        else:
                            break
                    if streak >= 2:
                        verdict = "连续%d日净流入" % streak
                    elif out_streak >= 2:
                        verdict = "连续%d日净流出⚠" % out_streak
                    else:
                        verdict = ""
                    pattern = []
                    if absorb >= 3:
                        pattern.append("跌日吸筹×%d(主力接散户筹码)" % absorb)
                    if panic >= 3:
                        pattern.append("涨日出逃×%d⚠" % panic)
                    lines.append("主力资金(近%d日): %s%s%s" % (
                        len(mf), " → ".join(rows_txt[-6:]),
                        ("｜" + verdict) if verdict else "",
                        ("｜" + "，".join(pattern)) if pattern else ""))
                    mf60 = data.stock_moneyflow_daily(code, 60)
            except Exception:
                mf60 = []
                pass
        anchor = "〖图%d〗%s 近60日量价+主力资金" % (len(imgs) + 1, q.get("name") or code)
        lines += [anchor, ""]
        try:
            n60 = min(60, len(k))
            p = charts.chart_stock(q.get("name") or code,
                                   [r["date"] for r in k[-n60:]], closes[-n60:], vols[-n60:],
                                   cost=cost, high52=hi52, trigger=trig,
                                   out=os.path.join(outdir, "chart%d_%s.png" % (len(imgs) + 1, code)),
                                   main_flows=mf60)
            if p:
                imgs.append((p, anchor[:4]))
        except Exception:
            pass

    # ===== 模块五 三行小结 + 昨日观察回检 =====
    trend = (msh or {}).get("trend", "—")
    top_in = fl["inflow"][0]["name"] if fl["inflow"] else "—"
    top_out = fl["outflow"][0]["name"] if fl["outflow"] else "—"
    lines += ["## 五、三行小结", "",
              "- 定性：⟦CLAUDE: 依据指数分化与涨跌家数(及素材包中昨日对比), 一句话定性今日市场⟧",
              "- 最重要信号：主力主攻 **%s**、出逃 **%s**；M短线=%s%s" % (
                  top_in, top_out, trend,
                  " 年线β=%s" % msh.get("beta") if msh and msh.get("beta") else ""),
              "- 明日观察：⟦CLAUDE: 写1-3个具体观察点(价位/事件/数据), 必须可机检(明日自动回检), 如「上证站稳XXXX」「XX突破YY.ZZ」⟧", ""]

    # 昨日观察回检(机检部分)
    checks = _recheck_yesterday(prev, batch, sh_rows, msh)
    if prev or checks:
        lines += ["## 六、昨日观察回检", ""]
        for c in checks:
            lines.append("- %s：**%s**（昨日说「%s」）" % (c["desc"], c["result"], c["quote"]))
        yw = None
        if prev:
            for dd in sorted({d.get("date") for d in hist
                              if d.get("date") and d.get("date") != trade_date}, reverse=True):
                yw = parse_yesterday_watch(os.path.join(OUTROOT, dd))
                if yw:
                    break
        if yw:
            lines.append("- ⟦CLAUDE: 对照昨日观察原文「%s」, 补充机检覆盖不到的判断: 哪条兑现/哪条落空, 一句话⟧" % yw[:60])
        lines.append("")

    if degraded:
        lines += ["---", "**本次数据降级项**：%s（不影响主流程, 次日重跑可恢复）" % "；".join(degraded), ""]

    md = "\n".join(lines)
    md_path = os.path.join(outdir, "review.md")
    open(md_path, "w").write(md)

    # machine.jsonl append(时间序列, 一日一行; 在 .gitignore 保护下)
    snap = {"date": trade_date,
            "indices": ov, "breadth": br,
            "total_amount_yi": total_amt, "amount_state": amt_state,
            "ma": {k: msh.get(k) for k in ("ma5", "ma20", "ma250", "trend", "beta")} if msh else None,
            "flows": {"inflow": fl["inflow"], "outflow": fl["outflow"]},
            "lianban": [{"name": x["name"], "days": x["days"]} for x in lb],
            "gainers_top5": [{"name": g["name"], "pct": g["pct"]} for g in gainers[:5]],
            "watchlist": wl_snap,
            "degraded": degraded}
    # 幂等: 同日重跑先剔除旧今日行(时间序列一日一行)
    keep = []
    if os.path.exists(MACHINE):
        for line in open(MACHINE):
            try:
                if json.loads(line.strip()).get("date") != trade_date:
                    keep.append(line.rstrip("\n"))
            except Exception:
                keep.append(line.rstrip("\n"))
    with open(MACHINE, "w") as f:
        for line in keep:
            f.write(line + "\n")
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    # 判断素材包(Claude 润色前必读, 不进正稿)
    _write_notes(outdir, hist, fl, lb, prev, amt_series, checks)

    json.dump({"trade_date": trade_date, "imgs": [(p, a) for p, a in imgs], "degraded": degraded},
              open(os.path.join(outdir, "manifest.json"), "w"), ensure_ascii=False, indent=1)
    return md_path, trade_date


def _load_final_pool():
    """十倍股终选池(可选联动): finance-analysis 最新筛选目录下的 l3_final_ranked.json。"""
    import glob
    p = sorted(glob.glob(os.path.expanduser(
        "~/Documents/work/finance-analysis/0 分析结果/*/l3_final_ranked.json")))
    if not p:
        return None
    try:
        return {r["name"] for r in json.load(open(p[-1]))}
    except Exception:
        return None


def _recheck_yesterday(prev, batch, sh_rows, msh_today):
    """机检昨日观察: 触发位突破 / MA5 站守。返回 [{desc, result, quote}]。"""
    out = []
    if not prev:
        return out
    ymd = prev.get("date")
    prev_wl = prev.get("watchlist") or {}
    close_today = sh_rows[-1]["close"] if sh_rows else None
    for code, w in prev_wl.items():
        trig = w.get("trig")
        price = (batch.get(code) or {}).get("price")
        if trig and price:
            hit = price >= trig * 0.995
            out.append({"desc": "%s 触发位%.2f" % (code, trig),
                        "result": "已突破✅(现价%.2f)" % price if hit else "未突破(现价%.2f)" % price,
                        "quote": "20日高%.2f" % trig})
    ma5_y = ((prev.get("ma") or {}).get("ma5"))
    if ma5_y and close_today:
        out.append({"desc": "上证MA5(%s)" % ma5_y,
                    "result": "站稳✅(收盘%.2f)" % close_today if close_today >= ma5_y else "失守(收盘%.2f)" % close_today,
                    "quote": "MA5=%s" % ma5_y})
    return out


def _write_notes(outdir, hist, fl, lb_today, prev, amt_series, checks):
    """判断素材包: 昨日vs今日的资金/连板/量能对比 + 回检结果。Claude 润色前必读。"""
    if not prev:
        return
    L = ["# 素材包(Claude润色用, 不进正稿) — %s vs 今日" % prev.get("date"), ""]
    pin = {r["name"]: r for r in (prev.get("flows") or {}).get("inflow") or []}
    tin = {r["name"]: r for r in fl["inflow"]}
    new_in = [n for n in tin if n not in pin]
    gone_in = [n for n in pin if n not in tin]
    if new_in:
        L.append("- 新进流入榜(切换信号): " + "、".join(new_in))
    if gone_in:
        L.append("- 退出流入榜(退潮信号): " + "、".join(gone_in))
    ylb = {x.get("name"): x.get("days") for x in prev.get("lianban") or []}
    if ylb:
        promo = []
        for x in lb_today:
            d = ylb.get(x["name"])
            if d and x["days"] > d:
                promo.append("%s(%s→%d板)" % (x["name"], d, x["days"]))
        tnames = set(x["name"] for x in lb_today)
        broke = ["%s(昨日%d板,今日出局)" % (n, d) for n, d in ylb.items() if n not in tnames]
        if promo:
            L.append("- 连板晋级: " + "、".join(promo))
        if broke:
            L.append("- 连板断板: " + "、".join(broke[:6]))
    yamt = prev.get("total_amount_yi")
    if yamt and amt_series:
        L.append("- 量能: 昨日%s亿 → 今日%s亿(%s)" % (
            "{:,.0f}".format(yamt), "{:,.0f}".format(amt_series[-1]["amount_yi"]),
            amt_series[-1].get("amount_state", "")))
    if checks:
        L.append("- 昨日观察机检: " + "；".join("%s=%s" % (c["desc"], c["result"]) for c in checks))
    open(os.path.join(outdir, "notes_for_claude.md"), "w").write("\n".join(L) + "\n")


# ---------------- 飞书发布 ----------------

def _esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(t):
    """md行内: **bold** → <b>。"""
    out, bold = [], False
    for part in _esc(t).split("**"):
        out.append(("<b>" if not bold else "</b>") + part if part or not bold else part)
        bold = not bold
    s = "".join(out)
    return s.replace("</b><b>", "").replace("<b></b>", "")


def md_to_xml_segments(md, img_by_anchor):
    """把 review.md 切成 [xml段, ('img', png), xml段, ...]。"""
    segs, buf, lines = [], [], md.split("\n")
    i = 0

    def flush():
        if buf:
            segs.append(("xml", "\n".join(buf)))
            del buf[:]

    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if not s:
            i += 1
            continue
        if s.startswith("〖图") and "〗" in s:
            anchor = s[:s.index("〗") + 1]
            png = img_by_anchor.get(anchor)
            flush()
            if png:
                segs.append(("img", png))
                cap = s[len(anchor):].strip()
                if cap:
                    segs.append(("xml", '<p align="center"><span text-color="gray">%s</span></p>' % _esc(cap)))
            else:
                buf.append("<p>%s</p>" % _esc(s))
            i += 1
            continue
        if s.startswith("### "):
            flush()
            segs.append(("xml", "<h3>%s</h3>" % _inline(s[4:])))
        elif s.startswith("## "):
            flush()
            segs.append(("xml", "<h2>%s</h2>" % _inline(s[3:])))
        elif s.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            head, body = rows[0], rows[1:]
            t = ['<table><thead><tr>']
            t += ['<th background-color="light-gray"><p>%s</p></th>' % _inline(c) for c in head]
            t.append("</tr></thead><tbody>")
            for r in body:
                t.append("<tr>" + "".join("<td><p>%s</p></td>" % _inline(c) for c in r) + "</tr>")
            t.append("</tbody></table>")
            segs.append(("xml", "".join(t)))
            continue
        elif s.startswith("- "):
            flush()
            items = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(lines[i].strip()[2:])
                i += 1
            segs.append(("xml", "<ul>" + "".join("<li>%s</li>" % _inline(x) for x in items) + "</ul>"))
            continue
        else:
            buf.append("<p>%s</p>" % _inline(s))
        i += 1
    flush()
    return segs


def sh(cmd, cwd="/tmp", timeout=120):
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                          timeout=timeout).stdout.decode("utf-8", "ignore")


def folder_token():
    if os.path.exists(FEISHU_CACHE):
        try:
            tok = json.load(open(FEISHU_CACHE)).get("token")
            if tok:
                return tok
        except Exception:
            pass
    out = sh('lark-cli drive +create-folder --name "%s" --as user' % FOLDER_NAME)
    m = re.search(r'"(?:token|folder_token|url)":\s*"?([A-Za-z0-9]+)', out)
    if not m:
        raise RuntimeError("创建飞书文件夹失败: " + out[:200])
    tok = m.group(1)
    json.dump({"token": tok}, open(FEISHU_CACHE, "w"))
    return tok


def publish(md_path, trade_date):
    tok = folder_token()
    outdir = os.path.dirname(os.path.abspath(md_path))
    title = "%sA股复盘" % cn_date(trade_date)
    man = json.load(open(os.path.join(outdir, "manifest.json")))
    img_by_anchor = dict((a, os.path.basename(p)) for p, a in man.get("imgs", []))
    open(os.path.join(outdir, "stub.md"), "w").write("　")
    out = sh('lark-cli drive +import --file stub.md --type docx --name "%s" '
             '--folder-token %s --as user' % (title, tok), cwd=outdir)
    m = re.search(r'"token":\s*"([A-Za-z0-9]+)"', out)
    if not m:
        raise RuntimeError("创建文档失败: " + out[:300])
    doc = m.group(1)
    url = "https://my.feishu.cn/docx/" + doc
    md = open(md_path).read()
    segs = md_to_xml_segments(md, img_by_anchor)
    ok_img = total_img = 0
    for kind, val in segs:
        if kind == "xml":
            open(os.path.join(outdir, "seg.xml"), "w").write(val)
            sh('lark-cli docs +update --doc %s --command append --content "$(cat seg.xml)" --as user'
               % doc, cwd=outdir, timeout=120)
        else:
            total_img += 1
            r = sh("lark-cli docs +update --doc %s --command append "
                   "--content '<img path=\"@./%s\" width=\"820\"/>' --as user" % (doc, val),
                   cwd=outdir, timeout=180)
            ok_img += 1 if '"result": "success"' in r or '"ok": true' in r else 0
    return doc, url, ok_img, total_img


def main():
    ap = argparse.ArgumentParser(description="A股复盘 v2(手动运行)")
    ap.add_argument("--set", help="设定关注列表(带成本=持仓): 603268:194.67,300750")
    ap.add_argument("--add", action="append", help="追加观察标的(可多次)")
    ap.add_argument("--remove", action="append", help="移除标的")
    ap.add_argument("--clear", action="store_true", help="清空关注列表")
    ap.add_argument("--list", action="store_true", help="仅列出关注列表")
    ap.add_argument("--no-publish", action="store_true", help="只生成md+图表, 不发飞书")
    ap.add_argument("--publish", help="发布指定日期(YYYY-MM-DD)的已生成报告")
    args = ap.parse_args()

    if args.list:
        print(json.dumps(load_watchlist(), ensure_ascii=False, indent=1))
        return 0
    stocks, changed = watchlist_ops(args)
    if changed:
        print("关注列表已更新:", json.dumps(stocks, ensure_ascii=False))
    if args.set or args.clear or args.add or args.remove:
        return 0
    if args.publish:
        md = os.path.join(OUTROOT, args.publish, "review.md")
        doc, url, ok, n = publish(md, args.publish)
        print("已发布:", url, "(插图 %d/%d)" % (ok, n))
        return 0
    md_path, trade_date = build(args)
    print("报告草稿:", md_path, "| 交易日:", trade_date)
    if args.no_publish:
        print("(未发布。润色后可用 --publish %s 发布)" % trade_date)
        return 0
    doc, url, ok, n = publish(md_path, trade_date)
    print("已发布:", url, "(插图 %d/%d)" % (ok, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
