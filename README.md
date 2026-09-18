# Merge Reviewer

`Merge Reviewer` 是一個用於審查 Git 分支或 commit 合併結果的 Codex skill。它不需要 checkout 任一版本，就能以 Git object 為基礎比較兩個已提交的版本，檢查合併整合可能遺失的驗證、授權、錯誤處理、設定或資料轉換邏輯，並產生繁體中文 Markdown 報告。

## 功能

- **合併前審查**：以 `merge-base(基礎版本, 比較版本)` 到比較版本的範圍，檢查即將帶入的變更。
- **直接比較**：直接比較指定的基礎版本與比較版本。
- **Merge commit 檢查**：逐一對照 merge commit 的各個 parent 與合併結果，確認任一側的必要邏輯沒有在解衝突時遺失。
- **證據導向 finding**：依 P0、P1、P2、P3 排序，記錄觸發條件、程式證據、影響與聚焦的修正方向。
- **多 repository 工作區支援**：可指定 repository 名稱或路徑，也能處理 VS Code `.code-workspace`。

## 需求環境

- Git，且基礎版本與比較版本可由本機 repository 或其 remote 解析。
- Python 3.10 以上，用於執行內附的 Git context helper。
- 可載入 Codex skill 的環境。

## 安裝

將 `skills/merge-reviewer` 複製或 symlink 到 Codex 的 skill 目錄：

```text
$CODEX_HOME/skills/merge-reviewer
```

若沒有設定 `CODEX_HOME`，使用預設位置：

```text
~/.codex/skills/merge-reviewer
```

PowerShell 複製範例：

```powershell
# CODEX_HOME 已設定時
Copy-Item -Recurse .\skills\merge-reviewer "$env:CODEX_HOME\skills\merge-reviewer"

# CODEX_HOME 未設定時
Copy-Item -Recurse .\skills\merge-reviewer "$HOME\.codex\skills\merge-reviewer"
```

安裝後可使用 `$merge-reviewer` 呼叫此 skill。

## 快速開始

指定專案、基礎分支與比較分支：

```text
$merge-reviewer 專案名稱=OrderService 基礎分支=main 比較分支=feature/payment
```

使用 commit，並改用直接比較：

```text
$merge-reviewer 基礎分支=abc123 比較分支=def456 比較模式=直接比較
```

### 輸入參數

| 參數 | 必要性 | 說明 |
| --- | --- | --- |
| `專案名稱` | 多 repository 時必要 | Repository 資料夾名稱或路徑。工作區只有一個 repository 時可省略。 |
| `基礎分支` | 必要 | 分支、remote ref、tag 或 commit。 |
| `比較分支` | 必要 | 要審查的分支、remote ref、tag 或 commit。 |
| `比較模式` | 選填 | `合併前審查`（預設）或 `直接比較`。 |

如果 repository、remote 或 ref 無法唯一解析，skill 會列出候選項目並要求選擇，不會自行猜測。

## 比較模式

| 模式 | 比較範圍 | 適用情境 |
| --- | --- | --- |
| `合併前審查` | `merge-base(基礎版本, 比較版本)` → `比較版本` | 檢查比較分支相對共同祖先新增或保留的行為。 |
| `直接比較` | `基礎版本` → `比較版本` | 需要精確檢查兩個指定版本之間的完整差異。 |

報告會記錄輸入 ref、解析後的完整 SHA、比較模式、merge base、fetch 結果，以及工作樹是否維持不變。

## 審查安全行為

審查過程使用 Git object 命令讀取版本內容，不會：

- checkout、merge、reset、stage 或修改來源分支與 commit；
- 將未提交的工作樹變更混入比較範圍；
- 自動修正程式碼、建立 commit、發布評論或推送變更。

為了確認 remote 分支不是過期版本，helper 可能執行 `git fetch --no-tags --no-prune`。除 fetch 外，唯一預期的寫入是產生審查報告。

## 報告輸出

報告會寫入被審查 repository 的：

```text
<repo>/review-reports/merge-review-<UTC-timestamp>-<base-short>-<head-short>.md
```

報告包含：

- 專案名稱、repository 路徑、輸入 ref 與解析後 SHA；
- 比較模式、merge base、fetch 狀態與工作樹狀態；
- 變更檔案摘要、binary／submodule 限制與檢查過的 merge commits；
- 依 P0 至 P3 排序的 findings；
- 本次靜態審查未執行的測試。

### 結果狀態

| 狀態 | 意義 |
| --- | --- |
| `沒有差異` | 版本與比較範圍有效，但沒有變更檔案。 |
| `未發現具體問題` | 可行的審查範圍已完整檢查，沒有足夠證據建立 finding；不代表程式絕對正確。 |
| `審查未完成` | ref、fetch、merge base、檔案讀取或上下文檢查存在明確缺口。 |

## 限制與錯誤處理

- Remote branch 的 fetch 失敗時會停止審查，不會靜默使用可能過期的 remote ref。
- 無效 ref、shallow history、沒有共同祖先或存在多個 merge base 時，審查會停止並回報原因。
- 分支同時存在於多個 remote 時，請使用明確的 ref，例如 `origin/release`。
- Binary、submodule、rename、copy 或 mode-only 變更會列為審查範圍限制，必要時需人工補充檢查。
- Merge Reviewer 是靜態審查工具；除非使用者明確要求且實際執行，報告不會宣稱測試已通過。
- 對 squash、rebase 或手動複製的變更，可以審查最終行為，但不會將問題歸因於人工合併。

## 手動執行 Git context helper

需要檢查準備資料或整合其他工具時，可直接執行 helper：

```powershell
python skills\merge-reviewer\scripts\git_review_context.py `
  --workspace . `
  --project OrderService `
  --base main `
  --head feature/payment `
  --mode merge `
  --format json --pretty
```

常用選項：

- `--workspace`：搜尋 repository 的工作區根目錄。
- `--workspace-file`：指定 VS Code `.code-workspace` 檔案。
- `--project`：指定 repository 名稱或路徑。
- `--base`、`--head`：指定兩個比較輸入。
- `--mode merge|direct`：選擇比較模式。
- `--no-fetch`：停用 remote fetch；只適合已確認本機 ref 最新的情境。
- `--context-dir`：將 manifest 與完整 patch 寫入新的暫存目錄。

Helper 會輸出固定版本 SHA、比較範圍、變更檔案、merge commit、diff 統計、fetch 結果與工作樹快照，供審查流程作為來源真相。

## 專案結構

```text
.
├── README.md
└── skills/
    └── merge-reviewer/
        ├── SKILL.md
        ├── agents/
        │   └── openai.yaml
        ├── references/
        │   └── review-rules.md
        └── scripts/
            └── git_review_context.py
```

## 相關文件

- [Merge Reviewer 技能說明](skills/merge-reviewer/SKILL.md)
- [審查規則與報告格式](skills/merge-reviewer/references/review-rules.md)
- [Git context helper](skills/merge-reviewer/scripts/git_review_context.py)
