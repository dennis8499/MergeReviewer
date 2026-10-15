# Claude Code 使用 Concise

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
