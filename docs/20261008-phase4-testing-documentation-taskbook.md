# Phase 4 任务书：测试和文档

**目标**：3天内完成批量测试、准确率评估、性能优化、完整文档编写，交付可演示的Demo系统。

---

## 任务 0：环境验证和前置检查（30分钟）

```bash
# 1. 确认Phase 3已完成
test -f backend/orchestrator/orchestrator.py && echo "✓ 编排器存在" || (echo "✗ Phase 3未完成"; exit 1)
test -f backend/llm/client.py && echo "✓ LLM客户端存在" || (echo "✗ LLM客户端缺失"; exit 1)

# 2. 测试端到端流程可用
python -c "
import asyncio
from backend.orchestrator.orchestrator import Orchestrator

async def test():
    orchestrator = Orchestrator()
    result = await orchestrator.process('测试消息', {})
    assert 'answer' in result, '编排器返回格式错误'
    print(f'✓ 端到端流程可用: {result[\"answer\"][:30]}...')

asyncio.run(test())
"

# 3. 确认Web界面可访问
python -m backend.main &
SERVER_PID=$!
sleep 3

curl -s http://localhost:8000 > /dev/null && echo "✓ Web服务可访问" || echo "✗ Web服务无法访问"

kill $SERVER_PID 2>/dev/null

# 4. 创建测试目录
mkdir -p tests/results logs/test_runs

# 5. 更新PROGRESS.md
cat >> PROGRESS.md << 'EOF'

## Phase 4 开始
### $(date +%Y-%m-%d)
- 任务0: 环境验证通过，开始测试和文档工作

EOF
```

**不通过停止条件**：Phase 3核心模块缺失、端到端流程不可用、Web服务无法启动。

---

## 任务 1：批量测试脚本（Day 13上午，4小时）

### 1.1 批量测试脚本（tests/batch_test.py）

**实现要求**：
- 读取测试问题文件（每行一个问题，空行分隔会话）
- 支持多轮对话测试
- 记录每个问题的：
  - 用户输入
  - 机器人回复
  - 答案来源
  - 响应时间
  - 决策引擎类型
  - 处理路径
- 输出JSON格式结果到tests/results/目录
- 支持指定决策引擎（通过环境变量）

**验收**：
```bash
# 1. 创建测试问题文件
cat > tests/test_questions_final.txt << 'EOF'
你好

直播线路多少钱

我想咨询价格

IDC线路多少钱
那家庭IP线路呢

tiktok登不上怎么办

怎么使用

客户端在哪里下载

看视频有点卡怎么办

支持YouTube吗

咋整

多少钱

能直播吗

价格表

网速慢

有没有便宜的

怎么购买

可以试用吗

退款怎么办

联系客服
EOF

# 2. 运行批量测试
python tests/batch_test.py tests/test_questions_final.txt

# 3. 验证输出
python -c "
import json
import os
from pathlib import Path

# 查找最新的测试结果文件
results_dir = Path('tests/results')
result_files = list(results_dir.glob('batch_test_*.json'))
assert len(result_files) > 0, '未找到测试结果文件'

latest_file = max(result_files, key=lambda p: p.stat().st_mtime)

with open(latest_file) as f:
    data = json.load(f)

assert 'metadata' in data, '缺少metadata字段'
assert 'results' in data, '缺少results字段'
assert len(data['results']) >= 20, f'测试问题不足: {len(data[\"results\"])}'

# 检查每个结果的完整性
for i, item in enumerate(data['results'][:5]):  # 检查前5个
    assert 'question' in item, f'第{i+1}个结果缺少question'
    assert 'answer' in item, f'第{i+1}个结果缺少answer'
    assert 'latency_ms' in item, f'第{i+1}个结果缺少latency_ms'
    assert 'engine' in item, f'第{i+1}个结果缺少engine'

print(f'✓ 批量测试通过，测试了{len(data[\"results\"])}个问题')
print(f'✓ 结果文件: {latest_file}')
"
```

### 1.2 测试报告生成器（tests/generate_report.py）

**实现要求**：
- 读取批量测试结果JSON
- 生成Markdown格式报告
- 包含内容：
  - 测试概览（总数、通过率、平均响应时间）
  - 各决策引擎统计
  - 各处理路径统计
  - 详细测试结果表格
  - 失败/异常案例列表
- 输出到tests/results/目录

**验收**：
```bash
# 生成测试报告
python tests/generate_report.py tests/results/batch_test_*.json

# 验证报告存在
test -f tests/results/test_report_*.md && echo "✓ 测试报告已生成" || echo "✗ 报告生成失败"

# 查看报告内容
python -c "
from pathlib import Path

reports = list(Path('tests/results').glob('test_report_*.md'))
assert len(reports) > 0, '未找到测试报告'

latest_report = max(reports, key=lambda p: p.stat().st_mtime)

with open(latest_report) as f:
    content = f.read()

# 验证报告包含关键章节
assert '# 测试报告' in content or '## 测试概览' in content, '缺少标题'
assert '平均响应时间' in content or 'latency' in content.lower(), '缺少性能数据'
assert '| ' in content, '缺少表格'

print(f'✓ 测试报告完整: {latest_report}')
print(f'✓ 报告长度: {len(content)}字符')
"
```

---

## 任务 2：准确率评估（Day 13下午，4小时）

### 2.1 准确率评估脚本（tests/accuracy_evaluation.py）

**实现要求**：
- 读取标注数据文件（JSON格式）
- 每条数据包含：question, expected_answer_keywords, expected_sources
- 运行测试并比较结果
- 计算准确率指标：
  - 答案包含关键词准确率
  - 来源标注准确率
  - 无法回答判断准确率
- 输出详细对比结果

**验收**：
```bash
# 1. 创建标注数据（基于真实需求）
cat > tests/accuracy_test_cases.json << 'EOF'
[
  {
    "question": "直播线路多少钱",
    "expected_keywords": ["260", "元/月", "直播"],
    "expected_sources": ["问题1"],
    "should_answer": true
  },
  {
    "question": "IDC线路价格",
    "expected_keywords": ["120", "元/月", "IDC"],
    "expected_sources": ["问题1"],
    "should_answer": true
  },
  {
    "question": "tiktok登不上",
    "expected_keywords": ["拔卡", "地区", "美国", "清理"],
    "expected_sources": ["问题11"],
    "should_answer": true
  },
  {
    "question": "客户端下载地址",
    "expected_keywords": ["docs.qq.com", "链接"],
    "expected_sources": ["问题9"],
    "should_answer": true
  },
  {
    "question": "支持YouTube吗",
    "expected_keywords": ["无法回答", "人工"],
    "expected_sources": [],
    "should_answer": false
  },
  {
    "question": "网速慢怎么办",
    "expected_keywords": ["卡", "优化", "线路"],
    "expected_sources": ["问题13", "问题14"],
    "should_answer": true
  },
  {
    "question": "可以试用吗",
    "expected_keywords": ["无法回答", "人工"],
    "expected_sources": [],
    "should_answer": false
  },
  {
    "question": "路由器多少钱",
    "expected_keywords": ["300", "元", "路由器"],
    "expected_sources": ["问题3"],
    "should_answer": true
  },
  {
    "question": "怎么使用",
    "expected_keywords": ["客户端", "路由器", "安装"],
    "expected_sources": ["问题3", "问题9"],
    "should_answer": true
  },
  {
    "question": "有什么套餐",
    "expected_keywords": ["120", "180", "260", "价格"],
    "expected_sources": ["问题1"],
    "should_answer": true
  }
]
EOF

# 2. 运行准确率评估
python tests/accuracy_evaluation.py tests/accuracy_test_cases.json

# 3. 验证评估结果
python -c "
import json
from pathlib import Path

results_dir = Path('tests/results')
accuracy_files = list(results_dir.glob('accuracy_eval_*.json'))
assert len(accuracy_files) > 0, '未找到准确率评估结果'

latest_file = max(accuracy_files, key=lambda p: p.stat().st_mtime)

with open(latest_file) as f:
    data = json.load(f)

assert 'accuracy_metrics' in data, '缺少准确率指标'
assert 'detailed_results' in data, '缺少详细结果'

metrics = data['accuracy_metrics']
assert 'answer_accuracy' in metrics, '缺少答案准确率'
assert 'source_accuracy' in metrics, '缺少来源准确率'

answer_acc = metrics['answer_accuracy']
print(f'答案准确率: {answer_acc*100:.1f}%')

# 检查是否达标
if answer_acc >= 0.85:
    print('✓ 准确率达标（≥85%）')
else:
    print(f'⚠ 准确率未达标: {answer_acc*100:.1f}% < 85%')
    print('建议：优化Few-shot示例或Prompt构建')

print(f'✓ 准确率评估完成: {latest_file}')
"
```

### 2.2 3种决策引擎对比评估

**实现要求**：
- 对相同测试用例运行3种决策引擎
- 对比准确率、响应时间、路径选择
- 生成对比报告

**验收**：
```bash
# 运行3引擎对比
for engine in rule jev kev; do
    echo "测试引擎: $engine"
    export DECISION_ENGINE=$engine
    python tests/accuracy_evaluation.py tests/accuracy_test_cases.json
    mv tests/results/accuracy_eval_*.json tests/results/accuracy_eval_${engine}_*.json 2>/dev/null || true
done

# 生成对比报告
python tests/compare_engine_accuracy.py tests/results/accuracy_eval_*.json

echo "✓ 3引擎对比评估完成"
```

**Day 13 完成检查清单**：
- [ ] 批量测试脚本运行成功（20+问题）
- [ ] 测试报告自动生成
- [ ] 准确率评估完成（10个标注用例）
- [ ] 答案准确率≥85%（如果未达标，记录原因）
- [ ] 3种决策引擎对比完成

---

## 任务 3：性能优化（Day 13晚上，3小时）

### 3.1 性能瓶颈分析

**实现要求**：
- 性能分析脚本（tests/performance_profile.py）
- 分析各组件耗时：
  - 决策层（规则/Jev/Kev）
  - 知识库检索
  - Prompt构建
  - LLM推理
  - JSON解析
- 找出最耗时的环节

**验收**：
```bash
# 运行性能分析
python tests/performance_profile.py

# 查看结果
python -c "
import json
from pathlib import Path

profile_files = list(Path('tests/results').glob('performance_profile_*.json'))
assert len(profile_files) > 0, '未找到性能分析结果'

latest_file = max(profile_files, key=lambda p: p.stat().st_mtime)

with open(latest_file) as f:
    data = json.load(f)

print('性能分析结果:')
for component, time_ms in data.get('average_times', {}).items():
    print(f'  {component}: {time_ms:.0f}ms')

# 找出瓶颈
times = data.get('average_times', {})
if times:
    bottleneck = max(times.items(), key=lambda x: x[1])
    print(f'\\n⚠ 性能瓶颈: {bottleneck[0]} ({bottleneck[1]:.0f}ms)')
    
    if bottleneck[1] > 5000:
        print('建议: 考虑优化LLM调用或Prompt长度')

print(f'✓ 性能分析完成: {latest_file}')
"
```

### 3.2 优化实施

**优化点检查清单**：
```bash
echo "性能优化检查清单："
echo ""
echo "[ ] 1. Prompt长度控制（目标<8000字符）"
echo "[ ] 2. Few-shot示例数量（目标每个意图3-5个）"
echo "[ ] 3. 上下文轮数（目标10轮）"
echo "[ ] 4. 知识库意图筛选生效（不发送全量）"
echo "[ ] 5. LLM温度参数（目标0.3-0.5）"
echo "[ ] 6. 决策引擎选择（Kev比Jev慢但更准确）"
echo "[ ] 7. WebSocket连接池复用"
echo "[ ] 8. JSON解析容错减少重试"
echo ""
echo "执行优化后重新运行批量测试，对比前后性能数据"
```

**优化后验收**：
```bash
# 重新运行批量测试
python tests/batch_test.py tests/test_questions_final.txt

# 对比优化前后
python -c "
import json
from pathlib import Path

results_dir = Path('tests/results')
result_files = sorted(results_dir.glob('batch_test_*.json'), key=lambda p: p.stat().st_mtime)

if len(result_files) >= 2:
    # 对比最新两次测试
    with open(result_files[-2]) as f:
        before = json.load(f)
    with open(result_files[-1]) as f:
        after = json.load(f)
    
    avg_before = before['metadata']['average_latency_ms']
    avg_after = after['metadata']['average_latency_ms']
    
    improvement = (avg_before - avg_after) / avg_before * 100
    
    print(f'优化前平均延迟: {avg_before:.0f}ms')
    print(f'优化后平均延迟: {avg_after:.0f}ms')
    print(f'性能提升: {improvement:.1f}%')
    
    if improvement > 0:
        print('✓ 性能优化有效')
    else:
        print('⚠ 性能未改善，需进一步分析')
else:
    print('⚠ 测试数据不足，无法对比')
"
```

---

## 任务 4：日志和监控完善（Day 14上午，3小时）

### 4.1 日志格式统一

**实现要求**：
- 统一日志格式：时间戳、级别、模块、消息
- 关键日志点：
  - 用户消息接收
  - 决策结果（intent, confidence）
  - 路由路径选择
  - LLM调用开始/结束
  - 回复发送
  - 错误异常
- 日志轮转（按日期）
- 支持DEBUG/INFO/WARNING/ERROR级别

**验收**：
```bash
# 1. 启动服务并测试
python -m backend.main &
SERVER_PID=$!
sleep 3

# 2. 发送测试消息
python -c "
import asyncio
import websockets
import json

async def test():
    uri = 'ws://localhost:8000/ws'
    async with websockets.connect(uri) as websocket:
        await websocket.send('测试日志')
        response = await websocket.recv()
        print('✓ 消息已发送')

asyncio.run(test())
"

sleep 2
kill $SERVER_PID

# 3. 验证日志文件
python -c "
from pathlib import Path
import re

log_dir = Path('logs')
log_files = list(log_dir.glob('app_*.log'))

if not log_files:
    log_files = list(log_dir.glob('app.log'))

assert len(log_files) > 0, '未找到日志文件'

latest_log = max(log_files, key=lambda p: p.stat().st_mtime)

with open(latest_log) as f:
    content = f.read()

# 验证日志格式
log_lines = [line for line in content.split('\\n') if line.strip()]
assert len(log_lines) > 0, '日志文件为空'

# 检查关键日志
has_user_message = any('用户消息' in line or 'user message' in line.lower() for line in log_lines)
has_decision = any('决策' in line or 'decision' in line.lower() for line in log_lines)
has_reply = any('回复' in line or 'reply' in line.lower() for line in log_lines)

print(f'✓ 日志文件: {latest_log}')
print(f'✓ 日志行数: {len(log_lines)}')
print(f'✓ 用户消息日志: {\"是\" if has_user_message else \"否\"}')
print(f'✓ 决策日志: {\"是\" if has_decision else \"否\"}')
print(f'✓ 回复日志: {\"是\" if has_reply else \"否\"}')

if has_user_message and has_decision and has_reply:
    print('✓ 关键日志完整')
else:
    print('⚠ 部分关键日志缺失，请补充')
"
```

### 4.2 监控指标收集

**实现要求**：
- 记录运行时指标（可选，存入JSON）
- 指标包括：
  - 总请求数
  - 各决策引擎使用次数
  - 各处理路径使用次数
  - 平均响应时间
  - 错误次数
- 支持重置和查询

**验收**：
```bash
# 验证指标收集（如果实现了）
python -c "
from pathlib import Path
import json

metrics_file = Path('logs/metrics.json')

if metrics_file.exists():
    with open(metrics_file) as f:
        metrics = json.load(f)
    
    print('✓ 监控指标文件存在')
    print(f'总请求数: {metrics.get(\"total_requests\", 0)}')
    print(f'平均响应时间: {metrics.get(\"avg_latency_ms\", 0):.0f}ms')
else:
    print('⚠ 监控指标文件不存在（可选功能）')
"
```

---

## 任务 5：文档编写（Day 14下午 + Day 15上午，共6小时）

### 5.1 更新README.md

**完善内容**：
- 项目介绍（基于实际功能）
- 功能特性列表
- 快速开始（环境、安装、配置、启动）
- 使用说明（界面操作、切换引擎、批量测试）
- 项目结构（实际目录树）
- 常见问题（基于开发中遇到的问题）
- 测试结果摘要（链接到测试报告）

**验收**：
```bash
# 检查README完整性
python -c "
with open('README.md') as f:
    content = f.read()

required_sections = [
    '快速开始',
    '使用说明',
    '项目结构',
    '测试',
    '常见问题'
]

missing = []
for section in required_sections:
    if section not in content:
        missing.append(section)

if missing:
    print(f'⚠ README缺少章节: {missing}')
else:
    print('✓ README章节完整')

# 检查长度
if len(content) < 3000:
    print(f'⚠ README过短: {len(content)}字符')
elif len(content) > 15000:
    print(f'⚠ README过长: {len(content)}字符，建议精简')
else:
    print(f'✓ README长度适中: {len(content)}字符')
"
```

### 5.2 更新.env.example

**完善内容**：
- 所有配置项注释说明
- 推荐值和可选值
- 3种决策引擎配置示例
- Qwen模型配置示例

**验收**：
```bash
# 检查.env.example完整性
python -c "
with open('.env.example') as f:
    content = f.read()

required_configs = [
    'MODEL_API_BASE',
    'MODEL_NAME',
    'DECISION_ENGINE',
    'CONTEXT_TURNS',
    'WAIT_SLIDE_SECONDS',
    'WAIT_MAX_SECONDS',
    'JEV_API_KEY',
    'KEV_MODEL_PATH',
    'NO_ANSWER_MESSAGE'
]

missing = []
for config in required_configs:
    if config not in content:
        missing.append(config)

if missing:
    print(f'⚠ .env.example缺少配置: {missing}')
else:
    print('✓ .env.example配置完整')

# 检查注释
lines = content.split('\\n')
comment_lines = [l for l in lines if l.strip().startswith('#')]

if len(comment_lines) < 10:
    print(f'⚠ 注释过少: {len(comment_lines)}行')
else:
    print(f'✓ 注释充分: {len(comment_lines)}行')
"
```

### 5.3 代码注释补充

**检查清单**：
```bash
# 检查关键模块的docstring
python -c "
import ast
from pathlib import Path

critical_files = [
    'backend/orchestrator/orchestrator.py',
    'backend/orchestrator/router.py',
    'backend/decision_layer/rule_engine.py',
    'backend/llm/client.py',
]

total_missing = 0

for file_path in critical_files:
    if not Path(file_path).exists():
        print(f'⚠ 文件不存在: {file_path}')
        continue
    
    with open(file_path) as f:
        tree = ast.parse(f.read())
    
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and not node.name.startswith('_')]
    
    missing_in_file = 0
    
    for cls in classes:
        if not ast.get_docstring(cls):
            print(f'  ⚠ {file_path}: 类 {cls.name} 缺少docstring')
            missing_in_file += 1
    
    for func in functions:
        if not ast.get_docstring(func):
            # 跳过__init__等特殊方法
            if not func.name.startswith('__'):
                print(f'  ⚠ {file_path}: 函数 {func.name} 缺少docstring')
                missing_in_file += 1
    
    if missing_in_file == 0:
        print(f'✓ {file_path}: docstring完整')
    
    total_missing += missing_in_file

if total_missing > 0:
    print(f'\\n⚠ 总计缺少{total_missing}个docstring，请补充')
else:
    print('\\n✓ 所有关键模块docstring完整')
"
```

### 5.4 API文档（可选）

**实现要求**：
- 创建docs/api.md
- 文档化WebSocket消息格式
- 文档化配置项含义
- 文档化决策引擎接口

**验收**：
```bash
# 检查API文档
if [ -f docs/api.md ]; then
    echo "✓ API文档存在"
    wc -l docs/api.md
else
    echo "⚠ API文档不存在（可选）"
fi
```

---

## 任务 6：最终验收和交付（Day 15下午，4小时）

### 6.1 完整演示流程

**演示脚本**：
```bash
echo "=== SDWAN智能客服Demo演示 ==="
echo ""
echo "1. 启动服务"
python -m backend.main &
SERVER_PID=$!
sleep 3

echo "✓ 服务已启动: http://localhost:8000"
echo ""

echo "2. 测试核心功能"
python -c "
import asyncio
import websockets
import json

async def demo():
    uri = 'ws://localhost:8000/ws'
    
    test_cases = [
        ('你好', '首次对话-身份告知'),
        ('直播线路多少钱', '价格咨询'),
        ('tiktok登不上', '技术支持'),
        ('支持YouTube吗', '知识库外问题'),
    ]
    
    async with websockets.connect(uri) as websocket:
        for question, desc in test_cases:
            print(f'\\n测试: {desc}')
            print(f'问题: {question}')
            
            await websocket.send(question)
            response = await asyncio.wait_for(websocket.recv(), timeout=30.0)
            data = json.loads(response)
            
            answer = data.get('content', '')[:100]
            print(f'回复: {answer}...')
            print('✓ 通过')

asyncio.run(demo())
"

echo ""
echo "3. 查看测试结果"
echo "批量测试结果:"
ls -lh tests/results/batch_test_*.json | tail -1
echo ""
echo "准确率评估:"
ls -lh tests/results/accuracy_eval_*.json | tail -1
echo ""

echo "4. 停止服务"
kill $SERVER_PID
echo "✓ 演示完成"
```

### 6.2 交付物检查清单

```bash
cat > DELIVERY_CHECKLIST.md << 'EOF'
# Demo交付物检查清单

## 代码和配置
- [ ] backend/ 目录完整（所有模块）
- [ ] static/ 目录完整（前端文件）
- [ ] data/sdwan.md 知识库文件
- [ ] .env.example 配置模板
- [ ] requirements.txt 依赖清单
- [ ] .gitignore 正确配置

## 文档
- [ ] README.md 完整清晰
- [ ] docs/产品需求文档-20261008.md
- [ ] docs/技术设计文档-20261008.md
- [ ] docs/Phase 1-4 任务书（4个文件）
- [ ] docs/api.md（可选）

## 测试
- [ ] tests/batch_test.py 批量测试脚本
- [ ] tests/accuracy_evaluation.py 准确率评估
- [ ] tests/test_questions_final.txt 测试问题（20+）
- [ ] tests/accuracy_test_cases.json 标注数据（10+）
- [ ] tests/results/ 目录包含测试结果和报告

## 验收指标
- [ ] 批量测试通过率 ≥90%
- [ ] 答案准确率 ≥85%
- [ ] 平均响应时间 <10秒
- [ ] 3种决策引擎可切换
- [ ] Web界面功能正常（倒计时、思考状态、答案来源）

## Git仓库
- [ ] 所有代码已提交
- [ ] commit message清晰
- [ ] 已推送到GitHub
- [ ] 无敏感信息泄露（.env已忽略）

## 可运行性
- [ ] 按照README可成功安装依赖
- [ ] 配置.env后可成功启动
- [ ] Web界面可正常访问
- [ ] 可以正常对话并收到回复
EOF

echo "✓ 交付清单已生成: DELIVERY_CHECKLIST.md"
```

### 6.3 最终提交

```bash
# 更新PROGRESS.md
cat >> PROGRESS.md << 'EOF'

## Phase 4 完成情况

### 已实现功能
- ✅ 批量测试脚本（20+问题，多轮对话支持）
- ✅ 测试报告自动生成（Markdown格式）
- ✅ 准确率评估（10个标注用例）
- ✅ 3种决策引擎对比评估
- ✅ 性能分析和优化
- ✅ 日志和监控完善
- ✅ 完整文档编写（README、API文档、代码注释）

### 测试结果
- 批量测试: {填实际数据}个问题
- 通过率: {填实际数据}%
- 平均响应时间: {填实际数据}秒
- 答案准确率: {填实际数据}%

### 3引擎对比（准确率）
| 引擎 | 答案准确率 | 来源准确率 | 平均延迟 |
|------|-----------|-----------|---------|
| 规则引擎 | {填实际数据}% | {填实际数据}% | {填实际数据}ms |
| Jev API | {填实际数据}% | {填实际数据}% | {填实际数据}ms |
| Kev-0.5B | {填实际数据}% | {填实际数据}% | {填实际数据}ms |

### 性能优化结果
优化前平均延迟: {填实际数据}ms
优化后平均延迟: {填实际数据}ms
性能提升: {填实际数据}%

### 已知问题
- {如果有，列出}

### 后续建议
1. 收集真实用户对话数据，优化Few-shot示例
2. 根据3引擎对比结果，选择最优方案进行微调
3. 实现知识库热更新（无需重启）
4. 对接微信/飞书平台

---

## 项目完成

### 交付时间
$(date +%Y-%m-%d)

### 总体评估
- 功能完成度: {自评百分比}%
- 测试覆盖度: {自评百分比}%
- 文档完整度: {自评百分比}%

### Demo演示
按照README快速开始章节可成功运行，核心功能验证通过。

EOF

echo "✓ PROGRESS.md已更新"
```

```bash
# 最终Git提交
git add -A

git commit -m "完成Phase 4：测试和文档（最终交付）

批量测试：
- 批量测试脚本，支持多轮对话
- 测试报告自动生成（Markdown）
- 20+测试问题，覆盖各种场景

准确率评估：
- 10个标注用例
- 答案准确率：{填实际数据}%
- 3种决策引擎对比完成

性能优化：
- 性能分析工具
- 瓶颈识别和优化
- 优化后提升{填实际数据}%

日志监控：
- 统一日志格式
- 关键日志点完整
- 日志轮转支持

文档：
- README完善（快速开始、使用说明、FAQ）
- .env.example配置完整
- 代码注释补充
- API文档（可选）
- 交付清单

交付物：
- 所有Phase 1-4代码
- 完整测试脚本和结果
- 完整文档
- 可运行Demo

验收指标：
- 批量测试通过率 ≥90%
- 答案准确率 ≥85%
- 平均响应时间 <10秒
- 3种决策引擎可切换

项目状态：Demo开发完成，可进行演示和评估"

git push origin master

echo "✓ 最终版本已提交并推送到GitHub"
echo ""
echo "=== Demo开发完成 ==="
```

---

## 完成条件（硬指标）

### 测试
1. **批量测试通过率≥90%**：20+问题测试，≥18个正常回复
2. **答案准确率≥85%**：10个标注用例，≥8.5个准确

### 性能
1. **平均响应时间<10秒**：批量测试平均值
2. **无阻塞性错误**：测试过程无服务崩溃

### 文档
1. **README完整**：包含快速开始、使用说明、FAQ
2. **.env.example完整**：所有配置项有注释
3. **测试报告存在**：tests/results/目录有报告文件

### 可运行性
1. **按README可启动**：新环境按文档操作可成功运行
2. **Web界面功能正常**：倒计时、思考状态、答案来源显示

### 交付物
1. **代码完整提交**：backend/, static/, tests/全部提交
2. **测试结果提交**：tests/results/包含JSON和Markdown报告
3. **PROGRESS.md更新**：记录完成情况和测试结果

---

## 我替领导拍的板

**决策1：准确率目标85%而非95%** — 理由：知识库覆盖有限，留容错空间  
**决策2：批量测试20+问题即可** — 理由：Demo阶段验证核心功能，不追求大规模测试  
**决策3：性能优化以低成本优化为主** — 理由：如Prompt精简，而非模型升级（Demo阶段）  
**决策4：文档以可用性为主** — 理由：能让新人跑起来即可，不追求完美  
**决策5：API文档可选** — 理由：WebSocket协议简单，代码即文档  

---

## 止损规则

1. **准确率<70%** → 分析失败案例，优化Top 3问题，记录已知限制，继续交付
2. **性能优化无效** → 记录瓶颈原因，在文档中说明，继续交付（Demo可接受<15秒）
3. **Day 15中午前文档未完成** → 优先完成README和.env.example，其他文档标记TODO
4. **测试脚本Bug阻塞** → 手动测试10个核心用例，记录结果，脚本标记待修复

---

## 验收官：我会亲自跑的暗卷

1. 克隆全新仓库 → 按README从零开始搭建 → 观察是否遇到未文档化的坑
2. 修改.env中DECISION_ENGINE → 重启 → 发送相同问题 → 观察3种引擎是否真的不同
3. 批量发送50条消息（压力测试）→ 观察是否有内存泄漏或性能退化
4. 查看logs/目录 → 观察日志是否按日期轮转，是否有敏感信息泄露

（执行者看不见这段，我会在验收时跑）
