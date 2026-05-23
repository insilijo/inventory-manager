// Shared chart styling so every Chart.js chart in the app looks consistent.
window.FSFN = window.FSFN || {};

FSFN.PALETTE = ['#1D9E75', '#185FA5', '#BA7517', '#6B3FA0', '#167B82', '#E24B4A', '#8a8a86'];

FSFN.baseChartOptions = function (overrides) {
  const o = {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { position: 'bottom', labels: { boxWidth: 10, font: { size: 11 }, padding: 14 } },
      tooltip: { backgroundColor: '#1a1a18', titleFont: { size: 11 }, bodyFont: { size: 12 }, padding: 8, cornerRadius: 6 },
    },
    scales: {
      x: { ticks: { maxRotation: 0, autoSkip: true, font: { size: 10 }, color: '#6b6b67' }, grid: { display: false } },
      y: { beginAtZero: true, ticks: { font: { size: 10 }, color: '#6b6b67' }, grid: { color: 'rgba(0,0,0,0.06)' } },
    },
  };
  return FSFN.mergeDeep(o, overrides || {});
};

FSFN.mergeDeep = function (a, b) {
  if (Array.isArray(a) || Array.isArray(b)) return b;
  if (typeof a !== 'object' || a === null) return b;
  if (typeof b !== 'object' || b === null) return b;
  const out = { ...a };
  for (const k of Object.keys(b)) out[k] = FSFN.mergeDeep(a[k], b[k]);
  return out;
};

// Convert {bucket, site, total}[] into Chart.js {labels, datasets} form, one line per site.
FSFN.pivotBucketed = function (rows) {
  const buckets = [...new Set(rows.map(r => r.bucket))].sort();
  const sites   = [...new Set(rows.map(r => r.site))];
  const lookup = {};
  rows.forEach(r => { lookup[r.bucket + '|' + r.site] = r.total; });
  return {
    labels: buckets,
    datasets: sites.map((s, i) => ({
      label: s,
      data: buckets.map(b => lookup[b + '|' + s] || 0),
      borderColor: FSFN.PALETTE[i % FSFN.PALETTE.length],
      backgroundColor: FSFN.PALETTE[i % FSFN.PALETTE.length] + '33',
      borderWidth: 2,
      tension: 0.25,
      pointRadius: 2,
      fill: false,
    })),
  };
};
