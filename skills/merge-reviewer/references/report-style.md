---
name: Merge Report concise
description: 結果先行的繁體中文（zh-TW）精簡審查報告
---

# Merge Report 寫作風格

本文件定義報告與聊天結果的寫作規範，語言統一為繁體中文（zh-TW）。核心審查與 P0–P3 優先程度仍採 Codex 原生 Code Review。

## Claude Code 使用 Concise

在 Claude Code 中套用這份輸出規範時，請明確選用內建的 `Concise` 輸出風格。依 [Claude Code 輸出風格官方文件](https://code.claude.com/docs/en/output-styles)，內建 Concise 需要 **v2.1.237 以上**；以下 `/output-style` 指令需要 **v2.1.269 以上**，請先切換，再確認目前樣式：

~~~text
/output-style concise
/output-style
~~~

第一個指令切換為 Concise；第二個指令列出可選樣式並標示目前樣式。終端機也可執行 `/config`，在 `Output style` 選單中選擇 `Concise`。已支援 Concise 但尚未支援 `/output-style` 的版本，可使用該選單或設定檔；低於 v2.1.237 的版本需先升級。

也可由使用者在專案 `.claude/settings.local.json` 的既有設定中加入以下欄位，保留其他設定：

~~~json
{
  "outputStyle": "Concise"
}
~~~

設定檔的值區分大小寫，必須寫成 `Concise`；寫成 `concise` 會退回 Default。`/output-style` 指令的樣式參數不區分大小寫。

切換會影響整個 Claude Code 工作階段；指令與選單會將選擇儲存至專案的 `.claude/settings.local.json`。v2.1.251 以上從下一則訊息生效；較舊版本需執行 `/clear` 或開始新工作階段。本技能僅提供 Claude Code 設定指引；在 Codex 中直接遵循下列寫作規範，由 Codex 完成核心審查。

## 報告與聊天結果

- 第一段直接給合併建議、審查狀態與一句具體結論，省略開場、操作流水帳及收尾重述。
- 用繁體中文（zh-TW）與日常用語說明影響；必要的 API、欄位及檔案名稱可保留，幫助定位問題。
- 問題依原生 P0–P3 排序，全數保留。每項說明影響、觸發、預期／實際差異、修正方向與可查證位置。
- 沒有問題時直接說明；不填虛構情境、空問題表或空下一步區段。下一步只列實際需要的行動，最多三項。
- 明確說明必要缺口及未執行的檢查。靜態推導不得寫成已重現、測試通過或部署安全。
- 完整 SHA、逐檔覆蓋、跨儲存庫判定、功能鏈及搜尋紀錄放在技術附錄，不重複搬到正文。
- 完整程度、問題數量、優先程度及證據來自同一份驗證結果；不能為了縮短報告刪掉問題或限制。
- 精簡只縮短敘述，仍完整保留錯誤資訊、失敗測試輸出、安全警告與破壞性操作確認內容；使用者要求解釋或更多細節時，提供完整說明。

順序：結論 → 有問題時的詳情 → 範圍與限制 → 有行動時的下一步 → 技術附錄。標題為 `Merge Report: <Repo>`，聊天回覆另附報告連結。報告僅輸出 Markdown，檔名為 `merge-review-<時間戳>-<基礎分支commit>-<比較分支commit>.md`，使用 UTC 發布時間（含微秒）及兩個分支的固定完整 SHA。
