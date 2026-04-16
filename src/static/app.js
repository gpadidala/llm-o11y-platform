/* =====================================================================
   LLM O11y Platform - Frontend JavaScript
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
// Password Toggle
// ---------------------------------------------------------------------------

function togglePassword(btn) {
    const input = btn.parentElement.querySelector('input');
    if (!input) return;

    if (input.type === 'password') {
        input.type = 'text';
        btn.textContent = '\uD83D\uDE48';  // see-no-evil monkey
    } else {
        input.type = 'password';
        btn.textContent = '\uD83D\uDC41';  // eye
    }
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
        // Mark all as unknown on fetch failure
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
// Settings - Load
// ---------------------------------------------------------------------------

async function loadSettings() {
    try {
        const resp = await fetch('/api/settings');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const settings = data.settings || {};

        // Populate form fields
        const form = document.getElementById('settings-form');
        if (!form) return;

        for (const [key, value] of Object.entries(settings)) {
            const input = form.querySelector(`[name="${key}"]`);
            if (input && value !== null && value !== undefined) {
                if (input.type === 'checkbox') {
                    input.checked = Boolean(value);
                } else {
                    // Don't overwrite password placeholders with redacted values
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
                addMcpServer();  // Add one empty entry
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
        // Add default empty MCP server entry
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

    // Gather form data
    const data = {};
    const inputs = form.querySelectorAll('input[name], select[name]');
    for (const input of inputs) {
        if (input.name.startsWith('mcp_')) continue;  // handled separately
        if (input.type === 'password' && input.value === '') continue;  // skip empty passwords
        if (input.type === 'checkbox') {
            data[input.name] = input.checked;
        } else {
            data[input.name] = input.value;
        }
    }

    // Gather MCP servers
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
// Utilities
// ---------------------------------------------------------------------------

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
