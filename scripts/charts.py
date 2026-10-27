#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""charts.py — a-share-review 图表层(matplotlib PNG, A股配色: 红涨绿跌/红入绿出)。
零API消耗, 全部本地渲染。中文字体自动探测(PingFang→Songti→Arial Unicode)。"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

RED, GREEN, GRAY, BLUE = "#d6404e", "#2e9e5b", "#8a8f98", "#3b7dd8"
_LIGHT, _DARK = "#f7f8fa", "#22262b"

for _f in ("PingFang SC", "Hiragino Sans GB", "Songti SC", "Arial Unicode MS", "STHeiti"):
    if any(_f.lower() in f.name.lower() for f in font_manager.fontManager.ttflist):
        plt.rcParams["font.family"] = _f
        break
plt.rcParams["axes.unicode_minus"] = False


def _style(ax):
    ax.set_facecolor(_LIGHT)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#d5d9e0")
    ax.tick_params(colors="#555", labelsize=9)
    ax.grid(axis="y", color="#e3e6eb", lw=0.6)


def save(fig, path, dpi=150):
    fig.tight_layout()
    fig.savefig(path, dpi=dpi, facecolor="white")
    plt.close(fig)
    return path


# ---- 图1: 近10日成交额(柱) + 上证收盘(折线, 右轴) ----
def chart_volume(dates, total_amount_yi, close, out):
    fig, ax = fig_ax = plt.subplots(figsize=(8.6, 3.4))
    colors = [RED if i == len(dates) - 1 else "#b9c2cf" for i in range(len(dates))]
    ax.bar(range(len(dates)), total_amount_yi, color=colors, width=0.62, zorder=2)
    _style(ax)
    ax.set_xticks(range(len(dates)))
    ax.set_xticklabels([d[5:] for d in dates], rotation=0, fontsize=8)
    ax.set_ylabel("两市成交额(亿)", fontsize=9, color="#555")
    for i, v in enumerate(total_amount_yi):
        ax.text(i, v, "%d" % v, ha="center", va="bottom", fontsize=7,
                color=RED if i == len(dates) - 1 else "#889")
    ax2 = ax.twinx()
    ax2.plot(range(len(dates)), close, color=BLUE, lw=1.6, marker="o", ms=3, zorder=3)
    ax2.set_ylabel("上证收盘", fontsize=9, color=BLUE)
    ax2.tick_params(colors=BLUE, labelsize=8)
    ax2.spines["top"].set_visible(False)
    return save(fig, out)


# ---- 图2: 近20日涨幅榜 横向条形(按板块着色, 精选表★) ----
def chart_gainers(items, out):
    items = sorted(items, key=lambda x: x["pct"])
    names = ["%s%s" % (it["name"], " ★" if it.get("starred") else "") for it in items]
    pcts = [it["pct"] for it in items]
    sectors = [it.get("industry") or "—" for it in items]
    uniq = sorted(set(sectors))
    cmap = dict((s, c) for s, c in zip(
        uniq, ["#3b7dd8", "#e08b2f", "#7a5fd0", "#2e9e5b", "#c2599c", "#5aa8d8",
               "#b9674e", "#6d8b3d", "#a8843c", "#8b6cc4", "#4fa3a5", "#c07f4f",
               "#7f8fa6", "#d6404e"][: len(uniq)]))
    fig, ax = plt.subplots(figsize=(8.6, max(3.2, 0.34 * len(items) + 0.9)))
    ax.barh(range(len(items)), pcts, color=[cmap[s] for s in sectors], height=0.62, zorder=2)
    _style(ax)
    ax.grid(axis="x", color="#e3e6eb", lw=0.6)
    ax.grid(axis="y", lw=0)
    ax.set_yticks(range(len(items)))
    ax.set_yticklabels(names, fontsize=9)
    for i, p in enumerate(pcts):
        ax.text(p + max(pcts) * 0.012, i, "+%.0f%%" % p, va="center", fontsize=8, color="#444")
    ax.set_xlim(0, max(pcts) * 1.16)
    ax.set_xlabel("近20日涨幅(%)", fontsize=9, color="#555")
    handles = [plt.Rectangle((0, 0), 1, 1, color=cmap[s]) for s in uniq]
    ax.legend(handles, uniq, fontsize=7.5, ncol=min(4, len(uniq)),
              loc="lower right", frameon=False)
    return save(fig, out)


# ---- 图3: 行业主力净额 发散条形(红入右/绿出左) ----
def chart_flow(inflow, outflow, out):
    rows = (inflow or [])[:8] + (outflow or [])[:6]
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: r.get("amount", 0))
    names = [r.get("name", "") for r in rows]
    vals = [r.get("amount", 0) for r in rows]
    chgs = [r.get("chg", None) for r in rows]
    fig, ax = plt.subplots(figsize=(8.6, max(3.0, 0.34 * len(rows) + 0.8)))
    colors = [RED if v > 0 else GREEN for v in vals]
    ax.barh(range(len(rows)), vals, color=colors, height=0.6, zorder=2)
    _style(ax)
    ax.grid(axis="x", color="#e3e6eb", lw=0.6)
    ax.grid(axis="y", lw=0)
    ax.axvline(0, color="#99a", lw=0.8)
    ax.set_yticks(range(len(rows)))
    labels = []
    for n, c in zip(names, chgs):
        labels.append("%s(%s)" % (n, ("%+.1f%%" % c) if isinstance(c, (int, float)) else "—"))
    ax.set_yticklabels(labels, fontsize=9)
    span = max(vals + [1]) - min(vals + [-1])
    for i, v in enumerate(vals):
        ax.text(v + (span * 0.012 if v >= 0 else -span * 0.012), i,
                "%+.1f亿" % v, va="center", ha="left" if v >= 0 else "right",
                fontsize=8, color="#444")
    ax.set_xlabel("主力净额(亿)  红=净流入 绿=净流出  括号内为板块涨跌幅", fontsize=8.5, color="#555")
    return save(fig, out)


# ---- 图4: 个股近60日 收盘+MA20+参考线 ----
def chart_stock(name, dates, closes, cost=None, high52=None, trigger=None, out=None):
    if len(closes) < 21:
        return None
    ma20 = [None] * len(closes)
    for i in range(19, len(closes)):
        ma20[i] = sum(closes[i - 19:i + 1]) / 20.0
    x = range(len(dates))
    fig, ax = plt.subplots(figsize=(8.6, 3.2))
    ax.plot(x, closes, color=BLUE, lw=1.6, label="收盘价", zorder=3)
    ax.plot(x, ma20, color="#e08b2f", lw=1.2, ls="--", label="MA20", zorder=2)
    cur = closes[-1]
    up20 = max(closes[-20:]) if len(closes) >= 20 else None
    refs = []
    if cost:
        refs.append(("成本 %.1f" % cost, cost, GRAY))
    if high52:
        refs.append(("一年新高 %.1f" % high52, high52, RED))
    if trigger:
        refs.append(("右侧触发(20日高) %.1f" % trigger, trigger, GREEN))
    for label, v, c in refs:
        ax.axhline(v, color=c, lw=1.0, ls=":", alpha=0.85)
        ax.text(len(dates) - 0.5, v, " " + label, fontsize=7.5, color=c, va="center")
    ax.scatter([len(dates) - 1], [cur], color=RED if closes[-1] >= (ma20[-1] or cur) else GREEN,
               s=26, zorder=4)
    _style(ax)
    step = max(1, len(dates) // 8)
    ax.set_xticks(list(x)[::step])
    ax.set_xticklabels([d[5:] for d in dates][::step], fontsize=8)
    ax.set_title("%s  现价 %.2f (距20日高 %.1f%%)" %
                 (name, cur, (cur / up20 - 1) * 100 if up20 else 0), fontsize=10, color=_DARK)
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    return save(fig, out)
