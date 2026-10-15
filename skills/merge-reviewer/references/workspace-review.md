# 分支證據與 v3 結果契約

讀取此參考文件來準備固定來源、記錄實際覆蓋並發布報告。它不提供替代的語意審查判斷。

## 證據目錄與固定讀取

~~~powershell
python "<skill-dir>/scripts/git_review_context.py" --workspace "<root>" --project "<Repo>" --base main --head feature/payment --pretty
~~~

`schema` 為 `merge-reviewer-workspace-context/v3`；`selected_repo` 是唯一受審儲存庫 ID，`seed_repos` 必須只有此 ID。`extension_enabled` 由宣告邊界內的不同 Git 根目錄數決定，不能以 `scope` 參數覆寫。`scope`／`target` 是內部紀錄，固定為自動辨識的 `repo`／`group` 及 `branch`。

其他欄位：`workspace_roots`、`extension_reason`、`review_basis=codex-native`、`repositories`、`required_coverage`、`changed_contracts`、`searches`、`context_gaps`。所選儲存庫記錄兩個分支的固定版本、差異、合併父提交與合併預覽；其他儲存庫只擷取自身 HEAD。

~~~powershell
python "<skill-dir>/scripts/review_workspace.py" inventory --context-dir "<context>"
python "<skill-dir>/scripts/review_workspace.py" read --context-dir "<context>" --repo repo-000 --source head --path src/orders.py
python "<skill-dir>/scripts/review_workspace.py" search --context-dir "<context>" --repo repo-000 --query orderId
~~~

`inventory` 提供 ID、來源及路徑／檔案內容物件（blob）清單；`read` 提供參照（ref）、SHA-256 及逐行內容。完整差異的位置列於各儲存庫的 `patches`。閱讀所選儲存庫的差異、基礎內容、必要合併父提交差異與合併預覽，完成語意審查。

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

`next_steps` 可空，最多三項 `owner`／`action`。`limitations` 記錄必要缺口；未執行測試放在 `tests_not_executed`。問題不因其他證據缺口而丟棄。

~~~powershell
python "<skill-dir>/scripts/review_report.py" --context-dir "<context>" --result "<context>/draft.json"
~~~

只發布一個 Markdown Merge Report，預設路徑是 `<所選Repo>/review-reports/merge-review-<時間戳>-<基礎分支commit前8碼>-<比較分支commit前8碼>.md`。時間戳採 UTC 發布時間及微秒；檔名提交使用固定的 `base`／`head` 分支頂端 SHA 前 8 碼。結論與問題數量後的「比較資訊」列出所選 Repo／本機路徑、兩個輸入分支與實際 ref、8 碼 commit、合併方向及主要差異範圍（共同祖先 → 比較分支）；完整 SHA 留在技術附錄。多儲存庫延伸證據整合於同一份報告。內部驗證結果的 `extension_status=disabled|complete|incomplete`、`review_status`、`priority_counts`、固定版本及技術紀錄呈現在 Markdown，發布器標準輸出只回傳完整報告路徑。

發布前核對工作區宣告、儲存庫清單、所選分支參照、相關 HEAD 與固定搜尋；漂移使結果未完成。完整 Markdown 先寫入暫存，再原子發布到報告檔案；同名時遞增時間戳，不覆寫既有內容。成功、驗證失敗及寫入失敗均清理本次工作階段建立並由工具識別的證據目錄；提前中止用 `review_session.py cleanup`。
