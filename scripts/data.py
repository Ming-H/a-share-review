#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data.py — a-share-review 取数层(全部免费源, 零密钥依赖, mac 本机运行)。

依赖:
- 东财 push2/push2his (指数/个股行情、K线、主力净流入) — 免费
- 腾讯 fqkline (个股日K) — 免费
- 问财 cli (近20日涨幅榜/连板天梯, 经 hithink-market-query, 消耗问财额度)
- hithink-moneyflow flow.py (板块主力净流入/流出, 消耗问财额度)
- 巨潮 hisAnnouncement (个股近7日公告) — 免费
- 华尔街见闻 API (个股新闻搜索) — 免费
所有函数失败返回 None/[] 并带 degraded 标记, 不阻塞整体复盘。
"""
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com"}
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WENCAI_CLI = os.path.expanduser("~/.claude/skills/hithink-market-query/scripts/cli.py")
FLOW_CLI = os.path.expanduser("~/.claude/skills/hithink-moneyflow/scripts/flow.py")
ENV = dict(os.environ)
for envf in ("~/.hithink_env", "~/.claude/.hithink_env"):
    p = os.path.expanduser(envf)
    if os.path.exists(p):
        for line in open(p):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                ENV[k] = v
        break


def _get(url, headers=None, timeout=12, retries=3):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or UA)
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore"))
        except Exception as ex:
            last = ex
            time.sleep(min(2 * (i + 1), 8))
    raise last


def _post(url, data, headers=None, timeout=15):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers=headers or {"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore"))


def _secid(code):
    return ("1." if code.startswith(("6", "9")) else "0.") + code


def _sym(code):
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


# ---------------- 模块一: 市场总览 ----------------

INDICES = [("1.000001", "上证指数"), ("0.399001", "深证成指"), ("0.399006", "创业板指"),
           ("1.000688", "科创50"), ("0.899050", "北证50")]


def index_overview():
    """五大指数收盘/涨跌幅/成交额。主链: push2his K线推算; 限流后备: 腾讯批量行情。"""
    tq = None
    out = []
    for secid, name in INDICES:
        row = {"name": name, "close": None, "chg": None, "amount_yi": None}
        try:
            rows = idx_kline(secid, 5)
            last, prev = rows[-1], rows[-2]
            row = {"name": name, "close": last["close"],
                   "chg": round((last["close"] / prev["close"] - 1) * 100, 2),
                   "amount_yi": last["amount_yi"]}
        except Exception:
            if tq is None:
                tq = tencent_index_quotes()
            if name in tq:
                row = {"name": name, **tq[name]}
        out.append(row)
    return out


def breadth_today():
    """全市场涨跌家数(东财涨跌分布: fenbu=[{"-1":358},...] 每项单键值对)。失败 None。"""
    try:
        d = _get("https://push2ex.eastmoney.com/getTopicZDFenBu?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt", retries=1)
        up = down = flat = 0
        for item in (d.get("data") or {}).get("fenbu") or []:
            for k, v in item.items():
                try:
                    ki = int(k)
                except ValueError:
                    continue
                if ki > 0:
                    up += v
                elif ki < 0:
                    down += v
                else:
                    flat += v
        if up + down + flat == 0:
            return None
        return {"up": up, "down": down, "flat": flat}
    except Exception:
        return None


def tencent_index_quotes():
    """腾讯批量指数行情(push2限流后备): {名称: {close, chg, amount_yi}}。
    qt.gtimg.cn 字段: 3=现价 32=涨跌% 37=成交额(万)。"""
    syms = "sh000001,sz399001,sz399006,sh000688,bj899050"
    try:
        req = urllib.request.Request("https://qt.gtimg.cn/q=" + syms,
                                     headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", "ignore")
        out = {}
        for m in re.finditer(r'v_(\w+)="([^"]+)"', raw):
            p = m.group(2).split("~")
            if len(p) < 38:
                continue
            name = p[1]
            out[name] = {"close": float(p[3]), "chg": float(p[32]),
                         "amount_yi": round(float(p[37]) / 1e4, 0)}
        return out
    except Exception:
        return {}


def idx_kline(secid, days=70):
    """东财日K: [{date, close, amount_yi}] (amount=成交额元)。"""
    d = _get("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s&fields1=f1,f2,f3"
             "&fields2=f51,f53,f57&klt=101&fqt=1&end=20500101&lmt=%d" % (secid, days))
    rows = []
    for k in (d.get("data") or {}).get("klines") or []:
        p = k.split(",")
        rows.append({"date": p[0], "close": float(p[1]), "amount_yi": round(float(p[2]) / 1e8, 0)})
    return rows


def idx_closes_tx(sym, days=70):
    """腾讯指数日K(东财限流后备, 仅收盘价; 指数无复权, 数据在 day 键)。"""
    d = _get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq"
             % (sym, days), {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com"})
    node = (d.get("data") or {}).get(sym) or {}
    k = node.get("day") or node.get("qfqday") or []
    return [{"date": x[0], "close": float(x[2])} for x in k]


def ma_status(rows, n1=5, n2=20):
    """收盘序列 → (MA5, MA20, 多头/空头, 近n日累计涨幅%)。"""
    closes = [r["close"] for r in rows]
    if len(closes) < n2:
        return None
    ma1 = sum(closes[-n1:]) / n1
    ma2 = sum(closes[-n2:]) / n2
    chg20 = (closes[-1] / closes[-n2] - 1) * 100 if len(closes) >= n2 else None
    return {"ma5": round(ma1, 1), "ma20": round(ma2, 1),
            "trend": "多头" if ma1 > ma2 else "空头", "chg20": round(chg20, 1)}


# ---------------- 模块二: 大涨标的 ----------------

def _wencai(query, limit=20):
    """问财 cli 封装: 返回 datas(list[dict]) 或 []。cli 输出 {success, datas:[...]}。"""
    if not os.path.exists(WENCAI_CLI):
        return []
    try:
        out = subprocess.check_output(
            ["python3", WENCAI_CLI, "--query", query, "--limit", str(limit)],
            env=ENV, timeout=60, stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            return []
        d = json.loads(m.group(0))
        return d.get("datas") or []
    except Exception:
        return []


def _pick(row, *prefixes):
    """问财字段带日期后缀, 前缀匹配取值。"""
    for p in prefixes:
        for k, v in row.items():
            if k.startswith(p) and v not in (None, ""):
                return v
    return None


def top_gainers_20d(n=15):
    """近20日涨幅榜 Top N: [{name, code, pct, industry, mcap_yi}]。
    问财返回的区间涨幅字段形如 涨跌幅[20260922-20261027] —— 前缀匹配取值。"""
    rows = _wencai("近20日涨幅前%d的股票 所属行业 总市值" % (n * 2), n * 2)
    out = []
    for r in rows:
        name = _pick(r, "股票简称")
        code = str(_pick(r, "股票代码") or "").split(".")[0]
        pct = None
        for k, v in r.items():
            if k.startswith("涨跌幅[") and isinstance(v, (int, float)):
                pct = v
        ind = _pick(r, "所属行业", "所属申万行业", "行业")
        mcap = _pick(r, "总市值")
        if pct is None or not name:
            continue
        out.append({"name": name, "code": code, "pct": round(float(pct), 1),
                    "industry": str(ind) if ind else "—",
                    "mcap_yi": round(float(mcap) / 1e8, 0) if mcap else None})
        if len(out) >= n:
            break
    return out


def lianban_ladder(min_days=2):
    """连板天梯(问财原词"连板天梯", 按天数降序): [{name, code, days}]。"""
    rows = _wencai("连板天梯", 20)
    out = []
    for r in rows:
        name = _pick(r, "股票简称")
        code = str(_pick(r, "股票代码") or "").split(".")[0]
        days = _pick(r, "连续涨停天数", "连板数")
        try:
            days = int(float(days))
        except (TypeError, ValueError):
            continue
        if days >= min_days:
            out.append({"name": name, "code": code, "days": days})
    return out


def jingxuan_members():
    """L2 精选表(Base)成员: {code: name}。失败返回 {}(不影响主流程)。"""
    try:
        subprocess.check_output(
            "cd /tmp && lark-cli base +record-list --base-token J8RGbNZobaXu9VsqtFWcKUemnQe "
            "--table-id tblHhkqqSWcpGD7Y --limit 100 --format ndjson "
            "--output ./asr_jx.ndjson --as user",
            shell=True, timeout=60, stderr=subprocess.DEVNULL)
        out = {}
        for line in open("/tmp/asr_jx.ndjson"):
            try:
                r = json.loads(line)
                c = str(r.get("代码", "")).split(".")[0].strip()
                if c:
                    out[c] = r.get("股票名称", "")
            except Exception:
                pass
        return out
    except Exception:
        return {}


# ---------------- 模块三: 板块资金流(复用 moneyflow skill) ----------------

def sector_flows(top_in=8, top_out=6):
    """行业板块主力净流入/流出(经 hithink-moneyflow): {inflow: [...], outflow: [...]}。"""
    res = {"inflow": [], "outflow": []}
    if not os.path.exists(FLOW_CLI):
        return res
    for view, key, n in (("industry-inflow", "inflow", top_in), ("industry-outflow", "outflow", top_out)):
        try:
            out = subprocess.check_output(
                ["python3", FLOW_CLI, "--view", view, "--top", str(n), "--format", "json"],
                env=ENV, timeout=90, stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
            m = re.search(r"\{.*\}", out, re.S)
            data = json.loads(m.group(0)) if m else {}
            # 归一化: name / amount(亿) / pct(涨跌幅)
            res[key] = [{"name": r.get("name"),
                         "amount": round((r.get("amount") or 0) / 1e8, 2),
                         "pct": r.get("pct")} for r in (data.get("rows") or [])]
        except Exception:
            res[key] = []
    return res


# ---------------- 模块四: 个股全景 ----------------

def stock_quote(code):
    """个股快照: 价/涨跌幅/换手/主力净流入/市值/PE。主链东财push2, 限流后备腾讯(仅名称/价/涨跌)。"""
    try:
        d = _get("https://push2.eastmoney.com/api/qt/stock/get?secid=%s&fields=f43,f57,f58,f60,f62,f116,f162,f168&ut=fa5fd1943c7b386f172d6893dbfba10b" % _secid(code))["data"]
        return {"code": d.get("f57"), "name": d.get("f58"),
                "price": d["f43"] / 100.0 if d.get("f43") else None,
                "chg": (d.get("f170") / 100.0) if d.get("f170") is not None else None,
                "main_inflow_wan": round(d["f62"] / 1e4, 0) if d.get("f62") is not None else None,
                "mktcap_yi": round(d["f116"] / 1e8, 0) if d.get("f116") else None,
                "pe_ttm": d["f162"] / 100.0 if d.get("f162") else None,
                "turnover_pct": d["f168"] / 100.0 if d.get("f168") else None}
    except Exception:
        try:
            req = urllib.request.Request("https://qt.gtimg.cn/q=" + _sym(code),
                                         headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", "ignore")
            p = raw.split('"')[1].split("~")
            return {"code": code, "name": p[1], "price": float(p[3]), "chg": float(p[32]),
                    "main_inflow_wan": None, "mktcap_yi": None,
                    "pe_ttm": None, "turnover_pct": None}
        except Exception:
            raise


def stock_kline_tx(code, days=250):
    """腾讯个股日K(前复权): [{date, close}]。"""
    sym = _sym(code)
    d = _get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq"
             % (sym, days), {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com"})
    node = (d.get("data") or {}).get(sym) or {}
    k = node.get("qfqday") or node.get("day") or []
    return [{"date": x[0], "close": float(x[2])} for x in k]


def stock_news(name, limit=5):
    """华尔街见闻搜索: [{title, uri, time}]。"""
    try:
        d = _get("https://api-one-wscn.awtmt.com/apiv1/search/article?query=%s&limit=%d"
                 % (urllib.parse.quote(name), limit),
                 {"User-Agent": "Mozilla/5.0"}, timeout=10)
        out = []
        for it in (d.get("data") or {}).get("items") or []:
            a = (it.get("resource") or {}).get("data") or {}
            if a.get("title"):
                out.append({"title": a["title"], "uri": a.get("uri"),
                            "time": (a.get("display_time") or "")})
        return out
    except Exception:
        return []


def stock_announcements(code, days=7):
    """巨潮近N日公告: [{title, date, url}]。code 需带交易所后缀(orgId 由接口解析)。"""
    try:
        d = _post("http://www.cninfo.com.cn/new/hisAnnouncement/query", {
            "pageNum": 1, "pageSize": 8, "column": "szse",
            "tabName": "fulltext", "stock": "%s,%s" % (code, "sh" if code.startswith("6") else "sz"),
            "seDate": ",,%d天前" % 0,  # 占位, 下面用日期区间
            "searchkey": "", "secid": "", "plate": "", "category": "",
        })
        return d
    except Exception:
        return None


def announcements_recent(code, days=7, limit=6):
    """巨潮公告(日期区间版): [{title, date}]。"""
    import datetime
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    try:
        d = _post("http://www.cninfo.com.cn/new/hisAnnouncement/query", {
            "pageNum": 1, "pageSize": limit, "column": "szse", "tabName": "fulltext",
            "stock": "%s,%s" % (code, "sh" if code.startswith("6") else "sz"),
            "seDate": "%s~%s" % (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
            "searchkey": "", "secid": "", "plate": "", "category": "", "isHLtitle": "true",
        })
        anns = (d.get("announcements") or [])
        return [{"title": (a.get("announcementTitle") or "").replace("<em>", "").replace("</em>", ""),
                 "date": time.strftime("%Y-%m-%d", time.localtime((a.get("announcementTime") or 0) / 1000))}
                for a in anns if a.get("announcementTitle")]
    except Exception:
        return []
