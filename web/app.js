(function () {
  "use strict";

  var state = {
    manifest: null,
    data: null,
    xStart: 0,
    windowWidth: 8,
    speed: 5,
    playing: false,
    lastFrame: 0,
    lastFetchAt: 0,
    fetchPending: false,
    requestSerial: 0,
    cursorX: null,
    tracks: new Set([0, 1]),
    gates: new Set(),
    gateOptions: [],
    usMetrics: null,
    mflMetrics: null,
    mflYLimit: 8
  };

  var gateColors = [
    "#5bd7e5", "#a98cff", "#ffad68", "#ff7f9d",
    "#83dc9d", "#f5d76e", "#78a8ff", "#d28fff"
  ];

  var ui = {
    healthDot: document.getElementById("healthDot"),
    healthText: document.getElementById("healthText"),
    healthDetail: document.getElementById("healthDetail"),
    playButton: document.getElementById("playButton"),
    jointButton: document.getElementById("jointButton"),
    homeButton: document.getElementById("homeButton"),
    endButton: document.getElementById("endButton"),
    windowSelect: document.getElementById("windowSelect"),
    speedSelect: document.getElementById("speedSelect"),
    startInput: document.getElementById("startInput"),
    jumpButton: document.getElementById("jumpButton"),
    timeline: document.getElementById("timeline"),
    rangeLabel: document.getElementById("rangeLabel"),
    cursorLabel: document.getElementById("cursorLabel"),
    loadLabel: document.getElementById("loadLabel"),
    gateFilters: document.getElementById("gateFilters"),
    selectAllGatesButton: document.getElementById("selectAllGatesButton"),
    clearGatesButton: document.getElementById("clearGatesButton"),
    usCanvas: document.getElementById("usCanvas"),
    mflCanvas: document.getElementById("mflCanvas"),
    usEmpty: document.getElementById("usEmpty"),
    mflEmpty: document.getElementById("mflEmpty"),
    usCountLabel: document.getElementById("usCountLabel"),
    mflCountLabel: document.getElementById("mflCountLabel"),
    coordinateInfo: document.getElementById("coordinateInfo"),
    jointInfo: document.getElementById("jointInfo"),
    coverageInfo: document.getElementById("coverageInfo"),
    sourceInfo: document.getElementById("sourceInfo"),
    warningInfo: document.getElementById("warningInfo"),
    serverInfo: document.getElementById("serverInfo")
  };

  function clamp(value, low, high) {
    return Math.max(low, Math.min(high, value));
  }

  function numberOr(value, fallback) {
    var parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function formatNumber(value, digits) {
    if (!Number.isFinite(Number(value))) {
      return "—";
    }
    return Number(value).toLocaleString("zh-CN", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits
    });
  }

  function formatDistance(value) {
    var number = Number(value);
    if (!Number.isFinite(number)) {
      return "—";
    }
    var abs = Math.abs(number);
    if (abs >= 1000) {
      return (number / 1000).toFixed(abs >= 10000 ? 1 : 2) + " km";
    }
    return number.toFixed(abs < 10 ? 2 : 1) + " m";
  }

  function formatRange(start, end) {
    return formatDistance(start) + "  →  " + formatDistance(end);
  }

  function setHealth(kind, title, detail) {
    ui.healthDot.className = "health-dot " + kind;
    ui.healthText.textContent = title;
    ui.healthDetail.textContent = detail || "";
  }

  function setStatusMessage(message) {
    ui.loadLabel.textContent = message;
  }

  function getDataRange() {
    if (!state.manifest || !state.manifest.range) {
      return { start: -1, end: 1 };
    }
    return {
      start: numberOr(state.manifest.range.startM, -1),
      end: numberOr(state.manifest.range.endM, 1)
    };
  }

  function maxStart() {
    var range = getDataRange();
    return Math.max(range.start, range.end - state.windowWidth);
  }

  function normalizeStart(value) {
    var range = getDataRange();
    if (range.end <= range.start) {
      return range.start;
    }
    return clamp(numberOr(value, range.start), range.start, maxStart());
  }

  function setStart(value, shouldLoad) {
    state.xStart = normalizeStart(value);
    ui.startInput.value = state.xStart.toFixed(2);
    updateTimeline();
    if (shouldLoad !== false) {
      requestWindow();
    }
  }

  function updateTimeline() {
    var range = getDataRange();
    var span = Math.max(0.000001, range.end - range.start - state.windowWidth);
    var ratio = clamp((state.xStart - range.start) / span, 0, 1);
    ui.timeline.min = "0";
    ui.timeline.max = "1";
    ui.timeline.step = "0.0001";
    ui.timeline.value = ratio.toFixed(4);
    ui.rangeLabel.textContent = "范围：" + formatRange(state.xStart, state.xStart + state.windowWidth);
    if (state.cursorX === null) {
      ui.cursorLabel.textContent = "光标：—";
    } else {
      ui.cursorLabel.textContent = "光标：" + formatDistance(state.cursorX);
    }
  }

  function gateColor(gate) {
    var index = Math.abs(Number(gate)) % gateColors.length;
    return gateColors[index];
  }

  function buildGateFilters() {
    var gates = state.gateOptions;
    ui.gateFilters.innerHTML = "";
    if (!gates.length) {
      ui.gateFilters.innerHTML = '<span class="muted-inline">没有 Gate 元数据</span>';
      return;
    }

    gates.forEach(function (item) {
      var gate = Number(item.gate);
      var button = document.createElement("button");
      button.type = "button";
      button.className = "gate-pill active";
      button.style.setProperty("--gate-color", gateColor(gate));
      button.dataset.gate = String(gate);
      button.innerHTML = "G" + gate + "<em>" + formatNumber(item.count || 0, 0) + "</em>";
      button.addEventListener("click", function () {
        if (state.gates.has(gate)) {
          state.gates.delete(gate);
          button.classList.remove("active");
        } else {
          state.gates.add(gate);
          button.classList.add("active");
        }
        requestWindow();
      });
      ui.gateFilters.appendChild(button);
    });
  }

  function selectedGateQuery() {
    if (!state.gateOptions.length) {
      return "";
    }
    if (state.gates.size === state.gateOptions.length) {
      return "all";
    }
    if (!state.gates.size) {
      return "none";
    }
    return Array.from(state.gates).sort(function (a, b) {
      return a - b;
    }).join(",");
  }

 function selectedTrackQuery() {
    if (!state.tracks.size) {
      return "none";
    }
    return Array.from(state.tracks).sort(function (a, b) {
      return a - b;
    }).join(",");
 }

  function initializeControls() {
    ui.windowSelect.addEventListener("change", function () {
      state.windowWidth = numberOr(ui.windowSelect.value, 8);
      setStart(state.xStart, true);
    });

    ui.speedSelect.addEventListener("change", function () {
      state.speed = numberOr(ui.speedSelect.value, 5);
    });

    ui.playButton.addEventListener("click", function () {
      state.playing = !state.playing;
      updatePlayButton();
      if (state.playing) {
        state.lastFrame = performance.now();
        requestAnimationFrame(playFrame);
      }
    });

    ui.jointButton.addEventListener("click", function () {
      setStart(-state.windowWidth / 2, true);
    });

    ui.homeButton.addEventListener("click", function () {
      setStart(getDataRange().start, true);
    });

    ui.endButton.addEventListener("click", function () {
      setStart(maxStart(), true);
    });

    ui.jumpButton.addEventListener("click", function () {
      setStart(numberOr(ui.startInput.value, state.xStart), true);
    });

    ui.startInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        setStart(numberOr(ui.startInput.value, state.xStart), true);
      }
    });

    ui.timeline.addEventListener("input", function () {
      var range = getDataRange();
      var span = Math.max(0, range.end - range.start - state.windowWidth);
      setStart(range.start + numberOr(ui.timeline.value, 0) * span, true);
    });

    document.querySelectorAll("[data-track]").forEach(function (input) {
      input.addEventListener("change", function () {
        var track = Number(input.dataset.track);
        if (input.checked) {
          state.tracks.add(track);
        } else {
          state.tracks.delete(track);
        }
        requestWindow();
      });
    });

    ui.selectAllGatesButton.addEventListener("click", function () {
      state.gates = new Set(state.gateOptions.map(function (item) {
        return Number(item.gate);
      }));
      document.querySelectorAll("[data-gate]").forEach(function (button) {
        button.classList.add("active");
      });
      requestWindow();
    });

    ui.clearGatesButton.addEventListener("click", function () {
      state.gates.clear();
      document.querySelectorAll("[data-gate]").forEach(function (button) {
        button.classList.remove("active");
      });
      requestWindow();
    });

    window.addEventListener("keydown", function (event) {
      var tag = event.target && event.target.tagName ? event.target.tagName.toLowerCase() : "";
      if (tag === "input" || tag === "select" || tag === "textarea") {
        return;
      }
      if (event.key === " ") {
        event.preventDefault();
        ui.playButton.click();
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        setStart(state.xStart - (event.shiftKey ? 0.2 : 1), true);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        setStart(state.xStart + (event.shiftKey ? 0.2 : 1), true);
      } else if (event.key === "Home") {
        setStart(getDataRange().start, true);
      } else if (event.key === "End") {
        setStart(maxStart(), true);
      }
    });

    [ui.usCanvas, ui.mflCanvas].forEach(function (canvas) {
      canvas.addEventListener("pointermove", onCanvasPointerMove);
      canvas.addEventListener("pointerleave", function () {
        state.cursorX = null;
        updateTimeline();
        drawAll();
      });
      canvas.addEventListener("dblclick", function (event) {
        var metrics = canvas === ui.usCanvas ? state.usMetrics : state.mflMetrics;
        if (!metrics) {
          return;
        }
        var rect = canvas.getBoundingClientRect();
        var x = metrics.xStart + ((event.clientX - rect.left - metrics.left) / metrics.plotWidth) * (metrics.xEnd - metrics.xStart);
        setStart(x - state.windowWidth / 2, true);
      });
    });

    window.addEventListener("resize", function () {
      if (state.data) {
        drawAll();
      }
    });
  }

  function onCanvasPointerMove(event) {
    var canvas = event.currentTarget;
    var metrics = canvas === ui.usCanvas ? state.usMetrics : state.mflMetrics;
    if (!metrics) {
      return;
    }
    var rect = canvas.getBoundingClientRect();
    var px = event.clientX - rect.left;
    if (px < metrics.left || px > metrics.left + metrics.plotWidth) {
      state.cursorX = null;
    } else {
      state.cursorX = metrics.xStart + ((px - metrics.left) / metrics.plotWidth) * (metrics.xEnd - metrics.xStart);
    }
    updateTimeline();
    drawAll();
  }

  function updatePlayButton() {
    ui.playButton.textContent = state.playing ? "Ⅱ 暂停回放" : "▶ 开始回放";
    ui.playButton.classList.toggle("is-playing", state.playing);
  }

  function playFrame(timestamp) {
    if (!state.playing) {
      return;
    }
    var elapsed = Math.min(0.25, Math.max(0, (timestamp - state.lastFrame) / 1000));
    state.lastFrame = timestamp;
    var next = state.xStart + state.speed * elapsed;
    if (next >= maxStart()) {
      state.xStart = maxStart();
      state.playing = false;
      updatePlayButton();
      requestWindow();
      return;
    }
    state.xStart = normalizeStart(next);
    updateTimeline();
    if (timestamp - state.lastFetchAt > 160 && !state.fetchPending) {
      requestWindow();
    }
    requestAnimationFrame(playFrame);
  }

  function requestWindow() {
    if (!state.manifest) {
      return;
    }
    var serial = ++state.requestSerial;
    var params = new URLSearchParams();
    params.set("x_start", String(state.xStart));
    params.set("x_end", String(state.xStart + state.windowWidth));
    params.set("tracks", selectedTrackQuery());
    params.set("gates", selectedGateQuery());
    params.set("max_mfl_points", "3600");
    params.set("max_us_records", "120000");
    state.fetchPending = true;
    setStatusMessage("窗口读取中…");
    fetch("/api/window?" + params.toString(), { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.json();
      })
      .then(function (payload) {
        if (serial !== state.requestSerial) {
          return;
        }
        state.data = payload;
        state.lastFetchAt = performance.now();
        state.fetchPending = false;
        updateDataLabels();
        drawAll();
        setStatusMessage("窗口已加载");
      })
      .catch(function (error) {
        if (serial !== state.requestSerial) {
          return;
        }
        state.fetchPending = false;
        setStatusMessage("读取失败：" + error.message);
        setHealth("error", "窗口读取失败", "请查看服务窗口或刷新页面");
      });
  }

  function chartMetrics(width, height, left, right, top, bottom) {
    return {
      width: width,
      height: height,
      left: left,
      right: right,
      top: top,
      bottom: bottom,
      plotWidth: Math.max(1, width - left - right),
      plotHeight: Math.max(1, height - top - bottom),
      xStart: state.xStart,
      xEnd: state.xStart + state.windowWidth
    };
  }

  function prepareCanvas(canvas, height) {
    var width = Math.max(500, Math.floor(canvas.clientWidth || canvas.parentElement.clientWidth || 900));
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.style.height = height + "px";
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, width: width, height: height };
  }

  function clearCanvas(ctx, width, height) {
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#07111b";
    ctx.fillRect(0, 0, width, height);
  }

  function drawXGrid(ctx, metrics, labelY) {
    var tickCount = state.windowWidth <= 10 ? 5 : 6;
    ctx.save();
    ctx.font = "11px Segoe UI, Microsoft YaHei, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (var i = 0; i <= tickCount; i += 1) {
      var ratio = i / tickCount;
      var px = metrics.left + ratio * metrics.plotWidth;
      ctx.strokeStyle = i === 0 || i === tickCount ? "rgba(166,191,211,0.27)" : "rgba(166,191,211,0.11)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(px, metrics.top);
      ctx.lineTo(px, metrics.height - metrics.bottom);
      ctx.stroke();
      ctx.fillStyle = "#78909e";
      var value = metrics.xStart + ratio * (metrics.xEnd - metrics.xStart);
      ctx.fillText(formatDistance(value), px, labelY);
    }
    ctx.restore();
  }

  function drawCrosshair(ctx, metrics) {
    if (state.cursorX === null || state.cursorX < metrics.xStart || state.cursorX > metrics.xEnd) {
      return;
    }
    var px = metrics.left + ((state.cursorX - metrics.xStart) / (metrics.xEnd - metrics.xStart)) * metrics.plotWidth;
    ctx.save();
    ctx.strokeStyle = "rgba(255,255,255,0.68)";
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(px, metrics.top);
    ctx.lineTo(px, metrics.height - metrics.bottom);
    ctx.stroke();
    ctx.restore();
  }

  function drawUltrasound() {
    var data = state.data && state.data.ultrasound ? state.data.ultrasound : null;
    var prep = prepareCanvas(ui.usCanvas, 370);
    var ctx = prep.ctx;
    var metrics = chartMetrics(prep.width, prep.height, 72, 22, 22, 38);
    state.usMetrics = metrics;
    clearCanvas(ctx, prep.width, prep.height);
    drawXGrid(ctx, metrics, prep.height - metrics.bottom + 10);

    var activeTracks = Array.from(state.tracks).sort(function (a, b) {
      return a - b;
    });
    var bandHeight = metrics.plotHeight / Math.max(1, activeTracks.length);
    var depthMax = 176;

    ctx.save();
    ctx.font = "11px Segoe UI, Microsoft YaHei, sans-serif";
    activeTracks.forEach(function (track, bandIndex) {
      var top = metrics.top + bandIndex * bandHeight;
      var bottom = top + bandHeight;
      ctx.fillStyle = track === 0 ? "rgba(91,215,229,0.045)" : "rgba(255,173,104,0.045)";
      ctx.fillRect(metrics.left, top, metrics.plotWidth, bandHeight);
      ctx.strokeStyle = "rgba(166,191,211,0.18)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(metrics.left, bottom);
      ctx.lineTo(metrics.left + metrics.plotWidth, bottom);
      ctx.stroke();
      ctx.fillStyle = track === 0 ? "#5bd7e5" : "#ffad68";
      ctx.textAlign = "right";
      ctx.textBaseline = "top";
      ctx.fillText("Track " + track, metrics.left - 11, top + 5);

      [0, 44, 88, 132, 176].forEach(function (depth) {
        var y = top + 24 + (depth / depthMax) * Math.max(1, bandHeight - 38);
        ctx.strokeStyle = "rgba(166,191,211,0.10)";
        ctx.beginPath();
        ctx.moveTo(metrics.left, y);
        ctx.lineTo(metrics.left + metrics.plotWidth, y);
        ctx.stroke();
        if (bandIndex === 0 || depth !== 0) {
          ctx.fillStyle = "#78909e";
          ctx.textAlign = "right";
          ctx.textBaseline = "middle";
          ctx.fillText(String(depth), metrics.left - 11, y);
        }
      });
    });
    ctx.restore();

    var count = 0;
    if (data && data.available && data.x && data.x.length) {
      var xValues = data.x;
      var depthValues = data.depth || [];
      var trackValues = data.track || [];
      var gateValues = data.gate || [];
      count = xValues.length;
      var stride = count > 45000 ? Math.ceil(count / 45000) : 1;
      var trackIndex = {};
      activeTracks.forEach(function (track, index) {
        trackIndex[track] = index;
      });
      ctx.save();
      for (var i = 0; i < count; i += stride) {
        var track = Number(trackValues[i]);
        var bandIndex = trackIndex[track];
        if (bandIndex === undefined) {
          continue;
        }
        var x = metrics.left + ((Number(xValues[i]) - metrics.xStart) / (metrics.xEnd - metrics.xStart)) * metrics.plotWidth;
        if (x < metrics.left || x > metrics.left + metrics.plotWidth) {
          continue;
        }
        var depth = clamp(Number(depthValues[i]) * 176 / 127, 0, 176);
        var bandTop = metrics.top + bandIndex * bandHeight;
        var y = bandTop + 24 + (depth / depthMax) * Math.max(1, bandHeight - 38);
        ctx.fillStyle = gateColor(Number(gateValues[i]));
        ctx.globalAlpha = 0.78;
        ctx.fillRect(x - 1.6, y - 1.6, 3.2, 3.2);
      }
      ctx.restore();
    }

    drawCrosshair(ctx, metrics);
    ui.usEmpty.classList.toggle("hidden", count > 0);
    ui.usCountLabel.textContent = "事件：" + formatNumber(data && data.count !== undefined ? data.count : 0, 0) +
      (data && data.decimated ? "（已抽样）" : "");
  }

  function valueExtents() {
    var data = state.data && state.data.mfl;
    var maxAbs = 0;
    if (!data || !data.rows) {
      return 0;
    }
    data.rows.forEach(function (row) {
      (row.segments || []).forEach(function (segment) {
        (segment.xValue || []).forEach(function (value) {
          maxAbs = Math.max(maxAbs, Math.abs(Number(value)));
        });
        (segment.zValue || []).forEach(function (value) {
          maxAbs = Math.max(maxAbs, Math.abs(Number(value)));
        });
      });
    });
    return maxAbs;
  }

  function drawMfl() {
    var data = state.data && state.data.mfl ? state.data.mfl : null;
    var rows = data && data.rows ? data.rows : [];
    var rowCount = Math.max(16, rows.length);
    var height = Math.max(470, rowCount * 57 + 46);
    var prep = prepareCanvas(ui.mflCanvas, height);
    var ctx = prep.ctx;
    var metrics = chartMetrics(prep.width, prep.height, 96, 22, 18, 34);
    state.mflMetrics = metrics;
    clearCanvas(ctx, prep.width, prep.height);
    drawXGrid(ctx, metrics, prep.height - metrics.bottom + 9);

    var rowHeight = metrics.plotHeight / rowCount;
    var yLimit = state.mflYLimit;
    var actualMax = valueExtents();
    if (actualMax > yLimit * 1.12 && actualMax < 40) {
      state.mflYLimit = Math.max(yLimit, Math.ceil(actualMax * 1.1 * 2) / 2);
      yLimit = state.mflYLimit;
    }

    ctx.save();
    ctx.font = "10px Segoe UI, Microsoft YaHei, sans-serif";
    for (var rowIndex = 0; rowIndex < rowCount; rowIndex += 1) {
      var top = metrics.top + rowIndex * rowHeight;
      var bottom = top + rowHeight;
      var middle = top + rowHeight / 2;
      ctx.fillStyle = rowIndex % 2 ? "rgba(166,191,211,0.025)" : "rgba(166,191,211,0.045)";
      ctx.fillRect(metrics.left, top, metrics.plotWidth, rowHeight);
      ctx.strokeStyle = "rgba(166,191,211,0.13)";
      ctx.beginPath();
      ctx.moveTo(metrics.left, bottom);
      ctx.lineTo(metrics.left + metrics.plotWidth, bottom);
      ctx.stroke();
      ctx.strokeStyle = "rgba(166,191,211,0.08)";
      [0.25, 0.5, 0.75].forEach(function (ratio) {
        var y = top + ratio * rowHeight;
        ctx.beginPath();
        ctx.moveTo(metrics.left, y);
        ctx.lineTo(metrics.left + metrics.plotWidth, y);
        ctx.stroke();
      });
      ctx.strokeStyle = "rgba(166,191,211,0.28)";
      ctx.beginPath();
      ctx.moveTo(metrics.left, middle);
      ctx.lineTo(metrics.left + metrics.plotWidth, middle);
      ctx.stroke();

      var row = rows[rowIndex];
      var side = row && row.side ? String(row.side).toUpperCase() : (rowIndex < 8 ? "LEFT" : "RIGHT");
      var number = row && row.channel !== undefined ? row.channel : (rowIndex % 8) + 1;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillStyle = side === "LEFT" ? "#5bd7e5" : "#ffad68";
      ctx.fillText(side + "  " + number, metrics.left - 12, middle);
      ctx.fillStyle = "#78909e";
      ctx.textAlign = "left";
      ctx.fillText("+" + yLimit.toFixed(1), 8, top + 4);
      ctx.fillText("−" + yLimit.toFixed(1), 8, bottom - 5);
    }
    ctx.restore();

    var hasData = false;
    if (rows.length) {
      rows.forEach(function (row, index) {
        var rowTop = metrics.top + index * rowHeight;
        var mid = rowTop + rowHeight / 2;
        var scale = (rowHeight / 2 - 5) / yLimit;
        (row.segments || []).forEach(function (segment) {
          var xs = segment.x || [];
          var xValues = segment.xValue || [];
          var zValues = segment.zValue || [];
          drawMflTrace(ctx, metrics, xs, xValues, mid, scale, "#5bd7e5");
          drawMflTrace(ctx, metrics, xs, zValues, mid, scale, "#ffad68");
          if (xs.length) {
            hasData = true;
          }
        });
      });
    }

    drawCrosshair(ctx, metrics);
    ui.mflEmpty.classList.toggle("hidden", hasData);
    ui.mflCountLabel.textContent = "原始记录：" + formatNumber(data && data.rawRecordsPerChannel !== undefined ? data.rawRecordsPerChannel : 0, 0) +
      " / 通道";
  }

  function drawMflTrace(ctx, metrics, xs, values, middle, scale, color) {
    if (!xs.length || !values.length) {
      return;
    }
    ctx.save();
    ctx.strokeStyle = color;
    ctx.globalAlpha = 0.92;
    ctx.lineWidth = 1.15;
    ctx.beginPath();
    var started = false;
    var length = Math.min(xs.length, values.length);
    for (var i = 0; i < length; i += 1) {
      var x = metrics.left + ((Number(xs[i]) - metrics.xStart) / (metrics.xEnd - metrics.xStart)) * metrics.plotWidth;
      if (x < metrics.left - 2 || x > metrics.left + metrics.plotWidth + 2) {
        continue;
      }
      var value = clamp(Number(values[i]), -state.mflYLimit, state.mflYLimit);
      var y = middle - value * scale;
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else {
        ctx.lineTo(x, y);
      }
    }
    if (started) {
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawAll() {
    if (!state.manifest) {
      return;
    }
    updateTimeline();
    drawUltrasound();
    drawMfl();
  }

  function updateDataLabels() {
    var data = state.data;
    var manifest = state.manifest;
    var us = data && data.ultrasound ? data.ultrasound : {};
    var mfl = data && data.mfl ? data.mfl : {};
    var sourceSegments = mfl.sourceSegments || [];
    var names = sourceSegments.map(function (item) {
      return item.name;
    });
    ui.sourceInfo.textContent = "当前窗口：" + (names.length ? names.join(" · ") : "无命中段") +
      "；超声 Track：" + (Array.from(state.tracks).sort().join("/") || "无");
    ui.serverInfo.textContent = "服务端：" + formatNumber(data && data.serverMs, 2) + " ms";
    ui.loadLabel.textContent = "窗口已加载 · US " + formatNumber(us.count || 0, 0) +
      " · MFL " + formatNumber(mfl.rawRecordsPerChannel || 0, 0) + " / 通道";
  }

  function updateManifestLabels() {
    var manifest = state.manifest;
    var coordinate = manifest.coordinate || {};
    var joint = manifest.joint || {};
    var range = manifest.range || {};
    var mfl = manifest.mfl || {};
    var us = manifest.ultrasound || {};

    ui.coordinateInfo.textContent = "接头相对距离 / m";
    ui.jointInfo.textContent = "MFL " + (joint.mflSegment || "—") +
      " · record_pos " + formatNumber(joint.mflRecordPos, 0) +
      " · DAT.index " + formatNumber(joint.mflRawIndex, 0) +
      "；US ID " + formatNumber(joint.usId, 0);
    ui.coverageInfo.textContent = formatRange(range.startM, range.endM);
    ui.sourceInfo.textContent = "MFL " + formatDistance(range.mfl && range.mfl.startM) +
      " 至 " + formatDistance(range.mfl && range.mfl.endM) +
      "；US " + (us.available ? formatDistance(us.startM) + " 至 " + formatDistance(us.endM) : "不可用");

    var warnings = (manifest.warnings || []).slice(0, 4);
    var warningText = warnings.length ? warnings.join("；") : "当前未发现服务端警告。";
    ui.warningInfo.textContent = warningText;
    setHealth("ok", "数据服务已连接", "MFL " + (mfl.segments ? mfl.segments.length : 0) + " 段 · US " + (us.chunkCount || 0) + " 个块");

    state.gateOptions = us.gates || [];
    state.gates = new Set(state.gateOptions.map(function (item) {
      return Number(item.gate);
    }));
    buildGateFilters();

    var rangeStart = numberOr(range.startM, -1);
    state.xStart = normalizeStart(-state.windowWidth / 2);
    ui.startInput.value = state.xStart.toFixed(2);
    updateTimeline();
  }

  function showFatalError(error) {
    setHealth("error", "数据服务不可用", error.message || String(error));
    ui.warningInfo.textContent = "请在程序目录运行 python server.py；如果是路径问题，请检查 config.json。";
    ui.sourceInfo.textContent = "未能读取 /api/manifest";
    setStatusMessage("初始化失败");
  }

  function init() {
    initializeControls();
    fetch("/api/manifest", { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.json();
      })
      .then(function (manifest) {
        state.manifest = manifest;
        updateManifestLabels();
        setStatusMessage("正在读取接头窗口…");
        requestWindow();
      })
      .catch(showFatalError);
  }

  init();
})();
