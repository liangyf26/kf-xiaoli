/* SDWAN智能客服 - 前端逻辑 */
(function () {
    "use strict";

    var messageList = document.getElementById("message-list");
    var input = document.getElementById("message-input");
    var sendBtn = document.getElementById("send-btn");
    var clearBtn = document.getElementById("clear-btn");
    var waitingBar = document.getElementById("waiting-bar");
    var waitingText = document.getElementById("waiting-text");
    var thinkingBar = document.getElementById("thinking-bar");
    var disconnectTip = document.getElementById("disconnect-tip");
    var reconnectBtn = document.getElementById("reconnect-btn");
    var engineBtns = Array.prototype.slice.call(document.querySelectorAll(".engine-btn"));

    var ENGINE_LABELS = {
        rule: "规则引擎",
        jev: "Jev引擎",
        kev: "Kev引擎",
        qwen: "Qwen引擎",
        kev_failed: "Kev引擎（服务未就绪，已降级）",
        qwen_failed: "Qwen引擎（决策失败，已降级）",
    };

    var INTENT_LABELS = {
        price_inquiry: "价格咨询",
        product_comparison: "产品对比",
        technical_support: "技术支持",
        usage_guide: "使用指导",
        troubleshooting: "故障排查",
        purchase_process: "购买流程",
        unclear: "未识别",
    };

    var EMOTION_LABELS = {
        neutral: "中性",
        positive: "积极",
        urgent: "紧急",
        dissatisfied: "不满",
        complaint_risk: "投诉风险",
    };

    var ws = null;
    var countdownTimer = null;
    var reconnectDelay = 1000;
    var pendingMessages = []; // WebSocket未就绪时先排队，连接建立后补发

    /* 决策引擎选择器 */
    function setEngineActive(name) {
        engineBtns.forEach(function (btn) {
            btn.classList.toggle("active", btn.getAttribute("data-engine") === name);
        });
    }

    function initEngineSelector() {
        engineBtns.forEach(function (btn) {
            btn.addEventListener("click", function () {
                var engine = btn.getAttribute("data-engine");
                if (!ws || ws.readyState !== WebSocket.OPEN) {
                    disconnectTip.classList.remove("hidden");
                    return;
                }
                ws.send(JSON.stringify({ type: "switch_engine", engine: engine }));
            });
        });
        // 初始化高亮服务端当前引擎
        fetch("/engine")
            .then(function (r) { return r.json(); })
            .then(function (d) { if (d && d.engine) { setEngineActive(d.engine); } })
            .catch(function () { /* 查询失败时保持默认无高亮 */ });
    }

    function connect() {
        // 服务与本页面同源，按当前地址推导WebSocket地址
        var protocol = location.protocol === "https:" ? "wss://" : "ws://";
        ws = new WebSocket(protocol + location.host + "/ws");

        ws.onopen = function () {
            reconnectDelay = 1000;
            disconnectTip.classList.add("hidden");
            // 补发连接建立前排队的消息
            while (pendingMessages.length > 0) {
                ws.send(JSON.stringify({ type: "user_message", content: pendingMessages.shift() }));
            }
        };

        ws.onmessage = function (event) {
            var data;
            try {
                data = JSON.parse(event.data);
            } catch (e) {
                return;
            }
            handleMessage(data);
        };

    ws.onclose = function () {
        stopCountdown();
        hideThinkingStatus();
        waitingBar.classList.add("hidden");
        disconnectTip.classList.remove("hidden");
    };

        ws.onerror = function () {
            ws.close();
        };
    }

    function handleMessage(data) {
        switch (data.type) {
            case "waiting":
                showCountdown(data.remaining_seconds || 0);
                break;
            case "thinking":
                showThinkingStatus();
                break;
            case "greeting":
                // 开场语（会话建立/清空时由服务端推送，内容来自FIRST_MESSAGE_GREETING配置）
                appendMessage("assistant", data.data ? data.data.text : "", []);
                break;
            case "response":
                stopCountdown();
                hideThinkingStatus();
                waitingBar.classList.add("hidden");
                appendMessage("assistant", data.data ? data.data.answer : "", data.data ? data.data.sources : [], data.data);
                break;
            case "engine_switched":
                setEngineActive(data.data ? data.data.engine : "");
                appendSystemTip("已切换决策引擎：" + (ENGINE_LABELS[data.data.engine] || data.data.engine));
                break;
            case "error":
                stopCountdown();
                hideThinkingStatus();
                waitingBar.classList.add("hidden");
                var msg = data.data && data.data.message ? data.data.message : "服务暂时不可用";
                appendMessage("assistant", msg, []);
                break;
            default:
                break;
        }
    }

    /* 思考状态（含点点点动画） */
    function showThinkingStatus() {
        stopCountdown();
        thinkingBar.innerHTML = "💭 机器人正在思考中<span class=\"dots\"></span>";
        thinkingBar.classList.remove("hidden");
    }

    function hideThinkingStatus() {
        thinkingBar.classList.add("hidden");
    }

    /* 倒计时：服务端推送剩余秒数，前端本地每秒递减 */
    function showCountdown(seconds) {
        stopCountdown();
        var remaining = Math.max(1, Math.round(seconds));
        waitingBar.classList.remove("hidden");
        waitingText.textContent = "正在等待您补充问题，剩余" + remaining + "秒...";
        countdownTimer = setInterval(function () {
            remaining -= 1;
            if (remaining <= 0) {
                waitingText.textContent = "正在汇总您的问题...";
                stopCountdown();
            } else {
                waitingText.textContent = "正在等待您补充问题，剩余" + remaining + "秒...";
            }
        }, 1000);
    }

    function stopCountdown() {
        if (countdownTimer) {
            clearInterval(countdownTimer);
            countdownTimer = null;
        }
    }

    function appendMessage(role, content, sources, data) {
        var div = document.createElement("div");
        div.className = "message " + role;
        div.textContent = content;
        if (role === "assistant" && sources && sources.length > 0) {
            // 答案来源（默认展开，点击折叠/展开）
            var src = document.createElement("span");
            src.className = "sources";
            src.textContent = "📎 来源（点击折叠）：";
            var list = document.createElement("span");
            list.className = "source-list";
            list.textContent = sources.join("、");
            src.appendChild(list);
            src.addEventListener("click", function () {
                src.classList.toggle("collapsed");
            });
            div.appendChild(src);
        }
        if (role === "assistant" && data && data.engine) {
            // 决策结果行（Phase 5任务4）：引擎 · 决策耗时 · 意图+置信度 · 情绪
            var meta = document.createElement("span");
            meta.className = "msg-meta";
            var parts = ["⚙ " + (ENGINE_LABELS[data.engine] || data.engine)];
            if (typeof data.decision_latency_ms === "number") {
                parts.push("决策" + data.decision_latency_ms + "ms");
            }
            if (data.intent) {
                parts.push((INTENT_LABELS[data.intent] || data.intent) + " " + data.intent_confidence);
            }
            meta.textContent = parts.join(" · ");
            if (data.emotion && EMOTION_LABELS[data.emotion]) {
                var emotion = document.createElement("span");
                emotion.className = "msg-emotion msg-emotion-" + data.emotion;
                emotion.textContent = " · 情绪：" + EMOTION_LABELS[data.emotion];
                meta.appendChild(emotion);
            }
            div.appendChild(meta);
        }
        messageList.appendChild(div);
        messageList.scrollTop = messageList.scrollHeight;
    }

    function appendSystemTip(text) {
        var tip = document.createElement("div");
        tip.className = "system-tip";
        tip.textContent = text;
        messageList.appendChild(tip);
        messageList.scrollTop = messageList.scrollHeight;
    }

    function sendMessage() {
        var content = input.value.trim();
        if (!content) {
            return; // 空消息拦截
        }
        if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
            disconnectTip.classList.remove("hidden");
            return;
        }
        appendMessage("user", content, []);
        if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "user_message", content: content }));
        } else {
            // CONNECTING状态：排队等待onopen补发
            pendingMessages.push(content);
        }
        input.value = "";
    }

    function clearConversation() {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "clear_conversation" }));
        }
        stopCountdown();
        hideThinkingStatus();
        waitingBar.classList.add("hidden");
        messageList.innerHTML = "";
    }

    sendBtn.addEventListener("click", sendMessage);
    clearBtn.addEventListener("click", clearConversation);
    reconnectBtn.addEventListener("click", function () {
        disconnectTip.classList.add("hidden");
        connect();
    });
    input.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
            sendMessage();
        }
    });

    initEngineSelector();
    connect();
})();
