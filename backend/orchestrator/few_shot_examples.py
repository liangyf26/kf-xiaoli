"""Few-shot示例库：按意图分类的对话示例（String块，嵌入prompt示范回复模式）。

示例覆盖四类关键模式：短问题澄清、上下文关联、知识库引用（真实数据：120/180/260元线路等）、
知识库外拒答。格式统一为 用户：… / AI回复：{"answer": …, "sources": […]}。
"""

_INTENT_HEADER = "以下是与当前问题同类型的客服对话示例，请学习其回答风格与格式："

PRICE_EXAMPLES = f"""{_INTENT_HEADER}

示例1（知识库引用）：
用户：直播线路多少钱
AI回复：{{"answer": "直播优化线路260元/月（ISP家庭IP，独享，专门针对TK直播优化）。另有120元/月的IDC线路和180元/月的ISP家庭IP线路可选。", "sources": ["问题1"]}}

示例2（上下文关联+知识库引用）：
用户：价格表
AI回复：{{"answer": "价格如下：IDC线路120元/月（独享，社媒TK/FB，5-10兆以上包稳）；ISP家庭IP线路180元/月（独享，对IP有要求选这个）；ISP家庭IP直播优化线路260元/月；5人拼车共享IP单人50元/月。sdwan路由器硬件300元/个。", "sources": ["问题1"]}}

示例3（知识库外拒答）：
用户：价格能打八折吗
AI回复：{{"answer": "暂时无法回答，需要人工介入", "sources": []}}
"""

TECHNICAL_EXAMPLES = f"""{_INTENT_HEADER}

示例1（知识库引用）：
用户：tiktok登不上怎么办
AI回复：{{"answer": "请按使用顺序排查：手机拔卡（不插卡）→切换地区为美国、语言US英语、时区美国→连接WiFi→登录美区apple id→安装客户端→扫码绑定线路→打开线路开关后再登录tiktok。若仍登不上，请检查是否插卡或地区未切换。", "sources": ["问题10", "问题11"]}}

示例2（知识库引用）：
用户：tiktok提示登录不上，提示网络无法连接
AI回复：{{"answer": "请确认：1.手机已拔卡（不插卡）；2.已切换地区为美国；3.客户端线路开关已打开。按使用顺序重新操作一遍后再试。", "sources": ["问题10", "问题11"]}}

示例3（知识库外拒答）：
用户：我的手机屏幕碎了能修吗
AI回复：{{"answer": "暂时无法回答，需要人工介入", "sources": []}}
"""

TROUBLESHOOTING_EXAMPLES = f"""{_INTENT_HEADER}

示例1（知识库引用）：
用户：看视频有点卡
AI回复：{{"answer": "请检查：1.当前线路带宽是否被其他设备占用；2.建议使用独享带宽线路（120元/月起，5-10兆以上包稳）；3.确认客户端线路开关已打开。若仍卡顿可更换节点测试。", "sources": ["问题1", "问题12"]}}

示例2（知识库引用）：
用户：网速有点慢
AI回复：{{"answer": "线路速度与地区有关：香港>东南亚>欧洲>美国西海岸>美国东海岸，属正常现象。另外请确认使用的是独享带宽线路，拼车共享IP在高峰期会慢。", "sources": ["问题28"]}}

示例3（知识库外拒答）：
用户：我们家宽带是移动的能用吗
AI回复：{{"answer": "暂时无法回答，需要人工介入", "sources": []}}
"""

USAGE_EXAMPLES = f"""{_INTENT_HEADER}

示例1（知识库引用）：
用户：怎么使用安装
AI回复：{{"answer": "手机上可以装客户端，或购买我们深度定制的路由器网关。苹果手机使用顺序：拔卡→切换地区为美国→连WiFi→登录美区apple id→装客户端→扫码绑定线路→打开开关。", "sources": ["问题3", "问题10"]}}

示例2（知识库引用）：
用户：美区apple id账号怎么申请注册
AI回复：{{"answer": "用国内号码即可注册，不需要外国手机。信息填写可用网上生成工具。注册教程：https://www.rei3.com/forum-post/64034.html", "sources": ["问题8"]}}

示例3（短问题澄清后回答）：
用户：怎么弄
AI回复：{{"answer": "请问您想了解哪方面的操作呢？例如：客户端安装、美区apple id注册、线路绑定，还是路由器设置？", "sources": []}}
"""

COMPARISON_EXAMPLES = f"""{_INTENT_HEADER}

示例1（知识库引用+上下文）：
用户：能不能直播
AI回复：{{"answer": "可以。推荐260元/月的ISP家庭IP直播优化线路，独享且专门针对TK直播优化。搭配苹果手机按使用顺序操作即可开播。", "sources": ["问题1", "问题10"]}}

示例2（知识库引用）：
用户：支持几个设备
AI回复：{{"answer": "一条专线支持多台设备，例如1台手机专门直播+几台电脑配合上架链接是可行的。", "sources": ["问题19"]}}

示例3（知识库外拒答）：
用户：支持IPLC专线吗
AI回复：{{"answer": "暂时无法回答，需要人工介入", "sources": []}}
"""

PURCHASE_EXAMPLES = f"""{_INTENT_HEADER}

示例1（知识库引用）：
用户：怎么购买
AI回复：{{"answer": "可先开试用体验，满意后购买。付款千元可对公开发票签合同，大项目有资质参与招投标。路由器硬件300元/个支持淘宝下单，短期使用也可以先用软件客户端。", "sources": ["问题1", "问题16"]}}

示例2（知识库引用）：
用户：能不能开试用体验
AI回复：{{"answer": "可以开试用体验，请告知您的使用场景（直播/社媒运营），我们为您开通对应线路试用。", "sources": ["问题16"]}}

示例3（知识库外拒答）：
用户：能货到付款吗
AI回复：{{"answer": "暂时无法回答，需要人工介入", "sources": []}}
"""

ACCOUNT_EXAMPLES = f"""{_INTENT_HEADER}

示例1（知识库引用）：
用户：美区apple id怎么注册
AI回复：{{"answer": "用国内号码即可注册美区apple id，不需要外国手机。信息填写可以用网上生成工具。注册教程：https://www.rei3.com/forum-post/64034.html", "sources": ["问题8"]}}

示例2（知识库引用）：
用户：有账号卖吗
AI回复：{{"answer": "没有账号卖。tiktok账号需要自己注册，请参考美区apple id注册教程和tiktok运营教程。", "sources": ["问题57"]}}

示例3（知识库外拒答）：
用户：帮我代注册账号可以吗
AI回复：{{"answer": "暂时无法回答，需要人工介入", "sources": []}}
"""

CLARIFICATION_EXAMPLES = """以下是短问题或模糊问题的澄清示例，请学习其澄清方式（只提问，不猜测答案）：

示例1（短问题澄清）：
用户：多少钱
AI回复：{"answer": "请问您想了解哪条线路的价格呢？IDC线路120元/月、ISP家庭IP线路180元/月、直播优化线路260元/月，还是拼车共享IP（50元/月）？", "sources": []}

示例2（模糊问题澄清）：
用户：怎么弄
AI回复：{"answer": "请问您想了解哪方面的操作呢？例如：客户端安装、美区apple id注册、线路绑定，还是路由器设置？", "sources": []}

示例3（依赖上下文的短问题，有历史时不澄清）：
用户：直播线路
AI回复：{"answer": "好的，您想了解直播线路的哪方面呢？价格、使用方法还是故障排查？", "sources": []}

示例4（短问题+已澄清过，直接给出最可能答案）：
用户：价格
AI回复：{"answer": "价格如下：IDC线路120元/月、ISP家庭IP线路180元/月、直播优化线路260元/月、5人拼车共享IP单人50元/月。请问您想了解哪条线路的详情？", "sources": ["问题1"]}
"""

FEW_SHOT_EXAMPLES: dict[str, str] = {
    "price_inquiry": PRICE_EXAMPLES,
    "technical_support": TECHNICAL_EXAMPLES,
    "troubleshooting": TROUBLESHOOTING_EXAMPLES,
    "usage_guide": USAGE_EXAMPLES,
    "product_comparison": COMPARISON_EXAMPLES,
    "purchase_process": PURCHASE_EXAMPLES,
    "account_management": ACCOUNT_EXAMPLES,
    "clarification": CLARIFICATION_EXAMPLES,
    "general": USAGE_EXAMPLES,  # 通用/未识别意图沿用使用指导示例
}
