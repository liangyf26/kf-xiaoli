# 任务书：微信真实聊天记录 → 脱敏测试集 → 四引擎批量评测

这活为什么干：至今的测试题都是我们自己编的，要用两个微信群的真实客户提问检验四个引擎，找出真实场景下哪条路线更好，并顺带验证"重复回答"有没有改善。

## 现状（已实测，不用再查）
- 原始数据：`scripts/biz_wx_message.sql`（958行，09-14~09-29，真实SD-WAN咨询，价值高）、`scripts/biz_wx_message2.sql`（25274行，07-26~09-14，噪声多）。均为 UTF-8 的 `INSERT INTO \`biz_wx_message\` VALUES (...);` 一行一条，13列：id,msg_id,self_wxid,direction(1入2出),peer_nickname,sender_id,to_wxid,content,from_wxid,is_chatroom,msg_type(1=文本),file_path,created_at。msg_id 以 `bot_` 开头的是旧机器人回复
- 噪声：心跳"正常29.26:8097"、"掉线微信号"、"首次上线"、广告、同一群消息被多个机器人账号重复收到；群消息 from_wxid 常为空
- 两个 SQL 已被 `.gitignore` 忽略，不许改这条规则
- 引擎：`SUPPORTED_ENGINES=("rule","jev","kev","qwen")`，情绪五级 neutral/positive/urgent/dissatisfied/complaint_risk
- `tests/batch_test.py` 读"每行一问、空行分隔会话"的 txt，`--engine` 选引擎；标注格式见 `tests/accuracy_test_cases.json`
- 基线：`.venv/Scripts/python.exe -m pytest tests -q` → **83 passed, 1 skipped**

## 任务
0. 跑基线，对不上写 `BLOCKED.md` 停下；读 `PROGRESS.md`，写不超过10行开工说明。
1. **提取脚本** `scripts/extract_wx_sessions.py`：直接解析两个 SQL（不装 MySQL）；只留文本；去噪；按"内容＋发言人＋60秒内"去重；会话键=私聊对方 或 群＋发言人，同键30分钟内连续消息为一个会话；只留含业务关键词的会话。每轮带上紧随其后的旧机器人回复和人工回复（作参考答案）。
2. **脱敏**（在写出任何文件之前做）：微信号/wxid/昵称换成 `U001`、`G01` 这样的编号；手机号、邮箱、IP:端口、网址里的个人参数、身份证号打码。编号↔真实身份映射只写 `data/wx_real/mapping.json`（已被忽略）。
3. **输出**到 `data/wx_real/`：`sessions.txt`（batch_test 格式）、`sessions.json`（含参考答案和来源文件）、`stats.md`（各过滤步骤前后数量）。
4. **四引擎对比** `tests/compare_real_sessions.py`：同一批会话跑 rule/kev/qwen（jev 见拍板），输出 `tests/results/real_compare_<时间>.md`：意图分布、情绪分布、决策耗时 P50/P95、澄清比例、"无法回答"比例、各引擎意图不一致的问题清单。
5. **标注集**：从会话里抽100条单句（各意图都要有，情绪类至少20条）写 `data/wx_real/labeled.json`，格式兼容 accuracy_test_cases，另加 `expected_intent`、`expected_emotion`；先由 qwen 预标，`"reviewed": false`，留给人工复核。
6. **情绪专项**：抽出含"卡/慢/掉线/退/垃圾/骗/投诉/搞不定"等的消息，单独统计四引擎判成 dissatisfied/complaint_risk 的命中率，写进第4步报告。
7. **新旧对比**：对有旧机器人回复的问题，用当前系统重答，报告里并排列出：问题／旧回复／新回复；统计旧回复里相邻两次内容相同（重复回答）的比例和新系统的比例。
8. 新增 `tests/test_phase6_units.py`（用造的假SQL行，不读真文件），至少覆盖：SQL 行解析（含转义引号、换行）；去噪；去重；会话切分；脱敏后无手机号/wxid 残留。
9. 更新 `README.md`、`PROGRESS.md`，提交推送 master（中文提交说明）。

## 不许
- 不许提交任何含真实微信号、昵称、手机号的文件；`data/wx_real/` 里只能提交脱敏后的产物
- 真实消息不许发给 Jev API（数据会出境）
- 不许删、跳过、放松已有测试；不许 `|| true` 凑绿
- 不许改 `data/sdwan.md`

## 建议（可以换更好的做法，在 PROGRESS.md 写原因）
- 业务关键词表放在脚本顶部常量，便于调整
- 对比脚本复用 `tests/batch_test.py` 的会话运行逻辑

## 我替领导拍的板
- Jev 只跑脱敏后的 `sessions.txt`，且默认关闭，加 `--include-jev` 才跑
- 会话切分间隔 30 分钟、去重窗口 60 秒，写成命令行参数
- 标注集先由 qwen 预标，人工复核留给领导，不阻塞本任务完成
- kev 本机慢就只跑前30个会话，在报告里注明

## 止损
- 同一验收连败3次：写 `BLOCKED.md`，跳下一项
- 第二个 SQL 解析后有效业务会话 < 10 个：只用第一个，在 stats.md 说明

## 完成条件
1. `.venv/Scripts/python.exe -m pytest tests -q` → 通过 ≥ 88（83＋至少5个新测试），skipped ≤ 1，0 failed
2. `data/wx_real/sessions.txt` 有效会话 ≥ 40，`labeled.json` = 100 条
3. 泄露检查无输出：`git ls-files data/wx_real | xargs grep -lE "wxid_|1[3-9][0-9]{9}"`；`git ls-files | grep -E "\.sql$|mapping"` 也无输出
4. `tests/results/real_compare_*.md` 存在，含四项内容：引擎对比表、情绪专项、新旧对比、重复回答比例
5. `git status` 干净且已推送
