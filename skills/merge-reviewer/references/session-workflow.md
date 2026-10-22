# 三階段操作

`review_session.py start／step／finish` 編排既有 Git、固定讀取、契約、搜尋及發布實作。以下 runtime、id、context 沿用 start；Repo ID、來源、path 及 SHA 使用實際固定索引。JSON 以 UTF-8 寫檔。PowerShell 管線傳 Python 前設定 `$OutputEncoding = [System.Text.UTF8Encoding]::new($false)`。

## 開始與恢復

~~~powershell
python "<skill-dir>/scripts/review_session.py" start --workspace "C:/team" --project Orders --base origin/main --head origin/feature/payment
python "<skill-dir>/scripts/review_session.py" status --runtime-root "C:/team/.review-runtime" --request-id "<id>"
~~~

start 回傳 `MergeReviewStart/v1`，含固定 Repo／source ref、必要覆蓋及 parent、指示、patch、草稿與操作範例。`<context>/start.json` 保存完整索引；status 補上已執行操作、契約及草稿尚未完成項。stdout 超過 24 KiB 時回傳 `MergeReviewEvidenceSummary/v1`，保留完整 artifact；stderr 為階段事件。

可自行產生 32 位小寫十六進位 request ID，透過 `start --request-id` 在輸出遺失後重送。相同 ID／輸入回傳既有狀態，不重新擷取；改變輸入須新 ID。`--refresh` 忽略已完成快取及失敗冷卻，仍可加入開始等待後完成的同需求同步。

成功 refresh 後，新請求優先採最新同步結果。已知 SHA 改變或同需求同步失敗的舊收據不再重用；冷卻結束會重新同步。這些失效標記不改動已固定的審查。

單 Repo 根目錄加 `--runtime-root "C:/review-runtime/team"`；也可設定 `MERGE_REVIEW_RUNTIME_DIR`。必須可寫且位於所有宣告 Repo 外，拒絕 symlink／junction 或未標記的非空目錄。Git 共用網路 runtime 沿用 `GITLAB_WORKSPACE_RUNTIME_DIR`，兩個 runtime 用途不同，start 都會預檢。

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

較大的 step 優先使用 `MergeReviewEvidenceStep/v1`：read 的 `content_id` 指向頂層 `contents[id].lines`，每筆保留自己的來源、完整 ref、路徑及全文 hash；搜尋的共同 query／contract_id／source 移到該操作 result，個別紀錄沿用，所有 matches 保留。只省略與 matches 重複的原始 output，以及已由請求提供的契約欄位；契約回傳 id／parent_ids／anchor_sha256 與 `input_fields_reused=true`。`projection_complete=true` 代表必要行段及命中全部回傳，原始完整結果仍由 artifact 保存。超過上限才用摘要並要求分頁。

同輪註冊後搜尋，再閱讀 consumer；實際發現間接輸出契約才建立下一輪。單 Repo 搜尋可省略 contract，跨 Repo 須先註冊。

## artifact 續讀

~~~json
[
  {"id":"page-1","op":"view","artifact":"<SHA-256>","pointer":"/operations","offset":0,"page_size":20},
  {"id":"matches-1","op":"view","artifact":"<operation artifact>","pointer":"/searches/0/matches","offset":0,"page_size":100}
]
~~~

JSON pointer 的 `/`、`~` 以 `~1`、`~0` 轉義。陣列回傳 items／total／next_offset／view_complete；長字串用 string_offset，依 next_string_offset 續讀。字典由 fields 選 pointer。操作 artifact 保存原始 result；外層精簡摘要 artifact 保存完整回傳，兩者 pointer 不同。artifact 位於 `<context>/views/<SHA-256>.json`，helper 讀取核對 hash。

24 KiB 限制傳輸，不取代語意覆蓋。必要命中及指示讀完才能判斷；用 artifact 翻頁。

## 完成、取消與錯誤

草稿初始 summary 空、必要覆蓋與 parent 為 not-reviewed，其他 Repo 為 unresolved。agent 依實際工作補齊，不能直接批次改為 reviewed。finish 使用同一套 v3 驗證器；exit 3 保留 context，exit 2 終止。JSON 格式錯誤可修，固定快照損毀屬終止。

~~~powershell
python "<skill-dir>/scripts/review_session.py" finish --runtime-root "<runtime>" --request-id "<id>"
python "<skill-dir>/scripts/review_session.py" cancel --runtime-root "<runtime>" --request-id "<id>"
~~~

發布先保存預定路徑／hash，再原子建立報告，保存完成紀錄後清理。輸出遺失可重試 finish，回傳同一報告。取消等待者不影響其他請求；仍有等待者時先完成共用同步再取消自己的擷取。已發布不可取消。終止摘要保留 24 小時，之後新 start 清除；報告歷史保留。

相同需求失敗 30 秒內回報既有錯誤；明確 start --refresh 可重試。缺 refs 或同步失敗不採舊 tracking refs。固定證據交付後不回讀原 Repo 補救；capture 階段壞 peer 快取可從同一固定 SHA 本機重建。
