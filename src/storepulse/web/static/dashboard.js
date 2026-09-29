// Renders the installs charts from JSON embedded in the page (<script type="application/json">),
// so no inline script is needed and the Content-Security-Policy can stay script-src 'self'.
(function () {
  "use strict";
  if (typeof Chart === "undefined") return;

  var IOS = "#2563eb";
  var ANDROID = "#16a34a";

  function draw(canvas, sourceId) {
    var source = document.getElementById(sourceId);
    if (!canvas || !source) return;
    var data = JSON.parse(source.textContent);
    var datasets = [];
    if (data.ios.some(function (v) { return v > 0; })) {
      datasets.push({ label: "iOS", data: data.ios, borderColor: IOS, backgroundColor: IOS });
    }
    if (data.android.some(function (v) { return v > 0; })) {
      datasets.push({ label: "Android", data: data.android, borderColor: ANDROID, backgroundColor: ANDROID });
    }
    new Chart(canvas, {
      type: "line",
      data: { labels: data.days, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        elements: { point: { radius: 0 }, line: { borderWidth: 2, tension: 0.25 } },
        scales: {
          x: { ticks: { maxTicksLimit: 8 }, grid: { display: false } },
          y: { beginAtZero: true, ticks: { precision: 0 } }
        },
        plugins: { legend: { display: datasets.length > 1 } }
      }
    });
  }

  draw(document.getElementById("installs"), "chart-data");
  var charts = document.querySelectorAll("canvas.app-chart");
  for (var i = 0; i < charts.length; i++) {
    draw(charts[i], charts[i].getAttribute("data-source"));
  }
})();
