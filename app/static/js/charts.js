/* ============================================================
 * 图表封装：优先使用本地 ECharts 5；若资源缺失则退回纯 SVG 绘制
 * 统一接口：AppChart.render(el, spec)
 *   spec = {
 *     type: 'line' | 'bar' | 'pie' | 'hbar',
 *     x: [...],                        // 折线/柱状分类轴
 *     series: [{ name, data:[], color }],
 *     pie: [{ name, value }],
 *     title, suffix, height, legend: true
 *   }
 * ============================================================ */
(function () {
    "use strict";

    var PALETTE = ["#14508c", "#c62828", "#1b7f3b", "#a86400", "#6a1b9a", "#00695c", "#b71c1c", "#283593"];

    function num(v) { var n = Number(v); return isFinite(n) ? n : 0; }

    function echartsOption(spec) {
        var type = spec.type || "line";
        var colors = PALETTE;
        if (type === "pie") {
            return {
                color: colors,
                tooltip: { trigger: "item", textStyle: { fontSize: 15 } },
                legend: { bottom: 0, textStyle: { fontSize: 15 } },
                series: [{
                    type: "pie",
                    radius: ["38%", "66%"],
                    center: ["50%", "46%"],
                    avoidLabelOverlap: true,
                    itemStyle: { borderColor: "#fff", borderWidth: 3 },
                    label: { fontSize: 16, formatter: "{b}\n{c} ({d}%)" },
                    data: spec.pie || []
                }]
            };
        }
        var isBar = type === "bar" || type === "hbar";
        var series = (spec.series || []).map(function (s, i) {
            var base = {
                name: s.name || ("系列" + (i + 1)),
                type: isBar ? "bar" : "line",
                data: (s.data || []).map(num),
                smooth: type === "line",
                symbolSize: 8,
                barMaxWidth: 34,
                itemStyle: { color: s.color || colors[i % colors.length] }
            };
            if (type === "line" && spec.area) {
                base.areaStyle = { opacity: 0.16 };
            }
            return base;
        });
        var option = {
            color: colors,
            tooltip: { trigger: "axis", textStyle: { fontSize: 15 } },
            legend: spec.legend === false || series.length < 2 ? { show: false } : { bottom: 0, textStyle: { fontSize: 15 } },
            grid: { left: 18, right: 26, top: 30, bottom: series.length >= 2 ? 54 : 34, containLabel: true },
            xAxis: {
                type: "category",
                data: spec.x || [],
                axisLabel: { fontSize: 14, interval: "auto", rotate: (spec.x || []).length > 12 ? 35 : 0 },
                axisLine: { lineStyle: { color: "#7f8c9b" } }
            },
            yAxis: {
                type: "value",
                axisLabel: { fontSize: 14 },
                splitLine: { lineStyle: { color: "#e8edf3" } }
            },
            series: series
        };
        if (type === "hbar") {
            option.xAxis = { type: "value", axisLabel: { fontSize: 14 }, splitLine: { lineStyle: { color: "#e8edf3" } } };
            option.yAxis = {
                type: "category",
                data: spec.x || [],
                axisLabel: { fontSize: 14 },
                axisLine: { lineStyle: { color: "#7f8c9b" } }
            };
            option.series = option.series.map(function (s) { return s; });
        }
        if (spec.suffix) {
            option.tooltip.valueFormatter = function (v) { return v + spec.suffix; };
        }
        return option;
    }

    /* -------------------- SVG 兜底 -------------------- */
    var NS = "http://www.w3.org/2000/svg";

    function svgEl(tag, attrs) {
        var el = document.createElementNS(NS, tag);
        Object.keys(attrs || {}).forEach(function (k) { el.setAttribute(k, attrs[k]); });
        return el;
    }

    function textEl(x, y, text, opts) {
        opts = opts || {};
        var t = svgEl("text", {
            x: x, y: y,
            "font-size": opts.size || 13,
            fill: opts.fill || "#4a5561",
            "text-anchor": opts.anchor || "middle",
            "font-weight": opts.weight || "normal"
        });
        t.textContent = text;
        return t;
    }

    function drawEmpty(host, msg) {
        host.innerHTML = '<div class="empty-state"><span class="icon">📈</span>' + (msg || "暂无图表数据") + "</div>";
    }

    function renderLineOrBar(host, spec) {
        var width = host.clientWidth || 640;
        var height = host.clientHeight || 320;
        var pad = { left: 62, right: 22, top: 26, bottom: 52 };
        var x = spec.x || [];
        var series = spec.series || [];
        if (!x.length || !series.length) { drawEmpty(host); return; }
        var allValues = [];
        series.forEach(function (s) { (s.data || []).forEach(function (v) { allValues.push(num(v)); }); });
        var maxV = Math.max.apply(null, allValues.concat([1]));
        var minV = 0;
        var plotW = Math.max(60, width - pad.left - pad.right);
        var plotH = Math.max(60, height - pad.top - pad.bottom);

        var svg = svgEl("svg", { viewBox: "0 0 " + width + " " + height, width: "100%", height: "100%", role: "img" });
        svg.appendChild(svgEl("rect", { x: 0, y: 0, width: width, height: height, fill: "#ffffff" }));
        // 网格 + Y 轴刻度
        var steps = 4;
        for (var i = 0; i <= steps; i += 1) {
            var v = minV + (maxV - minV) * (i / steps);
            var y = pad.top + plotH - plotH * (i / steps);
            svg.appendChild(svgEl("line", { x1: pad.left, y1: y, x2: width - pad.right, y2: y, stroke: "#e8edf3", "stroke-width": 1 }));
            svg.appendChild(textEl(pad.left - 10, y + 5, String(Math.round(v * 10) / 10), { anchor: "end", size: 13 }));
        }
        // 分类轴标签（最多显示 8 个）
        var step = Math.max(1, Math.ceil(x.length / 8));
        x.forEach(function (label, idx) {
            if (idx % step !== 0) { return; }
            var cx = pad.left + (x.length === 1 ? plotW / 2 : plotW * idx / (x.length - 1));
            svg.appendChild(textEl(cx, height - 16, String(label).slice(-5), { size: 12 }));
        });
        var colorSet = PALETTE;
        series.forEach(function (s, si) {
            var color = s.color || colorSet[si % colorSet.length];
            var pts = (s.data || []).map(function (v, idx) {
                var cx = pad.left + (x.length === 1 ? plotW / 2 : plotW * idx / (x.length - 1));
                var cy = pad.top + plotH - plotH * ((num(v) - minV) / Math.max(1, maxV - minV));
                return [cx, cy];
            });
            var isBar = (spec.type === "bar" || spec.type === "hbar");
            if (isBar) {
                var barW = Math.max(6, Math.min(42, plotW / x.length * 0.62));
                pts.forEach(function (p) {
                    svg.appendChild(svgEl("rect", {
                        x: p[0] - barW / 2, y: p[1], width: barW,
                        height: Math.max(1, pad.top + plotH - p[1]), fill: color, rx: 3
                    }));
                });
            } else {
                var d = pts.map(function (p, i) { return (i === 0 ? "M" : "L") + p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" ");
                if (spec.area && pts.length) {
                    var areaD = d + " L" + pts[pts.length - 1][0].toFixed(1) + "," + (pad.top + plotH) + " L" + pts[0][0].toFixed(1) + "," + (pad.top + plotH) + " Z";
                    svg.appendChild(svgEl("path", { d: areaD, fill: color, opacity: 0.14 }));
                }
                svg.appendChild(svgEl("path", { d: d, fill: "none", stroke: color, "stroke-width": 3, "stroke-linejoin": "round", "stroke-linecap": "round" }));
                pts.forEach(function (p) { svg.appendChild(svgEl("circle", { cx: p[0], cy: p[1], r: 4, fill: "#fff", stroke: color, "stroke-width": 3 })); });
            }
        });
        // 图例
        if (series.length >= 2) {
            var lx = pad.left;
            series.forEach(function (s, i) {
                svg.appendChild(svgEl("rect", { x: lx, y: height - 34, width: 14, height: 14, fill: s.color || colorSet[i % colorSet.length], rx: 3 }));
                svg.appendChild(textEl(lx + 20, height - 22, s.name || "系列", { anchor: "start", size: 13 }));
                lx += 24 + String(s.name || "系列").length * 14;
            });
        }
        host.innerHTML = "";
        host.appendChild(svg);
    }

    function renderPie(host, spec) {
        var data = spec.pie || [];
        if (!data.length) { drawEmpty(host); return; }
        var width = host.clientWidth || 520;
        var height = host.clientHeight || 320;
        var cx = width / 2, cy = height / 2, r = Math.min(width, height) / 2 - 46;
        if (r < 40) { r = 40; }
        var total = data.reduce(function (acc, d) { return acc + num(d.value); }, 0) || 1;
        var svg = svgEl("svg", { viewBox: "0 0 " + width + " " + height, width: "100%", height: "100%", role: "img" });
        var angle = -Math.PI / 2;
        data.forEach(function (d, i) {
            var sweep = (num(d.value) / total) * Math.PI * 2;
            var x1 = cx + r * Math.cos(angle), y1 = cy + r * Math.sin(angle);
            var x2 = cx + r * Math.cos(angle + sweep), y2 = cy + r * Math.sin(angle + sweep);
            var large = sweep > Math.PI ? 1 : 0;
            if (sweep >= Math.PI * 1.999) {
                svg.appendChild(svgEl("circle", { cx: cx, cy: cy, r: r, fill: PALETTE[i % PALETTE.length] }));
            } else {
                svg.appendChild(svgEl("path", {
                    d: "M" + cx + "," + cy + " L" + x1.toFixed(2) + "," + y1.toFixed(2) +
                        " A" + r + "," + r + " 0 " + large + " 1 " + x2.toFixed(2) + "," + y2.toFixed(2) + " Z",
                    fill: PALETTE[i % PALETTE.length], stroke: "#fff", "stroke-width": 2
                }));
            }
            var mid = angle + sweep / 2;
            var lx = cx + (r + 26) * Math.cos(mid), ly = cy + (r + 26) * Math.sin(mid);
            svg.appendChild(textEl(lx, ly, d.name + " " + d.value, { size: 13, fill: "#33475b", weight: "bold" }));
            angle += sweep;
        });
        host.innerHTML = "";
        host.appendChild(svg);
    }

    var AppChart = {
        render: function (target, spec) {
            var host = typeof target === "string" ? document.getElementById(target) : target;
            if (!host) { return; }
            spec = spec || {};
            if (spec.height) { host.style.height = spec.height + "px"; }
            var echarts = window.echarts;
            if (echarts && typeof echarts.init === "function") {
                if (host.__chart) { host.__chart.dispose(); }
                var chart = echarts.init(host);
                host.__chart = chart;
                try {
                    chart.setOption(echartsOption(spec));
                } catch (e) {
                    chart.dispose();
                    host.__chart = null;
                    this.fallback(host, spec);
                }
                window.removeEventListener("resize", AppChart._resize);
                AppChart._resize = function () {
                    if (host.__chart) { host.__chart.resize(); }
                };
                window.addEventListener("resize", AppChart._resize);
                return;
            }
            this.fallback(host, spec);
        },
        fallback: function (host, spec) {
            host.innerHTML = "";
            if (spec.type === "pie") { renderPie(host, spec); }
            else { renderLineOrBar(host, spec); }
        },
        /** 从 <script type="application/json" id="..."> 读取数据并渲染 */
        fromElement: function (chartElId, jsonScriptId) {
            var node = document.getElementById(jsonScriptId);
            if (!node) { return; }
            var spec;
            try { spec = JSON.parse(node.textContent); } catch (e) { spec = null; }
            if (!spec) { return; }
            var self = this;
            var draw = function () { self.render(chartElId, spec); };
            draw();
            window.addEventListener("resize", function () {
                var host = document.getElementById(chartElId);
                if (host && !host.__chart) { self.render(chartElId, spec); }
            });
        }
    };

    window.AppChart = AppChart;
})();
