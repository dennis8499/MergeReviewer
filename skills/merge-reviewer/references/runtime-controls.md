# 操作與負載

新請求使用 [start／step／check／finish](session-workflow.md)，以下獨立 CLI 為相容入口。共用協定集中在 `review_coordinator.py`；現有讀取、搜尋及發布仍各一份實作。check／status 的 diagnostics 不執行 Git，輸出明確 `evidence_verified=false`；固定證據仍由 finish 驗證。

新流程以 Git common directory、remote URL、有效認證設定／使用者範圍及各分支識別同步。每個 ref 成功後 300 秒有效、命中不延長；失敗 ref 冷卻 30 秒。不同 head 可重用同一 base，只同步缺少或失效的 refs；`--refresh` 重同步要求的 refs。快取命中不呼叫 ls-remote／GitLab API；fetch 使用唯一私人 refs，保留 atomic、無 tags／prune／submodule／maintenance。遠端、帳號或工作台已知 fetch／push／pull 變動使後續請求失效；已固定快照繼續離線。

網路只占用既有共用 Git lease（配額 1），Git 程序樹結束後立即釋放；本機封裝另以 OS 鎖限全 runtime 2 份。peer pack 依 Repo／SHA／object format／policy 共用單一 producer，驗證後複製進各自 context；LRU 上限 2 GiB。所有需協作的審查使用同一 runtime。成功重用者的 fetch 計數為 0，`sync_activity.mode` 記錄 owner／joined／cached，避免重複加總。

新 runtime 的 owner／state／同步／封裝鎖由 OS 檔案鎖維持，程序死亡即釋放；共用網路 lease 的未知 Git 程序樹仍隔離。草稿、manifest、artifact、報告及清理獨立；終止紀錄保留 24 小時。既有獨立 CLI 維持原本同步語意，只有新 start 啟用跨請求快取。

`--summary-v2` 回傳 `merge-reviewer-workspace-summary/v2`，保留固定版本及必要覆蓋索引。完整 manifest／樹清單留在 owned context。舊 `--summary` 與預設輸出保持原格式。

一次請求由主 agent capture 一次並保存 `context_dir`；子 agent 共用固定 context，契約／搜尋寫入保持單一寫入者。只有明確遠端 base／head 按 remote 合併為一次同步；其他 Repo 使用本機 HEAD。草稿驗證 exit 3 沿原 context 修正，終止錯誤回報後停止，不自動重新 capture。

`network_activity` 記錄 `fetch_attempt_count`、`fetch_success_count`、去重的 `requested_ref_count` 及實際 fetch 成功的 `synced_ref_count`。首次抓兩個 ref 為 `1/1/2/2`，重用 base 並抓另一 head 為 `1/1/2/1`。`sync_activity` 逐收據記錄 owner／joined／cached、每個 ref 的 SHA 與同步時間，並區分要求及實際同步的 refs；`fetches` 列出要求的遠端分支，長度不是連線次數。這些數字不是 Git 傳輸內部的 HTTP／SSH 請求數。本機分支不需要遠端同步。

`review_workspace.py batch-read --auto-content` 選較小的原格式或 `MergeReviewEvidenceBatch/v1`。共享格式的每筆 read 保留 Repo／source／ref／path，以 `content_id` 取 `contents[id].lines`；每個來源仍獨立判斷。同一全文的不同切片使用各自的 `content_id`，`sha256` 仍是全文 hash。`--shared-content` 可明確選共享格式。

已知必要行段時用 `read --line-start/--line-end` 或批次 `line_start`／`line_end`；保留全文 hash 及原始行號。行段、inventory 索引與契約 JSON 的確切格式見[證據與 v3 結果契約](workspace-review.md)。

搜尋加 `--page-size 100` 得 `ReviewEvidenceView/v1`：`artifact` 是完整輸出的 SHA-256；`result` 每筆 `matches` 有 `view.total_matches`、`next_offset`、`view_complete`。`evidence_complete=true` 只表示完整搜尋已保存，不表示讀者完成語意審查。以相同 artifact 續讀，無須重新 grep：

```powershell
python "<skill-dir>/scripts/review_workspace.py" view --context-dir "<context>" --artifact "<artifact>" --page-size 100 --offset 100
```

只讀部分搜尋時加可重複的 `--search-id S-0001`；先篩選再對 matches 分頁，未知 ID 報錯。原 artifact hash 與搜尋 ledger 維持原狀。stdout 的 `result.searches` wrapper 與磁碟 artifact 的頂層 `searches` 不同；用 `view` 續讀，完整範例見結果契約。

完整命中同時保存在 manifest 搜尋 ledger；發布仍以固定物件重播核對。分頁只縮減輸出，必要命中或來源尚未判斷時保留未完成。

ordered step 的成功搜尋另以 context 內 `search-cache/` 跨輪保存，綁定固定來源與全部搜尋選項，驗 checksum 後作傳輸快取。每筆邏輯搜尋仍有自己的契約與 ledger ID；失敗不重用，快取損壞重新搜尋。finish 的 FixedSearchCache 不讀此持久快取，實際重播保存的 query 與固定來源。該快取不是完成證明，也不跨 context 共用。

新 context 的 `merge_parent_policy=necessary-merge-parents/v1` 只要求與有效差異路徑相關的 parent views。`merge_parent_review` 每筆含 `id`、`status`、`reason`、`evidence`；reviewed 引用最終固定版本，parent 有文字內容時另引用該 parent。not-reviewed 保留原因及未完成。與最終內容無關的歷史變更不擴大審查；無有效差異可空集合。此紀錄是完成對帳，不證明模型判斷正確。

新 v2 owner context 的 manifest 寫入使用 runtime/jobs 下的 OS 檔案鎖；程序中斷即釋放。舊 context 仍持有 `manifest-writer.lock`。兩者皆核對 `generation`；generation 過期時重新載入，再依新狀態操作，不能覆寫 ledger。程序中斷而遺留不確定鎖時，回報路徑並等待人工查明；不按 PID 或時間自行解鎖。

遠端同步使用單一 `git-network.lock`；兩 Reviewer 與工作台共用 `GitLabWorkspaceLease/v1`。網路 runtime 使用 `GITLAB_WORKSPACE_RUNTIME_DIR`，否則 Windows `%LOCALAPPDATA%/GitLabWorkspace/runtime-v1`；POSIX 為 `$XDG_RUNTIME_DIR/gitlab-workspace/runtime-v1` 或 `~/.cache/gitlab-workspace/runtime-v1`。Git 程序樹終止後才釋放 nonce 所有權；不確定時隔離並保留鎖。配額適用同一 runtime 使用者。
