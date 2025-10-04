# UR Vacancy Monitor (GitHub Actions)

UR公式の **`detail_bukken_room/`** を **POST直叩き** して空室の **差分だけ通知**。JS後描画の取りこぼしを回避。

- 窓口：**9:30–18:30 JST**（30分刻み、合計 **19回/日**）
- 通知：**LINE Notify**（トークンが無ければActionログに出力）
- 監視対象：`20_7080`（`shisya=20, danchi=708`）

## 使い方
1. リポジトリにこのファイル群を置く（`ur_monitor.py` と `.github/workflows/ur-monitor.yml`）。
2. リポの **Settings → Secrets and variables → Actions → New repository secret** で  
   `LINE_NOTIFY_TOKEN` を追加（LINE Notifyで発行）。
3. 30分おきに動く。初回は「初期化」通知。以後、**新規/変更/消滅** で通知。

## 設定を変える場合
- 時間帯：`ur_monitor.py` → `WINDOW_START` / `WINDOW_END`（JST）
- 物件：`FORM_BASE` の `danchi` を変更（必要なら `7080` など）
- ページ数：`PAGE_INDEXES = [0, 1, 2]` を拡張
- 通知先：`notify()` をSlackやメールに差し替え可

## 補足
- 5xx/429は自動リトライ（1回）。
- レスポンスがHTML断片に変わっても正規表現で最低限を抽出。
- 状態は `.state.json` に保存し、Actionがコミットする仕組み。
