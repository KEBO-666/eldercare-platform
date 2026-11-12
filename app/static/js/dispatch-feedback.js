/* Dispatch recommendations and writes share visible, persistent outcome feedback. */
(function () {
    "use strict";
    var F = App.appointmentFeedback;
    var D = { timeoutMs: 15000 };
    App.dispatchFeedback = D;
    var keys = ["skill", "distance", "load", "rating"];
    var labels = ["技能匹配", "距离", "服务当日负荷", "近30天评分"];
    var states = Object.create(null);
    var feedback = document.getElementById("dispatch-feedback");
    function finite(value) { return typeof value === "number" && Number.isFinite(value); }
    function id(value) { return Number.isInteger(value) && value > 0; }
    function count(value) { return Number.isInteger(value) && value >= 0; }
    function date(value) { return typeof value === "string" && !isNaN(new Date(value).getTime()); }
    function text(value) { return App.esc(value); }
    function number(value, digits) { return Number(value).toFixed(digits === undefined ? 4 : digits); }
    function invalid() { var err = new Error("返回结果无法确认"); err.code = "INVALID_RESPONSE"; return err; }
    var epsilon = 0.00000001;
    function close(a, b) { return finite(a) && finite(b) && Math.abs(a - b) <= epsilon; }
    function clamp(value) { return Math.max(0, Math.min(1, value)); }
    function raw(value) { return String(value); }

    function validRecommendation(res, appointmentId) {
        if (!res || res.code !== 0 || !res.data) { return false; }
        var d = res.data, w = d.weights, r = d.rules;
        if (d.appointment_id !== appointmentId || typeof d.status !== "string" || !Array.isArray(d.candidates) ||
            ![d.eligible_count, d.returned_count, d.evaluated_count, d.excluded_count].every(count) ||
            !id(d.top_n) || d.returned_count !== d.candidates.length || d.eligible_count < d.returned_count ||
            d.evaluated_count !== d.eligible_count + d.excluded_count || !Array.isArray(d.filter_counts) ||
            !w || !Array.isArray(w.values) || w.values.length !== 4 ||
            !w.values.every(function (v) { return finite(v) && v >= 0 && v <= 1; }) ||
            Math.abs(w.values.reduce(function (a, b) { return a + b; }, 0) - 1) > 0.00001 ||
            typeof w.source_label !== "string" || !r || !finite(r.radius_km) || !id(r.daily_limit) ||
            !finite(r.rating_default) || !id(r.rating_window_days) || !finite(r.focus_penalty) ||
            !Array.isArray(r.factor_order) || r.factor_order.join(",") !== keys.join(",") ||
            !finite(r.rating_full_score) || r.rating_full_score <= 0 || !finite(r.score_scale) || r.score_scale <= 0 ||
            r.rating_default < 0 || r.rating_default > r.rating_full_score || !date(d.computed_at) ||
            typeof d.service_date !== "string" || !date(d.desired_start) || !date(d.desired_end) ||
            d.service_date !== d.desired_start.slice(0, 10) || new Date(d.desired_end) <= new Date(d.desired_start) ||
            typeof r.sort_order !== "string" || typeof r.skill_note !== "string") { return false; }
        if (!d.filter_counts.every(function (f) { return f && typeof f.code === "string" && typeof f.label === "string" && count(f.count) && typeof f.suggestion === "string"; }) ||
            d.filter_counts.reduce(function (sum, f) { return sum + f.count; }, 0) !== d.excluded_count ||
            !((d.outcome === "ready" && d.returned_count > 0) || (d.outcome === "no_candidates" && d.eligible_count === 0 && d.returned_count === 0))) { return false; }
        if (d.coordination !== null && !validCoordination(d.coordination)) { return false; }
        var seen = new Set();
        return d.candidates.every(function (c, index) {
            if (!c || !id(c.staff_id) || seen.has(c.staff_id) || c.rank !== index + 1 ||
                typeof c.name !== "string" || !finite(c.distance_km) || !finite(c.distance_km_precision) || c.distance_km_precision < 0 || !count(c.today_count) ||
                !finite(c.allowed_radius_km) || !id(c.daily_limit) || !finite(c.rating_used) ||
                c.allowed_radius_km <= 0 || c.distance_km_precision > c.allowed_radius_km || c.daily_limit !== r.daily_limit ||
                c.today_count >= c.daily_limit || c.rating_used < 0 || c.rating_used > r.rating_full_score ||
                typeof c.is_focus !== "boolean" || !close(c.penalty, c.is_focus ? r.focus_penalty : 1) ||
                typeof c.rating_is_default !== "boolean" || !finite(c.base_score) ||
                c.base_score < -0.00000001 || c.base_score > 1.00000001 || !finite(c.score_precision) || c.score_precision < -0.00000001 || c.score_precision > 1.00000001 ||
                !finite(c.penalty) || c.penalty <= 0 || c.penalty > 1 ||
                !Array.isArray(c.weights_precision) || c.weights_precision.length !== 4 ||
                !c.factors_precision || !c.contributions) { return false; }
            seen.add(c.staff_id);
            if (index > 0) {
                var previous = d.candidates[index - 1];
                if (previous.score_precision < c.score_precision || (previous.score_precision === c.score_precision &&
                    (previous.distance_km_precision > c.distance_km_precision || (previous.distance_km_precision === c.distance_km_precision &&
                    (previous.today_count > c.today_count || (previous.today_count === c.today_count && previous.staff_id > c.staff_id)))))) { return false; }
            }
            var consistent = keys.every(function (key, i) {
                return finite(c.factors_precision[key]) && c.factors_precision[key] >= 0 && c.factors_precision[key] <= 1 &&
                    finite(c.contributions[key]) && c.contributions[key] >= -0.00000001 && c.contributions[key] <= 1.00000001 &&
                    finite(c.weights_precision[i]) && Math.abs(c.weights_precision[i] - w.values[i]) < 0.00000001 &&
                    Math.abs(c.contributions[key] - c.factors_precision[key] * c.weights_precision[i]) < 0.00000001;
            });
            var total = keys.reduce(function (sum, key) { return sum + c.contributions[key]; }, 0);
            var expected = { skill: 1, distance: clamp(1 - c.distance_km_precision / c.allowed_radius_km),
                load: clamp(1 - c.today_count / c.daily_limit), rating: c.rating_used / r.rating_full_score };
            return consistent && keys.every(function (key) { return close(c.factors_precision[key], expected[key]); }) &&
                (!c.rating_is_default || close(c.rating_used, r.rating_default)) &&
                Math.abs(c.base_score - total) < 0.00000001 &&
                Math.abs(c.score_precision - c.base_score * c.penalty) < 0.00000001 &&
                Math.abs(c.weights_precision.reduce(function (a, b) { return a + b; }, 0) - 1) < 0.00000001;
        });
    }
    D.validRecommendation = validRecommendation;

    function validCoordination(c) {
        return c && id(c.id) && typeof c.reason === "string" && c.reason.trim().length > 0 && date(c.requested_at);
    }
    function failure(element, err, operation) {
        var reading = operation === "recommendation";
        var info = F.error(err, reading ? "preview" : "dispatch");
        if (reading) {
            if (!info.login) { info.title = "候选计算暂未完成"; }
            info.next = "本次仅计算候选，没有执行派单。请重新计算或查看预约状态；接口故障不代表无人可派。";
        } else if (info.uncertain) {
            info.next = (operation === "assign" ? "派单" : "人工协调申请") + "结果尚未确认，服务器可能已经处理。请先核对工单与预约列表，避免重复提交。本页已暂停此预约的写入操作。";
        } else if (info.rejected) {
            info.next = "本次请求未获受理。请核对预约最新状态并重新计算候选，不能绕过技能、距离、负荷或时段约束。";
        }
        info.links = [{ text: "查看预约", href: "/appointments" }, { text: "查看工单", href: "/orders" }];
        if (info.login) { info.links.unshift({ text: "重新登录", href: "/login?next=%2Fdispatch" }); }
        F.show(element, info);
        return info;
    }
    function link(element, label, href) {
        var a = document.createElement("a");
        a.className = "btn btn-secondary btn-sm mt-1";
        a.href = href;
        a.textContent = label;
        element.appendChild(a);
    }
    function lockRow(button, message) {
        button.disabled = true;
        var note = button.parentElement.querySelector("[data-dispatch-result]");
        if (!note) {
            note = document.createElement("p");
            note.setAttribute("data-dispatch-result", "true");
            note.setAttribute("role", "status");
            note.style.whiteSpace = "normal";
            note.style.maxWidth = "240px";
            button.parentElement.appendChild(note);
        }
        note.textContent = message;
    }

    function calculationHtml(c, d) {
        var r = d.rules, f = c.factors_precision;
        var expressions = { skill: "通过全部必需技能 = " + raw(f.skill),
            distance: "clamp(1 - " + raw(c.distance_km_precision) + " / " + raw(c.allowed_radius_km) + ", 0, 1) = " + raw(f.distance),
            load: "clamp(1 - " + raw(c.today_count) + " / " + raw(c.daily_limit) + ", 0, 1) = " + raw(f.load),
            rating: raw(c.rating_used) + " / " + raw(r.rating_full_score) + " = " + raw(f.rating) };
        return '<details data-calculation style="overflow-wrap:anywhere"><summary>展开原始数值与计算过程</summary>' +
            '<p class="hint">以下数值来自本次服务器计算；clamp 表示限制在 0–1。简表数字仅作舍入显示，相加可能有末位差异；排序使用未舍入值。</p>' +
            keys.map(function (key, i) { return '<p data-calculation-factor="' + key + '">' + labels[i] + '：' + expressions[key] + '</p>' +
                '<p data-calculation-contribution="' + key + '">' + raw(f[key]) + ' × ' + raw(c.weights_precision[i]) + ' × ' + raw(r.score_scale) + ' = ' + raw(c.contributions[key] * r.score_scale) + ' 分</p>'; }).join("") +
            '<p data-calculation-base>贡献原值之和：(' + keys.map(function (key) { return raw(c.contributions[key]); }).join(' + ') + ') × ' + raw(r.score_scale) + ' = ' + raw(c.base_score * r.score_scale) + ' 分</p>' +
            '<p data-calculation-final>最终：' + raw(c.base_score) + ' × ' + raw(c.penalty) + ' × ' + raw(r.score_scale) + ' = ' + raw(c.score_precision * r.score_scale) + ' 分</p></details>';
    }

    function candidateHtml(c, d) {
        var scale = d.rules.score_scale;
        var factors = keys.map(function (key, i) {
            return '<div class="factor"><div class="factor-head" style="flex-wrap:wrap;gap:4px"><span>' + labels[i] +
                '</span><span>因子 ' + number(c.factors_precision[key]) + '</span></div>' +
                '<div class="progress"><span style="width:' + number(c.factors_precision[key] * 100, 2) + '%"></span></div>' +
                '<div class="small">权重 ' + number(c.weights_precision[i] * 100, 2) + '% · 加权贡献 ' + number(c.contributions[key] * scale) + ' 分</div></div>';
        }).join("");
        return '<section class="card" data-candidate="' + c.staff_id + '" style="margin-bottom:14px"><div class="card-body">' +
            '<div class="flex-between mb-1" style="flex-wrap:wrap;gap:8px"><strong style="font-size:20px">第 ' + c.rank + ' 名 · ' + text(c.name) +
            '</strong><span class="badge badge-blue">综合分 ' + number(c.score_precision * scale) + '</span></div>' +
            '<p class="small">直线距离 ' + number(c.distance_km_precision, 2) + ' km / 可用半径 ' + number(c.allowed_radius_km, 2) +
            ' km；服务日 ' + text(d.service_date) + ' 已派 ' + c.today_count + ' / ' + c.daily_limit + ' 单（不含取消）；' +
            (c.rating_is_default ? '近 ' + d.rules.rating_window_days + ' 天暂无评价，使用默认 ' : '近 ' + d.rules.rating_window_days + ' 天平均评价 ') +
            number(c.rating_used, 2) + ' / ' + raw(d.rules.rating_full_score) + ' 分。</p>' + factors +
            '<div class="notice ' + (c.penalty < 1 ? 'warn' : '') + '">加权合计 ' + number(c.base_score * scale) +
            ' 分 × ' + number(c.penalty, 2) + (c.penalty < 1 ? '（重点关注降权）' : '（无降权）') + ' = 最终 ' + number(c.score_precision * scale) + ' 分。</div>' + calculationHtml(c, d) +
            '<button type="button" class="btn btn-success mt-1" data-assign-nurse="' + c.staff_id + '">' +
            (c.rank === 1 ? '按第 1 名一键派单' : '选择第 ' + c.rank + ' 名派单') + '</button></div></section>';
    }

    function detailsHtml(d) {
        var r = d.rules;
        return '<div class="notice"><strong>本次实际权重</strong><div>' +
            labels.map(function (label, i) { return label + ' ' + number(d.weights.values[i] * 100, 2) + '%'; }).join(' · ') +
            '</div><div>来源：' + text(d.weights.source_label) + (d.weights.fallback ? '（配置无效，已回退）' : '') +
            '</div><div>合格 ' + d.eligible_count + ' 人，本次展示前 ' + d.returned_count + ' 人。计算不会占用护理员时段，派单时将再次校验。</div></div>' +
            '<p class="small">计算时间：' + text(d.computed_at) + '；服务时段：' + text(d.desired_start) + ' 至 ' + text(d.desired_end) + '（服务器本地时间）。页面顶部为打开页面时的配置；本次结果以上述权重为准。</p>' +
            '<details><summary>查看因素定义、惩罚与同分规则</summary><ul style="padding-left:22px;line-height:1.8">' +
            '<li>技能：' + text(r.skill_note) + '</li><li>距离：1 − 直线距离 / 可用服务半径，限制在 0–1；半径取系统与护理员本人半径较小值（系统 ' + number(r.radius_km, 2) + ' km）。</li>' +
            '<li>负荷：1 − 服务当日未取消工单数 / 每日上限 ' + r.daily_limit + ' 单，限制在 0–1；不是今天的工单数，也不是累计单量。</li>' +
            '<li>评分：计算时刻之前最近 ' + r.rating_window_days + ' 天评价均分 / ' + raw(r.rating_full_score) + '；无有效评价使用默认 ' + number(r.rating_default, 2) + ' 分，采用值限制在 0–' + raw(r.rating_full_score) + '。</li>' +
            '<li>四项加权贡献相加后，重点关注护理员乘 ' + number(r.focus_penalty, 2) + '；其余乘 1。显示分数乘 ' + raw(r.score_scale) + '；简表末位可能有舍入差异，原始过程可展开核对。</li>' +
            '<li>排序：' + text(r.sort_order) + '；排序使用未舍入数值。</li></ul></details>';
    }
    D.candidateHtml = candidateHtml;
    D.detailsHtml = detailsHtml;

    D.open = function (button) {
        var appointmentId = Number(button.getAttribute("data-dispatch"));
        if (!id(appointmentId) || button.disabled || document.querySelector('[data-dispatch-dialog="' + appointmentId + '"]')) { return; }
        var state = states[appointmentId] || (states[appointmentId] = { locked: false });
        var pending = false, revision = 0, latest = null, coordinationDraft = "";
        var modal = App.modal({ title: "智能派单 · " + (button.getAttribute("data-code") || appointmentId),
            html: '<div id="dispatch-status" role="status" aria-live="polite"></div>' +
                '<div class="btn-row mb-1"><button type="button" class="btn btn-secondary" data-recalculate>重新计算</button><a class="btn btn-ghost" href="/appointments">查看预约</a></div>' +
                '<div id="dispatch-details"></div><div id="dispatch-candidates" class="mt-1"></div><div id="dispatch-coordinate" class="mt-1"></div>',
            buttons: [{ text: "关闭", cls: "btn-secondary" }] });
        var root = modal.el;
        root.setAttribute("data-dispatch-dialog", String(appointmentId));
        var status = root.querySelector("#dispatch-status");
        var calc = root.querySelector("[data-recalculate]");
        var details = root.querySelector("#dispatch-details");
        var candidates = root.querySelector("#dispatch-candidates");
        var coordination = root.querySelector("#dispatch-coordinate");

        function render(d) {
            latest = d;
            details.innerHTML = detailsHtml(d);
            candidates.innerHTML = d.candidates.map(function (c) { return candidateHtml(c, d); }).join("");
            coordination.replaceChildren();
            if (d.outcome === "no_candidates") {
                F.show(status, { title: "当前无人可派", tone: "warn", detail: "已完成候选计算，当前没有满足全部硬约束的护理员。请核对下方过滤原因，再调整资源或申请人工协调。", next: "人工协调只记录申请并通知处理，不会自动派单，也不能绕过约束。" });
                candidates.innerHTML = '<p>共评估 ' + d.evaluated_count + ' 人，排除 ' + d.excluded_count + ' 人；下列数量按首个未通过条件互斥统计，可相加。</p>' +
                    d.filter_counts.map(function (f) { return '<div class="notice"><strong>' + text(f.label) + '：' + f.count + ' 人</strong>' +
                        '<div>' + text(f.suggestion) + '</div></div>'; }).join("");
                if (!d.coordination) {
                    coordination.innerHTML = '<div class="form-field"><label for="coordination-reason">申请人工协调原因（必填，最多255字）</label>' +
                        '<textarea id="coordination-reason" maxlength="255" aria-describedby="dispatch-status" placeholder="请说明已核对的资源情况和需要协调的事项"></textarea></div>' +
                        '<button type="button" class="btn btn-primary mt-1" data-coordinate>申请人工协调</button>' +
                        '<p class="hint">预约仍保留当前状态；协调成功记录不表示已生成工单。</p>';
                    coordination.querySelector("#coordination-reason").value = coordinationDraft;
                }
            } else {
                F.show(status, { title: "候选计算完成", detail: "以下护理员均通过本次硬约束检查。可按排名选择，提交派单时仍会重新校验。", tone: "success" });
            }
            if (d.coordination) {
                var note = document.createElement("div");
                F.show(note, { title: "已申请人工协调 · 记录 " + d.coordination.id, detail: "原因：" + d.coordination.reason, next: "申请时间：" + d.coordination.requested_at + "。当前阶段不重复提交；尚未因此创建工单。", tone: "success" });
                coordination.appendChild(note);
            }
        }

        function calculate() {
            if (pending || state.locked) { return; }
            var requestRevision = ++revision;
            latest = null;
            calc.disabled = true;
            candidates.replaceChildren();
            details.replaceChildren();
            coordination.replaceChildren();
            F.pending(status, "正在重新计算合格候选…");
            App.api.get("/dispatch/" + appointmentId + "/recommendation", { timeoutMs: D.timeoutMs }).then(function (res) {
                if (!root.isConnected || requestRevision !== revision || pending || state.locked) { return; }
                if (!validRecommendation(res, appointmentId)) { throw invalid(); }
                render(res.data);
            }).catch(function (err) {
                if (!root.isConnected || requestRevision !== revision || pending || state.locked) { return; }
                failure(status, err, "recommendation");
            }).finally(function () {
                if (root.isConnected && requestRevision === revision && !pending && !state.locked) { calc.disabled = false; }
            });
        }

        function write(operation, nurseId) {
            if (pending || state.locked || !latest) { return; }
            var reasonInput = root.querySelector("#coordination-reason");
            var body;
            if (operation === "assign") {
                if (!latest.candidates.some(function (c) { return c.staff_id === nurseId; })) { return; }
                body = { nurse_id: nurseId };
            } else {
                var reason = reasonInput ? reasonInput.value.trim() : "";
                if (latest.outcome !== "no_candidates" || latest.coordination) { return; }
                if (!reason || reason.length > 255) {
                    F.show(status, { title: !reason ? "请填写人工协调原因" : "人工协调原因过长", detail: !reason ? "原因不能为空或只有空格。" : "原因最多255字，请精简后再提交。", next: "填写需要协调的事项后再提交；此操作不会自动派单。", tone: "danger" });
                    reasonInput.setAttribute("aria-invalid", "true");
                    reasonInput.focus();
                    return;
                }
                reasonInput.removeAttribute("aria-invalid");
                coordinationDraft = reason;
                body = { reason: reason };
            }
            pending = true;
            revision += 1;
            button.disabled = true;
            root.setAttribute("aria-busy", "true");
            root.querySelectorAll("button, textarea, select").forEach(function (el) { el.disabled = true; });
            F.pending(status, operation === "assign" ? "正在派单并再次校验硬约束…" : "正在记录人工协调申请…");
            App.api.post("/dispatch/" + appointmentId + "/" + operation, body, { timeoutMs: D.timeoutMs }).then(function (res) {
                var d = res && res.data;
                if (!res || res.code !== 0 || !d || (operation === "assign" ?
                    !id(d.id) || typeof d.code !== "string" || !d.code || d.appointment_id !== appointmentId || d.nurse_id !== nurseId || d.status !== "pending_accept" :
                    d.appointment_id !== appointmentId || ["accepted", "pending_dispatch"].indexOf(d.status) < 0 || !validCoordination(d.coordination) || typeof d.already_recorded !== "boolean" ||
                    d.work_order_created !== false || !count(d.notification_count))) { throw invalid(); }
                pending = false;
                root.removeAttribute("aria-busy");
                root.querySelectorAll(".modal-close, .modal-foot button").forEach(function (el) { el.disabled = false; });
                if (operation === "assign") {
                    state.locked = true;
                    var message = "派单成功，已生成工单 " + d.code + "。";
                    F.success(status, message, "本次操作已确认，工单当前待护理员接单。请打开工单详情查看；本页不会自动刷新或重复派单。");
                    F.success(feedback, message, "当前预约表格尚未刷新，以上派单结果已确认。");
                    link(status, "打开工单详情", "/orders/" + d.id);
                    link(feedback, "打开工单详情", "/orders/" + d.id);
                    lockRow(button, "已派单，工单号 " + d.code + "。请通过上方工单详情查看。");
                } else {
                    var updated = Object.assign({}, latest, { coordination: d.coordination });
                    render(updated);
                    calc.disabled = false;
                    button.disabled = false;
                    F.success(feedback, (d.already_recorded ? "本阶段此前已申请人工协调" : "人工协调申请已记录") + "，记录 " + d.coordination.id + "。", "原因：" + d.coordination.reason + "。预约状态保持不变，没有创建工单；后续由工作人员核对资源与预约时间。");
                }
            }).catch(function (err) {
                pending = false;
                root.removeAttribute("aria-busy");
                var info = failure(status, err, operation);
                failure(feedback, err, operation);
                root.querySelectorAll(".modal-close, .modal-foot button").forEach(function (el) { el.disabled = false; });
                if (reasonInput) { reasonInput.disabled = false; }
                if (info.uncertain || info.login) {
                    state.locked = true;
                    lockRow(button, info.login ? "请重新登录后核对派单结果。" : "写入结果尚未确认，请先查看预约及工单，勿重复操作。");
                } else {
                    latest = null;
                    calc.disabled = false;
                    button.disabled = false;
                    var note = document.createElement("p");
                    note.textContent = "此前候选已失效，请重新计算后再提交。当前填写原因仍保留。";
                    status.appendChild(note);
                }
            });
        }

        root.addEventListener("click", function (event) {
            var target = event.target.closest("[data-recalculate], [data-assign-nurse], [data-coordinate]");
            if (!target || target.disabled) { return; }
            if (target.hasAttribute("data-recalculate")) { calculate(); }
            else if (target.hasAttribute("data-coordinate")) { write("coordinate"); }
            else { write("assign", Number(target.getAttribute("data-assign-nurse"))); }
        });
        root.addEventListener("input", function (event) {
            if (event.target.id === "coordination-reason") {
                coordinationDraft = event.target.value;
                if (coordinationDraft.trim()) { event.target.removeAttribute("aria-invalid"); }
            }
        });
        root.addEventListener("keydown", function (event) {
            if (pending && event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); }
        }, true);
        root.addEventListener("click", function (event) {
            if (pending && event.target === root) { event.preventDefault(); event.stopImmediatePropagation(); }
        }, true);
        calculate();
    };
    document.addEventListener("click", function (event) {
        var button = event.target.closest("[data-dispatch]");
        if (button && !button.disabled) { D.open(button); }
    });
})();
