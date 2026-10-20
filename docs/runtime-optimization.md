# 0.16.0 本機候選驗證

本輪新增跨語言 Git 程序樹配額、精確 fetch 防護、BOM／symlink 修正、選用 v2 摘要／共享全文／完整搜尋分頁、manifest generation 與必要 merge-parent 完成對帳。Windows／POSIX 的鎖只由相符 nonce 釋放；未知狀態保留供人工查明。套件固定文字檔以 LF 打包，二進位保持原 bytes。

實際模型驗收沿用既有 `gpt-6-luna`、`reasoning_effort=max`，只用於獨立測試，不設定技能執行模型。`tests/create_quality_fixture.py` 可建立 p0、p1、safe、net-zero、gap、secrets 六種 source-only 工作區；parent-only oracle 位於工作區外，不能交給盲測 agent。`test_quality_fixtures.py` 以合成執行證明來源確有失敗／安全反例，不能當成模型已完成審查。

模型比較三組：未載入技能 prose、現行 HEAD 0.15.0、候選 0.16.0；使用相同來源、任務、工具與預算。未載入技能組以獨立 adapter 評分；技能組仍需通過正式 publisher 的固定證據及完成 gate。關鍵案例各三個 fresh session，裁定根因、固定證據、缺口與敏感外洩；問題拆合及文字不同可接受。所有失敗與重試保留，任一嚴重漏報／誤報、假完成或敏感外洩不得由速度平均抵銷。

Helper 重播使用初始 HEAD 的原生來源，normal／large 各五次交替執行。正規化只移除擷取時間、位置、generation 與空的新 parent ledger；所有非空 parent 義務、SHA／blob、引用、覆蓋、問題與報告仍比較。Helper 秒數與輸出 bytes 不稱為模型耗時或 tokens。沒有實際 token 計量時記 null。

本輪測試／重播證據由任務的 `.tmp/reviewer-runtime-*` 獨立目錄保留。歷史 0.14.0／0.4.1 A/B 及既有大型案結果原樣保留，不能用來宣稱本候選加速。新模型盲測未完成前不宣稱語意品質已驗收；未發布、未推送或建立 tag。
