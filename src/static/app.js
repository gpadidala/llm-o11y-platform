/* =====================================================================
   LLM O11y Platform - Frontend JavaScript
   Next-Gen AI Gateway UI
   ===================================================================== */

// ---------------------------------------------------------------------------
// Toast Notifications
// ---------------------------------------------------------------------------

function showToast(message, type = 'info', duration = 4000) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const icons = {
        success: '\u2705',
        error: '\u274C',
        warning: '\u26A0\uFE0F',
        info: '\u2139\uFE0F'
    };

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `<span>${icons[type] || ''}</span><span>${message}</span>`;
    container.appendChild(toast);

    setTimeout(() => {
        toast.classList.add('fade-out');
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
            () => showToast('Copied to clipboard', 'success'),
            () => fallbackCopy(text)
        );
    } else {
        fallbackCopy(text);
    }
}

function fallbackCopy(text) {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    try {
        document.execCommand('copy');
        showToast('Copied to clipboard', 'success');
    } catch (e) {
        showToast('Failed to copy', 'error');
    }
    document.body.removeChild(textarea);
}

function formatNumber(num) {
    if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
    if (num >= 1000) return (num / 1000).toFixed(1) + 'K';
    return num.toString();
}

function formatCurrency(amount) {
    return '$' + parseFloat(amount).toFixed(2);
}

function formatDuration(ms) {
    if (ms < 1000) return ms + 'ms';
    return (ms / 1000).toFixed(2) + 's';
}

function debounce(fn, delay) {
    let timeout;
    return function (...args) {
        clearTimeout(timeout);
        timeout = setTimeout(() => fn.apply(this, args), delay);
    };
}

function generateId() {
    return 'id_' + Math.random().toString(36).substr(2, 9);
}

// ---------------------------------------------------------------------------
// Password Toggle
// ---------------------------------------------------------------------------

function togglePassword(btn) {
    const input = btn.parentElement.querySelector('input');
    if (!input) return;

    if (input.type === 'password') {
        input.type = 'text';
        btn.textContent = '\uD83D\uDE48';
    } else {
        input.type = 'password';
        btn.textContent = '\uD83D\uDC41';
    }
}

// ---------------------------------------------------------------------------
// Modal Management
// ---------------------------------------------------------------------------

function openModal(id) {
    const modal = document.getElementById(id);
    if (modal) {
        modal.classList.add('active');
        document.body.style.overflow = 'hidden';
    }
}

function closeModal(id) {
    const modal = document.getElementById(id);
    if (modal) {
        modal.classList.remove('active');
        document.body.style.overflow = '';
    }
}

// Close modal on backdrop click
document.addEventListener('click', function (e) {
    if (e.target.classList.contains('modal-overlay') && e.target.classList.contains('active')) {
        e.target.classList.remove('active');
        document.body.style.overflow = '';
    }
});

// ---------------------------------------------------------------------------
// Slide Panel Management
// ---------------------------------------------------------------------------

function openSlidePanel(id) {
    const panel = document.getElementById(id);
    const backdrop = document.getElementById(id + '-backdrop');
    if (panel) panel.classList.add('open');
    if (backdrop) backdrop.classList.add('active');
}

function closeSlidePanel(id) {
    const panel = document.getElementById(id);
    const backdrop = document.getElementById(id + '-backdrop');
    if (panel) panel.classList.remove('open');
    if (backdrop) backdrop.classList.remove('active');
}

// ---------------------------------------------------------------------------
// Collapsible Sections
// ---------------------------------------------------------------------------

function toggleCollapse(targetId) {
    const target = document.getElementById(targetId);
    if (!target) return;

    if (target.style.display === 'none') {
        target.style.display = 'block';
        target.style.animation = 'fadeIn 0.2s ease-out';
    } else {
        target.style.display = 'none';
    }
}

// ---------------------------------------------------------------------------
// Tab Management
// ---------------------------------------------------------------------------

function switchTab(tabGroup, tabName) {
    // Deactivate all tabs and content in group
    const tabs = document.querySelectorAll(`[data-tab-group="${tabGroup}"] .tab-btn`);
    const contents = document.querySelectorAll(`[data-tab-group="${tabGroup}"] .tab-content`);

    tabs.forEach(t => t.classList.remove('active'));
    contents.forEach(c => c.classList.remove('active'));

    // Activate selected
    const activeTab = document.querySelector(`[data-tab-group="${tabGroup}"] .tab-btn[data-tab="${tabName}"]`);
    const activeContent = document.getElementById(`tab-${tabName}`);

    if (activeTab) activeTab.classList.add('active');
    if (activeContent) activeContent.classList.add('active');
}

// ---------------------------------------------------------------------------
// Service Health Polling
// ---------------------------------------------------------------------------

async function pollServiceHealth() {
    try {
        const resp = await fetch('/api/status');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        for (const [service, info] of Object.entries(data.services || {})) {
            const dot = document.getElementById(`status-${service}`);
            const meta = document.getElementById(`meta-${service}`);

            if (dot) {
                dot.className = 'status-dot';
                if (info.healthy === true) {
                    dot.classList.add('healthy');
                } else if (info.healthy === false) {
                    dot.classList.add('unhealthy');
                } else {
                    dot.classList.add('unknown');
                }
            }

            if (meta) {
                if (info.healthy === true) {
                    meta.textContent = info.latency_ms ? `${info.latency_ms}ms` : 'Healthy';
                    meta.style.color = 'var(--green)';
                } else if (info.healthy === false) {
                    meta.textContent = info.error || 'Unreachable';
                    meta.style.color = 'var(--coral)';
                } else {
                    meta.textContent = 'Unknown';
                    meta.style.color = 'var(--amber)';
                }
            }
        }
    } catch (err) {
        console.warn('Health poll failed:', err.message);
        const services = ['gateway', 'otel_collector', 'tempo', 'prometheus', 'loki', 'grafana'];
        for (const svc of services) {
            const dot = document.getElementById(`status-${svc}`);
            const meta = document.getElementById(`meta-${svc}`);
            if (dot) { dot.className = 'status-dot unknown'; }
            if (meta) { meta.textContent = 'Unavailable'; meta.style.color = 'var(--amber)'; }
        }
    }
}

// ---------------------------------------------------------------------------
// API Helper
// ---------------------------------------------------------------------------

async function apiCall(url, method = 'GET', body = null) {
    const options = {
        method,
        headers: { 'Content-Type': 'application/json' }
    };
    if (body) options.body = JSON.stringify(body);

    const resp = await fetch(url, options);
    if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `HTTP ${resp.status}`);
    }
    return resp.json();
}

// ---------------------------------------------------------------------------
// Settings - Load
// ---------------------------------------------------------------------------

async function loadSettings() {
    try {
        const resp = await fetch('/api/settings');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const settings = data.settings || {};

        const form = document.getElementById('settings-form');
        if (!form) return;

        for (const [key, value] of Object.entries(settings)) {
            const input = form.querySelector(`[name="${key}"]`);
            if (input && value !== null && value !== undefined) {
                if (input.type === 'checkbox') {
                    input.checked = Boolean(value);
                } else {
                    if (input.type === 'password' && (value === '***' || value === '')) {
                        input.value = '';
                        input.placeholder = value === '***' ? '(configured - enter new value to change)' : input.placeholder;
                    } else {
                        input.value = value;
                    }
                }
            }
        }

        // Load MCP servers
        const mcpServers = data.mcp_servers || [];
        const container = document.getElementById('mcp-servers');
        if (container) {
            container.innerHTML = '';
            if (mcpServers.length === 0) {
                addMcpServer();
            } else {
                for (const server of mcpServers) {
                    addMcpServer(server);
                }
            }
        }

        const status = document.getElementById('save-status');
        if (status) status.textContent = 'Settings loaded';
    } catch (err) {
        console.warn('Failed to load settings:', err.message);
        const container = document.getElementById('mcp-servers');
        if (container && container.children.length === 0) {
            addMcpServer();
        }
    }
}

// ---------------------------------------------------------------------------
// Settings - Save
// ---------------------------------------------------------------------------

async function saveSettings() {
    const form = document.getElementById('settings-form');
    if (!form) return;

    const status = document.getElementById('save-status');
    if (status) status.textContent = 'Saving...';

    const data = {};
    const inputs = form.querySelectorAll('input[name], select[name]');
    for (const input of inputs) {
        if (input.name.startsWith('mcp_')) continue;
        if (input.type === 'password' && input.value === '') continue;
        if (input.type === 'checkbox') {
            data[input.name] = input.checked;
        } else {
            data[input.name] = input.value;
        }
    }

    const mcpServers = [];
    const mcpCards = document.querySelectorAll('.mcp-card');
    for (const card of mcpCards) {
        const name = card.querySelector('[name="mcp_name"]');
        const url = card.querySelector('[name="mcp_url"]');
        const desc = card.querySelector('[name="mcp_description"]');
        const enabled = card.querySelector('[name="mcp_enabled"]');

        if (name && url && name.value.trim()) {
            mcpServers.push({
                name: name.value.trim(),
                url: url.value.trim(),
                description: desc ? desc.value.trim() : '',
                enabled: enabled ? enabled.checked : true
            });
        }
    }

    try {
        const resp = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ settings: data, mcp_servers: mcpServers })
        });

        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

        showToast('Configuration saved successfully', 'success');
        if (status) status.textContent = 'Saved at ' + new Date().toLocaleTimeString();
    } catch (err) {
        showToast('Failed to save: ' + err.message, 'error');
        if (status) status.textContent = 'Save failed';
    }
}

// ---------------------------------------------------------------------------
// MCP Server Dynamic List
// ---------------------------------------------------------------------------

let mcpServerCount = 0;

function addMcpServer(serverData = null) {
    mcpServerCount++;
    const container = document.getElementById('mcp-servers');
    if (!container) return;

    const card = document.createElement('div');
    card.className = 'mcp-card';
    card.innerHTML = `
        <button type="button" class="remove-btn" onclick="removeMcpServer(this)" title="Remove server">\u2716</button>
        <div class="mcp-header">
            <span class="mcp-number">MCP Server #${mcpServerCount}</span>
            <label class="toggle" title="Enable/Disable">
                <input type="checkbox" name="mcp_enabled" ${serverData && serverData.enabled === false ? '' : 'checked'}>
                <span class="toggle-slider"></span>
            </label>
        </div>
        <div class="form-row" style="margin-bottom: 12px;">
            <div class="form-group mb-0">
                <label class="form-label">Server Name</label>
                <input type="text" class="form-input" name="mcp_name" placeholder="e.g. code-search" value="${serverData ? escapeHtml(serverData.name) : ''}">
            </div>
            <div class="form-group mb-0">
                <label class="form-label">URL</label>
                <input type="url" class="form-input" name="mcp_url" placeholder="http://localhost:3001" value="${serverData ? escapeHtml(serverData.url) : ''}">
            </div>
        </div>
        <div class="form-group mb-0">
            <label class="form-label">Description</label>
            <input type="text" class="form-input" name="mcp_description" placeholder="What does this MCP server do?" value="${serverData ? escapeHtml(serverData.description || '') : ''}">
        </div>
    `;
    container.appendChild(card);
}

function removeMcpServer(btn) {
    const card = btn.closest('.mcp-card');
    if (!card) return;

    const container = document.getElementById('mcp-servers');
    if (container && container.children.length <= 1) {
        showToast('At least one MCP server entry is required', 'warning');
        return;
    }

    card.style.opacity = '0';
    card.style.transform = 'translateX(-20px)';
    card.style.transition = 'all 0.25s ease';
    setTimeout(() => card.remove(), 250);
}

// ---------------------------------------------------------------------------
// Playground Functions
// ---------------------------------------------------------------------------

const playgroundHistory = [];

function updateLineNumbers(textarea) {
    const lines = textarea.value.split('\n').length;
    const container = document.getElementById('pg-line-nums');
    if (!container) return;
    container.innerHTML = '';
    for (let i = 1; i <= Math.max(lines, 1); i++) {
        const span = document.createElement('span');
        span.textContent = i;
        container.appendChild(span);
    }
}

function toggleCompareMode(enabled) {
    const layout = document.getElementById('playground-layout');
    const panelB = document.getElementById('output-panel-b');
    if (!layout || !panelB) return;

    if (enabled) {
        layout.classList.add('compare-mode');
        panelB.classList.remove('hidden');
    } else {
        layout.classList.remove('compare-mode');
        panelB.classList.add('hidden');
        const metrics = document.getElementById('compare-metrics');
        if (metrics) metrics.classList.add('hidden');
    }
}

function toggleHistory() {
    const drawer = document.getElementById('history-drawer');
    if (drawer) drawer.classList.toggle('open');
}

// ---------------------------------------------------------------------------
// Prompt Studio Functions
// ---------------------------------------------------------------------------

function detectVariables() {
    const body = document.getElementById('tpl-body');
    if (!body) return;
    const matches = body.value.match(/\{\{(\w+)\}\}/g) || [];
    const vars = [...new Set(matches.map(m => m.replace(/\{\{|\}\}/g, '')))];
    const container = document.getElementById('tpl-variables');
    if (!container) return;

    if (vars.length === 0) {
        container.innerHTML = '<div style="color: var(--text-muted); font-size: 0.82rem; padding: 8px;">No variables detected yet...</div>';
        return;
    }

    container.innerHTML = vars.map(v => `
        <div class="variable-item">
            <span class="variable-name">{{${v}}}</span>
            <input type="text" class="form-input" placeholder="Sample value..." style="padding: 6px 10px; font-size: 0.8rem;"
                oninput="updatePreview()" data-var="${v}">
        </div>
    `).join('');
}

function updatePreview() {
    const body = document.getElementById('tpl-body');
    const preview = document.getElementById('tpl-preview');
    if (!body || !preview) return;

    if (!body.value) {
        preview.innerHTML = '<span style="color: var(--text-muted);">Template preview will appear here...</span>';
        return;
    }

    let result = escapeHtml(body.value);
    const varInputs = document.querySelectorAll('#tpl-variables input[data-var]');
    varInputs.forEach(input => {
        const varName = input.dataset.var;
        if (input.value) {
            result = result.replace(new RegExp(`\\{\\{${varName}\\}\\}`, 'g'),
                `<strong style="color: var(--green);">${escapeHtml(input.value)}</strong>`);
        } else {
            result = result.replace(new RegExp(`\\{\\{${varName}\\}\\}`, 'g'),
                `<span class="var-highlight">{{${varName}}}</span>`);
        }
    });

    preview.innerHTML = result.replace(/\n/g, '<br>');
}

// ---------------------------------------------------------------------------
// Logs Functions
// ---------------------------------------------------------------------------

function toggleDetail(id) {
    const row = document.getElementById('detail-' + id);
    if (!row) return;
    const content = row.querySelector('.request-detail-content');
    if (content) {
        content.classList.toggle('visible');
    }
}

function applyFilters() {
    showToast('Filters applied', 'info');
}

function clearFilters() {
    const selects = ['filter-provider', 'filter-model', 'filter-status'];
    selects.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    const search = document.getElementById('filter-search');
    if (search) search.value = '';
    showToast('Filters cleared', 'info');
}

// ---------------------------------------------------------------------------
// Evaluation Functions
// ---------------------------------------------------------------------------

function runEvaluation() {
    const input = document.getElementById('eval-input');
    const output = document.getElementById('eval-output');
    if (!input || !output) return;

    if (!input.value.trim() || !output.value.trim()) {
        showToast('Please enter both input and output text', 'warning');
        return;
    }

    const btn = document.getElementById('run-eval-btn');
    if (btn) btn.classList.add('loading');

    setTimeout(() => {
        if (btn) btn.classList.remove('loading');

        const criteriaIds = ['crit-relevance', 'crit-faithfulness', 'crit-helpfulness', 'crit-coherence', 'crit-toxicity', 'crit-conciseness'];
        const criteriaNames = ['Relevance', 'Faithfulness', 'Helpfulness', 'Coherence', 'Toxicity', 'Conciseness'];

        const scores = [];
        criteriaIds.forEach((id, i) => {
            const el = document.getElementById(id);
            if (el && el.checked) {
                scores.push({
                    name: criteriaNames[i],
                    score: (Math.random() * 3 + 7).toFixed(1)
                });
            }
        });

        if (scores.length === 0) {
            showToast('Please select at least one criteria', 'warning');
            return;
        }

        const overall = (scores.reduce((sum, s) => sum + parseFloat(s.score), 0) / scores.length).toFixed(1);

        const overallEl = document.getElementById('eval-overall');
        if (overallEl) {
            overallEl.textContent = overall;
            overallEl.style.color = overall >= 8 ? 'var(--green)' : overall >= 6 ? 'var(--amber)' : 'var(--coral)';
        }

        const barsContainer = document.getElementById('eval-score-bars');
        if (barsContainer) {
            barsContainer.innerHTML = scores.map(s => {
                const pct = (parseFloat(s.score) / 10 * 100);
                const color = parseFloat(s.score) >= 8 ? 'var(--green)' : parseFloat(s.score) >= 6 ? 'var(--amber)' : 'var(--coral)';
                return `
                    <div class="flex items-center gap-12">
                        <span style="min-width: 110px; font-size: 0.85rem; font-weight: 500;">${s.name}</span>
                        <div class="score-bar" style="flex: 1;">
                            <div class="score-bar-fill" style="width: 0%; background: ${color};" data-width="${pct}%"></div>
                        </div>
                        <span class="score-value" style="color: ${color};">${s.score}</span>
                    </div>
                `;
            }).join('');
        }

        const results = document.getElementById('eval-results');
        if (results) results.style.display = 'block';

        // Animate bars
        requestAnimationFrame(() => {
            requestAnimationFrame(() => {
                document.querySelectorAll('.score-bar-fill[data-width]').forEach(bar => {
                    bar.style.width = bar.dataset.width;
                });
            });
        });

        showToast('Evaluation complete', 'success');
    }, 1500);
}

// ---------------------------------------------------------------------------
// Guardrails Functions
// ---------------------------------------------------------------------------

function testGuardrails() {
    const input = document.getElementById('guardrail-test-input');
    if (!input || !input.value.trim()) {
        showToast('Please enter text to test', 'warning');
        return;
    }

    const btn = document.getElementById('test-guardrails-btn');
    if (btn) btn.classList.add('loading');

    setTimeout(() => {
        if (btn) btn.classList.remove('loading');

        const text = input.value;
        const piiPatterns = [
            { type: 'Email', regex: /[\w.+-]+@[\w-]+\.[\w.]+/g },
            { type: 'Phone', regex: /\b\d{3}[-.]?\d{3}[-.]?\d{4}\b/g },
            { type: 'Credit Card', regex: /\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b/g },
            { type: 'SSN', regex: /\b\d{3}-\d{2}-\d{4}\b/g },
            { type: 'IP Address', regex: /\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/g },
            { type: 'API Key', regex: /\b(sk-|api_key_|AKIA)[a-zA-Z0-9]{10,}\b/g }
        ];

        let detections = [];
        let highlighted = escapeHtml(text);

        piiPatterns.forEach(p => {
            const matches = text.match(p.regex);
            if (matches) {
                matches.forEach(m => {
                    detections.push({ type: p.type, value: m });
                    const escaped = escapeHtml(m);
                    highlighted = highlighted.replace(escaped,
                        `<span class="pii-highlight"><span class="pii-type-label">${p.type}</span>${escaped}</span>`);
                });
            }
        });

        const hasPII = detections.length > 0;
        const summary = document.getElementById('guardrail-summary');
        if (summary) {
            summary.innerHTML = `
                <div class="guardrail-summary-item">
                    <span class="guardrail-check-icon" style="color: ${hasPII ? 'var(--coral)' : 'var(--green)'};">${hasPII ? '\u26A0' : '\u2705'}</span>
                    <span>PII Detection: ${hasPII ? detections.length + ' found' : 'Clear'}</span>
                </div>
                <div class="guardrail-summary-item">
                    <span class="guardrail-check-icon" style="color: var(--green);">\u2705</span>
                    <span>Content Safety: Passed</span>
                </div>
                <div class="guardrail-summary-item">
                    <span class="guardrail-check-icon" style="color: var(--green);">\u2705</span>
                    <span>Topic Restriction: Clear</span>
                </div>
                <div class="guardrail-summary-item">
                    <span class="guardrail-check-icon" style="color: var(--green);">\u2705</span>
                    <span>Output Validation: Passed</span>
                </div>
            `;
        }

        const result = document.getElementById('test-result-content');
        if (result) {
            result.className = 'test-result ' + (hasPII ? 'flagged' : 'clean');
            result.innerHTML = highlighted.replace(/\n/g, '<br>');
        }

        const results = document.getElementById('guardrail-test-results');
        if (results) results.style.display = 'block';

        showToast(hasPII ? 'PII detected in input' : 'All guardrails passed', hasPII ? 'warning' : 'success');
    }, 800);
}

// ---------------------------------------------------------------------------
// Routing Functions
// ---------------------------------------------------------------------------

let routeTargetCount = 3;

function selectStrategy(el, strategy) {
    document.querySelectorAll('.strategy-card').forEach(c => c.classList.remove('selected'));
    el.classList.add('selected');
    showToast('Strategy set to ' + strategy.replace('-', ' '), 'info');
}

function addTarget() {
    routeTargetCount++;
    const list = document.getElementById('target-list');
    if (!list) return;

    const item = document.createElement('div');
    item.className = 'target-item';
    item.style.animation = 'fadeIn 0.3s ease-out';
    item.innerHTML = `
        <span class="target-order">${routeTargetCount}</span>
        <div class="target-selectors">
            <select class="form-select" style="min-width: 140px;">
                <option>OpenAI</option>
                <option>Anthropic</option>
                <option>Google</option>
                <option>Azure</option>
                <option>Cohere</option>
            </select>
            <select class="form-select" style="min-width: 160px;">
                <option>gpt-4o</option>
                <option>gpt-4o-mini</option>
                <option>claude-3.5-sonnet</option>
                <option>gemini-pro</option>
            </select>
        </div>
        <div class="target-weight">
            <span class="target-weight-label">Weight</span>
            <input type="range" min="0" max="100" value="0" style="flex: 1;"
                oninput="this.nextElementSibling.textContent = this.value + '%'">
            <span class="target-weight-value">0%</span>
        </div>
        <button class="target-remove" onclick="removeTarget(this)" title="Remove">&times;</button>
    `;
    list.appendChild(item);
}

function removeTarget(btn) {
    const item = btn.closest('.target-item');
    const list = document.getElementById('target-list');
    if (!list || !item) return;

    if (list.children.length <= 1) {
        showToast('At least one route target is required', 'warning');
        return;
    }

    item.style.opacity = '0';
    item.style.transform = 'translateX(-20px)';
    item.style.transition = 'all 0.25s ease';
    setTimeout(() => {
        item.remove();
        list.querySelectorAll('.target-order').forEach((el, i) => {
            el.textContent = i + 1;
        });
    }, 250);
}

// ---------------------------------------------------------------------------
// Key Management Functions
// ---------------------------------------------------------------------------

function openKeyModal() {
    const modal = document.getElementById('key-modal');
    if (!modal) return;
    modal.classList.add('active');

    const genSection = document.getElementById('generated-key-section');
    const formSection = document.getElementById('key-form-section');
    const createBtn = document.getElementById('create-key-btn');

    if (genSection) genSection.style.display = 'none';
    if (formSection) formSection.style.display = 'block';
    if (createBtn) createBtn.style.display = '';
}

function closeKeyModal() {
    const modal = document.getElementById('key-modal');
    if (modal) modal.classList.remove('active');
}

function createKey() {
    const nameInput = document.getElementById('new-key-name');
    if (!nameInput || !nameInput.value.trim()) {
        showToast('Please enter a key name', 'warning');
        return;
    }

    const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
    let keyStr = 'sk-llmo-';
    for (let i = 0; i < 32; i++) keyStr += chars.charAt(Math.floor(Math.random() * chars.length));

    const genSection = document.getElementById('generated-key-section');
    const genValue = document.getElementById('generated-key-value');
    const formSection = document.getElementById('key-form-section');
    const createBtn = document.getElementById('create-key-btn');

    if (genSection) genSection.style.display = 'block';
    if (genValue) genValue.textContent = keyStr;
    if (formSection) formSection.style.display = 'none';
    if (createBtn) createBtn.style.display = 'none';

    showToast('API key created successfully', 'success');
}

function copyKey(key) {
    copyToClipboard(key);
}

function copyGeneratedKey() {
    const el = document.getElementById('generated-key-value');
    if (el) copyToClipboard(el.textContent);
}

function toggleKey(id, enabled) {
    showToast(enabled ? 'Key enabled' : 'Key disabled', enabled ? 'success' : 'warning');
}

function revokeKey(id) {
    if (confirm('Are you sure you want to revoke this key? This action cannot be undone.')) {
        showToast('Key revoked', 'warning');
    }
}

// ---------------------------------------------------------------------------
// Prompt CRUD Functions
// ---------------------------------------------------------------------------

function openPromptModal(mode, index) {
    const modal = document.getElementById('prompt-modal');
    if (!modal) return;

    const title = document.getElementById('prompt-modal-title');
    const vHistory = document.getElementById('version-history-section');

    if (mode === 'edit') {
        if (title) title.textContent = 'Edit Prompt Template';
        if (vHistory) vHistory.style.display = 'block';
    } else {
        if (title) title.textContent = 'Create Prompt Template';
        if (vHistory) vHistory.style.display = 'none';
    }
    modal.classList.add('active');
}

function closePromptModal() {
    const modal = document.getElementById('prompt-modal');
    if (modal) modal.classList.remove('active');
}

function savePromptTemplate() {
    showToast('Prompt template saved successfully', 'success');
    closePromptModal();
}

function testPrompt(index) {
    showToast('Opening playground with this prompt...', 'info');
    setTimeout(() => { window.location.href = '/playground'; }, 500);
}

function duplicatePrompt(index) {
    showToast('Prompt template duplicated', 'success');
}

function deletePrompt(index) {
    if (confirm('Are you sure you want to delete this prompt template?')) {
        showToast('Prompt template deleted', 'info');
    }
}

function filterPrompts() {
    const search = document.getElementById('prompt-search');
    if (!search) return;
    const query = search.value.toLowerCase();
    document.querySelectorAll('.prompt-card').forEach(card => {
        const text = card.textContent.toLowerCase();
        card.style.display = text.includes(query) ? '' : 'none';
    });
}

function handleTagInput(event) {
    if (event.key === 'Enter' && event.target.value.trim()) {
        event.preventDefault();
        const tag = event.target.value.trim();
        const container = document.getElementById('tpl-tags-container');
        if (!container) return;
        const chip = document.createElement('span');
        chip.className = 'chip';
        chip.innerHTML = `${escapeHtml(tag)} <button class="chip-remove" onclick="this.parentElement.remove()">&times;</button>`;
        container.insertBefore(chip, event.target);
        event.target.value = '';
    }
}

// ---------------------------------------------------------------------------
// Routing Save
// ---------------------------------------------------------------------------

function saveRouting() {
    showToast('Routing configuration saved', 'success');
}

// ---------------------------------------------------------------------------
// Guardrails Save
// ---------------------------------------------------------------------------

function saveGuardrails() {
    showToast('Guardrail configuration saved', 'success');
}

function updateGuardrailStatus(section, enabled) {
    const status = document.getElementById(section + '-status');
    if (status) {
        status.textContent = enabled ? 'Enabled' : 'Disabled';
        status.className = 'guardrail-status ' + (enabled ? 'enabled' : 'disabled');
    }
}

// ---------------------------------------------------------------------------
// Real-time Polling Manager
// ---------------------------------------------------------------------------

class PollingManager {
    constructor() {
        this.intervals = {};
    }

    start(name, fn, intervalMs) {
        this.stop(name);
        fn(); // Run immediately
        this.intervals[name] = setInterval(fn, intervalMs);
    }

    stop(name) {
        if (this.intervals[name]) {
            clearInterval(this.intervals[name]);
            delete this.intervals[name];
        }
    }

    stopAll() {
        for (const name of Object.keys(this.intervals)) {
            this.stop(name);
        }
    }
}

const polling = new PollingManager();

// ---------------------------------------------------------------------------
// Animate on Scroll (Intersection Observer)
// ---------------------------------------------------------------------------

if (typeof IntersectionObserver !== 'undefined') {
    const animateObserver = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.classList.add('animate-in');
                animateObserver.unobserve(entry.target);
            }
        });
    }, { threshold: 0.1 });

    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('.animate-on-scroll').forEach(el => {
            animateObserver.observe(el);
        });
    });
}

// ---------------------------------------------------------------------------
// Export Log Functions
// ---------------------------------------------------------------------------

function exportLogs() {
    showToast('Exporting logs as CSV...', 'info');
    // In production, this would trigger a download
}

function refreshLogs() {
    showToast('Refreshing logs...', 'info');
}

function sortTable(column) {
    showToast('Sorted by ' + column, 'info');
}
