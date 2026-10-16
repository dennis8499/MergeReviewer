# 分支證據與 v3 結果契約

讀取此參考文件來準備固定來源、記錄實際覆蓋並發布報告。它不提供替代的語意審查判斷。

## 證據目錄與固定讀取

~~~powershell
python "<skill-dir>/scripts/git_review_context.py" --workspace "<root>" --project "<Repo>" --base main --head feature/payment --summary --pretty
~~~

`--summary` 回傳 `merge-reviewer-workspace-summary/v1` 摘要，提供 `context_dir`、`manifest_path`、Repo／來源索引、必要覆蓋及缺口，省略完整檔案清單與 snapshot。`context_schema` 標示固定證據仍為 v3。完整 `manifest.json` 與證據照常保存，未加 `--summary` 時仍回傳完整 manifest。

磁碟上的 manifest `schema` 為 `merge-reviewer-workspace-context/v3`；`selected_repo` 是唯一受審儲存庫 ID，`seed_repos` 必須只有此 ID。`extension_enabled` 由宣告邊界內的不同 Git 根目錄數決定，不能以 `scope` 參數覆寫。`scope`／`target` 是內部紀錄，固定為自動辨識的 `repo`／`group` 及 `branch`。

其他欄位：`workspace_roots`、`extension_reason`、`review_basis=codex-native`、`repositories`、`required_coverage`、`changed_contracts`、`searches`、`context_gaps`。所選儲存庫記錄兩個分支的固定版本、差異、合併父提交與合併預覽；其他儲存庫只擷取自身 HEAD。

新 context 加上 `snapshot_policy=frozen-start/v1`、`capture_completed_at` 及 `workspace_instructions`。各成功擷取的 Repo 帶有 `snapshot`（`policy`、owned 相對 `git_dir`、`captured_at`、`object_format`）及固定 `instructions` 索引；所有來源的 `objects` 都指向該 owned Git 證據庫。普通非 thin pack 保存所需 commit、完整 tree/blob 及預覽物件，不借用原 Repo objects、不依賴原分支或後續 fetch。準備階段完成前仍需完整取得物件；完成後雲端／本機更新、force push、原 Repo GC 或工作區宣告變動不會替換或使這份快照失效。

快照紀錄的是準備階段選定的各個 SHA，跨 Repo 不宣稱同一瞬間的原子擷取。報告列出準備起迄時間並只適用於這次固定版本；不自動重審或補抓新版本。缺少 `snapshot_policy` 的舊 v3 context 維持原本發布前漂移驗證，不能視為自足快照。

~~~powershell
python "<skill-dir>/scripts/review_workspace.py" inventory --context-dir "<context>" --repo repo-000
python "<skill-dir>/scripts/review_workspace.py" read --context-dir "<context>" --repo repo-000 --source head --path src/orders.py
python "<skill-dir>/scripts/review_workspace.py" search --context-dir "<context>" --repo repo-000 --query orderId
python "<skill-dir>/scripts/review_workspace.py" instructions --context-dir "<context>" --repo repo-000 --path src/orders.py
~~~

`inventory` 提供 ID、來源及路徑／檔案內容物件（blob）清單；保留已取得的索引，需要定位檔案時才查對應 Repo，避免重讀完整清單。`read` 提供參照（ref）、SHA-256 及逐行內容。完整差異的位置列於各儲存庫的 `patches`。閱讀所選儲存庫的差異、基礎內容、必要合併父提交差異與合併預覽，完成語意審查。

`instructions` 回傳擷取時適用的工作區指示與該 Repo 固定主要來源中的 `AGENTS.md` 內容、原始路徑／來源及 SHA-256；省略 `--path` 時列出該 Repo 全部固定指示，指定時只列祖先路徑。工作區 Git 外指示複製到 owned context，Repo 指示來自固定 head；未提交的 Repo 指示不替代固定版本。後續修改不改本次背景，使用者／系統指示仍優先。base／preview 中的指示差異作為受審資料閱讀。

### 批次固定讀取與搜尋

將已知且互不相依的操作寫入本次 owned context 內的 UTF-8 JSON 陣列，依陣列順序處理。`batch-read` 每項使用 `repo`、`path` 及選填 `source`；省略來源沿用該 Repo 的固定主來源。例如 `<context>/reads.json`：

~~~json
[
  {"repo": "repo-000", "source": "head", "path": "src/orders.py"},
  {"repo": "repo-000", "source": "merge-base", "path": "src/orders.py"}
]
~~~

~~~powershell
python "<skill-dir>/scripts/review_workspace.py" batch-read --context-dir "<context>" --requests-file "<context>/reads.json"
~~~

回傳 `{"reads": [...]}`，各項保留原本 `read` 的結果格式。請求中的 ID、來源與路徑換成實際固定資料；僅在來源確實有該路徑時列入。

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

範例搜尋詞必須換成該契約已註冊的相關詞；停用延伸時可省略 `contract`。回傳 `{"searches": [...]}`，依查詢及 Repo 順序平展原本的搜尋紀錄。每個查詢完成即保存 manifest，搜尋 ID 按原有順序遞增。

同一 context 的搜尋及契約註冊只用單一寫入者，避免同時執行會修改 manifest 的命令。先註冊本輪所需契約及其父契約，再批次搜尋；閱讀命中內容後，將新發現的延伸契約另行註冊並進入下一輪。批次只合併工具往返，逐檔覆蓋、搜尋證據與直接／間接消費端追查要求不變。

## 單一儲存庫最小結果

停用延伸時不需要 `changed_contracts`、`impact_coverage` 或 `repository_coverage`，可使用一般固定搜尋。範例路徑與 SHA 必須換成實際資料，逐一列出所有 `required_coverage`：

~~~json
{
  "schema": "merge-reviewer-workspace-result/v3",
  "summary": "已檢查本次變更，未建立有證據的具體問題。",
  "coverage": [{
    "repo": "repo-000", "source": "head", "path": "src/orders.py", "status": "reviewed",
    "evidence": [{
      "repo": "repo-000", "source": "head", "ref": "<固定完整提交 SHA>",
      "path": "src/orders.py", "line_start": 18, "line_end": 24
    }]
  }],
  "findings": [],
  "next_steps": [],
  "tests_executed": [],
  "tests_not_executed": ["未執行專案測試；本次為靜態審查。"],
  "limitations": []
}
~~~

`coverage` 恰好涵蓋每個 `(repo, source, path)`，含改名舊路徑及合併預覽。`reviewed` 需固定證據；`metadata-only`／`not-reviewed` 需原因並使結果未完成。二進位檔案／子模組不能僅靠中繼資料宣稱語意審查完整。

證據來源是 `manifest.json` 列出的 `head`、`base`、`merge-base`、`merge-preview` 或 `parent-<SHA>`。刪除的 `head` 路徑引用 `diff_base` 對應的來源；刪除的合併預覽路徑引用 `head`。來源、參照、路徑及行號一致，不用另一儲存庫的同名檔案替代。

## 多儲存庫延伸

同一份結果另加 `impact_coverage` 與 `repository_coverage`，追查必要的直接／間接影響；其他儲存庫不成為第二起點。

1. 從實際變更行擷取 API、事件、資料、套件、設定或行為契約。每個已審查的變更路徑需固定契約；無法理解的路徑保留未完成。
2. 契約 JSON 包含 `id`、`root`（一筆 `required_coverage`）、`kind=api|event|package|data|table|config|behavior`、`symbol`、`evidence`、`search_terms`（`query`／`reason`）。搜尋詞出現在固定變更證據；別名需自己的固定證據（`evidence`）。註冊後以契約 ID 搜尋。
3. 閱讀呼叫端／消費端，發現延伸契約時加上 `parent_ids` 並繼續查找，直到必要鏈路查清或標為 `unresolved`。預覽契約使用合併預覽來源。
4. 每個儲存庫記錄 `seed|related|not-related|unresolved`、原因及 `search_ids`。每個跨儲存庫契約都要搜尋候選的固定主來源；排除需至少兩個不同且相關的詞，解釋符合／不符合結果。
5. 功能鏈節點包含 `id`、`repo`、`source`、`path`、`status=reviewed|not-reviewed`、`reason`、`contract_ids`、`related_to`、`evidence`。變更覆蓋紀錄（`coverage`）的 `impact_ids` 連到自己的固定根，涵蓋直接註冊的變更契約；延伸契約在自身來源節點引用證據，不需回填成來源檔案的契約。所有節點從變更根可達，循環去重。
6. 必要外部消費端缺失時，在相關節點留下 `not-reviewed` 與原因，不捏造儲存庫 ID。必要來源、節點或儲存庫未完成時，整體仍為「審查未完成」。

~~~powershell
python "<skill-dir>/scripts/review_workspace.py" register-contract --context-dir "<context>" --contract-file "<context>/contract.json"
python "<skill-dir>/scripts/review_workspace.py" search --context-dir "<context>" --contract C-001 --query totalCents
python "<skill-dir>/scripts/review_workspace.py" read --context-dir "<context>" --repo repo-001 --source head --path src/client.py
~~~

準備、搜尋與影響圖驗證不能替代 Codex 實際審查。單一儲存庫停用延伸時，不註冊這些紀錄。

## 問題與發布

問題紀錄包含 `id`（F-001 起連續編號）、原生 `priority`（P0–P3 順序）、`title`、`impact`、`trigger`、`expected`、`actual`、`recommendation`、`owner`（開發／維運設定／QA）、`scenario_source`（`code-derived`／`reproduced`）及 `evidence`。選填 `evidence_summary`／`engineering_fix`。至少一條證據連到已閱讀的所選儲存庫變更，其餘可包含既有消費端。

v3 輸入欄位維持上述名稱；報告的六欄依序對應 `impact`（問題描述）、`trigger` 與 `scenario_source`（重現原因）、`expected`（預期結果）、`actual`（實際結果）、`evidence` 與選填 `evidence_summary`（證據）、`recommendation`、`owner` 與選填 `engineering_fix`（建議改善方式）。`code-derived` 只能標為依程式推導，`reproduced` 須有實際執行紀錄。

每條非空文字問題證據必填 `line_start`／`line_end`，以 1 起算且包含兩端；選取能證明問題的最小必要行段，不附無關上下文。發布器依 Repo、來源、完整 ref、路徑及行號驗證並擷取固定內容，草稿不必提供程式碼片段。固定版本為空檔案時，兩個行號可同時省略，報告標示「固定版本內容為空（0 行）」；必要時另引用基礎版本的相關行段。一般覆蓋紀錄仍沿用既有證據規則。

`next_steps` 可空，最多三項 `owner`／`action`。`limitations` 記錄必要缺口；未執行測試放在 `tests_not_executed`。問題不因其他證據缺口而丟棄。

### 待人工確認

`pending_confirmations` 為選填陣列，省略等同 `[]`。用於具體疑問尚無足夠證據驗證的情況，不是已確認缺陷，也不接受 `priority`、`confirmed` 或草稿中的人工簽核。每項必填 `id`（Q-001 起連續編號）、`title`、`reason`、`missing_evidence`、`verification`（人工應執行的核對步驟文字）、`owner`（開發／維運設定／QA）及 `related_changes`（至少一筆恰含 `repo`、`source`、`path` 的必要變更覆蓋鍵）。例如：

~~~json
{
  "id": "Q-001",
  "title": "確認外部消費端是否支援新格式",
  "reason": "工作區沒有部署中的外部消費端，無法確認相容性。",
  "missing_evidence": "消費端版本與契約測試紀錄。",
  "verification": "請服務負責人核對實際版本並提供契約測試結果。",
  "owner": "QA",
  "related_changes": [{"repo": "repo-000", "source": "head", "path": "src/orders.py"}],
  "evidence": []
}
~~~

`evidence` 選填且可空；提供時仍依固定 Repo／來源／ref／路徑及行號完整驗證。找不到證據不需虛構行號；錯誤引用不會自動降格成待確認。項目及關聯變更不可重複，不能指向本次必要變更以外的路徑。

待確認與 `findings` 分開計數，不計入 P0–P3，保留全部已確認問題及證據。沒有其他必要缺口時狀態為「待人工確認」、`review_complete=false`，建議為「需人工確認」；已確認 P0／P1 仍優先建議「暫緩合併」／「修正後再合併」。既有覆蓋或來源缺口仍為「審查未完成／需補做審查」。任何必要待確認項都不能產生無條件「可以合併」。

報告在固定問題後列出每個疑問、缺少證據與確認方式，另以 `merge-confirmations:start`／`merge-confirmations:end` 註解包住可編輯人工紀錄。checkbox 初始未勾選；人工填寫核對人、日期、確認結果與證據。只編輯此區，不改動上方固定判斷；勾選不會自動重算結果或證明目前分支已受審。

~~~powershell
python "<skill-dir>/scripts/review_report.py" --context-dir "<context>" --result "<context>/draft.json"
~~~

只發布一個 Markdown Merge Report，預設路徑是 `<所選Repo>/review-reports/merge-review-<時間戳>-<基礎分支commit前8碼>-<比較分支commit前8碼>.md`。時間戳採 UTC 發布時間及微秒；檔名提交使用固定的 `base`／`head` 分支頂端 SHA 前 8 碼。結論與問題數量後的「比較資訊」列出所選 Repo／本機路徑、兩個輸入分支與實際 ref、8 碼 commit、合併方向及主要差異範圍（共同祖先 → 比較分支）。問題證據附完整固定 ref；多儲存庫延伸證據整合於同一份報告。

Markdown 呈現 `extension_status=disabled|complete|incomplete`、`review_status`、`priority_counts` 對應的結論、問題及限制，不輸出技術附錄。固定版本、逐檔覆蓋、跨儲存庫判定、功能鏈、搜尋及契約仍完整驗證；省略顯示不改變完成程度或問題數量。發布器標準輸出只回傳完整報告路徑。

發布前核對快照的 owned 儲存、固定物件身分、指示、證據、覆蓋及固定搜尋。新的固定快照不再比較目前雲端／本機 refs、工作區宣告與 Repo 清單；owned 物件缺失或損壞不能回退到原 Repo。完整 Markdown 先寫入暫存，再原子發布到報告檔案；同名時遞增時間戳，不覆寫既有內容。成功、驗證失敗及寫入失敗均清理本次工作階段建立並由工具識別的證據目錄；提前中止用 `review_session.py cleanup`。
