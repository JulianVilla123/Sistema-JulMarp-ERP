document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.main-nav').forEach(function (nav, navIndex) {
        var items = Array.from(nav.querySelectorAll('.nav-item.dropdown'));
        function closeAll() {
            items.forEach(function (item) {
                item.classList.remove('is-open');
                item.querySelector('.dropdown-toggle').setAttribute('aria-expanded', 'false');
            });
        }
        items.forEach(function (item, index) {
            var toggle = item.querySelector('.dropdown-toggle');
            var menu = item.querySelector('.dropdown-menu');
            menu.id = 'navigation-menu-' + navIndex + '-' + index;
            toggle.setAttribute('aria-controls', menu.id);
            toggle.addEventListener('click', function () {
                var wasOpen = item.classList.contains('is-open');
                closeAll();
                if (!wasOpen) {
                    item.classList.add('is-open');
                    toggle.setAttribute('aria-expanded', 'true');
                }
            });
            menu.addEventListener('click', function (event) {
                if (event.target.closest('a')) closeAll();
            });
        });
        document.addEventListener('click', function (event) {
            if (!event.target.closest('.main-nav .nav-item.dropdown')) closeAll();
        });
        nav.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') {
                var open = nav.querySelector('.is-open > .dropdown-toggle');
                closeAll();
                if (open) open.focus();
            }
        });
        nav.addEventListener('focusout', function (event) {
            if (!nav.contains(event.relatedTarget)) closeAll();
        });
    });
});
