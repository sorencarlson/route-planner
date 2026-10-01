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
import streamlit as st
import streamlit.components.v1 as components

# =========================================================================
# 1. CORE ENGINE & GEOCODING CACHE
# =========================================================================
CACHE_FILE = "address_cache.json"
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
    pattern = r"^\s*([+-]?\d{1,2}(?:\.\d+)?)\s*,\s*([+-]?\d{1,3}(?:\.\d+)?)\s*$"
    match = re.match(pattern, str(text).strip())
    if match:
        try:
            lat = float(match.group(1))
            lon = float(match.group(2))
            if 36.0 <= lat <= 40.5 and -80.0 <= lon <= -75.0:
                return [lat, lon]
        except Exception:
            pass
    return None


def strip_unit_designation(address):
    addr_clean = re.sub(r"/\s*\d+", "", str(address))
    addr_clean = re.sub(r"#\s*[\w-]+", "", addr_clean)
    addr_clean = re.sub(r"(?i)\bN\s+W\b", "NW", addr_clean)
    addr_clean = re.sub(r"(?i)\bN\s+E\b", "NE", addr_clean)
    addr_clean = re.sub(r"(?i)\bS\s+W\b", "SW", addr_clean)
    addr_clean = re.sub(r"(?i)\bS\s+E\b", "SE", addr_clean)
    addr_clean = re.sub(r"(?i)\b(NW|NE|SW|SE)\s+\d+\b", r"\1", addr_clean)
    unit_pattern = r"(?i)\b(apt|apartment|unit|ste|suite|bldg|building|fl|floor|dept|lot|rm|room|bsmt|basement|spc|space|trailer)\.?\s*[\w#-]+"
    addr_clean = re.sub(unit_pattern, "", addr_clean)
    addr_clean = re.sub(r",\s*,", ",", addr_clean)
    addr_clean = re.sub(r"\s{2,}", " ", addr_clean).strip(" ,/")
    return addr_clean


def suggest_address_cleanup(raw_address):
    cleaned = str(raw_address).strip()
    reasons = []

    if "/" in cleaned:
        cleaned = re.sub(r"/\s*\d+", "", cleaned).strip(" /")
        reasons.append("Stripped appended Work Order / ID number")

    cleaned_spaced = re.sub(r"(?i)\bN\s+W\b", "NW", cleaned)
    cleaned_spaced = re.sub(r"(?i)\bN\s+E\b", "NE", cleaned_spaced)
    cleaned_spaced = re.sub(r"(?i)\bS\s+W\b", "SW", cleaned_spaced)
    cleaned_spaced = re.sub(r"(?i)\bS\s+E\b", "SE", cleaned_spaced)
    if cleaned_spaced != cleaned:
        cleaned = cleaned_spaced
        reasons.append("Fixed spaced directional (e.g. 'N W' -> 'NW')")

    cleaned_quad_unit = re.sub(r"(?i)\b(NW|NE|SW|SE)\s+\d+\b", r"\1", cleaned)
    if cleaned_quad_unit != cleaned:
        cleaned = cleaned_quad_unit
        reasons.append("Stripped bare unit number after directional")

    cleaned_unit = strip_unit_designation(cleaned)
    if cleaned_unit != cleaned:
        reasons.append("Parsed base street address without unit/apartment number")

    return cleaned, reasons


def fast_distance_meters(lat1, lon1, lat2, lon2):
    dlat = (lat2 - lat1) * 111000
    dlon = (lon2 - lon1) * 111000 * math.cos(math.radians((lat1 + lat2) / 2))
    return int(math.sqrt(dlat * dlat + dlon * dlon))


def geocode_arcgis(address, cache):
    if address in cache:
        return cache[address]
    try:
        clean_addr = strip_unit_designation(address)
        if not re.search(r"(?i)\b(VA|MD|DC|USA|Virginia|Maryland)\b", clean_addr):
            clean_addr = f"{clean_addr}, USA"

        encoded_query = urllib.parse.quote(clean_addr)
        url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={encoded_query}&maxLocations=1&sourceCountry=USA"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            candidates = data.get("candidates", [])
            if candidates:
                loc = candidates[0].get("location", {})
                lat = float(loc.get("y"))
                lon = float(loc.get("x"))
                if 24.0 <= lat <= 50.0 and -125.0 <= lon <= -66.0:
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

    arc_coords = geocode_arcgis(address, cache)
    if arc_coords:
        return arc_coords

    geolocator = Nominatim(user_agent="cfs_field_geocoder_us_v38")
    try:
        clean_addr = strip_unit_designation(address)
        location = geolocator.geocode(
            clean_addr,
            country_codes="us",
            viewbox=[(-83.7, 36.5), (-75.0, 40.0)],
            bounded=False,
            timeout=6,
        )
        if location:
            lat = float(location.latitude)
            lon = float(location.longitude)
            if 24.0 <= lat <= 50.0 and -125.0 <= lon <= -66.0:
                coords = [lat, lon]
                cache[address] = coords
                save_cache(cache)
                return coords
    except Exception:
        pass
    return None


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

    for idx, row in stops_df.iterrows():
        insp_id = str(row["Inspection ID"]).strip()
        addr = str(row["Address"]).strip()

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


def build_road_distance_matrix_cached(coords):
    num_pts = len(coords)
    if num_pts <= 1:
        return [], []

    coord_str = ";".join([f"{pt[1]},{pt[0]}" for pt in coords])
    url = f"https://router.project-osrm.org/table/v1/driving/{coord_str}?annotations=distance,duration"

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

    dist_matrix, dur_matrix = [], []
    for p1 in coords:
        d_row, t_row = [], []
        for p2 in coords:
            dist = fast_distance_meters(p1[0], p1[1], p2[0], p2[1])
            d_row.append(dist)
            t_row.append(max(60, int(dist / 15.6)))
        dist_matrix.append(d_row)
        dur_matrix.append(t_row)
    return dist_matrix, dur_matrix


@st.cache_data(show_spinner=False)
def get_road_polyline_cached(lat1, lon1, lat2, lon2):
    url = f"https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=polyline"
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
                    route[i : k + 1] = reversed(route[i : k + 1])
                    improved = True
                    break
            if improved:
                break

    return route


def generate_map(coords, routes, map_tile="OpenStreetMap"):
    if not coords:
        return
    depot_lat, depot_lon = coords[0][0], coords[0][1]

    route_map = folium.Map(location=[depot_lat, depot_lon], zoom_start=10, tiles=map_tile)
    colors = ["red", "blue", "green", "purple", "orange", "darkred"]

    folium.Marker(
        [depot_lat, depot_lon],
        popup="<b>START / DEPOT</b>",
        icon=folium.Icon(color="black", icon="home", prefix="fa"),
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

            icon_html = f"""<div style="font-size: 11pt; font-weight: bold; color: white; 
                            background-color: {driver_color}; border-radius: 50%; 
                            width: 26px; height: 26px; text-align: center; line-height: 26px;
                            border: 2px solid white; box-shadow: 2px 2px 4px rgba(0,0,0,0.4);">
                            {stop_num}</div>"""

            folium.Marker(
                location=[lat, lon],
                popup=f"<b>Stop #{stop_num}</b><br><b>ID:</b> {inspection_id}<br>{full_addr}",
                icon=folium.DivIcon(html=icon_html),
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
                driver_schedule.append(
                    {
                        "Stop Number": 1,
                        "Inspection ID": "",
                        "Description": f"Start: {full_addr}",
                        "Arrival": current_time.strftime("%I:%M %p"),
                        "Departure": current_time.strftime("%I:%M %p"),
                        "Total Miles": 0.0,
                        "Leg Miles": 0.0,
                        "Latitude": lat,
                        "Longitude": lon,
                    }
                )
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
                    driver_schedule.append(
                        {
                            "Stop Number": stop_idx + 1,
                            "Inspection ID": "",
                            "Description": f"End: {full_addr}",
                            "Arrival": arrival_str,
                            "Departure": "---",
                            "Total Miles": round(running_miles, 2),
                            "Leg Miles": round(dist_miles, 2),
                            "Latitude": lat,
                            "Longitude": lon,
                        }
                    )
                else:
                    current_time += timedelta(minutes=int(dwell_mins))
                    departure_str = current_time.strftime("%I:%M %p")
                    driver_schedule.append(
                        {
                            "Stop Number": stop_idx + 1,
                            "Inspection ID": inspection_id,
                            "Description": full_addr,
                            "Arrival": arrival_str,
                            "Departure": departure_str,
                            "Total Miles": round(running_miles, 2),
                            "Leg Miles": round(dist_miles, 2),
                            "Latitude": lat,
                            "Longitude": lon,
                        }
                    )
        schedules[driver_id] = pd.DataFrame(driver_schedule)
    return schedules


def export_waypoints_gpx(sched_df):
    gpx_xml = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="RoutePlanner" xmlns="http://www.topografix.com/GPX/1/1">',
        "  <rte>",
        f'    <name>{datetime.now().strftime("%Y-%m-%d")}</name>',
    ]
    seen_ids = set()
    for _, row in sched_df.iterrows():
        try:
            insp_id = str(row["Inspection ID"]).strip()
            if not insp_id or insp_id.lower() in ["depot", "nan"] or insp_id in seen_ids:
                continue
            seen_ids.add(insp_id)
            lat = float(row["Latitude"])
            lon = float(row["Longitude"])
            desc = str(row["Description"]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            gpx_xml.append(f'    <rtept lat="{lat}" lon="{lon}">')
            gpx_xml.append(f"      <name>{insp_id}</name>")
            gpx_xml.append(f"      <desc>{desc}</desc>")
            gpx_xml.append("    </rtept>")
        except Exception:
            continue
    gpx_xml.append("  </rte>")
    gpx_xml.append("</gpx>")
    return "\n".join(gpx_xml)


def export_directions_txt(sched_df):
    lines = [
        "==========================================",
        f"ROUTE DIRECTIONS - {datetime.now().strftime('%Y-%m-%d')}",
        "==========================================\n",
    ]
    for _, row in sched_df.iterrows():
        insp_str = f" [ID: {row['Inspection ID']}]" if row["Inspection ID"] else ""
        lines.append(f"Stop {row['Stop Number']}{insp_str}: {row['Description']}")
        lines.append(f"  Arrival: {row['Arrival']}  |  Departure: {row['Departure']}")
        lines.append(f"  Total Distance: {row['Total Miles']} miles")
        lines.append("-" * 40)
    return "\n".join(lines)


# =========================================================================
# 2. STREAMLIT APP & MULTI-INSPECTOR ISOLATION
# =========================================================================
st.set_page_config(page_title="Route Planner & Mobile App", layout="wide")

st.sidebar.markdown("### 👤 Inspector Workspace")
inspector_profile = st.sidebar.selectbox(
    "Active Inspector:",
    ["Soren", "Huny", "Driver 3", "Driver 4"],
    index=0,
    help="Each inspector has an isolated workspace so multiple people can route simultaneously."
)
inspector_slug = re.sub(r"\W+", "_", inspector_profile.strip().lower())
PERSISTENT_FILE = f"active_route_{inspector_slug}.csv"

if f"processed_uploads_{inspector_slug}" not in st.session_state:
    st.session_state[f"processed_uploads_{inspector_slug}"] = set()


def load_persisted_stops():
    if os.path.exists(PERSISTENT_FILE):
        try:
            df = pd.read_csv(PERSISTENT_FILE)
            if not df.empty and "Address" in df.columns:
                return df.dropna(subset=["Address"]).reset_index(drop=True)
        except Exception:
            pass
    return pd.DataFrame(columns=["Inspection ID", "Address"])


def persist_stops(df):
    clean_df = (
        df.dropna(subset=["Address"])
        .drop_duplicates(subset=["Inspection ID"], keep="last")
        .reset_index(drop=True)
    )
    clean_df.to_csv(PERSISTENT_FILE, index=False)
    return clean_df


@st.cache_data
def search_address(query):
    if not query or len(query.strip()) < 3:
        return []
    geolocator = Nominatim(user_agent="cfs_field_geocoder_us_v38")
    try:
        locations = geolocator.geocode(
            query,
            country_codes="us",
            viewbox=[(-83.7, 36.5), (-75.0, 40.0)],
            bounded=False,
            exactly_one=False,
            limit=6,
        )
        if locations:
            return [loc.address for loc in locations]
    except Exception:
        pass
    return []


st.sidebar.markdown("---")
st.sidebar.markdown("### 🔀 Display View")
view_mode = st.sidebar.radio(
    "Choose Interface Mode:",
    ["🖥️ Desktop Planner", "📱 Mobile Driver Deck"],
    index=0,
)

st.sidebar.markdown("---")
st.sidebar.header("⚙️ Route Settings")

start_input = st.sidebar.text_input("Starting Address / Base:", "2644 S Shirlington Rd, Arlington, VA")
start_suggestions = search_address(start_input)
depot_address = (
    st.sidebar.selectbox("Select Matched Start Address:", start_suggestions, index=0)
    if start_suggestions
    else start_input
)

start_time = st.sidebar.time_input("Route Start Time:", value=time(8, 0))
stop_duration = st.sidebar.number_input(
    "Inspection Time per Stop (mins):", min_value=1, max_value=120, value=5
)

# Recall Saved Route (Filtered by Workspace)
st.sidebar.markdown("---")
st.sidebar.subheader(f"📂 Recall Saved Route ({inspector_profile})")
all_saved_files = glob.glob(os.path.join(SAVED_DIR, "*.csv"))
inspector_saved_files = sorted(
    [f for f in all_saved_files if inspector_slug in os.path.basename(f).lower() or not any(s in os.path.basename(f).lower() for s in ["soren", "huny", "driver_3", "driver_4"])],
    key=os.path.getmtime,
    reverse=True,
)

if inspector_saved_files:
    file_options = ["-- Select a saved route --"] + [os.path.basename(f) for f in inspector_saved_files]
    selected_saved = st.sidebar.selectbox("Choose From Saved Routes:", file_options)
    if st.sidebar.button("📥 Open Route", key="btn_load_saved"):
        if selected_saved != "-- Select a saved route --":
            target_path = os.path.join(SAVED_DIR, selected_saved)
            try:
                loaded_df = pd.read_csv(target_path)
                recalled_rows = []
                for _, r in loaded_df.iterrows():
                    insp_id = str(r["Inspection ID"]).strip() if "Inspection ID" in r else str(r.iloc[0]).strip()
                    addr = str(r["Address"]).strip() if "Address" in r else str(r.iloc[1]).strip()
                    
                    if (
                        addr
                        and str(addr).lower() != "nan"
                        and not str(addr).startswith("Start:")
                        and not str(addr).startswith("End:")
                        and str(insp_id).lower() not in ["depot", "start/end depot", "nan", ""]
                    ):
                        recalled_rows.append({"Inspection ID": insp_id, "Address": addr})

                if recalled_rows:
                    persist_stops(pd.DataFrame(recalled_rows))
                    st.toast(f"Opened {selected_saved} into {inspector_profile}'s desk!", icon="📂")
                    st.rerun()
            except Exception as e:
                st.sidebar.error(f"Error loading file: {e}")

# Sidebar Add Stop
st.sidebar.markdown("---")
with st.sidebar.expander("➕ Add Stop / Special Address", expanded=False):
    manual_addr_in = st.text_input(
        "Property Address:",
        placeholder="e.g. 100 Main St, Alexandria, VA",
        key="desk_add_addr_in",
    )
    manual_id_in = st.text_input(
        "Work Order / Inspection ID:",
        placeholder="e.g. SPECIAL-1 or Order #",
        key="desk_add_id_in",
    )
    if st.sidebar.button("➕ Add Stop to Route", type="primary", use_container_width=True):
        if manual_addr_in.strip():
            cur_df = load_persisted_stops()
            final_id = manual_id_in.strip()
            if not final_id or final_id in cur_df["Inspection ID"].values:
                final_id = f"ADD_{len(cur_df)+1}_{datetime.now().strftime('%H%M%S')}"

            new_entry = pd.DataFrame([{"Inspection ID": final_id, "Address": manual_addr_in.strip()}])
            updated_df = pd.concat([cur_df, new_entry], ignore_index=True)
            persist_stops(updated_df)

            cache = load_cache()
            geocode_single_nominatim(manual_addr_in.strip(), cache)

            st.toast(f"Added stop: {final_id} to {inspector_profile}!", icon="✅")
            st.rerun()
        else:
            st.sidebar.warning("Please type an address first.")

# Import CSV Files
st.sidebar.markdown("---")
st.sidebar.subheader("📁 Import CSV Files")
uploaded_files = st.sidebar.file_uploader(
    f"Upload Spreadsheets for {inspector_profile}", type=["csv"], accept_multiple_files=True
)

master_df = load_persisted_stops()

if uploaded_files:
    new_rows = []
    for f in uploaded_files:
        if f.name not in st.session_state[f"processed_uploads_{inspector_slug}"]:
            try:
                raw_df = pd.read_csv(f)
                raw_df.columns = [str(c).strip() for c in raw_df.columns]
                if "Name" in raw_df.columns and "Address" in raw_df.columns:
                    for _, r in raw_df.iterrows():
                        insp_id = str(r["Name"]).strip()
                        addr = str(r["Address"]).strip()
                        if addr and insp_id.lower() not in ["depot", "start/end depot", "nan"]:
                            new_rows.append({"Inspection ID": insp_id, "Address": addr})
                else:
                    for idx, row in raw_df.iterrows():
                        insp_id = str(row.iloc[0]).strip() if len(row) > 0 else f"ID_{idx+1}"
                        addr1 = str(row.get("Address1", "")) if "Address1" in row else ""
                        city = str(row.get("City", "")) if "City" in row else ""
                        state = str(row.get("State", "")) if "State" in row else ""
                        zip_c = str(row.get("Zip", "")) if "Zip" in row else ""
                        full_addr = f"{addr1}, {city}, {state} {zip_c}".strip(", ")
                        if not full_addr or full_addr == ",":
                            full_addr = str(row.iloc[1]).strip() if len(row) > 1 else insp_id
                        if full_addr and full_addr.lower() != "nan":
                            new_rows.append({"Inspection ID": insp_id, "Address": full_addr})
                st.session_state[f"processed_uploads_{inspector_slug}"].add(f.name)
            except Exception:
                continue

    if new_rows:
        master_df = pd.concat([master_df, pd.DataFrame(new_rows)], ignore_index=True)
        master_df = persist_stops(master_df)

# Route Controls
st.sidebar.markdown("---")
btn_reverse = st.sidebar.button("⇄ Reverse Entire Route Order", use_container_width=True)

if st.sidebar.button(f"🔄 Clear {inspector_profile}'s Route & Start Fresh", use_container_width=True):
    if os.path.exists(PERSISTENT_FILE):
        try:
            os.remove(PERSISTENT_FILE)
        except Exception:
            pass
    st.session_state[f"processed_uploads_{inspector_slug}"] = set()
    st.rerun()

# =========================================================================
# 3. MAIN LOGIC PIPELINE
# =========================================================================
master_df = load_persisted_stops()

if not master_df.empty:
    coords, valid_indices, failed_stops = get_coordinates_and_failed_stops(
        master_df, depot_address
    )

    if failed_stops:
        st.error(
            f"⚠️ **Attention: {len(failed_stops)} Address(es) could not be mapped automatically!**"
        )
        with st.expander("🛠️ Click Here to Review & Fix Unresolved Addresses", expanded=True):
            for item in failed_stops:
                f_idx = item["Index"]
                f_id = item["Inspection ID"]
                f_addr = item["Address"]
                suggested_val, reasons = suggest_address_cleanup(f_addr)

                st.markdown(f"#### 🛑 Stop ID: `{f_id}`")
                if reasons:
                    st.caption(f"**Diagnostic Detected:** {' | '.join(reasons)}")

                col_input, col_sugg, col_action = st.columns([4, 3, 1])
                corrected_text = col_input.text_input(
                    f"Edit address for {f_id}:",
                    value=suggested_val,
                    key=f"fix_in_{f_idx}",
                )

                if suggested_val != f_addr:
                    if col_sugg.button(
                        f"✨ Apply: {suggested_val[:22]}...",
                        key=f"btn_sugg_{f_idx}",
                    ):
                        master_df.at[f_idx, "Address"] = suggested_val
                        persist_stops(master_df)
                        st.toast(f"Applied suggestion for {f_id}!", icon="✨")
                        st.rerun()

                if col_action.button("💾 Fix", key=f"btn_fix_{f_idx}"):
                    master_df.at[f_idx, "Address"] = corrected_text.strip()
                    persist_stops(master_df)
                    st.toast(f"Updated {f_id}!", icon="✅")
                    st.rerun()

    valid_master_df = master_df.iloc[valid_indices].reset_index(drop=True)

    if btn_reverse and not valid_master_df.empty:
        valid_master_df = valid_master_df.iloc[::-1].reset_index(drop=True)
        persist_stops(valid_master_df)
        st.rerun()

    if not valid_master_df.empty and len(coords) > 1:
        dist_matrix, dur_matrix = build_road_distance_matrix_cached(coords)

        # Desktop Optimization Form (Defensive Parsing Guard)
        st.markdown(f"### ⚡ Route Direction & Optimization ({inspector_profile})")
        stop_options = ["-- Auto-Pick Closest Stop --"] + [
            f"#{i+1}: {r['Inspection ID']} ({str(r['Address'])[:22]}...)"
            for i, r in valid_master_df.iterrows()
        ]

        with st.form("optimizer_control_form"):
            c_start, c_end, c_btn = st.columns([4, 4, 3], gap="medium")
            selected_first_opt = c_start.selectbox("📍 Lock First Stop:", stop_options, index=0)
            selected_last_opt = c_end.selectbox("🏁 Lock Last Stop:", stop_options, index=0)

            c_btn.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
            btn_do_optimize = c_btn.form_submit_button(
                "⚡ Auto-Optimize Route",
                type="primary",
                use_container_width=True,
            )

            if btn_do_optimize:
                first_row_id = None
                if selected_first_opt != "-- Auto-Pick Closest Stop --":
                    try:
                        raw_idx = int(re.match(r"#(\d+):", selected_first_opt).group(1)) - 1
                        first_row_id = valid_master_df.iloc[raw_idx]["Inspection ID"]
                    except Exception:
                        pass

                last_row_id = None
                if selected_last_opt != "-- Auto-Pick Closest Stop --":
                    try:
                        raw_idx = int(re.match(r"#(\d+):", selected_last_opt).group(1)) - 1
                        last_row_id = valid_master_df.iloc[raw_idx]["Inspection ID"]
                    except Exception:
                        pass

                if first_row_id is not None:
                    match_first = valid_master_df[valid_master_df["Inspection ID"] == first_row_id]
                    rest = valid_master_df[valid_master_df["Inspection ID"] != first_row_id]
                    valid_master_df = pd.concat([match_first, rest]).reset_index(drop=True)

                if last_row_id is not None and last_row_id != first_row_id:
                    match_last = valid_master_df[valid_master_df["Inspection ID"] == last_row_id]
                    rest = valid_master_df[valid_master_df["Inspection ID"] != last_row_id]
                    valid_master_df = pd.concat([rest, match_last]).reset_index(drop=True)

                coords, valid_indices, _ = get_coordinates_and_failed_stops(valid_master_df, depot_address)
                dist_matrix, dur_matrix = build_road_distance_matrix_cached(coords)

                optimized_nodes = optimize_stops_sequence(
                    dist_matrix,
                    lock_first=(first_row_id is not None),
                    lock_last=(last_row_id is not None),
                )

                reordered_indices = [n - 1 for n in optimized_nodes]
                valid_master_df = valid_master_df.iloc[reordered_indices].reset_index(drop=True)
                persist_stops(valid_master_df)
                st.toast("Route optimized successfully!", icon="⚡")
                st.rerun()

        num_valid = len(coords) - 1
        routes = {0: [0] + list(range(1, num_valid + 1)) + [0]}

        generate_map(coords, routes)
        schedules = calculate_schedule(coords, routes, dist_matrix, dur_matrix, start_time, stop_duration)
        sched_df = schedules[0]
        custom_route_name = f"{inspector_profile}_Route_{datetime.now().strftime('%Y%m%d_%H%M')}"

        # =========================================================================
        # VIEW 1: DESKTOP PLANNER
        # =========================================================================
        if view_mode == "🖥️ Desktop Planner":
            col_list, col_map = st.columns([1, 2], gap="small")

            with col_list:
                st.markdown(f"### 📋 Manage Stops ({len(valid_master_df)} Active)")
                table_rows = []
                for i, r in valid_master_df.iterrows():
                    table_rows.append(
                        {
                            "Drop?": False,
                            "Stop #": int(i + 1),
                            "Inspection ID": str(r["Inspection ID"]),
                            "Address": str(r["Address"]),
                        }
                    )
                edit_table = pd.DataFrame(table_rows)

                with st.form("bulk_reorder_and_prune_form", clear_on_submit=False):
                    edited_df = st.data_editor(
                        edit_table,
                        column_config={
                            "Drop?": st.column_config.CheckboxColumn("Drop?", default=False),
                            "Stop #": st.column_config.NumberColumn(
                                "Stop #",
                                min_value=1,
                                max_value=len(table_rows) + 50,
                                step=1,
                                width="small",
                                help="Change numbers directly to rearrange stops",
                            ),
                            "Inspection ID": st.column_config.TextColumn("ID", width="medium"),
                            "Address": st.column_config.TextColumn("Address", width="medium"),
                        },
                        disabled=["Inspection ID", "Address"],
                        hide_index=True,
                        use_container_width=True,
                        height=650,
                    )

                    submit_changes = st.form_submit_button(
                        "💾 Apply Sequence & Reorder Changes",
                        type="primary",
                        use_container_width=True,
                    )

                    if submit_changes:
                        kept_df = edited_df[edited_df["Drop?"] == False].copy()
                        kept_df["Stop #"] = pd.to_numeric(kept_df["Stop #"], errors="coerce").fillna(9999)
                        kept_df = kept_df.sort_values(by=["Stop #"]).reset_index(drop=True)

                        order_map = {row["Inspection ID"]: idx for idx, row in kept_df.iterrows()}
                        valid_master_df = valid_master_df[valid_master_df["Inspection ID"].isin(order_map.keys())].copy()
                        valid_master_df["_sort_key"] = valid_master_df["Inspection ID"].map(order_map)
                        valid_master_df = valid_master_df.sort_values(by=["_sort_key"]).drop(columns=["_sort_key"]).reset_index(drop=True)

                        persist_stops(valid_master_df)
                        st.toast("Updated sequence & removed selected stops!", icon="💾")
                        st.rerun()

            with col_map:
                st.markdown(f"### 🗺️ Live Route Map ({inspector_profile})")
                if os.path.exists("route_map.html"):
                    with open("route_map.html", "r", encoding="utf-8") as f:
                        components.html(f.read(), height=750)

            st.markdown("---")
            final_row = sched_df.iloc[-1]
            first_row = sched_df.iloc[0]
            col_m1, col_m2, col_m3, col_m4 = st.columns(4)
            col_m1.metric("🚗 Total Mileage", f"{final_row['Total Miles']:.1f} mi")
            col_m2.metric("⏱️ Total Planned Span", f"{first_row['Arrival']} - {final_row['Arrival']}")
            col_m3.metric("📍 Active Stops", f"{max(0, len(sched_df) - 2)}")
            col_m4.metric("🏁 Target Office Return", final_row["Arrival"])

            # ----------------- SAVE ROUTE & EXPORTS -----------------
            st.markdown("---")
            st.subheader(f"💾 Save Route & Export Data ({inspector_profile})")
            exp_col1, exp_col2, exp_col3, exp_col4 = st.columns(4)

            with exp_col1:
                default_name = f"{inspector_profile}_{datetime.now().strftime('%Y%m%d_%H%M')}"
                route_save_name = st.text_input("Save As Route Name:", value=default_name, key=f"desk_save_{inspector_slug}")
                if st.button("💾 Save Route", type="primary", use_container_width=True):
                    os.makedirs(SAVED_DIR, exist_ok=True)
                    save_path = os.path.join(SAVED_DIR, f"{route_save_name}.csv")
                    valid_master_df.to_csv(save_path, index=False)
                    st.toast(f"Saved {route_save_name} cleanly!", icon="💾")
                    st.rerun()

            with exp_col2:
                csv_bytes = sched_df.to_csv(index=False).encode("utf-8")
                st.download_button(
                    label="📥 Download CSV Timetable",
                    data=csv_bytes,
                    file_name=f"{route_save_name}_timetable.csv",
                    mime="text/csv",
                    key=f"desk_dl_csv_{inspector_slug}",
                    use_container_width=True
                )

            with exp_col3:
                gpx_data = export_waypoints_gpx(sched_df)
                st.download_button(
                    label="🗺️ Download GPX",
                    data=gpx_data,
                    file_name=f"{route_save_name}.gpx",
                    mime="application/gpx+xml",
                    key=f"desk_dl_gpx_{inspector_slug}",
                    use_container_width=True
                )

            with exp_col4:
                txt_data = export_directions_txt(sched_df)
                st.download_button(
                    label="📄 Download Directions (TXT)",
                    data=txt_data,
                    file_name=f"{route_save_name}_directions.txt",
                    mime="text/plain",
                    key=f"desk_dl_txt_{inspector_slug}",
                    use_container_width=True
                )

        # =========================================================================
        # VIEW 2: MOBILE DRIVER DECK (LOCKED ENGINE & CIRCUIT WORKFLOW)
        # =========================================================================
        elif view_mode == "📱 Mobile Driver Deck":
            final_row = sched_df.iloc[-1]
            total_inspections = max(0, len(sched_df) - 2)

            st.markdown(f"## 📱 Mobile Driver Deck — {inspector_profile}")
            st.caption(f"**Route:** {custom_route_name} | Circuit-grade telemetry & locked timetable")

            c_mb1, c_mb2, c_mb3 = st.columns(3)
            c_mb1.metric("📍 Total Stops", f"{total_inspections}")
            c_mb2.metric("🚗 Total Route", f"{final_row['Total Miles']:.1f} mi")
            c_mb3.metric("🏁 Target Base ETA", f"{final_row['Arrival']}")
            st.markdown("---")

            planned_total_miles = float(final_row["Total Miles"])
            planned_finish_str = str(final_row["Arrival"]).strip()

            stops_payload = []
            for idx, row in sched_df.iterrows():
                insp_id = str(row.get("Inspection ID", "")).strip()
                desc = str(row.get("Description", "")).strip()
                raw_addr = desc.replace("Start: ", "").replace("End: ", "").strip()

                parts = [p.strip() for p in raw_addr.split(",") if p.strip()]
                street_val = parts[0] if len(parts) > 0 else raw_addr
                city_val = parts[1] if len(parts) > 1 else ""
                state_val = "VA"
                zip_val = ""

                if len(parts) >= 3:
                    state_zip_tokens = parts[2].split()
                    if len(state_zip_tokens) > 0:
                        state_val = state_zip_tokens[0]
                    if len(state_zip_tokens) > 1:
                        zip_val = state_zip_tokens[1]

                addr_tokens = [street_val]
                if city_val:
                    addr_tokens.append(city_val)
                addr_tokens.append(state_val)
                if zip_val:
                    addr_tokens.append(zip_val)
                full_verified_dest = ", ".join(addr_tokens)

                is_depot = idx == 0 or idx == len(sched_df) - 1
                is_finish = idx == len(sched_df) - 1

                stops_payload.append(
                    {
                        "row_idx": idx,
                        "stop_num": int(row.get("Stop Number", idx + 1)),
                        "insp_id": insp_id,
                        "street": street_val,
                        "city": city_val,
                        "state": state_val,
                        "zip": zip_val,
                        "full_dest": full_verified_dest,
                        "order_num": insp_id,
                        "planned_arrival": str(row.get("Arrival", "--:--")),
                        "cum_miles": float(row.get("Total Miles", 0.0)),
                        "leg_miles": float(row.get("Leg Miles", 0.0)),
                        "lat": float(row.get("Latitude", 0.0)),
                        "lon": float(row.get("Longitude", 0.0)),
                        "is_depot": is_depot,
                        "is_finish_leg": is_finish,
                        "status": "pending",
                        "arrived_at": None,
                    }
                )

            stops_json_str = json.dumps(stops_payload)
            route_sig = f"sig_{inspector_slug}_{len(stops_payload)}_{planned_total_miles}_{total_inspections}_{datetime.now().strftime('%d%H%M')}"

            deck_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <style>
        * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
        body {{ margin: 0; padding: 8px; background-color: #0b132b; color: #f3f4f6; }}
        
        #toast {{
            visibility: hidden;
            min-width: 260px;
            background-color: #059669;
            color: #fff;
            text-align: center;
            border-radius: 10px;
            padding: 12px;
            position: fixed;
            z-index: 99999;
            left: 50%;
            top: 16px;
            transform: translateX(-50%);
            font-size: 0.95rem;
            font-weight: 800;
            box-shadow: 0 4px 15px rgba(0,0,0,0.5);
        }}
        #toast.show {{
            visibility: visible;
            animation: fadein 0.2s, fadeout 0.3s 2.0s;
        }}
        @keyframes fadein {{ from {{ top: 0; opacity: 0; }} to {{ top: 16px; opacity: 1; }} }}
        @keyframes fadeout {{ from {{ top: 16px; opacity: 1; }} to {{ top: 0; opacity: 0; }} }}

        .hud-bar {{
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            background: #111827;
            border: 1px solid #1f2937;
            border-radius: 12px;
            padding: 10px 2px;
            margin-bottom: 12px;
            text-align: center;
        }}
        .hud-label {{ font-size: 0.58rem; color: #9ca3af; text-transform: uppercase; font-weight: 800; margin-bottom: 2px; }}
        .hud-val {{ font-size: 0.95rem; font-weight: 800; }}
        .c-blue {{ color: #60a5fa; }}
        .c-orange {{ color: #fb923c; }}
        .c-green {{ color: #34d399; }}
        .c-yellow {{ color: #fbbf24; }}
        .c-cyan {{ color: #38bdf8; }}
        .hud-sep {{ border-left: 1px solid #1f2937; }}

        .card {{
            background: #1c2541;
            border: 1px solid #3a506b;
            border-radius: 14px;
            padding: 16px;
            margin-bottom: 12px;
            box-shadow: 0 4px 10px rgba(0, 0, 0, 0.3);
        }}
        .card-badge {{
            display: inline-block;
            padding: 3px 8px;
            font-size: 0.75rem;
            font-weight: 800;
            border-radius: 6px;
            margin-bottom: 8px;
            text-transform: uppercase;
        }}
        .badge-inspection {{ background: #1e3a8a; color: #93c5fd; }}
        .badge-depot {{ background: #4b5563; color: #f3f4f6; }}

        .card-title {{ font-size: 1.35rem; font-weight: 800; color: #ffffff; margin: 0 0 4px 0; line-height: 1.25; }}
        .card-city {{ font-size: 1.05rem; color: #60a5fa; margin-bottom: 12px; font-weight: 700; }}
        .card-info-box {{
            background: #0b132b;
            border-radius: 8px;
            padding: 10px 12px;
            margin-top: 8px;
            border-left: 4px solid #3b82f6;
        }}
        .info-line {{ font-size: 0.95rem; margin: 4px 0; color: #e5e7eb; }}
        .info-bold {{ font-weight: 700; color: #93c5fd; }}

        .btn-nav {{
            display: block;
            width: 100%;
            background: linear-gradient(135deg, #2563eb, #1d4ed8);
            color: #ffffff;
            text-align: center;
            text-decoration: none;
            padding: 15px;
            border-radius: 12px;
            font-size: 1.15rem;
            font-weight: 800;
            margin-bottom: 10px;
            border: none;
            cursor: pointer;
        }}
        .btn-next {{
            display: block;
            width: 100%;
            background: linear-gradient(135deg, #059669, #10b981);
            color: #ffffff;
            padding: 18px;
            border-radius: 14px;
            font-size: 1.3rem;
            font-weight: 900;
            letter-spacing: 0.5px;
            border: none;
            cursor: pointer;
            box-shadow: 0 4px 14px rgba(16, 185, 129, 0.4);
            margin-bottom: 10px;
            text-transform: uppercase;
        }}
        .btn-next:active {{ transform: scale(0.98); background: #047857; }}

        .btn-row {{
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 8px;
            margin-bottom: 10px;
        }}
        .btn-secondary {{
            background: #1f2937;
            color: #d1d5db;
            padding: 12px 6px;
            border-radius: 10px;
            font-size: 0.85rem;
            font-weight: 700;
            border: 1px solid #374151;
            cursor: pointer;
            text-align: center;
        }}
        .btn-warn {{ background: #7c2d12; color: #fecaca; border: none; }}
        .btn-danger {{ background: #881337; color: #fecdd3; border: none; }}

        .control-deck {{
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 8px;
            margin-bottom: 8px;
        }}
        .btn-tool {{
            background: #1c2541;
            color: #cbd5e1;
            border: 1px solid #3a506b;
            padding: 14px 4px;
            border-radius: 10px;
            font-size: 0.82rem;
            font-weight: 700;
            cursor: pointer;
            text-align: center;
        }}
        .btn-tool:active {{ background: #3a506b; }}

        .modal-overlay {{
            display: none;
            position: fixed;
            z-index: 10000;
            left: 0; top: 0; width: 100%; height: 100%;
            background: rgba(0, 0, 0, 0.85);
            padding: 18px;
        }}
        .modal-sheet {{
            background: #161e2e;
            border-radius: 14px;
            padding: 20px;
            margin-top: 30px;
            border: 1px solid #3a506b;
        }}
        .modal-opt-btn {{
            width: 100%;
            background: #1e293b;
            border: 1px solid #334155;
            color: white;
            padding: 14px;
            border-radius: 10px;
            margin-bottom: 12px;
            text-align: left;
            cursor: pointer;
        }}
        .modal-opt-btn strong {{ font-size: 1.05rem; display: block; color: #38bdf8; }}
        .modal-opt-btn span {{ font-size: 0.82rem; color: #94a3b8; }}
    </style>
</head>
<body>

    <div id="toast">💾 Updated!</div>

    <div class="hud-bar">
        <div>
            <div class="hud-label">Stop</div>
            <div class="hud-val c-blue" id="hud-stop">1/{total_inspections}</div>
        </div>
        <div>
            <div class="hud-label">Left</div>
            <div class="hud-val c-orange" id="hud-left">{total_inspections}</div>
        </div>
        <div>
            <div class="hud-label">Driven</div>
            <div class="hud-val c-green" id="hud-driven">0 mi</div>
        </div>
        <div>
            <div class="hud-label">Total Miles</div>
            <div class="hud-val c-yellow" id="hud-to-office">{int(round(planned_total_miles))} mi</div>
        </div>
        <div class="hud-sep">
            <div class="hud-label">Office ETA</div>
            <div class="hud-val c-cyan" id="hud-finish">{planned_finish_str}</div>
        </div>
    </div>

    <div class="card">
        <span class="card-badge" id="disp-badge">Inspection Stop</span>
        <div class="card-title" id="disp-addr">Loading...</div>
        <div class="card-city" id="disp-city"></div>
        
        <div class="card-info-box">
            <div class="info-line"><span class="info-bold">Planned Target:</span> <span id="disp-planned-arrival">--:--</span></div>
            <div class="info-line"><span class="info-bold">Work Order:</span> <span id="disp-order">--</span></div>
        </div>
    </div>

    <a id="nav-link" href="#" target="_blank" class="btn-nav">📍 Open in Google Maps</a>
    <button id="btn-next-action" class="btn-next" onclick="completeStop()">Next Stop (Complete) ⏩</button>

    <div class="btn-row">
        <button class="btn-secondary" onclick="prevStop()">⬅️ Previous</button>
        <button class="btn-secondary btn-warn" onclick="skipStop()">⏭️ Skip</button>
        <button class="btn-secondary btn-danger" onclick="deleteStop()">🗑️ Delete</button>
    </div>

    <div class="control-deck">
        <button class="btn-tool" onclick="openReoptMenu()">⚡ Re-Sync</button>
        <button class="btn-tool" onclick="openAddModal()">➕ Add Stop</button>
        <button class="btn-tool" onclick="openTimetableModal()">📋 Upcoming</button>
    </div>

    <!-- Circuit Action Split Modal -->
    <div id="reopt-modal" class="modal-overlay">
        <div class="modal-sheet">
            <h3 style="margin-top:0; color:#fff;">Reoptimization Options</h3>
            <p style="color:#94a3b8; font-size:0.85rem; margin-bottom:14px;">Select how to adjust the remaining route from your physical location:</p>
            
            <button class="modal-opt-btn" onclick="executeSyncOnly()">
                <strong>➔ Update Route (Sync Only)</strong>
                <span>Preserve stop order; recalculate driving legs from current GPS</span>
            </button>
            
            <button class="modal-opt-btn" onclick="executeFullReoptimize()">
                <strong>🔀 Reoptimize Route</strong>
                <span>Reorder remaining stops from current GPS for minimum mileage</span>
            </button>

            <button class="btn-secondary btn-danger" style="width:100%; padding:10px; margin-top:8px;" onclick="closeReoptMenu()">Cancel</button>
        </div>
    </div>

    <!-- Add Stop Modal -->
    <div id="add-modal" class="modal-overlay">
        <div class="modal-sheet">
            <h3 style="margin-top:0; color:#fff;">Add Inspection Stop</h3>
            <input type="text" id="modal-street" style="width:100%; padding:12px; margin-bottom:8px; border-radius:8px; background:#0b132b; color:#fff; border:1px solid #475569;" placeholder="Street Address (e.g. 12253 Piney Ln)">
            <div style="display:grid; grid-template-columns: 2fr 1fr 1fr; gap:6px; margin-bottom:8px;">
                <input type="text" id="modal-city" style="padding:10px; border-radius:8px; background:#0b132b; color:#fff; border:1px solid #475569;" placeholder="City">
                <input type="text" id="modal-state" style="padding:10px; border-radius:8px; background:#0b132b; color:#fff; border:1px solid #475569;" value="VA">
                <input type="text" id="modal-zip" style="padding:10px; border-radius:8px; background:#0b132b; color:#fff; border:1px solid #475569;" placeholder="Zip">
            </div>
            <input type="text" id="modal-order" style="width:100%; padding:12px; margin-bottom:12px; border-radius:8px; background:#0b132b; color:#fff; border:1px solid #475569;" placeholder="Work Order #">
            
            <button id="modal-submit-btn" class="btn-next" style="padding:14px; font-size:1.05rem;" onclick="confirmAddStop()">Add Stop & Geocode</button>
            <button class="btn-secondary btn-danger" style="width:100%; padding:10px; margin-top:8px;" onclick="closeAddModal()">Cancel</button>
        </div>
    </div>

    <!-- Upcoming Stops Modal (Circuit-Style Itinerary List) -->
    <div id="timetable-modal" class="modal-overlay">
        <div class="modal-sheet" style="max-height: 80vh; display: flex; flex-direction: column;">
            <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #334155; padding-bottom: 10px; margin-bottom: 10px;">
                <div>
                    <h3 style="margin: 0; color: #fff; font-size: 1.1rem;">Upcoming Stops</h3>
                    <span id="modal-route-summary" style="font-size: 0.78rem; color: #94a3b8;">--</span>
                </div>
                <button class="btn-secondary" style="padding: 6px 12px; margin: 0;" onclick="closeTimetableModal()">✕ Close</button>
            </div>
            
            <div id="timetable-list" style="overflow-y: auto; flex: 1; padding-right: 4px;"></div>
        </div>
    </div>

    <script>
        let stops = {stops_json_str};
        const MASTER_TOTAL_MILES = {planned_total_miles};
        const baselineFinishStr = "{planned_finish_str}";
        const totalInspectionsCount = {total_inspections};
        const currentSig = "{route_sig}";

        const savedSig = localStorage.getItem("cfs_sig_{inspector_slug}");
        if (savedSig !== currentSig) {{
            localStorage.removeItem("cfs_stops_{inspector_slug}");
            localStorage.removeItem("cfs_idx_{inspector_slug}");
            localStorage.removeItem("cfs_banked_min_{inspector_slug}");
            localStorage.setItem("cfs_sig_{inspector_slug}", currentSig);
        }}

        const savedStops = localStorage.getItem("cfs_stops_{inspector_slug}");
        if (savedStops) {{
            try {{
                const parsed = JSON.parse(savedStops);
                if (Array.isArray(parsed) && parsed.length > 0) stops = parsed;
            }} catch(e) {{}}
        }}

        let curIdx = parseInt(localStorage.getItem("cfs_idx_{inspector_slug}") || "0", 10);
        if (isNaN(curIdx) || curIdx >= stops.length) curIdx = 0;
        if (curIdx === 0 && stops[0].is_depot && stops.length > 1) curIdx = 1;

        let bankedMinutes = parseFloat(localStorage.getItem("cfs_banked_min_{inspector_slug}") || "0.0");
        let liveCoords = null;

        if (navigator.geolocation) {{
            navigator.geolocation.watchPosition(
                function(pos) {{
                    liveCoords = {{ lat: pos.coords.latitude, lon: pos.coords.longitude }};
                }},
                function(err) {{}},
                {{ enableHighAccuracy: true, maximumAge: 5000 }}
            );
        }}

        function showToast(msg) {{
            const toast = document.getElementById("toast");
            toast.innerText = msg;
            toast.className = "show";
            setTimeout(function() {{ toast.className = toast.className.replace("show", ""); }}, 2200);
        }}

        function autoSave() {{
            try {{
                localStorage.setItem("cfs_stops_{inspector_slug}", JSON.stringify(stops));
                localStorage.setItem("cfs_idx_{inspector_slug}", curIdx.toString());
                localStorage.setItem("cfs_banked_min_{inspector_slug}", bankedMinutes.toString());
            }} catch(e) {{}}
        }}

        function applyDeltaToTimeString(baseTimeStr, deltaMinutes) {{
            try {{
                let d = new Date();
                let match = baseTimeStr.match(/(\d+):(\d+)\s*(AM|PM)/i);
                if (!match) return baseTimeStr;
                let hrs = parseInt(match[1], 10);
                let mins = parseInt(match[2], 10);
                let ampm = match[3].toUpperCase();

                if (ampm === "PM" && hrs < 12) hrs += 12;
                if (ampm === "AM" && hrs === 12) hrs = 0;

                d.setHours(hrs);
                d.setMinutes(mins);
                d.setSeconds(0);

                let adjusted = new Date(d.getTime() - (deltaMinutes * 60000));
                
                let outHrs = adjusted.getHours();
                let outMins = adjusted.getMinutes();
                let outAmpm = outHrs >= 12 ? 'PM' : 'AM';
                outHrs = outHrs % 12;
                outHrs = outHrs ? outHrs : 12;
                outMins = outMins < 10 ? '0' + outMins : outMins;
                return outHrs + ':' + outMins + ' ' + outAmpm;
            }} catch(e) {{
                return baseTimeStr;
            }}
        }}

        function updateDeck() {{
            if (!stops || stops.length === 0) return;
            if (curIdx >= stops.length) curIdx = stops.length - 1;

            const s = stops[curIdx];

            if (!s.arrived_at && !s.is_depot && s.status === "pending") {{
                s.arrived_at = Date.now();
            }}

            let activeInsps = stops.filter(function(st) {{ return !st.is_depot && st.status !== "deleted"; }});
            let totalActive = activeInsps.length > 0 ? activeInsps.length : totalInspectionsCount;
            let inspsLeft = stops.slice(curIdx).filter(function(st) {{ return !st.is_depot && st.status === "pending"; }}).length;

            if (s.is_depot) {{
                document.getElementById("hud-stop").innerText = s.is_finish_leg ? "END" : "START";
                document.getElementById("disp-badge").className = "card-badge badge-depot";
                document.getElementById("disp-badge").innerText = s.is_finish_leg ? "🏁 RETURN TO BASE" : "DEPARTURE BASE";
            }} else {{
                let currentNum = stops.slice(0, curIdx + 1).filter(function(st) {{ return !st.is_depot && st.status !== "deleted"; }}).length;
                document.getElementById("hud-stop").innerText = currentNum + "/" + totalActive;
                document.getElementById("disp-badge").className = "card-badge badge-inspection";
                document.getElementById("disp-badge").innerText = "INSPECTION #" + currentNum + " OF " + totalActive;
            }}

            document.getElementById("hud-left").innerText = inspsLeft;

            let drivenMiles = 0.0;
            for (let i = 0; i < curIdx; i++) {{
                if (stops[i].status === "completed") {{
                    drivenMiles += parseFloat(stops[i].leg_miles || 0.0);
                }}
            }}
            document.getElementById("hud-driven").innerText = Math.round(drivenMiles) + " mi";
            document.getElementById("hud-to-office").innerText = Math.round(MASTER_TOTAL_MILES) + " mi";

            let dynamicFinish = applyDeltaToTimeString(baselineFinishStr, bankedMinutes);
            document.getElementById("hud-finish").innerText = dynamicFinish;

            document.getElementById("disp-addr").innerText = s.street || "Property Address";
            let cityLine = [s.city, s.state, s.zip].filter(Boolean).join(", ");
            document.getElementById("disp-city").innerText = cityLine ? ("📍 " + cityLine) : "📍 Virginia";
            document.getElementById("disp-order").innerText = s.order_num || "N/A";
            
            let plannedArrivalAdjusted = applyDeltaToTimeString(s.planned_arrival, bankedMinutes);
            document.getElementById("disp-planned-arrival").innerText = plannedArrivalAdjusted;

            let verifiedNavTokens = [s.street, s.city || "Front Royal", s.state || "VA", s.zip].filter(Boolean);
            let verifiedAddress = verifiedNavTokens.join(", ");
            document.getElementById("nav-link").href = "https://www.google.com/maps/dir/?api=1&destination=" + encodeURIComponent(verifiedAddress) + "&travelmode=driving";

            const btnNext = document.getElementById("btn-next-action");
            if (curIdx === stops.length - 2 && stops[stops.length - 1].is_depot) {{
                btnNext.innerText = "Finish & Return to Base 🏁";
            }} else if (curIdx >= stops.length - 1) {{
                btnNext.innerText = "Route Complete ✅";
            }} else {{
                btnNext.innerText = "Next Stop (Complete) ⏩";
            }}

            autoSave();
        }}

        function completeStop() {{
            if (curIdx < stops.length - 1) {{
                const s = stops[curIdx];
                s.status = "completed";

                // 1. Dynamic Departure Snap on Stop #1 (e.g. 6:30 AM vs planned 8:00 AM)
                let isFirstInspection = (curIdx === 1 || (curIdx === 0 && !s.is_depot));
                if (isFirstInspection && s.planned_arrival && s.planned_arrival !== "--:--") {{
                    try {{
                        let match = s.planned_arrival.match(/(\d+):(\d+)\s*(AM|PM)/i);
                        if (match) {{
                            let pDate = new Date();
                            let pHours = parseInt(match[1], 10);
                            let pMins = parseInt(match[2], 10);
                            let pAmpm = match[3].toUpperCase();
                            if (pAmpm === "PM" && pHours < 12) pHours += 12;
                            if (pAmpm === "AM" && pHours === 12) pHours = 0;
                            pDate.setHours(pHours, pMins, 0, 0);

                            let startDeltaMinutes = (pDate.getTime() - Date.now()) / 60000.0;
                            if (startDeltaMinutes > 10.0) {{
                                bankedMinutes += startDeltaMinutes;
                            }}
                        }}
                    }} catch(e) {{}}
                }}

                // 2. Normal On-Site Dwell Absorption (5-minute allocation)
                if (s.arrived_at && !s.is_depot) {{
                    let elapsedMin = (Date.now() - s.arrived_at) / 60000.0;
                    let delta = 5.0 - elapsedMin;
                    bankedMinutes += delta;
                }}

                curIdx++;
                updateDeck();
                showToast("✅ Stop Completed (" + (bankedMinutes >= 0 ? "+" : "") + Math.round(bankedMinutes) + "m banked)");
            }}
        }}

        function skipStop() {{
            if (curIdx < stops.length - 1) {{
                stops[curIdx].status = "skipped";
                curIdx++;
                updateDeck();
                showToast("⏭️ Stop Skipped");
            }}
        }}

        function deleteStop() {{
            if (confirm("Delete stop from active route?")) {{
                stops[curIdx].status = "deleted";
                stops.splice(curIdx, 1);
                if (curIdx >= stops.length) curIdx = Math.max(0, stops.length - 1);
                updateDeck();
                showToast("🗑️ Stop Deleted");
            }}
        }}

        function prevStop() {{
            if (curIdx > 0) {{
                curIdx--;
                if (curIdx === 0 && stops[0].is_depot && stops.length > 1) curIdx = 1;
                updateDeck();
            }}
        }}

        function openReoptMenu() {{
            document.getElementById("reopt-modal").style.display = "block";
        }}
        function closeReoptMenu() {{
            document.getElementById("reopt-modal").style.display = "none";
        }}

        function openAddModal() {{
            document.getElementById("modal-street").value = "";
            document.getElementById("modal-city").value = "";
            document.getElementById("modal-state").value = "VA";
            document.getElementById("modal-zip").value = "";
            document.getElementById("modal-order").value = "";
            document.getElementById("add-modal").style.display = "block";
        }}

        function closeAddModal() {{
            document.getElementById("add-modal").style.display = "none";
        }}

        function confirmAddStop() {{
            const street = document.getElementById("modal-street").value.trim();
            const city = document.getElementById("modal-city").value.trim();
            const state = document.getElementById("modal-state").value.trim() || "VA";
            const zip = document.getElementById("modal-zip").value.trim();
            const order = document.getElementById("modal-order").value.trim();

            if (!street) {{
                alert("Please enter a street address");
                return;
            }}

            const submitBtn = document.getElementById("modal-submit-btn");
            submitBtn.innerText = "Geocoding Address...";

            let tokens = [street, city, state, zip].filter(Boolean);
            const fullDest = tokens.join(", ");

            fetch("https://nominatim.openstreetmap.org/search?q=" + encodeURIComponent(fullDest) + "&format=json&limit=1&countrycodes=us")
                .then(r => r.json())
                .then(data => {{
                    let lat = (data && data.length > 0) ? parseFloat(data[0].lat) : 0.0;
                    let lon = (data && data.length > 0) ? parseFloat(data[0].lon) : 0.0;
                    finishAddStop(street, city, state, zip, fullDest, order, lat, lon);
                }})
                .catch(e => {{
                    finishAddStop(street, city, state, zip, fullDest, order, 0.0, 0.0);
                }});
        }}

        function finishAddStop(street, city, state, zip, fullDest, order, lat, lon) {{
            document.getElementById("modal-submit-btn").innerText = "Add Stop & Geocode";

            const newStop = {{
                row_idx: stops.length,
                stop_num: stops.length,
                insp_id: order || "ADDED-STOP",
                street: street,
                city: city,
                state: state,
                zip: zip,
                full_dest: fullDest,
                order_num: order || "ADDED-STOP",
                planned_arrival: "--:--",
                cum_miles: 0,
                leg_miles: 4.0,
                lat: lat,
                lon: lon,
                is_depot: false,
                is_finish_leg: false,
                status: "pending",
                arrived_at: null
            }};

            const lastIdx = stops.length - 1;
            if (stops[lastIdx] && stops[lastIdx].is_depot) {{
                stops.splice(lastIdx, 0, newStop);
            }} else {{
                stops.push(newStop);
            }}

            closeAddModal();
            updateDeck();
            showToast("➕ Stop Added Before Base!");
        }}

        function openTimetableModal() {{
            const listContainer = document.getElementById("timetable-list");
            listContainer.innerHTML = "";

            let remainingStops = stops.slice(curIdx).filter(function(st) {{
                return st.status !== "deleted";
            }});

            let dynamicFinish = applyDeltaToTimeString(baselineFinishStr, bankedMinutes);
            let activeRemainingCount = remainingStops.filter(function(st) {{ return !st.is_depot; }}).length;
            document.getElementById("modal-route-summary").innerText = 
                "Finish: " + dynamicFinish + " · " + activeRemainingCount + " stops left";

            remainingStops.forEach(function(s, offset) {{
                let actualIndex = curIdx + offset;
                let isCurrent = (offset === 0);
                let adjustedArrival = applyDeltaToTimeString(s.planned_arrival, bankedMinutes);

                let item = document.createElement("div");
                item.style.backgroundColor = isCurrent ? "#1e293b" : "#0f172a";
                item.style.border = isCurrent ? "1px solid #38bdf8" : "1px solid #1e293b";
                item.style.borderLeft = isCurrent ? "4px solid #38bdf8" : (s.is_depot ? "4px solid #64748b" : "4px solid #2563eb");
                item.style.borderRadius = "8px";
                item.style.padding = "10px";
                item.style.marginBottom = "8px";
                item.style.cursor = "pointer";

                let badgeLabel = s.is_depot ? (s.is_finish_leg ? "RETURN BASE" : "DEPOT") : ("STOP #" + (s.stop_num - 1));
                let orderLabel = s.order_num ? ("ID: " + s.order_num) : "";

                item.innerHTML = `
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                        <span style="font-size: 0.72rem; font-weight: 800; color: ${{isCurrent ? '#38bdf8' : '#94a3b8'}};">${{badgeLabel}} ${{isCurrent ? '• ACTIVE' : ''}}</span>
                        <span style="font-size: 0.85rem; font-weight: 800; color: #fbbf24;">${{adjustedArrival}}</span>
                    </div>
                    <div style="font-size: 0.95rem; font-weight: 700; color: #f1f5f9; text-transform: uppercase;">${{s.street || 'Property Address'}}</div>
                    <div style="font-size: 0.78rem; color: #94a3b8;">${{[s.city, s.state, s.zip].filter(Boolean).join(", ")}}</div>
                    ${{orderLabel ? `<div style="font-size: 0.72rem; color: #60a5fa; margin-top: 4px; font-weight: 600;">${{orderLabel}}</div>` : ''}}
                `;

                item.onclick = function() {{
                    curIdx = actualIndex;
                    closeTimetableModal();
                    updateDeck();
                }};

                listContainer.appendChild(item);
            }});

            document.getElementById("timetable-modal").style.display = "block";
        }}

        function closeTimetableModal() {{
            document.getElementById("timetable-modal").style.display = "none";
        }}

        function executeSyncOnly() {{
            closeReoptMenu();
            showToast("⚡ Updating route telemetry from GPS...");

            if (!liveCoords || !liveCoords.lat) {{
                showToast("Preserving schedule (No GPS lock)");
                return;
            }}

            let coordChain = [liveCoords.lon + "," + liveCoords.lat];
            for (let i = curIdx; i < stops.length; i++) {{
                if (stops[i].lat && stops[i].lon) {{
                    coordChain.push(stops[i].lon + "," + stops[i].lat);
                }}
            }}

            if (coordChain.length >= 2) {{
                fetch("https://router.project-osrm.org/route/v1/driving/" + coordChain.join(";") + "?overview=false")
                    .then(r => r.json())
                    .then(data => {{
                        if (data && data.routes && data.routes.length > 0) {{
                            let legMeters = data.routes[0].legs[0].distance;
                            stops[curIdx].leg_miles = Math.round((legMeters * 0.000621371) * 10) / 10;
                            updateDeck();
                            showToast("⚡ Telemetry Synced!");
                        }}
                    }})
                    .catch(e => updateDeck());
            }}
        }}

        function executeFullReoptimize() {{
            closeReoptMenu();
            if (curIdx >= stops.length - 2) {{
                showToast("Route already optimal!");
                return;
            }}

            showToast("🔀 Solving road matrix...");

            let startLat = (liveCoords && liveCoords.lat) ? liveCoords.lat : stops[curIdx].lat;
            let startLon = (liveCoords && liveCoords.lon) ? liveCoords.lon : stops[curIdx].lon;

            let depotStop = stops[stops.length - 1].is_depot ? stops.pop() : null;
            let remaining = stops.splice(curIdx + 1);

            let coordList = [startLon + "," + startLat];
            for (let i = 0; i < remaining.length; i++) {{
                coordList.push(remaining[i].lon + "," + remaining[i].lat);
            }}

            fetch("https://router.project-osrm.org/table/v1/driving/" + coordList.join(";") + "?sources=all&destinations=all")
                .then(r => r.json())
                .then(matrixData => {{
                    if (matrixData && matrixData.durations) {{
                        let durations = matrixData.durations;
                        let unvisited = remaining.map((s, i) => ({{ stop: s, matrixIdx: i + 1 }}));
                        let currentMatrixIdx = 0;
                        let optimized = [];

                        while (unvisited.length > 0) {{
                            let bestIndex = 0;
                            let minDuration = Infinity;
                            for (let j = 0; j < unvisited.length; j++) {{
                                let d = durations[currentMatrixIdx][unvisited[j].matrixIdx];
                                if (d < minDuration) {{
                                    minDuration = d;
                                    bestIndex = j;
                                }}
                            }}
                            let nextStop = unvisited.splice(bestIndex, 1)[0];
                            currentMatrixIdx = nextStop.matrixIdx;
                            optimized.push(nextStop.stop);
                        }}

                        if (depotStop) optimized.push(depotStop);
                        stops = stops.concat(optimized);
                        updateDeck();
                        showToast("🔀 Route reordered for shortest drive!");
                    }} else {{
                        if (depotStop) remaining.push(depotStop);
                        stops = stops.concat(remaining);
                        updateDeck();
                    }}
                }})
                .catch(e => {{
                    if (depotStop) remaining.push(depotStop);
                    stops = stops.concat(remaining);
                    updateDeck();
                }});
        }}

        window.onload = updateDeck;
        updateDeck();
    </script>
</body>
</html>"""

            components.html(deck_html, height=680, scrolling=False)
