# 作業ログ（worklog）
## GEO 定点観測 分析アプリ（GEO-analysis）

ブランチ単位の作業記録。**一番上が最新**（新しい順に追記）。
書式は `docs/引き継ぎプロンプト_v1.md` §6-2 を参照。コミットの詳細は各コミット本文（`git show <ハッシュ>`）にも残す。

---

## 2026-09-25  branch: feature/one-click-update
- 目的：最新データを反映して開く操作を簡単にする。関係者が開く BOX フォルダも常に最新にする。
- 作業内容：
  - **`更新して開く.bat` を新規追加**：ダブルクリックで 再生成 → `share_dirs` へコピー → 既定ブラウザで表示。Python は Anaconda（`C:\work\anaconda_install`）を自動検出し、無ければ `py`/`python`。エラー時はウィンドウを残す（pause）。bat 本文は文字化け回避のため ASCII のみ。
  - `generate.py`：`--open`（生成後にブラウザ表示）・`--no-share`（コピー抑止）を追加。config の `share_dirs`（リスト）へ `analysis.html` をコピー（フォルダ無し・コピー失敗は WARN でスキップし、生成は成功扱い）。
  - `config.json`：`share_dirs` に BOX `事業推進Div□\DX推進課\生成AI\GEO-analysis` を設定。
  - docs：README・実行手順書にワンクリック手順／`share_dirs` を追記。パス一覧の旧 `Downloads\06_GEO` を現行パスに更新。
  - BOX フォルダのアプリ一式（generate.py・config.json・README・docs・bat）もリポジトリの最新版に同期。
  - **修正（確認NG→継続）**：`.html` の関連付けがエディタ（VS Code）の環境では `os.startfile` だとエディタで開いてしまうため、`--open` を「既定のブラウザ」で開く方式に変更。レジストリの `UrlAssociations\https|http\UserChoice` の ProgId から起動コマンドを取得し `file:///` URL で起動（ブラウザ種別は固定しない）。取得できない場合は Edge → `webbrowser` の順にフォールバック。
- 確認：bat を実行し rows=1794 hits=49 files=14 で生成。BOX へのコピー（サイズ・更新時刻一致）を確認。.html=VSCode・既定ブラウザ=Chrome の環境で Chrome で開くことを確認。HTML テンプレートは変更なし。
- コミット：8d96303（feat）・1ce5847（fix：既定ブラウザで開く）＋本worklogのハッシュ追記コミット／マージ：main への --no-ff マージコミット

---

## 2026-08-05  branch: feature/all-runs-summary-and-context
- 目的：結果分析の拡張。(1) 過去回すべての比較・総合を可能にする。(2) 回答本文（1回ごとの全履歴）のコンテキスト分析を追加する。
- 作業内容：
  - **新タブ「全回サマリー」**：全回の出現率推移チャート（run/timing 切替）＋回ごとテーブル（有効行/hits/出現率/エラー/空/ユニーク競合/トップ競合）＋総合集計（全回プールのドメイン別・特異度別 出現率＋回ごとのブレ=最小〜最大の幅）。出現率は**有効行（エラー・空を除く）**を分母に算出し、全行エラーの回は「–」で自動除外。回クリックでその回のヒット回答へドリルダウン。
  - **新タブ「回答分析」**：①回答タイプ自動分類（社名列挙型/一般論型/留保・拒否型/空/エラー）＋Set2ティア別構成、②評価軸キーワード分析（未言及回答でAIが重視する観点の頻度＝GEO強化の切り口・チャート＋表）、③競合の文脈スニペット（社名を含む一文を本文から抽出）、④自社ヒット時の文脈（前後文＋共起する評価軸）。各行クリックで回答全文へドリルダウン。
  - `generate.py`：`classify_answer()`/`detect_criteria()` と辞書 `ATYPE_LABELS`・`CRITERIA`・`REFUSAL_HINTS` を追加。各行に `atype`/`criteria` を付与し payload に埋め込み（重い処理はPython 1回・集計はJS動的の分担を踏襲）。
  - `config.json` の results/reference パスを実データ（`Documents\GitHub\GEO\monitoring`）に更新（旧 `Downloads\06_GEO` は現存せず）。
  - docs：仕様書 §5・README「実装している分析」に2機能を追記。
- 確認：`python generate.py` 再生成（rows=828 hits=8 files=7・エラー0）＋ **アプリ内ブラウザで目視**。両タブの全セクション描画・Chart.js描画・粒度切替(run↔timing)・ドリルダウンモーダルを確認。既存タブは加算的変更のため不変。
  - 所見：hits=8 のうち Raffiria/ラフィーリア 系の数件は、AIが「情報が無い」と留保しつつ設問由来の名称を復唱したための**誤検知（false-positive）**。回答分析④で可視化された。mention 判定の精度は monitoring 側の課題として別途共有。
- コミット：1d1c416（＋本worklogのハッシュ追記コミット）／マージ：main への --no-ff マージコミット

## 2026-07-28  branch: docs/review-gate
- 目的：マージ前の「確認」を独立ゲートにし、NG→修正して継続／OK→マージの2経路を明示する。
- 作業内容：
  - §6手順を①〜⑥に再構成。確認(④)をコミット(③)の後の独立ゲートに変更し、NGは②へ戻るループを明記。
  - 冒頭に分岐フロー図（テキスト）を追加。ルール・早見表の参照番号と表現を更新。
- 確認：文書のみ。python generate.py の挙動・アプリ機能は不変。
- コミット：（本ブランチ）／マージ：（main への --no-ff）

## 2026-07-28  branch: docs/worklog-and-commit-body
- 目的：ブランチの作業内容を残す運用（コミット本文＋作業ログ）を整備する。
- 作業内容：
  - 引き継ぎ書 §6 に「④ 作業ログ追記」「⑤ 件名＋本文でコミット（`-m` 複数指定）」を追加し、手順を①〜⑦に再整理。
  - §6-2「作業ログ（worklog）の書式」を新設。早見表・ルールに worklog とコミット本文の2か所記録を明記。
  - 本ファイル `docs/worklog.md` を新規作成。
- 確認：文書のみの変更。`python generate.py` の挙動に影響なし（生成物・アプリ機能は不変）。
- コミット：（本ブランチのコミット）／マージ：（main への --no-ff マージコミット）

---

## これまでの経緯（worklog 導入前・コミットからの要約）

以下は本ログ導入前の作業。履歴は `git log --oneline --graph` で確認できる。

### 2026-07-28  merge 3e8a101 ← docs/handoff-workflow
- 引き継ぎ書に作業フロー（ブランチ運用→確認→main へマージコミット）を追加。

### 2026-07-28  fd4cc48  feat（回粒度・過去回比較）
- ファイル名から回(run)/タイミング(timing)を解析し、各行・dims に付与。
- 全タブ共通フィルタに「タイミング」「回(run)」を追加。クロス集計の軸にも run/timing を追加。
- 「過去回比較」タブを新設（A/B 出現率デルタ・競合の増減・質問の反転 hit↔miss・回答差分ドリルダウン、粒度 run/timing 切替）。
- 6ファイル690行で再生成し jsdom ランタイム検証（エラー0）。

### 2026-07-28  56aec8d  docs（Anaconda Prompt）
- 実行手順書を Anaconda Prompt 前提に更新（パス一覧・`python`・`cd /d`）。

### 2026-07-28  021eabb  docs（初期ドキュメント）
- 仕様書・引き継ぎプロンプト・実行手順書を docs/ に追加。

### 2026-07-28  e6e0a24  初版
- 分析アプリ初版：generate.py（stdlibのみ）＋自己完結HTML。
- P1 競合共起／P1 多軸クロス集計＋ドリルダウン／P2 引用URL／P2 自社突合／P3 設計。
- git 初期化・初回コミット。`analysis.html` は .gitignore 対象。
