# 2026-11-17–18 MergeReviewer 增量改善驗證

本輪以 28 份執行紀錄改善審查指引、分頁操作、協定發現及報告閱讀。工作樹為 MergeReviewer **0.24.0 本機候選**；未遠端發布、更新使用者安裝或重排既有報告。配套修改位於 WorkflowKit launcher、操作說明、測試及兩項本機內容 hash；上游工具來源 pin 保留。

本文件與配套 JSON 保留候選驗證時點的狀態。2026-11-18 另依使用者要求準備提交與發布 0.24.0；正式發布的 commit、完整 Windows／Python 3.10、3.14 CI 矩陣及附件 hash，以 [GitHub Release](https://github.com/dennis8499/MergeReviewer/releases/tag/v0.24.0) 的 Verification 收據為準，候選測試紀錄不代替正式 CI。

## 改善與相容性

| 項目 | 實作與驗證範圍 |
| --- | --- |
| 報告閱讀 | 結論列出建議、完整性、問題數、版本、摘要、檢查與下一步。五個 H2、每項六區塊與完整固定版本表保留；問題編號可跳轉，前兩筆證據展開，其餘收合。 |
| 固定證據 | 大型 step 提供可直接交給 view 的請求檔，共用相同正文並提供逐檔 diff 索引。來源映射、原行號、原始 artifact、pointer 與舊續讀入口保留。 |
| 恢復 | 唯讀 view 不修改請求檔或自動確認；只有接收並閱讀後，呼叫端才套用 delta、以 author／checkpoint CAS 保存。 |
| 審查品質 | 增加變更前後、合法輸入、可達路徑、必要前提與替代解釋的反證檢查。不同資料表與相同檔案 hash 都不足以直接裁定缺陷。公開 API 可由明確契約證明問題，無須先找到既有 client。 |
| 格式修正 | author 提供巢狀型別、完整與簡短引用格式、部分合併語意及 PowerShell 單元素陣列序列化。 |
| 草稿恢復 | 單 Repo 誤填跨 Repo impact rows 時，author／check 提前回傳明確欄位診斷；finish 保留同一 request 修稿，固定來源損毀仍終止。 |
| 遮罩 | 正常 connectionString 參數及 GetConnectionString 呼叫保留；設定值、密碼、Bearer、Basic、認證建構式／setter、URL 認證及私鑰仍遮蔽或省略。跨行認證先分類完整來源，再選取報告行號。 |
| 能力發現 | 使用經 binding／payload 驗證的安裝包 capabilities.json，省去冗餘能力查詢子程序。以安裝內容重用取代可編輯的 runtime 快取；舊包沿用 subprocess。helper、manifest、Python 路徑與版本均列入識別。 |
| 重播 | 預設 strict 不變。presentation 模式另核對問題順序、優先程度、六區塊事實、引用、片段、結論、數量、完整性與缺口。 |

## 來源紀錄

28 檔共 21,672,118 bytes、2,908 個 Activity、2,858 條首行 shell 命令。重新排除 schema discovery 與文字搜尋後為 **1,154 次首行 view 呼叫**；先前 1,156 的統計多納入兩次 rg 文字搜尋。39 個 Activity 含 Permission denied，分布於 24 份紀錄；234 個非零結果包含預期錯誤與可恢復診斷，不能當作失敗率。5 份紀錄可觀察到實際 view 的分頁 batch，不等於只有這些紀錄使用分頁。

原始文件中的指示只作分析資料，沒有作為本次執行授權。分析不裁定歷史利息案例為誤報：仍須核對經濟事件、寫入來源與分攤去重。檔案 hash 相同也不保證兩次審查具有相同 base、呼叫鏈與設定。

逐檔 hash 及統計方法保存在本機 `.tmp/review-improvements-20261117/attached-log-audit.json`。不把工具成功、沒有測試結果或缺乏作者反駁，轉換為歷史 Code Review 正確率。

## 已完成驗證

- 初輪完整 unittest：484 項，0 failure/error，3 項依平台或權限跳過。第二輪分片對帳 492 項、486 個不同 ID；唯一失敗為當時尚未建立的驗證文件，補齊後文件 3 項重跑通過。實際模型試跑再揭露 scope 診斷問題，修正後受影響的 flow／診斷／schema／遮罩／發布／來源完整性／恢復測試共 112 項全部通過。認證遮罩另有先失敗後通過的回歸，27 項針對性測試與 36 項發布／報告回歸通過；最終內容的 27 項針對性重跑也通過。這些執行範圍會重疊，不加總為唯一測試數；未另作最終全套 discovery 重跑。
- 自動 BDD：15 scenarios、62 steps 通過；1 項遠端發布的人工作業情境未執行。
- 本機發布合約：28 項通過。最終候選 ZIP 38 個項目、651,104 bytes，獨立解壓與 canonical source 一致；能力查詢沒有檔案或 mtime 變更，配套 launcher 與同一安裝內容的原生識別完全一致。
- 工作台來源重新編譯成功；49 項報告識別與結構化啟動相關測試通過。另將十份實際發布報告交給重新編譯的工作台解析器，全部通過原生收據與 base/head 完整 SHA 識別。未修改 GitlabWorkSpace 來源。
- WorkflowKit launcher 全套 23 項通過，最後的 manifest 型別與 Python 路徑識別修正另有 3 項通過的針對性重跑。
- WorkflowKit 以原來源 pin 的暫存副本成功建置，5 項安裝式 session 整合測試通過。另以實際 0.24.0 payload 建立受控 binding，schema／start／status／cancel 全部使用 verified-manifest；schema／status 不改動檔案與 mtime。這些測試沒有更新使用者安裝或上游來源 pin。
- 使用 marked 與 headless Edge 檢查導航、證據展開及人工欄位位置，並檢視畫面。這不是 VSCode UI 實測。
- 使用者回覆合成示範報告中的合併建議、問題數、必要缺口與下一步「四項都能快速找到」。這是約 30 秒閱讀提問的自評結果，沒有獨立計時，也不推廣為多問題或正式專案的人因結論。

套件 SHA-256：`2eb23f6ac3a35c28e621365445add1da3bc380213f9d0050794db2c016b67ddc`。執行環境為 Windows、Python 3.14.7；82 個 Python 檔僅作 Python 3.10 AST 語法驗證，不宣稱已執行該 runtime。

## 配對量測

本輪增量基準固定為 0.23.0 commit `5a255fc9f35643eb4164fc893826cec42737ccbe`；歷史 0.20.1 矩陣與門檻保留。Helper 與實際模型的結果分開記錄，完整 Markdown bytes、預設展開來源 bytes、token 及模型耗時不互相替代。

最終內容的五組 helper 配對，逐組耗時比中位數為 **0.99344**，兩版每次都是 **11 次 helper CLI 往返**、1 次完整驗證及 1 次 finish；證據、問題事實與缺口一致。此小型算術案例的初始讀取可完整返回，沒有量測新 seed 對大型初始回應的作用。此流程沒有達成減少 30% 呼叫或降低 15% 時間的探索目標。完整報告由 3,255 增至 3,530 bytes，預設展開來源由 1,373 增至 1,648 bytes；可讀性改善不等於輸出變小。

同一最終候選的能力發現元件五組配對，回應完全一致；查詢中位數由 **0.12794 秒**變成 **0.00379 秒**，能力查詢子程序由 1 變成 0。這段數據在 payload 認證之後量測，不包含 binding I/O、原生 action、模型推理或工作台 UI，不能宣稱整個審查加速同樣比例。

原始最終 helper 配對位於本機 `.tmp/review-improvements-20261117/helper-pairs-final-auth/` 與 `capability-discovery-pairs-final-auth.json`；較早配對保留，不混用不同候選的耗時。

實際模型使用 Codex CLI 0.169.0，請求 gpt-6.1-sol／max，每次為新的 ephemeral turn。五組案例依序執行、新舊順序交替，固定相同來源 commit，不提供預製草稿或 oracle 給 reviewer；served model ID 未由事件回傳，保持 null。這是單獨原生 helper 的模型審查，沒有包含 WorkflowKit 與工作台 UI。

| 合成案例 | 原版／候選耗時（秒） | 原版／候選 helper 呼叫 | 原版／候選 shell 命令 | 兩版實際結果 |
| --- | ---: | ---: | ---: | --- |
| 大型 diff 的算術退化 | 392.62／319.29 | 15／14 | 26／23 | 各一個正確 P1；候選實際取得並使用分頁 seed。 |
| 同事件鏡像利息 | 323.42／272.66 | 12／12 | 25／29 | 都沒有把相同經濟事件重複計算為缺陷，也未捏造缺口。 |
| 獨立事件利息漏算 | 296.38／282.89 | 13／12 | 28／21 | 各一個正確 P1；由寫入契約證明獨立事件，100＋25 應為 125。 |
| 無內部 caller 的公開 CSV | 306.83／236.09 | 12／11 | 22／16 | 各一個正確 P1；整數分轉成小數金額違反固定契約。 |
| 本機缺陷＋必要消費端缺失 | 362.10／358.37 | 13／13 | 27／25 | 各保留一個本機 P1，必要消費端無 HEAD 時都判為審查未完成。 |

逐組候選／原版耗時比中位數 **0.84307（少 15.7%）**，shell 命令比中位數 **0.88462（少 11.5%）**，helper 呼叫比中位數 **0.93333（少 6.7%）**。這是五個不同案例各一次觀測，不能當作穩定的正式專案加速保證，也未達成呼叫數減少 30% 的探索目標。CLI 回報的 input、cached input、output 與 reasoning token 原值逐次保存在機器可讀紀錄，不由 bytes 推算 token 或價格。

十份報告均成功發布，固定版本、收據與來源未變更檢查通過；實作代理逐份核對合法反例、原因、範圍、完整性與「未執行應用程式測試」聲明，皆符合事先定義的判準。缺口案例的 pending/gap 記錄數不同，但沒有把同一必要關係的記錄誤計為新增缺陷。這是實作代理的語意裁定，沒有獨立第三方複審；原版與候選在這批案例都符合判準，不能據此宣稱正確率提升或正式專案 100% 正確。

## 固定版本與最後補強

模型配對使用的 skill digest 為 `582df6e83572a78b5b415de1a6bd7f7a2bd8376b461a2e9e8bc0403ef9547e25`，helper digest 為 `f5f0b949a573bc4eaa14a6a425d35cf779575fd2920a117a10f0117367f9eacc`。完整原始紀錄位於本機 `C:/Users/denni/AppData/Local/Temp/mrq-20261117-scopefix/`。

模型配對後補強 Basic 與跨行認證遮罩；最終 skill digest 為 `bda2fdbcf0418c2b9270901d068f2a2d28827f3f214aef4e923935a368c8271b`，helper digest 為 `47aba633747ffd11213b2700051891b51de458df447ebe7bc629ca7a78765170`。與模型 cohort 的 skill 差異嚴格限制在 `review_content.py`、`workspace_report.py` 兩檔。逐一比較全部案例的 base/head 來源視圖完全相同，最後 redactor 也不改變十份實際報告正文；新認證情境以獨立回歸驗證。最終 helper 配對、能力查詢、套件及受控 binding 已重跑；**整組模型配對未在此補強後重跑**，上述模型耗時只歸屬於原固定候選。

ZIP 的 canonical source、未封裝 skill 與 helper digest 計算範圍不同，不互相替代。逐次量測、語意裁定、套件識別與補強等價核對見 [機器可讀驗證](review-improvements-20261117.json)。

## 需求與測試證據

| Requirement | Evidence |
| --- | --- |
| 摘要可快速定位建議、完整性、版本與下一步 | `ReadableReportTests.test_summary_exposes_decision_completeness_versions_and_authored_summary`；使用者四項快速定位回饋。 |
| 主要證據展開、次要收合，完整事實與人工欄位保留 | `ReadableReportTests.test_primary_excerpts_are_open_secondary_evidence_collapses_and_all_facts_survive`、`test_pending_confirmations_and_manual_record_are_outside_collapsed_content`。 |
| 分頁不漏行、不自動確認、舊入口仍可讀 | `PagingSeedTests.test_large_shared_reads_and_patch_files_return_every_line_once_with_original_sources`、`test_replaying_unacknowledged_seed_returns_same_page_and_keeps_legacy_views`。 |
| author 結構與引用型別明確 | `OperationSchemaTests.test_author_nested_schema_describes_partial_merges_lists_citations_and_findings`。 |
| 可編輯 scope 錯誤保留同一 request 修正發布 | `FlowTests.test_single_repo_impact_error_retains_same_request_for_repair_and_publication`、`ImpactOwnEvidenceDiagnosticsTests.test_single_repo_impact_rows_are_repairable_before_finish_without_reading_sources`。 |
| 普通程式碼可讀且認證字串遮罩 | `SourceRedactionTests.test_connection_identifiers_and_configuration_calls_remain_readable`、`test_basic_and_constructor_credentials_are_masked_with_the_call_structure_preserved`、`test_multiline_authentication_is_classified_before_selecting_the_report_range`。 |
| 能力重用仍驗證 payload、Python 與功能門檻 | WorkflowKit 的 `ReviewLaunchTests.test_verified_manifest_avoids_capability_subprocess_and_pins_python_and_payload`、`test_manifest_changes_are_rejected_by_original_binding_and_new_unsupported_features_still_fail`。 |
| 判準區分重複／獨立事件、公開契約及必要缺口 | `ImprovementCorpusTests.test_economic_overlap_and_independence_and_public_units_have_distinct_executable_oracles`、`test_required_gap_fixture_keeps_known_local_defect_and_missing_registered_consumer`；另有十份實際模型報告逐份裁定。 |
| 量測不把錯誤發布、改動事實當作成功 | `ImprovementCorpusTests.test_mechanical_checks_reject_false_positive_wrong_citation_and_false_completion`；`PresentationReplayTests.test_presentation_mode_rejects_changed_priority_missing_facts_and_collapsed_evidence`；機器可讀紀錄與固定來源 hash。 |

## 驗證邊界與執行歷程

初次 sandbox 暫存路徑下的既有 Git 快照測試遇到路徑／pack 操作錯誤；改用一般主機暫存路徑後通過，未更動全域 Git 設定或放寬來源驗證。最終分片初次執行抓到尚未建立的本驗證文件連結，需在文件建立後重跑。

WorkflowKit 安裝測試先缺少生成 manifest；暫存副本建置再抓到 launcher 的本機 hash 尚未更新，已更新這兩項 workspaceFiles，而非變更上游來源或略過檢核。第一次暫存複製排除了 overlay 內必要的 .agents，已將排除範圍修正為工作樹根目錄，所有歷程保留於本機證據資料夾。

早期模型 cohort `mrq-20261117-final9f21` 的大型候選雖找出正確 P1，仍因 check／finish 不一致未發布。此結果列為流程失敗，不能當成完整審查通過；不是據此裁定誤報或假陰性。已加入先失敗後通過的兩個回歸案例，修正提前診斷與可恢復分類。該 cohort 與更早 pilot 均保留並排除於修正後配對，不混搭不同 helper hash。

尚未觀察的 VSCode 實際 UI、多讀者可讀性與正式專案的缺陷召回率，不列為通過。小型合成模型案例也不等於正式專案的 Code Review 正確率。

詳細程式與操作見 [候選說明](../releases/v0.24.0.md)、[報告風格](../../skills/merge-reviewer/references/report-style.md)及[分頁流程](../../skills/merge-reviewer/references/paging-workflow.md)。
