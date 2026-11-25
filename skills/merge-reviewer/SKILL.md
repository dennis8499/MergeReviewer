---
name: merge-reviewer
description: 依明確指定的單一 Repo、基礎分支與比較分支，以 Codex 原生 Code Review 審查分支差異並產生繁體中文 Merge Report。多 Repo 工作區延伸追查所選變更的契約與消費端；支援陸續啟動多份審查及共用同步。未提交內容與上版環境核對另循其流程。
metadata:
  short-description: 固定版本的合併審查、必要影響追查與報告
---

# Merge Reviewer

## 必須遵守的來源與審查規則

- `main`、`feature/payment` 等未加 remote 前綴的分支只解析為本機 ref；不得從 upstream 推定需要同步。只有明確指定 `origin/main` 這類遠端 ref 才能依五分鐘同步收據同步該 ref；`--refresh` 只更新本次指定且需要更新的 ref。
- 間接 Repo 一律固定並審查本機 `HEAD`。分支審查不呼叫 GitLab MR、分支或檔案 API。快照完成後，Repo 的讀取、搜尋及確認都使用本機固定證據。`--no-fetch` 的請求全程只用本機證據。套件對外查證前，確認同一請求的 `no_fetch=false` 且會話可使用 Web Search。`check` 和 `finish` 使用已保存的結果。
- 單 Repo 必須檢查每個變更、相關呼叫端、base/head、適用的 merge-parent 與 merge-preview。多 Repo 只有一個受審 Repo，其餘 Repo 只用於契約消費端追查；按證據標示 `contained`、`propagates` 或 `unresolved`。
- 只回報本次變更引入、可重現或可由固定證據推導且可修正的缺陷，保留 P0–P3，說明觸發條件、影響範圍、修正方向與 Repo／來源／SHA／路徑／行號。區分靜態推導與實際重現，遮蔽敏感值；排除既有問題、已修復問題及安全重構誤報。
- 逐層套用根目錄與巢狀 `AGENTS.md`：只採用涵蓋目標路徑的規則，並檢查相反的安全範例。零命中、保存 artifact、helper 成功都不能代替語意判斷或表示審查完成；必要證據未讀完、關鍵路徑 unresolved 或安全反例未核對時不得標記完成。

由執行本 Skill 的 agent 依 Codex Code Review 準則判斷本次引入的可修正問題及 P0–P3；helper 準備固定證據、驗證覆蓋及發布。helper 成功不代表語意審查完成。正常啟動無條件破壞整批持久資料的 P0 例子及條件性影響的 P1 邊界，見[審查規則](references/review-rules.md)。

開始時讀[審查規則](references/review-rules.md)，使用 start 回傳的固定索引與草稿。操作或續讀 artifact 查[操作範例](references/session-workflow.md)；多 Repo 判定前讀[契約邊界](references/contract-boundaries.md)；填精簡草稿才讀[草稿格式](references/authoring.md)，舊草稿才讀[v3 契約](references/workspace-review.md)。撰寫報告時遵循[報告風格](references/report-style.md)；只有使用者明確要求或選取 output-style，才載入該 Skill 與本次必要參考，必要報告欄位與證據契約仍保留。適用的 `AGENTS.md` 以固定內容為準。

三項必填：恰一個 Repo 名稱或路徑、基礎分支、比較分支。缺漏或有歧義時補齊後才開始；分支使用本機名稱或明確含遠端的名稱，例如 `origin/main`。只接受已提交分支；tag、commit、HEAD、Stage、quick、多起點及 MR task 不屬此入口。工作區以使用者宣告根目錄及實際 `.code-workspace` 為界。

## 每輪閱讀與保存

1. 用固定索引選下一條功能鏈，合併該輪獨立讀取與搜尋。首次 step 使用 `initial_hunk_reads_file`（如有），否則使用 `initial_reads_file`。
2. 回應有 `paging_requests_file` 時，原樣交給 `view --requests-file`；它含逐檔 diff 索引與共用正文。否則將工具的 continuation 直接序列化並沿實際游標續讀。支援 `paging_state` 時依[分頁與恢復](references/paging-workflow.md)確認與保存。
3. 必要內容及安全反例讀完後作語意判斷；核對變更前後、合法輸入或可達路徑、必要前提及替代解釋。證據保存、頁面返回與「已審查」分開確認。
4. 每條鏈完成即原子保存判斷、引用、待讀狀態與下一個操作。成功取得並解析頁面後才保存其確認，遺失回應可沿原狀態重送。
5. 恢復先讀 `status --summary`，依 checkpoint、`initial_evidence` 或 `next_action` 接續。遇到 `retry-step` 時依[操作範例](references/session-workflow.md)提交原批次。發布後核對收據與 `cleanup_pending`。

## 1. start：固定證據與草稿

由目前 agent 使用已選模型與強度直接審查。先查詢安裝版本支援的協定能力；只有 `capabilities` 成功回報並明確列出 `compact` 才使用精簡草稿。舊版不支援能力查詢時，直接使用 v3：

~~~powershell
python "<skill-dir>/scripts/review_session.py" capabilities
python "<skill-dir>/scripts/review_session.py" start --workspace "<declared-root>" --project "<Repo-name-or-path>" --base "<base>" --head "<head>" --draft-format compact
~~~

能力列出 `launch_preflight` 時，首次啟動先以相同 workspace／project／base／head／runtime 參數執行 `preflight`，`valid=true` 後執行 start。預檢純讀、不 fetch、不建立 runtime 或 request；start 仍會再次核對。使用工作台提供的結構化啟動資料；沿用原始路徑，既存 request 直接恢復其固定證據。

已宣告 `.code-workspace` 時加 `--workspace-file "<file>"`。群組根目錄預設使用 Repo 外的 `.review-runtime/`；根目錄本身是 Git Repo 時指定 Repo 外可寫的 `--runtime-root`。工作台提供的 runtime／auth-scope 一併沿用。

首次完整快照以前若有 `dubious ownership`，依[確切 Repo 修復](references/review-rules.md#確切-repo-的-ownership-修復)保存診斷，僅在同一 helper 程序授權已報錯的確切 Repo。

保存 `request_id`、`runtime_root`、`context_dir`、草稿路徑、索引與 `tool_identity`。capabilities 回報技能版本、helper 摘要與 Python 版本；start 保存本次工具識別。start 固定所有 SHA 並封裝自足證據；其他 Repo 每次重新固定本機 HEAD。遠端各 ref 完成同步後可重用 300 秒，命中不延長期限；不同審查的草稿與快照獨立。需要新版本時另起 `start --refresh`。

閱讀回傳指示、完整必要差異及背景。固定比較 `merge-base(base, head) → head`，另檢查必要 merge-parent 與 merge-preview；原始工作目錄、索引及目前分支保持原樣。

## 2. step：閱讀、判斷及必要追查

~~~powershell
python "<skill-dir>/scripts/review_session.py" step --runtime-root "<runtime>" --request-id "<id>" --requests-file "<initial-read-file>"
~~~

首輪將啟動指南的讀取檔原樣交給 step。讀取固定差異、適用的 `AGENTS.md` 與各版本的內容。只有區段證據時，依呼叫鏈補讀完整語意所需的內容。必要 parent 核對與有效變更路徑相交的差異及最終內容。後續每輪合併固定讀取、契約註冊及搜尋，各操作給唯一 ID，先登錄父契約。

遇到可能改變相依套件或版本的宣告、來源設定或鎖定檔差異時，先讀[套件版本查核](references/dependency-review.md)。將每項適用的開源套件異動查證完畢，或把確實受阻的項目寫入 `pending_confirmations`，再把對應路徑標記為 `reviewed`。

逐一判斷 `required_coverage`、`required_merge_views`，讀取基礎版本、最終內容及必要 parent。每完成一條功能／契約鏈，就把判斷、固定證據引用與未完成項寫入草稿；恢復時由 `status` 的 `remaining` 接續未完成項。二進位讀取會回傳大小與 hash、但不提供文字；無法可靠解碼的文字會標示 `lossy=true`，兩者都不能當成已讀懂。刪除 API／欄位但檔案保留時，舊符號錨定本次比較左側的實際刪除行，最終內容仍須覆蓋；來源與影響節點範例見[操作範例](references/session-workflow.md#刪除契約與影響圖)。改名及 submodule 也需明確判定。`ancestor-noop/v1` 的空集合依 helper 推導。

多 Repo 先按[契約邊界](references/contract-boundaries.md)明確選擇 `repo-local`、`identified-repos` 或 `workspace` 範圍；Repo 內行為契約不會只因檔名、public 函式或泛稱 export 而擴成 workspace 契約。`propagates` 只追查固定證據指出的消費端；未知但必要的消費端保持 `unresolved`。每個其他 Repo 仍有 disposition。

回應上限 24 KiB，全文與命中落盤；逐操作查看是否完整回傳，必要時使用純讀取 `view` 命令按 artifact、JSON pointer 及 offset 續讀。能力支援 `batch_artifact_view` 時，用 `view --requests-file` 合併獨立續讀；批次 `next_index` 表示尚未回傳的請求，各頁的 continue 仍須讀完。`paging_state` 的去重、確認與恢復依[分頁與恢復](references/paging-workflow.md)；舊版仍沿原批次介面。不要把翻頁包成 step 操作。敏感值在展示內容中遮罩，行號與原始內容 hash 仍保留。`evidence_complete` 表示保存，語意「已審查」由 agent 判斷；必要內容未讀完不能標已審查。定位命中可用 `around_line`。

有 `paging_requests_file` 時優先使用該固定請求檔，讀取原始來源對應及各份共用正文；實際返回並讀完後才確認 delta。`paging_projection` 僅改呈現，既有 artifact 仍可按原 pointer 重看。操作格式不確定時使用安裝版 `schema --operation author` 的巢狀欄位及範例；PowerShell 用 `ConvertTo-Json -InputObject @($operations) -Depth 100` 保留單筆陣列。

能力列出 `atomic_authoring` 時，以 `step` 的 `author` 操作原子保存精簡更新與 checkpoint，詳見草稿格式。草稿由 agent 填寫逐檔與 parent 判定、影響鏈、Repo 判定及實際測試；靜態推導用 `code-derived`。每完成一條契約鏈即保存草稿、回讀確認再 check。待確認須有實際必要關係的固定證據；有可取得的必要內容就沿 remaining 繼續補查，確實受阻才保留缺口並發布未完成報告。

`tests_not_executed` 記錄未執行測試；`scope_notes` 記錄宣告工作區等非阻擋範圍說明；`limitations` 僅記錄影響判斷的必要證據缺口。例如「未執行目標測試」放前者，不將已完整核對的靜態審查改成未完成；已證實必要的 consumer 無法取得則保留 limitations。

## 3. check／finish：修稿、驗證與發布

~~~powershell
python "<skill-dir>/scripts/review_session.py" check --runtime-root "<runtime>" --request-id "<id>"
python "<skill-dir>/scripts/review_session.py" finish --runtime-root "<runtime>" --request-id "<id>"
~~~

可沿用 v3 草稿或改用 `merge-reviewer-authoring/v1`：每項工程事實寫一次，證據引用實際 read 操作 ID 與行段；helper 補入固定來源及重複欄位，不代填判斷。先用 check 查看欄位、搜尋與邊界缺口；check 不執行 Git，`evidence_verified=false`。

用 check 的 `expected_outcome` 與 `blocking_gaps` 判斷補查範圍。若有 `publication_readiness=ready`，可進入 `finish`，合法待確認項仍會標在收據。若確定無法補足必要證據，使用 `finish` 發布如實標示未完成的報告。

finish 完整驗證固定證據並在所選 Repo 的 `review-reports/` 發布五區塊報告，保留問題、敏感內容處理及展開的人工核對區；不發布至 GitLab。

- exit **0**：報告已保存；`cleanup_pending=true` 時以同一 ID 重試清理。
- exit **3**：沿原快照修復草稿或從 `next_index` 續跑。`phase=publication-repair` 時修復報告輸出權限或容量，再用同一 ID 重試 finish；原草稿與 SHA 保留。
- exit **2**：停止並回報終止錯誤，不退回舊 refs 或重新 capture。

純報告渲染的內部例外會 terminal 停止並保留 owned context 24 小時供診斷；相同 ID 不可重試發布或換 helper 接續。`cancel` 可清理該證據；逾時由下一次 start 惰性清理。既有發布收據與 outcome 保持不變。

發布後以 `review_outcome.review_complete` 和缺口判斷審查是否完整；舊紀錄的 unknown 不算完成。人工核對區編輯不會重算判斷；細節見[恢復與發布](references/session-workflow.md#完成取消與錯誤)。

對話壓縮後先以 `status --runtime-root "<runtime>" --request-id "<id>" --summary` 取得保存的狀態、最近檢查與 `next_action`；`check_current=false` 時以 check 取得最新診斷；較舊能力未列出 `summary_status` 時使用原 status。沿 details 取得完整版本、artifact、草稿及未完成項；摘要的預覽與計數不代替證據續讀。放棄時用 `cancel` 同一 ID，只取消該請求；舊 context 的 `cleanup --context-dir` 仍保留。Git 網路鎖由 helper 管理，不按 PID 猜測其他審查或自行解鎖。

能力列出 `read_only_inspection` 時，`status --summary` 保證不寫檔；artifact 續讀使用 `view --read-only`，沿回傳的唯讀 continuation 接續，不建立額外暫存或更新 metrics。

交付本機報告連結、重要發現及必要缺口；合併建議只適用固定 SHA。安裝／升級見[使用指南](references/usage.md)，配額與舊 CLI 相容性見[操作與負載](references/runtime-controls.md)。
