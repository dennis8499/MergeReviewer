---
name: merge-reviewer
description: 使用 Codex 原生 Code Review，依明確指定的基礎分支與比較分支審查單一儲存庫。宣告工作區包含多個儲存庫時，只追查所選變更影響的契約與消費端，並發布繁體中文（zh-TW）的精簡 Merge Report。
metadata:
  short-description: 審查單一儲存庫的分支差異與相關影響
---

# Merge Reviewer（合併審查）

核心審查使用 Codex 原生 Code Review 判斷。本技能提供不可變的 Git 證據、依條件啟用的跨儲存庫背景資訊與報告流程；缺陷準則及優先程度定義沿用原生標準。套用相關根目錄及巢狀 `AGENTS.md` 的審查指示。報告格式正確或輔助程式執行成功，不能證明語意審查正確。

開始前讀取[輸出與報告寫作風格](references/report-style.md)，套用到進度、問答及新增或更新文件的措辭。

## 請求與邊界

以下三個業務輸入皆為必要：

- `專案名稱`：恰好一個儲存庫名稱或路徑（`--project`）。
- `基礎分支`：本機分支，或明確包含遠端名稱的分支（`--base`）。
- `比較分支`：本機分支，或明確包含遠端名稱的分支（`--head`）。

輸入缺漏或儲存庫無法唯一定位時，列出已找到的候選並請使用者補齊；不要自行推測分支。不支援標籤、提交 ID、`HEAD`、Stage、未提交內容、quick、直接比較、多起點審查、MR 任務或交付收據。遇到已移除的模式，說明支援的分支請求方式，不要默默轉換模式。

使用者提供 VS Code `.code-workspace` 時，使用該檔案的實際宣告。否則使用目前任務宣告的工作區根目錄，不向上搜尋無關的父目錄或相鄰資料夾。工作區與報告選項僅設定環境，不增加審查目標。需要安裝、範例、報告解讀或疑難排解時，讀取[詳細使用指南](references/usage.md)。

## 準備固定證據

~~~powershell
python "<skill-dir>/scripts/git_review_context.py" --workspace "<declared-root>" --project "<Repo-name-or-path>" --base "<base-branch>" --head "<comparison-branch>" --pretty
~~~

使用宣告的 VS Code 邊界時，加上 `--workspace-file <file.code-workspace>`。`--no-fetch` 僅適用於本機分支。比較範圍為 `merge-base(base, head) → head`，另包含合併父提交與合併預覽證據。包含遠端名稱的分支會精確同步；同步失敗時，不退回使用過期的參照。原始檔案、HEAD 與索引維持不變。

公開的證據與結果結構皆為 v3。以 `manifest.json` 為依據：`selected_repo` 識別唯一審查起點；只有宣告邊界包含至少兩個不同 Git 根目錄時，`extension_enabled` 才為 true。相關儲存庫使用擷取時的 HEAD，不套用審查起點的分支名稱或本機修改。

## 審查

讀取[原生審查與證據規則](references/review-rules.md)，了解原生審查與證據處理的關係；讀取[分支證據與 v3 結果契約](references/workspace-review.md)，了解固定來源讀取及結果契約。

閱讀完整差異與相關固定背景資訊，包含基礎版本及適用的 `AGENTS.md`。由原生 Code Review 判斷問題是否可修正及其 P0–P3 優先程度。即使其他證據不完整，仍保留已有證據支持的問題。

`extension_enabled=false` 時，正常審查所選儲存庫，不註冊跨儲存庫契約或要求影響圖。啟用時，沿著所選變更的契約、呼叫端與消費端追查已擷取的候選儲存庫。搜尋與圖的連線皆須綁定固定證據；排除無關候選時保留理由，必要鏈路無法查清時標示未完成。不要將無關儲存庫另列為獨立審查目標。

## 發布

遵循已載入的[寫作風格](references/report-style.md)。在本次工作階段建立並由工具識別的證據目錄內，由 Codex 依實際 v3 覆蓋情形撰寫 `draft.json`。輔助程式不能代替你產生語意審查結論。

報告與聊天結果使用繁體中文（zh-TW），採結論先行的精簡風格。若在 Claude Code 中套用相同輸出規範，請使用內建 `Concise`；啟用、確認及版本需求見 [Claude Code 使用 Concise](references/report-style.md#claude-code-使用-concise)。核心審查仍由 Codex 執行。

~~~powershell
python "<skill-dir>/scripts/review_report.py" --context-dir "<context>" --result "<context>/draft.json"
~~~

發布器驗證證據與覆蓋情形、計算完成程度及問題數量，並只寫入一份 `<selected-Repo>/review-reports/merge-review-<timestamp>-<base-commit前8碼>-<head-commit前8碼>.md`，標題為 `Merge Report: <Repo>`。使用含微秒的 UTC 發布時間戳（`YYYYMMDDTHHMMSSffffffZ`），以及擷取時兩個分支頂端 SHA 的固定前 8 碼，不使用共同祖先或目前取出的版本。僅發布 Markdown，並透過標準輸出回傳報告路徑。同名時保留既有報告。相關儲存庫的證據整合於同一份報告。

結論與問題數量後顯示「比較資訊」：Repo／本機路徑、基礎與比較分支的輸入名稱、實際 ref、8 碼 commit、比較分支到基礎分支的合併方向，以及共同祖先到比較分支的主要差異範圍。資訊來自已擷取的固定證據；完整 SHA 保留在技術附錄。

回覆精簡的繁體中文結果，包含有證據支持的問題、重要缺口與下一步，以及可點擊的報告連結。沒有找到有證據支持的問題時，明確說明；審查未完成不能當作通過。只有實際執行過專案測試，才能聲稱已執行。

## 清理與副作用

允許的副作用為同步明確指定的遠端分支、在系統暫存目錄建立本次工作階段的證據，以及寫入報告。不要修改原始碼或設定、切換版本、合併到實際分支、重設、暫存、提交、推送或發布留言。

發布成功與失敗時都會清理本次工作階段的證據目錄。若在發布前中止審查，只清理該工作階段：

~~~powershell
python "<skill-dir>/scripts/review_session.py" cleanup --context-dir "<context>"
~~~

發布或驗證失敗後，重新準備最新證據並重做審查。證據準備失敗時，回報確切錯誤與輔助程式建議的下一步。

## 範例

~~~text
$merge-reviewer 專案名稱=OrderService 基礎分支=main 比較分支=feature/payment
~~~
