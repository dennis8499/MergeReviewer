# 分支證據與 v3 結果契約

此文件集中定義 v3 證據、契約及草稿。新審查依 [start／step／finish](session-workflow.md)，下列獨立 CLI 保留相容；它不提供替代的語意審查判斷。新 context 可附加 request_id、runtime_root、sync_activity、operations，context/result 維持 v3。

## 證據目錄與固定讀取

~~~powershell
python "<skill-dir>/scripts/git_review_context.py" --workspace "<root>" --project "<Repo>" --base main --head feature/payment --summary --pretty
~~~

`--summary` 回傳 `merge-reviewer-workspace-summary/v1` 摘要，提供 `context_dir`、`manifest_path`、Repo／來源索引、必要覆蓋及缺口，省略完整檔案清單與 snapshot。`context_schema` 標示固定證據仍為 v3。完整 `manifest.json` 與證據照常保存，未加 `--summary` 時仍回傳完整 manifest。

磁碟上的 manifest `schema` 為 `merge-reviewer-workspace-context/v3`；`selected_repo` 是唯一受審儲存庫 ID，`seed_repos` 必須只有此 ID。`extension_enabled` 由宣告邊界內的不同 Git 根目錄數決定，不能以 `scope` 參數覆寫。`scope`／`target` 是內部紀錄，固定為自動辨識的 `repo`／`group` 及 `branch`。

其他欄位：`workspace_roots`、`extension_reason`、`review_basis=codex-native`、`repositories`、`required_coverage`、`changed_contracts`、`searches`、`context_gaps`。所選儲存庫記錄兩個分支的固定版本、差異、合併父提交與合併預覽；其他儲存庫只擷取自身本機 HEAD。`network_activity` 的四項計數及 `fetches` 的單位見[操作與負載](runtime-controls.md)。

新 context 加上 `snapshot_policy=frozen-start/v1`、`refs_pinned_at`、`capture_completed_at` 及 `workspace_instructions`。各成功擷取的 Repo 帶有 `snapshot`（`policy`、owned 相對 `git_dir`、`captured_at`、`object_format`）及固定 `instructions` 索引；所有來源的 `objects` 都指向該 owned Git 證據庫。普通非 thin pack 保存所需 commit、完整 tree/blob 及預覽物件，不借用原 Repo objects、不依賴原分支或後續 fetch。準備階段完成前仍需完整取得物件；完成後雲端／本機更新、force push、原 Repo GC 或工作區宣告變動不會替換或使這份快照失效。

準備時先固定入口 base／head 與所有其他 Repo 當前本機 HEAD，再使用這些 SHA 封裝全部快照；其間 ref 移動不切換已選版本，必要物件遺失則明確失敗、不自動重取。快照紀錄的是準備階段選定的各個 SHA，跨 Repo 不宣稱同一瞬間的原子擷取。報告列出準備起迄時間並只適用於這次固定版本；不自動重審或補抓新版本。缺少 `snapshot_policy` 的舊 v3 context 維持原本發布前漂移驗證，不能視為自足快照。

~~~powershell
python "<skill-dir>/scripts/review_workspace.py" inventory --context-dir "<context>" --repo repo-000
python "<skill-dir>/scripts/review_workspace.py" read --context-dir "<context>" --repo repo-000 --source head --path src/orders.py
python "<skill-dir>/scripts/review_workspace.py" search --context-dir "<context>" --repo repo-000 --query orderId
python "<skill-dir>/scripts/review_workspace.py" instructions --context-dir "<context>" --repo repo-000 --path src/orders.py
~~~

`inventory` 的 Repo ID 位於 `repositories[].repo`，來源檔案位於 `repositories[].sources[source].files`；不是 `repositories[].id`。例如：

~~~json
{"repositories":[{"repo":"repo-000","name":"OrderService","path":"<Repo-path>","role":"seed","sources":{"head":{"ref":"<完整提交 SHA>","files":[{"path":"src/orders.py","mode":"100644","kind":"blob","blob_sha":"<完整 blob SHA>"}]}}}]}
~~~

保留已取得的索引，需要定位檔案時才查對應 Repo，避免重讀完整清單。`read` 提供參照（ref）、全文 SHA-256 及逐行內容；指定 `--line-start 18 --line-end 24` 時只輸出該行段，另列 `line_start`／`line_end`／`total_lines`，保留原始行號與全文 hash。兩參數須成對、以 1 起算並包含兩端；非法、顛倒或超出內容的區間報錯，省略則讀取全文。完整差異的位置列於各儲存庫的 `patches`。閱讀所選儲存庫的差異、基礎內容、必要合併父提交差異與合併預覽，完成語意審查。

`instructions` 回傳擷取時適用的工作區指示與該 Repo 固定主要來源中的 `AGENTS.md` 內容、原始路徑／來源及 SHA-256；省略 `--path` 時列出該 Repo 全部固定指示，指定時只列祖先路徑。工作區 Git 外指示複製到 owned context，Repo 指示來自固定 head；未提交的 Repo 指示不替代固定版本。後續修改不改本次背景，使用者／系統指示仍優先。base／preview 中的指示差異作為受審資料閱讀。

### 批次固定讀取與搜尋

將已知且互不相依的操作寫入本次 owned context 內的 UTF-8 JSON 陣列，依陣列順序處理。`batch-read` 每項使用 `repo`、`path` 及選填 `source`；省略來源沿用該 Repo 的固定主來源。選填的 `line_start`／`line_end` 須成對，與單筆讀取採相同的 1-based inclusive 規則、全文 hash 及原始行號；省略則全文，非法區間報錯。例如 `<context>/reads.json`：

~~~json
[
  {"repo": "repo-000", "source": "head", "path": "src/orders.py", "line_start": 18, "line_end": 24},
  {"repo": "repo-000", "source": "merge-base", "path": "src/orders.py", "line_start": 18, "line_end": 24}
]
~~~

~~~powershell
python "<skill-dir>/scripts/review_workspace.py" batch-read --context-dir "<context>" --requests-file "<context>/reads.json"
~~~

回傳 `{"reads": [...]}`，各項保留原本 `read` 的結果格式。請求中的 ID、來源與路徑換成實際固定資料；僅在來源確實有該路徑時列入。同一批重複讀取共用已驗證內容，每項仍檢查來源、檔案身分與路徑；下一次命令重新取得證據。

`batch-search` 每項使用 `query`，可選 `contract`、`repo`（Repo ID 陣列）及 `source`，其語意與單次 `search` 相同。例如已註冊 C-001 後的 `<context>/searches.json`：

~~~json
[
  {"query": "totalCents", "contract": "C-001", "repo": ["repo-000", "repo-001"]},
  {"query": "orderId", "contract": "C-001", "repo": ["repo-000", "repo-001"]}
]
~~~

~~~powershell
python "<skill-dir>/scripts/review_workspace.py" batch-search --context-dir "<context>" --requests-file "<context>/searches.json"
~~~

範例搜尋詞必須換成該契約已註冊的相關詞；停用延伸時可省略 `contract`。回傳 `{"searches": [...]}`，依查詢及 Repo 順序平展原本的搜尋紀錄。每個查詢完成即保存 manifest，搜尋 ID 按原有順序遞增。相同固定來源與搜尋詞在單批內可共用成功結果，每筆契約綁定、紀錄及 checkpoint 照常建立；失敗的搜尋不共用，發布仍重新驗證固定搜尋。

同一 context 的搜尋及契約註冊只用單一寫入者，避免同時執行會修改 manifest 的命令。先註冊本輪所需契約及其父契約，再批次搜尋；閱讀命中內容後，將新發現的延伸契約另行註冊並進入下一輪。批次只合併工具往返，逐檔覆蓋、搜尋證據與直接／間接消費端追查要求不變。

### 搜尋 artifact 與續讀

加 `--page-size` 的搜尋標準輸出是 `ReviewEvidenceView/v1` wrapper，資料在 `result.searches`：

~~~json
{"schema":"ReviewEvidenceView/v1","artifact":"<完整 artifact SHA-256>","artifact_path":"<context>/views/<artifact>.json","result":{"searches":[{"id":"S-0001","repo":"repo-000","source":"head","query":"totalCents","contract_id":"C-001","matches":[],"status":"complete","ref":"<固定完整提交 SHA>","view":{"total_matches":0,"offset":0,"next_offset":null,"view_complete":true,"evidence_complete":true}}]}}
~~~

`artifact_path` 的磁碟檔案保存原始完整結果，頂層是 `searches`，沒有 `schema` 或 `result` wrapper；例如沒有命中的單筆搜尋原始內容：

~~~json
{"searches":[{"id":"S-0001","repo":"repo-000","source":"head","query":"totalCents","contract_id":"C-001","matches":[],"status":"complete","ref":"<固定完整提交 SHA>","output":""}]}
~~~

用 `view` 讀取 wrapper，避免自行猜測磁碟格式：

~~~powershell
python "<skill-dir>/scripts/review_workspace.py" view --context-dir "<context>" --artifact "<artifact>" --search-id S-0001 --search-id S-0002 --page-size 100 --offset 0
~~~

`--search-id` 可重複，只適用搜尋 artifact；重複 ID 去重，保留原結果順序，先篩選再按 match offset 分頁。未知 ID 或其他 artifact 類型報錯。篩選不改原 artifact ID／path、hash、完整結果或搜尋 ledger；未指定 ID 時沿用原本全部搜尋的格式。

### 祖先分支無新增內容

所有新 context 都記錄 `coverage_policy="ancestor-noop/v1"`，代表使用這套推導規則。僅在所選 Repo 同時滿足 `head == merge_base`、主要 diff 為空、merge-preview 為 clean／exit 0、預覽 tree 等於 base tree，且所選 Repo 沒有必要缺口時，`required_coverage` 為空，不為 base 已有內容建立 coverage、契約或搜尋。`required_merge_views` 仍依既有的有效差異／必要 parent 算法產生。

符合祖先條件時，所選 Repo 記錄 `ancestor_path`：由固定 base 到固定 head 的非空、不重複 commit SHA 陣列；兩者相同時只有一筆。擷取與封存驗證原始 commit 的 Git 物件雜湊及逐段 parent 關係，只額外保存路徑上的 commit，不新增中間歷史 tree／blob 或語意覆蓋。正式發布與保留草稿都從 owned snapshot 重驗這條路徑；缺失、損壞或偽造證明屬不可保留的證據錯誤，不回讀原 Repo 或重新擷取。未帶 `coverage_policy` 的舊 v3 context 沿用原覆蓋規則；啟用政策的祖先豁免必須有完整路徑證明。

所有 Repo 的固定 snapshots、原始主要／預覽 patches 及缺口仍保留；其他 Repo 的必要缺口不因這項規則消失。結果仍如實對帳缺口，不能把政策標記解讀成整個工作區無條件通過。只有主要 diff 空、預覽失敗或有其他所選缺口時，不省略必要覆蓋。

### 批次契約註冊

同輪已知契約可放在 UTF-8 JSON 陣列，例如 `<context>/contracts.json`；各項沿用下方「多儲存庫延伸」的契約物件，父契約必須先於子契約。執行：

~~~powershell
python "<skill-dir>/scripts/review_workspace.py" batch-register-contract --context-dir "<context>" --requests-file "<context>/contracts.json"
~~~

成功回傳 `{"contracts": [...]}`，每項與逐筆 `register-contract` 的結果相同，含固定 anchor hash。每個契約驗證成功即保存 manifest；首項錯誤停止後續項目，先前成功紀錄保留。批次命令失敗時 exit code 為 2、錯誤寫入 stderr、stdout 為空；空陣列成功回傳對應的空結果陣列。後續追查才發現的契約留到下一輪註冊。

## 單一儲存庫最小結果

停用延伸時不需要 `changed_contracts`、`impact_coverage` 或 `repository_coverage`，可使用一般固定搜尋。範例路徑與 SHA 必須換成實際資料，逐一列出所有 `required_coverage`：

~~~json
{
  "schema": "merge-reviewer-workspace-result/v3",
  "report_layout": "engineering-review/v1",
  "summary": "已檢查事件欄位變更與消費端，確認一項相容性問題。",
  "coverage": [{
    "repo": "repo-000", "source": "head", "path": "src/orders.py", "status": "reviewed",
    "evidence": [{
      "repo": "repo-000", "source": "head", "ref": "<固定完整提交 SHA>",
      "path": "src/orders.py", "line_start": 18, "line_end": 24
    }]
  }],
  "findings": [{
    "id": "F-001", "priority": "P1", "title": "事件金額欄位改名導致消費端拒絕資料",
    "impact": "既有消費端無法完成訂單結算。",
    "trigger": "publisher 改送 amountCents，但已固定的 validator 仍要求 totalCents。",
    "expected": "部署新 publisher 後既有消費端仍可處理事件。",
    "actual": "validator 因缺少 totalCents 拒絕新事件。",
    "operation_scenario": "使用者建立訂單後，背景結算服務接收 orders.created 事件並驗證金額。",
    "technical_trigger": "執行新 publisher 產生的事件會進入仍讀 totalCents 的驗證路徑。",
    "technical_explanation": "變更移除舊欄位，但消費端相容層尚未同步，缺值會觸發固定驗證分支。",
    "impact_scope": "本次訂單事件 publisher 與證據鏈中仍要求舊欄位的直接消費端。",
    "recommendation": "保留舊欄位別名並加入契約回歸測試。", "owner": "開發",
    "engineering_fix": "先讓事件同時提供 totalCents 與 amountCents；消費端升級後再移除別名。",
    "scenario_source": "code-derived",
    "evidence": [{
      "repo": "repo-000", "source": "head", "ref": "<固定完整提交 SHA>",
      "path": "src/orders.py", "line_start": 18, "line_end": 24
    }]
  }],
  "next_steps": [],
  "tests_executed": [],
  "tests_not_executed": ["未執行專案測試；本次為靜態審查。"],
  "limitations": []
}
~~~

這是有問題時的格式示意，必須替換為實際結論與固定證據；沒有已確認問題時使用 `findings: []`。本次新草稿一律帶 `report_layout="engineering-review/v1"`，每項 finding 必填 `operation_scenario`、`technical_trigger`、`technical_explanation`、`impact_scope`、`engineering_fix`，均須為具體非空文字，不能填「未提供」。標記或欄位不完整是可修正草稿錯誤；修正後沿用原快照重試。沒有標記的舊 v3 草稿仍可發布，缺少的工程欄位顯示「未提供」，不因此重取證據。

`coverage` 恰好涵蓋每個 `(repo, source, path)`，含改名舊路徑及合併預覽。`reviewed` 需固定證據；`metadata-only`／`not-reviewed` 需原因並使結果未完成。二進位檔案／子模組不能僅靠中繼資料宣稱語意審查完整。

證據來源是 `manifest.json` 列出的 `head`、`base`、`merge-base`、`merge-preview` 或 `parent-<SHA>`。刪除的 `head` 路徑引用 `diff_base` 對應的來源；刪除的合併預覽路徑引用 `head`。來源、參照、路徑及行號一致，不用另一儲存庫的同名檔案替代。

## 多儲存庫延伸

同一份結果另加 `impact_coverage` 與 `repository_coverage`，追查必要的直接／間接影響；其他儲存庫不成為第二起點。

1. 從實際變更行擷取 API、事件、資料、套件、設定或行為契約。每個已審查的變更路徑需固定契約；無法理解的路徑保留未完成。
2. 契約 JSON 包含 `id`、`root`（一筆 `required_coverage`）、`kind=api|event|package|data|table|config|behavior`、`symbol`、`evidence`、`search_terms`（`query`／`reason`）。初次註冊即選至少兩個不同且在固定 anchor 中出現的契約詞，包含精確 symbol 與相關欄位／Topic／API 名稱，供無關 Repo 的替代詞排除。必要時擴至最小相關行段；別名需自己的固定證據（`evidence`）。註冊後以契約 ID 搜尋，所有紀錄由 helper 保存，不直接修改 manifest 或搜尋 ledger。
3. 閱讀呼叫端／消費端，發現延伸契約時加上 `parent_ids` 並繼續查找，直到必要鏈路查清或標為 `unresolved`。預覽契約使用合併預覽來源。
4. 每個儲存庫記錄 `seed|related|not-related|unresolved`、原因及 `search_ids`。每個跨儲存庫契約都要搜尋候選的固定主來源；排除需至少兩個不同且相關的詞，解釋符合／不符合結果。
5. 功能鏈節點包含 `id`、`repo`、`source`、`path`、`status=reviewed|not-reviewed`、`reason`、`contract_ids`、`related_to`、`evidence`。變更覆蓋紀錄（`coverage`）的 `impact_ids` 連到自己的固定根，涵蓋直接註冊的變更契約；延伸契約在自身來源節點引用證據，不需回填成來源檔案的契約。所有節點從變更根可達，循環去重。
6. 必要外部消費端缺失時，在相關節點留下 `not-reviewed` 與原因，不捏造儲存庫 ID。必要來源、節點或儲存庫未完成時，整體仍為「審查未完成」。

以下是可寫入 `<context>/contract.json` 的完整最小契約。假設 `required_coverage` 含此 `root`，固定 `head` 的第 18–24 行包含 `totalCents` 與 `orderId`，且 anchor 與實際變更行重疊；依實際快照替換路徑、來源、ref 及行段。`evidence` 是單一物件，不能用陣列；`root` 恰含與必要覆蓋相同的 `repo`／`source`／`path`。

~~~json
{
  "id": "C-001",
  "root": {"repo": "repo-000", "source": "head", "path": "src/orders.py"},
  "kind": "event",
  "symbol": "totalCents",
  "evidence": {
    "repo": "repo-000", "source": "head", "ref": "<固定完整提交 SHA>",
    "path": "src/orders.py", "line_start": 18, "line_end": 24
  },
  "search_terms": [
    {"query": "totalCents", "reason": "本次變更的事件金額欄位。"},
    {"query": "orderId", "reason": "同一事件的訂單識別欄位，用於核對消費端契約。"}
  ]
}
~~~

~~~powershell
python "<skill-dir>/scripts/review_workspace.py" register-contract --context-dir "<context>" --contract-file "<context>/contract.json"
python "<skill-dir>/scripts/review_workspace.py" search --context-dir "<context>" --contract C-001 --query totalCents
python "<skill-dir>/scripts/review_workspace.py" read --context-dir "<context>" --repo repo-001 --source head --path src/client.py
~~~

準備、搜尋與影響圖驗證不能替代 Codex 實際審查。單一儲存庫停用延伸時，不註冊這些紀錄。

## 問題與發布

問題紀錄包含 `id`（F-001 起連續編號）、原生 `priority`（P0–P3 順序）、`title`、`impact`、`trigger`、`expected`、`actual`、`recommendation`、`owner`（開發／維運設定／QA）、`scenario_source`（`code-derived`／`reproduced`）及 `evidence`。選填非空文字 `operation_scenario`（操作情境）、`technical_trigger`（技術觸發方式）、`technical_explanation`（證據說明）、`impact_scope`（影響範圍）、`evidence_summary`（證據補充）及 `engineering_fix`（工程修正建議）。至少一條證據連到已閱讀的所選儲存庫變更，其餘可包含既有消費端。

v3 必填欄位、`engineering-review/v1` 完整性驗證與 CLI 維持相容。新舊草稿均使用[報告風格](report-style.md#報告與聊天結果)的六區塊版面，整合既有影響、操作情境、觸發、證據說明、影響範圍、工程修正建議及固定片段。無標記的舊草稿缺少工程欄位時仍顯示「未提供」，不得推造內容。預期與實際結果、責任角色及證據補充保留。`code-derived` 只能標為依程式推導，`reproduced` 須有實際執行紀錄；每項只列一次重現狀態標示。

每條非空文字問題證據必填 `line_start`／`line_end`，以 1 起算且包含兩端；選取能證明問題的最小必要行段，不附無關上下文。發布器依 Repo、來源、完整 ref、路徑及行號驗證並擷取固定內容，草稿不必提供程式碼片段。固定版本為空檔案時，兩個行號可同時省略，報告標示「固定版本內容為空（0 行）」；必要時另引用基礎版本的相關行段。一般覆蓋紀錄仍沿用既有證據規則。

`next_steps` 可空，最多三項 `owner`／`action`。`limitations` 記錄必要缺口；未執行測試放在 `tests_not_executed`。問題不因其他證據缺口而丟棄。

### 待人工確認

`pending_confirmations` 為選填陣列，省略等同 `[]`。只用於固定證據已指出的實際必要 caller／consumer／契約，其具體未知條件會影響本次變更判斷。僅缺任意外部消費端清單或假設 strict schema 不構成待確認／必要缺口，也不填入造成未完成的 draft `limitations`；固定工作區以外只是範圍界線。待確認不是已確認缺陷，也不接受 `priority`、`confirmed` 或草稿中的人工簽核。每項必填 `id`（Q-001 起連續編號）、`title`、`reason`、`missing_evidence`、`verification`（人工應執行的核對步驟文字）、`owner`（開發／維運設定／QA）及 `related_changes`（至少一筆恰含 `repo`、`source`、`path` 的必要變更覆蓋鍵）。以下僅為已有必要消費端證據時的格式示意：

~~~json
{
  "id": "Q-001",
  "title": "確認已登錄 BillingWorker 的實際欄位驗證模式",
  "reason": "固定 consumer 註冊程式明列 BillingWorker 為必要消費端，且欄位驗證模式由部署值決定；本次新增欄位在 strict 模式下的處理仍缺實際部署證據。",
  "missing_evidence": "已登錄 BillingWorker 的部署驗證模式與此變更的契約測試紀錄。",
  "verification": "請該消費端負責人核對實際模式，並以本次固定 payload 執行契約測試。",
  "owner": "QA",
  "related_changes": [{"repo": "repo-000", "source": "head", "path": "src/orders.py"}],
  "evidence": [{"repo": "repo-000", "source": "head", "ref": "<固定完整提交 SHA>", "path": "src/consumer_registry.py", "line_start": 5, "line_end": 8}]
}
~~~

`evidence` 選填且可空；提供時仍依固定 Repo／來源／ref／路徑及行號完整驗證。找不到證據不需虛構行號；錯誤引用不會自動降格成待確認。項目及關聯變更不可重複，不能指向本次必要變更以外的路徑。

待確認與 `findings` 分開計數，不計入 P0–P3，保留全部已確認問題及證據。沒有其他必要缺口時狀態為「待人工確認」、`review_complete=false`，建議為「需人工確認」；已確認 P0／P1 仍優先建議「暫緩合併」／「修正後再合併」。既有覆蓋或來源缺口仍為「審查未完成／需補做審查」。任何必要待確認項都不能產生無條件「可以合併」。

報告在固定問題後列出每個疑問、缺少證據與確認方式，另以 `merge-confirmations:start`／`merge-confirmations:end` 註解包住可編輯人工紀錄。checkbox 初始未勾選；人工填寫核對人、日期、確認結果與證據。只編輯此區，不改動上方固定判斷；勾選不會自動重算結果或證明目前分支已受審。

~~~powershell
python "<skill-dir>/scripts/review_report.py" --context-dir "<context>" --result "<context>/draft.json" --retain-on-validation-error
~~~

只發布一個 Markdown Merge Report，預設路徑是 `<所選Repo>/review-reports/merge-review-<時間戳>-<基礎分支commit前8碼>-<比較分支commit前8碼>.md`。時間戳採 UTC 發布時間及微秒；檔名提交使用固定的 `base`／`head` 分支頂端 SHA 前 8 碼。正文前段的「比較資訊」列出所選 Repo／本機路徑、兩個輸入分支與實際 ref、8 碼 commit、合併方向及主要差異範圍（共同祖先 → 比較分支）。問題證據附完整固定 ref；多儲存庫延伸證據整合於同一份報告。

新草稿的 Markdown 使用 engineering-review/v1 五個 H2，結論呈現 `review_status`、`priority_counts`；比較資訊與版本放技術紀錄、下一步放範圍下的 H3。舊無標記草稿保留原版面；限制中呈現 `extension_status=disabled|complete|incomplete`。固定版本、逐檔覆蓋、跨儲存庫判定、功能鏈、搜尋及契約完整驗證後保留於同一份報告末尾的 `<details>` 技術紀錄；必要缺口與人工核對區維持展開。下一步為空時明確顯示無額外行動。搜尋僅列識別、來源、契約、狀態與命中數，不複製原始命中內容。設定與私鑰片段省略、認證或連線行遮蔽；仍先完整驗證 ref／行號，再保留原始內容 SHA-256，不能以遮蔽掩蓋無效證據。發布器標準輸出只回傳完整報告路徑。

發布前核對快照的 owned 儲存、固定物件身分、指示、證據、覆蓋及固定搜尋。新的固定快照不再比較目前雲端／本機 refs、工作區宣告與 Repo 清單；owned 物件缺失或損壞不能回退到原 Repo。完整 Markdown 先寫入暫存，再原子發布到報告檔案；同名時遞增時間戳，不覆寫既有內容。預設成功、驗證失敗及寫入失敗均清理本次工作階段建立並由工具識別的證據目錄。

發布使用 `--retain-on-validation-error`：成功仍 exit 0、輸出報告路徑並清理；可修正的草稿錯誤 exit 3，stderr 回傳含 `context_dir`、`context_retained: true` 與 `error` 的 JSON，不發布報告，修正草稿後沿用該 context 重試。固定來源、快照、hash、指示、搜尋紀錄損毀及 I/O 錯誤仍清理並依原錯誤處理；中斷也不保留。終止錯誤停止本次審查並回報，不自動 capture 重跑。舊命令不加旗標時維持原本 exit 2 與清理行為。放棄重試或提前中止時用 `review_session.py cleanup`。
