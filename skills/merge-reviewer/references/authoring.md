# 精簡草稿

使用 `merge-reviewer-authoring/v1` 可減少重複欄位；既有 `merge-reviewer-workspace-result/v3` 草稿仍可使用。start 初始產生 v3 草稿；選用精簡格式時將 `schema` 改為 `merge-reviewer-authoring/v1`，保留其必要陣列，依實際審查填寫。

每筆 `coverage` 可用 start 回傳的 `boundary_changes[].id` 作 `change_id`，helper 展開為原有變更鍵。`evidence` 可使用 `{"read_id":"<step 操作 ID>","line_start":1,"line_end":3}`；行號以 1 起算且包含兩端，必須已包含在該次 `read` 結果內。若舊 checkpoint 沒有 read 操作種類，先以新 ID 重新讀取。helper 從已保存的操作補入固定 Repo、source、ref 和 path；finish 仍重讀固定物件、校驗來源及行號。

多 Repo 填 `boundary_reviews`、`repository_coverage`，影響跨 Repo 才依[契約邊界](contract-boundaries.md)建立所需 impact 節點。精簡格式的 impact 節點若省略 `repo/source/path`，從首筆 evidence 補入；必要 parent 的 `final_ref` 可省略，使用本 context 的固定最終來源。待確認的 `related_changes` 可使用 change ID。所有 status、理由、判斷、測試與必要缺口都由 agent 明確提供。

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
