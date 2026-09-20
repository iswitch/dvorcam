(() => {
    const system = window.matchMedia('(prefers-color-scheme: dark)');
    let choice = 'auto';
    try { choice = localStorage.getItem('dvorcam-theme') || 'auto'; } catch (_) {}
    if (!['auto', 'light', 'dark'].includes(choice)) choice = 'auto';
    const apply = () => {
        document.documentElement.dataset.theme = choice === 'auto' ? (system.matches ? 'dark' : 'light') : choice;
        document.documentElement.dataset.bsTheme = document.documentElement.dataset.theme;
        document.querySelectorAll('[data-theme-choice]').forEach(select => { select.value = choice; });
    };
    // Set the palette before CSS paints, including when browser storage is unavailable.
    apply();
    system.addEventListener('change', apply);
    document.addEventListener('DOMContentLoaded', () => {
        apply();
        document.querySelectorAll('[data-theme-choice]').forEach(select => {
            select.addEventListener('change', () => {
                choice = select.value;
                try { localStorage.setItem('dvorcam-theme', choice); } catch (_) {}
                apply();
            });
        });
    });
    window.addEventListener('storage', event => {
        if (event.key !== 'dvorcam-theme' && event.key !== null) return;
        choice = ['light', 'dark'].includes(event.newValue) ? event.newValue : 'auto';
        apply();
    });
})();
