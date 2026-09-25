/**
 * Multi-Motion Manager
 *
 * Manages multiple motion videos for a character,
 * handling random selection and seamless transitions
 * between motions using double-buffering technique.
 */
class MultiMotionManager {
    /**
     * @param {LipsyncEngine} lipsyncEngine - LipsyncEngine instance
     */
    constructor(lipsyncEngine) {
        this.lipsyncEngine = lipsyncEngine;

        // Motion data
        this.motions = [];
        this.currentIndex = -1;
        this.nextIndex = -1;

        // Video elements (double buffer for seamless transition)
        this.frontVideo = null;  // Currently displayed video
        this.backVideo = null;   // Preloading video

        // Track data cache
        this.trackDataCache = {};

        // Preload state
        this.nextMotionReady = false;
        this.preloadedSprites = {};
        this.preloadedSpriteUrls = {};

        // State
        this.isPlaying = false;
        this.isTransitioning = false;

        // Set by cleanup(): this manager was replaced and must never touch
        // the shared engine / video elements again
        this.disposed = false;
    }

    /**
     * True once this manager was stopped or replaced. Async continuations
     * (transition / initial load) check this after every await so that an
     * old manager cannot overwrite the state of the session that replaced it.
     */
    _isStale() {
        return this.disposed || !this.isPlaying;
    }

    /**
     * Initialize with motion data
     * @param {Array} motionDataList - List of motion data from main process
     */
    async init(motionDataList) {
        console.log('[MultiMotion] Initializing with', motionDataList.length, 'motions');

        // Initialize double buffer video elements.
        // Always take both from the DOM. The engine is reused across re-inits
        // (remote mode: re-Appear / character_changed in the same page) and
        // every transition swaps lipsyncEngine.video between the two elements,
        // so after an odd number of transitions engine.video is #back-video:
        // taking front from the engine made front and back the same element,
        // and the next preload overwrote the playing video (freeze + mouth
        // drawn with the wrong motion's track).
        this.frontVideo = document.getElementById('base-video');
        this.backVideo = document.getElementById('back-video');
        this.lipsyncEngine.video = this.frontVideo;

        // Reset what a previous manager left on the shared elements
        for (const v of [this.frontVideo, this.backVideo]) {
            v.onended = null;
            v.pause();
            v.style.zIndex = '';
            v.style.visibility = '';
        }

        // Configure back video element
        this.backVideo.muted = true;
        this.backVideo.playsInline = true;
        this.backVideo.preload = 'auto';
        this.backVideo.loop = false;

        // Load all motion data
        for (const motion of motionDataList) {
            try {
                // Read mouth_track.json
                const result = await window.electronAPI.readFile(motion.trackPath);
                if (result.error) {
                    console.error('[MultiMotion] Failed to read track data:', motion.name, result.error);
                    continue;
                }

                const trackData = JSON.parse(result.content);
                this.trackDataCache[motion.name] = trackData;

                // Build sprite paths
                const spritePaths = {};
                for (const sprite of motion.availableSprites) {
                    const spriteName = sprite.replace('.png', '');
                    spritePaths[spriteName] = this._buildFilePath(motion.mouthPath, sprite);
                }

                this.motions.push({
                    name: motion.name,
                    videoPath: this._buildFilePath(motion.videoPath),
                    trackData: trackData,
                    mouthPath: motion.mouthPath,
                    spritePaths: spritePaths
                });

                console.log('[MultiMotion] Loaded motion:', motion.name);

            } catch (error) {
                console.error('[MultiMotion] Error loading motion:', motion.name, error);
            }
        }

        if (this.motions.length === 0) {
            throw new Error('No valid motions loaded');
        }

        console.log('[MultiMotion] Total valid motions:', this.motions.length);

        // Load first motion (uses traditional method for initial load)
        await this._switchMotion(0);

        // Single motion: use video.loop, bypass transition machinery
        if (this.motions.length === 1) {
            this.frontVideo.loop = true;
            this.frontVideo.onended = null;
            console.log('[MultiMotion] Single motion: using video.loop');
        }
    }

    /**
     * Build file path with local:// protocol.
     * Always produce local:///<path> (empty authority): a Windows path like
     * C:/... placed right after local:// would be URL-parsed as a hostname
     * and lose its drive-letter colon.
     */
    _buildFilePath(filePath, fileName = null) {
        let fullPath = fileName ? `${filePath}/${fileName}` : filePath;
        // Normalize path separators (in case Windows paths are passed)
        fullPath = fullPath.replace(/\\/g, '/');
        return fullPath.startsWith('/') ? `local://${fullPath}` : `local:///${fullPath}`;
    }

    /**
     * Get current track data
     */
    getCurrentTrackData() {
        if (this.currentIndex >= 0 && this.currentIndex < this.motions.length) {
            return this.motions[this.currentIndex].trackData;
        }
        return null;
    }

    /**
     * Switch to a specific motion (used for initial load only)
     * @param {number} index - Motion index
     */
    async _switchMotion(index) {
        if (index < 0 || index >= this.motions.length) {
            console.error('[MultiMotion] Invalid motion index:', index);
            return;
        }

        if (this.disposed) return;

        const motion = this.motions[index];
        console.log('[MultiMotion] Switching to motion:', motion.name);

        this.currentIndex = index;
        this.isTransitioning = true;

        try {
            // Update video source
            this.frontVideo.src = motion.videoPath;
            this.frontVideo.loop = false;

            // Update LipsyncEngine track data
            this.lipsyncEngine.trackData = motion.trackData;

            // Load mouth sprites (non-fatal: continue even if loading fails)
            try {
                await this.lipsyncEngine._loadMouthSprites(motion.spritePaths);
                console.log('[MultiMotion] Sprites loaded:', Object.keys(this.lipsyncEngine.mouthSprites));
            } catch (spriteError) {
                console.error('[MultiMotion] WARNING: Sprite loading failed:', spriteError);
            }

            if (this.disposed) return;

            // Initialize mouth state to closed
            this.lipsyncEngine.setMouthState('closed', true);

            // Update canvas size
            if (motion.trackData.width && motion.trackData.height) {
                this.lipsyncEngine.mouthCanvas.width = motion.trackData.width;
                this.lipsyncEngine.mouthCanvas.height = motion.trackData.height;
            }

            // Set up video end handler for transition
            this.frontVideo.onended = () => {
                if (this.isPlaying) {
                    this._transitionToNextMotion();
                }
            };

            // Wait for the load started by the src assignment above.
            // Do NOT call load() here: it resets a possibly-complete load and
            // the subsequent seek+play() race the reload → media error (E43).
            await new Promise((resolve, reject) => {
                if (this.frontVideo.error) {
                    reject(new Error('Video load failed: ' + this.frontVideo.error.message));
                    return;
                }
                if (this.frontVideo.readyState >= 2) {
                    resolve();
                    return;
                }
                const onReady = () => {
                    this.frontVideo.removeEventListener('canplaythrough', onReady);
                    this.frontVideo.removeEventListener('loadeddata', onReady);
                    resolve();
                };
                const onError = (e) => {
                    reject(new Error('Video load failed: ' + (e.message || 'unknown')));
                };
                this.frontVideo.addEventListener('canplaythrough', onReady);
                this.frontVideo.addEventListener('loadeddata', onReady);
                this.frontVideo.onerror = onError;
            });

            if (this.disposed) return;

            // Preload next motion (only if multiple motions exist)
            if (this.motions.length > 1) {
                this._preloadNextMotion();
            }

            console.log('[MultiMotion] Motion switch complete:', motion.name);

        } catch (error) {
            console.error('[MultiMotion] Error switching motion:', error);
        } finally {
            this.isTransitioning = false;
        }
    }

    /**
     * Preload the next random motion into backVideo
     */
    _preloadNextMotion() {
        if (this.disposed) return;

        // Select next motion index
        if (this.motions.length <= 1) {
            this.nextIndex = 0;
        } else {
            // Select random motion (different from current)
            do {
                this.nextIndex = Math.floor(Math.random() * this.motions.length);
            } while (this.nextIndex === this.currentIndex);
        }

        const nextMotion = this.motions[this.nextIndex];
        this.nextMotionReady = false;

        console.log('[MultiMotion] Preloading next motion:', nextMotion.name);

        // Wait for video to be ready, then preload sprites
        // (listener attached BEFORE src so a fast/cached load cannot slip past)
        const onVideoReady = () => {
            this.backVideo.removeEventListener('canplaythrough', onVideoReady);
            if (this.disposed) return;

            // Preload mouth sprites
            this._preloadSprites(nextMotion.spritePaths).then(() => {
                this.nextMotionReady = true;
                console.log('[MultiMotion] Next motion fully preloaded:', nextMotion.name);
            }).catch(error => {
                console.error('[MultiMotion] Failed to preload sprites:', error);
                // Mark as ready anyway to prevent infinite waiting
                this.nextMotionReady = true;
            });
        };
        this.backVideo.addEventListener('canplaythrough', onVideoReady);

        // Preload video into backVideo (src assignment starts the load;
        // no explicit load() — it would reset the element and re-download)
        this.backVideo.src = nextMotion.videoPath;
    }

    /**
     * Preload mouth sprites for the next motion
     * @param {Object} spritePaths - Map of sprite name to path
     */
    async _preloadSprites(spritePaths) {
        this.preloadedSprites = {};
        this.preloadedSpriteUrls = {};

        const promises = Object.entries(spritePaths).map(async ([key, src]) => {
            const img = new Image();
            img.src = src;
            await new Promise((resolve, reject) => {
                img.onload = resolve;
                img.onerror = () => reject(new Error(`Failed to load sprite: ${key}`));
            });
            this.preloadedSprites[key] = img;
            this.preloadedSpriteUrls[key] = src;
        });

        await Promise.all(promises);
    }

    /**
     * Transition to the next motion using double-buffering
     * This is called when the current video ends
     */
    async _transitionToNextMotion() {
        if (this.isTransitioning) {
            console.log('[MultiMotion] Already transitioning, skipping');
            return;
        }

        this.isTransitioning = true;
        console.log('[MultiMotion] Transitioning to next motion');

        const nextMotion = this.motions[this.nextIndex];

        // Save pre-transition state (outside try — needed in catch)
        const prevTrackData = this.lipsyncEngine.trackData;
        const prevSprites = { ...this.lipsyncEngine.mouthSprites };
        const prevSpriteUrls = { ...this.lipsyncEngine.mouthSpriteUrls };

        try {
            // Wait for preload to complete (with timeout)
            if (!this.nextMotionReady) {
                console.log('[MultiMotion] Waiting for preload to complete...');
                const timeoutMs = 5000;
                const startTime = Date.now();

                await new Promise((resolve) => {
                    const check = () => {
                        if (this.nextMotionReady) {
                            resolve();
                        } else if (Date.now() - startTime > timeoutMs) {
                            console.warn('[MultiMotion] Preload timeout, proceeding anyway');
                            resolve();
                        } else {
                            setTimeout(check, 50);
                        }
                    };
                    check();
                });
            }

            // Stopped or replaced while waiting: leave the engine alone
            if (this._isStale()) return;

            // 1. Update LipsyncEngine settings before switching video
            this.lipsyncEngine.trackData = nextMotion.trackData;
            this.lipsyncEngine.mouthSprites = this.preloadedSprites;
            this.lipsyncEngine.mouthSpriteUrls = this.preloadedSpriteUrls;
            this.lipsyncEngine.setMouthState('closed', true);

            // 2. Update canvas size
            if (nextMotion.trackData.width && nextMotion.trackData.height) {
                this.lipsyncEngine.mouthCanvas.width = nextMotion.trackData.width;
                this.lipsyncEngine.mouthCanvas.height = nextMotion.trackData.height;
            }

            // 3. Update video reference in LipsyncEngine
            this.lipsyncEngine.video = this.backVideo;

            // 4. Start playback on backVideo
            this.backVideo.currentTime = 0;
            await this.backVideo.play();
            if (this._isStale()) return;

            // 5. Swap z-index and visibility (bring backVideo to front)
            this.backVideo.style.visibility = 'visible';
            this.backVideo.style.zIndex = '1';
            this.frontVideo.style.zIndex = '0';
            this.frontVideo.style.visibility = 'hidden';

            // 6. Restart render loop for new video
            this.lipsyncEngine.startRenderLoop();

            // 7. Stop old frontVideo and clear its event handler
            this.frontVideo.pause();
            this.frontVideo.onended = null;

            // 8. Set up onended handler for new front video (currently backVideo)
            this.backVideo.onended = () => {
                if (this.isPlaying) {
                    this._transitionToNextMotion();
                }
            };

            // 9. Swap front and back references
            [this.frontVideo, this.backVideo] = [this.backVideo, this.frontVideo];
            this.currentIndex = this.nextIndex;

            console.log('[MultiMotion] Transition complete:', nextMotion.name);

            // 10. Start preloading next motion
            this._preloadNextMotion();

        } catch (error) {
            // A stop / replacement interrupts the pending play() (pause or new
            // src rejects it): that is not a failure, and the recovery below
            // would overwrite the engine state of the new session
            if (this._isStale()) return;

            console.error('[MultiMotion] Transition failed, replaying the current motion and retrying:', error);

            // Restore pre-transition state
            this.lipsyncEngine.trackData = prevTrackData;
            this.lipsyncEngine.mouthSprites = prevSprites;
            this.lipsyncEngine.mouthSpriteUrls = prevSpriteUrls;
            this.lipsyncEngine.video = this.frontVideo;
            this.lipsyncEngine.setMouthState('closed', true);

            // Fallback: replay the current motion once and try the transition
            // again when it ends. Do NOT switch to loop=true: a looping video
            // never fires 'ended', so a single transient failure left the
            // character repeating one motion until the next Appear. The usual
            // cause is transient: ~10 s after the window is minimized Chromium
            // pauses muted video and rejects a pending play() with AbortError
            // ("video-only background media was paused to save power")
            this.frontVideo.loop = false;
            this.frontVideo.onended = () => {
                if (this.isPlaying) {
                    this._transitionToNextMotion();
                }
            };
            this.frontVideo.currentTime = 0;
            this.frontVideo.play().catch(e =>
                console.error('[MultiMotion] Recovery play also failed:', e)
            );

            // Clean up backVideo
            this.backVideo.pause();

            // Restart render loop (generation counter auto-stops old chain)
            this.lipsyncEngine.startRenderLoop();
        } finally {
            this.isTransitioning = false;
        }
    }

    /**
     * Start playing
     */
    startPlaying() {
        if (this.disposed) return;
        if (this.isPlaying) return;

        this.isPlaying = true;
        this._startVideoPlayback();

        // Start LipsyncEngine render loop
        this.lipsyncEngine.start();

        console.log('[MultiMotion] Started playing');
    }

    /**
     * Stop playing
     */
    stopPlaying() {
        // A replaced manager no longer owns the shared videos / engine:
        // pausing them here would stop the session that replaced it
        if (this.disposed) {
            this.isPlaying = false;
            return;
        }

        this.isPlaying = false;

        if (this.frontVideo) {
            this.frontVideo.pause();
        }
        if (this.backVideo) {
            this.backVideo.pause();
        }

        this.lipsyncEngine.stop();

        console.log('[MultiMotion] Stopped playing');
    }

    /**
     * Start video playback
     */
    _startVideoPlayback() {
        if (!this.frontVideo) return;

        // Avoid a no-op seek: seeking while a load is still in flight
        // aborts media loading on newer Chromium
        if (this.frontVideo.currentTime > 0) {
            this.frontVideo.currentTime = 0;
        }
        this.frontVideo.play().catch(error => {
            console.error('[MultiMotion] Video play error:', error.name + ': ' + error.message);
            // Retry on user interaction
            const retryPlay = () => {
                this.frontVideo.play();
                document.removeEventListener('click', retryPlay);
            };
            document.addEventListener('click', retryPlay);
        });
    }

    /**
     * Get current motion name
     */
    getCurrentMotionName() {
        if (this.currentIndex >= 0 && this.currentIndex < this.motions.length) {
            return this.motions[this.currentIndex].name;
        }
        return null;
    }

    /**
     * Get total motion count
     */
    getMotionCount() {
        return this.motions.length;
    }

    /**
     * Manually switch to a specific motion by name
     * @param {string} name - Motion name
     */
    async switchToMotionByName(name) {
        const index = this.motions.findIndex(m => m.name === name);
        if (index >= 0) {
            await this._switchMotion(index);
            if (this.isPlaying) {
                this._startVideoPlayback();
            }
        } else {
            console.error('[MultiMotion] Motion not found:', name);
        }
    }

    /**
     * Clean up resources
     */
    cleanup() {
        this.stopPlaying();
        this.disposed = true;   // from here on this manager is inert

        // Clear video sources
        if (this.frontVideo) {
            this.frontVideo.onended = null;
        }
        if (this.backVideo) {
            this.backVideo.onended = null;
        }

        this.motions = [];
        this.trackDataCache = {};
        this.preloadedSprites = {};
        this.preloadedSpriteUrls = {};
        this.nextMotionReady = false;
    }
}
