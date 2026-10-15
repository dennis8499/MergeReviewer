# Merge Reviewer

`0.12.1` 是以 Codex 原生 Code Review 準則審查**單一 Repo、基礎分支與比較分支**的技能。宣告工作區包含多個 Repo 時，只延伸檢查所選變更的相關契約及消費端，產生繁體中文精簡 Merge Report。

## 安裝與第一次使用

需求：Git（預覽需支援 `git merge-tree --write-tree`）、Python 3.10 以上，以及可載入 Codex skill 的環境。

從 [Releases](https://github.com/dennis8499/MergeReviewer/releases) 下載 `merge-reviewer-0.12.1.zip`，把其中的 `merge-reviewer` 放到 `$CODEX_HOME/skills/merge-reviewer`；未設定 CODEX_HOME 時用 `~/.codex/skills/merge-reviewer`。原始碼安裝複製 `skills/merge-reviewer`。已有舊版時先依[升級說明](skills/merge-reviewer/references/usage.md#8-升級與舊入口遷移)處理，避免混入舊檔案。

在 Codex 對話輸入：

~~~text
$merge-reviewer 專案名稱=OrderService 基礎分支=main 比較分支=feature/payment
~~~

三個參數必填。名稱重複時用完整路徑；遠端分支明確寫 `origin/main`。本機功能分支不用先 push。

完整操作、PowerShell 安裝、工作區範例、報告及錯誤排除：[詳細使用指南](skills/merge-reviewer/references/usage.md)。

## 審查行為

- 核心準則及 P0–P3 採 Codex 原生 Code Review，套用適用的 `AGENTS.md` 背景；工具只準備固定證據及驗證覆蓋。
- 固定比較 `merge-base(基礎分支, 比較分支) → 比較分支`，另提供 preview 及 merge-parent 證據；不納入未提交內容。
- 一個 Git 根目錄或只有一個 Repo 的容器停用跨 Repo 檢核；至少兩個宣告範圍內的不同根目錄才啟用。
- 永遠只有一個受審 Repo；其他 Repo 使用擷取時的 HEAD，不套用同名分支或本機修改。
- 不修改原始檔案、index 或目前分支；只同步指定遠端、建立系統暫存證據及寫報告。
- 必要來源、預覽或影響鏈缺失時保持未完成，保留已有證據的問題。

## 報告

~~~text
<所選Repo>/review-reports/merge-review-<時間戳>-<基礎分支commit前8碼>-<比較分支commit前8碼>.md
~~~

標題為 `Merge Report: <Repo>`，採結論先行的繁體中文精簡風格，全數問題依原生優先程度排序。結論與問題數量後的「比較資訊」列出 Repo／本機路徑、輸入分支、實際 ref、8 碼 commit、合併方向及共同祖先到比較分支的主要差異範圍；完整 SHA 與搜尋紀錄保留在技術附錄。

每次只產生一個 Markdown 檔案。時間戳為 UTC 發布時間，格式 `YYYYMMDDTHHMMSSffffffZ`（含微秒）；檔名依序使用擷取時基礎分支頂端與比較分支頂端 SHA 的固定前 8 碼，基礎分支取分支頂端而非共同祖先。同名時遞增時間戳，保留既有報告。

狀態為「發現具體問題」「沒有差異」「未發現具體問題」「審查未完成」。未發現問題不保證完全正確；未執行的專案測試不會寫成通過。

## 0.12.0 相容性

不支援 Stage、未提交、quick、直接比較、tag／commit／HEAD、多起點、MR task 或 Megin 收據。舊參數直接拒絕，不自動轉成其他審查。

公開 context/result 為 v3，見[證據與結果契約](skills/merge-reviewer/references/workspace-review.md)。GitlabWorkSpace 0.24.0 整合本版的原生分支審查介面；仍固定 MergeReviewer 0.11.0 的舊版工作台須更新完整 VSIX／組合包，不能直接替換其依賴 MR、Group quick 及收據 adapter 的技能目錄。

## 開發驗證與套件

~~~powershell
python -m pip install -r requirements-dev.txt
python -X utf8 -B -m unittest discover -s tests -v
python -X utf8 -B -m behave tests/features --tags="not @human-acceptance"
python scripts/package_release.py --tag v0.12.1
~~~

CI 維持 Linux／Windows × Python 3.10／3.14 矩陣；版本在 `skills/merge-reviewer/VERSION`，Release tag 需一致。ZIP 包含技能、使用指南、原生準則、風格、v3 契約及五個必要 helper，不包含測試及舊收據 runtime。

自動測試驗證介面、證據、延伸、報告及清理；[語意品質案例](tests/review_quality_cases.md)與[實際技能紀錄](tests/skill_acceptance_results.md)另行驗收。Helper 通過不代表模型審查品質已通過。

## 文件

- [技能流程](skills/merge-reviewer/SKILL.md)
- [詳細使用指南](skills/merge-reviewer/references/usage.md)
- [原生準則與證據](skills/merge-reviewer/references/review-rules.md)
- [報告風格](skills/merge-reviewer/references/report-style.md)
- [分支證據與 v3 契約](skills/merge-reviewer/references/workspace-review.md)
