# Merge Reviewer

`Merge Reviewer` 是一個用於審查 Git 分支或 commit 合併結果的 Codex skill。它不需要 checkout 任一版本，就能以 Git object 為基礎比較兩個已提交的版本，或快速比較目前本地分支與遠端主分支，檢查合併整合可能遺失的驗證、授權、錯誤處理、設定或資料轉換邏輯，並產生繁體中文 Markdown 報告。

## 功能

- **合併前審查**：以 `merge-base(基礎版本, 比較版本)` 到比較版本的範圍，檢查即將帶入的變更。
- **直接比較**：直接比較指定的基礎版本與比較版本。
- **Merge commit 檢查**：逐一對照 merge commit 的各個 parent 與合併結果，確認任一側的必要邏輯沒有在解衝突時遺失。
- **證據導向 finding**：依 P0、P1、P2、P3 排序，記錄觸發條件、程式證據、影響與聚焦的修正方向。
- **多 repository 工作區支援**：可指定 repository 名稱或路徑，也能處理 VS Code `.code-workspace`。
- **快速本地分支審查**：一行自動辨識目前分支、遠端預設主分支與最新 base；可選擇納入已儲存但尚未提交的檔案。

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

## 版本與 GitHub Release

目前版本記錄在 `skills/merge-reviewer/VERSION`，採用 `X.Y.Z` 的 SemVer 格式。GitHub Release 的 tag 必須與版本檔一致，例如：

```text
VERSION: 0.1.2
tag: v0.1.2
```

完成版本變更並推送到 `main` 後，建立並推送 tag 即可觸發 Release workflow：

```powershell
git tag -a v0.1.2 -m "Release v0.1.2"
git push origin v0.1.2
```

Workflow 會先執行單元測試，再建立 `merge-reviewer-0.1.2.zip`。Release 附件內含可直接複製到 Codex skill 目錄的 `merge-reviewer` 資料夾，不包含 repository 的測試檔或其他開發檔案。

若 workflow 建立 Release 時收到權限錯誤，請在 GitHub repository 的 **Settings → Actions → General → Workflow permissions** 啟用 **Read and write permissions**；組織層級政策可能限制此設定。

## 快速開始

指定專案、基礎分支與比較分支：

```text
$merge-reviewer 專案名稱=OrderService 基礎分支=main 比較分支=feature/payment
```

使用 commit，並改用直接比較：

```text
$merge-reviewer 基礎分支=abc123 比較分支=def456 比較模式=直接比較
```

快速比較目前本地分支與遠端預設主分支：

```text
$merge-reviewer 快速審查
$merge-reviewer 快速審查 包含未提交變更
$merge-reviewer 快速審查 遠端=upstream
```

快速模式只需要一行即可使用目前本地分支的 `HEAD`，不要求開發分支已 push。預設只看已提交內容；`包含未提交變更` 會把 staged、unstaged、刪除與未忽略的未追蹤檔案建立成固定快照。若未指定 `--context-dir`，helper 會在 repository 外自動建立可供審查的 context bundle，完成報告後再刪除 `context_dir`。編輯器尚未儲存的內容不在範圍內。

### 輸入參數

| 參數 | 必要性 | 說明 |
| --- | --- | --- |
| `專案名稱` | 多 repository 時必要 | Repository 資料夾名稱或路徑。工作區只有一個 repository 時可省略。 |
| `基礎分支` | 必要 | 本機分支、明確 remote ref、tag 或 commit。未加 remote 前綴的分支只查本機。 |
| `比較分支` | 必要 | 要審查的本機分支、明確 remote ref、tag 或 commit。未加 remote 前綴的分支只查本機。 |
| `比較模式` | 選填 | `合併前審查`（預設）或 `直接比較`。 |
| `快速審查` | 選填 | 使用目前本地分支與遠端預設主分支；不可同時指定 `比較分支`。 |
| `遠端` | 快速模式選填 | 多個 remote 時指定要使用的 remote；單一 remote 會自動選取。 |
| `包含未提交變更` | 快速模式選填 | 將工作區建立成固定 tree 快照後納入比較。 |

Repository 或 remote 選擇不明確時，skill 會列出候選項目並要求選擇。指定的 ref 找不到時會直接回傳錯誤，不會改查 upstream、同名本機／遠端分支或過期快取。

`main`、`feature/login`、`refs/heads/main` 只查本機 branch；`origin/main`、`refs/remotes/origin/main` 只查指定 remote。Remote branch 會先連線確認並以 exact refspec 更新暫存 remote-tracking ref；因此 remote-qualified ref 不能搭配 `--no-fetch`。Tag 可使用 `refs/tags/<name>`（或沒有同名本機 branch 時使用短 tag 名稱），commit SHA 與 `HEAD` 仍可直接使用。

## 比較模式

| 模式 | 比較範圍 | 適用情境 |
| --- | --- | --- |
| `合併前審查` | `merge-base(基礎版本, 比較版本)` → `比較版本` | 檢查比較分支相對共同祖先新增或保留的行為。 |
| `直接比較` | `基礎版本` → `比較版本` | 需要精確檢查兩個指定版本之間的完整差異。 |

報告會記錄輸入 ref、解析後的完整 SHA、比較模式、merge base、fetch 結果，以及工作樹是否維持不變。

快速模式透過 `git ls-remote --symref <remote> HEAD` 取得遠端宣告的預設分支，並連線確認實際 branch；遠端未提供 HEAD 時才使用本地候選，再以 fetch 驗證該 branch。多個 remote 或多個主分支候選都會停止並列出選項，不會猜測。

## 審查安全行為

審查過程使用 Git object 命令讀取版本內容，不會：

- checkout、merge、reset、stage 或修改來源分支與 commit；
- 預設不會將未提交的工作樹變更混入比較範圍；快速模式明確使用 `包含未提交變更` 時，會以 repository 外的 alternate index/object directory 建立固定快照；
- 自動修正程式碼、建立 commit、發布評論或推送變更。

為了確認 remote 分支不是過期版本，helper 可能執行 `git fetch --no-tags --no-prune`。除 fetch、repository 外的工作區快照暫存資料與 context bundle 外，helper 不會寫入受審 repository；審查報告是唯一的 repository 產物。

## 報告輸出

報告會寫入被審查 repository 的：

```text
<repo>/review-reports/merge-review-<UTC-timestamp>-<base-short>-<head-short>.md
```

報告包含：

- **審查結論**：先用白話說明整體影響、P0–P3 數量、優先處理的 finding 編號，以及會影響結論的檢查缺口；
- **問題總覽**：用表格列出固定編號、中文優先程度、問題和對使用者／資料／服務的影響；
- **問題詳情**：依 P0 至 P3 排序，每項先說明影響，再列操作情境、預期結果、實際結果、修正方向與技術證據；
- **範圍與限制**：變更檔案摘要、binary／submodule 限制、檢查過的 merge commits，以及本次靜態審查未執行的測試；
- **技術審查紀錄**：專案與 repository 路徑、輸入 ref 與解析後 SHA、比較模式與範圍、merge base、fetch 狀態、工作樹狀態及證據來源。

問題總覽的數量和問題詳情的固定編號必須一致；詳細格式與示意請參閱
[`skills/merge-reviewer/references/review-rules.md`](skills/merge-reviewer/references/review-rules.md)。

### 結果狀態

| 狀態 | 意義 |
| --- | --- |
| `發現具體問題` | 審查範圍完整，至少有一個可由程式或 Git 證據支持的 finding；問題總覽會列出所有 finding。 |
| `沒有差異` | 版本與比較範圍有效，但沒有變更檔案。 |
| `未發現具體問題` | 可行的審查範圍已完整檢查，沒有足夠證據建立 finding；不代表程式絕對正確。 |
| `審查未完成` | ref、fetch、merge base、檔案讀取或上下文檢查存在明確缺口；即使已找到部分 finding，也必須列出缺口並保留已確認的問題。 |

聊天摘要會依序顯示結果與白話結論、P0–P3 數量、最多三項優先問題及各自的一句情境，最後提供完整報告連結。沒有具體問題時不會虛構情境；有更多問題時會提示讀者查看完整報告。

## 限制與錯誤處理

- Remote branch 的 fetch 失敗時會停止審查，不會靜默使用可能過期的 remote ref。
- 本機 branch 不會因為設定 upstream 而觸發 fetch；指定不存在的本機 branch 會直接回報錯誤。
- Remote-qualified ref 不能搭配 `--no-fetch`；`--no-fetch` 只適用於沒有 remote branch 輸入的比較。
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
- `--quick`：以目前本地分支 `HEAD` 對遠端預設分支執行快速審查。
- `--remote`：快速模式指定 remote；多個 remote 時必要。
- `--include-working-tree`：快速模式納入 staged、unstaged、刪除與未忽略的未追蹤檔案。
- `--mode merge|direct`：選擇比較模式。
- `--no-fetch`：只允許沒有 remote-qualified ref 的比較；指定 remote branch 時會直接回報參數衝突。
- `--context-dir`：將 manifest 與完整 patch 寫入新的暫存目錄。

Helper 會輸出固定版本 SHA、比較範圍、變更檔案、merge commit、diff 統計、fetch 結果、遠端選擇與工作樹快照，供審查流程作為來源真相。Manifest schema version 為 2；工作區模式會另外輸出 `review_tree_sha`、`review_scope=working-tree` 與 `snapshot_read_info`，並在 context bundle 保留可供審查的 `working-tree.patch` 和變更檔案內容。

如果 submodule 內有未提交或未初始化內容，helper 會列在 `dirty_submodule_paths` 和 `review_limitations`，此結果不能被回報為完整審查。

## 專案結構

```text
.
├── .github/
│   └── workflows/
│       └── release.yml
├── scripts/
│   └── package_release.py
├── README.md
└── skills/
    └── merge-reviewer/
        ├── SKILL.md
        ├── VERSION
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

## 開發驗證

安裝 `requirements-dev.txt` 後，可執行快速審查情境與 Python 回歸測試：

```powershell
python -m behave tests/features --tags=@quick-review
python -m behave tests/features --tags=@release
python -m unittest discover -s tests -v
```
