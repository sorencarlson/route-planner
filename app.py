import os
import re
import math
import json
import requests
from datetime import datetime, timedelta, time
import glob
import urllib.parse
import pandas as pd
import folium
import polyline
from geopy.geocoders import Nominatim
from ortools.constraint_solver import routing_enums_pb2, pywrapcp
import streamlit as st
import streamlit.components.v1 as components

# ==========================================
# 1. CORE ENGINE & GEOCODING
# ==========================================
CACHE_FILE = "address_cache.json"
PERSISTENT_FILE = "current_active_route.csv"
SAVED_DIR = os.path.abspath("saved_routes")
os.makedirs(SAVED_DIR, exist_ok=True)

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_cache(cache):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass

def parse_lat_lon_string(text):
    pattern = r'^\s*([+-]?\d{1,2}(?:\.\d+)?)\s*,\s*([+-]?\d{1,3}(?:\.\d+)?)\s*$'
    match = re.match(pattern, str(text).strip())
    if match:
        try:
            lat = float(match.group(1))
            lon = float(match.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return [lat, lon]
        except Exception:
            pass
    return None

def suggest_address_cleanup(raw_address):
    cleaned = str(raw_address).strip()
    reasons = []

    # Fix spaced directionals: "N W" -> "NW", "S E" -> "SE"
    cleaned_spaced = re.sub(r'(?i)\bN\s+W\b', 'NW', cleaned)
    cleaned_spaced = re.sub(r'(?i)\bN\s+E\b', 'NE', cleaned_spaced)
    cleaned_spaced = re.sub(r'(?i)\bS\s+W\b', 'SW', cleaned_spaced)
    cleaned_spaced = re.sub(r'(?i)\bS\s+E\b', 'SE', cleaned_spaced)
    if cleaned_spaced != cleaned:
        cleaned = cleaned_spaced
        reasons.append("Fixed spaced directional (e.g. 'N W' -> 'NW')")

    # Fix bare numbers after quadrant: "ST SE 301," -> "ST SE,"
    cleaned_quad_unit = re.sub(r'(?i)\b(NW|NE|SW|SE)\s+\d+\b', r'\1', cleaned)
    if cleaned_quad_unit != cleaned:
        cleaned = cleaned_quad_unit
        reasons.append("Stripped bare unit number after directional")

    if re.search(r'(?i)\bstree\b(?!\w)', cleaned):
        cleaned = re.sub(r'(?i)\bstree\b', 'Street', cleaned)
        reasons.append("Fixed typo 'STREE' -> 'Street'")
        
    if re.search(r'(?i)\bdrve\b(?!\w)', cleaned):
        cleaned = re.sub(r'(?i)\bdrve\b', 'Drive', cleaned)
        reasons.append("Fixed typo 'DRVE' -> 'Drive'")

    if re.search(r'(?i)\bnortheast\b', cleaned):
        cleaned = re.sub(r'(?i)\bnortheast\b', 'NE', cleaned)
        reasons.append("Standardized 'NORTHEAST' -> 'NE'")
    if re.search(r'(?i)\bnorthwest\b', cleaned):
        cleaned = re.sub(r'(?i)\bnorthwest\b', 'NW', cleaned)
        reasons.append("Standardized 'NORTHWEST' -> 'NW'")
    if re.search(r'(?i)\bsoutheast\b', cleaned):
        cleaned = re.sub(r'(?i)\bsoutheast\b', 'SE', cleaned)
        reasons.append("Standardized 'SOUTHEAST' -> 'SE'")
    if re.search(r'(?i)\bsouthwest\b', cleaned):
        cleaned = re.sub(r'(?i)\bsouthwest\b', 'SW', cleaned)
        reasons.append("Standardized 'SOUTHWEST' -> 'SW'")

    match_dup_se = re.search(r'^\s*(\d+)\s+(SE|SW|NE|NW)\s+(.*?)\s+\2\b', cleaned, re.IGNORECASE)
    if match_dup_se:
        num, quad, rest = match_dup_se.group(1), match_dup_se.group(2).upper(), match_dup_se.group(3)
        cleaned = re.sub(r'^\s*\d+\s+(SE|SW|NE|NW)\s+.*?\s+\1\b', f"{num} {rest} {quad}", cleaned, flags=re.IGNORECASE)
        reasons.append(f"Removed duplicate directional prefix '{quad}'")

    cleaned_unit = strip_unit_designation(cleaned)
    if cleaned_unit != cleaned:
        reasons.append("Parsed base street address without unit/apartment number")

    return cleaned, reasons

def strip_unit_designation(address):
    addr_clean = re.sub(r'#\s*[\w-]+', '', str(address))
    addr_clean = re.sub(r'(?i)\bN\s+W\b', 'NW', addr_clean)
    addr_clean = re.sub(r'(?i)\bN\s+E\b', 'NE', addr_clean)
    addr_clean = re.sub(r'(?i)\bS\s+W\b', 'SW', addr_clean)
    addr_clean = re.sub(r'(?i)\bS\s+E\b', 'SE', addr_clean)
    addr_clean = re.sub(r'(?i)\b(NW|NE|SW|SE)\s+\d+\b', r'\1', addr_clean)
    unit_pattern = r'(?i)\b(apt|apartment|unit|ste|suite|bldg|building|fl|floor|dept|lot|rm|room|bsmt|basement|spc|space|trailer)\.?\s*[\w#-]+'
    addr_clean = re.sub(unit_pattern, '', addr_clean)
    addr_clean = re.sub(r',\s*,', ',', addr_clean)
    addr_clean = re.sub(r'\s{2,}', ' ', addr_clean).strip(" ,")
    return addr_clean

def fast_distance_meters(lat1, lon1, lat2, lon2):
    dlat = (lat2 - lat1) * 111000
    dlon = (lon2 - lon1) * 111000 * math.cos(math.radians((lat1 + lat2) / 2))
    return int(math.sqrt(dlat * dlat + dlon * dlon))

def geocode_arcgis(address, cache):
    if address in cache:
        return cache[address]
    try:
        clean_addr = strip_unit_designation(address)
        encoded_query = urllib.parse.quote(clean_addr)
        url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={encoded_query}&maxLocations=1"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            candidates = data.get("candidates", [])
            if candidates:
                loc = candidates[0].get("location", {})
                lat = float(loc.get("y"))
                lon = float(loc.get("x"))
                coords = [lat, lon]
                cache[address] = coords
                save_cache(cache)
                return coords
    except Exception:
        pass
    return None

def geocode_single_nominatim(address, cache):
    if address in cache:
        return cache[address]
    
    direct_coords = parse_lat_lon_string(address)
    if direct_coords:
        cache[address] = direct_coords
        save_cache(cache)
        return direct_coords

    addr_upper = str(address).upper()
    has_dumfries = "DUMFRIES" in addr_upper or "22026" in addr_upper

    # ArcGIS fallback gate
    arc_coords = geocode_arcgis(address, cache)
    if arc_coords:
        if has_dumfries:
            dist = fast_distance_meters(arc_coords[0], arc_coords[1], 38.56, -77.30)
            if dist < 25000:
                return arc_coords
        else:
            return arc_coords

    geolocator = Nominatim(user_agent="cfs_field_app_geocoder_v30")
    try:
        location = geolocator.geocode(address, addressdetails=True, timeout=6)
        if location:
            lat = float(location.latitude)
            lon = float(location.longitude)
            if has_dumfries:
                dist = fast_distance_meters(lat, lon, 38.56, -77.30)
                if dist > 25000:
                    return None
            coords = [lat, lon]
            cache[address] = coords
            save_cache(cache)
            return coords
    except Exception:
        pass

    return None

def geocode_batch_census(address_list, cache):
    to_lookup = [addr for addr in address_list if addr not in cache]
    if not to_lookup:
        return

    csv_lines = []
    for idx, raw_addr in enumerate(to_lookup):
        addr = strip_unit_designation(raw_addr)
        parts = [p.strip() for p in addr.split(",")]
        street = parts[0] if len(parts) > 0 else ""
        city = parts[1] if len(parts) > 1 else ""
        state_zip = parts[2] if len(parts) > 2 else ""
        state = ""
        zip_code = ""
        if state_zip:
            sz_parts = state_zip.strip().split()
            state = sz_parts[0] if len(sz_parts) > 0 else ""
            zip_code = sz_parts[1] if len(sz_parts) > 1 else ""
        csv_lines.append(f'{idx},"{street}","{city}","{state}","{zip_code}"')

    csv_payload = "\n".join(csv_lines)
    url = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
    files = {'addressFile': ('addresses.csv', csv_payload, 'text/csv')}
    data = {'benchmark': 'Public_AR_Current'}

    try:
        response = requests.post(url, files=files, data=data, timeout=10)
        if response.status_code == 200:
            lines = response.text.strip().split("\n")
            for line in lines:
                cols = [c.strip('"') for c in line.split('","')]
                if len(cols) >= 6 and cols[2].lower() == "match":
                    original_idx = int(cols[0])
                    coords_str = cols[5]
                    lon_str, lat_str = coords_str.split(",")
                    raw_matched_addr = to_lookup[original_idx]
                    cache[raw_matched_addr] = [float(lat_str), float(lon_str)]
    except Exception:
        pass
    save_cache(cache)

def get_coordinates_and_failed_stops(stops_df, depot_address):
    coords = []
    valid_indices = []
    failed_stops = []
    cache = load_cache()
    
    depot_coords = geocode_single_nominatim(depot_address, cache)
    if depot_coords:
        coords.append((depot_coords[0], depot_coords[1], depot_address, "DEPOT"))
    else:
        coords.append((38.8502, -77.0841, depot_address, "DEPOT"))

    if stops_df.empty:
        return coords, valid_indices, failed_stops

    needed_geocoding = []
    for _, row in stops_df.iterrows():
        addr = str(row['Address']).strip()
        lat_val = row.get('Latitude', None)
        lon_val = row.get('Longitude', None)
        
        found = False
        try:
            if pd.notnull(lat_val) and pd.notnull(lon_val) and float(lat_val) != 0:
                cache[addr] = [float(lat_val), float(lon_val)]
                found = True
        except Exception:
            pass

        if not found and addr not in cache:
            needed_geocoding.append(addr)

    if needed_geocoding:
        geocode_batch_census(needed_geocoding, cache)

    for idx, row in stops_df.iterrows():
        insp_id = str(row['Inspection ID']).strip()
        addr = str(row['Address']).strip()
        
        if addr not in cache:
            geocode_single_nominatim(addr, cache)
            
        if addr in cache:
            lat, lon = cache[addr]
            coords.append((lat, lon, addr, insp_id))
            valid_indices.append(idx)
        else:
            failed_stops.append({"Index": idx, "Inspection ID": insp_id, "Address": addr})

    save_cache(cache)
    return coords, valid_indices, failed_stops

@st.cache_data(show_spinner=False)
def build_road_distance_matrix_cached(coords):
    num_pts = len(coords)
    if num_pts <= 1:
        return [], []

    coord_str = ";".join([f"{pt[1]},{pt[0]}" for pt in coords])
    url = f"http://router.project-osrm.org/table/v1/driving/{coord_str}?annotations=distance,duration"
    
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get("code") == "Ok":
                dist_matrix = [[int(val) for val in row] for row in data["distances"]]
                dur_matrix = [[int(val) for val in row] for row in data["durations"]]
                return dist_matrix, dur_matrix
    except Exception:
        pass

    dist_matrix = []
    dur_matrix = []
    for p1 in coords:
        d_row = []
        t_row = []
        for p2 in coords:
            dist = fast_distance_meters(p1[0], p1[1], p2[0], p2[1])
            d_row.append(dist)
            t_row.append(max(60, int(dist / 15.6)))
        dist_matrix.append(d_row)
        dur_matrix.append(t_row)
    return dist_matrix, dur_matrix

@st.cache_data(show_spinner=False)
def get_road_polyline_cached(lat1, lon1, lat2, lon2):
    url = f"http://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=polyline"
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get("code") == "Ok" and len(data["routes"]) > 0:
                geom = data["routes"][0]["geometry"]
                return polyline.decode(geom)
    except Exception:
        pass
    return [(lat1, lon1), (lat2, lon2)]

def optimize_stops_sequence(distance_matrix, lock_first=False, lock_last=False):
    num_stops = len(distance_matrix) - 1
    if num_stops <= 2:
        return list(range(1, num_stops + 1))

    stops = list(range(1, num_stops + 1))
    start_node = stops[0] if lock_first else 0
    end_node = stops[-1] if lock_last else None

    unvisited = [s for s in stops]
    route = []

    if lock_first:
        route.append(start_node)
        unvisited.remove(start_node)

    if lock_last and end_node in unvisited:
        unvisited.remove(end_node)

    current = start_node
    while unvisited:
        next_node = min(unvisited, key=lambda x: distance_matrix[current][x])
        route.append(next_node)
        unvisited.remove(next_node)
        current = next_node

    if lock_last and end_node is not None:
        route.append(end_node)

    start_idx = 1 if lock_first else 0
    end_idx = len(route) - 1 if lock_last else len(route)

    improved = True
    iterations = 0
    while improved and iterations < 50:
        improved = False
        iterations += 1
        for i in range(start_idx, end_idx - 1):
            for k in range(i + 1, end_idx):
                prev_node = 0 if i == 0 else route[i - 1]
                next_node = route[k + 1] if k + 1 < len(route) else 0

                old_dist = distance_matrix[prev_node][route[i]] + distance_matrix[route[k]][next_node]
                new_dist = distance_matrix[prev_node][route[k]] + distance_matrix[route[i]][next_node]

                if new_dist < old_dist:
                    route[i:k + 1] = reversed(route[i:k + 1])
                    improved = True
                    break
            if improved:
                break

    return route

def generate_map(coords, routes, map_tile="OpenStreetMap"):
    if not coords:
        return
    depot_lat, depot_lon = coords[0][0], coords[0][1]
    
    if map_tile == "Esri World Imagery (Satellite)":
        route_map = folium.Map(
            location=[depot_lat, depot_lon], 
            zoom_start=10, 
            tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", 
            attr="Esri World Imagery"
        )
    elif map_tile == "CartoDB Positron (Clean Light)":
        route_map = folium.Map(
            location=[depot_lat, depot_lon], 
            zoom_start=10, 
            tiles="https://{s}.basemaps.cartocdn.light_all/{z}/{x}/{y}{r}.png", 
            attr="CartoDB Positron"
        )
    elif map_tile == "CartoDB Dark Matter (High Contrast)":
        route_map = folium.Map(
            location=[depot_lat, depot_lon], 
            zoom_start=10, 
            tiles="https://{s}.basemaps.cartocdn.light_all/{z}/{x}/{y}{r}.png", 
            attr="CartoDB Dark Matter"
        )
    else:
        route_map = folium.Map(location=[depot_lat, depot_lon], zoom_start=10, tiles="OpenStreetMap")

    colors = ['red', 'blue', 'green', 'purple', 'orange', 'darkred']

    folium.Marker(
        [depot_lat, depot_lon],
        popup="<b>START / DEPOT</b>",
        icon=folium.Icon(color='black', icon='home', prefix='fa')
    ).add_to(route_map)

    for driver_id, path in routes.items():
        driver_color = colors[driver_id % len(colors)]
        
        for stop_num, node_idx in enumerate(path):
            if node_idx >= len(coords):
                continue
            lat, lon, full_addr, inspection_id = coords[node_idx]
            
            if stop_num > 0:
                prev_node = path[stop_num - 1]
                if prev_node < len(coords):
                    plat, plon, _, _ = coords[prev_node]
                    road_points = get_road_polyline_cached(plat, plon, lat, lon)
                    folium.PolyLine(road_points, color=driver_color, weight=5, opacity=0.85).add_to(route_map)

            if node_idx == 0:
                continue

            icon_html = f'''<div style="font-size: 11pt; font-weight: bold; color: white; 
                            background-color: {driver_color}; border-radius: 50%; 
                            width: 26px; height: 26px; text-align: center; line-height: 26px;
                            border: 2px solid white; box-shadow: 2px 2px 4px rgba(0,0,0,0.4);">
                            {stop_num}</div>'''
            
            folium.Marker(
                location=[lat, lon],
                popup=f"<b>Stop #{stop_num}</b><br><b>ID:</b> {inspection_id}<br>{full_addr}",
                icon=folium.DivIcon(html=icon_html)
            ).add_to(route_map)

    route_map.save("route_map.html")

def calculate_schedule(coords, routes, dist_matrix, dur_matrix, start_time_obj, dwell_mins):
    schedules = {}
    for driver_id, path in routes.items():
        current_time = datetime.combine(datetime.today(), start_time_obj)
        driver_schedule = []
        
        for stop_idx in range(len(path)):
            node_idx = path[stop_idx]
            if node_idx >= len(coords):
                continue
            lat, lon, full_addr, inspection_id = coords[node_idx]
            
            if stop_idx == 0:
                driver_schedule.append({
                    "Stop Number": 1,
                    "Inspection ID": "",
                    "Description": f"Start: {full_addr}",
                    "Arrival": current_time.strftime("%I:%M %p"),
                    "Departure": current_time.strftime("%I:%M %p"),
                    "Total Miles": 0.0,
                    "Leg Miles": 0.0,
                    "Latitude": lat,
                    "Longitude": lon
                })
            else:
                prev_node = path[stop_idx - 1]
                if prev_node < len(dist_matrix) and node_idx < len(dist_matrix[prev_node]):
                    dist_meters = dist_matrix[prev_node][node_idx]
                    dur_seconds = dur_matrix[prev_node][node_idx]
                else:
                    dist_meters = 1000
                    dur_seconds = 60
                
                dist_miles = dist_meters / 1609.34
                prev_total = driver_schedule[-1]["Total Miles"]
                running_miles = prev_total + dist_miles
                
                drive_minutes = max(1, int(dur_seconds / 60))
                current_time += timedelta(minutes=drive_minutes)
                arrival_str = current_time.strftime("%I:%M %p")
                
                if node_idx == 0 and stop_idx == len(path) - 1:
                    driver_schedule.append({
                        "Stop Number": stop_idx + 1,
                        "Inspection ID": "",
                        "Description": f"End: {full_addr}",
                        "Arrival": arrival_str,
                        "Departure": "---",
                        "Total Miles": round(running_miles, 2),
                        "Leg Miles": round(dist_miles, 2),
                        "Latitude": lat,
                        "Longitude": lon
                    })
                else:
                    current_time += timedelta(minutes=int(dwell_mins))
                    departure_str = current_time.strftime("%I:%M %p")
                    driver_schedule.append({
                        "Stop Number": stop_idx + 1,
                        "Inspection ID": inspection_id,
                        "Description": full_addr,
                        "Arrival": arrival_str,
                        "Departure": departure_str,
                        "Total Miles": round(running_miles, 2),
                        "Leg Miles": round(dist_miles, 2),
                        "Latitude": lat,
                        "Longitude": lon
                    })
        schedules[driver_id] = pd.DataFrame(driver_schedule)
    return schedules

def export_waypoints_gpx(sched_df):
    gpx_xml = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="RoutePlanner" xmlns="http://www.topografix.com/GPX/1/1">',
        '  <rte>',
        f'    <name>{datetime.now().strftime("%Y-%m-%d")}</name>'
    ]
    seen_ids = set()
    for _, row in sched_df.iterrows():
        try:
            insp_id = str(row['Inspection ID']).strip()
            if not insp_id or insp_id.lower() in ['depot', 'nan'] or insp_id in seen_ids:
                continue
            seen_ids.add(insp_id)

            lat = float(row['Latitude'])
            lon = float(row['Longitude'])
            desc = str(row['Description']).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            
            gpx_xml.append(f'    <rtept lat="{lat}" lon="{lon}">')
            gpx_xml.append(f'      <name>{insp_id}</name>')
            gpx_xml.append(f'      <desc>{desc}</desc>')
            gpx_xml.append('    </rtept>')
        except Exception:
            continue
    gpx_xml.append('  </rte>')
    gpx_xml.append('</gpx>')
    return "\n".join(gpx_xml)

def export_directions_txt(sched_df):
    lines = ["==========================================", f"ROUTE DIRECTIONS - {datetime.now().strftime('%Y-%m-%d')}", "==========================================\n"]
    for _, row in sched_df.iterrows():
        insp_str = f" [ID: {row['Inspection ID']}]" if row['Inspection ID'] else ""
        lines.append(f"Stop {row['Stop Number']}{insp_str}: {row['Description']}")
        lines.append(f"  Arrival: {row['Arrival']}  |  Departure: {row['Departure']}")
        lines.append(f"  Total Distance: {row['Total Miles']} miles")
        lines.append("-" * 40)
    return "\n".join(lines)

def export_mobile_dispatch_html(sched_df, route_name, start_time_obj, dwell_mins):
    stops_payload = []
    for idx, row in sched_df.iterrows():
        insp_id = str(row['Inspection ID']).strip()
        stop_num = int(row['Stop Number'])
        addr = str(row['Description']).strip()
        lat = float(row.get('Latitude', 0.0))
        lon = float(row.get('Longitude', 0.0))
        leg_miles = float(row.get('Leg Miles', 0.0))
        
        # Estimate drive minutes from leg miles
        drive_mins = max(1, int(round((leg_miles / 30.0) * 60))) if idx > 0 else 0

        is_start = (idx == 0)
        is_end = (idx == len(sched_df) - 1)

        stops_payload.append({
            "idx": idx,
            "stop_num": stop_num,
            "insp_id": insp_id,
            "addr": addr,
            "lat": lat,
            "lon": lon,
            "leg_miles": leg_miles,
            "drive_mins": drive_mins,
            "dwell_mins": int(dwell_mins) if not (is_start or is_end) else 0,
            "is_start": is_start,
            "is_end": is_end,
            "original_arrival": row.get('Arrival', '--:--'),
            "nav_url": f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}"
        })

    stops_json = json.dumps(stops_payload)
    total_active = max(0, len(sched_df) - 2)

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>{route_name}</title>
<style>
    * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
    body {{ background-color: #0b0f19; color: #f1f5f9; margin: 0; padding: 12px; }}
    .header-bar {{ position: sticky; top: 0; background-color: #0b0f19; padding: 10px 0 14px 0; z-index: 99; border-bottom: 2px solid #1e293b; }}
    .title {{ font-size: 1.15rem; font-weight: 800; color: #38bdf8; margin: 0; }}
    .dash-metrics {{ display: flex; justify-content: space-between; gap: 8px; margin-top: 10px; }}
    .metric-box {{ background-color: #1e293b; border-radius: 8px; padding: 8px 10px; flex: 1; text-align: center; border: 1px solid #334155; }}
    .metric-box .val {{ font-size: 1.05rem; font-weight: 700; color: #10b981; }}
    .metric-box .lbl {{ font-size: 0.72rem; color: #94a3b8; text-transform: uppercase; }}
    
    .card-list {{ display: flex; flex-direction: column; gap: 12px; margin-top: 14px; }}
    .card {{ background-color: #161e2e; border-radius: 12px; padding: 14px; border: 1px solid #334155; }}
    .stop-card {{ border-left: 5px solid #2563eb; }}
    .depot-card {{ border-left: 5px solid #64748b; }}
    .return-card {{ border-left: 5px solid #10b981; }}
    
    .card-header {{ display: flex; justify-content: space-between; align-items: center; }}
    .badge {{ background-color: #2563eb; color: white; padding: 4px 8px; border-radius: 6px; font-weight: 700; font-size: 0.75rem; }}
    .depot-card .badge {{ background-color: #475569; }}
    .return-card .badge {{ background-color: #059669; }}
    .eta-pill {{ background-color: #0f172a; padding: 4px 8px; border-radius: 6px; font-weight: 700; font-size: 0.85rem; color: #f59e0b; border: 1px solid #334155; }}
    
    .insp-title {{ font-size: 1.05rem; font-weight: 700; color: #e2e8f0; margin: 8px 0 4px 0; }}
    .address {{ font-size: 0.92rem; color: #94a3b8; line-height: 1.35; }}
    .leg-metrics {{ display: flex; flex-wrap: wrap; gap: 10px; background-color: #0f172a; border-radius: 6px; padding: 8px; margin-top: 10px; font-size: 0.78rem; color: #cbd5e1; }}
    
    .btn-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px; }}
    .nav-btn {{ background-color: #0284c7; color: white; text-decoration: none; padding: 12px 10px; border-radius: 8px; font-weight: 700; font-size: 0.9rem; text-align: center; display: block; }}
    .done-btn {{ background-color: #10b981; color: white; border: none; padding: 12px 10px; border-radius: 8px; font-weight: 700; font-size: 0.9rem; cursor: pointer; }}
    .undo-bar {{ margin-top: 16px; text-align: center; }}
    .undo-btn {{ background: none; border: 1px solid #475569; color: #94a3b8; padding: 6px 12px; border-radius: 6px; font-size: 0.8rem; cursor: pointer; }}
</style>
</head>
<body>
    <div class="header-bar">
        <div class="title">🚚 {route_name}</div>
        <div class="dash-metrics">
            <div class="metric-box">
                <div class="val" id="remaining-count">{total_active}</div>
                <div class="lbl">Remaining</div>
            </div>
            <div class="metric-box">
                <div class="val" id="next-drive">--</div>
                <div class="lbl">Next Leg Drive</div>
            </div>
            <div class="metric-box">
                <div class="val" id="office-eta">--:--</div>
                <div class="lbl">Office ETA</div>
            </div>
        </div>
    </div>

    <div class="card-list" id="card-container"></div>
    <div class="undo-bar">
        <button class="undo-btn" onclick="undoLastStop()">↩️ Undo Last Completed Stop</button>
    </div>

<script>
    const stops = {stops_json};
    let completedIndices = [];

    function formatTime(dateObj) {{
        let hours = dateObj.getHours();
        let minutes = dateObj.getMinutes();
        const ampm = hours >= 12 ? 'PM' : 'AM';
        hours = hours % 12;
        hours = hours ? hours : 12;
        minutes = minutes < 10 ? '0' + minutes : minutes;
        return hours + ':' + minutes + ' ' + ampm;
    }}

    function recalculateETAs() {{
        let now = new Date();
        let runningTime = new Date(now.getTime());

        for (let i = 0; i < stops.length; i++) {{
            if (completedIndices.includes(i)) continue;

            // Travel time to reach stop
            runningTime = new Date(runningTime.getTime() + (stops[i].drive_mins * 60000));
            stops[i].live_arrival = formatTime(runningTime);

            // Dwell time on site
            runningTime = new Date(runningTime.getTime() + (stops[i].dwell_mins * 60000));
            stops[i].live_departure = formatTime(runningTime);
        }}

        // Final Return ETA
        const lastStop = stops[stops.length - 1];
        document.getElementById('office-eta').innerText = lastStop.live_arrival || '--:--';
    }}

    function renderCards() {{
        recalculateETAs();
        const container = document.getElementById('card-container');
        container.innerHTML = '';

        let remaining = stops.filter((s, idx) => !completedIndices.includes(idx) && !s.is_start && !s.is_end).length;
        document.getElementById('remaining-count').innerText = remaining;

        let visibleStops = stops.filter((s, idx) => !completedIndices.includes(idx));

        if (visibleStops.length > 0) {{
            document.getElementById('next-drive').innerText = visibleStops[0].drive_mins + ' min';
        }} else {{
            document.getElementById('next-drive').innerText = '0 min';
        }}

        visibleStops.forEach(s => {{
            let card = document.createElement('div');
            let cardClass = s.is_start ? 'depot-card' : (s.is_end ? 'depot-card return-card' : 'stop-card');
            card.className = 'card ' + cardClass;

            let badgeLabel = s.is_start ? 'DEPOT START' : (s.is_end ? 'DEPOT RETURN' : 'STOP #' + (s.stop_num - 1));
            let titleText = s.is_start ? 'BASE / OFFICE DEPARTURE' : (s.is_end ? 'FINAL RETURN TO BASE' : 'ID: ' + s.insp_id);

            card.innerHTML = `
                <div class="card-header">
                    <span class="badge">${{badgeLabel}}</span>
                    <span class="eta-pill">ETA: ${{s.live_arrival}}</span>
                </div>
                <div class="insp-title">${{titleText}}</div>
                <div class="address">${{s.addr}}</div>
                <div class="leg-metrics">
                    <span>🚗 Leg: ${{s.leg_miles}} mi (~${{s.drive_mins}}m)</span>
                    <span>⏱️ Stay: ${{s.dwell_mins}}m</span>
                </div>
                <div class="btn-row">
                    <a href="${{s.nav_url}}" target="_blank" class="nav-btn">🗺️ Navigate</a>
                    ${{s.is_end ? '' : `<button class="done-btn" onclick="completeStop(${{s.idx}})">✅ Done / Next</button>`}}
                </div>
            `;
            container.appendChild(card);
        }});
    }}

    function completeStop(idx) {{
        completedIndices.push(idx);
        renderCards();
    }}

    function undoLastStop() {{
        if (completedIndices.length > 0) {{
            completedIndices.pop();
            renderCards();
        }}
    }}

    window.onload = renderCards;
</script>
</body>
</html>"""
    return full_html

# ==========================================
# 2. STREAMLIT UI
# ==========================================
st.set_page_config(page_title="Route Planner & Mobile App", layout="wide")

if 'processed_uploads' not in st.session_state:
    st.session_state['processed_uploads'] = set()

if 'completed_stops' not in st.session_state:
    st.session_state['completed_stops'] = set()

def load_persisted_stops():
    if os.path.exists(PERSISTENT_FILE):
        try:
            df = pd.read_csv(PERSISTENT_FILE)
            if not df.empty and 'Address' in df.columns:
                return df.drop_duplicates(subset=['Inspection ID'], keep='last').reset_index(drop=True)
        except Exception:
            pass
    return pd.DataFrame(columns=['Inspection ID', 'Address'])

def persist_stops(df):
    clean_df = df.drop_duplicates(subset=['Inspection ID'], keep='last').dropna(subset=['Address']).reset_index(drop=True)
    clean_df.to_csv(PERSISTENT_FILE, index=False)
    return clean_df

@st.cache_data
def search_address(query):
    if not query or len(query.strip()) < 3:
        return []
    geolocator = Nominatim(user_agent="cfs_address_search_bar_v30")
    try:
        locations = geolocator.geocode(query, exactly_one=False, limit=6)
        if locations:
            return [loc.address for loc in locations]
    except Exception:
        pass
    return []

# Sidebar Screen View Selector
st.sidebar.markdown("### 🔀 Display View")
view_mode = st.sidebar.radio(
    "Choose Interface Mode:",
    ["🖥️ Desktop Planner", "📱 Mobile Driver Deck"],
    index=0
)

# Sidebar Settings
st.sidebar.markdown("---")
st.sidebar.header("⚙️ Route Settings")

st.sidebar.markdown("### 📍 Starting Point (Depot)")
start_input = st.sidebar.text_input("Starting Address / Base:", "2644 S Shirlington Rd, Arlington, VA")
start_suggestions = search_address(start_input)
depot_address = st.sidebar.selectbox("Select Matched Start Address:", start_suggestions, index=0) if start_suggestions else start_input

start_time = st.sidebar.time_input("Route Start Time:", value=time(8, 0))
stop_duration = st.sidebar.number_input("Inspection Time per Stop (mins):", min_value=1, max_value=120, value=5)

st.sidebar.subheader("🗺️ Map Style")
map_style = st.sidebar.selectbox(
    "Select Map Style:",
    ["OpenStreetMap", "Esri World Imagery (Satellite)", "CartoDB Positron (Clean Light)", "CartoDB Dark Matter (High Contrast)"]
)

# ----------------- RECALL SAVED ROUTE -----------------
st.sidebar.markdown("---")
st.sidebar.subheader("📂 Recall Saved Route")
saved_route_files = sorted(glob.glob(os.path.join(SAVED_DIR, "*.csv")), key=os.path.getmtime, reverse=True)

if saved_route_files:
    file_options = ["-- Select a saved route --"] + [os.path.basename(f) for f in saved_route_files]
    selected_saved = st.sidebar.selectbox("Choose From Saved Routes:", file_options)
    
    if st.sidebar.button("📥 Open Route", key="btn_load_saved"):
        if selected_saved != "-- Select a saved route --":
            target_path = os.path.join(SAVED_DIR, selected_saved)
            try:
                loaded_df = pd.read_csv(target_path)
                recalled_rows = []
                for _, r in loaded_df.iterrows():
                    insp_id = str(r['Name']).strip() if 'Name' in r else (str(r['Inspection ID']).strip() if 'Inspection ID' in r else str(r.iloc[0]).strip())
                    addr = str(r['Address']).strip() if 'Address' in r else str(r.iloc[1]).strip()
                    lat_val = r.get('Latitude', None)
                    lon_val = r.get('Longitude', None)
                    
                    if addr and str(addr).lower() != 'nan' and str(insp_id).lower() not in ['depot', 'start/end depot', 'nan']:
                        row_dict = {"Inspection ID": insp_id, "Address": addr}
                        if pd.notnull(lat_val):
                            row_dict["Latitude"] = lat_val
                        if pd.notnull(lon_val):
                            row_dict["Longitude"] = lon_val
                        recalled_rows.append(row_dict)
                
                if recalled_rows:
                    new_master = pd.DataFrame(recalled_rows)
                    persist_stops(new_master)
                    st.toast(f"Opened {selected_saved} instantly!", icon="📂")
                    st.rerun()
            except Exception as e:
                st.sidebar.error(f"Error loading file: {e}")
else:
    st.sidebar.caption("No saved routes found in folder yet.")

# ----------------- IMPORT SPREADSHEETS -----------------
st.sidebar.markdown("---")
st.sidebar.subheader("📁 Import CSV Files")
uploaded_files = st.sidebar.file_uploader(
    "Upload Spreadsheets", 
    type=["csv"], 
    accept_multiple_files=True
)

master_df = load_persisted_stops()

if uploaded_files:
    new_rows = []
    for f in uploaded_files:
        if f.name not in st.session_state['processed_uploads']:
            try:
                raw_df = pd.read_csv(f)
                raw_df.columns = [str(c).strip() for c in raw_df.columns]
                
                if 'Name' in raw_df.columns and 'Address' in raw_df.columns:
                    for _, r in raw_df.iterrows():
                        insp_id = str(r['Name']).strip()
                        addr = str(r['Address']).strip()
                        if addr and insp_id.lower() not in ['depot', 'start/end depot', 'nan']:
                            new_rows.append({"Inspection ID": insp_id, "Address": addr})
                else:
                    for idx, row in raw_df.iterrows():
                        insp_id = str(row.iloc[0]).strip() if len(row) > 0 else f"ID_{idx+1}"
                        addr1 = str(row.get('Address1', '')) if 'Address1' in row else ''
                        city = str(row.get('City', '')) if 'City' in row else ''
                        state = str(row.get('State', '')) if 'State' in row else ''
                        zip_c = str(row.get('Zip', '')) if 'Zip' in row else ''
                        
                        full_addr = f"{addr1}, {city}, {state} {zip_c}".strip(", ")
                        if not full_addr or full_addr == ",":
                            full_addr = str(row.iloc[1]).strip() if len(row) > 1 else insp_id
                        
                        if full_addr and full_addr.lower() != 'nan':
                            new_rows.append({"Inspection ID": insp_id, "Address": full_addr})
                st.session_state['processed_uploads'].add(f.name)
            except Exception:
                continue

    if new_rows:
        master_df = pd.concat([master_df, pd.DataFrame(new_rows)], ignore_index=True)
        master_df = persist_stops(master_df)

# Route Controls
st.sidebar.markdown("---")
st.sidebar.subheader("🔄 Route Controls")
btn_reverse = st.sidebar.button("⇄ Reverse Entire Route Order", width='stretch')

# ----------------- ADD STOP MANUALLY -----------------
with st.sidebar.expander("➕ Search & Add Stop Manually", expanded=False):
    manual_insp_id = st.text_input("Inspection ID (Optional):", key="m_id_input")
    manual_search_text = st.text_input("Type Address or Lat,Lon (Press Enter):", key="m_addr_search")
    
    matched_results = []
    if len(manual_search_text.strip()) >= 3 and not parse_lat_lon_string(manual_search_text):
        matched_results = search_address(manual_search_text)

    if matched_results:
        final_address_choice = st.selectbox(
            "📍 Select Closest Matched Address:", 
            matched_results, 
            key="sb_matched_addr"
        )
    else:
        final_address_choice = manual_search_text

    if st.button("➕ Add This Stop to Route", key="btn_add_live", type="primary", width='stretch'):
        addr_to_save = final_address_choice.strip() if final_address_choice else manual_search_text.strip()
        if addr_to_save:
            current_records = load_persisted_stops()
            assigned_id = manual_insp_id.strip() if manual_insp_id.strip() else f"ADD_{len(current_records)+1}"
            
            new_entry = pd.DataFrame([{"Inspection ID": assigned_id, "Address": addr_to_save}])
            updated_master = pd.concat([current_records, new_entry], ignore_index=True)
            persist_stops(updated_master)
            st.toast(f"Added {assigned_id} to route!", icon="📍")
            st.rerun()
        else:
            st.warning("Please type an address before adding.")

# Clear Route
st.sidebar.markdown("---")
if st.sidebar.button("🔄 Clear Active Route & Start Fresh"):
    if os.path.exists(PERSISTENT_FILE):
        try:
            os.remove(PERSISTENT_FILE)
        except Exception:
            pass
    st.session_state['processed_uploads'] = set()
    st.session_state['completed_stops'] = set()
    st.rerun()

# ----------------- MAIN SCREEN EXECUTION -----------------
master_df = load_persisted_stops()

if not master_df.empty:
    coords, valid_indices, failed_stops = get_coordinates_and_failed_stops(master_df, depot_address)
    
    # ----------------- UNRESOLVED ADDRESSES WARNING & GUIDED FIX -----------------
    if failed_stops:
        st.error(f"⚠️ **Attention: {len(failed_stops)} Address(es) could not be mapped automatically!**")
        with st.expander("🛠️ Click Here to Review & Fix Unresolved Addresses", expanded=True):
            st.info("💡 **Format Tip:** Ensure addresses have a number, street, city, state, and zip (e.g. `123 Main St, Manassas, VA 20110`).")
            
            for item in failed_stops:
                f_idx = item['Index']
                f_id = item['Inspection ID']
                f_addr = item['Address']
                suggested_val, reasons = suggest_address_cleanup(f_addr)
                
                st.markdown(f"#### 🛑 Stop ID: `{f_id}`")
                if reasons:
                    st.caption(f"**Diagnostic Detected:** {' | '.join(reasons)}")
                
                col_input, col_sugg, col_action = st.columns([4, 3, 1])
                corrected_text = col_input.text_input(f"Edit address for {f_id}:", value=f_addr, key=f"fix_in_{f_idx}")
                
                if suggested_val != f_addr:
                    if col_sugg.button(f"✨ Apply: {suggested_val[:22]}...", key=f"btn_sugg_{f_idx}", help=f"Click to use: {suggested_val}"):
                        master_df.at[f_idx, 'Address'] = suggested_val
                        persist_stops(master_df)
                        st.toast(f"Applied suggestion for {f_id}!", icon="✨")
                        st.rerun()
                else:
                    col_sugg.write("")

                if col_action.button("💾 Fix", key=f"btn_fix_{f_idx}"):
                    master_df.at[f_idx, 'Address'] = corrected_text.strip()
                    persist_stops(master_df)
                    st.toast(f"Updated {f_id}!", icon="✅")
                    st.rerun()
                st.markdown("---")

    valid_master_df = master_df.iloc[valid_indices].reset_index(drop=True)
    
    # ----------------- REVERSE ROUTE LOGIC -----------------
    if btn_reverse and not valid_master_df.empty:
        valid_master_df = valid_master_df.iloc[::-1].reset_index(drop=True)
        persist_stops(valid_master_df)
        st.rerun()

    if not valid_master_df.empty and len(coords) > 1:
        dist_matrix, dur_matrix = build_road_distance_matrix_cached(coords)
        
        # ----------------- HORIZONTAL ALIGNED AUTO-OPTIMIZER BAR -----------------
        st.markdown("### ⚡ Route Direction & Optimization")
        
        stop_options = ["-- Auto-Pick Closest Stop --"] + [
            f"#{i+1}: {r['Inspection ID']} ({str(r['Address'])[:22]}...)" 
            for i, r in valid_master_df.iterrows()
        ]

        with st.form("optimizer_control_form"):
            c_start, c_end, c_btn = st.columns([4, 4, 3], gap="medium")
            selected_first_opt = c_start.selectbox("📍 Lock First Stop:", stop_options, index=0)
            selected_last_opt = c_end.selectbox("🏁 Lock Last Stop:", stop_options, index=0)
            
            c_btn.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
            btn_do_optimize = c_btn.form_submit_button("⚡ Auto-Optimize Route", type="primary", width='stretch')

            if btn_do_optimize:
                first_row_id = None
                if selected_first_opt != "-- Auto-Pick Closest Stop --":
                    raw_first_idx = int(selected_first_opt.split(":")[0].replace("#", "")) - 1
                    first_row_id = valid_master_df.iloc[raw_first_idx]['Inspection ID']

                last_row_id = None
                if selected_last_opt != "-- Auto-Pick Closest Stop --":
                    raw_last_idx = int(selected_last_opt.split(":")[0].replace("#", "")) - 1
                    last_row_id = valid_master_df.iloc[raw_last_idx]['Inspection ID']

                if first_row_id is not None:
                    match_first = valid_master_df[valid_master_df['Inspection ID'] == first_row_id]
                    rest = valid_master_df[valid_master_df['Inspection ID'] != first_row_id]
                    valid_master_df = pd.concat([match_first, rest]).reset_index(drop=True)

                if last_row_id is not None and last_row_id != first_row_id:
                    match_last = valid_master_df[valid_master_df['Inspection ID'] == last_row_id]
                    rest = valid_master_df[valid_master_df['Inspection ID'] != last_row_id]
                    valid_master_df = pd.concat([rest, match_last]).reset_index(drop=True)

                coords, valid_indices, _ = get_coordinates_and_failed_stops(valid_master_df, depot_address)
                dist_matrix, dur_matrix = build_road_distance_matrix_cached(coords)

                optimized_nodes = optimize_stops_sequence(
                    dist_matrix, 
                    lock_first=(first_row_id is not None), 
                    lock_last=(last_row_id is not None)
                )

                reordered_indices = [n - 1 for n in optimized_nodes]
                valid_master_df = valid_master_df.iloc[reordered_indices].reset_index(drop=True)
                persist_stops(valid_master_df)
                st.toast("Route optimized with locked start/finish!", icon="⚡")
                st.rerun()

        num_valid = len(coords) - 1
        routes = {0: [0] + list(range(1, num_valid + 1)) + [0]}

        generate_map(coords, routes, map_tile=map_style)
        schedules = calculate_schedule(coords, routes, dist_matrix, dur_matrix, start_time, stop_duration)
        sched_df = schedules[0]
        custom_route_name = f"Route_{datetime.now().strftime('%Y%m%d_%H%M')}"

        # =========================================================================
        # VIEW 1: DESKTOP PLANNER (ORIGINAL CONTROL SETUP)
        # =========================================================================
        if view_mode == "🖥️ Desktop Planner":
            col_list, col_map = st.columns([1, 2], gap="small")
            
            with col_list:
                st.markdown(f"### 📋 Manage Stops ({len(valid_master_df)} Active)")
                st.caption("Check boxes to drop stops, then click remove:")

                table_rows = []
                for i, r in valid_master_df.iterrows():
                    table_rows.append({
                        "Drop?": False,
                        "Stop #": i + 1,
                        "Inspection ID": str(r["Inspection ID"]),
                        "Address": str(r["Address"])
                    })
                edit_table = pd.DataFrame(table_rows)

                with st.form("bulk_prune_form", clear_on_submit=False):
                    edited_df = st.data_editor(
                        edit_table,
                        column_config={
                            "Drop?": st.column_config.CheckboxColumn("Drop?", default=False),
                            "Stop #": st.column_config.NumberColumn("#", width="small"),
                            "Inspection ID": st.column_config.TextColumn("ID", width="medium"),
                            "Address": st.column_config.TextColumn("Address", width="medium"),
                        },
                        disabled=["Stop #", "Inspection ID", "Address"],
                        hide_index=True,
                        width='stretch',
                        height=750,
                        key="side_by_side_editor"
                    )

                    submit_prune = st.form_submit_button("🗑️ Remove Selected Stops", type="secondary", width='stretch')
                    
                    if submit_prune:
                        to_remove_ids = edited_df[edited_df["Drop?"] == True]["Inspection ID"].tolist()
                        if to_remove_ids:
                            valid_master_df = valid_master_df[~valid_master_df["Inspection ID"].isin(to_remove_ids)].reset_index(drop=True)
                            persist_stops(valid_master_df)
                            st.toast(f"Removed {len(to_remove_ids)} inspections!", icon="🗑️")
                            st.rerun()
                        else:
                            st.info("Check at least one stop box before clicking remove.")

            with col_map:
                st.markdown("### 🗺️ Live Route Map")
                if os.path.exists("route_map.html"):
                    with open("route_map.html", "r", encoding="utf-8") as f:
                        html_data = f.read()
                        components.html(html_data, height=860)

            # ----------------- ITINERARY TABLE & NAV LINK -----------------
            st.markdown("---")
            route_name_str = f"Driver 1 - {datetime.now().strftime('%Y-%m-%d')}"
            st.markdown(f"## View Route Itinerary - Driver 1")
            st.caption(f"**Route Name:** `{route_name_str}` | **Planned Date:** {datetime.now().strftime('%m/%d/%Y')}")

            sched_df_display = sched_df.copy()
            sched_df_display['Navigate'] = sched_df_display.apply(
                lambda r: f"https://www.google.com/maps/dir/?api=1&destination={r['Latitude']},{r['Longitude']}" 
                if r['Latitude'] != 0 else "", 
                axis=1
            )

            st.dataframe(
                sched_df_display[['Stop Number', 'Inspection ID', 'Description', 'Arrival', 'Total Miles', 'Navigate']], 
                column_config={
                    "Navigate": st.column_config.LinkColumn("Google Maps", display_text="🗺️ Open Maps")
                },
                width='stretch', 
                hide_index=True
            )

            # ----------------- ROUTE OPERATIONAL SUMMARY -----------------
            final_row = sched_df.iloc[-1]
            first_row = sched_df.iloc[0]
            total_miles = final_row['Total Miles']
            
            t_start = datetime.combine(datetime.today(), start_time)
            t_end_parsed = datetime.strptime(final_row['Arrival'], "%I:%M %p")
            t_end = datetime.combine(datetime.today(), t_end_parsed.time())
            if t_end < t_start:
                t_end += timedelta(days=1)
                
            total_elapsed_minutes = int((t_end - t_start).total_seconds() / 60)
            elapsed_hrs = total_elapsed_minutes // 60
            elapsed_mins = total_elapsed_minutes % 60
            
            num_inspections = max(0, len(sched_df) - 2)
            total_on_site_mins = num_inspections * int(stop_duration)
            total_drive_mins = max(0, total_elapsed_minutes - total_on_site_mins)
            drive_hrs = total_drive_mins // 60
            drive_rem_mins = total_drive_mins % 60

            st.markdown("### 📊 Route Operational Summary")
            col_m1, col_m2, col_m3, col_m4 = st.columns(4)
            col_m1.metric("🚗 Total Mileage", f"{total_miles:.1f} mi")
            col_m2.metric("⏱️ Total Time on Road", f"{elapsed_hrs}h {elapsed_mins}m")
            col_m3.metric("🚙 Pure Driving Time", f"{drive_hrs}h {drive_rem_mins}m")
            col_m4.metric("🏁 Return to Base ETA", final_row['Arrival'])

            st.markdown(
                f"""
                <div style="background-color: #1e293b; padding: 12px 18px; border-radius: 8px; border-left: 4px solid #38bdf8; margin-top: 10px; margin-bottom: 20px; font-size: 0.92rem; color: #cbd5e1;">
                    📌 <b>Run Details:</b> Departed base at <b>{first_row['Arrival']}</b> across <b>{num_inspections}</b> active inspections ({total_on_site_mins} mins on site at {stop_duration} min/stop) and driving <b>{total_miles:.1f} miles</b>. Estimated completion and return back to base: <b>{final_row['Arrival']}</b>.
                </div>
                """,
                unsafe_allow_html=True
            )

        # =========================================================================
        # VIEW 2: MOBILE DRIVER DECK (TOUCH-FIRST FIELD APP MODE)
        # =========================================================================
        elif view_mode == "📱 Mobile Driver Deck":
            final_row = sched_df.iloc[-1]
            total_stops = max(0, len(sched_df) - 2)
            
            st.markdown(f"## 📱 Mobile Driver Deck")
            st.caption(f"**Route:** {custom_route_name} | Tap any stop to launch turn-by-turn navigation")

            c_mb1, c_mb2, c_mb3 = st.columns(3)
            c_mb1.metric("📍 Total Stops", f"{total_stops}")
            c_mb2.metric("🚗 Total Route", f"{final_row['Total Miles']:.1f} mi")
            c_mb3.metric("🏁 Office ETA", f"{final_row['Arrival']}")
            st.markdown("---")
            # --- LIVE FLOATING TRACKER & ACTION HUB ---
            target_df = sched_df if "sched_df" in locals() and sched_df is not None else None

            if target_df is not None:
                  now_live = datetime.utcnow() - timedelta(hours=4)
                  if "route_start_time" not in st.session_state:
                      st.session_state.route_start_time = None
                  if "completed_stops" not in st.session_state:
                      st.session_state.completed_stops = set()

                  completed_cnt = len(st.session_state.completed_stops)
                  total_stops = len(target_df)
                  rem_cnt = max(0, total_stops - completed_cnt)

                  proj_finish = now_live + timedelta(minutes=rem_cnt * 10)
                  finish_str = proj_finish.strftime("%I:%M %p").lstrip("0")

                  st.markdown(
            f"""
            <div style="position: sticky; top: 0; z-index: 999; background: #111827; color: white; padding: 12px 16px; border-radius: 8px; margin-bottom: 15px; border-left: 5px solid #2563eb;">
                <div style="display: flex; justify-content: space-between; align-items: center; font-size: 1.05rem; font-weight: bold;">
                    <span>📍 Active Progress: Stop {completed_cnt} of {total_stops} ({rem_cnt} remaining)</span>
                    <span>🏁 Projected Finish: {finish_str}</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        c_btn1, c_btn2 = st.columns(2)
        with c_btn1:
            if st.button("🚀 Start / Sync Route (Now)"):
                st.session_state.route_start_time = now_live
                st.success(f"Route marked active at {now_live.strftime('%I:%M %p')}")
        with c_btn2:
            if st.button("🔄 Reset Progress"):
                st.session_state.completed_stops = set()
                st.session_state.route_start_time = None
                st.rerun()

            st.markdown("---")
            target_df = sched_df if "sched_df" in locals() and sched_df is not None else None
            if target_df is None and "route_df" in locals(): target_df = route_df
            if target_df is None and "df" in locals(): target_df = df
            # --- SAVE & EXPORT TOOLS ---
            st.subheader("💾 Save Route & Export Data")
            exp_col1, exp_col2, exp_col3, exp_col4 = st.columns(4)
    
            # 1. Save Route
            with exp_col1:
                route_save_name = st.text_input("Route Name", key="save_rt_name")
                if st.button("💾 Save Route"):
                os.makedirs("saved_routes", exist_ok=True)
                save_path = os.path.join("saved_routes", f"{route_save_name}.csv")
                target_df.to_csv(save_path, index=False)
                st.success("Route saved successfully!")
                st.rerun()
            # 2. CSV Export
            with exp_col2:
                csv_bytes = target_df.to_csv(index=False).encode("utf-8")
                st.download_button(
                    label="📥 Download CSV",
                    data=csv_bytes,
                    file_name=f"{route_save_name}.csv",
                    mime="text/csv",
                    key="dl_btn_csv",
                )
    
            # 3. GPX Download
            with exp_col3:
                gpx_lines = [
                    '<?xml version="1.0" encoding="UTF-8"?>',
                    '<gpx version="1.1" creator="RoutePlanner">'
                ]
                for _, row in target_df.iterrows():
                    lat = row.get("Latitude") or row.get("lat") or row.get("Lat") or 0.0
                    lon = row.get("Longitude") or row.get("lon") or row.get("Lon") or row.get("Lng") or 0.0
                    addr = row.get("Address") or row.get("Full Address") or row.get("Street") or "Stop"
                    clean_addr = str(addr).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    gpx_lines.append(f'  <wpt lat="{lat}" lon="{lon}"><name>{clean_addr}</name></wpt>')
                gpx_lines.append('</gpx>')
                gpx_string = "\n".join(gpx_lines)
                
                st.download_button(
                    label="🗺️ Download GPX",
                    data=gpx_string,
                    file_name=f"{route_save_name}.gpx",
                    mime="application/gpx+xml",
                    key="dl_btn_gpx",
                )
    
            # 4. InspectorAde File Export
            with exp_col4:
                ade_lines = ["OrderNumber,Address,City,State,Zip"]
                for _, row in target_df.iterrows():
                    order = row.get("Order_Number") or row.get("Work_Order") or row.get("Order") or ""
                    addr = row.get("Address") or row.get("Street") or ""
                    city = row.get("City") or ""
                    state = row.get("State") or ""
                    zip_code = row.get("Zip") or row.get("PostalCode") or ""
                    ade_lines.append(f'"{order}","{addr}","{city}","{state}","{zip_code}"')
                ade_csv = "\n".join(ade_lines).encode("utf-8")
                st.download_button(
                    label="📋 InspectorAde",
                    data=ade_csv,
                    file_name=f"{route_save_name}_InspectorAde.csv",
                    mime="text/csv",
                    key="dl_btn_inspectorade",
                )
    
            st.markdown("---")
    
            # --- PRINTABLE CLIPBOARD MANIFEST ---
            with st.expander("🖨️ Open Printable Clipboard Manifest"):
                st.button("Print Manifest", on_click=None, help="Use browser Print (Ctrl+P)")
                display_cols = [c for c in target_df.columns if c in ["Order_Number", "Work_Order", "Address", "Full Address", "Arrival", "Departure", "Total Miles", "Miles", "Duration"]]
                if display_cols:
                    st.dataframe(target_df[display_cols], use_container_width=True)
                else:
                    st.dataframe(target_df, use_container_width=True)
