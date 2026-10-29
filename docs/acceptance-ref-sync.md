# 逐 ref 同步與審查流程：驗證紀錄

早期工作樹曾以 `v0.19.0` 的 commit `16706dc7b402a0c61de82cf5c24878075e1d1cff` 為機械比較基準；以 `git archive --format=zip v0.19.0` 保存的基準 ZIP SHA-256 為 `cec02ec624c41933f3f37814942729f22dd1be44fe69cbdb485ac701e52e13f3`。其發布內容準備 commit 為 `e23ebf5ba18c1eb7d8b7b62f1d41aa73bb72688f`。既有 0.19.0 完整審查紀錄為 4/6 有效配對、中位耗時比 0.984，見 [0.19.0 發布紀錄](releases/v0.19.0.md)。本輪有效基準與門檻列於本文件底部。

**本輪計畫驗收尚未全部通過。** 自動化測試或固定草稿重播不能證明模型語意品質，也不能代替完整審查時間。未知的純模型時間、工具外往返及完整審查時間均不以 helper 秒數估算。

| 驗收項 | 目標 | 目前狀態 |
| --- | --- | --- |
| 大型多 Repo 完整審查 | 內部變更及跨 Repo 變更各五組配對，中位耗時比 ≤ 0.90 | 尚未執行；無本輪完整審查時間數據 |
| 大型多 Repo helper 暖快取 | 五組交替配對的耗時比中位數 ≤ 0.80 | 本輪待測；先前 0.20.0 快照紀錄僅為歷史量測 |
| 大型多 Repo helper 冷啟動、陸續啟動 | 五組交替配對的耗時比中位數 ≤ 0.90 | 本輪待測；先前 0.20.0 快照紀錄僅為歷史量測 |
| 單 Repo 完整審查 | 各主要情境五組配對，中位耗時比 ≤ 1.05 | 尚未執行；無本輪完整審查時間數據 |
| 語意品質 | P0–P3、安全反例、消費端、AGENTS 範圍與敏感內容均通過獨立稽核 | 尚未完成指定模型對照 |
| 三模型技能可靠性 | `gpt-6.1-sol`、`gpt-6.1-terra`、`gpt-6-astra`，均 `high`；比較無技能／0.20.1／候選 | 尚未執行 |
| 平台矩陣 | Windows Python 3.10 與 3.14 | 本機為 Windows、Python 3.14；本輪 CI 尚未執行 |

## 已有檢查

- Windows 自動化 BDD：16 個情境、65 個步驟通過；GitHub 真實發布的人工情境未執行。
- Windows Python 最新完整回歸：封存版 `683ba08a0860eae713f1c3cb40754339452586c3985f66823b6f81ff03abbf03` 分三組執行 330 項，328 項通過、2 項跳過；兩項因本機沒有建立 symbolic link 的權限而跳過，不能視為實際連結測試通過。紀錄為 `final3-shard-1.log` 至 `final3-shard-3.log`。最新快照的 BDD 與 WorkflowKit 整合冒煙紀錄為 `final3-bdd.log`、`workflowkit-final3.log`。此封存版的 14 個 helper 與正式效能量測快照內容相同。
- 發布準備提交 `f89c2bd` 的 [Windows／Python 3.10 CI](https://github.com/dennis8499/MergeReviewer/actions/runs/40606655807/job/122547714135) 有一項計時測試失敗：極短測試在該平台讀到零秒。測試改用受控時鐘，仍檢查零秒不可被接受；原失敗保留，不視為通過。
- `v0.20.0` 標籤的 [Windows／Python 3.10 CI](https://github.com/dennis8499/MergeReviewer/actions/runs/40607667224/job/122551150818) 因同一測試失敗而未發布；保留原標籤及紀錄，以含修正的 `v0.20.1` 重新驗證。
- 較早封存版 `feb431894e0122fd563ccd9ce24dffdc66f8d857223f285130b9e41713ed8ecc` 執行 316 項，314 項通過、2 項跳過；紀錄為 `final2-shard-*.log`。其後新增驗收工具與案例，並依暖快取剖析調整固定快照的路徑檢查，故較早的封存版結果保留為歷史紀錄。
- 逐 ref 的 TTL、refresh、失敗冷卻、同分支別名、十份同時請求及取消測試通過；四個不同 head 的協調測試確認 main 只同步一次。新的 TTL 邊界及同步中失效測試使用受控時鐘與本機假 transport。
- v1 邊界相容、v2 內部／傳播／未解決、固定讀取行段、搜尋重播及精簡草稿定向測試通過。這些測試檢查結構與固定證據，不是模型判斷的品質評估。
- 技能 frontmatter 驗證及 ZIP 解壓獨立執行通過；WorkflowKit 的臨時安裝環境以候選技能覆蓋後，兩項 facade 整合冒煙測試通過。GitlabWorkSpace 的既有編譯輸出以隔離網路 runtime 執行 26 項相容性測試通過；該測試使用現有編譯輸出，不代表重新編譯後的完整套件驗證。14 個 helper 模組通過 Python 3.10 語法解析；這不是 Python 3.10 執行測試。
- 技能入口從 7,302 bytes 減至 6,203 bytes，減少 15.1%；這只衡量文字大小，不代表模型延遲或品質改善。

原始本機輸出及未修改的基準來源保留在 Git 忽略的 `test-artifacts/`。首次 GitlabWorkSpace 測試因既有沙箱外 runtime 的 `EPERM` 失敗，原始紀錄為 `gitlab-compat.log`；使用工作區內隔離 runtime 的重跑紀錄為 `gitlab-compat-isolated.log`。完整 Python 回歸的結果以其實際完成紀錄為準。

最初的工作樹基準在修改後仍持續執行，無法代表單一固定版本，已中止並保留為 `baseline-contaminated-interrupted.log`。第一個候選全套測試使用系統預設網路鎖路徑，已觀察到沙箱寫入錯誤；未隔離網路鎖的基準與候選測試均已停止，原始輸出保留。候選回歸改用封存的工作樹及各測試程序獨立的暫存網路 runtime，避免混合版本與共享鎖干擾。

封存候選的逐檔摘要為 `500a4c7f034fb54b61db961caf5d79f4695e8d133ad8070f427ad86ecec6a46d`。封存版三組共執行 310 項：一組 103 項通過、其中 2 項跳過，另外兩組分別有 1 項 error 與 2 項 failure。原因是共用測試草稿仍以舊 v1 邊界格式填入新 v2 context；已更新測試作者提供的明確 v2 判定，三項重跑通過。原始失敗及重跑保留於 `candidate-shard-*.log`、`published-regression.log`、`published-regression-fixed.log`。

封存後另修改 `review_authoring.py` 的衝突檢查、`review_flow.py` 的回傳狀態標記、`review_workspace.py` 的批次搜尋跨輪快取，以及 `workspace_report.py` 的 repo-local 搜尋範圍驗證，並補測這些修改；封存版全套結果不能當成最新工作樹的完整回歸結果。

後續封存版 `ab4a9659449cab300b7e69e85077e12e3dbedfdd7c3e10723e9c6ad890fc5250` 的測試發現四項舊測試仍假設單筆固定物件使用兩個 Git 程序。診斷後更新測試，改為核對單次 `cat-file --batch`、快取隔離與驗證行為；九項固定來源快取測試重跑通過。原分片測試因此中止，日誌保留於 `final-shard-*.log`；新的完整回歸結果見上方紀錄。

單 Repo 的一次試跑使用已檢視的 `ready = False → True` 固定夾具，冷／暖／陸續啟動的 candidate/base 比值分別為 **1.025、0.871、1.070**；最後一項超過 1.05。試跑只有一組且與其他測試並行，不能判定回退門檻，也不能代表完整審查時間；試跑後又加入草稿衝突檢查與輸出狀態標記，最新程式尚未重新量測。失敗與逐次數據保留於 `test-artifacts/single-benchmark-dry/summary.json` 及 `single-benchmark-dry.log`，不以後續重跑取代。

其後以目前 v2 單 Repo context 做了一組證據等價冒煙測試，固定來源、讀取、搜尋、驗證結果與 finding 內容相同；同時有其他測試執行，helper 時間為基準 9.513 秒、候選 10.474 秒（比值約 1.101），不是有效效能驗收。資料保留於 `test-artifacts/single-v2-equality/` 及 `single-v2-smoke.log`。

五組單 Repo 交替重播使用隔離 runtime，逐組證據及結果等價；cold／warm／staggered 的配對比值中位數依序為 **0.910、1.016、1.071**，staggered 超過 1.05。單次樣本分布不穩定，且此量測不清除作業系統檔案快取；個別資料、各階段時間與失敗門檻保留於 `test-artifacts/single-benchmark-paired/summary.json`。量測後再將單筆固定 blob 的讀取改為校驗過的 batch，因此不能把這組數據視為該補強之後的結果；亦不得以後續重跑抹除這次失敗。

上述 batch 補強後，使用同一草稿再做一輪完整五組交替量測，cold／warm／staggered 的配對比值中位數依序為 **0.998、0.934、0.879**，該輪三項均未超過 1.05；基準每次 77 個 Git 程序，候選為 76 個。第二輪資料保留於 `test-artifacts/single-benchmark-after-batch/summary.json`，必須與第一輪失敗一起解讀，且不能推論大型案例或完整模型審查已達標。

大型來源工作負載位於 `test-artifacts/large-fixture/`，含 12 個 Repo、12,000 個固定 head 檔案及四條契約鏈。已根據固定來源撰寫 [可重播的固定草稿](../tests/scenarios/large-reviewed-scenario.json)：20 個變更、48 次讀取、192 筆搜尋 ledger，以及一項 P1 發現。以固定來源在記憶體事件 bus 重現後，舊 producer 的四條鏈均得到 12300，新 producer 的四條鏈均出現 `KeyError: amount`；原始結果在 `large-review-reproduction.json`。也可對由 `tests/create_benchmark_fixture.py` 建立的 fixture 執行 `python tests/reproduce_large_fixture.py --fixture-root <fixture>`。這是合成夾具的實際重現，不代表真實服務或模型品質驗收。第一份草稿把合成夾具的適用範圍放入缺口欄位，導致結果未完成；修正為範圍說明後通過，兩次驗證紀錄分別保留在 `large-review-authoring.log` 與 `large-review-authoring2.log`。

大型重播的首次功能冒煙因搜尋操作錯用 `contract_id` 欄位而被舊版 helper 拒絕；未作效能判定。修正為公開 step 介面的 `contract` 後重跑。失敗的 scenario 與輸出保留於 `large-reviewed-scenario-invalid-search-argument.json`、`large-batching-smoke.log`。

修正後的大型功能冒煙，兩版的固定來源、讀取、搜尋、驗證結果及 finding 正文相同；基準／修改版各使用 626／363 個 Git 程序。因基準版與完整回歸並行，而修改版接近回歸尾聲，168.627／102.823 秒不作效能驗收；紀錄保留在 `large-batching-smoke2.log`。正式五組交替重播以隔離 runtime 執行，從暖快取開始，再測 cold 與 staggered，結果以實際完成紀錄為準。偵測到同工作區其他驗證期間會等待；若量測途中偵測到該驗證鎖，保留該筆資料但拒絕作為有效樣本。

第一輪正式暖快取五組配對比值為 **0.7609、0.7922、0.8175、0.7653、0.7966**，中位數 **0.7922**，未達 0.75。基準／修改版的 helper 總時間中位數為 36.090／28.443 秒，Git 程序分別為 571／308；各組固定來源、讀取、搜尋、驗證結果與 finding 正文相同。暖快取失敗後為分析原因而中止 cold／staggered，不能把該輪視為三模式完整驗收。逐次資料及這次失敗的摘要保留於 `large-benchmark-paired-3.log`、`large-benchmark-paired/warm-partial-summary.json`；不得以後續重跑抹除。

剖析後合併固定快照內部子路徑的重複解析，仍在每次存取檢查路徑連結及物件儲存。65 項快照、批次及搜尋定向測試通過；一次暖快取冒煙的修改版耗時 24.915 秒，固定證據及驗證結果與基準相同。這次冒煙不是配對驗收。

路徑優化後固定程式快照 `e4d660637b8a70c719d8b8185252a3713dd320048baf7aee7e3b5fc4a2d5f569`，對同一大型草稿完成每個模式五組交替重播：暖快取／cold／staggered 的配對比值中位數為 **0.6974／0.7650／0.7172**，分別符合 0.75／1.05／1.05；每次固定證據、驗證結果及 finding 正文一致。全部資料在 `large-benchmark-after-path/summary.json`。同一程式快照的單 Repo 暖快取／cold／staggered 五組中位數為 **0.9957／1.0319／1.0195**，均符合 1.05，資料在 `single-benchmark-after-path/summary.json`。這些均為 helper 量測，不代表完整模型審查時間或其他主要情境已通過；前述失敗輪次仍須一併保留。

## 可重播的 helper 測量

以下原始 helper 數據是舊 0.20.0 快照的歷史紀錄。本輪工具要求傳入已固定的 0.20.1 skill，並核對版本與完整內容 hash。

`tests/benchmark_flow.py` 按 cold／warm／staggered 模式交替執行，每個版本使用獨立 runtime，記錄準備、讀取／搜尋、驗證／發布、Git 程序數、CLI 次數、stdout／stderr、網路等待及各次原始結果。`--mechanical-v1` 在一次性 benchmark context 模擬舊 v1 政策，以同一已撰寫 v3 草稿比較機械變更；不代表新版 v2 語意判斷已通過。此選項僅供基準工具使用，不改寫實際審查 context。

這裡的 cold 指新的 reviewer runtime；工具不清除作業系統的檔案快取。staggered 是先前收據仍有效時的陸續獨立啟動，重疊執行另由並行測試涵蓋。

單 Repo 沒有多 Repo 邊界義務，可省略 `--mechanical-v1`，直接量測新版 v2 context；比較時只忽略單 Repo 的政策標記，不忽略固定來源、判斷或 finding。多 Repo 的 v2 語意比較須另作品質審查，不能以忽略政策差異宣告逐字等價。

提供已實際審查的固定 scenario 後執行：

~~~powershell
python -X utf8 -B tests/benchmark_flow.py --baseline "<驗收根目錄>/skills/baseline" --fixture-root "<固定 Git 工作區>" --scenario "<已審查 scenario.json>" --output-dir "<新的結果目錄>" --repeats 5
~~~

大型 fixture 增加 `--large-multi-repo`，工具確認至少 12 個 Repo、12,000 個固定 head 檔案，再對暖快取套用 0.80、冷啟動與陸續啟動套用 0.90；單 Repo 主要情境維持 1.05。比較使用各組 candidate／baseline 比值的中位數，而非兩個中位數相除。結果差異、失敗紀錄及全部原始數據應保留，不能挑最快重試。runner 要求必要讀取與命中在量測範圍內完整返回；需要未量測續讀時直接拒絕該次驗收。

若同一輪操作無法在上限內完整返回，可用 `--operations-per-step 5` 等固定批次大小重播；兩版使用相同的操作順序與批次，所有增加的 step 往返與輸出均計時。`--warm-first` 只改變三種模式的量測順序，不減少配對。這些選項不把落盤摘要當作已讀，若任何批次仍需未量測的續讀，仍拒絕驗收。

## 舊版 0.19.0 三模型比較矩陣（歷史）

以下排程、命令與結果記錄的是先前以 0.19.0 為基準的矩陣；本輪有效基準、門檻及命令見本文件底部的「2026-10-29 全面優化實作與驗收狀態」。

`tests/merge_acceptance_matrix.py` 是本輪專用的 parent-owned 驗收工具，不會呼叫模型或替審查者產生結論。它安排 117 個品質案例（13 類案例 × 三個模型 × 無技能／0.19.0／候選版），以及 `gpt-6.1-sol`、`high` 設定下的 18 次完整審查（大型內部變更、跨 Repo 變更、單 Repo 各三組雙版本配對）。新增案例包含一般 P2／P3、安全修復反例、內部值流入事件，以及兩個分支各自正常但合併後失敗的情境；父層 oracle 不放進審查工作區。

本機已在系統暫存目錄 `merge-reviewer-matrix-20261028-c` 建立 135 個排程及固定來源，並準備第一組 P0 的三種盲測請求；尚未啟動任何模型審查，`test-artifacts/model-matrix-c-pending.json` 如實顯示模型門檻未通過。矩陣 `c` 固定的是發版前的 skill 版本檔，不能當成 `0.20.1` 的逐位元組模型對照。首次建立的 `merge-reviewer-matrix-20261028-a` 因補入安全修復反例而主動中止；`b` 固定的是路徑檢查優化前的 skill。三者均保留。

以下準備步驟會建立兩個各含 18 個 Repo 的大型固定來源；驗收根目錄必須是全新路徑且位於任何 Git Repo 之外，以便 runtime 隔離。基準 skill 的內容 hash 須精確符合 `v0.19.0` 封存版，候選 skill 也會複製並固定 hash。保留準備失敗的原目錄，重試時選新路徑。

~~~powershell
python tests/merge_acceptance_matrix.py prepare --root "<全新驗收根目錄>" --baseline-skill "<封存 v0.19.0 的 skills/merge-reviewer>" --candidate-skill "<目前的 skills/merge-reviewer>"
python tests/merge_acceptance_matrix.py prepare-run --root "<驗收根目錄>" --id "<schedule 中的 run ID>"
python tests/merge_acceptance_matrix.py start --root "<驗收根目錄>" --id "<run ID>"
python tests/merge_acceptance_matrix.py complete --root "<驗收根目錄>" --id "<run ID>" --report "<該 run 的 reports 內檔案>" --actual-model "<供應端實際模型 ID>" --actual-reasoning "<供應端實際推理設定>"
python tests/merge_acceptance_matrix.py audit --root "<驗收根目錄>" --id "<run ID>" --audit-file "<父層獨立撰寫的 audit.json>"
python tests/merge_acceptance_matrix.py summarize --root "<驗收根目錄>" --output "<新的彙整 JSON 路徑>"
~~~

外部執行者須逐次以 `public-request.json` 的模型與設定啟動全新審查；無技能條件不得載入 MergeReviewer。`start` 應緊接實際審查開始前，`complete` 應在報告完成後記錄供應端實際識別資料。計時包含模型、helper 和往返；純模型時間維持未知。啟動、完成及 audit 收據均不可覆寫。

兩個 skill 條件的 helper 呼叫應使用盲測請求中固定 hash 的 `trace_wrapper`，依請求中的 `skill_dir`、`trace_file`、`audit_dir` 傳入相同 wrapper，再接原本的 `review_session` 與操作參數。完整審查配對要求未修改的 trace、start／step／finish 及至少一次完整驗證；工具會彙整各階段、Git 次數及 helper 輸出量。step 時間包含同批其他操作；網路等待或非 helper 往返無法確定時保留未知，不能從總時間減除 helper 時間冒充模型時間。`complete` 的報告需位於該 run 的 `reports/`，父層核對它與實際發布內容一致。

每個父層 audit 需對照 oracle、報告及實際執行紀錄，明確填寫 `semantic.passed`、非空 `semantic.observations`、該案例在 `DIMENSIONS` 指定的布林判定，以及 `mechanical_checks` 的 `fixed_sources`、`no_secret_sentinel`、`no_post_publication_edits`、`expected_review_outcome`、`model_reasoning_verified`、`correct_skill_condition`、`no_gitlab_api`、`no_unexpected_remote`。例如 P0 案例的語意判定需同時填 `required_findings` 與 `actionability`。工具另核對報告 hash、計時收據和敏感字串；它不能憑結構檢查推導語意正確性。各版本的失敗都保留，無技能或舊版的失敗可供比較，候選版則須通過全部指定品質案例。

彙整只對正確模型、設定、固定來源及語意／證據核對均通過的配對計時：兩類大型案例各須三組，中位配對耗時比須 ≤ 0.85，三種主要情境各自中位數須 ≤ 1.05。單一模型結果、缺少樣本或額外重試不會補足其他條件。此彙整只涵蓋模型品質與完整審查時間；helper、平台矩陣及封裝仍須分別通過。

## 2026-10-29 全面優化實作與驗收狀態

本節記錄新一輪優化；前文的歷史結果與失敗均保留，不作為本輪通過證據。

### 固定基準

- Git commit：`e2430b69b005226a1d77402bb090289c74e31dc2`
- 技能版本：`0.20.1`
- `skills/merge-reviewer` 全目錄（正規化相對路徑排序，逐檔 SHA-256 彙總）的固定內容 digest：`2a1d524800860595248ae232dc8ec3aec228ea71e620bd262a08ec44335e1d6a`
- 矩陣會從這個本機 commit 封存技能，並逐一驗證 commit、版本與 `SKILL.md` hash。候選版使用準備時固定的技能 hash。

### 預定門檻（尚待實測）

| 情境 | 五組交替配對的中位比值 | 狀態 |
|---|---:|---|
| 大型多 Repo，內部變更完整審查 | 候選／基準 ≤ 0.90 | 待測 |
| 大型多 Repo，跨 Repo 變更完整審查 | 候選／基準 ≤ 0.90 | 待測 |
| 單 Repo 各主要情境完整審查 | 候選／基準 ≤ 1.05 | 待測 |
| 大型多 Repo helper 暖快取 | 候選／基準 ≤ 0.80 | 待測 |
| 大型多 Repo helper 冷啟動、陸續啟動 | 候選／基準 ≤ 0.90 | 待測 |
| 固定本機來源／間接 Repo 在快照後的遠端存取 | 0 | 待測 |
| 本流程 GitLab API 呼叫 | 0 | 待測 |

完整審查時間從 agent 開始操作到報告完成，包含模型分析、工具往返、續讀與草稿修正。Helper 時間獨立列出。Helper 收據觀測 Git 程序數、fetch 命令數、Git 程序時間、同步等待、封裝等待、快取命中、start／step／finish 時間與完整驗證數；fetch 命令數不冒充實際連線數，實際連線證據目前標示為未觀測，模型分析時間與 token 數維持未知。

### 本輪功能驗證狀態

- 驗收矩陣固定使用上述 commit/hash，完整審查速度採五組交替配對，並設獨立單 Repo 5% 回歸上限。
- 新矩陣共 309 項：每模型／技能條件 31 個品質執行（279 項），以及 `local`、`mixed`、`single` 各五組 baseline／candidate 完整審查配對（30 項）。P0、所有安全反例及跨分支案例在每個模型／技能條件下各重複三次。
- 盲測新增適用的根目錄 `AGENTS.md` 規則、巢狀不適用規則及相反的 README 干擾指示案例，含破壞性變更與相容安全變更兩種 oracle。
- 本輪從固定 commit 建立基準，不再傳入舊 `--baseline-skill` 路徑：

  ```powershell
  python -X utf8 -B tests/merge_acceptance_matrix.py prepare --root "<全新且位於 Repo 外的驗收根目錄>" --candidate-skill skills/merge-reviewer
  ```

- 先前探索選取的 13 個測試有 4 項通過、9 項受 Windows 沙箱的 Git transport 限制；這是歷史探索結果，不代表本輪候選版完整驗收。
- 尚未完成的驗收包括網路與 TTL 邊界矩陣、固定證據與搜尋等價矩陣、Windows／Python 3.10／3.14 CI，以及三模型 `high` 設定的無 Skill／基準 Skill／候選 Skill 盲測。P0、安全反例與跨分支整合案例仍需各重複三次。
- 關鍵漏報、誤報、錯誤宣告完成或敏感資料外洩任一發生時，品質閘門不通過。所有失敗與重跑都須留存。

本輪必須同時通過效能、來源政策、語意品質與相容性門檻後才能標記完成；目前各測量門檻仍是目標，不能引用既有 helper 的局部加速紀錄代替。
