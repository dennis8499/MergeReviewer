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

## 2026-11-04 後續改善

這一輪只納入新提供紀錄中的 7 份 MergeReviewer，排除 6 份 ReleaseReviewer。Activity 首行可辨識 992 次 view、45 次 start、32 次 finish；這是命令呼叫數，不是失敗率。11 次 capabilities 回應均未包含目前 0.20.5 的版本識別、發布重試、批次續讀及摘要狀態，無法由紀錄確認現場確切版本。

0.20.6 修正以下仍存在的問題，保留 v3／compact 及既有 CLI：

- Repo 掃描剪除巢狀 `.gitlab-workspace/runtime`、預設網路 runtime 及設定指定的 runtime 子目錄；保留 kit 其他子目錄及明確宣告的根。真正受審範圍的讀取錯誤仍失敗。
- 存續檔案的已刪除／替換符號可註冊左側契約，須綁定實際 diff_base，或 merge-preview 的 head，且符號位於實際移除行。最終內容覆蓋不放寬。
- check 與 finish 共用契約自身位置及 coverage 影響圖起點規則，提前拒絕契約 ID 掛在錯誤版本／路徑的節點；check 保持零 Git。
- capabilities 新增 `validation_progress`；status 保存 Repo、契約、搜尋完成數、預讀活動與最後驗證時間。預讀不是驗證完成；修復完整性重驗與正式驗證分開計數，單一活動最多每秒更新，發布重試保留最後計數。

新增回歸位於 `tests/test_log_workflow_followup.py`。首輪 58 項定向測試有 1 項 error、1 項平台跳過：發布前的完整性重驗尚未提供細項進度，測試讀不到 activity。已將同一觀測器接入該路徑；首次結果保留在本次操作紀錄，後續結果另存新檔，不覆蓋失敗。

品質矩陣新增 `deleted-api` 與 `deleted-api-safe`：前者實際重現刪除舊函式後固定 consumer 的 AttributeError，後者保留相同刪除但 consumer 已使用新函式，結果不變。fixture 原始程式的執行核對與模型語意評分分開；oracle 仍只對父層稽核可見。三模型、三種技能條件與五組完整審查配對沿用原方法，新排程共 345 項；安全反例依既有規則各重複三次。

本輪驗證產物使用 `test-artifacts/log-followup/` 下的新檔及新目錄。正式模型審查須由外部執行者留下供應端模型／推理識別、helper trace、完成收據及獨立 audit；未取得這些資料前，模型品質與完整模型時間保持待驗收。企業安裝包的實際帳號、沙箱 token 與 ACL 同樣需現場驗收，本輪不改動該環境。

### 本輪本機驗證結果

Windows／Python 3.14.7 的最終完整單元執行 393 項，390 通過、3 跳過，無 failure／error；受測 skill 在完整執行前後摘要相同。兩項跳過因沒有 symbolic-link 權限，一項為 POSIX 檔案模式案例，不能視為該平台行為通過。完整紀錄為 `unit-20261104T131207Z-9451bc.log`。

- 第二輪定向測試 30 項通過：`target-20261104T130659Z-494942.log`。
- 自動 BDD：15 個情境、62 個步驟通過；1 個人工發布情境及其 4 個步驟跳過，見 `bdd-20261104T131205Z-52e925.log`。
- 本機發布合約 28 項通過；ZIP 獨立解壓後 capabilities 提供 validation_progress，helper 摘要與工作樹相同，未生成 bytecode。75 個 helper／測試檔通過 Python 3.10 語法解析，這不是 Python 3.10 執行或 GitHub Actions 驗證。
- ZIP 按既有 canonical 換行規則逐檔一致。第一次額外核對誤用原始全目錄 hash，因 agents/openai.yaml 的 CRLF／LF 正規化及原有 bytecode cache 而失敗；沒有修改發布規則，另以 canonical bytes 核對通過。首個產物保留於 `package-20261104T131522Z-bc7276/`，正式本機核對為 `package-20261104T131625Z-b1e6aa/summary.json`。

所有驗證結束後，對固定 baseline `58c173d22cf4b33199ea990e70007c344f3862eb`／0.20.5 獨立完成五組交錯 helper 重播：

| 指標 | Baseline | Candidate |
| --- | ---: | ---: |
| 工具往返，每組 | 7 | 7 |
| Git 程序，每組 | 71 | 71 |
| finish 嘗試／完整驗證，每組 | 1／1 | 1／1 |
| stdout bytes，中位數 | 20,828 | 21,055 |
| stderr bytes，每組 | 3,041 | 5,696 |

每組固定來源、返回頁面、finding 正文及 outcome／缺口一致，算術 oracle 全部通過。逐組候選／基準耗時比為 0.88082、1.13515、0.82857、1.02108、1.03970，中位數 **1.02108**，約增加 2.1%；原始波動及較慢配對均保留，不宣稱加速。stderr 增量包含驗證進度事件。此結果只適用該脫敏 helper 互動，不代表冷／暖快取矩陣、完整模型審查或語意品質已驗收。

重播原始資料為 `replay-20261104T134415Z-aa3887/`，包含兩版固定技能、每次 trace、報告及 summary。工作樹的來源 payload 與品質矩陣、重播候選逐檔相同；全目錄摘要算法及忽略 bytecode 的範圍以各工具的 identity 為準，不混用不同算法的 hash。統整數據與產物位置保存於 `verification-summary.json`。

已在 Repo 外準備全新 345 項品質／完整審查排程，並產出新增情境的 36 份盲測請求；`model-matrix-pointer.json` 記錄固定候選及位置。實際模型執行數為 0，pending-summary 的品質與完整時間閘門保持未通過。這些準備資料及 source oracle 的執行檢查不代替供應端模型結果、獨立語意 audit、Python 3.10 實跑或企業 ACL 現場驗收。

上述本機完整回歸、封裝及配對重播使用升版前的 0.20.5 候選；0.20.6 發版另更新版本標記與安裝指南，helper 摘要保持相同。正式 0.20.6 的 Python 3.10／3.14 執行及 ZIP 以同一最終 commit 的 GitHub Actions 證據為準；模型驗收須以正式技能內容重新固定候選，既有預備矩陣不冒充已驗收的正式版本。
