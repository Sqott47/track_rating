(function () {
    var socket = null;

    var state = {
        track_name: "",
        raters: {},
        criteria: []
    };

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

        var inner = document.createElement("div");
        inner.className = "panel-inner";
        panel.appendChild(inner);

        var header = document.createElement("div");
        header.className = "panel-header";
        inner.appendChild(header);

        var headerTop = document.createElement("div");
        headerTop.className = "panel-header-top";
        header.appendChild(headerTop);


        var removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "panel-remove-btn";
        removeBtn.textContent = "×";
        removeBtn.title = "Удалить оценщика";
        headerTop.appendChild(removeBtn);

        removeBtn.addEventListener("click", function () {
            if (socket) {
                socket.emit("remove_rater", { rater_id: rater.id });
            }
        });

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

        rnInput.addEventListener("change", function () {
            if (socket) {
                socket.emit("change_rater_name", {
                    rater_id: rater.id,
                    name: rnInput.value
                });
            }
        });

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

            slider.addEventListener("input", function () {
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
        var addBtn = document.getElementById("add-rater-btn");
        if (addBtn) {
            addBtn.addEventListener("click", function () {
                if (socket) {
                    socket.emit("add_rater");
                }
            });
        }

        var evalBtn = document.getElementById("evaluate-btn");
        if (evalBtn) {
            evalBtn.addEventListener("click", function () {
                if (socket) {
                    socket.emit("evaluate");
                }
            });
        }

        var newTrackBtn = document.getElementById("new-track-btn");
        if (newTrackBtn) {
            newTrackBtn.addEventListener("click", function () {
                if (socket) {
                    socket.emit("reset_state");
                }
            });
        }
    }


function initSocket() {
        if (typeof io === "undefined") {
            console.error("Socket.IO script not loaded");
            return;
        }
        socket = io();

        socket.on("connect", function () {
            console.log("[socket] connected");
            socket.emit("request_initial_state");
        });

        socket.on("connect_error", function (err) {
            console.error("[socket] connect_error", err);
        });

        socket.on("initial_state", function (payload) {
            state.track_name = payload.track_name || "";
            state.criteria = payload.criteria || [];
            state.raters = {};
            (payload.raters || []).forEach(function (r) {
                state.raters[r.id] = r;
            });

            var trackInput = document.getElementById("track-name-input");
            if (trackInput) {
                trackInput.value = state.track_name || "";
            }
            updateTrackNameDisplays(state.track_name);
            renderAllPanels();
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

        var ICON_TYPES = ["logo", "frog", "text"];

        function spawnIcon() {
            if (!layer) return;

            // контейнер, который падает вниз
            var el = document.createElement("div");
            el.classList.add("rain-icon");

            // внутренний элемент, который несёт на себе картинку / текст и поворот
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

            // более медленное падение: ~9–12 секунд на весь экран
            var duration = 9 + Math.random() * 3;
            el.style.setProperty("--duration", duration + "s");
            el.style.animationDuration = duration + "s";

            // случайный наклон элемента от -40 до 40 градусов
            var rot = (Math.random() * 80 - 40).toFixed(1);
            inner.style.setProperty("--rot", rot + "deg");

            el.appendChild(inner);
            layer.appendChild(el);

            // удаляем элемент по окончанию анимации, чтобы он не пропадал посередине
            function cleanup() {
                if (el && el.parentNode) {
                    el.parentNode.removeChild(el);
                }
            }
            el.addEventListener("animationend", cleanup);

            // запасной таймер на случай, если animationend не сработает
            setTimeout(cleanup, (duration + 5) * 1000);
        }
// начальное заполнение — побольше элементов сразу
        for (var i = 0; i < 10; i++) {
            setTimeout(spawnIcon, i * 400);
        }

        // далее — новые элементы примерно раз в 0.9 секунды,
        // чтобы на экране почти всегда было 6–10+ иконок
        setInterval(spawnIcon, 900);
    }


    
    
function initTopPage() {
        var tbody = document.getElementById("top-table-body");
        if (!tbody) {
            return; // не на странице топа
        }

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
        fetch("/api/track/" + trackId + "/summary")
            .then(function (resp) {
                if (!resp.ok) throw new Error("failed");
                return resp.json();
            })
            .then(function (data) {
                var backdrop = document.getElementById("track-modal-backdrop");
                if (!backdrop) return;

                var subtitle = document.getElementById("track-modal-subtitle");
                var tbodyCriteria = document.getElementById("track-modal-criteria-body");
                var tbodyRaters = document.getElementById("track-modal-raters-body");
                var overallEl = document.getElementById("track-modal-overall");

                if (subtitle && data.track) {
                    var name = data.track.name || "Без названия";
                    subtitle.textContent = "Трек: " + name;
                }

                if (tbodyCriteria) {
                    tbodyCriteria.innerHTML = "";
                    (data.criteria || []).forEach(function (c) {
                        var tr = document.createElement("tr");
                        var tdName = document.createElement("td");
                        var tdScore = document.createElement("td");

                        tdName.textContent = criterionLabelFromKey(c.key);
                        tdScore.textContent = (c.avg != null ? c.avg.toFixed(2) : "?");

                        tr.appendChild(tdName);
                        tr.appendChild(tdScore);
                        tbodyCriteria.appendChild(tr);
                    });
                }

                if (tbodyRaters) {
                    tbodyRaters.innerHTML = "";
                    (data.raters || []).forEach(function (r) {
                        var tr = document.createElement("tr");
                        var tdName = document.createElement("td");
                        var tdScore = document.createElement("td");

                        tdName.textContent = r.name;
                        tdScore.textContent = (r.avg != null ? r.avg.toFixed(2) : "?");

                        tr.appendChild(tdName);
                        tr.appendChild(tdScore);
                        tbodyRaters.appendChild(tr);
                    });
                }

                if (overallEl) {
                    var overall = data.overall_avg;
                    if (overall != null) {
                        overallEl.textContent = overall.toFixed(2);
                        if (typeof applyHeatToChip === "function") {
                            applyHeatToChip(overallEl, overall);
                        }
                    } else {
                        overallEl.textContent = "?";
                        if (overallEl.classList) {
                            overallEl.classList.remove("score-chip--flame", "score-chip--hot");
                        }
                    }
                }


                var openPageBtn = document.getElementById("track-modal-open-page");
                if (openPageBtn) {
                    openPageBtn.setAttribute("href", "/track/" + trackId);
                }

                // зрители: таблица и общий балл
                var tbodyViewersCriteria = document.getElementById("track-modal-viewers-criteria-body");
                var viewersOverallEl = document.getElementById("track-modal-viewers-overall");

                if (tbodyViewersCriteria) {
                    tbodyViewersCriteria.innerHTML = "";
                    (data.viewer_criteria || []).forEach(function (c) {
                        var tr = document.createElement("tr");
                        var tdName = document.createElement("td");
                        var tdScore = document.createElement("td");

                        tdName.textContent = criterionLabelFromKey(c.key);
                        if (c.avg != null) {
                            tdScore.textContent = c.avg.toFixed(2);
                        } else {
                            tdScore.textContent = "?";
                        }

                        tr.appendChild(tdName);
                        tr.appendChild(tdScore);
                        tbodyViewersCriteria.appendChild(tr);
                    });
                }

                if (viewersOverallEl) {
                    var vOverall = data.viewer_overall_avg;
                    if (vOverall != null) {
                        viewersOverallEl.textContent = vOverall.toFixed(2);
                        if (typeof applyHeatToChip === "function") {
                            applyHeatToChip(viewersOverallEl, vOverall);
                        }
                    } else {
                        viewersOverallEl.textContent = "?";
                        if (viewersOverallEl.classList) {
                            viewersOverallEl.classList.remove("score-chip--flame", "score-chip--hot");
                        }
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
            })
            .catch(function (err) {
                console.error("Failed to load track summary", err);
            });
    }

    

    function initTrackDetailsModalHandlers() {
        var backdrop = document.getElementById("track-modal-backdrop");
        var closeBtn = document.getElementById("track-modal-close");
        if (!backdrop) {
            return; // на этой странице модалки нет
        }

        function closeModal() {
            backdrop.classList.remove("is-open");
        }

        if (closeBtn) {
            closeBtn.addEventListener("click", function () {
                closeModal();
            });
        }

        backdrop.addEventListener("click", function (evt) {
            if (evt.target === backdrop) {
                closeModal();
            }
        });

        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" || e.key === "Esc") {
                if (backdrop.classList.contains("is-open")) {
                    closeModal();
                }
            }
        });
    }

function criterionLabelFromKey(key) {
        var map = {
            "rhyme": "Текст + Рифмы",
            "structure": "Структура + Ритмика",
            "style": "Реализация стиля + Жанра",
            "quality": "Качество + Сведение",
            "vibe": "Вайб + Общее впечатление"
        };
        return map[key] || key;
    }

document.addEventListener("DOMContentLoaded", function () {
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
        initSocket();
        initBackgroundRain();
        initTopPage();
        initTrackDetailsModalHandlers();
    });


    // Сделаем openTrackDetailsModal доступной глобально для inline onclick на главной
    if (typeof window !== "undefined") {
        window.openTrackDetailsModal = openTrackDetailsModal;
    }

    
    // Подкрашиваем все score-chip на странице карточки трека
    document.addEventListener("DOMContentLoaded", function () {
        var root = document.getElementById("track-page-root");
        if (!root || typeof applyHeatToChip !== "function") return;

        var chips = root.querySelectorAll(".score-chip");
        chips.forEach(function (chip) {
            var txt = (chip.textContent || "").replace(",", ".").trim();
            var val = parseFloat(txt);
            if (!isNaN(val)) {
                applyHeatToChip(chip, val);
            }
        });
    });

    
    // Подкрашиваем все score-chip на сайте + оборачиваем текст в .score-chip-label,
    // чтобы пламя было над чипом, но под цифрой
    document.addEventListener("DOMContentLoaded", function () {
        var chips = document.querySelectorAll(".score-chip");
        chips.forEach(function (chip) {
            // если цифра ещё не обёрнута во внутренний span — оборачиваем
            if (!chip.querySelector(".score-chip-label")) {
                var rawText = (chip.textContent || "").trim();
                chip.textContent = "";
                var inner = document.createElement("span");
                inner.className = "score-chip-label";
                inner.textContent = rawText;
                chip.appendChild(inner);
            }

            var label = chip.querySelector(".score-chip-label");
            var txt = (label && label.textContent ? label.textContent : chip.textContent || "")
                .replace(",", ".")
                .trim();
            var val = parseFloat(txt);
            if (!isNaN(val) && typeof applyHeatToChip === "function") {
                applyHeatToChip(chip, val);
            }
        });
    });


})();

