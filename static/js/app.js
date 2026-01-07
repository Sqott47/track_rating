
// --- Queue UI interaction lock (fix select auto-close) ---
var queueUIBusy = false;
var queueUIBusyTimer = null;


var pendingQueuePayload = null;
(function () {
    var socket = null;
    var socketInited = false;

    var state = {
        track_name: "",
        raters: {},
        criteria: []
    };
    // Map rater_id -> user_id (used for admin kick button; some server snapshots may omit user_id in rater payload)
    window.__RATER_USER_MAP__ = window.__RATER_USER_MAP__ || {};

    // Update kick button targets (data-user-id) without re-rendering panels.
    window.updateKickButtonTargets = window.updateKickButtonTargets || function () {
        try {
            var buttons = document.querySelectorAll(".btn-kick-rater[data-rater-id]");
            buttons.forEach(function (btn) {
                var rid = btn.getAttribute("data-rater-id");
                if (!rid) return;
                var uid = (window.__RATER_USER_MAP__ && window.__RATER_USER_MAP__[String(rid)]) ? window.__RATER_USER_MAP__[String(rid)] : null;
                if (uid) btn.setAttribute("data-user-id", String(uid));
            });
        } catch (e) {}
    };

    // Enable sliders only on "my" panel once join info arrives, without re-rendering.
    window.updateEditablePanels = window.updateEditablePanels || function () {
        try {
            var myRaterId = (window.__MY_RATER_ID__ != null) ? String(window.__MY_RATER_ID__) : null;
            var inRating = !!window.__IN_RATING__;
            var panels = document.querySelectorAll(".rating-panel[data-rater-id]");
            panels.forEach(function (panel) {
                var rid = panel.getAttribute("data-rater-id");
                var editable = !!(inRating && myRaterId && rid && String(rid) === myRaterId);
                var sliders = panel.querySelectorAll("input.score-slider");
                sliders.forEach(function (sl) {
                    sl.disabled = !editable;
                    if (!editable) sl.title = "Можно менять только свой слот";
                    else sl.title = "";
                });
            });
        } catch (e) {}
    };


    // Очередь треков + синхро‑плеер (используется только на /panel)
    var queueState = { items: [], counts: {} };
    var playbackState = { active: null, playback: { is_playing: false, position_ms: 0 } };

    // NOTE: this file is cached by Turbo Drive; keep admin flag in sync
    // with server-rendered value on each visit.
    var isAdmin = !!(window && window.__IS_ADMIN__);
    // Queue moderation is allowed for judges and admins.
    var canQueueModerate = !!(window && window.__IS_JUDGE__) || isAdmin;
    var isPanelPage = false;
    // Публичная страница очереди /queue (без сокет‑доступа), обновляем через /api/queue.
    var isQueuePublicPage = false;

    // Polling timer for /queue (must be single instance across Turbo navigations).
    var queuePublicPollIntervalId = null;

    var audioEl = null;
    var applyingRemoteAudio = false;

    // Rating membership: when true, this client must stay synced and cannot play local tracks.
    window.__IN_RATING__ = !!window.__IN_RATING__;

    function clamp(value, min, max) {
        return Math.min(max, Math.max(min, value));
    }

    // Пулы мемных фраз по диапазонам итогового балла
    var MEME_BUCKETS = [
        {
            min: 0,
            max: 3,
            phrases: [
                "Явно лучше, чем трек стримера",
                "Ну ты явно старался, брат",
                "Почти соус, пока не газ",
                "Кажется, хулагу должен за тебя шарить"
            ]
        },
        {
            min: 3,
            max: 6,
            phrases: [
                "Есть газ, но надо работать",
                "Могло быть лучше, если бы задонатил побольше",
                "СТРИМЕРУ ЖАБЕ НЕ ХВАТИЛО ДЕНЕГ НА ОЦЕНКУ",
                "Все еще лучше, чем любой трек стримера",
                "Надеюсь за этот, блядь, три будет"
            ]
        },
        {
            min: 6,
            max: 8,
            phrases: [
                "Нужно больше соуса!",
                "Нужно больше гаааза",
                "Ух бля, походу в плейлист",
                "Уже минетчик, но еще не здравый"
            ]
        },
        {
            min: 8,
            max: 10.0001,
            phrases: [
                "ANTIGAZZZZZZZZZZ",
                "ЖАБА лично одобряет этот звук",
                "БРАТ, СКИДЫВАЙ БОЛЬШЕ ТРЕКОВ",
                "ЭТОТ ГАЗОВЫЙ ГИГАНТ ДАСТ В РОТ ЮПИТЕРУ",
                "Чувак, ты пришел сюда выебываться? У ТЕБЯ ПОЛУЧИЛОСЬ",
                "Стример завершает карьеру, лучше уже не будет",
                "Если бы ты был соусом, чувак, сто процентов КЕТЧУНЕЗ"
            ]
        }
    ];

    function getMemePhrase(score) {
        var s = Number(score) || 0;
        for (var i = 0; i < MEME_BUCKETS.length; i++) {
            var b = MEME_BUCKETS[i];
            if (s >= b.min && s < b.max) {
                var list = b.phrases || [];
                if (!list.length) return "";
                var idx = Math.floor(Math.random() * list.length);
                return list[idx];
            }
        }
        return "";
    }

    function heatColorForScore(score) {
        var v = clamp(Number(score) || 0, 0, 10);
        var t = v / 10;
        // cold to hot: from cool blue-violet (~215deg) to softer red (~0deg, close to #ff4545)
        var startHue = 215;
        var endHue = 0;
        var hue = startHue + (endHue - startHue) * t;
        var sat = 68;            // немного поярче, но без дичайшей кислотности
        var light = 50 - 4 * t;  // держим в среднем диапазоне по яркости
        return "hsl(" + hue + ", " + sat + "%, " + light + "%)";
    }



function applyHeatToChip(el, score) {
        if (!el) return;
        var v = clamp(Number(score) || 0, 0, 10);
        var color = heatColorForScore(v);

        // Calm pill background without dark vignette
        el.style.background = "linear-gradient(135deg, " + color + ", rgba(15,23,42,0.96))";
        el.style.color = v > 0 ? "#f9fafb" : "#e5e7eb";
        el.style.boxShadow = v > 0 ? "0 0 " + (3 + v * 0.8) + "px rgba(248,113,113,0.45)" : "none";

        if (v >= 9.95 && v <= 10.05) {
            el.classList.add("score-chip--flame");
            el.classList.add("score-chip--hot");

            // sync all 10/10 flames by aligning animation phase to global time
            var periodPulse = 1.8;
            var periodFlame = 1.3;
            var now = Date.now() / 1000;
            var phasePulse = now % periodPulse;
            var phaseFlame = now % periodFlame;

            el.style.animationDelay = (-phasePulse).toFixed(2) + "s";
            // pseudo-element can't read this but we can use CSS variable if needed later
            el.style.setProperty("--flame-delay", (-phaseFlame).toFixed(2) + "s");
        } else {
            el.classList.remove("score-chip--flame");
            el.classList.remove("score-chip--hot");
            el.style.animationDelay = "";
            el.style.removeProperty("--flame-delay");
        }
    }


    

function applyHeatToAllScoreChips(rootEl) {
    try {
        var root = rootEl || document;
        var chips = root.querySelectorAll ? root.querySelectorAll(".score-chip") : [];
        chips.forEach(function (chip) {
            // Ensure number text is wrapped so the flame (pseudo-element) stays under the digits.
            if (!chip.querySelector(".score-chip-label")) {
                var rawText = (chip.textContent || "").trim();
                chip.textContent = "";
                var inner = document.createElement("span");
                inner.className = "score-chip-label";
                inner.textContent = rawText;
                chip.appendChild(inner);
            }

            var label = chip.querySelector(".score-chip-label");
            var txt = (label && label.textContent ? label.textContent : (chip.textContent || ""))
                .replace(",", ".")
                .trim();
            var val = parseFloat(txt);
            if (!isNaN(val)) {
                applyHeatToChip(chip, val);
            }
        });
    } catch (e) {}
}


function applyHeatToSlider(slider, score) {
    if (!slider) return;
    var v = clamp(Number(score) || 0, 0, 10);
    var color = heatColorForScore(v);
    // slider background
    slider.style.background = "linear-gradient(90deg, " + color + ", rgba(15,23,42,0.95))";
    slider.style.boxShadow = v > 0 ? "0 0 " + (4 + v) + "px " + color : "none";

    // если значение 10 — включаем режим диджея (бесконечное лёгкое покачивание)
    if (v >= 9.95 && v <= 10.05) {
        slider.classList.add("frog-dj-mode");
    } else {
        slider.classList.remove("frog-dj-mode");
    }
}
function updateTrackNameDisplays(name) {
        var display = document.getElementById("track-name-display");
        if (display) {
            display.textContent = name || "—";
        }
        document.querySelectorAll("[data-track-display]").forEach(function (el) {
            el.textContent = name || "—";
        });
    }

    function computeAndRenderTotalsFromState() {
        var ratersArray = Object.values(state.raters || {});
        if (!ratersArray.length) {
            var global = document.getElementById("global-total");
            if (global) {
                global.textContent = "0.0";
                applyHeatToChip(global, 0);
            }
            return;
        }

        var globalSum = 0;
        var globalCount = 0;

        ratersArray.forEach(function (rater) {
            var scores = rater.scores || {};
            var vals = Object.values(scores).map(function (v) { return Number(v) || 0; });
            var avg = vals.length ? vals.reduce(function (a, b) { return a + b; }, 0) / vals.length : 0;
            rater._avgLive = avg;

            var panel = document.querySelector('.rating-panel[data-rater-id="' + rater.id + '"]');
            if (panel) {
                var totalEl = panel.querySelector("[data-panel-total]");
                if (totalEl) {
                    totalEl.textContent = avg.toFixed(1);
                    applyHeatToChip(totalEl, avg);
                }
            }

            globalSum += avg;
            globalCount += 1;
        });

        var globalAvg = globalCount ? globalSum / globalCount : 0;
        var globalEl = document.getElementById("global-total");
        if (globalEl) {
            globalEl.textContent = globalAvg.toFixed(1);
            applyHeatToChip(globalEl, globalAvg);
        }
        updateRaterFireStates();
    }

    function updateRaterFireStates() {
        var ratersArray = Object.values(state.raters || {});
        ratersArray.forEach(function (rater) {
            var panel = document.querySelector('.rating-panel[data-rater-id="' + rater.id + '"]');
            if (!panel) return;

            var avg = Number(rater._avgLive || 0);
            var allTen = avg >= 9.95 && avg <= 10.05;

            if (allTen) {
                panel.classList.add("rater-card--on-fire");
            } else {
                panel.classList.remove("rater-card--on-fire");
            }
        });
    }

    function createPanelElement(rater) {
        var panel = document.createElement("section");
        panel.className = "rating-panel";
        panel.dataset.raterId = rater.id;

        var myRaterId = (window.__MY_RATER_ID__ != null) ? String(window.__MY_RATER_ID__) : null;
        var editable = !!(window.__IN_RATING__ && myRaterId && String(rater.id) === myRaterId);
        if (!editable) { panel.classList.add('rating-panel--readonly'); }

        var inner = document.createElement("div");
        inner.className = "panel-inner";
        panel.appendChild(inner);

        var header = document.createElement("div");
        header.className = "panel-header";
        inner.appendChild(header);

        var headerTop = document.createElement("div");
        headerTop.className = "panel-header-top";
        header.appendChild(headerTop);


        // Header actions (admin kick)
        var headerActions = document.createElement("div");
        headerActions.className = "panel-header-actions";
        headerTop.appendChild(headerActions);

        var isAdmin = !!window.__IS_ADMIN__;
        var myUserId = (window.__USER_ID__ != null) ? String(window.__USER_ID__) : null;
        var targetUserId = null;
        if (rater && rater.user_id != null) {
            targetUserId = String(rater.user_id);
        } else if (window.__RATER_USER_MAP__ && window.__RATER_USER_MAP__[String(rater.id)] != null) {
            targetUserId = String(window.__RATER_USER_MAP__[String(rater.id)]);
        }

        // Kick button: shown on every panel. Only admins can actually kick.
        var canKick = true;
        if (canKick) {
            var kickBtn = document.createElement("button");
            kickBtn.type = "button";
            kickBtn.className = "btn-danger btn-xs btn-kick-rater";
            kickBtn.textContent = "✕";
            kickBtn.setAttribute("data-rater-id", String(rater.id));
            if (targetUserId) kickBtn.setAttribute("data-user-id", String(targetUserId));
            // Inline fallback to guarantee the click is handled even if other scripts swallow events.
            // NOTE: some UI code navigates on pointerdown, so we also hook pointerdown.
            kickBtn.setAttribute("onclick", "window.kickRaterFromBtn && window.kickRaterFromBtn(this, event); return false;");
            kickBtn.setAttribute("onpointerdown", "window.kickRaterFromBtn && window.kickRaterFromBtn(this, event); return false;");
            // Direct listener as a final fallback (some pages attach handlers on pointerdown
            // and swallow bubbling/click; capturing ensures we still get the event).
            try {
                kickBtn.addEventListener("pointerdown", function (ev) {
                    kickRaterFromBtn(kickBtn, ev);
                }, true);
            } catch (e) {}
            headerActions.appendChild(kickBtn);
        }
        var trackLine = document.createElement("div");
        trackLine.className = "track-title-line";
        header.appendChild(trackLine);

        var trackLabel = document.createElement("span");
        trackLabel.className = "track-title-label";
        trackLabel.textContent = "Название трека:";
        trackLine.appendChild(trackLabel);

        var trackValue = document.createElement("span");
        trackValue.className = "track-title-value";
        trackValue.dataset.trackDisplay = "";
        trackValue.textContent = state.track_name || "—";
        trackLine.appendChild(trackValue);

        var raterName = document.createElement("label");
        raterName.className = "rater-name";
        header.appendChild(raterName);

        var rnSpan = document.createElement("span");
        rnSpan.textContent = "Имя оценщика:";
        raterName.appendChild(rnSpan);

        var rnInput = document.createElement("input");
        rnInput.type = "text";
        rnInput.className = "rater-name-input";
        rnInput.value = rater.name || "";
        raterName.appendChild(rnInput);

        rnInput.disabled = true;
        rnInput.readOnly = true;

        var body = document.createElement("div");
        body.className = "panel-body";
        inner.appendChild(body);

        (state.criteria || []).forEach(function (criterion) {
            var row = document.createElement("div");
            row.className = "slider-row";
            body.appendChild(row);

            var label = document.createElement("div");
            label.className = "slider-label";
            label.textContent = criterion.label;
            row.appendChild(label);

            var control = document.createElement("div");
            control.className = "slider-control";
            row.appendChild(control);

            var slider = document.createElement("input");
            slider.type = "range";
            slider.min = "0";
            slider.max = "10";
            slider.step = "1";
            slider.className = "score-slider";
            slider.disabled = !editable;
            if (!editable) { slider.title = "Можно менять только свой слот"; }
            var v = (rater.scores && Object.prototype.hasOwnProperty.call(rater.scores, criterion.key))
                ? Number(rater.scores[criterion.key] || 0)
                : 0;
            slider.value = String(v);
            slider.dataset.criterionKey = criterion.key;
            control.appendChild(slider);

            var valueBox = document.createElement("div");
            valueBox.className = "slider-value score-chip";
            valueBox.dataset.sliderValue = "";
            valueBox.textContent = String(v.toFixed ? v.toFixed(0) : v);
            control.appendChild(valueBox);

            applyHeatToSlider(slider, v);
            applyHeatToChip(valueBox, v);

            slider.disabled = !editable;

            slider.addEventListener("input", function () {
                if (!editable) return;
                var newVal = Number(slider.value) || 0;
                valueBox.textContent = String(newVal);
                applyHeatToSlider(slider, newVal);
                applyHeatToChip(valueBox, newVal);

                if (state.raters[rater.id]) {
                    if (!state.raters[rater.id].scores) {
                        state.raters[rater.id].scores = {};
                    }
                    state.raters[rater.id].scores[criterion.key] = newVal;
                }

                computeAndRenderTotalsFromState();

                if (socket) {
                    socket.emit("change_slider", {
                        rater_id: rater.id,
                        criterion_key: criterion.key,
                        value: newVal
                    });
                }
            });
        });

        var footer = document.createElement("div");
        footer.className = "panel-footer";
        inner.appendChild(footer);

        var totalText = document.createElement("div");
        totalText.className = "panel-total-text";
        footer.appendChild(totalText);

        var totalLabel = document.createElement("span");
        totalLabel.textContent = "Общий балл:";
        totalText.appendChild(totalLabel);

        var totalValue = document.createElement("span");
        totalValue.className = "panel-total-value score-chip";
        totalValue.dataset.panelTotal = "";
        totalValue.textContent = "0.0";
        totalText.appendChild(totalValue);

        applyHeatToChip(totalValue, 0);

        return panel;
    }

    function renderAllPanels() {
        var container = document.getElementById("panels-container");
        if (!container) return;
        container.innerHTML = "";

        var ratersArray = Object.values(state.raters || {});
        ratersArray.sort(function (a, b) {
            return (a.order || 0) - (b.order || 0);
        });

        ratersArray.forEach(function (rater) {
            container.appendChild(createPanelElement(rater));
        });

        computeAndRenderTotalsFromState();
        updateRaterFireStates();
    }

    function openResultModal(payload) {
        var backdrop = document.getElementById("result-modal-backdrop");
        if (!backdrop) return;

        var trackName = payload.track_name || "Без названия";
        var criteria = payload.criteria || [];
        var raters = payload.raters || [];
        var overall = typeof payload.overall === "number" ? payload.overall : null;

        var modalTrack = document.getElementById("modal-track-name");
        if (modalTrack) {
            modalTrack.textContent = "Трек: " + trackName;
        }

        var tbodyCriteria = document.querySelector("#criteria-table tbody");
        if (tbodyCriteria) {
            tbodyCriteria.innerHTML = "";
            criteria.forEach(function (c) {
                var tr = document.createElement("tr");
                var tdName = document.createElement("td");
                tdName.textContent = c.label || c.key;

                var tdVal = document.createElement("td");
                var value = typeof c.average === "number" ? c.average : 0;

                var chip = document.createElement("span");
                chip.className = "score-chip";

                var inner = document.createElement("span");
                inner.className = "score-chip-label";
                inner.textContent = value.toFixed(2);
                chip.appendChild(inner);

                tdVal.appendChild(chip);

                tr.appendChild(tdName);
                tr.appendChild(tdVal);
                tbodyCriteria.appendChild(tr);

                if (typeof applyHeatToChip === "function") {
                    applyHeatToChip(chip, value);
                }
            });

        }

        var tbodyRaters = document.querySelector("#raters-table tbody");
        if (tbodyRaters) {
            tbodyRaters.innerHTML = "";
            raters.forEach(function (r) {
                var tr = document.createElement("tr");
                var tdName = document.createElement("td");
                tdName.textContent = r.name || ("Оценщик " + r.id);

                var tdVal = document.createElement("td");
                var value = typeof r.average === "number" ? r.average : 0;

                var chip = document.createElement("span");
                chip.className = "score-chip";

                var inner = document.createElement("span");
                inner.className = "score-chip-label";
                inner.textContent = value.toFixed(2);
                chip.appendChild(inner);

                tdVal.appendChild(chip);

                tr.appendChild(tdName);
                tr.appendChild(tdVal);
                tbodyRaters.appendChild(tr);

                if (typeof applyHeatToChip === "function") {
                    applyHeatToChip(chip, value);
                }
            });

        }

        var modalOverall = document.getElementById("modal-overall");
        if (modalOverall) {
            modalOverall.textContent = overall != null ? overall.toFixed(2) : "0.00";
            applyHeatToChip(modalOverall, overall || 0);
        }

        var rankEl = document.getElementById("modal-top-rank");
        if (rankEl) {
            var pos = payload.top_position;
            var rankText = "";
            if (typeof pos === "number" && pos > 0) {
                if (pos === 1) {
                    rankText = "🔥 ТОП-1";
                } else if (pos <= 3) {
                    rankText = "⭐ ТОП-3 (место " + pos + ")";
                } else if (pos <= 10) {
                    rankText = "🥉 ТОП-10 (место " + pos + ")";
                } else {
                    rankText = pos + " место в топе";
                }
            }
            rankEl.textContent = rankText;
            rankEl.style.display = rankText ? "inline-flex" : "none";
        }

        var memeEl = document.getElementById("modal-meme-phrase");
        if (memeEl) {
            var phrase = getMemePhrase(overall || 0);
            if (phrase) {
                memeEl.textContent = phrase;
                memeEl.style.display = "block";
            } else {
                memeEl.textContent = "";
                memeEl.style.display = "none";
            }
        }


        // QR-код и ссылка на страницу трека
        var qrImg = document.getElementById("modal-track-qr");
        if (qrImg) {
            if (payload.qr_url) {
                qrImg.src = payload.qr_url;
                qrImg.style.display = "block";
            } else {
                qrImg.style.display = "none";
            }
        }
        

        backdrop.classList.add("is-open");
    }

    function closeResultModal() {
        var backdrop = document.getElementById("result-modal-backdrop");
        if (backdrop) {
            backdrop.classList.remove("is-open");
        }
        // больше не сбрасываем состояние при закрытии поп-апа —
        // сброс только через кнопку "Новый трек"
    }


function initModalHandlers() {
        var closeBtn = document.getElementById("modal-close-btn");
        if (closeBtn) {
            closeBtn.addEventListener("click", function () {
                closeResultModal();
            });
        }
        var backdrop = document.getElementById("result-modal-backdrop");
        if (backdrop) {
            backdrop.addEventListener("click", function (e) {
                if (e.target === backdrop) {
                    closeResultModal();
                }
            });
        }
        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape") {
                closeResultModal();
            }
        });
    }

    function initImageLightbox() {
        // Use event delegation so it works after Turbo navigation.
        if (window.__imageLightboxDelegated) return;
        window.__imageLightboxDelegated = true;

        function getLightbox() {
            return {
                root: document.getElementById("img-lightbox"),
                img: document.getElementById("img-lightbox-img"),
                close: document.getElementById("img-lightbox-close")
            };
        }

        function open(src, alt) {
            var lb = getLightbox();
            if (!lb.root || !lb.img) return;
            lb.img.src = src;
            lb.img.alt = alt || "";
            lb.root.classList.add("is-open");
            lb.root.setAttribute("aria-hidden", "false");
            try { document.body.style.overflow = "hidden"; } catch (e) {}
        }

        function close() {
            var lb = getLightbox();
            if (!lb.root || !lb.img) return;
            lb.root.classList.remove("is-open");
            lb.root.setAttribute("aria-hidden", "true");
            lb.img.src = "";
            try { document.body.style.overflow = ""; } catch (e) {}
        }

        document.addEventListener("click", function (e) {
            var btn = e.target && e.target.closest ? e.target.closest(".js-image-preview") : null;
            if (btn) {
                var src = btn.getAttribute("data-src");
                var alt = btn.getAttribute("data-alt") || "";
                if (src) {
                    e.preventDefault();
                    open(src, alt);
                    return;
                }
            }

            var lb = getLightbox();
            if (lb.root && lb.root.classList.contains("is-open")) {
                if (e.target === lb.root || (e.target && e.target.getAttribute && e.target.getAttribute("data-close") === "1")) {
                    close();
                }
            }
        });

        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape") {
                var lb = getLightbox();
                if (lb.root && lb.root.classList.contains("is-open")) close();
            }
        });

        // Close button
        document.addEventListener("click", function (e) {
            var lb = getLightbox();
            if (!lb.root) return;
            if (e.target && lb.close && e.target === lb.close) {
                close();
            }
        });
    }


    function initTrackInput() {
        var input = document.getElementById("track-name-input");
        if (!input) return;

        input.addEventListener("change", function () {
            if (socket) {
                socket.emit("change_track_name", { track_name: input.value });
            }
        });

        input.addEventListener("keyup", function (e) {
            if (e.key === "Enter") {
                if (socket) {
                    socket.emit("change_track_name", { track_name: input.value });
                }
                input.blur();
            }
        });
    }

    function initControls() {
        var joinBtn = document.getElementById("join-rating-btn");
        if (joinBtn && !joinBtn.dataset.bound) {
            joinBtn.dataset.bound = "1";
            joinBtn.addEventListener("click", function () {
                if (socket) socket.emit("join_rating");
            });
        }
        var leaveBtn = document.getElementById("leave-rating-btn");
        if (leaveBtn && !leaveBtn.dataset.bound) {
            leaveBtn.dataset.bound = "1";
            leaveBtn.addEventListener("click", function () {
                if (socket) socket.emit("leave_rating");
            });
        }

        var evalBtn = document.getElementById("evaluate-btn");
        if (evalBtn && !evalBtn.dataset.bound) {
            evalBtn.dataset.bound = "1";
            evalBtn.addEventListener("click", function () {
                if (socket) {
                    socket.emit("evaluate");
                }
            });
        }

        var newTrackBtn = document.getElementById("new-track-btn");
        if (newTrackBtn && !newTrackBtn.dataset.bound) {
            newTrackBtn.dataset.bound = "1";
            newTrackBtn.addEventListener("click", function () {
                if (socket) {
                    socket.emit("reset_state");
                }
            });
        }
    }


    function formatQueueStatus(status) {
        if (status === "queued") return "в очереди";
        if (status === "converting") return "конвертируется";
        if (status === "failed") return "ошибка";
        return status || "—";
    }


    
    function stopSyncAudio() {
        var a = getSyncAudioEl();
        if (!a) return;
        try { applyingRemoteAudio = true; } catch (e) {}
        try { a.pause(); } catch (e) {}
        try { a.currentTime = 0; } catch (e) {}
        try { a.removeAttribute("src"); a.load(); } catch (e) {}
        try { applyingRemoteAudio = false; } catch (e) {}
    }

function getSyncAudioEl() {
        if (audioEl) return audioEl;
        audioEl = document.getElementById("sync-audio");
        return audioEl;
    }


    function renderQueueState(payload) {

// 🔒 Не перерисовываем очередь, если пользователь с ней взаимодействует (иначе закроется <select>)
if (queueUIBusy) {
    pendingQueuePayload = payload || pendingQueuePayload;
    // обновляем состояние в памяти, но не трогаем DOM
    queueState.items = (payload && payload.items) ? payload.items : (queueState.items || []);
    queueState.counts = (payload && payload.counts) ? payload.counts : (queueState.counts || {});
    queueState.active = (payload && payload.active) ? payload.active : (queueState.active || null);
    return;
}

        // Панель (/panel) использует div‑список; публичная очередь (/queue) — таблицу.
        var container = document.getElementById("queue-items");
        var publicTbody = document.getElementById("queue-public-tbody");
        if (!container && !publicTbody) return;

        var empty = document.getElementById("queue-empty");
        var meta = document.getElementById("queue-count-meta");

        var items = (payload && payload.items) ? payload.items : [];
        var counts = (payload && payload.counts) ? payload.counts : {};

        queueState.items = items;
        queueState.counts = counts;
        // keep last known active track meta (for UI fallback)
        queueState.active = (payload && payload.active) ? payload.active : (queueState.active || null);

        if (meta) {
            var q = counts.queued || 0;
            var c = counts.converting || 0;
            meta.textContent = q + " в очереди" ;
        }

        // 1) Обновление панели
        if (container) {
            container.innerHTML = "";

            if (!items.length) {
                if (empty) empty.style.display = "block";
            } else {
                if (empty) empty.style.display = "none";
            }

            items.forEach(function (item) {
                var row = document.createElement("div");
                row.className = "queue-item";

            var main = document.createElement("div");
            main.className = "queue-item-main";

            var title = document.createElement("div");
            title.className = "queue-item-title";
            title.textContent = item.display_name || "—";

            var metaRow = document.createElement("div");
            metaRow.className = "queue-item-meta";
            var posText = item.queue_position ? ("#" + item.queue_position) : "—";
            var prText = "prio " + (item.priority || 0);
            var stText = formatQueueStatus(item.status);
            metaRow.textContent = posText + " · " + prText + " · " + stText;

            main.appendChild(title);
            main.appendChild(metaRow);
            row.appendChild(main);

            var actions = document.createElement("div");
            actions.className = "queue-item-actions";

            if (canQueueModerate) {
                // приоритет
                var sel = document.createElement("select");
                sel.className = "queue-priority-select";
                [0, 100, 200, 300, 400].forEach(function (v) {
                    var opt = document.createElement("option");
                    opt.value = String(v);
                    opt.textContent = String(v);
                    if (Number(item.priority || 0) === v) opt.selected = true;
                    sel.appendChild(opt);
                });
                sel.addEventListener("change", function () {
                    if (!socket) return;
                    socket.emit("admin_set_submission_priority", {
                        submission_id: item.id,
                        priority: Number(sel.value) || 0
                    });
                });
                actions.appendChild(sel);

                var playBtn = document.createElement("button");
                playBtn.type = "button";
                playBtn.className = "btn-primary queue-action-btn";
                playBtn.textContent = "Включить";
                if (item.status !== "queued") {
                    playBtn.disabled = true;
                }
                playBtn.addEventListener("click", function () {
                    if (!socket) return;
                    socket.emit("admin_activate_submission", {
                        submission_id: item.id,
                        autoplay: true
                    });
                });
                actions.appendChild(playBtn);

                var delBtn = document.createElement("button");
                delBtn.type = "button";
                delBtn.className = "btn-ghost queue-action-btn";
                delBtn.textContent = "Удалить";
                delBtn.addEventListener("click", function () {
                    if (!socket) return;
                    var ok = confirm("Удалить трек из очереди?");
                    if (!ok) return;
                    socket.emit("admin_delete_submission", { submission_id: item.id });
                });
                actions.appendChild(delBtn);

                // 🔧 Debug: quick link to the generated track page (opens in new tab)
                // Available for judge/admin only (same as canQueueModerate).
                if (item.linked_track_id) {
                    var trackLink = document.createElement("a");
                    trackLink.href = "/track/" + String(item.linked_track_id);
                    trackLink.target = "_blank";
                    trackLink.rel = "noopener";
                    trackLink.className = "queue-debug-link";
                    trackLink.title = "Открыть страницу трека";
                    trackLink.textContent = "↗";
                    actions.appendChild(trackLink);
                }
            }

            row.appendChild(actions);
                container.appendChild(row);
            });
        }

        // 2) Обновление публичной таблицы (/queue)
        if (publicTbody) {
            renderQueuePublicTable(items, counts);
        }
    }


    function renderQueuePublicTable(items, counts) {
        var tbody = document.getElementById("queue-public-tbody");
        if (!tbody) return;

        // stats
        var qEl = document.getElementById("queue-stat-queued");
        var cEl = document.getElementById("queue-stat-converting");
        if (qEl) qEl.textContent = String((counts && counts.queued) ? counts.queued : 0);
        // rows
        tbody.innerHTML = "";
        if (!items || !items.length) {
            var tr = document.createElement("tr");
            var td = document.createElement("td");
            td.colSpan = 5;
            td.innerHTML = "<em>Очередь пуста.</em>";
            tr.appendChild(td);
            tbody.appendChild(tr);
            return;
        }

        items.forEach(function (item) {
            var tr = document.createElement("tr");
            tr.className = "top-row";

            var tdPos = document.createElement("td");
            tdPos.className = "top-pos";
            tdPos.textContent = item.queue_position ? String(item.queue_position) : "—";

            var tdName = document.createElement("td");
            tdName.className = "top-name-cell";
            tdName.textContent = item.display_name || "—";

            var tdPr = document.createElement("td");
            tdPr.className = "top-score-cell";
            var pill = document.createElement("span");
            pill.className = "queue-priority-pill";
            pill.textContent = String(item.priority || 0);
            tdPr.appendChild(pill);

            var tdSt = document.createElement("td");
            tdSt.className = "top-date-cell";
            var st = document.createElement("span");
            if (item.status === "queued") {
                st.className = "queue-status queue-status--queued";
                st.textContent = "в очереди";
            } else {
                st.className = "queue-status";
                st.textContent = String(item.status || "—");
            }
            tdSt.appendChild(st);

            var tdDt = document.createElement("td");
            tdDt.className = "top-date-cell";
            tdDt.textContent = formatDateDDMMYYYY(item.created_at);

            tr.appendChild(tdPos);
            tr.appendChild(tdName);
            tr.appendChild(tdPr);
            tr.appendChild(tdSt);
            tr.appendChild(tdDt);
            tbody.appendChild(tr);
        });
    }


    function formatDateDDMMYYYY(isoStr) {
        if (!isoStr) return "—";
        // сервер отдаёт YYYY-MM-DD...
        if (typeof isoStr === "string" && isoStr.length >= 10) {
            var y = isoStr.slice(0, 4);
            var m = isoStr.slice(5, 7);
            var d = isoStr.slice(8, 10);
            if (y && m && d) return d + "." + m + "." + y;
        }
        return "—";
    }


    function applyPlaybackState(payload) {
        var a = getSyncAudioEl();
        if (!a) return;

        // Show/hide the global dock depending on whether there is an active synced track.
        var dock = document.getElementById("yplayer-dock");

        playbackState = payload || playbackState;

        var active = payload && payload.active ? payload.active : null;
        var pb = payload && payload.playback ? payload.playback : { is_playing: false, position_ms: 0 };

        // Fallback: sometimes playback_state may omit active meta; use last queue_state.active
        if (!active && queueState && queueState.active) {
            active = queueState.active;
        }

        // Верхняя панель "Сейчас играет"
        var meta = document.getElementById("queue-active-meta");
        if (meta) {
            meta.textContent = active ? (active.display_name || "—") : "—";
        }

        // Нижний dock-плеер: метаданные трека
        // Источник истины: payload.active, но если сервер прислал playback без active — берём из последнего queueState.active
        var effectiveActive = active || (queueState && queueState.active ? queueState.active : null);
        var yTitle = document.getElementById("yplayer-title");
        var ySub = document.getElementById("yplayer-subtitle");
        if (yTitle) {
            if (!effectiveActive) yTitle.textContent = "—";
            else {
                // если сервер не отдаёт artist/title отдельно — используем display_name и пытаемся разделить по "—"
                var dn = effectiveActive.display_name || "";
                var parts = dn.split("—").map(function (s) { return (s || "").trim(); }).filter(Boolean);
                if (parts.length >= 2) {
                    yTitle.textContent = parts.slice(1).join(" — ");
                } else {
                    yTitle.textContent = dn || "—";
                }
            }
        }
        if (ySub) {
            if (!effectiveActive) ySub.textContent = "—";
            else {
                var dn2 = effectiveActive.display_name || "";
                var parts2 = dn2.split("—").map(function (s) { return (s || "").trim(); }).filter(Boolean);
                var artist = (parts2.length >= 2) ? parts2[0] : "";
                var pr = (effectiveActive.priority != null) ? ("donate prio " + effectiveActive.priority) : "";
                var bits = [];
                if (pr) bits.push(pr);
                if (artist) bits.push(artist);
                ySub.textContent = bits.length ? bits.join(" • ") : "—";
            }
        }

        var yWrap = document.getElementById("sync-player");
        if (yWrap) {
            if (pb && pb.is_playing) yWrap.classList.add("is-playing");
            else yWrap.classList.remove("is-playing");
        }

        // если активного трека нет — сбрасываем
        if (!active || !active.audio_url) {
            if (dock) dock.style.display = "none";
            try {
                applyingRemoteAudio = true;
                a.pause();
                a.removeAttribute("src");
                a.load();
            } catch (e) { }
            finally {
                applyingRemoteAudio = false;
            }
            return;
        }

        if (dock) dock.style.display = "block";

        var desiredSrc = active.audio_url;
        var needsReload = (a.getAttribute("src") !== desiredSrc);

        var targetSec = (Number(pb.position_ms) || 0) / 1000.0;

        function doPlayPause() {
            if (pb.is_playing) {
                var p = a.play();
                if (p && typeof p.then === "function") {
                    p.then(function () {
                        var warn = document.getElementById("sync-audio-warning");
                        if (warn) warn.style.display = "none";
                    });
                }
                if (p && typeof p.catch === "function") {
                    p.catch(function () {
                        var warn = document.getElementById("sync-audio-warning");
                        if (warn) warn.style.display = "block";
                    });
                }
            } else {
                a.pause();
                var warn2 = document.getElementById("sync-audio-warning");
                if (warn2) warn2.style.display = "none";
            }
        }

        applyingRemoteAudio = true;

        if (needsReload) {
            try {
                a.setAttribute("src", desiredSrc);
                a.load();
                // дождёмся метаданных и поставим позицию (иначе currentTime может не примениться)
                var onMeta = function () {
                    a.removeEventListener("loadedmetadata", onMeta);
                    try {
                        if (isFinite(targetSec)) {
                            a.currentTime = Math.max(0, targetSec);
                        }
                    } catch (e) { }
                    doPlayPause();
                    setTimeout(function () { applyingRemoteAudio = false; }, 0);
                };
                a.addEventListener("loadedmetadata", onMeta);
            } catch (e) {
                // fallback: просто попробуем play/pause
                doPlayPause();
                setTimeout(function () { applyingRemoteAudio = false; }, 0);
            }
            return;
        }

        try {
            // позиция (на том же src)
            var cur = Number(a.currentTime) || 0;
            if (isFinite(targetSec) && Math.abs(cur - targetSec) > 0.75) {
                try {
                    a.currentTime = Math.max(0, targetSec);
                } catch (e) {
                    // ignore
                }
            }
            doPlayPause();
        } finally {
            // небольшая задержка, чтобы события play/pause/seeked от программных действий не ушли в сокет
            setTimeout(function () { applyingRemoteAudio = false; }, 0);
        }
    }


    function initPlaybackControls() {
        var a = getSyncAudioEl();
        if (!a) return;

        // Кнопка "Включить звук" для оценщиков (из-за autoplay policy)
        var unlock = document.getElementById("unlock-audio-btn");
        if (unlock && unlock.dataset.bound !== "1") {
            unlock.dataset.bound = "1";
            unlock.addEventListener("click", function () {
                var warn = document.getElementById("sync-audio-warning");
                if (warn) warn.style.display = "none";
                var p = a.play();
                if (p && typeof p.then === "function") {
                    p.then(function () {
                        // если по серверу сейчас пауза — сразу ставим паузу,
                        // чтобы кнопка использовалась как "unlock" без рассинхрона
                        try {
                            var shouldPlay = !!(playbackState && playbackState.playback && playbackState.playback.is_playing);
                            if (!shouldPlay) a.pause();
                        } catch (e) { }
                    });
                }
                if (p && typeof p.catch === "function") {
                    p.catch(function () {
                        if (warn) warn.style.display = "block";
                    });
                }
            });
        }

        if (!isAdmin) {
            return;
        }

        var playBtn = document.getElementById("player-play-btn");
        if (playBtn && playBtn.dataset.bound !== "1") {
            playBtn.dataset.bound = "1";
            playBtn.addEventListener("click", function () {
                // Админ управляет синхро‑плеером (play/pause). Не‑админ — только «разрешает звук» (user gesture).
                if (!IS_ADMIN) {
                    var a = getSyncAudioEl();
                    if (a) {
                        var p = a.play();
                        if (p && typeof p.catch === "function") p.catch(function(){});
                    }
                    return;
                }
                var isPlaying = playbackState && playbackState.playback ? !!playbackState.playback.is_playing : false;
                if (socket) socket.emit("admin_playback_cmd", { action: isPlaying ? "pause" : "play" });
            });
        }
        var pauseBtn = document.getElementById("player-pause-btn");
        if (pauseBtn && pauseBtn.dataset.bound !== "1") {
            pauseBtn.dataset.bound = "1";
            pauseBtn.addEventListener("click", function () {
                if (socket) socket.emit("admin_playback_cmd", { action: "pause" });
            });
        }
        var restartBtn = document.getElementById("player-restart-btn");
        if (restartBtn && restartBtn.dataset.bound !== "1") {
            restartBtn.dataset.bound = "1";
            restartBtn.addEventListener("click", function () {
                if (socket) socket.emit("admin_playback_cmd", { action: "restart" });
            });
        }
        var stopBtn = document.getElementById("player-stop-btn");
        if (stopBtn && stopBtn.dataset.bound !== "1") {
            stopBtn.dataset.bound = "1";
            stopBtn.addEventListener("click", function () {
                if (!IS_ADMIN) return;
                if (socket) socket.emit("admin_playback_cmd", { action: "stop" });
            });
        }

        
        // Кастомный прогресс‑бар (YPlayer). Seek — только для админа.
        var bar = document.getElementById("yplayer-bar");
        if (bar && bar.dataset.bound !== "1") {
            bar.dataset.bound = "1";
            var seekFromClient = function (clientX) {
                var rect = bar.getBoundingClientRect();
                var ratio = (clientX - rect.left) / rect.width;
                ratio = Math.max(0, Math.min(1, ratio));
                var a2 = getSyncAudioEl();
                var dur = a2 && isFinite(a2.duration) ? a2.duration : 0;
                if (!dur) return;
                var targetMs = Math.floor(dur * ratio * 1000);
                if (IS_ADMIN && socket) socket.emit("admin_playback_cmd", { action: "seek", position_ms: targetMs });
                else {
                    // Не‑админ: только локально отображаем (без изменения синхры)
                    try { a2.currentTime = dur * ratio; } catch(e) {}
                }
            };

            bar.addEventListener("click", function (e) {
                seekFromClient(e.clientX);
            });

            bar.addEventListener("keydown", function (e) {
                var a2 = getSyncAudioEl();
                var dur = a2 && isFinite(a2.duration) ? a2.duration : 0;
                if (!dur) return;
                var step = 5; // seconds
                if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
                    e.preventDefault();
                    var next = a2.currentTime + (e.key === "ArrowRight" ? step : -step);
                    next = Math.max(0, Math.min(dur, next));
                    if (IS_ADMIN && socket) socket.emit("admin_playback_cmd", { action: "seek", position_ms: Math.floor(next * 1000) });
                }
            });
        }

        // Громкость — локально у клиента (не синхронизируем). Запоминаем в localStorage.
        var VOL_KEY = "antigaz_player_volume";
        var MUTED_KEY = "antigaz_player_muted";
        var vol = document.getElementById("yplayer-vol");
        var mute = document.getElementById("yplayer-mute-btn");

        function setMuteUi(isMuted) {
            if (!mute) return;
            // используем текстовые иконки, чтобы работало одинаково везде
            mute.textContent = isMuted ? "🔇" : "🔊";
            mute.classList.toggle("is-muted", !!isMuted);
        }

        function setVolUi(v) {
            if (!vol) return;
            // input range у нас 0..1
            vol.value = String(Math.max(0, Math.min(1, v)));
        }

        function restoreVolume() {
            var a2 = getSyncAudioEl();
            if (!a2) return;
            try {
                var savedVol = localStorage.getItem(VOL_KEY);
                if (savedVol !== null && savedVol !== "") {
                    var v = Number(savedVol);
                    if (!isNaN(v)) a2.volume = Math.max(0, Math.min(1, v));
                }
                var savedMuted = localStorage.getItem(MUTED_KEY);
                if (savedMuted === "1") a2.muted = true;
                if (savedMuted === "0") a2.muted = false;
            } catch (e) {}
            setVolUi(a2.volume);
            setMuteUi(a2.muted);
        }
        restoreVolume();

        function persistVol(a2) {
            try { localStorage.setItem(VOL_KEY, String(a2.volume)); } catch (e) {}
        }
        function persistMuted(a2) {
            try { localStorage.setItem(MUTED_KEY, a2.muted ? "1" : "0"); } catch (e) {}
        }

        if (vol && vol.dataset.bound !== "1") {
            vol.dataset.bound = "1";
            var onVol = function () {
                var a2 = getSyncAudioEl();
                if (!a2) return;
                var v = Number(vol.value);
                if (!isNaN(v)) a2.volume = Math.max(0, Math.min(1, v));
                // если подняли громкость — размьютим
                if (a2.volume > 0 && a2.muted) {
                    a2.muted = false;
                    persistMuted(a2);
                }
                persistVol(a2);
                setMuteUi(a2.muted);
            };
            vol.addEventListener("input", onVol);
            vol.addEventListener("change", onVol);
        }

        if (mute && mute.dataset.bound !== "1") {
            mute.dataset.bound = "1";
            var lastPointerMuteTs = 0;
            var onMute = function () {
                var a2 = getSyncAudioEl();
                if (!a2) return;
                a2.muted = !a2.muted;
                persistMuted(a2);
                setMuteUi(a2.muted);
            };
            mute.addEventListener("click", function (e) {
                // If a pointer handler already toggled mute, ignore the subsequent click (prevents double-toggle).
                if (lastPointerMuteTs && (Date.now() - lastPointerMuteTs) < 600) return;
                onMute();
            });
            // On touch devices, pointerdown feels snappier, but it also triggers a click afterwards.
            mute.addEventListener("pointerdown", function (e) {
                lastPointerMuteTs = Date.now();
                e.preventDefault();
                onMute();
            });
        }

function formatTime(sec) {
            sec = Math.max(0, Math.floor(sec || 0));
            var m = Math.floor(sec / 60);
            var s = sec % 60;
            return m + ":" + (s < 10 ? "0" + s : s);
        }

        function updateYPlayerUI() {
            var a2 = getSyncAudioEl();
            if (!a2) return;
            var curEl = document.getElementById("yplayer-time-current");
            var totEl = document.getElementById("yplayer-time-total");
            var fill = document.getElementById("yplayer-bar-fill");
            var handle = document.getElementById("yplayer-bar-handle");
            var bar = document.getElementById("yplayer-bar");

            var cur = a2.currentTime || 0;
            var dur = isFinite(a2.duration) ? a2.duration : 0;

            if (curEl) curEl.textContent = formatTime(cur);
            if (totEl) totEl.textContent = dur ? formatTime(dur) : "0:00";

            var ratio = dur ? (cur / dur) : 0;
            ratio = Math.max(0, Math.min(1, ratio));

            if (fill) fill.style.width = (ratio * 100) + "%";
            if (handle) handle.style.left = (ratio * 100) + "%";
            if (bar) bar.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
        }

        a.addEventListener("timeupdate", updateYPlayerUI);
        a.addEventListener("loadedmetadata", updateYPlayerUI);
        a.addEventListener("durationchange", updateYPlayerUI);

// Управление прямо с аудио‑контролов (seek/play/pause) — только для админа
        a.addEventListener("play", function () {
            if (!socket || applyingRemoteAudio) return;
            socket.emit("admin_playback_cmd", { action: "play" });
        });
        a.addEventListener("pause", function () {
            if (!socket || applyingRemoteAudio) return;
            // ended тоже триггерит pause — не страшно, пусть будет pause
            socket.emit("admin_playback_cmd", { action: "pause" });
        });
        a.addEventListener("seeked", function () {
            if (!socket || applyingRemoteAudio) return;
            var ms = Math.round((Number(a.currentTime) || 0) * 1000);
            socket.emit("admin_playback_cmd", { action: "seek", position_ms: ms });
        });
        a.addEventListener("ended", function () {
            if (!socket || applyingRemoteAudio) return;
            socket.emit("admin_playback_cmd", { action: "stop" });
        });
    }



    // --- Kick button delegation (capture) ---
    // IMPORTANT:
    // Some parts of the UI attach navigation handlers on pointerdown/mousedown
    // (e.g. clicking a panel/track navigates). In that case a normal "click" handler
    // never fires. We therefore intercept pointerdown/mousedown in capture phase.
    function bindKickDelegationOnce() {
        if (window.__KICK_DELEGATION_BOUND__) return;
        window.__KICK_DELEGATION_BOUND__ = true;

        function handleKickEvent(e) {
            var btn = e.target && e.target.closest ? e.target.closest(".btn-kick-rater") : null;
            if (!btn) return;

            // Capture phase: ensure we receive event even if other handlers stop it.
            try {
                e.preventDefault();
                e.stopPropagation();
                if (typeof e.stopImmediatePropagation === "function") e.stopImmediatePropagation();
            } catch (err) {}

            // Delegate to a single global implementation.
            if (window.kickRaterFromBtn) {
                window.kickRaterFromBtn(btn, e);
            }
        }

        // Intercept early (before click), otherwise parent pointerdown navigation can swallow it.
        document.addEventListener("pointerdown", handleKickEvent, true);
        document.addEventListener("mousedown", handleKickEvent, true);
        document.addEventListener("click", handleKickEvent, true);
    }

    // Inline onclick fallback.
    // In some layouts (especially with nested overlays / draggable headers),
    // delegated listeners could be swallowed by other scripts. Inline handler
    // ensures the click always triggers.
    function kickRaterFromBtn(btn, e) {
        if (!btn) return;
        try {
            if (e) {
                e.preventDefault();
                e.stopPropagation();
                if (typeof e.stopImmediatePropagation === "function") e.stopImmediatePropagation();
            }
        } catch (err) {}
        // NOTE: we always emit; server enforces admin rights.
        // This avoids "silent" failures when template flags are missing.
        var raterId = btn.getAttribute("data-rater-id") || null;
        var userId = btn.getAttribute("data-user-id") || null;
        var ok = confirm("Кикнуть оценщика и убрать его панель?");
        if (!ok) return;
        var s = window.__APP_SOCKET__ || socket;
        if (!s) {
            try { toast("Socket не подключён"); } catch (err) {}
            return;
        }
        try { console.log("[kick] emit", { user_id: userId, rater_id: raterId }); } catch (err) {}
        s.emit("kick_rater", { user_id: userId, rater_id: raterId });
    }

    try { window.kickRaterFromBtn = kickRaterFromBtn; } catch (e) {}

function initSocket() {
        if (typeof io === "undefined") {
            console.error("Socket.IO script not loaded");
            return;
        }
        socket = io();
        try { window.__APP_SOCKET__ = socket; } catch(e) {}

        socket.on("connect", function () {
    console.log("[socket] connected");
    socket.emit("request_initial_state");
    // Join/leave panel room (observers get synced state only while on panel)
    if (isPanelPage) {
        socket.emit("enter_panel");
    } else {
        socket.emit("leave_panel");
    }
});
socket.on("connect_error", function (err) {
            console.error("[socket] connect_error", err);
        });

        function refreshRatingButtons() {
            var joinBtn = document.getElementById("join-rating-btn");
            var leaveBtn = document.getElementById("leave-rating-btn");
            if (joinBtn) joinBtn.style.display = window.__IN_RATING__ ? "none" : "inline-flex";
            if (leaveBtn) leaveBtn.style.display = window.__IN_RATING__ ? "inline-flex" : "none";
        }

        socket.on("rating_joined", function (payload) {
            window.__IN_RATING__ = true;
            try { window.__MY_RATER_ID__ = payload && payload.rater_id ? String(payload.rater_id) : null; } catch (e) {}
            try { window.__MY_USER_ID__ = payload && payload.user_id ? String(payload.user_id) : null; } catch (e) {}
            refreshRatingButtons();
            if (window.updateEditablePanels) window.updateEditablePanels();
            if (window.updateKickButtonTargets) window.updateKickButtonTargets();
        });

        socket.on("rating_left", function () {
            window.__IN_RATING__ = false;
            try { window.__MY_RATER_ID__ = null; } catch (e) {}
            try { window.__MY_USER_ID__ = null; } catch (e) {}
            stopSyncAudio();
            refreshRatingButtons();
        });

        socket.on("kicked", function () {
            window.__IN_RATING__ = false;
            try { window.__MY_RATER_ID__ = null; } catch (e) {}
            try { window.__MY_USER_ID__ = null; } catch (e) {}
            stopSyncAudio();
            refreshRatingButtons();
            alert("Вас кикнули из оценки");
        });

        socket.on("kick_result", function (payload) {
            try { console.log("[kick] result", payload); } catch (err) {}
            var ok = payload && payload.ok;
            var msg = (payload && payload.msg) || (ok ? "ok" : "fail");
            try {
                if (ok) toast("Кик выполнен");
                else if (msg === "not_admin") toast("Нет прав (только админ)");
                else if (msg === "not_found") toast("Оценщик не найден");
                else if (msg === "no_target") toast("Некорректная цель");
                else toast("Кик не выполнен");
            } catch (err) {}
        });

        socket.on("raters_presence_updated", function (payload) {
            try {
                var list = (payload && payload.raters) ? payload.raters : [];
                list.forEach(function (r) {
                    if (r && r.rater_id && r.user_id != null) {
                        window.__RATER_USER_MAP__[String(r.rater_id)] = String(r.user_id);
                    }
                });
            } catch (e) {}

            // Update UI without full re-render (avoids re-playing "appear" animations).
            refreshRatingButtons();
            if (window.updateKickButtonTargets) window.updateKickButtonTargets();
            if (window.updateEditablePanels) window.updateEditablePanels();
        });


        socket.on("initial_state", function (payload) {
            state.track_name = payload.track_name || "";
            state.criteria = payload.criteria || [];
            state.raters = {};
            (payload.raters || []).forEach(function (r) {
                state.raters[r.id] = r;
                if (r && (r.user_id == null) && window.__RATER_USER_MAP__ && r.id && window.__RATER_USER_MAP__[String(r.id)]) {
                    r.user_id = window.__RATER_USER_MAP__[String(r.id)];
                }
            });

            var trackInput = document.getElementById("track-name-input");
            if (trackInput) {
                trackInput.value = state.track_name || "";
            }
            updateTrackNameDisplays(state.track_name);
            renderAllPanels();
            if (window.updateEditablePanels) window.updateEditablePanels();
            if (window.updateKickButtonTargets) window.updateKickButtonTargets();
        });

        socket.on("queue_state", function (payload) {
            renderQueueState(payload);
            // On a hard reload the server can emit `playback_state` before `queue_state`.
            // If `playback_state` arrives without `active` meta, the player may not attach
            // the correct src until we re-apply after queue meta becomes available.
            try {
                if (playbackState && (!playbackState.active || !playbackState.active.audio_url) && payload && payload.active) {
                    applyPlaybackState(playbackState);
                }
            } catch (e) {}
        });

        socket.on("playback_state", function (payload) {
            applyPlaybackState(payload);
        });

        socket.on("track_name_changed", function (payload) {
            state.track_name = (payload && payload.track_name) || "";
            var trackInput = document.getElementById("track-name-input");
            if (trackInput && trackInput !== document.activeElement) {
                trackInput.value = state.track_name;
            }
            updateTrackNameDisplays(state.track_name);
        });

        socket.on("rater_name_changed", function (payload) {
            if (!payload) return;
            var raterId = payload.rater_id;
            var name = payload.name;
            var rater = state.raters[raterId];
            if (rater) {
                rater.name = name;
            }
            var panel = document.querySelector('.rating-panel[data-rater-id="' + raterId + '"]');
            if (panel) {
                var input = panel.querySelector(".rater-name-input");
                if (input && input !== document.activeElement) {
                    input.value = name;
                }
            }
        });

        socket.on("slider_updated", function (payload) {
            if (!payload) return;
            var raterId = payload.rater_id;
            var key = payload.criterion_key;
            var value = Number(payload.value) || 0;

            var rater = state.raters[raterId];
            if (!rater) return;
            if (!rater.scores) {
                rater.scores = {};
            }
            rater.scores[key] = value;

            var panel = document.querySelector('.rating-panel[data-rater-id="' + raterId + '"]');
            if (!panel) {
                return;
            }
            var slider = panel.querySelector('.score-slider[data-criterion-key="' + key + '"]');
            if (slider) {
                slider.value = String(value);
                applyHeatToSlider(slider, value);
                var valueBox = slider.parentElement.querySelector("[data-slider-value]");
                if (valueBox) {
                    valueBox.textContent = String(value);
                    applyHeatToChip(valueBox, value);
                }
            }

            computeAndRenderTotalsFromState();
        });

        socket.on("rater_added", function (payload) {
            if (!payload || !payload.rater) return;
            var r = payload.rater;
            state.raters[r.id] = r;
            renderAllPanels();
        });

        socket.on("rater_removed", function (payload) {
            if (!payload) return;
            var raterId = payload.rater_id;
            delete state.raters[raterId];
            var panel = document.querySelector('.rating-panel[data-rater-id="' + raterId + '"]');
            if (panel && panel.parentElement) {
                panel.parentElement.removeChild(panel);
            }
            computeAndRenderTotalsFromState();
        });

        socket.on("evaluation_result", function (payload) {
            if (!payload) return;
            computeAndRenderTotalsFromState();
            openResultModal(payload);
        });

        socket.on("state_reset", function (payload) {
            state.track_name = payload.track_name || "";
            state.criteria = payload.criteria || state.criteria;
            state.raters = {};
            (payload.raters || []).forEach(function (r) {
                state.raters[r.id] = r;
            });

            var trackInput = document.getElementById("track-name-input");
            if (trackInput) {
                trackInput.value = "";
            }
            updateTrackNameDisplays("");

            renderAllPanels();
        });
    }

    
    function initBackgroundRain() {
        var layer = document.getElementById("bg-rain-layer");
        if (!layer) return;

        // Turbo navigation triggers TrackRaterRebind() on every page visit.
        // Without a guard we would create multiple intervals and the rain would duplicate.
        if (layer.__rainInited) return;
        layer.__rainInited = true;

        var ICON_TYPES = ["logo", "frog", "text"];

        function spawnIcon() {
            if (!layer) return;

            var el = document.createElement("div");
            el.classList.add("rain-icon");

            var inner = document.createElement("div");
            inner.classList.add("rain-icon-inner");

            var t = ICON_TYPES[Math.floor(Math.random() * ICON_TYPES.length)];
            inner.classList.add("rain-icon--" + t);

            if (t === "text") {
                inner.textContent = "ANTIGAZ";
            }

            var left = Math.random() * 100;
            el.style.left = left + "vw";

            var size = 48 + Math.random() * 96; // 48–144px
            el.style.width = size + "px";
            el.style.height = size + "px";

            var duration = 9 + Math.random() * 3;
            el.style.setProperty("--duration", duration + "s");
            el.style.animationDuration = duration + "s";

            var rot = (Math.random() * 80 - 40).toFixed(1);
            inner.style.setProperty("--rot", rot + "deg");

            el.appendChild(inner);
            layer.appendChild(el);

            function cleanup() {
                if (el && el.parentNode) {
                    el.parentNode.removeChild(el);
                }
            }
            el.addEventListener("animationend", cleanup);
            setTimeout(cleanup, (duration + 5) * 1000);
        }

        for (var i = 0; i < 10; i++) {
            setTimeout(spawnIcon, i * 400);
        }

        setInterval(spawnIcon, 900);
    }
function initTopPage() {
        var tbody = document.getElementById("top-table-body");
        if (!tbody) {
            return; // не на странице топа
        }

        // Prevent double-binding when navigating with Turbo.
        if (tbody.__topInited) {
            return;
        }
        tbody.__topInited = true;

        // Клик по строке открывает модалку, но игнорируем клики по админ-кнопкам и ссылкам
        tbody.addEventListener("click", function (evt) {
            if (evt.target.closest(".top-action-btn")) {
                // админские действия обрабатываются отдельно
                return;
            }
            if (evt.target.closest("a")) {
                // по ссылкам (например, на страницу трека) даём сработать переходу
                return;
            }
            var row = evt.target.closest(".top-row");
            if (!row) return;
            var trackId = row.getAttribute("data-track-id");
            if (!trackId) return;
            openTrackDetailsModal(trackId);
        });

        // Админ: обработчики переименования и удаления
        var renameButtons = document.querySelectorAll(".top-action-rename");
        renameButtons.forEach(function (btn) {
            if (btn.__topInited) return;
            btn.__topInited = true;
            btn.addEventListener("click", function (evt) {
                evt.stopPropagation();
                var row = btn.closest(".top-row");
                if (!row) return;
                var trackId = row.getAttribute("data-track-id");
                if (!trackId) return;
                var nameSpan = row.querySelector(".top-name-text");
                var currentName = (nameSpan ? nameSpan.textContent : btn.getAttribute("data-track-name") || "").trim();
                var newName = prompt("Новое название трека:", currentName);
                if (!newName) return;
                newName = newName.trim();
                if (!newName || newName === currentName) return;

                fetch("/admin/tracks/" + trackId + "/rename", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify({ name: newName })
                })
                    .then(function (resp) { return resp.json(); })
                    .then(function (data) {
                        if (!data || !data.success) {
                            alert("Ошибка при переименовании трека.");
                            return;
                        }
                        if (nameSpan) {
                            nameSpan.textContent = data.name;
                        }
                    })
                    .catch(function () {
                        alert("Ошибка сети при переименовании трека.");
                    });
            });
        });

        var deleteButtons = document.querySelectorAll(".top-action-delete");
        deleteButtons.forEach(function (btn) {
            if (btn.__topInited) return;
            btn.__topInited = true;
            btn.addEventListener("click", function (evt) {
                evt.stopPropagation();
                var row = btn.closest(".top-row");
                if (!row) return;
                var trackId = row.getAttribute("data-track-id");
                if (!trackId) return;
                var nameSpan = row.querySelector(".top-name-text");
                var currentName = (nameSpan ? nameSpan.textContent : btn.getAttribute("data-track-name") || "").trim();
                var ok = confirm("Удалить трек \"" + currentName + "\" из топа?\nТрек будет скрыт, но останется в базе.");
                if (!ok) return;

                fetch("/admin/tracks/" + trackId + "/delete", {
                    method: "POST"
                })
                    .then(function (resp) { return resp.json(); })
                    .then(function (data) {
                        if (!data || !data.success) {
                            alert("Ошибка при удалении трека.");
                            return;
                        }
                        // Удаляем строку из таблицы или перезагружаем страницу
                        if (row && row.parentNode) {
                            row.parentNode.removeChild(row);
                        } else {
                            window.location.reload();
                        }
                    })
                    .catch(function () {
                        alert("Ошибка сети при удалении трека.");
                    });
            });
        });

        if (typeof applyHeatToChip === "function") {
            var chips = document.querySelectorAll(".top-score-chip.score-chip");
            chips.forEach(function (chip) {
                var raw = chip.textContent.trim().replace(",", ".");
                var val = parseFloat(raw);
                if (!isNaN(val)) {
                    applyHeatToChip(chip, val);
                }
            });
        }
    }
function openTrackDetailsModal(trackId) {
        // Track details modal for Top/Mini-top ("Информация по треку")
        // NOTE: This is NOT the rating/result modal.
        var backdrop = document.getElementById("track-modal-backdrop");
        if (!backdrop) {
            // Fallback: if a page doesn't have the track details modal, open the track page.
            window.location.href = "/track/" + trackId;
            return;
        }

        fetch("/api/track/" + trackId + "/summary")
            .then(function (resp) {
                if (!resp.ok) throw new Error("failed");
                return resp.json();
            })
            .then(function (data) {
                var trackName = (data && data.track && data.track.name) ? data.track.name : "Без названия";
                var criteria = (data && Array.isArray(data.criteria)) ? data.criteria : [];
                var raters = (data && Array.isArray(data.raters)) ? data.raters : [];
                var viewerCriteria = (data && Array.isArray(data.viewer_criteria)) ? data.viewer_criteria : [];

                var subtitle = document.getElementById("track-modal-subtitle");
                if (subtitle) subtitle.textContent = "Трек: " + trackName;

                var bodyA = document.getElementById("track-modal-criteria-body");
                if (bodyA) {
                    bodyA.innerHTML = "";
                    criteria.forEach(function (c) {
                        var tr = document.createElement("tr");
                        var td1 = document.createElement("td");
                        td1.textContent = c.label || criterionLabelFromKey(c.key);
                        var td2 = document.createElement("td");
                        var chip = document.createElement("span");
                        chip.className = "score-chip";
                        chip.textContent = (typeof c.avg === "number") ? c.avg.toFixed(2) : "—";
                        td2.appendChild(chip);
                        tr.appendChild(td1);
                        tr.appendChild(td2);
                        bodyA.appendChild(tr);
                        if (typeof applyHeatToChip === "function" && typeof c.avg === "number") {
                            try { applyHeatToChip(chip, c.avg); } catch (e) {}
                        }
                    });
                }

                var bodyV = document.getElementById("track-modal-viewers-criteria-body");
                if (bodyV) {
                    bodyV.innerHTML = "";
                    viewerCriteria.forEach(function (c) {
                        var tr = document.createElement("tr");
                        var td1 = document.createElement("td");
                        td1.textContent = c.label || criterionLabelFromKey(c.key);
                        var td2 = document.createElement("td");
                        var chip = document.createElement("span");
                        chip.className = "score-chip";
                        chip.textContent = (typeof c.avg === "number") ? c.avg.toFixed(2) : "—";
                        td2.appendChild(chip);
                        tr.appendChild(td1);
                        tr.appendChild(td2);
                        bodyV.appendChild(tr);
                        if (typeof applyHeatToChip === "function" && typeof c.avg === "number") {
                            try { applyHeatToChip(chip, c.avg); } catch (e) {}
                        }
                    });
                }

                var bodyR = document.getElementById("track-modal-raters-body");
                if (bodyR) {
                    bodyR.innerHTML = "";
                    raters.forEach(function (r) {
                        var tr = document.createElement("tr");
                        var td1 = document.createElement("td");
                        td1.textContent = r.name || "—";
                        var td2 = document.createElement("td");
                        var chip = document.createElement("span");
                        chip.className = "score-chip";
                        chip.textContent = (typeof r.avg === "number") ? r.avg.toFixed(2) : "—";
                        td2.appendChild(chip);
                        tr.appendChild(td1);
                        tr.appendChild(td2);
                        bodyR.appendChild(tr);
                        if (typeof applyHeatToChip === "function" && typeof r.avg === "number") {
                            try { applyHeatToChip(chip, r.avg); } catch (e) {}
                        }
                    });
                }

                var overall = document.getElementById("track-modal-overall");
                if (overall) {
                    var v = (typeof data.overall_avg === "number") ? data.overall_avg : null;
                    overall.textContent = (v != null) ? v.toFixed(2) : "—";
                    if (typeof applyHeatToChip === "function" && v != null) {
                        try { applyHeatToChip(overall, v); } catch (e) {}
                    }
                }

                var vOverall = document.getElementById("track-modal-viewers-overall");
                if (vOverall) {
                    var vv = (typeof data.viewer_overall_avg === "number") ? data.viewer_overall_avg : null;
                    vOverall.textContent = (vv != null) ? vv.toFixed(2) : "—";
                    if (typeof applyHeatToChip === "function" && vv != null) {
                        try { applyHeatToChip(vOverall, vv); } catch (e) {}
                    }
                }

                var openLink = document.getElementById("track-modal-open-page");
                if (openLink) openLink.setAttribute("href", "/track/" + trackId);

                backdrop.classList.add("is-open");
            })
            .catch(function () {
                try { showToast("Не удалось загрузить информацию по треку", "error"); } catch (e) {}
            });
}

    function initTrackDetailsModalHandlers() {
        var backdrop = document.getElementById("track-modal-backdrop");
        if (!backdrop) return;
        if (backdrop.dataset.bound === "1") return;
        backdrop.dataset.bound = "1";

        var closeBtn = document.getElementById("track-modal-close");
        var openLink = document.getElementById("track-modal-open-page");

        function close() {
            backdrop.classList.remove("is-open");
        }

        if (closeBtn) {
            closeBtn.addEventListener("click", function (e) {
                e.preventDefault();
                close();
            });
        }

        // Clicking outside the card closes the modal
        backdrop.addEventListener("click", function (e) {
            if (e.target === backdrop) close();
        });

        // Let the link behave normally but close the modal if user clicks it
        if (openLink) {
            openLink.addEventListener("click", function () {
                close();
            });
        }

        // ESC to close (bind once per page)
        if (!document.documentElement.dataset.trackModalEscBound) {
            document.documentElement.dataset.trackModalEscBound = "1";
            document.addEventListener("keydown", function (e) {
                if (e.key === "Escape") {
                    var b = document.getElementById("track-modal-backdrop");
                    if (b && b.classList.contains("is-open")) {
                        b.classList.remove("is-open");
                    }
                }
            });
        }
    }

    function initAdminTabs() {
        // Admin page tabs: navigate via /admin?tab=...
        var tabsBar = document.querySelector(".admin-tabs");
        if (!tabsBar) return;

        var buttons = tabsBar.querySelectorAll("[data-admin-tab]");
        buttons.forEach(function (btn) {
            if (btn.dataset.bound) return;
            btn.dataset.bound = "1";
            btn.addEventListener("click", function () {
                var tab = btn.getAttribute("data-admin-tab");
                if (!tab) return;
                var url = "/admin?tab=" + encodeURIComponent(tab);
                try {
                    if (window.Turbo && typeof window.Turbo.visit === "function") {
                        window.Turbo.visit(url);
                    } else {
                        window.location.href = url;
                    }
                } catch (e) {
                    window.location.href = url;
                }
            });
        });
    }


    function TrackRaterRebind() {
        // Refresh privilege flags after Turbo navigation.
        try {
            isAdmin = !!(window && window.__IS_ADMIN__);
            canQueueModerate = (!!(window && window.__IS_JUDGE__)) || isAdmin;
        } catch (e) {}
        isPanelPage = !!document.getElementById("queue-panel");
        isQueuePublicPage = !!document.getElementById("queue-public-page");

        // If the auth state changed (login/logout) during a Turbo navigation,
        // the existing Socket.IO connection can still be tied to the previous
        // session cookie. A simple disconnect/connect is sometimes not enough
        // (Socket.IO can reuse the underlying manager/transport). We therefore
        // recreate the socket instance on auth changes.
        try {
            var cu = (document.body && document.body.dataset) ? (document.body.dataset.currentUser || "") : "";
            var sv = (document.body && document.body.dataset) ? (document.body.dataset.sessionVersion || "") : "";
            var sig = String(cu) + "|" + String(sv);
            if (typeof window !== "undefined") {
                if (window.__TR_LAST_AUTH_SIG__ != null && window.__TR_LAST_AUTH_SIG__ !== sig) {
                    // Fully recreate socket (more reliable than disconnect/connect)
                    if (socket) {
                        try { socket.removeAllListeners(); } catch (e) {}
                        try { socket.disconnect(); } catch (e) {}
                    }
                    socket = null;
                    try { window.__APP_SOCKET__ = null; } catch (e) {}
                    socketInited = false;
                }
                window.__TR_LAST_AUTH_SIG__ = sig;
            }
        } catch (e) {}
        if (window.INITIAL_STATE) {
            state.track_name = window.INITIAL_STATE.track_name || "";
            state.raters = {};
            (window.INITIAL_STATE.raters || []).forEach(function (r) {
                state.raters[r.id] = r;
            });
            state.criteria = window.INITIAL_STATE.criteria || [];
            updateTrackNameDisplays(state.track_name);
            renderAllPanels();
        }

        initTrackInput();
        initControls();
        initModalHandlers();
        bindKickDelegationOnce();
        initImageLightbox();
        // Update panel room membership on navigation.
        try {
            if (socket) {
                if (isPanelPage) socket.emit("enter_panel");
                else socket.emit("leave_panel");
            }
        } catch (e) {}
        // Create (or recreate) the Socket.IO connection.
        if (!socketInited) {
            socketInited = true;
            initSocket();
        }

        // Публичная очередь (/queue) обновляется через JSON‑API,
        // чтобы статус "конвертируется" переходил в "в очереди" без перезагрузки.
        // Public queue polling (/queue)
        if (isQueuePublicPage) {
            try {
                if (!queuePublicPollIntervalId) {
                    queuePublicPollIntervalId = setInterval(function () {
                        fetch("/api/queue", { credentials: "same-origin" })
                            .then(function (r) { return r.json(); })
                            .then(function (payload) {
                                if (!payload) return;
                                renderQueueState(payload);
                                // обновим "Сейчас играет" на публичной странице
                                var cur = document.getElementById("queue-current-value");
                                if (cur) {
                                    cur.textContent = (payload.active && payload.active.display_name) ? payload.active.display_name : "—";
                                }
                            })
                            .catch(function () { });
                    }, 2000);
                }
            } catch (e) { }
        } else {
            // Stop polling when leaving /queue
            if (queuePublicPollIntervalId) {
                try { clearInterval(queuePublicPollIntervalId); } catch (e) {}
                queuePublicPollIntervalId = null;
            }
        }
        initPlaybackControls();
        initBackgroundRain();
        initTopPage();
        try { applyHeatToAllScoreChips(document); } catch (e) {}
        initTrackDetailsModalHandlers();
        initAdminTabs();
        try { if (window.initYplayerEmbeds) window.initYplayerEmbeds(); } catch (e) {}
    }


    // Hotwire Turbo-safe boot:
    // turbo:load fires on initial page load and after every Turbo navigation.
    document.addEventListener("turbo:load", function () {
        TrackRaterRebind();
        try { window.TrackRaterRebind = TrackRaterRebind; } catch (e) {}
    });


    // Сделаем openTrackDetailsModal доступной глобально для inline onclick на главной
    if (typeof window !== "undefined") {
        window.openTrackDetailsModal = openTrackDetailsModal;
    }


})();