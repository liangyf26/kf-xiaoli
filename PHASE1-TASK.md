# Phase 1 任务书：基础架构搭建

**目标**：4天内完成FastAPI后端框架、知识库模块和Web界面，能跑通"用户发消息→等待15秒→知识库查询→返回回复"的完整流程。

---

## 任务 0：环境验证（30分钟，不通过就停）

```bash
# 1. Python版本检查（必须3.10+）
python --version

# 2. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install fastapi==0.109.0 uvicorn[standard]==0.27.0 websockets==12.0 python-dotenv==1.0.1 pydantic==2.5.3

# 3. 验证安装
python -c "import fastapi; import uvicorn; print('✓ 依赖安装成功')"

# 4. 检查知识库文件存在
test -f sdwan.md && echo "✓ 知识库存在" || echo "✗ 知识库不存在"

# 5. 创建 PROGRESS.md 记录进度
echo "# Phase 1 进度记录\n\n## $(date +%Y-%m-%d)\n- 任务0: 环境验证通过" > PROGRESS.md
```

**不通过停止条件**：Python版本<3.10、依赖安装失败、sdwan.md不存在。

---

## 任务 1：后端核心框架（Day 1-2，2天）

### 1.1 创建目录结构

```bash
mkdir -p backend/decision_layer backend/orchestrator backend/llm static data logs tests
touch backend/__init__.py backend/decision_layer/__init__.py backend/orchestrator/__init__.py backend/llm/__init__.py
```

### 1.2 配置管理（backend/config.py）

**实现要求**：
- 用pydantic Settings加载.env文件
- 必需字段：MODEL_API_BASE, MODEL_NAME, CONTEXT_TURNS, WAIT_SLIDE_SECONDS, WAIT_MAX_SECONDS, KNOWLEDGE_BASE_PATH, DECISION_ENGINE
- 缺失必需字段时启动报错，不许静默使用默认值

**验收**：
```bash
# 1. 创建测试配置文件
echo "MODEL_API_BASE=http://test
MODEL_NAME=test-model
CONTEXT_TURNS=10
WAIT_SLIDE_SECONDS=15
WAIT_MAX_SECONDS=30
KNOWLEDGE_BASE_PATH=./sdwan.md
DECISION_ENGINE=rule
FIRST_MESSAGE_GREETING=测试问候
NO_ANSWER_MESSAGE=测试拒答" > .env.test

# 2. 测试配置加载
python -c "
import sys
sys.path.insert(0, '.')
import os
os.environ['ENV_FILE'] = '.env.test'
from backend.config import settings
assert settings.MODEL_API_BASE == 'http://test', '配置加载失败'
assert settings.CONTEXT_TURNS == 10, '数字配置解析失败'
print('✓ 配置加载测试通过')
"

# 3. 测试缺失必需字段报错
echo "MODEL_API_BASE=http://test" > .env.broken
python -c "
import os
os.environ['ENV_FILE'] = '.env.broken'
try:
    from backend.config import settings
    print('✗ 应该报错但没有')
    exit(1)
except:
    print('✓ 缺失字段正确报错')
"

rm .env.test .env.broken
```

### 1.3 数据模型（backend/models.py）

**实现要求**：
- 用pydantic BaseModel定义Message, ConversationContext, WaitingQueue, SessionState
- Message必须有role(user/assistant), content, timestamp字段
- SessionState必须有session_id, context, waiting_queue, is_first_message字段

**验收**：
```python
python -c "
from backend.models import Message, SessionState, ConversationContext, WaitingQueue
from datetime import datetime

# 测试Message创建
msg = Message(role='user', content='测试', timestamp=datetime.now())
assert msg.role == 'user'

# 测试SessionState创建
session = SessionState(
    session_id='test123',
    context=ConversationContext(history=[], covered_topics=[], user_needs={}),
    waiting_queue=WaitingQueue(messages=[]),
    connection=None
)
assert session.session_id == 'test123'
assert session.is_first_message == True

print('✓ 数据模型测试通过')
"
```

### 1.4 WebSocket连接管理器（backend/connection_manager.py）

**实现要求**：
- ConnectionManager类管理多个WebSocket连接
- connect()方法接受连接并生成session_id（用uuid4）
- disconnect()方法清理会话并取消等待定时器
- send_message()方法发送JSON消息
- 每个连接维护独立的SessionState

**验收**：
```python
python -c "
import asyncio
from backend.connection_manager import ConnectionManager
from backend.models import SessionState

# 模拟WebSocket对象
class MockWebSocket:
    def __init__(self):
        self.messages = []
    async def accept(self):
        pass
    async def send_json(self, data):
        self.messages.append(data)

async def test():
    manager = ConnectionManager()
    ws = MockWebSocket()
    
    # 测试连接
    session_id = await manager.connect(ws)
    assert session_id in manager.active_sessions
    assert len(session_id) == 36  # uuid4长度
    
    # 测试发送消息
    await manager.send_message(session_id, {'type': 'test', 'data': 'hello'})
    assert len(ws.messages) == 1
    assert ws.messages[0]['type'] == 'test'
    
    # 测试断开
    manager.disconnect(session_id)
    assert session_id not in manager.active_sessions
    
    print('✓ 连接管理器测试通过')

asyncio.run(test())
"
```

### 1.5 等待汇总层（backend/wait_aggregator.py）

**实现要求**：
- WaitAggregator类实现滑动窗口机制
- add_message()添加消息到缓冲，启动/重置定时器
- 每次新消息推送倒计时状态（remaining_seconds）
- 达到slide_seconds或max_seconds时调用回调函数
- 定时器可被取消

**验收**：
```python
python -c "
import asyncio
from backend.wait_aggregator import WaitAggregator
from backend.models import SessionState, ConversationContext, WaitingQueue

class MockConnection:
    def __init__(self):
        self.messages = []
    async def send_json(self, data):
        self.messages.append(data)

async def test():
    aggregator = WaitAggregator(slide_seconds=2, max_seconds=5)
    
    session = SessionState(
        session_id='test',
        context=ConversationContext(history=[], covered_topics=[], user_needs={}),
        waiting_queue=WaitingQueue(messages=[]),
        connection=MockConnection()
    )
    
    triggered = []
    async def on_timeout(sess, msg):
        triggered.append(msg)
    
    # 添加第一条消息
    await aggregator.add_message(session, '第一条', on_timeout)
    assert len(session.waiting_queue.messages) == 1
    
    # 等待1秒后添加第二条（应该重置定时器）
    await asyncio.sleep(1)
    await aggregator.add_message(session, '第二条', on_timeout)
    assert len(session.waiting_queue.messages) == 2
    
    # 再等待2秒，应该触发（总共3秒但slide=2）
    await asyncio.sleep(2.5)
    assert len(triggered) == 1
    assert triggered[0] == '第一条 第二条'
    assert len(session.waiting_queue.messages) == 0
    
    print('✓ 等待汇总层测试通过')

asyncio.run(test())
"
```

### 1.6 FastAPI基础框架（backend/main.py）

**实现要求**：
- FastAPI应用，serve静态文件（static/目录）
- WebSocket端点/ws，接受连接
- 处理user_message和clear_conversation两种消息类型
- 集成ConnectionManager和WaitAggregator
- 暂时mock处理逻辑：收到消息后等待汇总，返回"Echo: {消息内容}"

**验收**：
```bash
# 1. 启动服务（后台）
uvicorn backend.main:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!
sleep 3

# 2. 测试静态文件（创建临时HTML）
mkdir -p static
echo "<html><body>Test</body></html>" > static/test.html
curl -s http://localhost:8000/static/test.html | grep "Test" && echo "✓ 静态文件服务正常" || echo "✗ 静态文件服务失败"

# 3. 测试WebSocket（用Python客户端）
python -c "
import asyncio
import websockets
import json

async def test():
    uri = 'ws://localhost:8000/ws'
    async with websockets.connect(uri) as ws:
        # 发送消息
        await ws.send(json.dumps({'type': 'user_message', 'content': '测试消息'}))
        
        # 应该收到waiting状态
        response = await asyncio.wait_for(ws.recv(), timeout=5)
        data = json.loads(response)
        assert data['type'] == 'waiting', f'预期waiting，收到{data}'
        
        # 等待最终回复
        response = await asyncio.wait_for(ws.recv(), timeout=20)
        data = json.loads(response)
        assert data['type'] == 'response', f'预期response，收到{data}'
        assert 'Echo' in data['data']['answer'], '回复内容错误'
        
        print('✓ WebSocket通信测试通过')

asyncio.run(test())
" && echo "✓ 端到端测试通过"

# 4. 停止服务
kill $SERVER_PID
```

**Day 1-2 完成检查清单**：
- [ ] 配置加载正常，缺失字段报错
- [ ] 数据模型可正常创建实例
- [ ] 连接管理器测试通过
- [ ] 等待汇总层测试通过
- [ ] FastAPI服务启动无报错
- [ ] WebSocket端到端测试通过

---

## 任务 2：知识库模块（Day 3，1天）

### 2.1 知识库解析（backend/knowledge.py）

**实现要求**：
- KnowledgeBase类加载sdwan.md
- 正则解析：`^\d+\.\s+(.+?)$` 匹配问题标题
- 每个问答对提取：number, title, content, category
- 自动分类：price/usage/troubleshooting/product/purchase/technical/general
- 分类规则：按关键词匹配（价格/多少钱→price，怎么/如何→usage等）
- get_by_intent()根据意图返回相关问答
- get_all()返回全部知识库

**验收**：
```python
python -c "
from backend.knowledge import KnowledgeBase

# 1. 加载知识库
kb = KnowledgeBase('sdwan.md')
assert len(kb.qa_pairs) > 0, '知识库为空'
print(f'✓ 加载了 {len(kb.qa_pairs)} 个问答对')

# 2. 检查解析结构
first_qa = kb.qa_pairs[0]
assert hasattr(first_qa, 'number'), '缺少number字段'
assert hasattr(first_qa, 'title'), '缺少title字段'
assert hasattr(first_qa, 'content'), '缺少content字段'
assert hasattr(first_qa, 'category'), '缺少category字段'
print(f'✓ 第一个QA: 问题{first_qa.number}, 类别{first_qa.category}')

# 3. 测试分类索引
assert 'price' in kb.category_index, '缺少price分类'
price_count = len(kb.category_index.get('price', []))
print(f'✓ price分类包含 {price_count} 个问答')

# 4. 测试按意图获取
content = kb.get_by_intent('price_inquiry')
assert '120' in content or '180' in content, '价格内容缺失'
print('✓ 按意图检索正常')

# 5. 测试get_all
all_content = kb.get_all()
assert len(all_content) > 1000, '全量内容过短'
print(f'✓ 全量知识库长度: {len(all_content)} 字符')

print('✓ 知识库模块测试通过')
"
```

### 2.2 知识库错误处理测试

**验收**：
```bash
# 1. 测试文件不存在
python -c "
from backend.knowledge import KnowledgeBase
try:
    kb = KnowledgeBase('nonexist.md')
    print('✗ 应该报错但没有')
    exit(1)
except FileNotFoundError:
    print('✓ 文件不存在正确报错')
"

# 2. 测试空文件
echo "" > empty.md
python -c "
from backend.knowledge import KnowledgeBase
try:
    kb = KnowledgeBase('empty.md')
    print('✗ 应该报错但没有')
    exit(1)
except ValueError:
    print('✓ 空文件正确报错')
"
rm empty.md

# 3. 测试格式错误回退（创建错误格式文件）
echo "这是一段没有编号的文本" > broken.md
python -c "
from backend.knowledge import KnowledgeBase
kb = KnowledgeBase('broken.md')
# 应该回退到纯文本模式，至少有1个QA
assert len(kb.qa_pairs) >= 1, '回退模式失败'
print('✓ 格式错误回退正常')
"
rm broken.md
```

### 2.3 将知识库移到data目录

```bash
# 移动知识库文件
mkdir -p data
mv sdwan.md data/ 2>/dev/null || cp sdwan.md data/
test -f data/sdwan.md && echo "✓ 知识库已移至data目录" || echo "✗ 移动失败"
```

**Day 3 完成检查清单**：
- [ ] 知识库加载测试通过（>50个问答对）
- [ ] 分类索引正常（至少6个类别）
- [ ] 按意图检索返回相关内容
- [ ] 文件不存在/空文件/格式错误的错误处理正常
- [ ] data/sdwan.md文件存在

---

## 任务 3：Web界面（Day 4，1天）

### 3.1 HTML页面（static/index.html）

**实现要求**：
- 页面标题："SDWAN智能客服"
- 顶部标题："SDWAN智能客服机器人 Demo" + "清空对话"按钮
- 消息列表区域（滚动）
- 输入区域：输入框 + 发送按钮
- 支持回车键发送
- 空消息不发送（前端拦截）

**样式要求**：
- 浅色主题
- 用户消息：蓝色背景，右对齐
- 机器人消息：灰色背景，左对齐
- 等待倒计时：橙色提示条
- 答案来源：小字灰色

### 3.2 JavaScript逻辑（static/app.js）

**实现要求**：
- WebSocket连接到ws://localhost:8000/ws
- 发送user_message和clear_conversation消息
- 处理waiting, thinking, response, error四种服务端消息
- 等待倒计时显示："正在等待您补充问题，剩余X秒..."
- 思考状态显示："💭 正在思考中..."
- 回复显示：消息内容 + 答案来源（如果有）
- 自动滚动到最新消息
- 断开连接显示提示，提供重连按钮

### 3.3 验收测试

```bash
# 1. 启动服务
uvicorn backend.main:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!
sleep 3

# 2. 检查HTML文件
test -f static/index.html && echo "✓ HTML文件存在" || (echo "✗ HTML文件不存在"; exit 1)
test -f static/app.js && echo "✓ JS文件存在" || (echo "✗ JS文件不存在"; exit 1)

# 3. 测试首页访问
curl -s http://localhost:8000/ | grep "SDWAN智能客服" && echo "✓ 首页可访问" || echo "✗ 首页访问失败"

# 4. 端到端UI测试（用Python Selenium或手动）
echo "
【手动测试检查清单】
1. 打开浏览器访问 http://localhost:8000
2. 输入'测试'并发送，观察是否显示倒计时
3. 15秒后是否收到Echo回复
4. 点击'清空对话'，消息列表是否清空
5. 发送空消息，是否被拦截（不发送）
6. 快速连续发送2条消息，是否只收到1次回复
"

read -p "请完成手动测试后按回车继续..."

kill $SERVER_PID
```

**Day 4 完成检查清单**：
- [ ] HTML页面可访问，标题正确
- [ ] WebSocket连接成功
- [ ] 发送消息显示倒计时
- [ ] 等待后收到回复
- [ ] 清空对话功能正常
- [ ] 空消息被拦截
- [ ] 连续消息只回复一次

---

## 任务 4：集成测试和文档（4小时）

### 4.1 创建.env配置文件

```bash
cp .env.example .env
echo "
请编辑.env文件，填入以下配置（最低要求）：
MODEL_API_BASE=http://localhost:11434/v1
MODEL_NAME=qwen2.5:27b
KNOWLEDGE_BASE_PATH=./data/sdwan.md
DECISION_ENGINE=rule

其他配置可保持默认值。
"
read -p "配置完成后按回车继续..."

# 验证配置
python -c "
from backend.config import settings
assert settings.KNOWLEDGE_BASE_PATH == './data/sdwan.md'
print('✓ .env配置有效')
"
```

### 4.2 端到端集成测试

```bash
# 1. 启动服务
uvicorn backend.main:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!
sleep 3

# 2. 完整流程测试
python -c "
import asyncio
import websockets
import json

async def full_test():
    uri = 'ws://localhost:8000/ws'
    async with websockets.connect(uri) as ws:
        print('1. 连接成功')
        
        # 发送第一条消息
        await ws.send(json.dumps({'type': 'user_message', 'content': '价格'}))
        print('2. 已发送消息')
        
        # 接收waiting状态
        msg = await asyncio.wait_for(ws.recv(), timeout=5)
        data = json.loads(msg)
        assert data['type'] == 'waiting'
        print(f'3. 收到倒计时: {data.get(\"remaining_seconds\")}秒')
        
        # 3秒后发送第二条（测试滑动窗口）
        await asyncio.sleep(3)
        await ws.send(json.dumps({'type': 'user_message', 'content': '直播的'}))
        print('4. 已发送第二条消息')
        
        # 等待最终回复
        for _ in range(20):
            msg = await asyncio.wait_for(ws.recv(), timeout=1)
            data = json.loads(msg)
            if data['type'] == 'response':
                print(f'5. 收到回复: {data[\"data\"][\"answer\"][:50]}...')
                assert 'Echo' in data['data']['answer']
                break
        else:
            raise TimeoutError('未收到最终回复')
        
        # 测试清空对话
        await ws.send(json.dumps({'type': 'clear_conversation'}))
        print('6. 已发送清空指令')
        
        print('✓ 端到端集成测试通过')

asyncio.run(full_test())
"

kill $SERVER_PID
```

### 4.3 更新PROGRESS.md

```bash
cat >> PROGRESS.md << 'EOF'

## Phase 1 完成情况

### 已实现功能
- ✅ FastAPI + WebSocket基础框架
- ✅ 配置管理（.env加载）
- ✅ 数据模型（Message, SessionState等）
- ✅ WebSocket连接管理器
- ✅ 等待汇总层（滑动窗口机制）
- ✅ 知识库加载和解析（59个问答对）
- ✅ 自动分类（7个类别）
- ✅ Web界面（HTML + WebSocket客户端）

### 技术指标
- 知识库问答对数量: {实际数字}
- 代码文件数: {实际数字}
- 单元测试通过数: {实际数字}
- 端到端测试: 通过

### 待Phase 2实现
- 规则引擎决策层
- Jev API集成
- Kev模型部署
- LLM客户端（Qwen27B）
- 真实回复生成（目前是Echo mock）

### 遗留问题
- {如果有，列出}

EOF

echo "✓ PROGRESS.md已更新"
```

### 4.4 提交到Git

```bash
# 1. 查看修改
git status

# 2. 添加所有新文件
git add backend/ static/ data/ tests/ PROGRESS.md .env

# 3. 提交
git commit -m "完成Phase 1：基础架构搭建

- 后端：FastAPI + WebSocket + 连接管理 + 等待汇总
- 知识库：加载解析sdwan.md（59问答），自动分类7类别
- Web界面：HTML页面 + WebSocket客户端 + 基础交互
- 测试：配置加载、数据模型、连接管理、等待汇总、端到端测试
- 功能：用户发消息→等待15秒→汇总→返回Echo回复（mock）

下一步：Phase 2决策层实现（规则引擎/Jev/Kev）"

# 4. 推送
git push origin master

echo "✓ 已提交并推送到GitHub"
```

---

## 完成条件（硬指标）

### 功能性
1. **端到端流程通过**：用户发送消息→15秒等待→收到回复（虽然是Echo mock）
2. **知识库加载成功**：加载≥50个问答对，分类≥6个类别

### 代码质量
1. **所有验收命令通过**：配置、数据模型、连接管理、等待汇总、知识库的测试全绿
2. **无硬编码**：配置项都来自.env，无magic number

### 交付物
1. **代码提交**：backend/、static/、data/目录及相关代码已提交到GitHub
2. **PROGRESS.md**：记录完成情况和遗留问题

---

## 我替领导拍的板

**决策1：Python版本下限3.10** — 理由：需要pydantic 2.x的新特性，TDD要求3.10+  
**决策2：暂不实现样式美化** — 基础功能型UI，浅色主题，无需CSS框架  
**决策3：Echo mock回复** — Phase 1验证架构，真实LLM生成在Phase 2  
**决策4：单元测试嵌入验收命令** — 快速验证，无需pytest框架（Phase 2再引入）  
**决策5：手动UI测试** — 无Selenium依赖，手动检查清单足够验证基础交互  

---

## 止损规则

1. **单个验收命令连败3次** → 跳过该模块，在PROGRESS.md标记BLOCKED，继续其他任务
2. **Day 3中午前任务1未完成** → 只完成最小可用版本（去掉单元测试，只保证能启动）
3. **集成测试失败** → 回退到最后一次验收通过的commit，在PROGRESS.md记录失败原因

---

## 验收官：我会亲自跑的暗卷

1. 发送3条连续消息（间隔2秒），观察是否只触发1次回复
2. 启动时故意删除sdwan.md，观察是否报错退出而非静默失败
3. .env缺少KNOWLEDGE_BASE_PATH字段，观察是否报错而非使用默认值

（执行者看不见这段，我会在验收时跑）
