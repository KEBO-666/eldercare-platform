/* Appointment feedback: keep confirmed outcomes distinct from uncertain requests. */
(function () {
    "use strict";
    var F = {};
    var App = window.App;
    App.appointmentFeedback = F;
    F.timeoutMs = 15000;
    var labels = { preview: "时段检查", create: "预约提交", cancel: "取消预约", "no-show": "爽约登记", accept: "预约受理", "to-dispatch": "转待派单" };
    var fields = { elder_id: "服务老人", service_item_id: "服务项目", desired_start: "上门时间", cycle_rule: "预约频率", reason: "原因", remark: "备注", address: "服务地址" };
    var rejectionStatuses = { RULE_VIOLATION: 400, NEEDS_MANUAL: 422, VALIDATION_ERROR: 422,
        FORBIDDEN: 403, NOT_FOUND: 404, INVALID_TRANSITION: 409, CONFLICT: 409, BAD_REQUEST: 400 };

    function positiveId(value) { return typeof value === "number" && Number.isInteger(value) && value > 0; }
    function validDate(value) { return typeof value === "string" && !isNaN(new Date(value).getTime()); }
    function validResponse(operation, res, body) {
        if (!res || res.code !== 0 || !res.data || typeof res.data !== "object") { return false; }
        var data = res.data;
        if (operation === "preview") {
            var count = body.cycle_rule === "weekly" ? 4 : 1;
            return data.reservation_created === false && data.occurrence_count === count && Array.isArray(data.occurrences) &&
                data.occurrences.length === count && data.occurrences.every(function (row) {
                    return row && validDate(row.desired_start) && validDate(row.desired_end) &&
                        new Date(row.desired_end) > new Date(row.desired_start);
                });
        }
        if (!positiveId(data.id)) { return false; }
        if (operation === "create") {
            if (typeof data.code !== "string" || data.status !== "pending_accept") { return false; }
            if (body.cycle_rule !== "weekly") { return true; }
            return data.occurrence_count === 4 && Array.isArray(data.occurrences) && data.occurrences.length === 4 &&
                data.occurrences.every(function (row) { return row && positiveId(row.id) && validDate(row.desired_start); }) &&
                new Set(data.occurrences.map(function (row) { return row.id; })).size === 4;
        }
        if (operation === "cancel" || operation === "no-show") {
            return data.status === (operation === "cancel" ? "cancelled" : "no_show") &&
                typeof data.cancellation_fee === "number" && isFinite(data.cancellation_fee) && data.cancellation_fee >= 0 &&
                Number.isInteger(data.released_order_count) && data.released_order_count >= 0;
        }
        return data.status === (operation === "accept" ? "accepted" : "pending_dispatch");
    }

    F.post = function (operation, url, body) {
        return App.api.post(url, body, { timeoutMs: F.timeoutMs }).then(function (res) {
            var expectedId = url.match(/\/appointments\/(\d+)\//);
            if (!validResponse(operation, res, body || {}) || (expectedId && res.data.id !== Number(expectedId[1]))) {
                var error = new Error("响应内容无法确认");
                error.code = "INVALID_RESPONSE";
                throw error;
            }
            return res;
        });
    };

    F.error = function (err, operation) {
        err = err || {};
        var payload = err.payload || {};
        var status = err.status;
        var code = payload.code;
        var readOnly = operation === "preview";
        var login = status === 401 || err.message === "UNAUTHORIZED";
        var known = Object.prototype.hasOwnProperty.call(rejectionStatuses, code) && rejectionStatuses[code] === status;
        var info = { title: "暂时无法完成" + (labels[operation] || "操作"), detail: "", next: "", tone: "danger", uncertain: !readOnly && !known, login: login, rejected: known };
        if (login) {
            info.title = "登录已过期";
            info.detail = "请重新登录后继续。页面将跳转到登录页，也可使用下方入口。";
            info.next = "登录后先核对预约列表，再继续操作。";
            info.uncertain = false;
            info.tone = "warn";
        } else if (known) {
            if (code === "VALIDATION_ERROR") {
                var invalid = Array.isArray(payload.data) ? payload.data.map(function (row) {
                    var location = row && Array.isArray(row.loc) ? row.loc : [];
                    return fields[location[location.length - 1]] || "填写内容";
                }).filter(function (field, index, all) { return all.indexOf(field) === index; }) : [];
                info.title = "填写内容不符合要求";
                info.detail = "请检查" + (invalid.length ? invalid.join("、") : "表单内容") + "的格式或长度。";
            } else {
                info.title = status === 403 ? "当前账号无权操作" : status === 404 ? "预约或服务项目不存在" :
                    status === 409 ? "预约状态已变化" : "本次操作未通过校验";
                info.detail = typeof payload.message === "string" ? payload.message : "请核对预约信息。";
            }
            info.next = status === 403 ? "请使用有权限的账号，或联系网格员协助。" : status === 404 || status === 409 ?
                "请刷新预约列表，确认最新状态后再操作。" : "请根据提示调整填写内容，再检查时段或重试。";
            if (operation === "create") { info.next = "本次提交未创建预约。" + info.next; }
        } else if (status === 429) {
            info.title = "操作过于频繁";
            info.detail = "请稍后再操作，不要连续点击。";
        } else if (err.code === "REQUEST_TIMEOUT" || status === 408 || status === 504) {
            info.title = "请求超时";
            info.detail = "等待响应时间过长，请检查网络。";
        } else if (err.code === "INVALID_RESPONSE") {
            info.title = "返回结果无法确认";
            info.detail = "没有收到完整的操作结果，暂时无法判断是否成功。";
        } else if (!status) {
            info.title = "网络连接中断";
            info.detail = "无法连接服务，请检查网络或确认应用仍在运行。";
        } else if (status >= 500) {
            info.title = "服务暂时不可用";
            info.detail = "服务器未能正常返回结果，请稍后查看。";
        } else {
            info.title = "请求结果无法确认";
            info.detail = "服务返回了无法识别的结果。";
        }
        if (!info.next) {
            info.next = readOnly ? "本次只检查时段，没有创建预约；网络恢复后可重新检查。" :
                "操作结果尚未确认，服务器可能已处理。请先查看预约列表核对状态，避免重复提交。";
        }
        return info;
    };

    F.show = function (element, info) {
        element.className = "notice " + (info.tone || "");
        element.setAttribute("role", info.tone === "danger" || info.tone === "warn" ? "alert" : "status");
        element.replaceChildren();
        var title = document.createElement("strong");
        title.textContent = info.title;
        element.appendChild(title);
        [info.detail, info.next].forEach(function (value) {
            if (!value) { return; }
            var text = document.createElement("div");
            text.textContent = value;
            element.appendChild(text);
        });
        (info.links || []).forEach(function (item) {
            var link = document.createElement("a");
            link.className = "btn btn-ghost btn-sm mt-1";
            link.href = item.href;
            link.textContent = item.text;
            element.appendChild(link);
        });
    };
    F.failure = function (element, error, operation) {
        var info = F.error(error, operation);
        info.links = [{ text: "查看预约列表", href: "/appointments" }];
        if (info.login) { info.links.unshift({ text: "重新登录", href: "/login?next=" + encodeURIComponent(window.location.pathname) }); }
        F.show(element, info);
        return info;
    };
    F.pending = function (element, message) {
        F.show(element, { title: message, detail: "正在等待服务器确认，请勿重复操作。" });
    };
    F.success = function (element, message, detail) {
        F.show(element, { title: message, detail: detail, tone: "success", links: [{ text: "查看预约列表", href: "/appointments" }] });
    };
    F.remember = function (message) {
        try { sessionStorage.setItem("eldercare-appointment-feedback", JSON.stringify({ message: message, createdAt: Date.now() })); } catch (e) { /* Visible feedback remains available. */ }
    };
    F.restore = function (element) {
        try {
            var saved = JSON.parse(sessionStorage.getItem("eldercare-appointment-feedback") || "null");
            sessionStorage.removeItem("eldercare-appointment-feedback");
            if (saved && typeof saved.message === "string" && Date.now() - saved.createdAt >= 0 && Date.now() - saved.createdAt < 60000) {
                F.success(element, saved.message);
                return true;
            }
        } catch (e) { /* Storage failure must not change the operation result. */ }
        return false;
    };
})();
