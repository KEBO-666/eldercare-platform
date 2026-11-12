/* Health reporting and alert tracking use confirmed server snapshots. */
(function () {
    "use strict";
    var H = App.healthAlert = {};
    var labels = { unacknowledged: "待响应", responded: "已响应，待处置", handling: "处置中", awaiting_followup: "转诊待随访", closed: "已闭环" };
    var actionNames = { report: "人工异常上报", push: "通知推送", sms: "模拟短信", ack: "确认响应", progress: "处置进展", escalate: "超时升级", resolve: "处置结论", followup: "随访进展", followup_close: "随访完成并闭环" };
    var rejectionStatuses = { RULE_VIOLATION: [400, 422], VALIDATION_ERROR: [422], BAD_REQUEST: [400], FORBIDDEN: [403], NOT_FOUND: [404], CONFLICT: [409], INVALID_TRANSITION: [409] };
    function id(value) { return Number.isInteger(value) && value > 0; }
    function date(value) { return typeof value === "string" && !isNaN(new Date(value).getTime()); }
    function text(value) { return typeof value === "string" && value.trim().length > 0; }
    function envelope(res) { return res && res.code === 0 && res.data && typeof res.data === "object"; }
    function invalid() { var error = new Error("返回结果无法确认"); error.code = "INVALID_RESPONSE"; return error; }
    H.validAlert = function (data, expectedId) {
        return data && id(data.id) && (!expectedId || data.id === expectedId) && id(data.elder_id) &&
            ["pending", "referred", "resolved", "false_alarm"].indexOf(data.status) >= 0 &&
            Object.prototype.hasOwnProperty.call(labels, data.lifecycle_status) && typeof data.closed === "boolean" &&
            (data.closed === (data.lifecycle_status === "closed")) &&
            (data.status !== "pending" || ["unacknowledged", "responded", "handling"].indexOf(data.lifecycle_status) >= 0) &&
            (["resolved", "false_alarm"].indexOf(data.status) < 0 || data.closed) &&
            (data.status !== "referred" || data.closed || data.lifecycle_status === "awaiting_followup") &&
            Array.isArray(data.actions) && data.actions.every(function (action) {
                return action && id(action.id) && action.alert_id === data.id && text(action.action_type) && date(action.action_at || action.created_at);
            }) && date(data.created_at) && (!data.closed || date(data.closed_at));
    };
    H.validMetric = function (res, body) {
        if (!envelope(res)) { return false; }
        var d = res.data, e = d.alert_evaluation;
        return id(d.metric_id) && d.metric && d.metric.id === d.metric_id && d.metric.elder_id === body.elder_id &&
            d.metric.metric_code === body.metric_code && d.metric.value === body.value &&
            Array.isArray(d.alerts) && d.alerts.every(function (a) { return a && id(a.id) && ["red", "yellow", "blue"].indexOf(a.level) >= 0; }) &&
            e && ["created", "no_match", "cooldown", "no_rules", "error"].indexOf(e.status) >= 0 &&
            ((e.status === "created") === (d.alerts.length > 0));
    };
    H.validWrite = function (operation, res, body, expectedId) {
        if (operation === "metric") { return H.validMetric(res, body); }
        if (!envelope(res) || !H.validAlert(res.data, expectedId)) { return false; }
        var d = res.data;
        if (operation === "report") { return d.elder_id === body.elder_id && d.summary === body.summary && d.level === body.level && !d.closed; }
        if (operation === "ack") { return d.status === "pending" && date(d.first_responded_at); }
        if (operation === "handle") { return d.status === body.resolution_type && d.measure === body.measure && d.closed === (body.resolution_type !== "referred"); }
        var expectedAction = operation === "progress" ? "progress" : body.close ? "followup_close" : "followup";
        return (operation === "progress" ? d.status === "pending" && d.lifecycle_status === "handling" : d.status === "referred" && d.closed === body.close) &&
            d.actions.some(function (a) { return a.action_type === expectedAction && a.note === body.note; });
    };
    H.post = function (operation, url, body, expectedId) {
        return App.api.post(url, body, { timeoutMs: 15000 }).then(function (res) {
            if (!H.validWrite(operation, res, body, expectedId)) { throw invalid(); }
            return res.data;
        });
    };
    H.error = function (err, readOnly) {
        err = err || {};
        var payload = err.payload || {}, allowed = rejectionStatuses[payload.code];
        var known = Array.isArray(allowed) && allowed.indexOf(err.status) >= 0;
        var login = err.status === 401 || err.message === "UNAUTHORIZED";
        var result = { title: "操作结果尚未确认", detail: "服务器可能已处理，请先核对记录，避免重复提交。", tone: "warn", uncertain: !readOnly && !known && !login };
        if (login) { result.title = "登录已过期"; result.detail = "请重新登录后核对记录，再继续操作。当前填写内容尚未重新提交。"; }
        else if (known) { result.title = err.status === 409 ? "状态已变化" : "本次操作未通过校验"; result.detail = typeof payload.message === "string" ? payload.message : "请检查填写内容及当前账号权限。"; result.tone = "danger"; }
        else if (err.code === "REQUEST_TIMEOUT") { result.title = "请求超时，结果尚未确认"; }
        else if (!err.status && err.code !== "INVALID_RESPONSE") { result.title = "连接中断，结果尚未确认"; }
        if (readOnly && !known && !login) { result.title = "暂时无法刷新追踪记录"; result.detail = "页面仍显示上次快照。连接恢复后请再次刷新，暂不提交新操作。"; }
        result.login = login;
        return result;
    };
    H.show = function (element, info, links) {
        element.hidden = false;
        element.className = "notice " + (info.tone || "");
        element.setAttribute("role", info.tone === "danger" || info.tone === "warn" ? "alert" : "status");
        element.replaceChildren();
        var title = document.createElement("strong"); title.textContent = info.title; element.appendChild(title);
        if (info.detail) { var p = document.createElement("p"); p.textContent = info.detail; element.appendChild(p); }
        (links || []).forEach(function (item) {
            var a = document.createElement("a"); a.href = item.href; a.textContent = item.text; a.className = "btn btn-ghost btn-sm"; element.appendChild(a);
        });
    };
    H.metricOutcome = function (data) {
        var messages = {
            created: "指标已保存，已生成 " + data.alerts.length + " 条预警",
            no_match: "指标已保存，本次未触发启用的预警规则",
            cooldown: "指标已保存，异常命中冷却规则，未重复生成预警",
            no_rules: "指标已保存，当前无适用的启用规则",
            error: "指标已保存，但预警评估失败"
        };
        return { title: messages[data.alert_evaluation.status],
            detail: "记录编号 #" + data.metric_id + "。" + (data.alert_evaluation.status === "error" ?
                "请联系网格员人工核实并按需上报异常。不要重复录入这条指标。" : "未生成新预警不代表健康状况正常，请结合实际情况判断。"),
            tone: ["error", "no_rules", "cooldown"].indexOf(data.alert_evaluation.status) >= 0 ? "warn" : "success" };
    };
    function localMinute() {
        var d = new Date(), pad = function (n) { return String(n).padStart(2, "0"); };
        return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
    }
    function linksFor(elderId) { return [{ text: "查看预警看板", href: "/alerts" }, { text: "核对已保存指标", href: "/monitoring/" + elderId }]; }
    function validateForm(form, feedback) {
        var invalidFields = Array.from(form.querySelectorAll("input,textarea,select")).filter(function (field) {
            var valid = field.validity.valid && (!field.required || text(field.value));
            if (valid) { field.removeAttribute("aria-invalid"); } else { field.setAttribute("aria-invalid", "true"); }
            return !valid;
        });
        if (!invalidFields.length) { return true; }
        H.show(feedback, { title: "请检查填写内容", detail: "请补全必填项，并按提示填写有效的数值、时间和长度。", tone: "danger" });
        invalidFields[0].focus();
        return false;
    }
    H.mountHealth = function () {
        var form = document.getElementById("metric-form");
        if (!form) { return; }
        var elderId = Number(form.dataset.elderId), ranges = JSON.parse(form.dataset.ranges);
        var value = document.getElementById("metric_value"), code = document.getElementById("metric_code"), measured = document.getElementById("measured_at");
        var btn = document.getElementById("btn-metric"), feedback = document.getElementById("metric-feedback"), locked = false, busy = false;
        measured.value = localMinute();
        function range() { var r = ranges[code.value]; value.min = r[0]; value.max = r[1]; document.getElementById("metric-range").textContent = "允许录入范围：" + r[0] + "–" + r[1] + "（范围校验不代表健康正常）"; }
        code.addEventListener("change", range); range();
        form.addEventListener("submit", function (event) {
            event.preventDefault();
            if (busy || locked) { return; }
            if (!validateForm(form, feedback) || !text(value.value) || !Number.isFinite(Number(value.value))) { return; }
            var body = { elder_id: elderId, metric_code: code.value, value: Number(value.value), measured_at: measured.value, source: "visit" };
            busy = true; btn.disabled = true;
            H.show(feedback, { title: "正在保存并检测预警", detail: "请等待服务器确认，避免重复提交。" });
            H.post("metric", "/health/metrics", body).then(function (data) {
                var links = data.alerts.map(function (a) { return { text: "追踪预警 #" + a.id, href: "/alerts/" + a.id }; });
                (data.alert_evaluation.suppressed || []).forEach(function (rule) {
                    if (id(rule.existing_alert_id) && !links.some(function (a) { return a.href === "/alerts/" + rule.existing_alert_id; })) {
                        links.push({ text: "查看冷却关联预警 #" + rule.existing_alert_id, href: "/alerts/" + rule.existing_alert_id });
                    }
                });
                links = links.concat(linksFor(elderId));
                H.show(feedback, H.metricOutcome(data), links);
                value.value = "";
            }).catch(function (err) {
                var info = H.error(err); locked = info.uncertain || info.login;
                H.show(feedback, info, linksFor(elderId));
            }).finally(function () { busy = false; btn.disabled = locked; });
        });
        var reportForm = document.getElementById("report-form"), reportLocked = false, reportBusy = false;
        reportForm.addEventListener("submit", function (event) {
            event.preventDefault();
            if (reportBusy || reportLocked || !validateForm(reportForm, document.getElementById("report-feedback"))) { return; }
            var body = { elder_id: elderId, summary: document.getElementById("report-summary").value.trim(), note: document.getElementById("report-note").value.trim(), level: document.getElementById("report-level").value };
            var feedback = document.getElementById("report-feedback"), button = document.getElementById("btn-report");
            if (!body.summary || !body.note) { H.show(feedback, { title: "请填写异常摘要和具体情况", tone: "danger" }); return; }
            reportBusy = true; button.disabled = true;
            H.show(feedback, { title: "正在上报异常", detail: "请等待确认，不要重复操作。" });
            H.post("report", "/alerts/report", body).then(function (data) {
                H.show(feedback, { title: "异常已上报，预警 #" + data.id + " 待响应", detail: "请到追踪页查看响应和处置进展。站内通知已留痕，短信为模拟记录。", tone: "success" }, [{ text: "追踪本次异常", href: "/alerts/" + data.id }]);
                reportForm.reset();
            }).catch(function (err) {
                var info = H.error(err); reportLocked = info.uncertain || info.login;
                H.show(feedback, info, linksFor(elderId));
            }).finally(function () { reportBusy = false; button.disabled = reportLocked; });
        });
    };
    function fmt(value) { return value ? String(value).replace("T", " ") : "—"; }
    function kv(label, value) { return '<div class="info-item"><div class="k">' + App.esc(label) + '</div><div class="v" style="overflow-wrap:anywhere">' + App.esc(value === null || value === undefined ? "—" : value) + '</div></div>'; }
    H.stateHtml = function (data) {
        var source = data.source || {}, origin = source.kind === "manual" ? "人工异常上报" : source.kind === "metric" ? "健康指标 #" + source.metric_id : source.kind === "work_order" ? "服务工单 #" + source.work_order_id : "预警规则 / 历史记录";
        return '<p><strong>' + App.esc(data.summary || "预警") + '</strong></p><p><span class="badge badge-' + (data.closed ? "green" : "yellow") + '">' + App.esc(labels[data.lifecycle_status]) + '</span> · ' + App.esc(data.level_name || data.level) + '</p>' +
            (data.status === "referred" && !data.closed ? '<p class="notice warn">转诊已登记，仍需随访确认实际结果；本预警尚未闭环。</p>' : "") +
            (data.overdue ? '<p class="notice danger">已超过处置截止时间，请优先处理。确认响应不会停止未闭环超时追踪。</p>' : "") +
            '<div class="info-grid">' + kv("上报来源", origin) + kv("触发时间", fmt(data.created_at)) + kv("推送时间（站内）", fmt(data.pushed_at)) +
            kv("首次响应时间", fmt(data.first_responded_at)) + kv("响应耗时（秒）", data.response_seconds) + kv("处置截止", fmt(data.deadline_at)) +
            kv("升级时间", fmt(data.escalated_at)) + kv(data.status === "referred" ? "转诊登记时间" : "处置登记时间", fmt(data.resolved_at)) +
            kv("最终闭环时间", fmt(data.closed_at)) + kv("闭环耗时（秒）", data.closure_seconds) +
            kv("处置措施", data.measure) + kv("处置备注", data.resolution_note) + '</div>';
    };
    H.historyHtml = function (data) {
        var rows = [{ action_at: data.created_at, action_type: "trigger", note: data.summary || "预警生成", operator_id: null }].concat(data.actions);
        return rows.map(function (a) {
            return '<div class="notice" data-alert-action="' + App.esc(a.action_type) + '"><strong>' + App.esc(a.action_type === "trigger" ? "预警触发" : actionNames[a.action_type] || a.action_type) + '</strong><div class="small">' + App.esc(fmt(a.action_at || a.created_at)) + ' · ' + App.esc(a.operator_name || (a.operator_id ? "账号 #" + a.operator_id : "系统")) + (a.channel ? ' · ' + App.esc(a.channel === "sms" ? "模拟短信" : a.channel) : "") + '</div><p>' + App.esc(a.note || "—") + '</p>' + (a.target ? '<div class="small">通知/交接目标：' + App.esc(a.target) + '</div>' : "") + '</div>';
        }).join("");
    };
    H.mountAlert = function () {
        var snapshot = document.getElementById("alert-snapshot"), data = JSON.parse(snapshot.textContent), expectedId = data.id;
        var feedback = document.getElementById("alert-feedback"), busy = false, locked = false;
        var buttons = Array.from(document.querySelectorAll("[data-alert-operation]")), refresh = document.getElementById("alert-refresh");
        function update() {
            document.getElementById("alert-state").innerHTML = H.stateHtml(data);
            document.getElementById("alert-history").innerHTML = H.historyHtml(data);
            var pending = data.status === "pending" && !data.closed, followup = data.status === "referred" && !data.closed;
            buttons.forEach(function (b) {
                var op = b.dataset.alertOperation;
                b.hidden = op === "ack" ? !pending || !!data.first_responded_at : op === "progress" || op === "handle" ? !pending : !followup;
                b.disabled = busy || locked;
            });
            var form = document.getElementById("alert-work-form");
            if (form) {
                document.getElementById("alert-operation-hint").textContent = data.closed ? "本预警已闭环，记录保留供追踪，不再接受新的处置。" : followup ? "请记录接收反馈与随访结果。仅登记转诊不会完成闭环。" : data.first_responded_at ? "已记录首次响应。继续记录实际处置进展，完成后提交结论。" : "请先确认响应。若直接登记进展或处置，系统会同时记录首次响应。";
                document.getElementById("alert-resolution-field").hidden = !pending;
                document.getElementById("alert-measure-field").hidden = !pending;
                document.getElementById("a-note").disabled = data.closed || busy;
                document.getElementById("a-measure").disabled = data.closed || busy;
            }
        }
        if (!H.validAlert(data, expectedId)) { locked = true; H.show(feedback, { title: "追踪数据无法确认", detail: "请刷新核对最新记录。", tone: "warn" }); }
        else { update(); }
        refresh.addEventListener("click", function () {
            if (busy) { return; }
            busy = true; refresh.disabled = true; update();
            App.api.get("/alerts/" + expectedId, { timeoutMs: 15000 }).then(function (res) {
                if (!envelope(res) || !H.validAlert(res.data, expectedId)) { throw invalid(); }
                data = res.data; locked = false;
                H.show(feedback, { title: "追踪记录已刷新", detail: "已取得服务器最新快照，请核对流水后再继续。", tone: "success" });
            }).catch(function (err) { locked = true; H.show(feedback, H.error(err, true)); })
                .finally(function () { busy = false; refresh.disabled = false; update(); });
        });
        buttons.forEach(function (button) { button.addEventListener("click", function () {
            if (busy || locked) { return; }
            var operation = button.dataset.alertOperation, note = document.getElementById("a-note").value.trim(), measure = document.getElementById("a-measure").value.trim();
            if (operation !== "ack" && operation !== "handle" && !note || operation === "handle" && !measure) {
                H.show(feedback, { title: operation === "handle" ? "请填写实际处置措施" : "请填写本次进展或随访记录", detail: "填写内容会保留到追踪流水中。", tone: "danger" });
                var field = document.getElementById(operation === "handle" ? "a-measure" : "a-note");
                field.setAttribute("aria-invalid", "true"); field.focus(); return;
            }
            document.getElementById("a-note").removeAttribute("aria-invalid");
            document.getElementById("a-measure").removeAttribute("aria-invalid");
            var body = operation === "ack" ? {} : operation === "handle" ? { resolution_type: document.getElementById("a-resolution").value, measure: measure, note: note || null } : { note: note };
            if (operation === "followup" || operation === "close") { body.close = operation === "close"; }
            var path = operation === "followup" || operation === "close" ? "follow-up" : operation;
            busy = true; update();
            H.show(feedback, { title: "正在保存本次操作", detail: "请等待确认，不要重复点击。" });
            H.post(operation, "/alerts/" + expectedId + "/" + path, body, expectedId).then(function (result) {
                data = result;
                var message = operation === "ack" ? data.already_acknowledged ? "此前已确认响应，首次响应记录保留" : "已确认响应，首次响应时间已记录" : operation === "handle" ? data.status === "referred" ? "转诊已登记，等待随访闭环" : "处置已记录，本预警已闭环" : operation === "close" ? "随访结果已记录，本预警已闭环" : "本次进展已保存";
                H.show(feedback, { title: message, detail: "操作人与时间已记录，可在下方全过程流水中核对。", tone: "success" });
                if (operation !== "ack") { document.getElementById("a-note").value = ""; }
                if (operation === "handle") { document.getElementById("a-measure").value = ""; }
            }).catch(function (err) {
                var info = H.error(err); locked = info.uncertain || info.login || err.status === 409;
                H.show(feedback, info, [{ text: "查看预警看板", href: "/alerts" }]);
            }).finally(function () { busy = false; update(); });
        }); });
    };
})();
