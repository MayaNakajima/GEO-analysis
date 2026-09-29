#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GEO 定点観測 分析アプリ ── 生成スクリプト
────────────────────────────────────────────────────────────────
役割：定点観測アプリ（monitoring）が出力した row データ（results_*.csv）を
      読み込み、「なぜ出ないのか・何を直すか」を掘り下げるための
      自己完結型 HTML ダッシュボードを 1 枚生成する。

役割分担：
  ・本アプリ（analysis.html）= 関係者の唯一の入口。「ホーム」タブで 結論→注意→次にやること→議論ポイント、
    詳細タブで 回答全文の内容分析・競合共起・突合（なぜ・何を直すか）
  ・内蔵 dashboard / insights（monitoring）= 運用者向け。本アプリは reports/index.json・insights_*.json を読むだけ

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
    python generate.py --reports-dir <dir>  # ホームタブ用の monitoring/data/reports を上書き
    python generate.py --open              # 生成後に共有フォルダへコピーしブラウザで開く
    （ふだんは「更新して開く.bat」をダブルクリックするだけで同じことができる）
"""

import argparse
import csv
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import gsc_reader

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
# 回答コンテキスト分析（オフライン・ヒューリスティック）
# ────────────────────────────────────────────────────────────────
# 回答タイプ（1回答を1つに分類）
ATYPE_LABELS = {
    "list": "社名列挙型",       # 具体的な企業名を2社以上挙げている
    "general": "一般論型",      # 具体名を出さず一般論・観点だけ述べる
    "refusal": "留保・拒否型",  # 「特定の社名は控える／把握していない」等の留保
    "empty": "空回答",
    "error": "エラー",          # ERROR:（APIキー未設定等の空振り）
}

# 「具体名を出さない／答えを留保する」ことを示すフレーズ（refusal 判定用）
REFUSAL_HINTS = [
    "特定の企業名", "特定の企業", "特定の会社", "特定のメーカー", "特定のブランド",
    "具体的な社名", "具体的な企業名", "具体的な会社名", "具体的な企業",
    "お答えでき", "お答えする立場", "存じ上げ", "把握してお", "把握してい",
    "情報を持ち合わせ", "確実な情報を持ち合", "知識の範囲では", "学習データ",
    "該当する企業", "という名称の", "確認できる情報", "申し訳",
]

# 評価軸キーワード辞書：AI が回答で持ち出す「選定・評価の観点」。
# 未言及回答での頻度＝「当社が勝つべき土俵／強化すべきコンテンツの切り口」。
# 形式： key -> (表示名, [キーワード…])
CRITERIA = {
    "quality":   ("品質・耐久性",   ["品質", "高品質", "耐久", "丈夫", "堅牢", "縫製"]),
    "track":     ("実績・事例",     ["実績", "導入実績", "納入実績", "採用実績", "事例"]),
    "price":     ("価格・コスト",   ["価格", "コスト", "費用", "安価", "リーズナブル", "低価格", "コストパフォーマンス", "コスパ"]),
    "delivery":  ("納期・生産体制", ["納期", "短納期", "量産", "生産体制", "小ロット", "大量生産", "供給体制"]),
    "design":    ("デザイン性",     ["デザイン性", "おしゃれ", "スタイリッシュ", "意匠", "デザイン"]),
    "brand":     ("ブランド力",     ["ブランドイメージ", "ブランディング", "ブランド力", "ブランド"]),
    "support":   ("対応・サポート", ["サポート", "アフター", "フォロー", "提案力", "相談", "対応力"]),
    "custom":    ("別注・カスタム", ["オーダーメイド", "フルオーダー", "セミオーダー", "別注", "カスタム", "オリジナル", "オーダー"]),
    "function":  ("機能性",         ["機能性", "ストレッチ", "制電", "帯電防止", "防炎", "透湿", "撥水", "動きやすさ", "快適性"]),
    "sustain":   ("環境・サステナ", ["サステナ", "サステナブル", "環境配慮", "リサイクル", "SDGs", "再生素材", "エコ"]),
    "scale":     ("規模・信頼性",   ["大手", "老舗", "全国展開", "上場", "国内最大", "最大手", "シェア"]),
    "specialty": ("業種特化",       ["業界特化", "医療用", "白衣", "作業服", "サービス業", "専門", "特化"]),
}


def classify_answer(answer, n_company):
    """回答を1タイプに分類する。n_company=本文中の企業名候補（太字）数。"""
    a = (answer or "").strip()
    if not a:
        return "empty"
    if a.startswith("ERROR"):
        return "error"
    if n_company >= 2:
        return "list"
    if any(h in a for h in REFUSAL_HINTS):
        return "refusal"
    return "general"


def detect_criteria(answer):
    """回答本文に登場する評価軸キーワードの key 一覧を返す。"""
    if not answer:
        return []
    hits = []
    for key, (_label, words) in CRITERIA.items():
        if any(w in answer for w in words):
            hits.append(key)
    return hits


# ────────────────────────────────────────────────────────────────
# 読み込み
# ────────────────────────────────────────────────────────────────
def load_config(path):
    cfg = {
        "results_dir": "",
        "reference_md": "",
        "output_html": os.path.join(HERE, "analysis.html"),
        "share_dirs": [],   # 生成後に analysis.html をコピーする共有フォルダ（BOX 等）
        "reports_dir": "",  # monitoring/data/reports（index.json・insights_*.json。ホームタブ用）
        "measurement_notes": [],   # 計測の断絶・注意事項 [{date, label, note}]
        "domain_owner": {},        # domain_label -> 担当事業（UN/SCH/MED/IS/コーポレート/横断）
        # 稼働モデル名にこれらが含まれれば Web 検索（グラウンディング）ありとみなす
        "grounding_model_keywords": ["perplexity", "sonar", "grounding", "search"],
        # Google参考値タブ：GSC の手動ダウンロード（xlsx）の置き場所と、設問キーワードのマスタ
        "gsc_dir": "",
        "google_keywords_csv": "",   # 空なら results_dir の2つ上（monitoring）/config/google_keywords.csv
        "gsc_sites": {},             # サイト略称 -> {label, domain}（空なら gsc_reader.GSC_SITES）
        "action_notes": [],          # 施策の記録 [{date, qids:[設問ID...], note}]（推移に表示）
    }
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def google_keywords_path(cfg):
    p = cfg.get("google_keywords_csv") or ""
    if not p and cfg.get("results_dir"):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(cfg["results_dir"]))),
                         "config", "google_keywords.csv")
    return p


def load_google(cfg):
    """Google参考値タブのデータ。失敗しても生成は止めない（タブに「データなし」を出す）。"""
    try:
        g = gsc_reader.build(cfg.get("gsc_dir", ""), google_keywords_path(cfg), cfg.get("gsc_sites") or None)
    except Exception as e:
        g = {"available": False, "periods": [], "keywords": {}, "ref": {}, "daily": {}, "ai_pages": {},
             "log": [], "sites": gsc_reader.GSC_SITES, "dir": cfg.get("gsc_dir", ""),
             "warnings": [f"GSC の読み込みに失敗しました（{e}）"]}
    notes = []
    for n in cfg.get("action_notes") or []:
        if isinstance(n, dict) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(n.get("date", ""))):
            q = n.get("qids") or []
            notes.append({"date": n["date"], "qids": [str(x) for x in (q if isinstance(q, list) else [q])],
                          "note": str(n.get("note", ""))})
    g["action_notes"] = sorted(notes, key=lambda n: n["date"])
    return g


_fn_re = re.compile(r'results_(\d{8})_(\d{6})_r(\d+)\.csv', re.I)


def parse_run(fn):
    """ファイル名 results_YYYYMMDD_HHMMSS_rN.csv から回情報を解析。
    返り値: timing_id, run_no, run_key, run_label, timing_label"""
    m = _fn_re.search(fn)
    if not m:
        # 想定外の名前でも動くようフォールバック（ファイル名そのものを回として扱う）
        base = fn.replace("results_", "").replace(".csv", "")
        return base, "1", base, base, base
    ymd, hms, rn = m.group(1), m.group(2), m.group(3)
    timing_id = f"{ymd}_{hms}"
    try:
        dt = datetime.strptime(timing_id, "%Y%m%d_%H%M%S")
        tlabel = dt.strftime("%m-%d %H:%M")
    except Exception:
        tlabel = timing_id
    run_key = f"{timing_id}_r{rn}"
    return timing_id, rn, run_key, f"{tlabel} r{rn}", tlabel


def load_rows(results_dir):
    files = sorted(glob.glob(os.path.join(results_dir, "results_*.csv")))
    if not files:
        raise SystemExit(f"[ERROR] results_*.csv が見つかりません: {results_dir}")
    rows = []
    files_meta = []
    for fp in files:
        fn = os.path.basename(fp)
        timing_id, run_no, run_key, run_label, timing_label = parse_run(fn)
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
                    "run": run_key,
                    "run_label": run_label,
                    "run_no": run_no,
                    "timing": timing_id,
                    "timing_label": timing_label,
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
                bold = extract_competitors(row["answer"])
                row["competitors"] = bold if not hit else []
                row["atype"] = classify_answer(row["answer"], len(bold))
                row["criteria"] = detect_criteria(row["answer"])
                rows.append(row)
                n += 1
                if not first_set:
                    first_set = qset
        files_meta.append({"name": fn, "rows": n})
    return rows, files_meta


_ins_re = re.compile(r'insights_(\d{8})_(\d{6})(?:_r\d+)?\.json$', re.I)
INSIGHTS_KEYS = ("generated_at", "source", "summary", "weak_cells", "extraction", "actions")


def load_reports(reports_dir):
    """monitoring/data/reports から index.json と最新の insights_*.json を読む（ホームタブ用）。
    無い・壊れている場合もエラーにせず、該当部分を None にして warnings に理由を残す。"""
    rep = {"dir": reports_dir or "", "available": False, "index": None,
           "insights": None, "insights_file": "", "insights_timing": "", "warnings": []}
    if not reports_dir or not os.path.isdir(reports_dir):
        rep["warnings"].append(f"reports_dir が見つかりません: {reports_dir or '（未設定）'}")
        return rep
    rep["available"] = True

    ip = os.path.join(reports_dir, "index.json")
    try:
        with open(ip, encoding="utf-8-sig") as f:
            idx = json.load(f)
        if isinstance(idx, list):
            rep["index"] = [e for e in idx if isinstance(e, dict)]
        else:
            rep["warnings"].append("index.json が配列ではありません（無視）")
    except FileNotFoundError:
        rep["warnings"].append("index.json がありません")
    except (OSError, ValueError) as e:
        rep["warnings"].append(f"index.json を読めません（{e}）")

    # insights_YYYYMMDD_HHMMSS.json と insights_YYYYMMDD_HHMMSS_rN.json が混在 → 日時でソートし最新を採用
    cands = []
    for fp in glob.glob(os.path.join(reports_dir, "insights_*.json")):
        m = _ins_re.search(os.path.basename(fp))
        if m:
            cands.append((f"{m.group(1)}_{m.group(2)}", os.path.basename(fp), fp))
    cands.sort(reverse=True)
    for timing, fn, fp in cands:
        try:
            with open(fp, encoding="utf-8-sig") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                raise ValueError("オブジェクトではありません")
        except (OSError, ValueError) as e:
            rep["warnings"].append(f"{fn} を読めません（{e}）。1つ前のファイルを試します")
            continue
        rep["insights"] = {k: d.get(k) for k in INSIGHTS_KEYS}
        rep["insights_file"] = fn
        rep["insights_timing"] = timing
        break
    if not cands:
        rep["warnings"].append("insights_*.json がありません")
    return rep


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
def build_payload(rows, files_meta, ref, cfg, reports=None):
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

    # 回（run）・タイミング（timing）を時系列順に整理（キーは YYYYMMDD_HHMMSS で自然に昇順）
    run_keys = sorted({r["run"] for r in rows})
    run_labels = {r["run"]: r["run_label"] for r in rows}
    timing_keys = sorted({r["timing"] for r in rows})
    timing_labels = {r["timing"]: r["timing_label"] for r in rows}
    # 各タイミングに含まれる run 数（比較ビューの注記用）
    timing_runs = {}
    for r in rows:
        timing_runs.setdefault(r["timing"], set()).add(r["run"])
    timing_runs = {k: sorted(v) for k, v in timing_runs.items()}

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
            "runs": run_keys, "run_labels": run_labels,
            "timings": timing_keys, "timing_labels": timing_labels,
            "timing_runs": timing_runs,
            "atype_labels": ATYPE_LABELS,
            "criteria_labels": {k: v[0] for k, v in CRITERIA.items()},
            "criteria_order": list(CRITERIA.keys()),
        },
        "rows": [
            {
                "i": idx,
                "file": r["file"], "date": r["run_date"], "ts": r["run_ts"],
                "run": r["run"], "run_label": r["run_label"],
                "timing": r["timing"], "timing_label": r["timing_label"],
                "qid": r["qid"], "domain": r["domain"], "domain_label": r["domain_label"],
                "type": r["type"], "type_label": r["type_label"],
                "stakeholder": r["stakeholder"], "stakeholder_label": r["stakeholder_label"],
                "model": r["model"], "set": r["set"], "tier": r["tier"],
                "hit": r["hit"], "question": r["question"], "answer": r["answer"],
                "entities": r["entities"], "urls": r["urls"], "comp": r["competitors"],
                "atype": r["atype"], "crit": r["criteria"],
            }
            for idx, r in enumerate(rows)
        ],
        "reference": ref,
        # ホームタブ用：monitoring の reports（読むだけ）＋ config の注記・担当対応表
        "reports": reports or load_reports(""),
        "home": {
            "measurement_notes": [n for n in (cfg.get("measurement_notes") or [])
                                  if isinstance(n, dict)
                                  and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(n.get("date", "")))],
            "domain_owner": cfg.get("domain_owner") or {},
            "grounding_model_keywords": cfg.get("grounding_model_keywords") or [],
        },
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
    ap.add_argument("--reports-dir", default=None, help="monitoring/data/reports（ホームタブ用）")
    ap.add_argument("--open", action="store_true", help="生成後に analysis.html をブラウザで開く")
    ap.add_argument("--no-share", action="store_true", help="share_dirs へのコピーを行わない")
    ap.add_argument("--skip-if-unchanged", action="store_true",
                    help="入力が前回生成から変わっていなければ再生成しない（自動更新タスク用）")
    ap.add_argument("--log", default=None, help="出力をこのファイルに追記する（自動更新タスク用）")
    args = ap.parse_args()

    if not args.log:
        return run(args)
    # 自動更新タスク（pythonw＝画面なし）から呼ばれる場合：出力とエラーをログへ追記する
    os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
    with open(args.log, "a", encoding="utf-8") as lf:
        sys.stdout = sys.stderr = lf
        print(f"==== {datetime.now():%Y-%m-%d %H:%M:%S} generate.py 自動更新 ====")
        try:
            run(args)
        except SystemExit as e:
            if e.code not in (None, 0):
                print(e.code if isinstance(e.code, str) else f"[ERROR] exit {e.code}")
                raise
        except Exception:
            import traceback
            traceback.print_exc()
            raise SystemExit(1)
        finally:
            lf.flush()
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__


# 変更判定：これらより analysis.html が新しければ「入力に変更なし」
SETTLE_SECONDS = 600   # 直近10分以内に更新された入力は書き込み途中の可能性があるため次回に回す


def _input_files(cfg, config_path):
    files = glob.glob(os.path.join(cfg.get("results_dir", ""), "results_*.csv"))
    rd = cfg.get("reports_dir", "")
    if rd:
        files += glob.glob(os.path.join(rd, "index.json")) + glob.glob(os.path.join(rd, "insights_*.json"))
    files += [p for p in (cfg.get("reference_md", ""), config_path, os.path.abspath(__file__)) if p]
    files += gsc_reader.input_files(cfg.get("gsc_dir", ""))
    files += [google_keywords_path(cfg), os.path.abspath(gsc_reader.__file__)]
    return [p for p in files if p and os.path.isfile(p)]


def _share_is_stale(out, share_dirs):
    """共有フォルダのコピーが無い・古い・サイズ違いなら True（前回コピー失敗の再試行用）。"""
    if isinstance(share_dirs, str):
        share_dirs = [share_dirs]
    for d in share_dirs:
        dst = os.path.join(d, os.path.basename(out))
        if not os.path.isdir(d) or os.path.abspath(dst) == os.path.abspath(out):
            continue
        if (not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(out)
                or os.path.getmtime(dst) + 2 < os.path.getmtime(out)):
            return True
    return False


def run(args):
    cfg = load_config(args.config)
    if args.results_dir:
        cfg["results_dir"] = args.results_dir
    if args.reference:
        cfg["reference_md"] = args.reference
    if args.out:
        cfg["output_html"] = args.out
    if args.reports_dir:
        cfg["reports_dir"] = args.reports_dir

    if not cfg.get("results_dir"):
        raise SystemExit("[ERROR] results_dir 未設定。config.json か --results-dir で指定してください。")

    # results_*.csv が見つからない場合は変更判定をせず、下の load_rows でエラーとして記録させる
    if args.skip_if_unchanged and glob.glob(os.path.join(cfg["results_dir"], "results_*.csv")):
        out = cfg["output_html"]
        inputs = _input_files(cfg, args.config)
        newest = max((os.path.getmtime(p) for p in inputs), default=0)
        if os.path.exists(out) and newest <= os.path.getmtime(out):
            if not args.no_share and _share_is_stale(out, cfg.get("share_dirs") or []):
                print("[SKIP] 入力に変更なし。共有フォルダのコピーが古いため、コピーだけ行います")
                copy_to_share_dirs(out, cfg.get("share_dirs") or [])
            else:
                print("[SKIP] 入力に変更がないため再生成しません")
            return
        if datetime.now().timestamp() - newest < SETTLE_SECONDS:
            print("[SKIP] 入力ファイルが直近10分以内に更新されています（計測中の可能性）。次回に再試行します")
            return

    rows, files_meta = load_rows(cfg["results_dir"])
    ref = load_reference(cfg.get("reference_md", ""))
    reports = load_reports(cfg.get("reports_dir", ""))
    payload = build_payload(rows, files_meta, ref, cfg, reports)
    google = load_google(cfg)
    payload["google"] = google
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
    for w in reports["warnings"]:
        print(f"[WARN] {w}")
    n_bad = len(cfg.get("measurement_notes") or []) - len(payload["home"]["measurement_notes"])
    if n_bad:
        print(f"[WARN] measurement_notes のうち {n_bad} 件は date が YYYY-MM-DD でないため無視しました")
    if reports["insights_file"]:
        # ホームと同じく「有効行（エラー・空以外）のある最新タイミング」と突き合わせる
        valid_timings = [r["timing"] for r in rows if r["atype"] not in ("error", "empty")]
        latest_timing = max(valid_timings) if valid_timings else ""
        same = "最新回と一致" if reports["insights_timing"] == latest_timing else f"最新回 {latest_timing} と不一致"
        print(f"     insights: {reports['insights_file']}（{same}）  "
              f"index: {len(reports['index'] or [])} timings")
    else:
        print("     insights: なし（ホームの該当ブロックは「データなし」表示）")
    n_ok = sum(1 for l in google.get("log", []) if l["status"] == "取り込み")
    print(f"     google: GSC {n_ok} ファイル取り込み／期間 {len(google.get('periods', []))}／"
          f"設問キーワード {len(google.get('keywords', {}))} 問")
    for w in google.get("warnings", []):
        print(f"[WARN] {w}")

    if not args.no_share:
        copy_to_share_dirs(out, cfg.get("share_dirs") or [])
    if args.open:
        open_in_browser(out)


def copy_to_share_dirs(out, share_dirs):
    """生成した HTML を共有フォルダ（BOX 等）へ同名でコピーする。失敗しても生成自体は成功扱い。"""
    if isinstance(share_dirs, str):
        share_dirs = [share_dirs]
    src = os.path.abspath(out)
    for d in share_dirs:
        dst = os.path.join(d, os.path.basename(out))
        if os.path.abspath(dst) == src:
            continue
        if not os.path.isdir(d):
            print(f"[WARN] 共有フォルダが見つかりません（コピーをスキップ）: {d}")
            continue
        try:
            shutil.copy2(src, dst)
            print(f"[OK] 共有フォルダへコピー: {dst}")
        except OSError as e:
            print(f"[WARN] 共有フォルダへのコピーに失敗: {dst} ({e})")


def _default_browser_command():
    """Windows の「既定のブラウザ」（http の関連付け）の起動コマンドを返す。取れなければ None。
    .html の関連付けはエディタ等になっている場合があるため、そちらは使わない。"""
    try:
        import winreg
    except ImportError:
        return None
    for scheme in ("https", "http"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\Shell\Associations"
                                rf"\UrlAssociations\{scheme}\UserChoice") as k:
                progid = winreg.QueryValueEx(k, "ProgId")[0]
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                                rf"{progid}\shell\open\command") as k:
                cmd = winreg.QueryValueEx(k, "")[0]
            if cmd:
                return cmd
        except OSError:
            continue
    return None


def open_in_browser(path):
    """既定のブラウザ（Chrome／Edge／Firefox 等、ユーザーの設定どおり）で HTML を開く。"""
    url = Path(os.path.abspath(path)).as_uri()   # 日本語・空白は %XX に変換される
    cmd = _default_browser_command()
    if cmd:
        cmd = cmd.replace("%1", url) if "%1" in cmd else f'{cmd} "{url}"'
        cmd = re.sub(r'\s%[*\dL]', "", cmd)      # 残りのプレースホルダ（%* 等）は除去
        try:
            subprocess.Popen(cmd)
            print(f"[OK] ブラウザで開きました: {url}")
            return
        except OSError as e:
            print(f"[WARN] 既定のブラウザを起動できませんでした ({e})。Edge で開きます。")
    if os.name == "nt":
        try:
            # 既定ブラウザが取れない場合は Windows 標準の Edge で開く
            subprocess.Popen(f'cmd /c start "" msedge "{url}"')
            print(f"[OK] Edge で開きました: {url}")
            return
        except OSError:
            pass
    if not webbrowser.open(url):
        print(f"[WARN] ブラウザで開けませんでした。手動で開いてください: {path}")


# ────────────────────────────────────────────────────────────────
# HTML テンプレート（自己完結／外部依存は Chart.js CDN のみ）
# ────────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GEO 定点観測 ｜ 生成AI出現モニタリング</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  /* 明るくクリーンなトーン（コーポレートサイトに合わせる）。暗い単色背景は使わない */
  :root{--bg:#f5f7fa;--card:#ffffff;--ink:#1f2a37;--sub:#5f6b7a;--accent:#1d4f91;
        --good:#1a7f4b;--warn:#b7791f;--bad:#c0392b;--line:#dde3ea;--panel:#f1f4f8;}
  *{box-sizing:border-box;}
  body{margin:0;font-family:"Segoe UI","Hiragino Kaku Gothic ProN","Yu Gothic",Meiryo,sans-serif;
       background:var(--bg);color:var(--ink);}
  header{padding:20px 28px;border-bottom:1px solid var(--line);background:var(--card);
         border-top:4px solid var(--accent);}
  header h1{margin:0;font-size:20px;letter-spacing:.02em;}
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
  /* ── ホーム */
  .home-head{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px;margin:2px 0 12px;}
  .home-head .ttl{font-size:18px;font-weight:700;}
  .hblock{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin:0 0 14px;}
  .hblock>h2,.hblock>summary{margin:0 0 10px;font-size:15px;font-weight:700;border-left:3px solid var(--accent);padding-left:8px;}
  details.hblock>summary{cursor:pointer;}
  details.hblock:not([open])>summary{margin-bottom:0;}
  .concl{font-size:16px;line-height:1.8;margin:0 0 12px;}
  .kpis{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;}
  @media(max-width:760px){.kpis{grid-template-columns:1fr;}}
  .kpi-box{border:1px solid var(--line);border-radius:10px;padding:10px 14px;background:var(--panel);}
  .kpi-box .lbl{font-size:12px;color:var(--sub);}
  .kpi-box .val{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums;}
  .kpi-box .dt{font-size:12px;color:var(--sub);}
  .set-box{margin-top:10px;padding:8px 14px;border:1px solid var(--line);border-radius:10px;}
  .set-row{display:flex;flex-wrap:wrap;gap:4px 22px;align-items:baseline;font-size:13px;line-height:1.7;}
  .set-curve{margin-top:2px;font-size:13px;line-height:1.7;} .set-val{font-size:16px;}
  .delta-up{color:var(--good);font-weight:700;} .delta-down{color:var(--bad);font-weight:700;}
  .delta-flat{color:var(--sub);font-weight:700;} .delta-na{color:var(--warn);font-weight:700;}
  .notes li{margin:4px 0;line-height:1.7;font-size:13.5px;}
  .acts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;}
  @media(max-width:960px){.acts{grid-template-columns:1fr;}}
  .act{border:1px solid var(--line);border-radius:10px;padding:10px 14px;font-size:13px;line-height:1.7;}
  .act .hd{font-weight:700;font-size:14px;margin-bottom:4px;}
  .act .k{color:var(--sub);font-size:11.5px;margin-top:6px;}
  .pill.pri{color:#fff;background:var(--bad);border-color:var(--bad);}
  .pill.pri.mid{background:var(--warn);border-color:var(--warn);}
  .pill.pri.low{background:var(--sub);border-color:var(--sub);}
  /* ── Google参考値 */
  table.gquad{table-layout:fixed;} table.gquad th,table.gquad td{text-align:center;vertical-align:middle;}
  table.gquad td.qc{cursor:pointer;border:1px solid var(--line);border-radius:8px;padding:10px 6px;}
  table.gquad td.qc:hover{outline:2px solid var(--accent);}
  table.gquad td.qc.sel{outline:3px solid var(--accent);}
  table.gquad .n{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums;}
  table.gquad .lb{font-size:12px;font-weight:700;} table.gquad .ds{font-size:11px;color:var(--sub);}
  .q-geo{background:#fdecea;} .q-geo .lb{color:var(--bad);}
  .q-both{background:#e8f5ee;} .q-both .lb{color:var(--good);}
  .q-ai{background:#eef4ff;} .q-ai .lb{color:var(--accent);}
  .q-weak{background:#fdf6e7;} .q-weak .lb{color:var(--warn);}
  .q-none{background:var(--panel);}
  .judge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;white-space:nowrap;}
  td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;}
  .approx{color:var(--warn);font-weight:700;cursor:help;} .gap{color:var(--warn);cursor:help;}
  .pill.own{color:var(--accent);border-color:var(--accent);background:#fff;font-weight:700;}
  .qgrid{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
  @media(max-width:960px){.qgrid{grid-template-columns:1fr;}}
  .q{font-weight:700;margin:0 0 6px;font-size:14px;}
  .q .no{display:inline-block;background:var(--accent);color:#fff;border-radius:6px;padding:0 7px;margin-right:6px;}
  table.memo th,.hblock td.nw{white-space:nowrap;} .hblock td.num{text-align:right;font-variant-numeric:tabular-nums;}
  table.memo td{height:34px;} table.memo td.blank{min-width:110px;background:#fff;border:1px dashed var(--line);}
  .nodata{color:var(--sub);background:var(--panel);border:1px dashed var(--line);border-radius:8px;padding:10px 14px;font-size:13px;}
  .links{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;}
  .links button{text-align:left;background:var(--panel);line-height:1.6;padding:9px 12px;}
  .links button b{color:var(--accent);}
  .help{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;
        border:1px solid var(--accent);color:var(--accent);font-size:10.5px;font-weight:700;cursor:help;
        position:relative;margin-left:4px;vertical-align:1px;background:#fff;}
  .help:hover::after,.help:focus::after{content:attr(data-tip);position:absolute;left:50%;top:22px;transform:translateX(-50%);
        width:260px;background:var(--ink);color:#fff;font-weight:400;font-size:12px;line-height:1.6;
        padding:8px 10px;border-radius:8px;z-index:20;white-space:normal;text-align:left;}
  /* ── 印刷（A4横）：表示中のタブだけを出す。ホームは1〜2枚に収める */
  @media print{
    @page{size:A4 landscape;margin:8mm;}
    *{-webkit-print-color-adjust:exact;print-color-adjust:exact;}
    body{background:#fff;font-size:11px;}
    header{padding:6px 0;border-top:none;} header .sub{display:none;}
    header h1{font-size:14px;}
    main{padding:0;}
    .tabs,.filters,details.howto,.modal,.noprint,.help{display:none!important;}
    section{display:none!important;padding-top:4px;} section.active{display:block!important;}
    .hblock{padding:7px 10px;margin:0 0 6px;break-inside:avoid;}
    .hblock>h2,.hblock>summary{font-size:12.5px;margin-bottom:5px;}
    .concl{font-size:12.5px;margin-bottom:6px;line-height:1.6;}
    .kpi-box{padding:5px 9px;} .kpi-box .val{font-size:18px;}
    .set-box{margin-top:5px;padding:3px 9px;} .set-row,.set-curve{font-size:10.5px;line-height:1.45;} .set-val{font-size:12px;}
    .pill{font-size:9.5px;padding:0 6px;} .act .k{margin-top:2px;font-size:10px;} .kpi-box .lbl,.kpi-box .dt{font-size:10px;}
    .kpis,.acts{gap:8px!important;} .home-head{margin:0 0 6px;} .home-head .ttl{font-size:14px;}
    .notes li{font-size:11px;margin:1px 0;line-height:1.5;}
    .act{padding:6px 9px;font-size:10.5px;line-height:1.5;}
    .act .hd{font-size:11.5px;}
    table{font-size:10.5px;} th,td{padding:3px 6px;} th{position:static;}
    table.memo td{height:28px;}
    .kpis,.acts{grid-template-columns:repeat(3,minmax(0,1fr))!important;}
    .qgrid{grid-template-columns:1fr 1fr!important;}
  }
</style>
</head>
<body>
<header>
  <h1>GEO 定点観測 ｜ 生成AI出現モニタリング <span class="muted">｜ 関係者向けの入口（ホーム → 詳しい分析）</span></h1>
  <div class="sub" id="hdr-sub"></div>
</header>
<main>
  <div class="tabs" id="tabs"></div>

  <!-- ホーム：今どうなっているか → なぜか → 次に何をするか → 何を議論するか -->
  <section id="s-home" class="active">
    <div class="home-head"><div class="ttl" id="home-title">今回のまとめ</div>
      <div class="muted" id="home-asof"></div></div>
    <div class="hblock"><h2>1) 今回の結論</h2><div id="home-concl"></div></div>
    <details class="hblock" open><summary>2) 計測上の注意</summary><div id="home-notes"></div></details>
    <div class="hblock"><h2>3) 次にやること <span class="muted" style="font-weight:400">（示唆レポートの改善アクション 上位3件）</span></h2>
      <div id="home-actions"></div></div>
    <div class="hblock"><h2>4) 今回の議論ポイント</h2>
      <div class="qgrid">
        <div><div class="q"><span class="no">Q1</span>前回から出た／消えた質問は？</div><div id="home-q1"></div></div>
        <div><div class="q"><span class="no">Q2</span>出なかった質問で、AIは代わりに誰を・どんな観点で挙げたか？</div><div id="home-q2"></div></div>
      </div>
      <div class="q" style="margin-top:14px"><span class="no">Q3</span>次の回までに何を直すか・担当は？</div>
      <div id="home-q3"></div>
    </div>
    <div class="hblock noprint"><h2>5) 詳しく見るには</h2><div class="links" id="home-links"></div></div>
  </section>

  <!-- P1: 競合共起 -->
  <section id="s-comp">
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

  <!-- 回答分析（コンテキスト分析） -->
  <section id="s-context">
    <div class="summary" id="context-summary"></div>
    <details class="howto"><summary>この画面の見方</summary>
      <div class="body">回答全文（1回ごとの全履歴）を、社名だけでなく<b>「どう答えているか」</b>で分析します。
        <b>①回答タイプ</b>＝各回答を「社名列挙型／一般論型／留保・拒否型／エラー・空」に自動分類（崖の下でAIが“名を出さず一般論に逃げる”構造を可視化）。
        <b>②評価軸キーワード</b>＝未言及回答でAIが重視する観点（品質・実績・納期…）の頻度＝<b>強化すべき自社コンテンツの切り口</b>。
        <b>③競合の文脈</b>＝競合名がどんな一文で描写されているか。<b>④自社ヒット文脈</b>＝当社が出た回答での前後文とトリガー。
        <br>※ すべてオフラインのヒューリスティック（要目視確認）。分類・キーワードは generate.py の CRITERIA/ATYPE で調整できます。</div>
    </details>
    <div class="filters" id="filters-context"></div>
    <div class="grid g2">
      <div class="card"><h3>① 回答タイプの分布</h3><div id="ctx-type"></div></div>
      <div class="card"><h3>② 評価軸キーワード（未言及回答でAIが重視する観点）</h3>
        <div class="bar-wrap"><canvas id="chart-ctx" height="320"></canvas></div>
        <div id="ctx-crit"></div></div>
    </div>
    <h2>③ 競合の文脈スニペット（未言及回答で競合がどう描かれているか）</h2>
    <div class="card"><div id="ctx-comp"></div></div>
    <h2>④ 自社ヒット時の文脈（当社が出た回答の前後文・共起する評価軸）</h2>
    <div class="card"><div id="ctx-self"></div></div>
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

  <!-- 過去回比較 -->
  <section id="s-compare">
    <div class="summary" id="compare-summary"></div>
    <details class="howto"><summary>この画面の見方</summary>
      <div class="body">2つの回（A=過去 / B=比較）を選び、A→B の変化を見ます。粒度は
        <b>run</b>（ファイル1つ＝1回。r1/r2 を個別に）と <b>timing</b>（同一タイミングの r1/r2 をまとめて平均）から選べます。
        見られるもの：全体・ドメイン別・特異度別の出現率デルタ／競合の増減（新規登場・消失）／質問の反転（出た↔消えた）。
        反転質問はクリックで A と B の回答全文を並べて比較できます。</div>
    </details>
    <div class="filters" id="filters-compare"></div>
    <div class="grid g2">
      <div class="card"><h3>全体・軸別の出現率デルタ（A→B）</h3><div id="cmp-rates"></div></div>
      <div class="card"><h3>競合の増減（登場回答数 A→B）</h3><div id="cmp-comp"></div></div>
    </div>
    <h2>質問の反転（出た↔消えた）</h2>
    <div class="card"><div id="cmp-flip"></div></div>
  </section>

  <!-- 全回サマリー（過去回すべての比較・総合） -->
  <section id="s-runs">
    <div class="summary" id="runs-summary"></div>
    <details class="howto"><summary>この画面の見方</summary>
      <div class="body">過去<b>すべての回</b>を一望します。粒度は <b>run</b>（ファイル1つ＝1回）と <b>timing</b>（同一タイミングの r1/r2 をまとめ）から選べます。
        <b>推移チャート</b>＝全回の出現率の時系列。<b>全回テーブル</b>＝回ごとの出現率・hits・エラー行数・競合数・トップ競合。
        <b>総合集計</b>＝全回をプールしたドメイン別・特異度別の出現率と、回ごとのブレ（最小〜最大の幅）。
        <br>※ 出現率は<b>有効行（エラー・空を除く）</b>を分母に計算します。APIキー未設定などで全行エラーの回は「—」と表示され、集計から自動で外れます。</div>
    </details>
    <div class="filters" id="filters-runs"></div>
    <div class="card"><h3>出現率の推移（全回）</h3>
      <div class="bar-wrap" style="max-height:340px"><canvas id="chart-runs" height="300"></canvas></div></div>
    <h2>全回テーブル（クリックで その回のヒット回答へ）</h2>
    <div class="card xtab"><div id="runs-table"></div></div>
    <h2>総合集計（全回プール）＋ 回ごとのブレ</h2>
    <div class="grid g2">
      <div class="card"><h3>ドメイン別</h3><div id="runs-agg-domain"></div></div>
      <div class="card"><h3>特異度ティア別（Set2）</h3><div id="runs-agg-tier"></div></div>
    </div>
  </section>

  <!-- Google参考値（GSC ＝ 自社サイトの Google 自然検索） -->
  <section id="s-google">
    <div class="summary" id="g-summary"></div>
    <details class="howto"><summary>この画面の見方</summary>
      <div class="body">Claude の出現と<b>同じ設問</b>について、Google 自然検索での自社サイトの順位（GSC＝Google Search Console）を<b>参考値</b>として並べ、
        <b>担当ごとに次にやること</b>を出します。見る順番：<b>①次にやること</b>（優先度の高い打ち手と、効果の大きい設問）→ <b>②担当別</b>（自分の担当の件数）→
        <b>③設問一覧</b>（設問ごとの「次の一手」。行クリックで期間ごとの値と推移）。<b>④判定の根拠</b>は、Claude の出現（出現率が0%より大きいか）×
        Google の順位（10位以内か）のマス目です。
        <br>やることの決め方：Claude で出現 →「維持」／Claude で出ない かつ Google 10位以内 →「<b>AI向けに直す</b>」（ページはあるので AI に届く形に直す）／
        11〜20位 →「あと一歩」／21位以下 →「順位を上げる」／GSC にデータなし →「検索チェック待ち」。
        <br>※ GSC は<b>自社サイトのみ</b>・<b>表示されたときの平均順位</b>です。1設問に1つの「GSC参照クエリ」（実際に検索されている近い語）で照合しており、
        観測キーワードとずれる場合は「≈」を付けています。GSC に出てこない語は<b>データなし</b>（検索されていない／自社が出ていない／上位1,000件の外）で、0位ではありません。</div>
    </details>
    <div class="filters" id="filters-google"></div>
    <h2>① 次にやること</h2>
    <div class="acts" id="g-todo"></div>
    <h2>② 担当別のやること（数字をクリックで一覧を絞り込み）</h2>
    <div class="card xtab" id="g-owner"></div>
    <h2>③ 設問一覧 <span class="muted" style="font-weight:400" id="g-table-head"></span></h2>
    <div class="card xtab"><div id="g-table"></div></div>
    <h2>④ 判定の根拠（Claude の出現 × Google の順位）</h2>
    <div class="card"><div id="g-quad"></div>
      <div class="muted" id="g-quad-note" style="margin-top:8px"></div></div>
    <h2>⑤ AI Overview（GSC 生成AI機能：自社ページが表示された回数）</h2>
    <div class="grid g2">
      <div class="card"><h3>日別の表示回数</h3><div class="filters" id="g-ai-filters" style="margin:0 0 10px;padding:8px 10px"></div>
        <div class="bar-wrap" style="max-height:320px"><canvas id="chart-g-ai" height="280"></canvas></div></div>
      <div class="card"><h3>表示回数の多いページ</h3><div id="g-ai-pages" class="bar-wrap"></div></div>
    </div>
    <details class="howto" style="margin-top:16px"><summary>GSC ファイルの取り込み状況</summary>
      <div class="body" id="g-log"></div></details>
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
  ["s-home","ホーム"],
  ["s-comp","競合共起 (P1)"],["s-context","回答分析"],["s-cross","多軸クロス集計 (P1)"],
  ["s-compare","過去回比較"],["s-runs","全回サマリー"],["s-google","Google参考値"],
  ["s-url","引用URL (P2)"],["s-self","自社突合 (P2)"],["s-p3","競合サイト (P3)"]
];
const tabsEl = $("#tabs");
function showTab(id){
  $$(".tab").forEach(t=>t.classList.toggle("active", t.dataset.id===id));
  $$("section").forEach(s=>s.classList.remove("active")); $("#"+id).classList.add("active");
  if(id==="s-comp") renderComp(); if(id==="s-context") renderContext();
  if(id==="s-cross") renderCross(); if(id==="s-compare") renderCompare();
  if(id==="s-runs") renderRuns(); if(id==="s-google") renderGoogle();
}
TABS.forEach(([id,label],i)=>{
  const b=document.createElement("div"); b.className="tab"+(i===0?" active":""); b.textContent=label;
  b.dataset.id=id; b.onclick=()=>showTab(id);
  tabsEl.appendChild(b);
});

// ── フィルタ UI（各タブに独立生成）
const FSTATE = {comp:{},cross:{},url:{},context:{},runs:{gran:"run"}};
function optionSet(){ return {
  timing:["タイミング",D.timings], run:["回(run)",D.runs],
  date:["実行日",D.dates], domain:["ドメイン",D.domains],
  tier:["特異度",D.tiers], set:["質問セット",D.sets], model:["モデル",D.models]
};}
function optLabel(k,v){
  if(k==="tier") return `${v} ${D.tier_labels[v]||""}`;
  if(k==="domain") return `${v}｜${D.domain_labels[v]||""}`;
  if(k==="timing") return D.timing_labels[v]||v;
  if(k==="run") return D.run_labels[v]||v;
  return v;
}
function buildFilters(mountId, key, onchange, extra=[]){
  const mount=$("#"+mountId); mount.innerHTML="";
  const opts=optionSet();
  const order = ["timing","run","date","domain","tier","set","model"];
  order.forEach(k=>{
    const [label,vals]=opts[k];
    if(!vals || !vals.length) return;
    const f=document.createElement("div"); f.className="f";
    const lab=document.createElement("label"); lab.textContent=label;
    const sel=document.createElement("select"); sel.dataset.k=k;
    sel.innerHTML=`<option value="">すべて</option>`+vals.map(v=>
      `<option value="${esc(v)}">${esc(optLabel(k,v))}</option>`).join("");
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
  if(st.timing && r.timing!==st.timing) return false;
  if(st.run && r.run!==st.run) return false;
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
  return `rgba(29,79,145,${0.07+a*0.38})`; }

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
        backgroundColor:"rgba(29,79,145,.6)",borderColor:"#1d4f91",borderWidth:1}]},
    options:{indexAxis:"y",responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false}},
      scales:{x:{ticks:{color:"#5f6b7a"},grid:{color:"#e6ebf1"}},
              y:{ticks:{color:"#1f2a37"},grid:{display:false}}}}});
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
const AXES = {run:"回(run)",timing:"タイミング",domain:"ドメイン",tier:"特異度",type:"質問タイプ",stakeholder:"ステークホルダー",model:"モデル",date:"実行日",set:"質問セット"};
function axisVals(ax){
  if(ax==="domain")return D.domains; if(ax==="tier")return D.tiers;
  if(ax==="type")return D.types; if(ax==="stakeholder")return D.stakeholders;
  if(ax==="model")return D.models; if(ax==="date")return D.dates; if(ax==="set")return D.sets;
  if(ax==="run")return D.runs; if(ax==="timing")return D.timings;
  return [];
}
function axisLabel(ax,v){
  if(ax==="domain")return `${v}｜${D.domain_labels[v]||""}`;
  if(ax==="tier")return `${v} ${D.tier_labels[v]||""}`;
  if(ax==="type")return `${v}｜${D.type_labels[v]||""}`;
  if(ax==="stakeholder")return `${v}｜${D.stakeholder_labels[v]||""}`;
  if(ax==="run")return D.run_labels[v]||v;
  if(ax==="timing")return D.timing_labels[v]||v;
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

// ─────────────────────────────────────────── 過去回比較
function cmpUnits(gran){ return gran==="timing" ? D.timings : D.runs; }
function cmpLabel(gran,key){ return gran==="timing" ? (D.timing_labels[key]||key) : (D.run_labels[key]||key); }
function cmpRows(gran,key){ return ROWS.filter(r => (gran==="timing"?r.timing:r.run)===key); }
function dpt(d){ // デルタ表示（pt）
  if(d==null) return `<span class="flat">–</span>`;
  if(d>0) return `<span style="color:var(--good)">▲ +${d}pt</span>`;
  if(d<0) return `<span style="color:var(--bad)">▼ ${d}pt</span>`;
  return `<span class="muted">± 0pt</span>`;
}
function compAgg(rows){ // 競合名 -> 登場回答数（未言及回答のみ）
  const m={}; rows.forEach(r=>{ if(!r.hit)(r.comp||[]).forEach(n=>m[n]=(m[n]||0)+1); }); return m; }
function byQid(rows){ // qid -> {hit:bool, idxs:[], rep:i}
  const m={};
  rows.forEach(r=>{ const o=m[r.qid]||(m[r.qid]={hit:false,idxs:[],rep:null,q:r.question,domain:r.domain,tier:r.tier});
    o.idxs.push(r.i); if(r.hit){o.hit=true; if(o.rep==null)o.rep=r.i;} });
  Object.values(m).forEach(o=>{ if(o.rep==null)o.rep=o.idxs[0]; });
  return m;
}
function renderCompare(){
  const st=FSTATE.compare;
  const gran=st.gran||"run";
  const units=cmpUnits(gran);
  if(units.length<2){
    $("#compare-summary").innerHTML=`比較には2回以上の実行が必要です（現在 ${units.length} ${gran}）。`;
    ["cmp-rates","cmp-comp","cmp-flip"].forEach(id=>$("#"+id).innerHTML="");
    return;
  }
  let A=st.A, B=st.B;
  if(!units.includes(A)) A=units[units.length-2];
  if(!units.includes(B)) B=units[units.length-1];
  st.A=A; st.B=B; st.gran=gran;
  const ra=cmpRows(gran,A), rb=cmpRows(gran,B);
  const rA=rate(ra), rB=rate(rb);
  const dOverall = (rA!=null&&rB!=null)?Math.round((rB-rA)*10)/10:null;
  $("#compare-summary").innerHTML =
    `<b>${esc(cmpLabel(gran,A))}</b>（${ra.filter(r=>r.hit).length}/${ra.length}・${rA}%）→ `
    +`<b>${esc(cmpLabel(gran,B))}</b>（${rb.filter(r=>r.hit).length}/${rb.length}・${rB}%）　全体 ${dpt(dOverall)}`;
  // 軸別デルタ（ドメイン→特異度）
  const axisTable=(ax,vals)=>{
    const rowsH=vals.filter(v=>ra.some(r=>r[ax]===v)||rb.some(r=>r[ax]===v)).map(v=>{
      const a=rate(ra.filter(r=>r[ax]===v)), b=rate(rb.filter(r=>r[ax]===v));
      const d=(a!=null&&b!=null)?Math.round((b-a)*10)/10:null;
      return `<tr><td>${esc(axisLabel(ax,v))}</td><td class="rate">${a==null?"–":a+"%"}</td>
        <td class="rate">${b==null?"–":b+"%"}</td><td class="rate">${dpt(d)}</td></tr>`;}).join("");
    return `<table><thead><tr><th>${AXES[ax]}</th><th>A</th><th>B</th><th>Δ</th></tr></thead><tbody>${rowsH}</tbody></table>`;
  };
  let ratesHtml=`<div class="muted" style="margin:2px 0 8px">ドメイン別</div>`+axisTable("domain",D.domains);
  if(D.tiers.length) ratesHtml+=`<div class="muted" style="margin:14px 0 8px">特異度ティア別（Set2）</div>`+axisTable("tier",D.tiers);
  $("#cmp-rates").innerHTML=ratesHtml;
  // 競合の増減
  const ca=compAgg(ra), cb=compAgg(rb);
  const names=[...new Set([...Object.keys(ca),...Object.keys(cb)])];
  const comp=names.map(n=>({n,a:ca[n]||0,b:cb[n]||0,d:(cb[n]||0)-(ca[n]||0)}))
    .sort((x,y)=>Math.abs(y.d)-Math.abs(x.d)||y.b-x.b).slice(0,25);
  $("#cmp-comp").innerHTML = comp.length
    ? `<table><thead><tr><th>競合候補</th><th>A</th><th>B</th><th>Δ</th><th></th></tr></thead><tbody>`
      + comp.map(c=>`<tr><td>${esc(c.n)}</td><td class="rate">${c.a}</td><td class="rate">${c.b}</td>
          <td class="rate">${c.d>0?'<span style="color:var(--bad)">+'+c.d+'</span>':(c.d<0?'<span style="color:var(--good)">'+c.d+'</span>':'0')}</td>
          <td>${c.a===0?'<span class="pill bad">新規</span>':(c.b===0?'<span class="pill good">消失</span>':'')}</td></tr>`).join("")
      + `</tbody></table><div class="muted" style="margin-top:6px">競合の登場が増える(＋・赤)＝負けが拡大、減る(－・緑)＝改善の可能性。</div>`
    : `<div class="muted">競合候補なし</div>`;
  // 質問の反転
  const qa=byQid(ra), qb=byQid(rb);
  const common=Object.keys(qa).filter(q=>q in qb);
  const gained=[], lost=[], stayHit=[], stayMiss=[];
  common.forEach(q=>{ const a=qa[q].hit, b=qb[q].hit;
    if(!a&&b) gained.push(q); else if(a&&!b) lost.push(q);
    else if(a&&b) stayHit.push(q); else stayMiss.push(q); });
  const flipRow=(q,kind)=>{ const o=qb[q]||qa[q];
    return `<tr class="click" onclick='showFlip(${JSON.stringify(q)},${JSON.stringify(qa[q].rep)},${JSON.stringify(qb[q].rep)})'>
      <td>${kind}</td><td><b>${esc(q)}</b></td><td class="muted">${esc((o.q||"").slice(0,54))}</td>
      <td>${esc(o.domain)}${o.tier?(" / "+o.tier):""}</td><td class="muted">A/B比較 ›</td></tr>`; };
  const rowsHtml = [...gained.map(q=>flipRow(q,'<span class="tagH">出た↑</span>')),
                    ...lost.map(q=>flipRow(q,'<span class="tagM">消えた↓</span>'))].join("");
  $("#cmp-flip").innerHTML =
    `<div class="muted" style="margin-bottom:8px">共通質問 ${common.length}件：
      <span class="tagH">出た↑ ${gained.length}</span> ／ <span class="tagM">消えた↓ ${lost.length}</span> ／
      出続け ${stayHit.length} ／ 消え続け ${stayMiss.length}</div>`
    + (rowsHtml
      ? `<table><thead><tr><th>変化</th><th>質問ID</th><th>質問</th><th>領域</th><th></th></tr></thead><tbody>${rowsHtml}</tbody></table>`
      : `<div class="muted">反転した質問はありません（両回とも同じ結果）。</div>`);
}
function showFlip(qid, ia, ib){
  const A=ROWS[ia], B=ROWS[ib];
  const block=(lbl,r)=> r ? `<div style="margin-bottom:6px"><span class="pill">${esc(lbl)}: ${esc(r.run_label)}</span>
      <span class="${r.hit?'tagH':'tagM'}">${r.hit?'● 言及あり':'× 未言及'}</span>
      ${(r.comp&&r.comp.length)?' 競合: '+r.comp.slice(0,6).map(n=>`<span class="pill bad">${esc(n)}</span>`).join(''):''}</div>
      <div class="answer" style="max-height:34vh">${esc(r.answer)}</div>` : `<div class="muted">${esc(lbl)}: 該当なし</div>`;
  openModal(`<h3>${esc(qid)}｜A→B 回答比較</h3>
    <div class="meta">${esc((A||B).question)}</div>
    <div style="margin-top:8px">${block("A",A)}</div>
    <div style="margin-top:14px">${block("B",B)}</div>`);
}

// ─────────────────────────────────────────── 回答分析（コンテキスト）
const isValid = r => r.atype!=='error' && r.atype!=='empty';
function stripBold(s){ return (s==null?"":String(s)).replace(/\*\*/g,""); }
function sentencesWith(text, term){
  if(!text||!term) return [];
  const parts=String(text).split(/\n+|(?<=[。！？])/);
  const out=[];
  for(const p of parts){ const s=stripBold(p).trim(); if(s && s.indexOf(term)>=0) out.push(s); }
  return out;
}
function sentencesWithAny(text, terms){
  const parts=String(text||"").split(/\n+|(?<=[。！？])/);
  const low=(terms||[]).map(t=>String(t).toLowerCase());
  const out=[];
  for(const p of parts){ const s=stripBold(p).trim(); if(!s) continue;
    const sl=s.toLowerCase(); if(low.some(t=>t&&sl.indexOf(t)>=0)) out.push(s); }
  return out;
}
let ctxChart=null;
function renderContext(){
  const st=FSTATE.context;
  const rows=ROWS.filter(r=>passFilter(r,st));
  const valid=rows.filter(isValid);
  const miss=valid.filter(r=>!r.hit);
  const hitRows=valid.filter(r=>r.hit);
  const typeOrder=["list","general","refusal","empty","error"];
  const tc={}; rows.forEach(r=>tc[r.atype]=(tc[r.atype]||0)+1);
  const tot=rows.length||1; const pct=n=>Math.round(n/tot*1000)/10;
  $("#context-summary").innerHTML = rows.length
    ? `対象 <b>${rows.length}</b> 回答（有効 <b>${valid.length}</b>／エラー ${tc.error||0}／空 ${tc.empty||0}）。`
      +`社名列挙型 <b>${pct(tc.list||0)}%</b>・一般論型 <b>${pct(tc.general||0)}%</b>・留保拒否型 <b>${pct(tc.refusal||0)}%</b>。`
      +`<span class="muted"> 崖の下ほど“列挙型が減り一般論・留保が増える”＝AIの記憶に社名が無い＝GEO余地。</span>`
    : `該当データがありません（フィルタを緩めてください）。`;
  // ① タイプ表 ＋ ティア別構成
  let typeHtml=`<table><thead><tr><th>回答タイプ</th><th>件数</th><th>割合</th><th></th></tr></thead><tbody>`
    + typeOrder.filter(t=>tc[t]).map(t=>`<tr class="click" onclick='showTypeRows(${JSON.stringify(t)})'>
        <td>${esc(D.atype_labels[t]||t)}</td><td class="rate">${tc[t]}</td><td class="rate">${pct(tc[t])}%</td>
        <td class="muted">回答を見る ›</td></tr>`).join("") + `</tbody></table>`;
  const s2tiers=D.tiers.filter(t=>rows.some(r=>r.set==="set2"&&r.tier===t));
  if(s2tiers.length){
    const shownTypes=typeOrder.filter(t=>tc[t]);
    typeHtml+=`<div class="muted" style="margin:14px 0 6px">特異度ティア別（Set2）の回答タイプ構成</div>`
      +`<table><thead><tr><th>ティア</th>`+shownTypes.map(t=>`<th>${esc(D.atype_labels[t])}</th>`).join("")+`</tr></thead><tbody>`
      + s2tiers.map(ti=>{ const rr=rows.filter(r=>r.set==="set2"&&r.tier===ti);
          return `<tr><td><b>${ti} ${esc(D.tier_labels[ti]||"")}</b></td>`
            + shownTypes.map(t=>{const c=rr.filter(r=>r.atype===t).length;
                return `<td class="rate">${c?c:'<span class="muted">–</span>'}</td>`;}).join("")+`</tr>`;
        }).join("") + `</tbody></table>`;
  }
  $("#ctx-type").innerHTML=typeHtml;
  // ② 評価軸キーワード（未言及の有効回答）
  const order=D.criteria_order||Object.keys(D.criteria_labels||{});
  const cc={}; order.forEach(k=>cc[k]=0);
  miss.forEach(r=>(r.crit||[]).forEach(k=>{ if(k in cc) cc[k]++; }));
  const critList=order.map(k=>({k,label:D.criteria_labels[k]||k,c:cc[k]}))
    .filter(o=>o.c>0).sort((a,b)=>b.c-a.c);
  const missN=miss.length||1;
  const wrap=$("#chart-ctx").parentElement;
  if(ctxChart){ ctxChart.destroy(); ctxChart=null; }
  if(critList.length){
    wrap.style.display="";
    ctxChart=new Chart($("#chart-ctx"),{type:"bar",
      data:{labels:critList.map(o=>o.label),
        datasets:[{label:"該当回答数",data:critList.map(o=>o.c),
          backgroundColor:"rgba(26,127,75,.45)",borderColor:"#1a7f4b",borderWidth:1}]},
      options:{indexAxis:"y",responsive:true,maintainAspectRatio:false,
        plugins:{legend:{display:false}},
        scales:{x:{ticks:{color:"#5f6b7a"},grid:{color:"#e6ebf1"}},
                y:{ticks:{color:"#1f2a37"},grid:{display:false}}}}});
    $("#ctx-crit").innerHTML=`<table><thead><tr><th>評価軸</th><th>該当回答数</th><th>該当率</th><th></th></tr></thead><tbody>`
      + critList.map(o=>`<tr class="click" onclick='showCritRows(${JSON.stringify(o.k)})'>
          <td>${esc(o.label)}</td><td class="rate">${o.c}</td><td class="rate">${Math.round(o.c/missN*1000)/10}%</td>
          <td class="muted">回答を見る ›</td></tr>`).join("")
      + `</tbody></table><div class="muted" style="margin-top:6px">分母＝未言及の有効回答 ${miss.length} 件。頻出＝AIがその観点で他社を選定＝当社が同じ土俵で示すべき切り口。</div>`;
  } else {
    wrap.style.display="none";
    $("#ctx-crit").innerHTML=`<div class="muted">該当なし</div>`;
  }
  // ③ 競合の文脈スニペット
  const cagg={};
  miss.forEach(r=>(r.comp||[]).forEach(n=>{
    const o=cagg[n]||(cagg[n]={name:n,count:0,rows:[]}); o.count++; o.rows.push(r.i); }));
  const topc=Object.values(cagg).sort((a,b)=>b.count-a.count).slice(0,8);
  $("#ctx-comp").innerHTML = topc.length
    ? topc.map(o=>{ const snip=[];
        for(const idx of o.rows){ if(snip.length>=2) break;
          const ss=sentencesWith(ROWS[idx].answer,o.name); if(ss.length) snip.push({s:ss[0],i:idx}); }
        return `<div style="margin:6px 0 14px"><b class="tagM">${esc(o.name)}</b>
          <span class="muted">登場 ${o.count} 回答</span>
          ${snip.map(x=>`<div class="answer" style="max-height:none;margin:6px 0;cursor:pointer" onclick='showAnswer(${x.i})'>… ${esc(x.s)} …</div>`).join("")
            || `<div class="muted">前後文を抽出できませんでした（クリックで全文）</div>`}</div>`;
      }).join("")
    : `<div class="muted">競合候補なし（未言及の有効回答がありません）。</div>`;
  // ④ 自社ヒット文脈
  const owns=(DATA.meta.own_names||[]);
  $("#ctx-self").innerHTML = hitRows.length
    ? hitRows.map(r=>{ const ss=sentencesWithAny(r.answer,owns);
        const crit=(r.crit||[]).map(k=>`<span class="pill">${esc(D.criteria_labels[k]||k)}</span>`).join("");
        return `<div class="card" style="margin:8px 0;cursor:pointer" onclick='showAnswer(${r.i})'>
          <span class="tagH">●言及</span> <b>${esc(r.qid)}</b>
          <span class="pill">${esc(r.domain)}${r.tier?(" / "+r.tier):""}</span>
          <span class="pill">${esc(cmpLabel('run',r.run))}</span><br>
          <span class="muted">${esc(r.question)}</span>
          ${ss.length?`<div class="answer" style="max-height:none;margin:6px 0">… ${esc(ss[0])} …</div>`:""}
          ${crit?`<div style="margin-top:4px">共起する評価軸: ${crit}</div>`:""}</div>`;
      }).join("")
    : `<div class="muted">当社が言及された有効回答は このフィルタ内にありません。</div>`;
}
function showTypeRows(t){
  const st=FSTATE.context;
  const rows=ROWS.filter(r=>passFilter(r,st)&&r.atype===t);
  openModal(`<h3>回答タイプ「${esc(D.atype_labels[t]||t)}」（${rows.length}件）</h3>
    <div class="meta">クリックで回答全文。</div>`
    + rows.slice(0,300).map(r=>`<div class="card" style="margin:8px 0;cursor:pointer" onclick='showAnswer(${r.i})'>
        <span class="${r.hit?'tagH':'tagM'}">${r.hit?'●言及':'×未言及'}</span>
        <b>${esc(r.qid)}</b> <span class="pill">${esc(r.domain)}${r.tier?(" / "+r.tier):""}</span>
        <span class="pill">${esc(r.date)}</span><br><span class="muted">${esc(r.question)}</span></div>`).join("")
    + (rows.length>300?`<div class="muted">（先頭300件を表示）</div>`:""));
}
function showCritRows(k){
  const st=FSTATE.context;
  const rows=ROWS.filter(r=>passFilter(r,st)&&isValid(r)&&!r.hit&&(r.crit||[]).includes(k));
  openModal(`<h3>評価軸「${esc(D.criteria_labels[k]||k)}」を含む未言及回答（${rows.length}件）</h3>
    <div class="meta">AIがこの観点で他社を語っている回答です。クリックで全文。</div>`
    + rows.slice(0,300).map(r=>`<div class="card" style="margin:8px 0;cursor:pointer" onclick='showAnswer(${r.i})'>
        <b>${esc(r.qid)}</b> <span class="pill">${esc(r.domain)}${r.tier?(" / "+r.tier):""}</span>
        <span class="pill">${esc(r.date)}</span>
        ${(r.comp&&r.comp.length)?`<br>競合候補: `+r.comp.slice(0,6).map(n=>`<span class="pill bad">${esc(n)}</span>`).join(""):""}
        <br><span class="muted">${esc(r.question)}</span></div>`).join("")
    + (rows.length>300?`<div class="muted">（先頭300件を表示）</div>`:""));
}

// ─────────────────────────────────────────── 全回サマリー
let runsChart=null;
// 計測の断絶（config.measurement_notes）：回キー YYYYMMDD_… から日付を取り、断絶日をまたぐ位置を求める
const NOTES = ((DATA.home||{}).measurement_notes||[]).slice().sort((a,b)=>a.date<b.date?-1:1);
function unitDate(key){ const m=/^(\d{4})(\d{2})(\d{2})_/.exec(key||""); return m?`${m[1]}-${m[2]}-${m[3]}`:""; }
function breakMarks(units){ // [{idx, note}]：units[idx-1] が断絶日より前、units[idx] が断絶日以後
  const out=[];
  NOTES.forEach(n=>{ for(let i=1;i<units.length;i++){
    const a=unitDate(units[i-1]), b=unitDate(units[i]);
    if(a && b && a<n.date && b>=n.date){ out.push({idx:i,note:n}); break; } } });
  return out;
}
function crossesBreak(keyA,keyB){ // A→B の比較が断絶日をまたぐなら該当 note を返す
  const a=unitDate(keyA), b=unitDate(keyB); if(!a||!b) return null;
  const lo=a<b?a:b, hi=a<b?b:a;
  return NOTES.find(n=>lo<n.date && hi>=n.date)||null;
}
// annotation プラグインを使わずに、断絶より前を薄く塗り・縦の破線を引くインラインプラグイン
const BREAK_PLUGIN={id:"breakLines",
  beforeDatasetsDraw(chart,args,opts){
    const marks=(opts&&opts.marks)||[]; if(!marks.length) return;
    const {ctx,chartArea:ar,scales:{x}}=chart;
    marks.forEach(m=>{
      const xm=(x.getPixelForValue(m.idx-1)+x.getPixelForValue(m.idx))/2;
      ctx.save();
      ctx.fillStyle="rgba(95,107,122,.10)"; ctx.fillRect(ar.left,ar.top,xm-ar.left,ar.bottom-ar.top);
      ctx.strokeStyle="#b7791f"; ctx.lineWidth=2; ctx.setLineDash([5,4]);
      ctx.beginPath(); ctx.moveTo(xm,ar.top); ctx.lineTo(xm,ar.bottom); ctx.stroke();
      ctx.setLineDash([]); ctx.fillStyle="#b7791f"; ctx.font="11px sans-serif"; ctx.textAlign="right";
      ctx.fillText(`← ${m.note.date.slice(5).replace("-","/")} 以前は比較不可`,xm-4,ar.top+12);
      ctx.restore();
    });
  }};
function passRunsFilter(r,st){
  if(st.domain && r.domain!==st.domain) return false;
  if(st.tier && r.tier!==st.tier) return false;
  if(st.set && r.set!==st.set) return false;
  if(st.model && r.model!==st.model) return false;
  return true;
}
function runRate(rows){ const v=rows.filter(isValid); if(!v.length) return null;
  return Math.round(v.filter(r=>r.hit).length/v.length*1000)/10; }
function renderRuns(){
  const st=FSTATE.runs; const gran=st.gran||"run";
  const units=cmpUnits(gran);
  const base=ROWS.filter(r=>passRunsFilter(r,st));
  const keyOf = r => gran==="timing"?r.timing:r.run;
  const per=units.map(u=>{
    const rr=base.filter(r=>keyOf(r)===u);
    const v=rr.filter(isValid);
    const comps={}; rr.forEach(r=>{ if(!r.hit)(r.comp||[]).forEach(n=>comps[n]=(comps[n]||0)+1); });
    const topc=Object.entries(comps).sort((a,b)=>b[1]-a[1])[0];
    return {u,label:cmpLabel(gran,u),n:rr.length,valid:v.length,
      err:rr.filter(r=>r.atype==="error").length, emp:rr.filter(r=>r.atype==="empty").length,
      hits:v.filter(r=>r.hit).length, rate:runRate(rr), ncomp:Object.keys(comps).length, topc};
  });
  const allValid=base.filter(isValid); const pooled=runRate(base);
  const active=per.filter(p=>p.rate!=null);
  $("#runs-summary").innerHTML =
    `全 <b>${units.length}</b> ${gran}（うち有効な回 ${active.length}）。全回プールの出現率 <b>${pooled==null?"–":pooled+"%"}</b>`
    +`（有効行 ${allValid.length} 中 ヒット ${allValid.filter(r=>r.hit).length}）。`
    +`<span class="muted"> エラーのみの回は集計から自動除外。</span>`;
  if(runsChart){ runsChart.destroy(); runsChart=null; }
  const marks=breakMarks(per.map(p=>p.u));
  const noteSets=marks.map(m=>({label:`┆ ${m.note.date} ${m.note.label}：${m.note.note||""}`,data:[],
    borderColor:"#b7791f",borderDash:[5,4],borderWidth:2,backgroundColor:"rgba(95,107,122,.14)",pointRadius:0}));
  runsChart=new Chart($("#chart-runs"),{type:"line",
    data:{labels:per.map(p=>p.label),
      datasets:[{label:"出現率%",data:per.map(p=>p.rate),spanGaps:true,tension:.25,
        borderColor:"#1d4f91",backgroundColor:"rgba(29,79,145,.2)",fill:true,
        pointRadius:4,pointBackgroundColor:"#1d4f91"}, ...noteSets]},
    plugins:[BREAK_PLUGIN],
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:marks.length>0,labels:{color:"#1f2a37",boxWidth:18}},breakLines:{marks}},
      scales:{x:{ticks:{color:"#5f6b7a"},grid:{color:"#e6ebf1"}},
              y:{beginAtZero:true,ticks:{color:"#5f6b7a"},grid:{color:"#e6ebf1"}}}}});
  $("#runs-table").innerHTML=
    `<table><thead><tr><th>回</th><th>有効行</th><th>hits</th><th>出現率</th><th>エラー</th><th>空</th><th>ユニーク競合</th><th>トップ競合</th></tr></thead><tbody>`
    + per.map(p=>`<tr class="click" onclick='showRunRows(${JSON.stringify(gran)},${JSON.stringify(p.u)})'>
        <td><b>${esc(p.label)}</b></td><td class="rate">${p.valid}</td><td class="rate">${p.hits}</td>
        <td class="rate">${p.rate==null?'<span class="muted">–</span>':p.rate+"%"}</td>
        <td class="rate">${p.err||0}</td><td class="rate">${p.emp||0}</td><td class="rate">${p.ncomp}</td>
        <td>${p.topc?`<span class="pill bad">${esc(p.topc[0])} ${p.topc[1]}</span>`:'<span class="muted">–</span>'}</td>
      </tr>`).join("") + `</tbody></table>`;
  const aggTable=(ax,vals)=>{
    const rowsH=vals.filter(v=>base.some(r=>r[ax]===v)).map(v=>{
      const pooledV=runRate(base.filter(r=>r[ax]===v));
      const rates=per.map(p=>runRate(base.filter(r=>keyOf(r)===p.u && r[ax]===v))).filter(x=>x!=null);
      let spread='<span class="muted">–</span>';
      if(rates.length){ const mn=Math.min(...rates),mx=Math.max(...rates);
        spread=`${mn}%〜${mx}%<span class="muted"> (幅 ${Math.round((mx-mn)*10)/10}pt)</span>`; }
      return `<tr><td>${esc(axisLabel(ax,v))}</td><td class="rate">${pooledV==null?"–":pooledV+"%"}</td><td>${spread}</td></tr>`;
    }).join("");
    return `<table><thead><tr><th>${AXES[ax]}</th><th>全回プール</th><th>回ごとのブレ(最小〜最大)</th></tr></thead>`
      +`<tbody>${rowsH||`<tr><td colspan="3" class="muted">該当なし</td></tr>`}</tbody></table>`;
  };
  $("#runs-agg-domain").innerHTML=aggTable("domain",D.domains);
  $("#runs-agg-tier").innerHTML = D.tiers.length? aggTable("tier",D.tiers) : `<div class="muted">Set2 特異度データがありません。</div>`;
}
function showRunRows(gran,u){
  const st=FSTATE.runs;
  const rr=ROWS.filter(r=>passRunsFilter(r,st) && (gran==="timing"?r.timing:r.run)===u);
  const hits=rr.filter(r=>r.hit);
  openModal(`<h3>${esc(cmpLabel(gran,u))} のヒット回答（${hits.length}件）</h3>
    <div class="meta">この回で当社が言及された回答。有効行 ${rr.filter(isValid).length}／全 ${rr.length}行。クリックで全文。</div>`
    + (hits.length? hits.map(r=>`<div class="card" style="margin:8px 0;cursor:pointer" onclick='showAnswer(${r.i})'>
        <span class="tagH">●言及</span> <b>${esc(r.qid)}</b>
        <span class="pill">${esc(r.domain)}${r.tier?(" / "+r.tier):""}</span><br>
        <span class="muted">${esc(r.question)}</span></div>`).join("")
      : `<div class="muted">この回に当社ヒットはありません。</div>`));
}
function buildRunsFilters(){
  const mount=$("#filters-runs"); mount.innerHTML="";
  const mk=(label,build,onchange)=>{ const f=document.createElement("div"); f.className="f";
    const lab=document.createElement("label"); lab.textContent=label;
    const sel=document.createElement("select"); build(sel);
    sel.onchange=()=>{ onchange(sel.value); renderRuns(); };
    f.appendChild(lab); f.appendChild(sel); mount.appendChild(f); };
  mk("粒度",sel=>{sel.innerHTML=`<option value="run">run（ファイル1つ＝1回）</option><option value="timing">timing（r1/r2をまとめ）</option>`;
    sel.value=FSTATE.runs.gran||"run";}, v=>FSTATE.runs.gran=v);
  const addF=(label,k,vals,lab)=>{ if(!vals||!vals.length) return;
    mk(label,sel=>{sel.innerHTML=`<option value="">すべて</option>`+vals.map(v=>`<option value="${esc(v)}">${esc(lab?lab(v):v)}</option>`).join("");
      sel.value=FSTATE.runs[k]||"";}, v=>FSTATE.runs[k]=v); };
  addF("ドメイン","domain",D.domains,v=>`${v}｜${D.domain_labels[v]||""}`);
  addF("特異度","tier",D.tiers,v=>`${v} ${D.tier_labels[v]||""}`);
  addF("質問セット","set",D.sets,null);
  addF("モデル","model",D.models,null);
  const rf=document.createElement("div"); rf.className="f";
  rf.appendChild(document.createElement("label"));
  const rb=document.createElement("button"); rb.className="reset"; rb.textContent="リセット";
  rb.onclick=()=>{ const g=FSTATE.runs.gran||"run"; FSTATE.runs={gran:g}; buildRunsFilters(); renderRuns(); };
  rf.appendChild(rb); mount.appendChild(rf);
}

// ─────────────────────────────────────────── ホーム（関係者向けの入口）
const REP = DATA.reports||{};
const HOME = DATA.home||{};
const escAttr = s => esc(s).replace(/"/g,"&quot;");
const HELP = {
  rate:"出現率＝AIの回答に当社（オンワード）が登場した割合。分母は有効行（エラー・空回答を除いた回答数）で、全回サマリーと同じ計算です。",
  valid:"有効行＝エラー（API失敗など）と空回答を除いた、中身のある回答の件数。出現率の分母に使います。",
  nom:"指名＝質問文に社名が入っている質問（Set2 の D1、Set1 の社名系）。非指名＝社名を含まない質問（Set2 の D2〜D4、Set1 の一般質問）。増やしたいのは非指名での出現です。",
  ground:"非グラウンディング＝AIが Web 検索をせず、学習済みの知識だけで答える方式。サイトを直しても、次にAIが学習し直すまで結果に反映されにくい。",
};
const help = k => `<span class="help" tabindex="0" data-tip="${escAttr(HELP[k])}" aria-label="${escAttr(HELP[k])}">?</span>`;
const nodata = msg => `<div class="nodata"><b>データなし</b>${msg?`<span class="muted">（${esc(msg)}）</span>`:""}</div>`;
const arr = x => Array.isArray(x)?x:[];
const OWN_LC = (DATA.meta.own_names||[]).map(s=>String(s).toLowerCase());
function isNominated(r){ // 指名＝Set2 の D1／Set1 は質問文に社名を含むもの（社名系）
  if(r.set==="set2") return r.tier==="D1";
  const q=(r.question||"").toLowerCase(); return OWN_LC.some(o=>o && q.indexOf(o)>=0);
}
function mmdd(key){ const d=unitDate(key); return d?d.slice(5).replace("-","/"):String(key||""); }
function homeStat(rows){ const v=rows.filter(isValid);
  return {n:rows.length,valid:v.length,hits:v.filter(r=>r.hit).length,rate:runRate(rows),
          err:rows.filter(r=>r.atype==="error").length,emp:rows.filter(r=>r.atype==="empty").length}; }
function homeDelta(a,b,brk,hasPrev){
  if(!hasPrev) return `<span class="delta-na">前回データなし</span>`;
  if(brk) return `<span class="delta-na">比較対象外</span><span class="muted">（${esc(brk.date)} ${esc(brk.label)} をまたぐため）</span>`;
  if(a==null||b==null) return `<span class="delta-na">比較できません</span>`;
  const d=Math.round((b-a)*10)/10;
  if(d>0) return `<span class="delta-up">▲ 改善 +${d}pt</span>`;
  if(d<0) return `<span class="delta-down">▼ 低下 ${d}pt</span>`;
  return `<span class="delta-flat">± 横ばい 0pt</span>`;
}
const pct = v => v==null?"–":v+"%";
const pills = (xs,cls) => arr(xs).length ? arr(xs).map(x=>`<span class="pill ${cls||""}">${esc(x)}</span>`).join(" ") : `<span class="muted">–</span>`;
const ownerOf = label => (HOME.domain_owner||{})[label] || "未割当";
function topActions(){
  const ins=REP.insights; if(!ins || !Array.isArray(ins.actions)) return null;
  const PRI={"高":0,"中":1,"低":2};
  return ins.actions.filter(a=>a && typeof a==="object").map((a,i)=>({a,i}))
    .sort((x,y)=>((PRI[x.a.priority]??9)-(PRI[y.a.priority]??9))||x.i-y.i).slice(0,3).map(o=>o.a);
}
// 質問セット別（Set1＝一般質問／Set2＝特異度つき）の出現率と前回比 ＋ Set2 の特異度カーブ（D1→D4）
const SET_LABELS={set1:"Set1（一般質問）",set2:"Set2（特異度つき D1〜D4）"};
function homeSetBreakdown(lr,pr,brk,hasPrev){
  const sets=D.sets.filter(s=>lr.some(r=>r.set===s));
  if(!sets.length) return "";
  const items=sets.map(s=>{ const c=homeStat(lr.filter(r=>r.set===s)), p=homeStat(pr.filter(r=>r.set===s));
    return `<span class="set-item"><b>${esc(SET_LABELS[s]||s)}</b> <b class="set-val">${pct(c.rate)}</b>
      <span class="muted">（${c.hits} / ${c.valid}）</span> 前回比 ${homeDelta(p.rate,c.rate,brk,hasPrev&&p.valid>0)}</span>`; }).join("");
  // Set2 の特異度カーブ：有効行のある tier だけを並べ、D1 以外で最初に 10% 未満になる所を「崖」とする（describeCliff と同じ基準）
  const s2=lr.filter(r=>r.set==="set2");
  const seg=["D1","D2","D3","D4"].map(t=>[t,runRate(s2.filter(r=>r.tier===t))]).filter(([,v])=>v!=null);
  let curve="";
  if(seg.length){
    const ci=seg.findIndex(([t,v],i)=>i>0 && v<10);
    curve=`<div class="set-curve"><b>Set2 特異度カーブ</b>：`
      + seg.map(([t,v])=>`${t} ${esc(D.tier_labels[t]||"")} <b>${v}%</b>`).join(" → ")
      + (ci>0?`　<span class="delta-down">崖は ${seg[ci-1][0]}→${seg[ci][0]}</span><span class="muted">（${esc(D.tier_labels[seg[ci][0]]||"")}で ${seg[ci][1]}% に低下）</span>`:"")
      + `</div>`;
  }
  return `<div class="set-box"><div class="set-row"><span class="muted">質問セット別（ヒット / 有効行）</span>${items}</div>${curve}</div>`;
}
function renderHome(){
  // 「回」はタイミング単位（同一タイミングの r1/r2 はまとめる。1タイミング1回なら全回サマリーの run 表示と同値）
  const units=D.timings.filter(u=>runRate(ROWS.filter(r=>r.timing===u))!=null);  // 有効行のある回だけ
  const L=units[units.length-1]||null, P=units.length>1?units[units.length-2]:null;
  const lr=L?ROWS.filter(r=>r.timing===L):[], pr=P?ROWS.filter(r=>r.timing===P):[];
  const brk=P?crossesBreak(P,L):null;
  const lab=k=>{ const n=(D.timing_runs[k]||[]).length; return (D.timing_labels[k]||k)+(n>1?`（${n}回分）`:""); };
  const latestTiming=L||"";
  const insMismatch = REP.insights && REP.insights_timing && REP.insights_timing!==latestTiming;
  const insNote = insMismatch
    ? `<div class="note" style="margin:0 0 10px">示唆は <b>${esc(mmdd(REP.insights_timing))} 実行分</b>に基づく（ホームの最新回 ${esc(mmdd(L))} とは異なる回です）。</div>` : "";
  $("#home-asof").innerHTML = L
    ? `最新回：<b>${esc(lab(L))}</b>${P?` ／ 前回：${esc(lab(P))}`:""} ／ 生成：${esc(DATA.meta.generated_at)}`
      + ` <button class="noprint" style="margin-left:8px" onclick="window.print()">印刷（A4横）</button>` : "";

  // 1) 今回の結論
  if(!L){ $("#home-concl").innerHTML=nodata("有効な回答のある回がありません"); }
  else {
    const all=homeStat(lr), allP=homeStat(pr);
    const nom=homeStat(lr.filter(isNominated)), non=homeStat(lr.filter(r=>!isNominated(r)));
    const nomP=homeStat(pr.filter(isNominated)), nonP=homeStat(pr.filter(r=>!isNominated(r)));
    let verdict="";
    if(non.rate!=null){
      if(non.rate<5 && (nom.rate??0)>non.rate) verdict=`<b>非指名での出現が課題</b>（社名を出さずに聞かれると当社が挙がらない）。`;
      else if(non.rate>=5) verdict=`非指名でも出現が見られます。`;
      else verdict=`指名・非指名とも出現が少ない状態です。`;
    }
    const kpi=(title,s,sp)=>`<div class="kpi-box"><div class="lbl">${title}</div>
      <div class="val">${pct(s.rate)}</div>
      <div class="dt">有効行 ${s.valid} 件中 ${s.hits} 件 ／ 前回比 ${homeDelta(sp.rate,s.rate,brk,!!P)}</div></div>`;
    $("#home-concl").innerHTML =
      `<p class="concl">${esc(lab(L))} 実行分の出現率${help("rate")}は <b>${pct(all.rate)}</b>（有効行${help("valid")} ${all.valid} 件中 ${all.hits} 件）、`
      +`前回比 ${homeDelta(allP.rate,all.rate,brk,!!P)}。`
      +`指名質問${help("nom")}では <b>${pct(nom.rate)}</b>、非指名質問では <b>${pct(non.rate)}</b>。${verdict}</p>`
      +`<div class="kpis">${kpi("全体の出現率",all,allP)}${kpi("指名質問（D1・社名系）",nom,nomP)}${kpi("非指名質問（D2以下・Set1一般）",non,nonP)}</div>`
      + homeSetBreakdown(lr,pr,brk,!!P);
  }

  // 2) 計測上の注意
  const items=[];
  NOTES.forEach(n=>items.push(`<b>計測の断絶：${esc(n.date)} ${esc(n.label)}</b> — ${esc(n.note||"")}。`
    +(brk===n?`今回の前回比はこの断絶をまたぐため比較対象外です。`:``)
    +`<span class="muted">（全回サマリーの推移チャートに破線で表示）</span>`));
  if(L){
    const s=homeStat(lr);
    items.push(`最新回（${esc(lab(L))}）：全 ${s.n} 行のうち <b>有効行 ${s.valid}</b>${help("valid")}（エラー ${s.err} 件・空回答 ${s.emp} 件）。出現率はこの有効行を分母に計算しています。`);
    const idx=arr(REP.index).find(e=>e.timing_id===latestTiming);
    items.push(idx
      ? `参考：monitoring 側の集計（index.json）は出現率 ${esc(idx.overall_mean)}%・安定度 ${esc(idx.stability)}%。こちらは空回答も分母に含むため、上の数値より低く出ることがあります。`
      : `参考：monitoring 側の集計（index.json）：<b>データなし</b>`);
    const models = (idx && arr(idx.models).length) ? arr(idx.models) : [...new Set(lr.map(r=>r.model).filter(Boolean))];
    const kw=arr(HOME.grounding_model_keywords).map(s=>String(s).toLowerCase()).filter(Boolean);
    const grounded=models.filter(m=>kw.some(k=>String(m).toLowerCase().indexOf(k)>=0));
    items.push(`稼働モデル：${models.map(m=>`<span class="pill">${esc(m)}｜${grounded.includes(m)?"Web検索あり":"非グラウンディング"}</span>`).join(" ")}${help("ground")}`
      + (models.length && !grounded.length ? `<br><b>学習データ上の認知を測定中。サイト施策の効果は週単位では動きにくい。</b>` : ``));
  }
  if(!REP.available) items.push(`monitoring の reports フォルダを読めません：<b>データなし</b> <span class="muted">（${esc(REP.dir||"reports_dir 未設定")}）</span>`);
  if(insMismatch) items.push(`次にやること・Q2・Q3 の示唆は <b>${esc(mmdd(REP.insights_timing))} 実行分</b>（${esc(REP.insights_file)}）に基づきます。`);
  $("#home-notes").innerHTML=`<ul class="notes" style="margin:0;padding-left:20px">${items.map(x=>`<li>${x}</li>`).join("")}</ul>`;

  // 3) 次にやること
  const top=topActions();
  const priCls=p=>p==="高"?"":(p==="中"?"mid":"low");
  $("#home-actions").innerHTML = top==null
    ? nodata(REP.available?"insights_*.json がありません／読めません":"reports_dir が見つかりません")
    : (!top.length ? nodata("改善アクションは 0 件です")
      : insNote + `<div class="acts">` + top.map((a,i)=>`<div class="act">
          <div class="hd"><span class="pill pri ${priCls(a.priority)}">優先度 ${esc(a.priority||"–")}</span>
            ${i+1}. ${esc(a.domain)} × ${esc(a.type)} <span class="pill own">担当 ${esc(ownerOf(a.domain))}</span></div>
          <div class="muted">出現率 ${a.rate!=null?esc(a.rate)+"%":"–"}${a.total!=null?`（${esc(a.total)} 件中 ${esc(a.miss??"–")} 件で非出現）`:""}</div>
          <div class="k">打ち手（playbook）</div><div>${esc(a.playbook||"–")}</div>
          <div class="k">AIが重視している観点（cover_attributes）</div><div>${pills(a.cover_attributes)}</div>
          <div class="k">代わりに想起されている競合（beating_competitors）</div><div>${pills(a.beating_competitors,"bad")}</div>
        </div>`).join("") + `</div>`);

  // 4) Q1 出た／消えた（過去回比較の反転ロジックを再利用）
  if(!P){ $("#home-q1").innerHTML=nodata("比較できる前回がありません"); }
  else {
    const qa=byQid(pr), qb=byQid(lr);
    const common=Object.keys(qa).filter(q=>q in qb);
    const gained=common.filter(q=>!qa[q].hit&&qb[q].hit), lost=common.filter(q=>qa[q].hit&&!qb[q].hit);
    const emptyNow=q=>qb[q].idxs.every(i=>!isValid(ROWS[i]));
    const MAX=12; const list=[...gained.map(q=>["g",q]),...lost.map(q=>["l",q])];
    const rowsH=list.slice(0,MAX).map(([k,q])=>{ const o=qb[q];
      return `<tr class="click" onclick='showFlip(${JSON.stringify(q)},${JSON.stringify(qa[q].rep)},${JSON.stringify(qb[q].rep)})'>
        <td class="nw">${k==="g"?'<span class="tagH">出た↑</span>':'<span class="tagM">消えた↓</span>'+(emptyNow(q)?'<span class="muted">（今回は空回答）</span>':'')}</td>
        <td><b>${esc(q)}</b></td><td class="muted">${esc((o.q||"").slice(0,40))}</td></tr>`; }).join("");
    $("#home-q1").innerHTML =
      (brk?`<div class="note" style="margin:0 0 8px">${esc(brk.date)} の断絶をまたぐ比較です。参考程度に見てください。</div>`:"")
      +`<div class="muted" style="margin-bottom:6px">${esc(lab(P))} → ${esc(lab(L))}：共通質問 ${common.length} 件のうち
        <span class="tagH">出た↑ ${gained.length}</span> ／ <span class="tagM">消えた↓ ${lost.length}</span></div>`
      +(rowsH?`<table><thead><tr><th>変化</th><th>質問ID</th><th>質問</th></tr></thead><tbody>${rowsH}</tbody></table>`
             :`<div class="muted">反転した質問はありません（前回と同じ結果）。</div>`)
      +(list.length>MAX?`<div class="muted">ほか ${list.length-MAX} 件は「過去回比較」タブで確認できます。</div>`:"")
      +`<div class="muted noprint" style="margin-top:4px">行クリックで前回・今回の回答全文を並べて比較。</div>`;
  }

  // 4) Q2 代わりに誰を・どんな観点で
  const ex=REP.insights && REP.insights.extraction;
  if(!ex || typeof ex!=="object"){ $("#home-q2").innerHTML=nodata(REP.available?"insights の extraction がありません":"reports_dir が見つかりません"); }
  else {
    const t5=(xs,title)=>{ const top5=arr(xs).filter(Array.isArray).slice(0,5);
      return `<table><thead><tr><th>${title}</th><th style="text-align:right">回答数</th></tr></thead><tbody>`
        + (top5.length?top5.map(([n,c],i)=>`<tr><td>${i+1}. ${esc(n)}</td><td class="num">${esc(c)}</td></tr>`).join("")
                      :`<tr><td colspan="2" class="muted">データなし</td></tr>`) + `</tbody></table>`; };
    $("#home-q2").innerHTML = (insMismatch?`<div class="muted" style="margin-bottom:4px">※ ${esc(mmdd(REP.insights_timing))} 実行分の示唆</div>`:"")
      +`<div class="muted" style="margin-bottom:6px">当社が出なかった回答 ${esc(ex.nd_count??"–")} 件で、AIが挙げたもの（上位5件）</div>`
      +`<div class="grid g2" style="gap:10px">${t5(ex.competitors,"代わりに挙がった競合")}${t5(ex.attributes,"重視された観点")}</div>`;
  }

  // 4) Q3 次の回までに何を直すか（会議で担当・期限を書き込む表）
  $("#home-q3").innerHTML = top==null
    ? nodata(REP.available?"insights_*.json がありません／読めません":"reports_dir が見つかりません")
    : (!top.length ? nodata("改善アクションは 0 件です")
      : `<table class="memo"><thead><tr><th>#</th><th>優先度</th><th>対象（事業ドメイン × 質問タイプ）</th><th>何を直すか</th>
          <th>担当事業</th><th>担当者</th><th>期限</th><th>メモ</th></tr></thead><tbody>`
        + top.map((a,i)=>`<tr><td>${i+1}</td><td>${esc(a.priority||"–")}</td><td>${esc(a.domain)} × ${esc(a.type)}</td>
            <td>${esc(a.playbook||"–")}</td><td><b>${esc(ownerOf(a.domain))}</b></td>
            <td class="blank"></td><td class="blank"></td><td class="blank"></td></tr>`).join("")
        + `</tbody></table>`);

  // 5) 詳しく見るには
  const LINKS=[["s-runs","全回サマリー","全回の出現率の推移・回ごとのブレ・計測の断絶"],
    ["s-compare","過去回比較","2回を選んで、出た／消えた質問と競合の増減を比べる"],
    ["s-comp","競合共起","当社が出ない回答で、代わりに挙がる競合のランキング"],
    ["s-context","回答分析","AIがどう答えているか（回答タイプ・重視する観点・文脈）"],
    ["s-cross","多軸クロス集計","ドメイン×特異度などで、どこから当社が消えるか（崖）"],
    ["s-self","自社突合","サイト掲載実績のうち、AIに言及されていないもの"],
    ["s-url","引用URL","AIが引用したページ（Web検索型モデルの導入後に有効）"]];
  $("#home-links").innerHTML = LINKS.map(([id,t,d])=>
      `<button onclick='showTab(${JSON.stringify(id)});window.scrollTo(0,0)'><b>${esc(t)} ›</b><br><span class="muted">${esc(d)}</span></button>`).join("")
    + `<div class="muted" style="grid-column:1/-1">※ monitoring に内蔵の dashboard / insights は運用者向けです。関係者はこの画面（analysis.html）だけを見れば足ります。</div>`;
}
window.addEventListener("beforeprint",()=>$$("#s-home details").forEach(d=>d.open=true));

// ── init
buildFilters("filters-comp","comp",renderComp);
buildFilters("filters-context","context",renderContext);
buildFilters("filters-url","url",renderUrl);
buildRunsFilters();
// 過去回比較：粒度・A・B セレクタ
FSTATE.compare = {gran:"run"};
(function(){
  const mount=$("#filters-compare");
  const mkSel=(label,onchange,build)=>{ const f=document.createElement("div"); f.className="f";
    const lab=document.createElement("label"); lab.textContent=label;
    const sel=document.createElement("select"); build(sel); sel.onchange=()=>onchange(sel.value);
    f.appendChild(lab); f.appendChild(sel); mount.appendChild(f); return sel; };
  let selA, selB;
  const fillAB=()=>{ const g=FSTATE.compare.gran, u=cmpUnits(g);
    const opt=k=>`<option value="${esc(k)}">${esc(cmpLabel(g,k))}</option>`;
    selA.innerHTML=u.map(opt).join(""); selB.innerHTML=u.map(opt).join("");
    FSTATE.compare.A=u[u.length-2]||u[0]; FSTATE.compare.B=u[u.length-1];
    selA.value=FSTATE.compare.A; selB.value=FSTATE.compare.B; };
  mkSel("粒度",v=>{FSTATE.compare.gran=v; fillAB(); renderCompare();},sel=>{
    sel.innerHTML=`<option value="run">run（ファイル1つ＝1回）</option><option value="timing">timing（r1/r2をまとめ）</option>`;
    sel.value="run";});
  selA=mkSel("A（過去）",v=>{FSTATE.compare.A=v; renderCompare();},()=>{});
  selB=mkSel("B（比較）",v=>{FSTATE.compare.B=v; renderCompare();},()=>{});
  fillAB();
})();
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
// ─────────────────────────────────────────── Google参考値（GSC）
const G = Object.assign({available:false,periods:[],keywords:{},ref:{},daily:{},ai_pages:{},log:[],warnings:[],
  sites:{},action_notes:[]}, DATA.google||{});
const GS = {period:"latest", set:"", domain:"", owner:"", act:"", cell:"", aiSite:"", aiPeriod:""};
const G_SEARCH_PERIODS = arr(G.periods).filter(p=>arr(p.sites).length);
const G_SITE_ORDER = Object.keys(G.sites||{});
const siteLabel = s => ((G.sites||{})[s]||{}).label || s;
const byEndDesc = (a,b) => a.end<b.end?1:(a.end>b.end?-1:0);
HELP.gsc = "GSC（Google Search Console）＝自社サイトが Google 検索でどう表示されたかの公式データ。自社サイトのみ・表示されたときの平均順位で、競合は見えません。毎月、前月1日〜末日で書き出したファイルを置くと月次で比較できます。";
// やること：Claude の出現 × Google の順位 → 担当が次にやること
const G_ACTIONS = {
  fix:  {label:"AI向けに直す",           cls:"q-geo",  pri:"高", desc:"Google は10位以内なのに Claude に出ない"},
  near: {label:"あと一歩（11〜20位）",    cls:"q-weak", pri:"中", desc:"もう少しで Google の1ページ目"},
  rank: {label:"順位を上げる（21位〜）",  cls:"q-weak", pri:"中", desc:"Google でも AI でも弱い"},
  check:{label:"検索チェック待ち",        cls:"q-none", pri:"–",  desc:"GSC にデータがない"},
  keep: {label:"維持",                   cls:"q-both", pri:"–",  desc:"Claude で出現している"},
};
const G_ACT_ORDER = ["fix","near","rank","check","keep"];
function gAction(cy,b){
  if(!cy) return "";
  if(cy==="hit") return "keep";
  if(!b || b.pos==null) return "check";
  return b.pos<=10 ? "fix" : (b.pos<=20 ? "near" : "rank");
}
// 判定の根拠（4象限のマス目）：縦＝Claude、横＝Google
const G_CELLS = {
  "hit|top": {label:"維持",               cls:"q-both", desc:"両方で出ている"},
  "hit|low": {label:"維持",               cls:"q-both", desc:"Google は伸ばせる"},
  "hit|none":{label:"維持",               cls:"q-both", desc:"Google は未確認"},
  "miss|top":{label:"AI向けに直す",        cls:"q-geo",  desc:"ページはあるが AI に届いていない"},
  "miss|low":{label:"あと一歩／順位を上げる",cls:"q-weak", desc:"Google でも弱い"},
  "miss|none":{label:"検索チェック待ち",    cls:"q-none", desc:"Google データなし"},
};
const G_TODO = {
  fix:{title:"1. AI向けにページを直す", pri:"高",
    why:"Google では10位以内に自社ページがあるのに、Claude には出てこない設問です。ページはすでにあるので、AI が引用しやすい形に直すのが近道です。",
    todo:["本文に社名・ブランド名（オンワードコーポレートデザイン、ラフィーリア等）を明記する",
          "導入実績・顧客名を具体的に書く","FAQ・比較表など、質問に直接答える形にする",
          "外部メディア・業界サイトでの言及を増やす（プレスリリース・寄稿・事例掲載）"]},
  near:{title:"2. あと一歩で Google の1ページ目", pri:"中",
    why:"Google で11〜20位の設問です。少しの改善で1ページ目に入り、AI にも届きやすくなります。",
    todo:["タイトル・見出しにこの語を入れる","関連ページから内部リンクを張る","情報を最新にし、事例を追加する"]},
  check:{title:"3. Google でも確認できていない設問", pri:"次の作業",
    why:"GSC にデータがない設問です（検索されていない、または自社が上位1,000件に入っていない）。実際の検索結果を確認します。",
    todo:["毎月の検索チェック（ブックマークレット）で、設問文と観測キーワードを Google で確認する",
          "確認して自社が出ない語は、新しいページ（事例・解説）の候補にする"]},
};
const fmtN = v => v==null ? "–" : Number(v).toLocaleString("ja-JP");
const fmtPos = v => v==null ? "–" : (Math.round(v*10)/10).toFixed(1)+"位";

// 期間の選び方：latest＝サイトごとに最新のファイル（月次があれば最新の月次、無ければ最新の期間集計）
function gSitePeriods(sel){
  const out={};
  if(sel==="latest"){
    G_SITE_ORDER.forEach(s=>{
      const c=G_SEARCH_PERIODS.filter(p=>p.sites.includes(s));
      const p=c.filter(p=>p.monthly).sort(byEndDesc)[0] || c.filter(p=>!p.monthly).sort(byEndDesc)[0];
      if(p) out[s]=p;
    });
  } else {
    const p=G_SEARCH_PERIODS.find(p=>p.key===sel);
    if(p) p.sites.forEach(s=>out[s]=p);
  }
  return out;
}
// Claude 側の集計期間：Google 側が月次ならその月、月次でなければ全期間
function gClaudeWindow(sel){
  const monthly=G_SEARCH_PERIODS.filter(p=>p.monthly).sort(byEndDesc);
  let p=null;
  if(sel==="latest") p=monthly[0]||null;
  else { const x=G_SEARCH_PERIODS.find(q=>q.key===sel); if(x && x.monthly) p=x; }
  return p ? {start:p.start,end:p.end,label:`${p.key}（Google と同じ月）`}
           : {start:"",end:"",label:"全期間（Google 側が月単位のファイルでないため）"};
}
function gClaudeStats(win){ // qid -> {n, h, comp:{社名:回数}}（有効行・期間内）
  const m={};
  ROWS.forEach(r=>{ if(!isValid(r)) return;
    if(win.start && (r.date<win.start || r.date>win.end)) return;
    const o=m[r.qid]||(m[r.qid]={n:0,h:0,comp:{}}); o.n++; if(r.hit) o.h++;
    arr(r.comp).forEach(c=>{ o.comp[c]=(o.comp[c]||0)+1; }); });
  return m;
}
const G_QINFO = (()=>{ const m={};
  Object.entries(G.keywords||{}).forEach(([q,k])=>{ m[q]={qid:q,question:k.q||"",set:String(k.set||"").toLowerCase(),
    domain:"",domain_label:k.domain_label||"",tier:""}; });
  ROWS.forEach(r=>{ m[r.qid]={qid:r.qid,question:r.question,set:r.set,domain:r.domain,
    domain_label:r.domain_label,tier:r.tier,type:r.type}; });
  return m; })();
// 「代わりに挙げた会社」を出す設問か。課題解決型（A2）は回答の太字が施策名になりノイズになるため、
// 自社について聞く設問（A6・Set2 の D1）は競合比較の対象でないため、表示しない
const gAsksCompany = info => !["A2","A6"].includes(info.type) && info.tier!=="D1";
function gRef(k, sp){ // GSC参照クエリの値（代表＝表示回数が最大のサイト）
  if(!k || !k.refn) return {best:null,all:[]};
  const byP=(G.ref||{})[k.refn]||{}, list=[];
  Object.entries(sp).forEach(([s,p])=>{ const v=(byP[p.key]||{})[s];
    if(v) list.push({site:s,period:p,clk:v[0],imp:v[1],pos:v[2],q:v[3]}); });
  list.sort((a,b)=>(b.imp||0)-(a.imp||0));
  return {best:list[0]||null,all:list};
}
function gItems(opt){
  const sp=gSitePeriods(opt.period), win=gClaudeWindow(opt.period), cs=gClaudeStats(win);
  const items=Object.keys(G_QINFO).sort().map(q=>{
    const info=G_QINFO[q], k=(G.keywords||{})[q]||null, c=cs[q];
    const claude=c?{n:c.n,h:c.h,rate:Math.round(c.h/c.n*1000)/10}:null;
    const comp=(c && gAsksCompany(info))?Object.entries(c.comp).sort((a,b)=>b[1]-a[1]).slice(0,3).map(x=>x[0]):[];
    const ref=gRef(k,sp), b=ref.best;
    const gy=!b?"none":(b.pos!=null && b.pos<=10?"top":"low");
    const cy=claude?(claude.h>0?"hit":"miss"):"";
    return {q,info,k,claude,comp,ref,b,gy,cy,cell:cy?`${cy}|${gy}`:"",act:gAction(cy,b),
            owner:ownerOf(info.domain_label)};
  }).filter(it=>(!opt.set || it.info.set===opt.set) && (!opt.domain || it.info.domain===opt.domain)
            && (!opt.owner || it.owner===opt.owner));
  return {items,sp,win};
}
function gCounts(items,key){ const c={}; items.forEach(it=>{ const v=it[key||"cell"]; if(v) c[v]=(c[v]||0)+1; }); return c; }
function gPeriodText(sp){
  const byKey={};
  Object.entries(sp).forEach(([s,p])=>{ (byKey[p.key]||(byKey[p.key]={p,sites:[]})).sites.push(s); });
  return Object.values(byKey).map(x=>`${x.sites.join("・")}：${esc(x.p.monthly?x.p.key:x.p.label)}`).join("／");
}
function gNext(it){ // 設問ごとの「次の一手」
  const k=it.k||{}, b=it.b, w=esc(k.ref||k.obs||"");
  switch(it.act){
    case "fix":  return `「${w}」で ${fmtPos(b.pos)}（${esc(b.site)}）のページに、社名・ブランド名、導入実績（具体名）、FAQ・比較表を追記する`;
    case "near": return `「${w}」は ${fmtPos(b.pos)}。タイトル・見出しにこの語を入れ、関連ページから内部リンクを張って10位以内を目指す`;
    case "rank": return `「${w}」は ${fmtPos(b.pos)}。この語に答えるページ（事例・解説）を強化するか、新しく作る`;
    case "check":return k.obs ? `検索チェックで「${esc(k.obs)}」と設問文を Google で確認する`
                              : `キーワード未設定（google_keywords.csv に追加する）`;
    case "keep": return b ? `維持（Google ${fmtPos(b.pos)}）` : `維持（Google は未確認）`;
  }
  return `<span class="muted">Claude の有効な回答がありません</span>`;
}
const G_OWNERS = () => { const o=[...new Set(Object.values(HOME.domain_owner||{}))]; if(!o.includes("未割当")) o.push("未割当"); return o; };
function buildGoogleFilters(){
  const mount=$("#filters-google"); mount.innerHTML="";
  const mk=(label,key,opts)=>{ const f=document.createElement("div"); f.className="f";
    const lab=document.createElement("label"); lab.textContent=label;
    const sel=document.createElement("select");
    sel.innerHTML=opts.map(([v,t])=>`<option value="${escAttr(v)}">${esc(t)}</option>`).join("");
    sel.value=GS[key]; sel.dataset.k=key; sel.onchange=()=>{ GS[key]=sel.value; GS.cell=""; renderGoogle(); };
    f.appendChild(lab); f.appendChild(sel); mount.appendChild(f); };
  mk("Google 期間","period",[["latest","最新（サイトごとの最新ファイル）"]]
    .concat(G_SEARCH_PERIODS.map(p=>[p.key,(p.monthly?p.key:p.label)+`（${p.sites.join("・")}）`])));
  mk("担当","owner",[["","すべて"]].concat(G_OWNERS().map(o=>[o,o])));
  mk("やること","act",[["","すべて"]].concat(G_ACT_ORDER.map(a=>[a,G_ACTIONS[a].label])));
  mk("質問セット","set",[["","すべて"]].concat(D.sets.map(s=>[s,SET_LABELS[s]||s])));
  mk("ドメイン","domain",[["","すべて"]].concat(D.domains.map(d=>[d,optLabel("domain",d)])));
  const rf=document.createElement("div"); rf.className="f";
  const rb=document.createElement("button"); rb.className="reset"; rb.textContent="リセット";
  rb.onclick=()=>{ Object.assign(GS,{period:"latest",set:"",domain:"",owner:"",act:"",cell:""}); buildGoogleFilters(); renderGoogle(); };
  rf.appendChild(document.createElement("label")); rf.appendChild(rb); mount.appendChild(rf);
}
function gSyncFilters(){ $$("#filters-google select").forEach(s=>{ s.value=GS[s.dataset.k]||""; }); }
function gFocus(o){ // 担当・やることで一覧を絞り込んで表までスクロール
  Object.assign(GS,{cell:""},o); gSyncFilters(); renderGoogle();
  $("#g-table").scrollIntoView({behavior:"smooth",block:"start"});
}
function gCell(key){ GS.cell = GS.cell===key ? "" : key; renderGoogle(); }
function gGroups(list){ // 同じ GSC参照クエリの設問をまとめ、検索の多い順に
  const g={};
  list.forEach(it=>{ const key=(it.k&&it.k.refn)||it.q; (g[key]||(g[key]={items:[],b:it.b,k:it.k})).items.push(it); });
  return Object.values(g).sort((a,b)=>((b.b&&b.b.imp)||0)-((a.b&&a.b.imp)||0));
}
function renderGoogleTodo(items){
  const by=a=>items.filter(it=>it.act===a);
  const ownerCount=list=>{ const c={}; list.forEach(it=>c[it.owner]=(c[it.owner]||0)+1);
    return Object.entries(c).sort((a,b)=>b[1]-a[1]).map(([o,n])=>`<span class="pill own">${esc(o)} ${n}</span>`).join(" "); };
  const card=(a,extra)=>{ const t=G_TODO[a], list=by(a);
    const priCls=t.pri==="高"?"":(t.pri==="中"?"mid":"low");
    let top="";
    if(a==="check"){
      top=list.length?`<div class="k">担当別</div><div>${ownerCount(list)}</div>`:"";
    } else {
      top=gGroups(list).slice(0,3).map(g=>{ const b=g.b, it0=g.items[0];
        const comp=[...new Set(g.items.flatMap(it=>it.comp))].slice(0,3);
        return `<li><b>「${esc(g.k.ref)}」</b> ${esc(b.site)} ${fmtPos(b.pos)}・${fmtN(b.imp)}回
          <span class="pill own">担当 ${esc(it0.owner)}</span>${g.items.length>1?` <span class="muted">（${g.items.length}問）</span>`:""}
          ${a==="fix"&&comp.length?`<div class="muted">Claude は代わりに ${comp.map(esc).join("／")} を挙げている</div>`:""}</li>`; }).join("");
      top=top?`<div class="k">検索の多い順（上位3件）</div><ul class="notes" style="margin:2px 0;padding-left:18px">${top}</ul>`:"";
    }
    return `<div class="act">
      <div class="hd"><span class="pill pri ${priCls}">${esc(t.pri)}</span> ${esc(t.title)}（${list.length}問）</div>
      <div class="muted">${esc(t.why)}</div>
      ${list.length?top:`<div class="k">該当なし</div>`}
      <div class="k">やること</div><ul class="notes" style="margin:2px 0;padding-left:18px">${t.todo.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>
      ${extra||""}
      ${list.length?`<button class="noprint" style="margin-top:6px" onclick="gFocus({act:'${a}'})">この ${list.length} 問を一覧で見る</button>`:""}
    </div>`; };
  const nRank=by("rank").length;
  $("#g-todo").innerHTML = card("fix") + card("near", nRank?`<div class="muted" style="margin-top:6px">21位以下の ${nRank} 問は一覧の「順位を上げる」で確認できます。</div>`:"") + card("check");
}
function renderGoogleOwners(items){
  const owners=G_OWNERS().filter(o=>items.some(it=>it.owner===o));
  const acts=G_ACT_ORDER;
  const cnt={}; items.forEach(it=>{ if(!it.act) return; const k=it.owner+"|"+it.act; cnt[k]=(cnt[k]||0)+1; });
  $("#g-owner").innerHTML = !owners.length ? nodata("該当する設問がありません") :
    `<table class="gquad"><tr><th>担当</th>${acts.map(a=>`<th>${esc(G_ACTIONS[a].label)}</th>`).join("")}</tr>`
    + owners.map(o=>`<tr><th>${esc(o)}</th>${acts.map(a=>{ const n=cnt[o+"|"+a]||0;
        return n ? `<td class="qc ${G_ACTIONS[a].cls}" onclick="gFocus({owner:'${escAttr(o)}',act:'${a}'})"><div class="n" style="font-size:18px">${n}</div></td>`
                 : `<td class="muted">–</td>`; }).join("")}</tr>`).join("")
    + `</table><div class="muted" style="margin-top:6px">数字をクリックすると、その担当・やることの設問だけに一覧を絞り込みます。担当は config.json の domain_owner（ホームと同じ）。</div>`;
}
function renderGoogle(){
  const {items,sp,win}=gItems(GS);
  const cnt=gCounts(items), act=gCounts(items,"act");
  const noClaude=items.filter(it=>!it.cy).length;
  const warn=arr(G.warnings).map(w=>`<div class="muted">⚠ ${esc(w)}</div>`).join("");
  const fixOwners=(()=>{ const c={}; items.filter(it=>it.act==="fix").forEach(it=>c[it.owner]=(c[it.owner]||0)+1);
    return Object.entries(c).sort((a,b)=>b[1]-a[1]).map(([o,n])=>`${esc(o)} ${n}`).join("・"); })();
  $("#g-summary").innerHTML = !G.available
    ? `Google参考値${help("gsc")}：<b>GSC データなし</b>。GSC の Excel を ${esc(G.dir||"gsc_dir（未設定）")} に置いて再生成してください。${warn}`
    : `次にやること：<b>AI向けに直す ${act.fix||0} 問</b>${fixOwners?`（担当 ${fixOwners}）`:""}、`
      +`あと一歩 ${act.near||0} 問、順位を上げる ${act.rank||0} 問、検索チェック待ち ${act.check||0} 問（維持 ${act.keep||0} 問）。`
      +`<br><span class="muted">Google${help("gsc")}：${gPeriodText(sp)||"–"} ／ Claude：${esc(win.label)}（有効行で集計）</span>${warn}`;

  renderGoogleTodo(items);
  renderGoogleOwners(items);

  const cellTd=key=>{ const c=G_CELLS[key];
    return `<td class="qc ${c.cls}${GS.cell===key?" sel":""}" onclick="gCell('${key}')">
      <div class="lb">${esc(c.label)}</div><div class="n">${cnt[key]||0}</div><div class="ds">${esc(c.desc)}</div></td>`; };
  $("#g-quad").innerHTML = `<table class="gquad"><tr><th style="width:17%"></th>
      <th>Google 10位以内</th><th>Google 11位以下</th><th>Google データなし</th></tr>
    <tr><th>Claude 出現あり<div class="muted">（出現率 &gt; 0%）</div></th>${cellTd("hit|top")}${cellTd("hit|low")}${cellTd("hit|none")}</tr>
    <tr><th>Claude 出現なし<div class="muted">（出現率 0%）</div></th>${cellTd("miss|top")}${cellTd("miss|low")}${cellTd("miss|none")}</tr></table>`;
  $("#g-quad-note").innerHTML = (noClaude?`Claude の有効な回答がない設問 ${noClaude} 問は含めていません。`:"")
    + ` マスをクリックすると一覧を絞り込みます。`;

  const list=items.filter(it=>(!GS.cell || it.cell===GS.cell) && (!GS.act || it.act===GS.act)).sort((a,b)=>{
    const ia=a.act?G_ACT_ORDER.indexOf(a.act):99, ib=b.act?G_ACT_ORDER.indexOf(b.act):99;
    if(ia!==ib) return ia-ib;
    const ma=a.b?(a.b.imp||0):-1, mb=b.b?(b.b.imp||0):-1;
    return ma!==mb ? mb-ma : (a.q<b.q?-1:1); });
  const filt=[GS.owner&&`担当 ${GS.owner}`, GS.act&&G_ACTIONS[GS.act].label, GS.cell&&`根拠のマス「${G_CELLS[GS.cell].label}（${G_CELLS[GS.cell].desc}）」`].filter(Boolean);
  $("#g-table-head").innerHTML = filt.length
    ? `絞り込み中：<b>${filt.map(esc).join(" ／ ")}</b>（${list.length} 問） <button class="reset" onclick="gFocus({owner:'',act:'',cell:''})">解除</button>`
    : `${list.length} 問`;
  $("#g-table").innerHTML = !list.length ? nodata("該当する設問がありません") :
    `<table><tr><th>やること</th><th>設問</th><th>担当</th><th>Claude</th><th>次の一手</th>
      <th>Claude が代わりに挙げた会社<span class="help" tabindex="0" data-tip="${escAttr("当社が出なかった回答から自動で抜き出した社名の候補（上位3件）。課題解決型（A2）と自社について聞く設問（A6・Set2 の D1）には表示しません。一般語が混ざることがあるため、詳細は回答全文で確認してください。")}">?</span></th>
      <th>観測キーワード</th><th>GSC参照クエリ</th><th>Google 順位</th><th>表示回数</th></tr>`
    + list.map(it=>{ const A=it.act?G_ACTIONS[it.act]:null, k=it.k||{}, b=it.b;
      const judge=A?`<span class="judge ${A.cls}">${esc(A.label)}</span>`:`<span class="muted">Claude 未計測</span>`;
      const setTag=it.info.set?`<span class="pill">${esc(it.info.set.replace(/^set/,"Set"))}</span>`:"";
      const tier=it.info.tier?`<span class="pill">${esc(it.info.tier)}</span>`:"";
      const obs=k.obs?esc(k.obs)+(k.gap?` <span class="gap" title="${escAttr("意図のズレ："+k.gap)}">⚠</span>`:""):`<span class="muted">未設定</span>`;
      const ref=k.ref?((k.refdiff && k.refdiff!=="同一")?`<span class="approx" title="${escAttr(k.refdiff)}">≈</span> `:"")+esc(k.ref)
                     :`<span class="muted">なし</span>`;
      const more=it.ref.all.length>1?` +${it.ref.all.length-1}`:"";
      return `<tr class="click" onclick="gDetail('${escAttr(it.q)}')"><td>${judge}</td>
        <td>${esc(it.info.question||"")}<div class="muted">${esc(it.q)} ${setTag}${tier} ${esc(it.info.domain_label||"")}</div></td>
        <td class="nw">${esc(it.owner)}</td>
        <td class="num">${it.claude?`${it.claude.rate}%<div class="muted">${it.claude.h}/${it.claude.n}</div>`:"–"}</td>
        <td style="min-width:220px">${gNext(it)}</td>
        <td>${it.comp.length?it.comp.map(c=>`<span class="pill bad">${esc(c)}</span>`).join(" "):`<span class="muted">–</span>`}</td>
        <td>${obs}</td><td>${ref}</td>
        <td class="num">${b?`${fmtPos(b.pos)}<div class="muted">${esc(b.site)}${more}</div>`:`<span class="muted">データなし</span>`}</td>
        <td class="num">${b?fmtN(b.imp):"–"}</td></tr>`; }).join("")
    + `</table><div class="muted" style="margin-top:6px">並び順：やることの優先度 → Google で検索されている回数（表示回数）が多い順。`
    + `⚠＝観測キーワードで設問から落ちた要素あり ／ ≈＝GSC参照クエリが観測キーワードより広い（近い語で代用）。行クリックで詳細。</div>`;
  renderGoogleAI();
  renderGoogleLog();
}
let gDetailChart=null;
function gDetail(q){
  const info=G_QINFO[q]||{}, k=(G.keywords||{})[q]||{};
  const byP=(G.ref||{})[k.refn]||{};
  const periods=G_SEARCH_PERIODS.slice().sort((a,b)=>a.start<b.start?-1:1);
  const gRows=[];
  periods.forEach(p=>p.sites.forEach(s=>{ const v=(byP[p.key]||{})[s];
    if(v) gRows.push(`<tr><td>${esc(p.monthly?p.key:p.label)}</td><td>${esc(s)}｜${esc(siteLabel(s))}</td>
      <td class="num">${fmtPos(v[2])}</td><td class="num">${fmtN(v[1])}</td><td class="num">${fmtN(v[0])}</td><td>${esc(v[3])}</td></tr>`); }));
  const cm={};
  ROWS.filter(r=>r.qid===q && isValid(r)).forEach(r=>{ const m=(r.date||"").slice(0,7); if(!m) return;
    const o=cm[m]||(cm[m]={n:0,h:0}); o.n++; if(r.hit) o.h++; });
  const monthsG=periods.filter(p=>p.monthly).map(p=>p.key);
  const months=[...new Set(Object.keys(cm).concat(monthsG))].sort();
  const posOf=m=>{ const x=byP[m]; if(!x) return null; const v=Object.values(x).sort((a,b)=>(b[1]||0)-(a[1]||0))[0]; return v?v[2]:null; };
  const notes=arr(G.action_notes).filter(n=>arr(n.qids).includes(q));
  const canChart=monthsG.length>=1 && months.length>=2;
  openModal(`<h3>${esc(info.question||q)}</h3>
    <div class="meta">${esc(q)} ／ ${esc(info.domain_label||"")}</div>
    <table><tr><th>観測キーワード</th><td>${esc(k.obs||"未設定")}${k.gap?`<div class="muted">意図のズレ：${esc(k.gap)}</div>`:""}</td></tr>
      <tr><th>GSC参照クエリ</th><td>${esc(k.ref||"なし")}${k.ref&&k.refdiff?`<div class="muted">${esc(k.refdiff)}</div>`:""}</td></tr></table>
    <h3 style="margin-top:14px">GSC（期間 × サイト）</h3>
    ${gRows.length?`<table><tr><th>期間</th><th>サイト</th><th>平均順位</th><th>表示回数</th><th>クリック</th><th>GSC上のクエリ</th></tr>${gRows.join("")}</table>`
      :nodata(k.ref?"GSC参照クエリが、取り込んだ GSC ファイルに出てきません":"この設問には GSC参照クエリがありません（検索チェックで確認します）")}
    <h3 style="margin-top:14px">推移（月別：Google 平均順位 と Claude 出現率）</h3>
    ${canChart?`<div style="height:260px"><canvas id="chart-g-detail"></canvas></div>`
      :`<div class="muted">月単位の GSC ファイル（前月1日〜末日）が入ると、ここに月別の推移が出ます。</div>`}
    <table style="margin-top:8px"><tr><th>月</th><th>Claude 出現率</th><th>Google 平均順位</th></tr>
      ${months.map(m=>`<tr><td>${m}</td><td class="num">${cm[m]?`${Math.round(cm[m].h/cm[m].n*1000)/10}%（${cm[m].h}/${cm[m].n}）`:"–"}</td>
        <td class="num">${monthsG.includes(m)?(posOf(m)!=null?fmtPos(posOf(m)):"データなし"):"–"}</td></tr>`).join("")}</table>
    <h3 style="margin-top:14px">施策の記録（config.json の action_notes）</h3>
    ${notes.length?`<ul class="notes">${notes.map(n=>`<li><b>${esc(n.date)}</b> ${esc(n.note)}</li>`).join("")}</ul>`
      :`<div class="muted">記録なし</div>`}`);
  if(gDetailChart){ gDetailChart.destroy(); gDetailChart=null; }
  if(canChart){
    gDetailChart=new Chart($("#chart-g-detail"),{type:"line",
      data:{labels:months,datasets:[
        {label:"Google 平均順位（左・上ほど上位）",data:months.map(m=>monthsG.includes(m)?posOf(m):null),yAxisID:"yPos",
         borderColor:"#b7791f",backgroundColor:"#b7791f",spanGaps:true},
        {label:"Claude 出現率 %（右）",data:months.map(m=>cm[m]?Math.round(cm[m].h/cm[m].n*1000)/10:null),yAxisID:"yRate",
         borderColor:"#1d4f91",backgroundColor:"#1d4f91",spanGaps:true}]},
      options:{maintainAspectRatio:false,scales:{
        yPos:{position:"left",reverse:true,min:1,title:{display:true,text:"順位"}},
        yRate:{position:"right",min:0,suggestedMax:100,grid:{drawOnChartArea:false},title:{display:true,text:"%"}}}}});
  }
}
let gAiChart=null;
function renderGoogleAI(){
  const aiSites=G_SITE_ORDER.filter(s=>arr(((G.daily||{})[s]||{}).ai).length);
  const aiPeriods=Object.keys(G.ai_pages||{}).map(k=>G.periods.find(p=>p.key===k)||{key:k,label:k,end:""}).sort(byEndDesc);
  if(!GS.aiPeriod && aiPeriods.length) GS.aiPeriod=aiPeriods[0].key;
  const mount=$("#g-ai-filters");
  if(!mount.dataset.built){
    mount.dataset.built="1";
    const mk=(label,key,opts)=>{ const f=document.createElement("div"); f.className="f";
      const lab=document.createElement("label"); lab.textContent=label; const sel=document.createElement("select");
      sel.innerHTML=opts.map(([v,t])=>`<option value="${escAttr(v)}">${esc(t)}</option>`).join("");
      sel.value=GS[key]; sel.onchange=()=>{ GS[key]=sel.value; renderGoogleAI(); };
      f.appendChild(lab); f.appendChild(sel); mount.appendChild(f); };
    mk("サイト","aiSite",[["","すべて（サイト別の線）"]].concat(aiSites.map(s=>[s,`${s}｜${siteLabel(s)}`])));
    mk("ページ一覧の期間","aiPeriod",aiPeriods.map(p=>[p.key,p.monthly?p.key:p.label]));
  }
  if(gAiChart){ gAiChart.destroy(); gAiChart=null; }
  const show=GS.aiSite?[GS.aiSite]:aiSites;
  if(!show.length){ $("#g-ai-pages").innerHTML=nodata("GSC「生成AI機能」レポートがありません"); return; }
  const dates=[...new Set(show.flatMap(s=>arr(G.daily[s].ai).map(d=>d[0])))].sort();
  const colors=["#1d4f91","#c0392b","#1a7f4b","#b7791f","#7b3fa0","#0f7c8c"];
  gAiChart=new Chart($("#chart-g-ai"),{type:"line",
    data:{labels:dates,datasets:show.map((s,i)=>{ const m=Object.fromEntries(arr(G.daily[s].ai).map(d=>[d[0],d[2]]));
      return {label:`${s}｜${siteLabel(s)}`,data:dates.map(d=>m[d]??null),borderColor:colors[i%colors.length],
        backgroundColor:colors[i%colors.length],pointRadius:0,borderWidth:1.5,spanGaps:true}; })},
    options:{maintainAspectRatio:false,interaction:{mode:"index",intersect:false},
      scales:{x:{ticks:{maxTicksLimit:10}},y:{min:0,title:{display:true,text:"表示回数"}}}}});
  const pp=(G.ai_pages||{})[GS.aiPeriod]||{};
  const pages=show.flatMap(s=>arr(pp[s]).map(x=>({s,u:x[0],n:x[1]}))).sort((a,b)=>b.n-a.n).slice(0,30);
  $("#g-ai-pages").innerHTML = pages.length
    ? `<table><tr><th>サイト</th><th>ページ</th><th>表示回数</th></tr>${pages.map(x=>`<tr><td>${esc(x.s)}</td>
        <td style="word-break:break-all"><a href="${escAttr(x.u)}" target="_blank" rel="noopener">${esc(x.u)}</a></td>
        <td class="num">${fmtN(x.n)}</td></tr>`).join("")}</table>`
    : nodata("この期間・サイトのページデータがありません");
}
function renderGoogleLog(){
  $("#g-log").innerHTML = `<div>GSC フォルダ：${esc(G.dir||"未設定")}</div>`
    + arr(G.warnings).map(w=>`<div>⚠ ${esc(w)}</div>`).join("")
    + (arr(G.log).length?`<table style="margin-top:8px"><tr><th>状態</th><th>ファイル</th><th>サイト</th><th>種類</th><th>期間</th><th>メモ</th></tr>`
      + G.log.map(l=>`<tr><td class="nw">${esc(l.status)}</td><td>${esc(l.file)}</td><td>${esc(l.site)}</td>
        <td>${l.kind==="ai"?"生成AI機能":(l.kind==="search"?"検索パフォーマンス":"–")}</td><td>${esc(l.period)}</td>
        <td>${esc(l.note)}</td></tr>`).join("")+`</table>`:`<div>GSC ファイルが見つかりません。</div>`);
}
function renderHomeGoogle(){ // ホーム「1) 今回の結論」の末尾に1行
  const el=$("#home-concl"); if(!el) return;
  let body;
  if(!G.available) body=`<b>Google参考値</b>${help("gsc")}：<b>データなし</b>（GSC ファイル未取り込み）`;
  else {
    const {items,sp}=gItems({period:"latest",set:"",domain:"",owner:""}), c=gCounts(items,"act");
    body=`<b>Google参考値</b>${help("gsc")}：次にやること　<b>AI向けに直す ${c.fix||0} 問</b>`
      +`（Google 10位以内なのに Claude で出ていない）／あと一歩 ${c.near||0} 問／順位を上げる ${c.rank||0} 問／`
      +`検索チェック待ち ${c.check||0} 問 <span class="muted">（${gPeriodText(sp)}）</span>`;
  }
  el.insertAdjacentHTML("beforeend", `<div class="set-box">${body}
    <button class="noprint" style="margin-left:8px" onclick="showTab('s-google')">Google参考値タブへ</button></div>`);
}
buildGoogleFilters();

renderComp(); renderContext(); renderCross(); renderUrl(); renderSelf(); renderCompare(); renderRuns();
renderHome(); renderHomeGoogle();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
