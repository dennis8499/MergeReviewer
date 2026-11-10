# 三階段操作

`review_session.py start／step／check／finish` 編排固定讀取、契約與發布。以下 runtime、id、context 沿用 start；Repo ID、來源、path 及 SHA 使用實際固定索引。JSON 以 UTF-8 寫檔。PowerShell 管線傳 Python 前設定 `$OutputEncoding = [System.Text.UTF8Encoding]::new($false)`。

## 開始與恢復

0.21.0 capabilities 增加 `features.launch_preflight`、`read_only_inspection`、`completion_preview`、`scope_notes`，均為 true。既有 `request_preflight` 只代表 step 請求預檢，不能用來推定啟動預檢能力。

首次啟動用 `preflight` 取代相同命令中的 `start`，保留其餘參數。回應 `MergeReviewPreflight/v1` 含 `valid`、`errors[].code/pointer/message`、`canonical_inputs` 與 `selected_repo`；exit 0 為有效、exit 3 為輸入需修正。它只讀工作區、分支設定及 runtime 身份，不 fetch、不建立 runtime／job／lock／報告，也不以預檢代替 start 的實際寫入測試。遠端分支可用性仍待 start 同步確認。fresh start 自行再預檢，失敗不消耗 request ID；既存 ID 依固定輸入及快照恢復，不重驗可變工作區。

~~~powershell
python "<skill-dir>/scripts/review_session.py" start --workspace "C:/team" --project Orders --base origin/main --head origin/feature/payment
python "<skill-dir>/scripts/review_session.py" status --runtime-root "C:/team/.review-runtime" --request-id "<id>" --summary
~~~

start 回傳 `MergeReviewStart/v1`，含固定 Repo／source ref、必要覆蓋及 parent、指示、patch、草稿與操作範例。`<context>/start.json` 保存完整索引；status 補上已執行操作、契約及草稿尚未完成項。stdout 超過 24 KiB 時回傳 `MergeReviewEvidenceSummary/v1`，保留完整 artifact；stderr 為階段事件。

先查 capabilities：`skill_version`、`helper_sha256`、`python_version` 識別目前安裝；start 與持久 job 的 `tool_identity` 識別擷取時的工具。helper 摘要依 scripts 內所有 Python 檔名及內容 SHA-256 計算；同版本的不同 helper 內容可區分，不會因工具更新而自動重新擷取。

能力列出 `summary_status` 時，`status --summary` 回傳 `MergeReviewStatusSummary/v1`：狀態、錯誤與缺口預覽／總數、未完成項計數、報告路徑及 `next_action`。大型收據與完整操作索引透過 `details` 的原 status 命令取得；預覽不代表全部內容已讀完。原 status 介面保留，發布狀態仍核對報告正文收據。

能力列出 `read_only_inspection` 時，`status --summary` 與 capabilities 保證不寫檔，artifact 使用 `view --read-only`；單筆及批次均略過 metrics 寫入，continuation 保留 `read_only=true`。舊版 view 預設的分頁計量維持相容。唯讀檢視不建立額外 artifact 或快取。

可自行產生 32 位小寫十六進位 request ID，透過 `start --request-id` 在輸出遺失後重送。相同 ID／輸入回傳既有狀態，不重新擷取；改變輸入須新 ID。`--refresh` 忽略已完成快取及失敗冷卻，仍可加入開始等待後完成的同需求同步。

成功 refresh 後，新請求優先採最新同步結果。已知 SHA 改變或同需求同步失敗的舊收據不再重用；冷卻結束會重新同步。這些失效標記不改動已固定的審查。

單 Repo 根目錄加 `--runtime-root "C:/review-runtime/team"`；也可設定 `MERGE_REVIEW_RUNTIME_DIR`。必須可寫且位於所有宣告 Repo 外，拒絕 symlink／junction 或未標記的非空目錄。Git 共用網路 runtime 沿用 `GITLAB_WORKSPACE_RUNTIME_DIR`，兩個 runtime 用途不同，start 都會預檢。

Repo 掃描會略過 `.review-runtime`、`.gitlab-workspace/runtime` 與上述設定指定的 runtime 子目錄，保留其他 kit 子目錄中的 Repo。明確宣告的 workspace folder 仍作為掃描根；真正受審範圍的讀取錯誤維持失敗，不以略過權限錯誤縮小範圍。

工作台自動傳入帳號範圍；直接 CLI 也可用 `--auth-scope "<account-identity>"`。預設以 OS 使用者及可見認證設定／環境區隔；外部 credential manager 的不可觀察帳號切換，須改 scope 或明確 refresh。

每份 start 都重新固定 peer Repo 的本機 HEAD。peer 只讀該 SHA 的已提交物件，省下整份工作目錄與 index 的前後掃描；擷取後 HEAD 移動或未提交編輯不改變當次證據，下一份 start 才取得新的 HEAD。封裝、固定 refs、完整物件及發布證據驗證仍須通過。

## 同輪有序操作

~~~json
[
  {"id":"instructions-1","op":"instructions","repo":"repo-000","path":"src/publish.py"},
  {"id":"diff-1","op":"patch","repo":"repo-000","path":"repos/repo-000/native/diff.patch"},
  {"id":"read-head","op":"read","repo":"repo-000","source":"head","path":"src/publish.py","around_line":18,"context_lines":15},
  {"id":"read-base","op":"read","repo":"repo-000","source":"merge-base","path":"src/publish.py","line_start":1,"line_end":12},
  {"id":"register-1","op":"register-contract","contract":{
    "id":"C-001","root":{"repo":"repo-000","source":"head","path":"src/publish.py"},
    "kind":"behavior","symbol":"amountCents","parent_ids":[],
    "evidence":{"repo":"repo-000","source":"head","ref":"<fixed SHA>","path":"src/publish.py","line_start":18,"line_end":19},
    "search_terms":[{"query":"amountCents","reason":"新欄位"},{"query":"totalCents","reason":"相同固定行段的舊欄位別名"}]
  }},
  {"id":"search-new","op":"search","contract":"C-001","query":"amountCents","repo":["repo-000","repo-001"]},
  {"id":"search-old","op":"search","contract":"C-001","query":"totalCents","repo":["repo-000","repo-001"]}
]
~~~

存到 `<context>/round-1.json` 後執行 step。初始 anchor 須覆蓋實際變更行；替代詞不在同一 anchor 時提供固定 alias evidence。契約與草稿定義在 [workspace-review.md](workspace-review.md#多儲存庫延伸)。

操作為 read／register-contract／search／instructions／inventory／patch／view。inventory 可限 repo／source；patch 用 start 列出的 path，可選精確 line_start／line_end。read 預設 Repo 主來源；around_line 大於檔尾時縮到尾端，context_lines 為非負整數；精確範圍須成對且不得越界。

結果 `operations[]` 按順序帶 id／artifact／result。成功一次保存 manifest；失敗保存成功前綴並回傳 `complete=false／next_index／error`。修正失敗項後可重送整份陣列；成功 ID 的內容須保持一致，重用 artifact 而不新增契約或搜尋。新操作給新 ID。

job `progress` 使用 `completed/total`、`operation_id`、`operation` 與 `at`；每項完成及整批出口持久更新。完成後清除 active operation，整批成功為 N/N；部分失敗是已完成前綴／原批總數。重用 operation 也會結算完成；語意覆蓋仍只看草稿，不能以操作完成數代替。

較大的 step 優先使用 `MergeReviewEvidenceStep/v1`：read 的 `content_id` 指向頂層 `contents[id].lines`，每筆保留來源與全文 hash；搜尋的共同欄位合併，所有 matches 保留。仍超過上限時使用 `MergeReviewEvidenceStep/v2`：按操作保留可容納的小結果，其餘以 `result_returned=false` 與 `continue` 指向完整 artifact；操作太多時，`operations_next_offset`／`continue_operations` 指向後續清單。`evidence_complete` 表示完整保存，`result_returned` 表示此次是否回傳該結果；語意是否審查由 agent 判斷。必要內容續讀後才標 reviewed。

Repo 內搜尋可用 `change_id` 指向必要變更；單 Repo 搜尋也可省略 contract。跨 Repo 須先註冊契約，再搜尋及閱讀 consumer；發現間接輸出契約才建立下一輪。

契約的 `search_scope` 可為 `repo-local|workspace`；省略時為 workspace。判定及草稿例子見[契約邊界](contract-boundaries.md)。同來源的安全查詢可合併執行，每個查詢仍有獨立 ledger；特殊輸入以單查詢處理。finish 忽略閱讀階段的搜尋快取並重新驗證。

## artifact 續讀

使用專用 `view` 子命令分頁。每次續讀不建立 step operation、不更動 manifest，也不啟動 Git：

~~~powershell
python "<skill-dir>/scripts/review_session.py" view --runtime-root "<runtime>" --request-id "<id>" --artifact-scope context --artifact "<SHA-256>" --pointer "/operations" --offset 0 --page-size 20
~~~

回應會標明 `scope`，並在未完成時提供完整 `continue` 參數。續讀 terminal response 時將 scope 設為 `runtime`。`status --artifact` 仍相容，可加 `--artifact-scope context|runtime`。JSON pointer 的 `/`、`~` 以 `~1`、`~0` 轉義；陣列回傳 items／total／next_offset，長字串用 string_offset。字典的 `fields` 是輸出欄位，不能當成輸入參數。若單一欄位超出頁面上限，回應提供更深一層 pointer 的續讀連結。操作 artifact 保存原始 result；外層摘要 artifact 保存完整回傳，兩者 pointer 不同。artifact 位於 `<context>/views/<SHA-256>.json`，helper 讀取核對 hash。

`view_complete=false` 表示仍有未回傳內容。長字串陣列的 preview 後沿 continue 讀取剩餘字元；大型字典的 `value.continue` 按 offset 接續未列出的欄位，`drilldowns` 讀取尚未展開的值。保留每個分支的續讀入口，不能以外層陣列已到末端推定內容完整。

能力列出 `batch_artifact_view` 時，可將多筆續讀寫成 JSON 陣列後使用同一 `view` 入口：

~~~json
[
  {"id":"source-lines","artifact":"<artifact SHA-256>","artifact_scope":"context","pointer":"/lines","offset":0,"page_size":40},
  {"id":"search-matches","artifact":"<artifact SHA-256>","artifact_scope":"context","pointer":"/searches/0/matches","offset":0,"page_size":20}
]
~~~

~~~powershell
python "<skill-dir>/scripts/review_session.py" view --runtime-root "<runtime>" --request-id "<id>" --requests-file "<pages.json>"
~~~

批次回傳 `MergeReviewArtifactBatch/v1`，`results[].result` 與單筆頁面相同；同一 artifact 在該批只讀取及驗 hash 一次。整批仍受 24 KiB 限制；若 `complete=false`，用同一檔案及 `--offset <next_index>` 接續未回傳的請求。此 offset 是批次索引，各筆 JSON 的 offset 是該頁資料位置。單頁太大時減少該筆 page_size。獨立錯誤留在對應 result，其他頁仍可返回；有錯誤或尚有未回傳請求時 exit 3。`complete=true` 只表示所有請求已有回應，各頁仍須沿 continue／drilldowns 讀完。批次不新增 step operation、不改 manifest、不產生新 artifact、不啟動 Git。單筆 view 與 status --artifact 介面保留。

終止後 context 已清理、且 status 或 finish 結果仍超過上限時，摘要標示 `response_scope=runtime`，完整回應保存於該 request 的 runtime；用 `review_session.py view --artifact-scope runtime` 續讀，長字串依 `string_offset` 接續。此續讀只讀本機回應，不重新驗證或連線；終止紀錄到期後隨之清理。

24 KiB 限制傳輸，不取代語意覆蓋。必要命中及指示讀完才能判斷；用 artifact 翻頁。

## 完成、取消與錯誤

check 增加 `expected_outcome`（`review_complete/review_status/recommendation/pending_confirmation_count/extension_status`）及結構化 `blocking_gaps`。必要 limitations、固定擷取缺口及待確認會在發布前呈現；`scope_notes` 與 `tests_not_executed` 不阻擋完成。有 authoring errors 時 outcome 為 unknown，修正後重查。check 仍零 Git、`evidence_verified=false`；其 `remaining_counts`、`expected_outcome`、`blocking_gaps` 與 `last_check_at` 保存至 job，供工作台顯示最近一次檢查。正式 finish 另做完整固定證據驗證。

agent 先補查所有可取得的必要證據，再判斷是否真正受阻。helper 保留發布誠實未完成報告的能力；來源無法取得時，保留已確認問題、具體缺口及下一步，不能把未讀完當成資料不可取得。

草稿初始 summary 空、必要覆蓋與 parent 為 not-reviewed，其他 Repo 為 unresolved。parent 模板已填固定最終來源 ref；每筆 reviewed parent 仍須引用與路徑相符的 parent 及最終證據。agent 依實際工作補齊，不能直接批次改為 reviewed。先用 check 取得 `errors[].code/pointer/message` 與 `gaps`；pointer 指向草稿欄位，manifest 問題以 `/manifest/...` 指出。check 使用固定索引提前核對 parent 與最終 ref，不執行 Git，`evidence_verified=false`。無欄位錯誤且無未完成項時 exit 0，否則 exit 3；finish 仍須完整驗證來源、引用及搜尋。finish 的可修錯誤 exit 3 保留 context，快照損毀 exit 2 終止。

check 與 finish 共用 Repo 關聯及 finding 引用範圍判定；沒有 impact 證據的 peer 不得標為 related，finding／待確認的證據也不能擴張已排除 Repo。已知且綁定固定 blob 的行數也用於提前檢查引用上界；未知上界仍由 finish 從固定物件核對。compact 診斷一次列出獨立錯誤，pointer 指向確切 finding／evidence／欄位；`available_read_ids`、`available_line_ranges` 及總數協助修正引用。這些提示只引用已完成操作，不代填語意判斷。

發布預檢與正式發布共用 sibling 暫存檔、完整寫入／讀回及原子提交；Windows 檔案繼承輸出目錄 ACL，既有報告不覆寫。完整驗證後若輸出遇到可恢復的權限、占用或容量錯誤，finish 回傳 exit 3、`phase=publication-repair`、`recoverable=true`、`context_retained=true`，保留原 request、草稿與固定 SHA。修復輸出後重試同一 ID 的 finish，仍完整驗證證據；需要放棄時 cancel。損毀來源與非輸出錯誤仍是終止錯誤。舊 review_report.py 的預設失敗清理行為保留。

純 formatter 內部例外例外保存為 `failure_kind=render`、`state=failed/phase=terminal`、`context_retained=true`、`recoverable=false`，`next_action=inspect-render-error`；不建立 publication receipt，原工具身份及 SHA 保持不變。相同 request 的 finish 停止，不自動重試或換版本繼續發布。可用 status／唯讀 view 診斷，`cancel` 清理 retained owned context。保留期限從首次失敗起 24 小時，status 不延長；下一次 start 才惰性驗 ownership 並清理，清理失敗留診斷。已被舊版清除的 context 無法復原；已發布 receipt 與 outcome 不改寫。

每個 reviewed impact 至少有一份合法 evidence 與節點自身 `repo/source/path` 相同，其他版本的輔助證據可一併保留。check 與 finish 共用契約錨點及 coverage 起點的同位置規則：僅把契約 ID 放在另一個檔案或版本的節點上仍不完整。

`--requests-file` 遺失、不可讀、編碼或 JSON 錯誤回傳 exit 3、`terminal=false/recoverable=true`；status 保持 `state=reviewing/phase=input-repair`。欄位、型別、操作 ID 與已知參數錯誤會先在本機預檢，回傳 `code/pointer/operation_id/next_index` 及修正提示，再沿同一 request ID 重送。已完成的操作前綴保留。草稿遺失或不可讀也沿同一 ID 修復；恢復路徑以 manifest 及 pack 內容雜湊確認證據未變，若收據不符才執行完整證據檢查。每次 finish 保留獨立耗時與 Git 計數；最終發布前仍完整驗證所有證據。損毀的 manifest、pack 或收據維持終止錯誤。

`metrics` 按 start、每次 step、check 與每次 finish 保留 Git 程序數及耗時；step 另列 read/search 秒數與次數、各操作類型耗時、錯誤後證據重驗及略過次數、owner lock 等待，artifact 分頁另計。已知的請求格式／欄位錯誤不執行失敗操作；此前有效操作仍保留。行數越界會記住以固定 Repo、來源、ref、path 與 blob SHA 綁定的行數，重送時先預檢。此類修稿不重掃 pack；損毀快照、manifest、搜尋紀錄仍由再次讀取或發布前完整驗證攔截。不可讀請求檔仍沿恢復路徑檢查 pack。finish 保存每次嘗試、owner lock 等待、草稿準備與重驗耗時。`progress`／stderr 階段事件回報目前操作與完成數；Git fetch 的傳輸層連線數、模型時間及 token 未觀測時分別標成 `not-observed`、`null`、`null`，不可推算成零。

capabilities 列出 `validation_progress` 時，完整 status 與 `status --summary` 的 `progress.validation` 分別提供 `repositories`、`contracts`、`searches` 的 `completed/total`，`activity` 區分來源預讀、搜尋預讀及逐項核對，`validation_updated_at` 是最後驗證更新時間。預讀不增加已驗證搜尋數；只有重播內容與 ledger 比對成功才計入。證據修復重驗使用 `stage=evidence-recheck`，正式發布核對使用 `stage=validation`，兩次計數各自從零開始。階段切換及完成立即更新，同一活動最多每秒寫一次；單一 Git 命令尚未返回時，時間與計數保持最近的實際進度。發布與清理後保留最後驗證計數；狀態查詢只讀本機狀態，不啟動 Git 或改動 manifest。

~~~powershell
python "<skill-dir>/scripts/review_session.py" check --runtime-root "<runtime>" --request-id "<id>"
python "<skill-dir>/scripts/review_session.py" finish --runtime-root "<runtime>" --request-id "<id>"
python "<skill-dir>/scripts/review_session.py" cancel --runtime-root "<runtime>" --request-id "<id>"
~~~

發布先保存預定路徑、全文與固定正文 hash，以及同一次完整驗證產生的 `review_outcome`，再原子建立報告，保存完成紀錄後清理。摘要的 `schema=MergeReviewOutcome/v1`、`review_complete`、`review_status`、`recommendation`、`extension_status`、`pending_confirmation_count`、`gaps` 及 `remaining` 保留首次判斷；remaining 只含未完成的檔案、parent、impact、Repo、boundary 識別與理由，摘要套用敏感內容遮蔽，不保存原始來源片段。gaps 包含驗證後的 context／覆蓋／影響鏈／邊界缺口與待確認項。

status 與重試 finish 確認報告存在且正文符合收據；發布後 status／check 從持久摘要回報 `review_complete`、`gaps`、`remaining`，不重新執行 Git 或完整驗證。`state=published`／`phase=complete` 表示發布流程完成，審查完整性另看 `review_outcome.review_complete`。發布後 check 的 `publication_complete=true`、`ready_for_finish=false`（已無待發布操作）、`evidence_verified=false`；已知審查完整時 exit 0，未完整或未知時 exit 3。舊 job 沒有摘要時回傳 `outcome_available=false`、`review_complete=null`、`review_status=unknown` 及說明缺口，不從 Markdown 猜出通過。

人工核對標記區內的編輯可保留，但不改寫首次語意摘要；區外修改或遺失不回報成功。清理失敗仍回傳已發布路徑及 `cleanup_pending=true`；沿同一 ID 重試只清理，保留相同審查摘要，不重做 Git 驗證或建立另一份報告。取消等待者不影響其他請求；仍有等待者時先完成共用同步再取消自己的擷取。已發布不可取消。終止摘要保留 24 小時，之後新 start 清除；報告歷史保留。

相同需求失敗 30 秒內回報既有錯誤；明確 start --refresh 可重試。缺 refs 或同步失敗不採舊 tracking refs。固定證據交付後不回讀原 Repo 補救；capture 階段壞 peer 快取可從同一固定 SHA 本機重建。

## 刪除契約與影響圖

整檔刪除的 coverage 沿用 `required_coverage` 的比較來源，impact 的 source 採實際引用的左側版本。檔案仍存在但 API／欄位被刪除或替換時，coverage 仍引用最終內容，另外註冊舊符號契約並建立連通的左側 impact 節點：

| 本次變更 | 契約 evidence | coverage evidence |
| --- | --- | --- |
| 分支 `head` 中移除符號 | ref 等於 `diff_base` 的固定來源，通常是 `merge-base` | `head` |
| `merge-preview` 中移除符號 | 固定 `head` | `merge-preview` |

舊符號須出現在 evidence 行段內的實際刪除／替換行。分支分歧時不能用基礎分支頂端取代共同祖先；未修改行及錯誤 SHA 會被拒絕。最終內容的 coverage 起點節點須列出該初始契約，另以相同契約 ID 連到 `repo/source/path` 與舊符號 evidence 相同的節點，再追查固定 peer HEAD 的消費端。只有左側 evidence 不能代替存續檔案的最終內容覆蓋。
