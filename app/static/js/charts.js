// charts.js — Plotly chart rendering

function updateChart(data) {
    var symbols = ['NIFTY', 'BANKNIFTY', 'FINNIFTY'];
    var prices  = symbols.map(function(s) { return data[s] && !data[s].error ? data[s].price  : 0; });
    var changes = symbols.map(function(s) { return data[s] && !data[s].error ? data[s].change : 0; });
    var colors  = changes.map(function(c) { return c >= 0 ? '#00e676' : '#ff5252'; });

    var trace = {
        x: symbols,
        y: prices,
        type: 'bar',
        marker: { color: colors, opacity: 0.85 },
        text: changes.map(function(c) { return (c >= 0 ? '▲ ' : '▼ ') + Math.abs(c).toFixed(2) + '%'; }),
        textposition: 'outside',
        textfont: { color: colors },
        hovertemplate: '<b>%{x}</b><br>Price: ₹%{y:,.2f}<br>Change: %{text}<extra></extra>'
    };

    var layout = {
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor:  'rgba(0,0,0,0)',
        font: { color: '#8899aa', size: 12 },
        margin: { t: 30, b: 40, l: 60, r: 20 },
        yaxis: {
            gridcolor: 'rgba(255,255,255,0.06)',
            tickformat: ',.0f',
            tickprefix: '₹'
        },
        xaxis: { tickfont: { size: 13, color: '#e0e8f0' } },
        hoverlabel: { bgcolor: '#1a2a4a', bordercolor: 'rgba(255,255,255,0.15)' }
    };

    var config = { responsive: true, displayModeBar: false };

    Plotly.newPlot('plotly-chart', [trace], layout, config);
}
