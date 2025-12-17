(function () {
    function $(sel) { return document.querySelector(sel); }
    function $all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

    var currentTrackId = null;
    var toastTimer = null;
    var alreadyRated = false;

    function clamp(value, min, max) {
        return Math.min(max, Math.max(min, value));
    }

    // те же цвета "от холода к жаре", что и у стримеров
    function heatColorForScore(score) {
        var v = clamp(Number(score) || 0, 0, 10);
        var t = v / 10;
        var startHue = 215;
        var endHue = 0;
        var hue = startHue + (endHue - startHue) * t;
        var sat = 68;
        var light = 50 - 4 * t;
        return "hsl(" + hue + ", " + sat + "%, " + light + "%)";
    }

    function applyHeatToSlider(slider, score) {
        if (!slider) return;
        var v = clamp(Number(score) || 0, 0, 10);
        var color = heatColorForScore(v);
        slider.style.background = "linear-gradient(90deg, " + color + ", rgba(15,23,42,0.95))";
        slider.style.boxShadow = v > 0 ? "0 0 " + (4 + v) + "px " + color : "none";

        if (v >= 9.95 && v <= 10.05) {
            slider.classList.add("frog-dj-mode");
        } else {
            slider.classList.remove("frog-dj-mode");
        }
    }

    function applyHeatToChip(el, score) {
        if (!el) return;
        var v = clamp(Number(score) || 0, 0, 10);
        var color = heatColorForScore(v);
        el.style.background = "linear-gradient(135deg, " + color + ", rgba(15,23,42,0.96))";
        el.style.color = v > 0 ? "#f9fafb" : "#e5e7eb";
        el.style.boxShadow = v > 0 ? "0 0 " + (3 + v * 0.8) + "px rgba(248,113,113,0.45)" : "none";

        if (v >= 9.95 && v <= 10.05) {
            el.classList.add("score-chip--flame");
            el.classList.add("score-chip--hot");

            var periodPulse = 1.8;
            var periodFlame = 1.3;
            var now = Date.now() / 1000;
            var phasePulse = now % periodPulse;
            var phaseFlame = now % periodFlame;

            el.style.animationDelay = (-phasePulse).toFixed(2) + "s";
            el.style.setProperty("--flame-delay", (-phaseFlame).toFixed(2) + "s");
        } else {
            el.classList.remove("score-chip--flame");
            el.classList.remove("score-chip--hot");
            el.style.animationDelay = "";
            el.style.removeProperty("--flame-delay");
        }
    }

function openModal() {
        var backdrop = $("#viewer-modal-backdrop");
        if (backdrop) {
            backdrop.classList.add("is-open");
        }
    }

    function closeModal() {
        var backdrop = $("#viewer-modal-backdrop");
        if (backdrop) {
            backdrop.classList.remove("is-open");
        }
        currentTrackId = null;
    }


    function setSlidersEnabled(enabled) {
        $all(".viewer-slider").forEach(function (s) {
            s.disabled = !enabled;
        });
    }

    function updateViewerButtonState() {
        var btn = $("#viewer-submit-btn");
        if (!btn) return;
        if (alreadyRated) {
            btn.classList.add("btn-viewer-rated");
        } else {
            btn.classList.remove("btn-viewer-rated");
        }
    }

function hideAllToasts() {
        var thanks = $("#viewer-thanks");
        var already = $("#viewer-already");
        [thanks, already].forEach(function (el) {
            if (!el) return;
            el.classList.remove("is-visible");
            el.style.display = "none";
        });
        if (toastTimer) {
            clearTimeout(toastTimer);
            toastTimer = null;
        }
    }

    function showToast(which) {
        hideAllToasts();
        var el = null;
        if (which === "thanks") {
            el = $("#viewer-thanks");
        } else if (which === "already") {
            el = $("#viewer-already");
        }
        if (!el) return;
        el.style.display = "block";
        el.classList.add("is-visible");
        toastTimer = setTimeout(function () {
            el.classList.remove("is-visible");
        }, 2600);
    }

    function updateViewerOverall() {
        var sliders = $all(".viewer-slider");
        if (!sliders.length) return;
        var sum = 0;
        var count = 0;
        sliders.forEach(function (s) {
            var v = Number(s.value) || 0;
            sum += v;
            count += 1;
        });
        var avg = count ? sum / count : 0;
        var chip = $("#viewer-self-overall");
        if (chip) {
            chip.textContent = avg.toFixed(1);
            applyHeatToChip(chip, avg);
        }
    }

    function attachSliderHandlers() {
        $all(".viewer-slider").forEach(function (s) {
            s.addEventListener("input", function () {
                var key = s.dataset.criterion;
                var v = s.value;
                var label = document.querySelector('[data-criterion-value="' + key + '"]');
                if (label) {
                    label.textContent = v;
                    applyHeatToChip(label, v);
                }
                applyHeatToSlider(s, v);
                updateViewerOverall();
            });
        });
    }

    function loadTrack(trackId) {
        hideAllToasts();
        fetch("/api/viewers/track/" + trackId)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || data.error) return;

                currentTrackId = data.track.id;
                if ($("#viewer-modal-title")) {
                    $("#viewer-modal-title").textContent = data.track.name || "Оценка трека";
                }
                if ($("#viewer-modal-subtitle")) {
                    var dt = data.track.created_at ? new Date(data.track.created_at) : null;
                    $("#viewer-modal-subtitle").textContent = dt
                        ? "Добавлен: " + dt.toLocaleString("ru-RU")
                        : "Дата добавления неизвестна";
                }

                if ($("#viewer-modal-overall")) {
                    $("#viewer-modal-overall").textContent = (data.overall_avg || 0).toFixed(2);
                    applyHeatToChip($("#viewer-modal-overall"), data.overall_avg || 0);
                }

                $all(".viewer-slider").forEach(function (s) {
                    var key = s.dataset.criterion;
                    var val = 0;
                    if (data.viewer && data.viewer.scores && data.viewer.scores[key] != null) {
                        val = data.viewer.scores[key];
                    }
                    s.value = val;
                    var label = document.querySelector('[data-criterion-value="' + key + '"]');
                    if (label) {
                        label.textContent = val;
                        applyHeatToChip(label, val);
                    }
                    applyHeatToSlider(s, val);
                });

                var hasVoted = data.viewer && data.viewer.has_voted;
                alreadyRated = !!hasVoted;
                setSlidersEnabled(!hasVoted);
                updateViewerButtonState();
                updateViewerOverall();
                openModal();
            });
    }

    

    // Глобальный помощник для открытия модалки оценки по trackId (страница трека)
    window.openViewerRatingModal = function (trackId) {
        loadTrack(trackId);
    };

function sendRating() {
        if (!currentTrackId) return;

        if (alreadyRated) {
            showToast("already");
            return;
        }

        var payload = {
            track_id: currentTrackId,
            ratings: {}
        };

        $all(".viewer-slider").forEach(function (s) {
            var key = s.dataset.criterion;
            payload.ratings[key] = parseInt(s.value || "0", 10);
        });

        fetch("/api/viewers/rate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data) return;
                if (data.error === "already_rated") {
                    alreadyRated = true;
                    setSlidersEnabled(false);
                    updateViewerButtonState();
                    showToast("already");
                    return;
                }
                if (data.status === "ok") {
                    if ($("#viewer-modal-overall")) {
                        $("#viewer-modal-overall").textContent = (data.overall_avg || 0).toFixed(2);
                        applyHeatToChip($("#viewer-modal-overall"), data.overall_avg || 0);
                    }
                    alreadyRated = true;
                    setSlidersEnabled(false);
                    updateViewerButtonState();
                    updateViewerOverall();
                    showToast("thanks");
                }
            });
    }

    document.addEventListener("DOMContentLoaded", function () {
        attachSliderHandlers();

                $all(".viewer-track-row").forEach(function (row) {
            row.addEventListener("click", function () {
                var id = row.getAttribute("data-track-id");
                if (!id) return;
                loadTrack(id);
            });
        });

        // Если пришли на страницу с параметром ?track_id=..., сразу откроем модалку для этого трека (если он на текущей странице)
        try {
            var params = new URLSearchParams(window.location.search);
            var preTrackId = params.get("track_id");
            if (preTrackId) {
                var targetRow = null;
                $all(".viewer-track-row").forEach(function (row) {
                    if (row.getAttribute("data-track-id") === preTrackId) {
                        targetRow = row;
                    }
                });
                if (targetRow) {
                    loadTrack(preTrackId);
                }
            }
        } catch (e) {
            console.warn("Cannot parse URL params for preselect track_id", e);
        }

        var closeBtn = $("#viewer-modal-close");
        if (closeBtn) {
            closeBtn.addEventListener("click", closeModal);
        }
        var backdrop = $("#viewer-modal-backdrop");
        if (backdrop) {
            backdrop.addEventListener("click", function (e) {
                if (e.target === backdrop) {
                    closeModal();
                }
            });
        }

        var submitBtn = $("#viewer-submit-btn");
        if (submitBtn) {
            submitBtn.addEventListener("click", sendRating);
        }
    });
})();
