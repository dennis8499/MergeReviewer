# 0.12.2 效能改善驗收 — 2026-10-16

以 `df4e50c9d00db8f42388b1b62ccbb8eafb9f784c` 的未修改副本作基準，在同一台 Windows／Python 3.14 機器比較目前工作樹。批次操作、摘要輸出及單次驗證快取減少 helper 成本；原有命令預設輸出、v3 證據／結果契約、完整 snapshot、發布前搜尋重驗與漂移檢查保留。

## 五次 helper 對照

兩個系統暫存目錄中的六 Repo 工作區各自使用同一組固定提交、五個已實際審查的契約、10 次固定讀取、60 筆搜尋及同一份 Codex 草稿。每版各跑五次，交替先後順序；正式量測時未並行執行測試。計時包含 CLI 程序啟動及 Git 呼叫計數器，不含工具平台往返。以下為秒數中位數，總計欄獨立取中位數，並非各欄中位數相加。

| 情境／階段 | 原版 | 改善後 |
| --- | ---: | ---: |
| 安全別名：擷取 | 5.84 | 4.81 |
| 安全別名：讀取／搜尋／契約註冊 | 7.11 | 4.21 |
| 安全別名：驗證／發布 | 5.21 | 4.53 |
| 安全別名：helper 總計 | 18.17 | 13.72（減少 24.5%） |
| 欄位移除：擷取 | 5.70 | 5.42 |
| 欄位移除：讀取／搜尋／契約註冊 | 7.17 | 4.38 |
| 欄位移除：驗證／發布 | 5.01 | 4.83 |
| 欄位移除：helper 總計 | 17.95 | 14.52（減少 19.1%） |

兩例每次 Python CLI 呼叫由 27 降為 9，Git subprocess 由 407 降為 368；擷取回傳由 10,920 降為 5,868 bytes，完整 manifest 仍寫入磁碟。60 筆發布前搜尋照常重跑。20 次重播的完整 manifest、讀取內容、搜尋紀錄／ID／順序及 Markdown 均相等；只正規化時間、run ID、暫存路徑及衍生 context hash。

這是**固定草稿的 helper 重播量測**。模型語意審查另行執行，未做舊新版各五次模型分析時間對照，`model_review_seconds` 明確為 null；上述百分比不能當作完整技能或模型加速比例。

## 語意與相容性驗收

| 實際靜態審查案例 | 結果 |
| --- | --- |
| 保留 totalCents，新增 amountCents 別名 | 完整追查 web、invoice、共用驗證／轉換及間接 settlement 消費端；零問題。 |
| 從事件及 API 回應移除 totalCents | 一項 P1 結算鏈中斷、一項 P2 金額顯示 NaN；完整覆蓋並保留無關 Repo 排除理由。 |
| Merge 解衝突遺失 owner 檢查 | 一項 P1，以 main parent 與最終版本證明授權回歸。 |
| 後續提交補回 owner 檢查 | 零問題，未把舊 merge 的缺陷回報為目前問題。 |
| 乾淨合併預覽仍有 charge 簽章衝突 | 一項 P1，以 base 新介面及 preview 舊呼叫證明不相容。 |

後三例也使用相同 fixtures／Codex 草稿比較原版與新版的完整 manifest、驗證結果及報告，結果一致。這些是知道案例背景的實際靜態審查，不是盲測或一般化模型準確率評估；未執行應用程式測試／編譯器，問題標示 `code-derived`。

| 本機檢查 | 結果 |
| --- | --- |
| 完整 Python 回歸 | 90 項：89 通過，1 項因 Windows 符號連結權限不足略過。 |
| 最後稽核後的快取回歸 | 8 項通過，包含追加的兩項損壞 JSON metadata 案例；維持原有錯誤與忽略欄位行為。 |
| 快取整合／安裝 ZIP 再驗證 | 7 項通過；解壓後的 batch-read、batch-search 及報告發布可執行。 |
| 自動 BDD | 15 情境／57 步驟通過；GitHub 發布人工情境未執行。 |
| Skill validator、Python 3.10 語法及 diff 空白檢查 | 通過；未宣稱執行 Linux 或 Python 3.10 runtime。 |

snapshot 另與原版逐欄比較，涵蓋特殊路徑、tracked／untracked 報告、缺少檔案、衝突索引及 clean／dirty／missing submodule。批次驗證涵蓋順序、逐查詢 checkpoint、部分失敗及二進位內容；快取驗證涵蓋跨 Repo／版本／物件儲存隔離、損壞 metadata、下一次驗證重新讀取及 tree identity 檢查。

本機忽略於 Git 的可重播資料：[彙總](../test-artifacts/performance-20261016/summary.json)、[安全案例](../test-artifacts/performance-20261016/safe/summary.json)、[欄位移除案例](../test-artifacts/performance-20261016/regression/summary.json)、[merge／preview 驗收](../test-artifacts/performance-20261016/merge-acceptance.json)。同目錄保留每次完整 artifacts、原版來源、固定 Git fixtures 與實際審查 scenarios；本次 owned context 及原始一次性 fixture 已清理。

資料封存到工作樹後，另用封存的原版來源、Git fixtures 及 scenario 實際重跑一組舊新版，證據／報告仍相等；這組重播位於 OneDrive，僅驗證可重跑性，未納入上述五次中位數。

重跑安全案例可執行以下命令，輸出目錄須尚未存在；將 `safe` 換成 `regression` 可重跑缺陷案例：

~~~powershell
python -X utf8 -B tests/benchmark_review.py --baseline test-artifacts/performance-20261016/baseline --fixture-root test-artifacts/performance-20261016/fixtures/safe --scenario test-artifacts/performance-20261016/safe-scenario.json --output-dir test-artifacts/performance-rerun-safe --repeats 5
~~~

---

# 0.12.1 報告格式驗收 — 2026-10-15

本次調整報告檔名的固定 8 碼 commit，並在結論後補上比較資訊。完整 SHA、UTC 微秒時間戳、單一 Markdown 輸出及碰撞保護均保留。

| 本機檢查 | 結果 |
| --- | --- |
| Python 完整回歸 | 65 項：64 通過，1 項因 Windows 符號連結權限不足略過。 |
| 自動 BDD | 15 情境／57 步驟通過；GitHub 發布人工情境未執行。 |
| 版本提升後的文件／安裝套件回歸 | 2 項通過。 |
| Skill validator、Python 3.10 語法相容及 Git diff 空白檢查 | 通過。 |

回歸涵蓋本機與遠端分支、四個版本均不同的分支／共同祖先／checkout、沒有差異、審查未完成，以及兩種發布碰撞。安裝套件驗證確認短版檔名、比較分支表與附錄完整 SHA。

本機 `merge-reviewer-0.12.1.zip` SHA-256：`5347167ee10781fba708e075717be87201ea3a53085ae84b0f792e281835ab49`。下方舊版套件與語意案例保留原始歷史紀錄。

---

# 0.12.0 驗收 — 2026-10-14

本次在 Windows／Python 3.14.7 完成新版分支介面、條件式延伸及精簡報告驗證。以下為實際執行紀錄，未宣稱已執行 Linux 或 Python 3.10 的 CI 矩陣。

| 檢查 | 結果 |
| --- | --- |
| Python 回歸（所有六個測試模組） | 61 項：60 通過、1 項因 Windows 符號連結權限不足略過。 |
| 自動 BDD | 15 情境／55 步驟通過；GitHub 發布的人工情境未執行。 |
| Skill creator validator | 通過。 |
| Python 3.10 語法相容檢查 | 通過；不是 Python 3.10 runtime 測試。 |
| Markdown 本機連結、套件解壓與獨立 helper 操作 | 通過。 |
| Git diff whitespace check | 通過。 |
| 安裝 ZIP 與 source bytes | 12 個檔案逐一一致，不含停用的入口及 receipt runtime。 |

## 實際 Codex 靜態技能案例

實作的 Codex 工作階段閱讀固定差異、基礎版本、呼叫端、事件消費端、套件身分及共用契約，執行契約搜尋、撰寫語意結果，再由發布器驗證並產生 Markdown／JSON。此工作階段知道案例預期，因此不是盲測或一般化模型準確率評估；TypeScript 專案測試未執行。

| 變更 | 審查結果 |
| --- | --- |
| 移除舊金額欄位，只輸出新名稱 | 一項 P1，涵蓋頁面金額失效、背景驗證拒絕及後續結算中斷。 |
| 保留舊欄位並新增等值別名 | 未建立具體問題，已查的消費端仍相容。 |

兩例都有唯一受審 Repo、5 個固定契約、66 筆固定版本搜尋及 7 個相連功能節點。相關但未修改的 Repo 使用自身 HEAD，無關使用者管理 Repo 有搜尋及排除理由；`extension_status=complete`。報告只發布到所選 Repo，owned context 和一次性 fixture 均已清理。

這輪實際使用修正了「把下游延伸契約也強制綁回源根節點」的覆蓋錯誤：源根涵蓋直接變更契約，下游契約在自己的來源節點提供證據並連回變更。回歸已保護此行為。

本機忽略於 Git 的證據：

- [欄位改名報告](../test-artifacts/0.12.0/regression/summary.md)及[驗證結果](../test-artifacts/0.12.0/regression/result.json)。
- [安全別名報告](../test-artifacts/0.12.0/safe/summary.md)及[驗證結果](../test-artifacts/0.12.0/safe/result.json)。
- [最終單元測試紀錄](../test-artifacts/0.12.0/unit-tests.log)及[BDD 紀錄](../test-artifacts/0.12.0/bdd.log)。

可由 `python -X utf8 -B tests/skill_acceptance.py --case regression`／`--case safe` 重新準備隔離來源，再依新版 Skill 執行實際審查。此 helper 只準備來源，不模擬模型結論。

## 報告命名修正

報告名稱已統一為 **Merge Report**，Markdown 標題及 JSON 的 `merge-report-concise/v1` 已由 6 項報告／文件測試、1 項發布 BDD 情境與 Skill validator 再次驗證。上述靜態技能案例的本機證據保留名稱修正前的原始輸出；命名修正另記於 [驗證紀錄](../test-artifacts/0.12.0/report-naming.log)。

## 單一 Markdown 檔名及移除 JSON — 2026-10-14

每次成功發布只留下 `<所選Repo>/review-reports/merge-review-<時間戳>-<基礎分支commit>-<比較分支commit>.md`。時間戳為 UTC 發布時間並含微秒；兩個 commit 使用擷取時完整分支頂端 SHA。同名及競態不覆寫既有報告，寫入失敗不留下部分報告。發布器只回傳 Markdown 完整路徑；JSON 匯出選項已移除。

本次調整的報告測試 18 項、文件／安裝套件測試 2 項全部通過；包括固定分支 commit、共同祖先與目前 checkout 的區分、P0–P3 問題完整保留、移除 JSON 選項、碰撞／競態及失敗清理。自動 BDD 15 情境／56 步驟通過，1 項 GitHub 發布人工情境未執行。Skill validator 與 Python 3.10 語法相容檢查通過。

上方 61 項完整回歸及實際靜態案例屬輸出調整前的執行紀錄，本次未重跑語意案例。最新檢查保留於 [Markdown 輸出驗證紀錄](../test-artifacts/0.12.0/markdown-output.log)；安裝 ZIP 12 個檔案已與目前 source 逐一比對。

## 套件

本機產物：`dist/merge-reviewer-0.12.0.zip`，141,616 bytes。

SHA-256：`edfe9a1888fc07a1599f24a3418b18373af3909879e3909dcd47f7efc86d8933`。

此紀錄不代表已推送、建立 tag 或發布 GitHub Release；GitlabWorkSpace 的既有 0.11.0 固定未更新。

---

# Independent Group v2 Skill acceptance — 2026-10-13

An independent agent read the revised Skill and executed two isolated Group
reviews without being supplied the intended verdict. Each review registered four
fixed-source contracts, performed 84 recorded searches, reviewed eight connected
impact nodes and published Markdown/JSON through `review_report.py` (exit 0).

| Input | Observed review |
| --- | --- |
| Replace the existing amount field | One P1 covering the web display failure, shared validation rejection and missing downstream settlement event. |
| Preserve the existing field and add an equivalent alias | No findings; existing consumers retain compatible values. |

Both reviews traced `orders-api → invoice-worker → shared-contracts →
settlements.ready → settlement-worker`, checked the unchanged web caller, and
excluded unrelated-admin after contract-related searches and source inspection.
Both reports were complete and their owned contexts were cleared. The agent did
not change product files, refs or index, access the network, or execute application
tests; all finding scenarios are code-derived. This demonstrates the workflow on
these fixtures, not general model accuracy.

Local ignored evidence: [field replacement](../test-artifacts/group-v2-acceptance/A/summary.md)
and [compatible alias](../test-artifacts/group-v2-acceptance/B/summary.md), each with
its adjacent `result.json` and Repo detail reports. Subsequent independent code
review added regressions for Repo-only search compatibility, partial receipt
selection, dual-source consumer coverage, Group boundary drift, propagated
wrapper aliases, binary consumer drift and crash-safe upgrade recovery.

The same two Groups were then reviewed through the final GitlabWorkSpace 0.22.0
offline installation using `MergeReviewTask/v2`, explicit `groupRoot` and the
original fixed source/target SHAs. The installed reviewer reread ten fixed-source
files/previews per case, registered four contracts, recorded 84 searches and
reviewed eight impact nodes. Each case completed 99 commands without failure,
published `MergeReviewReport/v2` and passed the installed MR binding verifier.
The field replacement still produced one P1; the compatible alias produced no
findings. Both reports retained Group scope and complete coverage. Product files,
HEAD, branches, refs, index and status stayed unchanged, and owned contexts were
cleared. Local evidence: [installed MR field replacement](../test-artifacts/group-v2-acceptance/Workspace-MR-A/merge-review-20261013T065008Z-222829ed/summary.md)
and [installed MR compatible alias](../test-artifacts/group-v2-acceptance/Workspace-MR-B/merge-review-20261013T065010Z-5795a4ff/summary.md).

The final installation also validated the independently explored Megin
`orders-api → checkout-ui → receipt-worker` architecture against the same fixed
sources (exit 0). All 58 files in the 13 bundled Megin Skills matched the final
native package; product state and architecture records remained unchanged. This
was architecture-only acceptance, with proposed compatibility checks retained as
unexecuted drafts.

Current Windows validation for Megin 0.8.0, MergeReviewer 0.11.0 and
GitlabWorkSpace 0.22.0:

| Check | Result |
| --- | --- |
| Megin quality gates | 98 passed. |
| MergeReviewer full regression | 141 cases: 140 passed, one symbolic-link case skipped because the Windows account lacks permission. |
| Final Group quick entry regressions | Four passed, including the additional declared-Group quick alias case. |
| Automated BDD | 24 scenarios / 99 steps passed; real GitHub publishing remains a separate manual scenario. |
| Workspace integration and maintenance | 156 passed across installer, profiles, transactions, installed Group, MR adapter, TypeScript, AutoHook, ReleaseReviewer and historical receipts. |
| Release artifacts | Native source pins, installed bytes, VSIX/ZIP payloads, SHA-256 and archive reproducibility passed. |

# Earlier portable v1 acceptance — 2026-10-12

The implementing Codex session executed the portable Group workflow against two isolated Git workspaces prepared by `skill_acceptance.py`. Codex read the frozen changes, baseline/merge-preview, callers, event consumer, package identities and shared contract; searched the captured sources; decided the findings; authored drafts; and published them through `review_report.py`. The fixture helper prepares source only and never generates review decisions.

| Case | Expected behavior | Observed Codex review |
| --- | --- | --- |
| Remove `totalCents`, expose only `amountCents` | Detect incompatibility in unchanged callers and event consumers | One P1 finding: the web display reads a missing field and Worker contract validation rejects the event before settlement. |
| Keep `totalCents` and add the equivalent `amountCents` alias | Preserve existing consumers without an invented regression | No findings; existing display, validation and settlement still receive the original value. |

Both reports follow `orders-api → web` and `orders-api → invoice-worker → shared-contracts`; the four involved Repos are recorded with fixed source refs. `unrelated-admin` is explicitly excluded after source inspection and contract searches. The API is the only changed Repo. Changed-path coverage and connected impact coverage pass validation; no candidate is silently included or omitted. Original HEAD, branches, index and source files remain unchanged by review preparation and publication.

Local generated artifacts (ignored by Git):

- [Regression report](../test-artifacts/skill-acceptance/regression/summary.md) and [validated result](../test-artifacts/skill-acceptance/regression/result.json).
- [Safe-change report](../test-artifacts/skill-acceptance/safe/summary.md) and [validated result](../test-artifacts/skill-acceptance/safe/result.json).

Reproduce the inputs with `python -X utf8 -B tests/skill_acceptance.py --case regression` or `--case safe`, then ask Codex to execute the returned skill request. Follow `references/workspace-review.md` for the frozen-source commands and draft contract. Publish or clean the returned context, then clean the fixture root with `review_session.py cleanup --context-dir <fixture_root>`.

This is actual static skill execution by the current Codex session, with known expected outcomes, not an independent blind benchmark. TypeScript application code and application tests were not executed. Findings use `scenario_source: code-derived`. These two cases establish workflow acceptance, not a general model recall/false-positive rate; helper regression tests separately exercise scope, snapshots, evidence validation, incompleteness and cleanup.

## Implementation validation

Final source/package checks ran locally on Windows with Python 3.14.7:

| Suite | Result |
| --- | --- |
| MergeReviewer `python -X utf8 -B -m unittest discover -s tests -p 'test_*.py'` | 121 tests: 120 passed, 1 skipped because this Windows account cannot create symbolic links. Includes 29 new workspace tests. |
| `python -X utf8 -B -m behave tests/features --tags='not @human-acceptance'` | 24 scenarios passed; the GitHub release-publishing human scenario was excluded. |
| GitlabWorkSpace `test/overlays/merge-reviewer/test_group_review.py` | 10 passed, including portable target dispatch, fixed MR rejection and existing legacy Group/MR behavior. |
| GitlabWorkSpace `test/release/workflow_kit_installer_test.py` | 11 passed. |
| GitlabWorkSpace `test/release/workflow_kit_profiles_test.py` | 7 passed. |
| GitlabWorkSpace `test/release/workflow_kit_transactions_test.py` | 20 passed, including reproducible archive digests and upgrade/rollback preservation. |
| Skill creator validator and `git diff --check` in both repositories | Passed. |

The final standalone `dist/merge-reviewer-0.10.0.zip` contains all 19 installable skill files and matches the fixed Workspace source archive byte-for-byte. SHA-256: `ed02ca66030420c776447c31725600be9a96d97ba70eb7844ff7017acabb3d2e`. The Workspace offline tar and release ZIP were rebuilt with this pin. These local checks do not claim execution of the Linux/Python 3.10 CI matrix or external release publication.
