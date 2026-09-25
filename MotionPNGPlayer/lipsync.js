/**
 * MotionPNG Tuber - Browser Lipsync Engine (DOM overlay)
 */
class LipsyncEngine {
    /**
     * @param {Object} config - 設定オブジェクト
     * @param {Object} config.elements - 必須DOM要素
     * @param {HTMLVideoElement} config.elements.video - 動画要素
     * @param {HTMLCanvasElement} config.elements.mouthCanvas - 口スプライト描画用Canvas
     * @param {HTMLElement} config.elements.stage - ステージ要素（リサイズ監視用）
     * @param {Object} config.callbacks - UI更新用コールバック（オプション）
     * @param {Function} config.callbacks.onLog - ログ出力時
     * @param {Function} config.callbacks.onFileStatus - ファイル状態変更時 (status: 'success'|'error', message: string)
     * @param {Function} config.callbacks.onVolumeChange - 音量変化時 (volume: number 0-1)
     * @param {Function} config.callbacks.onPlayStateChange - 再生状態変更時 (isPlaying: boolean)
     * @param {Function} config.callbacks.onSectionsVisibility - セクション表示変更時 (visible: boolean)
     * @param {Function} config.callbacks.onError - エラー発生時 (message: string)
     * @param {Object} config.assets - HTTPパスでアセットを指定（オプション）
     * @param {string} config.assets.video - 動画のHTTPパス
     * @param {string} config.assets.track - mouth_track.jsonのHTTPパス
     * @param {string} config.assets.mouth_closed - closed.pngのHTTPパス（必須）
     * @param {string} config.assets.mouth_open - open.pngのHTTPパス（必須）
     * @param {string} config.assets.mouth_half - half.pngのHTTPパス（オプション）
     * @param {string} config.assets.mouth_e - e.pngのHTTPパス（オプション）
     * @param {string} config.assets.mouth_u - u.pngのHTTPパス（オプション）
     * @param {Object} config.options - オプション設定（オプション）
     * @param {boolean} config.options.debug - デバッグログを出力するか（デフォルト: false）
     * @param {boolean} config.options.hqAudioEnabled - HQ Audioモードを有効にするか（デフォルト: false）
     * @param {number} config.options.sensitivity - 感度 0-100（デフォルト: 50）
     */
    constructor({ elements, callbacks = {}, assets = null, options = {} }) {
        // 必須DOM要素
        this.video = elements.video;
        this.mouthCanvas = elements.mouthCanvas;
        this.mouthCtx = this.mouthCanvas.getContext('2d');
        this.stage = elements.stage;

        // 任意: 動画転写先キャンバス。指定時は <video> を画面に出さず、毎フレーム
        // ここへ転写して表示する (Windowsの透過ウィンドウでは video 要素の合成
        // サーフェスが hide/show・最小化復元等あらゆる状態遷移で壊れるため、
        // 全ての故障モードを生き残る canvas 経路に表示を一本化する)
        this.videoCanvas = elements.videoCanvas || null;
        this.videoCanvasCtx = this.videoCanvas ? this.videoCanvas.getContext('2d') : null;

        // コールバック
        this.callbacks = callbacks;

        // オプション設定
        const {
            debug = true,
            hqAudioEnabled = false,
            sensitivity = 50
        } = options;
        this.debug = debug;

        // データ
        this.trackData = null;
        this.mouthSprites = {};
        this.mouthSpriteUrls = {};
        this.activeSprite = null;
        this.videoUrl = null;
        this.isRunning = false;

        // 音量解析用（AudioCaptureから受け取ったデータを処理）
        this.volume = 0;
        this.smoothedHighRatio = 0;
        this.sensitivity = sensitivity;
        this.hqAudioEnabled = hqAudioEnabled;
        this.envelope = 0;
        this.noiseFloor = 0.002;
        this.levelPeak = 0.02;
        this.mouthChangeMinMs = hqAudioEnabled ? 45 : 70;

        // 口状態
        this.mouthState = 'closed';
        this.lastMouthChange = 0;
        this.lastFrameIndex = null;
        this.resizeObserver = null;

        // ループ用
        this.animationId = null;
        this.statusInterval = null;
        this._renderGeneration = 0;

        this.init();

        // assetsが指定されていたらHTTPから読み込み
        if (assets) {
            this._loadFromAssets(assets);
        }
    }

    init() {
        window.addEventListener('resize', () => this.handleResize());
        window.addEventListener('beforeunload', () => this.cleanup());
        if ('ResizeObserver' in window) {
            this.resizeObserver = new ResizeObserver(() => this.handleResize());
            this.resizeObserver.observe(this.stage);
            this.resizeObserver.observe(this.video);
        }
    }

    // 外部から呼び出すAPI
    setSensitivity(value) {
        this.sensitivity = value;
    }

    setHQAudioEnabled(enabled) {
        this.hqAudioEnabled = enabled;
        this.mouthChangeMinMs = enabled ? 45 : 70;
        this.resetAudioStats();
        this.log(enabled ? 'HQ Audio: ON' : 'HQ Audio: OFF');
    }

    log(msg) {
        if (this.debug) {
            console.log(msg);
            this.callbacks.onLog?.(msg);
        }
    }

    async loadFiles(files) {
        this.log('ファイル数: ' + files.length);

        let videoFile = null;
        let trackFile = null;
        const spriteFiles = {};

        for (const file of files) {
            const name = file.name.toLowerCase();
            const path = file.webkitRelativePath.toLowerCase().replace(/\\/g, '/');

            if (name.includes('mouthless') && (name.endsWith('.mp4') || name.endsWith('.webm'))) {
                // Prefer h264 mp4, then any mp4, then webm
                if (name.includes('h264') || !videoFile) {
                    videoFile = file;
                    this.log('動画発見: ' + file.name);
                }
            }
            if (name === 'mouth_track.json') {
                trackFile = file;
            }
            if (path.includes('mouth/') || path.includes('mouth\\')) {
                if (name === 'closed.png') spriteFiles.closed = file;
                if (name === 'open.png') spriteFiles.open = file;
                if (name === 'half.png') spriteFiles.half = file;
                if (name === 'e.png') spriteFiles.e = file;
                if (name === 'u.png') spriteFiles.u = file;
            }
        }

        const missing = [];
        if (!videoFile) missing.push('*_mouthless.mp4 or .webm');
        if (!trackFile) missing.push('mouth_track.json');
        if (!spriteFiles.closed) missing.push('mouth/closed.png');
        if (!spriteFiles.open) missing.push('mouth/open.png');

        if (missing.length > 0) {
            this.callbacks.onFileStatus?.('error', `Missing: ${missing.join(', ')}`);
            return;
        }

        try {
            this.cleanup();

            // 動画読み込み（File → ObjectURL）
            const videoSrc = URL.createObjectURL(videoFile);
            await this._setupVideo(videoSrc, true);

            // トラックデータ読み込み
            const trackText = await trackFile.text();
            await this._setupTrackData(JSON.parse(trackText));

            // スプライト読み込み（File → ObjectURL）
            const spriteSources = {};
            for (const [key, file] of Object.entries(spriteFiles)) {
                spriteSources[key] = URL.createObjectURL(file);
            }
            await this._loadMouthSprites(spriteSources);

            this._onLoadComplete();
        } catch (err) {
            this.callbacks.onFileStatus?.('error', `Load error: ${err.message}`);
            console.error(err);
        }
    }

    async _loadFromAssets(assets) {
        const missing = [];
        if (!assets.video) missing.push('video');
        if (!assets.track) missing.push('track');
        if (!assets.mouth_closed) missing.push('mouth_closed');
        if (!assets.mouth_open) missing.push('mouth_open');

        if (missing.length > 0) {
            this.callbacks.onFileStatus?.('error', `Missing assets: ${missing.join(', ')}`);
            return;
        }

        try {
            this.cleanup();

            // 動画読み込み（HTTPパス）
            await this._setupVideo(assets.video, false);

            // トラックデータ読み込み（HTTP fetch）
            this.log('トラックデータ読み込み中...');
            const response = await fetch(assets.track);
            if (!response.ok) throw new Error(`track fetch failed: ${response.status}`);
            const trackData = await response.json();
            await this._setupTrackData(trackData);

            // スプライト読み込み（HTTPパス）
            const spriteSources = {
                closed: assets.mouth_closed,
                open: assets.mouth_open
            };
            if (assets.mouth_half) spriteSources.half = assets.mouth_half;
            if (assets.mouth_e) spriteSources.e = assets.mouth_e;
            if (assets.mouth_u) spriteSources.u = assets.mouth_u;
            await this._loadMouthSprites(spriteSources);

            this._onLoadComplete(true);  // assets読み込み時は自動開始
        } catch (err) {
            this.callbacks.onFileStatus?.('error', `Load error: ${err.message}`);
            console.error(err);
        }
    }

    async _setupVideo(src, isObjectUrl) {
        this.log('動画読み込み中...');

        if (isObjectUrl) {
            this.videoUrl = src;
        }
        this.video.src = src;
        this.video.loop = true;
        this.video.muted = true;
        this.video.playsInline = true;
        this.video.preload = 'auto';
        this.video.controls = false;

        await new Promise((resolve, reject) => {
            const onReady = () => {
                this.log(
                    '動画準備完了: ' +
                        this.video.videoWidth +
                        'x' +
                        this.video.videoHeight
                );
                this.video.removeEventListener('canplaythrough', onReady);
                this.video.removeEventListener('loadeddata', onReady);
                resolve();
            };
            this.video.addEventListener('canplaythrough', onReady);
            this.video.addEventListener('loadeddata', onReady);
            this.video.onerror = (e) => {
                this.log('動画エラー: ' + (e.message || 'unknown'));
                reject(new Error('Video load failed'));
            };
            this.video.load();
        });

        this.mouthCanvas.width = this.video.videoWidth || 1;
        this.mouthCanvas.height = this.video.videoHeight || 1;
        if (this.mouthCtx) {
            this.mouthCtx.setTransform(1, 0, 0, 1, 0, 0);
            this.mouthCtx.imageSmoothingEnabled = true;
            this.mouthCtx.clearRect(0, 0, this.mouthCanvas.width, this.mouthCanvas.height);
        }
    }

    async _setupTrackData(trackData) {
        this.trackData = trackData;
        this.log('トラッキング: ' + this.trackData.frames.length + 'フレーム');
    }

    async _loadMouthSprites(sources) {
        this.mouthSprites = {};
        this.mouthSpriteUrls = {};

        for (const [key, src] of Object.entries(sources)) {
            const img = await this._loadImageFromSrc(src);
            this.mouthSprites[key] = img;
            this.mouthSpriteUrls[key] = src;
        }
    }

    _loadImageFromSrc(src) {
        return new Promise((resolve, reject) => {
            const img = new Image();
            img.onload = () => resolve(img);
            img.onerror = () => reject(new Error(`Image load failed: ${src}`));
            img.src = src;
        });
    }

    _onLoadComplete(autoStart = false) {
        const statusMsg = `Load complete: ${this.trackData.frames.length} frames, ${this.trackData.fps}fps (${this.video.videoWidth}x${this.video.videoHeight})`;
        this.callbacks.onFileStatus?.('success', statusMsg);
        this.callbacks.onSectionsVisibility?.(true);

        this.setMouthState('closed', true);

        if (autoStart) {
            this.start();
        } else {
            this.renderPreview();
        }
    }

    loadImage(file) {
        return new Promise((resolve, reject) => {
            const img = new Image();
            img.onload = () => resolve(img);
            img.onerror = reject;
            img.src = URL.createObjectURL(file);
        });
    }

    cleanup() {
        this.stop();
        if (this.videoUrl) {
            URL.revokeObjectURL(this.videoUrl);
            this.videoUrl = null;
        }
        for (const url of Object.values(this.mouthSpriteUrls)) {
            if (url) URL.revokeObjectURL(url);
        }
        this.mouthSpriteUrls = {};
        this.mouthSprites = {};
        this.activeSprite = null;
        if (this.video) {
            this.video.removeAttribute('src');
            this.video.load();
        }
        if (this.mouthCtx && this.mouthCanvas) {
            this.mouthCtx.setTransform(1, 0, 0, 1, 0, 0);
            this.mouthCtx.clearRect(0, 0, this.mouthCanvas.width, this.mouthCanvas.height);
        }
    }

    // AudioCaptureからデータを受け取るメソッド
    processAudioData(data) {
        if (!data) return;

        if (this.hqAudioEnabled) {
            this.processAudioDataHQ(data);
            return;
        }

        const smoothing = 0.2;
        const ratio = data.high / (data.low + data.high + 1e-6);
        this.volume = this.volume * (1 - smoothing) + data.rms * smoothing;
        this.smoothedHighRatio =
            this.smoothedHighRatio * (1 - smoothing) + ratio * smoothing;

        const thresholds = this.getVolumeThresholds();
        const meter = Math.min(1, this.volume / (thresholds.half * 1.8));
        this.callbacks.onVolumeChange?.(meter);

        const nextState = this.selectMouthState(
            this.volume,
            this.smoothedHighRatio,
            thresholds
        );
        this.setMouthState(nextState);
    }

    resetAudioStats() {
        this.volume = 0;
        this.envelope = 0;
        this.noiseFloor = 0.002;
        this.levelPeak = 0.02;
        this.smoothedHighRatio = 0;
        this.callbacks.onVolumeChange?.(0);
    }

    processAudioDataHQ(data) {
        const ratio = data.high / (data.low + data.high + 1e-6);
        const ratioSmoothing = 0.25;
        this.smoothedHighRatio =
            this.smoothedHighRatio * (1 - ratioSmoothing) +
            ratio * ratioSmoothing;

        const rms = data.rms;
        const sensitivity = this.sensitivity / 100;
        const attack = 0.35;
        const release = 0.6;
        const k = rms > this.envelope ? attack : release;
        this.envelope = this.envelope * (1 - k) + rms * k;

        if (!this.noiseFloor) {
            this.noiseFloor = this.envelope;
        }
        if (this.envelope < this.noiseFloor) {
            const fall = 0.25;
            this.noiseFloor = this.noiseFloor * (1 - fall) + this.envelope * fall;
        } else {
            const rise = 0.01;
            this.noiseFloor = this.noiseFloor * (1 - rise) + this.envelope * rise;
        }

        const peakDecay = 0.985;
        this.levelPeak = Math.max(this.envelope, this.levelPeak * peakDecay);
        const minRange = 0.006;
        if (this.levelPeak < this.noiseFloor + minRange) {
            this.levelPeak = this.noiseFloor + minRange;
        }

        const gateMargin = 0.002 + (1 - sensitivity) * 0.008;
        const gateLevel = this.noiseFloor + gateMargin;
        if (this.envelope < gateLevel) {
            this.volume = 0;
            this.callbacks.onVolumeChange?.(0);
            this.setMouthState('closed');
            return;
        }

        const rawLevel =
            (this.envelope - this.noiseFloor) / (this.levelPeak - this.noiseFloor);
        const level = Math.max(0, Math.min(1, rawLevel));
        const gain = 0.6 + sensitivity * 0.8;
        const shaped = Math.min(1, Math.pow(level, 0.75) * gain);

        this.volume = shaped;
        this.callbacks.onVolumeChange?.(shaped);

        const thresholds = this.getVolumeThresholdsHQ();
        const nextState = this.selectMouthStateHQ(
            shaped,
            this.smoothedHighRatio,
            thresholds
        );
        this.setMouthState(nextState);
    }

    getVolumeThresholds() {
        const sensitivity = this.sensitivity / 100;
        const closed = 0.008 + (1 - sensitivity) * 0.018;
        const half = 0.02 + (1 - sensitivity) * 0.06;
        return { closed, half };
    }

    getVolumeThresholdsHQ() {
        const sensitivity = this.sensitivity / 100;
        const closed = 0.07 + (1 - sensitivity) * 0.08;
        const half = 0.22 + (1 - sensitivity) * 0.12;
        return { closed, half };
    }

    selectMouthState(volume, highRatio, thresholds) {
        if (volume < thresholds.closed) return 'closed';
        if (volume < thresholds.half) return this.mouthSpriteUrls.half ? 'half' : 'open';

        if (highRatio > 0.62 && this.mouthSpriteUrls.e) return 'e';
        if (highRatio < 0.38 && this.mouthSpriteUrls.u) return 'u';
        return 'open';
    }

    selectMouthStateHQ(level, highRatio, thresholds) {
        const hasHalf = !!this.mouthSpriteUrls.half;
        const hasE = !!this.mouthSpriteUrls.e;
        const hasU = !!this.mouthSpriteUrls.u;

        const closeTh = Math.max(0.02, thresholds.closed - 0.03);
        const halfDownTh = Math.max(closeTh + 0.02, thresholds.half - 0.02);

        let state = this.mouthState;
        if (state === 'e' || state === 'u') {
            state = 'open';
        }

        if (state === 'closed') {
            if (level >= thresholds.half) {
                state = 'open';
            } else if (level >= thresholds.closed && hasHalf) {
                state = 'half';
            } else if (level >= thresholds.closed) {
                state = 'open';
            } else {
                state = 'closed';
            }
        } else if (state === 'half') {
            if (level < closeTh) {
                state = 'closed';
            } else if (level >= thresholds.half) {
                state = 'open';
            } else {
                state = 'half';
            }
        } else {
            if (level < closeTh) {
                state = 'closed';
            } else if (level < halfDownTh && hasHalf) {
                state = 'half';
            } else {
                state = 'open';
            }
        }

        if (state === 'open') {
            if (highRatio > 0.62 && hasE) return 'e';
            if (highRatio < 0.38 && hasU) return 'u';
        }
        return state;
    }

    setMouthState(state, force = false) {
        const sprite =
            this.mouthSprites[state] ||
            this.mouthSprites.closed ||
            this.mouthSprites.open;
        if (!sprite) return;

        const now = performance.now();
        if (
            !force &&
            state !== this.mouthState &&
            now - this.lastMouthChange < this.mouthChangeMinMs
        ) {
            return;
        }

        if (force || state !== this.mouthState) {
            this.mouthState = state;
            this.activeSprite = sprite;
            this.lastMouthChange = now;
        }
    }

    renderPreview() {
        if (!this.video || !this.trackData) return;
        this.video.pause();
        const render = () => {
            this.renderFrame();
            this.log('プレビュー描画完了');
        };
        const onSeeked = () => {
            clearTimeout(timer);
            render();
        };
        // seek完了を待って描画（seekedが来ない場合のタイムアウト保険つき）
        const timer = setTimeout(() => {
            this.video.removeEventListener('seeked', onSeeked);
            render();
        }, 500);
        this.video.addEventListener('seeked', onSeeked, { once: true });
        this.video.currentTime = 0;
    }

    start() {
        if (!this.video || !this.trackData) {
            this.callbacks.onError?.('Load data before playing');
            return;
        }
        this.isRunning = true;
        this.callbacks.onPlayStateChange?.(true);
        this.startRenderLoop();
    }

    stop() {
        this.isRunning = false;
        this.callbacks.onPlayStateChange?.(false);

        if (this.video) {
            this.video.pause();
        }
        if (this.animationId) {
            cancelAnimationFrame(this.animationId);
        }
        if (this.statusInterval) {
            clearInterval(this.statusInterval);
        }
    }

    startRenderLoop() {
        if (!this.isRunning) return;

        this._renderGeneration++;
        const gen = this._renderGeneration;

        // videoCanvas転写モードでは rAF 駆動にする: 非表示の video 要素は
        // requestVideoFrameCallback の発火が保証されないため
        if (this.video.requestVideoFrameCallback && !this.videoCanvas) {
            const onFrame = () => {
                if (!this.isRunning || gen !== this._renderGeneration) return;
                this.renderFrame();
                this.video.requestVideoFrameCallback(onFrame);
            };
            this.video.requestVideoFrameCallback(onFrame);
        } else {
            const loop = () => {
                if (!this.isRunning || gen !== this._renderGeneration) return;
                this.renderFrame();
                this.animationId = requestAnimationFrame(loop);
            };
            loop();
        }
    }

    renderFrame() {
        const video = this.video;
        const data = this.trackData;

        if (!video || video.readyState < 2 || !data) return;

        // 動画フレームを表示キャンバスへ転写 (videoCanvasモード時)
        if (this.videoCanvas && this.videoCanvasCtx) {
            if (this.videoCanvas.width !== video.videoWidth ||
                this.videoCanvas.height !== video.videoHeight) {
                this.videoCanvas.width = video.videoWidth || 1;
                this.videoCanvas.height = video.videoHeight || 1;
            }
            this.videoCanvasCtx.clearRect(0, 0, this.videoCanvas.width, this.videoCanvas.height);
            this.videoCanvasCtx.drawImage(video, 0, 0);
        }

        const totalFrames = data.frames.length;
        if (!totalFrames) return;

        const currentTime = video.currentTime;
        const fps = data.fps || 30;
        const frameIndex = Math.floor(currentTime * fps) % totalFrames;
        this.lastFrameIndex = frameIndex;
        this.updateMouthTransform(frameIndex);
    }

    handleResize() {
        if (!this.trackData || !this.video || this.video.readyState < 2) return;
        const totalFrames = this.trackData.frames.length;
        if (!totalFrames) return;

        const frameIndex =
            this.lastFrameIndex !== null
                ? this.lastFrameIndex
                : Math.floor(this.video.currentTime * (this.trackData.fps || 30)) % totalFrames;
        this.updateMouthTransform(frameIndex);
    }

    updateMouthTransform(frameIndex) {
        const data = this.trackData;
        if (!data || !data.frames || data.frames.length === 0) return;
        if (!this.mouthCtx || !this.mouthCanvas) return;

        const frame = data.frames[frameIndex];
        const ctx = this.mouthCtx;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, this.mouthCanvas.width, this.mouthCanvas.height);
        if (!frame || !frame.valid) return;

        const sprite =
            this.activeSprite ||
            this.mouthSprites.closed ||
            this.mouthSprites.open;
        if (!sprite) return;

        const quad = frame.quad;
        const adjustedQuad = this.applyCalibrationToQuad(quad, data);
        this.drawMouthSprite(sprite, adjustedQuad);
    }

    applyCalibrationToQuad(quad, data) {
        const calib = data.calibration || { offset: [0, 0], scale: 1, rotation: 0 };
        const applyCalib = data.calibrationApplied === true;
        if (!applyCalib) {
            return quad.map((pt) => [pt[0], pt[1]]);
        }

        const offsetX = calib.offset[0] || 0;
        const offsetY = calib.offset[1] || 0;
        const scale = calib.scale || 1;
        const rotation = ((calib.rotation || 0) * Math.PI) / 180;

        let cx = 0;
        let cy = 0;
        for (const [x, y] of quad) {
            cx += x;
            cy += y;
        }
        cx /= 4;
        cy /= 4;

        const cos = Math.cos(rotation);
        const sin = Math.sin(rotation);

        return quad.map(([x, y]) => {
            const dx = (x - cx) * scale;
            const dy = (y - cy) * scale;
            const rx = dx * cos - dy * sin + cx + offsetX;
            const ry = dx * sin + dy * cos + cy + offsetY;
            return [rx, ry];
        });
    }

    /**
     * 口 PNG を quad に描く。
     *
     * 位置データ (mouth_track.json / NPZ) の契約 — AG と MPC で同一 (2026-09-13 稜裁定):
     *   1. frames[i].quad は動画ネイティブ座標の 4 点。quad[0]→quad[1] が PNG の上辺
     *      (左→右)、quad[0]→quad[3] が左辺 (上→下) に対応する。
     *      quad[2] = quad[1] + quad[3] − quad[0] (平行四辺形。MPC は回転した長方形を書く)。
     *   2. 描画は PNG 全体 (透明部分を含むキャンバス) を quad に写す。口の大きさは quad が
     *      持ち、PNG の画素数では決まらない (原型 MotionPNGTuber と同じ規則)。
     *   3. refSpriteSize = [PNG の幅, PNG の高さ] (キャンバス)。open/closed/half は同一サイズ。
     *   4. trackFormat: 2。未指定は旧形式 (MPC 旧出力 = quad が元画像の口 bbox で PNG より
     *      小さい)。プレイヤーは版番号で挙動を変えない。旧形式は MPC の変換スクリプトで直す。
     *   5. calibration / calibrationApplied は現状維持 (applyCalibrationToQuad)。
     *   6. 角度・拡縮・基準値はデータに持たない。すべて quad に畳み込み済み
     *      (角度は「口 PNG を切り出した向きからの回転量」。静止時 0)。
     *
     * 左上・右上・左下の 3 頂点から求めた 1 つのアフィン変換で丸ごと描く。
     * 旧実装 (外接矩形に drawImage) は回転を捨てていた。原型の三角 2 枚 + clip() は
     * クリップ縁のアンチエイリアスで対角線上の画素が両方から半分ずつしか塗られず、
     * 口を横切る細い線 (「＼」) が出る。
     */
    drawMouthSprite(sprite, quad) {
        if (!this.mouthCtx) return;
        const sw = sprite.naturalWidth || sprite.width;
        const sh = sprite.naturalHeight || sprite.height;
        if (!sw || !sh) return;

        const mat = this.computeAffine(
            [0, 0], [sw, 0], [0, sh],
            quad[0], quad[1], quad[3],
        );
        if (!mat) return;

        const ctx = this.mouthCtx;
        ctx.save();
        ctx.setTransform(mat.a, mat.b, mat.c, mat.d, mat.e, mat.f);
        ctx.drawImage(sprite, 0, 0);
        ctx.restore();
    }

    computeAffine(s0, s1, s2, d0, d1, d2) {
        const sx0 = s0[0];
        const sy0 = s0[1];
        const sx1 = s1[0];
        const sy1 = s1[1];
        const sx2 = s2[0];
        const sy2 = s2[1];

        const dx0 = d0[0];
        const dy0 = d0[1];
        const dx1 = d1[0];
        const dy1 = d1[1];
        const dx2 = d2[0];
        const dy2 = d2[1];

        const denom = sx0 * (sy1 - sy2) + sx1 * (sy2 - sy0) + sx2 * (sy0 - sy1);
        if (denom === 0) return null;

        const a = (dx0 * (sy1 - sy2) + dx1 * (sy2 - sy0) + dx2 * (sy0 - sy1)) / denom;
        const b = (dy0 * (sy1 - sy2) + dy1 * (sy2 - sy0) + dy2 * (sy0 - sy1)) / denom;
        const c = (dx0 * (sx2 - sx1) + dx1 * (sx0 - sx2) + dx2 * (sx1 - sx0)) / denom;
        const d = (dy0 * (sx2 - sx1) + dy1 * (sx0 - sx2) + dy2 * (sx1 - sx0)) / denom;
        const e =
            (dx0 * (sx1 * sy2 - sx2 * sy1) +
                dx1 * (sx2 * sy0 - sx0 * sy2) +
                dx2 * (sx0 * sy1 - sx1 * sy0)) /
            denom;
        const f =
            (dy0 * (sx1 * sy2 - sx2 * sy1) +
                dy1 * (sx2 * sy0 - sx0 * sy2) +
                dy2 * (sx0 * sy1 - sx1 * sy0)) /
            denom;

        return { a, b, c, d, e, f };
    }
}
