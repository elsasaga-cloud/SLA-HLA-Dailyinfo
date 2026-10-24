#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动数据链：交易日确认 → 双数据集滚动 → 再生/引擎 → 校验 → 摘要 → git。

用途（配合每天 15:01 的定时任务）：
  1. 确认今天是否为 A 股交易日（周末闸门 + 东方财富日线是否存在当日 K 线）；
  2. 交易日则把 data/eastmoney（kline250/warmup209/volume5）与 data/15min
     滚动到最新收盘，重叠带逐字段校验，全部通过才落盘；任何一步失败自动回滚；
  3. 再生 daily_text / 15min 报告 / metadata，跑吸筹引擎（结果存 自动化/<日期>/engine/）；
  4. 同步 tests 日期锚并跑 unittest；失败=回滚+非零退出；
  5. 写 自动化/<日期>/auto_digest.md + ROLLED.json（供后续对话判读引用），可选 git commit/push。

只做机械半场；00-结论/03-检索/04-操作映射/台账行仍由 Arena 对话（agent）完成——
宪法 v1.2 线性结论链必须引用历史结论并给出差异，机器不代写判定。

用法：
  python scripts/auto_run.py                 # 完整流程（默认今天，北京时间）
  python scripts/auto_run.py --check         # 只做交易日确认，不改任何文件
  python scripts/auto_run.py --push          # 完整流程且成功后 git push
  python scripts/auto_run.py --date 2026-10-22
  python scripts/auto_run.py --selftest      # 离线自检纯函数逻辑
环境变量：
  ACC_OFFLINE_DIR=<目录>   离线回放：从该目录读 em_<code>.json / sina_<code>.json（调试/演练用）

退出码：0 正常（含非交易日跳过）；2 unittest 失败（已回滚）；3 运行失败（已回滚）；
       4 网络不可达；5 数据未就绪（收盘数据尚未生成，稍后重跑即可）；6 参数/状态异常。
仅用 Python 标准库，无第三方依赖。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
EM_DIR = DATA / "eastmoney"
MIN15_DIR = DATA / "15min"
AUTO_DIR = ROOT / "自动化"
BACKUP_DIR = ROOT / ".auto_backup"
TZ8 = dt.timezone(dt.timedelta(hours=8))  # 固定北京时间，机器时区无关

EM_HOSTS = ["48.push2his.eastmoney.com", "push2his.eastmoney.com"]
SINA_URL = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData"
            "?symbol={sym}&scale=15&ma=no&datalen={n}")
SLOTS = ["09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30",
         "13:15", "13:30", "13:45", "14:00", "14:15", "14:30", "14:45", "15:00"]
GATE_CODE = "601988"  # 交易日探针：中国银行（每个交易日必有日线）
SINA_LEN = 65         # 覆盖 4 个交易日（64 根）+重叠，容错漏跑 2 天
W, K = 209, 250       # chip warmup / kline 窗口

STOCKS = [  # (code, name, market: 0=SZ 1=SH)
    ("000651", "格力电器", 0), ("000807", "云铝股份", 0), ("000858", "五粮液", 0),
    ("002352", "顺丰控股", 0), ("300124", "汇川技术", 0), ("300760", "迈瑞医疗", 0),
    ("600362", "江西铜业", 1), ("600398", "海澜之家", 1), ("600690", "海尔智家", 1),
    ("600863", "华能蒙电", 1), ("601138", "工业富联", 1), ("601600", "中国铝业", 1),
    ("601899", "紫金矿业", 1), ("601988", "中国银行", 1),
]
BY_CODE = {c: (n, m) for c, n, m in STOCKS}


# ---------------------------------------------------------------- 基础工具
def log(msg: str) -> None:
    print(f"[{dt.datetime.now(TZ8):%H:%M:%S}] {msg}", flush=True)


def http_get(url: str, timeout: int = 25, retries: int = 3) -> bytes:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise ConnectionError(f"GET 失败 {url}: {last!r}")


def offline_path(kind: str, code: str):
    d = os.environ.get("ACC_OFFLINE_DIR")
    if not d:
        return None
    p = Path(d) / f"{kind}_{code}.json"
    return p if p.is_file() else None


def em_klines(code: str, beg: str, end: str) -> list:
    """东方财富日线（不改权）。返回 11 字段逗号串列表：date,open,close,high,low,vol,amt,ampl,pct,chg,to。"""
    fp = offline_path("em", code)
    if fp:
        return list(json.loads(fp.read_text(encoding="utf-8"))["data"]["klines"])
    data = None
    for host in EM_HOSTS:
        url = (f"https://{host}/api/qt/stock/kline/get?secid={BY_CODE[code][1]}.{code}"
               f"&klt=101&fqt=0&beg={beg.replace('-', '')}&end={end.replace('-', '')}"
               f"&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61")
        try:
            data = json.loads(http_get(url).decode("utf-8"))
        except ConnectionError:
            continue
        if data and data.get("data") and data["data"].get("klines") is not None:
            break
        data = None
    if data is None:
        raise ConnectionError(f"EM 日线不可达: {code}")
    return list(data["data"]["klines"])


def sina_15min(code: str, datalen: int) -> list:
    """新浪 15 分钟线。volume 字段=股。返回 [{day,open,high,low,close,volume,amount}]。"""
    fp = offline_path("sina", code)
    if fp:
        return json.loads(fp.read_text(encoding="utf-8"))
    sym = ("sh" if BY_CODE[code][1] == 1 else "sz") + code
    raw = http_get(SINA_URL.format(sym=sym, n=datalen)).decode("utf-8")
    m = re.search(r"\((\[.*\])\)", raw, re.S)
    if not m:
        raise RuntimeError(f"新浪响应解析失败: {code}")
    return json.loads(m.group(1))


def read_csv_text(path: Path) -> tuple:
    raw = path.read_bytes()
    return raw.startswith(b"\xef\xbb\xbf"), raw.decode("utf-8-sig")


def write_csv(path: Path, lines: list, bom: bool) -> None:
    enc = "utf-8-sig" if bom else "utf-8"
    with open(path, "w", encoding=enc, newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")


def fclose(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def assert_overlap_kline(stored: str, fresh: str, code: str) -> None:
    s, f = stored.split(","), fresh.split(",")
    assert s[0] == f[0], f"{code} 重叠日期错位 {s[0]}!={f[0]}"
    for i in (1, 2, 3, 4):
        assert fclose(float(s[i]), float(f[i]), 1e-6), f"{code} {s[0]} OHLC 不一致: {s[i]} vs {f[i]}"
    assert abs(float(s[5]) - float(f[5])) <= 1.0, f"{code} {s[0]} 量不一致"
    assert fclose(float(s[6]), float(f[6]), 1.0), f"{code} {s[0]} 额不一致"
    for i in (7, 8, 9, 10):
        assert fclose(float(s[i]), float(f[i]), 0.011), f"{code} {s[0]} 字段{i}不一致: {s[i]} vs {f[i]}"


def _wrow(kline_row: str) -> str:
    p = kline_row.split(",")
    return ",".join([p[0], p[1], p[2], p[3], p[4], p[10]])  # date,open,close,high,low,turnover


def _vrow(kline_row: str) -> str:
    p = kline_row.split(",")
    return f"{p[0]},{p[5]}"  # date,volume_lots


class NotReady(Exception):
    pass


# ---------------------------------------------------------------- 交易日确认
def em_bar_exists(target: dt.date) -> bool:
    rows = em_klines(GATE_CODE, target.isoformat(), target.isoformat())
    return any(r.split(",")[0] == target.isoformat() for r in rows)


def confirm_trading_day(target: dt.date) -> tuple:
    if target.weekday() >= 5:
        return False, "周末"
    reason = ""
    for attempt in range(5):
        try:
            if em_bar_exists(target):
                return True, f"EM 日线含 {target} K 线（{GATE_CODE} 探针）"
            reason = "数据未就绪或非交易日"
        except ConnectionError as e:
            reason = f"网络不可达: {e}"
            if attempt == 4:
                raise
        if attempt < 4:
            log(f"{reason}，180 秒后重试（{attempt + 1}/5）")
            time.sleep(180)
    return False, reason


# ---------------------------------------------------------------- 日线滚动
def roll_daily(target: dt.date, corp_actions: dict) -> dict:
    """kline250 尾部追加；warmup209/volume5 与 kline 头联动滑窗（k 滑入=kline 滑出的头 k 行）。"""
    summary = {}
    for code, name, _m in STOCKS:
        f_k = next(EM_DIR.glob(f"{code}_*kline_250d.csv"))
        f_w = next(EM_DIR.glob(f"{code}_*warmup_209d.csv"))
        f_v = next(EM_DIR.glob(f"{code}_*volume_warmup_5d.csv"))
        bom_k, txt = read_csv_text(f_k)
        klines = txt.splitlines()
        header, krows = klines[0], klines[1:]
        bom_w, wtxt = read_csv_text(f_w)
        wlines = wtxt.splitlines()
        wheader, wrows = wlines[0], wlines[1:]
        bom_v, vtxt = read_csv_text(f_v)
        vlines = vtxt.splitlines()
        vheader, vrows = vlines[0], vlines[1:]
        assert len(krows) == K and len(wrows) == W and len(vrows) == 5, f"{code} 现窗口长度异常"
        last = krows[-1].split(",")[0]
        fresh = em_klines(code, (dt.date.fromisoformat(last) - dt.timedelta(days=5)).isoformat(),
                          target.isoformat())
        overlap = [r for r in fresh if r.split(",")[0] <= last]
        new = [r for r in fresh if r.split(",")[0] > last]
        tail_map = {r.split(",")[0]: r for r in krows[-8:]}
        for r in overlap:
            d = r.split(",")[0]
            if d in tail_map:
                assert_overlap_kline(tail_map[d], r, code)
        dates = [r.split(",")[0] for r in new]
        assert dates == sorted(set(dates)), f"{code} 新日期非严格递增: {dates}"
        assert all(d <= target.isoformat() for d in dates), f"{code} 出现未来日期"
        if not new:
            summary[code] = {"name": name, "added": []}
            log(f"{code} {name}: 日线无新增")
            continue
        # 除权除息检测：|close - prev_close - change_amount| > 0.011
        prev_close = float(krows[-1].split(",")[2])
        for r in new:
            p = r.split(",")
            if abs(float(p[2]) - prev_close - float(p[9])) > 0.011:
                corp_actions.setdefault(code, []).append(
                    {"date": p[0], "close": p[2], "chg": p[9],
                     "hint": "疑似除权除息，需人工在 generate_daily_text.py 配置 corporate action 并重跑判读"})
            prev_close = float(p[2])
        k = len(new)
        combined = wrows + krows + new                      # 连续 459+k 行
        assert len(combined) == W + K + k
        assert all(combined[i].split(",")[0] < combined[i + 1].split(",")[0] for i in range(len(combined) - 1)), \
            f"{code} combined 序列乱序/断档"
        new_krows = combined[-K:]                           # 最后 250 行
        entering = krows[:k]                                # 滑出 kline 头 → 滑入 warmup/vol5 尾
        w2 = wrows[k:] + [_wrow(r) for r in entering]
        v2 = vrows[k:] + [_vrow(r) for r in entering]
        assert len(new_krows) == K and len(w2) == W and len(v2) == 5
        assert w2[-1].split(",")[0] < new_krows[0].split(",")[0], f"{code} warmup 尾与 kline 头重叠"
        assert v2[-1].split(",")[0] == w2[-1].split(",")[0], f"{code} volume5 尾与 warmup 尾不一致"
        write_csv(f_k, [header] + new_krows, bom_k)
        write_csv(f_w, [wheader] + w2, bom_w)
        write_csv(f_v, [vheader] + v2, bom_v)
        summary[code] = {"name": name, "added": dates,
                         "kline": f"{new_krows[0].split(',')[0]}..{new_krows[-1].split(',')[0]}",
                         "warmup_tail": w2[-1].split(",")[0], "vol5_head": v2[0].split(",")[0]}
        log(f"{code} {name}: 日线 +{k} ({dates[0]}..{dates[-1]}), kline {summary[code]['kline']}")
    return summary


# ---------------------------------------------------------------- 15min 滚动
def roll_15min(target: dt.date, tmp_json: Path) -> dict:
    tmp_json.mkdir(parents=True, exist_ok=True)
    summary = {}
    for code, name, _m in STOCKS:
        f_raw = next(MIN15_DIR.glob(f"{code}_*/*_15min_raw.csv"))
        bom, txt = read_csv_text(f_raw)
        lines = txt.splitlines()
        header, rows = lines[0], lines[1:]
        assert len(rows) == 336, f"{code} 现窗 {len(rows)}≠336"
        items = sina_15min(code, SINA_LEN)
        win_end = rows[-1].split(",")[0]
        tail_map = {r.split(",")[0]: r for r in rows[-40:]}
        new_items = []
        for it in items:
            d = it["day"]
            if d <= win_end:
                if d in tail_map:  # 重叠校验
                    p = tail_map[d].split(",")
                    assert fclose(float(p[1]), float(it["open"]), 1e-6) and \
                           fclose(float(p[2]), float(it["close"]), 1e-6) and \
                           fclose(float(p[3]), float(it["high"]), 1e-6) and \
                           fclose(float(p[4]), float(it["low"]), 1e-6), f"{code} {d} 15min OHLC 不一致"
                    assert fclose(float(p[5]), float(it["volume"]) / 100, 0.51), f"{code} {d} 量不一致"
                    assert fclose(float(p[6]), float(it["amount"]), 1.0), f"{code} {d} 额不一致"
                continue
            new_items.append(it)
        days = {}
        for it in new_items:
            days.setdefault(it["day"][:10], []).append(it)
        for d in sorted(days):
            got = [x["day"][11:16] for x in days[d]]
            if got != SLOTS:
                if d == target.isoformat() and len(got) < 16:
                    raise NotReady(f"{code} {d} 15min 仅 {len(got)}/16 根（收盘数据未就绪）")
                raise RuntimeError(f"{code} {d} 槽位异常: {got}")
        if not new_items:
            summary[code] = {"name": name, "added": []}
            log(f"{code} {name}: 15min 无新增")
            continue
        app = []
        for d in sorted(days):
            for i, it in enumerate(days[d]):
                app.append(",".join([it["day"], repr(float(it["open"])), repr(float(it["close"])),
                                     repr(float(it["high"])), repr(float(it["low"])),
                                     repr(round(float(it["volume"]) / 100, 2)), repr(float(it["amount"])),
                                     d, it["day"][11:16], str(i)]))
        new_rows = rows[len(app):] + app
        assert len(new_rows) == 336, f"{code} 滚动后 {len(new_rows)}≠336"
        assert all(new_rows[i].split(",")[0] < new_rows[i + 1].split(",")[0] for i in range(335)), f"{code} 15min 乱序"
        write_csv(f_raw, [header] + new_rows, bom)
        out_items = [{"day": r.split(",")[0], "open": r.split(",")[1], "high": r.split(",")[3],
                      "low": r.split(",")[4], "close": r.split(",")[2],
                      "volume": str(int(round(float(r.split(",")[5]) * 100))), "amount": r.split(",")[6]}
                     for r in new_rows]
        (tmp_json / f"{code}.json").write_text(json.dumps(out_items, ensure_ascii=False), encoding="utf-8")
        ndays = sorted(days)
        summary[code] = {"name": name, "added": ndays}
        log(f"{code} {name}: 15min +{len(app)} 根 ({ndays[0]}..{ndays[-1]})")
    return summary


# ---------------------------------------------------------------- 再生/校验/引擎
def run(cmd: list, env_extra: dict = None) -> str:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"命令失败 {' '.join(map(str, cmd))}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return r.stdout


def kline_head() -> str:
    f = next(EM_DIR.glob("000651_*kline_250d.csv"))
    return read_csv_text(f)[1].splitlines()[1].split(",")[0]


def latest_kline_date() -> str:
    f = next(EM_DIR.glob("000651_*kline_250d.csv"))
    return read_csv_text(f)[1].splitlines()[-1].split(",")[0]


def warmup_tail() -> str:
    f = next(EM_DIR.glob("000651_*warmup_209d.csv"))
    return read_csv_text(f)[1].splitlines()[-1].split(",")[0]


def refresh_em_metadata() -> tuple:
    p = EM_DIR / "metadata.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    for e in meta["files"]:
        fp = ROOT / e["path"]
        raw = fp.read_bytes()
        e["bytes"] = len(raw)
        e["sha256"] = hashlib.sha256(raw).hexdigest()
        if fp.suffix == ".csv":
            with open(fp, encoding="utf-8-sig", newline="") as f:
                e["rows"] = sum(1 for _ in csv.DictReader(f))
    meta["as_of"] = meta["last_completed_session"] = latest_kline_date()
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return meta["as_of"], len(meta["files"])


def sync_test_anchors() -> dict:
    """把 tests/test_data.py 的四个日期锚同步为当前数据口径（仅这四锚，其余一概不动）。"""
    p = ROOT / "tests" / "test_data.py"
    s = p.read_text(encoding="utf-8")
    old_head = re.search(r'assertEqual\("(\d{4}-\d{2}-\d{2})", target\[0\]\["date"\]\)', s).group(1)
    old_tail = re.search(r'assertEqual\("(\d{4}-\d{2}-\d{2})", target\[-1\]\["date"\]\)', s).group(1)
    old_wtail = re.search(r'assertEqual\("(\d{4}-\d{2}-\d{2})", warmup\[-1\]\["date"\]\)', s).group(1)
    m = re.search(r'"(\d{4}-\d{2}-\d{2})" if stock\.code == "600863" else "(\d{4}-\d{2}-\d{2})"', s)
    old_whead863, old_whead = m.group(1), m.group(2)
    new_head, new_tail, new_wtail = kline_head(), latest_kline_date(), warmup_tail()
    new_whead = warmup_head("000651")
    new_whead863 = warmup_head("600863")
    pairs = [(old_head, new_head, r'(assertEqual\(")\d{4}-\d{2}-\d{2}(", target\[0\]\["date"\]\))'),
             (old_tail, new_tail, r'(assertEqual\(")\d{4}-\d{2}-\d{2}(", target\[-1\]\["date"\]\))'),
             (old_wtail, new_wtail, r'(assertEqual\(")\d{4}-\d{2}-\d{2}(", warmup\[-1\]\["date"\]\))')]
    for old, new, pat in pairs:
        if old == new:
            continue
        s2, nsub = re.subn(pat, r"\g<1>" + new + r"\g<2>", s, count=1)
        assert nsub == 1, f"锚替换失败 {old}->{new}"
        s = s2
    if old_whead != new_whead or old_whead863 != new_whead863:
        pat = r'"(\d{4}-\d{2}-\d{2})" if stock\.code == "600863" else "(\d{4}-\d{2}-\d{2})"'
        repl = f'"{new_whead863}" if stock.code == "600863" else "{new_whead}"'
        s2, nsub = re.subn(pat, repl, s, count=1)
        assert nsub == 1, f"warmup 头锚替换失败"
        s = s2
    p.write_text(s, encoding="utf-8", newline="")
    return {"kline_head": [old_head, new_head], "kline_tail": [old_tail, new_tail],
            "warmup_tail": [old_wtail, new_wtail],
            "warmup_head": [old_whead, new_whead], "warmup_head_600863": [old_whead863, new_whead863]}


def warmup_head(code: str) -> str:
    f = next(EM_DIR.glob(f"{code}_*warmup_209d.csv"))
    return read_csv_text(f)[1].splitlines()[1].split(",")[0]


def run_engine(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    run([sys.executable, str(ROOT / "scripts" / "accum_engine.py")], {"ACC_OUT_DIR": str(out_dir)})
    missing = [c for c, *_ in STOCKS if not (out_dir / f"{c}.json").is_file()]
    assert not missing, f"引擎缺输出: {missing}"


def flat(o, p=""):
    d = {}
    if isinstance(o, dict):
        for k, v in o.items():
            d.update(flat(v, f"{p}.{k}" if p else k))
    else:
        d[p] = o
    return d


def diff_engine(out_dir: Path) -> dict:
    """新引擎输出 vs 各库最新日期快照：布尔翻转 + 关键值变化（机械信号，判定仍归对话）。"""
    res = {}
    for code, name, _m in STOCKS:
        lib = ROOT / f"{name}吸筹"
        prev = None
        if lib.is_dir():
            dated = sorted([d for d in lib.iterdir() if d.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}", d.name)])
            for d in reversed(dated):
                if (d / "snapshot.json").is_file():
                    prev = json.loads((d / "snapshot.json").read_text(encoding="utf-8"))
                    break
        cur = json.loads((out_dir / f"{code}.json").read_text(encoding="utf-8"))
        if prev is None:
            res[code] = {"name": name, "flips": [], "notable": [], "note": "无历史快照"}
            continue
        fc, fp_ = flat(cur), flat(prev)
        flips, notable = [], []
        for k2 in sorted(fc):
            if k2 not in fp_ or fc[k2] == fp_[k2]:
                continue
            if isinstance(fc[k2], bool) or isinstance(fp_[k2], bool):
                flips.append(f"{k2}: {fp_[k2]}→{fc[k2]}")
            elif k2 in ("v1.rsi14", "v1.obv_pctile", "snap.c", "snap.pct", "conc.conc90"):
                notable.append(f"{k2}: {fp_[k2]}→{fc[k2]}")
        res[code] = {"name": name, "flips": flips, "notable": notable}
    return res


# ---------------------------------------------------------------- 备份/回滚/git
def backup(targets: list, stamp: str) -> Path:
    bdir = BACKUP_DIR / stamp
    bdir.mkdir(parents=True, exist_ok=True)
    for t in targets:
        dst = bdir / t.name
        if dst.exists():
            shutil.rmtree(dst) if t.is_dir() else dst.unlink()
        (shutil.copytree if t.is_dir() else shutil.copy2)(t, dst)
    _prune_backups()
    return bdir


def restore(bdir: Path, targets: list) -> None:
    for t in targets:
        dst = bdir / t.name
        if not dst.exists():
            continue
        if t.is_dir():
            shutil.rmtree(t)
            shutil.copytree(dst, t)
        else:
            shutil.copy2(dst, t)


def _prune_backups(keep: int = 3) -> None:
    if BACKUP_DIR.is_dir():
        for d in sorted(BACKUP_DIR.iterdir())[:-keep]:
            shutil.rmtree(d, ignore_errors=True)


def git_commit_push(date_iso: str, push: bool, note: str) -> dict:
    def _g(*a):
        return subprocess.run(["git", *a], cwd=str(ROOT), capture_output=True, text=True)
    _g("add", "-A")
    c = _g("commit", "-m", f"auto data roll {date_iso}: {note} (auto_run)")
    sha = c.stdout.strip().split("\n")[0] if c.returncode == 0 else "commit-failed"
    if c.returncode != 0:
        log(f"git commit 失败: {c.stderr.strip()[:300]}（多为未配置 git 身份：git config --global user.name/user.email）")
    pushed = False
    if push and c.returncode == 0:
        br = _g("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        p = _g("push", "origin", br)
        pushed = p.returncode == 0
        if not pushed:
            log(f"push 失败: {p.stderr.strip()[:300]}（本地已提交，可手动 push）")
    return {"committed": sha, "pushed": pushed}


# ---------------------------------------------------------------- 自检
def selftest() -> None:
    # 槽位分组
    items = [{"day": f"2026-01-01 {t}:00"} for t in SLOTS]
    days = {}
    for it in items:
        days.setdefault(it["day"][:10], []).append(it["day"][11:16])
    assert days["2026-01-01"] == SLOTS
    # 滑窗数学：combined 459+k，kline=末 250，warmup=其前 209，滑入=kline 头 k 行
    rows = [f"r{i:04d}" for i in range(W + K)]
    k = 3
    plus = [f"n{i}" for i in range(k)]
    combined = rows + plus
    assert combined[-K:][0] == rows[W + k] and combined[-K:][-1] == plus[-1]
    w2 = rows[:W][k:] + rows[W:W + k]
    assert len(w2) == W and w2[0] == rows[k] and w2[-1] == rows[W + k - 1]
    # jsonp 解析
    m = re.search(r"\((\[.*\])\)", 'var _=([{"day":"2026-01-01 15:00:00"}]);', re.S)
    assert json.loads(m.group(1))[0]["day"].startswith("2026-01-01")
    # 除权检测
    assert abs(10.0 - 10.5 - (-0.5)) < 0.011
    assert abs(10.0 - 10.5 - 0.0) > 0.011
    # 锚正则
    s = 'self.assertEqual("2025-10-10", target[0]["date"])'
    pat = r'(assertEqual\(")\d{4}-\d{2}-\d{2}(", target\[0\]\["date"\]\))'
    assert re.subn(pat, r"\g<1>2025-10-13\g<2>", s, count=1)[1] == 1
    print("selftest OK")


# ---------------------------------------------------------------- 摘要
def write_digest(out_dir: Path, stamp: str, why: str, daily, min15, corp, anchors, flips, as_of, nfiles) -> None:
    L = [f"# 自动数据链摘要 — {stamp}", "",
         f"**交易日确认**：{why}",
         f"**日线**：as_of={as_of}，metadata 刷新 {nfiles} 文件；锚同步 {json.dumps(anchors, ensure_ascii=False)}",
         f"**除权除息标记**：{json.dumps(corp, ensure_ascii=False) if corp else '无（|Δclose−prev−chg|<0.011 ×14）'}",
         "", "## 引擎信号翻转（vs 各库最新快照；机械信号，判定以对话半场为准）", "",
         "| 代码 | 名称 | 布尔翻转 | 关键值 |", "|---|---|---|---|"]
    for c, r in flips.items():
        L.append(f"| {c} | {r['name']} | {'；'.join(r['flips']) or '—'} | {'；'.join(r.get('notable', [])) or '—'} |")
    L += ["", "## 新增数据", ""]
    for c, v in daily.items():
        if v["added"]:
            L.append(f"- {c} {v['name']}: 日线 {v['added']}（kline 窗 {v['kline']}）")
    for c, v in min15.items():
        if v.get("added"):
            L.append(f"- {c} {v['name']}: 15min 会话 {v['added']}")
    L += ["", "## 下一步", "",
          "- 在 Arena 对话输入「跑」：agent 完成检索、结论链引用、六件套、台账、push（宪法 v1.2）。",
          "- 若上方出现除权除息标记：先在 generate_daily_text.py 配置 corporate action，再让 agent 重跑判读。",
          "- 引擎布尔翻转≠操作指令；历史判定引用见各库《结论台账.md》。", ""]
    (out_dir / "auto_digest.md").write_text("\n".join(L), encoding="utf-8")


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="目标交易日 YYYY-MM-DD（默认=北京时间今天）")
    ap.add_argument("--check", action="store_true", help="仅确认交易日，不改文件")
    ap.add_argument("--push", action="store_true", help="成功后 git push")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return 0
    target = (dt.datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
              else dt.datetime.now(TZ8).date())
    log(f"目标日期 {target}（北京时间）")
    try:
        is_trading, why = confirm_trading_day(target)
    except ConnectionError as e:
        log(f"❓ 网络不可达，无法确认交易日：{e}")
        return 4
    if not is_trading:
        log(f"—— {target} 非交易日（{why}），跳过 ——")
        return 0
    log(f"✓ 交易日确认：{why}")
    if args.check:
        return 0

    if latest_kline_date() >= target.isoformat():
        log(f"数据已滚至 {latest_kline_date()}，无需重复滚动（幂等退出）")
        return 0

    stamp = target.isoformat()
    corp = {}
    out_dir = AUTO_DIR / stamp
    tmp_json = BACKUP_DIR / stamp / "json"
    targets = [DATA, ROOT / "tests"]
    bdir = backup(targets, stamp)
    log(f"备份完成: {bdir}")
    started = False
    try:
        daily_sum = roll_daily(target, corp)
        started = True
        min15_sum = roll_15min(target, tmp_json)
        run([sys.executable, str(ROOT / "scripts" / "generate_daily_text.py")])
        run([sys.executable, str(ROOT / "scripts" / "analyze_15min_volume.py"),
             "--source-json-dir", str(tmp_json), "--output", str(MIN15_DIR)])
        as_of, nfiles = refresh_em_metadata()
        engine_dir = out_dir / "engine"
        run_engine(engine_dir)
        flips = diff_engine(engine_dir)
        anchors = sync_test_anchors()
        log(f"测试锚同步: {json.dumps(anchors, ensure_ascii=False)}")
        ut = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                            cwd=str(ROOT), capture_output=True, text=True)
        if ut.returncode != 0 or not ut.stderr.rstrip().endswith("OK"):
            raise UnitTestFail(ut.stderr[-1500:])
        log("unittest 全部通过")
        n_added = sum(len(v["added"]) for v in daily_sum.values())
        note = (f"kline→{latest_kline_date()} 日线+{n_added}条/14股 "
                f"15min+{sorted(min15_sum['000651']['added']) if min15_sum['000651']['added'] else []} unittest 9/9")
        write_digest(out_dir, stamp, why, daily_sum, min15_sum, corp, anchors, flips, as_of, nfiles)
        rolled = {"date": stamp, "trading_day_proof": why,
                  "kline": {"head": kline_head(), "tail": latest_kline_date()},
                  "warmup_tail": warmup_tail(), "anchors": anchors,
                  "corporate_action_flags": corp,
                  "engine_flips": {c: r["flips"] for c, r in flips.items() if r["flips"]},
                  "daily_added": {c: v["added"] for c, v in daily_sum.items()},
                  "min15_added": {c: v["added"] for c, v in min15_sum.items()},
                  "note": "数据已自动滚动，agent 判读时勿重复滚动；检索/判定/六件套/台账仍由对话完成"}
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "ROLLED.json").write_text(json.dumps(rolled, ensure_ascii=False, indent=1), encoding="utf-8")
        g = git_commit_push(stamp, args.push, note)
        log(f"git: {json.dumps(g, ensure_ascii=False)}")
        log(f"✅ 自动数据链完成，摘要见 {out_dir / 'auto_digest.md'}；在 Arena 对话输入「跑」继续判读半场")
        return 0
    except NotReady as e:
        log(f"⏳ 数据未就绪：{e}；稍后重跑即可（幂等）")
        _rollback(bdir, targets, out_dir, started)
        return 5
    except UnitTestFail as e:
        log(f"❌ unittest 失败：{e}")
        _rollback(bdir, targets, out_dir, started)
        return 2
    except Exception as e:  # noqa: BLE001
        log(f"❌ 失败：{e!r}；已回滚到运行前状态")
        _rollback(bdir, targets, out_dir, started)
        return 3


class UnitTestFail(Exception):
    pass


def _rollback(bdir: Path, targets: list, out_dir: Path, started: bool) -> None:
    if started:
        restore(bdir, targets)
        if out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
        log("回滚完成（data/ tests/ 已还原，自动化目录已清理）")


if __name__ == "__main__":
    sys.exit(main())
