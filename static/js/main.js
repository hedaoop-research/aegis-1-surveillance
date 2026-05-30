const socket = io();

// UI Elements
const totalCountEl = document.getElementById('total-count');
const detectionListEl = document.getElementById('detection-list');
const logsEl = document.getElementById('logs');
const fpsEl = document.getElementById('fps');
const latencyEl = document.getElementById('latency');
const clockEl = document.getElementById('live-clock');
const urlInput = document.getElementById('stream-url');
const connectBtn = document.getElementById('connect-btn');

// Update Stream URL
connectBtn.addEventListener('click', async () => {
    const newUrl = urlInput.value;
    if (!newUrl) return;

    connectBtn.innerText = 'CONNECTING...';
    try {
        const response = await fetch('/update_url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: newUrl })
        });
        const result = await response.json();
        if (result.status === 'success') {
            addLog(`STREAM UPDATED: ${newUrl}`);
        } else {
            addLog(`ERROR: ${result.message}`);
        }
    } catch (err) {
        addLog(`CONNECTION ERROR: ${err.message}`);
    } finally {
        connectBtn.innerText = 'CONNECT';
    }
});

// State
let lastUpdate = Date.now();

// Update Clock
setInterval(() => {
    const now = new Date();
    clockEl.innerText = now.toLocaleTimeString();
}, 1000);

// Fetch stats every 500ms
async function fetchStats() {
    try {
        const start = Date.now();
        const response = await fetch('/stats');
        const data = await response.json();
        const end = Date.now();
        
        // Update Latency
        latencyEl.innerText = `${end - start} ms`;
        
        // Update FPS
        fpsEl.innerText = data.fps.toFixed(1);
        
        // Update Count
        totalCountEl.innerText = data.count;
        
        // Update Detection List
        updateDetectionList(data.detections);
        
        // Add log if something new detected
        if (data.detections.length > 0) {
            addLog(`DETECTED: ${data.detections.map(d => d.label).join(', ')}`);
        }
    } catch (err) {
        console.error("Error fetching stats:", err);
    }
}

function updateDetectionList(detections) {
    detectionListEl.innerHTML = '';
    detections.forEach(det => {
        const div = document.createElement('div');
        div.className = 'detection-item';
        div.innerHTML = `
            <span class="name">${det.label}</span>
            <span class="conf">${(det.confidence * 100).toFixed(1)}% [${det.source}]</span>
        `;
        detectionListEl.appendChild(div);
    });
}

function addLog(msg) {
    const entry = document.createElement('div');
    entry.className = 'log-entry new';
    const time = new Date().toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    entry.innerText = `[${time}] ${msg}`;
    logsEl.prepend(entry);
    
    // Limit logs
    if (logsEl.children.length > 20) {
        logsEl.removeChild(logsEl.lastChild);
    }
}

// Start polling
setInterval(fetchStats, 500);

// Socket Listeners (for future expandability)
socket.on('connect', () => {
    addLog('CONNECTION ESTABLISHED WITH SERVER');
});

socket.on('disconnect', () => {
    addLog('WARNING: SERVER DISCONNECTED');
});
