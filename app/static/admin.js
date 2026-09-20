(() => {
    const search = document.querySelector('#camera-search');
    const status = document.querySelector('#status-filter');
    let selectedGroup = '*';
    const filter = () => {
        let total = 0;
        const query = search.value.trim().toLowerCase();
        document.querySelectorAll('.camera-group').forEach(group => {
            let visible = 0;
            group.querySelectorAll('.camera-row').forEach(camera => {
                const state = status.value;
                const match = (selectedGroup === '*' || selectedGroup === group.dataset.groupName)
                    && camera.dataset.search.includes(query)
                    && (state === 'all' || (state === 'online' && camera.dataset.status === 'online')
                    || (state === 'attention' && camera.dataset.status !== 'online')
                    || (state === 'recording' && camera.dataset.record === 'yes'));
                camera.hidden = !match;
                visible += Number(match);
            });
            group.hidden = visible === 0;
            group.querySelector('.group-count').textContent = visible;
            total += visible;
        });
        document.querySelector('#no-results').hidden = total > 0 || !document.querySelector('.camera-row');
        document.querySelector('#result-count').textContent = 'Показано камер: ' + total;
        document.querySelectorAll('[data-group]').forEach(button => {
            const active = button.dataset.group === selectedGroup;
            button.classList.toggle('active', active);
            button.setAttribute('aria-pressed', active);
        });
    };
    if (search) {
        search.addEventListener('input', filter);
        status.addEventListener('change', filter);
        document.querySelectorAll('[data-group]').forEach(button => button.addEventListener('click', () => {
            selectedGroup = button.dataset.group;
            filter();
        }));
        document.querySelector('#reset-filters').addEventListener('click', () => {
            search.value = ''; status.value = 'all'; selectedGroup = '*'; filter(); search.focus();
        });
    }
    document.querySelectorAll('[data-show-password]').forEach(button => button.addEventListener('click', () => {
        const field = document.getElementById(button.dataset.showPassword);
        const visible = field.type === 'password';
        field.type = visible ? 'text' : 'password';
        button.textContent = visible ? 'Скрыть' : 'Показать';
        button.setAttribute('aria-pressed', visible);
        button.setAttribute('aria-label', (visible ? 'Скрыть' : 'Показать') + ' пароль ' + field.id.split('-').pop().toUpperCase());
    }));
    document.querySelectorAll('[data-delete-camera]').forEach(form => form.addEventListener('submit', event => {
        if (!confirm('Удалить камеру «' + form.dataset.deleteCamera + '»? Запись остановится. Старый архив сохранится до плановой очистки.')) event.preventDefault();
    }));
    const cameraForm = document.querySelector('[data-camera-form]');
    cameraForm?.addEventListener('submit', async event => {
        event.preventDefault();
        const button = event.submitter;
        const original = button.textContent;
        const data = new FormData(cameraForm);
        button.disabled = true;
        button.textContent = button.dataset.busyText;
        cameraForm.setAttribute('aria-busy', 'true');
        let message = '';
        try {
            // Keep entered credentials in the form only; a failed probe must not erase the user's edits.
            const response = await fetch(cameraForm.action, {method: 'POST', body: data});
            const target = new URL(response.url);
            message = target.searchParams.get('error') || (!response.ok ? 'Не удалось сохранить камеру. Проверьте подключение и попробуйте ещё раз.' : '');
            if (!message) { window.location.assign(response.url); return; }
        } catch (_) {
            message = 'Нет связи с сервером. Введённые данные сохранены в форме — попробуйте ещё раз.';
        } finally {
            button.disabled = false;
            button.textContent = original;
            cameraForm.removeAttribute('aria-busy');
        }
        let alert = document.querySelector('#camera-form-error');
        if (!alert) {
            alert = document.createElement('div'); alert.id = 'camera-form-error';
            alert.className = 'alert alert-danger'; alert.setAttribute('role', 'alert'); alert.tabIndex = -1;
            cameraForm.prepend(alert);
        }
        alert.textContent = message; alert.focus();
    });
    document.querySelectorAll('[data-busy]').forEach(form => form.addEventListener('submit', event => {
        if (event.defaultPrevented) return;
        const button = event.submitter;
        if (!button) return;
        button.disabled = true;
        button.dataset.originalText = button.textContent;
        button.textContent = button.dataset.busyText || 'Сохраняем…';
        form.setAttribute('aria-busy', 'true');
    }));
    window.addEventListener('pageshow', () => {
        document.querySelectorAll('[data-original-text]').forEach(button => {
            button.disabled = false; button.textContent = button.dataset.originalText;
            button.form?.removeAttribute('aria-busy');
        });
    });
})();
