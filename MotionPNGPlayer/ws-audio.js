/**
 * WebSocket Client for MotionPNGPlayer
 *
 * Connects to the AG server, receives server commands and TTS messages,
 * and drives lip-sync animation from pre-computed lipsync_frames.
 *
 * Mode differences:
 * - local:  connects to ws://localhost:<port>
 * - remote: connects to wss://<host>:<port>/ws
 *
 * The player NEVER outputs audio in either mode — the browser UI client is
 * the single audio player (production AG: Gradio UI / rewind: replay UI),
 * mirroring the legacy player where the analyzer consumed the signal and
 * nothing reached the destination. Playing here too doubles the TTS audio.
 * Frames animate on receive; both playbacks share the same tts_audio
 * broadcast as their trigger, so drift stays within tens of ms.
 */
class WebSocketAudioPlayer {
    /**
     * @param {Object} config
     * @param {string} config.mode - 'local' or 'remote'
     * @param {number} config.wsPort - WebSocket port
     * @param {string} config.wsHost - WebSocket host
     * @param {LipsyncEngine} config.lipsyncEngine - LipsyncEngine instance
     * @param {Function} config.onCommand - Callback for server commands
     * @param {Function} config.onEvent - Callback for server events surfaced
     *   to the renderer UI (tts_audio / is_generating_update / text_prompt_response)
     */
    constructor(config) {
        this.mode = config.mode || 'remote';
        this.wsPort = config.wsPort || 8765;
        this.wsHost = config.wsHost || 'localhost';
        this.lipsyncEngine = config.lipsyncEngine || null;
        this.onCommand = config.onCommand || null;
        this.onEvent = config.onEvent || null;

        this.ws = null;

        this.reconnectAttempts = 0;
        this.reconnectDelay = 3000;
        this.maxReconnectDelay = 30000;
        this.reconnectTimer = null;

        // Lipsync frame animation
        this._animationId = null;
        this.lipsyncEnabled = false;

        // Bind methods
        this._onMessage = this._onMessage.bind(this);
        this._onClose = this._onClose.bind(this);
        this._onError = this._onError.bind(this);
    }

    /**
     * Connect to WebSocket server
     */
    connect() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            console.log('[WS-Audio] Already connected');
            return;
        }

        // Keep both URL forms exactly as the AG server expects them:
        // local plain-WS server has no path; remote WSS endpoint is /ws.
        const url = this.mode === 'local'
            ? `ws://localhost:${this.wsPort}`
            : `wss://${this.wsHost}:${this.wsPort}/ws`;
        console.log('[WS-Audio] Connecting to:', url);

        try {
            this.ws = new WebSocket(url);

            this.ws.onopen = () => {
                console.log('[WS-Audio] Connected');
                this.reconnectAttempts = 0;
                // Send identify message
                this.ws.send(JSON.stringify({type: 'identify', client_type: 'motion_pngtuber'}));
                console.log('[WS-Audio] Identify sent: motion_pngtuber');

                // Notify connection status
                if (window.electronAPI && window.electronAPI.notifyConnectionStatus) {
                    window.electronAPI.notifyConnectionStatus('Connected');
                }
            };

            this.ws.onmessage = this._onMessage;
            this.ws.onclose = this._onClose;
            this.ws.onerror = this._onError;

        } catch (error) {
            console.error('[WS-Audio] Connection error:', error);
            this._scheduleReconnect();
        }
    }

    /**
     * Disconnect from WebSocket server
     */
    disconnect() {
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }

        if (this.ws) {
            this.ws.onclose = null;  // Prevent reconnect
            this.ws.close();
            this.ws = null;
        }

        this._stopLipsyncAnimation();
    }

    /**
     * Send a message through the WebSocket
     * @param {Object} message - Message to send as JSON
     */
    sendMessage(message) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(message));
        }
    }

    /**
     * Handle WebSocket message
     */
    async _onMessage(event) {
        try {
            const data = JSON.parse(event.data);

            // Handle TTS audio message.
            // Payload is identical in both modes (audio + lipsync_frames);
            // audio_base64 is ignored — frames only (see header comment).
            if (data.action === 'tts_audio' && data.data) {
                if (data.data.lipsync_frames && this.lipsyncEnabled) {
                    const durationMs = data.data.duration_ms || 0;
                    console.log('[WS-Audio] Received lipsync frames, duration:', durationMs, 'ms');
                    this._playLipsyncFrames(data.data.lipsync_frames);
                }
            }

            // Handle connected message
            if (data.type === 'connected') {
                console.log('[WS-Audio] Server confirmed connection');
            }

            // Handle server commands
            if (data.action === 'character_appear' ||
                data.action === 'character_disappear' ||
                data.action === 'character_changed') {
                if (this.onCommand) {
                    this.onCommand(data);
                }
            }

            // Surface UI events to the renderer (speech bubble / prompt input)
            if (this.onEvent &&
                (data.action === 'tts_audio' ||
                 data.action === 'is_generating_update' ||
                 data.action === 'text_prompt_response')) {
                try {
                    this.onEvent(data);
                } catch (error) {
                    console.error('[WS-Audio] onEvent error:', error);
                }
            }

        } catch (error) {
            console.error('[WS-Audio] Error processing message:', error);
        }
    }

    /**
     * Handle WebSocket close
     */
    _onClose(event) {
        console.log('[WS-Audio] Connection closed, code:', event.code);
        this.ws = null;

        // Notify connection status
        if (window.electronAPI && window.electronAPI.notifyConnectionStatus) {
            window.electronAPI.notifyConnectionStatus('Disconnected');
        }

        this._scheduleReconnect();
    }

    /**
     * Handle WebSocket error
     */
    _onError(error) {
        console.error('[WS-Audio] WebSocket error:', error);
    }

    /**
     * Schedule reconnection attempt (exponential backoff, no upper limit on attempts)
     */
    _scheduleReconnect() {
        this.reconnectAttempts++;
        const delay = Math.min(
            this.maxReconnectDelay,
            this.reconnectDelay * Math.pow(1.5, Math.min(this.reconnectAttempts - 1, 10))
        );

        console.log(`[WS-Audio] Reconnecting in ${Math.round(delay)}ms (attempt ${this.reconnectAttempts})`);

        this.reconnectTimer = setTimeout(() => {
            this.connect();
        }, delay);
    }

    /**
     * Play pre-computed lipsync frames
     * Each frame: { t: timeMs, rms: float, high: float, low: float }
     * @param {Array} frames - Array of lipsync frame objects
     */
    _playLipsyncFrames(frames) {
        if (!frames || frames.length === 0) return;
        if (!this.lipsyncEnabled) return;

        // Stop any existing animation
        this._stopLipsyncAnimation();

        const startTime = performance.now();
        let frameIndex = 0;

        const animate = () => {
            if (!this.lipsyncEnabled) {
                this._animationId = null;
                return;
            }

            const elapsed = performance.now() - startTime;
            while (frameIndex < frames.length && frames[frameIndex].t <= elapsed) {
                if (this.lipsyncEngine) {
                    this.lipsyncEngine.processAudioData(frames[frameIndex]);
                }
                frameIndex++;
            }
            if (frameIndex < frames.length) {
                this._animationId = requestAnimationFrame(animate);
            } else {
                // Animation complete → close mouth
                if (this.lipsyncEngine) {
                    this.lipsyncEngine.processAudioData({ rms: 0, high: 0, low: 0 });
                }
                this._animationId = null;
            }
        };
        this._animationId = requestAnimationFrame(animate);
    }

    /**
     * Stop current lipsync frame animation
     */
    _stopLipsyncAnimation() {
        if (this._animationId) {
            cancelAnimationFrame(this._animationId);
            this._animationId = null;
        }
        // Close the mouth so an interrupted animation doesn't freeze mid-shape
        if (this.lipsyncEngine) {
            this.lipsyncEngine.processAudioData({ rms: 0, high: 0, low: 0 });
        }
    }

    /**
     * Check if connected
     */
    isConnected() {
        return this.ws && this.ws.readyState === WebSocket.OPEN;
    }
}
