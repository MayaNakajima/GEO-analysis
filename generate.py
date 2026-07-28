#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GEO 定点観測 分析アプリ ── 生成スクリプト
────────────────────────────────────────────────────────────────
役割：定点観測アプリ（monitoring）が出力した row データ（results_*.csv）を
      読み込み、「なぜ出ないのか・何を直すか」を掘り下げるための
      自己完結型 HTML ダッシュボードを 1 枚生成する。

内蔵ダッシュボード（monitoring/src/dashboard.py）との役割分担：
  ・内蔵  = 出現率・推移・特異度カーブ・ブレ（今どうなっているか）
  ・本アプリ = 回答全文の内容分析・競合共起・突合（なぜ・何を直すか）

実装分析（改善案 §3-2 の優先度順）：
  P1  競合共起分析          … 当社が出ない回答で代わりに挙がる競合名を抽出・ランキング
  P1  多軸クロス集計+ドリルダウン … ドメイン×ティア×タイプ×… の出現率、セルクリックで回答全文へ
  P2  引用元URL分析          … ヒット回答の urls_found を集計（非グラウンディング時は空表示）
  P2  自社サイト突合          … reference.md の実績 × AI回答の言及有無 →「掲載あるが未言及」
  P3  競合サイト突合          … （将来）AI回答に出た競合サイトのみ取得し被引用ギャップ比較

依存：Python 標準ライブラリのみ。出力 HTML の外部依存は CDN の Chart.js のみ。
使い方：
    python generate.py                     # config.json の設定で生成
    python generate.py --results-dir <dir> --reference <md> --out <html>
"""

import argparse
import csv
import glob
import html
import json
import os
import re
import sys
from datetime import datetime

# ────────────────────────────────────────────────────────────────
# 設定
# ────────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "config.json")

# 当社・自社ブランド（競合抽出から除外する）
OWN_NAMES = [
    "オンワードコーポレートデザイン", "オンワードコーポレート", "オンワードcd",
    "オンワード樫山", "オンワードグループ", "オンワード", "onward",
    "raffiria", "ラフィーリア",
]

# 競合抽出の除外：一般語・非企業名の断片（部分一致で除外）
GENERIC_STOP = [
    "実績", "ポイント", "注意", "体制", "機能性", "機能", "量産", "メンテ",
    "サンプル", "ロット", "選定", "確認", "依頼", "基準", "テスト", "コラボ",
    "シリーズ", "など", "以下", "主要", "大手", "専門", "業界", "向け",
    "製作", "製造", "調達", "発注", "対応", "導入", "事例", "比較", "特徴",
    "おすすめ", "検討", "相談", "見積", "納期", "価格", "費用", "品質",
    "デザイン", "サポート", "アフター", "カスタム", "オーダー", "まとめ",
    "とは", "について", "できます", "ください", "ましょう", "です", "ます",
    "強み", "特長", "観点", "手順", "流れ", "方法", "種類", "一覧", "注目",
]

# reference.md から抽出する自社実績の顧客名（AI回答内では「顧客／エンドクライアント」
# として登場するため、競合ではない＝競合抽出のストップリストにも使う）
# ※ generate 時に reference.md からも自動抽出して合流させる
CUSTOMER_HINTS = [
    "ANA", "JAL", "全日本空輸", "日本航空", "JR東日本", "JR東海", "JR-PLUS",
    "JR各社", "東京メトロ", "ヤマト運輸", "佐川急便", "日本郵便", "エアージャパン",
    "アルビオン", "セントラル警備保障", "DHC", "ファンケル", "NewDays",
    "関電ファシリティーズ", "KOKUYO", "三菱電機", "SBSホールディングス",
    "スタジオアリス", "大成建設", "多摩信用金庫", "TDK", "東横イン", "東横INN",
    "ヤクルト", "クラシアン", "フジテック", "TOHOシネマズ", "横河ブリッジ",
    "NTT東日本", "デニーズ", "ミライト・ワン", "ブリヂストン", "キヤノン",
    "住友ゴム", "DUNLOP", "フェリシモ", "フコク生命", "読売ジャイアンツ",
    "メナード", "物語コーポレーション", "日本生命", "住友生命", "サントリー",
    "阪神タイガース", "トヨタ", "タリーズ", "ヤマハ音楽振興会", "横浜ゴム",
    "東邦薬品", "アサヒ",
]

TIER_LABELS = {
    "D1": "指名", "D2": "業種特化・実績", "D3": "高特異度・非指名", "D4": "一般需要",
    "": "（Set1・未分類）",
}


# ────────────────────────────────────────────────────────────────
# 読み込み
# ────────────────────────────────────────────────────────────────
def load_config(path):
    cfg = {
        "results_dir": "",
        "reference_md": "",
        "output_html": os.path.join(HERE, "analysis.html"),
    }
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def load_rows(results_dir):
    files = sorted(glob.glob(os.path.join(results_dir, "results_*.csv")))
    if not files:
        raise SystemExit(f"[ERROR] results_*.csv が見つかりません: {results_dir}")
    rows = []
    files_meta = []
    for fp in files:
        fn = os.path.basename(fp)
        with open(fp, encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            n = 0
            first_set = ""
            for r in reader:
                # 旧スキーマ（07-16）には question_set / specificity_tier が無い
                qset = (r.get("question_set") or "").strip() or "set1"
                tier = (r.get("specificity_tier") or "").strip()
                hit = str(r.get("mention_detected", "")).strip().lower() == "true"
                row = {
                    "file": fn,
                    "run_date": (r.get("run_date") or "").strip(),
                    "run_ts": (r.get("run_timestamp") or "").strip(),
                    "qid": (r.get("question_id") or "").strip(),
                    "domain": (r.get("axis_domain") or "").strip(),
                    "domain_label": (r.get("domain_label") or "").strip(),
                    "type": (r.get("axis_type") or "").strip(),
                    "type_label": (r.get("type_label") or "").strip(),
                    "stakeholder": (r.get("axis_stakeholder") or "").strip(),
                    "stakeholder_label": (r.get("stakeholder_label") or "").strip(),
                    "abm": (r.get("abm_relevant") or "").strip(),
                    "model": (r.get("model_name") or "").strip(),
                    "question": (r.get("question") or "").strip(),
                    "answer": (r.get("answer") or "").strip(),
                    "hit": hit,
                    "position": (r.get("mention_position") or "").strip(),
                    "entities": (r.get("entities_found") or "").strip(),
                    "urls": (r.get("urls_found") or "").strip(),
                    "set": qset,
                    "tier": tier,
                }
                row["competitors"] = extract_competitors(row["answer"]) if not hit else []
                rows.append(row)
                n += 1
                if not first_set:
                    first_set = qset
        files_meta.append({"name": fn, "rows": n})
    return rows, files_meta


def load_reference(md_path):
    """reference.md を軽くパースし、ドメイン別の掲載顧客・事例・URLを抽出する。"""
    ref = {"domains": {}, "customers": [], "raw_available": bool(md_path and os.path.exists(md_path))}
    if not ref["raw_available"]:
        return ref
    with open(md_path, encoding="utf-8") as f:
        text = f.read()
    # 見出し「## C1：オリジナルユニフォーム（...）」単位に分割
    blocks = re.split(r'\n(?=##\s*C\d)', text)
    for b in blocks:
        m = re.match(r'##\s*(C[\d\-A-Za-z]+)\s*[：:]\s*([^（(\n]+)', b)
        if not m:
            continue
        code = m.group(1).strip()
        label = m.group(2).strip()
        # 「掲載顧客」「実績」「事例」行から企業名らしきものを抽出
        names = set()
        for line in b.splitlines():
            if any(k in line for k in ["掲載顧客", "実績企業", "掲載校", "導入実績",
                                       "事例", "掲載病院", "エピソード", "掲載", "実績"]):
                # 太字と、読点/中点区切りの固有名詞を拾う
                for mm in re.findall(r'\*\*(.+?)\*\*', line):
                    names.add(mm.strip())
                tail = re.sub(r'^.*?[:：]', '', line)
                for part in re.split(r'[、,／/]', tail):
                    p = part.strip(" 　-*。.")
                    if 2 <= len(p) <= 18 and not any(g in p for g in ["強み", "傾向", "実績記事", "掲載"]):
                        names.add(p)
        urls = re.findall(r'https?://[^\s）)]+', b)
        clean = sorted({n for n in names if n and not n.startswith("「")})
        ref["domains"][code] = {"label": label, "names": clean, "urls": urls}
        ref["customers"].extend(clean)
    ref["customers"] = sorted(set(ref["customers"]))
    return ref


# ────────────────────────────────────────────────────────────────
# 競合共起抽出（ヒューリスティック・透明性重視）
# ────────────────────────────────────────────────────────────────
_num_prefix = re.compile(r'^[0-9０-９]+\s*[\.\．、)）]\s*')
_bold = re.compile(r'\*\*(.+?)\*\*')


def _norm_key(name):
    n = _num_prefix.sub("", name).strip(" 　*・")
    core = re.split(r'[（(]', n)[0].strip()
    core = re.sub(r'(株式会社|有限会社|合同会社|㈱|\(株\))', '', core).strip(" 　・")
    return core or n


def _is_company_like(raw):
    """太字スパンが企業名候補かどうかの判定。"""
    n = _num_prefix.sub("", raw).strip(" 　*・")
    if not n:
        return False
    core = _norm_key(n)
    if not (2 <= len(core) <= 24):
        return False
    # 記号・句読点を含むものは説明句として除外
    if re.search(r'[。、！？：:「」【】＝=…／/\n\t]', n):
        return False
    low = core.lower()
    if any(o in low for o in [o.lower() for o in OWN_NAMES]):
        return False
    if any(g in core for g in GENERIC_STOP):
        return False
    # 企業サフィックスがあれば強く企業とみなす
    if re.search(r'(株式会社|有限会社|ホールディングス|HD|工業|製作所|商会|商事|産業)', n):
        return True
    # それ以外は「単一トークンの固有名詞（漢字/カナ/英字中心）」であることを要件に
    if re.search(r'\s', core):
        return False
    if not re.search(r'[一-龥ぁ-んァ-ヴA-Za-z]', core):
        return False
    return True


def extract_competitors(answer):
    """回答全文から競合候補名を抽出。太字スパンを主対象に、企業らしさで絞る。"""
    if not answer:
        return []
    found = []
    seen = set()
    for raw in _bold.findall(answer):
        if _is_company_like(raw):
            key = _norm_key(raw)
            if key not in seen:
                seen.add(key)
                found.append(key)
    return found


# ────────────────────────────────────────────────────────────────
# データ束ね
# ────────────────────────────────────────────────────────────────
def build_payload(rows, files_meta, ref, cfg):
    # 顧客ストップリスト（reference + ハードコード）を competitors から後処理除外
    customer_stop = set(CUSTOMER_HINTS) | set(ref.get("customers", []))
    cust_norm = {_norm_key(c) for c in customer_stop}
    for r in rows:
        r["competitors"] = [c for c in r["competitors"] if c not in cust_norm]

    dates = sorted({r["run_date"] for r in rows if r["run_date"]})
    domains = sorted({r["domain"] for r in rows if r["domain"]})
    domain_labels = {r["domain"]: r["domain_label"] for r in rows if r["domain"]}
    types = sorted({r["type"] for r in rows if r["type"]})
    type_labels = {r["type"]: r["type_label"] for r in rows if r["type"]}
    stakeholders = sorted({r["stakeholder"] for r in rows if r["stakeholder"]})
    stakeholder_labels = {r["stakeholder"]: r["stakeholder_label"] for r in rows if r["stakeholder"]}
    models = sorted({r["model"] for r in rows if r["model"]})
    sets = sorted({r["set"] for r in rows if r["set"]})
    tiers = [t for t in ["D1", "D2", "D3", "D4"] if any(r["tier"] == t for r in rows)]

    payload = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "results_dir": cfg.get("results_dir", ""),
            "files": files_meta,
            "total_rows": len(rows),
            "own_names": OWN_NAMES,
            "grounding_note": ("稼働モデルは Claude 単体＝非グラウンディング。"
                               "測定対象は「AIの学習記憶に当社があるか」。"
                               "検索引用（urls_found）の測定は Perplexity 等のキー許可後。"),
        },
        "dims": {
            "dates": dates,
            "domains": domains, "domain_labels": domain_labels,
            "types": types, "type_labels": type_labels,
            "stakeholders": stakeholders, "stakeholder_labels": stakeholder_labels,
            "models": models, "sets": sets, "tiers": tiers,
            "tier_labels": TIER_LABELS,
        },
        "rows": [
            {
                "i": idx,
                "file": r["file"], "date": r["run_date"], "ts": r["run_ts"],
                "qid": r["qid"], "domain": r["domain"], "domain_label": r["domain_label"],
                "type": r["type"], "type_label": r["type_label"],
                "stakeholder": r["stakeholder"], "stakeholder_label": r["stakeholder_label"],
                "model": r["model"], "set": r["set"], "tier": r["tier"],
                "hit": r["hit"], "question": r["question"], "answer": r["answer"],
                "entities": r["entities"], "urls": r["urls"], "comp": r["competitors"],
            }
            for idx, r in enumerate(rows)
        ],
        "reference": ref,
    }
    return payload


# ────────────────────────────────────────────────────────────────
# HTML 生成
# ────────────────────────────────────────────────────────────────
def render_html(payload):
    data_json = json.dumps(payload, ensure_ascii=False)
    # インライン <script> 内に安全に埋め込むため、タグ境界・JS行区切りになり得る文字をエスケープ
    data_json = (data_json.replace('<', '\\u003c')
                          .replace('>', '\\u003e')
                          .replace('\u2028', '\\u2028')
                          .replace('\u2029', '\\u2029'))
    return HTML_TEMPLATE.replace('/*__DATA__*/', data_json)


def main():
    ap = argparse.ArgumentParser(description="GEO 定点観測 分析アプリ HTML 生成")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--results-dir", default=None)
    ap.add_argument("--reference", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.results_dir:
        cfg["results_dir"] = args.results_dir
    if args.reference:
        cfg["reference_md"] = args.reference
    if args.out:
        cfg["output_html"] = args.out

    if not cfg.get("results_dir"):
        raise SystemExit("[ERROR] results_dir 未設定。config.json か --results-dir で指定してください。")

    rows, files_meta = load_rows(cfg["results_dir"])
    ref = load_reference(cfg.get("reference_md", ""))
    payload = build_payload(rows, files_meta, ref, cfg)
    html_text = render_html(payload)

    out = cfg["output_html"]
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_text)

    hits = sum(1 for r in rows if r["hit"])
    print(f"[OK] {out}")
    print(f"     rows={len(rows)}  hits={hits}  files={len(files_meta)}")
    print(f"     competitors extracted (miss rows): "
          f"{sum(len(r['competitors']) for r in rows)} mentions")


# ────────────────────────────────────────────────────────────────
# HTML テンプレート（自己完結／外部依存は Chart.js CDN のみ）
# ────────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GEO 定点観測 分析アプリ ｜ 回答全文の深掘り・突合</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--ink:#e2e8f0;--sub:#94a3b8;--accent:#38bdf8;
        --good:#34d399;--warn:#fbbf24;--bad:#f87171;--line:#334155;--panel:#0b1220;}
  *{box-sizing:border-box;}
  body{margin:0;font-family:"Segoe UI","Hiragino Kaku Gothic ProN",Meiryo,sans-serif;
       background:var(--bg);color:var(--ink);}
  header{padding:20px 28px;border-bottom:1px solid var(--line);}
  header h1{margin:0;font-size:20px;}
  header .sub{color:var(--sub);font-size:13px;margin-top:4px;}
  main{padding:0 28px 60px;}
  .tabs{display:flex;gap:6px;margin:16px 0 0;flex-wrap:wrap;border-bottom:1px solid var(--line);}
  .tab{padding:9px 16px;border-radius:8px 8px 0 0;background:transparent;color:var(--sub);
       cursor:pointer;border:1px solid transparent;font-size:14px;}
  .tab.active{background:var(--card);color:var(--ink);border-color:var(--line);border-bottom:none;}
  section{display:none;padding-top:18px;}
  section.active{display:block;}
  .summary{margin:4px 0 16px;padding:14px 18px;border-radius:12px;font-size:15px;line-height:1.7;
           background:var(--panel);border:1px solid var(--line);border-left:5px solid var(--accent);}
  .summary b{color:var(--ink);}
  details.howto{margin:0 0 16px;background:var(--panel);border:1px solid var(--line);border-radius:10px;}
  details.howto>summary{cursor:pointer;padding:10px 14px;color:var(--accent);font-size:13px;}
  details.howto .body{padding:2px 16px 14px;color:var(--sub);font-size:13px;line-height:1.9;}
  .filters{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end;margin:6px 0 18px;
           padding:14px 16px;background:var(--card);border:1px solid var(--line);border-radius:12px;}
  .filters .f{display:flex;flex-direction:column;gap:4px;}
  .filters label{color:var(--sub);font-size:11px;}
  select,button{background:var(--panel);color:var(--ink);border:1px solid var(--line);
         border-radius:8px;padding:7px 10px;font-size:13px;}
  button{cursor:pointer;}
  button.reset{border-color:var(--accent);color:var(--accent);}
  .grid{display:grid;gap:16px;}
  .g2{grid-template-columns:1fr 1fr;}
  @media(max-width:960px){.g2{grid-template-columns:1fr;}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;}
  h2{font-size:15px;margin:22px 0 10px;border-left:3px solid var(--accent);padding-left:8px;}
  h3{font-size:14px;margin:0 0 10px;color:var(--ink);}
  table{width:100%;border-collapse:collapse;font-size:13px;}
  th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top;}
  th{color:var(--sub);font-weight:600;position:sticky;top:0;background:var(--card);}
  tr.click{cursor:pointer;} tr.click:hover{background:var(--panel);}
  .muted{color:var(--sub);font-size:12px;} .kpi{font-size:28px;font-weight:700;}
  .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;
        background:var(--panel);color:var(--sub);border:1px solid var(--line);margin:1px;}
  .pill.bad{color:var(--bad);border-color:var(--bad);}
  .pill.good{color:var(--good);border-color:var(--good);}
  .bar-wrap{max-height:420px;overflow:auto;}
  .xtab{overflow:auto;} .xtab td.rate{text-align:right;font-variant-numeric:tabular-nums;}
  .cell{cursor:pointer;border-radius:6px;padding:6px 8px;text-align:center;min-width:60px;}
  .note{border-left:4px solid var(--warn);background:var(--panel);padding:12px 16px;border-radius:8px;
        margin:14px 0;font-size:13px;line-height:1.8;color:var(--sub);}
  .note b{color:var(--ink);}
  /* modal */
  .modal{position:fixed;inset:0;background:rgba(2,6,23,.72);display:none;z-index:50;
         align-items:flex-start;justify-content:center;padding:40px 16px;overflow:auto;}
  .modal.open{display:flex;}
  .modal .box{background:var(--card);border:1px solid var(--line);border-radius:14px;
       max-width:900px;width:100%;padding:22px 24px;}
  .modal .close{float:right;cursor:pointer;color:var(--sub);border:1px solid var(--line);
       border-radius:8px;padding:4px 10px;background:var(--panel);}
  .modal h3{margin:0 0 6px;} .modal .meta{color:var(--sub);font-size:12px;margin-bottom:12px;}
  .answer{white-space:pre-wrap;line-height:1.75;font-size:13.5px;background:var(--panel);
          border:1px solid var(--line);border-radius:10px;padding:14px 16px;max-height:52vh;overflow:auto;}
  .tagH{color:var(--good);font-weight:700;} .tagM{color:var(--bad);font-weight:700;}
  .checklist td .st-yes{color:var(--good);} .checklist td .st-no{color:var(--bad);}
</style>
</head>
<body>
<header>
  <h1>GEO 定点観測 分析アプリ <span class="muted">｜ 回答全文の深掘り・突合</span></h1>
  <div class="sub" id="hdr-sub"></div>
</header>
<main>
  <div class="tabs" id="tabs"></div>

  <!-- P1: 競合共起 -->
  <section id="s-comp" class="active">
    <div class="summary" id="comp-summary"></div>
    <details class="howto"><summary>この画面の見方</summary>
      <div class="body">当社が出なかった回答（未言及）で、代わりに挙がっている企業名を抽出・ランキングします。
        「どの土俵（ドメイン・特異度ティア）で誰に負けているか」を把握し、行をクリックすると根拠となった回答全文へドリルダウンできます。
        <br>※ 抽出は回答本文の太字表記からのヒューリスティック（候補）です。自社・既知の掲載顧客は自動除外していますが、最終確認は回答全文で行ってください。</div>
    </details>
    <div class="filters" id="filters-comp"></div>
    <div class="grid g2">
      <div class="card"><h3>競合共起ランキング（未言及回答での登場社数）</h3>
        <div class="bar-wrap"><canvas id="chart-comp" height="380"></canvas></div></div>
      <div class="card"><h3>ランキング表（クリックで登場回答へ）</h3>
        <div id="comp-table"></div></div>
    </div>
    <h2>ドメイン別 × 競合（上位）</h2>
    <div class="card"><div id="comp-domain"></div></div>
  </section>

  <!-- P1: クロス集計 -->
  <section id="s-cross">
    <div class="summary" id="cross-summary"></div>
    <details class="howto"><summary>この画面の見方</summary>
      <div class="body">選んだ2軸で出現率のクロス表を作ります。セルの色が濃い＝出現率が高い。
        セルをクリックすると、その条件に該当する質問・回答全文の一覧（ドリルダウン）が開きます。
        「崖はどこか＝どの具体度から当社が消えるか」を数値で追えます。</div>
    </details>
    <div class="filters" id="filters-cross"></div>
    <div class="card xtab"><div id="crosstab"></div></div>
    <div class="muted" style="margin-top:8px">セル = 出現率（ヒット数 / 該当行数）。0% でも該当行があればクリックで回答を確認できます。</div>
  </section>

  <!-- P2: 引用URL -->
  <section id="s-url">
    <div class="summary" id="url-summary"></div>
    <details class="howto"><summary>この画面の見方</summary>
      <div class="body">ヒットした回答が引用している URL（urls_found）を集計し、効いているページを特定します。
        非グラウンディング（Claude 単体）では引用が発生しないため空になります。Perplexity 等の許可後に有効化されます。</div>
    </details>
    <div class="filters" id="filters-url"></div>
    <div class="card"><div id="url-body"></div></div>
  </section>

  <!-- P2: 自社突合 -->
  <section id="s-self">
    <div class="summary" id="self-summary"></div>
    <details class="howto"><summary>この画面の見方</summary>
      <div class="body">reference.md（自社サイトの掲載実績・顧客・事例）を初期データに、AI回答本文でその名前が言及されているかを突合します。
        「掲載はあるが AI 未言及」＝ GEO 強化余地（学習記憶に載っていない実績）です。ドメイン単位で確認できます。</div>
    </details>
    <div class="card"><div id="self-body"></div></div>
  </section>

  <!-- P3: 競合サイト突合 -->
  <section id="s-p3">
    <div class="summary">P3｜競合サイト突合 <span class="muted">（将来実装・スクレイピング要）</span></div>
    <div class="note" id="p3-body"></div>
  </section>
</main>

<!-- ドリルダウン modal -->
<div class="modal" id="modal"><div class="box">
  <span class="close" onclick="closeModal()">閉じる ✕</span>
  <div id="modal-content"></div>
</div></div>

<script>
const DATA = /*__DATA__*/;
const $ = (s,el=document)=>el.querySelector(s);
const $$ = (s,el=document)=>[...el.querySelectorAll(s)];
const esc = s => (s==null?"":String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const D = DATA.dims;
const ROWS = DATA.rows;

// ── ヘッダ
$("#hdr-sub").innerHTML =
  `生成: ${DATA.meta.generated_at} ／ 対象行: ${DATA.meta.total_rows} ／ ファイル: ${DATA.meta.files.length}件 ｜ `
  + `<span class="muted">${esc(DATA.meta.grounding_note)}</span>`;

// ── タブ
const TABS = [
  ["s-comp","競合共起 (P1)"],["s-cross","多軸クロス集計 (P1)"],
  ["s-url","引用URL (P2)"],["s-self","自社突合 (P2)"],["s-p3","競合サイト (P3)"]
];
const tabsEl = $("#tabs");
TABS.forEach(([id,label],i)=>{
  const b=document.createElement("div"); b.className="tab"+(i===0?" active":""); b.textContent=label;
  b.onclick=()=>{ $$(".tab").forEach(t=>t.classList.remove("active")); b.classList.add("active");
    $$("section").forEach(s=>s.classList.remove("active")); $("#"+id).classList.add("active");
    if(id==="s-comp") renderComp(); if(id==="s-cross") renderCross(); };
  tabsEl.appendChild(b);
});

// ── フィルタ UI（各タブに独立生成）
const FSTATE = {comp:{},cross:{},url:{}};
function optionSet(){ return {
  date:["期間",D.dates], domain:["ドメイン",D.domains],
  tier:["特異度",D.tiers], set:["質問セット",D.sets], model:["モデル",D.models]
};}
function buildFilters(mountId, key, onchange, extra=[]){
  const mount=$("#"+mountId); mount.innerHTML="";
  const opts=optionSet();
  const order = ["date","domain","tier","set","model"];
  order.forEach(k=>{
    const [label,vals]=opts[k];
    const f=document.createElement("div"); f.className="f";
    const lab=document.createElement("label"); lab.textContent=label;
    const sel=document.createElement("select"); sel.dataset.k=k;
    sel.innerHTML=`<option value="">すべて</option>`+vals.map(v=>{
      let t=v; if(k==="tier") t=`${v} ${D.tier_labels[v]||""}`;
      if(k==="domain") t=`${v}｜${esc(D.domain_labels[v]||"")}`;
      return `<option value="${esc(v)}">${esc(t)}</option>`;}).join("");
    sel.value = FSTATE[key][k]||"";
    sel.onchange=()=>{ FSTATE[key][k]=sel.value; onchange(); };
    f.appendChild(lab); f.appendChild(sel); mount.appendChild(f);
  });
  extra.forEach(node=>mount.appendChild(node));
  const rf=document.createElement("div"); rf.className="f";
  const rb=document.createElement("button"); rb.className="reset"; rb.textContent="リセット";
  rb.onclick=()=>{ FSTATE[key]={}; buildFilters(mountId,key,onchange,extra); onchange(); };
  rf.appendChild(document.createElement("label")); rf.appendChild(rb); mount.appendChild(rf);
}
function passFilter(r, st){
  if(st.date && r.date!==st.date) return false;
  if(st.domain && r.domain!==st.domain) return false;
  if(st.tier && r.tier!==st.tier) return false;
  if(st.set && r.set!==st.set) return false;
  if(st.model && r.model!==st.model) return false;
  return true;
}

// ── 出現率ヘルパ
function rate(rows){ if(!rows.length) return null; const h=rows.filter(r=>r.hit).length; return Math.round(h/rows.length*1000)/10; }
function heat(v){ if(v==null) return "var(--panel)"; const a=Math.min(v/30,1);
  return `rgba(56,189,248,${0.10+a*0.55})`; }

// ─────────────────────────────────────────── P1 競合共起
let compChart=null;
function renderComp(){
  const st=FSTATE.comp;
  const rows=ROWS.filter(r=>!r.hit && passFilter(r,st));
  // 集計：社名 -> {count, rowIdx:Set}
  const agg={};
  rows.forEach(r=>{ (r.comp||[]).forEach(name=>{
    const o=agg[name]||(agg[name]={name,count:0,rows:[]}); o.count++; o.rows.push(r.i); }); });
  let list=Object.values(agg).sort((a,b)=>b.count-a.count);
  const top=list.slice(0,20);
  // サマリー
  const missN=rows.length, compTotal=list.length;
  $("#comp-summary").innerHTML = compTotal
    ? `未言及回答 <b>${missN}</b> 件から <b>${compTotal}</b> 社の競合候補を抽出。最頻出は `
      + top.slice(0,3).map(t=>`<b>${esc(t.name)}</b>（${t.count}回）`).join("、") + `。`
      + `<span class="muted"> ＝ 当社が挙がるべき土俵でこれらが選ばれています。</span>`
    : `該当データがありません（フィルタを緩めてください）。`;
  // チャート
  if(compChart) compChart.destroy();
  compChart=new Chart($("#chart-comp"),{type:"bar",
    data:{labels:top.map(t=>t.name),
      datasets:[{label:"登場回答数",data:top.map(t=>t.count),
        backgroundColor:"rgba(56,189,248,.6)",borderColor:"#38bdf8",borderWidth:1}]},
    options:{indexAxis:"y",responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false}},
      scales:{x:{ticks:{color:"#94a3b8"},grid:{color:"#334155"}},
              y:{ticks:{color:"#e2e8f0"},grid:{display:false}}}}});
  // 表
  $("#comp-table").innerHTML =
    `<table><thead><tr><th>#</th><th>競合候補</th><th>登場回答数</th><th></th></tr></thead><tbody>`
    + top.map((t,i)=>`<tr class="click" onclick='showCompRows(${JSON.stringify(t.name)})'>
        <td>${i+1}</td><td>${esc(t.name)}</td><td>${t.count}</td>
        <td class="muted">回答を見る ›</td></tr>`).join("")
    + `</tbody></table>`;
  // ドメイン別 上位
  const byDom={};
  rows.forEach(r=>{ const d=r.domain; (r.comp||[]).forEach(n=>{
    const o=byDom[d]||(byDom[d]={}); o[n]=(o[n]||0)+1; }); });
  const domKeys=Object.keys(byDom).sort();
  $("#comp-domain").innerHTML = domKeys.length
    ? `<table><thead><tr><th>ドメイン</th><th>上位競合（登場回数）</th></tr></thead><tbody>`
      + domKeys.map(d=>{ const top3=Object.entries(byDom[d]).sort((a,b)=>b[1]-a[1]).slice(0,5);
          return `<tr><td>${esc(d)}｜${esc(D.domain_labels[d]||"")}</td>
            <td>${top3.map(([n,c])=>`<span class="pill bad">${esc(n)} ${c}</span>`).join(" ")}</td></tr>`;
        }).join("") + `</tbody></table>`
    : `<div class="muted">該当なし</div>`;
}
function showCompRows(name){
  const st=FSTATE.comp;
  const rows=ROWS.filter(r=>!r.hit && passFilter(r,st) && (r.comp||[]).includes(name));
  const body = `<h3>「${esc(name)}」が登場した未言及回答（${rows.length}件）</h3>
    <div class="meta">当社（オンワード）が出ずにこの企業が挙がった回答です。クリックで全文。</div>`
    + rows.map(r=>`<div class="card" style="margin:8px 0;cursor:pointer" onclick='showAnswer(${r.i})'>
        <b>${esc(r.qid)}</b> <span class="pill">${esc(r.domain)}${r.tier?(" / "+r.tier):""}</span>
        <span class="pill">${esc(r.date)}</span><br>
        <span class="muted">${esc(r.question)}</span></div>`).join("");
  openModal(body);
}

// ─────────────────────────────────────────── P1 クロス集計
const AXES = {domain:"ドメイン",tier:"特異度",type:"質問タイプ",stakeholder:"ステークホルダー",model:"モデル",date:"実行日",set:"質問セット"};
function axisVals(ax){
  if(ax==="domain")return D.domains; if(ax==="tier")return D.tiers;
  if(ax==="type")return D.types; if(ax==="stakeholder")return D.stakeholders;
  if(ax==="model")return D.models; if(ax==="date")return D.dates; if(ax==="set")return D.sets;
  return [];
}
function axisLabel(ax,v){
  if(ax==="domain")return `${v}｜${D.domain_labels[v]||""}`;
  if(ax==="tier")return `${v} ${D.tier_labels[v]||""}`;
  if(ax==="type")return `${v}｜${D.type_labels[v]||""}`;
  if(ax==="stakeholder")return `${v}｜${D.stakeholder_labels[v]||""}`;
  return v;
}
function renderCross(){
  const st=FSTATE.cross;
  if(!st._rax) st._rax="domain"; if(!st._cax) st._cax="tier";
  const rows=ROWS.filter(r=>passFilter(r,st));
  const rax=st._rax, cax=st._cax;
  const rv=axisVals(rax).filter(v=>rows.some(r=>r[rax]===v));
  const cv=axisVals(cax).filter(v=>rows.some(r=>r[cax]===v));
  let html=`<table><thead><tr><th>${AXES[rax]} ＼ ${AXES[cax]}</th>`
    + cv.map(c=>`<th>${esc(axisLabel(cax,c))}</th>`).join("")+`<th>行計</th></tr></thead><tbody>`;
  rv.forEach(r=>{
    html+=`<tr><td><b>${esc(axisLabel(rax,r))}</b></td>`;
    cv.forEach(c=>{
      const cell=rows.filter(x=>x[rax]===r && x[cax]===c);
      const v=rate(cell);
      html+= v==null
        ? `<td class="rate"><span class="muted">–</span></td>`
        : `<td class="rate"><div class="cell" style="background:${heat(v)}"
             onclick='drillCross(${JSON.stringify(rax)},${JSON.stringify(r)},${JSON.stringify(cax)},${JSON.stringify(c)})'>
             ${v}%<div class="muted" style="font-size:10px">${cell.filter(x=>x.hit).length}/${cell.length}</div></div></td>`;
    });
    const rowAll=rows.filter(x=>x[rax]===r); const rv2=rate(rowAll);
    html+=`<td class="rate">${rv2==null?"–":rv2+"%"}</td></tr>`;
  });
  // 列計
  html+=`<tr><td><b>列計</b></td>`+cv.map(c=>{const cc=rows.filter(x=>x[cax]===c);const v=rate(cc);
    return `<td class="rate">${v==null?"–":v+"%"}</td>`;}).join("")
    +`<td class="rate">${rate(rows)==null?"–":rate(rows)+"%"}</td></tr>`;
  html+=`</tbody></table>`;
  $("#crosstab").innerHTML=html;
  const cliffNote = describeCliff(rows);
  $("#cross-summary").innerHTML = `対象 <b>${rows.length}</b> 行・全体出現率 <b>${rate(rows)??0}%</b>。${cliffNote}`;
}
function describeCliff(rows){
  // Set2 の tier カーブから崖を説明
  const s2=rows.filter(r=>r.set==="set2");
  if(!s2.length) return "";
  const order=["D1","D2","D3","D4"]; const seg=[];
  order.forEach(t=>{const c=s2.filter(r=>r.tier===t); if(c.length) seg.push([t,rate(c)]);});
  if(!seg.length) return "";
  const curve=seg.map(([t,v])=>`${t}(${D.tier_labels[t]}) ${v}%`).join(" / ");
  const cliff=seg.find(([t,v])=>v<10 && t!=="D1");
  let msg=` Set2特異度カーブ：${curve}。`;
  if(cliff){const idx=seg.findIndex(s=>s[0]===cliff[0]);const prev=seg[idx-1][0];
    msg+=`<b>崖は ${prev}→${cliff[0]}</b>（${D.tier_labels[cliff[0]]}で ${cliff[1]}% に低下）。`;}
  return msg;
}
function drillCross(rax,rv,cax,cv){
  const st=FSTATE.cross;
  const rows=ROWS.filter(r=>passFilter(r,st) && r[rax]===rv && r[cax]===cv);
  const body=`<h3>${esc(axisLabel(rax,rv))} × ${esc(axisLabel(cax,cv))}（${rows.length}件・出現率 ${rate(rows)}%）</h3>
    <div class="meta">クリックで回答全文。<span class="tagH">緑=言及あり</span> / <span class="tagM">赤=未言及</span></div>`
    + rows.map(r=>`<div class="card" style="margin:8px 0;cursor:pointer" onclick='showAnswer(${r.i})'>
        <span class="${r.hit?'tagH':'tagM'}">${r.hit?'●言及':'×未言及'}</span>
        <b>${esc(r.qid)}</b> <span class="pill">${esc(r.date)}</span><br>
        <span class="muted">${esc(r.question)}</span>
        ${(r.comp&&r.comp.length)?`<br>競合候補: `+r.comp.slice(0,6).map(n=>`<span class="pill bad">${esc(n)}</span>`).join(""):""}
      </div>`).join("");
  openModal(body);
}

// ─────────────────────────────────────────── P2 引用URL
function renderUrl(){
  const st=FSTATE.url;
  const rows=ROWS.filter(r=>passFilter(r,st));
  const withUrl=rows.filter(r=>r.urls && r.urls.trim());
  if(!withUrl.length){
    $("#url-summary").innerHTML=`引用URL（urls_found）は <b>0件</b>。`;
    $("#url-body").innerHTML=`<div class="note"><b>現状は空です。</b> 稼働モデルが Claude 単体（非グラウンディング）のため、
      回答に引用URLが付きません。Perplexity 等の検索引用型モデルのキーが許可され、Set を再測定すると、
      ここに「効いているページ（URL別ヒット数）」が自動集計されます。</div>`;
    return;
  }
  const agg={};
  withUrl.forEach(r=>r.urls.split(/[\s,;|]+/).filter(Boolean).forEach(u=>{agg[u]=(agg[u]||0)+1;}));
  const list=Object.entries(agg).sort((a,b)=>b[1]-a[1]);
  $("#url-summary").innerHTML=`引用URLを含む回答 <b>${withUrl.length}</b> 件／ユニークURL <b>${list.length}</b> 件。`;
  $("#url-body").innerHTML=`<table><thead><tr><th>#</th><th>URL</th><th>引用回数</th></tr></thead><tbody>`
    + list.map(([u,c],i)=>`<tr><td>${i+1}</td><td>${esc(u)}</td><td>${c}</td></tr>`).join("")+`</tbody></table>`;
}

// ─────────────────────────────────────────── P2 自社突合
function renderSelf(){
  const ref=DATA.reference;
  if(!ref || !ref.raw_available){
    $("#self-summary").innerHTML=`reference.md が読み込まれていません。`;
    $("#self-body").innerHTML=`<div class="note">config.json の <b>reference_md</b> に
      <b>Set2_実績スクレイピング_reference.md</b> のパスを設定して再生成してください。</div>`;
    return;
  }
  // 全回答テキストを結合して名前照合
  const allText = ROWS.map(r=>r.answer).join("\n");
  const doms=Object.keys(ref.domains);
  let totalListed=0, totalMentioned=0;
  let html="";
  doms.forEach(code=>{
    const d=ref.domains[code];
    const names=(d.names||[]).filter(n=>n.length>=2);
    if(!names.length) return;
    const rowsHtml=names.map(n=>{
      const mentioned=allText.indexOf(n)>=0;
      totalListed++; if(mentioned) totalMentioned++;
      return `<tr><td>${esc(n)}</td>
        <td>${mentioned?'<span class="st-yes">● AI言及あり</span>':'<span class="st-no">× AI未言及</span>'}</td></tr>`;
    }).join("");
    html+=`<div class="card" style="margin:10px 0"><h3>${esc(code)}｜${esc(d.label)}
      <span class="muted">（掲載実績 ${names.length}件）</span></h3>
      <table class="checklist"><thead><tr><th>自社サイト掲載（顧客・事例）</th><th>AI回答での言及</th></tr></thead>
      <tbody>${rowsHtml}</tbody></table></div>`;
  });
  const gap=totalListed-totalMentioned;
  $("#self-summary").innerHTML=`自社サイト掲載実績 <b>${totalListed}</b>件のうち、AI回答で言及されたのは <b>${totalMentioned}</b>件。`
    +` <b>${gap}件が「掲載あるが AI 未言及」</b>＝ GEO 強化余地（AIの学習記憶に載っていない実績）。`;
  $("#self-body").innerHTML=html || `<div class="muted">reference から実績名を抽出できませんでした。</div>`;
}

// ─────────────────────────────────────────── P3
$("#p3-body").innerHTML = `<b>競合サイト突合（P3）は将来実装です。</b><br>
  設計：AI回答に実際に登場した競合サイトのみを対象に取得し、当社の被引用ギャップ（どのページが引用され、当社に無いか）を比較します。
  スクレイピングはネット接続・取得制限への配慮が必要なため、本アプリ（オフライン自己完結）とは別工程で追加します。
  <br>現状の優先度：P1（競合共起・クロス集計）→ P2（引用URL・自社突合）→ P3。まず P1/P2 で「誰に・どこで負けているか」を特定してから着手します。`;

// ─────────────────────────────────────────── modal
function openModal(html){ $("#modal-content").innerHTML=html; $("#modal").classList.add("open"); }
function closeModal(){ $("#modal").classList.remove("open"); }
$("#modal").addEventListener("click",e=>{ if(e.target.id==="modal") closeModal(); });
function showAnswer(i){
  const r=ROWS[i];
  const body=`<h3>${esc(r.qid)} <span class="${r.hit?'tagH':'tagM'}">${r.hit?'● 言及あり':'× 未言及'}</span></h3>
    <div class="meta">${esc(r.domain)}｜${esc(r.domain_label)} ／ ${r.tier?('特異度 '+r.tier+' '+(D.tier_labels[r.tier]||"")+' ／ '):''}
      ${esc(r.set)} ／ ${esc(r.model)} ／ ${esc(r.date)} ${esc(r.ts)}</div>
    <div style="margin:6px 0 10px"><b>Q.</b> ${esc(r.question)}</div>
    ${r.entities?`<div class="muted" style="margin-bottom:6px">検出エンティティ: ${esc(r.entities)}</div>`:""}
    ${(r.comp&&r.comp.length)?`<div style="margin-bottom:8px">競合候補: `+r.comp.map(n=>`<span class="pill bad">${esc(n)}</span>`).join("")+`</div>`:""}
    <div class="answer">${esc(r.answer)}</div>`;
  openModal(body);
}

// ── init
buildFilters("filters-comp","comp",renderComp);
buildFilters("filters-url","url",renderUrl);
// クロス集計は軸セレクタを追加
(function(){
  const mk=(k,def)=>{ const f=document.createElement("div"); f.className="f";
    const lab=document.createElement("label"); lab.textContent=(k==="_rax"?"行軸":"列軸");
    const sel=document.createElement("select");
    sel.innerHTML=Object.entries(AXES).map(([v,t])=>`<option value="${v}">${t}</option>`).join("");
    sel.value=def; sel.onchange=()=>{FSTATE.cross[k]=sel.value; renderCross();};
    f.appendChild(lab); f.appendChild(sel); return f; };
  const extra=[mk("_rax","domain"),mk("_cax","tier")];
  buildFilters("filters-cross","cross",renderCross,extra);
})();
renderComp(); renderCross(); renderUrl(); renderSelf();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
