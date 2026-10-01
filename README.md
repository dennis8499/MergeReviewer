# Merge Reviewer

`Merge Reviewer` 是一個用於審查 Git 分支或 commit 合併結果的 Codex skill。它不需要 checkout 任一版本，就能以 Git object 為基礎比較兩個已提交的版本，或快速比較目前本地分支與遠端主分支，檢查合併整合可能遺失的驗證、授權、錯誤處理、設定或資料轉換邏輯，並產生繁體中文 Markdown 報告。

## 功能

- **合併前審查**：以 `merge-base(基礎版本, 比較版本)` 到比較版本的範圍，檢查即將帶入的變更。
- **實際合併預覽**：在 repository 外產生基礎與比較版本的預覽合併樹，檢查兩邊變更整合後的相容性。
- **直接比較**：直接比較指定的基礎版本與比較版本。
- **Merge commit 檢查**：逐一對照 merge commit 的各個 parent 與合併結果，確認任一側的必要邏輯沒有在解衝突時遺失。
- **證據導向 finding**：依 P0、P1、P2、P3 排序，記錄觸發條件、程式證據、影響與聚焦的修正方向。
- **多 repository 工作區支援**：可指定 repository 名稱或路徑，也能處理 VS Code `.code-workspace`。
- **快速本地分支審查**：一行自動辨識目前分支、遠端預設主分支與最新 base；可選擇納入已儲存但尚未提交的檔案。
- **可查證的審查報告**：記錄逐檔覆蓋與固定版本證據，驗證一致性後預設產生 Markdown；需要程式整合時可選擇附加 JSON 報告。

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
VERSION: 0.3.0
tag: v0.3.0
```

完成版本變更並推送到 `main` 後，建立並推送 tag 即可觸發 Release workflow：

```powershell
git tag -a v0.3.0 -m "Release v0.3.0"
git push origin v0.3.0
```

Workflow 會先執行單元測試，再建立 `merge-reviewer-0.3.0.zip`。Release 附件內含可直接複製到 Codex skill 目錄的 `merge-reviewer` 資料夾，不包含 repository 的測試檔或其他開發檔案。

若 workflow 建立 Release 時收到權限錯誤，請在 GitHub repository 的 **Settings → Actions → General → Workflow permissions** 啟用 **Read and write permissions**；組織層級政策可能限制此設定。

## 快速開始

指定專案、基礎分支與比較分支：

```text
$merge-reviewer 專案名稱=OrderService 基礎分支=main 比較分支=feature/payment
```

一般分支或 commit 比較只審查指定版本的已提交內容。專案即使有已暫存、未暫存、已刪除或未追蹤的檔案，也能直接執行；這些本機內容不會納入比較，原始修改會保留，不必先 stage、commit 或 stash。

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

快速模式只需要一行即可使用目前本地分支的 `HEAD`，不要求開發分支已 push。預設只看已提交內容；`包含未提交變更` 會把 staged、unstaged、刪除與未忽略的未追蹤檔案建立成固定快照，也會包含強制暫存的 ignored 檔案。若未指定 `--context-dir`，helper 會在 repository 外自動建立可供審查的 context bundle，完成報告後再刪除 `context_dir`。編輯器尚未儲存的內容不在範圍內。

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
| `合併前審查` | `merge-base(基礎版本, 比較版本)` → 比較版本，並產生預覽合併樹 | 檢查功能分支變更及主分支更新後的整合行為。 |
| `直接比較` | `基礎版本` → `比較版本` | 需要精確檢查兩個指定版本之間的完整差異。 |

直接比較不要求共同祖先。Manifest 的 `merge_base` 在唯一共同祖先存在時記錄 SHA；沒有或有多個共同祖先時記錄 `null`。合併前審查仍要求唯一共同祖先。

報告會記錄輸入 ref、解析後的完整 SHA、比較模式、merge base、fetch 結果，以及工作樹是否維持不變。

快速模式透過 `git ls-remote --symref <remote> HEAD` 取得遠端宣告的預設分支，並連線確認實際 branch；遠端未提供 HEAD 時才使用本地候選，再以 fetch 驗證該 branch。多個 remote 或多個主分支候選都會停止並列出選項，不會猜測。

## 審查安全行為

審查過程使用 Git object 命令讀取版本內容，不會：

- checkout、merge、reset、stage 或修改來源分支與 commit；
- 一般指定版本比較只讀取指定的已提交版本，既存的 staged、unstaged、刪除或未追蹤變更不會混入差異，且不會要求先清理；快速模式明確使用 `包含未提交變更` 時，才會以 repository 外的 alternate index/object directory 建立固定快照。合併預覽也將新 Git objects 寫到 repository 外。唯讀 Git 查詢會停用選擇性 index 更新；
- 自動修正程式碼、建立 commit、發布評論或推送變更。

為了確認 remote 分支不是過期版本，helper 可能執行 `git fetch --no-tags --no-prune`。除 fetch、repository 外的工作區快照暫存資料與 context bundle 外，helper 不會寫入受審 repository；審查報告是唯一的 repository 產物。

## 報告輸出

Markdown 報告預設寫入被審查 repository 的：

```text
<repo>/review-reports/merge-review-<UTC-timestamp>-<base-short>-<head-short>.md
```

要求 `輸出JSON` 時，另產生同名 `.json` 報告。

報告包含：

- **審查結論**：先給合併建議（`可以合併`、`修正後再合併`、`暫緩合併`、`需補做審查`）與白話一句話摘要，再列 P0–P3 數量、下一步（含處理角色）與會影響結論的檢查缺口；
- **問題總覽**：用表格列出固定編號、行動導向的優先程度、問題、對使用者／資料／服務的影響，以及建議處理者（開發、維運設定、QA）；
- **問題詳情**：依 P0 至 P3 排序，每項先用白話說明影響，再列操作情境、預期結果、實際結果、建議處理與技術證據（含工程修正建議）；
- **JSON 審查結果（選用）**：使用 `輸出JSON` 要求時，保留逐檔檢查狀態、已驗證的證據版本與行號、P0–P3 數量及一致的結果狀態；
- **範圍與限制**：用白話說明已檢查與未檢查的範圍、變更檔案摘要、binary／submodule 限制，以及本次靜態審查未執行的測試；
- **技術審查紀錄（工程師參考）**：專案與 repository 路徑、輸入 ref 與解析後 SHA、比較模式與範圍、共同起點、同步遠端狀態、工作樹狀態及證據來源。

問題總覽的數量和問題詳情的固定編號必須一致；結論、總覽與影響說明只用白話，程式名稱、檔名與 SHA 只出現在技術區段。詳細格式與示意請參閱
[`skills/merge-reviewer/references/review-rules.md`](skills/merge-reviewer/references/review-rules.md)。

### 問題等級

| 等級 | 行動標籤 | 意義 |
| --- | --- | --- |
| P0 | 必須立即處理 | 普遍觸發，可能造成資料遺失、重大安全暴露、服務完全不可用或阻止部署。 |
| P1 | 合併前必修 | 常見流程中的高影響問題。 |
| P2 | 近期排程修正 | 限定情境或較低影響，但有明確證據。 |
| P3 | 可後續改善 | 低風險的具體缺陷。 |

### 結果狀態與合併建議

| 狀態 | 意義 |
| --- | --- |
| `發現具體問題` | 審查範圍完整，至少有一個可由程式或 Git 證據支持的問題；問題總覽會列出所有問題。 |
| `沒有差異` | 固定版本有效、審查範圍沒有差異，且合併預覽沒有額外變更。 |
| `未發現具體問題` | 已檢查的範圍內沒有找到有證據的問題；不代表程式絕對正確。 |
| `審查未完成` | 部分內容無法檢查（ref、同步遠端、共同起點、檔案讀取、合併預覽或逐檔覆蓋有缺口），結果不能視為通過；即使已找到部分問題，也必須列出缺口並保留已確認的問題。 |

合併建議依狀態與最高等級判定：`審查未完成` 為 `需補做審查`；有 P0 為 `暫緩合併`；有 P1 為 `修正後再合併`；其他情況為 `可以合併`（P2／P3 排入後續修正）。

聊天摘要會依序顯示合併建議與白話結論、P0–P3 數量、最多三項優先問題及各自的一句情境、下一步，最後提供完整報告連結。沒有具體問題時不會虛構情境；有更多問題時會提示讀者查看完整報告。

## 限制與錯誤處理

- 準備階段失敗時，helper 會先輸出 `merge-reviewer: error: <原因>`，多數情況再輸出 `merge-reviewer: 建議：<下一步>`，例如多個 remote 時提示使用 `--remote`、shallow clone 時提示 `git fetch --unshallow`。

- Remote branch 的 fetch 失敗時會停止審查，不會靜默使用可能過期的 remote ref。
- 本機 branch 不會因為設定 upstream 而觸發 fetch；指定不存在的本機 branch 會直接回報錯誤。
- Remote-qualified ref 不能搭配 `--no-fetch`；`--no-fetch` 只適用於沒有 remote branch 輸入的比較。
- 無效 ref 與 shallow history 會停止並回報原因；合併前審查要求唯一共同祖先。直接比較可以處理沒有或有多個共同祖先的版本組合，manifest 會以 `merge_base: null` 記錄。
- 分支同時存在於多個 remote 時，請使用明確的 ref，例如 `origin/release`。
- Binary、submodule、rename、copy 或 mode-only 變更會列為審查範圍限制，必要時需人工補充檢查。Submodule 變更會辨識 Git link 指標；內部程式碼不在 parent repository 的差異中。
- Merge Reviewer 是靜態審查工具；除非使用者明確要求且實際執行，報告不會宣稱測試已通過。
- `git merge-tree --write-tree` 不支援或產生衝突時，整合審查會標記為未完成。
- 單一 Git 指令預設最多執行 180 秒；使用 `--git-timeout <秒>` 可調整。一般準備指令逾時會停止並附上重試建議；若合併預覽逾時，helper 會記錄 `merge_preview.status=unavailable` 和上下文缺口，報告仍須標為 `審查未完成`。
- 對 squash、rebase 或手動複製的變更，可以審查最終行為，但不會將問題歸因於人工合併。

## 手動執行 Git context helper

需要檢查準備資料或整合其他工具時，可直接執行 helper：

```powershell
$contextDir = Join-Path $env:TEMP ("merge-reviewer-" + [Guid]::NewGuid().ToString("N"))
python skills\merge-reviewer\scripts\git_review_context.py `
  --workspace . `
  --project OrderService `
  --base main `
  --head feature/payment `
  --mode merge `
  --context-dir $contextDir `
  --git-timeout 180 `
  --format json --pretty
```

常用選項：

- `--workspace`：搜尋 repository 的工作區根目錄。
- `--workspace-file`：指定 VS Code `.code-workspace` 檔案。
- `--project`：指定 repository 名稱或路徑。
- `--base`、`--head`：指定兩個比較輸入。
- `--quick`：以目前本地分支 `HEAD` 對遠端預設分支執行快速審查。
- `--remote`：快速模式指定 remote；多個 remote 時必要。
- `--include-working-tree`：快速模式納入 staged、unstaged、刪除與未忽略的未追蹤檔案，也保留使用者以 `git add -f` 暫存的 ignored 檔案。
- `--mode merge|direct`：選擇比較模式。
- `--no-fetch`：只允許沒有 remote-qualified ref 的比較；指定 remote branch 時會直接回報參數衝突。
- `--context-dir`：將 manifest、完整 patch、工作區／合併預覽檔案及其證據摘要寫入新的暫存目錄；保留到本次選定的報告都成功產生後再刪除。
- `--include-json`：除了 Markdown，另外寫入同名的結構化 JSON 審查結果。
- `--git-timeout`：每個 Git 指令的秒數上限，預設 180 秒。

Helper 會輸出固定版本 SHA、比較範圍、變更檔案、merge commit、diff 統計、fetch 結果、遠端選擇、逐檔證據摘要與工作樹快照，供審查流程作為來源真相。Manifest schema version 為 4，新增 `context_complete`、`context_gaps` 與 `merge_preview`，並保留舊的 `review_complete` 欄位；兩者都只表示準備階段是否完成，不代表 Skill 已逐檔審查。工作區模式另外輸出固定 `review_tree_sha`、`review_scope=working-tree`、逐檔 Git blob SHA／SHA-256 摘要及 `snapshot_read_info`，也會納入使用者以 `git add -f` 暫存的 ignored 檔案。合併預覽會將 Git objects 保留在 context bundle，供報告驗證後清理。

如果 submodule 內有未提交或未初始化內容，helper 會列在 `dirty_submodule_paths` 和 `review_limitations`，此結果不能被回報為完整審查。

審查流程會先在 context bundle 建立結構化 `review-result-draft.json`，再使用 `scripts/review_report.py` 驗證每一條證據並預設產生 Markdown 報告。使用者要求 `輸出JSON` 時，再加上 `--include-json` 產生同名 JSON 報告。直接比較模式也須傳入位於 repository 外的 `--context-dir`；本次選定的報告全部建立完成後才可清理整個 context bundle，若驗證或寫入失敗則保留以供重試。

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
            ├── git_review_context.py
            └── review_report.py
```

## 相關文件

- [Merge Reviewer 技能說明](skills/merge-reviewer/SKILL.md)
- [審查規則與報告格式](skills/merge-reviewer/references/review-rules.md)
- [Git context helper](skills/merge-reviewer/scripts/git_review_context.py)
- [Review report validator](skills/merge-reviewer/scripts/review_report.py)
- [Skill review acceptance cases](tests/review_quality_cases.md)

## 開發驗證

安裝 `requirements-dev.txt` 後，可執行快速審查情境與 Python 回歸測試：

```powershell
python -m behave tests/features --tags="not @human-acceptance"
python -m unittest discover -s tests -v
```

GitHub Actions 會在 pull request、`main` 推送與 tag Release 執行 Linux／Windows × Python 3.10／3.14 測試矩陣。Release 必須先通過相同檢查。

自動 CI 驗證 Git context、輸出證據與報告格式；Skill 的漏報與誤報仍依 [`tests/review_quality_cases.md`](tests/review_quality_cases.md) 人工驗收，避免把 deterministic Git 測試誤當成模型審查品質評估。
