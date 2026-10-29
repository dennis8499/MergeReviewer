---
name: merge-reviewer
description: 依明確指定的單一 Repo、基礎分支與比較分支，以 Codex 原生 Code Review 審查分支差異並產生繁體中文 Merge Report。多 Repo 工作區延伸追查所選變更的契約與消費端；支援陸續啟動多份審查及共用同步。未提交內容與上版環境核對另循其流程。
metadata:
  short-description: 固定版本的合併審查、必要影響追查與報告
---

# Merge Reviewer

## 必須遵守的來源與審查規則

- `main`、`feature/payment` 等未加 remote 前綴的分支只解析為本機 ref；不得從 upstream 推定需要同步。只有明確指定 `origin/main` 這類遠端 ref 才能依五分鐘同步收據同步該 ref；`--refresh` 只更新本次指定且需要更新的 ref。
- 間接 Repo 一律固定並審查本機 `HEAD`。分支審查不呼叫 GitLab MR、分支或檔案 API。快照完成後，讀取、搜尋、修稿、`check`、`finish` 與恢復都使用本機固定證據；來源不足就列出缺口並維持未完成。
- 單 Repo 必須檢查每個變更、相關呼叫端、base/head、適用的 merge-parent 與 merge-preview。多 Repo 只有一個受審 Repo，其餘 Repo 只用於契約消費端追查；按證據標示 `contained`、`propagates` 或 `unresolved`。
- 只回報本次變更引入、可重現或可由固定證據推導且可修正的缺陷，保留 P0–P3，說明觸發條件、影響範圍、修正方向與 Repo／來源／SHA／路徑／行號。區分靜態推導與實際重現，遮蔽敏感值；排除既有問題、已修復問題及安全重構誤報。
- 逐層套用根目錄與巢狀 `AGENTS.md`：只採用涵蓋目標路徑的規則，並檢查相反的安全範例。零命中、保存 artifact、helper 成功都不能代替語意判斷或表示審查完成；必要證據未讀完、關鍵路徑 unresolved 或安全反例未核對時不得標記完成。

由執行本 Skill 的 agent 依 Codex Code Review 準則判斷本次引入的可修正問題及 P0–P3；helper 準備固定證據、驗證覆蓋及發布。helper 成功不代表語意審查完成。正常啟動無條件破壞整批持久資料的 P0 例子及條件性影響的 P1 邊界，見[審查規則](references/review-rules.md)。

開始時讀[審查規則](references/review-rules.md)，使用 start 回傳的固定索引與草稿。操作或續讀 artifact 查[操作範例](references/session-workflow.md)；多 Repo 判定前讀[契約邊界](references/contract-boundaries.md)；填精簡草稿查[草稿格式](references/authoring.md)，舊草稿查[v3 契約](references/workspace-review.md)。同層 `../output-style/SKILL.md` 存在時套用其情境風格，否則用[報告風格](references/report-style.md)。適用的 `AGENTS.md` 以固定內容為準。

三項必填：恰一個 Repo 名稱或路徑、基礎分支、比較分支。缺漏或有歧義時補齊後才開始；分支使用本機名稱或明確含遠端的名稱，例如 `origin/main`。只接受已提交分支；tag、commit、HEAD、Stage、quick、多起點及 MR task 不屬此入口。工作區以使用者宣告根目錄及實際 `.code-workspace` 為界。

## 1. start：固定證據與草稿

~~~powershell
python "<skill-dir>/scripts/review_session.py" start --workspace "<declared-root>" --project "<Repo-name-or-path>" --base "<base>" --head "<head>" --draft-format compact
~~~

已宣告 `.code-workspace` 時加 `--workspace-file "<file>"`。群組根目錄預設使用 Repo 外的 `.review-runtime/`；根目錄本身是 Git Repo 時指定 Repo 外可寫的 `--runtime-root`。工作台提供的 runtime／auth-scope 一併沿用。

首次完整快照以前若有 `dubious ownership`，依[確切 Repo 修復](references/review-rules.md#確切-repo-的-ownership-修復)保存診斷，僅在同一 helper 程序授權已報錯的確切 Repo。

保存 `request_id`、`runtime_root`、`context_dir`、草稿路徑與索引。start 固定所有 SHA 並封裝自足證據；其他 Repo 每次重新固定本機 HEAD。遠端各 ref 完成同步後可重用 300 秒，命中不延長期限；不同審查的草稿與快照獨立。需要新版本時另起 `start --refresh`。

閱讀回傳指示、完整必要差異及背景。固定比較 `merge-base(base, head) → head`，另檢查必要 merge-parent 與 merge-preview；原始工作目錄、索引及目前分支保持原樣。

## 2. step：閱讀、判斷及必要追查

~~~powershell
python "<skill-dir>/scripts/review_session.py" step --runtime-root "<runtime>" --request-id "<id>" --requests-file "<initial_reads_file>"
~~~

首輪將 start 產生的 `initial_reads_file` 原樣交給 step，先讀取已擷取 diff、適用的 `AGENTS.md`、改動檔及必要 base／head／merge-preview／parent 內容；逐項讀完再決定動態呼叫鏈追查。後續每輪合併固定讀取、契約註冊及搜尋；各操作給唯一 ID，先登錄父契約，再登錄子契約及搜尋。

逐一判斷 `required_coverage`、`required_merge_views`，讀取基礎版本、最終內容及必要 parent。二進位、刪除、改名及 submodule 也需明確判定；`ancestor-noop/v1` 的空集合依 helper 推導。

多 Repo 逐筆完成 `boundary_changes`：`contained` 引用變更及必要使用／輸出證據；`propagates` 建立實際 workspace 契約與連通影響圖，追查直接及間接消費端；不確定則 `unresolved`。Repo 內搜尋可綁定 `change_id`，跨 Repo 搜尋須綁定已註冊契約。副檔名、private 名稱或零命中不足以排除外部影響；每個其他 Repo 仍保留判定。

回應上限 24 KiB，全文與命中落盤；逐操作查看是否完整回傳，必要時依 artifact、JSON pointer 及 offset 續讀。`evidence_complete` 表示保存，語意「已審查」由 agent 判斷；必要內容未讀完不能標已審查。定位命中可用 `around_line`。

草稿由 agent 填寫逐檔與 parent 判定、影響鏈、Repo 判定及實際測試；靜態推導用 `code-derived`。待確認須有實際必要關係的固定證據；必要內容查不清時保持未完成。

## 3. check／finish：修稿、驗證與發布

~~~powershell
python "<skill-dir>/scripts/review_session.py" check --runtime-root "<runtime>" --request-id "<id>"
python "<skill-dir>/scripts/review_session.py" finish --runtime-root "<runtime>" --request-id "<id>"
~~~

可沿用 v3 草稿或改用 `merge-reviewer-authoring/v1`：每項工程事實寫一次，證據引用實際 read 操作 ID 與行段；helper 補入固定來源及重複欄位，不代填判斷。先用 check 查看欄位、搜尋與邊界缺口；check 不執行 Git，`evidence_verified=false`。

finish 完整驗證固定證據並在所選 Repo 的 `review-reports/` 發布五區塊報告，保留問題、敏感內容處理及展開的人工核對區；不發布至 GitLab。

- exit **0**：報告已保存；`cleanup_pending=true` 時以同一 ID 重試清理。
- exit **3**：沿原快照修復草稿或從 `next_index` 續跑。
- exit **2**：停止並回報終止錯誤，不退回舊 refs 或重新 capture。

發布後以 `review_outcome.review_complete` 和缺口判斷審查是否完整；舊紀錄的 unknown 不算完成。人工核對區編輯不會重算判斷；細節見[恢復與發布](references/session-workflow.md#完成取消與錯誤)。

對話壓縮後以 `status --runtime-root "<runtime>" --request-id "<id>"` 恢復版本、artifact、草稿及未完成項。放棄時用 `cancel` 同一 ID，只取消該請求；舊 context 的 `cleanup --context-dir` 仍保留。Git 網路鎖由 helper 管理，不按 PID 猜測其他審查或自行解鎖。

交付本機報告連結、重要發現及必要缺口；合併建議只適用固定 SHA。安裝／升級見[使用指南](references/usage.md)，配額與舊 CLI 相容性見[操作與負載](references/runtime-controls.md)。
