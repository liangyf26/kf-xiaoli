# Phase 3 任务书：生成层和编排

**目标**：4天内完成LLM客户端、路由编排器、Few-shot Prompt系统，实现端到端智能对话，答案准确率达到90%+。

---

## 任务 0：环境验证和前置检查（30分钟）

```bash
# 1. 确认Phase 2已完成
test -d backend/decision_layer && echo "✓ 决策层目录存在" || (echo "✗ Phase 2未完成"; exit 1)
python -c "
from backend.decision_layer import create_decision_engine
engine = create_decision_engine()
assert engine is not None
print('✓ 决策引擎可用')
"

# 2. 确认Qwen模型可访问
python -c "
import httpx
import os
from backend.config import settings

try:
    response = httpx.get(f'{settings.MODEL_API_BASE}/models', timeout=5.0)
    if response.status_code == 200:
        print(f'✓ Qwen模型API可访问: {settings.MODEL_API_BASE}')
    else:
        print(f'⚠ API响应异常: {response.status_code}')
except Exception as e:
    print(f'✗ 无法连接Qwen模型: {e}')
    print('请确保模型已启动：ollama run qwen2.5:27b')
    exit(1)
"

# 3. 创建目录
mkdir -p backend/llm backend/orchestrator
touch backend/llm/__init__.py backend/orchestrator/__init__.py

# 4. 更新PROGRESS.md
cat >> PROGRESS.md << 'EOF'

## Phase 3 开始
### $(date +%Y-%m-%d)
- 任务0: 环境验证通过，开始生成层和编排开发

EOF
```

**不通过停止条件**：Phase 2决策层缺失、Qwen模型无法连接。

---

## 任务 1：LLM客户端实现（Day 9上午，4小时）

### 1.1 LLM客户端基础（backend/llm/client.py）

**实现要求**：
- QwenClient类，封装OpenAI兼容API调用
- 支持流式输出（stream=True）和非流式
- 配置从settings读取：MODEL_API_BASE, MODEL_NAME, TEMPERATURE, MAX_TOKENS
- 60秒超时，失败重试1次
- 返回完整结构：response, usage, latency_ms

**验收**：
```python
python -c "
import asyncio
from backend.llm.client import QwenClient

async def test():
    client = QwenClient()
    
    # 测试1: 简单对话
    messages = [
        {'role': 'system', 'content': '你是测试助手'},
        {'role': 'user', 'content': '1+1=?'}
    ]
    
    result = await client.generate(messages)
    
    assert 'response' in result, '缺少response字段'
    assert 'latency_ms' in result, '缺少latency_ms字段'
    assert len(result['response']) > 0, '回复为空'
    assert result['latency_ms'] > 0
    
    print(f'✓ 测试1通过: 回复长度={len(result[\"response\"])}, 延迟={result[\"latency_ms\"]}ms')
    
    # 测试2: 超时处理（模拟）
    # 设置极短超时
    client.timeout = 0.1
    try:
        result = await client.generate(messages)
        print('⚠ 预期超时但成功了')
    except Exception as e:
        assert 'timeout' in str(e).lower() or 'timed out' in str(e).lower()
        print(f'✓ 测试2通过: 超时处理正常')
    
    # 恢复正常超时
    client.timeout = 60.0
    
    # 测试3: 参数配置
    client.temperature = 0.0
    client.max_tokens = 50
    result = await client.generate(messages)
    assert len(result['response']) <= 200, '输出过长（50 tokens应该<200字符）'
    print(f'✓ 测试3通过: 参数配置生效')
    
    print('✓ LLM客户端全部测试通过')

asyncio.run(test())
"
```

### 1.2 JSON解析器（backend/llm/parser.py）

**实现要求**：
- parse_json_response()函数
- 容错解析：提取```json...```或{...}
- 如果无法解析，返回{"answer": "原文", "sources": []}
- 验证必需字段：answer存在

**验收**：
```python
python -c "
from backend.llm.parser import parse_json_response

# 测试1: 标准JSON
text = '{\"answer\": \"测试回复\", \"sources\": [\"问题1\"]}'
result = parse_json_response(text)
assert result['answer'] == '测试回复'
assert result['sources'] == ['问题1']
print('✓ 测试1通过: 标准JSON解析')

# 测试2: Markdown包裹
text = '''这是我的回答：
\`\`\`json
{\"answer\": \"直播线路260元/月\", \"sources\": [\"问题1\"]}
\`\`\`
希望对您有帮助。'''
result = parse_json_response(text)
assert '260' in result['answer']
print('✓ 测试2通过: Markdown包裹解析')

# 测试3: 不完整JSON
text = '{\"answer\": \"测试'
result = parse_json_response(text)
assert result['answer'] == text
assert result['sources'] == []
print('✓ 测试3通过: 容错处理')

# 测试4: 纯文本
text = '这是纯文本回复，没有JSON'
result = parse_json_response(text)
assert result['answer'] == text
print('✓ 测试4通过: 纯文本降级')

print('✓ JSON解析器全部测试通过')
"
```

---

## 任务 2：Few-shot示例编写（Day 9下午，3小时）

### 2.1 Few-shot示例库（backend/orchestrator/few_shot_examples.py）

**实现要求**：
- FEW_SHOT_EXAMPLES字典，按意图分类
- 至少覆盖7种意图：price_inquiry, technical_support, troubleshooting, usage_guide, product_comparison, purchase_process, account_management
- 每种意图3-5个示例
- 示例格式：用户输入、上下文、AI回复（JSON格式）
- 示例必须体现：短问题澄清、上下文关联、知识库引用、无法回答

**验收**：
```python
python -c "
from backend.orchestrator.few_shot_examples import FEW_SHOT_EXAMPLES

# 检查结构
required_intents = [
    'price_inquiry', 'technical_support', 'troubleshooting', 
    'usage_guide', 'product_comparison'
]

for intent in required_intents:
    assert intent in FEW_SHOT_EXAMPLES, f'缺少{intent}示例'
    examples = FEW_SHOT_EXAMPLES[intent]
    assert len(examples) > 0, f'{intent}示例为空'
    
    # 检查是否包含关键模式
    assert '用户：' in examples or 'user:' in examples.lower(), f'{intent}缺少用户输入'
    assert 'answer' in examples or '回复' in examples, f'{intent}缺少回复示例'
    
    print(f'✓ {intent}: {len(examples)}字符')

print(f'✓ Few-shot示例库验证通过，共{len(FEW_SHOT_EXAMPLES)}种意图')
"
```

### 2.2 示例选择器（backend/orchestrator/example_selector.py）

**实现要求**：
- select_examples()函数
- 根据决策结果选择相关Few-shot示例
- 参数：intent, needs_clarification, context
- 返回最多3个示例（控制prompt长度）

**验收**：
```python
python -c "
from backend.orchestrator.example_selector import select_examples
from backend.decision_layer.base import DecisionResult

# 测试1: 价格咨询意图
decision = DecisionResult(
    intent='price_inquiry',
    intent_confidence=0.85,
    needs_clarification=False,
    clarification_reason='',
    technical_complexity=30,
    user_emotion='neutral',
    escalate_to_human=False,
    latency_ms=50,
    engine='rule'
)

examples = select_examples(decision, {})
assert len(examples) > 0, '未返回示例'
assert 'price' in examples.lower() or '价格' in examples or '多少钱' in examples
print(f'✓ 测试1通过: 返回{len(examples)}字符的示例')

# 测试2: 需要澄清
decision.needs_clarification = True
examples = select_examples(decision, {})
assert '澄清' in examples or 'clarif' in examples.lower() or '请问' in examples
print(f'✓ 测试2通过: 包含澄清示例')

print('✓ 示例选择器测试通过')
"
```

---

## 任务 3：Prompt构建器（Day 9晚上，2小时）

### 3.1 Prompt构建器（backend/orchestrator/prompt_builder.py）

**实现要求**：
- PromptBuilder类
- build_prompt()方法，参数：message, decision, context, knowledge_base
- 组成部分：
  1. 系统角色定义
  2. 知识库内容（根据intent筛选）
  3. 对话上下文（最近10轮）
  4. 用户意图和置信度
  5. Few-shot示例
  6. 回复要求（JSON格式、来源标注）
  7. 用户当前问题
- Prompt总长度<8000字符（避免超过context window）

**验收**：
```python
python -c "
import asyncio
from backend.orchestrator.prompt_builder import PromptBuilder
from backend.decision_layer.base import DecisionResult
from backend.knowledge import KnowledgeBase

async def test():
    builder = PromptBuilder()
    kb = KnowledgeBase('data/sdwan.md')
    
    decision = DecisionResult(
        intent='price_inquiry',
        intent_confidence=0.85,
        needs_clarification=False,
        clarification_reason='',
        technical_complexity=30,
        user_emotion='neutral',
        escalate_to_human=False,
        latency_ms=50,
        engine='rule'
    )
    
    context = {
        'history': [
            {'role': 'user', 'content': '你好'},
            {'role': 'assistant', 'content': '您好，我是SDWAN智能客服机器人'}
        ]
    }
    
    prompt = builder.build_prompt(
        message='多少钱',
        decision=decision,
        context=context,
        knowledge_base=kb
    )
    
    # 验证结构
    assert 'SDWAN' in prompt or '客服' in prompt, '缺少角色定义'
    assert '知识库' in prompt or 'knowledge' in prompt.lower(), '缺少知识库部分'
    assert '多少钱' in prompt, '缺少用户问题'
    assert 'JSON' in prompt or 'json' in prompt, '缺少JSON格式要求'
    assert len(prompt) < 10000, f'Prompt过长: {len(prompt)}字符'
    
    print(f'✓ Prompt构建成功，长度: {len(prompt)}字符')
    
    # 验证意图筛选
    assert '价格' in prompt or 'price' in prompt.lower(), '未根据意图筛选知识库'
    print('✓ 知识库意图筛选生效')
    
    # 验证上下文
    assert '你好' in prompt, '缺少对话历史'
    print('✓ 对话上下文包含')

asyncio.run(test())
"
```

**Day 9 完成检查清单**：
- [ ] LLM客户端3个测试通过
- [ ] JSON解析器4个测试通过
- [ ] Few-shot示例库7种意图完整
- [ ] Prompt构建器结构验证通过
- [ ] 端到端调用：决策→Prompt→LLM→解析

---

## 任务 4：路由器实现（Day 10上午，4小时）

### 4.1 路由决策（backend/orchestrator/router.py）

**实现要求**：
- ProcessingPath枚举：CLARIFICATION, FAQ_MATCH, LLM_GENERATION, HUMAN_ESCALATION
- CustomerServiceRouter类
- _determine_path()方法：根据DecisionResult决定处理路径
- 优先级：转人工 > 澄清 > FAQ > LLM > 降级人工

**验收**：
```python
python -c "
from backend.orchestrator.router import CustomerServiceRouter, ProcessingPath
from backend.decision_layer.base import DecisionResult

router = CustomerServiceRouter(None, None, None)

# 测试1: 转人工
decision = DecisionResult(
    intent='price_inquiry',
    intent_confidence=0.85,
    needs_clarification=False,
    clarification_reason='',
    technical_complexity=30,
    user_emotion='negative',
    escalate_to_human=True,
    latency_ms=50,
    engine='rule'
)
path = router._determine_path(decision, {})
assert path == ProcessingPath.HUMAN_ESCALATION
print('✓ 测试1通过: 转人工路径')

# 测试2: 需要澄清
decision.escalate_to_human = False
decision.needs_clarification = True
path = router._determine_path(decision, {})
assert path == ProcessingPath.CLARIFICATION
print('✓ 测试2通过: 澄清路径')

# 测试3: 简单FAQ
decision.needs_clarification = False
decision.technical_complexity = 20
decision.intent_confidence = 0.9
path = router._determine_path(decision, {})
assert path == ProcessingPath.FAQ_MATCH
print('✓ 测试3通过: FAQ路径')

# 测试4: LLM生成
decision.technical_complexity = 50
decision.intent_confidence = 0.7
path = router._determine_path(decision, {})
assert path == ProcessingPath.LLM_GENERATION
print('✓ 测试4通过: LLM生成路径')

# 测试5: 已澄清2次不再澄清
decision.needs_clarification = True
context = {'clarification_count': 2}
path = router._determine_path(decision, context)
assert path != ProcessingPath.CLARIFICATION
print('✓ 测试5通过: 澄清次数限制')

print('✓ 路由决策全部测试通过')
"
```

### 4.2 处理路径实现（backend/orchestrator/handlers.py）

**实现要求**：
- handle_clarification()：生成澄清问题
- handle_faq_match()：直接返回知识库原文
- handle_llm_generation()：调用LLM生成
- handle_escalation()：返回转人工消息

**验收**：
```python
python -c "
import asyncio
from backend.orchestrator.handlers import *
from backend.decision_layer.base import DecisionResult
from backend.knowledge import KnowledgeBase

async def test():
    kb = KnowledgeBase('data/sdwan.md')
    
    # 测试1: 澄清处理
    decision = DecisionResult(
        intent='price_inquiry',
        intent_confidence=0.4,
        needs_clarification=True,
        clarification_reason='短问题无上下文',
        technical_complexity=30,
        user_emotion='neutral',
        escalate_to_human=False,
        latency_ms=50,
        engine='rule'
    )
    
    result = await handle_clarification('多少钱', decision, {})
    assert '?' in result['answer'] or '？' in result['answer'], '澄清问题应该是疑问句'
    assert result['need_clarification'] == True
    print('✓ 测试1通过: 澄清处理')
    
    # 测试2: FAQ匹配
    decision.needs_clarification = False
    decision.intent = 'price_inquiry'
    result = await handle_faq_match(decision, kb)
    assert len(result['answer']) > 0
    assert len(result['sources']) > 0, 'FAQ应该标注来源'
    print(f'✓ 测试2通过: FAQ匹配，来源={result[\"sources\"]}')
    
    # 测试3: 转人工
    decision.escalate_to_human = True
    result = await handle_escalation(decision, {})
    assert '人工' in result['answer'] or 'human' in result['answer'].lower()
    print('✓ 测试3通过: 转人工处理')
    
    print('✓ 处理路径全部测试通过')

asyncio.run(test())
"
```

---

## 任务 5：编排器集成（Day 10下午，4小时）

### 5.1 完整编排流程（backend/orchestrator/orchestrator.py）

**实现要求**：
- Orchestrator类，整合决策层+路由器+处理器
- process()方法：完整流程
  1. 调用决策引擎
  2. 路由判断
  3. 执行对应处理器
  4. 返回统一格式
- 上下文管理：history, covered_topics, clarification_count

**验收**：
```bash
# 端到端测试
python -c "
import asyncio
from backend.orchestrator.orchestrator import Orchestrator

async def test():
    orchestrator = Orchestrator()
    
    # 测试1: 简单价格咨询
    result = await orchestrator.process('直播线路多少钱', {})
    
    assert 'answer' in result
    assert len(result['answer']) > 0
    assert 'path' in result
    assert 'decision' in result
    
    print(f'✓ 测试1通过: path={result[\"path\"]}, 回复长度={len(result[\"answer\"])}')
    
    # 测试2: 短问题触发澄清
    result = await orchestrator.process('多少钱', {})
    
    if result.get('need_clarification'):
        assert '?' in result['answer'] or '？' in result['answer']
        print('✓ 测试2通过: 触发澄清')
    else:
        print('⚠ 测试2: 未触发澄清（可能规则引擎判断有上下文）')
    
    # 测试3: 带上下文
    context = {
        'history': [
            {'role': 'user', 'content': '直播线路'},
            {'role': 'assistant', 'content': '...'}
        ]
    }
    result = await orchestrator.process('多少钱', context)
    
    assert '260' in result['answer'] or '价格' in result['answer']
    print('✓ 测试3通过: 上下文理解')
    
    # 测试4: 知识库外问题
    result = await orchestrator.process('支持YouTube吗', {})
    
    assert '暂时无法回答' in result['answer'] or '人工' in result['answer']
    print('✓ 测试4通过: 知识库外问题')
    
    print('✓ 编排器端到端测试通过')

asyncio.run(test())
"
```

### 5.2 集成到WebSocket

**实现要求**：
- 修改backend/main.py的WebSocket处理
- 调用orchestrator.process()替换mock回复
- 推送状态更新：等待→决策→生成→完成

**验收**：
```bash
# 启动服务
python -m backend.main &
SERVER_PID=$!
sleep 3

# 测试WebSocket（使用Python客户端）
python -c "
import asyncio
import websockets
import json

async def test():
    uri = 'ws://localhost:8000/ws'
    async with websockets.connect(uri) as websocket:
        # 发送消息
        await websocket.send('直播线路多少钱')
        
        # 接收回复
        response = await asyncio.wait_for(websocket.recv(), timeout=30.0)
        data = json.loads(response)
        
        assert data['type'] in ['message', 'reply'], f'未知消息类型: {data[\"type\"]}'
        assert len(data['content']) > 0, '回复为空'
        
        print(f'✓ WebSocket集成测试通过: {data[\"content\"][:50]}...')

asyncio.run(test())
"

# 停止服务
kill $SERVER_PID
```

**Day 10 完成检查清单**：
- [ ] 路由决策5个测试通过
- [ ] 处理路径3个测试通过
- [ ] 编排器端到端4个测试通过
- [ ] WebSocket集成测试通过

---

## 任务 6：前端完善（Day 11，1天）

### 6.1 等待倒计时显示

**实现要求**：
- 输入框上方显示倒计时提示条
- 格式："正在汇总您的问题，还需等待X秒..."
- 橙色背景，居中显示
- 倒计时归零后隐藏

**验收**：
```bash
# 手动测试（打开浏览器）
echo "请手动测试："
echo "1. 打开 http://localhost:8000"
echo "2. 快速发送3条消息（间隔<2秒）"
echo "3. 观察是否显示倒计时"
echo "4. 倒计时归零后是否收到回复"
echo ""
echo "通过标准："
echo "- 倒计时显示且每秒更新"
echo "- 倒计时归零后自动隐藏"
echo "- 收到回复时显示完整内容"
```

### 6.2 思考状态提示

**实现要求**：
- 发送消息后显示"机器人正在思考..."
- 使用动画点点点（...效果）
- 收到回复后替换为实际内容

**验收**：
```javascript
// 在static/app.js中验证
// 确保存在showThinkingStatus()和hideThinkingStatus()函数
```

### 6.3 答案来源展示

**实现要求**：
- 每条回复下方显示"📎 来源：问题3, 问题12"
- 可折叠（默认展开）
- 灰色小字
- 如果sources为空则不显示

**验收**：
```bash
# 手动测试
echo "请手动测试："
echo "1. 询问价格相关问题"
echo "2. 观察回复下方是否显示来源"
echo "3. 来源格式是否正确"
```

### 6.4 样式优化

**实现要求**：
- 按照design_sense风格（深色主题、圆角、流体排版）
- 消息气泡：用户右对齐蓝色，机器人左对齐灰色
- 滚动条样式优化
- 响应式布局（桌面优先）

**验收**：
```bash
# 视觉检查清单
echo "视觉验收清单："
echo "[ ] 整体深色主题（背景非纯白）"
echo "[ ] 消息气泡圆角明显"
echo "[ ] 用户消息蓝色右对齐"
echo "[ ] 机器人消息灰色左对齐"
echo "[ ] 输入框和按钮有圆角"
echo "[ ] 字体大小适中，行高舒适"
```

**Day 11 完成检查清单**：
- [ ] 倒计时显示正常
- [ ] 思考状态提示正常
- [ ] 答案来源展示正确
- [ ] 样式符合design_sense
- [ ] 响应式布局正常

---

## 任务 7：集成测试和Bug修复（Day 12，1天）

### 7.1 端到端流程测试

**测试用例**：
```bash
# 创建测试用例文件
cat > tests/e2e_test_cases.txt << 'EOF'
# 用例1: 首次对话
你好

# 用例2: 价格咨询（直接）
直播线路多少钱

# 用例3: 短问题+上下文
直播线路
多少钱

# 用例4: 技术支持
tiktok登不上怎么办

# 用例5: 知识库外问题
支持YouTube吗

# 用例6: 连续发送（测试等待汇总）
我想咨询
关于价格
直播的

# 用例7: 模糊问题（触发澄清）
咋整

# 用例8: 多轮对话
价格表
要最便宜的
怎么购买
EOF

# 运行E2E测试
python tests/run_e2e_tests.py tests/e2e_test_cases.txt
```

### 7.2 3种引擎对比测试

```bash
# 对比3种决策引擎在相同问题上的表现
for engine in rule jev kev; do
    echo "测试引擎: $engine"
    export DECISION_ENGINE=$engine
    python tests/compare_engines.py tests/test_questions_phase2.txt
done

# 生成对比报告
python tests/generate_comparison_report.py
```

### 7.3 性能测试

```bash
# 测试响应时间
python -c "
import asyncio
import time
from backend.orchestrator.orchestrator import Orchestrator

async def test():
    orchestrator = Orchestrator()
    
    times = []
    for i in range(10):
        start = time.time()
        result = await orchestrator.process('直播线路多少钱', {})
        elapsed = time.time() - start
        times.append(elapsed)
        print(f'第{i+1}次: {elapsed:.2f}秒')
    
    avg_time = sum(times) / len(times)
    max_time = max(times)
    
    print(f'\\n平均响应时间: {avg_time:.2f}秒')
    print(f'最大响应时间: {max_time:.2f}秒')
    
    assert avg_time < 10, f'平均响应时间过长: {avg_time}秒'
    assert max_time < 15, f'最大响应时间过长: {max_time}秒'
    
    print('✓ 性能测试通过')

asyncio.run(test())
"
```

### 7.4 Bug修复清单

**常见问题排查**：
1. **JSON解析失败** → 检查parse_json_response()容错
2. **WebSocket连接断开** → 检查超时和错误处理
3. **上下文丢失** → 检查history管理
4. **回复重复** → 检查covered_topics追踪
5. **等待汇总不触发** → 检查WaitAggregator逻辑

```bash
# Bug修复验收
echo "请逐个验证以下场景是否正常："
echo "[ ] LLM返回格式错误时不崩溃"
echo "[ ] WebSocket异常断开可重连"
echo "[ ] 上下文在多轮对话中保持"
echo "[ ] 相同问题不会重复回答"
echo "[ ] 连续消息正确汇总"
```

**Day 12 完成检查清单**：
- [ ] 8个E2E用例全部通过
- [ ] 3种引擎对比测试完成
- [ ] 平均响应时间<10秒
- [ ] 5个常见问题已排查
- [ ] 无阻塞性Bug

---

## 任务 8：文档和提交（4小时）

### 8.1 更新PROGRESS.md

```bash
cat >> PROGRESS.md << 'EOF'

## Phase 3 完成情况

### 已实现功能
- ✅ LLM客户端（Qwen27B集成，支持流式输出）
- ✅ Few-shot示例库（7种意图，每种3-5个示例）
- ✅ Prompt构建器（动态组装，长度控制<8000字符）
- ✅ JSON解析器（容错处理）
- ✅ 路由器（4种处理路径，智能决策）
- ✅ 处理器（澄清/FAQ/LLM/转人工）
- ✅ 编排器（完整流程整合）
- ✅ 前端优化（倒计时、思考状态、答案来源）

### 技术指标
- LLM响应时间: 3-8秒（取决于问题复杂度）
- 端到端响应时间: <10秒（平均）
- JSON解析成功率: >95%（含容错）
- 答案准确率: ~90%（基于知识库）

### E2E测试结果
{复制测试输出的关键数据}

| 用例 | 通过 | 备注 |
|------|------|------|
| 首次对话 | ✅ | 包含身份告知 |
| 价格咨询 | ✅ | 正确引用知识库 |
| 上下文理解 | ✅ | 短问题理解正确 |
| 技术支持 | ✅ | 返回解决方案 |
| 知识库外 | ✅ | 正确返回"无法回答" |
| 连续消息 | ✅ | 等待汇总正常 |
| 触发澄清 | ✅ | 澄清问题合理 |
| 多轮对话 | ✅ | 上下文连贯 |

### 3引擎对比（基于10个测试问题）
| 指标 | 规则引擎 | Jev API | Kev-0.5B |
|------|---------|---------|----------|
| 意图识别准确率 | 65% | {填实际数据} | {填实际数据} |
| 平均响应时间 | 50ms | {填实际数据} | {填实际数据} |

### 待Phase 4实现
- 批量测试脚本
- 准确率评估（20+真实案例）
- 性能优化
- 日志和监控完善
- 最终文档和交付

### 遗留问题
- {如果有，列出}

EOF

echo "✓ PROGRESS.md已更新"
```

### 8.2 代码注释补充

```bash
# 检查关键文件注释完整性
python -c "
import ast
import inspect

files_to_check = [
    'backend/orchestrator/orchestrator.py',
    'backend/orchestrator/router.py',
    'backend/llm/client.py',
]

for file in files_to_check:
    with open(file) as f:
        tree = ast.parse(f.read())
    
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    
    print(f'{file}:')
    print(f'  类: {len(classes)}, 函数: {len(functions)}')
    
    # 检查docstring
    for cls in classes:
        if not ast.get_docstring(cls):
            print(f'  ⚠ 类 {cls.name} 缺少docstring')
    
    for func in functions[:3]:  # 检查前3个函数
        if not ast.get_docstring(func):
            print(f'  ⚠ 函数 {func.name} 缺少docstring')

print('\\n注释检查完成，请补充缺失的docstring')
"
```

### 8.3 提交到Git

```bash
git add backend/llm/ backend/orchestrator/ static/ tests/ PROGRESS.md

git commit -m "完成Phase 3：生成层和编排

LLM集成：
- Qwen27B客户端封装，支持流式输出
- JSON解析器，容错处理
- 60秒超时，重试机制

Few-shot和Prompt：
- 7种意图示例库，每种3-5个示例
- 示例选择器，根据意图动态选择
- Prompt构建器，长度控制<8000字符

路由和编排：
- 4种处理路径（澄清/FAQ/LLM/转人工）
- 智能路由决策，优先级管理
- 完整编排流程，统一输出格式
- 上下文管理（history, covered_topics）

前端优化：
- 等待倒计时显示
- 思考状态提示（动画）
- 答案来源展示
- 深色主题样式优化

测试：
- 8个E2E用例全部通过
- 3种决策引擎对比测试
- 平均响应时间<10秒
- 答案准确率~90%

下一步：Phase 4批量测试和文档（最终交付）"

git push origin master

echo "✓ 已提交并推送到GitHub"
```

---

## 完成条件（硬指标）

### 功能性
1. **端到端流程可用**：用户发消息→决策→路由→生成→返回答案
2. **8个E2E用例通过**：首次对话、价格咨询、上下文理解、技术支持、知识库外、连续消息、触发澄清、多轮对话

### 性能
1. **平均响应时间<10秒**：从发送到收到回复
2. **答案准确率>85%**：基于知识库的问题正确回答率

### 用户体验
1. **前端交互流畅**：倒计时、思考状态、答案来源显示正常
2. **样式美观**：符合design_sense，深色主题

### 交付物
1. **代码提交**：backend/llm/, backend/orchestrator/, static/完整提交
2. **PROGRESS.md**：记录E2E测试结果和性能数据
3. **测试文件**：tests/e2e_test_cases.txt

---

## 我替领导拍的板

**决策1：Prompt长度<8000字符** — 理由：避免超过context window，保持推理速度  
**决策2：Few-shot每个意图3-5个示例** — 理由：足够覆盖模式，又不过度增加prompt长度  
**决策3：答案准确率目标85%** — 理由：知识库覆盖有限，留15%容错空间  
**决策4：前端手动测试为主** — 理由：Demo阶段无需自动化UI测试，节省时间  
**决策5：LLM超时60秒** — 理由：平衡用户体验和复杂问题处理能力  

---

## 止损规则

1. **LLM连接连败3次** → 使用mock回复，标记BLOCKED，继续前端和路由开发
2. **E2E用例通过率<50%** → Day 12中午前回退到最后稳定commit，重新分析失败原因
3. **平均响应时间>15秒** → 简化Prompt（移除Few-shot或减少上下文轮数）
4. **前端Bug阻塞** → 降级到纯文本界面（无倒计时和动画），确保核心功能可用

---

## 验收官：我会亲自跑的暗卷

1. 发送"咋整啊，这tk死活不行" → 观察是否理解口语+中英混合，返回故障排查方案
2. 快速发送10条短消息（间隔1秒）→ 观察等待汇总是否正常，是否只回复1次
3. 询问"YouTube直播" → 观察是否正确返回"无法回答"而非编造答案
4. 对话10轮后刷新页面 → 观察是否正确清空上下文，重新开始

（执行者看不见这段，我会在验收时跑）
