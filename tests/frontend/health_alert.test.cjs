"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const context = vm.createContext({ App: { esc: (s) => String(s).replaceAll("<", "&lt;").replaceAll('"', "&quot;") }, document: {} });
vm.runInContext(fs.readFileSync(path.resolve(__dirname, "../../app/static/js/health-alert.js"), "utf8"), context);
const H = context.App.healthAlert;
const body = { elder_id: 2, metric_code: "sbp", value: 190 };
function alert() {
    return { id: 1, elder_id: 2, status: "pending", lifecycle_status: "unacknowledged", closed: false,
        summary: "异常", level: "red", created_at: "2026-11-12T12:00:00", actions: [] };
}
function metric(status = "created") {
    return { code: 0, data: { metric_id: 3, metric: { id: 3, ...body }, alerts: status === "created" ? [{ id: 1, level: "red" }] : [], alert_evaluation: { status } } };
}
test("accepts a saved metric only with matching inputs and an explicit evaluation result", () => {
    assert.equal(H.validMetric(metric(), body), true);
    assert.equal(H.validMetric({ code: 0, data: {} }, body), false);
    const r = metric(); r.data.metric.value = 130;
    assert.equal(H.validMetric(r, body), false);
});
for (const status of ["no_match", "cooldown", "no_rules", "error"]) {
    test(`saved metric evaluation ${status} is valid and has a distinct explanation`, () => {
        const r = metric(status);
        assert.equal(H.validMetric(r, body), true);
        assert.ok(H.metricOutcome(r.data).title.includes("指标已保存"));
        if (status === "error") { assert.match(H.metricOutcome(r.data).detail, /不要重复录入/); }
        if (status === "cooldown") { assert.match(H.metricOutcome(r.data).title, /冷却/); }
    });
}
test("does not accept created without alerts, or failed evaluation with successful alerts", () => {
    const r = metric(); r.data.alerts = [];
    assert.equal(H.validMetric(r, body), false);
    r.data.alert_evaluation.status = "error"; r.data.alerts = [{ id: 1, level: "red" }];
    assert.equal(H.validMetric(r, body), false);
});
test("alert snapshot verifies expected id, lifecycle and action ownership", () => {
    const a = alert(); assert.equal(H.validAlert(a, 1), true);
    assert.equal(H.validAlert(a, 7), false);
    a.actions = [{ id: 2, alert_id: 3, action_type: "ack", action_at: a.created_at }];
    assert.equal(H.validAlert(a, 1), false);
});
test("referred remains open until follow-up closure has an actual time", () => {
    const a = { ...alert(), status: "referred", lifecycle_status: "awaiting_followup" };
    assert.equal(H.validAlert(a, 1), true);
    a.closed = true; assert.equal(H.validAlert(a, 1), false);
    a.lifecycle_status = "closed"; assert.equal(H.validAlert(a, 1), false);
    a.closed_at = a.created_at; assert.equal(H.validAlert(a, 1), true);
});
test("progress acknowledgement requires returned matching action and persisted text", () => {
    const a = { ...alert(), lifecycle_status: "handling" }, res = { code: 0, data: a };
    assert.equal(H.validWrite("progress", res, { note: "已联系家属" }, 1), false);
    a.actions = [{ id: 2, alert_id: 1, action_type: "progress", action_at: a.created_at, note: "已联系家属" }];
    assert.equal(H.validWrite("progress", res, { note: "已联系家属" }, 1), true);
});
test("handle acknowledges actual conclusion and measures rather than a generic 200", () => {
    const a = { ...alert(), status: "referred", lifecycle_status: "awaiting_followup", measure: "已交接" };
    assert.equal(H.validWrite("handle", { code: 0, data: a }, { resolution_type: "referred", measure: "已交接" }, 1), true);
    assert.equal(H.validWrite("handle", { code: 0, data: a }, { resolution_type: "resolved", measure: "已交接" }, 1), false);
});
test("manual report verifies elder, summary and level", () => {
    const a = alert(), body = { elder_id: 2, summary: "异常", level: "red" };
    assert.equal(H.validWrite("report", { code: 0, data: a }, body), true);
    assert.equal(H.validWrite("report", { code: 0, data: a }, { ...body, elder_id: 4 }), false);
});
for (const error of [{ code: "REQUEST_TIMEOUT" }, { code: "INVALID_RESPONSE" }, { status: 500 }, { message: "offline" }]) {
    test(`uncertain write ${JSON.stringify(error)} locks duplicate submission`, () => {
        assert.equal(H.error(error).uncertain, true);
        assert.match(H.error(error).detail, /避免重复提交/);
        assert.equal(H.error(error, true).uncertain, false);
    });
}
test("confirmed validation rejection allows correction but forged code/status does not", () => {
    assert.equal(H.error({ status: 422, payload: { code: "RULE_VIOLATION", message: "错误" } }).uncertain, false);
    assert.equal(H.error({ status: 500, payload: { code: "RULE_VIOLATION" } }).uncertain, true);
});
test("expired login is explicit", () => {
    assert.equal(H.error({ status: 401 }).login, true);
    assert.match(H.error({ status: 401 }).title, /登录已过期/);
});
test("history and state escape descriptions, notes and actor names", () => {
    const a = { ...alert(), summary: "<script>alert(1)</script>" };
    a.actions = [{ action_type: "progress", note: "<img onerror=x>", operator_name: "<b>x</b>", action_at: a.created_at }];
    assert.ok(!H.stateHtml(a).includes("<script>"));
    assert.ok(!H.historyHtml(a).includes("<img"));
    assert.match(H.historyHtml(a), /&lt;b/);
});
