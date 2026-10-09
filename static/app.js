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
        kev_failed: "Kev引擎（服务未就绪，已降级）",
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
            case "response":
                stopCountdown();
                hideThinkingStatus();
                waitingBar.classList.add("hidden");
                appendMessage(
                    "assistant",
                    data.data ? data.data.answer : "",
                    data.data ? data.data.sources : [],
                    data.data ? data.data.engine : ""
                );
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

    function appendMessage(role, content, sources, engine) {
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
        if (role === "assistant" && engine) {
            // 决策引擎标记（便于验证引擎切换已生效）
            var meta = document.createElement("span");
            meta.className = "msg-meta";
            meta.textContent = "⚙ " + (ENGINE_LABELS[engine] || engine);
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
