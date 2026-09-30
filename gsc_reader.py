# -*- coding: utf-8 -*-
"""
GSC（Google Search Console）レポートの読み込み ── Google参考値タブ用
────────────────────────────────────────────────────────────────
GSC の管理画面から手動でダウンロードした Excel（.xlsx）を読み、Google参考値タブに
埋め込むデータを作る。依存は Python 標準ライブラリのみ（xlsx は zip＋XML として直接読む）。

入力：
  gsc_dir 配下の  <サイト略称>｜GSC｜Performance-on-Search*.xlsx
    - 「検索パフォーマンス」：クエリ・ページ・日別（クリック数／表示回数／CTR／掲載順位）
    - 「…Generative-AI-Features…」（生成AI機能）：ページ・日別の表示回数のみ
  サイト略称はファイル名の先頭（最初の「｜」まで）。GSC_SITES にないものは読み飛ばす。

期間：「フィルタ」シートの日付（YYYY/MM/DD-YYYY/MM/DD）。読めなければ日別シートの最初と最後の日。
  1日〜月末ちょうどなら「月次」（例 2026-10）、それ以外は「期間集計（参考）」として扱う。
  同じサイト・種類・期間のファイルが複数あれば、更新日時が新しいものを採用する。
"""

import calendar
import csv
import glob
import io
import json
import os
import re
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}

# サイト略称 -> 表示名・ドメイン（config.json の gsc_sites で上書き可）
GSC_SITES = {
    "CO":     {"label": "コーポレート",            "domain": "onward-cd.co.jp"},
    "UN":     {"label": "ユニフォーム",            "domain": "uniform.onward-cd.co.jp"},
    "SCH":    {"label": "スクール",               "domain": "school.onward-cd.co.jp"},
    "MED":    {"label": "メディカル（病院向け）",   "domain": "medical.onward-cd.co.jp"},
    "MED-EC": {"label": "メディカル（EC）",        "domain": "onward-raffiria.shop"},
    "IS":     {"label": "インサイトセールス",       "domain": "solution.onward-cd.co.jp"},
}

AI_PAGES_TOP = 30   # 生成AI機能：期間・サイトごとに埋め込む上位ページ数


def norm_query(s):
    """照合用の正規化：全角半角・大文字小文字・空白の揺れを揃える。"""
    return " ".join(unicodedata.normalize("NFKC", str(s or "")).lower().split())


# ────────────────────────────────────────────────────────────────
# xlsx（標準ライブラリで読む）
# ────────────────────────────────────────────────────────────────
def _col_index(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(path):
    """{シート名: [[セル値, ...], ...]}。数値は float、文字列は str。"""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
        rels = {}
        for rel in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).findall("rel:Relationship", NS):
            tgt = rel.get("Target").lstrip("/")
            rels[rel.get("Id")] = tgt if tgt.startswith("xl/") else "xl/" + tgt
        book = {}
        for sh in ET.fromstring(z.read("xl/workbook.xml")).find("m:sheets", NS):
            target = rels.get(sh.get(f"{{{NS['r']}}}id"))
            if not target or target not in names:
                continue
            rows = []
            for row in ET.fromstring(z.read(target)).iter(f"{{{NS['m']}}}row"):
                vals = []
                for c in row.findall("m:c", NS):
                    i = _col_index(c.get("r")) if c.get("r") else len(vals)
                    while len(vals) < i:
                        vals.append(None)
                    t = c.get("t")
                    v = c.find("m:v", NS)
                    if t == "s" and v is not None:
                        val = shared[int(v.text)]
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(f"{{{NS['m']}}}t"))
                    elif t in ("str", "b") and v is not None:
                        val = v.text
                    elif v is not None and v.text is not None:
                        try:
                            val = float(v.text)
                        except ValueError:
                            val = v.text
                    else:
                        val = None
                    vals.append(val)
                rows.append(vals)
            book[sh.get("name")] = rows
        return book


# ────────────────────────────────────────────────────────────────
# GSC ファイル 1 本の解釈
# ────────────────────────────────────────────────────────────────
def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except ValueError:
        return None


def _to_date(v):
    """'2026-05-28' / '2026/05/28' / Excel のシリアル値 → date。"""
    if isinstance(v, float):
        return date(1899, 12, 30) + timedelta(days=int(v))
    m = re.match(r"\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})", str(v or ""))
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _table(rows):
    """先頭行を見出しとして dict のリストに。空行は除く。"""
    if not rows:
        return []
    head = [str(h or "").strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not any(x not in (None, "") for x in r):
            continue
        out.append({head[i]: (r[i] if i < len(r) else None) for i in range(len(head))})
    return out


def _pick(d, *keys):
    for k in keys:
        if k in d:
            return d[k]
    return None


def _period_key(start, end):
    """(key, label, monthly)。1日〜月末ちょうどなら月次。"""
    if start and end and start.day == 1 and start.year == end.year and start.month == end.month \
            and end.day == calendar.monthrange(end.year, end.month)[1]:
        k = f"{start:%Y-%m}"
        return k, k, True
    k = f"{start:%Y-%m-%d}~{end:%Y-%m-%d}"
    return k, f"{start:%Y/%m/%d}〜{end:%Y/%m/%d}（期間集計・参考）", False


def site_of(filename, sites):
    """ファイル名の先頭（最初の「｜」または「|」まで）をサイト略称とみなす。"""
    head = re.split(r"[｜|]", os.path.basename(filename), maxsplit=1)[0].strip()
    head = unicodedata.normalize("NFKC", head).upper()
    return head if head in sites else None


def parse_file(path, sites):
    """返り値: dict（ok=False なら note に理由）。"""
    fn = os.path.basename(path)
    info = {"file": fn, "site": None, "kind": None, "ok": False, "note": "",
            "mtime": os.path.getmtime(path)}
    site = site_of(fn, sites)
    if not site:
        info["note"] = "サイト略称を判定できません（ファイル名の先頭を CO／UN／SCH／MED／MED-EC／IS にしてください）"
        return info
    info["site"] = site
    info["kind"] = "ai" if re.search(r"generative[-_ ]?ai", fn, re.I) else "search"
    try:
        book = read_xlsx(path)
    except Exception as e:
        info["note"] = f"Excel として読めません（{e}）"
        return info

    # 日別：最初のシート（GSC の書き出しでは「…チャート」）。日付列を持つシートを探す
    daily = []
    for name, rows in book.items():
        tbl = _table(rows)
        if tbl and "日付" in tbl[0]:
            for r in tbl:
                d = _to_date(r.get("日付"))
                if not d:
                    continue
                daily.append([d.isoformat(), _num(r.get("クリック数")), _num(r.get("表示回数")),
                              _num(r.get("掲載順位"))])
            break
    daily.sort()

    # 期間：フィルタシート → 読めなければ日別の最初と最後
    start = end = None
    for r in _table(book.get("フィルタ", [])):
        if str(_pick(r, "フィルタ") or "").strip() == "日付":
            m = re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", str(_pick(r, "値") or ""))
            if len(m) == 2:
                start, end = _to_date(m[0]), _to_date(m[1])
    if not (start and end) and daily:
        start, end = _to_date(daily[0][0]), _to_date(daily[-1][0])
        info["note"] = "フィルタシートの日付を読めないため、日別データの最初と最後の日を期間にしました"
    if not (start and end):
        info["note"] = "期間（フィルタシートの日付）を読めません"
        return info
    key, label, monthly = _period_key(start, end)
    info.update(start=start.isoformat(), end=end.isoformat(), period=key, period_label=label, monthly=monthly)

    queries = []
    for r in _table(book.get("クエリ", [])):
        q = _pick(r, "上位のクエリ", "クエリ")
        if q in (None, ""):
            continue
        queries.append({"q": str(q), "n": norm_query(q), "clk": _num(r.get("クリック数")),
                        "imp": _num(r.get("表示回数")), "pos": _num(r.get("掲載順位"))})
    pages = []
    for r in _table(book.get("ページ", [])):
        u = _pick(r, "上位のページ", "ページ")
        if u in (None, ""):
            continue
        pages.append([str(u), _num(r.get("表示回数")) or 0])
    info.update(daily=daily, queries=queries, pages=pages, ok=True)
    if info["kind"] == "search" and not queries:
        info["note"] = (info["note"] + "／" if info["note"] else "") + "クエリシートが空です"
    if info["kind"] == "search" and len(queries) >= 1000:
        info["note"] = (info["note"] + "／" if info["note"] else "") + \
            "クエリは上位1,000件まで（GSC の書き出し上限）。これ以外は「データなし」になります"
    if not monthly:
        info["note"] = (info["note"] + "／" if info["note"] else "") + \
            "月単位の期間ではないため「期間集計（参考）」として扱います。毎月は前月1日〜末日で書き出してください"
    return info


# ────────────────────────────────────────────────────────────────
# 設問マスタ（monitoring/config/google_keywords.csv）
# ────────────────────────────────────────────────────────────────
KW_COLS = {"設問ID": "qid", "観測キーワード": "obs", "意図のズレ(設問→観測KW)": "gap",
           "GSC参照クエリ": "ref", "参照クエリとの差": "refdiff",
           # 参照用の列（Claude の結果にまだ無い設問の表示に使う）
           "設問（参照用）": "q", "セット（参照用）": "set", "領域（参照用）": "domain_label"}


def load_keywords(path):
    """{qid: {obs, gap, ref, refdiff}}, warnings"""
    if not path:
        return {}, ["google_keywords_csv が未設定です（Google参考値タブの設問キーワードは表示されません）"]
    if not os.path.isfile(path):
        return {}, [f"設問キーワード（google_keywords.csv）が見つかりません: {path}"]
    out = {}
    # Excel で「CSV（コンマ区切り）」保存すると Shift-JIS（cp932）になるため、読めなければそちらで読む
    raw = open(path, "rb").read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp932", errors="replace")
    with io.StringIO(text, newline="") as fh:
        for r in csv.DictReader(fh):
            row = {v: (r.get(k) or "").strip() for k, v in KW_COLS.items()}
            row["refn"] = norm_query(row["ref"])
            if row["qid"]:
                out[row["qid"]] = row
    return out, []


# ────────────────────────────────────────────────────────────────
# まとめ：Google参考値タブに埋め込む payload
# ────────────────────────────────────────────────────────────────
def input_files(gsc_dir):
    """自動更新の変更判定用（generate.py の _input_files から使う）。"""
    if not gsc_dir or not os.path.isdir(gsc_dir):
        return []
    return [p for p in glob.glob(os.path.join(gsc_dir, "*.xlsx"))
            if re.search(r"GSC", os.path.basename(p), re.I) and not os.path.basename(p).startswith("~$")]


def build(gsc_dir, keywords_csv, sites=None):
    sites = sites or GSC_SITES
    warnings = []
    keywords, kw_warn = load_keywords(keywords_csv)
    warnings += kw_warn

    files = input_files(gsc_dir)
    if not gsc_dir:
        warnings.append("gsc_dir が未設定です（GSC データなし）")
    elif not os.path.isdir(gsc_dir):
        warnings.append(f"GSC フォルダが見つかりません: {gsc_dir}")

    parsed = [parse_file(p, sites) for p in sorted(files)]
    log = []
    chosen = {}
    for f in parsed:
        if not f["ok"]:
            log.append({"file": f["file"], "site": f["site"] or "", "kind": f["kind"] or "",
                        "period": "", "status": "取り込みなし", "note": f["note"]})
            continue
        k = (f["site"], f["kind"], f["period"])
        if k in chosen and chosen[k]["mtime"] >= f["mtime"]:
            log.append({"file": f["file"], "site": f["site"], "kind": f["kind"], "period": f["period_label"],
                        "status": "重複（不採用）", "note": f"同じサイト・期間の新しいファイル {chosen[k]['file']} を採用"})
            continue
        if k in chosen:
            old = chosen[k]
            log.append({"file": old["file"], "site": old["site"], "kind": old["kind"], "period": old["period_label"],
                        "status": "重複（不採用）", "note": f"同じサイト・期間の新しいファイル {f['file']} を採用"})
        chosen[k] = f
    for f in chosen.values():
        log.append({"file": f["file"], "site": f["site"], "kind": f["kind"], "period": f["period_label"],
                    "status": "取り込み", "note": f["note"]})
    log.sort(key=lambda x: (x["status"] != "取り込み", x["site"], x["kind"], x["file"]))

    periods = {}
    for f in chosen.values():
        p = periods.setdefault(f["period"], {"key": f["period"], "label": f["period_label"], "start": f["start"],
                                             "end": f["end"], "monthly": f["monthly"], "sites": []})
        if f["kind"] == "search" and f["site"] not in p["sites"]:
            p["sites"].append(f["site"])
    # 並び：月次（新しい順）→ 期間集計（新しい順）
    period_list = sorted(periods.values(), key=lambda p: (not p["monthly"], p["end"]), reverse=False)
    period_list = ([p for p in sorted(period_list, key=lambda p: p["end"], reverse=True) if p["monthly"]]
                   + [p for p in sorted(period_list, key=lambda p: p["end"], reverse=True) if not p["monthly"]])
    for p in period_list:
        p["sites"].sort(key=lambda s: list(sites).index(s) if s in sites else 99)

    # GSC参照クエリ（設問マスタに載っている語）だけを、期間×サイトの値として埋め込む
    wanted = {norm_query(k["ref"]) for k in keywords.values() if k.get("ref")}
    ref = {}
    for f in chosen.values():
        if f["kind"] != "search":
            continue
        for q in f["queries"]:
            if q["n"] in wanted:
                ref.setdefault(q["n"], {}).setdefault(f["period"], {})[f["site"]] = \
                    [q["clk"], q["imp"], q["pos"], q["q"]]

    # 日別（サイト×種類）：ファイルをまたいで日付で重複除去（新しいファイル優先）
    daily = {}
    for f in sorted(chosen.values(), key=lambda f: f["mtime"]):
        dd = daily.setdefault(f["site"], {}).setdefault(f["kind"], {})
        for d in f["daily"]:
            dd[d[0]] = d[1:]
    daily = {s: {k: [[d] + v for d, v in sorted(x.items())] for k, x in kinds.items()}
             for s, kinds in daily.items()}

    ai_pages = {}
    for f in chosen.values():
        if f["kind"] == "ai":
            top = sorted(f["pages"], key=lambda x: -x[1])[:AI_PAGES_TOP]
            ai_pages.setdefault(f["period"], {})[f["site"]] = top

    monthly_n = sum(1 for p in period_list if p["monthly"])
    if files and not monthly_n:
        warnings.append("月単位（前月1日〜末日）の GSC ファイルがまだありません。いまは期間集計（参考）で表示しています")

    return {
        "available": bool(chosen),
        "dir": gsc_dir or "",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sites": {k: v for k, v in sites.items()},
        "periods": period_list,
        "keywords": keywords,
        "ref": ref,
        "daily": daily,
        "ai_pages": ai_pages,
        "log": log,
        "warnings": warnings,
    }


# ────────────────────────────────────────────────────────────────
# Google 検索チェック（monitoring の GUI＋ブックマークレットで毎月記録した CSV）
# ────────────────────────────────────────────────────────────────
def check_files(check_dir):
    if not check_dir or not os.path.isdir(check_dir):
        return []
    return sorted(glob.glob(os.path.join(check_dir, "google_check_*.csv")))


def _rank(v):
    v = str(v or "").strip()
    return int(v) if v.isdigit() else None


def _host(s):
    s = str(s or "").strip().split("›")[0].strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("/")[0].split("?")[0].split(":")[0].lower().strip(" .")
    return s[4:] if s.startswith("www.") else s


def load_domain_dict(path):
    """monitoring/config/google_competitor_domains.json → {"comp":{ドメイン:会社名}, "media":{ドメイン:媒体名}}"""
    out = {"comp": {}, "media": {}}
    try:
        d = json.loads(open(path, encoding="utf-8").read())
    except Exception:
        return out
    for c in d.get("competitors", []):
        for dom in c.get("domains", []):
            out["comp"][_host(dom)] = c.get("canonical", dom)
    for c in d.get("media", []):
        for dom in c.get("domains", []):
            out["media"][_host(dom)] = c.get("name", dom)
    return out


def classify(host, own, group, dd):
    """(種類, 名前)。種類＝own（自社）／group（グループ）／comp（競合）／media（媒体）／other"""
    h = _host(host)
    hit = lambda doms: next((d for d in doms if h == d or h.endswith("." + d)), None)
    if hit(own):
        return "own", h
    if hit(group):
        return "group", h
    d = hit(dd["comp"])
    if d:
        return "comp", dd["comp"][d]
    d = hit(dd["media"])
    if d:
        return "media", dd["media"][d]
    return "other", h


def load_checks(check_dir, keywords, own=(), group=(), domain_dict=None):
    """{"months":[新しい順], "by_month":{月:{設問ID:{"kw":記録, "q":記録}}}, "stats":{月:{...}}}
    記録 = {rank(int|None), out(圏外), own_url, aio, aio_own, aio_group, aio_hosts[], comp[], group_rank,
            top[[順位,ドメイン,タイトル]], at, method, term}
    競合は記録時の判定ではなく、上位10件のドメインと今の辞書から判定し直す（辞書の追加がすぐ反映されるように）。"""
    own = [_host(x) for x in own if x]
    group = [_host(x) for x in group if x]
    dd = domain_dict or {"comp": {}, "media": {}}
    by_month, stats = {}, {}
    for path in check_files(check_dir):
        month = os.path.basename(path)[len("google_check_"):-len(".csv")]
        raw = open(path, "rb").read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp932", errors="replace")
        m = by_month.setdefault(month, {})
        st = stats.setdefault(month, {"done": 0, "aio": 0, "aio_own": 0, "aio_group": 0, "group_top": 0,
                                      "aio_hosts": {}})
        for r in csv.DictReader(io.StringIO(text, newline="")):
            term = (r.get("検索語") or "").strip()
            if not term:
                continue
            try:
                top = json.loads(r.get("上位10件") or "[]")
            except ValueError:
                top = []
            rank = _rank(r.get("自社最高順位"))
            aio_hosts = [h for h in (r.get("AIO引用元") or "").split(";") if h]
            comp, group_rank = [], None
            for t in top[:10]:
                kind, name = classify(t[1], own, group, dd)
                if kind == "comp" and name not in comp:
                    comp.append(name)
                if kind == "group" and group_rank is None:
                    group_rank = t[0]
            if not top:        # 手入力の記録は、記録時の競合をそのまま使う
                comp = [c for c in (r.get("競合（上位10件内）") or "").split(";") if c]
            rec = {"term": term, "rank": rank, "out": rank is None,
                   "own_url": (r.get("自社URL") or "").strip(),
                   "aio": (r.get("AI Overview") or "") == "あり",
                   "aio_own": (r.get("AIO自社引用") or "") == "あり",
                   "aio_group": any(classify(h, own, group, dd)[0] == "group" for h in aio_hosts),
                   "aio_hosts": aio_hosts, "comp": comp, "group_rank": group_rank,
                   "top": top[:10], "at": (r.get("観測日時") or "").strip(),
                   "method": (r.get("記録方法") or "").strip()}
            st["done"] += 1
            if rec["aio"]:
                st["aio"] += 1
                for h in aio_hosts:
                    st["aio_hosts"][_host(h)] = st["aio_hosts"].get(_host(h), 0) + 1
            st["aio_own"] += rec["aio_own"]
            st["aio_group"] += rec["aio_group"]
            st["group_top"] += group_rank is not None
            n = norm_query(term)
            for qid in [x for x in (r.get("設問ID") or "").split(";") if x]:
                slot = "kw" if n == norm_query((keywords.get(qid) or {}).get("obs", "")) else "q"
                m.setdefault(qid, {})[slot] = rec
    for st in stats.values():
        st["aio_hosts"] = [[h, n] + list(classify(h, own, group, dd))
                           for h, n in sorted(st["aio_hosts"].items(), key=lambda x: -x[1])[:40]]
    return {"months": sorted(by_month, reverse=True), "by_month": by_month, "stats": stats,
            "dir": check_dir or "", "group_domains": group}


# ────────────────────────────────────────────────────────────────
# 施策管理表（Box に置く Excel。関係者が「担当者・状態・実施内容」を記入する）
# ────────────────────────────────────────────────────────────────
ACTION_SHEET = "施策管理"
ACTION_COLS = {"設問ID": "qid", "担当（事業）": "owner", "担当者": "person", "状態": "status",
               "期限": "due", "実施日": "done", "実施内容": "content", "対象ページURL": "url", "メモ": "memo"}
ACTION_STATUS = ["未着手", "対応中", "完了", "見送り"]


def _cell_text(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return str(v).strip()


def _cell_date(v):
    d = _to_date(v) if v not in (None, "") else None
    return d.isoformat() if d else _cell_text(v)


def load_actions(path):
    """{"file", "mtime", "rows":[...], "by_q":{設問ID:[行...]}, "warnings":[]}。
    1設問に複数行（施策の履歴）を書いてよい。設問ID が空の行と、担当者・状態・実施内容がすべて空の行は無視。"""
    out = {"file": path or "", "mtime": "", "rows": [], "by_q": {}, "owner": {}, "warnings": []}
    if not path:
        return out
    if not os.path.isfile(path):
        out["warnings"].append(f"施策管理表が見つかりません: {path}")
        return out
    out["mtime"] = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
    try:
        book = read_xlsx(path)
    except Exception as e:
        out["warnings"].append(f"施策管理表を読めません（Excel で開いたまま保存中の可能性）：{e}")
        return out
    rows = book.get(ACTION_SHEET) or next(iter(book.values()), [])
    # 見出し行（「設問ID」を含む行）を探す（上に説明行があってもよい）
    hi = next((i for i, r in enumerate(rows[:10]) if any(_cell_text(c) == "設問ID" for c in r)), None)
    if hi is None:
        out["warnings"].append("施策管理表に「設問ID」の見出しがありません")
        return out
    head = [_cell_text(c) for c in rows[hi]]
    idx = {v: head.index(k) for k, v in ACTION_COLS.items() if k in head}
    for n, r in enumerate(rows[hi + 1:], start=hi + 2):
        get = lambda key: r[idx[key]] if key in idx and idx[key] < len(r) else None
        row = {"row": n, "qid": _cell_text(get("qid")), "owner": _cell_text(get("owner")),
               "person": _cell_text(get("person")), "status": _cell_text(get("status")),
               "due": _cell_date(get("due")), "done": _cell_date(get("done")),
               "content": _cell_text(get("content")), "url": _cell_text(get("url")), "memo": _cell_text(get("memo"))}
        if not row["qid"]:
            continue
        if row["status"] and row["status"] not in ACTION_STATUS:
            out["warnings"].append(f"施策管理表 {n}行目：状態「{row['status']}」は {'／'.join(ACTION_STATUS)} のどれかにしてください")
        if row["owner"]:
            out["owner"][row["qid"]] = row["owner"]      # 担当（事業）だけの行も、担当の上書きには使う
        if not any(row[k] for k in ("person", "status", "due", "done", "content", "url", "memo")):
            continue
        out["rows"].append(row)
        out["by_q"].setdefault(row["qid"], []).append(row)
    return out
