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

    var ws = null;
    var countdownTimer = null;
    var reconnectDelay = 1000;
    var pendingMessages = []; // WebSocket未就绪时先排队，连接建立后补发

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
            thinkingBar.classList.add("hidden");
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
                stopCountdown();
                thinkingBar.classList.remove("hidden");
                break;
            case "response":
                stopCountdown();
                thinkingBar.classList.add("hidden");
                waitingBar.classList.add("hidden");
                appendMessage("assistant", data.data ? data.data.answer : "", data.data ? data.data.sources : []);
                break;
            case "error":
                stopCountdown();
                thinkingBar.classList.add("hidden");
                waitingBar.classList.add("hidden");
                var msg = data.data && data.data.message ? data.data.message : "服务暂时不可用";
                appendMessage("assistant", msg, []);
                break;
            default:
                break;
        }
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

    function appendMessage(role, content, sources) {
        var div = document.createElement("div");
        div.className = "message " + role;
        div.textContent = content;
        if (role === "assistant" && sources && sources.length > 0) {
            var src = document.createElement("span");
            src.className = "sources";
            src.textContent = "来源：" + sources.join("、");
            div.appendChild(src);
        }
        messageList.appendChild(div);
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
        thinkingBar.classList.add("hidden");
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

    connect();
})();
