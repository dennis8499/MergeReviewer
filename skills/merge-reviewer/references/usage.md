# MergeReviewer 詳細使用指南

適用版本：**0.15.0**。主要讀者是使用 Codex 執行合併前審查的開發者；進階流程供工具維護者使用。

內容：安裝 → 第一次審查 → 參數 → 常見情境 → 多儲存庫判定 → 報告 → 疑難排解 → 升級 → 手動輔助程式。

## 1. 環境與安裝

### 需求

- Git；預覽需支援 `git merge-tree --write-tree`。不支援時報告保留預覽缺口。
- Python 3.10 以上，執行證據及報告輔助程式。
- 可以載入 Codex 技能的環境。核心由目前的 Codex 審查，不需再啟動巢狀 Codex CLI。
- 本機可讀取的儲存庫，以及可解析的基礎／比較分支。遠端分支需要遠端連線及讀取權限。

~~~powershell
git --version
python --version
~~~

### 安裝 ZIP 或原始碼

使用 `merge-reviewer-0.15.0.zip`，解壓後得到 `merge-reviewer` 資料夾。發布套件見 [GitHub Releases](https://github.com/dennis8499/MergeReviewer/releases)；尚未發布時，可在原始碼根目錄執行 `python scripts/package_release.py --tag v0.15.0`，取得 `dist/merge-reviewer-0.15.0.zip`。

以下假設目前目錄已解壓：

~~~powershell
$skillParent = if ($env:CODEX_HOME) {
    Join-Path $env:CODEX_HOME "skills"
} else {
    Join-Path $HOME ".codex\skills"
}
$skillTarget = Join-Path $skillParent "merge-reviewer"
if (Test-Path -LiteralPath $skillTarget) {
    throw "已有 MergeReviewer；請依第 8 節備份並移出舊技能，再安裝。"
}
New-Item -ItemType Directory -Path $skillParent -Force | Out-Null
Copy-Item -LiteralPath ".\merge-reviewer" -Destination $skillParent -Recurse
Test-Path -LiteralPath (Join-Path $skillTarget "SKILL.md")
Test-Path -LiteralPath (Join-Path $skillTarget "references\usage.md")
Get-Content -LiteralPath (Join-Path $skillTarget "VERSION")
~~~

兩個 `Test-Path` 應為 `True`，`VERSION` 應為 0.15.0。原始碼安裝把來源換成 `.\skills\merge-reviewer`；macOS／Linux 使用相同技能目錄規則與對應檔案複製工具。

在 Codex 技能清單確認 **Merge Reviewer（合併審查）**。若仍未載入，重新開始工作階段，確認清單及安裝路徑。

## 2. 第一次審查

假設工作區是 `C:\Work\OrderService`，有一個 Git 儲存庫，本機存在 `main` 及 `feature/payment`：

1. 在 Codex 開啟此工作區。用 `git branch --list` 確認本機分支名稱，`git remote -v` 確認遠端。
2. 在 **Codex 對話**輸入以下要求；它不是 PowerShell 指令：

~~~text
$merge-reviewer 專案名稱=OrderService 基礎分支=main 比較分支=feature/payment
~~~

3. Codex 準備固定版本，閱讀差異與必要背景，完成核心審查並發布報告。單一儲存庫不要求跨儲存庫契約或影響圖。
4. 點開聊天中的 Markdown 連結，或前往：

~~~text
C:\Work\OrderService\review-reports\merge-review-<時間戳>-<基礎分支commit前8碼>-<比較分支commit前8碼>.md
~~~

每次只產生一個 Markdown 檔案。時間戳使用 UTC 發布時間，格式為 `YYYYMMDDTHHMMSSffffffZ`，例如 `20261014T091530123456Z`；檔名兩個提交取本次擷取分支 SHA 的固定前 8 碼，依序為基礎分支頂端及比較分支頂端。基礎分支提交不使用共同祖先；問題證據附完整固定 ref。同名時時間戳遞增一微秒，保留先前報告。

5. 按問題及下一步處理。必要來源缺失時補齊後重新審查；程式修正需先提交到比較分支，下一次分支審查才涵蓋修正。

本機功能分支不用先推送，也不用切換成比較分支。既有已暫存、未暫存及未追蹤內容不納入本次分支審查，不必先貯存（stash）或清理。

## 3. 參數及分支寫法

### 三個必要輸入

| Codex 名稱 | CLI | 可接受內容 | 範例 |
| --- | --- | --- | --- |
| 專案名稱 | `--project` | 一個儲存庫名稱、完整路徑或帶目錄分隔的相對路徑 | `OrderService`、`C:\Work\OrderService`、`./OrderService` |
| 基礎分支 | `--base` | 本機分支，或明確包含遠端名稱的分支 | `main`、`refs/heads/main`、`origin/main` |
| 比較分支 | `--head` | 同一儲存庫的本機或遠端分支 | `feature/payment`、`origin/feature/payment` |

三者必填。名稱不唯一時用完整路徑；含空白路徑用引號：

~~~text
$merge-reviewer 專案名稱="C:\Work\Customer Portal" 基礎分支=main 比較分支=feature/payment
~~~

`main` 只查本機分支；`origin/main`／`refs/remotes/origin/main` 只查指定遠端。遠端分支先同步，失敗就停止，不使用過期快取。不會將缺少的本機 `main` 自動替換成 `origin/main`。

不接受標籤、提交 SHA、`HEAD` 或 `main~1` 等版本運算式。

### 工具設定

這些設定不增加審查目標：

| 設定 | 用途及預設 |
| --- | --- |
| 工作區／`--workspace` | 儲存庫搜尋根目錄；預設為目前任務的宣告工作區。 |
| VSCode工作區／`--workspace-file` | 使用實際 `.code-workspace` 的 `folders`，優先於根目錄掃描。 |
| `--no-fetch` | 離線準備；僅本機分支可使用。 |
| `--git-timeout` | 每個 Git 指令秒數，預設 180；有限正數。 |
| `--context-dir` | 系統暫存目錄中的全新證據路徑；省略時自動建立。 |
| `--summary` | 擷取時僅回傳來源索引、必要覆蓋與缺口；完整 manifest 與證據仍保存。省略時保留完整 JSON 回傳。 |
| `--pretty` | 格式化準備／搜尋輔助程式的內部證據 JSON。 |
| 發布器 `--report-dir` | 進階目的地覆寫；預設所選儲存庫的 `review-reports` 目錄。 |

`--scope`、`--target`、`--quick`、`--mode` 已移除。

## 4. 常見使用情境

### 本機功能分支尚未推送

前提：兩個本機分支存在，功能變更已提交。

~~~text
$merge-reviewer 專案名稱=OrderService 基礎分支=main 比較分支=feature/payment
~~~

預期：使用本機固定提交，不查找上游分支，不要求功能分支已發布。

### 明確比較遠端分支

前提：儲存庫已設定 `origin`，兩個遠端分支可讀取。

~~~text
$merge-reviewer 專案名稱=OrderService 基礎分支=origin/main 比較分支=origin/feature/payment
~~~

預期：同步指定的兩個分支並固定 SHA；遠端失敗或分支刪除時停止。要使用本機主分支時明確改寫 `main`。

### 多儲存庫容器，只審查一個儲存庫

~~~text
C:\Work\Shop
├─ OrderService\.git
├─ CheckoutWeb\.git
└─ InvoiceWorker\.git
~~~

~~~text
$merge-reviewer 工作區="C:\Work\Shop" 專案名稱=OrderService 基礎分支=main 比較分支=feature/payment
~~~

預期：只比較 OrderService 的兩個分支。若回應欄位或事件有變更，追查 CheckoutWeb／InvoiceWorker 的相關呼叫與消費端；其來源是擷取時的 HEAD，本機修改及同名功能分支不會套入。報告仍屬於 OrderService。

### VS Code 多資料夾工作區

例如 `C:\Work\Shop.code-workspace`：

~~~json
{
  "folders": [
    { "path": "Shop/OrderService" },
    { "path": "Shop/CheckoutWeb" }
  ]
}
~~~

~~~text
$merge-reviewer VSCode工作區="C:\Work\Shop.code-workspace" 專案名稱=OrderService 基礎分支=main 比較分支=feature/payment
~~~

預期：只使用 `folders` 宣告的兩個資料夾，不自動搜尋其他相鄰資料夾。支援 JSONC 註解與尾逗號；不存在、不可讀或非本機的資料夾需先修正。

## 5. 多儲存庫延伸判定

| 宣告環境 | 行為 |
| --- | --- |
| 一個 Git 根目錄 | 停用；一般核心審查。 |
| 非 Git 容器只有一個儲存庫 | 停用，不因容器名稱啟用。 |
| 工作區只有一個儲存庫資料夾 | 停用。 |
| 至少兩個不同 Git 根目錄 | 啟用，只追查所選變更的功能鏈。 |
| 重複宣告相同根目錄 | 去重計算。 |
| Git 工作樹（worktree）使用 `.git` 檔案 | 辨識為宣告範圍內的 Git 根目錄。 |

計算單位是本機 Git 根目錄路徑，不是遠端 URL、分支或儲存庫名稱。搜尋到 Git 邊界就停止，不把子模組內部當成已審查來源。

啟用時記錄每個候選是所選儲存庫、相關、無關或未解決。無關儲存庫保留搜尋與排除理由，不進行完整稽核。缺少必要外部消費端、契約或來源時，結果為未完成，已有證據的問題仍保留。

## 6. Merge Report 解讀

報告首行直接呈現合併建議、狀態與摘要，接著列 P0–P3 數量；依序包含「比較資訊、問題、範圍與限制、下一步、技術審查紀錄」。完整欄位與證據規則見[報告風格](report-style.md#報告與聊天結果)。

目前 agent 統一依 Codex Code Review 準則判斷，不另呼叫原生審查引擎，也不設定或變更執行模型。單次仍只有一組 Repo／base／head；所有其他 Repo 固定當前本機 HEAD。先選定全部 SHA，再封裝快照，全部必要快照完成後才開始語意審查。本機分支不連雲端；明確遠端分支按 remote 合併為一次精確 fetch。後續 ref 變動不自動重取或重審。

問題依 P0–P3 全數保留，每項固定包含問題描述、重現原因、預期結果、實際結果、證據、建議改善方式六區塊，整合既有工程內容；新草稿工程欄位必填，舊草稿缺少時顯示「未提供」。技術紀錄在同一份報告末尾以 `<details>` 收合，保留已驗證的完整版本、逐檔覆蓋、功能鏈、Repo 判定、契約及搜尋摘要；人工核對維持展開。省略設定值、私鑰和認證內容，不複製原始搜尋命中結果。

`審查未完成` 表示必要來源或覆蓋仍有缺口；`待人工確認` 表示具體疑問尚缺驗證。兩者均不能當作通過。已確認問題及固定證據仍保留。靜態推導不得聲稱已執行測試或實際重現。待確認項與 P0–P3 分開計數，人工僅編輯 `merge-confirmations:start/end` 中的 checkbox、核對人、日期、確認結果與證據。

只發布一份所選 Repo 的 `review-reports/merge-review-<UTC時間戳>-<base前8碼>-<head前8碼>.md`；內部 draft JSON 留在 owned context，並非另一份公開產出。保留歷史報告。快照結論僅適用當次 SHA；需要審查新版時另開任務。

## 7. 疑難排解

| 訊息／狀況 | 原因 | 處理方式 |
| --- | --- | --- |
| 缺 `project`、`base` 或 `head` | 必要輸入未齊 | 補一個儲存庫及兩個分支。 |
| 專案名稱無法唯一定位 | 同名或不在範圍 | 查看候選，改用完整路徑或修正工作區。 |
| 找不到本機分支 | 拼字、只存在遠端、非分支輸入 | 查看 `git branch`；要讀遠端時用 `origin/分支`。 |
| `fetch` 失敗 | 分支已刪除、連線或權限問題 | 確認指定遠端、分支及連線後重做；不使用舊快取。 |
| 遠端參照不能搭配 `--no-fetch` | 離線設定衝突 | 移除 `--no-fetch` 或改用已存在的本機分支。 |
| 淺層歷史／沒有或多個共同祖先 | 歷史不足或範圍不可靠 | 取得完整歷史，確認同一儲存庫並整理共同歷史後重試。 |
| 預覽衝突或不可用 | 分支整合問題或 Git 不支援 | 處理衝突或更新 Git，再重做；現有結果保持未完成。 |
| 必要功能鏈無法查清 | 缺少必要消費端／契約 | 補正宣告範圍和來源，不任意擴大搜尋。 |
| 工作區或分支擷取後改變 | 後續變更不屬於本次固定快照 | 新快照照常審查固定版本，不自動重審；舊 v3 context 仍保留原本漂移驗證。 |
| 固定物件缺失／損壞 | owned 證據庫不完整 | 回報實際錯誤；不能借用原 Repo 或雲端新版本掩蓋問題。 |
| `unrecognized arguments` | 使用舊入口或拼錯參數 | 按第 8 節改為新版要求。 |
| 報告未建立／驗證失敗 | 引用、覆蓋或寫入問題 | 依訊息修正，重新建立證據目錄；本次暫存會清理。 |
| 報告已建立但清理失敗 | 暫存有占用或檔案問題 | 保留錯誤中的報告路徑，排除占用，再對列出的本次工作階段證據目錄執行 `cleanup`。 |

二進位檔案、子模組內部及未閱讀的必要路徑不自動通過；保留限制或補做檢查。

## 8. 升級與舊入口遷移

0.12.0 是破壞相容性更新。先將舊 `merge-reviewer` 技能目錄備份到 **skills 搜尋範圍之外**，再安裝完整新資料夾，避免留下舊執行元件或重複可發現技能。

| 舊版入口／契約 | 新版處理 |
| --- | --- |
| `Stage`／未提交／包含未提交 | 不支援；先自行提交到分支，再指定兩個分支。 |
| `quick`／自動主分支 | 不支援；明確填三個必要輸入。 |
| `tag`／SHA／HEAD／直接比較 | 不支援；選實際分支，不自動轉換。 |
| `scope`／`target`／`group-root`／多起點 | 不支援；工作區提供背景資訊，永遠選一個儲存庫。 |
| MR 任務／Megin 收據 | 不支援；介接元件需另行遷移。 |
| 證據／結果 v1／v2、`schema-4` 報告 | 新發布器只接受公開 v3；舊報告是歷史紀錄。 |
| 儲存庫群組根目錄報告／多儲存庫明細 | 新版只在所選儲存庫發布單一 Merge Report；相關證據併入對應問題，覆蓋及搜尋紀錄供內部驗證。 |
| 輸出 JSON／`--include-json` | 已移除；最終只發布 Markdown。 |

GitlabWorkSpace **0.24.4** 整合 MergeReviewer **0.15.0** 的原生分支審查介面。仍固定 **MergeReviewer 0.11.0** 的舊版工作台須更新完整 VSIX／組合包；不要將 0.15.0 直接覆蓋到其依賴 MR／`quick`／收據的目錄。獨立技能可直接依本指南安裝完整新版。

## 9. 進階：手動輔助程式

單獨準備證據**不會完成 Code Review**。完整流程是準備 → Codex 閱讀判斷 → Codex 撰寫草稿 → 驗證發布 → 清理。

下面假設 OrderService 已有所需本機分支：

~~~powershell
$skillDir = if ($env:CODEX_HOME) {
    Join-Path $env:CODEX_HOME "skills\merge-reviewer"
} else {
    Join-Path $HOME ".codex\skills\merge-reviewer"
}
$prepared = python "$skillDir/scripts/git_review_context.py" `
    --workspace "C:\Work\OrderService" `
    --project "OrderService" --base "main" --head "feature/payment" --summary --pretty
if ($LASTEXITCODE -ne 0) { throw "證據準備失敗，請依訊息處理。" }
$summary = $prepared | ConvertFrom-Json
$contextDir = $summary.context_dir
$summary.selected_repo
$summary.extension_enabled
~~~

成功回傳 `merge-reviewer-workspace-summary/v1`，包含系統暫存證據目錄、唯一受審儲存庫、固定來源索引、必要覆蓋及缺口；完整 v3 manifest 留在 `$contextDir/manifest.json`。多儲存庫時把 `--workspace` 改為容器，或提供 `--workspace-file`，三個審查輸入相同。需要完整 JSON 的既有整合可省略 `--summary`。

已知的固定讀取及搜尋可用 `review_workspace.py batch-read`／`batch-search --requests-file`，以 context 內的 JSON 陣列減少工具往返；請求與回傳格式見[批次操作](workspace-review.md#批次固定讀取與搜尋)。共用 manifest 的搜尋及契約註冊依序執行；新發現的延伸契約分輪追查。

讓 Codex 依 [v3 結果契約](workspace-review.md) 閱讀固定來源，完成真實審查並寫入 `$contextDir/draft.json`。不能以輔助程式成功訊息取代此步，也不能預填「沒有問題」當作完成。

草稿完成後：

~~~powershell
python "$skillDir/scripts/review_report.py" `
    --context-dir "$contextDir" --result "$contextDir/draft.json"
~~~

發布器標準輸出只回傳 Markdown 報告的完整路徑。證據與草稿的 JSON 僅供內部證據驗證，發布成功或失敗都清理本次工作階段建立並由工具識別的證據目錄；失敗需重新準備並審查。

提前中止且尚未發布時：

~~~powershell
python "$skillDir/scripts/review_session.py" cleanup --context-dir "$contextDir"
~~~

清理指令只接受系統暫存中由工具識別為本次工作階段建立的有效目錄，拒絕連結及使用者自有資料夾，不刪除儲存庫。
