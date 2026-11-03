# 實際 Log 衍生的流程優化驗收

本輪只修改 MergeReviewer。分析來源為 2026-11-02 至 2026-11-03 的 12 份 MergeReviewer 紀錄；另一份 ReleaseReviewer 紀錄不列入統計。Log 中的歷史指令只作分析資料，業務程式、帳號、主機與原始設定值不複製至測試夾具。

## 回歸案例

| 來源與現象 | 脫敏驗收 |
|---|---|
| 發布暫存目錄只有受限 ACL，報告建立後一般讀取仍失敗 | 預檢與發布共用 staging／commit；Windows 暫存檔繼承輸出目錄 ACL，子程序可讀取成品 |
| 發布失敗終止 session，重擷取後分支已移動 | 可恢復輸出錯誤保留 request、草稿與證據；來源分支移動後仍發布原 SHA；重試只產生一份報告 |
| `check` 放過沒有 impact 證據的 related Repo，`finish` 才拒絕 | 共用純結構規則，在完整 Git 驗證以前取得相同錯誤 |
| compact 欄位衝突、read ID／artifact 混用與未讀行號 | 一次回報獨立錯誤、確切 pointer、可引用 read ID 與行段 |
| 大量分頁及逐次讀取 hash／狀態 | 批次頁面等同單筆內容，重用已驗 artifact，不改 manifest、不執行 Git；summary 保留狀態、計數及下一步 |
| 本次原始碼分析另重現的分頁錯誤 | 12,000 字長字串、巢狀清單與大型字典的省略內容都有續讀入口，尚未返回時維持未完成 |
| 安裝版本無法完整對帳 | capabilities 回報版本與 helper 摘要，start 將工具及 Python 識別保存至 job |

自動案例位於 `tests/test_log_regressions.py`，包含固定證據遭破壞後仍終止、cancel 清理保留 context，以及 CLI 批次讀取／summary 的相容性。既有 `tests/test_report_retry.py` 繼續核對低階 CLI 的預設失敗清理。

Windows 測試檢查實際 DACL 是否繼承，並由子程序讀取成品。原企業環境的沙箱 token、工作流程包 runtime ACL 與一般使用者身分切換仍需現場驗收；測試不修改外部 runtime 或帳號權限。

## 固定來源配對重播

使用全新輸出目錄執行：

```powershell
python -X utf8 -B tests/replay_log_workflow.py --baseline-revision 87a6e05 --output-dir test-artifacts/log-workflow-paired --repeats 3
```

工具從指定本機 commit 封存 baseline，複製並固定 candidate，全目錄摘要保存在 identity.json。它只在一次性 Git 夾具中建立本機分支，不同步遠端。每組交換 baseline／candidate 執行順序；兩版共用相同固定來源及頁面請求。baseline 使用既有單筆 view／完整 status；candidate 依 capabilities 使用批次 view／summary。所有續讀與工具往返納入計時，超出預期的未量測續讀直接拒絕該次結果。

算術案例由獨立 fixture oracle 核對：base 對單價 10、數量 3 算出 30，head 改為加法後算出 13。報告須保留指定 P2 finding 與數值證據、完整覆蓋及空缺口；遺失 finding 或隱藏缺口的結果不接受。每次比較固定 ref／tree／檔案清單、實際返回頁面、finding 正文與審查 outcome。

每次的 trace、發布前 context、成品、signature 及 metrics 都保留。summary.json 列出往返次數、stdout／stderr bytes、Git 計數、finish 嘗試、完整驗證數、整條 helper 流程耗時，以及逐組時間比值的中位數。任一不等價或失敗立即保留原始結果並停止，重跑須使用新目錄。

此重播是 Log 中逐欄位續讀模式的脫敏重現，包含觀測 wrapper 與工具啟動成本。它不重跑原始業務審查，也不是冷／暖快取矩陣、完整模型審查或模型品質驗收；`model_review_seconds` 維持 null。完整模型審查仍使用既有驗收矩陣與獨立語意稽核，不能用本重播的加速比例替代。

## 本輪狀態

2026-11-03，在 Windows／Python 3.14.7 完成以下驗證：

- 最終完整單元：377 項，374 通過、3 跳過。兩項因環境缺少 symbolic-link 權限，一項為 POSIX 檔案模式案例；跳過不視為該平台行為通過。完整紀錄為 `test-artifacts/log-optimizations/unit-final.log`。
- 自動 BDD：15 個情境、62 個步驟通過；真實 GitHub 發布的人工情境依標記跳過。
- 發布 CI 合約：28 項通過，這是本機合約測試，並非已執行 GitHub Actions。
- ZIP 建置、獨立解壓及 capabilities 驗證通過；publication_retry、batch_artifact_view、summary_status 三項能力均可用，未產生 bytecode。
- 18 個 helper 與本輪 3 個測試／重播檔通過 Python 3.10 語法解析；本機只有 Python 3.14，這不是 Python 3.10 執行驗證。

其他驗證結束後，對 baseline `87a6e050f1f7b70c2d3db9feff1f187cf3c906f5` 完成三組交錯配對。兩版每次的固定來源、返回頁面、finding 正文與 outcome／缺口一致，算術 oracle 均通過：

| 指標 | Baseline | Candidate |
|---|---:|---:|
| 工具往返，每組 | 24 | 7 |
| finish 嘗試，每組 | 1 | 1 |
| 完整驗證，每組 | 1 | 1 |
| Git 程序，每組 | 71 | 71 |
| stdout bytes，中位數 | 23,991 | 20,863 |
| helper 流程秒數，中位數 | 8.167 | 4.271 |

逐組 candidate／baseline 耗時比的中位數為 **0.52293**，只適用這個脫敏互動模式。完整原始資料位於 `test-artifacts/log-optimizations/replay-paired-final/`；summary.json 保留每組數據及固定內容摘要。該 candidate 全 skill SHA-256 為 `8c264b4bd9e3aed5251a0a97d9c883924157e39ebbd6e9ceb4e68a0cedcc75f0`，helper SHA-256 為 `0fdde9e2e9bc2ecf31871109c2c63dd945c71b73dad4b7678b20c57345eb40c3`；量測後再次核對與工作樹相同。

開發過程的沙箱暫存 ACL 失敗、測試修正與重播工具初次把 runtime 放入 Git Repo 的失敗均保留於同一 artifact 根目錄。首輪完整回歸執行期間仍有補強，僅保留作開發紀錄，最終驗收採 unit-final.log；與測試同時執行的單組冒煙也不作正式效能證據。上述正式三組使用新目錄獨立執行，沒有覆寫失敗資料。

上述配對與本機完整回歸使用升版前的 0.20.4 候選快照。0.20.5 發版只另更新版本標記及使用指南版本，helper 摘要維持相同；正式版本的完整平台驗證以同一最終 commit 的 GitHub Actions 證據為準。

模型品質、完整模型審查時間，以及原企業 runtime 的身分驗收仍待完成；本輪 helper 重播不代替這些門檻。本機未執行 Python 3.10，其結果由正式發布 CI 個別核對。
