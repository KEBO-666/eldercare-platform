"use strict";

// Exercise the shipped browser scripts with native Node tools and an inert DOM.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const scripts = ["app.js", "appointment-feedback.js"].map((name) => ({
    name,
    source: fs.readFileSync(path.join(root, "app/static/js", name), "utf8"),
}));

class Element {
    constructor(tag) {
        this.tagName = tag;
        this.children = [];
        this.attributes = {};
        this.style = {};
        this.textContent = "";
    }
    set innerHTML(_value) { throw new Error("Feedback must render text without parsing HTML"); }
    setAttribute(name, value) { this.attributes[name] = value; }
    appendChild(child) { this.children.push(child); return child; }
    replaceChildren(...children) { this.children = children; }
    remove() {}
}

function harness(fetchImpl, firstTimer = 1) {
    const body = new Element("body");
    const timers = new Map();
    const cleared = [];
    let nextTimer = firstTimer;
    const window = {
        location: { pathname: "/appointments/new", search: "?mode=weekly", href: "" },
        setTimeout(callback, delay) {
            const id = nextTimer++;
            timers.set(id, { callback, delay });
            return id;
        },
        clearTimeout(id) { cleared.push(id); timers.delete(id); },
        addEventListener() {},
    };
    const storage = new Map();
    const context = vm.createContext({
        window,
        document: {
            body,
            documentElement: { dataset: {} },
            getElementById(id) { return body.children.find((child) => child.id === id) || null; },
            createElement(tag) { return new Element(tag); },
        },
        fetch: fetchImpl || (() => Promise.reject(new TypeError("Failed to fetch"))),
        AbortController,
        FormData,
        localStorage: { getItem() { return null; }, setItem() {} },
        sessionStorage: {
            getItem(key) { return storage.get(key) || null; },
            setItem(key, value) { storage.set(key, value); },
            removeItem(key) { storage.delete(key); },
        },
    });
    for (const script of scripts) vm.runInContext(script.source, context, { filename: script.name });
    return {
        App: window.App,
        F: window.App.appointmentFeedback,
        window, timers, cleared, body,
        fire(delay) {
            const entry = [...timers].find(([, timer]) => timer.delay === delay);
            assert.ok(entry, `Expected a pending ${delay} ms timer`);
            timers.delete(entry[0]);
            entry[1].callback();
        },
    };
}

function response(status, payload, raw = false) {
    return { status, ok: status >= 200 && status < 300, text: async () => raw ? payload : JSON.stringify(payload) };
}

function deferredFetch(capture) {
    return (_url, init) => {
        capture.signal = init.signal;
        return new Promise((_resolve, reject) => {
            init.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        });
    };
}

test("opt-in request timeout aborts transport and produces REQUEST_TIMEOUT", async () => {
    const capture = {};
    const h = harness(deferredFetch(capture));
    const request = h.App.api.post("/appointments", {}, { timeoutMs: 25 });
    const rejected = assert.rejects(request, (error) => error.code === "REQUEST_TIMEOUT");
    assert.equal(capture.signal.aborted, false);
    h.fire(25);
    await rejected;
    assert.equal(capture.signal.aborted, true);
    assert.equal(h.timers.size, 0);
    assert.equal(h.cleared.length, 1);
});

test("timeout covers a response body that stalls after headers", async () => {
    let signal;
    const h = harness(async (_url, init) => {
        signal = init.signal;
        return { status: 200, ok: true, text: () => new Promise((_resolve, reject) => {
            signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        }) };
    });
    const request = h.App.api.post("/appointments", {}, { timeoutMs: 25 });
    const rejected = assert.rejects(request, (error) => error.code === "REQUEST_TIMEOUT");
    await Promise.resolve();
    h.fire(25);
    await rejected;
    assert.equal(signal.aborted, true);
    assert.equal(h.timers.size, 0);
});

for (const outcome of ["success", "http-error", "network-error", "body-error"]) {
    test(`request clears its timeout on ${outcome}`, async () => {
        let signal;
        const failure = new TypeError("Network interrupted");
        const h = harness(async (_url, init) => {
            signal = init.signal;
            if (outcome === "network-error") throw failure;
            if (outcome === "body-error") return { status: 200, ok: true, text: async () => { throw failure; } };
            return response(outcome === "http-error" ? 409 : 200, { code: outcome === "http-error" ? "CONFLICT" : 0 });
        });
        const request = h.App.api.post("/appointments", {}, { timeoutMs: 25 });
        if (outcome === "success") assert.equal((await request).code, 0);
        else await assert.rejects(request, (error) => outcome === "http-error" ? error.status === 409 : error === failure);
        assert.equal(h.timers.size, 0);
        assert.equal(h.cleared.length, 1);
        assert.equal(signal.aborted, false);
    });
}

test("requests without timeout options retain existing transport behavior", async () => {
    let init;
    const h = harness(async (url, options) => {
        init = options;
        assert.equal(url, "/api/v1/appointments");
        return response(200, { code: 0 });
    });
    await h.App.api.post("/appointments", { reason: "test" });
    assert.equal(init.credentials, "same-origin");
    assert.equal(init.signal, undefined);
    assert.equal(h.timers.size, 0);
});

test("cleanup also clears a valid timer handle of zero", async () => {
    const h = harness(async () => response(200, { code: 0 }), 0);
    await h.App.api.post("/appointments", {}, { timeoutMs: 25 });
    assert.deepEqual(h.cleared, [0]);
    assert.equal(h.timers.size, 0);
});

test("401 preserves unauthorized status, login redirect and clears the request timer", async () => {
    const h = harness(async () => response(401, { code: "UNAUTHORIZED" }));
    let unauthorized;
    await assert.rejects(h.F.post("create", "/appointments", {}), (error) => {
        unauthorized = error;
        return error.status === 401 && error.message === "UNAUTHORIZED";
    });
    assert.equal([...h.timers.values()].some((timer) => timer.delay === h.F.timeoutMs), false);
    assert.equal(h.cleared.length, 1);
    const info = h.F.error(unauthorized, "create");
    assert.equal(info.login, true);
    assert.equal(info.uncertain, false);
    h.fire(600);
    assert.equal(h.window.location.href, "/login?next=%2Fappointments%2Fnew%3Fmode%3Dweekly");
});

for (const [status, code] of [[403, "FORBIDDEN"], [404, "NOT_FOUND"], [409, "INVALID_TRANSITION"],
    [409, "CONFLICT"], [422, "NEEDS_MANUAL"], [422, "VALIDATION_ERROR"], [400, "RULE_VIOLATION"]]) {
    test(`known ${status}/${code} is a confirmed rejection`, () => {
        const { F } = harness();
        const info = F.error({ status, payload: { code, message: "Request rejected", data: [{ loc: ["body", "reason"] }] } }, "create");
        assert.equal(info.rejected, true);
        assert.equal(info.uncertain, false);
        assert.equal(info.login, false);
        assert.match(info.next, /未创建预约/);
        if (code === "VALIDATION_ERROR") assert.match(info.detail, /原因/);
    });
}

const unknownErrors = [
    ["rate-limit", { status: 429 }], ["http-timeout", { status: 408 }],
    ["rate-limit-with-known-code", { status: 429, payload: { code: "BAD_REQUEST" } }],
    ["http-timeout-with-known-code", { status: 408, payload: { code: "RULE_VIOLATION" } }],
    ["server-error", { status: 500, payload: { code: "RULE_VIOLATION" } }],
    ["unavailable", { status: 503 }], ["gateway-timeout", { status: 504 }],
    ["offline", new TypeError("Failed to fetch")], ["client-timeout", { code: "REQUEST_TIMEOUT" }],
    ["invalid-success", { code: "INVALID_RESPONSE" }],
    ["unrecognized-client-error", { status: 422, payload: { code: "PROXY_ERROR" } }],
    ["mismatched-status-and-code", { status: 422, payload: { code: "FORBIDDEN" } }],
    ["code-without-status", { payload: { code: "NEEDS_MANUAL" } }],
    ["inherited-property-code", { status: 400, payload: { code: "toString" } }],
];
for (const [label, error] of unknownErrors) {
    test(`${label} keeps write outcome unknown and preview explicitly read-only`, () => {
        const { F } = harness();
        for (const operation of ["create", "cancel"]) {
            const info = F.error(error, operation);
            assert.equal(info.uncertain, true);
            assert.equal(info.rejected, false);
            assert.match(info.next, /可能已处理/);
            assert.doesNotMatch(info.next, /未创建预约/);
        }
        const preview = F.error(error, "preview");
        assert.equal(preview.uncertain, false);
        assert.match(preview.next, /没有创建预约/);
    });
}

test("malformed validation details cannot suppress the visible rejection feedback", () => {
    const { F } = harness();
    const info = F.error({
        status: 422,
        payload: { code: "VALIDATION_ERROR", data: [null, "proxy detail", {}, { loc: "reason" }, { loc: ["body", "reason"] }] },
    }, "create");
    assert.equal(info.rejected, true);
    assert.equal(info.uncertain, false);
    assert.match(info.detail, /原因/);
    assert.match(info.next, /未创建预约/);
});

for (const data of [null, {}, "proxy detail"]) {
    test(`non-array validation details (${JSON.stringify(data)}) retain usable feedback`, () => {
        const { F } = harness();
        const info = F.error({ status: 422, payload: { code: "VALIDATION_ERROR", data } }, "create");
        assert.equal(info.rejected, true);
        assert.match(info.detail, /表单内容/);
    });
}

const row = { desired_start: "2027-01-01T09:00:00", desired_end: "2027-01-01T10:00:00" };
const preview = { code: 0, data: { reservation_created: false, occurrence_count: 1, occurrences: [row] } };
const created = { code: 0, data: { id: 1, code: "AP-1", status: "pending_accept" } };
const cancelled = { code: 0, data: { id: 1, status: "cancelled", cancellation_fee: 0, released_order_count: 1 } };
for (const [operation, payload] of [["preview", preview], ["create", created], ["cancel", cancelled]]) {
    test(`${operation} accepts a confirmed contract and uses the feedback timeout`, async () => {
        let options;
        const h = harness(async (_url, init) => { options = init; return response(200, payload); });
        const actual = await h.F.post(operation, "/appointments", {});
        assert.equal(actual.code, 0);
        assert.ok(options.signal instanceof AbortSignal);
        assert.equal(h.cleared.length, 1);
        assert.equal(h.timers.size, 0);
    });
}

test("weekly preview and create require four confirmed occurrences", async () => {
    const weeklyPreview = { code: 0, data: { reservation_created: false, occurrence_count: 4, occurrences: Array.from({ length: 4 }, () => ({ ...row })) } };
    const weeklyCreate = { code: 0, data: { ...created.data, occurrence_count: 4, occurrences: Array.from({ length: 4 }, (_, i) => ({ id: i + 1, desired_start: row.desired_start })) } };
    for (const [operation, payload] of [["preview", weeklyPreview], ["create", weeklyCreate]]) {
        const h = harness(async () => response(200, payload));
        assert.equal((await h.F.post(operation, "/appointments", { cycle_rule: "weekly" })).data.occurrence_count, 4);
        payload.data.occurrences.pop();
        await assert.rejects(h.F.post(operation, "/appointments", { cycle_rule: "weekly" }), (error) => error.code === "INVALID_RESPONSE");
    }
});

test("weekly create cannot claim four successes by repeating the same appointment id", async () => {
    const payload = { code: 0, data: { ...created.data, occurrence_count: 4,
        occurrences: Array.from({ length: 4 }, () => ({ id: 1, desired_start: row.desired_start })) } };
    const h = harness(async () => response(200, payload));
    await assert.rejects(h.F.post("create", "/appointments", { cycle_rule: "weekly" }), (error) => error.code === "INVALID_RESPONSE");
});

for (const desired_end of [row.desired_start, "2027-01-01T08:59:00", "not a date"]) {
    test(`preview rejects an empty, inverted or invalid interval (${desired_end})`, async () => {
        const payload = { code: 0, data: { ...preview.data, occurrences: [{ ...row, desired_end }] } };
        const h = harness(async () => response(200, payload));
        await assert.rejects(h.F.post("preview", "/appointments/preview", {}), (error) => error.code === "INVALID_RESPONSE");
    });
}

for (const operation of ["cancel", "no-show", "accept", "to-dispatch"]) {
    test(`${operation} rejects a response for a different appointment id`, async () => {
        const status = { cancel: "cancelled", "no-show": "no_show", accept: "accepted", "to-dispatch": "pending_dispatch" }[operation];
        const h = harness(async () => response(200, { code: 0, data: { ...cancelled.data, id: 2, status } }));
        await assert.rejects(h.F.post(operation, `/appointments/1/${operation}`, { reason: "test" }), (error) => error.code === "INVALID_RESPONSE");
    });
}

for (const [label, payload, raw] of [["HTML", "<html>proxy failure</html>", true], ["empty-body", "", true],
    ["null", null], ["missing-data", { code: 0 }], ["wrong-code", { code: "0", data: created.data }],
    ["nonzero-code", { code: "RULE_VIOLATION", data: created.data }]]) {
    test(`200 ${label} cannot become a successful appointment operation`, async () => {
        const h = harness(async () => response(200, payload, raw));
        for (const operation of ["preview", "create", "cancel"]) {
            await assert.rejects(h.F.post(operation, "/appointments", {}), (error) => {
                assert.equal(error.code, "INVALID_RESPONSE");
                assert.equal(h.F.error(error, operation).uncertain, operation !== "preview");
                return true;
            });
        }
        assert.equal(h.timers.size, 0);
    });
}

for (const [label, overrides] of [["missing-fee", { cancellation_fee: undefined }],
    ["string-fee", { cancellation_fee: "0" }], ["negative-fee", { cancellation_fee: -1 }],
    ["missing-release-count", { released_order_count: undefined }],
    ["wrong-status", { status: "dispatched" }], ["missing-id", { id: undefined }]]) {
    test(`cancel ${label} cannot report success or default to a zero fee`, async () => {
        const h = harness(async () => response(200, { code: 0, data: { ...cancelled.data, ...overrides } }));
        await assert.rejects(h.F.post("cancel", "/appointments/1/cancel", { reason: "test" }), (error) => error.code === "INVALID_RESPONSE");
    });
}

test("server-supplied feedback is rendered as inert text, with an alert and safe navigation", () => {
    const h = harness();
    const target = new Element("section");
    const hostile = '<img src=x onerror="globalThis.compromised=true"><script>attack()</script>';
    const info = h.F.failure(target, { status: 422, payload: { code: "NEEDS_MANUAL", message: hostile } }, "create");
    assert.equal(info.rejected, true);
    assert.equal(target.attributes.role, "alert");
    assert.ok(target.children.some((child) => child.textContent === hostile));
    assert.equal(target.children.some((child) => ["img", "script"].includes(child.tagName)), false);
    const links = target.children.filter((child) => child.tagName === "a");
    assert.equal(links.length, 1);
    assert.equal(links[0].href, "/appointments");
    h.F.success(target, hostile, hostile);
    assert.equal(target.attributes.role, "status");
    assert.equal(target.children[0].textContent, hostile);
    assert.equal(target.children[1].textContent, hostile);
});
