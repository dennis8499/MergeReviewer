# 多 Repo 邊界判定

新 context 使用 `boundary-contracts/v2`。`boundary_changes` 為每筆 `required_coverage` 提供固定的 `CH-0001` 類識別；先完成逐檔 coverage，再為每筆必要變更在 `boundary_reviews` 填一筆判定。單 Repo 維持逐檔、必要呼叫端、merge-parent 與 merge-preview 審查，不要求邊界紀錄。舊 context 的 [v1 規則](contract-boundaries-v1.md)維持不變。

## 先定範圍

讀完固定變更、Repo 內使用處與必要輸出後，為每筆新契約明確指定 `search_scope`。`CONTRACT.md` 檔名、public／top-level 函式、`export` 字樣或未知 caller 的可能性，單獨都不證明跨 Repo 傳播。

| `search_scope` | 使用條件 | 搜尋義務 |
| --- | --- | --- |
| `repo-local` | 固定資料流留在受審 Repo，沒有證據指出實際外部消費端。 | 只搜尋契約所在 Repo；不搜尋其他 Repo。 |
| `identified-repos` | 固定證據點名或映射到特定已擷取 Repo 的消費端。 | `candidate_repos` 列出這些 Repo ID；以至少兩個固定契約詞搜尋每個列出的 Repo。其他 Repo 不搜尋。 |
| `workspace` | 固定證據明示契約適用於整個宣告工作區，或使用者明確要求工作區全域審查。 | 搜尋每個候選 Repo；排除使用至少兩個有固定依據的不同詞。 |

若固定契約指出必要消費端存在，但實作或 Repo 身分未提供，使用 `identified-repos`、空的 `candidate_repos`，並以 `unresolved` 記錄具體缺口。不要將單一已知消費端擴大為全工作區搜尋，也不要把未提供的必要來源當成無影響。

## 邊界結果

判斷由 agent 作出，helper 驗證固定證據、搜尋綁定及 impact 關聯。

| outcome | 必要工作 |
| --- | --- |
| `contained` | 說明資料流，引用與 reviewed coverage 相同的固定變更證據，以及判斷所需使用／輸出內容；不需建立額外契約或影響圖。 |
| `propagates` | 註冊每個實際跨 Repo 契約，列入 `boundary_contract_ids`；以連通 impact 節點追查直接、間接消費端，依 `search_scope` 搜尋指定 Repo。 |
| `unresolved` | 記錄具體缺口及已知證據；結果保持未完成。 |

每個其他 Repo 仍須有 disposition。`repo-local` 契約下，未被固定證據連結的 Repo 可標 `not-related` 並留空搜尋清單；`identified-repos` 只要求搜尋 `candidate_repos`，其他 Repo 以具體範圍理由標 `not-related`；`workspace` 契約才要求檢查每個候選 Repo。必要 consumer、來源或連通性缺失時保持未完成。

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

若固定 `CONTRACT.md` 指出 `reports-consumer` 讀取 response 的 `amount` 欄位，將其 Repo ID 放入 `candidate_repos`，以 `identified-repos` 搜尋該 Repo；不要把其餘未被連結的 Repo 加入搜尋範圍。

上述精簡引用用於 `merge-reviewer-authoring/v1`；完整 v3 草稿使用固定 `repo/source/ref/path/line_start/line_end` 證據。完成前使用 check 發現欄位與已知缺口；finish 從自足快照重新核對來源、行段與搜尋。
