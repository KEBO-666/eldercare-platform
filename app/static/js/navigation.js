/* The mobile drawer shares the server-rendered, permission-filtered navigation. */
(function () {
    "use strict";
    var sidebar = document.getElementById("sidebar");
    var toggle = document.querySelector(".menu-toggle");
    var closeButton = document.querySelector(".sidebar-close");
    var backdrop = document.querySelector(".sidebar-backdrop");
    var main = document.getElementById("app-main");
    if (!sidebar || !toggle || !closeButton || !backdrop || !main) { return; }
    var mobile = window.matchMedia("(max-width: 900px)");
    var opened = false;

    function setOpen(value, restoreFocus) {
        opened = mobile.matches && value;
        sidebar.classList.toggle("open", opened);
        document.body.classList.toggle("nav-open", opened);
        toggle.setAttribute("aria-expanded", String(opened));
        backdrop.hidden = !opened;
        // Off-screen navigation cannot receive keyboard focus. The background
        // also becomes inert while the drawer is open.
        sidebar.inert = mobile.matches && !opened;
        main.inert = opened;
        if (opened) { closeButton.focus(); }
        else if (restoreFocus) { toggle.focus(); }
    }
    toggle.addEventListener("click", function () { setOpen(!opened, true); });
    closeButton.addEventListener("click", function () { setOpen(false, true); });
    backdrop.addEventListener("click", function () { setOpen(false, true); });
    document.addEventListener("keydown", function (event) {
        if (!opened) { return; }
        if (event.key === "Escape") { event.preventDefault(); setOpen(false, true); return; }
        if (event.key !== "Tab") { return; }
        var controls = Array.from(sidebar.querySelectorAll('a[href], button:not([disabled])'))
            .filter(function (element) { return element.getClientRects().length > 0; });
        var first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    mobile.addEventListener("change", function () {
        var focusWasInSidebar = sidebar.contains(document.activeElement);
        var focusWasOnToggle = document.activeElement === toggle;
        setOpen(false, false);
        if (mobile.matches && focusWasInSidebar) { toggle.focus(); }
        else if (!mobile.matches && focusWasOnToggle) {
            (sidebar.querySelector('[aria-current="page"]') || sidebar.querySelector("a")).focus();
        }
    });
    document.documentElement.dataset.navReady = "true";
    setOpen(false, false);
    window.App.toggleSidebar = function () { setOpen(!opened, true); };
    window.toggleSidebar = window.App.toggleSidebar;
})();
