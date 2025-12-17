(function () {
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
  function formatTime(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    var m = Math.floor(sec / 60);
    var s = sec % 60;
    return m + ":" + (s < 10 ? "0" + s : s);
  }

  function initOne(root) {
    var audio = root.querySelector(".yplayer__audio");
    if (!audio) return;

    var btnToggle = root.querySelector("[data-yplayer-toggle]");
    var bar = root.querySelector("[data-yplayer-bar]");
    var fill = root.querySelector("[data-yplayer-fill]");
    var handle = root.querySelector("[data-yplayer-handle]");
    var curEl = root.querySelector("[data-yplayer-time-current]");
    var totEl = root.querySelector("[data-yplayer-time-total]");
    var vol = root.querySelector("[data-yplayer-vol]");
    var mute = root.querySelector("[data-yplayer-mute]");

    var VOL_KEY = "antigaz_embed_player_volume";
    var MUTED_KEY = "antigaz_embed_player_muted";

    function setMuteUi(isMuted) {
      if (!mute) return;
      mute.textContent = isMuted ? "🔇" : "🔊";
      mute.classList.toggle("is-muted", !!isMuted);
    }

    function updateUI() {
      var cur = audio.currentTime || 0;
      var dur = isFinite(audio.duration) ? audio.duration : 0;
      if (curEl) curEl.textContent = formatTime(cur);
      if (totEl) totEl.textContent = dur ? formatTime(dur) : "0:00";
      var ratio = dur ? (cur / dur) : 0;
      ratio = clamp(ratio, 0, 1);
      if (fill) fill.style.width = (ratio * 100) + "%";
      if (handle) handle.style.left = (ratio * 100) + "%";
      if (bar) bar.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
    }

    function restoreVolume() {
      try {
        var savedVol = localStorage.getItem(VOL_KEY);
        if (savedVol !== null && savedVol !== "") {
          var v = Number(savedVol);
          if (!isNaN(v)) audio.volume = clamp(v, 0, 1);
        }
        var savedMuted = localStorage.getItem(MUTED_KEY);
        if (savedMuted === "1") audio.muted = true;
        if (savedMuted === "0") audio.muted = false;
      } catch (e) {}
      if (vol) vol.value = String(clamp(audio.volume || 1, 0, 1));
      setMuteUi(audio.muted);
    }

    function setPlayingUi(isPlaying) {
      root.classList.toggle("is-playing", !!isPlaying);
    }

    function togglePlay() {
      if (audio.paused) {
        var p = audio.play();
        if (p && typeof p.catch === "function") p.catch(function () {});
      } else {
        audio.pause();
      }
    }

    if (btnToggle) {
      btnToggle.addEventListener("click", togglePlay);
    }

    function seekToRatio(ratio) {
      var dur = isFinite(audio.duration) ? audio.duration : 0;
      if (!dur) return;
      audio.currentTime = dur * clamp(ratio, 0, 1);
    }

    if (bar) {
      bar.addEventListener("click", function (e) {
        var rect = bar.getBoundingClientRect();
        var ratio = (e.clientX - rect.left) / rect.width;
        seekToRatio(ratio);
      });

      bar.addEventListener("keydown", function (e) {
        var dur = isFinite(audio.duration) ? audio.duration : 0;
        if (!dur) return;
        var step = 5;
        if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
          e.preventDefault();
          var next = (audio.currentTime || 0) + (e.key === "ArrowRight" ? step : -step);
          audio.currentTime = clamp(next, 0, dur);
        }
      });
    }

    if (vol) {
      var onVol = function () {
        var v = Number(vol.value);
        if (!isNaN(v)) audio.volume = clamp(v, 0, 1);
        if (audio.volume > 0 && audio.muted) audio.muted = false;
        try { localStorage.setItem(VOL_KEY, String(audio.volume)); } catch (e) {}
        try { localStorage.setItem(MUTED_KEY, audio.muted ? "1" : "0"); } catch (e) {}
        setMuteUi(audio.muted);
      };
      vol.addEventListener("input", onVol);
      vol.addEventListener("change", onVol);
    }

    if (mute) {
      mute.addEventListener("click", function () {
        audio.muted = !audio.muted;
        try { localStorage.setItem(MUTED_KEY, audio.muted ? "1" : "0"); } catch (e) {}
        setMuteUi(audio.muted);
      });
    }

    audio.addEventListener("timeupdate", updateUI);
    audio.addEventListener("loadedmetadata", updateUI);
    audio.addEventListener("durationchange", updateUI);
    audio.addEventListener("play", function () { setPlayingUi(true); });
    audio.addEventListener("pause", function () { setPlayingUi(false); });
    audio.addEventListener("ended", function () { setPlayingUi(false); });

    restoreVolume();
    updateUI();
    setPlayingUi(false);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var nodes = document.querySelectorAll("[data-yplayer-embed]");
    for (var i = 0; i < nodes.length; i++) {
      initOne(nodes[i]);
    }
  });
})();
