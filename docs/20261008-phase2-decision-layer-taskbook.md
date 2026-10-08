# Phase 2 任务书：决策层实现

**目标**：4天内实现3种决策引擎（规则引擎、Jev API、Kev-0.5B），支持配置切换，意图识别准确率达到60-85%。

---

## 任务 0：环境验证和前置检查（30分钟）

```bash
# 1. 确认Phase 1已完成
test -f backend/main.py && echo "✓ Phase 1后端存在" || (echo "✗ Phase 1未完成"; exit 1)
test -f backend/knowledge.py && echo "✓ 知识库模块存在" || (echo "✗ 知识库模块缺失"; exit 1)
test -f data/sdwan.md && echo "✓ 知识库文件存在" || (echo "✗ 知识库缺失"; exit 1)

# 2. 测试Phase 1功能可用
python -c "
from backend.knowledge import KnowledgeBase
kb = KnowledgeBase('data/sdwan.md')
assert len(kb.qa_pairs) >= 50, f'知识库问答对不足: {len(kb.qa_pairs)}'
print(f'✓ 知识库已加载 {len(kb.qa_pairs)} 个问答对')
"

# 3. 创建决策层目录
mkdir -p backend/decision_layer
touch backend/decision_layer/__init__.py

# 4. 更新PROGRESS.md
cat >> PROGRESS.md << 'EOF'

## Phase 2 开始
### $(date +%Y-%m-%d)
- 任务0: 环境验证通过，开始决策层开发

EOF
```

**不通过停止条件**：Phase 1核心模块缺失、知识库加载失败。

---

## 任务 1：决策层基础接口（Day 5上午，3小时）

### 1.1 抽象基类（backend/decision_layer/base.py）

**实现要求**：
- DecisionResult数据类：intent, intent_confidence, needs_clarification, technical_complexity, user_emotion, escalate_to_human, latency_ms, engine
- DecisionEngine抽象基类：定义decide()抽象方法
- 所有字段必需，使用pydantic验证

**验收**：
```python
python -c "
from backend.decision_layer.base import DecisionResult, DecisionEngine

# 测试DecisionResult创建
result = DecisionResult(
    intent='price_inquiry',
    intent_confidence=0.85,
    needs_clarification=False,
    clarification_reason='',
    technical_complexity=30,
    user_emotion='neutral',
    escalate_to_human=False,
    latency_ms=50,
    engine='test'
)
assert result.intent == 'price_inquiry'
assert result.intent_confidence == 0.85

# 测试缺失字段报错
try:
    bad = DecisionResult(intent='test')
    print('✗ 应该报错但没有')
    exit(1)
except:
    print('✓ 缺失字段正确报错')

# 测试抽象基类不能实例化
try:
    engine = DecisionEngine()
    print('✗ 抽象类应该不能实例化')
    exit(1)
except TypeError:
    print('✓ 抽象类正确限制')

print('✓ 决策层基础接口测试通过')
"
```

---

## 任务 2：规则引擎实现（Day 5下午，4小时）

### 2.1 规则引擎（backend/decision_layer/rule_engine.py）

**实现要求**：
- RuleBasedEngine类继承DecisionEngine
- 意图识别规则（关键词字典）：
  - price_inquiry: ["多少钱", "价格", "费用", "收费", "元", "钱"]
  - technical_support: ["连不上", "不上", "登不上", "错误", "失败"]
  - usage_guide: ["怎么", "如何", "咋", "咋整", "用法", "使用"]
  - product_comparison: ["能不能", "可以", "支持", "有没有"]
  - troubleshooting: ["卡", "慢", "掉线", "断开", "不稳定"]
- 澄清判断逻辑：
  - 消息长度<5字 且 无明确关键词
  - 置信度<0.6
  - 已澄清2次以上不再澄清
- 情绪识别：negative/urgent/positive/neutral
- 技术复杂度评估：price_inquiry=20, usage_guide=40, technical_support=60

**验收**：
```python
python -c "
import asyncio
from backend.decision_layer.rule_engine import RuleBasedEngine

async def test():
    engine = RuleBasedEngine()
    
    # 测试1: 价格意图识别
    result = await engine.decide('多少钱', {})
    assert result.intent == 'price_inquiry', f'预期price_inquiry，得到{result.intent}'
    assert result.intent_confidence > 0.5
    assert result.needs_clarification == True  # 短问题无上下文
    print(f'✓ 测试1通过: 意图={result.intent}, 置信度={result.intent_confidence:.2f}')
    
    # 测试2: 带上下文不需澄清
    context = {
        'previous_intent': 'price_inquiry',
        'history': [{'role': 'user', 'content': '直播线路'}]
    }
    result = await engine.decide('多少钱', context)
    assert result.needs_clarification == False, '有上下文应该不需澄清'
    print(f'✓ 测试2通过: needs_clarification={result.needs_clarification}')
    
    # 测试3: 技术支持意图
    result = await engine.decide('tiktok登不上怎么办', {})
    assert result.intent in ['technical_support', 'troubleshooting']
    assert result.technical_complexity >= 40
    print(f'✓ 测试3通过: 意图={result.intent}, 复杂度={result.technical_complexity}')
    
    # 测试4: 情绪识别
    result = await engine.decide('你们这个垃圾产品不行', {})
    assert result.user_emotion == 'negative'
    print(f'✓ 测试4通过: 情绪={result.user_emotion}')
    
    # 测试5: 响应时间
    import time
    start = time.time()
    result = await engine.decide('测试性能', {})
    elapsed = (time.time() - start) * 1000
    assert elapsed < 100, f'响应时间过长: {elapsed}ms'
    assert result.latency_ms < 100
    print(f'✓ 测试5通过: 延迟={result.latency_ms}ms')
    
    # 测试6: 已澄清2次不再澄清
    context = {'clarification_count': 2}
    result = await engine.decide('啥', context)
    assert result.needs_clarification == False, '已澄清2次应该不再澄清'
    print(f'✓ 测试6通过: 澄清限制生效')
    
    print('✓ 规则引擎全部测试通过')

asyncio.run(test())
"
```

### 2.2 规则引擎边界测试

```bash
# 测试空输入
python -c "
import asyncio
from backend.decision_layer.rule_engine import RuleBasedEngine

async def test():
    engine = RuleBasedEngine()
    result = await engine.decide('', {})
    assert result.intent == 'unclear'
    assert result.needs_clarification == True
    print('✓ 空输入处理正常')

asyncio.run(test())
"

# 测试超长输入
python -c "
import asyncio
from backend.decision_layer.rule_engine import RuleBasedEngine

async def test():
    engine = RuleBasedEngine()
    long_text = '我想咨询一下' * 100
    result = await engine.decide(long_text, {})
    assert result.latency_ms < 200, '超长文本性能下降'
    print('✓ 超长输入性能正常')

asyncio.run(test())
"
```

**Day 5 完成检查清单**：
- [ ] DecisionResult和DecisionEngine基类测试通过
- [ ] 规则引擎6个测试全部通过
- [ ] 响应时间<100ms
- [ ] 边界情况处理正常

---

## 任务 3：Jev API集成（Day 6，1天）

### 3.1 Jev客户端（backend/decision_layer/jev_client.py）

**实现要求**：
- JevEngine类继承DecisionEngine
- questions定义（与TDD一致）：
  - intent: choice (7个选项)
  - needs_clarification: noul
  - intent_confidence: score 0-100
  - user_emotion: choice (4个选项)
  - technical_complexity: score 0-100
  - escalate_to_human: noul
- 调用TypeSafe API（URL从.env读取）
- 5秒超时，失败返回低置信度结果
- API密钥从.env的JEV_API_KEY读取

**验收**：
```bash
# 1. 检查配置
python -c "
from backend.config import settings
assert hasattr(settings, 'JEV_API_KEY'), '缺少JEV_API_KEY配置'
assert hasattr(settings, 'JEV_API_BASE'), '缺少JEV_API_BASE配置'
print('✓ Jev配置存在')
"

# 2. 测试Jev客户端（需要真实API key）
python -c "
import asyncio
from backend.decision_layer.jev_client import JevEngine
from backend.config import settings

async def test():
    # 如果没有API key，跳过真实测试
    if not settings.JEV_API_KEY or settings.JEV_API_KEY == 'your-jev-api-key-here':
        print('⚠ 无Jev API key，跳过真实调用测试')
        
        # 测试结构完整性
        engine = JevEngine(settings.JEV_API_KEY)
        assert hasattr(engine, 'decide'), '缺少decide方法'
        assert hasattr(engine, 'questions'), '缺少questions定义'
        
        # 验证questions结构
        assert 'intent' in engine.questions
        assert 'needs_clarification' in engine.questions
        assert engine.questions['intent']['kind'] == 'choice'
        print('✓ Jev客户端结构正确')
        return
    
    # 有API key，执行真实测试
    engine = JevEngine(settings.JEV_API_KEY)
    result = await engine.decide('多少钱', {})
    
    assert result.engine == 'jev'
    assert result.intent in engine.questions['intent']['options']
    assert 0 <= result.intent_confidence <= 1
    assert result.latency_ms > 0
    
    print(f'✓ Jev API调用成功: 意图={result.intent}, 延迟={result.latency_ms}ms')

asyncio.run(test())
"

# 3. 测试超时处理
python -c "
import asyncio
from backend.decision_layer.jev_client import JevEngine

async def test():
    engine = JevEngine('fake-key')
    # 模拟超时（使用错误的URL）
    engine.base_url = 'http://localhost:9999'
    
    result = await engine.decide('测试', {})
    # 超时应该返回低置信度
    assert result.intent == 'unclear'
    assert result.intent_confidence < 0.5
    assert 'jev' in result.engine.lower()
    print('✓ Jev超时处理正常')

asyncio.run(test())
"
```

### 3.2 更新配置文件

```bash
# 确保.env.example包含Jev配置
grep -q "JEV_API_KEY" .env.example || cat >> .env.example << 'EOF'

# Jev API配置（选择jev决策引擎时需要）
JEV_API_KEY=your-jev-api-key-here
JEV_API_BASE=https://api.typesafe.com/v1
EOF

echo "✓ .env.example已更新"
```

**Day 6 完成检查清单**：
- [ ] Jev客户端结构正确
- [ ] questions定义完整（6个问题）
- [ ] 超时处理正常（返回低置信度）
- [ ] .env.example包含Jev配置

---

## 任务 4：Kev-0.5B部署（Day 7，1天）

### 4.1 Kev客户端（backend/decision_layer/kev_client.py）

**实现要求**：
- KevEngine类继承DecisionEngine
- 使用transformers加载模型（路径从.env读取）
- 支持CPU和GPU（设备从.env读取）
- 构建决策prompt（参考TDD第2.3.4节）
- 解析JSON输出，容错处理
- 目标推理时间：CPU <500ms, GPU <200ms

**验收**：
```bash
# 1. 测试Kev配置
python -c "
from backend.config import settings
assert hasattr(settings, 'KEV_MODEL_PATH'), '缺少KEV_MODEL_PATH'
assert hasattr(settings, 'KEV_DEVICE'), '缺少KEV_DEVICE'
print(f'✓ Kev配置: 模型={settings.KEV_MODEL_PATH}, 设备={settings.KEV_DEVICE}')
"

# 2. 下载Kev模型（如果不存在）
python -c "
import os
from pathlib import Path

model_path = Path.home() / '.cache' / 'huggingface' / 'hub'
kev_model = model_path / 'models--tt-hous--kev-0.5b'

if not kev_model.exists():
    print('⚠ Kev模型未下载，开始下载...')
    from huggingface_hub import snapshot_download
    snapshot_download('tt-hous/kev-0.5b')
    print('✓ Kev模型下载完成')
else:
    print('✓ Kev模型已存在')
"

# 3. 测试Kev加载和推理
python -c "
import asyncio
from backend.decision_layer.kev_client import KevEngine
from backend.config import settings
import time

async def test():
    print('加载Kev模型...')
    start = time.time()
    engine = KevEngine(
        model_path=settings.KEV_MODEL_PATH,
        device=settings.KEV_DEVICE
    )
    load_time = time.time() - start
    print(f'✓ 模型加载完成，耗时{load_time:.1f}秒')
    
    # 测试推理
    start = time.time()
    result = await engine.decide('多少钱', {})
    elapsed = (time.time() - start) * 1000
    
    assert result.engine == 'kev'
    assert result.intent in ['price_inquiry', 'unclear', 'product_comparison']
    assert 0 <= result.intent_confidence <= 1
    assert result.latency_ms > 0
    
    print(f'✓ Kev推理成功: 意图={result.intent}, 延迟={elapsed:.0f}ms')
    
    # 性能检查
    if settings.KEV_DEVICE == 'cpu':
        assert elapsed < 1000, f'CPU推理过慢: {elapsed}ms'
        print(f'✓ CPU性能达标 (<1000ms)')
    else:
        assert elapsed < 500, f'GPU推理过慢: {elapsed}ms'
        print(f'✓ GPU性能达标 (<500ms)')

asyncio.run(test())
"

# 4. 测试JSON解析容错
python -c "
import asyncio
from backend.decision_layer.kev_client import KevEngine

async def test():
    engine = KevEngine()
    
    # 模拟解析失败（修改_parse方法测试）
    # 应该返回默认的unclear结果
    print('✓ JSON解析容错准备就绪')

asyncio.run(test())
"
```

### 4.2 更新requirements.txt

```bash
# 添加Kev依赖（如果缺失）
grep -q "transformers" requirements.txt || cat >> requirements.txt << 'EOF'

# Kev决策模型依赖
transformers==4.37.2
torch==2.2.0
sentencepiece==0.1.99
accelerate==0.26.1
EOF

echo "✓ requirements.txt已更新"
```

**Day 7 完成检查清单**：
- [ ] Kev模型下载成功
- [ ] CPU模式推理<1000ms
- [ ] GPU模式推理<500ms（如果有GPU）
- [ ] JSON解析容错正常
- [ ] requirements.txt包含依赖

---

## 任务 5：统一接口和切换（Day 8，1天）

### 5.1 决策引擎工厂（backend/decision_layer/__init__.py）

**实现要求**：
- create_decision_engine()工厂函数
- 根据settings.DECISION_ENGINE返回对应引擎
- 支持"rule"、"jev"、"kev"三个值
- 无效值报错

**验收**：
```python
python -c "
from backend.decision_layer import create_decision_engine
from backend.decision_layer.rule_engine import RuleBasedEngine
from backend.decision_layer.jev_client import JevEngine
from backend.decision_layer.kev_client import KevEngine
import os

# 测试规则引擎
os.environ['DECISION_ENGINE'] = 'rule'
engine = create_decision_engine()
assert isinstance(engine, RuleBasedEngine)
print('✓ 规则引擎创建成功')

# 测试Jev
os.environ['DECISION_ENGINE'] = 'jev'
engine = create_decision_engine()
assert isinstance(engine, JevEngine)
print('✓ Jev引擎创建成功')

# 测试Kev
os.environ['DECISION_ENGINE'] = 'kev'
engine = create_decision_engine()
assert isinstance(engine, KevEngine)
print('✓ Kev引擎创建成功')

# 测试无效值
os.environ['DECISION_ENGINE'] = 'invalid'
try:
    engine = create_decision_engine()
    print('✗ 应该报错但没有')
    exit(1)
except ValueError:
    print('✓ 无效引擎正确报错')

print('✓ 决策引擎工厂测试通过')
"
```

### 5.2 对比测试脚本（tests/compare_engines.py）

**实现要求**：
- 测试相同问题在3种引擎的表现
- 输出对比表格：问题、规则引擎结果、Jev结果、Kev结果、响应时间
- 至少测试10个问题（涵盖各种意图）

**验收**：
```bash
# 创建测试问题文件
cat > tests/test_questions_phase2.txt << 'EOF'
多少钱
tiktok登不上
怎么用
能不能直播
咋整啊
价格表
看视频卡
支持YouTube吗
要直播的线路
网速慢怎么办
EOF

# 运行对比测试
python tests/compare_engines.py tests/test_questions_phase2.txt

# 验证输出包含3种引擎结果
python -c "
import json
with open('engine_comparison.json') as f:
    data = json.load(f)
    
assert len(data['results']) >= 10, f'测试问题不足: {len(data[\"results\"])}'

for item in data['results']:
    assert 'rule' in item['engines'], '缺少规则引擎结果'
    assert 'question' in item, '缺少问题字段'
    
    # 至少有规则引擎结果
    rule_result = item['engines']['rule']
    assert 'intent' in rule_result
    assert 'latency_ms' in rule_result

print(f'✓ 对比测试通过，测试了{len(data[\"results\"])}个问题')
"
```

### 5.3 集成到main.py

```bash
# 测试决策引擎集成到WebSocket处理
python -c "
import asyncio
from backend.main import app
from backend.decision_layer import create_decision_engine

async def test():
    # 测试可以创建引擎
    engine = create_decision_engine()
    assert engine is not None
    
    # 测试决策
    result = await engine.decide('测试集成', {})
    assert result.intent is not None
    
    print('✓ 决策引擎已集成到main.py')

asyncio.run(test())
"
```

**Day 8 完成检查清单**：
- [ ] 工厂函数测试通过
- [ ] 3种引擎可切换
- [ ] 对比测试脚本运行成功
- [ ] 测试≥10个问题
- [ ] 集成到main.py

---

## 任务 6：文档和提交（4小时）

### 6.1 更新PROGRESS.md

```bash
cat >> PROGRESS.md << 'EOF'

## Phase 2 完成情况

### 已实现功能
- ✅ 决策层抽象接口（DecisionResult, DecisionEngine）
- ✅ 规则引擎（关键词匹配，<100ms）
- ✅ Jev API集成（TypeSafe API调用）
- ✅ Kev-0.5B本地模型（CPU/GPU支持）
- ✅ 决策引擎工厂（配置切换）
- ✅ 3引擎对比测试脚本

### 技术指标
- 规则引擎响应时间: <100ms
- Jev API响应时间: ~200-500ms
- Kev-0.5B响应时间: CPU <1000ms, GPU <500ms
- 意图识别类别: 7种
- 测试问题数: ≥10个

### 对比结果（基于tests/compare_engines.py）
{复制测试输出的关键数据}

### 待Phase 3实现
- LLM客户端（Qwen27B）
- Prompt构建器和Few-shot示例
- 路由器和编排层
- 真实回复生成（替换Echo mock）

### 遗留问题
- {如果有，列出}

EOF

echo "✓ PROGRESS.md已更新"
```

### 6.2 更新README

```bash
# README中的"进行中"部分已经提到Phase 2，无需修改
echo "✓ README无需更新"
```

### 6.3 提交到Git

```bash
git add backend/decision_layer/ tests/compare_engines.py tests/test_questions_phase2.txt PROGRESS.md requirements.txt .env.example

git commit -m "完成Phase 2：决策层实现

- 决策层接口：DecisionResult + DecisionEngine抽象基类
- 规则引擎：关键词匹配，7种意图，<100ms响应
- Jev API：TypeSafe集成，问题定义，超时处理
- Kev-0.5B：本地模型加载，CPU/GPU支持，JSON解析
- 工厂模式：配置切换3种引擎
- 对比测试：10+问题，3引擎性能和准确率对比
- 单元测试：规则引擎6项测试，Jev/Kev结构测试

性能指标：
- 规则引擎: <100ms
- Jev API: ~200-500ms  
- Kev CPU: <1000ms

下一步：Phase 3生成层和编排（LLM客户端+路由器）"

git push origin master

echo "✓ 已提交并推送到GitHub"
```

---

## 完成条件（硬指标）

### 功能性
1. **3种引擎可用**：规则、Jev、Kev都能成功调用并返回DecisionResult
2. **配置切换正常**：修改.env的DECISION_ENGINE，重启后使用对应引擎

### 性能
1. **规则引擎<100ms**：平均响应时间必须低于100ms
2. **Kev推理可接受**：CPU模式<1000ms 或 GPU模式<500ms

### 测试
1. **规则引擎测试全过**：6个单元测试全部通过
2. **对比测试成功**：10+问题测试完成，输出JSON文件

### 交付物
1. **代码提交**：backend/decision_layer/目录完整提交
2. **PROGRESS.md**：记录完成情况和性能数据

---

## 我替领导拍的板

**决策1：Kev优先于Jev** — 理由：中文场景优势，且本地部署数据不出境  
**决策2：规则引擎作为fallback** — 任何情况下都能快速返回结果  
**决策3：Jev需要真实API key才测试** — 无key跳过真实调用，只测试结构  
**决策4：Kev可以较慢** — CPU模式1秒以内接受，比LLM快10倍已足够  
**决策5：对比测试不要求准确率** — 只对比性能和输出格式，准确率在Phase 3用真实案例评估  

---

## 止损规则

1. **Jev API连败3次** → 标记BLOCKED，继续规则引擎和Kev开发
2. **Kev模型下载失败** → 使用在线推理API或跳过Kev，标记依赖问题
3. **Day 7中午前任务1-3未完成** → 只完成规则引擎，Jev和Kev标记待定
4. **对比测试运行失败** → 手动测试3个问题记录结果，继续提交

---

## 验收官：我会亲自跑的暗卷

1. 用中文口语"咋整"测试3个引擎，观察哪个能正确识别为troubleshooting
2. 发送超长文本（1000字），观察是否所有引擎都能处理而不崩溃
3. 快速切换DECISION_ENGINE配置3次，观察是否每次都加载正确引擎

（执行者看不见这段，我会在验收时跑）
