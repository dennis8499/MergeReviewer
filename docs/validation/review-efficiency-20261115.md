# 2026-11-15 審查互動效率驗證

本輪已完成分頁、checkpoint、操作規格、技能指引及本機封裝的實作與驗證。固定來源的五組 helper 配對顯示工具往返及 helper 耗時下降，但總輸出 bytes 增加；這些結果不能代表模型審查加速。18 次模型品質觀測已通過獨立 Agent 稽核及收據核對；WorkflowKit 完整測試範圍亦已核對通過，保留平台跳過及中途失敗歷程。產物為本機候選，沒有遠端發布。

本報告的機器摘要為 [review-efficiency-20261115.json](review-efficiency-20261115.json)。原始證據位置均以整個工作區為起點；下列 `.tmp/...` 路徑是未入版的執行證據，不是此 Repo 內的相對連結。

## 範圍與固定產物

- MergeReviewer **0.23.0**、WorkflowKit **1.11.0**、GitlabWorkSpace **0.51.0**。保留原有使用者說明與報告風格變更，沒有以本次工作覆蓋它們；其他工具來源 pin 與歷史 fixtures 保留。
- 執行環境為 Windows、Python **3.14.7**。22 個 native helper 與 2 個 WorkflowKit Python 檔通過 Python 3.10 AST 語法解析，共 24 檔；這不是 Python 3.10 執行驗收，也不是其他作業系統驗收。
- 主體為[分頁協定](../../skills/merge-reviewer/scripts/review_paging.py)、[checkpoint 保存](../../skills/merge-reviewer/scripts/review_checkpoint.py)、[原子 author](../../skills/merge-reviewer/scripts/review_author_operation.py)與[操作規格](../../skills/merge-reviewer/scripts/review_operation_schema.py)。技能的[續讀範例](../../skills/merge-reviewer/references/paging-workflow.md)由獨立程序執行測試核對。

| 本機候選產物 | SHA-256 |
| --- | --- |
| `MergeReviewer/dist/merge-reviewer-0.23.0.zip` 與 WorkflowKit 同名凍結來源 ZIP；36 個項目及完整 bytes 相同 | `be4b41298af51296b34705a7639282d3590a19bae078e926301170f0e3e218f5` |
| `WorkflowKit/dist/gitlab-workspace-kit-1.11.0.zip`；33,679,965 bytes | `14657002f6f7ab688e5c80333d195f30572703bf59afd96649421d54535570dc` |
| Group／Repository 新安裝的 Reviewer payload | `6221e167abd3358a2fad959cd46d97bfff38b91a1aff442e7b57f32a12a4a1ba` |

來源與產物對照為 `.tmp/review-efficiency-20261115/source-asset-parity-post-bootstrap.json`；建置與發布格式核對為 `workflowkit-build-post-bootstrap.log`、`workflowkit-verify-release-post-bootstrap.log`。ZIP、安裝 payload、重播 skill digest 使用不同範圍及算法，不可互換。Python 語法清單、每檔 hash 與限制記錄於 `python310-syntax-final.json`。

## 功能回歸與相容性

MergeReviewer 以明確 Repo 測試路徑分片，重新對帳完整 discovery：**預期 471、實際 471、468 通過、3 項依平台／環境條件跳過、0 failure／error**。跳過不視為相應行為通過。完整 UTF-8 證據為 `.tmp/review-efficiency-20261115/merge-shards-verified/summary.json` 及同目錄三份 `shard-*.log`。

| 驗證層 | 結果與證據 |
| --- | --- |
| WorkflowKit 綁定 launcher | 20 項通過；`.tmp/review-efficiency-20261115/workflowkit-launch-current.log` |
| WorkflowKit 身份／oracle／模型設定防呆 | 17 項通過：benchmark 14、prompt 3；`workflowkit-reliability-current.log` |
| WorkflowKit source freeze | 10 項通過；`workflowkit-sources-tests-final.log` |
| WorkflowKit release CI 本機合約 | 最新 25／25 通過；`workflowkit-matrix-tail/release-ci.log`；不是遠端 GitHub Actions 執行結果 |
| GitlabWorkSpace 審查相容性選定測試 | 49 項通過，0 skipped；`gws-review-compat-elevated.log`；不是 Extension 的完整測試套件 |
| 新 Group／Repository 安裝 | 各 15 項檢查通過，使用全新 request／binding，兩者 `reviewComplete=true`；`installed-group-post-bootstrap.json`、`installed-repository-post-bootstrap.json` |
| 原先出錯的 BMAD 情境 | `test_bmad_output_alone_does_not_suppress_the_bundled_component` 單項重跑通過；`workflowkit-bmad-hosttemp-final.log` |
| WorkflowKit 完整測試範圍 | 197／197 個唯一 ID 對帳：195 通過、2 項 Windows 符號連結權限跳過、0 最終失敗；`workflowkit-matrix-tail/summary.json` |
| WorkflowKit 初次直接 `scripts/test.py` | 初次執行中斷，`workflowkit-full-initial.log` 保留；最後採相同四階段的分片／分組等價執行，並非單次未中斷腳本 |

最後完整回歸以固定 197 個唯一測試 ID 對帳，結果為 **195 通過、2 跳過、0 最終失敗**。同一測試在歷史矩陣分組及瀏覽器重測中可有多次實體呼叫；本輪 ledger 共 **209 次已完成實體呼叫**，包含兩次保留的瀏覽器失敗。另一次中斷的矩陣沒有取得任何情境信用；更早的中斷 sequential 與窄範圍測試不納入 209 的定義。各選定窄範圍 suite 與完整清單有重疊，不可相加當作不同測試總數。

38 組歷史升級情境使用既有 VERSION 篩選、11 次獨立呼叫逐個成功 subTest 對帳，完整列集 SHA-256 為 `c0bc7b22c42a631193fc9a37f26233c96d082808c27fa41c39dcd54dfbec600f`，在原 197 清單中只計為一個矩陣測試 ID。兩個跳過是 `test_linked_resources_are_rejected` 及 `test_rejects_symlink_runtime_before_launch`：前者明列 WinError 1314，後者記錄 Windows 帳戶不能建立 symlink，不能視為該行為通過。

最後執行涵蓋 `scripts/test.py` 相同四階段：build、verify、完整 unittest 清單、release CI；unittest 採隔離模組分片與保留原斷言的矩陣分組／瀏覽器重測，**不是單次未中斷的原腳本**。原分片 orchestrator 的 exit 1 及失敗 summary 保留，最終對帳另存 `workflowkit-matrix-tail/summary.json`，SHA-256 `b4456e29071b5876c0b9bf09518433eddf91e284d87f3c2860c7d24553e52c15`；`workflowkit-full-reconciled-final.log` 的退出碼為 0。

瀏覽器最初 Group 檢查失敗而詳細 receipt 被清理，具體原因未知。保存收據的第二次執行 Group 通過、Repository 報 `viewer/temp-cleanup-incomplete`，觀察到 Chrome 在 SIGTERM 與 SIGKILL 各 1.5 秒內均未回報退出；此 finally 清理錯誤可能遮蔽較早 inspection 錯誤，不能直接推定版面結果或初次失敗原因。全部背景測試結束後，最後單獨執行同一個測試，兩個 profile 的原始 publish／verify 判準均通過；沒有修改 vendor、測試斷言或時限。三次結果分別保存於原 `shard-1.log`、`workflowkit-browser-diagnostic/` 與 `workflowkit-browser-isolated/`。這只證明最後隔離執行通過，不代表兩次失敗原因都已根治。

最後核對八個 asset hash 全部不變、五份凍結上游來源與 28 個 workspace payload 通過、當前 36 個 Merge skill entries 與 native ZIP 一致，21 個測試模組與原始 discovery hash 相同。

表內未重複前綴的 log／JSON 均位於 `.tmp/review-efficiency-20261115/`。當前 launcher 與 reliability 證據使用重新保留的 UTF-8 native log，未採用較早或其他日期的 PowerShell transcript。

| Requirement | Evidence |
| --- | --- |
| 同批去重、已確認頁面不重讀；輸出遺失不提前確認 | [分頁測試](../../tests/test_paging_protocol.py)：`test_duplicate_pages_share_load_and_acknowledged_pages_do_not_read_again`、`test_lost_output_repeats_same_batch_until_caller_applies_delta` |
| 巢狀內容與長字串完整續讀 | `test_nested_and_long_string_continuations_recover_all_required_content`、`test_large_dictionary_field_pages_remain_reachable_after_each_acknowledgement` |
| 24 KiB 含控制資訊，保留未返回尾段及過大頁面 | `test_byte_budget_preserves_unreturned_tail_and_does_not_print_large_history`、`test_final_cache_count_digit_growth_preserves_wire_budget_and_pending_prefix`、`test_oversized_single_page_remains_pending_without_acknowledgement` |
| 錯誤只阻擋一次，不跟隨來源內容中的假控制資訊 | `test_bad_inputs_are_blocked_once_and_do_not_hide_valid_rows`、`test_source_content_named_continue_is_not_followed`、`test_nonadvancing_continuation_and_changed_scope_block_instead_of_looping` |
| 身份、順序及每個已處理頁面均有確認／阻擋對帳 | `test_identity_and_stale_or_reordered_deltas_are_rejected`、`test_delta_cannot_silently_drop_processed_pages_without_acknowledgement`、`test_delta_cannot_acknowledge_an_unprocessed_page_or_inject_foreign_pending` |
| checkpoint-only CAS、舊 journal／快取 author 不回退新狀態 | [checkpoint 測試](../../tests/test_checkpoint_paging.py)：`test_checkpoint_only_commit_advances_revision_without_changing_draft`、`test_old_successful_journal_cannot_roll_back_newer_checkpoint`、`test_cached_author_replay_preserves_latest_checkpoint_and_paging_artifact` |
| 中斷後恢復、完整狀態引用與舊 checkpoint 相容 | `test_checkpoint_commit_recovers_after_draft_or_checkpoint_interruption`、`test_large_paging_state_recovers_from_summary_reference_without_writes`、`test_legacy_author_preserves_paging_and_new_paging_requires_revision` |
| schema／help 不需 runtime，規格與預檢同源 | [規格測試](../../tests/test_operation_schema.py)：`test_schema_and_help_work_without_runtime_or_request`、`test_schema_argument_sets_match_preflight_and_author_fields` |
| 所有權建立競態可恢復，foreign root 仍拒絕 | [啟動測試](../../tests/test_runtime_bootstrap_preflight.py)：`test_launch_preflight_uses_race_safe_check_without_writes_or_git`、`test_forged_lock_never_accepts_foreign_directory_and_wait_is_bounded`、`test_foreign_without_lock_and_invalid_marker_remain_rejected` |
| 文件兩段 Python 可在不同程序執行 | [範例測試](../../tests/test_paging_examples.py)：`test_documented_creation_and_confirmation_work_in_independent_processes` |
| wrapper 身份不可覆寫，schema 純讀，計時不污染 stdout | `ReviewLaunchTests.test_bound_schema_matches_native_without_runtime_or_writes`、`test_schema_rejects_identity_override_and_missing_capability`、`test_action_timings_are_structured_stderr_without_protocol_contamination`；launcher current log |
| 原生驗證活動計時累加正確，節流與項目計數不變 | [活動計時測試](../../tests/test_validation_activity_metrics.py)：`test_prefetch_transitions_close_elapsed_activities_without_validating_items`、`test_revisited_activity_accumulates_once_and_final_counts_match_actual_advances`、`test_no_observer_preserves_no_clock_work_and_context_is_reset` |
| 新身份 manifest 及模型 effort 在啟動前驗證 | `EngineFreezeTests.test_explicit_identity_accepts_new_versions_and_pins_source_payloads`、`test_explicit_identity_requires_complete_valid_component_pins`、`ReviewerReliabilityPromptTests.test_trial_rejects_inconsistent_effort_before_any_model_process`；reliability current log |
| 重播實際使用原子 author／checkpoint，必要證據等價可獨立核對 | [重播測試](../../tests/test_log_workflow_replay.py)：`test_paging_replay_uses_atomic_author_and_recovers_equivalent_evidence`、`test_evidence_comparison_ignores_duplicate_calls_but_detects_changed_content`；五組 helper 配對均通過 oracle |
| 封裝只更新所選 pin；新安裝完整驗證及發布不縮水 | `SourcePackageTests.test_scoped_freeze_preserves_unselected_source_without_reading_its_live_checkout`；來源 parity、兩份 post-bootstrap 安裝 JSON；helper 配對的 `git_commands=79`、`finish_attempts=1`、`full_validations=1` |

## 五組隔離 helper 配對

採固定來源與五組交錯 baseline／candidate 配對；本輪 worker 實際經過原生 continuation、新分頁格式、確認差異、兩次原子 author 與一次 checkpoint 恢復。五組全部通過獨立 fixture oracle，必要證據、finding 與 gap／outcome 等價。

| 指標 | Baseline | Candidate |
| --- | ---: | ---: |
| 工具往返，每組 | 124 | 11 |
| helper 流程秒數，中位數 | 40.975997 | 7.624708 |
| 避免重複頁面請求，每組 | 0 | 100 |
| Git 程序，每組 | 79 | 79 |
| finish 嘗試／完整驗證，每組 | 1／1 | 1／1 |
| stdout bytes，中位數 | 43,191 | 52,133 |
| stderr bytes，中位數 | 5,947 | 8,189 |

逐組 candidate／baseline 耗時比的中位數為 **0.188679**，僅適用這個脫敏 helper 互動模式。這是配對比值中位數，不是兩個耗時中位數相除，也不是整段模型審查的速度比例。

**輸出成本沒有全面下降。** stdout 約增加 **20.7%**、stderr 約增加 **37.7%**；分頁確認、checkpoint 與診斷回應有額外成本。本輪能確認避免 100 筆重複請求及減少往返，不能宣稱降低了總輸出量。總 bytes 壓縮仍是後續可量測的改善項。

原始配對、trace、固定身份及 oracle 結果為 `.tmp/review-efficiency-20261115/helper-pairs-final/summary.json` 與同目錄產物。`model_review_seconds=null`；保留原始數據，不將未量測值寫成零。

## 失敗、修正與證據界線

1. 初期受限 sandbox 的路徑及 MSYS `NtCreateDirectoryObject`／`BaseNamedObjects` 存取限制使 local Git fixture 失敗；OneDrive 暫存路徑另發生 `WinError 32` 清理鎖定。後續改用每程序正常使用者暫存目錄及正常 Windows token，沒有改全域 Git 或 Windows 設定。初始失敗與中斷 log 保留，不以重跑成功覆蓋。
2. 完整回歸暴露 readonly preflight 的既有所有權建立競態，baseline 0.22.0 也可重現。修正為重新檢查及最多 0.5 秒純讀等待，仍由原嚴格所有權驗證器確認 marker；foreign root、錯 marker 及假 lock 不因等待而獲准。相關證據為 `runtime-race-baseline.log`、`runtime-race-current.log`、`runtime-race-current-fixed.log`、`runtime-bootstrap-public-fixed-unit.log`。
3. 初期 scratch 分片器載入了錯誤的 `tests` 來源；該次不列為完整通過。修正明確路徑及 discovery 對帳後，最終 expected／observed 均為 471，以 `merge-shards-verified/summary.json` 為準。
4. 開發過程另修正 checkpoint-only 重送回退／重寫、分頁差異漏對帳、最終 byte budget、獨立 Python 範例 import 與 checkpoint revision 說明；對應測試見上表。這些修正已納入最終候選。
5. WorkflowKit 初次完整測試在出現 ERROR 後中斷，未取得該次完整 traceback；抽出的 BMAD 情境重跑通過，不據此宣稱原完整套件已綠燈。
6. 完整分組回歸的 Group Archify 瀏覽器失敗與第二次 Repository cleanup 失敗均保留；最後無背景安裝負載的同項原測試通過兩個 profile。初次原因未知，第二次只確定觀察到退出確認逾時，沒有改寫為已確認的環境或版面原因；詳細證據與判定界線見上文。

## 模型品質驗收

三個情境各三組 baseline／candidate 配對，**18／18 次**觀測均通過獨立語意稽核及最終收據核對。六次日期邊界觀測均找到唯一應有的 P2 問題；六次安全變更均無誤報；六次必要消費端缺失均保持 `review_complete=false`，沒有捏造具體消費端故障。所有觀測的誤報與必要可取得證據漏讀均為 **0**；不可取得的必要證據以缺口保留，沒有視為已讀。

| 情境 | 配對數 | 六次觀測的預期結果均符合 | 工具次數中位數 baseline → candidate | 診斷秒數中位數 baseline → candidate |
| --- | ---: | --- | ---: | ---: |
| `date-boundary-files-7` | 3 | 完整；找到日期邊界問題 | 26 → 28 | 390.4 → 421.3 |
| `safe-existing` | 3 | 完整；無具體問題 | 30 → 31 | 293.5 → 313.6 |
| `required-consumer-gap` | 3 | 未完成；必要消費端缺口保留 | 29 → 24 | 344.4 → 312.3 |

**模型端工具次數沒有全面下降，也未證明整體審查加速。** 上表工具次數是模型工具事件，與 helper 重播的原生往返計數不同。原始秒數、個別工具次數與變異保留於機器摘要及收據；最初 pilot 與 unit shards 重疊，後續最多並行兩個獨立情境，較晚的觀測亦與 WorkflowKit 回歸重疊。所有模型耗時均為 quality-only、`diagnostic/nonisolated`，沒有速度閘門。每個情境內按 baseline／candidate、candidate／baseline、baseline／candidate 順序執行。

CLI 明確請求固定 `gpt-6.1-sol`／effort `max`，產品 prompt 使用相同 effort，啟動前防呆通過；provider 實際服務模型名稱沒有另行獨立觀測。既有使用者設定的 `ultra` 沒有變更；兩個固定產品版本的表單不接受 ultra，因此比較採最高共同支援值 max。

模型 baseline 是 committed MergeReviewer 0.22.0、WorkflowKit 1.10.0、GitlabWorkSpace 0.51.0；candidate 是本輪固定工作樹 payload。這與保留初始使用者文件變更的 helper baseline 不同，兩者不能混為同一來源。首個 baseline 觀測沿用已完成的原始收據，經不可變重用 manifest 驗證，沒有改寫其路徑、執行 ID 或內容；其餘 17 次為本排程的新執行。

稽核者是獨立於每次原生模型執行的 Agent，同時參與本輪實作／測試工具；這不是盲測或人工審查。稽核逐份驗證來源／報告／trace／原生發布收據 hash、所有預期原始 refs 與 tracked source 狀態，以及記錄到的 prompt／trace 中沒有 oracle 洩漏。嚴格收尾核對要求完整 3×2×3 矩陣、唯一 execution IDs、完整必要證據清單，並拒絕仍有 gaps／remaining／待確認數量的「完整」結果。

協定去重不代表 Agent 全程零重送、零格式修正。baseline 與 candidate 都出現過被拒絕後修正的 author／契約操作。candidate 日期案例一次重新交付 24 個已快取頁面，沒有額外 Git；trace 未提供模型可見的外層輸出預算，重送必要性未知，可能是外層回應恢復，不定性為無意義重讀。原 runner 的 `authoring_retries` 只計 check／finish 的 exit 3，不能代表所有 author 操作零錯誤；缺口案例的 check 不完整結果本來就是預期。

獨立逐 trace 計數得到 baseline 9 次被拒絕的 author 命令及 1 次契約搜尋詞拒絕，candidate 為 5 次及 0 次，均在發布前修復。這是明確 native structured error 的命令數，排除預期不完整 check、discovery 與另行重現的退出碼。六次安全變更觀測都至少一次碰到同一個跨 Repo 邊界引用規則，顯示提示／規格發現仍有改善空間；沒有為了降低重試而放寬驗證。分類及 trace 行位於 `.tmp/review-efficiency-20261115/adjudications/intermediate-rejections.json`。

這些小型模型 fixtures 的必要內容已由共享 projection 返回，**沒有實際使用 `paging_state` view**；不能宣稱已證明模型採用新分頁協定。該機制的證據來自獨立 unit tests、五組真實原生 helper 互動重播及兩種全新安裝測試。18 次觀測只支持所選情境的品質，不能推廣為所有審查任務的保證。

原始 aggregate 為 `.tmp/rm17/runs/all-runs.json`；獨立側錄及最終索引為 `.tmp/review-efficiency-20261115/adjudications/currentadjudication-index.json`。原始 `execution-status.json` 保留執行器的 awaiting-independent-adjudication 狀態，不覆寫 raw receipts；最終接受另存 `.tmp/rm17/quality-acceptance.json`，SHA-256 `eaca5eafd85ce1dce3f1597b856a91ede2d50631b485ae188d357f5d4339f947`。固定身份與原排程位於 `.tmp/rm17/identity-manifest.json`、`.tmp/rm17/schedule.json`。

完整 WorkflowKit 測試範圍現已對帳通過，兩項權限跳過與兩次瀏覽器失敗後的隔離成功均保留。Python 3.10 仍只有語法檢查，沒有實跑；模型耗時界線與所選品質情境的限制也不因完整回歸而取消。
