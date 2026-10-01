def export_printable_run_sheet_html(sched_df, inspector_name, depot_addr, route_date_str):
    total_miles = sched_df.iloc[-1]["Total Miles"] if not sched_df.empty else 0.0
    start_time_str = sched_df.iloc[0]["Arrival"] if not sched_df.empty else "--"
    end_time_str = sched_df.iloc[-1]["Arrival"] if not sched_df.empty else "--"
    
    stops = []
    for _, r in sched_df.iterrows():
        raw_id = str(r["Inspection ID"]).strip()
        addr = str(r.get("Address", "")).strip()
        if raw_id.upper() in ["BASE", "BASE RETURN", "DEPOT", ""] or addr.startswith("Start:") or addr.startswith("End:"):
            continue
        
        clean_id = raw_id.split("/")[-1].strip() if "/" in raw_id else raw_id
        stops.append({
            "stop_num": len(stops) + 1,
            "id": clean_id,
            "address": addr,
            "arrival": r["Arrival"],
            "total_miles": r["Total Miles"]
        })

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Daily Field Sheet - {inspector_name} - {route_date_str}</title>
<style>
    @page {{
        size: letter portrait;
        margin: 0.4in;
    }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
        color: #111;
        margin: 0;
        padding: 0;
        font-size: 9pt;
        line-height: 1.25;
    }}
    .tax-header {{
        border-bottom: 2px solid #000;
        padding-bottom: 4px;
        margin-bottom: 8px;
    }}
    .tax-title {{
        font-size: 13pt;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin: 0 0 2px 0;
    }}
    .meta-grid {{
        display: flex;
        justify-content: space-between;
        font-size: 8.5pt;
        font-weight: 600;
        margin-top: 2px;
    }}
    .summary-badge {{
        font-size: 9.5pt;
        font-weight: 800;
        background: #eee;
        padding: 2px 6px;
        border-radius: 4px;
    }}
    .stop-row {{
        padding: 4px 0;
        border-bottom: 1px solid #e5e7eb;
        display: flex;
        align-items: center;
        page-break-inside: avoid;
    }}
    .checkbox-box {{
        font-family: "Courier New", Courier, monospace;
        font-size: 10pt;
        font-weight: bold;
        width: 28px;
        flex-shrink: 0;
        white-space: nowrap;
    }}
    .stop-num {{
        font-weight: 800;
        width: 60px;
        flex-shrink: 0;
    }}
    .stop-main {{
        flex-grow: 1;
        padding-right: 12px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }}
    .stop-id {{
        font-weight: 700;
        color: #000;
    }}
    .stop-addr {{
        color: #222;
    }}
    .stop-time {{
        width: 75px;
        text-align: right;
        font-weight: 700;
        flex-shrink: 0;
    }}
    .stop-miles {{
        width: 65px;
        text-align: right;
        font-weight: 600;
        color: #444;
        flex-shrink: 0;
    }}
    .footer-note {{
        margin-top: 12px;
        font-size: 7.5pt;
        color: #666;
        border-top: 1px solid #ccc;
        padding-top: 4px;
        text-align: center;
    }}
    @media print {{
        .no-print {{ display: none !important; }}
    }}
</style>
</head>
<body>

<div class="no-print" style="background:#fef3c7; border:1px solid #f59e0b; padding:8px 12px; margin-bottom:12px; border-radius:6px; display:flex; justify-content:space-between; align-items:center;">
    <span>📄 <strong>Print-Ready Run Sheet:</strong> Formatted for standard 8.5" x 11" paper & tax records.</span>
    <button onclick="window.print()" style="background:#0284c7; color:#fff; font-weight:bold; padding:6px 14px; border:none; border-radius:5px; cursor:pointer;">🖨️ Print Document</button>
</div>

<div class="tax-header">
    <div class="tax-title">Carlson Field Services — Daily Inspection Run Sheet</div>
    <div class="meta-grid">
        <div><strong>Inspector:</strong> {inspector_name} | <strong>Date:</strong> {route_date_str}</div>
        <div><strong>Base:</strong> {depot_addr}</div>
    </div>
    <div class="meta-grid" style="margin-top: 4px;">
        <div><strong>Active Stops:</strong> {len(stops)} properties</div>
        <div><strong>Planned Span:</strong> {start_time_str} – {end_time_str}</div>
        <div class="summary-badge">Total Route: {total_miles:.1f} Miles</div>
    </div>
</div>

<div class="stop-list">
"""

    for s in stops:
        html += f"""
    <div class="stop-row">
        <div class="checkbox-box">[ &nbsp; ]</div>
        <div class="stop-num">Stop #{s['stop_num']}</div>
        <div class="stop-main">
            <span class="stop-id">ID: {s['id']}</span> &mdash; 
            <span class="stop-addr">{s['address']}</span>
        </div>
        <div class="stop-time">{s['arrival']}</div>
        <div class="stop-miles">{s['total_miles']:.1f} mi</div>
    </div>
"""

    html += f"""
</div>

<div class="footer-note">
    Official Carlson Field Services daily mileage & route log for tax year {datetime.now().year}. Generated {datetime.now().strftime('%B %d, %Y at %I:%M %p')}.
</div>

</body>
</html>
"""
    return html
