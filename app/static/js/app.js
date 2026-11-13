/* ============================================================
 * 公共前端脚本：API 封装 / Toast / 模态框 / 小工具
 * - 同源请求自动携带 access_token Cookie
 * - 401 自动跳转登录页
 * - 所有 API 失败都优雅降级（只提示，不白屏）
 * ============================================================ */
(function () {
    "use strict";

    var App = window.App || {};
    window.App = App;

    var base = "/api/v1";
    var fontControl = document.getElementById("font-size");
    if (fontControl) {
        var savedFont = "standard";
        try { savedFont = localStorage.getItem("eldercare-font-size") || "standard"; } catch (e) { /* 存储不可用时使用默认 */ }
        if (["standard", "large", "extra"].indexOf(savedFont) < 0) { savedFont = "standard"; }
        fontControl.value = savedFont;
        document.documentElement.dataset.fontSize = savedFont;
        fontControl.addEventListener("change", function () {
            document.documentElement.dataset.fontSize = fontControl.value;
            try { localStorage.setItem("eldercare-font-size", fontControl.value); } catch (e) { /* 本次仍然生效 */ }
        });
    }

    function goLogin() {
        var next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.href = "/login?next=" + next;
    }

    function toast(message, type) {
        var box = document.getElementById("toast-box");
        if (!box) {
            box = document.createElement("div");
            box.id = "toast-box";
            box.className = "toast-box";
            document.body.appendChild(box);
        }
        var el = document.createElement("div");
        el.className = "toast " + (type || "");
        el.setAttribute("role", "status");
        el.textContent = message;
        box.appendChild(el);
        window.setTimeout(function () {
            el.style.opacity = "0";
            window.setTimeout(function () { el.remove(); }, 260);
        }, 3600);
    }
    App.toast = toast;

    function request(method, url, body, opts) {
        opts = opts || {};
        var full = url.indexOf("/api/") === 0 ? url : base + url;
        var init = {
            method: method,
            credentials: "same-origin",
            headers: { "Accept": "application/json" }
        };
        if (body !== undefined && body !== null) {
            if (body instanceof FormData) {
                init.body = body;
            } else {
                init.headers["Content-Type"] = "application/json";
                init.body = JSON.stringify(body);
            }
        }
        var controller, timeout, timedOut = false;
        if (opts.timeoutMs > 0) {
            controller = new AbortController();
            init.signal = controller.signal;
            timeout = window.setTimeout(function () { timedOut = true; controller.abort(); }, opts.timeoutMs);
        }
        return fetch(full, init).then(function (resp) {
            if (resp.status === 401) {
                toast("登录已过期，请重新登录", "warn");
                window.setTimeout(goLogin, 600);
                var unauthorized = new Error("UNAUTHORIZED");
                unauthorized.status = 401;
                throw unauthorized;
            }
            return resp.text().then(function (text) {
                var data = {};
                try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { message: text }; }
                if (!resp.ok) {
                    var msg = data.message || data.detail || ("请求失败（HTTP " + resp.status + "）");
                    if (typeof msg !== "string") { msg = JSON.stringify(msg); }
                    var err = new Error(msg);
                    err.status = resp.status;
                    err.payload = data;
                    throw err;
                }
                return data;
            });
        }).catch(function (err) {
            if (timedOut) {
                var timeoutError = new Error("请求超时");
                timeoutError.code = "REQUEST_TIMEOUT";
                throw timeoutError;
            }
            throw err;
        }).finally(function () {
            if (timeout !== undefined) { window.clearTimeout(timeout); }
        });
    }

    App.api = {
        get: function (url, opts) { return request("GET", url, undefined, opts); },
        post: function (url, body, opts) { return request("POST", url, body === undefined ? {} : body, opts); },
        put: function (url, body) { return request("PUT", url, body === undefined ? {} : body); },
        del: function (url) { return request("DELETE", url); },
        upload: function (url, formData) { return request("POST", url, formData); }
    };

    /** 调用接口并统一处理成功/失败，返回 Promise；失败时只 toast 不抛出 */
    App.call = function (promise, okMsg) {
        return promise.then(function (res) {
            if (okMsg) { toast(okMsg, "success"); }
            return res;
        }).catch(function (err) {
            if (err && err.message !== "UNAUTHORIZED") {
                toast(err.message || "操作失败", "error");
            }
            return null;
        });
    };

    App.esc = function (value) {
        if (value === null || value === undefined) { return ""; }
        return String(value).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    };

    App.money = function (value) {
        var n = Number(value || 0);
        return n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    };

    App.getQuery = function (name) {
        var m = new RegExp("[?&]" + name + "=([^&]*)").exec(window.location.search);
        return m ? decodeURIComponent(m[1].replace(/\+/g, " ")) : "";
    };

    /* ---------------- 模态框 ---------------- */
    App.modal = function (options) {
        var opts = options || {};
        var previousFocus = document.activeElement;
        var mask = document.createElement("div");
        mask.className = "modal-mask show";
        mask.innerHTML =
            '<div class="modal-box" role="dialog" aria-modal="true">' +
            '  <div class="modal-head"><span id="modal-title">' + App.esc(opts.title || "提示") + "</span>" +
            '    <button type="button" class="modal-close" aria-label="关闭">×</button></div>' +
            '  <div class="modal-body">' + (opts.html || "") + "</div>" +
            '  <div class="modal-foot"></div>' +
            "</div>";
        document.body.appendChild(mask);
        var foot = mask.querySelector(".modal-foot");
        mask.querySelector('[role="dialog"]').setAttribute("aria-labelledby", "modal-title");
        function close() {
            mask.remove();
            if (previousFocus && previousFocus.isConnected) { previousFocus.focus(); }
        }
        mask.addEventListener("keydown", function (ev) {
            if (ev.key === "Escape") { ev.preventDefault(); close(); return; }
            if (ev.key !== "Tab") { return; }
            var items = Array.from(mask.querySelectorAll('button, input, select, textarea, a[href], [tabindex="0"]'))
                .filter(function (el) { return !el.disabled && el.getClientRects().length > 0; });
            if (!items.length) { return; }
            var first = items[0], last = items[items.length - 1];
            if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
            else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
        });
        mask.querySelector(".modal-close").addEventListener("click", close);
        mask.addEventListener("click", function (ev) { if (ev.target === mask) { close(); } });
        (opts.buttons || [{ text: "关闭", cls: "btn-secondary" }]).forEach(function (b) {
            var btn = document.createElement("button");
            btn.type = "button";
            btn.className = "btn " + (b.cls || "btn-primary");
            btn.textContent = b.text;
            btn.addEventListener("click", function () {
                if (typeof b.onClick === "function") {
                    var keep = b.onClick(mask, close);
                    if (keep === true) { return; }
                }
                if (b.keepOpen !== true) { close(); }
            });
            foot.appendChild(btn);
        });
        if (opts.onReady) { opts.onReady(mask, close); }
        var initialFocus = mask.querySelector("input, select, textarea, button");
        if (initialFocus) { initialFocus.focus(); }
        return { el: mask, close: close };
    };

    /** 通用「调用接口 + 刷新」按钮动作 */
    App.action = function (button, url, body, okMsg, opts) {
        opts = opts || {};
        if (button && button.disabled) { return Promise.resolve(null); }
        var originalText = button ? button.textContent : "";
        var succeeded = false;
        if (button) {
            button.disabled = true;
            button.setAttribute("aria-busy", "true");
            button.textContent = "处理中…";
        }
        return App.api.post(url, body).then(function (res) {
            succeeded = true;
            toast(okMsg || "操作成功", "success");
            if (opts.reload !== false) {
                window.setTimeout(function () { window.location.reload(); }, 400);
            }
            return res;
        }).catch(function (err) {
            if (err && err.message !== "UNAUTHORIZED") { toast(err.message || "操作失败", "error"); }
        }).finally(function () {
            if (button) {
                button.removeAttribute("aria-busy");
                button.textContent = originalText;
                if (!succeeded || opts.reload === false) { button.disabled = false; }
            }
        });
    };

    App.confirmAction = function (message, onOk) {
        App.modal({
            title: "请确认",
            html: '<p style="font-size:18px">' + App.esc(message) + "</p>",
            buttons: [
                { text: "取消", cls: "btn-secondary" },
                { text: "确认执行", cls: "btn-danger", onClick: function () { onOk(); } }
            ]
        });
    };

    /* ---------------- 简易表单提交 ---------------- */
    App.formToJson = function (form) {
        var data = {};
        new FormData(form).forEach(function (value, key) { data[key] = value; });
        return data;
    };

    App.submitJson = function (form, url, opts) {
        opts = opts || {};
        var data = App.formToJson(form);
        Object.keys(data).forEach(function (k) {
            if (data[k] === "") { delete data[k]; }
            else if (typeof data[k] === "string" && /^-?\d+(\.\d+)?$/.test(data[k]) && opts.numberFields && opts.numberFields.indexOf(k) >= 0) {
                data[k] = Number(data[k]);
            }
        });
        var btn = form.querySelector('button[type="submit"]');
        if (btn) { btn.disabled = true; }
        return App.api.post(url, data).then(function (res) {
            toast(opts.okMsg || "提交成功", "success");
            if (opts.after) { opts.after(res); }
            return res;
        }).catch(function (err) {
            if (err && err.message !== "UNAUTHORIZED") { toast(err.message || "提交失败", "error"); }
            return null;
        }).finally(function () {
            if (btn) { btn.disabled = false; }
        });
    };

    // 全局兜底：未捕获的 promise 异常不打扰用户（控制台留痕）
    window.addEventListener("unhandledrejection", function (ev) {
        if (ev.reason && ev.reason.message === "UNAUTHORIZED") { ev.preventDefault(); }
    });
})();
