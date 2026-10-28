# 多 Repo 契約邊界

本文件適用既有 `contract_scope_policy="boundary-contracts/v1"` context；新 context 依 [v2 規則](contract-boundaries.md)審查。更早的 v3 context 未帶政策標記時，維持每個契約對所有候選 Repo 的原有搜尋義務。未知政策停止。契約省略 `search_scope` 時保守視為 `workspace`。

多 Repo 延伸時，每個實際變更仍需 anchored contract、逐檔 coverage 及連通 impact node。分流限制搜尋哪些名稱，不替代行為審查。先閱讀定義、使用處、包裝及輸出路徑，再判斷資料會否到 API payload、event、共享 table/file/cache、設定或套件邊界。無法確認時保留 unresolved。單 Repo context 即使帶政策標記，仍沿用既有 v3 單 Repo 規則：逐檔固定 coverage 與來源證據，不啟用 Group 的跨 Repo 契約／影響圖義務。

| 契約 | 搜尋義務 | 完成判定 |
| --- | --- | --- |
| `repo-local` | exact symbol 搜自身 evidence Repo／source；預設自動限自身 Repo | boundary review 交代使用處及操作結果，附固定 anchor 與必要上下文 |
| `workspace` | 對其他 Repo 固定主來源搜尋邊界 symbol 及相關別名 | 保留每 Repo 實際相關性；排除使用至少兩個不同且有固定依據的詞 |

`boundary_reviews` 是草稿陣列，每個 repo-local contract 恰一筆。`outcome` 為 contained、propagates 或 unresolved；`reason` 說明資料流，`evidence` 沿用固定證據物件陣列，`search_ids` 明確引用該 local contract 的本 Repo exact-symbol 搜尋。已判定的證據須含 local anchor 來源／路徑，且屬同一 Repo；另補足使用處與 enclosing operation／輸出的必要片段。helper 核對固定證據及結構，資料流判斷由 agent 負責。

## 例一：只改內部排序

變更 `displayOrder`，自身 Repo 搜尋已讀完所有相關使用處，結果僅改內部診斷畫面的排列，未改傳輸／持久資料契約。登錄 `search_scope="repo-local"`，其 exact-symbol 搜尋可省略 repo（helper 自動限自身 Repo）。草稿記錄：

~~~json
{"contract_id":"C-local","outcome":"contained","reason":"已對照 displayOrder 的使用處與診斷呈現結果；未傳入 API、事件或共享資料寫入。","evidence":[{"repo":"repo-000","source":"head","ref":"<固定 SHA>","path":"src/diagnostics.py","line_start":10,"line_end":35}],"search_ids":["S-0001"],"boundary_contract_ids":[]}
~~~

每個其他 Repo 仍有 disposition。若本次全部契約均 local 且已取得有效邊界判定，可填 not-related、空 search_ids，理由引用已查清的本 Repo 邊界；報告明確記錄未做跨 Repo 名稱搜尋。其他 Repo 擷取失敗仍保留 unresolved。

## 例二：內部變數改變事件金額

`calculatedAmount` 雖是 private/local，卻寫入 `orders.created.totalCents`。local 契約保留自身搜尋，另登錄 `search_scope="workspace"` 的事件／欄位子契約，以 `parent_ids:["C-local"]` 連回本次變更，引用 serializer 的固定行段。按 workspace 子契約搜尋 producer／wrapper／consumer；只查內部變數不足以完成。

~~~json
{"contract_id":"C-local","outcome":"propagates","reason":"calculatedAmount 經 serializer 決定 orders.created 的 totalCents；後續以事件欄位追查固定 consumer。","evidence":[{"repo":"repo-000","source":"head","ref":"<固定 SHA>","path":"src/orders.py","line_start":10,"line_end":45}],"search_ids":["S-0001"],"boundary_contract_ids":["C-event"]}
~~~

列出的 boundary IDs 必須是已登錄的 workspace 後代，擁有與 local 節點連通的固定 impact anchor；每個 workspace 後代都需對帳。contained 不能同時藏有 workspace 後代或跨 Repo 的 local impact node。

## 例三：動態 mapping 尚未查清

local 名稱會交給動態 serializer，但缺少本次判斷必要的 mapping。使用 `outcome="unresolved"`、具體 reason，保留已知 evidence；`boundary_contract_ids:[]`。整體維持未完成，留下需要取得的固定來源，沿同一 ID 繼續。未知不是 contained，也不憑空建立外部 consumer。

填完用 check 查看欄位／搜尋／邊界缺口，再送 finish。check 僅檢查已保存 JSON 與固定索引；finish 重新從固定物件核對來源、引用及搜尋，不採 step 的持久搜尋快取當最終證據。
