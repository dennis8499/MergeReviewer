# 精簡草稿

使用 `merge-reviewer-authoring/v1` 可減少重複欄位；既有 `merge-reviewer-workspace-result/v3` 草稿仍可使用。先用 `review_session.py capabilities` 確認安裝版支援 compact，再於 start 明確指定 `--draft-format compact`；較舊安裝或舊 session 保留 v3。精簡格式保留必要陣列，依實際審查填寫。

每筆 `coverage` 可用 start 回傳的 `boundary_changes[].id` 作 `change_id`，helper 展開為原有變更鍵。`evidence` 可使用 `{"read_id":"<step 操作 ID>","line_start":1,"line_end":3}`；行號以 1 起算且包含兩端，必須已包含在該次 `read` 結果內。若舊 checkpoint 沒有 read 操作種類，先以新 ID 重新讀取。helper 從已保存的操作補入固定 Repo、source、ref 和 path；finish 仍重讀固定物件、校驗來源及行號。

修稿時依 check 的 `errors[].pointer` 定位欄位。`read_id` 使用操作 ID；artifact hash、搜尋 ID 不是 read ID。引用未讀行段時，診斷列出 `read_id` 與 `available_line_ranges`；選已讀行段或以新操作補讀。找不到 read ID 時列出 `available_read_ids` 與總數。compact 與展開欄位衝突時，只保留一次工程事實，移除衝突的展開欄位；所有獨立衝突會一併回報。

多 Repo 填 `boundary_reviews`、`repository_coverage`，影響跨 Repo 才依[契約邊界](contract-boundaries.md)建立所需 impact 節點。精簡格式的 impact 節點若省略 `repo/source/path`，從首筆 evidence 補入；parent 模板會帶入固定最終來源的 `final_ref`。reviewed parent 必須同時引用對應 parent 與最終來源的必要路徑；`check` 和 `finish` 使用相同規則。待確認的 `related_changes` 可使用 change ID。所有 status、理由、判斷、測試與必要缺口都由 agent 明確提供。

完成一條鏈就保存並回讀草稿，再用 check 核對 `expected_outcome` 與 `blocking_gaps`。`limitations` 是必要證據缺口；未跑測試用 `tests_not_executed`，工作區外未宣告範圍等說明用選填 `scope_notes`。可讀的剩餘證據先補查，確實無法取得才發布未完成報告。JSON 請求始終序列化為陣列，單筆也保留外層 `[]`。

reviewed coverage 的既有 `reason` 缺值或 null 視為未提供文字，仍須有合法固定證據；布林、數字、陣列及物件為草稿型別錯誤。未完成 coverage 必須有非空理由。helper 不自動補造判斷理由。

純刪除變更可用比較左側實際包含該符號的固定來源作初始錨點；helper 會核對 anchor 路徑、ref、行號及本次 diff 的刪除行。若來源不是 UTF-8，helper 會標出 `encoding`；只有 `lossy=false` 的內容可作契約錨點。二進位讀取僅提供位元組大小與 SHA-256，應保留為 coverage 缺口並說明無法檢視內容。

每個 finding 寫一次情境、觸發、證據說明、預期與實際、影響及修法。範例的文字、操作 ID 及行號必須換成實際內容：

~~~json
{
  "id":"F-001", "priority":"P1", "title":"事件欄位變更使既有消費端失敗",
  "scenario":"建立訂單後處理事件", "trigger":"新版事件缺少舊欄位",
  "explanation":"固定的消費端驗證路徑仍要求舊欄位。",
  "expected":"既有消費端可處理事件", "actual":"驗證器拒絕事件",
  "impact":"既有訂單無法結算", "fix":"保留欄位別名並加入契約測試。",
  "owner":"開發", "scenario_source":"code-derived",
  "evidence":[{"read_id":"read-change","line_start":18,"line_end":24}]
}
~~~

helper 將 `scenario` 展開為 `operation_scenario`，`trigger` 同時用於 `technical_trigger`，`explanation` 對應 `technical_explanation`，`impact` 同時用於 `impact_scope`，`fix` 同時用於 `recommendation` 與 `engineering_fix`。這些欄位的具體內容仍須由 agent 提供；`code-derived` 與實際 `reproduced` 不可混用。`check` 和 `finish` 使用相同轉換，轉換後走完整 v3 驗證；helper 不自動標記 reviewed 或可以合併。

範例只描述格式；必須閱讀相關固定證據並填入實際 finding。完整 v3 欄位、測試、缺口與人工核對方式見 [v3 契約](workspace-review.md)。
