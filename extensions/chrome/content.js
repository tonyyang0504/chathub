/**
 * ChatHub Chrome Extension - Content Script
 * Injects a sidebar into messaging web pages for AI assistance.
 */

// Inject sidebar when activated
function createSidebar() {
    if (document.getElementById('chathub-sidebar')) return;

    const sidebar = document.createElement('div');
    sidebar.id = 'chathub-sidebar';
    sidebar.innerHTML = `
        <div class="chathub-header">
            <span>ChatHub AI</span>
            <button onclick="document.getElementById('chathub-sidebar').remove()">✕</button>
        </div>
        <div class="chathub-messages" id="chathub-messages"></div>
        <div class="chathub-input">
            <input type="text" id="chathub-input" placeholder="Ask AI..." />
            <button onclick="sendMessage()">→</button>
        </div>
    `;
    document.body.appendChild(sidebar);
}

async function sendMessage() {
    const input = document.getElementById('chathub-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';

    const messages = document.getElementById('chathub-messages');
    messages.innerHTML += `<div class="msg user">${text}</div>`;

    // Get settings
    const settings = await chrome.storage.local.get(['serverUrl', 'apiToken']);
    const serverUrl = settings.serverUrl || 'http://localhost:8000';

    try {
        // TODO: Call ChatHub API for AI response
        messages.innerHTML += `<div class="msg bot">AI response coming soon...</div>`;
    } catch (e) {
        messages.innerHTML += `<div class="msg error">Error: ${e.message}</div>`;
    }

    messages.scrollTop = messages.scrollHeight;
}

// Listen for activation from popup
chrome.runtime.onMessage.addListener((msg) => {
    if (msg.action === 'toggleSidebar') createSidebar();
});

console.log('ChatHub extension loaded');
