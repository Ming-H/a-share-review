#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""review.py — a-share-review 编排入口(手动运行, 本机)。

用法:
  python3 review.py                          # 全自动: 取数→图表→markdown→飞书发布
  python3 review.py --no-publish             # 只产出 outputs/{date}/review.md + 图表(Claude可润色后再发)
  python3 review.py --publish                # 发布已存在的 outputs/{date}/review.md
  python3 review.py --set 603268,603259      # 设定关注列表(可带成本: 603268:194.67)
  python3 review.py --add 300750 --remove 603268 --list --clear

隐私: watchlist.json 在 .gitignore 中, 永不进仓库。
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
FOLDER_NAME = "A股复盘"
L2_BASE, L2_TABLE = "J8RGbNZobaXu9VsqtFWcKUemnQe", "tblHhkqqSWcpGD7Y"


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
        for s in stocks:
            if not s.get("name"):
                try:
                    s["name"] = data.stock_quote(s["code"])["name"]
                except Exception:
                    s["name"] = s["code"]
        save_watchlist(stocks)
    return stocks, changed


# ---------------- 数据与渲染 ----------------

def cn_date(iso):
    y, m, d = iso.split("-")
    return "%d年%d月%d日" % (int(y), int(m), int(d))


def build(args):
    # 交易日 = 上证K线最后一根(失败降级为自然日)
    try:
        sh = data.idx_kline("1.000001", 70)
    except Exception:
        sh = []
    trade_date = sh[-1]["date"] if sh else datetime.date.today().isoformat()
    outdir = os.path.join(OUTROOT, trade_date)
    os.makedirs(outdir, exist_ok=True)
    lines, imgs = [], []   # imgs: [(png, anchor)]

    # ===== 模块一 市场总览 =====
    try:
        ov = data.index_overview()
    except Exception:
        ov = [{"name": n, "close": None, "chg": None, "amount_yi": None} for _, n in data.INDICES]
    br = data.breadth_today()
    try:
        sz = data.idx_kline("0.399001", 70)
    except Exception:
        sz = []
    msh = None
    if len(sh) >= 20:
        msh = data.ma_status(sh)
    else:
        try:
            msh = data.ma_status(data.idx_closes_tx("sh000001", 70))
        except Exception:
            msh = None
    try:
        mcyb = data.ma_status(data.idx_kline("0.399006", 70))
    except Exception:
        try:
            mcyb = data.ma_status(data.idx_closes_tx("sz399006", 70))
        except Exception:
            mcyb = None
    total_amt = round(sum(x["amount_yi"] or 0 for x in ov), 0)
    prev_amt = round((sh[-2]["amount_yi"] + sz[-2]["amount_yi"]), 1) \
        if len(sh) > 1 and len(sz) > 1 else None
    amt_delta = round(total_amt - prev_amt) if prev_amt else None

    lines += ["# %s A股复盘" % cn_date(trade_date), ""]
    lines += ["## 一、市场总览", "",
              "| 指数 | 收盘 | 涨跌幅 |",
              "|---|---:|---:|"]
    for x in ov:
        lines.append("| %s | %s | %s |" % (
            x["name"], ("%.2f" % x["close"]) if x["close"] else "—",
            ("%+.2f%%" % x["chg"]) if x["chg"] is not None else "—"))
    br_txt = ("涨%d家 / 跌%d家 / 平%d家" % (br["up"], br["down"], br["flat"])) if br else "涨跌家数未获取"
    m_txt = "M判定: %s(上证MA5 %s vs MA20 %s, 创业板MA5 %s vs MA20 %s)" % (
        "开" if (msh or {}).get("trend") == "多头" else "关",
        (msh or {}).get("ma5"), (msh or {}).get("ma20"),
        (mcyb or {}).get("ma5"), (mcyb or {}).get("ma20")) if msh else "M判定未获取"
    lines += ["", "**两市成交 %s 亿**%s | %s | %s" % (
        "{:,.0f}".format(total_amt),
        ("（较昨日%+,.0f亿）" % amt_delta) if amt_delta else "", br_txt, m_txt), ""]
    lines += ["〖图1〗近10日两市成交额与上证走势", ""]

    # 图1
    try:
        n = 10
        if len(sh) >= n and len(sz) >= n:
            dates = [r["date"] for r in sh[-n:]]
            amt = [(a["amount_yi"] + b["amount_yi"]) for a, b in zip(sh[-n:], sz[-n:])]
            closes = [r["close"] for r in sh[-n:]]
            p = charts.chart_volume(dates, amt, closes, os.path.join(outdir, "chart1_volume.png"))
            imgs.append((p, "〖图1〗"))
        else:
            lines.append("(图1数据不足)")
    except Exception:
        lines.append("(图1生成失败)")

    # ===== 模块二 大涨标的 =====
    lines += ["## 二、大涨标的雷达（近20日）", ""]
    gainers = data.top_gainers_20d(15)
    jx = data.jingxuan_members()
    for g in gainers:
        g["starred"] = g["code"] in jx
    if gainers:
        lines += ["| 标的 | 近20日涨幅 | 板块 | 总市值 | 精选表 |",
                  "|---|---:|---|---:|---|"]
        for g in gainers:
            lines.append("| %s(%s) | +%d%% | %s | %s亿 | %s |" % (
                g["name"], g["code"], g["pct"], g["industry"],
                "{:,}".format(g["mcap_yi"]) if g["mcap_yi"] else "—",
                "★A级" if g["starred"] else ""))
    else:
        lines.append("(近20日涨幅榜未获取)")
    lb = data.lianban_ladder()
    if lb:
        lines += ["", "**连板梯队**（≥2连板 %d 只）：" % len(lb)]
        lines.append("、".join("%s(%d板)" % (x["name"], x["days"]) for x in lb[:14]))
    lines.append("")
    lines += ["〖图2〗近20日涨幅榜 Top15（★=L2精选表标的）", ""]
    try:
        p = charts.chart_gainers(gainers, os.path.join(outdir, "chart2_gainers.png"))
        imgs.append((p, "〖图2〗"))
    except Exception:
        lines.append("(图2生成失败)")

    # ===== 模块三 当前热点 =====
    fl = data.sector_flows(8, 6)
    lines += ["## 三、当前热点", "",
              "**主力净流入**：" + ("、".join("%s(+%.1f亿, %+.1f%%)" % (r["name"], r["amount"], r.get("pct"))
                                        for r in fl["inflow"][:8]) or "未获取"), "",
              "**主力净流出**：" + ("、".join("%s(%.1f亿, %+.1f%%)" % (r["name"], r["amount"], r.get("pct"))
                                         for r in fl["outflow"][:6]) or "未获取"), ""]
    lines += ["〖图3〗行业主力资金净额（红入绿出）", ""]
    try:
        p = charts.chart_flow(fl["inflow"], fl["outflow"], os.path.join(outdir, "chart3_flow.png"))
        if p:
            imgs.append((p, "〖图3〗"))
    except Exception:
        lines.append("(图3生成失败)")
    lines += ["⟦CLAUDE: 今日主线一句话——结合上方流入/流出/涨跌幅交叉判断，写1-2句：主线是延续/切换/新起⟧", ""]

    # ===== 模块四 关注标的 =====
    wl = load_watchlist()
    lines += ["## 四、关注标的全息卡", ""]
    if not wl:
        lines += ["（未设定关注列表。用 `python3 review.py --set 代码1,代码2` 或让 Claude 帮你设定。）", ""]
    for i, s in enumerate(wl, 1):
        code = s["code"]
        try:
            q = data.stock_quote(code)
        except Exception:
            lines += ["### %s（行情获取失败）" % code, ""]
            continue
        k = data.stock_kline_tx(code, 250)
        closes = [r["close"] for r in k]
        hi52 = max(closes) if closes else None
        trig = max(closes[-20:]) if len(closes) >= 20 else None
        off52 = round((q["price"] / hi52 - 1) * 100, 1) if (q["price"] and hi52) else None
        cost = s.get("cost")
        pnl = round((q["price"] / cost - 1) * 100, 1) if (cost and q["price"]) else None
        lines += ["### %s（%s）" % (q["name"], code), "",
                  "现价 **%.2f**（%+.2f%%）｜换手 %s%%｜市值 %s亿｜PE-TTM %s｜主力净流入 **%s万**" % (
                      q["price"], q["chg"],
                      ("%.2f" % q["turnover_pct"]) if q["turnover_pct"] else "—",
                      "{:,}".format(q["mktcap_yi"]) if q["mktcap_yi"] else "—",
                      ("%.1f" % q["pe_ttm"]) if q["pe_ttm"] else "—",
                      "{:+,.0f}".format(q["main_inflow_wan"]) if q["main_inflow_wan"] is not None else "—"),
                  "距一年新高 **%s%%**%s｜右侧触发位(20日高) %.2f%s" % (
                      off52, ("｜距成本 %+.1f%%" % pnl) if pnl is not None else "",
                      trig or 0, " ←已进入触发区" if (trig and q["price"] >= trig * 0.995) else ""),
                  ""]
        anns = data.announcements_recent(code, days=7, limit=5)
        if anns:
            lines += ["近7日公告：" + "；".join("%s(%s)" % (a["title"][:28], a["date"]) for a in anns)]
        news = data.stock_news(q["name"], 3)
        if news:
            lines += ["相关新闻：" + "；".join("%s" % n["title"][:30] for n in news[:3])]
        lines.append("")
        anchor = "〖图%d〗%s 近60日走势" % (3 + i, q["name"])
        lines += [anchor, ""]
        try:
            p = charts.chart_stock(q["name"], [r["date"] for r in k[-60:]], closes[-60:],
                                   cost=cost, high52=hi52, trigger=trig,
                                   out=os.path.join(outdir, "chart%d_%s.png" % (3 + i, code)))
            if p:
                imgs.append((p, anchor[:4]))
        except Exception:
            pass

    # ===== 五、三行小结 =====
    trend = (msh or {}).get("trend", "—")
    top_in = fl["inflow"][0]["name"] if fl["inflow"] else "—"
    top_out = fl["outflow"][0]["name"] if fl["outflow"] else "—"
    lines += ["## 五、三行小结", "",
              "- 定性：⟦CLAUDE: 依据指数分化与涨跌家数，一句话定性今日市场⟧",
              "- 最重要信号：主力主攻 **%s**、出逃 **%s**；M=%s" % (top_in, top_out, trend),
              "- 明日观察：⟦CLAUDE: 一个具体观察点（价位/事件/数据）⟧", ""]

    md = "\n".join(lines)
    md_path = os.path.join(outdir, "review.md")
    open(md_path, "w").write(md)
    json.dump({"trade_date": trade_date, "imgs": [(p, a) for p, a in imgs]},
              open(os.path.join(outdir, "manifest.json"), "w"), ensure_ascii=False, indent=1)
    return md_path, trade_date


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
    """把 review.md 切成 [xml段, ('img', png), xml段, ...]。
    支持: h2/h3、表格、-列表、**粗体**、普通段落；〖图N〗锚点行替换为图片块。"""
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
    # ① 空文档占位(标题在文档名), 图片经 XML <img path> 内联追加
    open(os.path.join(outdir, "stub.md"), "w").write("　")
    out = sh('lark-cli drive +import --file stub.md --type docx --name "%s" '
             '--folder-token %s --as user' % (title, tok), cwd=outdir)
    m = re.search(r'"token":\s*"([A-Za-z0-9]+)"', out)
    if not m:
        raise RuntimeError("创建文档失败: " + out[:300])
    doc = m.group(1)
    url = "https://my.feishu.cn/docx/" + doc
    # ② 分段追加(XML) 与 图片(<img path=@./相对路径>) 交错
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
    ap = argparse.ArgumentParser(description="A股复盘(手动运行)")
    ap.add_argument("--set", help="设定关注列表: 603268,603259:159.39")
    ap.add_argument("--add", action="append", help="追加标的(可多次)")
    ap.add_argument("--remove", action="append", help="移除标的(可多次)")
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
        return 0  # 纯管理操作: 保存即退出, 不触发复盘
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
