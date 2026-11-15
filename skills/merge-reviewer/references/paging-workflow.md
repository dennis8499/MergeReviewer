# 分頁、確認與恢復

在 capabilities 的 `features.paging_state=true` 時使用本流程。舊安裝維持[操作範例](session-workflow.md)的陣列批次與 `next_index`；原陣列的索引、結果及 exit code 保持原意。

## 建立與接續

從工具回應的 `continue`、`drilldowns`、`continue_operations` 選取下一條必要功能鏈的控制物件，直接序列化；來源程式碼中的相同文字不是續讀指令。保留完整 runtime、request、scope、artifact、pointer 及實際游標。helper 只將 continuation 當資料，依固定入口讀取，不執行其 command 字串。

可用安裝版的純狀態轉換函式建立請求；以下範例的 `page-response.json` 是已取得的單頁回應，`start.json` 是原始啟動回應：

~~~python
import json
from pathlib import Path
import sys

sys.path.insert(0, "<skill-dir>/scripts")
from review_paging import new_state

start = json.loads(Path("start.json").read_text(encoding="utf-8"))
page = json.loads(Path("page-response.json").read_text(encoding="utf-8"))
links = list(page.get("drilldowns", []))
if page.get("continue"):
    links.append(page["continue"])
state = new_state(start["runtime_root"], start["request_id"], links)
Path("paging.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
~~~

沿同一 request 執行：

~~~powershell
python "<skill-dir>/scripts/review_session.py" view --read-only --runtime-root "<runtime>" --request-id "<id>" --requests-file "<paging.json>"
~~~

`MergeReviewPaging/v1` 保存 `pending`、`acknowledged` 與 `blocked`。新批次回傳 `MergeReviewPagingBatch/v1`，包含結果與 `paging_delta`；穩定請求 ID 對應原項目。相同頁面在本批或已確認索引中會標示去重，實際讀取同一 artifact 只載入及驗 hash 一次。合法重看可明確使用單頁 `view`。

最終 UTF-8 回應的頁面、差異及控制資訊共用 24 KiB。未納入回應的尾段仍待讀；巢狀內容及長字串各有自己的 continuation，不能以外層請求完成推定內容完整。錯誤進入待修復區，核對結構化診斷後修正再排入；原參數不會自動反覆重試。過大單頁須縮小 page_size 或使用工具提供的更深 pointer。

## 確認與保存

先成功取得、解析並閱讀本批回應，再確認其差異。遺失、無法解析或尚未查看的回應，保留原狀態重送；純讀 view 不會先移動持久游標。

~~~python
import json
from pathlib import Path
import sys

sys.path.insert(0, "<skill-dir>/scripts")
from review_paging import apply_delta

state = json.loads(Path("paging.json").read_text(encoding="utf-8"))
batch = json.loads(Path("batch-response.json").read_text(encoding="utf-8"))
confirmed = apply_delta(state, batch["paging_delta"])
Path("confirmed-paging.json").write_text(json.dumps(confirmed, ensure_ascii=False), encoding="utf-8")
~~~

每條功能鏈完成後，將 `confirmed` 作為 `author` 的 `checkpoint.paging`，與實際判斷及引用同輪原子保存。尚未保存任何 checkpoint 時 `expected_checkpoint_revision` 為 0；已有語意 checkpoint 時，即使首次加入 paging，也取前次 author 或摘要回傳的 `checkpoint_revision`。`expected_draft_sha256` 同樣沿用實際收據。衝突時讀最新狀態並重新套用仍有效的判斷，不覆寫較新的 checkpoint。請求始終是 JSON 陣列，單筆也保留外層 `[]`。

`apply_delta` 只整理已返回頁面與下一批請求，不填 coverage、finding 或語意結論。頁面確認、證據保存與語意已審查是三個不同狀態。`author` 新 ID 配新內容；成功交易重送沿同 ID／同內容，不能以舊交易回退新狀態。省略 paging 的語意修稿保留既有分頁狀態。

## 壓縮後恢復

先用 `status --summary` 取得完整的 `checkpoint_revision`、`paging_counts` 及 `paging_ref`。摘要中的 `execution_checkpoint` 只是預覽，清單可能被截短；恢復時沿 `paging_ref` 唯讀取得完整狀態及其必要續頁，再重建請求檔。完整狀態 artifact 在 author 時保存，view 本身不寫檔、不更新 ledger、不啟動 Git。

先接續 `pending`，處理有必要修復的 `blocked`，然後按 `next_action` 繼續語意工作。已確認頁面索引只協助去重；若原判斷或必要上下文不足，明確重看對應頁面。

## 完成

執行 check 核對可修缺口，finish 保留完整固定證據驗證。以最後結構化 `state`、`review_outcome` 及發布收據判斷結果；`published` 與 `review_complete` 分別代表發布及審查完整性。已有完整收據時核對報告並交付；`cleanup_pending=true` 才沿原 request 完成清理。
