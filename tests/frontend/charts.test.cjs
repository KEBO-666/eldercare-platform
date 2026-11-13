"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../../app/static/js/charts.js"), "utf8");
const night = {
    "--color-text": "#edf2fa",
    "--color-text-muted": "#b8c5d8",
    "--color-border": "#41516b",
    "--color-surface": "#17243a",
    "--chart-colors": "#79b8ff, #ff9b9b, #83d9a5",
};

class Element {
    constructor(tag = "div") {
        this.tagName = tag;
        this.children = [];
        this.attributes = {};
        this.style = {};
        this.clientWidth = 390;
        this.clientHeight = 320;
        this.isConnected = true;
    }
    set innerHTML(value) { this.children = []; this.html = value; }
    appendChild(child) { this.children.push(child); return child; }
    setAttribute(key, value) { this.attributes[key] = value; }
}

function harness({ fallback = false, failOptions = false } = {}) {
    const charts = [];
    const observers = [];
    const listeners = [];
    const tokens = { ...night };
    const window = {
        getComputedStyle() { return { getPropertyValue(name) { return tokens[name] || ""; } }; },
        addEventListener(type, callback) { listeners.push({ type, callback }); },
        ResizeObserver: class {
            constructor(callback) { this.callback = callback; observers.push(this); }
            observe(host) { this.host = host; }
        },
    };
    if (!fallback) {
        window.echarts = {
            init(host) {
                const chart = {
                    host, resizes: 0, disposed: false,
                    setOption(option) {
                        if (failOptions) { throw new Error("ECharts renderer unavailable"); }
                        this.option = option;
                    },
                    resize() { this.resizes += 1; },
                    dispose() { this.disposed = true; },
                };
                charts.push(chart);
                return chart;
            },
        };
    }
    vm.runInContext(source, vm.createContext({
        window,
        document: {
            documentElement: {},
            createElementNS(_namespace, tag) { return new Element(tag); },
            getElementById() { return null; },
        },
    }), { filename: "charts.js" });
    return { app: window.AppChart, charts, observers, listeners, tokens };
}

function lineSpec() {
    return { type: "line", x: ["周一", "周二"], series: [
        { name: "预约", data: [3, 5] }, { name: "完成", data: [2, 4] },
    ] };
}

function descendants(element, tag) {
    return element.children.flatMap((child) => [
        ...(child.tagName === tag ? [child] : []), ...descendants(child, tag),
    ]);
}

test("night charts use the active text, legend, tooltip surface and axis tokens", () => {
    const h = harness();
    for (const type of ["line", "hbar"]) {
        h.app.render(new Element(), { ...lineSpec(), type });
        const option = h.charts.at(-1).option;
        assert.equal(option.textStyle.color, night["--color-text"]);
        assert.equal(option.legend.textStyle.color, night["--color-text-muted"]);
        assert.equal(option.tooltip.textStyle.color, night["--color-text"]);
        assert.equal(option.tooltip.backgroundColor, night["--color-surface"]);
        assert.equal(option.tooltip.borderColor, night["--color-border"]);
        assert.equal(option.xAxis.axisLabel.color, night["--color-text-muted"]);
        assert.equal(option.yAxis.axisLabel.color, night["--color-text-muted"]);
    }
});

test("legacy server palette follows the theme while explicit custom colors survive", () => {
    const h = harness();
    h.app.render(new Element(), { type: "bar", x: ["社区"], series: [
        { data: [1], color: "#14508C" }, { data: [2], color: "#c62828" },
        { data: [3], color: "#ffc107" }, { data: [4] },
    ] });
    const colors = h.charts[0].option.series.map((s) => s.itemStyle.color);
    assert.deepEqual(Array.from(colors), ["#79b8ff", "#ff9b9b", "#ffc107", "#79b8ff"]);
});

test("narrow pie cards keep labels inside and scroll long legends", () => {
    const h = harness();
    h.app.render(new Element(), { type: "pie", pie: [{ name: "很长的社区服务名称", value: 93 }, { name: "其他", value: 7 }] });
    const option = h.charts[0].option;
    const pie = option.series[0];
    assert.equal(option.legend.type, "scroll");
    assert.equal(option.legend.pageTextStyle.color, night["--color-text-muted"]);
    assert.equal(option.tooltip.backgroundColor, night["--color-surface"]);
    assert.equal(pie.itemStyle.borderColor, night["--color-surface"]);
    assert.equal(pie.label.position, "inside");
    assert.equal(pie.labelLine.show, false);
    assert.equal(pie.label.formatter({ percent: 93, name: "很长的社区服务名称" }), "{percent|93%}");
    assert.equal(pie.label.formatter({ percent: 7 }), "{percent|7%}");
    assert.equal(pie.label.formatter({ percent: 6 }), "");
    assert.equal(pie.label.backgroundColor, undefined, "hidden small labels must not paint a background block");
    assert.equal(pie.label.padding, undefined, "hidden small labels must not retain a padded box");
    assert.ok(pie.label.rich.percent.backgroundColor, "visible percentage tokens retain their contrast backing");
    assert.ok(pie.label.rich.percent.padding);
});

test("all charts resize after window or container changes and rerenders do not bind twice", () => {
    const h = harness();
    const first = new Element(), second = new Element();
    h.app.render(first, lineSpec());
    h.app.render(second, lineSpec());
    h.app.render(first, { ...lineSpec(), type: "bar" });
    assert.equal(h.charts[0].disposed, true);
    assert.equal(h.observers.length, 2);
    const resizeListeners = h.listeners.filter((entry) => entry.type === "resize");
    const listenerCount = resizeListeners.length;
    resizeListeners.forEach(({ callback }) => callback());
    assert.equal(h.charts[0].resizes, 0, "disposed chart must not receive resize");
    assert.equal(h.charts[1].resizes, 1, "second card must not be omitted");
    assert.equal(h.charts[2].resizes, 1, "replacement instance must receive resize");
    h.observers.forEach((observer) => observer.callback());
    assert.equal(h.charts[1].resizes, 2);
    assert.equal(h.charts[2].resizes, 2);
    h.app.render(first, lineSpec());
    assert.equal(h.listeners.filter((entry) => entry.type === "resize").length, listenerCount);
    assert.equal(h.observers.length, 2);
    second.isConnected = false;
    h.observers.find((observer) => observer.host === second).callback();
    assert.equal(h.charts[1].resizes, 2, "detached cards should be ignored");
});

test("SVG fallback uses readable night axes, grid, background and pie text", () => {
    const h = harness({ fallback: true });
    const host = new Element();
    h.app.render(host, lineSpec());
    const svg = host.children[0];
    assert.equal(svg.attributes.role, "img");
    assert.equal(svg.children[0].attributes.fill, night["--color-surface"]);
    assert.ok(descendants(svg, "text").every((node) => node.attributes.fill === night["--color-text-muted"]));
    assert.ok(descendants(svg, "line").every((node) => node.attributes.stroke === night["--color-border"]));
    assert.equal(descendants(svg, "path").at(-1).attributes.stroke, "#ff9b9b");
    h.app.render(host, { type: "pie", pie: [{ name: "已完成", value: 10 }, { name: "待办", value: 5 }] });
    assert.ok(descendants(host.children[0], "text").every((node) => node.attributes.fill === night["--color-text"]));
});

test("failed ECharts rendering falls back and resizing redraws current data and theme", () => {
    const h = harness({ failOptions: true });
    const host = new Element();
    h.app.render(host, lineSpec());
    assert.equal(h.charts[0].disposed, true);
    assert.equal(host.children[0].tagName, "svg");
    h.app.render(host, { type: "bar", x: ["新版"], series: [{ data: [9] }] });
    h.tokens["--color-surface"] = "#ffffff";
    host.clientWidth = 720;
    h.observers[0].callback();
    const svg = host.children[0];
    assert.equal(svg.attributes.viewBox, "0 0 720 320");
    assert.equal(svg.children[0].attributes.fill, "#ffffff");
    assert.ok(descendants(svg, "text").some((node) => node.textContent === "新版"));
    assert.equal(h.observers.length, 1);
});
