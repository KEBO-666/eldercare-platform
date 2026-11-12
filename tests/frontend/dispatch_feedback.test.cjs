"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../../app/static/js/dispatch-feedback.js"), "utf8");
const context = vm.createContext({
    App: { appointmentFeedback: {}, esc: (value) => String(value) },
    document: { getElementById() { return {}; }, addEventListener() {} },
});
vm.runInContext(source, context);
const validate = (payload) => context.App.dispatchFeedback.validRecommendation(payload, 12);

function report() {
    return { code: 0, data: {
        appointment_id: 12, status: "pending_dispatch", top_n: 3,
        candidates: [{ staff_id: 8, name: "护理员", rank: 1, distance_km: 1.5,
            today_count: 3, allowed_radius_km: 3, daily_limit: 6,
            rating_used: 3.5, rating_is_default: true,
            base_score: 0.72, score_precision: 0.576, penalty: 0.8,
            weights_precision: [0.4, 0.3, 0.2, 0.1],
            factors_precision: { skill: 1, distance: 0.5, load: 0.5, rating: 0.7 },
            contributions: { skill: 0.4, distance: 0.15, load: 0.1, rating: 0.07 } }],
        eligible_count: 1, returned_count: 1, evaluated_count: 2, excluded_count: 1,
        filter_counts: [{ code: "missing_skills", label: "缺少必需技能", count: 1, suggestion: "协调有技能的护理员" }],
        weights: { values: [0.4, 0.3, 0.2, 0.1], source: "runtime", source_label: "运行期配置", fallback: false },
        rules: { radius_km: 3, daily_limit: 6, rating_default: 3.5, rating_window_days: 30,
            focus_penalty: 0.8, sort_order: "精确总分、距离、负荷、编号", skill_note: "技能是硬约束" },
        coordination: null, outcome: "ready",
    } };
}

test("accepts a consistent recommendation including focus penalty and floating point contributions", () => {
    assert.equal(validate(report()), true);
});

test("valid floating point summation at the upper score bound remains usable", () => {
    const payload = report();
    const c = payload.data.candidates[0];
    payload.data.weights.values = [0.1, 0.2, 0.3, 0.4000000000000001];
    c.weights_precision = [...payload.data.weights.values];
    c.factors_precision = { skill: 1, distance: 1, load: 1, rating: 1 };
    c.contributions = Object.fromEntries(["skill", "distance", "load", "rating"].map((key, i) => [key, c.weights_precision[i]]));
    c.penalty = 1;
    c.base_score = Object.values(c.contributions).reduce((a, b) => a + b, 0);
    c.score_precision = c.base_score;
    assert.ok(c.base_score > 1);
    assert.equal(validate(payload), true);
});

test("valid empty recommendation is distinct from a failed or missing response", () => {
    const payload = report();
    Object.assign(payload.data, { candidates: [], eligible_count: 0, returned_count: 0,
        evaluated_count: 1, excluded_count: 1, outcome: "no_candidates" });
    assert.equal(validate(payload), true);
    assert.equal(validate({ code: 0, data: {} }), false);
    assert.equal(validate({ code: 500, data: payload.data }), false);
});

test("accepts a zero-workforce result without inventing a candidate", () => {
    const payload = report();
    Object.assign(payload.data, { candidates: [], eligible_count: 0, returned_count: 0,
        evaluated_count: 0, excluded_count: 0, filter_counts: [], outcome: "no_candidates" });
    assert.equal(validate(payload), true);
});

test("a recorded coordination must include its actual reason and timestamp", () => {
    const payload = report();
    payload.data.coordination = { id: 7, reason: "需协调人员", requested_at: "2026-11-12T19:00:00" };
    assert.equal(validate(payload), true);
    payload.data.coordination.reason = "  ";
    assert.equal(validate(payload), false);
    payload.data.coordination = { id: 7, reason: "需协调人员", requested_at: "invalid" };
    assert.equal(validate(payload), false);
});

const invalid = [
    ["wrong appointment", (d) => { d.appointment_id = 13; }],
    ["missing weight metadata", (d) => { delete d.weights; }],
    ["non-normalized weights", (d) => { d.weights.values = [0.4, 0.3, 0.2, 0.2]; }],
    ["nonfinite weights", (d) => { d.weights.values[0] = NaN; }],
    ["incorrect returned count", (d) => { d.returned_count = 0; }],
    ["incorrect evaluated count", (d) => { d.evaluated_count = 3; }],
    ["overlapping rejection counts", (d) => { d.filter_counts[0].count = 2; }],
    ["fractional rejection count", (d) => { d.filter_counts[0].count = 0.5; }],
    ["ready with no candidates", (d) => { d.candidates = []; d.returned_count = 0; }],
    ["false empty outcome", (d) => { d.outcome = "no_candidates"; }],
    ["duplicate candidate", (d) => { d.candidates.push({ ...d.candidates[0], rank: 2 }); d.returned_count = 2; d.eligible_count = 2; d.evaluated_count = 3; }],
    ["incorrect rank", (d) => { d.candidates[0].rank = 2; }],
    ["invalid nursing identifier", (d) => { d.candidates[0].staff_id = 0; }],
    ["missing default-rating disclosure", (d) => { delete d.candidates[0].rating_is_default; }],
    ["nonfinite final score", (d) => { d.candidates[0].score_precision = Infinity; }],
    ["factor outside its bounds", (d) => { d.candidates[0].factors_precision.skill = 1.2; }],
    ["nonfinite contribution", (d) => { d.candidates[0].contributions.skill = NaN; }],
    ["factor and contribution disagree", (d) => { d.candidates[0].contributions.skill = 0.3; }],
    ["base score and sum disagree", (d) => { d.candidates[0].base_score = 0.9; }],
    ["penalty and final score disagree", (d) => { d.candidates[0].score_precision = 0.72; }],
    ["candidate and report weights disagree", (d) => { d.candidates[0].weights_precision = [0.1, 0.2, 0.3, 0.4]; }],
];
for (const [name, mutate] of invalid) {
    test(`damaged recommendation cannot enable assignment: ${name}`, () => {
        const payload = report();
        mutate(payload.data);
        assert.equal(validate(payload), false);
    });
}
