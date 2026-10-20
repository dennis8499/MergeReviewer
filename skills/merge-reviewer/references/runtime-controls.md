# 操作與負載

`--summary-v2` 回傳 `merge-reviewer-workspace-summary/v2`，保留固定版本及必要覆蓋索引。完整 manifest／樹清單留在 owned context。舊 `--summary` 與預設輸出保持原格式。

`review_workspace.py batch-read --auto-content` 選較小的原格式或 `MergeReviewEvidenceBatch/v1`。共享格式的每筆 read 保留 Repo／source／ref／path，以 `content_id` 取 `contents[id].lines`；每個來源仍獨立判斷。`--shared-content` 可明確選共享格式。

搜尋加 `--page-size 100` 得 `ReviewEvidenceView/v1`：`artifact` 是完整輸出的 SHA-256；`result` 每筆 `matches` 有 `view.total_matches`、`next_offset`、`view_complete`。`evidence_complete=true` 只表示完整搜尋已保存，不表示讀者完成語意審查。以相同 artifact 續讀，無須重新 grep：

```powershell
python "<skill-dir>/scripts/review_workspace.py" view --context-dir "<context>" --artifact "<artifact>" --page-size 100 --offset 100
```

完整命中同時保存在 manifest 搜尋 ledger；發布仍以固定物件重播核對。分頁只縮減輸出，必要命中或來源尚未判斷時保留未完成。

新 context 的 `merge_parent_policy=necessary-merge-parents/v1` 只要求與有效差異路徑相關的 parent views。`merge_parent_review` 每筆含 `id`、`status`、`reason`、`evidence`；reviewed 引用最終固定版本，parent 有文字內容時另引用該 parent。not-reviewed 保留原因及未完成。與最終內容無關的歷史變更不擴大審查；無有效差異可空集合。此紀錄是完成對帳，不證明模型判斷正確。

manifest 寫入持有 `manifest-writer.lock` 並核對 `generation`；generation 過期時重新載入，再依新狀態操作，不能覆寫 ledger。程序中斷而遺留不確定鎖時，回報路徑並等待人工查明；不按 PID 或時間自行解鎖。

遠端同步使用單一 `git-network.lock`；兩 Reviewer 與工作台共用 `GitLabWorkspaceLease/v1`。Runtime 使用 `GITLAB_WORKSPACE_RUNTIME_DIR`，否則 Windows `%LOCALAPPDATA%/GitLabWorkspace/runtime-v1`；POSIX 為 `$XDG_RUNTIME_DIR/gitlab-workspace/runtime-v1` 或 `~/.cache/gitlab-workspace/runtime-v1`。每個獨立請求都重新同步，無跨請求 freshness 快取。Git 程序樹終止後才釋放 nonce 所有權；不確定時隔離並保留鎖。鎖只限制同一 runtime 使用者，不能代表整台 GitLab 的容量保證。
