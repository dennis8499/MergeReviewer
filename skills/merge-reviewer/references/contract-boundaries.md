# 多 Repo 邊界判定

新 context 使用 `boundary-contracts/v2`。`boundary_changes` 為每筆 `required_coverage` 提供固定的 `CH-0001` 類識別；先完成逐檔 coverage，再為每筆必要變更在 `boundary_reviews` 填一筆判定。單 Repo 維持逐檔、必要呼叫端、merge-parent 與 merge-preview 審查，不要求邊界紀錄。舊 context 的 [v1 規則](contract-boundaries-v1.md)維持不變。

先讀固定變更、使用處及必要輸出，追查是否流入 API、event、共享資料、設定或套件。檔案類型、private 名稱或搜尋零命中不能單獨證明影響留在 Repo。判斷由 agent 作出，helper 驗證固定證據和關聯。

| outcome | 必要工作 |
| --- | --- |
| `contained` | 說明資料流，引用與 reviewed coverage 相同的固定變更證據，以及判斷所需使用／輸出內容；不需建立額外契約或影響圖。 |
| `propagates` | 註冊每個實際 workspace 契約，列入 `boundary_contract_ids`；以連通 impact 節點追查直接、間接消費端，按契約搜尋各候選 Repo。 |
| `unresolved` | 記錄具體缺口及已知證據；結果保持未完成。 |

每個其他 Repo 仍須有 disposition。沒有 workspace 契約且邊界確定為 contained 時，可根據已查清的資料流給 `not-related`、空搜尋清單；擷取失敗的 Repo 應保持 `unresolved`。有 workspace 契約時，排除應使用至少兩個有固定依據的不同詞並解釋結果；必要 consumer、來源或連通性缺失時保持未完成。

Repo 內搜尋可以用 change ID，預設只搜尋該變更所屬 Repo：

~~~json
{"id":"local-use","op":"search","change_id":"CH-0001","query":"displayOrder"}
~~~

跨 Repo 搜尋必須先註冊契約並使用 `contract`；每筆邊界記錄的 `search_ids` 僅引用同一 change ID 的已完成 Repo 內搜尋。必要搜尋和讀取由 agent 決定，不因空清單而自動推定已審查。

## 實際資料流範例

診斷畫面內部排序經使用處及輸出查核後沒有外部影響，可填 `contained`，不建立契約。將下列證據換成實際讀取操作與行號：

~~~json
{"change_id":"CH-0001","outcome":"contained","reason":"已讀取排序的使用處及畫面輸出；該值未流入傳輸或共享資料。","evidence":[{"read_id":"read-change","line_start":10,"line_end":20}],"search_ids":["S-0001"],"boundary_contract_ids":[]}
~~~

若內部 `calculatedAmount` 經 serializer 寫入 `orders.created.totalCents`，使用 `propagates`，登錄事件欄位 workspace 契約，並追查固定 relay／consumer。`boundary_contract_ids` 列出該變更所有 workspace 契約；每個契約的 anchor 及消費端節點保持連通。若 serializer mapping 尚未取得，使用 `unresolved` 並寫明缺少的證據；不可將未知當作 contained。

上述精簡引用用於 `merge-reviewer-authoring/v1`；完整 v3 草稿使用固定 `repo/source/ref/path/line_start/line_end` 證據。完成前使用 check 發現欄位與已知缺口；finish 從自足快照重新核對來源、行段與搜尋。
