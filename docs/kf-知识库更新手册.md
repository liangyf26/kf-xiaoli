# kf-xiaoli 知识库更新手册（运维）

> 面向第一次接触本项目的人。照做即可完成知识库热切换，**全程不需要重启服务，也不依赖大模型**。
> 所有命令在仓库根目录 `/home/lyf/project/kf-xiaoli` 下执行；`python` 指仓库虚拟环境 `.venv/bin/python`。

## 0. 前置确认（30 秒）

1. 服务在跑：`curl -s http://127.0.0.1:8000/healthz` 有返回（不在 8000 就把下文 `--base-url` 换成实际地址）。
2. 管理令牌已配置：`.env` 里有非空的 `ADMIN_TOKEN`。**没配就先配上再重启服务**——服务端没配 token 时，管理接口一律 401（fail-closed，没有 localhost 豁免）。
3. 执行环境有令牌：`export ADMIN_TOKEN=<你的令牌>`（CLI 默认读这个环境变量，也可每次加 `--token`）。

## 1. 新知识库怎么放

1. 把新库文件放进 `data/` 目录，起一个带日期的新名字，例如 `data/sdwan-20261101.md`。**不要覆盖旧文件**——旧 `data/sdwan.md` 就是你的回滚点。
2. 文件格式要求（解析器 `backend/knowledge.py`）：
   - 每条问答以「`N. 标题`」开头（N 是数字，支持 `.`、`、`、`．`），其后到下一个编号行之间的内容属于这一条；
   - 整个文件**至少要有一条**编号问答；全是纯文本（没有编号行）会被拒绝（防止整篇被当成一条）；
   - UTF-8 编码；
   - `{{{发送图片:…}}}` / `{{{发送文件:…}}}` 会被整段删除（本机路径不进答案），`{{{人工介入：…}}}` 会变成「（需人工介入：…）」——写库时不必手删，但别指望图片真的发出去（发图是后续功能）。

## 2. 第一步：先校验（不动线上）

```bash
.venv/bin/python scripts/kb_reload.py --path data/sdwan-20261101.md --dry-run
```

看到「校验通过（未切换）· 新库」+ 条数/分类分布即通过；**此时线上库没有任何变化**。
失败会显示原因（退出码 1）：文件不存在 / 空文件 / 编码错 / 无编号标题。照提示修文件再试。

## 3. 第二步：切换

```bash
.venv/bin/python scripts/kb_reload.py --path data/sdwan-20261101.md
```

输出「切换成功 · 新库」+ 新库条数/分类分布 + 上一个库对比（条数差）。**立即生效**，运行中的用户请求下一步就用到新库，无需重启。
不带 `--path` 时，切到 `.env` 里 `KNOWLEDGE_BASE_PATH` 指向的文件。

## 4. 第三步：核对

```bash
.venv/bin/python scripts/kb_reload.py --stats
```

看「当前库」的来源文件名和条数是不是你要的新库；`previous` 是上一个库，用来对账。
抽查业务问题：问两条新库独有问题的原文，答案应体现新内容。

## 5. 回滚（再切一次旧文件）

```bash
.venv/bin/python scripts/kb_reload.py --path data/sdwan.md
```

`--stats` 看到 `source: sdwan.md`、条数回到旧值即回滚完成。

## 6. 退出码与常见错误

| 退出码 | 含义 | 处理 |
|---|---|---|
| 0 | 成功 | — |
| 1 | 校验失败或服务不可达（HTTP 400 等） | 看输出的原因；确认服务地址（`--base-url`） |
| 2 | 鉴权失败（HTTP 401） | 检查 `ADMIN_TOKEN` 环境变量与服务端 `.env` 是否一致；服务端未配置 token 时一律 401 |

## 7. 两条重要边界

1. **reload 不改配置**：热切换只影响当前运行的进程；重启后回到 `.env` 的 `KNOWLEDGE_BASE_PATH`。**确认新库稳定后，把 `.env` 的 `KNOWLEDGE_BASE_PATH` 改成新文件并提交**，否则下次重启会切回旧库。
2. **换库与模型无关**：reload 只读文件、解析、换内存引用；把服务的 `MODEL_API_BASE` 指到不可达地址，换库照样成功。答案质量问题请走单独的质量对比（A/B）流程，不要指望换库程序来评估。

## 8. 一分钟速查

```bash
export ADMIN_TOKEN=<令牌>
.venv/bin/python scripts/kb_reload.py --path data/新库.md --dry-run   # ① 校验
.venv/bin/python scripts/kb_reload.py --path data/新库.md            # ② 切换
.venv/bin/python scripts/kb_reload.py --stats                        # ③ 核对
.venv/bin/python scripts/kb_reload.py --path data/sdwan.md           # ④ 回滚（需要时）
```
