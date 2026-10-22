---
name: merge-reviewer
description: 依明確指定的單一 Repo、基礎分支與比較分支，以 Codex 原生 Code Review 審查分支差異並產生繁體中文 Merge Report。多 Repo 工作區延伸追查所選變更的契約與消費端；支援陸續啟動多份審查及共用同步。未提交內容與上版環境核對另循其流程。
metadata:
  short-description: 固定版本的合併審查、必要影響追查與報告
---

# Merge Reviewer

由執行本 Skill 的 agent 依 Codex Code Review 準則判斷本次引入的可修正問題及 P0–P3；helper 準備固定證據、驗證覆蓋及發布。helper 成功不代表語意審查完成。正常啟動無條件破壞整批持久資料的 P0 例子及條件性影響的 P1 邊界，見[審查規則](references/review-rules.md)。

開始時一次讀取[審查規則](references/review-rules.md)、[三階段操作與 JSON 範例](references/session-workflow.md)、[v3 結果契約](references/workspace-review.md)。同層 `../output-style/SKILL.md` 存在時套用其情境風格，否則使用[報告風格](references/report-style.md)。工作區／根目錄／巢狀 `AGENTS.md` 以擷取時適用的固定內容為準。

三項必填：恰一個 Repo 名稱或路徑、基礎分支、比較分支。缺漏或有歧義時補齊後才開始；分支使用本機名稱或明確含遠端的名稱，例如 `origin/main`。只接受已提交分支；tag、commit、HEAD、Stage、quick、多起點及 MR task 不屬此入口。工作區以使用者宣告根目錄及實際 `.code-workspace` 為界。

## 1. start：固定證據與草稿

~~~powershell
python "<skill-dir>/scripts/review_session.py" start --workspace "<declared-root>" --project "<Repo-name-or-path>" --base "<base>" --head "<head>"
~~~

已宣告 `.code-workspace` 時加 `--workspace-file "<file>"`。群組根目錄預設使用 Repo 外的 `.review-runtime/`；根目錄本身是 Git Repo 時指定 Repo 外可寫的 `--runtime-root`。工作台提供的 runtime／auth-scope 一併沿用。

首次完整快照以前若有 `dubious ownership`，依[確切 Repo 修復](references/review-rules.md#確切-repo-的-ownership-修復)保存診斷，僅在同一 helper 程序授權已報錯的確切 Repo。

保存回傳的 `request_id`、`runtime_root`、`context_dir`、草稿路徑與索引。start 已預檢權限、取得或重用必要遠端同步、固定所有 SHA 並封裝自足證據。其他 Repo 每次重新固定本機 HEAD；草稿與紀錄獨立。相同同步需求進行中或完成未滿 300 秒可共用，命中不延長期限；已固定的審查持續使用原快照。需要新版本時另起 `start --refresh`。

閱讀回傳指示、完整必要差異及背景。固定比較 `merge-base(base, head) → head`，另檢查必要 merge-parent 與 merge-preview；原始工作目錄、索引及目前分支保持原樣。

## 2. step：閱讀、判斷及必要追查

~~~powershell
python "<skill-dir>/scripts/review_session.py" step --runtime-root "<runtime>" --request-id "<id>" --requests-file "<context>/round-1.json"
~~~

每輪將已知的固定讀取、契約註冊及搜尋合為有序操作陣列，各操作給唯一 ID；父契約先於子契約，搜尋接在註冊後。格式與範例見三階段操作文件，正常流程無須查 helper 原始碼。

逐一判斷 `required_coverage`、`required_merge_views`，讀取基礎版本、最終內容及必要 parent。二進位、刪除、改名及 submodule 也需明確判定；`ancestor-noop/v1` 的空集合依 helper 推導。

多 Repo 時從本次差異建立有固定 anchor 的契約，以 exact symbol、相關別名／wrapper 追查直接及間接 consumer；新發現的 consumer 契約才進下一輪。每個其他 Repo 記錄實際相關性與搜尋證據，既有消費端的必要條件未查清則保留未完成。一般單 Repo 審查不要求跨 Repo 契約。

預設回傳最多 24 KiB，全文與命中完整落盤。依回傳 artifact、JSON pointer 及 offset 續讀；`evidence_complete` 只表示完整保存，必要內容未讀完不能標已審查。精確行段嚴格驗證；定位命中可用 `around_line` 自動縮到檔案邊界。

草稿由 agent 填寫語意內容、逐檔與 parent 判定、契約影響鏈、Repo 判定及測試情形。只寫實際執行的測試；靜態推導用 `code-derived`。待確認項須有固定證據指出實際必要關係，不能憑空假設工作區外 strict consumer。必要內容查不清時保持「審查未完成」。

## 3. finish：驗證、發布與清理

~~~powershell
python "<skill-dir>/scripts/review_session.py" finish --runtime-root "<runtime>" --request-id "<id>"
~~~

沿用產生的 v3 草稿與 `report_layout="engineering-review/v1"`。每項 finding 填齊 `operation_scenario`、`technical_trigger`、`technical_explanation`、`impact_scope`、`engineering_fix` 及結果契約原有欄位；「未提供」不是完成。

finish 集中列出可修欄位錯誤；成功發布只做一次完整證據驗證，直接產生「審查結論、問題總攬、問題詳情（工程師參考）、範圍與限制、技術審查紀錄（工程師參考）」五個 H2。保留問題六區塊、敏感內容處理及展開的人工核對區。報告只寫入所選 Repo 的 `review-reports/`，不發布到 GitLab。

- exit **0**：發布已保存，context 清理。相同 request 重試回傳既有報告。
- exit **3**：可修草稿或批次輸入錯誤，沿同一固定證據修正／從 `next_index` 續跑。
- exit **2**：終止錯誤，停止本次審查並回報；不退回舊 refs、不自動重新 capture。

對話壓縮後以 `status --runtime-root "<runtime>" --request-id "<id>"` 恢復版本、artifact、草稿及未完成項。放棄時用 `cancel` 同一 ID，只取消該請求；舊 context 的 `cleanup --context-dir` 仍保留。Git 網路鎖由 helper 管理，不按 PID 猜測其他審查或自行解鎖。

交付本機報告連結、重要發現及必要缺口；合併建議只適用固定 SHA。安裝／升級見[使用指南](references/usage.md)，配額與舊 CLI 相容性見[操作與負載](references/runtime-controls.md)。
